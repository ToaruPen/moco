from __future__ import annotations

import asyncio
import gc
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Literal, cast

import pytest
from fastapi.testclient import TestClient
from irodori_tts_infra.contracts import (
    CapabilitiesResponse,
    ConditioningCapabilities,
    EmojiCapability,
    Readiness,
    VoiceCapability,
)
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocket, WebSocketDisconnect

from moco.codex.agent import AgentActivityEvent
from moco.codex.approval import ApprovalDecision
from moco.codex.broker import ReviewWithdrawal
from moco.codex.capabilities import (
    ApprovalMode,
    CapabilityDiscovery,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    SandboxMode,
)
from moco.codex.connection import CodexConnectionSupervisor
from moco.codex.rpc import JsonValue, RpcNotification
from moco.codex.schema import (
    ClientMethodContract,
    CodexProtocolContract,
    CodexSchemaProbe,
    ParamsKind,
    SemanticMethod,
    ServerRequestCategory,
)
from moco.codex.session import (
    ActivityEvent,
    CodexConnection,
    CodexRealtimeSession,
    RealtimeErrorEvent,
    RealtimeEvent,
    ReasoningSummaryEvent,
    TranscriptEvent,
    load_realtime_prompt,
)
from moco.config import (
    AgentProfileMode,
    CodexSettings,
    IrodoriSettings,
    MocoSettings,
    RuntimeSettings,
    ServerSettings,
    SpeechSettings,
)
from moco.errors import (
    AgentTurnErrorCode,
    CodexPromptError,
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
from moco.runtime.hotkeys import Control
from moco.runtime.lifecycle import IdleLeaseTimer, LifecycleState
from moco.speech import queue as speech_queue
from moco.speech.contracts import IrodoriCapabilities
from moco.speech.irodori import (
    _MAX_CAPABILITY_VOICES,
    IrodoriClient,
    IrodoriError,
    IrodoriSynthesizer,
)
from moco.speech.queue import SpeechQueue
from moco.web import app as web_app
from moco.web.app import RealtimeSession, WebSynthesizer, create_app
from moco.web.messages import ClientControl, StartMessage
from moco.web.reviewer import ReviewerBroker
from test_codex_agent import FakeSharedConnection, make_session
from test_codex_approval import (
    Registrar,
    command_request,
    file_change_patch_contract,
    published,
)
from test_codex_approval import broker as make_broker
from test_coordinator import (
    EffectsRecorder,
    begin_real_coordinator_turn,
    emit_real_turn_completion,
    settle,
    wait_for_real_turn_count,
)

CAPABILITY = "test-capability"


@pytest.mark.asyncio
async def test_production_owner_composes_one_contract_before_start_and_publishes_after_voice(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    modern = SimpleNamespace(
        category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
        changes_member=None,
    )
    contract = SimpleNamespace(
        approval_profiles={"modern": modern},
        file_change_patch_profile=None,
    )
    snapshot = make_codex_snapshot()

    class Probe:
        def __init__(self, command: object) -> None:
            assert command == "resolved"

        async def probe(self) -> object:
            events.append("probe")
            return contract

    class Connection:
        terminal_callbacks: ClassVar[list[Callable[[], object]]] = []

        def __init__(self, command: object) -> None:
            assert command == "resolved"

        def register_notification_observer(self, _observer: object) -> None:
            events.append("observer")

        def register_terminal_callback(self, callback: object) -> None:
            self.terminal_callbacks.append(cast("Callable[[], object]", callback))
            events.append("terminal")

        def register_server_request_handler(self, _method: str, _handler: object) -> None:
            events.append("approval")

        async def start(self) -> None:
            events.append("connection.start")

        async def close(self) -> None:
            events.append("connection.close")

    class Broker:
        instance: Broker | None = None

        def __init__(self, value: object) -> None:
            assert value is contract
            type(self).instance = self
            events.append("broker")

        def register_approval_handlers(self, registrar: object) -> None:
            assert isinstance(registrar, Connection)
            registrar.register_notification_observer(object())
            registrar.register_server_request_handler("approval", object())
            registrar.register_terminal_callback(object())

        def bind_pending_count_changed(self, _callback: object) -> None:
            events.append("broker.bind")

        def bind_active_turn_check(self, _callback: object) -> None:
            events.append("broker.active")

        def bind_turn_terminal(self, _callback: object) -> None:
            events.append("broker.terminal")

        def cancel_pending(self) -> None:
            events.append("broker.withdraw")

        def close(self) -> None:
            events.append("broker.close")

    class Discovery:
        def __init__(self, rpc: object, **kwargs: object) -> None:
            assert isinstance(rpc, Connection)
            assert kwargs["contract"] is contract
            events.append("discovery.construct")

        async def discover(self) -> CapabilitySnapshot:
            events.append("discovery")
            return snapshot

    class Agent:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError

    class Coordinator:
        def __init__(self, session: object, **_kwargs: object) -> None:
            assert session is None
            self.snapshot = InteractionSnapshot(
                connection=ConnectionState.STARTING,
                voice=VoiceState.IDLE,
                task=TaskState.NONE,
                speech=SpeechState.SILENT,
            )
            events.append("coordinator")

        def review_count_changed(self, _count: int) -> None:
            return None

        def connection_changed(self, state: ConnectionState) -> None:
            events.append(f"publish.{state.value}")

        def connection_lost(self) -> None:
            events.append("coordinator.connection_lost")

        async def cancel_turn(self) -> None:
            return None

    class Voice:
        thread_id = "thr_test"

        def __init__(self, rpc: object, **kwargs: object) -> None:
            assert isinstance(rpc, Connection)
            assert kwargs["capabilities"] is snapshot
            assert kwargs["contract"] is contract
            events.append("voice.construct")

        async def start(self, sdp: str) -> str:
            assert sdp == "offer-sdp"
            events.append("voice.start")
            return "answer-sdp"

        async def close(self) -> None:
            events.append("voice.close")

    class Slot:
        def bind(self, value: object) -> None:
            assert isinstance(value, Broker)
            events.append("slot.bind")

        def release(self, value: object) -> None:
            assert isinstance(value, Broker)
            events.append("slot.release")

    monkeypatch.setattr(web_app, "resolve_codex_command", lambda _value: "resolved")
    monkeypatch.setattr(web_app, "CodexSchemaProbe", Probe)
    monkeypatch.setattr(web_app, "CodexConnectionSupervisor", Connection)
    monkeypatch.setattr(web_app, "InteractionBroker", Broker, raising=False)
    monkeypatch.setattr(web_app, "CapabilityDiscovery", Discovery)
    monkeypatch.setattr(web_app, "AgentSession", Agent, raising=False)
    monkeypatch.setattr(web_app, "InteractionCoordinator", Coordinator, raising=False)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", Voice)

    owner = web_app._codex_session_factory(  # noqa: SLF001
        MocoSettings(),
        Slot(),  # type: ignore[arg-type]
    )()
    assert isinstance(owner, web_app._CodexConversationOwner)  # noqa: SLF001

    class Effects(FakeInteractionEffects):
        def __init__(self) -> None:
            self.activities: list[AgentActivityEvent] = []

        def on_agent_activity(self, event: AgentActivityEvent) -> None:
            self.activities.append(event)

    effects = Effects()
    owner.bind_effects(effects)

    assert await owner.start("offer-sdp") == "answer-sdp"
    assert events == [
        "probe",
        "broker",
        "broker.terminal",
        "observer",
        "approval",
        "terminal",
        "terminal",
        "connection.start",
        "discovery.construct",
        "discovery",
        "broker.active",
        "coordinator",
        "broker.bind",
        "voice.construct",
        "voice.start",
        "slot.bind",
        "publish.degraded",
    ]
    assert effects.activities == []
    Connection.terminal_callbacks[-1]()
    Connection.terminal_callbacks[-1]()
    for _ in range(20):
        if owner.closed:
            break
        await asyncio.sleep(0)

    assert owner.closed
    assert events[-5:] == [
        "coordinator.connection_lost",
        "slot.release",
        "broker.close",
        "voice.close",
        "connection.close",
    ]
    assert events.count("coordinator.connection_lost") == 1


class FakeInteractionEffects:
    def on_snapshot_changed(self, _snapshot: InteractionSnapshot) -> None:
        return None

    def on_turn_terminal_claimed(self) -> None:
        return None

    def on_turn_finished(self, _result: object) -> None:
        return None

    def on_submission_error(self, _code: str) -> None:
        return None

    def on_agent_activity(self, _event: AgentActivityEvent) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("close_path", ["owner", "stop", "socket", "idle"])
async def test_published_owner_closes_without_starting_a_second_agent_thread(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
    close_path: str,
) -> None:
    events: list[str] = []
    snapshot = replace(
        make_codex_snapshot(),
        steer=CapabilityState(CapabilityStatus.VERSION_MISMATCH, "missing"),
    )

    class Probe:
        async def probe(self) -> object:
            return SimpleNamespace(approval_profiles={})

    class Connection:
        def register_notification_observer(self, _observer: object) -> None:
            return None

        def register_terminal_callback(self, _callback: object) -> None:
            return None

        def register_server_request_handler(self, _method: str, _handler: object) -> None:
            return None

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            events.append("connection.close")

    class Broker:
        def __init__(self, _contract: object) -> None:
            return None

        def register_approval_handlers(self, _registrar: object) -> None:
            return None

        def bind_pending_count_changed(self, _callback: object) -> None:
            return None

        def bind_active_turn_check(self, _callback: object) -> None:
            return None

        def bind_turn_terminal(self, _callback: object) -> None:
            return None

        def cancel_pending(self) -> None:
            events.append("broker.withdraw")

        def close(self) -> None:
            events.append("broker.close")

    class Discovery:
        def __init__(self, _rpc: object, **_kwargs: object) -> None:
            return None

        async def discover(self) -> CapabilitySnapshot:
            return snapshot

    class Agent:
        instance: Agent | None = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError

    class Voice:
        thread_id = "thr_test"
        active_turn_id: str | None = None

        def __init__(self, _rpc: object, **_kwargs: object) -> None:
            return None

        async def start(self, _sdp: str) -> str:
            return "answer-sdp"

        async def close(self) -> None:
            events.append("voice.close")

    class Slot:
        def bind(self, _broker: object) -> None:
            events.append("slot.bind")

        def release(self, _broker: object) -> None:
            events.append("slot.release")

    class Effects(FakeInteractionEffects):
        def __init__(self) -> None:
            self.results: list[TurnResult] = []
            self.snapshots: list[InteractionSnapshot] = []
            self.submission_errors: list[str] = []
            self.terminal_claims = 0

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            self.snapshots.append(snapshot)

        def on_turn_terminal_claimed(self) -> None:
            events.append("terminal.claim")
            self.terminal_claims += 1

        def on_turn_finished(self, result: object) -> None:
            self.results.append(cast("TurnResult", result))

        def on_submission_error(self, code: str) -> None:
            self.submission_errors.append(code)

    monkeypatch.setattr(web_app, "InteractionBroker", Broker)
    monkeypatch.setattr(web_app, "CapabilityDiscovery", Discovery)
    monkeypatch.setattr(web_app, "AgentSession", Agent)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", Voice)
    effects = Effects()
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", Connection()),
        contract_probe=cast("CodexSchemaProbe", Probe()),
        reviewer_slot=cast("web_app._ReviewerBrokerSlot", Slot()),  # noqa: SLF001
        working_directory=Path.cwd(),
    )
    owner.bind_effects(effects)
    assert await owner.start("offer-sdp") == "answer-sdp"
    assert Agent.instance is None

    browser = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", owner),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    browser._session = cast("RealtimeSession", owner)  # noqa: SLF001
    if close_path == "owner":
        await owner.close()
    elif close_path == "stop":
        assert not await browser._handle(json.dumps({"type": "stop"}))  # noqa: SLF001
        await browser.close()
    elif close_path == "socket":
        await browser.close()
    else:
        await browser._expire_conversation()  # noqa: SLF001
    await asyncio.sleep(0)

    assert owner.closed
    assert effects.terminal_claims == 0
    assert effects.results == []
    assert all(
        snapshot.connection is not ConnectionState.DISCONNECTED for snapshot in effects.snapshots
    )
    assert effects.submission_errors == []
    assert events[events.index("slot.bind") + 1 :] == [
        "broker.withdraw",
        "slot.release",
        "broker.close",
        "voice.close",
        "connection.close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "close"])
async def test_owner_withdraws_review_before_blocked_turn_settlement(  # noqa: C901, PLR0915
    operation: str,
) -> None:
    connection = FakeSharedConnection()
    connection.interrupt_gate = asyncio.Event()
    agent = make_session(connection)
    interaction_broker = make_broker()
    reviewer_slot = web_app._ReviewerBrokerSlot()  # noqa: SLF001
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", connection),
        working_directory=Path.cwd(),
        reviewer_slot=reviewer_slot,
    )
    downstream = EffectsRecorder()
    coordinator = InteractionCoordinator(
        agent,
        steer_available=False,
        effects=web_app._ConversationEffects(  # noqa: SLF001
            owner,
            downstream,
        ),
    )
    pending_counts: list[int] = []

    def pending_count_changed(count: int) -> None:
        pending_counts.append(count)
        owner._review_count_changed(count)  # noqa: SLF001

    owner._broker = interaction_broker  # noqa: SLF001
    owner._agent = agent  # noqa: SLF001
    owner._coordinator = coordinator  # noqa: SLF001
    owner._reviewer_bound = True  # noqa: SLF001
    owner._started = True  # noqa: SLF001
    interaction_broker.bind_pending_count_changed(pending_count_changed)
    reviewer = interaction_broker.connect_reviewer()
    reviewer_slot.bind(interaction_broker)
    coordinator.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(coordinator, connection, "first")
    review_task, envelope = await published(interaction_broker, reviewer)
    assert coordinator.snapshot.task is TaskState.WAITING_REVIEW
    assert pending_counts == [1]

    if operation == "cancel":

        class RealtimeVoice:
            active_turn_id = "agent-turn-1"

            async def interrupt_active_turn(self) -> bool:
                connection.interrupt_requested.set()
                gate = connection.interrupt_gate
                assert gate is not None
                await gate.wait()
                return True

            async def close(self) -> None:
                return None

        owner._voice = cast("CodexRealtimeSession", RealtimeVoice())  # noqa: SLF001
        owner._voice_active = True  # noqa: SLF001

    close_state_lock_held = operation == "close"
    if close_state_lock_held:
        await owner._state_lock.acquire()  # noqa: SLF001
    operation_task = asyncio.create_task(
        owner.cancel_turn() if operation == "cancel" else owner.close()
    )
    try:
        if close_state_lock_held:
            withdrawal = await asyncio.wait_for(anext(reviewer), timeout=1)
            assert withdrawal == ReviewWithdrawal(envelope.handle)
            assert pending_counts == [1, 0]
            assert not connection.interrupt_requested.is_set()
            owner._state_lock.release()  # noqa: SLF001
            close_state_lock_held = False

        await asyncio.wait_for(connection.interrupt_requested.wait(), timeout=1)
        assert not operation_task.done()
        for decision in ApprovalDecision:
            with pytest.raises(CodexReviewError, match="handle"):
                interaction_broker.decide(reviewer, envelope.handle, decision)

        if operation == "cancel":
            withdrawal = await asyncio.wait_for(anext(reviewer), timeout=1)
            assert withdrawal == ReviewWithdrawal(envelope.handle)
            assert pending_counts == [1, 0]

        connection.interrupt_gate.set()
        result = await asyncio.wait_for(operation_task, timeout=1)
        assert result is (True if operation == "cancel" else None)
        with pytest.raises(CodexReviewError):
            await review_task
        if operation == "cancel":
            assert await coordinator.cancel_turn()
        assert downstream.results == [
            TurnResult(final_answer=None, error_code="agent_turn_interrupted")
        ]

        if operation == "close":
            with pytest.raises(StopAsyncIteration):
                await anext(reviewer)
            return

        duplicate = asyncio.create_task(anext(reviewer))
        done, _ = await asyncio.wait({duplicate}, timeout=0.01)
        assert not done
        duplicate.cancel()
        with suppress(asyncio.CancelledError):
            await duplicate

        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("next") is HandoffDisposition.STARTED
        await wait_for_real_turn_count(connection, 2)
        next_review_task, next_envelope = await published(interaction_broker, reviewer)
        interaction_broker.decide(reviewer, next_envelope.handle, ApprovalDecision.ACCEPT)
        assert await next_review_task == {"decision": "accept"}
        await emit_real_turn_completion(connection, final_answer="next final")
        await settle()
        assert pending_counts == [1, 0, 1, 0]
    finally:
        if close_state_lock_held:
            owner._state_lock.release()  # noqa: SLF001
        connection.interrupt_gate.set()
        await asyncio.gather(operation_task, return_exceptions=True)
        if not review_task.done():
            review_task.cancel()
        await asyncio.gather(review_task, return_exceptions=True)
        await owner.close()


@pytest.mark.asyncio
async def test_broken_review_count_callback_terminalizes_the_whole_owner_lease() -> None:
    class SyntheticReviewCountError(RuntimeError):
        pass

    class Coordinator:
        def __init__(self) -> None:
            self.connection_losses = 0

        def review_count_changed(self, _count: int) -> None:
            raise SyntheticReviewCountError

        def connection_lost(self) -> None:
            self.connection_losses += 1

        async def cancel_turn(self) -> bool:
            return False

    connection = FakeSharedConnection()
    interaction_broker = make_broker()
    coordinator = Coordinator()
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", connection),
        working_directory=Path.cwd(),
    )
    owner._broker = interaction_broker  # noqa: SLF001
    owner._coordinator = cast("InteractionCoordinator", coordinator)  # noqa: SLF001
    owner._started = True  # noqa: SLF001
    interaction_broker.bind_pending_count_changed(owner._review_count_changed)  # noqa: SLF001
    interaction_broker.connect_reviewer()

    with pytest.raises(CodexReviewError):
        await interaction_broker.review(command_request())

    assert owner._connection_terminated  # noqa: SLF001
    close_task = owner._connection_loss_close_task  # noqa: SLF001
    assert close_task is not None
    await asyncio.wait_for(asyncio.shield(close_task), timeout=1)
    assert coordinator.connection_losses == 1
    assert owner.closed
    assert connection.close_called


