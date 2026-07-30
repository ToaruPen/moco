from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from moco.errors import CodexProcessExitedError, CodexRpcError, CodexRpcTimeoutError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

_APP_SERVER_ARGS = (
    "app-server",
    "--listen",
    "stdio://",
    "--enable",
    "realtime_conversation",
)
_CLIENT_INFO: dict[str, JsonValue] = {
    "name": "moco",
    "title": "moco",
    "version": "0.1.0",
}
_NOTIFICATION_END = object()
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: dict[str, JsonValue]


class CodexRpcClient:
    def __init__(
        self,
        codex_bin: str | Path,
        *,
        request_timeout: float = 10.0,
        shutdown_timeout: float = 1.0,
    ) -> None:
        if request_timeout <= 0:
            msg = "request_timeout must be positive"
            raise ValueError(msg)
        if shutdown_timeout <= 0:
            msg = "shutdown_timeout must be positive"
            raise ValueError(msg)
        self._codex_bin = Path(codex_bin)
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[JsonValue]] = {}
        self._notifications: asyncio.Queue[RpcNotification | CodexRpcError | object] = (
            asyncio.Queue()
        )
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._next_id = 1
        self._started = False
        self._closing = False
        self._closed = False
        self._terminal_error: CodexRpcError | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.close()

    async def start(self) -> None:
        if self._started:
            if self._closed:
                msg = "Codex RPC client is already closed"
                raise CodexProcessExitedError(msg)
            return
        self._started = True
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(self._codex_bin),
                *_APP_SERVER_ARGS,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            self._closed = True
            msg = f"failed to start Codex app server at {self._codex_bin}"
            raise CodexProcessExitedError(msg) from error

        process = self._process
        if process.stdin is None or process.stdout is None or process.stderr is None:
            await self.close()
            msg = "Codex app server did not expose stdio pipes"
            raise CodexProcessExitedError(msg)

        self._reader_task = asyncio.create_task(
            self._reader_loop(process.stdout),
            name="codex-rpc-reader",
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process.stderr),
            name="codex-rpc-stderr",
        )
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": dict(_CLIENT_INFO),
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.notify("initialized")
        except BaseException:
            await self.close()
            raise

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        process = self._running_process()
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, JsonValue] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)

        try:
            await self._write(process, message)
            deadline = self._request_timeout if request_timeout is None else request_timeout
            if deadline <= 0:
                msg = "timeout must be positive"
                raise ValueError(msg)
            try:
                return await asyncio.wait_for(asyncio.shield(future), deadline)
            except TimeoutError as error:
                raise CodexRpcTimeoutError(method, deadline) from error
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
    ) -> None:
        process = self._running_process()
        message: dict[str, JsonValue] = {"method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._write(process, message)

    async def notifications(self) -> AsyncIterator[RpcNotification]:
        while True:
            item = await self._notifications.get()
            if item is _NOTIFICATION_END:
                return
            if isinstance(item, CodexRpcError):
                raise item
            yield cast("RpcNotification", item)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            process = self._process
            close_error = CodexProcessExitedError("Codex app server was closed")
            self._fail_pending(close_error)

            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                    with suppress(BrokenPipeError, ConnectionResetError):
                        await process.stdin.wait_closed()
                try:
                    await asyncio.wait_for(process.wait(), self._shutdown_timeout)
                except TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), self._shutdown_timeout)
                    except TimeoutError:
                        process.kill()
                        await process.wait()

            await self._finish_task(self._reader_task)
            await self._finish_task(self._stderr_task)
            self._closed = True
            await self._notifications.put(_NOTIFICATION_END)

    def _running_process(self) -> asyncio.subprocess.Process:
        if self._terminal_error is not None:
            raise self._terminal_error
        process = self._process
        if not self._started or process is None:
            msg = "Codex RPC client has not been started"
            raise CodexProcessExitedError(msg)
        if self._closed or self._closing:
            msg = "Codex RPC client is closed"
            raise CodexProcessExitedError(msg)
        if process.returncode is not None:
            raise self._process_exited_error(process.returncode)
        return process

    async def _write(
        self,
        process: asyncio.subprocess.Process,
        message: Mapping[str, JsonValue],
    ) -> None:
        writer = process.stdin
        if writer is None:
            msg = "Codex app server stdin is unavailable"
            raise CodexProcessExitedError(msg)
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        async with self._write_lock:
            try:
                writer.write(encoded + b"\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                terminal_error = self._process_exited_error(process.returncode)
                self._set_terminal_error(terminal_error)
                raise terminal_error from error

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while line := await reader.readline():
                message = self._decode_message(line)
                self._handle_message(message)
            if not self._closing:
                process = self._process
                returncode = await process.wait() if process is not None else None
                self._set_terminal_error(self._process_exited_error(returncode))
        except asyncio.CancelledError:
            raise
        except CodexRpcError as error:
            self._set_terminal_error(error)
            await self._terminate_after_protocol_error()

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(8192):
            fingerprint = hashlib.sha256(chunk).hexdigest()[:12]
            logger.warning(
                "Codex app-server emitted stderr (bytes=%d, fingerprint=%s)",
                len(chunk),
                fingerprint,
            )

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
            raise CodexRpcError(msg) from error
        value = _as_json_value(cast("object", decoded))
        if not isinstance(value, dict):
            msg = "Codex app server sent a non-object JSON-RPC message"
            raise CodexRpcError(msg)
        return value

    def _handle_message(self, message: dict[str, JsonValue]) -> None:
        if "id" in message:
            self._handle_response(message)
            return
        method = message.get("method")
        if not isinstance(method, str):
            msg = "Codex app server sent a notification without a method"
            raise CodexRpcError(msg)
        raw_params = message.get("params", {})
        if not isinstance(raw_params, dict):
            msg = f"Codex app server notification {method!r} had invalid params"
            raise CodexRpcError(msg)
        self._notifications.put_nowait(RpcNotification(method, raw_params))

    def _handle_response(self, message: dict[str, JsonValue]) -> None:
        request_id = message["id"]
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            msg = "Codex app server sent a response with an invalid id"
            raise CodexRpcError(msg)
        future = self._pending.get(request_id)
        if future is None:
            return
        if "error" in message:
            future.set_exception(self._response_error(message["error"]))
            return
        if "result" not in message:
            msg = "Codex app server sent a response without result or error"
            raise CodexRpcError(msg)
        future.set_result(message["result"])

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

    def _set_terminal_error(self, error: CodexRpcError) -> None:
        if self._terminal_error is not None or self._closing:
            return
        self._terminal_error = error
        self._fail_pending(error)
        self._notifications.put_nowait(error)

    def _fail_pending(self, error: CodexRpcError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def _terminate_after_protocol_error(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self._shutdown_timeout)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _finish_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _process_exited_error(returncode: int | None) -> CodexProcessExitedError:
        if returncode is None:
            return CodexProcessExitedError("Codex app server exited before reporting its status")
        return CodexProcessExitedError(
            f"Codex app server exited with status {returncode}",
            returncode=returncode,
        )


def _as_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
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
