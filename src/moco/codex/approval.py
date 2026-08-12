"""Typed adapters for the two Codex approval requests a local reviewer may answer.

Only command execution and file change approvals are adapted, and only as the profile the
generated schema of the running build proves. That profile decides which params members may
appear, which must, what each one may hold, which identifiers the family states, and which
JSON value each reviewer decision is sent as. Anything else about an approval payload that
this module cannot explain - a method without a profile, a member holding a value its own
schema refuses, a scope that outlives the one request, or a decision moco cannot answer -
raises `CodexSchemaError` so the caller stops the turn instead of showing a reviewer.

The values built here are the only place an approval detail lives. Their `repr` carries
bounded metadata alone, so a log line, an exception, or a traceback never reveals a
command, an argument, a path, an identifier, or any other payload fragment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from moco.codex.schema import (
    ApprovalCorrelation,
    ApprovalDecision,
    ApprovalProfile,
    ServerRequestCategory,
    _is_transport_safe,
)
from moco.errors import CodexSchemaError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from moco.codex.rpc import JsonValue, RequestId, RpcNotification
    from moco.codex.schema import CodexProtocolContract

# The reviewer semantics live with the generated contract that spells each one, and are part
# of this adapter's surface, so they are re-exported rather than named twice.
__all__ = [
    "ApprovalDecision",
    "ApprovalRequestCorrelation",
    "ApprovalReview",
    "CommandApprovalReview",
    "ConversationCallCorrelation",
    "FileChangeApprovalReview",
    "FileChangeEntry",
    "FileChangeExplanation",
    "FileChangeKind",
    "ThreadItemCorrelation",
    "adapt_approval_request",
    "adapt_file_change_patch_notification",
]

_UNSUPPORTED_METHOD = "Codex approval request method is not adaptable"
_UNSUPPORTED_PARAMS = "Codex approval request params are not adaptable"
_UNSUPPORTED_SCOPE = "Codex approval request scope cannot be explained"
_UNSUPPORTED_DECISION = "Codex approval decision is not available"
_UNSUPPORTED_REVIEW = "Codex approval review value cannot be presented"

# One inbound params object is a plain object the JSON-RPC decoder built, so it is copied
# once into at most this many members. Every generated approval params declares far fewer,
# and a member past the bound is unknown anyway, so the bound only stops an unbounded read.
_MAX_PARAMS_MEMBERS = 64
# One decision offer is a list from the same payload, so it is read no further than a build
# could plausibly spell. Every observed decision type lists far fewer.
_MAX_OFFERED_DECISIONS = 32
# How many arguments of one command, and how many changed files of one patch, a reviewer is
# shown. A request past either bound is refused rather than truncated into a half-review.
_MAX_COMMAND_ARGUMENTS = 256
_MAX_FILE_CHANGES = 512
_MAX_PATCH_CHANGES = 64
# How long one reviewed path may be. Every filesystem a Codex build writes to names a file
# in far less, and a longer one is refused rather than shown to a reviewer in part.
_MAX_PATH_CHARACTERS = 4_096


class FileChangeKind(StrEnum):
    """How one file named by a pending change is affected."""

    ADD = "add"
    DELETE = "delete"
    UPDATE = "update"


# Presented in this order unless the request narrows the offer itself.
_ONE_SHOT_DECISIONS = (ApprovalDecision.ACCEPT, ApprovalDecision.DECLINE, ApprovalDecision.CANCEL)
# A reviewer who cannot refuse is not a reviewer, so an offer without one fails closed.
_REFUSALS = frozenset({ApprovalDecision.DECLINE, ApprovalDecision.CANCEL})

_ADAPTED_CATEGORIES = frozenset(
    {ServerRequestCategory.COMMAND_APPROVAL, ServerRequestCategory.FILE_CHANGE_APPROVAL}
)

# The members this adapter reads by meaning. Whether a build declares or requires them, and
# what else it may send, is the profile's to say.
_THREAD_FIELD = "threadId"
_TURN_FIELD = "turnId"
_ITEM_FIELD = "itemId"
_CONVERSATION_FIELD = "conversationId"
_CALL_FIELD = "callId"
_COMMAND_FIELD = "command"
_CWD_FIELD = "cwd"
_REASON_FIELD = "reason"
_PATCH_FIELDS = frozenset({_THREAD_FIELD, _TURN_FIELD, _ITEM_FIELD, "changes"})
_PATCH_CHANGE_FIELDS = frozenset({"diff", "kind", "path"})


def adapt_file_change_patch_notification(
    contract: CodexProtocolContract,
    notification: RpcNotification,
) -> FileChangeExplanation | None:
    """Adapt the schema-proven patch event into bounded explanation metadata only."""
    profile = contract.file_change_patch_profile
    if profile is None or notification.method != profile.method:
        return None
    fields = _params_snapshot(notification.params)
    if frozenset(fields) != _PATCH_FIELDS or not profile.admits(fields):
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    raw_changes = fields["changes"]
    if type(raw_changes) is not list or not raw_changes or len(raw_changes) > _MAX_PATCH_CHANGES:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    changes = tuple(_adapt_file_change_patch_entry(change) for change in raw_changes)
    return FileChangeExplanation(
        thread_id=_required_text(fields, _THREAD_FIELD, _UNSUPPORTED_PARAMS),
        turn_id=_required_text(fields, _TURN_FIELD, _UNSUPPORTED_PARAMS),
        item_id=_required_text(fields, _ITEM_FIELD, _UNSUPPORTED_PARAMS),
        changes=changes,
    )


def _adapt_file_change_patch_entry(value: JsonValue) -> FileChangeEntry:
    if type(value) is not dict or _stated_members(value) != _PATCH_CHANGE_FIELDS:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    diff = value["diff"]
    if type(diff) is not str or not _is_transport_safe(diff):
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    raw_kind = value["kind"]
    if type(raw_kind) is not dict:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    kind_fields = _stated_members(raw_kind)
    kind = _change_kind(raw_kind.get("type"))
    if kind is FileChangeKind.UPDATE:
        if kind_fields not in (frozenset({"type"}), frozenset({"move_path", "type"})):
            raise CodexSchemaError(_UNSUPPORTED_PARAMS)
        destination = _change_destination(raw_kind.get("move_path"))
    else:
        if kind_fields != frozenset({"type"}):
            raise CodexSchemaError(_UNSUPPORTED_PARAMS)
        destination = None
    return FileChangeEntry(
        kind=kind,
        path=_require_path(value["path"]),
        destination=destination,
    )


@dataclass(frozen=True, slots=True, repr=False)
class ThreadItemCorrelation:
    """Which request one newer approval is asking about: its thread, turn and item.

    The request id the app server sent this approval under is carried beside them. It is the
    only identifier that is unique on its own, and the later owner of the response answers
    exactly it; nothing here tracks, retains, or answers a request.
    """

    request_id: RequestId
    thread_id: str
    turn_id: str
    item_id: str

    def __post_init__(self) -> None:
        _require_request_id(self.request_id)
        for value in (self.thread_id, self.turn_id, self.item_id):
            _require_text(value, _UNSUPPORTED_REVIEW)

    def __repr__(self) -> str:
        return "ThreadItemCorrelation()"


@dataclass(frozen=True, slots=True, repr=False)
class ConversationCallCorrelation:
    """Which request one legacy approval is asking about: its conversation and tool call.

    A legacy approval states no turn and no item, so none is invented here. The conversation
    and the call are what that family says, and the app server's own request id completes
    the identity of the one prompt under review.
    """

    request_id: RequestId
    conversation_id: str
    call_id: str

    def __post_init__(self) -> None:
        _require_request_id(self.request_id)
        for value in (self.conversation_id, self.call_id):
            _require_text(value, _UNSUPPORTED_REVIEW)

    def __repr__(self) -> str:
        return "ConversationCallCorrelation()"


type ApprovalRequestCorrelation = ThreadItemCorrelation | ConversationCallCorrelation

_CORRELATION_KINDS: Mapping[ApprovalCorrelation, type[ApprovalRequestCorrelation]] = {
    ApprovalCorrelation.THREAD_ITEM: ThreadItemCorrelation,
    ApprovalCorrelation.CONVERSATION_CALL: ConversationCallCorrelation,
}


@dataclass(frozen=True, slots=True, repr=False)
class FileChangeEntry:
    """One file a pending change affects, named for the reviewer only.

    A change that moves or renames the file states where it ends up as well as where it
    starts, because accepting authorises both. An entry that could not state one of them is
    not a change this reviewer can present, so it is refused rather than shown in part.
    """

    kind: FileChangeKind
    path: str
    destination: str | None = None

    def __post_init__(self) -> None:
        kind = cast("object", self.kind)
        if type(kind) is not FileChangeKind:
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
        _require_path(self.path)
        destination = cast("object", self.destination)
        if destination is None:
            return
        # Only a change rewriting a file in place also moves it; a created or deleted file
        # naming a second path is an operation no reviewed shape spells.
        if kind is not FileChangeKind.UPDATE:
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
        _require_path(destination)

    def __repr__(self) -> str:
        return f"FileChangeEntry(kind={self.kind.value})"


@dataclass(frozen=True, slots=True, repr=False)
class FileChangeExplanation:
    """What a file change approval is about, when its own params never state it.

    The newer file change approval carries no patch body, so the caller must supply the
    changes it already observed for the same item. There is no default: an approval whose
    effect nobody can explain has no reviewer and fails closed. A legacy file change
    approval states its own changed files and needs no explanation.

    An item identifier repeats across threads and turns, so the whole request identity is
    carried and compared. An explanation gathered for one turn can then never describe the
    change another turn is asking about.
    """

    thread_id: str
    turn_id: str
    item_id: str
    changes: tuple[FileChangeEntry, ...]

    def __post_init__(self) -> None:
        for value in (self.thread_id, self.turn_id, self.item_id):
            _require_text(value, _UNSUPPORTED_SCOPE)
        object.__setattr__(self, "changes", _validated_changes(self.changes))

    def explains(self, correlation: ApprovalRequestCorrelation) -> bool:
        if not isinstance(correlation, ThreadItemCorrelation):
            return False
        return (self.thread_id, self.turn_id, self.item_id) == (
            correlation.thread_id,
            correlation.turn_id,
            correlation.item_id,
        )

    def __repr__(self) -> str:
        return f"FileChangeExplanation(changes={len(self.changes)})"


@dataclass(frozen=True, slots=True, repr=False)
class CommandApprovalReview:
    """One command execution approval a local reviewer can decide exactly once."""

    profile: ApprovalProfile
    correlation: ApprovalRequestCorrelation
    command: str | tuple[str, ...]
    cwd: str
    reason: str | None
    decisions: tuple[ApprovalDecision, ...]

    def __post_init__(self) -> None:
        _require_profile(self.profile, ServerRequestCategory.COMMAND_APPROVAL, self.correlation)
        command = cast("object", self.command)
        if type(command) is str:
            object.__setattr__(self, "command", _require_text(command, _UNSUPPORTED_REVIEW))
        else:
            object.__setattr__(self, "command", _validated_command(command))
        _require_text(self.cwd, _UNSUPPORTED_REVIEW)
        _require_optional_text(self.reason)
        object.__setattr__(self, "decisions", _validated_decisions(self.decisions))

    @property
    def category(self) -> ServerRequestCategory:
        return self.profile.category

    def response_for(self, decision: ApprovalDecision) -> dict[str, JsonValue]:
        return _decision_response(self.profile, self.decisions, decision)

    def __repr__(self) -> str:
        command_metadata = (
            "text=True" if type(self.command) is str else f"arguments={len(self.command)}"
        )
        return (
            f"CommandApprovalReview({command_metadata}, "
            f"decisions={_decision_names(self.decisions)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FileChangeApprovalReview:
    """One file change approval a local reviewer can decide exactly once."""

    profile: ApprovalProfile
    correlation: ApprovalRequestCorrelation
    reason: str | None
    changes: tuple[FileChangeEntry, ...]
    decisions: tuple[ApprovalDecision, ...]

    def __post_init__(self) -> None:
        _require_profile(self.profile, ServerRequestCategory.FILE_CHANGE_APPROVAL, self.correlation)
        _require_optional_text(self.reason)
        object.__setattr__(self, "changes", _validated_changes(self.changes))
        object.__setattr__(self, "decisions", _validated_decisions(self.decisions))

    @property
    def category(self) -> ServerRequestCategory:
        return self.profile.category

    def response_for(self, decision: ApprovalDecision) -> dict[str, JsonValue]:
        return _decision_response(self.profile, self.decisions, decision)

    def __repr__(self) -> str:
        return (
            f"FileChangeApprovalReview(changes={len(self.changes)}, "
            f"decisions={_decision_names(self.decisions)})"
        )


type ApprovalReview = CommandApprovalReview | FileChangeApprovalReview


def adapt_approval_request(
    contract: CodexProtocolContract,
    method: str,
    params: dict[str, JsonValue],
    *,
    request_id: RequestId,
    file_change_explanation: FileChangeExplanation | None = None,
) -> ApprovalReview:
    """Read one inbound approval request into the value a local reviewer decides.

    The method is routed through the discovered contract and answered only with the profile
    that build proves, so no approval family is assumed to be the one an installed build
    selects. `request_id` is the app server's own id for this request, which completes the
    identity of a family that states no turn or item. `file_change_explanation` is required
    for a file change request whose own params carry no changed files, and must be absent
    everywhere else.
    """
    profile = _adapted_profile(contract, method)
    fields = _params_snapshot(params)
    _reject_unexplained_members(profile, fields)
    correlation = _correlation(profile, fields, request_id)
    if profile.category is ServerRequestCategory.COMMAND_APPROVAL:
        if file_change_explanation is not None:
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
        return _command_review(profile, fields, correlation)
    if profile.category is ServerRequestCategory.FILE_CHANGE_APPROVAL:
        return _file_change_review(profile, fields, correlation, file_change_explanation)
    # An adapted category is always one of the two above. Routing still names the remaining
    # case explicitly, so a category moco stops adapting can never fall into a file review.
    raise CodexSchemaError(_UNSUPPORTED_METHOD)  # pragma: no cover - routing invariant


def _adapted_profile(contract: CodexProtocolContract, method: str) -> ApprovalProfile:
    """Return the profile this build proves for one advertised method."""
    category = _adapted_category(contract, method)
    profile = contract.approval_profile(method)
    if profile is None or profile.category is not category:
        raise CodexSchemaError(_UNSUPPORTED_METHOD)
    return profile


def _adapted_category(contract: CodexProtocolContract, method: str) -> ServerRequestCategory:
    """Return the one category that advertises this method, as the exact discovered value.

    A category key is compared by identity rather than equality: a plain string spelling a
    category value compares equal to the member and hashes the same, so equality alone would
    let a hand-built contract redirect a command approval into the file change branch.
    """
    if type(method) is not str or not _is_transport_safe(method):
        raise CodexSchemaError(_UNSUPPORTED_METHOD)
    owners = [category for category, names in contract.server_requests.items() if method in names]
    if len(owners) != 1:
        raise CodexSchemaError(_UNSUPPORTED_METHOD)
    owner = owners[0]
    if type(owner) is not ServerRequestCategory or owner not in _ADAPTED_CATEGORIES:
        raise CodexSchemaError(_UNSUPPORTED_METHOD)
    return owner


def _reject_unexplained_members(profile: ApprovalProfile, fields: dict[str, JsonValue]) -> None:
    """Hold the payload to what this build declares, requires, may hold, and may not widen."""
    present = frozenset(fields)
    if not present <= profile.declared_members or not profile.required_members <= present:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    for name, value in fields.items():
        # Every declared member is checked whole against its own compiled schema, including
        # the nested values moco only displays or drops, so nothing reaches a reviewer, a
        # response, or a later slice that this build's own schema would refuse.
        if not profile.admits_member(name, value):
            raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    for name in profile.absent_or_null_members & present:
        if fields[name] is not None:
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)


def _correlation(
    profile: ApprovalProfile,
    fields: dict[str, JsonValue],
    request_id: RequestId,
) -> ApprovalRequestCorrelation:
    """Read the identifiers this family states, and no identifier it does not state."""
    if profile.correlation is ApprovalCorrelation.THREAD_ITEM:
        return ThreadItemCorrelation(
            request_id=request_id,
            thread_id=_required_text(fields, _THREAD_FIELD, _UNSUPPORTED_PARAMS),
            turn_id=_required_text(fields, _TURN_FIELD, _UNSUPPORTED_PARAMS),
            item_id=_required_text(fields, _ITEM_FIELD, _UNSUPPORTED_PARAMS),
        )
    if profile.correlation is ApprovalCorrelation.CONVERSATION_CALL:
        return ConversationCallCorrelation(
            request_id=request_id,
            conversation_id=_required_text(fields, _CONVERSATION_FIELD, _UNSUPPORTED_PARAMS),
            call_id=_required_text(fields, _CALL_FIELD, _UNSUPPORTED_PARAMS),
        )
    # A profile carries one of the two discovered kinds, and a hand-built one is refused
    # before it can route, so nothing else can reach a review.
    raise CodexSchemaError(_UNSUPPORTED_PARAMS)  # pragma: no cover - routing invariant


def _command_review(
    profile: ApprovalProfile,
    fields: dict[str, JsonValue],
    correlation: ApprovalRequestCorrelation,
) -> CommandApprovalReview:
    return CommandApprovalReview(
        profile=profile,
        correlation=correlation,
        command=_reviewed_command(profile, fields),
        cwd=_required_text(fields, _CWD_FIELD, _UNSUPPORTED_SCOPE),
        reason=_optional_text(fields, _REASON_FIELD),
        decisions=_presented_decisions(profile, fields),
    )


def _reviewed_command(
    profile: ApprovalProfile,
    fields: dict[str, JsonValue],
) -> str | tuple[str, ...]:
    """Read the command under review, whichever way this family spells it.

    A family that sends the argument vector is shown as that vector, unjoined: quoting it
    into one line would show a reviewer a command that is not the one about to run.
    """
    argv_member = profile.argv_member
    if argv_member is None:
        return _required_text(fields, _COMMAND_FIELD, _UNSUPPORTED_SCOPE)
    arguments = fields.get(argv_member)
    if type(arguments) is not list or not arguments or len(arguments) > _MAX_COMMAND_ARGUMENTS:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    return _validated_command(tuple(cast("list[object]", arguments)))


def _file_change_review(
    profile: ApprovalProfile,
    fields: dict[str, JsonValue],
    correlation: ApprovalRequestCorrelation,
    explanation: FileChangeExplanation | None,
) -> FileChangeApprovalReview:
    changes_member = profile.changes_member
    if changes_member is None:
        if explanation is None or not explanation.explains(correlation):
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
        changes = explanation.changes
    else:
        if explanation is not None:
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
        changes = _declared_changes(profile, fields, changes_member)
    return FileChangeApprovalReview(
        profile=profile,
        correlation=correlation,
        reason=_optional_text(fields, _REASON_FIELD),
        changes=changes,
        decisions=_presented_decisions(profile, fields),
    )


def _declared_changes(
    profile: ApprovalProfile,
    fields: dict[str, JsonValue],
    changes_member: str,
) -> tuple[FileChangeEntry, ...]:
    """Read the changed files a family states itself, as the reviewer's bounded list.

    Every path accepting would authorise is kept: the file each change names, and the second
    path a move or rename ends at. The patch body beside them is left unread, so no file
    content reaches a review, a log line, or a later slice. A change carrying a member this
    shape does not spell could mean something else again, so it fails closed instead.
    """
    shape = profile.change_shape
    declared = fields.get(changes_member)
    if (
        shape is None
        or type(declared) is not dict
        or not declared
        or len(declared) > _MAX_FILE_CHANGES
    ):
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    # Every path is checked before any of them is compared, hashed again, or ordered, so an
    # element overriding one of those cannot run at all.
    paths = _validated_change_paths(declared)
    entries: list[FileChangeEntry] = []
    for path in paths:
        change = declared[path]
        if type(change) is not dict or not _stated_members(change) <= shape.members:
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
        entries.append(
            FileChangeEntry(
                kind=_change_kind(change.get(shape.kind_member)),
                path=path,
                destination=_change_destination(change.get(shape.destination_member)),
            )
        )
    return tuple(entries)


def _validated_change_paths(declared: dict[str, JsonValue]) -> list[str]:
    """Return the changed paths in a stable order, refusing one no reviewer could be shown."""
    named = cast("dict[object, object]", declared)
    for path in named:
        _require_path(path)
    return sorted(declared)


def _stated_members(change: dict[str, JsonValue]) -> frozenset[str]:
    """Name what one change object states, refusing a member name that is not plain text."""
    members = cast("dict[object, object]", change)
    for name in members:
        if type(name) is not str or not _is_transport_safe(name):
            raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    return frozenset(change)


def _change_kind(value: JsonValue) -> FileChangeKind:
    """Read how one file is affected, refusing a way this reviewer cannot present."""
    if type(value) is not str or not _is_transport_safe(value):
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    try:
        return FileChangeKind(value)
    except ValueError:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE) from None


def _change_destination(value: JsonValue) -> str | None:
    """Read where a move or rename ends, or nothing when this change moves no file.

    The destination is kept exactly as the request states it. Rewriting it into a shorter or
    absolute form would show the reviewer a scope the request never asked for.
    """
    if value is None:
        return None
    return _require_path(value)


def _params_snapshot(params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Copy the inbound params into a bounded plain object of at most the declared size.

    The JSON-RPC decoder hands over a plain object, so exactly that is accepted here. A
    mapping of any other type may answer one member twice differently, repeat a name until
    a bound on distinct members never trips, or raise an exception quoting the payload, so
    it is refused rather than read.
    """
    if type(params) is not dict:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    members = cast("dict[object, object]", params)
    if len(members) > _MAX_PARAMS_MEMBERS:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    snapshot: dict[str, JsonValue] = {}
    for name, value in members.items():
        if type(name) is not str or not _is_transport_safe(name):
            raise CodexSchemaError(_UNSUPPORTED_PARAMS)
        snapshot[name] = cast("JsonValue", value)
    return snapshot