def test_owner_observes_turn_completion_while_voice_is_disconnected() -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.completed: list[str] = []

        def realtime_turn_completed(self, turn_id: str) -> None:
            self.completed.append(turn_id)

    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", FakeSharedConnection()),
        working_directory=Path.cwd(),
    )
    coordinator = Coordinator()
    owner._conversation_thread_id = "thr_test"  # noqa: SLF001
    owner._active_turn_id = "turn-1"  # noqa: SLF001
    owner._coordinator = cast("InteractionCoordinator", coordinator)  # noqa: SLF001
    interaction = make_broker(file_change_patch_contract(agent_events=True))
    interaction.bind_turn_terminal(owner._realtime_turn_terminal)  # noqa: SLF001
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)

    registrar.notification[0](
        RpcNotification(
            "turn/completed",
            {
                "threadId": "thr_test",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
    )

    active_turn_id: object = owner._active_turn_id  # noqa: SLF001
    assert active_turn_id is None
    assert coordinator.completed == ["turn-1"]
    assert not owner._owns_active_turn("thr_test", "turn-1")  # noqa: SLF001
    interaction.close()


@pytest.mark.asyncio
async def test_browser_close_withdraws_review_before_blocked_effect_settlement(  # noqa: PLR0915
) -> None:
    shared_connection = FakeSharedConnection()
    interaction_broker = make_broker()
    reviewer_slot = web_app._ReviewerBrokerSlot()  # noqa: SLF001
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", shared_connection),
        working_directory=Path.cwd(),
        reviewer_slot=reviewer_slot,
    )
    pending_counts: list[int] = []

    def pending_count_changed(count: int) -> None:
        pending_counts.append(count)
        owner._review_count_changed(count)  # noqa: SLF001

    owner._broker = interaction_broker  # noqa: SLF001
    owner._reviewer_bound = True  # noqa: SLF001
    owner._started = True  # noqa: SLF001
    interaction_broker.bind_pending_count_changed(pending_count_changed)
    reviewer = interaction_broker.connect_reviewer()
    reviewer_slot.bind(interaction_broker)
    review_task, envelope = await published(interaction_broker, reviewer)
    assert pending_counts == [1]

    effect_started = asyncio.Event()
    effect_cancelled = asyncio.Event()
    effect_release = asyncio.Event()

    async def blocked_effect() -> None:
        effect_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            effect_cancelled.set()
            await effect_release.wait()
            raise

    browser = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", owner),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    browser._session = cast("RealtimeSession", owner)  # noqa: SLF001
    effect = browser._spawn_effect(blocked_effect(), name="test-blocked-effect")  # noqa: SLF001
    assert effect is not None
    await effect_started.wait()
    close_task = asyncio.create_task(browser.close())

    try:
        await asyncio.wait_for(effect_cancelled.wait(), timeout=1)
        assert not close_task.done()
        assert pending_counts == [1, 0]
        assert await asyncio.wait_for(anext(reviewer), timeout=1) == ReviewWithdrawal(
            envelope.handle
        )
        for decision in ApprovalDecision:
            with pytest.raises(CodexReviewError, match="handle"):
                interaction_broker.decide(reviewer, envelope.handle, decision)

        late_review = asyncio.create_task(
            interaction_broker.review(command_request("review-after-close-claim"))
        )
        with pytest.raises(CodexReviewError):
            await late_review
        assert pending_counts == [1, 0, 1, 0]
        unread = asyncio.create_task(anext(reviewer))
        ready, _ = await asyncio.wait({unread}, timeout=0.01)
        assert ready == set()
        unread.cancel()
        await asyncio.gather(unread, return_exceptions=True)
    finally:
        effect_release.set()
        await asyncio.gather(close_task, return_exceptions=True)
        if not review_task.done():
            review_task.cancel()
        await asyncio.gather(review_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_connection_terminal_broker_first_still_invalidates_before_unknown_result(  # noqa: C901
) -> None:
    class TerminalRegistrar:
        def __init__(self) -> None:
            self.terminal_callbacks: list[Callable[[], None]] = []

        def register_notification_observer(self, _observer: object) -> None:
            return None

        def register_terminal_callback(self, callback: Callable[[], None]) -> None:
            self.terminal_callbacks.append(callback)

        def register_server_request_handler(self, _method: str, _handler: object) -> None:
            return None

        def terminate(self) -> None:
            for callback in tuple(self.terminal_callbacks):
                callback()

        async def close(self) -> None:
            return None

    class SpeechOrderEffects(FakeInteractionEffects):
        def __init__(self) -> None:
            self.events: list[str] = []

        def on_turn_terminal_claimed(self) -> None:
            self.events.append("invalidate")

        def on_turn_finished(self, result: object) -> None:
            assert result == TurnResult(
                final_answer=None,
                error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN,
            )
            self.events.append("unknown-summary")

    agent_connection = FakeSharedConnection()
    agent = make_session(agent_connection)
    registrar = TerminalRegistrar()
    interaction_broker = make_broker()
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", registrar),
        working_directory=Path.cwd(),
    )
    downstream = SpeechOrderEffects()
    coordinator = InteractionCoordinator(
        agent,
        steer_available=False,
        effects=web_app._ConversationEffects(  # noqa: SLF001
            owner,
            downstream,
        ),
    )
    owner._broker = interaction_broker  # noqa: SLF001
    owner._agent = agent  # noqa: SLF001
    owner._coordinator = coordinator  # noqa: SLF001
    owner._started = True  # noqa: SLF001
    interaction_broker.register_approval_handlers(registrar)
    registrar.register_terminal_callback(owner._connection_terminal)  # noqa: SLF001
    assert registrar.terminal_callbacks == [
        interaction_broker.connection_lost,
        owner._connection_terminal,  # noqa: SLF001
    ]

    coordinator.connection_changed(ConnectionState.READY)
    await begin_real_coordinator_turn(coordinator, agent_connection, "first")
    await settle()
    assert downstream.events == []

    registrar.terminate()

    assert downstream.events == ["invalidate", "unknown-summary"]
    await owner.close()


@pytest.mark.asyncio
async def test_pre_coordinator_terminal_publishes_connection_lost_and_aborts_start(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Probe:
        async def probe(self) -> object:
            events.append("probe")
            return object()

    class Connection:
        def __init__(self) -> None:
            self.terminal_callbacks: list[Callable[[], None]] = []
            self.close_calls = 0

        def register_notification_observer(self, _observer: object) -> None:
            return None

        def register_terminal_callback(self, callback: Callable[[], None]) -> None:
            self.terminal_callbacks.append(callback)

        def register_server_request_handler(self, _method: str, _handler: object) -> None:
            return None

        async def start(self) -> None:
            events.append("connection.start")
            for callback in tuple(self.terminal_callbacks):
                callback()

        async def close(self) -> None:
            self.close_calls += 1
            events.append("connection.close")

    class Broker:
        instance: Broker | None = None

        def __init__(self, _contract: object) -> None:
            type(self).instance = self
            self.close_calls = 0

        def register_approval_handlers(self, registrar: Connection) -> None:
            registrar.register_notification_observer(object())
            registrar.register_terminal_callback(self.connection_lost)
            registrar.register_server_request_handler("approval", object())

        def bind_turn_terminal(self, _callback: object) -> None:
            return None

        def connection_lost(self) -> None:
            events.append("broker.connection_lost")

        def close(self) -> None:
            self.close_calls += 1
            events.append("broker.close")

    class Discovery:
        def __init__(self, _rpc: object, **_kwargs: object) -> None:
            events.append("discovery.construct")

    class Voice:
        thread_id = "thr_test"

        def __init__(self, _rpc: object, **_kwargs: object) -> None:
            events.append("voice.construct")

    class RecordingBrowserConnection(web_app._BrowserConnection):  # noqa: SLF001
        observed_snapshots: list[InteractionSnapshot]

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            self.observed_snapshots.append(snapshot)
            super().on_snapshot_changed(snapshot)

    monkeypatch.setattr(web_app, "InteractionBroker", Broker)
    monkeypatch.setattr(web_app, "CapabilityDiscovery", Discovery)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", Voice)
    connection_owner = Connection()
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", connection_owner),
        contract_probe=cast("CodexSchemaProbe", Probe()),
        reviewer_slot=web_app._ReviewerBrokerSlot(),  # noqa: SLF001
        working_directory=Path.cwd(),
    )
    websocket = CapturingWebSocket()
    synthesizer = FakeSynthesizer()
    browser = RecordingBrowserConnection(
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", owner),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    browser.observed_snapshots = []

    await browser._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    await asyncio.sleep(0)

    disconnected = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    assert browser.observed_snapshots == [disconnected]
    states = [message["state"] for message in websocket.messages if message["type"] == "state"]
    assert states[-1] == "connection_lost"
    assert {"type": "error", "code": "conversation_start_failed"} in websocket.messages
    assert "discovery.construct" not in events
    assert "voice.construct" not in events
    assert connection_owner.close_calls == 1
    broker = cast("Broker", Broker.instance)
    assert broker.close_calls == 1
    assert synthesizer.closed


def test_pre_coordinator_terminal_contains_effect_failure_once_without_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = RuntimeError("private terminal effect detail")

    class Effects(FakeInteractionEffects):
        def __init__(self) -> None:
            self.snapshots: list[InteractionSnapshot] = []

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            self.snapshots.append(snapshot)
            raise error

    effects = Effects()
    connection = OwnerConnection()
    owner = make_owner(
        tmp_path,
        connection,
        OwnerDiscovery(make_codex_snapshot(), connection.event_log),
    )
    owner.bind_effects(effects)
    caplog.set_level(logging.WARNING, logger=web_app.logger.name)

    owner._connection_terminal()  # noqa: SLF001
    owner._connection_terminal()  # noqa: SLF001

    assert effects.snapshots == [
        InteractionSnapshot(
            connection=ConnectionState.DISCONNECTED,
            voice=VoiceState.IDLE,
            task=TaskState.NONE,
            speech=SpeechState.SILENT,
        )
    ]
    assert "RuntimeError" in caplog.text
    assert "private terminal effect detail" not in caplog.text


def test_terminal_callback_during_claimed_close_does_not_reclassify_unpublished_failure(
    tmp_path: Path,
) -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.connection_losses = 0

        def connection_lost(self) -> None:
            self.connection_losses += 1

    class Effects(FakeInteractionEffects):
        def __init__(self) -> None:
            self.snapshots: list[InteractionSnapshot] = []

        def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None:
            self.snapshots.append(snapshot)

    connection = OwnerConnection()
    owner = make_owner(
        tmp_path,
        connection,
        OwnerDiscovery(make_codex_snapshot(), connection.event_log),
    )
    coordinator = Coordinator()
    effects = Effects()
    owner.bind_effects(effects)
    owner._coordinator = cast("InteractionCoordinator", coordinator)  # noqa: SLF001
    owner._closing = True  # noqa: SLF001

    owner._connection_terminal()  # noqa: SLF001

    assert coordinator.connection_losses == 0
    assert effects.snapshots == []


@pytest.mark.asyncio
async def test_connection_terminal_background_close_retrieves_error_for_later_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = OwnerConnection()
    cleanup_error = OwnerConnectionCleanupError("private terminal cleanup failure")
    connection.close_error = cleanup_error
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    voice = OwnerVoice(connection.event_log)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    await owner.start("offer-sdp")
    caplog.set_level(logging.WARNING, logger=web_app.logger.name)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unretrieved: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    background: asyncio.Task[None] | None = None

    try:
        owner._connection_terminal()  # noqa: SLF001
        background = owner._connection_loss_close_task  # noqa: SLF001
        assert background is not None
        await asyncio.wait({background})
        await asyncio.sleep(0)

        with pytest.raises(OwnerConnectionCleanupError) as later:
            await owner.close()

        assert later.value is cleanup_error
        assert owner._connection_loss_close_task is None  # noqa: SLF001
        assert unretrieved == []
        assert "OwnerConnectionCleanupError" in caplog.text
        assert "private terminal cleanup failure" not in caplog.text
        assert voice.close_calls == 1
        assert connection.close_calls == 1
    finally:
        loop.set_exception_handler(previous_handler)
        if background is not None and background.done():
            with suppress(BaseException):
                background.exception()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_phase", ["initial", "replacement"])
async def test_terminal_before_voice_start_success_never_publishes_dead_voice(  # noqa: C901, PLR0915
    monkeypatch: pytest.MonkeyPatch,
    terminal_phase: str,
) -> None:
    events: list[str] = []
    snapshot = make_codex_snapshot()
    contract = SimpleNamespace(approval_profiles={})

    class Probe:
        async def probe(self) -> object:
            return contract

    class Connection:
        def __init__(self) -> None:
            self.terminal_callbacks: list[Callable[[], None]] = []
            self.close_calls = 0

        def register_notification_observer(self, _observer: object) -> None:
            return None

        def register_terminal_callback(self, callback: Callable[[], None]) -> None:
            self.terminal_callbacks.append(callback)

        def register_server_request_handler(self, _method: str, _handler: object) -> None:
            return None

        async def start(self) -> None:
            events.append("connection.start")

        async def close(self) -> None:
            self.close_calls += 1
            events.append("connection.close")

        def terminate(self) -> None:
            events.append("connection.terminal")
            for callback in tuple(self.terminal_callbacks):
                callback()

    connection_owner = Connection()

    class Broker:
        instance: Broker | None = None

        def __init__(self, value: object) -> None:
            assert value is contract
            type(self).instance = self
            self.close_calls = 0

        def register_approval_handlers(self, registrar: Connection) -> None:
            registrar.register_notification_observer(object())
            registrar.register_terminal_callback(self.connection_lost)
            registrar.register_server_request_handler("approval", object())

        def connection_lost(self) -> None:
            events.append("broker.connection_lost")

        def bind_pending_count_changed(self, _callback: object) -> None:
            return None

        def bind_active_turn_check(self, _callback: object) -> None:
            return None

        def bind_turn_terminal(self, _callback: object) -> None:
            return None

        def cancel_pending(self) -> None:
            events.append("broker.withdraw")

        def close(self) -> None:
            self.close_calls += 1
            events.append("broker.close")

    class Discovery:
        def __init__(self, _rpc: object, **kwargs: object) -> None:
            assert kwargs["contract"] is contract

        async def discover(self) -> CapabilitySnapshot:
            return snapshot

    class Agent:
        reusable = True

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def start_turn(self, _text: str) -> str:
            return "unreachable"

        async def steer(self, _text: str) -> None:
            return None

        def owns_active_turn(self, _thread_id: str, _turn_id: str) -> bool:
            return True

        async def close(self) -> None:
            self.reusable = False
            events.append("agent.close")

    class Voice(OwnerVoice):
        def __init__(self, *, terminal_on_start: bool) -> None:
            super().__init__(events)
            self.terminal_on_start = terminal_on_start

        async def start(self, sdp: str) -> str:
            answer = await super().start(sdp)
            if self.terminal_on_start:
                connection_owner.terminate()
            return answer

    voices = [
        Voice(terminal_on_start=terminal_phase == "initial"),
        Voice(terminal_on_start=terminal_phase == "replacement"),
    ]

    class Slot:
        def __init__(self) -> None:
            self.bound: object | None = None
            self.bind_calls = 0
            self.release_calls = 0

        def bind(self, broker: object) -> None:
            assert self.bound is None
            self.bound = broker
            self.bind_calls += 1
            events.append("slot.bind")

        def release(self, broker: object) -> None:
            assert self.bound is broker
            self.bound = None
            self.release_calls += 1
            events.append("slot.release")

    class RecordingBrowserConnection(web_app._BrowserConnection):  # noqa: SLF001
        observed_snapshots: list[InteractionSnapshot]

        def on_snapshot_changed(self, value: InteractionSnapshot) -> None:
            self.observed_snapshots.append(value)
            super().on_snapshot_changed(value)

    monkeypatch.setattr(web_app, "InteractionBroker", Broker)
    monkeypatch.setattr(web_app, "CapabilityDiscovery", Discovery)
    monkeypatch.setattr(web_app, "AgentSession", Agent)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voices.pop(0))
    slot = Slot()
    owner = web_app._CodexConversationOwner(  # noqa: SLF001
        MocoSettings(),
        connection=cast("CodexConnectionSupervisor", connection_owner),
        contract_probe=cast("CodexSchemaProbe", Probe()),
        reviewer_slot=cast("web_app._ReviewerBrokerSlot", slot),  # noqa: SLF001
        working_directory=Path.cwd(),
    )
    websocket = CapturingWebSocket()
    browser = RecordingBrowserConnection(
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", owner),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    browser.observed_snapshots = []

    try:
        if terminal_phase == "replacement":
            await browser._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
            await browser._handle_voice_loss(owner, 1)  # noqa: SLF001
        published_answers = len(
            [message for message in websocket.messages if message["type"] == "sdp_answer"]
        )
        snapshot_index = len(browser.observed_snapshots)
        dead_voice = voices[0] if voices else None

        await browser._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
        for _ in range(20):
            await asyncio.sleep(0)
            if owner.closed and browser._session is None:  # noqa: SLF001
                break

        answers = [message for message in websocket.messages if message["type"] == "sdp_answer"]
        assert len(answers) == published_answers
        assert dead_voice is not None
        assert dead_voice.close_calls == 1
        assert owner.closed
        assert not owner.voice_active
        assert browser._session is None  # noqa: SLF001
        assert slot.bound is None
        assert slot.bind_calls == (0 if terminal_phase == "initial" else 1)
        assert slot.release_calls == (0 if terminal_phase == "initial" else 1)
        assert all(
            value.connection not in {ConnectionState.READY, ConnectionState.DEGRADED}
            for value in browser.observed_snapshots[snapshot_index:]
        )
        states = [message["state"] for message in websocket.messages if message["type"] == "state"]
        assert states[-1] == "connection_lost"
        assert not browser._voice_reconnect_required  # noqa: SLF001
        assert connection_owner.close_calls == 1
        broker = cast("Broker", Broker.instance)
        assert broker.close_calls == 1
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_terminal_after_voice_commit_before_browser_resume_does_not_send_sdp() -> None:
    disconnected = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.effects: InteractionEffects | None = None
            self.voice_active = True

        def bind_effects(self, effects: InteractionEffects) -> None:
            self.effects = effects

        async def start(self, sdp: str) -> str:
            answer = await super().start(sdp)
            self.voice_active = False
            effects = self.effects
            assert effects is not None
            effects.on_snapshot_changed(disconnected)
            return answer

    session = Session()
    websocket = CapturingWebSocket()
    browser = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    await browser._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    for _ in range(10):
        await asyncio.sleep(0)
        if session.closed:
            break

    assert [message for message in websocket.messages if message["type"] == "sdp_answer"] == []
    assert session.closed
    assert browser._session is None  # noqa: SLF001
    states = [message["state"] for message in websocket.messages if message["type"] == "state"]
    assert states[-1] == "connection_lost"


@pytest.mark.parametrize("required", ["agent_admission", "realtime", "interrupt"])
def test_conversation_readiness_rejects_missing_required_capability(required: str) -> None:
    unavailable = CapabilityState(CapabilityStatus.VERSION_MISMATCH, "missing")
    base = make_codex_snapshot()
    if required == "agent_admission":
        snapshot = replace(base, agent_admission=unavailable)
    elif required == "realtime":
        snapshot = replace(base, realtime=unavailable)
    else:
        snapshot = replace(base, interrupt=unavailable)

    with pytest.raises(CodexRpcError, match="required Codex capability"):
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={}),
            snapshot,
        )


def test_conversation_readiness_degrades_when_only_steer_is_unavailable() -> None:
    unavailable = CapabilityState(CapabilityStatus.VERSION_MISMATCH, "missing")
    snapshot = replace(make_codex_snapshot(), steer=unavailable)

    assert (
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={}),
            snapshot,
        )
        is ConnectionState.DEGRADED
    )


def test_conversation_readiness_rejects_unsafe_inherited_policy() -> None:
    snapshot = replace(
        make_codex_snapshot(),
        effective_policy=EffectivePolicy(
            sandbox=SandboxMode.DANGER_FULL_ACCESS,
            approval=ApprovalMode.NEVER,
        ),
    )

    with pytest.raises(CodexRpcError, match="required Codex capability"):
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={}),
            snapshot,
            AgentProfileMode.INHERIT_CODEX,
        )


def test_conversation_readiness_rejects_unknown_inherited_policy() -> None:
    snapshot = replace(make_codex_snapshot(), effective_policy=None)

    with pytest.raises(CodexRpcError, match="required Codex capability"):
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={}),
            snapshot,
            AgentProfileMode.INHERIT_CODEX,
        )


def test_conversation_readiness_allows_explicit_profile_with_unsafe_global_policy() -> None:
    snapshot = replace(
        make_codex_snapshot(),
        effective_policy=EffectivePolicy(
            sandbox=SandboxMode.DANGER_FULL_ACCESS,
            approval=ApprovalMode.NEVER,
        ),
    )

    assert (
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={}),
            snapshot,
            AgentProfileMode.READ_ONLY,
        )
        is ConnectionState.READY
    )


@pytest.mark.parametrize(
    "degrade_snapshot",
    [
        lambda snapshot, unavailable: replace(snapshot, account=unavailable),
        lambda snapshot, _unavailable: replace(snapshot, effective_policy=None),
        lambda snapshot, unavailable: replace(snapshot, policy_state=unavailable),
        lambda snapshot, unavailable: replace(snapshot, managed_requirements=unavailable),
        lambda snapshot, unavailable: replace(snapshot, steer=unavailable),
        lambda snapshot, unavailable: replace(snapshot, server_requests=unavailable),
        lambda snapshot, _unavailable: replace(
            snapshot,
            has_unclassified_server_requests=True,
        ),
    ],
    ids=[
        "account",
        "effective_policy",
        "policy_state",
        "managed_requirements",
        "steer",
        "server_requests",
        "has_unclassified_server_requests",
    ],
)
def test_conversation_readiness_degrades_for_each_unavailable_optional_axis(
    degrade_snapshot: Callable[[CapabilitySnapshot, CapabilityState], CapabilitySnapshot],
) -> None:
    unavailable = CapabilityState(CapabilityStatus.VERSION_MISMATCH, "missing")
    snapshot = degrade_snapshot(make_codex_snapshot(), unavailable)
    legacy = SimpleNamespace(
        category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
        changes_member="changes",
    )

    assert (
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(
                approval_profiles={"legacy": legacy},
                file_change_patch_profile=None,
            ),
            snapshot,
        )
        is ConnectionState.DEGRADED
    )


def test_conversation_readiness_degrades_without_modern_patch_evidence() -> None:
    modern = SimpleNamespace(
        category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
        changes_member=None,
    )
    legacy = SimpleNamespace(
        category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
        changes_member="changes",
    )

    assert (
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={"modern": modern}, file_change_patch_profile=None),
            make_codex_snapshot(),
        )
        is ConnectionState.DEGRADED
    )

    assert (
        web_app._conversation_readiness(  # noqa: SLF001
            SimpleNamespace(approval_profiles={"legacy": legacy}, file_change_patch_profile=None),
            make_codex_snapshot(),
        )
        is ConnectionState.READY
    )


def test_reviewer_broker_slot_binds_and_releases_only_the_same_identity() -> None:
    slot = web_app._ReviewerBrokerSlot()  # noqa: SLF001
    connection = object()

    class Broker:
        def connect_reviewer(self) -> object:
            return connection

        def disconnect_reviewer(self, value: object) -> None:
            assert value is connection

        def decide(self, value: object, handle: str, decision: object) -> None:
            assert value is connection
            assert handle == "opaque-handle"
            assert decision is not None

    broker = Broker()
    other = Broker()
    with pytest.raises(CodexReviewError, match="unavailable"):
        slot.connect_reviewer()

    slot.bind(cast("ReviewerBroker", broker))
    assert slot.connect_reviewer() is connection
    slot.release(cast("ReviewerBroker", other))
    assert slot.connect_reviewer() is connection
    with pytest.raises(CodexReviewError, match="unavailable"):
        slot.bind(cast("ReviewerBroker", other))

    slot.release(cast("ReviewerBroker", broker))
    with pytest.raises(CodexReviewError, match="unavailable"):
        slot.connect_reviewer()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"idle_expired": True, "connection_lost": True}, LifecycleState.IDLE_EXPIRED),
        ({"connection_lost": True, "connecting": True}, LifecycleState.CONNECTION_LOST),
        ({"connecting": True, "voice_reconnect_required": True}, LifecycleState.CONNECTING),
        (
            {"voice_reconnect_required": True, "snapshot_voice": VoiceState.LISTENING},
            LifecycleState.VOICE_RECONNECT_REQUIRED,
        ),
        ({"snapshot_voice": VoiceState.LISTENING}, LifecycleState.LISTENING),
        ({"snapshot_voice": VoiceState.TRANSCRIBING}, LifecycleState.TRANSCRIBING),
        ({"snapshot_task": TaskState.WAITING_REVIEW}, LifecycleState.WAITING_FOR_LOCAL_REVIEW),
        ({"snapshot_speech": SpeechState.PLAYING}, LifecycleState.SPEAKING),
        ({}, LifecycleState.READY),
    ],
)
def test_ui_state_projection_has_one_privacy_safe_priority(
    overrides: dict[str, object],
    expected: LifecycleState,
) -> None:
    snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=cast("VoiceState", overrides.get("snapshot_voice", VoiceState.IDLE)),
        task=cast("TaskState", overrides.get("snapshot_task", TaskState.NONE)),
        speech=cast("SpeechState", overrides.get("snapshot_speech", SpeechState.SILENT)),
    )

    state = web_app._project_ui_state(  # noqa: SLF001
        snapshot,
        idle_expired=overrides.get("idle_expired") is True,
        connection_lost=overrides.get("connection_lost") is True,
        connecting=overrides.get("connecting") is True,
        voice_reconnect_required=overrides.get("voice_reconnect_required") is True,
    )

    assert state is expected


def test_codex_session_factory_composes_one_resolved_connection_and_deferred_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    working_directory = tmp_path / "workspace"
    configured = (str(executable), "--profile", "voice")
    settings = MocoSettings(
        codex=CodexSettings(
            command=configured,
            working_directory=working_directory,
        ),
    )
    resolved_command = object()
    connection = object()
    probe = object()
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def record(name: str, result: object) -> Callable[..., object]:
        def build(*args: object, **kwargs: object) -> object:
            calls.append((name, args, kwargs))
            return result

        return build

    monkeypatch.setattr(web_app, "resolve_codex_command", record("resolve", resolved_command))
    monkeypatch.setattr(web_app, "CodexConnectionSupervisor", record("connection", connection))
    monkeypatch.setattr(web_app, "CodexSchemaProbe", record("probe", probe))

    result = web_app._codex_session_factory(settings)()  # noqa: SLF001

    assert isinstance(result, web_app._CodexConversationOwner)  # noqa: SLF001
    assert result._connection is connection  # noqa: SLF001
    assert result._contract_probe is probe  # noqa: SLF001
    assert calls == [
        ("resolve", (configured,), {}),
        ("connection", (resolved_command,), {}),
        ("probe", (resolved_command,), {}),
    ]


def test_codex_session_factory_resolves_default_working_directory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd_calls = 0

    def current_directory() -> Path:
        nonlocal cwd_calls
        cwd_calls += 1
        return tmp_path

    monkeypatch.setattr(Path, "cwd", staticmethod(current_directory))
    monkeypatch.setattr(web_app, "resolve_codex_command", lambda _configured: object())
    monkeypatch.setattr(web_app, "CodexConnectionSupervisor", lambda _command: object())
    monkeypatch.setattr(web_app, "CodexSchemaProbe", lambda _command: object())
    monkeypatch.setattr(
        web_app,
        "CapabilityDiscovery",
        lambda _connection, **_kwargs: object(),
    )

    web_app._codex_session_factory(MocoSettings())()  # noqa: SLF001

    assert cwd_calls == 1


@pytest.mark.asyncio
async def test_private_owner_starts_voice_after_one_discovery_and_closes_in_order(
    tmp_path: Path,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    owner = make_owner(tmp_path, connection, discovery)

    assert await owner.start("offer-sdp") == "answer-sdp"
    assert discovery.calls == 1
    assert event_log[:5] == [
        "connection.start",
        "discovery.discover",
        "notifications",
        "thread/start",
        "thread/realtime/start",
    ]

    await owner.close()
    await owner.close()

    assert event_log[-2:] == ["thread/realtime/stop", "connection.close"]
    assert connection.close_calls == 1
    assert owner.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connection", "discovery", "voice"])
async def test_owner_start_failure_cleans_created_resources_and_preserves_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    caplog.set_level(logging.WARNING, logger=web_app.logger.name)
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    primary = OwnerPrimaryError(f"private primary {phase}")
    connection.close_error = OwnerConnectionCleanupError("private connection cleanup")

    if phase == "connection":
        connection.start_error = primary
    elif phase == "discovery":
        discovery.error = primary
    else:
        voice.start_error = primary
        voice.close_error = OwnerVoiceCleanupError("private voice cleanup")

    monkeypatch.setattr(
        web_app,
        "CodexRealtimeSession",
        lambda *_args, **_kwargs: voice,
    )
    owner = make_owner(tmp_path, connection, discovery)

    with pytest.raises(OwnerPrimaryError) as caught:
        await owner.start("offer-sdp")

    assert caught.value is primary
    assert owner.closed
    assert connection.close_calls == 1
    assert voice.close_calls == (1 if phase == "voice" else 0)
    assert discovery.calls == (0 if phase == "connection" else 1)
    assert "OwnerConnectionCleanupError" in caplog.text
    assert "private connection cleanup" not in caplog.text
    if phase == "voice":
        assert "OwnerVoiceCleanupError" in caplog.text
        assert "private voice cleanup" not in caplog.text
    with pytest.raises(CodexRpcError, match="already been started"):
        await owner.start("offer-sdp")


@pytest.mark.asyncio
async def test_owner_sdp_failure_cleans_voice_then_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = OwnerConnection()
    connection.emit_sdp = False
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    owner = make_owner(tmp_path, connection, discovery)
    real_voice = CodexRealtimeSession

    def build_voice(
        connection_value: object,
        *,
        contract: CodexProtocolContract,
        settings: MocoSettings,
        capabilities: CapabilitySnapshot,
        working_directory: Path,
        prompt: str,
    ) -> object:
        return real_voice(
            cast("CodexConnection", connection_value),
            contract=contract,
            settings=settings,
            capabilities=capabilities,
            working_directory=working_directory,
            prompt=prompt,
            sdp_timeout=0.01,
        )

    monkeypatch.setattr(web_app, "CodexRealtimeSession", build_voice)

    with pytest.raises(CodexRpcTimeoutError, match="thread/realtime/sdp"):
        await owner.start("offer-sdp")

    assert owner.closed
    assert connection.close_calls == 1
    assert connection.event_log[-2:] == ["thread/realtime/stop", "connection.close"]


@pytest.mark.asyncio
async def test_owner_close_cancels_real_voice_start_waiting_for_sdp(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    private_offer = "private-owner-offer-sdp"
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    connection.emit_sdp = False
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    owner = make_owner(tmp_path, connection, discovery)
    start_task = asyncio.create_task(owner.start(private_offer))
    close_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(connection.realtime_start_started.wait(), 0.5)
        close_task = asyncio.create_task(owner.close())
        await asyncio.sleep(0.05)
        assert close_task.done()
        with pytest.raises((asyncio.CancelledError, CodexRpcError)):
            await start_task
        await close_task
    finally:
        if close_task is not None and not close_task.done():
            startup_task = owner._startup_task  # noqa: SLF001
            if startup_task is not None and not startup_task.done():
                startup_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        if not start_task.done():
            await asyncio.gather(start_task, return_exceptions=True)

    assert owner.closed
    assert connection.closed
    assert connection.event_log.count("thread/realtime/stop") == 1
    assert connection.event_log.count("connection.close") == 1
    assert private_offer not in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.asyncio
async def test_owner_close_drains_discovery_failure_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=web_app.logger.name)
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    await owner.start("offer-sdp")

    discovery_started = asyncio.Event()
    discovery_release = asyncio.Event()

    async def discovery_boom() -> CapabilitySnapshot:
        discovery_started.set()
        await discovery_release.wait()
        message = f"private discovery failure path={tmp_path}"
        raise OwnerDiscoveryError(message)

    owner._discovery_task = asyncio.create_task(  # noqa: SLF001
        discovery_boom(),
        name="moco-test-private-discovery-failure",
    )
    await discovery_started.wait()
    close_task = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    discovery_release.set()
    await close_task

    assert owner.closed
    assert voice.closed
    assert connection.closed
    assert voice.close_calls == 1
    assert connection.close_calls == 1
    assert event_log.index("voice.close") < event_log.index("connection.close")
    assert "OwnerDiscoveryError" in caplog.text
    assert "codex_discovery_drain" in caplog.text
    assert (
        caplog.messages.count(
            "Boundary failure (boundary=codex_discovery_drain, error_type=OwnerDiscoveryError)",
        )
        == 1
    )
    assert "private discovery failure" not in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.asyncio
async def test_owner_close_preserves_voice_primary_and_logs_secondary_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=web_app.logger.name)
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    owner_error = OwnerVoiceCleanupError("private voice close")
    connection_error = OwnerConnectionCleanupError("private connection close")
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    await owner.start("offer-sdp")
    voice.close_error = owner_error
    connection.close_error = connection_error

    with pytest.raises(OwnerVoiceCleanupError) as caught:
        await owner.close()
    with pytest.raises(OwnerVoiceCleanupError) as later:
        await owner.close()

    assert caught.value is owner_error
    assert later.value is owner_error
    assert owner.closed
    assert voice.close_calls == 1
    assert connection.close_calls == 1
    assert "OwnerConnectionCleanupError" in caplog.text
    assert "private voice close" not in caplog.text
    assert "private connection close" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connection", "discovery", "voice", "sdp"])
async def test_owner_start_cancellation_cleans_every_created_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    if phase == "connection":
        connection.start_release = asyncio.Event()
    elif phase == "discovery":
        discovery.release = asyncio.Event()
    elif phase == "voice":
        voice.start_release = asyncio.Event()
        monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    else:
        connection.emit_sdp = False
    owner = make_owner(tmp_path, connection, discovery)
    start_task = asyncio.create_task(owner.start("offer-sdp"))

    if phase == "connection":
        await asyncio.wait_for(connection.start_started.wait(), 0.5)
    elif phase == "discovery":
        await asyncio.wait_for(discovery.started.wait(), 0.5)
    elif phase == "voice":
        await asyncio.wait_for(voice.start_started.wait(), 0.5)
    else:
        await asyncio.wait_for(connection.realtime_start_started.wait(), 0.5)
    start_task.cancel()

    if phase == "discovery":
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(start_task), 0.05)
        assert connection.close_calls == 0
        assert discovery.release is not None
        discovery.release.set()

    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert owner.closed
    assert connection.close_calls == 1
    if phase == "voice":
        assert voice.close_calls == 1
    elif phase in {"connection", "discovery"}:
        assert voice.close_calls == 0
    else:
        assert "thread/realtime/stop" in event_log
    assert discovery.calls == (0 if phase == "connection" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["voice", "connection"])
