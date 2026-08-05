from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from pynput import keyboard

from moco.runtime.hotkeys import Control, GlobalHotkeyListener, HotkeyMapper


def test_key_repeat_emits_one_listen_start() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(start_key="f1", stop_key="f2", emit=emitted.append)

    mapper.key_down("f1")
    mapper.key_down("f1")
    mapper.key_up("f1")

    assert emitted == [Control.LISTEN_START]


def test_key_up_without_matching_down_is_ignored() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(start_key="f1", stop_key="f2", emit=emitted.append)

    mapper.key_up("f1")

    assert emitted == []


def test_listen_stop_emits_once_per_physical_press() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(start_key="f1", stop_key="f2", emit=emitted.append)

    mapper.key_down("f2")
    mapper.key_down("f2")
    mapper.key_up("f2")
    mapper.key_down("f2")
    mapper.key_up("f2")

    assert emitted == [Control.LISTEN_STOP, Control.LISTEN_STOP]


def test_unconfigured_keys_are_ignored() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(start_key="f1", stop_key="f2", emit=emitted.append)

    mapper.key_down("f3")
    mapper.key_up("f3")

    assert emitted == []


def test_bindings_are_not_coupled_to_function_key_defaults() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(start_key="space", stop_key="escape", emit=emitted.append)

    mapper.key_down("space")
    mapper.key_up("space")
    mapper.key_down("escape")

    assert emitted == [Control.LISTEN_START, Control.LISTEN_STOP]


class ImmediateLoop:
    def call_soon_threadsafe(
        self,
        callback: Callable[..., object],
        *args: object,
    ) -> None:
        callback(*args)


class FakeListener:
    instances: ClassVar[list[FakeListener]] = []

    def __init__(self, *, on_press: object, on_release: object) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.running = False
        self.stopped = False
        self.waited = False
        self.instances.append(self)

    def start(self) -> None:
        self.running = True

    def wait(self) -> None:
        self.waited = True

    def stop(self) -> None:
        self.running = False
        self.stopped = True


def test_global_listener_maps_named_and_character_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeListener.instances.clear()
    monkeypatch.setattr(keyboard, "Listener", FakeListener)
    emitted: list[Control] = []
    mapper = HotkeyMapper(start_key="f1", stop_key="x", emit=emitted.append)
    listener = GlobalHotkeyListener(
        loop=cast("asyncio.AbstractEventLoop", ImmediateLoop()),
        mapper=mapper,
    )

    listener.start()
    listener.start()
    backend = FakeListener.instances[0]
    cast("Callable[[object], None]", backend.on_press)(
        SimpleNamespace(name="f1"),
    )
    cast("Callable[[object], None]", backend.on_release)(
        SimpleNamespace(name="f1"),
    )
    cast("Callable[[object], None]", backend.on_press)(
        SimpleNamespace(char="X"),
    )
    cast("Callable[[object], None]", backend.on_press)(object())

    assert listener.running
    assert len(FakeListener.instances) == 1
    assert backend.waited
    assert emitted == [Control.LISTEN_START, Control.LISTEN_STOP]

    listener.stop()
    listener.stop()
    assert backend.stopped
    assert not listener.running


class DeniedListener(FakeListener):
    IS_TRUSTED = False


def test_global_listener_reports_denied_platform_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    DeniedListener.instances.clear()
    monkeypatch.setattr(keyboard, "Listener", DeniedListener)
    listener = GlobalHotkeyListener(
        loop=cast("asyncio.AbstractEventLoop", ImmediateLoop()),
        mapper=HotkeyMapper(
            start_key="v",
            stop_key="escape",
            emit=lambda _control: None,
        ),
    )

    listener.start()

    assert not listener.running
