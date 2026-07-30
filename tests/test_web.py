from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from irodori_tts_infra.contracts import HealthResponse
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from moco.codex.session import RealtimeEvent
from moco.config import MocoSettings, RuntimeSettings
from moco.runtime.hotkeys import Control
from moco.web.app import RealtimeSession, WebSynthesizer, create_app

CAPABILITY = "test-capability"


class FakeSession:
    def __init__(self) -> None:
        self.cancelled = False
        self.closed = False
        self.active_turn_id: str | None = None
        self._events: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()

    async def start(self, sdp: str) -> str:
        assert sdp == "offer-sdp"
        return "answer-sdp"

    async def notifications(self) -> AsyncIterator[RealtimeEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    async def cancel_current(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True
        await self._events.put(None)


class FakeSynthesizer:
    def __init__(self) -> None:
        self.closed = False

    async def health(self) -> HealthResponse:
        return HealthResponse(model_loaded=True)

    async def synthesize(self, text: str) -> bytes:
        del text
        return b"RIFF\x04\x00\x00\x00WAVE"

    async def close(self) -> None:
        self.closed = True


def websocket_context(
    client: TestClient,
    *,
    capability: str = CAPABILITY,
    origin: str = "http://127.0.0.1:8765",
) -> WebSocketTestSession:
    return client.websocket_connect(
        "/ws",
        headers={"host": "127.0.0.1:8765", "origin": origin},
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


def test_start_cancel_and_hotkey_broadcast() -> None:
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
        assert socket.receive_json()["state"] == "ready"
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        assert socket.receive_json()["state"] == "connecting"
        assert socket.receive_json() == {"type": "sdp_answer", "sdp": "answer-sdp"}
        assert socket.receive_json()["state"] == "ready"

        socket.send_json({"type": "control", "control": "cancel"})
        assert socket.receive_json()["type"] == "audio_invalidate"
        assert socket.receive_json()["state"] == "cancelling"
        assert sessions[0].cancelled

        portal = client.portal
        assert portal is not None
        portal.call(
            app.state.control_hub.publish,
            Control.PTT_DOWN,
        )
        assert socket.receive_json() == {
            "type": "control",
            "control": "ptt_down",
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
