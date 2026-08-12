from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator, Mapping
from contextlib import suppress
from inspect import iscoroutine
from itertools import pairwise
from typing import cast

import pytest

import moco.codex.rpc as rpc_module
from moco.codex.rpc import (
    _MAX_ACTIVE_SERVER_REQUESTS,
    JsonValue,
    RpcFailure,
    RpcNotification,
    RpcPeer,
    RpcServerRequest,
    RpcSuccess,
    _as_json_value,
    _classify_message,
    _NotificationSubscription,
)
from moco.errors import (
    CodexProcessExitedError,
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTimeoutError,
)


class _QueueWriter:
    def __init__(self, written: asyncio.Queue[dict[str, JsonValue]]) -> None:
        self._written = written
        self.error: OSError | None = None
        self.drain_gate: asyncio.Event | None = None

    def write(self, data: bytes) -> None:
        if self.error is not None:
            raise self.error
        for line in data.splitlines():
            decoded = json.loads(line)
            self._written.put_nowait(cast("dict[str, JsonValue]", decoded))

    async def drain(self) -> None:
        if self.drain_gate is not None:
            await self.drain_gate.wait()
        await asyncio.sleep(0)


class _SensitiveInvalidJson:
    def __repr__(self) -> str:
        return "JSON_SECRET_REPR"


class _ReadFailureReader:
    async def readline(self) -> bytes:
        msg = "READ_TRANSPORT_SECRET"
        raise OSError(msg)


class PeerHarness:
    def __init__(self, *, request_timeout: float = 1.0) -> None:
        self.reader = asyncio.StreamReader()
        self.written: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue()
        self.writer = _QueueWriter(self.written)
        self.request_timeout = request_timeout
        self.peer = RpcPeer(
            self.reader,
            cast("asyncio.StreamWriter", self.writer),
            request_timeout=self.request_timeout,
        )

    def feed(self, message: dict[str, JsonValue]) -> None:
        encoded = json.dumps(message, separators=(",", ":")).encode()
        self.reader.feed_data(encoded + b"\n")

    async def next_written(self) -> dict[str, JsonValue]:
        return await asyncio.wait_for(self.written.get(), 1.0)

    async def receive(self, message: dict[str, JsonValue]) -> None:
        self.feed(message)
        await asyncio.sleep(0)


# One buffered burst far larger than anything the peer admits at once.
BURST = 2000


def active_incoming(peer: RpcPeer) -> int:
    """Read how many inbound server requests the peer is serving right now."""
    return len(peer._incoming)  # noqa: SLF001


def inbound_tasks(peer: RpcPeer) -> list[asyncio.Task[None]]:
    """Take the tasks the peer created for the requests it is serving right now."""
    return [call.task for call in peer._incoming.values()]  # noqa: SLF001


def register_contract_violating_observer(
    peer: RpcPeer,
    observer: Callable[[RpcNotification], object],
) -> None:
    """Bypass the static boundary to verify its fail-closed runtime guard."""
    peer.register_notification_observer(cast("Callable[[RpcNotification], None]", observer))


async def settle() -> None:
    """Run every task that is already runnable, without waiting on the clock."""
    for _ in range(10):
        await asyncio.sleep(0)


def test_rpc_module_does_not_expose_process_owning_client() -> None:
    assert not hasattr(rpc_module, "CodexRpcClient")


@pytest.fixture
async def peer_harness() -> AsyncIterator[PeerHarness]:
    harness = PeerHarness()
    await harness.peer.start()
    try:
        yield harness
    finally:
        await harness.peer.close()


async def test_peer_keeps_client_and_server_request_ids_independent() -> None:
    harness = PeerHarness()
    peer = harness.peer

    async def handle_server_request(request: RpcServerRequest) -> JsonValue:
        assert request.params == {"side": "server"}
        return {"handled": True}

    peer.register_server_request_handler("server/do", handle_server_request)
    await peer.start()
    client_request = asyncio.create_task(peer.request("client/do"))
    assert await harness.next_written() == {"id": 1, "method": "client/do"}

    harness.feed({"id": 1, "method": "server/do", "params": {"side": "server"}})
    assert await harness.next_written() == {"id": 1, "result": {"handled": True}}
    assert not client_request.done()

    harness.feed({"id": 1, "result": {"side": "client"}})
    assert await client_request == {"side": "client"}
    await peer.close()


async def test_peer_start_is_idempotent() -> None:
    harness = PeerHarness()

    await harness.peer.start()
    reader_task = harness.peer._reader_task  # noqa: SLF001
    await harness.peer.start()

    assert harness.peer._reader_task is reader_task  # noqa: SLF001
    await harness.peer.close()


async def test_notification_observer_registers_once_before_start() -> None:
    harness = PeerHarness()

    harness.peer.register_notification_observer(lambda _notification: None)

    with pytest.raises(RuntimeError, match="one notification observer"):
        harness.peer.register_notification_observer(lambda _notification: None)

    await harness.peer.start()
    with pytest.raises(RuntimeError, match=r"before.*start"):
        harness.peer.register_notification_observer(lambda _notification: None)
    await harness.peer.close()


async def test_notification_before_request_runs_observer_then_fanout_then_handler() -> None:
    harness = PeerHarness()
    observed: list[tuple[str, str]] = []
    handler_started = asyncio.Event()
    subscription = cast("_NotificationSubscription", harness.peer.notifications())

    def observe(notification: RpcNotification) -> None:
        observed.append(("observer", notification.method))

    async def handle(request: RpcServerRequest) -> JsonValue:
        assert request.params == {"after": True}
        assert observed == [("observer", "event/before")]
        assert subscription._queue.qsize() == 1  # noqa: SLF001
        observed.append(("handler", request.method))
        handler_started.set()
        return {"handled": True}

    harness.peer.register_notification_observer(observe)
    harness.peer.register_server_request_handler("host/after", handle)
    await harness.peer.start()
    harness.reader.feed_data(
        b'{"method":"event/before","params":{"order":"first"}}\n'
        b'{"id":"server-after","method":"host/after","params":{"after":true}}\n'
    )

    await asyncio.wait_for(handler_started.wait(), 1.0)
    notification = await asyncio.wait_for(anext(subscription), 1.0)
    response = await harness.next_written()
    await settle()

    assert notification == RpcNotification("event/before", {"order": "first"})
    assert observed == [("observer", "event/before"), ("handler", "host/after")]
    assert response == {"id": "server-after", "result": {"handled": True}}
    assert harness.written.empty()
    await subscription.aclose()
    await harness.peer.close()


@pytest.mark.parametrize("returns_awaitable", [False, True])
async def test_notification_observer_failure_is_payload_free_and_terminal(
    returns_awaitable: bool,
) -> None:
    harness = PeerHarness()
    handler_started = False
    sensitive = "NOTIFICATION_OBSERVER_PAYLOAD_SECRET"

    async def awaitable_observer(_notification: RpcNotification) -> None:
        return None

    def failing_observer(_notification: RpcNotification) -> object:
        if returns_awaitable:
            return awaitable_observer(_notification)
        raise RuntimeError(sensitive)

    async def handle(_request: RpcServerRequest) -> JsonValue:
        nonlocal handler_started
        handler_started = True
        return None

    register_contract_violating_observer(harness.peer, failing_observer)
    harness.peer.register_server_request_handler("host/after", handle)
    subscription = harness.peer.notifications()
    await harness.peer.start()
    harness.reader.feed_data(
        b'{"method":"event/fail","params":{"secret":"wire-secret"}}\n'
        b'{"id":1,"method":"host/after","params":{}}\n'
    )

    with pytest.raises(CodexRpcProtocolError) as caught:
        await asyncio.wait_for(anext(subscription), 1.0)
    with pytest.raises(CodexRpcProtocolError) as sticky:
        await harness.peer.request("client/after-observer-failure")

    assert sticky.value is caught.value
    assert caught.value.data is None
    assert sensitive not in str(caught.value)
    assert "wire-secret" not in str(caught.value)
    assert not handler_started
    assert harness.written.empty()
    await harness.peer.close()


