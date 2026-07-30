from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from moco.codex.rpc import CodexRpcClient, RpcNotification, _as_json_value
from moco.errors import CodexProcessExitedError, CodexRpcError, CodexRpcTimeoutError


@pytest.fixture
def fake_codex_path() -> Path:
    return Path(__file__).parent / "fixtures" / "fake_codex.py"


@pytest.fixture
async def rpc_client(fake_codex_path: Path) -> AsyncIterator[CodexRpcClient]:
    client = CodexRpcClient(fake_codex_path, request_timeout=1.0)
    await client.start()
    try:
        yield client
    finally:
        await client.close()


async def test_initializes_before_requests_and_correlates_interleaved_messages(
    rpc_client: CodexRpcClient,
) -> None:
    notifications = rpc_client.notifications()

    assert (await anext(notifications)).method == "fake/ready"
    response = await rpc_client.request("ping", {"value": 7})

    assert response == {"value": 7}
    interleaved = await anext(notifications)
    assert interleaved.method == "fake/interleaved"
    assert interleaved.params == {"value": 7}


async def test_correlates_concurrent_responses_received_out_of_order(
    rpc_client: CodexRpcClient,
) -> None:
    first = asyncio.create_task(rpc_client.request("concurrent/first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(rpc_client.request("concurrent/second"))

    assert await second == {"order": "second"}
    assert await first == {"order": "first"}


async def test_converts_server_error_to_domain_error(rpc_client: CodexRpcClient) -> None:
    with pytest.raises(CodexRpcError, match="realtime unavailable") as caught:
        await rpc_client.request("server/error")

    assert caught.value.code == -32000
    assert caught.value.data == {"retryable": False}


async def test_timeout_does_not_poison_connection(rpc_client: CodexRpcClient) -> None:
    with pytest.raises(CodexRpcTimeoutError, match="never"):
        await rpc_client.request("never", request_timeout=0.01)

    assert await rpc_client.request("ping", {"value": 9}) == {"value": 9}


async def test_malformed_server_message_fails_request(rpc_client: CodexRpcClient) -> None:
    with pytest.raises(CodexRpcError, match="invalid JSON"):
        await rpc_client.request("malformed")


async def test_process_exit_is_sticky(rpc_client: CodexRpcClient) -> None:
    with pytest.raises(CodexProcessExitedError, match="status 23"):
        await rpc_client.request("exit")

    with pytest.raises(CodexProcessExitedError, match="status 23"):
        await rpc_client.request("ping", {"value": 1})


async def test_close_is_idempotent_and_bounds_uncooperative_process(
    fake_codex_path: Path,
) -> None:
    client = CodexRpcClient(fake_codex_path, shutdown_timeout=0.01)
    await client.start()
    assert await client.request("hang-on-close") == {}

    await client.close()
    await client.close()

    assert client.closed


async def test_stderr_is_logged_by_size_and_fingerprint_only(
    rpc_client: CodexRpcClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="moco.codex.rpc")
    assert await rpc_client.request("stderr") == {}
    await asyncio.sleep(0)

    assert "bytes=" in caplog.text
    assert "fingerprint=" in caplog.text
    assert "diagnostic output" not in caplog.text
    assert "RPC_SENSITIVE" not in caplog.text


async def test_missing_binary_reports_process_error(tmp_path: Path) -> None:
    client = CodexRpcClient(tmp_path / "missing-codex")

    with pytest.raises(CodexProcessExitedError, match="failed to start"):
        await client.start()


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("request_timeout", 0.0, "request_timeout must be positive"),
        ("shutdown_timeout", -1.0, "shutdown_timeout must be positive"),
    ],
)
def test_rejects_non_positive_constructor_timeouts(
    fake_codex_path: Path,
    keyword: str,
    value: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CodexRpcClient(fake_codex_path, **{keyword: value})


@pytest.mark.parametrize("operation", ["request", "notify"])
async def test_rejects_operations_before_start(
    fake_codex_path: Path,
    operation: str,
) -> None:
    client = CodexRpcClient(fake_codex_path)

    with pytest.raises(CodexProcessExitedError, match="has not been started"):
        await getattr(client, operation)("ping")


@pytest.mark.parametrize("operation", ["request", "notify"])
async def test_rejects_operations_after_close(
    fake_codex_path: Path,
    operation: str,
) -> None:
    client = CodexRpcClient(fake_codex_path)
    await client.start()
    await client.close()

    with pytest.raises(CodexProcessExitedError, match="closed"):
        await getattr(client, operation)("ping")


async def test_context_manager_and_duplicate_start(fake_codex_path: Path) -> None:
    async with CodexRpcClient(fake_codex_path) as client:
        await client.start()
        assert await client.request("ping", {"value": 11}) == {"value": 11}

    assert client.closed
    with pytest.raises(CodexProcessExitedError, match="already closed"):
        await client.start()


async def test_initialization_failure_closes_client(
    fake_codex_path: Path,
    tmp_path: Path,
) -> None:
    rejecting = tmp_path / "reject-initialize-codex"
    rejecting.symlink_to(fake_codex_path)
    client = CodexRpcClient(rejecting)

    with pytest.raises(CodexRpcError, match="initialize rejected"):
        await client.start()

    assert client.closed


async def test_rejects_non_positive_request_timeout(
    rpc_client: CodexRpcClient,
) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        await rpc_client.request("never", request_timeout=0)


@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("non-object", "non-object"),
        ("invalid-constant", "invalid JSON"),
        ("invalid-id", "invalid id"),
        ("missing-result", "without result"),
        ("notification-without-method", "without a method"),
        ("notification-invalid-params", "invalid params"),
    ],
)
async def test_protocol_violations_are_terminal(
    fake_codex_path: Path,
    method: str,
    message: str,
) -> None:
    client = CodexRpcClient(fake_codex_path, request_timeout=1)
    await client.start()

    with pytest.raises(CodexRpcError, match=message):
        await client.request(method)

    await client.close()


@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("invalid-error", "invalid RPC error"),
        ("invalid-error-message", "invalid RPC error message"),
        ("invalid-error-code", "bad code"),
    ],
)
async def test_normalizes_invalid_error_shapes(
    rpc_client: CodexRpcClient,
    method: str,
    message: str,
) -> None:
    with pytest.raises(CodexRpcError, match=message) as caught:
        await rpc_client.request(method)

    assert caught.value.code is None


async def test_unknown_response_and_default_notification_params(
    rpc_client: CodexRpcClient,
) -> None:
    notifications = rpc_client.notifications()
    await anext(notifications)
    assert await rpc_client.request("unknown-response") == {"accepted": True}
    await rpc_client.request("notification-default-params")
    assert await anext(notifications) == RpcNotification("fake/default-params", {})


async def test_notify_sends_params(rpc_client: CodexRpcClient) -> None:
    await rpc_client.notify("client/status", {"ready": True})
    assert await rpc_client.request("ping", {"value": 14}) == {"value": 14}


async def test_notification_iterator_ends_after_close(fake_codex_path: Path) -> None:
    client = CodexRpcClient(fake_codex_path)
    await client.start()
    notifications = client.notifications()
    await anext(notifications)
    await client.close()

    with pytest.raises(StopAsyncIteration):
        await anext(notifications)


def test_json_value_validation() -> None:
    value = {"items": [None, True, 1, 1.5, "text"]}
    assert _as_json_value(value) == value
    with pytest.raises(CodexRpcError, match="non-string key"):
        _as_json_value({1: "value"})
    with pytest.raises(CodexRpcError, match="unsupported"):
        _as_json_value(object())
