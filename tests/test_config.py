from __future__ import annotations

from pathlib import Path

import pytest

from moco.config import ConfigError, default_config_path, load_config


def test_load_config_applies_defaults(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")

    settings = load_config(path)

    assert settings.server.host == "127.0.0.1"
    assert settings.runtime.idle_timeout_seconds == 300
    assert settings.hotkeys.push_to_talk == "f1"
    assert settings.hotkeys.cancel == "f2"
    assert settings.speech.segment_max_chars == 80


def test_load_config_applies_yaml_values(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        "runtime:\n  idle_timeout_seconds: 42\n"
        "irodori:\n  base_url: http://100.64.0.1:8923\n",
        encoding="utf-8",
    )

    settings = load_config(path)

    assert settings.runtime.idle_timeout_seconds == 42
    assert str(settings.irodori.base_url) == "http://100.64.0.1:8923/"


def test_load_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("runtime:\n  mystery: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"runtime\.mystery"):
        load_config(path)


def test_operator_server_must_bind_loopback(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("server:\n  host: 0.0.0.0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="loopback"):
        load_config(path)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "server:\n  port: 0\n",
        "runtime:\n  idle_timeout_seconds: 0\n",
        "irodori:\n  timeout_seconds: -1\n",
        "irodori:\n  max_wav_bytes: 0\n",
        "speech:\n  segment_max_chars: 0\n",
    ],
)
def test_positive_values_are_required(tmp_path: Path, yaml_text: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_hotkeys_must_be_distinct(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("hotkeys:\n  push_to_talk: F1\n  cancel: f1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="distinct"):
        load_config(path)


@pytest.mark.parametrize("field", ["binary", "working_directory"])
def test_codex_paths_must_be_absolute(tmp_path: Path, field: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(f"codex:\n  {field}: relative/path\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=f"codex.{field}"):
        load_config(path)


def test_irodori_url_must_not_contain_credentials(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    sensitive_value = "do-not-echo"
    path.write_text(
        f"irodori:\n  base_url: http://user:{sensitive_value}@127.0.0.1:8923\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as caught:
        load_config(path)

    assert "credentials" in str(caught.value)
    assert sensitive_value not in str(caught.value)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "irodori:\n  num_steps: 0\n",
        "irodori:\n  duration_scale: 0\n",
        "irodori:\n  cfg_scale_text: 0\n",
        "irodori:\n  cfg_scale_speaker: 0\n",
        "speech:\n  vad_threshold: 0\n",
        "speech:\n  vad_threshold: 1.1\n",
    ],
)
def test_synthesis_ranges_are_enforced(tmp_path: Path, yaml_text: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("url", ["file:///tmp/otel", "ftp://127.0.0.1"])
def test_otlp_endpoint_requires_http(tmp_path: Path, url: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(f"telemetry:\n  otlp_endpoint: {url}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="HTTP"):
        load_config(path)


def test_invalid_yaml_is_reported_without_values(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    sensitive_value = "hidden-value"
    path.write_text(f"runtime: [\n  {sensitive_value}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_config(path)

    assert "YAML" in str(caught.value)
    assert sensitive_value not in str(caught.value)


def test_default_config_path_uses_macos_application_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/example")

    assert default_config_path() == Path(
        "/Users/example/Library/Application Support/moco/moco.yaml",
    )
