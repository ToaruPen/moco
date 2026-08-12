from __future__ import annotations

import asyncio
from dataclasses import fields
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

import pytest

from moco import runtime
from moco.codex.approval import ApprovalDecision
from moco.codex.broker import InteractionBroker, ReviewWithdrawal
from moco.codex.rpc import JsonValue
from moco.codex.schema import SemanticMethod
from moco.errors import (
    AgentTurnErrorCode,
    CodexAgentError,
    CodexReviewError,
    CodexRpcError,
    CodexRpcTimeoutError,
)
from moco.runtime.coordinator import (
    ConnectionState,
    HandoffDisposition,
    InteractionCoordinator,
    InteractionEffects,
    InteractionSnapshot,
    SpeechState,
    TaskState,
    TurnResult,
    VoiceState,
)
from test_codex_agent import (
    ITEM_COMPLETED_NOTIFICATION,
    TURN_COMPLETED_NOTIFICATION,
    WIRE_METHODS,
    FakeSharedConnection,
    make_session,
)
from test_codex_approval import broker, command_request, published

if TYPE_CHECKING:
    from collections.abc import Iterator


class AwaitableCleanupError(RuntimeError):
    pass


class CloseRaisesAwaitable:
    def __await__(self) -> Iterator[None]:
        return iter(())

    def close(self) -> None:
        message = "synthetic awaitable cleanup failure"
        raise AwaitableCleanupError(message)


class OtherErrorCode(StrEnum):
    RAW = "raw_server_detail"


_TRUE_VALUE: object = True


class FakeSession:
    def __init__(self, *, steer_available: bool = False) -> None:
        self.reusable = True
        self.steer_available = steer_available
        self.started: list[str] = []
        self.steered: list[str] = []
        self.start_futures: list[asyncio.Future[str]] = []
        self.steer_future: asyncio.Future[None] | None = None
        self.steer_error: CodexAgentError | None = None
        self.steer_cancel_gate: asyncio.Event | None = None
        self.start_immediate: str | None = None
        self.start_return_gate: asyncio.Event | None = None
        self.start_cancelled = 0
        self.interrupt_calls = 0

    async def start_turn(self, text: str) -> str:
        self.started.append(text)
        if self.start_immediate is not None:
            return self.start_immediate
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.start_futures.append(future)
        try:
            answer = await asyncio.shield(future)
            if self.start_return_gate is not None:
                await self.start_return_gate.wait()
        except asyncio.CancelledError:
            self.start_cancelled += 1
            message = "synthetic interrupted"
            raise CodexAgentError(
                message,
                code=AgentTurnErrorCode.INTERRUPTED,
            ) from None
        else:
            return answer

    async def steer(self, text: str) -> None:
        self.steered.append(text)
        if self.steer_error is not None:
            raise self.steer_error
        future = self.steer_future
        if future is not None:
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                if self.steer_cancel_gate is not None:
                    await self.steer_cancel_gate.wait()
                raise

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


class ContractBreakingSession(FakeSession):
    def __init__(self, final_answer: object) -> None:
        super().__init__()
        self.final_answer = final_answer

    async def start_turn(self, text: str) -> str:
        self.started.append(text)
        return cast("str", self.final_answer)


class SessionContract(Protocol):
    reusable: bool
    steer_available: bool

    async def start_turn(self, text: str) -> str: ...

    async def steer(self, text: str) -> None: ...


class EffectsRecorder:
    def __init__(self) -> None:
        self.snapshots: list[InteractionSnapshot] = []
        self.terminal_claims = 0
        self.results: list[TurnResult] = []
        self.submission_errors: list[str] = []

    def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
        self.snapshots.append(snapshot)

    def on_turn_terminal_claimed(self) -> None:
        self.terminal_claims += 1

    def on_turn_finished(self, result: TurnResult) -> None:
        self.results.append(result)

    def on_submission_error(self, code: str) -> None:
        self.submission_errors.append(code)


def coordinator(
    session: FakeSession,
    effects: InteractionEffects,
) -> InteractionCoordinator:
    return InteractionCoordinator(
        cast("SessionContract", session),
        steer_available=session.steer_available,
        effects=effects,
    )


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def finish_latest(session: FakeSession, result: str = "done") -> None:
    session.start_futures[-1].set_result(result)
    await settle()


async def emit_real_turn_completion(
    connection: FakeSharedConnection,
    *,
    final_answer: str,
) -> None:
    turn_id = f"agent-turn-{connection.turn_number}"
    await connection.emit(
        ITEM_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turnId": turn_id,
            "item": {
                "id": f"agent-item-{connection.turn_number}",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": final_answer,
            },
        },
    )
    await connection.emit(
        TURN_COMPLETED_NOTIFICATION,
        {
            "threadId": "agent-thread-1",
            "turn": {
                "id": turn_id,
                "items": [],
                "status": "completed",
            },
        },
    )


async def wait_for_real_turn_count(connection: FakeSharedConnection, count: int) -> None:
    for _ in range(20):
        if connection.turn_number == count:
            return
        await asyncio.sleep(0)
    message = "real Agent turn count did not converge"
    raise AssertionError(message)


async def begin_real_coordinator_turn(
    interaction: InteractionCoordinator,
    connection: FakeSharedConnection,
    text: str,
) -> None:
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final(text) is HandoffDisposition.STARTED
    await wait_for_real_turn_count(connection, 1)


