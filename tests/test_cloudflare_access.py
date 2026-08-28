from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from moco.config import CloudflareAccessSettings
from moco.web.access import AccessAuthorization, CloudflareAccessVerifier

TEAM_DOMAIN = "https://example-team.cloudflareaccess.com"
AUDIENCE = "audience_123-ABC"
ALLOWED_EMAIL = "Owner@Example.COM"
KID = "test-key"


def _settings() -> CloudflareAccessSettings:
    return CloudflareAccessSettings(
        team_domain=TEAM_DOMAIN,
        audience=AUDIENCE,
        allowed_email=ALLOWED_EMAIL,
    )


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_uint(value: int) -> str:
    return _base64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwk(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = KID,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, object]:
    numbers = private_key.public_key().public_numbers()
    result: dict[str, object] = {
        "kid": kid,
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": _base64url_uint(numbers.n),
        "e": _base64url_uint(numbers.e),
    }
    if overrides is not None:
        result.update(overrides)
    return result


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    result: dict[str, object] = {
        "iss": TEAM_DOMAIN,
        "aud": AUDIENCE,
        "email": "owner@example.com",
        "iat": now - 1,
        "exp": now + 300,
    }
    result.update(overrides)
    return result


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    claims: Mapping[str, object] | None = None,
    header: Mapping[str, object] | None = None,
    header_json: bytes | None = None,
) -> str:
    encoded_header = _base64url(
        header_json
        if header_json is not None
        else json.dumps(header or {"alg": "RS256", "kid": KID}, separators=(",", ":")).encode()
    )
    encoded_claims = _base64url(json.dumps(claims or _claims(), separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode('ascii')}.{_base64url(signature)}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeFetcher:
    def __init__(self, *results: Mapping[str, object] | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    async def __call__(self) -> Mapping[str, object]:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


class YieldingFetcher(FakeFetcher):
    async def __call__(self) -> Mapping[str, object]:
        await asyncio.sleep(0)
        return await super().__call__()


@pytest.fixture
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize("email", ["owner@example.com", "OWNER@EXAMPLE.COM"])
async def test_accepts_valid_rs256_assertion_and_casefolds_email(
    private_key: rsa.RSAPrivateKey,
    email: str,
) -> None:
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([_token(private_key, claims=_claims(email=email))]) is None
    assert fetcher.calls == 1


async def test_returns_verified_ascii_identity_and_expiry(
    private_key: rsa.RSAPrivateKey,
) -> None:
    expires_at = int(time.time()) + 120
    token = _token(private_key, claims=_claims(email="OWNER@EXAMPLE.COM", exp=expires_at))
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    rejection, authorization = await verifier.authorization([token])

    assert rejection is None
    assert authorization == AccessAuthorization(
        identity="owner@example.com",
        expires_at=expires_at,
    )


@pytest.mark.parametrize("email", ["straße@example.com", "STRAßE@example.com"])
async def test_rejects_non_ascii_jwt_email_without_casefold_collision(
    private_key: rsa.RSAPrivateKey,
    email: str,
) -> None:
    settings = CloudflareAccessSettings(
        team_domain=TEAM_DOMAIN,
        audience=AUDIENCE,
        allowed_email="strasse@example.com",
    )
    verifier = CloudflareAccessVerifier(
        settings,
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert (
        await verifier.rejection_code([_token(private_key, claims=_claims(email=email))])
        == "access_token_invalid"
    )


@pytest.mark.parametrize(
    "email",
    [
        "owner.example.com",
        "@example.com",
        "owner@",
        "owner@@example.com",
        "owner\n@example.com",
        "Owner <owner@example.com>",
    ],
)
async def test_rejects_ascii_jwt_email_that_is_not_mailbox_text(
    private_key: rsa.RSAPrivateKey,
    email: str,
) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert (
        await verifier.rejection_code([_token(private_key, claims=_claims(email=email))])
        == "access_token_invalid"
    )


@pytest.mark.parametrize(
    ("assertions", "expected"),
    [
        ([], "access_token_missing"),
        (["", ""], "access_token_invalid"),
        (["not-a-jwt"], "access_token_invalid"),
        (["x" * 16_385], "access_token_invalid"),
        (cast("Sequence[str]", [b"not-text"]), "access_token_invalid"),
    ],
)
async def test_rejects_missing_or_malformed_assertions_without_fetching_keys(
    assertions: Sequence[str],
    expected: str,
) -> None:
    fetcher = FakeFetcher({"keys": []})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code(assertions) == expected
    assert fetcher.calls == 0


async def test_rejects_duplicate_assertions_without_fetching_keys(
    private_key: rsa.RSAPrivateKey,
) -> None:
    token = _token(private_key)
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([token, token]) == "access_token_invalid"
    assert fetcher.calls == 0


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "HS256", "kid": KID},
        {"alg": "none", "kid": KID},
        {"alg": "RS256"},
        {"alg": "RS256", "kid": ""},
        {"alg": "RS256", "kid": 7},
        {"alg": "RS256", "kid": "k" * 257},
    ],
)
async def test_rejects_unapproved_algorithm_or_invalid_kid_before_fetch(
    private_key: rsa.RSAPrivateKey,
    header: Mapping[str, object],
) -> None:
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert (
        await verifier.rejection_code([_token(private_key, header=header)])
        == "access_token_invalid"
    )
    assert fetcher.calls == 0


