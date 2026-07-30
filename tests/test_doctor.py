from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from irodori_tts_infra.contracts import HealthResponse

from moco.codex.rpc import JsonValue
from moco.config import CodexSettings, MocoSettings
from moco.doctor import DoctorRpcClient, DoctorSynthesizer, run_doctor


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
        if method in {"experimentalFeature/list", "thread/realtime/listVoices"}:
            return {}
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
        "codex_binary",
        "codex_account",
        "codex_features",
        "codex_voices",
        "irodori_health",
        "irodori_synthesis",
        "hotkeys",
    }
    assert all(check.status == "ok" for check in checks)
    assert "private@example.com" not in rendered
    assert "private-token" not in rendered
    assert str(settings.irodori.base_url) not in rendered
    assert rpc.closed
    assert synthesizer.closed
