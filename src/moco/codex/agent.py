from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Self, cast

from moco.codex.capabilities import (
    ApprovalMode,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    SandboxMode,
    is_unsafe_voice_policy,
)
from moco.codex.rpc import JsonValue, RpcNotification
from moco.codex.schema import (
    AgentEventProfile,
    ClientMethodContract,
    CodexProtocolContract,
    ParamsKind,
    SemanticMethod,
)
from moco.config import AgentProfileMode
from moco.errors import (
    AgentTurnErrorCode,
    CodexAgentError,
    CodexProcessExitedError,
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTimeoutError,
)
from moco.runtime.telemetry import safe_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping


class _SharedConnection(Protocol):
    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue: ...

    def notifications(self) -> AsyncIterator[RpcNotification]: ...


_MAX_AGENT_INPUT_BYTES = 64 * 1024
_MAX_AGENT_OUTPUT_BYTES = 256 * 1024
_MAX_ID_BYTES = 4 * 1024
_MAX_TURN_START_BUFFER_ITEMS = 32
_MAX_TURN_START_BUFFER_BYTES = 64 * 1024
_CLEANUP_TIMEOUT_SECONDS = 0.25
_CLOSE_WAIT_SECONDS = 0.25

_ADMISSION_UNAVAILABLE = "agent admission is unavailable"
_CONNECTION_INVALID = "agent connection is invalid"
_CONTRACT_INVALID = "agent protocol contract cannot express the required request"
_INPUT_INVALID = "agent input is invalid"
_THREAD_RESULT_INVALID = "agent thread result is invalid"
_TURN_RESULT_INVALID = "agent turn result is invalid"
_COMPLETION_INVALID = "agent completion is invalid"
_FINAL_UNAVAILABLE = "agent final answer is unavailable"
_TURN_FAILED = "agent turn failed"
_TURN_INTERRUPTED = "agent turn was interrupted"
_TURN_ACTIVE = "agent turn is already active"
_NO_ACTIVE_TURN = "no Agent turn is active"
_STEER_UNAVAILABLE = "agent steer is unavailable"
_STEER_ACTIVE = "agent steer is already active"
_STEER_REJECTED = "agent_steer_rejected"
_REQUEST_FAILED = "agent request failed"
_CONNECTION_LOST = "agent connection was lost"
_SESSION_CLOSED = "agent session is closed"
_UNKNOWN_OUTCOME = "agent turn outcome is unknown"
_TURN_START_BUFFER_OVERFLOW = "agent turn start notification buffer overflow"

type AgentActivityKind = Literal[
    "turn",
    "reasoning",
    "command_execution",
    "file_change",
    "external_tool",
    "subagent",
    "web_search",
    "image_view",
    "image_generation",
    "context_compaction",
    "codex_work",
]
type AgentActivityPhase = Literal["started", "completed"]

