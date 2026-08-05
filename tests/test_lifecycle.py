from __future__ import annotations

import pytest

from moco.runtime.lifecycle import BusyKind, LifecycleController, LifecycleState


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    "kind",
    [BusyKind.LISTENING, BusyKind.DELEGATED, BusyKind.SYNTHESIS, BusyKind.PLAYBACK],
)
async def test_busy_activity_prevents_idle_expiry(kind: BusyKind) -> None:
    clock = Clock()
    expirations = 0

    async def expire() -> None:
        nonlocal expirations
        expirations += 1

    lifecycle = LifecycleController(
        idle_timeout_seconds=10,
        clock=clock,
        on_expire=expire,
    )
    lifecycle.enable()
    lifecycle.set_busy(kind, active=True)
    clock.now = 100

    assert not await lifecycle.poll()
    assert lifecycle.state is not LifecycleState.IDLE_EXPIRED

    lifecycle.set_busy(kind, active=False)
    clock.now = 109
    assert not await lifecycle.poll()
    clock.now = 110
    assert await lifecycle.poll()
    assert expirations == 1


async def test_idle_expiry_fires_once() -> None:
    clock = Clock()
    expirations = 0

    async def expire() -> None:
        nonlocal expirations
        expirations += 1

    lifecycle = LifecycleController(
        idle_timeout_seconds=5,
        clock=clock,
        on_expire=expire,
    )
    lifecycle.enable()
    clock.now = 5

    assert await lifecycle.poll()
    assert not await lifecycle.poll()
    assert expirations == 1
    assert lifecycle.state is LifecycleState.IDLE_EXPIRED


async def test_listen_start_after_expiry_requests_fresh_session() -> None:
    clock = Clock()

    async def expire() -> None:
        return None

    lifecycle = LifecycleController(
        idle_timeout_seconds=5,
        clock=clock,
        on_expire=expire,
    )
    lifecycle.enable()
    clock.now = 5
    await lifecycle.poll()

    starts_fresh = lifecycle.listen_start()

    assert starts_fresh
    assert lifecycle.state is LifecycleState.LISTENING
    assert lifecycle.is_busy


def test_listening_start_and_stop_update_state_and_activity() -> None:
    clock = Clock()

    async def expire() -> None:
        return None

    lifecycle = LifecycleController(
        idle_timeout_seconds=5,
        clock=clock,
        on_expire=expire,
    )
    lifecycle.enable()

    assert not lifecycle.listen_start()
    assert lifecycle.state.value == "listening"
    clock.now = 2
    lifecycle.listen_stop()

    assert lifecycle.state is LifecycleState.READY
    assert not lifecycle.is_busy
    assert lifecycle.last_activity == 2
