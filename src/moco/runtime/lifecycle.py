from __future__ import annotations

import time
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class LifecycleState(StrEnum):
    DISABLED = "disabled"
    CONNECTING = "connecting"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    WAITING_FOR_LOCAL_REVIEW = "waiting_for_local_review"
    SPEAKING = "speaking"
    READY = "ready"
    VOICE_RECONNECT_REQUIRED = "voice_reconnect_required"
    CONNECTION_LOST = "connection_lost"
    IDLE_EXPIRED = "idle_expired"
    ERROR = "error"


class IdleLeaseTimer:
    """A one-shot idle expiry timestamp for one published conversation lease."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_timeout_seconds <= 0:
            message = "idle_timeout_seconds must be positive"
            raise ValueError(message)
        self._idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        self._last_activity = clock()
        self._expired = False

    @property
    def last_activity(self) -> float:
        return self._last_activity

    @property
    def expired(self) -> bool:
        return self._expired

    def touch(self) -> None:
        self._last_activity = self._clock()

    def claim_expired(self, *, is_idle: bool) -> bool:
        if self._expired or not is_idle:
            return False
        if self._clock() - self._last_activity < self._idle_timeout_seconds:
            return False
        self._expired = True
        return True
