from __future__ import annotations

from typing import Literal, Self

from irodori_tts_infra.contracts import (
    EmojiCapability,
    Readiness,
    SynthesisRequest,
    VoiceCapability,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DeliveryCaptionCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supported: bool = False
    max_chars: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _require_matching_limit(self) -> Self:
        if self.supported != (self.max_chars is not None):
            message = "max_chars must be present exactly when delivery captions are supported"
            raise ValueError(message)
        return self


class ConditioningCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_caption: DeliveryCaptionCapability = Field(
        default_factory=DeliveryCaptionCapability,
    )
    emoji: EmojiCapability = Field(default_factory=EmojiCapability)


class IrodoriCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    generation: str = Field(min_length=1)
    ready: bool
    readiness: Readiness
    voices: tuple[VoiceCapability, ...]
    conditioning: ConditioningCapabilities = Field(
        default_factory=ConditioningCapabilities,
    )

    @field_validator("generation")
    @classmethod
    def _normalize_generation(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            message = "generation must not be blank"
            raise ValueError(message)
        return stripped

    @model_validator(mode="after")
    def _validate_readiness_and_catalog(self) -> Self:
        if self.ready != (self.readiness == "ready"):
            message = "ready must be true exactly when readiness is ready"
            raise ValueError(message)

        ids = [voice.id for voice in self.voices]
        aliases = [alias for voice in self.voices for alias in voice.aliases]
        if len(ids) != len(set(ids)):
            message = "voice catalog IDs must be unique"
            raise ValueError(message)
        if len(aliases) != len(set(aliases)):
            message = "voice catalog aliases must be unique"
            raise ValueError(message)
        if set(ids) & set(aliases):
            message = "voice catalog aliases must not collide with IDs"
            raise ValueError(message)
        if sum(voice.default for voice in self.voices) > 1:
            message = "voice catalog must contain at most one default"
            raise ValueError(message)
        return self


class IrodoriSynthesisRequest(SynthesisRequest):
    delivery_caption: str | None = None
