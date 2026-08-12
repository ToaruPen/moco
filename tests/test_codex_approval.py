from __future__ import annotations

import asyncio
import json
import re
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, fields, replace
from types import MappingProxyType
from typing import cast

import pytest

from moco.codex.approval import (
    ApprovalDecision,
    CommandApprovalReview,
    ConversationCallCorrelation,
    FileChangeApprovalReview,
    FileChangeEntry,
    FileChangeExplanation,
    FileChangeKind,
    ThreadItemCorrelation,
    adapt_approval_request,
    adapt_file_change_patch_notification,
)
from moco.codex.broker import (
    _MAX_HANDLE_ATTEMPTS,
    _MAX_UNREAD_REVIEWS,
    InteractionBroker,
    ReviewEnvelope,
    ReviewerConnection,
    ReviewWithdrawal,
)
from moco.codex.rpc import (
    JsonValue,
    RpcNotification,
    RpcPeer,
    RpcServerRequest,
    _validate_handler_result,
)
from moco.codex.schema import (
    AgentEventProfile,
    ApprovalCorrelation,
    ApprovalProfile,
    CodexProtocolContract,
    FileChangePatchProfile,
    ServerRequestCategory,
    _freeze_value_contract,
    _json_value_key,
    _ValueContract,
)
from moco.errors import CodexReviewError, CodexRpcProtocolError, CodexSchemaError

# Method names are read back from the discovered contract, never spelled by moco, so the
# tests pick names no observed Codex build uses.
COMMAND_METHOD = "alias/commandApproval"
FILE_METHOD = "alias/fileApproval"
LEGACY_COMMAND_METHOD = "alias/legacyCommandApproval"
LEGACY_FILE_METHOD = "alias/legacyFileApproval"
# Values a reviewer may see but no message, repr, or traceback may carry.
THREAD_ID = "thread-7f3ac21e"
TURN_ID = "turn-91b2d40a"
ITEM_ID = "item-55c1e8f7"
CONVERSATION_ID = "conversation-6b0d92a4"
CALL_ID = "call-2ad7fe10"
COMMAND = "git push --force origin release-candidate"
CWD = "/opt/moco-demo/workspace"
REASON = "needs network access to example.invalid"
CHANGED_PATH = "/opt/moco-demo/workspace/config/credentials.yaml"
# Where a legacy update moves that file to, which decides what accepting authorises just as
# much as the source does.
MOVED_PATH = "/opt/moco-demo/published/credentials.yaml"
PATCH_BODY = "@@ -1 +1 @@ -old-secret +new-secret"
REQUEST_ID = 41
SECRETS = (
    THREAD_ID,
    TURN_ID,
    ITEM_ID,
    CONVERSATION_ID,
    CALL_ID,
    COMMAND,
    CWD,
    REASON,
    CHANGED_PATH,
    MOVED_PATH,
    PATCH_BODY,
)
# One optional cross-version metadata value that must not become correlation or authority.
STARTED_AT_MS = 1_770_000_000_000
_OMITTED = object()
_ONE_SHOT = (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE, ApprovalDecision.CANCEL)
# A string Python holds but UTF-8 cannot encode, which the transport would refuse to write.
LONE_SURROGATE = "review-\udc80"


# One member's compiled schema, spelled as the retained generated bundles spell it. The
# loader compiles these from the effective schema; a test states the shape it needs.
STRING = _ValueContract(types=frozenset({"string"}))
NULLABLE_STRING = _ValueContract(types=frozenset({"string", "null"}))
INT64 = _ValueContract(types=frozenset({"integer"}), int64=True)


def literal(*values: str) -> _ValueContract:
    return _ValueContract(
        types=frozenset({"string"}),
        enum=tuple(cast("tuple[str, str]", _json_value_key(value)) for value in values),
    )


def obj(
    properties: dict[str, _ValueContract],
    required: tuple[str, ...] = (),
    *,
    refuse_extra: bool = False,
    nullable: bool = False,
    additional: _ValueContract | None = None,
) -> _ValueContract:
    return _ValueContract(
        types=frozenset({"object", "null"} if nullable else {"object"}),
        properties=properties,
        required=frozenset(required),
        additional=additional,
        additional_refused=refuse_extra,
    )


def array_of(items: _ValueContract, *, nullable: bool = False) -> _ValueContract:
    return _ValueContract(
        types=frozenset({"array", "null"} if nullable else {"array"}),
        items=items,
    )


def one_of(*branches: _ValueContract) -> _ValueContract:
    return _ValueContract(one_of=branches)


NETWORK_AMENDMENT = obj(
    {"action": literal("allow", "deny"), "host": STRING},
    ("action", "host"),
)
COMMAND_ACTION = one_of(
    obj(
        {"command": STRING, "name": STRING, "path": STRING, "type": literal("read")},
        ("command", "name", "path", "type"),
    ),
    obj({"command": STRING, "type": literal("unknown")}, ("command", "type")),
)
PARSED_COMMAND = one_of(
    obj(
        {"cmd": STRING, "name": STRING, "path": STRING, "type": literal("read")},
        ("cmd", "name", "path", "type"),
    ),
    obj({"cmd": STRING, "type": literal("unknown")}, ("cmd", "type")),
)
FILE_CHANGE = one_of(
    obj({"content": STRING, "type": literal("add")}, ("content", "type")),
    obj({"content": STRING, "type": literal("delete")}, ("content", "type")),
    obj(
        {"move_path": NULLABLE_STRING, "type": literal("update"), "unified_diff": STRING},
        ("type", "unified_diff"),
    ),
)
PATCH_CHANGE_KIND = one_of(
    obj({"type": literal("add")}, ("type",)),
    obj({"type": literal("delete")}, ("type",)),
    obj(
        {"move_path": NULLABLE_STRING, "type": literal("update")},
        ("type",),
    ),
)
PATCH_CHANGE = obj(
    {"diff": STRING, "kind": PATCH_CHANGE_KIND, "path": STRING},
    ("diff", "kind", "path"),
)
PATCH_PARAMS = obj(
    {
        "changes": _ValueContract(
            types=frozenset({"array"}),
            items=PATCH_CHANGE,
            min_items=1,
            max_items=64,
        ),
        "itemId": STRING,
        "threadId": STRING,
        "turnId": STRING,
    },
    ("changes", "itemId", "threadId", "turnId"),
)
# The newer decision vocabulary: two object variants moco never sends, each declaring the
# members its own build spells. Only the outer variant refuses an unknown member, exactly
# as the retained generated documents do.
EXECPOLICY_VARIANT = obj(
    {
        "acceptWithExecpolicyAmendment": obj(
            {"execpolicy_amendment": array_of(STRING)},
            ("execpolicy_amendment",),
        )
    },
    ("acceptWithExecpolicyAmendment",),
    refuse_extra=True,
)
NETWORK_VARIANT = obj(
    {
        "applyNetworkPolicyAmendment": obj(
            {"network_policy_amendment": NETWORK_AMENDMENT},
            ("network_policy_amendment",),
        )
    },
    ("applyNetworkPolicyAmendment",),
    refuse_extra=True,
)
DECISION_CONTRACT = one_of(
    literal("accept"),
    literal("acceptForSession"),
    EXECPOLICY_VARIANT,
    NETWORK_VARIANT,
    literal("decline"),
    literal("cancel"),
)
# The legacy vocabulary, in the two spellings the retained builds prove. Only the refusal
# differs: the older builds spell a plain string, the newer one an object carrying text.
LEGACY_EXECPOLICY_VARIANT = obj(
    {
        "approved_execpolicy_amendment": obj(
            {"proposed_execpolicy_amendment": array_of(STRING)},
            ("proposed_execpolicy_amendment",),
        )
    },
    ("approved_execpolicy_amendment",),
    refuse_extra=True,
)
LEGACY_NETWORK_VARIANT = obj(
    {
        "network_policy_amendment": obj(
            {"network_policy_amendment": NETWORK_AMENDMENT},
            ("network_policy_amendment",),
        )
    },
    ("network_policy_amendment",),
    refuse_extra=True,
)
DENIED_VARIANT = obj(
    {"denied": obj({"rejection": STRING}, ("rejection",))},
    ("denied",),
    refuse_extra=True,
)


def legacy_decision_contract(*, denied_object: bool) -> _ValueContract:
    refusal = DENIED_VARIANT if denied_object else literal("denied")
    return one_of(
        literal("approved"),
        LEGACY_EXECPOLICY_VARIANT,
        literal("approved_for_session"),
        LEGACY_NETWORK_VARIANT,
        refusal,
        literal("timed_out"),
        literal("abort"),
    )


LEGACY_STRING_DECISIONS = legacy_decision_contract(denied_object=False)
LEGACY_OBJECT_DECISIONS = legacy_decision_contract(denied_object=True)

# The two newer families the retained macOS bundles spell, as the loader reports them. Only
# the generated schema decides these, so a test that needs another build states its own.
COMMAND_CONTRACTS: dict[str, _ValueContract] = {
    "threadId": STRING,
    "turnId": STRING,
    "itemId": STRING,
    "command": NULLABLE_STRING,
    "cwd": NULLABLE_STRING,
    "reason": NULLABLE_STRING,
    "startedAtMs": INT64,
    "approvalId": NULLABLE_STRING,
    "commandActions": array_of(COMMAND_ACTION, nullable=True),
    "availableDecisions": array_of(DECISION_CONTRACT, nullable=True),
    "additionalPermissions": obj({"network": obj({})}, nullable=True),
    "environmentId": NULLABLE_STRING,
    "networkApprovalContext": obj(
        {"host": STRING, "protocol": literal("http", "https")},
        ("host", "protocol"),
        nullable=True,
    ),
    "proposedExecpolicyAmendment": array_of(STRING, nullable=True),
    "proposedNetworkPolicyAmendments": array_of(NETWORK_AMENDMENT, nullable=True),
}
COMMAND_WIDENING = frozenset(
    {
        "additionalPermissions",
        "environmentId",
        "networkApprovalContext",
        "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments",
    }
)
FILE_CONTRACTS: dict[str, _ValueContract] = {
    "threadId": STRING,
    "turnId": STRING,
    "itemId": STRING,
    "reason": NULLABLE_STRING,
    "startedAtMs": INT64,
    "grantRoot": NULLABLE_STRING,
}
LEGACY_COMMAND_CONTRACTS: dict[str, _ValueContract] = {
    "conversationId": STRING,
    "callId": STRING,
    "command": array_of(STRING),
    "cwd": STRING,
    "parsedCmd": array_of(PARSED_COMMAND),
    "approvalId": NULLABLE_STRING,
    "reason": NULLABLE_STRING,
}
LEGACY_FILE_CONTRACTS: dict[str, _ValueContract] = {
    "conversationId": STRING,
    "callId": STRING,
    "fileChanges": obj({}, additional=FILE_CHANGE),
    "grantRoot": NULLABLE_STRING,
    "reason": NULLABLE_STRING,
}
CORRELATION = frozenset({"threadId", "turnId", "itemId"})
LEGACY_CORRELATION = frozenset({"conversationId", "callId"})
ONE_SHOT_WIRE: dict[ApprovalDecision, JsonValue] = {
    ApprovalDecision.ACCEPT: "accept",
    ApprovalDecision.DECLINE: "decline",
    ApprovalDecision.CANCEL: "cancel",
}
LEGACY_STRING_WIRE: dict[ApprovalDecision, JsonValue] = {
    ApprovalDecision.ACCEPT: "approved",
    ApprovalDecision.DECLINE: "denied",
    ApprovalDecision.CANCEL: "abort",
}
LEGACY_OBJECT_WIRE: dict[ApprovalDecision, JsonValue] = {
    ApprovalDecision.ACCEPT: "approved",
    ApprovalDecision.DECLINE: {"denied": {"rejection": ""}},
    ApprovalDecision.CANCEL: "abort",
}


def command_profile(**overrides: object) -> ApprovalProfile:
    """The command approval profile a macOS experimental bundle proves."""
    members: dict[str, object] = {
        "category": ServerRequestCategory.COMMAND_APPROVAL,
        "correlation": ApprovalCorrelation.THREAD_ITEM,
        "required_members": CORRELATION | {"startedAtMs"},
        "absent_or_null_members": COMMAND_WIDENING,
        "member_contracts": dict(COMMAND_CONTRACTS),
        "argv_member": None,
        "changes_member": None,
        "offer_member": "availableDecisions",
        "decisions": dict(ONE_SHOT_WIRE),
        "decision_contract": DECISION_CONTRACT,
    }
    members.update(overrides)
    return ApprovalProfile(**members)  # type: ignore[arg-type]


def file_change_profile(**overrides: object) -> ApprovalProfile:
    """The file change approval profile a macOS bundle proves: no decision offer member."""
    members: dict[str, object] = {
        "category": ServerRequestCategory.FILE_CHANGE_APPROVAL,
        "correlation": ApprovalCorrelation.THREAD_ITEM,
        "required_members": CORRELATION | {"startedAtMs"},
        "absent_or_null_members": frozenset({"grantRoot"}),
        "member_contracts": dict(FILE_CONTRACTS),
        "argv_member": None,
        "changes_member": None,
        "offer_member": None,
        "decisions": dict(ONE_SHOT_WIRE),
        "decision_contract": DECISION_CONTRACT,
    }
    members.update(overrides)
    return ApprovalProfile(**members)  # type: ignore[arg-type]


def legacy_command_profile(*, denied_object: bool = False, **overrides: object) -> ApprovalProfile:
    """The legacy command approval profile every retained bundle still advertises."""
    members: dict[str, object] = {
        "category": ServerRequestCategory.COMMAND_APPROVAL,
        "correlation": ApprovalCorrelation.CONVERSATION_CALL,
        "required_members": LEGACY_CORRELATION | {"command", "cwd", "parsedCmd"},
        "absent_or_null_members": frozenset(),
        "member_contracts": dict(LEGACY_COMMAND_CONTRACTS),
        "argv_member": "command",
        "changes_member": None,
        "offer_member": None,
        "decisions": dict(LEGACY_OBJECT_WIRE if denied_object else LEGACY_STRING_WIRE),
        "decision_contract": (
            LEGACY_OBJECT_DECISIONS if denied_object else LEGACY_STRING_DECISIONS
        ),
    }
    members.update(overrides)
    return ApprovalProfile(**members)  # type: ignore[arg-type]


def legacy_file_profile(*, denied_object: bool = False, **overrides: object) -> ApprovalProfile:
    """The legacy file change profile, which states its own changed files."""
    members: dict[str, object] = {
        "category": ServerRequestCategory.FILE_CHANGE_APPROVAL,
        "correlation": ApprovalCorrelation.CONVERSATION_CALL,
        "required_members": LEGACY_CORRELATION | {"fileChanges"},
        "absent_or_null_members": frozenset({"grantRoot"}),
        "member_contracts": dict(LEGACY_FILE_CONTRACTS),
        "argv_member": None,
        "changes_member": "fileChanges",
        "offer_member": None,
        "decisions": dict(LEGACY_OBJECT_WIRE if denied_object else LEGACY_STRING_WIRE),
        "decision_contract": (
            LEGACY_OBJECT_DECISIONS if denied_object else LEGACY_STRING_DECISIONS
        ),
    }
    members.update(overrides)
    return ApprovalProfile(**members)  # type: ignore[arg-type]


# The retained Windows observation declares no started-at member at all, so the same two
# families read differently there. Nothing but the effective schema decides which applies.
WINDOWS_COMMAND_PROFILE = command_profile(
    member_contracts={
        name: contract for name, contract in COMMAND_CONTRACTS.items() if name != "startedAtMs"
    },
    required_members=CORRELATION,
)
WINDOWS_FILE_PROFILE = file_change_profile(
    member_contracts={
        name: contract for name, contract in FILE_CONTRACTS.items() if name != "startedAtMs"
    },
    required_members=CORRELATION,
)


def approval_contract(
    *,
    command_method: str = COMMAND_METHOD,
    file_method: str = FILE_METHOD,
    extra: dict[ServerRequestCategory, frozenset[str]] | None = None,
    profiles: dict[str, ApprovalProfile] | None = None,
) -> CodexProtocolContract:
    server_requests: dict[ServerRequestCategory, frozenset[str]] = {
        ServerRequestCategory.COMMAND_APPROVAL: frozenset({command_method}),
        ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset({file_method}),
    }
    server_requests.update(extra or {})
    return CodexProtocolContract(
        version="test-version",
        methods={},
        server_requests=server_requests,
        unclassified_server_request_count=0,
        experimental_schema=False,
        approval_profiles=profiles
        or {command_method: command_profile(), file_method: file_change_profile()},
    )


