from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span

from moco.config import TelemetrySettings
from moco.runtime import telemetry as telemetry_module
from moco.runtime.telemetry import (
    TelemetryAttributeError,
    boundary_label,
    configure_telemetry,
    safe_event,
    sanitize_attributes,
)


class TelemetryShutdownError(RuntimeError):
    """Synthetic telemetry provider shutdown failure."""


class TelemetryConfigurationError(RuntimeError):
    """Synthetic telemetry provider configuration failure."""


def _batch_worker_is_alive(processor: BatchSpanProcessor) -> bool:
    return processor._batch_processor._worker_thread.is_alive()  # noqa: SLF001


def _shutdown_batch_processor(processor: BatchSpanProcessor) -> None:
    processor.shutdown()  # type: ignore[no-untyped-call]


def test_allows_only_bounded_operational_attributes() -> None:
    attributes = {
        "audio_id": 7,
        "context_state": "running",
        "event_code": "cancelled",
        "generation": 3,
        "phase": "completed",
        "queue_depth": 2,
        "state": "ready",
        "text_chars": 18,
        "duration_ms": 42,
        "trace_id": "0123456789abcdef",
        "component": "speech",
        "boundary": "irodori_http",
        "wav_bytes": 4096,
    }

    assert sanitize_attributes(attributes, strict=True) == attributes


def test_allows_only_bounded_irodori_capability_metadata() -> None:
    attributes = {
        "contract_version": 1,
        "ready": True,
        "readiness": "model_loading",
        "voice_count": 3,
    }

    assert sanitize_attributes(attributes, strict=True) == attributes
    for forbidden in ["voice_id", "voice_label", "aliases", "caption"]:
        with pytest.raises(TelemetryAttributeError, match=forbidden):
            sanitize_attributes({forbidden: "fixture-sensitive"}, strict=True)


def test_caption_telemetry_accepts_metadata_without_content() -> None:
    assert sanitize_attributes(
        {
            "caption_present": True,
            "plan_chars": 120,
            "caption_mode": "auto",
            "delivery_caption": "private caption",
            "body": "private transcript",
        },
        strict=False,
    ) == {
        "caption_present": True,
        "plan_chars": 120,
        "caption_mode": "auto",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("caption_present", 1),
        ("caption_present", "true"),
        ("plan_chars", True),
        ("plan_chars", -1),
        ("plan_chars", 1.0),
        ("caption_mode", "dynamic"),
    ],
)
def test_caption_telemetry_rejects_wrong_types_and_unknown_modes(
    key: str,
    value: object,
) -> None:
    with pytest.raises(TelemetryAttributeError, match=key):
        sanitize_attributes({key: value}, strict=True)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("generation", "remote-irodori-generation"),
        ("audio_id", True),
        ("text_chars", -1),
        ("wav_bytes", 1.5),
        ("queue_depth", "2"),
        ("phase", "再生中"),
        ("phase", "stopped"),
        ("context_state", "x" * 65),
    ],
)
def test_rejects_unbounded_or_wrongly_typed_audio_metadata(
    key: str,
    value: object,
) -> None:
    with pytest.raises(TelemetryAttributeError, match=key):
        sanitize_attributes({key: value}, strict=True)


@pytest.mark.parametrize("key", ["component", "state", "event_code", "duration_ms"])
def test_rejects_boolean_values_for_non_boolean_attributes(key: str) -> None:
    with pytest.raises(TelemetryAttributeError, match=key):
        sanitize_attributes({key: True}, strict=True)


@pytest.mark.parametrize("phase", ["started", "completed", "failed"])
def test_allows_only_known_playback_phases(phase: str) -> None:
    assert sanitize_attributes({"phase": phase}, strict=True) == {"phase": phase}


