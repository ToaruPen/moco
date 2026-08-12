from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Self, cast

from moco.codex.capabilities import CapabilitySnapshot, CapabilityState, CapabilityStatus
from moco.config import default_prompt_path
from moco.errors import (
    CodexCapabilityError,
    CodexPromptError,
    CodexRpcError,
    CodexRpcTimeoutError,
)
from moco.runtime._cleanup import await_cleanup
from moco.runtime.telemetry import safe_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from moco.codex.rpc import JsonValue, RpcNotification
    from moco.config import MocoSettings

DEFAULT_REALTIME_PROMPT = (
    "Respond in short, natural Japanese suitable for speech synthesis. "
    "Use clear punctuation. Use Irodori-supported emoji only when expression requires it. "
    "Do not respond with structured JSON or Markdown."
)
_MAX_REALTIME_PROMPT_BYTES = 65_536
type TranscriptKind = Literal["delta", "done"]
type TranscriptRole = Literal["assistant", "user"]
type ActivityKind = Literal[
    "turn",
    "reasoning",
    "command_execution",
    "file_change",
    "external_tool",
    "subagent",
    "web_search",
    "image_view",
    "image_generation",
    "context_compaction",
    "codex_work",
]
type ActivityPhase = Literal["started", "completed"]

_ITEM_ACTIVITY: dict[str, ActivityKind] = {
    "reasoning": "reasoning",
    "commandExecution": "command_execution",
    "fileChange": "file_change",
    "mcpToolCall": "external_tool",
    "dynamicToolCall": "external_tool",
    "collabAgentToolCall": "subagent",
    "subAgentActivity": "subagent",
    "webSearch": "web_search",
    "imageView": "image_view",
    "imageGeneration": "image_generation",
    "contextCompaction": "context_compaction",
}

_MAX_PENDING_REALTIME_EVENTS = 64
_EVENTS_END = object()
logger = logging.getLogger(__name__)


class _RealtimeBacklogExceededError(CodexRpcError):
    """The bounded Realtime event queue cannot accept another event."""


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    kind: TranscriptKind
    thread_id: str
    role: TranscriptRole
    text: str


@dataclass(frozen=True, slots=True)
class RealtimeErrorEvent:
    thread_id: str
    message: str


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    kind: ActivityKind
    phase: ActivityPhase
    thread_id: str
    turn_id: str
    occurred_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ReasoningSummaryEvent:
    thread_id: str
    turn_id: str
    item_id: str
    delta: str


type RealtimeEvent = TranscriptEvent | RealtimeErrorEvent | ActivityEvent | ReasoningSummaryEvent


class CodexConnection(Protocol):
    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue: ...

    def notifications(self) -> AsyncIterator[RpcNotification]: ...


