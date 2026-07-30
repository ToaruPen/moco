from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast

import pytest

from moco.codex.rpc import JsonValue, RpcNotification
from moco.codex.session import (
    CANCEL_INSTRUCTION,
    DEFAULT_REALTIME_PROMPT,
    CodexRealtimeSession,
    RealtimeErrorEvent,
    TranscriptEvent,
)
from moco.config import CodexSettings, MocoSettings
from moco.errors import CodexRpcError, CodexRpcTimeoutError

_QUEUE_END = object()


class FakeRpc:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, JsonValue]]] = []
        self.started = False
        self.closed = False
        self.thread_result: JsonValue = {"thread": {"id": "thr_test"}}
        self.emit_sdp = True
        self.fail_method: str | None = None
        self.notification_error: CodexRpcError | None = None
        self._notifications: asyncio.Queue[RpcNotification | object] = asyncio.Queue()

    async def start(self) -> None:
        self.started = True

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        del request_timeout
        copied = dict(params or {})
        self.requests.append((method, copied))
        if method == self.fail_method:
            message = "forced failure"
            raise CodexRpcError(message)
        if method == "thread/start":
            return self.thread_result
        if method == "thread/realtime/start":
            if self.emit_sdp:
                await self.emit(
                    "thread/realtime/sdp",
                    {"threadId": "thr_test", "sdp": "answer-sdp"},
                )
            return {}
        if method in {
            "thread/realtime/stop",
            "turn/interrupt",
            "thread/realtime/appendText",
        }:
            return {}
        msg = f"unexpected request: {method}"
        raise AssertionError(msg)

    async def notifications(self) -> AsyncIterator[RpcNotification]:
        if self.notification_error is not None:
            raise self.notification_error
        while True:
            item = await self._notifications.get()
            if item is _QUEUE_END:
                return
            yield cast("RpcNotification", item)

    async def close(self) -> None:
        self.closed = True
        await self._notifications.put(_QUEUE_END)

    async def emit(self, method: str, params: dict[str, JsonValue]) -> None:
        await self._notifications.put(RpcNotification(method=method, params=params))


def make_settings(tmp_path: Path) -> MocoSettings:
    return MocoSettings(
        codex=CodexSettings(
            binary=tmp_path / "unused-codex",
            working_directory=tmp_path,
        ),
    )


async def test_starts_ephemeral_read_only_audio_v3_session(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))

    answer = await session.start("offer-sdp")

    assert answer == "answer-sdp"
    assert rpc.requests[:2] == [
        (
            "thread/start",
            {
                "ephemeral": True,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "cwd": str(tmp_path),
            },
        ),
        (
            "thread/realtime/start",
            {
                "threadId": "thr_test",
                "outputModality": "audio",
                "includeStartupContext": False,
                "prompt": DEFAULT_REALTIME_PROMPT,
                "transport": {"type": "webrtc", "sdp": "offer-sdp"},
                "version": "v3",
            },
        ),
    ]
    await session.close()


async def test_exposes_transcript_and_error_notifications(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    events = session.notifications()

    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_test", "role": "assistant", "delta": "こん"},
    )
    await rpc.emit(
        "thread/realtime/transcript/done",
        {"threadId": "thr_test", "role": "assistant", "text": "こんにちは。"},
    )
    await rpc.emit(
        "thread/realtime/error",
        {"threadId": "thr_test", "message": "transport closed"},
    )

    assert await anext(events) == TranscriptEvent("delta", "thr_test", "assistant", "こん")
    assert await anext(events) == TranscriptEvent(
        "done",
        "thr_test",
        "assistant",
        "こんにちは。",
    )
    assert await anext(events) == RealtimeErrorEvent("thr_test", "transport closed")
    await session.close()


async def test_cancel_interrupts_active_turn_and_appends_stop_request(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)

    await session.cancel_current()

    assert rpc.requests[-2:] == [
        ("turn/interrupt", {"threadId": "thr_test", "turnId": "turn-1"}),
        (
            "thread/realtime/appendText",
            {
                "threadId": "thr_test",
                "role": "user",
                "text": CANCEL_INSTRUCTION,
            },
        ),
    ]
    await session.close()


