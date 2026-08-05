from __future__ import annotations

import pytest
from pydantic import ValidationError

from moco.web.messages import (
    ClientControl,
    ControlMessage,
    PlaybackMessage,
    SelectVoiceMessage,
    StartMessage,
    parse_client_message,
)


def test_parses_start_control_and_playback_messages() -> None:
    assert parse_client_message({"type": "start", "sdp": "offer"}) == StartMessage(
        sdp="offer",
    )
    assert parse_client_message(
        {"type": "control", "control": "listen_start"},
    ) == ControlMessage(control=ClientControl.LISTEN_START)
    assert parse_client_message(
        {"type": "control", "control": "listen_stop"},
    ) == ControlMessage(control=ClientControl.LISTEN_STOP)
    assert parse_client_message(
        {"type": "playback", "active": True},
    ) == PlaybackMessage(active=True)


def test_parses_and_normalizes_an_opaque_voice_id() -> None:
    assert parse_client_message(
        {"type": "select_voice", "voice_id": "  fixture-0  "},
    ) == SelectVoiceMessage(voice_id="fixture-0")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "start", "sdp": ""},
        {"type": "control", "control": "unknown"},
        {"type": "playback", "active": "yes"},
        {"type": "select_voice", "voice_id": None},
        {"type": "select_voice", "voice_id": ""},
        {"type": "select_voice", "voice_id": "   "},
        {
            "type": "select_voice",
            "speaker": "fixture-0",
        },
        {
            "type": "select_voice",
            "voice_id": "fixture-0",
            "speaker": "fixture-0",
        },
        {"type": "stop", "extra": True},
        {"type": "unknown"},
    ],
)
def test_rejects_invalid_or_extra_fields(payload: object) -> None:
    with pytest.raises(ValidationError):
        parse_client_message(payload)