@pytest.mark.parametrize("awaitable_kind", ["future", "task"])
async def test_notification_observer_consumes_failed_awaitable_exception(
    awaitable_kind: str,
) -> None:
    loop = asyncio.get_running_loop()
    reports: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    secret = f"FAILED_{awaitable_kind.upper()}_OBSERVER_SECRET"

    async def exercise() -> CodexRpcProtocolError:
        harness = PeerHarness()
        if awaitable_kind == "future":
            returned: asyncio.Future[None] = loop.create_future()
            returned.set_exception(RuntimeError(secret))
        else:

            async def fail() -> None:
                raise RuntimeError(secret)

            returned = asyncio.create_task(fail())
            await settle()
            assert returned.done()
        values: list[object] = [returned]

        def observe(_notification: RpcNotification) -> object:
            return values.pop()

        register_contract_violating_observer(harness.peer, observe)
        subscription = harness.peer.notifications()
        await harness.peer.start()
        harness.feed({"method": "event/fail", "params": {"secret": "wire-secret"}})
        try:
            with pytest.raises(CodexRpcProtocolError) as caught:
                await asyncio.wait_for(anext(subscription), 1.0)
            assert returned._log_traceback is False  # noqa: SLF001
            return caught.value
        finally:
            await harness.peer.close()
            with suppress(BaseException):
                returned.exception()

    try:
        error = await exercise()
        gc.collect()
        await settle()

        assert error.data is None
        assert reports == []
    finally:
        loop.set_exception_handler(previous_handler)


async def test_notification_observer_does_not_cancel_caller_owned_future() -> None:
    harness = PeerHarness()
    pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    register_contract_violating_observer(harness.peer, lambda _notification: pending)
    subscription = harness.peer.notifications()
    await harness.peer.start()
    harness.feed({"method": "event/fail", "params": {"secret": "wire-secret"}})

    try:
        with pytest.raises(CodexRpcProtocolError) as caught:
            await asyncio.wait_for(anext(subscription), 1.0)
        await settle()

        assert caught.value.data is None
        assert not pending.done()
    finally:
        if not pending.done():
            pending.cancel()
        await harness.peer.close()


async def test_notification_observer_does_not_cancel_caller_owned_task() -> None:
    harness = PeerHarness()
    blocker = asyncio.Event()
    finished = asyncio.Event()

    async def wait_forever() -> None:
        try:
            await blocker.wait()
        finally:
            finished.set()

    pending = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)
    register_contract_violating_observer(harness.peer, lambda _notification: pending)
    subscription = harness.peer.notifications()
    await harness.peer.start()
    harness.feed({"method": "event/fail", "params": {"secret": "wire-secret"}})

    try:
        with pytest.raises(CodexRpcProtocolError) as caught:
            await asyncio.wait_for(anext(subscription), 1.0)
        await settle()

        assert caught.value.data is None
        assert not pending.done()
        assert not finished.is_set()
    finally:
        if not pending.done():
            pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await harness.peer.close()


class _ObserverAwaitableProbe:
    def __init__(self, *owners: asyncio.Task[None]) -> None:
        self.owners = owners
        self.close_result: object = None
        self.cancel_result: object = None
        self.await_started = False
        self.close_reads = 0
        self.close_calls = 0
        self.cancel_reads = 0
        self.cancel_calls = 0

    def __await__(self) -> Generator[None]:
        self.await_started = True
        yield from ()

    @property
    def close(self) -> Callable[[], object]:
        self.close_reads += 1
        return self._close

    def _close(self) -> object:
        self.close_calls += 1
        return self.close_result

    @property
    def cancel(self) -> Callable[[], object]:
        self.cancel_reads += 1
        return self._cancel

    def _cancel(self) -> object:
        self.cancel_calls += 1
        for owner in self.owners:
            owner.cancel()
        return self.cancel_result


def _custom_cleanup_chain(length: int, leaf: object) -> tuple[_ObserverAwaitableProbe, ...]:
    chain = tuple(_ObserverAwaitableProbe() for _ in range(length))
    for current, following in pairwise(chain):
        current.close_result = following
    chain[-1].close_result = leaf
    return chain


async def _payload_bearing_backing_work(blocker: asyncio.Event) -> None:
    await blocker.wait()
    msg = "BACKING_TASK_FAILURE_SECRET"
    raise RuntimeError(msg)


async def _terminalize_observer_awaitable(result: object) -> CodexRpcProtocolError:
    harness = PeerHarness()
    register_contract_violating_observer(harness.peer, lambda _notification: result)
    subscription = harness.peer.notifications()
    await harness.peer.start()
    harness.feed({"method": "event/fail", "params": {"secret": "wire-secret"}})
    try:
        with pytest.raises(CodexRpcProtocolError) as caught:
            await asyncio.wait_for(anext(subscription), 1.0)
        return caught.value
    finally:
        await harness.peer.close()


async def test_notification_observer_does_not_close_custom_awaitable() -> None:
    returned = _ObserverAwaitableProbe()
    error = await _terminalize_observer_awaitable(returned)

    assert returned.close_reads == 0
    assert returned.close_calls == 0
    assert returned.cancel_reads == 0
    assert returned.cancel_calls == 0
    assert not returned.await_started
    assert error.data is None


@pytest.mark.parametrize("method", ["close", "cancel"])
async def test_notification_observer_does_not_inspect_custom_cleanup_methods(
    method: str,
) -> None:
    returned = _ObserverAwaitableProbe()
    error = await _terminalize_observer_awaitable(returned)

    assert method in {"close", "cancel"}
    assert returned.close_reads == 0
    assert returned.close_calls == 0
    assert returned.cancel_reads == 0
    assert returned.cancel_calls == 0
    assert not returned.await_started
    assert error.data is None


async def test_notification_observer_does_not_follow_custom_cleanup_result() -> None:
    outer = _ObserverAwaitableProbe()
    nested = _ObserverAwaitableProbe()
    outer.close_result = nested

    error = await _terminalize_observer_awaitable(outer)
    await settle()

    assert outer.close_reads == 0
    assert outer.close_calls == 0
    assert nested.close_reads == 0
    assert nested.close_calls == 0
    assert not outer.await_started
    assert not nested.await_started
    assert error.data is None


async def test_notification_observer_does_not_traverse_custom_cleanup_cycle() -> None:
    first = _ObserverAwaitableProbe()
    second = _ObserverAwaitableProbe()
    first.close_result = second
    second.close_result = first

    error = await _terminalize_observer_awaitable(first)
    await settle()

    assert first.close_reads == 0
    assert first.close_calls == 0
    assert second.close_reads == 0
    assert second.close_calls == 0
    assert not first.await_started
    assert not second.await_started
    assert error.data is None


async def test_notification_observer_does_not_claim_custom_awaitable_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = asyncio.get_running_loop()
    reports: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    backing = asyncio.create_task(_payload_bearing_backing_work(asyncio.Event()))
    returned = _ObserverAwaitableProbe(backing)
    await asyncio.sleep(0)

    try:
        error = await _terminalize_observer_awaitable(returned)
        await settle()
        gc.collect()
        stderr = capsys.readouterr().err

        assert returned.close_reads == 0
        assert returned.close_calls == 0
        assert returned.cancel_reads == 0
        assert returned.cancel_calls == 0
        assert not backing.done()
        assert not backing.cancelled()
        assert not returned.await_started
        assert error.data is None
        assert "BACKING_TASK_FAILURE_SECRET" not in repr(reports)
        assert "BACKING_TASK_FAILURE_SECRET" not in stderr
    finally:
        loop.set_exception_handler(previous_handler)
        if not backing.done():
            backing.cancel()
        await asyncio.gather(backing, return_exceptions=True)


async def test_notification_observer_does_not_claim_owners_in_cleanup_cycle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = asyncio.get_running_loop()
    reports: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    tasks = tuple(
        asyncio.create_task(_payload_bearing_backing_work(asyncio.Event())) for _ in range(2)
    )
    first = _ObserverAwaitableProbe(tasks[0])
    second = _ObserverAwaitableProbe(tasks[1])
    first.close_result = second
    second.close_result = first
    await asyncio.sleep(0)

    try:
        error = await _terminalize_observer_awaitable(first)
        await settle()
        gc.collect()
        stderr = capsys.readouterr().err

        assert (first.close_reads, second.close_reads) == (0, 0)
        assert (first.close_calls, second.close_calls) == (0, 0)
        assert (first.cancel_reads, second.cancel_reads) == (0, 0)
        assert (first.cancel_calls, second.cancel_calls) == (0, 0)
        assert all(not task.done() and not task.cancelled() for task in tasks)
        assert not first.await_started
        assert not second.await_started
        assert error.data is None
        assert "BACKING_TASK_FAILURE_SECRET" not in repr(reports)
        assert "BACKING_TASK_FAILURE_SECRET" not in stderr
    finally:
        loop.set_exception_handler(previous_handler)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("close_kind", ["future", "coroutine"])
