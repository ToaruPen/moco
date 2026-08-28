from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from moco import config as config_module
from moco.config import (
    AgentProfileMode,
    CloudflareAccessSettings,
    CodexSettings,
    ConfigError,
    IrodoriSettings,
    ServerSettings,
    SpeechSettings,
    default_config_path,
    default_prompt_path,
    load_config,
)
from moco.errors import PrivateStateError


@pytest.fixture(autouse=True)
def _isolate_config_content_tests_from_windows_host_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        monkeypatch.setattr(config_module, "_current_platform", lambda: "darwin")


def test_load_config_applies_defaults(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")

    settings = load_config(path)

    assert settings.server.host == "127.0.0.1"
    assert settings.runtime.idle_timeout_seconds == 300
    assert settings.hotkeys.start_listening == "f1"
    assert settings.hotkeys.stop_listening == "f2"
    assert settings.speech.segment_max_chars == 80


def test_windows_config_load_fails_closed_on_unsafe_access_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_current_platform", lambda: "win32")

    def reject(_path: Path) -> None:
        message = "untrusted principal"
        raise PrivateStateError(message)

    @contextmanager
    def namespace(_path: Path, *, platform_name: str) -> Iterator[None]:
        assert platform_name == "win32"
        yield

    monkeypatch.setattr(config_module, "_config_namespace", namespace)
    monkeypatch.setattr(
        config_module,
        "_validate_windows_config_path",
        reject,
        raising=False,
    )

    with pytest.raises(ConfigError, match="security requirements"):
        load_config(path)


def test_load_config_applies_yaml_values(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        "runtime:\n  idle_timeout_seconds: 42\nirodori:\n  base_url: http://100.64.0.1:8923\n",
        encoding="utf-8",
    )

    settings = load_config(path)

    assert settings.runtime.idle_timeout_seconds == 42
    assert str(settings.irodori.base_url) == "http://100.64.0.1:8923/"


@pytest.mark.parametrize(
    ("configured_host", "expected_host"),
    [
        ("localhost", "127.0.0.1"),
        (" LOCALHOST ", "127.0.0.1"),
        ("127.0.0.42", "127.0.0.42"),
        ("::1", "::1"),
        ("0:0:0:0:0:0:0:1", "::1"),
    ],
)
def test_server_host_is_canonical_numeric_loopback(
    configured_host: str,
    expected_host: str,
) -> None:
    assert ServerSettings(host=configured_host).host == expected_host


@pytest.mark.parametrize(
    "configured_host",
    [
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::1%lo0",
        "::1%25lo0",
    ],
)
def test_server_host_rejects_mapped_and_scoped_loopback_forms(
    configured_host: str,
) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        ServerSettings(host=configured_host)


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
    settings = ServerSettings.model_validate(
        {
            "public_url": " HTTPS://Voice.Example.COM/ ",
            "cloudflare_access": {
                "team_domain": " HTTPS://Example-Team.CloudflareAccess.COM/ ",
                "audience": " audience_123-ABC ",
                "allowed_email": "Owner@Example.COM",
            },
        },
    )

    assert settings.public_url == "https://voice.example.com"
    assert settings.cloudflare_access is not None
    assert settings.cloudflare_access.team_domain == "https://example-team.cloudflareaccess.com"
    assert settings.cloudflare_access.audience == "audience_123-ABC"
    assert settings.cloudflare_access.allowed_email == "Owner@Example.COM"


@pytest.mark.parametrize(
    "public_url",
    [
        "https://xn--bcher-kva.example",
        "https://xn--fa-hia.example",
        "https://xn--strae-oqa.example",
    ],
)
def test_public_operator_url_accepts_valid_ace_hostname(public_url: str) -> None:
    settings = ServerSettings.model_validate(
        {
            "public_url": public_url,
            "cloudflare_access": {
                "team_domain": "https://example-team.cloudflareaccess.com",
                "audience": "audience_123-ABC",
                "allowed_email": "owner@example.com",
            },
        },
    )

    assert settings.public_url == public_url


@pytest.mark.parametrize(
    "server",
    [
        {"public_url": "https://voice.example.com"},
        {
            "cloudflare_access": {
                "team_domain": "https://example-team.cloudflareaccess.com",
                "audience": "audience_123-ABC",
                "allowed_email": "owner@example.com",
            },
        },
    ],
    ids=["public-url-only", "access-only"],
)
def test_public_operator_url_and_cloudflare_access_are_required_together(
    server: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match=r"public_url.*cloudflare_access"):
        ServerSettings.model_validate(server)


def test_cloudflare_access_settings_are_frozen_and_reject_unknown_keys() -> None:
    settings = CloudflareAccessSettings(
        team_domain="https://example-team.cloudflareaccess.com",
        audience="audience_123-ABC",
        allowed_email="owner@example.com",
    )

    with pytest.raises(ValidationError, match="frozen"):
        settings.audience = "replacement"
    with pytest.raises(ValidationError, match="extra"):
        CloudflareAccessSettings.model_validate(
            {
                "team_domain": "https://example-team.cloudflareaccess.com",
                "audience": "audience_123-ABC",
                "allowed_email": "owner@example.com",
                "token": "must-not-be-configurable",
            },
        )


@pytest.mark.parametrize(
    "team_domain",
    [
        "http://example-team.cloudflareaccess.com",
        "https://user@example-team.cloudflareaccess.com",
        "https://example-team.cloudflareaccess.com:443",
        "https://example-team.cloudflareaccess.com:invalid",
        "https://example-team.cloudflareaccess.com:",
        "https://example-team.cloudflareaccess.com/path",
        "https://example-team.cloudflareaccess.com?query=yes",
        "https://example-team.cloudflareaccess.com#fragment",
        "https://*.cloudflareaccess.com",
        "https://127.0.0.1",
        "https://example-team.cloudflareaccess.com.evil.example",
        "https://example-team.cloudflareaccess.com.",
        "https://example-team.cloudflareaccess.com%2eevil.example",
        "https://example- team.cloudflareaccess.com",
        "https://example-\tteam.cloudflareaccess.com",
        "https://example-\rteam.cloudflareaccess.com",
        "https://example-\nteam.cloudflareaccess.com",
        "https://foo.bar.cloudflareaccess.com",
        "https://-example.cloudflareaccess.com",
        "https://example-.cloudflareaccess.com",
        "https://example_team.cloudflareaccess.com",
        f"https://{'a' * 64}.cloudflareaccess.com",
        "https://équipe.cloudflareaccess.com",
        "https://cloudflareaccess.com",
        "https://example.com",
        "https://[::1",
    ],
)
def test_cloudflare_access_rejects_invalid_team_domain(team_domain: str) -> None:
    with pytest.raises(ValidationError, match="team domain"):
        CloudflareAccessSettings(
            team_domain=team_domain,
            audience="audience_123-ABC",
            allowed_email="owner@example.com",
        )


def test_cloudflare_access_accepts_maximum_audience_length() -> None:
    audience = "a" * 256

    settings = CloudflareAccessSettings(
        team_domain="https://example-team.cloudflareaccess.com",
        audience=f" {audience} ",
        allowed_email="owner@example.com",
    )

    assert settings.audience == audience


@pytest.mark.parametrize(
    "audience",
    [
        "",
        "   ",
        "audience with space",
        "audience\tvalue",
        "audience\nvalue",
        "\taudience",
        "audience\n",
        "audience\0value",
        "audience.value",
        "é",
        "a" * 257,
    ],
)
def test_cloudflare_access_rejects_invalid_audience(audience: str) -> None:
    with pytest.raises(ValidationError, match="audience"):
        CloudflareAccessSettings(
            team_domain="https://example-team.cloudflareaccess.com",
            audience=audience,
            allowed_email="owner@example.com",
        )


def test_cloudflare_access_accepts_maximum_email_length_and_preserves_exact_case() -> None:
    allowed_email = f"{'A' * 242}@Example.com"

    settings = CloudflareAccessSettings(
        team_domain="https://example-team.cloudflareaccess.com",
        audience="audience_123-ABC",
        allowed_email=allowed_email,
    )

    assert len(settings.allowed_email) == 254
    assert settings.allowed_email == allowed_email


@pytest.mark.parametrize(
    "allowed_email",
    [
        "",
        "   ",
        "owner.example.com",
        "@example.com",
        "owner@",
        "owner@@example.com",
        "owner @example.com",
        "owner@example .com",
        "owner\texample@example.com",
        "owner\n@example.com",
        " owner@example.com",
        "owner@example.com ",
        "\towner@example.com",
        "owner@example.com\t",
        "\rowner@example.com",
        "owner@example.com\r",
        "\nowner@example.com",
        "owner@example.com\n",
        "owner\0@example.com",
        "Owner <owner@example.com>",
        "owner@example.com,other@example.com",
        "owner@example.com,",
        f"{'a' * 243}@example.com",
    ],
)
def test_cloudflare_access_rejects_invalid_allowed_email(allowed_email: str) -> None:
    with pytest.raises(ValidationError, match="allowed email"):
        CloudflareAccessSettings(
            team_domain="https://example-team.cloudflareaccess.com",
            audience="audience_123-ABC",
            allowed_email=allowed_email,
        )


@pytest.mark.parametrize(
    "public_url",
    [
        "http://voice.example.com",
        "https://127.0.0.1",
        "https://127.1",
        "https://0177.0.0.1",
        "https://127.0.0.01",
        "https://1.2",
        "https://0x7f.1",
        "https://0177.1",
        "https://2130706433",
        "https://127..1",
        "https://1.4294967296",
        "https://xn--a.com",
        "https://faß.example",
        "https://straße.example",
        "https://K.example",
        "https://ſ.example",
        "https://*.example.com",
        "https://voice.example.com:",
        "https://voice.example.com:8443",
        "https://voice.example.com/path",
        "https://voice.example.com?",
        "https://voice.example.com?mode=mobile",
        "https://voice.example.com#",
        "https://voice.example.com/#fragment",
        "https://user@voice.example.com",
        "https://localhost",
    ],
)
def test_public_operator_url_rejects_unsafe_shapes(public_url: str) -> None:
    with pytest.raises(ValueError, match="public URL"):
        ServerSettings.model_validate(
            {
                "public_url": public_url,
                "cloudflare_access": {
                    "team_domain": "https://example-team.cloudflareaccess.com",
                    "audience": "audience_123-ABC",
                    "allowed_email": "owner@example.com",
                },
            },
        )


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


def test_codex_working_directory_must_be_absolute(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("codex:\n  working_directory: relative/path\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"codex\.working_directory"):
        load_config(path)


def test_codex_command_and_working_directory_are_portable_defaults() -> None:
    settings = CodexSettings()

    assert settings.command is None
    assert settings.working_directory is None


def test_codex_command_accepts_an_argv_list(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        'codex:\n  command: ["codex", "--strict-config"]\n',
        encoding="utf-8",
    )

    assert load_config(path).codex.command == ("codex", "--strict-config")


@pytest.mark.parametrize("command", [[], [""], ["   "], ["codex", "bad\0arg"]])
def test_codex_command_rejects_unsafe_argv(
    tmp_path: Path,
    command: list[str],
) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(yaml.safe_dump({"codex": {"command": command}}), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"codex\.command"):
        load_config(path)


def test_default_prompt_path_uses_dot_moco() -> None:
    assert default_prompt_path(
        platform_name="darwin",
        environ={"HOME": "/Users/example"},
    ) == Path("/Users/example/.moco/prompt.md")


def test_codex_prompt_file_defaults_to_implicit_path() -> None:
    assert CodexSettings().prompt_file is None


def test_codex_prompt_file_expands_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "portable-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    settings = CodexSettings(prompt_file=Path("~/.moco/character.md"))

    assert settings.prompt_file == home / ".moco" / "character.md"


def test_codex_prompt_file_rejects_named_user_when_host_does_not_expand_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "expanduser", lambda path: path)

    with pytest.raises(ValidationError, match="home directory"):
        CodexSettings(prompt_file=Path("~unknown-moco-user/prompt.md"))


def test_codex_prompt_file_rejects_named_user_even_when_host_expands_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Path,
        "expanduser",
        lambda _path: Path("/Users/another-user/prompt.md"),
    )

    with pytest.raises(ValidationError, match="home directory"):
        CodexSettings(prompt_file=Path("~another-user/prompt.md"))


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


def test_agent_profile_modes_are_exactly_three() -> None:
    assert [mode.value for mode in AgentProfileMode] == [
        "read_only",
        "workspace_write",
        "inherit_codex",
    ]


def test_agent_profile_defaults_to_read_only(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")

    assert load_config(path).agent.profile == "read_only"


@pytest.mark.parametrize("profile", ["read_only", "workspace_write", "inherit_codex"])
def test_agent_profile_accepts_supported_modes(tmp_path: Path, profile: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(f"agent:\n  profile: {profile}\n", encoding="utf-8")

    assert load_config(path).agent.profile == profile


def test_agent_profile_rejects_unknown_mode(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    rejected_profile = "danger_full_access"
    path.write_text(f"agent:\n  profile: {rejected_profile}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"agent\.profile") as caught:
        load_config(path)

    assert rejected_profile not in str(caught.value)


def test_agent_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("agent:\n  mystery: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"agent\.mystery"):
        load_config(path)


def test_example_config_documents_the_agent_profile_modes() -> None:
    path = Path(__file__).parents[1] / "config" / "moco.example.yaml"
    example = path.read_text(encoding="utf-8")

    assert "profile: read_only" in example
    for mode in AgentProfileMode:
        assert mode.value in example
    assert load_config(path).agent.profile is AgentProfileMode.READ_ONLY


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


def test_irodori_caption_mode_accepts_auto(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("irodori:\n  caption_mode: auto\n", encoding="utf-8")

    assert load_config(path).irodori.caption_mode == "auto"


def test_irodori_caption_mode_rejects_unknown_value(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("irodori:\n  caption_mode: dynamic\n", encoding="utf-8")

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


def test_default_config_path_uses_macos_application_support() -> None:
    assert default_config_path(
        platform_name="darwin",
        environ={"HOME": "/Users/example"},
    ) == Path(
        "/Users/example/Library/Application Support/moco/moco.yaml",
    )
