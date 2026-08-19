from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from moco.codex.capabilities import (
    ApprovalMode,
    CapabilityDiscovery,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    SandboxMode,
)
from moco.codex.rpc import JsonValue, RpcPeer
from moco.codex.schema import (
    AGENT_READINESS_METHODS,
    STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES,
    AgentEventProfile,
    ApprovalCorrelation,
    ApprovalDecision,
    ApprovalProfile,
    ClientMethodContract,
    CodexProtocolContract,
    FileChangePatchProfile,
    ParamsKind,
    SemanticMethod,
    ServerRequestCategory,
    _json_value_key,
    _ValueContract,
)
from moco.errors import CodexRpcError, CodexRpcProtocolError, CodexSchemaError

_OMITTED = object()
type Action = JsonValue | BaseException
HOSTILE_METHOD_SECRET = "HOSTILE_METHOD_NAME_SECRET"  # noqa: S105


class HostileMethodName(str):
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        raise RuntimeError(HOSTILE_METHOD_SECRET)

    def __hash__(self) -> int:
        return str.__hash__(self)

    def encode(self, *_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError(HOSTILE_METHOD_SECRET)


class FakeRequester:
    def __init__(
        self,
        actions: dict[str, Action],
        *,
        sequences: dict[str, list[Action]] | None = None,
    ) -> None:
        self.actions = actions
        self.sequences = {method: deque(values) for method, values in (sequences or {}).items()}
        self.calls: list[tuple[str, object]] = []

    async def request(
        self,
        method: str,
        params: object = _OMITTED,
        **_kwargs: object,
    ) -> JsonValue:
        self.calls.append((method, params))
        sequence = self.sequences.get(method)
        action = sequence.popleft() if sequence else self.actions[method]
        if isinstance(action, BaseException):
            raise action
        return action

    def params_for(self, method: str) -> list[object]:
        return [params for called_method, params in self.calls if called_method == method]


class _PeerWriter:
    def __init__(self) -> None:
        self.written: asyncio.Queue[dict[str, JsonValue]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            decoded = json.loads(line)
            self.written.put_nowait(cast("dict[str, JsonValue]", decoded))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeContractProbe:
    def __init__(self, result: CodexProtocolContract | BaseException) -> None:
        self.result = result
        self.calls = 0

    async def probe(self) -> CodexProtocolContract:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


_ALIASES = {
    SemanticMethod.ACCOUNT_READ: "alias/account",
    SemanticMethod.CONFIG_READ: "alias/config",
    SemanticMethod.CONFIG_REQUIREMENTS_READ: "alias/requirements",
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: "alias/features",
    SemanticMethod.REALTIME_VOICES_LIST: "alias/voices",
    SemanticMethod.THREAD_START: "alias/thread-start",
    SemanticMethod.THREAD_REALTIME_START: "alias/realtime-start",
    SemanticMethod.TURN_START: "alias/turn-start",
    SemanticMethod.TURN_STEER: "alias/steer",
    SemanticMethod.TURN_INTERRUPT: "alias/interrupt",
}
_FIELDS = {
    SemanticMethod.ACCOUNT_READ: frozenset(),
    SemanticMethod.CONFIG_READ: frozenset({"cwd", "includeLayers"}),
    SemanticMethod.CONFIG_REQUIREMENTS_READ: frozenset(),
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: frozenset({"cursor"}),
    SemanticMethod.REALTIME_VOICES_LIST: frozenset(),
    SemanticMethod.THREAD_START: frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
    SemanticMethod.THREAD_REALTIME_START: frozenset(
        {
            "clientManagedHandoffs",
            "codexResponseHandoffMode",
            "codexResponsesAsItems",
            "delegationAckFiller",
            "includeStartupContext",
            "outputModality",
            "prompt",
            "threadId",
            "transport",
            "version",
        }
    ),
    SemanticMethod.TURN_START: frozenset({"input", "threadId"}),
    SemanticMethod.TURN_STEER: frozenset({"expectedTurnId", "input", "threadId"}),
    SemanticMethod.TURN_INTERRUPT: frozenset({"threadId", "turnId"}),
}


def raw_method(category: ServerRequestCategory) -> str:
    return f"raw-{category.value}"


_STRING_CONTRACT = _ValueContract(types=frozenset({"string"}))
_DECISION_CONTRACT = _ValueContract(
    one_of=tuple(
        _ValueContract(
            types=frozenset({"string"}),
            enum=(cast("tuple[str, str]", _json_value_key(wire)),),
        )
        for wire in ("accept", "decline", "cancel")
    )
)
_PATCH_PROFILE = FileChangePatchProfile(
    "item/fileChange/patchUpdated",
    _ValueContract(types=frozenset({"object"})),
)


# The members each newer family declares beside its correlation, which differ by family
# exactly as the generated bundles differ.
_FAMILY_MEMBERS: dict[ServerRequestCategory, tuple[str, ...]] = {
    ServerRequestCategory.COMMAND_APPROVAL: ("command", "cwd"),
    ServerRequestCategory.FILE_CHANGE_APPROVAL: ("reason",),
}


def approval_profile(category: ServerRequestCategory) -> ApprovalProfile:
    """One adaptable approval family, as a generated bundle would report it."""
    return ApprovalProfile(
        category=category,
        correlation=ApprovalCorrelation.THREAD_ITEM,
        required_members=frozenset({"threadId", "turnId", "itemId"}),
        absent_or_null_members=frozenset(),
        member_contracts=dict.fromkeys(
            ("threadId", "turnId", "itemId", *_FAMILY_MEMBERS[category]),
            _STRING_CONTRACT,
        ),
        argv_member=None,
        changes_member=None,
        offer_member=None,
        decisions={
            ApprovalDecision.ACCEPT: "accept",
            ApprovalDecision.DECLINE: "decline",
            ApprovalDecision.CANCEL: "cancel",
        },
        decision_contract=_DECISION_CONTRACT,
    )


def legacy_file_approval_profile() -> ApprovalProfile:
    """One legacy file family that carries its changed files in the approval itself."""

    def literal(*values: str) -> _ValueContract:
        return _ValueContract(
            types=frozenset({"string"}),
            enum=tuple(cast("tuple[str, str]", _json_value_key(value)) for value in values),
        )

    nullable_string = _ValueContract(types=frozenset({"null", "string"}))
    file_change = _ValueContract(
        one_of=(
            _ValueContract(
                types=frozenset({"object"}),
                properties={"content": _STRING_CONTRACT, "type": literal("add")},
                required=frozenset({"content", "type"}),
                additional_refused=True,
            ),
            _ValueContract(
                types=frozenset({"object"}),
                properties={"content": _STRING_CONTRACT, "type": literal("delete")},
                required=frozenset({"content", "type"}),
                additional_refused=True,
            ),
            _ValueContract(
                types=frozenset({"object"}),
                properties={
                    "move_path": nullable_string,
                    "type": literal("update"),
                    "unified_diff": _STRING_CONTRACT,
                },
                required=frozenset({"type", "unified_diff"}),
                additional_refused=True,
            ),
        )
    )
    return ApprovalProfile(
        category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
        correlation=ApprovalCorrelation.CONVERSATION_CALL,
        required_members=frozenset({"callId", "conversationId", "fileChanges"}),
        absent_or_null_members=frozenset({"grantRoot"}),
        member_contracts={
            "callId": _STRING_CONTRACT,
            "conversationId": _STRING_CONTRACT,
            "fileChanges": _ValueContract(types=frozenset({"object"}), additional=file_change),
            "grantRoot": nullable_string,
            "reason": nullable_string,
        },
        argv_member=None,
        changes_member="fileChanges",
        offer_member=None,
        decisions={
            ApprovalDecision.ACCEPT: "approved",
            ApprovalDecision.DECLINE: "denied",
            ApprovalDecision.CANCEL: "abort",
        },
        decision_contract=literal("approved", "denied", "abort"),
    )


def adaptable_profiles(
    categories: frozenset[ServerRequestCategory],
) -> dict[str, ApprovalProfile]:
    return {
        raw_method(category): approval_profile(category)
        for category in categories & STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    }


def make_contract(
    *,
    included: frozenset[SemanticMethod] = frozenset(SemanticMethod),
    overrides: dict[SemanticMethod, ClientMethodContract] | None = None,
    categories: frozenset[ServerRequestCategory] = frozenset(
        {
            ServerRequestCategory.COMMAND_APPROVAL,
            ServerRequestCategory.FILE_CHANGE_APPROVAL,
        }
    ),
    unclassified: int = 0,
    profiles: dict[str, ApprovalProfile] | None = None,
    file_change_patch_profile: FileChangePatchProfile | None = _PATCH_PROFILE,
) -> CodexProtocolContract:
    methods: dict[SemanticMethod, ClientMethodContract] = {}
    for semantic in included:
        params_kind = (
            ParamsKind.OMITTED
            if semantic is SemanticMethod.CONFIG_REQUIREMENTS_READ
            else ParamsKind.OBJECT
        )
        methods[semantic] = ClientMethodContract(
            _ALIASES[semantic],
            params_kind,
            _FIELDS[semantic],
        )
    methods.update(overrides or {})
    return CodexProtocolContract(
        version="codex-fixture",
        methods=methods,
        server_requests={category: frozenset({raw_method(category)}) for category in categories},
        unclassified_server_request_count=unclassified,
        experimental_schema=True,
        approval_profiles=adaptable_profiles(categories) if profiles is None else profiles,
        file_change_patch_profile=file_change_patch_profile,
        agent_event_profile=AgentEventProfile(
            turn_completed_method="turn/completed",
            item_completed_method="item/completed",
            agent_message_delta_method="item/agentMessage/delta",
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
                "text": frozenset({"string"}),
                "type": frozenset({"string"}),
                "phase": frozenset({"string", "null"}),
            },
            agent_message_delta_required_fields=frozenset(
                {"threadId", "turnId", "itemId", "delta"}
            ),
            agent_message_delta_field_types={
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
                "itemId": frozenset({"string"}),
                "delta": frozenset({"string"}),
            },
            agent_message_phase_values=frozenset({"commentary", "final_answer"}),
            agent_message_phase_optional=True,
            turn_status_values=frozenset({"completed", "interrupted", "failed", "inProgress"}),
            completed_status="completed",
            interrupted_status="interrupted",
            failed_status="failed",
            in_progress_status="inProgress",
        ),
    )


def happy_actions(
    *,
    sandbox: str = "workspace-write",
    approval: JsonValue = "on-request",
) -> dict[str, Action]:
    return {
        _ALIASES[SemanticMethod.ACCOUNT_READ]: {
            "account": {"email": "ACCOUNT_EMAIL_SECRET"},
            "requiresOpenaiAuth": True,
        },
        _ALIASES[SemanticMethod.CONFIG_READ]: {
            "config": {"sandbox_mode": sandbox, "approval_policy": approval},
            "layers": [],
        },
        _ALIASES[SemanticMethod.CONFIG_REQUIREMENTS_READ]: {"requirements": {"managed": True}},
        _ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]: {
            "data": [{"name": "realtime_conversation", "enabled": True}],
            "nextCursor": None,
        },
        _ALIASES[SemanticMethod.REALTIME_VOICES_LIST]: {
            "voices": {"v-next": ["VOICE_NAME_SECRET"]},
        },
    }