async def test_notification_observer_does_not_call_custom_close_or_cancel(
    close_kind: str,
) -> None:
    blocker = asyncio.Event()
    backing = asyncio.create_task(_payload_bearing_backing_work(blocker))
    returned = _ObserverAwaitableProbe(backing)
    cancel_result = _ObserverAwaitableProbe()
    cleanup_ran = False

    async def cleanup_coroutine() -> None:
        nonlocal cleanup_ran
        cleanup_ran = True

    if close_kind == "future":
        close_result: object = asyncio.get_running_loop().create_future()
    else:
        close_result = cleanup_coroutine()
    returned.close_result = close_result
    returned.cancel_result = cancel_result
    await asyncio.sleep(0)

    try:
        error = await _terminalize_observer_awaitable(returned)
        await settle()

        assert returned.close_reads == 0
        assert returned.close_calls == 0
        assert returned.cancel_reads == 0
        assert returned.cancel_calls == 0
        assert not backing.cancelled()
        assert cancel_result.close_reads == 0
        assert cancel_result.close_calls == 0
        assert not cancel_result.await_started
        if close_kind == "future":
            assert isinstance(close_result, asyncio.Future)
            assert not close_result.done()
        else:
            assert iscoroutine(close_result)
            assert close_result.cr_frame is not None
            assert not cleanup_ran
        assert error.data is None
    finally:
        if not backing.done():
            backing.cancel()
        if isinstance(close_result, asyncio.Future) and not close_result.done():
            close_result.cancel()
        elif iscoroutine(close_result):
            close_result.close()
        await asyncio.gather(backing, return_exceptions=True)


@pytest.mark.parametrize("leaf_kind", ["future", "task", "coroutine"])
async def test_notification_observer_does_not_reach_native_leaf_through_custom_owner(
    leaf_kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = asyncio.get_running_loop()
    reports: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    blocker = asyncio.Event()
    cleanup_ran = False

    async def cleanup_coroutine() -> None:
        nonlocal cleanup_ran
        cleanup_ran = True
        msg = "BOUNDARY_COROUTINE_FAILURE_SECRET"
        raise RuntimeError(msg)

    if leaf_kind == "future":
        leaf: object = loop.create_future()
    elif leaf_kind == "task":
        leaf = asyncio.create_task(_payload_bearing_backing_work(blocker))
        await asyncio.sleep(0)
    else:
        leaf = cleanup_coroutine()
    chain = _custom_cleanup_chain(3, leaf)

    try:
        error = await _terminalize_observer_awaitable(chain[0])
        assert sum(owner.close_reads for owner in chain) == 0
        assert sum(owner.close_calls for owner in chain) == 0
        assert sum(owner.cancel_reads for owner in chain) == 0
        assert sum(owner.cancel_calls for owner in chain) == 0
        if isinstance(leaf, asyncio.Future):
            assert not leaf.done()
            assert not leaf.cancelled()
        else:
            assert iscoroutine(leaf)
            assert leaf.cr_frame is not None
            assert not cleanup_ran
        blocker.set()
        await settle()
        gc.collect()
        stderr = capsys.readouterr().err
        assert error.data is None
        for secret in (
            "BACKING_TASK_FAILURE_SECRET",
            "BOUNDARY_COROUTINE_FAILURE_SECRET",
        ):
            assert secret not in repr(reports)
            assert secret not in stderr
    finally:
        loop.set_exception_handler(previous_handler)
        if isinstance(leaf, asyncio.Future):
            if not leaf.done():
                leaf.cancel()
            await asyncio.gather(leaf, return_exceptions=True)
        elif iscoroutine(leaf):
            leaf.close()


async def test_notification_observer_never_starts_custom_cleanup_chain() -> None:
    chain = _custom_cleanup_chain(
        3,
        None,
    )

    error = await _terminalize_observer_awaitable(chain[0])

    assert sum(owner.close_reads for owner in chain) == 0
    assert sum(owner.close_calls for owner in chain) == 0
    assert sum(owner.cancel_reads for owner in chain) == 0
    assert sum(owner.cancel_calls for owner in chain) == 0
    assert all(not owner.await_started for owner in chain)
    assert chain[-1].close_calls == 0
    assert error.data is None


async def test_peer_notify_serializes_optional_params() -> None:
    harness = PeerHarness()
    await harness.peer.start()

    await harness.peer.notify("client/ready")
    await harness.peer.notify("client/status", {"ready": True})

    assert await harness.next_written() == {"method": "client/ready"}
    assert await harness.next_written() == {
        "method": "client/status",
        "params": {"ready": True},
    }
    await harness.peer.close()


async def test_peer_notifications_are_fanned_out_with_isolated_params(
    peer_harness: PeerHarness,
) -> None:
    first = peer_harness.peer.notifications()
    second = peer_harness.peer.notifications()

    await peer_harness.receive({"method": "event/ready", "params": {"nested": {"phase": "ready"}}})

    first_notification = await anext(first)
    first_nested = cast("dict[str, JsonValue]", first_notification.params["nested"])
    first_nested["phase"] = "mutated"
    assert await anext(second) == RpcNotification("event/ready", {"nested": {"phase": "ready"}})
    await cast(AsyncGenerator[RpcNotification], first).aclose()  # noqa: TC006
    await cast(AsyncGenerator[RpcNotification], second).aclose()  # noqa: TC006


async def test_slow_notification_subscriber_overflow_is_explicit_and_isolated(
    peer_harness: PeerHarness,
) -> None:
    slow = peer_harness.peer.notifications()
    fast = peer_harness.peer.notifications()
    slow = cast("_NotificationSubscription", slow)
    assert slow._queue.maxsize > 0  # noqa: SLF001

    for index in range(slow._queue.maxsize + 1):  # noqa: SLF001
        method = f"event/{index}"
        await peer_harness.receive({"method": method, "params": {"index": index}})
        assert await anext(fast) == RpcNotification(method, {"index": index})

    with pytest.raises(CodexRpcError, match="notification subscriber overflow") as overflow:
        await asyncio.wait_for(anext(slow), 0.1)
    assert overflow.value.data is None

    await peer_harness.receive({"method": "event/after-overflow", "params": {}})
    assert await anext(fast) == RpcNotification("event/after-overflow", {})
    outgoing = asyncio.create_task(peer_harness.peer.request("client/after-overflow"))
    sent = await peer_harness.next_written()
    await peer_harness.receive({"id": sent["id"], "result": {"ok": True}})
    assert await outgoing == {"ok": True}
    await cast(AsyncGenerator[RpcNotification], slow).aclose()  # noqa: TC006
    await cast(AsyncGenerator[RpcNotification], fast).aclose()  # noqa: TC006


async def test_peer_terminal_replaces_a_full_notification_backlog(
    peer_harness: PeerHarness,
) -> None:
    subscription = peer_harness.peer.notifications()
    subscription = cast("_NotificationSubscription", subscription)
    for index in range(subscription._queue.maxsize):  # noqa: SLF001
        await peer_harness.receive({"method": f"event/{index}", "params": {}})

    terminal = CodexRpcError("stable terminal")
    peer_harness.peer.abort(terminal)

    with pytest.raises(CodexRpcError) as caught:
        await asyncio.wait_for(anext(subscription), 0.1)
    assert caught.value is terminal


async def test_close_replaces_a_full_notification_backlog_with_end() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    subscription = harness.peer.notifications()
    subscription = cast("_NotificationSubscription", subscription)
    for index in range(subscription._queue.maxsize):  # noqa: SLF001
        await harness.receive({"method": f"event/{index}", "params": {}})

    await harness.peer.close()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription), 0.1)


