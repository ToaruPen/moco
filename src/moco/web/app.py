from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, assert_never, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from irodori_tts_infra.contracts import CapabilitiesResponse
from pydantic import ValidationError

from moco.codex.agent import AgentActivityEvent, AgentSession
from moco.codex.broker import InteractionBroker
from moco.codex.capabilities import (
    CapabilityDiscovery,
    CapabilitySnapshot,
    CapabilityStatus,
)
from moco.codex.connection import CodexConnectionSupervisor
from moco.codex.schema import CodexSchemaProbe, ServerRequestCategory
from moco.codex.session import (
    ActivityEvent,
    CodexRealtimeSession,
    RealtimeErrorEvent,
    RealtimeEvent,
    ReasoningSummaryEvent,
    TranscriptEvent,
    load_realtime_prompt,
)
from moco.config import MocoSettings, canonical_browser_loopback_host
from moco.errors import CodexReviewError, CodexRpcError
from moco.platform import resolve_codex_command
from moco.runtime._cleanup import await_cleanup
from moco.runtime.coordinator import (
    ConnectionState,
    InteractionCoordinator,
    InteractionEffects,
    InteractionSnapshot,
    SpeechState,
    TaskState,
    TurnResult,
    VoiceState,
)
from moco.runtime.lifecycle import IdleLeaseTimer, LifecycleState
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
    VoiceLostMessage,
    parse_client_message,
)
from moco.web.pairing import render_pairing_svg
from moco.web.review import ReviewGate
from moco.web.reviewer import ReviewerBroker, serve_reviewer_socket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from moco.codex.approval import ApprovalDecision
    from moco.codex.broker import ReviewerConnection
    from moco.codex.rpc import JsonValue
    from moco.runtime.hotkeys import Control

STATIC_DIR = Path(__file__).with_name("static")
_WEBSOCKET_PROTOCOL = "moco"
_CAPABILITY_PROTOCOL_PREFIX = f"{_WEBSOCKET_PROTOCOL}.capability."
_MAX_INVALID_MESSAGES = 3
_CAPABILITY_POLL_INTERVAL_SECONDS = 1.0
_CONNECTION_LOSS_SETTLEMENT_SECONDS = 5.0
_MAX_PENDING_AGENT_ACTIVITIES = 64
_MAX_PENDING_TRANSCRIPTS = 64
_MAX_USER_TRANSCRIPT_BYTES = 65_536
_MAX_USER_TRANSCRIPT_PARTS = 256


async def _resolved_result(value: object) -> object:
    return value


_CAPABILITY_MISMATCH = "capability_mismatch"
_IRODORI_UNAVAILABLE = "irodori_unavailable"
_REVIEW_UNAVAILABLE = "local review is unavailable"
_TERMINAL_READINESS = frozenset({"ready", "model_not_loaded", "voice_bank_invalid"})
_IRODORI_READINESS_CODES = frozenset(
    {"model_loading", "model_not_loaded", "voice_bank_invalid"},
)
_PROVISIONAL_SELECTION_ERRORS = frozenset(
    {"voice_catalog_empty", "configured_voice_unavailable", "voice_selection_required"},
)
_TURN_FAILURE_SPEECH = {
    "agent_turn_failed": "処理に失敗しました。",
    "agent_turn_interrupted": "処理を中断しました。",
    "agent_outcome_unknown": "処理結果を確認できませんでした。",
}
_GENERIC_TURN_FAILURE_SPEECH = "処理を完了できませんでした。"
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
    def voice_generation(self) -> int: ...

    @property
    def voice_active(self) -> bool: ...

    @property
    def interaction_snapshot(self) -> InteractionSnapshot | None: ...

    async def start(self, sdp: str) -> str: ...

    async def replace_voice(self, sdp: str) -> str: ...

    async def close_voice(
        self,
        expected_generation: int,
        *,
        on_claimed: Callable[[], None],
    ) -> bool: ...

    def bind_effects(self, effects: _ConversationEffectSink) -> None: ...

    def notifications(
        self,
        expected_generation: int | None = None,
    ) -> AsyncIterator[RealtimeEvent]: ...

    async def cancel_turn(self) -> bool: ...

    def listen_started(self) -> None: ...

    def listen_stopped(self) -> None: ...

    def consume_user_final(
        self,
        text: str,
        *,
        utterance_id: int | None = None,
    ) -> Awaitable[object]: ...

    def speech_changed(self, state: SpeechState) -> None: ...

    def claim_close(self) -> None: ...

    async def close(self) -> None: ...


class _ConversationEffectSink(InteractionEffects, Protocol):
    def on_agent_activity(self, event: AgentActivityEvent) -> None: ...


