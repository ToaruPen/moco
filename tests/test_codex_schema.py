from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

# Documents are built as values the transport can carry, while schema reading holds a JSON
# number exactly, so the two JSON value types are named apart where one is passed directly.
from moco.codex.rpc import JsonValue
from moco.codex.schema import (
    AGENT_READINESS_METHODS,
    STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES,
    VOICE_REQUIRED_METHODS,
    ApprovalCorrelation,
    ApprovalDecision,
    ApprovalProfile,
    ClientMethodContract,
    CodexProtocolContract,
    CodexSchemaProbe,
    ParamsKind,
    SemanticMethod,
    ServerRequestCategory,
    _Admission,
    _Budget,
    _contract_mapping,
    _contract_properties_snapshot,
    _enum_admits,
    _freeze_json,
    _freeze_value_contract,
    _MalformedDocumentError,
    _read_integer,
    _read_number,
    _type_admits,
    _ValueContract,
    load_generated_contract,
)
from moco.codex.schema import (
    JsonValue as SchemaJsonValue,
)
from moco.errors import CodexSchemaError
from moco.platform import CodexCommand


def schema_variant(
    method: str,
    *,
    params_title: str | None = None,
    request_title: str | None = None,
    method_title: str | None = None,
    params_schema: dict[str, JsonValue] | None = None,
    params_required: set[str] | frozenset[str] = frozenset(),
    params_properties: dict[str, JsonValue] | None = None,
    params_is_required: bool = True,
) -> dict[str, JsonValue]:
    if params_schema is None:
        required_fields: list[JsonValue] = [*sorted(params_required)]
        params_schema = {
            "type": "object",
            "properties": params_properties or {},
            "required": required_fields,
        }
        if params_title is not None:
            params_schema["title"] = params_title
    method_schema: dict[str, JsonValue] = {"type": "string", "enum": [method]}
    if method_title is not None:
        method_schema["title"] = method_title
    variant: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"method": method_schema, "params": params_schema},
        "required": ["method", *(("params",) if params_is_required else ())],
    }
    if request_title is not None:
        variant["title"] = request_title
    return variant


def file_change_patch_notification_variant(
    *,
    method: str = "item/fileChange/patchUpdated",
    kind_schema: JsonValue | None = None,
) -> dict[str, JsonValue]:
    patch_kind = kind_schema or {
        "oneOf": [
            {
                "type": "object",
                "required": ["type"],
                "properties": {"type": {"type": "string", "enum": ["add"]}},
            },
            {
                "type": "object",
                "required": ["type"],
                "properties": {"type": {"type": "string", "enum": ["delete"]}},
            },
            {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "move_path": {"type": ["string", "null"]},
                    "type": {"type": "string", "enum": ["update"]},
                },
            },
        ]
    }
    return schema_variant(
        method,
        params_title="FileChangePatchUpdatedNotification",
        params_required={"changes", "itemId", "threadId", "turnId"},
        params_properties={
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["diff", "kind", "path"],
                    "properties": {
                        "diff": {"type": "string"},
                        "kind": patch_kind,
                        "path": {"type": "string"},
                    },
                },
            },
            "itemId": {"type": "string"},
            "threadId": {"type": "string"},
            "turnId": {"type": "string"},
        },
    )


def required_agent_notification_variants() -> list[dict[str, JsonValue]]:
    turn: dict[str, JsonValue] = {
        "type": "object",
        "required": ["id", "items", "status"],
        "properties": {
            "id": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object"}},
            "status": {
                "type": "string",
                "enum": ["completed", "interrupted", "failed", "inProgress"],
            },
        },
    }
    agent_message: dict[str, JsonValue] = {
        "type": "object",
        "required": ["id", "text", "type"],
        "properties": {
            "id": {"type": "string"},
            "phase": {
                "type": ["string", "null"],
                "enum": ["commentary", "final_answer"],
            },
            "text": {"type": "string"},
            "type": {"type": "string", "enum": ["agentMessage"]},
        },
    }
    return [
        schema_variant(
            "turn/completed",
            params_title="TurnCompletedNotification",
            params_required={"threadId", "turn"},
            params_properties={
                "threadId": {"type": "string"},
                "turn": turn,
            },
        ),
        schema_variant(
            "item/completed",
            params_title="ItemCompletedNotification",
            params_required={"item", "threadId", "turnId"},
            params_properties={
                "item": {"oneOf": [agent_message]},
                "threadId": {"type": "string"},
                "turnId": {"type": "string"},
            },
        ),
    ]


def item_started_notification_variant(
    method: str = "activity/item-began",
) -> dict[str, JsonValue]:
    return schema_variant(
        method,
        params_title="ItemStartedNotification",
        params_required={"item", "threadId", "turnId"},
        params_properties={
            "item": {
                "type": "object",
                "required": ["id", "type"],
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["commandExecution"]},
                    "command": {"type": "string"},
                },
            },
            "threadId": {"type": "string"},
            "turnId": {"type": "string"},
        },
    )


# The generated `RequestId` shape every retained Codex bundle declares for a request envelope.
REQUEST_ID: dict[str, JsonValue] = {
    "anyOf": [{"type": "string"}, {"type": "integer", "format": "int64"}]
}
# Two directories moco's runtime working directory can equal, used to show that a schema
# narrowed to them cannot be excluded for a value moco only chooses per request.
RUNTIME_CWD = "/tmp"  # noqa: S108
OTHER_RUNTIME_CWD = "/var/tmp"  # noqa: S108


def with_request_id(
    variant: dict[str, JsonValue],
    id_schema: JsonValue = REQUEST_ID,
) -> dict[str, JsonValue]:
    """Declare the required envelope request id the generated bundles carry."""
    properties = cast("dict[str, JsonValue]", variant["properties"])
    properties["id"] = id_schema
    variant["required"] = ["id", *cast("list[JsonValue]", variant["required"])]
    return variant


def write_schema_bundle(
    bundle: Path,
    *,
    client_variants: list[dict[str, JsonValue]],
    server_variants: list[dict[str, JsonValue]],
    documents: dict[str, JsonValue] | None = None,
) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "ClientRequest.json").write_text(
        json.dumps({"oneOf": client_variants}),
        encoding="utf-8",
    )
    (bundle / "ServerRequest.json").write_text(
        json.dumps({"oneOf": server_variants}),
        encoding="utf-8",
    )
    for name, document in (documents or {}).items():
        (bundle / name).write_text(json.dumps(document), encoding="utf-8")


_SANDBOX_MODE: dict[str, JsonValue] = {
    "type": "string",
    "enum": ["read-only", "workspace-write", "danger-full-access"],
}
_ASK_FOR_APPROVAL: dict[str, JsonValue] = {
    "oneOf": [
        {"type": "string", "enum": ["untrusted", "on-request", "never"]},
        {
            "type": "object",
            "required": ["granular"],
            "properties": {"granular": {"type": "object"}},
        },
    ]
}


def thread_start_properties(
    *,
    sandbox: JsonValue | None = None,
    approval_policy: JsonValue | None = None,
    overrides: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {
        "cwd": {"type": ["string", "null"]},
        "ephemeral": {"type": ["boolean", "null"]},
        "sandbox": {"anyOf": [_SANDBOX_MODE if sandbox is None else sandbox, {"type": "null"}]},
        "approvalPolicy": {
            "anyOf": [
                _ASK_FOR_APPROVAL if approval_policy is None else approval_policy,
                {"type": "null"},
            ]
        },
    }
    properties.update(overrides or {})
    return properties


def turn_start_properties(
    overrides: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {
        "input": {"type": "array", "items": {"type": "object"}},
        "threadId": {"type": "string"},
    }
    properties.update(overrides or {})
    return properties


def thread_start_variant(
    method: str = "alias-thread-start",
    *,
    sandbox: JsonValue | None = None,
    approval_policy: JsonValue | None = None,
    overrides: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    return schema_variant(
        method,
        params_title="ThreadStartParams",
        params_properties=thread_start_properties(
            sandbox=sandbox,
            approval_policy=approval_policy,
            overrides=overrides,
        ),
    )


def thread_realtime_start_variant(
    method: str = "alias-realtime-start",
    *,
    versions: tuple[str, ...] = ("v1", "v2", "v3"),
) -> dict[str, JsonValue]:
    return schema_variant(
        method,
        params_title="ThreadRealtimeStartParams",
        params_required={"outputModality", "threadId"},
        params_properties={
            "includeStartupContext": {"type": ["boolean", "null"]},
            "outputModality": {"type": "string", "enum": ["text", "audio"]},
            "prompt": {"type": ["string", "null"]},
            "threadId": {"type": "string"},
            "transport": {
                "anyOf": [
                    {
                        "type": "object",
                        "required": ["sdp", "type"],
                        "properties": {
                            "sdp": {"type": "string"},
                            "type": {"type": "string", "enum": ["webrtc"]},
                        },
                    },
                    {"type": "null"},
                ]
            },
            "version": {
                "anyOf": [
                    {"type": "string", "enum": list(versions)},
                    {"type": "null"},
                ]
            },
        },
    )


def turn_start_variant(
    method: str = "alias-turn-start",
    *,
    input_schema: JsonValue | None = None,
) -> dict[str, JsonValue]:
    overrides: dict[str, JsonValue] | None = (
        None if input_schema is None else {"input": input_schema}
    )
    return schema_variant(
        method,
        params_title="TurnStartParams",
        params_required={"input", "threadId"},
        params_properties=turn_start_properties(overrides),
    )


def turn_steer_variant(
    method: str = "alias-turn-steer",
    *,
    input_schema: JsonValue | None = None,
    params_title: str = "TurnSteerParams",
    request_title: str = "Turn/steerRequest",
    omitted_property: str | None = None,
    params_required: set[str] | None = None,
) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {
        "expectedTurnId": {"type": "string"},
        "input": user_input_array() if input_schema is None else input_schema,
        "threadId": {"type": "string"},
    }
    if omitted_property is not None:
        properties.pop(omitted_property)
    return schema_variant(
        method,
        params_title=params_title,
        request_title=request_title,
        params_required=set(properties) if params_required is None else params_required,
        params_properties=properties,
    )


def text_user_input_branch(
    *,
    properties: dict[str, JsonValue] | None = None,
    required: list[JsonValue] | None = None,
    additional_properties: JsonValue | None = None,
) -> dict[str, JsonValue]:
    """One generated `TextUserInput` union branch, optionally narrowed for a rejection case."""
    branch: dict[str, JsonValue] = {
        "type": "object",
        "required": ["text", "type"] if required is None else required,
        "properties": properties
        or {
            "text": {"type": "string"},
            "text_elements": {
                "description": "UI-defined spans within `text`.",
                "default": [],
                "type": "array",
                "items": {"type": "object"},
            },
            "type": {"type": "string", "enum": ["text"], "title": "TextUserInputType"},
        },
        "title": "TextUserInput",
    }
    if additional_properties is not None:
        branch["additionalProperties"] = additional_properties
    return branch


def user_input_array(
    text_branch: JsonValue | None = None,
    *,
    bounds: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """The generated `UserInput` union array that moco's single text item must satisfy."""
    other_branches: list[JsonValue] = [
        {
            "type": "object",
            "required": ["type", "url"],
            "properties": {
                "type": {"type": "string", "enum": ["image"], "title": "ImageUserInputType"},
                "url": {"type": "string"},
            },
            "title": "ImageUserInput",
        },
        {
            "type": "object",
            "required": ["path", "type"],
            "properties": {
                "path": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["localImage"],
                    "title": "LocalImageUserInputType",
                },
            },
            "title": "LocalImageUserInput",
        },
        {
            "type": "object",
            "required": ["name", "path", "type"],
            "properties": {
                "name": {"type": "string"},
                "path": {"type": "string"},
                "type": {"type": "string", "enum": ["skill"], "title": "SkillUserInputType"},
            },
            "title": "SkillUserInput",
        },
    ]
    branch = text_user_input_branch() if text_branch is None else text_branch
    array: dict[str, JsonValue] = {
        "type": "array",
        "items": {"oneOf": [branch, *other_branches]},
    }
    array.update(bounds or {})
    return array


def variant_properties(variant: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", variant["properties"])


def variant_params_schema(variant: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return cast("dict[str, JsonValue]", variant_properties(variant)["params"])


# Type declarations a generated bundle may carry that this evaluator cannot read: a
# non-string, non-list declaration; an unknown JSON type name; a malformed member inside a
# type list; an empty type list; and a type list repeating a member, which JSON Schema
# forbids. None of them describes a type moco can exclude.
MALFORMED_TYPE_DECLARATIONS = [
    pytest.param({"type": 5}, id="non-string-type"),
    pytest.param({"type": "str"}, id="unknown-type-name"),
    pytest.param({"type": ["string", 5]}, id="malformed-type-list-member"),
    pytest.param({"type": []}, id="empty-type-list"),
    pytest.param({"type": ["string", "string"]}, id="repeated-type-list-member"),
    pytest.param({"type": ["null", "null"]}, id="repeated-null-type-list-member"),
]
# Declarations carrying only object keywords. Without `type` every JSON type stays possible,
# and the keyword is evaluated only for an object instance, so none of them rejects a string.
OBJECT_KEYWORDS_WITHOUT_TYPE = [
    pytest.param({"required": ["future"]}, id="required-without-type"),
    pytest.param({"properties": {"future": {"type": "string"}}}, id="properties-without-type"),
    pytest.param({"additionalProperties": False}, id="additional-properties-without-type"),
    pytest.param({"required": "future"}, id="malformed-required-without-type"),
    pytest.param({"properties": ["future"]}, id="malformed-properties-without-type"),
]


def test_realtime_start_schema_selects_alias_for_the_v3_audio_webrtc_payload(
    tmp_path: Path,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[thread_realtime_start_variant("effective/realtime-start")],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_REALTIME_START) == ClientMethodContract(
        "effective/realtime-start",
        ParamsKind.OBJECT,
        frozenset(
            {
                "includeStartupContext",
                "outputModality",
                "prompt",
                "threadId",
                "transport",
                "version",
            }
        ),
    )


def test_realtime_start_schema_rejects_a_build_without_v3(tmp_path: Path) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[thread_realtime_start_variant(versions=("v1", "v2"))],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_REALTIME_START) is None


def test_turn_steer_schema_selects_alias_from_matching_generated_titles(
    tmp_path: Path,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[turn_steer_variant()],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="fake")

    semantic = SemanticMethod("turn_steer")
    assert contract.require_method(semantic) == ClientMethodContract(
        "alias-turn-steer",
        ParamsKind.OBJECT,
        frozenset({"expectedTurnId", "input", "threadId"}),
    )
    assert semantic not in AGENT_READINESS_METHODS


def test_turn_steer_schema_rejects_a_title_collision(tmp_path: Path) -> None:
    variant = turn_steer_variant(request_title="Turn/startRequest")
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    with pytest.raises(CodexSchemaError, match="ambiguous"):
        load_generated_contract(tmp_path, version="fake")


@pytest.mark.parametrize("missing", ["expectedTurnId", "input", "threadId"])
def test_turn_steer_schema_requires_every_semantic_field(
    tmp_path: Path,
    missing: str,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[turn_steer_variant(omitted_property=missing)],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="fake")
    semantic = SemanticMethod("turn_steer")

    assert contract.method(semantic) is None
    assert semantic in contract.missing_methods


@pytest.mark.parametrize("missing", ["expectedTurnId", "input", "threadId"])
def test_turn_steer_schema_requires_every_semantic_field_to_be_required(
    tmp_path: Path,
    missing: str,
) -> None:
    semantic_fields = {"expectedTurnId", "input", "threadId"}
    write_schema_bundle(
        tmp_path,
        client_variants=[turn_steer_variant(params_required=semantic_fields - {missing})],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_STEER) is None
    assert SemanticMethod.TURN_STEER in contract.missing_methods


def test_turn_steer_schema_rejects_an_extra_required_field(tmp_path: Path) -> None:
    semantic_fields = {"expectedTurnId", "input", "threadId"}
    variant = turn_steer_variant(params_required=semantic_fields | {"future"})
    properties = cast(
        "dict[str, JsonValue]",
        variant_params_schema(variant)["properties"],
    )
    properties["future"] = {"type": "string"}
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_STEER) is None
    assert SemanticMethod.TURN_STEER in contract.missing_methods


def test_turn_steer_schema_requires_one_text_input_witness(tmp_path: Path) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[
            turn_steer_variant(input_schema={"type": "array", "items": {"type": "string"}})
        ],
        server_variants=[],
    )

    contract = load_generated_contract(tmp_path, version="fake")
    semantic = SemanticMethod("turn_steer")

    assert contract.method(semantic) is None
    assert semantic in contract.missing_methods


def test_file_change_patch_schema_derives_optional_notification_evidence(
    tmp_path: Path,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={
            "ServerNotification.json": {"oneOf": [file_change_patch_notification_variant()]}
        },
    )

    contract = load_generated_contract(tmp_path, version="fake")
    profile = contract.file_change_patch_profile

    assert profile is not None
    assert profile.method == "item/fileChange/patchUpdated"
    assert profile.admits(
        {
            "changes": [
                {"diff": "@@ secret patch", "kind": {"type": "add"}, "path": "added.txt"},
                {
                    "diff": "@@ moved patch",
                    "kind": {"type": "update", "move_path": "moved.txt"},
                    "path": "source.txt",
                },
            ],
            "itemId": "item-1",
            "threadId": "thread-1",
            "turnId": "turn-1",
        }
    )


@pytest.mark.parametrize(
    "kind_schema",
    [
        {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["type"],
                    "properties": {"type": {"type": "string", "enum": ["future"]}},
                }
            ]
        },
        {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["type"],
                    "properties": {"type": {"type": "string", "enum": ["add"]}},
                },
                {
                    "type": "object",
                    "required": ["type"],
                    "properties": {"type": {"type": "string", "enum": ["delete"]}},
                },
                {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "move_path": {"type": ["integer", "null"]},
                        "type": {"type": "string", "enum": ["update"]},
                    },
                },
            ]
        },
    ],
    ids=["unknown-kind", "invalid-move-path"],
)
def test_malformed_file_change_patch_schema_does_not_withdraw_agent_events(
    tmp_path: Path,
    kind_schema: JsonValue,
) -> None:
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        file_change_patch_notification_variant(kind_schema=kind_schema),
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.agent_event_profile is not None
    assert contract.file_change_patch_profile is None


def test_agent_profile_derives_optional_item_started_method_and_outer_shape(
    tmp_path: Path,
) -> None:
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        item_started_notification_variant(),
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.agent_event_profile
    assert profile is not None
    assert profile.item_started_method == "activity/item-began"
    assert profile.item_started_required_fields == frozenset({"item", "threadId", "turnId"})
    assert profile.item_started_field_types == {
        "item": frozenset({"object"}),
        "threadId": frozenset({"string"}),
        "turnId": frozenset({"string"}),
    }


def test_agent_profile_rejects_terminal_statuses_narrowed_by_all_of(
    tmp_path: Path,
) -> None:
    notifications = cast("list[JsonValue]", required_agent_notification_variants())
    turn_variant = cast("dict[str, JsonValue]", notifications[0])
    turn_params = cast(
        "dict[str, JsonValue]",
        cast("dict[str, JsonValue]", turn_variant["properties"])["params"],
    )
    turn = cast(
        "dict[str, JsonValue]",
        cast("dict[str, JsonValue]", turn_params["properties"])["turn"],
    )
    status = cast(
        "dict[str, JsonValue]",
        cast("dict[str, JsonValue]", turn["properties"])["status"],
    )
    status["allOf"] = [{"type": "string", "enum": ["inProgress"]}]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.agent_event_profile is None


def test_agent_profile_rejects_a_required_string_with_overlapping_one_of(
    tmp_path: Path,
) -> None:
    notifications = cast("list[JsonValue]", required_agent_notification_variants())
    turn_variant = cast("dict[str, JsonValue]", notifications[0])
    turn_params = cast(
        "dict[str, JsonValue]",
        cast("dict[str, JsonValue]", turn_variant["properties"])["params"],
    )
    cast("dict[str, JsonValue]", turn_params["properties"])["threadId"] = {
        "oneOf": [{"type": "string"}, {"type": "string"}]
    }
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.agent_event_profile is None


def test_unreadable_optional_item_started_keeps_terminal_agent_profile(
    tmp_path: Path,
) -> None:
    unreadable_started = schema_variant(
        "activity/item-began",
        params_title="ItemStartedNotification",
        params_required={"item", "threadId"},
        params_properties={
            "item": {"type": "object"},
            "threadId": {"type": "string"},
        },
    )
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        unreadable_started,
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.agent_event_profile
    assert profile is not None
    assert profile.item_started_method is None


