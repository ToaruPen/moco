from __future__ import annotations

import pytest

from moco.speech.text import (
    TranscriptSegment,
    TranscriptSegmenter,
    is_speakable,
    strip_control_emojis,
)


def test_segments_complete_sentences_and_trailing_closers() -> None:
    segmenter = TranscriptSegmenter(max_chars=80)

    assert segmenter.push("「こんにちは！」「次です") == [
        TranscriptSegment("「こんにちは！」", "sentence_end")
    ]
    assert segmenter.flush() == [TranscriptSegment("「次です", "turn_flush")]


def test_long_text_prefers_soft_break() -> None:
    segmenter = TranscriptSegmenter(max_chars=8)

    assert segmenter.push("あいうえお、かきくけこ") == [
        TranscriptSegment("あいうえお、", "max_chars")
    ]
    assert segmenter.flush() == [TranscriptSegment("かきくけこ", "turn_flush")]


def test_first_soft_break_waits_for_first_eligible_break() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=80,
        first_segment_soft_break_min_chars=18,
    )

    assert segmenter.push("あ" * 16 + "、続きます") == []
    assert segmenter.push("、さらに続きます") == [
        TranscriptSegment("あ" * 16 + "、続きます、", "first_soft_break")
    ]


def test_first_soft_break_does_not_extend_past_segment_limit() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=8,
        first_segment_soft_break_min_chars=4,
    )

    assert segmenter.push("あ" * 8 + "、続きます") == [TranscriptSegment("あ" * 8, "max_chars")]


def test_sentence_end_takes_priority_over_first_soft_break() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=80,
        first_segment_soft_break_min_chars=18,
    )
    text = "あ" * 18 + "、最後まで読みます。"

    assert segmenter.push(text) == [TranscriptSegment(text, "sentence_end")]


def test_first_soft_break_is_only_used_for_first_segment() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=80,
        first_segment_soft_break_min_chars=4,
    )

    assert segmenter.push("一つ目、") == [TranscriptSegment("一つ目、", "first_soft_break")]
    assert segmenter.push("二つ目の途中、まだ続く") == []


def test_none_disables_first_soft_break() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=80,
        first_segment_soft_break_min_chars=None,
    )

    assert segmenter.push("あ" * 18 + "、") == []


def test_flush_resets_first_segment_state_for_next_turn() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=80,
        first_segment_soft_break_min_chars=4,
    )

    assert segmenter.push("一つ目、") == [TranscriptSegment("一つ目、", "first_soft_break")]
    assert segmenter.push("残り") == []
    assert segmenter.flush() == [TranscriptSegment("残り", "turn_flush")]
    assert segmenter.push("次の話、") == [TranscriptSegment("次の話、", "first_soft_break")]


def test_clear_resets_first_segment_state_for_next_turn() -> None:
    segmenter = TranscriptSegmenter(
        max_chars=80,
        first_segment_soft_break_min_chars=4,
    )

    assert segmenter.push("一つ目、") == [TranscriptSegment("一つ目、", "first_soft_break")]
    segmenter.clear()

    assert segmenter.push("次の話、") == [TranscriptSegment("次の話、", "first_soft_break")]


def test_control_emoji_only_text_is_not_speakable() -> None:
    assert not is_speakable(" 😮‍💨 🤔 。")
    assert is_speakable("🤔 考えます。")
    assert strip_control_emojis("🤔考えます。") == "考えます。"


def test_rejects_non_positive_segment_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        TranscriptSegmenter(max_chars=0)
