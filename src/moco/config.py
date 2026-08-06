from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    IPvAnyAddress,
    ValidationError,
    field_validator,
    model_validator,
)

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
Port = Annotated[int, Field(gt=0, le=65_535)]
VadThreshold = Annotated[float, Field(gt=0.0, le=1.0)]
_MIN_PUBLIC_DNS_LABELS = 2
_MAX_DNS_LABEL_LENGTH = 63


class ConfigError(ValueError):
    """A safe, user-facing configuration error."""


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(StrictSettings):
    host: str = "127.0.0.1"
    port: Port = 8765
    public_url: str | None = None

    @field_validator("host")
    @classmethod
    def _require_loopback(cls, value: str) -> str:
        host = value.strip().lower()
        if host == "localhost":
            return host
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            msg = "operator server host must be a loopback address"
            raise ValueError(msg) from error
        if not address.is_loopback:
            msg = "operator server host must be a loopback address"
            raise ValueError(msg)
        return host

    @field_validator("public_url")
    @classmethod
    def _validate_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        try:
            address = ipaddress.ip_address(hostname or "")
        except ValueError:
            address = None
        try:
            port = parsed.port
        except ValueError as error:
            msg = "operator public URL must be a portless HTTPS FQDN"
            raise ValueError(msg) from error
        labels = (hostname or "").rstrip(".").split(".")
        labels_valid = len(labels) >= _MIN_PUBLIC_DNS_LABELS and all(
            label.isascii()
            and 1 <= len(label) <= _MAX_DNS_LABEL_LENGTH
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
        if (
            parsed.scheme.casefold() != "https"
            or hostname is None
            or address is not None
            or not labels_valid
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "*" in candidate
        ):
            msg = "operator public URL must be a portless HTTPS FQDN"
            raise ValueError(msg)
        return f"https://{hostname.rstrip('.').casefold()}"


class HotkeySettings(StrictSettings):
    enabled: bool = True
    start_listening: str = "f1"
    stop_listening: str = "f2"

    @field_validator("start_listening", "stop_listening")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        key = value.strip().lower()
        if not key:
            msg = "hotkey must not be blank"
            raise ValueError(msg)
        return key

    @model_validator(mode="after")
    def _require_distinct_keys(self) -> Self:
        if self.start_listening == self.stop_listening:
            msg = "start-listening and stop-listening hotkeys must be distinct"
            raise ValueError(msg)
        return self


class RuntimeSettings(StrictSettings):
    idle_timeout_seconds: PositiveFloat = 300


class CodexSettings(StrictSettings):
    binary: Path = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    working_directory: Path = Field(default_factory=Path.cwd)
    prompt_file: Path | None = None

    @field_validator("prompt_file", mode="before")
    @classmethod
    def _expand_prompt_file(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            try:
                path = Path(value).expanduser()
            except RuntimeError as error:
                msg = "prompt path uses an unknown home directory"
                raise ValueError(msg) from error
            if "\0" in str(path):
                msg = "prompt path must not contain NUL"
                raise ValueError(msg)
            return path
        return value

    @field_validator("binary", "working_directory")
    @classmethod
    def _require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            msg = "path must be absolute"
            raise ValueError(msg)
        return value

    @field_validator("prompt_file")
    @classmethod
    def _require_absolute_prompt_file(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            msg = "path must be absolute"
            raise ValueError(msg)
        return value


def _validate_http_url(value: object, *, label: str) -> object:
    if not isinstance(value, str):
        return value
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        msg = f"{label} must use HTTP or HTTPS"
        raise ValueError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = f"{label} must not contain credentials"
        raise ValueError(msg)
    return value.strip()


class IrodoriSettings(StrictSettings):
    base_url: HttpUrl = HttpUrl("http://127.0.0.1:8923")
    connect_ip: IPvAnyAddress | None = None
    speaker: str | None = None
    caption_mode: Literal["off"] = "off"
    num_steps: PositiveInt = 24
    duration_scale: PositiveFloat = 1.0
    cfg_scale_text: PositiveFloat = 3.0
    cfg_scale_speaker: PositiveFloat = 5.0
    timeout_seconds: PositiveFloat = 30.0
    max_wav_bytes: PositiveInt = 33_554_432

    @field_validator("base_url", mode="before")
    @classmethod
    def _validate_base_url(cls, value: object) -> object:
        return _validate_http_url(value, label="Irodori base URL")

    @field_validator("speaker")
    @classmethod
    def _normalize_speaker(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _require_secure_address_override(self) -> Self:
        if self.connect_ip is None:
            return self
        parsed = urlsplit(str(self.base_url))
        hostname = parsed.hostname or ""
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            hostname_is_ip = False
        else:
            hostname_is_ip = True
        if (
            parsed.scheme != "https"
            or parsed.port is not None
            or "." not in hostname
            or hostname_is_ip
        ):
            msg = "connect_ip requires a portless HTTPS base_url with a DNS FQDN"
            raise ValueError(msg)
        return self


class SpeechSettings(StrictSettings):
    segment_max_chars: PositiveInt = 80
    first_segment_soft_break_min_chars: PositiveInt | None = Field(
        default=None,
        strict=True,
    )
    vad_threshold: VadThreshold = 0.04
    vad_hold_ms: PositiveInt = 120

    @model_validator(mode="after")
    def _validate_segment_limits(self) -> Self:
        minimum = self.first_segment_soft_break_min_chars
        if minimum is not None and minimum > self.segment_max_chars:
            msg = "first_segment_soft_break_min_chars must not exceed segment_max_chars"
            raise ValueError(msg)
        return self


class TelemetrySettings(StrictSettings):
    console: bool = True
    otlp_endpoint: HttpUrl | None = None
    service_name: str = "moco"

    @field_validator("otlp_endpoint", mode="before")
    @classmethod
    def _validate_otlp_endpoint(cls, value: object) -> object:
        if value is None:
            return None
        return _validate_http_url(value, label="OTLP endpoint")

    @field_validator("service_name")
    @classmethod
    def _require_service_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            msg = "telemetry service name must not be blank"
            raise ValueError(msg)
        return name


class MocoSettings(StrictSettings):
    server: ServerSettings = ServerSettings()
    hotkeys: HotkeySettings = HotkeySettings()
    runtime: RuntimeSettings = RuntimeSettings()
    codex: CodexSettings = CodexSettings()
    irodori: IrodoriSettings = IrodoriSettings()
    speech: SpeechSettings = SpeechSettings()
    telemetry: TelemetrySettings = TelemetrySettings()


def default_config_path() -> Path:
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / "Library" / "Application Support" / "moco" / "moco.yaml"


def default_prompt_path() -> Path:
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".moco" / "prompt.md"


def _format_validation_error(error: ValidationError) -> str:
    messages: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "configuration"
        messages.append(f"{location}: {item['msg']}")
    return "; ".join(messages)


def load_config(path: Path | None = None) -> MocoSettings:
    config_path = path or default_config_path()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        message = f"configuration file not found: {config_path}"
        raise ConfigError(message) from error
    except OSError as error:
        message = f"configuration file could not be read: {config_path}"
        raise ConfigError(message) from error
    except yaml.YAMLError as error:
        message = "configuration contains invalid YAML"
        raise ConfigError(message) from error

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        message = "configuration YAML root must be a mapping"
        raise ConfigError(message)

    try:
        return MocoSettings.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(_format_validation_error(error)) from error
