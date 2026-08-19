from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from moco.errors import AgentTurnErrorCode, CodexAgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


_TURN_ERROR_CODES = frozenset(code.value for code in AgentTurnErrorCode)


async def _resolved_handoff(value: HandoffDisposition) -> HandoffDisposition:
    return value


async def _settle_handoff(
    task: asyncio.Task[HandoffDisposition],
) -> HandoffDisposition:
    return await asyncio.shield(task)


class ConnectionState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


class VoiceState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"


class TaskState(StrEnum):
    NONE = "none"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class SpeechState(StrEnum):
    SILENT = "silent"
    SYNTHESIZING = "synthesizing"
    PLAYING = "playing"


class HandoffDisposition(StrEnum):
    STARTED = "started"
    STEERED = "steered"
    QUEUED = "queued"
    REJECTED = "rejected"
    BUSY = "busy"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class InteractionSnapshot:
    connection: ConnectionState
    voice: VoiceState
    task: TaskState
    speech: SpeechState

    @property
    def idle(self) -> bool:
        return (
            self.connection in {ConnectionState.READY, ConnectionState.DEGRADED}
            and self.voice is VoiceState.IDLE
            and self.task
            in {TaskState.NONE, TaskState.COMPLETED, TaskState.FAILED, TaskState.INTERRUPTED}
            and self.speech is SpeechState.SILENT
        )


@dataclass(frozen=True, slots=True)
class TurnResult:
    final_answer: str | None
    error_code: str | None

    def __post_init__(self) -> None:
        if (self.final_answer is None) == (self.error_code is None):
            message = "exactly one turn result value is required"
            raise ValueError(message)
        if self.final_answer is not None and type(self.final_answer) is not str:
            message = "turn result final answer must be a string"
            raise ValueError(message)
        code = self.error_code
        if code is None:
            return
        if isinstance(code, AgentTurnErrorCode):
            canonical_code = code.value
        elif type(code) is str and code in _TURN_ERROR_CODES:
            canonical_code = code
        else:
            message = "turn result requires a stable terminal error code"
            raise ValueError(message)
        object.__setattr__(self, "error_code", canonical_code)


class InteractionEffects(Protocol):
    def on_snapshot_changed(self, snapshot: InteractionSnapshot) -> None: ...

    def on_turn_terminal_claimed(self) -> None: ...

    def on_turn_finished(self, result: TurnResult) -> None: ...

    def on_submission_error(self, code: str) -> None: ...


class _AgentSession(Protocol):
    @property
    def reusable(self) -> bool: ...

    async def start_turn(self, text: str) -> str: ...

    async def steer(self, text: str) -> None: ...


