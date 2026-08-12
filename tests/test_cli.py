from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import uvicorn
import yaml
from typer.testing import CliRunner

from moco import cli
from moco import config as config_module
from moco.cli import _is_safe_operator_url, _run_runtime, app
from moco.config import MocoSettings, load_config
from moco.doctor import DoctorCheck
from moco.errors import PrivateStateError
from moco.runtime.private_state import (
    PrivateStateIdentity,
)
from moco.service.launchd import LaunchdError, ServiceStatus

runner = CliRunner()


def _patch_private_state_boundary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial: dict[Path, bytes] | None = None,
) -> tuple[
    dict[Path, bytes],
    list[tuple[Path, bytes]],
    Callable[[Path, bytes], PrivateStateIdentity],
]:
    contents = dict(initial or {})
    identities: dict[Path, PrivateStateIdentity] = {}
    writes: list[tuple[Path, bytes]] = []

    def write(path: Path, content: bytes) -> PrivateStateIdentity:
        identity = cast("PrivateStateIdentity", object())
        contents[path] = content
        identities[path] = identity
        writes.append((path, content))
        return identity

    def read(path: Path) -> bytes:
        try:
            return contents[path]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def remove(path: Path, *, expected_identity: PrivateStateIdentity | None = None) -> None:
        if expected_identity is None or identities.get(path) is expected_identity:
            contents.pop(path, None)
            identities.pop(path, None)

    @contextmanager
    def lease(_path: Path) -> Iterator[None]:
        yield

    monkeypatch.setattr(cli, "write_private_state", write)
    monkeypatch.setattr(cli, "read_private_state", read)
    monkeypatch.setattr(cli, "remove_private_state", remove)
    monkeypatch.setattr(cli, "hold_private_runtime_lease", lease, raising=False)
    return contents, writes, write


def test_config_init_is_non_destructive_and_generated_yaml_validates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "moco.yaml"

    first = runner.invoke(app, ["config", "init", "--path", str(path)])
    second = runner.invoke(app, ["config", "init", "--path", str(path)])

    assert first.exit_code == 0
    assert second.exit_code == 1
    assert "exists" in second.output
    assert load_config(path).hotkeys.start_listening == "f1"
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)

    forced = runner.invoke(
        app,
        ["config", "init", "--path", str(path), "--force"],
    )
    assert forced.exit_code == 0


def test_config_validate_and_public_command_surface(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "validate", "--path", str(path)])
    help_result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "valid" in result.output
    for command in ["config", "doctor", "run", "open", "service"]:
        assert command in help_result.output


def test_open_delegates_to_platform_browser_without_printing_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    capability_value = "private-capability"
    payload = json.dumps(
        {
            "version": 1,
            "url": f"http://127.0.0.1:8765/#{capability_value}",
            "mobile_url": f"https://voice.example.com/#{capability_value}",
            "control_secret": "private-control-secret",
        }
    ).encode()
    _patch_private_state_boundary(
        monkeypatch,
        initial={state_path: payload},
    )
    opened: list[str] = []

    def open_successfully(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(cli, "open_browser", open_successfully, raising=False)
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)

    result = runner.invoke(app, ["open"])

    assert result.exit_code == 0
    assert capability_value not in result.output
    assert opened == [f"http://127.0.0.1:8765/#{capability_value}"]


def test_open_reports_bounded_browser_failure_without_exposing_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    capability_value = "private-browser-capability"
    _patch_private_state_boundary(
        monkeypatch,
        initial={
            state_path: json.dumps(
                {
                    "version": 1,
                    "url": f"http://127.0.0.1:8765/#{capability_value}",
                    "control_secret": "private-control-secret",
                }
            ).encode()
        },
    )
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)
    monkeypatch.setattr(cli, "open_browser", lambda _url: False, raising=False)

    result = runner.invoke(app, ["open"])

    assert result.exit_code == 1
    assert result.output == "ERROR [browser]: unavailable\n"
    assert capability_value not in result.output
    assert "operator page opened" not in result.output


