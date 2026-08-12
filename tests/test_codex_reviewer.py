from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from moco.codex.approval import ApprovalDecision, CommandApprovalReview
from moco.codex.broker import (
    InteractionBroker,
    ReviewEnvelope,
    ReviewerConnection,
    ReviewWithdrawal,
)
from moco.codex.rpc import JsonValue, RpcServerRequest
from moco.codex.schema import (
    ApprovalCorrelation,
    ApprovalProfile,
    CodexProtocolContract,
    ServerRequestCategory,
    _ValueContract,
)
from moco.errors import CodexReviewError, CodexSchemaError
from moco.web import reviewer as reviewer_module
from moco.web.app import create_app
from moco.web.review import ReviewGate
from moco.web.reviewer import ReviewerBroker, serve_reviewer_socket

MEDIA_TOKEN = "media-token"  # noqa: S105 - deterministic test credential
CONTROL_SECRET = "control-secret"  # noqa: S105 - deterministic test credential
HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"


class _ReviewerConnection:
    def __init__(self) -> None:
        self.items: asyncio.Queue[ReviewEnvelope | object] = asyncio.Queue()

    def __aiter__(self) -> _ReviewerConnection:
        return self

    async def __anext__(self) -> ReviewEnvelope:
        item = await self.items.get()
        if item is _STREAM_END:
            raise StopAsyncIteration
        return cast("ReviewEnvelope", item)

    def publish(self, envelope: ReviewEnvelope) -> None:
        self.items.put_nowait(envelope)


class _ReviewerBroker:
    def __init__(self) -> None:
        self.connection: _ReviewerConnection | None = None
        self.disconnects = 0
        self.decisions: list[tuple[object, str, ApprovalDecision]] = []

    def connect_reviewer(self) -> _ReviewerConnection:
        if self.connection is not None:
            raise CodexReviewError(_SLOT_ERROR)
        self.connection = _ReviewerConnection()
        return self.connection

    def disconnect_reviewer(self, connection: _ReviewerConnection) -> None:
        if self.connection is connection:
            self.connection = None
            self.disconnects += 1
            connection.items.put_nowait(_STREAM_END)

    def decide(
        self,
        connection: _ReviewerConnection,
        handle: str,
        decision: ApprovalDecision,
    ) -> None:
        if self.connection is not connection:
            raise CodexReviewError(_CONNECTION_ERROR)
        self.decisions.append((connection, handle, decision))


_SLOT_ERROR = "reviewer slot is occupied"
_CONNECTION_ERROR = "reviewer connection is unavailable"


def _nonce(app: FastAPI) -> str:
    return cast(
        "str",
        app.state.review_gate.issue_bootstrap_nonce(
            CONTROL_SECRET,
            peer_host="127.0.0.1",
            host=HOST,
            origin=ORIGIN,
        ),
    )


def _review_headers() -> dict[str, str]:
    return {
        "host": HOST,
        "origin": ORIGIN,
        "sec-websocket-protocol": "moco-review",
    }


def _local_headers() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN}


_STREAM_END = object()

_REVIEW_STRING = _ValueContract(types=frozenset({"string"}))
_REVIEW_NULLABLE_STRING = _ValueContract(types=frozenset({"string", "null"}))
_REVIEW_INTEGER = _ValueContract(types=frozenset({"integer"}), int64=True)
_REVIEW_DECISION_LIST = _ValueContract(
    types=frozenset({"array", "null"}),
    items=_REVIEW_STRING,
)