def test_ambiguous_optional_item_started_keeps_terminal_agent_profile(
    tmp_path: Path,
) -> None:
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        item_started_notification_variant("activity/item-began-one"),
        item_started_notification_variant("activity/item-began-two"),
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.agent_event_profile
    assert profile is not None
    assert profile.item_started_method is None


def test_item_started_method_collision_keeps_only_terminal_agent_profile(
    tmp_path: Path,
) -> None:
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        item_started_notification_variant("item/completed"),
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.agent_event_profile
    assert profile is not None
    assert profile.item_completed_method == "item/completed"
    assert profile.item_started_method is None

    with pytest.raises(CodexSchemaError):
        replace(
            profile,
            item_started_method=profile.item_completed_method,
            item_started_required_fields=frozenset({"item", "threadId", "turnId"}),
            item_started_field_types={
                "item": frozenset({"object"}),
                "threadId": frozenset({"string"}),
                "turnId": frozenset({"string"}),
            },
        )


def test_duplicate_file_change_patch_title_is_optional_fail_closed(tmp_path: Path) -> None:
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        file_change_patch_notification_variant(method="patch/one"),
        file_change_patch_notification_variant(method="patch/two"),
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.agent_event_profile is not None
    assert contract.file_change_patch_profile is None


def test_file_change_patch_method_collision_is_optional_fail_closed(tmp_path: Path) -> None:
    method = "item/fileChange/patchUpdated"
    notifications: list[JsonValue] = [
        *required_agent_notification_variants(),
        file_change_patch_notification_variant(method=method),
        schema_variant(method, params_title="FutureNotification"),
    ]
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[],
        documents={"ServerNotification.json": {"oneOf": notifications}},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.agent_event_profile is not None
    assert contract.file_change_patch_profile is None


def test_contract_selects_aliases_only_by_semantic_schema_signals(tmp_path: Path) -> None:
    client_variants = [
        schema_variant("alias-a", params_title="GetAccountParams"),
        schema_variant(
            "alias-b",
            params_title="ConfigReadParams",
            params_properties={"cwd": {"type": ["string", "null"]}},
        ),
        schema_variant(
            "alias-c",
            request_title="ConfigRequirements/readRequest",
            params_schema={"type": "null"},
            params_is_required=False,
        ),
        schema_variant(
            "alias-d",
            params_title="ExperimentalFeatureListParams",
            params_properties={"cursor": {"type": ["string", "null"]}},
        ),
        schema_variant("alias-e", params_title="ThreadRealtimeListVoicesParams"),
        schema_variant(
            "alias-f",
            params_title="TurnInterruptParams",
            params_required={"threadId", "turnId"},
            params_properties={
                "threadId": {"type": "string"},
                "turnId": {"type": "string"},
            },
        ),
        thread_start_variant("alias-g"),
        turn_start_variant("alias-h"),
        turn_steer_variant("alias-i"),
        thread_realtime_start_variant("alias-j"),
        schema_variant("account/read", params_title="UnrelatedParams"),
    ]
    write_schema_bundle(tmp_path, client_variants=client_variants, server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake 1")

    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "alias-a"
    assert contract.require_method(SemanticMethod.CONFIG_READ).semantic_fields == frozenset({"cwd"})
    assert contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ) == ClientMethodContract(
        "alias-c",
        ParamsKind.OMITTED,
    )
    assert contract.require_method(SemanticMethod.TURN_INTERRUPT).semantic_fields == frozenset(
        {"threadId", "turnId"}
    )
    assert contract.require_method(SemanticMethod.THREAD_START) == ClientMethodContract(
        "alias-g",
        ParamsKind.OBJECT,
        frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
    )
    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-h",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )
    assert contract.require_method(SemanticMethod.TURN_STEER) == ClientMethodContract(
        "alias-i",
        ParamsKind.OBJECT,
        frozenset({"expectedTurnId", "input", "threadId"}),
    )
    assert contract.require_method(SemanticMethod.THREAD_REALTIME_START).name == "alias-j"
    assert contract.version == "fake 1"
    assert contract.experimental_schema is True
    assert contract.missing_methods == frozenset()
    assert (
        frozenset(
            {
                SemanticMethod.ACCOUNT_READ,
                SemanticMethod.REALTIME_VOICES_LIST,
                SemanticMethod.THREAD_REALTIME_START,
            }
        )
        == VOICE_REQUIRED_METHODS
    )
    assert (
        frozenset(
            {
                SemanticMethod.THREAD_START,
                SemanticMethod.TURN_START,
                SemanticMethod.TURN_INTERRUPT,
            }
        )
        == AGENT_READINESS_METHODS
    )
    assert not AGENT_READINESS_METHODS & VOICE_REQUIRED_METHODS
    assert (
        frozenset(
            {ServerRequestCategory.COMMAND_APPROVAL, ServerRequestCategory.FILE_CHANGE_APPROVAL}
        )
        == STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    )


def test_parameterless_request_accepts_normalized_method_property_title(tmp_path: Path) -> None:
    variant = schema_variant(
        "opaque",
        method_title="ConfigRequirements/readRequestMethod",
        params_schema={"type": "null"},
        params_is_required=False,
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake", experimental_schema=False)

    assert contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ).name == "opaque"
    assert contract.experimental_schema is False


def test_parameterless_request_carries_the_transport_request_id(tmp_path: Path) -> None:
    variant = with_request_id(
        schema_variant(
            "opaque",
            request_title="ConfigRequirements/readRequest",
            params_schema={"type": "null"},
            params_is_required=False,
        )
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ) == ClientMethodContract(
        "opaque",
        ParamsKind.OMITTED,
    )


@pytest.mark.parametrize(
    ("id_schema", "composition"),
    [
        ({"type": "string"}, {}),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, {}),
        (
            REQUEST_ID,
            {"anyOf": [{"type": "object", "properties": {"id": {"type": "string"}}}]},
        ),
        (
            REQUEST_ID,
            {"anyOf": [{"type": "object", "properties": {"method": {"const": "other/alias"}}}]},
        ),
    ],
)
def test_parameterless_request_rejects_an_envelope_moco_cannot_emit(
    tmp_path: Path,
    id_schema: JsonValue,
    composition: dict[str, JsonValue],
) -> None:
    variant = with_request_id(
        schema_variant(
            "opaque",
            request_title="ConfigRequirements/readRequest",
            params_schema={"type": "null"},
            params_is_required=False,
        ),
        id_schema,
    )
    variant.update(composition)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) is None
    assert SemanticMethod.CONFIG_REQUIREMENTS_READ in contract.missing_methods


def test_generated_parameterless_variant_shape_classifies_and_omits_params(
    tmp_path: Path,
) -> None:
    """A realistic `configRequirements/read` variant: required id and method, params null."""
    variant = with_request_id(
        schema_variant(
            "configRequirements/read",
            method_title="ConfigRequirements/readRequestMethod",
            params_schema={"type": "null"},
            params_is_required=False,
        )
    )
    config_read = with_request_id(
        schema_variant(
            "config/read",
            params_title="ConfigReadParams",
            params_properties={"cwd": {"type": ["string", "null"]}},
        )
    )
    write_schema_bundle(tmp_path, client_variants=[variant, config_read], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    requirements = contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ)
    assert requirements.name == "configRequirements/read"
    assert requirements.params_kind is ParamsKind.OMITTED
    assert requirements.semantic_fields == frozenset()
    assert contract.require_method(SemanticMethod.CONFIG_READ).params_kind is ParamsKind.OBJECT


def test_parameterless_request_rejects_outer_any_of_branch_requiring_params(
    tmp_path: Path,
) -> None:
    variant = schema_variant(
        "opaque",
        request_title="ConfigRequirements/readRequest",
        params_schema={"type": "null"},
        params_is_required=False,
    )
    variant["anyOf"] = [
        {
            "type": "object",
            "required": ["params"],
        }
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) is None
    assert SemanticMethod.CONFIG_REQUIREMENTS_READ in contract.missing_methods


@pytest.mark.parametrize(
    "params_schema",
    [
        {"type": "null", "oneOf": [{"type": "string"}]},
        {"type": "null", "allOf": [{"type": "null"}, {"type": "string"}]},
        {"type": "null", "oneOf": [{"type": "null"}, {"type": ["null", "string"]}]},
        {"type": "null", "oneOf": []},
        {"type": "null", "allOf": "not-a-list"},
        {"type": "null", "anyOf": []},
    ],
)
def test_parameterless_request_rejects_params_composition_that_cannot_be_null(
    tmp_path: Path,
    params_schema: dict[str, JsonValue],
) -> None:
    variant = schema_variant(
        "opaque",
        request_title="ConfigRequirements/readRequest",
        params_schema=params_schema,
        params_is_required=False,
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) is None
    assert SemanticMethod.CONFIG_REQUIREMENTS_READ in contract.missing_methods


def test_parameterless_request_accepts_params_one_of_with_exactly_one_null_branch(
    tmp_path: Path,
) -> None:
    variant = schema_variant(
        "opaque",
        request_title="ConfigRequirements/readRequest",
        params_schema={"type": "null", "oneOf": [{"type": "null"}, {"type": "string"}]},
        params_is_required=False,
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ) == ClientMethodContract(
        "opaque",
        ParamsKind.OMITTED,
    )


@pytest.mark.parametrize("malformed", MALFORMED_TYPE_DECLARATIONS)
def test_parameterless_params_any_of_with_a_malformed_branch_stays_unavailable(
    tmp_path: Path,
    malformed: dict[str, JsonValue],
) -> None:
    """An unreadable branch may accept a value other than null, so omitting params is unproven."""
    variant = schema_variant(
        "opaque",
        request_title="ConfigRequirements/readRequest",
        params_schema={"anyOf": [{"type": "null"}, malformed]},
        params_is_required=False,
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) is None
    assert SemanticMethod.CONFIG_REQUIREMENTS_READ in contract.missing_methods


@pytest.mark.parametrize("malformed", MALFORMED_TYPE_DECLARATIONS)
def test_parameterless_params_with_a_malformed_direct_type_stays_unavailable(
    tmp_path: Path,
    malformed: dict[str, JsonValue],
) -> None:
    """An unreadable declaration on the params schema itself never proves params are null."""
    variant = schema_variant(
        "opaque",
        request_title="ConfigRequirements/readRequest",
        params_schema={**malformed, "anyOf": [{"type": "null"}]},
        params_is_required=False,
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) is None
    assert SemanticMethod.CONFIG_REQUIREMENTS_READ in contract.missing_methods


@pytest.mark.parametrize(
    "params_schema",
    [
        pytest.param({"type": ["null", "null"]}, id="repeated-null"),
        pytest.param({"type": ["null", "string", "null"]}, id="repeated-null-inside-union"),
    ],
)
def test_parameterless_params_with_a_repeated_type_member_stays_unavailable(
    tmp_path: Path,
    params_schema: dict[str, JsonValue],
) -> None:
    """A repeated `type` member is unreadable, so a params schema never proves it is null."""
    variant = schema_variant(
        "opaque",
        request_title="ConfigRequirements/readRequest",
        params_schema=params_schema,
        params_is_required=False,
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_REQUIREMENTS_READ) is None
    assert SemanticMethod.CONFIG_REQUIREMENTS_READ in contract.missing_methods


