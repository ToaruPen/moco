from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import moco.codex.agent as agent_module
from moco.codex.agent import AgentActivityEvent, AgentActivityKind, AgentSession
from moco.codex.capabilities import (
    ApprovalMode,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    SandboxMode,
)
from moco.codex.rpc import JsonValue, RpcNotification, RpcPeer
from moco.codex.schema import (
    AgentEventProfile,
    ClientMethodContract,
    CodexProtocolContract,
    ParamsKind,
    SemanticMethod,
)
from moco.config import AgentProfileMode
from moco.errors import (
    AgentTurnErrorCode,
    CodexAgentError,
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTimeoutError,
)

WORKING_DIRECTORY = Path.cwd() / "agent-workspace"
THREAD_NOTIFICATION = "thread/started"
TURN_NOTIFICATION = "turn/started"
TURN_COMPLETED_NOTIFICATION = "turn/completed"
ITEM_COMPLETED_NOTIFICATION = "item/completed"
ITEM_STARTED_NOTIFICATION = "item/started"
AGENT_MESSAGE_DELTA_NOTIFICATION = "item/agentMessage/delta"

WIRE_METHODS = {
    SemanticMethod.THREAD_START: "effective/thread-start",
    SemanticMethod.TURN_START: "effective/turn-start",
    SemanticMethod.TURN_STEER: "effective/turn-steer",
    SemanticMethod.TURN_INTERRUPT: "effective/turn-interrupt",
}
REQUIRED_FIELDS = {
    SemanticMethod.THREAD_START: frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
    SemanticMethod.TURN_START: frozenset({"input", "threadId"}),
    SemanticMethod.TURN_STEER: frozenset({"expectedTurnId", "input", "threadId"}),
    SemanticMethod.TURN_INTERRUPT: frozenset({"threadId", "turnId"}),
}


def effective_contract(
    *,
    overrides: Mapping[SemanticMethod, ClientMethodContract] | None = None,
) -> CodexProtocolContract:
    methods = {
        semantic: ClientMethodContract(
            WIRE_METHODS[semantic],
            ParamsKind.OBJECT,
            REQUIRED_FIELDS[semantic],
        )
        for semantic in WIRE_METHODS
    }
    methods.update(overrides or {})
    return CodexProtocolContract(
        version="test-contract",
        methods=methods,
        server_requests={},
        unclassified_server_request_count=0,
        experimental_schema=True,
        agent_event_profile=AgentEventProfile(
            turn_completed_method=TURN_COMPLETED_NOTIFICATION,
            item_completed_method=ITEM_COMPLETED_NOTIFICATION,
            agent_message_delta_method=AGENT_MESSAGE_DELTA_NOTIFICATION,
            turn_completed_required_fields=frozenset({"threadId", "turn"}),
            item_completed_required_fields=frozenset({"threadId", "turnId", "item"}),
            turn_required_fields=frozenset({"id", "items", "status"}),
            agent_message_required_fields=frozenset({"id", "text", "type"}),
            turn_completed_field_types={
                "threadId": frozenset({"string"}),
                "turn": frozenset({"object"}),
            },
            item_completed_field_types={
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
                "item": frozenset({"object"}),
            },
            turn_field_types={
                "id": frozenset({"string"}),
                "items": frozenset({"array"}),
                "status": frozenset({"string"}),
            },
            agent_message_field_types={
                "id": frozenset({"string"}),
                "text": frozenset({"string"}),
                "type": frozenset({"string"}),
                "phase": frozenset({"string", "null"}),
            },
            agent_message_delta_required_fields=frozenset(
                {"threadId", "turnId", "itemId", "delta"}
            ),
            agent_message_delta_field_types={
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
                "itemId": frozenset({"string"}),
                "delta": frozenset({"string"}),
            },
            agent_message_phase_values=frozenset({"commentary", "final_answer"}),
            agent_message_phase_optional=True,
            turn_status_values=frozenset({"completed", "interrupted", "failed", "inProgress"}),
            completed_status="completed",
            interrupted_status="interrupted",
            failed_status="failed",
            in_progress_status="inProgress",
            item_started_method=ITEM_STARTED_NOTIFICATION,
            item_started_required_fields=frozenset({"threadId", "turnId", "item"}),
            item_started_field_types={
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
                "item": frozenset({"object"}),
            },
        ),
    )


def capabilities(
    status: CapabilityStatus = CapabilityStatus.AVAILABLE,
    *,
    steer_status: CapabilityStatus | None = None,
    effective_policy: EffectivePolicy | None = None,
    unknown_policy: bool = False,
) -> CapabilitySnapshot:
    state = CapabilityState(status, "private capability detail")
    policy = (
        None
        if unknown_policy
        else effective_policy or EffectivePolicy(SandboxMode.READ_ONLY, ApprovalMode.NEVER)
    )
    return CapabilitySnapshot(
        version="test-capabilities",
        account=state,
        effective_policy=policy,
        policy_state=state,
        managed_requirements=state,
        agent_admission=state,
        realtime=state,
        interrupt=state,
        steer=CapabilityState(steer_status or status, "private steer detail"),
        server_requests=state,
        server_request_categories=frozenset(),
        has_unclassified_server_requests=False,
    )


