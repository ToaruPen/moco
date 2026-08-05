from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter, field_validator


class ClientControl(StrEnum):
    LISTEN_START = "listen_start"
    LISTEN_STOP = "listen_stop"


class _ClientMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartMessage(_ClientMessage):
    type: Literal["start"] = "start"
    sdp: str = Field(min_length=1)


class ControlMessage(_ClientMessage):
    type: Literal["control"] = "control"
    control: ClientControl


class PlaybackMessage(_ClientMessage):
    type: Literal["playback"] = "playback"
    active: StrictBool


class SelectVoiceMessage(_ClientMessage):
    type: Literal["select_voice"] = "select_voice"
    speaker: str | None

    @field_validator("speaker")
    @classmethod
    def _normalize_speaker(cls, value: str | None) -> str | None:
        if value is None:
            return None
        speaker = value.strip()
        if not speaker:
            msg = "speaker must not be blank"
            raise ValueError(msg)
        return speaker


class StopMessage(_ClientMessage):
    type: Literal["stop"] = "stop"


ClientMessage = Annotated[
    StartMessage | ControlMessage | PlaybackMessage | SelectVoiceMessage | StopMessage,
    Field(discriminator="type"),
]
_CLIENT_MESSAGE_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(payload: object) -> ClientMessage:
    return _CLIENT_MESSAGE_ADAPTER.validate_python(payload)