async def test_owner_close_waits_through_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    await owner.start("offer-sdp")

    release = asyncio.Event()
    if phase == "voice":
        voice.close_release = release
        awaitable_started = voice.close_started
    else:
        connection.close_release = release
        awaitable_started = connection.close_started
    close_task = asyncio.create_task(owner.close())
    await asyncio.wait_for(awaitable_started.wait(), 0.5)

    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    close_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert owner.closed
    assert voice.close_calls == 1
    assert connection.close_calls == 1
    assert event_log.index("voice.close") < event_log.index("connection.close")
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["child-first", "caller-first"])
async def test_owner_drain_task_preserves_first_caller_cancellation_when_child_cancels_same_turn(
    order: str,
) -> None:
    owner = object.__new__(web_app._CodexConversationOwner)  # noqa: SLF001
    child_task = asyncio.create_task(asyncio.Event().wait())
    drain_task = asyncio.create_task(
        owner._drain_task(  # noqa: SLF001
            child_task,
            "codex_start_drain",
            ignore_task_cancellation=True,
        ),
    )
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    if order == "child-first":
        loop.call_soon(child_task.cancel, "close-owned child cancellation")
        loop.call_soon(drain_task.cancel, "first caller cancellation")
    else:
        loop.call_soon(drain_task.cancel, "first caller cancellation")
        loop.call_soon(child_task.cancel, "close-owned child cancellation")

    caller_cancellation = await drain_task

    assert caller_cancellation is not None
    assert caller_cancellation.args == ("first caller cancellation",)


@pytest.mark.asyncio
async def test_owner_drain_task_does_not_report_intentional_child_cancellation() -> None:
    owner = object.__new__(web_app._CodexConversationOwner)  # noqa: SLF001
    child_task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    child_task.cancel("close-owned child cancellation")

    caller_cancellation = await owner._drain_task(  # noqa: SLF001
        child_task,
        "codex_start_drain",
        ignore_task_cancellation=True,
    )

    assert caller_cancellation is None


@pytest.mark.asyncio
async def test_owner_close_gates_blocked_voice_start_and_hides_unready_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    voice.start_release = asyncio.Event()
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    start_task = asyncio.create_task(owner.start("offer-sdp"))

    try:
        await asyncio.wait_for(voice.start_started.wait(), 0.5)
        with pytest.raises(RuntimeError, match="not been started"):
            owner.notifications()

        close_task = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert not close_task.done()

        voice.start_release.set()
        with pytest.raises(CodexRpcError, match="closed"):
            await start_task
        await close_task
    finally:
        voice.start_release.set()
        await asyncio.gather(start_task, return_exceptions=True)

    assert owner.closed
    assert not owner.voice_active
    assert connection.close_calls == 1
    assert voice.close_calls == 1
    with pytest.raises(RuntimeError, match="not been started"):
        owner.notifications()


@pytest.mark.asyncio
async def test_owner_start_cancellation_drains_blocked_discovery_before_connection_close(
    tmp_path: Path,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    discovery.release = asyncio.Event()
    owner = make_owner(tmp_path, connection, discovery)
    start_task = asyncio.create_task(owner.start("offer-sdp"))

    try:
        await asyncio.wait_for(discovery.started.wait(), 0.5)
        start_task.cancel()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(start_task), 0.05)
        assert connection.close_calls == 0

        discovery.release.set()
        with pytest.raises(asyncio.CancelledError):
            await start_task
    finally:
        discovery.release.set()
        await asyncio.gather(start_task, return_exceptions=True)

    assert discovery.calls == 1
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_owner_close_preserves_caller_cancellation_during_blocked_discovery(
    tmp_path: Path,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    discovery.release = asyncio.Event()
    owner = make_owner(tmp_path, connection, discovery)
    start_task = asyncio.create_task(owner.start("offer-sdp"))
    close_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(discovery.started.wait(), 0.5)
        close_task = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        assert connection.close_calls == 0

        close_task.cancel("first caller cancellation")
        await asyncio.sleep(0)
        assert not close_task.done()
        close_task.cancel("second caller cancellation")
        await asyncio.sleep(0)
        assert not close_task.done()
        assert connection.close_calls == 0

        discovery.release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await close_task
        assert caught.value.args == ("first caller cancellation",)
        with pytest.raises((asyncio.CancelledError, CodexRpcError)):
            await start_task
    finally:
        discovery.release.set()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await asyncio.gather(start_task, return_exceptions=True)

    assert owner.closed
    assert connection.closed
    assert connection.close_calls == 1
    assert event_log.count("connection.close") == 1


@pytest.mark.asyncio
async def test_owner_close_preserves_caller_cancellation_during_blocked_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = BlockingStartupCancellationVoice(event_log)
    voice.start_release = asyncio.Event()
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    start_task = asyncio.create_task(owner.start("offer-sdp"))
    close_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(voice.start_started.wait(), 0.5)
        close_task = asyncio.create_task(owner.close())
        await asyncio.wait_for(voice.cancellation_started.wait(), 0.5)
        assert not close_task.done()
        assert connection.close_calls == 0

        close_task.cancel("first caller cancellation")
        await asyncio.sleep(0)
        assert not close_task.done()
        close_task.cancel("second caller cancellation")
        await asyncio.sleep(0)
        assert not close_task.done()
        assert connection.close_calls == 0

        voice.cancellation_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await close_task
        assert caught.value.args == ("first caller cancellation",)
        with pytest.raises((asyncio.CancelledError, CodexRpcError)):
            await start_task
    finally:
        voice.cancellation_release.set()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await asyncio.gather(start_task, return_exceptions=True)

    assert owner.closed
    assert connection.closed
    assert voice.closed
    assert voice.close_calls == 1
    assert connection.close_calls == 1
    assert event_log.count("voice.close") == 1
    assert event_log.count("connection.close") == 1


@pytest.mark.asyncio
async def test_owner_close_callers_share_cleanup_and_later_retrieve_its_error(
    tmp_path: Path,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    owner = make_owner(tmp_path, connection, discovery)
    await owner.start("offer-sdp")
    connection.close_release = asyncio.Event()
    cleanup_error = OwnerConnectionCleanupError("private shared close error")
    connection.close_error = cleanup_error
    first = asyncio.create_task(owner.close())
    await connection.close_started.wait()
    first.cancel("first caller cancellation")
    second = asyncio.create_task(owner.close())
    await asyncio.sleep(0)

    connection.close_release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await first
    with pytest.raises(OwnerConnectionCleanupError) as caught:
        await second
    with pytest.raises(OwnerConnectionCleanupError) as later:
        await owner.close()

    assert cancelled.value.args == ("first caller cancellation",)
    assert caught.value is cleanup_error
    assert later.value is cleanup_error
    assert connection.close_calls == 1
    assert event_log.count("thread/realtime/stop") == 1


@pytest.mark.asyncio
async def test_owner_rejects_invalid_prompt_before_connection_and_discovery(
    tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "invalid-prompt.md"
    prompt_file.write_bytes(b"\xff")
    settings = MocoSettings(
        codex=CodexSettings(
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    owner = _VoiceOnlyConversationOwner(
        settings,
        connection=cast("CodexConnectionSupervisor", connection),
        capability_discovery=cast("CapabilityDiscovery", discovery),
        working_directory=tmp_path,
    )

    with pytest.raises(CodexPromptError, match="UTF-8"):
        await owner.start("offer-sdp")

    assert connection.start_calls == 0
    assert discovery.calls == 0
    assert not any(method.startswith("thread/") for method in event_log)


@pytest.mark.asyncio
async def test_owner_passes_one_preflighted_prompt_to_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("first owner persona", encoding="utf-8")
    settings = MocoSettings(
        codex=CodexSettings(
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    connection.start_release = asyncio.Event()
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voice = OwnerVoice(event_log)
    captured_prompt: str | None = None

    def build_voice(*_args: object, **kwargs: object) -> OwnerVoice:
        nonlocal captured_prompt
        captured_prompt = cast("str", kwargs["prompt"])
        return voice

    monkeypatch.setattr(web_app, "CodexRealtimeSession", build_voice)
    owner = _VoiceOnlyConversationOwner(
        settings,
        connection=cast("CodexConnectionSupervisor", connection),
        capability_discovery=cast("CapabilityDiscovery", discovery),
        working_directory=tmp_path,
    )
    start_task = asyncio.create_task(owner.start("offer-sdp"))

    try:
        await asyncio.wait_for(connection.start_started.wait(), 0.5)
        prompt_file.write_text("second owner persona", encoding="utf-8")
        connection.start_release.set()
        assert await start_task == "answer-sdp"
    finally:
        connection.start_release.set()
        await asyncio.gather(start_task, return_exceptions=True)

    assert captured_prompt == "first owner persona"
    await owner.close()


@pytest.mark.asyncio
async def test_owner_reoffers_only_inactive_voice_and_rejects_stale_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_log: list[str] = []
    connection = OwnerConnection(event_log)
    discovery = OwnerDiscovery(make_codex_snapshot(), event_log)
    voices = [ReofferVoice("answer-1"), ReofferVoice("answer-2")]
    voice_kwargs: list[dict[str, object]] = []

    def voice_factory(*_args: object, **kwargs: object) -> ReofferVoice:
        voice_kwargs.append(kwargs)
        return voices.pop(0)

    monkeypatch.setattr(web_app, "CodexRealtimeSession", voice_factory)
    owner = make_owner(tmp_path, connection, discovery)
    coordinator = VoiceLossCoordinator()

    assert await owner.start("offer-1") == "answer-1"
    owner._coordinator = cast("InteractionCoordinator", coordinator)  # noqa: SLF001
    first_voice = cast("ReofferVoice", owner._voice)  # noqa: SLF001
    assert owner.voice_generation == 1
    first_stream = owner.notifications(1)
    assert first_stream is not first_voice.stream
    await cast("AsyncGenerator[RealtimeEvent]", first_stream).aclose()
    assert not await owner.close_voice(2, on_claimed=lambda: None)
    assert await owner.close_voice(1, on_claimed=lambda: None)
    assert first_voice.close_calls == 1
    assert coordinator.voice_losses == 1
    with pytest.raises(RuntimeError, match="generation"):
        owner.notifications(1)

    assert await owner.replace_voice("offer-2") == "answer-2"
    second_voice = cast("ReofferVoice", owner._voice)  # noqa: SLF001
    assert owner.voice_generation == 2
    assert "existing_thread_id" not in voice_kwargs[0]
    assert voice_kwargs[1]["existing_thread_id"] == "thr_test"
    second_stream = owner.notifications(2)
    assert second_stream is not second_voice.stream
    await cast("AsyncGenerator[RealtimeEvent]", second_stream).aclose()
    assert not await owner.close_voice(1, on_claimed=lambda: None)

    await owner.close()


@pytest.mark.asyncio
async def test_owner_retains_active_turn_across_voice_replacement(tmp_path: Path) -> None:
    connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    owner = make_owner(tmp_path, connection, discovery)

    assert await owner.start("offer-sdp") == "answer-sdp"
    first_events = owner.notifications(1)
    await connection.emit(
        "turn/started",
        {"threadId": "owner-thread", "turn": {"id": "turn-1"}},
    )
    assert await anext(first_events) == ActivityEvent(
        "turn",
        "started",
        "owner-thread",
        "turn-1",
        None,
    )
    assert owner._owns_active_turn("owner-thread", "turn-1")  # noqa: SLF001

    assert await owner.close_voice(1, on_claimed=lambda: None)
    assert owner._owns_active_turn("owner-thread", "turn-1")  # noqa: SLF001
    assert await owner.replace_voice("replacement-offer") == "answer-sdp"
    assert owner._owns_active_turn("owner-thread", "turn-1")  # noqa: SLF001
    assert await owner.cancel_turn()
    assert connection.event_log.count("turn/interrupt") == 1

    replacement_events = owner.notifications(2)
    await connection.emit(
        "turn/completed",
        {
            "threadId": "owner-thread",
            "turn": {"id": "turn-1", "status": "completed"},
        },
    )
    assert await anext(replacement_events) == ActivityEvent(
        "turn",
        "completed",
        "owner-thread",
        "turn-1",
        None,
    )
    assert not owner._owns_active_turn("owner-thread", "turn-1")  # noqa: SLF001
    await owner.close()


@pytest.mark.asyncio
async def test_terminal_observer_cannot_be_overwritten_by_stale_voice_on_reoffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    voices = [ReofferVoice("answer-1"), ReofferVoice("answer-2")]
    voice_kwargs: list[dict[str, object]] = []

    def voice_factory(*_args: object, **kwargs: object) -> ReofferVoice:
        voice_kwargs.append(kwargs)
        return voices.pop(0)

    monkeypatch.setattr(web_app, "CodexRealtimeSession", voice_factory)
    owner = make_owner(tmp_path, connection, discovery)
    assert await owner.start("offer-1") == "answer-1"
    first_voice = cast("ReofferVoice", owner._voice)  # noqa: SLF001
    first_voice.active_turn_id = "turn-1"
    owner._active_turn_id = "turn-1"  # noqa: SLF001

    owner._realtime_turn_terminal("thr_test", "turn-1")  # noqa: SLF001
    assert not owner._owns_active_turn("thr_test", "turn-1")  # noqa: SLF001
    assert not await owner.cancel_turn()
    assert await owner.close_voice(1, on_claimed=lambda: None)
    assert await owner.replace_voice("offer-2") == "answer-2"

    assert voice_kwargs[1]["existing_active_turn_id"] is None
    assert not owner._owns_active_turn("thr_test", "turn-1")  # noqa: SLF001
    await owner.close()


@pytest.mark.asyncio
async def test_terminal_observer_suppresses_a_delayed_started_event(tmp_path: Path) -> None:
    connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    owner = make_owner(tmp_path, connection, discovery)

    assert await owner.start("offer-sdp") == "answer-sdp"
    events = owner.notifications(1)
    await connection.emit(
        "turn/started",
        {"threadId": "owner-thread", "turn": {"id": "turn-1"}},
    )
    await connection.emit(
        "turn/completed",
        {
            "threadId": "owner-thread",
            "turn": {"id": "turn-1", "status": "completed"},
        },
    )
    owner._realtime_turn_terminal("owner-thread", "turn-1")  # noqa: SLF001

    assert await anext(events) == ActivityEvent(
        "turn",
        "completed",
        "owner-thread",
        "turn-1",
        None,
    )
    assert not owner._owns_active_turn("owner-thread", "turn-1")  # noqa: SLF001
    await owner.close()


@pytest.mark.asyncio
async def test_terminal_observer_remembers_multiple_completed_turns_during_backlog(
    tmp_path: Path,
) -> None:
    connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    owner = make_owner(tmp_path, connection, discovery)

    assert await owner.start("offer-sdp") == "answer-sdp"
    events = owner.notifications(1)
    for turn_id in ("turn-1", "turn-2"):
        await connection.emit(
            "turn/started",
            {"threadId": "owner-thread", "turn": {"id": turn_id}},
        )
        await connection.emit(
            "turn/completed",
            {
                "threadId": "owner-thread",
                "turn": {"id": turn_id, "status": "completed"},
            },
        )
        owner._realtime_turn_terminal("owner-thread", turn_id)  # noqa: SLF001

    assert [await anext(events), await anext(events)] == [
        ActivityEvent("turn", "completed", "owner-thread", "turn-1", None),
        ActivityEvent("turn", "completed", "owner-thread", "turn-2", None),
    ]
    await owner.close()


@pytest.mark.asyncio
async def test_owner_voice_loss_callback_failure_still_settles_claimed_voice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class VoiceLossCallbackError(RuntimeError):
        pass

    connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    voice = ReofferVoice("answer-1")
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voice)
    owner = make_owner(tmp_path, connection, discovery)
    coordinator = VoiceLossCoordinator()
    callback_calls = 0

    def fail_after_claim() -> None:
        nonlocal callback_calls
        callback_calls += 1
        raise VoiceLossCallbackError

    await owner.start("offer-1")
    owner._coordinator = cast("InteractionCoordinator", coordinator)  # noqa: SLF001

    with pytest.raises(VoiceLossCallbackError):
        await owner.close_voice(1, on_claimed=fail_after_claim)

    assert callback_calls == 1
    assert voice.close_calls == 1
    assert coordinator.voice_losses == 1
    assert not owner.voice_active
    await owner.close()


@pytest.mark.asyncio
async def test_owner_close_does_not_close_voice_during_replacement_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), connection.event_log)
    initial_voice = ReofferVoice("answer-1")
    replacement_voice = BlockingReofferVoice("answer-2")
    voices = [initial_voice, replacement_voice]
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voices.pop(0))
    owner = make_owner(tmp_path, connection, discovery)
    await owner.start("offer-1")
    assert await owner.close_voice(1, on_claimed=lambda: None)

    replace_task = asyncio.create_task(owner.replace_voice("offer-2"))
    await replacement_voice.start_started.wait()
    close_task = asyncio.create_task(owner.close())
    await asyncio.sleep(0.01)

    assert not replacement_voice.close_started.is_set()
    assert not close_task.done()

    replacement_voice.start_release.set()
    with pytest.raises(CodexRpcError, match="closed"):
        await replace_task
    await close_task
    assert replacement_voice.close_calls == 1


class ReofferVoice:
    thread_id = "thr_test"
    active_turn_id: str | None = None

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.offers: list[str] = []
        self.close_calls = 0

        async def events() -> AsyncIterator[RealtimeEvent]:
            while True:
                await asyncio.Event().wait()
                yield cast("RealtimeEvent", None)

        self.stream = events()

    async def start(self, sdp: str) -> str:
        self.offers.append(sdp)
        return self.answer

    def notifications(self) -> AsyncIterator[RealtimeEvent]:
        return self.stream

    def owns_active_turn(self, thread_id: str, turn_id: str) -> bool:
        return self.thread_id == thread_id and self.active_turn_id == turn_id

    async def close(self) -> None:
        self.close_calls += 1


class BlockingReofferVoice(ReofferVoice):
    def __init__(self, answer: str) -> None:
        super().__init__(answer)
        self.start_started = asyncio.Event()
        self.start_release = asyncio.Event()
        self.close_started = asyncio.Event()

    async def start(self, sdp: str) -> str:
        self.start_started.set()
        await self.start_release.wait()
        return await super().start(sdp)

    async def close(self) -> None:
        self.close_started.set()
        await super().close()


class VoiceLossCoordinator:
    def __init__(self) -> None:
        self.voice_losses = 0

    def voice_lost(self) -> None:
        self.voice_losses += 1


def make_capabilities(
    count: int,
    *,
    default_index: int | None = 0,
    generation: str = "fixture-generation-0",
    ready: bool = True,
    readiness: Readiness | None = None,
    emoji_supported: bool = True,
) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        generation=generation,
        ready=ready,
        readiness=readiness or ("ready" if ready else "model_loading"),
        conditioning=ConditioningCapabilities(
            emoji=EmojiCapability(supported=emoji_supported),
        ),
        voices=tuple(
            VoiceCapability(
                id=f"fixture-id-{index}",
                label=f"Fixture label {index}",
                aliases=(f"fixture-alias-{index}",),
                default=index == default_index,
            )
            for index in range(count)
        ),
    )


def make_dynamic_capabilities(*, max_chars: int = 300) -> IrodoriCapabilities:
    payload = make_capabilities(2).model_dump(mode="python")
    payload["conditioning"] = {
        "delivery_caption": {"supported": True, "max_chars": max_chars},
        "emoji": {"supported": True},
    }
    return IrodoriCapabilities.model_validate(payload, strict=True)


def browser_voice(capabilities: CapabilitiesResponse, selected: str | None) -> dict[str, object]:
    return {
        "selected": selected,
        "options": [
            {"id": voice.id, "label": voice.label, "default": voice.default}
            for voice in capabilities.voices
        ],
        "ready": capabilities.ready,
        "readiness": capabilities.readiness,
    }


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.start_calls = 0
        self.voice_active = True
        self.voice_generation = 1
        self.active_turn_id: str | None = None
        self.interaction_snapshot: InteractionSnapshot | None = None
        self.cancel_results: list[bool] = []
        self.cancel_calls = 0
        self._events: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()

    async def start(self, sdp: str) -> str:
        assert sdp == "offer-sdp"
        self.start_calls += 1
        if self.interaction_snapshot is None:
            self.interaction_snapshot = InteractionSnapshot(
                connection=ConnectionState.READY,
                voice=VoiceState.IDLE,
                task=TaskState.NONE,
                speech=SpeechState.SILENT,
            )
        return "answer-sdp"

    async def replace_voice(self, sdp: str) -> str:
        answer = await self.start(sdp)
        self.voice_generation += 1
        self.voice_active = True
        return answer

    async def close_voice(
        self,
        expected_generation: int,
        *,
        on_claimed: Callable[[], None],
    ) -> bool:
        if expected_generation != self.voice_generation or not self.voice_active:
            return False
        self.voice_active = False
        on_claimed()
        return True

    def bind_effects(self, _effects: InteractionEffects) -> None:
        return None

    async def notifications(
        self,
        _expected_generation: int | None = None,
    ) -> AsyncIterator[RealtimeEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    async def emit(self, event: RealtimeEvent) -> None:
        await self._events.put(event)

    async def close(self) -> None:
        self.closed = True
        self.voice_active = False
        await self._events.put(None)

    def claim_close(self) -> None:
        return None

    async def cancel_turn(self) -> bool:
        self.cancel_calls += 1
        return self.cancel_results.pop(0) if self.cancel_results else False

    def listen_started(self) -> None:
        snapshot = cast("InteractionSnapshot", self.interaction_snapshot)
        self.interaction_snapshot = replace(snapshot, voice=VoiceState.LISTENING)

    def listen_stopped(self) -> None:
        snapshot = cast("InteractionSnapshot", self.interaction_snapshot)
        self.interaction_snapshot = replace(snapshot, voice=VoiceState.IDLE)

    async def consume_user_final(
        self,
        _text: str,
        *,
        utterance_id: int | None = None,
    ) -> None:
        del utterance_id

    def speech_changed(self, state: SpeechState) -> None:
        snapshot = self.interaction_snapshot
        if snapshot is not None:
            self.interaction_snapshot = replace(snapshot, speech=state)


class EffectsSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.effects: InteractionEffects | None = None

    def bind_effects(self, effects: InteractionEffects) -> None:
        self.effects = effects


_OWNER_QUEUE_END = object()


def make_codex_snapshot() -> CapabilitySnapshot:
    available = CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    return CapabilitySnapshot(
        version="owner-fixture",
        account=available,
        effective_policy=EffectivePolicy(
            sandbox=SandboxMode.WORKSPACE_WRITE,
            approval=ApprovalMode.ON_REQUEST,
        ),
        policy_state=available,
        managed_requirements=available,
        agent_admission=available,
        realtime=available,
        interrupt=available,
        steer=available,
        server_requests=available,
        server_request_categories=frozenset(),
        has_unclassified_server_requests=False,
    )


class OwnerConnection:
    def __init__(self, event_log: list[str] | None = None) -> None:
        self.event_log = event_log if event_log is not None else []
        self.start_calls = 0
        self.close_calls = 0
        self.closed = False
        self.start_error: BaseException | None = None
        self.start_started = asyncio.Event()
        self.start_release: asyncio.Event | None = None
        self.fail_method: str | None = None
        self.fail_error: BaseException = CodexRpcError("owner request failed")
        self.emit_sdp = True
        self.realtime_start_started = asyncio.Event()
        self.close_error: BaseException | None = None
        self.close_started = asyncio.Event()
        self.close_release: asyncio.Event | None = None
        self._notifications: asyncio.Queue[RpcNotification | object] = asyncio.Queue()

    async def start(self) -> None:
        self.start_calls += 1
        self.event_log.append("connection.start")
        self.start_started.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.start_error is not None:
            raise self.start_error

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        del params, request_timeout
        self.event_log.append(method)
        if method == self.fail_method:
            raise self.fail_error
        if method == "thread/start":
            return {"thread": {"id": "owner-thread"}}
        if method == "thread/realtime/start":
            self.realtime_start_started.set()
            if self.emit_sdp:
                await self.emit(
                    "thread/realtime/sdp",
                    {"threadId": "owner-thread", "sdp": "answer-sdp"},
                )
            return {}
        if method == "thread/realtime/stop":
            return {}
        if method == "turn/interrupt":
            return {}
        message = f"unexpected owner request: {method}"
        raise AssertionError(message)

    def notifications(self) -> AsyncIterator[RpcNotification]:
        self.event_log.append("notifications")
        return self._notification_stream()

    async def _notification_stream(self) -> AsyncIterator[RpcNotification]:
        while True:
            item = await self._notifications.get()
            if item is _OWNER_QUEUE_END:
                return
            yield cast("RpcNotification", item)

    async def emit(self, method: str, params: dict[str, JsonValue]) -> None:
        await self._notifications.put(RpcNotification(method=method, params=params))

    async def close(self) -> None:
        self.close_calls += 1
        self.event_log.append("connection.close")
        self.close_started.set()
        if self.close_release is not None:
            await self.close_release.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
        await self._notifications.put(_OWNER_QUEUE_END)


class OwnerDiscovery:
    def __init__(
        self,
        snapshot: CapabilitySnapshot,
        event_log: list[str],
    ) -> None:
        self.snapshot = snapshot
        self.event_log = event_log
        self.calls = 0
        self.error: BaseException | None = None
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def discover(self) -> CapabilitySnapshot:
        self.calls += 1
        self.event_log.append("discovery.discover")
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.snapshot


class OwnerVoice:
    thread_id = "thr_test"
    active_turn_id: str | None = None

    def __init__(self, event_log: list[str]) -> None:
        self.event_log = event_log
        self.start_calls = 0
        self.close_calls = 0
        self.closed = False
        self.start_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.start_started = asyncio.Event()
        self.start_release: asyncio.Event | None = None
        self.close_started = asyncio.Event()
        self.close_release: asyncio.Event | None = None

    async def start(self, sdp: str) -> str:
        assert sdp == "offer-sdp"
        self.start_calls += 1
        self.event_log.append("voice.start")
        self.start_started.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.start_error is not None:
            raise self.start_error
        return "answer-sdp"

    def notifications(self) -> AsyncIterator[RealtimeEvent]:
        async def stream() -> AsyncIterator[RealtimeEvent]:
            while True:
                await asyncio.Event().wait()
                yield cast("RealtimeEvent", None)

        return stream()

    async def close(self) -> None:
        self.close_calls += 1
        self.event_log.append("voice.close")
        self.close_started.set()
        if self.close_release is not None:
            await self.close_release.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class BlockingStartupCancellationVoice(OwnerVoice):
    def __init__(self, event_log: list[str]) -> None:
        super().__init__(event_log)
        self.cancellation_started = asyncio.Event()
        self.cancellation_release = asyncio.Event()

    async def start(self, sdp: str) -> str:
        try:
            return await super().start(sdp)
        except asyncio.CancelledError:
            self.cancellation_started.set()
            await self.cancellation_release.wait()
            raise


class OwnerPrimaryError(RuntimeError):
    """Synthetic primary owner failure."""


class OwnerDiscoveryError(RuntimeError):
    """Synthetic private discovery failure."""


class OwnerVoiceCleanupError(RuntimeError):
    """Synthetic Voice cleanup failure."""


class OwnerConnectionCleanupError(RuntimeError):
    """Synthetic connection cleanup failure."""


def make_voice_contract() -> CodexProtocolContract:
    return CodexProtocolContract(
        version="codex-fixture",
        methods={
            SemanticMethod.THREAD_START: ClientMethodContract(
                "thread/start",
                ParamsKind.OBJECT,
                frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
            ),
            SemanticMethod.THREAD_REALTIME_START: ClientMethodContract(
                "thread/realtime/start",
                ParamsKind.OBJECT,
                frozenset(
                    {
                        "includeStartupContext",
                        "clientManagedHandoffs",
                        "codexResponseHandoffMode",
                        "codexResponsesAsItems",
                        "delegationAckFiller",
                        "outputModality",
                        "prompt",
                        "threadId",
                        "transport",
                        "version",
                    }
                ),
            ),
            SemanticMethod.TURN_INTERRUPT: ClientMethodContract(
                "turn/interrupt",
                ParamsKind.OBJECT,
                frozenset({"threadId", "turnId"}),
            ),
        },
        server_requests={},
        unclassified_server_request_count=0,
        experimental_schema=True,
    )


class _VoiceOnlyConversationOwner(web_app._CodexConversationOwner):  # noqa: SLF001
    """Minimal Voice composition for owner lifecycle tests."""

    def __init__(
        self,
        settings: MocoSettings,
        *,
        connection: CodexConnectionSupervisor,
        capability_discovery: CapabilityDiscovery,
        working_directory: Path,
    ) -> None:
        super().__init__(
            settings,
            connection=connection,
            working_directory=working_directory,
        )
        self._test_capability_discovery = capability_discovery
        self._test_contract = make_voice_contract()

    async def _start_once(self, sdp: str) -> str:
        prompt = load_realtime_prompt(self._settings)
        await self._connection.start()
        await self._ensure_open()
        discovery_task = asyncio.create_task(
            self._test_capability_discovery.discover(),
            name="moco-test-capability-discovery",
        )
        self._discovery_task = discovery_task
        try:
            capabilities = await asyncio.shield(discovery_task)
        finally:
            if discovery_task.done() and self._discovery_task is discovery_task:
                self._discovery_task = None
        await self._ensure_open()
        self._voice_capabilities = capabilities
        self._voice_contract = self._test_contract
        self._voice_prompt = prompt
        voice_factory = cast(
            "Callable[..., CodexRealtimeSession]",
            web_app.CodexRealtimeSession,  # type: ignore[attr-defined]
        )
        voice = voice_factory(
            self._connection,
            contract=self._test_contract,
            settings=self._settings,
            capabilities=capabilities,
            working_directory=self._working_directory,
            prompt=prompt,
        )
        async with self._state_lock:
            if self._closing or self._closed:
                message = "Codex conversation is closed"
                raise CodexRpcError(message)
            self._starting_voice = voice
        answer = await voice.start(sdp)
        await self._publish_voice(voice)
        return answer


def make_owner(
    tmp_path: Path,
    connection: OwnerConnection,
    discovery: OwnerDiscovery,
) -> web_app._CodexConversationOwner:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("owner prompt", encoding="utf-8")
    return _VoiceOnlyConversationOwner(
        MocoSettings(
            codex=CodexSettings(
                working_directory=tmp_path,
                prompt_file=prompt,
            ),
        ),
        connection=cast("CodexConnectionSupervisor", connection),
        capability_discovery=cast("CapabilityDiscovery", discovery),
        working_directory=tmp_path,
    )


class FakeSynthesizer:
    def __init__(
        self,
        capabilities: CapabilitiesResponse
        | object
        | list[CapabilitiesResponse | object]
        | None = None,
        *,
        synthesis_error: IrodoriError | None = None,
        selection_error: IrodoriError | None = None,
    ) -> None:
        self.closed = False
        default = make_capabilities(2)
        self.capability_responses = (
            capabilities if isinstance(capabilities, list) else [capabilities or default]
        )
        self.capability_calls = 0
        self.selected_voices: list[str] = []
        self.synthesis_error = synthesis_error
        self.selection_error = selection_error
        self.synthesized_texts: list[str] = []
        self.synthesized = asyncio.Event()
        self.second_synthesized = asyncio.Event()

    async def capabilities(self) -> CapabilitiesResponse:
        index = min(self.capability_calls, len(self.capability_responses) - 1)
        self.capability_calls += 1
        response = self.capability_responses[index]
        if isinstance(response, Exception):
            raise response
        return cast("CapabilitiesResponse", response)

    async def synthesize(self, text: str) -> bytes:
        self.synthesized_texts.append(text)
        self.synthesized.set()
        if len(self.synthesized_texts) >= 2:
            self.second_synthesized.set()
        if self.synthesis_error is not None:
            raise self.synthesis_error
        return b"RIFF\x04\x00\x00\x00WAVE"

    def select_voice(self, voice_id: str) -> None:
        if self.selection_error is not None:
            raise self.selection_error
        self.selected_voices.append(voice_id)

    async def close(self) -> None:
        self.closed = True


class CapabilityBoundaryClient:
    def __init__(self, capabilities: CapabilitiesResponse) -> None:
        self.capabilities_response = capabilities
        self.closed = False

    async def capabilities(self) -> CapabilitiesResponse:
        return self.capabilities_response

    async def aclose(self) -> None:
        self.closed = True


class BlockingSynthesizer(FakeSynthesizer):
    def __init__(self, capabilities: CapabilitiesResponse | None = None) -> None:
        super().__init__(capabilities)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(self, text: str) -> bytes:
        del text
        self.started.set()
        await self.release.wait()
        return b"RIFF\x04\x00\x00\x00WAVE"


class StartFailureError(RuntimeError):
    """Synthetic realtime startup failure."""


class InvalidNotificationError(RuntimeError):
    """Synthetic notification-stream failure."""


class FailingSession(FakeSession):
    async def start(self, sdp: str) -> str:
        del sdp
        raise StartFailureError


class ConnectionLostStartSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.effects: InteractionEffects | None = None

    def bind_effects(self, effects: InteractionEffects) -> None:
        self.effects = effects

    async def start(self, sdp: str) -> str:
        del sdp
        snapshot = InteractionSnapshot(
            connection=ConnectionState.DISCONNECTED,
            voice=VoiceState.IDLE,
            task=TaskState.FAILED,
            speech=SpeechState.SILENT,
        )
        self.interaction_snapshot = snapshot
        effects = self.effects
        assert effects is not None
        effects.on_snapshot_changed(snapshot)
        await asyncio.sleep(0)
        raise StartFailureError


class InvalidNotificationSession(FakeSession):
    async def notifications(
        self,
        _expected_generation: int | None = None,
    ) -> AsyncIterator[RealtimeEvent]:
        if self.closed:
            yield RealtimeErrorEvent("thr_test", "unreachable")
        raise InvalidNotificationError


class CapturingWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.byte_messages: list[bytes] = []

    async def send_json(self, message: dict[str, object]) -> None:
        self.messages.append(message)

    async def send_bytes(self, message: bytes) -> None:
        self.byte_messages.append(message)


class GenerationSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.voice_active = True
        self.voice_generation = 1
        self.user_finals: list[str] = []
        self.user_final_started = asyncio.Event()
        self._generation_events: dict[int, asyncio.Queue[RealtimeEvent | None]] = {}

    async def notifications(
        self,
        expected_generation: int | None = None,
    ) -> AsyncIterator[RealtimeEvent]:
        generation = self.voice_generation if expected_generation is None else expected_generation
        queue = self._generation_events.setdefault(generation, asyncio.Queue())
        while (event := await queue.get()) is not None:
            yield event

    async def emit_for_generation(self, generation: int, event: RealtimeEvent) -> None:
        queue = self._generation_events.setdefault(generation, asyncio.Queue())
        await queue.put(event)

    async def end_generation(self, generation: int) -> None:
        queue = self._generation_events.setdefault(generation, asyncio.Queue())
        await queue.put(None)

    def consume_user_final(
        self,
        text: str,
        *,
        utterance_id: int | None = None,
    ) -> Coroutine[Any, Any, None]:
        del utterance_id
        self.user_finals.append(text)
        self.user_final_started.set()

        async def settled() -> None:
            return None

        return settled()


class BlockingJsonWebSocket(CapturingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()
        self.send_release = asyncio.Event()

    async def send_json(self, message: dict[str, object]) -> None:
        self.send_started.set()
        await self.send_release.wait()
        await super().send_json(message)


class RecordingSpeech:
    def __init__(self) -> None:
        self.transcripts: list[tuple[str, str, bool]] = []

    async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
        self.transcripts.append((role, delta, done))


class RecordingSpeechComposition:
    def __init__(self, events: list[object] | None = None) -> None:
        self.events = events if events is not None else []
        self.transcripts: list[tuple[str, str, bool]] = []
        self.invalidations: list[str] = []
        self.is_busy = False

    async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
        self.transcripts.append((role, delta, done))
        self.events.append(("speech", role, delta, done))
        if role == "assistant" and delta:
            self.is_busy = True
        elif role == "user" and done:
            self.events.append("speech.reset")

    async def invalidate(self, *, reason: str) -> None:
        self.invalidations.append(reason)
        self.events.append("speech.invalidate")
        self.is_busy = False

    async def join(self) -> None:
        return None

    async def close(self) -> None:
        self.is_busy = False


class CaptionRecordingSpeech:
    def __init__(self) -> None:
        self.transcripts: list[tuple[str, str, bool, str | None]] = []

    async def on_transcript(
        self,
        *,
        role: str,
        delta: str,
        done: bool,
        delivery_caption: str | None = None,
    ) -> None:
        self.transcripts.append((role, delta, done, delivery_caption))

    async def join(self) -> None:
        return None


def mark_audio_delivered(
    connection: web_app._BrowserConnection,
    audio_id: int,
    generation: int,
) -> None:
    connection._playback_states[(audio_id, generation)] = "delivered"  # noqa: SLF001


async def start_recorded_agent_speech(
    connection: web_app._BrowserConnection,
    text: str,
) -> RecordingSpeechComposition:
    speech = RecordingSpeechComposition()
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection.on_turn_finished(TurnResult(final_answer=text, error_code=None))
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001
    return speech


class AudioDeliveryError(RuntimeError):
    """Synthetic browser audio delivery failure."""

    def __init__(self) -> None:
        super().__init__("private websocket failure detail")


class FailingAudioWebSocket(CapturingWebSocket):
    async def send_bytes(self, message: bytes) -> None:
        del message
        raise AudioDeliveryError


class BlockingAudioWebSocket(CapturingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def send_bytes(self, message: bytes) -> None:
        del message
        self.started.set()
        await asyncio.wait_for(asyncio.Event().wait(), timeout=1)


class GatedAudioWebSocket(CapturingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_bytes(self, message: bytes) -> None:
        self.started.set()
        await self.release.wait()
        self.byte_messages.append(message)


class InvalidationObservingWebSocket(CapturingWebSocket):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.invalidation_sent = asyncio.Event()
        self.fail = fail

    async def send_json(self, message: dict[str, object]) -> None:
        await super().send_json(message)
        if message.get("type") != "audio_invalidate":
            return
        self.invalidation_sent.set()
        if self.fail:
            raise AudioDeliveryError


class CancellationCleanupSynthesizer(FakeSynthesizer):
    def __init__(self) -> None:
        super().__init__()
        self.cancellation_started = asyncio.Event()
        self.release_cancellation = asyncio.Event()

    async def synthesize(self, text: str) -> bytes:
        self.synthesized_texts.append(text)
        self.synthesized.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=1)
        except asyncio.CancelledError:
            self.cancellation_started.set()
            await asyncio.wait_for(self.release_cancellation.wait(), timeout=1)
            raise
        return b""


class RecordingSpeechInvalidation:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def invalidate(self, *, reason: str) -> None:
        self.reasons.append(reason)


def websocket_context(
    client: TestClient,
    *,
    capability: str = CAPABILITY,
    origin: str = "http://127.0.0.1:8765",
    host: str = "127.0.0.1:8765",
) -> WebSocketTestSession:
    return client.websocket_connect(
        "/ws",
        headers={"host": host, "origin": origin},
        subprotocols=["moco", f"moco.capability.{capability}"],
    )


def receive_ready_catalog(socket: WebSocketTestSession) -> dict[str, object]:
    initial = socket.receive_json()
    assert initial["voice"] == {
        "selected": None,
        "options": [],
        "ready": False,
        "readiness": "loading",
    }
    assert initial["conditioning"]["emojiSupported"] is False
    ready = socket.receive_json()
    assert ready["voice"]["ready"] is True
    assert ready["voice"]["readiness"] == "ready"
    return cast("dict[str, object]", ready)


def test_rejects_non_loopback_origin_and_wrong_capability() -> None:
    app = create_app(capability_token=CAPABILITY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for origin, capability in [
            ("https://example.com", CAPABILITY),
            ("http://127.0.0.1:8765", "wrong"),
        ]:
            with (
                pytest.raises(WebSocketDisconnect),
                websocket_context(client, origin=origin, capability=capability),
            ):
                pass


def test_accepts_arbitrary_numeric_loopback_media_origin() -> None:
    app = create_app(capability_token=CAPABILITY)
    with (
        TestClient(app, base_url="http://127.0.0.42:8765") as client,
        websocket_context(
            client,
            origin="http://127.0.0.42:8765",
            host="127.0.0.42:8765",
        ) as socket,
    ):
        assert socket.receive_json()["state"] == "ready"


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://192.0.2.1:8765", "192.0.2.1:8765"),
        ("http://127.0.0.42.evil:8765", "127.0.0.42.evil:8765"),
        ("http://127.0.0.42:8765", "127.0.0.43:8765"),
    ],
)
def test_rejects_non_loopback_or_mismatched_numeric_media_authority(
    origin: str,
    host: str,
) -> None:
    app = create_app(capability_token=CAPABILITY)
    with (
        TestClient(app, base_url="http://127.0.0.42:8765") as client,
        pytest.raises(WebSocketDisconnect),
        websocket_context(client, origin=origin, host=host),
    ):
        pass


@pytest.mark.parametrize(
    "authority",
    [
        "[::ffff:127.0.0.1]:8765",
        "[::ffff:7f00:1]:8765",
        "[::1%lo0]:8765",
        "[::1%25lo0]:8765",
    ],
)
def test_rejects_mapped_or_scoped_media_authority(authority: str) -> None:
    app = create_app(capability_token=CAPABILITY)
    with (
        TestClient(app, base_url="http://127.0.0.42:8765") as client,
        pytest.raises(WebSocketDisconnect),
        websocket_context(
            client,
            origin=f"http://{authority}",
            host=authority,
        ),
    ):
        pass


def test_accepts_exact_configured_public_origin() -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with (
        TestClient(app, base_url="https://voice.example.com") as client,
        websocket_context(
            client,
            origin="https://voice.example.com",
            host="voice.example.com",
        ) as socket,
    ):
        assert socket.receive_json()["state"] == "ready"


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://voice.example.com", "voice.example.com"),
        ("https://evil.example.com", "voice.example.com"),
        ("https://voice.example.com", "evil.example.com"),
        ("https://voice.example.com.evil.test", "voice.example.com.evil.test"),
        ("https://voice.example.com:443", "voice.example.com"),
    ],
)
def test_rejects_public_origin_variants(origin: str, host: str) -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with (
        TestClient(app, base_url="https://voice.example.com") as client,
        pytest.raises(WebSocketDisconnect),
        websocket_context(client, origin=origin, host=host),
    ):
        pass


def test_pairing_svg_is_private_and_not_cached() -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get(
            "/pairing.svg",
            headers={
                "host": "127.0.0.1:8765",
                "x-moco-capability": CAPABILITY,
                "sec-fetch-site": "same-origin",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.content.startswith(b"<svg")


def test_pairing_svg_accepts_arbitrary_numeric_loopback_host() -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with TestClient(app, base_url="http://127.0.0.42:8765") as client:
        response = client.get(
            "/pairing.svg",
            headers={
                "host": "127.0.0.42:8765",
                "x-moco-capability": CAPABILITY,
                "sec-fetch-site": "same-origin",
            },
        )

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["192.0.2.1:8765", "127.0.0.42.evil:8765"])
def test_pairing_svg_rejects_non_loopback_or_hostname_trick(host: str) -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with TestClient(app, base_url="http://127.0.0.42:8765") as client:
        response = client.get(
            "/pairing.svg",
            headers={
                "host": host,
                "x-moco-capability": CAPABILITY,
                "sec-fetch-site": "same-origin",
            },
        )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "host",
    [
        "[::ffff:127.0.0.1]:8765",
        "[::ffff:7f00:1]:8765",
        "[::1%lo0]:8765",
        "[::1%25lo0]:8765",
    ],
)
def test_pairing_svg_rejects_mapped_or_scoped_loopback_host(host: str) -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with TestClient(app, base_url="http://127.0.0.42:8765") as client:
        response = client.get(
            "/pairing.svg",
            headers={
                "host": host,
                "x-moco-capability": CAPABILITY,
                "sec-fetch-site": "same-origin",
            },
        )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "voice.example.com", "x-moco-capability": CAPABILITY},
        {"host": "127.0.0.1:8765"},
        {"host": "127.0.0.1:8765", "x-moco-capability": "wrong"},
        {
            "host": "127.0.0.1:8765",
            "x-moco-capability": CAPABILITY,
            "sec-fetch-site": "cross-site",
        },
    ],
)
def test_pairing_svg_rejects_untrusted_requests(headers: dict[str, str]) -> None:
    settings = MocoSettings(
        server=ServerSettings(public_url="https://voice.example.com"),
    )
    app = create_app(settings, capability_token=CAPABILITY)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/pairing.svg", headers=headers)

    assert response.status_code == 404


