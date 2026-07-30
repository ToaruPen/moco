from __future__ import annotations

from moco.runtime.hotkeys import Control, HotkeyMapper


def test_key_repeat_emits_one_ptt_pair() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(ptt_key="f1", cancel_key="f2", emit=emitted.append)

    mapper.key_down("f1")
    mapper.key_down("f1")
    mapper.key_up("f1")

    assert emitted == [Control.PTT_DOWN, Control.PTT_UP]


def test_key_up_without_matching_down_is_ignored() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(ptt_key="f1", cancel_key="f2", emit=emitted.append)

    mapper.key_up("f1")

    assert emitted == []


def test_cancel_emits_once_per_physical_press() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(ptt_key="f1", cancel_key="f2", emit=emitted.append)

    mapper.key_down("f2")
    mapper.key_down("f2")
    mapper.key_up("f2")
    mapper.key_down("f2")
    mapper.key_up("f2")

    assert emitted == [Control.CANCEL, Control.CANCEL]


def test_unconfigured_keys_are_ignored() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(ptt_key="f1", cancel_key="f2", emit=emitted.append)

    mapper.key_down("f3")
    mapper.key_up("f3")

    assert emitted == []
