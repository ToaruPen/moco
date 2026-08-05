from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from irodori_tts_infra.contracts import HealthResponse

from moco.codex.rpc import JsonValue
from moco.config import CodexSettings, MocoSettings
from moco.doctor import (
    DoctorCheck,
    DoctorRpcClient,
    DoctorSynthesizer,
    _default_hotkey_probe,
    run_doctor,
)


class FakeRpc:
    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> None:
        return None

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        del params, request_timeout
        if method == "account/read":
            return {
                "account": {
                    "email": "private@example.com",
                    "accessToken": "private-token",
                    "planType": "pro",
                },
            }
        if method == "experimentalFeature/list":
            return {
                "data": [
                    {
                        "name": "realtime_conversation",
                        "enabled": True,
                    },
                ],
                "nextCursor": None,
            }
        if method == "thread/realtime/listVoices":
            return {
                "voices": {
                    "v1": ["test-voice"],
                    "v2": [],
                    "defaultV1": "test-voice",
                    "defaultV2": "test-voice",
                },
            }
        message = f"unexpected method: {method}"
        raise AssertionError(message)

    async def close(self) -> None:
        self.closed = True


class FakeSynthesizer:
    def __init__(self) -> None:
        self.closed = False

    async def health(self) -> HealthResponse:
        return HealthResponse(model_loaded=True)

    async def synthesize(self, text: str) -> bytes:
        del text
        return b"RIFF\x04\x00\x00\x00WAVE"

    async def close(self) -> None:
        self.closed = True


class PrivateFailureError(RuntimeError):
    """Synthetic private boundary failure."""


class InputDeniedError(OSError):
    """Synthetic Input Monitoring denial."""


async def test_doctor_reports_stable_checks_without_sensitive_values(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    settings = MocoSettings(
        codex=CodexSettings(binary=binary, working_directory=tmp_path),
    )
    rpc = FakeRpc()
    synthesizer = FakeSynthesizer()

    checks = await run_doctor(
        settings,
        rpc_factory=lambda _path: cast("DoctorRpcClient", rpc),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize="接続確認",
    )
    rendered = "\n".join(f"{check.code}:{check.status}:{check.detail}" for check in checks)

    assert {check.code for check in checks} == {
        "python",
        "config",
        "operator_public_url",
        "cloudflared_binary",
        "cloudflared_service",
        "codex_binary",
        "codex_account",
        "codex_features",
        "codex_voices",
        "irodori_health",
        "irodori_route",
        "irodori_synthesis",
        "hotkeys",
    }
    assert all(check.status == "ok" for check in checks)
    assert "private@example.com" not in rendered
    assert "private-token" not in rendered
    assert str(settings.irodori.base_url) not in rendered
    assert rpc.closed
    assert synthesizer.closed


async def test_doctor_reports_public_operator_boundary(tmp_path: Path) -> None:
    settings = MocoSettings.model_validate(
        {
            "server": {"public_url": "https://voice.example.com"},
            "codex": {
                "binary": str(tmp_path / "missing"),
                "working_directory": str(tmp_path),
            },
        },
    )
    checks = await run_doctor(
        settings,
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            FakeSynthesizer(),
        ),
        hotkey_probe=lambda: True,
        cloudflared_probe=lambda: (True, True),
    )
    by_code = {check.code: check for check in checks}

    assert by_code["operator_public_url"] == DoctorCheck(
        "operator_public_url",
        "ok",
        "configured",
    )
    assert by_code["cloudflared_binary"] == DoctorCheck(
        "cloudflared_binary",
        "ok",
        "available",
    )
    assert by_code["cloudflared_service"] == DoctorCheck(
        "cloudflared_service",
        "ok",
        "running",
    )
    assert "voice.example.com" not in "\n".join(check.detail for check in checks)


