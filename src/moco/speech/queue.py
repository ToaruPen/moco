from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from moco.runtime.telemetry import safe_event
from moco.speech.irodori import IrodoriError
from moco.speech.text import SegmentReason, TranscriptSegment, TranscriptSegmenter

type TranscriptRole = Literal["assistant", "user"]
type Delivery = Callable[[bytes, int, int], object | Awaitable[object]]
type ErrorReporter = Callable[[str], object | Awaitable[object]]
type AudioIdAllocator = Callable[[], int]
type InvalidationReason = Literal["owner_request", "user_transcript", "queue_close"]
logger = logging.getLogger(__name__)
_PUBLIC_IRODORI_ERROR_CODES = frozenset(
    {
        "runtime_generation_mismatch",
        "voice_not_found",
        "voice_catalog_empty",
        "voice_selection_required",
        "model_loading",
        "model_not_loaded",
        "voice_bank_invalid",
        "invalid_response",
        "audio_too_large",
        "invalid_audio",
        "synthesis_failed",
        "caption_unsupported",
        "speech_caption_invalid",
    },
)


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class CaptionSynthesizer(Protocol):
    async def synthesize(self, text: str, *, delivery_caption: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _SpeechItem:
    text: str
    delivery_caption: str | None
    audio_id: int
    generation: int
    reason: SegmentReason
    segment_index: int
    segment_wait_ms: int


class SpeechQueue:
    def __init__(
        self,
        synthesizer: Synthesizer,
        *,
        deliver: Delivery,
        max_chars: int,
        on_error: ErrorReporter | None = None,
        initial_generation: int = 0,
        reserve_audio_id: AudioIdAllocator | None = None,
        first_segment_soft_break_min_chars: int | None = None,
    ) -> None:
        if type(initial_generation) is not int or initial_generation < 0:
            message = "initial_generation must be a non-negative integer"
            raise ValueError(message)
        self._synthesizer = synthesizer
        self._deliver = deliver
        self._on_error = on_error
        self._reserve_audio_id = reserve_audio_id or self._reserve_local_audio_id
        self._local_audio_id = 0
        self._segmenter = TranscriptSegmenter(
            max_chars=max_chars,
            first_segment_soft_break_min_chars=first_segment_soft_break_min_chars,
        )
        self._items: deque[_SpeechItem] = deque()
        self._condition = asyncio.Condition()
        self._generation = initial_generation
        self._suppressed = False
        self._turn_started_ns: int | None = None
        self._turn_delivery_caption: str | None = None
        self._turn_caption_set = False
        self._next_segment_index = 1
        self._worker: asyncio.Task[None] | None = None
        self._active: asyncio.Task[None] | None = None
        self._active_cancel_reason: InvalidationReason | None = None
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
        delivery_caption: str | None = None,
    ) -> None:
        if role == "user":
            if done:
                self._suppressed = False
                self._segmenter.clear()
                self._reset_turn_state()
            return
        if self._suppressed or self._closed:
            return

        if not self._turn_caption_set:
            self._turn_delivery_caption = delivery_caption
            self._turn_caption_set = True
        if delta and self._turn_started_ns is None:
            self._turn_started_ns = time.monotonic_ns()
        segments = self._segmenter.push(delta)
        if done:
            segments.extend(self._segmenter.flush())
        await self._enqueue(
            segments,
            delivery_caption=self._turn_delivery_caption,
        )
        if done:
            self._reset_turn_state()

    async def invalidate(self, *, reason: InvalidationReason = "owner_request") -> None:
        async with self._condition:
            self._generation += 1
            generation = self._generation
            self._suppressed = True
            self._segmenter.clear()
            self._reset_turn_state()
            self._items.clear()
            active = self._active
            if active is not None and not active.done() and self._active_cancel_reason is None:
                self._active_cancel_reason = reason
            self._condition.notify_all()
        safe_event(
            logger,
            "speech_invalidated",
            component="speech",
            event_code=reason,
            generation=generation,
            state="invalidating",
        )
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
        await self.invalidate(reason="queue_close")
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
        worker = self._worker
        if worker is not None:
            with suppress(asyncio.CancelledError):
                await worker

    async def _enqueue(
        self,
        segments: list[TranscriptSegment],
        *,
        delivery_caption: str | None,
    ) -> None:
        if not segments:
            return
        enqueued_ns = time.monotonic_ns()
        segment_wait_ms = self._segment_wait_ms(enqueued_ns)
        async with self._condition:
            starting_depth = len(self._items)
            items: list[_SpeechItem] = []
            for segment in segments:
                items.append(
                    _SpeechItem(
                        text=segment.text,
                        delivery_caption=delivery_caption,
                        audio_id=self._reserve_audio_id(),
                        generation=self._generation,
                        reason=segment.reason,
                        segment_index=self._next_segment_index,
                        segment_wait_ms=segment_wait_ms,
                    ),
                )
                self._next_segment_index += 1
            self._items.extend(items)
            for offset, item in enumerate(items, start=1):
                safe_event(
                    logger,
                    "speech_segment_ready",
                    include_trace_id=False,
                    audio_id=item.audio_id,
                    component="speech",
                    duration_ms=item.segment_wait_ms,
                    generation=item.generation,
                    queue_depth=starting_depth + offset,
                    segment_index=item.segment_index,
                    segment_reason=item.reason,
                    text_chars=len(item.text),
                )
            self._condition.notify_all()

    def _segment_wait_ms(self, enqueued_ns: int) -> int:
        if self._turn_started_ns is None:
            return 0
        return max(0, (enqueued_ns - self._turn_started_ns) // 1_000_000)

    def _reset_turn_state(self) -> None:
        self._turn_started_ns = None
        self._turn_delivery_caption = None
        self._turn_caption_set = False
        self._next_segment_index = 1

    def _reserve_local_audio_id(self) -> int:
        self._local_audio_id += 1
        return self._local_audio_id

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
                        self._active_cancel_reason = None
                    self._busy = False
                    self._condition.notify_all()

    async def _process_item(self, item: _SpeechItem) -> None:
        started_ns = time.monotonic_ns()
        safe_event(
            logger,
            "synthesis_started",
            audio_id=item.audio_id,
            component="speech",
            boundary="irodori_http",
            generation=item.generation,
            queue_depth=len(self._items),
            text_chars=len(item.text),
        )
        try:
            if item.delivery_caption is None:
                wav = await self._synthesizer.synthesize(item.text)
            else:
                synthesizer = cast("CaptionSynthesizer", self._synthesizer)
                wav = await synthesizer.synthesize(
                    item.text,
                    delivery_caption=item.delivery_caption,
                )
        except asyncio.CancelledError:
            safe_event(
                logger,
                "synthesis_cancelled",
                audio_id=item.audio_id,
                component="speech",
                boundary="irodori_http",
                duration_ms=_elapsed_ms(started_ns),
                event_code=self._active_cancel_reason or "owner_request",
                generation=item.generation,
                text_chars=len(item.text),
            )
            raise
        except IrodoriError as error:
            code = error.code if error.code in _PUBLIC_IRODORI_ERROR_CODES else "synthesis_failed"
            self._error_codes.append(code)
            safe_event(
                logger,
                "synthesis_failed",
                audio_id=item.audio_id,
                component="speech",
                boundary="irodori_http",
                duration_ms=_elapsed_ms(started_ns),
                event_code=code,
                generation=item.generation,
                result="error",
                text_chars=len(item.text),
            )
            await self._report_error(code)
            return
        except Exception:  # noqa: BLE001
            code = "synthesis_failed"
            self._error_codes.append(code)
            safe_event(
                logger,
                "synthesis_failed",
                audio_id=item.audio_id,
                component="speech",
                boundary="irodori_http",
                duration_ms=_elapsed_ms(started_ns),
                event_code=code,
                generation=item.generation,
                result="error",
                text_chars=len(item.text),
            )
            await self._report_error(code)
            return

        safe_event(
            logger,
            "synthesis_completed",
            audio_id=item.audio_id,
            component="speech",
            boundary="irodori_http",
            duration_ms=_elapsed_ms(started_ns),
            generation=item.generation,
            result="ok",
            text_chars=len(item.text),
            wav_bytes=len(wav),
        )

        if item.generation != self._generation:
            return
        try:
            result = self._deliver(wav, item.audio_id, item.generation)
            if inspect.isawaitable(result):
                await cast("Awaitable[object]", result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._error_codes.append("audio_delivery_failed")
            await self._report_error("audio_delivery_failed")

    async def _report_error(self, code: str) -> None:
        if self._on_error is None:
            return
        try:
            result = self._on_error(code)
            if inspect.isawaitable(result):
                await cast("Awaitable[object]", result)
        except Exception:  # noqa: BLE001
            safe_event(
                logger,
                "speech_error_notification_failed",
                component="speech",
                boundary="browser_websocket",
                event_code="speech_error_notification_failed",
                result="error",
            )


def _elapsed_ms(started_ns: int) -> int:
    return (time.monotonic_ns() - started_ns) // 1_000_000
