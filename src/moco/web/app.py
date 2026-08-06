from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, assert_never, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from irodori_tts_infra.contracts import CapabilitiesResponse
from pydantic import ValidationError

from moco.codex.rpc import CodexRpcClient
from moco.codex.session import (
    ActivityEvent,
    CodexRealtimeSession,
    RealtimeErrorEvent,
    RealtimeEvent,
    ReasoningSummaryEvent,
    TranscriptEvent,
)
from moco.config import MocoSettings
from moco.runtime.lifecycle import BusyKind, LifecycleController, LifecycleState
from moco.runtime.telemetry import safe_event
from moco.speech.irodori import IrodoriError, IrodoriSynthesizer
from moco.speech.queue import SpeechQueue
from moco.speech.text import strip_control_emojis
from moco.web.messages import (
    ClientControl,
    ControlMessage,
    PlaybackMessage,
    SelectVoiceMessage,
    StartMessage,
    StopMessage,
    parse_client_message,
)
from moco.web.pairing import render_pairing_svg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from moco.runtime.hotkeys import Control

STATIC_DIR = Path(__file__).with_name("static")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WEBSOCKET_PROTOCOL = "moco"
_CAPABILITY_PROTOCOL_PREFIX = f"{_WEBSOCKET_PROTOCOL}.capability."
_MAX_INVALID_MESSAGES = 3
_CAPABILITY_POLL_INTERVAL_SECONDS = 1.0
_CAPABILITY_MISMATCH = "capability_mismatch"
_IRODORI_UNAVAILABLE = "irodori_unavailable"
_TERMINAL_READINESS = frozenset({"ready", "model_not_loaded", "voice_bank_invalid"})
_IRODORI_READINESS_CODES = frozenset(
    {"model_loading", "model_not_loaded", "voice_bank_invalid"},
)
_PROVISIONAL_SELECTION_ERRORS = frozenset(
    {"voice_catalog_empty", "configured_voice_unavailable", "voice_selection_required"},
)
_ACTIVITY_LABELS = {
    "turn": ("turn", "応答処理"),
    "reasoning": ("reasoning", "推論要約"),
    "command_execution": ("work", "コマンド実行"),
    "file_change": ("work", "ファイル変更"),
    "external_tool": ("work", "外部ツール"),
    "subagent": ("work", "サブエージェント"),
    "web_search": ("work", "Web 検索"),
    "image_view": ("work", "画像確認"),
    "image_generation": ("work", "画像生成"),
    "context_compaction": ("work", "コンテキスト整理"),
    "codex_work": ("work", "Codex 処理"),
}
logger = logging.getLogger(__name__)
_PlaybackState = Literal["delivering", "delivered", "started"]


class _CapabilityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RealtimeSession(Protocol):
    @property
    def active_turn_id(self) -> str | None: ...

    async def start(self, sdp: str) -> str: ...

    def notifications(self) -> AsyncIterator[RealtimeEvent]: ...

    async def close(self) -> None: ...


class WebSynthesizer(Protocol):
    async def capabilities(self) -> CapabilitiesResponse: ...

    def select_voice(self, voice_id: str) -> None: ...

    async def synthesize(self, text: str) -> bytes: ...

    async def close(self) -> None: ...


type SessionFactory = Callable[[], RealtimeSession]
type SynthesizerFactory = Callable[[], WebSynthesizer]


class ControlHub:
    def __init__(self) -> None:
        self._connection: _BrowserConnection | None = None
        self._lock = asyncio.Lock()

    async def register(self, connection: _BrowserConnection) -> bool:
        async with self._lock:
            if self._connection is not None:
                return False
            self._connection = connection
            return True

    async def unregister(self, connection: _BrowserConnection) -> None:
        async with self._lock:
            if self._connection is connection:
                self._connection = None

    async def publish(self, control: Control) -> None:
        async with self._lock:
            connection = self._connection
        if connection is not None:
            await connection.send_control(control)