def test_object_request_title_without_recognized_params_does_not_classify_method(
    tmp_path: Path,
) -> None:
    variant = schema_variant(
        "opaque",
        params_title="UnknownFutureParams",
        request_title="Config/readRequest",
        params_properties={"cwd": {"type": "string"}},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize(
    "variants",
    [
        [
            schema_variant("one", params_title="AccountReadParams"),
            schema_variant("two", params_title="GetAccountParams"),
        ],
        [
            schema_variant(
                "conflict",
                params_title="GetAccountParams",
                request_title="Config/readRequest",
            )
        ],
    ],
)
def test_contract_rejects_ambiguous_client_signals(
    tmp_path: Path,
    variants: list[dict[str, JsonValue]],
) -> None:
    write_schema_bundle(tmp_path, client_variants=variants, server_variants=[])

    with pytest.raises(CodexSchemaError, match="ambiguous"):
        load_generated_contract(tmp_path, version="fake")


def test_duplicate_semantic_is_ambiguous_even_when_first_shape_is_unavailable(
    tmp_path: Path,
) -> None:
    unavailable = schema_variant(
        "unavailable",
        params_title="ConfigReadParams",
        params_required={"cwd"},
        params_properties={"cwd": {"type": "integer"}},
    )
    available = schema_variant(
        "available",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    write_schema_bundle(
        tmp_path,
        client_variants=[unavailable, available],
        server_variants=[],
    )

    with pytest.raises(CodexSchemaError, match="ambiguous"):
        load_generated_contract(tmp_path, version="fake")


@pytest.mark.parametrize(
    ("semantic", "variant"),
    [
        (
            SemanticMethod.ACCOUNT_READ,
            schema_variant(
                "a",
                params_title="GetAccountParams",
                params_required={"refreshToken"},
                params_properties={"refreshToken": {"type": "boolean"}},
            ),
        ),
        (
            SemanticMethod.CONFIG_READ,
            schema_variant(
                "b",
                params_title="ConfigReadParams",
                params_required={"cwd"},
                params_properties={"cwd": {"type": "integer"}},
            ),
        ),
        (
            SemanticMethod.REALTIME_VOICES_LIST,
            schema_variant(
                "voice",
                params_title="ThreadRealtimeListVoicesParams",
                params_required={"locale"},
                params_properties={"locale": {"type": "string"}},
            ),
        ),
        (
            SemanticMethod.EXPERIMENTAL_FEATURE_LIST,
            schema_variant(
                "c",
                params_title="ExperimentalFeatureListParams",
                params_properties={"cursor": {"type": "string"}},
            ),
        ),
        (
            SemanticMethod.TURN_INTERRUPT,
            schema_variant(
                "d",
                params_title="TurnInterruptParams",
                params_required={"threadId"},
                params_properties={"threadId": {"type": "string"}},
            ),
        ),
        (
            SemanticMethod.TURN_INTERRUPT,
            schema_variant(
                "interrupt-type-mismatch",
                params_title="TurnInterruptParams",
                params_required={"threadId", "turnId"},
                params_properties={
                    "threadId": {"type": "string"},
                    "turnId": {"type": "integer"},
                },
            ),
        ),
        (
            SemanticMethod.CONFIG_REQUIREMENTS_READ,
            schema_variant(
                "e",
                request_title="ConfigRequirements/readRequest",
                params_schema={"type": "null"},
                params_is_required=True,
            ),
        ),
        (
            SemanticMethod.CONFIG_REQUIREMENTS_READ,
            schema_variant(
                "f",
                request_title="ConfigRequirements/readRequest",
                params_schema={"type": ["object", "null"]},
                params_is_required=False,
            ),
        ),
    ],
)
def test_incompatible_invocation_shape_makes_method_unavailable(
    tmp_path: Path,
    semantic: SemanticMethod,
    variant: dict[str, JsonValue],
) -> None:
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(semantic) is None
    assert semantic in contract.missing_methods


def test_agent_execution_semantics_ignore_unrelated_optional_properties(
    tmp_path: Path,
) -> None:
    thread = thread_start_variant(
        "future/thread/begin",
        overrides={
            "startedAtMs": {"type": ["integer", "null"]},
            "permissions": {"type": ["string", "null"]},
            "personality": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    )
    turn = schema_variant(
        "future/turn/begin",
        params_title="TurnStartParams",
        params_required={"input", "threadId"},
        params_properties=turn_start_properties(
            {
                "outputSchema": True,
                "clientUserMessageId": {"type": ["string", "null"]},
            }
        ),
    )
    write_schema_bundle(tmp_path, client_variants=[thread, turn], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "future/thread/begin"
    assert contract.require_method(SemanticMethod.THREAD_START).semantic_fields == frozenset(
        {"cwd", "ephemeral", "sandbox", "approvalPolicy"}
    )
    assert contract.require_method(SemanticMethod.TURN_START).name == "future/turn/begin"
    assert contract.require_method(SemanticMethod.TURN_START).semantic_fields == frozenset(
        {"input", "threadId"}
    )


@pytest.mark.parametrize(
    "branches",
    [
        [{"type": "object", "required": ["sandbox", "approvalPolicy"]}],
        [{"type": "object", "properties": {"sandbox": {"type": "null"}}}],
        [{"type": "object", "properties": {"approvalPolicy": {"type": "null"}}}],
        [{"type": "object", "properties": {"ephemeral": {"type": "string"}}}],
    ],
)
def test_thread_start_needs_both_inherit_and_explicit_profile_assignments(
    tmp_path: Path,
    branches: list[dict[str, JsonValue]],
) -> None:
    variant = thread_start_variant()
    properties = cast("dict[str, JsonValue]", variant["properties"])
    params_schema = cast("dict[str, JsonValue]", properties["params"])
    params_schema["anyOf"] = cast("JsonValue", branches)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    ("sandbox", "approval_policy"),
    [
        ({"type": "string", "enum": ["workspace-write", "danger-full-access"]}, None),
        ({"type": "string", "enum": ["read-only", "danger-full-access"]}, None),
        (
            None,
            {"oneOf": [{"type": "string", "enum": ["untrusted", "never"]}]},
        ),
        (None, {"type": "string", "const": "never"}),
    ],
)
def test_thread_start_requires_exact_semantic_enum_literals(
    tmp_path: Path,
    sandbox: JsonValue | None,
    approval_policy: JsonValue | None,
) -> None:
    variant = thread_start_variant(sandbox=sandbox, approval_policy=approval_policy)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


def test_thread_start_accepts_semantic_enum_literals_behind_refs(tmp_path: Path) -> None:
    variant = thread_start_variant(
        sandbox={"$ref": "Types.json#/defs/SandboxMode"},
        approval_policy={"$ref": "Types.json#/defs/AskForApproval"},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps({"defs": {"SandboxMode": _SANDBOX_MODE, "AskForApproval": _ASK_FOR_APPROVAL}}),
        encoding="utf-8",
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


def test_thread_start_requires_read_only_never_profile_witness(tmp_path: Path) -> None:
    variant = thread_start_variant(
        approval_policy={"type": "string", "const": "on-request"},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    "narrowed",
    [
        {"type": "string", "enum": ["danger-full-access"]},
        {"type": "string", "const": "danger-full-access"},
        {"type": "string", "enum": ["read-only"]},
        {"type": "string", "enum": ["workspace-write"]},
        {"anyOf": [{"type": "string", "const": "danger-full-access"}]},
        {"oneOf": [{"type": "string", "enum": ["danger-full-access"]}]},
        {"allOf": [{"type": "string", "const": "danger-full-access"}]},
        {"type": "string", "enum": []},
        {"anyOf": []},
        {"oneOf": "not-a-list"},
        {"allOf": []},
    ],
)
def test_thread_start_rejects_params_any_of_branch_narrowing_sandbox_literals(
    tmp_path: Path,
    narrowed: dict[str, JsonValue],
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema["anyOf"] = [
        {"type": "object", "properties": {"sandbox": narrowed}},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    "narrowed",
    [
        {"type": "string", "enum": ["never"]},
        {"oneOf": [{"type": "string", "enum": ["untrusted", "never"]}]},
        {"allOf": [{"type": "string", "const": "never"}]},
    ],
)
def test_thread_start_rejects_params_any_of_branch_narrowing_approval_literals(
    tmp_path: Path,
    narrowed: dict[str, JsonValue],
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema["anyOf"] = [
        {"type": "object", "properties": {"approvalPolicy": narrowed}},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("keyword", ["oneOf", "allOf"])
def test_thread_start_rejects_params_one_of_and_all_of_narrowing_semantic_literals(
    tmp_path: Path,
    keyword: str,
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema[keyword] = [
        {
            "type": "object",
            "properties": {"sandbox": {"type": "string", "const": "danger-full-access"}},
        },
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


def test_thread_start_accepts_params_composition_permitting_every_semantic_literal(
    tmp_path: Path,
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema["anyOf"] = [
        {
            "type": "object",
            "properties": {
                "sandbox": {"type": "string", "enum": ["read-only", "danger-full-access"]},
            },
        },
        {
            "type": "object",
            "properties": {
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"]},
                "approvalPolicy": {"oneOf": [{"type": "string", "enum": ["on-request", "never"]}]},
            },
        },
    ]
    params_schema["allOf"] = [
        {"type": "object", "properties": {"cwd": {"type": ["string", "null"]}}},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START) == ClientMethodContract(
        "alias-thread-start",
        ParamsKind.OBJECT,
        frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
    )


def test_params_literal_any_of_rejects_a_later_unresolvable_branch(tmp_path: Path) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema["anyOf"] = [
        {
            "type": "object",
            "properties": {
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write"],
                    # The first branch admits every semantic literal, so the missing
                    # reference is only reached when every anyOf branch is resolved.
                    "anyOf": [{"type": "string"}, {"$ref": "Missing.json#/branch"}],
                }
            },
        }
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


@pytest.mark.parametrize(
    "branches",
    [
        [
            {"type": "object", "required": ["unknownA"]},
            {"type": "object", "required": ["unknownB"]},
        ],
        [{"type": "object"}, {"type": "object"}],
        [
            {"type": "object", "properties": {"cwd": {"type": ["string", "null"]}}},
            {"type": "object", "properties": {"ephemeral": {"type": ["boolean", "null"]}}},
        ],
        # The emitted inherit profile carries both members, so each branch admits the
        # params object moco actually sends and the method fails closed.
        [
            {"type": "object", "required": ["cwd"]},
            {"type": "object", "required": ["ephemeral"]},
        ],
    ],
)
def test_thread_start_params_one_of_needs_exactly_one_accepting_branch(
    tmp_path: Path,
    branches: list[dict[str, JsonValue]],
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema["oneOf"] = cast("JsonValue", branches)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


def test_thread_start_accepts_params_one_of_with_exactly_one_accepting_branch(
    tmp_path: Path,
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    # Only the object branch can carry the params member, so exactly one branch accepts.
    params_schema["oneOf"] = [
        {"type": "object", "properties": {"cwd": {"type": ["string", "null"]}}},
        {"type": "string"},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START) == ClientMethodContract(
        "alias-thread-start",
        ParamsKind.OBJECT,
        frozenset({"cwd", "ephemeral", "sandbox", "approvalPolicy"}),
    )


@pytest.mark.parametrize(
    ("sandbox", "approval_policy"),
    [
        (
            {
                "oneOf": [
                    {"type": "string", "enum": ["read-only", "workspace-write"]},
                    {
                        "type": "string",
                        "enum": ["read-only", "workspace-write", "danger-full-access"],
                    },
                ]
            },
            None,
        ),
        (
            None,
            {
                "oneOf": [
                    {"type": "string", "enum": ["untrusted", "on-request"]},
                    {"type": "string", "enum": ["on-request", "never"]},
                ]
            },
        ),
    ],
)
def test_thread_start_rejects_literal_one_of_matching_more_than_one_branch(
    tmp_path: Path,
    sandbox: JsonValue | None,
    approval_policy: JsonValue | None,
) -> None:
    variant = thread_start_variant(sandbox=sandbox, approval_policy=approval_policy)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


def test_thread_start_accepts_literal_one_of_matching_exactly_one_branch(tmp_path: Path) -> None:
    variant = thread_start_variant(
        sandbox={
            "oneOf": [
                {"type": "string", "enum": ["read-only", "workspace-write"]},
                {"type": "string", "enum": ["danger-full-access"]},
            ]
        },
        approval_policy={
            "oneOf": [
                {"type": "string", "enum": ["untrusted", "on-request", "never"]},
                {
                    "type": "object",
                    "required": ["granular"],
                    "properties": {"granular": {"type": "object"}},
                },
            ]
        },
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


def test_thread_start_rejects_ref_backed_literal_one_of_with_duplicate_branches(
    tmp_path: Path,
) -> None:
    variant = thread_start_variant(sandbox={"$ref": "Types.json#/defs/SandboxChoice"})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps(
            {
                "defs": {
                    "SandboxChoice": {
                        "oneOf": [{"$ref": "#/defs/Modes"}, {"$ref": "#/defs/Modes"}]
                    },
                    "Modes": _SANDBOX_MODE,
                }
            }
        ),
        encoding="utf-8",
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


def test_object_request_rejects_unknown_required_envelope_field(tmp_path: Path) -> None:
    variant = turn_start_variant()
    required = cast("list[JsonValue]", variant["required"])
    variant["required"] = [*required, "futureEnvelopeField"]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    "envelope",
    [
        {"anyOf": [{"type": "object", "required": ["futureEnvelopeField"]}]},
        {"anyOf": [{"type": "object", "properties": {"params": {"type": "array"}}}]},
        {"anyOf": [{"type": "object", "properties": {"method": {"type": "integer"}}}]},
        {"anyOf": [{"type": "string"}]},
        {"anyOf": []},
        {"oneOf": [{"type": "object", "required": ["futureEnvelopeField"]}]},
        {"oneOf": []},
        {"allOf": [{"type": "object", "required": ["futureEnvelopeField"]}]},
        {"allOf": [{"type": "object", "properties": {"params": {"type": "null"}}}]},
        {"allOf": "not-a-list"},
    ],
)
def test_object_request_envelope_composition_must_accept_method_and_params(
    tmp_path: Path,
    envelope: dict[str, JsonValue],
) -> None:
    variant = turn_start_variant()
    variant.update(envelope)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    "branches",
    [
        [{"type": "object"}, {"type": "object"}],
        [{"type": "object"}, {"type": "object", "required": ["method"]}],
    ],
)
def test_object_request_envelope_one_of_rejects_more_than_one_accepting_branch(
    tmp_path: Path,
    branches: list[dict[str, JsonValue]],
) -> None:
    variant = turn_start_variant()
    variant["oneOf"] = cast("JsonValue", branches)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


def test_object_request_envelope_accepts_one_of_with_exactly_one_accepting_branch(
    tmp_path: Path,
) -> None:
    variant = turn_start_variant()
    variant["oneOf"] = [
        {"type": "object"},
        {"type": "object", "required": ["futureEnvelopeField"]},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-turn-start",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )


@pytest.mark.parametrize(
    "unresolvable",
    [
        {"$ref": "Missing.json#/branch"},
        {"$ref": "Types.json#/defs/Loop"},
    ],
)
def test_object_request_envelope_any_of_rejects_a_later_unresolvable_branch(
    tmp_path: Path,
    unresolvable: dict[str, JsonValue],
) -> None:
    variant = turn_start_variant()
    # The first branch accepts the envelope, so the unresolvable branch is only reached
    # when every anyOf branch is resolved.
    variant["anyOf"] = cast("JsonValue", [{"type": "object"}, unresolvable])
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps({"defs": {"Loop": {"$ref": "#/defs/Loop"}}}),
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_object_request_accepts_the_transport_envelope_carrying_the_request_id(
    tmp_path: Path,
) -> None:
    variant = turn_start_variant()
    variant_properties(variant)["id"] = {
        "anyOf": [{"type": "string"}, {"type": "integer", "format": "int64"}]
    }
    variant["required"] = ["id", "method", "params"]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-turn-start",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )


def test_object_request_rejects_envelope_that_cannot_carry_the_transport_request_id(
    tmp_path: Path,
) -> None:
    variant = turn_start_variant()
    variant_properties(variant)["id"] = {"type": "string"}
    variant["required"] = ["id", "method", "params"]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    ("semantic", "variant"),
    [
        (
            SemanticMethod.THREAD_START,
            schema_variant(
                "thread-unknown-required",
                params_title="ThreadStartParams",
                params_required={"model"},
                params_properties=thread_start_properties(overrides={"model": {"type": "string"}}),
            ),
        ),
        (
            SemanticMethod.THREAD_START,
            schema_variant(
                "thread-cwd-type",
                params_title="ThreadStartParams",
                params_properties=thread_start_properties(
                    overrides={"cwd": {"type": ["integer", "null"]}}
                ),
            ),
        ),
        (
            SemanticMethod.THREAD_START,
            schema_variant(
                "thread-ephemeral-type",
                params_title="ThreadStartParams",
                params_properties=thread_start_properties(
                    overrides={"ephemeral": {"type": ["string", "null"]}}
                ),
            ),
        ),
        (
            SemanticMethod.THREAD_START,
            schema_variant(
                "thread-sandbox-missing",
                params_title="ThreadStartParams",
                params_properties={
                    key: value
                    for key, value in thread_start_properties().items()
                    if key != "sandbox"
                },
            ),
        ),
        (
            SemanticMethod.TURN_START,
            schema_variant(
                "turn-unknown-required",
                params_title="TurnStartParams",
                params_required={"input", "threadId", "collaborationMode"},
                params_properties=turn_start_properties({"collaborationMode": {"type": "object"}}),
            ),
        ),
        (
            SemanticMethod.TURN_START,
            schema_variant(
                "turn-input-type",
                params_title="TurnStartParams",
                params_required={"input", "threadId"},
                params_properties=turn_start_properties({"input": {"type": "object"}}),
            ),
        ),
        (
            SemanticMethod.TURN_START,
            schema_variant(
                "turn-thread-id-type",
                params_title="TurnStartParams",
                params_required={"input", "threadId"},
                params_properties=turn_start_properties({"threadId": {"type": "integer"}}),
            ),
        ),
        (
            SemanticMethod.TURN_START,
            schema_variant(
                "turn-thread-id-missing",
                params_title="TurnStartParams",
                params_required={"input"},
                params_properties={"input": {"type": "array"}},
            ),
        ),
    ],
)
def test_agent_execution_params_reject_unknown_required_or_incompatible_types(
    tmp_path: Path,
    semantic: SemanticMethod,
    variant: dict[str, JsonValue],
) -> None:
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(semantic) is None
    assert semantic in contract.missing_methods


def test_agent_execution_request_titles_participate_in_ambiguity_detection(
    tmp_path: Path,
) -> None:
    variant = schema_variant(
        "conflict",
        params_title="ThreadStartParams",
        request_title="Turn/startRequest",
        params_properties=thread_start_properties(),
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    with pytest.raises(CodexSchemaError, match="ambiguous"):
        load_generated_contract(tmp_path, version="fake")


def test_type_lists_any_of_and_refs_are_resolved_for_invocation(tmp_path: Path) -> None:
    definitions: dict[str, JsonValue] = {
        "Cursor": {"anyOf": [{"type": "string"}, {"$ref": "#/defs/Null"}]},
        "Null": {"type": ["null"]},
    }
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={"cursor": {"$ref": "Types.json#/defs/Cursor"}},
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])
    (tmp_path / "Types.json").write_text(json.dumps({"defs": definitions}), encoding="utf-8")

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.EXPERIMENTAL_FEATURE_LIST).semantic_fields == (
        frozenset({"cursor"})
    )


def test_direct_type_and_any_of_are_intersected_for_invocation(tmp_path: Path) -> None:
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={
            "cursor": {
                "type": "string",
                "anyOf": [{"type": "string"}, {"type": "null"}],
            }
        },
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.EXPERIMENTAL_FEATURE_LIST) is None
    assert SemanticMethod.EXPERIMENTAL_FEATURE_LIST in contract.missing_methods


@pytest.mark.parametrize(
    "ephemeral",
    [
        {"allOf": [{"type": "boolean"}, {"type": "string"}]},
        {"allOf": [{"type": ["boolean", "null"]}, {"type": "null"}]},
        {"oneOf": [{"type": "boolean"}, {"type": ["boolean", "null"]}]},
        {"oneOf": [{"type": "string"}, {"type": "null"}]},
        {"oneOf": []},
        {"allOf": "not-a-list"},
        {
            "anyOf": [
                {"oneOf": [{"type": "boolean"}, {"type": ["boolean", "null"]}]},
                {"type": "null"},
            ]
        },
    ],
)
def test_thread_start_rejects_supplied_type_composition_excluding_the_supplied_type(
    tmp_path: Path,
    ephemeral: dict[str, JsonValue],
) -> None:
    variant = thread_start_variant(overrides={"ephemeral": ephemeral})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    "ephemeral",
    [
        {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
        {"allOf": [{"type": ["boolean", "null"]}, {"type": ["boolean", "integer"]}]},
        {"anyOf": [{"oneOf": [{"type": "boolean"}, {"type": "string"}]}, {"type": "null"}]},
    ],
)
def test_thread_start_accepts_supplied_type_composition_admitting_the_supplied_type(
    tmp_path: Path,
    ephemeral: dict[str, JsonValue],
) -> None:
    variant = thread_start_variant(overrides={"ephemeral": ephemeral})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


def test_supplied_type_any_of_rejects_a_later_unresolvable_branch(tmp_path: Path) -> None:
    variant = thread_start_variant(
        overrides={
            "ephemeral": {
                "oneOf": [
                    {
                        "type": ["boolean", "null"],
                        # The first branch admits the supplied boolean, so the missing
                        # reference is only reached when every anyOf branch is resolved.
                        "anyOf": [{"type": "boolean"}, {"$ref": "Missing.json#/branch"}],
                    }
                ]
            }
        }
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_thread_start_rejects_ref_backed_supplied_type_one_of_with_duplicate_branches(
    tmp_path: Path,
) -> None:
    variant = thread_start_variant(overrides={"ephemeral": {"$ref": "Types.json#/defs/Flag"}})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps(
            {
                "defs": {
                    "Flag": {"oneOf": [{"$ref": "#/defs/Bool"}, {"$ref": "#/defs/Bool"}]},
                    "Bool": {"type": ["boolean", "null"]},
                }
            }
        ),
        encoding="utf-8",
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    "branches",
    [
        [
            {"type": "object", "required": ["unknownA"]},
            {"type": "object", "required": ["unknownB"]},
        ],
        [
            {
                "type": "object",
                "properties": {"cwd": {"type": "integer"}},
            }
        ],
    ],
)
def test_any_of_branch_must_accept_the_complete_object_invocation(
    tmp_path: Path,
    branches: list[dict[str, JsonValue]],
) -> None:
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    properties = cast("dict[str, JsonValue]", variant["properties"])
    params_schema = cast("dict[str, JsonValue]", properties["params"])
    params_schema["anyOf"] = cast("JsonValue", branches)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


def test_any_of_self_reference_is_a_stable_schema_error(tmp_path: Path) -> None:
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={"cursor": {"$ref": "Types.json#/defs/Loop"}},
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps({"defs": {"Loop": {"anyOf": [{"$ref": "#/defs/Loop"}]}}}),
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_excessive_any_of_depth_is_a_stable_schema_error(tmp_path: Path) -> None:
    cursor_schema: dict[str, JsonValue] = {"type": ["string", "null"]}
    for _ in range(129):
        cursor_schema = {"anyOf": [cursor_schema]}
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={"cursor": cursor_schema},
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])

    with pytest.raises(CodexSchemaError, match="generated schema is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_same_ref_in_independent_any_of_branches_is_allowed(tmp_path: Path) -> None:
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={"cursor": {"$ref": "Types.json#/defs/Cursor"}},
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps(
            {
                "defs": {
                    "Cursor": {
                        "anyOf": [
                            {"$ref": "#/defs/Scalar"},
                            {"$ref": "#/defs/Scalar"},
                            {"type": "null"},
                        ]
                    },
                    "Scalar": {"type": "string"},
                }
            }
        ),
        encoding="utf-8",
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.EXPERIMENTAL_FEATURE_LIST).name == (
        "feature-alias"
    )


def test_repeated_ref_dag_exceeding_work_budget_is_a_stable_error(tmp_path: Path) -> None:
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={"cursor": {"$ref": "Types.json#/defs/Level9"}},
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])
    definitions: dict[str, JsonValue] = {
        "Level0": {"type": ["string", "null"]},
    }
    for level in range(1, 10):
        definitions[f"Level{level}"] = {
            "anyOf": [{"$ref": f"#/defs/Level{level - 1}"} for _ in range(4)]
        }
    (tmp_path / "Types.json").write_text(
        json.dumps({"defs": definitions}),
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="generated schema is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_ref_with_instance_assertion_sibling_is_rejected(tmp_path: Path) -> None:
    feature = schema_variant(
        "feature-alias",
        params_title="ExperimentalFeatureListParams",
        params_properties={
            "cursor": {
                "$ref": "Types.json#/defs/Cursor",
                "type": "integer",
            }
        },
    )
    write_schema_bundle(tmp_path, client_variants=[feature], server_variants=[])
    (tmp_path / "Types.json").write_text(
        json.dumps({"defs": {"Cursor": {"type": ["string", "null"]}}}),
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


@pytest.mark.parametrize(
    "ephemeral",
    [
        {"type": "boolean", "const": False},
        {"type": "boolean", "enum": [False]},
        {"type": ["boolean", "null"], "enum": [None]},
        {"anyOf": [{"type": "boolean", "const": False}, {"type": "null"}]},
    ],
)
def test_thread_start_rejects_ephemeral_values_moco_never_sends(
    tmp_path: Path,
    ephemeral: dict[str, JsonValue],
) -> None:
    variant = thread_start_variant(overrides={"ephemeral": ephemeral})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    "ephemeral",
    [
        {"type": "boolean", "const": True},
        {"type": "boolean", "enum": [True, False]},
        {"anyOf": [{"type": "boolean", "const": True}, {"type": "null"}]},
    ],
)
def test_thread_start_accepts_ephemeral_values_moco_actually_sends(
    tmp_path: Path,
    ephemeral: dict[str, JsonValue],
) -> None:
    variant = thread_start_variant(overrides={"ephemeral": ephemeral})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


# JSON Schema requires the values of one `enum` to be unique, and JSON value equality is not
# Python's: `true` and `1` are different JSON values, while `1` and `1.0` are one number.
# Arrays compare positionally, and objects compare independently of member order.
DUPLICATE_ENUM_VALUES = [
    pytest.param(["stay", "stay"], id="repeated-strings"),
    pytest.param([False, False], id="repeated-booleans"),
    pytest.param([None, None], id="repeated-nulls"),
    pytest.param([1, 1.0], id="one-number-in-two-python-types"),
    pytest.param([[1, "a"], [1, "a"]], id="repeated-arrays"),
    pytest.param(
        [{"a": 1, "b": 2}, {"b": 2, "a": 1}],
        id="objects-differing-only-in-member-order",
    ),
    pytest.param(
        [{"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}],
        id="repeated-nested-objects",
    ),
]
# Distinct JSON values a bare Python set would wrongly collapse, or would wrongly keep apart.
UNIQUE_ENUM_VALUES = [
    pytest.param([False, 0], id="boolean-and-number-stay-distinct"),
    pytest.param([1, 2], id="distinct-numbers"),
    pytest.param(["stay", "leave"], id="distinct-strings"),
    pytest.param([[1, 2], [2, 1]], id="arrays-differing-by-position"),
    pytest.param([{"a": 1}, {"a": 2}], id="objects-differing-by-member-value"),
]


def nested_json_value(depth: int, leaf: JsonValue) -> JsonValue:
    """Build a JSON value wrapped in `depth` objects, to exceed the value-reading budget."""
    value: JsonValue = leaf
    for _ in range(depth):
        value = {"nested": value}
    return value


@pytest.mark.parametrize("candidates", DUPLICATE_ENUM_VALUES)
def test_one_of_sibling_repeating_an_enum_value_keeps_the_fixed_value_unavailable(
    tmp_path: Path,
    candidates: list[JsonValue],
) -> None:
    """A repeated enum value is unreadable, so the sibling never proves exactly-one match."""
    variant = thread_start_variant(
        overrides={"ephemeral": {"oneOf": [{"enum": [True]}, {"enum": candidates}]}}
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("candidates", UNIQUE_ENUM_VALUES)
def test_one_of_sibling_with_unique_enum_values_keeps_its_definite_rejection(
    tmp_path: Path,
    candidates: list[JsonValue],
) -> None:
    """Distinct JSON values stay readable, so the sibling still rejects the value moco sends."""
    variant = thread_start_variant(
        overrides={"ephemeral": {"oneOf": [{"enum": [True]}, {"enum": candidates}]}}
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


def test_enum_repeating_the_value_moco_sends_makes_the_method_unavailable(tmp_path: Path) -> None:
    """The declaration applies directly to `ephemeral`, and a repeated value cannot be read."""
    variant = thread_start_variant(overrides={"ephemeral": {"enum": [True, True]}})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize(
    "candidates",
    [
        pytest.param([True, 1], id="boolean-beside-integer"),
        pytest.param([True, 1.0], id="boolean-beside-number"),
        pytest.param([True, "true"], id="boolean-beside-string"),
    ],
)
def test_enum_listing_distinct_json_types_still_admits_the_value_moco_sends(
    tmp_path: Path,
    candidates: list[JsonValue],
) -> None:
    """JSON keeps `true`, `1` and `"true"` apart, so the listed values are unique and readable."""
    variant = thread_start_variant(overrides={"ephemeral": {"enum": candidates}})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


def test_enum_values_nested_beyond_the_reading_budget_stay_undecided(tmp_path: Path) -> None:
    """Values too deep to compare leave uniqueness unproven, so the branch cannot reject."""
    candidates: list[JsonValue] = [
        nested_json_value(64, "leaf"),
        nested_json_value(64, "other"),
    ]
    variant = thread_start_variant(
        overrides={"ephemeral": {"oneOf": [{"enum": [True]}, {"enum": candidates}]}}
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


# Draft-07 declares `enum` to be a non-empty array of unique values, so an empty list, an
# absent list and a non-list are all declarations this evaluator cannot read. None of them
# may be reported as a definite rejection, nor as an absent constraint admitting everything.
UNREADABLE_ENUM_DECLARATIONS = [
    pytest.param([], id="empty-enum-list"),
    pytest.param(None, id="null-enum"),
    pytest.param("read-only", id="non-list-enum"),
    pytest.param({"0": True}, id="object-enum"),
]


@pytest.mark.parametrize("candidates", UNREADABLE_ENUM_DECLARATIONS)
def test_one_of_sibling_with_an_unreadable_enum_never_proves_exactly_one_match(
    tmp_path: Path,
    candidates: JsonValue,
) -> None:
    """A malformed sibling enum may admit the value moco sends, so exactly-one stays unproven."""
    variant = thread_start_variant(
        overrides={"ephemeral": {"oneOf": [{"enum": [True]}, {"enum": candidates}]}}
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("candidates", UNREADABLE_ENUM_DECLARATIONS)
def test_unreadable_enum_applied_directly_keeps_the_fixed_value_unavailable(
    tmp_path: Path,
    candidates: JsonValue,
) -> None:
    """The declaration constrains `ephemeral` itself, and an unreadable one proves nothing."""
    variant = thread_start_variant(overrides={"ephemeral": {"enum": candidates}})
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("candidates", UNREADABLE_ENUM_DECLARATIONS)
def test_unreadable_enum_declarations_are_undecided_rather_than_definite(
    candidates: SchemaJsonValue,
) -> None:
    """Neither a match nor a rejection can be read out of a malformed `enum` declaration."""
    assert _enum_admits({"enum": candidates}, value=True) is _Admission.UNDECIDED


def test_well_formed_enum_declarations_stay_definite() -> None:
    """A non-empty list of unique values keeps deciding the value moco sends, either way."""
    assert _enum_admits({}, value=True) is _Admission.ADMITTED
    assert _enum_admits({"enum": [True]}, value=True) is _Admission.ADMITTED
    assert _enum_admits({"enum": [True, False]}, value=True) is _Admission.ADMITTED
    assert _enum_admits({"enum": [None]}, value=None) is _Admission.ADMITTED
    assert _enum_admits({"enum": [False]}, value=True) is _Admission.REJECTED
    assert _enum_admits({"enum": ["read-only"]}, value="workspace-write") is _Admission.REJECTED


@pytest.mark.parametrize(
    "input_schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "array", "items": {"type": "object"}, "maxItems": 0},
        {"type": "array", "items": {"type": "object"}, "minItems": 2},
        {"type": "array", "items": True},
        {"type": "array", "items": [{"type": "object"}]},
        {"type": "array", "items": {"type": "object"}, "maxItems": "one"},
        user_input_array(bounds={"maxItems": 0}),
        user_input_array(
            text_user_input_branch(
                properties={
                    "text": {"type": "string"},
                    "type": {"type": "string", "enum": ["image"]},
                },
            )
        ),
        user_input_array(
            text_user_input_branch(
                properties={
                    "text": {"type": "integer"},
                    "type": {"type": "string", "enum": ["text"]},
                },
            )
        ),
        user_input_array(
            text_user_input_branch(
                properties={"type": {"type": "string", "enum": ["text"]}},
                required=["type"],
                additional_properties=False,
            )
        ),
        user_input_array(
            text_user_input_branch(required=["attachments", "text", "type"]),
        ),
    ],
)
def test_turn_start_rejects_input_arrays_that_cannot_carry_one_text_item(
    tmp_path: Path,
    input_schema: JsonValue,
) -> None:
    variant = turn_start_variant(input_schema=input_schema)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    "input_schema",
    [
        user_input_array(),
        user_input_array(bounds={"minItems": 1, "maxItems": 4}),
        user_input_array(text_user_input_branch(additional_properties=True)),
        user_input_array(
            text_user_input_branch(
                properties={
                    "text": {
                        "type": "string",
                        "title": "Text",
                        "description": "The user message.",
                        "default": "",
                        "examples": ["hello"],
                        "format": "text",
                        "deprecated": False,
                        "readOnly": False,
                    },
                    "type": {"type": "string", "enum": ["text"]},
                },
            )
        ),
    ],
)
def test_turn_start_accepts_the_generated_user_input_union(
    tmp_path: Path,
    input_schema: JsonValue,
) -> None:
    variant = turn_start_variant(input_schema=input_schema)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-turn-start",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )


@pytest.mark.parametrize(
    "composition",
    [
        {"anyOf": [{"type": "object", "properties": {"params": {"required": ["future"]}}}]},
        {"allOf": [{"type": "object", "properties": {"params": {"required": ["future"]}}}]},
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "params": {
                            "type": "object",
                            "properties": {"input": {"type": "array", "maxItems": 0}},
                        }
                    },
                }
            ]
        },
        {"anyOf": [{"type": "object", "properties": {"method": {"const": "other/alias"}}}]},
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"method": {"type": "string", "enum": ["other/alias"]}},
                }
            ]
        },
        {"anyOf": [{"type": "object", "properties": {"id": {"type": "string"}}}]},
    ],
)
def test_object_request_rejects_outer_composition_narrowing_the_emitted_request(
    tmp_path: Path,
    composition: dict[str, JsonValue],
) -> None:
    variant = turn_start_variant()
    variant.update(composition)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    "composition",
    [
        {"anyOf": [{"type": "object", "properties": {"method": {"const": "alias-turn-start"}}}]},
        {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"method": {"type": "string", "enum": ["alias-turn-start"]}},
                }
            ]
        },
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "params": {"type": "object", "properties": {"threadId": {"type": "string"}}}
                    },
                }
            ]
        },
    ],
)
def test_object_request_accepts_outer_composition_binding_the_extracted_alias(
    tmp_path: Path,
    composition: dict[str, JsonValue],
) -> None:
    variant = turn_start_variant()
    variant.update(composition)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START).name == "alias-turn-start"