def is_idle(interaction: InteractionCoordinator) -> bool:
    return interaction.idle


def voice_state(interaction: InteractionCoordinator) -> VoiceState:
    return interaction.snapshot.voice


def task_state(interaction: InteractionCoordinator) -> TaskState:
    return interaction.snapshot.task


def test_snapshot_contract_and_idle_projection() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)

    assert [field.name for field in fields(InteractionSnapshot)] == [
        "connection",
        "voice",
        "task",
        "speech",
    ]
    assert interaction.snapshot == InteractionSnapshot(
        connection=ConnectionState.STARTING,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    assert not is_idle(interaction)
    interaction.connection_changed(ConnectionState.READY)
    assert is_idle(interaction)
    interaction.speech_changed(SpeechState.SYNTHESIZING)
    assert not is_idle(interaction)
    interaction.speech_changed(SpeechState.SILENT)
    interaction.connection_changed(ConnectionState.DEGRADED)
    assert is_idle(interaction)
    interaction.connection_changed(ConnectionState.DISCONNECTED)
    assert not is_idle(interaction)


def test_coordinator_contract_is_exported_from_runtime_package() -> None:
    assert runtime.InteractionCoordinator is InteractionCoordinator
    assert runtime.InteractionSnapshot is InteractionSnapshot
    assert runtime.TurnResult is TurnResult


@pytest.mark.parametrize(
    ("enum_type", "values"),
    [
        (ConnectionState, ["starting", "ready", "degraded", "disconnected"]),
        (VoiceState, ["idle", "listening", "transcribing"]),
        (
            TaskState,
            [
                "none",
                "queued",
                "running",
                "waiting_review",
                "completed",
                "failed",
                "interrupted",
            ],
        ),
        (SpeechState, ["silent", "synthesizing", "playing"]),
    ],
)
def test_state_values_are_fixed(enum_type: type[StrEnum], values: list[str]) -> None:
    assert [state.value for state in enum_type] == values


async def test_duplicate_final_is_ignored_within_one_listen_generation() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)

    interaction.listen_started()
    assert voice_state(interaction) == VoiceState.LISTENING
    interaction.listen_started()
    interaction.listen_stopped()
    assert voice_state(interaction) == VoiceState.IDLE

    assert await interaction.consume_user_final("同じ依頼") is HandoffDisposition.STARTED
    assert await interaction.consume_user_final("duplicate") is HandoffDisposition.IGNORED
    await settle()
    assert session.started == ["同じ依頼"]

    session.start_futures[0].set_result("完了")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("同じ依頼") is HandoffDisposition.STARTED
    await settle()
    assert session.started == ["同じ依頼", "同じ依頼"]
    await finish_latest(session)


async def test_live_listening_handoffs_each_vad_final_without_listen_stop() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)

    interaction.listen_started()
    assert (
        await interaction.consume_user_final("first live utterance") is HandoffDisposition.STARTED
    )
    await settle()
    assert interaction.snapshot.voice is VoiceState.LISTENING
    assert session.started == ["first live utterance"]

    await finish_latest(session, "first final")
    assert (
        await interaction.consume_user_final("second live utterance") is HandoffDisposition.STARTED
    )
    await settle()
    assert interaction.snapshot.voice is VoiceState.LISTENING
    assert session.started == ["first live utterance", "second live utterance"]
    await finish_latest(session, "second final")


async def test_live_listening_claims_each_utterance_identity_once() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)

    interaction.listen_started()
    assert (
        await interaction.consume_user_final("同じ依頼", utterance_id=1)
        is HandoffDisposition.STARTED
    )
    assert (
        await interaction.consume_user_final("同じ依頼", utterance_id=1)
        is HandoffDisposition.IGNORED
    )
    await settle()
    assert session.started == ["同じ依頼"]

    await finish_latest(session, "first final")
    assert (
        await interaction.consume_user_final("同じ依頼", utterance_id=2)
        is HandoffDisposition.STARTED
    )
    await settle()
    assert session.started == ["同じ依頼", "同じ依頼"]
    await finish_latest(session, "second final")


def test_listen_stop_mutes_and_a_fresh_start_resumes_immediately() -> None:
    interaction = coordinator(FakeSession(), EffectsRecorder())
    interaction.connection_changed(ConnectionState.READY)

    interaction.listen_started()
    interaction.listen_stopped()
    assert interaction.snapshot.voice is VoiceState.IDLE

    interaction.listen_started()
    resumed = interaction.snapshot
    assert resumed.voice is VoiceState.LISTENING


async def test_voice_lost_abandons_unfinished_listen_generation() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)

    interaction.listen_started()
    interaction.listen_stopped()
    interaction.voice_lost()
    assert interaction.snapshot.voice is VoiceState.IDLE
    assert await interaction.consume_user_final("late old final") is HandoffDisposition.IGNORED

    interaction.listen_started()
    assert await interaction.consume_user_final("current final") is HandoffDisposition.STARTED
    await settle()
    assert session.started == ["current final"]
    await finish_latest(session)


async def test_running_turn_has_one_private_queue_and_promotes_without_terminal_snapshot() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)

    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("first") is HandoffDisposition.STARTED
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("second") is HandoffDisposition.QUEUED
    assert task_state(interaction) is TaskState.RUNNING
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("third") is HandoffDisposition.BUSY
    assert effects.submission_errors == ["interaction_busy"]

    session.start_futures[0].set_result("first final")
    await settle()
    assert session.started == ["first", "second"]
    assert interaction.snapshot.task is TaskState.RUNNING
    assert TaskState.COMPLETED not in [snapshot.task for snapshot in effects.snapshots]
    assert effects.terminal_claims == 1
    assert effects.results == [TurnResult(final_answer="first final", error_code=None)]
    await finish_latest(session)