@pytest.mark.parametrize(
    ("probe", "binary_check", "service_check"),
    [
        (
            (False, False),
            DoctorCheck("cloudflared_binary", "error", "unavailable"),
            DoctorCheck("cloudflared_service", "blocked", "binary_unavailable"),
        ),
        (
            (True, False),
            DoctorCheck("cloudflared_binary", "ok", "available"),
            DoctorCheck("cloudflared_service", "error", "not_running"),
        ),
    ],
)
async def test_doctor_distinguishes_cloudflared_failures(
    tmp_path: Path,
    probe: tuple[bool, bool],
    binary_check: DoctorCheck,
    service_check: DoctorCheck,
) -> None:
    settings = MocoSettings.model_validate(
        {
            "server": {"public_url": "https://voice.example.com"},
            "codex": {
                "binary": str(tmp_path / "missing"),
                "working_directory": str(tmp_path),
            },
        },
    )
    checks = await run_doctor(
        settings,
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            FakeSynthesizer(),
        ),
        hotkey_probe=lambda: True,
        cloudflared_probe=lambda: probe,
    )

    assert binary_check in checks
    assert service_check in checks


async def test_doctor_reports_explicit_irodori_address_override(
    tmp_path: Path,
) -> None:
    settings = MocoSettings.model_validate(
        {
            "codex": {
                "binary": str(tmp_path / "missing"),
                "working_directory": str(tmp_path),
            },
            "irodori": {
                "base_url": "https://windows-node.example.ts.net",
                "connect_ip": "100.112.161.83",
            },
        },
    )

    checks = await run_doctor(
        settings,
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            FakeSynthesizer(),
        ),
        hotkey_probe=lambda: True,
    )

    assert DoctorCheck("irodori_route", "ok", "address_override_active") in checks


async def test_doctor_blocks_codex_checks_when_binary_is_missing(
    tmp_path: Path,
) -> None:
    synthesizer = FakeSynthesizer()
    settings = MocoSettings(
        codex=CodexSettings(binary=tmp_path / "missing", working_directory=tmp_path),
    )

    checks = await run_doctor(
        settings,
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: False,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_binary"].status == "error"
    assert by_code["codex_account"].detail == "binary_unavailable"
    assert by_code["codex_features"].status == "blocked"
    assert by_code["codex_voices"].status == "blocked"
    assert by_code["hotkeys"].detail == "input_monitoring_required"


class FailingRpc(FakeRpc):
    def __init__(self, failure_method: str, *, close_fails: bool = False) -> None:
        super().__init__()
        self.failure_method = failure_method
        self.close_fails = close_fails

    async def start(self) -> None:
        if self.failure_method == "start":
            raise PrivateFailureError

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        if method == self.failure_method:
            raise PrivateFailureError
        return await super().request(
            method,
            params,
            request_timeout=request_timeout,
        )

    async def close(self) -> None:
        if self.close_fails:
            raise PrivateFailureError
        await super().close()


class InvalidResponseRpc(FakeRpc):
    def __init__(self, method: str, response: JsonValue) -> None:
        super().__init__()
        self.method = method
        self.response = response

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        if method == self.method:
            return self.response
        return await super().request(
            method,
            params,
            request_timeout=request_timeout,
        )


@pytest.mark.parametrize(
    ("method", "response", "failed_code"),
    [
        ("experimentalFeature/list", {}, "codex_features"),
        (
            "experimentalFeature/list",
            {"data": [{"name": "realtime_conversation", "enabled": False}]},
            "codex_features",
        ),
        ("thread/realtime/listVoices", {}, "codex_voices"),
        (
            "thread/realtime/listVoices",
            {"voices": {"v1": [], "v2": []}},
            "codex_voices",
        ),
    ],
)
async def test_doctor_rejects_unusable_codex_responses(
    tmp_path: Path,
    method: str,
    response: JsonValue,
    failed_code: str,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    rpc = InvalidResponseRpc(method, response)

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=binary, working_directory=tmp_path),
        ),
        rpc_factory=lambda _path: cast("DoctorRpcClient", rpc),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            FakeSynthesizer(),
        ),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code[failed_code] == DoctorCheck(failed_code, "error", "unavailable")


