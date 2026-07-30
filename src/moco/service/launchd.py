from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

LABEL = "dev.toarupen.moco"
_LAUNCHCTL = "/bin/launchctl"


class LaunchdError(RuntimeError):
    """A launchd operation failed its ownership or process boundary."""


class ServiceStatus(StrEnum):
    MISSING = "missing"
    STOPPED = "stopped"
    RUNNING = "running"


def default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def default_log_directory() -> Path:
    return Path.home() / "Library" / "Logs" / "moco"


def install_service(
    *,
    executable: Path,
    config_path: Path,
    plist_path: Path | None = None,
    log_directory: Path | None = None,
) -> Path:
    if not executable.is_absolute() or not config_path.is_absolute():
        message = "service executable and configuration paths must be absolute"
        raise LaunchdError(message)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        message = "service executable is unavailable"
        raise LaunchdError(message)
    target = plist_path or default_plist_path()
    logs = log_directory or default_log_directory()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            str(executable),
            "run",
            "--config",
            str(config_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str((logs / "stdout.log").absolute()),
        "StandardErrorPath": str((logs / "stderr.log").absolute()),
        "ProcessType": "Interactive",
    }
    _atomic_write(target, plistlib.dumps(payload, sort_keys=True))
    return target


def uninstall_service(
    *,
    executable: Path,
    plist_path: Path | None = None,
) -> None:
    target = plist_path or default_plist_path()
    if not target.exists():
        return
    try:
        payload = plistlib.loads(target.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        message = "refusing to remove an unreadable launchd plist"
        raise LaunchdError(message) from error
    arguments = payload.get("ProgramArguments")
    owned = (
        payload.get("Label") == LABEL
        and isinstance(arguments, list)
        and bool(arguments)
        and arguments[0] == str(executable)
    )
    if not owned:
        message = "refusing to remove a launchd plist not owned by moco"
        raise LaunchdError(message)
    target.unlink()


def read_service_status(
    plist_path: Path | None = None,
    *,
    launchctl_running: bool | None = None,
) -> ServiceStatus:
    target = plist_path or default_plist_path()
    if not target.exists():
        return ServiceStatus.MISSING
    running = _launchctl_is_running() if launchctl_running is None else launchctl_running
    return ServiceStatus.RUNNING if running else ServiceStatus.STOPPED


def start_service(plist_path: Path | None = None) -> None:
    target = plist_path or default_plist_path()
    _run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(target)])
    _run_launchctl(["kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"])


def stop_service() -> None:
    _run_launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"])


def _launchctl_is_running() -> bool:
    completed = subprocess.run(  # noqa: S603
        [_LAUNCHCTL, "print", f"gui/{os.getuid()}/{LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _run_launchctl(arguments: Sequence[str]) -> None:
    completed = subprocess.run(  # noqa: S603
        [_LAUNCHCTL, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = "launchctl operation failed"
        raise LaunchdError(message)


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