async def test_unstarted_notification_subscription_aclose_unregisters(
    peer_harness: PeerHarness,
) -> None:
    subscription = peer_harness.peer.notifications()

    await cast(AsyncGenerator[RpcNotification], subscription).aclose()  # noqa: TC006
    await peer_harness.receive({"method": "event/after-close", "params": {}})

    assert not peer_harness.peer._subscribers  # noqa: SLF001


async def test_cancelled_notification_wait_unregisters_subscription(
    peer_harness: PeerHarness,
) -> None:
    subscription = peer_harness.peer.notifications()

    async def wait_for_notification() -> RpcNotification:
        return await anext(subscription)

    waiting = asyncio.create_task(wait_for_notification())
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert not peer_harness.peer._subscribers  # noqa: SLF001


async def test_abandoned_notification_subscription_is_not_retained(
    peer_harness: PeerHarness,
) -> None:
    subscription = peer_harness.peer.notifications()
    assert len(peer_harness.peer._subscribers) == 1  # noqa: SLF001

    del subscription
    gc.collect()
    await peer_harness.receive({"method": "event/after-gc", "params": {}})

    assert not peer_harness.peer._subscribers  # noqa: SLF001


async def test_unknown_server_request_returns_error_without_success(
    peer_harness: PeerHarness,
) -> None:
    await peer_harness.receive({"method": "future/unknown", "id": "server-1", "params": {}})

    response = await peer_harness.next_written()
    assert response == {
        "id": "server-1",
        "error": {"code": -32601, "message": "unsupported server request"},
    }
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)


async def test_server_request_handler_failure_is_redacted_and_sent_once() -> None:
    peer_harness = PeerHarness()

    async def fail(request: RpcServerRequest) -> JsonValue:
        del request
        message = "RPC_HANDLER_SECRET"
        raise RuntimeError(message)

    peer_harness.peer.register_server_request_handler("host/fail", fail)
    await peer_harness.peer.start()
    await peer_harness.receive({"method": "host/fail", "id": 4})

    assert await peer_harness.next_written() == {
        "id": 4,
        "error": {"code": -32603, "message": "server request handler failed"},
    }
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)
    await peer_harness.peer.close()


async def test_duplicate_server_request_id_cancels_handler_and_is_terminal() -> None:
    peer_harness = PeerHarness()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(request: RpcServerRequest) -> JsonValue:
        del request
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return None

    peer_harness.peer.register_server_request_handler("host/block", block)
    await peer_harness.peer.start()
    await peer_harness.receive({"method": "host/block", "id": "duplicate"})
    await started.wait()
    await peer_harness.receive({"method": "host/block", "id": "duplicate"})

    response = await peer_harness.next_written()
    assert response["id"] == "duplicate"
    assert response["error"] == {
        "code": -32600,
        "message": "duplicate server request id",
    }
    await asyncio.wait_for(cancelled.wait(), 1.0)
    with pytest.raises(CodexRpcProtocolError, match="duplicate"):
        await peer_harness.peer.request("client/after-duplicate")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)
    await peer_harness.peer.close()


async def test_duplicate_after_response_claim_sends_no_second_response() -> None:
    harness = PeerHarness()

    async def succeed(request: RpcServerRequest) -> JsonValue:
        del request
        return {"ok": True}

    harness.peer.register_server_request_handler("host/succeed", succeed)
    await harness.peer.start()
    harness.writer.drain_gate = asyncio.Event()
    harness.feed({"method": "host/succeed", "id": 12})
    assert await harness.next_written() == {"id": 12, "result": {"ok": True}}

    await harness.receive({"method": "host/succeed", "id": 12})

    with pytest.raises(CodexRpcProtocolError, match="duplicate"):
        await harness.peer.request("client/after-duplicate")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(harness.written.get(), 0.01)
    await harness.peer.close()


async def test_a_buffered_server_request_burst_bounds_inbound_tasks() -> None:
    """A burst that arrives before any handler runs must not become one task per message."""
    harness = PeerHarness()
    observed: list[int] = []

    async def block(request: RpcServerRequest) -> JsonValue:
        del request
        observed.append(active_incoming(harness.peer))
        await asyncio.Event().wait()
        return None

    harness.peer.register_server_request_handler("host/block", block)
    await harness.peer.start()
    for index in range(BURST):
        harness.feed({"method": "host/block", "id": index})
    refusals = [await harness.next_written() for _ in range(BURST - _MAX_ACTIVE_SERVER_REQUESTS)]

    assert active_incoming(harness.peer) == _MAX_ACTIVE_SERVER_REQUESTS
    assert len(observed) == _MAX_ACTIVE_SERVER_REQUESTS
    assert max(observed) == _MAX_ACTIVE_SERVER_REQUESTS
    for message in refusals:
        assert message["error"] == {"code": -32603, "message": "too many server requests"}
    assert [message["id"] for message in refusals] == list(
        range(_MAX_ACTIVE_SERVER_REQUESTS, BURST)
    )
    tasks = inbound_tasks(harness.peer)

    await harness.peer.close()
    await settle()

    assert active_incoming(harness.peer) == 0
    assert all(task.done() for task in tasks)
    assert harness.written.empty()


async def test_a_refused_server_request_id_is_free_for_a_later_request() -> None:
    """Refusing a request answers it, so its id is not held against a later one."""
    harness = PeerHarness()
    gate = asyncio.Event()

    async def serve(request: RpcServerRequest) -> JsonValue:
        del request
        await gate.wait()
        return {"ok": True}

    harness.peer.register_server_request_handler("host/serve", serve)
    await harness.peer.start()
    for index in range(_MAX_ACTIVE_SERVER_REQUESTS):
        harness.feed({"method": "host/serve", "id": index})
    harness.feed({"method": "host/serve", "id": "over-bound"})

    assert await harness.next_written() == {
        "id": "over-bound",
        "error": {"code": -32603, "message": "too many server requests"},
    }
    gate.set()
    for _ in range(_MAX_ACTIVE_SERVER_REQUESTS):
        await harness.next_written()
    await settle()

    assert active_incoming(harness.peer) == 0
    await harness.receive({"method": "host/serve", "id": "over-bound"})

    assert await harness.next_written() == {"id": "over-bound", "result": {"ok": True}}
    await harness.peer.notify("client/after-refusal")
    assert await harness.next_written() == {"method": "client/after-refusal"}
    await harness.peer.close()


async def test_malformed_server_request_gets_protocol_error_and_is_terminal(
    peer_harness: PeerHarness,
) -> None:
    await peer_harness.receive({"method": "host/bad", "id": 8, "result": {}})

    response = await peer_harness.next_written()
    assert response["id"] == 8
    assert cast("dict[str, JsonValue]", response["error"])["code"] == -32600
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)
    with pytest.raises(CodexRpcProtocolError):
        await peer_harness.peer.request("client/after-malformed")


async def test_malformed_server_request_is_terminal_before_response_drain() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    harness.writer.drain_gate = asyncio.Event()
    harness.feed({"method": "host/bad", "id": 9, "result": {}})
    assert (await harness.next_written())["id"] == 9

    with pytest.raises(CodexRpcProtocolError):
        await asyncio.wait_for(harness.peer.request("client/raced"), 0.01)

    harness.writer.drain_gate.set()
    await harness.peer.close()


async def test_malformed_request_does_not_reply_after_success_claim() -> None:
    harness = PeerHarness()

    async def succeed(request: RpcServerRequest) -> JsonValue:
        del request
        return {"ok": True}

    harness.peer.register_server_request_handler("host/succeed", succeed)
    await harness.peer.start()
    harness.writer.drain_gate = asyncio.Event()
    try:
        harness.feed({"method": "host/succeed", "id": 12})
        assert await harness.next_written() == {"id": 12, "result": {"ok": True}}

        await harness.receive({"method": 7, "id": 12})

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(harness.written.get(), 0.01)
        with pytest.raises(CodexRpcProtocolError):
            await harness.peer.request("client/after-malformed")
    finally:
        harness.writer.drain_gate.set()
        await harness.peer.close()


