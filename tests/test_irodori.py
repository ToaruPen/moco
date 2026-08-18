from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Literal, cast

import httpx
import pytest
from irodori_tts_infra.client.errors import ClientError
from irodori_tts_infra.contracts import (
    CapabilitiesResponse,
    HealthResponse,
    Readiness,
    SynthesisRequest,
    SynthesisResult,
    VoiceCapability,
)
from pydantic import ValidationError

from moco.config import IrodoriSettings, MocoSettings
from moco.speech.contracts import DeliveryCaptionCapability, IrodoriCapabilities
from moco.speech.irodori import (
    _JSON_ENVELOPE_BYTES,
    _MAX_CAPABILITY_ALIASES_PER_VOICE,
    _MAX_CAPABILITY_RESPONSE_BYTES,
    _MAX_CAPABILITY_TEXT_CHARS,
    _MAX_CAPABILITY_VOICES,
    IrodoriClient,
    IrodoriError,
    IrodoriSynthesizer,
    _AddressOverrideTransport,
    _HttpCapabilityClient,
    _is_complete_wav,
    _LimitedResponseStream,
    _LimitedResponseTransport,
)


def dynamic_capabilities_payload() -> dict[str, object]:
    return {
        "contract_version": 1,
        "generation": "fixture-generation",
        "ready": True,
        "readiness": "ready",
        "voices": [
            {
                "id": "narrator",
                "label": "Narrator",
                "aliases": [],
                "default": True,
            },
        ],
        "conditioning": {
            "delivery_caption": {"supported": True, "max_chars": 300},
            "emoji": {"supported": True},
        },
    }


def test_dynamic_delivery_caption_capability_is_accepted() -> None:
    capabilities = IrodoriCapabilities.model_validate_json(
        json.dumps(dynamic_capabilities_payload()),
        strict=True,
    )

    assert capabilities.conditioning.delivery_caption == DeliveryCaptionCapability(
        supported=True,
        max_chars=300,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"supported": True, "max_chars": None},
        {"supported": False, "max_chars": 300},
    ],
)
def test_delivery_caption_capability_requires_matching_limit(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DeliveryCaptionCapability.model_validate(payload, strict=True)


def valid_wav(payload: bytes = b"") -> bytes:
    body = b"WAVE" + payload
    return b"RIFF" + len(body).to_bytes(4, byteorder="little") + body


def make_capabilities(
    count: int,
    *,
    ready: bool = True,
    readiness: Readiness | None = None,
    generation: str = "fixture-generation",
) -> CapabilitiesResponse:
    voices = tuple(
        VoiceCapability(
            id=f"fixture-id-{index}",
            label=f"Fixture label {index}",
            aliases=(f"fixture-alias-{index}",),
            default=index == 0,
        )
        for index in range(count)
    )
    return CapabilitiesResponse(
        generation=generation,
        ready=ready,
        readiness=readiness or ("ready" if ready else "model_loading"),
        voices=voices,
    )


def make_capabilities_with_voice(
    *,
    generation: str = "fixture-generation",
    voice_id: str = "fixture-id",
    label: str = "Fixture label",
    aliases: tuple[str, ...] = ("fixture-alias",),
) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        generation=generation,
        ready=True,
        readiness="ready",
        voices=(
            VoiceCapability(
                id=voice_id,
                label=label,
                aliases=aliases,
                default=True,
            ),
        ),
    )


class FakeIrodoriClient:
    def __init__(self, wav: bytes | None = None, *, voice_count: int = 2) -> None:
        self.wav = wav if wav is not None else valid_wav()
        self.capabilities_response: object = make_capabilities(voice_count)
        self.capabilities_calls = 0
        self.requests: list[SynthesisRequest] = []
        self.error: Exception | None = None
        self.close_error: Exception | None = None
        self.closed = False

    async def health(self) -> HealthResponse:
        if self.error is not None:
            raise self.error
        return HealthResponse(model_loaded=True)

    async def capabilities(self) -> CapabilitiesResponse:
        if self.error is not None:
            raise self.error
        self.capabilities_calls += 1
        return cast("CapabilitiesResponse", self.capabilities_response)

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


async def prepare_synthesizer(
    client: FakeIrodoriClient,
    *,
    settings: MocoSettings | None = None,
) -> IrodoriSynthesizer:
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=settings or MocoSettings(),
    )
    await synthesizer.capabilities()
    synthesizer.select_voice("fixture-id-0")
    return synthesizer


