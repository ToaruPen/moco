from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

import httpx
import jwt

if TYPE_CHECKING:
    from moco.config import CloudflareAccessSettings

AccessRejectionCode = Literal[
    "access_token_missing",
    "access_token_invalid",
    "access_identity_mismatch",
    "access_keys_unavailable",
]

JwksFetcher = Callable[[], Awaitable[Mapping[str, object]]]

_ALGORITHM = "RS256"
_ASSERTION_MAX_BYTES = 16_384
_JWKS_RESPONSE_MAX_BYTES = 65_536
_JWKS_CACHE_TTL_SECONDS = 3600.0
_HTTP_TIMEOUT_SECONDS = 5.0
_KID_MAX_BYTES = 256
_EMAIL_MAX_BYTES = 254
_AUDIENCE_LIST_MAX_ITEMS = 16
_JWKS_MAX_KEYS = 64
_JWK_MAX_BYTES = 16_384
_JWK_MODULUS_MAX_BYTES = 8192
_JWK_EXPONENT_MAX_BYTES = 16
_BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_PRIVATE_RSA_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})
_JWT_SEGMENT_COUNT = 3


class _KeysUnavailableError(Exception):
    pass


class _UnknownKeyError(Exception):
    pass


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError


def _load_json_object(data: bytes) -> dict[str, object]:
    value = json.loads(
        data,
        object_pairs_hook=_reject_duplicate_object_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise TypeError
    return cast("dict[str, object]", value)


def _is_json_content_type(value: str) -> bool:
    media_type = value.partition(";")[0].strip().casefold()
    return media_type == "application/json" or media_type.endswith("+json")


async def _fetch_cloudflare_jwks(team_domain: str) -> Mapping[str, object]:
    endpoint = f"{team_domain}/cdn-cgi/access/certs"
    try:
        async with (
            httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
            ) as client,
            client.stream(
                "GET",
                endpoint,
                headers={"accept": "application/json"},
            ) as response,
        ):
            body = await _read_json_response(response)
        return _load_json_object(bytes(body))
    except _KeysUnavailableError:
        raise
    except (httpx.HTTPError, OSError, TypeError, UnicodeError, ValueError):
        raise _KeysUnavailableError from None


async def _read_json_response(response: httpx.Response) -> bytearray:
    if response.status_code != httpx.codes.OK:
        raise _KeysUnavailableError
    if not _is_json_content_type(response.headers.get("content-type", "")):
        raise _KeysUnavailableError
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > _JWKS_RESPONSE_MAX_BYTES:
            raise _KeysUnavailableError
        body.extend(chunk)
    return body


def _valid_bounded_string(value: object, *, max_bytes: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    return size <= max_bytes


def _valid_kid(value: object) -> bool:
    return _valid_bounded_string(value, max_bytes=_KID_MAX_BYTES) and not any(
        character.isspace() or not character.isprintable() for character in cast("str", value)
    )


def _decode_canonical_segment(segment: str) -> bytes:
    if not segment or _BASE64URL_PATTERN.fullmatch(segment) is None:
        raise ValueError
    try:
        padded = segment + "=" * (-len(segment) % 4)
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeError, binascii.Error, ValueError) as error:
        raise ValueError from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != segment:
        raise ValueError
    return decoded


def _preflight_token(token: str) -> str:
    segments = token.split(".")
    if len(segments) != _JWT_SEGMENT_COUNT or any(not segment for segment in segments):
        raise ValueError
    raw_header, raw_payload, _raw_signature = (
        _decode_canonical_segment(segment) for segment in segments
    )
    header = _load_json_object(raw_header)
    _load_json_object(raw_payload)
    if header.get("alg") != _ALGORITHM or not _valid_kid(header.get("kid")):
        raise ValueError
    return cast("str", header["kid"])


