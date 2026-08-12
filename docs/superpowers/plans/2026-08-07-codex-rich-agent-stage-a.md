# Codex rich-agent 段階A 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** mocoのCodex接続を双方向・version-awareなapp-server clientへ置き換え、macOS/Windowsの両方で安全に起動・診断・契約検証できるprotocol基盤を完成させる。

**Architecture:** wire messageの分類とserver request lifecycleは純粋な`RpcPeer`が所有し、processとinitialize/終了は`CodexConnectionSupervisor`が所有する。生成schemaからsemantic methodを解決する`CodexProtocolContract`と、Stage Aに必要なreadinessだけを返す`CapabilityDiscovery`をその上に置く。OS差分は`moco.platform`とprivate runtime state境界へ限定し、既存Voice体験は新接続へ移して回帰維持する。

**Tech Stack:** Python 3.13、asyncio subprocess/stdio、Pydantic v2、pytest、Typer、FastAPI、Windows限定pywin32、GitHub Actions、`uv`、`just`

---

## 実行規則と境界

- Source of truthは`docs/superpowers/specs/2026-08-07-codex-rich-agent-client-design.md`。
- この計画は同仕様の段階Aだけを実装する。`AgentSession`、Voice→Agent handoff、三つのAgent profile mode、Reviewer、approval adapter、final-only speech、Browser/Computer Useは作らない。
- Voice Threadの現行`ephemeral`、`read-only`、`approvalPolicy=never`、Realtime audio v3、assistant発話は段階Aでは維持する。固定profileの置換とtask回答の禁止は段階Bで行う。
- `turn/interrupt`はschema上のreadiness発見までとし、Agent turn制御には接続しない。
- production version、method件数、tool/voice一覧、全payload field集合をtestへ固定しない。
- schema、account、effective config、stderrの生payloadをbrowser、telemetry、通常logへ出さない。
- Windows Store package内部のprivate resourceを探索・直接実行しない。
- Cloudflare、Tailscale Serve、macOS privacy permission、Windows permission、service設定を変更しない。
- Repositoryの指示に従い、commit stepは意図的に含めない。commitは利用者が明示した場合だけ行う。
- 既存の未追跡`.superpowers/`と他のdirty-worktree変更を保持する。

計画作成時のローカルCodex 0.144.1では、生成schemaにStage Aのsemantic categoryが存在し、effective policyは`danger-full-access`と`never`だった。この値はproduction constantにしない。段階AのVoiceは継続可能だが、CapabilitySnapshotはAgent admissionを`unsafe_voice_policy`としてblockedにする。

## ファイル構成

### Protocol/capability lane

- Modify `src/moco/codex/rpc.py`: process ownershipを外し、排他的message分類、双方向pending map、notification fan-out、server requestのexactly-once応答を所有する。
- Create `src/moco/codex/connection.py`: `CodexConnectionSupervisor`、subprocess、initialize、stderr drain、sticky connection loss、bounded shutdownを所有する。
- Create `src/moco/codex/schema.py`: 一時生成schemaの読込、direction判定、semantic method選択、server request category抽出を所有する。
- Create `src/moco/codex/capabilities.py`: Stage Aのtyped readiness snapshotとeffective policy正規化を所有する。
- Modify `src/moco/codex/session.py`: 新supervisor/discovery上へVoiceを移し、既存Realtime契約を維持する。
- Modify `src/moco/codex/__init__.py`: 新しい公開integration surfaceだけをexportする。
- Modify `src/moco/errors.py`: Task 1でprotocol、schema、host securityのstable domain errorを一括定義し、並行laneからは編集しない。
- Create `tests/test_codex_connection.py`, `tests/test_codex_schema.py`, `tests/test_codex_capabilities.py`。
- Modify `tests/test_codex_rpc.py`, `tests/test_codex_session.py`, `tests/fixtures/fake_codex.py`。

### Host/runtime lane

- Create `src/moco/platform.py`: OS family、portable path、Codex command解決、service/hotkey/browserの小さな差分を所有する。
- Create `src/moco/runtime/private_state.py`: private runtime directory、Windows security policy、stateのread/write/removeを所有する。
- Create `src/moco/runtime/_windows_acl.py`: pywin32によるowner SID、reparse point、DACLのOS bindingだけを所有する。
- Modify `src/moco/config.py`: `codex.command`とportable working directory/pathを定義する。
- Modify `src/moco/cli.py`: private state API、Windows service拒否、OS別hotkey説明を利用する。
- Modify `src/moco/doctor.py`: command resolverとCapabilitySnapshotだけをCodex readinessのsourceにする。
- Modify `src/moco/web/app.py`: resolved commandからsupervisor/discovery/VoiceSessionを構成する。
- Create `tests/test_platform.py`, `tests/test_private_state.py`。
- Modify `tests/test_config.py`, `tests/test_cli.py`, `tests/test_doctor.py`, `tests/test_launchd.py`, `tests/test_repository_contract.py`。

### Contract/CI/docs

- Create `tests/test_codex_contract.py`: installed Codexからruntime-derived schema contractを検証する。
- Modify `pyproject.toml`, `uv.lock`: Windows dependency、marker、Windows classifier。
- Modify `config/moco.example.yaml`, `README.md`: portable command/path、foreground Windows、Stage A readiness。
- Modify `justfile`: Task 1で`test-python`、Task 11で`contract-codex`を追加する。
- Modify `.github/workflows/ci.yml`: Ubuntu完全gateとmacOS/Windows Python matrix。

## 実行順と分業

Task 1のportable command型、共通error型、portable Python test entrypointを共有境界として先に完了する。その後、Task 2–6のprotocol/capability laneとTask 7–8のhost/runtime laneは別workerが並行実行できる。両laneがGREENになってからTask 9–11を統合する。同じfileを同時編集しない。

実行開始時は変更前baselineを確認する。

```bash
just test
git status --short
```

Expected: normal suiteがPASSし、未追跡`.superpowers/`と承認済みspec/planを含む既存差分がそのまま表示される。失敗があれば実装へ進まず、既存failureか環境failureかを切り分ける。

### Task 1: Portable pathとCodex command契約

**Files:**
- Create: `src/moco/platform.py`
- Create: `tests/test_platform.py`
- Modify: `src/moco/errors.py`
- Modify: `src/moco/config.py`
- Modify: `tests/test_config.py`
- Modify: `config/moco.example.yaml`
- Modify: `justfile`

- [ ] **Step 1: portable pathとcommandのRED testsを書く**

`tests/test_platform.py`を作り、環境を引数注入してhost OSを偽装する。PATH探索のtestではproduction pathをassertせず、注入した`which`の戻り値だけを使う。

```python
from __future__ import annotations

from pathlib import Path

import pytest

from moco.errors import CodexCommandError, HostPlatformError
from moco.platform import (
    CodexCommand,
    default_config_path,
    default_prompt_path,
    default_runtime_state_path,
    resolve_codex_command,
)


def test_windows_paths_use_roaming_and_private_local_state() -> None:
    environ = {
        "APPDATA": r"C:\Users\voice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\voice\AppData\Local",
    }

    roaming = Path(environ["APPDATA"])
    local = Path(environ["LOCALAPPDATA"])
    assert default_config_path(platform_name="win32", environ=environ) == (
        roaming / "moco" / "moco.yaml"
    )
    assert default_prompt_path(platform_name="win32", environ=environ) == (
        roaming / "moco" / "prompt.md"
    )
    assert default_runtime_state_path(platform_name="win32", environ=environ) == (
        local / "moco" / "runtime-private" / "runtime.json"
    )


def test_windows_path_requires_documented_environment() -> None:
    with pytest.raises(HostPlatformError, match="APPDATA"):
        default_config_path(platform_name="win32", environ={})


def test_unconfigured_command_uses_path_without_store_fallback() -> None:
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return r"C:\Tools\codex.exe" if name == "codex" else None

    command = resolve_codex_command(None, platform_name="win32", which=which)

    assert command == CodexCommand((r"C:\Tools\codex.exe",))
    assert calls == ["codex"]


def test_invalid_explicit_command_does_not_fallback() -> None:
    with pytest.raises(CodexCommandError, match="unavailable"):
        resolve_codex_command(
            ("missing-private-codex",),
            platform_name="win32",
            which=lambda _name: None,
        )
```

`tests/test_config.py`へ、commandの既定値、YAML list→tuple、blank/NUL拒否、portable working directoryを追加する。

```python
def test_codex_command_and_working_directory_are_portable_defaults() -> None:
    settings = CodexSettings()

    assert settings.command is None
    assert settings.working_directory is None


def test_codex_command_accepts_an_argv_list(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text('codex:\n  command: ["codex", "--strict-config"]\n', encoding="utf-8")

    assert load_config(path).codex.command == ("codex", "--strict-config")


@pytest.mark.parametrize("command", [[], [""], ["codex", "bad\u0000arg"]])
def test_codex_command_rejects_unsafe_argv(tmp_path: Path, command: list[str]) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(yaml.safe_dump({"codex": {"command": command}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="codex.command"):
        load_config(path)
```

- [ ] **Step 2: focused testsを実行してREDを確認する**

Run:

```bash
uv run pytest tests/test_platform.py tests/test_config.py -k 'path or command or working_directory' -v
```

Expected: `moco.platform`、`CodexCommandError`、`codex.command`が未定義のためFAIL。

- [ ] **Step 3: platformとstrict configを最小実装する**

`src/moco/errors.py`へStage A共通のerror型を値本文を含めず一括追加する。後続の並行laneはこのfileを編集しない。

```python
class CodexCommandError(CodexError):
    """The configured or discovered Codex command is unavailable."""


class CodexRpcProtocolError(CodexRpcError):
    def __init__(
        self,
        message: str,
        *,
        client_response_id: int | str | None = None,
        server_request_id: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.client_response_id = client_response_id
        self.server_request_id = server_request_id


class CodexSchemaError(CodexError):
    """The installed Codex schema cannot satisfy a semantic contract."""


class CodexCapabilityError(CodexError):
    """Required Codex runtime capability discovery failed closed."""


class HostPlatformError(MocoError):
    """The host cannot provide a required portable platform boundary."""


class PrivateStateError(MocoError):
    """The runtime state location does not satisfy the host security boundary."""
```

