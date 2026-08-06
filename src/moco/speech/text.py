from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SENTENCE_ENDS = frozenset("。！？!?")
TRAILING_CLOSERS = frozenset("」』）】》”’")
SOFT_BREAKS = frozenset("、，,；;：:")
CONTROL_EMOJIS = (
    "😮‍💨",
    "👂",
    "⏸️",
    "🤭",
    "🥵",
    "📢",
    "😏",
    "🥺",
    "🌬️",
    "😮",
    "👅",
    "💋",
    "🫶",
    "😭",
    "😱",
    "😪",
    "⏩",
    "📞",
    "🐢",
    "🥤",
    "🤧",
    "😒",
    "😰",
    "😆",
    "😠",
    "😲",
    "🥱",
    "😖",
    "😟",
    "🫣",
    "🙄",
    "😊",
    "👌",
    "🙏",
    "🥴",
    "🎵",
    "🤐",
    "😌",
    "🤔",
)
NON_SPEECH_RE = re.compile(r"[\s。！？!?、，,；;：:…「」『』（）【】《》“”‘’]+")

type SegmentReason = Literal[
    "sentence_end",
    "first_soft_break",
    "max_chars",
    "turn_flush",
]


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    reason: SegmentReason


def strip_control_emojis(text: str) -> str:
    stripped = text
    for emoji in CONTROL_EMOJIS:
        stripped = stripped.replace(emoji, "")
    return stripped


def is_speakable(text: str) -> bool:
    visible = strip_control_emojis(text)
    return bool(NON_SPEECH_RE.sub("", visible))


class TranscriptSegmenter:
    def __init__(
        self,
        max_chars: int,
        first_segment_soft_break_min_chars: int | None = None,
    ) -> None:
        if max_chars <= 0:
            msg = "max_chars must be positive"
            raise ValueError(msg)
        self._max_chars = max_chars
        self._first_segment_soft_break_min_chars = first_segment_soft_break_min_chars
        self._buffer = ""
        self._first_segment_emitted = False

    def push(self, delta: str) -> list[TranscriptSegment]:
        self._buffer += delta
        return self._take_ready()

    def flush(self) -> list[TranscriptSegment]:
        ready = self._take_ready()
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder:
            self._append_if_speakable(ready, remainder, "turn_flush")
        self._first_segment_emitted = False
        return ready

    def clear(self) -> None:
        self._buffer = ""
        self._first_segment_emitted = False

    def _take_ready(self) -> list[TranscriptSegment]:
        ready: list[TranscriptSegment] = []
        while self._buffer:
            sentence_end = self._sentence_end()
            if sentence_end is not None:
                segment = self._take(sentence_end)
                reason: SegmentReason = "sentence_end"
            elif (
                not self._first_segment_emitted
                and self._first_segment_soft_break_min_chars is not None
                and (soft_break := self._first_soft_break()) is not None
            ):
                segment = self._take(soft_break)
                reason = "first_soft_break"
            elif len(self._buffer) >= self._max_chars:
                segment = self._take(self._long_text_cut())
                reason = "max_chars"
            else:
                break
            self._append_if_speakable(ready, segment, reason)
        return ready

    def _append_if_speakable(
        self,
        ready: list[TranscriptSegment],
        text: str,
        reason: SegmentReason,
    ) -> None:
        if is_speakable(text):
            ready.append(TranscriptSegment(text, reason))
            self._first_segment_emitted = True

    def _sentence_end(self) -> int | None:
        for index, character in enumerate(self._buffer):
            if character not in SENTENCE_ENDS:
                continue
            end = index + 1
            while end < len(self._buffer) and self._buffer[end] in SENTENCE_ENDS:
                end += 1
            while end < len(self._buffer) and self._buffer[end] in TRAILING_CLOSERS:
                end += 1
            return end
        return None

    def _long_text_cut(self) -> int:
        for index in range(self._max_chars - 1, -1, -1):
            if self._buffer[index] in SOFT_BREAKS:
                return index + 1
        return self._max_chars

    def _first_soft_break(self) -> int | None:
        minimum = self._first_segment_soft_break_min_chars
        if minimum is None:
            return None
        for index in range(min(len(self._buffer), self._max_chars)):
            if self._buffer[index] in SOFT_BREAKS and index + 1 >= minimum:
                return index + 1
        return None

    def _take(self, end: int) -> str:
        segment = self._buffer[:end].strip()
        self._buffer = self._buffer[end:].lstrip()
        return segment