def test_connection_projects_only_safe_runtime_voice_capabilities(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capabilities = make_capabilities(3, default_index=1, emoji_supported=False)
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    synthesizers: list[FakeSynthesizer] = []

    def synthesizer_factory() -> WebSynthesizer:
        synthesizer = FakeSynthesizer(capabilities)
        synthesizers.append(synthesizer)
        return cast("WebSynthesizer", synthesizer)

    app = create_app(
        synthesizer_factory=synthesizer_factory,
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        state = receive_ready_catalog(socket)

    assert state["voice"] == browser_voice(capabilities, capabilities.voices[1].id)
    assert state["conditioning"] == {
        "captionMode": "off",
        "deliveryCaptionSupported": False,
        "emojiSupported": False,
    }
    rendered = repr(state)
    assert capabilities.generation not in rendered
    assert all(alias not in rendered for voice in capabilities.voices for alias in voice.aliases)
    assert synthesizers[0].closed
    capability_log = next(
        record.message
        for record in caplog.records
        if "event=irodori_capabilities_received" in record.message
    )
    assert "contract_version=1" in capability_log
    assert "ready=True" in capability_log
    assert "readiness=ready" in capability_log
    assert f"voice_count={len(capabilities.voices)}" in capability_log
    assert capabilities.generation not in capability_log
    assert all(voice.id not in capability_log for voice in capabilities.voices)


def test_auto_mode_projects_dynamic_caption_capability() -> None:
    capabilities = make_dynamic_capabilities(max_chars=300)
    app = create_app(
        MocoSettings(irodori=IrodoriSettings(caption_mode="auto")),
        synthesizer_factory=lambda: cast(
            "WebSynthesizer",
            FakeSynthesizer(capabilities),
        ),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        state = receive_ready_catalog(socket)

    assert state["conditioning"] == {
        "captionMode": "auto",
        "deliveryCaptionSupported": True,
        "emojiSupported": True,
    }


def test_oversized_runtime_catalog_is_rejected_before_browser_projection() -> None:
    capabilities = make_capabilities(_MAX_CAPABILITY_VOICES + 1)
    boundary_client = CapabilityBoundaryClient(capabilities)

    def synthesizer_factory() -> WebSynthesizer:
        synthesizer = IrodoriSynthesizer(
            cast("IrodoriClient", boundary_client),
            settings=MocoSettings(),
        )
        return cast("WebSynthesizer", synthesizer)

    app = create_app(
        synthesizer_factory=synthesizer_factory,
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        initial = socket.receive_json()
        rejected = socket.receive_json()

    assert initial["voice"]["readiness"] == "loading"
    assert rejected["voice"] == {
        "selected": None,
        "options": [],
        "ready": False,
        "readiness": "capability_mismatch",
    }
    assert capabilities.voices[0].id not in repr(rejected)
    assert capabilities.voices[-1].id not in repr(rejected)
    assert boundary_client.closed


@pytest.mark.parametrize(
    "terminal_readiness",
    ["ready", "model_not_loaded", "voice_bank_invalid"],
)
def test_capability_poll_stops_at_ready_or_terminal_readiness(
    terminal_readiness: Readiness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loading = make_capabilities(2, ready=False, readiness="model_loading")
    terminal = make_capabilities(
        2,
        ready=terminal_readiness == "ready",
        readiness=terminal_readiness,
    )
    synthesizer = FakeSynthesizer([loading, terminal])
    monkeypatch.setattr(web_app, "_CAPABILITY_POLL_INTERVAL_SECONDS", 0.001)
    app = create_app(
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        assert socket.receive_json()["voice"]["readiness"] == "loading"
        assert socket.receive_json()["voice"]["readiness"] == "model_loading"
        assert socket.receive_json()["voice"]["readiness"] == terminal_readiness
        time.sleep(0.01)

    assert synthesizer.capability_calls == 2
    assert synthesizer.closed


@pytest.mark.parametrize("selector_kind", ["default", "canonical", "alias"])
def test_cold_start_resolves_voice_when_same_generation_catalog_becomes_populated(
    selector_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loading = make_capabilities(
        0,
        generation="fixture-cold-generation",
        ready=False,
        readiness="model_loading",
    )
    ready = make_capabilities(3, default_index=2, generation=loading.generation)
    selected_voice = ready.voices[2 if selector_kind == "default" else 1]
    selector = {
        "default": None,
        "canonical": selected_voice.id,
        "alias": selected_voice.aliases[0],
    }[selector_kind]
    discovery = FakeSynthesizer([loading, ready])
    active = FakeSynthesizer(ready)
    synthesizers = [discovery, active]
    monkeypatch.setattr(web_app, "_CAPABILITY_POLL_INTERVAL_SECONDS", 0.001)
    app = create_app(
        MocoSettings(irodori=IrodoriSettings(speaker=selector)),
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizers.pop(0)),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        assert socket.receive_json()["voice"]["readiness"] == "loading"
        assert socket.receive_json()["voice"] == browser_voice(loading, None)
        assert socket.receive_json()["voice"] == browser_voice(ready, selected_voice.id)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json()["type"] == "sdp_answer"
        assert socket.receive_json()["state"] == "ready"

    assert active.selected_voices == [selected_voice.id]


def test_socket_close_cancels_model_loading_poll_and_closes_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loading = make_capabilities(2, ready=False, readiness="model_loading")
    synthesizer = FakeSynthesizer(loading)
    monkeypatch.setattr(web_app, "_CAPABILITY_POLL_INTERVAL_SECONDS", 0.001)
    app = create_app(
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        with websocket_context(client) as socket:
            socket.receive_json()
            assert socket.receive_json()["voice"]["readiness"] == "model_loading"
            time.sleep(0.005)
        calls_after_close = synthesizer.capability_calls
        time.sleep(0.005)

    assert synthesizer.closed
    assert synthesizer.capability_calls == calls_after_close


@pytest.mark.parametrize("selector_kind", ["canonical", "alias", "default"])
def test_connection_resolves_configured_id_unique_alias_or_catalog_default(
    selector_kind: str,
) -> None:
    capabilities = make_capabilities(3, default_index=2)
    selected_voice = capabilities.voices[1 if selector_kind != "default" else 2]
    selector = {
        "canonical": selected_voice.id,
        "alias": selected_voice.aliases[0],
        "default": None,
    }[selector_kind]
    app = create_app(
        MocoSettings(irodori=IrodoriSettings(speaker=selector)),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer(capabilities)),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        state = receive_ready_catalog(socket)

    voice = cast("dict[str, object]", state["voice"])
    assert voice["selected"] == selected_voice.id


@pytest.mark.parametrize(
    ("capabilities", "settings", "error_code"),
    [
        (
            make_capabilities(2),
            MocoSettings(irodori=IrodoriSettings(speaker="missing-fixture")),
            "configured_voice_unavailable",
        ),
        (make_capabilities(0), MocoSettings(), "voice_catalog_empty"),
        (
            make_capabilities(2, default_index=None),
            MocoSettings(),
            "voice_selection_required",
        ),
        (
            make_capabilities(2, ready=False, readiness="model_loading"),
            MocoSettings(),
            "model_loading",
        ),
        (
            make_capabilities(2, ready=False, readiness="model_not_loaded"),
            MocoSettings(),
            "model_not_loaded",
        ),
        (
            make_capabilities(2, ready=False, readiness="voice_bank_invalid"),
            MocoSettings(),
            "voice_bank_invalid",
        ),
        (
            make_capabilities(2).model_copy(update={"contract_version": 2}),
            MocoSettings(),
            "capability_mismatch",
        ),
        (
            make_capabilities(2),
            MocoSettings(irodori=IrodoriSettings(caption_mode="auto")),
            "caption_unsupported",
        ),
        (OSError("fixture network failure"), MocoSettings(), "irodori_unavailable"),
    ],
)
def test_start_fails_closed_before_creating_codex_session(
    capabilities: object,
    settings: MocoSettings,
    error_code: str,
) -> None:
    sessions: list[FakeSession] = []

    def session_factory() -> RealtimeSession:
        session = FakeSession()
        sessions.append(session)
        return cast("RealtimeSession", session)

    app = create_app(
        settings,
        session_factory=session_factory,
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer(capabilities)),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {"type": "error", "code": error_code}
        assert socket.receive_json()["state"] == "idle_expired"

    assert sessions == []


def test_immediate_start_uses_fresh_capabilities_before_background_catalog_is_required() -> None:
    capabilities = make_capabilities(2)
    session = FakeSession()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer(capabilities)),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        assert socket.receive_json()["voice"]["readiness"] == "loading"
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        messages = [socket.receive_json() for _ in range(4)]

    assert any(message["type"] == "sdp_answer" for message in messages)
    assert session.start_calls == 1


@pytest.mark.parametrize(
    ("fresh_capabilities", "error_code"),
    [
        (
            make_capabilities(2, generation="fixture-generation-1"),
            "runtime_generation_mismatch",
        ),
        (make_capabilities(1), "voice_not_found"),
    ],
)
def test_start_rejects_cached_generation_or_selection_conflicts(
    fresh_capabilities: CapabilitiesResponse,
    error_code: str,
) -> None:
    cached = make_capabilities(2)
    synthesizers = [FakeSynthesizer(cached), FakeSynthesizer(fresh_capabilities)]
    sessions: list[FakeSession] = []

    def session_factory() -> RealtimeSession:
        session = FakeSession()
        sessions.append(session)
        return cast("RealtimeSession", session)

    app = create_app(
        MocoSettings(irodori=IrodoriSettings(speaker=cached.voices[1].id)),
        session_factory=session_factory,
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizers.pop(0)),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {"type": "error", "code": error_code}
        assert socket.receive_json()["state"] == "idle_expired"

    assert sessions == []


def test_start_stop_listening_and_hotkey_broadcast() -> None:
    sessions: list[FakeSession] = []

    def session_factory() -> RealtimeSession:
        session = FakeSession()
        sessions.append(session)
        return cast("RealtimeSession", session)

    app = create_app(
        session_factory=session_factory,
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        ready = receive_ready_catalog(socket)
        assert ready["state"] == "ready"
        assert ready["hotkeys"] == {
            "enabled": True,
            "startListening": "f1",
            "stopListening": "f2",
        }
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {"type": "sdp_answer", "sdp": "answer-sdp"}
        assert socket.receive_json()["state"] == "ready"

        socket.send_json({"type": "control", "control": "listen_start"})
        assert socket.receive_json()["state"] == "listening"
        assert not sessions[0].closed

        socket.send_json({"type": "control", "control": "listen_stop"})
        assert socket.receive_json()["state"] == "ready"
        assert not sessions[0].closed

        socket.send_json({"type": "control", "control": "listen_start"})
        assert socket.receive_json()["state"] == "listening"

        portal = client.portal
        assert portal is not None
        portal.call(
            app.state.control_hub.publish,
            Control.LISTEN_START,
        )
        assert socket.receive_json() == {
            "type": "control",
            "control": "listen_start",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (TaskState.NONE, False),
        (TaskState.QUEUED, False),
        (TaskState.RUNNING, True),
        (TaskState.WAITING_REVIEW, True),
        (TaskState.COMPLETED, False),
        (TaskState.FAILED, False),
        (TaskState.INTERRUPTED, False),
    ],
)
async def test_server_state_projects_only_privacy_safe_can_cancel(
    task: TaskState,
    expected: bool,
) -> None:
    websocket = CapturingWebSocket()
    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=task,
        speech=SpeechState.SILENT,
    )
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001

    await connection._send_state()  # noqa: SLF001

    assert websocket.messages[0]["canCancel"] is expected
    assert "task" not in websocket.messages[0]
    assert "taskDetail" not in websocket.messages[0]


@pytest.mark.asyncio
async def test_connection_loss_releases_whole_lease_before_next_start() -> None:
    websocket = CapturingWebSocket()
    lost_session = FakeSession()
    lost_session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    replacement_session = FakeSession()
    synthesizers: list[FakeSynthesizer] = []

    def build_synthesizer() -> FakeSynthesizer:
        synthesizer = FakeSynthesizer()
        synthesizers.append(synthesizer)
        return synthesizer

    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", replacement_session),
        synthesizer_factory=lambda: cast("WebSynthesizer", build_synthesizer()),
    )
    connection._session = cast("RealtimeSession", lost_session)  # noqa: SLF001

    connection.on_snapshot_changed(lost_session.interaction_snapshot)
    effects = tuple(connection._effect_tasks)  # noqa: SLF001
    await asyncio.gather(*effects)
    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001

    assert lost_session.closed
    assert replacement_session.start_calls == 1
    await connection._close_conversation_resources()  # noqa: SLF001


@pytest.mark.asyncio
async def test_connection_loss_state_send_failure_still_releases_whole_lease(  # noqa: C901
) -> None:
    class StateSendError(RuntimeError):
        """Synthetic failed state publication."""

        def __init__(self) -> None:
            super().__init__("private websocket failure")

    class FailingStateWebSocket(CapturingWebSocket):
        fail_state = True

        async def send_json(self, message: dict[str, object]) -> None:
            if self.fail_state and message.get("type") == "state":
                raise StateSendError
            await super().send_json(message)

    class CountingSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class CountingSpeech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class CountingSynthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    websocket = FailingStateWebSocket()
    lost_session = CountingSession()
    lost_session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    replacement_session = FakeSession()
    speech = CountingSpeech()
    synthesizer = CountingSynthesizer()
    replacement_synthesizer = FakeSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", replacement_session),
        synthesizer_factory=lambda: cast("WebSynthesizer", replacement_synthesizer),
    )
    connection._session = cast("RealtimeSession", lost_session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001
    unretrieved: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))

    try:
        connection.on_snapshot_changed(lost_session.interaction_snapshot)
        for _ in range(10):
            await asyncio.sleep(0)
            if connection._connection_loss_task is None:  # noqa: SLF001
                break
        gc.collect()
        await asyncio.sleep(0)

        detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
        assert detached is None
        assert lost_session.close_calls == 1
        assert speech.close_calls == 1
        assert synthesizer.close_calls == 1
        assert unretrieved == []

        websocket.fail_state = False
        await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
        assert replacement_session.start_calls == 1
        assert {"type": "error", "code": "already_started"} not in websocket.messages
    finally:
        loop.set_exception_handler(previous_handler)
        await connection.close()


@pytest.mark.asyncio
async def test_browser_close_does_not_wait_on_effect_joining_existing_claim() -> None:
    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    speech = Speech()
    session = Session()
    synthesizer = Synthesizer()
    effect_started = asyncio.Event()
    effect_entered_close = asyncio.Event()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001

    async def close_after_drain_starts() -> None:
        effect_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            effect_entered_close.set()
            await connection.close()

    effect_task = connection._spawn_effect(  # noqa: SLF001
        close_after_drain_starts(),
        name="effect-joining-browser-close",
    )
    assert effect_task is not None
    await effect_started.wait()
    external_close = asyncio.create_task(connection.close())
    await effect_entered_close.wait()

    done, _pending = await asyncio.wait(
        {external_close, effect_task},
        timeout=0.05,
    )

    assert external_close in done
    assert effect_task in done
    await external_close
    await effect_task
    assert speech.close_calls == 1
    assert session.close_calls == 1
    assert synthesizer.close_calls == 1


@pytest.mark.asyncio
async def test_browser_effect_can_create_the_close_claim_without_self_await() -> None:
    session = FakeSession()
    synthesizer = FakeSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001

    async def close_from_effect() -> None:
        await connection.close()

    effect_task = connection._spawn_effect(  # noqa: SLF001
        close_from_effect(),
        name="effect-claiming-browser-close",
    )
    assert effect_task is not None

    await asyncio.wait_for(effect_task, timeout=0.5)
    await connection.close()

    assert session.closed
    assert synthesizer.closed


@pytest.mark.asyncio
async def test_browser_close_rejects_snapshot_effect_created_during_resource_cleanup(  # noqa: C901
) -> None:
    class BlockingStateWebSocket(CapturingWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.send_calls = 0
            self.first_send_started = asyncio.Event()

        async def send_json(self, message: dict[str, object]) -> None:
            del message
            self.send_calls += 1
            if self.send_calls == 1:
                self.first_send_started.set()
            await asyncio.Event().wait()

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.effects: InteractionEffects | None = None
            self.settlement_snapshots = 0

        async def close(self) -> None:
            self.close_calls += 1
            effects = self.effects
            assert effects is not None
            self.settlement_snapshots += 1
            effects.on_snapshot_changed(
                InteractionSnapshot(
                    connection=ConnectionState.READY,
                    voice=VoiceState.IDLE,
                    task=TaskState.INTERRUPTED,
                    speech=SpeechState.SILENT,
                )
            )
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    websocket = BlockingStateWebSocket()
    speech = Speech()
    session = Session()
    synthesizer = Synthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    session.effects = connection
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001
    connection.on_snapshot_changed(
        InteractionSnapshot(
            connection=ConnectionState.READY,
            voice=VoiceState.IDLE,
            task=TaskState.RUNNING,
            speech=SpeechState.SILENT,
        )
    )
    await websocket.first_send_started.wait()

    try:
        await connection.close()
        await asyncio.sleep(0)

        assert websocket.send_calls == 1
        assert session.settlement_snapshots == 1
        assert all(task.done() for task in connection._effect_tasks)  # noqa: SLF001
        assert speech.close_calls == 1
        assert session.close_calls == 1
        assert synthesizer.close_calls == 1
    finally:
        leaked_effects = tuple(connection._effect_tasks)  # noqa: SLF001
        for task in leaked_effects:
            task.cancel()
        await asyncio.gather(*leaked_effects, return_exceptions=True)


@pytest.mark.asyncio
async def test_browser_close_replays_effect_failure_completed_before_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class EffectError(RuntimeError):
        pass

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    error = EffectError("private completed effect failure")

    async def fail_effect() -> None:
        raise error

    session = Session()
    synthesizer = Synthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001
    connection._spawn_effect(fail_effect(), name="failed-before-close")  # noqa: SLF001
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    first, concurrent = await asyncio.gather(
        connection.close(),
        connection.close(),
        return_exceptions=True,
    )
    (later,) = await asyncio.gather(connection.close(), return_exceptions=True)

    assert session.close_calls == 1
    assert synthesizer.close_calls == 1
    assert first is error
    assert concurrent is error
    assert later is error
    assert str(error) not in caplog.text


@pytest.mark.asyncio
async def test_browser_close_uses_first_registered_completed_effect_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class EffectError(RuntimeError):
        pass

    first_error = EffectError("private first registered effect failure")
    later_error = EffectError("private later registered effect failure")
    release_first = asyncio.Event()

    async def succeed() -> None:
        return None

    async def fail_first() -> None:
        await release_first.wait()
        raise first_error

    async def fail_later() -> None:
        raise later_error

    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._spawn_effect(succeed(), name="successful-effect")  # noqa: SLF001
    connection._spawn_effect(fail_first(), name="first-failed-effect")  # noqa: SLF001
    connection._spawn_effect(fail_later(), name="later-failed-effect")  # noqa: SLF001
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    (result,) = await asyncio.gather(connection.close(), return_exceptions=True)

    assert result is first_error
    assert str(first_error) not in caplog.text
    assert str(later_error) not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_child", ["effect", "capability", "idle"])
async def test_browser_close_reclaims_resources_after_failed_child_task(  # noqa: C901, PLR0915
    failed_child: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ChildTaskError(RuntimeError):
        pass

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    error = ChildTaskError(f"private {failed_child} task failure")

    child_started = asyncio.Event()

    async def fail_child() -> None:
        if failed_child == "effect":
            child_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise error from None
        raise error

    speech = Speech()
    session = Session()
    synthesizer = Synthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001
    if failed_child == "effect":
        child_task = connection._spawn_effect(  # noqa: SLF001
            fail_child(),
            name="failed-effect",
        )
        await child_started.wait()
    else:
        child_task = asyncio.create_task(fail_child(), name=f"failed-{failed_child}")
        await asyncio.sleep(0)
    if failed_child == "capability":
        connection._capability_task = child_task  # noqa: SLF001
    elif failed_child == "idle":
        connection._idle_task = child_task  # noqa: SLF001
    unretrieved: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))

    try:
        first, concurrent = await asyncio.gather(
            connection.close(),
            connection.close(),
            return_exceptions=True,
        )
        (later,) = await asyncio.gather(connection.close(), return_exceptions=True)
        del child_task
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert speech.close_calls == 1
    assert session.close_calls == 1
    assert synthesizer.close_calls == 1
    detached = cast(
        "tuple[SpeechQueue | None, RealtimeSession | None, WebSynthesizer | None]",
        (connection._speech, connection._session, connection._synthesizer),  # noqa: SLF001
    )
    assert detached == (None, None, None)
    assert first is error
    assert concurrent is error
    assert later is error
    assert unretrieved == []
    assert str(error) not in caplog.text


@pytest.mark.asyncio
async def test_browser_close_shares_cleanup_through_effect_drain_cancellation() -> None:
    class CancellationDelayingWebSocket(CapturingWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.send_started = asyncio.Event()
            self.cancellation_started = asyncio.Event()
            self.release_cancellation = asyncio.Event()

        async def send_json(self, message: dict[str, object]) -> None:
            del message
            self.send_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_started.set()
                while not self.release_cancellation.is_set():
                    try:
                        await self.release_cancellation.wait()
                    except asyncio.CancelledError:
                        continue
                raise

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    websocket = CancellationDelayingWebSocket()
    session = Session()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    synthesizer = Synthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001

    connection.on_snapshot_changed(session.interaction_snapshot)
    await websocket.send_started.wait()
    first = asyncio.create_task(connection.close())
    await websocket.cancellation_started.wait()
    first.cancel("first Browser close cancellation")
    await asyncio.sleep(0)
    first.cancel("second Browser close cancellation")
    second = asyncio.create_task(connection.close())
    websocket.release_cancellation.set()

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await first
    await second
    await connection.close()

    assert session.close_calls == 1
    assert synthesizer.close_calls == 1
    detached = cast(
        "tuple[RealtimeSession | None, WebSynthesizer | None]",
        (connection._session, connection._synthesizer),  # noqa: SLF001
    )
    assert detached == (None, None)
    assert cancelled.value.args == ("first Browser close cancellation",)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_resource", ["speech", "session", "synthesizer"])
async def test_browser_close_attempts_every_resource_and_replays_first_error(
    failing_resource: str,
) -> None:
    class ResourceCloseError(RuntimeError):
        pass

    error = ResourceCloseError(f"private {failing_resource} close failure")
    events: list[str] = []

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            events.append("speech.close")
            if failing_resource == "speech":
                raise error

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            events.append("session.close")
            if failing_resource == "session":
                raise error
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            events.append("synthesizer.close")
            if failing_resource == "synthesizer":
                raise error
            await super().close()

    speech = Speech()
    session = Session()
    synthesizer = Synthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001

    with pytest.raises(ResourceCloseError) as first:
        await connection.close()
    with pytest.raises(ResourceCloseError) as later:
        await connection.close()

    assert first.value is error
    assert later.value is error
    assert events == ["speech.close", "session.close", "synthesizer.close"]
    assert speech.close_calls == 1
    assert session.close_calls == 1
    assert synthesizer.close_calls == 1
    detached = cast(
        "tuple[SpeechQueue | None, RealtimeSession | None, WebSynthesizer | None]",
        (connection._speech, connection._session, connection._synthesizer),  # noqa: SLF001
    )
    assert detached == (None, None, None)


@pytest.mark.asyncio
async def test_browser_cleanup_callers_share_task_through_repeated_cancellation() -> None:
    events: list[str] = []

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            events.append("speech.close")
            self.started.set()
            await self.release.wait()

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            events.append("session.close")
            await super().close()

    class Synthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            events.append("synthesizer.close")
            await super().close()

    speech = Speech()
    session = Session()
    synthesizer = Synthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = cast("WebSynthesizer", synthesizer)  # noqa: SLF001

    first = asyncio.create_task(connection._close_conversation_resources())  # noqa: SLF001
    await speech.started.wait()
    first.cancel("first Browser cleanup cancellation")
    await asyncio.sleep(0)
    first.cancel("second Browser cleanup cancellation")
    second = asyncio.create_task(connection._close_conversation_resources())  # noqa: SLF001
    speech.release.set()

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await first
    await second

    assert cancelled.value.args == ("first Browser cleanup cancellation",)
    assert events == ["speech.close", "session.close", "synthesizer.close"]
    assert speech.close_calls == 1
    assert session.close_calls == 1
    assert synthesizer.close_calls == 1
    detached = cast(
        "tuple[SpeechQueue | None, RealtimeSession | None, WebSynthesizer | None]",
        (connection._speech, connection._session, connection._synthesizer),  # noqa: SLF001
    )
    assert detached == (None, None, None)


@pytest.mark.asyncio
async def test_browser_cleanup_drains_notification_cancellation_before_resources() -> None:
    cancellation_started = asyncio.Event()
    cancellation_release = asyncio.Event()

    async def notifications() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await cancellation_release.wait()
            raise

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    session = FakeSession()
    speech = Speech()
    synthesizer = FakeSynthesizer()
    notification_task = asyncio.create_task(notifications())
    await asyncio.sleep(0)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._notifications_task = notification_task  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001

    cleanup = asyncio.create_task(connection._close_conversation_resources())  # noqa: SLF001
    await cancellation_started.wait()
    assert speech.close_calls == 0
    assert not session.closed
    assert not synthesizer.closed
    cancellation_release.set()
    await cleanup

    assert notification_task.cancelled()
    assert speech.close_calls == 1
    closed = cast("tuple[bool, bool]", (session.closed, synthesizer.closed))
    assert closed == (True, True)


@pytest.mark.asyncio
async def test_initial_partial_cleanup_attempts_session_and_synth_after_caller_cancellation() -> (
    None
):
    events: list[str] = []

    class Session:
        def __init__(self) -> None:
            self.close_calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            events.append("session.close")
            self.started.set()
            await self.release.wait()

    class Synthesizer:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            events.append("synthesizer.close")

    session = Session()
    synthesizer = Synthesizer()
    cleanup = asyncio.create_task(
        web_app._close_start_resources(  # noqa: SLF001
            cast("RealtimeSession", session),
            cast("WebSynthesizer", synthesizer),
        )
    )
    await session.started.wait()
    cleanup.cancel("first partial cleanup cancellation")
    await asyncio.sleep(0)
    cleanup.cancel("second partial cleanup cancellation")
    session.release.set()

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await cleanup

    assert cancelled.value.args == ("first partial cleanup cancellation",)
    assert events == ["session.close", "synthesizer.close"]
    assert session.close_calls == 1
    assert synthesizer.close_calls == 1


@pytest.mark.asyncio
async def test_voice_replacement_defers_idle_expiry_until_settlement_timeout() -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    class ReplacementSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.voice_active = False
            self.voice_generation = 1
            self.replace_started = asyncio.Event()
            self.replace_release = asyncio.Event()

        async def replace_voice(self, sdp: str) -> str:
            assert sdp == "offer-sdp"
            self.replace_started.set()
            await self.replace_release.wait()
            self.voice_generation += 1
            self.voice_active = True
            return "answer-sdp"

        async def notifications(
            self,
            _expected_generation: int | None = None,
        ) -> AsyncIterator[RealtimeEvent]:
            async for event in super().notifications():
                yield event

    clock = Clock()
    session = ReplacementSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(runtime=RuntimeSettings(idle_timeout_seconds=5)),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._idle_timer = IdleLeaseTimer(  # noqa: SLF001
        idle_timeout_seconds=5,
        clock=clock,
    )
    clock.now = 4.9

    replace_task = asyncio.create_task(connection._start(StartMessage(sdp="offer-sdp")))  # noqa: SLF001
    await session.replace_started.wait()
    clock.now = 50

    assert not connection._idle_timer.claim_expired(  # noqa: SLF001
        is_idle=not connection._connecting  # noqa: SLF001
        and connection._snapshot.idle,  # noqa: SLF001
    )
    assert not session.closed

    session.replace_release.set()
    await replace_task
    assert connection._idle_timer.last_activity == 50  # noqa: SLF001
    clock.now = 54.9
    assert not connection._idle_timer.claim_expired(is_idle=True)  # noqa: SLF001
    clock.now = 55
    assert connection._idle_timer.claim_expired(is_idle=True)  # noqa: SLF001
    await connection._expire_conversation()  # noqa: SLF001
    assert session.closed


@pytest.mark.asyncio
async def test_new_lease_gets_fresh_timer_after_previous_idle_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    timers: list[IdleLeaseTimer] = []

    def build_timer(*, idle_timeout_seconds: float) -> IdleLeaseTimer:
        timer = IdleLeaseTimer(idle_timeout_seconds=idle_timeout_seconds, clock=clock)
        timers.append(timer)
        return timer

    sessions = [FakeSession(), FakeSession()]
    monkeypatch.setattr(web_app, "IdleLeaseTimer", build_timer)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(runtime=RuntimeSettings(idle_timeout_seconds=5)),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", sessions.pop(0)),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    first_session = cast("FakeSession", connection._session)  # noqa: SLF001
    clock.now = 5
    assert connection._idle_timer.claim_expired(is_idle=True)  # noqa: SLF001
    await connection._expire_conversation()  # noqa: SLF001
    assert first_session.closed

    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    second_session = cast("FakeSession", connection._session)  # noqa: SLF001
    clock.now = 10
    second_expired = connection._idle_timer.claim_expired(is_idle=True)  # noqa: SLF001
    if second_expired:
        await connection._expire_conversation()  # noqa: SLF001
    else:
        await connection._close_conversation_resources()  # noqa: SLF001

    assert second_expired
    assert second_session.closed
    assert len(timers) == 3


@pytest.mark.asyncio
async def test_turn_cancel_routes_only_through_conversation_owner_wrapper() -> None:
    websocket = CapturingWebSocket()
    session = FakeSession()
    session.cancel_results = [True]
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001

    await connection._apply_control(ClientControl.TURN_CANCEL)  # noqa: SLF001

    assert session.cancel_calls == 1
    assert websocket.messages == []


@pytest.mark.asyncio
async def test_user_done_invalidates_speech_without_client_managed_handoff() -> None:
    events: list[object] = []

    class Session(FakeSession):
        async def consume_user_final(
            self,
            text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            events.append(("handoff", text))
            await super().consume_user_final(text, utterance_id=utterance_id)

    session = Session()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.LISTENING,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    speech = RecordingSpeechComposition(events)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "user", "done only"),
    )

    assert events == [
        "speech.invalidate",
        ("speech", "user", "", True),
        "speech.reset",
    ]
    assert speech.invalidations == ["user_transcript"]
    assert connection._generation == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_duplicate_user_done_is_presented_once_without_agent_resubmission() -> None:
    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.user_finals: list[tuple[str, int | None]] = []

        async def consume_user_final(
            self,
            text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            self.user_finals.append((text, utterance_id))

    session = Session()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001

    final = TranscriptEvent("done", "thr_test", "user", "同じ依頼")
    await connection._enqueue_transcript(final)  # noqa: SLF001
    await connection._enqueue_transcript(final)  # noqa: SLF001
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "user", "次の依頼"),
    )
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "user", "同"),
    )
    await connection._enqueue_transcript(final)  # noqa: SLF001

    assert session.user_finals == []