@pytest.mark.parametrize(
    ("failure_method", "expected_ok"),
    [
        ("start", set()),
        ("experimentalFeature/list", {"codex_account"}),
        ("thread/realtime/listVoices", {"codex_account", "codex_features"}),
    ],
)
async def test_doctor_maps_partial_codex_failures(
    tmp_path: Path,
    failure_method: str,
    expected_ok: set[str],
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    rpc = FailingRpc(failure_method)

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=binary, working_directory=tmp_path),
        ),
        rpc_factory=lambda _path: cast("DoctorRpcClient", rpc),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            FakeSynthesizer(),
        ),
        hotkey_probe=lambda: True,
    )
    codex_checks = {
        check.code: check
        for check in checks
        if check.code in {"codex_account", "codex_features", "codex_voices"}
    }

    assert {code for code, check in codex_checks.items() if check.status == "ok"} == expected_ok
    assert all(
        check.detail == "probe_failed"
        for code, check in codex_checks.items()
        if code not in expected_ok
    )


class FailingSynthesizer(FakeSynthesizer):
    def __init__(
        self,
        *,
        model_loaded: bool = True,
        health_fails: bool = False,
        synthesis_fails: bool = False,
        close_fails: bool = False,
    ) -> None:
        super().__init__()
        self.model_loaded = model_loaded
        self.health_fails = health_fails
        self.synthesis_fails = synthesis_fails
        self.close_fails = close_fails

    async def health(self) -> HealthResponse:
        if self.health_fails:
            raise PrivateFailureError
        return HealthResponse(model_loaded=self.model_loaded)

    async def synthesize(self, text: str) -> bytes:
        if self.synthesis_fails:
            raise PrivateFailureError
        return await super().synthesize(text)

    async def close(self) -> None:
        if self.close_fails:
            raise PrivateFailureError
        await super().close()


@pytest.mark.parametrize(
    ("synthesizer", "synthesize", "expected"),
    [
        (
            FailingSynthesizer(model_loaded=False),
            None,
            {"irodori_health": ("error", "model_unavailable")},
        ),
        (
            FailingSynthesizer(health_fails=True),
            "test",
            {
                "irodori_health": ("error", "probe_failed"),
                "irodori_synthesis": ("error", "probe_failed"),
            },
        ),
        (
            FailingSynthesizer(synthesis_fails=True),
            "test",
            {
                "irodori_health": ("ok", "model_loaded"),
                "irodori_synthesis": ("error", "probe_failed"),
            },
        ),
    ],
)
async def test_doctor_maps_irodori_failures(
    tmp_path: Path,
    synthesizer: FailingSynthesizer,
    synthesize: str | None,
    expected: dict[str, tuple[str, str]],
) -> None:
    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=tmp_path / "missing", working_directory=tmp_path),
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize=synthesize,
    )
    by_code = {check.code: check for check in checks}

    assert {code: (by_code[code].status, by_code[code].detail) for code in expected} == expected


async def test_doctor_contains_probe_and_cleanup_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    synthesizer = FailingSynthesizer(close_fails=True)

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=binary, working_directory=tmp_path),
        ),
        rpc_factory=lambda _path: cast(
            "DoctorRpcClient",
            FailingRpc("start", close_fails=True),
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: (_ for _ in ()).throw(InputDeniedError),
    )

    assert next(check for check in checks if check.code == "hotkeys").status == "error"
    assert "cleanup failed" in caplog.text
    assert "private cleanup" not in caplog.text


class ProbeListener:
    def __init__(self, **_kwargs: object) -> None:
        self.running = True
        self.stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True


async def test_default_hotkey_probe_starts_and_stops_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners: list[ProbeListener] = []

    def build_listener(**kwargs: object) -> ProbeListener:
        listener = ProbeListener(**kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr("moco.doctor.GlobalHotkeyListener", build_listener)

    assert _default_hotkey_probe(MocoSettings())
    assert listeners[0].stopped
