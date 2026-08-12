from __future__ import annotations

import asyncio
import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, TYPE_CHECKING, Annotated, NoReturn, cast
from urllib.parse import urlsplit, urlunsplit

import typer
import uvicorn
import yaml

from moco.config import (
    ConfigError,
    MocoSettings,
    canonical_browser_loopback_host,
    default_config_path,
    load_config,
    write_config,
)
from moco.doctor import run_doctor
from moco.errors import PrivateStateError
from moco.platform import (
    default_runtime_state_path,
    hotkey_unavailable_detail,
    open_browser,
    service_supported,
)
from moco.runtime.hotkeys import GlobalHotkeyListener, HotkeyMapper
from moco.runtime.private_state import (
    PrivateStateIdentity,
    hold_private_runtime_lease,
    read_private_state,
    remove_private_state,
    write_private_state,
)
from moco.runtime.telemetry import configure_telemetry
from moco.service.launchd import (
    LaunchdError,
    install_service,
    read_service_status,
    start_service,
    stop_service,
    uninstall_service,
)
from moco.web.app import create_app
from moco.web.pairing import mobile_operator_url
from moco.web.review import is_valid_bootstrap_nonce, is_valid_control_secret

if TYPE_CHECKING:
    from http.client import HTTPMessage

app = typer.Typer(no_args_is_help=True, help="moco local voice agent")
config_app = typer.Typer(no_args_is_help=True, help="Manage strict YAML configuration.")
service_app = typer.Typer(no_args_is_help=True, help="Manage the user launchd service.")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")


_DEFAULT_CONFIG_PATH = default_config_path()
_RUNTIME_STATE_VERSION = 1
_REVIEW_BOOTSTRAP_PATH = "/review/bootstrap"
_REVIEW_PAGE_PATH = "/review"
_MAX_BOOTSTRAP_RESPONSE_BYTES = 4096
_HTTP_OK = 200


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> NoReturn:
        del msg, newurl
        fp.close()
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirects are disabled",
            headers,
            None,
        )