def _real_interaction_contract() -> CodexProtocolContract:
    command_method = "command/approval"
    file_method = "file/approval"
    command_profile = ApprovalProfile(
        category=ServerRequestCategory.COMMAND_APPROVAL,
        correlation=ApprovalCorrelation.THREAD_ITEM,
        required_members=frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        absent_or_null_members=frozenset(),
        member_contracts={
            "threadId": _REVIEW_STRING,
            "turnId": _REVIEW_STRING,
            "itemId": _REVIEW_STRING,
            "command": _REVIEW_NULLABLE_STRING,
            "cwd": _REVIEW_NULLABLE_STRING,
            "reason": _REVIEW_NULLABLE_STRING,
            "startedAtMs": _REVIEW_INTEGER,
            "availableDecisions": _REVIEW_DECISION_LIST,
        },
        argv_member=None,
        changes_member=None,
        offer_member="availableDecisions",
        decisions={
            ApprovalDecision.ACCEPT: "accept",
            ApprovalDecision.DECLINE: "decline",
            ApprovalDecision.CANCEL: "cancel",
        },
        decision_contract=_REVIEW_STRING,
    )
    file_profile = ApprovalProfile(
        category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
        correlation=ApprovalCorrelation.THREAD_ITEM,
        required_members=frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        absent_or_null_members=frozenset(),
        member_contracts={
            "threadId": _REVIEW_STRING,
            "turnId": _REVIEW_STRING,
            "itemId": _REVIEW_STRING,
            "reason": _REVIEW_NULLABLE_STRING,
            "startedAtMs": _REVIEW_INTEGER,
        },
        argv_member=None,
        changes_member=None,
        offer_member=None,
        decisions={
            ApprovalDecision.ACCEPT: "accept",
            ApprovalDecision.DECLINE: "decline",
            ApprovalDecision.CANCEL: "cancel",
        },
        decision_contract=_REVIEW_STRING,
    )
    return CodexProtocolContract(
        version="reviewer-test",
        methods={},
        server_requests={
            ServerRequestCategory.COMMAND_APPROVAL: frozenset({command_method}),
            ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset({file_method}),
        },
        unclassified_server_request_count=0,
        experimental_schema=False,
        approval_profiles={command_method: command_profile, file_method: file_profile},
    )


def _real_interaction_broker(handles: Callable[[], str]) -> InteractionBroker:
    interaction = InteractionBroker(_real_interaction_contract(), _handles=handles)
    interaction.bind_active_turn_check(lambda _thread_id, _turn_id: True)
    return interaction


def _real_command_request(
    request_id: str = "review-request",
    *,
    item_id: str = "item",
) -> RpcServerRequest:
    return RpcServerRequest(
        request_id=request_id,
        method="command/approval",
        params={
            "threadId": "thread",
            "turnId": "turn",
            "itemId": item_id,
            "command": "tool",
            "cwd": "/workspace",
            "availableDecisions": ["accept", "decline"],
            "startedAtMs": 1,
        },
    )


def _command_envelope(
    command: str | tuple[str, ...] = ("echo", "<private-detail>"),
) -> ReviewEnvelope:
    review = object.__new__(CommandApprovalReview)
    object.__setattr__(
        review,
        "profile",
        SimpleNamespace(category=SimpleNamespace(value="command_approval")),
    )
    object.__setattr__(review, "correlation", object())
    object.__setattr__(review, "command", command)
    object.__setattr__(review, "cwd", "/private/workspace")
    object.__setattr__(review, "reason", "private reason")
    object.__setattr__(review, "decisions", (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE))
    return ReviewEnvelope(handle="review-handle", review=review)


@pytest.mark.parametrize("credential", [MEDIA_TOKEN, CONTROL_SECRET])
def test_review_page_and_websocket_are_separate_from_media_authentication(
    credential: str,
) -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with TestClient(
        app,
        base_url=f"http://{HOST}",
        client=("127.0.0.1", 50000),
    ) as client:
        page = client.get("/review")
        assert page.status_code == 200
        assert "/static/review.js" in page.text

        with client.websocket_connect("/review/ws", headers=_review_headers()) as socket:
            socket.send_json({"nonce": credential})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_text()


@pytest.mark.parametrize("credential_kind", ["control_secret", "bootstrap_nonce"])
def test_media_websocket_rejects_reviewer_credentials(credential_kind: str) -> None:
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
    )
    candidate = CONTROL_SECRET if credential_kind == "control_secret" else _nonce(app)

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws",
            headers=_local_headers(),
            subprotocols=["moco", f"moco.capability.{candidate}"],
        ),
    ):
        pass