async def test_running_turn_steers_when_available() -> None:
    session = FakeSession(steer_available=True)
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()

    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("additional") is HandoffDisposition.STEERED
    await settle()

    assert session.started == ["first"]
    assert session.steered == ["additional"]
    assert interaction.snapshot.task is TaskState.RUNNING
    await finish_latest(session)


async def test_waiting_review_queues_even_when_steer_is_available() -> None:
    session = FakeSession(steer_available=True)
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()

    interaction.review_count_changed(1)
    assert interaction.snapshot.task is TaskState.WAITING_REVIEW
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("after review") is HandoffDisposition.QUEUED
    await settle()
    assert session.steered == []
    assert interaction.snapshot.task is TaskState.WAITING_REVIEW
    await finish_latest(session)


async def test_queued_review_input_stays_ahead_after_review_count_returns_to_zero() -> None:
    session = FakeSession(steer_available=True)
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("first") is HandoffDisposition.STARTED
    await settle()

    interaction.review_count_changed(1)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("queued first") is HandoffDisposition.QUEUED
    interaction.review_count_changed(0)
    assert interaction.snapshot.task is TaskState.RUNNING

    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("later input") is HandoffDisposition.BUSY
    assert session.steered == []
    assert effects.submission_errors == ["interaction_busy"]
    assert interaction.snapshot.task is TaskState.RUNNING

    await finish_latest(session, "first final")
    assert session.started == ["first", "queued first"]
    assert session.steered == []
    assert interaction.snapshot.task is TaskState.RUNNING
    await finish_latest(session, "queued final")


async def test_known_steer_rejection_is_submission_error_only() -> None:
    session = FakeSession(steer_available=True)
    session.steer_error = CodexAgentError(
        "must not leak",
        code="agent_steer_rejected",
    )
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()

    assert await interaction.consume_user_final("rejected") is HandoffDisposition.REJECTED
    await settle()

    assert effects.submission_errors == ["agent_steer_rejected"]
    assert effects.results == []
    assert interaction.snapshot.task is TaskState.RUNNING
    await finish_latest(session)


async def test_reusable_local_steer_rejection_without_code_preserves_turn() -> None:
    session = FakeSession(steer_available=True)
    session.steer_error = CodexAgentError("private local rejection")
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()

    disposition = await interaction.consume_user_final("rejected before send")

    assert disposition is HandoffDisposition.REJECTED
    assert effects.submission_errors == ["agent_steer_rejected"]
    assert effects.results == []
    assert interaction.snapshot.task is TaskState.RUNNING
    assert interaction.snapshot.connection is ConnectionState.READY
    await finish_latest(session)


async def test_actual_final_wins_reusable_no_active_steer_rejection() -> None:
    session = FakeSession(steer_available=True)
    session.start_return_gate = asyncio.Event()
    session.steer_error = CodexAgentError("no active turn")
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()

    session.start_futures[0].set_result("actual final")
    await settle()
    disposition = await interaction.consume_user_final("too late to steer")
    session.start_return_gate.set()
    await settle()

    assert disposition is HandoffDisposition.REJECTED
    assert effects.submission_errors == ["agent_steer_rejected"]
    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert effects.terminal_claims == 1
    assert task_state(interaction) is TaskState.COMPLETED
    assert interaction.snapshot.connection is ConnectionState.READY


async def test_unknown_steer_terminalizes_active_without_replay() -> None:
    session = FakeSession(steer_available=True)
    session.steer_error = CodexAgentError(
        "secret server payload",
        code=AgentTurnErrorCode.OUTCOME_UNKNOWN,
    )
    session.reusable = False
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()

    assert await interaction.consume_user_final("unknown submission") is HandoffDisposition.REJECTED
    await settle()

    assert effects.results == [TurnResult(final_answer=None, error_code="agent_outcome_unknown")]
    assert interaction.snapshot.task is TaskState.FAILED
    assert interaction.snapshot.connection is ConnectionState.DISCONNECTED
    assert session.interrupt_calls == 0


@pytest.mark.parametrize(
    ("code", "reusable", "expected_code", "expected_task"),
    [
        (AgentTurnErrorCode.FAILED, True, "agent_turn_failed", TaskState.FAILED),
        (AgentTurnErrorCode.INTERRUPTED, True, "agent_turn_interrupted", TaskState.INTERRUPTED),
        (AgentTurnErrorCode.OUTCOME_UNKNOWN, False, "agent_outcome_unknown", TaskState.FAILED),
    ],
)
async def test_turn_errors_map_only_to_stable_codes(
    code: AgentTurnErrorCode | None,
    reusable: bool,
    expected_code: str,
    expected_task: TaskState,
) -> None:
    session = FakeSession()
    session.reusable = reusable
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("private text")
    await settle()

    session.start_futures[0].set_exception(
        CodexAgentError("raw private failure", code=code),
    )
    await settle()

    assert effects.results == [TurnResult(final_answer=None, error_code=expected_code)]
    assert interaction.snapshot.task is expected_task
    assert "raw private failure" not in repr(effects.results)