`src/moco/platform.py`にshellを介さないcommand valueとpath ownerを作る。

```python
from __future__ import annotations

import os
import shutil
import sys
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from moco.errors import CodexCommandError, HostPlatformError

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class CodexCommand:
    argv: tuple[str, ...]

    def app_server_argv(self) -> tuple[str, ...]:
        return (*self.argv, "app-server", "--listen", "stdio://", "--enable", "realtime_conversation")

    def version_argv(self) -> tuple[str, ...]:
        return (*self.argv, "--version")

    def schema_argv(self, output: Path, *, experimental: bool) -> tuple[str, ...]:
        arguments = (*self.argv, "app-server", "generate-json-schema", "--out", str(output))
        return (*arguments, "--experimental") if experimental else arguments


def default_config_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_value = platform_name or sys.platform
    values = os.environ if environ is None else environ
    if platform_value == "win32":
        return _required_environment_path(values, "APPDATA") / "moco" / "moco.yaml"
    home = Path(values.get("HOME", str(Path.home())))
    return home / "Library" / "Application Support" / "moco" / "moco.yaml"


def default_prompt_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_value = platform_name or sys.platform
    values = os.environ if environ is None else environ
    if platform_value == "win32":
        return _required_environment_path(values, "APPDATA") / "moco" / "prompt.md"
    home = Path(values.get("HOME", str(Path.home())))
    return home / ".moco" / "prompt.md"


def default_runtime_state_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_value = platform_name or sys.platform
    values = os.environ if environ is None else environ
    if platform_value == "win32":
        root = _required_environment_path(values, "LOCALAPPDATA") / "moco" / "runtime-private"
    else:
        home = Path(values.get("HOME", str(Path.home())))
        root = home / "Library" / "Application Support" / "moco"
    return root / "runtime.json"


def resolve_codex_command(
    configured: tuple[str, ...] | None,
    *,
    platform_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> CodexCommand:
    platform_value = platform_name or sys.platform
    if configured is not None:
        executable = _resolve_executable(configured[0], which=which)
        if executable is None:
            raise CodexCommandError("configured Codex command is unavailable")
        return CodexCommand((executable, *configured[1:]))
    discovered = which("codex")
    if discovered is not None:
        return CodexCommand((discovered,))
    bundle = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if platform_value == "darwin" and bundle.is_file() and os.access(bundle, os.X_OK):
        return CodexCommand((str(bundle),))
    raise CodexCommandError("Codex command is unavailable")


def open_browser(url: str) -> bool:
    return webbrowser.open(url)


def _required_environment_path(values: Mapping[str, str], name: str) -> Path:
    raw = values.get(name)
    if raw is None or not raw.strip():
        raise HostPlatformError(f"{name} is unavailable")
    return Path(raw)


def _resolve_executable(value: str, *, which: Callable[[str], str | None]) -> str | None:
    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return which(value)
```

`src/moco/config.py`では旧`binary`と同file内のdefault path関数を削除し、`moco.platform`から`default_config_path`と`default_prompt_path`をimportして既存import surfaceを維持する。Codex起動設定はcommandだけへ一本化する。

```python
class CodexSettings(StrictSettings):
    command: tuple[str, ...] | None = None
    working_directory: Path | None = None
    prompt_file: Path | None = None

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value or any(not item.strip() or "\0" in item for item in value):
            msg = "command must contain non-blank NUL-free argv values"
            raise ValueError(msg)
        return value

    @field_validator("working_directory")
    @classmethod
    def _require_absolute_working_directory(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            msg = "path must be absolute"
            raise ValueError(msg)
        return value
```

`working_directory`は`None`なら会話開始時の`Path.cwd()`を使う。`config/moco.example.yaml`のCodex部分をportableにする。

```yaml
codex:
  # null resolves codex from PATH; macOS may then use the official app bundle resource.
  command: null
  # null uses moco's startup working directory on each host.
  working_directory: null
  # null checks the host-specific implicit prompt path.
  prompt_file: null
```

`justfile`へportable Python entrypointを追加する。pytest CLIの`-m`はini側のmarker式を置き換えるため、明示的にintegrationを含み、live/slow/contractだけを除外する。

```make
test-python *args:
    uv run pytest -m "not live and not slow and not contract" --durations=10 {{args}}
```

- [ ] **Step 4: config/platform testsをGREENにする**

Run:

```bash
just test-python tests/test_platform.py tests/test_config.py -v
```

Expected: PASS。`rg -n 'CodexSettings\(|\.codex\.binary|codex:\n  binary' src tests config README.md`で旧ownerを列挙し、後続Taskの変更対象以外に新旧契約が混在していないことを確認する。

### Task 2: 排他的JSON-RPC message分類

**Files:**
- Modify: `src/moco/codex/rpc.py`
- Modify: `tests/test_codex_rpc.py`

- [ ] **Step 1: classifierのRED table testsを書く**

```python
from moco.codex.rpc import (
    RpcFailure,
    RpcNotification,
    RpcServerRequest,
    RpcSuccess,
    _classify_message,
)


@pytest.mark.parametrize(
    ("message", "expected_type"),
    [
        ({"method": "server/do", "id": 7, "params": {}}, RpcServerRequest),
        ({"method": "server/do", "id": "req-7", "params": {}}, RpcServerRequest),
        ({"method": "event/done", "params": {}}, RpcNotification),
        ({"id": 7, "result": None}, RpcSuccess),
        ({"id": 7, "error": {"code": -1, "message": "failed"}}, RpcFailure),
    ],
)
def test_classifies_wire_messages_exclusively(
    message: dict[str, JsonValue],
    expected_type: type[object],
) -> None:
    assert isinstance(_classify_message(message), expected_type)


@pytest.mark.parametrize(
    "message",
    [
        {"method": "bad", "id": True, "params": {}},
        {"method": "bad", "id": 1, "result": {}},
        {"id": 1, "result": {}, "error": {}},
        {"id": 1},
        {"params": {}},
    ],
)
def test_rejects_malformed_or_overlapping_messages(message: dict[str, JsonValue]) -> None:
    with pytest.raises(CodexRpcProtocolError):
        _classify_message(message)


def test_malformed_response_preserves_client_pending_id() -> None:
    with pytest.raises(CodexRpcProtocolError) as caught:
        _classify_message({"id": "client-7"})

    assert caught.value.client_response_id == "client-7"
    assert caught.value.server_request_id is None
```

- [ ] **Step 2: classifier testsを実行してREDを確認する**

Run:

```bash
just test-python tests/test_codex_rpc.py -k 'classifies_wire or malformed_or_overlapping or client_pending_id' -v
```

Expected: new typesと`_classify_message`が未定義のためFAIL。

- [ ] **Step 3: immutable envelopeとclassifierを実装する**

Task 1で定義したdirection別ID文脈を使い、`src/moco/codex/rpc.py`のwire型を次へ置き換える。

```python
type RequestId = int | str


@dataclass(frozen=True, slots=True)
class RpcServerRequest:
    request_id: RequestId
    method: str
    params: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RpcSuccess:
    request_id: RequestId
    result: JsonValue


@dataclass(frozen=True, slots=True)
class RpcFailure:
    request_id: RequestId
    error: JsonValue


type RpcInbound = RpcServerRequest | RpcNotification | RpcSuccess | RpcFailure


def _classify_message(message: dict[str, JsonValue]) -> RpcInbound:
    has_method = "method" in message
    has_id = "id" in message
    has_result = "result" in message
    has_error = "error" in message
    method = message.get("method")
    raw_id = message.get("id")
    request_id = raw_id if _is_request_id(raw_id) else None
    if has_method:
        if not isinstance(method, str) or has_result or has_error:
            raise CodexRpcProtocolError(
                "Codex app server sent an overlapping request message",
                server_request_id=request_id,
            )
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise CodexRpcProtocolError(
                "Codex app server sent invalid request params",
                server_request_id=request_id,
            )
        if has_id:
            if request_id is None:
                raise CodexRpcProtocolError("Codex app server sent an invalid request id")
            return RpcServerRequest(request_id, method, params)
        return RpcNotification(method, params)
    if not has_id or request_id is None or has_result == has_error:
        raise CodexRpcProtocolError(
            "Codex app server sent an invalid response message",
            client_response_id=request_id,
        )
    if has_result:
        return RpcSuccess(request_id, message["result"])
    return RpcFailure(request_id, message["error"])


def _is_request_id(value: JsonValue) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
```

- [ ] **Step 4: classifierと既存response testsをGREENにする**

Run:

```bash
just test-python tests/test_codex_rpc.py -k 'classif or protocol or response or notification or invalid' -v
```

Expected: PASS。responseの`result: null`と`error`を混同しない。

### Task 3: 双方向RpcPeerとserver request lifecycle

**Files:**
- Modify: `src/moco/codex/rpc.py`
- Modify: `tests/test_codex_rpc.py`

- [ ] **Step 1: incoming requestとfan-outのRED testsを書く**

test用writerはwriteされたJSON lineをqueueへ入れ、`RpcPeer`には`StreamReader`とそのwriterを注入する。次を個別testにする。

