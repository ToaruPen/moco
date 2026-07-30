# moco First Usable Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a public macOS push-to-talk voice agent that uses Codex Realtime
for conversation and work, Irodori for speech, configurable semantic controls, an
idle-expiring conversation lifecycle, YAML configuration, safe telemetry, and
launchd service controls.

**Architecture:** A typed Python 3.13 runtime owns configuration, global
hotkeys, lifecycle, Codex App Server, Irodori, and a loopback operator server.
A capability-bound browser companion owns the proven WebRTC microphone path and
WAV playback. External boundaries are protocols with fake implementations in
normal tests.

**Tech Stack:** Python 3.13, uv, FastAPI, Pydantic, PyYAML, Typer, pynput,
OpenTelemetry, httpx, browser WebRTC/Web Audio, pytest, Ruff, mypy, vulture,
deptry, Biome, secretlint, ast-grep, GitHub Actions.

---

## File Map

```text
AGENTS.md                               repository operating contract
CLAUDE.md                               symlink to AGENTS.md
LICENSE                                 MIT license
README.md                               user setup and golden path
SECURITY.md                             local security/privacy boundary
pyproject.toml                          package, dependencies, Python tools
uv.lock                                 reproducible Python resolution
package.json / package-lock.json        browser and repository quality tools
biome.json                              browser formatting/linting
sgconfig.yml / rules/*.yml              structural anti-slop rules
justfile                                one local/CI command surface
config/moco.example.yaml                documented strict YAML example
.github/workflows/ci.yml                Ubuntu and macOS quality gates
.github/workflows/release.yml           tag-triggered build artifacts
src/moco/config.py                      strict YAML loading and path resolution
src/moco/errors.py                      stable safe error types/codes
src/moco/codex/rpc.py                   stdio JSON-RPC process boundary
src/moco/codex/session.py               thread-scoped Realtime adapter
src/moco/speech/text.py                 transcript cleanup/segmentation
src/moco/speech/irodori.py              bounded Irodori HTTP adapter
src/moco/speech/queue.py                generation-aware synthesis queue
src/moco/runtime/hotkeys.py             configurable key source and de-duplication
src/moco/runtime/lifecycle.py           activity/state/idle controller
src/moco/runtime/telemetry.py           redacted logs and OpenTelemetry
src/moco/service/launchd.py             exact user LaunchAgent management
src/moco/web/messages.py                browser message contracts
src/moco/web/app.py                     loopback HTTP/WebSocket orchestration
src/moco/web/static/index.html          operator page
src/moco/web/static/app.js              WebRTC/PTT/playback controller
src/moco/web/static/styles.css          compact operator presentation
src/moco/doctor.py                      Codex/Irodori/hotkey diagnostics
src/moco/cli.py                         public CLI
tests/                                  unit and mocked boundary tests
```

### Task 1: Repository and quality baseline

**Files:**

- Create: `.gitignore`
- Create: `AGENTS.md`
- Create: `CLAUDE.md` symlink
- Create: `LICENSE`
- Create: `pyproject.toml`
- Create: `package.json`
- Create: `biome.json`
- Create: `justfile`
- Create: `src/moco/__init__.py`
- Create: `tests/test_package.py`

- [ ] **Step 1: Write the failing package metadata test**

```python
from importlib.metadata import version


def test_distribution_and_import_use_moco_name() -> None:
    import moco

    assert moco.__version__ == version("moco")
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_package.py -q`

Expected: collection fails because `moco` has not been packaged.

- [ ] **Step 3: Add the package and pinned quality baseline**

Create a Python 3.13 hatchling project named `moco`, version `0.1.0`, with
runtime dependencies for FastAPI, httpx, the proven pinned
`irodori-tts-infra` commit, OpenTelemetry, Pydantic, PyYAML, pynput, Typer, and
Uvicorn. Add the dev tools listed in the design. Configure Ruff `ALL`, mypy
strict, pytest strict markers, branch coverage at 90%, and vulture at 80%
confidence.

`src/moco/__init__.py` must contain:

```python
from importlib.metadata import version

__version__ = version("moco")
```