@pytest.mark.parametrize(
    ("semantic", "variant"),
    [
        (
            SemanticMethod.CONFIG_READ,
            schema_variant(
                "config-enum-cwd",
                params_title="ConfigReadParams",
                params_properties={"cwd": {"type": "string", "enum": ["/only/one/cwd"]}},
            ),
        ),
        (
            SemanticMethod.CONFIG_READ,
            schema_variant(
                "config-const-cwd",
                params_title="ConfigReadParams",
                params_properties={"cwd": {"type": "string", "const": "/only/one/cwd"}},
            ),
        ),
        (
            SemanticMethod.TURN_INTERRUPT,
            schema_variant(
                "interrupt-const-thread",
                params_title="TurnInterruptParams",
                params_required={"threadId", "turnId"},
                params_properties={
                    "threadId": {"type": "string", "const": "thread-1"},
                    "turnId": {"type": "string"},
                },
            ),
        ),
        (
            SemanticMethod.TURN_INTERRUPT,
            schema_variant(
                "interrupt-enum-turn",
                params_title="TurnInterruptParams",
                params_required={"threadId", "turnId"},
                params_properties={
                    "threadId": {"type": "string"},
                    "turnId": {"type": "string", "enum": ["turn-1", "turn-2"]},
                },
            ),
        ),
        (
            SemanticMethod.EXPERIMENTAL_FEATURE_LIST,
            schema_variant(
                "feature-null-only-cursor",
                params_title="ExperimentalFeatureListParams",
                params_properties={"cursor": {"type": "null"}},
            ),
        ),
        (
            SemanticMethod.EXPERIMENTAL_FEATURE_LIST,
            schema_variant(
                "feature-enumerated-cursor",
                params_title="ExperimentalFeatureListParams",
                params_properties={
                    "cursor": {"type": ["string", "null"], "enum": ["page-2", None]},
                },
            ),
        ),
        (
            SemanticMethod.THREAD_START,
            thread_start_variant(
                "thread-enum-cwd",
                overrides={"cwd": {"type": ["string", "null"], "enum": ["/only/one/cwd"]}},
            ),
        ),
    ],
)
def test_dynamic_request_fields_reject_value_constraints_moco_cannot_guarantee(
    tmp_path: Path,
    semantic: SemanticMethod,
    variant: dict[str, JsonValue],
) -> None:
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(semantic) is None
    assert semantic in contract.missing_methods


@pytest.mark.parametrize(
    "cwd",
    [
        {"type": "string", "pattern": "^/"},
        {"type": "string", "minLength": 1},
        {"type": "string", "not": {"const": "/workspace"}},
        {"type": "object", "propertyNames": {"type": "string"}},
        {"type": "string", "if": {"const": "/workspace"}, "then": {"const": "/workspace"}},
    ],
)
def test_uninterpretable_assertions_make_the_method_unavailable(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize(
    "undecidable",
    [
        {"type": "string", "const": RUNTIME_CWD},
        {"type": "string", "enum": [RUNTIME_CWD, OTHER_RUNTIME_CWD]},
        {"type": "string", "pattern": "^/"},
        {"anyOf": [{"type": "string", "const": RUNTIME_CWD}]},
        {"anyOf": [{"type": "string", "enum": [RUNTIME_CWD, OTHER_RUNTIME_CWD]}]},
        {"anyOf": [{"type": "string", "pattern": "^/"}]},
        {"allOf": [{"type": "string"}, {"type": "string", "const": RUNTIME_CWD}]},
        {"allOf": [{"type": "string", "enum": [RUNTIME_CWD]}]},
        {"allOf": [{"type": "string", "minLength": 1}]},
    ],
)
def test_one_of_branch_moco_cannot_exclude_keeps_the_method_unavailable(
    tmp_path: Path,
    undecidable: dict[str, JsonValue],
) -> None:
    """A runtime `cwd` equal to the narrowed branch matches both, so `oneOf` is violated."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"oneOf": [{"type": "string"}, undecidable]}},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize(
    "unsupported",
    [
        {"type": "boolean", "not": {"const": False}},
        {"type": "boolean", "if": {"const": True}, "then": {"const": True}},
    ],
)
def test_one_of_branch_asserting_beyond_the_evaluator_keeps_a_fixed_value_unavailable(
    tmp_path: Path,
    unsupported: dict[str, JsonValue],
) -> None:
    """The fixed `ephemeral` true also satisfies the second branch, so `oneOf` may be violated."""
    variant = thread_start_variant(
        overrides={"ephemeral": {"oneOf": [{"type": "boolean", "const": True}, unsupported]}}
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


def test_one_of_branch_asserting_beyond_the_evaluator_keeps_the_alias_unavailable(
    tmp_path: Path,
) -> None:
    """The extracted alias also satisfies the unsupported pattern, so `oneOf` may be violated."""
    variant = turn_start_variant()
    variant_properties(variant)["method"] = {
        "type": "string",
        "enum": ["alias-turn-start"],
        "oneOf": [
            {"const": "alias-turn-start"},
            {"type": "string", "pattern": "^alias-"},
        ],
    }
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


def test_object_request_outer_one_of_discriminates_params_by_required_members(
    tmp_path: Path,
) -> None:
    """The emitted TurnStart params carry `input`, so exactly one outer branch admits it."""
    variant = turn_start_variant()
    variant["oneOf"] = [
        {"type": "object", "properties": {"params": {"type": "object", "required": ["input"]}}},
        {"type": "object", "properties": {"params": {"type": "object", "required": ["future"]}}},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-turn-start",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )


@pytest.mark.parametrize(
    "cwd",
    [
        {"anyOf": [{"type": "string", "pattern": "^/"}]},
        {"anyOf": [{"type": "string", "minLength": 1}, {"type": "string", "minLength": 2}]},
        {"allOf": [{"type": "string"}, {"type": "string", "not": {"const": "/workspace"}}]},
        {"oneOf": [{"type": "string", "propertyNames": {"type": "string"}}]},
        {"anyOf": [{"anyOf": [{"type": "string", "if": {"const": "/workspace"}}]}]},
    ],
)
def test_uninterpretable_assertions_inside_composition_make_the_method_unavailable(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize(
    "unreadable",
    [
        {"type": "object", "properties": ["cwd"], "additionalProperties": False},
        {"type": "object", "required": "cwd", "additionalProperties": False},
    ],
)
def test_one_of_branch_with_an_unreadable_member_declaration_stays_unavailable(
    tmp_path: Path,
    unreadable: dict[str, JsonValue],
) -> None:
    """An unreadable declaration cannot prove the branch rejects the params moco sends."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    variant_params_schema(variant)["oneOf"] = [{"type": "object"}, unreadable]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize("malformed", MALFORMED_TYPE_DECLARATIONS)
def test_one_of_branch_with_a_malformed_type_keeps_a_dynamic_value_unavailable(
    tmp_path: Path,
    malformed: dict[str, JsonValue],
) -> None:
    """An unreadable sibling may admit the runtime `cwd` too, so exactly-one stays unproven."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"oneOf": [{"type": "string"}, malformed]}},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize("malformed", MALFORMED_TYPE_DECLARATIONS)
def test_one_of_branch_with_a_malformed_type_keeps_a_fixed_value_unavailable(
    tmp_path: Path,
    malformed: dict[str, JsonValue],
) -> None:
    """An unreadable sibling may admit the fixed `ephemeral` true too, so `oneOf` is unproven."""
    variant = thread_start_variant(
        overrides={"ephemeral": {"oneOf": [{"type": "boolean"}, malformed]}}
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("malformed", MALFORMED_TYPE_DECLARATIONS)
def test_nested_malformed_type_keeps_the_array_item_union_unavailable(
    tmp_path: Path,
    malformed: dict[str, JsonValue],
) -> None:
    """An unreadable `text` declaration leaves the item union without a definite branch."""
    text_branch = text_user_input_branch(
        properties={
            "text": {"oneOf": [{"type": "string"}, malformed]},
            "type": {"type": "string", "enum": ["text"], "title": "TextUserInputType"},
        }
    )
    variant = turn_start_variant(input_schema=user_input_array(text_branch))
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param({"type": ["string", "string"]}, id="repeated-supplied-type"),
        pytest.param({"type": ["string", "null", "string"]}, id="repeated-type-inside-union"),
        pytest.param({"type": ["integer", "integer"]}, id="repeated-incompatible-type"),
    ],
)
def test_repeated_type_members_are_unreadable_for_the_value_moco_sends(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    """JSON Schema requires unique `type` members, so a repeated one is not a readable union."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


def test_one_of_sibling_with_a_repeated_type_member_keeps_the_dynamic_value_unavailable(
    tmp_path: Path,
) -> None:
    """An unreadable repeated-member branch is no definite rejection, so exactly-one is unproven."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={
            "cwd": {"oneOf": [{"type": "string"}, {"type": ["integer", "integer"]}]}
        },
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


def test_null_type_declaration_is_not_read_as_an_absent_declaration(tmp_path: Path) -> None:
    """A JSON null `type` declares nothing readable, so outside composition it fails closed."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": None}},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize("sibling", OBJECT_KEYWORDS_WITHOUT_TYPE)
def test_one_of_sibling_declaring_only_object_keywords_admits_the_dynamic_string(
    tmp_path: Path,
    sibling: dict[str, JsonValue],
) -> None:
    """Object keywords never reject a string, so the runtime `cwd` matches both branches."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"oneOf": [{"type": "string"}, sibling]}},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize("sibling", OBJECT_KEYWORDS_WITHOUT_TYPE)
def test_nested_one_of_sibling_without_a_type_keeps_the_array_item_union_unavailable(
    tmp_path: Path,
    sibling: dict[str, JsonValue],
) -> None:
    """The nested `text` string also matches the sibling, so the item union loses exactly-one."""
    text_branch = text_user_input_branch(
        properties={
            "text": {"oneOf": [{"type": "string"}, sibling]},
            "type": {"type": "string", "enum": ["text"], "title": "TextUserInputType"},
        }
    )
    variant = turn_start_variant(input_schema=user_input_array(text_branch))
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize("cwd", OBJECT_KEYWORDS_WITHOUT_TYPE)
def test_object_keywords_without_a_type_do_not_exclude_the_string_moco_sends(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    """An object keyword is not evaluated against a string, so it withdraws nothing."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_READ) == ClientMethodContract(
        "config-alias",
        ParamsKind.OBJECT,
        frozenset({"cwd"}),
    )


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param({"type": "string", "required": ["future"]}, id="required-beside-string"),
        pytest.param({"type": "string", "required": "future"}, id="malformed-required"),
        pytest.param(
            {"type": "string", "required": ["future", "future"]},
            id="repeated-required-member",
        ),
        pytest.param({"type": "string", "properties": ["future"]}, id="malformed-properties"),
        pytest.param({"type": "string", "additionalProperties": 5}, id="malformed-additional"),
    ],
)
def test_declared_string_keeps_inapplicable_object_keywords_from_withdrawing_the_method(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    """An unreadable object keyword cannot leave a declared string value in doubt."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_READ) == ClientMethodContract(
        "config-alias",
        ParamsKind.OBJECT,
        frozenset({"cwd"}),
    )


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param({"type": "object", "required": "future"}, id="malformed-required"),
        pytest.param({"type": "object", "properties": ["future"]}, id="malformed-properties"),
    ],
)
def test_malformed_object_keywords_still_fail_closed_for_the_object_moco_sends(
    tmp_path: Path,
    malformed: dict[str, JsonValue],
) -> None:
    """The keyword applies to the params object, so an unreadable declaration stays undecided."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    variant_params_schema(variant)["oneOf"] = [{"type": "object"}, malformed]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


@pytest.mark.parametrize(
    "required",
    [
        pytest.param(["cwd", "cwd"], id="repeated-emitted-member"),
        pytest.param(["cwd", "future", "future"], id="repeated-absent-member"),
    ],
)
def test_repeated_required_members_leave_the_params_object_undecided(
    tmp_path: Path,
    required: list[JsonValue],
) -> None:
    """A repeated `required` name is unreadable, never a declaration quietly deduplicated."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    variant_params_schema(variant)["required"] = required
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


def test_one_of_sibling_with_repeated_required_members_stays_unavailable(tmp_path: Path) -> None:
    """The repeated-member branch cannot be read as the definite rejection exactly-one needs."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    variant_params_schema(variant)["oneOf"] = [
        {"type": "object"},
        {"type": "object", "required": ["future", "future"]},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.CONFIG_READ) is None
    assert SemanticMethod.CONFIG_READ in contract.missing_methods


