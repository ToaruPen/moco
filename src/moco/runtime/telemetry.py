from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator, Mapping

    from opentelemetry.trace import Span, Tracer

    from moco.config import TelemetrySettings

type Scalar = str | bool | int | float

_ALLOWED_ATTRIBUTES = frozenset(
    {
        "boundary",
        "component",
        "contract_version",
        "control",
        "duration_ms",
        "event_code",
        "ready",
        "readiness",
        "result",
        "state",
        "trace_id",
        "voice_count",
    },
)
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
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


@dataclass(slots=True)
class TelemetryRuntime:
    provider: TracerProvider
    tracer: Tracer
    exporter_names: tuple[str, ...]

    @contextmanager
    def span(self, name: str, **attributes: Scalar) -> Iterator[Span]:
        safe = sanitize_attributes(attributes, strict=False)
        with self.tracer.start_as_current_span(name, attributes=safe) as current:
            yield current

    def close(self) -> None:
        self.provider.shutdown()


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
    **attributes: object,
) -> None:
    if not _SAFE_TEXT.fullmatch(event):
        return
    current_context = trace.get_current_span().get_span_context()
    enriched = dict(attributes)
    if current_context.is_valid and "trace_id" not in enriched:
        enriched["trace_id"] = f"{current_context.trace_id:032x}"
    safe = sanitize_attributes(enriched, strict=False)
    details = " ".join(f"{key}={safe[key]}" for key in sorted(safe))
    logger.info("event=%s %s", event, details)


def configure_telemetry(settings: TelemetrySettings) -> TelemetryRuntime:
    resource = Resource.create({"service.name": settings.service_name})
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    exporter_names: list[str] = []
    if settings.console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        exporter_names.append("console")
    if settings.otlp_endpoint is not None:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=str(settings.otlp_endpoint)),
            ),
        )
        exporter_names.append("otlp_http")
    return TelemetryRuntime(
        provider=provider,
        tracer=provider.get_tracer("moco"),
        exporter_names=tuple(exporter_names),
    )


def _is_safe_value(key: str, value: object) -> bool:
    if key in {"contract_version", "ready", "readiness", "voice_count"}:
        return _is_safe_capability_value(key, value)
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return value >= 0
    if not isinstance(value, str) or not _SAFE_TEXT.fullmatch(value):
        return False
    if key == "trace_id":
        return len(value) in {16, 32} and all(
            character in "0123456789abcdef" for character in value
        )
    return True


def _is_safe_capability_value(key: str, value: object) -> bool:
    if key in {"contract_version", "voice_count"}:
        return type(value) is int and value >= 0
    if key == "ready":
        return type(value) is bool
    return key == "readiness" and isinstance(value, str) and value in _SAFE_READINESS
