from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_PHYSICAL_LINE_BOUNDARY = re.compile(r"(?<=\n)|(?<=\r)(?!\n)")
_FORBIDDEN_CAPTION_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_MAX_SPEECH_PLAN_PREFIX_CHARS = 8_192


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


@dataclass(frozen=True, slots=True)
class SpeechPlanUpdate:
    text: str
    delta: str
    done: bool
    delivery_caption: str | None
    plan: SpeechPlanResult | None


class SpeechPlanStream:
    """Remove an optional first-line speech plan without delaying plain speech."""

    def __init__(self, *, max_chars: int) -> None:
        if type(max_chars) is not int or max_chars <= 0:
            message = "caption maximum must be a positive integer"
            raise ValueError(message)
        self._max_chars = max_chars
        self._raw = ""
        self._emitted = ""
        self._plan_decided = False
        self._plan_present = False
        self._initial_plan_reported = False

    def push(self, text: str, *, done: bool = False) -> SpeechPlanUpdate | None:
        if type(text) is not str:
            message = "speech plan stream text must be a string"
            raise TypeError(message)
        self._raw = text if done else self._raw + text
        plan: SpeechPlanResult | None = None

        if not self._plan_decided:
            decision = self._decide(done=done)
            if decision is None:
                return None
            plan = decision if decision.plan_present else None
            self._plan_decided = True
            self._plan_present = decision.plan_present

        current = (
            parse_speech_plan(self._raw, max_chars=self._max_chars)
            if self._plan_present
            else SpeechPlanResult.plain(self._raw)
        )
        delta = current.body[len(self._emitted) :] if current.body.startswith(self._emitted) else ""
        self._emitted = current.body
        if self._plan_present and not self._initial_plan_reported:
            plan = current
            self._initial_plan_reported = True
        update = SpeechPlanUpdate(
            text=current.body,
            delta=delta,
            done=done,
            delivery_caption=current.delivery_caption if plan is not None else None,
            plan=plan,
        )
        if done:
            self.reset()
        return update

    def reset(self) -> None:
        self._raw = ""
        self._emitted = ""
        self._plan_decided = False
        self._plan_present = False
        self._initial_plan_reported = False

    def _decide(self, *, done: bool) -> SpeechPlanResult | None:
        lines = _PHYSICAL_LINE_BOUNDARY.split(self._raw)
        candidate_index = next(
            (index for index, line in enumerate(lines) if line.strip()),
            None,
        )
        if candidate_index is None:
            return SpeechPlanResult.plain(self._raw) if done else None
        candidate = lines[candidate_index]
        if not candidate.lstrip().startswith("{"):
            return SpeechPlanResult.plain(self._raw)
        has_line_boundary = candidate.endswith(("\n", "\r"))
        if not has_line_boundary:
            if len(candidate) > _MAX_SPEECH_PLAN_PREFIX_CHARS:
                message = "speech plan prefix limit exceeded"
                raise ValueError(message)
            if not done:
                return None
        result = parse_speech_plan(self._raw, max_chars=self._max_chars)
        if not done and not result.body.strip():
            return None
        return result


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
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, ValidationError):
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
