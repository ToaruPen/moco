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
        {
            "type": "playback",
            "phase": "started",
            "audio_id": 17,
            "generation": 3,
            "context_state": "running",
        },
    ) == PlaybackMessage(
        phase="started",
        audio_id=17,
        generation=3,
        context_state="running",
    )
    assert parse_client_message(
        {
            "type": "playback",
            "phase": "completed",
            "audio_id": 17,
            "generation": 3,
            "context_state": "running",
        },
    ) == PlaybackMessage(
        phase="completed",
        audio_id=17,
        generation=3,
        context_state="running",
    )
    assert parse_client_message(
        {
            "type": "playback",
            "phase": "failed",
            "audio_id": 18,
            "generation": 3,
            "context_state": "suspended",
        },
    ) == PlaybackMessage(
        phase="failed",
        audio_id=18,
        generation=3,
        context_state="suspended",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "playback",
            "active": True,
            "phase": "started",
            "audio_id": 17,
            "generation": 3,
            "context_state": "running",
        },
        {"type": "playback", "phase": "stopped"},
    ],
)
def test_playback_contract_rejects_client_activity_and_uncorrelated_stop(
    payload: object,
) -> None:
    with pytest.raises(ValidationError):
        parse_client_message(payload)


def test_parses_and_normalizes_an_opaque_voice_id() -> None:
    assert parse_client_message(
        {"type": "select_voice", "voice_id": "  fixture-0  "},
    ) == SelectVoiceMessage(voice_id="fixture-0")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "start", "sdp": ""},
        {"type": "control", "control": "unknown"},
        {"type": "playback"},
        {"type": "playback", "phase": "started"},
        {"type": "playback", "phase": "completed", "audio_id": 17},
        {
            "type": "playback",
            "phase": "unknown",
            "audio_id": 17,
            "generation": 3,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "stopped",
            "audio_id": 17,
            "generation": 3,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": -1,
            "generation": 3,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": 17,
            "generation": -1,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": "17",
            "generation": 3,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": True,
            "generation": 3,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": 17,
            "generation": 3.5,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": 17,
            "generation": True,
            "context_state": "running",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": 17,
            "generation": 3,
            "context_state": "unknown",
        },
        {
            "type": "playback",
            "phase": "completed",
            "audio_id": 17,
            "generation": None,
            "context_state": "closed",
        },
        {
            "type": "playback",
            "phase": "started",
            "audio_id": 17,
            "generation": 3,
            "context_state": "running",
            "extra": True,
        },
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