@pytest.mark.asyncio
async def test_user_final_does_not_wait_for_obsolete_client_handoff() -> None:
    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.handoff_started = asyncio.Event()
            self.handoff_release = asyncio.Event()

        async def consume_user_final(
            self,
            _text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            del utterance_id
            self.handoff_started.set()
            await self.handoff_release.wait()

    session = Session()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications())  # noqa: SLF001

    await session.emit(TranscriptEvent("done", "thr_test", "user", "first"))
    await session.emit(TranscriptEvent("delta", "thr_test", "user", "next"))
    await asyncio.sleep(0.01)

    assert connection._transcripts == {"user": "next"}  # noqa: SLF001

    assert not session.handoff_started.is_set()
    await session.close()
    await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_automatic_delegation_preserves_outbound_user_transcript_order() -> None:
    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.handoff_started = asyncio.Event()
            self.handoff_release = asyncio.Event()

        async def consume_user_final(
            self,
            _text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            del utterance_id
            self.handoff_started.set()
            await self.handoff_release.wait()

    websocket = CapturingWebSocket()
    session = Session()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications())  # noqa: SLF001

    await session.emit(TranscriptEvent("done", "thr_test", "user", "first"))
    await session.emit(TranscriptEvent("delta", "thr_test", "user", "second"))
    for _ in range(10):
        if len([message for message in websocket.messages if message["type"] == "transcript"]) == 2:
            break
        await asyncio.sleep(0)

    transcripts = [message for message in websocket.messages if message["type"] == "transcript"]
    assert transcripts == [
        {"type": "transcript", "role": "user", "text": "first", "done": True},
        {"type": "transcript", "role": "user", "text": "second", "done": False},
    ]

    assert not session.handoff_started.is_set()
    await session.close()
    await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_fast_agent_final_waits_for_corresponding_user_final_presentation() -> None:
    class BlockingSpeech(RecordingSpeechComposition):
        def __init__(self) -> None:
            super().__init__()
            self.invalidation_started = asyncio.Event()
            self.invalidation_release = asyncio.Event()

        async def invalidate(self, *, reason: str) -> None:
            assert reason == "user_transcript"
            self.invalidation_started.set()
            await self.invalidation_release.wait()

    websocket = CapturingWebSocket()
    speech = BlockingSpeech()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    user_completion = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "user", "first request"),
    )
    await speech.invalidation_started.wait()

    connection.on_turn_finished(TurnResult(final_answer="fast result", error_code=None))
    await asyncio.sleep(0)
    assert [message for message in websocket.messages if message["type"] == "transcript"] == []

    speech.invalidation_release.set()
    await user_completion
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001
    assert [message for message in websocket.messages if message["type"] == "transcript"] == [
        {"type": "transcript", "role": "user", "text": "first request", "done": True},
        {"type": "transcript", "role": "assistant", "text": "fast result", "done": True},
    ]


@pytest.mark.asyncio
async def test_pending_transcript_work_is_bounded() -> None:
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", BlockingJsonWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    for index in range(web_app._MAX_PENDING_TRANSCRIPTS):  # noqa: SLF001
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "user", str(index)),
        )
    with pytest.raises(RuntimeError, match="transcript queue limit"):
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "user", "overflow"),
        )

    assert connection._transcript_queue.qsize() == web_app._MAX_PENDING_TRANSCRIPTS  # noqa: SLF001
    await connection.close()


@pytest.mark.asyncio
async def test_pending_assistant_transcript_work_is_bounded() -> None:
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", BlockingJsonWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    for index in range(web_app._MAX_PENDING_TRANSCRIPTS):  # noqa: SLF001
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "assistant", str(index)),
        )
    with pytest.raises(RuntimeError, match="transcript queue limit"):
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "assistant", "overflow"),
        )

    assistant_queue = connection._assistant_transcript_queue  # noqa: SLF001
    assert assistant_queue.qsize() == web_app._MAX_PENDING_TRANSCRIPTS  # noqa: SLF001
    await connection.close()


@pytest.mark.asyncio
async def test_assistant_transcript_text_is_bounded_by_utf8_bytes() -> None:
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    oversized = "a" * (web_app._MAX_ASSISTANT_TRANSCRIPT_BYTES + 1)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="transcript text limit"):
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("done", "thr_test", "assistant", oversized),
        )

    assert speech.transcripts == []
    await connection.close()


@pytest.mark.asyncio
async def test_assistant_transcript_part_count_is_bounded() -> None:
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    for _ in range(web_app._MAX_ASSISTANT_TRANSCRIPT_PARTS):  # noqa: SLF001
        await connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "assistant", "a"),
        )
    with pytest.raises(RuntimeError, match="transcript part limit"):
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "assistant", "overflow"),
        )

    await connection.close()


