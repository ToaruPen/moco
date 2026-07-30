from __future__ import annotations

import asyncio
import logging
import os
import platform
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
logger = logging.getLogger(__name__)


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
    synthesize: str | None = None,
) -> list[DoctorCheck]:
    checks = [
        DoctorCheck("python", "ok", platform.python_version()),
        DoctorCheck("config", "ok", "loaded"),
    ]
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
        await client.request("experimentalFeature/list", {})
        results.append(DoctorCheck("codex_features", "ok", "available"))
        await client.request("thread/realtime/listVoices", {})
        results.append(DoctorCheck("codex_voices", "ok", "available"))
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
        ptt_key=settings.hotkeys.push_to_talk,
        cancel_key=settings.hotkeys.cancel,
        emit=lambda _control: None,
    )
    listener = GlobalHotkeyListener(loop=loop, mapper=mapper)
    try:
        listener.start()
        return listener.running
    finally:
        listener.stop()