class InteractionCoordinator:
    def __init__(
        self,
        session: _AgentSession | None,
        *,
        steer_available: bool,
        effects: InteractionEffects,
    ) -> None:
        self._session = session
        self._steer_available = steer_available
        self._effects = effects
        self._listen_generation = 0
        self._consumed_listen_generation = 0
        self._latest_utterance_id = 0
        self._turn_generation = 0
        self._terminal_generation = 0
        self._queued_text: str | None = None
        self._deferred_terminal_state: TaskState | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._steer_task: asyncio.Task[HandoffDisposition] | None = None
        self._cancel_claimed = False
        self._realtime_turn_id: str | None = None
        self._realtime_turn_cancelled = False
        self._snapshot = InteractionSnapshot(
            connection=ConnectionState.STARTING,
            voice=VoiceState.IDLE,
            task=TaskState.NONE,
            speech=SpeechState.SILENT,
        )

    @property
    def snapshot(self) -> InteractionSnapshot:
        return self._snapshot

    @property
    def idle(self) -> bool:
        return self._snapshot.idle

    def connection_changed(self, state: ConnectionState) -> None:
        self._replace_snapshot(connection=state)

    def listen_started(self) -> None:
        if self._snapshot.voice is VoiceState.IDLE:
            self._listen_generation += 1
            self._replace_snapshot(voice=VoiceState.LISTENING)

    def listen_stopped(self) -> None:
        if self._snapshot.voice is VoiceState.LISTENING:
            self._replace_snapshot(voice=VoiceState.IDLE)

    def voice_lost(self) -> None:
        self._consumed_listen_generation = self._listen_generation
        if self._snapshot.voice is not VoiceState.IDLE:
            self._replace_snapshot(voice=VoiceState.IDLE)

    def consume_user_final(  # noqa: C901, PLR0911
        self,
        text: str,
        *,
        utterance_id: int | None = None,
    ) -> Coroutine[object, object, HandoffDisposition]:
        if self._session is None:
            return _resolved_handoff(HandoffDisposition.IGNORED)
        voice = self._snapshot.voice
        stopped_final = voice is VoiceState.IDLE and self._listen_generation not in {
            0,
            self._consumed_listen_generation,
        }
        if (voice is not VoiceState.LISTENING and not stopped_final) or not self._claim_utterance(
            utterance_id
        ):
            return _resolved_handoff(HandoffDisposition.IGNORED)
        if stopped_final:
            self._consumed_listen_generation = self._listen_generation

        if self._cancel_claimed:
            self._emit(self._effects.on_submission_error, "interaction_busy")
            return _resolved_handoff(HandoffDisposition.BUSY)

        if self._turn_task is None and self._deferred_terminal_state is not None:
            if self._queued_text is not None:
                self._emit(self._effects.on_submission_error, "interaction_busy")
                return _resolved_handoff(HandoffDisposition.BUSY)
            self._queued_text = text
            self._replace_snapshot(task=TaskState.QUEUED)
            return _resolved_handoff(HandoffDisposition.QUEUED)

        if self._turn_task is None:
            self._claim_turn(text)
            return _resolved_handoff(HandoffDisposition.STARTED)

        if self._queued_text is not None:
            self._emit(self._effects.on_submission_error, "interaction_busy")
            return _resolved_handoff(HandoffDisposition.BUSY)

        if self._snapshot.task is TaskState.RUNNING and self._steer_available:
            if self._steer_task is not None and not self._steer_task.done():
                self._emit(self._effects.on_submission_error, "interaction_busy")
                return _resolved_handoff(HandoffDisposition.BUSY)
            generation = self._turn_generation
            task = asyncio.create_task(self._run_steer(generation, text))
            self._steer_task = task
            return _settle_handoff(task)

        self._queued_text = text
        return _resolved_handoff(HandoffDisposition.QUEUED)

    def _claim_utterance(self, utterance_id: int | None) -> bool:
        if utterance_id is None:
            return True
        if type(utterance_id) is not int or utterance_id <= 0:
            raise ValueError
        if utterance_id <= self._latest_utterance_id:
            return False
        self._latest_utterance_id = utterance_id
        return True

    def review_count_changed(self, count: int) -> None:
        if count < 0:
            raise ValueError
        if self._turn_task is None and self._realtime_turn_id is None:
            return
        self._replace_snapshot(
            task=TaskState.WAITING_REVIEW if count else TaskState.RUNNING,
        )

    def realtime_turn_started(self, turn_id: str) -> None:
        if not turn_id:
            raise ValueError
        if self._realtime_turn_id == turn_id:
            return
        if self._realtime_turn_id is not None or self._turn_task is not None:
            self._emit(self._effects.on_submission_error, "interaction_busy")
            return
        self._realtime_turn_id = turn_id
        self._realtime_turn_cancelled = False
        self._replace_snapshot(task=TaskState.RUNNING)

    def realtime_turn_cancel_requested(self, turn_id: str) -> None:
        if turn_id == self._realtime_turn_id:
            self._realtime_turn_cancelled = True
            self._emit(self._effects.on_turn_terminal_claimed)

    def realtime_turn_completed(self, turn_id: str) -> None:
        if turn_id != self._realtime_turn_id:
            return
        terminal = TaskState.INTERRUPTED if self._realtime_turn_cancelled else TaskState.COMPLETED
        self._realtime_turn_id = None
        self._realtime_turn_cancelled = False
        self._replace_snapshot(task=terminal)

    async def cancel_turn(self) -> bool:
        turn_task = self._turn_task
        steer_task = self._steer_task
        running_barrier = self._is_running_steer_barrier(turn_task, steer_task)
        if (turn_task is None and not running_barrier) or self._cancel_claimed:
            return False
        generation = self._turn_generation
        self._cancel_claimed = True
        self._queued_text = None
        caller_cancellation = await self._cancel_and_drain_task(steer_task)

        if running_barrier:
            try:
                self._settle_terminal_steer(
                    generation,
                    reusable=self._session_reusable(),
                )
            finally:
                self._cancel_claimed = False
            self._propagate_caller_cancellation(caller_cancellation)
            return True

        turn_task = cast("asyncio.Task[None]", turn_task)
        if generation != self._turn_generation or generation == self._terminal_generation:
            self._propagate_caller_cancellation(caller_cancellation)
            return True
        if not self._session_reusable():
            self._finish_turn(
                generation,
                TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
            )
            if not turn_task.done():
                turn_task.cancel()
            self._propagate_caller_cancellation(caller_cancellation)
            return True

        if not turn_task.done():
            turn_cancellation = await self._cancel_and_drain_task(turn_task)
            caller_cancellation = caller_cancellation or turn_cancellation
        if generation != self._turn_generation or generation == self._terminal_generation:
            self._propagate_caller_cancellation(caller_cancellation)
            return True
        result = TurnResult(final_answer=None, error_code=AgentTurnErrorCode.INTERRUPTED)
        if not self._session_reusable():
            result = TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN)
        self._finish_turn(generation, result)
        self._propagate_caller_cancellation(caller_cancellation)
        return True

    def _is_running_steer_barrier(
        self,
        turn_task: asyncio.Task[None] | None,
        steer_task: asyncio.Task[HandoffDisposition] | None,
    ) -> bool:
        return (
            turn_task is None
            and self._deferred_terminal_state is not None
            and self._snapshot.task is TaskState.RUNNING
            and steer_task is not None
            and not steer_task.done()
        )

    @staticmethod
    async def _cancel_and_drain_task(
        task: asyncio.Task[None] | asyncio.Task[HandoffDisposition] | None,
    ) -> asyncio.CancelledError | None:
        if task is None or task.done():
            return None
        task.cancel()
        current = asyncio.current_task()
        first_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            cancellation_count = 0 if current is None else current.cancelling()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if (
                    first_cancellation is None
                    and current is not None
                    and current.cancelling() > cancellation_count
                ):
                    first_cancellation = error
        return first_cancellation

    @staticmethod
    def _propagate_caller_cancellation(error: asyncio.CancelledError | None) -> None:
        if error is not None:
            raise error

    def connection_lost(self) -> None:
        self._queued_text = None
        self._consumed_listen_generation = self._listen_generation
        deferred_terminal_state = self._deferred_terminal_state
        self._deferred_terminal_state = None
        steer_task = self._steer_task
        if steer_task is not None:
            steer_task.cancel()
        turn_task = self._turn_task
        if self._realtime_turn_id is not None:
            self._realtime_turn_id = None
            self._realtime_turn_cancelled = False
            self._replace_snapshot(
                connection=ConnectionState.DISCONNECTED,
                voice=VoiceState.IDLE,
                task=TaskState.FAILED,
            )
            return
        if turn_task is None:
            self._replace_snapshot(
                connection=ConnectionState.DISCONNECTED,
                voice=VoiceState.IDLE,
                task=deferred_terminal_state,
            )
            return
        generation = self._turn_generation
        self._snapshot = InteractionSnapshot(
            connection=ConnectionState.DISCONNECTED,
            voice=VoiceState.IDLE,
            task=self._snapshot.task,
            speech=self._snapshot.speech,
        )
        self._finish_turn(
            generation,
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
        )
        if not turn_task.done():
            turn_task.cancel()

    def speech_changed(self, state: SpeechState) -> None:
        self._replace_snapshot(speech=state)

    def _claim_turn(
        self,
        text: str,
        *,
        voice: VoiceState | None = None,
        publish_snapshot: bool = True,
    ) -> None:
        self._turn_generation += 1
        generation = self._turn_generation
        self._cancel_claimed = False
        task = asyncio.create_task(self._run_turn(generation, text))
        self._turn_task = task
        snapshot = InteractionSnapshot(
            connection=self._snapshot.connection,
            voice=self._snapshot.voice if voice is None else voice,
            task=TaskState.RUNNING,
            speech=self._snapshot.speech,
        )
        changed = snapshot != self._snapshot
        self._snapshot = snapshot
        if publish_snapshot and changed:
            self._emit(self._effects.on_snapshot_changed, snapshot)

    async def _run_turn(self, generation: int, text: str) -> None:
        session = cast("_AgentSession", self._session)
        try:
            final_answer = await session.start_turn(text)
        except CodexAgentError as error:
            if error.code not in {
                AgentTurnErrorCode.FAILED,
                AgentTurnErrorCode.INTERRUPTED,
                AgentTurnErrorCode.OUTCOME_UNKNOWN,
            }:
                self._reject_turn_submission(generation)
                return
            result = self._map_agent_error(error)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            result = TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN)
        else:
            if type(final_answer) is str:
                result = TurnResult(final_answer=final_answer, error_code=None)
            else:
                result = TurnResult(
                    final_answer=None,
                    error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN,
                )
        self._finish_turn(generation, result)

    async def _run_steer(self, generation: int, text: str) -> HandoffDisposition:
        # A locally claimed turn can be RUNNING one loop tick before its remote turn/start arrives.
        await asyncio.sleep(0)
        session = cast("_AgentSession", self._session)
        try:
            await session.steer(text)
        except CodexAgentError:
            if self._session_reusable():
                effect_eligible = generation == self._turn_generation
                self._settle_terminal_steer(generation, reusable=True)
                self._retire_current_steer_claim()
                if effect_eligible:
                    self._emit(self._effects.on_submission_error, "agent_steer_rejected")
                return HandoffDisposition.REJECTED
            if not self._settle_terminal_steer(generation, reusable=False):
                self._finish_unknown_steer(generation)
            return HandoffDisposition.REJECTED
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            if not self._settle_terminal_steer(generation, reusable=False):
                self._finish_unknown_steer(generation)
            return HandoffDisposition.REJECTED
        finally:
            self._retire_current_steer_claim()
        self._settle_terminal_steer(generation)
        return HandoffDisposition.STEERED

    def _settle_terminal_steer(
        self,
        generation: int,
        *,
        reusable: bool | None = None,
    ) -> bool:
        terminal_state = self._deferred_terminal_state
        if generation != self._turn_generation or terminal_state is None:
            return False
        self._deferred_terminal_state = None
        next_text = self._queued_text
        self._queued_text = None
        session_reusable = self._session_reusable() if reusable is None else reusable
        if session_reusable:
            if next_text is not None:
                self._claim_turn(next_text)
            else:
                snapshot = InteractionSnapshot(
                    connection=self._snapshot.connection,
                    voice=self._snapshot.voice,
                    task=terminal_state,
                    speech=self._snapshot.speech,
                )
                if snapshot != self._snapshot:
                    self._snapshot = snapshot
                    self._emit(self._effects.on_snapshot_changed, snapshot)
            return True
        snapshot = InteractionSnapshot(
            connection=ConnectionState.DISCONNECTED,
            voice=self._snapshot.voice,
            task=terminal_state,
            speech=self._snapshot.speech,
        )
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self._emit(self._effects.on_snapshot_changed, snapshot)
        self._emit(self._effects.on_submission_error, AgentTurnErrorCode.OUTCOME_UNKNOWN.value)
        return True

    def _finish_unknown_steer(self, generation: int) -> None:
        turn_task = self._turn_task
        self._finish_turn(
            generation,
            TurnResult(final_answer=None, error_code=AgentTurnErrorCode.OUTCOME_UNKNOWN),
        )
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()

    def _reject_turn_submission(self, generation: int) -> None:
        if generation != self._turn_generation or generation == self._terminal_generation:
            return
        self._turn_task = None
        self._queued_text = None
        self._snapshot = InteractionSnapshot(
            connection=self._snapshot.connection,
            voice=self._snapshot.voice,
            task=TaskState.NONE,
            speech=self._snapshot.speech,
        )
        self._emit(self._effects.on_snapshot_changed, self._snapshot)
        self._emit(self._effects.on_submission_error, "agent_submission_rejected")

    def _finish_turn(self, generation: int, result: TurnResult) -> None:
        if generation != self._turn_generation or generation == self._terminal_generation:
            return
        self._terminal_generation = generation
        current_task = asyncio.current_task()
        steer_task = self._steer_task
        pending_steer = (
            steer_task
            if steer_task is not None and steer_task is not current_task and not steer_task.done()
            else None
        )
        defer_for_steer = (
            pending_steer is not None
            and result.error_code != AgentTurnErrorCode.OUTCOME_UNKNOWN
            and not self._cancel_claimed
        )
        self._turn_task = None
        if defer_for_steer:
            terminal_state = self._terminal_state(result)
            self._deferred_terminal_state = terminal_state
            self._cancel_claimed = False
            self._snapshot = InteractionSnapshot(
                connection=self._snapshot.connection,
                voice=self._snapshot.voice,
                task=TaskState.QUEUED if self._queued_text is not None else TaskState.RUNNING,
                speech=self._snapshot.speech,
            )
            self._emit(self._effects.on_turn_terminal_claimed)
            self._emit(self._effects.on_snapshot_changed, self._snapshot)
            self._emit(self._effects.on_turn_finished, result)
            return

        self._steer_task = None
        if pending_steer is not None and pending_steer.cancelling() == 0:
            pending_steer.cancel()

        next_text = self._queued_text
        self._queued_text = None
        promote = (
            next_text is not None
            and result.error_code != AgentTurnErrorCode.OUTCOME_UNKNOWN
            and not self._cancel_claimed
            and self._session_reusable()
        )
        self._cancel_claimed = False
        if promote:
            previous_snapshot = self._snapshot
            self._claim_turn(cast("str", next_text), publish_snapshot=False)
        else:
            terminal_state = self._terminal_state(result)
            connection = self._snapshot.connection
            if result.error_code == AgentTurnErrorCode.OUTCOME_UNKNOWN or (
                not self._session_reusable() and result.error_code is not None
            ):
                connection = ConnectionState.DISCONNECTED
            self._snapshot = InteractionSnapshot(
                connection=connection,
                voice=self._snapshot.voice,
                task=terminal_state,
                speech=self._snapshot.speech,
            )

        self._emit(self._effects.on_turn_terminal_claimed)
        if not promote or self._snapshot != previous_snapshot:
            self._emit(self._effects.on_snapshot_changed, self._snapshot)
        self._emit(self._effects.on_turn_finished, result)

    def _map_agent_error(self, error: CodexAgentError) -> TurnResult:
        code = error.code
        if code is AgentTurnErrorCode.INTERRUPTED or code == AgentTurnErrorCode.INTERRUPTED:
            stable_code = AgentTurnErrorCode.INTERRUPTED
        elif (
            code is AgentTurnErrorCode.FAILED or code == AgentTurnErrorCode.FAILED
        ) and self._session_reusable():
            stable_code = AgentTurnErrorCode.FAILED
        else:
            stable_code = AgentTurnErrorCode.OUTCOME_UNKNOWN
        return TurnResult(final_answer=None, error_code=stable_code)

    @staticmethod
    def _terminal_state(result: TurnResult) -> TaskState:
        if result.final_answer is not None:
            return TaskState.COMPLETED
        if result.error_code == AgentTurnErrorCode.INTERRUPTED:
            return TaskState.INTERRUPTED
        return TaskState.FAILED

    def _session_reusable(self) -> bool:
        # Re-read after async boundaries; the session can become terminal between checks.
        return self._session is not None and self._session.reusable

    def _replace_snapshot(
        self,
        *,
        connection: ConnectionState | None = None,
        voice: VoiceState | None = None,
        task: TaskState | None = None,
        speech: SpeechState | None = None,
    ) -> None:
        snapshot = InteractionSnapshot(
            connection=self._snapshot.connection if connection is None else connection,
            voice=self._snapshot.voice if voice is None else voice,
            task=self._snapshot.task if task is None else task,
            speech=self._snapshot.speech if speech is None else speech,
        )
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        self._emit(self._effects.on_snapshot_changed, snapshot)

    def _retire_current_steer_claim(self) -> None:
        current = asyncio.current_task()
        if self._steer_task is current:
            self._steer_task = None

    @staticmethod
    def _emit(callback: Callable[..., object], *args: object) -> None:
        try:
            returned = callback(*args)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return
        try:
            if not inspect.isawaitable(returned):
                return
            if isinstance(returned, asyncio.Future):
                if not returned.done():
                    returned.cancel()
                InteractionCoordinator._consume_effect_future(returned)
                return
            cleanup = getattr(returned, "close", None)
            if not callable(cleanup):
                cleanup = getattr(returned, "cancel", None)
            if callable(cleanup):
                cleanup()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _consume_effect_future(future: asyncio.Future[object]) -> None:
        if not future.done():
            future.add_done_callback(InteractionCoordinator._consume_effect_future)
            return
        try:
            future.exception()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return
