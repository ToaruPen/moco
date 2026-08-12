from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from moco.codex.schema import (
    AGENT_READINESS_METHODS,
    STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES,
    AgentEventProfile,
    ApprovalProfile,
    ClientMethodContract,
    CodexProtocolContract,
    ParamsKind,
    SemanticMethod,
    ServerRequestCategory,
    _is_transport_safe,
)
from moco.errors import (
    CodexProcessExitedError,
    CodexRpcError,
    CodexRpcProtocolError,
    CodexSchemaError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Protocol

    from moco.codex.rpc import JsonValue

    class _Requester(Protocol):
        async def request(
            self,
            method: str,
            params: Mapping[str, JsonValue] | None = None,
            *,
            request_timeout: float | None = None,
        ) -> JsonValue: ...

    class _ContractProbe(Protocol):
        async def probe(self) -> CodexProtocolContract: ...


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    DISABLED = "disabled"
    AUTHENTICATION_REQUIRED = "authentication_required"
    VERSION_MISMATCH = "version_mismatch"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CapabilityState:
    status: CapabilityStatus
    detail: str


class SandboxMode(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalMode(StrEnum):
    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"
    GRANULAR = "granular"


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    sandbox: SandboxMode
    approval: ApprovalMode


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    version: str
    account: CapabilityState
    effective_policy: EffectivePolicy | None
    policy_state: CapabilityState
    managed_requirements: CapabilityState
    agent_admission: CapabilityState
    realtime: CapabilityState
    interrupt: CapabilityState
    steer: CapabilityState
    server_requests: CapabilityState
    server_request_categories: frozenset[ServerRequestCategory]
    has_unclassified_server_requests: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "server_request_categories",
            frozenset(self.server_request_categories),
        )


_AVAILABLE = CapabilityState(CapabilityStatus.AVAILABLE, "ready")
_METHOD_UNAVAILABLE = CapabilityState(CapabilityStatus.VERSION_MISMATCH, "method_unavailable")
_INVALID_RESPONSE = CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response")
_PROBE_FAILED = CapabilityState(CapabilityStatus.ERROR, "probe_failed")
_AUTHENTICATION_REQUIRED = CapabilityState(
    CapabilityStatus.AUTHENTICATION_REQUIRED,
    "authentication_required",
)
_FEATURE_DISABLED = CapabilityState(CapabilityStatus.DISABLED, "feature_disabled")
_NO_VOICE = CapabilityState(CapabilityStatus.DISABLED, "no_voice")
_APPROVAL_CATEGORIES_UNAVAILABLE = CapabilityState(
    CapabilityStatus.VERSION_MISMATCH,
    "approval_categories_unavailable",
)
# The build advertises the required approval requests, but the readiness axis being checked
# cannot safely adapt every family. Agent admission separately distinguishes an unreadable
# request profile from optional explanation evidence that only degrades local review.
_APPROVAL_FAMILY_UNADAPTABLE = CapabilityState(
    CapabilityStatus.VERSION_MISMATCH,
    "approval_family_unadaptable",
)
_UNCLASSIFIED_SERVER_REQUESTS = CapabilityState(
    CapabilityStatus.VERSION_MISMATCH,
    "unclassified_server_requests",
)
_UNSAFE_VOICE_POLICY = CapabilityState(CapabilityStatus.DISABLED, "unsafe_voice_policy")
_AGENT_EVENT_CONTRACT_UNAVAILABLE = CapabilityState(
    CapabilityStatus.VERSION_MISMATCH,
    "agent_event_contract_unavailable",
)
_MAX_FEATURE_PAGES = 32
_TERMINAL_ERRORS = (CodexProcessExitedError, CodexRpcProtocolError)

_EXPECTED_METHODS: dict[SemanticMethod, tuple[ParamsKind, frozenset[str]]] = {
    SemanticMethod.ACCOUNT_READ: (ParamsKind.OBJECT, frozenset()),
    SemanticMethod.CONFIG_READ: (ParamsKind.OBJECT, frozenset({"cwd"})),
    SemanticMethod.CONFIG_REQUIREMENTS_READ: (ParamsKind.OMITTED, frozenset()),
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: (
        ParamsKind.OBJECT,
        frozenset({"cursor"}),
    ),
    SemanticMethod.REALTIME_VOICES_LIST: (ParamsKind.OBJECT, frozenset()),
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


class _FeatureResult(StrEnum):
    NEUTRAL = "neutral"
    DISABLED = "disabled"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class _ContractValidation:
    invalid_methods: frozenset[SemanticMethod]
    categories: frozenset[ServerRequestCategory]
    adaptable_categories: frozenset[ServerRequestCategory]
    reviewable_categories: frozenset[ServerRequestCategory]
    has_unclassified: bool


class CapabilityDiscovery:
    def __init__(
        self,
        rpc: _Requester,
        *,
        working_directory: Path,
        contract: CodexProtocolContract | None = None,
        contract_probe: _ContractProbe | None = None,
    ) -> None:
        if (contract is None) == (contract_probe is None):
            msg = "exactly one Codex protocol contract source is required"
            raise ValueError(msg)
        if not working_directory.is_absolute():
            msg = "working directory must be absolute"
            raise ValueError(msg)
        self._rpc = rpc
        self._working_directory = working_directory
        self._contract = contract
        self._contract_probe = contract_probe

    async def discover(self) -> CapabilitySnapshot:  # noqa: PLR0911
        contract = self._contract
        if contract is None:
            try:
                probe = self._contract_probe
                if probe is None:  # pragma: no cover - constructor invariant
                    return _error_snapshot()
                contract = await probe.probe()
            except CodexSchemaError:
                return _error_snapshot()

        validation = _validate_contract(contract)
        if validation is None:
            raw_version = cast("object", contract.version)
            version = (
                raw_version if type(raw_version) is str and _is_transport_safe(raw_version) else ""
            )
            return _invalid_contract_snapshot(version)

        categories = validation.categories
        has_unclassified = validation.has_unclassified
        server_requests = _server_request_state(validation)

        account, terminal = await self._probe_account(contract, validation)
        if terminal:
            return _snapshot_after_terminal(
                contract,
                account=account,
                categories=categories,
                has_unclassified=has_unclassified,
                server_requests=server_requests,
            )

        effective_policy, policy_state, terminal = await self._probe_policy(contract, validation)
        if terminal:
            return _snapshot_after_terminal(
                contract,
                account=account,
                policy_state=policy_state,
                categories=categories,
                has_unclassified=has_unclassified,
                server_requests=server_requests,
            )

        managed_requirements, terminal = await self._probe_requirements(contract, validation)
        if terminal:
            return _snapshot_after_terminal(
                contract,
                account=account,
                effective_policy=effective_policy,
                policy_state=policy_state,
                managed_requirements=managed_requirements,
                categories=categories,
                has_unclassified=has_unclassified,
                server_requests=server_requests,
            )

        feature_result, terminal = await self._probe_features(contract, validation)
        if terminal:
            return _snapshot_after_terminal(
                contract,
                account=account,
                effective_policy=effective_policy,
                policy_state=policy_state,
                managed_requirements=managed_requirements,
                categories=categories,
                has_unclassified=has_unclassified,
                server_requests=server_requests,
            )

        voices, terminal = await self._probe_voices(contract, validation)
        realtime = _realtime_state(voices, feature_result)
        interrupt = _interrupt_state(contract, validation) if not terminal else _PROBE_FAILED
        steer = _steer_state(contract, validation) if not terminal else _PROBE_FAILED
        admission = (
            _PROBE_FAILED
            if terminal
            else _agent_admission(
                contract,
                validation,
                account,
                effective_policy,
                policy_state,
            )
        )
        return CapabilitySnapshot(
            version=contract.version,
            account=account,
            effective_policy=effective_policy,
            policy_state=policy_state,
            managed_requirements=managed_requirements,
            agent_admission=admission,
            realtime=realtime,
            interrupt=interrupt,
            steer=steer,
            server_requests=server_requests,
            server_request_categories=categories,
            has_unclassified_server_requests=has_unclassified,
        )

    async def _probe_account(
        self,
        contract: CodexProtocolContract,
        validation: _ContractValidation,
    ) -> tuple[CapabilityState, bool]:
        method, unavailable = _method_contract(
            contract,
            SemanticMethod.ACCOUNT_READ,
            validation,
        )
        if method is None:
            return unavailable, False
        try:
            response = await self._rpc.request(method.name, {})
        except _TERMINAL_ERRORS:
            return _PROBE_FAILED, True
        except CodexRpcError:
            return _PROBE_FAILED, False
        return _parse_account(response), False

    async def _probe_policy(
        self,
        contract: CodexProtocolContract,
        validation: _ContractValidation,
    ) -> tuple[EffectivePolicy | None, CapabilityState, bool]:
        method, unavailable = _method_contract(
            contract,
            SemanticMethod.CONFIG_READ,
            validation,
        )
        if method is None:
            return None, unavailable, False
        try:
            response = await self._rpc.request(
                method.name,
                {"cwd": str(self._working_directory)},
            )
        except _TERMINAL_ERRORS:
            return None, _PROBE_FAILED, True
        except CodexRpcError:
            return None, _PROBE_FAILED, False
        policy = _parse_policy(response)
        return policy, _AVAILABLE if policy is not None else _INVALID_RESPONSE, False

    async def _probe_requirements(
        self,
        contract: CodexProtocolContract,
        validation: _ContractValidation,
    ) -> tuple[CapabilityState, bool]:
        method, unavailable = _method_contract(
            contract,
            SemanticMethod.CONFIG_REQUIREMENTS_READ,
            validation,
        )
        if method is None:
            return unavailable, False
        try:
            response = await self._rpc.request(method.name)
        except _TERMINAL_ERRORS:
            return _PROBE_FAILED, True
        except CodexRpcError:
            return _PROBE_FAILED, False
        return (_AVAILABLE if isinstance(response, dict) else _INVALID_RESPONSE), False

    async def _probe_features(  # noqa: PLR0911
        self,
        contract: CodexProtocolContract,
        validation: _ContractValidation,
    ) -> tuple[_FeatureResult, bool]:
        method, _unavailable = _method_contract(
            contract,
            SemanticMethod.EXPERIMENTAL_FEATURE_LIST,
            validation,
        )
        if method is None:
            if SemanticMethod.EXPERIMENTAL_FEATURE_LIST in validation.invalid_methods:
                return _FeatureResult.INVALID, False
            return _FeatureResult.NEUTRAL, False
        cursor: str | None = None
        seen_cursors: set[str] = set()
        disabled_seen = False
        for _page in range(_MAX_FEATURE_PAGES):
            try:
                response = await self._rpc.request(method.name, {"cursor": cursor})
            except _TERMINAL_ERRORS:
                return _FeatureResult.NEUTRAL, True
            except CodexRpcError:
                result = _FeatureResult.DISABLED if disabled_seen else _FeatureResult.NEUTRAL
                return result, False
            parsed = _parse_feature_page(response)
            if parsed is None:
                result = _FeatureResult.DISABLED if disabled_seen else _FeatureResult.INVALID
                return result, False
            page_disabled, next_cursor = parsed
            disabled_seen = disabled_seen or page_disabled
            if next_cursor is None:
                result = _FeatureResult.DISABLED if disabled_seen else _FeatureResult.NEUTRAL
                return result, False
            if next_cursor in seen_cursors:
                result = _FeatureResult.DISABLED if disabled_seen else _FeatureResult.INVALID
                return result, False
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        result = _FeatureResult.DISABLED if disabled_seen else _FeatureResult.INVALID
        return result, False

    async def _probe_voices(
        self,
        contract: CodexProtocolContract,
        validation: _ContractValidation,
    ) -> tuple[CapabilityState, bool]:
        method, unavailable = _method_contract(
            contract,
            SemanticMethod.REALTIME_VOICES_LIST,
            validation,
        )
        if method is None:
            return unavailable, False
        try:
            response = await self._rpc.request(method.name, {})
        except _TERMINAL_ERRORS:
            return _PROBE_FAILED, True
        except CodexRpcError:
            return _PROBE_FAILED, False
        return _parse_voices(response), False


def _method_contract(
    contract: CodexProtocolContract,
    semantic: SemanticMethod,
    validation: _ContractValidation,
) -> tuple[ClientMethodContract | None, CapabilityState]:
    if semantic in validation.invalid_methods:
        return None, _INVALID_RESPONSE
    method = contract.method(semantic)
    if method is None:
        return None, _METHOD_UNAVAILABLE
    return method, _AVAILABLE


def _validate_contract(contract: CodexProtocolContract) -> _ContractValidation | None:
    version = cast("object", contract.version)
    experimental_schema = cast("object", contract.experimental_schema)
    if (
        type(version) is not str
        or not version
        or not _is_transport_safe(version)
        or type(experimental_schema) is not bool
        or any(
            type(value) not in (dict, MappingProxyType)
            for value in (
                contract.methods,
                contract.server_requests,
                contract.approval_profiles,
            )
        )
    ):
        return None

    invalid_methods: set[SemanticMethod] = set()
    methods = cast("Mapping[object, object]", contract.methods)
    for semantic, method in methods.items():
        if not isinstance(semantic, SemanticMethod):
            return None
        expected_kind, expected_fields = _EXPECTED_METHODS[semantic]
        if (
            type(method) is not ClientMethodContract
            or type(method.name) is not str
            or not method.name
            or not _is_transport_safe(method.name)
            or method.params_kind is not expected_kind
            or type(method.semantic_fields) is not frozenset
            or any(
                type(field) is not str or not field or not _is_transport_safe(field)
                for field in method.semantic_fields
            )
            or method.semantic_fields != expected_fields
        ):
            invalid_methods.add(semantic)

    server_requests = _validate_server_requests(contract)
    if server_requests is None:
        return None

    count = cast("object", contract.unclassified_server_request_count)
    if type(count) is not int or count < 0:
        return None
    categories, adaptable, reviewable = server_requests
    return _ContractValidation(
        invalid_methods=frozenset(invalid_methods),
        categories=categories,
        adaptable_categories=adaptable,
        reviewable_categories=reviewable,
        has_unclassified=count > 0,
    )


def _validate_server_requests(
    contract: CodexProtocolContract,
) -> (
    tuple[
        frozenset[ServerRequestCategory],
        frozenset[ServerRequestCategory],
        frozenset[ServerRequestCategory],
    ]
    | None
):
    """Return advertised, adaptable, and fully reviewable request categories.

    A raw server method owned by two categories would route ambiguously, and a profile that
    names no advertised method or answers another category is not a contract any probe
    produces, so a manual contract carrying either is rejected before any RPC.

    A category counts as readable only when every method it advertises has a profile. The
    build chooses which alias it sends, so a readable alias beside an unreadable one leaves
    the family unanswerable in exactly the turns the unreadable one is used.
    """
    categories: set[ServerRequestCategory] = set()
    owners: dict[str, ServerRequestCategory] = {}
    server_requests = cast("Mapping[object, object]", contract.server_requests)
    for category, names in server_requests.items():
        if (
            type(category) is not ServerRequestCategory
            or type(names) is not frozenset
            or not names
            or any(type(name) is not str or not _is_transport_safe(name) for name in names)
        ):
            return None
        raw_names = cast("frozenset[str]", names)
        if not owners.keys().isdisjoint(raw_names):
            return None
        owners.update(dict.fromkeys(raw_names, category))
        categories.add(category)

    profiled: set[str] = set()
    profiles = cast("Mapping[object, object]", contract.approval_profiles)
    for method, profile in profiles.items():
        if (
            type(method) is not str
            or not method
            or not _is_transport_safe(method)
            or type(profile) is not ApprovalProfile
        ):
            return None
        if owners.get(method) is not profile.category:
            return None
        profiled.add(method)
    adaptable = {
        category
        for category in categories
        if all(name in profiled for name, owner in owners.items() if owner is category)
    }
    reviewable = set(adaptable)
    file_category = ServerRequestCategory.FILE_CHANGE_APPROVAL
    file_methods = [method for method, owner in owners.items() if owner is file_category]
    if (
        file_category in reviewable
        and contract.file_change_patch_profile is None
        and any(
            cast("ApprovalProfile", profiles[method]).changes_member is None
            for method in file_methods
        )
    ):
        # Modern file approvals name only the correlated item. Without the separately
        # generated patch notification profile, moco cannot explain which files accepting
        # would affect. A legacy family carries its own changes and needs no such evidence.
        reviewable.remove(file_category)
    return frozenset(categories), frozenset(adaptable), frozenset(reviewable)


def _parse_account(response: object) -> CapabilityState:
    if not isinstance(response, dict):
        return _INVALID_RESPONSE
    account = response.get("account", _MISSING)
    requires_auth = response.get("requiresOpenaiAuth", _MISSING)
    if isinstance(account, dict) or requires_auth is False:
        return _AVAILABLE
    if requires_auth is True and (account is _MISSING or account is None):
        return _AUTHENTICATION_REQUIRED
    return _INVALID_RESPONSE


def _parse_policy(response: object) -> EffectivePolicy | None:
    if not isinstance(response, dict):
        return None
    config = response.get("config")
    if not isinstance(config, dict):
        return None
    sandbox_raw = config.get("sandbox_mode")
    approval_raw = config.get("approval_policy")
    if not isinstance(sandbox_raw, str):
        return None
    try:
        sandbox = SandboxMode(sandbox_raw)
    except ValueError:
        return None
    approval = _parse_approval(approval_raw)
    if approval is None:
        return None
    return EffectivePolicy(sandbox, approval)


def _parse_approval(value: object) -> ApprovalMode | None:
    if isinstance(value, str):
        try:
            approval = ApprovalMode(value)
        except ValueError:
            return None
        return approval if approval is not ApprovalMode.GRANULAR else None
    if not isinstance(value, dict) or set(value) != {"granular"}:
        return None
    granular = value["granular"]
    if (
        not isinstance(granular, dict)
        or not granular
        or not all(isinstance(key, str) and type(item) is bool for key, item in granular.items())
    ):
        return None
    return ApprovalMode.GRANULAR


def _parse_feature_page(response: object) -> tuple[bool, str | None] | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    next_cursor = response.get("nextCursor")
    if not isinstance(data, list) or not (next_cursor is None or isinstance(next_cursor, str)):
        return None
    disabled = False
    for entry in data:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        enabled = entry.get("enabled")
        if not isinstance(name, str) or type(enabled) is not bool:
            return None
        if name == "realtime_conversation" and not enabled:
            disabled = True
    return disabled, next_cursor


def _parse_voices(response: object) -> CapabilityState:
    if not isinstance(response, dict):
        return _INVALID_RESPONSE
    voices = response.get("voices")
    if not isinstance(voices, dict):
        return _INVALID_RESPONSE
    has_voice = False
    for group, value in voices.items():
        if not isinstance(group, str):
            return _INVALID_RESPONSE
        if isinstance(value, str):
            if not value:
                return _INVALID_RESPONSE
            has_voice = True
            continue
        if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
            return _INVALID_RESPONSE
        has_voice = has_voice or any(bool(name) for name in value)
    return _AVAILABLE if has_voice else _NO_VOICE


def _interrupt_state(
    contract: CodexProtocolContract,
    validation: _ContractValidation,
) -> CapabilityState:
    method, unavailable = _method_contract(
        contract,
        SemanticMethod.TURN_INTERRUPT,
        validation,
    )
    return _AVAILABLE if method is not None else unavailable


def _steer_state(
    contract: CodexProtocolContract,
    validation: _ContractValidation,
) -> CapabilityState:
    method, unavailable = _method_contract(
        contract,
        SemanticMethod.TURN_STEER,
        validation,
    )
    return _AVAILABLE if method is not None else unavailable


def _realtime_state(voices: CapabilityState, feature: _FeatureResult) -> CapabilityState:
    if voices.status is not CapabilityStatus.AVAILABLE:
        return voices
    if feature is _FeatureResult.DISABLED:
        return _FEATURE_DISABLED
    if feature is _FeatureResult.INVALID:
        return _INVALID_RESPONSE
    return _AVAILABLE


def _server_request_state(validation: _ContractValidation) -> CapabilityState:
    # Advertising the two approval requests is not the same as being able to read them, so
    # local-review readiness also requires the optional explanation evidence for a modern
    # file approval whose own request does not carry its changes.
    approvals = _approval_readiness(validation, validation.reviewable_categories)
    if approvals is not None:
        return approvals
    if validation.has_unclassified:
        return _UNCLASSIFIED_SERVER_REQUESTS
    return _AVAILABLE


def _agent_execution_readiness(
    contract: CodexProtocolContract,
    validation: _ContractValidation,
) -> CapabilityState:
    if type(contract.agent_event_profile) is not AgentEventProfile:
        return _AGENT_EVENT_CONTRACT_UNAVAILABLE
    for semantic in sorted(AGENT_READINESS_METHODS):
        method, unavailable = _method_contract(contract, semantic, validation)
        if method is None:
            return unavailable
    return _AVAILABLE


def _agent_admission(
    contract: CodexProtocolContract,
    validation: _ContractValidation,
    account: CapabilityState,
    effective_policy: EffectivePolicy | None,
    policy_state: CapabilityState,
) -> CapabilityState:
    readiness = _agent_execution_readiness(contract, validation)
    if readiness.status is not CapabilityStatus.AVAILABLE:
        return readiness
    if account.status is not CapabilityStatus.AVAILABLE:
        return account
    if policy_state.status is not CapabilityStatus.AVAILABLE or effective_policy is None:
        return policy_state
    # A prompt moco could not read would arrive mid-turn with nothing safe to answer, so an
    # unadaptable approval family stops the turn here rather than at the prompt.
    approvals = _approval_readiness(validation, validation.adaptable_categories)
    if approvals is not None:
        return approvals
    if is_unsafe_voice_policy(effective_policy):
        return _UNSAFE_VOICE_POLICY
    return _AVAILABLE


def is_unsafe_voice_policy(policy: EffectivePolicy | None) -> bool:
    """Use one canonical safety predicate at discovery and Agent's wire boundary."""
    return (
        type(policy) is EffectivePolicy
        and type(policy.sandbox) is SandboxMode
        and type(policy.approval) is ApprovalMode
        and policy.sandbox is SandboxMode.DANGER_FULL_ACCESS
        and policy.approval is ApprovalMode.NEVER
    )


def _approval_readiness(
    validation: _ContractValidation,
    adaptable_categories: frozenset[ServerRequestCategory],
) -> CapabilityState | None:
    """Report why the Stage B approvals are not usable, or nothing when they are."""
    if not validation.categories >= STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES:
        return _APPROVAL_CATEGORIES_UNAVAILABLE
    if not adaptable_categories >= STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES:
        return _APPROVAL_FAMILY_UNADAPTABLE
    return None


def _snapshot_after_terminal(
    contract: CodexProtocolContract,
    *,
    account: CapabilityState,
    categories: frozenset[ServerRequestCategory],
    has_unclassified: bool,
    server_requests: CapabilityState,
    effective_policy: EffectivePolicy | None = None,
    policy_state: CapabilityState = _PROBE_FAILED,
    managed_requirements: CapabilityState = _PROBE_FAILED,
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        version=contract.version,
        account=account,
        effective_policy=effective_policy,
        policy_state=policy_state,
        managed_requirements=managed_requirements,
        agent_admission=_PROBE_FAILED,
        realtime=_PROBE_FAILED,
        interrupt=_PROBE_FAILED,
        steer=_PROBE_FAILED,
        server_requests=server_requests,
        server_request_categories=categories,
        has_unclassified_server_requests=has_unclassified,
    )


def _error_snapshot() -> CapabilitySnapshot:
    return CapabilitySnapshot(
        version="",
        account=_PROBE_FAILED,
        effective_policy=None,
        policy_state=_PROBE_FAILED,
        managed_requirements=_PROBE_FAILED,
        agent_admission=_PROBE_FAILED,
        realtime=_PROBE_FAILED,
        interrupt=_PROBE_FAILED,
        steer=_PROBE_FAILED,
        server_requests=_PROBE_FAILED,
        server_request_categories=frozenset(),
        has_unclassified_server_requests=False,
    )


def _invalid_contract_snapshot(version: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        version=version,
        account=_INVALID_RESPONSE,
        effective_policy=None,
        policy_state=_INVALID_RESPONSE,
        managed_requirements=_INVALID_RESPONSE,
        agent_admission=_INVALID_RESPONSE,
        realtime=_INVALID_RESPONSE,
        interrupt=_INVALID_RESPONSE,
        steer=_INVALID_RESPONSE,
        server_requests=_INVALID_RESPONSE,
        server_request_categories=frozenset(),
        has_unclassified_server_requests=False,
    )


_MISSING = object()
