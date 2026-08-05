from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Self, cast

from moco.config import default_prompt_path
from moco.errors import CodexPromptError, CodexRpcError, CodexRpcTimeoutError
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

_EVENTS_END = object()
logger = logging.getLogger(__name__)


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


class RpcClient(Protocol):
    async def start(self) -> None: ...

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue: ...

    def notifications(self) -> AsyncIterator[RpcNotification]: ...

    async def close(self) -> None: ...


class CodexRealtimeSession:
    def __init__(
        self,
        rpc: RpcClient,
        *,
        settings: MocoSettings,
        sdp_timeout: float = 10.0,
    ) -> None:
        if sdp_timeout <= 0:
            msg = "sdp_timeout must be positive"
            raise ValueError(msg)
        self._rpc = rpc
        self._settings = settings
        self._sdp_timeout = sdp_timeout
        self._thread_id: str | None = None
        self._active_turn_id: str | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._sdp_future: asyncio.Future[str] | None = None
        self._events: asyncio.Queue[RealtimeEvent | CodexRpcError | object] = asyncio.Queue()
        self._close_lock = asyncio.Lock()
        self._started = False
        self._realtime_started = False
        self._rpc_failed = False
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
        prompt = _load_realtime_prompt(self._settings)
        self._started = True

        try:
            await self._rpc.start()
            self._sdp_future = asyncio.get_running_loop().create_future()
            self._notification_task = asyncio.create_task(
                self._pump_notifications(),
                name="codex-realtime-notifications",
            )
            thread_result = await self._rpc.request(
                "thread/start",
                {
                    "ephemeral": True,
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "cwd": str(self._settings.codex.working_directory),
                },
            )
            self._thread_id = _thread_id_from_result(thread_result)
            await self._rpc.request(
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
            with suppress(Exception):
                await self.close()
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
            stop_error: CodexRpcError | None = None
            if self._realtime_started and not self._rpc_failed and self._thread_id is not None:
                try:
                    await self._rpc.request(
                        "thread/realtime/stop",
                        {"threadId": self._thread_id},
                    )
                except CodexRpcError as error:
                    stop_error = error
                self._realtime_started = False

            await self._rpc.close()
            await self._finish_notification_task()
            self._closed = True
            self._end_events()
            safe_event(
                logger,
                "conversation_closed",
                component="codex",
                state="ready",
            )
            if stop_error is not None:
                raise stop_error

    async def _pump_notifications(self) -> None:
        try:
            async for notification in self._rpc.notifications():
                self._handle_notification(notification)
        except asyncio.CancelledError:
            raise
        except CodexRpcError as error:
            await self._close_failed_rpc()
            self._fail_session(error)
        finally:
            if not self._closing:
                self._end_events()

    async def _close_failed_rpc(self) -> None:
        self._rpc_failed = True
        self._realtime_started = False
        await self._rpc.close()

    def _handle_notification(self, notification: RpcNotification) -> None:
        if notification.method in {"turn/started", "turn/completed"}:
            self._handle_turn_notification(notification)
            return
        if notification.method in {"item/started", "item/completed"}:
            try:
                self._handle_item_notification(notification)
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
            self._events.put_nowait(
                TranscriptEvent(
                    kind="delta",
                    thread_id=thread_id,
                    role=_transcript_role(notification),
                    text=_required_string(notification.params, "delta", notification.method),
                ),
            )
            return
        if notification.method == "thread/realtime/transcript/done":
            self._events.put_nowait(
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
            self._events.put_nowait(RealtimeErrorEvent(thread_id=thread_id, message=message))
            if self._sdp_future is not None and not self._sdp_future.done():
                self._sdp_future.set_exception(CodexRpcError(message))

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
            self._events.put_nowait(
                ActivityEvent("turn", "started", thread_id, turn_id, None),
            )
        elif turn_id == self._active_turn_id:
            self._active_turn_id = None
            self._events.put_nowait(
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
        self._events.put_nowait(
            ActivityEvent(kind, phase, thread_id, turn_id, occurred_at_ms),
        )

    def _handle_reasoning_summary(self, notification: RpcNotification) -> None:
        thread_id = _required_string(notification.params, "threadId", notification.method)
        if thread_id != self._thread_id:
            return
        turn_id = _required_string(notification.params, "turnId", notification.method)
        if turn_id != self._active_turn_id:
            return
        self._events.put_nowait(
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

    def _fail_session(self, error: CodexRpcError) -> None:
        if self._sdp_future is not None and not self._sdp_future.done():
            self._sdp_future.set_exception(error)
        self._events.put_nowait(error)

    async def _finish_notification_task(self) -> None:
        task = self._notification_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _end_events(self) -> None:
        if self._events_ended:
            return
        self._events_ended = True
        self._events.put_nowait(_EVENTS_END)


def _load_realtime_prompt(settings: MocoSettings) -> str:
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
