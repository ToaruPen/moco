from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

from fastapi.testclient import TestClient
from irodori_tts_infra.contracts import (
    CapabilitiesResponse,
    SynthesisRequest,
    SynthesisResult,
    VoiceCapability,
)

from moco.codex.session import RealtimeEvent, TranscriptEvent
from moco.config import MocoSettings
from moco.speech.irodori import IrodoriClient, IrodoriSynthesizer
from moco.web.app import RealtimeSession, WebSynthesizer, create_app


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


class TranscriptSession:
    active_turn_id: str | None = None

    async def start(self, _sdp: str) -> str:
        return "answer-sdp"

    async def notifications(self) -> AsyncIterator[RealtimeEvent]:
        yield TranscriptEvent("done", "thr_test", "assistant", "こんにちは。")
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


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


def test_fake_codex_transcript_reaches_browser_as_irodori_wav() -> None:
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
        session_factory=lambda: cast("RealtimeSession", TranscriptSession()),
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
        "delta": "こんにちは。",
        "done": True,
    }
    assert audio["type"] == "audio"
    assert wav == b"RIFF\x04\x00\x00\x00WAVE"
    assert discovery.requests == []
    assert len(active.requests) == 1
    request = active.requests[0]
    assert request.text == "こんにちは。"
    assert request.voice_id == selected.id
    assert request.if_generation == capabilities.generation
    assert {"caption", "style", "cfg_scale_caption"}.isdisjoint(request.model_fields_set)
    assert discovery.closed
    assert active.closed
