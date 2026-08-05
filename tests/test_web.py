from __future__ import annotations

import asyncio
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
from moco.config import IrodoriSettings, MocoSettings, RuntimeSettings, ServerSettings
from moco.runtime.hotkeys import Control
from moco.speech.irodori import IrodoriError
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

    async def capabilities(self) -> CapabilitiesResponse:
        index = min(self.capability_calls, len(self.capability_responses) - 1)
        self.capability_calls += 1
        response = self.capability_responses[index]
        if isinstance(response, Exception):
            raise response
        return cast("CapabilitiesResponse", response)

    async def synthesize(self, text: str) -> bytes:
        self.synthesized_texts.append(text)
        if self.synthesis_error is not None:
            raise self.synthesis_error
        return b"RIFF\x04\x00\x00\x00WAVE"

    def select_voice(self, voice_id: str) -> None:
        if self.selection_error is not None:
            raise self.selection_error
        self.selected_voices.append(voice_id)

    async def close(self) -> None:
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

    async def send_json(self, message: dict[str, object]) -> None:
        self.messages.append(message)


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
        assert socket.receive_json()["delta"] == "一"

        portal.call(
            sessions[0].emit,
            TranscriptEvent("delta", "thr_test", "user", "つ"),
        )
        assert socket.receive_json()["delta"] == "つ"
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
        assert socket.receive_json()["delta"] == "次"


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