async def discover(
    tmp_path: Path,
    *,
    contract: CodexProtocolContract | None = None,
    actions: dict[str, Action] | None = None,
    sequences: dict[str, list[Action]] | None = None,
) -> tuple[CapabilitySnapshot, FakeRequester]:
    requester = FakeRequester(actions or happy_actions(), sequences=sequences)
    snapshot = await CapabilityDiscovery(
        requester,
        working_directory=tmp_path,
        contract=contract or make_contract(),
    ).discover()
    return snapshot, requester


async def test_happy_snapshot_uses_aliases_and_exact_adapter_params(tmp_path: Path) -> None:
    snapshot, requester = await discover(tmp_path)

    assert snapshot == CapabilitySnapshot(
        version="codex-fixture",
        account=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        effective_policy=EffectivePolicy(SandboxMode.WORKSPACE_WRITE, ApprovalMode.ON_REQUEST),
        policy_state=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        managed_requirements=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        realtime=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        interrupt=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        steer=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        server_requests=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        server_request_categories=frozenset(
            {
                ServerRequestCategory.COMMAND_APPROVAL,
                ServerRequestCategory.FILE_CHANGE_APPROVAL,
            }
        ),
        has_unclassified_server_requests=False,
    )
    assert requester.params_for(_ALIASES[SemanticMethod.ACCOUNT_READ]) == [{}]
    assert requester.params_for(_ALIASES[SemanticMethod.CONFIG_READ]) == [
        {"cwd": str(tmp_path), "includeLayers": True}
    ]
    assert requester.params_for(_ALIASES[SemanticMethod.CONFIG_REQUIREMENTS_READ]) == [_OMITTED]
    assert requester.params_for(_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]) == [
        {"cursor": None}
    ]
    assert requester.params_for(_ALIASES[SemanticMethod.REALTIME_VOICES_LIST]) == [{}]
    assert requester.params_for(_ALIASES[SemanticMethod.TURN_INTERRUPT]) == []
    rendered = repr(snapshot)
    assert "ACCOUNT_EMAIL_SECRET" not in rendered
    assert "VOICE_NAME_SECRET" not in rendered
    assert str(tmp_path) not in rendered
    assert "alias/" not in rendered


