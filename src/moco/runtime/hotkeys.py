from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pynput import keyboard

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable


class Control(StrEnum):
    PTT_DOWN = "ptt_down"
    PTT_UP = "ptt_up"
    CANCEL = "cancel"


class HotkeyMapper:
    def __init__(
        self,
        *,
        ptt_key: str,
        cancel_key: str,
        emit: Callable[[Control], object],
    ) -> None:
        self._ptt_key = ptt_key.lower()
        self._cancel_key = cancel_key.lower()
        self._emit = emit
        self._pressed: set[str] = set()

    def key_down(self, key: str) -> None:
        canonical = key.lower()
        if canonical in self._pressed:
            return
        if canonical == self._ptt_key:
            self._pressed.add(canonical)
            self._emit(Control.PTT_DOWN)
        elif canonical == self._cancel_key:
            self._pressed.add(canonical)
            self._emit(Control.CANCEL)

    def key_up(self, key: str) -> None:
        canonical = key.lower()
        if canonical not in self._pressed:
            return
        self._pressed.remove(canonical)
        if canonical == self._ptt_key:
            self._emit(Control.PTT_UP)


class GlobalHotkeyListener:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        mapper: HotkeyMapper,
    ) -> None:
        self._loop = loop
        self._mapper = mapper
        self._listener: keyboard.Listener | None = None

    @property
    def running(self) -> bool:
        listener = self._listener
        return listener is not None and listener.running

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()

    def stop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        listener.stop()
        self._listener = None

    def _on_press(self, key: object) -> None:
        canonical = _canonical_key(key)
        if canonical is not None:
            self._loop.call_soon_threadsafe(self._mapper.key_down, canonical)

    def _on_release(self, key: object) -> None:
        canonical = _canonical_key(key)
        if canonical is not None:
            self._loop.call_soon_threadsafe(self._mapper.key_up, canonical)


def _canonical_key(key: object) -> str | None:
    name = getattr(key, "name", None)
    if isinstance(name, str):
        return name.lower()
    character = getattr(key, "char", None)
    if isinstance(character, str):
        return character.lower()
    return None
