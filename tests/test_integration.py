from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from irodori_tts_infra.contracts import (
    CapabilitiesResponse,
    SynthesisRequest,
    SynthesisResult,
    VoiceCapability,
)
from starlette.websockets import WebSocket

from moco.codex.agent import AgentSession
from moco.codex.approval import ApprovalDecision, FileChangeApprovalReview, FileChangeKind
from moco.codex.broker import InteractionBroker, ReviewEnvelope
from moco.codex.connection import CodexConnectionSupervisor
from moco.codex.session import RealtimeEvent, TranscriptEvent
from moco.config import AgentProfileMode, MocoSettings
from moco.errors import AgentTurnErrorCode, CodexProcessExitedError
from moco.platform import CodexCommand
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
from moco.speech.irodori import IrodoriClient, IrodoriSynthesizer
from moco.web import app as web_app
from moco.web.app import RealtimeSession, WebSynthesizer, create_app
from test_codex_agent import capabilities, effective_contract
from test_codex_approval import file_change_patch_contract
from test_coordinator import EffectsRecorder

pytestmark = pytest.mark.integration


def interaction_command() -> CodexCommand:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"
    return CodexCommand((sys.executable, str(script), "--scenario=interaction"))


class IntegrationEffects(EffectsRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.turn_finished = asyncio.Event()

    def on_turn_finished(self, result: TurnResult) -> None:
        super().on_turn_finished(result)
        self.turn_finished.set()


async def wait_for_active_turn(
    agent: AgentSession,
    supervisor: CodexConnectionSupervisor,
) -> str:
    for _ in range(20):
        snapshot = await supervisor.request("test/interaction/snapshot", {})
        assert isinstance(snapshot, dict)
        active_turn_id = agent.active_turn_id
        if active_turn_id is not None and snapshot["activeTurnId"] == active_turn_id:
            return active_turn_id
    message = "fake Codex turn did not become active"
    raise AssertionError(message)


async def test_fake_codex_hands_off_once_and_steers_the_same_active_turn() -> None:
    supervisor = CodexConnectionSupervisor(interaction_command(), request_timeout=1)
    await supervisor.start()
    agent = AgentSession(
        supervisor,
        effective_contract(),
        capabilities(),
        Path.cwd(),
        AgentProfileMode.READ_ONLY,
    )
    effects = IntegrationEffects()
    coordinator = InteractionCoordinator(
        agent,
        steer_available=True,
        effects=effects,
    )
    coordinator.connection_changed(ConnectionState.READY)
    try:
        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("first") is HandoffDisposition.STARTED
        assert await coordinator.consume_user_final("duplicate") is HandoffDisposition.IGNORED
        turn_id = await wait_for_active_turn(agent, supervisor)

        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("steer") is HandoffDisposition.STEERED

        snapshot = await supervisor.request("test/interaction/snapshot", {})
        assert snapshot == {
            "threadId": "integration-thread-1",
            "activeTurnId": turn_id,
            "threadStartCount": 1,
            "turnStartCount": 1,
            "steerCount": 1,
            "interruptCount": 0,
        }
        await supervisor.request("test/interaction/complete", {})
        await asyncio.wait_for(effects.turn_finished.wait(), timeout=1)
        assert effects.results == [TurnResult(final_answer="integration final", error_code=None)]
    finally:
        await agent.close()
        await supervisor.close()


class OperatorCapture:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_json(self, message: dict[str, object]) -> None:
        self.messages.append(message)


async def test_fake_codex_agent_progress_reaches_operator_as_safe_frames_only() -> None:
    supervisor = CodexConnectionSupervisor(interaction_command(), request_timeout=1)
    capture = OperatorCapture()
    browser = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", capture),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", object()),
        synthesizer_factory=lambda: cast("WebSynthesizer", object()),
    )
    agent = AgentSession(
        supervisor,
        effective_contract(),
        capabilities(),
        Path.cwd(),
        AgentProfileMode.READ_ONLY,
        activity_sink=browser.on_agent_activity,
    )
    coordinator = InteractionCoordinator(
        agent,
        steer_available=True,
        effects=browser,
    )
    await supervisor.start()
    try:
        coordinator.connection_changed(ConnectionState.READY)
        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("progress") is HandoffDisposition.STARTED
        await wait_for_active_turn(agent, supervisor)
        await supervisor.request("test/interaction/complete", {})
        for _ in range(20):
            activities = [
                message for message in capture.messages if message.get("type") == "activity"
            ]
            if len(activities) == 6:
                break
            await asyncio.sleep(0)

        assert [
            {key: value for key, value in message.items() if key != "occurredAtMs"}
            for message in activities
        ] == [
            {
                "type": "activity",
                "kind": "turn",
                "source": "agent",
                "phase": "started",
                "label": "応答処理",
            },
            {
                "type": "activity",
                "kind": "work",
                "phase": "started",
                "label": "コマンド実行",
            },
            {
                "type": "activity",
                "kind": "work",
                "phase": "completed",
                "label": "コマンド実行",
            },
            {
                "type": "activity",
                "kind": "work",
                "phase": "started",
                "label": "Web 検索",
            },
            {
                "type": "activity",
                "kind": "work",
                "phase": "completed",
                "label": "Web 検索",
            },
            {
                "type": "activity",
                "kind": "turn",
                "source": "agent",
                "phase": "completed",
                "label": "応答処理",
            },
        ]
        assert all(type(message.get("occurredAtMs")) is int for message in activities)
        rendered = repr(activities)
        for private_detail in (
            "integration-thread-1",
            "integration-turn-1",
            "private/integration-command",
            "PRIVATE_WEB_QUERY",
            "PRIVATE_REASONING",
            "PRIVATE_MCP_ARGUMENT",
        ):
            assert private_detail not in rendered
    finally:
        await agent.close()
        await supervisor.close()
        await browser.close()