def test_turn_result_requires_exactly_one_value() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        TurnResult(final_answer=None, error_code=None)
    with pytest.raises(ValueError, match="exactly one"):
        TurnResult(final_answer="answer", error_code="agent_turn_failed")


@pytest.mark.parametrize(
    "code", [*AgentTurnErrorCode, *(item.value for item in AgentTurnErrorCode)]
)
def test_turn_result_accepts_only_canonical_terminal_codes(code: AgentTurnErrorCode | str) -> None:
    result = TurnResult(final_answer=None, error_code=code)

    assert result.error_code == str(code)
    assert type(result.error_code) is str


@pytest.mark.parametrize(
    "code",
    ["raw private detail", OtherErrorCode.RAW, cast("str", 7), cast("str", object())],
)
def test_turn_result_rejects_noncanonical_error_codes(code: str) -> None:
    with pytest.raises(ValueError, match="terminal error code") as caught:
        TurnResult(final_answer=None, error_code=code)

    assert "raw private detail" not in str(caught.value)


@pytest.mark.parametrize(
    "final_answer",
    [cast("str", 7), cast("str", _TRUE_VALUE), cast("str", object())],
)
def test_turn_result_rejects_non_string_final_answer(final_answer: str) -> None:
    with pytest.raises(ValueError, match="final answer"):
        TurnResult(final_answer=final_answer, error_code=None)


def test_turn_result_rejects_string_subclass_final_answer() -> None:
    class FinalText(str):
        __slots__ = ()

    with pytest.raises(ValueError, match="final answer"):
        TurnResult(final_answer=FinalText("answer"), error_code=None)


def test_turn_result_allows_blank_builtin_string_without_content_policy() -> None:
    assert TurnResult(final_answer="", error_code=None).final_answer == ""


@pytest.mark.parametrize(
    "invalid_answer",
    [
        pytest.param(None, id="none"),
        pytest.param(7, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(object(), id="object"),
        pytest.param(type("FinalText", (str,), {})("PRIVATE_FINAL"), id="str-subclass"),
    ],
)
async def test_agent_return_contract_violation_terminalizes_unknown_without_replay(
    invalid_answer: object,
) -> None:
    session = ContractBreakingSession(invalid_answer)
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("contract violation") is HandoffDisposition.STARTED
    turn_task = interaction._turn_task  # noqa: SLF001 - claim retirement seam
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("never replay") is HandoffDisposition.QUEUED

    await settle()

    assert turn_task is not None
    assert turn_task.done()
    assert turn_task.exception() is None
    assert interaction._turn_task is None  # noqa: SLF001 - claim retirement seam
    assert session.started == ["contract violation"]
    assert effects.results == [TurnResult(final_answer=None, error_code="agent_outcome_unknown")]
    assert effects.terminal_claims == 1
    assert effects.submission_errors == []
    assert interaction.snapshot.task is TaskState.FAILED
    assert interaction.snapshot.connection is ConnectionState.DISCONNECTED
    assert "PRIVATE_FINAL" not in repr(effects.results)


async def test_real_agent_terminal_first_steer_settles_before_session_reuse() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await connection.steer_requested.wait()

    await emit_real_turn_completion(connection, final_answer="actual final")
    await settle()

    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert effects.terminal_claims == 1
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("next") is HandoffDisposition.QUEUED
    assert connection.turn_number == 1
    connection.steer_gate.set()
    assert await steer is HandoffDisposition.STEERED
    assert session.reusable

    await wait_for_real_turn_count(connection, 2)
    turn_start_method = WIRE_METHODS[SemanticMethod.TURN_START]
    assert [call[0] for call in connection.calls].count(turn_start_method) == 2
    await emit_real_turn_completion(connection, final_answer="next final")
    await settle()
    assert effects.results == [
        TurnResult(final_answer="actual final", error_code=None),
        TurnResult(final_answer="next final", error_code=None),
    ]
    await session.close()


@pytest.mark.parametrize("known_rejection", [False, True])
async def test_real_agent_terminal_first_barrier_is_not_idle_until_known_settlement(
    known_rejection: bool,
) -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    if known_rejection:
        connection.steer_error = CodexRpcError("PRIVATE_REJECTION", code=-32000)
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await connection.steer_requested.wait()

    await emit_real_turn_completion(connection, final_answer="actual final")
    await settle()

    assert interaction.snapshot == InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.RUNNING,
        speech=SpeechState.SILENT,
    )
    assert not is_idle(interaction)
    settlement_snapshot_count = len(effects.snapshots)

    connection.steer_gate.set()
    expected = HandoffDisposition.REJECTED if known_rejection else HandoffDisposition.STEERED
    assert await steer is expected
    assert session.reusable
    assert task_state(interaction) is TaskState.COMPLETED
    assert is_idle(interaction)
    assert len(effects.snapshots) == settlement_snapshot_count + 1
    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    await session.close()


async def test_real_agent_cancel_claims_terminal_first_running_barrier_once() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await connection.steer_requested.wait()
    await emit_real_turn_completion(connection, final_answer="actual final")
    await settle()

    assert task_state(interaction) is TaskState.RUNNING
    assert await interaction.cancel_turn()
    assert not await interaction.cancel_turn()
    with pytest.raises(asyncio.CancelledError):
        await steer
    assert not session.reusable
    assert interaction.snapshot == InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.COMPLETED,
        speech=SpeechState.SILENT,
    )
    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert effects.terminal_claims == 1
    effect_counts = (
        len(effects.snapshots),
        len(effects.results),
        effects.terminal_claims,
        len(effects.submission_errors),
    )

    connection.steer_gate.set()
    await settle()

    assert connection.turn_number == 1
    assert (
        len(effects.snapshots),
        len(effects.results),
        effects.terminal_claims,
        len(effects.submission_errors),
    ) == effect_counts
    await session.close()


