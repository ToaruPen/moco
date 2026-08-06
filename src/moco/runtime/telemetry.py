from __future__ import annotations

import logging
import re
import sys
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opentelemetry.trace import Span, Tracer

    from moco.config import TelemetrySettings

type Scalar = str | bool | int | float

_ALLOWED_ATTRIBUTES = frozenset(
    {
        "boundary",
        "audio_id",
        "component",
        "context_state",
        "contract_version",
        "control",
        "duration_ms",
        "event_code",
        "generation",
        "phase",
        "queue_depth",
        "ready",
        "readiness",
        "result",
        "segment_index",
        "segment_reason",
        "state",
        "text_chars",
        "trace_id",
        "voice_count",
        "wav_bytes",
    },
)
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_AUDIO_NUMERIC_ATTRIBUTES = frozenset(
    {"audio_id", "generation", "queue_depth", "text_chars", "wav_bytes"},
)
_SAFE_PLAYBACK_PHASES = frozenset({"started", "completed", "failed"})
_SAFE_AUDIO_CONTEXT_STATES = frozenset(
    {"running", "suspended", "closed", "interrupted"},
)
_SAFE_SEGMENT_REASONS = frozenset(
    {"sentence_end", "first_soft_break", "max_chars", "turn_flush"},
)
_MAX_CONSOLE_LINE_CHARS = 1024
_SAFE_READINESS = frozenset(
    {
        "ready",
        "loading",
        "model_loading",
        "model_not_loaded",
        "voice_bank_invalid",
        "capability_mismatch",
        "unavailable",
    },
)


class TelemetryAttributeError(ValueError):
    """A content-bearing or unbounded telemetry attribute was rejected."""


class _BoundedConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return super().format(record)[:_MAX_CONSOLE_LINE_CHARS]


_console_lock = threading.Lock()
_console_handler: logging.Handler | None = None
_console_owners = 0
_console_original_level = logging.NOTSET
_console_original_propagate = True


@dataclass(slots=True)
class TelemetryRuntime:
    provider: TracerProvider
    tracer: Tracer
    exporter_names: tuple[str, ...]
    console_logging: bool = False
    _closed: bool = False
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    @contextmanager
    def span(self, name: str, **attributes: Scalar) -> Iterator[Span]:
        safe = sanitize_attributes(attributes, strict=False)
        with self.tracer.start_as_current_span(name, attributes=safe) as current:
            yield current

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.provider.shutdown()
            finally:
                if self.console_logging:
                    _release_console_logging()


def sanitize_attributes(
    attributes: Mapping[str, object],
    *,
    strict: bool,
) -> dict[str, Scalar]:
    safe: dict[str, Scalar] = {}
    for key, value in attributes.items():
        if key not in _ALLOWED_ATTRIBUTES or not _is_safe_value(key, value):
            if strict:
                message = f"unsafe telemetry attribute: {key}"
                raise TelemetryAttributeError(message)
            continue
        safe[key] = cast("Scalar", value)
    return safe


def boundary_label(component: str, address: str) -> str:
    del address
    if component == "irodori":
        return "irodori_http"
    if component == "codex":
        return "codex_stdio"
    return "external_boundary"


def safe_event(
    logger: logging.Logger,
    event: str,
    *,
    # Generic metadata splats may contain this key; only literal False disables tracing.
    include_trace_id: object = True,
    **attributes: object,
) -> None:
    if not _SAFE_TEXT.fullmatch(event):
        return
    current_context = trace.get_current_span().get_span_context()
    enriched = dict(attributes)
    if include_trace_id is not False and current_context.is_valid and "trace_id" not in enriched:
        enriched["trace_id"] = f"{current_context.trace_id:032x}"
    safe = sanitize_attributes(enriched, strict=False)
    details = " ".join(f"{key}={safe[key]}" for key in sorted(safe))
    logger.info("event=%s %s", event, details)