def file_change_patch_contract(*, agent_events: bool = False) -> CodexProtocolContract:
    patch_profile = FileChangePatchProfile(
        method="item/fileChange/patchUpdated",
        params_contract=PATCH_PARAMS,
    )
    base = approval_contract()
    event_profile = None
    if agent_events:
        event_profile = AgentEventProfile(
            turn_completed_method="turn/completed",
            item_completed_method="item/completed",
            agent_message_delta_method=None,
            turn_completed_required_fields=frozenset({"threadId", "turn"}),
            item_completed_required_fields=frozenset({"threadId", "turnId", "item"}),
            turn_required_fields=frozenset({"id", "items", "status"}),
            agent_message_required_fields=frozenset({"id", "text", "type"}),
            turn_completed_field_types={
                "threadId": frozenset({"string"}),
                "turn": frozenset({"object"}),
            },
            item_completed_field_types={
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
                "item": frozenset({"object"}),
            },
            turn_field_types={
                "id": frozenset({"string"}),
                "items": frozenset({"array"}),
                "status": frozenset({"string"}),
            },
            agent_message_field_types={
                "id": frozenset({"string"}),
                "phase": frozenset({"string", "null"}),
                "text": frozenset({"string"}),
                "type": frozenset({"string"}),
            },
            agent_message_phase_values=frozenset({"commentary", "final_answer"}),
            agent_message_phase_optional=True,
            turn_status_values=frozenset({"completed", "interrupted", "failed", "inProgress"}),
            completed_status="completed",
            interrupted_status="interrupted",
            failed_status="failed",
            in_progress_status="inProgress",
        )
    return CodexProtocolContract(
        version=base.version,
        methods=base.methods,
        server_requests=base.server_requests,
        unclassified_server_request_count=base.unclassified_server_request_count,
        experimental_schema=base.experimental_schema,
        approval_profiles=base.approval_profiles,
        agent_event_profile=event_profile,
        file_change_patch_profile=patch_profile,
    )


def legacy_contract(*, denied_object: bool = False) -> CodexProtocolContract:
    """A build advertising both the newer and the legacy alias of each required family."""
    return CodexProtocolContract(
        version="test-version",
        methods={},
        server_requests={
            ServerRequestCategory.COMMAND_APPROVAL: frozenset(
                {COMMAND_METHOD, LEGACY_COMMAND_METHOD}
            ),
            ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset(
                {FILE_METHOD, LEGACY_FILE_METHOD}
            ),
        },
        unclassified_server_request_count=0,
        experimental_schema=False,
        approval_profiles={
            COMMAND_METHOD: command_profile(),
            FILE_METHOD: file_change_profile(),
            LEGACY_COMMAND_METHOD: legacy_command_profile(denied_object=denied_object),
            LEGACY_FILE_METHOD: legacy_file_profile(denied_object=denied_object),
        },
    )


def merge(
    base: dict[str, JsonValue],
    overrides: dict[str, JsonValue | object],
) -> dict[str, JsonValue]:
    params = dict(base)
    for key, value in overrides.items():
        if value is _OMITTED:
            params.pop(key, None)
        else:
            params[key] = value  # type: ignore[assignment]
    return params


def command_params(**overrides: JsonValue | object) -> dict[str, JsonValue]:
    """The generated command execution approval params moco can explain."""
    base: dict[str, JsonValue] = {
        "threadId": THREAD_ID,
        "turnId": TURN_ID,
        "itemId": ITEM_ID,
        "command": COMMAND,
        "cwd": CWD,
        "startedAtMs": STARTED_AT_MS,
    }
    return merge(base, overrides)


def file_change_params(**overrides: JsonValue | object) -> dict[str, JsonValue]:
    """The generated file change approval params, which carry no patch body."""
    base: dict[str, JsonValue] = {
        "threadId": THREAD_ID,
        "turnId": TURN_ID,
        "itemId": ITEM_ID,
        "startedAtMs": STARTED_AT_MS,
    }
    return merge(base, overrides)


def file_change_patch_params(
    *,
    thread_id: str = THREAD_ID,
    turn_id: str = TURN_ID,
    item_id: str = ITEM_ID,
    changes: list[JsonValue] | None = None,
    **overrides: JsonValue | object,
) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "changes": changes
        if changes is not None
        else [
            {
                "diff": PATCH_BODY,
                "kind": {"move_path": MOVED_PATH, "type": "update"},
                "path": CHANGED_PATH,
            }
        ],
        "itemId": item_id,
        "threadId": thread_id,
        "turnId": turn_id,
    }
    return merge(base, overrides)


def adapt_file_change_patch(
    params: dict[str, JsonValue],
    *,
    method: str = "item/fileChange/patchUpdated",
) -> FileChangeExplanation | None:
    return adapt_file_change_patch_notification(
        file_change_patch_contract(),
        RpcNotification(method, params),
    )


def legacy_command_params(**overrides: JsonValue | object) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "conversationId": CONVERSATION_ID,
        "callId": CALL_ID,
        "command": ["git", "push", "--force"],
        "cwd": CWD,
        "parsedCmd": [{"cmd": COMMAND, "type": "unknown"}],
    }
    return merge(base, overrides)


def legacy_file_params(**overrides: JsonValue | object) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "conversationId": CONVERSATION_ID,
        "callId": CALL_ID,
        "fileChanges": {CHANGED_PATH: {"type": "update", "unified_diff": PATCH_BODY}},
    }
    return merge(base, overrides)


def explanation(
    *,
    thread_id: str = THREAD_ID,
    turn_id: str = TURN_ID,
    item_id: str = ITEM_ID,
    changes: tuple[FileChangeEntry, ...] | None = None,
) -> FileChangeExplanation:
    default = (FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH),)
    return FileChangeExplanation(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=item_id,
        changes=changes if changes is not None else default,
    )


def adapt_command(
    params: dict[str, JsonValue],
    *,
    contract: CodexProtocolContract | None = None,
    method: str = COMMAND_METHOD,
    request_id: object = REQUEST_ID,
) -> CommandApprovalReview:
    review = adapt_approval_request(
        contract or approval_contract(),
        method,
        params,
        request_id=cast("int", request_id),
        file_change_explanation=None,
    )
    assert isinstance(review, CommandApprovalReview)
    return review


def adapt_file_change(
    params: dict[str, JsonValue],
    *,
    file_change_explanation: FileChangeExplanation | None = None,
    contract: CodexProtocolContract | None = None,
    method: str = FILE_METHOD,
) -> FileChangeApprovalReview:
    review = adapt_approval_request(
        contract or approval_contract(),
        method,
        params,
        request_id=REQUEST_ID,
        file_change_explanation=file_change_explanation or explanation(),
    )
    assert isinstance(review, FileChangeApprovalReview)
    return review


def thread_correlation(review: CommandApprovalReview | FileChangeApprovalReview) -> tuple[str, ...]:
    correlation = review.correlation
    assert isinstance(correlation, ThreadItemCorrelation)
    return (correlation.thread_id, correlation.turn_id, correlation.item_id)


def test_command_approval_review_carries_generated_params() -> None:
    review = adapt_command(command_params(reason=REASON))

    assert review.category is ServerRequestCategory.COMMAND_APPROVAL
    assert thread_correlation(review) == (THREAD_ID, TURN_ID, ITEM_ID)
    assert review.correlation.request_id == REQUEST_ID
    assert (review.command, review.cwd, review.reason) == (COMMAND, CWD, REASON)
    assert review.decisions == _ONE_SHOT


def test_command_approval_accepts_the_shape_without_optional_started_at() -> None:
    """The retained Windows observation declares no `startedAtMs` property at all."""
    contract = approval_contract(profiles={COMMAND_METHOD: WINDOWS_COMMAND_PROFILE})

    review = adapt_command(command_params(startedAtMs=_OMITTED), contract=contract)

    assert review.command == COMMAND
    assert review.decisions == _ONE_SHOT


def test_optional_cross_version_metadata_is_not_retained() -> None:
    review = adapt_command(command_params())

    stored = [getattr(review, field.name) for field in fields(review)]
    assert STARTED_AT_MS not in stored
    assert not any(name.lower().startswith("started") for name in (f.name for f in fields(review)))


@pytest.mark.parametrize("command_method", ["execCommandApproval", "item/x/requestApproval"])
def test_supported_method_names_come_from_the_discovered_contract(command_method: str) -> None:
    """Routing follows the advertised alias alone; the params still decide what is adapted."""
    contract = approval_contract(command_method=command_method)

    review = adapt_command(command_params(), contract=contract, method=command_method)

    assert review.command == COMMAND


def test_method_absent_from_the_contract_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            approval_contract(),
            "alias/unadvertised",
            command_params(),
            request_id=REQUEST_ID,
        )


def test_ambiguous_method_alias_fails_closed() -> None:
    contract = approval_contract(
        extra={ServerRequestCategory.PERMISSION_APPROVAL: frozenset({COMMAND_METHOD})}
    )

    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            contract,
            COMMAND_METHOD,
            command_params(),
            request_id=REQUEST_ID,
        )


@pytest.mark.parametrize(
    "category",
    [
        ServerRequestCategory.PERMISSION_APPROVAL,
        ServerRequestCategory.MCP_ELICITATION,
        ServerRequestCategory.USER_INPUT,
        ServerRequestCategory.DYNAMIC_TOOL_CALL,
    ],
)
def test_other_recognized_categories_are_not_adapted(category: ServerRequestCategory) -> None:
    contract = approval_contract(extra={category: frozenset({"alias/other"})})

    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            contract,
            "alias/other",
            command_params(),
            request_id=REQUEST_ID,
        )


def test_legacy_command_payload_under_a_newer_profile_fails_closed() -> None:
    """Each advertised alias is read as its own family, never as the other one's."""
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            approval_contract(),
            COMMAND_METHOD,
            legacy_command_params(),
            request_id=REQUEST_ID,
        )


def test_newer_command_payload_under_a_legacy_profile_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            legacy_contract(),
            LEGACY_COMMAND_METHOD,
            command_params(),
            request_id=REQUEST_ID,
        )


def test_legacy_command_review_carries_its_own_correlation_and_argument_vector() -> None:
    review = adapt_approval_request(
        legacy_contract(),
        LEGACY_COMMAND_METHOD,
        legacy_command_params(reason=REASON),
        request_id=REQUEST_ID,
    )

    assert isinstance(review, CommandApprovalReview)
    correlation = review.correlation
    assert isinstance(correlation, ConversationCallCorrelation)
    assert (correlation.conversation_id, correlation.call_id) == (CONVERSATION_ID, CALL_ID)
    assert correlation.request_id == REQUEST_ID
    # An argument vector is shown unjoined: quoting it would show a command that is not the
    # one about to run.
    assert review.command == ("git", "push", "--force")
    assert (review.cwd, review.reason) == (CWD, REASON)
    assert review.decisions == _ONE_SHOT


def test_a_legacy_review_states_no_turn_or_item_it_never_received() -> None:
    review = adapt_approval_request(
        legacy_contract(),
        LEGACY_COMMAND_METHOD,
        legacy_command_params(),
        request_id=REQUEST_ID,
    )

    assert not hasattr(review.correlation, "turn_id")
    assert not hasattr(review.correlation, "item_id")


def test_legacy_file_review_reads_its_own_changed_files() -> None:
    params = legacy_file_params(
        fileChanges={
            CHANGED_PATH: {"type": "update", "unified_diff": PATCH_BODY},
            f"{CWD}/added.py": {"type": "add", "content": PATCH_BODY},
            f"{CWD}/gone.py": {"type": "delete", "content": PATCH_BODY},
        }
    )

    review = adapt_approval_request(
        legacy_contract(),
        LEGACY_FILE_METHOD,
        params,
        request_id=REQUEST_ID,
    )

    assert isinstance(review, FileChangeApprovalReview)
    assert {(entry.kind, entry.path) for entry in review.changes} == {
        (FileChangeKind.UPDATE, CHANGED_PATH),
        (FileChangeKind.ADD, f"{CWD}/added.py"),
        (FileChangeKind.DELETE, f"{CWD}/gone.py"),
    }
    # The patch body beside each change is never read into the review.
    assert PATCH_BODY not in repr(review.changes)


def adapt_legacy_file(changes: JsonValue) -> FileChangeApprovalReview:
    review = adapt_approval_request(
        legacy_contract(),
        LEGACY_FILE_METHOD,
        legacy_file_params(fileChanges=changes),
        request_id=REQUEST_ID,
    )
    assert isinstance(review, FileChangeApprovalReview)
    return review


def test_a_legacy_move_states_both_endpoints_before_a_decision_exists() -> None:
    """Accepting a move authorises the destination as much as the source, so both are shown."""
    review = adapt_legacy_file(
        {CHANGED_PATH: {"type": "update", "unified_diff": PATCH_BODY, "move_path": MOVED_PATH}}
    )

    assert [(entry.kind, entry.path, entry.destination) for entry in review.changes] == [
        (FileChangeKind.UPDATE, CHANGED_PATH, MOVED_PATH)
    ]
    assert review.response_for(ApprovalDecision.ACCEPT) == {"decision": "approved"}


@pytest.mark.parametrize(
    "change",
    [
        {"type": "update", "unified_diff": PATCH_BODY},
        {"type": "update", "unified_diff": PATCH_BODY, "move_path": None},
    ],
)
def test_a_legacy_change_that_moves_nothing_states_no_destination(change: JsonValue) -> None:
    review = adapt_legacy_file({CHANGED_PATH: change})

    assert [entry.destination for entry in review.changes] == [None]


@pytest.mark.parametrize(
    "destination",
    [
        # A schema-valid string the reviewer could not be shown or the transport could not
        # carry names no destination at all.
        "",
        "   ",
        LONE_SURROGATE,
    ],
)
def test_a_legacy_move_destination_that_names_nothing_fails_closed(destination: str) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_legacy_file(
            {
                CHANGED_PATH: {
                    "type": "update",
                    "unified_diff": PATCH_BODY,
                    "move_path": destination,
                }
            }
        )


def test_a_legacy_change_member_moco_cannot_explain_fails_closed() -> None:
    """A change carrying a member this reviewer never reads could widen what accept means."""
    with pytest.raises(CodexSchemaError):
        adapt_legacy_file(
            {
                CHANGED_PATH: {
                    "type": "update",
                    "unified_diff": PATCH_BODY,
                    "unknownScopeMember": MOVED_PATH,
                }
            }
        )


def test_a_legacy_move_destination_never_reaches_a_rendering() -> None:
    review = adapt_legacy_file(
        {CHANGED_PATH: {"type": "update", "unified_diff": PATCH_BODY, "move_path": MOVED_PATH}}
    )

    rendered = f"{review!r} {review.changes!r}"
    assert not any(secret in rendered for secret in SECRETS)


def test_a_legacy_move_destination_cannot_be_rewritten() -> None:
    review = adapt_legacy_file(
        {CHANGED_PATH: {"type": "update", "unified_diff": PATCH_BODY, "move_path": MOVED_PATH}}
    )

    with pytest.raises(FrozenInstanceError):
        review.changes[0].destination = CHANGED_PATH  # type: ignore[misc]


def test_a_move_explained_for_another_request_is_never_reviewed() -> None:
    moved = FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH, MOVED_PATH)

    with pytest.raises(CodexSchemaError):
        adapt_file_change(
            file_change_params(),
            file_change_explanation=explanation(item_id="item-other", changes=(moved,)),
        )


def test_an_explained_move_states_both_endpoints() -> None:
    moved = FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH, MOVED_PATH)

    review = adapt_file_change(
        file_change_params(),
        file_change_explanation=explanation(changes=(moved,)),
    )

    assert [(entry.path, entry.destination) for entry in review.changes] == [
        (CHANGED_PATH, MOVED_PATH)
    ]


@pytest.mark.parametrize(
    ("kind", "destination"),
    [
        # Only an update states a destination; a created or deleted file that claims one is
        # an operation this reviewer cannot describe.
        (FileChangeKind.ADD, MOVED_PATH),
        (FileChangeKind.DELETE, MOVED_PATH),
        (FileChangeKind.UPDATE, ""),
        (FileChangeKind.UPDATE, LONE_SURROGATE),
        (FileChangeKind.UPDATE, 7),
    ],
)
def test_a_file_change_entry_destination_it_cannot_state_is_rejected(
    kind: FileChangeKind,
    destination: object,
) -> None:
    with pytest.raises(CodexSchemaError):
        FileChangeEntry(kind, CHANGED_PATH, cast("str", destination))


def test_a_legacy_file_request_refuses_an_external_explanation() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            legacy_contract(),
            LEGACY_FILE_METHOD,
            legacy_file_params(),
            request_id=REQUEST_ID,
            file_change_explanation=explanation(),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {CHANGED_PATH: {"type": "rename", "unified_diff": PATCH_BODY}},
        {CHANGED_PATH: {"type": 7, "unified_diff": PATCH_BODY}},
        {CHANGED_PATH: {"unified_diff": PATCH_BODY}},
        {CHANGED_PATH: "update"},
        {"": {"type": "add", "content": PATCH_BODY}},
        {CHANGED_PATH: {"type": "add"}},
    ],
)
def test_an_unreadable_legacy_file_change_fails_closed(changes: JsonValue) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            legacy_contract(),
            LEGACY_FILE_METHOD,
            legacy_file_params(fileChanges=changes),
            request_id=REQUEST_ID,
        )