@pytest.mark.parametrize("interrupt_failure", [False, True])
async def test_real_agent_active_cancel_drains_interrupt_before_propagating_caller_cancel(
    interrupt_failure: bool,
) -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    if interrupt_failure:
        connection.interrupt_error = RuntimeError("PRIVATE_INTERRUPT_FAILURE")
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")

    cancellation = asyncio.create_task(interaction.cancel_turn())
    await connection.interrupt_requested.wait()
    cancellation.cancel("first caller cancellation")
    await settle()
    cancellation.cancel("later caller cancellation")
    interaction.listen_started()
    interaction.listen_stopped()
    assert (
        await interaction.consume_user_final("blocked until settlement") is HandoffDisposition.BUSY
    )
    await settle()

    assert not cancellation.done()
    assert effects.results == []
    assert effects.terminal_claims == 0

    connection.interrupt_gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await cancellation

    assert caught.value.args == ("first caller cancellation",)
    interrupt_method = WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    assert [call[0] for call in connection.calls].count(interrupt_method) == 1
    expected_result = TurnResult(
        final_answer=None,
        error_code=(
            AgentTurnErrorCode.OUTCOME_UNKNOWN
            if interrupt_failure
            else AgentTurnErrorCode.INTERRUPTED
        ),
    )
    assert effects.results == [expected_result]
    assert effects.terminal_claims == 1
    assert interaction.snapshot.task is (
        TaskState.FAILED if interrupt_failure else TaskState.INTERRUPTED
    )
    assert interaction.snapshot.connection is (
        ConnectionState.DISCONNECTED if interrupt_failure else ConnectionState.READY
    )
    assert session.reusable is not interrupt_failure
    await session.close()


@pytest.mark.parametrize("queued", [False, True])
async def test_terminal_first_rejection_claims_state_before_submission_effect(
    queued: bool,
) -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    connection.steer_error = CodexRpcError("PRIVATE_REJECTION", code=-32000)
    session = make_session(connection)
    observed: list[tuple[InteractionSnapshot, bool]] = []
    reentrant_cancel: asyncio.Task[bool] | None = None

    class InspectingEffects(EffectsRecorder):
        def on_submission_error(self, code: str) -> None:
            nonlocal reentrant_cancel
            super().on_submission_error(code)
            observed.append((interaction.snapshot, interaction.idle))
            if not queued:
                reentrant_cancel = asyncio.create_task(interaction.cancel_turn())

    effects = InspectingEffects()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await connection.steer_requested.wait()
    if queued:
        interaction.review_count_changed(1)
        interaction.listen_started()
        interaction.listen_stopped()
        assert await interaction.consume_user_final("queued") is HandoffDisposition.QUEUED
    await emit_real_turn_completion(connection, final_answer="actual final")
    await settle()

    connection.steer_gate.set()
    assert await steer is HandoffDisposition.REJECTED
    await settle()

    assert observed == [
        (
            InteractionSnapshot(
                connection=ConnectionState.READY,
                voice=VoiceState.IDLE,
                task=TaskState.RUNNING if queued else TaskState.COMPLETED,
                speech=SpeechState.SILENT,
            ),
            not queued,
        )
    ]
    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert effects.terminal_claims == 1
    if queued:
        assert reentrant_cancel is None
        await wait_for_real_turn_count(connection, 2)
        await emit_real_turn_completion(connection, final_answer="queued final")
        await settle()
    else:
        assert reentrant_cancel is not None
        assert not await reentrant_cancel
    await session.close()


async def test_real_agent_terminal_first_steer_defers_queued_promotion() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await connection.steer_requested.wait()
    interaction.review_count_changed(1)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("queued") is HandoffDisposition.QUEUED

    await emit_real_turn_completion(connection, final_answer="actual final")
    await settle()

    assert connection.turn_number == 1
    assert interaction.snapshot == InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.QUEUED,
        speech=SpeechState.SILENT,
    )
    assert not interaction.idle
    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    connection.steer_gate.set()
    assert await steer is HandoffDisposition.STEERED
    await wait_for_real_turn_count(connection, 2)
    assert session.reusable
    assert interaction.snapshot.task is TaskState.RUNNING
    turn_start_method = WIRE_METHODS[SemanticMethod.TURN_START]
    assert [call[0] for call in connection.calls].count(turn_start_method) == 2
    await emit_real_turn_completion(connection, final_answer="queued final")
    await settle()
    assert effects.results == [
        TurnResult(final_answer="actual final", error_code=None),
        TurnResult(final_answer="queued final", error_code=None),
    ]
    await session.close()