```python
async def test_client_and_server_request_ids_use_independent_namespaces(peer_harness: PeerHarness) -> None:
    peer_harness.peer.register_server_request_handler(
        "host/echo",
        lambda request: asyncio.sleep(0, result={"seen": request.params["value"]}),
    )
    outgoing = asyncio.create_task(peer_harness.peer.request("client/ping", {"value": 1}))
    await peer_harness.receive({"method": "host/echo", "id": 1, "params": {"value": 2}})
    await peer_harness.receive({"id": 1, "result": {"pong": True}})

    assert await outgoing == {"pong": True}
    assert await peer_harness.next_sent() == {"id": 1, "result": {"seen": 2}}


async def test_notifications_are_fanned_out_to_two_subscribers(peer_harness: PeerHarness) -> None:
    first = peer_harness.peer.notifications()
    second = peer_harness.peer.notifications()

    await peer_harness.receive({"method": "event/ready", "params": {"phase": "ready"}})

    expected = RpcNotification("event/ready", {"phase": "ready"})
    assert await anext(first) == expected
    assert await anext(second) == expected


async def test_unknown_server_request_returns_error_without_success(
    peer_harness: PeerHarness,
) -> None:
    await peer_harness.receive({"method": "future/unknown", "id": "server-1", "params": {}})

    response = await peer_harness.next_sent()
    assert response["id"] == "server-1"
    assert response["error"] == {"code": -32601, "message": "unsupported server request"}
    assert "result" not in response


async def test_malformed_response_fails_matching_client_pending(
    peer_harness: PeerHarness,
) -> None:
    outgoing = asyncio.create_task(peer_harness.peer.request("client/ping", {}))
    sent = await peer_harness.next_sent()

    await peer_harness.receive({"id": sent["id"]})

    with pytest.raises(CodexRpcProtocolError):
        await outgoing


async def test_request_without_params_omits_wire_field(peer_harness: PeerHarness) -> None:
    outgoing = asyncio.create_task(
        peer_harness.peer.request("future/requirements/read")
    )
    sent = await peer_harness.next_sent()

    assert "params" not in sent

    await peer_harness.receive({"id": sent["id"], "result": {}})
    assert await outgoing == {}
```

重複incoming IDでは先行handlerをcancelし、protocol errorを一度だけ返し、peerをterminalにするtestも追加する。handler exceptionは`-32603`と固定文だけを返し、exception本文を出さない。

- [ ] **Step 2: incoming lifecycle testsのREDを確認する**

Run:

```bash
just test-python tests/test_codex_rpc.py -k 'server_request or independent_namespaces or fanned_out or duplicate or handler or malformed_response or without_params' -v
```

Expected: `register_server_request_handler`、incoming map、fan-outがないためFAIL。

- [ ] **Step 3: process非依存のRpcPeerを実装する**

`CodexRpcClient`からprocess start/terminate/stderrを外し、次のownershipへ縮小する。

```python
type ServerRequestHandler = Callable[[RpcServerRequest], Awaitable[JsonValue]]


@dataclass(slots=True)
class _IncomingCall:
    task: asyncio.Task[None]
    response_sent: bool = False


class RpcPeer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_timeout: float = 10.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._request_timeout = request_timeout
        self._pending: dict[RequestId, asyncio.Future[JsonValue]] = {}
        self._incoming: dict[RequestId, _IncomingCall] = {}
        self._handlers: dict[str, ServerRequestHandler] = {}
        self._subscribers: set[asyncio.Queue[RpcNotification | CodexRpcError | object]] = set()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._next_id = 1
        self._reader_task: asyncio.Task[None] | None = None
        self._terminal_error: CodexRpcError | None = None

    def register_server_request_handler(
        self,
        method: str,
        handler: ServerRequestHandler,
    ) -> None:
        if self._reader_task is not None:
            raise RuntimeError("server request handlers must be registered before peer start")
        self._handlers[method] = handler

    async def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._reader_loop(), name="codex-rpc-reader")

    def notifications(self) -> AsyncIterator[RpcNotification]:
        queue: asyncio.Queue[RpcNotification | CodexRpcError | object] = asyncio.Queue()
        self._subscribers.add(queue)

        async def iterate() -> AsyncIterator[RpcNotification]:
            try:
                async for notification in self._iterate_subscription(queue):
                    yield notification
            finally:
                self._subscribers.discard(queue)

        return iterate()

    def abort(self, error: CodexRpcError) -> None:
        self._set_terminal_error(error)
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()

    async def _dispatch_server_request(self, request: RpcServerRequest) -> None:
        handler = self._handlers.get(request.method)
        if handler is None:
            await self._complete_incoming(
                request.request_id,
                error={"code": -32601, "message": "unsupported server request"},
            )
            return
        try:
            result = await handler(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._complete_incoming(
                request.request_id,
                error={"code": -32603, "message": "server request handler failed"},
            )
        else:
            await self._complete_incoming(request.request_id, result=result)
        finally:
            call = self._incoming.get(request.request_id)
            if call is not None and call.task is asyncio.current_task():
                self._incoming.pop(request.request_id, None)

    async def _complete_incoming(
        self,
        request_id: RequestId,
        *,
        result: JsonValue = None,
        error: dict[str, JsonValue] | None = None,
    ) -> bool:
        call = self._incoming.get(request_id)
        if call is None or call.response_sent:
            return False
        call.response_sent = True
        message: dict[str, JsonValue] = {"id": request_id}
        message["error" if error is not None else "result"] = error if error is not None else result
        await self._write_message(message)
        return True
```

`_reader_loop()`は`await _handle_inbound(_classify_message(...))`を順に呼ぶ。`notifications()`はasync generatorを返す前にqueueをsubscriber setへ登録するため、最初の`anext()`前のeventも失わない。`method`と有効なIDを持つmalformed requestは`server_request_id`を使って`-32600`を一度だけ返してterminal化する。`method`のないmalformed responseは`client_response_id`で該当client pendingを失敗させ、server responseは送らない。duplicate incoming IDでは`response_sent`をawaitなしで先にclaimし、先行taskをcancelして`-32600`を送った後`CodexRpcProtocolError`でterminal化する。すでにresponseをclaim済みなら二通目を送らずterminal化する。`abort()`と`close()`はclient pendingを同じterminal errorで失敗させ、incoming taskをcancelし、全subscriberへerrorまたはend sentinelを一度だけ配信する。各notification generatorは`finally`でsubscriber setから自分のqueueを外す。完了済みIDの永久setは持たない。

- [ ] **Step 4: RpcPeer testsをGREENにする**

Run:

```bash
just test-python tests/test_codex_rpc.py -v
```

Expected: PASS。`rg -n '_pending|_incoming|_subscribers' src/moco/codex/rpc.py`で各state ownerが一つだけであることを確認する。

### Task 4: CodexConnectionSupervisorとcross-platform fake process

**Files:**
- Create: `src/moco/codex/connection.py`
- Create: `tests/test_codex_connection.py`
- Modify: `tests/fixtures/fake_codex.py`
- Modify: `src/moco/codex/__init__.py`

- [ ] **Step 1: supervisor process contractのRED testsを書く**

fake commandはshebangで直接実行せず、常に現在のPythonをargv先頭にする。

```python
pytestmark = pytest.mark.integration


@pytest.fixture
def fake_codex_command() -> CodexCommand:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"
    return CodexCommand((sys.executable, str(script)))


async def test_supervisor_initializes_and_preserves_initialize_metadata(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    await supervisor.start()

    assert supervisor.initialize_info == InitializeInfo(
        user_agent="fake-codex",
        platform_family="test",
        platform_os="test",
    )
    assert await supervisor.request("ping", {"value": 7}) == {"value": 7}
    await supervisor.close()


async def test_server_request_round_trip_keeps_string_id(
    fake_codex_command: CodexCommand,
) -> None:
    supervisor = CodexConnectionSupervisor(fake_codex_command, request_timeout=1)
    supervisor.register_server_request_handler(
        "fake/ask",
        lambda request: asyncio.sleep(0, result={"accepted": request.params["allowed"]}),
    )
    await supervisor.start()

    assert await supervisor.request("trigger/server-request", {"idKind": "string"}) == {
        "clientResponse": {"accepted": True},
        "responseIdType": "str",
    }
    await supervisor.close()
```

process exit、initialization拒否、stderr fingerprint-only、close idempotenceも既存`tests/test_codex_rpc.py`からこのfileへ移す。Windowsではsignal固有の段階をassertせず、deadline内にcloseが完了することだけをassertする。

- [ ] **Step 2: supervisor testsのREDを確認する**

Run:

```bash
just test-python tests/test_codex_connection.py -v
```

Expected: `CodexConnectionSupervisor`が未定義のためFAIL。

- [ ] **Step 3: process lifecycleをsupervisorへ移す**

`src/moco/codex/connection.py`の公開shapeを固定する。

```python
@dataclass(frozen=True, slots=True)
class InitializeInfo:
    user_agent: str
    platform_family: str | None
    platform_os: str | None


class CodexConnectionSupervisor:
    def __init__(
        self,
        command: CodexCommand,
        *,
        request_timeout: float = 10.0,
        shutdown_timeout: float = 1.0,
    ) -> None:
        self._command = command
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._handlers: dict[str, ServerRequestHandler] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._peer: RpcPeer | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._process_task: asyncio.Task[None] | None = None
        self._initialize_info: InitializeInfo | None = None

    @property
    def initialize_info(self) -> InitializeInfo:
        if self._initialize_info is None:
            raise CodexProcessExitedError("Codex app server is not initialized")
        return self._initialize_info

    def register_server_request_handler(
        self,
        method: str,
        handler: ServerRequestHandler,
    ) -> None:
        if self._process is not None:
            raise RuntimeError("server request handlers must be registered before connection start")
        self._handlers[method] = handler

    async def start(self) -> None:
        process = await asyncio.create_subprocess_exec(
            *self._command.app_server_argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise CodexProcessExitedError("Codex app server did not expose stdio pipes")
        peer = RpcPeer(process.stdout, process.stdin, request_timeout=self._request_timeout)
        for method, handler in self._handlers.items():
            peer.register_server_request_handler(method, handler)
        await peer.start()
        self._process = process
        self._peer = peer
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process.stderr),
            name="codex-app-server-stderr",
        )
        self._process_task = asyncio.create_task(
            self._watch_process(process),
            name="codex-app-server-process",
        )
        try:
            result = await peer.request(
                "initialize",
                {"clientInfo": dict(_CLIENT_INFO), "capabilities": {"experimentalApi": True}},
            )
            self._initialize_info = _parse_initialize_info(result)
            await peer.notify("initialized")
        except BaseException:
            await self.close()
            raise
```