@pytest.mark.asyncio
async def test_speech_segment_overflow_closes_voice_without_irodori_work() -> None:
    session = GenerationSession()
    synthesizer = FakeSynthesizer()
    speech = SpeechQueue(synthesizer, deliver=lambda *_args: None, max_chars=80)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = speech  # noqa: SLF001

    completion = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent(
            "done",
            "thr_test",
            "assistant",
            "一。" * (speech_queue._MAX_PENDING_SPEECH_ITEMS + 1),  # noqa: SLF001
        ),
        session=cast("RealtimeSession", session),
        expected_generation=1,
    )
    with pytest.raises(RuntimeError, match="speech queue limit"):
        await completion
    await asyncio.gather(*tuple(connection._effect_tasks), return_exceptions=True)  # noqa: SLF001

    assert not session.voice_active
    assert connection._voice_reconnect_required  # noqa: SLF001
    assert synthesizer.synthesized_texts == []
    await connection.close()


@pytest.mark.asyncio
async def test_user_barge_in_discards_assistant_transcripts_waiting_to_send() -> None:
    websocket = CapturingWebSocket()
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    await connection._send_lock.acquire()  # noqa: SLF001

    old_first = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "old-1"),
    )
    await asyncio.sleep(0)
    old_second = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "old-2"),
    )
    user = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "user", "割り込み"),
    )

    connection._send_lock.release()  # noqa: SLF001
    await asyncio.gather(old_first, old_second, user, return_exceptions=True)
    await asyncio.gather(*tuple(connection._effect_tasks), return_exceptions=True)  # noqa: SLF001

    assert old_second.cancelled()
    assert [message for message in websocket.messages if message.get("role") == "assistant"] == []
    assert all(role != "assistant" for role, _text, _done in speech.transcripts)
    await connection.close()


@pytest.mark.asyncio
async def test_new_ack_waits_for_user_speech_invalidation_before_irodori() -> None:
    class BlockingSuppressingSpeech(RecordingSpeechComposition):
        def __init__(self) -> None:
            super().__init__()
            self.suppressed = False
            self.invalidation_started = asyncio.Event()
            self.invalidation_release = asyncio.Event()

        async def invalidate(self, *, reason: str) -> None:
            assert reason == "user_transcript"
            self.suppressed = True
            self.invalidation_started.set()
            await self.invalidation_release.wait()

        async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
            if role == "user" and done:
                self.suppressed = False
                return
            if role == "assistant" and not self.suppressed:
                await super().on_transcript(role=role, delta=delta, done=done)

    websocket = CapturingWebSocket()
    speech = BlockingSuppressingSpeech()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    user = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "user", "調べて"),
    )
    await speech.invalidation_started.wait()
    acknowledgement = connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "assistant", "確認するね。"),
    )
    for _ in range(10):
        if any(message.get("role") == "assistant" for message in websocket.messages):
            break
        await asyncio.sleep(0)

    assert any(message.get("text") == "確認するね。" for message in websocket.messages)
    assert speech.transcripts == []

    speech.invalidation_release.set()
    await asyncio.gather(user, acknowledgement)
    assert speech.transcripts == [("assistant", "確認するね。", True)]
    await connection.close()


@pytest.mark.asyncio
async def test_user_transcript_text_is_bounded_by_utf8_bytes() -> None:
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    oversized = "あ" * (web_app._MAX_USER_TRANSCRIPT_BYTES // 3 + 1)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="transcript text limit"):
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("done", "thr_test", "user", oversized),
        )


@pytest.mark.asyncio
async def test_user_transcript_part_count_is_bounded() -> None:
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    for _ in range(web_app._MAX_USER_TRANSCRIPT_PARTS):  # noqa: SLF001
        await connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "user", "a"),
        )
    with pytest.raises(RuntimeError, match="transcript part limit"):
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "user", "overflow"),
        )


@pytest.mark.asyncio
async def test_user_transcript_overflow_closes_voice_fail_closed() -> None:
    websocket = CapturingWebSocket()
    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.LISTENING,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001

    oversized = "あ" * (web_app._MAX_USER_TRANSCRIPT_BYTES // 3 + 1)  # noqa: SLF001
    await session.emit(TranscriptEvent("done", "thr_test", "user", oversized))
    await asyncio.wait_for(consumer, timeout=1)

    assert not session.voice_active
    assert connection._voice_reconnect_required  # noqa: SLF001
    assert any(message.get("state") == "voice_reconnect_required" for message in websocket.messages)


@pytest.mark.asyncio
async def test_user_final_claim_survives_speech_invalidation_failure() -> None:
    class SyntheticSpeechInvalidationError(RuntimeError):
        pass

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.handoffs: list[str] = []

        async def consume_user_final(
            self,
            text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            del utterance_id
            self.handoffs.append(text)

    class FailingSpeech(RecordingSpeechComposition):
        async def invalidate(self, *, reason: str) -> None:
            del reason
            raise SyntheticSpeechInvalidationError

    session = Session()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.LISTENING,
        task=TaskState.NONE,
        speech=SpeechState.SYNTHESIZING,
    )
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", FailingSpeech())  # noqa: SLF001

    with pytest.raises(SyntheticSpeechInvalidationError):
        await asyncio.wait_for(
            connection._enqueue_transcript(  # noqa: SLF001
                TranscriptEvent("done", "thr_test", "user", "must hand off once"),
            ),
            timeout=0.1,
        )

    assert session.handoffs == []


@pytest.mark.asyncio
async def test_user_final_claim_survives_transcript_send_failure() -> None:
    class SyntheticTranscriptSendError(RuntimeError):
        pass

    class TranscriptFailingWebSocket(CapturingWebSocket):
        async def send_json(self, message: dict[str, object]) -> None:
            if message.get("type") == "transcript":
                raise SyntheticTranscriptSendError
            await super().send_json(message)

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.handoffs: list[str] = []

        async def consume_user_final(
            self,
            text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            del utterance_id
            self.handoffs.append(text)

    session = Session()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", TranscriptFailingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001

    with pytest.raises(SyntheticTranscriptSendError):
        await connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("done", "thr_test", "user", "must hand off once"),
        )

    assert session.handoffs == []


@pytest.mark.asyncio
async def test_terminal_invalidation_and_agent_final_speech_are_fifo() -> None:
    events: list[object] = []
    speech = RecordingSpeechComposition(events)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    speech.is_busy = True
    connection._turn_cancel_pending = True  # noqa: SLF001
    connection.on_turn_terminal_claimed()
    connection.on_turn_finished(TurnResult(final_answer="Agent の最終回答。", error_code=None))
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert events == [
        "speech.invalidate",
        ("speech", "user", "", True),
        "speech.reset",
        ("speech", "assistant", "Agent の最終回答。", True),
    ]
    assert connection._generation == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_later_successful_turn_does_not_invalidate_prior_final_speech() -> None:
    events: list[object] = []
    speech = RecordingSpeechComposition(events)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    connection.on_turn_terminal_claimed()
    connection.on_turn_finished(TurnResult(final_answer="最初の確定回答。", error_code=None))
    await connection._await_speech_effects()  # noqa: SLF001

    connection.on_turn_terminal_claimed()
    connection.on_turn_finished(TurnResult(final_answer="次の確定回答。", error_code=None))
    await connection._await_speech_effects()  # noqa: SLF001

    assert speech.invalidations == []
    assert speech.transcripts == [
        ("assistant", "最初の確定回答。", True),
        ("assistant", "次の確定回答。", True),
    ]


@pytest.mark.asyncio
async def test_promoted_turn_connection_loss_invalidates_prior_final_speech() -> None:
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    running = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.RUNNING,
        speech=SpeechState.SILENT,
    )
    connection.on_snapshot_changed(running)

    connection.on_turn_terminal_claimed()
    connection.on_snapshot_changed(running)
    connection.on_turn_finished(TurnResult(final_answer="最初の確定回答。", error_code=None))
    await connection._await_speech_effects()  # noqa: SLF001
    generation = connection._speech_effect_generation  # noqa: SLF001

    connection.on_snapshot_changed(
        replace(
            running,
            connection=ConnectionState.DISCONNECTED,
            task=TaskState.FAILED,
        )
    )

    assert connection._speech_effect_generation == generation + 1  # noqa: SLF001
    await connection._await_speech_effects()  # noqa: SLF001
    assert speech.invalidations == ["owner_request"]
    await connection.close()


@pytest.mark.asyncio
async def test_user_invalidation_waits_for_started_speech_enqueue_before_clearing() -> None:
    events: list[str] = []

    class BlockingSpeech(RecordingSpeechComposition):
        def __init__(self) -> None:
            super().__init__()
            self.enqueue_started = asyncio.Event()
            self.enqueue_release = asyncio.Event()

        async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
            del delta, done
            if role == "assistant":
                events.append("enqueue.started")
                self.enqueue_started.set()
                await self.enqueue_release.wait()
                events.append("enqueue.finished")

        async def invalidate(self, *, reason: str) -> None:
            del reason
            events.append("invalidate")

    speech = BlockingSpeech()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._queue_speech_text(  # noqa: SLF001
        "先行する旧音声。",
        name="test-old-speech",
    )
    await speech.enqueue_started.wait()

    user_event = asyncio.ensure_future(
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "user", "割り込み"),
        ),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events == ["enqueue.started"]

    speech.enqueue_release.set()
    await user_event

    assert events == ["enqueue.started", "enqueue.finished", "invalidate"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "summary"),
    [
        ("agent_turn_failed", "処理に失敗しました。"),
        ("agent_turn_interrupted", "処理を中断しました。"),
        ("agent_outcome_unknown", "処理結果を確認できませんでした。"),
    ],
)
async def test_agent_failure_shows_and_speaks_only_same_stable_summary(
    error_code: str,
    summary: str,
) -> None:
    websocket = CapturingWebSocket()
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    connection.on_turn_terminal_claimed()
    connection.on_turn_finished(TurnResult(final_answer=None, error_code=error_code))
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert speech.transcripts == [("assistant", summary, True)]
    assert websocket.messages == [
        {
            "type": "transcript",
            "role": "assistant",
            "text": summary,
            "done": True,
        }
    ]


@pytest.mark.asyncio
async def test_agent_final_is_same_authoritative_text_in_browser_and_speech() -> None:
    websocket = CapturingWebSocket()
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    connection.on_turn_terminal_claimed()
    connection.on_turn_finished(
        TurnResult(final_answer="Agent の確定回答。", error_code=None),
    )
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert speech.transcripts == [("assistant", "Agent の確定回答。", True)]
    assert websocket.messages == [
        {
            "type": "transcript",
            "role": "assistant",
            "text": "Agent の確定回答。",
            "done": True,
        }
    ]


@pytest.mark.asyncio
async def test_auto_mode_removes_valid_plan_and_passes_caption_to_speech(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    speech = CaptionRecordingSpeech()
    capabilities = make_dynamic_capabilities(max_chars=300)
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(irodori=IrodoriSettings(caption_mode="auto")),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    await connection._cache_capabilities(capabilities)  # noqa: SLF001
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":" calm "}'

    connection.on_turn_finished(
        TurnResult(final_answer=f"{control_line}\n本文です。", error_code=None),
    )
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert speech.transcripts == [("assistant", "本文です。", True, "calm")]
    transcript = next(
        message for message in websocket.messages if message.get("type") == "transcript"
    )
    assert transcript == {
        "type": "transcript",
        "role": "assistant",
        "text": "本文です。",
        "done": True,
    }
    assert control_line not in repr(websocket.messages)
    assert control_line not in repr(speech.transcripts)
    event = next(
        record.message
        for record in caplog.records
        if "event=speech_plan_received" in record.message
    )
    assert "caption_present=True" in event
    assert "contract_version=1" in event
    assert f"plan_chars={len(control_line)}" in event
    assert "calm" not in caplog.text
    assert "本文です" not in caplog.text


@pytest.mark.asyncio
async def test_streamed_realtime_plan_never_leaks_control_line_to_display_or_speech() -> None:
    websocket = CapturingWebSocket()
    speech = CaptionRecordingSpeech()
    capabilities = make_dynamic_capabilities(max_chars=300)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(irodori=IrodoriSettings(caption_mode="auto")),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    await connection._cache_capabilities(capabilities)  # noqa: SLF001
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":"calm"}'

    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", control_line[:24]),
    )
    assert websocket.messages == []
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", f"{control_line[24:]}\n本"),
    )
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "文です。"),
    )
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "assistant", f"{control_line}\n本文です。"),
    )

    assert [
        message["text"] for message in websocket.messages if message["type"] == "transcript"
    ] == [
        "本",
        "本文です。",
        "本文です。",
    ]
    assert speech.transcripts == [
        ("assistant", "本", False, "calm"),
        ("assistant", "文です。", False, None),
        ("assistant", "", True, None),
    ]
    assert control_line not in repr(websocket.messages)
    assert control_line not in repr(speech.transcripts)


@pytest.mark.asyncio
async def test_auto_mode_invalid_plan_reports_once_and_speaks_body_without_caption(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    speech = CaptionRecordingSpeech()
    capabilities = make_dynamic_capabilities(max_chars=300)
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(irodori=IrodoriSettings(caption_mode="auto")),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    await connection._cache_capabilities(capabilities)  # noqa: SLF001
    control_line = '{"type":"moco.speech_plan","version":2,"delivery_caption":"calm"}'

    connection.on_turn_finished(
        TurnResult(final_answer=f"{control_line}\n本文です。", error_code=None),
    )
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert speech.transcripts == [("assistant", "本文です。", True, None)]
    assert [
        message
        for message in websocket.messages
        if message == {"type": "error", "code": "speech_caption_invalid"}
    ] == [{"type": "error", "code": "speech_caption_invalid"}]
    transcript = next(
        message for message in websocket.messages if message.get("type") == "transcript"
    )
    assert transcript["text"] == "本文です。"
    assert control_line not in repr(websocket.messages)
    assert control_line not in repr(speech.transcripts)
    event = next(
        record.message for record in caplog.records if "event=speech_plan_invalid" in record.message
    )
    assert "event_code=speech_caption_invalid" in event
    assert f"plan_chars={len(control_line)}" in event
    assert "calm" not in caplog.text
    assert "本文です" not in caplog.text


@pytest.mark.asyncio
async def test_auto_mode_rejects_plan_with_control_emoji_only_body() -> None:
    websocket = CapturingWebSocket()
    speech = CaptionRecordingSpeech()
    capabilities = make_dynamic_capabilities(max_chars=300)
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(irodori=IrodoriSettings(caption_mode="auto")),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    await connection._cache_capabilities(capabilities)  # noqa: SLF001
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":"calm"}'

    connection.on_turn_finished(
        TurnResult(final_answer=f"{control_line}\n🤔", error_code=None),
    )
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert websocket.messages == [
        {"type": "error", "code": "speech_caption_invalid"},
    ]
    assert speech.transcripts == []


def test_unknown_agent_failure_code_has_generic_non_leaking_summary() -> None:
    private_code = "private-path-and-command"

    summary = web_app._turn_failure_speech(private_code)  # noqa: SLF001

    assert summary == "処理を完了できませんでした。"
    assert private_code not in summary


@pytest.mark.asyncio
async def test_explicit_cancel_waits_for_old_speech_invalidation_before_summary() -> None:
    events: list[object] = []

    class Session(FakeSession):
        effects: InteractionEffects | None = None

        async def cancel_turn(self) -> bool:
            self.cancel_calls += 1
            events.append("cancel")
            effects = self.effects
            assert effects is not None
            effects.on_turn_terminal_claimed()
            effects.on_turn_finished(
                TurnResult(final_answer=None, error_code="agent_turn_interrupted"),
            )
            return True

    session = Session()
    speech = RecordingSpeechComposition(events)
    speech.is_busy = True
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    session.effects = connection
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    await connection._apply_control(ClientControl.TURN_CANCEL)  # noqa: SLF001

    assert events == [
        "cancel",
        "speech.invalidate",
        ("speech", "user", "", True),
        "speech.reset",
        ("speech", "assistant", "処理を中断しました。", True),
    ]
    assert connection._generation == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_turn_cancel_without_an_active_turn_returns_stable_error() -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    await connection._apply_control(ClientControl.TURN_CANCEL)  # noqa: SLF001

    assert websocket.messages == [{"type": "error", "code": "turn_not_active"}]


def test_duplicate_turn_cancel_member_is_rejected() -> None:
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_text('{"type":"control","control":"turn_cancel","control":"listen_start"}')

        assert socket.receive_json() == {"type": "error", "code": "invalid_message"}


def test_each_user_utterance_invalidates_old_speech_once() -> None:
    sessions: list[FakeSession] = []

    def session_factory() -> RealtimeSession:
        session = FakeSession()
        sessions.append(session)
        return cast("RealtimeSession", session)

    app = create_app(
        session_factory=session_factory,
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None

        portal.call(
            sessions[0].emit,
            TranscriptEvent("delta", "thr_test", "user", "一"),
        )
        assert socket.receive_json()["type"] == "audio_invalidate"
        assert socket.receive_json()["text"] == "一"

        portal.call(
            sessions[0].emit,
            TranscriptEvent("delta", "thr_test", "user", "つ"),
        )
        assert socket.receive_json()["text"] == "一つ"
        portal.call(
            sessions[0].emit,
            TranscriptEvent("done", "thr_test", "user", "一つ"),
        )
        assert socket.receive_json()["done"] is True

        portal.call(
            sessions[0].emit,
            TranscriptEvent("delta", "thr_test", "user", "次"),
        )
        assert socket.receive_json()["type"] == "audio_invalidate"
        assert socket.receive_json()["text"] == "次"


def test_streamed_realtime_assistant_response_is_the_only_speech_source() -> None:
    session = EffectsSession()
    synthesizer = FakeSynthesizer()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None

        portal.call(
            session.emit,
            TranscriptEvent("delta", "thr_test", "assistant", "確"),
        )
        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "assistant", "確認します。"),
        )
        delta = socket.receive_json()
        completed = socket.receive_json()

        assert delta == {
            "type": "transcript",
            "role": "assistant",
            "text": "確",
            "done": False,
        }
        assert completed == {
            "type": "transcript",
            "role": "assistant",
            "text": "確認します。",
            "done": True,
        }

        portal.call(asyncio.sleep, 0.1)

    assert synthesizer.synthesized_texts == ["確認します。"]


def test_agent_final_uses_existing_speech_queue() -> None:
    capabilities = make_capabilities(3, default_index=1)
    runtime_default = next(voice for voice in capabilities.voices if voice.default)
    settings = MocoSettings(
        speech=SpeechSettings(first_segment_soft_break_min_chars=18),
    )

    session = EffectsSession()
    synthesizer = FakeSynthesizer(capabilities)
    first_segment = "これは最初の音声を早く届けるための自然な区切りです、"
    remainder = "残りを順番に届けます。"
    app = create_app(
        settings,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None

        effects = session.effects
        assert effects is not None
        portal.call(effects.on_turn_terminal_claimed)
        portal.call(
            effects.on_turn_finished,
            TurnResult(final_answer=first_segment + remainder, error_code=None),
        )

        portal.call(asyncio.sleep, 0.1)
        assert synthesizer.synthesized_texts == [first_segment + remainder]

    assert runtime_default.id in synthesizer.selected_voices
    assert synthesizer.synthesized_texts == [first_segment + remainder]


def test_user_final_transcript_replaces_incorrect_interim_text() -> None:
    session = FakeSession()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None

        portal.call(
            session.emit,
            TranscriptEvent("delta", "thr_test", "user", "きょは"),
        )
        assert socket.receive_json()["type"] == "audio_invalidate"
        assert socket.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "きょは",
            "done": False,
        }

        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "user", "今日は"),
        )
        assert socket.receive_json() == {
            "type": "transcript",
            "role": "user",
            "text": "今日は",
            "done": True,
        }


@pytest.mark.asyncio
async def test_voice_assistant_transcript_is_displayed_and_streamed_to_speech_once() -> None:
    websocket = CapturingWebSocket()
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "中断される応答"),
    )
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "user", "割り込み"),
    )
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "assistant", "新しい応答です。"),
    )

    transcripts = [message for message in websocket.messages if message["type"] == "transcript"]
    assert transcripts == [
        {
            "type": "transcript",
            "role": "assistant",
            "text": "中断される応答",
            "done": False,
        },
        {
            "type": "transcript",
            "role": "user",
            "text": "割り込み",
            "done": True,
        },
        {
            "type": "transcript",
            "role": "assistant",
            "text": "新しい応答です。",
            "done": True,
        },
    ]
    assert speech.transcripts == [
        ("assistant", "中断される応答", False),
        ("user", "", True),
        ("assistant", "新しい応答です。", True),
    ]


@pytest.mark.asyncio
async def test_user_final_is_not_resubmitted_to_a_client_managed_agent_turn() -> None:
    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.handoffs: list[str] = []

        async def consume_user_final(
            self,
            text: str,
            *,
            utterance_id: int | None = None,
        ) -> None:
            del utterance_id
            self.handoffs.append(text)

    session = Session()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001

    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "user", "同じ依頼を二重実行しない"),
    )

    assert session.handoffs == []


@pytest.mark.asyncio
async def test_agent_final_control_emoji_is_sanitized_identically_for_display_and_speech() -> None:
    websocket = CapturingWebSocket()
    speech = RecordingSpeechComposition()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    connection.on_turn_terminal_claimed()
    connection.on_turn_finished(
        TurnResult(final_answer="😮‍💨確認", error_code=None),
    )
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert websocket.messages == [
        {
            "type": "transcript",
            "role": "assistant",
            "text": "確認",
            "done": True,
        }
    ]
    assert speech.transcripts == [("assistant", "確認", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        ActivityEvent("turn", "started", "thr_test", "turn-1", None),
        ReasoningSummaryEvent("thr_test", "turn-1", "item-1", "stale reasoning"),
        TranscriptEvent("done", "thr_test", "assistant", "stale transcript"),
    ],
    ids=["activity", "reasoning", "transcript"],
)
async def test_stale_voice_generation_drops_outbound_event_waiting_for_send_lock(
    event: RealtimeEvent,
) -> None:
    websocket = CapturingWebSocket()
    session = GenerationSession()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    await connection._send_lock.acquire()  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
    await session.emit_for_generation(1, event)
    await session.end_generation(1)
    await asyncio.sleep(0)

    session.voice_generation = 2
    connection._send_lock.release()  # noqa: SLF001
    await consumer
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert websocket.messages == []


@pytest.mark.asyncio
async def test_user_final_is_never_resubmitted_while_outbound_send_is_blocked() -> None:
    websocket = CapturingWebSocket()
    session = GenerationSession()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    await connection._send_lock.acquire()  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
    await session.emit_for_generation(
        1,
        TranscriptEvent("done", "thr_test", "user", "current final"),
    )
    await session.end_generation(1)
    await asyncio.sleep(0)
    claim_began_before_reoffer = session.user_final_started.is_set()

    session.voice_generation = 2
    connection._send_lock.release()  # noqa: SLF001
    await consumer
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert not claim_began_before_reoffer
    assert session.user_finals == []
    assert websocket.messages == []


@pytest.mark.asyncio
async def test_stale_generation_after_blocked_event_drops_queued_user_final() -> None:
    websocket = CapturingWebSocket()
    session = GenerationSession()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    await connection._send_lock.acquire()  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
    await session.emit_for_generation(
        1,
        ActivityEvent("turn", "started", "thr_test", "turn-1", None),
    )
    await session.emit_for_generation(
        1,
        TranscriptEvent("done", "thr_test", "user", "stale final"),
    )
    await session.end_generation(1)
    await asyncio.sleep(0)

    session.voice_generation = 2
    connection._send_lock.release()  # noqa: SLF001
    await consumer

    assert session.user_finals == []
    assert websocket.messages == []


@pytest.mark.asyncio
async def test_voice_assistant_reaches_outbound_send_and_speech() -> None:
    websocket = BlockingJsonWebSocket()
    session = GenerationSession()
    speech = RecordingSpeech()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
    await session.emit_for_generation(
        1,
        TranscriptEvent("done", "thr_test", "assistant", "current transcript"),
    )
    await session.end_generation(1)
    await consumer

    assert websocket.send_started.is_set()
    websocket.send_release.set()
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001
    assert websocket.messages == [
        {
            "type": "transcript",
            "role": "assistant",
            "text": "current transcript",
            "done": True,
        }
    ]
    assert speech.transcripts == [("assistant", "current transcript", True)]


@pytest.mark.asyncio
async def test_inactive_same_generation_drops_all_buffered_voice_effects() -> None:
    websocket = CapturingWebSocket()
    session = GenerationSession()
    speech = RecordingSpeech()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._user_utterance_active = True  # noqa: SLF001
    session.voice_active = False
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
    await session.emit_for_generation(
        1,
        ActivityEvent("turn", "started", "thr_test", "turn-1", None),
    )
    await session.emit_for_generation(
        1,
        ReasoningSummaryEvent("thr_test", "turn-1", "item-1", "buffered reasoning"),
    )
    await session.emit_for_generation(
        1,
        TranscriptEvent("done", "thr_test", "assistant", "buffered transcript"),
    )
    await session.emit_for_generation(
        1,
        TranscriptEvent("done", "thr_test", "user", "buffered user final"),
    )
    await session.end_generation(1)

    await consumer

    assert websocket.messages == []
    assert session.user_finals == []
    assert speech.transcripts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "old_delta", "new_delta", "new_final"),
    [
        ("user", "古い質問", "新しい質問", "新しい質問です"),
        ("assistant", "古い応答", "新しい応答", "新しい応答です"),
    ],
)
async def test_successful_voice_replacement_clears_prior_generation_transcript_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: Literal["assistant", "user"],
    old_delta: str,
    new_delta: str,
    new_final: str,
) -> None:
    owner_connection = OwnerConnection()
    discovery = OwnerDiscovery(make_codex_snapshot(), owner_connection.event_log)
    voices = [ReofferVoice("answer-1"), ReofferVoice("answer-2")]
    monkeypatch.setattr(web_app, "CodexRealtimeSession", lambda *_args, **_kwargs: voices.pop(0))
    owner = make_owner(tmp_path, owner_connection, discovery)
    await owner.start("offer-1")
    websocket = CapturingWebSocket()

    class BlockingOutwardConnection(web_app._BrowserConnection):  # noqa: SLF001
        def __init__(self, websocket_value: WebSocket) -> None:
            super().__init__(
                websocket_value,
                settings=MocoSettings(),
                global_hotkeys_active=True,
                session_factory=lambda: cast("RealtimeSession", owner),
                synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
            )
            self.block_outward = False
            self.outward_started = asyncio.Event()
            self.outward_release = asyncio.Event()

        async def _send_voice_json(
            self,
            message: dict[str, object],
            *,
            session: RealtimeSession | None,
            expected_generation: int | None,
        ) -> bool:
            if self.block_outward:
                self.outward_started.set()
                await self.outward_release.wait()
            return await super()._send_voice_json(
                message,
                session=session,
                expected_generation=expected_generation,
            )

    connection = BlockingOutwardConnection(
        cast("WebSocket", websocket),
    )
    connection._session = cast("RealtimeSession", owner)  # noqa: SLF001
    loss_task: asyncio.Task[None] | None = None

    try:
        await connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", role, old_delta),
            session=cast("RealtimeSession", owner),
            expected_generation=1,
        )
        connection.block_outward = True
        loss_task = asyncio.create_task(
            connection._handle_voice_loss(  # noqa: SLF001
                cast("RealtimeSession", owner),
                1,
            )
        )
        await asyncio.wait_for(connection.outward_started.wait(), 0.5)
        assert connection._transcripts == {}  # noqa: SLF001
        assert not connection._user_utterance_active  # noqa: SLF001
        connection.outward_release.set()
        await loss_task
        loss_task = None
        connection.block_outward = False

        assert not owner.voice_active
        await connection._start(StartMessage(sdp="offer-2"))  # noqa: SLF001
        start_index = len(websocket.messages)
        await connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", role, new_delta),
            session=cast("RealtimeSession", owner),
            expected_generation=2,
        )
        await connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("done", "thr_test", role, new_final),
            session=cast("RealtimeSession", owner),
            expected_generation=2,
        )

        transcripts = [
            message
            for message in websocket.messages[start_index:]
            if message["type"] == "transcript"
        ]
        expected_texts = [new_delta, new_final]
        assert [message["text"] for message in transcripts] == expected_texts
        assert old_delta not in repr(transcripts)
        assert owner.voice_generation == 2
        assert owner.voice_active
    finally:
        connection.outward_release.set()
        if loss_task is not None:
            await asyncio.gather(loss_task, return_exceptions=True)
        await connection._close_conversation_resources()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["realtime", "voice_lost_message"])
