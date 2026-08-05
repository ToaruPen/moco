from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pynput import keyboard

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable


class Control(StrEnum):
    LISTEN_START = "listen_start"
    LISTEN_STOP = "listen_stop"


class HotkeyMapper:
    def __init__(
        self,
        *,
        start_key: str,
        stop_key: str,
        emit: Callable[[Control], object],
    ) -> None:
        self._start_key = start_key.lower()
        self._stop_key = stop_key.lower()
        self._emit = emit
        self._pressed: set[str] = set()

    def key_down(self, key: str) -> None:
        canonical = key.lower()
        if canonical in self._pressed:
            return
        if canonical == self._start_key:
            self._pressed.add(canonical)
            self._emit(Control.LISTEN_START)
        elif canonical == self._stop_key:
            self._pressed.add(canonical)
            self._emit(Control.LISTEN_STOP)

    def key_up(self, key: str) -> None:
        canonical = key.lower()
        if canonical not in self._pressed:
            return
        self._pressed.remove(canonical)


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
        return (
            listener is not None
            and listener.running
            and bool(getattr(listener, "IS_TRUSTED", True))
        )

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.start()
        self._listener.wait()

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
