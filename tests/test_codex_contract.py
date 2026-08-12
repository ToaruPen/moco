from __future__ import annotations

import json

import pytest

from moco.codex.schema import (
    AGENT_READINESS_METHODS,
    STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES,
    VOICE_REQUIRED_METHODS,
    ApprovalCorrelation,
    ApprovalDecision,
    CodexSchemaProbe,
    ParamsKind,
    SemanticMethod,
    ServerRequestCategory,
)
from moco.platform import resolve_codex_command

pytestmark = pytest.mark.contract

# What each approval family states about the request it is asking about.
_CORRELATION_MEMBERS = {
    ApprovalCorrelation.THREAD_ITEM: frozenset({"threadId", "turnId", "itemId"}),
    ApprovalCorrelation.CONVERSATION_CALL: frozenset({"conversationId", "callId"}),
}

# Every Codex build moco supports advertises a parameterless managed-requirements read, so
# it is classified rather than optional. A build that drops it must fail this contract loudly.
_REQUIRED_SEMANTICS = (
    VOICE_REQUIRED_METHODS
    | AGENT_READINESS_METHODS
    | frozenset({SemanticMethod.CONFIG_REQUIREMENTS_READ})
)
_OPTIONAL_SEMANTICS = frozenset(SemanticMethod) - _REQUIRED_SEMANTICS


def test_installed_codex_advertises_stage_b_core_semantics() -> None:
    command = resolve_codex_command(None)
    contract = CodexSchemaProbe(command).probe_sync()

    assert contract.methods.keys() >= VOICE_REQUIRED_METHODS
    assert contract.missing_methods <= _OPTIONAL_SEMANTICS
    assert contract.server_requests.keys() >= STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    assert all(
        isinstance(method, str) and method
        for methods in contract.server_requests.values()
        for method in methods
    )
    assert contract.version


def test_installed_codex_advertises_parameterless_managed_requirements_read() -> None:
    command = resolve_codex_command(None)
    contract = CodexSchemaProbe(command).probe_sync()

    requirements = contract.require_method(SemanticMethod.CONFIG_REQUIREMENTS_READ)
    assert requirements.name
    assert requirements.params_kind is ParamsKind.OMITTED
    assert requirements.semantic_fields == frozenset()


def test_installed_codex_advertises_agent_execution_semantics() -> None:
    command = resolve_codex_command(None)
    contract = CodexSchemaProbe(command).probe_sync()

    assert contract.methods.keys() >= AGENT_READINESS_METHODS
    for semantic in AGENT_READINESS_METHODS:
        method = contract.require_method(semantic)
        assert method.name
        assert method.params_kind is ParamsKind.OBJECT
        assert method.semantic_fields
    profile = contract.agent_event_profile
    assert profile is not None
    assert profile.turn_completed_method
    assert profile.item_completed_method
    assert profile.agent_message_phase_values >= {"commentary", "final_answer"}
    assert profile.turn_status_values >= {"completed", "interrupted", "failed", "inProgress"}


def test_installed_codex_approval_families_are_adaptable_per_raw_method() -> None:
    command = resolve_codex_command(None)
    contract = CodexSchemaProbe(command).probe_sync()

    advertised = {
        method: category
        for category, methods in contract.server_requests.items()
        for method in methods
    }
    assert contract.adaptable_approval_categories == STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    assert contract.approval_profiles.keys() <= advertised.keys()
    # Which advertised alias a live turn sends is not something a client chooses, so every
    # alias of a required category must be readable before a privileged turn may start.
    required_aliases = {
        method
        for method, category in advertised.items()
        if category in STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    }
    assert required_aliases
    assert required_aliases <= contract.approval_profiles.keys()
    for method, profile in contract.approval_profiles.items():
        assert contract.approval_profile(method) is profile
        assert advertised[method] is profile.category
        assert profile.required_members >= _CORRELATION_MEMBERS[profile.correlation]
        assert profile.required_members <= profile.declared_members
        assert profile.declared_members == frozenset(profile.member_contracts)
        assert profile.required_members.isdisjoint(profile.absent_or_null_members)
        assert profile.decisions.keys() == set(ApprovalDecision)
        wires = [profile.wire_decision(decision) for decision in ApprovalDecision]
        assert all(profile.admits_decision(wire) for wire in wires)
        assert len({json.dumps(wire, sort_keys=True) for wire in wires}) == len(ApprovalDecision)

    modern_file_profiles = [
        profile
        for profile in contract.approval_profiles.values()
        if profile.category is ServerRequestCategory.FILE_CHANGE_APPROVAL
        and profile.changes_member is None
    ]
    if modern_file_profiles:
        assert contract.file_change_patch_profile is not None


def test_installed_codex_keeps_non_stage_b_categories_unadapted() -> None:
    """Extra categories may be discovered without claiming an unsupported adapter."""
    command = resolve_codex_command(None)
    contract = CodexSchemaProbe(command).probe_sync()

    other = {
        method
        for category, methods in contract.server_requests.items()
        for method in methods
        if category not in STAGE_B_REQUIRED_SERVER_REQUEST_CATEGORIES
    }
    assert other.isdisjoint(contract.approval_profiles.keys())
