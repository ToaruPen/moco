from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from moco.codex.rpc import CodexRpcClient
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
