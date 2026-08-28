# Cloudflare Access Operator Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cloudflare Access session が有効な本人だけが、moco capability なしで固定公開 URL から operator HTTP / WebSocket を利用できるようにする。

**Architecture:** 公開 Host は Cloudflare が付与する Access JWT を moco の独立した verifier で検証し、loopback Host は既存 capability を検証する。JWT verifier は strict configuration、bounded JWKS fetch、in-memory cache を所有し、FastAPI は Host / Origin に応じて二つの認証境界を明示的に分岐する。登録済み公開 WebSocket は server-enforced の最大 60 秒 connection lease を browser の Access 認証付き same-origin POST で約 20 秒ごとに更新し、logout / revoke / JWT expiry 後に既存接続を残さない。公開 browser は capability を保存・送信せず、QR と runtime mobile URL も秘密を含まない固定公開 URL にする。

**Tech Stack:** Python 3.13、FastAPI / Starlette、Pydantic v2、httpx、PyJWT + cryptography、Node.js test runner / JSDOM、Cloudflare Tunnel / Access、uv / just

---

## 実装前提とファイル責務

- `src/moco/config.py`: Cloudflare Access の非秘密設定と public URL との strict な組み合わせだけを所有する。
- `src/moco/web/access.py`: Access assertion、JWKS 取得・cache、JWT claim 検証、ASCII identity と JWT expiry の検証結果を所有する。FastAPI の routing や moco capability は知らない。
- `src/moco/web/app.py`: HTTP / WebSocket の Host / Origin を分類し、公開 request は Access verifier、loopback request は capability へ委譲する。公開 WebSocket の opaque connection lease、更新 endpoint、server deadline、close/unregister を同一 state で所有する。
- `src/moco/web/pairing.py`: capability を含まない公開 URL の QR だけを生成する。
- `src/moco/cli.py`: runtime state の local owner URL と bare mobile URL を安全に直列化・検証する。
- `src/moco/web/static/app.js`: loopback だけで capability を読み、公開 origin の旧 capability を消し、接続前に認証状態を確認する。公開接続では lease token を memory 内だけに保持し、約 20 秒ごとに Access 認証付き same-origin POST で更新する。
- `tests/test_cloudflare_access.py`: JWT / JWKS 境界の unit test。実 Cloudflare や process 環境変数へ依存しない。
- `tests/test_config.py`、`tests/test_web.py`、`tests/test_cli.py`、`tests/js/app.test.js`: 各既存境界の回帰 test。
- `config/moco.example.yaml`、`README.md`: 新しい設定・運用契約。
- `/Users/sankenbisha/Library/Application Support/moco/moco.yaml`: owner-private な実運用設定。repository へ追加しない。
- `/Users/sankenbisha/.cloudflared/moco.yml`: tunnel の Access JWT 必須検証。repository へ追加しない。

既存の未コミット差分は別作業として保持する。各 commit はこの計画で列挙した path だけを
明示的に `git add` し、無関係な差分を含めない。

### Task 1: Strict Cloudflare Access configuration

**Files:**
- Modify: `src/moco/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_doctor.py`
- Modify: `config/moco.example.yaml`
- Modify: `README.md`

- [ ] **Step 1: 公開 URL と Access 設定の RED test を追加する**

`tests/test_config.py` に `CloudflareAccessSettings` の import と次の test を追加する。

```python
ACCESS_SETTINGS = CloudflareAccessSettings(
    team_domain="https://owner.cloudflareaccess.com",
    audience="A" * 64,
    allowed_email="owner@example.com",
)


def test_public_operator_requires_cloudflare_access_settings() -> None:
    with pytest.raises(ValidationError, match="cloudflare_access"):
        ServerSettings(public_url="https://voice.example.com")


def test_cloudflare_access_requires_public_operator_url() -> None:
    with pytest.raises(ValidationError, match="public_url"):
        ServerSettings(cloudflare_access=ACCESS_SETTINGS)


def test_cloudflare_access_settings_are_normalized() -> None:
    settings = CloudflareAccessSettings(
        team_domain=" HTTPS://Owner.CloudflareAccess.com/ ",
        audience="A" * 64,
        allowed_email="Owner@Example.COM",
    )

    assert settings.team_domain == "https://owner.cloudflareaccess.com"
    assert settings.allowed_email == "Owner@Example.COM"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("team_domain", "http://owner.cloudflareaccess.com"),
        ("team_domain", "https://owner.cloudflareaccess.com/path"),
        ("team_domain", "https://owner.example.com"),
        ("audience", ""),
        ("audience", "contains space"),
        ("audience", "A" * 257),
        ("allowed_email", "not-an-email"),
        ("allowed_email", "owner@example.com\nsecond@example.com"),
    ],
)
def test_cloudflare_access_rejects_unsafe_values(field: str, value: str) -> None:
    values = {
        "team_domain": "https://owner.cloudflareaccess.com",
        "audience": "A" * 64,
        "allowed_email": "owner@example.com",
    }
    values[field] = value

    with pytest.raises(ValidationError):
        CloudflareAccessSettings.model_validate(values)
```

