from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import IntEnum, StrEnum
from math import isfinite
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Never, TypeGuard, cast
from urllib.parse import SplitResult, unquote, urlsplit

from moco.errors import CodexSchemaError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from moco.platform import CodexCommand

_INVALID_SCHEMA = "Codex generated schema is invalid"
_INVALID_REFERENCE = "Codex generated schema reference is invalid"
_AMBIGUOUS_SCHEMA = "Codex generated schema semantic signals are ambiguous"
_SCHEMA_PROBE_FAILED = "Codex schema probe failed"
_VERSION_PROBE_FAILED = "Codex version probe failed"
_REQUIRED_METHOD_UNAVAILABLE = "required Codex semantic method is unavailable"
_INVALID_APPROVAL_PROFILE = "Codex approval profile is not coherent"
_INVALID_CONTRACT = "Codex protocol contract is not coherent"
# How many methods, categories, advertised names, or profiles one contract may carry.
_MAX_CONTRACT_ENTRIES = 256
_SUBPROCESS_TIMEOUT_SECONDS = 15.0
_MAX_SUBPROCESS_OUTPUT_BYTES = 262_144
_MAX_SCHEMA_DOCUMENT_BYTES = 1_048_576
_MAX_SCHEMA_BUNDLE_BYTES = 16_777_216
_MAX_SCHEMA_BUNDLE_FILES = 512
_MAX_REFERENCE_DEPTH = 128
_MAX_SCHEMA_VISITS = 20_000
# How deeply one listed JSON value is read while its uniqueness is checked. A generated enum
# lists short values, so a deeper one is left unread instead of driving unbounded recursion.
_MAX_VALUE_DEPTH = 32
_ALL_JSON_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
# JSON Schema type inclusion, keyed by the type of the value moco emits: an integer instance
# also satisfies `number`, while a fractional number never satisfies `integer`. Every other
# type is admitted only by a declaration naming it.
_ADMITTING_DECLARATIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"integer": frozenset({"integer", "number"})}
)
_ALLOWED_REF_SIBLINGS = frozenset(
    {
        "$anchor",
        "$comment",
        "$defs",
        "$dynamicAnchor",
        "$id",
        "$schema",
        "$vocabulary",
        "default",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)

# A JSON number written with a fraction or an exponent reads as a `Decimal`, which holds the
# number the document spells rather than the nearest binary float. That reading is private to
# schema loading and evaluation: no contract member or outbound request parameter carries one.
type JsonValue = None | bool | int | Decimal | str | list[JsonValue] | dict[str, JsonValue]
# A JSON scalar moco can send as a fixed value: null, boolean, integer, or string.
type _Scalar = bool | int | str | None
type _RefKey = tuple[Path, str]
type _RefStack = tuple[_RefKey, ...]
type _ResolvedSchema = tuple[dict[str, JsonValue], Path, _RefStack]
# One JSON value rewritten so that two keys are equal exactly when the values are the same
# JSON value: the JSON type is carried along, because Python alone conflates `true` and `1`.
type _ValueKey = tuple[str, _KeyPayload]
# One JSON value copied into a deeply immutable form, so a stored wire value can be handed
# out only as a fresh plain value built from it.
type _FrozenJson = (
    None | bool | int | str | tuple[_FrozenJson, ...] | MappingProxyType[str, _FrozenJson]
)
type _KeyPayload = (
    None | bool | int | Decimal | str | tuple[_ValueKey, ...] | tuple[tuple[str, _ValueKey], ...]
)


class _Admission(IntEnum):
    """How a resolved schema treats one value moco emits.

    Ordered so that narrowing a verdict with another one is `min`: a definite rejection
    anywhere wins, an undecided part downgrades an otherwise definite match, and only a
    schema every part of which definitely admits the value stays `ADMITTED`.
    """

    REJECTED = 0
    UNDECIDED = 1
    ADMITTED = 2


# Judges one resolved composition branch against the value under evaluation.
type _BranchAdmission = Callable[[_ResolvedSchema, int], _Admission]

_COMPOSITION_KEYWORDS = ("anyOf", "oneOf", "allOf")
# Definitely-admitting branch count at which the keyword's outcome is already decided.
# anyOf is absent on purpose: an admitting branch never decides the outcome, so every
# branch is resolved and a malformed later branch fails closed instead of hiding.
_COMPOSITION_BRANCH_LIMITS: Mapping[str, int] = MappingProxyType({"oneOf": 2})
# JSON types whose complete set of values an enum or const can spell out, so that a
# runtime value of that type is admitted whatever moco ends up sending.
_FINITE_VALUE_DOMAINS: Mapping[str, tuple[_Scalar, ...]] = MappingProxyType(
    {"boolean": (False, True), "null": (None,)}
)

# Assertions this module interprets when it checks one outbound request value against a
# resolved subschema. Anything else that is not a pure annotation cannot be interpreted
# safely, so the enclosing method is reported unavailable instead of guessed.
_SUPPORTED_ASSERTIONS = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "items",
        "maxItems",
        "minItems",
        "oneOf",
        "properties",
        "required",
        "type",
    }
)
# Annotations never constrain an instance, so they are ignored exactly like a $ref sibling.
_ANNOTATION_KEYWORDS = _ALLOWED_REF_SIBLINGS | frozenset({"format"})
_INTERPRETABLE_KEYWORDS = _SUPPORTED_ASSERTIONS | _ANNOTATION_KEYWORDS


@dataclass(frozen=True, slots=True)
class _DynamicValue:
    """An arbitrary runtime value of one JSON type that moco fills in per request."""

    json_type: str


@dataclass(frozen=True, slots=True)
class _LiteralValue:
    """A fixed value moco always sends verbatim."""

    value: _Scalar


@dataclass(frozen=True, slots=True)
class _ObjectValue:
    """A JSON object moco emits with exactly these members."""

    fields: Mapping[str, _Witness]


@dataclass(frozen=True, slots=True)
class _ArrayValue:
    """A JSON array moco emits with exactly these elements."""

    items: tuple[_Witness, ...]


# One complete request value moco can emit, described precisely enough to check it against
# a resolved schema without accepting values moco never sends.
type _Witness = _DynamicValue | _LiteralValue | _ObjectValue | _ArrayValue

_DYNAMIC_STRING = _DynamicValue("string")
# RpcPeer numbers its outbound requests, so the envelope id is always an integer.
_REQUEST_ID = _DynamicValue("integer")
_NULL = _LiteralValue(None)


def _object_value(fields: Mapping[str, _Witness]) -> _ObjectValue:
    return _ObjectValue(MappingProxyType(dict(fields)))


class _BudgetExhaustedError(Exception):
    """One bounded schema or value walk reached its work limit, carrying no payload."""


class _Budget:
    """A single walk's remaining work, so one payload can never drive an unbounded read."""

    __slots__ = ("_remaining",)

    def __init__(self, limit: int) -> None:
        self._remaining = limit

    def spend(self) -> None:
        self._remaining -= 1
        if self._remaining < 0:
            raise _BudgetExhaustedError


_EMPTY_CONTRACTS: Mapping[str, _ValueContract] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class _ValueContract:
    """One resolved approval subschema, compiled into a self-contained immutable check.

    A profile outlives the generated bundle it was read from, so every assertion a runtime
    value must satisfy is copied here while the bundle is still readable, with references
    already followed. Compilation refuses anything this representation cannot state, so a
    contract that exists checks its subschema whole rather than a summary of it.
    """

    types: frozenset[str] | None = None
    enum: tuple[_ValueKey, ...] | None = None
    # A one-tuple marks a declared `const`, which `None` alone could not tell from `null`.
    const: tuple[_ValueKey] | None = None
    properties: Mapping[str, _ValueContract] = _EMPTY_CONTRACTS
    required: frozenset[str] = frozenset()
    # An undeclared member is checked against this contract; `additional_refused` spells the
    # `additionalProperties: false` case, and neither one means the member may be anything.
    additional: _ValueContract | None = None
    additional_refused: bool = False
    items: _ValueContract | None = None
    min_items: int | None = None
    max_items: int | None = None
    minimum: Decimal | int | None = None
    maximum: Decimal | int | None = None
    # The generated `int64` format bounds an integer to what the app server can deserialize.
    int64: bool = False
    all_of: tuple[_ValueContract, ...] = ()
    any_of: tuple[_ValueContract, ...] = ()
    one_of: tuple[_ValueContract, ...] = ()

    def admits(self, value: object) -> bool:
        """Report whether one concrete runtime value satisfies this compiled subschema."""
        try:
            if not _value_contract_is_safe(self):
                return False
            return _value_admits(self, value, _Budget(_MAX_VALUE_NODES), 0)
        except _BudgetExhaustedError:
            return False
        except Exception:  # noqa: BLE001 - malformed caller-owned contracts fail closed
            return False


# How deeply one compiled subschema and one runtime value are read. Every generated approval
# schema nests far less; a deeper one is refused rather than followed.
_MAX_CONTRACT_DEPTH = 48
_MAX_RUNTIME_DEPTH = 48
# How many subschemas one member may compile into, and how many values one check may read.
_MAX_CONTRACT_NODES = 8_192
_MAX_VALUE_NODES = 32_768
# What a Rust `i64` timestamp can hold, which the generated `int64` format names.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
# The JSON type of one decoded runtime value, keyed by its exact Python type. A boolean is
# keyed apart from an integer, because Python counts `True` as `1` while JSON does not, and
# a subclass of either is not a value the JSON decoder produces.
_RUNTIME_TYPES: Mapping[type, str] = MappingProxyType(
    {
        type(None): "null",
        bool: "boolean",
        int: "integer",
        str: "string",
        list: "array",
        dict: "object",
    }
)
# Assertions the compiler states. Anything else that is not a pure annotation cannot be
# stated, so the enclosing approval family is left unprofiled instead of half-checked.
_CONTRACT_ASSERTIONS = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maximum",
        "minItems",
        "minimum",
        "oneOf",
        "properties",
        "required",
        "type",
    }
)
# `format` is an annotation in the draft the bundles declare, so an unknown one never
# narrows an instance. `int64` is still honoured, because honouring it can only reject a
# value the app server could not have deserialized anyway.
_INT64_FORMAT = "int64"
# How many members, branches or elements one compiled subschema may declare.
_MAX_CONTRACT_BREADTH = 256
_KEY_PAIR_SIZE = 2


def _compile_contract(
    raw: JsonValue,
    base_path: Path,
    stack: _RefStack,
    resolver: _SchemaResolver,
    budget: _Budget,
    depth: int = 0,
) -> _ValueContract:
    """Compile one resolved subschema, refusing everything this representation cannot state."""
    budget.spend()
    if depth > _MAX_CONTRACT_DEPTH:
        raise CodexSchemaError(_INVALID_SCHEMA)
    schema, path, ref_stack = resolver.resolve(raw, base_path, stack)
    if not schema.keys() <= (_CONTRACT_ASSERTIONS | _ANNOTATION_KEYWORDS):
        raise CodexSchemaError(_INVALID_SCHEMA)
    if "exclusiveMinimum" in schema or "exclusiveMaximum" in schema:
        # Draft-04 spells these as booleans beside `minimum`, later drafts as numbers, and
        # the generated bundles declare neither, so no reading of them can be proven here.
        raise CodexSchemaError(_INVALID_SCHEMA)

    def child(value: JsonValue) -> _ValueContract:
        return _compile_contract(value, path, ref_stack, resolver, budget, depth + 1)

    return _ValueContract(
        types=_compiled_types(schema),
        enum=_compiled_enum(schema),
        const=_compiled_const(schema),
        properties=_compiled_properties(schema, child),
        required=_compiled_required(schema),
        additional=_compiled_additional(schema, child),
        additional_refused=schema.get("additionalProperties") is False,
        items=_compiled_items(schema, child),
        min_items=_compiled_bound(schema, "minItems"),
        max_items=_compiled_bound(schema, "maxItems"),
        minimum=_compiled_limit(schema, "minimum"),
        maximum=_compiled_limit(schema, "maximum"),
        int64=schema.get("format") == _INT64_FORMAT,
        all_of=_compiled_branches(schema, "allOf", child),
        any_of=_compiled_branches(schema, "anyOf", child),
        one_of=_compiled_branches(schema, "oneOf", child),
    )


def _compiled_types(schema: dict[str, JsonValue]) -> frozenset[str] | None:
    if "type" not in schema:
        return None
    declared = _declared_types(schema)
    if declared is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    return declared


def _compiled_enum(schema: dict[str, JsonValue]) -> tuple[_ValueKey, ...] | None:
    if "enum" not in schema:
        return None
    listed = schema["enum"]
    if not isinstance(listed, list) or not listed or len(listed) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_SCHEMA)
    keys = tuple(_contract_key(value) for value in listed)
    if len(set(keys)) != len(keys):
        raise CodexSchemaError(_INVALID_SCHEMA)
    return keys


def _compiled_const(schema: dict[str, JsonValue]) -> tuple[_ValueKey] | None:
    return None if "const" not in schema else (_contract_key(schema["const"]),)


def _contract_key(value: JsonValue) -> _ValueKey:
    key = _json_value_key(value)
    if key is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    return key


def _compiled_properties(
    schema: dict[str, JsonValue],
    child: Callable[[JsonValue], _ValueContract],
) -> Mapping[str, _ValueContract]:
    if "properties" not in schema:
        return _EMPTY_CONTRACTS
    declared = schema["properties"]
    if not _is_schema_map(declared) or len(cast("dict[str, JsonValue]", declared)) > (
        _MAX_CONTRACT_BREADTH
    ):
        raise CodexSchemaError(_INVALID_SCHEMA)
    members = cast("dict[str, JsonValue]", declared)
    return MappingProxyType({name: child(member) for name, member in members.items()})


def _compiled_required(schema: dict[str, JsonValue]) -> frozenset[str]:
    if "required" not in schema:
        return frozenset()
    required = _required_fields(schema)
    if required is None or len(required) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_SCHEMA)
    return required


def _compiled_additional(
    schema: dict[str, JsonValue],
    child: Callable[[JsonValue], _ValueContract],
) -> _ValueContract | None:
    declared = schema.get("additionalProperties")
    if declared is None or declared is True or declared is False:
        return None
    if not _is_schema_map(declared):
        raise CodexSchemaError(_INVALID_SCHEMA)
    return child(declared)


def _compiled_items(
    schema: dict[str, JsonValue],
    child: Callable[[JsonValue], _ValueContract],
) -> _ValueContract | None:
    if "items" not in schema:
        return None
    declared = schema["items"]
    # A list spells per-position schemas, which no generated approval document declares and
    # which this representation does not state, so it is refused rather than approximated.
    if not _is_schema_map(declared):
        raise CodexSchemaError(_INVALID_SCHEMA)
    return child(declared)


def _compiled_bound(schema: dict[str, JsonValue], keyword: str) -> int | None:
    if keyword not in schema:
        return None
    value = schema[keyword]
    if not _is_item_bound(value):
        raise CodexSchemaError(_INVALID_SCHEMA)
    return value


def _compiled_limit(schema: dict[str, JsonValue], keyword: str) -> Decimal | int | None:
    if keyword not in schema:
        return None
    value = schema[keyword]
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise CodexSchemaError(_INVALID_SCHEMA)
    return value


def _compiled_branches(
    schema: dict[str, JsonValue],
    keyword: str,
    child: Callable[[JsonValue], _ValueContract],
) -> tuple[_ValueContract, ...]:
    if keyword not in schema:
        return ()
    listed = schema[keyword]
    if not isinstance(listed, list) or not listed or len(listed) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_SCHEMA)
    return tuple(child(branch) for branch in listed)


def _value_admits(contract: _ValueContract, value: object, budget: _Budget, depth: int) -> bool:
    """Report whether one concrete runtime value satisfies one compiled subschema."""
    budget.spend()
    if depth > _MAX_RUNTIME_DEPTH:
        return False
    json_type = _runtime_type(value)
    if json_type is None or not _declared_type_admits(contract, json_type):
        return False
    if json_type == "string" and not _is_transport_safe(cast("str", value)):
        return False
    return (
        _listed_value_admits(contract, value)
        and _number_admits(contract, value, json_type)
        and _structured_value_admits(contract, value, budget, depth)
        and _composed_value_admits(contract, value, budget, depth)
    )


def _declared_type_admits(contract: _ValueContract, json_type: str) -> bool:
    if contract.types is None:
        return True
    accepted = _ADMITTING_DECLARATIONS.get(json_type, frozenset({json_type}))
    return bool(accepted & contract.types)


def _runtime_type(value: object) -> str | None:
    """Name the JSON type of one decoded runtime value, or nothing when it is not JSON.

    A boolean is checked before an integer, because Python counts `True` as `1` while JSON
    keeps the two apart, and a non-finite number is not a JSON value at all.
    """
    if isinstance(value, float | Decimal):
        return "number" if isfinite(value) else None
    return _RUNTIME_TYPES.get(type(value))


def _listed_value_admits(contract: _ValueContract, value: object) -> bool:
    if contract.enum is None and contract.const is None:
        return True
    key = _runtime_value_key(value)
    if key is None:
        return False
    if contract.const is not None and key != contract.const[0]:
        return False
    return contract.enum is None or key in contract.enum


def _number_admits(contract: _ValueContract, value: object, json_type: str) -> bool:
    if json_type not in {"integer", "number"}:
        return True
    number = cast("int | Decimal | float", value)
    if contract.int64 and not (_INT64_MIN <= number <= _INT64_MAX):
        return False
    if contract.minimum is not None and number < contract.minimum:
        return False
    return contract.maximum is None or number <= contract.maximum


def _structured_value_admits(
    contract: _ValueContract,
    value: object,
    budget: _Budget,
    depth: int,
) -> bool:
    if type(value) is dict:
        return _object_value_admits(contract, cast("dict[object, object]", value), budget, depth)
    if type(value) is list:
        return _array_value_admits(contract, cast("list[object]", value), budget, depth)
    return True