async def test_fake_codex_patch_is_correlated_before_adjacent_file_approval() -> None:
    patch_contract = file_change_patch_contract()
    contract = replace(
        effective_contract(),
        server_requests=patch_contract.server_requests,
        approval_profiles=patch_contract.approval_profiles,
        file_change_patch_profile=patch_contract.file_change_patch_profile,
    )
    supervisor = CodexConnectionSupervisor(interaction_command(), request_timeout=1)
    interaction = InteractionBroker(contract)
    agent = AgentSession(
        supervisor,
        contract,
        capabilities(),
        Path.cwd(),
        AgentProfileMode.READ_ONLY,
    )
    interaction.bind_active_turn_check(agent.owns_active_turn)
    counts: list[int] = []
    interaction.bind_pending_count_changed(counts.append)
    reviewer = interaction.connect_reviewer()
    interaction.register_approval_handlers(supervisor)
    await supervisor.start()
    try:
        turn = asyncio.create_task(agent.start_turn("review the integration patch"))
        await wait_for_active_turn(agent, supervisor)
        trigger = asyncio.create_task(supervisor.request("test/interaction/patch-approval", {}))
        envelope = await asyncio.wait_for(anext(reviewer), timeout=1)
        assert isinstance(envelope, ReviewEnvelope)
        assert isinstance(envelope.review, FileChangeApprovalReview)
        assert [(change.path, change.kind) for change in envelope.review.changes] == [
            ("/private/integration-target.txt", FileChangeKind.UPDATE)
        ]
        assert counts == [1]

        interaction.decide(reviewer, envelope.handle, ApprovalDecision.ACCEPT)
        assert await trigger == {"approvalResponse": {"decision": "accept"}}
        assert counts == [1, 0]
        await supervisor.request("test/interaction/complete", {})
        assert await turn == "integration final"
    finally:
        interaction.close()
        await agent.close()
        await supervisor.close()


async def test_fake_codex_connection_loss_discards_queue_without_replay() -> None:
    supervisor = CodexConnectionSupervisor(interaction_command(), request_timeout=1)
    agent = AgentSession(
        supervisor,
        effective_contract(),
        capabilities(),
        Path.cwd(),
        AgentProfileMode.READ_ONLY,
    )
    effects = IntegrationEffects()
    coordinator = InteractionCoordinator(
        agent,
        steer_available=False,
        effects=effects,
    )
    supervisor.register_terminal_callback(coordinator.connection_lost)
    await supervisor.start()
    coordinator.connection_changed(ConnectionState.READY)
    try:
        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("active") is HandoffDisposition.STARTED
        await wait_for_active_turn(agent, supervisor)

        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("queued") is HandoffDisposition.QUEUED
        coordinator.listen_started()
        coordinator.listen_stopped()
        assert await coordinator.consume_user_final("busy") is HandoffDisposition.BUSY

        with pytest.raises(CodexProcessExitedError):
            await supervisor.request("test/interaction/connection-loss", {})
        await asyncio.wait_for(effects.turn_finished.wait(), timeout=1)

        assert effects.results == [
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN)
        ]
        assert effects.terminal_claims == 1
        assert coordinator.snapshot == InteractionSnapshot(
            connection=ConnectionState.DISCONNECTED,
            voice=VoiceState.IDLE,
            task=TaskState.FAILED,
            speech=SpeechState.SILENT,
        )
        assert not agent.reusable
    finally:
        await agent.close()
        await supervisor.close()


def make_capabilities(count: int) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        generation="private-integration-generation",
        ready=True,
        readiness="ready",
        voices=tuple(
            VoiceCapability(
                id=f"integration-id-{index}",
                label=f"Integration voice {index}",
                aliases=(f"private-integration-alias-{index}",),
                default=index == 0,
            )
            for index in range(count)
        ),
    )