既存の `ServerSettings(public_url=...)` test fixture はすべて
`cloudflare_access=ACCESS_SETTINGS` を明示する。YAML test も同じ三値を inline mapping で
渡し、環境変数を参照しない。

- [ ] **Step 2: 設定 test が期待どおり失敗することを確認する**

Run:

```bash
uv run pytest tests/test_config.py tests/test_web.py tests/test_cli.py tests/test_doctor.py -k 'public_url or cloudflare_access or pairing or runtime_writes' -v
```

Expected: `CloudflareAccessSettings` が未定義、または public URL と Access 設定の相互必須制約がないため FAIL。

- [ ] **Step 3: strict settings を最小実装する**

`src/moco/config.py` へ次の型を `ServerSettings` より前に追加し、`ServerSettings` に
`cloudflare_access` と model validator を追加する。

```python
_MAX_ACCESS_AUDIENCE_LENGTH = 256
_MAX_ACCESS_EMAIL_LENGTH = 254


class CloudflareAccessSettings(StrictSettings):
    team_domain: str
    audience: str
    allowed_email: str

    @field_validator("team_domain")
    @classmethod
    def _validate_team_domain(cls, value: str) -> str:
        candidate = value.strip()
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        try:
            port = parsed.port
        except ValueError as error:
            msg = "Cloudflare Access team domain must be an HTTPS cloudflareaccess.com origin"
            raise ValueError(msg) from error
        labels = (hostname or "").rstrip(".").split(".")
        labels_valid = len(labels) >= 3 and all(
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
            or not labels_valid
            or not hostname.casefold().endswith(".cloudflareaccess.com")
            or hostname.casefold() == "cloudflareaccess.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            msg = "Cloudflare Access team domain must be an HTTPS cloudflareaccess.com origin"
            raise ValueError(msg)
        return f"https://{hostname.casefold()}"

    @field_validator("audience")
    @classmethod
    def _validate_audience(cls, value: str) -> str:
        candidate = value.strip()
        if (
            not candidate
            or len(candidate) > _MAX_ACCESS_AUDIENCE_LENGTH
            or not candidate.isascii()
            or any(not (character.isalnum() or character in "_-") for character in candidate)
        ):
            msg = "Cloudflare Access audience is invalid"
            raise ValueError(msg)
        return candidate

    @field_validator("allowed_email")
    @classmethod
    def _validate_allowed_email(cls, value: str) -> str:
        candidate = value.strip()
        local, separator, domain = candidate.rpartition("@")
        if (
            not separator
            or not local
            or not domain
            or len(candidate) > _MAX_ACCESS_EMAIL_LENGTH
            or any(character.isspace() or ord(character) < 32 for character in candidate)
        ):
            msg = "Cloudflare Access allowed email is invalid"
            raise ValueError(msg)
        return candidate


class ServerSettings(StrictSettings):
    host: str = "127.0.0.1"
    port: Port = 8765
    public_url: str | None = None
    cloudflare_access: CloudflareAccessSettings | None = None

    @model_validator(mode="after")
    def _require_public_access_pair(self) -> Self:
        if (self.public_url is None) != (self.cloudflare_access is None):
            msg = "server.public_url and server.cloudflare_access must be configured together"
            raise ValueError(msg)
        return self
```

- [ ] **Step 4: 設定例と README を新契約へ更新する**

`config/moco.example.yaml` の `server` に次を追加する。

```yaml
  # Required together with public_url. Values are non-secret Access identifiers.
  cloudflare_access: null
  # cloudflare_access:
  #   team_domain: https://owner.cloudflareaccess.com
  #   audience: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
  #   allowed_email: owner@example.com
```

`README.md` のスマートフォン節は次を明記する。

- `server.public_url` と三つの `cloudflare_access` field は同時に設定する。
- QR は bare public URL を表示する便宜機能で、認証情報を含まない。
- 公開 origin は capability を保存せず、Cloudflare Access session が有効なら URL を直接開ける。
- session duration 終了、logout、cookie 削除、policy 変更後は再 login が必要。
- loopback の `moco open` と operator capability rotate は引き続き有効。
- `cloudflared` の ingress でも Access JWT validation を必須にする。

- [ ] **Step 5: 設定境界を GREEN にする**

Run:

```bash
uv run pytest tests/test_config.py tests/test_web.py tests/test_cli.py tests/test_doctor.py -k 'public_url or cloudflare_access or pairing or runtime_writes' -v
```

Expected: PASS。

- [ ] **Step 6: 設定変更だけを commit する**

```bash
git add src/moco/config.py tests/test_config.py tests/test_web.py tests/test_cli.py tests/test_doctor.py config/moco.example.yaml README.md
git commit -m "feat: require Cloudflare Access settings"
```

### Task 2: Bounded Cloudflare Access JWT verifier

