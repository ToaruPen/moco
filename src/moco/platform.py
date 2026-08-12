from __future__ import annotations

import os
import shutil
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from moco.errors import CodexCommandError, HostPlatformError

_DEFAULT_CODEX_BUNDLE = Path("/Applications/ChatGPT.app/Contents/Resources/codex")

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True, slots=True)
class CodexCommand:
    argv: tuple[str, ...]

    def app_server_argv(self) -> tuple[str, ...]:
        return (
            *self.argv,
            "app-server",
            "--listen",
            "stdio://",
            "--enable",
            "realtime_conversation",
        )

    def version_argv(self) -> tuple[str, ...]:
        return (*self.argv, "--version")

    def schema_argv(self, output: Path, *, experimental: bool) -> tuple[str, ...]:
        arguments = (
            *self.argv,
            "app-server",
            "generate-json-schema",
            "--out",
            str(output),
        )
        return (*arguments, "--experimental") if experimental else arguments


def default_config_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_value = platform_name or sys.platform
    values = os.environ if environ is None else environ
    if platform_value == "win32":
        return _required_environment_path(values, "APPDATA") / "moco" / "moco.yaml"
    home = _home_directory(values)
    return home / "Library" / "Application Support" / "moco" / "moco.yaml"


def default_prompt_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_value = platform_name or sys.platform
    values = os.environ if environ is None else environ
    if platform_value == "win32":
        return _required_environment_path(values, "APPDATA") / "moco" / "prompt.md"
    home = _home_directory(values)
    return home / ".moco" / "prompt.md"


def default_runtime_state_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_value = platform_name or sys.platform
    values = os.environ if environ is None else environ
    if platform_value == "win32":
        root = _required_environment_path(values, "LOCALAPPDATA") / "moco" / "runtime-private"
    else:
        home = _home_directory(values)
        root = home / "Library" / "Application Support" / "moco"
    return root / "runtime.json"


def resolve_codex_command(
    configured: tuple[str, ...] | None,
    *,
    platform_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> CodexCommand:
    platform_value = platform_name or sys.platform
    if configured is not None:
        if not configured:
            message = "configured Codex command is unavailable"
            raise CodexCommandError(message)
        executable = _resolve_executable(configured[0], which=which)
        if executable is None:
            message = "configured Codex command is unavailable"
            raise CodexCommandError(message)
        return CodexCommand((executable, *configured[1:]))

    discovered = which("codex")
    if discovered is not None:
        return CodexCommand((discovered,))

    if (
        platform_value == "darwin"
        and _DEFAULT_CODEX_BUNDLE.is_file()
        and os.access(_DEFAULT_CODEX_BUNDLE, os.X_OK)
    ):
        return CodexCommand((str(_DEFAULT_CODEX_BUNDLE),))
    message = "Codex command is unavailable"
    raise CodexCommandError(message)


def open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url)
    except (webbrowser.Error, OSError):
        return False


def service_supported(*, platform_name: str | None = None) -> bool:
    return (platform_name or sys.platform) == "darwin"


def hotkey_unavailable_detail(*, platform_name: str | None = None) -> str:
    return (
        "browser_hotkey_fallback"
        if (platform_name or sys.platform) == "win32"
        else "input_monitoring_required"
    )


def _required_environment_path(values: Mapping[str, str], name: str) -> Path:
    raw = values.get(name)
    if raw is None or not raw.strip():
        message = f"{name} is unavailable"
        raise HostPlatformError(message)
    return Path(raw)


def _home_directory(values: Mapping[str, str]) -> Path:
    raw = values.get("HOME")
    if raw is None:
        return Path.home()
    if not raw.strip():
        message = "HOME is unavailable"
        raise HostPlatformError(message)
    return Path(raw)


def _resolve_executable(
    value: str,
    *,
    which: Callable[[str], str | None],
) -> str | None:
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return which(value)
