from __future__ import annotations

import ipaddress
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Self
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

from moco import platform as _platform
from moco.errors import PrivateStateError

if TYPE_CHECKING:
    from collections.abc import Iterator

default_config_path = _platform.default_config_path
default_prompt_path = _platform.default_prompt_path

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
Port = Annotated[int, Field(gt=0, le=65_535)]
VadThreshold = Annotated[float, Field(gt=0.0, le=1.0)]
IrodoriNumSteps = Annotated[int, Field(gt=0, le=64)]
_MIN_PUBLIC_DNS_LABELS = 2
_MAX_DNS_LABEL_LENGTH = 63
_IPV4_VERSION = 4
_IPV6_VERSION = 6
_IPV6_LOOPBACK = ipaddress.IPv6Address("::1")
_CONFIG_DIRECTORY_MODE = 0o700
_CONFIG_FILE_MODE = 0o600
_CONFIG_SECURITY_ERROR = "configuration path does not satisfy host security requirements"


def canonical_browser_loopback_host(
    value: str | None,
    *,
    allow_localhost: bool = False,
) -> str | None:
    """Return a canonical loopback host that a browser can represent safely."""
    if not isinstance(value, str):
        return None
    if allow_localhost and value.casefold() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if getattr(address, "scope_id", None) is not None:
        return None
    if getattr(address, "ipv4_mapped", None) is not None:
        return None
    canonical: str | None = None
    if address.version == _IPV4_VERSION and address.is_loopback:
        canonical = str(address)
    elif address.version == _IPV6_VERSION and address == _IPV6_LOOPBACK:
        canonical = "::1"
    return canonical


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
        host = canonical_browser_loopback_host(
            value.strip().casefold(),
            allow_localhost=True,
        )
        if host is None:
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
    command: tuple[str, ...] | None = None
    working_directory: Path | None = None
    prompt_file: Path | None = None

    @field_validator("command")
    @classmethod
    def _validate_command(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value or any(not item.strip() or "\0" in item for item in value):
            msg = "command must contain non-blank NUL-free argv values"
            raise ValueError(msg)
        return value

    @field_validator("prompt_file", mode="before")
    @classmethod
    def _expand_prompt_file(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            raw_path = str(value)
            has_named_home = (
                raw_path.startswith("~") and len(raw_path) > 1 and raw_path[1] not in {"/", "\\"}
            )
            if has_named_home:
                msg = "prompt path uses an unknown home directory"
                raise ValueError(msg)
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

    @field_validator("working_directory", "prompt_file")
    @classmethod
    def _require_absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            msg = "path must be absolute"
            raise ValueError(msg)
        return value


class AgentProfileMode(StrEnum):
    """The Agent capability profile moco requests, owned by local configuration."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    INHERIT_CODEX = "inherit_codex"


class AgentSettings(StrictSettings):
    profile: AgentProfileMode = AgentProfileMode.READ_ONLY


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
    caption_mode: Literal["off", "auto"] = "off"
    num_steps: IrodoriNumSteps = 12
    t_schedule_mode: Literal["linear", "sway"] = "sway"
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
    agent: AgentSettings = AgentSettings()
    irodori: IrodoriSettings = IrodoriSettings()
    speech: SpeechSettings = SpeechSettings()
    telemetry: TelemetrySettings = TelemetrySettings()


def _format_validation_error(error: ValidationError) -> str:
    messages: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "configuration"
        messages.append(f"{location}: {item['msg']}")
    return "; ".join(messages)


def load_config(path: Path | None = None) -> MocoSettings:
    config_path = path or default_config_path()
    try:
        raw = yaml.safe_load(_read_config_text(config_path))
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


def write_config(path: Path, content: bytes) -> None:
    platform_name = _current_platform()
    try:
        _prepare_config_directory(path.parent, platform_name=platform_name)
        with _config_namespace(path.parent, platform_name=platform_name):
            parent_identity = _config_path_identity(path.parent)
            if os.path.lexists(path) and platform_name == "win32":
                _validate_config_file(path)

            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
            )
            temporary = Path(temporary_name)
            descriptor_open = True
            try:
                _require_config_identity(path.parent, parent_identity)
                if platform_name == "win32":
                    _protect_windows_config_path(temporary)
                    _validate_config_file(temporary)
                else:
                    os.fchmod(descriptor, _CONFIG_FILE_MODE)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor_open = False
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_identity = _config_path_identity(temporary)
                _require_config_identity(path.parent, parent_identity)
                temporary.replace(path)
                if platform_name == "win32":
                    _validate_config_file(path)
                    _require_config_identity(path, temporary_identity)
            finally:
                if descriptor_open:
                    os.close(descriptor)
                if os.path.lexists(temporary):
                    temporary.unlink()
    except PrivateStateError:
        raise ConfigError(_CONFIG_SECURITY_ERROR) from None


def _read_config_text(path: Path, *, platform_name: str | None = None) -> str:
    platform_value = platform_name or _current_platform()
    if platform_value != "win32":
        return path.read_text(encoding="utf-8")
    try:
        with _config_namespace(path.parent, platform_name="win32"):
            parent_identity = _config_path_identity(path.parent)
            _validate_config_directory(path.parent)
            _validate_config_file(path)
            path_identity = _config_path_identity(path)
            descriptor = os.open(path, os.O_RDONLY)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or (
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                    != path_identity
                ):
                    raise PrivateStateError(_CONFIG_SECURITY_ERROR)
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = -1
                    content = stream.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            _validate_config_file(path)
            _require_config_identity(path, path_identity)
            _require_config_identity(path.parent, parent_identity)
            return content
    except PrivateStateError:
        raise ConfigError(_CONFIG_SECURITY_ERROR) from None


def _prepare_config_directory(path: Path, *, platform_name: str) -> None:
    created = False
    try:
        os.lstat(path)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=_CONFIG_DIRECTORY_MODE)
        except FileExistsError:
            created = False
        else:
            created = True
    if platform_name == "win32":
        if created:
            _protect_windows_config_path(path)
        _validate_config_directory(path)


def _validate_config_directory(path: Path) -> None:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PrivateStateError(_CONFIG_SECURITY_ERROR)
    _validate_windows_config_path(path)


def _validate_config_file(path: Path) -> None:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PrivateStateError(_CONFIG_SECURITY_ERROR)
    _validate_windows_config_path(path)


def _validate_windows_config_path(path: Path) -> None:
    from moco.runtime._windows_acl import read_windows_security  # noqa: PLC0415
    from moco.runtime.private_state import validate_windows_security  # noqa: PLC0415

    validate_windows_security(read_windows_security(path))


def _protect_windows_config_path(path: Path) -> None:
    from moco.runtime._windows_acl import protect_windows_dacl  # noqa: PLC0415

    protect_windows_dacl(path)


@contextmanager
def _config_namespace(path: Path, *, platform_name: str) -> Iterator[None]:
    if platform_name != "win32":
        yield
        return
    from moco.runtime._windows_acl import (  # noqa: PLC0415
        hold_windows_directory_namespace,
    )

    with hold_windows_directory_namespace(path):
        yield


def _config_path_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    return metadata.st_dev, metadata.st_ino


def _require_config_identity(path: Path, expected: tuple[int, int]) -> None:
    if _config_path_identity(path) != expected:
        raise PrivateStateError(_CONFIG_SECURITY_ERROR)


def _current_platform() -> str:
    return sys.platform
