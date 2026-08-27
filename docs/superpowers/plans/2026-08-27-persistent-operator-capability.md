# Persistent Operator Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the moco operator capability valid across daemon and browser restarts until the owner explicitly rotates it.

**Architecture:** Store one versioned capability document beside the ephemeral runtime state through the existing owner-private filesystem boundary. Load it under the runtime lease, keep Reviewer control secrets ephemeral, and migrate the browser from tab-scoped `sessionStorage` to origin-scoped `localStorage` while preserving URL-fragment precedence.

**Tech Stack:** Python 3.13, Typer, FastAPI/WebSocket, hardened private-state filesystem helpers, browser JavaScript, pytest, Node test runner, `uv`, and `just`.

---

## File map

- Create `src/moco/runtime/operator_capability.py`: strict capability document parsing, create/reuse, path derivation, and offline rotation.
- Create `tests/test_operator_capability.py`: persistence, strict parsing, permission, and no-secret error tests.
- Modify `src/moco/cli.py`: load the persistent capability under the runtime lease and expose `moco operator rotate`.
- Modify `tests/test_cli.py`: runtime reuse, ephemeral control secret, cleanup, and rotation command tests.
- Modify `src/moco/web/static/app.js`: URL → local storage → legacy session storage capability resolution.
- Modify `tests/js/app.test.js`: persistent browser storage and migration tests.
- Modify `src/moco/web/app.py` and `tests/test_web.py`: retain the already-tested bounded rejection diagnostics from live debugging.
- Modify `README.md`, `SECURITY.md`, and `tests/test_repository_contract.py`: replace the process/tab-lifetime contract and document rotation.

### Task 1: Preserve bounded WebSocket rejection diagnostics

**Files:**
- Modify: `src/moco/web/app.py:3197-3219,3358-3377`
- Test: `tests/test_web.py:3465-3494`

- [ ] **Step 1: Verify the focused diagnostic test already passes**

Run:

```bash
uv run pytest tests/test_web.py -k logs_bounded_operator_websocket_rejection_reason -q
```

Expected: three passing cases for `origin_rejected`, `capability_missing`, and `capability_mismatch`; no capability value appears in captured logs.

- [ ] **Step 2: Review the diagnostic boundary**

Keep this exact behavior in `_capability_rejection_code`:

```python
if _WEBSOCKET_PROTOCOL not in protocols or not candidate:
    return "capability_missing"
if not secrets.compare_digest(candidate, expected_token):
    return "capability_mismatch"
return None
```

The log call must contain only stable metadata:

```python
safe_event(
    logger,
    "operator_websocket_rejected",
    component="web",
    boundary="operator_websocket",
    event_code=rejection_code,
    result="rejected",
)
```

- [ ] **Step 3: Commit the diagnostic change**

```bash
git add src/moco/web/app.py tests/test_web.py
git commit -m "feat: diagnose operator websocket rejection"
```

### Task 2: Add the owner-private persistent capability document

**Files:**
- Create: `src/moco/runtime/operator_capability.py`
- Create: `tests/test_operator_capability.py`

- [ ] **Step 1: Write the failing create-and-reuse test**

Create `tests/test_operator_capability.py` with:

```python
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from moco.errors import PrivateStateError
from moco.runtime import operator_capability


TOKEN_A = "A" * 43
TOKEN_B = "B" * 43


def test_load_or_create_persists_and_reuses_owner_private_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "operator-capability.json"
    generated = iter((TOKEN_A, TOKEN_B))
    monkeypatch.setattr(
        operator_capability.secrets,
        "token_urlsafe",
        lambda _size: next(generated),
    )

    assert operator_capability.load_or_create_operator_capability(path) == TOKEN_A
    assert operator_capability.load_or_create_operator_capability(path) == TOKEN_A
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_bytes()) == {"version": 1, "capability": TOKEN_A}


def test_operator_capability_path_is_sibling_of_runtime_state(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-private" / "runtime.json"

    assert operator_capability.operator_capability_path(runtime) == runtime.with_name(
        "operator-capability.json"
    )


@pytest.mark.parametrize(
    ("platform_name", "environ", "expected"),
    [
        (
            "darwin",
            {"HOME": "/Users/example"},
            Path("/Users/example/Library/Application Support/moco/operator-capability.json"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": r"C:\\Users\\example\\AppData\\Local"},
            Path(r"C:\\Users\\example\\AppData\\Local/moco/runtime-private/operator-capability.json"),
        ),
    ],
)
def test_operator_capability_path_follows_injected_runtime_environment(
    platform_name: str,
    environ: dict[str, str],
    expected: Path,
) -> None:
    from moco.platform import default_runtime_state_path

    runtime = default_runtime_state_path(platform_name=platform_name, environ=environ)

    assert operator_capability.operator_capability_path(runtime) == expected
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
uv run pytest tests/test_operator_capability.py -q
```