`request()`、`notify()`、`notifications()`はstarted peerへdelegateする。`_watch_process()`はreturn codeをstable `CodexProcessExitedError`へ変換し、peerのclient pending、incoming handler、subscriberを同じ原因で終了させる。`close()`はpeerを先にterminal化し、stdin close→bounded wait→terminate→killの順でprocessを閉じる。接続喪失時はpending requestを再送しない。stderrはbyte countとSHA-256先頭12文字だけをwarningにする。

- [ ] **Step 4: fakeを二方向loopへ拡張してGREENにする**

`tests/fixtures/fake_codex.py`は先頭の`--scenario=<name>`だけをtest controlとして消費し、その後のapp-server argvを検証する。server requestを送った後は次のstdin lineをresponseとして読み、IDの型とexactly-onceを結果へ要約する。initialize responseには`userAgent`、`platformFamily`、`platformOs`を含める。fakeが受け取ったcommand本文やresponse payloadをstderrへ出さない。

Run:

```bash
just test-python tests/test_codex_connection.py tests/test_codex_rpc.py -v
```

Expected: PASS。macOSでもfakeは`sys.executable`経由で起動している。

### Task 5: runtime-generated schema contract

**Files:**
- Create: `src/moco/codex/schema.py`
- Create: `tests/test_codex_schema.py`
- Modify: `tests/fixtures/fake_codex.py`

- [ ] **Step 1: semantic resolutionのRED testsを書く**

synthetic schemaのmethod名はproduction名と異なる値にし、params type/titleまたはparameterless variantのrequest titleからsemanticを解決する。

```python
def test_contract_selects_methods_by_semantic_schema_signals(tmp_path: Path) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[
            schema_variant("future/account/read", params_title="GetAccountParams"),
            schema_variant(
                "future/config/read",
                params_title="ConfigReadParams",
                params_properties={"cwd": {"type": ["string", "null"]}},
            ),
            schema_variant(
                "future/requirements/read",
                request_title="ConfigRequirements/readRequest",
                params_schema={"type": "null"},
            ),
            schema_variant(
                "future/features/list",
                params_title="ExperimentalFeatureListParams",
                params_properties={"cursor": {"type": ["string", "null"]}},
            ),
            schema_variant(
                "future/realtime/voices",
                params_title="ThreadRealtimeListVoicesParams",
            ),
            schema_variant(
                "future/turn/interrupt",
                params_title="TurnInterruptParams",
                params_required={"threadId", "turnId"},
                params_properties={
                    "threadId": {"type": "string"},
                    "turnId": {"type": "string"},
                },
            ),
        ],
        server_variants=[
            schema_variant(
                "future/request/approval",
                params_title="CommandExecutionRequestApprovalParams",
            ),
            schema_variant("future/tool/call", params_title="DynamicToolCallParams"),
        ],
    )

    contract = load_generated_contract(tmp_path, version="codex-cli test")

    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "future/account/read"
    assert contract.require_method(SemanticMethod.CONFIG_READ).semantic_fields == frozenset(
        {"cwd"}
    )
    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) == ClientMethodContract(
        "future/requirements/read",
        ParamsKind.OMITTED,
    )
    assert contract.server_requests[ServerRequestCategory.COMMAND_APPROVAL] == frozenset(
        {"future/request/approval"}
    )
    assert contract.server_requests[ServerRequestCategory.DYNAMIC_TOOL_CALL] == frozenset(
        {"future/tool/call"}
    )


def test_parameterless_request_uses_request_title_not_a_fictional_params_type(
    tmp_path: Path,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[
            schema_variant(
                "renamed/requirements",
                request_title="ConfigRequirements/readRequest",
                params_schema={"type": "null"},
            )
        ],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="codex-cli test")

    assert contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ).name == (
        "renamed/requirements"
    )


def test_contract_rejects_ambiguous_semantic_method(tmp_path: Path) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[
            schema_variant("future/account/read-a", params_title="AccountReadParams"),
            schema_variant("future/account/read-b", params_title="GetAccountParams"),
        ],
        server_variants=[],
    )

    with pytest.raises(CodexSchemaError, match="ambiguous"):
        load_generated_contract(tmp_path, version="codex-cli test")


def test_optional_method_missing_required_semantic_field_is_unavailable(
    tmp_path: Path,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[
            schema_variant("future/config/read", params_title="ConfigReadParams")
        ],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="codex-cli test")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize(
    ("semantic", "variant"),
    [
        (
            SemanticMethod.ACCOUNT_READ,
            schema_variant(
                "future/account",
                params_title="GetAccountParams",
                params_required={"refreshToken"},
                params_properties={"refreshToken": {"type": "boolean"}},
            ),
        ),
        (
            SemanticMethod.CONFIG_READ,
            schema_variant(
                "future/config",
                params_title="ConfigReadParams",
                params_required={"cwd"},
                params_properties={"cwd": {"type": "integer"}},
            ),
        ),
        (
            SemanticMethod.REALTIME_VOICES_LIST,
            schema_variant(
                "future/voices",
                params_title="ThreadRealtimeListVoicesParams",
                params_required={"locale"},
                params_properties={"locale": {"type": "string"}},
            ),
        ),
    ],
)
def test_contract_rejects_request_shapes_the_adapter_cannot_construct(
    tmp_path: Path,
    semantic: SemanticMethod,
    variant: dict[str, JsonValue],
) -> None:
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="codex-cli test")

    assert contract.method(semantic) is None
    assert semantic in contract.missing_methods
```

schema probe testではfake CLIが`--experimental`を拒否するscenarioを使い、stable schemaへ一度だけretryする。どちらも失敗した場合はstderr本文をexceptionに含めない。

- [ ] **Step 2: schema testsのREDを確認する**

Run:

```bash
just test-python tests/test_codex_schema.py -v
```

Expected: schema module未定義のためFAIL。

- [ ] **Step 3: protocol contract loaderとprobeを実装する**

```python
class SemanticMethod(StrEnum):
    ACCOUNT_READ = "account_read"
    CONFIG_READ = "config_read"
    CONFIG_REQUIREMENTS_READ = "config_requirements_read"
    EXPERIMENTAL_FEATURE_LIST = "experimental_feature_list"
    REALTIME_VOICES_LIST = "realtime_voices_list"
    TURN_INTERRUPT = "turn_interrupt"


class ParamsKind(StrEnum):
    OBJECT = "object"
    OMITTED = "omitted"


class ServerRequestCategory(StrEnum):
    COMMAND_APPROVAL = "command_approval"
    FILE_CHANGE_APPROVAL = "file_change_approval"
    USER_INPUT = "user_input"
    MCP_ELICITATION = "mcp_elicitation"
    PERMISSION_APPROVAL = "permission_approval"
    DYNAMIC_TOOL_CALL = "dynamic_tool_call"
    AUTH_TOKEN_REFRESH = "auth_token_refresh"
    ATTESTATION = "attestation"
    CURRENT_TIME = "current_time"


VOICE_REQUIRED_METHODS = frozenset(
    {SemanticMethod.ACCOUNT_READ, SemanticMethod.REALTIME_VOICES_LIST}
)
AGENT_READINESS_METHODS = frozenset(set(SemanticMethod) - VOICE_REQUIRED_METHODS)
STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES = frozenset(
    {
        ServerRequestCategory.COMMAND_APPROVAL,
        ServerRequestCategory.FILE_CHANGE_APPROVAL,
    }
)


@dataclass(frozen=True, slots=True)
class ClientMethodContract:
    name: str
    params_kind: ParamsKind
    semantic_fields: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CodexProtocolContract:
    version: str
    methods: Mapping[SemanticMethod, ClientMethodContract]
    server_requests: Mapping[ServerRequestCategory, frozenset[str]]
    unclassified_server_request_count: int
    experimental_schema: bool

    def method(self, semantic: SemanticMethod) -> ClientMethodContract | None:
        return self.methods.get(semantic)

    def require_method(self, semantic: SemanticMethod) -> ClientMethodContract:
        try:
            return self.methods[semantic]
        except KeyError as error:
            raise CodexSchemaError("required Codex semantic method is unavailable") from error

    @property
    def missing_methods(self) -> frozenset[SemanticMethod]:
        return frozenset(set(SemanticMethod) - self.methods.keys())

    @property
    def server_request_categories(self) -> frozenset[ServerRequestCategory]:
        return frozenset(self.server_requests)
```

`load_generated_contract()`は`ClientRequest.json`と`ServerRequest.json`の`oneOf`を走査する。variant自体が`$ref`の場合はschema rootを越えないrelative file pathとRFC 6901 JSON pointerだけを解決し、既訪問`(path, pointer)`をsetで検出してcycleを拒否する。absolute path、`..`でbundle外へ出るpath、HTTP(S) refは`CodexSchemaError`にする。

client variantはparams `$ref` basename/titleを第一signal、request variant titleまたは`RequestMethod` suffixを`Request`へ正規化したmethod property titleをparameterless request用の第二signalとして照合する。method enum文字列そのものは選択signalにせず、解決後のtransport名としてだけ保存する。複数signalが異なるsemanticを示す場合、同じsemanticへ複数variantが一致する場合はambiguousとして拒否する。

解決したparams schemaは下記の`_InvocationSpec`と照合する。schema側required fieldがmocoのsupplied field集合の部分集合であること、mocoが送る各fieldのJSON型をschemaがすべて受理することを、resolved `$ref`、`type` list、`anyOf`をたどって確認する。これにより全propertiesを固定せず、実際に構築するsubsetだけを厳密に検証する。object paramsは`OBJECT`、request variantのrequired listにparamsを含まずparams schemaがnullのparameterless requestは`OMITTED`にする。明示null必須、object/null以外、未知required field、送信fieldの型不一致はそのmethodをcontractへ追加せず、`missing_methods`へ現す。`ClientMethodContract.semantic_fields`には検証済みsupplied field名だけを保存する。

