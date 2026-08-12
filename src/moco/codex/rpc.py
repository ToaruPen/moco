from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable, iscoroutine
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Self, TypeGuard, cast
from weakref import WeakSet

from moco.errors import (
    CodexProcessExitedError,
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type RequestId = int | str

_NOTIFICATION_END = object()

# How many inbound server requests this peer serves at once. A buffered burst arrives before
# any handler can run, so the admission bound lives here, in front of task creation, rather
# than in whatever a handler owns behind it. It matches the reviews an approval broker holds
# open, so no request a broker would have taken is refused by the transport in front of it.
_MAX_ACTIVE_SERVER_REQUESTS = 64
# A notification stream is shared by long-lived owners.  A slow owner must not turn an
# untrusted burst into unbounded memory, and overflow is an explicit local terminal rather
# than a silent drop that could hide a turn completion.
_MAX_NOTIFICATION_SUBSCRIBER_QUEUE = 64


@dataclass(frozen=True, slots=True)
class RpcServerRequest:
    request_id: RequestId
    method: str
    params: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RpcSuccess:
    request_id: RequestId
    result: JsonValue


@dataclass(frozen=True, slots=True)
class RpcFailure:
    request_id: RequestId
    error: JsonValue


type RpcInbound = RpcServerRequest | RpcNotification | RpcSuccess | RpcFailure
type RpcServerRequestHandler = Callable[[RpcServerRequest], Awaitable[JsonValue]]


@dataclass(frozen=True, slots=True)
class _QueuedNotification:
    method: str
    params: object


def _freeze_notification_value(value: JsonValue) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_notification_value(nested) for key, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_notification_value(nested) for nested in value)
    return value


def _thaw_notification_value(value: object) -> JsonValue:
    if isinstance(value, MappingProxyType):
        return {str(key): _thaw_notification_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_notification_value(nested) for nested in value]
    return cast("JsonValue", value)


def _call_notification_observer(
    observer: Callable[[RpcNotification], None],
    notification: _QueuedNotification,
) -> None:
    params = _thaw_notification_value(notification.params)
    if not isinstance(params, dict):  # pragma: no cover - only private frozen data enters
        raise TypeError
    result = cast("Callable[[RpcNotification], object]", observer)(
        RpcNotification(notification.method, params)
    )
    if not isawaitable(result):
        return
    _silence_native_observer_awaitable(result)
    raise TypeError


def _silence_native_observer_awaitable(result: object) -> None:
    """Reject an async observer without claiming ownership of what it returned."""
    if isinstance(result, asyncio.Future):
        if result.done():
            _consume_observer_future(result)
        else:
            result.add_done_callback(_consume_observer_future)
    elif iscoroutine(result):
        with suppress(BaseException):
            result.close()


def _consume_observer_future(future: asyncio.Future[object]) -> None:
    with suppress(BaseException):
        future.exception()


def _classify_message(message: dict[str, JsonValue]) -> RpcInbound:
    has_method = "method" in message
    has_id = "id" in message
    has_result = "result" in message
    has_error = "error" in message
    method = message.get("method")
    raw_id = message.get("id")
    request_id = raw_id if _is_request_id(raw_id) else None

    if has_method:
        if not isinstance(method, str) or has_result or has_error:
            msg = "Codex app server sent an overlapping request message"
            raise CodexRpcProtocolError(msg, server_request_id=request_id)
        params = message.get("params", {})
        if not isinstance(params, dict):
            msg = "Codex app server notification had invalid params"
            raise CodexRpcProtocolError(msg, server_request_id=request_id)
        if has_id:
            if request_id is None:
                msg = "Codex app server sent a request with an invalid id"
                raise CodexRpcProtocolError(msg)
            return RpcServerRequest(request_id, method, params)
        return RpcNotification(method, params)

    if not has_id:
        msg = "Codex app server sent a notification without a method"
        raise CodexRpcProtocolError(msg)
    if request_id is None:
        msg = "Codex app server sent a response with an invalid id"
        raise CodexRpcProtocolError(msg)
    if has_result == has_error:
        msg = (
            "Codex app server sent an overlapping response message"
            if has_result
            else "Codex app server sent a response without result or error"
        )
        raise CodexRpcProtocolError(msg, client_response_id=request_id)
    if has_result:
        return RpcSuccess(request_id, message["result"])
    return RpcFailure(request_id, message["error"])