class FakeSharedConnection:
    """A started shared connection whose close method must never be used by AgentSession."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []
        self.notifications_queue: asyncio.Queue[object] = asyncio.Queue()
        self.thread_result: JsonValue = {"thread": {"id": "agent-thread-1"}}
        self.request_error: BaseException | None = None
        self.turn_number = 0
        self.thread_requested = asyncio.Event()
        self.thread_start_cancelled = asyncio.Event()
        self.turn_requested = asyncio.Event()
        self.thread_start_gate: asyncio.Event | None = None
        self.turn_start_gate: asyncio.Event | None = None
        self.steer_gate: asyncio.Event | None = None
        self.steer_requested = asyncio.Event()
        self.steer_completed = asyncio.Event()
        self.steer_cancelled = asyncio.Event()
        self.steer_error: BaseException | None = None
        self.steer_ignore_cancellation = False
        self.steer_settled_callback: Callable[[], object] | None = None
        self.steer_result: JsonValue = {"turnId": "agent-turn-1"}
        self.interrupt_gate: asyncio.Event | None = None
        self.interrupt_requested = asyncio.Event()
        self.interrupt_completed = asyncio.Event()
        self.interrupt_error: BaseException | None = None
        self.ignore_pump_cancellation = False
        self.pump_waiting = asyncio.Event()
        self.pump_cancelled = asyncio.Event()
        self.pump_finished = asyncio.Event()
        self.release_pump = asyncio.Event()
        self.close_called = False

    async def request(  # noqa: C901, PLR0912
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        **kwargs: object,
    ) -> JsonValue:
        self.calls.append((method, params, kwargs))
        if self.request_error is not None:
            raise self.request_error
        if method == WIRE_METHODS[SemanticMethod.THREAD_START]:
            self.thread_requested.set()
            try:
                if self.thread_start_gate is not None:
                    await self.thread_start_gate.wait()
            except asyncio.CancelledError:
                self.thread_start_cancelled.set()
                raise
            return self.thread_result
        if method == WIRE_METHODS[SemanticMethod.TURN_START]:
            self.turn_number += 1
            self.turn_requested.set()
            if self.turn_start_gate is not None:
                await self.turn_start_gate.wait()
            return {"turn": {"id": f"agent-turn-{self.turn_number}"}}
        if method == WIRE_METHODS[SemanticMethod.TURN_STEER]:
            self.steer_requested.set()
            if self.steer_gate is not None:
                try:
                    await self.steer_gate.wait()
                except asyncio.CancelledError:
                    self.steer_cancelled.set()
                    if not self.steer_ignore_cancellation:
                        raise
                    await self.steer_gate.wait()
            if self.steer_settled_callback is not None:
                self.steer_settled_callback()
            if self.steer_error is not None:
                raise self.steer_error
            self.steer_completed.set()
            return self.steer_result
        if method == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]:
            self.interrupt_requested.set()
            if self.interrupt_gate is not None:
                await self.interrupt_gate.wait()
            if self.interrupt_error is not None:
                raise self.interrupt_error
            self.interrupt_completed.set()
            return {}
        message = "unexpected semantic alias in test"
        raise AssertionError(message)

    def notifications(self) -> AsyncIterator[RpcNotification]:
        async def stream() -> AsyncIterator[RpcNotification]:
            try:
                while True:
                    if self.ignore_pump_cancellation:
                        self.pump_waiting.set()
                    try:
                        event = await self.notifications_queue.get()
                    except asyncio.CancelledError:
                        if not self.ignore_pump_cancellation:
                            raise
                        self.pump_cancelled.set()
                        await self.release_pump.wait()
                        raise
                    if isinstance(event, BaseException):
                        raise event
                    yield cast("RpcNotification", event)
            finally:
                self.pump_finished.set()

        return stream()

    async def emit(self, method: str, params: dict[str, JsonValue]) -> None:
        await self.notifications_queue.put(RpcNotification(method, params))

    async def lose_connection(self, error: BaseException | None = None) -> None:
        await self.notifications_queue.put(error or RuntimeError("connection secret"))

    async def close(self) -> None:
        self.close_called = True


class _PeerWriter:
    def __init__(self, written: asyncio.Queue[dict[str, JsonValue]]) -> None:
        self.written = written

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            self.written.put_nowait(cast("dict[str, JsonValue]", json.loads(line)))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class RpcPeerSharedConnection:
    """A real RpcPeer with an in-memory wire, used for notification/response ordering."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.written: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue()
        self.writer = _PeerWriter(self.written)
        self.peer = RpcPeer(
            self.reader,
            cast("asyncio.StreamWriter", self.writer),
            request_timeout=1.0,
        )

    async def start(self) -> None:
        await self.peer.start()

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        return await self.peer.request(method, params, request_timeout=request_timeout)

    def notifications(self) -> AsyncIterator[RpcNotification]:
        return self.peer.notifications()

    async def next_written(self) -> dict[str, JsonValue]:
        return await asyncio.wait_for(self.written.get(), 1.0)

    async def feed(self, message: dict[str, JsonValue]) -> None:
        self.reader.feed_data(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await asyncio.sleep(0)

    async def close(self) -> None:
        await self.peer.close()


def make_session(
    connection: FakeSharedConnection,
    *,
    profile: AgentProfileMode = AgentProfileMode.READ_ONLY,
    contract: CodexProtocolContract | None = None,
    snapshot: CapabilitySnapshot | None = None,
    working_directory: Path = WORKING_DIRECTORY,
    activity_sink: Callable[[AgentActivityEvent], object] | None = None,
    terminal_sink: Callable[[], object] | None = None,
) -> AgentSession:
    return AgentSession(
        connection,
        contract or effective_contract(),
        snapshot or capabilities(),
        working_directory,
        profile,
        activity_sink=activity_sink,
        terminal_sink=terminal_sink,
    )


async def wait_for_active_agent_turn(session: AgentSession) -> None:
    for _ in range(20):
        if session.active_turn_id is not None:
            return
        await asyncio.sleep(0)
    message = "Agent turn did not become active"
    raise AssertionError(message)


async def finish_turn(
    connection: FakeSharedConnection,
    session: AgentSession,
    text: str,
) -> str:
    task = asyncio.create_task(session.start_turn(text))
    await connection.turn_requested.wait()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": f"agent-turn-{connection.turn_number}",
            "item": {
                "id": f"agent-item-{connection.turn_number}",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "final answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {
                "id": f"agent-turn-{connection.turn_number}",
                "items": [],
                "status": "completed",
            },
        },
    )
    return await task


async def start_active_turn(
    connection: FakeSharedConnection,
    session: AgentSession,
    text: str = "hello",
) -> asyncio.Task[str]:
    task = asyncio.create_task(session.start_turn(text))
    await connection.turn_requested.wait()
    for _ in range(10):
        if session.active_turn_id is not None:
            return task
        await asyncio.sleep(0)
    message = "turn did not become active"
    raise AssertionError(message)


async def test_steer_targets_the_snapshotted_active_turn_with_exact_payload() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    await session.steer("追加指示")

    steer_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_STEER]
    ]
    assert steer_calls == [
        (
            WIRE_METHODS[SemanticMethod.TURN_STEER],
            {
                "expectedTurnId": "agent-turn-1",
                "input": [{"type": "text", "text": "追加指示"}],
                "threadId": "agent-thread-1",
            },
            {},
        )
    ]
    assert session.active_turn_id == "agent-turn-1"
    assert not turn.done()
    await session.close()
    with suppress(CodexAgentError):
        await turn


@pytest.mark.parametrize(
    "text",
    ["  \n", "x" * (64 * 1024 + 1)],
    ids=["blank", "too_long"],
)
async def test_steer_rejects_invalid_text_before_send(text: str) -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    with pytest.raises(CodexAgentError, match="agent input is invalid"):
        await session.steer(text)

    assert all(call[0] != WIRE_METHODS[SemanticMethod.TURN_STEER] for call in connection.calls)
    assert not turn.done()
    await session.close()
    with suppress(CodexAgentError):
        await turn


async def test_steer_requires_an_active_turn_before_send() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)

    with pytest.raises(CodexAgentError, match="no Agent turn is active"):
        await session.steer("追加指示")

    assert connection.calls == []
    await session.close()


async def test_steer_requires_the_optional_capability_before_send() -> None:
    connection = FakeSharedConnection()
    session = make_session(
        connection,
        snapshot=capabilities(steer_status=CapabilityStatus.VERSION_MISMATCH),
    )
    turn = await start_active_turn(connection, session)

    with pytest.raises(CodexAgentError, match="agent steer is unavailable"):
        await session.steer("追加指示")

    assert all(call[0] != WIRE_METHODS[SemanticMethod.TURN_STEER] for call in connection.calls)
    assert not turn.done()
    await session.close()
    with suppress(CodexAgentError):
        await turn


async def test_steer_known_rpc_rejection_preserves_the_running_turn() -> None:
    connection = FakeSharedConnection()
    connection.steer_error = CodexRpcError(
        "SERVER_STEER_SECRET",
        code=-32000,
        data={"private": "payload"},
    )
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    with pytest.raises(CodexAgentError) as caught:
        await session.steer("追加指示")

    assert str(caught.value) == "agent_steer_rejected"
    assert caught.value.code == "agent_steer_rejected"
    assert "SERVER_STEER_SECRET" not in repr(caught.value)
    assert "payload" not in repr(caught.value)
    assert session.active_turn_id == "agent-turn-1"
    assert session.reusable
    assert not turn.done()
    await session.close()
    with suppress(CodexAgentError):
        await turn


