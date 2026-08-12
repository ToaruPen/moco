from __future__ import annotations

import pytest

from moco.runtime.lifecycle import IdleLeaseTimer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_idle_lease_timer_records_only_timestamp_and_expired_claim() -> None:
    clock = Clock()
    timer = IdleLeaseTimer(idle_timeout_seconds=5, clock=clock)

    assert timer.last_activity == 0
    assert not timer.expired
    assert set(vars(timer)) == {"_clock", "_idle_timeout_seconds", "_last_activity", "_expired"}


def test_idle_lease_timer_expires_once_only_while_snapshot_is_idle() -> None:
    clock = Clock()
    timer = IdleLeaseTimer(idle_timeout_seconds=5, clock=clock)
    clock.now = 5

    assert not timer.claim_expired(is_idle=False)
    assert not timer.expired
    assert timer.claim_expired(is_idle=True)
    assert timer.expired
    assert not timer.claim_expired(is_idle=True)  # type: ignore[unreachable]


def test_idle_lease_timer_touch_starts_a_fresh_period() -> None:
    clock = Clock()
    timer = IdleLeaseTimer(idle_timeout_seconds=5, clock=clock)
    clock.now = 4
    timer.touch()
    clock.now = 8

    assert not timer.claim_expired(is_idle=True)
    clock.now = 9
    assert timer.claim_expired(is_idle=True)


def test_idle_lease_timer_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="positive"):
        IdleLeaseTimer(idle_timeout_seconds=0)
