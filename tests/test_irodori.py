from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import cast

import httpx
import pytest
from irodori_tts_infra.client.errors import ClientError
from irodori_tts_infra.contracts import HealthResponse, SynthesisRequest, SynthesisResult

from moco.config import IrodoriSettings, MocoSettings
from moco.speech.irodori import (
    IrodoriClient,
    IrodoriError,
    IrodoriSynthesizer,
    _AddressOverrideTransport,
    _is_complete_wav,
    _LimitedResponseStream,
    _LimitedResponseTransport,
)


def valid_wav(payload: bytes = b"") -> bytes:
    body = b"WAVE" + payload
    return b"RIFF" + len(body).to_bytes(4, byteorder="little") + body


class FakeIrodoriClient:
    def __init__(self, wav: bytes | None = None) -> None:
        self.wav = wav if wav is not None else valid_wav()
        self.requests: list[SynthesisRequest] = []
        self.error: Exception | None = None
        self.close_error: Exception | None = None
        self.closed = False

    async def health(self) -> HealthResponse:
        if self.error is not None:
            raise self.error
        return HealthResponse(model_loaded=True)

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if self.error is not None:
            raise self.error
        self.requests.append(request)
        return SynthesisResult(
            segment_index=0,
            wav_bytes=self.wav,
            elapsed_seconds=0.1,
        )

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


async def test_uses_portable_speaker_and_configured_parameters() -> None:
    client = FakeIrodoriClient()
    settings = MocoSettings(
        irodori=IrodoriSettings(
            speaker="portable-name",
            num_steps=16,
            duration_scale=1.2,
            cfg_scale_text=2.5,
            cfg_scale_speaker=4.5,
        ),
    )
    synthesizer = IrodoriSynthesizer(cast("IrodoriClient", client), settings=settings)

    assert await synthesizer.synthesize("こんにちは。") == valid_wav()

    request = client.requests[0]
    assert request.speaker == "portable-name"
    assert request.ref_embed is None
    assert request.num_steps == 16
    assert request.duration_scale == 1.2
    assert request.cfg_scale_text == 2.5
    assert request.cfg_scale_speaker == 4.5


async def test_selected_speaker_applies_to_subsequent_synthesis() -> None:
    client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(irodori=IrodoriSettings(speaker="default")),
    )

    synthesizer.select_speaker("alternate")
    await synthesizer.synthesize("別の声。")
    synthesizer.select_speaker(None)
    await synthesizer.synthesize("ナレーター。")

    assert [request.speaker for request in client.requests] == ["alternate", None]


async def test_maps_client_errors_to_stable_codes() -> None:
    client = FakeIrodoriClient()
    client.error = ClientError("offline", code="connection_error")
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError, match="offline") as caught:
        await synthesizer.health()

    assert caught.value.code == "connection_error"


@pytest.mark.parametrize(
    ("wav", "code"),
    [
        (b"not-wav", "invalid_audio"),
        (valid_wav(b"too-large"), "audio_too_large"),
    ],
)
async def test_rejects_invalid_or_oversized_wav(wav: bytes, code: str) -> None:
    max_bytes = len(valid_wav()) if code == "audio_too_large" else 1024
    client = FakeIrodoriClient(wav)
    settings = MocoSettings(irodori=IrodoriSettings(max_wav_bytes=max_bytes))
    synthesizer = IrodoriSynthesizer(cast("IrodoriClient", client), settings=settings)

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == code


async def test_unexpected_validation_failure_becomes_invalid_response() -> None:
    client = FakeIrodoriClient()
    client.error = ValueError("raw response contained sensitive details")
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError, match="invalid response") as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == "invalid_response"
    assert "sensitive" not in str(caught.value)


async def test_transport_rejects_declared_response_over_limit() -> None:
    request = httpx.Request("GET", "http://127.0.0.1/health")
    inner = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-length": "200"},
            content=b"{}",
        ),
    )
    transport = _LimitedResponseTransport(inner, max_bytes=100)

    with pytest.raises(httpx.ReadError, match="size limit"):
        await transport.handle_async_request(request)

    await transport.aclose()