async def test_real_agent_terminal_first_unknown_steer_discards_queue_without_replay() -> None:
    connection = FakeSharedConnection()
    connection.steer_gate = asyncio.Event()
    connection.steer_error = CodexRpcTimeoutError("turn/steer", 0.1)
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(interaction, connection, "first")
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await connection.steer_requested.wait()
    interaction.review_count_changed(1)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("never replay") is HandoffDisposition.QUEUED

    await emit_real_turn_completion(connection, final_answer="actual final")
    await settle()
    assert task_state(interaction) is TaskState.QUEUED
    assert not interaction.idle
    connection.steer_gate.set()
    assert await steer is HandoffDisposition.REJECTED
    await settle()

    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert effects.terminal_claims == 1
    assert effects.submission_errors == ["agent_outcome_unknown"]
    assert interaction.snapshot.connection is ConnectionState.DISCONNECTED
    assert task_state(interaction) is TaskState.COMPLETED
    assert not interaction.idle
    assert connection.turn_number == 1
    assert not session.reusable
    await session.close()


async def test_cancel_waits_for_inflight_steer_then_cancels_wrapper_once() -> None:
    session = FakeSession(steer_available=True)
    session.steer_future = asyncio.get_running_loop().create_future()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await settle()

    assert await interaction.cancel_turn()
    await settle()
    with pytest.raises(asyncio.CancelledError):
        await steer

    assert session.start_cancelled == 1
    assert session.interrupt_calls == 0
    assert effects.results == [TurnResult(final_answer=None, error_code="agent_turn_interrupted")]
    assert interaction.snapshot.task is TaskState.INTERRUPTED


async def test_terminal_wins_while_cancel_waits_for_steer_settlement() -> None:
    session = FakeSession(steer_available=True)
    session.steer_future = asyncio.get_running_loop().create_future()
    session.steer_cancel_gate = asyncio.Event()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await settle()

    cancellation = asyncio.create_task(interaction.cancel_turn())
    await settle()
    session.start_futures[0].set_result("actual final")
    await settle()
    assert not cancellation.done()
    session.steer_cancel_gate.set()
    assert await cancellation
    with pytest.raises(asyncio.CancelledError):
        await steer
    await settle()

    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert session.start_cancelled == 0
    assert session.interrupt_calls == 0


async def test_duplicate_terminal_is_prevented_when_final_waits_for_inflight_steer() -> None:
    session = FakeSession(steer_available=True)
    session.steer_future = asyncio.get_running_loop().create_future()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("first")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await settle()

    session.start_futures[0].set_result("actual final")
    await settle()
    assert session.steer_future is not None
    session.steer_future.set_result(None)
    assert await steer is HandoffDisposition.STEERED

    assert effects.results == [TurnResult(final_answer="actual final", error_code=None)]
    assert effects.terminal_claims == 1


async def test_slow_turn_emits_no_terminal_result_until_completion() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("slow")

    await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    assert effects.results == []
    assert interaction.snapshot.task is TaskState.RUNNING
    session.start_futures[0].set_result("done")
    await settle()
    assert effects.results == [TurnResult(final_answer="done", error_code=None)]
    assert effects.terminal_claims == 1


async def test_fast_turn_emits_only_final_result() -> None:
    session = FakeSession()
    session.start_immediate = "fast"
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("fast")
    await settle()

    assert effects.results == [TurnResult(final_answer="fast", error_code=None)]
    assert effects.terminal_claims == 1


async def test_review_terminal_claim_precedes_snapshot_and_late_count_cannot_resurrect() -> None:
    events: list[str] = []

    class OrderedEffects(EffectsRecorder):
        def on_turn_terminal_claimed(self) -> None:
            super().on_turn_terminal_claimed()
            events.append("claim")

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            super().on_snapshot_changed(snapshot)
            if snapshot.task is TaskState.COMPLETED:
                events.append("snapshot")

        def on_turn_finished(self, result: TurnResult) -> None:
            super().on_turn_finished(result)
            events.append("finished")

    session = FakeSession()
    effects = OrderedEffects()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("reviewed")
    await settle()
    interaction.review_count_changed(1)
    session.start_futures[0].set_result("done")
    await settle()

    assert events == ["claim", "snapshot", "finished"]
    interaction.review_count_changed(1)
    assert interaction.snapshot.task is TaskState.COMPLETED


async def test_connection_lost_discards_active_queue_and_old_utterance() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("active")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("queued")

    interaction.connection_lost()
    await settle()

    assert interaction.snapshot.connection is ConnectionState.DISCONNECTED
    assert interaction.snapshot.task is TaskState.FAILED
    assert interaction.snapshot.voice is VoiceState.IDLE
    assert effects.results == [TurnResult(final_answer=None, error_code="agent_outcome_unknown")]
    assert await interaction.consume_user_final("late") is HandoffDisposition.IGNORED
    assert session.started == ["active"]


async def test_admission_rejection_is_submission_error_without_terminal_result() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    assert (
        await interaction.consume_user_final("rejected before wire") is HandoffDisposition.STARTED
    )
    await settle()
    session.start_futures[0].set_exception(CodexAgentError("private admission detail"))
    await settle()

    assert effects.submission_errors == ["agent_submission_rejected"]
    assert effects.results == []
    assert effects.terminal_claims == 0
    assert interaction.snapshot.task is TaskState.NONE


async def test_cancel_discards_queue_and_second_cancel_is_noop() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("active")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("queued")

    assert await interaction.cancel_turn()
    assert not await interaction.cancel_turn()
    await settle()

    assert session.started == ["active"]
    assert effects.results == [TurnResult(final_answer=None, error_code="agent_turn_interrupted")]

    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("fresh after cancel") is HandoffDisposition.STARTED
    await settle()
    assert session.started == ["active", "fresh after cancel"]
    await finish_latest(session)