@pytest.mark.parametrize("known_rejection", [False, True])
async def test_steer_settled_cancellation_preserves_the_known_inner_outcome(
    known_rejection: bool,
) -> None:
    connection = FakeSharedConnection()
    if known_rejection:
        connection.steer_error = CodexRpcError("PRIVATE_REJECTION", code=-32000)
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer: asyncio.Task[None]
    connection.steer_settled_callback = lambda: asyncio.get_running_loop().call_soon(steer.cancel)
    steer = asyncio.create_task(session.steer("追加指示"))

    with pytest.raises(asyncio.CancelledError):
        await steer

    try:
        assert session.reusable
        assert session.active_turn_id == "agent-turn-1"
        assert not turn.done()
    finally:
        await session.close()
        with suppress(CodexAgentError):
            await turn


async def test_steer_terminal_precedence_over_a_late_known_rejection() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    connection.steer_error = CodexRpcError("PRIVATE_REJECTION", code=-32000)
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer = asyncio.create_task(session.steer("追加指示"))
    await connection.steer_requested.wait()

    await connection.lose_connection(RuntimeError("PRIVATE_CONNECTION"))
    with pytest.raises(CodexAgentError) as terminal:
        await turn
    assert terminal.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
    connection.steer_gate.set()

    with pytest.raises(CodexAgentError) as caught:
        await steer
    try:
        assert caught.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
        assert str(caught.value) == "agent turn outcome is unknown"
        assert not session.reusable
    finally:
        await session.close()


@pytest.mark.parametrize("code", [None, True])
async def test_steer_invalid_rpc_error_code_is_unknown(code: int | None) -> None:
    connection = FakeSharedConnection()
    connection.steer_error = CodexRpcError(
        "PRIVATE_MALFORMED_ERROR",
        code=code,
        data={"private": "payload"},
    )
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    with pytest.raises(CodexAgentError) as caught:
        await session.steer("追加指示")

    try:
        assert caught.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
        assert "PRIVATE_MALFORMED_ERROR" not in repr(caught.value)
        assert "payload" not in repr(caught.value)
        assert not session.reusable
        with pytest.raises(CodexAgentError) as terminal:
            await turn
        assert terminal.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
    finally:
        await session.close()
        with suppress(CodexAgentError):
            await turn


@pytest.mark.parametrize(
    "error",
    [
        CodexRpcTimeoutError("turn/steer", 0.1),
        CodexRpcProtocolError("STEER_PROTOCOL_SECRET"),
        RuntimeError("STEER_CONNECTION_SECRET"),
    ],
)
async def test_steer_transport_uncertainty_terminalizes_unknown(error: BaseException) -> None:
    connection = FakeSharedConnection()
    connection.steer_error = error
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    with pytest.raises(CodexAgentError) as caught:
        await session.steer("追加指示")

    code_type = AgentTurnErrorCode
    assert caught.value.code is code_type.OUTCOME_UNKNOWN
    assert not session.reusable
    with pytest.raises(CodexAgentError) as terminal:
        await turn
    assert terminal.value.code is code_type.OUTCOME_UNKNOWN
    await session.close()


@pytest.mark.parametrize(
    "response",
    [None, {}, {"turnId": 1}, {"turnId": "different-turn"}, {"turnId": ""}],
)
async def test_steer_malformed_or_mismatched_response_is_unknown(response: JsonValue) -> None:
    connection = FakeSharedConnection()
    connection.steer_result = response
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    with pytest.raises(CodexAgentError) as caught:
        await session.steer("追加指示")

    code_type = AgentTurnErrorCode
    assert caught.value.code is code_type.OUTCOME_UNKNOWN
    assert not session.reusable
    with pytest.raises(CodexAgentError) as terminal:
        await turn
    assert terminal.value.code is code_type.OUTCOME_UNKNOWN
    await session.close()


async def test_steer_caller_cancellation_recovers_the_claim_as_unknown() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer = asyncio.create_task(session.steer("追加指示"))
    await connection.steer_requested.wait()

    steer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await steer

    code_type = AgentTurnErrorCode
    assert not session.reusable
    with pytest.raises(CodexAgentError) as terminal:
        await turn
    assert terminal.value.code is code_type.OUTCOME_UNKNOWN
    assert getattr(session, "_steer_task", None) is None
    connection.steer_gate.set()
    await session.close()


async def test_steer_noncooperative_cancellation_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_CLEANUP_TIMEOUT_SECONDS", 0.01)
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    connection.steer_ignore_cancellation = True
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer = asyncio.create_task(session.steer("追加指示"))
    await connection.steer_requested.wait()

    steer.cancel()
    await connection.steer_cancelled.wait()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(steer), 0.1)
        assert getattr(session, "_steer_task", None) is None
        assert not session.reusable
        with pytest.raises(CodexAgentError) as terminal:
            await turn
        assert terminal.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
    finally:
        connection.steer_gate.set()
        await connection.steer_completed.wait()
        await asyncio.gather(steer, return_exceptions=True)
        await session.close()
        with suppress(CodexAgentError):
            await turn


async def test_steer_noncooperative_repeated_cancellation_finishes_unknown_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_CLEANUP_TIMEOUT_SECONDS", 0.05)
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    connection.steer_ignore_cancellation = True
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer = asyncio.create_task(session.steer("追加指示"))
    await connection.steer_requested.wait()

    assert steer.cancel("first-cancel")
    await connection.steer_cancelled.wait()
    assert not steer.done()
    assert steer.cancel("second-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await steer

    try:
        assert getattr(session, "_steer_task", None) is None
        assert not session.reusable
        with pytest.raises(CodexAgentError) as terminal:
            await turn
        assert terminal.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
        assert caught.value.args == ("first-cancel",)
    finally:
        connection.steer_gate.set()
        await connection.steer_completed.wait()
        await session.close()
        with suppress(CodexAgentError):
            await turn


async def test_steer_close_cancels_and_recovers_the_inflight_claim() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer = asyncio.create_task(session.steer("追加指示"))
    await connection.steer_requested.wait()

    await session.close()

    code_type = AgentTurnErrorCode
    with pytest.raises(asyncio.CancelledError):
        await steer
    with pytest.raises(CodexAgentError) as terminal:
        await turn
    assert terminal.value.code is code_type.OUTCOME_UNKNOWN
    assert getattr(session, "_steer_task", None) is None
    assert not session.reusable
    connection.steer_gate.set()


async def test_steer_noncooperative_close_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "_CLEANUP_TIMEOUT_SECONDS", 0.01)
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    connection.steer_ignore_cancellation = True
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    steer = asyncio.create_task(session.steer("追加指示"))
    await connection.steer_requested.wait()
    close = asyncio.create_task(session.close())
    await connection.steer_cancelled.wait()

    try:
        await asyncio.wait_for(asyncio.shield(close), 0.1)
        assert getattr(session, "_steer_task", None) is None
        assert not session.reusable
        with pytest.raises(CodexAgentError) as terminal:
            await turn
        assert terminal.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN

        connection.steer_gate.set()
        await connection.steer_completed.wait()
        with pytest.raises(CodexAgentError) as late:
            await steer
        assert late.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
    finally:
        connection.steer_gate.set()
        await asyncio.gather(steer, close, return_exceptions=True)
        with suppress(CodexAgentError):
            await turn


