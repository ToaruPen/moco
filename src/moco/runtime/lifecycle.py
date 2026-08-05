from __future__ import annotations

import time
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class LifecycleState(StrEnum):
    DISABLED = "disabled"
    READY = "ready"
    CONNECTING = "connecting"
    LISTENING = "listening"
    SPEAKING = "speaking"
    IDLE_EXPIRED = "idle_expired"
    ERROR = "error"


class BusyKind(StrEnum):
    LISTENING = "listening"
    DELEGATED = "delegated"
    SYNTHESIS = "synthesis"
    PLAYBACK = "playback"


class LifecycleController:
    def __init__(
        self,
        *,
        idle_timeout_seconds: float,
        on_expire: Callable[[], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_timeout_seconds <= 0:
            msg = "idle_timeout_seconds must be positive"
            raise ValueError(msg)
        self._idle_timeout_seconds = idle_timeout_seconds
        self._on_expire = on_expire
        self._clock = clock
        self._state = LifecycleState.DISABLED
        self._last_activity = clock()
        self._busy = dict.fromkeys(BusyKind, False)

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def last_activity(self) -> float:
        return self._last_activity

    @property
    def is_busy(self) -> bool:
        return any(self._busy.values())

    def enable(self) -> None:
        self._state = LifecycleState.READY
        self.touch()

    def disable(self) -> None:
        self._state = LifecycleState.DISABLED
        for kind in BusyKind:
            self._busy[kind] = False

    def touch(self) -> None:
        self._last_activity = self._clock()

    def set_state(self, state: LifecycleState) -> None:
        self._state = state
        self.touch()

    def set_busy(self, kind: BusyKind, *, active: bool) -> None:
        self._busy[kind] = active
        self.touch()

    def listen_start(self) -> bool:
        starts_fresh = self._state is LifecycleState.IDLE_EXPIRED
        self._busy[BusyKind.LISTENING] = True
        self._state = LifecycleState.LISTENING
        self.touch()
        return starts_fresh

    def listen_stop(self) -> None:
        self._busy[BusyKind.LISTENING] = False
        self._state = LifecycleState.READY
        self.touch()

    async def poll(self) -> bool:
        if self._state in {
            LifecycleState.DISABLED,
            LifecycleState.IDLE_EXPIRED,
            LifecycleState.ERROR,
        }:
            return False
        if self.is_busy:
            return False
        elapsed = self._clock() - self._last_activity
        if elapsed < self._idle_timeout_seconds:
            return False
        self._state = LifecycleState.IDLE_EXPIRED
        await self._on_expire()
        return True
