from __future__ import annotations

import asyncio

from moco.speech.irodori import IrodoriError
from moco.speech.queue import SpeechQueue


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_once = False
        self.gate: asyncio.Event | None = None

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail_once:
            self.fail_once = False
            message = "bad response"
            raise IrodoriError(message, code="invalid_response")
        if self.gate is not None:
            await self.gate.wait()
        return f"wav:{text}".encode()


class DeliveryFailureError(RuntimeError):
    """Synthetic playback delivery failure."""


async def test_worker_synthesizes_and_delivers_fifo() -> None:
    synthesizer = FakeSynthesizer()
    delivered: list[bytes] = []
    queue = SpeechQueue(synthesizer, deliver=delivered.append, max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta="一つ。二つ。", done=True)
    await queue.join()

    assert synthesizer.calls == ["一つ。", "二つ。"]
    assert delivered == ["wav:一つ。".encode(), "wav:二つ。".encode()]
    await queue.close()


async def test_cancel_discards_active_generation_before_delivery() -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.gate = asyncio.Event()
    delivered: list[bytes] = []
    queue = SpeechQueue(synthesizer, deliver=delivered.append, max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await asyncio.sleep(0)

    await queue.cancel()
    synthesizer.gate.set()
    await queue.join()

    assert delivered == []
    await queue.close()


async def test_cancel_suppresses_assistant_until_next_user_turn() -> None:
    synthesizer = FakeSynthesizer()
    queue = SpeechQueue(synthesizer, deliver=lambda _wav: None, max_chars=80)

    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await queue.cancel()
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
    queue = SpeechQueue(synthesizer, deliver=delivered.append, max_chars=80)
    queue.start()

    await queue.on_transcript(role="assistant", delta="失敗。成功。", done=True)
    await queue.join()

    assert queue.error_codes == ("invalid_response",)
    assert delivered == ["wav:成功。".encode()]
    await queue.close()


async def test_cancel_waits_for_and_stops_in_progress_delivery() -> None:
    synthesizer = FakeSynthesizer()
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivered: list[bytes] = []

    async def gated_delivery(wav: bytes) -> None:
        delivery_started.set()
        await release_delivery.wait()
        delivered.append(wav)

    queue = SpeechQueue(synthesizer, deliver=gated_delivery, max_chars=80)
    queue.start()
    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await delivery_started.wait()

    await queue.cancel()
    release_delivery.set()
    await queue.join()

    assert delivered == []
    await queue.close()


async def test_delivery_failure_does_not_break_worker_or_cleanup() -> None:
    synthesizer = FakeSynthesizer()
    attempts = 0

    async def failing_delivery(_wav: bytes) -> None:
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