async def test_steer_allows_only_one_inflight_claim() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    first = asyncio.create_task(session.steer("first steer"))
    await connection.steer_requested.wait()

    with pytest.raises(CodexAgentError, match="agent steer is already active") as caught:
        await session.steer("second steer")
    assert caught.value.code is None

    connection.steer_gate.set()
    await first
    assert sum(call[0] == WIRE_METHODS[SemanticMethod.TURN_STEER] for call in connection.calls) == 1
    await session.close()
    with suppress(CodexAgentError):
        await turn


async def test_reusable_is_true_only_while_open_and_not_unknown_terminal() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    try:
        assert session.reusable

        turn = await start_active_turn(connection, session)
        await connection.emit(
            TURN_COMPLETED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turn": {"id": "agent-turn-1", "items": [], "status": "failed"},
            },
        )
        with pytest.raises(CodexAgentError):
            await turn
        assert session.reusable

        connection.turn_requested.clear()
        turn = await start_active_turn(connection, session, "second")
        await connection.lose_connection()
        with pytest.raises(CodexAgentError):
            await turn
        assert not session.reusable
    finally:
        await session.close()


async def test_reusable_is_false_after_close() -> None:
    session = make_session(FakeSharedConnection())

    await session.close()

    assert not session.reusable


async def test_idle_notification_terminal_is_reported_exactly_once() -> None:
    connection = FakeSharedConnection()
    terminal_calls = 0

    def terminal_sink() -> None:
        nonlocal terminal_calls
        terminal_calls += 1

    session = make_session(connection, terminal_sink=terminal_sink)
    assert await finish_turn(connection, session, "first") == "final answer"

    await connection.lose_connection()
    for _index in range(20):
        if terminal_calls:
            break
        await asyncio.sleep(0)

    assert terminal_calls == 1
    assert not session.reusable
    await session.close()


async def test_active_turn_ownership_tracks_the_exact_agent_turn() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    assert session.owns_active_turn("agent-thread-1", "agent-turn-1")
    assert not session.owns_active_turn("other-thread", "agent-turn-1")
    assert not session.owns_active_turn("agent-thread-1", "other-turn")

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "final answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await turn == "final answer"
    assert not session.owns_active_turn("agent-thread-1", "agent-turn-1")
    await session.close()


async def test_reusable_remains_true_after_successful_cancellation_settlement() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert session.reusable
    assert session.active_turn_id is None
    await session.close()


def test_turn_outcome_code_is_an_exact_payload_free_three_value_enum() -> None:
    code_type = AgentTurnErrorCode

    assert [code.value for code in code_type] == [
        "agent_turn_failed",
        "agent_turn_interrupted",
        "agent_outcome_unknown",
    ]


@pytest.mark.parametrize(
    ("status", "expected_name"),
    [("failed", "FAILED"), ("interrupted", "INTERRUPTED")],
)
async def test_turn_outcome_code_types_known_server_terminal_status(
    status: str,
    expected_name: str,
) -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)

    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": status},
        },
    )

    with pytest.raises(CodexAgentError) as caught:
        await turn
    code_type = AgentTurnErrorCode
    assert caught.value.code is getattr(code_type, expected_name)
    assert session.reusable
    await session.close()


async def test_turn_outcome_code_maps_missing_final_to_failed() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )

    with pytest.raises(CodexAgentError) as caught:
        await turn
    code_type = AgentTurnErrorCode
    assert caught.value.code is code_type.FAILED
    assert session.reusable
    await session.close()


async def test_turn_outcome_code_maps_connection_loss_to_unknown_without_payload() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    turn = await start_active_turn(connection, session)
    await connection.lose_connection(RuntimeError("PRIVATE_CONNECTION_PAYLOAD"))

    with pytest.raises(CodexAgentError) as caught:
        await turn
    code_type = AgentTurnErrorCode
    assert caught.value.code is code_type.OUTCOME_UNKNOWN
    assert vars(caught.value) == {"code": code_type.OUTCOME_UNKNOWN}
    assert "PRIVATE_CONNECTION_PAYLOAD" not in repr(caught.value)
    assert not session.reusable
    await session.close()


@pytest.mark.parametrize(
    ("profile", "sandbox", "approval"),
    [
        (AgentProfileMode.READ_ONLY, "read-only", "never"),
        (AgentProfileMode.WORKSPACE_WRITE, "workspace-write", "on-request"),
    ],
)
async def test_thread_start_uses_explicit_profile_policy(
    profile: AgentProfileMode,
    sandbox: str,
    approval: str,
) -> None:
    connection = FakeSharedConnection()
    session = make_session(connection, profile=profile)

    assert await finish_turn(connection, session, "hello") == "final answer"

    thread_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.THREAD_START]
    ]
    assert len(thread_calls) == 1
    params = cast("dict[str, JsonValue]", thread_calls[0][1])
    assert params == {
        "cwd": str(WORKING_DIRECTORY),
        "ephemeral": True,
        "sandbox": sandbox,
        "approvalPolicy": approval,
    }
    await session.close()


async def test_inherit_codex_omits_policy_fields() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection, profile=AgentProfileMode.INHERIT_CODEX)

    await finish_turn(connection, session, "hello")

    thread_call = next(
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.THREAD_START]
    )
    params = cast("dict[str, JsonValue]", thread_call[1])
    assert params == {"cwd": str(WORKING_DIRECTORY), "ephemeral": True}
    await session.close()


@pytest.mark.parametrize(
    ("effective_policy", "unknown_policy"),
    [
        (EffectivePolicy(SandboxMode.DANGER_FULL_ACCESS, ApprovalMode.NEVER), False),
        (None, True),
    ],
)
async def test_inherit_rechecks_profile_policy_at_wire_boundary(
    effective_policy: EffectivePolicy | None,
    unknown_policy: bool,
) -> None:
    connection = FakeSharedConnection()
    session = make_session(
        connection,
        profile=AgentProfileMode.INHERIT_CODEX,
        snapshot=capabilities(
            effective_policy=effective_policy,
            unknown_policy=unknown_policy,
        ),
    )

    with pytest.raises(CodexAgentError, match="agent admission is unavailable"):
        await session.start_turn("hello")
    assert connection.calls == []
    await session.close()


async def test_unavailable_admission_makes_no_wire_call() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection, snapshot=capabilities(CapabilityStatus.DISABLED))

    with pytest.raises(CodexAgentError) as caught:
        await session.start_turn("hello")

    assert str(caught.value) == "agent admission is unavailable"
    assert connection.calls == []
    assert "private capability detail" not in repr(caught.value)
    await session.close()


@pytest.mark.parametrize("text", ["", "   \n\t", "bad\ud800"])
async def test_invalid_input_makes_no_wire_call(text: str) -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)

    with pytest.raises(CodexAgentError, match="agent input is invalid"):
        await session.start_turn(text)

    assert connection.calls == []
    await session.close()