async def test_voice_close_error_after_inactive_settles_reconnect_state(source: str) -> None:
    class VoiceCloseError(RuntimeError):
        """Synthetic Voice shutdown failure after local deactivation."""

        def __init__(self) -> None:
            super().__init__("private Voice close failure")

    class Session(GenerationSession):
        def __init__(self) -> None:
            super().__init__()
            self.voice_close_calls = 0

        async def close_voice(
            self,
            expected_generation: int,
            *,
            on_claimed: Callable[[], None],
        ) -> bool:
            assert expected_generation == self.voice_generation
            self.voice_close_calls += 1
            self.voice_active = False
            on_claimed()
            raise VoiceCloseError

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.invalidation_reasons: list[str] = []
            self.reset_calls = 0

        async def invalidate(self, *, reason: str) -> None:
            self.invalidation_reasons.append(reason)

        async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
            assert (role, delta, done) == ("user", "", True)
            self.reset_calls += 1

        async def close(self) -> None:
            return None

    session = Session()
    speech = Speech()
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._transcripts = {  # noqa: SLF001
        "user": "buffered user",
        "assistant": "buffered assistant",
    }
    connection._user_utterance_active = True  # noqa: SLF001

    try:
        if source == "realtime":
            consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
            await session.emit_for_generation(1, RealtimeErrorEvent("thr_test", "terminal"))
            await consumer
        else:
            assert await connection._handle(json.dumps({"type": "voice_lost"}))  # noqa: SLF001

        assert session.voice_close_calls == 1
        assert not session.voice_active
        assert connection._session is session  # noqa: SLF001
        assert connection._transcripts == {}  # noqa: SLF001
        assert not connection._user_utterance_active  # noqa: SLF001
        assert speech.invalidation_reasons == ["user_transcript"]
        assert speech.reset_calls == 1
        assert connection._voice_reconnect_required  # noqa: SLF001
        states = [message["state"] for message in websocket.messages if message["type"] == "state"]
        assert states[-1] == "voice_reconnect_required"
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_text"),
    [
        (TurnResult(final_answer="Agent の最終回答。", error_code=None), "Agent の最終回答。"),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.FAILED),
            "処理に失敗しました。",
        ),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.INTERRUPTED),
            "処理を中断しました。",
        ),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
            "処理結果を確認できませんでした。",
        ),
    ],
)
async def test_voice_loss_resets_suppression_before_agent_result(
    result: TurnResult,
    expected_text: str,
) -> None:
    class Session(GenerationSession):
        async def close_voice(
            self,
            expected_generation: int,
            *,
            on_claimed: Callable[[], None],
        ) -> bool:
            assert expected_generation == self.voice_generation
            self.voice_active = False
            on_claimed()
            return True

    session = Session()
    synthesizer = FakeSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=lambda *_args: None,
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()

    try:
        await connection._handle_voice_loss(session, 1)  # noqa: SLF001
        connection.on_turn_terminal_claimed()
        connection.on_turn_finished(result)
        await connection._await_speech_effects()  # noqa: SLF001
        await speech.join()

        assert synthesizer.synthesized_texts == [expected_text]
        assert speech.pending_count == 0
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_text"),
    [
        (TurnResult(final_answer="Agent の最終回答。", error_code=None), "Agent の最終回答。"),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.FAILED),
            "処理に失敗しました。",
        ),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.INTERRUPTED),
            "処理を中断しました。",
        ),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
            "処理結果を確認できませんでした。",
        ),
    ],
)
async def test_voice_loss_claims_invalidation_before_blocked_close_and_agent_result(
    result: TurnResult,
    expected_text: str,
) -> None:
    class Session(GenerationSession):
        def __init__(self) -> None:
            super().__init__()
            self.voice_close_started = asyncio.Event()
            self.voice_close_release = asyncio.Event()

        async def close_voice(
            self,
            expected_generation: int,
            *,
            on_claimed: Callable[[], None],
        ) -> bool:
            assert expected_generation == self.voice_generation
            self.voice_active = False
            on_claimed()
            self.voice_close_started.set()
            await self.voice_close_release.wait()
            return True

    session = Session()
    synthesizer = FakeSynthesizer()
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()
    voice_loss = asyncio.create_task(connection._handle_voice_loss(session, 1))  # noqa: SLF001

    try:
        await session.voice_close_started.wait()
        connection.on_turn_terminal_claimed()
        connection.on_turn_finished(result)
        await connection._await_speech_effects()  # noqa: SLF001
        await speech.join()
        session.voice_close_release.set()
        await voice_loss
        await connection._await_speech_effects()  # noqa: SLF001
        await speech.join()

        assert synthesizer.synthesized_texts == [expected_text]
        audio_lifecycle = [
            message
            for message in websocket.messages
            if message["type"] in {"audio", "audio_invalidate"}
        ]
        assert [message["type"] for message in audio_lifecycle] == [
            "audio_invalidate",
            "audio",
        ]
        assert [message["generation"] for message in audio_lifecycle] == [1, 1]
    finally:
        session.voice_close_release.set()
        await asyncio.gather(voice_loss, return_exceptions=True)
        await connection.close()


@pytest.mark.asyncio
async def test_active_turn_connection_loss_delivers_unknown_summary_before_cleanup() -> None:
    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.close_claims = 0

        def claim_close(self) -> None:
            self.close_claims += 1

    session = Session()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.FAILED,
        speech=SpeechState.SILENT,
    )
    synthesizer = FakeSynthesizer()
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()

    connection.on_turn_terminal_claimed()
    connection.on_snapshot_changed(session.interaction_snapshot)
    assert session.close_claims == 1
    connection.on_turn_finished(
        TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
    )
    cleanup = connection._connection_loss_task  # noqa: SLF001
    assert cleanup is not None
    await cleanup

    assert synthesizer.synthesized_texts == ["処理結果を確認できませんでした。"]
    assert [message["type"] for message in websocket.messages].count("audio") == 1
    assert session.closed
    assert synthesizer.closed
    detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
    assert detached is None
    await connection.close()


@pytest.mark.asyncio
async def test_connection_loss_bounds_blocked_summary_delivery_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_app,
        "_CONNECTION_LOSS_SETTLEMENT_SECONDS",
        0.01,
        raising=False,
    )
    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.FAILED,
        speech=SpeechState.SILENT,
    )
    synthesizer = FakeSynthesizer()
    websocket = GatedAudioWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()

    connection.on_turn_terminal_claimed()
    connection.on_snapshot_changed(session.interaction_snapshot)
    connection.on_turn_finished(
        TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
    )
    cleanup = connection._connection_loss_task  # noqa: SLF001
    assert cleanup is not None
    await asyncio.wait_for(asyncio.shield(cleanup), timeout=0.2)

    assert synthesizer.synthesized_texts == ["処理結果を確認できませんでした。"]
    assert websocket.started.is_set()
    assert session.closed
    assert synthesizer.closed
    detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
    assert detached is None
    websocket.release.set()
    await connection.close()


@pytest.mark.asyncio
async def test_connection_loss_delivery_failure_still_cleans_up_after_summary_attempt() -> None:
    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.FAILED,
        speech=SpeechState.SILENT,
    )
    synthesizer = FakeSynthesizer()
    websocket = FailingAudioWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()

    connection.on_turn_terminal_claimed()
    connection.on_snapshot_changed(session.interaction_snapshot)
    connection.on_turn_finished(
        TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
    )
    cleanup = connection._connection_loss_task  # noqa: SLF001
    assert cleanup is not None
    await asyncio.wait_for(asyncio.shield(cleanup), timeout=0.2)

    assert synthesizer.synthesized_texts == ["処理結果を確認できませんでした。"]
    assert speech.error_codes == ("audio_delivery_failed",)
    assert session.closed
    assert synthesizer.closed
    detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
    assert detached is None
    await connection.close()


@pytest.mark.asyncio
async def test_connection_loss_drain_cancellation_still_cleans_up_lease() -> None:
    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.FAILED,
        speech=SpeechState.SILENT,
    )
    synthesizer = FakeSynthesizer()
    websocket = GatedAudioWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()

    connection.on_turn_terminal_claimed()
    connection.on_snapshot_changed(session.interaction_snapshot)
    connection.on_turn_finished(
        TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
    )
    cleanup = connection._connection_loss_task  # noqa: SLF001
    assert cleanup is not None
    await websocket.started.wait()
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert synthesizer.synthesized_texts == ["処理結果を確認できませんでした。"]
    assert session.closed
    assert synthesizer.closed
    detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
    assert detached is None
    websocket.release.set()
    await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["synthesis", "delivery"])
@pytest.mark.parametrize(
    ("result", "terminal_state", "loss_before_result", "expected_text"),
    [
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
            TaskState.FAILED,
            True,
            "処理結果を確認できませんでした。",
        ),
        (
            TurnResult(final_answer="Agent の最終回答。", error_code=None),
            TaskState.COMPLETED,
            False,
            "Agent の最終回答。",
        ),
        (
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.INTERRUPTED),
            TaskState.INTERRUPTED,
            False,
            "処理を中断しました。",
        ),
    ],
)
async def test_connection_loss_owns_terminal_delivery_after_effect_tail_completes(  # noqa: C901, PLR0915
    blocked_at: str,
    result: TurnResult,
    terminal_state: TaskState,
    loss_before_result: bool,
    expected_text: str,
) -> None:
    class GatedTerminalSynthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def synthesize(self, text: str) -> bytes:
            self.synthesized_texts.append(text)
            self.synthesized.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return b"RIFF\x04\x00\x00\x00WAVE"

    class YieldingStateWebSocket(CapturingWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.connection: web_app._BrowserConnection | None = None
            self.observed_completed_tail = False
            self.connection_loss_state_observed = asyncio.Event()

        async def send_json(self, message: dict[str, object]) -> None:
            if message.get("type") == "state":
                for _ in range(10):
                    await asyncio.sleep(0)
                connection = self.connection
                self.observed_completed_tail = (
                    connection is not None and connection._speech_effect_tail is None  # noqa: SLF001
                )
                if message.get("state") == "connection_lost":
                    self.connection_loss_state_observed.set()
            await super().send_json(message)

    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.RUNNING,
        speech=SpeechState.SILENT,
    )
    synthesizer = GatedTerminalSynthesizer() if blocked_at == "synthesis" else FakeSynthesizer()
    websocket = YieldingStateWebSocket()
    delivery_started = asyncio.Event()
    delivery_release = asyncio.Event()

    async def deliver(wav: bytes, audio_id: int, generation: int) -> None:
        if blocked_at == "delivery":
            delivery_started.set()
            await delivery_release.wait()
        await connection._deliver_audio(wav, audio_id, generation)  # noqa: SLF001

    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    websocket.connection = connection
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    speech = SpeechQueue(
        synthesizer,
        deliver=deliver,
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()
    ready_terminal = replace(
        session.interaction_snapshot,
        task=terminal_state,
    )
    disconnected = replace(ready_terminal, connection=ConnectionState.DISCONNECTED)

    connection.on_turn_terminal_claimed()
    if loss_before_result:
        session.interaction_snapshot = disconnected
        connection.on_snapshot_changed(disconnected)
        connection.on_turn_finished(result)
    else:
        session.interaction_snapshot = ready_terminal
        connection.on_snapshot_changed(ready_terminal)
        connection.on_turn_finished(result)
        await connection._await_speech_effects()  # noqa: SLF001
        session.interaction_snapshot = disconnected
        connection.on_snapshot_changed(disconnected)
    cleanup = connection._connection_loss_task  # noqa: SLF001
    assert cleanup is not None
    gate_started = synthesizer.synthesized if blocked_at == "synthesis" else delivery_started
    await gate_started.wait()
    await websocket.connection_loss_state_observed.wait()
    await asyncio.sleep(0)

    assert not cleanup.done()
    assert websocket.observed_completed_tail
    if blocked_at == "synthesis":
        assert not cast("GatedTerminalSynthesizer", synthesizer).cancelled.is_set()
        cast("GatedTerminalSynthesizer", synthesizer).release.set()
    else:
        delivery_release.set()
    await cleanup

    assert synthesizer.synthesized_texts == [expected_text]
    assert [message["type"] for message in websocket.messages].count("audio") == 1
    assert session.closed
    assert synthesizer.closed
    await connection.close()


@pytest.mark.asyncio
async def test_blocked_connection_loss_state_notice_cannot_gate_lease_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_app,
        "_CONNECTION_LOSS_SETTLEMENT_SECONDS",
        0.01,
        raising=False,
    )

    class BlockingStateWebSocket(CapturingWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.state_started = asyncio.Event()
            self.state_release = asyncio.Event()

        async def send_json(self, message: dict[str, object]) -> None:
            if message.get("type") == "state":
                self.state_started.set()
                await self.state_release.wait()
            await super().send_json(message)

    session = FakeSession()
    session.interaction_snapshot = InteractionSnapshot(
        connection=ConnectionState.DISCONNECTED,
        voice=VoiceState.IDLE,
        task=TaskState.NONE,
        speech=SpeechState.SILENT,
    )
    synthesizer = FakeSynthesizer()
    websocket = BlockingStateWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001

    try:
        connection.on_snapshot_changed(session.interaction_snapshot)
        cleanup = connection._connection_loss_task  # noqa: SLF001
        assert cleanup is not None
        await websocket.state_started.wait()
        await asyncio.sleep(0.03)

        assert cleanup.done()
        assert not cleanup.cancelled()
        cleanup.result()
        assert session.closed
        assert synthesizer.closed
        detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
        assert detached is None
    finally:
        websocket.state_release.set()
        await connection.close()


@pytest.mark.asyncio
async def test_speech_invalidation_error_after_voice_close_still_settles_reconnect_once() -> None:
    invalidation_error = RuntimeError("private speech invalidation failure")

    class Session(GenerationSession):
        def __init__(self) -> None:
            super().__init__()
            self.voice_close_calls = 0

        async def close_voice(
            self,
            expected_generation: int,
            *,
            on_claimed: Callable[[], None],
        ) -> bool:
            assert expected_generation == self.voice_generation
            self.voice_close_calls += 1
            self.voice_active = False
            on_claimed()
            return True

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.reset_calls = 0

        async def invalidate(self, *, reason: str) -> None:
            assert reason == "user_transcript"
            raise invalidation_error

        async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
            assert (role, delta, done) == ("user", "", True)
            self.reset_calls += 1

        async def close(self) -> None:
            return None

    session = Session()
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    speech = Speech()
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    try:
        consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
        await session.emit_for_generation(1, RealtimeErrorEvent("thr_test", "terminal"))
        await consumer

        states = [message["state"] for message in websocket.messages if message["type"] == "state"]
        assert session.voice_close_calls == 1
        assert not session.voice_active
        assert connection._session is session  # noqa: SLF001
        assert connection._voice_reconnect_required  # noqa: SLF001
        assert states == ["voice_reconnect_required"]
        assert speech.reset_calls == 1
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["close_voice", "invalidation"])
async def test_connection_loss_during_voice_loss_settlement_never_restores_reconnect(
    blocked_at: str,
) -> None:
    class Session(GenerationSession):
        def __init__(self) -> None:
            super().__init__()
            self.voice_close_calls = 0
            self.voice_close_started = asyncio.Event()
            self.voice_close_release = asyncio.Event()

        async def close_voice(
            self,
            expected_generation: int,
            *,
            on_claimed: Callable[[], None],
        ) -> bool:
            assert expected_generation == self.voice_generation
            self.voice_close_calls += 1
            self.voice_active = False
            on_claimed()
            self.voice_close_started.set()
            if blocked_at == "close_voice":
                await self.voice_close_release.wait()
            return True

    class Speech:
        is_busy = False

        def __init__(self) -> None:
            self.invalidation_started = asyncio.Event()
            self.invalidation_release = asyncio.Event()
            self.close_calls = 0

        async def invalidate(self, *, reason: str) -> None:
            assert reason == "user_transcript"
            self.invalidation_started.set()
            if blocked_at == "invalidation":
                await self.invalidation_release.wait()

        async def on_transcript(self, *, role: str, delta: str, done: bool) -> None:
            assert (role, delta, done) == ("user", "", True)

        async def close(self) -> None:
            self.close_calls += 1

    session = Session()
    speech = Speech()
    synthesizer = FakeSynthesizer()
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    connection._transcripts = {"user": "buffered"}  # noqa: SLF001
    connection._user_utterance_active = True  # noqa: SLF001
    handler = asyncio.create_task(connection._handle_voice_loss(session, 1))  # noqa: SLF001

    try:
        await session.voice_close_started.wait()
        if blocked_at == "invalidation":
            await speech.invalidation_started.wait()
        disconnected = InteractionSnapshot(
            connection=ConnectionState.DISCONNECTED,
            voice=VoiceState.IDLE,
            task=TaskState.NONE,
            speech=SpeechState.SILENT,
        )
        connection.on_snapshot_changed(disconnected)
        whole_close = connection._connection_loss_task  # noqa: SLF001
        assert whole_close is not None
        await asyncio.shield(whole_close)
        state_count = len([message for message in websocket.messages if message["type"] == "state"])

        session.voice_close_release.set()
        speech.invalidation_release.set()
        await handler

        states = [message["state"] for message in websocket.messages if message["type"] == "state"]
        assert len(states) == state_count
        assert states[-1] == "connection_lost"
        assert not connection._voice_reconnect_required  # noqa: SLF001
        detached = cast("RealtimeSession | None", connection._session)  # noqa: SLF001
        assert detached is None
        assert connection._transcripts == {}  # noqa: SLF001
        assert not connection._user_utterance_active  # noqa: SLF001
        assert session.voice_close_calls == 1
        assert speech.close_calls == 1
        assert synthesizer.closed
    finally:
        session.voice_close_release.set()
        speech.invalidation_release.set()
        await asyncio.gather(handler, return_exceptions=True)
        await connection.close()


def test_forwards_safe_codex_activity_without_payload_details() -> None:
    session = FakeSession()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None
        before_ms = int(time.time() * 1000)
        portal.call(
            session.emit,
            ActivityEvent("turn", "started", "thr_test", "turn-1", None),
        )
        portal.call(
            session.emit,
            ActivityEvent("web_search", "started", "thr_test", "turn-1", 1234),
        )
        turn = socket.receive_json()
        assert turn["type"] == "activity"
        assert turn["kind"] == "turn"
        assert turn["source"] == "voice"
        assert turn["phase"] == "started"
        assert turn["label"] == "応答処理"
        assert before_ms <= turn["occurredAtMs"] <= int(time.time() * 1000)
        assert socket.receive_json() == {
            "type": "activity",
            "kind": "work",
            "phase": "started",
            "label": "Web 検索",
            "occurredAtMs": 1234,
        }
        rendered = repr(turn)
        assert "thread" not in rendered
        assert "turn-1" not in rendered


@pytest.mark.asyncio
async def test_agent_activity_sends_only_current_safe_projection() -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    before_ms = int(time.time() * 1000)

    connection.on_agent_activity(AgentActivityEvent("external_tool", "completed"))
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    assert len(websocket.messages) == 1
    message = websocket.messages[0]
    occurred_at_ms = message.pop("occurredAtMs")
    assert message == {
        "type": "activity",
        "kind": "work",
        "phase": "completed",
        "label": "外部ツール",
    }
    assert isinstance(occurred_at_ms, int)
    assert before_ms <= occurred_at_ms <= int(time.time() * 1000)


@pytest.mark.asyncio
async def test_agent_turn_activity_completes_once_across_connection_loss_snapshots() -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    running = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.RUNNING,
        speech=SpeechState.SILENT,
    )
    disconnected_running = replace(running, connection=ConnectionState.DISCONNECTED)
    disconnected_failed = replace(disconnected_running, task=TaskState.FAILED)

    connection.on_snapshot_changed(running)
    connection.on_snapshot_changed(disconnected_running)
    connection.on_snapshot_changed(disconnected_failed)
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001

    activities = [message for message in websocket.messages if message["type"] == "activity"]
    assert [message["phase"] for message in activities] == ["started", "completed"]
    await connection.close()


@pytest.mark.asyncio
async def test_agent_activity_uses_one_bounded_sender_when_websocket_is_blocked() -> None:
    websocket = BlockingJsonWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )

    connection.on_agent_activity(AgentActivityEvent("external_tool", "started"))
    await websocket.send_started.wait()
    for _index in range(1_000):
        connection.on_agent_activity(AgentActivityEvent("external_tool", "completed"))

    try:
        assert connection._agent_activity_queue.qsize() == 64  # noqa: SLF001
        activity_tasks = [
            task
            for task in connection._effect_tasks  # noqa: SLF001
            if task.get_name() == "moco-agent-activity-worker"
        ]
        assert len(activity_tasks) == 1
        assert connection._agent_activity_backpressure_reported  # noqa: SLF001
    finally:
        websocket.send_release.set()
        await connection.close()


@pytest.mark.asyncio
async def test_agent_turn_terminal_evicts_droppable_work_when_activity_queue_is_full() -> None:
    websocket = BlockingJsonWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    running = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.RUNNING,
        speech=SpeechState.SILENT,
    )

    connection.on_snapshot_changed(running)
    await websocket.send_started.wait()
    for _index in range(web_app._MAX_PENDING_AGENT_ACTIVITIES):  # noqa: SLF001
        connection.on_agent_activity(AgentActivityEvent("external_tool", "completed"))
    connection.on_snapshot_changed(replace(running, task=TaskState.COMPLETED))
    assert connection._agent_activity_queue.qsize() == 64  # noqa: SLF001

    websocket.send_release.set()
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001
    turn_phases = [
        message["phase"]
        for message in websocket.messages
        if message.get("type") == "activity" and message.get("kind") == "turn"
    ]
    assert turn_phases == ["started", "completed"]
    await connection.close()


@pytest.mark.asyncio
async def test_agent_turn_terminal_replaces_stale_lifecycle_when_activity_queue_is_full() -> None:
    websocket = BlockingJsonWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    running = InteractionSnapshot(
        connection=ConnectionState.READY,
        voice=VoiceState.IDLE,
        task=TaskState.RUNNING,
        speech=SpeechState.SILENT,
    )
    completed = replace(running, task=TaskState.COMPLETED)

    connection.on_snapshot_changed(running)
    await websocket.send_started.wait()
    for _index in range(web_app._MAX_PENDING_AGENT_ACTIVITIES // 2):  # noqa: SLF001
        connection.on_snapshot_changed(completed)
        connection.on_snapshot_changed(running)
    connection.on_snapshot_changed(completed)
    assert connection._agent_activity_queue.qsize() == 64  # noqa: SLF001

    websocket.send_release.set()
    await asyncio.gather(*tuple(connection._effect_tasks))  # noqa: SLF001
    turn_phases = [
        message["phase"]
        for message in websocket.messages
        if message.get("type") == "activity" and message.get("kind") == "turn"
    ]
    assert turn_phases[-1] == "completed"
    await connection.close()


@pytest.mark.asyncio
async def test_progress_drops_reasoning_payload_and_keeps_safe_category_only() -> None:
    websocket = CapturingWebSocket()
    session = GenerationSession()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    consumer = asyncio.create_task(connection._consume_notifications(1))  # noqa: SLF001
    await session.emit_for_generation(
        1,
        ActivityEvent("command_execution", "started", "private-thread", "private-turn", 9),
    )
    await session.emit_for_generation(
        1,
        ReasoningSummaryEvent(
            "private-thread",
            "private-turn",
            "private-item",
            "run /private/path with --secret and patch payload",
        ),
    )
    await session.end_generation(1)

    await consumer

    assert websocket.messages == [
        {
            "type": "activity",
            "kind": "work",
            "phase": "started",
            "label": "コマンド実行",
            "occurredAtMs": 9,
        },
    ]


def test_reports_synthesis_start_and_completion() -> None:
    session = EffectsSession()
    synthesizer = BlockingSynthesizer()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None
        effects = session.effects
        assert effects is not None
        portal.call(effects.on_turn_terminal_claimed)
        portal.call(
            effects.on_turn_finished,
            TurnResult(final_answer="確認しました。", error_code=None),
        )
        portal.call(synthesizer.started.wait)

        assert socket.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "確認しました。",
            "done": True,
        }
        started = socket.receive_json()
        assert started["type"] == "activity"
        assert started["kind"] == "voice"
        assert started["phase"] == "started"
        assert started["label"] == "音声生成"

        portal.call(synthesizer.release.set)
        assert socket.receive_json()["type"] == "audio"
        assert socket.receive_bytes().startswith(b"RIFF")
        completed = socket.receive_json()
        assert completed["type"] == "activity"
        assert completed["kind"] == "voice"
        assert completed["phase"] == "completed"


