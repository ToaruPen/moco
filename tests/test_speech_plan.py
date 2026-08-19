from __future__ import annotations

import json

import pytest

from moco.speech.plan import (
    SpeechPlanResult,
    SpeechPlanStream,
    SpeechPlanUpdate,
    parse_speech_plan,
)


def test_parses_plan_and_removes_control_line() -> None:
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":" calm "}'

    result = parse_speech_plan(f"{control_line}\n本文です。", max_chars=300)

    assert result == SpeechPlanResult(
        body="本文です。",
        delivery_caption="calm",
        error_code=None,
        plan_chars=len(control_line),
        plan_present=True,
    )


def test_accepts_explicit_null_caption() -> None:
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":null}'

    result = parse_speech_plan(f"{control_line}\n本文です。", max_chars=300)

    assert result.delivery_caption is None
    assert result.error_code is None
    assert result.plan_present is True


def test_plain_body_is_preserved_unchanged() -> None:
    body = "\n  本文です。\n次の行です。"

    assert parse_speech_plan(body, max_chars=300) == SpeechPlanResult.plain(body)


@pytest.mark.parametrize(
    "control_line",
    [
        "{not-json}",
        (
            '{"type":"moco.speech_plan","version":1,'
            '"delivery_caption":"calm","delivery_caption":"bright"}'
        ),
        ('{"type":"moco.speech_plan","version":1,"delivery_caption":"calm","unknown":true}'),
        ('{"type":"moco.speech_plan","version":2,"delivery_caption":"calm"}'),
        ('{"type":"moco.speech_plan","version":true,"delivery_caption":"calm"}'),
        ('{"type":"moco.speech_plan","version":1.0,"delivery_caption":"calm"}'),
        ('{"type":"wrong","version":1,"delivery_caption":"calm"}'),
        '{"type":"moco.speech_plan","version":1}',
        ('{"type":"moco.speech_plan","version":1,"delivery_caption":3}'),
    ],
    ids=[
        "malformed-json",
        "duplicate-key",
        "unknown-field",
        "wrong-version",
        "boolean-version",
        "float-version",
        "wrong-type-name",
        "missing-caption",
        "wrong-caption-type",
    ],
)
def test_invalid_plan_drops_only_control_line(control_line: str) -> None:
    result = parse_speech_plan(f"{control_line}\n本文です。", max_chars=300)

    assert result == SpeechPlanResult(
        body="本文です。",
        delivery_caption=None,
        error_code="speech_caption_invalid",
        plan_chars=len(control_line),
        plan_present=True,
    )


def test_deeply_nested_plan_is_reported_as_invalid() -> None:
    control_line = (
        '{"type":"moco.speech_plan","version":1,"delivery_caption":'
        + "[" * 10_000
        + "null"
        + "]" * 10_000
        + "}"
    )

    result = parse_speech_plan(f"{control_line}\n本文です。", max_chars=300)

    assert result == SpeechPlanResult(
        body="本文です。",
        delivery_caption=None,
        error_code="speech_caption_invalid",
        plan_chars=len(control_line),
        plan_present=True,
    )


@pytest.mark.parametrize(
    "caption",
    [
        "abcd",
        "   ",
        "a\u0000b",
        "a\u007fb",
        "a\u2028b",
        "a\u2029b",
        "<a>",
    ],
    ids=[
        "over-limit",
        "blank",
        "nul",
        "delete",
        "line-separator",
        "paragraph-separator",
        "angle-brackets",
    ],
)
def test_rejects_invalid_caption_content(caption: str) -> None:
    control_line = (
        f'{{"type":"moco.speech_plan","version":1,"delivery_caption":{json.dumps(caption)}}}'
    )

    result = parse_speech_plan(f"{control_line}\n本文です。", max_chars=3)

    assert result.delivery_caption is None
    assert result.error_code == "speech_caption_invalid"
    assert result.body == "本文です。"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_literal_unicode_separator_does_not_leak_control_line_fragment(
    separator: str,
) -> None:
    control_line = f'{{"type":"moco.speech_plan","version":1,"delivery_caption":"a{separator}b"}}'

    result = parse_speech_plan(f"{control_line}\n本文です。", max_chars=300)

    assert result == SpeechPlanResult(
        body="本文です。",
        delivery_caption=None,
        error_code="speech_caption_invalid",
        plan_chars=len(control_line),
        plan_present=True,
    )


def test_valid_plan_requires_nonblank_body() -> None:
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":"calm"}'

    result = parse_speech_plan(f"{control_line}\n \n", max_chars=300)

    assert result.body == " \n"
    assert result.delivery_caption is None
    assert result.error_code == "speech_caption_invalid"
    assert result.plan_present is True


def test_leading_blank_lines_do_not_hide_plan_candidate() -> None:
    control_line = '  {"type":"moco.speech_plan","version":1,"delivery_caption":"calm"}'

    result = parse_speech_plan(f"\n{control_line}\n本文です。", max_chars=300)

    assert result.body == "\n本文です。"
    assert result.delivery_caption == "calm"
    assert result.plan_chars == len(control_line)


def test_stream_holds_control_line_and_emits_only_speakable_body_deltas() -> None:
    control_line = '{"type":"moco.speech_plan","version":1,"delivery_caption":"calm"}'
    stream = SpeechPlanStream(max_chars=300)

    assert stream.push(control_line[:24]) is None
    assert stream.push(f"{control_line[24:]}\n本") == SpeechPlanUpdate(
        text="本",
        delta="本",
        done=False,
        delivery_caption="calm",
        plan=SpeechPlanResult(
            body="本",
            delivery_caption="calm",
            error_code=None,
            plan_chars=len(control_line),
            plan_present=True,
        ),
    )
    assert stream.push("文です。") == SpeechPlanUpdate(
        text="本文です。",
        delta="文です。",
        done=False,
        delivery_caption=None,
        plan=None,
    )
    assert stream.push(f"{control_line}\n本文です。", done=True) == SpeechPlanUpdate(
        text="本文です。",
        delta="",
        done=True,
        delivery_caption=None,
        plan=None,
    )


def test_stream_plain_text_is_available_without_waiting_for_done() -> None:
    stream = SpeechPlanStream(max_chars=300)

    assert stream.push("確認") == SpeechPlanUpdate(
        text="確認",
        delta="確認",
        done=False,
        delivery_caption=None,
        plan=None,
    )
    assert stream.push("します。") == SpeechPlanUpdate(
        text="確認します。",
        delta="します。",
        done=False,
        delivery_caption=None,
        plan=None,
    )
    assert stream.push("確認します。", done=True) == SpeechPlanUpdate(
        text="確認します。",
        delta="",
        done=True,
        delivery_caption=None,
        plan=None,
    )


def test_stream_invalid_plan_reports_once_and_keeps_body() -> None:
    stream = SpeechPlanStream(max_chars=300)

    update = stream.push("{not-json}\n本文")

    assert update is not None
    assert update.text == "本文"
    assert update.delta == "本文"
    assert update.plan is not None
    assert update.plan.error_code == "speech_caption_invalid"
    assert stream.push("です。") == SpeechPlanUpdate(
        text="本文です。",
        delta="です。",
        done=False,
        delivery_caption=None,
        plan=None,
    )
