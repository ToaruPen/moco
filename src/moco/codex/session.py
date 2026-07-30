from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Self, cast

from moco.errors import CodexRpcError, CodexRpcTimeoutError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from moco.codex.rpc import JsonValue, RpcNotification
    from moco.config import MocoSettings

DEFAULT_REALTIME_PROMPT = (
    "Respond in short, natural Japanese suitable for speech synthesis. "
    "Use clear punctuation. Use Irodori-supported emoji only when expression requires it. "
    "Do not respond with structured JSON or Markdown."
)
CANCEL_INSTRUCTION = "現在の応答と作業を中止してください。"

type TranscriptKind = Literal["delta", "done"]
type TranscriptRole = Literal["assistant", "user"]

_EVENTS_END = object()


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


type RealtimeEvent = TranscriptEvent | RealtimeErrorEvent


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
                    "prompt": DEFAULT_REALTIME_PROMPT,
                    "transport": {"type": "webrtc", "sdp": offer_sdp},
                    "version": "v3",
                },
            )
            self._realtime_started = True
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

    async def cancel_current(self) -> None:
        if self._thread_id is None or not self._realtime_started:
            return
        if self._active_turn_id is not None:
            await self._rpc.request(
                "turn/interrupt",
                {
                    "threadId": self._thread_id,
                    "turnId": self._active_turn_id,
                },
            )
            self._active_turn_id = None
        await self._rpc.request(
            "thread/realtime/appendText",
            {
                "threadId": self._thread_id,
                "role": "user",
                "text": CANCEL_INSTRUCTION,
            },
        )

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
        if not notification.method.startswith("thread/realtime/"):
            return
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
        elif turn_id == self._active_turn_id:
            self._active_turn_id = None

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


def _transcript_role(notification: RpcNotification) -> TranscriptRole:
    role = _required_string(notification.params, "role", notification.method)
    if role not in {"assistant", "user"}:
        msg = f"Codex notification {notification.method!r} had unsupported role {role!r}"
        raise CodexRpcError(msg)
    return cast("TranscriptRole", role)