@pytest.mark.parametrize(
    "context_state",
    ["running", "suspended", "closed", "interrupted"],
)
def test_allows_only_known_audio_context_states(context_state: str) -> None:
    assert sanitize_attributes({"context_state": context_state}, strict=True) == {
        "context_state": context_state,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("phase", "https://example.com"),
        ("phase", "arbitrary_ascii_token"),
        ("phase", "secret-token-123"),
        ("context_state", "unknown"),
        ("context_state", "https://example.com"),
        ("context_state", "secret_context_token"),
    ],
)
def test_rejects_unknown_playback_enum_values(key: str, value: str) -> None:
    with pytest.raises(TelemetryAttributeError, match=key):
        sanitize_attributes({key: value}, strict=True)


def test_production_sanitizer_drops_unknown_playback_enum_values() -> None:
    assert sanitize_attributes(
        {
            "component": "web",
            "phase": "https://example.com",
            "context_state": "secret_context_token",
        },
        strict=False,
    ) == {"component": "web"}


@pytest.mark.parametrize(
    "segment_reason",
    ["sentence_end", "first_soft_break", "max_chars", "turn_flush"],
)
def test_allows_only_known_segment_reasons(segment_reason: str) -> None:
    assert sanitize_attributes({"segment_reason": segment_reason}, strict=True) == {
        "segment_reason": segment_reason,
    }


@pytest.mark.parametrize("segment_reason", ["unknown", "https://example.com/secret"])
def test_rejects_unknown_segment_reasons(segment_reason: str) -> None:
    with pytest.raises(TelemetryAttributeError, match="segment_reason"):
        sanitize_attributes({"segment_reason": segment_reason}, strict=True)
    assert sanitize_attributes(
        {"component": "speech", "segment_reason": segment_reason},
        strict=False,
    ) == {"component": "speech"}


def test_segment_index_accepts_only_positive_strict_integers() -> None:
    assert sanitize_attributes({"segment_index": 1}, strict=True) == {
        "segment_index": 1,
    }
    for invalid in [0, -1, True, 1.0, "1"]:
        with pytest.raises(TelemetryAttributeError, match="segment_index"):
            sanitize_attributes({"segment_index": invalid}, strict=True)
        assert sanitize_attributes({"segment_index": invalid}, strict=False) == {}


@pytest.mark.parametrize("readiness", ["loading", "capability_mismatch", "unavailable"])
def test_allows_bounded_web_capability_readiness(readiness: str) -> None:
    assert sanitize_attributes({"readiness": readiness}, strict=True) == {
        "readiness": readiness,
    }


@pytest.mark.parametrize(
    "attributes",
    [
        {"contract_version": True},
        {"ready": 1},
        {"readiness": "unknown"},
        {"voice_count": -1},
        {"voice_count": 1.5},
    ],
)
def test_rejects_invalid_capability_metadata_types_or_values(
    attributes: dict[str, object],
) -> None:
    with pytest.raises(TelemetryAttributeError):
        sanitize_attributes(attributes, strict=True)


@pytest.mark.parametrize(
    "key",
    [
        "transcript",
        "prompt",
        "audio_bytes",
        "token",
        "capability",
        "account",
        "email",
        "memory",
        "url",
    ],
)
def test_forbidden_content_keys_raise_in_strict_mode(key: str) -> None:
    with pytest.raises(TelemetryAttributeError, match=key):
        sanitize_attributes({key: "sensitive"}, strict=True)


def test_production_sanitizer_drops_forbidden_and_unknown_keys() -> None:
    assert sanitize_attributes(
        {
            "component": "web",
            "transcript": "do not retain",
            "arbitrary": "not part of contract",
        },
        strict=False,
    ) == {"component": "web"}


def test_irodori_url_is_reduced_to_boundary_label() -> None:
    assert boundary_label("irodori", "http://user:password@100.64.0.1:8923") == "irodori_http"


def test_safe_event_logs_only_sanitized_attributes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.telemetry")
    caplog.set_level(logging.INFO, logger=logger.name)

    safe_event(
        logger,
        "conversation_state",
        component="runtime",
        state="ready",
        transcript="must disappear",
    )

    assert "conversation_state" in caplog.text
    assert "component=runtime" in caplog.text
    assert "state=ready" in caplog.text
    assert "must disappear" not in caplog.text
    assert "transcript" not in caplog.text