class _BrowserConnection:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        settings: MocoSettings,
        global_hotkeys_active: bool,
        session_factory: SessionFactory,
        synthesizer_factory: SynthesizerFactory,
    ) -> None:
        self._websocket = websocket
        self._settings = settings
        self._global_hotkeys_active = global_hotkeys_active
        self._session_factory = session_factory
        self._synthesizer_factory = synthesizer_factory
        self._session: RealtimeSession | None = None
        self._synthesizer: WebSynthesizer | None = None
        self._speech: SpeechQueue | None = None
        self._notifications_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._capability_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._resource_lock = asyncio.Lock()
        self._capability_lock = asyncio.Lock()
        self._closed = False
        self._invalid_messages = 0
        self._generation = 0
        self._audio_id = 0
        self._playback_states: dict[tuple[int, int], _PlaybackState] = {}
        self._first_playback_started_ns: int | None = None
        self._first_playback_audio_id: int | None = None
        self._first_playback_generation: int | None = None
        self._transcripts: dict[str, str] = {}
        self._synthesis_busy = False
        self._delegated_busy = False
        self._voice_options: tuple[dict[str, object], ...] = ()
        self._voice_ready = False
        self._voice_readiness = "loading"
        self._emoji_supported = False
        self._voice_generation: str | None = None
        self._selected_voice_id: str | None = None
        self._voice_selection_error: str | None = None
        self._voice_selected_explicitly = False
        self._browser_state = LifecycleState.READY
        self._user_utterance_active = False
        self._lifecycle = LifecycleController(
            idle_timeout_seconds=settings.runtime.idle_timeout_seconds,
            on_expire=self._expire_conversation,
        )

    async def run(self) -> None:
        safe_event(
            logger,
            "operator_connected",
            component="web",
            state="ready",
        )
        await self._send_state(LifecycleState.READY)
        self._capability_task = asyncio.create_task(
            self._capability_loop(),
            name="moco-irodori-capabilities",
        )
        self._idle_task = asyncio.create_task(self._idle_loop(), name="moco-idle-loop")
        try:
            while True:
                payload = await self._websocket.receive_text()
                if not await self._handle(payload):
                    return
        except WebSocketDisconnect:
            return
        finally:
            await self.close()

    async def send_control(self, control: Control) -> None:
        await self._send_json({"type": "control", "control": control.value})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        capability_task = self._capability_task
        self._capability_task = None
        if capability_task is not None and capability_task is not asyncio.current_task():
            capability_task.cancel()
            with suppress(asyncio.CancelledError):
                await capability_task
        idle_task = self._idle_task
        self._idle_task = None
        if idle_task is not None and idle_task is not asyncio.current_task():
            idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await idle_task
        await self._close_conversation_resources()
        self._lifecycle.disable()
        safe_event(
            logger,
            "operator_disconnected",
            component="web",
            state="disabled",
        )

    async def _handle(self, payload: str) -> bool:
        try:
            message = parse_client_message(json.loads(payload))
        except (json.JSONDecodeError, ValidationError):
            return await self._reject_invalid_message()

        if isinstance(message, PlaybackMessage):
            if not await self._handle_playback(message):
                safe_event(
                    logger,
                    "browser_playback_rejected",
                    boundary="browser_audio",
                    component="web",
                    event_code="invalid_playback_transition",
                    result="error",
                    state="rejected",
                )
                return await self._reject_invalid_message()
        elif isinstance(message, StartMessage):
            await self._start(message)
        elif isinstance(message, ControlMessage):
            await self._apply_control(message.control)
        elif isinstance(message, SelectVoiceMessage):
            await self._select_voice(message.voice_id)
        elif isinstance(message, StopMessage):
            return False
        else:
            assert_never(message)
        self._invalid_messages = 0
        return True

    async def _reject_invalid_message(self) -> bool:
        self._invalid_messages += 1
        await self._send_error("invalid_message")
        return self._invalid_messages < _MAX_INVALID_MESSAGES

    async def _handle_playback(self, message: PlaybackMessage) -> bool:
        key = (message.audio_id, message.generation)
        state = self._playback_states.get(key)
        if state == "delivering":
            async with self._send_lock:
                pass
            state = self._playback_states.get(key)
        if message.generation != self._generation or state is None:
            return False
        if message.phase == "started":
            if state != "delivered":
                return False
            self._playback_states[key] = "started"
        else:
            expected = "started" if message.phase == "completed" else "delivered"
            if state != expected:
                return False
            del self._playback_states[key]

        active = any(value == "started" for value in self._playback_states.values())
        self._lifecycle.set_busy(BusyKind.PLAYBACK, active=active)
        if active:
            self._lifecycle.set_state(LifecycleState.SPEAKING)
        safe_event(
            logger,
            "browser_playback",
            **self._playback_attributes(message, active=active),
        )
        return True

    def _playback_attributes(
        self,
        message: PlaybackMessage,
        *,
        active: bool,
    ) -> dict[str, object]:
        attributes: dict[str, object] = {
            "component": "web",
            "boundary": "browser_audio",
            "phase": message.phase,
            "state": "active" if active else "inactive",
            "audio_id": message.audio_id,
            "generation": message.generation,
            "context_state": message.context_state,
        }
        if (
            message.phase in {"started", "failed"}
            and message.audio_id == self._first_playback_audio_id
            and message.generation == self._first_playback_generation
            and self._first_playback_started_ns is not None
        ):
            if message.phase == "started":
                attributes["duration_ms"] = _elapsed_ms(self._first_playback_started_ns)
            self._clear_first_playback_timing()
        if message.phase == "failed":
            attributes["result"] = "error"
        return attributes

    async def _capability_loop(self) -> None:
        synthesizer: WebSynthesizer | None = None
        try:
            try:
                synthesizer = self._synthesizer_factory()
            except (OSError, RuntimeError):
                await self._set_capability_failure("unavailable")
                return
            await self._poll_capabilities(synthesizer)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, WebSocketDisconnect):
            return
        finally:
            if synthesizer is not None:
                try:
                    await synthesizer.close()
                except Exception as error:  # noqa: BLE001
                    _log_boundary_failure("irodori_capability_cleanup", error)

    async def _poll_capabilities(self, synthesizer: WebSynthesizer) -> None:
        while True:
            try:
                capabilities = await self._fetch_capabilities(synthesizer)
            except _CapabilityError as error:
                await self._set_capability_failure(_readiness_for_capability_error(error.code))
                return
            conflict = await self._cache_capabilities(capabilities)
            await self._send_state(self._browser_state)
            if conflict is not None or capabilities.readiness in _TERMINAL_READINESS:
                return
            await asyncio.sleep(_CAPABILITY_POLL_INTERVAL_SECONDS)

    async def _fetch_capabilities(
        self,
        synthesizer: WebSynthesizer,
    ) -> CapabilitiesResponse:
        try:
            response = await synthesizer.capabilities()
            capabilities = CapabilitiesResponse.model_validate(
                response.model_dump(mode="python"),
                strict=True,
            )
        except IrodoriError as error:
            if error.code == "invalid_response":
                raise _CapabilityError(_CAPABILITY_MISMATCH) from error
            if error.code in _IRODORI_READINESS_CODES:
                raise _CapabilityError(error.code) from error
            raise _CapabilityError(_IRODORI_UNAVAILABLE) from error
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError) as error:
            raise _CapabilityError(_CAPABILITY_MISMATCH) from error
        except OSError as error:
            raise _CapabilityError(_IRODORI_UNAVAILABLE) from error
        safe_event(
            logger,
            "irodori_capabilities_received",
            contract_version=capabilities.contract_version,
            ready=capabilities.ready,
            readiness=capabilities.readiness,
            voice_count=len(capabilities.voices),
        )
        return capabilities

    async def _cache_capabilities(self, capabilities: CapabilitiesResponse) -> str | None:
        options = tuple(
            {"id": voice.id, "label": voice.label, "default": voice.default}
            for voice in capabilities.voices
        )
        voice_ids = {voice.id for voice in capabilities.voices}
        async with self._capability_lock:
            self._emoji_supported = capabilities.conditioning.emoji.supported
            cached_generation = self._voice_generation
            if cached_generation is not None and cached_generation != capabilities.generation:
                self._voice_options = options
                self._voice_ready = False
                self._voice_readiness = "capability_mismatch"
                self._selected_voice_id = None
                self._voice_selection_error = "runtime_generation_mismatch"
                safe_event(
                    logger,
                    "irodori_generation_mismatch",
                    event_code="runtime_generation_mismatch",
                )
                return "runtime_generation_mismatch"
            should_resolve_selection = cached_generation is None or (
                self._selected_voice_id is None
                and self._voice_selection_error in _PROVISIONAL_SELECTION_ERRORS
                and not self._voice_selected_explicitly
            )
            if should_resolve_selection:
                selected, selection_error = _resolve_voice_selection(
                    capabilities,
                    self._settings.irodori.speaker,
                )
                self._selected_voice_id = selected
                self._voice_selection_error = selection_error
            elif self._selected_voice_id is not None and self._selected_voice_id not in voice_ids:
                self._selected_voice_id = None
                self._voice_selection_error = "voice_not_found"
                self._voice_options = options
                self._voice_ready = capabilities.ready
                self._voice_readiness = capabilities.readiness
                return "voice_not_found"
            self._voice_generation = capabilities.generation
            self._voice_options = options
            self._voice_ready = capabilities.ready
            self._voice_readiness = capabilities.readiness
        return None

    async def _set_capability_failure(self, readiness: str) -> None:
        async with self._capability_lock:
            self._voice_ready = False
            self._voice_readiness = readiness
            if readiness == "capability_mismatch":
                self._voice_selection_error = "capability_mismatch"
            elif readiness in _IRODORI_READINESS_CODES:
                self._voice_selection_error = readiness
            else:
                self._voice_selection_error = "irodori_unavailable"
        await self._send_state(self._browser_state)

    async def _start(self, message: StartMessage) -> None:
        if self._session is not None:
            await self._send_error("already_started")
            return
        await self._send_state(LifecycleState.CONNECTING)
        synthesizer: WebSynthesizer | None = None
        session: RealtimeSession | None = None
        try:
            synthesizer = self._synthesizer_factory()
            preparation_error = await self._prepare_start_synthesizer(synthesizer)
            if preparation_error is not None:
                await self._fail_start(None, synthesizer, preparation_error)
                return
            session = self._session_factory()
            answer = await session.start(message.sdp)
        except (OSError, RuntimeError) as error:
            _log_boundary_failure("conversation_start", error)
            safe_event(
                logger,
                "conversation_start_failed",
                component="web",
                event_code="conversation_start_failed",
                result="error",
            )
            await self._fail_start(session, synthesizer, "conversation_start_failed")
            return

        self._synthesizer = synthesizer
        self._session = session
        self._speech = SpeechQueue(
            synthesizer,
            deliver=self._deliver_audio,
            max_chars=self._settings.speech.segment_max_chars,
            on_error=self._handle_speech_error,
            initial_generation=self._generation,
            reserve_audio_id=self._reserve_audio_id,
            first_segment_soft_break_min_chars=(
                self._settings.speech.first_segment_soft_break_min_chars
            ),
        )
        self._speech.start()
        self._lifecycle.enable()
        await self._send_json({"type": "sdp_answer", "sdp": answer})
        await self._send_state(LifecycleState.READY)
        self._notifications_task = asyncio.create_task(
            self._consume_notifications(),
            name="moco-realtime-events",
        )
        safe_event(
            logger,
            "conversation_ready",
            component="web",
            state="ready",
            result="ok",
        )

    async def _prepare_start_synthesizer(self, synthesizer: WebSynthesizer) -> str | None:
        try:
            capabilities = await self._fetch_capabilities(synthesizer)
        except _CapabilityError as error:
            return error.code
        preparation_error = await self._cache_capabilities(capabilities)
        if preparation_error is None:
            preparation_error = _start_voice_error(
                capabilities,
                selection_error=self._voice_selection_error,
                selected_voice_id=self._selected_voice_id,
            )
        if preparation_error is not None:
            return preparation_error
        selected_voice_id = cast("str", self._selected_voice_id)
        try:
            synthesizer.select_voice(selected_voice_id)
        except IrodoriError as error:
            return error.code
        return None

    async def _fail_start(
        self,
        session: RealtimeSession | None,
        synthesizer: WebSynthesizer | None,
        error_code: str,
    ) -> None:
        await _close_start_resources(session, synthesizer)
        await self._send_error(error_code)
        self._lifecycle.disable()
        self._lifecycle.set_state(LifecycleState.IDLE_EXPIRED)
        await self._send_state(LifecycleState.IDLE_EXPIRED)

    async def _apply_control(self, control: ClientControl) -> None:
        safe_event(
            logger,
            "control_received",
            component="web",
            control=control.value,
        )
        if control is ClientControl.LISTEN_START:
            if self._session is None:
                await self._send_error("conversation_not_started")
                return
            self._lifecycle.listen_start()
            await self._send_state(LifecycleState.LISTENING)
            return
        if control is ClientControl.LISTEN_STOP:
            if self._session is None:
                self._lifecycle.disable()
                self._lifecycle.set_state(LifecycleState.IDLE_EXPIRED)
                await self._send_state(LifecycleState.IDLE_EXPIRED)
                return
            self._lifecycle.listen_stop()
            await self._send_state(LifecycleState.READY)
            return
        assert_never(control)

    async def _select_voice(self, voice_id: str) -> None:
        async with self._capability_lock:
            available = any(option["id"] == voice_id for option in self._voice_options)
            if not available:
                selection_failed = True
            elif self._synthesizer is not None:
                try:
                    self._synthesizer.select_voice(voice_id)
                except IrodoriError:
                    selection_failed = True
                else:
                    selection_failed = False
            else:
                selection_failed = False
            if not selection_failed:
                self._selected_voice_id = voice_id
                self._voice_selection_error = None
                self._voice_selected_explicitly = True
        if selection_failed:
            await self._send_error("voice_not_available")
            return
        await self._send_json({"type": "voice", "selected": voice_id})

    async def _consume_notifications(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            async for event in session.notifications():
                if isinstance(event, RealtimeErrorEvent):
                    await self._terminate_conversation("codex_realtime_error")
                    return
                if isinstance(event, ActivityEvent):
                    await self._send_activity(event)
                    continue
                if isinstance(event, ReasoningSummaryEvent):
                    await self._send_reasoning_summary(event)
                    continue
                if isinstance(event, TranscriptEvent):
                    await self._handle_transcript(event)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as error:
            _log_boundary_failure("realtime_events", error)
            with suppress(RuntimeError):
                await self._terminate_conversation("invalid_realtime_event")

    async def _terminate_conversation(self, error_code: str) -> None:
        await self._send_error(error_code)
        await self._close_conversation_resources()
        self._lifecycle.disable()
        self._lifecycle.set_state(LifecycleState.IDLE_EXPIRED)
        await self._send_state(LifecycleState.IDLE_EXPIRED)

    async def _handle_speech_error(self, code: str) -> None:
        if code == "runtime_generation_mismatch":
            safe_event(
                logger,
                "irodori_generation_mismatch",
                event_code=code,
            )
        await self._send_error(code)

    async def _send_activity(self, event: ActivityEvent) -> None:
        kind, label = _ACTIVITY_LABELS[event.kind]
        await self._send_json(
            {
                "type": "activity",
                "kind": kind,
                "phase": event.phase,
                "label": label,
                "occurredAtMs": (
                    event.occurred_at_ms if event.occurred_at_ms is not None else _now_ms()
                ),
            },
        )

    async def _send_reasoning_summary(self, event: ReasoningSummaryEvent) -> None:
        await self._send_json(
            {
                "type": "reasoning_summary",
                "itemId": event.item_id,
                "delta": _display_text(event.delta)[:500],
                "occurredAtMs": _now_ms(),
            },
        )

    async def _handle_transcript(self, event: TranscriptEvent) -> None:
        if event.role == "user" and not self._user_utterance_active:
            self._user_utterance_active = True
            self._transcripts.pop("assistant", None)
            await self._invalidate_speech()
        if event.role == "assistant":
            accumulated = self._transcripts.get(event.role, "")
            if event.role not in self._transcripts:
                self._clear_first_playback_timing()
            if event.text and not accumulated and self._first_playback_started_ns is None:
                self._first_playback_started_ns = time.monotonic_ns()
        text, speech_delta, done = self._transcript_update(event)
        await self._send_json(
            {
                "type": "transcript",
                "role": event.role,
                "text": strip_control_emojis(text),
                "done": done,
            },
        )
        speech = self._speech
        if speech is not None:
            await speech.on_transcript(role=event.role, delta=speech_delta, done=done)
        if event.role == "user" and done:
            self._user_utterance_active = False
        self._lifecycle.touch()

    async def _invalidate_speech(self) -> None:
        self._generation += 1
        self._clear_first_playback_timing()
        self._clear_playback_states()
        speech_invalidation = (
            asyncio.create_task(
                self._speech.invalidate(reason="user_transcript"),
                name="moco-speech-invalidation",
            )
            if self._speech is not None
            else None
        )
        try:
            await self._send_json(
                {"type": "audio_invalidate", "generation": self._generation},
            )
        finally:
            if speech_invalidation is not None:
                await speech_invalidation

    def _transcript_update(self, event: TranscriptEvent) -> tuple[str, str, bool]:
        accumulated = self._transcripts.get(event.role, "")
        if event.kind == "delta":
            text = accumulated + event.text
            self._transcripts[event.role] = text
            speech_delta = event.text if event.role == "assistant" else ""
            return text, speech_delta, False
        self._transcripts.pop(event.role, None)
        if event.role == "user":
            return event.text, "", True
        if accumulated and not event.text.startswith(accumulated):
            message = "assistant transcript did not extend its deltas"
            raise RuntimeError(message)
        return event.text, event.text[len(accumulated) :], True

    def _reserve_audio_id(self) -> int:
        self._audio_id += 1
        if self._first_playback_started_ns is not None and self._first_playback_audio_id is None:
            self._first_playback_audio_id = self._audio_id
            self._first_playback_generation = self._generation
        return self._audio_id

    def _clear_first_playback_timing(self) -> None:
        self._first_playback_started_ns = None
        self._first_playback_audio_id = None
        self._first_playback_generation = None

    def _clear_playback_states(self) -> None:
        self._playback_states.clear()
        self._lifecycle.set_busy(BusyKind.PLAYBACK, active=False)

    async def _deliver_audio(self, wav: bytes, audio_id: int, generation: int) -> None:
        started_ns = time.monotonic_ns()
        key = (audio_id, generation)
        metadata = {
            "component": "web",
            "boundary": "browser_audio",
            "audio_id": audio_id,
            "generation": generation,
            "wav_bytes": len(wav),
        }
        safe_event(logger, "audio_delivery_started", **metadata)
        try:
            async with self._send_lock:
                await self._websocket.send_json(
                    {
                        "type": "audio",
                        "audioId": audio_id,
                        "generation": generation,
                    },
                )
                if generation == self._generation:
                    self._playback_states[key] = "delivering"
                try:
                    await self._websocket.send_bytes(wav)
                except (asyncio.CancelledError, Exception):
                    if self._playback_states.get(key) == "delivering":
                        del self._playback_states[key]
                    raise
                if (
                    generation == self._generation
                    and self._playback_states.get(key) == "delivering"
                ):
                    self._playback_states[key] = "delivered"
        except asyncio.CancelledError:
            safe_event(
                logger,
                "audio_delivery_cancelled",
                **metadata,
                duration_ms=_elapsed_ms(started_ns),
            )
            raise
        except Exception:
            safe_event(
                logger,
                "audio_delivery_failed",
                **metadata,
                duration_ms=_elapsed_ms(started_ns),
                event_code="audio_delivery_failed",
                result="error",
            )
            raise
        safe_event(
            logger,
            "audio_delivery_completed",
            **metadata,
            duration_ms=_elapsed_ms(started_ns),
            result="ok",
        )
        if generation == self._generation:
            self._lifecycle.set_state(LifecycleState.SPEAKING)

    async def _idle_loop(self) -> None:
        interval = min(0.05, self._settings.runtime.idle_timeout_seconds / 2)
        while True:
            await asyncio.sleep(interval)
            speech_busy = self._speech is not None and self._speech.is_busy
            if speech_busy != self._synthesis_busy:
                self._synthesis_busy = speech_busy
                self._lifecycle.set_busy(BusyKind.SYNTHESIS, active=speech_busy)
                await self._send_json(
                    {
                        "type": "activity",
                        "kind": "voice",
                        "phase": "started" if speech_busy else "completed",
                        "label": "音声生成",
                        "occurredAtMs": _now_ms(),
                    },
                )
            delegated_busy = self._session is not None and self._session.active_turn_id is not None
            if delegated_busy != self._delegated_busy:
                self._delegated_busy = delegated_busy
                self._lifecycle.set_busy(BusyKind.DELEGATED, active=delegated_busy)
            await self._lifecycle.poll()

    async def _expire_conversation(self) -> None:
        await self._close_conversation_resources()
        await self._send_state(LifecycleState.IDLE_EXPIRED)
        safe_event(
            logger,
            "conversation_expired",
            component="runtime",
            state="idle_expired",
        )

    async def _close_conversation_resources(self) -> None:
        async with self._resource_lock:
            self._clear_first_playback_timing()
            self._clear_playback_states()
            notification_task = self._notifications_task
            self._notifications_task = None
            if notification_task is not None and notification_task is not asyncio.current_task():
                notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await notification_task

            speech, session, synthesizer = (
                self._speech,
                self._session,
                self._synthesizer,
            )
            self._speech = None
            self._session = None
            self._synthesizer = None
            self._transcripts.clear()
            self._user_utterance_active = False
            if speech is not None:
                await speech.close()
            if session is not None:
                await session.close()
            if synthesizer is not None:
                await synthesizer.close()

    async def _send_state(self, state: LifecycleState) -> None:
        self._browser_state = state
        await self._send_json(
            {
                "type": "state",
                "state": state.value,
                "hotkeys": {
                    "enabled": self._global_hotkeys_active,
                    "startListening": self._settings.hotkeys.start_listening,
                    "stopListening": self._settings.hotkeys.stop_listening,
                },
                "voice": {
                    "selected": self._selected_voice_id,
                    "options": list(self._voice_options),
                    "ready": self._voice_ready,
                    "readiness": self._voice_readiness,
                },
                "conditioning": {
                    "captionMode": "off",
                    "deliveryCaptionSupported": False,
                    "emojiSupported": self._emoji_supported,
                },
            },
        )

    async def _send_error(self, code: str) -> None:
        await self._send_json({"type": "error", "code": code})

    async def _send_json(self, message: dict[str, object]) -> None:
        async with self._send_lock:
            await self._websocket.send_json(message)


def create_app(
    settings: MocoSettings | None = None,
    *,
    session_factory: SessionFactory | None = None,
    synthesizer_factory: SynthesizerFactory | None = None,
    capability_token: str | None = None,
    global_hotkeys_active: bool | None = None,
) -> FastAPI:
    resolved = settings or MocoSettings()
    build_session = session_factory or _codex_session_factory(resolved)
    build_synthesizer = synthesizer_factory or cast(
        "SynthesizerFactory",
        lambda: IrodoriSynthesizer.from_settings(resolved),
    )
    control_hub = ControlHub()
    app = FastAPI(title="moco", docs_url=None, redoc_url=None)
    app.state.capability_token = capability_token or secrets.token_urlsafe(32)
    app.state.control_hub = control_hub
    app.state.global_hotkeys_active = (
        resolved.hotkeys.enabled if global_hotkeys_active is None else global_hotkeys_active
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.head("/pairing.svg", include_in_schema=False)
    async def pairing_status(request: Request) -> Response:
        if resolved.server.public_url is None or not _pairing_request_allowed(
            request,
            app.state.capability_token,
        ):
            raise HTTPException(status_code=404)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get("/pairing.svg", include_in_schema=False)
    async def pairing_svg(request: Request) -> Response:
        public_url = resolved.server.public_url
        if public_url is None or not _pairing_request_allowed(
            request,
            app.state.capability_token,
        ):
            raise HTTPException(status_code=404)
        return Response(
            render_pairing_svg(public_url, app.state.capability_token),
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.websocket("/ws")
    async def operator_socket(websocket: WebSocket) -> None:
        if not _origin_allowed(websocket, resolved.server.public_url) or not _capability_allowed(
            websocket,
            app.state.capability_token,
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=_WEBSOCKET_PROTOCOL)
        connection = _BrowserConnection(
            websocket,
            settings=resolved,
            global_hotkeys_active=app.state.global_hotkeys_active,
            session_factory=build_session,
            synthesizer_factory=build_synthesizer,
        )
        if not await control_hub.register(connection):
            await websocket.send_json(
                {"type": "error", "code": "single_operator_only"},
            )
            await websocket.close(code=1008)
            return
        try:
            await connection.run()
        finally:
            await control_hub.unregister(connection)

    return app


def _codex_session_factory(settings: MocoSettings) -> SessionFactory:
    def build() -> RealtimeSession:
        rpc = CodexRpcClient(settings.codex.binary)
        return CodexRealtimeSession(rpc, settings=settings)

    return build


def _origin_allowed(websocket: WebSocket, public_url: str | None) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin is None or host is None:
        return False
    origin_parts = urlsplit(origin)
    host_parts = urlsplit(f"//{host}")
    if (
        origin_parts.path not in {"", "/"}
        or origin_parts.query
        or origin_parts.fragment
        or origin_parts.username is not None
        or origin_parts.password is not None
    ):
        return False
    local = (
        origin_parts.scheme == "http"
        and origin_parts.hostname in _LOOPBACK_HOSTS
        and host_parts.hostname in _LOOPBACK_HOSTS
        and origin_parts.netloc.casefold() == host.casefold()
    )
    public = (
        public_url is not None
        and origin.casefold().rstrip("/") == public_url
        and host.casefold() == urlsplit(public_url).netloc.casefold()
    )
    return local or public


def _capability_allowed(websocket: WebSocket, expected_token: str) -> bool:
    offered = websocket.headers.get("sec-websocket-protocol", "")
    protocols = {value.strip() for value in offered.split(",")}
    candidate = next(
        (
            value.removeprefix(_CAPABILITY_PROTOCOL_PREFIX)
            for value in protocols
            if value.startswith(_CAPABILITY_PROTOCOL_PREFIX)
        ),
        None,
    )
    return (
        _WEBSOCKET_PROTOCOL in protocols
        and candidate is not None
        and secrets.compare_digest(candidate, expected_token)
    )


def _pairing_request_allowed(request: Request, expected_token: str) -> bool:
    host = urlsplit(f"//{request.headers.get('host', '')}").hostname
    candidate = request.headers.get("x-moco-capability")
    fetch_site = request.headers.get("sec-fetch-site")
    return (
        host in _LOOPBACK_HOSTS
        and candidate is not None
        and secrets.compare_digest(candidate, expected_token)
        and fetch_site in {None, "same-origin"}
    )


def _log_boundary_failure(boundary: str, error: BaseException) -> None:
    logger.warning(
        "Boundary failure (boundary=%s, error_type=%s)",
        boundary,
        type(error).__name__,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _elapsed_ms(started_ns: int) -> int:
    return (time.monotonic_ns() - started_ns) // 1_000_000


def _display_text(text: str) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in strip_control_emojis(text)
    )
    return " ".join(printable.split())


def _resolve_voice_selection(
    capabilities: CapabilitiesResponse,
    configured: str | None,
) -> tuple[str | None, str | None]:
    if not capabilities.voices:
        return None, "voice_catalog_empty"
    if configured is not None:
        canonical = next(
            (voice.id for voice in capabilities.voices if voice.id == configured),
            None,
        )
        if canonical is not None:
            return canonical, None
        aliases = [voice.id for voice in capabilities.voices if configured in voice.aliases]
        if len(aliases) == 1:
            return aliases[0], None
        return None, "configured_voice_unavailable"
    defaults = [voice.id for voice in capabilities.voices if voice.default]
    if len(defaults) == 1:
        return defaults[0], None
    return None, "voice_selection_required"


def _readiness_for_capability_error(code: str) -> str:
    if code == _CAPABILITY_MISMATCH:
        return _CAPABILITY_MISMATCH
    if code in _IRODORI_READINESS_CODES:
        return code
    return "unavailable"


def _start_voice_error(
    capabilities: CapabilitiesResponse,
    *,
    selection_error: str | None,
    selected_voice_id: str | None,
) -> str | None:
    if not capabilities.ready:
        return capabilities.readiness
    if selection_error is not None:
        return selection_error
    if selected_voice_id is None:
        return "voice_selection_required"
    return None


async def _close_start_resources(
    session: RealtimeSession | None,
    synthesizer: WebSynthesizer | None,
) -> None:
    for boundary, resource in (
        ("realtime_start_cleanup", session),
        ("irodori_start_cleanup", synthesizer),
    ):
        if resource is None:
            continue
        try:
            await resource.close()
        except Exception as error:  # noqa: BLE001
            _log_boundary_failure(boundary, error)
