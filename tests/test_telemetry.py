from __future__ import annotations

import logging

import pytest

from moco.config import TelemetrySettings
from moco.runtime.telemetry import (
    TelemetryAttributeError,
    boundary_label,
    configure_telemetry,
    safe_event,
    sanitize_attributes,
)


def test_allows_only_bounded_operational_attributes() -> None:
    attributes = {
        "event_code": "cancelled",
        "state": "ready",
        "duration_ms": 42,
        "trace_id": "0123456789abcdef",
        "component": "speech",
        "boundary": "irodori_http",
    }

    assert sanitize_attributes(attributes, strict=True) == attributes


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