```python
@dataclass(frozen=True, slots=True)
class _SemanticSignals:
    params_titles: frozenset[str]
    request_titles: frozenset[str]


@dataclass(frozen=True, slots=True)
class _InvocationSpec:
    params_kind: ParamsKind
    supplied_field_types: Mapping[str, frozenset[str]]


_CLIENT_SIGNALS: dict[SemanticMethod, _SemanticSignals] = {
    SemanticMethod.ACCOUNT_READ: _SemanticSignals(
        frozenset({"GetAccountParams", "AccountReadParams"}),
        frozenset({"Account/readRequest"}),
    ),
    SemanticMethod.CONFIG_READ: _SemanticSignals(
        frozenset({"ConfigReadParams"}),
        frozenset({"Config/readRequest"}),
    ),
    SemanticMethod.CONFIG_REQUIREMENTS_READ: _SemanticSignals(
        frozenset(),
        frozenset({"ConfigRequirements/readRequest"}),
    ),
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: _SemanticSignals(
        frozenset({"ExperimentalFeatureListParams"}),
        frozenset({"ExperimentalFeature/listRequest"}),
    ),
    SemanticMethod.REALTIME_VOICES_LIST: _SemanticSignals(
        frozenset({"ThreadRealtimeListVoicesParams"}),
        frozenset({"Thread/realtime/listVoicesRequest"}),
    ),
    SemanticMethod.TURN_INTERRUPT: _SemanticSignals(
        frozenset({"TurnInterruptParams"}),
        frozenset({"Turn/interruptRequest"}),
    ),
}

_CLIENT_INVOCATIONS: dict[SemanticMethod, _InvocationSpec] = {
    SemanticMethod.ACCOUNT_READ: _InvocationSpec(ParamsKind.OBJECT, {}),
    SemanticMethod.CONFIG_READ: _InvocationSpec(
        ParamsKind.OBJECT,
        {"cwd": frozenset({"string"})},
    ),
    SemanticMethod.CONFIG_REQUIREMENTS_READ: _InvocationSpec(
        ParamsKind.OMITTED,
        {},
    ),
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: _InvocationSpec(
        ParamsKind.OBJECT,
        {"cursor": frozenset({"string", "null"})},
    ),
    SemanticMethod.REALTIME_VOICES_LIST: _InvocationSpec(ParamsKind.OBJECT, {}),
    SemanticMethod.TURN_INTERRUPT: _InvocationSpec(
        ParamsKind.OBJECT,
        {
            "threadId": frozenset({"string"}),
            "turnId": frozenset({"string"}),
        },
    ),
}

_SERVER_PARAM_CATEGORIES: dict[ServerRequestCategory, frozenset[str]] = {
    ServerRequestCategory.COMMAND_APPROVAL: frozenset(
        {"CommandExecutionRequestApprovalParams", "ExecCommandApprovalParams"}
    ),
    ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset(
        {"FileChangeRequestApprovalParams", "ApplyPatchApprovalParams"}
    ),
    ServerRequestCategory.USER_INPUT: frozenset({"ToolRequestUserInputParams"}),
    ServerRequestCategory.MCP_ELICITATION: frozenset(
        {"McpServerElicitationRequestParams"}
    ),
    ServerRequestCategory.PERMISSION_APPROVAL: frozenset(
        {"PermissionsRequestApprovalParams"}
    ),
    ServerRequestCategory.DYNAMIC_TOOL_CALL: frozenset({"DynamicToolCallParams"}),
    ServerRequestCategory.AUTH_TOKEN_REFRESH: frozenset(
        {"ChatgptAuthTokensRefreshParams"}
    ),
    ServerRequestCategory.ATTESTATION: frozenset({"AttestationGenerateParams"}),
    ServerRequestCategory.CURRENT_TIME: frozenset({"CurrentTimeReadParams"}),
}
```

server variantはparams typeからsemantic categoryへ分類し、同categoryのnew/legacy methodを一つの`frozenset`へ集約する。未知variantはraw methodを公開せずcountだけを保持する。production method名、件数、payload propertiesは比較しない。`CodexSchemaProbe`はconstructorで`CodexCommand`を受け取り、`probe_sync()`と、それを`asyncio.to_thread()`で包む`async probe()`を提供する。一時directory内で`command.version_argv()`とexperimental schema生成を実行し、experimental optionを利用できない場合だけstable生成へretryする。生成物はcontext終了時に削除し、contractは必要情報をimmutableなmappingと`frozenset`へcopyしてから返す。

- [ ] **Step 4: fake schema CLIを実装しtestsをGREENにする**

fakeの`--version`は固定test文字列を返し、`app-server generate-json-schema --out DIR`はtest用minimal `ClientRequest.json`と`ServerRequest.json`を書く。schema全field集合をproductionからcopyしない。fake CLI processを起動するprobe testだけに`@pytest.mark.integration`を付け、pure loader testsは通常unit testのままにする。

Run:

```bash
just test-python tests/test_codex_schema.py -v
```

Expected: PASS。schema一時directoryがtest終了後に残らない。

### Task 6: Stage A capability snapshot

**Files:**
- Create: `src/moco/codex/capabilities.py`
- Create: `tests/test_codex_capabilities.py`

- [ ] **Step 1: arbitrary method aliasを使うRED testsを書く**

```python
async def test_discovers_stage_a_snapshot_without_fixed_method_names(
    tmp_path: Path,
) -> None:
    contract = make_contract(
        account="future/account",
        config="future/config",
        requirements="future/requirements",
        features="future/features",
        voices="future/voices",
        interrupt="future/interrupt",
        server_requests={
            ServerRequestCategory.COMMAND_APPROVAL: {"future/approval"},
            ServerRequestCategory.FILE_CHANGE_APPROVAL: {"future/file-approval"},
        },
    )
    rpc = FakeRequester(
        {
            "future/account": {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True},
            "future/config": {
                "config": {"sandbox_mode": "workspace-write", "approval_policy": "on-request"},
                "origins": {},
            },
            "future/requirements": {"requirements": {}},
            "future/features": {
                "data": [{"name": "realtime_conversation", "enabled": True}],
                "nextCursor": None,
            },
            "future/voices": {"voices": {"v-next": ["fixture-voice"]}},
        }
    )

    snapshot = await CapabilityDiscovery(
        rpc,
        contract=contract,
        working_directory=tmp_path,
    ).discover()

    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.effective_policy == EffectivePolicy(
        SandboxMode.WORKSPACE_WRITE,
        ApprovalMode.ON_REQUEST,
    )
    assert rpc.requests_for("future/config") == [{"cwd": str(tmp_path)}]
    assert rpc.requests_for("future/features") == [{"cursor": None}]
    assert snapshot.agent_admission.status is CapabilityStatus.AVAILABLE
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE
    assert snapshot.interrupt.status is CapabilityStatus.AVAILABLE
    assert snapshot.server_request_categories == frozenset(
        {
            ServerRequestCategory.COMMAND_APPROVAL,
            ServerRequestCategory.FILE_CHANGE_APPROVAL,
        }
    )


async def test_blocks_only_unsafe_voice_policy_combination(tmp_path: Path) -> None:
    snapshot = await discover_with_policy(
        "danger-full-access",
        "never",
        working_directory=tmp_path,
    )

    assert snapshot.effective_policy == EffectivePolicy(
        SandboxMode.DANGER_FULL_ACCESS,
        ApprovalMode.NEVER,
    )
    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.DISABLED,
        "unsafe_voice_policy",
    )
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE


async def test_agent_admission_requires_next_stage_approval_categories(
    tmp_path: Path,
) -> None:
    snapshot = await discover_with_policy(
        "workspace-write",
        "on-request",
        working_directory=tmp_path,
        server_requests={
            ServerRequestCategory.COMMAND_APPROVAL: {"future/approval"},
        },
    )

    assert snapshot.policy_state.status is CapabilityStatus.AVAILABLE
    assert snapshot.server_requests.status is CapabilityStatus.VERSION_MISMATCH
    assert snapshot.agent_admission.status is CapabilityStatus.VERSION_MISMATCH


async def test_granular_approval_is_validated_then_reduced_to_stable_category(
    tmp_path: Path,
) -> None:
    snapshot = await discover_with_policy(
        "workspace-write",
        {
            "granular": {
                "mcp_elicitations": False,
                "rules": True,
                "sandbox_approval": True,
            }
        },
        working_directory=tmp_path,
    )

    assert snapshot.effective_policy == EffectivePolicy(
        SandboxMode.WORKSPACE_WRITE,
        ApprovalMode.GRANULAR,
    )
    assert "mcp_elicitations" not in repr(snapshot)


async def test_malformed_granular_approval_is_version_mismatch(tmp_path: Path) -> None:
    snapshot = await discover_with_policy(
        "workspace-write",
        {"granular": {"rules": "sometimes"}},
        working_directory=tmp_path,
    )

    assert snapshot.effective_policy is None
    assert snapshot.policy_state.status is CapabilityStatus.VERSION_MISMATCH


async def test_optional_probe_failure_does_not_hide_voice_readiness(
    tmp_path: Path,
) -> None:
    contract = make_contract(
        account="future/account",
        config="future/config",
        voices="future/voices",
    )
    rpc = FakeRequester(
        {
            "future/account": {
                "account": {"type": "apiKey"},
                "requiresOpenaiAuth": True,
            },
            "future/config": CodexRpcError("fixture failure"),
            "future/voices": {"voices": {"v-next": ["fixture-voice"]}},
        }
    )

    snapshot = await CapabilityDiscovery(
        rpc,
        contract=contract,
        working_directory=tmp_path,
    ).discover()

    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE
    assert snapshot.policy_state.status is CapabilityStatus.ERROR
    assert snapshot.agent_admission.status is CapabilityStatus.ERROR


async def test_optional_agent_readiness_method_absence_does_not_block_voice(
    tmp_path: Path,
) -> None:
    contract = make_contract(
        account="future/account",
        voices="future/voices",
    )
    rpc = FakeRequester(
        {
            "future/account": {
                "account": {"type": "apiKey"},
                "requiresOpenaiAuth": True,
            },
            "future/voices": {"voices": {"v-next": ["fixture-voice"]}},
        }
    )

    snapshot = await CapabilityDiscovery(
        rpc,
        contract=contract,
        working_directory=tmp_path,
    ).discover()

    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE
    assert snapshot.policy_state.status is CapabilityStatus.VERSION_MISMATCH
    assert snapshot.interrupt.status is CapabilityStatus.VERSION_MISMATCH
    assert snapshot.agent_admission.status is CapabilityStatus.VERSION_MISMATCH
```

