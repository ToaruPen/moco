from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from moco.runtime.telemetry import safe_event
from moco.speech.irodori import IrodoriError
from moco.speech.text import TranscriptSegmenter

type TranscriptRole = Literal["assistant", "user"]
type Delivery = Callable[[bytes], object | Awaitable[object]]
logger = logging.getLogger(__name__)


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _SpeechItem:
    text: str
    generation: int


class SpeechQueue:
    def __init__(
        self,
        synthesizer: Synthesizer,
        *,
        deliver: Delivery,
        max_chars: int,
    ) -> None:
        self._synthesizer = synthesizer
        self._deliver = deliver
        self._segmenter = TranscriptSegmenter(max_chars=max_chars)
        self._items: deque[_SpeechItem] = deque()
        self._condition = asyncio.Condition()
        self._generation = 0
        self._suppressed = False
        self._assistant_has_streamed = False
        self._worker: asyncio.Task[None] | None = None
        self._active: asyncio.Task[None] | None = None
        self._busy = False
        self._closed = False
        self._error_codes: list[str] = []

    @property
    def pending_count(self) -> int:
        return len(self._items)

    @property
    def is_busy(self) -> bool:
        return self._busy or bool(self._items)

    @property
    def pending_texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self._items)

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(self._error_codes)

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="moco-speech-queue")

    async def on_transcript(
        self,
        *,
        role: TranscriptRole,
        delta: str,
        done: bool,
    ) -> None:
        if role == "user":
            if done:
                self._suppressed = False
                self._assistant_has_streamed = False
                self._segmenter.clear()
            return
        if self._suppressed or self._closed:
            return

        text_to_push = delta
        if done and self._assistant_has_streamed:
            text_to_push = ""
        if not done and delta:
            self._assistant_has_streamed = True

        segments = self._segmenter.push(text_to_push)
        if done:
            segments.extend(self._segmenter.flush())
            self._assistant_has_streamed = False
        await self._enqueue(segments)

    async def cancel(self) -> None:
        safe_event(
            logger,
            "speech_cancelled",
            component="speech",
            control="cancel",
            state="cancelling",
        )
        async with self._condition:
            self._generation += 1
            self._suppressed = True
            self._assistant_has_streamed = False
            self._segmenter.clear()
            self._items.clear()
            active = self._active
            self._condition.notify_all()
        if active is not None and not active.done():
            active.cancel()
            with suppress(asyncio.CancelledError):
                await active

    async def join(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._items and not self._busy)

    async def close(self) -> None:
        if self._closed:
            return
        await self.cancel()
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
        worker = self._worker
        if worker is not None:
            with suppress(asyncio.CancelledError):
                await worker

    async def _enqueue(self, segments: list[str]) -> None:
        if not segments:
            return
        async with self._condition:
            self._items.extend(_SpeechItem(text, self._generation) for text in segments)
            self._condition.notify_all()

    async def _run(self) -> None:
        while True:
            async with self._condition:
                await self._condition.wait_for(lambda: bool(self._items) or self._closed)
                if self._closed and not self._items:
                    return
                item = self._items.popleft()
                self._busy = True
                self._active = asyncio.create_task(
                    self._process_item(item),
                    name="moco-speech-delivery",
                )
                active = self._active

            try:
                await active
            except asyncio.CancelledError:
                continue
            finally:
                async with self._condition:
                    if self._active is active:
                        self._active = None
                    self._busy = False
                    self._condition.notify_all()

    async def _process_item(self, item: _SpeechItem) -> None:
        try:
            wav = await self._synthesizer.synthesize(item.text)
        except asyncio.CancelledError:
            raise
        except IrodoriError as error:
            self._error_codes.append(error.code)
            safe_event(
                logger,
                "synthesis_failed",
                component="speech",
                boundary="irodori_http",
                event_code=error.code,
                result="error",
            )
            return

        if item.generation != self._generation:
            return
        try:
            result = self._deliver(wav)
            if inspect.isawaitable(result):
                await cast("Awaitable[object]", result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._error_codes.append("audio_delivery_failed")
            safe_event(
                logger,
                "audio_delivery_failed",
                component="speech",
                boundary="browser_audio",
                event_code="audio_delivery_failed",
                result="error",
            )