**Files:**
- Create: `src/moco/web/access.py`
- Create: `tests/test_cloudflare_access.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: verifier の RED test と署名 fixture を追加する**

`tests/test_cloudflare_access.py` に process 環境変数や network を使わない fixture を作る。
RSA key は test process 内で生成し、JWT は現在時刻を基準に一時間だけ有効にする。

```python
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import cast

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from moco.config import CloudflareAccessSettings
from moco.web.access import CloudflareAccessVerifier

SETTINGS = CloudflareAccessSettings(
    team_domain="https://owner.cloudflareaccess.com",
    audience="A" * 64,
    allowed_email="owner@example.com",
)


class FakeJwksFetcher:
    def __init__(self, documents: list[Mapping[str, object] | Exception]) -> None:
        self.documents = documents
        self.calls = 0

    async def __call__(self, url: str) -> Mapping[str, object]:
        assert url == "https://owner.cloudflareaccess.com/cdn-cgi/access/certs"
        response = self.documents[min(self.calls, len(self.documents) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def signed_access_token() -> tuple[str, Mapping[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    public_jwk = cast(
        "dict[str, object]",
        jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True),
    )
    public_jwk.update({"kid": "current-key", "alg": "RS256", "use": "sig"})
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": SETTINGS.team_domain,
            "aud": [SETTINGS.audience],
            "email": "OWNER@example.com",
            "iat": now,
            "exp": now + 3_600,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "current-key"},
    )
    return token, {"keys": [public_jwk]}


@pytest.mark.asyncio
async def test_accepts_valid_access_identity(
    signed_access_token: tuple[str, Mapping[str, object]],
) -> None:
    token, jwks = signed_access_token
    fetcher = FakeJwksFetcher([jwks])
    verifier = CloudflareAccessVerifier(SETTINGS, fetch_jwks=fetcher)

    assert await verifier.rejection_code([token]) is None
    assert fetcher.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assertions", "expected"),
    [
        ([], "access_token_missing"),
        (["", "duplicate"], "access_token_invalid"),
        (["not-a-jwt"], "access_token_invalid"),
    ],
)
async def test_rejects_missing_duplicate_or_malformed_assertion(
    assertions: list[str],
    expected: str,
) -> None:
    verifier = CloudflareAccessVerifier(
        SETTINGS,
        fetch_jwks=FakeJwksFetcher([{"keys": []}]),
    )

    assert await verifier.rejection_code(assertions) == expected
```

同じ file に payload / signer helper を追加し、別 issuer、別 audience、expired、future `iat`、
future `nbf`、email 不一致、HS256、未知 `kid`、oversized token、malformed JWKS、fetch failure、
cache hit、未知 `kid` の一回 refresh を個別に assert する。exception message と `caplog` に
token / email が含まれないことも assert する。

- [ ] **Step 2: verifier test が未実装で RED になることを確認する**

Run:

```bash
uv run pytest tests/test_cloudflare_access.py -v
```

Expected: `moco.web.access` が存在しないため collection FAIL。

- [ ] **Step 3: PyJWT dependency を追加する**

`pyproject.toml` の runtime dependencies に次を追加する。

```toml
"PyJWT[crypto]>=2.10,<3",
```

Run:

```bash
uv lock
uv sync
```

Expected: `PyJWT` と署名検証に必要な `cryptography` が `uv.lock` に解決される。

- [ ] **Step 4: focused verifier を実装する**

`src/moco/web/access.py` に次の公開境界を実装する。

```python
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

import httpx
import jwt

from moco.config import CloudflareAccessSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

AccessRejectionCode = Literal[
    "access_token_missing",
    "access_token_invalid",
    "access_identity_mismatch",
    "access_keys_unavailable",
]

_ALGORITHM = "RS256"
_MAX_ASSERTION_BYTES = 16_384
_MAX_JWKS_BYTES = 65_536
_JWKS_TTL_SECONDS = 3_600.0
_JWKS_TIMEOUT_SECONDS = 5.0


class JwksFetcher(Protocol):
    async def __call__(self, url: str) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class _KeyCache:
    keys: Mapping[str, object]
    expires_at: float


class _KeysUnavailable(RuntimeError):
    pass


class CloudflareAccessVerifier:
    def __init__(
        self,
        settings: CloudflareAccessSettings,
        *,
        fetch_jwks: JwksFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._certs_url = f"{settings.team_domain}/cdn-cgi/access/certs"
        self._fetch_jwks = fetch_jwks or _fetch_cloudflare_jwks
        self._monotonic = monotonic
        self._cache: _KeyCache | None = None
        self._refresh_lock = asyncio.Lock()

    async def rejection_code(self, assertions: Sequence[str]) -> AccessRejectionCode | None:
        if not assertions:
            return "access_token_missing"
        if len(assertions) != 1:
            return "access_token_invalid"
        token = assertions[0]
        if not token or len(token.encode("utf-8")) > _MAX_ASSERTION_BYTES:
            return "access_token_invalid"
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != _ALGORITHM or not isinstance(kid, str) or not kid:
                return "access_token_invalid"
            key = await self._resolve_key(kid)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[_ALGORITHM],
                audience=self._settings.audience,
                issuer=self._settings.team_domain,
                options={"require": ["exp", "iss", "aud", "email"]},
            )
        except _KeysUnavailable:
            return "access_keys_unavailable"
        except jwt.PyJWTError:
            return "access_token_invalid"
        email = claims.get("email")
        if not isinstance(email, str) or email.casefold() != self._settings.allowed_email.casefold():
            return "access_identity_mismatch"
        return None
