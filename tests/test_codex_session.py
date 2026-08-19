from __future__ import annotations

import asyncio
import gc
import logging
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast

import pytest

from moco.codex import session as session_module
from moco.codex.capabilities import (
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
)
from moco.codex.rpc import JsonValue, RpcNotification
from moco.codex.schema import (
    ClientMethodContract,
    CodexProtocolContract,
    ParamsKind,
    SemanticMethod,
)
from moco.codex.session import (
    DEFAULT_REALTIME_PROMPT,
    ActivityEvent,
    CodexConnection,
    CodexRealtimeSession,
    RealtimeErrorEvent,
    ReasoningSummaryEvent,
    TranscriptEvent,
)
from moco.config import AgentProfileMode, AgentSettings, CodexSettings, MocoSettings
from moco.errors import (
    CodexCapabilityError,
    CodexPromptError,
    CodexRpcError,
    CodexRpcTimeoutError,
)

_QUEUE_END = object()


class SecondaryCloseError(RuntimeError):
    """Synthetic secondary cleanup failure."""


class FakeRpc:
    def __init__(
        self,
        event_log: list[str] | None = None,
        *,
        thread_start_method: str = "thread/start",
        realtime_start_method: str = "thread/realtime/start",
    ) -> None:
        self.requests: list[tuple[str, dict[str, JsonValue]]] = []
        self.event_log = event_log
        self.thread_start_method = thread_start_method
        self.realtime_start_method = realtime_start_method
        self.started = False
        self.closed = False
        self.thread_result: JsonValue = {"thread": {"id": "thr_test"}}
        self.emit_sdp = True
        self.fail_method: str | None = None
        self.close_error: Exception | None = None
        self.close_calls = 0
        self.notification_error: CodexRpcError | None = None
        self._notifications: asyncio.Queue[RpcNotification | object] = asyncio.Queue()

    async def start(self) -> None:
        if self.event_log is not None:
            self.event_log.append("connection.start")
        self.started = True

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        del request_timeout
        copied = dict(params or {})
        self.requests.append((method, copied))
        if self.event_log is not None:
            self.event_log.append(method)
        if method == self.fail_method:
            message = "forced failure"
            raise CodexRpcError(message)
        if method == self.thread_start_method:
            return self.thread_result
        if method == self.realtime_start_method:
            if self.emit_sdp:
                await self.emit(
                    "thread/realtime/sdp",
                    {"threadId": copied["threadId"], "sdp": "answer-sdp"},
                )
            return {}
        if method == "thread/realtime/stop":
            return {}
        if method == "turn/interrupt":
            return {}
        msg = f"unexpected request: {method}"
        raise AssertionError(msg)

    def notifications(self) -> AsyncIterator[RpcNotification]:
        if self.event_log is not None:
            self.event_log.append("notifications")
        return self._iter_notifications()

    async def _iter_notifications(self) -> AsyncIterator[RpcNotification]:
        if self.notification_error is not None:
            raise self.notification_error
        while True:
            item = await self._notifications.get()
            if item is _QUEUE_END:
                return
            yield cast("RpcNotification", item)

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
        await self._notifications.put(_QUEUE_END)

    async def emit(self, method: str, params: dict[str, JsonValue]) -> None:
        await self._notifications.put(RpcNotification(method=method, params=params))


class CancellationPhaseRpc(FakeRpc):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.phase_started = asyncio.Event()
        self.phase_release = asyncio.Event()

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        if self.phase == "stop" and method == "thread/realtime/stop":
            self.requests.append((method, dict(params or {})))
            self.phase_started.set()
            await self.phase_release.wait()
        return await super().request(method, params, request_timeout=request_timeout)

    async def _iter_notifications(self) -> AsyncIterator[RpcNotification]:
        try:
            async for notification in super()._iter_notifications():
                yield notification
        except asyncio.CancelledError:
            if self.phase in {"connection", "notification"}:
                self.phase_started.set()
                await self.phase_release.wait()
            raise


class BlockingNotificationCleanupRpc(FakeRpc):
    def __init__(self) -> None:
        super().__init__()
        self.notification_cancel_started = asyncio.Event()
        self.notification_release = asyncio.Event()

    async def _iter_notifications(self) -> AsyncIterator[RpcNotification]:
        try:
            async for notification in super()._iter_notifications():
                yield notification
        except asyncio.CancelledError:
            self.notification_cancel_started.set()
            await self.notification_release.wait()
            raise


class StartupPrimaryAfterNotificationRpc(FakeRpc):
    def __init__(self, primary: CodexRpcError, secondary: CodexRpcError) -> None:
        super().__init__()
        self.primary = primary
        self.notification_error = secondary

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        if method == "thread/start":
            self.requests.append((method, dict(params or {})))
            await asyncio.sleep(0)
            raise self.primary
        return await super().request(method, params, request_timeout=request_timeout)


def make_snapshot(
    *,
    account: CapabilityState | None = None,
    realtime: CapabilityState | None = None,
    agent_admission: CapabilityState | None = None,
) -> CapabilitySnapshot:
    available = CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    return CapabilitySnapshot(
        version="codex-fixture",
        account=account or available,
        effective_policy=None,
        policy_state=available,
        managed_requirements=available,
        agent_admission=agent_admission or available,
        realtime=realtime or available,
        interrupt=available,
        steer=available,
        server_requests=available,
        server_request_categories=frozenset(),
        has_unclassified_server_requests=False,
    )