async def test_sdp_timeout_stops_and_closes(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.emit_sdp = False
    session = CodexRealtimeSession(
        rpc,
        settings=make_settings(tmp_path),
        sdp_timeout=0.01,
    )

    with pytest.raises(CodexRpcTimeoutError, match="thread/realtime/sdp"):
        await session.start("offer-sdp")

    assert rpc.closed
    assert "thread/realtime/stop" in [method for method, _params in rpc.requests]


async def test_invalid_notification_surfaces_protocol_error(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_test", "delta": "missing role"},
    )

    with pytest.raises(CodexRpcError, match="invalid 'role'"):
        await anext(events)
    assert rpc.closed
    await session.close()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")

    await session.close()
    await session.close()

    methods = [method for method, _params in rpc.requests]
    assert methods.count("thread/realtime/stop") == 1
    assert session.closed


@pytest.mark.parametrize("sdp_timeout", [0.0, -1.0])
def test_rejects_non_positive_sdp_timeout(
    tmp_path: Path,
    sdp_timeout: float,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        CodexRealtimeSession(
            FakeRpc(),
            settings=make_settings(tmp_path),
            sdp_timeout=sdp_timeout,
        )


async def test_rejects_empty_duplicate_and_closed_start(tmp_path: Path) -> None:
    empty_rpc = FakeRpc()
    empty = CodexRealtimeSession(empty_rpc, settings=make_settings(tmp_path))
    with pytest.raises(ValueError, match="must not be empty"):
        await empty.start("")
    assert not empty_rpc.started

    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    with pytest.raises(CodexRpcError, match="already been started"):
        await session.start("second")
    await session.close()

    closed = CodexRealtimeSession(FakeRpc(), settings=make_settings(tmp_path))
    await closed.close()
    with pytest.raises(CodexRpcError, match="closed"):
        await closed.start("offer-sdp")


async def test_context_manager_closes_unstarted_session(tmp_path: Path) -> None:
    rpc = FakeRpc()
    async with CodexRealtimeSession(rpc, settings=make_settings(tmp_path)) as session:
        assert session.thread_id is None
    assert session.closed
    assert rpc.closed


@pytest.mark.parametrize(
    ("thread_result", "message"),
    [
        (None, "invalid result"),
        ({}, "contain a thread"),
        ({"thread": {"id": ""}}, "valid thread id"),
        ({"thread": {"id": True}}, "valid thread id"),
    ],
)
async def test_invalid_thread_results_close_rpc(
    tmp_path: Path,
    thread_result: JsonValue,
    message: str,
) -> None:
    rpc = FakeRpc()
    rpc.thread_result = thread_result
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    with pytest.raises(CodexRpcError, match=message):
        await session.start("offer-sdp")
    assert rpc.closed


async def test_start_request_failure_closes_rpc(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.fail_method = "thread/start"
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    with pytest.raises(CodexRpcError, match="forced failure"):
        await session.start("offer-sdp")
    assert rpc.closed


async def test_ignores_other_thread_and_non_realtime_events(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit("fake/status", {})
    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_other", "role": "assistant", "delta": "wrong"},
    )
    await rpc.emit(
        "thread/realtime/transcript/done",
        {"threadId": "thr_test", "role": "user", "text": "right"},
    )
    assert await anext(events) == TranscriptEvent("done", "thr_test", "user", "right")
    await session.close()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"threadId": "thr_test", "role": "system", "delta": "bad"}, "unsupported"),
        ({"role": "assistant", "delta": "bad"}, "threadId"),
    ],
)
async def test_invalid_transcript_notification_closes_rpc(
    tmp_path: Path,
    params: dict[str, JsonValue],
    message: str,
) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit("thread/realtime/transcript/delta", params)
    with pytest.raises(CodexRpcError, match=message):
        await anext(events)
    assert rpc.closed
    await session.close()


async def test_turn_completion_clears_active_turn(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    await rpc.emit("turn/started", {"threadId": "thr_test", "turn": {"id": "turn-1"}})
    await asyncio.sleep(0)
    assert session.active_turn_id == "turn-1"
    await rpc.emit(
        "turn/completed",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)
    completed_turn: object = session.active_turn_id
    assert completed_turn is None
    await session.close()


async def test_cancel_without_active_turn_still_appends_instruction(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    await session.cancel_current()
    assert rpc.requests[-1][0] == "thread/realtime/appendText"
    await session.close()


async def test_notification_stream_failure_surfaces_and_closes(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.notification_error = CodexRpcError("stream failed")
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    with pytest.raises(CodexRpcError, match="stream failed"):
        await session.start("offer-sdp")
    assert rpc.closed


async def test_stop_failure_still_closes_rpc(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = CodexRealtimeSession(rpc, settings=make_settings(tmp_path))
    await session.start("offer-sdp")
    rpc.fail_method = "thread/realtime/stop"
    with pytest.raises(CodexRpcError, match="forced failure"):
        await session.close()
    assert session.closed
    assert rpc.closed