def _add_batch_span_processor(
    provider: TracerProvider,
    exporter: SpanExporter,
) -> None:
    processor = BatchSpanProcessor(exporter)
    try:
        provider.add_span_processor(processor)
    except BaseException:
        with suppress(BaseException):
            processor.shutdown()  # type: ignore[no-untyped-call]
        raise


def configure_telemetry(settings: TelemetrySettings) -> TelemetryRuntime:
    resource = Resource.create({"service.name": settings.service_name})
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    exporter_names: list[str] = []
    console_acquired = False
    try:
        if settings.console:
            _acquire_console_logging()
            console_acquired = True
            _add_batch_span_processor(provider, ConsoleSpanExporter())
            exporter_names.append("console")
        if settings.otlp_endpoint is not None:
            _add_batch_span_processor(
                provider,
                OTLPSpanExporter(endpoint=str(settings.otlp_endpoint)),
            )
            exporter_names.append("otlp_http")
        return TelemetryRuntime(
            provider=provider,
            tracer=provider.get_tracer("moco"),
            exporter_names=tuple(exporter_names),
            console_logging=settings.console,
        )
    except BaseException:
        with suppress(BaseException):
            provider.shutdown()
        if console_acquired:
            with suppress(BaseException):
                _release_console_logging()
        raise


def _is_safe_value(key: str, value: object) -> bool:
    if key in {"contract_version", "ready", "readiness", "voice_count"}:
        return _is_safe_capability_value(key, value)
    if key == "segment_index":
        return type(value) is int and value > 0
    if key == "segment_reason":
        return isinstance(value, str) and value in _SAFE_SEGMENT_REASONS
    if key == "phase":
        return isinstance(value, str) and value in _SAFE_PLAYBACK_PHASES
    if key == "context_state":
        return isinstance(value, str) and value in _SAFE_AUDIO_CONTEXT_STATES
    if isinstance(value, bool):
        valid = False
    elif isinstance(value, int | float):
        valid = value >= 0 and (key not in _AUDIO_NUMERIC_ATTRIBUTES or type(value) is int)
    elif (
        not isinstance(value, str)
        or not _SAFE_TEXT.fullmatch(value)
        or key in _AUDIO_NUMERIC_ATTRIBUTES
    ):
        valid = False
    elif key == "trace_id":
        valid = len(value) in {16, 32} and all(
            character in "0123456789abcdef" for character in value
        )
    else:
        valid = True
    return valid


def _is_safe_capability_value(key: str, value: object) -> bool:
    if key in {"contract_version", "voice_count"}:
        return type(value) is int and value >= 0
    if key == "ready":
        return type(value) is bool
    return key == "readiness" and isinstance(value, str) and value in _SAFE_READINESS


def _acquire_console_logging() -> None:
    global _console_handler  # noqa: PLW0603
    global _console_original_level  # noqa: PLW0603
    global _console_original_propagate  # noqa: PLW0603
    global _console_owners  # noqa: PLW0603

    with _console_lock:
        moco_logger = logging.getLogger("moco")
        if _console_handler is None:
            _console_original_level = moco_logger.level
            _console_original_propagate = moco_logger.propagate
            handler = logging.StreamHandler(sys.stderr)
            handler.setLevel(logging.INFO)
            handler.setFormatter(
                _BoundedConsoleFormatter("%(levelname)s %(name)s %(message)s"),
            )
            moco_logger.addHandler(handler)
            moco_logger.setLevel(logging.INFO)
            moco_logger.propagate = False
            _console_handler = handler
        _console_owners += 1


def _release_console_logging() -> None:
    global _console_handler  # noqa: PLW0603
    global _console_owners  # noqa: PLW0603

    with _console_lock:
        if _console_owners == 0:
            return
        _console_owners -= 1
        if _console_owners != 0:
            return
        moco_logger = logging.getLogger("moco")
        handler = _console_handler
        if handler is not None:
            moco_logger.removeHandler(handler)
            handler.close()
        moco_logger.setLevel(_console_original_level)
        moco_logger.propagate = _console_original_propagate
        _console_handler = None
