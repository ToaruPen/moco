# Configurable Realtime Prompt Implementation Plan

> **Status:** Completed. This file is a non-executable historical record of the original
> TDD sequence. Do not run its steps or copy its intermediate code snippets. The design
> specification and current implementation are authoritative.

**Goal:** `codex.prompt_file`または`~/.moco/prompt.md`からRealtimeプロンプトを会話開始ごとに安全に読み込み、次の会話から編集内容を反映できるようにする。

**Architecture:** `CodexSettings`は任意の明示パスだけを保持し、暗黙の既定パスは`default_prompt_path()`で解決する。`CodexRealtimeSession.start()`はRPC開始前に小さな専用helperで最大64 KiBのUTF-8本文を読み、暗黙ファイル欠損時だけ内蔵プロンプトへfallbackする。既存のsession、Irodori、browser境界は変更しない。

**Tech Stack:** Python 3.13、Pydantic v2、pytest、Typer、YAML、`uv`/`just`

---

The implementation originally proceeded without commit authorization and preserved the
pre-existing worktree changes in `src/moco/codex/session.py` and
`tests/test_codex_session.py`. Publishing was authorized separately after implementation.

Post-review hardening in the final implementation intentionally supersedes the snippets
below:

- Prompt path validation converts `expanduser()` failures into configuration errors and
  rejects NUL characters.
- Prompt loading wraps both `OSError` and `ValueError` as `CodexPromptError`.
- Prompt decoding uses `utf-8-sig`, accepts a leading BOM without forwarding it, and
  rejects BOM-only content as blank.
- Regression tests cover each of these boundaries before Codex RPC startup.

## File structure

- Modify `src/moco/config.py`: `codex.prompt_file`の型、`~`展開、絶対パス検証、暗黙既定パスのowner。
- Modify `src/moco/errors.py`: prompt本文を含めない`CodexPromptError`境界。
- Modify `src/moco/codex/session.py`: prompt fileのbounded readとsession開始時の解決。
- Modify `tests/test_config.py`: 設定境界のRED/GREEN tests。
- Modify `tests/test_codex_session.py`: fallback、再読込、failure-before-RPCのRED/GREEN tests。
- Create `config/moco.prompt.example.md`: 現在の内蔵プロンプトと同じ編集用template。
- Modify `config/moco.example.yaml`: `codex.prompt_file`設定例。
- Modify `README.md`: 暗黙パス、任意パス、反映タイミング、validation rules。
- Modify `tests/test_repository_contract.py`: 公開設定例とtemplateのrepository contract。

### Task 1: Strict prompt path configuration

**Files:**
- Modify: `tests/test_config.py`
- Modify: `src/moco/config.py`

- [ ] **Step 1: Write failing configuration tests**

`tests/test_config.py`へ、暗黙既定パス、`~`展開、絶対パス受理、相対パス拒否を追加する。

```python
from moco.config import CodexSettings, default_prompt_path


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
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_config.py -k 'prompt_file or default_prompt_path' -v
```

Expected: collection/import or assertions fail because `default_prompt_path` and `CodexSettings.prompt_file` do not exist.

- [ ] **Step 3: Implement the minimal strict configuration**

`src/moco/config.py`へ暗黙パス関数とfieldを追加し、path validatorを分離する。

```python
def default_prompt_path() -> Path:
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".moco" / "prompt.md"


class CodexSettings(StrictSettings):
    binary: Path = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    working_directory: Path = Field(default_factory=Path.cwd)
    prompt_file: Path | None = None

    @field_validator("prompt_file", mode="before")
    @classmethod
    def _expand_prompt_file(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            return Path(value).expanduser()
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
```

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: all `tests/test_config.py` tests pass.

### Task 2: Bounded prompt loading at conversation start

**Files:**
- Modify: `tests/test_codex_session.py`
- Modify: `src/moco/errors.py`
- Modify: `src/moco/codex/session.py`

- [ ] **Step 1: Make existing session fixtures independent from the real home directory**

Change `make_settings()` so every existing test uses an explicit temporary prompt containing `DEFAULT_REALTIME_PROMPT`.

```python
def make_settings(tmp_path: Path) -> MocoSettings:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(DEFAULT_REALTIME_PROMPT, encoding="utf-8")
    return MocoSettings(
        codex=CodexSettings(
            binary=tmp_path / "unused-codex",
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
```

- [ ] **Step 2: Write failing happy-path and reload tests**

Add tests that prove implicit fallback, implicit file use, configured file use, and rereading for a new session.