async def test_address_override_preserves_host_sni_and_tls_identity() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            host=request.url.host,
            host_header=request.headers["host"],
            sni_hostname=request.extensions["sni_hostname"],
        )
        return httpx.Response(200, json={"model_loaded": True})

    transport = _AddressOverrideTransport(
        httpx.MockTransport(handler),
        connect_ip="100.112.161.83",
    )
    request = httpx.Request(
        "GET",
        "https://voice-host.example.ts.net/health",
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert seen == {
        "host": "100.112.161.83",
        "host_header": "voice-host.example.ts.net",
        "sni_hostname": "voice-host.example.ts.net",
    }
    await transport.aclose()


async def test_close_delegates_to_client() -> None:
    client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    await synthesizer.close()

    assert client.closed


async def test_health_and_synthesis_use_separate_clients() -> None:
    health_client = FakeIrodoriClient()
    synthesis_client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", health_client),
        settings=MocoSettings(),
        synthesis_client=cast("IrodoriClient", synthesis_client),
    )

    await synthesizer.health()
    await synthesizer.synthesize("test")
    await synthesizer.close()

    assert health_client.requests == []
    assert len(synthesis_client.requests) == 1
    assert health_client.closed
    assert synthesis_client.closed


async def test_synthesis_client_closes_when_health_client_close_fails() -> None:
    health_client = FakeIrodoriClient()
    health_client.close_error = RuntimeError("health close failed")
    synthesis_client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", health_client),
        settings=MocoSettings(),
        synthesis_client=cast("IrodoriClient", synthesis_client),
    )

    with pytest.raises(RuntimeError, match="health close failed"):
        await synthesizer.close()

    assert health_client.closed
    assert synthesis_client.closed


async def test_from_settings_disables_only_synthesis_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float | None] = []

    class RecordingClient(FakeIrodoriClient):
        def __init__(
            self,
            *,
            base_url: str | None = None,
            timeout: float | httpx.Timeout | None = 30.0,
            transport: httpx.AsyncBaseTransport | None = None,
        ) -> None:
            super().__init__()
            del base_url, transport
            assert timeout is None or isinstance(timeout, float)
            timeouts.append(timeout)

    monkeypatch.setattr("moco.speech.irodori.AsyncIrodoriClient", RecordingClient)

    synthesizer = IrodoriSynthesizer.from_settings(
        MocoSettings(irodori=IrodoriSettings(timeout_seconds=7.5)),
    )
    await synthesizer.close()

    assert timeouts == [7.5, None]


async def test_health_validation_failure_is_safely_mapped() -> None:
    client = FakeIrodoriClient()
    client.error = TypeError("unexpected shape")
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.health()

    assert caught.value.code == "invalid_response"


async def test_synthesis_client_error_and_explicit_overrides() -> None:
    failing = FakeIrodoriClient()
    failing.error = ClientError("timeout", code="timeout")
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", failing),
        settings=MocoSettings(),
    )
    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")
    assert caught.value.code == "timeout"

    client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )
    await synthesizer.synthesize(
        "test",
        speaker="override",
        num_steps=3,
        duration_scale=1.1,
        cfg_scale_text=2.2,
        cfg_scale_speaker=4.4,
    )
    request = client.requests[0]
    assert (
        request.speaker,
        request.num_steps,
        request.duration_scale,
        request.cfg_scale_text,
        request.cfg_scale_speaker,
    ) == ("override", 3, 1.1, 2.2, 4.4)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def read_stream(stream: httpx.AsyncByteStream) -> None:
    async for chunk in stream:
        _ = chunk


async def test_stream_limit_closes_oversized_response() -> None:
    request = httpx.Request("GET", "http://127.0.0.1/test")
    inner = ChunkStream([b"123", b"456"])
    stream = _LimitedResponseStream(inner, max_bytes=5, request=request)

    with pytest.raises(httpx.ReadError, match="size limit"):
        await read_stream(stream)

    assert inner.closed
    await stream.aclose()


class SyncOnlyStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"ok"


class SyncStreamTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SyncOnlyStream(), request=request)


async def test_transport_rejects_sync_stream_and_ignores_bad_length() -> None:
    request = httpx.Request("GET", "http://127.0.0.1/test")
    transport = _LimitedResponseTransport(SyncStreamTransport(), max_bytes=10)
    with pytest.raises(httpx.ReadError, match="synchronous"):
        await transport.handle_async_request(request)
    await transport.aclose()

    inner = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-length": "unknown"},
            content=b"ok",
        ),
    )
    accepted = _LimitedResponseTransport(inner, max_bytes=10)
    response = await accepted.handle_async_request(request)
    assert await response.aread() == b"ok"
    await accepted.aclose()


async def test_from_settings_builds_real_client_without_network() -> None:
    synthesizer = IrodoriSynthesizer.from_settings(MocoSettings())
    await synthesizer.close()


def test_wav_validator_rejects_mismatched_declared_size() -> None:
    assert _is_complete_wav(valid_wav())
    assert not _is_complete_wav(b"RIFF\x05\x00\x00\x00WAVE")