@pytest.mark.parametrize(
    ("denied_object", "wire"),
    [
        (False, "denied"),
        (True, {"denied": {"rejection": ""}}),
    ],
)
def test_the_legacy_refusal_uses_the_shape_that_build_proves(
    denied_object: bool,
    wire: JsonValue,
) -> None:
    """The retained builds spell the legacy refusal in two shapes; both stay answerable."""
    contract = legacy_contract(denied_object=denied_object)

    review = adapt_approval_request(
        contract,
        LEGACY_COMMAND_METHOD,
        legacy_command_params(),
        request_id=REQUEST_ID,
    )

    assert review.response_for(ApprovalDecision.DECLINE) == {"decision": wire}
    assert review.response_for(ApprovalDecision.CANCEL) == {"decision": "abort"}
    assert review.response_for(ApprovalDecision.ACCEPT) == {"decision": "approved"}


@pytest.mark.parametrize("denied_object", [False, True])
def test_a_legacy_object_refusal_survives_the_transport(denied_object: bool) -> None:
    contract = legacy_contract(denied_object=denied_object)
    review = adapt_approval_request(
        contract,
        LEGACY_FILE_METHOD,
        legacy_file_params(),
        request_id=REQUEST_ID,
    )

    response = review.response_for(ApprovalDecision.DECLINE)

    assert type(response) is dict
    assert _validate_handler_result(response) == response
    assert json.dumps(response, allow_nan=False).encode()


@pytest.mark.parametrize("field_name", ["threadId", "turnId", "itemId", "command", "cwd"])
def test_missing_required_command_field_fails_closed(field_name: str) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(**{field_name: _OMITTED}))


@pytest.mark.parametrize("field_name", ["command", "cwd"])
def test_unexplained_command_scope_fails_closed(field_name: str) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(**{field_name: None}))


def test_unknown_command_field_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(unknownFutureField="value"))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("threadId", 7),
        ("threadId", ""),
        ("turnId", None),
        ("itemId", True),
        ("command", [COMMAND]),
        ("command", ""),
        ("cwd", {"path": CWD}),
        ("reason", 3),
        ("startedAtMs", "1770000000000"),
        ("startedAtMs", True),
        ("startedAtMs", 1.5),
        ("availableDecisions", "accept"),
    ],
)
def test_wrong_json_type_fails_closed(field_name: str, value: JsonValue) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(**{field_name: value}))


@pytest.mark.parametrize(
    "started_at",
    [2**63, -(2**63) - 1, 10**30],
)
def test_an_int64_member_outside_the_declared_range_fails_closed(started_at: int) -> None:
    """A timestamp the app server could not have serialized is not a value moco reads."""
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(startedAtMs=started_at))


@pytest.mark.parametrize("started_at", [0, 2**63 - 1, -(2**63)])
def test_an_int64_member_inside_the_declared_range_is_admitted(started_at: int) -> None:
    review = adapt_command(command_params(startedAtMs=started_at))

    assert review.decisions == _ONE_SHOT


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("approvalId", 7),
        ("approvalId", ["approval-3f"]),
        ("commandActions", "unknown"),
        ("commandActions", [{"type": "unknown"}]),
        ("commandActions", [{"type": "unknown", "command": 7}]),
        ("commandActions", [{"type": "read", "command": COMMAND, "name": "cat", "path": 7}]),
        ("commandActions", [{"type": "rename", "command": COMMAND}]),
        ("commandActions", [[{"type": "unknown", "command": COMMAND}]]),
        ("networkApprovalContext", {"host": "example.invalid", "protocol": "gopher"}),
    ],
)
def test_a_display_only_member_that_its_own_schema_refuses_fails_closed(
    field_name: str,
    value: JsonValue,
) -> None:
    """A member moco only displays or drops is still checked whole before any review."""
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(**{field_name: value}))


def test_params_that_are_not_an_object_fail_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            approval_contract(),
            COMMAND_METHOD,
            cast("dict[str, JsonValue]", []),
            request_id=REQUEST_ID,
        )


def test_params_with_a_member_name_that_is_not_text_fail_closed() -> None:
    params = cast("dict[str, JsonValue]", {3: COMMAND})

    with pytest.raises(CodexSchemaError):
        adapt_command(params)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("environmentId", "container-1"),
        ("networkApprovalContext", {"host": "example.invalid", "protocol": "https"}),
        ("proposedExecpolicyAmendment", ["allow git push"]),
        ("proposedNetworkPolicyAmendments", [{"action": "allow", "host": "example.invalid"}]),
        ("additionalPermissions", {"network": {}}),
    ],
)
def test_continuing_or_widened_command_scope_fails_closed(
    field_name: str,
    value: JsonValue,
) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(**{field_name: value}))


@pytest.mark.parametrize(
    "field_name",
    [
        "environmentId",
        "networkApprovalContext",
        "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments",
        "additionalPermissions",
        "reason",
        "approvalId",
        "commandActions",
    ],
)
def test_explicitly_null_optional_command_fields_are_admitted(field_name: str) -> None:
    review = adapt_command(command_params(**{field_name: None}))

    assert review.decisions == _ONE_SHOT


def test_display_only_command_fields_do_not_widen_scope() -> None:
    review = adapt_command(
        command_params(
            approvalId="approval-3f",
            commandActions=[{"type": "unknown", "command": COMMAND}],
        )
    )

    assert (review.command, review.cwd) == (COMMAND, CWD)


def test_available_decisions_narrow_the_presented_set() -> None:
    review = adapt_command(command_params(availableDecisions=["cancel", "accept"]))

    assert review.decisions == (ApprovalDecision.CANCEL, ApprovalDecision.ACCEPT)


def test_available_decisions_drop_session_and_object_variants() -> None:
    offered: JsonValue = [
        "accept",
        "acceptForSession",
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow"]}},
        {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {"action": "allow", "host": "example.invalid"}
            }
        },
        "decline",
    ]

    review = adapt_command(command_params(availableDecisions=offered))

    assert review.decisions == (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE)


@pytest.mark.parametrize(
    "offered",
    [
        ["accept", "approveEverythingForever"],
        ["accept", {"unknownObjectDecision": {}}],
        ["accept", 3],
        ["accept", None],
        [],
        ["acceptForSession"],
        ["accept"],
    ],
)
def test_unpresentable_decision_offer_fails_closed(offered: JsonValue) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(availableDecisions=offered))


def test_repeated_decision_offer_is_presented_once() -> None:
    review = adapt_command(command_params(availableDecisions=["accept", "accept", "decline"]))

    assert review.decisions == (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE)


def test_absent_decision_offer_presents_the_supported_one_shot_set() -> None:
    review = adapt_command(command_params(availableDecisions=None))

    assert review.decisions == _ONE_SHOT


@pytest.mark.parametrize(
    ("decision", "wire"),
    [
        (ApprovalDecision.ACCEPT, "accept"),
        (ApprovalDecision.DECLINE, "decline"),
        (ApprovalDecision.CANCEL, "cancel"),
    ],
)
def test_decision_response_keeps_decline_and_cancel_apart(
    decision: ApprovalDecision,
    wire: str,
) -> None:
    review = adapt_command(command_params())

    assert dict(review.response_for(decision)) == {"decision": wire}


def test_file_change_decision_response_uses_the_same_one_shot_contract() -> None:
    review = adapt_file_change(file_change_params())

    assert dict(review.response_for(ApprovalDecision.CANCEL)) == {"decision": "cancel"}


def test_decision_response_is_immutable() -> None:
    """The response is the caller's to send; the reviewed state it came from never changes."""
    review = adapt_command(command_params())

    response = review.response_for(ApprovalDecision.ACCEPT)
    response["decision"] = "acceptForSession"
    response["extra"] = True

    assert review.decisions == _ONE_SHOT
    assert review.response_for(ApprovalDecision.ACCEPT) == {"decision": "accept"}
    with pytest.raises(FrozenInstanceError):
        review.decisions = ()  # type: ignore[misc]


def test_a_nested_object_response_cannot_be_reached_through_the_returned_value() -> None:
    """A build answering with an object must still hand out a fresh value every time."""
    contract = legacy_contract(denied_object=True)
    review = adapt_approval_request(
        contract,
        LEGACY_COMMAND_METHOD,
        legacy_command_params(),
        request_id=REQUEST_ID,
    )

    first = review.response_for(ApprovalDecision.DECLINE)
    nested = cast("dict[str, JsonValue]", first["decision"])
    cast("dict[str, JsonValue]", nested["denied"])["rejection"] = "REJECTION_SECRET"

    assert review.response_for(ApprovalDecision.DECLINE) == {
        "decision": {"denied": {"rejection": ""}}
    }


def test_decision_outside_the_presented_set_fails_closed() -> None:
    review = adapt_command(command_params(availableDecisions=["decline", "cancel"]))

    with pytest.raises(CodexSchemaError):
        review.response_for(ApprovalDecision.ACCEPT)


def test_file_change_review_carries_the_correlated_explanation() -> None:
    entries = (
        FileChangeEntry(FileChangeKind.ADD, CHANGED_PATH),
        FileChangeEntry(FileChangeKind.DELETE, f"{CWD}/old.txt"),
    )

    review = adapt_file_change(
        file_change_params(reason=REASON),
        file_change_explanation=explanation(changes=entries),
    )

    assert review.category is ServerRequestCategory.FILE_CHANGE_APPROVAL
    assert thread_correlation(review) == (THREAD_ID, TURN_ID, ITEM_ID)
    assert review.changes == entries
    assert review.reason == REASON
    assert review.decisions == _ONE_SHOT


def test_file_change_patch_notification_adapts_without_retaining_diff() -> None:
    value = adapt_file_change_patch(
        file_change_patch_params(
            changes=[
                {"diff": "add secret", "kind": {"type": "add"}, "path": "added.txt"},
                {
                    "diff": PATCH_BODY,
                    "kind": {"move_path": MOVED_PATH, "type": "update"},
                    "path": CHANGED_PATH,
                },
                {"diff": "delete secret", "kind": {"type": "delete"}, "path": "gone.txt"},
            ]
        )
    )

    assert value == FileChangeExplanation(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        item_id=ITEM_ID,
        changes=(
            FileChangeEntry(FileChangeKind.ADD, "added.txt"),
            FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH, MOVED_PATH),
            FileChangeEntry(FileChangeKind.DELETE, "gone.txt"),
        ),
    )
    assert PATCH_BODY not in repr(value)


@pytest.mark.parametrize(
    "params",
    [
        file_change_patch_params(unknown="value"),
        file_change_patch_params(changes=[]),
        file_change_patch_params(
            changes=[{"diff": PATCH_BODY, "kind": {"type": "add"}, "path": CHANGED_PATH}] * 65
        ),
        file_change_patch_params(
            changes=[
                {
                    "diff": PATCH_BODY,
                    "kind": {"type": "add"},
                    "path": CHANGED_PATH,
                    "unknown": "value",
                }
            ]
        ),
        file_change_patch_params(
            changes=[{"diff": PATCH_BODY, "kind": {"type": "future"}, "path": CHANGED_PATH}]
        ),
        file_change_patch_params(
            changes=[
                {
                    "diff": PATCH_BODY,
                    "kind": {"move_path": 7, "type": "update"},
                    "path": CHANGED_PATH,
                }
            ]
        ),
        file_change_patch_params(
            changes=[{"diff": 7, "kind": {"type": "delete"}, "path": CHANGED_PATH}]
        ),
    ],
    ids=[
        "unknown-top-level",
        "empty-changes",
        "too-many-changes",
        "unknown-change-member",
        "unknown-kind",
        "invalid-move-path",
        "invalid-diff",
    ],
)
def test_malformed_file_change_patch_notification_fails_closed(
    params: dict[str, JsonValue],
) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_file_change_patch(params)


def test_unrelated_notification_is_not_a_file_change_patch() -> None:
    assert adapt_file_change_patch(file_change_patch_params(), method="item/other") is None


def test_file_change_without_an_explanation_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            approval_contract(),
            FILE_METHOD,
            file_change_params(),
            request_id=REQUEST_ID,
        )


def test_file_change_explanation_for_another_item_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_file_change(
            file_change_params(),
            file_change_explanation=explanation(item_id="item-other"),
        )


def test_empty_file_change_explanation_is_rejected() -> None:
    with pytest.raises(CodexSchemaError):
        FileChangeExplanation(thread_id=THREAD_ID, turn_id=TURN_ID, item_id=ITEM_ID, changes=())


@pytest.mark.parametrize("item_id", ["", "   "])
def test_file_change_explanation_without_an_item_is_rejected(item_id: str) -> None:
    with pytest.raises(CodexSchemaError):
        explanation(item_id=item_id)


def test_file_change_explanation_rejects_an_unexplained_change_value() -> None:
    changes = cast("tuple[FileChangeEntry, ...]", (CHANGED_PATH,))

    with pytest.raises(CodexSchemaError):
        FileChangeExplanation(
            thread_id=THREAD_ID, turn_id=TURN_ID, item_id=ITEM_ID, changes=changes
        )


@pytest.mark.parametrize("path", ["", "  "])
def test_file_change_entry_without_a_path_is_rejected(path: str) -> None:
    with pytest.raises(CodexSchemaError):
        FileChangeEntry(FileChangeKind.UPDATE, path)


def test_file_change_entry_without_a_known_kind_is_rejected() -> None:
    kind = cast("FileChangeKind", "update")

    with pytest.raises(CodexSchemaError):
        FileChangeEntry(kind, CHANGED_PATH)


def test_command_approval_with_a_file_explanation_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            approval_contract(),
            COMMAND_METHOD,
            command_params(),
            request_id=REQUEST_ID,
            file_change_explanation=explanation(),
        )


def test_session_wide_file_grant_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_file_change(file_change_params(grantRoot=CWD))


def test_null_file_grant_is_admitted() -> None:
    review = adapt_file_change(file_change_params(grantRoot=None))

    assert review.decisions == _ONE_SHOT


def test_unknown_file_change_field_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_file_change(file_change_params(unknownFutureField="value"))


@pytest.mark.parametrize("field_name", ["threadId", "turnId", "itemId"])
def test_missing_required_file_change_field_fails_closed(field_name: str) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_file_change(file_change_params(**{field_name: _OMITTED}))


def test_file_change_shape_without_optional_started_at_is_admitted() -> None:
    contract = approval_contract(profiles={FILE_METHOD: WINDOWS_FILE_PROFILE})

    review = adapt_file_change(file_change_params(startedAtMs=_OMITTED), contract=contract)

    assert thread_correlation(review) == (THREAD_ID, TURN_ID, ITEM_ID)


@pytest.mark.parametrize(
    "review",
    [
        adapt_command(command_params(reason=REASON)),
        adapt_file_change(file_change_params(reason=REASON)),
        adapt_approval_request(
            legacy_contract(),
            LEGACY_COMMAND_METHOD,
            legacy_command_params(reason=REASON),
            request_id=REQUEST_ID,
        ),
        adapt_approval_request(
            legacy_contract(),
            LEGACY_FILE_METHOD,
            legacy_file_params(reason=REASON),
            request_id=REQUEST_ID,
        ),
    ],
)
def test_review_rendering_is_privacy_safe(
    review: CommandApprovalReview | FileChangeApprovalReview,
) -> None:
    rendered = (
        repr(review),
        str(review),
        f"{review}",
        repr(review.decisions),
        repr(review.correlation),
        str(review.correlation),
    )

    for text in rendered:
        assert not any(secret in text for secret in SECRETS)
    assert type(review).__name__ in repr(review)


def test_file_change_entry_rendering_is_privacy_safe() -> None:
    entry = FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH)

    for text in (repr(entry), str(entry), repr(explanation(changes=(entry,)))):
        assert CHANGED_PATH not in text
        assert ITEM_ID not in text


@pytest.mark.parametrize(
    "params",
    [
        command_params(unknownFutureField=CHANGED_PATH),
        command_params(command=None),
        command_params(availableDecisions=["approveEverythingForever"]),
        command_params(commandActions=[{"type": "unknown", "command": 7, "note": CHANGED_PATH}]),
    ],
)
def test_failure_reporting_is_privacy_safe(params: dict[str, JsonValue]) -> None:
    with pytest.raises(CodexSchemaError) as failure:
        adapt_command(params)

    report = "".join(traceback.format_exception(failure.value))
    assert not any(secret in report for secret in SECRETS)
    assert "unknownFutureField" not in report


@pytest.mark.parametrize(
    "review",
    [
        adapt_command(command_params()),
        adapt_file_change(file_change_params()),
    ],
)
def test_reviews_are_immutable(review: CommandApprovalReview | FileChangeApprovalReview) -> None:
    with pytest.raises(FrozenInstanceError):
        review.correlation = None  # type: ignore[assignment, misc]


def test_correlation_values_are_immutable() -> None:
    correlation = ThreadItemCorrelation(
        request_id=REQUEST_ID,
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        item_id=ITEM_ID,
    )

    with pytest.raises(FrozenInstanceError):
        correlation.thread_id = "thread-other"  # type: ignore[misc]