Make `CLAUDE.md` exactly a relative symlink to `AGENTS.md`:

```bash
ln -s AGENTS.md CLAUDE.md
```

- [ ] **Step 4: Resolve dependencies and verify GREEN**

Run:

```bash
uv lock
uv sync
uv run pytest tests/test_package.py -q
```

Expected: one passing test.

- [ ] **Step 5: Commit**

```bash
git add .gitignore AGENTS.md CLAUDE.md LICENSE pyproject.toml uv.lock \
  package.json biome.json justfile src/moco/__init__.py tests/test_package.py
git commit -m "build: establish moco repository baseline"
```

### Task 2: Strict YAML configuration

**Files:**

- Create: `src/moco/config.py`
- Create: `config/moco.example.yaml`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for defaults, overrides, and rejection**

Tests must establish:

```python
def test_load_config_applies_yaml_values(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(
        "runtime:\n  idle_timeout_seconds: 42\n"
        "irodori:\n  base_url: http://100.64.0.1:8923\n",
        encoding="utf-8",
    )

    settings = load_config(path)

    assert settings.runtime.idle_timeout_seconds == 42
    assert str(settings.irodori.base_url) == "http://100.64.0.1:8923/"


def test_load_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("runtime:\n  mystery: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="runtime.mystery"):
        load_config(path)


def test_operator_server_must_bind_loopback(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("server:\n  host: 0.0.0.0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="loopback"):
        load_config(path)
```

Also cover positive timeouts, distinct control bindings, absolute Codex/cwd paths, no URL
credentials, synthesis ranges, safe OTLP URL schemes, and default config path.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_config.py -q`

Expected: import failure for `moco.config`.

- [ ] **Step 3: Implement nested frozen Pydantic models and YAML loading**

Use `ConfigDict(extra="forbid", frozen=True)`. Convert `yaml.safe_load()` parse
errors and Pydantic error locations into `ConfigError` messages without
including raw YAML values. Define:

```python
class MocoSettings(BaseModel):
    server: ServerSettings = ServerSettings()
    hotkeys: HotkeySettings = HotkeySettings()
    runtime: RuntimeSettings = RuntimeSettings()
    codex: CodexSettings = CodexSettings()
    irodori: IrodoriSettings = IrodoriSettings()
    speech: SpeechSettings = SpeechSettings()
    telemetry: TelemetrySettings = TelemetrySettings()
```

Default Irodori URL stays localhost in the public example; the local generated
config is later initialized with the user's Tailscale IP.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/test_config.py -q`

Expected: all configuration tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/moco/config.py config/moco.example.yaml tests/test_config.py
git commit -m "feat: add strict YAML configuration"
```

### Task 3: Codex JSON-RPC and Realtime adapter

**Files:**

- Create: `src/moco/errors.py`
- Create: `src/moco/codex/__init__.py`
- Create: `src/moco/codex/rpc.py`
- Create: `src/moco/codex/session.py`
- Create: `tests/fixtures/fake_codex.py`
- Create: `tests/test_codex_rpc.py`
- Create: `tests/test_codex_session.py`

- [ ] **Step 1: Port the proven boundary tests first**

Adapt the tests from commit
`cd1b070abcf2cc4a324c572de0a787cb50b65c9f` of
`codex-irodori-voice`, replacing the package namespace with `moco.codex`.
Retain tests for initialize ordering, request/response correlation, timeouts,
malformed JSON, process exit, bounded shutdown, stderr fingerprinting,
ephemeral read-only thread creation, Realtime v3 start, SDP, transcript
delta/done, errors, and idempotent close.

Add a cancellation test:

```python
async def test_cancel_interrupts_active_turn_and_appends_stop_request(
    fake_rpc: FakeRpc,
    settings: MocoSettings,
) -> None:
    session = CodexRealtimeSession(fake_rpc, settings=settings)
    await session.start("offer")
    fake_rpc.notify("turn/started", {"threadId": session.thread_id, "turn": {"id": "turn-1"}})

    await session.cancel_current()

    assert fake_rpc.requests[-2:] == [
        ("turn/interrupt", {"threadId": session.thread_id, "turnId": "turn-1"}),
        (
            "thread/realtime/appendText",
            {
                "threadId": session.thread_id,
                "role": "user",
                "text": "現在の応答と作業を中止してください。",
            },
        ),
    ]
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_codex_rpc.py tests/test_codex_session.py -q
```

Expected: imports fail because the adapters do not exist.

- [ ] **Step 3: Implement the adapters**

Adapt the proven process and protocol implementation. Change client identity to
`moco`, use settings from YAML, opt into `experimentalApi`, and keep
`--enable realtime_conversation`.

Track `turn/started` and `turn/completed` for the current thread. Implement
`cancel_current()` so a known active turn is interrupted and the Realtime model
receives a stop instruction. Never log RPC payloads, account identity, prompts,
or transcript text.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_codex_rpc.py tests/test_codex_session.py -q
```