async def test_spoken_cancel_is_a_normal_queued_utterance_not_explicit_control() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("active")
    await settle()

    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("キャンセル") is HandoffDisposition.QUEUED

    assert session.start_cancelled == 0
    assert effects.results == []
    await finish_latest(session)
    assert session.started == ["active", "キャンセル"]
    await finish_latest(session)


class _CancellationSettlingConnection(FakeSharedConnection):
    """Return the accepted turn id after caller cancellation, as RpcPeer settlement does."""

    async def request(self, method: str, params: object = None, **kwargs: object) -> JsonValue:
        if method != WIRE_METHODS[SemanticMethod.TURN_START]:
            return await super().request(
                method,
                cast("dict[str, JsonValue] | None", params),
                **kwargs,
            )
        self.calls.append((method, params, kwargs))
        self.turn_number += 1
        self.turn_requested.set()
        gate = self.turn_start_gate
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
        return {"turn": {"id": f"agent-turn-{self.turn_number}"}}


async def test_real_agent_cancel_during_thread_start_sends_no_interrupt() -> None:
    connection = FakeSharedConnection()
    connection.thread_start_gate = asyncio.Event()
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("before thread") is HandoffDisposition.STARTED
    await connection.thread_requested.wait()

    assert await interaction.cancel_turn()

    interrupt_method = WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    assert [call[0] for call in connection.calls].count(interrupt_method) == 0
    assert effects.results == [
        TurnResult(final_answer=None, error_code=AgentTurnErrorCode.INTERRUPTED)
    ]
    await session.close()


async def test_real_agent_cancel_waits_for_turn_start_id_then_interrupts_once() -> None:
    connection = _CancellationSettlingConnection()
    connection.turn_start_gate = asyncio.Event()
    session = make_session(connection)
    effects = EffectsRecorder()
    interaction = InteractionCoordinator(
        session,
        steer_available=True,
        effects=effects,
    )
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("accepted start") is HandoffDisposition.STARTED
    await connection.turn_requested.wait()

    cancellation = asyncio.create_task(interaction.cancel_turn())
    await settle()
    assert not cancellation.done()
    connection.turn_start_gate.set()

    assert await cancellation
    interrupt_method = WIRE_METHODS[SemanticMethod.TURN_INTERRUPT]
    assert [call[0] for call in connection.calls].count(interrupt_method) == 1
    assert effects.results == [
        TurnResult(final_answer=None, error_code=AgentTurnErrorCode.INTERRUPTED)
    ]
    assert session.reusable
    await session.close()


async def test_waiting_review_cancel_withdraws_before_coordinator_cancel_and_reuses_broker() -> (
    None
):
    interaction_broker = broker()
    session = FakeSession()

    class BrokerEffects(EffectsRecorder):
        def __init__(self, owned_broker: InteractionBroker) -> None:
            super().__init__()
            self.broker = owned_broker

        def on_turn_terminal_claimed(self) -> None:
            super().on_turn_terminal_claimed()
            self.broker.cancel_pending()

    effects = BrokerEffects(interaction_broker)
    interaction = coordinator(session, effects)
    interaction_broker.bind_pending_count_changed(interaction.review_count_changed)
    reviewer = interaction_broker.connect_reviewer()
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("needs review")
    await settle()
    review_task, envelope = await published(interaction_broker, reviewer, command_request())

    assert interaction.snapshot.task is TaskState.WAITING_REVIEW
    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("queued") is HandoffDisposition.QUEUED

    interaction_broker.cancel_pending()
    assert task_state(interaction) is TaskState.RUNNING
    assert await interaction.cancel_turn()

    with pytest.raises(CodexReviewError, match="local review was cancelled"):
        await review_task
    withdrawal = await anext(reviewer)
    assert withdrawal == ReviewWithdrawal(handle=envelope.handle)
    with pytest.raises(CodexReviewError):
        interaction_broker.decide(reviewer, envelope.handle, ApprovalDecision.ACCEPT)
    assert session.started == ["needs review"]

    interaction.listen_started()
    interaction.listen_stopped()
    assert await interaction.consume_user_final("fresh") is HandoffDisposition.STARTED
    await settle()
    next_review, next_envelope = await published(
        interaction_broker,
        reviewer,
        command_request("next-review"),
    )
    interaction_broker.decide(reviewer, next_envelope.handle, ApprovalDecision.ACCEPT)
    assert await next_review == {"decision": "accept"}
    await finish_latest(session)
    interaction_broker.close()


async def test_turn_terminal_hook_withdraws_review_before_completed_snapshot() -> None:
    interaction_broker = broker()
    session = FakeSession()
    events: list[str] = []

    class TerminalBrokerEffects(EffectsRecorder):
        def on_turn_terminal_claimed(self) -> None:
            super().on_turn_terminal_claimed()
            events.append("terminal")
            interaction_broker.cancel_pending()

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            super().on_snapshot_changed(snapshot)
            if snapshot.task is TaskState.COMPLETED:
                events.append("completed")

    effects = TerminalBrokerEffects()
    interaction = coordinator(session, effects)
    interaction_broker.bind_pending_count_changed(interaction.review_count_changed)
    reviewer = interaction_broker.connect_reviewer()
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("terminal review")
    await settle()
    review_task, envelope = await published(interaction_broker, reviewer, command_request())
    assert interaction.snapshot.task is TaskState.WAITING_REVIEW

    session.start_futures[0].set_result("done")
    await settle()

    with pytest.raises(CodexReviewError, match="local review was cancelled"):
        await review_task
    assert events == ["terminal", "completed"]
    assert task_state(interaction) is TaskState.COMPLETED
    assert await anext(reviewer) == ReviewWithdrawal(handle=envelope.handle)
    with pytest.raises(CodexReviewError):
        interaction_broker.decide(reviewer, envelope.handle, ApprovalDecision.ACCEPT)
    interaction_broker.close()