def test_one_of_sibling_with_unique_required_members_keeps_its_definite_rejection(
    tmp_path: Path,
) -> None:
    """A readable `required` the params object cannot satisfy still rejects the sibling branch."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": {"type": "string"}},
    )
    variant_params_schema(variant)["oneOf"] = [
        {"type": "object"},
        {"type": "object", "required": ["future", "later"]},
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_READ) == ClientMethodContract(
        "config-alias",
        ParamsKind.OBJECT,
        frozenset({"cwd"}),
    )


def test_request_id_matching_both_numeric_one_of_branches_is_unavailable(tmp_path: Path) -> None:
    """An integer id matches `integer` and `number`, so the envelope loses exactly-one."""
    variant = with_request_id(
        turn_start_variant(),
        {"oneOf": [{"type": "integer"}, {"type": "number"}]},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


@pytest.mark.parametrize(
    "id_schema",
    [
        pytest.param({"type": "number"}, id="number"),
        pytest.param({"type": ["number", "null"]}, id="number-union"),
        pytest.param({"oneOf": [{"type": "number"}, {"type": "string"}]}, id="one-of-number"),
        pytest.param({"anyOf": [{"type": "null"}, {"type": "number"}]}, id="any-of-number"),
    ],
)
def test_request_id_is_admitted_by_a_number_declaration(
    tmp_path: Path,
    id_schema: dict[str, JsonValue],
) -> None:
    """JSON Schema counts the outbound integer id as a number, so `number` definitely admits it."""
    variant = with_request_id(turn_start_variant(), id_schema)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-turn-start",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )


@pytest.mark.parametrize(
    "id_schema",
    [
        pytest.param({"oneOf": [{"type": "integer"}, {"type": "string"}]}, id="one-of-integer"),
        pytest.param({"oneOf": [{"type": "number"}, {"type": "object"}]}, id="one-of-number"),
    ],
)
def test_request_id_keeps_exactly_one_numeric_branch_available(
    tmp_path: Path,
    id_schema: dict[str, JsonValue],
) -> None:
    """A well-formed incompatible sibling still definitely rejects the integer id."""
    variant = with_request_id(turn_start_variant(), id_schema)
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_START) == ClientMethodContract(
        "alias-turn-start",
        ParamsKind.OBJECT,
        frozenset({"input", "threadId"}),
    )


@pytest.mark.parametrize(
    ("declaration", "json_type", "expected"),
    [
        pytest.param({"type": "integer"}, "integer", _Admission.ADMITTED, id="integer-integer"),
        pytest.param({"type": "number"}, "integer", _Admission.ADMITTED, id="number-integer"),
        pytest.param({"type": "integer"}, "number", _Admission.REJECTED, id="integer-number"),
        pytest.param({"type": "number"}, "number", _Admission.ADMITTED, id="number-number"),
        pytest.param({"type": "number"}, "string", _Admission.REJECTED, id="number-string"),
        pytest.param(
            {"type": ["integer", "null"]},
            "integer",
            _Admission.ADMITTED,
            id="integer-union-integer",
        ),
        pytest.param(
            {"type": ["integer", "null"]},
            "number",
            _Admission.REJECTED,
            id="integer-union-number",
        ),
        pytest.param({"type": 5}, "integer", _Admission.UNDECIDED, id="malformed-integer"),
        pytest.param({"required": ["future"]}, "string", _Admission.ADMITTED, id="absent-string"),
        pytest.param({"properties": {}}, "null", _Admission.ADMITTED, id="absent-null"),
    ],
)
def test_type_declarations_are_judged_directionally_per_json_type(
    declaration: dict[str, SchemaJsonValue],
    json_type: str,
    expected: _Admission,
) -> None:
    """`number` includes an integer instance, while `integer` never includes a fractional one."""
    assert _type_admits(declaration, json_type) is expected


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param(
            {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            id="well-formed-incompatible-one-of-sibling",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}, {"type": 5}]},
            id="malformed-any-of-sibling",
        ),
    ],
)
def test_readable_type_declarations_keep_their_composition_verdict(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    """A definitely incompatible sibling still rejects, and `anyOf` still needs one match."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_READ) == ClientMethodContract(
        "config-alias",
        ParamsKind.OBJECT,
        frozenset({"cwd"}),
    )


@pytest.mark.parametrize(
    "cwd",
    [
        {"anyOf": [{"type": "string"}, {"type": "string", "const": RUNTIME_CWD}]},
        {"anyOf": [{"type": "string"}, {"type": "string", "pattern": "^/"}]},
        {"anyOf": [{"type": "string", "minLength": 1}, {"type": ["string", "null"]}]},
    ],
)
def test_any_of_stays_available_when_one_branch_definitely_admits_the_dynamic_value(
    tmp_path: Path,
    cwd: dict[str, JsonValue],
) -> None:
    """A branch moco cannot decide never withdraws a branch that definitely admits the value."""
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={"cwd": cwd},
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_READ) == ClientMethodContract(
        "config-alias",
        ParamsKind.OBJECT,
        frozenset({"cwd"}),
    )


def test_annotation_only_fields_do_not_constrain_the_emitted_request(tmp_path: Path) -> None:
    variant = schema_variant(
        "config-alias",
        params_title="ConfigReadParams",
        params_properties={
            "cwd": {
                "type": ["string", "null"],
                "title": "Cwd",
                "description": "Optional working directory.",
                "default": None,
                "examples": ["/workspace"],
                "format": "path",
                "deprecated": False,
                "readOnly": False,
                "writeOnly": False,
                "$comment": "generated",
            }
        },
    )
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.CONFIG_READ) == ClientMethodContract(
        "config-alias",
        ParamsKind.OBJECT,
        frozenset({"cwd"}),
    )


def test_params_object_rejecting_additional_properties_still_admits_declared_fields(
    tmp_path: Path,
) -> None:
    variant = schema_variant(
        "interrupt-alias",
        params_title="TurnInterruptParams",
        params_required={"threadId", "turnId"},
        params_properties={"threadId": {"type": "string"}, "turnId": {"type": "string"}},
    )
    variant_params_schema(variant)["additionalProperties"] = False
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.TURN_INTERRUPT).name == "interrupt-alias"


def test_object_request_envelope_rejecting_additional_properties_must_declare_the_id(
    tmp_path: Path,
) -> None:
    variant = turn_start_variant()
    variant["additionalProperties"] = False
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.TURN_START) is None
    assert SemanticMethod.TURN_START in contract.missing_methods


def test_server_categories_aggregate_aliases_and_hide_unknown_methods(tmp_path: Path) -> None:
    titles = {
        ServerRequestCategory.COMMAND_APPROVAL: (
            "CommandExecutionRequestApprovalParams",
            "ExecCommandApprovalParams",
        ),
        ServerRequestCategory.FILE_CHANGE_APPROVAL: ("ApplyPatchApprovalParams",),
        ServerRequestCategory.USER_INPUT: ("ToolRequestUserInputParams",),
        ServerRequestCategory.MCP_ELICITATION: ("McpServerElicitationRequestParams",),
        ServerRequestCategory.PERMISSION_APPROVAL: ("PermissionsRequestApprovalParams",),
        ServerRequestCategory.DYNAMIC_TOOL_CALL: ("DynamicToolCallParams",),
        ServerRequestCategory.AUTH_TOKEN_REFRESH: ("ChatgptAuthTokensRefreshParams",),
        ServerRequestCategory.ATTESTATION: ("AttestationGenerateParams",),
        ServerRequestCategory.CURRENT_TIME: ("CurrentTimeReadParams",),
    }
    variants: list[dict[str, JsonValue]] = []
    expected: dict[ServerRequestCategory, frozenset[str]] = {}
    index = 0
    for category, params_titles in titles.items():
        aliases = []
        for params_title in params_titles:
            index += 1
            alias = f"server-alias-{index}"
            aliases.append(alias)
            variants.append(schema_variant(alias, params_title=params_title))
        expected[category] = frozenset(aliases)
    variants.append(schema_variant("DO-NOT-EXPOSE-UNKNOWN", params_title="FutureParams"))
    write_schema_bundle(tmp_path, client_variants=[], server_variants=variants)

    contract = load_generated_contract(tmp_path, version="fake")

    assert dict(contract.server_requests) == expected
    assert contract.server_request_categories == frozenset(ServerRequestCategory)
    assert contract.unclassified_server_request_count == 1
    assert "DO-NOT-EXPOSE-UNKNOWN" not in repr(contract.server_requests)