async def test_modern_file_approval_without_patch_evidence_degrades_review_not_agent(
    tmp_path: Path,
) -> None:
    snapshot, _ = await discover(
        tmp_path,
        contract=make_contract(file_change_patch_profile=None),
    )

    unadaptable = CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "approval_family_unadaptable",
    )
    assert snapshot.server_requests == unadaptable
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_legacy_inline_file_approval_does_not_require_patch_evidence(
    tmp_path: Path,
) -> None:
    profiles = adaptable_profiles(STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES)
    profiles[raw_method(ServerRequestCategory.FILE_CHANGE_APPROVAL)] = (
        legacy_file_approval_profile()
    )

    snapshot, _ = await discover(
        tmp_path,
        contract=make_contract(
            profiles=profiles,
            file_change_patch_profile=None,
        ),
    )

    assert snapshot.server_requests == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_modern_file_approval_with_patch_evidence_is_ready_and_private(
    tmp_path: Path,
) -> None:
    private_method = "PRIVATE_PATCH_METHOD_SECRET"
    snapshot, _ = await discover(
        tmp_path,
        contract=make_contract(
            file_change_patch_profile=FileChangePatchProfile(
                private_method,
                _ValueContract(types=frozenset({"object"})),
            )
        ),
    )

    assert snapshot.server_requests == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert private_method not in repr(snapshot)


async def test_missing_patch_evidence_precedes_unclassified_server_requests(
    tmp_path: Path,
) -> None:
    snapshot, _ = await discover(
        tmp_path,
        contract=make_contract(
            file_change_patch_profile=None,
            unclassified=1,
        ),
    )

    assert snapshot.server_requests == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "approval_family_unadaptable",
    )
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_missing_agent_event_contract_withdraws_agent_admission(tmp_path: Path) -> None:
    base = make_contract()
    contract = CodexProtocolContract(
        version=base.version,
        methods=base.methods,
        server_requests=base.server_requests,
        unclassified_server_request_count=base.unclassified_server_request_count,
        experimental_schema=base.experimental_schema,
        approval_profiles=base.approval_profiles,
        agent_event_profile=None,
    )

    snapshot, _ = await discover(tmp_path, contract=contract)

    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "agent_event_contract_unavailable",
    )


@pytest.mark.parametrize(
    ("sandbox", "approval"),
    [
        ("read-only", "untrusted"),
        ("read-only", "on-request"),
        ("read-only", "never"),
        ("workspace-write", "untrusted"),
        ("workspace-write", "on-request"),
        ("workspace-write", "never"),
        ("danger-full-access", "untrusted"),
        ("danger-full-access", "on-request"),
    ],
)
async def test_all_safe_policy_combinations_admit_agent(
    tmp_path: Path,
    sandbox: str,
    approval: str,
) -> None:
    snapshot, _ = await discover(
        tmp_path,
        actions=happy_actions(sandbox=sandbox, approval=approval),
    )

    assert snapshot.policy_state.status is CapabilityStatus.AVAILABLE
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_global_unsafe_policy_does_not_change_profile_independent_admission(
    tmp_path: Path,
) -> None:
    snapshot, _ = await discover(
        tmp_path,
        actions=happy_actions(sandbox="danger-full-access", approval="never"),
    )

    assert snapshot.effective_policy == EffectivePolicy(
        SandboxMode.DANGER_FULL_ACCESS,
        ApprovalMode.NEVER,
    )
    assert snapshot.policy_state == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE


async def test_granular_policy_is_validated_and_reduced_without_keys(tmp_path: Path) -> None:
    secret_key = "GRANULAR_RULE_SECRET"  # noqa: S105
    actions = happy_actions(approval={"granular": {secret_key: True, "sandbox_approval": False}})

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.effective_policy == EffectivePolicy(
        SandboxMode.WORKSPACE_WRITE,
        ApprovalMode.GRANULAR,
    )
    assert secret_key not in repr(snapshot)


@pytest.mark.parametrize(
    "approval",
    [
        {"granular": {}},
        {"granular": {"rules": "sometimes"}},
        {"granular": {1: True}},
        {"granular": {"rules": True}, "extra": {}},
    ],
)
async def test_malformed_granular_policy_is_version_mismatch(
    tmp_path: Path,
    approval: JsonValue,
) -> None:
    snapshot, _ = await discover(tmp_path, actions=happy_actions(approval=approval))

    assert snapshot.effective_policy is None
    assert snapshot.policy_state == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


@pytest.mark.parametrize(
    "config_response",
    [
        {},
        {"config": []},
        {"config": {"sandbox_mode": "future", "approval_policy": "on-request"}},
        {"config": {"sandbox_mode": "read-only", "approval_policy": "future"}},
    ],
)
async def test_invalid_config_shapes_fail_closed(
    tmp_path: Path,
    config_response: JsonValue,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_READ]] = config_response

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.effective_policy is None
    assert snapshot.policy_state.status is CapabilityStatus.VERSION_MISMATCH
    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.realtime.status is CapabilityStatus.VERSION_MISMATCH