def test_reviewer_nonce_is_first_message_single_use_and_slot_bound() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with TestClient(
        app,
        base_url=f"http://{HOST}",
        client=("127.0.0.1", 50000),
    ) as client:
        first_nonce = _nonce(app)
        with client.websocket_connect("/review/ws", headers=_review_headers()) as first:
            first.send_json({"nonce": first_nonce})
            assert first.receive_json() == {"type": "ready"}

            first.send_json(
                {"reviewHandle": "opaque-handle", "decision": "accept", "extra": "reject"}
            )
            with pytest.raises(WebSocketDisconnect):
                first.receive_text()

        with client.websocket_connect("/review/ws", headers=_review_headers()) as replay:
            replay.send_json({"nonce": first_nonce})
            with pytest.raises(WebSocketDisconnect):
                replay.receive_text()

        second_nonce = _nonce(app)
        with client.websocket_connect("/review/ws", headers=_review_headers()) as second:
            second.send_json({"nonce": second_nonce})
            assert second.receive_json() == {"type": "ready"}
            second.send_json({"nonce": second_nonce})
            with pytest.raises(WebSocketDisconnect):
                second.receive_text()

    assert broker.disconnects == 2


def test_reviewer_rejects_binary_bootstrap_with_a_policy_close() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with TestClient(
        app,
        base_url=f"http://{HOST}",
        client=("127.0.0.1", 50000),
    ) as client:
        with client.websocket_connect("/review/ws", headers=_review_headers()) as socket:
            socket.send_bytes(b"not-text")
            with pytest.raises(WebSocketDisconnect) as disconnected:
                socket.receive_text()
            assert disconnected.value.code == 1008

        assert broker.connection is None
        assert broker.disconnects == 0
        assert _nonce(app)


def test_reviewer_rejects_binary_decision_with_a_policy_close_and_releases_state() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with TestClient(
        app,
        base_url=f"http://{HOST}",
        client=("127.0.0.1", 50000),
    ) as client:
        with client.websocket_connect("/review/ws", headers=_review_headers()) as socket:
            socket.send_json({"nonce": _nonce(app)})
            assert socket.receive_json() == {"type": "ready"}
            socket.send_bytes(b"not-text")
            with pytest.raises(WebSocketDisconnect) as disconnected:
                socket.receive_text()
            assert disconnected.value.code == 1008

        assert broker.connection is None
        assert broker.disconnects == 1
        assert broker.decisions == []
        assert _nonce(app)


def test_reviewer_cancel_not_offered_by_real_broker_closes_and_releases_state() -> None:
    interaction = _real_interaction_broker(lambda: "real-review-handle")
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", interaction),
    )

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        client.websocket_connect("/review/ws", headers=_review_headers()) as socket,
    ):
        socket.send_json({"nonce": _nonce(app)})
        assert socket.receive_json() == {"type": "ready"}
        portal = client.portal
        assert portal is not None
        review_task = portal.start_task_soon(
            interaction.review,
            _real_command_request(),
        )
        review = socket.receive_json()
        assert review["reviewHandle"] == "real-review-handle"
        socket.send_json({"reviewHandle": "real-review-handle", "decision": "cancel"})
        with pytest.raises(WebSocketDisconnect) as disconnected:
            socket.receive_text()
        assert disconnected.value.code == 1008

    with pytest.raises(CodexReviewError):
        review_task.result(timeout=1)
    assert repr(interaction) == "InteractionBroker(pending=0, reviewer=False, closed=False)"
    assert _nonce(app)