Expected: collection fails because `moco.runtime.operator_capability` does not exist.

- [ ] **Step 3: Implement the minimal persistence module**

Create `src/moco/runtime/operator_capability.py`:

```python
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from moco.errors import PrivateStateError
from moco.runtime.private_state import read_private_state, remove_private_state, write_private_state


_DOCUMENT_VERSION = 1
_CAPABILITY_FILE_NAME = "operator-capability.json"
_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def operator_capability_path(runtime_state_path: Path) -> Path:
    return runtime_state_path.with_name(_CAPABILITY_FILE_NAME)


def load_or_create_operator_capability(path: Path) -> str:
    try:
        content = read_private_state(path)
    except FileNotFoundError:
        capability = secrets.token_urlsafe(32)
        payload = json.dumps(
            {"version": _DOCUMENT_VERSION, "capability": capability},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        write_private_state(path, payload)
        return capability
    return _parse_operator_capability(content)


def rotate_operator_capability(path: Path) -> None:
    remove_private_state(path)


def _parse_operator_capability(content: bytes) -> str:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivateStateError("operator capability document is invalid") from error
    if type(payload) is not dict or set(payload) != {"version", "capability"}:
        raise PrivateStateError("operator capability document is invalid")
    capability = payload["capability"]
    if (
        type(payload["version"]) is not int
        or payload["version"] != _DOCUMENT_VERSION
        or type(capability) is not str
        or _CAPABILITY_PATTERN.fullmatch(capability) is None
    ):
        raise PrivateStateError("operator capability document is invalid")
    return capability
```

- [ ] **Step 4: Run the focused test to verify GREEN**

Run:

```bash
uv run pytest tests/test_operator_capability.py -q
```

Expected: all initial persistence and injected-path tests pass.

- [ ] **Step 5: Add strict invalid-document tests**

Append:

```python
@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"version":true,"capability":"' + TOKEN_A.encode() + b'"}',
        b'{"version":2,"capability":"' + TOKEN_A.encode() + b'"}',
        b'{"version":1,"capability":"short"}',
        b'{"version":1,"capability":"' + (b"!" * 43) + b'"}',
        b'{"version":1,"capability":"' + TOKEN_A.encode() + b'","extra":true}',
    ],
)
def test_existing_invalid_document_fails_closed_without_secret_in_error(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "runtime-private" / "operator-capability.json"
    path.parent.mkdir(mode=0o700)
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(PrivateStateError) as caught:
        operator_capability.load_or_create_operator_capability(path)

    assert "capability document is invalid" in str(caught.value)
    assert TOKEN_A not in str(caught.value)
    assert path.read_bytes() == payload


def test_private_boundary_failure_is_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "operator-capability.json"

    def reject_read(_path: Path) -> bytes:
        raise PrivateStateError("operator capability permissions are not private")

    monkeypatch.setattr(operator_capability, "read_private_state", reject_read)
    monkeypatch.setattr(
        operator_capability,
        "write_private_state",
        lambda *_args, **_kwargs: pytest.fail("unsafe state must not be overwritten"),
    )

    with pytest.raises(PrivateStateError, match="permissions are not private"):
        operator_capability.load_or_create_operator_capability(path)
```

- [ ] **Step 6: Run the strict parser tests**

Run:

```bash
uv run pytest tests/test_operator_capability.py -q
```

Expected: all cases pass and the invalid file remains unchanged.

- [ ] **Step 7: Commit the persistence boundary**

```bash
git add src/moco/runtime/operator_capability.py tests/test_operator_capability.py
git commit -m "feat: persist operator capability privately"
```

### Task 3: Reuse the capability across daemon restarts

**Files:**
- Modify: `src/moco/cli.py:270-335`
- Modify: `tests/test_cli.py:589-690`

- [ ] **Step 1: Write a failing runtime reuse test**

Add to `tests/test_cli.py`:

```python
async def test_runtime_reuses_operator_capability_but_rotates_control_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []
    operator_app = SimpleNamespace(
        state=SimpleNamespace(control_hub=SimpleNamespace(publish=lambda _control: None))
    )

    def create(
        _settings: MocoSettings,
        *,
        capability_token: str,
        control_secret: str,
    ) -> SimpleNamespace:
        observed.append((capability_token, control_secret))
        return operator_app

    monkeypatch.setattr(cli, "create_app", create)
    monkeypatch.setattr(cli, "configure_telemetry", lambda _settings: FakeTelemetry())
    monkeypatch.setattr(cli, "GlobalHotkeyListener", FakeHotkeyListener)
    monkeypatch.setattr(uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda _config: FakeServer())
    state_path = tmp_path / "runtime-private" / "runtime.json"

    await _run_runtime(MocoSettings(), state_path=state_path)
    await _run_runtime(MocoSettings(), state_path=state_path)

    assert observed[0][0] == observed[1][0]
    assert observed[0][1] != observed[1][1]
    assert not state_path.exists()
    assert state_path.with_name("operator-capability.json").exists()
```

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
uv run pytest tests/test_cli.py -k reuses_operator_capability -q
```

Expected: the two observed capability tokens differ.

- [ ] **Step 3: Move capability loading under the runtime lease**

Import the boundary in `src/moco/cli.py`:

```python
from moco.runtime.operator_capability import (
    load_or_create_operator_capability,
    operator_capability_path,
    rotate_operator_capability,
)
```

Change the runtime functions to:

```python
async def _run_runtime(settings: MocoSettings, *, state_path: Path) -> None:
    with hold_private_runtime_lease(state_path):
        capability_value = load_or_create_operator_capability(
            operator_capability_path(state_path)
        )
        await _run_owned_runtime(
            settings,
            state_path=state_path,
            capability_value=capability_value,
        )


async def _run_owned_runtime(
    settings: MocoSettings,
    *,
    state_path: Path,
    capability_value: str,
) -> None:
    telemetry = configure_telemetry(settings.telemetry)
    control_secret = secrets.token_urlsafe(32)
```

Update direct `_run_owned_runtime` test calls, if any, to pass `capability_value=TOKEN_A`.

- [ ] **Step 4: Run runtime and CLI tests**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_operator_capability.py -q
```

Expected: all tests pass; `runtime.json` cleanup expectations remain green.

- [ ] **Step 5: Commit runtime reuse**

```bash
git add src/moco/cli.py tests/test_cli.py
git commit -m "feat: reuse operator capability across restarts"
```

### Task 4: Add explicit offline capability rotation

**Files:**
- Modify: `src/moco/cli.py:45-60,120-270`
- Modify: `tests/test_cli.py:35-115,250-360`

- [ ] **Step 1: Write failing CLI rotation tests**

Add to `tests/test_cli.py`:

```python
def test_operator_rotate_removes_capability_only_under_runtime_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    capability_path = state_path.with_name("operator-capability.json")
    capability_path.parent.mkdir(mode=0o700)
    capability_path.write_text(
        json.dumps({"version": 1, "capability": "A" * 43}),
        encoding="utf-8",
    )
    capability_path.chmod(0o600)
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)

    result = runner.invoke(app, ["operator", "rotate"])

    assert result.exit_code == 0
    assert result.output == "operator capability will rotate on next start\n"
    assert not capability_path.exists()
    assert "A" * 43 not in result.output


def test_operator_rotate_refuses_to_change_state_while_runtime_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    removed: list[Path] = []
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)

    @contextmanager
    def reject_lease(_path: Path) -> Iterator[None]:
        raise PrivateStateError("runtime lease is already held")
        yield

    monkeypatch.setattr(cli, "hold_private_runtime_lease", reject_lease)
    monkeypatch.setattr(cli, "rotate_operator_capability", removed.append)

    result = runner.invoke(app, ["operator", "rotate"])

    assert result.exit_code == 1
    assert result.output == "ERROR [operator_capability]: stop moco before rotating\n"
    assert removed == []
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
uv run pytest tests/test_cli.py -k operator_rotate -q
```

Expected: Typer reports that the `operator` command does not exist.

- [ ] **Step 3: Add the operator command group and rotation command**

Add beside the existing Typer groups:

```python
operator_app = typer.Typer(no_args_is_help=True, help="Manage operator access.")
app.add_typer(operator_app, name="operator")
```

Add the command:

```python
@operator_app.command("rotate")
def operator_rotate_command() -> None:
    """Invalidate the persistent operator capability while moco is stopped."""
    state_path = default_runtime_state_path()
    try:
        with hold_private_runtime_lease(state_path):
            rotate_operator_capability(operator_capability_path(state_path))
    except PrivateStateError as error:
        typer.echo("ERROR [operator_capability]: stop moco before rotating")
        raise typer.Exit(code=1) from error
    typer.echo("operator capability will rotate on next start")
```

