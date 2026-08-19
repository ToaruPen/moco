"""The state owner between one Codex approval request and the single trusted local reviewer.

An approval arrives as a server request the app server is waiting on. The broker reads it
through the typed adapter beside this module, publishes the resulting immutable review to
whichever reviewer connection currently holds the local slot, and waits. What the reviewer
answers is one of the decisions that request itself offered, named by an opaque handle this
broker issued, and nothing else: not the app server's request id, not a method, not a JSON
body of the reviewer's choosing. The handle is single-use and bound to the one connection
that was shown the review, so a second click, a late answer, a replayed handle, or a handle
offered by another connection has nothing to resolve.

A review ends exactly once, in one of five ways: the trusted reviewer decides, that reviewer
disconnects, the awaiting handler is cancelled, the app-server connection is lost, or the
broker closes. moco adds no deadline of its own to a request the app server set none on.
Every ending other than a decision fails closed - the awaiting handler raises rather than
inventing an accept or a decline for someone. An ending the reviewer did not choose takes
the review back: one the reviewer never read is destroyed unread, and one it was already
shown is withdrawn by handle so the screen holding it can close.

The transport stays where it already is. This module never writes JSON-RPC: it returns the
plain response object to the app server's own request handler, and `RpcPeer` remains the one
owner of answering a request id exactly once. Patch notifications retain only bounded typed
path/kind metadata until the correlated approval consumes it; raw params and diffs are never
stored. Once published, the handle, the approval details, and the decision live only in the
review value and the reviewer's own screen.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self, cast

from moco.codex.approval import (
    ApprovalDecision,
    CommandApprovalReview,
    FileChangeApprovalReview,
    FileChangeExplanation,
    ThreadItemCorrelation,
    adapt_approval_request,
    adapt_file_change_patch_notification,
)
from moco.codex.schema import (
    STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES,
    ServerRequestCategory,
    _is_transport_safe,
)
from moco.errors import CodexReviewError, CodexRpcProtocolError

if TYPE_CHECKING:
    from collections.abc import Callable

    from moco.codex.approval import ApprovalReview
    from moco.codex.rpc import JsonValue, RpcNotification, RpcServerRequest, RpcServerRequestHandler
    from moco.codex.schema import CodexProtocolContract

__all__ = [
    "ApprovalHandlerRegistrar",
    "InteractionBroker",
    "ReviewEnvelope",
    "ReviewWithdrawal",
    "ReviewerConnection",
]

# Every refusal names the state that refused, never the request that was refused.
_NO_REVIEWER = "no local reviewer is connected"
_REVIEWER_TAKEN = "the local reviewer slot is already held"
_UNKNOWN_REVIEWER = "that value is not a local reviewer connection"
_UNKNOWN_HANDLE = "the local review handle is not pending"
_UNSUPPORTED_DECISION = "that value is not a local review decision"
_REVIEWER_GONE = "the local reviewer disconnected"
_CONNECTION_LOST = "the Codex connection was lost"
_BROKER_CLOSED = "the local review broker is closed"
_TOO_MANY_REVIEWS = "too many local reviews are pending"
_UNUSABLE_HANDLE = "a local review handle could not be issued"
_UNEXPLAINED_CHANGE = "the pending file change could not be explained"
_UNADAPTABLE_CONTRACT = "this Codex build advertises an approval moco cannot read"
_UNUSABLE_ENVELOPE = "a local review cannot be published"
_REVIEW_CANCELLED = "the local review was cancelled"
_COUNT_CALLBACK_BOUND = "the pending review callback is already bound"
_COUNT_CALLBACK_LATE = "the pending review callback must be bound before reviewer use"
_COUNT_CALLBACK_ASYNC = "the pending review callback must be synchronous"
_ACTIVE_TURN_CALLBACK_BOUND = "the active turn callback is already bound"
_ACTIVE_TURN_CALLBACK_LATE = "the active turn callback must be bound before reviewer use"
_ACTIVE_TURN_CALLBACK_ASYNC = "the active turn callback must be synchronous"
_INACTIVE_TURN = "the local review does not belong to the active Agent turn"

# How many approvals one turn may hold open, and how many items may wait unread on one
# reviewer's stream. A build that asks for more is refused rather than queued without end.
# A review that ends leaves the stream with it, so only live reviews and the withdrawals of
# reviews a reviewer has already read occupy the second bound; both only stop unbounded
# growth, and no observed turn approaches either.
_MAX_PENDING_REVIEWS = 64
_MAX_UNREAD_REVIEWS = 64
_MAX_FILE_CHANGE_EXPLANATIONS = 64
_MAX_TERMINAL_TURNS = 64
# How much fresh randomness one handle carries, and the longest handle any source may hand
# back. The generated value is far shorter than the bound; the bound holds an injected one.
_HANDLE_BYTES = 32
_MAX_HANDLE_CHARACTERS = 128
# How many times one publication may ask its source for a handle. Two independent 32-byte
# values repeating is not something a working source does, so a few attempts absorb the
# arithmetically possible collision while a source stuck on one value still stops here.
_MAX_HANDLE_ATTEMPTS = 4

_STREAM_END = object()
_REVIEW_KINDS = frozenset({CommandApprovalReview, FileChangeApprovalReview})

# What one reviewer reads: the reviews it has not taken yet, the withdrawals of reviews it
# already took, and the one marker that ends the stream.
type _ReviewStream = asyncio.Queue[ReviewEnvelope | ReviewWithdrawal | object]


class ApprovalHandlerRegistrar(Protocol):
    """What a peer or a connection supervisor already offers before it is started."""

    def register_server_request_handler(
        self,
        method: str,
        handler: RpcServerRequestHandler,
    ) -> None: ...

    def register_notification_observer(
        self,
        observer: Callable[[RpcNotification], None],
    ) -> None: ...

    def register_terminal_callback(self, callback: Callable[[], None]) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class ReviewEnvelope:
    """One pending review, as the trusted reviewer receives it.

    The handle is the only name the reviewer answers with, and it says nothing about the
    request it opens. The review beside it is the immutable value the adapter built, which
    is where the details a reviewer must weigh live and the only place they live.
    """

    handle: str
    review: ApprovalReview

    def __post_init__(self) -> None:
        _require_handle(self.handle, _UNUSABLE_ENVELOPE)
        if type(cast("object", self.review)) not in _REVIEW_KINDS:
            raise CodexReviewError(_UNUSABLE_ENVELOPE)

    def __repr__(self) -> str:
        return f"ReviewEnvelope(category={self.review.category.value})"


@dataclass(frozen=True, slots=True, repr=False)
class ReviewWithdrawal:
    """One review the reviewer was shown and may no longer decide.

    A review can end without the reviewer: the awaiting handler may be cancelled while the
    approval is still on screen. The reviewer is told by handle alone, which is the only
    name it was ever given, so the screen holding that review can close itself. Nothing else
    is carried, because the reason a review ended is not the reviewer's to see, and the
    details it was shown are already in its hands or already gone.
    """

    handle: str

    def __post_init__(self) -> None:
        _require_handle(self.handle, _UNUSABLE_ENVELOPE)

    def __repr__(self) -> str:
        return "ReviewWithdrawal()"


class ReviewerConnection:
    """One trusted local reviewer: its identity, and the reviews published to it.

    The value itself is the identity. It is compared by object identity alone, so no
    identifier an untrusted client could spell names a reviewer, and a decision can only be
    offered by the connection that was actually shown the review. Iterating it yields each
    published review once and stops when the broker ends this reviewer.
    """

    __slots__ = ("_ended", "_stream")

    def __init__(self, stream: _ReviewStream) -> None:
        self._stream = stream
        self._ended = False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ReviewEnvelope | ReviewWithdrawal:
        if self._ended:
            raise StopAsyncIteration
        published = await self._stream.get()
        if published is _STREAM_END:
            self._ended = True
            raise StopAsyncIteration
        return cast("ReviewEnvelope | ReviewWithdrawal", published)

    def __repr__(self) -> str:
        return "ReviewerConnection()"


@dataclass(frozen=True, slots=True)
class _ReviewerSlot:
    """The one reviewer this process is talking to, and the stream it reads."""

    connection: ReviewerConnection
    stream: _ReviewStream


@dataclass(slots=True)
class _PendingReview:
    """One published review: what was shown, who was shown it, and who is waiting."""

    handle: str
    connection: ReviewerConnection
    review: ApprovalReview
    future: asyncio.Future[JsonValue]
    _withdrawn: bool = False

    def claim_withdrawal(self) -> bool:
        if self._withdrawn:
            return False
        self._withdrawn = True
        return True


class InteractionBroker:
    """Publishes each Codex approval to the trusted reviewer and answers it exactly once."""

    def __init__(
        self,
        contract: CodexProtocolContract,
        *,
        _handles: Callable[[], str] | None = None,
    ) -> None:
        """Own the reviews of one app-server connection.

        The synchronous notification observer correlates the changed files for the newer
        file change family, whose own approval params carry none. `_handles` is a test seam
        for a deterministic handle source and is never supplied in production.
        """
        self._contract = contract
        self._issue = _random_handle if _handles is None else _handles
        self._pending: dict[str, _PendingReview] = {}
        self._file_change_explanations: dict[tuple[str, str, str], FileChangeExplanation] = {}
        self._terminal_turns: dict[tuple[str, str], None] = {}
        self._active_turn_check: Callable[[str, str], bool] | None = None
        self._active_turn_callback_bound = False
        self._turn_terminal_callback: Callable[[str, str], None] | None = None
        self._turn_terminal_callback_bound = False
        self._reviewer: _ReviewerSlot | None = None
        self._terminal: str | None = None
        self._pending_count_changed: Callable[[int], None] | None = None
        self._pending_count_callback_bound = False

    def __repr__(self) -> str:
        return (
            f"InteractionBroker(pending={len(self._pending)}, "
            f"reviewer={self._reviewer is not None}, closed={self._terminal is not None})"
        )

    def connect_reviewer(self) -> ReviewerConnection:
        """Bind the one local reviewer slot and hand back that reviewer's capability.

        This process shows approvals to one reviewer at a time, so a second connection is
        refused rather than allowed to shadow the first. Whether a connection may reach this
        far - loopback, origin, and a one-shot bootstrap - belongs to the review gate in
        front of it, not here.
        """
        self._require_open()
        if self._reviewer is not None:
            raise CodexReviewError(_REVIEWER_TAKEN)
        stream: _ReviewStream = asyncio.Queue(maxsize=_MAX_UNREAD_REVIEWS)
        connection = ReviewerConnection(stream)
        self._reviewer = _ReviewerSlot(connection=connection, stream=stream)
        return connection

    def bind_pending_count_changed(self, callback: Callable[[int], None]) -> None:
        """Bind the one synchronous pending-count sink before a reviewer can use this broker."""
        self._require_open()
        if self._pending_count_callback_bound:
            raise CodexReviewError(_COUNT_CALLBACK_BOUND)
        if self._reviewer is not None or self._pending:
            raise CodexReviewError(_COUNT_CALLBACK_LATE)
        if not callable(callback):
            raise CodexReviewError(_COUNT_CALLBACK_LATE)
        if inspect.iscoroutinefunction(callback):
            raise CodexReviewError(_COUNT_CALLBACK_ASYNC)
        self._pending_count_changed = callback
        self._pending_count_callback_bound = True

    def bind_active_turn_check(self, callback: Callable[[str, str], bool]) -> None:
        """Bind the Agent turn owner used to refuse stale correlated approvals."""
        self._require_open()
        if self._active_turn_callback_bound:
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_BOUND)
        if self._reviewer is not None or self._pending or self._file_change_explanations:
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_LATE)
        if not callable(callback):
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_LATE)
        if inspect.iscoroutinefunction(callback):
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_ASYNC)
        self._active_turn_check = callback
        self._active_turn_callback_bound = True

    def bind_turn_terminal(self, callback: Callable[[str, str], None]) -> None:
        """Bind the conversation owner that must observe terminal turns across Voice gaps."""
        self._require_open()
        if self._turn_terminal_callback_bound:
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_BOUND)
        if self._reviewer is not None or self._pending or self._file_change_explanations:
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_LATE)
        if not callable(callback):
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_LATE)
        if inspect.iscoroutinefunction(callback):
            raise CodexReviewError(_ACTIVE_TURN_CALLBACK_ASYNC)
        self._turn_terminal_callback = callback
        self._turn_terminal_callback_bound = True

    def disconnect_reviewer(self, connection: ReviewerConnection) -> None:
        """Release the reviewer slot and fail every review bound to that reviewer closed.

        A reviewer that is gone cannot decide, and nobody else may decide for it, so each
        awaiting handler ends without an accept or a decline. Whatever this reviewer never
        read is dropped with it. Disconnecting twice, or disconnecting a reviewer that
        already lost the slot, changes nothing further.
        """
        if type(cast("object", connection)) is not ReviewerConnection:
            raise CodexReviewError(_UNKNOWN_REVIEWER)
        slot = self._reviewer
        if slot is not None and slot.connection is connection:
            self._reviewer = None
            _end_stream(slot.stream)
        for pending in tuple(self._pending.values()):
            if pending.connection is connection:
                self._fail(pending, _REVIEWER_GONE)

    def register_approval_handlers(self, registrar: ApprovalHandlerRegistrar) -> None:
        """Answer every approval method this build proves readable, and no other.

        Registration happens before the connection starts, and starting or owning that
        connection is not this broker's job. A category is taken whole or not at all: which
        advertised alias a live turn sends is not something a client chooses, so one
        readable alias beside an unreadable one would arrive mid-turn with nothing safe to
        answer. Everything this build offers is checked before anything is registered, and
        the registrations that follow are made together, without awaiting between them.

        The connection's own ending is taken first, in the same step: a broker that answers
        approvals on a connection it never hears end would hold every reviewer waiting on a
        request that can no longer be answered.
        """
        self._require_open()
        contract = self._contract
        adaptable = contract.adaptable_approval_categories
        if not STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES.issubset(adaptable):
            raise CodexReviewError(_UNADAPTABLE_CONTRACT)
        methods = sorted(
            {
                method
                for category in STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
                for method in contract.server_requests[category]
            }
        )
        handler: RpcServerRequestHandler = self.review
        registrar.register_notification_observer(self._observe_notification)
        registrar.register_terminal_callback(self.connection_lost)
        for method in methods:
            registrar.register_server_request_handler(method, handler)

    def _observe_notification(self, notification: RpcNotification) -> None:
        """Correlate patch metadata synchronously before the next inbound request starts."""
        if self._terminal is not None:
            return
        terminal_turn = self._terminal_turn_identity(notification)
        if terminal_turn is not None:
            self._remember_terminal_turn(terminal_turn)
            self._notify_turn_terminal(terminal_turn)
            for key in tuple(self._file_change_explanations):
                if key[:2] == terminal_turn:
                    del self._file_change_explanations[key]
            self._withdraw_terminal_turn(terminal_turn)
            return
        explanation = adapt_file_change_patch_notification(self._contract, notification)
        if explanation is not None:
            turn = (explanation.thread_id, explanation.turn_id)
            if turn in self._terminal_turns:
                message = "file change patch arrived after turn terminal"
                raise CodexRpcProtocolError(message)
            key = (explanation.thread_id, explanation.turn_id, explanation.item_id)
            if (
                key not in self._file_change_explanations
                and len(self._file_change_explanations) >= _MAX_FILE_CHANGE_EXPLANATIONS
            ):
                message = "too many file change explanations are pending"
                raise CodexRpcProtocolError(message)
            self._file_change_explanations[key] = explanation

    def _remember_terminal_turn(self, turn: tuple[str, str]) -> None:
        self._terminal_turns[turn] = None
        while len(self._terminal_turns) > _MAX_TERMINAL_TURNS:
            del self._terminal_turns[next(iter(self._terminal_turns))]

    def _notify_turn_terminal(self, turn: tuple[str, str]) -> None:
        callback = self._turn_terminal_callback
        if callback is None:
            return
        returned = cast("Callable[[str, str], object]", callback)(*turn)
        if returned is None:
            return
        if inspect.iscoroutine(returned):
            with suppress(Exception):
                returned.close()
        message = "turn terminal callback returned an invalid result"
        raise CodexReviewError(message)

    def _withdraw_terminal_turn(self, turn: tuple[str, str]) -> None:
        pending_reviews = tuple(
            pending
            for pending in self._pending.values()
            if isinstance(pending.review.correlation, ThreadItemCorrelation)
            and (
                pending.review.correlation.thread_id,
                pending.review.correlation.turn_id,
            )
            == turn
        )
        if not pending_reviews:
            return
        for pending in pending_reviews:
            del self._pending[pending.handle]
            if not pending.future.done():
                pending.future.set_exception(CodexReviewError(_REVIEW_CANCELLED))
        self._notify_pending_count_changed()
        for pending in pending_reviews:
            self._withdraw_once(pending)

    def _turn_is_active(self, thread_id: str, turn_id: str) -> bool:
        callback = self._active_turn_check
        if callback is None:
            return False
        try:
            active = cast("Callable[[str, str], object]", callback)(thread_id, turn_id)
        except BaseException:  # noqa: BLE001 - owner boundary must fail closed
            return False
        return active is True

    def _terminal_turn_identity(
        self,
        notification: RpcNotification,
    ) -> tuple[str, str] | None:
        profile = self._contract.agent_event_profile
        if profile is None or notification.method != profile.turn_completed_method:
            return None
        params = notification.params
        thread_id = params.get(profile.thread_id_field)
        turn = params.get(profile.turn_field)
        if type(thread_id) is not str or type(turn) is not dict:
            return None
        turn_id = turn.get(profile.id_field)
        status = turn.get(profile.status_field)
        if type(turn_id) is not str or status not in {
            profile.completed_status,
            profile.failed_status,
            profile.interrupted_status,
        }:
            return None
        return (thread_id, turn_id)

    async def review(self, request: RpcServerRequest) -> JsonValue:
        """Publish one inbound approval to the trusted reviewer and await its one decision.

        The caller is the app server's own request handler, so what this returns is exactly
        what that request id is answered with, once, by the transport that owns it. There is
        no deadline here: a request the app server set no timeout on stays pending until the
        reviewer decides, that reviewer goes away, this handler is cancelled, the connection
        is lost, or the broker closes.
        """
        pending = self._publish(request)
        try:
            return await pending.future
        finally:
            self._discard(pending)

    def decide(
        self,
        connection: ReviewerConnection,
        handle: str,
        decision: ApprovalDecision,
    ) -> None:
        """Answer one published review, once, from the reviewer that was shown it.

        A decision is one of moco's own typed semantics, never text the reviewer chose: a
        string enum member equals its own spelling, so only the exact member is taken. The
        request's own response value is built before the handle is consumed, so a decision
        this request never offered leaves the review pending for one it did.
        """
        self._require_open()
        if type(cast("object", decision)) is not ApprovalDecision:
            raise CodexReviewError(_UNSUPPORTED_DECISION)
        if (
            type(cast("object", connection)) is not ReviewerConnection
            or type(cast("object", handle)) is not str
        ):
            raise CodexReviewError(_UNKNOWN_HANDLE)
        pending = self._pending.get(handle)
        if pending is None or pending.connection is not connection or pending.future.done():
            raise CodexReviewError(_UNKNOWN_HANDLE)
        response = pending.review.response_for(decision)
        del self._pending[handle]
        if not self._notify_pending_count_changed():
            if not pending.future.done():
                pending.future.set_exception(CodexReviewError(_BROKER_CLOSED))
            return
        pending.future.set_result(response)

    def cancel_pending(self) -> None:
        """Withdraw every current review without closing the reviewer or this broker."""
        self._require_open()
        pending_reviews = tuple(self._pending.values())
        if not pending_reviews:
            return
        self._pending.clear()
        for pending in pending_reviews:
            if not pending.future.done():
                pending.future.set_exception(CodexReviewError(_REVIEW_CANCELLED))
        self._notify_pending_count_changed()
        for pending in pending_reviews:
            self._withdraw_once(pending)

    def connection_lost(self) -> None:
        """End every pending review because the app-server connection is gone.

        What that connection still owes each request belongs to the transport, which knows
        what it has already sent. Nothing here answers a connection that can no longer carry
        an answer, and no outcome is invented for a turn whose progress is unknown.
        """
        self._terminate(_CONNECTION_LOST)

    def close(self) -> None:
        """End this broker: unblock every waiter fail-closed and refuse new work.

        Closing twice, or closing after the connection was already lost, keeps the first
        ending rather than replacing it, so a waiter is never told two different stories.
        """
        self._terminate(_BROKER_CLOSED)

    def _publish(self, request: RpcServerRequest) -> _PendingReview:
        """Read one request into a review and hand it to the reviewer, in one step.

        Reading the payload, issuing the handle, and registering the pending review all
        happen without awaiting, so a decision, a disconnect, a lost connection, or a close
        can never observe half of a publication.
        """
        self._require_open()
        slot = self._reviewer
        if slot is None:
            raise CodexReviewError(_NO_REVIEWER)
        turn = _approval_turn_identity(request.params)
        if turn is not None and (turn in self._terminal_turns or not self._turn_is_active(*turn)):
            raise CodexReviewError(_INACTIVE_TURN)
        review = adapt_approval_request(
            self._contract,
            request.method,
            request.params,
            request_id=request.request_id,
            file_change_explanation=self._explanation(request),
        )
        if len(self._pending) >= _MAX_PENDING_REVIEWS:
            raise CodexReviewError(_TOO_MANY_REVIEWS)
        handle = self._issued_handle()
        pending = _PendingReview(
            handle=handle,
            connection=slot.connection,
            review=review,
            future=asyncio.get_running_loop().create_future(),
        )
        try:
            slot.stream.put_nowait(ReviewEnvelope(handle=handle, review=review))
        except asyncio.QueueFull:
            raise CodexReviewError(_TOO_MANY_REVIEWS) from None
        self._pending[handle] = pending
        self._notify_pending_count_changed()
        return pending

    def _issued_handle(self) -> str:
        """Issue one opaque handle, or fail closed rather than name a review twice.

        A handle names nothing about the review it opens: not the method, not the app
        server's request id, not the command, the path, or the decision. In production it is
        fresh randomness the operating system provides. A value that repeats one already
        pending would answer the wrong review, so it is asked for again a fixed number of
        times and then refused: a source that keeps answering the same thing stops at the
        bound instead of looping. A value no transport could carry is not a collision and is
        refused at once, because asking such a source again only repeats its answer.
        """
        for _ in range(_MAX_HANDLE_ATTEMPTS):
            handle = _require_handle(self._issue(), _UNUSABLE_HANDLE)
            if handle not in self._pending:
                return handle
        raise CodexReviewError(_UNUSABLE_HANDLE)

    def _explanation(self, request: RpcServerRequest) -> FileChangeExplanation | None:
        """Consume correlated changed files only where the request states none.

        The newer file change family carries no patch body, so what accepting would do can
        only come from the notification already observed for the same item. The correlated
        value is consumed exactly once here.
        """
        if not self._states_no_changes(request.method):
            return None
        key = _file_change_correlation_key(request.params)
        if key is None:
            return None
        explanation = self._file_change_explanations.pop(key, None)
        if explanation is None or not self._turn_is_active(key[0], key[1]):
            return None
        return explanation

    def _states_no_changes(self, method: str) -> bool:
        """Say whether this build's file change family leaves its changed files unstated."""
        profile = self._contract.approval_profile(method)
        return (
            profile is not None
            and profile.category is ServerRequestCategory.FILE_CHANGE_APPROVAL
            and profile.changes_member is None
        )

    def _discard(self, pending: _PendingReview) -> None:
        """Drop one review the awaiting handler no longer owns, however it ended.

        The handle dies with it, so a decision arriving afterwards has nothing to answer.
        However the review ended is read here as well, so a failure the handler was no
        longer waiting for is never left behind unobserved. A review that ended with the
        reviewer's own decision is finished for that reviewer too; every other ending is
        taken back from it.
        """
        if self._pending.get(pending.handle) is pending:
            del self._pending[pending.handle]
            self._notify_pending_count_changed()
        future = pending.future
        decided = future.done() and not future.cancelled() and future.exception() is None
        if not decided:
            self._withdraw_once(pending)

    def _withdraw_once(self, pending: _PendingReview) -> None:
        """Claim and perform the one withdrawal allowed for a published review."""
        if not pending.claim_withdrawal():
            return
        self._withdraw(pending)

    def _withdraw(self, pending: _PendingReview) -> None:
        """Take one ended review back from the reviewer that may no longer decide it.

        A review still waiting on the stream was never seen, so it is destroyed there: its
        details leave memory and the space it held returns to the reviewer, rather than an
        ended approval sitting in front of the next one. A review the reviewer already read
        is on a screen only that reviewer can close, so it is named once more, by handle
        alone. A reviewer that has stopped reading its stream is bounded by that stream: the
        withdrawal is dropped rather than grown into, and the review is over either way.
        """
        slot = self._reviewer
        if slot is None or slot.connection is not pending.connection:
            return
        if _drop_unread(slot.stream, pending.handle):
            return
        with suppress(asyncio.QueueFull):
            slot.stream.put_nowait(ReviewWithdrawal(handle=pending.handle))

    def _fail(self, pending: _PendingReview, message: str) -> None:
        """End one pending review closed, without an accept or a decline for anyone."""
        removed = self._pending.pop(pending.handle, None) is pending
        if not pending.future.done():
            pending.future.set_exception(CodexReviewError(message))
        if removed:
            self._notify_pending_count_changed()

    def _notify_pending_count_changed(self) -> bool:
        """Publish one synchronous count transition, terminalizing on a broken sink."""
        callback = self._pending_count_changed
        if callback is None:
            return True
        failed = False
        try:
            returned = cast("Callable[[int], object]", callback)(len(self._pending))
        except BaseException:  # noqa: BLE001 - the callback is an owner boundary
            failed = True
        else:
            failed = returned is not None
            if inspect.iscoroutine(returned):
                with suppress(Exception):
                    returned.close()
        if failed:
            self._pending_count_changed = None
            self._terminate(_BROKER_CLOSED)
            return False
        return True

    def _terminate(self, message: str) -> None:
        """End the broker once, keeping the first reason every later caller is told."""
        if self._terminal is not None:
            return
        self._terminal = message
        self._file_change_explanations.clear()
        self._terminal_turns.clear()
        self._turn_terminal_callback = None
        slot = self._reviewer
        self._reviewer = None
        for pending in tuple(self._pending.values()):
            self._fail(pending, message)
        if slot is not None:
            _end_stream(slot.stream)

    def _require_open(self) -> None:
        if self._terminal is not None:
            raise CodexReviewError(self._terminal)