def _object_value_admits(
    contract: _ValueContract,
    value: dict[object, object],
    budget: _Budget,
    depth: int,
) -> bool:
    # Every member name is checked as plain text before any of them is looked up, compared,
    # or hashed again. A decoded JSON object carries none other, while a caller-supplied one
    # could carry a string subclass whose own equality raises with the payload attached.
    for name in value:
        if type(name) is not str or not _is_transport_safe(name):
            return False
    if not contract.required <= value.keys():
        return False
    for name, member in value.items():
        budget.spend()
        declared = contract.properties.get(cast("str", name))
        if declared is None:
            # A nested object that declares no `additionalProperties` permits an unknown
            # member, and refusing one here would fail a payload its own schema allows.
            # Unexpected members are refused where the approval params and the response
            # value are read, not inside a nested value moco only displays.
            if contract.additional_refused:
                return False
            declared = contract.additional
        if declared is not None and not _value_admits(declared, member, budget, depth + 1):
            return False
    return True


def _array_value_admits(
    contract: _ValueContract,
    value: list[object],
    budget: _Budget,
    depth: int,
) -> bool:
    if contract.min_items is not None and len(value) < contract.min_items:
        return False
    if contract.max_items is not None and len(value) > contract.max_items:
        return False
    if contract.items is None:
        return True
    return all(_value_admits(contract.items, item, budget, depth + 1) for item in value)


def _composed_value_admits(
    contract: _ValueContract,
    value: object,
    budget: _Budget,
    depth: int,
) -> bool:
    if any(not _value_admits(branch, value, budget, depth + 1) for branch in contract.all_of):
        return False
    if contract.any_of and not any(
        _value_admits(branch, value, budget, depth + 1) for branch in contract.any_of
    ):
        return False
    if not contract.one_of:
        return True
    matched = sum(
        1 for branch in contract.one_of if _value_admits(branch, value, budget, depth + 1)
    )
    return matched == 1


def _contract_types(contract: _ValueContract, depth: int = 0) -> frozenset[str]:
    """Over-approximate the JSON types one compiled subschema may admit."""
    if depth > _MAX_CONTRACT_DEPTH:
        return frozenset()
    types = _ALL_JSON_TYPES if contract.types is None else contract.types
    for listed in (contract.enum, contract.const):
        if listed is not None:
            types &= frozenset().union(*(_key_types(key) for key in listed))
    for branch in contract.all_of:
        types &= _contract_types(branch, depth + 1)
    for group in (contract.any_of, contract.one_of):
        if group:
            types &= frozenset().union(*(_contract_types(branch, depth + 1) for branch in group))
    return types


def _key_types(key: _ValueKey) -> frozenset[str]:
    """Name the JSON types one listed value can be, keyed numbers covering both spellings."""
    named = key[0]
    return frozenset({"integer", "number"}) if named == "number" else frozenset({named})


class SemanticMethod(StrEnum):
    ACCOUNT_READ = "account_read"
    CONFIG_READ = "config_read"
    CONFIG_REQUIREMENTS_READ = "config_requirements_read"
    EXPERIMENTAL_FEATURE_LIST = "experimental_feature_list"
    REALTIME_VOICES_LIST = "realtime_voices_list"
    THREAD_START = "thread_start"
    TURN_START = "turn_start"
    TURN_STEER = "turn_steer"
    TURN_INTERRUPT = "turn_interrupt"


class ParamsKind(StrEnum):
    OBJECT = "object"
    OMITTED = "omitted"


class ServerRequestCategory(StrEnum):
    COMMAND_APPROVAL = "command_approval"
    FILE_CHANGE_APPROVAL = "file_change_approval"
    USER_INPUT = "user_input"
    MCP_ELICITATION = "mcp_elicitation"
    PERMISSION_APPROVAL = "permission_approval"
    DYNAMIC_TOOL_CALL = "dynamic_tool_call"
    AUTH_TOKEN_REFRESH = "auth_token_refresh"  # noqa: S105
    ATTESTATION = "attestation"
    CURRENT_TIME = "current_time"


VOICE_REQUIRED_METHODS = frozenset(
    {SemanticMethod.ACCOUNT_READ, SemanticMethod.REALTIME_VOICES_LIST}
)
AGENT_READINESS_METHODS = frozenset(
    {
        SemanticMethod.THREAD_START,
        SemanticMethod.TURN_START,
        SemanticMethod.TURN_INTERRUPT,
    }
)
STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES = frozenset(
    {
        ServerRequestCategory.COMMAND_APPROVAL,
        ServerRequestCategory.FILE_CHANGE_APPROVAL,
    }
)


class ApprovalDecision(StrEnum):
    """One reviewer decision that answers exactly the request under review and nothing after.

    These are moco's own semantics. The JSON value each one is sent as belongs to the
    effective generated response schema and is read from it per method, never assumed here.
    """

    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class ApprovalCorrelation(StrEnum):
    """Which identifiers one approval family states about the request it is asking about.

    The newer families name the thread, turn and item an approval belongs to. The legacy
    families name the conversation and the tool call instead, and state no turn or item, so
    a reader must carry what they do say rather than invent the identifiers they do not.
    """

    THREAD_ITEM = "thread_item"
    CONVERSATION_CALL = "conversation_call"


@dataclass(frozen=True, slots=True)
class ClientMethodContract:
    name: str
    params_kind: ParamsKind
    semantic_fields: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True, repr=False)
class FileChangePatchProfile:
    """Generated evidence for the one file-change patch notification moco consumes."""

    method: str
    params_contract: _ValueContract

    def __post_init__(self) -> None:
        if type(self.method) is not str or not self.method or not _is_transport_safe(self.method):
            raise CodexSchemaError(_INVALID_CONTRACT)
        if type(self.params_contract) is not _ValueContract:
            raise CodexSchemaError(_INVALID_CONTRACT)
        contract = _freeze_value_contract(self.params_contract)
        if not all(contract.admits(params) for params in _file_change_patch_witnesses()):
            raise CodexSchemaError(_INVALID_CONTRACT)
        object.__setattr__(self, "params_contract", contract)

    def admits(self, params: object) -> bool:
        return self.params_contract.admits(params)

    def __repr__(self) -> str:
        return "FileChangePatchProfile()"


def _file_change_patch_witnesses() -> tuple[dict[str, object], ...]:
    def params(kind: dict[str, object]) -> dict[str, object]:
        return {
            "changes": [{"diff": "", "kind": kind, "path": "path"}],
            "itemId": "item",
            "threadId": "thread",
            "turnId": "turn",
        }

    return (
        params({"type": "add"}),
        params({"type": "delete"}),
        params({"type": "update"}),
        params({"move_path": None, "type": "update"}),
        params({"move_path": "destination", "type": "update"}),
    )


@dataclass(frozen=True, slots=True)
class AgentEventProfile:
    """The smallest generated evidence needed to own one Agent turn safely.

    This is intentionally an event contract, not a copy of the server's item catalog.  The
    runtime needs the aliases and shapes that correlate a turn and prove its terminal result;
    every other item remains generic progress owned by the provider.
    """

    turn_completed_method: str
    item_completed_method: str
    agent_message_delta_method: str | None
    turn_completed_required_fields: frozenset[str]
    item_completed_required_fields: frozenset[str]
    turn_required_fields: frozenset[str]
    agent_message_required_fields: frozenset[str]
    turn_completed_field_types: Mapping[str, frozenset[str]]
    item_completed_field_types: Mapping[str, frozenset[str]]
    turn_field_types: Mapping[str, frozenset[str]]
    agent_message_field_types: Mapping[str, frozenset[str]]
    agent_message_phase_values: frozenset[str]
    agent_message_phase_optional: bool
    turn_status_values: frozenset[str]
    completed_status: str
    interrupted_status: str
    failed_status: str
    in_progress_status: str
    agent_message_type: str = "agentMessage"
    agent_message_delta_required_fields: frozenset[str] = frozenset()
    agent_message_delta_field_types: Mapping[str, frozenset[str]] = MappingProxyType({})
    thread_id_field: str = "threadId"
    turn_id_field: str = "turnId"
    item_field: str = "item"
    turn_field: str = "turn"
    id_field: str = "id"
    type_field: str = "type"
    text_field: str = "text"
    phase_field: str = "phase"
    status_field: str = "status"
    item_id_field: str = "itemId"
    delta_field: str = "delta"
    item_started_method: str | None = None
    item_started_required_fields: frozenset[str] = frozenset()
    item_started_field_types: Mapping[str, frozenset[str]] = MappingProxyType({})

    def __post_init__(self) -> None:  # noqa: C901, PLR0912
        for method in (
            self.turn_completed_method,
            self.item_completed_method,
            self.agent_message_delta_method,
            self.item_started_method,
        ):
            if method is not None and (type(method) is not str or not method):
                raise CodexSchemaError(_INVALID_CONTRACT)
        for names in (
            self.turn_completed_required_fields,
            self.item_completed_required_fields,
            self.turn_required_fields,
            self.agent_message_required_fields,
            self.agent_message_delta_required_fields,
            self.item_started_required_fields,
            self.agent_message_phase_values,
            self.turn_status_values,
        ):
            if type(names) is not frozenset or not all(
                type(name) is str and bool(name) for name in names
            ):
                raise CodexSchemaError(_INVALID_CONTRACT)
        for attribute in (
            "turn_completed_field_types",
            "item_completed_field_types",
            "turn_field_types",
            "agent_message_field_types",
            "agent_message_delta_field_types",
            "item_started_field_types",
        ):
            field_map = getattr(self, attribute)
            object.__setattr__(self, attribute, _freeze_agent_field_types(field_map))
        if (self.item_started_method is None) != (
            not self.item_started_required_fields and not self.item_started_field_types
        ):
            raise CodexSchemaError(_INVALID_CONTRACT)
        if self.item_started_method is not None and self.item_started_method in {
            self.turn_completed_method,
            self.item_completed_method,
            self.agent_message_delta_method,
        }:
            raise CodexSchemaError(_INVALID_CONTRACT)
        if not {"commentary", "final_answer"} <= self.agent_message_phase_values:
            raise CodexSchemaError(_INVALID_CONTRACT)
        if (
            not {
                self.completed_status,
                self.interrupted_status,
                self.failed_status,
                self.in_progress_status,
            }
            <= self.turn_status_values
        ):
            raise CodexSchemaError(_INVALID_CONTRACT)
        if type(self.agent_message_phase_optional) is not bool:
            raise CodexSchemaError(_INVALID_CONTRACT)
        if type(self.agent_message_type) is not str or not self.agent_message_type:
            raise CodexSchemaError(_INVALID_CONTRACT)
        for field_name in (
            self.thread_id_field,
            self.turn_id_field,
            self.item_field,
            self.turn_field,
            self.id_field,
            self.type_field,
            self.text_field,
            self.phase_field,
            self.status_field,
            self.item_id_field,
            self.delta_field,
        ):
            if type(field_name) is not str or not field_name:
                raise CodexSchemaError(_INVALID_CONTRACT)


@dataclass(frozen=True, slots=True)
class ApprovalProfile:
    """One raw approval method moco can read and answer, as one effective schema spells it.

    The profile carries what differs between builds and families: which params members
    exist and what each one may hold, which of them the build requires, which ones would
    widen the reviewed scope, which identifiers the family states, and the exact JSON value
    each reviewer decision is answered with. A profile exists only when every declared
    params member compiled into a checkable contract and every decision moco may send was
    proved by the matching generated response document, so a method without one is
    advertised but not adaptable.
    """

    category: ServerRequestCategory
    correlation: ApprovalCorrelation
    required_members: frozenset[str]
    absent_or_null_members: frozenset[str]
    member_contracts: Mapping[str, _ValueContract]
    argv_member: str | None
    changes_member: str | None
    offer_member: str | None
    # A caller states the wire values as plain JSON; construction freezes them in place.
    decisions: Mapping[ApprovalDecision, JsonValue]
    decision_contract: _ValueContract
    # How one changed file of this family is spelled, which only a family stating its own
    # changes has. Construction derives it from the family, so no caller can narrow what a
    # reviewed change may say.
    change_shape: _FileChangeShape | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        category = cast("object", self.category)
        # A profile belongs to a family with a typed adapter, which today is the two Stage B
        # approval families. Discovery keeps reporting every other category on its own.
        if (
            type(category) is not ServerRequestCategory
            or category not in STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
            or type(cast("object", self.correlation)) is not ApprovalCorrelation
            or type(cast("object", self.decision_contract)) is not _ValueContract
        ):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        decision_contract = _freeze_value_contract(self.decision_contract)
        raw_contracts = _member_contracts(self.member_contracts)
        contracts = MappingProxyType(
            {name: _freeze_value_contract(contract) for name, contract in raw_contracts.items()}
        )
        object.__setattr__(self, "decision_contract", decision_contract)
        object.__setattr__(self, "member_contracts", contracts)
        declared = frozenset(contracts)
        for names in (self.required_members, self.absent_or_null_members):
            if not _member_names(names) <= declared:
                raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        # A build that starts requiring a member moco must refuse is a build moco cannot read.
        if not self.required_members.isdisjoint(self.absent_or_null_members):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        for member in (self.argv_member, self.changes_member, self.offer_member):
            named = cast("object", member)
            if named is None:
                continue
            if type(named) is not str or not named or not _is_transport_safe(named):
                raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
            if named not in declared:
                raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        object.__setattr__(self, "decisions", _decision_values(self.decisions))
        # Everything above states that the profile is well formed on its own terms. What
        # follows states that it describes one real approval family, using exactly the
        # invariants a generated bundle is read through, so a hand-built profile and a
        # discovered one cannot mean two different things.
        spec = _family_spec(self.category, self.correlation)
        object.__setattr__(self, "change_shape", spec.change_shape)
        _require_family_shape(self, spec, declared)
        _require_family_decisions(self._frozen_decisions, spec, self.decision_contract)

    @property
    def declared_members(self) -> frozenset[str]:
        """Return every params member this build declares, which is all it may send."""
        return frozenset(self.member_contracts)

    def admits_member(self, name: str, value: object) -> bool:
        """Report whether one runtime params member holds what this build declares for it."""
        contract = (
            self.member_contracts.get(name)
            if type(name) is str and _is_transport_safe(name)
            else None
        )
        return contract is not None and contract.admits(value)

    def admits_decision(self, value: object) -> bool:
        """Report whether one offered decision value is one this build's schema spells."""
        return self.decision_contract.admits(value)

    def wire_decision(self, decision: ApprovalDecision) -> JsonValue:
        """Return a fresh JSON value this build spells for one reviewer decision."""
        wire = self._frozen_decisions.get(decision) if type(decision) is ApprovalDecision else None
        if wire is None:
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        return _materialize_json(wire)

    def semantic_decision(self, value: object) -> ApprovalDecision | None:
        """Return the reviewer decision one offered value means, or None when it means none."""
        key = _runtime_value_key(value)
        if key is None:
            return None
        for decision, candidate in self._frozen_decisions.items():
            if _json_value_key(_materialize_json(candidate)) == key:
                return decision
        return None

    @property
    def _frozen_decisions(self) -> Mapping[ApprovalDecision, _FrozenJson]:
        """Read the wire values as construction stored them, deeply frozen and transport-safe."""
        return cast("Mapping[ApprovalDecision, _FrozenJson]", self.decisions)


def _member_names(value: object) -> frozenset[str]:
    """Read one exact frozenset of member names, refusing a subclass that could override it."""
    if type(value) is not frozenset or len(value) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    for name in cast("frozenset[object]", value):
        if type(name) is not str or not name or not _is_transport_safe(name):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    return cast("frozenset[str]", value)


def _member_contracts(value: object) -> Mapping[str, _ValueContract]:
    """Freeze the per-member contracts, which must cover exactly the declared members."""
    if type(value) is not dict:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    declared = cast("dict[object, object]", value)
    if not declared or len(declared) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    frozen: dict[str, _ValueContract] = {}
    for name, contract in declared.items():
        if type(name) is not str or not name or not _is_transport_safe(name):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        if type(contract) is not _ValueContract:
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        frozen[name] = contract
    return MappingProxyType(frozen)


def _decision_values(value: object) -> Mapping[ApprovalDecision, _FrozenJson]:
    """Freeze the decision-to-wire mapping, which must answer every decision moco presents.

    Each value is frozen whole, so a profile cannot hand out a shared mutable response, and
    the values are compared as JSON values, so a build spelling decline and cancel alike
    would leave the reviewer unable to tell one refusal from the other and is refused.
    """
    if type(value) is not dict:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    wires = cast("dict[object, object]", value)
    if len(wires) != len(ApprovalDecision):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    for key in wires:
        if type(key) is not ApprovalDecision:
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    if [key for key in wires if type(key) is ApprovalDecision] != list(wires):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    if wires.keys() != set(ApprovalDecision):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    if not all(_is_decision_shape(wire) for wire in wires.values()):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    frozen = {
        cast("ApprovalDecision", decision): _freeze_json(wire) for decision, wire in wires.items()
    }
    keys = [_runtime_value_key(_materialize_json(wire)) for wire in frozen.values()]
    if any(key is None for key in keys) or len(set(keys)) != len(keys):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    return MappingProxyType(frozen)


def _is_decision_shape(value: object) -> bool:
    """Report whether one value is shaped like a decision a generated vocabulary spells.

    Every observed vocabulary spells a decision as a named string or as a single-member
    object carrying that decision's own values. Nothing else names a decision, so a number,
    a list, or a blank name is not a decision this profile could send.
    """
    if type(value) is str:
        return bool(value.strip())
    return type(value) is dict and bool(value)


