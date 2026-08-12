from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from moco.codex.rpc import JsonValue, RpcPeer
from moco.errors import (
    CodexProcessExitedError,
    CodexRpcProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from moco.codex.rpc import RpcNotification, RpcServerRequestHandler
    from moco.platform import CodexCommand

_CLIENT_INFO: dict[str, JsonValue] = {
    "name": "moco",
    "title": "moco",
    "version": "0.1.0",
}
_STREAM_LIMIT = 1024 * 1024
_STDERR_CHUNK_SIZE = 64 * 1024

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InitializeInfo:
    user_agent: str
    platform_family: str | None
    platform_os: str | None


class CodexConnectionSupervisor:
    def __init__(
        self,
        command: CodexCommand,
        *,
        request_timeout: float = 10.0,
        shutdown_timeout: float = 1.0,
    ) -> None:
        if request_timeout <= 0:
            message = "request_timeout must be positive"
            raise ValueError(message)
        if shutdown_timeout <= 0:
            message = "shutdown_timeout must be positive"
            raise ValueError(message)
        self._command = command
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._handlers: dict[str, RpcServerRequestHandler] = {}
        self._notification_observer: Callable[[RpcNotification], None] | None = None
        self._terminal_callbacks: list[Callable[[], None]] = []
        self._process: asyncio.subprocess.Process | None = None
        self._peer: RpcPeer | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._process_task: asyncio.Task[None] | None = None
        self._initialize_info: InitializeInfo | None = None
        self._terminal_error: CodexProcessExitedError | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._closed = False

    @property
    def initialize_info(self) -> InitializeInfo:
        if self._initialize_info is None:
            message = "Codex app server is not initialized"
            raise CodexProcessExitedError(message)
        return self._initialize_info

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

    def register_server_request_handler(
        self,
        method: str,
        handler: RpcServerRequestHandler,
    ) -> None:
        if self._started or self._closing or self._closed:
            message = "server request handlers must be registered before connection start"
            raise RuntimeError(message)
        self._handlers[method] = handler

    def register_notification_observer(
        self,
        observer: Callable[[RpcNotification], None],
    ) -> None:
        if self._started or self._closing or self._closed:
            message = "notification observers must be registered before connection start"
            raise RuntimeError(message)
        if self._notification_observer is not None:
            message = "only one notification observer may be registered"
            raise RuntimeError(message)
        self._notification_observer = observer

    def register_terminal_callback(self, callback: Callable[[], None]) -> None:
        """Register one callback for this supervisor's exactly-once terminal fan-out.

        A single supervisor terminalizer is wired into the peer for normal peer endings. The
        supervisor invokes that same terminalizer when startup fails before a peer exists or
        when it closes before startup, so lifecycle owners do not depend on peer construction.
        """
        if self._started or self._closing or self._closed:
            message = "terminal callbacks must be registered before connection start"
            raise RuntimeError(message)
        self._terminal_callbacks.append(callback)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed or self._closing:
                message = "Codex connection is already closed"
                raise CodexProcessExitedError(message)
            task = self._start_task
            if task is None:
                self._started = True
                task = asyncio.create_task(
                    self._start_once(),
                    name="codex-app-server-start",
                )
                self._start_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise

    async def _start_once(self) -> None:
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command.app_server_argv(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_STREAM_LIMIT,
                )
            except OSError:
                message = "Codex app server failed to start"
                raise CodexProcessExitedError(message) from None

            self._process = process
            stdout, stdin, stderr = _stdio_pipes(process)

            peer = RpcPeer(
                stdout,
                stdin,
                request_timeout=self._request_timeout,
            )
            observer = self._notification_observer
            if observer is not None:
                peer.register_notification_observer(observer)
            for method, handler in self._handlers.items():
                peer.register_server_request_handler(method, handler)
            peer.register_terminal_callback(self._terminalize_callbacks)
            self._peer = peer
            await peer.start()
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(stderr),
                name="codex-app-server-stderr",
            )
            self._process_task = asyncio.create_task(
                self._watch_process(process, peer),
                name="codex-app-server-process",
            )
            result = await peer.request(
                "initialize",
                {
                    "clientInfo": dict(_CLIENT_INFO),
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._initialize_info = _parse_initialize_info(result)
            await peer.notify("initialized")
        except BaseException:
            self._terminalize_callbacks()
            await self._close_resources()
            raise

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        peer = self._running_peer()
        try:
            return await peer.request(method, params, request_timeout=request_timeout)
        except CodexProcessExitedError as fallback:
            error = await self._connection_error(fallback)
            raise error from None

    async def notify(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
    ) -> None:
        peer = self._running_peer()
        try:
            await peer.notify(method, params)
        except CodexProcessExitedError as fallback:
            error = await self._connection_error(fallback)
            raise error from None

    def notifications(self) -> AsyncIterator[RpcNotification]:
        return self._connection_notifications(self._running_peer().notifications())

    async def close(self) -> None:
        async with self._lifecycle_lock:
            task = self._close_task
            if task is None:
                self._closing = True
                start_task = self._start_task
                if start_task is not None and not start_task.done():
                    start_task.cancel()
                task = asyncio.create_task(
                    self._finish_close(start_task),
                    name="codex-app-server-close",
                )
                self._close_task = task
        await asyncio.shield(task)

    async def _finish_close(self, start_task: asyncio.Task[None] | None) -> None:
        if start_task is not None and start_task is not asyncio.current_task():
            await asyncio.gather(start_task, return_exceptions=True)
        await self._close_resources()

    async def _close_resources(self) -> None:
        async with self._cleanup_lock:
            if self._closed:
                return
            self._closing = True
            peer = self._peer
            if peer is not None:
                await peer.close()
            else:
                self._terminalize_callbacks()

            process = self._process
            if process is not None:
                await self._close_stdin(process)
                await self._stop_process(process)

            await self._finish_task(self._process_task)
            await self._finish_task(self._stderr_task)
            self._closed = True

    def _running_peer(self) -> RpcPeer:
        if self._terminal_error is not None:
            raise self._terminal_error
        if not self._started or self._peer is None:
            message = "Codex connection has not been started"
            raise CodexProcessExitedError(message)
        if self._closing or self._closed:
            message = "Codex connection is closed"
            raise CodexProcessExitedError(message)
        return self._peer

    async def _watch_process(
        self,
        process: asyncio.subprocess.Process,
        peer: RpcPeer,
    ) -> None:
        returncode = await process.wait()
        if self._closing:
            return
        error = self._set_process_error(returncode)
        peer.abort(error)

    async def _connection_error(
        self,
        fallback: CodexProcessExitedError,
    ) -> CodexProcessExitedError:
        terminal_error = self._terminal_error
        if terminal_error is not None:
            return terminal_error
        process = self._process
        if process is not None and process.returncode is None:
            task = self._process_task
            if task is not None and not task.done():
                await asyncio.wait({task}, timeout=self._shutdown_timeout)
        terminal_error = self._terminal_error
        if terminal_error is not None:
            return terminal_error
        if process is not None and process.returncode is not None:
            return self._set_process_error(process.returncode)
        self._terminal_error = fallback
        return fallback

    async def _connection_notifications(
        self,
        notifications: AsyncIterator[RpcNotification],
    ) -> AsyncIterator[RpcNotification]:
        try:
            async for notification in notifications:
                yield notification
        except CodexProcessExitedError as fallback:
            error = await self._connection_error(fallback)
            raise error from None

    def _set_process_error(self, returncode: int) -> CodexProcessExitedError:
        if self._terminal_error is None:
            message = f"Codex app server exited with status {returncode}"
            self._terminal_error = CodexProcessExitedError(
                message,
                returncode=returncode,
            )
        return self._terminal_error

    def _terminalize_callbacks(self) -> None:
        callbacks = tuple(self._terminal_callbacks)
        self._terminal_callbacks.clear()
        for callback in callbacks:
            # Terminal callback failures, including BaseException such as CancelledError,
            # must not interrupt cleanup or prevent a later callback from running. Callback
            # payloads are intentionally not logged or returned across this boundary.
            with suppress(BaseException):
                callback()

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(_STDERR_CHUNK_SIZE):
            fingerprint = hashlib.sha256(chunk).hexdigest()[:12]
            logger.warning(
                "Codex app server wrote stderr bytes=%d fingerprint=%s",
                len(chunk),
                fingerprint,
            )

    async def _close_stdin(self, process: asyncio.subprocess.Process) -> None:
        writer = process.stdin
        if writer is None:
            return
        writer.close()
        with suppress(BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), self._shutdown_timeout)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if await self._wait_process(process):
            return
        with suppress(ProcessLookupError):
            process.terminate()
        if await self._wait_process(process):
            return
        with suppress(ProcessLookupError):
            process.kill()
        await self._wait_process(process)

    async def _wait_process(self, process: asyncio.subprocess.Process) -> bool:
        try:
            await asyncio.wait_for(process.wait(), self._shutdown_timeout)
        except TimeoutError:
            return False
        return True

    async def _finish_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=self._shutdown_timeout)
        if task.done():
            with suppress(asyncio.CancelledError):
                task.exception()
        else:
            task.add_done_callback(self._consume_task_exception)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[None]) -> None:
        with suppress(asyncio.CancelledError):
            task.exception()


def _parse_initialize_info(result: JsonValue) -> InitializeInfo:
    if not isinstance(result, dict):
        raise _invalid_initialize_result()
    user_agent = result.get("userAgent")
    if not isinstance(user_agent, str):
        raise _invalid_initialize_result()
    return InitializeInfo(
        user_agent=user_agent,
        platform_family=_optional_metadata(result, "platformFamily"),
        platform_os=_optional_metadata(result, "platformOs"),
    )


def _stdio_pipes(
    process: asyncio.subprocess.Process,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader]:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        message = "Codex app server did not expose stdio pipes"
        raise CodexProcessExitedError(message)
    return process.stdout, process.stdin, process.stderr


def _optional_metadata(result: dict[str, JsonValue], name: str) -> str | None:
    value = result.get(name)
    if value is None or isinstance(value, str):
        return value
    raise _invalid_initialize_result()


def _invalid_initialize_result() -> CodexRpcProtocolError:
    return CodexRpcProtocolError("Codex app server returned an invalid initialize result")