- [ ] **Step 4: Run the CLI tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_cli.py -k 'operator_rotate or public_command_surface' -q
```

Expected: rotation succeeds only offline, the public help contains `operator`, and no token appears.

- [ ] **Step 5: Commit rotation**

```bash
git add src/moco/cli.py tests/test_cli.py
git commit -m "feat: rotate operator capability offline"
```

### Task 5: Persist and migrate the browser capability

**Files:**
- Modify: `src/moco/web/static/app.js:10-15,864-875,1160-1170`
- Modify: `tests/js/app.test.js:2615-2670`

- [ ] **Step 1: Replace the tab-only test with failing persistence tests**

Replace `retains the capability within the tab when the URL is reloaded` with:

```javascript
it("persists a fragment capability for reloads and new tabs", () => {
  const persistentValues = new Map();
  const persistentStorage = {
    getItem: (key) => persistentValues.get(key) ?? null,
    setItem: (key, value) => persistentValues.set(key, value),
  };
  const sessionStorage = { getItem: () => null };
  const history = {
    replaceState: (_state, _unused, url) => assert.equal(url, "/"),
  };

  assert.equal(
    appModule.loadCapability({
      history,
      location: { hash: "#test-capability", pathname: "/" },
      persistentStorage,
      sessionStorage,
    }),
    "test-capability",
  );
  assert.equal(
    appModule.loadCapability({
      history,
      location: { hash: "", pathname: "/" },
      persistentStorage,
      sessionStorage,
    }),
    "test-capability",
  );
});

it("migrates a legacy tab capability and lets a new fragment replace it", () => {
  const values = new Map();
  const persistentStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const sessionStorage = { getItem: () => "legacy-capability" };
  const history = { replaceState: () => {} };

  assert.equal(
    appModule.loadCapability({
      history,
      location: { hash: "", pathname: "/" },
      persistentStorage,
      sessionStorage,
    }),
    "legacy-capability",
  );
  assert.equal(values.get("moco.capability"), "legacy-capability");
  assert.equal(
    appModule.loadCapability({
      history,
      location: { hash: "#fresh-capability", pathname: "/" },
      persistentStorage,
      sessionStorage,
    }),
    "fresh-capability",
  );
  assert.equal(values.get("moco.capability"), "fresh-capability");
});
```

- [ ] **Step 2: Run the JavaScript tests to verify RED**

Run:

```bash
just test-frontend
```

Expected: the new tests fail because `loadCapability` still expects one `storage` argument.

- [ ] **Step 3: Implement persistent resolution and legacy migration**

Change `loadCapability` to:

```javascript
export function loadCapability({
  location,
  history,
  persistentStorage,
  sessionStorage,
}) {
  const capabilityFromUrl = location.hash.slice(1);
  if (capabilityFromUrl) {
    persistentStorage.setItem(CAPABILITY_STORAGE_KEY, capabilityFromUrl);
    history.replaceState(null, "", location.pathname);
    return capabilityFromUrl;
  }
  const persisted = persistentStorage.getItem(CAPABILITY_STORAGE_KEY);
  if (persisted) {
    return persisted;
  }
  const legacy = sessionStorage.getItem(CAPABILITY_STORAGE_KEY) ?? "";
  if (legacy) {
    persistentStorage.setItem(CAPABILITY_STORAGE_KEY, legacy);
  }
  return legacy;
}
```

Update bootstrap wiring:

```javascript
const capability = loadCapability({
  location: window.location,
  history: window.history,
  persistentStorage: window.localStorage,
  sessionStorage: window.sessionStorage,
});
```

- [ ] **Step 4: Run JavaScript tests to verify GREEN**

Run:

```bash
just test-frontend
```

Expected: all JavaScript and DOM tests pass.

- [ ] **Step 5: Confirm the storage identifier has only intended owners**

Run:

```bash
rg -n "moco\.capability|persistentStorage|sessionStorage" src/moco/web/static tests/js
```

Expected: capability storage is owned only by `loadCapability`, its bootstrap call, and focused tests; no cookie, query, or telemetry copy exists.

- [ ] **Step 6: Commit browser persistence**

```bash
git add src/moco/web/static/app.js tests/js/app.test.js
git commit -m "feat: remember operator capability in browser"
```

### Task 6: Update the public security and operating contract

**Files:**
- Modify: `README.md:245-255,380-401`
- Modify: `SECURITY.md:12-30`
- Modify: `tests/test_repository_contract.py:170-215,400-415`

- [ ] **Step 1: Write failing repository-contract assertions**

Update the mobile/security contract test to require these phrases:

```python
for required in [
    "operator-capability.json",
    "localStorage",
    "moco operator rotate",
    "owner-private",
]:
    assert required in readme
    assert required in security