unknown sandbox、unknown approval、config shape欠損は`VERSION_MISMATCH`、account nullかつ`requiresOpenaiAuth=True`は`AUTHENTICATION_REQUIRED`にする。voice名や件数はsnapshotへ保存しない。

- [ ] **Step 2: capability testsのREDを確認する**

Run:

```bash
just test-python tests/test_codex_capabilities.py -v
```

Expected: capability typesが未定義のためFAIL。

- [ ] **Step 3: bounded snapshotを実装する**

```python
class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    DISABLED = "disabled"
    AUTHENTICATION_REQUIRED = "authentication_required"
    VERSION_MISMATCH = "version_mismatch"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CapabilityState:
    status: CapabilityStatus
    detail: str


class SandboxMode(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalMode(StrEnum):
    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"
    GRANULAR = "granular"


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    sandbox: SandboxMode
    approval: ApprovalMode


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    version: str
    account: CapabilityState
    effective_policy: EffectivePolicy | None
    policy_state: CapabilityState
    managed_requirements: CapabilityState
    agent_admission: CapabilityState
    realtime: CapabilityState
    interrupt: CapabilityState
    server_requests: CapabilityState
    server_request_categories: frozenset[ServerRequestCategory]
    has_unclassified_server_requests: bool
```

`CapabilityDiscovery`はRPC、absolute `working_directory`、`contract`または`contract_probe`のちょうど一方を受け取る。`discover()`はprobeを使う場合に`await contract_probe.probe()`を一度だけ実行する。各`ClientMethodContract`は`ParamsKind.OMITTED`なら既存`request(method)`契約でparams field自体を省略し、`ParamsKind.OBJECT`なら検証済みinvocationだけを構築する。account/voicesは空object、config readは`{"cwd": str(working_directory)}`、feature list初回は`{"cursor": None}`、以後は返却されたcursor文字列、interruptはStage Aでは呼ばない。transport method名や全payload field集合を別のmoco設定へ複製しない。

Voice必須のaccountまたはvoice-list semanticが欠損すれば、それぞれ`VERSION_MISMATCH`にする。config、config requirements、feature list、interruptはAgent readiness用optionalであり、欠損した個別stateだけを`VERSION_MISMATCH`にしてaccount/Realtimeの成功を潰さない。各probeの非terminal RPC errorも個別`ERROR`へ閉じ込め、connection lossのようなterminal errorだけは残りの未実行stateを`ERROR/probe_failed`にする。feature listが存在する場合は`nextCursor`が文字列の間だけ同じsemantic methodへ`{"cursor": value}`を送り、Realtime disabledを尊重する。feature list自体が欠損または非terminal failureでもvoice-list probeが成功すればRealtimeはavailableとする。

account objectがある場合、または`requiresOpenaiAuth`がfalseの場合をaccount readyとし、accountがなく認証必須なら`AUTHENTICATION_REQUIRED`にする。effective sandboxは`SandboxMode`の三値だけ、approvalは`ApprovalMode`へ正規化し、unknown値をfallbackしない。granular objectはtop-levelが`{"granular": object}`だけで、nested mappingが非空の`str -> bool`であることを検証した後、個別key/valueを保持せず`ApprovalMode.GRANULAR`へ縮約する。policy semanticが欠損・不正なら`effective_policy=None`、`policy_state=VERSION_MISMATCH`、`agent_admission=VERSION_MISMATCH`にする。

server request stateはsemantic categoryだけをsnapshotへ写す。Stage B必須categoryが欠ける場合、またはunclassified variantがある場合は`VERSION_MISMATCH`、それ以外は`AVAILABLE`とするが、Stage AのVoice gateには使わない。`agent_admission`はaccount、safe effective policy、Stage B必須のcommand/file approval categoryがすべて揃った場合だけ`AVAILABLE`にする。unclassifiedな追加categoryは`server_requests`をdegradedにするが、必須categoryが揃う限りadmission全体は止めず、将来そのrequestを受けたturnがfail-closedになる。raw method名はdispatcher構築用の`CodexProtocolContract`内だけに留め、snapshot、doctor、browser、telemetryへ出さない。生response、account、voice IDもdiscovery外へ返さない。

- [ ] **Step 4: capability testsをGREENにする**

Run:

```bash
just test-python tests/test_codex_capabilities.py -v
```

Expected: PASS。`repr(snapshot)`にaccount email、voice ID、config pathが含まれない。

### Task 7: owner-private runtime stateとWindows ACL

**Files:**
- Create: `src/moco/runtime/private_state.py`
- Create: `src/moco/runtime/_windows_acl.py`
- Create: `tests/test_private_state.py`
- Modify: `src/moco/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: pure security policyとCLIのRED testsを書く**

```python
def test_windows_snapshot_requires_current_user_owner_and_only_trusted_allow_aces() -> None:
    safe = WindowsSecuritySnapshot(
        owner_sid="S-1-user",
        current_user_sid="S-1-user",
        allowed_sids=frozenset({"S-1-user", "S-1-system", "S-1-admins"}),
        trusted_sids=frozenset({"S-1-user", "S-1-system", "S-1-admins"}),
        null_dacl=False,
        reparse_point=False,
    )

    validate_windows_security(safe)
    with pytest.raises(PrivateStateError, match="owner"):
        validate_windows_security(replace(safe, owner_sid="S-1-sandbox"))
    with pytest.raises(PrivateStateError, match="access"):
        validate_windows_security(
            replace(safe, allowed_sids=safe.allowed_sids | {"S-1-sandbox"})
        )
    with pytest.raises(PrivateStateError, match="reparse"):
        validate_windows_security(replace(safe, reparse_point=True))


def test_unsafe_existing_directory_is_not_repaired(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o755)

    with pytest.raises(PrivateStateError):
        prepare_private_runtime_directory(private, platform_name="darwin")

    assert stat.S_IMODE(private.stat().st_mode) == 0o755
```

CLI testsは`run`/`open`から公開`--state-path`が消え、testでは`default_runtime_state_path`をmonkeypatchすることを確認する。state read/writeはmedia capabilityをstdoutへ出さない。

- [ ] **Step 2: private state testsのREDを確認する**

Run:

```bash
just test-python tests/test_private_state.py tests/test_cli.py -k 'private or runtime_state or open' -v
```

Expected: private state moduleが未定義でFAIL。

- [ ] **Step 3: Windows dependencyとsecurity snapshotを実装する**

`pyproject.toml`の既存`dependencies`配列へWindows限定runtime dependencyを一行追加し、mypyにはmodule overrideを置く。

```toml
  "pywin32>=311,<400; sys_platform == 'win32'",

[[tool.mypy.overrides]]
module = ["win32api", "win32con", "win32security"]
ignore_missing_imports = true
```

既存`[tool.coverage.run]` tableへ次を追加する。

```toml
omit = ["src/moco/runtime/_windows_acl.py"]
```

coverageから除外するのはpywin32 binding fileだけである。security判断を行う`WindowsSecuritySnapshot`と`validate_windows_security()`は`private_state.py`に置いて通常coverageで検証し、binding自体はWindows matrixの実DACL testで実行する。

Run:

```bash
uv lock
```

Expected: Windows wheelだけがconditional resolutionへ追加され、既存Irodori pinは変わらない。

`src/moco/runtime/private_state.py`へpure policy valueとvalidatorを置く。

```python
@dataclass(frozen=True, slots=True)
class WindowsSecuritySnapshot:
    owner_sid: str
    current_user_sid: str
    allowed_sids: frozenset[str]
    trusted_sids: frozenset[str]
    null_dacl: bool
    reparse_point: bool


def validate_windows_security(snapshot: WindowsSecuritySnapshot) -> None:
    if snapshot.reparse_point:
        raise PrivateStateError("runtime-private must not be a reparse point")
    if snapshot.owner_sid != snapshot.current_user_sid:
        raise PrivateStateError("runtime-private owner is not the current user")
    if snapshot.null_dacl or not snapshot.allowed_sids <= snapshot.trusted_sids:
        raise PrivateStateError("runtime-private grants access to an untrusted principal")
```

`src/moco/runtime/_windows_acl.py`は`GetNamedSecurityInfo`、current process token、well-known SYSTEM/Administrators SIDを構造的に読み、locale依存の`icacls` textを解析しない。allow ACEのSIDだけを`allowed_sids`へ入れ、null DACL、reparse point、owner/current/trusted SIDsをsnapshotにする。未知ACE typeは安全と推測せず`PrivateStateError`にする。

```python
from __future__ import annotations

import os
import stat
from pathlib import Path

import win32api
import win32con
import win32security

from moco.errors import PrivateStateError
from moco.runtime.private_state import WindowsSecuritySnapshot