@pytest.mark.parametrize("request_id", [None, 1.5, True, b"41", "", "   ", LONE_SURROGATE])
def test_a_request_id_the_transport_never_carried_is_rejected(request_id: object) -> None:
    with pytest.raises(CodexSchemaError):
        ThreadItemCorrelation(
            request_id=cast("int", request_id),
            thread_id=THREAD_ID,
            turn_id=TURN_ID,
            item_id=ITEM_ID,
        )


@pytest.mark.parametrize("request_id", [0, -3, "request-41"])
def test_a_request_id_the_transport_can_carry_is_kept(request_id: object) -> None:
    correlation = ConversationCallCorrelation(
        request_id=cast("int", request_id),
        conversation_id=CONVERSATION_ID,
        call_id=CALL_ID,
    )

    assert correlation.request_id == request_id


def test_file_change_explanation_is_immutable() -> None:
    value = explanation()

    with pytest.raises(FrozenInstanceError):
        value.item_id = "item-other"  # type: ignore[misc]


class ShiftingParams(Mapping[str, JsonValue]):
    """Params whose member values change after one snapshot's worth of reads.

    A mapping that is not the decoder's own object may answer two reads of the same member
    differently, so it is not a params value this adapter reads at all.
    """

    def __init__(self, members: dict[str, JsonValue], shifted: dict[str, JsonValue]) -> None:
        self._members = dict(members)
        self._shifted = {**members, **shifted}
        self._reads_before_shift = len(self._members)
        self.reads = 0

    def __getitem__(self, key: str) -> JsonValue:
        self.reads += 1
        source = self._members if self.reads <= self._reads_before_shift else self._shifted
        return source[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._members)

    def __len__(self) -> int:
        return len(self._members)


class HostileParams(Mapping[str, JsonValue]):
    """Params that refuse to be read, quoting the payload in the refusal."""

    def __init__(self, members: dict[str, JsonValue], *, reads_before_failure: int) -> None:
        self._members = dict(members)
        self._remaining = reads_before_failure

    def _consume(self) -> None:
        if self._remaining <= 0:
            raise RuntimeError(COMMAND)
        self._remaining -= 1

    def __getitem__(self, key: str) -> JsonValue:
        self._consume()
        return self._members[key]

    def __iter__(self) -> Iterator[str]:
        self._consume()
        return iter(self._members)

    def __len__(self) -> int:
        return len(self._members)


class RepeatingParams(Mapping[str, JsonValue]):
    """Params that yield one member name again and again, never repeating a distinct one."""

    def __init__(self, name: str, value: JsonValue, repeats: int) -> None:
        self._name = name
        self._value = value
        self._repeats = repeats
        self.reads = 0

    def __getitem__(self, key: str) -> JsonValue:
        self.reads += 1
        return self._value

    def __iter__(self) -> Iterator[str]:
        for _ in range(self._repeats):
            yield self._name

    def __len__(self) -> int:
        return self._repeats


class HostileDict(dict[str, JsonValue]):
    """A params object that is a dict subclass, overriding how it is read."""

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(COMMAND)

    def items(self) -> Iterator[tuple[str, JsonValue]]:  # type: ignore[override]
        raise RuntimeError(COMMAND)


class HostileList(list[object]):
    """A sequence that rewrites itself while it is copied."""

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError(COMMAND)


class HostileTuple(tuple[object, ...]):
    """A tuple subclass whose iteration is not the built-in one."""

    __slots__ = ()

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError(COMMAND)


def test_decision_response_is_the_plain_json_object_the_transport_accepts() -> None:
    """A response the RPC boundary refuses never reaches Codex, so no decision is delivered."""
    review = adapt_command(command_params())

    response = review.response_for(ApprovalDecision.ACCEPT)

    assert type(response) is dict
    assert _validate_handler_result(response) == {"decision": "accept"}


def test_file_change_decision_response_is_also_accepted_by_the_transport() -> None:
    review = adapt_file_change(file_change_params())

    response = review.response_for(ApprovalDecision.DECLINE)

    assert _validate_handler_result(response) == {"decision": "decline"}


def test_decision_response_is_a_fresh_object_that_cannot_reach_review_state() -> None:
    review = adapt_command(command_params())

    first = review.response_for(ApprovalDecision.DECLINE)
    second = review.response_for(ApprovalDecision.DECLINE)
    first["decision"] = "accept"

    assert first is not second
    assert second == {"decision": "decline"}
    assert review.response_for(ApprovalDecision.DECLINE) == {"decision": "decline"}


def test_a_category_key_that_is_not_the_exact_enum_never_routes_to_the_file_branch() -> None:
    """A raw string category compares equal to the enum, so equality must not decide routing."""
    contract = CodexProtocolContract(
        version="test-version",
        methods={},
        server_requests={
            cast("ServerRequestCategory", str(ServerRequestCategory.COMMAND_APPROVAL)): frozenset(
                {COMMAND_METHOD}
            ),
        },
        unclassified_server_request_count=0,
        experimental_schema=False,
        approval_profiles={COMMAND_METHOD: command_profile()},
    )

    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            contract,
            COMMAND_METHOD,
            file_change_params(),
            request_id=REQUEST_ID,
            file_change_explanation=explanation(),
        )


@pytest.mark.parametrize(
    "params",
    [
        ShiftingParams(command_params(), {"threadId": "thread-shifted", "cwd": "/"}),
        HostileParams(command_params(), reads_before_failure=0),
        HostileParams(command_params(), reads_before_failure=1),
        HostileParams(command_params(), reads_before_failure=4),
        RepeatingParams("threadId", THREAD_ID, 100_000),
        HostileDict(command_params()),
    ],
)
def test_params_that_are_not_the_decoder_object_fail_closed(params: object) -> None:
    """The transport decodes a plain object, so nothing else is read as approval params."""
    with pytest.raises(CodexSchemaError) as failure:
        adapt_command(cast("dict[str, JsonValue]", params))

    report = "".join(traceback.format_exception(failure.value))
    assert not any(secret in report for secret in SECRETS)


def test_a_repeated_member_name_never_drives_an_unbounded_read() -> None:
    params = RepeatingParams("threadId", THREAD_ID, 100_000)

    with pytest.raises(CodexSchemaError):
        adapt_command(cast("dict[str, JsonValue]", params))

    assert params.reads == 0


def test_params_beyond_the_declared_bound_fail_closed() -> None:
    params = {f"member-{index}": index for index in range(65)}

    with pytest.raises(CodexSchemaError):
        adapt_command(cast("dict[str, JsonValue]", params))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("threadId", LONE_SURROGATE),
        ("cwd", LONE_SURROGATE),
        ("reason", LONE_SURROGATE),
        ("command", LONE_SURROGATE),
    ],
)
def test_a_value_utf8_cannot_carry_fails_closed(field_name: str, value: str) -> None:
    """A lone surrogate passes a JSON value check and then breaks the transport's write."""
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(**{field_name: value}))


def test_a_legacy_argument_utf8_cannot_carry_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            legacy_contract(),
            LEGACY_COMMAND_METHOD,
            legacy_command_params(command=["git", LONE_SURROGATE]),
            request_id=REQUEST_ID,
        )


def test_a_legacy_changed_path_utf8_cannot_carry_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            legacy_contract(),
            LEGACY_FILE_METHOD,
            legacy_file_params(
                fileChanges={LONE_SURROGATE: {"type": "add", "content": PATCH_BODY}}
            ),
            request_id=REQUEST_ID,
        )


def command_review_members(**overrides: object) -> dict[str, object]:
    members: dict[str, object] = {
        "profile": command_profile(),
        "correlation": ThreadItemCorrelation(
            request_id=REQUEST_ID,
            thread_id=THREAD_ID,
            turn_id=TURN_ID,
            item_id=ITEM_ID,
        ),
        "command": (COMMAND,),
        "cwd": CWD,
        "reason": None,
        "decisions": _ONE_SHOT,
    }
    members.update(overrides)
    return members


def file_review_members(**overrides: object) -> dict[str, object]:
    members: dict[str, object] = {
        "profile": file_change_profile(),
        "correlation": ThreadItemCorrelation(
            request_id=REQUEST_ID,
            thread_id=THREAD_ID,
            turn_id=TURN_ID,
            item_id=ITEM_ID,
        ),
        "reason": None,
        "changes": (FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH),),
        "decisions": _ONE_SHOT,
    }
    members.update(overrides)
    return members


@pytest.mark.parametrize(
    "overrides",
    [
        {"correlation": None},
        {"correlation": "CORRELATION_VALUE_SECRET"},
        {
            "correlation": ConversationCallCorrelation(
                request_id=REQUEST_ID,
                conversation_id=CONVERSATION_ID,
                call_id=CALL_ID,
            )
        },
        {"command": ()},
        {"command": (COMMAND, 7)},
        {"command": ("", "  ")},
        {"command": (LONE_SURROGATE,)},
        {"command": HostileList([COMMAND])},
        {"command": HostileTuple((COMMAND,))},
        {"cwd": 3},
        {"cwd": LONE_SURROGATE},
        {"reason": 3},
        {"reason": LONE_SURROGATE},
        {"decisions": (ApprovalDecision.ACCEPT, "decline")},
        {"decisions": ()},
        {"decisions": HostileList([ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE])},
    ],
)
def test_command_review_constructor_rejects_values_it_cannot_render(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError):
        CommandApprovalReview(**command_review_members(**overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"correlation": None},
        {"reason": ""},
        {"reason": LONE_SURROGATE},
        {"changes": ()},
        {"changes": (CHANGED_PATH,)},
        {"changes": HostileList([FileChangeEntry(FileChangeKind.ADD, CHANGED_PATH)])},
        {"changes": HostileTuple((FileChangeEntry(FileChangeKind.ADD, CHANGED_PATH),))},
        {"decisions": [ApprovalDecision.ACCEPT]},
    ],
)
def test_file_change_review_constructor_rejects_values_it_cannot_render(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError):
        FileChangeApprovalReview(**file_review_members(**overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        HostileList([FileChangeEntry(FileChangeKind.ADD, CHANGED_PATH)]),
        HostileTuple((FileChangeEntry(FileChangeKind.ADD, CHANGED_PATH),)),
    ],
)
def test_file_change_explanation_rejects_a_container_that_rewrites_itself(
    changes: object,
) -> None:
    with pytest.raises(CodexSchemaError) as failure:
        FileChangeExplanation(
            thread_id=THREAD_ID,
            turn_id=TURN_ID,
            item_id=ITEM_ID,
            changes=cast("tuple[FileChangeEntry, ...]", changes),
        )

    report = "".join(traceback.format_exception(failure.value))
    assert not any(secret in report for secret in SECRETS)


@pytest.mark.parametrize("value", [LONE_SURROGATE, f"{CWD}/{LONE_SURROGATE}"])
def test_a_file_change_entry_path_utf8_cannot_carry_is_rejected(value: str) -> None:
    with pytest.raises(CodexSchemaError):
        FileChangeEntry(FileChangeKind.ADD, value)


def test_review_decisions_cannot_alias_a_caller_owned_sequence() -> None:
    offered = [ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE]

    review = CommandApprovalReview(
        **command_review_members(decisions=offered)  # type: ignore[arg-type]
    )
    offered.clear()

    assert review.decisions == (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE)
    assert review.response_for(ApprovalDecision.ACCEPT) == {"decision": "accept"}


def test_review_command_cannot_alias_a_caller_owned_sequence() -> None:
    arguments = ["git", "push"]

    review = CommandApprovalReview(
        **command_review_members(command=arguments)  # type: ignore[arg-type]
    )
    arguments.clear()

    assert review.command == ("git", "push")


def test_file_change_explanation_changes_cannot_alias_a_caller_owned_sequence() -> None:
    entries = [FileChangeEntry(FileChangeKind.ADD, CHANGED_PATH)]

    value = FileChangeExplanation(
        thread_id=THREAD_ID,
        turn_id=TURN_ID,
        item_id=ITEM_ID,
        changes=cast("tuple[FileChangeEntry, ...]", entries),
    )
    entries.clear()

    assert len(value.changes) == 1


def test_review_constructor_failure_is_privacy_safe() -> None:
    with pytest.raises(CodexSchemaError) as failure:
        CommandApprovalReview(
            **command_review_members(command=object(), reason=COMMAND)  # type: ignore[arg-type]
        )

    report = "".join(traceback.format_exception(failure.value))
    assert not any(secret in report for secret in SECRETS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"thread_id": "thread-other"},
        {"turn_id": "turn-other"},
        {"item_id": "item-other"},
        {"thread_id": "thread-other", "turn_id": "turn-other"},
    ],
)
def test_file_change_explanation_from_another_request_fails_closed(
    overrides: dict[str, str],
) -> None:
    """One item id repeats across threads and turns, so all three must agree."""
    other = explanation(
        thread_id=overrides.get("thread_id", THREAD_ID),
        turn_id=overrides.get("turn_id", TURN_ID),
        item_id=overrides.get("item_id", ITEM_ID),
    )

    with pytest.raises(CodexSchemaError):
        adapt_file_change(file_change_params(), file_change_explanation=other)


def test_file_change_explanation_correlation_is_privacy_safe() -> None:
    value = explanation()

    for text in (repr(value), str(value), f"{value}"):
        assert not any(secret in text for secret in SECRETS)


@pytest.mark.parametrize("field_name", ["thread_id", "turn_id", "item_id"])
@pytest.mark.parametrize("value", ["", "   ", 7, None])
def test_file_change_explanation_without_a_correlation_value_is_rejected(
    field_name: str,
    value: object,
) -> None:
    members: dict[str, object] = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "item_id": ITEM_ID,
        "changes": (FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH),),
        field_name: value,
    }

    with pytest.raises(CodexSchemaError):
        FileChangeExplanation(**members)  # type: ignore[arg-type]


def test_a_required_member_this_build_declares_must_be_present() -> None:
    """The installed macOS builds require a started-at member the Windows build never has."""
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(startedAtMs=_OMITTED))


def test_a_member_this_build_never_declares_fails_closed() -> None:
    contract = approval_contract(profiles={COMMAND_METHOD: WINDOWS_COMMAND_PROFILE})

    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(), contract=contract)


def test_a_file_change_required_member_this_build_declares_must_be_present() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_file_change(file_change_params(startedAtMs=_OMITTED))


def test_a_file_change_member_this_build_never_declares_fails_closed() -> None:
    contract = approval_contract(profiles={FILE_METHOD: WINDOWS_FILE_PROFILE})

    with pytest.raises(CodexSchemaError):
        adapt_file_change(file_change_params(), contract=contract)


def test_a_build_without_a_decision_offer_member_rejects_one_in_the_payload() -> None:
    """Only the experimental bundles declare the offer member; a stable build never does."""
    contract = approval_contract(
        profiles={
            COMMAND_METHOD: command_profile(
                member_contracts={
                    name: contract
                    for name, contract in COMMAND_CONTRACTS.items()
                    if name != "availableDecisions"
                },
                offer_member=None,
            )
        }
    )

    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(availableDecisions=["accept", "decline"]), contract=contract)


@pytest.mark.parametrize(
    ("decision", "wire"),
    [
        (ApprovalDecision.ACCEPT, "approved"),
        (ApprovalDecision.DECLINE, "denied"),
        (ApprovalDecision.CANCEL, "abort"),
    ],
)
def test_decision_response_uses_the_wire_value_this_build_proves(
    decision: ApprovalDecision,
    wire: str,
) -> None:
    review = adapt_approval_request(
        legacy_contract(),
        LEGACY_COMMAND_METHOD,
        legacy_command_params(),
        request_id=REQUEST_ID,
    )

    assert review.response_for(decision) == {"decision": wire}


def test_a_decision_offer_is_read_in_this_build_vocabulary() -> None:
    offered: JsonValue = ["approved", "approved_for_session", "denied"]
    contract = legacy_contract()
    profile = legacy_command_profile(
        member_contracts={
            **LEGACY_COMMAND_CONTRACTS,
            "availableDecisions": array_of(LEGACY_STRING_DECISIONS, nullable=True),
        },
        offer_member="availableDecisions",
    )
    contract = CodexProtocolContract(
        version="test-version",
        methods=contract.methods,
        server_requests=dict(contract.server_requests),
        unclassified_server_request_count=0,
        experimental_schema=False,
        approval_profiles={
            **dict(contract.approval_profiles),
            LEGACY_COMMAND_METHOD: profile,
        },
    )

    review = adapt_approval_request(
        contract,
        LEGACY_COMMAND_METHOD,
        legacy_command_params(availableDecisions=offered),
        request_id=REQUEST_ID,
    )

    assert review.decisions == (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE)


def test_a_decision_offer_from_another_build_vocabulary_fails_closed() -> None:
    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(availableDecisions=["approved"]))


