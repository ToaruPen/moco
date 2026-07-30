from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.parse import urlsplit

import typer
import uvicorn
import yaml

from moco.config import ConfigError, MocoSettings, default_config_path, load_config
from moco.doctor import run_doctor
from moco.runtime.hotkeys import GlobalHotkeyListener, HotkeyMapper
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

app = typer.Typer(no_args_is_help=True, help="moco local voice agent")
config_app = typer.Typer(no_args_is_help=True, help="Manage strict YAML configuration.")
service_app = typer.Typer(no_args_is_help=True, help="Manage the user launchd service.")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")


def default_state_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "moco"
        / "runtime.json"
    )


_DEFAULT_CONFIG_PATH = default_config_path()
_DEFAULT_STATE_PATH = default_state_path()


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
    _atomic_write(path, rendered.encode())
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
    state_path: Annotated[Path, typer.Option("--state-path")] = _DEFAULT_STATE_PATH,
) -> None:
    """Run the foreground daemon and loopback operator server."""
    settings = _load_or_exit(config)
    asyncio.run(_run_runtime(settings, state_path=state_path))


@app.command("open")
def open_command(
    *,
    state_path: Annotated[Path, typer.Option("--state-path")] = _DEFAULT_STATE_PATH,
) -> None:
    """Open the active operator page without printing its capability."""
    try:
        url = _read_state_url(state_path)
    except (FileNotFoundError, KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        typer.echo("ERROR [runtime_state]: no safe running moco instance was found")
        raise typer.Exit(code=1) from error
    webbrowser.open(url)
    typer.echo("operator page opened")


@service_app.command("install")
def service_install_command(
    *,
    config: Annotated[Path, typer.Option("--config")] = _DEFAULT_CONFIG_PATH,
    executable: Annotated[Path | None, typer.Option("--executable")] = None,
) -> None:
    """Install the exact moco user LaunchAgent."""
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
    try:
        start_service()
    except LaunchdError as error:
        _exit_service(error)
    typer.echo("service start requested")


@service_app.command("stop")
def service_stop_command() -> None:
    """Stop the moco user LaunchAgent."""
    try:
        stop_service()
    except LaunchdError as error:
        _exit_service(error)
    typer.echo("service stop requested")


@service_app.command("status")
def service_status_command() -> None:
    """Report whether the service is missing, stopped, or running."""
    typer.echo(read_service_status().value)


@service_app.command("uninstall")
def service_uninstall_command(
    *,
    executable: Annotated[Path | None, typer.Option("--executable")] = None,
) -> None:
    """Remove only the exact moco LaunchAgent."""
    resolved_executable = executable or Path(sys.argv[0]).resolve()
    try:
        uninstall_service(executable=resolved_executable)
    except LaunchdError as error:
        _exit_service(error)
    typer.echo("service uninstalled")


async def _run_runtime(settings: MocoSettings, *, state_path: Path) -> None:
    telemetry = configure_telemetry(settings.telemetry)
    capability_value = secrets.token_urlsafe(32)
    operator_app = create_app(settings, capability_token=capability_value)
    loop = asyncio.get_running_loop()
    mapper = HotkeyMapper(
        ptt_key=settings.hotkeys.push_to_talk,
        cancel_key=settings.hotkeys.cancel,
        emit=lambda control: asyncio.create_task(
            operator_app.state.control_hub.publish(control),
        ),
    )
    listener = GlobalHotkeyListener(loop=loop, mapper=mapper)
    if settings.hotkeys.enabled:
        try:
            listener.start()
        except (OSError, RuntimeError):
            typer.echo("WARN [hotkeys]: Input Monitoring permission may be required")

    server = uvicorn.Server(
        uvicorn.Config(
            operator_app,
            host=settings.server.host,
            port=settings.server.port,
            log_config=None,
        ),
    )
    task = asyncio.create_task(server.serve(), name="moco-uvicorn")
    try:
        while not server.started and not task.done():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        if not server.started:
            await task
            return
        url = (
            f"http://{settings.server.host}:{settings.server.port}/"
            f"#{capability_value}"
        )
        _atomic_write(state_path, json.dumps({"url": url}).encode())
        typer.echo(
            f"moco is ready on {settings.server.host}:{settings.server.port}; "
            "run `moco open`"
        )
        await task
    finally:
        listener.stop()
        telemetry.close()
        await asyncio.to_thread(_remove_state_file, state_path)


def _load_or_exit(path: Path) -> MocoSettings:
    try:
        return load_config(path)
    except ConfigError as error:
        typer.echo(f"ERROR [configuration]: {error}")
        raise typer.Exit(code=1) from error


def _is_safe_operator_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.username is None
        and parsed.password is None
        and bool(parsed.fragment)
        and not parsed.query
    )


def _read_state_url(state_path: Path) -> str:
    if state_path.stat().st_mode & 0o077:
        message = "runtime state permissions are not private"
        raise ValueError(message)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    url = payload["url"]
    if not isinstance(url, str) or not _is_safe_operator_url(url):
        message = "runtime state contains an invalid operator URL"
        raise ValueError(message)
    return url


def _remove_state_file(state_path: Path) -> None:
    if state_path.exists():
        state_path.unlink()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _exit_service(error: LaunchdError) -> NoReturn:
    typer.echo(f"ERROR [service]: {error}")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