async def test_malformed_response_terminalizes_all_peer_work() -> None:
    harness = PeerHarness()
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def block(request: RpcServerRequest) -> JsonValue:
        del request
        handler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return None

    harness.peer.register_server_request_handler("host/block", block)
    await harness.peer.start()
    notifications = harness.peer.notifications()
    first = asyncio.create_task(harness.peer.request("client/first"))
    first_sent = await harness.next_written()
    second = asyncio.create_task(harness.peer.request("client/second"))
    await harness.next_written()
    await harness.receive({"method": "host/block", "id": "server-block"})
    await handler_started.wait()
    try:
        await harness.receive({"id": first_sent["id"]})

        with pytest.raises(CodexRpcProtocolError) as first_error:
            await first
        with pytest.raises(CodexRpcProtocolError) as second_error:
            await asyncio.wait_for(second, 0.05)
        with pytest.raises(CodexRpcProtocolError) as notification_error:
            await anext(notifications)
        await handler_cancelled.wait()
        assert second_error.value is first_error.value
        assert notification_error.value is first_error.value
        with pytest.raises(CodexRpcProtocolError) as sticky_error:
            await harness.peer.request("client/after-malformed")
        assert sticky_error.value is first_error.value
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(harness.written.get(), 0.01)
    finally:
        await harness.peer.close()


async def test_malformed_unknown_response_id_is_terminal(
    peer_harness: PeerHarness,
) -> None:
    notifications = peer_harness.peer.notifications()

    await peer_harness.receive({"id": "unknown-response"})

    with pytest.raises(CodexRpcProtocolError):
        await asyncio.wait_for(anext(notifications), 0.05)
    with pytest.raises(CodexRpcProtocolError):
        await peer_harness.peer.request("client/after-malformed")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)


async def test_unknown_integer_and_string_response_ids_are_ignored(
    peer_harness: PeerHarness,
) -> None:
    await peer_harness.receive({"id": 999, "result": "ignored"})
    await peer_harness.receive({"id": "999", "result": "ignored"})

    outgoing = asyncio.create_task(peer_harness.peer.request("client/ping"))
    sent = await peer_harness.next_written()
    await peer_harness.receive({"id": sent["id"], "result": "ok"})
    assert await outgoing == "ok"


async def test_request_without_params_omits_wire_field(
    peer_harness: PeerHarness,
) -> None:
    outgoing = asyncio.create_task(peer_harness.peer.request("future/requirements/read"))
    sent = await peer_harness.next_written()

    assert "params" not in sent

    await peer_harness.receive({"id": sent["id"], "result": {}})
    assert await outgoing == {}


async def test_abort_fails_pending_and_notification_subscribers_with_same_error(
    peer_harness: PeerHarness,
) -> None:
    notifications = peer_harness.peer.notifications()
    outgoing = asyncio.create_task(peer_harness.peer.request("client/pending"))
    await peer_harness.next_written()
    error = CodexProcessExitedError("connection lost")

    peer_harness.peer.abort(error)

    with pytest.raises(CodexProcessExitedError) as request_error:
        await outgoing
    with pytest.raises(CodexProcessExitedError) as notification_error:
        await anext(notifications)
    assert request_error.value is error
    assert notification_error.value is error
    with pytest.raises(CodexProcessExitedError) as sticky_error:
        await peer_harness.peer.request("client/late")
    assert sticky_error.value is error


async def test_abort_prevents_response_from_handler_suppressing_cancellation() -> None:
    harness = PeerHarness()
    started = asyncio.Event()
    suppressed = asyncio.Event()

    async def suppress_cancellation(request: RpcServerRequest) -> JsonValue:
        del request
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            suppressed.set()
            return {"late": True}
        return None

    harness.peer.register_server_request_handler("host/suppress", suppress_cancellation)
    await harness.peer.start()
    harness.feed({"method": "host/suppress", "id": 21})
    await started.wait()

    harness.peer.abort(CodexProcessExitedError("connection lost"))
    await suppressed.wait()
    await asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(harness.written.get(), 0.01)
    await harness.peer.close()


async def test_handler_closing_own_peer_cannot_send_success_after_close() -> None:
    harness = PeerHarness()
    close_returned = asyncio.Event()

    async def close_peer(request: RpcServerRequest) -> JsonValue:
        del request
        await harness.peer.close()
        close_returned.set()
        return {"late": True}

    harness.peer.register_server_request_handler("host/close", close_peer)
    await harness.peer.start()
    harness.feed({"method": "host/close", "id": 22})
    await close_returned.wait()
    await asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(harness.written.get(), 0.01)


async def test_close_does_not_wait_for_handler_suppressing_cancellation() -> None:
    harness = PeerHarness()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def suppress_cancellation(request: RpcServerRequest) -> JsonValue:
        del request
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return {"late": True}
        finally:
            finished.set()
        return None

    harness.peer.register_server_request_handler("host/stubborn", suppress_cancellation)
    await harness.peer.start()
    harness.feed({"method": "host/stubborn", "id": 23})
    await started.wait()
    try:
        await asyncio.wait_for(harness.peer.close(), 0.05)
        await cancelled.wait()
        assert not finished.is_set()
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 0.1)
    await asyncio.sleep(0)
    assert not harness.peer._incoming  # noqa: SLF001


async def test_close_ends_subscribers_and_fails_pending() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    outgoing = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()

    await harness.peer.close()
    await harness.peer.close()

    with pytest.raises(CodexProcessExitedError, match="closed"):
        await outgoing
    with pytest.raises(StopAsyncIteration):
        await anext(notifications)


async def test_reader_eof_is_sticky_and_reaches_subscribers() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    pending = asyncio.create_task(harness.peer.request("client/pending-at-eof"))
    await harness.next_written()

    harness.reader.feed_eof()
    with pytest.raises(CodexProcessExitedError, match="EOF") as pending_error:
        await pending
    with pytest.raises(CodexProcessExitedError, match="EOF") as subscriber_error:
        await anext(notifications)
    with pytest.raises(CodexProcessExitedError, match="EOF") as sticky_error:
        await harness.peer.request("client/after-eof")
    assert subscriber_error.value is pending_error.value
    assert sticky_error.value is pending_error.value
    await harness.peer.close()


@pytest.mark.parametrize(
    "invalid_line",
    [b'{"unterminated":\n', b'["non-object"]\n'],
    ids=["invalid-json", "non-object"],
)
async def test_invalid_json_shape_is_an_exact_sticky_protocol_error(
    invalid_line: bytes,
) -> None:
    harness = PeerHarness()
    await harness.peer.start()
    pending = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()
    harness.reader.feed_data(invalid_line)

    with pytest.raises(CodexRpcProtocolError) as first:
        await pending
    with pytest.raises(CodexRpcProtocolError) as sticky:
        await harness.peer.request("client/after-invalid")

    assert type(first.value) is CodexRpcProtocolError
    assert sticky.value is first.value
    await harness.peer.close()


async def test_read_transport_failure_is_an_exact_sticky_process_error() -> None:
    writer = _QueueWriter(asyncio.Queue())
    peer = RpcPeer(
        cast("asyncio.StreamReader", _ReadFailureReader()),
        cast("asyncio.StreamWriter", writer),
    )
    await peer.start()
    await asyncio.sleep(0)

    with pytest.raises(CodexProcessExitedError) as first:
        await peer.request("client/after-read-failure")
    with pytest.raises(CodexProcessExitedError) as sticky:
        await peer.request("client/still-terminal")

    assert type(first.value) is CodexProcessExitedError
    assert sticky.value is first.value
    assert "READ_TRANSPORT_SECRET" not in str(first.value)
    await peer.close()


async def test_oversized_line_is_a_sticky_redacted_terminal_error() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    pending = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()
    try:
        harness.reader.feed_data(b'{"secret":"' + b"x" * 70_000 + b'"}\n')

        with pytest.raises(CodexRpcError, match="oversized") as notification_error:
            await asyncio.wait_for(anext(notifications), 0.05)
        with pytest.raises(CodexRpcError) as pending_error:
            await pending
        with pytest.raises(CodexRpcError) as sticky_error:
            await harness.peer.request("client/after-oversized")
        assert pending_error.value is notification_error.value
        assert sticky_error.value is notification_error.value
        assert "secret" not in str(notification_error.value).lower()
        await asyncio.wait_for(harness.peer.close(), 0.05)
    finally:
        with suppress(Exception):
            await harness.peer.close()


