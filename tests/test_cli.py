from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from moco.cli import app
from moco.config import load_config

runner = CliRunner()


def test_config_init_is_non_destructive_and_generated_yaml_validates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "moco.yaml"

    first = runner.invoke(app, ["config", "init", "--path", str(path)])
    second = runner.invoke(app, ["config", "init", "--path", str(path)])

    assert first.exit_code == 0
    assert second.exit_code == 1
    assert "exists" in second.output
    assert load_config(path).hotkeys.push_to_talk == "f1"
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)

    forced = runner.invoke(
        app,
        ["config", "init", "--path", str(path), "--force"],
    )
    assert forced.exit_code == 0


def test_config_validate_and_public_command_surface(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("{}\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "validate", "--path", str(path)])
    help_result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "valid" in result.output
    for command in ["config", "doctor", "run", "open", "service"]:
        assert command in help_result.output


def test_open_uses_private_state_without_printing_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime.json"
    capability_value = "private-capability"
    state_path.write_text(
        json.dumps({"url": f"http://127.0.0.1:8765/#{capability_value}"}),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    opened: list[str] = []
    monkeypatch.setattr("moco.cli.webbrowser.open", opened.append)

    result = runner.invoke(app, ["open", "--state-path", str(state_path)])

    assert result.exit_code == 0
    assert capability_value not in result.output
    assert opened == [f"http://127.0.0.1:8765/#{capability_value}"]