def test_reviewer_sends_typed_details_and_accepts_only_the_opaque_decision() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        client.websocket_connect("/review/ws", headers=_review_headers()) as socket,
    ):
        socket.send_json({"nonce": _nonce(app)})
        assert socket.receive_json() == {"type": "ready"}
        assert broker.connection is not None
        broker.connection.publish(_command_envelope())

        assert socket.receive_json() == {
            "type": "review",
            "reviewHandle": "review-handle",
            "category": "command_approval",
            "decisions": ["accept", "decline"],
            "command": ["echo", "<private-detail>"],
            "cwd": "/private/workspace",
            "reason": "private reason",
        }
        socket.send_json({"reviewHandle": "review-handle", "decision": "accept"})
        assert socket.receive_json() == {
            "type": "resolved",
            "reviewHandle": "review-handle",
        }
        socket.close()

    assert len(broker.decisions) == 1
    assert broker.decisions[0][1:] == ("review-handle", ApprovalDecision.ACCEPT)


def test_reviewer_sends_a_modern_raw_command_as_text_not_as_argv() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        client.websocket_connect("/review/ws", headers=_review_headers()) as socket,
    ):
        socket.send_json({"nonce": _nonce(app)})
        assert socket.receive_json() == {"type": "ready"}
        assert broker.connection is not None
        broker.connection.publish(_command_envelope("echo one && echo two"))

        message = socket.receive_json()
        assert message["commandText"] == "echo one && echo two"
        assert "command" not in message
        socket.close()


def test_reviewer_rejects_duplicate_decision_fields_without_answering() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        client.websocket_connect("/review/ws", headers=_review_headers()) as socket,
    ):
        socket.send_json({"nonce": _nonce(app)})
        assert socket.receive_json() == {"type": "ready"}
        assert broker.connection is not None
        broker.connection.publish(_command_envelope())
        assert socket.receive_json()["reviewHandle"] == "review-handle"
        socket.send_text(
            '{"reviewHandle":"review-handle","decision":"accept","decision":"decline"}'
        )
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
        socket.close()

    assert broker.decisions == []


def test_reviewer_slot_rejects_a_second_connection_until_the_first_disconnects() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with TestClient(
        app,
        base_url=f"http://{HOST}",
        client=("127.0.0.1", 50000),
    ) as client:
        first_nonce = _nonce(app)
        second_nonce = _nonce(app)
        with client.websocket_connect("/review/ws", headers=_review_headers()) as first:
            first.send_json({"nonce": first_nonce})
            assert first.receive_json() == {"type": "ready"}
            with client.websocket_connect("/review/ws", headers=_review_headers()) as second:
                second.send_json({"nonce": second_nonce})
                with pytest.raises(WebSocketDisconnect):
                    second.receive_text()
            first.close()

        with client.websocket_connect("/review/ws", headers=_review_headers()) as reopened:
            reopened.send_json({"nonce": _nonce(app)})
            assert reopened.receive_json() == {"type": "ready"}
            reopened.close()


@pytest.mark.parametrize(
    "headers",
    [
        {
            "host": "127.0.0.1.evil:8765",
            "origin": ORIGIN,
            "sec-websocket-protocol": "moco-review",
        },
        {
            "host": HOST,
            "origin": "http://127.0.0.1.evil:8765",
            "sec-websocket-protocol": "moco-review",
        },
    ],
)
def test_reviewer_socket_rejects_an_invalid_boundary_before_accepting(
    headers: dict[str, str],
) -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/review/ws", headers=headers),
    ):
        pytest.fail("invalid reviewer transport was accepted")

    assert broker.connection is None
    assert broker.disconnects == 0


def test_reviewer_socket_requires_the_exact_offered_subprotocol_before_accepting() -> None:
    broker = _ReviewerBroker()
    app = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
        review_broker=cast("ReviewerBroker", broker),
    )

    with (
        TestClient(
            app,
            base_url=f"http://{HOST}",
            client=("127.0.0.1", 50000),
        ) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/review/ws",
            headers=_local_headers(),
            subprotocols=["moco-review.evil"],
        ),
    ):
        pytest.fail("unoffered reviewer protocol was accepted")

    assert broker.connection is None
    assert broker.disconnects == 0