Expected: all Codex boundary tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/moco/errors.py src/moco/codex tests/fixtures/fake_codex.py \
  tests/test_codex_rpc.py tests/test_codex_session.py
git commit -m "feat: integrate Codex realtime adapter"
```

### Task 4: Irodori speech pipeline and cancellation

**Files:**

- Create: `src/moco/speech/__init__.py`
- Create: `src/moco/speech/text.py`
- Create: `src/moco/speech/irodori.py`
- Create: `src/moco/speech/queue.py`
- Create: `tests/test_speech_text.py`
- Create: `tests/test_irodori.py`
- Create: `tests/test_speech_queue.py`

- [ ] **Step 1: Port proven tests and add suppression coverage**

Adapt the PoC tests for sentence splitting, speakable text, control emoji
stripping, HTTP response limits, portable speaker use, Irodori error mapping,
RIFF validation, FIFO synthesis, and generation cancellation.

Add:

```python
async def test_cancel_suppresses_assistant_until_next_user_turn(queue: SpeechQueue) -> None:
    await queue.on_transcript(role="assistant", delta="古い返事。", done=True)
    await queue.cancel()
    await queue.on_transcript(role="assistant", delta="まだ古い返事。", done=True)
    assert queue.pending_count == 0

    await queue.on_transcript(role="user", delta="次の質問", done=True)
    await queue.on_transcript(role="assistant", delta="新しい返事。", done=True)

    assert queue.pending_texts == ("新しい返事。",)
```

Also assert unexpected Irodori JSON/validation failures become the safe
`IrodoriError(code="invalid_response")` instead of killing the consumer task.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_speech_text.py tests/test_irodori.py \
  tests/test_speech_queue.py -q
```

Expected: imports fail for `moco.speech`.

- [ ] **Step 3: Implement the speech modules**

Adapt the proven bounded transport and generation-aware queue. The queue
increments its generation before cancelling work, drains stale items, and
checks generation again before delivery. `cancel()` sets suppression until a
completed user transcript. Catch all expected Irodori contract and transport
errors at the boundary and report stable codes.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: all speech tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/moco/speech tests/test_speech_text.py tests/test_irodori.py \
  tests/test_speech_queue.py