class CodexRealtimeSession:
    def __init__(
        self,
        connection: CodexConnection,
        *,
        settings: MocoSettings,
        capabilities: CapabilitySnapshot,
        working_directory: Path | None = None,
        prompt: str | None = None,
        sdp_timeout: float = 10.0,
    ) -> None:
        if sdp_timeout <= 0:
            msg = "sdp_timeout must be positive"
            raise ValueError(msg)
        self._connection = connection
        self._capabilities = capabilities
        self._settings = settings
        self._prompt = prompt
        resolved_working_directory = (
            working_directory or settings.codex.working_directory or Path.cwd()
        )
        if not resolved_working_directory.is_absolute():
            msg = "working directory must be absolute"
            raise ValueError(msg)
        self._working_directory = resolved_working_directory
        self._sdp_timeout = sdp_timeout
        self._thread_id: str | None = None
        self._active_turn_id: str | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._sdp_future: asyncio.Future[str] | None = None
        self._events: asyncio.Queue[RealtimeEvent | CodexRpcError | object] = asyncio.Queue(
            maxsize=_MAX_PENDING_REALTIME_EVENTS + 2,
        )
        self._close_lock = asyncio.Lock()
        self._started = False
        self._realtime_started = False
        self._closing = False
        self._closed = False
        self._events_ended = False

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def active_turn_id(self) -> str | None:
        return self._active_turn_id

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()

    async def start(self, offer_sdp: str) -> str:
        if not offer_sdp:
            msg = "offer SDP must not be empty"
            raise ValueError(msg)
        if self._started:
            msg = "Codex realtime session has already been started"
            raise CodexRpcError(msg)
        if self._closed:
            msg = "Codex realtime session is closed"
            raise CodexRpcError(msg)
        prompt = self._prompt if self._prompt is not None else load_realtime_prompt(self._settings)
        self._started = True

        try:
            _require_voice_readiness(self._capabilities)
            self._sdp_future = asyncio.get_running_loop().create_future()
            notifications = self._connection.notifications()
            self._notification_task = asyncio.create_task(
                self._pump_notifications(notifications),
                name="codex-realtime-notifications",
            )
            thread_result = await self._connection.request(
                "thread/start",
                {
                    "ephemeral": True,
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "cwd": str(self._working_directory),
                },
            )
            self._thread_id = _thread_id_from_result(thread_result)
            await self._connection.request(
                "thread/realtime/start",
                {
                    "threadId": self._thread_id,
                    "outputModality": "audio",
                    "includeStartupContext": False,
                    "prompt": prompt,
                    "transport": {"type": "webrtc", "sdp": offer_sdp},
                    "version": "v3",
                },
            )
            self._realtime_started = True
            safe_event(
                logger,
                "conversation_started",
                component="codex",
                boundary="codex_stdio",
                state="ready",
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(self._sdp_future),
                    self._sdp_timeout,
                )
            except TimeoutError as error:
                method = "thread/realtime/sdp"
                raise CodexRpcTimeoutError(
                    method,
                    self._sdp_timeout,
                ) from error
        except BaseException:
            cleanup_error, cleanup_cancellation = await await_cleanup(self.close())
            if cleanup_error is not None:
                _log_cleanup_failure("voice_start", cleanup_error)
            if cleanup_cancellation is not None:
                _log_cleanup_failure("voice_start", cleanup_cancellation)
            raise

    async def notifications(self) -> AsyncIterator[RealtimeEvent]:
        while True:
            item = await self._events.get()
            if item is _EVENTS_END:
                return
            if isinstance(item, CodexRpcError):
                raise item
            yield cast("RealtimeEvent", item)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            stop_error: BaseException | None = None
            cleanup_cancellation: asyncio.CancelledError | None = None
            if self._realtime_started and self._thread_id is not None:
                stop_error, stop_cancellation = await await_cleanup(
                    self._connection.request(
                        "thread/realtime/stop",
                        {"threadId": self._thread_id},
                    ),
                )
                if stop_cancellation is not None and cleanup_cancellation is None:
                    cleanup_cancellation = stop_cancellation
                self._realtime_started = False

            notification_error, notification_cancellation = await await_cleanup(
                self._finish_notification_task(),
            )
            if notification_cancellation is not None and cleanup_cancellation is None:
                cleanup_cancellation = notification_cancellation

            self._closed = True
            self._end_events()
            safe_event(
                logger,
                "conversation_closed",
                component="codex",
                state="ready",
            )
            errors = tuple(
                error
                for error in (
                    stop_error,
                    notification_error,
                )
                if error is not None
            )
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            if errors:
                for secondary in errors[1:]:
                    _log_cleanup_failure("voice_close", secondary)
                raise errors[0]

    async def _pump_notifications(
        self,
        notifications: AsyncIterator[RpcNotification],
    ) -> None:
        try:
            async for notification in notifications:
                self._handle_notification(notification)
        except asyncio.CancelledError:
            raise
        except CodexRpcError as error:
            self._fail_session(error)
        finally:
            if not self._closing:
                self._end_events()

    def _handle_notification(self, notification: RpcNotification) -> None:
        if notification.method in {"turn/started", "turn/completed"}:
            self._handle_turn_notification(notification)
            return
        if notification.method in {"item/started", "item/completed"}:
            try:
                self._handle_item_notification(notification)
            except _RealtimeBacklogExceededError:
                raise
            except CodexRpcError:
                safe_event(
                    logger,
                    "codex_auxiliary_notification_discarded",
                    component="codex",
                    event_code="invalid_activity",
                )
            return
        if notification.method == "item/reasoning/summaryTextDelta":
            try:
                self._handle_reasoning_summary(notification)
            except _RealtimeBacklogExceededError:
                raise
            except CodexRpcError:
                safe_event(
                    logger,
                    "codex_auxiliary_notification_discarded",
                    component="codex",
                    event_code="invalid_reasoning_summary",
                )
            return
        if notification.method == "item/reasoning/textDelta":
            return
        if not notification.method.startswith("thread/realtime/"):
            return
        self._handle_realtime_notification(notification)

    def _handle_realtime_notification(self, notification: RpcNotification) -> None:
        thread_id = _required_string(notification.params, "threadId", notification.method)
        if self._thread_id is None or thread_id != self._thread_id:
            return

        if notification.method == "thread/realtime/sdp":
            sdp = _required_string(notification.params, "sdp", notification.method)
            if self._sdp_future is not None and not self._sdp_future.done():
                self._sdp_future.set_result(sdp)
            return
        if notification.method == "thread/realtime/transcript/delta":
            self._enqueue_event(
                TranscriptEvent(
                    kind="delta",
                    thread_id=thread_id,
                    role=_transcript_role(notification),
                    text=_required_string(notification.params, "delta", notification.method),
                ),
            )
            return
        if notification.method == "thread/realtime/transcript/done":
            self._enqueue_event(
                TranscriptEvent(
                    kind="done",
                    thread_id=thread_id,
                    role=_transcript_role(notification),
                    text=_required_string(notification.params, "text", notification.method),
                ),
            )
            return
        if notification.method == "thread/realtime/error":
            message = _required_string(notification.params, "message", notification.method)
            self._enqueue_event(RealtimeErrorEvent(thread_id=thread_id, message=message))
            self._set_sdp_exception(CodexRpcError(message))

    def _handle_turn_notification(self, notification: RpcNotification) -> None:
        thread_id = _required_string(notification.params, "threadId", notification.method)
        if thread_id != self._thread_id:
            return
        raw_turn = notification.params.get("turn")
        if not isinstance(raw_turn, dict):
            msg = f"Codex notification {notification.method!r} had an invalid 'turn'"
            raise CodexRpcError(msg)
        turn_id = _required_string(raw_turn, "id", notification.method)
        if notification.method == "turn/started":
            self._active_turn_id = turn_id
            self._enqueue_event(
                ActivityEvent("turn", "started", thread_id, turn_id, None),
            )
        elif turn_id == self._active_turn_id:
            self._active_turn_id = None
            self._enqueue_event(
                ActivityEvent("turn", "completed", thread_id, turn_id, None),
            )

    def _handle_item_notification(self, notification: RpcNotification) -> None:
        thread_id = _required_string(notification.params, "threadId", notification.method)
        if thread_id != self._thread_id:
            return
        turn_id = _required_string(notification.params, "turnId", notification.method)
        if turn_id != self._active_turn_id:
            return
        raw_item = notification.params.get("item")
        if not isinstance(raw_item, dict):
            msg = f"Codex notification {notification.method!r} had an invalid 'item'"
            raise CodexRpcError(msg)
        item_type = _required_string(raw_item, "type", notification.method)
        kind = _ITEM_ACTIVITY.get(item_type, "codex_work")
        if notification.method == "item/started":
            phase: ActivityPhase = "started"
            timestamp_field = "startedAtMs"
        else:
            phase = "completed"
            timestamp_field = "completedAtMs"
        occurred_at_ms = _required_int(
            notification.params,
            timestamp_field,
            notification.method,
        )
        self._enqueue_event(
            ActivityEvent(kind, phase, thread_id, turn_id, occurred_at_ms),
        )

    def _handle_reasoning_summary(self, notification: RpcNotification) -> None:
        thread_id = _required_string(notification.params, "threadId", notification.method)
        if thread_id != self._thread_id:
            return
        turn_id = _required_string(notification.params, "turnId", notification.method)
        if turn_id != self._active_turn_id:
            return
        self._enqueue_event(
            ReasoningSummaryEvent(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=_required_string(
                    notification.params,
                    "itemId",
                    notification.method,
                ),
                delta=_required_string(
                    notification.params,
                    "delta",
                    notification.method,
                ),
            ),
        )

    def _enqueue_event(self, event: RealtimeEvent) -> None:
        if self._events.qsize() >= _MAX_PENDING_REALTIME_EVENTS:
            message = "Codex Realtime event backlog limit exceeded"
            raise _RealtimeBacklogExceededError(message)
        self._events.put_nowait(event)

    def _fail_session(self, error: CodexRpcError) -> None:
        self._set_sdp_exception(error)
        self._events.put_nowait(error)

    def _set_sdp_exception(self, error: CodexRpcError) -> None:
        future = self._sdp_future
        if future is None:
            return
        if not future.done():
            future.set_exception(error)
        try:
            future.exception()
        except asyncio.CancelledError:
            return

    async def _finish_notification_task(self) -> None:
        task = self._notification_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if task.cancelled():
                return
            raise

    def _end_events(self) -> None:
        if self._events_ended:
            return
        self._events_ended = True
        self._events.put_nowait(_EVENTS_END)