class _BootstrapSocket:
    def __init__(
        self,
        *,
        peer_host: str = "127.0.0.1",
        headers: dict[str, str] | None = None,
        block_receive: bool = False,
    ) -> None:
        self.client = SimpleNamespace(host=peer_host)
        self.headers = headers or _review_headers()
        self.block_receive = block_receive
        self.accepted = False
        self.close_codes: list[int] = []
        self._blocked = asyncio.Event()

    async def accept(self, *, subprotocol: str) -> None:
        assert subprotocol == "moco-review"
        self.accepted = True

    async def receive(self) -> dict[str, object]:
        if self.block_receive:
            await self._blocked.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)


class _ServingSocket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.received: asyncio.Queue[None] = asyncio.Queue()
        self.resolved = asyncio.Event()

    async def receive(self) -> dict[str, object]:
        message = await self.messages.get()
        self.received.put_nowait(None)
        return message

    async def send_json(self, message: dict[str, object]) -> None:
        self.sent.put_nowait(message)
        if message.get("type") == "resolved":
            self.resolved.set()


@pytest.mark.parametrize(
    ("peer_host", "headers"),
    [
        ("203.0.113.8", _review_headers()),
        (
            "127.0.0.1",
            _review_headers() | {"host": "review.example:8765"},
        ),
        (
            "127.0.0.1",
            _review_headers() | {"origin": "https://tunnel.example"},
        ),
        (
            "127.0.0.1",
            _review_headers() | {"sec-websocket-protocol": "moco-review.evil"},
        ),
    ],
)
async def test_invalid_reviewer_transport_is_closed_before_accept_or_broker_slot(
    peer_host: str,
    headers: dict[str, str],
) -> None:
    broker = _ReviewerBroker()
    socket = _BootstrapSocket(peer_host=peer_host, headers=headers)

    await serve_reviewer_socket(
        cast("object", socket),  # type: ignore[arg-type]
        review_gate=ReviewGate(CONTROL_SECRET),
        broker=cast("ReviewerBroker", broker),
    )

    assert socket.accepted is False
    assert socket.close_codes == [1008]
    assert broker.connection is None


async def test_reviewer_bootstrap_first_message_times_out_without_a_broker_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reviewer_module, "_FIRST_MESSAGE_TIMEOUT_SECONDS", 0.01, raising=False)
    broker = _ReviewerBroker()
    socket = _BootstrapSocket(block_receive=True)

    await asyncio.wait_for(
        serve_reviewer_socket(
            cast("object", socket),  # type: ignore[arg-type]
            review_gate=ReviewGate(CONTROL_SECRET),
            broker=cast("ReviewerBroker", broker),
        ),
        timeout=0.25,
    )

    assert socket.accepted is True
    assert socket.close_codes == [1008]
    assert broker.connection is None


async def test_review_transport_frame_releases_detail_after_send_and_decision() -> None:
    broker = _ReviewerBroker()
    connection = broker.connect_reviewer()
    envelope = _command_envelope("private command detail")
    socket = _ServingSocket()
    socket.messages.put_nowait(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {"reviewHandle": envelope.handle, "decision": "accept"},
            ),
        }
    )
    connection.publish(envelope)

    transport = asyncio.create_task(
        reviewer_module._serve_review_messages(  # noqa: SLF001
            cast("object", socket),  # type: ignore[arg-type]
            cast("ReviewerBroker", broker),
            cast("object", connection),  # type: ignore[arg-type]
        )
    )
    try:
        await asyncio.wait_for(socket.resolved.wait(), timeout=0.25)
        await asyncio.sleep(0)

        frame_values = [
            value for frame in transport.get_stack() for value in frame.f_locals.values()
        ]
        assert all(value is not envelope for value in frame_values)
        assert all(value is not envelope.review for value in frame_values)
        assert "private command detail" not in repr(frame_values)
    finally:
        transport.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transport