git commit -m "feat: add interruptible Irodori speech pipeline"
```

### Task 5: Lifecycle and global hotkeys

**Files:**

- Create: `src/moco/runtime/__init__.py`
- Create: `src/moco/runtime/hotkeys.py`
- Create: `src/moco/runtime/lifecycle.py`
- Create: `tests/test_hotkeys.py`
- Create: `tests/test_lifecycle.py`

- [ ] **Step 1: Write state-machine and key-debounce tests**

Define typed controls `PTT_DOWN`, `PTT_UP`, and `CANCEL`. Tests must prove:

- repeated OS key-down does not emit repeated PTT events;
- key-up without a matching down is ignored;
- cancel emits once per physical press;
- activity during recording, active delegated work, synthesis, or playback
  prevents idle expiry;
- idle expiry fires once after the configured duration;
- an expired lifecycle returns to ready on the next PTT down.

Example:

```python
def test_key_repeat_emits_one_ptt_pair() -> None:
    emitted: list[Control] = []
    mapper = HotkeyMapper(ptt_key="f1", cancel_key="f2", emit=emitted.append)

    mapper.key_down("f1")
    mapper.key_down("f1")
    mapper.key_up("f1")

    assert emitted == [Control.PTT_DOWN, Control.PTT_UP]
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_hotkeys.py tests/test_lifecycle.py -q`

Expected: imports fail for `moco.runtime`.

- [ ] **Step 3: Implement pure logic, then pynput adapter**

Keep `HotkeyMapper` independent of pynput so all behavior is deterministic.
`GlobalHotkeyListener` converts configured pynput keys into canonical strings and
uses `loop.call_soon_threadsafe` to cross from the listener thread.

`LifecycleController` uses an injected monotonic clock and async callback. It
holds activity flags rather than resetting the timer blindly. It never expires
while any busy flag is true.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: all runtime tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/moco/runtime tests/test_hotkeys.py tests/test_lifecycle.py
git commit -m "feat: add hotkeys and conversation lifecycle"
```

### Task 6: Operator server and browser push-to-talk

**Files:**

- Create: `src/moco/web/__init__.py`
- Create: `src/moco/web/messages.py`
- Create: `src/moco/web/app.py`
- Create: `src/moco/web/static/index.html`
- Create: `src/moco/web/static/app.js`
- Create: `src/moco/web/static/styles.css`
- Create: `tests/test_web_messages.py`
- Create: `tests/test_web.py`
- Create: `tests/test_integration.py`
- Create: `tests/js/app.test.js`

- [ ] **Step 1: Port secure bridge tests before code**

Adapt the PoC tests for loopback Origin/Host equality, capability subprotocol,
one-client slot, start/stop, SDP, transcript, WAV metadata+binary pairing,
generation invalidation, and cleanup.

Add server tests for:

- hotkey controls broadcast as `{"type":"control","control":"ptt_down"}`;
- cancel calls both speech cancellation and Codex cancellation;
- idle expiry closes the current call resources but keeps the WebSocket ready;
- a subsequent start creates fresh Codex and Irodori adapters.

Add JS tests:

```javascript
it("enables the microphone only while push-to-talk is held", async () => {
  await controller.applyControl("ptt_down");
  expect(controller.stream.getAudioTracks()[0].enabled).toBe(true);

  await controller.applyControl("ptt_up");
  expect(controller.stream.getAudioTracks()[0].enabled).toBe(false);
});

it("cancel disables capture and invalidates old audio", async () => {
  const generation = controller.audioGeneration;
  await controller.applyControl("cancel");

  expect(controller.stream.getAudioTracks()[0].enabled).toBe(false);
  expect(controller.audioGeneration).toBe(generation + 1);
  expect(controller.playback.isPlaying).toBe(false);
});
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_web_messages.py tests/test_web.py \
  tests/test_integration.py -q
npm test -- --run tests/js/app.test.js
```

Expected: imports/assets are missing.

- [ ] **Step 3: Implement the operator runtime**

Adapt the proven PoC modules and browser controller with these changes:

- use `moco` names and WebSocket protocol;
- acquire media only after the visible enable action;
- immediately set microphone tracks `enabled=false`;
- implement server `control` messages for PTT down/up/cancel;
- preserve the control WebSocket across idle-expired conversation teardown;
- create a fresh peer/session on the next PTT down;
- forward busy/activity changes to `LifecycleController`;
- send explicit generation invalidation to the browser whenever the server
  cancels speech;
- add ICE gathering and WebSocket open timeouts;
- never persist transcript text.

- [ ] **Step 4: Verify GREEN**

Run the commands from Step 2.

Expected: Python and browser tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/moco/web tests/test_web_messages.py tests/test_web.py \
  tests/test_integration.py tests/js/app.test.js
