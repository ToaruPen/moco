from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from moco.service import launchd
from moco.service.launchd import (
    LABEL,
    LaunchdError,
    ServiceStatus,
    install_service,
    read_service_status,
    start_service,
    stop_service,
    uninstall_service,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="launchd is unsupported on Windows")


def test_install_writes_exact_user_launch_agent(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "moco"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    config = tmp_path / "config" / "moco.yaml"
    config.parent.mkdir()
    config.write_text("{}\n", encoding="utf-8")
    plist_path = tmp_path / "LaunchAgents" / f"{LABEL}.plist"

    install_service(
        executable=executable,
        config_path=config,
        plist_path=plist_path,
        log_directory=tmp_path / "Logs",
    )

    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == LABEL
    assert payload["ProgramArguments"] == [
        str(executable),
        "run",
        "--config",
        str(config),
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert Path(payload["StandardOutPath"]).is_absolute()
    assert Path(payload["StandardErrorPath"]).is_absolute()
    assert plist_path.stat().st_mode & 0o077 == 0


def test_uninstall_refuses_foreign_or_modified_plist(tmp_path: Path) -> None:
    plist_path = tmp_path / "agent.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.example.foreign",
                "ProgramArguments": [str(tmp_path / "foreign")],
            },
        ),
    )

    with pytest.raises(LaunchdError, match="refusing"):
        uninstall_service(
            executable=Path("/absolute/moco"),
            plist_path=plist_path,
        )

    assert plist_path.exists()


def test_status_distinguishes_missing_stopped_and_running(tmp_path: Path) -> None:
    plist_path = tmp_path / "agent.plist"
    assert read_service_status(plist_path, launchctl_running=False) is ServiceStatus.MISSING

    plist_path.write_bytes(plistlib.dumps({"Label": LABEL}))
    assert read_service_status(plist_path, launchctl_running=False) is ServiceStatus.STOPPED
    assert read_service_status(plist_path, launchctl_running=True) is ServiceStatus.RUNNING


@pytest.mark.parametrize(
    ("executable", "config"),
    [
        (Path("moco"), Path("/absolute/moco.yaml")),
        (Path("/absolute/moco"), Path("moco.yaml")),
    ],
)
def test_install_requires_absolute_paths(
    executable: Path,
    config: Path,
) -> None:
    with pytest.raises(LaunchdError, match="absolute"):
        install_service(executable=executable, config_path=config)


def test_install_requires_executable_file(tmp_path: Path) -> None:
    with pytest.raises(LaunchdError, match="unavailable"):
        install_service(
            executable=tmp_path / "missing",
            config_path=tmp_path / "moco.yaml",
        )


def test_uninstall_owned_service_and_missing_service_are_safe(tmp_path: Path) -> None:
    executable = Path("/absolute/moco")
    plist_path = tmp_path / "agent.plist"
    uninstall_service(executable=executable, plist_path=plist_path)

    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [str(executable), "run"],
            },
        ),
    )
    uninstall_service(executable=executable, plist_path=plist_path)

    assert not plist_path.exists()


@pytest.mark.parametrize("payload", [b"not a plist", plistlib.dumps({"Label": LABEL})])
def test_uninstall_refuses_unreadable_or_incomplete_plist(
    tmp_path: Path,
    payload: bytes,
) -> None:
    plist_path = tmp_path / "agent.plist"
    plist_path.write_bytes(payload)

    with pytest.raises(LaunchdError, match="refusing"):
        uninstall_service(executable=Path("/absolute/moco"), plist_path=plist_path)


def test_launchctl_commands_and_live_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plist_path = tmp_path / "agent.plist"
    plist_path.write_bytes(plistlib.dumps({"Label": LABEL}))
    calls: list[list[str]] = []

    def successful_run(
        arguments: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert not check
        assert capture_output
        assert text
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", successful_run)

    assert read_service_status(plist_path) is ServiceStatus.RUNNING
    start_service(plist_path)
    stop_service()

    assert [call[1] for call in calls] == ["print", "bootstrap", "kickstart", "bootout"]


def test_launchctl_failure_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_run(
        arguments: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        return subprocess.CompletedProcess(arguments, 1, "", "private error")

    monkeypatch.setattr(subprocess, "run", failed_run)

    with pytest.raises(LaunchdError, match="operation failed"):
        start_service(tmp_path / "agent.plist")


def test_default_paths_are_user_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = Path("/Users/tester")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    assert launchd.default_plist_path() == (
        fake_home / "Library/LaunchAgents/dev.toarupen.moco.plist"
    )
    assert launchd.default_log_directory() == fake_home / "Library/Logs/moco"
