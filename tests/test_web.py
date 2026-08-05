from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from irodori_tts_infra.contracts import HealthResponse
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
from moco.web import app as web_app
from moco.web.app import RealtimeSession, WebSynthesizer, create_app

CAPABILITY = "test-capability"


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.active_turn_id: str | None = None
        self._events: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()

    async def start(self, sdp: str) -> str:
        assert sdp == "offer-sdp"
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
    def __init__(self) -> None:
        self.closed = False
        self.selected_speakers: list[str | None] = []

    async def health(self) -> HealthResponse:
        return HealthResponse(model_loaded=True)

    async def synthesize(self, text: str) -> bytes:
        del text
        return b"RIFF\x04\x00\x00\x00WAVE"

    def select_speaker(self, speaker: str | None) -> None:
        self.selected_speakers.append(speaker)

    async def close(self) -> None:
        self.closed = True


class BlockingSynthesizer(FakeSynthesizer):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(self, text: str) -> bytes:
        del text
        self.started.set()
        await self.release.wait()
        return b"RIFF\x04\x00\x00\x00WAVE"


class NotReadySynthesizer(FakeSynthesizer):
    async def health(self) -> HealthResponse:
        return HealthResponse(model_loaded=False)


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
        ready = socket.receive_json()
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
        socket.receive_json()
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
        socket.receive_json()
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
        socket.receive_json()
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
            "voice": {"selected": None, "options": []},
        },
    ]
    assert session.closed
    assert synthesizer.closed


def test_voice_model_can_be_selected_before_or_during_a_conversation() -> None:
    synthesizers: list[FakeSynthesizer] = []

    def synthesizer_factory() -> WebSynthesizer:
        synthesizer = FakeSynthesizer()
        synthesizers.append(synthesizer)
        return cast("WebSynthesizer", synthesizer)

    settings = MocoSettings(
        irodori=IrodoriSettings(
            speaker="kasumi",
            speakers=("alternate",),
        ),
    )
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
        ready = socket.receive_json()
        assert ready["voice"] == {
            "selected": "kasumi",
            "options": ["kasumi", "alternate"],
        }

        socket.send_json({"type": "select_voice", "speaker": "alternate"})
        assert socket.receive_json() == {
            "type": "voice",
            "selected": "alternate",
        }
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()
        assert synthesizers[0].selected_speakers == ["alternate"]

        socket.send_json({"type": "select_voice", "speaker": None})
        assert socket.receive_json() == {"type": "voice", "selected": None}
        assert synthesizers[0].selected_speakers == ["alternate", None]

        socket.send_json({"type": "select_voice", "speaker": "unknown"})
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
        socket.receive_json()
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()

        assert socket.receive_json()["state"] == "idle_expired"
        socket.send_json({"type": "control", "control": "listen_stop"})
        assert socket.receive_json()["state"] == "idle_expired"
        assert sessions[0].closed
        assert synthesizers[0].closed

        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json()["type"] == "sdp_answer"
        assert socket.receive_json()["state"] == "ready"
        assert len(sessions) == 2
        assert len(synthesizers) == 2


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
        socket.receive_json()
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {
            "type": "error",
            "code": "conversation_start_failed",
        }
        assert socket.receive_json()["state"] == "idle_expired"

    assert session.closed
    assert synthesizer.closed


def test_unready_synthesizer_returns_terminal_state() -> None:
    session = FakeSession()
    synthesizer = NotReadySynthesizer()
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", session),
        synthesizer_factory=lambda: cast("WebSynthesizer", synthesizer),
        capability_token=CAPABILITY,
    )

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        websocket_context(client) as socket,
    ):
        socket.receive_json()
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {"type": "error", "code": "irodori_not_ready"}
        assert socket.receive_json()["state"] == "idle_expired"

    assert session.closed
    assert synthesizer.closed
