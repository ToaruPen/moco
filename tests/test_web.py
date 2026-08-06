from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import cast

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

from moco.codex.session import (
    ActivityEvent,
    RealtimeErrorEvent,
    RealtimeEvent,
    ReasoningSummaryEvent,
    TranscriptEvent,
)
from moco.config import (
    IrodoriSettings,
    MocoSettings,
    RuntimeSettings,
    ServerSettings,
    SpeechSettings,
)
from moco.runtime.hotkeys import Control
from moco.speech.irodori import (
    _MAX_CAPABILITY_VOICES,
    IrodoriClient,
    IrodoriError,
    IrodoriSynthesizer,
)
from moco.speech.queue import SpeechQueue
from moco.web import app as web_app
from moco.web.app import RealtimeSession, WebSynthesizer, create_app
from moco.web.messages import StartMessage

CAPABILITY = "test-capability"


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
        self.active_turn_id: str | None = None
        self._events: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()

    async def start(self, sdp: str) -> str:
        assert sdp == "offer-sdp"
        self.start_calls += 1
        return "answer-sdp"

    async def notifications(self) -> AsyncIterator[RealtimeEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    async def emit(self, event: RealtimeEvent) -> None:
        await self._events.put(event)

    async def close(self) -> None:
        self.closed = True
        await self._events.put(None)


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


class InvalidNotificationSession(FakeSession):
    async def notifications(self) -> AsyncIterator[RealtimeEvent]:
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


def mark_audio_delivered(
    connection: web_app._BrowserConnection,
    audio_id: int,
    generation: int,
) -> None:
    connection._playback_states[(audio_id, generation)] = "delivered"  # noqa: SLF001


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


def test_streamed_assistant_response_displays_and_synthesizes_complete_text() -> None:
    session = FakeSession()
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
        streamed = socket.receive_json()
        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "assistant", "確認します。"),
        )
        completed = socket.receive_json()

        assert streamed == {
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

        async def wait_for_synthesis() -> None:
            try:
                await asyncio.wait_for(synthesizer.synthesized.wait(), timeout=1)
            except TimeoutError:
                pytest.fail(
                    "assistant response was not synthesized within one second; "
                    f"calls={synthesizer.synthesized_texts!r}",
                )

        portal.call(wait_for_synthesis)

    assert synthesizer.synthesized_texts == ["確認します。"]


def test_first_segment_setting_synthesizes_soft_break_then_remainder_fifo() -> None:
    capabilities = make_capabilities(3, default_index=1)
    runtime_default = next(voice for voice in capabilities.voices if voice.default)
    settings = MocoSettings(
        speech=SpeechSettings(first_segment_soft_break_min_chars=18),
    )
    session = FakeSession()
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

        portal.call(
            session.emit,
            TranscriptEvent("delta", "thr_test", "assistant", first_segment),
        )
        assert socket.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": first_segment,
            "done": False,
        }
        portal.call(
            session.emit,
            TranscriptEvent(
                "done",
                "thr_test",
                "assistant",
                first_segment + remainder,
            ),
        )

        portal.call(asyncio.wait_for, synthesizer.second_synthesized.wait(), 1)

    assert runtime_default.id in synthesizer.selected_voices
    assert synthesizer.synthesized_texts == [first_segment, remainder]


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


def test_control_emoji_split_across_deltas_is_sanitized_after_accumulation() -> None:
    session = FakeSession()
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
            TranscriptEvent("delta", "thr_test", "assistant", "😮"),
        )
        assert socket.receive_json()["text"] == ""
        portal.call(
            session.emit,
            TranscriptEvent("delta", "thr_test", "assistant", "\u200d💨確認"),
        )
        assert socket.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "確認",
            "done": False,
        }
        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "assistant", "😮‍💨確認"),
        )
        assert socket.receive_json() == {
            "type": "transcript",
            "role": "assistant",
            "text": "確認",
            "done": True,
        }

        async def wait_for_synthesis() -> None:
            try:
                await asyncio.wait_for(synthesizer.synthesized.wait(), timeout=1)
            except TimeoutError:
                pytest.fail(
                    "raw assistant cue was not synthesized within one second; "
                    f"calls={synthesizer.synthesized_texts!r}",
                )

        portal.call(wait_for_synthesis)

    assert synthesizer.synthesized_texts == ["😮‍💨確認"]


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
        portal.call(
            session.emit,
            ReasoningSummaryEvent(
                "thr_test",
                "turn-1",
                "r-1",
                "設定を確認中。",
            ),
        )

        turn = socket.receive_json()
        assert turn["type"] == "activity"
        assert turn["kind"] == "turn"
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
        summary = socket.receive_json()
        assert summary["type"] == "reasoning_summary"
        assert summary["itemId"] == "r-1"
        assert summary["delta"] == "設定を確認中。"
        assert before_ms <= summary["occurredAtMs"] <= int(time.time() * 1000)
        rendered = repr((turn, summary))
        assert "thread" not in rendered
        assert "turn-1" not in rendered