def _freeze_json(
    value: object,
    depth: int = 0,
    *,
    budget: _Budget | None = None,
) -> _FrozenJson:
    """Copy one JSON value into a deeply immutable one the transport can carry.

    A stored wire value is handed to a reviewer boundary and then serialized, so it is
    checked here for what JSON and UTF-8 can carry rather than at the socket, where the
    failure would arrive with a payload attached and no response left to send.
    """
    if budget is None:
        budget = _Budget(_MAX_VALUE_NODES)
    try:
        budget.spend()
        if depth > _MAX_RUNTIME_DEPTH:
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        if value is None or type(value) is bool or type(value) is int:
            return value
        if type(value) is str:
            if not _is_transport_safe(value):
                raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
            return value
        if type(value) is list:
            return _freeze_json_list(cast("list[object]", value), depth, budget)
        if type(value) is dict:
            return _freeze_json_object(cast("dict[object, object]", value), depth, budget)
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    except _BudgetExhaustedError:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE) from None


def _freeze_json_list(
    values: list[object],
    depth: int,
    budget: _Budget,
) -> tuple[_FrozenJson, ...]:
    if len(values) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    frozen_items = [_freeze_json(item, depth + 1, budget=budget) for item in values]
    return tuple(frozen_items)


def _freeze_json_object(
    members: dict[object, object],
    depth: int,
    budget: _Budget,
) -> MappingProxyType[str, _FrozenJson]:
    if len(members) > _MAX_CONTRACT_BREADTH:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    frozen: dict[str, _FrozenJson] = {}
    for name, member in members.items():
        if type(name) is not str or not _is_transport_safe(name):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        frozen[name] = _freeze_json(member, depth + 1, budget=budget)
    return MappingProxyType(frozen)


def _materialize_json(value: _FrozenJson, depth: int = 0) -> JsonValue:
    """Build a fresh plain JSON value, so no caller holds the stored one."""
    if depth > _MAX_RUNTIME_DEPTH:  # pragma: no cover - a frozen value is already bounded
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    if isinstance(value, tuple):
        return [_materialize_json(item, depth + 1) for item in value]
    if isinstance(value, MappingProxyType):
        return {name: _materialize_json(member, depth + 1) for name, member in value.items()}
    return value


def _is_transport_safe(text: str) -> bool:
    """Report whether one string survives JSON and UTF-8 transport, without quoting it."""
    if type(text) is not str:
        return False
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def _value_key_is_safe(value: object, depth: int, budget: _Budget) -> bool:
    """Check a compiled comparison key before equality or hashing can inspect it."""
    budget.spend()
    if type(value) is not tuple or len(value) != _KEY_PAIR_SIZE or depth > _MAX_VALUE_DEPTH:
        return False
    kind, payload = value
    if type(kind) is not str or not _is_transport_safe(kind):
        return False
    return _value_key_payload_is_safe(kind, payload, depth, budget)


def _value_key_payload_is_safe(kind: str, payload: object, depth: int, budget: _Budget) -> bool:
    if kind == "array":
        return _value_key_sequence_is_safe(payload, depth, budget)
    if kind == "object":
        return _value_key_object_is_safe(payload, depth, budget)
    return (
        payload is None
        or type(payload) is bool
        or type(payload) is int
        or (type(payload) is Decimal or (type(payload) is str and _is_transport_safe(payload)))
    )


def _value_key_sequence_is_safe(payload: object, depth: int, budget: _Budget) -> bool:
    if type(payload) is not tuple or len(payload) > _MAX_CONTRACT_BREADTH:
        return False
    return all(
        _value_key_is_safe(item, depth + 1, budget) for item in cast("tuple[object, ...]", payload)
    )


def _value_key_object_is_safe(payload: object, depth: int, budget: _Budget) -> bool:
    if type(payload) is not tuple or len(payload) > _MAX_CONTRACT_BREADTH:
        return False
    for member in cast("tuple[object, ...]", payload):
        if type(member) is not tuple or len(member) != _KEY_PAIR_SIZE:
            return False
        name, nested = member
        if type(name) is not str or not _is_transport_safe(name):
            return False
        if not _value_key_is_safe(nested, depth + 1, budget):
            return False
    return True


def _value_contract_is_safe(
    contract: _ValueContract,
    depth: int = 0,
    budget: _Budget | None = None,
) -> bool:
    """Validate internal contract containers before any set or mapping operation uses them."""
    if budget is None:
        budget = _Budget(_MAX_CONTRACT_NODES)
    budget.spend()
    if type(contract) is not _ValueContract or depth > _MAX_CONTRACT_DEPTH:
        return False
    return (
        _contract_node_is_safe(contract, budget)
        and _contract_properties_are_safe(contract.properties, depth, budget)
        and _contract_children_are_safe((contract.additional, contract.items), depth, budget)
        and _contract_branches_are_safe(
            (contract.all_of, contract.any_of, contract.one_of), depth, budget
        )
    )


def _contract_node_is_safe(contract: _ValueContract, budget: _Budget) -> bool:
    """Check the containers one contract node states about itself, apart from its children.

    Freezing reads every caller-owned property mapping once and checks the children that one
    read returned, so what a node states about itself is described here rather than only
    inside a whole-tree walk a second reader would have to repeat.
    """
    return (
        _contract_text_set_is_safe(contract.types, allow_none=True)
        and _contract_text_set_is_safe(contract.required)
        and _contract_keys_are_safe(contract.enum, budget)
        and _contract_keys_are_safe(contract.const, budget)
        and _contract_bounds_are_safe(contract)
    )


def _contract_text_set_is_safe(value: object, *, allow_none: bool = False) -> bool:
    if allow_none and value is None:
        return True
    if type(value) is not frozenset or len(value) > _MAX_CONTRACT_BREADTH:
        return False
    return all(
        type(name) is str and _is_transport_safe(name) for name in cast("frozenset[object]", value)
    )


def _contract_properties_snapshot(value: object, budget: _Budget) -> dict[object, object] | None:
    """Snapshot a property mapping while containing every caller-owned read failure."""
    return _bounded_mapping_snapshot(value, max_entries=_MAX_CONTRACT_BREADTH, budget=budget)


def _bounded_mapping_snapshot(
    value: object,
    *,
    max_entries: int,
    budget: _Budget,
) -> dict[object, object] | None:
    """Read an exact mapping or mapping proxy to its declared size plus one item at most.

    A mapping proxy can wrap a caller-owned mapping whose declared length and iterator do
    not agree. Reading through ``dict`` or an unbounded ``items`` loop would let that source
    exceed this contract's breadth and work budget, so the count and uniqueness are checked
    while each possible item read consumes the caller's shared budget.
    """
    if type(value) is not dict and type(value) is not MappingProxyType:
        return None
    budget.spend()
    try:
        declared_length = len(value)
        if type(declared_length) is not int or declared_length < 0 or declared_length > max_entries:
            return None
        iterator = _mapping_item_iterator(value, budget)
        return _read_bounded_mapping_items(iterator, declared_length, budget)
    except _BudgetExhaustedError:
        raise
    except Exception:  # noqa: BLE001 - hostile contract containers fail closed
        return None


def _mapping_item_iterator(
    value: object,
    budget: _Budget,
) -> Iterator[tuple[object, object]]:
    """Yield one mapping value at a time without invoking an eager ``items`` override."""
    mapping = cast("Mapping[object, object]", value)
    for key in mapping:
        budget.spend()
        yield key, mapping[key]


def _read_bounded_mapping_items(
    iterator: Iterator[tuple[object, object]],
    declared_length: int,
    budget: _Budget,
) -> dict[object, object] | None:
    """Read at most one item beyond a mapping's declared length."""
    snapshot: dict[object, object] = {}
    for index in range(declared_length + 1):
        budget.spend()
        try:
            item = next(iterator)
        except StopIteration:
            return (
                snapshot if index == declared_length and len(snapshot) == declared_length else None
            )
        if type(item) is not tuple or len(item) != _KEY_PAIR_SIZE:
            return None
        key, member = item
        snapshot[key] = member
        if len(snapshot) != index + 1 or index >= declared_length:
            return None
    return None


def _contract_properties_are_safe(value: object, depth: int, budget: _Budget) -> bool:
    members = _contract_properties_snapshot(value, budget)
    if members is None:
        return False
    return all(
        type(name) is str
        and _is_transport_safe(name)
        and type(nested) is _ValueContract
        and _value_contract_is_safe(nested, depth + 1, budget)
        for name, nested in members.items()
    )


def _contract_keys_are_safe(value: object, budget: _Budget) -> bool:
    if value is None:
        return True
    if type(value) is not tuple or len(value) > _MAX_CONTRACT_BREADTH:
        return False
    return all(_value_key_is_safe(key, 0, budget) for key in value)


def _contract_children_are_safe(value: tuple[object, ...], depth: int, budget: _Budget) -> bool:
    return all(
        child is None
        or (type(child) is _ValueContract and _value_contract_is_safe(child, depth + 1, budget))
        for child in value
    )


def _contract_branches_are_safe(value: tuple[object, ...], depth: int, budget: _Budget) -> bool:
    for branches in value:
        if type(branches) is not tuple or len(branches) > _MAX_CONTRACT_BREADTH:
            return False
        if not all(
            type(branch) is _ValueContract and _value_contract_is_safe(branch, depth + 1, budget)
            for branch in branches
        ):
            return False
    return True


def _contract_bounds_are_safe(contract: _ValueContract) -> bool:
    if (
        type(cast("object", contract.additional_refused)) is not bool
        or type(cast("object", contract.int64)) is not bool
    ):
        return False
    for bound in (contract.min_items, contract.max_items):
        if bound is not None and (type(bound) is not int or bound < 0):
            return False
    return all(
        limit is None or type(limit) in (int, Decimal)
        for limit in (contract.minimum, contract.maximum)
    )


def _freeze_value_contract(contract: _ValueContract) -> _ValueContract:
    """Snapshot a checked contract so a manual profile cannot retain caller-owned mappings.

    A mapping proxy states nothing about the mapping behind it, so a caller-owned source may
    answer one lookup with a child a check accepts and the next with another. Each node is
    therefore read once here, and the children that one read returned are the same ones the
    checks are made about and the copy is built from, so a profile keeps what was checked.
    """
    try:
        return _checked_contract_copy(contract, 0, _Budget(_MAX_CONTRACT_NODES))
    except Exception:  # noqa: BLE001 - a malformed manual contract fails closed
        _invalid_profile()


def _invalid_profile() -> Never:
    """Refuse one profile without naming, quoting, or chaining what was being read."""
    raise CodexSchemaError(_INVALID_APPROVAL_PROFILE) from None


def _checked_contract_copy(current: object, depth: int, budget: _Budget) -> _ValueContract:
    """Check one contract node and copy it from the same reads that were checked."""
    budget.spend()
    if type(current) is not _ValueContract or depth > _MAX_CONTRACT_DEPTH:
        _invalid_profile()
    node = current
    if not _contract_node_is_safe(node, budget):
        _invalid_profile()
    members = _contract_properties_snapshot(node.properties, budget)
    if members is None:
        _invalid_profile()
    additional = node.additional
    items = node.items
    return _ValueContract(
        types=node.types,
        enum=node.enum,
        const=node.const,
        properties=MappingProxyType(_checked_member_copies(members, depth, budget)),
        required=node.required,
        additional=None
        if additional is None
        else _checked_contract_copy(additional, depth + 1, budget),
        additional_refused=node.additional_refused,
        items=None if items is None else _checked_contract_copy(items, depth + 1, budget),
        min_items=node.min_items,
        max_items=node.max_items,
        minimum=node.minimum,
        maximum=node.maximum,
        int64=node.int64,
        all_of=_checked_branch_copies(node.all_of, depth, budget),
        any_of=_checked_branch_copies(node.any_of, depth, budget),
        one_of=_checked_branch_copies(node.one_of, depth, budget),
    )


def _checked_member_copies(
    members: dict[object, object],
    depth: int,
    budget: _Budget,
) -> dict[str, _ValueContract]:
    """Copy the members one property snapshot returned, checking each name as plain text."""
    copied: dict[str, _ValueContract] = {}
    for name, nested in members.items():
        if type(name) is not str or not _is_transport_safe(name):
            _invalid_profile()
        copied[name] = _checked_contract_copy(nested, depth + 1, budget)
    return copied


def _checked_branch_copies(
    branches: object,
    depth: int,
    budget: _Budget,
) -> tuple[_ValueContract, ...]:
    """Copy one composition group, which states its branches as an exact bounded tuple."""
    if type(branches) is not tuple or len(branches) > _MAX_CONTRACT_BREADTH:
        _invalid_profile()
    listed = cast("tuple[object, ...]", branches)
    return tuple(_checked_contract_copy(branch, depth + 1, budget) for branch in listed)


@dataclass(frozen=True, slots=True)
class CodexProtocolContract:
    version: str
    methods: Mapping[SemanticMethod, ClientMethodContract]
    server_requests: Mapping[ServerRequestCategory, frozenset[str]]
    unclassified_server_request_count: int
    experimental_schema: bool
    # Server requests stay discoverable by category for later slices, while only a raw
    # method with a profile here can be read into a review and answered.
    approval_profiles: Mapping[str, ApprovalProfile] = MappingProxyType({})
    # The generated notification evidence AgentSession needs.  Older or synthetic bundles
    # may omit ServerNotification.json; those contracts remain useful for other probes but
    # cannot advertise Agent readiness.
    agent_event_profile: AgentEventProfile | None = None
    # This notification is optional evidence. Its absence cannot withdraw Agent admission;
    # only a modern file approval that needs the evidence then fails closed.
    file_change_patch_profile: FileChangePatchProfile | None = None

    def __post_init__(self) -> None:
        budget = _Budget(_MAX_CONTRACT_NODES)
        methods = _contract_mapping(self.methods, budget=budget)
        advertised = _contract_mapping(self.server_requests, budget=budget)
        profiles = _approval_profile_mapping(self.approval_profiles, budget=budget)
        if (
            self.agent_event_profile is not None
            and type(self.agent_event_profile) is not AgentEventProfile
        ):
            raise CodexSchemaError(_INVALID_CONTRACT)
        if (
            self.file_change_patch_profile is not None
            and type(self.file_change_patch_profile) is not FileChangePatchProfile
        ):
            raise CodexSchemaError(_INVALID_CONTRACT)
        server_requests = {
            category: _advertised_methods(names) for category, names in advertised.items()
        }
        object.__setattr__(self, "methods", MappingProxyType(methods))
        object.__setattr__(self, "server_requests", MappingProxyType(server_requests))
        object.__setattr__(self, "approval_profiles", MappingProxyType(profiles))

    def method(self, semantic: SemanticMethod) -> ClientMethodContract | None:
        if type(semantic) is not SemanticMethod:
            return None
        return self.methods.get(semantic)

    def require_method(self, semantic: SemanticMethod) -> ClientMethodContract:
        if type(semantic) is not SemanticMethod:
            raise CodexSchemaError(_REQUIRED_METHOD_UNAVAILABLE)
        try:
            return self.methods[semantic]
        except KeyError as error:
            raise CodexSchemaError(_REQUIRED_METHOD_UNAVAILABLE) from error

    @property
    def missing_methods(self) -> frozenset[SemanticMethod]:
        return frozenset(set(SemanticMethod) - self.methods.keys())

    @property
    def server_request_categories(self) -> frozenset[ServerRequestCategory]:
        return frozenset(self.server_requests)

    def approval_profile(self, method: str) -> ApprovalProfile | None:
        """Return the profile for one raw method, matched by the exact advertised name."""
        if type(method) is not str or not _is_transport_safe(method):
            return None
        profile = self.approval_profiles.get(method)
        return profile if isinstance(profile, ApprovalProfile) else None

    @property
    def adaptable_approval_categories(self) -> frozenset[ServerRequestCategory]:
        """Return the categories every advertised method of which can be read into a review.

        Which advertised alias a live turn sends is not something a client can choose or
        observe in advance, so one readable alias beside an unreadable one is not readiness:
        the unreadable one would arrive mid-turn with nothing safe to answer.
        """
        return frozenset(
            category
            for category, names in self.server_requests.items()
            if names and all(self._method_is_adaptable(name, category) for name in names)
        )

    def _method_is_adaptable(self, method: str, category: ServerRequestCategory) -> bool:
        profile = self.approval_profile(method)
        return profile is not None and profile.category is category


def _contract_mapping[K, V](value: Mapping[K, V], *, budget: _Budget | None = None) -> dict[K, V]:
    """Copy one contract mapping, refusing a container that could rewrite how it is read.

    A discovered contract is built here from plain objects, but the same public value may be
    handed in by a later slice or a test, so only the exact built-in mapping and the
    immutable view this class hands back are copied. The copy itself stays inside a bounded,
    payload-neutral refusal, so nothing a hostile container raises reaches a traceback.
    """
    if type(value) is not dict and type(value) is not MappingProxyType:
        raise CodexSchemaError(_INVALID_CONTRACT)
    try:
        copied = _bounded_mapping_snapshot(
            value,
            max_entries=_MAX_CONTRACT_ENTRIES,
            budget=budget or _Budget(_MAX_CONTRACT_NODES),
        )
    except _BudgetExhaustedError:
        raise CodexSchemaError(_INVALID_CONTRACT) from None
    if copied is None:
        raise CodexSchemaError(_INVALID_CONTRACT) from None
    try:
        keys_are_safe = all(
            (type(key) is str and _is_transport_safe(key))
            or type(key) in (SemanticMethod, ServerRequestCategory)
            for key in copied
        )
    except Exception:  # noqa: BLE001 - an untrusted exact mapping must not leak its failure
        raise CodexSchemaError(_INVALID_CONTRACT) from None
    if not keys_are_safe:
        raise CodexSchemaError(_INVALID_CONTRACT)
    return cast("dict[K, V]", copied)