git commit -m "feat: add push-to-talk operator runtime"
```

### Task 7: Safe observability

**Files:**

- Create: `src/moco/runtime/telemetry.py`
- Create: `tests/test_telemetry.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/speech/queue.py`
- Modify: `src/moco/codex/session.py`

- [ ] **Step 1: Write redaction and span tests**

Prove that:

- allowed attributes include stable event code, state, duration, trace ID, and
  component;
- forbidden keys such as transcript, prompt, audio, token, capability,
  account, email, and memory raise in tests and are dropped in production;
- Irodori URLs are reduced to a non-sensitive boundary label;
- console and optional OTLP exporters are selected only from YAML.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_telemetry.py -q`

Expected: import failure for telemetry.

- [ ] **Step 3: Implement and instrument**

Expose:

```python
def configure_telemetry(settings: TelemetrySettings) -> TelemetryRuntime: ...
def safe_event(logger: logging.Logger, event: str, **attributes: Scalar) -> None: ...
```

Instrument process, operator connection, hotkeys, conversation, Codex,
Irodori, playback, cancellation, timeout, and error boundaries. Do not add
content-bearing attributes.

- [ ] **Step 4: Verify GREEN and no content-bearing instrumentation**

Run:

```bash
uv run pytest tests/test_telemetry.py -q
rg -n 'set_attribute|safe_event' src/moco
```

Expected: tests pass and every call uses an allowed field.

- [ ] **Step 5: Commit**

```bash
git add src/moco/runtime/telemetry.py src/moco/web/app.py \
  src/moco/speech/queue.py src/moco/codex/session.py tests/test_telemetry.py
git commit -m "feat: add redacted runtime telemetry"
```

### Task 8: Doctor, CLI, and launchd service

**Files:**

- Create: `src/moco/doctor.py`
- Create: `src/moco/service/__init__.py`
- Create: `src/moco/service/launchd.py`
- Create: `src/moco/cli.py`
- Create: `tests/test_doctor.py`
- Create: `tests/test_launchd.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing command and plist tests**

Tests must cover:

- `config init` is non-destructive unless `--force`;
- generated YAML validates;
- doctor reports Python, config, Codex binary/account/experimental feature/
  voices, Irodori health/model, and hotkey listener with stable codes;
- doctor never prints account identity, tokens, capability, or configured URL;
- service install uses an exact label `dev.toarupen.moco`, absolute executable
  and config paths, `RunAtLoad`, `KeepAlive`, and standard log paths;
- uninstall refuses any plist whose label or executable does not match moco;
- status distinguishes installed/running/stopped.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_doctor.py tests/test_launchd.py tests/test_cli.py -q
```

Expected: imports fail for the new modules.

- [ ] **Step 3: Implement the commands**

The CLI must expose:

```text
moco config init
moco config validate
moco doctor
moco run
moco open
moco service install|start|stop|status|uninstall
```

`run` starts telemetry, hotkeys, lifecycle, and Uvicorn. `open` requests the
active capability URL from the running process through a user-only state file,
then opens it without printing the token. State and plist files are written
atomically with user-only permissions.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: all CLI and service tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/moco/doctor.py src/moco/service src/moco/cli.py \
  tests/test_doctor.py tests/test_launchd.py tests/test_cli.py pyproject.toml
git commit -m "feat: add diagnostics and launchd controls"
```

### Task 9: Documentation, anti-slop rules, and CI/CD

**Files:**

- Create: `README.md`
- Create: `SECURITY.md`
- Create: `rules/no-placeholder-comments.yml`
- Create: `rules/no-empty-except.yml`
- Create: `sgconfig.yml`
- Create: `.secretlintrc.json`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`
- Modify: `AGENTS.md`
- Modify: `justfile`
- Create: `tests/test_repository_contract.py`

- [ ] **Step 1: Write repository contract tests**