class FinalSpeechSession:
    active_turn_id: str | None = None

    def __init__(self) -> None:
        self.voice_active = True
        self.voice_generation = 1
        self.interaction_snapshot = InteractionSnapshot(
            connection=ConnectionState.READY,
            voice=VoiceState.IDLE,
            task=TaskState.NONE,
            speech=SpeechState.SILENT,
        )
        self.effects: InteractionEffects | None = None

    def bind_effects(self, effects: InteractionEffects) -> None:
        self.effects = effects

    async def start(self, _sdp: str) -> str:
        return "answer-sdp"

    async def notifications(
        self,
        expected_generation: int | None = None,
    ) -> AsyncIterator[RealtimeEvent]:
        assert expected_generation in {None, self.voice_generation}
        yield TranscriptEvent("done", "thr_test", "assistant", "Voice の応答。")
        effects = self.effects
        assert effects is not None
        effects.on_turn_terminal_claimed()
        effects.on_turn_finished(TurnResult(final_answer="Agent の最終回答。", error_code=None))
        await asyncio.Event().wait()

    def speech_changed(self, state: SpeechState) -> None:
        self.interaction_snapshot = replace(self.interaction_snapshot, speech=state)

    def claim_close(self) -> None:
        return None

    async def close(self) -> None:
        self.voice_active = False


class BoundaryIrodoriClient:
    def __init__(self, capabilities: CapabilitiesResponse) -> None:
        self.capabilities_response = capabilities
        self.requests: list[SynthesisRequest] = []
        self.closed = False

    async def capabilities(self) -> CapabilitiesResponse:
        return self.capabilities_response

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        self.requests.append(request)
        return SynthesisResult(
            segment_index=0,
            wav_bytes=b"RIFF\x04\x00\x00\x00WAVE",
            elapsed_seconds=0.1,
        )

    async def aclose(self) -> None:
        self.closed = True


def test_agent_final_reaches_browser_as_irodori_wav_without_speaking_voice_reply() -> None:
    capabilities = make_capabilities(3)
    discovery = BoundaryIrodoriClient(capabilities)
    active = BoundaryIrodoriClient(capabilities)
    clients = [discovery, active]
    capability_value = "integration-capability"

    def synthesizer_factory() -> WebSynthesizer:
        assert clients, "synthesizer_factory was called more than twice"
        synthesizer = IrodoriSynthesizer(
            cast("IrodoriClient", clients.pop(0)),
            settings=MocoSettings(),
        )
        return cast("WebSynthesizer", synthesizer)

    app = create_app(
        session_factory=lambda: cast("RealtimeSession", FinalSpeechSession()),
        synthesizer_factory=synthesizer_factory,
        capability_token=capability_value,
    )
    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        client.websocket_connect(
            "/ws",
            headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
            },
            subprotocols=["moco", f"moco.capability.{capability_value}"],
        ) as socket,
    ):
        initial = socket.receive_json()
        catalog = socket.receive_json()
        selected = capabilities.voices[1]
        socket.send_json({"type": "select_voice", "voice_id": selected.id})
        selected_message = socket.receive_json()
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        connecting = socket.receive_json()
        sdp_answer = socket.receive_json()
        ready = socket.receive_json()

        transcript = socket.receive_json()
        audio = socket.receive_json()
        wav = socket.receive_bytes()

    assert initial["voice"] == {
        "selected": None,
        "options": [],
        "ready": False,
        "readiness": "loading",
    }
    assert initial["conditioning"] == {
        "captionMode": "off",
        "deliveryCaptionSupported": False,
        "emojiSupported": False,
    }
    assert catalog["voice"] == {
        "selected": capabilities.voices[0].id,
        "options": [
            {"id": voice.id, "label": voice.label, "default": voice.default}
            for voice in capabilities.voices
        ],
        "ready": True,
        "readiness": "ready",
    }
    rendered_catalog = repr(catalog)
    assert capabilities.generation not in rendered_catalog
    assert all(
        alias not in rendered_catalog for voice in capabilities.voices for alias in voice.aliases
    )
    assert selected_message == {"type": "voice", "selected": selected.id}
    assert connecting["state"] == "connecting"
    assert sdp_answer == {"type": "sdp_answer", "sdp": "answer-sdp"}
    assert ready["state"] == "ready"
    assert transcript == {
        "type": "transcript",
        "role": "assistant",
        "text": "Agent の最終回答。",
        "done": True,
    }
    assert audio["type"] == "audio"
    assert wav == b"RIFF\x04\x00\x00\x00WAVE"
    assert discovery.requests == []
    assert len(active.requests) == 1
    request = active.requests[0]
    assert request.text == "Agent の最終回答。"
    assert request.voice_id == selected.id
    assert request.if_generation == capabilities.generation
    assert discovery.closed
    assert active.closed
