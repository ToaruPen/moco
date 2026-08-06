from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from moco.config import (
    CodexSettings,
    ConfigError,
    IrodoriSettings,
    ServerSettings,
    SpeechSettings,
    default_config_path,
    default_prompt_path,
    load_config,
)


def test_load_config_applies_defaults(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")

    settings = load_config(path)

    assert settings.server.host == "127.0.0.1"
    assert settings.runtime.idle_timeout_seconds == 300
    assert settings.hotkeys.start_listening == "f1"
    assert settings.hotkeys.stop_listening == "f2"
    assert settings.speech.segment_max_chars == 80


def test_load_config_applies_yaml_values(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        "runtime:\n  idle_timeout_seconds: 42\nirodori:\n  base_url: http://100.64.0.1:8923\n",
        encoding="utf-8",
    )

    settings = load_config(path)

    assert settings.runtime.idle_timeout_seconds == 42
    assert str(settings.irodori.base_url) == "http://100.64.0.1:8923/"


def test_example_config_loads() -> None:
    path = Path(__file__).parents[1] / "config" / "moco.example.yaml"

    settings = load_config(path)

    assert settings.irodori.caption_mode == "off"


def test_speech_first_segment_soft_break_defaults_to_disabled() -> None:
    assert SpeechSettings().first_segment_soft_break_min_chars is None


def test_speech_first_segment_soft_break_can_be_disabled() -> None:
    settings = SpeechSettings(first_segment_soft_break_min_chars=None)

    assert settings.first_segment_soft_break_min_chars is None


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "18"])
def test_speech_first_segment_soft_break_is_strict(value: object) -> None:
    with pytest.raises(ValidationError):
        SpeechSettings(first_segment_soft_break_min_chars=value)  # type: ignore[arg-type]


def test_speech_first_segment_soft_break_cannot_exceed_segment_limit() -> None:
    with pytest.raises(
        ValidationError,
        match="first_segment_soft_break_min_chars",
    ):
        SpeechSettings(
            segment_max_chars=17,
            first_segment_soft_break_min_chars=18,
        )


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


def test_public_operator_url_is_normalized() -> None:
    settings = ServerSettings(public_url=" HTTPS://Voice.Example.COM ")

    assert settings.public_url == "https://voice.example.com"


@pytest.mark.parametrize(
    "public_url",
    [
        "http://voice.example.com",
        "https://127.0.0.1",
        "https://*.example.com",
        "https://voice.example.com:8443",
        "https://voice.example.com/path",
        "https://voice.example.com?mode=mobile",
        "https://voice.example.com/#fragment",
        "https://user@voice.example.com",
        "https://localhost",
    ],
)
def test_public_operator_url_rejects_unsafe_shapes(public_url: str) -> None:
    with pytest.raises(ValueError, match="public URL"):
        ServerSettings(public_url=public_url)


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
    path.write_text(
        "hotkeys:\n  start_listening: F1\n  stop_listening: f1\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="distinct"):
        load_config(path)


@pytest.mark.parametrize("field", ["binary", "working_directory"])
def test_codex_paths_must_be_absolute(tmp_path: Path, field: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(f"codex:\n  {field}: relative/path\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=f"codex.{field}"):
        load_config(path)


def test_default_prompt_path_uses_dot_moco(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/example")

    assert default_prompt_path() == Path("/Users/example/.moco/prompt.md")


def test_codex_prompt_file_defaults_to_implicit_path() -> None:
    assert CodexSettings().prompt_file is None


def test_codex_prompt_file_expands_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/Users/example")

    settings = CodexSettings(prompt_file=Path("~/.moco/character.md"))

    assert settings.prompt_file == Path("/Users/example/.moco/character.md")


def test_codex_prompt_file_rejects_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("codex:\n  prompt_file: prompts/moco.md\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"codex\.prompt_file"):
        load_config(path)


@pytest.mark.parametrize(
    ("raw_prompt_file", "message"),
    [
        ('"~moco-user-that-does-not-exist/prompt.md"', "home directory"),
        ('"/tmp/moco\\0prompt"', "NUL"),
    ],
    ids=["unknown-home", "nul"],
)
def test_codex_prompt_file_rejects_unusable_path(
    tmp_path: Path,
    raw_prompt_file: str,
    message: str,
) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        f"codex:\n  prompt_file: {raw_prompt_file}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=rf"codex\.prompt_file.*{message}"):
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
    ("configured", "expected"),
    [(" preferred-id ", "preferred-id"), ("   ", None), (None, None)],
)
def test_irodori_speaker_is_normalized(
    configured: str | None,
    expected: str | None,
) -> None:
    settings = IrodoriSettings(speaker=configured)

    assert settings.speaker == expected


def test_irodori_caption_mode_defaults_to_off() -> None:
    assert IrodoriSettings().caption_mode == "off"


def test_irodori_caption_mode_rejects_non_off(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("irodori:\n  caption_mode: auto\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"irodori\.caption_mode"):
        load_config(path)


def test_irodori_static_speakers_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        "irodori:\n  speakers:\n    - fixture-id\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"irodori\.speakers"):
        load_config(path)


def test_irodori_static_speaker_property_is_removed() -> None:
    legacy_property = "available_" + "speakers"

    assert not hasattr(IrodoriSettings(), legacy_property)


def test_irodori_connect_ip_must_be_an_ip_address(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        "irodori:\n  connect_ip: not-a-hostname\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="connect_ip"):
        load_config(path)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://windows-node.example.ts.net",
        "https://100.64.0.2",
        "https://windows-node.example.ts.net:8443",
        "https://localhost",
    ],
)
def test_irodori_connect_ip_requires_portless_https_fqdn(
    tmp_path: Path,
    base_url: str,
) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        f"irodori:\n  base_url: {base_url}\n  connect_ip: 100.64.0.1\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="connect_ip"):
        load_config(path)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "irodori:\n  num_steps: 0\n",
        "irodori:\n  num_steps: 65\n",
        "irodori:\n  duration_scale: 0\n",
        "irodori:\n  cfg_scale_text: 0\n",
        "irodori:\n  cfg_scale_speaker: 0\n",
        "irodori:\n  t_schedule_mode: unsupported\n",
        "speech:\n  vad_threshold: 0\n",
        "speech:\n  vad_threshold: 1.1\n",
    ],
)
def test_synthesis_ranges_are_enforced(tmp_path: Path, yaml_text: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("num_steps", [1, 64])
def test_irodori_num_steps_accepts_supported_boundaries(num_steps: int) -> None:
    assert IrodoriSettings(num_steps=num_steps).num_steps == num_steps


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
