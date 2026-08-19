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

from pydantic import ValidationError

from moco.codex.capabilities import (
    CapabilityDiscovery,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    profile_agent_admission,
)
from moco.codex.connection import CodexConnectionSupervisor
from moco.codex.schema import CodexSchemaProbe
from moco.config import AgentProfileMode, MocoSettings
from moco.errors import CodexCommandError
from moco.platform import (
    CodexCommand,
    hotkey_unavailable_detail,
    resolve_codex_command,
    service_supported,
)
from moco.runtime.hotkeys import GlobalHotkeyListener, HotkeyMapper
from moco.speech.contracts import IrodoriCapabilities
from moco.speech.irodori import IrodoriError, IrodoriSynthesizer

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from irodori_tts_infra.contracts import CapabilitiesResponse

    from moco.codex.rpc import JsonValue


class DoctorConnection(Protocol):
    async def start(self) -> None: ...

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue: ...

    async def close(self) -> None: ...


class DoctorCapabilityDiscovery(Protocol):
    async def discover(self) -> CapabilitySnapshot: ...


class DoctorSynthesizer(Protocol):
    async def capabilities(
        self,
    ) -> CapabilitiesResponse | IrodoriCapabilities: ...

    def select_voice(self, voice_id: str) -> None: ...

    async def synthesize(self, text: str) -> bytes: ...

    async def close(self) -> None: ...


type CommandResolver = Callable[[tuple[str, ...] | None], CodexCommand]
type ConnectionFactory = Callable[[CodexCommand], DoctorConnection]
type DiscoveryFactory = Callable[
    [CodexCommand, DoctorConnection, Path],
    DoctorCapabilityDiscovery,
]
type SynthesizerFactory = Callable[[MocoSettings], DoctorSynthesizer]
type CloudflaredProbe = Callable[[], tuple[bool, bool]]
logger = logging.getLogger(__name__)
_CLOUDFLARED_SERVICE_LABEL = "dev.toarupen.moco-cloudflared"
_CODEX_CHECK_CODES = (
    "codex_command",
    "codex_schema",
    "codex_account",
    "codex_policy",
    "codex_agent_admission",
    "codex_local_review",
    "codex_realtime",
    "codex_interrupt",
    "codex_server_requests",
)
_CODEX_DETAILS_BY_STATUS: dict[CapabilityStatus, frozenset[str]] = {
    CapabilityStatus.DISABLED: frozenset(
        {"feature_disabled", "no_voice", "prompt_overridden", "unsafe_voice_policy"}
    ),
    CapabilityStatus.AUTHENTICATION_REQUIRED: frozenset({"authentication_required"}),
    CapabilityStatus.VERSION_MISMATCH: frozenset(
        {
            "approval_categories_unavailable",
            "approval_family_unadaptable",
            "agent_event_contract_unavailable",
            "invalid_response",
            "method_unavailable",
            "unclassified_server_requests",
        }
    ),
    CapabilityStatus.ERROR: frozenset({"probe_failed"}),
}
_IRODORI_SYNTHESIS_DETAILS = frozenset(
    {
        "runtime_generation_mismatch",
        "voice_not_found",
        "model_loading",
        "model_not_loaded",
        "voice_bank_invalid",
        "audio_too_large",
        "invalid_audio",
    },
)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    code: str
    status: str
    detail: str