async def test_unbounded_input_makes_no_wire_call() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)

    with pytest.raises(CodexAgentError, match="agent input is invalid"):
        await session.start_turn("x" * (64 * 1024 + 1))

    assert connection.calls == []
    await session.close()


async def test_turn_preserves_exact_text_and_uses_effective_aliases() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    prompt = "  exact user text — do not reinterpret  "

    task = asyncio.create_task(session.start_turn(prompt))
    await connection.turn_requested.wait()
    turn_call = next(
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_START]
    )
    params = cast("dict[str, JsonValue]", turn_call[1])
    assert params["threadId"] == "agent-thread-1"
    assert params["input"] == [{"type": "text", "text": prompt}]
    assert turn_call[2] == {}

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    assert [call[0] for call in connection.calls] == [
        WIRE_METHODS[SemanticMethod.THREAD_START],
        WIRE_METHODS[SemanticMethod.TURN_START],
    ]
    await session.close()


async def test_thread_is_ephemeral_and_continues_across_sequential_turns() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)

    assert await finish_turn(connection, session, "first") == "final answer"
    connection.turn_requested.clear()
    assert await finish_turn(connection, session, "second") == "final answer"

    assert (
        sum(call[0] == WIRE_METHODS[SemanticMethod.THREAD_START] for call in connection.calls) == 1
    )
    turn_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_START]
    ]
    assert len(turn_calls) == 2
    assert all(
        cast("dict[str, JsonValue]", call[1])["threadId"] == "agent-thread-1" for call in turn_calls
    )
    await session.close()


async def test_duplicate_turn_is_rejected_while_first_turn_is_active() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    first = asyncio.create_task(session.start_turn("first"))
    await connection.turn_requested.wait()

    with pytest.raises(CodexAgentError, match="agent turn is already active"):
        await session.start_turn("second")

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "first answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await first == "first answer"
    await session.close()


async def test_foreign_threads_and_turns_are_ignored() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "voice-thread",
            "turnId": "agent-turn-1",
            "item": {"id": "foreign-item", "type": "agentMessage", "text": "foreign"},
        },
    )
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "older-agent-turn",
            "item": {"id": "old-item", "type": "agentMessage", "text": "old"},
        },
    )
    assert not task.done()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    await session.close()


async def test_only_completed_agent_message_is_final() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        AGENT_MESSAGE_DELTA_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "itemId": "agent-item-1",
            "delta": "partial secret",
        },
    )
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "command-1", "type": "commandExecution", "command": "secret"},
        },
    )
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "the only answer",
            },
        },
    )
    assert not task.done()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "the real answer",
            },
        },
    )
    assert not task.done()
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "the real answer"
    await session.close()


async def test_commentary_then_failed_turn_never_succeeds() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "progress only",
            },
        },
    )
    assert not task.done()
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "failed"},
        },
    )
    with pytest.raises(CodexAgentError, match="turn failed"):
        await task
    await session.close()


async def test_phase_absent_candidate_waits_for_correlated_completion() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "text": "legacy final",
            },
        },
    )
    assert not task.done()
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "legacy final"
    await session.close()


async def test_schema_unknown_progress_item_is_ignored() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "sleep-1", "type": "sleep"},
        },
    )
    assert not task.done()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    await session.close()


async def test_agent_progress_projects_only_correlated_bounded_categories() -> None:
    connection = FakeSharedConnection()
    events: list[AgentActivityEvent] = []
    session = make_session(connection, activity_sink=events.append)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await wait_for_active_agent_turn(session)

    private_payload: dict[str, JsonValue] = {
        "command": "cat /private/agent-secret",
        "path": "/private/worktree/secret.py",
        "patch": "PRIVATE_PATCH_BODY",
        "reasoning": "PRIVATE_REASONING_BODY",
        "arguments": {"token": "PRIVATE_MCP_ARGUMENT"},
    }
    for thread_id, turn_id in (
        ("voice-thread", "agent-turn-1"),
        ("agent-thread-1", "other-turn"),
    ):
        item: dict[str, JsonValue] = {
            "id": "private-wrong-item",
            "type": "commandExecution",
        }
        item.update(private_payload)
        await connection.emit(
            ITEM_STARTED_NOTIFICATION,
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": item,
            },
        )

    item_categories: tuple[tuple[str, AgentActivityKind], ...] = (
        ("reasoning", "reasoning"),
        ("commandExecution", "command_execution"),
        ("fileChange", "file_change"),
        ("mcpToolCall", "external_tool"),
        ("dynamicToolCall", "external_tool"),
        ("collabAgentToolCall", "subagent"),
        ("subAgentActivity", "subagent"),
        ("webSearch", "web_search"),
        ("imageView", "image_view"),
        ("imageGeneration", "image_generation"),
        ("contextCompaction", "context_compaction"),
        ("futurePrivateWork", "codex_work"),
    )
    for index, (item_type, _kind) in enumerate(item_categories):
        item = {"id": f"private-item-{index}", "type": item_type}
        item.update(private_payload)
        await connection.emit(
            ITEM_STARTED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turnId": "agent-turn-1",
                "item": item,
            },
        )
    item = {"id": "private-command-complete", "type": "commandExecution"}
    item.update(private_payload)
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": item,
        },
    )
    await asyncio.sleep(0)

    assert events == [
        *[AgentActivityEvent(kind, "started") for _item_type, kind in item_categories],
        AgentActivityEvent("command_execution", "completed"),
    ]
    rendered = repr(events)
    assert "private" not in rendered.lower()
    assert "secret" not in rendered.lower()
    assert "reasoning_body" not in rendered.lower()
    assert "mcp_argument" not in rendered.lower()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    await session.close()


async def test_agent_message_start_is_not_progress_and_final_still_settles() -> None:
    connection = FakeSharedConnection()
    events: list[AgentActivityEvent] = []
    session = make_session(connection, activity_sink=events.append)
    task = await start_active_turn(connection, session)

    await connection.emit(
        ITEM_STARTED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-final",
                "type": "agentMessage",
                "text": "private unfinished answer",
            },
        },
    )
    await asyncio.sleep(0)
    assert events == []

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )

    assert await task == "answer"
    assert events == []
    await session.close()


async def test_item_started_before_turn_response_replays_after_exact_correlation() -> None:
    connection = FakeSharedConnection()
    connection.turn_start_gate = asyncio.Event()
    events: list[AgentActivityEvent] = []
    session = make_session(connection, activity_sink=events.append)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    for turn_id in ("other-turn", "agent-turn-1"):
        await connection.emit(
            ITEM_STARTED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turnId": turn_id,
                "item": {
                    "id": "private-command-item",
                    "type": "commandExecution",
                    "command": "cat /private/secret",
                },
            },
        )
    await asyncio.sleep(0)
    assert events == []

    connection.turn_start_gate.set()
    await wait_for_active_agent_turn(session)
    await asyncio.sleep(0)
    assert events == [AgentActivityEvent("command_execution", "started")]

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    await session.close()