@pytest.mark.parametrize(
    "offered",
    [
        [{"acceptWithExecpolicyAmendment": {}}],
        [{"acceptWithExecpolicyAmendment": {"execpolicy_amendment": "allow"}}],
        [{"acceptWithExecpolicyAmendment": {"execpolicy_amendment": [7]}}],
        [{"acceptWithExecpolicyAmendment": "allow"}],
        [{"applyNetworkPolicyAmendment": {"execpolicy_amendment": []}}],
        [
            {
                "applyNetworkPolicyAmendment": {
                    "network_policy_amendment": {"action": "allow", "host": 7}
                }
            }
        ],
        [
            {
                "applyNetworkPolicyAmendment": {
                    "network_policy_amendment": {"action": "escalate", "host": "example.invalid"}
                }
            }
        ],
        [{"acceptWithExecpolicyAmendment": None}],
        [{}],
        [{"acceptWithExecpolicyAmendment": {}, "applyNetworkPolicyAmendment": {}}],
        [{"unknownObjectDecision": {"member": 1}}],
        [3],
        [None],
        [True],
    ],
)
def test_a_continuing_decision_moco_cannot_read_whole_fails_closed(offered: JsonValue) -> None:
    """A known key alone is not the decision; the whole offered value must satisfy its schema."""
    variants = cast("list[JsonValue]", offered)

    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(availableDecisions=[*variants, "accept", "decline"]))


def test_a_complete_continuing_decision_is_never_offered_as_a_one_shot_button() -> None:
    offered: JsonValue = [
        "accept",
        "acceptForSession",
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow git push"]}},
        "cancel",
    ]

    review = adapt_command(command_params(availableDecisions=offered))

    assert review.decisions == (ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL)


def test_a_continuing_decision_its_own_schema_permits_is_dropped_not_refused() -> None:
    """The generated inner object declares no `additionalProperties`, so an extra member is
    a value that build's own schema allows; moco still never sends it.
    """
    offered: JsonValue = [
        "accept",
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": [], "note": "future"}},
        "decline",
    ]

    review = adapt_command(command_params(availableDecisions=offered))

    assert review.decisions == (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE)


def test_an_unbounded_decision_offer_fails_closed() -> None:
    offered: JsonValue = ["accept", "decline"] * 200

    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(availableDecisions=offered))


def test_a_profile_whose_category_does_not_match_the_advertised_one_fails_closed() -> None:
    contract = approval_contract(profiles={COMMAND_METHOD: file_change_profile()})

    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(), contract=contract)


def test_a_method_without_a_profile_fails_closed() -> None:
    contract = approval_contract(profiles={FILE_METHOD: file_change_profile()})

    with pytest.raises(CodexSchemaError):
        adapt_command(command_params(), contract=contract)


HOSTILE_SECRET = "HOSTILE_ELEMENT_VALUE_SECRET"  # noqa: S105


class HostileText(str):
    """A string that attacks whatever compares, orders, iterates, or encodes it.

    It hashes like the plain string it spells, so an exact built-in dict or set accepts it
    and only reads it back through one of the protocols it has taken over.
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        raise RuntimeError(HOSTILE_SECRET)

    def __lt__(self, other: str) -> bool:
        raise RuntimeError(HOSTILE_SECRET)

    def __gt__(self, other: str) -> bool:
        raise RuntimeError(HOSTILE_SECRET)

    def __hash__(self) -> int:
        return str.__hash__(self)

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(HOSTILE_SECRET)

    def encode(self, *_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError(HOSTILE_SECRET)


def hostile_report(failure: BaseException) -> str:
    return "".join(traceback.format_exception(failure))


def test_a_hostile_changed_path_is_refused_before_it_is_ordered() -> None:
    """Sorting the changed files must never run an element's own comparison."""
    changes: JsonValue = {
        cast("str", HostileText(CHANGED_PATH)): {"type": "update", "unified_diff": PATCH_BODY},
        f"{CWD}/added.py": {"type": "add", "content": PATCH_BODY},
    }

    with pytest.raises(CodexSchemaError) as failure:
        adapt_legacy_file(changes)

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_hostile_member_name_inside_a_change_is_refused_before_it_is_matched() -> None:
    changes: JsonValue = {
        CHANGED_PATH: {
            cast("str", HostileText("type")): "update",
            "unified_diff": PATCH_BODY,
        }
    }

    with pytest.raises(CodexSchemaError) as failure:
        adapt_legacy_file(changes)

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_hostile_params_member_name_is_refused_before_it_is_matched() -> None:
    params = dict(command_params())
    params[cast("str", HostileText("reason"))] = REASON

    with pytest.raises(CodexSchemaError) as failure:
        adapt_command(params)

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_hostile_advertised_method_name_never_reaches_a_lookup() -> None:
    """A contract carrying one is refused where it is built, not where it is read."""
    hostile = cast("str", HostileText(COMMAND_METHOD))

    with pytest.raises(CodexSchemaError) as failure:
        CodexProtocolContract(
            version="test-version",
            methods={},
            server_requests={
                ServerRequestCategory.COMMAND_APPROVAL: frozenset({hostile}),
                ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset({FILE_METHOD}),
            },
            unclassified_server_request_count=0,
            experimental_schema=False,
            approval_profiles={hostile: command_profile(), FILE_METHOD: file_change_profile()},
        )

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_hostile_profiled_method_name_never_reaches_a_lookup() -> None:
    with pytest.raises(CodexSchemaError) as failure:
        approval_contract(
            profiles={
                cast("str", HostileText(COMMAND_METHOD)): command_profile(),
                FILE_METHOD: file_change_profile(),
            }
        )

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def hostile_member_contracts() -> dict[str, _ValueContract]:
    """The declared members of one build, with one name replaced by a hostile spelling."""
    contracts = {name: value for name, value in COMMAND_CONTRACTS.items() if name != "cwd"}
    contracts[cast("str", HostileText("cwd"))] = NULLABLE_STRING
    return contracts


@pytest.mark.parametrize(
    "overrides",
    [
        {"required_members": frozenset({cast("str", HostileText("threadId")), "turnId", "itemId"})},
        {"member_contracts": hostile_member_contracts()},
        {"argv_member": cast("str", HostileText("command"))},
        {"offer_member": cast("str", HostileText("availableDecisions"))},
    ],
)
def test_a_hostile_profile_member_name_never_reaches_a_lookup(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError) as failure:
        command_profile(**overrides)

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_hostile_value_contract_type_is_rejected_without_comparison() -> None:
    hostile_types = frozenset({cast("str", HostileText("string"))})

    with pytest.raises(CodexSchemaError) as failure:
        command_profile(
            member_contracts={
                **COMMAND_CONTRACTS,
                "threadId": _ValueContract(types=hostile_types),
            }
        )

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_hostile_nested_value_contract_key_is_rejected_before_lookup() -> None:
    hostile_properties = {
        cast("str", HostileText("type")): STRING,
    }
    hostile_action = _ValueContract(
        types=frozenset({"object"}),
        properties=hostile_properties,
    )

    with pytest.raises(CodexSchemaError) as failure:
        adapt_command(
            command_params(
                commandActions=[{"type": "unknown"}],
            ),
            contract=approval_contract(
                profiles={
                    COMMAND_METHOD: command_profile(
                        member_contracts={
                            **COMMAND_CONTRACTS,
                            "commandActions": array_of(hostile_action, nullable=True),
                        }
                    ),
                    FILE_METHOD: file_change_profile(),
                }
            ),
        )

    assert HOSTILE_SECRET not in hostile_report(failure.value)


def test_a_mappingproxy_wrapped_hostile_contract_is_rejected_privacy_safely() -> None:
    properties = MappingProxyType(HostileMappingProxyDict({"type": STRING}))
    nested = _ValueContract(types=frozenset({"object"}), properties=properties)

    with pytest.raises(CodexSchemaError) as failure:
        command_profile(
            member_contracts={
                **COMMAND_CONTRACTS,
                "commandActions": array_of(nested, nullable=True),
            }
        )

    assert str(failure.value) == "Codex approval profile is not coherent"
    assert failure.value.__cause__ is None
    assert MAPPING_PROXY_SECRET not in hostile_report(failure.value)


def test_a_valid_mappingproxy_contract_is_still_frozen_and_admitted() -> None:
    properties = MappingProxyType({"type": STRING})
    nested = _ValueContract(types=frozenset({"object"}), properties=properties)
    profile = command_profile(
        member_contracts={
            **COMMAND_CONTRACTS,
            "commandActions": array_of(nested, nullable=True),
        }
    )

    assert profile.admits_member("commandActions", [{"type": "unknown"}])


# The nested member a substituting source answers for, spelled as the newer command family
# spells the one member its own permission object declares.
SUBSTITUTED_MEMBER = "network"


class SubstitutingContractMapping(Mapping[str, _ValueContract]):
    """A mapping proxy source that answers a second lookup with a different child.

    A mapping proxy states nothing about the mapping behind it, so a caller-owned source may
    answer one lookup with the contract a check accepts and the next with another. A profile
    keeps what it checked, so freezing must read each such source once.
    """

    def __init__(self, checked: _ValueContract, substituted: _ValueContract) -> None:
        self._children = (checked, substituted)
        self.reads = 0

    def __len__(self) -> int:
        return 1

    def __iter__(self) -> Iterator[str]:
        yield SUBSTITUTED_MEMBER

    def __getitem__(self, key: str) -> _ValueContract:
        if key != SUBSTITUTED_MEMBER:
            raise KeyError(key)
        child = self._children[min(self.reads, len(self._children) - 1)]
        self.reads += 1
        return child


def test_a_substituting_property_mapping_is_read_once_while_freezing() -> None:
    """What a frozen contract keeps is the child that was checked, not a later answer."""
    source = SubstitutingContractMapping(STRING, INT64)
    nested = _ValueContract(types=frozenset({"object"}), properties=MappingProxyType(source))

    frozen = _freeze_value_contract(nested)

    kept = frozen.properties[SUBSTITUTED_MEMBER]
    assert source.reads == 1
    assert kept.types == frozenset({"string"})
    assert kept.admits("text")
    assert not kept.admits(1)


def test_a_substituted_child_never_reaches_a_profile_or_its_repr() -> None:
    """A second answer is never read, so no profile can retain or quote what it carries."""
    hostile = _ValueContract(types=frozenset({cast("str", HostileText(HOSTILE_SECRET))}))
    source = SubstitutingContractMapping(STRING, hostile)
    permissions = _ValueContract(
        types=frozenset({"object", "null"}),
        properties=MappingProxyType(source),
    )

    profile = command_profile(
        member_contracts={**COMMAND_CONTRACTS, "additionalPermissions": permissions}
    )

    assert HOSTILE_SECRET not in repr(profile)
    assert source.reads == 1
    kept = profile.member_contracts["additionalPermissions"].properties[SUBSTITUTED_MEMBER]
    assert kept.types == frozenset({"string"})


def test_a_hostile_nested_child_type_is_refused_where_a_profile_freezes_it() -> None:
    """The one answer freezing reads is still checked whole, at every depth it reaches."""
    hostile = _ValueContract(types=frozenset({cast("str", HostileText(HOSTILE_SECRET))}))
    permissions = _ValueContract(
        types=frozenset({"object", "null"}),
        properties=MappingProxyType({SUBSTITUTED_MEMBER: hostile}),
    )

    with pytest.raises(CodexSchemaError) as failure:
        command_profile(
            member_contracts={**COMMAND_CONTRACTS, "additionalPermissions": permissions}
        )

    assert str(failure.value) == "Codex approval profile is not coherent"
    assert failure.value.__cause__ is None
    assert HOSTILE_SECRET not in hostile_report(failure.value)


@pytest.mark.parametrize(
    "contract",
    [
        _ValueContract(
            types=frozenset({"object"}),
            properties={SUBSTITUTED_MEMBER: cast("_ValueContract", {"types": "string"})},
        ),
        _ValueContract(items=cast("_ValueContract", {"types": "string"})),
        _ValueContract(additional=cast("_ValueContract", {"types": "string"})),
        _ValueContract(one_of=cast("tuple[_ValueContract, ...]", [STRING])),
    ],
)
def test_a_container_holding_something_other_than_a_contract_is_refused(
    contract: _ValueContract,
) -> None:
    """Every child a frozen contract keeps is one this representation states, or none is."""
    with pytest.raises(CodexSchemaError) as failure:
        _freeze_value_contract(contract)

    assert str(failure.value) == "Codex approval profile is not coherent"
    assert failure.value.__cause__ is None


def test_a_repeated_child_contract_dag_is_rejected_within_the_work_budget() -> None:
    repeated = STRING
    for _ in range(6):
        repeated = _ValueContract(one_of=(repeated,) * 8)

    with pytest.raises(CodexSchemaError) as failure:
        _freeze_value_contract(repeated)

    assert str(failure.value) == "Codex approval profile is not coherent"
    assert failure.value.__cause__ is None


def test_a_nested_value_object_key_must_be_transport_safe() -> None:
    params = command_params(
        commandActions=[
            {
                "command": COMMAND,
                "type": "unknown",
                LONE_SURROGATE: REASON,
            }
        ]
    )

    with pytest.raises(CodexSchemaError) as failure:
        adapt_command(params)

    assert LONE_SURROGATE not in hostile_report(failure.value)


class HostileFrozenset(frozenset[str]):
    """A frozenset subclass that refuses the iteration a copy would need."""

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(COMMAND)


class HostileMemberDict(dict[str, _ValueContract]):
    """A member mapping that rewrites itself while it is read."""

    def items(self) -> Iterator[tuple[str, _ValueContract]]:  # type: ignore[override]
        raise RuntimeError(COMMAND)


MAPPING_PROXY_SECRET = "MAPPING_PROXY_HOSTILE_SECRET"  # noqa: S105


class HostileMappingProxyDict(dict[str, _ValueContract]):
    """A mapping whose read failure must stay contained by a mapping proxy boundary."""

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(MAPPING_PROXY_SECRET)

    def items(self) -> Iterator[tuple[str, _ValueContract]]:  # type: ignore[override]
        raise RuntimeError(MAPPING_PROXY_SECRET)


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": ServerRequestCategory.PERMISSION_APPROVAL},
        {"category": cast("ServerRequestCategory", "command_approval")},
        {"correlation": cast("ApprovalCorrelation", "thread_item")},
        {"correlation": None},
        {"required_members": CORRELATION | {"undeclaredFutureMember"}},
        {"absent_or_null_members": frozenset({"undeclaredFutureMember"})},
        {"offer_member": "undeclaredFutureMember"},
        {"offer_member": ""},
        {"argv_member": "undeclaredFutureMember"},
        {"changes_member": "undeclaredFutureMember"},
        {"member_contracts": {}},
        {"member_contracts": {"threadId": "CONTRACT_VALUE_SECRET"}},
        {"member_contracts": {7: STRING}},
        {"member_contracts": {LONE_SURROGATE: STRING}},
        {"member_contracts": HostileMemberDict(COMMAND_CONTRACTS)},
        {"member_contracts": "MEMBERS_VALUE_SECRET"},
        {"required_members": HostileFrozenset(CORRELATION)},
        {"required_members": set(CORRELATION)},
        {"decisions": {ApprovalDecision.ACCEPT: "accept"}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: "accept"}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: ""}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: LONE_SURROGATE}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: {"denied": LONE_SURROGATE}}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: 1.5}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: {7: "cancel"}}},
        {"decisions": {"accept": "accept", "decline": "decline", "cancel": "cancel"}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: ["cancel"]}},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.CANCEL: {}}},
        {"decisions": "DECISIONS_VALUE_SECRET"},
        {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.ACCEPT: 7}},
        {"decision_contract": None},
        {"decision_contract": "CONTRACT_VALUE_SECRET"},
        {"required_members": CORRELATION | {"startedAtMs", "environmentId"}},
    ],
)
def test_an_incoherent_profile_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(CodexSchemaError) as failure:
        command_profile(**overrides)

    report = "".join(traceback.format_exception(failure.value))
    assert "SECRET" not in report
    assert COMMAND not in report


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        # A profile that keeps one build's decision type while answering with another
        # build's vocabulary would send a value its own schema refuses.
        (command_profile, {"decisions": dict(LEGACY_STRING_WIRE)}),
        (command_profile, {"decisions": dict(LEGACY_OBJECT_WIRE)}),
        (legacy_command_profile, {"decisions": dict(ONE_SHOT_WIRE)}),
        (legacy_file_profile, {"decisions": dict(ONE_SHOT_WIRE)}),
        # A decision this family's own vocabulary spells, but which outlives the request
        # under review, is never the value a reviewer decision is answered with.
        (
            command_profile,
            {"decisions": {**ONE_SHOT_WIRE, ApprovalDecision.ACCEPT: "acceptForSession"}},
        ),
    ],
)
def test_a_profile_cannot_answer_with_a_vocabulary_its_own_decisions_refuse(
    build: object,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError) as failure:
        cast("Callable[..., ApprovalProfile]", build)(**overrides)

    report = "".join(traceback.format_exception(failure.value))
    assert COMMAND not in report


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        (command_profile, {"member_contracts": {**COMMAND_CONTRACTS, "threadId": INT64}}),
        (command_profile, {"member_contracts": {**COMMAND_CONTRACTS, "turnId": NULLABLE_STRING}}),
        (
            legacy_file_profile,
            {"member_contracts": {**LEGACY_FILE_CONTRACTS, "conversationId": INT64}},
        ),
    ],
)
def test_a_profile_correlation_member_must_hold_the_identifier_it_states(
    build: object,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError):
        cast("Callable[..., ApprovalProfile]", build)(**overrides)


