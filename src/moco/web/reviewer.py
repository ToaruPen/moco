"""The authenticated local Reviewer WebSocket boundary.

This module carries only the local review transport. The InteractionBroker remains the
owner of approval state and response conversion; the browser receives a bounded typed
projection and can answer only with an opaque handle and one offered decision.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from fastapi import WebSocketDisconnect

from moco.codex.approval import (
    ApprovalDecision,
    CommandApprovalReview,
    FileChangeApprovalReview,
)
from moco.codex.broker import ReviewEnvelope, ReviewerConnection, ReviewWithdrawal
from moco.errors import CodexReviewError, CodexSchemaError
from moco.web.review import ReviewerCapability, ReviewGate, is_valid_bootstrap_nonce

if TYPE_CHECKING:
    from fastapi import WebSocket

__all__ = ["ReviewerBroker", "serve_reviewer_socket"]

_REVIEW_PROTOCOL = "moco-review"
_REVIEW_UNAVAILABLE = "local review is unavailable"
_INVALID_MESSAGE = "invalid local review message"
_MAX_MESSAGE_CHARACTERS = 4_096
_FIRST_MESSAGE_TIMEOUT_SECONDS = 5.0
_VALID_DECISIONS = frozenset(ApprovalDecision)
_MAX_ACTIVE_REVIEWS = 64
_MAX_RECENT_WITHDRAWALS = _MAX_ACTIVE_REVIEWS


class _WithdrawnDecisionRaceError(Exception):
    """One offered decision received while its displayed review was withdrawn."""


@dataclass(slots=True, repr=False)
class _DeferredDecision:
    handle: str
    decision: ApprovalDecision
    failure: CodexReviewError


class ReviewerBroker(Protocol):
    """The broker seam the existing Codex connection may provide to the web layer."""

    def connect_reviewer(self) -> ReviewerConnection: ...

    def disconnect_reviewer(self, connection: ReviewerConnection) -> None: ...

    def decide(
        self,
        connection: ReviewerConnection,
        handle: str,
        decision: ApprovalDecision,
    ) -> None: ...


async def serve_reviewer_socket(
    websocket: WebSocket,
    *,
    review_gate: ReviewGate,
    broker: ReviewerBroker | None,
) -> None:
    """Authenticate one local reviewer and serve it until either side ends.

    The bootstrap is accepted only as the first WebSocket message. It is consumed by the
    ReviewGate before the broker slot is acquired, and both resources are released in the
    one finally block. A missing broker is an unavailable boundary, not an implicit local
    approval implementation.
    """
    capability: ReviewerCapability | None = None
    connection: ReviewerConnection | None = None
    if broker is None:
        await _close_unavailable(websocket)
        return
    try:
        peer_host = websocket.client.host if websocket.client is not None else None
        review_gate.validate_transport(
            peer_host=peer_host,
            host=websocket.headers.get("host"),
            origin=websocket.headers.get("origin"),
        )
        _require_review_protocol(websocket.headers.get("sec-websocket-protocol"))
        await websocket.accept(subprotocol=_REVIEW_PROTOCOL)
        nonce = _first_message_nonce(
            await asyncio.wait_for(
                _receive_text_message(websocket),
                timeout=_FIRST_MESSAGE_TIMEOUT_SECONDS,
            )
        )
        capability = review_gate.redeem_bootstrap_nonce(
            nonce,
            peer_host=peer_host,
            host=websocket.headers.get("host"),
            origin=websocket.headers.get("origin"),
        )
        connection = broker.connect_reviewer()
        await websocket.send_json({"type": "ready"})
        await _serve_review_messages(websocket, broker, connection)
    except (
        CodexReviewError,
        CodexSchemaError,
        TimeoutError,
        TypeError,
        ValueError,
        WebSocketDisconnect,
    ):
        await _close_unavailable(websocket)
    finally:
        if connection is not None:
            with suppress(CodexReviewError):
                broker.disconnect_reviewer(connection)
        if capability is not None:
            capability.release()


async def _serve_review_messages(  # noqa: C901, PLR0912, PLR0915
    websocket: WebSocket,
    broker: ReviewerBroker,
    connection: ReviewerConnection,
) -> None:
    active_reviews: dict[str, frozenset[ApprovalDecision]] = {}
    recent_withdrawals: dict[str, frozenset[ApprovalDecision]] = {}
    deferred: _DeferredDecision | None = None
    next_review = asyncio.create_task(anext(connection), name="moco-review-next")
    next_message: asyncio.Task[str] | None = asyncio.create_task(
        _receive_text_message(websocket),
        name="moco-review-message",
    )
    try:
        while True:
            waiters = (next_review,) if next_message is None else (next_review, next_message)
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_review in done:
                try:
                    published = next_review.result()
                except StopAsyncIteration:
                    if deferred is not None:
                        raise deferred.failure from None
                    return
                _require_deferred_withdrawal(deferred, published)
                wire_message = _review_wire_message(published)
                _record_publication(active_reviews, recent_withdrawals, published)
                await websocket.send_json(wire_message)
                del wire_message
                next_review = asyncio.create_task(anext(connection), name="moco-review-next")
                del published
                if deferred is not None:
                    try:
                        _decide_review(
                            broker,
                            connection,
                            active_reviews,
                            recent_withdrawals,
                            deferred.handle,
                            deferred.decision,
                        )
                    except _WithdrawnDecisionRaceError:
                        deferred = None
                        next_message = asyncio.create_task(
                            _receive_text_message(websocket),
                            name="moco-review-message",
                        )
            if next_message is not None and next_message in done:
                payload = next_message.result()
                handle, decision = _decision_message(payload)
                send_resolution = True
                try:
                    _decide_review(
                        broker,
                        connection,
                        active_reviews,
                        recent_withdrawals,
                        handle,
                        decision,
                    )
                except _WithdrawnDecisionRaceError:
                    send_resolution = False
                except CodexReviewError as error:
                    if not _is_active_offered_decision(active_reviews, handle, decision):
                        raise
                    deferred = _DeferredDecision(handle, decision, error)
                    next_message = None
                    continue
                if send_resolution:
                    await websocket.send_json(
                        {"type": "resolved", "reviewHandle": handle},
                    )
                next_message = asyncio.create_task(
                    _receive_text_message(websocket),
                    name="moco-review-message",
                )
    finally:
        tasks = (next_review,) if next_message is None else (next_review, next_message)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _require_deferred_withdrawal(
    deferred: _DeferredDecision | None,
    published: ReviewEnvelope | ReviewWithdrawal,
) -> None:
    if deferred is not None and (
        type(published) is not ReviewWithdrawal or published.handle != deferred.handle
    ):
        raise deferred.failure


def _record_publication(
    active_reviews: dict[str, frozenset[ApprovalDecision]],
    recent_withdrawals: dict[str, frozenset[ApprovalDecision]],
    published: ReviewEnvelope | ReviewWithdrawal,
) -> None:
    if type(published) is ReviewEnvelope:
        if len(active_reviews) >= _MAX_ACTIVE_REVIEWS:
            raise CodexReviewError(_REVIEW_UNAVAILABLE)
        active_reviews[published.handle] = frozenset(published.review.decisions)
        return
    if type(published) is not ReviewWithdrawal:
        raise CodexReviewError(_REVIEW_UNAVAILABLE)
    decisions = active_reviews.pop(published.handle, None)
    if decisions is not None:
        recent_withdrawals[published.handle] = decisions
        if len(recent_withdrawals) > _MAX_RECENT_WITHDRAWALS:
            del recent_withdrawals[next(iter(recent_withdrawals))]


def _decide_review(
    broker: ReviewerBroker,
    connection: ReviewerConnection,
    active_reviews: dict[str, frozenset[ApprovalDecision]],
    recent_withdrawals: dict[str, frozenset[ApprovalDecision]],
    handle: str,
    decision: ApprovalDecision,
) -> None:
    withdrawn_decisions = recent_withdrawals.get(handle)
    if withdrawn_decisions is not None and decision in withdrawn_decisions:
        del recent_withdrawals[handle]
        raise _WithdrawnDecisionRaceError
    broker.decide(connection, handle, decision)
    active_reviews.pop(handle, None)


def _is_active_offered_decision(
    active_reviews: dict[str, frozenset[ApprovalDecision]],
    handle: str,
    decision: ApprovalDecision,
) -> bool:
    decisions = active_reviews.get(handle)
    return decisions is not None and decision in decisions


async def _receive_text_message(websocket: WebSocket) -> str:
    message = await websocket.receive()
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(code=message.get("code", 1000))
    payload = message.get("text")
    if message.get("type") != "websocket.receive" or type(payload) is not str:
        raise CodexReviewError(_INVALID_MESSAGE)
    return payload


def _require_review_protocol(value: object) -> None:
    if type(value) is not str or not any(
        token.strip() == _REVIEW_PROTOCOL for token in value.split(",")
    ):
        raise CodexReviewError(_REVIEW_UNAVAILABLE)


def _first_message_nonce(payload: str) -> str:
    message = _strict_json_object(payload)
    if set(message) != {"nonce"}:
        raise CodexReviewError(_INVALID_MESSAGE)
    nonce = message["nonce"]
    if type(nonce) is not str or not is_valid_bootstrap_nonce(nonce):
        raise CodexReviewError(_INVALID_MESSAGE)
    return nonce


def _decision_message(payload: str) -> tuple[str, ApprovalDecision]:
    message = _strict_json_object(payload)
    if set(message) != {"reviewHandle", "decision"}:
        raise CodexReviewError(_INVALID_MESSAGE)
    handle = message["reviewHandle"]
    decision = message["decision"]
    if type(handle) is not str or type(decision) is not str:
        raise CodexReviewError(_INVALID_MESSAGE)
    try:
        selected = ApprovalDecision(decision)
    except ValueError:
        raise CodexReviewError(_INVALID_MESSAGE) from None
    if selected not in _VALID_DECISIONS:
        raise CodexReviewError(_INVALID_MESSAGE)
    return handle, selected


def _strict_json_object(payload: str) -> dict[str, object]:
    if type(payload) is not str or len(payload) > _MAX_MESSAGE_CHARACTERS:
        raise CodexReviewError(_INVALID_MESSAGE)
    try:
        decoded = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, TypeError):
        raise CodexReviewError(_INVALID_MESSAGE) from None
    if type(decoded) is not dict:
        raise CodexReviewError(_INVALID_MESSAGE)
    return cast("dict[str, object]", decoded)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    message: dict[str, object] = {}
    for key, value in pairs:
        if key in message:
            raise ValueError
        message[key] = value
    return message


def _review_wire_message(
    published: ReviewEnvelope | ReviewWithdrawal,
) -> dict[str, object]:
    if type(published) is ReviewWithdrawal:
        return {"type": "withdrawn", "reviewHandle": published.handle}
    if type(published) is not ReviewEnvelope:
        raise CodexReviewError(_REVIEW_UNAVAILABLE)
    review = published.review
    message: dict[str, object] = {
        "type": "review",
        "reviewHandle": published.handle,
        "category": review.category.value,
        "decisions": [decision.value for decision in review.decisions],
    }
    if isinstance(review, CommandApprovalReview):
        message["cwd"] = review.cwd
        if type(review.command) is str:
            message["commandText"] = review.command
        else:
            message["command"] = list(review.command)
        if review.reason is not None:
            message["reason"] = review.reason
    elif isinstance(review, FileChangeApprovalReview):
        message["changes"] = [
            {
                "kind": change.kind.value,
                "path": change.path,
                **({"destination": change.destination} if change.destination is not None else {}),
            }
            for change in review.changes
        ]
        if review.reason is not None:
            message["reason"] = review.reason
    else:  # pragma: no cover - ReviewEnvelope validates the two concrete families
        raise CodexReviewError(_REVIEW_UNAVAILABLE)
    return message


async def _close_unavailable(websocket: WebSocket) -> None:
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code=1008)
