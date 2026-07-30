from __future__ import annotations

import pytest

from moco.speech.text import TranscriptSegmenter, is_speakable, strip_control_emojis


def test_segments_complete_sentences_and_trailing_closers() -> None:
    segmenter = TranscriptSegmenter(max_chars=80)

    assert segmenter.push("「こんにちは！」「次です") == ["「こんにちは！」"]
    assert segmenter.flush() == ["「次です"]


def test_long_text_prefers_soft_break() -> None:
    segmenter = TranscriptSegmenter(max_chars=8)

    assert segmenter.push("あいうえお、かきくけこ") == ["あいうえお、"]
    assert segmenter.flush() == ["かきくけこ"]


def test_control_emoji_only_text_is_not_speakable() -> None:
    assert not is_speakable(" 😮‍💨 🤔 。")
    assert is_speakable("🤔 考えます。")
    assert strip_control_emojis("🤔考えます。") == "考えます。"


def test_rejects_non_positive_segment_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        TranscriptSegmenter(max_chars=0)