def _freeze_agent_field_types(
    value: Mapping[str, frozenset[str]],
) -> Mapping[str, frozenset[str]]:
    """Freeze the small scalar type evidence carried by an Agent event profile."""
    if type(value) not in (dict, MappingProxyType):
        raise CodexSchemaError(_INVALID_CONTRACT)
    frozen: dict[str, frozenset[str]] = {}
    for name, types in value.items():
        if (
            type(name) is not str
            or not name
            or type(types) is not frozenset
            or not types
            or not types <= _ALL_JSON_TYPES
        ):
            raise CodexSchemaError(_INVALID_CONTRACT)
        frozen[name] = types
    return MappingProxyType(frozen)


def _approval_profile_mapping(
    value: object,
    *,
    budget: _Budget | None = None,
) -> dict[str, ApprovalProfile]:
    """Copy profile names only after each one is proven to be plain transport-safe text."""
    profiles = _contract_mapping(cast("Mapping[object, object]", value), budget=budget)
    try:
        names_are_safe = all(
            type(method) is str and bool(method) and _is_transport_safe(method)
            for method in profiles
        )
    except Exception:  # noqa: BLE001 - an untrusted profile name must not leak its failure
        raise CodexSchemaError(_INVALID_CONTRACT) from None
    if not names_are_safe:
        raise CodexSchemaError(_INVALID_CONTRACT)
    return cast("dict[str, ApprovalProfile]", profiles)


def _advertised_methods(value: object) -> frozenset[str]:
    """Copy one category's advertised raw method names from an exact built-in set.

    A subclass could answer one membership test two ways, or raise while it is copied, so
    only the two built-in sets are read and the copy is the value that is kept.
    """
    if type(value) is not frozenset and type(value) is not set:
        raise CodexSchemaError(_INVALID_CONTRACT)
    members = cast("set[object]", value)
    if len(members) > _MAX_CONTRACT_ENTRIES:
        raise CodexSchemaError(_INVALID_CONTRACT)
    # Keep the capability layer's existing invalid-metadata reporting for non-string
    # values, but never retain a string subclass or an unencodable string that could later
    # reach a lookup or transport boundary with its overridden protocol.
    names: list[object] = []
    for name in members:
        if isinstance(name, str) and (type(name) is not str or not _is_transport_safe(name)):
            raise CodexSchemaError(_INVALID_CONTRACT)
        names.append(name)
    try:
        return frozenset(cast("str", name) for name in names)
    except Exception:  # noqa: BLE001 - an untrusted exact container must not leak its failure
        raise CodexSchemaError(_INVALID_CONTRACT) from None


@dataclass(frozen=True, slots=True)
class _SemanticSignals:
    params_titles: frozenset[str]
    request_titles: frozenset[str]


@dataclass(frozen=True, slots=True)
class _InvocationSpec:
    """One client method moco can build, and every params object it must be able to send."""

    params_kind: ParamsKind
    params_witnesses: tuple[_ObjectValue, ...] = ()

    @property
    def semantic_fields(self) -> frozenset[str]:
        return frozenset(name for witness in self.params_witnesses for name in witness.fields)


_CLIENT_SIGNALS: dict[SemanticMethod, _SemanticSignals] = {
    SemanticMethod.ACCOUNT_READ: _SemanticSignals(
        frozenset({"GetAccountParams", "AccountReadParams"}),
        frozenset({"Account/readRequest"}),
    ),
    SemanticMethod.CONFIG_READ: _SemanticSignals(
        frozenset({"ConfigReadParams"}),
        frozenset({"Config/readRequest"}),
    ),
    SemanticMethod.CONFIG_REQUIREMENTS_READ: _SemanticSignals(
        frozenset(),
        frozenset({"ConfigRequirements/readRequest"}),
    ),
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: _SemanticSignals(
        frozenset({"ExperimentalFeatureListParams"}),
        frozenset({"ExperimentalFeature/listRequest"}),
    ),
    SemanticMethod.REALTIME_VOICES_LIST: _SemanticSignals(
        frozenset({"ThreadRealtimeListVoicesParams"}),
        frozenset({"Thread/realtime/listVoicesRequest"}),
    ),
    SemanticMethod.THREAD_START: _SemanticSignals(
        frozenset({"ThreadStartParams"}),
        frozenset({"Thread/startRequest"}),
    ),
    SemanticMethod.TURN_START: _SemanticSignals(
        frozenset({"TurnStartParams"}),
        frozenset({"Turn/startRequest"}),
    ),
    SemanticMethod.TURN_STEER: _SemanticSignals(
        frozenset({"TurnSteerParams"}),
        frozenset({"Turn/steerRequest"}),
    ),
    SemanticMethod.TURN_INTERRUPT: _SemanticSignals(
        frozenset({"TurnInterruptParams"}),
        frozenset({"Turn/interruptRequest"}),
    ),
}

_EMPTY_PARAMS = _object_value({})
# moco always starts ephemeral threads rooted at a runtime working directory. The inherit
# profile omits the policy members; each explicit profile pins one supported policy pair.
_THREAD_START_BASE: Mapping[str, _Witness] = MappingProxyType(
    {"cwd": _DYNAMIC_STRING, "ephemeral": _LiteralValue(value=True)}
)


def _explicit_thread_start(sandbox: str, approval_policy: str) -> _ObjectValue:
    return _object_value(
        {
            **_THREAD_START_BASE,
            "sandbox": _LiteralValue(sandbox),
            "approvalPolicy": _LiteralValue(approval_policy),
        }
    )


# One user turn carries exactly one text input item.
_TEXT_INPUT_ITEM = _object_value({"type": _LiteralValue("text"), "text": _DYNAMIC_STRING})

_CLIENT_INVOCATIONS: dict[SemanticMethod, _InvocationSpec] = {
    SemanticMethod.ACCOUNT_READ: _InvocationSpec(ParamsKind.OBJECT, (_EMPTY_PARAMS,)),
    SemanticMethod.CONFIG_READ: _InvocationSpec(
        ParamsKind.OBJECT,
        (_object_value({"cwd": _DYNAMIC_STRING}),),
    ),
    SemanticMethod.CONFIG_REQUIREMENTS_READ: _InvocationSpec(ParamsKind.OMITTED),
    SemanticMethod.EXPERIMENTAL_FEATURE_LIST: _InvocationSpec(
        ParamsKind.OBJECT,
        (
            _object_value({"cursor": _NULL}),
            _object_value({"cursor": _DYNAMIC_STRING}),
        ),
    ),
    SemanticMethod.REALTIME_VOICES_LIST: _InvocationSpec(ParamsKind.OBJECT, (_EMPTY_PARAMS,)),
    SemanticMethod.THREAD_START: _InvocationSpec(
        ParamsKind.OBJECT,
        (
            _object_value(_THREAD_START_BASE),
            _explicit_thread_start("read-only", "never"),
            _explicit_thread_start("workspace-write", "on-request"),
        ),
    ),
    SemanticMethod.TURN_START: _InvocationSpec(
        ParamsKind.OBJECT,
        (
            _object_value(
                {
                    "input": _ArrayValue((_TEXT_INPUT_ITEM,)),
                    "threadId": _DYNAMIC_STRING,
                }
            ),
        ),
    ),
    SemanticMethod.TURN_STEER: _InvocationSpec(
        ParamsKind.OBJECT,
        (
            _object_value(
                {
                    "expectedTurnId": _DYNAMIC_STRING,
                    "input": _ArrayValue((_TEXT_INPUT_ITEM,)),
                    "threadId": _DYNAMIC_STRING,
                }
            ),
        ),
    ),
    SemanticMethod.TURN_INTERRUPT: _InvocationSpec(
        ParamsKind.OBJECT,
        (_object_value({"threadId": _DYNAMIC_STRING, "turnId": _DYNAMIC_STRING}),),
    ),
}

_SERVER_PARAM_CATEGORIES: dict[ServerRequestCategory, frozenset[str]] = {
    ServerRequestCategory.COMMAND_APPROVAL: frozenset(
        {"CommandExecutionRequestApprovalParams", "ExecCommandApprovalParams"}
    ),
    ServerRequestCategory.FILE_CHANGE_APPROVAL: frozenset(
        {"FileChangeRequestApprovalParams", "ApplyPatchApprovalParams"}
    ),
    ServerRequestCategory.USER_INPUT: frozenset({"ToolRequestUserInputParams"}),
    ServerRequestCategory.MCP_ELICITATION: frozenset({"McpServerElicitationRequestParams"}),
    ServerRequestCategory.PERMISSION_APPROVAL: frozenset({"PermissionsRequestApprovalParams"}),
    ServerRequestCategory.DYNAMIC_TOOL_CALL: frozenset({"DynamicToolCallParams"}),
    ServerRequestCategory.AUTH_TOKEN_REFRESH: frozenset({"ChatgptAuthTokensRefreshParams"}),
    ServerRequestCategory.ATTESTATION: frozenset({"AttestationGenerateParams"}),
    ServerRequestCategory.CURRENT_TIME: frozenset({"CurrentTimeReadParams"}),
}


@dataclass(frozen=True, slots=True)
class _FileChangeShape:
    """How one family spells a single changed file inside the changes member it states.

    A reviewer is shown which file is affected, how, and - when the change moves or renames
    it - where it ends up, because all of those decide what accepting authorises. Any other
    member of a change object is one moco cannot explain, so a request carrying it fails
    closed rather than reaching a reviewer described in part.
    """

    kind_member: str
    destination_member: str
    members: frozenset[str]
    # The change objects a build must admit before this family is profiled at all. They
    # prove the schema still spells every kind moco presents and still names the move
    # destination this shape reads.
    witnesses: tuple[tuple[tuple[str, JsonValue], ...], ...]
    # And the ones it must refuse. A change type declares no `additionalProperties`, so a
    # build that stopped declaring the destination would still admit one as an unknown
    # member: only a value the declared destination refuses tells the two apart. Without
    # this, a renamed or dropped destination would be read as absent and never reviewed.
    refused_witnesses: tuple[tuple[tuple[str, JsonValue], ...], ...]


# Values used only to ask a compiled contract what it admits; neither reaches a payload.
_CHANGE_WITNESS_PATH = "moco-witness"
_CHANGE_WITNESS_NON_TEXT = 0
_LEGACY_FILE_CHANGE_SHAPE = _FileChangeShape(
    kind_member="type",
    destination_member="move_path",
    members=frozenset({"type", "content", "unified_diff", "move_path"}),
    witnesses=(
        (("type", "add"), ("content", "")),
        (("type", "delete"), ("content", "")),
        (("type", "update"), ("unified_diff", "")),
        (("type", "update"), ("unified_diff", ""), ("move_path", None)),
        (("type", "update"), ("unified_diff", ""), ("move_path", _CHANGE_WITNESS_PATH)),
    ),
    refused_witnesses=(
        (("type", "update"), ("unified_diff", ""), ("move_path", _CHANGE_WITNESS_NON_TEXT)),
    ),
)


@dataclass(frozen=True, slots=True)
class _ApprovalSpec:
    """One approval family moco knows, named by the params title its schema declares.

    The entry states what each member of that family means and which response document
    proves the decisions. Nothing here is believed on its own: a profile is built only if
    the effective schema declares every named member with a shape the meaning allows, every
    declared member compiles into a checkable contract, and the response document spells
    exactly the vocabulary the entry expects, including a value for each decision moco may
    send. A family whose advertised method has no profile withdraws Stage B readiness, so
    every family a retained build advertises is described here.
    """

    category: ServerRequestCategory
    response_document: str
    correlation: ApprovalCorrelation
    correlation_members: frozenset[str]
    displayed_members: frozenset[str]
    optional_text_members: frozenset[str]
    integer_members: frozenset[str]
    # Checked against its declared type and then dropped, so it can never become
    # correlation, expiry, or authority. Each one names the JSON types its meaning allows,
    # so a build that keeps a familiar name for a different value is not read as this
    # family at all.
    ignored_members: Mapping[str, frozenset[str]]
    absent_or_null_members: frozenset[str]
    decisions: Mapping[ApprovalDecision, tuple[JsonValue, ...]]
    # Values this family's schema may spell that moco never sends: a decision outliving the
    # request under review, and an outcome only the app server itself reports.
    unsent_decisions: frozenset[str]
    unsent_variants: frozenset[str]
    argv_member: str | None = None
    changes_member: str | None = None
    offer_member: str | None = None
    # Stated exactly when this family states its own changed files.
    change_shape: _FileChangeShape | None = None

    @property
    def known_members(self) -> frozenset[str]:
        named = {self.argv_member, self.changes_member, self.offer_member} - {None}
        return (
            self.correlation_members
            | self.displayed_members
            | self.optional_text_members
            | self.integer_members
            | frozenset(self.ignored_members)
            | self.absent_or_null_members
            | frozenset(cast("set[str]", named))
        )


def _one_shot(
    accept: JsonValue, decline: tuple[JsonValue, ...], cancel: JsonValue
) -> Mapping[ApprovalDecision, tuple[JsonValue, ...]]:
    """Name the values one family may answer each reviewer decision with, most exact first."""
    return MappingProxyType(
        {
            ApprovalDecision.ACCEPT: (accept,),
            ApprovalDecision.DECLINE: decline,
            ApprovalDecision.CANCEL: (cancel,),
        }
    )


# The newer families spell one string per decision.
_ONE_SHOT_WIRE = _one_shot("accept", ("decline",), "cancel")
# The legacy families spell the same three refusable decisions in the older vocabulary. Their
# refusal changed shape between retained builds: the older ones spell a plain `denied`, the
# newer one an object carrying the rejection text a reviewer would have typed. moco has no
# such text and must not invent one, so the empty string is sent - and only where that exact
# response schema proves an unconstrained string is a value the member accepts.
_LEGACY_WIRE = _one_shot("approved", ("denied", {"denied": {"rejection": ""}}), "abort")
# The member every observed build names its decision offer with, when it declares one at
# all. No retained legacy bundle does, so the legacy families read the whole one-shot set;
# a build that starts declaring it narrows the offer instead of being left unprofiled.
_OFFER_MEMBER = "availableDecisions"
_LEGACY_UNSENT_DECISIONS = frozenset({"approved_for_session", "timed_out"})
_LEGACY_UNSENT_VARIANTS = frozenset({"approved_execpolicy_amendment", "network_policy_amendment"})
_APPROVAL_CORRELATION = frozenset({"threadId", "turnId", "itemId"})
_LEGACY_CORRELATION = frozenset({"conversationId", "callId"})
_NO_IGNORED_MEMBERS: Mapping[str, frozenset[str]] = MappingProxyType({})
_NULLABLE_TEXT = frozenset({"string", "null"})
_NEW_COMMAND_IGNORED: Mapping[str, frozenset[str]] = MappingProxyType(
    {"approvalId": _NULLABLE_TEXT, "commandActions": frozenset({"array", "null"})}
)
_LEGACY_COMMAND_IGNORED: Mapping[str, frozenset[str]] = MappingProxyType(
    {"approvalId": _NULLABLE_TEXT, "parsedCmd": frozenset({"array"})}
)
_APPROVAL_SPECS: Mapping[str, _ApprovalSpec] = MappingProxyType(
    {
        "CommandExecutionRequestApprovalParams": _ApprovalSpec(
            category=ServerRequestCategory.COMMAND_APPROVAL,
            response_document="CommandExecutionRequestApprovalResponse.json",
            correlation_members=_APPROVAL_CORRELATION,
            correlation=ApprovalCorrelation.THREAD_ITEM,
            displayed_members=frozenset({"command", "cwd"}),
            optional_text_members=frozenset({"reason"}),
            integer_members=frozenset({"startedAtMs"}),
            ignored_members=_NEW_COMMAND_IGNORED,
            absent_or_null_members=frozenset(
                {
                    "additionalPermissions",
                    "environmentId",
                    "networkApprovalContext",
                    "proposedExecpolicyAmendment",
                    "proposedNetworkPolicyAmendments",
                }
            ),
            offer_member=_OFFER_MEMBER,
            decisions=_ONE_SHOT_WIRE,
            unsent_decisions=frozenset({"acceptForSession"}),
            unsent_variants=frozenset(
                {"acceptWithExecpolicyAmendment", "applyNetworkPolicyAmendment"}
            ),
        ),
        "FileChangeRequestApprovalParams": _ApprovalSpec(
            category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
            response_document="FileChangeRequestApprovalResponse.json",
            correlation=ApprovalCorrelation.THREAD_ITEM,
            correlation_members=_APPROVAL_CORRELATION,
            displayed_members=frozenset(),
            optional_text_members=frozenset({"reason"}),
            integer_members=frozenset({"startedAtMs"}),
            ignored_members=_NO_IGNORED_MEMBERS,
            absent_or_null_members=frozenset({"grantRoot"}),
            offer_member=_OFFER_MEMBER,
            decisions=_ONE_SHOT_WIRE,
            unsent_decisions=frozenset({"acceptForSession"}),
            unsent_variants=frozenset(),
        ),
        "ExecCommandApprovalParams": _ApprovalSpec(
            category=ServerRequestCategory.COMMAND_APPROVAL,
            response_document="ExecCommandApprovalResponse.json",
            correlation=ApprovalCorrelation.CONVERSATION_CALL,
            correlation_members=_LEGACY_CORRELATION,
            displayed_members=frozenset({"cwd"}),
            optional_text_members=frozenset({"reason"}),
            integer_members=frozenset(),
            # The parsed command is a friendlier retelling of the argument vector moco
            # already shows, so it is checked and dropped rather than shown twice.
            ignored_members=_LEGACY_COMMAND_IGNORED,
            absent_or_null_members=frozenset(),
            decisions=_LEGACY_WIRE,
            unsent_decisions=_LEGACY_UNSENT_DECISIONS,
            unsent_variants=_LEGACY_UNSENT_VARIANTS,
            argv_member="command",
            offer_member=_OFFER_MEMBER,
        ),
        "ApplyPatchApprovalParams": _ApprovalSpec(
            category=ServerRequestCategory.FILE_CHANGE_APPROVAL,
            response_document="ApplyPatchApprovalResponse.json",
            correlation=ApprovalCorrelation.CONVERSATION_CALL,
            correlation_members=_LEGACY_CORRELATION,
            displayed_members=frozenset(),
            optional_text_members=frozenset({"reason"}),
            integer_members=frozenset(),
            ignored_members=_NO_IGNORED_MEMBERS,
            absent_or_null_members=frozenset({"grantRoot"}),
            decisions=_LEGACY_WIRE,
            unsent_decisions=_LEGACY_UNSENT_DECISIONS,
            unsent_variants=_LEGACY_UNSENT_VARIANTS,
            offer_member=_OFFER_MEMBER,
            changes_member="fileChanges",
            change_shape=_LEGACY_FILE_CHANGE_SHAPE,
        ),
    }
)
# The same four families, keyed by the semantics a profile states rather than the params
# title a bundle spells. Every profile - discovered or hand-built - is checked against the
# one entry its category and correlation name, so both paths read one description.
_FAMILY_SPECS: Mapping[tuple[ServerRequestCategory, ApprovalCorrelation], _ApprovalSpec] = (
    MappingProxyType({(spec.category, spec.correlation): spec for spec in _APPROVAL_SPECS.values()})
)