async def test_nonempty_realtime_backend_prompt_override_disables_realtime(
    tmp_path: Path,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_READ]] = {
        "config": {
            "sandbox_mode": "workspace-write",
            "approval_policy": "on-request",
        },
        "layers": [
            {
                "config": {
                    "experimental_realtime_ws_backend_prompt": "PRIVATE_OVERRIDE",
                },
                "disabledReason": None,
            }
        ],
    }

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(
        CapabilityStatus.DISABLED,
        "prompt_overridden",
    )
    assert "PRIVATE_OVERRIDE" not in repr(snapshot)


async def test_disabled_realtime_backend_prompt_override_is_ignored(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_READ]] = {
        "config": {
            "sandbox_mode": "workspace-write",
            "approval_policy": "on-request",
        },
        "layers": [
            {
                "config": {
                    "experimental_realtime_ws_backend_prompt": "PRIVATE_OVERRIDE",
                },
                "disabledReason": "superseded",
            }
        ],
    }

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"account": None, "requiresOpenaiAuth": True},
            CapabilityState(CapabilityStatus.AUTHENTICATION_REQUIRED, "authentication_required"),
        ),
        (
            {"requiresOpenaiAuth": False},
            CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
        ),
        (
            {"account": "malformed", "requiresOpenaiAuth": True},
            CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response"),
        ),
        (
            {},
            CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response"),
        ),
    ],
)
async def test_account_response_semantics(
    tmp_path: Path,
    response: JsonValue,
    expected: CapabilityState,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.ACCOUNT_READ]] = response

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.account == expected
    assert snapshot.agent_admission.status is expected.status


async def test_feature_pagination_sends_json_null_then_cursor(tmp_path: Path) -> None:
    first: JsonValue = {
        "data": [{"name": "future_feature", "enabled": True}],
        "nextCursor": "page-2",
    }
    second: JsonValue = {
        "data": [{"name": "realtime_conversation", "enabled": True}],
        "nextCursor": None,
    }
    snapshot, requester = await discover(
        tmp_path,
        sequences={_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]: [first, second]},
    )

    assert requester.params_for(_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]) == [
        {"cursor": None},
        {"cursor": "page-2"},
    ]
    assert snapshot.realtime == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_disabled_feature_overrides_usable_voices_without_leaking_names(
    tmp_path: Path,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]] = {
        "data": [{"name": "realtime_conversation", "enabled": False}],
        "nextCursor": None,
    }

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(
        CapabilityStatus.DISABLED,
        "feature_disabled",
    )
    assert "VOICE_NAME_SECRET" not in repr(snapshot)


@pytest.mark.parametrize(
    "feature_response",
    [
        {},
        {"data": {}, "nextCursor": None},
        {"data": [{"name": "realtime_conversation"}], "nextCursor": None},
        {"data": [], "nextCursor": 7},
    ],
)
async def test_malformed_feature_response_fails_realtime_closed(
    tmp_path: Path,
    feature_response: JsonValue,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]] = feature_response

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )


async def test_repeated_feature_cursor_is_version_mismatch(tmp_path: Path) -> None:
    page: JsonValue = {"data": [], "nextCursor": "same"}
    snapshot, requester = await discover(
        tmp_path,
        sequences={_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]: [page, page]},
    )

    assert snapshot.realtime.status is CapabilityStatus.VERSION_MISMATCH
    assert requester.params_for(_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]) == [
        {"cursor": None},
        {"cursor": "same"},
    ]


async def test_empty_voice_set_is_disabled(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.REALTIME_VOICES_LIST]] = {"voices": {"v1": []}}

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(CapabilityStatus.DISABLED, "no_voice")


async def test_voice_shape_accepts_default_string_alongside_voice_lists(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.REALTIME_VOICES_LIST]] = {
        "voices": {
            "ARBITRARY_DEFAULT_FIELD": "RAW_DEFAULT_VOICE_SECRET",
            "ARBITRARY_LIST_FIELD": ["RAW_LIST_VOICE_SECRET"],
        }
    }

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    rendered = repr(snapshot)
    assert "ARBITRARY_DEFAULT_FIELD" not in rendered
    assert "RAW_DEFAULT_VOICE_SECRET" not in rendered
    assert "RAW_LIST_VOICE_SECRET" not in rendered


@pytest.mark.parametrize(
    "voice_response",
    [
        {},
        {"voices": []},
        {"voices": {"group": [7]}},
        {"voices": {"default": ""}},
    ],
)
async def test_malformed_voice_response_is_version_mismatch(
    tmp_path: Path,
    voice_response: JsonValue,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.REALTIME_VOICES_LIST]] = voice_response

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )


async def test_missing_feature_cursor_is_a_valid_terminal_page(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]] = {
        "data": [{"name": "realtime_conversation", "enabled": True}],
    }

    snapshot, requester = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert requester.params_for(_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]) == [
        {"cursor": None}
    ]


@pytest.mark.parametrize(
    ("requirements_response", "expected"),
    [
        ([], CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response")),
        (
            CodexRpcError("REQUIREMENTS_SECRET"),
            CapabilityState(CapabilityStatus.ERROR, "probe_failed"),
        ),
    ],
)
async def test_managed_requirements_failures_do_not_gate_admission(
    tmp_path: Path,
    requirements_response: Action,
    expected: CapabilityState,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_REQUIREMENTS_READ]] = requirements_response

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.managed_requirements == expected
    assert snapshot.agent_admission.status is CapabilityStatus.AVAILABLE
    assert "REQUIREMENTS_SECRET" not in repr(snapshot)


async def test_feature_rpc_failure_does_not_hide_usable_voices(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]] = CodexRpcError("FEATURE_SECRET")

    snapshot, _ = await discover(tmp_path, actions=actions)

    assert snapshot.realtime == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert "FEATURE_SECRET" not in repr(snapshot)


async def test_feature_page_budget_fails_closed_and_remains_bounded(tmp_path: Path) -> None:
    pages: list[Action] = [{"data": [], "nextCursor": f"cursor-{index}"} for index in range(32)]
    snapshot, requester = await discover(
        tmp_path,
        sequences={_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST]: pages},
    )

    assert snapshot.realtime.status is CapabilityStatus.VERSION_MISMATCH
    assert len(requester.params_for(_ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST])) == 32