async def run_doctor(
    settings: MocoSettings,
    *,
    command_resolver: CommandResolver | None = None,
    connection_factory: ConnectionFactory | None = None,
    discovery_factory: DiscoveryFactory | None = None,
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
    checks.append(DoctorCheck("codex_profile", "ok", settings.agent.profile.value))
    resolver = command_resolver or resolve_codex_command
    try:
        command = resolver(settings.codex.command)
    except CodexCommandError:
        checks.extend(_codex_command_unavailable_checks())
    except Exception as error:  # noqa: BLE001
        logger.warning("Doctor Codex command probe failed (type=%s)", type(error).__name__)
        checks.extend(_codex_probe_failed_checks(include_command=True))
    else:
        checks.append(DoctorCheck("codex_command", "ok", "available"))
        try:
            working_directory = (settings.codex.working_directory or Path.cwd()).absolute()
        except Exception as error:  # noqa: BLE001
            logger.warning("Doctor Codex probe failed (type=%s)", type(error).__name__)
            checks.extend(_codex_probe_failed_checks())
        else:
            checks.extend(
                await _probe_codex(
                    command,
                    working_directory=working_directory,
                    profile=settings.agent.profile,
                    connection_factory=connection_factory or _default_connection_factory,
                    discovery_factory=discovery_factory or _default_discovery_factory,
                ),
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
            "available" if hotkeys_ok else hotkey_unavailable_detail(),
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
    if service_running:
        service_status = "ok"
        service_detail = "running"
    elif binary_available:
        service_status = "error"
        service_detail = "not_running"
    else:
        service_status = "blocked"
        service_detail = "binary_unavailable"
    return [
        DoctorCheck("operator_public_url", "ok", "configured"),
        DoctorCheck(
            "cloudflared_binary",
            "ok" if binary_available else "error",
            "available" if binary_available else "unavailable",
        ),
        DoctorCheck(
            "cloudflared_service",
            service_status,
            service_detail,
        ),
    ]


def _default_cloudflared_probe() -> tuple[bool, bool]:
    binary_available = shutil.which("cloudflared") is not None
    if not binary_available:
        return False, False
    if not service_supported():
        return True, False
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
    command: CodexCommand,
    *,
    working_directory: Path,
    profile: AgentProfileMode,
    connection_factory: ConnectionFactory,
    discovery_factory: DiscoveryFactory,
) -> list[DoctorCheck]:
    try:
        connection = connection_factory(command)
    except Exception as error:  # noqa: BLE001
        logger.warning("Doctor Codex probe failed (type=%s)", type(error).__name__)
        return _codex_probe_failed_checks()
    try:
        await connection.start()
        discovery = discovery_factory(command, connection, working_directory)
        snapshot: object = await discovery.discover()
        return _project_codex_snapshot(_validated_snapshot(snapshot), profile)
    except Exception as error:  # noqa: BLE001
        logger.warning("Doctor Codex probe failed (type=%s)", type(error).__name__)
        return _codex_probe_failed_checks()
    finally:
        try:
            await connection.close()
        except Exception as error:  # noqa: BLE001
            logger.warning("Doctor Codex cleanup failed (type=%s)", type(error).__name__)


def _validated_snapshot(value: object) -> CapabilitySnapshot:
    if not isinstance(value, CapabilitySnapshot):
        message = "Codex discovery returned an invalid snapshot"
        raise TypeError(message)
    return value


def _project_codex_snapshot(
    snapshot: CapabilitySnapshot,
    profile: AgentProfileMode,
) -> list[DoctorCheck]:
    return [
        _project_schema(snapshot),
        _project_capability("codex_account", snapshot.account, available_detail="authenticated"),
        _project_policy(snapshot.effective_policy, snapshot.policy_state),
        _project_capability(
            "codex_agent_admission",
            profile_agent_admission(snapshot, profile),
            available_detail="allowed",
        ),
        _project_capability(
            "codex_local_review",
            snapshot.server_requests,
            available_detail="available",
        ),
        _project_capability("codex_realtime", snapshot.realtime, available_detail="available"),
        _project_capability("codex_interrupt", snapshot.interrupt, available_detail="available"),
        _project_capability(
            "codex_server_requests",
            snapshot.server_requests,
            available_detail="discovered",
        ),
    ]


def _project_schema(snapshot: CapabilitySnapshot) -> DoctorCheck:
    states = (
        snapshot.account,
        snapshot.policy_state,
        snapshot.managed_requirements,
        snapshot.agent_admission,
        snapshot.realtime,
        snapshot.interrupt,
        snapshot.server_requests,
    )
    if all(
        state.status is CapabilityStatus.ERROR and state.detail == "probe_failed"
        for state in states
    ):
        return DoctorCheck("codex_schema", "error", "probe_failed")
    if all(
        state.status is CapabilityStatus.VERSION_MISMATCH and state.detail == "invalid_response"
        for state in states
    ):
        return DoctorCheck("codex_schema", "error", "version_mismatch")
    if not isinstance(snapshot.version, str) or not snapshot.version:
        return DoctorCheck("codex_schema", "error", "probe_failed")
    return DoctorCheck("codex_schema", "ok", "compatible")


def _project_policy(
    policy: EffectivePolicy | None,
    state: CapabilityState,
) -> DoctorCheck:
    if state.status is not CapabilityStatus.AVAILABLE:
        return _project_capability("codex_policy", state, available_detail="ready")
    if not isinstance(policy, EffectivePolicy):
        return DoctorCheck("codex_policy", "error", "invalid_response")
    detail = f"{policy.sandbox.value}_{policy.approval.value}".replace("-", "_")
    return DoctorCheck("codex_policy", "ok", detail)


def _project_capability(
    code: str,
    state: CapabilityState,
    *,
    available_detail: str,
) -> DoctorCheck:
    if state.status is CapabilityStatus.AVAILABLE:
        return DoctorCheck(code, "ok", available_detail)
    if state.status is CapabilityStatus.AUTHENTICATION_REQUIRED:
        return DoctorCheck(code, "blocked", "authentication_required")
    allowed = _CODEX_DETAILS_BY_STATUS.get(state.status, frozenset())
    detail = state.detail if state.detail in allowed else "invalid_response"
    return DoctorCheck(code, "error", detail)


def _codex_command_unavailable_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck("codex_command", "error", "unavailable"),
        *(DoctorCheck(code, "blocked", "command_unavailable") for code in _CODEX_CHECK_CODES[1:]),
    ]


def _codex_probe_failed_checks(*, include_command: bool = False) -> list[DoctorCheck]:
    codes = _CODEX_CHECK_CODES if include_command else _CODEX_CHECK_CODES[1:]
    return [DoctorCheck(code, "error", "probe_failed") for code in codes]