async def test_capabilities_fetches_from_bounded_client_and_caches_response() -> None:
    client = FakeIrodoriClient(voice_count=3)
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    capabilities = await synthesizer.capabilities()

    assert capabilities.model_dump(mode="python") == make_capabilities(3).model_dump(
        mode="python",
    )
    assert client.capabilities_calls == 1
    synthesizer.select_voice("fixture-id-2")


@pytest.mark.parametrize(
    ("num_steps", "t_schedule_mode"),
    [(17, "linear"), (31, "sway")],
)
async def test_uses_configured_voice_generation_and_sampling_settings(
    num_steps: int,
    t_schedule_mode: Literal["linear", "sway"],
) -> None:
    client = FakeIrodoriClient()
    settings = MocoSettings(
        irodori=IrodoriSettings(
            num_steps=num_steps,
            t_schedule_mode=t_schedule_mode,
            duration_scale=1.2,
            cfg_scale_text=2.5,
            cfg_scale_speaker=4.5,
        ),
    )
    synthesizer = IrodoriSynthesizer(cast("IrodoriClient", client), settings=settings)
    await synthesizer.capabilities()
    synthesizer.select_voice("fixture-id-1")

    assert await synthesizer.synthesize("こんにちは。") == valid_wav()

    request = client.requests[0]
    assert request.voice_id == "fixture-id-1"
    assert request.if_generation == "fixture-generation"
    assert request.num_steps == num_steps
    assert request.t_schedule_mode == t_schedule_mode
    assert request.duration_scale == 1.2
    assert request.cfg_scale_text == 2.5
    assert request.cfg_scale_speaker == 4.5


async def test_alias_selection_is_normalized_to_canonical_voice_id() -> None:
    client = FakeIrodoriClient()
    synthesizer = await prepare_synthesizer(client)

    synthesizer.select_voice("fixture-alias-1")
    await synthesizer.synthesize("別の声。")

    assert client.requests[0].voice_id == "fixture-id-1"


async def test_select_voice_requires_loaded_capabilities() -> None:
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", FakeIrodoriClient()),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        synthesizer.select_voice("fixture-id-0")

    assert caught.value.code == "capabilities_not_loaded"


async def test_select_voice_rejects_empty_or_unknown_catalog() -> None:
    empty_client = FakeIrodoriClient(voice_count=0)
    empty = IrodoriSynthesizer(
        cast("IrodoriClient", empty_client),
        settings=MocoSettings(),
    )
    await empty.capabilities()
    with pytest.raises(IrodoriError) as empty_caught:
        empty.select_voice("fixture-id-0")
    assert empty_caught.value.code == "voice_catalog_empty"

    client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )
    await synthesizer.capabilities()
    with pytest.raises(IrodoriError) as unknown_caught:
        synthesizer.select_voice("not-in-catalog")
    assert unknown_caught.value.code == "voice_not_found"


async def test_synthesis_requires_loaded_capabilities() -> None:
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", FakeIrodoriClient()),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == "capabilities_not_loaded"


@pytest.mark.parametrize(
    "readiness",
    ["model_loading", "model_not_loaded", "voice_bank_invalid"],
)
async def test_synthesis_preserves_unready_capability_code(readiness: Readiness) -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = make_capabilities(
        2,
        ready=False,
        readiness=readiness,
    )
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )
    await synthesizer.capabilities()

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == readiness


async def test_synthesis_requires_voice_selection() -> None:
    client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )
    await synthesizer.capabilities()

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == "voice_selection_required"


async def test_synthesis_rejects_selected_voice_removed_by_capability_refresh() -> None:
    client = FakeIrodoriClient(voice_count=2)
    synthesizer = await prepare_synthesizer(client)
    synthesizer.select_voice("fixture-id-1")
    client.capabilities_response = make_capabilities(1, generation="refreshed-generation")
    await synthesizer.capabilities()

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == "voice_not_found"


@pytest.mark.parametrize("operation", ["health", "capabilities"])
@pytest.mark.parametrize("code", ["connection_error", "private_backend_detail", "x" * 4096])
async def test_health_and_capabilities_bound_unknown_client_errors(
    operation: str,
    code: str,
) -> None:
    client = FakeIrodoriClient()
    private_message = "private backend host and token"
    client.error = ClientError(private_message, code=code)
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await getattr(synthesizer, operation)()

    assert caught.value.code == "irodori_unavailable"
    assert code not in str(caught.value)
    assert private_message not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_capabilities_preserves_client_error_code() -> None:
    client = FakeIrodoriClient()
    client.error = ClientError("runtime unavailable", code="model_not_loaded")
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.capabilities()

    assert caught.value.code == "model_not_loaded"
    assert "runtime unavailable" not in str(caught.value)