def test_reports_synthesis_start_and_completion() -> None:
    session = FakeSession()
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
        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "assistant", "確認しました。"),
        )
        assert socket.receive_json()["type"] == "transcript"
        portal.call(synthesizer.started.wait)

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
    session = FakeSession()
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
        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "assistant", "確認しました。"),
        )
        assert socket.receive_json()["type"] == "transcript"
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
    session = FakeSession()
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
        portal.call(
            session.emit,
            TranscriptEvent("done", "thr_test", "assistant", "確認しました。"),
        )
        assert socket.receive_json()["type"] == "transcript"
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
    connection._lifecycle.enable()  # noqa: SLF001
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
    assert not connection._lifecycle.is_busy  # noqa: SLF001
    assert connection._lifecycle.state.value == "ready"  # noqa: SLF001
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
    connection._lifecycle.enable()  # noqa: SLF001
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
    assert not connection._lifecycle.is_busy  # noqa: SLF001


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
    connection._lifecycle.enable()  # noqa: SLF001
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
    assert connection._lifecycle.is_busy  # noqa: SLF001
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

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", private_text),
    )
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
    await connection._handle_transcript(  # noqa: SLF001
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
async def test_first_playback_duration_starts_at_first_non_empty_assistant_event(
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

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", ""),
    )
    timing = (
        connection._first_playback_started_ns,  # noqa: SLF001
        connection._first_playback_audio_id,  # noqa: SLF001
        connection._first_playback_generation,  # noqa: SLF001
    )
    assert timing == (None, None, None)

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", private_text),
    )
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

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", private_text),
    )
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

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", private_text),
    )
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

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "assistant", "失敗する応答。"),
    )
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

    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "古い応答"),
    )
    old_audio_id = connection._reserve_audio_id()  # noqa: SLF001
    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("done", "thr_test", "assistant", "古い応答です。"),
    )
    await connection._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "新しい応答"),
    )
    new_audio_id = connection._reserve_audio_id()  # noqa: SLF001
    mark_audio_delivered(connection, old_audio_id, 0)
    mark_audio_delivered(connection, new_audio_id, 0)
    for audio_id in (old_audio_id, new_audio_id):
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
    assert "duration_ms" not in playback[0]
    assert "duration_ms=250" in playback[1]


@pytest.mark.asyncio
async def test_first_playback_duration_is_cleared_by_user_invalidation_and_close() -> None:
    invalidated = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    await invalidated._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "中断される応答"),
    )
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
    was_busy = invalidated._lifecycle.is_busy  # noqa: SLF001
    assert was_busy
    await invalidated._invalidate_speech()  # noqa: SLF001
    assert invalidated._first_playback_started_ns is None  # noqa: SLF001
    assert invalidated._first_playback_audio_id is None  # noqa: SLF001
    assert invalidated._first_playback_generation is None  # noqa: SLF001
    assert invalidated._playback_states == {}  # noqa: SLF001
    assert not invalidated._lifecycle.is_busy  # noqa: SLF001

    closed = web_app._BrowserConnection(  # noqa: SLF001
        cast("WebSocket", CapturingWebSocket()),
        settings=MocoSettings(),
        global_hotkeys_active=True,
        session_factory=lambda: cast("RealtimeSession", FakeSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", FakeSynthesizer()),
    )
    await closed._handle_transcript(  # noqa: SLF001
        TranscriptEvent("delta", "thr_test", "assistant", "閉じられる応答"),
    )
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

    user_transcript = asyncio.create_task(
        connection._handle_transcript(  # noqa: SLF001
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
