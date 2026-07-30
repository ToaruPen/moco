from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import uvicorn
import yaml
from typer.testing import CliRunner

from moco import cli
from moco.cli import _is_safe_operator_url, _remove_state_file, _run_runtime, app
from moco.config import HotkeySettings, MocoSettings, load_config
from moco.doctor import DoctorCheck
from moco.service.launchd import LaunchdError, ServiceStatus

runner = CliRunner()


def test_config_init_is_non_destructive_and_generated_yaml_validates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "moco.yaml"

    first = runner.invoke(app, ["config", "init", "--path", str(path)])
    second = runner.invoke(app, ["config", "init", "--path", str(path)])

    assert first.exit_code == 0
    assert second.exit_code == 1
    assert "exists" in second.output
    assert load_config(path).hotkeys.push_to_talk == "f1"
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


def test_open_uses_private_state_without_printing_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime.json"
    capability_value = "private-capability"
    state_path.write_text(
        json.dumps({"url": f"http://127.0.0.1:8765/#{capability_value}"}),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    opened: list[str] = []
    monkeypatch.setattr("moco.cli.webbrowser.open", opened.append)

    result = runner.invoke(app, ["open", "--state-path", str(state_path)])

    assert result.exit_code == 0
    assert capability_value not in result.output
    assert opened == [f"http://127.0.0.1:8765/#{capability_value}"]


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
    ("content", "mode"),
    [
        ('{"url":"http://example.com/#token"}', 0o600),
        ('{"url":42}', 0o600),
        ("not-json", 0o600),
        ('{"url":"http://127.0.0.1/#token"}', 0o644),
    ],
)
def test_open_rejects_unsafe_or_unreadable_runtime_state(
    tmp_path: Path,
    content: str,
    mode: int,
) -> None:
    state_path = tmp_path / "runtime.json"
    state_path.write_text(content, encoding="utf-8")
    state_path.chmod(mode)

    result = runner.invoke(app, ["open", "--state-path", str(state_path)])

    assert result.exit_code == 1
    assert "no safe running moco" in result.output


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


async def test_runtime_writes_private_capability_state_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = FakeTelemetry()
    listener = FakeHotkeyListener()
    server = FakeServer()
    removed_payloads: list[dict[str, str]] = []
    operator_app = SimpleNamespace(
        state=SimpleNamespace(
            control_hub=SimpleNamespace(publish=lambda _control: None),
        ),
    )

    def build_operator_app(
        _settings: MocoSettings,
        *,
        capability_token: str,
    ) -> SimpleNamespace:
        del capability_token
        return operator_app

    monkeypatch.setattr(cli, "configure_telemetry", lambda _settings: telemetry)
    monkeypatch.setattr(cli, "create_app", build_operator_app)
    monkeypatch.setattr(cli, "GlobalHotkeyListener", lambda **_kwargs: listener)
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)

    def capture_state(path: Path) -> None:
        removed_payloads.append(
            cast("dict[str, str]", json.loads(path.read_text(encoding="utf-8"))),
        )
        path.unlink()

    monkeypatch.setattr(cli, "_remove_state_file", capture_state)
    state_path = tmp_path / "runtime.json"

    await _run_runtime(
        MocoSettings(hotkeys=HotkeySettings(enabled=False)),
        state_path=state_path,
    )

    assert removed_payloads[0]["url"].startswith("http://127.0.0.1:")
    assert _is_safe_operator_url(removed_payloads[0]["url"])
    assert telemetry.closed
    assert listener.stopped
    assert not state_path.exists()


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
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)

    await _run_runtime(MocoSettings(), state_path=tmp_path / "runtime.json")

    assert operator_app.state.global_hotkeys_active is False
    assert "Input Monitoring permission may be required" in capsys.readouterr().out


def test_remove_state_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    _remove_state_file(path)
    path.write_text("{}", encoding="utf-8")
    _remove_state_file(path)

    assert not path.exists()
