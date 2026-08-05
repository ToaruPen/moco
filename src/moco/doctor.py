from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from moco.codex.rpc import CodexRpcClient
from moco.config import MocoSettings
from moco.runtime.hotkeys import GlobalHotkeyListener, HotkeyMapper
from moco.speech.irodori import IrodoriSynthesizer

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from irodori_tts_infra.contracts import HealthResponse

    from moco.codex.rpc import JsonValue


class DoctorRpcClient(Protocol):
    async def start(self) -> None: ...

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue: ...

    async def close(self) -> None: ...


class DoctorSynthesizer(Protocol):
    async def health(self) -> HealthResponse: ...

    async def synthesize(self, text: str) -> bytes: ...

    async def close(self) -> None: ...


type RpcFactory = Callable[[Path], DoctorRpcClient]
type SynthesizerFactory = Callable[[MocoSettings], DoctorSynthesizer]
type CloudflaredProbe = Callable[[], tuple[bool, bool]]
logger = logging.getLogger(__name__)
_CLOUDFLARED_SERVICE_LABEL = "dev.toarupen.moco-cloudflared"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    code: str
    status: str
    detail: str


async def run_doctor(
    settings: MocoSettings,
    *,
    rpc_factory: RpcFactory | None = None,
    synthesizer_factory: SynthesizerFactory | None = None,
    hotkey_probe: Callable[[], bool] | None = None,
    cloudflared_probe: CloudflaredProbe | None = None,
    synthesize: str | None = None,
) -> list[DoctorCheck]:
    checks = [
        DoctorCheck("python", "ok", platform.python_version()),
        DoctorCheck("config", "ok", "loaded"),
    ]
    checks.extend(
        _check_public_operator(
            settings,
            probe=cloudflared_probe or _default_cloudflared_probe,
        ),
    )
    binary = settings.codex.binary
    binary_ok = binary.is_file() and os.access(binary, os.X_OK)
    checks.append(
        DoctorCheck(
            "codex_binary",
            "ok" if binary_ok else "error",
            "available" if binary_ok else "unavailable",
        ),
    )
    if binary_ok:
        checks.extend(
            await _probe_codex(
                binary,
                factory=rpc_factory or _default_rpc_factory,
            ),
        )
    else:
        checks.extend(
            [
                DoctorCheck("codex_account", "blocked", "binary_unavailable"),
                DoctorCheck("codex_features", "blocked", "binary_unavailable"),
                DoctorCheck("codex_voices", "blocked", "binary_unavailable"),
            ],
        )

    checks.extend(
        await _probe_irodori(
            settings,
            factory=synthesizer_factory or _default_synthesizer_factory,
            synthesize=synthesize,
        ),
    )
    checks.append(
        DoctorCheck(
            "irodori_route",
            "ok",
            (
                "address_override_active"
                if settings.irodori.connect_ip is not None
                else "system_dns"
            ),
        ),
    )
    probe = hotkey_probe or (lambda: _default_hotkey_probe(settings))
    try:
        hotkeys_ok = probe()
    except (OSError, RuntimeError):
        hotkeys_ok = False
    checks.append(
        DoctorCheck(
            "hotkeys",
            "ok" if hotkeys_ok else "error",
            "available" if hotkeys_ok else "input_monitoring_required",
        ),
    )
    return checks


def _check_public_operator(
    settings: MocoSettings,
    *,
    probe: CloudflaredProbe,
) -> list[DoctorCheck]:
    if settings.server.public_url is None:
        return [
            DoctorCheck("operator_public_url", "ok", "not_configured"),
            DoctorCheck("cloudflared_binary", "ok", "not_configured"),
            DoctorCheck("cloudflared_service", "ok", "not_configured"),
        ]
    try:
        binary_available, service_running = probe()
    except (OSError, subprocess.SubprocessError):
        return [
            DoctorCheck("operator_public_url", "ok", "configured"),
            DoctorCheck("cloudflared_binary", "error", "probe_failed"),
            DoctorCheck("cloudflared_service", "blocked", "probe_failed"),
        ]
    return [
        DoctorCheck("operator_public_url", "ok", "configured"),
        DoctorCheck(
            "cloudflared_binary",
            "ok" if binary_available else "error",
            "available" if binary_available else "unavailable",
        ),
        DoctorCheck(
            "cloudflared_service",
            "ok" if service_running else ("error" if binary_available else "blocked"),
            (
                "running"
                if service_running
                else ("not_running" if binary_available else "binary_unavailable")
            ),
        ),
    ]


