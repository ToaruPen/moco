from __future__ import annotations

import json
import os
import re
import secrets
from typing import TYPE_CHECKING, NoReturn

from moco.errors import PrivateStateError
from moco.runtime.private_state import (
    read_private_state,
    remove_private_state,
    write_private_state,
)

if TYPE_CHECKING:
    from pathlib import Path

_DOCUMENT_VERSION = 1
_CAPABILITY_FILE_NAME = "operator-capability.json"
_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def operator_capability_path(runtime_state_path: Path) -> Path:
    return runtime_state_path.with_name(_CAPABILITY_FILE_NAME)


def load_or_create_operator_capability(path: Path) -> str:
    if os.path.lexists(path):
        return _parse_operator_capability(read_private_state(path))
    capability = secrets.token_urlsafe(32)
    payload = json.dumps(
        {"version": _DOCUMENT_VERSION, "capability": capability},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    write_private_state(path, payload)
    return capability


def rotate_operator_capability(path: Path) -> None:
    remove_private_state(path)


def _parse_operator_capability(content: bytes) -> str:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _raise_invalid_document(error)
    if type(payload) is not dict or set(payload) != {"version", "capability"}:
        _raise_invalid_document()
    capability = payload["capability"]
    if (
        type(payload["version"]) is not int
        or payload["version"] != _DOCUMENT_VERSION
        or type(capability) is not str
        or _CAPABILITY_PATTERN.fullmatch(capability) is None
    ):
        _raise_invalid_document()
    return capability


def _raise_invalid_document(error: BaseException | None = None) -> NoReturn:
    message = "operator capability document is invalid"
    if error is None:
        raise PrivateStateError(message)
    raise PrivateStateError(message) from error