async def test_rejects_duplicate_kid_header_before_fetch(private_key: rsa.RSAPrivateKey) -> None:
    token = _token(private_key, header_json=b'{"alg":"RS256","kid":"first","kid":"second"}')
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([token]) == "access_token_invalid"
    assert fetcher.calls == 0


@pytest.mark.parametrize(
    ("segment", "malformed_value"),
    [
        ("header_padding", None),
        ("payload_empty", ""),
        ("payload_padding", _base64url(b"{}") + "="),
        ("payload_invalid_character", "!"),
        ("payload_invalid_base64", "A"),
        ("payload_invalid_json", _base64url(b"not-json")),
        ("payload_non_object", _base64url(b"[]")),
        ("payload_duplicate_key", _base64url(b'{"exp":1,"exp":2}')),
        ("payload_nonfinite", _base64url(b'{"exp":Infinity}')),
        ("signature_empty", ""),
        ("signature_padding", "AA=="),
        ("signature_invalid_character", "!"),
        ("signature_noncanonical", "AB"),
        ("signature_invalid_base64", "A"),
    ],
)
async def test_rejects_malformed_compact_token_before_fetching_keys(
    private_key: rsa.RSAPrivateKey,
    segment: str,
    malformed_value: str | None,
) -> None:
    segments = _token(private_key).split(".")
    if segment == "header_padding":
        segments[0] += "="
    elif segment.startswith("payload_"):
        assert malformed_value is not None
        segments[1] = malformed_value
    else:
        assert malformed_value is not None
        segments[2] = malformed_value
    fetcher = FakeFetcher(RuntimeError("JWKS must not be fetched"))
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([".".join(segments)]) == "access_token_invalid"
    assert fetcher.calls == 0


@pytest.mark.parametrize(
    "claims",
    [
        _claims(iss="https://other.cloudflareaccess.com"),
        _claims(aud="another-audience"),
        _claims(aud=["another-audience", 1]),
        _claims(exp=int(time.time()) - 1),
        _claims(iat=int(time.time()) + 300),
        _claims(nbf=int(time.time()) + 300),
    ],
)
async def test_rejects_invalid_standard_claims(
    private_key: rsa.RSAPrivateKey,
    claims: Mapping[str, object],
) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert (
        await verifier.rejection_code([_token(private_key, claims=claims)])
        == "access_token_invalid"
    )


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("exp", True),
        ("exp", "9999999999"),
        ("exp", float("inf")),
        ("iat", True),
        ("iat", "0"),
        ("nbf", True),
        ("nbf", "0"),
        ("email", ""),
        ("email", 7),
        ("email", "a" * 255),
        ("iss", 7),
        ("aud", True),
    ],
)
async def test_rejects_invalid_claim_types_and_bounds(
    private_key: rsa.RSAPrivateKey,
    claim: str,
    value: object,
) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert (
        await verifier.rejection_code([_token(private_key, claims=_claims(**{claim: value}))])
        == "access_token_invalid"
    )


@pytest.mark.parametrize("missing", ["exp", "iss", "aud", "email"])
async def test_rejects_missing_required_claim(
    private_key: rsa.RSAPrivateKey,
    missing: str,
) -> None:
    claims = _claims()
    del claims[missing]
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert (
        await verifier.rejection_code([_token(private_key, claims=claims)])
        == "access_token_invalid"
    )