@pytest.mark.parametrize("schedule", ["withdrawal_ready", "message_first"])
async def test_withdrawn_decision_race_keeps_reviewer_transport_for_next_review(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    schedule: Literal["message_first", "withdrawal_ready"],
) -> None:
    handles = iter(("withdrawn-review-handle", "next-review-handle"))
    broker = _real_interaction_broker(lambda: next(handles))
    connection = broker.connect_reviewer()
    socket = _ServingSocket()
    next_review_blocked = asyncio.Event()
    next_review_release = asyncio.Event()
    if schedule == "message_first":
        original_anext = ReviewerConnection.__anext__
        calls = 0

        async def delay_second_review_read(
            candidate: ReviewerConnection,
        ) -> ReviewEnvelope | ReviewWithdrawal:
            nonlocal calls
            calls += 1
            if candidate is connection and calls == 2:
                next_review_blocked.set()
                await next_review_release.wait()
            return await original_anext(candidate)

        monkeypatch.setattr(ReviewerConnection, "__anext__", delay_second_review_read)
    transport = asyncio.create_task(
        reviewer_module._serve_review_messages(  # noqa: SLF001
            cast("object", socket),  # type: ignore[arg-type]
            cast("ReviewerBroker", broker),
            connection,
        )
    )
    first_review = asyncio.create_task(broker.review(_real_command_request()))
    second_review: asyncio.Task[JsonValue] | None = None

    try:
        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == {
            "type": "review",
            "reviewHandle": "withdrawn-review-handle",
            "category": "command_approval",
            "decisions": ["accept", "decline"],
            "commandText": "tool",
            "cwd": "/workspace",
        }
        if schedule == "message_first":
            await asyncio.wait_for(next_review_blocked.wait(), timeout=1)
        broker.cancel_pending()
        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "reviewHandle": "withdrawn-review-handle",
                        "decision": "accept",
                    }
                ),
            }
        )
        if schedule == "message_first":
            await asyncio.wait_for(socket.received.get(), timeout=1)
            stopped_before_withdrawal, _ = await asyncio.wait({transport}, timeout=0.05)
            assert stopped_before_withdrawal == set()
            next_review_release.set()

        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == {
            "type": "withdrawn",
            "reviewHandle": "withdrawn-review-handle",
        }
        with pytest.raises(CodexReviewError):
            await first_review
        stopped, _ = await asyncio.wait({transport}, timeout=0.05)
        assert stopped == set()
        assert not socket.resolved.is_set()

        second_review = asyncio.create_task(
            broker.review(
                _real_command_request("next-review-request", item_id="next-item"),
            )
        )
        next_offer = await asyncio.wait_for(socket.sent.get(), timeout=1)
        assert next_offer["type"] == "review"
        assert next_offer["reviewHandle"] == "next-review-handle"
        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {"reviewHandle": "next-review-handle", "decision": "accept"},
                ),
            }
        )
        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == {
            "type": "resolved",
            "reviewHandle": "next-review-handle",
        }
        assert await second_review == {"decision": "accept"}
    finally:
        next_review_release.set()
        transport.cancel()
        await asyncio.gather(transport, return_exceptions=True)
        if not first_review.done():
            first_review.cancel()
        if second_review is not None and not second_review.done():
            second_review.cancel()
        await asyncio.gather(
            first_review,
            *(task for task in (second_review,) if task is not None),
            return_exceptions=True,
        )
        broker.close()


