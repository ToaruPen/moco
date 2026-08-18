from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest
from irodori_tts_infra.contracts import CapabilitiesResponse, Readiness, VoiceCapability

from moco import doctor as doctor_module
from moco.codex.capabilities import (
    ApprovalMode,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    SandboxMode,
)
from moco.codex.rpc import JsonValue
from moco.config import (
    AgentProfileMode,
    AgentSettings,
    CodexSettings,
    IrodoriSettings,
    MocoSettings,
)
from moco.doctor import (
    DoctorCheck,
    DoctorSynthesizer,
    _default_cloudflared_probe,
    _default_hotkey_probe,
    run_doctor,
)
from moco.errors import CodexCommandError
from moco.platform import CodexCommand
from moco.speech.irodori import IrodoriError

_AVAILABLE = CapabilityState(CapabilityStatus.AVAILABLE, "ready")
_DEFAULT_POLICY = EffectivePolicy(
    SandboxMode.WORKSPACE_WRITE,
    ApprovalMode.ON_REQUEST,
)


def make_snapshot(  # noqa: PLR0913
    *,
    version: str = "private-version-value",
    account: CapabilityState = _AVAILABLE,
    effective_policy: EffectivePolicy | None = _DEFAULT_POLICY,
    policy_state: CapabilityState = _AVAILABLE,
    managed_requirements: CapabilityState = _AVAILABLE,
    agent_admission: CapabilityState = _AVAILABLE,
    realtime: CapabilityState = _AVAILABLE,
    interrupt: CapabilityState = _AVAILABLE,
    steer: CapabilityState = _AVAILABLE,
    server_requests: CapabilityState = _AVAILABLE,
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        version=version,
        account=account,
        effective_policy=effective_policy,
        policy_state=policy_state,
        managed_requirements=managed_requirements,
        agent_admission=agent_admission,
        realtime=realtime,
        interrupt=interrupt,
        steer=steer,
        server_requests=server_requests,
        server_request_categories=frozenset(),
        has_unclassified_server_requests=False,
    )