def _log_cleanup_failure(boundary: str, error: BaseException) -> None:
    logger.warning(
        "Codex Voice cleanup failed (boundary=%s, error_type=%s)",
        boundary,
        type(error).__name__,
    )


def load_realtime_prompt(settings: MocoSettings) -> str:
    configured = settings.codex.prompt_file
    path = configured or default_prompt_path()
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_REALTIME_PROMPT_BYTES + 1)
    except FileNotFoundError as error:
        if configured is None:
            return DEFAULT_REALTIME_PROMPT
        msg = "configured realtime prompt file was not found"
        raise CodexPromptError(msg) from error
    except (OSError, ValueError) as error:
        msg = "realtime prompt file could not be read"
        raise CodexPromptError(msg) from error
    if len(payload) > _MAX_REALTIME_PROMPT_BYTES:
        msg = "realtime prompt file exceeds 64 KiB"
        raise CodexPromptError(msg)
    try:
        prompt = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        msg = "realtime prompt file must be UTF-8"
        raise CodexPromptError(msg) from error
    if not prompt:
        msg = "realtime prompt file must not be blank"
        raise CodexPromptError(msg)
    return prompt


def _require_voice_readiness(snapshot: object) -> None:
    if not isinstance(snapshot, CapabilitySnapshot):
        message = "Codex capability snapshot is invalid"
        raise CodexCapabilityError(message)
    required_states = (snapshot.account, snapshot.realtime)
    if any(
        not isinstance(state, CapabilityState) or not isinstance(state.status, CapabilityStatus)
        for state in required_states
    ):
        message = "Codex capability snapshot is invalid"
        raise CodexCapabilityError(message)
    if (
        snapshot.account.status is CapabilityStatus.AVAILABLE
        and snapshot.realtime.status is CapabilityStatus.AVAILABLE
    ):
        return
    message = "Voice readiness is unavailable"
    raise CodexCapabilityError(message)


def _thread_id_from_result(result: JsonValue) -> str:
    if not isinstance(result, dict):
        msg = "thread/start returned an invalid result"
        raise CodexRpcError(msg)
    thread = result.get("thread")
    if not isinstance(thread, dict):
        msg = "thread/start result did not contain a thread"
        raise CodexRpcError(msg)
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        msg = "thread/start result did not contain a valid thread id"
        raise CodexRpcError(msg)
    return thread_id


def _required_string(
    params: Mapping[str, JsonValue],
    field: str,
    method: str,
) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value:
        msg = f"Codex notification {method!r} had an invalid {field!r}"
        raise CodexRpcError(msg)
    return value


def _required_int(
    params: Mapping[str, JsonValue],
    field: str,
    method: str,
) -> int:
    value = params.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"Codex notification {method!r} had an invalid {field!r}"
        raise CodexRpcError(msg)
    return value


def _transcript_role(notification: RpcNotification) -> TranscriptRole:
    role = _required_string(notification.params, "role", notification.method)
    if role not in {"assistant", "user"}:
        msg = f"Codex notification {notification.method!r} had unsupported role {role!r}"
        raise CodexRpcError(msg)
    return cast("TranscriptRole", role)