def _require_request_id(value: object) -> None:
    """Refuse a request id the transport could not have carried."""
    if type(value) is int:
        return
    if type(value) is not str or not value.strip() or not _is_transport_safe(value):
        raise CodexSchemaError(_UNSUPPORTED_REVIEW)


def _require_path(value: object) -> str:
    """Require one path a reviewer can read whole and a transport can carry unchanged."""
    text = _require_text(value, _UNSUPPORTED_SCOPE)
    if len(text) > _MAX_PATH_CHARACTERS:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    return text


def _require_text(value: object, message: str) -> str:
    """Require one non-blank string a reviewer can be shown and a transport can carry."""
    if type(value) is not str or not value.strip() or not _is_transport_safe(value):
        raise CodexSchemaError(message)
    return value


def _require_optional_text(value: object) -> None:
    if value is not None:
        _require_text(value, _UNSUPPORTED_REVIEW)


def _validated_command(value: object) -> tuple[str, ...]:
    """Copy the command under review, refusing a container that could rewrite itself."""
    if type(value) is not tuple and type(value) is not list:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    if len(value) > _MAX_COMMAND_ARGUMENTS:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    arguments = tuple(cast("tuple[object, ...]", value))
    if not arguments:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    # One argument may legitimately be empty, but a command that is blank throughout names
    # nothing a reviewer could weigh.
    if any(type(argument) is not str or not _is_transport_safe(argument) for argument in arguments):
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    text = cast("tuple[str, ...]", arguments)
    if not any(argument.strip() for argument in text):
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    return text