async def test_contract_without_item_started_evidence_uses_completed_progress_only() -> None:
    base_contract = effective_contract()
    profile = base_contract.agent_event_profile
    assert profile is not None
    contract = replace(
        base_contract,
        agent_event_profile=replace(
            profile,
            item_started_method=None,
            item_started_required_fields=frozenset(),
            item_started_field_types={},
        ),
    )
    connection = FakeSharedConnection()
    events: list[AgentActivityEvent] = []
    session = make_session(connection, contract=contract, activity_sink=events.append)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await wait_for_active_agent_turn(session)

    progress: dict[str, JsonValue] = {
        "threadId": "agent-thread-1",
        "turnId": "agent-turn-1",
        "item": {"id": "command-1", "type": "commandExecution"},
    }
    await connection.emit(ITEM_STARTED_NOTIFICATION, progress)
    await connection.emit(ITEM_COMPLETED_NOTIFICATION, progress)
    await asyncio.sleep(0)
    assert events == [AgentActivityEvent("command_execution", "completed")]

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    await session.close()


async def test_malformed_correlated_agent_progress_fails_turn_safely() -> None:
    connection = FakeSharedConnection()
    events: list[AgentActivityEvent] = []
    session = make_session(connection, activity_sink=events.append)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await wait_for_active_agent_turn(session)

    await connection.emit(
        ITEM_STARTED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "private-malformed-item"},
        },
    )

    with pytest.raises(CodexAgentError) as caught:
        await asyncio.wait_for(task, 0.2)
    assert caught.value.code is AgentTurnErrorCode.OUTCOME_UNKNOWN
    assert events == []
    await session.close()


async def test_agent_progress_sink_failure_does_not_terminalize_turn() -> None:
    class ActivitySinkError(RuntimeError):
        """Synthetic UI effect failure."""

    def fail_activity(_event: AgentActivityEvent) -> None:
        raise ActivitySinkError

    connection = FakeSharedConnection()
    session = make_session(connection, activity_sink=fail_activity)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await wait_for_active_agent_turn(session)
    await connection.emit(
        ITEM_STARTED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "command-1", "type": "commandExecution"},
        },
    )
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )

    assert await task == "answer"
    await session.close()


async def test_turn_start_interleaving_replays_final_after_response() -> None:
    connection = FakeSharedConnection()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "buffered answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert not task.done()

    connection.turn_start_gate.set()
    assert await asyncio.wait_for(task, 0.2) == "buffered answer"
    await session.close()


async def test_foreign_notifications_before_thread_response_are_ignored() -> None:
    connection = FakeSharedConnection()
    connection.thread_start_gate = asyncio.Event()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.thread_requested.wait()

    for index in range(40):
        await connection.emit(
            ITEM_COMPLETED_NOTIFICATION,
            {
                "threadId": "foreign-thread",
                "turnId": f"foreign-turn-{index}",
                "item": {
                    "id": f"foreign-item-{index}",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "foreign answer",
                },
            },
        )
        await asyncio.sleep(0)
    assert not task.done()

    try:
        connection.thread_start_gate.set()
        await asyncio.wait_for(connection.turn_requested.wait(), 0.2)
        await connection.emit(
            ITEM_COMPLETED_NOTIFICATION,
            {
                "threadId": "foreign-thread",
                "turnId": "agent-turn-1",
                "item": {
                    "id": "foreign-after-thread",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "foreign after thread",
                },
            },
        )
        await connection.emit(
            ITEM_COMPLETED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turnId": "agent-turn-1",
                "item": {
                    "id": "agent-item-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "same-thread answer",
                },
            },
        )
        await connection.emit(
            TURN_COMPLETED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
            },
        )
        await asyncio.sleep(0)
        assert not task.done()
        connection.turn_start_gate.set()
        assert await asyncio.wait_for(task, 0.2) == "same-thread answer"
    finally:
        connection.thread_start_gate.set()
        connection.turn_start_gate.set()
        if not task.done():
            task.cancel()
        with suppress(CodexAgentError, asyncio.CancelledError):
            await task
        await session.close()


async def test_actual_rpc_peer_interleaving_replays_final_after_response() -> None:
    connection = RpcPeerSharedConnection()
    await connection.start()
    session = make_session(cast("FakeSharedConnection", connection))
    task = asyncio.create_task(session.start_turn("hello"))

    thread_request = await connection.next_written()
    await connection.feed(
        {
            "id": thread_request["id"],
            "result": {"thread": {"id": "agent-thread-1"}},
        }
    )
    turn_request = await connection.next_written()
    await connection.feed(
        {
            "method": ITEM_COMPLETED_NOTIFICATION,
            "params": {
                "threadId": "agent-thread-1",
                "turnId": "agent-turn-1",
                "item": {
                    "id": "agent-item-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "wire-buffered answer",
                },
            },
        }
    )
    await connection.feed(
        {
            "method": TURN_COMPLETED_NOTIFICATION,
            "params": {
                "threadId": "agent-thread-1",
                "turn": {
                    "id": "agent-turn-1",
                    "items": [],
                    "status": "completed",
                },
            },
        }
    )
    assert not task.done()
    await connection.feed(
        {
            "id": turn_request["id"],
            "result": {"turn": {"id": "agent-turn-1"}},
        }
    )
    assert await asyncio.wait_for(task, 0.2) == "wire-buffered answer"
    await session.close()
    await connection.close()


async def test_active_cancellation_interrupts_once_and_preserves_cancelled_error() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupt_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    ]
    assert len(interrupt_calls) == 1
    assert session.closed is False
    await session.close()
    assert (
        len(
            [
                call
                for call in connection.calls
                if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
            ]
        )
        == 1
    )


async def test_close_interrupts_known_active_turn_once_without_closing_connection() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await session.close()
    with pytest.raises(CodexAgentError, match="session is closed"):
        await task
    assert (
        len(
            [
                call
                for call in connection.calls
                if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
            ]
        )
        == 1
    )
    assert connection.close_called is False


async def test_cancelled_close_continues_cleanup_for_a_later_close() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    turn = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    close_task = asyncio.create_task(session.close())
    await connection.interrupt_requested.wait()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    connection.interrupt_gate.set()
    await connection.interrupt_completed.wait()
    try:
        await session.close()
        assert getattr(session, "_pump_task", None) is None
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert len(interrupt_calls) == 1
        with pytest.raises(CodexAgentError, match="session is closed"):
            await turn
    finally:
        with suppress(CodexAgentError, asyncio.CancelledError):
            await turn
        pump = getattr(session, "_pump_task", None)
        if pump is not None:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump


async def test_close_bounds_noncooperative_notification_pump() -> None:
    connection = FakeSharedConnection()
    connection.ignore_pump_cancellation = True
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    turn = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await connection.pump_waiting.wait()

    close_task = asyncio.create_task(session.close())
    await connection.interrupt_requested.wait()
    connection.interrupt_gate.set()
    await connection.interrupt_completed.wait()
    await connection.pump_cancelled.wait()
    try:
        await asyncio.wait_for(session.close(), 1.0)
        pump = getattr(session, "_pump_task", None)
        assert pump is not None
        assert not pump.done()
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert len(interrupt_calls) == 1
        with pytest.raises(CodexAgentError, match="session is closed"):
            await turn
    finally:
        connection.release_pump.set()
        await connection.pump_finished.wait()
        with suppress(asyncio.CancelledError, CodexAgentError):
            await asyncio.wait_for(asyncio.shield(close_task), 1.0)
        assert getattr(session, "_pump_task", None) is None
        with suppress(asyncio.CancelledError, CodexAgentError):
            await turn