```

Keep the tracked-secret check and extend it:

```python
assert not any(path.endswith("runtime.json") for path in tracked)
assert not any(path.endswith("operator-capability.json") for path in tracked)
```

- [ ] **Step 2: Run the repository-contract tests to verify RED**

Run:

```bash
uv run pytest tests/test_repository_contract.py -q
```

Expected: assertions fail because README and SECURITY still describe process/tab lifetime.

- [ ] **Step 3: Update README and SECURITY**

Replace the old lifecycle statements with this contract, adapted to each document's surrounding prose:

```text
media capability は owner-private な operator-capability.json に永続化し、daemon
再起動後も再利用します。ブラウザは同一 origin の localStorage に保存します。
runtime.json と Reviewer control secret は引き続き process lifetime に限定します。
明示的に失効する場合は moco を停止して moco operator rotate を実行し、再起動後に
新しい QR を登録します。
```

Document that a browser profile reset, storage deletion, or explicit rotation requires one new QR scan.

- [ ] **Step 4: Run repository-contract tests to verify GREEN**

Run:

```bash
uv run pytest tests/test_repository_contract.py -q
```

Expected: all repository contract tests pass.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md SECURITY.md tests/test_repository_contract.py
git commit -m "docs: explain persistent operator access"
```

### Task 7: Verify security, migration, and full regression gates

**Files:**
- Review: all files changed since `3fb30d2`
- No production file changes unless a failing check or review finding requires a tested fix.

- [ ] **Step 1: Format deterministically**

Run:

```bash
just format
```

Expected: formatting completes without deleting or disabling tests.

- [ ] **Step 2: Run focused Python and browser suites**

Run:

```bash
uv run pytest tests/test_operator_capability.py tests/test_cli.py tests/test_web.py tests/test_repository_contract.py -q
just test-frontend
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the complete repository gate**

Run:

```bash
just check
```

Expected: formatting, lint, type checks, Python tests, browser tests, repository contracts, and secret scanning pass.

- [ ] **Step 4: Run the current security baseline review**

Check the current official OWASP Top 10 edition at execution time, then inspect the complete diff for:

```text
untrusted fragment -> localStorage -> WebSocket subprotocol -> constant-time comparison
owner-private file -> strict parser -> in-memory bearer credential
offline rotate command -> runtime lease -> identity-checked deletion
```

Required conclusions:

- no capability value reaches logs, CLI output, telemetry, query, cookie, or repository;
- invalid or unsafe persistent state fails closed and is not overwritten;
- Cloudflare Access and Origin / Host checks remain mandatory;
- Reviewer control secret remains process-scoped;
- rotation cannot report success while the daemon owns the runtime lease.

- [ ] **Step 5: Verify live restart behavior without printing the token**

After installing/restarting the service, compare only a digest in a temporary process:

```bash
uv run python -c 'import hashlib,json; from pathlib import Path; p=Path.home()/"Library/Application Support/moco/operator-capability.json"; d=json.loads(p.read_bytes()); print(hashlib.sha256(d["capability"].encode()).hexdigest())'
launchctl kickstart -k gui/$(id -u)/dev.toarupen.moco
sleep 2
uv run python -c 'import hashlib,json; from pathlib import Path; p=Path.home()/"Library/Application Support/moco/operator-capability.json"; d=json.loads(p.read_bytes()); print(hashlib.sha256(d["capability"].encode()).hexdigest())'
```

Expected: the two digests match. Do not include the digest in normal application logs or documentation.

- [ ] **Step 6: Verify the user flow**

Use the latest QR once, connect from the physical iPhone with iPhone Mirroring closed, then restart moco and reopen Chrome at `https://moco.toarupen.org` without rescanning.

Expected: Cloudflare may request email authentication only when its one-month session expires; the moco WebSocket no longer fails because of daemon or browser restart. iPhone Mirroring is excluded from microphone verification because Apple does not expose the iPhone microphone through Mirroring.

- [ ] **Step 7: Review final history and working tree**

Run:

```bash
git status --short --branch
git log --oneline --decorate -10
```

Expected: no unintended files, credentials, audio, transcript, runtime state, or generated QR files are tracked.
