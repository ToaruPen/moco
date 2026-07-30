from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_agent_instruction_link_and_public_example() -> None:
    claude = ROOT / "CLAUDE.md"
    assert claude.is_symlink()
    assert claude.readlink() == Path("AGENTS.md")

    example = (ROOT / "config" / "moco.example.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:8923" in example
    assert "100." not in example


def test_readme_documents_golden_path_and_browser_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in [
        "just sync",
        "uv run moco config init",
        "uv run moco config validate",
        "uv run moco doctor",
        "uv run moco run",
        "uv run moco open",
    ]:
        assert command in readme
    assert "ブラウザは常駐本体ではありません" in readme
    assert "Input Monitoring" in readme
    assert "experimental" in readme.lower()


def test_workflows_have_minimal_permissions_concurrency_and_pinned_actions() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert {path.name for path in workflows} == {"ci.yml", "release.yml"}
    action_pattern = re.compile(
        r"^\s*(?:-\s*)?uses:\s*[^@\s]+@([0-9a-f]{40})\s*(?:#.*)?$",
    )
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        payload = yaml.safe_load(text)
        assert payload["permissions"] == {"contents": "read"}
        assert payload["concurrency"]["cancel-in-progress"] is True
        uses_lines = [line for line in text.splitlines() if "uses:" in line]
        assert uses_lines
        assert all(action_pattern.match(line) for line in uses_lines)

    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8",
    )
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "extractions/setup-just@" in ci
    ci_payload = yaml.safe_load(ci)
    assert ci_payload["jobs"]["quality"]["env"]["PYNPUT_BACKEND"] == "dummy"
    assert "uv build" in release
    assert "actions/upload-artifact" in release


def test_just_check_covers_every_repository_gate() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    for gate in [
        "format-check",
        "lint",
        "typecheck",
        "dead-code",
        "dependencies",
        "ast-grep",
        "test-cov",
        "secret-scan",
        "build",
    ]:
        assert gate in justfile


def test_source_has_no_placeholder_comments_or_pass_only_handlers() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                assert not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)), path
        with path.open("rb") as stream:
            comments = [
                token.string.lower()
                for token in tokenize.tokenize(stream.readline)
                if token.type == tokenize.COMMENT
            ]
        assert not any(
            marker in comment for comment in comments for marker in ("todo", "fixme", "placeholder")
        ), path


def test_local_configuration_and_state_are_not_tracked() -> None:
    git = shutil.which("git")
    assert git is not None
    tracked = subprocess.run(  # noqa: S603
        [git, "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert "config/moco.yaml" not in tracked
    assert not any(path.endswith("runtime.json") for path in tracked)