async def test_late_withdrawn_decision_survives_a_subsequent_review_offer() -> None:
    handles = iter(("withdrawn-review-handle", "next-review-handle"))
    broker = _real_interaction_broker(lambda: next(handles))
    connection = broker.connect_reviewer()
    socket = _ServingSocket()
    transport = asyncio.create_task(
        reviewer_module._serve_review_messages(  # noqa: SLF001
            cast("object", socket),  # type: ignore[arg-type]
            cast("ReviewerBroker", broker),
            connection,
        )
    )
    withdrawn_review = asyncio.create_task(broker.review(_real_command_request()))
    next_review: asyncio.Task[JsonValue] | None = None

    try:
        withdrawn_offer = await asyncio.wait_for(socket.sent.get(), timeout=1)
        assert withdrawn_offer["reviewHandle"] == "withdrawn-review-handle"

        broker.cancel_pending()
        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == {
            "type": "withdrawn",
            "reviewHandle": "withdrawn-review-handle",
        }
        with pytest.raises(CodexReviewError):
            await withdrawn_review

        next_review = asyncio.create_task(
            broker.review(
                _real_command_request("next-review-request", item_id="next-item"),
            )
        )
        next_offer = await asyncio.wait_for(socket.sent.get(), timeout=1)
        assert next_offer["reviewHandle"] == "next-review-handle"

        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "reviewHandle": "withdrawn-review-handle",
                        "decision": "accept",
                    }
                ),
            }
        )
        await asyncio.wait_for(socket.received.get(), timeout=1)
        stopped, _ = await asyncio.wait({transport}, timeout=0.05)
        assert stopped == set()
        assert not socket.resolved.is_set()

        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {"reviewHandle": "next-review-handle", "decision": "accept"},
                ),
            }
        )
        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == {
            "type": "resolved",
            "reviewHandle": "next-review-handle",
        }
        assert await next_review == {"decision": "accept"}
    finally:
        if not transport.done():
            transport.cancel()
        if not withdrawn_review.done():
            withdrawn_review.cancel()
        if next_review is not None and not next_review.done():
            next_review.cancel()
        await asyncio.gather(
            transport,
            withdrawn_review,
            *(task for task in (next_review,) if task is not None),
            return_exceptions=True,
        )
        broker.close()


def test_recent_withdrawals_evict_the_oldest_transport_record() -> None:
    active_reviews: dict[str, frozenset[ApprovalDecision]] = {}
    recent_withdrawals: dict[str, frozenset[ApprovalDecision]] = {}
    decisions = frozenset({ApprovalDecision.ACCEPT})
    handles = [
        f"withdrawn-review-handle-{index}"
        for index in range(reviewer_module._MAX_ACTIVE_REVIEWS + 1)  # noqa: SLF001
    ]

    for handle in handles:
        active_reviews[handle] = decisions
        reviewer_module._record_publication(  # noqa: SLF001
            active_reviews,
            recent_withdrawals,
            ReviewWithdrawal(handle),
        )

    assert list(recent_withdrawals) == handles[1:]


@pytest.mark.parametrize(
    ("handle", "decision"),
    [("never-issued-handle", "accept"), ("active-review-handle", "cancel")],
)
async def test_non_stale_decision_error_terminates_reviewer_transport(
    handle: str,
    decision: str,
) -> None:
    broker = _real_interaction_broker(lambda: "active-review-handle")
    connection = broker.connect_reviewer()
    socket = _ServingSocket()
    transport = asyncio.create_task(
        reviewer_module._serve_review_messages(  # noqa: SLF001
            cast("object", socket),  # type: ignore[arg-type]
            cast("ReviewerBroker", broker),
            connection,
        )
    )
    review = asyncio.create_task(broker.review(_real_command_request()))

    try:
        offer = await asyncio.wait_for(socket.sent.get(), timeout=1)
        assert offer["reviewHandle"] == "active-review-handle"
        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps({"reviewHandle": handle, "decision": decision}),
            }
        )
        with pytest.raises((CodexReviewError, CodexSchemaError)):
            await asyncio.wait_for(transport, timeout=1)
        assert not socket.resolved.is_set()
    finally:
        if not transport.done():
            transport.cancel()
        review.cancel()
        await asyncio.gather(transport, review, return_exceptions=True)
        broker.close()


