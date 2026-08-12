"""The local trust gate in front of the future Reviewer transport.

This boundary issues short-lived bootstrap values to one authenticated local process and
binds a redeemed value to one in-memory connection capability. It does not carry approval
payloads, implement the Reviewer WebSocket protocol, or create a browser credential.
"""

from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from moco.config import canonical_browser_loopback_host
from moco.errors import CodexReviewError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ReviewGate",
    "ReviewerCapability",
    "is_valid_bootstrap_nonce",
    "is_valid_control_secret",
]

_REVIEW_UNAVAILABLE = "local review is unavailable"
_NONCE_BYTES = 32
_MAX_NONCE_CHARACTERS = 128
_MAX_SECRET_CHARACTERS = 256
_MAX_NONCE_ATTEMPTS = 4


class ReviewerCapability:
    """The one active in-memory reviewer right issued by a gate."""

    __slots__ = ("_active", "_gate")

    def __init__(self, gate: ReviewGate) -> None:
        self._gate = gate
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def release(self) -> None:
        self._gate.release(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(active={self._active})"


def _random_nonce() -> str:
    return secrets.token_urlsafe(_NONCE_BYTES)


class ReviewGate:
    """Issue and redeem one-time local reviewer bootstraps.

    The gate is deliberately synchronous: issuing and redeeming a bootstrap are short
    in-memory transactions, and the lock makes simultaneous redemption consume exactly one
    nonce and occupy exactly one reviewer slot. The caller owns the connection object and
    must release the returned capability when that connection goes away.
    """

    BOOTSTRAP_LIFETIME_SECONDS = 30.0
    MAX_PENDING_BOOTSTRAPS = 64

    def __init__(
        self,
        control_secret: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        nonce_source: Callable[[], str] = _random_nonce,
    ) -> None:
        if not _is_safe_secret(control_secret):
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        self._control_secret = control_secret
        self._clock = clock
        self._nonce_source = nonce_source
        self._pending: OrderedDict[str, float] = OrderedDict()
        self._reviewer: ReviewerCapability | None = None
        self._lock = threading.Lock()

    def issue_bootstrap_nonce(
        self,
        control_secret: str | None,
        *,
        peer_host: str | None,
        host: str | None,
        origin: str | None,
    ) -> str:
        """Issue one nonce only after the entire local request boundary is trusted."""
        self.validate_transport(peer_host=peer_host, host=host, origin=origin)
        if not _same_secret(control_secret, self._control_secret):
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        with self._lock:
            if self._reviewer is not None and self._reviewer.active:
                raise CodexReviewError(_REVIEW_UNAVAILABLE)
            now = self._clock()
            self._discard_expired(now)
            for _ in range(_MAX_NONCE_ATTEMPTS):
                nonce = self._nonce_source()
                if not _is_safe_nonce(nonce):
                    raise CodexReviewError(_REVIEW_UNAVAILABLE)
                if nonce in self._pending:
                    continue
                if len(self._pending) >= self.MAX_PENDING_BOOTSTRAPS:
                    self._pending.popitem(last=False)
                self._pending[nonce] = now + self.BOOTSTRAP_LIFETIME_SECONDS
                return nonce
        raise CodexReviewError(_REVIEW_UNAVAILABLE)

    def redeem_bootstrap_nonce(
        self,
        nonce: str | None,
        *,
        peer_host: str | None,
        host: str | None,
        origin: str | None,
    ) -> ReviewerCapability:
        """Consume a nonce and bind the resulting capability to one connection."""
        if not _is_safe_nonce(nonce):
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        nonce_text = cast("str", nonce)
        self.validate_transport(peer_host=peer_host, host=host, origin=origin)
        with self._lock:
            now = self._clock()
            self._discard_expired(now)
            matched = next(
                (stored for stored in self._pending if secrets.compare_digest(stored, nonce_text)),
                None,
            )
            if matched is None:
                raise CodexReviewError(_REVIEW_UNAVAILABLE)
            expires_at = self._pending.pop(matched)
            if expires_at <= now:
                raise CodexReviewError(_REVIEW_UNAVAILABLE)
            if self._reviewer is not None and self._reviewer.active:
                raise CodexReviewError(_REVIEW_UNAVAILABLE)
            capability = ReviewerCapability(self)
            self._pending.clear()
            self._reviewer = capability
            return capability

    def validate_transport(
        self,
        *,
        peer_host: str | None,
        host: str | None,
        origin: str | None,
    ) -> None:
        """Reject a reviewer transport that is not entirely local."""
        if not self._request_is_trusted(peer_host=peer_host, host=host, origin=origin):
            raise CodexReviewError(_REVIEW_UNAVAILABLE)

    def release(self, capability: ReviewerCapability) -> None:
        """Release a capability after its bound reviewer connection disconnects."""
        if type(capability) is not ReviewerCapability or capability._gate is not self:  # noqa: SLF001
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        with self._lock:
            if self._reviewer is capability:
                capability._active = False  # noqa: SLF001
                self._reviewer = None

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"{type(self).__name__}(pending={len(self._pending)}, "
                f"reviewer={self._reviewer is not None})"
            )

    def _discard_expired(self, now: float) -> None:
        expired = [nonce for nonce, expires_at in self._pending.items() if expires_at <= now]
        for nonce in expired:
            self._pending.pop(nonce, None)

    @staticmethod
    def _request_is_trusted(
        *,
        peer_host: str | None,
        host: str | None,
        origin: str | None,
    ) -> bool:
        if not _is_loopback_peer(peer_host):
            return False
        host_authority = _parse_authority(host)
        if (
            host_authority is None
            or canonical_browser_loopback_host(
                host_authority[0],
                allow_localhost=True,
            )
            is None
        ):
            return False
        if not isinstance(origin, str):
            return False
        try:
            origin_parts = urlsplit(origin)
        except ValueError:
            return False
        if (
            origin_parts.scheme.casefold() != "http"
            or origin_parts.path not in {"", "/"}
            or origin_parts.query
            or origin_parts.fragment
            or origin_parts.username is not None
            or origin_parts.password is not None
        ):
            return False
        origin_authority = _parse_authority(origin_parts.netloc)
        return (
            origin_authority is not None
            and canonical_browser_loopback_host(
                origin_authority[0],
                allow_localhost=True,
            )
            is not None
            and origin_authority[1] == host_authority[1]
            and origin_authority[2] == host_authority[2]
        )