async def test_interrupt_failure_terminalizes_unknown_outcome_and_rejects_reuse() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_error = RuntimeError("wire secret")
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with pytest.raises(CodexAgentError, match="outcome is unknown"):
        await session.interrupt()
    with pytest.raises(CodexAgentError, match="outcome is unknown"):
        await task
    with pytest.raises(CodexAgentError, match="outcome is unknown"):
        await session.start_turn("second")
    assert "wire secret" not in repr(session)
    await session.close()


async def test_cancelled_turn_start_without_response_terminalizes_unknown_outcome() -> None:
    connection = FakeSharedConnection()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(CodexAgentError, match="outcome is unknown"):
        await session.start_turn("second")
    assert [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    ] == []
    await session.close()


async def test_cancelled_pending_starts_interrupt_each_returned_turn_once() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    connection.turn_start_gate = asyncio.Event()

    first = asyncio.create_task(session.start_turn("first"))
    await connection.turn_requested.wait()
    first.cancel()
    await asyncio.sleep(0)
    connection.turn_start_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await first

    connection.turn_requested.clear()
    connection.turn_start_gate = asyncio.Event()
    second = asyncio.create_task(session.start_turn("second"))
    await connection.turn_requested.wait()
    second.cancel()
    await asyncio.sleep(0)
    connection.turn_start_gate.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await second
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert [call[1] for call in interrupt_calls] == [
            {"threadId": "agent-thread-1", "turnId": "agent-turn-1"},
            {"threadId": "agent-thread-1", "turnId": "agent-turn-2"},
        ]
        assert session.active_turn_id is None
    finally:
        await session.close()


async def test_close_with_turn_start_response_pending_is_bounded_and_terminal() -> None:
    connection = FakeSharedConnection()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await asyncio.wait_for(session.close(), 1.0)
    with pytest.raises(CodexAgentError, match="session is closed"):
        await task
    assert [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    ] == []
    assert connection.close_called is False


async def test_close_cancels_pending_initial_thread_start_request() -> None:
    connection = FakeSharedConnection()
    connection.thread_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.thread_requested.wait()

    try:
        await session.close()
        with pytest.raises(CodexAgentError, match="session is closed"):
            await asyncio.wait_for(asyncio.shield(task), 1.0)
        await connection.thread_start_cancelled.wait()
        assert [
            call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_START]
        ] == []
        assert getattr(session, "_thread_start_task", None) is None
        assert connection.close_called is False
    finally:
        connection.thread_start_gate.set()
        with suppress(asyncio.CancelledError, CodexAgentError):
            await task
        await session.close()


async def test_cancelled_initial_thread_start_cancels_owned_request() -> None:
    connection = FakeSharedConnection()
    connection.thread_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.thread_requested.wait()

    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(connection.thread_start_cancelled.wait(), 1.0)
        await asyncio.sleep(0)
        assert getattr(session, "_thread_start_task", None) is None
    finally:
        connection.thread_start_gate.set()
        with suppress(asyncio.CancelledError, CodexAgentError):
            await task
        await session.close()


async def test_turn_start_notification_buffer_overflow_fails_closed() -> None:
    connection = FakeSharedConnection()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    for index in range(40):
        await connection.emit(
            ITEM_COMPLETED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turnId": "agent-turn-1",
                "item": {"id": f"sleep-{index}", "type": "sleep"},
            },
        )
        await asyncio.sleep(0)
    connection.turn_start_gate.set()

    with pytest.raises(CodexAgentError, match="buffer overflow"):
        await task
    with pytest.raises(CodexAgentError, match="buffer overflow"):
        await session.start_turn("second")
    await session.close()


async def test_buffer_overflow_before_turn_start_response_interrupts_returned_turn() -> None:
    connection = FakeSharedConnection()
    connection.turn_start_gate = asyncio.Event()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    for index in range(40):
        await connection.emit(
            ITEM_COMPLETED_NOTIFICATION,
            {
                "threadId": "agent-thread-1",
                "turnId": "agent-turn-1",
                "item": {"id": f"sleep-{index}", "type": "sleep"},
            },
        )
        await asyncio.sleep(0)
    connection.turn_start_gate.set()

    try:
        await asyncio.wait_for(connection.interrupt_requested.wait(), 1.0)
        with pytest.raises(CodexAgentError, match="buffer overflow"):
            await task
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert [call[1] for call in interrupt_calls] == [
            {"threadId": "agent-thread-1", "turnId": "agent-turn-1"}
        ]
        assert getattr(session, "_turn_start_task", None) is None
        assert getattr(session, "_turn_start_sent", True) is False
        assert getattr(session, "_turn_starting", True) is False
        assert getattr(session, "_turn_start_buffer", []) == []
        assert session.active_turn_id is None
        with pytest.raises(CodexAgentError, match="buffer overflow"):
            await session.start_turn("second")
        assert (
            len(
                [
                    call
                    for call in connection.calls
                    if call[0] == WIRE_METHODS[SemanticMethod.TURN_START]
                ]
            )
            == 1
        )
    finally:
        connection.interrupt_gate.set()
        with suppress(asyncio.CancelledError, CodexAgentError):
            await task
        await session.close()


async def test_supported_nested_turn_notifications_are_correlated() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(THREAD_NOTIFICATION, {"thread": {"id": "agent-thread-1"}})
    await connection.emit(
        TURN_NOTIFICATION,
        {"threadId": "agent-thread-1", "turn": {"id": "agent-turn-1"}},
    )
    assert not task.done()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await task == "answer"
    await session.close()


async def test_completed_turn_without_agent_message_fails_closed() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    with pytest.raises(CodexAgentError, match="final answer is unavailable"):
        await task
    await session.close()


async def test_malformed_contract_is_rejected_before_wire_call() -> None:
    connection = FakeSharedConnection()
    contract = effective_contract(
        overrides={
            SemanticMethod.TURN_START: ClientMethodContract(
                WIRE_METHODS[SemanticMethod.TURN_START],
                ParamsKind.OMITTED,
                REQUIRED_FIELDS[SemanticMethod.TURN_START],
            )
        }
    )
    session = make_session(connection, contract=contract)

    with pytest.raises(CodexAgentError, match="protocol contract"):
        await session.start_turn("hello")

    assert connection.calls == []
    await session.close()


async def test_malformed_result_and_agent_message_fail_closed() -> None:
    connection = FakeSharedConnection()
    connection.thread_result = {"unexpected": "result"}
    session = make_session(connection)

    with pytest.raises(CodexAgentError, match="thread result"):
        await session.start_turn("hello")
    await session.close()

    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "malformed-agent", "type": "agentMessage"},
        },
    )
    with pytest.raises(CodexAgentError, match="completion is invalid"):
        await task
    await session.close()


async def test_malformed_active_completion_interrupts_and_terminalizes_session() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "malformed-agent", "type": "agentMessage"},
        },
    )
    try:
        await asyncio.wait_for(connection.interrupt_requested.wait(), 1.0)
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert len(interrupt_calls) == 1
        connection.interrupt_gate.set()
        await connection.interrupt_completed.wait()
        with pytest.raises(CodexAgentError, match="completion is invalid"):
            await task
        with pytest.raises(CodexAgentError, match="outcome is unknown"):
            await session.start_turn("second")
    finally:
        connection.interrupt_gate.set()
        with suppress(asyncio.CancelledError, CodexAgentError):
            await task
        await session.close()


