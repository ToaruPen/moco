from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

import httpx
from irodori_tts_infra.client.async_ import AsyncIrodoriClient
from irodori_tts_infra.client.errors import ClientError
from irodori_tts_infra.contracts import HealthResponse, SynthesisRequest, SynthesisResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from moco.config import MocoSettings

_WAV_HEADER_SIZE = 12
_JSON_ENVELOPE_BYTES = 4096


class IrodoriClient(Protocol):
    async def health(self) -> HealthResponse: ...

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


class IrodoriError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class IrodoriSynthesizer:
    def __init__(self, client: IrodoriClient, *, settings: MocoSettings) -> None:
        self._client = client
        self._settings = settings

    @classmethod
    def from_settings(cls, settings: MocoSettings) -> Self:
        max_wav_bytes = settings.irodori.max_wav_bytes
        max_response_bytes = ((max_wav_bytes + 2) // 3) * 4 + _JSON_ENVELOPE_BYTES
        return cls(
            AsyncIrodoriClient(
                base_url=str(settings.irodori.base_url),
                timeout=settings.irodori.timeout_seconds,
                transport=_LimitedResponseTransport(
                    httpx.AsyncHTTPTransport(),
                    max_bytes=max_response_bytes,
                ),
            ),
            settings=settings,
        )

    async def health(self) -> HealthResponse:
        try:
            return await self._client.health()
        except ClientError as error:
            raise _map_client_error(error) from error
        except (KeyError, TypeError, ValueError) as error:
            raise _invalid_response_error() from error

    async def synthesize(
        self,
        text: str,
        *,
        speaker: str | None = None,
        num_steps: int | None = None,
        duration_scale: float | None = None,
        cfg_scale_text: float | None = None,
        cfg_scale_speaker: float | None = None,
    ) -> bytes:
        config = self._settings.irodori
        request = SynthesisRequest(
            text=text,
            speaker=speaker if speaker is not None else config.speaker,
            num_steps=num_steps or config.num_steps,
            duration_scale=duration_scale or config.duration_scale,
            cfg_scale_text=cfg_scale_text or config.cfg_scale_text,
            cfg_scale_speaker=cfg_scale_speaker or config.cfg_scale_speaker,
        )
        try:
            result = await self._client.synthesize(request)
        except ClientError as error:
            raise _map_client_error(error) from error
        except (KeyError, TypeError, ValueError) as error:
            raise _invalid_response_error() from error
        wav = result.wav_bytes
        if len(wav) > config.max_wav_bytes:
            msg = "Irodori WAV exceeded the configured size limit"
            raise IrodoriError(msg, code="audio_too_large")
        if not _is_complete_wav(wav):
            msg = "Irodori server did not return a valid WAV file"
            raise IrodoriError(msg, code="invalid_audio")
        return wav

    async def close(self) -> None:
        await self._client.aclose()


def _map_client_error(error: ClientError) -> IrodoriError:
    return IrodoriError(error.message, code=error.code)


def _invalid_response_error() -> IrodoriError:
    return IrodoriError("Irodori server returned an invalid response", code="invalid_response")


def _is_complete_wav(data: bytes) -> bool:
    if len(data) < _WAV_HEADER_SIZE or data[:4] != b"RIFF" or data[8:_WAV_HEADER_SIZE] != b"WAVE":
        return False
    declared_size = int.from_bytes(data[4:8], byteorder="little")
    return declared_size + 8 == len(data)