def _family_spec(
    category: ServerRequestCategory,
    correlation: ApprovalCorrelation,
) -> _ApprovalSpec:
    """Return the one family a category and correlation name, refusing a pairing none states."""
    spec = _FAMILY_SPECS.get((category, correlation))
    if spec is None:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    return spec


def _require_family_shape(
    profile: ApprovalProfile,
    spec: _ApprovalSpec,
    declared: frozenset[str],
) -> None:
    """Refuse a profile whose members or selectors could not describe this family.

    The selectors are compared against the family rather than merely checked for existence:
    a command selector pointing at the offered decisions would show a reviewer the buttons
    as the command, and a suppressed offer selector would present accept for a request that
    only offers to decline. Both name a declared member, so only the family can tell them
    from the real thing.
    """
    if not _approval_shape_matches(spec, declared, profile.required_members):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    if profile.absent_or_null_members != spec.absent_or_null_members & declared:
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    offer_member = spec.offer_member if spec.offer_member in declared else None
    if (profile.argv_member, profile.changes_member, profile.offer_member) != (
        spec.argv_member,
        spec.changes_member,
        offer_member,
    ):
        raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    if offer_member is not None:
        offer_contract = profile.member_contracts[offer_member]
        if offer_contract.items != profile.decision_contract:
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
    for name, contract in profile.member_contracts.items():
        if not _approval_member_admits(spec, name, contract):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)


def _require_family_decisions(
    frozen_decisions: Mapping[ApprovalDecision, _FrozenJson],
    spec: _ApprovalSpec,
    decision_contract: _ValueContract,
) -> None:
    """Refuse a profile answering with a value this family or its own schema would not send.

    Both halves matter. A value the family never lists would answer a one-shot review with a
    standing grant or another build's word for it; a value this build's own decision schema
    refuses would be rejected by the app server after the reviewer already decided.
    """
    for decision, frozen in frozen_decisions.items():
        wire = _materialize_json(frozen)
        key = _json_value_key(wire)
        listed = spec.decisions.get(decision, ())
        if key is None or key not in {_json_value_key(value) for value in listed}:
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)
        if not decision_contract.admits(wire):
            raise CodexSchemaError(_INVALID_APPROVAL_PROFILE)


@dataclass(frozen=True, slots=True)
class _DecisionContract:
    """The decision vocabulary one generated document spells, read whole or not at all.

    The values here are plain JSON, freshly built while the document is read. A profile
    freezes them once when it is constructed, so freezing stays that one boundary's job.
    """

    decisions: Mapping[ApprovalDecision, JsonValue]


class _MalformedDocumentError(ValueError):
    """One generated document Python reads but moco cannot represent, carrying no payload.

    Every parser callback below raises this one exception, and the single method that reads
    a document maps it onto the stable redacted schema or reference error, so no token,
    member name, or path from an untrusted bundle reaches a message or a traceback.
    """


def _reject_non_finite_constant(_token: str) -> Never:
    """Refuse the non-finite constants Python reads as an extension to JSON.

    A generated schema artifact is JSON, so a document spelling one is malformed and is
    rejected while it is parsed, rather than carried into the evaluator as a float.
    """
    raise _MalformedDocumentError


def _read_integer(token: str) -> int:
    """Read one JSON integer, whose conversion the interpreter's digit limit may refuse.

    The refusal is a bare `ValueError` quoting the digit count, so only the fact that the
    document is unreadable is kept.
    """
    try:
        return int(token)
    except ValueError:
        raise _MalformedDocumentError from None


def _read_number(token: str) -> Decimal:
    """Read one JSON number exactly, refusing an exponent no exact finite reading can hold.

    Python's own conversion accepts `1e999` and yields an infinity, which is not a JSON number
    moco can represent, so it is rejected here exactly like a spelled-out non-finite constant.
    Every ordinary finite decimal and exponent passes that bound and is then kept as the
    number the token spells: `9007199254740993.0` rounds to `9007199254740992.0` as a float,
    which would make one listed enum value look like two, and two look like one.
    """
    try:
        bounded = float(token)
    except ValueError:
        raise _MalformedDocumentError from None
    if not isfinite(bounded):
        raise _MalformedDocumentError
    # That bound admits an exponent no exact reading can spell: `1e-9999999999999999999` reads
    # as a finite zero float, while `Decimal` refuses an exponent past its own range with an
    # `InvalidOperation` quoting the token. Refusing it here keeps that refusal payload-free,
    # so every number this returns is the finite JSON number the token spells.
    try:
        return Decimal(token)
    except InvalidOperation:
        raise _MalformedDocumentError from None


def _read_members(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """Read one JSON object, refusing two members that share a name.

    Python silently keeps the last of them, which would let parser overwrite order decide
    how an ambiguous generated document reads, and could turn one into readiness evidence.
    Member order still carries no meaning, so a unique object reads the same in any order.
    """
    members = dict(pairs)
    if len(members) != len(pairs):
        raise _MalformedDocumentError
    return members


class _SchemaResolver:
    def __init__(self, bundle: Path) -> None:
        try:
            self.root = bundle.resolve(strict=True)
        except OSError:
            raise CodexSchemaError(_INVALID_SCHEMA) from None
        if not self.root.is_dir():
            raise CodexSchemaError(_INVALID_SCHEMA)
        self._documents: dict[Path, dict[str, JsonValue]] = {}
        self._document_bytes = 0
        self._remaining_visits = _MAX_SCHEMA_VISITS

    def consume_visit(self) -> None:
        self._remaining_visits -= 1
        if self._remaining_visits < 0:
            raise CodexSchemaError(_INVALID_SCHEMA)

    def root_document(self, name: str) -> tuple[dict[str, JsonValue], Path]:
        path = self.root / name
        document = self._read_document(path, reference=False)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise CodexSchemaError(_INVALID_SCHEMA) from None
        return document, resolved

    def resolve(
        self,
        value: JsonValue,
        base_path: Path,
        stack: _RefStack = (),
    ) -> _ResolvedSchema:
        self.consume_visit()
        schema = _as_schema(value, _INVALID_SCHEMA)
        if "$ref" not in schema:
            return schema, base_path, stack
        if not (schema.keys() - {"$ref"}) <= _ALLOWED_REF_SIBLINGS:
            raise CodexSchemaError(_INVALID_REFERENCE)
        reference = schema["$ref"]
        if not isinstance(reference, str):
            raise CodexSchemaError(_INVALID_REFERENCE)
        target_path, pointer = self._reference_target(reference, base_path)
        key = (target_path, pointer)
        if key in stack or len(stack) >= _MAX_REFERENCE_DEPTH:
            raise CodexSchemaError(_INVALID_REFERENCE)
        document = self._read_document(target_path, reference=True)
        target = self._pointer_value(document, pointer)
        return self.resolve(target, target_path, (*stack, key))

    def reference_names(self, value: JsonValue) -> frozenset[str]:
        self.consume_visit()
        schema = _as_schema(value, _INVALID_SCHEMA)
        reference = schema.get("$ref")
        if reference is None:
            return frozenset()
        if not isinstance(reference, str):
            raise CodexSchemaError(_INVALID_REFERENCE)
        parsed = _safe_urlsplit(reference)
        names: set[str] = set()
        if parsed.path:
            names.add(Path(_safe_unquote(parsed.path)).stem)
        if parsed.fragment:
            tokens = _pointer_tokens(_safe_unquote(parsed.fragment))
            if tokens:
                names.add(tokens[-1])
        return frozenset(name for name in names if name)

    def _reference_target(self, reference: str, base_path: Path) -> tuple[Path, str]:
        if _has_malformed_percent_encoding(reference):
            raise CodexSchemaError(_INVALID_REFERENCE)
        parsed = _safe_urlsplit(reference)
        if parsed.scheme or parsed.netloc or parsed.query:
            raise CodexSchemaError(_INVALID_REFERENCE)
        raw_path = _safe_unquote(parsed.path)
        pure_path = PurePosixPath(raw_path)
        if pure_path.is_absolute() or ".." in pure_path.parts or "\\" in raw_path:
            raise CodexSchemaError(_INVALID_REFERENCE)
        candidate = base_path if not raw_path else base_path.parent / raw_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise CodexSchemaError(_INVALID_REFERENCE) from None
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise CodexSchemaError(_INVALID_REFERENCE)
        pointer = _safe_unquote(parsed.fragment)
        _pointer_tokens(pointer)
        return resolved, pointer

    def _read_document(self, path: Path, *, reference: bool) -> dict[str, JsonValue]:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            message = _INVALID_REFERENCE if reference else _INVALID_SCHEMA
            raise CodexSchemaError(message) from None
        if not resolved.is_relative_to(self.root):
            message = _INVALID_REFERENCE if reference else _INVALID_SCHEMA
            raise CodexSchemaError(message)
        cached = self._documents.get(resolved)
        if cached is not None:
            return cached
        message = _INVALID_REFERENCE if reference else _INVALID_SCHEMA
        try:
            size = resolved.stat().st_size
            if size > _MAX_SCHEMA_DOCUMENT_BYTES:
                raise CodexSchemaError(message)
            self._document_bytes += size
            if self._document_bytes > _MAX_SCHEMA_BUNDLE_BYTES:
                raise CodexSchemaError(message)
            payload = resolved.read_bytes()
            if len(payload) != size:
                raise CodexSchemaError(message)
            raw: object = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_read_members,
                parse_constant=_reject_non_finite_constant,
                parse_float=_read_number,
                parse_int=_read_integer,
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            _MalformedDocumentError,
        ):
            raise CodexSchemaError(message) from None
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise CodexSchemaError(message)
        document = cast("dict[str, JsonValue]", raw)
        self._documents[resolved] = document
        return document

    def _pointer_value(self, document: dict[str, JsonValue], pointer: str) -> JsonValue:
        current: JsonValue = document
        for token in _pointer_tokens(pointer):
            self.consume_visit()
            if isinstance(current, dict):
                if token not in current:
                    raise CodexSchemaError(_INVALID_REFERENCE)
                current = current[token]
            elif isinstance(current, list):
                max_index_digits = len(str(max(len(current) - 1, 0)))
                if (
                    not token.isascii()
                    or not token.isdecimal()
                    or (len(token) > 1 and token.startswith("0"))
                    or len(token) > max_index_digits
                ):
                    raise CodexSchemaError(_INVALID_REFERENCE)
                try:
                    index = int(token)
                except ValueError:
                    raise CodexSchemaError(_INVALID_REFERENCE) from None
                if index >= len(current):
                    raise CodexSchemaError(_INVALID_REFERENCE)
                current = current[index]
            else:
                raise CodexSchemaError(_INVALID_REFERENCE)
        return current


def load_generated_contract(  # noqa: C901, PLR0912
    bundle: Path,
    *,
    version: str,
    experimental_schema: bool = True,
) -> CodexProtocolContract:
    resolver = _SchemaResolver(bundle)
    client_root, client_path = resolver.root_document("ClientRequest.json")
    server_root, server_path = resolver.root_document("ServerRequest.json")
    try:
        notification_root, notification_path = resolver.root_document("ServerNotification.json")
    except CodexSchemaError:
        # A partial/synthetic bundle can still be useful to the Stage A capability probes.
        # Its Agent admission is withdrawn later because no completion evidence was proved.
        notification_root = None
        notification_path = None
    client_variants = _one_of(client_root)
    server_variants = _one_of(server_root)

    methods: dict[SemanticMethod, ClientMethodContract] = {}
    classified_semantics: set[SemanticMethod] = set()
    for raw_variant in client_variants:
        match = _classify_client_variant(raw_variant, client_path, resolver)
        if match is None:
            continue
        semantic, contract = match
        if semantic in classified_semantics:
            raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
        classified_semantics.add(semantic)
        if contract is not None:
            methods[semantic] = contract

    collected: dict[ServerRequestCategory, set[str]] = {}
    # One raw server method must belong to exactly one category, otherwise adapter routing
    # of an inbound request would be ambiguous while admission still reported available.
    owners: dict[str, ServerRequestCategory] = {}
    profiles: dict[str, ApprovalProfile] = {}
    unclassified = 0
    for raw_variant in server_variants:
        server_match = _classify_server_variant(raw_variant, server_path, resolver)
        if server_match is None:
            unclassified += 1
            continue
        category, method, profile = server_match
        if owners.setdefault(method, category) is not category:
            raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
        collected.setdefault(category, set()).add(method)
        if profile is not None:
            profiles[method] = profile

    agent_event_profile = None
    if notification_root is not None and notification_path is not None:
        try:
            agent_event_profile = _build_agent_event_profile(
                notification_root,
                notification_path,
                resolver,
            )
        except CodexSchemaError:
            # Keep discovery of the rest of the bundle, but never turn an unreadable event
            # contract into Agent readiness by guessing its aliases or fields.
            agent_event_profile = None

    file_change_patch_profile = None
    if notification_root is not None and notification_path is not None:
        try:
            file_change_patch_profile = _build_file_change_patch_profile(
                notification_root,
                notification_path,
                resolver,
            )
        except CodexSchemaError:
            # Patch evidence is optional for Agent admission. A build whose patch event
            # cannot be read simply cannot explain the modern file approval at runtime.
            file_change_patch_profile = None

    return CodexProtocolContract(
        version=version,
        methods=methods,
        server_requests={category: frozenset(names) for category, names in collected.items()},
        unclassified_server_request_count=unclassified,
        experimental_schema=experimental_schema,
        approval_profiles=profiles,
        agent_event_profile=agent_event_profile,
        file_change_patch_profile=file_change_patch_profile,
    )


_FILE_CHANGE_PATCH_TITLE = "FileChangePatchUpdatedNotification"
_CORE_AGENT_EVENT_TITLES = frozenset(
    {
        "TurnCompletedNotification",
        "ItemCompletedNotification",
        "AgentMessageDeltaNotification",
    }
)
_FILE_CHANGE_PATCH_FIELDS = frozenset({"changes", "itemId", "threadId", "turnId"})
_FILE_CHANGE_ENTRY_FIELDS = frozenset({"diff", "kind", "path"})
_FILE_CHANGE_KIND_VARIANT_COUNT = 3


def _build_file_change_patch_profile(
    root: dict[str, JsonValue],
    base_path: Path,
    resolver: _SchemaResolver,
) -> FileChangePatchProfile:
    candidates: list[tuple[str, _ResolvedSchema]] = []
    method_counts: dict[str, int] = {}
    for raw_variant in _one_of(root):
        variant, variant_path, variant_stack = resolver.resolve(raw_variant, base_path)
        properties = _properties(variant)
        method_raw = properties.get("method")
        params_raw = properties.get("params")
        if method_raw is None or params_raw is None:
            continue
        method_schema, _, _ = resolver.resolve(method_raw, variant_path, variant_stack)
        method = _transport_method(method_schema)
        method_counts[method] = method_counts.get(method, 0) + 1
        params = resolver.resolve(params_raw, variant_path, variant_stack)
        titles = _schema_titles(params_raw, params[0], resolver)
        if _FILE_CHANGE_PATCH_TITLE not in titles:
            continue
        if titles & _CORE_AGENT_EVENT_TITLES:
            raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
        candidates.append((method, params))
    if len(candidates) != 1:
        raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
    method, params = candidates[0]
    if method_counts.get(method) != 1:
        raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
    _require_file_change_patch_shape(params, resolver)
    schema, path, stack = params
    contract = _compile_contract(schema, path, stack, resolver, _contract_budget())
    return FileChangePatchProfile(method=method, params_contract=contract)