def _is_request_id(value: JsonValue) -> TypeGuard[RequestId]:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


@dataclass(slots=True)
class _IncomingCall:
    task: asyncio.Task[None]
    response_sent: bool = False


class _NotificationSubscription:
    def __init__(
        self,
        subscribers: WeakSet[_NotificationSubscription],
    ) -> None:
        self._subscribers = subscribers
        self._queue: asyncio.Queue[_QueuedNotification | CodexRpcError | object] = asyncio.Queue(
            maxsize=_MAX_NOTIFICATION_SUBSCRIBER_QUEUE,
        )
        self._closed = False
        subscribers.add(self)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> RpcNotification:
        if self._closed:
            raise StopAsyncIteration
        try:
            item = await self._queue.get()
        except BaseException:
            await self.aclose()
            raise
        if item is _NOTIFICATION_END:
            await self.aclose()
            raise StopAsyncIteration
        if isinstance(item, CodexRpcError):
            await self.aclose()
            raise item
        queued = cast("_QueuedNotification", item)
        params = _thaw_notification_value(queued.params)
        if not isinstance(params, dict):  # pragma: no cover - only private frozen data enters
            await self.aclose()
            message = "Codex RPC notification subscriber failed"
            raise CodexRpcError(message)
        return RpcNotification(queued.method, params)

    def enqueue(self, notification: _QueuedNotification) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            # Replace the backlog so overflow is observed immediately and never silently
            # displaces a completion behind arbitrary progress.  This failure is local to
            # this subscriber; the shared peer and every other subscriber remain usable.
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._queue.put_nowait(CodexRpcError("Codex RPC notification subscriber overflow"))

    def terminate(self, terminal: CodexRpcError | object) -> None:
        if self._closed:
            return
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(terminal)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._subscribers.discard(self)