class BorrowedOnlyRpc:
    """A connection that exposes only the borrowed session surface."""

    def __init__(self) -> None:
        self.connection = FakeRpc()

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        return await self.connection.request(method, params, request_timeout=request_timeout)

    def notifications(self) -> AsyncIterator[RpcNotification]:
        return self.connection.notifications()


class StartCancellationRpc(FakeRpc):
    def __init__(self) -> None:
        super().__init__()
        self.thread_started = asyncio.Event()
        self.release = asyncio.Event()

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        if method == "thread/start":
            self.thread_started.set()
            await self.release.wait()
        return await super().request(method, params, request_timeout=request_timeout)


def make_settings(tmp_path: Path) -> MocoSettings:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(DEFAULT_REALTIME_PROMPT, encoding="utf-8")
    return MocoSettings(
        codex=CodexSettings(
            command=(str(tmp_path / "unused-codex"),),
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )


def make_voice_contract(
    *,
    thread_start: str = "thread/start",
    realtime_start: str = "thread/realtime/start",
) -> CodexProtocolContract:
    return CodexProtocolContract(
        version="codex-fixture",
        methods={
            SemanticMethod.THREAD_START: ClientMethodContract(
                thread_start,
                ParamsKind.OBJECT,
                frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
            ),
            SemanticMethod.THREAD_REALTIME_START: ClientMethodContract(
                realtime_start,
                ParamsKind.OBJECT,
                frozenset(
                    {
                        "includeStartupContext",
                        "clientManagedHandoffs",
                        "codexResponseHandoffMode",
                        "codexResponsesAsItems",
                        "delegationAckFiller",
                        "outputModality",
                        "prompt",
                        "threadId",
                        "transport",
                        "version",
                    }
                ),
            ),
            SemanticMethod.TURN_INTERRUPT: ClientMethodContract(
                "turn/interrupt",
                ParamsKind.OBJECT,
                frozenset({"threadId", "turnId"}),
            ),
        },
        server_requests={},
        unclassified_server_request_count=0,
        experimental_schema=True,
    )


def make_session(  # noqa: PLR0913
    connection: CodexConnection,
    *,
    settings: MocoSettings,
    capabilities: CapabilitySnapshot,
    contract: CodexProtocolContract | None = None,
    working_directory: Path | None = None,
    prompt: str | None = None,
    existing_thread_id: str | None = None,
    existing_active_turn_id: str | None = None,
    sdp_timeout: float = 10.0,
) -> CodexRealtimeSession:
    return CodexRealtimeSession(
        connection,
        contract=contract or make_voice_contract(),
        settings=settings,
        capabilities=capabilities,
        working_directory=working_directory,
        prompt=prompt,
        existing_thread_id=existing_thread_id,
        existing_active_turn_id=existing_active_turn_id,
        sdp_timeout=sdp_timeout,
    )


async def test_discovers_once_before_notification_subscription_and_thread_start(
    tmp_path: Path,
) -> None:
    """The borrower subscribes before its thread requests and never starts the connection."""
    event_log: list[str] = []
    rpc = FakeRpc(event_log)
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )

    assert await session.start("offer-sdp") == "answer-sdp"
    assert event_log[:3] == [
        "notifications",
        "thread/start",
        "thread/realtime/start",
    ]
    assert "connection.start" not in event_log
    await session.close()


async def test_borrowed_connection_is_never_started_or_closed(tmp_path: Path) -> None:
    """A successful conversation leaves the borrowed connection lifecycle untouched."""
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )

    assert await session.start("offer-sdp") == "answer-sdp"
    assert rpc.started is False
    assert rpc.close_calls == 0

    await session.close()

    assert session.closed
    assert rpc.started is False
    assert rpc.close_calls == 0


async def test_borrowed_session_needs_no_connection_lifecycle_methods(tmp_path: Path) -> None:
    """The session-facing connection protocol no longer requires start or close."""
    rpc = BorrowedOnlyRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )

    assert await session.start("offer-sdp") == "answer-sdp"

    await session.close()

    assert session.closed
    assert [method for method, _params in rpc.connection.requests] == [
        "thread/start",
        "thread/realtime/start",
        "thread/realtime/stop",
    ]


@pytest.mark.parametrize(
    ("fail_method", "notification_error", "message"),
    [
        ("thread/start", None, "forced failure"),
        ("thread/realtime/start", None, "forced failure"),
        (None, CodexRpcError("stream failed"), "stream failed"),
    ],
    ids=["thread-start", "realtime-start", "notification-stream"],
)
async def test_borrowed_connection_survives_startup_and_notification_failure(
    tmp_path: Path,
    fail_method: str | None,
    notification_error: CodexRpcError | None,
    message: str,
) -> None:
    """Startup and notification failures never close the borrowed connection."""
    rpc = FakeRpc()
    rpc.fail_method = fail_method
    rpc.notification_error = notification_error
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
        sdp_timeout=0.05,
    )

    with pytest.raises(CodexRpcError, match=message):
        await session.start("offer-sdp")

    assert session.closed
    assert rpc.started is False
    assert rpc.close_calls == 0