async def test_buffered_malformed_completion_interrupts_and_terminalizes_session() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "buffered-malformed", "type": "agentMessage"},
        },
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(getattr(session, "_turn_start_buffer", ())) == 1
    connection.turn_start_gate.set()
    try:
        await asyncio.wait_for(connection.interrupt_requested.wait(), 1.0)
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert len(interrupt_calls) == 1
        connection.interrupt_gate.set()
        await connection.interrupt_completed.wait()
        with pytest.raises(CodexAgentError, match="completion is invalid"):
            await task
        with pytest.raises(CodexAgentError, match="outcome is unknown"):
            await session.start_turn("second")
    finally:
        connection.turn_start_gate.set()
        connection.interrupt_gate.set()
        with suppress(asyncio.CancelledError, CodexAgentError):
            await task
        await session.close()


async def test_interrupt_targets_only_the_captured_active_turn() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    first = asyncio.create_task(session.start_turn("first"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await session.interrupt()
    with pytest.raises(CodexAgentError, match="interrupted"):
        await first
    interrupt_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    ]
    assert len(interrupt_calls) == 1
    assert interrupt_calls[0][1] == {
        "threadId": "agent-thread-1",
        "turnId": "agent-turn-1",
    }

    with pytest.raises(CodexAgentError, match="no Agent turn is active"):
        await session.interrupt()

    connection.turn_requested.clear()
    second = asyncio.create_task(session.start_turn("second"))
    await connection.turn_requested.wait()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {"id": "late", "type": "agentMessage", "text": "late"},
        },
    )
    assert not second.done()
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-2",
            "item": {
                "id": "agent-item-2",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "second answer",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-2", "items": [], "status": "completed"},
        },
    )
    assert await second == "second answer"
    await session.close()


async def test_interrupt_claims_are_scoped_to_sequential_turns() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    first = asyncio.create_task(session.start_turn("first"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await session.interrupt()
    with pytest.raises(CodexAgentError, match="interrupted"):
        await first

    connection.turn_requested.clear()
    second = asyncio.create_task(session.start_turn("second"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        await session.interrupt()
        interrupt_calls = [
            call
            for call in connection.calls
            if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
        ]
        assert [call[1] for call in interrupt_calls] == [
            {"threadId": "agent-thread-1", "turnId": "agent-turn-1"},
            {"threadId": "agent-thread-1", "turnId": "agent-turn-2"},
        ]
        with pytest.raises(CodexAgentError, match="interrupted"):
            await asyncio.wait_for(second, 0.2)
    finally:
        await session.close()
        if not second.done():
            second.cancel()
        with suppress(CodexAgentError, asyncio.CancelledError):
            await second


async def test_later_caller_cancellation_claims_its_own_interrupt() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    first = asyncio.create_task(session.start_turn("first"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await session.interrupt()
    with pytest.raises(CodexAgentError, match="interrupted"):
        await first

    connection.turn_requested.clear()
    second = asyncio.create_task(session.start_turn("second"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    interrupt_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    ]
    assert [call[1] for call in interrupt_calls] == [
        {"threadId": "agent-thread-1", "turnId": "agent-turn-1"},
        {"threadId": "agent-thread-1", "turnId": "agent-turn-2"},
    ]
    await session.close()


async def test_concurrent_interrupts_share_one_turn_scoped_claim() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    turn = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    first = asyncio.create_task(session.interrupt())
    second = asyncio.create_task(session.interrupt())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    interrupt_calls = [
        call for call in connection.calls if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    ]
    assert interrupt_calls == [
        (
            WIRE_METHODS[SemanticMethod.TURN_INTERRUPT],
            {"threadId": "agent-thread-1", "turnId": "agent-turn-1"},
            {"request_timeout": 0.25},
        )
    ]

    connection.interrupt_gate.set()
    await asyncio.gather(first, second)
    with pytest.raises(CodexAgentError, match="interrupted"):
        await turn
    await session.close()


async def test_cancelled_interrupt_caller_does_not_own_settlement() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    turn = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    caller = asyncio.create_task(session.interrupt())
    await connection.interrupt_requested.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    connection.interrupt_gate.set()
    await connection.interrupt_completed.wait()
    try:
        assert session.active_turn_id is None
        with pytest.raises(CodexAgentError, match="no Agent turn is active"):
            await session.interrupt()
    finally:
        await session.close()
        with suppress(CodexAgentError, asyncio.CancelledError):
            await turn


async def test_repeated_start_cancellation_does_not_skip_interrupt_settlement() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    turn = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    turn.cancel()
    await connection.interrupt_requested.wait()
    assert not turn.done()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    connection.interrupt_gate.set()
    await connection.interrupt_completed.wait()
    assert session.active_turn_id is None
    await session.close()


async def test_inflight_old_interrupt_blocks_a_later_turn_after_terminal_notification() -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    session = make_session(connection)
    first = asyncio.create_task(session.start_turn("first"))
    await connection.turn_requested.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    cleanup = asyncio.create_task(session.interrupt())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": "agent-turn-1",
            "item": {
                "id": "agent-item-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "completed before interrupt response",
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {"id": "agent-turn-1", "items": [], "status": "completed"},
        },
    )
    assert await first == "completed before interrupt response"

    connection.turn_requested.clear()
    with pytest.raises(CodexAgentError, match="outcome is unknown"):
        await session.start_turn("second")
    assert not connection.turn_requested.is_set()

    connection.interrupt_gate.set()
    await cleanup
    await session.close()


async def test_connection_loss_and_cancellation_never_succeed_silently() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    await connection.lose_connection(RuntimeError("request-id-secret"))
    with pytest.raises(CodexAgentError, match="outcome is unknown") as lost:
        await task
    assert "request-id-secret" not in str(lost.value)
    with pytest.raises(CodexAgentError, match="outcome is unknown"):
        await session.start_turn("second")
    await session.close()

    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.closed is False
    assert (
        len(
            [
                call
                for call in connection.calls
                if call[0] == WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
            ]
        )
        == 1
    )
    await session.close()


async def test_close_is_idempotent_bounded_and_does_not_close_shared_connection() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection)
    task = asyncio.create_task(session.start_turn("hello"))
    await connection.turn_requested.wait()

    await asyncio.gather(session.close(), session.close(), session.close())
    with pytest.raises(CodexAgentError, match="session is closed"):
        await task
    assert connection.close_called is False
    assert repr(session) == "AgentSession(closed=True, thread_active=True, turn_active=False)"


async def test_privacy_safe_repr_and_public_errors() -> None:
    connection = FakeSharedConnection()
    session = make_session(connection, working_directory=Path.cwd() / "prompt-secret")
    assert "secret" not in repr(session)
    assert "agent-thread-1" not in repr(session)
    assert "agent-turn-1" not in repr(session)

    await session.close()

    connection = FakeSharedConnection()
    connection.request_error = RuntimeError("prompt secret request-id-secret")
    session = make_session(connection)
    with pytest.raises(CodexAgentError) as caught:
        await session.start_turn("prompt secret")
    assert str(caught.value) == "agent request failed"
    assert "prompt" not in repr(caught.value)
    assert "secret" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    await session.close()