```

`_resolve_key()` は fresh cache を先に参照し、missing `kid` のとき一度だけ lock 内で
`force=True` refresh する。JWKS は top-level `keys` list、各 item の `kid` / `kty=RSA` /
`alg=RS256` / `use=sig` を strict に検証し、`jwt.PyJWK.from_dict(item).key` だけを cache する。
fetch / parse / key conversion の例外は secret detail を連結せず `_KeysUnavailable` に変換する。

`_fetch_cloudflare_jwks()` は `httpx.AsyncClient(follow_redirects=False)` と
`client.stream("GET", ..., timeout=5.0)` を使い、status 200、JSON content type、合計
65,536 bytes 以下だけを受理する。body は `json.loads(bytes(body))` で object へ変換し、
response text や URL を error message に含めない。

- [ ] **Step 5: verifier test を GREEN にする**

Run:

```bash
uv run pytest tests/test_cloudflare_access.py -v
```

Expected: 全 case PASS。

- [ ] **Step 6: verifier と dependency だけを commit する**

```bash
git add pyproject.toml uv.lock src/moco/web/access.py tests/test_cloudflare_access.py
git commit -m "feat: verify Cloudflare Access assertions"
```

### Task 3: Public Access and loopback capability routing

**Files:**
- Modify: `src/moco/web/app.py`
- Modify: `tests/test_web.py`

- [ ] **Step 1: HTTP / WebSocket 分岐の RED test を追加する**

`tests/test_web.py` に次の fake と public settings helper を追加する。

```python
class FakeAccessVerifier:
    def __init__(self, rejection: str | None = None) -> None:
        self.rejection = rejection
        self.assertions: list[list[str]] = []

    async def rejection_code(self, assertions: list[str]) -> str | None:
        self.assertions.append(assertions)
        return self.rejection


def public_settings() -> MocoSettings:
    return MocoSettings.model_validate(
        {
            "server": {
                "public_url": "https://voice.example.com",
                "cloudflare_access": {
                    "team_domain": "https://owner.cloudflareaccess.com",
                    "audience": "A" * 64,
                    "allowed_email": "owner@example.com",
                },
            }
        }
    )
```

次を test する。

```python
def test_public_http_and_websocket_accept_access_without_capability() -> None:
    app = create_app(public_settings(), capability_token=CAPABILITY)
    verifier = FakeAccessVerifier()
    app.state.access_verifier = verifier
    public_headers = {
        "host": "voice.example.com",
        "origin": "https://voice.example.com",
        "cf-access-jwt-assertion": "signed-assertion",
    }

    with TestClient(app, base_url="https://voice.example.com") as client:
        assert client.get("/", headers=public_headers).status_code == 200
        assert client.get("/auth/status", headers=public_headers).status_code == 204
        with client.websocket_connect(
            "/ws",
            headers=public_headers,
            subprotocols=["moco"],
        ) as socket:
            assert socket.receive_json()["state"] == "ready"

    assert verifier.assertions == [
        ["signed-assertion"],
        ["signed-assertion"],
        ["signed-assertion"],
    ]


def test_public_access_does_not_fall_back_to_capability() -> None:
    app = create_app(public_settings(), capability_token=CAPABILITY)
    app.state.access_verifier = FakeAccessVerifier("access_token_missing")

    with (
        TestClient(app, base_url="https://voice.example.com") as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws",
            headers={"host": "voice.example.com", "origin": "https://voice.example.com"},
            subprotocols=["moco", f"moco.capability.{CAPABILITY}"],
        ),
    ):
        pass


def test_loopback_does_not_accept_access_instead_of_capability() -> None:
    app = create_app(public_settings(), capability_token=CAPABILITY)
    app.state.access_verifier = FakeAccessVerifier()

    with (
        TestClient(app, base_url="http://127.0.0.1:8765") as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws",
            headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
                "cf-access-jwt-assertion": "signed-assertion",
            },
            subprotocols=["moco"],
        ),
    ):
        pass
```

HTTP では missing / invalid Access が 403 + `Cache-Control: no-store`、unknown Host は既存挙動、
WebSocket log は stable Access code だけで token / email / capability を含まないことも test する。

- [ ] **Step 2: routing test が RED になることを確認する**

Run:

```bash
uv run pytest tests/test_web.py -k 'public_http or public_access or loopback_does_not_accept or operator_websocket_rejection' -v
```

Expected: 公開 WebSocket が `capability_missing`、`/auth/status` が 404、公開 HTTP middleware がないため FAIL。

- [ ] **Step 3: Host 分類と公開 HTTP middleware を実装する**

`src/moco/web/app.py` で `CloudflareAccessVerifier` と `AccessRejectionCode` を import し、
`create_app()` 内で public settings がある場合だけ verifier を作る。

```python
access_settings = resolved.server.cloudflare_access
app.state.access_verifier = (
    CloudflareAccessVerifier(access_settings) if access_settings is not None else None
)