async def test_borrowed_connection_survives_close_cancellation(tmp_path: Path) -> None:
    """Cancelled cleanup finalizes the borrower without closing the connection."""
    rpc = CancellationPhaseRpc("stop")
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    close_task = asyncio.create_task(session.close())
    await asyncio.wait_for(rpc.phase_started.wait(), 0.5)

    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    close_task.cancel()
    rpc.phase_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert session.closed
    assert rpc.close_calls == 0


async def test_borrowed_connection_survives_start_cancellation(tmp_path: Path) -> None:
    """Cancellation during Voice startup never starts or closes the borrowed connection."""
    rpc = StartCancellationRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    start_task = asyncio.create_task(session.start("offer-sdp"))
    await asyncio.wait_for(rpc.thread_started.wait(), 0.5)

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert session.closed
    assert rpc.close_calls == 0
    assert rpc.started is False


async def test_voice_allows_unsafe_agent_policy_when_realtime_is_ready(
    tmp_path: Path,
) -> None:
    blocked = CapabilityState(CapabilityStatus.DISABLED, "unsafe_voice_policy")
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(agent_admission=blocked),
    )

    assert await session.start("offer-sdp") == "answer-sdp"
    assert rpc.requests[0][0] == "thread/start"
    await session.close()


@pytest.mark.parametrize("field", ["account", "realtime"])
async def test_voice_rejects_required_readiness_failure(
    tmp_path: Path,
    field: str,
) -> None:
    """Readiness fails closed before any subscription, request, or connection close."""
    unavailable = CapabilityState(CapabilityStatus.ERROR, "unavailable")
    snapshot = make_snapshot(**{field: unavailable})
    event_log: list[str] = []
    rpc = FakeRpc(event_log)
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=snapshot,
    )

    with pytest.raises(CodexCapabilityError, match="Voice readiness"):
        await session.start("offer-sdp")

    assert event_log == []
    assert rpc.requests == []
    assert rpc.close_calls == 0


@pytest.mark.parametrize(
    "snapshot",
    [
        cast("CapabilitySnapshot", object()),
        make_snapshot(account=cast("CapabilityState", object())),
        make_snapshot(
            realtime=CapabilityState(
                cast("CapabilityStatus", "private-invalid-status"),
                "private-invalid-detail",
            )
        ),
    ],
    ids=["not-snapshot", "invalid-required-state", "invalid-required-status"],
)
async def test_invalid_discovery_snapshot_is_bounded_before_thread_start(
    tmp_path: Path,
    snapshot: CapabilitySnapshot,
) -> None:
    """An unusable snapshot fails closed before subscription and leaves the connection open."""
    event_log: list[str] = []
    rpc = FakeRpc(event_log)
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=snapshot,
    )

    with pytest.raises(CodexCapabilityError, match="snapshot is invalid") as caught:
        await session.start("offer-sdp")

    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert session.thread_id is None
    assert event_log == []
    assert rpc.requests == []
    assert rpc.close_calls == 0


async def test_resolves_default_working_directory_once_at_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = tmp_path / "initial"
    later = tmp_path / "later"
    initial.mkdir()
    later.mkdir()
    monkeypatch.chdir(initial)
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=MocoSettings(),
        capabilities=make_snapshot(),
    )
    monkeypatch.chdir(later)

    await session.start("offer-sdp")

    assert rpc.requests[0][1]["cwd"] == str(initial)
    await session.close()