def read_windows_security(path: Path) -> WindowsSecuritySnapshot:
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    current_user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    administrators = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid,
        None,
    )
    trusted = frozenset(
        win32security.ConvertSidToStringSid(sid)
        for sid in (current_user, system, administrators)
    )
    allowed: set[str] = set()
    if dacl is not None:
        allowed_types = {
            win32security.ACCESS_ALLOWED_ACE_TYPE,
            win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        }
        denied_types = {
            win32security.ACCESS_DENIED_ACE_TYPE,
            win32security.ACCESS_DENIED_OBJECT_ACE_TYPE,
        }
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            ace_type = ace[0][0]
            if ace_type in allowed_types:
                allowed.add(win32security.ConvertSidToStringSid(ace[-1]))
            elif ace_type not in denied_types:
                raise PrivateStateError("runtime-private contains an unknown ACE type")
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return WindowsSecuritySnapshot(
        owner_sid=win32security.ConvertSidToStringSid(owner),
        current_user_sid=win32security.ConvertSidToStringSid(current_user),
        allowed_sids=frozenset(allowed),
        trusted_sids=trusted,
        null_dacl=dacl is None,
        reparse_point=bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT),
    )
```

- [ ] **Step 4: OS-neutral private state APIを実装する**

`prepare_private_runtime_directory()`はWindowsで親`moco`までを作り、leafが存在しない場合だけ`mkdir(mode=0o700)`する。leafが既存なら修復しない。POSIXではcurrent UID owner、directory `0700`、file `0600`を要求する。read/writeのたびに親を再検証する。

```python
def write_private_state(path: Path, content: bytes, *, platform_name: str | None = None) -> None:
    platform_value = platform_name or sys.platform
    prepare_private_runtime_directory(path.parent, platform_name=platform_value)
    descriptor, raw_temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(raw_temporary)
    try:
        if platform_value != "win32":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        validate_private_state_file(path, platform_name=platform_value)
    finally:
        if temporary.exists():
            temporary.unlink()
```

`src/moco/cli.py`はruntime stateに対する`_atomic_write`、`_remove_state_file`、POSIX-only `_read_state_url` mode判定をこのAPIへ置換する。`config init`が使う既存atomic writerはruntime-private APIへ流用せず、config専用の`_write_config_atomically()`として残す。public `run`/`open`の`--state-path` optionを削除し、`default_runtime_state_path()`をcommand実行時に解決する。内部`_run_runtime(..., state_path=...)`はtest injection用に残す。

- [ ] **Step 5: private state testsをGREENにする**

Run:

```bash
just test-python tests/test_private_state.py tests/test_cli.py -v
```

Expected: macOS/Linux testsがPASS。Windows専用integration testは`sys.platform == "win32"`のときだけ実DACLを読み、新規safe directoryのowner/trusted allow ACEを確認する。

### Task 8: service、hotkey、browserの小さなOS差分

**Files:**
- Modify: `src/moco/platform.py`
- Modify: `src/moco/cli.py`
- Modify: `src/moco/doctor.py`
- Modify: `tests/test_platform.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_launchd.py`

- [ ] **Step 1: Windows unsupported/fallbackのRED testsを書く**

```python
@pytest.mark.parametrize("command", ["install", "start", "stop", "status", "uninstall"])
def test_windows_service_commands_never_call_launchd(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "service_supported", lambda: False)
    monkeypatch.setattr(cli, "start_service", lambda: calls.append("launchd"))

    result = runner.invoke(app, ["service", command])

    assert result.exit_code == 1
    assert "ERROR [service]: unsupported_platform" in result.output
    assert calls == []


def test_hotkey_detail_is_host_specific() -> None:
    assert hotkey_unavailable_detail(platform_name="darwin") == "input_monitoring_required"
    assert hotkey_unavailable_detail(platform_name="win32") == "browser_hotkey_fallback"
```

`install`/`uninstall` testには必須CLI引数を与える。Windows runtime warningに`Input Monitoring`が含まれないtestも追加する。

- [ ] **Step 2: focused testsのREDを確認する**

Run:

```bash
just test-python tests/test_platform.py tests/test_cli.py tests/test_doctor.py -k 'service or hotkey or browser' -v
```

Expected: platform seamが未実装のためFAIL。

- [ ] **Step 3: branchだけを追加する**

```python
def service_supported(*, platform_name: str | None = None) -> bool:
    return (platform_name or sys.platform) == "darwin"


def hotkey_unavailable_detail(*, platform_name: str | None = None) -> str:
    return (
        "browser_hotkey_fallback"
        if (platform_name or sys.platform) == "win32"
        else "input_monitoring_required"
    )
```

各service commandは最初に`service_supported()`を確認し、falseなら共通 `_exit_unsupported_service()`で終了する。`launchd.py`へWindows abstractionを追加しない。browser openは`moco.platform.open_browser()`だけを呼ぶ。`tests/test_launchd.py`はmodule-levelでWindowsだけskipし、macOS/Linux上の純粋launchd unit testsは維持する。

- [ ] **Step 4: OS surface testsをGREENにする**

Run:

```bash
just test-python tests/test_platform.py tests/test_cli.py tests/test_doctor.py tests/test_launchd.py -v
```

Expected: PASS。Windows向け文言に`Input Monitoring`がない。

### Task 9: doctorをCapabilitySnapshot consumerへ変更する

**Files:**
- Modify: `src/moco/doctor.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: stable readiness projectionのRED testsを書く**

既存`FakeRpc`の固定method分岐を削除し、`FakeCapabilityDiscovery`を注入する。

```python
async def test_doctor_projects_stage_a_codex_snapshot_without_private_values(
    tmp_path: Path,
) -> None:
    snapshot = make_snapshot(
        account=CapabilityState(CapabilityStatus.AVAILABLE, "authenticated"),
        effective_policy=EffectivePolicy(
            SandboxMode.WORKSPACE_WRITE,
            ApprovalMode.ON_REQUEST,
        ),
        policy_state=CapabilityState(CapabilityStatus.AVAILABLE, "workspace_write_on_request"),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "allowed"),
        realtime=CapabilityState(CapabilityStatus.AVAILABLE, "available"),
        interrupt=CapabilityState(CapabilityStatus.AVAILABLE, "available"),
        server_requests=CapabilityState(CapabilityStatus.AVAILABLE, "discovered"),
    )

    checks = await run_doctor(
        MocoSettings(codex=CodexSettings(command=("fixture-codex",))),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    by_code = {check.code: check for check in checks}
    assert by_code["codex_schema"] == DoctorCheck("codex_schema", "ok", "compatible")
    assert by_code["codex_policy"] == DoctorCheck(
        "codex_policy", "ok", "workspace_write_on_request"
    )
    assert by_code["codex_agent_admission"] == DoctorCheck(
        "codex_agent_admission", "ok", "allowed"
    )
    assert "fixture-codex" not in repr(checks)
```

unsafe policyは`codex_policy=ok/danger_full_access_never`と`codex_agent_admission=error/unsafe_voice_policy`に分ける。schema mismatch、authentication required、Realtime unavailable、interrupt unavailableも各codeへ直接写す。

- [ ] **Step 2: doctor testsのREDを確認する**

Run:

```bash
just test-python tests/test_doctor.py -k 'stage_a_codex or schema or policy or admission or realtime or interrupt' -v
```

Expected:旧`codex_features`/`codex_voices` projectionのためFAIL。

- [ ] **Step 3: duplicated method probeをsnapshot consumerへ置換する**

Codex check codesを次へ固定する。

```python
_CODEX_CHECK_CODES = (
    "codex_command",
    "codex_schema",
    "codex_account",
    "codex_policy",
    "codex_agent_admission",
    "codex_realtime",
    "codex_interrupt",
    "codex_server_requests",
)
```

`run_doctor()`は一度だけcommandと`settings.codex.working_directory or Path.cwd()`をresolveし、一つのsupervisorをstartし、そのabsolute working directoryを渡した一つのCapabilityDiscovery snapshotを取得する。外部errorはtypeだけをsafe logへ送り、未完了codeを`error/probe_failed`で埋める。binary path、version生文字列、account、schema path、server request method名をDoctorCheck detailへ入れない。

- [ ] **Step 4: doctor testsをGREENにする**

Run:

```bash
just test-python tests/test_doctor.py -v
```

Expected: PASS。旧`_realtime_feature_available`、`_realtime_voices_available`がunusedなら削除する。

### Task 10: VoiceSessionを新接続へ移して既存体験を回帰維持する

**Files:**
- Modify: `src/moco/codex/session.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/codex/__init__.py`
- Modify: `tests/test_codex_session.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_integration.py`

- [ ] **Step 1: readiness gateと回帰のRED testsを書く**

`CodexRealtimeSession`へ必須のdiscovery protocolを注入する。Voice開始はaccountとRealtimeだけを要求し、Agent admission blockedでは止めない。既存unit testも`make_available_snapshot()`を返すfake discoveryを必ず注入し、公開constructorからreadiness gateを迂回できる既定値は作らない。

既存`tests/test_integration.py`にはmodule-levelの`pytestmark = pytest.mark.integration`を追加する。Task 4のchild-process connection testとTask 5のschema probe testも同じmarker契約を使い、pure RPC/schema/session testsはunitのままにする。focused commandはTask 1で追加した`just test-python`を使うため、boundary testsも実行される。

```python
async def test_voice_allows_unsafe_agent_policy_when_realtime_is_ready(tmp_path: Path) -> None:
    rpc = FakeRpc()
    snapshot = make_snapshot(
        agent_admission=CapabilityState(CapabilityStatus.DISABLED, "unsafe_voice_policy"),
        account=CapabilityState(CapabilityStatus.AVAILABLE, "authenticated"),
        realtime=CapabilityState(CapabilityStatus.AVAILABLE, "available"),
    )
    session = CodexRealtimeSession(
        rpc,
        settings=make_settings(tmp_path),
        capability_discovery=FakeCapabilityDiscovery(snapshot),
    )

    assert await session.start("offer-sdp") == "answer-sdp"
    assert rpc.requests[-2][0] == "thread/start"
    await session.close()


@pytest.mark.parametrize(
    "field",
    ["account", "realtime"],
)
async def test_voice_rejects_required_readiness_failure(tmp_path: Path, field: str) -> None:
    snapshot = make_available_snapshot_with(
        **{field: CapabilityState(CapabilityStatus.ERROR, "unavailable")}
    )
    session = CodexRealtimeSession(
        FakeRpc(),
        settings=make_settings(tmp_path),
        capability_discovery=FakeCapabilityDiscovery(snapshot),
    )

    with pytest.raises(CodexCapabilityError, match="Voice readiness"):
        await session.start("offer-sdp")
```