def _validated_changes(value: object) -> tuple[FileChangeEntry, ...]:
    """Copy the explained changes, so no caller keeps a handle on reviewed state."""
    if type(value) is not tuple and type(value) is not list:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    if len(value) > _MAX_FILE_CHANGES:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    changes = tuple(cast("tuple[object, ...]", value))
    if not changes:
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    if any(type(change) is not FileChangeEntry for change in changes):
        raise CodexSchemaError(_UNSUPPORTED_SCOPE)
    return cast("tuple[FileChangeEntry, ...]", changes)


def _validated_decisions(value: object) -> tuple[ApprovalDecision, ...]:
    """Copy the offered decisions, refusing an offer no reviewer could answer."""
    if type(value) is not tuple and type(value) is not list:
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    if len(value) > len(_ONE_SHOT_DECISIONS):
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    decisions = tuple(cast("tuple[object, ...]", value))
    if not decisions or any(type(decision) is not ApprovalDecision for decision in decisions):
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    presented = cast("tuple[ApprovalDecision, ...]", decisions)
    if len(set(presented)) != len(presented) or _REFUSALS.isdisjoint(presented):
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    return presented


def _require_profile(
    value: object,
    category: ServerRequestCategory,
    correlation: object,
) -> None:
    """Refuse a review built on a profile that answers a different family or request."""
    if type(value) is not ApprovalProfile or value.category is not category:
        raise CodexSchemaError(_UNSUPPORTED_REVIEW)
    expected = _CORRELATION_KINDS.get(value.correlation)
    if expected is None or type(correlation) is not expected:
        raise CodexSchemaError(_UNSUPPORTED_REVIEW)