class FakeConnection:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.start_error = start_error
        self.close_error = close_error
        self.start_calls = 0
        self.close_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        del method, params, request_timeout
        message = "fake discovery must own capability responses"
        raise AssertionError(message)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeCapabilityDiscovery:
    def __init__(
        self,
        snapshot: CapabilitySnapshot,
        *,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    async def discover(self) -> CapabilitySnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


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


def raise_cloudflared_timeout() -> tuple[bool, bool]:
    raise subprocess.TimeoutExpired(cmd="launchctl", timeout=5)


def test_default_cloudflared_probe_never_calls_launchctl_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("moco.doctor.shutil.which", lambda _name: "/fixture/cloudflared")
    monkeypatch.setattr(doctor_module, "service_supported", lambda: False, raising=False)

    def reject_getuid() -> int:
        pytest.fail("non-Darwin cloudflared probe must not resolve a launchd uid")

    def reject_launchctl(*_args: object, **_kwargs: object) -> object:
        pytest.fail("non-Darwin cloudflared probe must not invoke launchctl")

    monkeypatch.setattr("moco.doctor.os.getuid", reject_getuid, raising=False)
    monkeypatch.setattr("moco.doctor.subprocess.run", reject_launchctl)

    assert _default_cloudflared_probe() == (True, False)


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


async def test_doctor_projects_stage_b_codex_snapshot_without_private_values(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(
        account=CapabilityState(CapabilityStatus.AVAILABLE, "authenticated"),
        policy_state=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "allowed"),
        realtime=CapabilityState(CapabilityStatus.AVAILABLE, "available"),
        interrupt=CapabilityState(CapabilityStatus.AVAILABLE, "available"),
        server_requests=CapabilityState(CapabilityStatus.AVAILABLE, "discovered"),
    )
    connection = FakeConnection()
    discovery = FakeCapabilityDiscovery(snapshot)
    resolved: list[tuple[str, ...] | None] = []
    discovery_arguments: list[tuple[CodexCommand, object, Path]] = []

    def resolve(value: tuple[str, ...] | None) -> CodexCommand:
        resolved.append(value)
        return CodexCommand(("fixture-codex-private",))

    def build_discovery(
        command: CodexCommand,
        rpc: object,
        working_directory: Path,
    ) -> FakeCapabilityDiscovery:
        discovery_arguments.append((command, rpc, working_directory))
        return discovery

    checks = await run_doctor(
        MocoSettings(
            codex=CodexSettings(command=("configured-private",), working_directory=tmp_path),
        ),
        command_resolver=resolve,
        discovery_factory=build_discovery,
        connection_factory=lambda _command: connection,
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    codex_checks = [check for check in checks if check.code.startswith("codex_")]
    by_code = {check.code: check for check in codex_checks}
    assert tuple(by_code) == (
        "codex_profile",
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
    assert by_code["codex_profile"] == DoctorCheck("codex_profile", "ok", "read_only")
    assert by_code["codex_command"] == DoctorCheck("codex_command", "ok", "available")
    assert by_code["codex_schema"] == DoctorCheck("codex_schema", "ok", "compatible")
    assert by_code["codex_account"] == DoctorCheck("codex_account", "ok", "authenticated")
    assert by_code["codex_policy"] == DoctorCheck(
        "codex_policy", "ok", "workspace_write_on_request"
    )
    assert by_code["codex_agent_admission"] == DoctorCheck("codex_agent_admission", "ok", "allowed")
    assert by_code["codex_local_review"] == DoctorCheck("codex_local_review", "ok", "available")
    assert by_code["codex_realtime"] == DoctorCheck("codex_realtime", "ok", "available")
    assert by_code["codex_interrupt"] == DoctorCheck("codex_interrupt", "ok", "available")
    assert by_code["codex_server_requests"] == DoctorCheck(
        "codex_server_requests", "ok", "discovered"
    )
    assert resolved == [("configured-private",)]
    assert discovery_arguments == [(CodexCommand(("fixture-codex-private",)), connection, tmp_path)]
    assert connection.start_calls == 1
    assert connection.close_calls == 1
    assert discovery.calls == 1
    assert "private" not in repr(checks)
    assert "private-version-value" not in repr(checks)


@pytest.mark.parametrize("profile", list(AgentProfileMode))
async def test_doctor_reports_selected_profile_even_when_codex_is_missing(
    tmp_path: Path,
    profile: AgentProfileMode,
) -> None:
    private_message = "private-command-path"

    def fail_resolution(_value: tuple[str, ...] | None) -> CodexCommand:
        raise CodexCommandError(private_message)

    checks = await run_doctor(
        MocoSettings(
            agent=AgentSettings(profile=profile),
            codex=CodexSettings(working_directory=tmp_path),
        ),
        command_resolver=fail_resolution,
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    assert DoctorCheck("codex_profile", "ok", profile.value) in checks
    assert private_message not in repr(checks)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
            DoctorCheck("codex_local_review", "ok", "available"),
        ),
        (
            CapabilityState(
                CapabilityStatus.VERSION_MISMATCH,
                "approval_categories_unavailable",
            ),
            DoctorCheck(
                "codex_local_review",
                "error",
                "approval_categories_unavailable",
            ),
        ),
        (
            CapabilityState(
                CapabilityStatus.VERSION_MISMATCH,
                "approval_family_unadaptable",
            ),
            DoctorCheck(
                "codex_local_review",
                "error",
                "approval_family_unadaptable",
            ),
        ),
        (
            CapabilityState(CapabilityStatus.ERROR, "private-review-detail"),
            DoctorCheck("codex_local_review", "error", "invalid_response"),
        ),
    ],
)
async def test_doctor_projects_local_review_readiness_with_bounded_codes(
    tmp_path: Path,
    state: CapabilityState,
    expected: DoctorCheck,
) -> None:
    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(
            make_snapshot(server_requests=state)
        ),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    assert expected in checks
    assert "private-review-detail" not in repr(checks)


@pytest.mark.parametrize(
    ("profile", "expected_admission"),
    [
        (
            AgentProfileMode.READ_ONLY,
            DoctorCheck("codex_agent_admission", "ok", "allowed"),
        ),
        (
            AgentProfileMode.WORKSPACE_WRITE,
            DoctorCheck("codex_agent_admission", "ok", "allowed"),
        ),
        (
            AgentProfileMode.INHERIT_CODEX,
            DoctorCheck("codex_agent_admission", "error", "unsafe_voice_policy"),
        ),
    ],
)
async def test_doctor_projects_unsafe_global_policy_by_selected_profile(
    tmp_path: Path,
    profile: AgentProfileMode,
    expected_admission: DoctorCheck,
) -> None:
    snapshot = make_snapshot(
        effective_policy=EffectivePolicy(
            SandboxMode.DANGER_FULL_ACCESS,
            ApprovalMode.NEVER,
        ),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
    )

    checks = await run_doctor(
        MocoSettings(
            agent=AgentSettings(profile=profile),
            codex=CodexSettings(working_directory=tmp_path),
        ),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_policy"] == DoctorCheck("codex_policy", "ok", "danger_full_access_never")
    assert by_code["codex_agent_admission"] == expected_admission
    assert by_code["codex_realtime"] == DoctorCheck("codex_realtime", "ok", "available")


async def test_doctor_rejects_unknown_policy_only_for_inherit_codex(tmp_path: Path) -> None:
    snapshot = make_snapshot(
        effective_policy=None,
        policy_state=CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response"),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
    )

    checks = await run_doctor(
        MocoSettings(
            agent=AgentSettings(profile=AgentProfileMode.INHERIT_CODEX),
            codex=CodexSettings(working_directory=tmp_path),
        ),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    assert DoctorCheck("codex_agent_admission", "error", "invalid_response") in checks


@pytest.mark.parametrize(
    ("snapshot", "expected_schema"),
    [
        (
            make_snapshot(
                version="private-probe-version",
                account=CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
                effective_policy=None,
                policy_state=CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
                managed_requirements=CapabilityState(
                    CapabilityStatus.ERROR,
                    "probe_failed",
                ),
                agent_admission=CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
                realtime=CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
                interrupt=CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
                server_requests=CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
            ),
            DoctorCheck("codex_schema", "error", "probe_failed"),
        ),
        (
            make_snapshot(
                version="",
                account=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
                effective_policy=None,
                policy_state=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
                managed_requirements=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
                agent_admission=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
                realtime=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
                interrupt=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
                server_requests=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "invalid_response",
                ),
            ),
            DoctorCheck("codex_schema", "error", "version_mismatch"),
        ),
    ],
)
async def test_doctor_projects_global_schema_failure_without_version_value(
    tmp_path: Path,
    snapshot: CapabilitySnapshot,
    expected_schema: DoctorCheck,
) -> None:
    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_schema"] == expected_schema
    assert snapshot.version not in repr(checks) or not snapshot.version


async def test_doctor_keeps_schema_compatible_for_one_capability_mismatch(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(
        account=CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response"),
    )

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_schema"] == DoctorCheck("codex_schema", "ok", "compatible")
    assert by_code["codex_account"] == DoctorCheck("codex_account", "error", "invalid_response")


@pytest.mark.parametrize(
    ("snapshot", "code", "expected"),
    [
        (
            make_snapshot(
                account=CapabilityState(
                    CapabilityStatus.AUTHENTICATION_REQUIRED,
                    "authentication_required",
                )
            ),
            "codex_account",
            DoctorCheck("codex_account", "blocked", "authentication_required"),
        ),
        (
            make_snapshot(realtime=CapabilityState(CapabilityStatus.DISABLED, "feature_disabled")),
            "codex_realtime",
            DoctorCheck("codex_realtime", "error", "feature_disabled"),
        ),
        (
            make_snapshot(
                realtime=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "method_unavailable",
                )
            ),
            "codex_realtime",
            DoctorCheck("codex_realtime", "error", "method_unavailable"),
        ),
        (
            make_snapshot(
                interrupt=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "method_unavailable",
                )
            ),
            "codex_interrupt",
            DoctorCheck("codex_interrupt", "error", "method_unavailable"),
        ),
        (
            make_snapshot(
                agent_admission=CapabilityState(
                    CapabilityStatus.VERSION_MISMATCH,
                    "agent_event_contract_unavailable",
                )
            ),
            "codex_agent_admission",
            DoctorCheck(
                "codex_agent_admission",
                "error",
                "agent_event_contract_unavailable",
            ),
        ),
        (
            make_snapshot(account=CapabilityState(CapabilityStatus.ERROR, "private-secret-detail")),
            "codex_account",
            DoctorCheck("codex_account", "error", "invalid_response"),
        ),
    ],
)
async def test_doctor_projects_only_bounded_capability_states(
    tmp_path: Path,
    snapshot: CapabilitySnapshot,
    code: str,
    expected: DoctorCheck,
) -> None:
    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code[code] == expected
    assert "private-secret-detail" not in repr(checks)


async def test_doctor_default_codex_pipeline_builds_each_boundary_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = CodexCommand(("fixture-codex",))
    contract = object()
    connections: list[FakeConnection] = []
    probed_commands: list[CodexCommand] = []
    discovery_arguments: list[tuple[object, Path, object]] = []

    class Supervisor(FakeConnection):
        def __init__(self, actual: CodexCommand) -> None:
            assert actual == command
            super().__init__()
            connections.append(self)

    class SchemaProbe:
        def __init__(self, actual: CodexCommand) -> None:
            probed_commands.append(actual)

        async def probe(self) -> object:
            return contract

    class RuntimeDiscovery:
        def __init__(
            self,
            rpc: object,
            *,
            working_directory: Path,
            contract: object,
        ) -> None:
            discovery_arguments.append((rpc, working_directory, contract))

        async def discover(self) -> CapabilitySnapshot:
            return make_snapshot()

    monkeypatch.setattr(doctor_module, "CodexConnectionSupervisor", Supervisor)
    monkeypatch.setattr(doctor_module, "CodexSchemaProbe", SchemaProbe)
    monkeypatch.setattr(doctor_module, "CapabilityDiscovery", RuntimeDiscovery)

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: command,
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    assert DoctorCheck("codex_schema", "ok", "compatible") in checks
    assert len(connections) == 1
    assert connections[0].start_calls == 1
    assert connections[0].close_calls == 1
    assert probed_commands == [command]
    assert discovery_arguments == [(connections[0], tmp_path, contract)]


async def test_doctor_blocks_all_snapshot_checks_when_command_is_missing(
    tmp_path: Path,
) -> None:
    private_message = "private-command-path"

    def fail_resolution(_value: tuple[str, ...] | None) -> CodexCommand:
        raise CodexCommandError(private_message)

    def reject_connection(_command: CodexCommand) -> FakeConnection:
        pytest.fail("missing command must not create a connection")

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=fail_resolution,
        connection_factory=reject_connection,
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    codex_checks = [check for check in checks if check.code.startswith("codex_")]

    assert codex_checks[0] == DoctorCheck("codex_profile", "ok", "read_only")
    assert codex_checks[1] == DoctorCheck("codex_command", "error", "unavailable")
    assert all(
        check.status == "blocked" and check.detail == "command_unavailable"
        for check in codex_checks[2:]
    )
    assert private_message not in repr(checks)


@pytest.mark.parametrize("failure_stage", ["cwd", "absolute"])
async def test_doctor_contains_default_working_directory_failure_and_continues(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_message = "private-working-directory"
    synthesizer = FakeSynthesizer()
    hotkey_calls: list[bool] = []

    class WorkingDirectory:
        def absolute(self) -> Path:
            raise PrivateFailureError(private_message)

    def fail_cwd() -> Path:
        if failure_stage == "cwd":
            raise PrivateFailureError(private_message)
        return cast("Path", WorkingDirectory())

    def reject_connection(_command: CodexCommand) -> FakeConnection:
        pytest.fail("working-directory failure must not create a connection")

    def probe_hotkey() -> bool:
        hotkey_calls.append(True)
        return True

    class FailingPath:
        cwd = staticmethod(fail_cwd)

    monkeypatch.setattr(doctor_module, "Path", FailingPath)

    checks = await run_doctor(
        MocoSettings(),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        connection_factory=reject_connection,
        synthesizer_factory=lambda _settings: synthesizer,
        hotkey_probe=probe_hotkey,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_command"] == DoctorCheck("codex_command", "ok", "available")
    assert all(
        by_code[code] == DoctorCheck(code, "error", "probe_failed")
        for code in (
            "codex_schema",
            "codex_account",
            "codex_policy",
            "codex_agent_admission",
            "codex_realtime",
            "codex_interrupt",
            "codex_server_requests",
        )
    )
    assert by_code["irodori_capabilities"] == DoctorCheck("irodori_capabilities", "ok", "ready")
    assert by_code["hotkeys"] == DoctorCheck("hotkeys", "ok", "available")
    assert synthesizer.closed
    assert hotkey_calls == [True]
    assert "PrivateFailureError" in caplog.text
    assert private_message not in caplog.text
    assert private_message not in repr(checks)


async def test_doctor_contains_discovery_and_cleanup_failure_by_type_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_message = "private-schema-path-and-token"
    connection = FakeConnection(close_error=PrivateFailureError(private_message))
    discovery = FakeCapabilityDiscovery(
        make_snapshot(),
        error=PrivateFailureError(private_message),
    )

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: discovery,
        connection_factory=lambda _command: connection,
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_command"] == DoctorCheck("codex_command", "ok", "available")
    assert all(
        by_code[code] == DoctorCheck(code, "error", "probe_failed")
        for code in (
            "codex_schema",
            "codex_account",
            "codex_policy",
            "codex_agent_admission",
            "codex_realtime",
            "codex_interrupt",
            "codex_server_requests",
        )
    )
    assert connection.close_calls == 1
    assert "PrivateFailureError" in caplog.text
    assert private_message not in caplog.text
    assert private_message not in repr(checks)


async def test_doctor_reports_stable_checks_without_sensitive_values(
    tmp_path: Path,
) -> None:
    settings = MocoSettings(
        codex=CodexSettings(command=("private-command",), working_directory=tmp_path),
    )
    connection = FakeConnection()
    capabilities = make_capabilities(3)
    synthesizer = FakeSynthesizer(capabilities)

    checks = await run_doctor(
        settings,
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        connection_factory=lambda _command: connection,
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(make_snapshot()),
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
        "codex_profile",
        "codex_command",
        "codex_schema",
        "codex_account",
        "codex_policy",
        "codex_agent_admission",
        "codex_local_review",
        "codex_realtime",
        "codex_interrupt",
        "codex_server_requests",
        "irodori_capabilities",
        "irodori_route",
        "irodori_synthesis",
        "hotkeys",
    }
    assert all(check.status == "ok" for check in checks)
    assert "private-command" not in rendered
    assert "private-version-value" not in rendered
    assert str(settings.irodori.base_url) not in rendered
    assert capabilities.generation not in rendered
    assert "voice_count" not in rendered
    assert all(voice.id not in rendered for voice in capabilities.voices)
    assert all(voice.label not in rendered for voice in capabilities.voices)
    assert all(alias not in rendered for voice in capabilities.voices for alias in voice.aliases)
    assert synthesizer.selected_voice_ids == [capabilities.voices[0].id]
    assert synthesizer.synthesized_texts == ["接続確認"]
    assert connection.close_calls == 1
    assert synthesizer.closed


async def test_doctor_reports_public_operator_boundary(tmp_path: Path) -> None:
    settings = MocoSettings.model_validate(
        {
            "server": {"public_url": "https://voice.example.com"},
            "codex": {
                "command": [str(tmp_path / "missing")],
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
                "command": [str(tmp_path / "missing")],
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
                "command": [str(tmp_path / "missing")],
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
                    command=(str(tmp_path / "missing"),),
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
            codex=CodexSettings(
                command=(str(tmp_path / "missing"),),
                working_directory=tmp_path,
            ),
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
            codex=CodexSettings(
                command=(str(tmp_path / "missing"),),
                working_directory=tmp_path,
            ),
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
            codex=CodexSettings(
                command=(str(tmp_path / "missing"),),
                working_directory=tmp_path,
            ),
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
            codex=CodexSettings(
                command=(str(tmp_path / "missing"),),
                working_directory=tmp_path,
            ),
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
    synthesizer = FakeSynthesizer(close_error=PrivateFailureError())
    connection = FakeConnection(
        start_error=PrivateFailureError("private probe"),
        close_error=PrivateFailureError("private cleanup"),
    )

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        connection_factory=lambda _command: connection,
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(make_snapshot()),
        synthesizer_factory=lambda _settings: cast(
            "DoctorSynthesizer",
            synthesizer,
        ),
        hotkey_probe=lambda: (_ for _ in ()).throw(InputDeniedError),
    )

    assert next(check for check in checks if check.code == "hotkeys").status == "error"
    assert connection.close_calls == 1
    assert "cleanup failed" in caplog.text
    assert "PrivateFailureError" in caplog.text
    assert "private probe" not in caplog.text
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


async def test_doctor_uses_host_specific_hotkey_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_module,
        "hotkey_unavailable_detail",
        lambda: "browser_hotkey_fallback",
        raising=False,
    )

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(working_directory=tmp_path)),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        connection_factory=lambda _command: FakeConnection(),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(make_snapshot()),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: False,
    )

    assert checks[-1] == DoctorCheck("hotkeys", "error", "browser_hotkey_fallback")