@config_app.command("init")
def config_init(
    *,
    path: Annotated[Path, typer.Option("--path")] = _DEFAULT_CONFIG_PATH,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Create a complete user-only configuration file."""
    if path.exists() and not force:
        typer.echo("ERROR [config_exists]: configuration already exists")
        raise typer.Exit(code=1)
    settings = MocoSettings()
    rendered = yaml.safe_dump(
        settings.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )
    try:
        write_config(path, rendered.encode())
    except ConfigError as error:
        typer.echo(f"ERROR [configuration]: {error}")
        raise typer.Exit(code=1) from error
    typer.echo(f"configuration initialized: {path}")


@config_app.command("validate")
def config_validate(
    *,
    path: Annotated[Path, typer.Option("--path")] = _DEFAULT_CONFIG_PATH,
) -> None:
    """Validate configuration without starting external services."""
    try:
        load_config(path)
    except ConfigError as error:
        typer.echo(f"ERROR [configuration]: {error}")
        raise typer.Exit(code=1) from error
    typer.echo("configuration is valid")


@app.command("doctor")
def doctor_command(
    *,
    config: Annotated[Path, typer.Option("--config")] = _DEFAULT_CONFIG_PATH,
    synthesize: Annotated[str | None, typer.Option("--synthesize")] = None,
) -> None:
    """Check Python, configuration, Codex, Irodori, and hotkeys."""
    settings = _load_or_exit(config)
    checks = asyncio.run(run_doctor(settings, synthesize=synthesize))
    failed = False
    for check in checks:
        typer.echo(f"[{check.status.upper()}] {check.code}: {check.detail}")
        failed = failed or check.status != "ok"
    if failed:
        raise typer.Exit(code=1)


@app.command("run")
def run_command(
    *,
    config: Annotated[Path, typer.Option("--config")] = _DEFAULT_CONFIG_PATH,
) -> None:
    """Run the foreground daemon and loopback operator server."""
    settings = _load_or_exit(config)
    try:
        asyncio.run(_run_runtime(settings, state_path=default_runtime_state_path()))
    except PrivateStateError as error:
        typer.echo("ERROR [runtime_state]: unavailable")
        raise typer.Exit(code=1) from error


@app.command("open")
def open_command() -> None:
    """Open the active operator page without printing its capability."""
    try:
        state_path = default_runtime_state_path()
        url = _read_state_url(state_path)
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        PrivateStateError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        typer.echo("ERROR [runtime_state]: no safe running moco instance was found")
        raise typer.Exit(code=1) from error
    if not open_browser(url):
        typer.echo("ERROR [browser]: unavailable")
        raise typer.Exit(code=1)
    typer.echo("operator page opened")


@app.command("review")
def review_command() -> None:
    """Open the local reviewer page through a short-lived private bootstrap."""
    try:
        state_path = default_runtime_state_path()
        local_url, control_secret = _read_review_state(state_path)
        bootstrap_url = _review_bootstrap_url(local_url)
        nonce = _request_review_bootstrap(bootstrap_url, control_secret)
        review_url = _review_page_url(local_url, nonce)
    except Exception:  # noqa: BLE001 - the CLI exposes one stable boundary error
        typer.echo("ERROR [review]: unavailable")
        raise typer.Exit(code=1) from None
    if not open_browser(review_url):
        typer.echo("ERROR [browser]: unavailable")
        raise typer.Exit(code=1)
    typer.echo("review page opened")


@service_app.command("install")
def service_install_command(
    *,
    config: Annotated[Path, typer.Option("--config")] = _DEFAULT_CONFIG_PATH,
    executable: Annotated[Path | None, typer.Option("--executable")] = None,
) -> None:
    """Install the exact moco user LaunchAgent."""
    if not service_supported():
        _exit_unsupported_service()
    resolved_executable = executable or Path(sys.argv[0]).resolve()
    try:
        path = install_service(
            executable=resolved_executable,
            config_path=config.absolute(),
        )
    except LaunchdError as error:
        _exit_service(error)
    typer.echo(f"service installed: {path}")


@service_app.command("start")
def service_start_command() -> None:
    """Start the installed moco user LaunchAgent."""
    if not service_supported():
        _exit_unsupported_service()
    try:
        start_service()
    except LaunchdError as error:
        _exit_service(error)
    typer.echo("service start requested")


@service_app.command("stop")
def service_stop_command() -> None:
    """Stop the moco user LaunchAgent."""
    if not service_supported():
        _exit_unsupported_service()
    try:
        stop_service()
    except LaunchdError as error:
        _exit_service(error)
    typer.echo("service stop requested")


@service_app.command("status")
def service_status_command() -> None:
    """Report whether the service is missing, stopped, or running."""
    if not service_supported():
        _exit_unsupported_service()
    typer.echo(read_service_status().value)


@service_app.command("uninstall")
def service_uninstall_command(
    *,
    executable: Annotated[Path | None, typer.Option("--executable")] = None,
) -> None:
    """Remove only the exact moco LaunchAgent."""
    if not service_supported():
        _exit_unsupported_service()
    resolved_executable = executable or Path(sys.argv[0]).resolve()
    try:
        uninstall_service(executable=resolved_executable)
    except LaunchdError as error:
        _exit_service(error)
    typer.echo("service uninstalled")


async def _run_runtime(settings: MocoSettings, *, state_path: Path) -> None:
    with hold_private_runtime_lease(state_path):
        await _run_owned_runtime(settings, state_path=state_path)


async def _run_owned_runtime(settings: MocoSettings, *, state_path: Path) -> None:
    telemetry = configure_telemetry(settings.telemetry)
    capability_value = secrets.token_urlsafe(32)
    control_secret = secrets.token_urlsafe(32)
    operator_app = create_app(
        settings,
        capability_token=capability_value,
        control_secret=control_secret,
    )
    loop = asyncio.get_running_loop()
    mapper = HotkeyMapper(
        start_key=settings.hotkeys.start_listening,
        stop_key=settings.hotkeys.stop_listening,
        emit=lambda control: asyncio.create_task(
            operator_app.state.control_hub.publish(control),
        ),
    )
    listener = GlobalHotkeyListener(loop=loop, mapper=mapper)
    global_hotkeys_active = False
    if settings.hotkeys.enabled:
        try:
            listener.start()
        except (OSError, RuntimeError):
            global_hotkeys_active = False
        else:
            global_hotkeys_active = listener.running
        if not global_hotkeys_active:
            typer.echo(f"WARN [hotkeys]: {hotkey_unavailable_detail()}")
    operator_app.state.global_hotkeys_active = global_hotkeys_active

    server = uvicorn.Server(
        uvicorn.Config(
            operator_app,
            host=settings.server.host,
            port=settings.server.port,
            log_config=None,
        ),
    )
    task = asyncio.create_task(server.serve(), name="moco-uvicorn")
    state_identity: PrivateStateIdentity | None = None
    try:
        while not server.started and not task.done():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        if not server.started:
            await task
            return
        state_identity = write_private_state(
            state_path,
            json.dumps(_runtime_state_payload(settings, capability_value, control_secret)).encode(),
        )
        typer.echo(
            f"moco is ready on {settings.server.host}:{settings.server.port}; run `moco open`"
        )
        await task
    finally:
        listener.stop()
        telemetry.close()
        if state_identity is not None:
            await asyncio.to_thread(
                remove_private_state,
                state_path,
                expected_identity=state_identity,
            )


def _load_or_exit(path: Path) -> MocoSettings:
    try:
        return load_config(path)
    except ConfigError as error:
        typer.echo(f"ERROR [configuration]: {error}")
        raise typer.Exit(code=1) from error


def _is_safe_operator_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and _is_numeric_loopback_host(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and bool(parsed.fragment)
        and not parsed.query
        and port is not None
    )


def _is_numeric_loopback_host(hostname: str | None) -> bool:
    return canonical_browser_loopback_host(hostname) is not None


def _is_safe_mobile_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and bool(parsed.fragment)
        and not parsed.query
        and port is None
    )


def _raise_review_unavailable() -> NoReturn:
    message = "review state is unavailable"
    raise ValueError(message)


def _runtime_state_payload(
    settings: MocoSettings,
    capability: str,
    control_secret: str,
) -> dict[str, object]:
    if not is_valid_control_secret(control_secret):
        _raise_review_unavailable()
    host = settings.server.host
    authority_host = f"[{host}]" if ":" in host else host
    payload = {
        "version": _RUNTIME_STATE_VERSION,
        "url": f"http://{authority_host}:{settings.server.port}/#{capability}",
        "control_secret": control_secret,
    }
    if settings.server.public_url is not None:
        payload["mobile_url"] = mobile_operator_url(settings.server.public_url, capability)
    return payload


def _read_state_url(state_path: Path) -> str:
    return _read_runtime_state(state_path)["url"]


def _read_review_state(state_path: Path) -> tuple[str, str]:
    state = _read_runtime_state(state_path)
    return state["url"], state["control_secret"]


def _read_runtime_state(state_path: Path) -> dict[str, str]:
    payload = json.loads(read_private_state(state_path))
    if type(payload) is not dict:
        _raise_review_unavailable()
    allowed = {"version", "url", "mobile_url", "control_secret"}
    required = {"version", "url", "control_secret"}
    if set(payload) - allowed or not required <= set(payload):
        _raise_review_unavailable()
    if type(payload["version"]) is not int or payload["version"] != _RUNTIME_STATE_VERSION:
        _raise_review_unavailable()
    url = payload["url"]
    control_secret = payload["control_secret"]
    if (
        type(url) is not str
        or not _is_safe_operator_url(url)
        or not is_valid_control_secret(control_secret)
    ):
        _raise_review_unavailable()
    if "mobile_url" in payload:
        mobile_url = payload["mobile_url"]
        if type(mobile_url) is not str or not _is_safe_mobile_url(mobile_url):
            _raise_review_unavailable()
    return {"url": url, "control_secret": control_secret}


def _review_bootstrap_url(local_url: str) -> str:
    parsed = urlsplit(local_url)
    return urlunsplit((parsed.scheme, parsed.netloc, _REVIEW_BOOTSTRAP_PATH, "", ""))


def _is_safe_bootstrap_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and _is_numeric_loopback_host(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == _REVIEW_BOOTSTRAP_PATH
        and not parsed.query
        and not parsed.fragment
        and port is not None
    )


def _review_page_url(local_url: str, nonce: str) -> str:
    if not is_valid_bootstrap_nonce(nonce):
        _raise_review_unavailable()
    parsed = urlsplit(local_url)
    return urlunsplit((parsed.scheme, parsed.netloc, _REVIEW_PAGE_PATH, "", nonce))


def _request_review_bootstrap(endpoint: str, control_secret: str) -> str:
    if not _is_safe_bootstrap_endpoint(endpoint):
        _raise_review_unavailable()
    parsed = urlsplit(endpoint)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    request = urllib.request.Request(  # noqa: S310 - endpoint is derived from validated state
        endpoint,
        data=b"",
        method="POST",
        headers={
            "Accept": "application/json",
            "Host": parsed.netloc,
            "Origin": origin,
            "X-Moco-Control-Secret": control_secret,
        },
    )
    status = 0
    body = b""
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirectHandler(),
        )
        with opener.open(
            request,
            timeout=5.0,
        ) as response:
            status = response.status
            body = response.read(_MAX_BOOTSTRAP_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError):
        _raise_review_unavailable()
    if status != _HTTP_OK or len(body) > _MAX_BOOTSTRAP_RESPONSE_BYTES:
        _raise_review_unavailable()
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise_review_unavailable()
    if (
        type(payload) is not dict
        or set(payload) != {"nonce"}
        or not is_valid_bootstrap_nonce(payload.get("nonce"))
    ):
        _raise_review_unavailable()
    return cast("str", payload["nonce"])


def _exit_service(error: LaunchdError) -> NoReturn:
    typer.echo(f"ERROR [service]: {error}")
    raise typer.Exit(code=1)


def _exit_unsupported_service() -> NoReturn:
    typer.echo("ERROR [service]: unsupported_platform")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
