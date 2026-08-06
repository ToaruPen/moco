from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import pytest
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span

from moco.speech import queue as speech_queue
from moco.speech.irodori import IrodoriError
from moco.speech.queue import SpeechQueue


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_once = False
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        self.started.set()
        if self.fail_once:
            self.fail_once = False
            message = "bad response"
            raise IrodoriError(message, code="invalid_response")
        if self.gate is not None:
            await self.gate.wait()
        return f"wav:{text}".encode()


class DeliveryFailureError(RuntimeError):
    """Synthetic playback delivery failure."""


class UnexpectedSynthesisError(OSError):
    """Synthetic unexpected synthesizer failure."""


def collect_audio(delivered: list[bytes]) -> speech_queue.Delivery:
    def deliver(wav: bytes, _audio_id: int, _generation: int) -> None:
        delivered.append(wav)

    return deliver


def ignore_audio(_wav: bytes, _audio_id: int, _generation: int) -> None:
    pass


def logged_event_attributes(message: str) -> dict[str, str]:
    return dict(part.split("=", maxsplit=1) for part in message.split()[1:])


def valid_test_span() -> NonRecordingSpan:
    return NonRecordingSpan(
        SpanContext(
            trace_id=int("1234567890abcdef1234567890abcdef", 16),
            span_id=int("1234567890abcdef", 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
    )


async def test_worker_synthesizes_and_delivers_fifo() -> None:
    synthesizer = FakeSynthesizer()
    delivered: list[bytes] = []
    queue = SpeechQueue(synthesizer, deliver=collect_audio(delivered), max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta="一つ。二つ。", done=True)
    await queue.join()

    assert synthesizer.calls == ["一つ。", "二つ。"]
    assert delivered == ["wav:一つ。".encode(), "wav:二つ。".encode()]
    await queue.close()


async def test_first_soft_break_segment_ready_logs_content_free_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = "あ" * 18 + "、"
    second = "続きます。"
    synthesizer = FakeSynthesizer()
    delivered: list[bytes] = []
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(
        synthesizer,
        deliver=collect_audio(delivered),
        max_chars=80,
        first_segment_soft_break_min_chars=18,
    )

    with use_span(valid_test_span()):
        await queue.on_transcript(role="assistant", delta=first, done=False)
        await queue.on_transcript(role="assistant", delta=second, done=True)
    queue.start()
    await queue.join()

    ready = [
        record.message
        for record in caplog.records
        if "event=speech_segment_ready" in record.message
    ]
    assert synthesizer.calls == [first, second]
    assert delivered == [f"wav:{first}".encode(), f"wav:{second}".encode()]
    assert len(ready) == 2
    first_metadata = logged_event_attributes(ready[0])
    assert first_metadata == {
        "audio_id": "1",
        "component": "speech",
        "duration_ms": first_metadata["duration_ms"],
        "generation": "0",
        "queue_depth": "1",
        "segment_index": "1",
        "segment_reason": "first_soft_break",
        "text_chars": "19",
    }
    assert int(first_metadata["duration_ms"]) >= 0
    second_metadata = logged_event_attributes(ready[1])
    assert second_metadata == {
        "audio_id": "2",
        "component": "speech",
        "duration_ms": second_metadata["duration_ms"],
        "generation": "0",
        "queue_depth": "2",
        "segment_index": "2",
        "segment_reason": "sentence_end",
        "text_chars": str(len(second)),
    }
    assert first not in caplog.text
    assert second not in caplog.text
    assert "voice" not in " ".join(ready)
    assert "runtime_generation" not in " ".join(ready)
    await queue.close()


async def test_segment_index_resets_for_next_assistant_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(FakeSynthesizer(), deliver=ignore_audio, max_chars=80)

    await queue.on_transcript(role="assistant", delta="一つ。二つ。", done=True)
    await queue.on_transcript(role="assistant", delta="次の返事。", done=True)

    ready = [
        logged_event_attributes(record.message)
        for record in caplog.records
        if "event=speech_segment_ready" in record.message
    ]
    assert [metadata["segment_index"] for metadata in ready] == ["1", "2", "1"]
    await queue.close()


async def test_segment_wait_starts_at_first_unfinished_nonempty_delta(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1_000_000, 26_000_000])
    monkeypatch.setattr(
        "moco.speech.queue.time.monotonic_ns",
        lambda: next(timestamps),
    )
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(
        FakeSynthesizer(),
        deliver=ignore_audio,
        max_chars=80,
        first_segment_soft_break_min_chars=18,
    )

    await queue.on_transcript(role="assistant", delta="あ" * 17, done=False)
    await queue.on_transcript(role="assistant", delta="あ、", done=False)

    ready = next(
        logged_event_attributes(record.message)
        for record in caplog.records
        if "event=speech_segment_ready" in record.message
    )
    assert ready["duration_ms"] == "25"
    await queue.close()


async def test_invalidate_resets_turn_timing_and_index() -> None:
    queue = SpeechQueue(FakeSynthesizer(), deliver=ignore_audio, max_chars=80)
    await queue.on_transcript(role="assistant", delta="未完了の一文。", done=False)

    await queue.invalidate()

    assert queue._turn_started_ns is None  # noqa: SLF001
    assert queue._next_segment_index == 1  # noqa: SLF001
    await queue.close()


async def test_user_done_resets_turn_timing_and_index(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1_000_000, 5_000_000, 20_000_000, 23_000_000])
    monkeypatch.setattr(
        "moco.speech.queue.time.monotonic_ns",
        lambda: next(timestamps),
    )
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(FakeSynthesizer(), deliver=ignore_audio, max_chars=80)

    await queue.on_transcript(role="assistant", delta="古い返事。", done=False)
    await queue.on_transcript(role="user", delta="割り込み", done=True)
    await queue.on_transcript(role="assistant", delta="新しい返事。", done=False)

    ready = [
        logged_event_attributes(record.message)
        for record in caplog.records
        if "event=speech_segment_ready" in record.message
    ]
    assert [metadata["segment_index"] for metadata in ready] == ["1", "1"]
    assert [metadata["duration_ms"] for metadata in ready] == ["4", "3"]
    await queue.close()