def _is_safe_secret(value: object) -> bool:
    if not _is_safe_opaque(value, max_characters=_MAX_SECRET_CHARACTERS):
        return False
    value_text = cast("str", value)
    return all(
        character.isascii() and (character.isalnum() or character in {"-", "_"})
        for character in value_text
    )


def is_valid_control_secret(value: object) -> bool:
    return _is_safe_secret(value)


def _is_safe_nonce(value: object) -> bool:
    if not _is_safe_opaque(value, max_characters=_MAX_NONCE_CHARACTERS):
        return False
    value_text = cast("str", value)
    return all(
        character.isascii() and (character.isalnum() or character in {"-", "_"})
        for character in value_text
    )


def is_valid_bootstrap_nonce(value: object) -> bool:
    return _is_safe_nonce(value)


def _is_safe_opaque(value: object, *, max_characters: int) -> bool:
    if type(value) is not str or not value or len(value) > max_characters:
        return False
    try:
        return len(value.encode("utf-8")) <= max_characters
    except UnicodeEncodeError:
        return False


def _same_secret(candidate: object, expected: str) -> bool:
    if not _is_safe_secret(candidate):
        return False
    return secrets.compare_digest(cast("str", candidate), expected)


def _is_loopback_peer(value: object) -> bool:
    """Accept OS transport loopback forms, including IPv4-mapped peers."""
    if type(value) is not str or not value:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _parse_authority(value: object) -> tuple[str, int | None, str] | None:
    if type(value) is not str or not value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        hostname is None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return hostname.casefold(), port, parsed.netloc.casefold()