def test_mid_conversation_generation_mismatch_is_reported_without_voice_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capabilities = make_capabilities(2)
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    session = EffectsSession()
    discovery = FakeSynthesizer(capabilities)
    active = FakeSynthesizer(
        capabilities,
        synthesis_error=IrodoriError(
            "fixture generation changed",
            code="runtime_generation_mismatch",
        ),
    )
    synthesizers = [discovery, active]
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizers.pop(0)),
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None
        effects = session.effects
        assert effects is not None
        portal.call(effects.on_turn_terminal_claimed)
        portal.call(
            effects.on_turn_finished,
            TurnResult(final_answer="確認しました。", error_code=None),
        )
        while (message := socket.receive_json())["type"] != "error":
            pass

    assert message == {"type": "error", "code": "runtime_generation_mismatch"}
    assert active.synthesized_texts == ["確認しました。"]
    assert active.selected_voices == [capabilities.voices[0].id]
    mismatch_log = next(
        record.message
        for record in caplog.records
        if "event=irodori_generation_mismatch" in record.message
    )
    assert "event_code=runtime_generation_mismatch" in mismatch_log
    assert capabilities.generation not in mismatch_log


def test_mid_conversation_unknown_synthesis_error_is_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_code = "private_backend_detail"
    private_message = "private backend host and token"
    capabilities = make_capabilities(2)
    caplog.set_level(logging.INFO)
    session = EffectsSession()
    discovery = FakeSynthesizer(capabilities)
    active = FakeSynthesizer(
        capabilities,
        synthesis_error=IrodoriError(private_message, code=private_code),
    )
    synthesizers = [discovery, active]
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizers.pop(0)),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        portal = client.portal
        assert portal is not None
        effects = session.effects
        assert effects is not None
        portal.call(effects.on_turn_terminal_claimed)
        portal.call(
            effects.on_turn_finished,
            TurnResult(final_answer="確認しました。", error_code=None),
        )
        while (message := socket.receive_json())["type"] != "error":
            pass

    assert message == {"type": "error", "code": "synthesis_failed"}
    assert private_code not in caplog.text
    assert private_message not in caplog.text


@pytest.mark.asyncio
async def test_failed_active_voice_selection_keeps_the_confirmed_voice() -> None:
    capabilities = make_capabilities(2)
    websocket = CapturingWebSocket()
    synthesizer = FakeSynthesizer(
        capabilities,
        selection_error=IrodoriError("fixture missing", code="voice_not_found"),
    )
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._voice_options = tuple(  # noqa: SLF001
        {"id": voice.id, "label": voice.label, "default": voice.default}
        for voice in capabilities.voices
    )
    connection._selected_voice_id = capabilities.voices[0].id  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001

    await connection._select_voice(capabilities.voices[1].id)  # noqa: SLF001

    assert websocket.messages == [{"type": "error", "code": "voice_not_available"}]
    assert connection._selected_voice_id == capabilities.voices[0].id  # noqa: SLF001


@pytest.mark.asyncio
async def test_audio_delivery_logs_correlated_bounded_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._generation = 9  # noqa: SLF001
    wav = b"private audio bytes"
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    await connection._deliver_audio(wav, audio_id, 4)  # noqa: SLF001

    started = next(
        record.message
        for record in caplog.records
        if "event=audio_delivery_started" in record.message
    )
    completed = next(
        record.message
        for record in caplog.records
        if "event=audio_delivery_completed" in record.message
    )
    for event in (started, completed):
        assert "audio_id=1" in event
        assert "boundary=browser_audio" in event
        assert "generation=4" in event
        assert f"wav_bytes={len(wav)}" in event
    assert "duration_ms=" in completed
    assert "result=ok" in completed
    assert wav.decode() not in caplog.text
    assert websocket.messages == [{"type": "audio", "audioId": 1, "generation": 4}]
    assert websocket.byte_messages == [wav]


@pytest.mark.asyncio
async def test_two_segments_share_unique_correlation_across_audio_stages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO)
    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    speech = connection._speech  # noqa: SLF001
    assert speech is not None
    caplog.clear()

    await speech.on_transcript(role="assistant", delta="一つ。二つ。", done=True)
    await asyncio.wait_for(speech.join(), timeout=1)
    for audio_id in (1, 2):
        for phase in ("started", "completed"):
            await connection._handle(  # noqa: SLF001
                json.dumps(
                    {
                        "type": "playback",
                        "phase": phase,
                        "audio_id": audio_id,
                        "generation": 0,
                        "context_state": "running",
                    },
                ),
            )

    def correlations(event_name: str) -> list[tuple[int, int]]:
        matched: list[tuple[int, int]] = []
        for record in caplog.records:
            if f"event={event_name}" not in record.message:
                continue
            attributes = dict(
                field.split("=", maxsplit=1) for field in record.message.split() if "=" in field
            )
            matched.append((int(attributes["audio_id"]), int(attributes["generation"])))
        return matched

    expected = [(1, 0), (2, 0)]
    assert correlations("synthesis_started") == expected
    assert correlations("synthesis_completed") == expected
    assert correlations("audio_delivery_started") == expected
    assert correlations("audio_delivery_completed") == expected
    assert correlations("browser_playback") == [(1, 0), (1, 0), (2, 0), (2, 0)]
    assert "一つ" not in caplog.text
    assert "二つ" not in caplog.text
    await connection._close_conversation_resources()  # noqa: SLF001


@pytest.mark.asyncio
async def test_audio_delivery_failure_is_logged_once_without_private_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = FailingAudioWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO)
    queue = SpeechQueue(
        FakeSynthesizer(),
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
        reserve_audio_id=connection._reserve_audio_id,  # noqa: SLF001
    )
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗する本文。", done=True)
    await queue.join()

    failures = [
        record.message
        for record in caplog.records
        if "event=audio_delivery_failed" in record.message
    ]
    assert len(failures) == 1
    assert "audio_id=1" in failures[0]
    assert "boundary=browser_audio" in failures[0]
    assert "generation=0" in failures[0]
    assert "result=error" in failures[0]
    assert "private websocket failure detail" not in caplog.text
    assert "失敗する本文" not in caplog.text
    assert queue.error_codes == ("audio_delivery_failed",)
    await queue.close()


@pytest.mark.asyncio
async def test_audio_delivery_cancellation_is_logged_without_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = BlockingAudioWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    delivery = asyncio.create_task(
        connection._deliver_audio(b"private", audio_id, 0),  # noqa: SLF001
    )
    await asyncio.wait_for(websocket.started.wait(), timeout=1)

    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery

    cancelled = next(
        record.message
        for record in caplog.records
        if "event=audio_delivery_cancelled" in record.message
    )
    assert "audio_id=1" in cancelled
    assert "boundary=browser_audio" in cancelled
    assert "duration_ms=" in cancelled
    assert "result=error" not in cancelled


@pytest.mark.asyncio
async def test_correlated_browser_playback_logs_only_validated_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    connection._generation = 4  # noqa: SLF001
    mark_audio_delivered(connection, 9, 4)

    handled = await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "failed",
                "audio_id": 9,
                "generation": 4,
                "context_state": "suspended",
            },
        ),
    )

    assert handled
    playback = next(
        record.message for record in caplog.records if "event=browser_playback " in record.message
    )
    assert "audio_id=9" in playback
    assert "context_state=suspended" in playback
    assert "generation=4" in playback
    assert "phase=failed" in playback
    assert "result=error" in playback
    assert "state=inactive" in playback


@pytest.mark.asyncio
async def test_playback_ack_requires_a_delivered_current_audio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    handled = await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": 999,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )

    assert handled
    assert "started" not in connection._playback_states.values()  # noqa: SLF001
    assert websocket.messages[-1] == {"type": "error", "code": "invalid_message"}
    assert "event=browser_playback_rejected" in caplog.text
    assert "event=browser_playback " not in caplog.text


@pytest.mark.asyncio
async def test_playback_ack_rejects_stale_replayed_and_out_of_order_transitions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    await connection._deliver_audio(b"RIFF-fixture", 1, 0)  # noqa: SLF001
    completed_before_started = {
        "type": "playback",
        "phase": "completed",
        "audio_id": 1,
        "generation": 0,
        "context_state": "running",
    }
    await connection._handle(json.dumps(completed_before_started))  # noqa: SLF001

    started = {**completed_before_started, "phase": "started"}
    await connection._handle(json.dumps(started))  # noqa: SLF001
    await connection._handle(json.dumps(started))  # noqa: SLF001

    completed = {**completed_before_started, "phase": "completed"}
    await connection._handle(json.dumps(completed))  # noqa: SLF001
    await connection._handle(json.dumps(completed))  # noqa: SLF001

    await connection._deliver_audio(b"RIFF-fixture", 2, 0)  # noqa: SLF001
    connection._generation = 1  # noqa: SLF001
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": 2,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )

    playback = [
        record.message for record in caplog.records if "event=browser_playback " in record.message
    ]
    rejected = [
        record.message
        for record in caplog.records
        if "event=browser_playback_rejected" in record.message
    ]
    assert ["phase=started" in event for event in playback] == [True, False]
    assert len(rejected) == 4
    assert "started" not in connection._playback_states.values()  # noqa: SLF001


@pytest.mark.asyncio
async def test_playback_ack_waits_for_in_flight_audio_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = GatedAudioWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    delivery = asyncio.create_task(
        connection._deliver_audio(b"RIFF-fixture", 1, 0),  # noqa: SLF001
    )
    await asyncio.wait_for(websocket.started.wait(), timeout=1)
    acknowledgement = asyncio.create_task(
        connection._handle(  # noqa: SLF001
            json.dumps(
                {
                    "type": "playback",
                    "phase": "started",
                    "audio_id": 1,
                    "generation": 0,
                    "context_state": "running",
                },
            ),
        ),
    )
    await asyncio.sleep(0)
    assert not acknowledgement.done()

    websocket.release.set()
    await asyncio.wait_for(delivery, timeout=1)
    assert await asyncio.wait_for(acknowledgement, timeout=1)

    assert websocket.messages == [{"type": "audio", "audioId": 1, "generation": 0}]
    assert list(connection._playback_states.values()) == ["started"]  # noqa: SLF001
    assert "event=browser_playback " in caplog.text
    assert "event=browser_playback_rejected" not in caplog.text


@pytest.mark.asyncio
async def test_first_playback_duration_matches_first_audio_and_does_not_rearm(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((1_000_000_000, 1_123_000_000))
    monkeypatch.setattr("moco.web.app.time.monotonic_ns", lambda: next(clock))
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    private_text = "ログに残してはいけない最初の応答"

    await start_recorded_agent_speech(connection, private_text)
    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, audio_id, 0)
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": audio_id + 1,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": audio_id,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )
    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "後続delta"),
    )

    playback = [
        record.message for record in caplog.records if "event=browser_playback " in record.message
    ]
    assert len(playback) == 1
    assert "duration_ms=123" in playback[0]
    assert private_text not in caplog.text
    assert connection._first_playback_started_ns is None  # noqa: SLF001
    assert connection._first_playback_audio_id is None  # noqa: SLF001
    assert connection._first_playback_generation is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_first_playback_duration_starts_at_non_empty_agent_result(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((2_000_000_000, 2_041_000_000))
    monkeypatch.setattr("moco.web.app.time.monotonic_ns", lambda: next(clock))
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    private_text = "ログに残してはいけない非空の応答"

    await connection._enqueue_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", ""),
    )
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (None, None, None)

    await start_recorded_agent_speech(connection, private_text)
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (2_000_000_000, None, None)
    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, audio_id, 0)
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": audio_id,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )

    playback = next(
        record.message for record in caplog.records if "event=browser_playback " in record.message
    )
    assert "duration_ms=41" in playback
    assert private_text not in caplog.text


@pytest.mark.asyncio
async def test_first_playback_duration_generation_mismatch_preserves_timing(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((3_000_000_000, 3_222_000_000))
    monkeypatch.setattr("moco.web.app.time.monotonic_ns", lambda: next(clock))
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    private_text = "generation不一致でもログに残さない応答"

    await start_recorded_agent_speech(connection, private_text)
    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, audio_id, 0)
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": audio_id,
                "generation": 1,
                "context_state": "running",
            },
        ),
    )

    assert "event=browser_playback_rejected" in caplog.text
    assert "event=browser_playback " not in caplog.text
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (3_000_000_000, audio_id, 0)

    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": audio_id,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )

    playback = [
        record.message for record in caplog.records if "event=browser_playback " in record.message
    ]
    assert "duration_ms=222" in playback[0]
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (None, None, None)
    assert private_text not in caplog.text


@pytest.mark.asyncio
async def test_first_playback_duration_out_of_order_completion_preserves_timing(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((4_000_000_000, 4_375_000_000))
    monkeypatch.setattr("moco.web.app.time.monotonic_ns", lambda: next(clock))
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)
    private_text = "非開始phaseでもログに残さない応答"

    await start_recorded_agent_speech(connection, private_text)
    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, audio_id, 0)
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "completed",
                "audio_id": audio_id,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )

    assert "event=browser_playback_rejected" in caplog.text
    assert "event=browser_playback " not in caplog.text
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (4_000_000_000, audio_id, 0)

    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": audio_id,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )

    playback = [
        record.message for record in caplog.records if "event=browser_playback " in record.message
    ]
    assert "duration_ms=375" in playback[0]
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (None, None, None)
    assert private_text not in caplog.text


@pytest.mark.asyncio
async def test_first_playback_duration_failed_phase_is_terminal(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((8_000_000, 20_000_000))
    monkeypatch.setattr("moco.web.app.time.monotonic_ns", lambda: next(clock))
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    await start_recorded_agent_speech(connection, "失敗する応答。")
    audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, audio_id, 0)
    await connection._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "failed",
                "audio_id": audio_id,
                "generation": 0,
                "context_state": "suspended",
            },
        ),
    )

    playback = next(
        record.message for record in caplog.records if "event=browser_playback " in record.message
    )
    assert "duration_ms" not in playback
    assert connection._first_playback_started_ns is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_first_playback_duration_discards_previous_turn_timing(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((1_000_000_000, 2_000_000_000, 2_250_000_000))
    monkeypatch.setattr("moco.web.app.time.monotonic_ns", lambda: next(clock))
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    speech = await start_recorded_agent_speech(connection, "古い応答")
    old_audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, old_audio_id, 0)
    await connection._invalidate_speech()  # noqa: SLF001
    speech.is_busy = False
    await start_recorded_agent_speech(connection, "新しい応答")
    new_audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, new_audio_id, 1)
    for audio_id, generation in ((old_audio_id, 0), (new_audio_id, 1)):
        await connection._handle(  # noqa: SLF001
            json.dumps(
                {
                    "type": "playback",
                    "phase": "started",
                    "audio_id": audio_id,
                    "generation": generation,
                    "context_state": "running",
                },
            ),
        )

    playback = [
        record.message for record in caplog.records if "event=browser_playback " in record.message
    ]
    assert len(playback) == 1
    assert "audio_id=2" in playback[0]
    assert "duration_ms=250" in playback[0]


@pytest.mark.asyncio
async def test_first_playback_duration_is_cleared_by_user_invalidation_and_close() -> None:
    invalidated = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    await start_recorded_agent_speech(invalidated, "中断される応答")
    invalidated_audio_id = invalidated._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(invalidated, invalidated_audio_id, 0)
    await invalidated._handle(  # noqa: SLF001
        json.dumps(
            {
                "type": "playback",
                "phase": "started",
                "audio_id": invalidated_audio_id,
                "generation": 0,
                "context_state": "running",
            },
        ),
    )
    assert list(invalidated._playback_states.values()) == ["started"]  # noqa: SLF001
    await invalidated._invalidate_speech()  # noqa: SLF001
    assert invalidated._first_playback_started_ns is None  # noqa: SLF001
    assert invalidated._first_playback_audio_id is None  # noqa: SLF001
    assert invalidated._first_playback_generation is None  # noqa: SLF001
    assert invalidated._playback_states == {}  # noqa: SLF001

    closed = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    await start_recorded_agent_speech(closed, "閉じられる応答")
    closed_audio_id = closed._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(closed, closed_audio_id, 0)
    await closed._close_conversation_resources()  # noqa: SLF001
    assert closed._first_playback_started_ns is None  # noqa: SLF001
    assert closed._first_playback_audio_id is None  # noqa: SLF001
    assert closed._first_playback_generation is None  # noqa: SLF001
    assert closed._playback_states == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_invalidate_before_cancel_cleanup_reaches_browser_first() -> None:
    websocket = InvalidationObservingWebSocket()
    synthesizer = CancellationCleanupSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()
    await speech.on_transcript(role="assistant", delta="取り消す応答。", done=True)
    await asyncio.wait_for(synthesizer.synthesized.wait(), timeout=1)

    user_transcript = asyncio.ensure_future(
        connection._enqueue_transcript(  # noqa: SLF001
            TranscriptEvent("delta", "thr_test", "user", "割り込み"),
        ),
    )
    await asyncio.wait_for(websocket.invalidation_sent.wait(), timeout=1)
    await asyncio.wait_for(synthesizer.cancellation_started.wait(), timeout=1)

    assert not user_transcript.done()
    assert websocket.messages[0] == {"type": "audio_invalidate", "generation": 1}
    synthesizer.release_cancellation.set()
    await asyncio.wait_for(user_transcript, timeout=1)
    await speech.close()


@pytest.mark.asyncio
async def test_invalidate_cancels_blocked_audio_delivery_before_sending_notice() -> None:
    websocket = GatedAudioWebSocket()
    synthesizer = FakeSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    speech = SpeechQueue(
        synthesizer,
        deliver=connection._deliver_audio,  # noqa: SLF001
        max_chars=80,
    )
    connection._speech = speech  # noqa: SLF001
    speech.start()
    await speech.on_transcript(role="assistant", delta="取り消す応答。", done=True)
    await asyncio.wait_for(websocket.started.wait(), timeout=1)

    invalidation = asyncio.create_task(connection._invalidate_speech())  # noqa: SLF001
    try:
        await asyncio.wait_for(asyncio.shield(invalidation), timeout=0.1)
    finally:
        websocket.release.set()
        await asyncio.wait_for(invalidation, timeout=1)
        await speech.close()

    assert websocket.messages == [
        {"type": "audio", "audioId": 1, "generation": 0},
        {"type": "audio_invalidate", "generation": 1},
    ]
    assert websocket.byte_messages == []


@pytest.mark.asyncio
async def test_invalidate_send_failure_still_invalidates_speech() -> None:
    websocket = InvalidationObservingWebSocket(fail=True)
    speech = RecordingSpeechInvalidation()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._speech = cast("SpeechQueue", speech)  # noqa: SLF001

    with pytest.raises(AudioDeliveryError):
        await connection._invalidate_speech()  # noqa: SLF001

    assert speech.reasons == ["user_transcript"]


@pytest.mark.asyncio
async def test_uncorrelated_stopped_playback_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    caplog.set_level(logging.INFO, logger=web_app.logger.name)

    handled = await connection._handle(  # noqa: SLF001
        json.dumps({"type": "playback", "active": False, "phase": "stopped"}),
    )

    assert handled
    assert websocket.messages == [{"type": "error", "code": "invalid_message"}]
    assert "event=browser_playback " not in caplog.text


@pytest.mark.asyncio
async def test_recreated_speech_queue_preserves_generation_across_audio_stages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = CapturingWebSocket()
    synthesizers = [FakeSynthesizer(), FakeSynthesizer()]
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizers.pop(0)),
    )
    caplog.set_level(logging.INFO)

    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    first_speech = connection._speech  # noqa: SLF001
    assert first_speech is not None
    caplog.clear()
    await first_speech.on_transcript(role="assistant", delta="再開前の音声。", done=True)
    await asyncio.wait_for(first_speech.join(), timeout=1)
    first_synthesis = next(
        record.message for record in caplog.records if "event=synthesis_started" in record.message
    )
    first_delivery = next(
        record.message
        for record in caplog.records
        if "event=audio_delivery_completed" in record.message
    )
    assert "audio_id=1" in first_synthesis
    assert "generation=0" in first_synthesis
    assert "audio_id=1" in first_delivery
    assert "generation=0" in first_delivery

    await connection._invalidate_speech()  # noqa: SLF001
    assert connection._generation == 1  # noqa: SLF001
    invalidated = next(
        record.message for record in caplog.records if "event=speech_invalidated" in record.message
    )
    assert "generation=1" in invalidated
    await connection._close_conversation_resources()  # noqa: SLF001

    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    speech = connection._speech  # noqa: SLF001
    assert speech is not None
    caplog.clear()
    await speech.on_transcript(role="assistant", delta="再開後の音声。", done=True)
    await asyncio.wait_for(speech.join(), timeout=1)

    synthesis = next(
        record.message for record in caplog.records if "event=synthesis_started" in record.message
    )
    delivery = next(
        record.message
        for record in caplog.records
        if "event=audio_delivery_completed" in record.message
    )
    assert "generation=1" in synthesis
    assert "audio_id=2" in synthesis
    assert "generation=1" in delivery
    assert "audio_id=2" in delivery
    audio_headers = [message for message in websocket.messages if message.get("type") == "audio"]
    assert audio_headers == [
        {"type": "audio", "audioId": 1, "generation": 0},
        {"type": "audio", "audioId": 2, "generation": 1},
    ]
    assert "再開後の音声" not in caplog.text
    await connection._close_conversation_resources()  # noqa: SLF001


@pytest.mark.asyncio
async def test_explicit_voice_is_not_reselected_after_it_disappears() -> None:
    capabilities = make_capabilities(2)
    websocket = CapturingWebSocket()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer(capabilities)),
    )
    await connection._cache_capabilities(capabilities)  # noqa: SLF001
    await connection._select_voice(capabilities.voices[1].id)  # noqa: SLF001

    assert await connection._cache_capabilities(make_capabilities(1)) == "voice_not_found"  # noqa: SLF001
    assert await connection._cache_capabilities(capabilities) is None  # noqa: SLF001

    assert connection._selected_voice_id is None  # noqa: SLF001
    assert connection._voice_selection_error == "voice_not_found"  # noqa: SLF001


@pytest.mark.asyncio
async def test_duplicate_start_sends_already_started_once() -> None:
    websocket = CapturingWebSocket()
    session = FakeSession()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001

    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001

    assert websocket.messages == [{"type": "error", "code": "already_started"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "error_code"),
    [
        (FakeSession(), "codex_realtime_error"),
        (InvalidNotificationSession(), "invalid_realtime_event"),
    ],
)
async def test_terminal_realtime_failure_closes_resources_and_expires_state(
    session: FakeSession,
    error_code: str,
) -> None:
    websocket = CapturingWebSocket()
    synthesizer = FakeSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )
    connection._session = cast("RealtimeSession", session)  # noqa: SLF001
    connection._synthesizer = synthesizer  # noqa: SLF001
    if error_code == "codex_realtime_error":
        await session.emit(RealtimeErrorEvent("thr_test", "terminal"))

    await connection._consume_notifications()  # noqa: SLF001

    assert websocket.messages == [
        {"type": "error", "code": error_code},
        {
            "type": "state",
            "state": "idle_expired",
            "canCancel": False,
            "hotkeys": {
                "enabled": True,
                "startListening": "f1",
                "stopListening": "f2",
            },
            "voice": {
                "selected": None,
                "options": [],
                "ready": False,
                "readiness": "loading",
            },
            "conditioning": {
                "captionMode": "off",
                "deliveryCaptionSupported": False,
                "emojiSupported": False,
            },
        },
    ]
    assert session.closed
    assert synthesizer.closed


def test_voice_model_can_be_selected_before_or_during_a_conversation() -> None:
    capabilities = make_capabilities(3)
    synthesizers: list[FakeSynthesizer] = []

    def synthesizer_factory() -> WebSynthesizer:
        synthesizer = FakeSynthesizer(capabilities)
        synthesizers.append(synthesizer)
        return cast("WebSynthesizer", synthesizer)

    settings = MocoSettings(irodori=IrodoriSettings(speaker=capabilities.voices[1].aliases[0]))
    app = create_app(
        settings,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=synthesizer_factory,
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        ready = receive_ready_catalog(socket)
        assert ready["voice"] == browser_voice(capabilities, capabilities.voices[1].id)

        socket.send_json({"type": "select_voice", "voice_id": capabilities.voices[2].id})
        assert socket.receive_json() == {
            "type": "voice",
            "selected": capabilities.voices[2].id,
        }
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        assert synthesizers[-1].selected_voices == [capabilities.voices[2].id]

        socket.send_json({"type": "select_voice", "voice_id": "unknown-fixture"})
        assert socket.receive_json() == {
            "type": "error",
            "code": "voice_not_available",
        }


def test_idle_expiry_keeps_socket_and_next_start_builds_fresh_resources() -> None:
    sessions: list[FakeSession] = []
    synthesizers: list[FakeSynthesizer] = []

    def session_factory() -> RealtimeSession:
        session = FakeSession()
        sessions.append(session)
        return cast("RealtimeSession", session)

    def synthesizer_factory() -> WebSynthesizer:
        synthesizer = FakeSynthesizer()
        synthesizers.append(synthesizer)
        return cast("WebSynthesizer", synthesizer)

    app = create_app(
        MocoSettings(runtime=RuntimeSettings(idle_timeout_seconds=0.02)),
        session_factory=session_factory,
        synthesizer_factory=synthesizer_factory,
        capability_token=CAPABILITY,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()

        assert socket.receive_json()["state"] == "idle_expired"
        socket.send_json({"type": "control", "control": "listen_stop"})
        assert socket.receive_json()["state"] == "idle_expired"
        assert sessions[0].closed
        assert synthesizers[-1].closed

        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json()["type"] == "sdp_answer"
        assert socket.receive_json()["state"] == "ready"
        assert len(sessions) == 2
        assert len(synthesizers) == 3


def test_only_one_operator_client_is_admitted() -> None:
    app = create_app(capability_token=CAPABILITY)
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as first,
        websocket_context(client) as second,
    ):
        first.receive_json()
        message = second.receive_json()
        assert message["code"] == "single_operator_only"


def test_browser_keyboard_fallback_is_advertised_when_global_listener_is_inactive() -> None:
    app = create_app(
        global_hotkeys_active=False,
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        ready = socket.receive_json()

    assert ready["hotkeys"]["enabled"] is False


def test_failed_conversation_start_closes_partial_resources() -> None:
    session = FailingSession()
    synthesizer = FakeSynthesizer()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        receive_ready_catalog(socket)
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {
            "type": "error",
            "code": "conversation_start_failed",
        }
        assert socket.receive_json()["state"] == "idle_expired"

    assert session.closed
    assert synthesizer.closed


@pytest.mark.asyncio
async def test_connection_loss_during_initial_start_is_not_hidden_as_idle_expiry() -> None:
    websocket = CapturingWebSocket()
    session = ConnectionLostStartSession()
    synthesizer = FakeSynthesizer()
    connection = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", websocket),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
    )

    await connection._start(StartMessage(sdp="offer-sdp"))  # noqa: SLF001
    effects = tuple(connection._effect_tasks)  # noqa: SLF001
    if effects:
        await asyncio.gather(*effects)

    states = [message["state"] for message in websocket.messages if message["type"] == "state"]
    assert states[-1] == "connection_lost"
    assert session.closed
    assert synthesizer.closed