def test_rejects_relative_working_directory_override_before_connection_start(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()

    with pytest.raises(ValueError, match="absolute"):
        make_session(
            rpc,
            settings=make_settings(tmp_path),
            capabilities=make_snapshot(),
            working_directory=Path("relative-workspace"),
        )

    assert not rpc.started
    assert rpc.requests == []


async def _started_prompt(rpc: FakeRpc, settings: MocoSettings) -> str:
    session = make_session(
        rpc,
        settings=settings,
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    prompt = cast("str", rpc.requests[1][1]["prompt"])
    await session.close()
    return prompt


async def test_starts_ephemeral_read_only_audio_v3_session(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )

    answer = await session.start("offer-sdp")

    assert answer == "answer-sdp"
    assert rpc.requests[:2] == [
        (
            "thread/start",
            {
                "ephemeral": True,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "cwd": str(tmp_path),
            },
        ),
        (
            "thread/realtime/start",
            {
                "threadId": "thr_test",
                "clientManagedHandoffs": False,
                "delegationAckFiller": True,
                "codexResponsesAsItems": False,
                "codexResponseHandoffMode": "bemTags",
                "outputModality": "audio",
                "includeStartupContext": False,
                "prompt": DEFAULT_REALTIME_PROMPT,
                "transport": {"type": "webrtc", "sdp": "offer-sdp"},
                "version": "v3",
            },
        ),
    ]
    await session.close()


async def test_starts_realtime_thread_with_workspace_write_profile(tmp_path: Path) -> None:
    rpc = FakeRpc()
    settings = make_settings(tmp_path).model_copy(
        update={"agent": AgentSettings(profile=AgentProfileMode.WORKSPACE_WRITE)}
    )
    session = make_session(
        rpc,
        settings=settings,
        capabilities=make_snapshot(),
    )

    await session.start("offer-sdp")

    assert rpc.requests[0] == (
        "thread/start",
        {
            "ephemeral": True,
            "sandbox": "workspace-write",
            "approvalPolicy": "on-request",
            "cwd": str(tmp_path),
        },
    )
    await session.close()


async def test_starts_realtime_thread_with_inherited_codex_profile(tmp_path: Path) -> None:
    rpc = FakeRpc()
    settings = make_settings(tmp_path).model_copy(
        update={"agent": AgentSettings(profile=AgentProfileMode.INHERIT_CODEX)}
    )
    session = make_session(
        rpc,
        settings=settings,
        capabilities=make_snapshot(),
    )

    await session.start("offer-sdp")

    assert rpc.requests[0] == (
        "thread/start",
        {"ephemeral": True, "cwd": str(tmp_path)},
    )
    await session.close()


async def test_reoffer_reuses_existing_realtime_thread_without_starting_another(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
        existing_thread_id="thr_existing",
    )

    await session.start("offer-sdp")

    assert rpc.requests[0] == (
        "thread/realtime/start",
        {
            "threadId": "thr_existing",
            "clientManagedHandoffs": False,
            "delegationAckFiller": True,
            "codexResponsesAsItems": False,
            "codexResponseHandoffMode": "bemTags",
            "outputModality": "audio",
            "includeStartupContext": False,
            "prompt": DEFAULT_REALTIME_PROMPT,
            "transport": {"type": "webrtc", "sdp": "offer-sdp"},
            "version": "v3",
        },
    )
    assert all(method != "thread/start" for method, _params in rpc.requests)
    await session.close()


async def test_reoffer_retains_and_interrupts_existing_active_turn(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
        existing_thread_id="thr_existing",
        existing_active_turn_id="turn_existing",
    )

    assert session.owns_active_turn("thr_existing", "turn_existing")
    await session.start("offer-sdp")
    assert await session.interrupt_active_turn()
    assert rpc.requests[-1] == (
        "turn/interrupt",
        {"threadId": "thr_existing", "turnId": "turn_existing"},
    )

    events = session.notifications()
    await rpc.emit(
        "turn/completed",
        {"threadId": "thr_existing", "turn": {"id": "turn_existing"}},
    )
    assert await anext(events) == ActivityEvent(
        "turn",
        "completed",
        "thr_existing",
        "turn_existing",
        None,
    )
    assert session.active_turn_id is None
    await session.close()


def test_reoffer_rejects_active_turn_without_existing_thread(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="active turn"):
        make_session(
            FakeRpc(),
            settings=make_settings(tmp_path),
            capabilities=make_snapshot(),
            existing_active_turn_id="turn_existing",
        )


async def test_uses_contract_derived_start_method_names(tmp_path: Path) -> None:
    rpc = FakeRpc(
        thread_start_method="effective/thread-start",
        realtime_start_method="effective/realtime-start",
    )
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
        contract=make_voice_contract(
            thread_start="effective/thread-start",
            realtime_start="effective/realtime-start",
        ),
    )

    assert await session.start("offer-sdp") == "answer-sdp"
    assert [method for method, _params in rpc.requests[:2]] == [
        "effective/thread-start",
        "effective/realtime-start",
    ]
    await session.close()


async def test_uses_built_in_prompt_when_implicit_file_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "default_prompt_path", lambda: tmp_path / "prompt.md")

    assert await _started_prompt(FakeRpc(), MocoSettings()) == DEFAULT_REALTIME_PROMPT


def test_built_in_prompt_defines_moco_frameless_and_irodori_contract() -> None:
    prompt = " ".join(DEFAULT_REALTIME_PROMPT.split())
    assert "You are moco" in prompt
    assert "delegate" in prompt
    assert "same unified assistant" in prompt
    assert "Irodori" in prompt
    assert "moco.speech_plan" in prompt


def test_packaged_prompt_example_matches_the_runtime_default() -> None:
    example = (
        (Path(__file__).resolve().parents[1] / "config" / "moco.prompt.example.md")
        .read_text(encoding="utf-8")
        .strip()
    )

    assert example == DEFAULT_REALTIME_PROMPT


async def test_reads_implicit_dot_moco_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_file = tmp_path / ".moco" / "prompt.md"
    prompt_file.parent.mkdir()
    prompt_file.write_text("Implicit persona", encoding="utf-8")
    monkeypatch.setattr(session_module, "default_prompt_path", lambda: prompt_file)

    assert await _started_prompt(FakeRpc(), MocoSettings()) == "Implicit persona"