class _AsyncClosable(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _UtteranceSpeechClaim:
    invalidation: asyncio.Task[None] | None
    stopped: asyncio.Event


@dataclass(frozen=True, slots=True)
class _UserFinalClaim:
    handoff: Awaitable[object] | None
    target_session: RealtimeSession | None


@dataclass(frozen=True, slots=True)
class _TranscriptWork:
    text: str
    done: bool
    user_final: _UserFinalClaim | None
    speech_claim: _UtteranceSpeechClaim | None
    session: RealtimeSession | None
    expected_generation: int | None
    presented: asyncio.Future[None]
    completion: asyncio.Future[None]


class _ReviewerBrokerSlot:
    """Forward the one local reviewer only to the currently published lease broker."""

    def __init__(self) -> None:
        self._broker: ReviewerBroker | None = None

    def bind(self, broker: ReviewerBroker) -> None:
        if self._broker is not None:
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        self._broker = broker

    def release(self, broker: ReviewerBroker) -> None:
        if self._broker is broker:
            self._broker = None

    def connect_reviewer(self) -> ReviewerConnection:
        broker = self._broker
        if broker is None:
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        return broker.connect_reviewer()

    def disconnect_reviewer(self, connection: ReviewerConnection) -> None:
        broker = self._broker
        if broker is None:
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        broker.disconnect_reviewer(connection)

    def decide(
        self,
        connection: ReviewerConnection,
        handle: str,
        decision: ApprovalDecision,
    ) -> None:
        broker = self._broker
        if broker is None:
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        broker.decide(connection, handle, decision)


class _ConversationEffects:
    def __init__(
        self,
        owner: _CodexConversationOwner,
        downstream: InteractionEffects,
    ) -> None:
        self._owner = owner
        self._downstream = downstream

    def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
        self._downstream.on_snapshot_changed(snapshot)

    def on_turn_terminal_claimed(self) -> None:
        self._owner._cancel_pending_reviews()  # noqa: SLF001
        self._downstream.on_turn_terminal_claimed()

    def on_turn_finished(self, result: TurnResult) -> None:
        self._downstream.on_turn_finished(result)

    def on_submission_error(self, code: str) -> None:
        self._downstream.on_submission_error(code)


class _CodexConversationOwner:
    """Own one Codex connection and lend it to one Voice realtime session."""

    def __init__(
        self,
        settings: MocoSettings,
        *,
        connection: CodexConnectionSupervisor,
        working_directory: Path,
        contract_probe: CodexSchemaProbe | None = None,
        reviewer_slot: _ReviewerBrokerSlot | None = None,
    ) -> None:
        self._settings = settings
        self._connection = connection
        self._contract_probe = contract_probe
        self._reviewer_slot = reviewer_slot
        self._working_directory = working_directory
        self._effects: _ConversationEffectSink | None = None
        self._broker: InteractionBroker | None = None
        self._pending_reviews_cancelled = False
        self._agent: AgentSession | None = None
        self._coordinator: InteractionCoordinator | None = None
        self._connection_terminated = False
        self._reviewer_bound = False
        self._voice_generation_number = 0
        self._voice_active = False
        self._voice_capabilities: CapabilitySnapshot | None = None
        self._voice_prompt: str | None = None
        self._voice_operation_lock = asyncio.Lock()
        self._connection_loss_close_task: asyncio.Task[None] | None = None
        self._voice: CodexRealtimeSession | None = None
        self._starting_voice: CodexRealtimeSession | None = None
        self._started = False
        self._closing = False
        self._closed = False
        self._voice_close_attempted = False
        self._connection_close_attempted = False
        self._startup_cancelled_for_close = False
        self._state_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[str] | None = None
        self._discovery_task: asyncio.Task[CapabilitySnapshot] | None = None

    @property
    def interaction_snapshot(self) -> InteractionSnapshot | None:
        coordinator = self._coordinator
        return None if coordinator is None else coordinator.snapshot

    async def cancel_turn(self) -> bool:
        coordinator = self._coordinator
        if coordinator is None:
            return False
        if not self._connection_terminated:
            self._cancel_pending_reviews()
        return await coordinator.cancel_turn()

    def _cancel_pending_reviews(self) -> None:
        if self._pending_reviews_cancelled:
            return
        broker = self._broker
        if broker is not None:
            broker.cancel_pending()
            self._pending_reviews_cancelled = True

    def _review_count_changed(self, count: int) -> None:
        try:
            if count > 0:
                self._pending_reviews_cancelled = False
                if self._closing or self._closed:
                    self._cancel_pending_reviews()
                    return
            coordinator = self._coordinator
            if coordinator is not None:
                coordinator.review_count_changed(count)
        except BaseException:  # terminalize the owner boundary before Broker
            self._connection_terminal()
            raise

    def claim_close(self) -> None:
        if self._closed:
            return
        self._closing = True
        if not self._connection_terminated:
            self._cancel_pending_reviews()

    def listen_started(self) -> None:
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.listen_started()

    def listen_stopped(self) -> None:
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.listen_stopped()

    def consume_user_final(
        self,
        text: str,
        *,
        utterance_id: int | None = None,
    ) -> Awaitable[object]:
        coordinator = self._coordinator
        if coordinator is None:
            return _resolved_result(None)
        return coordinator.consume_user_final(text, utterance_id=utterance_id)

    def speech_changed(self, state: SpeechState) -> None:
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.speech_changed(state)

    def _agent_activity(self, event: AgentActivityEvent) -> None:
        effects = self._effects
        if effects is None or self._closing or self._closed or self._connection_terminated:
            return
        effects.on_agent_activity(event)

    def _agent_terminal(self) -> None:
        if not self._connection_terminated:
            self._cancel_pending_reviews()
        self._connection_terminal()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def voice_generation(self) -> int:
        return self._voice_generation_number

    @property
    def voice_active(self) -> bool:
        return self._voice_active

    def bind_effects(self, effects: _ConversationEffectSink) -> None:
        if self._started or self._effects is not None:
            message = "interaction effects must be bound once before start"
            raise RuntimeError(message)
        self._effects = effects

    async def start(self, sdp: str) -> str:
        async with self._state_lock:
            if self._started:
                message = "Codex conversation has already been started"
                raise CodexRpcError(message)
            if self._closing or self._closed:
                message = "Codex conversation is closed"
                raise CodexRpcError(message)
            self._started = True
            startup_task = asyncio.create_task(
                self._start_once(sdp),
                name="moco-codex-conversation-start",
            )
            self._startup_task = startup_task
        try:
            answer = await startup_task
            await self._ensure_open()
        except BaseException as error:
            closing_before_cleanup = self._closing
            cleanup_error, cleanup_cancellation = await await_cleanup(self.close())
            if cleanup_error is not None:
                _log_boundary_failure("codex_start_cleanup", cleanup_error)
            if cleanup_cancellation is not None:
                _log_boundary_failure("codex_start_cleanup", cleanup_cancellation)
            if self._startup_cancelled_before_close(
                closing_before_cleanup=closing_before_cleanup,
            ):
                message = "Codex conversation is closed"
                raise CodexRpcError(message) from error
            raise
        else:
            return answer

    def _startup_cancelled_before_close(self, *, closing_before_cleanup: bool) -> bool:
        # close() may claim cancellation while start() is awaiting cleanup.
        return self._startup_cancelled_for_close and closing_before_cleanup

    async def _start_once(self, sdp: str) -> str:
        prompt = load_realtime_prompt(self._settings)
        effects = self._effects
        probe = self._contract_probe
        slot = self._reviewer_slot
        if effects is None or probe is None or slot is None:
            message = "Codex conversation composition is incomplete"
            raise CodexRpcError(message)
        contract = await probe.probe()
        broker = InteractionBroker(contract)
        self._broker = broker
        broker.register_approval_handlers(self._connection)
        self._connection.register_terminal_callback(self._connection_terminal)
        await self._connection.start()
        if self._connection_terminated:
            message = "Codex connection ended during startup"
            raise CodexRpcError(message)
        discovery = CapabilityDiscovery(
            self._connection,
            contract=contract,
            working_directory=self._working_directory,
        )
        capabilities = await discovery.discover()
        readiness = _conversation_readiness(contract, capabilities)
        agent = AgentSession(
            self._connection,
            contract,
            capabilities,
            self._working_directory,
            self._settings.agent.profile,
            activity_sink=self._agent_activity,
            terminal_sink=self._agent_terminal,
        )
        self._agent = agent
        broker.bind_active_turn_check(agent.owns_active_turn)
        owner_effects = _ConversationEffects(self, effects)
        coordinator = InteractionCoordinator(
            agent,
            steer_available=capabilities.steer.status is CapabilityStatus.AVAILABLE,
            effects=owner_effects,
        )
        self._coordinator = coordinator
        broker.bind_pending_count_changed(self._review_count_changed)
        self._voice_capabilities = capabilities
        self._voice_prompt = prompt
        voice = CodexRealtimeSession(
            self._connection,
            settings=self._settings,
            capabilities=capabilities,
            working_directory=self._working_directory,
            prompt=prompt,
        )
        async with self._state_lock:
            if self._closing or self._closed or self._connection_terminated:
                message = "Codex conversation is closed"
                raise CodexRpcError(message)
            self._starting_voice = voice
            self._voice_generation_number = 1
        answer = await voice.start(sdp)
        await self._publish_voice(voice)
        slot.bind(broker)
        self._reviewer_bound = True
        coordinator.connection_changed(readiness)
        return answer

    def _connection_terminal(self) -> None:
        if self._connection_terminated:
            return
        self._connection_terminated = True
        self._pending_reviews_cancelled = True
        if self._closing or self._closed:
            return
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.connection_lost()
        elif self._effects is not None:
            try:
                self._effects.on_snapshot_changed(
                    InteractionSnapshot(
                        connection=ConnectionState.DISCONNECTED,
                        voice=VoiceState.IDLE,
                        task=TaskState.NONE,
                        speech=SpeechState.SILENT,
                    )
                )
            except BaseException as error:  # noqa: BLE001 - terminal callback continues
                _log_boundary_failure("codex_connection_terminal_effect", error)
        if self._started:
            close_task = asyncio.create_task(
                self.close(),
                name="moco-codex-connection-lost-close",
            )
            self._connection_loss_close_task = close_task
            close_task.add_done_callback(self._finish_connection_loss_close)

    def _finish_connection_loss_close(self, task: asyncio.Task[None]) -> None:
        if task is self._connection_loss_close_task:
            self._connection_loss_close_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 - background task boundary
            _log_boundary_failure("codex_connection_lost_close", error)

    async def _publish_voice(self, voice: CodexRealtimeSession) -> None:
        async with self._state_lock:
            if self._closing or self._closed or self._connection_terminated:
                message = "Codex conversation is closed"
                raise CodexRpcError(message)
            self._voice = voice
            self._starting_voice = None
            if self._voice_generation_number == 0:
                self._voice_generation_number = 1
            self._voice_active = True

    async def _ensure_open(self) -> None:
        async with self._state_lock:
            if self._closing or self._closed or self._connection_terminated:
                message = "Codex conversation is closed"
                raise CodexRpcError(message)

    def notifications(self, expected_generation: int | None = None) -> AsyncIterator[RealtimeEvent]:
        if self._closing or self._closed:
            message = "Codex conversation has not been started"
            raise RuntimeError(message)
        voice = self._voice
        generation = self._voice_generation_number
        if (
            voice is None
            or not self._voice_active
            or (expected_generation is not None and expected_generation != generation)
        ):
            message = (
                "Codex conversation has not been started"
                if expected_generation is None
                else "Codex Voice generation is not active"
            )
            raise RuntimeError(message)
        return voice.notifications()

    async def close_voice(
        self,
        expected_generation: int,
        *,
        on_claimed: Callable[[], None],
    ) -> bool:
        async with self._voice_operation_lock:
            async with self._state_lock:
                voice = self._voice
                if (
                    self._closing
                    or self._closed
                    or not self._voice_active
                    or expected_generation != self._voice_generation_number
                    or voice is None
                ):
                    return False
                self._voice_active = False
                self._voice = None
                coordinator = self._coordinator
                claim_error: BaseException | None = None
                try:
                    on_claimed()
                except BaseException as error:  # noqa: BLE001 - settle claimed Voice first
                    claim_error = error
                if coordinator is not None:
                    coordinator.voice_lost()
            close_error, cancellation = await await_cleanup(voice.close())
            if cancellation is not None:
                if claim_error is not None:
                    _log_boundary_failure("voice_loss_claim", claim_error)
                raise cancellation
            if close_error is not None:
                if claim_error is not None:
                    _log_boundary_failure("voice_loss_claim", claim_error)
                raise close_error
            if claim_error is not None:
                raise claim_error
            return True

    async def replace_voice(self, sdp: str) -> str:
        async with self._voice_operation_lock:
            async with self._state_lock:
                if self._closing or self._closed:
                    message = "Codex conversation is closed"
                    raise CodexRpcError(message)
                if (
                    self._voice_active
                    or self._voice is not None
                    or self._starting_voice is not None
                ):
                    message = "Codex Voice is already active"
                    raise CodexRpcError(message)
                capabilities = self._voice_capabilities
                prompt = self._voice_prompt
                if capabilities is None or prompt is None:
                    message = "Codex Voice cannot be replaced"
                    raise CodexRpcError(message)
                self._voice_generation_number += 1
                voice = CodexRealtimeSession(
                    self._connection,
                    settings=self._settings,
                    capabilities=capabilities,
                    working_directory=self._working_directory,
                    prompt=prompt,
                )
                self._starting_voice = voice
            try:
                answer = await voice.start(sdp)
                await self._publish_voice(voice)
            except BaseException:
                async with self._state_lock:
                    if self._starting_voice is voice:
                        self._starting_voice = None
                await await_cleanup(voice.close())
                raise
            return answer

    async def close(self) -> None:
        self.claim_close()
        async with self._state_lock:
            close_task = self._close_task
            if close_task is None:
                close_task = asyncio.create_task(
                    self._run_close_once(),
                    name="moco-codex-conversation-close",
                )
                self._close_task = close_task
        _close_error, caller_cancellation = await await_cleanup(close_task)
        if caller_cancellation is not None:
            with suppress(BaseException):
                close_task.exception()
            raise caller_cancellation
        close_task.result()

    async def _run_close_once(self) -> None:
        async with self._voice_operation_lock:
            await self._close_once()

    async def _close_once(self) -> None:  # noqa: C901, PLR0912, PLR0915
        if self._closed:
            return

        cleanup_cancellation: asyncio.CancelledError | None = None
        settlement_error: BaseException | None = None
        coordinator = self._coordinator
        if self._reviewer_bound and coordinator is not None:
            settlement_error, cleanup_cancellation = await await_cleanup(coordinator.cancel_turn())

        startup_task = self._startup_task
        if startup_task is not None and not startup_task.done():
            self._startup_cancelled_for_close = True
            startup_task.cancel()

        discovery_task = self._discovery_task
        discovery_cancellation = await self._drain_task(
            discovery_task,
            "codex_discovery_drain",
        )
        if cleanup_cancellation is None:
            cleanup_cancellation = discovery_cancellation
        if self._discovery_task is discovery_task:
            self._discovery_task = None

        startup_cancellation = await self._drain_task(
            startup_task,
            "codex_start_drain",
            ignore_task_cancellation=True,
        )
        if cleanup_cancellation is None:
            cleanup_cancellation = startup_cancellation

        agent_error = settlement_error
        agent = self._agent
        self._agent = None
        if agent is not None:
            agent_error, agent_cancellation = await await_cleanup(agent.close())
            if cleanup_cancellation is None:
                cleanup_cancellation = agent_cancellation

        broker = self._broker
        self._broker = None
        slot = self._reviewer_slot
        if broker is not None:
            if slot is not None and self._reviewer_bound:
                slot.release(broker)
                self._reviewer_bound = False
            try:
                broker.close()
            except BaseException as error:  # noqa: BLE001 - cleanup continues
                if agent_error is None:
                    agent_error = error
                else:
                    _log_boundary_failure("codex_broker_close", error)

        voice_error, voice_cancellation = await self._close_voice()
        if cleanup_cancellation is None:
            cleanup_cancellation = voice_cancellation
        connection_error, connection_cancellation = await self._close_connection()
        if cleanup_cancellation is None:
            cleanup_cancellation = connection_cancellation

        self._closed = True
        self._voice_active = False
        self._voice = None
        self._starting_voice = None
        if agent_error is not None:
            if voice_error is not None:
                _log_boundary_failure("codex_voice_close", voice_error)
            if connection_error is not None:
                _log_boundary_failure("codex_connection_close", connection_error)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            raise agent_error
        _raise_close_errors(
            voice_error,
            voice_cancellation,
            connection_error,
            connection_cancellation,
            cleanup_cancellation=cleanup_cancellation,
        )

    async def _drain_task(
        self,
        task: Awaitable[object] | None,
        boundary: str,
        *,
        ignore_task_cancellation: bool = False,
    ) -> asyncio.CancelledError | None:
        if task is None:
            return None
        drained_task = asyncio.ensure_future(task)
        caller_cancellation: asyncio.CancelledError | None = None
        while not drained_task.done():
            try:
                await asyncio.wait({drained_task})
            except asyncio.CancelledError as error:
                if caller_cancellation is None:
                    caller_cancellation = error

        try:
            drained_task.result()
        except asyncio.CancelledError as error:
            if not ignore_task_cancellation:
                _log_boundary_failure(boundary, error)
        except BaseException as error:  # noqa: BLE001 - cleanup result is type-only logged
            _log_boundary_failure(boundary, error)
        return caller_cancellation

    async def _close_voice(
        self,
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        voice = self._voice or self._starting_voice
        if voice is None or self._voice_close_attempted:
            return None, None
        self._voice_close_attempted = True
        return await await_cleanup(voice.close())

    async def _close_connection(
        self,
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        if self._connection_close_attempted:
            return None, None
        self._connection_close_attempted = True
        return await await_cleanup(self._connection.close())


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
        self._close_task: asyncio.Task[None] | None = None
        self._resource_cleanup_task: asyncio.Task[None] | None = None
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
        self._voice_options: tuple[dict[str, object], ...] = ()
        self._voice_ready = False
        self._voice_readiness = "loading"
        self._emoji_supported = False
        self._voice_generation: str | None = None
        self._selected_voice_id: str | None = None
        self._voice_selection_error: str | None = None
        self._voice_selected_explicitly = False
        self._init_transcript_pipeline()
        self._idle_timer = IdleLeaseTimer(
            idle_timeout_seconds=settings.runtime.idle_timeout_seconds,
        )
        self._snapshot = InteractionSnapshot(
            connection=ConnectionState.STARTING,
            voice=VoiceState.IDLE,
            task=TaskState.NONE,
            speech=SpeechState.SILENT,
        )
        self._connecting = False
        self._voice_reconnect_required = False
        self._connection_lost = False
        self._idle_expired = False
        self._effect_tasks: list[asyncio.Task[None]] = []
        self._agent_activity_queue: asyncio.Queue[AgentActivityEvent] = asyncio.Queue(
            maxsize=_MAX_PENDING_AGENT_ACTIVITIES,
        )
        self._agent_activity_worker_task: asyncio.Task[None] | None = None
        self._agent_activity_backpressure_reported = False
        self._speech_effect_tail: asyncio.Task[None] | None = None
        self._speech_effect_generation = 0
        self._turn_cancel_pending = False
        self._turn_result_claimed = False
        self._terminal_speech_delivery: tuple[SpeechQueue, asyncio.Task[None]] | None = None
        self._connection_loss_task: asyncio.Task[None] | None = None

    def _init_transcript_pipeline(self) -> None:
        self._user_utterance_active = False
        self._user_utterance_id = 0
        self._claimed_user_utterance_id = 0
        self._last_user_done_event: TranscriptEvent | None = None
        self._user_transcript_parts = 0
        self._transcript_queue: asyncio.Queue[_TranscriptWork] = asyncio.Queue(
            maxsize=_MAX_PENDING_TRANSCRIPTS,
        )
        self._transcript_worker_task: asyncio.Task[None] | None = None
        self._transcript_settlement_tasks: set[asyncio.Task[None]] = set()
        self._transcript_presentation_tail: asyncio.Future[None] | None = None
        self._utterance_speech_claim: _UtteranceSpeechClaim | None = None

    async def run(self) -> None:
        safe_event(
            logger,
            "operator_connected",
            component="web",
            state="ready",
        )
        await self._send_state()
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

    def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
        was_task_active = (
            self._snapshot.connection is not ConnectionState.DISCONNECTED
            and self._snapshot.task
            in {
                TaskState.RUNNING,
                TaskState.WAITING_REVIEW,
            }
        )
        is_task_active = (
            snapshot.connection is not ConnectionState.DISCONNECTED
            and snapshot.task
            in {
                TaskState.RUNNING,
                TaskState.WAITING_REVIEW,
            }
        )
        self._snapshot = snapshot
        self._idle_timer.touch()
        if is_task_active != was_task_active:
            if is_task_active:
                self._turn_result_claimed = False
            self.on_agent_activity(
                AgentActivityEvent(
                    "turn",
                    "started" if is_task_active else "completed",
                )
            )
        if snapshot.connection is ConnectionState.DISCONNECTED:
            self._connection_lost = True
            self._voice_reconnect_required = False
            self._claim_conversation_close()
            if self._connection_loss_task is None:
                if not self._turn_result_claimed:
                    self._queue_owner_speech_invalidation()
                task = self._spawn_effect(
                    self._close_connection_lost_lease(self._session),
                    name="moco-connection-lost-close",
                )
                if task is not None:
                    self._connection_loss_task = task
                    task.add_done_callback(self._forget_connection_loss_task)
            return
        self._spawn_effect(self._send_state(), name="moco-interaction-state")

    def on_turn_terminal_claimed(self) -> None:
        if not self._turn_cancel_pending:
            return
        self._turn_cancel_pending = False
        self._queue_owner_speech_invalidation()

    def on_turn_finished(self, result: TurnResult) -> None:
        self._turn_result_claimed = self._snapshot.task not in {
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.WAITING_REVIEW,
        }
        text = result.final_answer
        if text is None:
            text = _turn_failure_speech(result.error_code)
        authoritative_text = strip_control_emojis(text)
        if not authoritative_text.strip():
            return
        self._spawn_effect(
            self._send_turn_result_transcript(
                authoritative_text,
                self._transcript_presentation_tail,
            ),
            name="moco-turn-result-transcript",
        )
        self._queue_terminal_speech_text(authoritative_text)

    async def _send_turn_result_transcript(
        self,
        text: str,
        presentation_barrier: asyncio.Future[None] | None,
    ) -> None:
        if presentation_barrier is not None:
            await asyncio.shield(presentation_barrier)
        await self._send_json(
            {
                "type": "transcript",
                "role": "assistant",
                "text": text,
                "done": True,
            }
        )

    def on_submission_error(self, code: str) -> None:
        self._spawn_effect(self._send_error(code), name="moco-submission-error")

    def on_agent_activity(self, event: AgentActivityEvent) -> None:
        if self._close_task is not None:
            return
        try:
            self._agent_activity_queue.put_nowait(event)
        except asyncio.QueueFull:
            enqueued_after_eviction = (
                self._evict_droppable_agent_activity() if event.kind == "turn" else False
            )
            if enqueued_after_eviction:
                self._agent_activity_queue.put_nowait(event)
            if not self._agent_activity_backpressure_reported:
                self._agent_activity_backpressure_reported = True
                safe_event(
                    logger,
                    "agent_activity_dropped",
                    component="web",
                    event_code="activity_backpressure",
                    state="backpressure",
                )
            if not enqueued_after_eviction:
                return
        self._ensure_agent_activity_worker()

    def _evict_droppable_agent_activity(self) -> bool:
        retained: list[AgentActivityEvent] = []
        evicted = False
        while True:
            try:
                queued = self._agent_activity_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not evicted and queued.kind != "turn":
                evicted = True
                continue
            retained.append(queued)
        if not evicted and retained:
            retained.pop(0)
            evicted = True
        for queued in retained:
            self._agent_activity_queue.put_nowait(queued)
        return evicted

    def _ensure_agent_activity_worker(self) -> None:
        task = self._agent_activity_worker_task
        if task is not None and not task.done():
            return
        task = self._spawn_effect(
            self._run_agent_activity_worker(),
            name="moco-agent-activity-worker",
        )
        if task is None:
            self._discard_pending_agent_activities()
            return
        self._agent_activity_worker_task = task
        task.add_done_callback(self._forget_agent_activity_worker)

    async def _run_agent_activity_worker(self) -> None:
        while True:
            try:
                event = self._agent_activity_queue.get_nowait()
            except asyncio.QueueEmpty:
                self._agent_activity_backpressure_reported = False
                return
            await self._send_agent_activity(event)

    def _forget_agent_activity_worker(self, task: asyncio.Task[None]) -> None:
        if task is self._agent_activity_worker_task:
            self._agent_activity_worker_task = None

    def _discard_pending_agent_activities(self) -> None:
        self._agent_activity_backpressure_reported = False
        while True:
            try:
                self._agent_activity_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _spawn_effect(
        self,
        effect: Coroutine[object, object, None],
        *,
        name: str,
    ) -> asyncio.Task[None] | None:
        if self._close_task is not None:
            effect.close()
            return None
        task: asyncio.Task[None] = asyncio.create_task(effect, name=name)
        self._effect_tasks.append(task)
        task.add_done_callback(self._finish_effect_task)
        return task

    def _queue_speech_text(self, text: str, *, name: str) -> asyncio.Task[None] | None:
        generation = self._speech_effect_generation

        async def speak() -> None:
            if generation != self._speech_effect_generation:
                return
            speech = self._speech
            if speech is None:
                return
            if self._first_playback_started_ns is None:
                self._first_playback_started_ns = time.monotonic_ns()
            await speech.on_transcript(role="assistant", delta=text, done=True)

        return self._queue_speech_effect(speak, name=name)

    def _queue_terminal_speech_text(self, text: str) -> None:
        speech = self._speech
        effect = self._queue_speech_text(text, name="moco-turn-result-speech")
        if speech is None or effect is None:
            return

        async def await_delivery() -> None:
            await effect
            if speech is self._speech:
                await speech.join()

        delivery = self._spawn_effect(
            await_delivery(),
            name="moco-turn-result-delivery",
        )
        if delivery is not None:
            self._terminal_speech_delivery = (speech, delivery)
            delivery.add_done_callback(self._forget_terminal_speech_delivery)

    def _forget_terminal_speech_delivery(self, task: asyncio.Task[None]) -> None:
        current = self._terminal_speech_delivery
        if current is not None and task is current[1]:
            self._terminal_speech_delivery = None

    def _queue_speech_effect(
        self,
        operation: Callable[[], Awaitable[object]],
        *,
        name: str,
    ) -> asyncio.Task[None] | None:
        previous = self._speech_effect_tail

        async def run_in_order() -> None:
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                except Exception as error:  # noqa: BLE001 - preserve ordered continuation
                    _log_boundary_failure("speech_effect_predecessor", error)
            await operation()

        task = self._spawn_effect(run_in_order(), name=name)
        if task is not None:
            self._speech_effect_tail = task
            task.add_done_callback(self._forget_speech_effect_tail)
        return task

    def _forget_speech_effect_tail(self, task: asyncio.Task[None]) -> None:
        if task is self._speech_effect_tail:
            self._speech_effect_tail = None

    def _queue_owner_speech_invalidation(self) -> None:
        self._terminal_speech_delivery = None
        self._speech_effect_generation += 1
        self._queue_speech_effect(
            self._invalidate_terminal_speech,
            name="moco-terminal-speech-invalidation",
        )

    async def _invalidate_terminal_speech(self) -> None:
        speech = self._speech
        if speech is None or (not speech.is_busy and not self._playback_states):
            return
        await self._invalidate_and_reset_speech(reason="owner_request")

    async def _await_speech_effects(self) -> None:
        task = self._speech_effect_tail
        if task is not None:
            await task

    def _finish_effect_task(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self._discard_effect_task(task)
            return
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 - task boundary contains failures
            _log_boundary_failure("web_effect", error)
            self._retain_first_effect_failure()
        else:
            self._discard_effect_task(task)

    def _retain_first_effect_failure(self) -> None:
        retained_failure = False
        for task in tuple(self._effect_tasks):
            if not task.done():
                continue
            if task.cancelled() or task.exception() is None or retained_failure:
                self._discard_effect_task(task)
            else:
                retained_failure = True

    def _discard_effect_task(self, task: asyncio.Task[None]) -> None:
        with suppress(ValueError):
            self._effect_tasks.remove(task)

    def _forget_connection_loss_task(self, task: asyncio.Task[None]) -> None:
        if task is self._connection_loss_task:
            self._connection_loss_task = None

    async def _close_connection_lost_lease(
        self,
        session: RealtimeSession | None,
    ) -> None:
        try:
            async with asyncio.timeout(_CONNECTION_LOSS_SETTLEMENT_SECONDS):
                await self._settle_connection_loss_effects()
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            _log_boundary_failure("connection_lost_settlement_timeout", error)
        finally:
            if session is not None and session is self._session:
                await self._close_conversation_resources()

    async def _settle_connection_loss_effects(self) -> None:
        try:
            await self._send_state()
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - result delivery still settles
            _log_boundary_failure("connection_lost_state", error)
        terminal_delivery = self._terminal_speech_delivery
        if terminal_delivery is None:
            return
        speech, delivery = terminal_delivery
        if speech is not self._speech:
            return
        try:
            await asyncio.shield(delivery)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - lease cleanup must continue
            _log_boundary_failure("connection_lost_terminal_delivery", error)

    async def close(self) -> None:
        caller_task = asyncio.current_task()
        close_task = self._close_task
        if close_task is None:
            self._claim_conversation_close()
            close_task = asyncio.create_task(
                self._run_close_once(caller_task),
                name="moco-browser-close",
            )
            self._close_task = close_task
        elif self._is_cleanup_child(caller_task):
            return
        _close_error, caller_cancellation = await await_cleanup(close_task)
        if caller_cancellation is not None:
            with suppress(BaseException):
                close_task.exception()
            raise caller_cancellation
        close_task.result()

    def _is_cleanup_child(self, task: asyncio.Task[object] | None) -> bool:
        if task is None:
            return False
        return (
            any(candidate is task for candidate in self._effect_tasks)
            or task is self._capability_task
            or task is self._idle_task
            or task is self._connection_loss_task
        )

    async def _run_close_once(  # noqa: C901
        self,
        caller_task: asyncio.Task[object] | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        settlement_error: BaseException | None = None

        def remember_error(boundary: str, error: BaseException | None) -> None:
            nonlocal settlement_error
            if error is None:
                return
            if settlement_error is None:
                settlement_error = error
            else:
                _log_boundary_failure(boundary, error)

        effect_tasks = tuple(task for task in self._effect_tasks if task is not caller_task)
        for task in effect_tasks:
            task.cancel()
        for task in effect_tasks:
            error, _cancellation = await await_cleanup(task)
            remember_error("browser_effect_close", error)
            self._discard_effect_task(task)
        self._agent_activity_worker_task = None
        self._discard_pending_agent_activities()
        self._discard_pending_transcripts()
        capability_task = self._capability_task
        if capability_task is not None and capability_task is not caller_task:
            capability_task.cancel()
            error, _cancellation = await await_cleanup(capability_task)
            remember_error("browser_capability_close", error)
        if self._capability_task is capability_task:
            self._capability_task = None
        idle_task = self._idle_task
        if idle_task is not None and idle_task is not caller_task:
            idle_task.cancel()
            error, _cancellation = await await_cleanup(idle_task)
            remember_error("browser_idle_close", error)
        if self._idle_task is idle_task:
            self._idle_task = None
        resource_error, resource_cancellation = await await_cleanup(
            self._close_conversation_resources()
        )
        remember_error("browser_resource_close", resource_error)
        if resource_cancellation is not None:
            if settlement_error is not None:
                _log_boundary_failure("browser_close", settlement_error)
            raise resource_cancellation
        if settlement_error is not None:
            raise settlement_error
        safe_event(
            logger,
            "operator_disconnected",
            component="web",
            state="disabled",
        )

    async def _handle(self, payload: str) -> bool:
        try:
            message = parse_client_message(json.loads(payload, object_pairs_hook=_unique_object))
        except (json.JSONDecodeError, ValidationError, ValueError):
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
        elif isinstance(message, VoiceLostMessage):
            session = self._session
            if session is not None:
                await self._handle_voice_loss(session, session.voice_generation)
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
        self._update_speech_state(playing=active)
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
            await self._send_state()
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
        await self._send_state()

    async def _start(self, message: StartMessage) -> None:  # noqa: C901, PLR0911, PLR0915
        connection_loss_task = self._connection_loss_task
        if connection_loss_task is not None and connection_loss_task is not asyncio.current_task():
            await asyncio.shield(connection_loss_task)
        current_session = self._session
        if current_session is not None:
            current_voice_active = current_session.voice_active
            if current_voice_active:
                await self._send_error("already_started")
                return
            self._connecting = True
            self._idle_timer.touch()
            await self._send_state()
            try:
                answer = await current_session.replace_voice(message.sdp)
            except (OSError, RuntimeError):
                self._connecting = False
                self._voice_reconnect_required = not self._connection_lost
                self._idle_timer.touch()
                await self._send_error("conversation_start_failed")
                await self._send_state()
                return
            replacement_voice_active = current_session.voice_active
            if (
                self._connection_lost
                or current_session is not self._session
                or not replacement_voice_active
            ):
                self._connecting = False
                self._voice_reconnect_required = not self._connection_lost
                self._idle_timer.touch()
                if not self._connection_lost:
                    await self._send_error("conversation_start_failed")
                    await self._send_state()
                return
            self._connecting = False
            self._voice_reconnect_required = False
            self._idle_timer.touch()
            await self._send_json({"type": "sdp_answer", "sdp": answer})
            await self._start_notification_consumer()
            await self._send_state()
            return
        self._idle_timer = IdleLeaseTimer(
            idle_timeout_seconds=self._settings.runtime.idle_timeout_seconds,
        )
        self._resource_cleanup_task = None
        self._connecting = True
        self._idle_timer.touch()
        self._connection_lost = False
        self._idle_expired = False
        await self._send_state()
        synthesizer: WebSynthesizer | None = None
        session: RealtimeSession | None = None
        try:
            synthesizer = self._synthesizer_factory()
            preparation_error = await self._prepare_start_synthesizer(synthesizer)
            if preparation_error is not None:
                await self._fail_start(None, synthesizer, preparation_error)
                return
            session = self._session_factory()
            session.bind_effects(self)
            answer = await session.start(message.sdp)
            if self._connection_lost or not session.voice_active:
                await self._fail_start(session, synthesizer, "conversation_start_failed")
                return
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
        candidate = session.interaction_snapshot
        if type(candidate) is InteractionSnapshot:
            self._snapshot = candidate
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
        self._connecting = False
        self._voice_reconnect_required = False
        self._idle_timer.touch()
        await self._send_json({"type": "sdp_answer", "sdp": answer})
        await self._send_state()
        await self._start_notification_consumer()
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
        self._connecting = False
        if not self._connection_lost:
            self._idle_expired = True
        await self._send_state()

    async def _apply_control(self, control: ClientControl) -> None:
        safe_event(
            logger,
            "control_received",
            component="web",
            control=control.value,
        )
        if control is ClientControl.TURN_CANCEL:
            await self._apply_turn_cancel()
            return
        if control is ClientControl.LISTEN_START:
            if self._session is None:
                await self._send_error("conversation_not_started")
                return
            self._session.listen_started()
            candidate = self._session.interaction_snapshot
            if type(candidate) is InteractionSnapshot:
                self._snapshot = candidate
            self._idle_timer.touch()
            await self._send_state()
            return
        if control is ClientControl.LISTEN_STOP:
            if self._session is None:
                self._idle_expired = True
                await self._send_state()
                return
            self._session.listen_stopped()
            candidate = self._session.interaction_snapshot
            if type(candidate) is InteractionSnapshot:
                self._snapshot = candidate
            self._idle_timer.touch()
            await self._send_state()
            return
        assert_never(control)

    async def _apply_turn_cancel(self) -> None:
        session = self._session
        if session is None:
            await self._send_error("turn_not_active")
            return
        self._turn_cancel_pending = True
        try:
            cancelled = await session.cancel_turn()
        except BaseException:
            self._turn_cancel_pending = False
            raise
        if not cancelled:
            self._turn_cancel_pending = False
            await self._send_error("turn_not_active")
            return
        self._turn_cancel_pending = False
        await self._await_speech_effects()

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

    async def _start_notification_consumer(self) -> None:
        task = self._notifications_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        session = self._session
        if session is None:
            return
        generation = session.voice_generation
        self._notifications_task = asyncio.create_task(
            self._consume_notifications(generation),
            name="moco-realtime-events",
        )

    async def _consume_notifications(  # noqa: C901
        self,
        expected_generation: int | None = None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            notifications = session.notifications
            stream = (
                notifications(expected_generation)
                if expected_generation is not None
                else notifications()
            )
            async for event in stream:
                if not self._voice_generation_is_current(session, expected_generation):
                    continue
                if isinstance(event, RealtimeErrorEvent):
                    if expected_generation is None:
                        await self._terminate_conversation("codex_realtime_error")
                    else:
                        await self._handle_voice_loss(session, expected_generation)
                    return
                if isinstance(event, ActivityEvent):
                    await self._send_activity(event, session, expected_generation)
                    continue
                if isinstance(event, ReasoningSummaryEvent):
                    continue
                if isinstance(event, TranscriptEvent):
                    completion = self._enqueue_transcript(
                        event,
                        session=session,
                        expected_generation=expected_generation,
                    )
                    completion.add_done_callback(self._report_transcript_completion)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as error:
            _log_boundary_failure("realtime_events", error)
            with suppress(RuntimeError):
                await self._handle_voice_loss(session, expected_generation)

    def _voice_generation_is_current(
        self,
        session: RealtimeSession,
        expected_generation: int | None,
    ) -> bool:
        return session is self._session and (
            expected_generation is None
            or (session.voice_active and expected_generation == session.voice_generation)
        )

    async def _handle_voice_loss(  # noqa: C901, PLR0911
        self,
        session: RealtimeSession,
        expected_generation: int | None,
    ) -> None:
        if session is not self._session or self._connection_lost:
            return
        if expected_generation is None:
            await self._terminate_conversation("invalid_realtime_event")
            return
        speech_invalidation: asyncio.Task[None] | None = None

        def claim_speech_invalidation() -> None:
            nonlocal speech_invalidation
            if speech_invalidation is not None:
                return
            self._transcripts.clear()
            self._user_utterance_active = False
            self._user_transcript_parts = 0
            self._utterance_speech_claim = None
            self._discard_pending_transcripts()
            self._last_user_done_event = None
            self._speech_effect_generation += 1
            speech_invalidation = self._queue_speech_effect(
                lambda: self._invalidate_and_reset_speech(reason="user_transcript"),
                name="moco-voice-loss-speech-invalidation",
            )

        try:
            closed = await session.close_voice(
                expected_generation,
                on_claimed=claim_speech_invalidation,
            )
        except (OSError, RuntimeError) as error:
            _log_boundary_failure("voice_close", error)
            if session is not self._session or self._connection_lost:
                return
            if session.voice_active:
                await self._terminate_conversation("codex_realtime_error")
                return
        else:
            if not closed:
                return
        if session is not self._session or self._connection_lost:
            return
        if speech_invalidation is not None:
            try:
                await speech_invalidation
            except (OSError, RuntimeError) as error:
                _log_boundary_failure("voice_loss_speech_invalidation", error)
                self._discard_effect_task(speech_invalidation)
        if session is not self._session or self._connection_lost:
            return
        self._voice_reconnect_required = True
        await self._send_state()

    async def _terminate_conversation(self, error_code: str) -> None:
        await self._send_error(error_code)
        await self._close_conversation_resources()
        self._idle_expired = True
        await self._send_state()

    async def _handle_speech_error(self, code: str) -> None:
        if code == "runtime_generation_mismatch":
            safe_event(
                logger,
                "irodori_generation_mismatch",
                event_code=code,
            )
        await self._send_error(code)

    async def _send_activity(
        self,
        event: ActivityEvent,
        session: RealtimeSession,
        expected_generation: int | None,
    ) -> None:
        kind, label = _ACTIVITY_LABELS[event.kind]
        message: dict[str, object] = {
            "type": "activity",
            "kind": kind,
            "phase": event.phase,
            "label": label,
            "occurredAtMs": (
                event.occurred_at_ms if event.occurred_at_ms is not None else _now_ms()
            ),
        }
        if event.kind == "turn":
            message["source"] = "voice"
        await self._send_voice_json(
            message,
            session=session,
            expected_generation=expected_generation,
        )

    async def _send_agent_activity(self, event: AgentActivityEvent) -> None:
        kind, label = _ACTIVITY_LABELS[event.kind]
        message: dict[str, object] = {
            "type": "activity",
            "kind": kind,
            "phase": event.phase,
            "label": label,
            "occurredAtMs": _now_ms(),
        }
        if event.kind == "turn":
            message["source"] = "agent"
        await self._send_json(message)

    def _enqueue_transcript(
        self,
        event: TranscriptEvent,
        *,
        session: RealtimeSession | None = None,
        expected_generation: int | None = None,
    ) -> asyncio.Future[None]:
        completion = asyncio.get_running_loop().create_future()
        if session is not None and not self._voice_generation_is_current(
            session,
            expected_generation,
        ):
            completion.set_result(None)
            return completion
        if event.role == "assistant":
            completion.set_result(None)
            return completion
        if self._transcript_queue.full():
            message = "transcript queue limit exceeded"
            raise RuntimeError(message)
        if (
            not self._user_utterance_active
            and event.kind == "done"
            and event is self._last_user_done_event
        ):
            completion.set_result(None)
            return completion
        starts_utterance = not self._user_utterance_active
        text, done = self._user_transcript_update(
            event,
            starts_utterance=starts_utterance,
        )
        speech_claim = self._prepare_transcript_event(
            starts_utterance=starts_utterance,
            session=session,
            expected_generation=expected_generation,
        )
        user_final = (
            self._claim_user_final(
                text,
                event=event,
                session=session,
                expected_generation=expected_generation,
            )
            if event.role == "user" and done
            else None
        )
        if done and user_final is None:
            completion.set_result(None)
            return completion
        presented = asyncio.get_running_loop().create_future()
        self._transcript_presentation_tail = presented
        self._transcript_queue.put_nowait(
            _TranscriptWork(
                text=text,
                done=done,
                user_final=user_final,
                speech_claim=speech_claim,
                session=session,
                expected_generation=expected_generation,
                presented=presented,
                completion=completion,
            )
        )
        self._ensure_transcript_worker()
        return completion

    def _ensure_transcript_worker(self) -> None:
        task = self._transcript_worker_task
        if task is not None and not task.done():
            return
        task = self._spawn_effect(
            self._run_transcript_worker(),
            name="moco-realtime-transcript-worker",
        )
        if task is None:
            self._discard_pending_transcripts()
            return
        self._transcript_worker_task = task
        task.add_done_callback(self._forget_transcript_worker)

    async def _run_transcript_worker(self) -> None:
        while True:
            try:
                work = self._transcript_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._present_transcript(work)
            except asyncio.CancelledError:
                self._cancel_transcript_work(work)
                raise
            except Exception as error:  # noqa: BLE001 - isolate one presentation
                self._finish_transcript_work(work, error=error)

    async def _present_transcript(self, work: _TranscriptWork) -> None:
        presentation_error = await self._prepare_transcript_presentation(work)
        if presentation_error is None:
            presentation_error = await self._publish_transcript(work)
        self._mark_transcript_presented(work)

        if work.done and work.speech_claim is self._utterance_speech_claim:
            self._utterance_speech_claim = None

        if work.user_final is None or work.user_final.handoff is None:
            self._finish_transcript_work(work, error=presentation_error)
            return
        await self._await_transcript_settlement_slot()
        task = self._spawn_effect(
            self._settle_user_final(
                work,
                presentation_error=presentation_error,
            ),
            name="moco-user-final-settlement",
        )
        if task is None:
            self._cancel_transcript_work(work)
            return
        self._transcript_settlement_tasks.add(task)
        task.add_done_callback(self._forget_transcript_settlement)

    async def _publish_transcript(self, work: _TranscriptWork) -> Exception | None:
        if work.session is not None and not self._voice_generation_is_current(
            work.session,
            work.expected_generation,
        ):
            return None
        try:
            sent = await self._send_voice_json(
                {
                    "type": "transcript",
                    "role": "user",
                    "text": strip_control_emojis(work.text),
                    "done": work.done,
                },
                session=work.session,
                expected_generation=work.expected_generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - handoff claim must still settle
            return error
        if sent:
            self._idle_timer.touch()
        return None

    async def _prepare_transcript_presentation(
        self,
        work: _TranscriptWork,
    ) -> Exception | None:
        try:
            await self._await_utterance_speech_stop(work.speech_claim)
            if work.done:
                speech = self._speech
                if speech is not None:
                    await speech.on_transcript(role="user", delta="", done=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - handoff claim must still settle
            return error
        return None

    def _prepare_transcript_event(
        self,
        *,
        starts_utterance: bool,
        session: RealtimeSession | None,
        expected_generation: int | None,
    ) -> _UtteranceSpeechClaim | None:
        if starts_utterance:
            self._user_utterance_active = True
            self._user_utterance_id += 1
            self._transcripts.pop("assistant", None)
            self._speech_effect_generation += 1
            speech_stopped = asyncio.Event()
            if self._speech is None:
                speech_invalidation, notice = self._claim_speech_invalidation(
                    reason="user_transcript",
                    session=session,
                    expected_generation=expected_generation,
                    speech_stopped=speech_stopped,
                )
                invalidation = self._queue_speech_effect(
                    lambda: self._settle_speech_invalidation(
                        speech_invalidation=speech_invalidation,
                        notice=notice,
                        speech_stopped=speech_stopped,
                    ),
                    name="moco-user-speech-invalidation",
                )
            else:
                invalidation = self._queue_speech_effect(
                    lambda: self._invalidate_speech(
                        reason="user_transcript",
                        speech_stopped=speech_stopped,
                        session=session,
                        expected_generation=expected_generation,
                    ),
                    name="moco-user-speech-invalidation",
                )
            self._utterance_speech_claim = _UtteranceSpeechClaim(
                invalidation=invalidation,
                stopped=speech_stopped,
            )
        return self._utterance_speech_claim

    def _claim_user_final(
        self,
        text: str,
        *,
        event: TranscriptEvent,
        session: RealtimeSession | None,
        expected_generation: int | None,
    ) -> _UserFinalClaim | None:
        target_session = self._session if session is None else session
        self._user_utterance_active = False
        if target_session is None:
            return _UserFinalClaim(handoff=None, target_session=None)
        if session is not None and not self._voice_generation_is_current(
            session=session,
            expected_generation=expected_generation,
        ):
            return None
        utterance_id = self._user_utterance_id
        if utterance_id <= self._claimed_user_utterance_id:
            return None
        self._claimed_user_utterance_id = utterance_id
        self._last_user_done_event = event
        handoff = target_session.consume_user_final(text, utterance_id=utterance_id)
        return _UserFinalClaim(
            handoff=handoff,
            target_session=target_session,
        )

    async def _settle_user_final(
        self,
        work: _TranscriptWork,
        *,
        presentation_error: Exception | None,
    ) -> None:
        claim = work.user_final
        if claim is None or claim.handoff is None or claim.target_session is None:
            self._finish_transcript_work(work, error=presentation_error)
            return
        try:
            await claim.handoff
        except asyncio.CancelledError:
            work.completion.cancel()
            raise
        except Exception as error:  # noqa: BLE001 - strict handoff boundary
            self._finish_transcript_work(work, error=error)
            return
        if presentation_error is not None:
            self._finish_transcript_work(work, error=presentation_error)
            return
        if work.session is not None and not self._voice_generation_is_current(
            work.session,
            work.expected_generation,
        ):
            self._finish_transcript_work(work)
            return
        candidate = claim.target_session.interaction_snapshot
        if type(candidate) is InteractionSnapshot:
            self._snapshot = candidate
        self._finish_transcript_work(work)

    async def _await_utterance_speech_stop(
        self,
        claim: _UtteranceSpeechClaim | None,
    ) -> None:
        if claim is None or claim.stopped.is_set():
            return
        if claim.invalidation is None:
            message = "utterance speech invalidation is unavailable"
            raise RuntimeError(message)
        stopped_wait = asyncio.create_task(
            claim.stopped.wait(),
            name="moco-utterance-speech-stop",
        )
        try:
            await asyncio.wait(
                (stopped_wait, claim.invalidation),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not claim.stopped.is_set():
                claim.invalidation.result()
        finally:
            if not stopped_wait.done():
                stopped_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await stopped_wait

    async def _await_transcript_settlement_slot(self) -> None:
        while len(self._transcript_settlement_tasks) >= _MAX_PENDING_TRANSCRIPTS:
            await asyncio.wait(
                tuple(self._transcript_settlement_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )

    def _forget_transcript_worker(self, task: asyncio.Task[None]) -> None:
        if task is self._transcript_worker_task:
            self._transcript_worker_task = None

    def _forget_transcript_settlement(self, task: asyncio.Task[None]) -> None:
        self._transcript_settlement_tasks.discard(task)

    @staticmethod
    def _report_transcript_completion(completion: asyncio.Future[None]) -> None:
        if completion.cancelled():
            return
        error = completion.exception()
        if error is not None:
            _log_boundary_failure("web_effect", error)

    @staticmethod
    def _finish_transcript_work(
        work: _TranscriptWork,
        *,
        error: Exception | None = None,
    ) -> None:
        _BrowserConnection._mark_transcript_presented(work)
        if work.completion.done():
            return
        if error is None:
            work.completion.set_result(None)
        else:
            work.completion.set_exception(error)

    @staticmethod
    def _cancel_transcript_work(work: _TranscriptWork) -> None:
        _BrowserConnection._mark_transcript_presented(work)
        if not work.completion.done():
            work.completion.cancel()
        claim = work.user_final
        if claim is None or claim.handoff is None:
            return
        if isinstance(claim.handoff, asyncio.Future):
            claim.handoff.cancel()
            return
        close = getattr(claim.handoff, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _mark_transcript_presented(work: _TranscriptWork) -> None:
        if not work.presented.done():
            work.presented.set_result(None)

    def _discard_pending_transcripts(self) -> None:
        while True:
            try:
                work = self._transcript_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._cancel_transcript_work(work)

    async def _stop_transcript_pipeline(
        self,
        caller_task: asyncio.Task[object] | None,
    ) -> None:
        tasks = {
            task
            for task in (
                self._transcript_worker_task,
                *self._transcript_settlement_tasks,
            )
            if task is not None and task is not caller_task and not task.done()
        }
        for task in tasks:
            task.cancel()
        for task in tasks:
            error, _cancellation = await await_cleanup(task)
            if error is not None:
                _log_boundary_failure("browser_transcript_close", error)
            self._discard_effect_task(task)
        self._transcript_worker_task = None
        self._transcript_settlement_tasks.clear()
        self._discard_pending_transcripts()

    async def _invalidate_and_reset_speech(
        self,
        *,
        reason: Literal["owner_request", "user_transcript"],
        session: RealtimeSession | None = None,
        expected_generation: int | None = None,
    ) -> None:
        speech = self._speech
        try:
            await self._invalidate_speech(
                reason=reason,
                session=session,
                expected_generation=expected_generation,
            )
        finally:
            if speech is not None:
                await speech.on_transcript(role="user", delta="", done=True)

    async def _invalidate_speech(
        self,
        *,
        reason: Literal["owner_request", "user_transcript"] = "user_transcript",
        session: RealtimeSession | None = None,
        expected_generation: int | None = None,
        speech_stopped: asyncio.Event | None = None,
    ) -> bool:
        if session is not None and not self._voice_generation_is_current(
            session,
            expected_generation,
        ):
            return False
        speech_invalidation, notice = self._claim_speech_invalidation(
            reason=reason,
            session=session,
            expected_generation=expected_generation,
            speech_stopped=speech_stopped,
        )
        return await self._settle_speech_invalidation(
            speech_invalidation=speech_invalidation,
            notice=notice,
            speech_stopped=speech_stopped,
        )

    def _claim_speech_invalidation(
        self,
        *,
        reason: Literal["owner_request", "user_transcript"],
        session: RealtimeSession | None,
        expected_generation: int | None,
        speech_stopped: asyncio.Event | None,
    ) -> tuple[asyncio.Task[None] | None, asyncio.Task[bool]]:
        self._generation += 1
        self._clear_first_playback_timing()
        self._clear_playback_states()
        speech_invalidation = (
            asyncio.create_task(
                self._speech.invalidate(reason=reason),
                name="moco-speech-invalidation",
            )
            if self._speech is not None
            else None
        )
        if speech_invalidation is None and speech_stopped is not None:
            speech_stopped.set()
        notice = asyncio.create_task(
            self._send_voice_json(
                {"type": "audio_invalidate", "generation": self._generation},
                session=session,
                expected_generation=expected_generation,
            ),
            name="moco-audio-invalidation-notice",
        )
        return speech_invalidation, notice

    async def _settle_speech_invalidation(
        self,
        *,
        speech_invalidation: asyncio.Task[None] | None,
        notice: asyncio.Task[bool],
        speech_stopped: asyncio.Event | None,
    ) -> bool:
        try:
            if speech_invalidation is not None:
                await speech_invalidation
            if speech_stopped is not None:
                speech_stopped.set()
            sent = await notice
        finally:
            if speech_invalidation is not None and not speech_invalidation.done():
                await speech_invalidation
            if not notice.done():
                notice.cancel()
                with suppress(asyncio.CancelledError):
                    await notice
        return sent

    def _user_transcript_update(
        self,
        event: TranscriptEvent,
        *,
        starts_utterance: bool,
    ) -> tuple[str, bool]:
        if starts_utterance:
            self._user_transcript_parts = 0
        self._user_transcript_parts += 1
        if self._user_transcript_parts > _MAX_USER_TRANSCRIPT_PARTS:
            message = "transcript part limit exceeded"
            raise RuntimeError(message)
        accumulated = self._transcripts.get("user", "")
        if event.kind == "delta":
            if len(accumulated.encode()) + len(event.text.encode()) > _MAX_USER_TRANSCRIPT_BYTES:
                message = "transcript text limit exceeded"
                raise RuntimeError(message)
            text = accumulated + event.text
            self._transcripts["user"] = text
            return text, False
        if len(event.text.encode()) > _MAX_USER_TRANSCRIPT_BYTES:
            message = "transcript text limit exceeded"
            raise RuntimeError(message)
        self._transcripts.pop("user", None)
        return event.text, True

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
        self._update_speech_state(playing=False)

    def _update_speech_state(self, *, playing: bool | None = None) -> None:
        session = self._session
        if session is None:
            return
        playback_active = (
            any(value == "started" for value in self._playback_states.values())
            if playing is None
            else playing
        )
        speech = self._speech
        state = (
            SpeechState.PLAYING
            if playback_active
            else SpeechState.SYNTHESIZING
            if speech is not None and speech.is_busy
            else SpeechState.SILENT
        )
        session.speech_changed(state)
        candidate = session.interaction_snapshot
        if type(candidate) is InteractionSnapshot:
            self._snapshot = candidate

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
            self._update_speech_state()

    async def _idle_loop(self) -> None:
        interval = min(0.05, self._settings.runtime.idle_timeout_seconds / 2)
        while True:
            await asyncio.sleep(interval)
            speech_busy = self._speech is not None and self._speech.is_busy
            previous = self._snapshot.speech
            self._update_speech_state()
            if self._snapshot.speech is not previous:
                await self._send_json(
                    {
                        "type": "activity",
                        "kind": "voice",
                        "phase": "started" if speech_busy else "completed",
                        "label": "音声生成",
                        "occurredAtMs": _now_ms(),
                    },
                )
            session = self._session
            snapshot = None if session is None else session.interaction_snapshot
            if type(snapshot) is InteractionSnapshot:
                self._snapshot = snapshot
            if self._idle_timer.claim_expired(
                is_idle=not self._connecting and self._snapshot.idle,
            ):
                await self._expire_conversation()

    async def _expire_conversation(self) -> None:
        self._idle_expired = True
        await self._close_conversation_resources()
        await self._send_state()
        safe_event(
            logger,
            "conversation_expired",
            component="runtime",
            state="idle_expired",
        )

    async def _close_conversation_resources(self) -> None:
        self._claim_conversation_close()
        async with self._resource_lock:
            cleanup_task = self._resource_cleanup_task
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(
                    self._run_resource_cleanup_once(asyncio.current_task()),
                    name="moco-browser-resource-close",
                )
                self._resource_cleanup_task = cleanup_task
        _cleanup_error, caller_cancellation = await await_cleanup(cleanup_task)
        if caller_cancellation is not None:
            with suppress(BaseException):
                cleanup_task.exception()
            raise caller_cancellation
        cleanup_task.result()

    def _claim_conversation_close(self) -> None:
        session = self._session
        if session is not None:
            session.claim_close()

    async def _run_resource_cleanup_once(
        self,
        caller_task: asyncio.Task[object] | None,
    ) -> None:
        await self._stop_transcript_pipeline(caller_task)
        self._clear_first_playback_timing()
        self._clear_playback_states()
        notification_task = self._notifications_task
        speech, session, synthesizer = (
            self._speech,
            self._session,
            self._synthesizer,
        )
        self._notifications_task = None
        self._speech = None
        self._terminal_speech_delivery = None
        self._session = None
        self._synthesizer = None
        self._transcripts.clear()
        self._user_utterance_active = False
        self._last_user_done_event = None
        self._user_transcript_parts = 0
        self._utterance_speech_claim = None

        if notification_task is not None and notification_task is not caller_task:
            notification_task.cancel()
            notification_error, _notification_cancellation = await await_cleanup(notification_task)
            if notification_error is not None:
                _log_boundary_failure("browser_notification_close", notification_error)

        await _close_resources(
            (
                ("browser_speech_close", speech),
                ("browser_session_close", session),
                ("browser_irodori_close", synthesizer),
            )
        )

    async def _send_state(self) -> None:
        candidate = None if self._session is None else self._session.interaction_snapshot
        snapshot = candidate if type(candidate) is InteractionSnapshot else self._snapshot
        self._snapshot = snapshot
        projected = _project_ui_state(
            snapshot,
            idle_expired=self._idle_expired,
            connection_lost=self._connection_lost,
            connecting=self._connecting,
            voice_reconnect_required=self._voice_reconnect_required,
        )
        can_cancel = snapshot.task in {
            TaskState.RUNNING,
            TaskState.WAITING_REVIEW,
        }
        await self._send_json(
            {
                "type": "state",
                "state": projected.value,
                "canCancel": can_cancel,
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

    async def _send_voice_json(
        self,
        message: dict[str, object],
        *,
        session: RealtimeSession | None,
        expected_generation: int | None,
    ) -> bool:
        async with self._send_lock:
            if session is not None and not self._voice_generation_is_current(
                session,
                expected_generation,
            ):
                return False
            await self._websocket.send_json(message)
            return session is None or self._voice_generation_is_current(
                session,
                expected_generation,
            )

    async def _send_json(self, message: dict[str, object]) -> None:
        async with self._send_lock:
            await self._websocket.send_json(message)


def create_app(  # noqa: C901
    settings: MocoSettings | None = None,
    *,
    session_factory: SessionFactory | None = None,
    synthesizer_factory: SynthesizerFactory | None = None,
    capability_token: str | None = None,
    control_secret: str | None = None,
    review_broker: ReviewerBroker | None = None,
    global_hotkeys_active: bool | None = None,
) -> FastAPI:
    resolved = settings or MocoSettings()
    reviewer_slot = _ReviewerBrokerSlot()
    build_session = session_factory or _codex_session_factory(resolved, reviewer_slot)
    build_synthesizer = synthesizer_factory or cast(
        "SynthesizerFactory",
        lambda: IrodoriSynthesizer.from_settings(resolved),
    )
    control_hub = ControlHub()
    media_token = capability_token or secrets.token_urlsafe(32)
    review_secret = secrets.token_urlsafe(32) if control_secret is None else control_secret
    if (
        type(media_token) is str
        and type(review_secret) is str
        and secrets.compare_digest(media_token, review_secret)
    ):
        message = "media and review credentials must be distinct"
        raise CodexReviewError(message)
    review_gate = ReviewGate(
        review_secret,
    )
    app = FastAPI(title="moco", docs_url=None, redoc_url=None)
    app.state.capability_token = media_token
    app.state.control_hub = control_hub
    app.state.review_gate = review_gate
    app.state.review_broker = review_broker or reviewer_slot
    app.state.global_hotkeys_active = (
        resolved.hotkeys.enabled if global_hotkeys_active is None else global_hotkeys_active
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/review", include_in_schema=False)
    async def review_page() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "review.html",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.post("/review/bootstrap", include_in_schema=False)
    async def review_bootstrap(request: Request) -> Response:
        peer_host = request.client.host if request.client is not None else None
        try:
            nonce = review_gate.issue_bootstrap_nonce(
                request.headers.get("x-moco-control-secret"),
                peer_host=peer_host,
                host=request.headers.get("host"),
                origin=request.headers.get("origin"),
            )
        except CodexReviewError:
            raise HTTPException(status_code=404) from None
        return JSONResponse(
            {"nonce": nonce},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.websocket("/review/ws")
    async def reviewer_socket(websocket: WebSocket) -> None:
        await serve_reviewer_socket(
            websocket,
            review_gate=review_gate,
            broker=app.state.review_broker,
        )

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


def _codex_session_factory(
    settings: MocoSettings,
    reviewer_slot: _ReviewerBrokerSlot | None = None,
) -> SessionFactory:
    slot = reviewer_slot or _ReviewerBrokerSlot()

    def build() -> RealtimeSession:
        command = resolve_codex_command(settings.codex.command)
        working_directory = settings.codex.working_directory or Path.cwd()
        connection = CodexConnectionSupervisor(command)
        schema_probe = CodexSchemaProbe(command)
        return _CodexConversationOwner(
            settings=settings,
            connection=connection,
            working_directory=working_directory,
            contract_probe=schema_probe,
            reviewer_slot=slot,
        )

    return build


def _conversation_readiness(
    contract: object,
    capabilities: CapabilitySnapshot,
) -> ConnectionState:
    if (
        capabilities.agent_admission.status is not CapabilityStatus.AVAILABLE
        or capabilities.realtime.status is not CapabilityStatus.AVAILABLE
    ):
        message = "required Codex capability is unavailable"
        raise CodexRpcError(message)
    profiles = getattr(contract, "approval_profiles", {})
    modern_file_requires_patch = any(
        getattr(profile, "category", None) is ServerRequestCategory.FILE_CHANGE_APPROVAL
        and getattr(profile, "changes_member", object()) is None
        for profile in profiles.values()
    )
    missing_modern_patch_evidence = (
        modern_file_requires_patch and getattr(contract, "file_change_patch_profile", None) is None
    )
    optional_states = (
        capabilities.account,
        capabilities.policy_state,
        capabilities.managed_requirements,
        capabilities.interrupt,
        capabilities.steer,
        capabilities.server_requests,
    )
    if (
        capabilities.effective_policy is None
        or capabilities.has_unclassified_server_requests
        or missing_modern_patch_evidence
        or any(state.status is not CapabilityStatus.AVAILABLE for state in optional_states)
    ):
        return ConnectionState.DEGRADED
    return ConnectionState.READY


def _project_ui_state(
    snapshot: InteractionSnapshot,
    *,
    idle_expired: bool,
    connection_lost: bool,
    connecting: bool,
    voice_reconnect_required: bool,
) -> LifecycleState:
    priorities = (
        (idle_expired, LifecycleState.IDLE_EXPIRED),
        (connection_lost, LifecycleState.CONNECTION_LOST),
        (connecting, LifecycleState.CONNECTING),
        (voice_reconnect_required, LifecycleState.VOICE_RECONNECT_REQUIRED),
        (snapshot.voice is VoiceState.LISTENING, LifecycleState.LISTENING),
        (snapshot.voice is VoiceState.TRANSCRIBING, LifecycleState.TRANSCRIBING),
        (
            snapshot.task is TaskState.WAITING_REVIEW,
            LifecycleState.WAITING_FOR_LOCAL_REVIEW,
        ),
        (snapshot.speech is not SpeechState.SILENT, LifecycleState.SPEAKING),
    )
    return next((state for active, state in priorities if active), LifecycleState.READY)


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
        and canonical_browser_loopback_host(
            origin_parts.hostname,
            allow_localhost=True,
        )
        is not None
        and canonical_browser_loopback_host(
            host_parts.hostname,
            allow_localhost=True,
        )
        is not None
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
        canonical_browser_loopback_host(host, allow_localhost=True) is not None
        and candidate is not None
        and secrets.compare_digest(candidate, expected_token)
        and fetch_site in {None, "same-origin"}
    )


def _raise_close_errors(
    voice_error: BaseException | None,
    voice_cancellation: asyncio.CancelledError | None,
    connection_error: BaseException | None,
    connection_cancellation: asyncio.CancelledError | None,
    *,
    cleanup_cancellation: asyncio.CancelledError | None = None,
) -> None:
    errors = tuple(error for error in (voice_error, connection_error) if error is not None)
    cancellation = cleanup_cancellation or voice_cancellation or connection_cancellation
    if cancellation is not None:
        for error in errors:
            _log_boundary_failure("codex_close", error)
        raise cancellation
    if errors:
        for error in errors[1:]:
            _log_boundary_failure("codex_close", error)
        raise errors[0]


def _log_boundary_failure(boundary: str, error: BaseException) -> None:
    logger.warning(
        "Boundary failure (boundary=%s, error_type=%s)",
        boundary,
        type(error).__name__,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _turn_failure_speech(code: str | None) -> str:
    if code is None:
        return _GENERIC_TURN_FAILURE_SPEECH
    return _TURN_FAILURE_SPEECH.get(code, _GENERIC_TURN_FAILURE_SPEECH)


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, member in pairs:
        if key in value:
            message = "duplicate JSON member"
            raise ValueError(message)
        value[key] = member
    return value


def _elapsed_ms(started_ns: int) -> int:
    return (time.monotonic_ns() - started_ns) // 1_000_000


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
    await _close_resources(
        (
            ("realtime_start_cleanup", session),
            ("irodori_start_cleanup", synthesizer),
        )
    )


async def _close_resources(
    resources: tuple[tuple[str, _AsyncClosable | None], ...],
) -> None:
    errors: list[tuple[str, BaseException]] = []
    cleanup_cancellation: asyncio.CancelledError | None = None
    for boundary, resource in resources:
        if resource is None:
            continue
        error, cancellation = await await_cleanup(resource.close())
        if error is not None:
            errors.append((boundary, error))
        if cleanup_cancellation is None:
            cleanup_cancellation = cancellation
    if cleanup_cancellation is not None:
        for boundary, error in errors:
            _log_boundary_failure(boundary, error)
        raise cleanup_cancellation
    if errors:
        for boundary, error in errors[1:]:
            _log_boundary_failure(boundary, error)
        raise errors[0][1]