async def test_deep_json_is_a_sticky_redacted_terminal_error() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    pending = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()
    deep_json = b"[" * 1_100 + b'"RPC_DEEP_SECRET"' + b"]" * 1_100 + b"\n"
    try:
        harness.reader.feed_data(deep_json)

        with pytest.raises(CodexRpcError, match="invalid JSON-RPC") as notification_error:
            await asyncio.wait_for(anext(notifications), 0.05)
        with pytest.raises(CodexRpcError) as pending_error:
            await pending
        with pytest.raises(CodexRpcError) as sticky_error:
            await harness.peer.request("client/after-deep-json")
        assert pending_error.value is notification_error.value
        assert sticky_error.value is notification_error.value
        assert "RPC_DEEP_SECRET" not in str(notification_error.value)
        await asyncio.wait_for(harness.peer.close(), 0.05)
    finally:
        with suppress(Exception):
            await harness.peer.close()


async def test_request_timeout_removes_pending_and_ignores_late_response() -> None:
    harness = PeerHarness(request_timeout=0.01)
    await harness.peer.start()

    timed_out = asyncio.create_task(harness.peer.request("client/slow"))
    await harness.next_written()
    with pytest.raises(CodexRpcTimeoutError):
        await timed_out
    harness.feed({"id": 1, "result": "late"})
    outgoing = asyncio.create_task(harness.peer.request("client/next"))
    sent = await harness.next_written()
    await harness.receive({"id": sent["id"], "result": "ok"})
    assert await outgoing == "ok"
    await harness.peer.close()


async def test_request_timeout_includes_blocked_writer_and_terminalizes_peer() -> None:
    harness = PeerHarness(request_timeout=0.01)
    await harness.peer.start()
    harness.writer.drain_gate = asyncio.Event()

    with pytest.raises(CodexRpcTimeoutError, match="write"):
        await harness.peer.request("client/blocked-write")
    assert harness.peer._pending == {}  # noqa: SLF001
    with pytest.raises(CodexRpcTimeoutError, match="write"):
        await harness.peer.request("client/after-blocked-write")

    harness.writer.drain_gate.set()
    await harness.peer.close()


async def test_request_timeout_includes_waiting_for_the_write_lock() -> None:
    harness = PeerHarness(request_timeout=0.1)
    await harness.peer.start()
    harness.writer.drain_gate = asyncio.Event()
    first_write = asyncio.create_task(harness.peer.notify("client/holding-write-lock"))
    await harness.next_written()

    with pytest.raises(CodexRpcTimeoutError, match="write") as timed_out:
        await harness.peer.request("client/waiting-for-write-lock", request_timeout=0.01)
    with pytest.raises(CodexRpcTimeoutError) as sticky:
        await harness.peer.request("client/after-write-lock-timeout")
    assert sticky.value is timed_out.value

    harness.writer.drain_gate.set()
    await first_write
    await harness.peer.close()


async def test_server_response_drain_is_bounded_and_terminalizes_peer() -> None:
    harness = PeerHarness(request_timeout=0.01)

    async def approve(_request: RpcServerRequest) -> JsonValue:
        return {"decision": "accept"}

    harness.peer.register_server_request_handler("host/approval", approve)
    await harness.peer.start()
    harness.writer.drain_gate = asyncio.Event()
    harness.feed({"method": "host/approval", "id": "review-1"})
    assert await harness.next_written() == {
        "id": "review-1",
        "result": {"decision": "accept"},
    }

    await asyncio.sleep(0.02)
    with pytest.raises(CodexRpcTimeoutError, match="write"):
        await harness.peer.request("client/after-blocked-approval-response")

    harness.writer.drain_gate.set()
    await harness.peer.close()


async def test_cancelled_request_removes_pending_and_ignores_late_response(
    peer_harness: PeerHarness,
) -> None:
    outgoing = asyncio.create_task(peer_harness.peer.request("client/cancel"))
    sent = await peer_harness.next_written()
    outgoing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outgoing

    await peer_harness.receive({"id": sent["id"], "result": "late"})
    next_request = asyncio.create_task(peer_harness.peer.request("client/next"))
    next_sent = await peer_harness.next_written()
    await peer_harness.receive({"id": next_sent["id"], "result": "ok"})
    assert await next_request == "ok"


async def test_writer_failure_is_sticky_and_fails_closed() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    harness.writer.error = BrokenPipeError("gone")

    with pytest.raises(CodexProcessExitedError, match="write") as first:
        await harness.peer.request("client/write")
    with pytest.raises(CodexProcessExitedError) as second:
        await harness.peer.request("client/after-write")
    assert second.value is first.value
    await harness.peer.close()


@pytest.mark.parametrize(
    "invalid_result",
    [_SensitiveInvalidJson(), float("nan"), float("inf")],
    ids=["object", "nan", "infinity"],
)
async def test_invalid_handler_result_returns_redacted_error_and_peer_survives(
    invalid_result: object,
) -> None:
    harness = PeerHarness()

    async def invalid(request: RpcServerRequest) -> JsonValue:
        del request
        return cast("JsonValue", invalid_result)

    harness.peer.register_server_request_handler("host/invalid", invalid)
    await harness.peer.start()
    harness.feed({"method": "host/invalid", "id": 31})

    response = await harness.next_written()
    assert response == {
        "id": 31,
        "error": {"code": -32603, "message": "server request handler failed"},
    }
    assert "JSON_SECRET_REPR" not in json.dumps(response)
    outgoing = asyncio.create_task(harness.peer.request("client/after-invalid"))
    sent = await harness.next_written()
    await harness.receive({"id": sent["id"], "result": "ok"})
    assert await outgoing == "ok"
    await harness.peer.close()


async def test_cyclic_handler_result_returns_redacted_error_and_peer_survives() -> None:
    harness = PeerHarness()
    cyclic_result: dict[str, object] = {}
    cyclic_result["RPC_CYCLE_SECRET"] = cyclic_result

    async def cyclic(request: RpcServerRequest) -> JsonValue:
        del request
        return cast("JsonValue", cyclic_result)

    harness.peer.register_server_request_handler("host/cyclic", cyclic)
    await harness.peer.start()
    harness.feed({"method": "host/cyclic", "id": 32})

    response = await harness.next_written()
    assert response == {
        "id": 32,
        "error": {"code": -32603, "message": "server request handler failed"},
    }
    assert "RPC_CYCLE_SECRET" not in json.dumps(response)
    outgoing = asyncio.create_task(harness.peer.request("client/after-cyclic"))
    sent = await harness.next_written()
    await harness.receive({"id": sent["id"], "result": "ok"})
    assert await outgoing == "ok"
    await harness.peer.close()


@pytest.mark.parametrize(
    ("operation", "invalid_value"),
    [("request", _SensitiveInvalidJson()), ("notify", float("nan"))],
)
async def test_invalid_client_params_are_local_errors_without_terminalizing_peer(
    peer_harness: PeerHarness,
    operation: str,
    invalid_value: object,
) -> None:
    params = cast(Mapping[str, JsonValue], {"value": invalid_value})  # noqa: TC006

    with pytest.raises(CodexRpcError, match=r"params.*valid JSON") as caught:
        await getattr(peer_harness.peer, operation)("client/invalid", params)

    assert "JSON_SECRET_REPR" not in str(caught.value)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)
    outgoing = asyncio.create_task(peer_harness.peer.request("client/after-invalid"))
    sent = await peer_harness.next_written()
    await peer_harness.receive({"id": sent["id"], "result": "ok"})
    assert await outgoing == "ok"


async def test_cyclic_client_params_do_not_allocate_pending_or_terminalize_peer(
    peer_harness: PeerHarness,
) -> None:
    cyclic_value: list[object] = []
    cyclic_value.append(cyclic_value)
    params = cast(
        Mapping[str, JsonValue],  # noqa: TC006
        {"RPC_CYCLE_SECRET": cyclic_value},
    )

    with pytest.raises(CodexRpcError, match=r"params.*valid JSON") as caught:
        await peer_harness.peer.request("client/cyclic", params)

    assert "RPC_CYCLE_SECRET" not in str(caught.value)
    assert not peer_harness.peer._pending  # noqa: SLF001
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(peer_harness.written.get(), 0.01)
    outgoing = asyncio.create_task(peer_harness.peer.request("client/after-cyclic"))
    sent = await peer_harness.next_written()
    await peer_harness.receive({"id": sent["id"], "result": "ok"})
    assert await outgoing == "ok"