@pytest.mark.parametrize("operation", ["health", "capabilities"])
@pytest.mark.parametrize(
    "code",
    ["model_loading", "model_not_loaded", "voice_bank_invalid"],
)
async def test_health_and_capabilities_preserve_documented_readiness(
    operation: str,
    code: str,
) -> None:
    client = FakeIrodoriClient()
    client.error = ClientError("private readiness message", code=code)
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await getattr(synthesizer, operation)()

    assert caught.value.code == code
    assert "private readiness message" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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
    synthesizer = await prepare_synthesizer(client, settings=settings)

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == code


async def test_unexpected_validation_failure_becomes_invalid_response() -> None:
    client = FakeIrodoriClient()
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )
    await synthesizer.capabilities()
    synthesizer.select_voice("fixture-id-0")
    client.error = ValueError("raw response contained sensitive details")

    with pytest.raises(IrodoriError, match="invalid response") as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == "invalid_response"
    assert "sensitive" not in str(caught.value)


async def test_capabilities_validation_failure_becomes_invalid_response() -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = {
        "generation": "fixture-generation",
        "ready": True,
        "readiness": "ready",
        "voices": (),
        "unexpected": "must be rejected",
    }
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError, match="invalid response") as caught:
        await synthesizer.capabilities()

    assert caught.value.code == "invalid_response"


async def test_capabilities_revalidates_typed_client_response_strictly() -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = CapabilitiesResponse.model_construct(
        generation="fixture-generation",
        ready=True,
        readiness="ready",
        voices=(
            VoiceCapability(id="fixture-id-0", label="Fixture 0", aliases=("collision",)),
            VoiceCapability(id="fixture-id-1", label="Fixture 1", aliases=("collision",)),
        ),
    )
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.capabilities()

    assert caught.value.code == "invalid_response"


async def test_capabilities_rejects_forged_same_type_instance() -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = CapabilitiesResponse.model_construct(
        contract_version=2,
        generation=" ",
        ready=True,
        readiness="ready",
        voices=(VoiceCapability.model_construct(id=" ", label="Fixture"),),
    )
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.capabilities()

    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "capabilities",
    [
        make_capabilities_with_voice(generation="g" * _MAX_CAPABILITY_TEXT_CHARS),
        make_capabilities(_MAX_CAPABILITY_VOICES),
        make_capabilities_with_voice(voice_id="i" * _MAX_CAPABILITY_TEXT_CHARS),
        make_capabilities_with_voice(label="l" * _MAX_CAPABILITY_TEXT_CHARS),
        make_capabilities_with_voice(aliases=("a" * _MAX_CAPABILITY_TEXT_CHARS,)),
        make_capabilities_with_voice(
            aliases=tuple(f"alias-{index}" for index in range(_MAX_CAPABILITY_ALIASES_PER_VOICE)),
        ),
    ],
    ids=[
        "generation",
        "voices",
        "voice-id",
        "voice-label",
        "alias-text",
        "aliases-per-voice",
    ],
)
async def test_capability_structural_limits_accept_boundary_values(
    capabilities: CapabilitiesResponse,
) -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = capabilities
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    actual = await synthesizer.capabilities()

    assert actual.model_dump(mode="python") == capabilities.model_dump(mode="python")


