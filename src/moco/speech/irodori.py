from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

import httpx
from irodori_tts_infra.client.async_ import AsyncIrodoriClient
from irodori_tts_infra.client.errors import ClientError
from irodori_tts_infra.contracts import (
    CapabilitiesResponse,
    HealthResponse,
    SynthesisRequest,
    SynthesisResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from moco.config import MocoSettings

_WAV_HEADER_SIZE = 12
_JSON_ENVELOPE_BYTES = 4096
_MAX_CAPABILITY_RESPONSE_BYTES = 256 * 1024
_MAX_CAPABILITY_VOICES = 256
_MAX_CAPABILITY_TEXT_CHARS = 256
_MAX_CAPABILITY_ALIASES_PER_VOICE = 32
_CAPABILITIES_NOT_LOADED = "capabilities_not_loaded"
_VOICE_CATALOG_EMPTY = "voice_catalog_empty"
_VOICE_NOT_FOUND = "voice_not_found"
_VOICE_SELECTION_REQUIRED = "voice_selection_required"
_READINESS_ERROR_CODES = frozenset(
    {"model_loading", "model_not_loaded", "voice_bank_invalid"},
)
_SYNTHESIS_ERROR_CODES = _READINESS_ERROR_CODES | frozenset(
    {"runtime_generation_mismatch", "voice_not_found"},
)


class IrodoriClient(Protocol):
    async def health(self) -> HealthResponse: ...

    async def capabilities(self) -> CapabilitiesResponse: ...

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult: ...

    async def aclose(self) -> None: ...


class _LimitedResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        *,
        max_bytes: int,
        request: httpx.Request,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            if received > self._max_bytes:
                await self._stream.aclose()
                message = "Irodori response exceeded the configured size limit"
                raise httpx.ReadError(message, request=self._request)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _LimitedResponseTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport, *, max_bytes: int) -> None:
        self._transport = transport
        self._max_bytes = max_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = 0
            if declared_length > self._max_bytes:
                await response.aclose()
                message = "Irodori response exceeded the configured size limit"
                raise httpx.ReadError(message, request=request)
        if not isinstance(response.stream, httpx.AsyncByteStream):
            response.close()
            message = "Irodori transport returned a synchronous response stream"
            raise httpx.ReadError(message, request=request)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_LimitedResponseStream(
                response.stream,
                max_bytes=self._max_bytes,
                request=request,
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


class _AddressOverrideTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport, *, connect_ip: str) -> None:
        self._transport = transport
        self._connect_ip = connect_ip

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        hostname = request.url.host
        rewritten = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=self._connect_ip),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": hostname},
        )
        return await self._transport.handle_async_request(rewritten)

    async def aclose(self) -> None:
        await self._transport.aclose()