def test_server_variant_with_conflicting_category_signals_is_rejected(tmp_path: Path) -> None:
    variant = schema_variant("alias", params_title="ExecCommandApprovalParams")
    properties = cast("dict[str, JsonValue]", variant["properties"])
    properties["params"] = {
        "$ref": "#/defs/ApplyPatchApprovalParams",
        "title": "ExecCommandApprovalParams",
    }
    document: dict[str, JsonValue] = {
        "oneOf": [variant],
        "defs": {"ApplyPatchApprovalParams": {"type": "object"}},
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ClientRequest.json").write_text('{"oneOf": []}', encoding="utf-8")
    (tmp_path / "ServerRequest.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CodexSchemaError, match="ambiguous"):
        load_generated_contract(tmp_path, version="fake")


@pytest.mark.parametrize(
    "titles",
    [
        ("ExecCommandApprovalParams", "ApplyPatchApprovalParams"),
        ("CommandExecutionRequestApprovalParams", "FileChangeRequestApprovalParams"),
        ("ExecCommandApprovalParams", "ToolRequestUserInputParams"),
    ],
)
def test_one_raw_server_method_owned_by_two_categories_is_ambiguous(
    tmp_path: Path,
    titles: tuple[str, str],
) -> None:
    shared = "SHARED_SERVER_METHOD_SECRET"
    variants = [schema_variant(shared, params_title=title) for title in titles]
    write_schema_bundle(tmp_path, client_variants=[], server_variants=variants)

    with pytest.raises(CodexSchemaError, match="ambiguous") as caught:
        load_generated_contract(tmp_path, version="fake")

    assert shared not in str(caught.value)


def test_multiple_raw_aliases_inside_one_server_category_remain_valid(tmp_path: Path) -> None:
    variants = [
        schema_variant("execCommandApproval", params_title="ExecCommandApprovalParams"),
        schema_variant(
            "item/commandExecution/requestApproval",
            params_title="CommandExecutionRequestApprovalParams",
        ),
        schema_variant("applyPatchApproval", params_title="ApplyPatchApprovalParams"),
    ]
    write_schema_bundle(tmp_path, client_variants=[], server_variants=variants)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.server_requests[ServerRequestCategory.COMMAND_APPROVAL] == frozenset(
        {"execCommandApproval", "item/commandExecution/requestApproval"}
    )
    assert contract.server_requests[ServerRequestCategory.FILE_CHANGE_APPROVAL] == frozenset(
        {"applyPatchApproval"}
    )


def test_one_raw_server_method_repeated_inside_one_category_stays_valid(tmp_path: Path) -> None:
    variants = [
        schema_variant("execCommandApproval", params_title="ExecCommandApprovalParams"),
        schema_variant(
            "execCommandApproval",
            params_title="CommandExecutionRequestApprovalParams",
        ),
    ]
    write_schema_bundle(tmp_path, client_variants=[], server_variants=variants)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.server_requests[ServerRequestCategory.COMMAND_APPROVAL] == frozenset(
        {"execCommandApproval"}
    )


# One payload fragment a hostile container tries to smuggle into a traceback.
CONTRACT_VALUE_SECRET = "CONTRACT_VALUE_SECRET"  # noqa: S105


class HostileMapping(dict[object, object]):
    """A contract mapping that quotes the payload while it is read."""

    def keys(self) -> Iterator[object]:  # type: ignore[override]
        raise RuntimeError(CONTRACT_VALUE_SECRET)

    def items(self) -> Iterator[tuple[object, object]]:  # type: ignore[override]
        raise RuntimeError(CONTRACT_VALUE_SECRET)

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError(CONTRACT_VALUE_SECRET)


class HostileNames(frozenset[str]):
    """An advertised-name set whose iteration is not the built-in one."""

    __slots__ = ()

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(CONTRACT_VALUE_SECRET)


class BoundedItemsMapping(Mapping[object, object]):
    """A mapping proxy source whose declared size may disagree with its iterators."""

    def __init__(
        self,
        first: tuple[object, object],
        *,
        declared_length: int = 1,
        item_count: int | None = None,
        fail_after: int | None = None,
        duplicate: bool = False,
    ) -> None:
        self._first = first
        self._declared_length = declared_length
        self._item_count = item_count
        self._fail_after = fail_after
        self._duplicate = duplicate
        self.item_reads = 0
        self.key_reads = 0

    def __len__(self) -> int:
        return self._declared_length

    def __iter__(self) -> Iterator[object]:
        index = 0
        while True:
            self.key_reads += 1
            if self._fail_after is not None and self.key_reads > self._fail_after:
                raise RuntimeError(CONTRACT_VALUE_SECRET)
            yield self._entry(index)[0]
            index += 1
            if self._item_count is not None and index >= self._item_count:
                return

    def __getitem__(self, key: object) -> object:
        if key == self._first[0]:
            return self._first[1]
        if type(key) is str and key.startswith("extra-"):
            return self._first[1]
        raise KeyError(key)

    def items(self) -> Iterator[tuple[object, object]]:  # type: ignore[override]
        index = 0
        while True:
            self.item_reads += 1
            if self._fail_after is not None and self.item_reads > self._fail_after:
                raise RuntimeError(CONTRACT_VALUE_SECRET)
            yield self._entry(index)
            index += 1
            if self._item_count is not None and index >= self._item_count:
                return

    def _entry(self, index: int) -> tuple[object, object]:
        if index == 0 or self._duplicate:
            return self._first
        return f"extra-{index}", self._first[1]


class EagerItemsMapping(BoundedItemsMapping):
    """A mapping whose items method materializes every over-yield before returning."""

    def __init__(self, first: tuple[object, object], *, item_count: int) -> None:
        super().__init__(first, item_count=item_count)
        self.materialized_items = 0

    def items(self) -> Iterator[tuple[object, object]]:  # type: ignore[override]
        entries = [self._entry(index) for index in range(self._item_count or 0)]
        self.materialized_items = len(entries)
        return iter(entries)


def _contract_members() -> dict[str, object]:
    return {
        "version": "fake",
        "methods": {SemanticMethod.ACCOUNT_READ: ClientMethodContract("alias", ParamsKind.OBJECT)},
        "server_requests": {ServerRequestCategory.COMMAND_APPROVAL: frozenset({"one"})},
        "unclassified_server_request_count": 0,
        "experimental_schema": True,
        "approval_profiles": {},
    }


def test_mappingproxy_property_snapshot_rejects_over_yield_within_one_extra_read() -> None:
    source = BoundedItemsMapping(
        ("type", _ValueContract(types=frozenset({"string"}))),
        item_count=100,
    )

    snapshot = _contract_properties_snapshot(MappingProxyType(source), _Budget(32))

    assert snapshot is None
    assert source.item_reads + source.key_reads <= 2


def test_mappingproxy_property_snapshot_stops_a_sentinel_infinite_iterator() -> None:
    source = BoundedItemsMapping(
        ("type", _ValueContract(types=frozenset({"string"}))),
        fail_after=2,
    )

    snapshot = _contract_properties_snapshot(MappingProxyType(source), _Budget(32))

    assert snapshot is None
    assert source.item_reads + source.key_reads <= 2


def test_mappingproxy_property_snapshot_does_not_call_eager_items_protocol() -> None:
    source = EagerItemsMapping(
        ("type", _ValueContract(types=frozenset({"string"}))),
        item_count=100,
    )

    snapshot = _contract_properties_snapshot(MappingProxyType(source), _Budget(32))

    assert snapshot is None
    assert source.materialized_items <= 2
    assert source.key_reads <= 2


@pytest.mark.parametrize(
    ("declared_length", "item_count", "duplicate"),
    [(2, 1, False), (1, 2, False), (2, 2, True)],
)
def test_mappingproxy_property_snapshot_requires_exact_unique_exhaustion(
    declared_length: int,
    item_count: int,
    duplicate: bool,
) -> None:
    source = BoundedItemsMapping(
        ("type", _ValueContract(types=frozenset({"string"}))),
        declared_length=declared_length,
        item_count=item_count,
        duplicate=duplicate,
    )

    snapshot = _contract_properties_snapshot(MappingProxyType(source), _Budget(32))

    assert snapshot is None
    assert source.item_reads + source.key_reads <= declared_length + 1


def test_manual_value_contract_rejects_mappingproxy_over_yield_privacy_safely() -> None:
    source = BoundedItemsMapping(
        ("type", _ValueContract(types=frozenset({"string"}))),
        item_count=100,
    )
    nested = _ValueContract(
        types=frozenset({"object"}),
        properties=MappingProxyType(cast("Mapping[str, _ValueContract]", source)),
    )

    with pytest.raises(CodexSchemaError) as failure:
        _freeze_value_contract(nested)

    report = "".join(traceback.format_exception(failure.value))
    assert str(failure.value) == "Codex approval profile is not coherent"
    assert failure.value.__cause__ is None
    assert CONTRACT_VALUE_SECRET not in report
    assert source.item_reads + source.key_reads <= 2


@pytest.mark.parametrize(
    "value",
    [
        {str(index): None for index in range(257)},
        [{"nested": None} for _ in range(257)],
    ],
)
def test_manual_frozen_json_rejects_nested_breadth_before_copy(value: object) -> None:
    with pytest.raises(CodexSchemaError) as failure:
        _freeze_json(value)

    assert str(failure.value) == "Codex approval profile is not coherent"
    assert failure.value.__cause__ is None


def test_contract_mapping_rejects_mappingproxy_over_yield_before_materializing() -> None:
    source = BoundedItemsMapping(
        (SemanticMethod.ACCOUNT_READ, ClientMethodContract("alias", ParamsKind.OBJECT)),
        item_count=100,
    )

    with pytest.raises(CodexSchemaError) as failure:
        CodexProtocolContract(
            version="fake",
            methods=MappingProxyType(cast("Mapping[SemanticMethod, ClientMethodContract]", source)),
            server_requests={},
            unclassified_server_request_count=0,
            experimental_schema=True,
        )

    report = "".join(traceback.format_exception(failure.value))
    assert str(failure.value) == "Codex protocol contract is not coherent"
    assert failure.value.__cause__ is None
    assert CONTRACT_VALUE_SECRET not in report
    assert source.item_reads + source.key_reads <= 2


def test_contract_mapping_stops_a_mappingproxy_infinite_iterator_privacy_safely() -> None:
    source = BoundedItemsMapping(
        (SemanticMethod.ACCOUNT_READ, ClientMethodContract("alias", ParamsKind.OBJECT)),
        fail_after=2,
    )

    with pytest.raises(CodexSchemaError) as failure:
        CodexProtocolContract(
            version="fake",
            methods=MappingProxyType(cast("Mapping[SemanticMethod, ClientMethodContract]", source)),
            server_requests={},
            unclassified_server_request_count=0,
            experimental_schema=True,
        )

    report = "".join(traceback.format_exception(failure.value))
    assert str(failure.value) == "Codex protocol contract is not coherent"
    assert failure.value.__cause__ is None
    assert CONTRACT_VALUE_SECRET not in report
    assert source.item_reads + source.key_reads <= 2


def test_contract_mapping_converts_budget_exhaustion_to_stable_error() -> None:
    with pytest.raises(CodexSchemaError) as failure:
        _contract_mapping({}, budget=_Budget(0))

    report = "".join(traceback.format_exception(failure.value))
    assert str(failure.value) == "Codex protocol contract is not coherent"
    assert failure.value.__cause__ is None
    assert "_BudgetExhaustedError" not in report


def test_valid_mappingproxy_contract_mappings_remain_accepted() -> None:
    contract = CodexProtocolContract(
        version="fake",
        methods=MappingProxyType(
            {SemanticMethod.ACCOUNT_READ: ClientMethodContract("alias", ParamsKind.OBJECT)}
        ),
        server_requests=MappingProxyType(
            {ServerRequestCategory.COMMAND_APPROVAL: frozenset({"one"})}
        ),
        unclassified_server_request_count=0,
        experimental_schema=True,
        approval_profiles=MappingProxyType({}),
    )

    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "alias"
    assert contract.server_requests[ServerRequestCategory.COMMAND_APPROVAL] == frozenset({"one"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"methods": HostileMapping()},
        {"server_requests": HostileMapping()},
        {"approval_profiles": HostileMapping()},
        {"methods": [("a", "b")]},
        {"server_requests": {ServerRequestCategory.COMMAND_APPROVAL: HostileNames({"one"})}},
        {"server_requests": {ServerRequestCategory.COMMAND_APPROVAL: ["one"]}},
        {"server_requests": {ServerRequestCategory.COMMAND_APPROVAL: "one"}},
    ],
)
def test_a_contract_container_that_rewrites_itself_is_rejected(
    overrides: dict[str, object],
) -> None:
    """A public value may be built by a later slice, so no overridden protocol is executed."""
    members = _contract_members()
    members.update(overrides)

    with pytest.raises(CodexSchemaError) as failure:
        CodexProtocolContract(**members)  # type: ignore[arg-type]

    report = "".join(traceback.format_exception(failure.value))
    assert "SECRET" not in report


def test_contract_copies_mappings_and_is_deeply_immutable() -> None:
    methods = {SemanticMethod.ACCOUNT_READ: ClientMethodContract("alias", ParamsKind.OBJECT)}
    server_requests = {ServerRequestCategory.COMMAND_APPROVAL: {"one"}}

    contract = CodexProtocolContract(
        version="fake",
        methods=methods,
        server_requests=cast(
            "dict[ServerRequestCategory, frozenset[str]]",
            server_requests,
        ),
        unclassified_server_request_count=0,
        experimental_schema=True,
    )
    methods.clear()
    server_requests[ServerRequestCategory.COMMAND_APPROVAL].add("two")

    assert isinstance(contract.methods, MappingProxyType)
    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "alias"
    assert contract.server_requests[ServerRequestCategory.COMMAND_APPROVAL] == frozenset({"one"})
    with pytest.raises(TypeError):
        cast("dict[SemanticMethod, ClientMethodContract]", contract.methods)[
            SemanticMethod.CONFIG_READ
        ] = ClientMethodContract("other", ParamsKind.OBJECT)
    with pytest.raises(FrozenInstanceError):
        contract.version = "changed"  # type: ignore[misc]
    with pytest.raises(CodexSchemaError, match="required Codex semantic method is unavailable"):
        contract.require_method(SemanticMethod.CONFIG_READ)


@pytest.mark.parametrize(
    "client_root",
    [
        [],
        {},
        {"oneOf": {}},
    ],
)
def test_invalid_schema_root_has_stable_redacted_error(
    tmp_path: Path,
    client_root: JsonValue,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ClientRequest.json").write_text(json.dumps(client_root), encoding="utf-8")
    (tmp_path / "ServerRequest.json").write_text('{"oneOf": []}', encoding="utf-8")

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "client_payload",
    [
        "{not-json",
        "[" * 2_000 + "null" + "]" * 2_000,
    ],
)
def test_malformed_or_excessively_deep_json_has_stable_error(
    tmp_path: Path,
    client_payload: str,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ClientRequest.json").write_text(client_payload, encoding="utf-8")
    (tmp_path / "ServerRequest.json").write_text('{"oneOf": []}', encoding="utf-8")

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    assert client_payload not in str(caught.value)


# `NaN`, `Infinity` and `-Infinity` are Python extensions its `json` module reads by default,
# while JSON itself defines no non-finite constant. A generated bundle spelling one is
# therefore malformed, wherever the token sits.
NON_FINITE_TOKENS = ["NaN", "Infinity", "-Infinity"]
NON_FINITE_DOCUMENTS = [
    pytest.param('{"oneOf": [], "default": TOKEN}', id="root-member"),
    pytest.param(
        '{"oneOf": [{"type": "object", "properties": {"cwd": {"const": TOKEN}}}]}',
        id="nested-variant-member",
    ),
]


@pytest.mark.parametrize("token", NON_FINITE_TOKENS)
@pytest.mark.parametrize("document", NON_FINITE_DOCUMENTS)
def test_non_finite_json_constant_is_a_stable_redacted_schema_error(
    tmp_path: Path,
    document: str,
    token: str,
) -> None:
    payload = document.replace("TOKEN", token)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ClientRequest.json").write_text(payload, encoding="utf-8")
    (tmp_path / "ServerRequest.json").write_text('{"oneOf": []}', encoding="utf-8")

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert token not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize("token", NON_FINITE_TOKENS)
def test_non_finite_json_constant_in_a_referenced_document_is_a_reference_error(
    tmp_path: Path,
    token: str,
) -> None:
    payload = '{"variant": {"const": TOKEN}}'.replace("TOKEN", token)
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": "Variants.json#/variant"}],
        server_variants=[],
    )
    (tmp_path / "Variants.json").write_text(payload, encoding="utf-8")

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert token not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


INVALID_SCHEMA_MESSAGE = "Codex generated schema is invalid"
INVALID_REFERENCE_MESSAGE = "Codex generated schema reference is invalid"
# Python converts JSON numbers with the interpreter's own conversions, and neither one is
# total: an integer longer than the interpreter's digit limit raises a bare `ValueError`,
# and an exponent past the float range yields an infinity. Both spell a number moco cannot
# represent faithfully, so both belong to the parser boundary rather than to the evaluator.
OVERSIZED_INTEGER = "5" * 5_000
OVERFLOWING_EXPONENTS = ["1e999", "-1e999", "1E999", "1e1000000", "1.7976931348623159e308"]
# An exponent the float conversion reads as a finite signed zero, and whose exact reading is
# still refused: `Decimal` cannot spell an exponent past its own range and raises
# `decimal.InvalidOperation` there. The float bound alone sees nothing wrong with such a
# token, so it belongs to the same parser boundary as an overflowing exponent. The last
# entry spells the same unrepresentable magnitude with a zero coefficient.
UNREPRESENTABLE_EXPONENTS = [
    "1e-9999999999999999999",
    "-1e-9999999999999999999",
    "1e-999999999999999999999999999999",
    "0e999999999999999999999999999",
]
# Ordinary JSON numbers, including an underflowing exponent that still reads as a finite
# zero and an integer exactly at the interpreter's digit limit.
FINITE_NUMBERS = ["0", "12345", "1e2", "-1.5e-3", "1E+308", "1e-999", "-0.0e0", "9" * 4_300]
NUMBER_DOCUMENTS = [
    pytest.param('{"oneOf": [], "default": NUMBER}', id="root-member"),
    pytest.param(
        '{"oneOf": [{"type": "object", "properties": {"cwd": {"const": NUMBER}}}]}',
        id="nested-variant-member",
    ),
]


@pytest.fixture
def default_int_digit_limit() -> Iterator[None]:
    """Pin the digit limit, which the interpreter otherwise reads from its environment."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(sys.int_info.default_max_str_digits)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


def write_raw_bundle(bundle: Path, client_payload: str) -> None:
    """Write one bundle whose root client document is raw text rather than dumped JSON."""
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "ClientRequest.json").write_text(client_payload, encoding="utf-8")
    (bundle / "ServerRequest.json").write_text('{"oneOf": []}', encoding="utf-8")


def write_raw_reference_bundle(bundle: Path, referenced_payload: str) -> None:
    """Write one bundle whose single client variant points at a raw referenced document."""
    write_schema_bundle(
        bundle,
        client_variants=[{"$ref": "Variants.json#/variant"}],
        server_variants=[],
    )
    (bundle / "Variants.json").write_text(referenced_payload, encoding="utf-8")


@pytest.mark.usefixtures("default_int_digit_limit")
@pytest.mark.parametrize("document", NUMBER_DOCUMENTS)
def test_oversized_json_integer_is_a_stable_redacted_schema_error(
    tmp_path: Path,
    document: str,
) -> None:
    payload = document.replace("NUMBER", OVERSIZED_INTEGER)
    write_raw_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_SCHEMA_MESSAGE
    assert caught.value.__cause__ is None
    assert "Exceeds the limit" not in rendered
    assert OVERSIZED_INTEGER not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.usefixtures("default_int_digit_limit")
def test_oversized_json_integer_in_a_referenced_document_is_a_reference_error(
    tmp_path: Path,
) -> None:
    payload = '{"variant": {"const": NUMBER}}'.replace("NUMBER", OVERSIZED_INTEGER)
    write_raw_reference_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_REFERENCE_MESSAGE
    assert caught.value.__cause__ is None
    assert "Exceeds the limit" not in rendered
    assert OVERSIZED_INTEGER not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize("number", OVERFLOWING_EXPONENTS)
@pytest.mark.parametrize("document", NUMBER_DOCUMENTS)
def test_overflowing_json_exponent_is_a_stable_redacted_schema_error(
    tmp_path: Path,
    document: str,
    number: str,
) -> None:
    payload = document.replace("NUMBER", number)
    write_raw_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_SCHEMA_MESSAGE
    assert caught.value.__cause__ is None
    assert number not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize("number", OVERFLOWING_EXPONENTS)
def test_overflowing_json_exponent_in_a_referenced_document_is_a_reference_error(
    tmp_path: Path,
    number: str,
) -> None:
    payload = '{"variant": {"const": NUMBER}}'.replace("NUMBER", number)
    write_raw_reference_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_REFERENCE_MESSAGE
    assert caught.value.__cause__ is None
    assert number not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.parametrize("token", UNREPRESENTABLE_EXPONENTS)
def test_unrepresentable_json_exponent_raises_one_payload_free_parser_error(token: str) -> None:
    with pytest.raises(_MalformedDocumentError) as caught:
        _read_number(token)

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == ""
    assert caught.value.__cause__ is None
    assert token not in rendered
    assert "InvalidOperation" not in rendered
    assert "ConversionSyntax" not in rendered


@pytest.mark.parametrize("number", UNREPRESENTABLE_EXPONENTS)
@pytest.mark.parametrize("document", NUMBER_DOCUMENTS)
def test_unrepresentable_json_exponent_is_a_stable_redacted_schema_error(
    tmp_path: Path,
    document: str,
    number: str,
) -> None:
    payload = document.replace("NUMBER", number)
    write_raw_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_SCHEMA_MESSAGE
    assert caught.value.__cause__ is None
    assert number not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered
    assert "InvalidOperation" not in rendered
    assert "ConversionSyntax" not in rendered


@pytest.mark.parametrize("number", UNREPRESENTABLE_EXPONENTS)
def test_unrepresentable_json_exponent_in_a_referenced_document_is_a_reference_error(
    tmp_path: Path,
    number: str,
) -> None:
    payload = '{"variant": {"const": NUMBER}}'.replace("NUMBER", number)
    write_raw_reference_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_REFERENCE_MESSAGE
    assert caught.value.__cause__ is None
    assert number not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered
    assert "InvalidOperation" not in rendered
    assert "ConversionSyntax" not in rendered


def test_an_underflowing_exponent_still_reads_as_the_exact_number_it_spells() -> None:
    """The neighbouring underflow stays accepted, and exactly, rather than reading as zero."""
    assert _read_number("1e-999") == Decimal("1e-999")
    assert _read_number("1e-999") != 0
    assert _read_number("1e-999999999999999999") == Decimal("1e-999999999999999999")


@pytest.mark.usefixtures("default_int_digit_limit")
@pytest.mark.parametrize("number", FINITE_NUMBERS)
@pytest.mark.parametrize("document", NUMBER_DOCUMENTS)
def test_finite_json_numbers_are_still_parsed(
    tmp_path: Path,
    document: str,
    number: str,
) -> None:
    write_raw_bundle(tmp_path, document.replace("NUMBER", number))

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.methods == {}


@pytest.mark.usefixtures("default_int_digit_limit")
@pytest.mark.parametrize("number", FINITE_NUMBERS)
def test_finite_json_numbers_keep_a_referenced_method_available(
    tmp_path: Path,
    number: str,
) -> None:
    variant = json.dumps(schema_variant("account/read", params_title="GetAccountParams"))
    write_raw_reference_bundle(tmp_path, f'{{"variant": {variant}, "default": {number}}}')

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "account/read"


@pytest.mark.parametrize("reader", [_read_integer, _read_number])
@pytest.mark.parametrize("token", ["not-a-number", "0x10", "NUMBER_TOKEN_SECRET"])
def test_unreadable_number_token_raises_one_payload_free_parser_error(
    reader: Callable[[str], object],
    token: str,
) -> None:
    with pytest.raises(_MalformedDocumentError) as caught:
        reader(token)

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == ""
    assert caught.value.__cause__ is None
    assert token not in rendered


# A binary float cannot hold every JSON number a generated bundle spells: `9007199254740993.0`
# reads back as `9007199254740992.0`, so two spellings of one number would look distinct and
# two distinct numbers would look identical. `json.dumps` rounds the same way, so every raw
# lexeme below is substituted into the document as text and never dumped.
RAW_ENUM_PLACEHOLDER = '"__RAW_ENUM__"'


def write_raw_enum_bundle(bundle: Path, enum_text: str, *, sibling: bool) -> None:
    """Write a ThreadStart bundle whose `ephemeral` enum keeps its raw JSON number lexemes.

    The declaration either constrains `ephemeral` directly, or sits in a `oneOf` beside a
    branch that admits the value moco sends, where only a readable enum proves exactly-one.
    """
    declaration: dict[str, JsonValue] = (
        {"oneOf": [{"enum": [True]}, {"enum": "__RAW_ENUM__"}]}
        if sibling
        else {"enum": "__RAW_ENUM__"}
    )
    variant = json.dumps(thread_start_variant(overrides={"ephemeral": declaration}))
    assert variant.count(RAW_ENUM_PLACEHOLDER) == 1
    write_raw_bundle(bundle, f'{{"oneOf": [{variant.replace(RAW_ENUM_PLACEHOLDER, enum_text)}]}}')


# Each pair spells one mathematical JSON number twice, so the enum repeats a value and is
# malformed however Python happens to represent it.
RAW_DUPLICATE_NUMBER_PAIRS = [
    pytest.param("9007199254740993, 9007199254740993.0", id="integer-then-decimal-above-2-53"),
    pytest.param("9007199254740993.0, 9007199254740993", id="decimal-then-integer-above-2-53"),
    pytest.param("10000000000000001, 1.0000000000000001e16", id="integer-and-exponent-above-2-53"),
    pytest.param("1000, 1e3", id="integer-and-exponent"),
    pytest.param("0, -0.0", id="positive-and-negative-zero"),
    pytest.param("1.5, 15e-1", id="decimal-and-exponent"),
    pytest.param("[9007199254740993], [9007199254740993.0]", id="arrays-repeating-one-number"),
    pytest.param(
        '{"a": 9007199254740993}, {"a": 9007199254740993.0}',
        id="objects-repeating-one-number",
    ),
]
# Distinct JSON numbers a rounded reading would wrongly collapse, beside ordinary ones and
# beside a boolean, which is never a number whatever its Python representation.
RAW_DISTINCT_NUMBER_PAIRS = [
    pytest.param("9007199254740993.0, 9007199254740992.0", id="adjacent-decimals-above-2-53"),
    pytest.param("9007199254740993, 9007199254740992", id="adjacent-integers-above-2-53"),
    pytest.param("1000, 1e4", id="integer-and-larger-exponent"),
    pytest.param("0, 1e-999", id="zero-and-underflowing-exponent"),
    pytest.param("1, 1.5", id="integer-and-decimal"),
    pytest.param("false, 0.0", id="boolean-beside-zero"),
    pytest.param("false, -0.0", id="boolean-beside-negative-zero"),
    pytest.param(
        "[9007199254740993.0], [9007199254740992.0]",
        id="arrays-of-adjacent-decimals",
    ),
    pytest.param(
        '{"a": 9007199254740993.0}, {"a": 9007199254740992.0}',
        id="objects-of-adjacent-decimals",
    ),
]
# Enum lists that contain the fixed value moco sends beside numbers no rounding may merge.
RAW_DISTINCT_ENUM_LISTS_WITH_THE_SENT_VALUE = [
    pytest.param("true, 1.0", id="boolean-beside-number"),
    pytest.param("true, 1e0", id="boolean-beside-exponent"),
    pytest.param(
        "true, 9007199254740993.0, 9007199254740992.0",
        id="boolean-beside-adjacent-decimals",
    ),
    pytest.param("true, 0, 1e-999", id="boolean-beside-zero-and-underflowing-exponent"),
]


@pytest.mark.parametrize("pair", RAW_DUPLICATE_NUMBER_PAIRS)
def test_one_of_sibling_repeating_a_raw_json_number_keeps_the_fixed_value_unavailable(
    tmp_path: Path,
    pair: str,
) -> None:
    """A sibling repeating one number is unreadable, so exactly-one is never proven."""
    write_raw_enum_bundle(tmp_path, f"[{pair}]", sibling=True)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("pair", RAW_DUPLICATE_NUMBER_PAIRS)
def test_enum_repeating_a_raw_json_number_beside_the_sent_value_is_unreadable(
    tmp_path: Path,
    pair: str,
) -> None:
    """A repeated number makes the whole declaration unreadable, listed value or not."""
    write_raw_enum_bundle(tmp_path, f"[true, {pair}]", sibling=False)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods


@pytest.mark.parametrize("pair", RAW_DISTINCT_NUMBER_PAIRS)
def test_one_of_sibling_with_distinct_raw_json_numbers_keeps_its_definite_rejection(
    tmp_path: Path,
    pair: str,
) -> None:
    """Distinct numbers stay readable, so the sibling still rejects the value moco sends."""
    write_raw_enum_bundle(tmp_path, f"[{pair}]", sibling=True)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


@pytest.mark.parametrize("candidates", RAW_DISTINCT_ENUM_LISTS_WITH_THE_SENT_VALUE)
def test_enum_listing_the_sent_value_beside_distinct_raw_numbers_stays_readable(
    tmp_path: Path,
    candidates: str,
) -> None:
    """No listed number equals another one, so the enum still admits the value moco sends."""
    write_raw_enum_bundle(tmp_path, f"[{candidates}]", sibling=False)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.THREAD_START).name == "alias-thread-start"


def test_json_numbers_keep_their_exact_value_through_the_parser() -> None:
    """The parser reads the number the document spells, not the nearest binary float."""
    assert _read_number("9007199254740993.0") == 9007199254740993
    assert _read_number("9007199254740993.0") != 9007199254740992
    assert _read_number("1.0000000000000001e16") == 10000000000000001
    assert _read_number("1e3") == 1000
    assert _read_number("1.50") == _read_number("15e-1")
    assert _read_number("-0.0") == 0
    assert _read_number("1e-999") != 0


def test_a_contract_read_from_exact_numbers_carries_no_parsed_number(tmp_path: Path) -> None:
    """The exact representation stays inside schema reading and never reaches the contract."""
    write_raw_enum_bundle(tmp_path, "[true, 9007199254740993.0]", sibling=False)

    contract = load_generated_contract(tmp_path, version="fake")

    method = contract.require_method(SemanticMethod.THREAD_START)
    assert type(contract.version) is str
    assert type(method.name) is str
    assert method.params_kind is ParamsKind.OBJECT
    assert {type(field) for field in method.semantic_fields} == {str}
    assert type(contract.unclassified_server_request_count) is int


DUPLICATE_MEMBER_SECRET = "DUPLICATE_MEMBER_SECRET"  # noqa: S105
# Python keeps the last of two members sharing one name, so at this trust boundary a
# duplicate would let parser overwrite order decide how the contract reads.
DUPLICATE_MEMBER_DOCUMENTS = [
    pytest.param('{"oneOf": [], "SECRET": 1, "SECRET": 2}', id="root-member"),
    pytest.param('{"oneOf": [{"type": "object", "SECRET": 1, "SECRET": 2}]}', id="variant-member"),
    pytest.param(
        '{"oneOf": [{"properties": {"method": {"SECRET": 1, "SECRET": 2}}}]}',
        id="deeply-nested-member",
    ),
    pytest.param('{"oneOf": [{"SECRET": [{"SECRET": 1, "SECRET": 2}]}]}', id="member-inside-array"),
]


@pytest.mark.parametrize("document", DUPLICATE_MEMBER_DOCUMENTS)
def test_duplicate_json_member_is_a_stable_redacted_schema_error(
    tmp_path: Path,
    document: str,
) -> None:
    payload = document.replace("SECRET", DUPLICATE_MEMBER_SECRET)
    write_raw_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_SCHEMA_MESSAGE
    assert caught.value.__cause__ is None
    assert DUPLICATE_MEMBER_SECRET not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


DUPLICATE_MEMBER_REFERENCE_DOCUMENTS = [
    pytest.param('{"variant": {}, "SECRET": 1, "SECRET": 2}', id="referenced-root-member"),
    pytest.param('{"variant": {"SECRET": 1, "SECRET": 2}}', id="referenced-target-member"),
    pytest.param(
        '{"variant": {"properties": {"method": {"SECRET": 1, "SECRET": 2}}}}',
        id="referenced-deeply-nested-member",
    ),
]


@pytest.mark.parametrize("document", DUPLICATE_MEMBER_REFERENCE_DOCUMENTS)
def test_duplicate_json_member_in_a_referenced_document_is_a_reference_error(
    tmp_path: Path,
    document: str,
) -> None:
    payload = document.replace("SECRET", DUPLICATE_MEMBER_SECRET)
    write_raw_reference_bundle(tmp_path, payload)

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == INVALID_REFERENCE_MESSAGE
    assert caught.value.__cause__ is None
    assert DUPLICATE_MEMBER_SECRET not in rendered
    assert payload not in rendered
    assert str(tmp_path) not in rendered


def test_duplicate_one_of_member_is_rejected_instead_of_overwritten(tmp_path: Path) -> None:
    variant = json.dumps(schema_variant("account/read", params_title="GetAccountParams"))
    # Last-member-wins parsing would read the second list and report the method available.
    write_raw_bundle(tmp_path, f'{{"oneOf": [], "oneOf": [{variant}]}}')

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    assert str(caught.value) == INVALID_SCHEMA_MESSAGE


def test_unique_members_in_any_order_describe_one_contract(tmp_path: Path) -> None:
    variant = json.dumps(schema_variant("account/read", params_title="GetAccountParams"))
    ordered = tmp_path / "ordered"
    reordered = tmp_path / "reordered"
    write_raw_bundle(ordered, f'{{"oneOf": [{variant}], "title": "ClientRequest"}}')
    write_raw_bundle(reordered, f'{{"title": "ClientRequest", "oneOf": [{variant}]}}')

    first = load_generated_contract(ordered, version="fake")
    second = load_generated_contract(reordered, version="fake")

    assert first.require_method(SemanticMethod.ACCOUNT_READ).name == "account/read"
    assert dict(first.methods) == dict(second.methods)
    assert dict(first.server_requests) == dict(second.server_requests)


def test_cross_file_local_refs_and_pointer_escapes_are_supported(tmp_path: Path) -> None:
    client: dict[str, JsonValue] = {"oneOf": [{"$ref": "Variants.json#/defs/a~1b~0c"}]}
    variant = schema_variant("ref-alias", params_schema={"$ref": "#/defs/GetAccountParams"})
    variants: dict[str, JsonValue] = {
        "defs": {
            "a/b~c": variant,
            "GetAccountParams": {"title": "GetAccountParams", "type": "object"},
        }
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ClientRequest.json").write_text(json.dumps(client), encoding="utf-8")
    (tmp_path / "ServerRequest.json").write_text('{"oneOf": []}', encoding="utf-8")
    (tmp_path / "Variants.json").write_text(json.dumps(variants), encoding="utf-8")

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "ref-alias"


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.json#/value",
        "/absolute.json#/value",
        "file:///tmp/schema.json#/value",
        "https://example.invalid/schema.json#/value",
        "urn:example:schema",
        "#/bad~2token",
        "#/missing",
    ],
)
def test_unsafe_or_malformed_refs_are_rejected_without_echo(
    tmp_path: Path,
    reference: str,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[cast("dict[str, JsonValue]", {"$ref": reference})],
        server_variants=[],
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    assert reference not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_missing_ref_has_no_exception_chain_or_secret_traceback(tmp_path: Path) -> None:
    secret_reference = "SCHEMA_REFERENCE_SECRET.json#/value"  # noqa: S105
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": secret_reference}],
        server_variants=[],
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert secret_reference not in rendered
    assert str(tmp_path) not in rendered


def test_malformed_url_ref_has_no_raw_value_error_or_payload_traceback(tmp_path: Path) -> None:
    malformed_reference = "http://[SCHEMA_URL_SECRET"
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": malformed_reference}],
        server_variants=[],
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert malformed_reference not in rendered


def test_oversized_list_pointer_index_has_no_raw_value_error_or_payload_traceback(
    tmp_path: Path,
) -> None:
    oversized_token = "9" * 5_000
    reference = f"Lists.json#/items/{oversized_token}"
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": reference}],
        server_variants=[],
    )
    (tmp_path / "Lists.json").write_text(
        json.dumps({"items": [schema_variant("alias", params_title="GetAccountParams")]}),
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid") as caught:
        load_generated_contract(tmp_path, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert oversized_token not in rendered


def test_missing_bundle_has_no_exception_chain_or_secret_traceback(tmp_path: Path) -> None:
    missing_bundle = tmp_path / "SCHEMA_BUNDLE_SECRET"

    with pytest.raises(CodexSchemaError, match="generated schema is invalid") as caught:
        load_generated_contract(missing_bundle, version="fake")

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert str(missing_bundle) not in rendered


def test_reference_cycles_are_rejected(tmp_path: Path) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": "Cycle.json#/one"}],
        server_variants=[],
    )
    (tmp_path / "Cycle.json").write_text(
        json.dumps({"one": {"$ref": "#/two"}, "two": {"$ref": "#/one"}}),
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text(
        json.dumps({"variant": schema_variant("x", params_title="GetAccountParams")}),
        encoding="utf-8",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "escape.json").symlink_to(outside)
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": "escape.json#/variant"}],
        server_variants=[],
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


def test_oversized_referenced_schema_is_rejected_as_an_invalid_reference(
    tmp_path: Path,
) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[{"$ref": "Oversized.json#/variant"}],
        server_variants=[],
    )
    (tmp_path / "Oversized.json").write_text(
        '{"variant":{},"padding":"' + ("x" * 2_000_000) + '"}',
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="schema reference is invalid"):
        load_generated_contract(tmp_path, version="fake")


def fake_command(script: Path, scenario: str = "default") -> CodexCommand:
    return CodexCommand((sys.executable, str(script), f"--scenario={scenario}"))


@pytest.mark.integration
@pytest.mark.parametrize("use_async", [False, True])
def test_probe_generates_experimental_contract_and_cleans_tempdir(
    monkeypatch: pytest.MonkeyPatch,
    use_async: bool,
) -> None:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"
    real_factory = tempfile.TemporaryDirectory
    observed: list[Path] = []

    def temporary_directory() -> tempfile.TemporaryDirectory[str]:
        instance = real_factory()
        observed.append(Path(instance.name))
        return instance

    monkeypatch.setattr(tempfile, "TemporaryDirectory", temporary_directory)
    probe = CodexSchemaProbe(fake_command(script))

    contract = asyncio.run(probe.probe()) if use_async else probe.probe_sync()

    assert contract.version == "fake-codex 99.1-test"
    assert contract.experimental_schema is True
    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "fake/account/attempt-1"
    assert observed
    assert not observed[0].exists()


@pytest.mark.integration
def test_probe_retries_stable_exactly_once_when_experimental_is_unsupported() -> None:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"

    contract = CodexSchemaProbe(fake_command(script, "schema-stable-only")).probe_sync()

    assert contract.experimental_schema is False
    assert contract.require_method(SemanticMethod.ACCOUNT_READ).name == "fake/account/attempt-2"


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["schema-other-failure", "schema-both-fail"])
def test_probe_does_not_leak_stderr_or_retry_unrelated_failures(scenario: str) -> None:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"

    with pytest.raises(CodexSchemaError, match="Codex schema probe failed") as caught:
        CodexSchemaProbe(fake_command(script, scenario)).probe_sync()

    message = str(caught.value)
    assert "SCHEMA_STDERR_SECRET" not in message
    assert str(script) not in message


@pytest.mark.integration
def test_probe_rejects_oversized_subprocess_output_without_retaining_it() -> None:
    script = "import sys; sys.stdout.write('x' * 2_000_000)"

    with pytest.raises(CodexSchemaError, match="probe failed"):
        CodexSchemaProbe._run(  # noqa: SLF001
            (sys.executable, "-c", script),
            failure_message="Codex schema probe failed",
        )


@pytest.mark.integration
def test_probe_does_not_wait_for_a_descendant_holding_inherited_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("moco.codex.schema._SUBPROCESS_TIMEOUT_SECONDS", 0.1)
    descendant = "import time; time.sleep(2)"
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}], "
        "stdout=sys.stdout, stderr=sys.stderr)"
    )

    started = time.monotonic()
    result = CodexSchemaProbe._run(  # noqa: SLF001
        (sys.executable, "-c", parent),
        failure_message="Codex schema probe failed",
    )

    assert result.returncode == 0
    assert time.monotonic() - started < 1.0


def test_generated_schema_document_size_is_bounded_before_json_parse(tmp_path: Path) -> None:
    write_schema_bundle(tmp_path, client_variants=[], server_variants=[])
    (tmp_path / "ClientRequest.json").write_text(
        '{"oneOf":[],"padding":"' + ("x" * 2_000_000) + '"}',
        encoding="utf-8",
    )

    with pytest.raises(CodexSchemaError, match="generated schema is invalid"):
        load_generated_contract(tmp_path, version="fake")


@pytest.mark.integration
def test_probe_redacts_non_utf8_subprocess_output_without_retry() -> None:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"

    with pytest.raises(CodexSchemaError, match="Codex schema probe failed") as caught:
        CodexSchemaProbe(fake_command(script, "schema-non-utf8")).probe_sync()

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert "UnicodeDecodeError" not in rendered


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["version-failure", "version-empty"])
def test_probe_rejects_failed_or_empty_version_without_leaking_details(scenario: str) -> None:
    script = Path(__file__).parent / "fixtures" / "fake_codex.py"

    with pytest.raises(CodexSchemaError, match="Codex version probe failed") as caught:
        CodexSchemaProbe(fake_command(script, scenario)).probe_sync()

    assert "VERSION_STDERR_SECRET" not in str(caught.value)


@pytest.mark.integration
def test_probe_reports_missing_version_command_as_a_redacted_version_failure() -> None:
    command = CodexCommand(("/CODEX_ARGV_SECRET/not/a/real/codex",))

    with pytest.raises(CodexSchemaError, match="Codex version probe failed") as caught:
        CodexSchemaProbe(command).probe_sync()

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert command.argv[0] not in rendered


# One approval family as a generated bundle spells it: the params document names the family,
# and a response document beside it proves which decisions may be sent.
COMMAND_APPROVAL_TITLE = "CommandExecutionRequestApprovalParams"
FILE_APPROVAL_TITLE = "FileChangeRequestApprovalParams"
COMMAND_APPROVAL_RESPONSE = "CommandExecutionRequestApprovalResponse.json"
FILE_APPROVAL_RESPONSE = "FileChangeRequestApprovalResponse.json"
COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
FILE_APPROVAL_METHOD = "item/fileChange/requestApproval"
LEGACY_COMMAND_METHOD = "execCommandApproval"


def decision_string(value: str) -> dict[str, JsonValue]:
    return {"type": "string", "enum": [value]}


def decision_object(name: str, member: str) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "required": [name],
        "properties": {
            name: {
                "type": "object",
                "required": [member],
                "properties": {member: {"type": "array"}},
            }
        },
    }


def command_decision_schema(
    *,
    variants: list[dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    listed: list[JsonValue] = list(
        variants
        if variants is not None
        else [
            decision_string("accept"),
            decision_string("acceptForSession"),
            decision_object("acceptWithExecpolicyAmendment", "execpolicy_amendment"),
            decision_object("applyNetworkPolicyAmendment", "network_policy_amendment"),
            decision_string("decline"),
            decision_string("cancel"),
        ]
    )
    return {"oneOf": listed}


def file_decision_schema() -> dict[str, JsonValue]:
    return {
        "oneOf": [
            decision_string("accept"),
            decision_string("acceptForSession"),
            decision_string("decline"),
            decision_string("cancel"),
        ]
    }


def decision_response_document(decision: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "required": ["decision"],
        "properties": {"decision": decision},
    }


def command_approval_properties(
    *,
    offer: bool = True,
    overrides: dict[str, JsonValue] | None = None,
    removed: frozenset[str] = frozenset(),
) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {
        "threadId": {"type": "string"},
        "turnId": {"type": "string"},
        "itemId": {"type": "string"},
        "startedAtMs": {"type": "integer"},
        "command": {"type": ["string", "null"]},
        "cwd": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"]},
        "approvalId": {"type": ["string", "null"]},
        "commandActions": {"type": ["array", "null"]},
        "environmentId": {"type": ["string", "null"]},
        "networkApprovalContext": {"type": ["object", "null"]},
        "proposedExecpolicyAmendment": {"type": ["array", "null"]},
        "proposedNetworkPolicyAmendments": {"type": ["array", "null"]},
        "additionalPermissions": {"type": ["object", "null"]},
    }
    if offer:
        properties["availableDecisions"] = {
            "type": ["array", "null"],
            "items": command_decision_schema(),
        }
    properties.update(overrides or {})
    for name in removed:
        properties.pop(name, None)
    return properties


def file_approval_properties(
    *,
    overrides: dict[str, JsonValue] | None = None,
    removed: frozenset[str] = frozenset(),
) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {
        "threadId": {"type": "string"},
        "turnId": {"type": "string"},
        "itemId": {"type": "string"},
        "startedAtMs": {"type": "integer"},
        "reason": {"type": ["string", "null"]},
        "grantRoot": {"type": ["string", "null"]},
    }
    properties.update(overrides or {})
    for name in removed:
        properties.pop(name, None)
    return properties


def approval_bundle(
    bundle: Path,
    *,
    command_properties: dict[str, JsonValue] | None = None,
    command_required: frozenset[str] = frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
    command_decision: dict[str, JsonValue] | None = None,
    file_properties: dict[str, JsonValue] | None = None,
    documents: dict[str, JsonValue] | None = None,
    extra_variants: list[dict[str, JsonValue]] | None = None,
) -> None:
    server_variants = [
        schema_variant(
            COMMAND_APPROVAL_METHOD,
            params_title=COMMAND_APPROVAL_TITLE,
            params_required=command_required,
            params_properties=command_properties
            if command_properties is not None
            else command_approval_properties(),
        ),
        schema_variant(
            FILE_APPROVAL_METHOD,
            params_title=FILE_APPROVAL_TITLE,
            params_required=frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
            params_properties=file_properties
            if file_properties is not None
            else file_approval_properties(),
        ),
        *(extra_variants or []),
    ]
    written: dict[str, JsonValue] = {
        COMMAND_APPROVAL_RESPONSE: decision_response_document(
            command_decision if command_decision is not None else command_decision_schema()
        ),
        FILE_APPROVAL_RESPONSE: decision_response_document(file_decision_schema()),
    }
    written.update(documents or {})
    write_schema_bundle(
        bundle,
        client_variants=[],
        server_variants=server_variants,
        documents=written,
    )


LEGACY_FILE_METHOD = "applyPatchApproval"
LEGACY_COMMAND_RESPONSE = "ExecCommandApprovalResponse.json"
LEGACY_FILE_RESPONSE = "ApplyPatchApprovalResponse.json"
# What each family states about the request it is asking about, as its own schema spells it.
_CORRELATION_MEMBERS = {
    ApprovalCorrelation.THREAD_ITEM: frozenset({"threadId", "turnId", "itemId"}),
    ApprovalCorrelation.CONVERSATION_CALL: frozenset({"conversationId", "callId"}),
}


def parsed_command_schema() -> dict[str, JsonValue]:
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["cmd", "type"],
                "properties": {
                    "cmd": {"type": "string"},
                    "type": {"type": "string", "enum": ["unknown"]},
                },
            }
        ]
    }


def file_change_schema(
    *,
    destination: bool = True,
    update_additional: JsonValue | None = None,
) -> dict[str, JsonValue]:
    """The changed-file type every retained legacy bundle spells, move destination included."""
    update: dict[str, JsonValue] = {
        "type": {"type": "string", "enum": ["update"]},
        "unified_diff": {"type": "string"},
    }
    if destination:
        update["move_path"] = {"type": ["string", "null"]}
    update_variant: dict[str, JsonValue] = {
        "type": "object",
        "required": ["type", "unified_diff"],
        "properties": update,
    }
    if update_additional is not None:
        update_variant["additionalProperties"] = update_additional
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["content", "type"],
                "properties": {
                    "content": {"type": "string"},
                    "type": {"type": "string", "enum": ["add"]},
                },
            },
            {
                "type": "object",
                "required": ["content", "type"],
                "properties": {
                    "content": {"type": "string"},
                    "type": {"type": "string", "enum": ["delete"]},
                },
            },
            update_variant,
        ]
    }


def legacy_decision_schema(
    *,
    denied_object: bool = False,
    rejection: dict[str, JsonValue] | None = None,
    mcp_policy_amendment: bool = False,
) -> dict[str, JsonValue]:
    """The legacy vocabulary, whose refusal changed shape between retained builds."""
    refusal: dict[str, JsonValue] = (
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["denied"],
            "properties": {
                "denied": {
                    "type": "object",
                    "required": ["rejection"],
                    "properties": {"rejection": rejection or {"type": "string"}},
                }
            },
        }
        if denied_object
        else decision_string("denied")
    )
    return {
        "oneOf": [
            decision_string("approved"),
            decision_object("approved_execpolicy_amendment", "proposed_execpolicy_amendment"),
            decision_string("approved_for_session"),
            *([decision_string("approved_mcp_policy_amendment")] if mcp_policy_amendment else []),
            decision_object("network_policy_amendment", "network_policy_amendment"),
            refusal,
            decision_string("timed_out"),
            decision_string("abort"),
        ]
    }


def legacy_approval_bundle(
    bundle: Path,
    *,
    denied_object: bool = False,
    rejection: dict[str, JsonValue] | None = None,
    mcp_policy_amendment: bool = False,
    command_properties: dict[str, JsonValue] | None = None,
    command_required: frozenset[str] = frozenset(
        {"callId", "command", "conversationId", "cwd", "parsedCmd"}
    ),
    file_change_destination: bool = True,
    file_change_additional: JsonValue | None = None,
) -> None:
    """A bundle advertising the legacy aliases beside the newer ones, as every build does."""
    legacy_command = schema_variant(
        LEGACY_COMMAND_METHOD,
        params_title="ExecCommandApprovalParams",
        params_required=command_required,
        params_properties=command_properties
        if command_properties is not None
        else {
            "approvalId": {"type": ["string", "null"]},
            "callId": {"type": "string"},
            "command": {"type": "array", "items": {"type": "string"}},
            "conversationId": {"type": "string"},
            "cwd": {"type": "string"},
            "parsedCmd": {"type": "array", "items": parsed_command_schema()},
            "reason": {"type": ["string", "null"]},
        },
    )
    legacy_file = schema_variant(
        LEGACY_FILE_METHOD,
        params_title="ApplyPatchApprovalParams",
        params_required=frozenset({"callId", "conversationId", "fileChanges"}),
        params_properties={
            "callId": {"type": "string"},
            "conversationId": {"type": "string"},
            "fileChanges": {
                "type": "object",
                "additionalProperties": file_change_schema(
                    destination=file_change_destination,
                    update_additional=file_change_additional,
                ),
            },
            "grantRoot": {"type": ["string", "null"]},
            "reason": {"type": ["string", "null"]},
        },
    )
    decision = legacy_decision_schema(
        denied_object=denied_object,
        rejection=rejection,
        mcp_policy_amendment=mcp_policy_amendment,
    )
    approval_bundle(
        bundle,
        extra_variants=[legacy_command, legacy_file],
        documents={
            LEGACY_COMMAND_RESPONSE: decision_response_document(decision),
            LEGACY_FILE_RESPONSE: decision_response_document(decision),
        },
    )


def test_approval_profiles_are_read_from_the_generated_request_and_response(
    tmp_path: Path,
) -> None:
    approval_bundle(tmp_path)

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.approval_profile(COMMAND_APPROVAL_METHOD)
    assert profile is not None
    assert profile.category is ServerRequestCategory.COMMAND_APPROVAL
    assert profile.correlation is ApprovalCorrelation.THREAD_ITEM
    assert profile.required_members == frozenset({"threadId", "turnId", "itemId", "startedAtMs"})
    assert profile.declared_members == frozenset(command_approval_properties())
    assert profile.offer_member == "availableDecisions"
    assert (profile.argv_member, profile.changes_member) == (None, None)
    assert dict(profile.decisions) == {
        ApprovalDecision.ACCEPT: "accept",
        ApprovalDecision.DECLINE: "decline",
        ApprovalDecision.CANCEL: "cancel",
    }
    # The offered vocabulary is the same document's, so a continuing decision is recognised
    # whole and never presented as a one-shot button.
    assert profile.admits_decision("acceptForSession")
    assert profile.semantic_decision("acceptForSession") is None
    assert profile.admits_decision(
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow"]}}
    )
    assert not profile.admits_decision(
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": "allow"}}
    )
    assert contract.adaptable_approval_categories == STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES


def test_a_profile_checks_every_declared_member_against_its_own_schema(tmp_path: Path) -> None:
    """A member moco only displays or drops is compiled and checked like any other."""
    approval_bundle(
        tmp_path,
        command_properties=command_approval_properties(
            overrides={
                "commandActions": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "required": ["command", "type"],
                        "properties": {
                            "command": {"type": "string"},
                            "type": {"type": "string", "enum": ["unknown"]},
                        },
                    },
                }
            }
        ),
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.approval_profile(COMMAND_APPROVAL_METHOD)
    assert profile is not None
    assert profile.declared_members == frozenset(profile.member_contracts)
    assert profile.admits_member("commandActions", None)
    assert profile.admits_member("commandActions", [{"type": "unknown", "command": "git"}])
    assert not profile.admits_member("commandActions", "unknown")
    assert not profile.admits_member("commandActions", [{"type": "unknown"}])
    assert not profile.admits_member("approvalId", 7)
    assert not profile.admits_member("threadId", None)
    assert not profile.admits_member("UNDECLARED_MEMBER_SECRET", "value")


def test_a_generated_int64_member_bounds_what_a_build_may_send(tmp_path: Path) -> None:
    approval_bundle(
        tmp_path,
        command_properties=command_approval_properties(
            overrides={"startedAtMs": {"type": "integer", "format": "int64"}}
        ),
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.approval_profile(COMMAND_APPROVAL_METHOD)
    assert profile is not None
    assert profile.admits_member("startedAtMs", 2**63 - 1)
    assert profile.admits_member("startedAtMs", -(2**63))
    assert not profile.admits_member("startedAtMs", 2**63)
    assert not profile.admits_member("startedAtMs", -(2**63) - 1)
    assert not profile.admits_member("startedAtMs", value=True)


def test_a_generated_integer_bound_is_honoured(tmp_path: Path) -> None:
    approval_bundle(
        tmp_path,
        command_properties=command_approval_properties(
            overrides={"startedAtMs": {"type": "integer", "minimum": 1, "maximum": 9}}
        ),
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.approval_profile(COMMAND_APPROVAL_METHOD)
    assert profile is not None
    assert profile.admits_member("startedAtMs", 1)
    assert not profile.admits_member("startedAtMs", 0)
    assert not profile.admits_member("startedAtMs", 10)


@pytest.mark.parametrize(
    "member",
    [
        {"type": "string", "pattern": "^item-"},
        {"type": "array", "items": [{"type": "string"}]},
        {"type": "string", "enum": ["a", "a"]},
        {"type": "string", "enum": []},
        {"type": "unknownJsonType"},
        {"type": "integer", "minimum": "1"},
        {"type": "array", "maxItems": -1},
        {"type": "object", "required": ["a", "a"]},
        {"type": "object", "properties": {"a": "not-a-schema"}},
        {"type": "integer", "exclusiveMinimum": 0},
        {"anyOf": []},
    ],
)
def test_a_member_assertion_this_reader_cannot_state_leaves_the_family_unadaptable(
    tmp_path: Path,
    member: dict[str, JsonValue],
) -> None:
    approval_bundle(
        tmp_path,
        command_properties=command_approval_properties(overrides={"commandActions": member}),
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None
    assert ServerRequestCategory.COMMAND_APPROVAL in contract.server_request_categories


def test_a_response_variant_contradicting_itself_leaves_the_family_unadaptable(
    tmp_path: Path,
) -> None:
    """A named decision another assertion refuses is not a decision moco can send."""
    contradictory = command_decision_schema(
        variants=[
            {"type": "string", "enum": ["accept"], "const": "acceptForSession"},
            decision_string("decline"),
            decision_string("cancel"),
        ]
    )
    approval_bundle(tmp_path, command_decision=contradictory)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None


def test_a_response_value_two_variants_admit_leaves_the_family_unadaptable(
    tmp_path: Path,
) -> None:
    """A `oneOf` admitting one value twice does not prove which variant moco is sending."""
    ambiguous = command_decision_schema(
        variants=[
            decision_string("accept"),
            {"type": "string"},
            decision_string("decline"),
            decision_string("cancel"),
        ]
    )
    approval_bundle(tmp_path, command_decision=ambiguous)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None


def test_a_build_without_the_optional_members_reports_the_narrower_profile(
    tmp_path: Path,
) -> None:
    """The retained Windows observation declares neither started-at nor a decision offer."""
    approval_bundle(
        tmp_path,
        command_properties=command_approval_properties(
            offer=False,
            removed=frozenset({"startedAtMs"}),
        ),
        command_required=frozenset({"threadId", "turnId", "itemId"}),
        file_properties=file_approval_properties(removed=frozenset({"startedAtMs"})),
    )

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.approval_profile(COMMAND_APPROVAL_METHOD)
    assert profile is not None
    assert profile.required_members == frozenset({"threadId", "turnId", "itemId"})
    assert profile.offer_member is None
    assert "startedAtMs" not in profile.declared_members


@pytest.mark.parametrize(
    "decision",
    [
        # One decision moco must be able to send is missing.
        command_decision_schema(
            variants=[
                decision_string("acceptForSession"),
                decision_string("decline"),
                decision_string("cancel"),
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
            ]
        ),
        # A build that renames a decision is a build moco cannot answer.
        command_decision_schema(
            variants=[
                decision_string("approved"),
                decision_string("declined"),
                decision_string("cancel"),
            ]
        ),
        # An unknown continuing decision, in either spelling.
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                decision_string("approveEverythingForever"),
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                decision_object("grantEverything", "scope"),
            ]
        ),
        # An object decision whose own shape cannot be read.
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {
                    "type": "object",
                    "required": ["acceptWithExecpolicyAmendment"],
                    "properties": {"acceptWithExecpolicyAmendment": {"type": "object"}},
                },
            ]
        ),
        # Two variants spelling the same value make the vocabulary ambiguous.
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
            ]
        ),
        # A decision type moco cannot enumerate at all.
        {"type": "string"},
        # Variants whose own declaration cannot be read as a decision at all.
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {"type": "string", "enum": [7]},
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {"type": "integer", "enum": ["acceptForSession"]},
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {"type": "array"},
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {
                    "type": "object",
                    "required": ["acceptForSession", "acceptWithExecpolicyAmendment"],
                    "properties": {
                        "acceptForSession": {"type": "object"},
                        "acceptWithExecpolicyAmendment": {"type": "object"},
                    },
                },
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {
                    "type": "object",
                    "required": ["acceptWithExecpolicyAmendment"],
                    "properties": {"acceptWithExecpolicyAmendment": {"type": "string"}},
                },
            ]
        ),
        command_decision_schema(
            variants=[
                decision_string("accept"),
                decision_string("decline"),
                decision_string("cancel"),
                {
                    "type": "object",
                    "required": ["acceptWithExecpolicyAmendment"],
                    "properties": {
                        "acceptWithExecpolicyAmendment": {
                            "type": "object",
                            "required": ["execpolicy_amendment"],
                            "properties": {
                                "execpolicy_amendment": {"type": "array"},
                                "note": {"type": "string"},
                            },
                        }
                    },
                },
            ]
        ),
    ],
)
def test_an_unreadable_response_decision_leaves_the_family_unadaptable(
    tmp_path: Path,
    decision: dict[str, JsonValue],
) -> None:
    approval_bundle(tmp_path, command_decision=decision)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None
    assert ServerRequestCategory.COMMAND_APPROVAL in contract.server_request_categories
    assert contract.adaptable_approval_categories == frozenset(
        {ServerRequestCategory.FILE_CHANGE_APPROVAL}
    )


@pytest.mark.parametrize(
    "response",
    [
        {"type": "object", "required": ["decision"], "properties": {}},
        {
            "type": "object",
            "required": ["decision", "note"],
            "properties": {"decision": command_decision_schema(), "note": {"type": "string"}},
        },
        {
            "type": "object",
            "required": [],
            "properties": {"decision": command_decision_schema()},
        },
        {"type": "array"},
    ],
)
def test_an_unreadable_response_document_leaves_the_family_unadaptable(
    tmp_path: Path,
    response: dict[str, JsonValue],
) -> None:
    approval_bundle(tmp_path, documents={COMMAND_APPROVAL_RESPONSE: response})

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None


def test_a_missing_response_document_leaves_the_family_unadaptable(tmp_path: Path) -> None:
    approval_bundle(tmp_path)
    (tmp_path / COMMAND_APPROVAL_RESPONSE).unlink()

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None
    assert contract.approval_profile(FILE_APPROVAL_METHOD) is not None


@pytest.mark.parametrize(
    ("properties", "required"),
    [
        # A member no meaning in this family explains.
        (
            command_approval_properties(overrides={"unknownFutureMember": {"type": "string"}}),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        # A required member moco must refuse for widening the reviewed scope.
        (
            command_approval_properties(),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs", "environmentId"}),
        ),
        # A required member the params never declares.
        (
            command_approval_properties(),
            frozenset({"threadId", "turnId", "itemId", "unknownFutureMember"}),
        ),
        # Correlation that a build may leave out or send as something else.
        (
            command_approval_properties(overrides={"threadId": {"type": ["string", "null"]}}),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        (
            command_approval_properties(),
            frozenset({"turnId", "itemId", "startedAtMs"}),
        ),
        # Displayed text moco could not render.
        (
            command_approval_properties(overrides={"command": {"type": ["array", "null"]}}),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        (
            command_approval_properties(removed=frozenset({"cwd"})),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        # Metadata whose declared type is not the one moco reads it as.
        (
            command_approval_properties(overrides={"startedAtMs": {"type": "string"}}),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        # A widening member a build stops allowing to be absent.
        (
            command_approval_properties(overrides={"environmentId": {"type": "string"}}),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        # A decision offer whose vocabulary is not the answerable one.
        (
            command_approval_properties(
                overrides={
                    "availableDecisions": {
                        "type": ["array", "null"],
                        "items": file_decision_schema(),
                    }
                }
            ),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
        (
            command_approval_properties(
                overrides={"availableDecisions": {"type": ["array", "null"]}}
            ),
            frozenset({"threadId", "turnId", "itemId", "startedAtMs"}),
        ),
    ],
)
def test_an_unreadable_request_member_leaves_the_family_unadaptable(
    tmp_path: Path,
    properties: dict[str, JsonValue],
    required: frozenset[str],
) -> None:
    approval_bundle(tmp_path, command_properties=properties, command_required=required)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None
    assert ServerRequestCategory.COMMAND_APPROVAL in contract.server_request_categories


def test_params_that_are_not_an_object_leave_the_family_unadaptable(tmp_path: Path) -> None:
    write_schema_bundle(
        tmp_path,
        client_variants=[],
        server_variants=[
            schema_variant(
                COMMAND_APPROVAL_METHOD,
                params_schema={"type": "string", "title": COMMAND_APPROVAL_TITLE},
            )
        ],
        documents={
            COMMAND_APPROVAL_RESPONSE: decision_response_document(command_decision_schema())
        },
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is None
    assert ServerRequestCategory.COMMAND_APPROVAL in contract.server_request_categories


def test_a_legacy_family_is_profiled_from_its_own_params_and_response(tmp_path: Path) -> None:
    """Every advertised alias must be readable, so the legacy families are profiled too."""
    legacy_approval_bundle(tmp_path)

    contract = load_generated_contract(tmp_path, version="fake")

    assert LEGACY_COMMAND_METHOD in contract.server_requests[ServerRequestCategory.COMMAND_APPROVAL]
    profile = contract.approval_profile(LEGACY_COMMAND_METHOD)
    assert profile is not None
    assert profile.correlation is ApprovalCorrelation.CONVERSATION_CALL
    assert profile.required_members == frozenset(
        {"callId", "command", "conversationId", "cwd", "parsedCmd"}
    )
    assert profile.argv_member == "command"
    assert profile.changes_member is None
    assert dict(profile.decisions) == {
        ApprovalDecision.ACCEPT: "approved",
        ApprovalDecision.DECLINE: "denied",
        ApprovalDecision.CANCEL: "abort",
    }
    file_profile = contract.approval_profile(LEGACY_FILE_METHOD)
    assert file_profile is not None
    assert file_profile.changes_member == "fileChanges"
    assert contract.adaptable_approval_categories == STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES


def test_legacy_mcp_policy_amendment_stays_unsent_without_withdrawing_profiles(
    tmp_path: Path,
) -> None:
    legacy_approval_bundle(tmp_path, denied_object=True, mcp_policy_amendment=True)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.adaptable_approval_categories == STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    for method in (LEGACY_COMMAND_METHOD, LEGACY_FILE_METHOD):
        profile = contract.approval_profile(method)
        assert profile is not None
        assert profile.semantic_decision("approved_mcp_policy_amendment") is None
        assert all(
            profile.wire_decision(decision) != "approved_mcp_policy_amendment"
            for decision in ApprovalDecision
        )


def test_a_build_that_cannot_state_a_move_destination_stays_unprofiled(tmp_path: Path) -> None:
    """A change shape without a destination could not show what accepting a move authorises."""
    legacy_approval_bundle(tmp_path, file_change_destination=False)

    contract = load_generated_contract(tmp_path, version="fake")

    assert (
        LEGACY_FILE_METHOD in contract.server_requests[ServerRequestCategory.FILE_CHANGE_APPROVAL]
    )
    assert contract.approval_profile(LEGACY_FILE_METHOD) is None
    # The alias stays advertised and unreadable, which withdraws Stage B readiness rather
    # than presenting a review with a destination missing from it.
    assert ServerRequestCategory.FILE_CHANGE_APPROVAL not in contract.adaptable_approval_categories


def test_a_legacy_move_destination_from_generic_additional_properties_stays_unprofiled(
    tmp_path: Path,
) -> None:
    """A generic nullable string must not become ownership of the semantic move destination."""
    legacy_approval_bundle(
        tmp_path,
        file_change_destination=False,
        file_change_additional={"type": ["string", "null"]},
    )

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(LEGACY_FILE_METHOD) is None
    assert ServerRequestCategory.FILE_CHANGE_APPROVAL not in contract.adaptable_approval_categories


def test_a_legacy_object_refusal_is_answered_with_a_schema_valid_witness(tmp_path: Path) -> None:
    """The newer builds spell the legacy refusal as an object carrying rejection text."""
    legacy_approval_bundle(tmp_path, denied_object=True)

    contract = load_generated_contract(tmp_path, version="fake")

    profile = contract.approval_profile(LEGACY_COMMAND_METHOD)
    assert profile is not None
    assert profile.wire_decision(ApprovalDecision.DECLINE) == {"denied": {"rejection": ""}}
    assert profile.wire_decision(ApprovalDecision.CANCEL) == "abort"


@pytest.mark.parametrize(
    "rejection",
    [
        {"type": "string", "minLength": 1},
        {"type": "string", "enum": ["policy violation"]},
        {"type": "integer"},
    ],
)
def test_a_legacy_object_refusal_without_a_provable_witness_is_unprofiled(
    tmp_path: Path,
    rejection: dict[str, JsonValue],
) -> None:
    """The empty rejection is sent only where that exact schema proves it is a value."""
    legacy_approval_bundle(tmp_path, denied_object=True, rejection=rejection)

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(LEGACY_COMMAND_METHOD) is None
    assert contract.approval_profile(LEGACY_FILE_METHOD) is None
    # Both legacy families answer with that same unreadable refusal, so neither required
    # category has every advertised alias readable any more.
    assert contract.adaptable_approval_categories == frozenset()
    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is not None


def test_an_unprofiled_alias_withdraws_its_whole_category(tmp_path: Path) -> None:
    """Which advertised alias a live turn sends is unverified, so one is not enough."""
    unreadable = schema_variant(
        LEGACY_COMMAND_METHOD,
        params_title="ExecCommandApprovalParams",
        params_required=frozenset({"callId", "conversationId"}),
        params_properties={
            "callId": {"type": "string"},
            "conversationId": {"type": "string"},
        },
    )
    approval_bundle(tmp_path, extra_variants=[unreadable])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile(LEGACY_COMMAND_METHOD) is None
    assert contract.approval_profile(COMMAND_APPROVAL_METHOD) is not None
    assert contract.adaptable_approval_categories == frozenset(
        {ServerRequestCategory.FILE_CHANGE_APPROVAL}
    )


def test_approval_profiles_are_immutable(tmp_path: Path) -> None:
    approval_bundle(tmp_path)
    contract = load_generated_contract(tmp_path, version="fake")
    profile = contract.approval_profile(COMMAND_APPROVAL_METHOD)
    assert profile is not None

    with pytest.raises(FrozenInstanceError):
        profile.offer_member = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        cast("dict[str, ApprovalProfile]", contract.approval_profiles)["other"] = profile
    with pytest.raises(TypeError):
        cast("dict[ApprovalDecision, str]", profile.decisions)[ApprovalDecision.ACCEPT] = "other"


def test_an_unadvertised_method_has_no_profile(tmp_path: Path) -> None:
    approval_bundle(tmp_path)
    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.approval_profile("METHOD_NAME_SECRET") is None
    assert contract.approval_profile(cast("str", 7)) is None


def retained_bundles() -> list[Path]:
    """Every generated bundle retained from an observed Codex build, if any is present."""
    artifacts = Path(__file__).resolve().parents[1] / ".codex" / "artifacts"
    if not artifacts.is_dir():
        return []
    return sorted(
        candidate
        for candidate in artifacts.glob("schema-probe-*/*/*")
        if (candidate / "ServerRequest.json").is_file()
    )


def test_every_retained_bundle_reports_adaptable_stage_b_approvals() -> None:
    bundles = retained_bundles()
    if not bundles:
        pytest.skip("no retained generated bundle is present")

    for bundle in bundles:
        contract = load_generated_contract(
            bundle,
            version="retained",
            experimental_schema=bundle.name == "experimental",
        )
        assert contract.agent_event_profile is not None
        thread_start = contract.method(SemanticMethod.THREAD_START)
        assert thread_start is not None
        assert thread_start.semantic_fields == frozenset(
            {"cwd", "ephemeral", "sandbox", "approvalPolicy"}
        )
        assert contract.agent_event_profile.turn_completed_method == "turn/completed"
        assert contract.agent_event_profile.item_completed_method == "item/completed"
        assert contract.agent_event_profile.agent_message_delta_method == (
            "item/agentMessage/delta"
        )
        assert contract.agent_event_profile.turn_status_values == frozenset(
            {"completed", "interrupted", "failed", "inProgress"}
        )
        if "moco-stage-b-schema" in str(bundle):
            assert (
                "completedAtMs" not in contract.agent_event_profile.item_completed_required_fields
            )
        else:
            assert "completedAtMs" in contract.agent_event_profile.item_completed_required_fields
        advertised = {
            method: category
            for category, methods in contract.server_requests.items()
            for method in methods
        }
        assert contract.server_request_categories >= STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
        assert contract.adaptable_approval_categories == STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
        assert contract.approval_profiles.keys() <= advertised.keys()
        # Every alias a required category advertises must be readable, whichever one a live
        # turn chooses.
        for method, category in advertised.items():
            if category in STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES:
                assert contract.approval_profile(method) is not None
        stated_changes = 0
        for method, profile in contract.approval_profiles.items():
            assert advertised[method] is profile.category
            assert profile.required_members >= _CORRELATION_MEMBERS[profile.correlation]
            assert profile.required_members <= profile.declared_members
            assert profile.declared_members == frozenset(profile.member_contracts)
            assert profile.offer_member is None or profile.offer_member in profile.declared_members
            wires = [profile.wire_decision(decision) for decision in ApprovalDecision]
            assert profile.decisions.keys() == set(ApprovalDecision)
            assert all(profile.admits_decision(wire) for wire in wires)
            assert len({json.dumps(wire, sort_keys=True) for wire in wires}) == len(wires)
            if profile.changes_member is None:
                assert profile.change_shape is None
                continue
            stated_changes += 1
            shape = profile.change_shape
            assert shape is not None
            # Every build stating its own changed files must still admit a move that names
            # where the file ends up, or the review could not disclose what accept covers.
            changes = profile.member_contracts[profile.changes_member]
            assert changes.admits(
                {
                    "source": {
                        shape.kind_member: "update",
                        "unified_diff": "",
                        shape.destination_member: "destination",
                    }
                }
            )
        # Every retained build advertises a family that states its own changed files, so no
        # bundle passes this test by having none.
        assert stated_changes


def test_retained_bundles_differ_in_the_members_and_offers_they_declare() -> None:
    """No single required-member set or decision offer is true of every observed build."""
    bundles = retained_bundles()
    if not bundles:
        pytest.skip("no retained generated bundle is present")

    required: set[frozenset[str]] = set()
    offers: set[str | None] = set()
    refusals: set[str] = set()
    correlations: set[ApprovalCorrelation] = set()
    unadaptable: set[str] = set()
    for bundle in bundles:
        contract = load_generated_contract(bundle, version="retained")
        advertised = {method for methods in contract.server_requests.values() for method in methods}
        unadaptable |= advertised - contract.approval_profiles.keys()
        for profile in contract.approval_profiles.values():
            required.add(profile.required_members)
            offers.add(profile.offer_member)
            correlations.add(profile.correlation)
            refusals.add(json.dumps(profile.wire_decision(ApprovalDecision.DECLINE)))

    assert len(required) > 1
    assert len(offers) > 1
    # The observed builds state their correlation and spell their refusal in more than one
    # way, so neither can be assumed from one build.
    assert correlations == set(ApprovalCorrelation)
    assert len(refusals) > 1
    # Categories with no typed adapter at all are still discovered, and are the reason a
    # build must not be called ready from its category titles alone.
    assert unadaptable


def test_a_profile_answers_only_a_decision_it_was_built_with(tmp_path: Path) -> None:
    legacy_approval_bundle(tmp_path)
    contract = load_generated_contract(tmp_path, version="fake")

    approval = contract.approval_profile(LEGACY_COMMAND_METHOD)

    assert approval is not None
    assert approval.wire_decision(ApprovalDecision.DECLINE) == "denied"
    assert approval.semantic_decision("abort") is ApprovalDecision.CANCEL
    assert approval.semantic_decision("timed_out") is None
    assert approval.semantic_decision(cast("JsonValue", object())) is None
    with pytest.raises(CodexSchemaError):
        approval.wire_decision(cast("ApprovalDecision", "approved"))