@app.middleware("http")
async def require_public_operator_access(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if _is_public_host(request.headers.get("host"), resolved.server.public_url):
        rejection = await _public_access_rejection(
            request.headers.getlist("cf-access-jwt-assertion"),
            app.state.access_verifier,
        )
        if rejection is not None:
            _log_operator_auth_rejection(rejection, boundary="operator_http")
            return JSONResponse(
                {"code": "access_auth_failed"},
                status_code=403,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
    return await call_next(request)


@app.get("/auth/status", include_in_schema=False)
async def operator_auth_status() -> Response:
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
```

`_is_public_host()` は `urlsplit(f"//{host}")` を安全に parse し、設定 public URL の netloc と
case-insensitive 完全一致するときだけ true を返す。`_public_access_rejection()` は verifier が
ない状態を `access_keys_unavailable` として fail closed にする。

- [ ] **Step 4: WebSocket 認証を public / loopback へ分岐する**

`operator_socket()` を次の順序にする。

```python
origin_allowed = _origin_allowed(websocket, resolved.server.public_url)
public_origin = _is_public_operator_socket(websocket, resolved.server.public_url)
if not origin_allowed:
    rejection_code = "origin_rejected"
elif public_origin:
    rejection_code = await _public_access_rejection(
        websocket.headers.getlist("cf-access-jwt-assertion"),
        app.state.access_verifier,
    )
else:
    rejection_code = _capability_rejection_code(
        websocket,
        app.state.capability_token,
    )
if rejection_code is not None:
    await _reject_operator_socket(websocket, rejection_code)
    return
```

`_is_public_operator_socket()` は Origin と Host の両方が configured public URL と完全一致する
場合だけ true にする。`_reject_operator_socket()` の Literal を Access code まで拡張し、
`safe_event` へ bounded code だけを渡す。公開側で正しい capability が送られても JWT failure を
上書きしない。loopback 側で正しい JWT があっても capability failure を上書きしない。

- [ ] **Step 5: HTTP / WebSocket routing を GREEN にする**

Run:

```bash
uv run pytest tests/test_web.py -k 'origin or capability or access or pairing' -v
```

Expected: PASS。

- [ ] **Step 6: web authorization routing を commit する**

```bash
git add src/moco/web/app.py tests/test_web.py
git commit -m "feat: authorize public operators with Access"
```

### Task 4: Remove capability from mobile URL and QR

**Files:**
- Modify: `src/moco/web/pairing.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/cli.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: bare mobile URL の RED test を書く**

既存 test を次の期待へ変更する。

```python
def test_mobile_operator_url_contains_no_capability() -> None:
    assert mobile_operator_url("https://voice.example.com") == "https://voice.example.com"


async def test_runtime_mobile_url_is_bare_public_url(...) -> None:
    # 既存 runtime state fixture と cleanup assertion は維持する。
    assert mobile_url == "https://voice.example.com"
    assert "#" not in mobile_url
    assert capability_value not in mobile_url


def test_pairing_svg_does_not_embed_operator_capability() -> None:
    svg = render_pairing_svg("https://voice.example.com")

    assert CAPABILITY.encode() not in svg
```

`_is_safe_mobile_url()` は fragment なしを受理し、fragment / query / credentials / port / path は
拒否する test へ変える。

- [ ] **Step 2: mobile URL test が RED になることを確認する**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_web.py -k 'mobile_url or pairing' -v
```

Expected: 現行 URL と QR が capability fragment を含むため FAIL。

- [ ] **Step 3: URL / QR owner を最小変更する**

`src/moco/web/pairing.py` を次の契約へ変更する。

```python
def mobile_operator_url(public_url: str) -> str:
    return public_url


def render_pairing_svg(public_url: str) -> bytes:
    stream = BytesIO()
    qr = segno.make(
        mobile_operator_url(public_url),
        error="m",
        micro=False,
        boost_error=False,
    )
    qr.save(stream, kind="svg", scale=6, xmldecl=False, nl=False)
    return stream.getvalue()
```

`src/moco/cli.py` は `_runtime_state_payload()` で
`mobile_operator_url(settings.server.public_url)` を使う。`_is_safe_mobile_url()` は
`not parsed.fragment` を要求する。local `url` は引き続き capability fragment 必須とする。

`src/moco/web/app.py` の pairing endpoint は認可用 `X-Moco-Capability` を引き続き要求するが、
SVG generator へ capability を渡さない。

- [ ] **Step 4: mobile URL / QR test を GREEN にする**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_web.py -k 'mobile_url or pairing or runtime_writes' -v
```

Expected: PASS。runtime state、SVG、CLI output に public capability がない。

- [ ] **Step 5: mobile bootstrap simplification を commit する**

```bash
git add src/moco/web/pairing.py src/moco/web/app.py src/moco/cli.py tests/test_web.py tests/test_cli.py
git commit -m "refactor: remove capability from mobile URLs"
```

### Task 5: Public browser credential cleanup and auth probe

**Files:**
- Modify: `src/moco/web/static/app.js`
- Modify: `tests/js/app.test.js`

- [ ] **Step 1: public / loopback browser behavior の RED test を書く**

`tests/js/app.test.js` の capability fixture に `hostname` と storage `removeItem` を追加し、
次を assert する。

```javascript
it("removes legacy capability state on a public origin", () => {
  const values = new Map([["moco.capability", "D".repeat(43)]]);
  const removed = [];
  const persistentStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => {
      removed.push(key);
      values.delete(key);
    },
  };
  const sessionStorage = {
    getItem: () => "E".repeat(43),
    removeItem: (key) => removed.push(`session:${key}`),
  };

  assert.equal(
    appModule.loadCapability({
      history: { replaceState: () => {} },
      location: { hash: "", hostname: "moco.toarupen.org", pathname: "/" },
      persistentStorage,
      sessionStorage,
    }),
    "",
  );
  assert.deepEqual(removed, ["moco.capability", "session:moco.capability"]);
});


it("builds a capability protocol only for loopback", () => {
  assert.deepEqual(appModule.operatorSocketProtocols("A".repeat(43)), [
    "moco",
    `moco.capability.${"A".repeat(43)}`,
  ]);
  assert.deepEqual(appModule.operatorSocketProtocols(""), ["moco"]);
});


it("requires a no-store auth status before connecting", async () => {
  const calls = [];
  await appModule.probeOperatorAccess(async (url, options) => {
    calls.push([url, options]);
    return { status: 204 };
  });
  assert.equal(calls[0][0], "/auth/status");
  assert.equal(calls[0][1].cache, "no-store");
  assert.equal(calls[0][1].credentials, "same-origin");
  assert.equal(calls[0][1].redirect, "manual");
});
```

status が 204 以外、fetch rejection、redirect response の場合は named error
`access_auth_failed` になることも test する。

- [ ] **Step 2: JavaScript test が RED になることを確認する**

Run:

```bash
npm test -- --test-name-pattern='capability|auth status|operator socket protocol'
```

Expected: 公開 storage cleanup、protocol builder、auth probe が未実装のため FAIL。

- [ ] **Step 3: origin-aware capability と protocol builder を実装する**

`loadCapability()` の先頭に公開 origin 分岐を追加する。

```javascript
export function loadCapability({ location, history, persistentStorage, sessionStorage }) {
  if (!isLoopbackHostname(location.hostname)) {
    if (location.hash) {
      history.replaceState(null, "", location.pathname);
    }
    persistentStorage.removeItem(CAPABILITY_STORAGE_KEY);
    sessionStorage.removeItem(CAPABILITY_STORAGE_KEY);
    return "";
  }
  // 既存の strict fragment -> localStorage -> legacy sessionStorage 順を維持する。
}


export function operatorSocketProtocols(capability) {
  return capability
    ? [WEBSOCKET_PROTOCOL, `${CAPABILITY_PREFIX}${capability}`]
    : [WEBSOCKET_PROTOCOL];
}
```

`new WebSocket(url, ...)` は `operatorSocketProtocols(capability)` を使う。loopback capability が
空なら server が従来どおり fail closed に拒否する。

- [ ] **Step 4: 接続前 auth probe と表示 code を実装する**

```javascript
export async function probeOperatorAccess(fetch) {
  let response;
  try {
    response = await fetch("/auth/status", {
      cache: "no-store",
      credentials: "same-origin",
      redirect: "manual",
    });
  } catch {
    throw namedError("access_auth_failed");
  }
  if (response.status !== 204) {
    throw namedError("access_auth_failed");
  }
}
```

`connectConversation()` の `connectSocket()` より前に
`await probeOperatorAccess(window.fetch.bind(window))` を置く。AudioContext activation と
microphone permission は既存どおり先に完了するため、iOS user activation 順序を変えない。

`ERROR_MESSAGES` に次を追加する。

```javascript
access_auth_failed: "Cloudflare Access の認証を確認できません。ページを再読み込みしてください",
```

- [ ] **Step 5: JavaScript test を GREEN にする**

Run:

```bash
npm test
```

Expected: 全 frontend test PASS。

- [ ] **Step 6: browser boundary を commit する**

```bash
git add src/moco/web/static/app.js tests/js/app.test.js
git commit -m "feat: use Access auth on public operator pages"
```

### Task 6: Repository verification and security regression

**Files:**
- Modify only if a failing gate identifies an in-scope defect in files already listed above.

- [ ] **Step 1: newly introduced identifiers の ownership を確認する**

Run:

```bash
rg -n "CloudflareAccessSettings|CloudflareAccessVerifier|access_auth_failed|operatorSocketProtocols|mobile_operator_url" src tests config README.md
```

Expected: JWT logic は `web/access.py`、routing は `web/app.py`、browser protocol は
`static/app.js` にだけ存在し、重複実装がない。

- [ ] **Step 2: targeted Python / frontend suites を実行する**

Run:

```bash
uv run pytest tests/test_cloudflare_access.py tests/test_config.py tests/test_cli.py tests/test_web.py -v
npm test
```

Expected: PASS。

- [ ] **Step 3: repository 全 gate を実行する**

Run:

```bash
just check
```

Expected: format、lint、mypy、Python tests、frontend tests、E2E、coverage、secret scan を含む全 gate PASS。coverage は repository 下限 90% 以上。

- [ ] **Step 4: diff と secret 非漏出を確認する**

Run:

```bash
git diff --check
npx secretlint .
git status --short
```

Expected: whitespace error と secret finding がなく、既存の無関係な作業差分がこの feature commit に混入していない。

- [ ] **Step 5: gate 修正があった場合だけ path-scoped commit を作る**

```bash
git add src/moco/config.py src/moco/web/access.py src/moco/web/app.py src/moco/web/pairing.py src/moco/web/static/app.js src/moco/cli.py tests/test_cloudflare_access.py tests/test_config.py tests/test_web.py tests/test_cli.py tests/test_doctor.py tests/js/app.test.js config/moco.example.yaml README.md pyproject.toml uv.lock
git commit -m "fix: harden Access operator authentication"
```

Expected: gate 修正がなければこの commit step は実行しない。

### Task 6.5: Ongoing Access authorization lease security fix

**Files:**
- Modify: `src/moco/config.py`
- Modify: `src/moco/web/access.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/web/static/app.js`
- Test: `tests/test_config.py`
- Test: `tests/test_cloudflare_access.py`
- Test: `tests/test_web.py`
- Test: `tests/js/app.test.js`
- Modify: `docs/superpowers/specs/2026-08-28-cloudflare-access-operator-auth-design.md`
- Modify: `docs/superpowers/plans/2026-08-28-cloudflare-access-operator-auth.md`

- [ ] **Step 1: ASCII identity と verified expiry の RED test を追加する**

`allowed_email` と JWT `email` の non-ASCII 値を拒否し、ASCII 大文字小文字だけを同一視する。
verifier は認証成功時に正規化済み ASCII identity と verified JWT `exp` を返す。test は fake JWKS
と inline settings を使い、process 環境変数へ固定値を入れない。

- [ ] **Step 2: server-enforced lease の RED test を追加する**

公開 WebSocket の単一オペレーター登録成功後だけ opaque 256-bit token を発行する。初回・refresh
deadline は `min(monotonic now + 60 seconds, JWT exp remaining)`。refresh、expiry、close/release を
同じ lock/state で直列化し、期限切れ後の復活を拒否する。refresh 停止で server が WebSocket を
close して operator を unregister し、各 privileged message 処理直前にも deadline を確認する。
loopback は lease authority を一切呼ばない。

- [ ] **Step 3: `/auth/lease` trust boundary の RED test を追加する**

設定済み public Host、exact same Origin、fresh Access JWT、同一 ASCII identity、bounded single
`X-Moco-Access-Lease` header をすべて要求する。duplicate / malformed header、別 Origin、loopback、
別 identity、expired token は generic no-store 403。成功は body-free no-store 204。token、JWT、email
は URL、body、response、log、telemetry に出さず token 比較には `secrets.compare_digest` を使う。

- [ ] **Step 4: browser refresh の RED test を追加する**

公開 browser は WebSocket から受けた lease token を memory 内だけに保持し、約 20 秒ごとに
same-origin credentials、manual redirect、no-store の body-free POST header で更新する。成功中は
microphone / WebRTC / audio を変更しない。403、redirect、network error では timer を止め、現在の
socket を閉じて `access_auth_failed` を表示する。socket close 時は timer と token を即時破棄する。

- [ ] **Step 5: GREEN implementation と全 gate を確認する**

Run:

```bash
uv run pytest tests/test_config.py tests/test_cloudflare_access.py tests/test_web.py -q
npm run test:frontend
just check
git diff --check
```

Expected: Access/JWT/lease/browser の RED が GREEN、既存 loopback・単一オペレーター・media lifecycle
回帰を含む全 gate PASS。Safari が background で timer を停止した場合は、安全側に約 60 秒で公開
WebSocket が切断される契約を design と運用確認へ反映する。

### Task 7: Owner-only Cloudflare rollout and physical iPhone verification

**Files:**
- Modify privately: `/Users/sankenbisha/Library/Application Support/moco/moco.yaml`
- Modify privately: `/Users/sankenbisha/.cloudflared/moco.yml`
- Verify: `/Users/sankenbisha/Library/LaunchAgents/dev.toarupen.moco.plist`
- Verify: `/Users/sankenbisha/Library/LaunchAgents/dev.toarupen.moco-cloudflared.plist`
- Observe: `/Users/sankenbisha/Library/Logs/moco/stderr.log`
- Observe: `/Users/sankenbisha/Library/Logs/moco/cloudflared.stderr.log`

- [ ] **Step 1: Cloudflare Access application の identity values を確認する**

認証済み Cloudflare Zero Trust dashboard で `moco.toarupen.org` の self-hosted application を開き、
次を確認する。

- application domain は `moco.toarupen.org` と完全一致する。
- allow policy は owner email 一件だけで、Bypass policy がない。
- application audience tag を取得する。
- team domain は現在の Zero Trust organization の `https://` origin で、host suffix は
  `.cloudflareaccess.com` である。
- session duration は一か月である。

値は private moco / cloudflared config へだけ転記し、repository、plan、通常 log、chat に
貼り付けない。Access JWT 自体は取得・保存しない。

- [ ] **Step 2: private moco config を更新して strict parse を確認する**

`/Users/sankenbisha/Library/Application Support/moco/moco.yaml` の既存 `server` mapping へ
dashboard で確認した三値を追加する。

```yaml
server:
  host: 127.0.0.1
  port: 8765
  public_url: https://moco.toarupen.org
  cloudflare_access:
    team_domain: https://owner.cloudflareaccess.com
    audience: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
    allowed_email: owner@example.com
```

上の三値は構造例であり、そのまま保存しない。`team_domain`、`audience`、`allowed_email` を
Step 1 で dashboard から確認した実値へ置き換える。file mode `0600` と parent directory mode
`0700` を維持する。

Run:

```bash
uv run moco doctor
```

Expected: configuration error がなく、`operator_public_url`、cloudflared、Codex、Irodori の既存 check が bounded detail で完了する。team domain、audience、email は出力されない。

- [ ] **Step 3: cloudflared origin validation を必須化する**

`/Users/sankenbisha/.cloudflared/moco.yml` の `moco.toarupen.org` ingress entry に、同じ
team name と audience tag を追加する。

```yaml
    originRequest:
      access:
        required: true
        teamName: owner
        audTag:
          - AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
```

`owner` と 64 文字の `A` は構造例であり、そのまま保存しない。team domain の先頭 label と
Step 1 の application audience tag へ置き換える。末尾の `service: http_status:404` catch-all と
tunnel credentials path は変更しない。

Run:

```bash
/opt/homebrew/bin/cloudflared tunnel --config /Users/sankenbisha/.cloudflared/moco.yml ingress validate
```

Expected: configuration valid、exit 0。

- [ ] **Step 4: service を順番に再起動して health を確認する**

Run:

```bash
launchctl kickstart -k gui/501/dev.toarupen.moco-cloudflared
launchctl kickstart -k gui/501/dev.toarupen.moco
uv run moco doctor
```

Expected: 両 LaunchAgent が running、loopback operator HTTP が応答し、doctor の全 required check が OK。

- [ ] **Step 5: Mac で loopback capability を回帰確認する**

Run:

```bash
uv run moco open
```

Expected: Mac browser は capability 付き loopback owner URL を開き、operator WebSocket へ接続できる。「スマホ接続」QR は表示できるが、QR payload は bare
`https://moco.toarupen.org` で capability を含まない。

- [ ] **Step 6: 物理 iPhone で固定 URL を直接確認する**

iPhone Mirroring は microphone を Mac から利用できないため使わず、物理 iPhone の Safari で
次を行う。

1. address bar へ `https://moco.toarupen.org` を直接入力する。
2. Cloudflare login が出た場合は owner identity で完了する。
3. 「接続」を押し、Safari の microphone permission を許可する。
4. 「入力開始」で発話し、transcript、Codex response、Irodori playback を確認する。
5. page reload、新規 tab、Safari 再起動後も QR なしで再接続する。

Expected: `capability_missing` / `websocket_failed` が出ず、operator 接続と双方向音声が成功する。

- [ ] **Step 7: bounded server evidence を確認する**

Run:

```bash
tail -n 200 /Users/sankenbisha/Library/Logs/moco/stderr.log | rg "operator_connected|operator_websocket_rejected|capability_missing|access_|conversation_started"
tail -n 100 /Users/sankenbisha/Library/Logs/moco/cloudflared.stderr.log
```

Expected: 新しい iPhone 接続に `operator_connected` と会話開始 evidence があり、新しい
`capability_missing`、Access rejection、JWT / email / capability の漏出がない。

- [ ] **Step 8: Access logout の fail-closed を確認して再 login する**

iPhone Safari で `https://moco.toarupen.org/cdn-cgi/access/logout` を一度開き、固定 URL への
再訪が Cloudflare login を要求し、既存の公開 WebSocket も lease refresh を継続できず約 60 秒以内に
moco server から切断され、未認証 WebSocket が moco へ到達しないことを確認する。
その後 owner identity で再 login し、接続を復元する。

Expected: logout 中は拒否、再 login 後は QR なしで成功。session duration は一か月のまま。

- [ ] **Step 9: 最終状態を記録する**

Run:

```bash
git log -6 --oneline
git status --short
just doctor
```

Expected: feature commits、既存の意図した未コミット差分、running service、全 doctor check が
区別して確認できる。private Cloudflare / moco config は git status に現れない。