async def _probe_irodori(
    settings: MocoSettings,
    *,
    factory: SynthesizerFactory,
    synthesize: str | None,
) -> list[DoctorCheck]:
    try:
        synthesizer = factory(settings)
    except Exception:  # noqa: BLE001
        unavailable_checks = [
            DoctorCheck("irodori_capabilities", "error", "irodori_unavailable"),
        ]
        if synthesize is not None:
            unavailable_checks.append(
                DoctorCheck("irodori_synthesis", "error", "irodori_unavailable"),
            )
        return unavailable_checks

    checks: list[DoctorCheck] = []
    try:
        capability_check, selected_voice_id = await _check_irodori_capabilities(
            synthesizer,
            configured=settings.irodori.speaker,
        )
        checks.append(capability_check)
        if selected_voice_id is None:
            if synthesize is not None:
                checks.append(
                    DoctorCheck(
                        "irodori_synthesis",
                        "error",
                        capability_check.detail,
                    ),
                )
            return checks
        if synthesize is not None:
            checks.append(
                await _check_irodori_synthesis(
                    synthesizer,
                    text=synthesize,
                    voice_id=selected_voice_id,
                ),
            )
    finally:
        try:
            await synthesizer.close()
        except Exception as error:  # noqa: BLE001
            logger.warning("Doctor Irodori cleanup failed (type=%s)", type(error).__name__)
    return checks


async def _check_irodori_capabilities(
    synthesizer: DoctorSynthesizer,
    *,
    configured: str | None,
) -> tuple[DoctorCheck, str | None]:
    try:
        capabilities = await _load_irodori_capabilities(synthesizer)
    except IrodoriError as error:
        error_detail = _irodori_capability_error_detail(error)
        return DoctorCheck("irodori_capabilities", "error", error_detail), None
    except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
        return DoctorCheck("irodori_capabilities", "error", "capability_mismatch"), None
    except Exception:  # noqa: BLE001
        return DoctorCheck("irodori_capabilities", "error", "irodori_unavailable"), None

    if not capabilities.ready:
        return DoctorCheck(
            "irodori_capabilities",
            "error",
            capabilities.readiness,
        ), None
    selected_voice_id, selection_detail = _resolve_irodori_voice(
        capabilities,
        configured=configured,
    )
    if selection_detail is not None:
        return DoctorCheck("irodori_capabilities", "error", selection_detail), None
    return DoctorCheck("irodori_capabilities", "ok", "ready"), selected_voice_id


async def _check_irodori_synthesis(
    synthesizer: DoctorSynthesizer,
    *,
    text: str,
    voice_id: str,
) -> DoctorCheck:
    try:
        synthesizer.select_voice(voice_id)
        wav = await synthesizer.synthesize(text)
    except IrodoriError as error:
        detail = error.code if error.code in _IRODORI_SYNTHESIS_DETAILS else "probe_failed"
        return DoctorCheck("irodori_synthesis", "error", detail)
    except Exception:  # noqa: BLE001
        return DoctorCheck("irodori_synthesis", "error", "probe_failed")
    return DoctorCheck("irodori_synthesis", "ok", f"wav_bytes_{len(wav)}")


async def _load_irodori_capabilities(
    synthesizer: DoctorSynthesizer,
) -> IrodoriCapabilities:
    response = await synthesizer.capabilities()
    return IrodoriCapabilities.model_validate(
        response.model_dump(mode="python"),
        strict=True,
    )


def _resolve_irodori_voice(
    capabilities: IrodoriCapabilities,
    *,
    configured: str | None,
) -> tuple[str | None, str | None]:
    if not capabilities.voices:
        return None, "catalog_empty"
    if configured is not None:
        canonical = next(
            (voice.id for voice in capabilities.voices if voice.id == configured),
            None,
        )
        if canonical is not None:
            return canonical, None
        aliases = [voice.id for voice in capabilities.voices if configured in voice.aliases]
        if len(aliases) == 1:
            return aliases[0], None
        return None, "configured_voice_unavailable"
    defaults = [voice.id for voice in capabilities.voices if voice.default]
    if len(defaults) == 1:
        return defaults[0], None
    return None, "voice_selection_required"


def _irodori_capability_error_detail(error: IrodoriError) -> str:
    if error.code == "invalid_response":
        return "capability_mismatch"
    if error.code in {"model_loading", "model_not_loaded", "voice_bank_invalid"}:
        return error.code
    return "irodori_unavailable"


def _default_connection_factory(command: CodexCommand) -> DoctorConnection:
    return CodexConnectionSupervisor(command)


@dataclass(frozen=True, slots=True)
class _SchemaBackedDiscovery:
    command: CodexCommand
    connection: DoctorConnection
    working_directory: Path

    async def discover(self) -> CapabilitySnapshot:
        contract = await CodexSchemaProbe(self.command).probe()
        return await CapabilityDiscovery(
            self.connection,
            working_directory=self.working_directory,
            contract=contract,
        ).discover()


def _default_discovery_factory(
    command: CodexCommand,
    connection: DoctorConnection,
    working_directory: Path,
) -> DoctorCapabilityDiscovery:
    return _SchemaBackedDiscovery(command, connection, working_directory)


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