def test_config_validate_reports_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("unknown: true\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "validate", "--path", str(path)])

    assert result.exit_code == 1
    assert "ERROR [configuration]" in result.output


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765/#token",
        "http://example.com:8765/#token",
        "http://user@127.0.0.1:8765/#token",
        "http://127.0.0.1:8765/?query=1#token",
        "http://127.0.0.1:8765/",
    ],
)
def test_operator_url_validation_rejects_unsafe_urls(url: str) -> None:
    assert not _is_safe_operator_url(url)


@pytest.mark.parametrize(
    "content",
    [
        '{"url":"http://example.com/#token"}',
        '{"url":42}',
        "not-json",
    ],
)
def test_open_rejects_unsafe_or_unreadable_runtime_state(
    tmp_path: Path,
    content: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    _patch_private_state_boundary(
        monkeypatch,
        initial={state_path: content.encode()},
    )
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)

    result = runner.invoke(app, ["open"])

    assert result.exit_code == 1
    assert "no safe running moco" in result.output


def test_open_rejects_private_state_boundary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"

    def reject_read(_path: Path) -> bytes:
        message = "private state permissions are not private"
        raise PrivateStateError(message)

    monkeypatch.setattr(cli, "read_private_state", reject_read)
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)

    result = runner.invoke(app, ["open"])

    assert result.exit_code == 1
    assert result.output == "ERROR [runtime_state]: no safe running moco instance was found\n"


def test_run_and_open_hide_state_path_and_resolve_default_at_command_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "moco.yaml"
    config.write_text("{}\n", encoding="utf-8")
    expected = tmp_path / "private" / "runtime.json"
    seen: list[Path] = []

    async def fake_runtime(_settings: MocoSettings, *, state_path: Path) -> None:
        seen.append(state_path)

    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: expected)
    monkeypatch.setattr(cli, "_run_runtime", fake_runtime)

    result = runner.invoke(app, ["run", "--config", str(config)])
    run_help = runner.invoke(app, ["run", "--help"])
    open_help = runner.invoke(app, ["open", "--help"])
    rejected_run = runner.invoke(app, ["run", "--state-path", str(expected)])
    rejected_open = runner.invoke(app, ["open", "--state-path", str(expected)])

    assert result.exit_code == 0
    assert seen == [expected]
    assert "state-path" not in run_help.output
    assert "state-path" not in open_help.output
    assert rejected_run.exit_code != 0
    assert rejected_open.exit_code != 0


def test_config_init_uses_config_writer_not_private_runtime_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "moco.yaml"

    def reject_runtime_writer(*_args: object, **_kwargs: object) -> None:
        pytest.fail("config init must not use the runtime-private writer")

    monkeypatch.setattr(cli, "write_private_state", reject_runtime_writer)

    result = runner.invoke(app, ["config", "init", "--path", str(path)])

    assert result.exit_code == 0
    assert load_config(path) == MocoSettings()


def test_config_init_does_not_use_posix_fchmod_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config" / "moco.yaml"
    monkeypatch.setattr(sys, "platform", "win32")

    def reject_fchmod(*_args: object) -> None:
        pytest.fail("Windows config writing must not call POSIX fchmod")

    monkeypatch.setattr(os, "fchmod", reject_fchmod)
    protected: list[Path] = []
    monkeypatch.setattr(
        config_module,
        "_protect_windows_config_path",
        protected.append,
        raising=False,
    )
    monkeypatch.setattr(
        config_module,
        "_validate_windows_config_path",
        lambda _path: None,
        raising=False,
    )

    @contextmanager
    def namespace(_path: Path, *, platform_name: str) -> Iterator[None]:
        assert platform_name == "win32"
        yield

    monkeypatch.setattr(config_module, "_config_namespace", namespace)

    result = runner.invoke(app, ["config", "init", "--path", str(path)])

    assert result.exit_code == 0
    assert path.parent in protected
    assert len(protected) == 2
    assert load_config(path) == MocoSettings()


def test_config_init_does_not_repair_an_unsafe_windows_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config" / "moco.yaml"
    path.parent.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    protected: list[Path] = []
    monkeypatch.setattr(config_module, "_protect_windows_config_path", protected.append)

    def reject(_path: Path) -> None:
        message = "unsafe inherited access"
        raise PrivateStateError(message)

    monkeypatch.setattr(config_module, "_validate_windows_config_path", reject)

    result = runner.invoke(app, ["config", "init", "--path", str(path)])

    assert result.exit_code == 1
    assert "security requirements" in result.output
    assert protected == []
    assert not path.exists()