def test_a_profile_correlation_member_cannot_be_a_fixed_string() -> None:
    with pytest.raises(CodexSchemaError):
        command_profile(member_contracts={**COMMAND_CONTRACTS, "threadId": literal(THREAD_ID)})


@pytest.mark.parametrize(
    ("build", "member", "contract"),
    [
        (legacy_command_profile, "command", array_of(INT64)),
        (command_profile, "availableDecisions", array_of(STRING, nullable=True)),
    ],
)
def test_a_profile_selector_contract_must_match_its_semantic_value(
    build: object,
    member: str,
    contract: _ValueContract,
) -> None:
    base = LEGACY_COMMAND_CONTRACTS if build is legacy_command_profile else COMMAND_CONTRACTS

    with pytest.raises(CodexSchemaError):
        cast("Callable[..., ApprovalProfile]", build)(member_contracts={**base, member: contract})


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        # The reviewed command must be the command, never the offered decisions beside it.
        (command_profile, {"argv_member": "availableDecisions"}),
        (command_profile, {"argv_member": "command"}),
        (command_profile, {"changes_member": "cwd"}),
        (legacy_command_profile, {"argv_member": "cwd"}),
        (legacy_command_profile, {"argv_member": None}),
        (legacy_command_profile, {"changes_member": "parsedCmd"}),
        (legacy_file_profile, {"changes_member": "reason"}),
        (legacy_file_profile, {"changes_member": None}),
        (file_change_profile, {"argv_member": "reason"}),
    ],
)
def test_a_profile_selector_cannot_alias_another_member(
    build: object,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError):
        cast("Callable[..., ApprovalProfile]", build)(**overrides)


def test_a_profile_cannot_suppress_a_declared_decision_offer() -> None:
    """Dropping the offer selector would present ACCEPT for a request that only declines."""
    with pytest.raises(CodexSchemaError):
        command_profile(offer_member=None)


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        (command_profile, {"correlation": ApprovalCorrelation.CONVERSATION_CALL}),
        (legacy_command_profile, {"correlation": ApprovalCorrelation.THREAD_ITEM}),
        (file_change_profile, {"correlation": ApprovalCorrelation.CONVERSATION_CALL}),
        (legacy_file_profile, {"correlation": ApprovalCorrelation.THREAD_ITEM}),
        (command_profile, {"category": ServerRequestCategory.FILE_CHANGE_APPROVAL}),
        (legacy_file_profile, {"category": ServerRequestCategory.COMMAND_APPROVAL}),
    ],
)
def test_a_profile_family_combination_that_no_build_states_is_rejected(
    build: object,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CodexSchemaError):
        cast("Callable[..., ApprovalProfile]", build)(**overrides)


def test_every_supported_approval_family_builds_a_coherent_profile() -> None:
    """The four semantic families a retained build states each construct, and stay apart."""
    profiles = [
        command_profile(),
        file_change_profile(),
        legacy_command_profile(),
        legacy_command_profile(denied_object=True),
        legacy_file_profile(),
        legacy_file_profile(denied_object=True),
        WINDOWS_COMMAND_PROFILE,
        WINDOWS_FILE_PROFILE,
    ]

    assert {(profile.category, profile.correlation) for profile in profiles} == {
        (ServerRequestCategory.COMMAND_APPROVAL, ApprovalCorrelation.THREAD_ITEM),
        (ServerRequestCategory.COMMAND_APPROVAL, ApprovalCorrelation.CONVERSATION_CALL),
        (ServerRequestCategory.FILE_CHANGE_APPROVAL, ApprovalCorrelation.THREAD_ITEM),
        (ServerRequestCategory.FILE_CHANGE_APPROVAL, ApprovalCorrelation.CONVERSATION_CALL),
    }
    for profile in profiles:
        for decision in ApprovalDecision:
            assert profile.admits_decision(profile.wire_decision(decision))


def test_a_profile_is_deeply_immutable() -> None:
    contracts = dict(COMMAND_CONTRACTS)
    decisions = dict(ONE_SHOT_WIRE)
    profile = command_profile(member_contracts=contracts, decisions=decisions)
    contracts["threadId"] = INT64
    decisions[ApprovalDecision.ACCEPT] = "acceptForSession"

    assert profile.admits_member("threadId", THREAD_ID)
    assert not profile.admits_member("threadId", 7)
    assert profile.wire_decision(ApprovalDecision.ACCEPT) == "accept"
    with pytest.raises(TypeError):
        cast("dict[str, _ValueContract]", profile.member_contracts)["other"] = STRING


def test_a_profile_does_not_alias_nested_value_contract_properties() -> None:
    nested_properties = {"type": STRING}
    contracts = dict(COMMAND_CONTRACTS)
    contracts["commandActions"] = array_of(obj(nested_properties), nullable=True)
    profile = command_profile(member_contracts=contracts)

    nested_properties["type"] = INT64

    assert profile.admits_member("commandActions", [{"type": "unknown"}])


class _ResponseWriter:
    """The transport side of one peer, keeping what it was asked to send."""

    def __init__(self) -> None:
        self.written: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            self.written.put_nowait(cast("dict[str, JsonValue]", json.loads(line)))

    async def drain(self) -> None:
        await asyncio.sleep(0)


async def test_a_decision_reaches_codex_through_the_server_request_handler() -> None:
    """The adapter's response must survive the peer's own JSON validation and its UTF-8
    line write, or no decision is delivered and the approval fails with an internal error.
    """
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    contract = legacy_contract(denied_object=True)

    async def handle(request: RpcServerRequest) -> JsonValue:
        assert request.method == LEGACY_COMMAND_METHOD
        review = adapt_approval_request(
            contract,
            request.method,
            request.params,
            request_id=request.request_id,
        )
        return review.response_for(ApprovalDecision.DECLINE)

    peer.register_server_request_handler(LEGACY_COMMAND_METHOD, handle)
    await peer.start()
    reader.feed_data(
        json.dumps(
            {"id": 41, "method": LEGACY_COMMAND_METHOD, "params": legacy_command_params()}
        ).encode()
        + b"\n"
    )

    try:
        written = await asyncio.wait_for(writer.written.get(), 1.0)
    finally:
        await peer.close()

    assert written == {"id": 41, "result": {"decision": {"denied": {"rejection": ""}}}}


@pytest.mark.parametrize("method", [7, None, b"alias/commandApproval"])
def test_a_method_name_that_is_not_text_fails_closed(method: object) -> None:
    with pytest.raises(CodexSchemaError):
        adapt_approval_request(
            approval_contract(),
            cast("str", method),
            command_params(),
            request_id=REQUEST_ID,
        )


@pytest.mark.parametrize("profile", [file_change_profile(), None, "PROFILE_VALUE_SECRET"])
def test_a_command_review_built_on_another_profile_is_rejected(profile: object) -> None:
    with pytest.raises(CodexSchemaError):
        CommandApprovalReview(**command_review_members(profile=profile))  # type: ignore[arg-type]


def test_a_file_change_review_built_on_another_profile_is_rejected() -> None:
    members = file_review_members(profile=command_profile())

    with pytest.raises(CodexSchemaError):
        FileChangeApprovalReview(**members)  # type: ignore[arg-type]


@pytest.mark.parametrize("decision", ["accept", None, 0])
def test_a_response_for_something_that_is_not_a_decision_fails_closed(decision: object) -> None:
    review = adapt_command(command_params())

    with pytest.raises(CodexSchemaError):
        review.response_for(cast("ApprovalDecision", decision))


# ---------------------------------------------------------------------------------------
# The broker that publishes one review to the trusted local reviewer and answers each
# app-server request exactly once, through the adapter above and never around it.
# ---------------------------------------------------------------------------------------

COMMAND_REQUEST_ID = 41
# The two families are answered under the two request id types the transport carries.
FILE_REQUEST_ID = "request-9c4f1a2b"
# A failure guard for a read the broker has already satisfied, never a deadline moco adds.
STREAM_TIMEOUT = 1.0


class Handles:
    """A deterministic handle source reached only through the broker's private test seam."""

    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        index = min(self.calls, len(self.values)) - 1
        return cast("str", self.values[index])


class Registrar:
    """The registration surface a peer or a connection supervisor already exposes."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.terminal: list[Callable[[], None]] = []
        self.notification: list[Callable[[RpcNotification], None]] = []
        self.order: list[str] = []

    def register_server_request_handler(
        self,
        method: str,
        handler: Callable[[RpcServerRequest], object],
    ) -> None:
        del handler
        self.registered.append(method)
        self.order.append(f"request:{method}")

    def register_notification_observer(
        self,
        observer: Callable[[RpcNotification], None],
    ) -> None:
        self.notification.append(observer)
        self.order.append("notification_observer")

    def register_terminal_callback(self, callback: Callable[[], None]) -> None:
        self.terminal.append(callback)
        self.order.append("terminal_callback")


def broker(
    contract: CodexProtocolContract | None = None,
    *,
    handles: Callable[[], str] | None = None,
) -> InteractionBroker:
    interaction = InteractionBroker(
        contract or approval_contract(),
        _handles=handles,
    )
    interaction.bind_active_turn_check(lambda _thread_id, _turn_id: True)
    return interaction


def command_request(
    request_id: int | str = COMMAND_REQUEST_ID,
    **overrides: JsonValue | object,
) -> RpcServerRequest:
    return RpcServerRequest(request_id, COMMAND_METHOD, command_params(**overrides))


def file_request(request_id: int | str = FILE_REQUEST_ID) -> RpcServerRequest:
    return RpcServerRequest(request_id, FILE_METHOD, file_change_params())


def legacy_file_request(request_id: int | str = REQUEST_ID) -> RpcServerRequest:
    return RpcServerRequest(request_id, LEGACY_FILE_METHOD, legacy_file_params())


async def published(
    interaction: InteractionBroker,
    connection: ReviewerConnection,
    request: RpcServerRequest | None = None,
) -> tuple[asyncio.Task[JsonValue], ReviewEnvelope]:
    """Start one awaiting handler and read the review its reviewer was shown."""
    task = asyncio.create_task(interaction.review(request or command_request()))
    envelope = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    assert isinstance(envelope, ReviewEnvelope)
    return task, envelope


def pending_reviews(interaction: InteractionBroker) -> int:
    """Read how many reviews the broker still owns, from its own payload-neutral repr."""
    match = re.search(r"pending=(\d+)", repr(interaction))
    assert match is not None
    return int(match.group(1))


async def settle() -> None:
    """Run every task that is already runnable, without waiting on the clock."""
    for _ in range(10):
        await asyncio.sleep(0)


def bind_pending_counts(interaction: InteractionBroker) -> list[int]:
    """Bind the one synchronous count sink and return its observed values."""
    counts: list[int] = []
    interaction.bind_pending_count_changed(counts.append)
    return counts


async def test_pending_count_changes_only_after_each_successful_publication_and_decision() -> None:
    interaction = broker(legacy_contract())
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()

    first, opened = await published(interaction, connection, command_request())
    second, other = await published(interaction, connection, legacy_file_request())

    assert counts == [1, 2]
    interaction.decide(connection, other.handle, ApprovalDecision.DECLINE)
    assert counts == [1, 2, 1]
    interaction.decide(connection, opened.handle, ApprovalDecision.ACCEPT)
    assert counts == [1, 2, 1, 0]
    assert await second == {"decision": "denied"}
    assert await first == {"decision": "accept"}
    interaction.close()


async def test_adaptation_failure_never_changes_pending_count() -> None:
    interaction = broker()
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexSchemaError):
        await interaction.review(command_request(unknownMember="future"))

    assert counts == []
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_modern_file_without_patch_evidence_fails_before_publication() -> None:
    contract = replace(file_change_patch_contract(), file_change_patch_profile=None)
    interaction = broker(contract)
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexSchemaError):
        await interaction.review(file_request())

    assert counts == []
    assert pending_reviews(interaction) == 0
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_full_reviewer_stream_never_changes_pending_count_for_failed_publication() -> None:
    interaction = broker()
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()

    tasks: list[asyncio.Task[JsonValue]] = []
    for index in range(_MAX_UNREAD_REVIEWS):
        task, _ = await published(interaction, connection, command_request(index))
        tasks.append(task)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pending_reviews(interaction) == 0
    counts.clear()

    with pytest.raises(CodexReviewError):
        await interaction.review(command_request("overflow"))

    assert counts == []
    interaction.close()


@pytest.mark.parametrize("ending", ["disconnect", "connection_lost", "close"])
async def test_pending_count_returns_to_zero_for_broker_endings(ending: str) -> None:
    interaction = broker()
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)

    if ending == "disconnect":
        interaction.disconnect_reviewer(connection)
    elif ending == "connection_lost":
        interaction.connection_lost()
    else:
        interaction.close()

    with pytest.raises(CodexReviewError):
        await task
    assert counts == [1, 0]
    interaction.close()


async def test_pending_count_returns_to_zero_when_handler_is_cancelled() -> None:
    interaction = broker()
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert counts == [1, 0]
    interaction.close()


def test_pending_count_callback_is_one_shot_and_bound_before_reviewer_use() -> None:
    interaction = broker()
    interaction.bind_pending_count_changed(lambda _count: None)

    with pytest.raises(CodexReviewError):
        interaction.bind_pending_count_changed(lambda _count: None)

    other = broker()
    other.connect_reviewer()
    with pytest.raises(CodexReviewError):
        other.bind_pending_count_changed(lambda _count: None)
    interaction.close()
    other.close()


def test_pending_count_callback_must_be_synchronous() -> None:
    interaction = broker()

    async def asynchronous_callback(_count: int) -> None:
        raise AssertionError

    with pytest.raises(CodexReviewError):
        interaction.bind_pending_count_changed(cast("Callable[[int], None]", asynchronous_callback))
    interaction.close()


async def test_pending_count_callback_failure_terminalizes_without_leaking_payload() -> None:
    callback_secret = "CALLBACK_PRIVATE_DETAIL"  # noqa: S105
    calls: list[int] = []
    interaction = broker()

    def fail(count: int) -> None:
        calls.append(count)
        raise RuntimeError(callback_secret)

    interaction.bind_pending_count_changed(fail)
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexReviewError) as waiting:
        await interaction.review(command_request(reason=REASON))

    assert calls == [1]
    assert pending_reviews(interaction) == 0
    assert callback_secret not in hostile_report(waiting.value)
    assert not any(secret in hostile_report(waiting.value) for secret in SECRETS)
    with pytest.raises(CodexReviewError):
        await interaction.review(command_request())
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)


async def test_cancel_pending_withdraws_read_review_once_and_broker_is_reusable() -> None:
    interaction = broker()
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    interaction.cancel_pending()
    interaction.cancel_pending()

    with pytest.raises(CodexReviewError, match="local review was cancelled"):
        await task
    withdrawal = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    assert withdrawal == ReviewWithdrawal(handle=envelope.handle)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert counts == [1, 0]

    next_task, next_envelope = await published(
        interaction,
        connection,
        command_request("request-after-cancel"),
    )
    interaction.decide(connection, next_envelope.handle, ApprovalDecision.ACCEPT)
    assert await next_task == {"decision": "accept"}
    assert counts == [1, 0, 1, 0]
    interaction.close()


async def test_cancel_pending_drops_unread_review_without_a_withdrawal() -> None:
    interaction = broker()
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()
    task = asyncio.create_task(interaction.review(command_request()))
    await settle()

    interaction.cancel_pending()

    with pytest.raises(CodexReviewError, match="local review was cancelled"):
        await task
    assert counts == [1, 0]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_cancel_pending_fails_all_current_reviews_with_one_zero_transition() -> None:
    interaction = broker(legacy_contract())
    counts = bind_pending_counts(interaction)
    connection = interaction.connect_reviewer()
    first, opened = await published(interaction, connection, command_request())
    second, other = await published(interaction, connection, legacy_file_request())

    interaction.cancel_pending()

    for task in (first, second):
        with pytest.raises(CodexReviewError, match="local review was cancelled"):
            await task
    withdrawals = {await anext(connection), await anext(connection)}
    assert withdrawals == {
        ReviewWithdrawal(handle=opened.handle),
        ReviewWithdrawal(handle=other.handle),
    }
    assert counts == [1, 2, 0]
    interaction.close()


async def test_a_review_reaches_the_trusted_reviewer_under_a_fresh_opaque_handle() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()

    task, envelope = await published(interaction, connection)

    assert isinstance(envelope.review, CommandApprovalReview)
    assert envelope.review.command == COMMAND
    assert envelope.review.correlation.request_id == COMMAND_REQUEST_ID
    assert pending_reviews(interaction) == 1
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    assert pending_reviews(interaction) == 0
    interaction.close()


async def test_each_pending_review_holds_its_own_handle() -> None:
    interaction = broker(legacy_contract())
    connection = interaction.connect_reviewer()

    first, opened = await published(interaction, connection, command_request())
    second, other = await published(interaction, connection, legacy_file_request())

    assert opened.handle != other.handle
    assert pending_reviews(interaction) == 2
    interaction.decide(connection, other.handle, ApprovalDecision.DECLINE)
    assert await asyncio.wait_for(second, STREAM_TIMEOUT) == {"decision": "denied"}
    assert not first.done()
    interaction.close()
    with pytest.raises(CodexReviewError):
        await first


async def test_a_payload_this_build_cannot_explain_never_reaches_a_reviewer() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexSchemaError):
        await interaction.review(command_request(unknownMember="future"))

    assert pending_reviews(interaction) == 0
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_a_decision_answers_with_the_response_this_build_proves() -> None:
    interaction = broker(legacy_contract(denied_object=True))
    connection = interaction.connect_reviewer()

    task, envelope = await published(interaction, connection, legacy_file_request())
    interaction.decide(connection, envelope.handle, ApprovalDecision.DECLINE)

    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {
        "decision": {"denied": {"rejection": ""}}
    }
    interaction.close()


async def test_a_decision_the_request_never_offered_leaves_the_review_pending() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()

    task, envelope = await published(
        interaction,
        connection,
        command_request(availableDecisions=["accept", "decline"]),
    )

    with pytest.raises(CodexSchemaError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.CANCEL)
    assert not task.done()
    interaction.decide(connection, envelope.handle, ApprovalDecision.DECLINE)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "decline"}
    interaction.close()


@pytest.mark.parametrize("decision", ["accept", None, 0, ApprovalDecision])
async def test_a_decision_that_is_not_a_typed_decision_fails_closed(decision: object) -> None:
    """A `StrEnum` member equals its own text, so only the exact member may be answered."""
    interaction = broker()
    connection = interaction.connect_reviewer()

    task, envelope = await published(interaction, connection)

    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, cast("ApprovalDecision", decision))
    assert not task.done()
    assert pending_reviews(interaction) == 1
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


async def test_the_app_server_request_id_is_not_the_reviewer_response_key() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()

    task, envelope = await published(interaction, connection)

    assert envelope.handle != str(COMMAND_REQUEST_ID)
    for impostor in (str(COMMAND_REQUEST_ID), COMMAND_METHOD):
        with pytest.raises(CodexReviewError):
            interaction.decide(connection, impostor, ApprovalDecision.ACCEPT)
    assert not task.done()
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


async def test_a_second_decision_on_the_same_handle_fails_closed() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    async def click() -> BaseException | None:
        try:
            interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
        except CodexReviewError as failure:
            return failure
        return None

    outcomes = await asyncio.gather(click(), click())

    assert [outcome is None for outcome in outcomes].count(True) == 1
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    assert pending_reviews(interaction) == 0
    interaction.close()


async def test_a_late_decision_after_the_handler_finished_fails_closed() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)
    interaction.decide(connection, envelope.handle, ApprovalDecision.DECLINE)
    await asyncio.wait_for(task, STREAM_TIMEOUT)

    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)

    assert task.result() == {"decision": "decline"}
    interaction.close()


async def test_a_handle_from_another_reviewer_connection_fails_closed() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    stranger = broker().connect_reviewer()
    task, envelope = await published(interaction, connection)

    with pytest.raises(CodexReviewError):
        interaction.decide(stranger, envelope.handle, ApprovalDecision.ACCEPT)

    assert not task.done()
    assert pending_reviews(interaction) == 1
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


@pytest.mark.parametrize(
    "handle",
    ["", "   ", "review-unknown", 7, None, b"handle"],
)
async def test_an_unknown_handle_fails_closed(handle: object) -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)

    with pytest.raises(CodexReviewError):
        interaction.decide(connection, cast("str", handle), ApprovalDecision.ACCEPT)

    assert not task.done()
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


async def test_a_hostile_handle_never_reaches_a_lookup() -> None:
    """A string subclass hashes like the handle it spells and attacks the comparison."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    with pytest.raises(CodexReviewError) as failure:
        interaction.decide(
            connection,
            cast("str", HostileText(envelope.handle)),
            ApprovalDecision.ACCEPT,
        )

    assert HOSTILE_SECRET not in hostile_report(failure.value)
    assert not task.done()
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


