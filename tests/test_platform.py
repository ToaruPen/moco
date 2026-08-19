from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from webbrowser import Error as BrowserError

import pytest

import moco.platform as platform_module
from moco.errors import CodexCommandError, HostPlatformError
from moco.platform import (
    CodexCommand,
    default_config_path,
    default_prompt_path,
    default_runtime_state_path,
    hotkey_unavailable_detail,
    open_browser,
    resolve_codex_command,
    service_supported,
)


def test_windows_paths_use_roaming_and_private_local_state() -> None:
    environ = {
        "APPDATA": r"C:\Users\voice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\voice\AppData\Local",
    }

    roaming = Path(environ["APPDATA"])
    local = Path(environ["LOCALAPPDATA"])
    assert default_config_path(platform_name="win32", environ=environ) == (
        roaming / "moco" / "moco.yaml"
    )
    assert default_prompt_path(platform_name="win32", environ=environ) == (
        roaming / "moco" / "prompt.md"
    )
    assert default_runtime_state_path(platform_name="win32", environ=environ) == (
        local / "moco" / "runtime-private" / "runtime.json"
    )


@pytest.mark.parametrize(
    ("path_function", "environment_name"),
    [
        (default_config_path, "APPDATA"),
        (default_prompt_path, "APPDATA"),
        (default_runtime_state_path, "LOCALAPPDATA"),
    ],
)
def test_windows_paths_require_documented_environment_without_echoing_values(
    path_function: Callable[..., Path],
    environment_name: str,
) -> None:
    sensitive_value = "do-not-echo"
    environ = {"UNRELATED_SECRET": sensitive_value}

    with pytest.raises(HostPlatformError, match=environment_name) as caught:
        path_function(platform_name="win32", environ=environ)

    assert sensitive_value not in str(caught.value)


def test_non_windows_paths_keep_existing_macos_locations() -> None:
    environ = {"HOME": "/Users/example"}

    assert default_config_path(platform_name="darwin", environ=environ) == Path(
        "/Users/example/Library/Application Support/moco/moco.yaml"
    )
    assert default_prompt_path(platform_name="darwin", environ=environ) == Path(
        "/Users/example/.moco/prompt.md"
    )
    assert default_runtime_state_path(platform_name="darwin", environ=environ) == Path(
        "/Users/example/Library/Application Support/moco/runtime.json"
    )


@pytest.mark.parametrize(
    "path_function",
    [default_config_path, default_prompt_path, default_runtime_state_path],
)
def test_non_windows_paths_reject_an_empty_home(
    path_function: Callable[..., Path],
) -> None:
    with pytest.raises(HostPlatformError, match="HOME"):
        path_function(platform_name="darwin", environ={"HOME": ""})


def test_service_and_hotkey_host_policy_is_explicit() -> None:
    assert service_supported(platform_name="darwin")
    assert not service_supported(platform_name="win32")
    assert not service_supported(platform_name="linux")
    assert hotkey_unavailable_detail(platform_name="darwin") == "input_monitoring_required"
    assert hotkey_unavailable_detail(platform_name="win32") == "browser_hotkey_fallback"


def test_open_browser_preserves_success_and_normalizes_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("moco.platform.webbrowser.open", lambda _url: True)
    assert open_browser("https://example.invalid/#secret")

    monkeypatch.setattr("moco.platform.webbrowser.open", lambda _url: False)
    assert not open_browser("https://example.invalid/#secret")


@pytest.mark.parametrize("error_type", [BrowserError, OSError])
def test_open_browser_normalizes_known_errors_without_exposing_them(
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_url: str) -> bool:
        message = "browser-secret"
        raise error_type(message)

    monkeypatch.setattr("moco.platform.webbrowser.open", fail)

    assert not open_browser("https://example.invalid/#capability-secret")