def test_doctor_command_reports_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "moco.yaml"
    config.write_text("{}\n", encoding="utf-8")
    responses = [
        [DoctorCheck("example", "ok", "available")],
        [DoctorCheck("example", "error", "unavailable")],
    ]

    async def fake_doctor(
        _settings: MocoSettings,
        *,
        synthesize: str | None,
    ) -> list[DoctorCheck]:
        assert synthesize == "確認"
        return responses.pop(0)

    monkeypatch.setattr(cli, "run_doctor", fake_doctor)

    success = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--synthesize", "確認"],
    )
    failure = runner.invoke(
        app,
        ["doctor", "--config", str(config), "--synthesize", "確認"],
    )

    assert success.exit_code == 0
    assert "[OK] example: available" in success.output
    assert failure.exit_code == 1
    assert "[ERROR] example: unavailable" in failure.output


def test_run_command_rejects_invalid_configuration(tmp_path: Path) -> None:
    config = tmp_path / "moco.yaml"
    config.write_text("unknown: true\n", encoding="utf-8")

    result = runner.invoke(app, ["run", "--config", str(config)])

    assert result.exit_code == 1
    assert "ERROR [configuration]" in result.output


def test_run_command_reports_runtime_lease_failure_without_private_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "moco.yaml"
    config.write_text("{}\n", encoding="utf-8")
    private_detail = "private-runtime-owner"

    async def fail_runtime(_settings: MocoSettings, *, state_path: Path) -> None:
        del state_path
        raise PrivateStateError(private_detail)

    monkeypatch.setattr(cli, "_run_runtime", fail_runtime)

    result = runner.invoke(app, ["run", "--config", str(config)])

    assert result.exit_code == 1
    assert result.output == "ERROR [runtime_state]: unavailable\n"
    assert private_detail not in result.output


def test_service_commands_delegate_without_exposing_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "moco"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[str] = []

    def install(**_kwargs: object) -> Path:
        calls.append("install")
        return tmp_path / "agent.plist"

    monkeypatch.setattr(cli, "install_service", install)
    monkeypatch.setattr(cli, "service_supported", lambda: True)
    monkeypatch.setattr(cli, "start_service", lambda: calls.append("start"))
    monkeypatch.setattr(cli, "stop_service", lambda: calls.append("stop"))
    monkeypatch.setattr(
        cli,
        "read_service_status",
        lambda: ServiceStatus.RUNNING,
    )
    monkeypatch.setattr(
        cli,
        "uninstall_service",
        lambda **_kwargs: calls.append("uninstall"),
    )

    results = [
        runner.invoke(
            app,
            ["service", "install", "--executable", str(executable)],
        ),
        runner.invoke(app, ["service", "start"]),
        runner.invoke(app, ["service", "stop"]),
        runner.invoke(app, ["service", "status"]),
        runner.invoke(
            app,
            ["service", "uninstall", "--executable", str(executable)],
        ),
    ]

    assert all(result.exit_code == 0 for result in results)
    assert calls == ["install", "start", "stop", "uninstall"]
    assert "running" in results[3].output