def _required_text(fields: dict[str, JsonValue], name: str, message: str) -> str:
    return _require_text(fields.get(name), message)


def _optional_text(fields: dict[str, JsonValue], name: str) -> str | None:
    value: object = fields.get(name)
    if value is None:
        return None
    return _require_text(value, _UNSUPPORTED_PARAMS)


def _presented_decisions(
    profile: ApprovalProfile,
    fields: dict[str, JsonValue],
) -> tuple[ApprovalDecision, ...]:
    """Intersect the decisions the request offers with the ones moco can answer.

    A build that declares no offer member never narrows the set, so the whole one-shot set
    this build proves is presented.
    """
    offer_member = profile.offer_member
    offered: object = fields.get(offer_member) if offer_member is not None else None
    if offered is None:
        return _ONE_SHOT_DECISIONS
    if type(offered) is not list or len(offered) > _MAX_OFFERED_DECISIONS:
        raise CodexSchemaError(_UNSUPPORTED_PARAMS)
    presented: list[ApprovalDecision] = []
    for candidate in cast("list[JsonValue]", offered):
        decision = _offered_decision(profile, candidate)
        if decision is not None and decision not in presented:
            presented.append(decision)
    if not presented or _REFUSALS.isdisjoint(presented):
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    return tuple(presented)