async def test_handler_cancellation_invalidates_the_handle_and_stays_cancelled() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert pending_reviews(interaction) == 0
    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    interaction.close()


async def test_cancelling_a_delivered_review_withdraws_it_from_the_reviewer() -> None:
    """The reviewer was shown this review, so the screen holding it must be told to close."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection, command_request(reason=REASON))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    withdrawal = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    assert isinstance(withdrawal, ReviewWithdrawal)
    assert withdrawal.handle == envelope.handle
    rendered = repr(withdrawal)
    assert envelope.handle not in rendered
    assert not any(secret in rendered for secret in SECRETS)
    with pytest.raises(FrozenInstanceError):
        withdrawal.handle = "review-forged"  # type: ignore[misc]
    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert pending_reviews(interaction) == 0
    interaction.close()


async def test_a_decided_review_is_not_withdrawn_from_the_reviewer_that_decided_it() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)

    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_a_withdrawal_never_reaches_a_reviewer_that_never_saw_the_review() -> None:
    """Only the reviewer holding a review is told to close it, whoever holds the slot later."""
    interaction = broker()
    first = interaction.connect_reviewer()
    task, envelope = await published(interaction, first, command_request(reason=REASON))

    interaction.disconnect_reviewer(first)
    second = interaction.connect_reviewer()

    with pytest.raises(CodexReviewError):
        await asyncio.wait_for(task, STREAM_TIMEOUT)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(second), 0.05)
    with pytest.raises(CodexReviewError):
        interaction.decide(second, envelope.handle, ApprovalDecision.ACCEPT)
    assert pending_reviews(interaction) == 0
    interaction.close()


async def test_an_unread_cancelled_review_is_never_shown_to_the_reviewer() -> None:
    """A review that ended before anyone read it is destroyed, not left waiting to be read."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    stale = asyncio.create_task(interaction.review(command_request("request-stale")))
    await settle()

    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale

    assert pending_reviews(interaction) == 0
    task, envelope = await published(interaction, connection, command_request("request-fresh"))
    assert isinstance(envelope.review, CommandApprovalReview)
    assert envelope.review.correlation.request_id == "request-fresh"
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()


async def test_a_decision_racing_handler_cancellation_fails_closed() -> None:
    """The handler is cancelled but has not resumed yet, so the review is already over."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    task.cancel()

    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending_reviews(interaction) == 0
    interaction.close()


async def test_reviewer_disconnect_fails_every_bound_review_closed() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    first, opened = await published(interaction, connection, command_request())
    second, _ = await published(interaction, connection, command_request("request-second"))

    interaction.disconnect_reviewer(connection)

    for task in (first, second):
        with pytest.raises(CodexReviewError):
            await asyncio.wait_for(task, STREAM_TIMEOUT)
    assert pending_reviews(interaction) == 0
    with pytest.raises(CodexReviewError):
        interaction.decide(connection, opened.handle, ApprovalDecision.ACCEPT)
    interaction.close()


async def test_reviewer_disconnect_ends_the_review_stream() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)

    interaction.disconnect_reviewer(connection)
    interaction.disconnect_reviewer(connection)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    with pytest.raises(CodexReviewError):
        await task
    interaction.close()


async def test_a_reviewer_reads_its_published_reviews_as_a_stream() -> None:
    """The stream is what the later reviewer transport iterates, and it ends by itself."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)
    interaction.disconnect_reviewer(connection)
    with pytest.raises(CodexReviewError):
        await task

    seen = [envelope async for envelope in connection]

    assert seen == []
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    interaction.close()


@pytest.mark.parametrize("stranger", [None, "connection", 7])
async def test_disconnecting_something_that_is_not_a_reviewer_fails_closed(
    stranger: object,
) -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)

    with pytest.raises(CodexReviewError):
        interaction.disconnect_reviewer(cast("ReviewerConnection", stranger))

    assert not task.done()
    assert pending_reviews(interaction) == 1
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


async def test_disconnecting_another_reviewer_leaves_this_one_reviewing() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    stranger = broker().connect_reviewer()
    task, envelope = await published(interaction, connection)

    interaction.disconnect_reviewer(stranger)

    assert pending_reviews(interaction) == 1
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()


async def test_an_unread_review_is_dropped_when_its_reviewer_disconnects() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task = asyncio.create_task(interaction.review(command_request()))
    await settle()

    interaction.disconnect_reviewer(connection)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    with pytest.raises(CodexReviewError):
        await task
    interaction.close()


async def test_a_reviewer_may_take_the_slot_again_after_a_disconnect() -> None:
    interaction = broker()
    first = interaction.connect_reviewer()
    task, envelope = await published(interaction, first)
    interaction.disconnect_reviewer(first)
    with pytest.raises(CodexReviewError):
        await task

    second = interaction.connect_reviewer()

    with pytest.raises(CodexReviewError):
        interaction.decide(second, envelope.handle, ApprovalDecision.ACCEPT)
    reopened, republished = await published(interaction, second)
    interaction.decide(second, republished.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(reopened, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()


async def test_only_one_reviewer_holds_the_slot() -> None:
    interaction = broker()
    interaction.connect_reviewer()

    with pytest.raises(CodexReviewError):
        interaction.connect_reviewer()

    interaction.close()


async def test_a_review_without_a_trusted_reviewer_fails_closed() -> None:
    interaction = broker()

    with pytest.raises(CodexReviewError):
        await interaction.review(command_request())

    assert pending_reviews(interaction) == 0
    interaction.close()


async def test_connection_loss_terminalizes_every_pending_review() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    interaction.connection_lost()
    interaction.connection_lost()

    with pytest.raises(CodexReviewError):
        await asyncio.wait_for(task, STREAM_TIMEOUT)
    assert pending_reviews(interaction) == 0
    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    with pytest.raises(CodexReviewError):
        await interaction.review(command_request())
    interaction.close()


async def test_a_pending_review_has_no_implicit_timeout() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    await settle()
    await settle()

    assert not task.done()
    assert pending_reviews(interaction) == 1
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()


async def test_handles_are_unpredictable_and_never_encode_the_request() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    tasks: list[asyncio.Task[JsonValue]] = []
    issued: dict[str, str] = {}

    for index in range(16):
        request_id = f"request-unpredictable-{index}"
        task, envelope = await published(interaction, connection, command_request(request_id))
        tasks.append(task)
        issued[envelope.handle] = request_id

    assert len(issued) == 16
    for handle, request_id in issued.items():
        assert 16 <= len(handle) <= 128
        assert re.fullmatch(r"[A-Za-z0-9_-]+", handle)
        assert not any(secret in handle for secret in SECRETS)
        assert not any(part in handle for part in (COMMAND_METHOD, request_id, "accept"))
    interaction.close()
    for task in tasks:
        with pytest.raises(CodexReviewError):
            await task


async def test_a_repeated_handle_is_retried_until_a_fresh_one_is_issued() -> None:
    """One collision costs one retry, and the review that follows it still reaches a reviewer."""
    handles = Handles("review-repeated", "review-repeated", "review-fresh")
    interaction = broker(handles=handles)
    connection = interaction.connect_reviewer()
    first, opened = await published(interaction, connection)

    second, other = await published(interaction, connection, command_request("request-second"))

    assert opened.handle == "review-repeated"
    assert other.handle == "review-fresh"
    assert handles.calls == 3
    assert pending_reviews(interaction) == 2
    interaction.decide(connection, other.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(second, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()
    with pytest.raises(CodexReviewError):
        await first


async def test_a_handle_source_that_always_repeats_stops_at_the_bound() -> None:
    """Retrying is bounded by a fixed count, so a source stuck on one value cannot loop."""
    handles = Handles("review-repeated")
    interaction = broker(handles=handles)
    connection = interaction.connect_reviewer()
    task, _ = await published(interaction, connection)

    with pytest.raises(CodexReviewError):
        await interaction.review(command_request("request-second"))

    assert handles.calls == 1 + _MAX_HANDLE_ATTEMPTS
    assert pending_reviews(interaction) == 1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


@pytest.mark.parametrize(
    "value",
    ["", "   ", 7, None, LONE_SURROGATE, "review-" + "x" * 200],
)
async def test_a_handle_the_transport_could_not_carry_fails_closed(value: object) -> None:
    """A handle no transport could carry is refused at once, never retried into a fresh one."""
    handles = Handles(value)
    interaction = broker(handles=handles)
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexReviewError):
        await interaction.review(command_request())

    assert handles.calls == 1
    assert pending_reviews(interaction) == 0
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_closing_is_idempotent_and_unblocks_every_waiter() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    interaction.close()
    interaction.close()

    with pytest.raises(CodexReviewError):
        await asyncio.wait_for(task, STREAM_TIMEOUT)
    assert pending_reviews(interaction) == 0
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    with pytest.raises(CodexReviewError):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)


async def test_new_work_after_close_is_rejected() -> None:
    interaction = broker()
    interaction.close()

    with pytest.raises(CodexReviewError):
        interaction.connect_reviewer()
    with pytest.raises(CodexReviewError):
        await interaction.review(command_request())
    with pytest.raises(CodexReviewError):
        interaction.register_approval_handlers(Registrar())


async def test_a_file_change_request_without_an_explanation_never_reaches_a_reviewer() -> None:
    """This slice has no observer of changed files yet, so that family fails closed."""
    interaction = broker()
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexSchemaError):
        await interaction.review(file_request())

    assert pending_reviews(interaction) == 0
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), 0.05)
    interaction.close()


async def test_a_self_explaining_request_is_reviewed_without_an_explanation() -> None:
    interaction = broker(legacy_contract())
    connection = interaction.connect_reviewer()

    _, envelope = await published(interaction, connection, legacy_file_request())

    assert isinstance(envelope.review, FileChangeApprovalReview)
    interaction.close()


async def test_the_published_envelope_is_immutable() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection)

    with pytest.raises(FrozenInstanceError):
        envelope.handle = "review-forged"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        envelope.review = cast("CommandApprovalReview", None)  # type: ignore[misc]
    review = envelope.review
    assert isinstance(review, CommandApprovalReview)
    with pytest.raises(FrozenInstanceError):
        review.command = ()  # type: ignore[misc]

    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


@pytest.mark.parametrize("review", [None, CHANGED_PATH, explanation()])
def test_an_envelope_cannot_carry_something_other_than_a_review(review: object) -> None:
    with pytest.raises(CodexReviewError):
        ReviewEnvelope(handle="review-handle", review=cast("CommandApprovalReview", review))


@pytest.mark.parametrize("handle", ["", "   ", 7, None, LONE_SURROGATE])
def test_an_envelope_cannot_carry_a_handle_the_transport_refuses(handle: object) -> None:
    with pytest.raises(CodexReviewError):
        ReviewEnvelope(handle=cast("str", handle), review=adapt_command(command_params()))


async def test_the_broker_surface_stays_payload_neutral() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection, command_request(reason=REASON))

    rendered = f"{interaction!r} {envelope!r} {connection!r}"

    assert not any(secret in rendered for secret in SECRETS)
    assert envelope.handle not in rendered
    assert "pending=1" in repr(interaction)
    interaction.close()
    with pytest.raises(CodexReviewError):
        await task


async def test_a_broker_failure_never_quotes_the_payload() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection, command_request(reason=REASON))
    interaction.close()

    with pytest.raises(CodexReviewError) as waiting:
        await task
    with pytest.raises(CodexReviewError) as late:
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)

    for failure in (waiting.value, late.value):
        report = hostile_report(failure)
        assert not any(secret in report for secret in SECRETS)
        assert envelope.handle not in report


async def test_pending_reviews_are_bounded() -> None:
    interaction = broker()
    connection = interaction.connect_reviewer()
    tasks: list[asyncio.Task[JsonValue]] = []

    for index in range(64):
        task, _ = await published(interaction, connection, command_request(index))
        tasks.append(task)

    with pytest.raises(CodexReviewError):
        await interaction.review(command_request("overflow"))
    assert pending_reviews(interaction) == 64
    interaction.close()
    for task in tasks:
        with pytest.raises(CodexReviewError):
            await task