async def test_config_failure_preserves_agent_readiness_but_blocks_realtime_identity(
    tmp_path: Path,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_READ]] = CodexRpcError("CONFIG_ERROR_SECRET")
    included = frozenset(SemanticMethod) - {SemanticMethod.EXPERIMENTAL_FEATURE_LIST}

    snapshot, requester = await discover(
        tmp_path,
        contract=make_contract(included=included),
        actions=actions,
    )

    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.policy_state == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert snapshot.realtime == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert requester.params_for(_ALIASES[SemanticMethod.REALTIME_VOICES_LIST]) == [{}]
    assert "CONFIG_ERROR_SECRET" not in repr(snapshot)


async def test_missing_config_preserves_agent_readiness_but_blocks_realtime_identity(
    tmp_path: Path,
) -> None:
    included = frozenset(SemanticMethod) - {SemanticMethod.CONFIG_READ}

    snapshot, _ = await discover(tmp_path, contract=make_contract(included=included))

    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.policy_state == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "method_unavailable",
    )
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.realtime == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "method_unavailable",
    )


@pytest.mark.parametrize(
    ("categories", "unclassified", "server_status", "admission_status"),
    [
        (
            frozenset({ServerRequestCategory.COMMAND_APPROVAL}),
            0,
            CapabilityStatus.VERSION_MISMATCH,
            CapabilityStatus.VERSION_MISMATCH,
        ),
        (
            frozenset(
                {
                    ServerRequestCategory.COMMAND_APPROVAL,
                    ServerRequestCategory.FILE_CHANGE_APPROVAL,
                }
            ),
            2,
            CapabilityStatus.VERSION_MISMATCH,
            CapabilityStatus.AVAILABLE,
        ),
    ],
)
async def test_server_request_categories_gate_states_without_raw_methods(
    tmp_path: Path,
    categories: frozenset[ServerRequestCategory],
    unclassified: int,
    server_status: CapabilityStatus,
    admission_status: CapabilityStatus,
) -> None:
    snapshot, _ = await discover(
        tmp_path,
        contract=make_contract(categories=categories, unclassified=unclassified),
    )

    assert snapshot.server_requests.status is server_status
    assert snapshot.agent_admission.status is admission_status
    assert snapshot.server_request_categories == categories
    assert snapshot.has_unclassified_server_requests is bool(unclassified)
    assert "raw-" not in repr(snapshot)


@pytest.mark.parametrize("semantic", sorted(AGENT_READINESS_METHODS))
async def test_missing_agent_execution_method_blocks_admission(
    tmp_path: Path,
    semantic: SemanticMethod,
) -> None:
    contract = make_contract(included=frozenset(SemanticMethod) - {semantic})

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "method_unavailable",
    )
    assert requester.params_for(_ALIASES[semantic]) == []
    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE


@pytest.mark.parametrize("semantic", sorted(AGENT_READINESS_METHODS))
async def test_invalid_agent_execution_method_blocks_admission(
    tmp_path: Path,
    semantic: SemanticMethod,
) -> None:
    invalid = ClientMethodContract(
        "AGENT_METHOD_SECRET",
        ParamsKind.OBJECT,
        frozenset({"AGENT_FIELD_SECRET"}),
    )
    contract = make_contract(overrides={semantic: invalid})

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
    assert requester.params_for("AGENT_METHOD_SECRET") == []
    rendered = repr(snapshot)
    assert "AGENT_METHOD_SECRET" not in rendered
    assert "AGENT_FIELD_SECRET" not in rendered


async def test_discovery_never_sends_agent_execution_methods(tmp_path: Path) -> None:
    snapshot, requester = await discover(tmp_path)

    called = {method for method, _params in requester.calls}
    assert called.isdisjoint({_ALIASES[semantic] for semantic in AGENT_READINESS_METHODS})
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_steer_capability_is_available_without_becoming_agent_readiness(
    tmp_path: Path,
) -> None:
    snapshot, requester = await discover(tmp_path)

    assert snapshot.steer == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert requester.params_for(_ALIASES[SemanticMethod.TURN_STEER]) == []
    assert SemanticMethod.TURN_STEER not in AGENT_READINESS_METHODS


async def test_missing_steer_is_optional_and_reported_unavailable(tmp_path: Path) -> None:
    contract = make_contract(
        included=frozenset(SemanticMethod) - {SemanticMethod.TURN_STEER},
    )

    snapshot, _ = await discover(tmp_path, contract=contract)

    assert snapshot.steer == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "method_unavailable",
    )
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")


async def test_malformed_steer_is_optional_and_reported_invalid(tmp_path: Path) -> None:
    contract = make_contract(
        overrides={
            SemanticMethod.TURN_STEER: ClientMethodContract(
                "STEER_METHOD_SECRET",
                ParamsKind.OBJECT,
                frozenset({"STEER_FIELD_SECRET"}),
            )
        },
    )

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert snapshot.steer == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert requester.params_for("STEER_METHOD_SECRET") == []
    assert "STEER_METHOD_SECRET" not in repr(snapshot)
    assert "STEER_FIELD_SECRET" not in repr(snapshot)


async def test_terminal_error_stops_later_requests(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_READ]] = CodexRpcProtocolError("TERMINAL_SECRET")

    snapshot, requester = await discover(tmp_path, actions=actions)

    assert [method for method, _params in requester.calls] == [
        _ALIASES[SemanticMethod.ACCOUNT_READ],
        _ALIASES[SemanticMethod.CONFIG_READ],
    ]
    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.policy_state.status is CapabilityStatus.ERROR
    assert snapshot.managed_requirements.status is CapabilityStatus.ERROR
    assert snapshot.realtime.status is CapabilityStatus.ERROR
    assert "TERMINAL_SECRET" not in repr(snapshot)