async def test_nonreusable_cancel_is_unknown_without_separate_interrupt() -> None:
    session = FakeSession(steer_available=True)
    session.steer_future = asyncio.get_running_loop().create_future()
    session.steer_cancel_gate = asyncio.Event()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("active")
    await settle()
    interaction.listen_started()
    interaction.listen_stopped()
    steer = asyncio.create_task(interaction.consume_user_final("steer"))
    await settle()

    cancellation = asyncio.create_task(interaction.cancel_turn())
    await settle()
    session.reusable = False
    session.steer_cancel_gate.set()
    assert await cancellation
    with pytest.raises(asyncio.CancelledError):
        await steer
    await settle()

    assert effects.results == [TurnResult(final_answer=None, error_code="agent_outcome_unknown")]
    assert interaction.snapshot.connection is ConnectionState.DISCONNECTED
    assert session.interrupt_calls == 0


async def test_terminal_claim_precedes_queued_promotion_snapshot() -> None:
    events: list[str] = []

    class OrderedEffects(EffectsRecorder):
        def on_turn_terminal_claimed(self) -> None:
            super().on_turn_terminal_claimed()
            events.append("claim")

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            super().on_snapshot_changed(snapshot)
            if snapshot.task is TaskState.RUNNING:
                events.append("running")

    session = FakeSession()
    effects = OrderedEffects()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("active")
    await settle()
    interaction.review_count_changed(1)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("queued")
    events.clear()

    session.start_futures[0].set_result("done")
    await settle()

    assert events[:2] == ["claim", "running"]
    assert session.started == ["active", "queued"]
    await finish_latest(session)


async def test_callback_failures_and_awaitables_are_contained_without_reissue() -> None:
    calls: list[str] = []

    async def forbidden_async_effect() -> None:
        calls.append("awaited")

    callback_error = "private callback detail"

    class HostileEffects:
        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> object:
            del snapshot
            calls.append("snapshot")
            return forbidden_async_effect()

        def on_turn_terminal_claimed(self) -> None:
            calls.append("claim")
            raise RuntimeError(callback_error)

        def on_turn_finished(self, result: TurnResult) -> None:
            del result
            calls.append("finished")
            raise RuntimeError(callback_error)

        def on_submission_error(self, code: str) -> None:
            del code

    session = FakeSession()
    effects = HostileEffects()
    interaction = coordinator(session, cast("InteractionEffects", effects))
    interaction.connection_changed(ConnectionState.READY)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("slow")
    await asyncio.sleep(0.01)
    session.start_futures[0].set_result("done")
    await settle()

    assert calls.count("claim") == 1
    assert calls.count("finished") == 1
    assert "awaited" not in calls


@pytest.mark.parametrize("failure_mode", ["cancelled", "close_raises", "pending_future"])
async def test_terminal_effect_cleanup_failure_does_not_stop_following_effects(
    failure_mode: str,
) -> None:
    events: list[str] = []
    returned_future: asyncio.Future[None] | None = None

    class CleanupEffects:
        def __init__(self) -> None:
            self.results: list[TurnResult] = []

        def on_turn_terminal_claimed(self) -> object:
            nonlocal returned_future
            events.append("claim")
            if failure_mode == "cancelled":
                raise asyncio.CancelledError
            if failure_mode == "close_raises":
                return CloseRaisesAwaitable()
            returned_future = asyncio.get_running_loop().create_future()
            return returned_future

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            if snapshot.task is TaskState.COMPLETED:
                events.append("snapshot")

        def on_turn_finished(self, result: TurnResult) -> None:
            self.results.append(result)
            events.append("finished")

        def on_submission_error(self, code: str) -> None:
            del code

    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        session = FakeSession()
        effects = CleanupEffects()
        interaction = coordinator(session, cast("InteractionEffects", effects))
        interaction.listen_started()
        interaction.listen_stopped()
        await interaction.consume_user_final("terminal effect containment")
        await settle()
        session.start_futures[0].set_result("done")
        await settle()
    finally:
        loop.set_exception_handler(previous_handler)

    assert interaction.snapshot.task is TaskState.COMPLETED
    assert events == ["claim", "snapshot", "finished"]
    assert effects.results == [TurnResult(final_answer="done", error_code=None)]
    if returned_future is not None:
        assert returned_future.cancelled()
    assert loop_errors == []


async def test_connection_lost_is_idempotent_for_terminal_result() -> None:
    session = FakeSession()
    effects = EffectsRecorder()
    interaction = coordinator(session, effects)
    interaction.listen_started()
    interaction.listen_stopped()
    await interaction.consume_user_final("active")
    await settle()

    interaction.connection_lost()
    interaction.connection_lost()
    await settle()

    assert effects.results == [TurnResult(final_answer=None, error_code="agent_outcome_unknown")]
    assert effects.terminal_claims == 1