def _default_cloudflared_probe() -> tuple[bool, bool]:
    binary_available = shutil.which("cloudflared") is not None
    if not binary_available:
        return False, False
    completed = subprocess.run(  # noqa: S603
        [
            "/bin/launchctl",
            "print",
            f"gui/{os.getuid()}/{_CLOUDFLARED_SERVICE_LABEL}",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    return True, completed.returncode == 0 and b"state = running" in completed.stdout


async def _probe_codex(
    binary: Path,
    *,
    factory: RpcFactory,
) -> list[DoctorCheck]:
    client = factory(binary)
    results: list[DoctorCheck] = []
    try:
        await client.start()
        account = await client.request("account/read", {"refreshToken": False})
        account_ok = (
            isinstance(account, dict)
            and isinstance(account.get("account"), dict)
            and bool(account["account"])
        )
        results.append(
            DoctorCheck(
                "codex_account",
                "ok" if account_ok else "error",
                "authenticated" if account_ok else "unavailable",
            ),
        )
        features = await client.request("experimentalFeature/list", {})
        features_ok = _realtime_feature_available(features)
        results.append(
            DoctorCheck(
                "codex_features",
                "ok" if features_ok else "error",
                "available" if features_ok else "unavailable",
            ),
        )
        voices = await client.request("thread/realtime/listVoices", {})
        voices_ok = _realtime_voices_available(voices)
        results.append(
            DoctorCheck(
                "codex_voices",
                "ok" if voices_ok else "error",
                "available" if voices_ok else "unavailable",
            ),
        )
    except Exception:  # noqa: BLE001
        existing = {check.code for check in results}
        results.extend(
            DoctorCheck(code, "error", "probe_failed")
            for code in ("codex_account", "codex_features", "codex_voices")
            if code not in existing
        )
    finally:
        try:
            await client.close()
        except Exception as error:  # noqa: BLE001
            logger.warning("Doctor Codex cleanup failed (type=%s)", type(error).__name__)
    return results


def _realtime_feature_available(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    features = response.get("data")
    if not isinstance(features, list):
        return False
    return any(
        isinstance(feature, dict)
        and feature.get("name") == "realtime_conversation"
        and feature.get("enabled") is True
        for feature in features
    )


def _realtime_voices_available(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    voices = response.get("voices")
    if not isinstance(voices, dict):
        return False
    return any(
        isinstance(options, list)
        and any(isinstance(voice, str) and bool(voice) for voice in options)
        for version in ("v1", "v2")
        if (options := voices.get(version)) is not None
    )


async def _probe_irodori(
    settings: MocoSettings,
    *,
    factory: SynthesizerFactory,
    synthesize: str | None,
) -> list[DoctorCheck]:
    synthesizer = factory(settings)
    checks: list[DoctorCheck] = []
    try:
        health = await synthesizer.health()
        checks.append(
            DoctorCheck(
                "irodori_health",
                "ok" if health.model_loaded else "error",
                "model_loaded" if health.model_loaded else "model_unavailable",
            ),
        )
        if synthesize is not None:
            wav = await synthesizer.synthesize(synthesize)
            checks.append(DoctorCheck("irodori_synthesis", "ok", f"wav_bytes_{len(wav)}"))
    except Exception:  # noqa: BLE001
        if not checks:
            checks.append(DoctorCheck("irodori_health", "error", "probe_failed"))
        if synthesize is not None:
            checks.append(DoctorCheck("irodori_synthesis", "error", "probe_failed"))
    finally:
        try:
            await synthesizer.close()
        except Exception as error:  # noqa: BLE001
            logger.warning("Doctor Irodori cleanup failed (type=%s)", type(error).__name__)
    return checks


def _default_rpc_factory(binary: Path) -> DoctorRpcClient:
    return CodexRpcClient(binary)


def _default_synthesizer_factory(settings: MocoSettings) -> DoctorSynthesizer:
    return IrodoriSynthesizer.from_settings(settings)


def _default_hotkey_probe(settings: MocoSettings) -> bool:
    loop = asyncio.get_running_loop()
    mapper = HotkeyMapper(
        start_key=settings.hotkeys.start_listening,
        stop_key=settings.hotkeys.stop_listening,
        emit=lambda _control: None,
    )
    listener = GlobalHotkeyListener(loop=loop, mapper=mapper)
    try:
        listener.start()
        return listener.running
    finally:
        listener.stop()
