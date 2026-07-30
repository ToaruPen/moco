from __future__ import annotations

import re

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


def strip_control_emojis(text: str) -> str:
    stripped = text
    for emoji in CONTROL_EMOJIS:
        stripped = stripped.replace(emoji, "")
    return stripped


def is_speakable(text: str) -> bool:
    visible = strip_control_emojis(text)
    return bool(NON_SPEECH_RE.sub("", visible))


class TranscriptSegmenter:
    def __init__(self, *, max_chars: int) -> None:
        if max_chars <= 0:
            msg = "max_chars must be positive"
            raise ValueError(msg)
        self._max_chars = max_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        self._buffer += delta
        return self._take_ready()

    def flush(self) -> list[str]:
        ready = self._take_ready()
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder and is_speakable(remainder):
            ready.append(remainder)
        return ready

    def clear(self) -> None:
        self._buffer = ""

    def _take_ready(self) -> list[str]:
        ready: list[str] = []
        while self._buffer:
            sentence_end = self._sentence_end()
            if sentence_end is not None:
                segment = self._take(sentence_end)
            elif len(self._buffer) >= self._max_chars:
                segment = self._take(self._long_text_cut())
            else:
                break
            if is_speakable(segment):
                ready.append(segment)
        return ready

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

    def _take(self, end: int) -> str:
        segment = self._buffer[:end].strip()
        self._buffer = self._buffer[end:].lstrip()
        return segment
