from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest
from irodori_tts_infra.contracts import CapabilitiesResponse, Readiness, VoiceCapability

from moco.codex.rpc import JsonValue
from moco.config import CodexSettings, IrodoriSettings, MocoSettings
from moco.doctor import (
    DoctorCheck,
    DoctorRpcClient,
    DoctorSynthesizer,
    _default_hotkey_probe,
    run_doctor,
)
from moco.speech.irodori import IrodoriError


def make_capabilities(
    count: int,
    *,
    default_index: int | None = 0,
    generation: str = "fixture-generation-0",
    ready: bool = True,
    readiness: Readiness | None = None,
) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        generation=generation,
        ready=ready,
        readiness=readiness or ("ready" if ready else "model_loading"),
        voices=tuple(
            VoiceCapability(
                id=f"fixture-id-{index}",
                label=f"Fixture label {index}",
                aliases=(f"fixture-alias-{index}",),
                default=index == default_index,
            )
            for index in range(count)
        ),
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


def raise_cloudflared_timeout() -> tuple[bool, bool]:
    raise subprocess.TimeoutExpired(cmd="launchctl", timeout=5)


class FakeSynthesizer:
    def __init__(
        self,
        capabilities: object | None = None,
        *,
        capability_error: Exception | None = None,
        selection_error: IrodoriError | None = None,
        synthesis_error: IrodoriError | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.closed = False
        self.capabilities_response = capabilities or make_capabilities(3)
        self.capability_error = capability_error
        self.selection_error = selection_error
        self.synthesis_error = synthesis_error
        self.close_error = close_error
        self.selected_voice_ids: list[str] = []
        self.synthesized_texts: list[str] = []

    async def capabilities(self) -> CapabilitiesResponse:
        if self.capability_error is not None:
            raise self.capability_error
        return cast("CapabilitiesResponse", self.capabilities_response)

    def select_voice(self, voice_id: str) -> None:
        if self.selection_error is not None:
            raise self.selection_error
        self.selected_voice_ids.append(voice_id)

    async def synthesize(self, text: str) -> bytes:
        self.synthesized_texts.append(text)
        if self.synthesis_error is not None:
            raise self.synthesis_error
        return b"RIFF\x04\x00\x00\x00WAVE"

    async def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
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
    capabilities = make_capabilities(3)
    synthesizer = FakeSynthesizer(capabilities)

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
        "irodori_capabilities",
        "irodori_route",
        "irodori_synthesis",
        "hotkeys",
    }
    assert all(check.status == "ok" for check in checks)
    assert "private@example.com" not in rendered
    assert "private-token" not in rendered
    assert str(settings.irodori.base_url) not in rendered
    assert capabilities.generation not in rendered
    assert "voice_count" not in rendered
    assert all(voice.id not in rendered for voice in capabilities.voices)
    assert all(voice.label not in rendered for voice in capabilities.voices)
    assert all(alias not in rendered for voice in capabilities.voices for alias in voice.aliases)
    assert synthesizer.selected_voice_ids == [capabilities.voices[0].id]
    assert synthesizer.synthesized_texts == ["接続確認"]
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
            lambda: (False, False),
            DoctorCheck("cloudflared_binary", "error", "unavailable"),
            DoctorCheck("cloudflared_service", "blocked", "binary_unavailable"),
        ),
        (
            lambda: (True, False),
            DoctorCheck("cloudflared_binary", "ok", "available"),
            DoctorCheck("cloudflared_service", "error", "not_running"),
        ),
        (
            raise_cloudflared_timeout,
            DoctorCheck("cloudflared_binary", "error", "probe_failed"),
            DoctorCheck("cloudflared_service", "blocked", "probe_failed"),
        ),
    ],
)
async def test_doctor_distinguishes_cloudflared_failures(
    tmp_path: Path,
    probe: Callable[[], tuple[bool, bool]],
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
        cloudflared_probe=probe,
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


@pytest.mark.parametrize(
    ("capabilities", "settings", "expected_detail"),
    [
        (
            make_capabilities(0),
            MocoSettings(),
            "catalog_empty",
        ),
        (
            make_capabilities(2),
            MocoSettings(irodori=IrodoriSettings(speaker="private-missing-voice")),
            "configured_voice_unavailable",
        ),
        (
            make_capabilities(2, default_index=None),
            MocoSettings(),
            "voice_selection_required",
        ),
        (
            make_capabilities(2, ready=False, readiness="model_loading"),
            MocoSettings(),
            "model_loading",
        ),
        (
            make_capabilities(2, ready=False, readiness="model_not_loaded"),
            MocoSettings(),
            "model_not_loaded",
        ),
        (
            make_capabilities(2, ready=False, readiness="voice_bank_invalid"),
            MocoSettings(),
            "voice_bank_invalid",
        ),
        (
            make_capabilities(2).model_copy(update={"contract_version": 2}),
            MocoSettings(),
            "capability_mismatch",
        ),
    ],
)
async def test_doctor_fails_closed_for_unusable_irodori_capabilities(
    tmp_path: Path,
    capabilities: object,
    settings: MocoSettings,
    expected_detail: str,
) -> None:
    synthesizer = FakeSynthesizer(capabilities)
    checks = await run_doctor(
        settings.model_copy(
            update={
                "codex": CodexSettings(
                    binary=tmp_path / "missing",
                    working_directory=tmp_path,
                ),
            },
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize="test",
    )
    by_code = {check.code: check for check in checks}

    assert by_code["irodori_capabilities"] == DoctorCheck(
        "irodori_capabilities",
        "error",
        expected_detail,
    )
    assert by_code["irodori_synthesis"] == DoctorCheck(
        "irodori_synthesis",
        "error",
        expected_detail,
    )
    assert synthesizer.selected_voice_ids == []
    assert synthesizer.synthesized_texts == []
    assert synthesizer.closed


@pytest.mark.parametrize("selector_kind", ["canonical", "alias", "default"])
async def test_doctor_selects_a_canonical_voice_only_after_ready_validation(
    tmp_path: Path,
    selector_kind: str,
) -> None:
    capabilities = make_capabilities(3, default_index=2)
    selected = capabilities.voices[2 if selector_kind == "default" else 1]
    selector = {
        "canonical": selected.id,
        "alias": selected.aliases[0],
        "default": None,
    }[selector_kind]
    synthesizer = FakeSynthesizer(capabilities)

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=tmp_path / "missing", working_directory=tmp_path),
            irodori=IrodoriSettings(speaker=selector),
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize="test",
    )

    assert DoctorCheck("irodori_capabilities", "ok", "ready") in checks
    assert synthesizer.selected_voice_ids == [selected.id]
    assert synthesizer.synthesized_texts == ["test"]
    assert synthesizer.closed


