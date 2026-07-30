from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, assert_never, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from moco.codex.rpc import CodexRpcClient
from moco.codex.session import (
    CodexRealtimeSession,
    RealtimeErrorEvent,
    RealtimeEvent,
    TranscriptEvent,
)
from moco.config import MocoSettings
from moco.runtime.lifecycle import BusyKind, LifecycleController, LifecycleState
from moco.runtime.telemetry import safe_event
from moco.speech.irodori import IrodoriSynthesizer
from moco.speech.queue import SpeechQueue
from moco.speech.text import strip_control_emojis
from moco.web.messages import (
    ClientControl,
    ControlMessage,
    PlaybackMessage,
    StartMessage,
    StopMessage,
    parse_client_message,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from irodori_tts_infra.contracts import HealthResponse

    from moco.runtime.hotkeys import Control

STATIC_DIR = Path(__file__).with_name("static")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WEBSOCKET_PROTOCOL = "moco"
_CAPABILITY_PROTOCOL_PREFIX = f"{_WEBSOCKET_PROTOCOL}.capability."
_MAX_INVALID_MESSAGES = 3
logger = logging.getLogger(__name__)


class RealtimeSession(Protocol):
    @property
    def active_turn_id(self) -> str | None: ...

    async def start(self, sdp: str) -> str: ...

    def notifications(self) -> AsyncIterator[RealtimeEvent]: ...

    async def cancel_current(self) -> None: ...

    async def close(self) -> None: ...


class WebSynthesizer(Protocol):
    async def health(self) -> HealthResponse: ...

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
        session_factory: SessionFactory,
        synthesizer_factory: SynthesizerFactory,
    ) -> None:
        self._websocket = websocket
        self._settings = settings
        self._session_factory = session_factory
        self._synthesizer_factory = synthesizer_factory
        self._session: RealtimeSession | None = None
        self._synthesizer: WebSynthesizer | None = None
        self._speech: SpeechQueue | None = None
        self._notifications_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._resource_lock = asyncio.Lock()
        self._closed = False
        self._invalid_messages = 0
        self._generation = 0
        self._audio_id = 0
        self._transcripts: dict[str, str] = {}
        self._synthesis_busy = False
        self._delegated_busy = False
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
            self._invalid_messages += 1
            await self._send_error("invalid_message")
            return self._invalid_messages < _MAX_INVALID_MESSAGES
        self._invalid_messages = 0

        if isinstance(message, StartMessage):
            await self._start(message)
            return True
        if isinstance(message, ControlMessage):
            await self._apply_control(message.control)
            return True
        if isinstance(message, PlaybackMessage):
            self._lifecycle.set_busy(BusyKind.PLAYBACK, active=message.active)
            if message.active:
                self._lifecycle.set_state(LifecycleState.SPEAKING)
            return True
        if isinstance(message, StopMessage):
            return False
        assert_never(message)

    async def _start(self, message: StartMessage) -> None:
        if self._session is not None:
            await self._send_error("already_started")
            return
        await self._send_state(LifecycleState.CONNECTING)
        try:
            synthesizer = self._synthesizer_factory()
            session = self._session_factory()
            health = await synthesizer.health()
            if not health.model_loaded:
                await synthesizer.close()
                await session.close()
                await self._send_error("irodori_not_ready")
                return
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
            await self._send_error("conversation_start_failed")
            return

        self._synthesizer = synthesizer
        self._session = session
        self._speech = SpeechQueue(
            synthesizer,
            deliver=self._deliver_audio,
            max_chars=self._settings.speech.segment_max_chars,
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

    async def _apply_control(self, control: ClientControl) -> None:
        safe_event(
            logger,
            "control_received",
            component="web",
            control=control.value,
        )
        if control is ClientControl.PTT_DOWN:
            if self._session is None:
                await self._send_error("conversation_not_started")
                return
            self._generation += 1
            speech = self._speech
            if speech is not None:
                await speech.cancel()
            await self._session.cancel_current()
            self._lifecycle.ptt_down()
            await self._send_json({"type": "audio_invalidate", "generation": self._generation})
            await self._send_state(LifecycleState.RECORDING)
            return
        if control is ClientControl.PTT_UP:
            self._lifecycle.ptt_up()
            await self._send_state(LifecycleState.WORKING)
            return
        if control is ClientControl.CANCEL:
            self._generation += 1
            if self._speech is not None:
                await self._speech.cancel()
            if self._session is not None:
                await self._session.cancel_current()
            self._lifecycle.set_state(LifecycleState.CANCELLING)
            await self._send_json({"type": "audio_invalidate", "generation": self._generation})
            await self._send_state(LifecycleState.CANCELLING)
            return
        assert_never(control)

    async def _consume_notifications(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            async for event in session.notifications():
                if isinstance(event, RealtimeErrorEvent):
                    await self._send_error("codex_realtime_error")
                    await self._close_conversation_resources()
                    return
                if isinstance(event, TranscriptEvent):
                    await self._handle_transcript(event)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as error:
            _log_boundary_failure("realtime_events", error)
            with suppress(RuntimeError):
                await self._send_error("invalid_realtime_event")

    async def _handle_transcript(self, event: TranscriptEvent) -> None:
        delta, done = self._transcript_delta(event)
        await self._send_json(
            {
                "type": "transcript",
                "role": event.role,
                "delta": strip_control_emojis(delta),
                "done": done,
            },
        )
        speech = self._speech
        if speech is not None:
            await speech.on_transcript(role=event.role, delta=delta, done=done)
        self._lifecycle.touch()

    def _transcript_delta(self, event: TranscriptEvent) -> tuple[str, bool]:
        accumulated = self._transcripts.get(event.role, "")
        if event.kind == "delta":
            self._transcripts[event.role] = accumulated + event.text
            return event.text, False
        self._transcripts.pop(event.role, None)
        if event.text.startswith(accumulated):
            return event.text[len(accumulated) :], True
        if not accumulated or event.role == "user":
            return event.text, True
        message = "assistant transcript did not extend its deltas"
        raise RuntimeError(message)

    async def _deliver_audio(self, wav: bytes) -> None:
        self._audio_id += 1
        async with self._send_lock:
            await self._websocket.send_json(
                {
                    "type": "audio",
                    "audioId": self._audio_id,
                    "generation": self._generation,
                },
            )
            await self._websocket.send_bytes(wav)
        self._lifecycle.set_state(LifecycleState.SPEAKING)

    async def _idle_loop(self) -> None:
        interval = min(0.05, self._settings.runtime.idle_timeout_seconds / 2)
        while True:
            await asyncio.sleep(interval)
            speech_busy = self._speech is not None and self._speech.is_busy
            if speech_busy != self._synthesis_busy:
                self._synthesis_busy = speech_busy
                self._lifecycle.set_busy(BusyKind.SYNTHESIS, active=speech_busy)
            delegated_busy = (
                self._session is not None and self._session.active_turn_id is not None
            )
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
            notification_task = self._notifications_task
            self._notifications_task = None
            if (
                notification_task is not None
                and notification_task is not asyncio.current_task()
            ):
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
            if speech is not None:
                await speech.close()
            if session is not None:
                await session.close()
            if synthesizer is not None:
                await synthesizer.close()

    async def _send_state(self, state: LifecycleState) -> None:
        await self._send_json(
            {
                "type": "state",
                "state": state.value,
                "hotkeys": {
                    "pushToTalk": self._settings.hotkeys.push_to_talk,
                    "cancel": self._settings.hotkeys.cancel,
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
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.websocket("/ws")
    async def operator_socket(websocket: WebSocket) -> None:
        if not _origin_allowed(websocket) or not _capability_allowed(
            websocket,
            app.state.capability_token,
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=_WEBSOCKET_PROTOCOL)
        connection = _BrowserConnection(
            websocket,
            settings=resolved,
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


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin is None or host is None:
        return False
    origin_parts = urlsplit(origin)
    host_parts = urlsplit(f"//{host}")
    return (
        origin_parts.scheme in {"http", "https"}
        and origin_parts.path in {"", "/"}
        and not origin_parts.query
        and not origin_parts.fragment
        and origin_parts.hostname in _LOOPBACK_HOSTS
        and host_parts.hostname in _LOOPBACK_HOSTS
        and origin_parts.netloc.casefold() == host.casefold()
    )


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


def _log_boundary_failure(boundary: str, error: BaseException) -> None:
    logger.warning(
        "Boundary failure (boundary=%s, error_type=%s)",
        boundary,
        type(error).__name__,
    )