async def test_handler_registration_after_start_is_rejected(
    peer_harness: PeerHarness,
) -> None:
    async def handler(request: RpcServerRequest) -> JsonValue:
        del request
        return None

    with pytest.raises(RuntimeError, match=r"before.*start"):
        peer_harness.peer.register_server_request_handler("host/late", handler)


async def test_terminal_callback_registration_after_start_is_rejected(
    peer_harness: PeerHarness,
) -> None:
    with pytest.raises(RuntimeError, match=r"before.*start"):
        peer_harness.peer.register_terminal_callback(lambda: None)


async def test_handler_registration_after_close_before_start_is_rejected() -> None:
    harness = PeerHarness()
    await harness.peer.close()

    async def handler(request: RpcServerRequest) -> JsonValue:
        del request
        return None

    with pytest.raises(
        RuntimeError,
        match=r"^server request handlers must be registered before peer start$",
    ):
        harness.peer.register_server_request_handler("host/late", handler)


async def test_terminal_callback_registration_after_close_before_start_is_rejected() -> None:
    harness = PeerHarness()
    ended: list[str] = []
    await harness.peer.close()

    with pytest.raises(
        RuntimeError,
        match=r"^terminal callbacks must be registered before peer start$",
    ):
        harness.peer.register_terminal_callback(lambda: ended.append("late"))

    with pytest.raises(CodexProcessExitedError, match="closed"):
        await harness.peer.start()
    assert ended == []


async def test_handler_registration_after_abort_before_start_is_rejected() -> None:
    harness = PeerHarness()
    harness.peer.abort(CodexRpcError("connection setup failed"))

    async def handler(request: RpcServerRequest) -> JsonValue:
        del request
        return None

    with pytest.raises(
        RuntimeError,
        match=r"^server request handlers must be registered before peer start$",
    ):
        harness.peer.register_server_request_handler("host/late", handler)


async def test_terminal_callback_registration_after_abort_before_start_is_rejected() -> None:
    harness = PeerHarness()
    ended: list[str] = []
    error = CodexRpcError("connection setup failed")
    harness.peer.abort(error)

    with pytest.raises(
        RuntimeError,
        match=r"^terminal callbacks must be registered before peer start$",
    ):
        harness.peer.register_terminal_callback(lambda: ended.append("late"))

    with pytest.raises(CodexRpcError) as caught:
        await harness.peer.start()
    assert caught.value is error
    assert ended == []


async def test_eof_runs_every_terminal_callback_exactly_once() -> None:
    """The peer ends for its own reasons, so whoever depends on it is told by the peer."""
    harness = PeerHarness()
    ended: list[str] = []
    harness.peer.register_terminal_callback(lambda: ended.append("first"))
    harness.peer.register_terminal_callback(lambda: ended.append("second"))
    await harness.peer.start()

    harness.reader.feed_eof()
    await settle()

    assert ended == ["first", "second"]
    await harness.peer.close()
    assert ended == ["first", "second"]


async def test_close_runs_terminal_callbacks_once() -> None:
    harness = PeerHarness()
    ended: list[str] = []
    harness.peer.register_terminal_callback(lambda: ended.append("closed"))
    await harness.peer.start()

    await harness.peer.close()
    await harness.peer.close()

    assert ended == ["closed"]


async def test_a_failing_terminal_callback_is_contained_and_stays_unquoted() -> None:
    harness = PeerHarness()
    ended: list[str] = []

    def explode() -> None:
        message = "RPC_CALLBACK_SECRET"
        raise RuntimeError(message)

    harness.peer.register_terminal_callback(explode)
    harness.peer.register_terminal_callback(lambda: ended.append("after"))
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()

    harness.reader.feed_eof()
    await settle()

    assert ended == ["after"]
    with pytest.raises(CodexProcessExitedError) as caught:
        await outgoing
    assert "RPC_CALLBACK_SECRET" not in str(caught.value)
    await harness.peer.close()
    with pytest.raises(CodexProcessExitedError):
        await harness.peer.notify("client/after-close")


async def test_cancelled_terminal_callback_is_contained_and_later_callbacks_run() -> None:
    harness = PeerHarness()
    ended: list[str] = []

    def cancel() -> None:
        ended.append("cancelled")
        raise asyncio.CancelledError

    harness.peer.register_terminal_callback(cancel)
    harness.peer.register_terminal_callback(lambda: ended.append("after"))
    await harness.peer.start()

    harness.reader.feed_eof()
    await settle()

    assert ended == ["cancelled", "after"]
    await harness.peer.close()


async def test_peer_round_trip_preserves_nested_json_params() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/ping", {"nested": {"value": 3}}))

    sent = await harness.next_written()
    assert sent == {
        "id": 1,
        "method": "client/ping",
        "params": {"nested": {"value": 3}},
    }
    await harness.receive({"id": sent["id"], "result": {"value": 3}})
    assert await outgoing == {"value": 3}
    await harness.peer.close()


async def test_peer_correlates_response_with_interleaved_notification() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    outgoing = asyncio.create_task(harness.peer.request("client/ping", {"value": 7}))
    sent = await harness.next_written()

    await harness.receive({"method": "event/interleaved", "params": {"value": 7}})
    await harness.receive({"id": sent["id"], "result": {"value": 7}})

    assert await outgoing == {"value": 7}
    assert await anext(notifications) == RpcNotification(
        "event/interleaved",
        {"value": 7},
    )
    await harness.peer.close()


async def test_correlates_concurrent_responses_received_out_of_order() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    first = asyncio.create_task(harness.peer.request("client/first"))
    first_sent = await harness.next_written()
    second = asyncio.create_task(harness.peer.request("client/second"))
    second_sent = await harness.next_written()

    await harness.receive({"id": second_sent["id"], "result": "second"})
    await harness.receive({"id": first_sent["id"], "result": "first"})

    assert await second == "second"
    assert await first == "first"
    await harness.peer.close()


async def test_converts_server_error_to_domain_error() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/error"))
    sent = await harness.next_written()
    await harness.receive(
        {
            "id": sent["id"],
            "error": {
                "code": -32000,
                "message": "realtime unavailable",
                "data": {"retryable": False},
            },
        }
    )

    with pytest.raises(CodexRpcError, match="realtime unavailable") as caught:
        await outgoing
    assert caught.value.code == -32000
    assert caught.value.data == {"retryable": False}
    await harness.peer.close()


async def test_timeout_does_not_poison_connection() -> None:
    harness = PeerHarness(request_timeout=0.01)
    await harness.peer.start()
    timed_out = asyncio.create_task(harness.peer.request("client/never"))
    await harness.next_written()
    with pytest.raises(CodexRpcTimeoutError, match="client/never"):
        await timed_out

    outgoing = asyncio.create_task(harness.peer.request("client/ping"))
    sent = await harness.next_written()
    await harness.receive({"id": sent["id"], "result": {"value": 9}})
    assert await outgoing == {"value": 9}
    await harness.peer.close()


async def test_malformed_server_message_fails_request() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()
    harness.reader.feed_data(b'{"broken":\n')

    with pytest.raises(CodexRpcProtocolError, match="invalid JSON") as caught:
        await outgoing
    with pytest.raises(CodexRpcProtocolError) as sticky:
        await harness.peer.request("client/after-malformed")
    assert sticky.value is caught.value
    await harness.peer.close()


async def test_peer_abort_error_is_sticky() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/pending"))
    await harness.next_written()
    error = CodexProcessExitedError("supervisor reported process exit", returncode=23)

    harness.peer.abort(error)

    with pytest.raises(CodexProcessExitedError) as pending_error:
        await outgoing
    with pytest.raises(CodexProcessExitedError) as sticky_error:
        await harness.peer.request("client/after-abort")
    assert pending_error.value is error
    assert sticky_error.value is error
    assert sticky_error.value.returncode == 23
    await harness.peer.close()


async def test_peer_close_is_idempotent_and_terminal() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()

    await harness.peer.close()
    await harness.peer.close()

    with pytest.raises(CodexProcessExitedError, match="closed"):
        await harness.peer.request("client/after-close")
    with pytest.raises(StopAsyncIteration):
        await anext(notifications)