def test_safe_event_includes_valid_span_trace_id_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.telemetry.trace")
    caplog.set_level(logging.INFO, logger=logger.name)
    trace_id = "1234567890abcdef1234567890abcdef"
    span = NonRecordingSpan(
        SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int("1234567890abcdef", 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
    )

    with use_span(span):
        safe_event(logger, "trace_correlated", component="runtime")

    assert f"trace_id={trace_id}" in caplog.text


def test_exporters_are_selected_only_from_settings() -> None:
    none = configure_telemetry(TelemetrySettings(console=False))
    console = configure_telemetry(TelemetrySettings(console=True))
    otlp = configure_telemetry(
        TelemetrySettings.model_validate(
            {
                "console": False,
                "otlp_endpoint": "http://127.0.0.1:4318/v1/traces",
            },
        ),
    )

    assert none.exporter_names == ()
    assert console.exporter_names == ("console",)
    assert otlp.exporter_names == ("otlp_http",)

    none.close()
    console.close()
    otlp.close()


def test_configuration_failure_shuts_down_unregistered_batch_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moco_logger = logging.getLogger("moco")
    original_handlers = tuple(moco_logger.handlers)
    original_level = moco_logger.level
    original_propagate = moco_logger.propagate
    processors: list[BatchSpanProcessor] = []

    real_batch_span_processor = BatchSpanProcessor

    def record_batch_span_processor(exporter: SpanExporter) -> BatchSpanProcessor:
        processor = real_batch_span_processor(exporter)
        processors.append(processor)
        return processor

    def fail_add_span_processor(*_args: object) -> None:
        raise TelemetryConfigurationError

    monkeypatch.setattr(telemetry_module, "BatchSpanProcessor", record_batch_span_processor)
    monkeypatch.setattr(
        "moco.runtime.telemetry.TracerProvider.add_span_processor",
        fail_add_span_processor,
    )

    try:
        with pytest.raises(TelemetryConfigurationError):
            configure_telemetry(TelemetrySettings(console=True))

        assert len(processors) == 1
        assert not _batch_worker_is_alive(processors[0])
        assert tuple(moco_logger.handlers) == original_handlers
        assert moco_logger.level == original_level
        assert moco_logger.propagate is original_propagate
    finally:
        for processor in processors:
            _shutdown_batch_processor(processor)


def test_configuration_failure_shuts_down_provider_without_masking_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration_error = TelemetryConfigurationError("fixture configuration failure")
    processors: list[BatchSpanProcessor] = []
    real_batch_span_processor = BatchSpanProcessor
    real_provider_shutdown = TracerProvider.shutdown

    def record_batch_span_processor(exporter: SpanExporter) -> BatchSpanProcessor:
        processor = real_batch_span_processor(exporter)
        processors.append(processor)
        return processor

    def fail_otlp_exporter(**_kwargs: object) -> SpanExporter:
        raise configuration_error

    def fail_provider_shutdown(provider: TracerProvider) -> None:
        real_provider_shutdown(provider)
        raise TelemetryShutdownError

    monkeypatch.setattr(telemetry_module, "BatchSpanProcessor", record_batch_span_processor)
    monkeypatch.setattr(telemetry_module, "OTLPSpanExporter", fail_otlp_exporter)
    monkeypatch.setattr(TracerProvider, "shutdown", fail_provider_shutdown)

    settings = TelemetrySettings.model_validate(
        {
            "console": True,
            "otlp_endpoint": "http://127.0.0.1:4318/v1/traces",
        },
    )
    try:
        with pytest.raises(TelemetryConfigurationError) as caught:
            configure_telemetry(settings)

        assert caught.value is configuration_error
        assert len(processors) == 1
        assert not _batch_worker_is_alive(processors[0])
    finally:
        for processor in processors:
            _shutdown_batch_processor(processor)


def test_console_telemetry_emits_moco_events_to_stderr_and_restores_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    moco_logger = logging.getLogger("moco")
    original_level = moco_logger.level
    original_propagate = moco_logger.propagate
    original_handlers = tuple(moco_logger.handlers)
    runtime = configure_telemetry(TelemetrySettings(console=True))

    safe_event(
        logging.getLogger("moco.audio.test"),
        "bounded_console_event",
        component="speech",
        transcript="must not appear",
    )

    captured = capsys.readouterr()
    assert "event=bounded_console_event component=speech" in captured.err
    assert "must not appear" not in captured.err
    assert "transcript" not in captured.err

    runtime.close()
    runtime.close()
    assert moco_logger.level == original_level
    assert moco_logger.propagate is original_propagate
    assert tuple(moco_logger.handlers) == original_handlers


def test_console_disabled_adds_no_moco_logger_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    moco_logger = logging.getLogger("moco")
    original_level = moco_logger.level
    original_propagate = moco_logger.propagate
    original_handlers = tuple(moco_logger.handlers)
    runtime = configure_telemetry(TelemetrySettings(console=False))

    safe_event(logging.getLogger("moco.audio.disabled"), "disabled_console_event")

    assert capsys.readouterr().err == ""
    runtime.close()
    assert moco_logger.level == original_level
    assert moco_logger.propagate is original_propagate
    assert tuple(moco_logger.handlers) == original_handlers


def test_repeated_console_configuration_uses_one_restorable_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = configure_telemetry(TelemetrySettings(console=True))
    second = configure_telemetry(TelemetrySettings(console=True))

    safe_event(logging.getLogger("moco.audio.deduplicated"), "one_console_line")
    assert capsys.readouterr().err.count("event=one_console_line") == 1

    first.close()
    safe_event(logging.getLogger("moco.audio.deduplicated"), "still_configured")
    assert capsys.readouterr().err.count("event=still_configured") == 1
    second.close()


def test_concurrent_duplicate_close_releases_only_its_console_owner_once(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = configure_telemetry(TelemetrySettings(console=True))
    second = configure_telemetry(TelemetrySettings(console=True))
    try:
        assert first._close_lock is not second._close_lock  # noqa: SLF001
        original_shutdown = first.provider.shutdown
        shutdown_calls = 0
        shutdown_calls_lock = threading.Lock()

        def counted_shutdown() -> None:
            nonlocal shutdown_calls
            with shutdown_calls_lock:
                shutdown_calls += 1
            time.sleep(0.02)
            original_shutdown()

        monkeypatch.setattr(first.provider, "shutdown", counted_shutdown)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(first.close) for _ in range(2)]
            for future in futures:
                future.result(timeout=1)

        assert shutdown_calls == 1
        safe_event(logging.getLogger("moco.audio.concurrent"), "second_owner_active")
        assert capsys.readouterr().err.count("event=second_owner_active") == 1
    finally:
        first.close()
        second.close()


def test_shutdown_failure_still_restores_console_handler(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moco_logger = logging.getLogger("moco")
    original_handlers = tuple(moco_logger.handlers)
    original_level = moco_logger.level
    original_propagate = moco_logger.propagate
    runtime = configure_telemetry(TelemetrySettings(console=True))
    original_shutdown = runtime.provider.shutdown

    def failing_shutdown() -> None:
        original_shutdown()
        raise TelemetryShutdownError

    monkeypatch.setattr(runtime.provider, "shutdown", failing_shutdown)
    with pytest.raises(TelemetryShutdownError):
        runtime.close()

    assert tuple(moco_logger.handlers) == original_handlers
    assert moco_logger.level == original_level
    assert moco_logger.propagate is original_propagate
    safe_event(logging.getLogger("moco.audio.closed"), "handler_is_gone")
    assert "handler_is_gone" not in capsys.readouterr().err
    runtime.close()


def test_console_formatter_bounds_each_line_to_1024_characters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = configure_telemetry(TelemetrySettings(console=True))
    try:
        logging.getLogger("moco.audio.bound").info("x" * 2048)
        rendered = capsys.readouterr().err.rstrip("\n")
        assert len(rendered) == 1024
    finally:
        runtime.close()
