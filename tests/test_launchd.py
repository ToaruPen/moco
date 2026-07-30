from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from moco.service.launchd import (
    LABEL,
    LaunchdError,
    ServiceStatus,
    install_service,
    read_service_status,
    uninstall_service,
)


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