def _random_handle() -> str:
    """Return one unpredictable, transport-safe, bounded handle."""
    return secrets.token_urlsafe(_HANDLE_BYTES)


def _file_change_correlation_key(
    params: dict[str, JsonValue],
) -> tuple[str, str, str] | None:
    values = (params.get("threadId"), params.get("turnId"), params.get("itemId"))
    if not all(type(value) is str for value in values):
        return None
    return cast("tuple[str, str, str]", values)


def _approval_turn_identity(params: dict[str, JsonValue]) -> tuple[str, str] | None:
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    if type(thread_id) is not str or type(turn_id) is not str:
        return None
    return (thread_id, turn_id)


def _require_handle(value: object, message: str) -> str:
    """Require one handle a transport can carry unchanged and a lookup can match exactly.

    The exact built-in string is required: a subclass spelling the same text hashes into the
    pending reviews and then answers a comparison however it likes.
    """
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _MAX_HANDLE_CHARACTERS
        or not _is_transport_safe(value)
    ):
        raise CodexReviewError(message)
    return value


def _end_stream(stream: _ReviewStream) -> None:
    """Drop whatever this reviewer never read and end its stream once.

    A review the reviewer is no longer entitled to see is discarded rather than left to be
    read afterwards, which is also what keeps those details out of memory once the reviewer
    is gone.
    """
    while not stream.empty():
        stream.get_nowait()
    stream.put_nowait(_STREAM_END)


def _drop_unread(stream: _ReviewStream, handle: str) -> bool:
    """Take one review off the stream if it is still there, and say whether it was.

    Being on the stream is what unread means: the reviewer takes each item exactly once, so
    a review it never took is the review it was never shown. Removal keeps the order of
    everything else, and a reviewer parked on an empty stream cannot have taken the item
    that is no longer in it.
    """
    held: list[ReviewEnvelope | ReviewWithdrawal | object] = []
    found = False
    while not stream.empty():
        item = stream.get_nowait()
        if isinstance(item, ReviewEnvelope) and item.handle == handle:
            found = True
            continue
        held.append(item)
    for item in held:
        stream.put_nowait(item)
    return found
