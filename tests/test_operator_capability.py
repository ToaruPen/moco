from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from moco.errors import PrivateStateError
from moco.platform import default_runtime_state_path
from moco.runtime import operator_capability

TOKEN_A = "A" * 43
TOKEN_B = "B" * 43


def test_load_or_create_persists_and_reuses_owner_private_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "operator-capability.json"
    generated = iter((TOKEN_A, TOKEN_B))
    monkeypatch.setattr(
        "moco.runtime.operator_capability.secrets.token_urlsafe",
        lambda _size: next(generated),
    )

    assert operator_capability.load_or_create_operator_capability(path) == TOKEN_A
    assert operator_capability.load_or_create_operator_capability(path) == TOKEN_A
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_bytes()) == {"version": 1, "capability": TOKEN_A}


def test_operator_capability_path_is_sibling_of_runtime_state(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-private" / "runtime.json"

    assert operator_capability.operator_capability_path(runtime) == runtime.with_name(
        "operator-capability.json"
    )


@pytest.mark.parametrize(
    ("platform_name", "environ", "expected"),
    [
        (
            "darwin",
            {"HOME": "/Users/example"},
            Path("/Users/example/Library/Application Support/moco/operator-capability.json"),
        ),
        (
            "win32",
            {"LOCALAPPDATA": r"C:\Users\example\AppData\Local"},
            Path(r"C:\Users\example\AppData\Local/moco/runtime-private/operator-capability.json"),
        ),
    ],
)
def test_operator_capability_path_follows_injected_runtime_environment(
    platform_name: str,
    environ: dict[str, str],
    expected: Path,
) -> None:
    runtime = default_runtime_state_path(platform_name=platform_name, environ=environ)

    assert operator_capability.operator_capability_path(runtime) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"version":true,"capability":"' + TOKEN_A.encode() + b'"}',
        b'{"version":2,"capability":"' + TOKEN_A.encode() + b'"}',
        b'{"version":1,"capability":"short"}',
        b'{"version":1,"capability":"' + (b"!" * 43) + b'"}',
        b'{"version":1,"capability":"' + TOKEN_A.encode() + b'","extra":true}',
    ],
)
def test_existing_invalid_document_fails_closed_without_secret_in_error(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "runtime-private" / "operator-capability.json"
    path.parent.mkdir(mode=0o700)
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(PrivateStateError) as caught:
        operator_capability.load_or_create_operator_capability(path)

    assert "capability document is invalid" in str(caught.value)
    assert TOKEN_A not in str(caught.value)
    assert path.read_bytes() == payload


def test_private_boundary_failure_is_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "operator-capability.json"

    def reject_read(_path: Path) -> bytes:
        message = "operator capability permissions are not private"
        raise PrivateStateError(message)

    monkeypatch.setattr(
        "moco.runtime.operator_capability.os.path.lexists",
        lambda _path: True,
    )
    monkeypatch.setattr(operator_capability, "read_private_state", reject_read)
    monkeypatch.setattr(
        operator_capability,
        "write_private_state",
        lambda *_args, **_kwargs: pytest.fail("unsafe state must not be overwritten"),
    )

    with pytest.raises(PrivateStateError, match="permissions are not private"):
        operator_capability.load_or_create_operator_capability(path)