def _require_file_change_patch_shape(
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> None:
    required, types, _ = _event_object_profile(params, resolver)
    if required != _FILE_CHANGE_PATCH_FIELDS:
        raise CodexSchemaError(_INVALID_SCHEMA)
    _event_require_fields(
        required,
        types,
        {name: frozenset({"string"}) for name in ("itemId", "threadId", "turnId")}
        | {"changes": frozenset({"array"})},
    )
    changes = _event_property(params, "changes", resolver)
    if changes is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    change = _event_array_item(changes, resolver)
    change_required, change_types, _ = _event_object_profile(change, resolver)
    if change_required != _FILE_CHANGE_ENTRY_FIELDS:
        raise CodexSchemaError(_INVALID_SCHEMA)
    _event_require_fields(
        change_required,
        change_types,
        {
            "diff": frozenset({"string"}),
            "kind": frozenset({"object"}),
            "path": frozenset({"string"}),
        },
    )
    kind = _event_property(change, "kind", resolver)
    if kind is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    _require_file_change_kind_shape(kind, resolver)


def _event_array_item(
    resolved: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> _ResolvedSchema:
    if _event_types(resolved, resolver) != frozenset({"array"}):
        raise CodexSchemaError(_INVALID_SCHEMA)
    schema, path, stack = resolved
    raw = schema.get("items")
    if raw is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    return resolver.resolve(raw, path, stack)


def _require_file_change_kind_shape(
    resolved: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> None:
    schema, path, stack = resolved
    raw_branches = schema.get("oneOf")
    if not isinstance(raw_branches, list) or len(raw_branches) != _FILE_CHANGE_KIND_VARIANT_COUNT:
        raise CodexSchemaError(_INVALID_SCHEMA)
    variants: dict[str, tuple[frozenset[str], Mapping[str, frozenset[str]]]] = {}
    for raw_branch in raw_branches:
        branch = resolver.resolve(raw_branch, path, stack)
        required, types, branch_schema = _event_object_profile(branch, resolver)
        type_field = _event_property(branch_schema, "type", resolver)
        if type_field is None:
            raise CodexSchemaError(_INVALID_SCHEMA)
        values = _event_enum_values(type_field, resolver)
        if len(values) != 1:
            raise CodexSchemaError(_INVALID_SCHEMA)
        value = next(iter(values))
        if value in variants:
            raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
        variants[value] = (required, types)
    if variants.keys() != {"add", "delete", "update"}:
        raise CodexSchemaError(_INVALID_SCHEMA)
    for value in ("add", "delete"):
        required, types = variants[value]
        if required != frozenset({"type"}) or set(types) != {"type"}:
            raise CodexSchemaError(_INVALID_SCHEMA)
    update_required, update_types = variants["update"]
    if (
        update_required != frozenset({"type"})
        or set(update_types) != {"move_path", "type"}
        or update_types["move_path"] != frozenset({"string", "null"})
    ):
        raise CodexSchemaError(_INVALID_SCHEMA)


def _build_agent_event_profile(  # noqa: C901, PLR0912, PLR0915
    root: dict[str, JsonValue],
    base_path: Path,
    resolver: _SchemaResolver,
) -> AgentEventProfile:
    """Compile only the notification evidence required by AgentSession.

    The generated bundle owns aliases, required members, enum values, and scalar shapes.  No
    complete ThreadItem catalog is copied here: an item with a valid id/type that is not the
    schema-proven agentMessage is generic progress and cannot settle a turn.
    """
    signal_titles: Mapping[str, frozenset[str]] = MappingProxyType(
        {
            "turn_completed": frozenset({"TurnCompletedNotification"}),
            "item_completed": frozenset({"ItemCompletedNotification"}),
            "agent_message_delta": frozenset({"AgentMessageDeltaNotification"}),
            "item_started": frozenset({"ItemStartedNotification"}),
        }
    )
    events: dict[str, tuple[str, _ResolvedSchema]] = {}
    ambiguous_optional_events: set[str] = set()
    for raw_variant in _one_of(root):
        variant, variant_path, variant_stack = resolver.resolve(raw_variant, base_path)
        properties = _properties(variant)
        method_raw = properties.get("method")
        params_raw = properties.get("params")
        if method_raw is None or params_raw is None:
            continue
        method_schema, _, _ = resolver.resolve(method_raw, variant_path, variant_stack)
        params = resolver.resolve(params_raw, variant_path, variant_stack)
        method = _transport_method(method_schema)
        titles = _schema_titles(params_raw, params[0], resolver)
        matches = [semantic for semantic, accepted in signal_titles.items() if titles & accepted]
        if len(matches) > 1:
            raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
        if not matches:
            continue
        semantic = matches[0]
        if semantic in ambiguous_optional_events:
            continue
        if semantic in events:
            if semantic == "item_started":
                events.pop(semantic)
                ambiguous_optional_events.add(semantic)
                continue
            raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
        events[semantic] = (method, params)

    required_events = {"turn_completed", "item_completed"}
    if not required_events <= events.keys():
        raise CodexSchemaError(_INVALID_SCHEMA)

    turn_completed_method, turn_completed = events["turn_completed"]
    item_completed_method, item_completed = events["item_completed"]
    turn_completed_required, turn_completed_types, _ = _event_object_profile(
        turn_completed,
        resolver,
    )
    item_completed_required, item_completed_types, _ = _event_object_profile(
        item_completed,
        resolver,
    )
    _event_require_fields(
        turn_completed_required,
        turn_completed_types,
        {"threadId": frozenset({"string"}), "turn": frozenset({"object"})},
    )
    _event_require_fields(
        item_completed_required,
        item_completed_types,
        {
            "threadId": frozenset({"string"}),
            "turnId": frozenset({"string"}),
            "item": frozenset({"object"}),
        },
    )

    turn_field = _event_property(turn_completed, "turn", resolver)
    if turn_field is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    turn_required, turn_types, turn_schema = _event_object_profile(turn_field, resolver)
    _event_require_fields(
        turn_required,
        turn_types,
        {
            "id": frozenset({"string"}),
            "items": frozenset({"array"}),
            "status": frozenset({"string"}),
        },
    )
    status_field = _event_property(turn_schema, "status", resolver)
    if status_field is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    status_values = _event_enum_values(status_field, resolver)
    required_statuses = frozenset({"completed", "interrupted", "failed", "inProgress"})
    if not required_statuses <= status_values:
        raise CodexSchemaError(_INVALID_SCHEMA)

    item_field = _event_property(item_completed, "item", resolver)
    if item_field is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    agent_message = _agent_message_branch(item_field, resolver)
    agent_required, agent_types, agent_schema = _event_object_profile(agent_message, resolver)
    _event_require_fields(
        agent_required,
        agent_types,
        {
            "id": frozenset({"string"}),
            "type": frozenset({"string"}),
            "text": frozenset({"string"}),
        },
    )
    type_field = _event_property(agent_schema, "type", resolver)
    if type_field is None or _event_enum_values(type_field, resolver) != frozenset(
        {"agentMessage"}
    ):
        raise CodexSchemaError(_INVALID_SCHEMA)
    phase_field = _event_property(agent_schema, "phase", resolver)
    if phase_field is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    phase_values = _event_enum_values(phase_field, resolver)
    if not {"commentary", "final_answer"} <= phase_values:
        raise CodexSchemaError(_INVALID_SCHEMA)
    phase_types = agent_types.get("phase")
    if phase_types is None or not phase_types <= frozenset({"string", "null"}):
        raise CodexSchemaError(_INVALID_SCHEMA)

    delta_method: str | None = None
    delta_types: Mapping[str, frozenset[str]] = {}
    delta_required: frozenset[str] = frozenset()
    if "agent_message_delta" in events:
        delta_method, delta = events["agent_message_delta"]
        delta_required, delta_types, _ = _event_object_profile(delta, resolver)
        _event_require_fields(
            delta_required,
            delta_types,
            {
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
                "itemId": frozenset({"string"}),
                "delta": frozenset({"string"}),
            },
        )

    item_started_method: str | None = None
    item_started_required: frozenset[str] = frozenset()
    item_started_types: Mapping[str, frozenset[str]] = {}
    if "item_started" in events:
        try:
            item_started_method, item_started = events["item_started"]
            item_started_required, item_started_types, _ = _event_object_profile(
                item_started,
                resolver,
            )
            _event_require_fields(
                item_started_required,
                item_started_types,
                {
                    "threadId": frozenset({"string"}),
                    "turnId": frozenset({"string"}),
                    "item": frozenset({"object"}),
                },
            )
        except CodexSchemaError:
            item_started_method = None
            item_started_required = frozenset()
            item_started_types = {}
    if item_started_method in {
        turn_completed_method,
        item_completed_method,
        delta_method,
    }:
        item_started_method = None
        item_started_required = frozenset()
        item_started_types = {}

    return AgentEventProfile(
        turn_completed_method=turn_completed_method,
        item_completed_method=item_completed_method,
        agent_message_delta_method=delta_method,
        turn_completed_required_fields=turn_completed_required,
        item_completed_required_fields=item_completed_required,
        turn_required_fields=turn_required,
        agent_message_required_fields=agent_required,
        turn_completed_field_types=turn_completed_types,
        item_completed_field_types=item_completed_types,
        turn_field_types=turn_types,
        agent_message_field_types=agent_types,
        agent_message_delta_required_fields=delta_required,
        agent_message_delta_field_types=delta_types,
        agent_message_phase_values=phase_values,
        agent_message_phase_optional="phase" not in agent_required,
        turn_status_values=status_values,
        completed_status="completed",
        interrupted_status="interrupted",
        failed_status="failed",
        in_progress_status="inProgress",
        item_started_method=item_started_method,
        item_started_required_fields=item_started_required,
        item_started_field_types=item_started_types,
    )


def _event_object_profile(
    resolved: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> tuple[frozenset[str], Mapping[str, frozenset[str]], _ResolvedSchema]:
    schema, _, _ = resolved
    if _event_types(resolved, resolver) != frozenset({"object"}):
        raise CodexSchemaError(_INVALID_SCHEMA)
    required = _required_fields(schema)
    if required is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    properties = _properties(schema)
    types: dict[str, frozenset[str]] = {}
    for name, raw in properties.items():
        child = resolver.resolve(raw, resolved[1], resolved[2])
        child_types = _event_types(child, resolver)
        if not child_types:
            raise CodexSchemaError(_INVALID_SCHEMA)
        types[name] = child_types
    return required, types, resolved


def _event_property(
    resolved: _ResolvedSchema,
    name: str,
    resolver: _SchemaResolver,
) -> _ResolvedSchema | None:
    schema, path, stack = resolved
    raw = _properties(schema).get(name)
    return None if raw is None else resolver.resolve(raw, path, stack)


def _event_require_fields(
    required: frozenset[str],
    types: Mapping[str, frozenset[str]],
    expected: Mapping[str, frozenset[str]],
) -> None:
    if not set(expected) <= required:
        raise CodexSchemaError(_INVALID_SCHEMA)
    for name, expected_types in expected.items():
        if types.get(name) != expected_types:
            raise CodexSchemaError(_INVALID_SCHEMA)


def _event_types(resolved: _ResolvedSchema, resolver: _SchemaResolver) -> frozenset[str]:
    """Return the JSON types a small event field can carry, following compositions."""
    schema, path, stack = resolved
    declared = _declared_types(schema)
    if "type" in schema and declared is None:
        raise CodexSchemaError(_INVALID_SCHEMA)
    result = _ALL_JSON_TYPES if declared is None else declared
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        if not isinstance(branches, list) or not branches:
            raise CodexSchemaError(_INVALID_SCHEMA)
        union: set[str] = set()
        for raw in branches:
            union.update(_event_types(resolver.resolve(raw, path, stack), resolver))
        result &= frozenset(union)
    if "allOf" in schema:
        branches = schema["allOf"]
        if not isinstance(branches, list) or not branches:
            raise CodexSchemaError(_INVALID_SCHEMA)
        for raw in branches:
            result &= _event_types(resolver.resolve(raw, path, stack), resolver)
    if "string" in result and not _event_string_has_witness(resolved, resolver):
        result -= {"string"}
    return result


def _event_string_has_witness(
    resolved: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> bool:
    schema, path, stack = resolved
    contract = _compile_contract(schema, path, stack, resolver, _contract_budget())
    candidates = (*_event_enum_values(resolved, resolver), "", "moco-contract-witness")
    return any(contract.admits(candidate) for candidate in candidates)


def _event_enum_values(resolved: _ResolvedSchema, resolver: _SchemaResolver) -> frozenset[str]:
    """Return the schema-admitted finite vocabulary for one event string field."""
    schema, path, stack = resolved
    values: set[str] = set()
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum or not all(type(value) is str for value in enum):
            raise CodexSchemaError(_INVALID_SCHEMA)
        values.update(cast("list[str]", enum))
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        if not isinstance(branches, list) or not branches:
            raise CodexSchemaError(_INVALID_SCHEMA)
        for raw in branches:
            values.update(_event_enum_values(resolver.resolve(raw, path, stack), resolver))
    contract = _compile_contract(schema, path, stack, resolver, _contract_budget())
    return frozenset(value for value in values if contract.admits(value))


def _agent_message_branch(
    resolved: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> _ResolvedSchema:
    schema, path, stack = resolved
    branches = schema.get("oneOf")
    if not isinstance(branches, list) or not branches:
        raise CodexSchemaError(_INVALID_SCHEMA)
    matches: list[_ResolvedSchema] = []
    for raw in branches:
        branch = resolver.resolve(raw, path, stack)
        type_field = _event_property(branch, "type", resolver)
        if type_field is not None and _event_enum_values(type_field, resolver) == frozenset(
            {"agentMessage"}
        ):
            matches.append(branch)
    if len(matches) != 1:
        raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
    return matches[0]


def _classify_client_variant(
    raw_variant: JsonValue,
    base_path: Path,
    resolver: _SchemaResolver,
) -> tuple[SemanticMethod, ClientMethodContract | None] | None:
    variant, variant_path, variant_stack = resolver.resolve(raw_variant, base_path)
    properties = _properties(variant)
    method_raw = properties.get("method")
    params_raw = properties.get("params")
    if method_raw is None or params_raw is None:
        return None
    method_schema, _, _ = resolver.resolve(method_raw, variant_path, variant_stack)
    params_schema, params_path, params_stack = resolver.resolve(
        params_raw,
        variant_path,
        variant_stack,
    )
    method = _transport_method(method_schema)

    param_titles = _schema_titles(params_raw, params_schema, resolver)
    request_titles = _request_titles(raw_variant, variant, method_raw, method_schema)
    param_matches = {
        semantic
        for semantic, signals in _CLIENT_SIGNALS.items()
        if param_titles & signals.params_titles
    }
    request_matches = {
        semantic
        for semantic, signals in _CLIENT_SIGNALS.items()
        if request_titles & signals.request_titles
    }
    if len(param_matches) > 1 or len(request_matches) > 1:
        raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
    if param_matches and request_matches and param_matches != request_matches:
        raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
    if param_matches:
        semantic = next(iter(param_matches))
    elif request_matches:
        semantic = next(iter(request_matches))
        if _CLIENT_INVOCATIONS[semantic].params_kind is not ParamsKind.OMITTED:
            return None
    else:
        return None
    spec = _CLIENT_INVOCATIONS[semantic]
    if (
        semantic is SemanticMethod.TURN_STEER
        and _required_fields(params_schema) != spec.semantic_fields
    ):
        return semantic, None
    contract = _validate_invocation(
        method,
        spec,
        (variant, variant_path, variant_stack),
        (params_schema, params_path, params_stack),
        resolver,
    )
    return semantic, contract


def _classify_server_variant(
    raw_variant: JsonValue,
    base_path: Path,
    resolver: _SchemaResolver,
) -> tuple[ServerRequestCategory, str, ApprovalProfile | None] | None:
    variant, variant_path, variant_stack = resolver.resolve(raw_variant, base_path)
    properties = _properties(variant)
    method_raw = properties.get("method")
    params_raw = properties.get("params")
    if method_raw is None or params_raw is None:
        return None
    method_schema, _, _ = resolver.resolve(method_raw, variant_path, variant_stack)
    params = resolver.resolve(params_raw, variant_path, variant_stack)
    method = _transport_method(method_schema)
    titles = _schema_titles(params_raw, params[0], resolver)
    matches = {
        category
        for category, accepted_titles in _SERVER_PARAM_CATEGORIES.items()
        if titles & accepted_titles
    }
    if len(matches) > 1:
        raise CodexSchemaError(_AMBIGUOUS_SCHEMA)
    if not matches:
        return None
    category = next(iter(matches))
    return category, method, _approval_profile(category, titles, params, resolver)


def _approval_profile(
    category: ServerRequestCategory,
    titles: frozenset[str],
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> ApprovalProfile | None:
    """Build the profile for one approval method, or none when this build is not readable.

    A build that cannot be read here stays discoverable by category and unadaptable, so an
    unreadable approval family withdraws readiness instead of failing the whole probe.
    """
    matched = titles & _APPROVAL_SPECS.keys()
    if len(matched) != 1:
        return None
    spec = _APPROVAL_SPECS[next(iter(matched))]
    if spec.category is not category:  # pragma: no cover - one title names one category
        return None
    try:
        return _build_approval_profile(spec, params, resolver)
    except CodexSchemaError:
        return None


def _build_approval_profile(
    spec: _ApprovalSpec,
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> ApprovalProfile | None:
    members = _approval_members(spec, params, resolver)
    if members is None:
        return None
    contracts, required, offer_member = members
    answered = _response_decisions(spec, resolver)
    if answered is None:
        return None
    decisions, decision_contract = answered
    # The offer lists values of the same decision type. Reading it from the params document
    # keeps the offered vocabulary and the answerable one provably the same document's.
    if offer_member is not None:
        offered = _offered_decision_contract(spec, params, resolver, offer_member)
        if offered is None or offered != decision_contract:
            return None
    declared = frozenset(contracts)
    return ApprovalProfile(
        category=spec.category,
        correlation=spec.correlation,
        required_members=required,
        absent_or_null_members=spec.absent_or_null_members & declared,
        member_contracts=contracts,
        argv_member=spec.argv_member,
        changes_member=spec.changes_member,
        offer_member=offer_member,
        decisions=dict(decisions),
        decision_contract=decision_contract,
    )


def _approval_members(
    spec: _ApprovalSpec,
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> tuple[dict[str, _ValueContract], frozenset[str], str | None] | None:
    """Compile what this build declares, refusing a member it cannot explain or check."""
    schema, base_path, stack = params
    if _possible_types(schema) != frozenset({"object"}):
        return None
    required = _required_fields(schema)
    properties = _properties(schema)
    declared = frozenset(properties)
    if required is None or not _approval_shape_matches(spec, declared, required):
        return None
    contracts: dict[str, _ValueContract] = {}
    for name, raw_member in properties.items():
        contract = _compile_contract(raw_member, base_path, stack, resolver, _contract_budget())
        if not _approval_member_admits(spec, name, contract):
            return None
        contracts[name] = contract
    offer_member = spec.offer_member if spec.offer_member in declared else None
    return contracts, required, offer_member


def _approval_shape_matches(
    spec: _ApprovalSpec,
    declared: frozenset[str],
    required: frozenset[str],
) -> bool:
    """Report whether one build declares and requires the members this family needs."""
    named = {member for member in (spec.argv_member, spec.changes_member) if member is not None}
    return (
        required <= declared
        and declared <= spec.known_members
        and spec.correlation_members <= required
        and spec.displayed_members <= declared
        and named <= declared
        and required.isdisjoint(spec.absent_or_null_members)
    )


def _contract_budget() -> _Budget:
    return _Budget(_MAX_CONTRACT_NODES)


def _approval_member_admits(spec: _ApprovalSpec, name: str, contract: _ValueContract) -> bool:
    """Report whether one declared member can hold what its meaning in this family needs."""
    accepted = _contract_types(contract)
    scalar = _approval_scalar_member_admits(spec, name, contract, accepted)
    if scalar is not None:
        return scalar
    return _approval_structured_member_admits(spec, name, contract, accepted)


def _usable_string_contract(contract: _ValueContract) -> bool:
    """Report whether a semantic string is open enough to carry a live identifier/value."""
    return (
        _contract_types(contract) == frozenset({"string"})
        and contract.enum is None
        and contract.const is None
        and not contract.all_of
        and not contract.any_of
        and not contract.one_of
    )


def _approval_scalar_member_admits(
    spec: _ApprovalSpec,
    name: str,
    contract: _ValueContract,
    accepted: frozenset[str],
) -> bool | None:
    """Judge a member read as one value, or report that this name means something else."""
    if name in spec.correlation_members:
        return _usable_string_contract(contract)
    if name in spec.displayed_members or name in spec.optional_text_members:
        return "string" in accepted and accepted <= frozenset({"string", "null"})
    if name in spec.integer_members:
        return "integer" in accepted and accepted <= frozenset({"integer", "null"})
    return None


def _approval_structured_member_admits(
    spec: _ApprovalSpec,
    name: str,
    contract: _ValueContract,
    accepted: frozenset[str],
) -> bool:
    """Judge a member read as a list, a map, an offer, or one moco checks and then drops."""
    if name == spec.argv_member:
        return (
            accepted == frozenset({"array"})
            and contract.items is not None
            and _usable_string_contract(contract.items)
        )
    if name == spec.changes_member:
        return accepted == frozenset({"object"}) and _changes_member_admits(spec, contract)
    if name in spec.absent_or_null_members:
        return "null" in accepted
    if name == spec.offer_member:
        return "array" in accepted and accepted <= frozenset({"array", "null"})
    allowed = spec.ignored_members.get(name)
    return allowed is not None and bool(accepted) and accepted <= allowed


def _changes_member_admits(spec: _ApprovalSpec, contract: _ValueContract) -> bool:
    """Report whether this build still spells every changed file moco would have to present.

    A reviewer decides on the whole effect of a patch, so a build whose change shape no
    longer admits one of these - a move stating where the file ends up above all - leaves
    the family unprofiled. Advertising it while dropping a destination would ask for accept
    on an operation the review never showed.
    """
    shape = spec.change_shape
    if shape is None:  # pragma: no cover - a changes member and its shape are stated together
        return False
    return (
        _change_update_variant_owns_destination(contract, shape)
        and all(
            contract.admits({_CHANGE_WITNESS_PATH: dict(witness)}) for witness in shape.witnesses
        )
        and not any(
            contract.admits({_CHANGE_WITNESS_PATH: dict(witness)})
            for witness in shape.refused_witnesses
        )
    )


def _change_update_variant_owns_destination(
    contract: _ValueContract,
    shape: _FileChangeShape,
) -> bool:
    """Require the update branch to declare its destination, not merely accept an extra key."""
    change_contract = contract.additional
    if change_contract is None:
        return False
    update_variants = [
        branch
        for branch in change_contract.one_of
        if _contract_has_exact_string(branch.properties.get(shape.kind_member), "update")
    ]
    if len(update_variants) != 1:
        return False
    destination = update_variants[0].properties.get(shape.destination_member)
    return destination is not None and _nullable_open_string_contract(destination)


def _contract_has_exact_string(contract: _ValueContract | None, value: str) -> bool:
    if contract is None or _contract_types(contract) != frozenset({"string"}):
        return False
    if contract.all_of or contract.any_of or contract.one_of:
        return False
    key = _json_value_key(value)
    return key is not None and (contract.const == (key,) or contract.enum == (key,))


def _nullable_open_string_contract(contract: _ValueContract) -> bool:
    return (
        _contract_types(contract) == frozenset({"string", "null"})
        and contract.enum is None
        and contract.const is None
        and not contract.all_of
        and not contract.any_of
        and not contract.one_of
    )


def _response_decisions(
    spec: _ApprovalSpec,
    resolver: _SchemaResolver,
) -> tuple[Mapping[ApprovalDecision, JsonValue], _ValueContract] | None:
    """Read the generated response document this family answers with.

    A decision is answerable only when this document spells it, and only when the whole
    compiled response value - the object moco would send - satisfies that document. A
    vocabulary that names a decision while another assertion contradicts it is therefore
    not a decision moco can send, and leaves the family unprofiled.
    """
    document, path = resolver.root_document(spec.response_document)
    if _possible_types(document) != frozenset({"object"}):
        return None
    if _required_fields(document) != frozenset({"decision"}):
        return None
    properties = _properties(document)
    if properties.keys() != {"decision"}:
        return None
    vocabulary = _decision_contract(spec, properties["decision"], path, (), resolver)
    if vocabulary is None:
        return None
    document_contract = _compile_contract(document, path, (), resolver, _contract_budget())
    decision_contract = _compile_contract(
        properties["decision"],
        path,
        (),
        resolver,
        _contract_budget(),
    )
    for value in vocabulary.decisions.values():
        # The value moco would send is the fresh one a response is built from, so that is
        # the value the whole response document must admit.
        if not document_contract.admits({"decision": value}):
            return None
    return vocabulary.decisions, decision_contract


def _offered_decision_contract(
    spec: _ApprovalSpec,
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
    offer_member: str,
) -> _ValueContract | None:
    """Read the decision type the params offer member lists, when this build declares one."""
    schema, base_path, stack = params
    raw_member = _properties(schema)[offer_member]
    resolved, path, ref_stack = resolver.resolve(raw_member, base_path, stack)
    items = resolved.get("items")
    if items is None:
        return None
    if _decision_contract(spec, items, path, ref_stack, resolver) is None:
        return None
    return _compile_contract(items, path, ref_stack, resolver, _contract_budget())


def _decision_contract(
    spec: _ApprovalSpec,
    raw_decision: JsonValue,
    base_path: Path,
    stack: _RefStack,
    resolver: _SchemaResolver,
) -> _DecisionContract | None:
    """Read one decision type whole, refusing a variant this adapter could not answer."""
    schema, path, ref_stack = resolver.resolve(raw_decision, base_path, stack)
    variants = schema.get("oneOf")
    if not isinstance(variants, list) or not variants:
        return None
    spelled: set[str] = set()
    named: dict[str, frozenset[str]] = {}
    for raw_variant in variants:
        variant, variant_path, variant_stack = resolver.resolve(raw_variant, path, ref_stack)
        wire = _decision_wire_value(variant)
        if wire is not None:
            if wire in spelled or wire in named:
                return None
            spelled.add(wire)
            continue
        object_variant = _decision_object_variant(variant, variant_path, variant_stack, resolver)
        if object_variant is None or object_variant[0] in named or object_variant[0] in spelled:
            return None
        named[object_variant[0]] = object_variant[1]
    return _answerable_decisions(spec, spelled, named)


def _answerable_decisions(
    spec: _ApprovalSpec,
    spelled: set[str],
    named: dict[str, frozenset[str]],
) -> _DecisionContract | None:
    """Choose the value each reviewer decision is sent as, from what this build spells.

    A family may spell one decision differently between builds, so the entry lists the
    values it may take in order and the first one this vocabulary proves is chosen. Every
    remaining value must be one the entry already knows moco never sends, otherwise this
    build offers a decision moco only half understands and the family stays unprofiled.
    """
    chosen: dict[ApprovalDecision, JsonValue] = {}
    answered: set[str] = set()
    for decision, candidates in spec.decisions.items():
        picked = _decision_candidate(candidates, spelled, named)
        if picked is None:
            return None
        name, value = picked
        if name in answered:
            return None
        answered.add(name)
        # Freezing and rebuilding checks the value moco would send and yields a fresh one,
        # so no listed template is shared with the profile that answers with it.
        chosen[decision] = _materialize_json(_freeze_json(value))
    if not (spelled - answered) <= spec.unsent_decisions:
        return None
    if not (named.keys() - answered) <= spec.unsent_variants:
        return None
    return _DecisionContract(MappingProxyType(chosen))


def _decision_candidate(
    candidates: tuple[JsonValue, ...],
    spelled: set[str],
    named: dict[str, frozenset[str]],
) -> tuple[str, JsonValue] | None:
    """Return the first listed value this vocabulary spells, named by its variant."""
    for candidate in candidates:
        if isinstance(candidate, str):
            if candidate in spelled:
                return candidate, candidate
            continue
        if not isinstance(candidate, dict) or len(candidate) != 1:  # pragma: no cover - table
            continue
        name, members = next(iter(candidate.items()))
        if isinstance(members, dict) and named.get(name) == frozenset(members):
            return name, candidate
    return None


def _decision_wire_value(variant: dict[str, JsonValue]) -> str | None:
    enum = variant.get("enum")
    if not isinstance(enum, list) or len(enum) != 1:
        return None
    value = enum[0]
    if not isinstance(value, str) or not value:
        return None
    if _possible_types(variant) != frozenset({"string"}):
        return None
    return value


def _decision_object_variant(
    variant: dict[str, JsonValue],
    base_path: Path,
    stack: _RefStack,
    resolver: _SchemaResolver,
) -> tuple[str, frozenset[str]] | None:
    """Read one object decision as its single name and the members it must carry.

    Only that one level is read. A reviewer never sends an object decision, so the shape is
    learned solely to recognise a complete offered value and drop it from the presented set.
    """
    if _possible_types(variant) != frozenset({"object"}):
        return None
    required = _required_fields(variant)
    properties = _properties(variant)
    if required is None or len(required) != 1 or properties.keys() != required:
        return None
    name = next(iter(required))
    inner, _, _ = resolver.resolve(properties[name], base_path, stack)
    if _possible_types(inner) != frozenset({"object"}):
        return None
    members = _required_fields(inner)
    if not members or frozenset(_properties(inner)) != members:
        return None
    return name, members


def _validate_invocation(
    method: str,
    spec: _InvocationSpec,
    request: _ResolvedSchema,
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> ClientMethodContract | None:
    if spec.params_kind is ParamsKind.OMITTED:
        return _validate_parameterless_invocation(method, request, params, resolver)
    return _validate_object_invocation(method, spec, request, params, resolver)


def _validate_parameterless_invocation(
    method: str,
    request: _ResolvedSchema,
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> ClientMethodContract | None:
    params_schema, params_path, params_stack = params
    accepted = _accepted_types(params_schema, params_path, params_stack, resolver)
    if accepted != frozenset({"null"}):
        return None
    if not _definitely_admits(params, _NULL, resolver):
        return None
    envelope = _object_value({"id": _REQUEST_ID, "method": _LiteralValue(method)})
    if not _definitely_admits(request, envelope, resolver):
        return None
    return ClientMethodContract(method, ParamsKind.OMITTED)


def _validate_object_invocation(
    method: str,
    spec: _InvocationSpec,
    request: _ResolvedSchema,
    params: _ResolvedSchema,
    resolver: _SchemaResolver,
) -> ClientMethodContract | None:
    params_schema, _, _ = params
    semantic_fields = spec.semantic_fields
    if not semantic_fields <= _properties(params_schema).keys():
        return None
    # Only complete request envelopes are validated. A generic params probe would describe
    # a value moco never sends, so it could withdraw an envelope every real profile admits.
    for witness in spec.params_witnesses:
        envelope = _object_value(
            {"id": _REQUEST_ID, "method": _LiteralValue(method), "params": witness}
        )
        if not _definitely_admits(request, envelope, resolver):
            return None
    return ClientMethodContract(method, ParamsKind.OBJECT, semantic_fields)


def _accepted_types(
    schema: dict[str, JsonValue],
    base_path: Path,
    stack: _RefStack,
    resolver: _SchemaResolver,
    depth: int = 0,
) -> frozenset[str]:
    """Over-approximate the JSON types a schema accepts, so a narrow reading is never claimed."""
    resolver.consume_visit()
    if depth >= _MAX_REFERENCE_DEPTH:
        raise CodexSchemaError(_INVALID_SCHEMA)
    direct_types = _possible_types(schema)
    if "anyOf" not in schema:
        return direct_types
    raw_options = schema["anyOf"]
    if not isinstance(raw_options, list) or not raw_options:
        return frozenset()
    any_of_types: set[str] = set()
    for raw_option in raw_options:
        option, option_path, option_stack = resolver.resolve(raw_option, base_path, stack)
        any_of_types.update(
            _accepted_types(
                option,
                option_path,
                option_stack,
                resolver,
                depth + 1,
            )
        )
    return direct_types & any_of_types


def _definitely_admits(
    resolved: _ResolvedSchema,
    witness: _Witness,
    resolver: _SchemaResolver,
) -> bool:
    """Report availability, which only a definite match earns: undecided is unavailable."""
    schema, base_path, stack = resolved
    return _schema_admits(schema, base_path, stack, witness, resolver) is _Admission.ADMITTED


def _schema_admits(
    schema: dict[str, JsonValue],
    base_path: Path,
    stack: _RefStack,
    witness: _Witness,
    resolver: _SchemaResolver,
    depth: int = 0,
) -> _Admission:
    """Report how an already resolved schema treats one value moco emits."""
    resolver.consume_visit()
    if depth >= _MAX_REFERENCE_DEPTH:
        raise CodexSchemaError(_INVALID_SCHEMA)
    type_verdict = _type_admits(schema, _witness_type(witness))
    if type_verdict is _Admission.REJECTED:
        return _Admission.REJECTED

    def branch_admits(branch: _ResolvedSchema, branch_depth: int) -> _Admission:
        branch_schema, branch_path, branch_stack = branch
        return _schema_admits(
            branch_schema,
            branch_path,
            branch_stack,
            witness,
            resolver,
            branch_depth,
        )

    # Every readable assertion is evaluated before an unreadable one may downgrade the
    # verdict, so a definite rejection is never weakened into a doubt.
    verdict = min(type_verdict, _value_constraints_admit(schema, witness))
    if verdict is not _Admission.REJECTED:
        structure = _structure_admits(schema, base_path, stack, witness, resolver, depth)
        verdict = min(verdict, structure)
    if verdict is not _Admission.REJECTED:
        composition = _composition_admits(
            schema,
            (base_path, stack),
            resolver,
            depth,
            branch_admits,
        )
        verdict = min(verdict, composition)
    if verdict is _Admission.REJECTED:
        return _Admission.REJECTED
    if not schema.keys() <= _INTERPRETABLE_KEYWORDS:
        # An applicable assertion this bounded evaluator cannot read may still admit the
        # value, so the schema is undecided rather than treated as a rejection.
        return _Admission.UNDECIDED
    return verdict


def _witness_type(witness: _Witness) -> str:
    match witness:
        case _ObjectValue():
            return "object"
        case _ArrayValue():
            return "array"
        case _DynamicValue(json_type):
            return json_type
        case _LiteralValue(value):
            return _literal_type(value)


def _literal_type(value: _Scalar) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return "string"


def _value_constraints_admit(schema: dict[str, JsonValue], witness: _Witness) -> _Admission:
    """Judge enum/const, which a value moco only chooses at runtime rarely settles."""
    if "enum" not in schema and "const" not in schema:
        return _Admission.ADMITTED
    if isinstance(witness, _LiteralValue):
        if not _const_admits(schema, witness.value):
            return _Admission.REJECTED
        return _enum_admits(schema, witness.value)
    if isinstance(witness, _DynamicValue) and _admits_every_value(schema, witness.json_type):
        return _Admission.ADMITTED
    # A runtime value, object or array moco assembles per request may or may not be one of
    # the listed values, so the constraint neither admits nor rejects what moco will send.
    return _Admission.UNDECIDED


def _admits_every_value(schema: dict[str, JsonValue], json_type: str) -> bool:
    """Report whether enum/const list every value one JSON type can take at runtime."""
    domain = _FINITE_VALUE_DOMAINS.get(json_type)
    if domain is None:
        return False
    return all(
        _const_admits(schema, value) and _enum_admits(schema, value) is _Admission.ADMITTED
        for value in domain
    )


def _enum_admits(schema: dict[str, JsonValue], value: _Scalar) -> _Admission:
    if "enum" not in schema:
        return _Admission.ADMITTED
    candidates = schema["enum"]
    # JSON Schema requires a non-empty list of unique values, so an empty list, a `null`, and
    # a list repeating one value are all declarations this evaluator cannot read. None of them
    # is an absent constraint, and none of them proves the listed values exclude anything.
    if not isinstance(candidates, list) or not candidates or not _values_are_unique(candidates):
        return _Admission.UNDECIDED
    if any(_is_same_json_value(candidate, value) for candidate in candidates):
        return _Admission.ADMITTED
    return _Admission.REJECTED


def _values_are_unique(candidates: list[JsonValue]) -> bool:
    """Report whether the listed values are provably distinct JSON values.

    A repeated value, and a value nested too deeply to compare, both leave uniqueness
    unproven, so the caller keeps the declaration unread instead of trusting it.
    """
    keys: set[_ValueKey] = set()
    for candidate in candidates:
        key = _json_value_key(candidate)
        if key is None or key in keys:
            return False
        keys.add(key)
    return True


def _runtime_value_key(value: object) -> _ValueKey | None:
    """Key one runtime value for comparison, or nothing when it is not a JSON value."""
    if _runtime_type(value) is None:
        return None
    return _json_value_key(cast("JsonValue", value))


def _json_value_key(value: JsonValue, depth: int = 0) -> _ValueKey | None:
    """Rewrite one JSON value into its comparison key, or None when it is nested too deeply."""
    if depth > _MAX_VALUE_DEPTH:
        return None
    if type(value) is list:
        return _array_key(value, depth)
    if type(value) is dict:
        return _object_key(value, depth)
    if value is None or type(value) in (bool, int, Decimal):
        return _scalar_key(value=cast("None | bool | int | Decimal | str", value))
    if type(value) is str and _is_transport_safe(value):
        return _scalar_key(value=value)
    return None


def _scalar_key(*, value: None | bool | int | Decimal | str) -> _ValueKey:
    """Key one JSON scalar, whose JSON type keeps `true`, `1` and `"true"` apart."""
    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, str):
        return "string", value
    # One JSON number keys once however it is spelled: `1`, `1.0` and `1e0` are the same
    # number, and so are `9007199254740993` and `9007199254740993.0`, which a binary float
    # could not tell apart from `9007199254740992`. An integer and the exact reading of a
    # decimal or exponent compare and hash alike, so mixing them in one key is sound. Every
    # number reaching here is finite, because a non-finite constant and an overflowing
    # exponent are both rejected while the document is parsed, so no value keys as distinct
    # from itself.
    return "number", value


def _array_key(values: list[JsonValue], depth: int) -> _ValueKey | None:
    """Key an array, which equals another one member by member, in order."""
    if type(values) is not list:
        return None
    items: list[_ValueKey] = []
    for value in values:
        key = _json_value_key(value, depth + 1)
        if key is None:
            return None
        items.append(key)
    return "array", tuple(items)


def _object_key(members: dict[str, JsonValue], depth: int) -> _ValueKey | None:
    """Key an object, whose member order carries no meaning, so equal objects share one key."""
    if type(members) is not dict:
        return None
    keyed: list[tuple[str, _ValueKey]] = []
    for name, value in members.items():
        if type(name) is not str or not _is_transport_safe(name):
            return None
        key = _json_value_key(value, depth + 1)
        if key is None:
            return None
        keyed.append((name, key))
    return "object", tuple(sorted(keyed, key=lambda member: member[0]))


def _const_admits(schema: dict[str, JsonValue], value: _Scalar) -> bool:
    if "const" not in schema:
        return True
    return _is_same_json_value(schema["const"], value)


def _is_same_json_value(candidate: JsonValue, value: _Scalar) -> bool:
    """Compare one listed value with the scalar moco sends, as JSON values rather than Python.

    An array or object is never the scalar, and the scalar keys carry the JSON type, so a
    boolean never matches a number while two spellings of one number always do.
    """
    if isinstance(candidate, list | dict):
        return False
    return _scalar_key(value=candidate) == _scalar_key(value=value)


def _structure_admits(
    schema: dict[str, JsonValue],
    base_path: Path,
    stack: _RefStack,
    witness: _Witness,
    resolver: _SchemaResolver,
    depth: int,
) -> _Admission:
    if isinstance(witness, _ObjectValue):
        return _object_admits(schema, base_path, stack, witness, resolver, depth)
    if isinstance(witness, _ArrayValue):
        return _array_admits(schema, base_path, stack, witness, resolver, depth)
    return _Admission.ADMITTED


def _undeclared_member(additional: JsonValue) -> tuple[JsonValue | None, _Admission]:
    """Resolve `additionalProperties` into a schema for members `properties` omits."""
    if additional is True:
        return None, _Admission.ADMITTED
    if additional is False:
        return None, _Admission.REJECTED
    if _is_schema_map(additional):
        return additional, _Admission.ADMITTED
    return None, _Admission.UNDECIDED


def _object_admits(
    schema: dict[str, JsonValue],
    base_path: Path,
    stack: _RefStack,
    witness: _ObjectValue,
    resolver: _SchemaResolver,
    depth: int,
) -> _Admission:
    required = _required_fields(schema)
    if required is not None and not required <= witness.fields.keys():
        return _Admission.REJECTED
    raw_properties = schema.get("properties")
    readable = required is not None and (raw_properties is None or _is_schema_map(raw_properties))
    # A malformed `required` or `properties` cannot be read, so no member verdict below is
    # definite; the members are still checked because one may reject the object outright.
    verdict = _Admission.ADMITTED if readable else _Admission.UNDECIDED
    properties = _properties(schema)
    undeclared_raw, undeclared_verdict = _undeclared_member(
        schema.get("additionalProperties", True)
    )
    if not readable and undeclared_verdict is _Admission.REJECTED:
        # An unreadable declaration cannot prove that a member moco sends is undeclared.
        undeclared_verdict = _Admission.UNDECIDED
    for name, value in witness.fields.items():
        member_raw = properties.get(name, undeclared_raw)
        if member_raw is None:
            verdict = min(verdict, undeclared_verdict)
        else:
            member, member_path, member_stack = resolver.resolve(member_raw, base_path, stack)
            verdict = min(
                verdict,
                _schema_admits(member, member_path, member_stack, value, resolver, depth + 1),
            )
        if verdict is _Admission.REJECTED:
            return _Admission.REJECTED
    return verdict


def _item_count_admits(schema: dict[str, JsonValue], keyword: str, count: int) -> _Admission:
    bound = schema.get(keyword)
    if bound is None:
        return _Admission.ADMITTED
    if not _is_item_bound(bound):
        return _Admission.UNDECIDED
    exceeded = count < bound if keyword == "minItems" else count > bound
    return _Admission.REJECTED if exceeded else _Admission.ADMITTED


def _array_admits(
    schema: dict[str, JsonValue],
    base_path: Path,
    stack: _RefStack,
    witness: _ArrayValue,
    resolver: _SchemaResolver,
    depth: int,
) -> _Admission:
    count = len(witness.items)
    verdict = min(
        _item_count_admits(schema, "minItems", count),
        _item_count_admits(schema, "maxItems", count),
    )
    if verdict is _Admission.REJECTED:
        return _Admission.REJECTED
    items_raw = schema.get("items")
    if items_raw is None:
        return verdict
    if not isinstance(items_raw, dict):
        # A tuple-form or boolean `items` is a shape this evaluator does not interpret.
        return min(verdict, _Admission.UNDECIDED)
    for value in witness.items:
        item, item_path, item_stack = resolver.resolve(items_raw, base_path, stack)
        verdict = min(
            verdict,
            _schema_admits(item, item_path, item_stack, value, resolver, depth + 1),
        )
        if verdict is _Admission.REJECTED:
            return _Admission.REJECTED
    return verdict


def _is_item_bound(value: JsonValue) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _is_schema_map(value: JsonValue) -> bool:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _composition_admits(
    schema: dict[str, JsonValue],
    location: tuple[Path, _RefStack],
    resolver: _SchemaResolver,
    depth: int,
    admits: _BranchAdmission,
) -> _Admission:
    """Enforce anyOf/oneOf/allOf cardinality for one schema that did not directly reject."""
    verdict = _Admission.ADMITTED
    for keyword in _COMPOSITION_KEYWORDS:
        raw_options = schema.get(keyword)
        if raw_options is None:
            continue
        if not isinstance(raw_options, list) or not raw_options:
            # A degenerate composition list is not a shape this evaluator interprets.
            verdict = _Admission.UNDECIDED
            continue
        verdict = min(
            verdict,
            _keyword_admits(keyword, raw_options, location, resolver, depth, admits),
        )
        if verdict is _Admission.REJECTED:
            return _Admission.REJECTED
    return verdict


def _keyword_admits(
    keyword: str,
    raw_options: list[JsonValue],
    location: tuple[Path, _RefStack],
    resolver: _SchemaResolver,
    depth: int,
    admits: _BranchAdmission,
) -> _Admission:
    base_path, stack = location
    # Without a limit every branch is resolved within the resolver's visit budget.
    limit = _COMPOSITION_BRANCH_LIMITS.get(keyword, len(raw_options))
    admitted = 0
    undecided = 0
    for raw_option in raw_options:
        branch = admits(resolver.resolve(raw_option, base_path, stack), depth + 1)
        if branch is _Admission.ADMITTED:
            admitted += 1
            if admitted >= limit:
                break
        elif branch is _Admission.UNDECIDED:
            undecided += 1
        elif keyword == "allOf":
            return _Admission.REJECTED
    return _cardinality_verdict(keyword, admitted, undecided, len(raw_options))


def _cardinality_verdict(keyword: str, admitted: int, undecided: int, total: int) -> _Admission:
    """Map the branch tally of one keyword onto the verdict for the whole composition."""
    if keyword == "anyOf":
        if admitted >= 1:
            return _Admission.ADMITTED
        return _Admission.UNDECIDED if undecided else _Admission.REJECTED
    if keyword == "oneOf":
        # An undecided branch may match the same runtime value the admitted branch does,
        # so exactly-one can only be claimed when every other branch definitely rejects.
        if admitted != 1:
            return _Admission.UNDECIDED if admitted == 0 and undecided else _Admission.REJECTED
        return _Admission.UNDECIDED if undecided else _Admission.ADMITTED
    # allOf: no branch rejected the value, so only undecided branches remain in doubt.
    return _Admission.ADMITTED if admitted == total else _Admission.UNDECIDED


def _declared_types(schema: dict[str, JsonValue]) -> frozenset[str] | None:
    """Return the JSON types `type` accepts, or None when the declaration is unreadable.

    A malformed, unknown or empty declaration, and a list repeating a member, which JSON
    Schema forbids, all constrain the instance in a way this evaluator cannot read, so none
    of them is reported as an absent declaration or quietly deduplicated. Only `type` is
    read: a type-specific keyword such as `properties` applies to instances of its own type
    alone, so it never narrows which types the schema accepts.
    """
    if "type" not in schema:
        return _ALL_JSON_TYPES
    raw_type = schema["type"]
    if isinstance(raw_type, str):
        return frozenset({raw_type}) if raw_type in _ALL_JSON_TYPES else None
    if isinstance(raw_type, list) and raw_type:
        if not all(isinstance(item, str) and item in _ALL_JSON_TYPES for item in raw_type):
            return None
        names = cast("list[str]", raw_type)
        declared = frozenset(names)
        return declared if len(declared) == len(names) else None
    return None


def _type_admits(schema: dict[str, JsonValue], json_type: str) -> _Admission:
    """Judge only the `type` declaration against the JSON type of the value moco emits."""
    declared = _declared_types(schema)
    if declared is None:
        return _Admission.UNDECIDED
    accepted = _ADMITTING_DECLARATIONS.get(json_type, frozenset({json_type}))
    return _Admission.ADMITTED if accepted & declared else _Admission.REJECTED


def _possible_types(schema: dict[str, JsonValue]) -> frozenset[str]:
    """Return the JSON types an instance may take, an unreadable declaration ruling out none."""
    declared = _declared_types(schema)
    return _ALL_JSON_TYPES if declared is None else declared


def _one_of(root: dict[str, JsonValue]) -> list[JsonValue]:
    variants = root.get("oneOf")
    if not isinstance(variants, list):
        raise CodexSchemaError(_INVALID_SCHEMA)
    return variants


def _properties(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = schema.get("properties")
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return {}
    return value


def _required_fields(schema: dict[str, JsonValue]) -> frozenset[str] | None:
    """Return the declared required member names, or None when the declaration is unreadable.

    JSON Schema requires the names to be unique, so a list repeating one is unreadable
    rather than quietly deduplicated into a declaration the generated bundle never made.
    """
    value = schema.get("required", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    names = cast("list[str]", value)
    required = frozenset(names)
    return required if len(required) == len(names) else None


def _transport_method(schema: dict[str, JsonValue]) -> str:
    enum = schema.get("enum")
    if not isinstance(enum, list) or len(enum) != 1 or not isinstance(enum[0], str) or not enum[0]:
        raise CodexSchemaError(_INVALID_SCHEMA)
    return enum[0]


def _schema_titles(
    raw_schema: JsonValue,
    resolved_schema: dict[str, JsonValue],
    resolver: _SchemaResolver,
) -> frozenset[str]:
    titles = set(resolver.reference_names(raw_schema))
    raw = _as_schema(raw_schema, _INVALID_SCHEMA)
    for schema in (raw, resolved_schema):
        title = schema.get("title")
        if isinstance(title, str):
            titles.add(title)
    return frozenset(titles)


def _request_titles(
    raw_variant: JsonValue,
    variant: dict[str, JsonValue],
    raw_method: JsonValue,
    method_schema: dict[str, JsonValue],
) -> frozenset[str]:
    raw = _as_schema(raw_variant, _INVALID_SCHEMA)
    raw_method_schema = _as_schema(raw_method, _INVALID_SCHEMA)
    titles: set[str] = set()
    for schema in (raw, variant, raw_method_schema, method_schema):
        title = schema.get("title")
        if isinstance(title, str):
            titles.add(_normalize_request_title(title))
    return frozenset(titles)


def _normalize_request_title(title: str) -> str:
    if title.endswith("RequestMethod"):
        return f"{title.removesuffix('RequestMethod')}Request"
    return title


def _as_schema(value: JsonValue, message: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CodexSchemaError(message)
    return value


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    if not pointer:
        return ()
    if not pointer.startswith("/"):
        raise CodexSchemaError(_INVALID_REFERENCE)
    tokens: list[str] = []
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise CodexSchemaError(_INVALID_REFERENCE)
        tokens.append(raw_token.replace("~1", "/").replace("~0", "~"))
    return tuple(tokens)


def _has_malformed_percent_encoding(value: str) -> bool:
    return re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None


def _safe_urlsplit(reference: str) -> SplitResult:
    try:
        return urlsplit(reference)
    except (UnicodeError, ValueError):
        raise CodexSchemaError(_INVALID_REFERENCE) from None


def _safe_unquote(value: str) -> str:
    try:
        return unquote(value, errors="strict")
    except (UnicodeError, ValueError):
        raise CodexSchemaError(_INVALID_REFERENCE) from None


class CodexSchemaProbe:
    def __init__(self, command: CodexCommand) -> None:
        self._command = command

    async def probe(self) -> CodexProtocolContract:
        return await asyncio.to_thread(self.probe_sync)

    def probe_sync(self) -> CodexProtocolContract:
        version = self._probe_version()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            experimental = self._run_schema(output, experimental=True)
            if experimental.returncode == 0:
                _validate_schema_bundle(output)
                return load_generated_contract(
                    output,
                    version=version,
                    experimental_schema=True,
                )
            if not _experimental_option_unavailable(experimental.stderr):
                raise CodexSchemaError(_SCHEMA_PROBE_FAILED)
            stable = self._run_schema(output, experimental=False)
            if stable.returncode != 0:
                raise CodexSchemaError(_SCHEMA_PROBE_FAILED)
            _validate_schema_bundle(output)
            return load_generated_contract(
                output,
                version=version,
                experimental_schema=False,
            )

    def _probe_version(self) -> str:
        result = self._run(
            self._command.version_argv(),
            failure_message=_VERSION_PROBE_FAILED,
        )
        version = result.stdout.strip()
        if result.returncode != 0 or not version:
            raise CodexSchemaError(_VERSION_PROBE_FAILED)
        return version

    def _run_schema(
        self,
        output: Path,
        *,
        experimental: bool,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            self._command.schema_argv(output, experimental=experimental),
            failure_message=_SCHEMA_PROBE_FAILED,
        )

    @staticmethod
    def _run(
        argv: tuple[str, ...],
        *,
        failure_message: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(  # noqa: S603
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
                try:
                    with process:
                        try:
                            returncode = process.wait(timeout=_SUBPROCESS_TIMEOUT_SECONDS)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                            raise
                finally:
                    stdout_bytes, stdout_overflow = _read_bounded_process_output(stdout)
                    stderr_bytes, stderr_overflow = _read_bounded_process_output(stderr)
            if stdout_overflow or stderr_overflow:
                raise CodexSchemaError(failure_message)
            return subprocess.CompletedProcess(
                argv,
                returncode,
                stdout=stdout_bytes.decode("utf-8"),
                stderr=stderr_bytes.decode("utf-8"),
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise CodexSchemaError(failure_message) from None


def _read_bounded_process_output(stream: IO[bytes]) -> tuple[bytes, bool]:
    stream.seek(0)
    captured = stream.read(_MAX_SUBPROCESS_OUTPUT_BYTES + 1)
    return captured[:_MAX_SUBPROCESS_OUTPUT_BYTES], len(captured) > _MAX_SUBPROCESS_OUTPUT_BYTES


def _validate_schema_bundle(root: Path) -> None:
    files = 0
    total_bytes = 0
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise CodexSchemaError(_INVALID_SCHEMA)
            if not path.is_file():
                continue
            files += 1
            size = path.stat().st_size
            total_bytes += size
            if (
                files > _MAX_SCHEMA_BUNDLE_FILES
                or size > _MAX_SCHEMA_DOCUMENT_BYTES
                or total_bytes > _MAX_SCHEMA_BUNDLE_BYTES
            ):
                raise CodexSchemaError(_INVALID_SCHEMA)
    except OSError:
        raise CodexSchemaError(_INVALID_SCHEMA) from None


def _experimental_option_unavailable(stderr: str) -> bool:
    lowered = stderr.lower()
    option_mentioned = "--experimental" in lowered
    unavailable_marker = any(
        marker in lowered
        for marker in (
            "unexpected argument",
            "unrecognized argument",
            "unrecognized option",
            "unknown option",
            "unknown argument",
        )
    )
    return option_mentioned and unavailable_marker