async def test_reads_configured_prompt_again_for_each_new_session(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("First persona", encoding="utf-8")
    settings = MocoSettings(
        codex=CodexSettings(
            command=(str(tmp_path / "unused-codex"),),
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )

    first = await _started_prompt(FakeRpc(), settings)
    prompt_file.write_text("Second persona", encoding="utf-8")
    second = await _started_prompt(FakeRpc(), settings)

    assert (first, second) == ("First persona", "Second persona")


async def test_reads_utf8_bom_without_forwarding_it(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_bytes(b"\xef\xbb\xbfBOM persona")
    settings = MocoSettings(
        codex=CodexSettings(
            command=(str(tmp_path / "unused-codex"),),
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )

    assert await _started_prompt(FakeRpc(), settings) == "BOM persona"


async def test_normalizes_prompt_line_endings_before_wire(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_bytes(b"First\r\n\r\nSecond\rThird\n")
    settings = MocoSettings(
        codex=CodexSettings(
            command=(str(tmp_path / "unused-codex"),),
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )

    assert await _started_prompt(FakeRpc(), settings) == "First\n\nSecond\nThird"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b" \n\t", "blank"),
        (b"\xef\xbb\xbf \n", "blank"),
        (b"\xff", "UTF-8"),
        (b"x" * 65_537, "64 KiB"),
    ],
    ids=["blank", "bom-only", "non_utf8", "oversized"],
)
async def test_rejects_invalid_prompt_before_rpc_start(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_bytes(payload)
    settings = MocoSettings(
        codex=CodexSettings(
            command=(str(tmp_path / "unused-codex"),),
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
    rpc = FakeRpc()

    with pytest.raises(CodexPromptError, match=message):
        await make_session(
            rpc,
            settings=settings,
            capabilities=make_snapshot(),
        ).start("offer-sdp")

    assert rpc.started is False
    assert rpc.requests == []


async def test_unusable_programmatic_prompt_path_is_a_prompt_error(tmp_path: Path) -> None:
    unsafe_codex = CodexSettings.model_construct(
        command=(str(tmp_path / "unused-codex"),),
        working_directory=tmp_path,
        prompt_file=tmp_path / "moco\0prompt",
    )
    settings = MocoSettings(codex=unsafe_codex)
    rpc = FakeRpc()

    with pytest.raises(CodexPromptError, match="could not be read"):
        await make_session(
            rpc,
            settings=settings,
            capabilities=make_snapshot(),
        ).start("offer-sdp")

    assert rpc.started is False
    assert rpc.requests == []


@pytest.mark.parametrize(
    ("kind", "message"),
    [("missing", "not found"), ("directory", "could not be read")],
)
async def test_rejects_unreadable_configured_prompt_before_rpc_start(
    tmp_path: Path,
    kind: str,
    message: str,
) -> None:
    prompt_file = tmp_path / "prompt.md"
    if kind == "directory":
        prompt_file.mkdir()
    settings = MocoSettings(
        codex=CodexSettings(
            command=(str(tmp_path / "unused-codex"),),
            working_directory=tmp_path,
            prompt_file=prompt_file,
        ),
    )
    rpc = FakeRpc()

    with pytest.raises(CodexPromptError, match=message):
        await make_session(
            rpc,
            settings=settings,
            capabilities=make_snapshot(),
        ).start("offer-sdp")

    assert rpc.started is False
    assert rpc.requests == []


async def test_exposes_transcript_and_error_notifications(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()

    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_test", "role": "assistant", "delta": "こん"},
    )
    await rpc.emit(
        "thread/realtime/transcript/done",
        {"threadId": "thr_test", "role": "assistant", "text": "こんにちは。"},
    )
    await rpc.emit(
        "thread/realtime/error",
        {"threadId": "thr_test", "message": "transport closed"},
    )

    assert await anext(events) == TranscriptEvent("delta", "thr_test", "assistant", "こん")
    assert await anext(events) == TranscriptEvent(
        "done",
        "thr_test",
        "assistant",
        "こんにちは。",
    )
    assert await anext(events) == RealtimeErrorEvent("thr_test", "transport closed")
    await session.close()


async def test_exposes_safe_turn_and_work_activity(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()

    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await rpc.emit(
        "item/started",
        {
            "threadId": "thr_test",
            "turnId": "turn-1",
            "startedAtMs": 1_785_496_800_000,
            "item": {
                "id": "item-1",
                "type": "commandExecution",
                "command": "private command must not escape",
                "commandActions": [],
                "cwd": "/private/path",
                "status": "inProgress",
            },
        },
    )
    await rpc.emit(
        "item/completed",
        {
            "threadId": "thr_test",
            "turnId": "turn-1",
            "completedAtMs": 1_785_496_801_000,
            "item": {
                "id": "item-1",
                "type": "commandExecution",
                "command": "private command must not escape",
                "commandActions": [],
                "cwd": "/private/path",
                "status": "completed",
            },
        },
    )
    await rpc.emit(
        "turn/completed",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )

    assert await anext(events) == ActivityEvent(
        "turn",
        "started",
        "thr_test",
        "turn-1",
        None,
    )
    assert await anext(events) == ActivityEvent(
        "command_execution",
        "started",
        "thr_test",
        "turn-1",
        1_785_496_800_000,
    )
    assert await anext(events) == ActivityEvent(
        "command_execution",
        "completed",
        "thr_test",
        "turn-1",
        1_785_496_801_000,
    )
    assert await anext(events) == ActivityEvent(
        "turn",
        "completed",
        "thr_test",
        "turn-1",
        None,
    )


async def test_realtime_event_backlog_is_bounded_and_fails_closed(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    notification_task = cast("asyncio.Task[None]", session._notification_task)  # noqa: SLF001

    try:
        for index in range(65):
            await rpc.emit(
                "thread/realtime/transcript/delta",
                {
                    "threadId": "thr_test",
                    "role": "user",
                    "delta": str(index),
                },
            )

        await asyncio.wait_for(asyncio.shield(notification_task), 0.5)
        events = session.notifications()
        for _index in range(64):
            assert isinstance(await anext(events), TranscriptEvent)
        with pytest.raises(CodexRpcError, match="event backlog limit exceeded"):
            await anext(events)
    finally:
        await session.close()
    await session.close()


async def test_auxiliary_event_backlog_fails_session_instead_of_looking_invalid(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    notification_task = cast("asyncio.Task[None]", session._notification_task)  # noqa: SLF001

    try:
        await rpc.emit(
            "turn/started",
            {"threadId": "thr_test", "turn": {"id": "turn-1"}},
        )
        for index in range(session_module._MAX_PENDING_REALTIME_EVENTS):  # noqa: SLF001
            await rpc.emit(
                "item/started",
                {
                    "threadId": "thr_test",
                    "turnId": "turn-1",
                    "item": {"type": "commandExecution"},
                    "startedAtMs": index,
                },
            )

        await asyncio.wait_for(asyncio.shield(notification_task), 0.5)
        events = session.notifications()
        for _index in range(session_module._MAX_PENDING_REALTIME_EVENTS):  # noqa: SLF001
            assert isinstance(await anext(events), ActivityEvent)
        with pytest.raises(CodexRpcError, match="event backlog limit exceeded"):
            await anext(events)
    finally:
        await session.close()


async def test_exposes_reasoning_summary_but_not_raw_reasoning(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    assert await anext(events) == ActivityEvent(
        "turn",
        "started",
        "thr_test",
        "turn-1",
        None,
    )
    await rpc.emit(
        "item/reasoning/textDelta",
        {
            "threadId": "thr_test",
            "turnId": "turn-1",
            "itemId": "r-1",
            "delta": "raw reasoning must not escape",
        },
    )
    await rpc.emit(
        "item/reasoning/summaryTextDelta",
        {
            "threadId": "thr_test",
            "turnId": "turn-1",
            "itemId": "r-1",
            "summaryIndex": 0,
            "delta": "設定を確認しています。",
        },
    )

    assert await anext(events) == ReasoningSummaryEvent(
        "thr_test",
        "turn-1",
        "r-1",
        "設定を確認しています。",
    )
    await session.close()


@pytest.mark.parametrize(
    ("item_type", "expected_kind"),
    [
        ("reasoning", "reasoning"),
        ("commandExecution", "command_execution"),
        ("fileChange", "file_change"),
        ("mcpToolCall", "external_tool"),
        ("dynamicToolCall", "external_tool"),
        ("collabAgentToolCall", "subagent"),
        ("subAgentActivity", "subagent"),
        ("webSearch", "web_search"),
        ("imageView", "image_view"),
        ("imageGeneration", "image_generation"),
        ("contextCompaction", "context_compaction"),
        ("futureItem", "codex_work"),
    ],
)
async def test_maps_item_types_without_forwarding_payload(
    tmp_path: Path,
    item_type: str,
    expected_kind: str,
) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    assert await anext(events) == ActivityEvent(
        "turn",
        "started",
        "thr_test",
        "turn-1",
        None,
    )
    await rpc.emit(
        "item/started",
        {
            "threadId": "thr_test",
            "turnId": "turn-1",
            "startedAtMs": 1234,
            "item": {
                "id": "item-1",
                "type": item_type,
                "command": "private",
                "cwd": "/private",
                "arguments": {"secret": True},
                "query": "private search",
                "result": "private result",
            },
        },
    )

    event = await anext(events)
    assert isinstance(event, ActivityEvent)
    assert event.kind == expected_kind
    assert "private" not in repr(event)
    await session.close()


async def test_tracks_active_turn_without_sending_control_requests(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)

    assert session.active_turn_id == "turn-1"
    assert all(method != "turn/interrupt" for method, _params in rpc.requests)
    assert all(method != "thread/realtime/appendText" for method, _params in rpc.requests)
    await session.close()


async def test_owns_only_the_active_turn_on_its_realtime_thread(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)

    assert session.owns_active_turn("thr_test", "turn-1")
    assert not session.owns_active_turn("thr_other", "turn-1")
    assert not session.owns_active_turn("thr_test", "turn-other")
    await session.close()


async def test_interrupts_the_active_realtime_turn_once(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)

    assert await session.interrupt_active_turn()
    assert rpc.requests[-1] == (
        "turn/interrupt",
        {"threadId": "thr_test", "turnId": "turn-1"},
    )

    await rpc.emit(
        "turn/completed",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)
    assert not await session.interrupt_active_turn()
    assert sum(method == "turn/interrupt" for method, _params in rpc.requests) == 1
    await session.close()


async def test_ignores_completion_for_a_different_active_turn(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)

    await rpc.emit(
        "turn/completed",
        {"threadId": "thr_test", "turn": {"id": "turn-2"}},
    )
    await asyncio.sleep(0)

    assert session.active_turn_id == "turn-1"
    await session.close()


async def test_sdp_timeout_stops_and_closes(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.emit_sdp = False
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
        sdp_timeout=0.01,
    )

    with pytest.raises(CodexRpcTimeoutError, match="thread/realtime/sdp"):
        await session.start("offer-sdp")

    assert session.closed
    assert rpc.close_calls == 0
    assert "thread/realtime/stop" in [method for method, _params in rpc.requests]


async def test_invalid_notification_surfaces_protocol_error(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_test", "delta": "missing role"},
    )

    with pytest.raises(CodexRpcError, match="invalid 'role'"):
        await anext(events)
    assert rpc.close_calls == 0
    await session.close()


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "item/started",
            {
                "threadId": "thr_test",
                "turnId": "turn-1",
                "item": {"type": "commandExecution"},
            },
        ),
        (
            "item/reasoning/summaryTextDelta",
            {
                "threadId": "thr_test",
                "turnId": "turn-1",
                "itemId": "reasoning-1",
                "delta": "",
            },
        ),
    ],
)
async def test_discards_invalid_auxiliary_notifications_without_ending_conversation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    method: str,
    params: dict[str, JsonValue],
) -> None:
    caplog.set_level(logging.INFO, logger="moco.codex.session")
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit(
        "turn/started",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    assert isinstance(await anext(events), ActivityEvent)

    await rpc.emit(method, params)
    await rpc.emit(
        "thread/realtime/transcript/done",
        {"threadId": "thr_test", "role": "assistant", "text": "継続中です。"},
    )

    assert await anext(events) == TranscriptEvent(
        "done",
        "thr_test",
        "assistant",
        "継続中です。",
    )
    assert not rpc.closed
    assert "event=codex_auxiliary_notification_discarded" in caplog.text
    await session.close()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")

    await session.close()
    await session.close()

    methods = [method for method, _params in rpc.requests]
    assert methods.count("thread/realtime/stop") == 1
    assert session.closed


@pytest.mark.parametrize("sdp_timeout", [0.0, -1.0])
def test_rejects_non_positive_sdp_timeout(
    tmp_path: Path,
    sdp_timeout: float,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        make_session(
            FakeRpc(),
            settings=make_settings(tmp_path),
            capabilities=make_snapshot(),
            sdp_timeout=sdp_timeout,
        )


async def test_rejects_empty_duplicate_and_closed_start(tmp_path: Path) -> None:
    empty_rpc = FakeRpc()
    empty = make_session(
        empty_rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    with pytest.raises(ValueError, match="must not be empty"):
        await empty.start("")
    assert not empty_rpc.started

    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    with pytest.raises(CodexRpcError, match="already been started"):
        await session.start("second")
    await session.close()

    closed = make_session(
        FakeRpc(),
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await closed.close()
    with pytest.raises(CodexRpcError, match="closed"):
        await closed.start("offer-sdp")


async def test_context_manager_closes_unstarted_session(tmp_path: Path) -> None:
    rpc = FakeRpc()
    async with make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    ) as session:
        assert session.thread_id is None
    assert session.closed
    assert rpc.close_calls == 0


@pytest.mark.parametrize(
    ("thread_result", "message"),
    [
        (None, "invalid result"),
        ({}, "contain a thread"),
        ({"thread": {"id": ""}}, "valid thread id"),
        ({"thread": {"id": True}}, "valid thread id"),
    ],
)
async def test_invalid_thread_results_close_rpc(
    tmp_path: Path,
    thread_result: JsonValue,
    message: str,
) -> None:
    """An invalid thread result closes the borrower only."""
    rpc = FakeRpc()
    rpc.thread_result = thread_result
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    with pytest.raises(CodexRpcError, match=message):
        await session.start("offer-sdp")
    assert session.closed
    assert rpc.close_calls == 0


async def test_start_request_failure_closes_rpc(tmp_path: Path) -> None:
    """A failed thread request closes the borrower only."""
    rpc = FakeRpc()
    rpc.fail_method = "thread/start"
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    with pytest.raises(CodexRpcError, match="forced failure"):
        await session.start("offer-sdp")
    assert session.closed
    assert rpc.close_calls == 0


async def test_ignores_other_thread_and_non_realtime_events(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit("fake/status", {})
    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_other", "role": "assistant", "delta": "wrong"},
    )
    await rpc.emit(
        "thread/realtime/transcript/done",
        {"threadId": "thr_test", "role": "user", "text": "right"},
    )
    assert await anext(events) == TranscriptEvent("done", "thr_test", "user", "right")
    await session.close()


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"threadId": "thr_test", "role": "system", "delta": "bad"}, "unsupported"),
        ({"role": "assistant", "delta": "bad"}, "threadId"),
    ],
)
async def test_invalid_transcript_notification_closes_rpc(
    tmp_path: Path,
    params: dict[str, JsonValue],
    message: str,
) -> None:
    """An invalid transcript notification fails the borrower without closing the connection."""
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    await rpc.emit("thread/realtime/transcript/delta", params)
    with pytest.raises(CodexRpcError, match=message):
        await anext(events)
    assert rpc.close_calls == 0
    await session.close()


async def test_turn_completion_clears_active_turn(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    await rpc.emit("turn/started", {"threadId": "thr_test", "turn": {"id": "turn-1"}})
    await asyncio.sleep(0)
    assert session.active_turn_id == "turn-1"
    await rpc.emit(
        "turn/completed",
        {"threadId": "thr_test", "turn": {"id": "turn-1"}},
    )
    await asyncio.sleep(0)
    completed_turn: object = session.active_turn_id
    assert completed_turn is None
    await session.close()


async def test_close_active_turn_does_not_append_a_cancel_instruction(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    await rpc.emit("turn/started", {"threadId": "thr_test", "turn": {"id": "turn-1"}})
    await asyncio.sleep(0)
    await session.close()

    methods = [method for method, _params in rpc.requests]
    assert "thread/realtime/stop" in methods
    assert "thread/realtime/appendText" not in methods
    assert "turn/interrupt" not in methods


async def test_notification_stream_failure_surfaces_and_closes(tmp_path: Path) -> None:
    """A notification stream failure closes the borrower only."""
    rpc = FakeRpc()
    rpc.notification_error = CodexRpcError("stream failed")
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    with pytest.raises(CodexRpcError, match="stream failed"):
        await session.start("offer-sdp")
    assert session.closed
    assert rpc.close_calls == 0


async def test_stream_failure_remains_primary_when_notification_cleanup_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_cleanup = "private-stream-cleanup"
    rpc = FakeRpc()
    primary = CodexRpcError("stream failed first")
    rpc.notification_error = primary
    rpc.close_error = SecondaryCloseError(private_cleanup)
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
        sdp_timeout=0.05,
    )

    with pytest.raises(CodexRpcError, match="stream failed first") as caught:
        await session.start("offer-sdp")

    assert caught.value is primary
    assert not isinstance(caught.value, CodexRpcTimeoutError)
    assert session.closed
    assert "SecondaryCloseError" not in caplog.text
    assert rpc.close_calls == 0
    assert private_cleanup not in caplog.text


async def test_validation_error_reaches_events_when_notification_cleanup_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_cleanup = "private-validation-cleanup"
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    rpc.close_error = SecondaryCloseError(private_cleanup)

    await rpc.emit(
        "thread/realtime/transcript/delta",
        {"threadId": "thr_test", "role": "system", "delta": "bad"},
    )

    with pytest.raises(CodexRpcError, match="unsupported role"):
        await asyncio.wait_for(anext(events), 0.5)
    assert "SecondaryCloseError" not in caplog.text
    assert private_cleanup not in caplog.text
    await session.close()
    assert session.closed
    assert rpc.close_calls == 0


async def test_stop_failure_still_closes_rpc(tmp_path: Path) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    rpc.fail_method = "thread/realtime/stop"
    with pytest.raises(CodexRpcError, match="forced failure"):
        await session.close()
    assert session.closed
    assert not rpc.closed
    assert rpc.close_calls == 0


async def test_connection_close_failure_still_finalizes_session(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    close_error = CodexRpcError("close failed")
    rpc.close_error = close_error

    await session.close()

    assert session.closed
    assert rpc.close_calls == 0
    assert session._notification_task is not None  # noqa: SLF001
    assert session._notification_task.done()  # noqa: SLF001
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 0.5)
    await session.close()


async def test_stop_failure_remains_primary_when_connection_close_also_fails(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    rpc.fail_method = "thread/realtime/stop"
    rpc.close_error = CodexRpcError("secondary close failure")

    with pytest.raises(CodexRpcError, match="forced failure"):
        await session.close()

    assert session.closed
    assert rpc.close_calls == 0


async def test_start_failure_remains_primary_when_connection_close_also_fails(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    rpc.fail_method = "thread/start"
    rpc.close_error = CodexRpcError("secondary close failure")
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )

    with pytest.raises(CodexRpcError, match="forced failure"):
        await session.start("offer-sdp")

    assert session.closed
    assert session.thread_id is None
    assert rpc.close_calls == 0


async def test_start_failure_retrieves_secondary_sdp_future_exception(
    tmp_path: Path,
) -> None:
    private_secondary = "private stream body"
    primary = CodexRpcError("primary startup failure")
    rpc = StartupPrimaryAfterNotificationRpc(
        primary,
        CodexRpcError(private_secondary),
    )
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    loop = asyncio.get_running_loop()
    handled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: handled.append(context))
    try:
        with pytest.raises(CodexRpcError) as caught:
            await session.start("offer-sdp")
        assert caught.value is primary
        assert session.closed

        session._notification_task = None  # noqa: SLF001
        session._sdp_future = None  # noqa: SLF001
        del session
        gc.collect()
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert handled == []
    assert private_secondary not in repr(handled)


@pytest.mark.parametrize("phase", ["stop", "connection"])
async def test_close_cancellation_still_finalizes_session(
    tmp_path: Path,
    phase: str,
) -> None:
    rpc = CancellationPhaseRpc(phase)
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    close_task = asyncio.create_task(session.close())
    await asyncio.wait_for(rpc.phase_started.wait(), 0.5)

    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    assert not rpc.closed
    close_task.cancel()
    rpc.phase_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert session.closed
    assert not rpc.closed
    assert rpc.close_calls == 0
    assert session._notification_task is not None  # noqa: SLF001
    assert session._notification_task.done()  # noqa: SLF001
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 0.5)


async def test_notification_cleanup_cancellation_still_finalizes_session(
    tmp_path: Path,
) -> None:
    rpc = BlockingNotificationCleanupRpc()
    session = make_session(
        rpc,
        settings=make_settings(tmp_path),
        capabilities=make_snapshot(),
    )
    await session.start("offer-sdp")
    events = session.notifications()
    close_task = asyncio.create_task(session.close())
    await asyncio.wait_for(rpc.notification_cancel_started.wait(), 0.5)

    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    close_task.cancel()
    rpc.notification_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert session.closed
    assert rpc.close_calls == 0
    assert session._notification_task is not None  # noqa: SLF001
    assert session._notification_task.done()  # noqa: SLF001
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(events), 0.5)