_ITEM_ACTIVITY: Mapping[str, AgentActivityKind] = {
    "reasoning": "reasoning",
    "commandExecution": "command_execution",
    "fileChange": "file_change",
    "mcpToolCall": "external_tool",
    "dynamicToolCall": "external_tool",
    "collabAgentToolCall": "subagent",
    "subAgentActivity": "subagent",
    "webSearch": "web_search",
    "imageView": "image_view",
    "imageGeneration": "image_generation",
    "contextCompaction": "context_compaction",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentActivityEvent:
    kind: AgentActivityKind
    phase: AgentActivityPhase


_METHOD_REQUIREMENTS: Mapping[SemanticMethod, tuple[ParamsKind, frozenset[str]]] = {
    SemanticMethod.THREAD_START: (
        ParamsKind.OBJECT,
        frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
    ),
    SemanticMethod.TURN_START: (
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    ),
    SemanticMethod.TURN_STEER: (
        ParamsKind.OBJECT,
        frozenset({"expectedTurnId", "input", "threadId"}),
    ),
    SemanticMethod.TURN_INTERRUPT: (
        ParamsKind.OBJECT,
        frozenset({"threadId", "turnId"}),
    ),
}


def _transport_safe_text(value: object, *, max_bytes: int, non_blank: bool) -> bool:
    if type(value) is not str:
        return False
    text = value
    if non_blank and not text.strip():
        return False
    try:
        return len(text.encode("utf-8")) <= max_bytes
    except UnicodeEncodeError:
        return False


def _valid_id(value: object) -> bool:
    return _transport_safe_text(value, max_bytes=_MAX_ID_BYTES, non_blank=True)


def _stable_error(
    message: str,
    *,
    code: AgentTurnErrorCode | str | None = None,
) -> CodexAgentError:
    if code is None:
        code = {
            _TURN_FAILED: AgentTurnErrorCode.FAILED,
            _FINAL_UNAVAILABLE: AgentTurnErrorCode.FAILED,
            _TURN_INTERRUPTED: AgentTurnErrorCode.INTERRUPTED,
            _UNKNOWN_OUTCOME: AgentTurnErrorCode.OUTCOME_UNKNOWN,
        }.get(message)
    return CodexAgentError(message, code=code)


def _turn_error(message: str, code: AgentTurnErrorCode) -> CodexAgentError:
    return _stable_error(message, code=code)


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    return {
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }.get(type(value), "")


def _field_admits(value: object, accepted: frozenset[str] | None) -> bool:
    if accepted is None:
        return False
    actual = _json_type(value)
    if actual == "integer" and "number" in accepted:
        return True
    return actual in accepted


class AgentSession:
    """Own one ephemeral Agent thread and one sequential turn at a time.

    The app-server connection belongs to the composition root. This owner borrows its request
    and notification interfaces, and never starts or closes that shared connection.
    """

    __slots__ = (
        "_active_future",
        "_active_turn_id",
        "_activity_sink",
        "_candidate_fallback",
        "_candidate_final",
        "_capabilities",
        "_close_lock",
        "_close_task",
        "_closed",
        "_connection",
        "_contract",
        "_interrupt_task",
        "_interrupt_thread_id",
        "_interrupt_turn_id",
        "_profile",
        "_pump_task",
        "_start_cleanup_source",
        "_start_cleanup_task",
        "_state_lock",
        "_steer_task",
        "_terminal_error",
        "_terminal_notified",
        "_terminal_sink",
        "_thread_id",
        "_thread_start_task",
        "_turn_start_buffer",
        "_turn_start_buffer_bytes",
        "_turn_start_sent",
        "_turn_start_task",
        "_turn_starting",
        "_working_directory",
    )

    def __init__(
        self,
        connection: _SharedConnection,
        contract: CodexProtocolContract,
        capabilities: CapabilitySnapshot,
        working_directory: Path,
        profile: AgentProfileMode,
        *,
        activity_sink: Callable[[AgentActivityEvent], object] | None = None,
        terminal_sink: Callable[[], object] | None = None,
    ) -> None:
        try:
            request = connection.request
            notifications = connection.notifications
        except Exception:  # noqa: BLE001
            raise _stable_error(_CONNECTION_INVALID) from None
        if not callable(request) or not callable(notifications):
            raise _stable_error(_CONNECTION_INVALID)
        if type(contract) is not CodexProtocolContract:
            raise _stable_error(_CONTRACT_INVALID)
        if type(capabilities) is not CapabilitySnapshot:
            raise _stable_error(_ADMISSION_UNAVAILABLE)
        if not isinstance(working_directory, Path) or not working_directory.is_absolute():
            raise _stable_error(_CONTRACT_INVALID)
        if type(profile) is not AgentProfileMode:
            raise _stable_error(_CONTRACT_INVALID)
        if activity_sink is not None and not callable(activity_sink):
            raise _stable_error(_CONTRACT_INVALID)
        if terminal_sink is not None and not callable(terminal_sink):
            raise _stable_error(_CONTRACT_INVALID)

        self._activity_sink = activity_sink
        self._terminal_sink = terminal_sink
        self._connection = connection
        self._contract = contract
        self._capabilities = capabilities
        self._working_directory = working_directory
        self._profile = profile
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._thread_id: str | None = None
        self._thread_start_task: asyncio.Task[JsonValue] | None = None
        self._turn_starting = False
        self._turn_start_sent = False
        self._turn_start_task: asyncio.Task[JsonValue] | None = None
        self._turn_start_buffer: list[RpcNotification] = []
        self._turn_start_buffer_bytes = 0
        self._active_turn_id: str | None = None
        self._active_future: asyncio.Future[str] | None = None
        self._candidate_final: str | None = None
        self._candidate_fallback: str | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._start_cleanup_task: asyncio.Task[None] | None = None
        self._start_cleanup_source: asyncio.Task[JsonValue] | None = None
        self._steer_task: asyncio.Task[None] | None = None
        self._interrupt_task: asyncio.Task[None] | None = None
        self._interrupt_thread_id: str | None = None
        self._interrupt_turn_id: str | None = None
        self._terminal_error: CodexAgentError | None = None
        self._terminal_notified = False
        self._closed = False

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def active_turn_id(self) -> str | None:
        return self._active_turn_id

    def owns_active_turn(self, thread_id: str, turn_id: str) -> bool:
        """Report whether one approval correlation names this session's active turn."""
        return self._thread_id == thread_id and self._active_turn_id == turn_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def reusable(self) -> bool:
        return not self._closed and self._terminal_error is None

    def __repr__(self) -> str:
        return (
            "AgentSession("
            f"closed={self._closed}, "
            f"thread_active={self._thread_id is not None}, "
            f"turn_active={self._active_future is not None}"
            ")"
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def start_turn(self, text: str) -> str:  # noqa: C901, PLR0912, PLR0915
        self._require_admission()
        if not _transport_safe_text(
            text,
            max_bytes=_MAX_AGENT_INPUT_BYTES,
            non_blank=True,
        ):
            raise _stable_error(_INPUT_INVALID)

        thread_method = self._require_method(SemanticMethod.THREAD_START)
        turn_method = self._require_method(SemanticMethod.TURN_START)

        async with self._state_lock:
            self._require_open()
            if self._turn_starting or self._active_future is not None:
                raise _stable_error(_TURN_ACTIVE)
            if self._thread_start_task is not None:
                if not self._thread_start_task.done():
                    self._terminalize_unknown()
                    raise _stable_error(_UNKNOWN_OUTCOME)
                self._thread_start_task = None
            if self._interrupt_task is not None:
                if not self._interrupt_task.done():
                    self._terminalize_unknown()
                    raise _stable_error(_UNKNOWN_OUTCOME)
                self._retire_interrupt_claim()
            if self._steer_task is not None:
                if not self._steer_task.done():
                    self._terminalize_unknown()
                    raise _turn_error(
                        _UNKNOWN_OUTCOME,
                        AgentTurnErrorCode.OUTCOME_UNKNOWN,
                    )
                self._retire_steer_claim()
            self._turn_starting = True
            self._turn_start_sent = False
            self._turn_start_buffer.clear()
            self._turn_start_buffer_bytes = 0

        turn_task: asyncio.Task[JsonValue] | None = None
        thread_start_task: asyncio.Task[JsonValue] | None = None
        turn_id: str | None = None
        try:
            self._ensure_pump()
            self._require_open()

            if self._thread_id is None:
                thread_start_task = asyncio.create_task(
                    self._request(
                        thread_method,
                        self._thread_params(),
                        _REQUEST_FAILED,
                    )
                )
                self._thread_start_task = thread_start_task
                thread_start_task.add_done_callback(self._finish_thread_start)
                thread_result = await asyncio.shield(thread_start_task)
                thread_id = self._parse_thread_id(thread_result)
                self._require_open()
                self._thread_id = thread_id

            thread_id = self._thread_id
            if thread_id is None:  # pragma: no cover - guarded by the assignment above
                raise _stable_error(_THREAD_RESULT_INVALID)  # noqa: TRY301

            turn_task = asyncio.create_task(
                self._request(
                    turn_method,
                    {
                        "input": [{"type": "text", "text": text}],
                        "threadId": thread_id,
                    },
                    _REQUEST_FAILED,
                )
            )
            self._turn_start_task = turn_task
            self._turn_start_sent = True
            turn_result = await asyncio.shield(turn_task)
            try:
                turn_id = self._parse_turn_id(turn_result)
            except CodexAgentError:
                self._terminalize_unknown()
                raise _stable_error(_UNKNOWN_OUTCOME) from None
            self._require_open()

            future = asyncio.get_running_loop().create_future()
            async with self._state_lock:
                self._require_open()
                if self._active_future is not None:
                    raise _stable_error(_TURN_ACTIVE)  # noqa: TRY301
                self._active_turn_id = turn_id
                self._active_future = future
                self._candidate_final = None
                self._candidate_fallback = None
                self._turn_starting = False
                self._turn_start_sent = False
                self._turn_start_task = None

            self._replay_turn_start_buffer()
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await self._handle_cancellation(turn_task, thread_start_task)
            if self._closed:
                raise _stable_error(_SESSION_CLOSED) from None
            raise
        except CodexAgentError as error:
            if turn_id is not None and self._turn_start_sent:
                cleanup: asyncio.Task[None] | None = None
                if self._turn_start_task is turn_task:
                    with suppress(CodexAgentError):
                        cleanup = self._claim_start_cleanup()
                if cleanup is not None:
                    with suppress(asyncio.CancelledError, CodexAgentError):
                        await asyncio.shield(cleanup)
                raise
            if turn_id is None and self._turn_start_sent:
                self._terminalize_unknown()
                raise _stable_error(_UNKNOWN_OUTCOME) from None
            self._settle_active(error=error, expected_turn_id=turn_id)
            raise
        except Exception:  # noqa: BLE001
            if self._turn_start_sent:
                self._terminalize_unknown()
                raise _stable_error(_UNKNOWN_OUTCOME) from None
            failure = _stable_error(_REQUEST_FAILED)
            self._settle_active(error=failure, expected_turn_id=turn_id)
            raise failure from None
        finally:
            if (
                thread_start_task is not None
                and self._thread_start_task is thread_start_task
                and thread_start_task.done()
            ):
                self._thread_start_task = None
            if (
                turn_task is not None
                and self._turn_start_task is turn_task
                and turn_task.done()
                and not (self._closed and turn_id is not None)
            ):
                self._turn_start_task = None
            if turn_id is None and not self._turn_start_sent and self._turn_starting:
                self._turn_starting = False
                self._turn_start_buffer.clear()
                self._turn_start_buffer_bytes = 0

    async def interrupt(self) -> None:
        self._require_admission()
        method = self._require_method(SemanticMethod.TURN_INTERRUPT)
        task: asyncio.Task[None] | None
        turn_id: str | None
        async with self._state_lock:
            self._require_open()
            thread_id = self._thread_id
            turn_id = self._active_turn_id
            if thread_id is None or turn_id is None or self._active_future is None:
                raise _stable_error(_NO_ACTIVE_TURN)
            task = self._claim_interrupt_locked(
                method,
                thread_id,
                turn_id,
                settlement_error=_stable_error(_TURN_INTERRUPTED),
            )

        if task is None or turn_id is None:
            raise _stable_error(_NO_ACTIVE_TURN)
        await asyncio.shield(task)
        self._settle_active(error=_stable_error(_TURN_INTERRUPTED), expected_turn_id=turn_id)

    async def steer(self, text: str) -> None:
        self._require_admission()
        if not _transport_safe_text(
            text,
            max_bytes=_MAX_AGENT_INPUT_BYTES,
            non_blank=True,
        ):
            raise _stable_error(_INPUT_INVALID)
        method = self._require_steer_method()

        async with self._state_lock:
            self._require_open()
            thread_id = self._thread_id
            turn_id = self._active_turn_id
            if thread_id is None or turn_id is None or self._active_future is None:
                raise _stable_error(_NO_ACTIVE_TURN)
            if self._steer_task is not None:
                if not self._steer_task.done():
                    raise _stable_error(_STEER_ACTIVE)
                self._retire_steer_claim()
            task = asyncio.create_task(
                self._send_steer(method, thread_id, turn_id, text),
            )
            self._steer_task = task
            task.add_done_callback(self._finish_steer_claim)

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            known_outcome = await self._cancel_and_recover_steer(task)
            if not known_outcome:
                self._terminalize_unknown()
            raise

    async def close(self) -> None:
        async with self._close_lock:
            task = self._close_task
            if task is None:
                self._closed = True
                task = asyncio.create_task(self._close_impl())
                self._close_task = task
                task.add_done_callback(self._consume_task)
        await asyncio.shield(task)

    async def _close_impl(self) -> None:  # noqa: C901, PLR0912
        try:
            steer = self._steer_task
            if steer is not None:
                known_outcome = await self._cancel_and_recover_steer(steer)
                if not known_outcome:
                    self._terminalize_unknown()

            thread_start = self._thread_start_task
            if thread_start is not None:
                self._cancel_thread_start_task(thread_start)
                done, _ = await asyncio.wait(
                    {thread_start},
                    timeout=_CLEANUP_TIMEOUT_SECONDS,
                )
                if thread_start not in done:
                    thread_start.add_done_callback(self._consume_task)

            if self._turn_start_sent and self._active_turn_id is None:
                cleanup = self._claim_start_cleanup()
                with suppress(Exception):
                    await asyncio.shield(cleanup)
                self._cancel_unresolved_start()

            interrupt = self._interrupt_task
            interrupt_turn_id = self._interrupt_turn_id
            if interrupt is None and self._active_turn_id is not None:
                method = self._method_for_cleanup(SemanticMethod.TURN_INTERRUPT)
                if method is not None and self._thread_id is not None:
                    interrupt = self._claim_interrupt_locked(
                        method,
                        self._thread_id,
                        self._active_turn_id,
                        settlement_error=_stable_error(_SESSION_CLOSED),
                    )
                    interrupt_turn_id = self._interrupt_turn_id
            if interrupt is not None:
                with suppress(Exception):
                    await asyncio.shield(interrupt)
                if interrupt_turn_id is not None and self._terminal_error is None:
                    self._settle_active(
                        error=_stable_error(_SESSION_CLOSED),
                        expected_turn_id=interrupt_turn_id,
                    )
            elif self._turn_start_sent:
                self._terminalize_unknown()

            if self._active_future is not None:
                self._settle_active(error=_stable_error(_SESSION_CLOSED))
            self._turn_starting = False
        finally:
            pump = self._pump_task
            if pump is not None:
                pump.cancel()
                done, _ = await asyncio.wait({pump}, timeout=_CLOSE_WAIT_SECONDS)
                if pump not in done:
                    pump.add_done_callback(self._consume_task)

    def _require_admission(self) -> None:
        admission = self._capabilities.agent_admission
        if type(admission) is not CapabilityState or type(admission.status) is not CapabilityStatus:
            raise _stable_error(_ADMISSION_UNAVAILABLE)
        if admission.status is not CapabilityStatus.AVAILABLE:
            raise _stable_error(_ADMISSION_UNAVAILABLE)
        if type(self._contract.agent_event_profile) is not AgentEventProfile:
            raise _stable_error(_ADMISSION_UNAVAILABLE)
        if self._profile is AgentProfileMode.INHERIT_CODEX:
            policy = self._capabilities.effective_policy
            policy_state = self._capabilities.policy_state
            if (
                type(policy_state) is not CapabilityState
                or policy_state.status is not CapabilityStatus.AVAILABLE
                or type(policy) is not EffectivePolicy
                or type(policy.sandbox) is not SandboxMode
                or type(policy.approval) is not ApprovalMode
                or is_unsafe_voice_policy(policy)
            ):
                raise _stable_error(_ADMISSION_UNAVAILABLE)

    def _require_open(self) -> None:
        if self._closed:
            raise _stable_error(_SESSION_CLOSED)
        if self._terminal_error is not None:
            raise self._terminal_error

    def _require_method(self, semantic: SemanticMethod) -> str:
        try:
            method = self._contract.require_method(semantic)
        except Exception:  # noqa: BLE001
            raise _stable_error(_CONTRACT_INVALID) from None
        expected = _METHOD_REQUIREMENTS.get(semantic)
        if (
            type(method) is not ClientMethodContract
            or expected is None
            or method.params_kind is not expected[0]
            or type(method.semantic_fields) is not frozenset
            or method.semantic_fields != expected[1]
            or not _transport_safe_text(method.name, max_bytes=_MAX_ID_BYTES, non_blank=True)
        ):
            raise _stable_error(_CONTRACT_INVALID)
        return method.name

    def _require_steer_method(self) -> str:
        steer = self._capabilities.steer
        if (
            type(steer) is not CapabilityState
            or type(steer.status) is not CapabilityStatus
            or steer.status is not CapabilityStatus.AVAILABLE
        ):
            raise _stable_error(_STEER_UNAVAILABLE)
        return self._require_method(SemanticMethod.TURN_STEER)

    def _method_for_cleanup(self, semantic: SemanticMethod) -> str | None:
        try:
            return self._require_method(semantic)
        except CodexAgentError:
            return None

    def _thread_params(self) -> dict[str, JsonValue]:
        params: dict[str, JsonValue] = {
            "cwd": str(self._working_directory),
            "ephemeral": True,
        }
        if self._profile is AgentProfileMode.READ_ONLY:
            params["sandbox"] = "read-only"
            params["approvalPolicy"] = "never"
        elif self._profile is AgentProfileMode.WORKSPACE_WRITE:
            params["sandbox"] = "workspace-write"
            params["approvalPolicy"] = "on-request"
        elif self._profile is not AgentProfileMode.INHERIT_CODEX:
            raise _stable_error(_CONTRACT_INVALID)
        return params

    async def _request(
        self,
        method: str,
        params: Mapping[str, JsonValue],
        failure_message: str,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        try:
            if request_timeout is None:
                return await self._connection.request(method, params)
            return await self._connection.request(
                method,
                params,
                request_timeout=request_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            if self._terminal_error is not None:
                raise self._terminal_error from None
            raise _stable_error(failure_message) from None

    def _parse_thread_id(self, result: JsonValue) -> str:
        if type(result) is not dict:
            raise _stable_error(_THREAD_RESULT_INVALID)
        thread = result.get("thread")
        if type(thread) is not dict:
            raise _stable_error(_THREAD_RESULT_INVALID)
        thread_id = thread.get("id")
        if not _valid_id(thread_id):
            raise _stable_error(_THREAD_RESULT_INVALID)
        return cast("str", thread_id)

    def _parse_turn_id(self, result: JsonValue) -> str:
        if type(result) is not dict:
            raise _stable_error(_TURN_RESULT_INVALID)
        turn = result.get("turn")
        if type(turn) is not dict:
            raise _stable_error(_TURN_RESULT_INVALID)
        turn_id = turn.get("id")
        if not _valid_id(turn_id):
            raise _stable_error(_TURN_RESULT_INVALID)
        return cast("str", turn_id)

    def _ensure_pump(self) -> None:
        if self._pump_task is not None:
            return
        self._require_open()
        try:
            stream = self._connection.notifications()
            if not callable(getattr(stream, "__aiter__", None)):
                error = _stable_error(_CONNECTION_LOST)
                self._terminal_error = error
                raise error from None  # noqa: TRY301
        except Exception:  # noqa: BLE001
            error = _stable_error(_CONNECTION_LOST)
            self._terminal_error = error
            raise error from None
        self._pump_task = asyncio.create_task(self._notification_pump(stream))

    async def _notification_pump(self, stream: AsyncIterator[RpcNotification]) -> None:
        try:
            async for notification in stream:
                if self._closed:
                    return
                try:
                    self._handle_notification(notification)
                except CodexAgentError as agent_error:
                    self._handle_notification_error(agent_error)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._handle_connection_end()
        else:
            self._handle_connection_end()
        finally:
            if self._pump_task is asyncio.current_task():
                self._pump_task = None

    def _handle_connection_end(self) -> None:
        if self._closed:
            return
        if self._turn_start_sent or self._active_turn_id is not None:
            self._terminalize_unknown()
            self._notify_terminal()
            return
        connection_error = _stable_error(_CONNECTION_LOST)
        self._terminal_error = connection_error
        self._settle_active(error=connection_error)
        self._notify_terminal()

    def _notify_terminal(self) -> None:
        if self._terminal_notified:
            return
        self._terminal_notified = True
        sink = self._terminal_sink
        if sink is None:
            return
        with suppress(Exception):
            sink()

    def _handle_notification_error(self, error: CodexAgentError) -> None:
        if error.code in {AgentTurnErrorCode.FAILED, AgentTurnErrorCode.INTERRUPTED}:
            self._settle_active(error=error)
            return

        thread_id = self._thread_id
        turn_id = self._active_turn_id
        if self._terminal_error is None and (
            self._turn_starting or self._turn_start_sent or turn_id is not None
        ):
            self._terminal_error = _turn_error(
                _UNKNOWN_OUTCOME,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            )
        if thread_id is not None and turn_id is not None:
            method = self._method_for_cleanup(SemanticMethod.TURN_INTERRUPT)
            if method is not None:
                with suppress(CodexAgentError):
                    self._claim_interrupt_locked(
                        method,
                        thread_id,
                        turn_id,
                        settlement_error=_stable_error(_UNKNOWN_OUTCOME),
                    )
        self._settle_active(
            error=_turn_error(str(error), AgentTurnErrorCode.OUTCOME_UNKNOWN),
            expected_turn_id=turn_id,
        )

    def _handle_notification(self, notification: RpcNotification) -> None:
        if type(notification) is not RpcNotification:
            raise _stable_error(_COMPLETION_INVALID)
        profile = self._event_profile()
        if profile is None:
            return
        relevant = {
            profile.turn_completed_method,
            profile.item_completed_method,
        }
        if profile.item_started_method is not None:
            relevant.add(profile.item_started_method)
        if profile.agent_message_delta_method is not None:
            relevant.add(profile.agent_message_delta_method)
        if notification.method not in relevant:
            return
        if self._active_future is None or self._active_turn_id is None:
            if self._turn_starting and self._turn_start_sent and self._thread_id is not None:
                self._buffer_turn_start_notification(notification, profile)
            return
        self._consume_notification(notification, profile)

    def _event_profile(self) -> AgentEventProfile | None:
        profile = self._contract.agent_event_profile
        return profile if type(profile) is AgentEventProfile else None

    def _buffer_turn_start_notification(
        self,
        notification: RpcNotification,
        profile: AgentEventProfile,
    ) -> None:
        if not self._turn_start_sent or self._thread_id is None:
            return
        params = notification.params
        if type(params) is not dict:
            raise _stable_error(_COMPLETION_INVALID)
        thread_id = params.get(profile.thread_id_field)
        if not _valid_id(thread_id):
            raise _stable_error(_COMPLETION_INVALID)
        if thread_id != self._thread_id:
            return
        try:
            encoded = json.dumps(
                {"method": notification.method, "params": params},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError):
            raise _stable_error(_COMPLETION_INVALID) from None
        size = len(encoded)
        if (
            len(self._turn_start_buffer) >= _MAX_TURN_START_BUFFER_ITEMS
            or self._turn_start_buffer_bytes + size > _MAX_TURN_START_BUFFER_BYTES
        ):
            self._terminalize_unknown(_TURN_START_BUFFER_OVERFLOW)
            raise _stable_error(_TURN_START_BUFFER_OVERFLOW)
        self._turn_start_buffer.append(notification)
        self._turn_start_buffer_bytes += size

    def _replay_turn_start_buffer(self) -> None:
        buffered = tuple(self._turn_start_buffer)
        self._turn_start_buffer.clear()
        self._turn_start_buffer_bytes = 0
        profile = self._event_profile()
        if profile is None:
            return
        for notification in buffered:
            if self._active_future is None:
                return
            try:
                self._consume_notification(notification, profile)
            except CodexAgentError as error:
                self._handle_notification_error(error)
                return

    def _consume_notification(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        notification: RpcNotification,
        profile: AgentEventProfile,
    ) -> None:
        params = notification.params
        if type(params) is not dict:
            raise _stable_error(_COMPLETION_INVALID)
        if not self._required_fields_valid(
            params,
            profile,
            "turn_completed"
            if notification.method == profile.turn_completed_method
            else "item_started"
            if notification.method == profile.item_started_method
            else "item_completed"
            if notification.method == profile.item_completed_method
            else "delta",
        ):
            raise _stable_error(_COMPLETION_INVALID)
        thread_id = params.get(profile.thread_id_field)
        if not _valid_id(thread_id):
            raise _stable_error(_COMPLETION_INVALID)
        if thread_id != self._thread_id:
            return

        if notification.method == profile.turn_completed_method:
            turn = params.get(profile.turn_field)
            if type(turn) is not dict or not self._nested_fields_valid(
                turn,
                profile.turn_required_fields,
                profile.turn_field_types,
            ):
                raise _stable_error(_COMPLETION_INVALID)
            turn_id = turn.get(profile.id_field)
            if not _valid_id(turn_id):
                raise _stable_error(_COMPLETION_INVALID)
            if turn_id != self._active_turn_id:
                return
            status = turn.get(profile.status_field)
            if type(status) is not str or status not in profile.turn_status_values:
                raise _stable_error(_COMPLETION_INVALID)
            if status == profile.failed_status:
                raise _stable_error(_TURN_FAILED)
            if status == profile.interrupted_status:
                raise _stable_error(_TURN_INTERRUPTED)
            if status == profile.completed_status:
                result = self._candidate_final or self._candidate_fallback
                if result is None:
                    raise _stable_error(_FINAL_UNAVAILABLE)
                self._settle_active(result=result, expected_turn_id=turn_id)
            return

        turn_id = params.get(profile.turn_id_field)
        if not _valid_id(turn_id):
            raise _stable_error(_COMPLETION_INVALID)
        if turn_id != self._active_turn_id:
            return
        if notification.method == profile.agent_message_delta_method:
            delta = params.get(profile.delta_field)
            item_id = params.get(profile.item_id_field)
            if not _valid_id(item_id) or not _transport_safe_text(
                delta,
                max_bytes=_MAX_AGENT_OUTPUT_BYTES,
                non_blank=False,
            ):
                raise _stable_error(_COMPLETION_INVALID)
            return

        item = params.get(profile.item_field)
        if type(item) is not dict:
            raise _stable_error(_COMPLETION_INVALID)
        item_id = item.get(profile.id_field)
        item_type = item.get(profile.type_field)
        if not _valid_id(item_id) or type(item_type) is not str or not item_type:
            raise _stable_error(_COMPLETION_INVALID)
        if notification.method == profile.item_started_method:
            if item_type != profile.agent_message_type:
                self._emit_activity(item_type, "started")
            return
        if item_type != profile.agent_message_type:
            self._emit_activity(item_type, "completed")
            return
        if not self._nested_fields_valid(
            item,
            profile.agent_message_required_fields,
            profile.agent_message_field_types,
        ):
            raise _stable_error(_COMPLETION_INVALID)
        text = item.get(profile.text_field)
        if not _transport_safe_text(text, max_bytes=_MAX_AGENT_OUTPUT_BYTES, non_blank=True):
            raise _stable_error(_COMPLETION_INVALID)
        phase = item.get(profile.phase_field)
        if phase is None:
            if not profile.agent_message_phase_optional:
                raise _stable_error(_COMPLETION_INVALID)
            self._candidate_fallback = cast("str", text)
        elif type(phase) is not str or phase not in profile.agent_message_phase_values:
            raise _stable_error(_COMPLETION_INVALID)
        elif phase == "final_answer":
            self._candidate_final = cast("str", text)

    def _emit_activity(self, item_type: str, phase: AgentActivityPhase) -> None:
        sink = self._activity_sink
        if sink is None:
            return
        event = AgentActivityEvent(_ITEM_ACTIVITY.get(item_type, "codex_work"), phase)
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - UI effect cannot change Agent outcome
            safe_event(
                logger,
                "agent_activity_effect_failed",
                component="codex",
                event_code="activity_sink_failed",
            )

    def _required_fields_valid(
        self,
        params: dict[str, JsonValue],
        profile: AgentEventProfile,
        event: str,
    ) -> bool:
        if event == "turn_completed":
            required = profile.turn_completed_required_fields
            types = profile.turn_completed_field_types
        elif event == "item_completed":
            required = profile.item_completed_required_fields
            types = profile.item_completed_field_types
        elif event == "item_started":
            required = profile.item_started_required_fields
            types = profile.item_started_field_types
        else:
            required = profile.agent_message_delta_required_fields
            types = profile.agent_message_delta_field_types
        return self._nested_fields_valid(params, required, types)

    @staticmethod
    def _nested_fields_valid(
        value: dict[str, JsonValue],
        required: frozenset[str],
        types: Mapping[str, frozenset[str]],
    ) -> bool:
        return all(
            field in value and _field_admits(value[field], types.get(field)) for field in required
        )

    def _claim_interrupt_locked(
        self,
        method: str,
        thread_id: str,
        turn_id: str,
        *,
        settlement_error: CodexAgentError,
    ) -> asyncio.Task[None]:
        if self._interrupt_task is not None:
            if (self._interrupt_thread_id, self._interrupt_turn_id) == (thread_id, turn_id):
                return self._interrupt_task
            if not self._interrupt_task.done():
                self._terminalize_unknown()
                raise _stable_error(_UNKNOWN_OUTCOME)
            self._retire_interrupt_claim()
        task = asyncio.create_task(
            self._send_interrupt(method, thread_id, turn_id, settlement_error)
        )
        self._interrupt_task = task
        self._interrupt_thread_id = thread_id
        self._interrupt_turn_id = turn_id
        task.add_done_callback(self._finish_interrupt_claim)
        return task

    async def _send_interrupt(
        self,
        method: str,
        thread_id: str,
        turn_id: str,
        settlement_error: CodexAgentError,
    ) -> None:
        try:
            await asyncio.wait_for(
                self._request(
                    method,
                    {"threadId": thread_id, "turnId": turn_id},
                    _UNKNOWN_OUTCOME,
                    request_timeout=_CLEANUP_TIMEOUT_SECONDS,
                ),
                _CLEANUP_TIMEOUT_SECONDS,
            )
            self._settle_active(error=settlement_error, expected_turn_id=turn_id)
        except asyncio.CancelledError:
            self._terminalize_unknown()
            raise
        except Exception:  # noqa: BLE001
            self._terminalize_unknown()
            raise _stable_error(_UNKNOWN_OUTCOME) from None

    async def _send_steer(
        self,
        method: str,
        thread_id: str,
        turn_id: str,
        text: str,
    ) -> None:
        try:
            result = await self._connection.request(
                method,
                {
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": text}],
                    "threadId": thread_id,
                },
            )
        except asyncio.CancelledError:
            self._terminalize_unknown()
            raise
        except (CodexRpcTimeoutError, CodexRpcProtocolError, CodexProcessExitedError):
            self._terminalize_unknown()
            raise self._terminal_error or _turn_error(
                _UNKNOWN_OUTCOME,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            ) from None
        except CodexRpcError as error:
            if self._terminal_error is not None:
                raise self._terminal_error from None
            if type(error.code) is int:
                raise _stable_error(_STEER_REJECTED, code=_STEER_REJECTED) from None
            self._terminalize_unknown()
            raise self._terminal_error or _turn_error(
                _UNKNOWN_OUTCOME,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            ) from None
        except Exception:  # noqa: BLE001
            self._terminalize_unknown()
            raise self._terminal_error or _turn_error(
                _UNKNOWN_OUTCOME,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            ) from None

        if self._terminal_error is not None:
            raise self._terminal_error
        if type(result) is not dict:
            self._terminalize_unknown()
            raise self._terminal_error or _turn_error(
                _UNKNOWN_OUTCOME,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            )
        response_turn_id = result.get("turnId")
        if not _valid_id(response_turn_id) or response_turn_id != turn_id:
            self._terminalize_unknown()
            raise self._terminal_error or _turn_error(
                _UNKNOWN_OUTCOME,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            )

    def _claim_start_cleanup(self) -> asyncio.Task[None]:
        start_task = self._turn_start_task
        if self._start_cleanup_task is not None:
            if self._start_cleanup_source is start_task:
                return self._start_cleanup_task
            if not self._start_cleanup_task.done():
                self._terminalize_unknown()
                raise _stable_error(_UNKNOWN_OUTCOME)
            self._retire_start_cleanup()
        task = asyncio.create_task(self._resolve_sent_turn_for_interrupt(start_task))
        self._start_cleanup_task = task
        self._start_cleanup_source = start_task
        task.add_done_callback(self._finish_start_cleanup)
        return task

    async def _resolve_sent_turn_for_interrupt(
        self,
        start_task: asyncio.Task[JsonValue] | None,
    ) -> None:
        if start_task is None:
            self._terminalize_unknown()
            return
        try:
            result = await asyncio.wait_for(
                asyncio.shield(start_task),
                _CLEANUP_TIMEOUT_SECONDS,
            )
            turn_id = self._parse_turn_id(result)
        except asyncio.CancelledError:
            self._terminalize_unknown()
            self._cancel_start_task(start_task)
            raise
        except Exception:  # noqa: BLE001
            self._terminalize_unknown()
            self._cancel_start_task(start_task)
            return

        async with self._state_lock:
            if self._turn_start_task is not start_task:
                self._terminalize_unknown()
                return
            thread_id = self._thread_id
            method = self._method_for_cleanup(SemanticMethod.TURN_INTERRUPT)
            interrupt: asyncio.Task[None] | None = None
            if thread_id is None or method is None:
                self._terminalize_unknown()
            else:
                with suppress(CodexAgentError):
                    interrupt = self._claim_interrupt_locked(
                        method,
                        thread_id,
                        turn_id,
                        settlement_error=_stable_error(_TURN_INTERRUPTED),
                    )
            self._turn_starting = False
            self._turn_start_sent = False
            self._turn_start_task = None
            self._turn_start_buffer.clear()
            self._turn_start_buffer_bytes = 0
        with suppress(Exception):
            if interrupt is not None:
                await asyncio.shield(interrupt)

    async def _handle_cancellation(
        self,
        turn_task: asyncio.Task[JsonValue] | None,
        thread_start_task: asyncio.Task[JsonValue] | None,
    ) -> None:
        if self._active_turn_id is not None and self._thread_id is not None:
            thread_id = self._thread_id
            turn_id = self._active_turn_id
            method = self._method_for_cleanup(SemanticMethod.TURN_INTERRUPT)
            if method is not None:
                try:
                    async with self._state_lock:
                        interrupt = self._claim_interrupt_locked(
                            method,
                            thread_id,
                            turn_id,
                            settlement_error=_stable_error(_TURN_INTERRUPTED),
                        )
                except CodexAgentError:
                    return
                try:
                    await asyncio.shield(interrupt)
                except CodexAgentError:
                    return
                self._settle_active(
                    error=_stable_error(_TURN_INTERRUPTED),
                    expected_turn_id=turn_id,
                )
            return
        if self._turn_start_sent:
            cleanup = self._claim_start_cleanup()
            with suppress(Exception):
                await asyncio.shield(cleanup)
            return
        if thread_start_task is not None:
            self._cancel_thread_start_task(thread_start_task)
        self._turn_starting = False
        self._turn_start_buffer.clear()
        self._turn_start_buffer_bytes = 0
        if turn_task is not None and not turn_task.done():
            turn_task.add_done_callback(self._consume_task)

    def _terminalize_unknown(self, message: str = _UNKNOWN_OUTCOME) -> None:
        error = _turn_error(message, AgentTurnErrorCode.OUTCOME_UNKNOWN)
        if self._terminal_error is None:
            self._terminal_error = error
        self._settle_active(error=self._terminal_error)

    def _cancel_start_task(self, task: asyncio.Task[JsonValue]) -> None:
        if not task.done():
            task.cancel()
            task.add_done_callback(self._consume_task)
        if self._turn_start_task is task:
            self._turn_start_task = None
        self._turn_start_sent = False
        self._turn_starting = False

    def _cancel_unresolved_start(self) -> None:
        task = self._turn_start_task
        if task is not None:
            self._cancel_start_task(task)

    def _cancel_thread_start_task(self, task: asyncio.Task[JsonValue]) -> None:
        if not task.done():
            task.cancel()

    def _finish_thread_start(self, task: asyncio.Task[JsonValue]) -> None:
        self._consume_task(task)
        if self._thread_start_task is task:
            self._thread_start_task = None

    def _retire_start_cleanup(self) -> None:
        self._start_cleanup_task = None
        self._start_cleanup_source = None

    def _retire_interrupt_claim(self) -> None:
        task = self._interrupt_task
        if task is None or not task.done():
            return
        self._interrupt_task = None
        self._interrupt_thread_id = None
        self._interrupt_turn_id = None

    def _retire_steer_claim(self) -> None:
        task = self._steer_task
        if task is None or not task.done():
            return
        self._steer_task = None

    async def _cancel_and_recover_steer(self, task: asyncio.Task[None]) -> bool:
        if not task.done():
            task.cancel()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _CLEANUP_TIMEOUT_SECONDS
            while not task.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait({task}, timeout=remaining)
                except asyncio.CancelledError:
                    continue
        known_outcome = task.done() and not task.cancelled()
        if task.done():
            self._consume_task(task)
        if self._steer_task is task:
            self._steer_task = None
        return known_outcome

    def _finish_steer_claim(self, task: asyncio.Task[None]) -> None:
        self._consume_task(task)
        if self._steer_task is task:
            self._retire_steer_claim()

    def _finish_interrupt_claim(self, task: asyncio.Task[None]) -> None:
        self._consume_task(task)
        if self._interrupt_task is task:
            self._retire_interrupt_claim()

    def _settle_active(
        self,
        *,
        result: str | None = None,
        error: CodexAgentError | None = None,
        expected_turn_id: str | None = None,
    ) -> bool:
        if expected_turn_id is not None and self._active_turn_id != expected_turn_id:
            return False
        self._retire_interrupt_claim()
        future = self._active_future
        self._active_future = None
        self._active_turn_id = None
        self._candidate_final = None
        self._candidate_fallback = None
        if future is None:
            return False
        if future.done():
            return True
        if error is not None:
            future.set_exception(error)
        elif result is not None:
            future.set_result(result)
        else:  # pragma: no cover - every caller supplies a terminal outcome
            future.set_exception(_stable_error(_FINAL_UNAVAILABLE))
        return True

    @staticmethod
    def _consume_task[T](task: asyncio.Task[T]) -> None:
        with suppress(BaseException):
            task.exception()

    def _finish_start_cleanup(self, task: asyncio.Task[None]) -> None:
        self._consume_task(task)
        if self._start_cleanup_task is task:
            self._retire_start_cleanup()