async def test_peer_writer_failure_redacts_transport_detail() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    harness.writer.error = OSError("RPC_WRITE_SECRET")

    with pytest.raises(CodexProcessExitedError, match="write failed") as caught:
        await harness.peer.notify("client/status")

    assert "RPC_WRITE_SECRET" not in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    await harness.peer.close()


@pytest.mark.parametrize("request_timeout", [0.0, -1.0])
async def test_peer_rejects_non_positive_default_request_timeout(
    request_timeout: float,
) -> None:
    harness = PeerHarness()

    with pytest.raises(ValueError, match="request_timeout must be positive"):
        RpcPeer(
            harness.reader,
            cast("asyncio.StreamWriter", harness.writer),
            request_timeout=request_timeout,
        )


@pytest.mark.parametrize("operation", ["request", "notify"])
async def test_rejects_operations_before_start(operation: str) -> None:
    harness = PeerHarness()

    with pytest.raises(CodexProcessExitedError, match="has not been started"):
        await getattr(harness.peer, operation)("client/ping")
    assert harness.peer._reader_task is None  # noqa: SLF001


@pytest.mark.parametrize("operation", ["request", "notify"])
async def test_rejects_operations_after_close(operation: str) -> None:
    harness = PeerHarness()
    await harness.peer.start()
    await harness.peer.close()

    with pytest.raises(CodexProcessExitedError, match="closed"):
        await getattr(harness.peer, operation)("client/ping")
    assert harness.peer._terminal_error is not None  # noqa: SLF001


async def test_peer_duplicate_start_reuses_reader_task() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    reader_task = harness.peer._reader_task  # noqa: SLF001

    await harness.peer.start()

    assert harness.peer._reader_task is reader_task  # noqa: SLF001
    assert reader_task is not None
    assert not reader_task.done()
    await harness.peer.close()


async def test_peer_abort_before_start_prevents_later_start() -> None:
    harness = PeerHarness()
    error = CodexRpcError("connection setup failed")

    harness.peer.abort(error)

    with pytest.raises(CodexRpcError) as caught:
        await harness.peer.start()
    assert caught.value is error
    assert harness.peer._reader_task is None  # noqa: SLF001
    await harness.peer.close()


async def test_rejects_non_positive_request_timeout(
    peer_harness: PeerHarness,
) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        await peer_harness.peer.request("client/never", request_timeout=0)
    assert not peer_harness.peer._pending  # noqa: SLF001


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (b'["non-object"]\n', "non-object"),
        (b'{"value":NaN}\n', "invalid JSON"),
        (b'{"id":true,"result":null}\n', "invalid id"),
        (b'{"id":1}\n', "without result"),
        (b'{"params":{}}\n', "without a method"),
        (b'{"method":"event","params":[]}\n', "invalid params"),
    ],
)
async def test_protocol_violations_are_terminal(
    message: bytes,
    expected: str,
) -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    harness.reader.feed_data(message)

    with pytest.raises(CodexRpcProtocolError, match=expected) as caught:
        await anext(notifications)
    with pytest.raises(CodexRpcProtocolError) as sticky:
        await harness.peer.request("client/after-protocol-error")
    assert sticky.value is caught.value
    await harness.peer.close()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("not-an-object", "invalid RPC error"),
        ({"code": -1}, "invalid RPC error message"),
        ({"code": "bad", "message": "bad code"}, "bad code"),
    ],
)
async def test_normalizes_invalid_error_shapes(
    error: JsonValue,
    message: str,
) -> None:
    harness = PeerHarness()
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/error"))
    sent = await harness.next_written()
    await harness.receive({"id": sent["id"], "error": error})

    with pytest.raises(CodexRpcError, match=message) as caught:
        await outgoing
    assert caught.value.code is None
    assert not harness.peer._pending  # noqa: SLF001
    await harness.peer.close()


async def test_unknown_response_and_default_notification_params() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    await harness.receive({"id": 999, "result": "ignored"})
    await harness.receive({"method": "event/default-params"})

    outgoing = asyncio.create_task(harness.peer.request("client/ping"))
    sent = await harness.next_written()
    await harness.receive({"id": sent["id"], "result": {"accepted": True}})

    assert await outgoing == {"accepted": True}
    assert await anext(notifications) == RpcNotification("event/default-params", {})
    await harness.peer.close()


async def test_notify_sends_params() -> None:
    harness = PeerHarness()
    await harness.peer.start()

    await harness.peer.notify("client/status", {"ready": True})

    assert await harness.next_written() == {
        "method": "client/status",
        "params": {"ready": True},
    }
    assert not harness.peer._pending  # noqa: SLF001
    await harness.peer.close()


async def test_notification_iterator_ends_after_close() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    notifications = harness.peer.notifications()
    await harness.receive({"method": "event/ready"})
    assert await anext(notifications) == RpcNotification("event/ready", {})

    await harness.peer.close()

    with pytest.raises(StopAsyncIteration):
        await anext(notifications)


def test_json_value_validation() -> None:
    value = {"items": [None, True, 1, 1.5, "text"]}
    assert _as_json_value(value) == value
    with pytest.raises(CodexRpcError, match="non-string key"):
        _as_json_value({1: "value"})
    with pytest.raises(CodexRpcError, match="unsupported"):
        _as_json_value(object())
    for nonfinite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(CodexRpcError, match="non-finite"):
            _as_json_value(nonfinite)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            {"method": "server/do", "id": 7, "params": {}},
            RpcServerRequest(7, "server/do", {}),
        ),
        (
            {"method": "server/do", "id": "req-7", "params": {}},
            RpcServerRequest("req-7", "server/do", {}),
        ),
        ({"method": "event/done", "params": {}}, RpcNotification("event/done", {})),
        ({"id": 7, "result": None}, RpcSuccess(7, None)),
        (
            {"id": 7, "error": {"code": -1, "message": "failed"}},
            RpcFailure(7, {"code": -1, "message": "failed"}),
        ),
    ],
)
def test_classifies_wire_messages_exclusively(
    message: dict[str, JsonValue],
    expected: object,
) -> None:
    assert _classify_message(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"method": "server/do", "id": 7}, RpcServerRequest(7, "server/do", {})),
        ({"method": "event/done"}, RpcNotification("event/done", {})),
    ],
)
def test_classifies_wire_messages_with_default_params(
    message: dict[str, JsonValue],
    expected: object,
) -> None:
    assert _classify_message(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        {"method": "bad", "id": True, "params": {}},
        {"method": "bad", "id": 1, "result": {}},
        {"id": 1, "result": {}, "error": {}},
        {"id": 1},
        {"params": {}},
        {"method": 7},
        {"method": "bad", "params": []},
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


@pytest.mark.parametrize(
    "message",
    [
        {"method": "bad", "id": 7, "result": {}},
        {"method": 7, "id": "server-7"},
        {"method": "bad", "id": 7, "params": []},
    ],
)
def test_malformed_server_request_preserves_server_request_id(
    message: dict[str, JsonValue],
) -> None:
    with pytest.raises(CodexRpcProtocolError) as caught:
        _classify_message(message)

    assert caught.value.client_response_id is None
    assert caught.value.server_request_id == message["id"]


async def test_peer_routes_server_request_to_registered_handler() -> None:
    harness = PeerHarness()

    async def handle(request: RpcServerRequest) -> JsonValue:
        assert request.request_id == "server-7"
        assert request.params == {"allowed": True}
        return {"accepted": True}

    harness.peer.register_server_request_handler("server/do", handle)
    await harness.peer.start()
    await harness.receive(
        {
            "method": "server/do",
            "id": "server-7",
            "params": {"allowed": True},
        }
    )

    assert await harness.next_written() == {
        "id": "server-7",
        "result": {"accepted": True},
    }
    assert harness.peer._terminal_error is None  # noqa: SLF001
    await harness.peer.close()


async def test_string_response_id_does_not_consume_integer_pending_request() -> None:
    harness = PeerHarness()
    await harness.peer.start()
    outgoing = asyncio.create_task(harness.peer.request("client/ping"))
    sent = await harness.next_written()
    assert sent["id"] == 1

    await harness.receive({"id": "1", "result": {"ignored": True}})
    assert not outgoing.done()

    await harness.receive({"id": 1, "result": None})
    assert await outgoing is None
    await harness.peer.close()
