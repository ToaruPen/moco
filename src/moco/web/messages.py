from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]


class ClientControl(StrEnum):
    LISTEN_START = "listen_start"
    LISTEN_STOP = "listen_stop"
    TURN_CANCEL = "turn_cancel"


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
    phase: Literal["started", "completed", "failed"]
    audio_id: NonNegativeStrictInt
    generation: NonNegativeStrictInt
    context_state: Literal["running", "suspended", "closed", "interrupted"]


class SelectVoiceMessage(_ClientMessage):
    type: Literal["select_voice"] = "select_voice"
    voice_id: str

    @field_validator("voice_id")
    @classmethod
    def _normalize_voice_id(cls, value: str) -> str:
        voice_id = value.strip()
        if not voice_id:
            msg = "voice_id must not be blank"
            raise ValueError(msg)
        return voice_id


class StopMessage(_ClientMessage):
    type: Literal["stop"] = "stop"


class VoiceLostMessage(_ClientMessage):
    type: Literal["voice_lost"] = "voice_lost"


ClientMessage = Annotated[
    StartMessage
    | ControlMessage
    | PlaybackMessage
    | SelectVoiceMessage
    | StopMessage
    | VoiceLostMessage,
    Field(discriminator="type"),
]
_CLIENT_MESSAGE_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(payload: object) -> ClientMessage:
    return _CLIENT_MESSAGE_ADAPTER.validate_python(payload)
