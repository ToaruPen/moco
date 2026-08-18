from __future__ import annotations

import asyncio
import io
import json
import math
import time
import wave
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Never

import pytest
from irodori_tts_infra.contracts import CapabilitiesResponse

from moco.config import MocoSettings, load_config
from moco.speech.contracts import IrodoriCapabilities
from moco.speech.irodori import IrodoriError, IrodoriSynthesizer
from moco.speech.text import TranscriptSegmenter

type Condition = Literal["baseline", "candidate"]

_CANDIDATE_MIN_CHARS = 18
_LIVE_PROBE_TIMEOUT_SECONDS = 300.0
_LIVE_PROBE_CLEANUP_TIMEOUT_SECONDS = 10.0
_SAMPLES = (
    "短い確認文です。",
    "音声の開始を早める効果を安全に確かめる説明として、後半の内容も続けます。",
    "「一定の手順で落ち着いて確認するための引用文です」",
    "この応答は自然な調子で聞こえますか？",
    "🤔処理の途中でも音声境界を安全に確認します。",
    (
        "音声合成の最大文字数境界を確認するため句読点を置かない説明を一定の長さまで"
        "続けて分割位置が決定的であることと後続部分も欠けずに処理されることを落ち"
        "着いて順番に確かめて最後まで読み上げます"
    ),
)
_WARMUP_TEXT = "合成準備を確認します。"


class _ProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _SegmentedSample:
    first_ready_chars: int
    segments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ConditionMeasurement:
    first_ready_chars: int
    first_synthesis_ms: float
    total_synthesis_ms: float
    estimated_turn_completion_ms: float
    playback_gap_ms: float
    segment_count: int


def _fail(code: str) -> Never:
    raise _ProbeError(code)


async def _with_timeout[Result](
    awaitable: Awaitable[Result],
    *,
    timeout_seconds: float,
    failure_code: str,
) -> Result:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await awaitable
    except TimeoutError as error:
        raise _ProbeError(failure_code) from error


async def _check_timeout_mapping(failure_code: str) -> None:
    never_ready = asyncio.Event()
    try:
        await _with_timeout(
            never_ready.wait(),
            timeout_seconds=0.0,
            failure_code=failure_code,
        )
    except _ProbeError as error:
        if str(error) != failure_code:
            _fail("timeout_helper_failed")
    else:
        _fail("timeout_helper_failed")


def _segment_sample(
    text: str,
    *,
    max_chars: int,
    minimum: int | None,
) -> _SegmentedSample:
    segmenter = TranscriptSegmenter(
        max_chars=max_chars,
        first_segment_soft_break_min_chars=minimum,
    )
    first_ready_chars: int | None = None
    segments: list[str] = []
    for received_chars, code_point in enumerate(text, start=1):
        ready = segmenter.push(code_point)
        if ready and first_ready_chars is None:
            first_ready_chars = received_chars
        segments.extend(segment.text for segment in ready)
    flushed = segmenter.flush()
    if flushed and first_ready_chars is None:
        first_ready_chars = len(text)
    segments.extend(segment.text for segment in flushed)
    if first_ready_chars is None or not segments:
        _fail("segment_probe_failed")
    return _SegmentedSample(first_ready_chars, tuple(segments))