@pytest.mark.parametrize("command", ["install", "start", "stop", "status", "uninstall"])
def test_unsupported_service_commands_never_call_launchd(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "moco"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    def reject_launchd(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unsupported host must not call launchd")

    monkeypatch.setattr(cli, "service_supported", lambda: False, raising=False)
    monkeypatch.setattr(cli, "install_service", reject_launchd)
    monkeypatch.setattr(cli, "start_service", reject_launchd)
    monkeypatch.setattr(cli, "stop_service", reject_launchd)
    monkeypatch.setattr(cli, "read_service_status", reject_launchd)
    monkeypatch.setattr(cli, "uninstall_service", reject_launchd)
    arguments = ["service", command]
    if command in {"install", "uninstall"}:
        arguments.extend(["--executable", str(executable)])

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1
    assert "ERROR [service]: unsupported_platform" in result.output


@pytest.mark.parametrize("command", ["install", "start", "stop", "uninstall"])
def test_service_commands_map_launchd_errors(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticLaunchdError(LaunchdError):
        """Synthetic launchd failure."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise SyntheticLaunchdError

    monkeypatch.setattr(cli, f"{command}_service", fail)
    monkeypatch.setattr(cli, "service_supported", lambda: True)
    arguments = ["service", command]
    if command in {"install", "uninstall"}:
        executable = tmp_path / "moco"
        executable.write_text("", encoding="utf-8")
        arguments.extend(["--executable", str(executable)])

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1
    assert "ERROR [service]" in result.output


class FakeTelemetry:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeHotkeyListener:
    def __init__(self, *, running: bool = False, **_kwargs: object) -> None:
        self.running = running
        self.stopped = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True


class FakeServer:
    def __init__(self) -> None:
        self.started = False

    async def serve(self) -> None:
        self.started = True


class NeverStartedServer:
    def __init__(self) -> None:
        self.started = False

    async def serve(self) -> None:
        return None


def _patch_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    server: FakeServer | NeverStartedServer,
) -> None:
    operator_app = SimpleNamespace(
        state=SimpleNamespace(
            control_hub=SimpleNamespace(publish=lambda _control: None),
        ),
    )
    monkeypatch.setattr(cli, "configure_telemetry", lambda _settings: FakeTelemetry())
    monkeypatch.setattr(cli, "create_app", lambda *_args, **_kwargs: operator_app)
    monkeypatch.setattr(cli, "GlobalHotkeyListener", FakeHotkeyListener)
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)


async def test_runtime_writes_private_capability_state_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = FakeTelemetry()
    listener = FakeHotkeyListener()
    server = FakeServer()
    removed_payloads: list[dict[str, object]] = []
    operator_app = SimpleNamespace(
        state=SimpleNamespace(
            control_hub=SimpleNamespace(publish=lambda _control: None),
        ),
    )

    def build_operator_app(
        _settings: MocoSettings,
        *,
        capability_token: str,
        control_secret: str,
    ) -> SimpleNamespace:
        del capability_token, control_secret
        return operator_app

    monkeypatch.setattr(cli, "configure_telemetry", lambda _settings: telemetry)
    monkeypatch.setattr(cli, "create_app", build_operator_app)
    monkeypatch.setattr(cli, "GlobalHotkeyListener", lambda **_kwargs: listener)
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)
    state_path = tmp_path / "runtime-private" / "runtime.json"
    contents, writes, _write = _patch_private_state_boundary(monkeypatch)

    settings = MocoSettings.model_validate(
        {
            "hotkeys": {"enabled": False},
            "server": {"public_url": "https://voice.example.com"},
        },
    )
    await _run_runtime(settings, state_path=state_path)
    removed_payloads.append(cast("dict[str, object]", json.loads(writes[-1][1])))

    local_url = cast("str", removed_payloads[0]["url"])
    mobile_url = cast("str", removed_payloads[0]["mobile_url"])
    assert local_url.startswith("http://127.0.0.1:")
    assert mobile_url.startswith("https://voice.example.com/#")
    assert local_url.split("#", 1)[1] == mobile_url.split("#", 1)[1]
    assert _is_safe_operator_url(local_url)
    assert telemetry.closed
    assert listener.stopped
    assert state_path not in contents


async def test_runtime_holds_exclusive_lease_across_state_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    server = FakeServer()
    _patch_runtime_dependencies(monkeypatch, server)
    state_path = tmp_path / "runtime-private" / "runtime.json"
    identity = cast("PrivateStateIdentity", object())

    @contextmanager
    def lease(path: Path) -> Iterator[None]:
        assert path == state_path
        events.append("lease-enter")
        try:
            yield
        finally:
            events.append("lease-exit")

    def write(path: Path, _content: bytes) -> PrivateStateIdentity:
        assert path == state_path
        events.append("write")
        return identity

    def remove(
        path: Path,
        *,
        expected_identity: PrivateStateIdentity | None = None,
    ) -> None:
        assert path == state_path
        assert expected_identity is identity
        events.append("remove")

    monkeypatch.setattr(cli, "hold_private_runtime_lease", lease, raising=False)
    monkeypatch.setattr(cli, "write_private_state", write)
    monkeypatch.setattr(cli, "remove_private_state", remove)

    await _run_runtime(MocoSettings(), state_path=state_path)

    assert events == ["lease-enter", "write", "remove", "lease-exit"]


async def test_runtime_start_failure_preserves_preexisting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    contents, _writes, _write = _patch_private_state_boundary(
        monkeypatch,
        initial={state_path: b"preexisting"},
    )
    _patch_runtime_dependencies(monkeypatch, NeverStartedServer())

    await _run_runtime(MocoSettings(), state_path=state_path)

    assert contents[state_path] == b"preexisting"


async def test_runtime_write_failure_preserves_preexisting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    contents, _writes, _write = _patch_private_state_boundary(
        monkeypatch,
        initial={state_path: b"preexisting"},
    )
    _patch_runtime_dependencies(monkeypatch, FakeServer())

    def fail_write(*_args: object, **_kwargs: object) -> PrivateStateIdentity:
        msg = "runtime state could not be written"
        raise PrivateStateError(msg)

    monkeypatch.setattr(cli, "write_private_state", fail_write)

    with pytest.raises(PrivateStateError):
        await _run_runtime(MocoSettings(), state_path=state_path)

    assert contents[state_path] == b"preexisting"


async def test_runtime_cleanup_preserves_state_replaced_after_its_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    _patch_runtime_dependencies(monkeypatch, FakeServer())
    contents, _writes, original_write = _patch_private_state_boundary(monkeypatch)

    def write_then_replace(path: Path, content: bytes) -> PrivateStateIdentity:
        owned = original_write(path, content)
        original_write(path, b"replacement")
        return owned

    monkeypatch.setattr(cli, "write_private_state", write_then_replace)

    await _run_runtime(MocoSettings(), state_path=state_path)

    assert contents[state_path] == b"replacement"


async def test_runtime_enables_browser_fallback_when_global_listener_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_app = SimpleNamespace(
        state=SimpleNamespace(
            control_hub=SimpleNamespace(publish=lambda _control: None),
            global_hotkeys_active=True,
        ),
    )
    listener = FakeHotkeyListener(running=False)
    server = FakeServer()

    monkeypatch.setattr(cli, "configure_telemetry", lambda _settings: FakeTelemetry())
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda *_args, **_kwargs: operator_app,
    )
    monkeypatch.setattr(cli, "GlobalHotkeyListener", lambda **_kwargs: listener)
    monkeypatch.setattr(cli, "hotkey_unavailable_detail", lambda: "input_monitoring_required")
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)
    _patch_private_state_boundary(monkeypatch)

    await _run_runtime(
        MocoSettings(),
        state_path=tmp_path / "runtime-private" / "runtime.json",
    )

    assert operator_app.state.global_hotkeys_active is False
    assert "input_monitoring_required" in capsys.readouterr().out


async def test_windows_runtime_warning_uses_browser_fallback_not_input_monitoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    listener = FakeHotkeyListener(running=False)
    server = FakeServer()
    operator_app = SimpleNamespace(
        state=SimpleNamespace(
            control_hub=SimpleNamespace(publish=lambda _control: None),
            global_hotkeys_active=True,
        ),
    )
    monkeypatch.setattr(cli, "configure_telemetry", lambda _settings: FakeTelemetry())
    monkeypatch.setattr(cli, "create_app", lambda *_args, **_kwargs: operator_app)
    monkeypatch.setattr(cli, "GlobalHotkeyListener", lambda **_kwargs: listener)
    monkeypatch.setattr(
        cli,
        "hotkey_unavailable_detail",
        lambda: "browser_hotkey_fallback",
        raising=False,
    )
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)
    _patch_private_state_boundary(monkeypatch)

    await _run_runtime(
        MocoSettings(),
        state_path=tmp_path / "runtime-private" / "runtime.json",
    )

    output = capsys.readouterr().out
    assert "browser_hotkey_fallback" in output
    assert "Input Monitoring" not in output