def _valid_numeric_date(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _valid_audience(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if not isinstance(value, list) or not 1 <= len(value) <= _AUDIENCE_LIST_MAX_ITEMS:
        return False
    return all(_valid_bounded_string(item, max_bytes=256) for item in value) and expected in value


def _valid_claims(claims: Mapping[str, object], settings: CloudflareAccessSettings) -> bool:
    if claims.get("iss") != settings.team_domain or not isinstance(claims.get("iss"), str):
        return False
    if not _valid_audience(claims.get("aud"), settings.audience):
        return False
    if not _valid_numeric_date(claims.get("exp")):
        return False
    for optional_numeric_date in ("iat", "nbf"):
        if optional_numeric_date in claims and not _valid_numeric_date(
            claims[optional_numeric_date]
        ):
            return False
    return _valid_bounded_string(claims.get("email"), max_bytes=_EMAIL_MAX_BYTES)


def _bounded_jwk_mapping(item: Mapping[str, object]) -> bool:
    try:
        encoded = json.dumps(dict(item), separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        return False
    return len(encoded) <= _JWK_MAX_BYTES


def _parse_jwks(payload: Mapping[str, object]) -> dict[str, jwt.PyJWK]:
    if not isinstance(payload, Mapping):
        raise _KeysUnavailableError
    items = payload.get("keys")
    if not isinstance(items, list) or not 1 <= len(items) <= _JWKS_MAX_KEYS:
        raise _KeysUnavailableError

    keys: dict[str, jwt.PyJWK] = {}
    for item in items:
        if not isinstance(item, Mapping) or not _bounded_jwk_mapping(item):
            raise _KeysUnavailableError
        kid = item.get("kid")
        modulus = item.get("n")
        exponent = item.get("e")
        if (
            not _valid_kid(kid)
            or kid in keys
            or item.get("kty") != "RSA"
            or item.get("alg") != _ALGORITHM
            or item.get("use") != "sig"
            or any(field in item for field in _PRIVATE_RSA_FIELDS)
            or not _valid_bounded_string(modulus, max_bytes=_JWK_MODULUS_MAX_BYTES)
            or not _valid_bounded_string(exponent, max_bytes=_JWK_EXPONENT_MAX_BYTES)
            or _BASE64URL_PATTERN.fullmatch(cast("str", modulus)) is None
            or _BASE64URL_PATTERN.fullmatch(cast("str", exponent)) is None
        ):
            raise _KeysUnavailableError
        try:
            pyjwk = jwt.PyJWK.from_dict(dict(item), algorithm=_ALGORITHM)
        except (jwt.PyJWTError, TypeError, ValueError):
            raise _KeysUnavailableError from None
        if pyjwk.key_type != "RSA" or pyjwk.algorithm_name != _ALGORITHM:
            raise _KeysUnavailableError
        keys[cast("str", kid)] = pyjwk
    return keys


def _extract_assertion(assertions: Sequence[str]) -> tuple[AccessRejectionCode | None, str | None]:
    if not assertions:
        return "access_token_missing", None
    if len(assertions) != 1:
        return "access_token_invalid", None
    token = assertions[0]
    if not isinstance(token, str) or not token:
        return "access_token_invalid", None
    try:
        if len(token.encode("utf-8")) > _ASSERTION_MAX_BYTES:
            return "access_token_invalid", None
    except UnicodeError:
        return "access_token_invalid", None
    return None, token


class CloudflareAccessVerifier:
    def __init__(
        self,
        settings: CloudflareAccessSettings,
        *,
        fetch_jwks: JwksFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._fetch_jwks = fetch_jwks or (lambda: _fetch_cloudflare_jwks(settings.team_domain))
        self._monotonic = monotonic
        self._cache_lock = asyncio.Lock()
        self._cached_keys: dict[str, jwt.PyJWK] | None = None
        self._cached_at = 0.0
        self._cache_generation = 0

    async def rejection_code(self, assertions: Sequence[str]) -> AccessRejectionCode | None:
        rejection_code, token = _extract_assertion(assertions)
        if rejection_code is not None or token is None:
            return rejection_code
        try:
            claims = await self._verified_claims(token)
        except _UnknownKeyError:
            return "access_token_invalid"
        except _KeysUnavailableError:
            return "access_keys_unavailable"
        except Exception:  # noqa: BLE001 - malformed assertions must fail closed.
            return "access_token_invalid"
        email = cast("str", claims["email"])
        if email.casefold() != self._settings.allowed_email.casefold():
            return "access_identity_mismatch"
        return None

    async def _verified_claims(self, token: str) -> Mapping[str, object]:
        kid = _preflight_token(token)
        key = await self._key_for(kid)
        decoded = jwt.decode(
            token,
            key,
            algorithms=[_ALGORITHM],
            audience=self._settings.audience,
            issuer=self._settings.team_domain,
            options={
                "require": ["exp", "iss", "aud", "email"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
        claims = cast("Mapping[str, object]", decoded)
        if not _valid_claims(claims, self._settings):
            raise jwt.InvalidTokenError
        return claims

    def _cache_is_fresh(self, now: float) -> bool:
        age = now - self._cached_at
        return self._cached_keys is not None and 0 <= age < _JWKS_CACHE_TTL_SECONDS

    async def _key_for(self, kid: str) -> jwt.PyJWK:
        now = self._monotonic()
        observed_generation = self._cache_generation
        if self._cache_is_fresh(now) and self._cached_keys is not None:
            cached_key = self._cached_keys.get(kid)
            if cached_key is not None:
                return cached_key

        async with self._cache_lock:
            now = self._monotonic()
            if self._cache_is_fresh(now) and self._cached_keys is not None:
                cached_key = self._cached_keys.get(kid)
                if cached_key is not None:
                    return cached_key
                if self._cache_generation != observed_generation:
                    raise _UnknownKeyError
                return await self._refresh_key(kid, now, retry_if_missing=False)
            return await self._refresh_key(kid, now, retry_if_missing=True)

    async def _refresh_key(
        self,
        kid: str,
        now: float,
        *,
        retry_if_missing: bool,
    ) -> jwt.PyJWK:
        keys = await self._fetch_and_cache(now)
        key = keys.get(kid)
        if key is None and retry_if_missing:
            keys = await self._fetch_and_cache(self._monotonic())
            key = keys.get(kid)
        if key is None:
            raise _UnknownKeyError
        return key

    async def _fetch_and_cache(self, now: float) -> dict[str, jwt.PyJWK]:
        try:
            payload = await self._fetch_jwks()
            keys = _parse_jwks(payload)
        except _KeysUnavailableError:
            raise
        except Exception:  # noqa: BLE001 - injected network boundaries must fail closed.
            raise _KeysUnavailableError from None
        self._cached_keys = keys
        self._cached_at = now
        self._cache_generation += 1
        return keys