async def test_rejects_wrong_signature(private_key: rsa.RSAPrivateKey) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert await verifier.rejection_code([_token(other_key)]) == "access_token_invalid"


async def test_returns_identity_mismatch_without_disclosing_identity(
    private_key: rsa.RSAPrivateKey,
) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key)]}),
    )

    assert (
        await verifier.rejection_code(
            [_token(private_key, claims=_claims(email="intruder@example.invalid"))]
        )
        == "access_identity_mismatch"
    )


@pytest.mark.parametrize(
    "jwks",
    [
        cast("Mapping[str, object]", []),
        {},
        {"keys": "not-a-list"},
        {"keys": ["not-an-object"]},
        {"keys": []},
        {"keys": [{"kid": "", "kty": "RSA", "alg": "RS256", "use": "sig"}]},
        {"keys": [{"kid": "k" * 257, "kty": "RSA", "alg": "RS256", "use": "sig"}]},
    ],
)
async def test_malformed_jwks_is_keys_unavailable(
    private_key: rsa.RSAPrivateKey,
    jwks: Mapping[str, object],
) -> None:
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=FakeFetcher(jwks))

    assert await verifier.rejection_code([_token(private_key)]) == "access_keys_unavailable"


@pytest.mark.parametrize(
    "overrides",
    [
        {"kty": "EC"},
        {"alg": "RS512"},
        {"use": "enc"},
        {"n": ""},
        {"n": "x" * 8193},
        {"e": ""},
        {"e": "x" * 17},
    ],
)
async def test_rejects_non_signing_or_oversized_jwk_material(
    private_key: rsa.RSAPrivateKey,
    overrides: Mapping[str, object],
) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key, overrides=overrides)]}),
    )

    assert await verifier.rejection_code([_token(private_key)]) == "access_keys_unavailable"


async def test_duplicate_jwks_kid_is_keys_unavailable(private_key: rsa.RSAPrivateKey) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher({"keys": [_jwk(private_key), _jwk(private_key)]}),
    )

    assert await verifier.rejection_code([_token(private_key)]) == "access_keys_unavailable"


async def test_fresh_cached_key_avoids_fetch(private_key: rsa.RSAPrivateKey) -> None:
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher, monotonic=FakeClock())
    token = _token(private_key)

    assert await verifier.rejection_code([token]) is None
    assert await verifier.rejection_code([token]) is None
    assert fetcher.calls == 1


async def test_concurrent_cache_misses_share_one_fetch(private_key: rsa.RSAPrivateKey) -> None:
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]})
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)
    token = _token(private_key)

    assert await asyncio.gather(*(verifier.rejection_code([token]) for _ in range(8))) == [None] * 8
    assert fetcher.calls == 1


async def test_expired_cache_refreshes_keys(private_key: rsa.RSAPrivateKey) -> None:
    replacement_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    clock = FakeClock()
    fetcher = FakeFetcher(
        {"keys": [_jwk(private_key)]},
        {"keys": [_jwk(replacement_key)]},
    )
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher, monotonic=clock)

    assert await verifier.rejection_code([_token(private_key)]) is None
    clock.now = 3600.0
    assert await verifier.rejection_code([_token(replacement_key)]) is None
    assert fetcher.calls == 2


async def test_unknown_kid_forces_exactly_one_refresh(private_key: rsa.RSAPrivateKey) -> None:
    token = _token(private_key, header={"alg": "RS256", "kid": "unknown"})
    fetcher = FakeFetcher(
        {"keys": [_jwk(private_key)]},
        {"keys": [_jwk(private_key)]},
    )
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([_token(private_key)]) is None
    assert await verifier.rejection_code([token]) == "access_token_invalid"
    assert fetcher.calls == 2


async def test_concurrent_unknown_kid_checks_share_one_forced_refresh(
    private_key: rsa.RSAPrivateKey,
) -> None:
    unknown_token = _token(private_key, header={"alg": "RS256", "kid": "unknown"})
    fetcher = YieldingFetcher(
        {"keys": [_jwk(private_key)]},
        {"keys": [_jwk(private_key)]},
    )
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([_token(private_key)]) is None
    results = await asyncio.gather(*(verifier.rejection_code([unknown_token]) for _ in range(8)))

    assert results == ["access_token_invalid"] * 8
    assert fetcher.calls == 2