class RpcPeer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_timeout: float = 10.0,
    ) -> None:
        if request_timeout <= 0:
            msg = "request_timeout must be positive"
            raise ValueError(msg)
        self._reader = reader
        self._writer = writer
        self._request_timeout = request_timeout
        self._pending: dict[RequestId, asyncio.Future[JsonValue]] = {}
        self._incoming: dict[RequestId, _IncomingCall] = {}
        self._handlers: dict[str, RpcServerRequestHandler] = {}
        self._notification_observer: Callable[[RpcNotification], None] | None = None
        self._terminal_callbacks: list[Callable[[], None]] = []
        self._subscribers: WeakSet[_NotificationSubscription] = WeakSet()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._next_id = 1
        self._reader_task: asyncio.Task[None] | None = None
        self._terminal_error: CodexRpcError | None = None
        self._subscriber_terminal: CodexRpcError | object | None = None
        self._closed = False

    def register_server_request_handler(
        self,
        method: str,
        handler: RpcServerRequestHandler,
    ) -> None:
        if self._reader_task is not None or self._terminal_error is not None:
            msg = "server request handlers must be registered before peer start"
            raise RuntimeError(msg)
        self._handlers[method] = handler

    def register_notification_observer(
        self,
        observer: Callable[[RpcNotification], None],
    ) -> None:
        """Register the one synchronous observer that runs inside inbound ordering."""
        if self._reader_task is not None or self._terminal_error is not None:
            msg = "notification observers must be registered before peer start"
            raise RuntimeError(msg)
        if self._notification_observer is not None:
            msg = "only one notification observer may be registered"
            raise RuntimeError(msg)
        self._notification_observer = observer

    def register_terminal_callback(self, callback: Callable[[], None]) -> None:
        """Ask to be told, once, when this peer stops carrying messages.

        Whoever holds state that only this connection can complete needs the ending itself,
        not a report of it after the fact. The callback is told nothing: why the peer ended
        is the peer's own terminal error, and repeating it here would carry a transport
        detail into somewhere that must not hold one. Registration closes at start, like the
        handlers, so no work begins on a peer that has not been fully wired.
        """
        if self._reader_task is not None or self._terminal_error is not None:
            msg = "terminal callbacks must be registered before peer start"
            raise RuntimeError(msg)
        self._terminal_callbacks.append(callback)

    async def start(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(
            self._reader_loop(),
            name="codex-rpc-peer-reader",
        )

    async def notify(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._ensure_running()
        message: dict[str, JsonValue] = {"method": method}
        if params is not None:
            message["params"] = _validate_outbound_params(params)
        await self._write(message)

    def notifications(self) -> AsyncIterator[RpcNotification]:
        subscription = _NotificationSubscription(self._subscribers)
        terminal = self._subscriber_terminal
        if terminal is not None:
            subscription.terminate(terminal)
        return subscription

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        self._ensure_running()
        deadline = self._request_timeout if request_timeout is None else request_timeout
        if deadline <= 0:
            msg = "timeout must be positive"
            raise ValueError(msg)
        validated_params = None if params is None else _validate_outbound_params(params)
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, JsonValue] = {"id": request_id, "method": method}
        if validated_params is not None:
            message["params"] = validated_params
        expires_at = asyncio.get_running_loop().time() + deadline
        try:
            await self._write(message, deadline_seconds=deadline)
            try:
                remaining = expires_at - asyncio.get_running_loop().time()
                return await asyncio.wait_for(asyncio.shield(future), remaining)
            except TimeoutError as error:
                raise CodexRpcTimeoutError(method, deadline) from error
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def abort(self, error: CodexRpcError) -> None:
        if self._set_terminal_error(error, subscriber_terminal=error):
            self._cancel_reader()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            error = CodexProcessExitedError("Codex RPC peer was closed")
            self._set_terminal_error(error, subscriber_terminal=_NOTIFICATION_END)
            self._cancel_reader()
            task = self._reader_task
            if task is not None and task is not asyncio.current_task():
                with suppress(asyncio.CancelledError):
                    await task
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0)

    def _ensure_running(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._reader_task is None:
            msg = "Codex RPC peer has not been started"
            raise CodexProcessExitedError(msg)

    async def _write(
        self,
        message: Mapping[str, JsonValue],
        *,
        deadline_seconds: float | None = None,
    ) -> None:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        deadline = self._request_timeout if deadline_seconds is None else deadline_seconds
        try:
            async with asyncio.timeout(deadline), self._write_lock:
                self._writer.write(encoded + b"\n")
                await self._writer.drain()
        except TimeoutError as error:
            terminal_error = CodexRpcTimeoutError("write", deadline)
            self._set_terminal_error(
                terminal_error,
                subscriber_terminal=terminal_error,
            )
            self._cancel_reader()
            raise terminal_error from error
        except (BrokenPipeError, ConnectionResetError, OSError) as error:
            write_error = CodexProcessExitedError("Codex RPC peer write failed")
            self._set_terminal_error(
                write_error,
                subscriber_terminal=write_error,
            )
            self._cancel_reader()
            raise write_error from error

    async def _reader_loop(self) -> None:
        try:
            while line := await self._read_line():
                message = self._decode_message(line)
                try:
                    inbound = _classify_message(message)
                except CodexRpcProtocolError as error:
                    await self._handle_protocol_error(error)
                else:
                    await self._handle_inbound(inbound)
                if self._terminal_error is not None:
                    return
            if not self._closed:
                eof_error = CodexProcessExitedError("Codex RPC peer reached EOF")
                self._set_terminal_error(eof_error, subscriber_terminal=eof_error)
        except asyncio.CancelledError:
            raise
        except CodexRpcError as error:
            self._set_terminal_error(error, subscriber_terminal=error)
        except Exception:  # noqa: BLE001 - peer must fail closed at boundary
            msg = "Codex app server sent an invalid JSON-RPC message"
            terminal_error = CodexRpcProtocolError(msg)
            self._set_terminal_error(
                terminal_error,
                subscriber_terminal=terminal_error,
            )

    async def _read_line(self) -> bytes:
        try:
            return await self._reader.readline()
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            msg = "Codex app server sent an oversized JSON-RPC message"
            raise CodexRpcProtocolError(msg) from error
        except Exception as error:
            msg = "Codex RPC peer failed while reading a message"
            raise CodexProcessExitedError(msg) from error

    def _decode_message(self, line: bytes) -> dict[str, JsonValue]:
        try:
            decoded = json.loads(
                line,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}"),
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            msg = "Codex app server sent invalid JSON"
            raise CodexRpcProtocolError(msg) from error
        value = _as_json_value(cast("object", decoded))
        if not isinstance(value, dict):
            msg = "Codex app server sent a non-object JSON-RPC message"
            raise CodexRpcProtocolError(msg)
        return value

    async def _handle_inbound(self, inbound: RpcInbound) -> None:
        if isinstance(inbound, RpcServerRequest):
            existing = self._incoming.get(inbound.request_id)
            if existing is not None:
                should_respond = not existing.response_sent
                existing.response_sent = True
                existing.task.cancel()
                msg = "Codex app server sent a duplicate server request id"
                error = CodexRpcProtocolError(
                    msg,
                    server_request_id=inbound.request_id,
                )
                self._set_terminal_error(error, subscriber_terminal=error)
                if should_respond:
                    await self._write(
                        {
                            "id": inbound.request_id,
                            "error": {
                                "code": -32600,
                                "message": "duplicate server request id",
                            },
                        }
                    )
                return
            if len(self._incoming) >= _MAX_ACTIVE_SERVER_REQUESTS:
                # Refused before a task exists, so a burst costs one response and no state.
                # The request is answered rather than dropped, and the peer stays usable for
                # the requests it did admit.
                await self._write(
                    {
                        "id": inbound.request_id,
                        "error": {
                            "code": -32603,
                            "message": "too many server requests",
                        },
                    }
                )
                return
            task = asyncio.create_task(self._serve_request(inbound))
            self._incoming[inbound.request_id] = _IncomingCall(task)
            task.add_done_callback(self._consume_task_exception)
            return
        if isinstance(inbound, RpcNotification):
            frozen = _QueuedNotification(
                inbound.method,
                _freeze_notification_value(inbound.params),
            )
            self._notify_observer(frozen)
            for subscription in tuple(self._subscribers):
                subscription.enqueue(frozen)
            return
        future = self._pending.get(inbound.request_id)
        if future is None or future.done():
            return
        if isinstance(inbound, RpcFailure):
            future.set_exception(self._response_error(inbound.error))
        else:
            future.set_result(inbound.result)

    def _notify_observer(self, notification: _QueuedNotification) -> None:
        observer = self._notification_observer
        if observer is None:
            return
        try:
            _call_notification_observer(observer, notification)
        except BaseException:  # noqa: BLE001 - trusted observer still fails closed
            msg = "Codex RPC notification observer failed"
            raise CodexRpcProtocolError(msg) from None

    async def _serve_request(self, request: RpcServerRequest) -> None:
        try:
            handler = self._handlers.get(request.method)
            if handler is None:
                await self._complete_incoming(
                    request.request_id,
                    error={
                        "code": -32601,
                        "message": "unsupported server request",
                    },
                )
                return
            try:
                result = await handler(request)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - handlers are an untrusted boundary
                await self._complete_incoming(
                    request.request_id,
                    error={
                        "code": -32603,
                        "message": "server request handler failed",
                    },
                )
            else:
                try:
                    validated_result = _validate_handler_result(result)
                except CodexRpcError:
                    await self._complete_incoming(
                        request.request_id,
                        error={
                            "code": -32603,
                            "message": "server request handler failed",
                        },
                    )
                else:
                    await self._complete_incoming(
                        request.request_id,
                        result=validated_result,
                    )
        finally:
            call = self._incoming.get(request.request_id)
            if call is not None and call.task is asyncio.current_task():
                self._incoming.pop(request.request_id, None)

    async def _complete_incoming(
        self,
        request_id: RequestId,
        *,
        result: JsonValue = None,
        error: dict[str, JsonValue] | None = None,
    ) -> bool:
        call = self._incoming.get(request_id)
        if call is None or call.response_sent:
            return False
        call.response_sent = True
        message: dict[str, JsonValue] = {"id": request_id}
        if error is None:
            message["result"] = result
        else:
            message["error"] = error
        await self._write(message)
        return True

    async def _handle_protocol_error(self, error: CodexRpcProtocolError) -> None:
        response_id = error.client_response_id
        if response_id is not None:
            self._set_terminal_error(error, subscriber_terminal=error)
            return
        request_id = error.server_request_id
        should_respond = request_id is not None
        if request_id is not None:
            call = self._incoming.get(request_id)
            if call is not None:
                should_respond = not call.response_sent
                call.response_sent = True
                call.task.cancel()
        self._set_terminal_error(error, subscriber_terminal=error)
        if request_id is not None and should_respond:
            await self._write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": "invalid server request",
                    },
                }
            )

    def _response_error(self, raw_error: JsonValue) -> CodexRpcError:
        if not isinstance(raw_error, dict):
            return CodexRpcError("Codex app server returned an invalid RPC error")
        message = raw_error.get("message")
        code = raw_error.get("code")
        if not isinstance(message, str):
            return CodexRpcError("Codex app server returned an invalid RPC error message")
        if not isinstance(code, int) or isinstance(code, bool):
            code = None
        return CodexRpcError(message, code=code, data=raw_error.get("data"))

    def _set_terminal_error(
        self,
        error: CodexRpcError,
        *,
        subscriber_terminal: CodexRpcError | object,
    ) -> bool:
        if self._terminal_error is not None:
            return False
        self._terminal_error = error
        self._subscriber_terminal = subscriber_terminal
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        for call in tuple(self._incoming.values()):
            call.response_sent = True
            call.task.cancel()
        for subscription in tuple(self._subscribers):
            subscription.terminate(subscriber_terminal)
        callbacks = tuple(self._terminal_callbacks)
        self._terminal_callbacks.clear()
        for callback in callbacks:
            # A callback may not re-enter this peer: the ending is reached from inside the
            # write and close locks. A callback that fails is contained here, unreported, so
            # neither its own detail nor the rest of this cleanup is lost. Terminal callback
            # failures, including BaseException such as CancelledError, are not lifecycle
            # control flow and must not stop the later callbacks.
            with suppress(BaseException):
                callback()
        return True

    def _cancel_reader(self) -> None:
        task = self._reader_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[None]) -> None:
        with suppress(asyncio.CancelledError):
            task.exception()


def _validate_outbound_params(
    params: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    try:
        validated = _as_json_value(dict(params))
    except Exception as error:
        msg = "Codex RPC params must contain valid JSON"
        raise CodexRpcError(msg) from error
    if not isinstance(validated, dict):
        msg = "Codex RPC params must contain valid JSON"
        raise CodexRpcError(msg)
    return validated


def _validate_handler_result(result: object) -> JsonValue:
    try:
        return _as_json_value(result)
    except Exception as error:
        msg = "Codex server request handler returned invalid JSON"
        raise CodexRpcError(msg) from error


def _as_json_value(value: object) -> JsonValue:
    if isinstance(value, float):
        if not isfinite(value):
            msg = "Codex app server sent a non-finite JSON number"
            raise CodexRpcError(msg)
        return value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, list):
        return [_as_json_value(item) for item in value]
    if isinstance(value, dict):
        converted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "Codex app server sent a JSON object with a non-string key"
                raise CodexRpcError(msg)
            converted[key] = _as_json_value(item)
        return converted
    msg = "Codex app server sent an unsupported JSON value"
    raise CodexRpcError(msg)