async def test_broker_terminal_while_offered_decision_is_deferred_still_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _real_interaction_broker(lambda: "active-review-handle")
    connection = broker.connect_reviewer()
    original_anext = ReviewerConnection.__anext__
    next_review_blocked = asyncio.Event()
    next_review_release = asyncio.Event()
    calls = 0

    async def delay_second_review_read(
        candidate: ReviewerConnection,
    ) -> ReviewEnvelope | ReviewWithdrawal:
        nonlocal calls
        calls += 1
        if candidate is connection and calls == 2:
            next_review_blocked.set()
            await next_review_release.wait()
        return await original_anext(candidate)

    monkeypatch.setattr(ReviewerConnection, "__anext__", delay_second_review_read)
    socket = _ServingSocket()
    transport = asyncio.create_task(
        reviewer_module._serve_review_messages(  # noqa: SLF001
            cast("object", socket),  # type: ignore[arg-type]
            cast("ReviewerBroker", broker),
            connection,
        )
    )
    review = asyncio.create_task(broker.review(_real_command_request()))

    try:
        offer = await asyncio.wait_for(socket.sent.get(), timeout=1)
        assert offer["reviewHandle"] == "active-review-handle"
        await asyncio.wait_for(next_review_blocked.wait(), timeout=1)
        broker.close()
        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {"reviewHandle": "active-review-handle", "decision": "accept"},
                ),
            }
        )
        await asyncio.wait_for(socket.received.get(), timeout=1)
        stopped_before_terminal, _ = await asyncio.wait({transport}, timeout=0.05)
        assert stopped_before_terminal == set()
        next_review_release.set()
        with pytest.raises(CodexReviewError):
            await asyncio.wait_for(transport, timeout=1)
        with pytest.raises(CodexReviewError):
            await review
        assert not socket.resolved.is_set()
    finally:
        next_review_release.set()
        if not transport.done():
            transport.cancel()
        if not review.done():
            review.cancel()
        await asyncio.gather(transport, review, return_exceptions=True)
        broker.close()


async def test_different_stream_item_after_deferred_decision_terminates_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handles = iter(("earlier-review-handle", "deferred-review-handle"))
    broker = _real_interaction_broker(lambda: next(handles))
    connection = broker.connect_reviewer()
    original_anext = ReviewerConnection.__anext__
    next_review_blocked = asyncio.Event()
    next_review_release = asyncio.Event()
    calls = 0

    async def delay_third_review_read(
        candidate: ReviewerConnection,
    ) -> ReviewEnvelope | ReviewWithdrawal:
        nonlocal calls
        calls += 1
        if candidate is connection and calls == 3:
            next_review_blocked.set()
            await next_review_release.wait()
        return await original_anext(candidate)

    monkeypatch.setattr(ReviewerConnection, "__anext__", delay_third_review_read)
    socket = _ServingSocket()
    transport = asyncio.create_task(
        reviewer_module._serve_review_messages(  # noqa: SLF001
            cast("object", socket),  # type: ignore[arg-type]
            cast("ReviewerBroker", broker),
            connection,
        )
    )
    earlier_review = asyncio.create_task(broker.review(_real_command_request("earlier")))
    deferred_review = asyncio.create_task(
        broker.review(_real_command_request("deferred", item_id="deferred-item"))
    )

    try:
        offers = [
            await asyncio.wait_for(socket.sent.get(), timeout=1),
            await asyncio.wait_for(socket.sent.get(), timeout=1),
        ]
        assert [offer["reviewHandle"] for offer in offers] == [
            "earlier-review-handle",
            "deferred-review-handle",
        ]
        await asyncio.wait_for(next_review_blocked.wait(), timeout=1)
        broker.cancel_pending()
        socket.messages.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "reviewHandle": "deferred-review-handle",
                        "decision": "accept",
                    }
                ),
            }
        )
        await asyncio.wait_for(socket.received.get(), timeout=1)
        stopped_before_stream_item, _ = await asyncio.wait({transport}, timeout=0.05)
        assert stopped_before_stream_item == set()

        next_review_release.set()
        with pytest.raises(CodexReviewError):
            await asyncio.wait_for(transport, timeout=1)
        assert not socket.resolved.is_set()
        with pytest.raises(CodexReviewError):
            await earlier_review
        with pytest.raises(CodexReviewError):
            await deferred_review
    finally:
        next_review_release.set()
        if not transport.done():
            transport.cancel()
        for review in (earlier_review, deferred_review):
            if not review.done():
                review.cancel()
        await asyncio.gather(transport, earlier_review, deferred_review, return_exceptions=True)
        broker.close()