async def test_unread_cancelled_reviews_never_starve_the_next_review() -> None:
    """A reviewer that never read them must not be blocked by reviews that already ended."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    cancelled: list[asyncio.Task[JsonValue]] = []

    for index in range(_MAX_UNREAD_REVIEWS):
        stale = asyncio.create_task(interaction.review(command_request(index)))
        cancelled.append(stale)
        await settle()
        stale.cancel()
    await settle()

    assert pending_reviews(interaction) == 0
    task, envelope = await published(interaction, connection, command_request("request-fresh"))
    assert isinstance(envelope.review, CommandApprovalReview)
    assert envelope.review.correlation.request_id == "request-fresh"
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()
    for stale in cancelled:
        with pytest.raises(asyncio.CancelledError):
            await stale


async def test_a_reviewer_that_stops_reading_its_withdrawals_is_bounded() -> None:
    """Withdrawals are bounded by the same stream, so a reviewer that stops reading stops work."""
    interaction = broker()
    connection = interaction.connect_reviewer()
    tasks: list[asyncio.Task[JsonValue]] = []

    for index in range(_MAX_UNREAD_REVIEWS):
        task, _ = await published(interaction, connection, command_request(index))
        tasks.append(task)
    for task in tasks:
        task.cancel()
    await settle()

    assert pending_reviews(interaction) == 0
    # Guarded, because a stream that stopped bounding this would otherwise never answer.
    with pytest.raises(CodexReviewError):
        await asyncio.wait_for(interaction.review(command_request("overflow")), STREAM_TIMEOUT)
    interaction.close()
    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task


def test_registration_covers_every_adaptable_alias() -> None:
    interaction = broker(legacy_contract())
    registrar = Registrar()

    interaction.register_approval_handlers(registrar)

    assert sorted(registrar.registered) == sorted(
        [COMMAND_METHOD, FILE_METHOD, LEGACY_COMMAND_METHOD, LEGACY_FILE_METHOD]
    )
    assert len(registrar.terminal) == 1
    assert registrar.order[0] == "notification_observer"
    assert len(registrar.notification) == 1
    interaction.close()


def test_registration_takes_the_connection_ending_with_the_approval_methods() -> None:
    """Answering approvals without hearing the connection end would leave them unanswerable."""
    interaction = broker(legacy_contract())
    registrar = Registrar()

    interaction.register_approval_handlers(registrar)

    assert "closed=False" in repr(interaction)
    registrar.terminal[0]()
    assert "closed=True" in repr(interaction)


def test_registration_registers_nothing_when_one_alias_is_unadaptable() -> None:
    contract = CodexProtocolContract(
        version="test-version",
        methods={},
        server_requests={
            ServerRequestCategory.COMMAND_APPROVAL: frozenset(
                {COMMAND_METHOD, LEGACY_COMMAND_METHOD}
            ),
            ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset({FILE_METHOD}),
        },
        unclassified_server_request_count=0,
        experimental_schema=False,
        approval_profiles={
            COMMAND_METHOD: command_profile(),
            FILE_METHOD: file_change_profile(),
        },
    )
    interaction = broker(contract)
    registrar = Registrar()

    with pytest.raises(CodexReviewError):
        interaction.register_approval_handlers(registrar)

    assert registrar.registered == []
    assert registrar.terminal == []
    assert registrar.notification == []
    interaction.close()


async def test_file_change_patch_notification_before_request_is_correlated_on_wire() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker(file_change_patch_contract())
    interaction.register_approval_handlers(peer)
    connection = interaction.connect_reviewer()
    notifications = peer.notifications()
    await peer.start()
    reader.feed_data(
        json.dumps(
            {
                "method": "item/fileChange/patchUpdated",
                "params": file_change_patch_params(),
            }
        ).encode()
        + b"\n"
        + json.dumps(
            {"id": FILE_REQUEST_ID, "method": FILE_METHOD, "params": file_change_params()}
        ).encode()
        + b"\n"
    )

    envelope = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
    notification = await asyncio.wait_for(anext(notifications), STREAM_TIMEOUT)
    assert isinstance(envelope, ReviewEnvelope)
    assert isinstance(envelope.review, FileChangeApprovalReview)
    assert envelope.review.changes == (
        FileChangeEntry(FileChangeKind.UPDATE, CHANGED_PATH, MOVED_PATH),
    )
    assert notification.method == "item/fileChange/patchUpdated"
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)

    try:
        assert await asyncio.wait_for(writer.written.get(), STREAM_TIMEOUT) == {
            "id": FILE_REQUEST_ID,
            "result": {"decision": "accept"},
        }
        await settle()
        assert writer.written.empty()
    finally:
        await peer.close()
        interaction.close()


async def test_turn_terminal_then_late_patch_is_terminal_before_request_on_wire() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker(file_change_patch_contract(agent_events=True))
    interaction.register_approval_handlers(peer)
    connection = interaction.connect_reviewer()
    notifications = peer.notifications()
    await peer.start()
    messages = (
        {
            "method": "turn/completed",
            "params": {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "completed"},
            },
        },
        {
            "method": "item/fileChange/patchUpdated",
            "params": file_change_patch_params(),
        },
        {"id": FILE_REQUEST_ID, "method": FILE_METHOD, "params": file_change_params()},
    )
    reader.feed_data(b"".join(json.dumps(message).encode() + b"\n" for message in messages))

    try:
        with pytest.raises(CodexRpcProtocolError) as caught:
            await asyncio.wait_for(anext(notifications), STREAM_TIMEOUT)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
        await settle()

        report = hostile_report(caught.value)
        assert caught.value.data is None
        assert not any(secret in report for secret in SECRETS)
        assert writer.written.empty()
    finally:
        await peer.close()
        interaction.close()


async def test_patch_then_turn_terminal_clears_explanation_before_request_on_wire() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker(file_change_patch_contract(agent_events=True))
    interaction.register_approval_handlers(peer)
    connection = interaction.connect_reviewer()
    await peer.start()
    messages = (
        {
            "method": "item/fileChange/patchUpdated",
            "params": file_change_patch_params(),
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "completed"},
            },
        },
        {"id": FILE_REQUEST_ID, "method": FILE_METHOD, "params": file_change_params()},
    )
    reader.feed_data(b"".join(json.dumps(message).encode() + b"\n" for message in messages))

    try:
        written = await asyncio.wait_for(writer.written.get(), STREAM_TIMEOUT)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(connection), 0.05)

        assert written == {
            "id": FILE_REQUEST_ID,
            "error": {"code": -32603, "message": "server request handler failed"},
        }
        assert not any(secret in repr(written) for secret in SECRETS)
        assert writer.written.empty()
    finally:
        await peer.close()
        interaction.close()


async def test_turn_terminal_guard_allows_a_different_next_turn() -> None:
    next_thread = "thread-next"
    next_turn = "turn-next"
    next_item = "item-next"
    interaction = broker(file_change_patch_contract(agent_events=True))
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    connection = interaction.connect_reviewer()
    observe = registrar.notification[0]
    observe(
        RpcNotification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "completed"},
            },
        )
    )
    observe(
        RpcNotification(
            "item/fileChange/patchUpdated",
            file_change_patch_params(
                thread_id=next_thread,
                turn_id=next_turn,
                item_id=next_item,
            ),
        )
    )
    request = RpcServerRequest(
        "next-request",
        FILE_METHOD,
        file_change_params(
            threadId=next_thread,
            turnId=next_turn,
            itemId=next_item,
        ),
    )

    task, envelope = await published(interaction, connection, request)
    assert isinstance(envelope.review, FileChangeApprovalReview)
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()


def test_later_terminal_does_not_forget_an_older_terminal_turn() -> None:
    interaction = broker(file_change_patch_contract(agent_events=True))
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    observe = registrar.notification[0]
    observe(
        RpcNotification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "completed"},
            },
        )
    )
    observe(
        RpcNotification(
            "turn/completed",
            {"threadId": "thread-next", "turn": {"id": "turn-next"}},
        )
    )

    with pytest.raises(CodexRpcProtocolError, match="after turn terminal"):
        observe(
            RpcNotification(
                "item/fileChange/patchUpdated",
                file_change_patch_params(),
            )
        )

    interaction.close()


async def test_approval_for_a_turn_not_owned_by_the_agent_is_refused() -> None:
    interaction = InteractionBroker(file_change_patch_contract(agent_events=True))
    interaction.bind_active_turn_check(
        lambda thread_id, turn_id: (thread_id, turn_id) == ("active-thread", "active-turn")
    )
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    connection = interaction.connect_reviewer()
    registrar.notification[0](
        RpcNotification(
            "item/fileChange/patchUpdated",
            file_change_patch_params(),
        )
    )

    with pytest.raises(CodexReviewError, match="active Agent turn"):
        await interaction.review(file_request())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)

    interaction.close()


async def test_command_approval_for_a_completed_agent_turn_is_not_published() -> None:
    interaction = InteractionBroker(approval_contract())
    interaction.bind_active_turn_check(lambda _thread_id, _turn_id: False)
    connection = interaction.connect_reviewer()

    with pytest.raises(CodexReviewError, match="active Agent turn"):
        await interaction.review(command_request())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)

    interaction.close()


async def test_terminal_tombstone_precedes_a_still_active_command_callback() -> None:
    interaction = broker(file_change_patch_contract(agent_events=True))
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    connection = interaction.connect_reviewer()
    registrar.notification[0](
        RpcNotification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "completed"},
            },
        )
    )

    with pytest.raises(CodexReviewError, match="active Agent turn"):
        await asyncio.wait_for(
            interaction.review(command_request()),
            STREAM_TIMEOUT,
        )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)

    interaction.close()


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_terminal_notification_withdraws_an_already_published_review(
    status: str,
) -> None:
    interaction = broker(file_change_patch_contract(agent_events=True))
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection, command_request())

    registrar.notification[0](
        RpcNotification(
            "turn/completed",
            {"threadId": THREAD_ID, "turn": {"id": TURN_ID, "status": status}},
        )
    )

    with pytest.raises(CodexReviewError, match="cancelled"):
        await asyncio.wait_for(task, STREAM_TIMEOUT)
    assert await asyncio.wait_for(anext(connection), STREAM_TIMEOUT) == ReviewWithdrawal(
        handle=envelope.handle
    )
    with pytest.raises(CodexReviewError, match="not pending"):
        interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    interaction.close()


async def test_in_progress_notification_keeps_a_published_review_decidable() -> None:
    interaction = broker(file_change_patch_contract(agent_events=True))
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    connection = interaction.connect_reviewer()
    task, envelope = await published(interaction, connection, command_request())

    registrar.notification[0](
        RpcNotification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "inProgress"},
            },
        )
    )

    assert pending_reviews(interaction) == 1
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}
    interaction.close()


async def test_file_change_patch_explanation_is_one_shot() -> None:
    interaction = broker(file_change_patch_contract())
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    connection = interaction.connect_reviewer()
    registrar.notification[0](
        RpcNotification("item/fileChange/patchUpdated", file_change_patch_params())
    )

    task, envelope = await published(interaction, connection, file_request())
    interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
    assert await asyncio.wait_for(task, STREAM_TIMEOUT) == {"decision": "accept"}

    with pytest.raises(CodexSchemaError):
        await interaction.review(file_request("second-file-request"))
    interaction.close()


def test_file_change_patch_map_replaces_a_key_without_consuming_capacity() -> None:
    interaction = broker(file_change_patch_contract())
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    observe = registrar.notification[0]

    for index in range(65):
        observe(
            RpcNotification(
                "item/fileChange/patchUpdated",
                file_change_patch_params(
                    changes=[
                        {
                            "diff": f"secret-{index}",
                            "kind": {"type": "update"},
                            "path": f"replacement-{index}.txt",
                        }
                    ]
                ),
            )
        )

    assert len(interaction._file_change_explanations) == 1  # noqa: SLF001
    assert interaction._file_change_explanations[(THREAD_ID, TURN_ID, ITEM_ID)].changes == (  # noqa: SLF001
        FileChangeEntry(FileChangeKind.UPDATE, "replacement-64.txt"),
    )
    interaction.close()


def test_file_change_patch_map_refuses_a_sixty_fifth_distinct_key() -> None:
    interaction = broker(file_change_patch_contract())
    registrar = Registrar()
    interaction.register_approval_handlers(registrar)
    observe = registrar.notification[0]

    for index in range(64):
        observe(
            RpcNotification(
                "item/fileChange/patchUpdated",
                file_change_patch_params(item_id=f"item-{index}"),
            )
        )

    with pytest.raises(CodexRpcProtocolError) as caught:
        observe(
            RpcNotification(
                "item/fileChange/patchUpdated",
                file_change_patch_params(item_id="item-overflow"),
            )
        )

    assert caught.value.data is None
    assert len(interaction._file_change_explanations) == 64  # noqa: SLF001
    interaction.close()


def test_file_change_patch_map_clears_on_turn_terminal_and_broker_endings() -> None:
    terminal_interaction = broker(file_change_patch_contract(agent_events=True))
    terminal_registrar = Registrar()
    terminal_interaction.register_approval_handlers(terminal_registrar)
    terminal_registrar.notification[0](
        RpcNotification("item/fileChange/patchUpdated", file_change_patch_params())
    )
    terminal_registrar.notification[0](
        RpcNotification(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {"id": TURN_ID, "status": "completed"},
            },
        )
    )
    assert terminal_interaction._file_change_explanations == {}  # noqa: SLF001
    terminal_interaction.close()

    for ending in ("connection", "broker"):
        interaction = broker(file_change_patch_contract())
        registrar = Registrar()
        interaction.register_approval_handlers(registrar)
        registrar.notification[0](
            RpcNotification("item/fileChange/patchUpdated", file_change_patch_params())
        )
        if ending == "connection":
            interaction.connection_lost()
        else:
            interaction.close()
        assert interaction._file_change_explanations == {}  # noqa: SLF001


async def test_invalid_file_change_patch_terminal_is_payload_free() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker(file_change_patch_contract())
    interaction.register_approval_handlers(peer)
    notifications = peer.notifications()
    await peer.start()
    reader.feed_data(
        json.dumps(
            {
                "method": "item/fileChange/patchUpdated",
                "params": file_change_patch_params(unknown="PATCH_NOTIFICATION_SECRET"),
            }
        ).encode()
        + b"\n"
    )

    try:
        with pytest.raises(CodexRpcProtocolError) as caught:
            await asyncio.wait_for(anext(notifications), STREAM_TIMEOUT)
        report = hostile_report(caught.value)
        assert caught.value.data is None
        assert "PATCH_NOTIFICATION_SECRET" not in report
        assert not any(secret in report for secret in SECRETS)
        assert writer.written.empty()
    finally:
        await peer.close()
        interaction.close()


async def test_one_server_request_answers_once_through_a_double_decision() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker()
    interaction.register_approval_handlers(peer)
    connection = interaction.connect_reviewer()
    await peer.start()
    reader.feed_data(
        json.dumps(
            {"id": COMMAND_REQUEST_ID, "method": COMMAND_METHOD, "params": command_params()}
        ).encode()
        + b"\n"
    )
    envelope = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)

    async def click() -> BaseException | None:
        try:
            interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
        except CodexReviewError as failure:
            return failure
        return None

    outcomes = await asyncio.gather(click(), click())

    try:
        written = await asyncio.wait_for(writer.written.get(), STREAM_TIMEOUT)
        await settle()
    finally:
        await peer.close()
        interaction.close()

    assert [outcome is None for outcome in outcomes].count(True) == 1
    assert written == {"id": COMMAND_REQUEST_ID, "result": {"decision": "accept"}}
    assert writer.written.empty()


async def test_a_reviewer_disconnect_answers_the_server_request_once_without_a_decision() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker()
    interaction.register_approval_handlers(peer)
    connection = interaction.connect_reviewer()
    await peer.start()
    reader.feed_data(
        json.dumps(
            {"id": COMMAND_REQUEST_ID, "method": COMMAND_METHOD, "params": command_params()}
        ).encode()
        + b"\n"
    )
    envelope = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)

    interaction.disconnect_reviewer(connection)

    try:
        written = await asyncio.wait_for(writer.written.get(), STREAM_TIMEOUT)
        with pytest.raises(CodexReviewError):
            interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
        await settle()
    finally:
        await peer.close()
        interaction.close()

    assert written == {
        "id": COMMAND_REQUEST_ID,
        "error": {"code": -32603, "message": "server request handler failed"},
    }
    assert writer.written.empty()


async def test_connection_loss_never_writes_a_second_response() -> None:
    reader = asyncio.StreamReader()
    writer = _ResponseWriter()
    peer = RpcPeer(reader, cast("asyncio.StreamWriter", writer), request_timeout=1.0)
    interaction = broker()
    interaction.register_approval_handlers(peer)
    connection = interaction.connect_reviewer()
    await peer.start()
    reader.feed_data(
        json.dumps(
            {"id": COMMAND_REQUEST_ID, "method": COMMAND_METHOD, "params": command_params()}
        ).encode()
        + b"\n"
    )
    envelope = await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)

    reader.feed_eof()
    await settle()

    try:
        with pytest.raises(CodexReviewError):
            interaction.decide(connection, envelope.handle, ApprovalDecision.ACCEPT)
        assert pending_reviews(interaction) == 0
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(connection), STREAM_TIMEOUT)
        with pytest.raises(CodexReviewError):
            await interaction.review(command_request("request-after-loss"))
        with pytest.raises(CodexReviewError):
            interaction.connect_reviewer()
        assert writer.written.empty()
    finally:
        await peer.close()
        interaction.close()