def _offered_decision(profile: ApprovalProfile, candidate: JsonValue) -> ApprovalDecision | None:
    """Read one offered decision, or none for a decision moco never presents.

    A decision that keeps applying after this request, or that only the app server itself
    reports, is dropped from the offer instead of being shown as a one-shot button - but
    only once its whole offered value is one this build's own decision schema admits.
    Anything else fails closed, because an offer moco half understands is not an offer.
    """
    if not profile.admits_decision(candidate):
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    return profile.semantic_decision(candidate)


def _decision_response(
    profile: ApprovalProfile,
    presented: tuple[ApprovalDecision, ...],
    decision: ApprovalDecision,
) -> dict[str, JsonValue]:
    """Build one fresh response object the JSON-RPC boundary accepts as a JSON object.

    The wire value is the one this build's generated response document proves, so decline
    and cancel stay apart in whichever vocabulary that build spells them. The transport
    admits a plain object only, and the caller owns what it sends, so each call answers with
    a new value instead of a view onto the reviewed state.
    """
    if type(decision) is not ApprovalDecision or decision not in presented:
        raise CodexSchemaError(_UNSUPPORTED_DECISION)
    # The profile materialises the value its own generated response document proves. That
    # reading holds a JSON number exactly, while the transport carries one as a float, so
    # the two spell one JSON value with two Python types and no decision carries a number.
    return {"decision": cast("JsonValue", profile.wire_decision(decision))}


def _decision_names(presented: tuple[ApprovalDecision, ...]) -> tuple[str, ...]:
    return tuple(decision.value for decision in presented)