async def test_doctor_maps_network_failure_without_rendering_private_detail(
    tmp_path: Path,
) -> None:
    private_message = "https://private-host.example.test/private-token"
    synthesizer = FakeSynthesizer(capability_error=OSError(private_message))

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=tmp_path / "missing", working_directory=tmp_path),
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize="test",
    )
    by_code = {check.code: check for check in checks}
    rendered = repr(checks)

    assert by_code["irodori_capabilities"].detail == "irodori_unavailable"
    assert by_code["irodori_synthesis"].detail == "irodori_unavailable"
    assert private_message not in rendered
    assert synthesizer.closed


@pytest.mark.parametrize(
    ("failure_stage", "error_code"),
    [
        ("selection", "voice_not_found"),
        ("synthesis", "runtime_generation_mismatch"),
        ("synthesis", "model_loading"),
        ("synthesis", "model_not_loaded"),
        ("synthesis", "voice_bank_invalid"),
        ("synthesis", "audio_too_large"),
        ("synthesis", "invalid_audio"),
    ],
)
async def test_doctor_preserves_known_synthesis_detail_without_fallback(
    tmp_path: Path,
    failure_stage: str,
    error_code: str,
) -> None:
    capabilities = make_capabilities(2)
    error = IrodoriError("private boundary message", code=error_code)
    synthesizer = FakeSynthesizer(
        capabilities,
        selection_error=error if failure_stage == "selection" else None,
        synthesis_error=error if failure_stage == "synthesis" else None,
    )

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=tmp_path / "missing", working_directory=tmp_path),
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize="test",
    )
    by_code = {check.code: check for check in checks}

    assert by_code["irodori_capabilities"] == DoctorCheck(
        "irodori_capabilities",
        "ok",
        "ready",
    )
    assert by_code["irodori_synthesis"] == DoctorCheck(
        "irodori_synthesis",
        "error",
        error_code,
    )
    assert synthesizer.selected_voice_ids == (
        [] if failure_stage == "selection" else [capabilities.voices[0].id]
    )
    assert synthesizer.synthesized_texts == ([] if failure_stage == "selection" else ["test"])
    assert "private boundary message" not in repr(checks)
    assert synthesizer.closed


async def test_doctor_bounds_unknown_synthesis_error_code(
    tmp_path: Path,
) -> None:
    private_code = "private-token-in-code"
    synthesizer = FakeSynthesizer(
        synthesis_error=IrodoriError("private message", code=private_code),
    )

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(binary=tmp_path / "missing", working_directory=tmp_path),
        ),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: True,
        synthesize="test",
    )
    by_code = {check.code: check for check in checks}
    rendered = repr(checks)

    assert by_code["irodori_synthesis"] == DoctorCheck(
        "irodori_synthesis",
        "error",
        "probe_failed",
    )
    assert private_code not in rendered
    assert "private message" not in rendered
    assert synthesizer.closed


async def test_doctor_contains_probe_and_cleanup_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    synthesizer = FakeSynthesizer(close_error=PrivateFailureError())

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
