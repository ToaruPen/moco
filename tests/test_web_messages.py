from __future__ import annotations

import pytest
from pydantic import ValidationError

from moco.web.messages import (
    ClientControl,
    ControlMessage,
    PlaybackMessage,
    StartMessage,
    parse_client_message,
)


def test_parses_start_control_and_playback_messages() -> None:
    assert parse_client_message({"type": "start", "sdp": "offer"}) == StartMessage(
        sdp="offer",
    )
    assert parse_client_message(
        {"type": "control", "control": "ptt_down"},
    ) == ControlMessage(control=ClientControl.PTT_DOWN)
    assert parse_client_message(
        {"type": "playback", "active": True},
    ) == PlaybackMessage(active=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "start", "sdp": ""},
        {"type": "control", "control": "unknown"},
        {"type": "playback", "active": "yes"},
        {"type": "stop", "extra": True},
        {"type": "unknown"},
    ],
)
def test_rejects_invalid_or_extra_fields(payload: object) -> None:
    with pytest.raises(ValidationError):
        parse_client_message(payload)