async def test_unknown_kid_forces_one_refresh_after_initial_fetch(
    private_key: rsa.RSAPrivateKey,
) -> None:
    token = _token(private_key, header={"alg": "RS256", "kid": "unknown"})
    fetcher = FakeFetcher(
        {"keys": [_jwk(private_key)]},
        {"keys": [_jwk(private_key)]},
    )
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher)

    assert await verifier.rejection_code([token]) == "access_token_invalid"
    assert fetcher.calls == 2


async def test_expired_cache_is_not_used_when_refresh_fails(private_key: rsa.RSAPrivateKey) -> None:
    clock = FakeClock()
    fetcher = FakeFetcher({"keys": [_jwk(private_key)]}, RuntimeError("network secret"))
    verifier = CloudflareAccessVerifier(_settings(), fetch_jwks=fetcher, monotonic=clock)
    token = _token(private_key)

    assert await verifier.rejection_code([token]) is None
    clock.now = 3600.0
    assert await verifier.rejection_code([token]) == "access_keys_unavailable"


async def test_fetch_failure_is_keys_unavailable(private_key: rsa.RSAPrivateKey) -> None:
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher(RuntimeError("sensitive endpoint detail")),
    )

    assert await verifier.rejection_code([_token(private_key)]) == "access_keys_unavailable"


def _install_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> dict[str, Any]:
    real_client = httpx.AsyncClient
    captured: dict[str, Any] = {}

    def client_factory(*, follow_redirects: bool, timeout: httpx.Timeout) -> httpx.AsyncClient:
        captured.update(follow_redirects=follow_redirects, timeout=timeout)
        return real_client(
            follow_redirects=follow_redirects,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("moco.web.access.httpx.AsyncClient", client_factory)
    return captured


class DripStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.chunks_emitted = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in (b'{"', b"keys", b'":', b"[]", b"}"):
            await asyncio.sleep(0.01)
            self.chunks_emitted += 1
            yield chunk


async def test_default_fetcher_uses_bounded_non_redirecting_json_request(
    private_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={"keys": [_jwk(private_key)]},
        )

    client_options = _install_http_transport(monkeypatch, handler)
    verifier = CloudflareAccessVerifier(_settings())

    assert await verifier.rejection_code([_token(private_key)]) is None
    assert requested_urls == [f"{TEAM_DOMAIN}/cdn-cgi/access/certs"]
    assert client_options["follow_redirects"] is False
    timeout = cast("httpx.Timeout", client_options["timeout"])
    assert timeout.connect == 5.0
    assert timeout.read == 5.0


async def test_default_fetcher_has_an_absolute_streaming_deadline(
    private_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = DripStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    _install_http_transport(monkeypatch, handler)
    monkeypatch.setattr("moco.web.access._HTTP_TIMEOUT_SECONDS", 0.02)
    verifier = CloudflareAccessVerifier(_settings())
    started = asyncio.get_running_loop().time()

    result = await asyncio.wait_for(
        verifier.rejection_code([_token(private_key)]),
        timeout=0.2,
    )

    assert result == "access_keys_unavailable"
    assert asyncio.get_running_loop().time() - started < 0.1
    assert stream.chunks_emitted < 5


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (302, "application/json", b'{"keys":[]}'),
        (200, "text/plain", b'{"keys":[]}'),
        (200, "application/json", b"x" * 65_537),
        (200, "application/json", b"[]"),
        (200, "application/json", b"not-json"),
        (200, "application/json", b'{"keys":[],"keys":[]}'),
    ],
)
async def test_default_fetcher_rejects_unsafe_or_malformed_responses(
    private_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    _install_http_transport(monkeypatch, handler)
    verifier = CloudflareAccessVerifier(_settings())

    assert await verifier.rejection_code([_token(private_key)]) == "access_keys_unavailable"


async def test_secrets_do_not_appear_in_repr_logs_or_exceptions(
    private_key: rsa.RSAPrivateKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = _token(private_key, claims=_claims(email="sensitive@example.invalid"))
    verifier = CloudflareAccessVerifier(
        _settings(),
        fetch_jwks=FakeFetcher(RuntimeError(f"{TEAM_DOMAIN} {AUDIENCE} {token}")),
    )

    assert await verifier.rejection_code([token]) == "access_keys_unavailable"
    visible = f"{verifier!r}\n{caplog.text}"
    for secret in (token, "sensitive@example.invalid", ALLOWED_EMAIL, TEAM_DOMAIN, AUDIENCE):
        assert secret not in visible
