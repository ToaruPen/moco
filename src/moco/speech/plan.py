from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_PHYSICAL_LINE_BOUNDARY = re.compile(r"(?<=\n)|(?<=\r)(?!\n)")
_FORBIDDEN_CAPTION_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


@dataclass(frozen=True, slots=True)
class SpeechPlanResult:
    body: str
    delivery_caption: str | None
    error_code: Literal["speech_caption_invalid"] | None
    plan_chars: int
    plan_present: bool

    @classmethod
    def plain(cls, body: str) -> SpeechPlanResult:
        return cls(
            body=body,
            delivery_caption=None,
            error_code=None,
            plan_chars=0,
            plan_present=False,
        )


class _SpeechPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["moco.speech_plan"]
    version: int = Field(strict=True)
    delivery_caption: str | None

    @field_validator("version")
    @classmethod
    def _require_version_one(cls, value: int) -> int:
        if value != 1:
            message = "unsupported speech plan version"
            raise ValueError(message)
        return value


def normalize_delivery_caption(value: str, *, max_chars: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars:
        message = "delivery caption length is invalid"
        raise ValueError(message)
    if any(unicodedata.category(char) in _FORBIDDEN_CAPTION_CATEGORIES for char in normalized):
        message = "delivery caption contains a control character"
        raise ValueError(message)
    if "<" in normalized or ">" in normalized:
        message = "delivery caption contains a forbidden delimiter"
        raise ValueError(message)
    return normalized


def parse_speech_plan(text: str, *, max_chars: int) -> SpeechPlanResult:
    lines = _PHYSICAL_LINE_BOUNDARY.split(text)
    candidate_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if candidate_index is None:
        return SpeechPlanResult.plain(text)

    candidate = lines[candidate_index].rstrip("\r\n")
    if not candidate.lstrip().startswith("{"):
        return SpeechPlanResult.plain(text)

    body = "".join((*lines[:candidate_index], *lines[candidate_index + 1 :]))
    invalid = SpeechPlanResult(
        body=body,
        delivery_caption=None,
        error_code="speech_caption_invalid",
        plan_chars=len(candidate),
        plan_present=True,
    )
    try:
        payload = json.loads(candidate, object_pairs_hook=_unique_object)
        plan = _SpeechPlan.model_validate(payload, strict=True)
        if not body.strip():
            return invalid
        caption = (
            normalize_delivery_caption(plan.delivery_caption, max_chars=max_chars)
            if plan.delivery_caption is not None
            else None
        )
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return invalid

    return SpeechPlanResult(
        body=body,
        delivery_caption=caption,
        error_code=None,
        plan_chars=len(candidate),
        plan_present=True,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            message = f"duplicate JSON key: {key}"
            raise ValueError(message)
        result[key] = value
    return result