class IrodoriError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class IrodoriSynthesizer:
    def __init__(
        self,
        client: IrodoriClient,
        *,
        settings: MocoSettings,
        synthesis_client: IrodoriClient | None = None,
    ) -> None:
        self._health_client = client
        self._synthesis_client = synthesis_client or client
        self._settings = settings
        self._capabilities: CapabilitiesResponse | None = None
        self._voice_id: str | None = None

    @classmethod
    def from_settings(cls, settings: MocoSettings) -> Self:
        max_wav_bytes = settings.irodori.max_wav_bytes
        max_response_bytes = ((max_wav_bytes + 2) // 3) * 4 + _JSON_ENVELOPE_BYTES
        base_url = str(settings.irodori.base_url)
        return cls(
            AsyncIrodoriClient(
                base_url=base_url,
                timeout=settings.irodori.timeout_seconds,
                transport=_build_transport(
                    settings,
                    max_bytes=_MAX_CAPABILITY_RESPONSE_BYTES,
                ),
            ),
            settings=settings,
            synthesis_client=AsyncIrodoriClient(
                base_url=base_url,
                timeout=None,
                transport=_build_transport(
                    settings,
                    max_bytes=max_response_bytes,
                ),
            ),
        )

    async def health(self) -> HealthResponse:
        try:
            response = await self._health_client.health()
        except ClientError as error:
            mapped_error = _availability_error(error)
        except (KeyError, TypeError, ValueError):
            mapped_error = _invalid_response_error()
        else:
            return response
        raise mapped_error

    async def capabilities(self) -> CapabilitiesResponse:
        try:
            response = await self._health_client.capabilities()
            capabilities = CapabilitiesResponse.model_validate(
                response.model_dump(mode="python"),
                strict=True,
            )
            _validate_capability_bounds(capabilities)
        except ClientError as error:
            mapped_error = _availability_error(error)
        except (AttributeError, KeyError, TypeError, ValueError):
            mapped_error = _invalid_response_error()
        else:
            self._capabilities = capabilities
            return capabilities
        raise mapped_error

    def select_voice(self, selector: str) -> None:
        capabilities = self._capabilities
        if capabilities is None:
            raise _state_error(_CAPABILITIES_NOT_LOADED)
        if not capabilities.voices:
            raise _state_error(_VOICE_CATALOG_EMPTY)
        for voice in capabilities.voices:
            if selector == voice.id or selector in voice.aliases:
                self._voice_id = voice.id
                return
        raise _state_error(_VOICE_NOT_FOUND)

    async def synthesize(self, text: str) -> bytes:
        capabilities = self._capabilities
        if capabilities is None:
            raise _state_error(_CAPABILITIES_NOT_LOADED)
        if not capabilities.ready:
            raise _state_error(capabilities.readiness)
        voice_id = self._voice_id
        if voice_id is None:
            raise _state_error(_VOICE_SELECTION_REQUIRED)
        if not any(voice.id == voice_id for voice in capabilities.voices):
            raise _state_error(_VOICE_NOT_FOUND)

        config = self._settings.irodori
        request = SynthesisRequest(
            text=text,
            voice_id=voice_id,
            if_generation=capabilities.generation,
            num_steps=config.num_steps,
            t_schedule_mode=config.t_schedule_mode,
            duration_scale=config.duration_scale,
            cfg_scale_text=config.cfg_scale_text,
            cfg_scale_speaker=config.cfg_scale_speaker,
        )
        try:
            result = await self._synthesis_client.synthesize(request)
        except ClientError as error:
            mapped_error = _synthesis_error(error)
        except (KeyError, TypeError, ValueError):
            mapped_error = _invalid_response_error()
        else:
            wav = result.wav_bytes
            if len(wav) > config.max_wav_bytes:
                msg = "Irodori WAV exceeded the configured size limit"
                raise IrodoriError(msg, code="audio_too_large")
            if not _is_complete_wav(wav):
                msg = "Irodori server did not return a valid WAV file"
                raise IrodoriError(msg, code="invalid_audio")
            return wav
        raise mapped_error

    async def close(self) -> None:
        try:
            await self._health_client.aclose()
        finally:
            if self._synthesis_client is not self._health_client:
                await self._synthesis_client.aclose()


def _build_transport(
    settings: MocoSettings,
    *,
    max_bytes: int,
) -> httpx.AsyncBaseTransport:
    transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport()
    if settings.irodori.connect_ip is not None:
        transport = _AddressOverrideTransport(
            transport,
            connect_ip=str(settings.irodori.connect_ip),
        )
    return _LimitedResponseTransport(transport, max_bytes=max_bytes)


def _availability_error(error: ClientError) -> IrodoriError:
    code = error.code if error.code in _READINESS_ERROR_CODES else "irodori_unavailable"
    return IrodoriError("Irodori capability endpoint is unavailable", code=code)


def _synthesis_error(error: ClientError) -> IrodoriError:
    code = error.code if error.code in _SYNTHESIS_ERROR_CODES else "synthesis_failed"
    return IrodoriError("Irodori synthesis request failed", code=code)


def _invalid_response_error() -> IrodoriError:
    return IrodoriError("Irodori server returned an invalid response", code="invalid_response")


def _state_error(code: str) -> IrodoriError:
    return IrodoriError("Irodori is not ready to synthesize", code=code)


def _validate_capability_bounds(capabilities: CapabilitiesResponse) -> None:
    if len(capabilities.generation) > _MAX_CAPABILITY_TEXT_CHARS:
        msg = "Irodori capability generation exceeded the size limit"
        raise ValueError(msg)
    if len(capabilities.voices) > _MAX_CAPABILITY_VOICES:
        msg = "Irodori capability voice catalog exceeded the size limit"
        raise ValueError(msg)
    for voice in capabilities.voices:
        if len(voice.id) > _MAX_CAPABILITY_TEXT_CHARS:
            msg = "Irodori capability voice ID exceeded the size limit"
            raise ValueError(msg)
        if len(voice.label) > _MAX_CAPABILITY_TEXT_CHARS:
            msg = "Irodori capability voice label exceeded the size limit"
            raise ValueError(msg)
        if len(voice.aliases) > _MAX_CAPABILITY_ALIASES_PER_VOICE:
            msg = "Irodori capability voice aliases exceeded the count limit"
            raise ValueError(msg)
        if any(len(alias) > _MAX_CAPABILITY_TEXT_CHARS for alias in voice.aliases):
            msg = "Irodori capability voice alias exceeded the size limit"
            raise ValueError(msg)


def _is_complete_wav(data: bytes) -> bool:
    if len(data) < _WAV_HEADER_SIZE or data[:4] != b"RIFF" or data[8:_WAV_HEADER_SIZE] != b"WAVE":
        return False
    declared_size = int.from_bytes(data[4:8], byteorder="little")
    return declared_size + 8 == len(data)