既存`test_starts_ephemeral_read_only_audio_v3_session`、transcript、activity秘匿、SDP timeout、prompt reload、integration WAV testsは変更後も同じ期待値を維持する。

- [ ] **Step 2: Voice integration testsのREDを確認する**

Run:

```bash
just test-python tests/test_codex_session.py tests/test_web.py tests/test_integration.py -v
```

Expected: production factoryとreadiness protocol未実装部分がFAIL。既存期待値は変更しない。

- [ ] **Step 3: production compositionを新surfaceへ差し替える**

session protocolは`start/request/notifications/close`を提供するconnectionへ名前を合わせ、discovery protocolはconstructorの必須引数にする。`CodexRealtimeSession.start()`はconnection start後、thread start前にdiscoveryを一度だけ行う。required readiness失敗ではthreadを作らずconnectionをcloseする。

`src/moco/web/app.py`のfactoryは次の構成だけを行う。

```python
def _codex_session_factory(settings: MocoSettings) -> SessionFactory:
    def build() -> RealtimeSession:
        command = resolve_codex_command(settings.codex.command)
        working_directory = settings.codex.working_directory or Path.cwd()
        connection = CodexConnectionSupervisor(command)
        discovery = CapabilityDiscovery(
            connection,
            contract_probe=CodexSchemaProbe(command),
            working_directory=working_directory,
        )
        return CodexRealtimeSession(
            connection,
            settings=settings,
            capability_discovery=discovery,
        )

    return build
```

working directoryは`settings.codex.working_directory or Path.cwd()`をthread/startへ渡す。Stage Aではpayloadの`ephemeral=True`、`sandbox=read-only`、`approvalPolicy=never`、audio v3を変えない。

- [ ] **Step 4: Voice/Web testsをGREENにする**

Run:

```bash
just test-python tests/test_codex_session.py tests/test_web.py tests/test_integration.py -v
```

Expected: PASS。Browser message schema、SpeechQueue、Irodori request shapeは変わらない。

### Task 11: installed contract、portable CI、documentation、全gate

**Files:**
- Create: `tests/test_codex_contract.py`
- Modify: `pyproject.toml`
- Modify: `justfile`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_repository_contract.py`
- Modify: `README.md`

- [ ] **Step 1: repository/CI contractのRED testsを書く**

`tests/test_repository_contract.py`はYAMLをparseし、次をassertする。

```python
def test_ci_uses_one_full_gate_and_two_os_python_matrix() -> None:
    payload = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    jobs = payload["jobs"]
    full_gate_owners = [
        name
        for name, job in jobs.items()
        if any(
            "just check" in str(step.get("run", ""))
            for step in job.get("steps", [])
        )
    ]
    assert full_gate_owners == ["quality"]
    platform_job = jobs["python-platform"]
    matrix = platform_job["strategy"]["matrix"]["include"]
    assert {entry["os"] for entry in matrix} == {"macos-latest", "windows-latest"}
    rendered = yaml.safe_dump(platform_job)
    assert "just test-python" in rendered
    assert "setup-node" not in rendered
    assert "npm " not in rendered
    assert "playwright" not in rendered.casefold()
```

symlink contractは`Path.is_symlink()`ではなくGit indexのmodeを使う。

```python
def test_agent_instruction_is_tracked_as_a_symlink() -> None:
    entry = subprocess.run(
        ["git", "ls-files", "-s", "CLAUDE.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert entry.startswith("120000 ")
    target = subprocess.run(
        ["git", "show", ":CLAUDE.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert target.strip() == "AGENTS.md"
```

- [ ] **Step 2: installed Codex contract testを書く**

```python
pytestmark = pytest.mark.contract


def test_installed_codex_advertises_stage_a_semantics() -> None:
    command = resolve_codex_command(None)
    contract = CodexSchemaProbe(command).probe_sync()

    assert VOICE_REQUIRED_METHODS <= contract.methods.keys()
    assert contract.missing_methods <= AGENT_READINESS_METHODS
    assert STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES <= contract.server_requests.keys()
    assert all(
        isinstance(method, str) and method
        for methods in contract.server_requests.values()
        for method in methods
    )
    assert contract.version
```

testはVoice必須semanticと次段で必要なserver request categoryだけを要求し、Agent readiness用optional semanticの欠損は許容する。version、raw method string、method件数、server request集合、voice/tool名をassertしない。installed CLIがない場合はskipせず、手動contract commandとして明示FAILにする。

- [ ] **Step 3: marker、coverage、contract recipeを追加する**

`pyproject.toml`のdefault test markerからcontractを除外し、markerを登録する。

```toml
addopts = [
  "--strict-config",
  "--strict-markers",
  "-m",
  "not integration and not live and not slow and not contract",
]
markers = [
  "contract: requires an installed public Codex CLI but no account mutation",
  "integration: crosses a process, HTTP, or WebSocket boundary",
  "live: requires a real Codex account or Irodori host",
  "slow: unsuitable for the default feedback loop",
]
```

Task 1の`test-python`はそのまま使う。`test-cov`からcontractだけを追加で除外し、`contract-codex`を追加する。CLIの`-m`がini marker式を置き換えるため、platform suiteはintegrationを含む。

```make
test-cov:
    uv run pytest -m "not live and not slow and not contract" --cov --cov-report=term-missing --cov-report=xml
    npm run test:frontend

contract-codex:
    uv run pytest -m contract tests/test_codex_contract.py --durations=10
```

- [ ] **Step 4: macOS/Windows Python matrixへ置換する**

Ubuntu `quality`を唯一の`just check` ownerとして維持し、既存`macos-integration`を次へ置換する。action SHAは既存のpinned値を再利用する。

```yaml
  python-platform:
    name: Python platform / ${{ matrix.name }}
    runs-on: ${{ matrix.os }}
    timeout-minutes: 15
    strategy:
      fail-fast: false
      matrix:
        include:
          - name: macOS
            os: macos-latest
          - name: Windows
            os: windows-latest
    steps:
      - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1
        with:
          python-version: "3.13"
      - uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock
      - uses: extractions/setup-just@f8a3cce218d9f83db3a2ecd90e41ac3de6cdfd9b
      - name: Install Python dependencies
        run: uv sync --frozen
      - name: Run portable Python suite
        run: just test-python
```

Node、Playwright、secretlint、build、追加cache、shardingをplatform jobへ入れない。

- [ ] **Step 5: package metadataとREADMEを更新する**

`pyproject.toml`へ`Operating System :: Microsoft :: Windows` classifierを追加する。READMEへ次を明記する。

- macOS-firstだがWindows 11 foregroundをStage Aから検証する。
- `codex.command: null`はPATHを使い、Windows Store private pathを探索しない。
- Windowsは`APPDATA` config、`LOCALAPPDATA` owner-private stateを使う。
- Windows `moco service`は`unsupported_platform`で、foreground `moco run`が正式経路。
- Browser microphone/global hotkeyの実permissionは利用者がinteractive desktopで確認する。
- Stage AはVoice回帰とreadinessまでで、Agent handoff/approval UIは未提供。

- [ ] **Step 6: focused suite、runtime contract、全品質gateを順に実行する**

Run:

```bash
just test-python
just contract-codex
just check
git diff --check
```

Expected: すべてPASS。`just contract-codex`は実行端末のinstalled Codexから一時schemaを生成し、method名やversionを固定せずStage A semantic categoryを解決する。

- [ ] **Step 7: Mac実機とWindows実機のStage A acceptanceを行う**

Mac:

```bash
just doctor
just contract-codex
just run
```

Windowsはtraditional OpenSSH over Tailscaleでsource/testを準備し、interactive desktopでforeground実行する。Tailscale Serve、login、service、privacy permissionを変更しない。

```powershell
just test-python
just contract-codex
just doctor
just run
```

Expected:

- 両OSでfake bidirectional request、string/int ID、unknown request、connection lossが合格する。
- 両OSでinstalled schemaから同じsemantic categoryを解決する。
- macOSの既存Voice→Irodori再生が維持される。
- Windowsのruntime-private owner/DACLが適合し、unsafe既存directoryは修復・迂回されない。
- Windows serviceはunsupported、hotkey unavailable時はbrowser fallbackを表示する。
- effective `danger-full-access + never`ならdoctorはAgent admissionだけを`unsafe_voice_policy`として失敗させ、Realtime readyを偽らない。

## 完了条件

- `RpcPeer`がrequest、response、notification、server requestを排他的に分類する。
- integer/string server Request IDを保持し、server requestへexactly onceで応答する。
- client/incoming pending mapとnotification subscriberが互いのmessageを奪わない。
- malformed overlap、boolean ID、duplicate incoming ID、unknown request、handler failure、connection lossがfail-closedになる。
- supervisorがprocess、initialize metadata、stderr drain、shutdownを一つのownerに集約する。
- installed schemaからStage A semantic methodとserver request categoryを導出し、version/method一覧を固定しない。
- CapabilitySnapshotがaccount、effective policy、managed requirements、Realtime、interrupt、server requestsをbounded stateで表す。
- current Codex policyをmoco独自allowlistへ複製せず、unsafe voice admissionだけを別stateで示す。
- WindowsがPATH Codex、APPDATA/LOCALAPPDATA、foreground run、owner-private state、browser fallbackを使う。
- Store private resource、remote stdio proxy、Cloudflare/Tailscale remote approvalを導入しない。
- Stage BのAgent/Reviewer/profile implementationが混入しない。
- `just test-python`、Mac/Windows `just contract-codex`、`just check`が成功する。
