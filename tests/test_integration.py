from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

from fastapi.testclient import TestClient
from irodori_tts_infra.contracts import HealthResponse

from moco.codex.session import RealtimeEvent, TranscriptEvent
from moco.web.app import RealtimeSession, WebSynthesizer, create_app


class TranscriptSession:
    active_turn_id: str | None = None

    async def start(self, _sdp: str) -> str:
        return "answer-sdp"

    async def notifications(self) -> AsyncIterator[RealtimeEvent]:
        yield TranscriptEvent("done", "thr_test", "assistant", "こんにちは。")
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


class WavSynthesizer:
    async def health(self) -> HealthResponse:
        return HealthResponse(model_loaded=True)

    async def synthesize(self, text: str) -> bytes:
        assert text == "こんにちは。"
        return b"RIFF\x04\x00\x00\x00WAVE"

    def select_speaker(self, speaker: str | None) -> None:
        del speaker

    async def close(self) -> None:
        return None


def test_fake_codex_transcript_reaches_browser_as_irodori_wav() -> None:
    capability_value = "integration-capability"
    app = create_app(
        session_factory=lambda: cast("RealtimeSession", TranscriptSession()),
        synthesizer_factory=lambda: cast("WebSynthesizer", WavSynthesizer()),
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
        socket.receive_json()
        socket.send_json({"type": "start", "sdp": "offer-sdp"})
        socket.receive_json()
        socket.receive_json()
        socket.receive_json()

        transcript = socket.receive_json()
        audio = socket.receive_json()
        wav = socket.receive_bytes()

    assert transcript == {
        "type": "transcript",
        "role": "assistant",
        "delta": "こんにちは。",
        "done": True,
    }
    assert audio["type"] == "audio"
    assert wav == b"RIFF\x04\x00\x00\x00WAVE"