```python
async def _started_prompt(rpc: FakeRpc, settings: MocoSettings) -> str:
    session = CodexRealtimeSession(rpc, settings=settings)
    await session.start("offer-sdp")
    prompt = cast("str", rpc.requests[1][1]["prompt"])
    await session.close()
    return prompt


async def test_uses_built_in_prompt_when_implicit_file_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert await _started_prompt(FakeRpc(), MocoSettings()) == DEFAULT_REALTIME_PROMPT


async def test_reads_implicit_dot_moco_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    prompt_file = tmp_path / ".moco" / "prompt.md"
    prompt_file.parent.mkdir()
    prompt_file.write_text("Implicit persona", encoding="utf-8")

    assert await _started_prompt(FakeRpc(), MocoSettings()) == "Implicit persona"


async def test_reads_configured_prompt_again_for_each_new_session(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("First persona", encoding="utf-8")
    settings = MocoSettings(
        codex=CodexSettings(
            binary=tmp_path / "unused-codex",
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )

    first = await _started_prompt(FakeRpc(), settings)
    prompt_file.write_text("Second persona", encoding="utf-8")
    second = await _started_prompt(FakeRpc(), settings)

    assert (first, second) == ("First persona", "Second persona")
```

- [ ] **Step 3: Run happy-path tests and verify RED**

Run:

```bash
uv run pytest tests/test_codex_session.py -k 'implicit_dot_moco_prompt or built_in_prompt or reads_configured_prompt_again' -v
```

Expected: custom prompt assertions fail because the session still sends `DEFAULT_REALTIME_PROMPT`.

- [ ] **Step 4: Write failing invalid-file tests**

Add one parametrized byte-content test plus missing/directory tests. Every assertion must also verify `rpc.started is False` and `rpc.requests == []`.

```python
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b" \n\t", "blank"),
        (b"\xff", "UTF-8"),
        (b"x" * 65_537, "64 KiB"),
    ],
)
async def test_rejects_invalid_prompt_before_rpc_start(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_bytes(payload)
    settings = MocoSettings(
        codex=CodexSettings(
            binary=tmp_path / "unused-codex",
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
    rpc = FakeRpc()

    with pytest.raises(CodexPromptError, match=message):
        await CodexRealtimeSession(rpc, settings=settings).start("offer-sdp")

    assert rpc.started is False
    assert rpc.requests == []


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_rejects_unreadable_configured_prompt_before_rpc_start(
    tmp_path: Path,
    kind: str,
) -> None:
    prompt_file = tmp_path / "prompt.md"
    if kind == "directory":
        prompt_file.mkdir()
    settings = MocoSettings(
        codex=CodexSettings(
            binary=tmp_path / "unused-codex",
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
    rpc = FakeRpc()

    with pytest.raises(CodexPromptError):
        await CodexRealtimeSession(rpc, settings=settings).start("offer-sdp")

    assert rpc.started is False
    assert rpc.requests == []
```

- [ ] **Step 5: Run invalid-file tests and verify RED**

Run:

```bash
uv run pytest tests/test_codex_session.py -k 'invalid_prompt or unreadable_configured_prompt' -v
```

Expected: import/behavior failures because `CodexPromptError` and bounded prompt loading do not exist.

- [ ] **Step 6: Implement the prompt error and bounded loader**

Add to `src/moco/errors.py`:

```python
class CodexPromptError(CodexError):
    """A local Realtime prompt file could not be used safely."""
```

Add to `src/moco/codex/session.py`, importing `default_prompt_path` and `CodexPromptError`:

```python
_MAX_REALTIME_PROMPT_BYTES = 65_536


def _load_realtime_prompt(settings: MocoSettings) -> str:
    configured = settings.codex.prompt_file
    path = configured or default_prompt_path()
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_REALTIME_PROMPT_BYTES + 1)
    except FileNotFoundError as error:
        if configured is None:
            return DEFAULT_REALTIME_PROMPT
        raise CodexPromptError("configured realtime prompt file was not found") from error
    except OSError as error:
        raise CodexPromptError("realtime prompt file could not be read") from error
    if len(payload) > _MAX_REALTIME_PROMPT_BYTES:
        raise CodexPromptError("realtime prompt file exceeds 64 KiB")
    try:
        prompt = payload.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise CodexPromptError("realtime prompt file must be UTF-8") from error
    if not prompt:
        raise CodexPromptError("realtime prompt file must not be blank")
    return prompt
```