def _wav_duration_ms(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except (EOFError, wave.Error) as error:
        message = "invalid_audio"
        raise _ProbeError(message) from error
    if frame_rate <= 0 or frame_count <= 0:
        _fail("invalid_audio")
    return frame_count / frame_rate * 1000.0


async def _measure_condition(
    synthesizer: IrodoriSynthesizer,
    segmented: _SegmentedSample,
) -> _ConditionMeasurement:
    synthesis_ms: list[float] = []
    audio_ms: list[float] = []
    for segment in segmented.segments:
        started_ns = time.perf_counter_ns()
        wav_bytes = await synthesizer.synthesize(segment)
        synthesis_ms.append((time.perf_counter_ns() - started_ns) / 1_000_000)
        audio_ms.append(_wav_duration_ms(wav_bytes))

    synthesis_completion_ms = 0.0
    playback_end_ms = 0.0
    playback_gaps_ms: list[float] = []
    for index, (segment_synthesis_ms, segment_audio_ms) in enumerate(
        zip(synthesis_ms, audio_ms, strict=True),
    ):
        synthesis_completion_ms += segment_synthesis_ms
        playback_start_ms = max(synthesis_completion_ms, playback_end_ms)
        if index > 0:
            playback_gaps_ms.append(max(0.0, playback_start_ms - playback_end_ms))
        playback_end_ms = playback_start_ms + segment_audio_ms

    return _ConditionMeasurement(
        first_ready_chars=segmented.first_ready_chars,
        first_synthesis_ms=synthesis_ms[0],
        total_synthesis_ms=sum(synthesis_ms),
        estimated_turn_completion_ms=playback_end_ms,
        playback_gap_ms=max(playback_gaps_ms, default=0.0),
        segment_count=len(segmented.segments),
    )


def _nearest_rank(values: list[float], percentile: int) -> float:
    if not values or not 1 <= percentile <= 100:
        _fail("percentile_probe_failed")
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def _percent_change(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        _fail("metric_probe_failed")
    return (candidate - baseline) / baseline * 100.0


def _rounded(value: float) -> float:
    return round(value, 3)


def _empty_summary() -> dict[str, int | float | bool | None]:
    return {
        "samples": 0,
        "capabilities_ms": None,
        "warmup_synthesis_ms": None,
        "baseline_first_ready_chars_p95": None,
        "candidate_first_ready_chars_p95": None,
        "baseline_first_synthesis_ms_p95": None,
        "candidate_first_synthesis_ms_p95": None,
        "estimated_first_audio_improvement_percent": None,
        "baseline_total_synthesis_ms_p95": None,
        "candidate_total_synthesis_ms_p95": None,
        "total_synthesis_overhead_percent": None,
        "baseline_estimated_turn_completion_ms_p95": None,
        "candidate_estimated_turn_completion_ms_p95": None,
        "turn_completion_overhead_percent": None,
        "segment_count_ratio": None,
        "baseline_playback_gap_ms_p95": None,
        "candidate_playback_gap_ms_p95": None,
        "failures": 1,
        "runtime_ready": False,
    }


def _select_voice(
    synthesizer: IrodoriSynthesizer,
    capabilities: CapabilitiesResponse | IrodoriCapabilities,
    configured_selector: str | None,
) -> None:
    selector = configured_selector
    if selector is None:
        default_selector: str | None = None
        for voice in capabilities.voices:
            if not voice.default:
                continue
            if default_selector is not None:
                _fail("default_voice_ambiguous")
            default_selector = voice.id
        if default_selector is None:
            _fail("default_voice_missing")
        selector = default_selector
    synthesizer.select_voice(selector)


async def _prepare_runtime(
    synthesizer: IrodoriSynthesizer,
    settings: MocoSettings,
) -> tuple[float, float]:
    capabilities_started_ns = time.perf_counter_ns()
    capabilities = await synthesizer.capabilities()
    capabilities_ms = (time.perf_counter_ns() - capabilities_started_ns) / 1_000_000
    if not capabilities.ready or capabilities.readiness != "ready":
        _fail("runtime_not_ready")
    _select_voice(synthesizer, capabilities, settings.irodori.speaker)

    warmup_started_ns = time.perf_counter_ns()
    warmup_wav = await synthesizer.synthesize(_WARMUP_TEXT)
    warmup_synthesis_ms = (time.perf_counter_ns() - warmup_started_ns) / 1_000_000
    _wav_duration_ms(warmup_wav)
    return capabilities_ms, warmup_synthesis_ms


async def _measure_samples(
    synthesizer: IrodoriSynthesizer,
    *,
    max_chars: int,
) -> dict[Condition, list[_ConditionMeasurement]]:
    results: dict[Condition, list[_ConditionMeasurement]] = {
        "baseline": [],
        "candidate": [],
    }
    for sample_index, sample in enumerate(_SAMPLES):
        segmented = {
            "baseline": _segment_sample(sample, max_chars=max_chars, minimum=None),
            "candidate": _segment_sample(
                sample,
                max_chars=max_chars,
                minimum=_CANDIDATE_MIN_CHARS,
            ),
        }
        order: tuple[Condition, Condition]
        order = ("baseline", "candidate") if sample_index % 2 == 0 else ("candidate", "baseline")
        for condition in order:
            results[condition].append(
                await _measure_condition(synthesizer, segmented[condition]),
            )
    return results


def _validate_results(
    baseline: list[_ConditionMeasurement],
    candidate: list[_ConditionMeasurement],
) -> None:
    if len(baseline) != len(_SAMPLES) or len(candidate) != len(_SAMPLES):
        _fail("measurement_count_mismatch")
    if not any(
        candidate_item.first_ready_chars < baseline_item.first_ready_chars
        for baseline_item, candidate_item in zip(baseline, candidate, strict=True)
    ):
        _fail("candidate_boundary_not_exercised")


def _build_summary(
    results: dict[Condition, list[_ConditionMeasurement]],
    *,
    capabilities_ms: float,
    warmup_synthesis_ms: float,
) -> dict[str, int | float | bool | None]:
    baseline = results["baseline"]
    candidate = results["candidate"]
    _validate_results(baseline, candidate)
    baseline_first_synthesis_p95 = _nearest_rank(
        [item.first_synthesis_ms for item in baseline],
        95,
    )
    candidate_first_synthesis_p95 = _nearest_rank(
        [item.first_synthesis_ms for item in candidate],
        95,
    )
    baseline_total_synthesis_p95 = _nearest_rank(
        [item.total_synthesis_ms for item in baseline],
        95,
    )
    candidate_total_synthesis_p95 = _nearest_rank(
        [item.total_synthesis_ms for item in candidate],
        95,
    )
    baseline_turn_completion_p95 = _nearest_rank(
        [item.estimated_turn_completion_ms for item in baseline],
        95,
    )
    candidate_turn_completion_p95 = _nearest_rank(
        [item.estimated_turn_completion_ms for item in candidate],
        95,
    )
    baseline_segments = sum(item.segment_count for item in baseline)
    candidate_segments = sum(item.segment_count for item in candidate)
    return {
        "samples": len(_SAMPLES),
        "capabilities_ms": _rounded(capabilities_ms),
        "warmup_synthesis_ms": _rounded(warmup_synthesis_ms),
        "baseline_first_ready_chars_p95": _rounded(
            _nearest_rank([float(item.first_ready_chars) for item in baseline], 95),
        ),
        "candidate_first_ready_chars_p95": _rounded(
            _nearest_rank([float(item.first_ready_chars) for item in candidate], 95),
        ),
        "baseline_first_synthesis_ms_p95": _rounded(baseline_first_synthesis_p95),
        "candidate_first_synthesis_ms_p95": _rounded(candidate_first_synthesis_p95),
        # Model-only estimate: transcript arrival and browser playback startup are excluded.
        "estimated_first_audio_improvement_percent": _rounded(
            -_percent_change(
                baseline_first_synthesis_p95,
                candidate_first_synthesis_p95,
            ),
        ),
        "baseline_total_synthesis_ms_p95": _rounded(baseline_total_synthesis_p95),
        "candidate_total_synthesis_ms_p95": _rounded(candidate_total_synthesis_p95),
        "total_synthesis_overhead_percent": _rounded(
            _percent_change(
                baseline_total_synthesis_p95,
                candidate_total_synthesis_p95,
            ),
        ),
        "baseline_estimated_turn_completion_ms_p95": _rounded(
            baseline_turn_completion_p95,
        ),
        "candidate_estimated_turn_completion_ms_p95": _rounded(
            candidate_turn_completion_p95,
        ),
        "turn_completion_overhead_percent": _rounded(
            _percent_change(
                baseline_turn_completion_p95,
                candidate_turn_completion_p95,
            ),
        ),
        "segment_count_ratio": _rounded(candidate_segments / baseline_segments),
        "baseline_playback_gap_ms_p95": _rounded(
            _nearest_rank([item.playback_gap_ms for item in baseline], 95),
        ),
        "candidate_playback_gap_ms_p95": _rounded(
            _nearest_rank([item.playback_gap_ms for item in candidate], 95),
        ),
        "failures": 0,
        "runtime_ready": True,
    }


async def _run_probe(
    synthesizer: IrodoriSynthesizer,
    settings: MocoSettings,
) -> dict[str, int | float | bool | None]:
    if settings.irodori.caption_mode != "off":
        _fail("caption_mode_not_off")
    capabilities_ms, warmup_synthesis_ms = await _prepare_runtime(synthesizer, settings)
    results = await _measure_samples(
        synthesizer,
        max_chars=settings.speech.segment_max_chars,
    )
    return _build_summary(
        results,
        capabilities_ms=capabilities_ms,
        warmup_synthesis_ms=warmup_synthesis_ms,
    )


@pytest.mark.live
@pytest.mark.slow
async def test_adaptive_first_segment_live_latency() -> None:
    # Keep deterministic helper checks inside the one explicitly-live probe.
    assert _nearest_rank([4.0, 1.0, 3.0, 2.0], 95) == 4.0
    assert _percent_change(100.0, 110.0) == 10.0
    await _check_timeout_mapping("live_probe_timeout")
    await _check_timeout_mapping("live_probe_cleanup_failed")

    summary = _empty_summary()
    failure_code: str | None = None
    synthesizer: IrodoriSynthesizer | None = None
    try:
        settings = load_config()
        synthesizer = IrodoriSynthesizer.from_settings(settings)
        summary = await _with_timeout(
            _run_probe(synthesizer, settings),
            timeout_seconds=_LIVE_PROBE_TIMEOUT_SECONDS,
            failure_code="live_probe_timeout",
        )
    except IrodoriError as error:
        failure_code = error.code
    except _ProbeError as error:
        failure_code = str(error)
    except Exception:  # noqa: BLE001 - boundary emits only a stable failure code
        failure_code = "live_probe_failed"
    finally:
        if synthesizer is not None:
            try:
                await _with_timeout(
                    synthesizer.close(),
                    timeout_seconds=_LIVE_PROBE_CLEANUP_TIMEOUT_SECONDS,
                    failure_code="live_probe_cleanup_failed",
                )
            except Exception:  # noqa: BLE001 - cleanup failure is intentionally bounded
                summary["failures"] = 1
                failure_code = "live_probe_cleanup_failed"

    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))  # noqa: T201
    if failure_code is not None:
        pytest.fail(failure_code, pytrace=False)
