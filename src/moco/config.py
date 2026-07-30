from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
Port = Annotated[int, Field(gt=0, le=65_535)]
VadThreshold = Annotated[float, Field(gt=0.0, le=1.0)]


class ConfigError(ValueError):
    """A safe, user-facing configuration error."""


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(StrictSettings):
    host: str = "127.0.0.1"
    port: Port = 8765

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


class HotkeySettings(StrictSettings):
    enabled: bool = True
    push_to_talk: str = "f1"
    cancel: str = "f2"

    @field_validator("push_to_talk", "cancel")
    @classmethod
    def _normalize_key(cls, value: str) -> str:
        key = value.strip().lower()
        if not key:
            msg = "hotkey must not be blank"
            raise ValueError(msg)
        return key

    @model_validator(mode="after")
    def _require_distinct_keys(self) -> Self:
        if self.push_to_talk == self.cancel:
            msg = "push-to-talk and cancel hotkeys must be distinct"
            raise ValueError(msg)
        return self


class RuntimeSettings(StrictSettings):
    idle_timeout_seconds: PositiveFloat = 300


class CodexSettings(StrictSettings):
    binary: Path = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    working_directory: Path = Field(default_factory=Path.cwd)

    @field_validator("binary", "working_directory")
    @classmethod
    def _require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
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
    speaker: str | None = None
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


class SpeechSettings(StrictSettings):
    segment_max_chars: PositiveInt = 80
    vad_threshold: VadThreshold = 0.04
    vad_hold_ms: PositiveInt = 120


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
