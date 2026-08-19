from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tokenize
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_global_test_fixtures_do_not_disable_windows_config_acl_validation() -> None:
    path = ROOT / "tests" / "conftest.py"
    conftest = path.read_text(encoding="utf-8") if path.exists() else ""

    assert "_validate_windows_config_path" not in conftest


def test_frontend_recipe_supports_one_optional_focused_pattern() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    recipe = (
        'test-frontend pattern="":\n'
        '    node --test {{ if pattern == "" { "" } else { '
        '"--test-name-pattern=" + quote(pattern) } }} tests/js/*.test.js'
    )

    assert justfile.count('test-frontend pattern="":') == 1
    assert recipe in justfile


def test_agent_instruction_is_tracked_as_a_symlink() -> None:
    git = shutil.which("git")
    assert git is not None
    entry = subprocess.run(  # noqa: S603
        [git, "ls-files", "-s", "CLAUDE.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert entry.startswith("120000 ")
    target = subprocess.run(  # noqa: S603
        [git, "show", ":CLAUDE.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert target.strip() == "AGENTS.md"


def test_public_examples_do_not_contain_private_endpoints() -> None:
    example = (ROOT / "config" / "moco.example.yaml").read_text(encoding="utf-8")
    assert "https://windows-node.example.ts.net" in example
    assert "connect_ip: null" in example
    assert "prompt_file: null" in example
    assert "100." not in example
    prompt_example = (ROOT / "config" / "moco.prompt.example.md").read_text(
        encoding="utf-8",
    )
    assert prompt_example.strip()
    assert "Irodori-supported emoji" in prompt_example


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
    assert "codex.prompt_file" in readme
    for stage_b_constraint in [
        "Windows 11",
        "codex.command: null",
        "APPDATA",
        "LOCALAPPDATA",
        "unsupported_platform",
        "foreground",
        "read_only",
        "workspace_write",
        "inherit_codex",
        "moco review",
    ]:
        assert stage_b_constraint in readme
    assert "Agent handoff と\napproval UI はまだ利用できません" not in readme


def test_readme_documents_strict_codex_command_migration() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for migration_detail in [
        "`codex.binary`",
        "削除",
        'command: ["/absolute/path/to/codex"]',
        "command: null",
        "後方互換",
    ]:
        assert migration_detail in readme


def test_readme_documents_host_specific_prompt_paths_and_creation_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    prompt = readme.split("### GPTの応答スタイルを変更する", maxsplit=1)[1].split(
        "## スマートフォンから使う", maxsplit=1
    )[0]
    for path in [r"`~/.moco/prompt.md`", r"`%APPDATA%\moco\prompt.md`"]:
        assert path in prompt
    for command in [
        "mkdir -p ~/.moco",
        "cp config/moco.prompt.example.md ~/.moco/prompt.md",
        'New-Item -ItemType Directory -Force "$env:APPDATA\\moco"',
        'Copy-Item config\\moco.prompt.example.md "$env:APPDATA\\moco\\prompt.md"',
    ]:
        assert command in prompt


def test_readme_documents_stage_b_interaction_and_privacy_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    stage_b = readme.split("### macOS / Windows Stage B", maxsplit=1)[1].split(
        "## 最短の起動手順", maxsplit=1
    )[0]
    for boundary in [
        "foreground",
        "read_only",
        "workspace_write",
        "inherit_codex",
        "moco review",
        "ローカル",
        "公開画面",
        "fail-closed",
    ]:
        assert boundary in stage_b

    interaction = readme.split("### Codex作業、取消、再接続", maxsplit=1)[1].split(
        "### GPTの応答スタイルを変更する", maxsplit=1
    )[0]
    for behavior in [
        "delegation.created",
        "acknowledgement",
        "speakable progress",
        "final",
        "新しい発話",
        "取消",
        "interrupt",
        "自動再送",
    ]:
        assert behavior in interaction
    assert "処理しています。" not in interaction
    assert "中間音声は発しません" not in interaction
    assert "Realtime 側の自然な割り込み" not in readme

    privacy = readme.split("## プライバシーと観測", maxsplit=1)[1].split("## 開発", maxsplit=1)[0]
    for private_content in [
        "ReasoningSummary",
        "本文は表示しません",
        "コマンド本文",
        "ファイルパス",
        "patch",
        "MCP arguments",
        "approval payload",
    ]:
        assert private_content in privacy
    for runtime_boundary in [
        "runtime.json",
        "media capability",
        "control secret",
        "owner-private",
        "プロセス終了時に削除",
        "stdout",
        "browser storage",
    ]:
        assert runtime_boundary in privacy
    assert "patch本文は表示しません" in privacy
    assert "コマンド、cwd、path、change kind、move target" in privacy


def test_readme_documents_browser_observation_and_separate_reviewer_roles() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    browser = readme.split("## 何が常駐するのか", maxsplit=1)[1].split("## 必要なもの", maxsplit=1)[
        0
    ]
    for role in ["文字起こし", "safe progress", "状態", "turn全体の取消"]:
        assert role in browser
    assert "Reviewer" in browser
    assert "loopback-only" in browser

    mobile = readme.split("## スマートフォンから使う", maxsplit=1)[1].split(
        "## Irodori の接続先", maxsplit=1
    )[0]
    assert "runtime.json" in mobile
    assert "唯一のファイル" in mobile


def test_readme_documents_stage_b_verification_without_claiming_live_acceptance() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    development = readme.split("## 開発", maxsplit=1)[1]
    for command in ["just test-python", "just contract-codex", "just check"]:
        assert command in development
    for host in ["macOS", "Windows"]:
        assert host in development
    assert "実機acceptanceの代替にはなりません" in development


def test_readme_documents_current_codex_requirements_and_doctor_codes() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`~/Library/Application Support/moco/moco.yaml`" in readme
    assert r"`%APPDATA%\moco\moco.yaml`" in readme

    requirements = readme.split("## 必要なもの", maxsplit=1)[1].split(
        "### macOS / Windows Stage B", maxsplit=1
    )[0]
    for requirement in [
        "macOS-first",
        "Windows 11",
        "Edge",
        "公開 Codex CLI",
        "macOS Input Monitoring",
        "Windows ではブラウザ",
    ]:
        assert requirement in requirements

    operator_design = (
        ROOT / "docs" / "superpowers" / "specs" / "2026-07-31-operator-console-design.md"
    ).read_text(encoding="utf-8")
    assert "reasoning summary の本文は表示しない" in operator_design
    assert "reasoning summary 更新中: 取得した短い要約" not in operator_design
    assert "reasoning summary の短い要約" not in operator_design

    doctor = readme.split("## `doctor` の見方", maxsplit=1)[1].split(
        "## プライバシーと観測", maxsplit=1
    )[0]
    for code in [
        "codex_profile",
        "codex_command",
        "codex_schema",
        "codex_account",
        "codex_policy",
        "codex_agent_admission",
        "codex_local_review",
        "codex_realtime",
        "codex_interrupt",
        "codex_server_requests",
    ]:
        assert f"`{code}`" in doctor

    for obsolete in ["codex_binary", "codex_features", "codex_voices"]:
        assert obsolete not in doctor
    assert "ChatGPT.app に同梱された Codex と" not in requirements
    assert "ChatGPT.app の状態" not in doctor

    stage_b = readme.split("### macOS / Windows Stage B", maxsplit=1)[1].split(
        "## 最短の起動手順", maxsplit=1
    )[0]
    assert (
        "`read_only` と `workspace_write` は global Codex policy を admission 条件にしません"
        in stage_b
    )
    assert "`inherit_codex` だけが global Codex policy を継承します" in stage_b

    hotkeys_row = next(line for line in doctor.splitlines() if "`hotkeys`" in line)
    assert "macOS Input Monitoring" in hotkeys_row
    assert "Windows browser fallback" in hotkeys_row


def test_ci_uses_one_full_gate_and_two_os_python_matrix() -> None:
    payload = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    jobs = payload["jobs"]
    full_gate_owners = [
        name
        for name, job in jobs.items()
        if any("just check" in str(step.get("run", "")) for step in job.get("steps", []))
    ]
    assert full_gate_owners == ["quality"]
    assert jobs["quality"]["runs-on"] == "ubuntu-latest"
    platform_job = jobs["python-platform"]
    matrix = platform_job["strategy"]["matrix"]["include"]
    assert {entry["os"] for entry in matrix} == {"macos-latest", "windows-latest"}
    rendered = yaml.safe_dump(platform_job)
    assert "just test-python" in rendered
    assert "setup-node" not in rendered
    assert "npm " not in rendered
    assert "playwright" not in rendered.casefold()


def test_contract_marker_recipe_and_coverage_exclusion_are_registered() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = project["tool"]["pytest"]["ini_options"]
    assert "not integration and not live and not slow and not contract" in pytest_options["addopts"]
    assert any(marker.startswith("contract:") for marker in pytest_options["markers"])

    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    assert (
        'uv run pytest -m "not live and not slow and not contract" --cov '
        "--cov-report=term-missing --cov-report=xml"
    ) in justfile
    assert "contract-codex:" in justfile
    assert "uv run pytest -m contract tests/test_codex_contract.py --durations=10" in justfile


def test_package_metadata_advertises_windows_support() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "Operating System :: Microsoft :: Windows" in project["project"]["classifiers"]


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
    assert "npx playwright install --with-deps chromium webkit" in ci
    ci_payload = yaml.safe_load(ci)
    assert ci_payload["jobs"]["quality"]["env"]["PYNPUT_BACKEND"] == "dummy"
    assert "uv build" in release
    assert "actions/upload-artifact" in release


def test_migration_plan_uses_a_portable_irodori_checkout_path() -> None:
    plan = (
        ROOT
        / "docs"
        / "superpowers"
        / "plans"
        / "2026-08-04-irodori-capability-client-migration.md"
    ).read_text(encoding="utf-8")

    assert "/Users/" not in plan
    assert "IRODORI_CONTRACT_REPO:?" in plan


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