async def test_suppressed_and_closed_assistant_deltas_do_not_start_turn_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_calls = 0

    def monotonic_ns() -> int:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return monotonic_calls

    monkeypatch.setattr("moco.speech.queue.time.monotonic_ns", monotonic_ns)
    queue = SpeechQueue(FakeSynthesizer(), deliver=ignore_audio, max_chars=80)

    await queue.invalidate()
    await queue.on_transcript(role="assistant", delta="抑止中。", done=False)
    await queue.close()
    await queue.on_transcript(role="assistant", delta="終了後。", done=False)

    assert monotonic_calls == 0


async def test_invalidate_preserves_old_segment_correlation_for_next_user_turn(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([1_000_000, 5_000_000, 20_000_000, 23_000_000])
    monkeypatch.setattr(
        "moco.speech.queue.time.monotonic_ns",
        lambda: next(timestamps),
    )
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(FakeSynthesizer(), deliver=ignore_audio, max_chars=80)

    await queue.on_transcript(role="assistant", delta="古い一つ。古い二つ。", done=True)
    await queue.invalidate()
    await queue.on_transcript(role="assistant", delta="抑止中。", done=True)
    await queue.on_transcript(role="user", delta="次の質問", done=True)
    await queue.on_transcript(role="assistant", delta="新しい返事。", done=True)

    ready = [
        logged_event_attributes(record.message)
        for record in caplog.records
        if "event=speech_segment_ready" in record.message
    ]
    assert [metadata["audio_id"] for metadata in ready] == ["1", "2", "3"]
    assert [metadata["generation"] for metadata in ready] == ["0", "0", "1"]
    assert [metadata["segment_index"] for metadata in ready] == ["1", "2", "1"]
    assert [metadata["duration_ms"] for metadata in ready] == ["4", "4", "3"]
    assert queue.pending_texts == ("新しい返事。",)
    await queue.close()


async def test_successful_synthesis_logs_bounded_correlated_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = "秘密の本文。"
    synthesizer = FakeSynthesizer()
    delivered: list[bytes] = []
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(synthesizer, deliver=collect_audio(delivered), max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta=transcript, done=True)
    await queue.join()

    started = next(
        record.message for record in caplog.records if "event=synthesis_started" in record.message
    )
    completed = next(
        record.message for record in caplog.records if "event=synthesis_completed" in record.message
    )
    assert "boundary=irodori_http" in started
    assert "audio_id=1" in started
    assert "generation=0" in started
    assert "queue_depth=0" in started
    assert f"text_chars={len(transcript)}" in started
    assert "boundary=irodori_http" in completed
    assert "audio_id=1" in completed
    assert "generation=0" in completed
    assert f"wav_bytes={len(delivered[0])}" in completed
    assert "duration_ms=" in completed
    assert "result=ok" in completed
    assert transcript not in caplog.text
    assert delivered[0].decode() not in caplog.text
    await queue.close()


async def test_initial_generation_is_used_for_synthesis_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(
        FakeSynthesizer(),
        deliver=ignore_audio,
        max_chars=80,
        initial_generation=7,
    )
    queue.start()

    await queue.on_transcript(role="assistant", delta="相関確認。", done=True)
    await queue.join()

    started = next(
        record.message for record in caplog.records if "event=synthesis_started" in record.message
    )
    assert "generation=7" in started
    await queue.close()


@pytest.mark.parametrize("initial_generation", [-1, True, 1.5])
def test_initial_generation_requires_a_non_negative_strict_integer(
    initial_generation: object,
) -> None:
    with pytest.raises(ValueError, match="initial_generation"):
        SpeechQueue(
            FakeSynthesizer(),
            deliver=ignore_audio,
            max_chars=80,
            initial_generation=initial_generation,  # type: ignore[arg-type]
        )


async def test_streamed_final_suffix_is_synthesized_once() -> None:
    synthesizer = FakeSynthesizer()
    delivered: list[bytes] = []
    queue = SpeechQueue(synthesizer, deliver=collect_audio(delivered), max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta="確", done=False)
    await queue.on_transcript(role="assistant", delta="認します。", done=True)
    await queue.join()

    assert synthesizer.calls == ["確認します。"]
    assert delivered == ["wav:確認します。".encode()]
    await queue.close()


async def test_invalidate_discards_active_generation_before_delivery() -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.gate = asyncio.Event()
    delivered: list[bytes] = []
    queue = SpeechQueue(synthesizer, deliver=collect_audio(delivered), max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await asyncio.sleep(0)

    await queue.invalidate()
    synthesizer.gate.set()
    await queue.join()

    assert delivered == []
    await queue.close()


async def test_invalidate_logs_stable_reason_and_cancels_synthesis_without_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.gate = asyncio.Event()
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(synthesizer, deliver=ignore_audio, max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="取り消す本文。", done=True)
    await asyncio.wait_for(synthesizer.started.wait(), timeout=1)

    await queue.invalidate(reason="user_transcript")

    invalidated = next(
        record.message for record in caplog.records if "event=speech_invalidated" in record.message
    )
    cancelled = next(
        record.message for record in caplog.records if "event=synthesis_cancelled" in record.message
    )
    assert "event_code=user_transcript" in invalidated
    assert "generation=1" in invalidated
    assert "event_code=user_transcript" in cancelled
    assert "audio_id=1" in cancelled
    assert "generation=0" in cancelled
    assert "result=error" not in cancelled
    assert "synthesis_failed" not in caplog.text
    assert "取り消す本文" not in caplog.text
    await queue.close()


async def test_first_concurrent_invalidation_owns_active_cancellation_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class CancellationCleanupSynthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.cancellation_started = asyncio.Event()

        async def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            self.started.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=1)
            except asyncio.CancelledError:
                self.cancellation_started.set()
                await asyncio.wait_for(asyncio.Event().wait(), timeout=1)
                raise
            return b""

    synthesizer = CancellationCleanupSynthesizer()
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(synthesizer, deliver=ignore_audio, max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="競合する本文。", done=True)
    await asyncio.wait_for(synthesizer.started.wait(), timeout=1)

    user_invalidation = asyncio.create_task(
        queue.invalidate(reason="user_transcript"),
    )
    await asyncio.wait_for(synthesizer.cancellation_started.wait(), timeout=1)
    close_invalidation = asyncio.create_task(queue.invalidate(reason="queue_close"))
    await asyncio.wait_for(
        asyncio.gather(user_invalidation, close_invalidation),
        timeout=1,
    )

    cancelled = next(
        record.message for record in caplog.records if "event=synthesis_cancelled" in record.message
    )
    assert "event_code=user_transcript" in cancelled
    invalidations = [
        record.message for record in caplog.records if "event=speech_invalidated" in record.message
    ]
    assert any("event_code=user_transcript" in event for event in invalidations)
    assert any("event_code=queue_close" in event for event in invalidations)
    await queue.close()


async def test_close_invalidates_with_queue_close_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(FakeSynthesizer(), deliver=ignore_audio, max_chars=80)

    await queue.close()

    invalidated = next(
        record.message for record in caplog.records if "event=speech_invalidated" in record.message
    )
    assert "event_code=queue_close" in invalidated
    assert "generation=1" in invalidated


async def test_invalidate_suppresses_assistant_until_next_user_turn() -> None:
    synthesizer = FakeSynthesizer()
    queue = SpeechQueue(synthesizer, deliver=ignore_audio, max_chars=80)

    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await queue.invalidate()
    await queue.on_transcript(role="assistant", delta="まだ古い返事。", done=True)
    assert queue.pending_count == 0

    await queue.on_transcript(role="user", delta="次の質問", done=True)
    await queue.on_transcript(role="assistant", delta="新しい返事。", done=True)

    assert queue.pending_texts == ("新しい返事。",)
    await queue.close()


async def test_contract_error_does_not_kill_consumer() -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.fail_once = True
    delivered: list[bytes] = []
    queue = SpeechQueue(synthesizer, deliver=collect_audio(delivered), max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗。成功。", done=True)
    await queue.join()

    assert queue.error_codes == ("invalid_response",)
    assert delivered == ["wav:成功。".encode()]
    await queue.close()


async def test_failed_synthesis_does_not_reuse_reserved_audio_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.fail_once = True
    delivered: list[tuple[bytes, int, int]] = []

    def deliver(wav: bytes, audio_id: int, generation: int) -> None:
        delivered.append((wav, audio_id, generation))

    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    queue = SpeechQueue(synthesizer, deliver=deliver, max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗。成功。", done=True)
    await queue.join()

    started = [
        record.message for record in caplog.records if "event=synthesis_started" in record.message
    ]
    failed = next(
        record.message for record in caplog.records if "event=synthesis_failed" in record.message
    )
    assert ["audio_id=1" in event for event in started] == [True, False]
    assert "audio_id=2" in started[1]
    assert "audio_id=1" in failed
    assert delivered == [("wav:成功。".encode(), 2, 0)]
    await queue.close()


async def test_contract_error_is_reported_to_the_owner() -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.fail_once = True
    reported: list[str] = []
    queue = SpeechQueue(
        synthesizer,
        deliver=ignore_audio,
        max_chars=80,
        on_error=reported.append,
    )
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗。", done=True)
    await queue.join()

    assert reported == ["invalid_response"]
    await queue.close()


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
async def test_known_irodori_error_is_preserved_for_the_owner(code: str) -> None:
    private_message = "private adapter message"

    class FailingSynthesizer(FakeSynthesizer):
        async def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            raise IrodoriError(private_message, code=code)

    reported: list[str] = []
    queue = SpeechQueue(
        FailingSynthesizer(),
        deliver=ignore_audio,
        max_chars=80,
        on_error=reported.append,
    )
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗。", done=True)
    await queue.join()

    assert queue.error_codes == (code,)
    assert reported == [code]
    await queue.close()


async def test_forged_irodori_error_is_bounded_before_reporting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_code = "private_backend_detail"
    private_message = "private backend host and token"

    class ForgedSynthesizer(FakeSynthesizer):
        async def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            raise IrodoriError(private_message, code=private_code)

    caplog.set_level(logging.INFO, logger=speech_queue.logger.name)
    reported: list[str] = []
    queue = SpeechQueue(
        ForgedSynthesizer(),
        deliver=ignore_audio,
        max_chars=80,
        on_error=reported.append,
    )
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗。", done=True)
    await queue.join()

    assert queue.error_codes == ("synthesis_failed",)
    assert reported == ["synthesis_failed"]
    assert private_code not in caplog.text
    assert private_message not in caplog.text
    await queue.close()


async def test_unexpected_synthesis_error_is_reported_without_stopping_worker() -> None:
    class UnexpectedFailureSynthesizer(FakeSynthesizer):
        async def synthesize(self, text: str) -> bytes:
            self.calls.append(text)
            if len(self.calls) == 1:
                raise UnexpectedSynthesisError
            return f"wav:{text}".encode()

    synthesizer = UnexpectedFailureSynthesizer()
    delivered: list[bytes] = []
    reported: list[str] = []
    queue = SpeechQueue(
        synthesizer,
        deliver=collect_audio(delivered),
        max_chars=80,
        on_error=reported.append,
    )
    queue.start()
    await queue.on_transcript(role="assistant", delta="失敗。成功。", done=True)

    try:
        await asyncio.wait_for(queue.join(), timeout=0.2)
    finally:
        with suppress(Exception):
            await queue.close()

    assert queue.error_codes == ("synthesis_failed",)
    assert reported == ["synthesis_failed"]
    assert delivered == ["wav:成功。".encode()]


async def test_invalidate_waits_for_and_stops_in_progress_delivery() -> None:
    synthesizer = FakeSynthesizer()
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivered: list[bytes] = []

    async def gated_delivery(wav: bytes, _audio_id: int, _generation: int) -> None:
        delivery_started.set()
        await release_delivery.wait()
        delivered.append(wav)

    queue = SpeechQueue(synthesizer, deliver=gated_delivery, max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await asyncio.wait_for(delivery_started.wait(), timeout=1)

    await queue.invalidate()
    release_delivery.set()
    await queue.join()

    assert delivered == []
    await queue.close()


async def test_delivery_failure_does_not_break_worker_or_cleanup() -> None:
    synthesizer = FakeSynthesizer()
    attempts = 0

    async def failing_delivery(_wav: bytes, _audio_id: int, _generation: int) -> None:
        nonlocal attempts
        attempts += 1
        raise DeliveryFailureError

    queue = SpeechQueue(synthesizer, deliver=failing_delivery, max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="一つ。二つ。", done=True)

    await asyncio.wait_for(queue.join(), timeout=0.2)
    await queue.close()

    assert attempts == 2
    assert not queue.is_busy
    assert queue.error_codes == (
        "audio_delivery_failed",
        "audio_delivery_failed",
    )