At the start of `CodexRealtimeSession.start()`, after state/SDP guards but before `self._started = True` and `await self._rpc.start()`, resolve once for that conversation:

```python
prompt = _load_realtime_prompt(self._settings)
```

Use `prompt` in the `thread/realtime/start` request.

- [ ] **Step 7: Run the complete session tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_codex_session.py -v
```

Expected: all `tests/test_codex_session.py` tests pass, including existing dirty-worktree behavior.

### Task 3: Public configuration and documentation

**Files:**
- Create: `config/moco.prompt.example.md`
- Modify: `config/moco.example.yaml`
- Modify: `README.md`
- Modify: `tests/test_repository_contract.py`

- [ ] **Step 1: Write the failing repository contract test**

Extend `test_agent_instruction_link_and_public_example()`:

```python
    assert "prompt_file: null" in example
    prompt_example = (ROOT / "config" / "moco.prompt.example.md").read_text(
        encoding="utf-8",
    )
    assert prompt_example.strip()
    assert "Irodori-supported emoji" in prompt_example
```

Extend `test_readme_documents_golden_path_and_browser_boundary()`:

```python
    assert "~/.moco/prompt.md" in readme
    assert "codex.prompt_file" in readme
```

- [ ] **Step 2: Run repository contract tests and verify RED**

Run:

```bash
uv run pytest tests/test_repository_contract.py -k 'agent_instruction or readme_documents' -v
```

Expected: FAIL because the YAML key, prompt template, and README guidance are absent.

- [ ] **Step 3: Add the prompt template and configuration example**

Create `config/moco.prompt.example.md` with the current built-in prompt:

```text
Respond in short, natural Japanese suitable for speech synthesis. Use clear punctuation. Use Irodori-supported emoji only when expression requires it. Do not respond with structured JSON or Markdown.
```

Add under `codex:` in `config/moco.example.yaml`:

```yaml
  # null checks ~/.moco/prompt.md and falls back to the built-in prompt when absent.
  # Set an absolute path (or a path beginning with ~) to require another prompt file.
  prompt_file: null
```

- [ ] **Step 4: Document operator workflow in README**

After the initial configuration paragraph, document:

```markdown
GPTの応答スタイルは`~/.moco/prompt.md`へUTF-8で記述できます。ファイルがなければ
内蔵プロンプトを使います。別のファイルを使う場合は、`moco.yaml`の
`codex.prompt_file`へ絶対パスまたは`~`から始まるパスを指定してください。
内容は会話開始ごとに読み直されるため、編集後の再起動は不要で、次の会話から反映されます。
空、非UTF-8、64 KiB超、または読めない明示設定ファイルは会話開始時に拒否されます。
```

- [ ] **Step 5: Run repository contract tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_repository_contract.py -v
```

Expected: all repository contract tests pass.

### Task 4: Integration verification

**Files:**
- Verify all files listed above and preserve unrelated worktree changes.

- [ ] **Step 1: Confirm repeated identifiers landed only in intended owners**

Run:

```bash
rg -n "prompt_file|default_prompt_path|CodexPromptError|_load_realtime_prompt|moco\.prompt\.example" src tests config README.md
```

Expected: configuration identifiers are owned by `config.py`, loading/error identifiers by the Codex boundary, and documentation references by public examples/tests only.

- [ ] **Step 2: Apply deterministic formatting**

Run:

```bash
just format
```

Expected: exit 0. Review any formatting changes so unrelated user edits remain intact.

- [ ] **Step 3: Run focused behavior tests after formatting**

Run:

```bash
uv run pytest tests/test_config.py tests/test_codex_session.py tests/test_repository_contract.py -v
```

Expected: all focused tests pass with zero failures.

- [ ] **Step 4: Run the repository quality gate**

Run:

```bash
just check
```

Expected: formatting, lint, typecheck, dead-code, dependency, AST, test coverage, secret scan, and build gates all exit 0.

- [ ] **Step 5: Review final diff and status**

Run:

```bash
git diff --check
git status --short
git diff -- src/moco/config.py src/moco/errors.py src/moco/codex/session.py tests/test_config.py tests/test_codex_session.py config/moco.example.yaml config/moco.prompt.example.md README.md tests/test_repository_contract.py docs/superpowers/specs/2026-08-05-configurable-realtime-prompt-design.md docs/superpowers/plans/2026-08-05-configurable-realtime-prompt.md
```

Expected: no whitespace errors; only requested prompt configuration changes are added on top of preserved pre-existing worktree changes. Do not commit.
