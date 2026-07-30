from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter


class ClientControl(StrEnum):
    PTT_DOWN = "ptt_down"
    PTT_UP = "ptt_up"
    CANCEL = "cancel"


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


class StopMessage(_ClientMessage):
    type: Literal["stop"] = "stop"


ClientMessage = Annotated[
    StartMessage | ControlMessage | PlaybackMessage | StopMessage,
    Field(discriminator="type"),
]
_CLIENT_MESSAGE_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(payload: object) -> ClientMessage:
    message: ClientMessage = _CLIENT_MESSAGE_ADAPTER.validate_python(payload)
    return message