async def test_rpc_peer_invalid_json_stops_discovery_after_account(tmp_path: Path) -> None:
    reader = asyncio.StreamReader()
    writer = _PeerWriter()
    peer = RpcPeer(
        reader,
        cast("asyncio.StreamWriter", writer),
        request_timeout=1.0,
    )
    await peer.start()
    discovery = asyncio.create_task(
        CapabilityDiscovery(
            peer,
            working_directory=tmp_path,
            contract=make_contract(),
        ).discover()
    )
    account_request = await asyncio.wait_for(writer.written.get(), 1.0)
    reader.feed_data(
        json.dumps(
            {
                "id": account_request["id"],
                "result": {"account": {}, "requiresOpenaiAuth": True},
            },
            separators=(",", ":"),
        ).encode()
        + b"\n{INVALID_JSON_SECRET\n"
    )

    snapshot = await asyncio.wait_for(discovery, 1.0)

    assert account_request["method"] == _ALIASES[SemanticMethod.ACCOUNT_READ]
    assert writer.written.empty()
    assert snapshot.account == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.policy_state == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert snapshot.managed_requirements == CapabilityState(
        CapabilityStatus.ERROR,
        "probe_failed",
    )
    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.ERROR,
        "probe_failed",
    )
    assert snapshot.realtime == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert snapshot.interrupt == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert snapshot.steer == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert "INVALID_JSON_SECRET" not in repr(snapshot)
    await peer.close()


async def test_nonterminal_errors_continue_remaining_requests(tmp_path: Path) -> None:
    actions = happy_actions()
    actions[_ALIASES[SemanticMethod.CONFIG_READ]] = CodexRpcError("NONTERMINAL_SECRET")

    snapshot, requester = await discover(tmp_path, actions=actions)

    called = {method for method, _params in requester.calls}
    assert _ALIASES[SemanticMethod.CONFIG_REQUIREMENTS_READ] in called
    assert _ALIASES[SemanticMethod.EXPERIMENTAL_FEATURE_LIST] in called
    assert _ALIASES[SemanticMethod.REALTIME_VOICES_LIST] in called
    assert snapshot.realtime.status is CapabilityStatus.ERROR


@pytest.mark.parametrize(
    ("missing", "account_status", "realtime_status"),
    [
        (
            SemanticMethod.ACCOUNT_READ,
            CapabilityStatus.VERSION_MISMATCH,
            CapabilityStatus.AVAILABLE,
        ),
        (
            SemanticMethod.REALTIME_VOICES_LIST,
            CapabilityStatus.AVAILABLE,
            CapabilityStatus.VERSION_MISMATCH,
        ),
        (
            SemanticMethod.THREAD_REALTIME_START,
            CapabilityStatus.AVAILABLE,
            CapabilityStatus.VERSION_MISMATCH,
        ),
    ],
)
async def test_missing_voice_required_semantics_are_independent(
    tmp_path: Path,
    missing: SemanticMethod,
    account_status: CapabilityStatus,
    realtime_status: CapabilityStatus,
) -> None:
    contract = make_contract(included=frozenset(SemanticMethod) - {missing})

    snapshot, _ = await discover(tmp_path, contract=contract)

    assert snapshot.account.status is account_status
    assert snapshot.realtime.status is realtime_status


async def test_missing_realtime_start_contract_blocks_voice_before_runtime(
    tmp_path: Path,
) -> None:
    included = frozenset(SemanticMethod) - {SemanticMethod.THREAD_REALTIME_START}

    snapshot, requester = await discover(tmp_path, contract=make_contract(included=included))

    assert snapshot.realtime == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "method_unavailable",
    )
    assert requester.params_for(_ALIASES[SemanticMethod.THREAD_REALTIME_START]) == []


@pytest.mark.parametrize("semantic", list(SemanticMethod))
@pytest.mark.parametrize("invalid_part", ["name", "params_kind", "semantic_fields"])
async def test_inconsistent_manual_method_contract_is_not_invoked(
    tmp_path: Path,
    semantic: SemanticMethod,
    invalid_part: str,
) -> None:
    expected_kind = (
        ParamsKind.OMITTED
        if semantic is SemanticMethod.CONFIG_REQUIREMENTS_READ
        else ParamsKind.OBJECT
    )
    invalid_name = "MANUAL_METHOD_SECRET"
    invalid = ClientMethodContract(
        "" if invalid_part == "name" else invalid_name,
        (ParamsKind.OBJECT if expected_kind is ParamsKind.OMITTED else ParamsKind.OMITTED)
        if invalid_part == "params_kind"
        else expected_kind,
        frozenset({"MANUAL_FIELD_SECRET"})
        if invalid_part == "semantic_fields"
        else _FIELDS[semantic],
    )
    contract = make_contract(overrides={semantic: invalid})

    snapshot, requester = await discover(tmp_path, contract=contract)

    affected_state = {
        SemanticMethod.ACCOUNT_READ: snapshot.account,
        SemanticMethod.CONFIG_READ: snapshot.policy_state,
        SemanticMethod.CONFIG_REQUIREMENTS_READ: snapshot.managed_requirements,
        SemanticMethod.EXPERIMENTAL_FEATURE_LIST: snapshot.realtime,
        SemanticMethod.REALTIME_VOICES_LIST: snapshot.realtime,
        SemanticMethod.THREAD_START: snapshot.agent_admission,
        SemanticMethod.THREAD_REALTIME_START: snapshot.realtime,
        SemanticMethod.TURN_START: snapshot.agent_admission,
        SemanticMethod.TURN_STEER: snapshot.steer,
        SemanticMethod.TURN_INTERRUPT: snapshot.interrupt,
    }[semantic]
    assert affected_state == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
    assert requester.params_for(invalid_name) == []
    assert "MANUAL_METHOD_SECRET" not in repr(snapshot)
    assert "MANUAL_FIELD_SECRET" not in repr(snapshot)


async def test_a_hostile_client_method_name_is_not_invoked(tmp_path: Path) -> None:
    hostile = HostileMethodName(_ALIASES[SemanticMethod.ACCOUNT_READ])
    contract = make_contract(
        overrides={
            SemanticMethod.ACCOUNT_READ: ClientMethodContract(
                hostile,
                ParamsKind.OBJECT,
                _FIELDS[SemanticMethod.ACCOUNT_READ],
            )
        }
    )

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert all(method != _ALIASES[SemanticMethod.ACCOUNT_READ] for method, _ in requester.calls)
    assert snapshot.account == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )


@pytest.mark.parametrize("hostile_location", ["server", "profile", "version"])
async def test_a_hostile_server_profile_or_version_text_fails_before_rpc(
    tmp_path: Path,
    hostile_location: str,
) -> None:
    hostile = HostileMethodName("raw/command")
    base = make_contract()
    contract = SimpleNamespace(
        version=hostile if hostile_location == "version" else "codex-fixture",
        methods=base.methods,
        method=base.method,
        server_requests=(
            {ServerRequestCategory.COMMAND_APPROVAL: frozenset({hostile})}
            if hostile_location in {"server", "version"}
            else {ServerRequestCategory.COMMAND_APPROVAL: frozenset({"raw/command"})}
        ),
        unclassified_server_request_count=0,
        experimental_schema=True,
        approval_profiles=(
            {}
            if hostile_location in {"server", "version"}
            else {hostile: approval_profile(ServerRequestCategory.COMMAND_APPROVAL)}
        ),
    )

    requester = FakeRequester(happy_actions())
    snapshot = await CapabilityDiscovery(
        requester,
        working_directory=tmp_path,
        contract=cast("CodexProtocolContract", contract),
    ).discover()

    assert requester.calls == []
    if hostile_location == "version":
        assert snapshot.version == ""
    assert snapshot.account == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )


@pytest.mark.parametrize(
    "contract",
    [
        CodexProtocolContract(
            version="codex-fixture",
            methods={**make_contract().methods, "METHOD_KEY_SECRET": object()},  # type: ignore[dict-item]
            server_requests=make_contract().server_requests,
            unclassified_server_request_count=0,
            experimental_schema=True,
        ),
        CodexProtocolContract(
            version="codex-fixture",
            methods=make_contract().methods,
            server_requests={"SERVER_KEY_SECRET": frozenset({"RAW_METHOD_SECRET"})},  # type: ignore[dict-item]
            unclassified_server_request_count=0,
            experimental_schema=True,
        ),
        CodexProtocolContract(
            version="codex-fixture",
            methods=make_contract().methods,
            server_requests={
                ServerRequestCategory.COMMAND_APPROVAL: frozenset({"", 7}),  # type: ignore[arg-type]
            },
            unclassified_server_request_count=0,
            experimental_schema=True,
        ),
        CodexProtocolContract(
            version="codex-fixture",
            methods=make_contract().methods,
            server_requests={
                ServerRequestCategory.COMMAND_APPROVAL: frozenset({"SHARED_RAW_METHOD_SECRET"}),
                ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset({"SHARED_RAW_METHOD_SECRET"}),
            },
            unclassified_server_request_count=0,
            experimental_schema=True,
        ),
        CodexProtocolContract(
            version="codex-fixture",
            methods=make_contract().methods,
            server_requests={
                ServerRequestCategory.COMMAND_APPROVAL: frozenset(
                    {"raw/command", "SHARED_RAW_METHOD_SECRET"}
                ),
                ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset(
                    {"raw/file-change", "SHARED_RAW_METHOD_SECRET"}
                ),
            },
            unclassified_server_request_count=0,
            experimental_schema=True,
        ),
        CodexProtocolContract(
            version="codex-fixture",
            methods=make_contract().methods,
            server_requests=make_contract().server_requests,
            unclassified_server_request_count=-1,
            experimental_schema=True,
        ),
        CodexProtocolContract(
            version="codex-fixture",
            methods=make_contract().methods,
            server_requests=make_contract().server_requests,
            unclassified_server_request_count=True,
            experimental_schema=True,
        ),
    ],
)
async def test_invalid_manual_contract_metadata_fails_closed_without_leaking_raw_values(
    tmp_path: Path,
    contract: CodexProtocolContract,
) -> None:
    requester = FakeRequester(happy_actions())

    snapshot = await CapabilityDiscovery(
        requester,
        working_directory=tmp_path,
        contract=contract,
    ).discover()

    assert requester.calls == []
    states = (
        snapshot.account,
        snapshot.policy_state,
        snapshot.managed_requirements,
        snapshot.agent_admission,
        snapshot.realtime,
        snapshot.interrupt,
        snapshot.steer,
        snapshot.server_requests,
    )
    assert all(
        state == CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response")
        for state in states
    )
    assert snapshot.server_request_categories == frozenset()
    rendered = repr(snapshot)
    assert "METHOD_KEY_SECRET" not in rendered
    assert "SERVER_KEY_SECRET" not in rendered
    assert "RAW_METHOD_SECRET" not in rendered
    assert "SHARED_RAW_METHOD_SECRET" not in rendered


def mixed_alias_contract(
    *,
    profiles: dict[str, ApprovalProfile],
) -> CodexProtocolContract:
    """A build advertising both the new and the legacy alias of each required family."""
    return CodexProtocolContract(
        version="codex-fixture",
        methods=make_contract().methods,
        server_requests={
            ServerRequestCategory.COMMAND_APPROVAL: frozenset(
                {"execCommandApproval", "item/commandExecution/requestApproval"}
            ),
            ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset(
                {"applyPatchApproval", "item/fileChange/requestApproval"}
            ),
        },
        unclassified_server_request_count=0,
        experimental_schema=True,
        approval_profiles=profiles,
        agent_event_profile=make_contract().agent_event_profile,
        file_change_patch_profile=_PATCH_PROFILE,
    )


async def test_an_unprofiled_alias_inside_a_required_category_is_not_admissible(
    tmp_path: Path,
) -> None:
    """Which advertised alias a live turn sends is unverified, so every one must be readable."""
    contract = mixed_alias_contract(
        profiles={
            "item/commandExecution/requestApproval": approval_profile(
                ServerRequestCategory.COMMAND_APPROVAL
            ),
            "item/fileChange/requestApproval": approval_profile(
                ServerRequestCategory.FILE_CHANGE_APPROVAL
            ),
        }
    )

    snapshot, _ = await discover(tmp_path, contract=contract)

    unadaptable = CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "approval_family_unadaptable",
    )
    assert snapshot.server_requests == unadaptable
    assert snapshot.agent_admission == unadaptable
    assert snapshot.server_request_categories == frozenset(
        {
            ServerRequestCategory.COMMAND_APPROVAL,
            ServerRequestCategory.FILE_CHANGE_APPROVAL,
        }
    )