def test_codex_command_builds_shell_free_argv(tmp_path: Path) -> None:
    command = CodexCommand(("codex", "--profile", "voice"))

    assert command.app_server_argv() == (
        "codex",
        "--profile",
        "voice",
        "app-server",
        "--listen",
        "stdio://",
        "--enable",
        "realtime_conversation",
    )
    assert command.version_argv() == ("codex", "--profile", "voice", "--version")
    assert command.schema_argv(tmp_path, experimental=False) == (
        "codex",
        "--profile",
        "voice",
        "app-server",
        "generate-json-schema",
        "--out",
        str(tmp_path),
    )
    assert command.schema_argv(tmp_path, experimental=True) == (
        "codex",
        "--profile",
        "voice",
        "app-server",
        "generate-json-schema",
        "--out",
        str(tmp_path),
        "--experimental",
    )


def test_codex_command_is_frozen_and_slotted() -> None:
    command = CodexCommand(("codex",))

    with pytest.raises(FrozenInstanceError):
        command.argv = ("other",)  # type: ignore[misc]

    assert not hasattr(command, "__dict__")


def test_unconfigured_command_uses_path_without_store_fallback() -> None:
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return r"C:\Tools\codex.exe" if name == "codex" else None

    command = resolve_codex_command(None, platform_name="win32", which=which)

    assert command == CodexCommand((r"C:\Tools\codex.exe",))
    assert calls == ["codex"]


def test_windows_without_path_command_does_not_use_private_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_candidate = tmp_path / "private-codex"
    private_candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    private_candidate.chmod(0o755)
    monkeypatch.setattr(platform_module, "_DEFAULT_CODEX_BUNDLE", private_candidate)
    calls: list[str] = []

    def which(name: str) -> None:
        calls.append(name)

    with pytest.raises(CodexCommandError, match="unavailable") as caught:
        resolve_codex_command(
            None,
            platform_name="win32",
            which=which,
        )

    assert str(private_candidate) not in str(caught.value)
    assert calls == ["codex"]


def test_darwin_official_bundle_precedes_unconfigured_path_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_candidate = tmp_path / "bundle-codex"
    bundle_candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    bundle_candidate.chmod(0o755)
    monkeypatch.setattr(platform_module, "_DEFAULT_CODEX_BUNDLE", bundle_candidate)
    path_command = "/opt/homebrew/bin/codex"

    command = resolve_codex_command(
        None,
        platform_name="darwin",
        which=lambda _name: path_command,
    )

    assert command == CodexCommand((str(bundle_candidate),))


def test_darwin_without_path_command_uses_official_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_candidate = tmp_path / "bundle-codex"
    bundle_candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    bundle_candidate.chmod(0o755)
    monkeypatch.setattr(platform_module, "_DEFAULT_CODEX_BUNDLE", bundle_candidate)

    command = resolve_codex_command(
        None,
        platform_name="darwin",
        which=lambda _name: None,
    )

    assert command == CodexCommand((str(bundle_candidate),))


def test_explicit_path_command_preserves_arguments(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    command = resolve_codex_command((str(executable), "--profile", "voice"))

    assert command == CodexCommand((str(executable), "--profile", "voice"))


def test_missing_absolute_executable_is_rejected_without_echoing_command(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "sensitive-private-codex"
    sensitive_argument = "sensitive-argument"

    with pytest.raises(CodexCommandError, match="unavailable") as caught:
        resolve_codex_command((str(candidate), sensitive_argument))

    assert str(candidate) not in str(caught.value)
    assert sensitive_argument not in str(caught.value)


def test_posix_non_executable_absolute_command_is_rejected_without_echoing_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "sensitive-private-codex"
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("moco.platform.os.access", lambda _path, _mode: False)

    with pytest.raises(CodexCommandError, match="unavailable") as caught:
        resolve_codex_command((str(candidate), "sensitive-argument"))

    assert str(candidate) not in str(caught.value)
    assert "sensitive-argument" not in str(caught.value)


def test_invalid_explicit_command_does_not_fallback() -> None:
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return "/usr/local/bin/codex" if name == "codex" else None

    with pytest.raises(CodexCommandError, match="unavailable") as caught:
        resolve_codex_command(
            ("missing-private-codex",),
            platform_name="win32",
            which=which,
        )

    assert "missing-private-codex" not in str(caught.value)
    assert calls == ["missing-private-codex"]


def test_empty_explicit_command_is_rejected_without_index_error() -> None:
    with pytest.raises(CodexCommandError, match="unavailable") as caught:
        resolve_codex_command(())

    assert str(caught.value) == "configured Codex command is unavailable"