@pytest.mark.parametrize(
    "capabilities",
    [
        make_capabilities_with_voice(
            generation="g" * (_MAX_CAPABILITY_TEXT_CHARS + 1),
        ),
        make_capabilities(_MAX_CAPABILITY_VOICES + 1),
        make_capabilities_with_voice(
            voice_id="i" * (_MAX_CAPABILITY_TEXT_CHARS + 1),
        ),
        make_capabilities_with_voice(
            label="l" * (_MAX_CAPABILITY_TEXT_CHARS + 1),
        ),
        make_capabilities_with_voice(
            aliases=("a" * (_MAX_CAPABILITY_TEXT_CHARS + 1),),
        ),
        make_capabilities_with_voice(
            aliases=tuple(
                f"alias-{index}" for index in range(_MAX_CAPABILITY_ALIASES_PER_VOICE + 1)
            ),
        ),
    ],
    ids=[
        "generation",
        "voices",
        "voice-id",
        "voice-label",
        "alias-text",
        "aliases-per-voice",
    ],
)
async def test_capability_structural_limits_reject_over_limit_before_cache(
    capabilities: CapabilitiesResponse,
) -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = capabilities
    synthesizer = IrodoriSynthesizer(
        cast("IrodoriClient", client),
        settings=MocoSettings(),
    )

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.capabilities()

    assert caught.value.code == "invalid_response"
    with pytest.raises(IrodoriError) as uncached:
        synthesizer.select_voice(capabilities.voices[0].id)
    assert uncached.value.code == "capabilities_not_loaded"


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


async def test_http_capability_client_accepts_dynamic_delivery_caption() -> None:
    client = _HttpCapabilityClient(
        base_url="https://voice-host.example.ts.net/",
        timeout=5.0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=dynamic_capabilities_payload()),
        ),
    )

    capabilities = await client.capabilities()

    assert capabilities.conditioning.delivery_caption.supported is True
    assert capabilities.conditioning.delivery_caption.max_chars == 300
    await client.aclose()


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
    await synthesizer.capabilities()
    synthesizer.select_voice("fixture-id-0")
    await synthesizer.synthesize("test")
    await synthesizer.close()

    assert health_client.requests == []
    assert health_client.capabilities_calls == 1
    assert synthesis_client.capabilities_calls == 0
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


@pytest.mark.parametrize("max_wav_bytes", [1_024, 134_217_728])
async def test_from_settings_separates_capability_and_synthesis_resource_bounds(
    monkeypatch: pytest.MonkeyPatch,
    max_wav_bytes: int,
) -> None:
    timeouts: list[float | None] = []
    response_limits: list[int] = []

    class RecordingClient(FakeIrodoriClient):
        def __init__(
            self,
            *,
            base_url: str | None = None,
            timeout: float | httpx.Timeout | None = 30.0,
            transport: httpx.AsyncBaseTransport | None = None,
        ) -> None:
            super().__init__()
            del base_url
            assert timeout is None or isinstance(timeout, float)
            assert isinstance(transport, _LimitedResponseTransport)
            timeouts.append(timeout)
            response_limits.append(transport._max_bytes)  # noqa: SLF001

    monkeypatch.setattr("moco.speech.irodori.AsyncIrodoriClient", RecordingClient)

    synthesizer = IrodoriSynthesizer.from_settings(
        MocoSettings(
            irodori=IrodoriSettings(
                timeout_seconds=7.5,
                max_wav_bytes=max_wav_bytes,
            ),
        ),
    )
    await synthesizer.close()

    assert timeouts == [7.5, None]
    assert response_limits == [
        _MAX_CAPABILITY_RESPONSE_BYTES,
        ((max_wav_bytes + 2) // 3) * 4 + _JSON_ENVELOPE_BYTES,
    ]


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


@pytest.mark.parametrize(
    "code",
    [
        "runtime_generation_mismatch",
        "voice_not_found",
        "model_loading",
        "model_not_loaded",
        "voice_bank_invalid",
    ],
)
async def test_synthesis_client_errors_preserve_stable_code(code: str) -> None:
    failing = FakeIrodoriClient()
    synthesizer = await prepare_synthesizer(failing)
    failing.error = ClientError("private synthesis message", code=code)

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == code
    assert "private synthesis message" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("code", ["private_backend_detail", "x" * 4096])
async def test_synthesis_bounds_unknown_client_error(code: str) -> None:
    failing = FakeIrodoriClient()
    synthesizer = await prepare_synthesizer(failing)
    private_message = "private synthesis host and token"
    failing.error = ClientError(private_message, code=code)

    with pytest.raises(IrodoriError) as caught:
        await synthesizer.synthesize("test")

    assert caught.value.code == "synthesis_failed"
    assert code not in str(caught.value)
    assert private_message not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_legacy_speaker_selection_interfaces_are_removed() -> None:
    # Keep legacy identifiers split so the migration gate does not match this negative test.
    legacy_selector = "select_" + "speaker"
    legacy_override = "speak" + "er"
    assert not hasattr(IrodoriSynthesizer, legacy_selector)
    assert legacy_override not in IrodoriSynthesizer.synthesize.__annotations__


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
