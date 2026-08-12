from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

from moco.codex.connection import CodexConnectionSupervisor, InitializeInfo
from moco.codex.rpc import JsonValue, RpcNotification, RpcServerRequest
from moco.errors import (
    CodexProcessExitedError,
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTimeoutError,
)
from moco.platform import CodexCommand

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_codex_script() -> Path:
    return Path(__file__).parent / "fixtures" / "fake_codex.py"


@pytest.fixture
def fake_codex_command(fake_codex_script: Path) -> CodexCommand:
    return CodexCommand((sys.executable, str(fake_codex_script)))


def scenario_command(script: Path, scenario: str) -> CodexCommand:
    return CodexCommand((sys.executable, str(script), f"--scenario={scenario}"))


@pytest.fixture
async def connection(
    fake_codex_command: CodexCommand,
) -> AsyncIterator[CodexConnectionSupervisor]:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    await supervisor.start()
    try:
        yield supervisor
    finally:
        await supervisor.close()


async def test_initializes_before_requests_and_correlates_interleaved_messages(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    await supervisor.start()
    notifications = supervisor.notifications()

    assert supervisor.initialize_info == InitializeInfo(
        user_agent="fake-codex",
        platform_family="test",
        platform_os="test",
    )
    assert await supervisor.request("ping", {"value": 7}) == {"value": 7}
    assert await anext(notifications) == RpcNotification(
        "fake/interleaved",
        {"value": 7},
    )
    await supervisor.close()


async def test_notification_observer_is_forwarded_once_before_connection_start(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    observed: list[RpcNotification] = []
    supervisor.register_notification_observer(observed.append)

    with pytest.raises(RuntimeError, match="one notification observer"):
        supervisor.register_notification_observer(lambda _notification: None)

    await supervisor.start()
    try:
        assert await supervisor.request("ping", {"value": 19}) == {"value": 19}
        assert observed[-1] == RpcNotification("fake/interleaved", {"value": 19})
        with pytest.raises(RuntimeError, match=r"before.*start"):
            supervisor.register_notification_observer(lambda _notification: None)
    finally:
        await supervisor.close()


async def test_client_accepts_portable_command_argv(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    await supervisor.start()
    try:
        assert await supervisor.request("ping", {"value": 3}) == {"value": 3}
    finally:
        await supervisor.close()


async def test_server_request_round_trip_keeps_string_id(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)

    async def answer(request: RpcServerRequest) -> JsonValue:
        return {"accepted": request.params["allowed"]}

    async def pass_barrier(request: RpcServerRequest) -> JsonValue:
        assert request.params == {"originalId": "fake-request-7"}
        return {"reached": True}

    supervisor.register_server_request_handler("fake/ask", answer)
    supervisor.register_server_request_handler("fake/barrier", pass_barrier)
    await supervisor.start()

    assert await supervisor.request(
        "trigger/server-request",
        {"idKind": "string"},
    ) == {
        "clientResponse": {"accepted": True},
        "responseIdType": "str",
        "responseCount": 1,
    }
    await supervisor.close()


class _PipeLessProcess:
    stdin = None
    stdout = None
    stderr = None
    returncode = 0

    async def wait(self) -> int:
        return 0


class _EventLogHandler(logging.Handler):
    def __init__(self, observed: asyncio.Event) -> None:
        super().__init__()
        self._observed = observed

    def emit(self, record: logging.LogRecord) -> None:
        del record
        self._observed.set()


async def test_process_uses_exact_argv_without_shell_and_rejects_missing_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = CodexCommand(("codex", "--profile", "ARGV_SECRET"))
    captured_argv: tuple[str, ...] | None = None
    captured_options: dict[str, object] = {}

    async def create_process(*argv: str, **options: object) -> asyncio.subprocess.Process:
        nonlocal captured_argv
        captured_argv = argv
        captured_options.update(options)
        return cast("asyncio.subprocess.Process", _PipeLessProcess())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    supervisor = CodexConnectionSupervisor(command)
    ended: list[str] = []
    supervisor.register_terminal_callback(lambda: ended.append("ended"))

    with pytest.raises(CodexProcessExitedError, match="did not expose stdio pipes") as caught:
        await supervisor.start()

    assert captured_argv == command.app_server_argv()
    assert "shell" not in captured_options
    assert captured_options["stdin"] is asyncio.subprocess.PIPE
    assert captured_options["stdout"] is asyncio.subprocess.PIPE
    assert captured_options["stderr"] is asyncio.subprocess.PIPE
    assert isinstance(captured_options["limit"], int)
    assert captured_options["limit"] > 0
    assert "ARGV_SECRET" not in str(caught.value)
    assert supervisor.closed
    assert ended == ["ended"]
    await supervisor.close()
    assert ended == ["ended"]


async def test_delegates_concurrent_timeout_error_notification_and_notify(
    connection: CodexConnectionSupervisor,
) -> None:
    notifications = connection.notifications()

    first = asyncio.create_task(connection.request("concurrent/first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(connection.request("concurrent/second"))
    assert await second == {"order": "second"}
    assert await first == {"order": "first"}

    with pytest.raises(CodexRpcTimeoutError, match="never"):
        await connection.request("never", request_timeout=0.01)
    with pytest.raises(CodexRpcError, match="realtime unavailable"):
        await connection.request("server/error")

    await connection.notify("client/status", {"ready": True})
    assert await connection.request("ping", {"value": 14}) == {"value": 14}
    assert await anext(notifications) == RpcNotification(
        "fake/interleaved",
        {"value": 14},
    )


async def test_process_exit_is_sticky(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    await supervisor.start()
    notifications = supervisor.notifications()

    with pytest.raises(CodexProcessExitedError, match="status 23") as caught:
        await supervisor.request("exit")
    assert caught.value.returncode == 23

    with pytest.raises(CodexProcessExitedError) as subscriber_error:
        await anext(notifications)
    assert subscriber_error.value is caught.value

    with pytest.raises(CodexProcessExitedError) as sticky_error:
        await supervisor.request("ping")
    assert sticky_error.value is caught.value

    await supervisor.close()


async def test_first_process_pipe_exit_cause_remains_sticky(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(
        fake_codex_command,
        request_timeout=1,
        shutdown_timeout=0.02,
    )
    await supervisor.start()
    notifications = supervisor.notifications()

    try:
        with pytest.raises(CodexProcessExitedError) as caught:
            await supervisor.request("stdout-eof-delayed-exit")
        if caught.value.returncode is None:
            assert "reached EOF" in str(caught.value)
        else:
            assert caught.value.returncode == 23

        with pytest.raises(CodexProcessExitedError) as subscriber_error:
            await anext(notifications)
        assert subscriber_error.value is caught.value

        process = supervisor._process  # noqa: SLF001
        assert process is not None
        async with asyncio.timeout(1):
            assert await process.wait() == 23

        with pytest.raises(CodexProcessExitedError) as sticky_error:
            await supervisor.notify("client/status", {"ready": True})
        assert sticky_error.value is caught.value
    finally:
        await supervisor.close()


async def test_eof_fallback_remains_sticky_when_process_status_arrives_later(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command)
    eof_error = CodexProcessExitedError("Codex RPC peer reached EOF")

    assert await supervisor._connection_error(eof_error) is eof_error  # noqa: SLF001
    assert supervisor._set_process_error(23) is eof_error  # noqa: SLF001
    assert eof_error.returncode is None


async def test_initialization_failure_closes_client(
    fake_codex_script: Path,
) -> None:
    supervisor = CodexConnectionSupervisor(
        scenario_command(fake_codex_script, "reject-initialize"),
        request_timeout=1,
    )

    with pytest.raises(CodexRpcError, match="initialize rejected"):
        await supervisor.start()

    assert supervisor.closed
    await supervisor.close()


@pytest.mark.parametrize(
    "scenario",
    ["initialize-missing-user-agent", "initialize-invalid-platform"],
)
async def test_malformed_initialize_metadata_is_redacted_protocol_error(
    fake_codex_script: Path,
    scenario: str,
) -> None:
    sensitive_value = "INITIALIZE_METADATA_SECRET"
    supervisor = CodexConnectionSupervisor(
        scenario_command(fake_codex_script, scenario),
        request_timeout=1,
    )

    with pytest.raises(CodexRpcProtocolError, match="invalid initialize result") as caught:
        await supervisor.start()

    assert sensitive_value not in str(caught.value)
    assert supervisor.closed


async def test_stderr_is_logged_by_size_and_fingerprint_only(
    connection: CodexConnectionSupervisor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="moco.codex.connection")
    stderr_observed = asyncio.Event()
    stderr_handler = _EventLogHandler(stderr_observed)
    connection_logger = logging.getLogger("moco.codex.connection")
    connection_logger.addHandler(stderr_handler)

    try:
        assert await connection.request("stderr") == {}
        async with asyncio.timeout(1):
            await stderr_observed.wait()
    finally:
        connection_logger.removeHandler(stderr_handler)

    assert "bytes=" in caplog.text
    assert "fingerprint=" in caplog.text
    assert "diagnostic output" not in caplog.text
    assert "RPC_SENSITIVE" not in caplog.text


async def test_close_is_idempotent_and_bounds_uncooperative_process(
    fake_codex_script: Path,
) -> None:
    supervisor = CodexConnectionSupervisor(
        scenario_command(fake_codex_script, "uncooperative-close"),
        request_timeout=1,
        shutdown_timeout=0.02,
    )
    await supervisor.start()
    assert await supervisor.request("hang-on-close") == {}

    async with asyncio.timeout(1):
        await supervisor.close()
        await supervisor.close()

    assert supervisor.closed


async def test_missing_binary_reports_process_error(tmp_path: Path) -> None:
    secret_path = tmp_path / "CODEX_PATH_SECRET" / "missing-codex"
    supervisor = CodexConnectionSupervisor(CodexCommand((str(secret_path),)))
    ended: list[str] = []
    supervisor.register_terminal_callback(lambda: ended.append("ended"))

    with pytest.raises(CodexProcessExitedError, match="failed to start") as caught:
        await supervisor.start()

    assert str(secret_path) not in str(caught.value)
    assert "CODEX_PATH_SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert supervisor.closed
    assert ended == ["ended"]
    await supervisor.close()
    assert ended == ["ended"]


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("request_timeout", 0.0, "request_timeout must be positive"),
        ("shutdown_timeout", -1.0, "shutdown_timeout must be positive"),
    ],
)
def test_rejects_non_positive_constructor_timeouts(
    fake_codex_command: CodexCommand,
    keyword: str,
    value: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CodexConnectionSupervisor(fake_codex_command, **{keyword: value})


async def test_start_is_idempotent_and_closed_connection_cannot_restart(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command)
    await supervisor.start()
    await supervisor.start()
    await supervisor.close()

    with pytest.raises(CodexProcessExitedError, match="already closed"):
        await supervisor.start()


async def test_close_before_start_prevents_process_creation(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command)
    ended: list[str] = []
    supervisor.register_terminal_callback(lambda: ended.append("ended"))

    await supervisor.close()
    try:
        with pytest.raises(CodexProcessExitedError, match="already closed"):
            await supervisor.start()
        assert supervisor._process is None  # noqa: SLF001
    finally:
        process = supervisor._process  # noqa: SLF001
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()

    assert ended == ["ended"]
    await supervisor.close()
    assert ended == ["ended"]
    with pytest.raises(RuntimeError, match=r"before.*start"):
        supervisor.register_terminal_callback(lambda: ended.append("late"))


async def test_concurrent_starts_return_only_after_single_initialize(
    fake_codex_command: CodexCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_create = cast(
        "Callable[..., Awaitable[asyncio.subprocess.Process]]",
        asyncio.create_subprocess_exec,
    )
    create_started = asyncio.Event()
    allow_create = asyncio.Event()
    create_count = 0

    async def delayed_create(
        *argv: str,
        **options: object,
    ) -> asyncio.subprocess.Process:
        nonlocal create_count
        create_count += 1
        create_started.set()
        await allow_create.wait()
        return await real_create(*argv, **options)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    first = asyncio.create_task(supervisor.start())
    await create_started.wait()
    second = asyncio.create_task(supervisor.start())
    await asyncio.sleep(0)

    try:
        assert not second.done()
        allow_create.set()
        await asyncio.gather(first, second)
        assert create_count == 1
        assert supervisor.initialize_info.user_agent == "fake-codex"
    finally:
        allow_create.set()
        await supervisor.close()


async def test_concurrent_start_and_close_cancels_process_creation(
    fake_codex_command: CodexCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_create = cast(
        "Callable[..., Awaitable[asyncio.subprocess.Process]]",
        asyncio.create_subprocess_exec,
    )
    create_started = asyncio.Event()
    allow_create = asyncio.Event()

    async def delayed_create(
        *argv: str,
        **options: object,
    ) -> asyncio.subprocess.Process:
        create_started.set()
        await allow_create.wait()
        return await real_create(*argv, **options)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    ended: list[str] = []
    supervisor.register_terminal_callback(lambda: ended.append("ended"))
    starting = asyncio.create_task(supervisor.start())
    await create_started.wait()
    closing = asyncio.create_task(supervisor.close())
    await asyncio.sleep(0)

    try:
        allow_create.set()
        start_result, close_result = await asyncio.gather(
            starting,
            closing,
            return_exceptions=True,
        )
        process = supervisor._process  # noqa: SLF001
        assert isinstance(start_result, asyncio.CancelledError)
        assert close_result is None
        assert supervisor.closed
        assert process is None or process.returncode is not None
        assert ended == ["ended"]
    finally:
        allow_create.set()
        for task in (starting, closing):
            if not task.done():
                task.cancel()
        process = supervisor._process  # noqa: SLF001
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_supervisor_contains_cancelled_terminal_callback_before_peer_exists(
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "SUPERVISOR_CALLBACK_SECRET" / "missing-codex"
    supervisor = CodexConnectionSupervisor(CodexCommand((str(secret_path),)))
    ended: list[str] = []

    def cancel() -> None:
        ended.append("cancelled")
        raise asyncio.CancelledError

    supervisor.register_terminal_callback(cancel)
    supervisor.register_terminal_callback(lambda: ended.append("after"))

    with pytest.raises(CodexProcessExitedError, match="failed to start"):
        await supervisor.start()

    assert ended == ["cancelled", "after"]
    await supervisor.close()
    assert ended == ["cancelled", "after"]


async def test_close_cancels_unresponsive_initialize_and_reaps_process(
    fake_codex_script: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="moco.codex.connection")
    stderr_observed = asyncio.Event()
    stderr_handler = _EventLogHandler(stderr_observed)
    connection_logger = logging.getLogger("moco.codex.connection")
    connection_logger.addHandler(stderr_handler)
    supervisor = CodexConnectionSupervisor(
        scenario_command(fake_codex_script, "unresponsive-initialize"),
        request_timeout=30,
        shutdown_timeout=0.02,
    )
    starting = asyncio.create_task(supervisor.start())

    try:
        async with asyncio.timeout(1):
            await stderr_observed.wait()
        assert "bytes=" in caplog.text
        process = supervisor._process  # noqa: SLF001
        assert process is not None

        async with asyncio.timeout(1):
            await supervisor.close()
        with pytest.raises((asyncio.CancelledError, CodexProcessExitedError)):
            await starting

        assert supervisor.closed
        assert process.returncode is not None
        await supervisor.close()
    finally:
        connection_logger.removeHandler(stderr_handler)
        if not starting.done():
            starting.cancel()
        with suppress(asyncio.CancelledError, CodexProcessExitedError):
            await starting
        await supervisor.close()
        process = supervisor._process  # noqa: SLF001
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_cancelled_start_cleans_up_shared_startup_and_process(
    fake_codex_script: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="moco.codex.connection")
    stderr_observed = asyncio.Event()
    stderr_handler = _EventLogHandler(stderr_observed)
    connection_logger = logging.getLogger("moco.codex.connection")
    connection_logger.addHandler(stderr_handler)
    supervisor = CodexConnectionSupervisor(
        scenario_command(fake_codex_script, "unresponsive-initialize"),
        request_timeout=30,
        shutdown_timeout=0.02,
    )
    starting = asyncio.create_task(supervisor.start())

    try:
        async with asyncio.timeout(1):
            await stderr_observed.wait()
        process = supervisor._process  # noqa: SLF001
        shared_start = supervisor._start_task  # noqa: SLF001
        assert process is not None
        assert shared_start is not None

        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting

        assert supervisor.closed
        assert shared_start.done()
        assert process.returncode is not None
        assert "Task exception was never retrieved" not in caplog.text
        await supervisor.close()
    finally:
        connection_logger.removeHandler(stderr_handler)
        if not starting.done():
            starting.cancel()
        with suppress(asyncio.CancelledError, CodexProcessExitedError):
            await starting
        await supervisor.close()
        process = supervisor._process  # noqa: SLF001
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def test_initialize_info_and_operations_require_started_connection(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command)

    with pytest.raises(CodexProcessExitedError, match="not initialized"):
        _ = supervisor.initialize_info
    with pytest.raises(CodexProcessExitedError, match="has not been started"):
        await supervisor.request("ping")
    with pytest.raises(CodexProcessExitedError, match="has not been started"):
        await supervisor.notify("ping")


async def test_handler_registration_after_start_is_rejected(
    connection: CodexConnectionSupervisor,
) -> None:
    async def answer(request: RpcServerRequest) -> JsonValue:
        del request
        return None

    with pytest.raises(RuntimeError, match=r"before.*start"):
        connection.register_server_request_handler("fake/late", answer)


async def test_terminal_callback_registration_after_start_is_rejected(
    connection: CodexConnectionSupervisor,
) -> None:
    with pytest.raises(RuntimeError, match=r"before.*start"):
        connection.register_terminal_callback(lambda: None)


async def test_a_registered_terminal_callback_runs_once_on_close(
    fake_codex_command: CodexCommand,
) -> None:
    """The supervisor owns the callback and the peer supplies the normal close event."""
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    ended: list[str] = []
    supervisor.register_terminal_callback(lambda: ended.append("ended"))
    await supervisor.start()

    assert ended == []
    await supervisor.close()
    await supervisor.close()

    assert ended == ["ended"]


async def test_a_registered_terminal_callback_runs_when_the_process_exits(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    ended: list[str] = []
    supervisor.register_terminal_callback(lambda: ended.append("ended"))
    await supervisor.start()

    with pytest.raises(CodexProcessExitedError, match="status 23"):
        await supervisor.request("exit")

    assert ended == ["ended"]
    await supervisor.close()
    assert ended == ["ended"]


async def test_context_manager_and_duplicate_start(
    fake_codex_command: CodexCommand,
) -> None:
    async with CodexConnectionSupervisor(fake_codex_command) as supervisor:
        await supervisor.start()
        assert await supervisor.request("ping", {"value": 11}) == {"value": 11}

    assert supervisor.closed
    with pytest.raises(CodexProcessExitedError, match="already closed"):
        await supervisor.start()