Assert the symlink, public example config, documented commands, required
workflow permissions/concurrency, immutable action SHAs, release artifact
build, no `pass`-only exception handlers, no placeholder comments in source,
and no generated/local config in Git.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_repository_contract.py -q`

Expected: missing docs/rules/workflows.

- [ ] **Step 3: Add the user and contributor surface**

README golden path:

```bash
git clone https://github.com/ToaruPen/moco.git
cd moco
just sync
uv run moco config init
uv run moco config validate
uv run moco doctor
uv run moco run
```

Document the local Tailscale Irodori URL edit, Chrome microphone permission,
macOS Input Monitoring permission, configurable control behavior, foreground first run,
launchd install, troubleshooting codes, Realtime experimental caveat, and
privacy boundary.

`just check` must run format check, Ruff, mypy, vulture, deptry, ast-grep,
Biome, pytest with branch coverage, secretlint, package build, and repository
contract tests.

- [ ] **Step 4: Run and repair the full quality gate**

Run:

```bash
just format
just check
```

Expected: both commands exit zero with no warnings from project code.

- [ ] **Step 5: Commit**

```bash
git add README.md SECURITY.md AGENTS.md justfile rules sgconfig.yml \
  .secretlintrc.json .github tests/test_repository_contract.py
git commit -m "ci: complete public repository quality gates"
```

### Task 10: Local configuration and live acceptance

**Files:**

- Create locally, ignored: `~/Library/Application Support/moco/moco.yaml`
- Modify if needed: runtime code and tests discovered by live failures
- Update: `README.md` troubleshooting only when evidence requires it

- [ ] **Step 1: Initialize the actual user configuration**

Run:

```bash
uv run moco config init
```

Set the ignored local configuration to the current Windows Tailscale address
and the proven portable speaker. Do not commit or print the address if the
configuration is treated as private.

- [ ] **Step 2: Start and verify Irodori**

Use the existing `irodori-tts-infra` deploy controls without syncing the dirty
local checkout:

```bash
uv run --env-file .env irodori-tts-deploy deploy-status
uv run --env-file .env irodori-tts-deploy deploy-start
```

Then wait for `/health` to return `model_loaded=true` and run:

```bash
uv run moco doctor --synthesize "接続確認です。"
```

Expected: Codex and Irodori checks pass and a valid WAV byte count is reported
without saving audio.

- [ ] **Step 3: Foreground browser acceptance**

Run: `uv run moco run`

Verify manually with the real browser/microphone:

1. Bind the feature-test profile to F1/F2, click enable once, and grant microphone access.
2. Hold F1, say a short Japanese question, release F1.
3. Hear only Irodori audio and see the current transcript.
4. Hold F1 during speech and confirm old playback stops.
5. Press F2 during speech and during delegated work; confirm current output
   stops and the next F1 works.
6. Set a short test idle timeout, observe session expiry, then confirm the next
   F1 creates a new conversation without restarting moco.
7. Ask for a harmless Codex work task and observe progress/terminal spans.

- [ ] **Step 4: Re-run all verification after live fixes**

Run:

```bash
just check
uv run moco doctor
git diff --check
git status --short
```

Expected: quality gate and doctor pass; only intended source/document changes
remain.

- [ ] **Step 5: Commit evidence-backed fixes**

```bash
git add src/moco tests README.md
git commit -m "fix: harden live moco voice workflow"
```

Skip this commit when the live run requires no tracked fixes.

### Task 11: Review, publish, and release handoff

**Files:**

- Review all tracked changes
- Create GitHub release only if a tag is justified by live acceptance

- [ ] **Step 1: Run independent specification and code reviews**

Ask separate reviewers to check:

- design/spec coverage and missing user-visible behavior;
- correctness, cancellation races, cleanup, security, and privacy;
- test adequacy and false-positive doctor behavior.

Fix every high/medium actionable finding with a failing test first.

- [ ] **Step 2: Run final fresh verification**

Run:

```bash
just check
uv run moco doctor
uv build
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 3: Push the exact verified state**

```bash
git push origin main
```

- [ ] **Step 4: Verify GitHub state**

Run:

```bash
gh repo view ToaruPen/moco --json visibility,url,defaultBranchRef
gh run list --limit 5
gh issue view 1
```

Expected: public visibility, `main` default branch, successful CI, and the
long-term-memory issue present.

- [ ] **Step 5: Tag the live-accepted release**

Only after the live microphone acceptance succeeds:

```bash
git tag -a v0.1.0 -m "moco v0.1.0"
git push origin v0.1.0
```

Verify the release workflow artifact before reporting the version as released.