async def test_every_advertised_alias_profiled_stays_admissible(tmp_path: Path) -> None:
    """A category may advertise several aliases when this build can be read as each of them."""
    contract = mixed_alias_contract(
        profiles={
            "item/commandExecution/requestApproval": approval_profile(
                ServerRequestCategory.COMMAND_APPROVAL
            ),
            "execCommandApproval": approval_profile(ServerRequestCategory.COMMAND_APPROVAL),
            "item/fileChange/requestApproval": approval_profile(
                ServerRequestCategory.FILE_CHANGE_APPROVAL
            ),
            "applyPatchApproval": approval_profile(ServerRequestCategory.FILE_CHANGE_APPROVAL),
        }
    )

    snapshot, _ = await discover(tmp_path, contract=contract)

    assert snapshot.server_requests == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.server_request_categories == frozenset(
        {
            ServerRequestCategory.COMMAND_APPROVAL,
            ServerRequestCategory.FILE_CHANGE_APPROVAL,
        }
    )


@pytest.mark.parametrize(
    "terminal_semantic",
    [
        SemanticMethod.CONFIG_REQUIREMENTS_READ,
        SemanticMethod.EXPERIMENTAL_FEATURE_LIST,
        SemanticMethod.REALTIME_VOICES_LIST,
    ],
)
async def test_terminal_error_after_policy_forces_agent_admission_error(
    tmp_path: Path,
    terminal_semantic: SemanticMethod,
) -> None:
    actions = happy_actions()
    actions[_ALIASES[terminal_semantic]] = CodexRpcProtocolError("TERMINAL_METADATA_SECRET")

    snapshot, requester = await discover(tmp_path, actions=actions)

    assert snapshot.account.status is CapabilityStatus.AVAILABLE
    assert snapshot.policy_state.status is CapabilityStatus.AVAILABLE
    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.ERROR,
        "probe_failed",
    )
    called = [method for method, _params in requester.calls]
    assert called[-1] == _ALIASES[terminal_semantic]
    assert "TERMINAL_METADATA_SECRET" not in repr(snapshot)


async def test_contract_probe_runs_once_and_snapshot_is_immutable(tmp_path: Path) -> None:
    probe = FakeContractProbe(make_contract())
    requester = FakeRequester(happy_actions())

    snapshot = await CapabilityDiscovery(
        requester,
        working_directory=tmp_path,
        contract_probe=probe,
    ).discover()

    assert probe.calls == 1
    assert isinstance(snapshot.server_request_categories, frozenset)
    with pytest.raises(FrozenInstanceError):
        snapshot.version = "changed"  # type: ignore[misc]


async def test_contract_probe_failure_returns_bounded_error_snapshot(tmp_path: Path) -> None:
    probe = FakeContractProbe(CodexSchemaError("PROBE_SCHEMA_SECRET"))
    requester = FakeRequester(happy_actions())

    snapshot = await CapabilityDiscovery(
        requester,
        working_directory=tmp_path,
        contract_probe=probe,
    ).discover()

    assert probe.calls == 1
    assert requester.calls == []
    assert snapshot.version == ""
    assert snapshot.account == CapabilityState(CapabilityStatus.ERROR, "probe_failed")
    assert snapshot.realtime.status is CapabilityStatus.ERROR
    assert "PROBE_SCHEMA_SECRET" not in repr(snapshot)


def test_constructor_requires_absolute_path_and_exactly_one_contract(tmp_path: Path) -> None:
    requester = FakeRequester(happy_actions())
    contract = make_contract()

    with pytest.raises(ValueError, match="exactly one"):
        CapabilityDiscovery(requester, working_directory=tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        CapabilityDiscovery(
            requester,
            working_directory=tmp_path,
            contract=contract,
            contract_probe=FakeContractProbe(contract),
        )
    with pytest.raises(ValueError, match="absolute"):
        CapabilityDiscovery(
            requester,
            working_directory=Path("relative"),
            contract=contract,
        )


async def test_advertised_but_unadaptable_approval_family_is_not_stage_b_ready(
    tmp_path: Path,
) -> None:
    """A build whose only approval families moco cannot read must fail before a turn."""
    contract = make_contract(profiles={})

    snapshot, _ = await discover(tmp_path, contract=contract)

    unadaptable = CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "approval_family_unadaptable",
    )
    assert snapshot.server_requests == unadaptable
    assert snapshot.agent_admission == unadaptable
    assert snapshot.server_request_categories >= STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES


async def test_one_category_left_unadaptable_is_not_stage_b_ready(tmp_path: Path) -> None:
    command = raw_method(ServerRequestCategory.COMMAND_APPROVAL)
    contract = make_contract(
        profiles={command: approval_profile(ServerRequestCategory.COMMAND_APPROVAL)}
    )

    snapshot, _ = await discover(tmp_path, contract=contract)

    assert snapshot.server_requests.status is CapabilityStatus.VERSION_MISMATCH
    assert snapshot.agent_admission.status is CapabilityStatus.VERSION_MISMATCH


async def test_a_profile_for_a_method_no_category_advertises_fails_closed(
    tmp_path: Path,
) -> None:
    profiles = adaptable_profiles(STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES)
    profiles["UNADVERTISED_RAW_METHOD_SECRET"] = approval_profile(
        ServerRequestCategory.COMMAND_APPROVAL
    )
    contract = make_contract(profiles=profiles)

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert requester.calls == []
    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
    assert "UNADVERTISED_RAW_METHOD_SECRET" not in repr(snapshot)


async def test_a_profile_that_is_not_a_profile_fails_closed(tmp_path: Path) -> None:
    contract = make_contract(
        profiles=cast(
            "dict[str, ApprovalProfile]",
            {raw_method(ServerRequestCategory.COMMAND_APPROVAL): "PROFILE_VALUE_SECRET"},
        )
    )

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert requester.calls == []
    assert snapshot.server_requests == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
    assert "PROFILE_VALUE_SECRET" not in repr(snapshot)


async def test_a_profile_answering_another_category_fails_closed(tmp_path: Path) -> None:
    profiles = adaptable_profiles(STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES)
    profiles[raw_method(ServerRequestCategory.COMMAND_APPROVAL)] = approval_profile(
        ServerRequestCategory.FILE_CHANGE_APPROVAL
    )
    contract = make_contract(profiles=profiles)

    snapshot, requester = await discover(tmp_path, contract=contract)

    assert requester.calls == []
    assert snapshot.agent_admission == CapabilityState(
        CapabilityStatus.VERSION_MISMATCH,
        "invalid_response",
    )
