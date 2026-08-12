from __future__ import annotations

import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from moco.errors import PrivateStateError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_STATE_LOCK_NAME = ".runtime-state.lock"
_PRIVATE_RUNTIME_LEASE_NAME = ".runtime-owner.lock"


@dataclass(frozen=True, slots=True)
class WindowsSecuritySnapshot:
    owner_sid: str
    current_user_sid: str
    allowed_sids: frozenset[str]
    trusted_sids: frozenset[str]
    null_dacl: bool
    reparse_point: bool
    dacl_protected: bool


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class PrivateStateIdentity:
    _path_identity: _PathIdentity = field(repr=False)


def validate_windows_security(snapshot: WindowsSecuritySnapshot) -> None:
    if snapshot.reparse_point:
        msg = "runtime-private must not be a reparse point"
        raise PrivateStateError(msg)
    identities = (
        snapshot.owner_sid,
        snapshot.current_user_sid,
        *snapshot.allowed_sids,
        *snapshot.trusted_sids,
    )
    if (
        not snapshot.owner_sid
        or not snapshot.current_user_sid
        or not snapshot.trusted_sids
        or any(not identity for identity in identities)
    ):
        msg = "runtime-private security identity is unavailable"
        raise PrivateStateError(msg)
    if snapshot.owner_sid != snapshot.current_user_sid:
        msg = "runtime-private owner is not the current user"
        raise PrivateStateError(msg)
    if snapshot.null_dacl or not snapshot.allowed_sids <= snapshot.trusted_sids:
        msg = "runtime-private grants access to an untrusted principal"
        raise PrivateStateError(msg)
    if not snapshot.dacl_protected:
        msg = "runtime-private access control is not protected"
        raise PrivateStateError(msg)


def prepare_private_runtime_directory(
    path: Path,
    *,
    platform_name: str | None = None,
) -> None:
    platform_value = platform_name or sys.platform
    created = False
    try:
        os.lstat(path)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            created = False
        else:
            created = True
    if created and platform_value == "win32":
        _protect_windows_dacl(path)
    validate_private_runtime_directory(path, platform_name=platform_value)


def validate_private_runtime_directory(
    path: Path,
    *,
    platform_name: str | None = None,
) -> None:
    platform_value = platform_name or sys.platform
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        msg = "runtime-private must not be a symbolic link"
        raise PrivateStateError(msg)
    if not stat.S_ISDIR(metadata.st_mode):
        msg = "runtime-private must be a directory"
        raise PrivateStateError(msg)
    if platform_value == "win32":
        validate_windows_security(_read_windows_security(path))
        return
    if metadata.st_uid != _current_posix_user_id():
        msg = "runtime-private owner is not the current user"
        raise PrivateStateError(msg)
    if _posix_permissions(metadata) != _PRIVATE_DIRECTORY_MODE:
        msg = "runtime-private permissions are not private"
        raise PrivateStateError(msg)


def validate_private_state_file(
    path: Path,
    *,
    platform_name: str | None = None,
) -> None:
    platform_value = platform_name or sys.platform
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        msg = "runtime state must not be a symbolic link"
        raise PrivateStateError(msg)
    if not stat.S_ISREG(metadata.st_mode):
        msg = "runtime state must be a regular file"
        raise PrivateStateError(msg)
    if platform_value == "win32":
        validate_windows_security(_read_windows_security(path))
        return
    _validate_posix_file_metadata(metadata)


def write_private_state(
    path: Path,
    content: bytes,
    *,
    platform_name: str | None = None,
) -> PrivateStateIdentity:
    platform_value = platform_name or sys.platform
    prepare_private_runtime_directory(path.parent, platform_name=platform_value)
    with (
        _hold_private_namespace(path.parent, platform_name=platform_value),
        _hold_private_state_lock(path, platform_name=platform_value),
    ):
        parent_identity = _validated_directory_identity(
            path.parent,
            platform_name=platform_value,
        )
        if os.path.lexists(path):
            validate_private_state_file(path, platform_name=platform_value)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        persisted_identity: _PathIdentity | None = None
        try:
            _require_same_directory(
                path.parent,
                parent_identity,
                platform_name=platform_value,
            )
            if platform_value == "win32":
                _protect_windows_dacl(temporary)
            else:
                os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            validate_private_state_file(temporary, platform_name=platform_value)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor_open = False
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _require_same_directory(
                path.parent,
                parent_identity,
                platform_name=platform_value,
            )
            persisted_identity = _path_identity(temporary)
            temporary.replace(path)
            validate_private_state_file(path, platform_name=platform_value)
            identity = PrivateStateIdentity(persisted_identity)
            _require_same_directory(
                path.parent,
                parent_identity,
                platform_name=platform_value,
            )
            _sync_directory(path.parent, platform_name=platform_value)
        except BaseException:
            if persisted_identity is not None:
                _best_effort_remove_owned_state(
                    path,
                    persisted_identity,
                    platform_name=platform_value,
                )
            raise
        finally:
            if descriptor_open:
                os.close(descriptor)
            if os.path.lexists(temporary):
                temporary.unlink()
        return identity


def read_private_state(
    path: Path,
    *,
    platform_name: str | None = None,
) -> bytes:
    platform_value = platform_name or sys.platform
    with (
        _hold_private_namespace(path.parent, platform_name=platform_value),
        _hold_private_state_lock(path, platform_name=platform_value),
    ):
        parent_identity = _validated_directory_identity(
            path.parent,
            platform_name=platform_value,
        )
        validate_private_state_file(path, platform_name=platform_value)
        before = _path_identity(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if platform_value != "win32":
                _validate_posix_file_metadata(metadata)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        validate_private_state_file(path, platform_name=platform_value)
        if _path_identity(path) != before:
            msg = "runtime state changed while it was being read"
            raise PrivateStateError(msg)
        _require_same_directory(
            path.parent,
            parent_identity,
            platform_name=platform_value,
        )
        return content


@contextmanager
def hold_private_runtime_lease(
    path: Path,
    *,
    platform_name: str | None = None,
) -> Iterator[None]:
    platform_value = platform_name or sys.platform
    prepare_private_runtime_directory(path.parent, platform_name=platform_value)
    with (
        _hold_private_namespace(path.parent, platform_name=platform_value),
        _hold_private_lock(
            path.parent / _PRIVATE_RUNTIME_LEASE_NAME,
            platform_name=platform_value,
            blocking=False,
        ),
    ):
        yield


def remove_private_state(
    path: Path,
    *,
    expected_identity: PrivateStateIdentity | None = None,
    platform_name: str | None = None,
) -> None:
    platform_value = platform_name or sys.platform
    try:
        os.lstat(path.parent)
    except FileNotFoundError:
        return
    with (
        _hold_private_namespace(path.parent, platform_name=platform_value),
        _hold_private_state_lock(path, platform_name=platform_value),
    ):
        parent_identity = _validated_directory_identity(
            path.parent,
            platform_name=platform_value,
        )
        try:
            validate_private_state_file(path, platform_name=platform_value)
        except FileNotFoundError:
            return
        before = _path_identity(path)
        if (
            expected_identity is not None and before != expected_identity._path_identity  # noqa: SLF001 - opaque token owner
        ):
            return
        _require_same_directory(
            path.parent,
            parent_identity,
            platform_name=platform_value,
        )
        if _path_identity(path) != before:
            if expected_identity is not None:
                return
            msg = "runtime state changed before it could be removed"
            raise PrivateStateError(msg)
        path.unlink()
        _require_same_directory(
            path.parent,
            parent_identity,
            platform_name=platform_value,
        )
        _sync_directory(path.parent, platform_name=platform_value)


def _validated_directory_identity(path: Path, *, platform_name: str) -> _PathIdentity:
    validate_private_runtime_directory(path, platform_name=platform_name)
    return _path_identity(path)


def _require_same_directory(
    path: Path,
    expected: _PathIdentity,
    *,
    platform_name: str,
) -> None:
    validate_private_runtime_directory(path, platform_name=platform_name)
    if _path_identity(path) != expected:
        msg = "runtime-private changed during state access"
        raise PrivateStateError(msg)


def _path_identity(path: Path) -> _PathIdentity:
    metadata = os.lstat(path)
    return _metadata_identity(metadata)


def _metadata_identity(metadata: os.stat_result) -> _PathIdentity:
    return _PathIdentity(metadata.st_dev, metadata.st_ino)


def _validate_posix_file_metadata(metadata: os.stat_result) -> None:
    if metadata.st_uid != _current_posix_user_id():
        msg = "runtime state owner is not the current user"
        raise PrivateStateError(msg)
    if _posix_permissions(metadata) != _PRIVATE_FILE_MODE:
        msg = "runtime state permissions are not private"
        raise PrivateStateError(msg)


def _current_posix_user_id() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        msg = "POSIX owner identity is unavailable"
        raise PrivateStateError(msg)
    return int(getuid())


def _posix_permissions(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


def _read_windows_security(path: Path) -> WindowsSecuritySnapshot:
    from moco.runtime._windows_acl import read_windows_security  # noqa: PLC0415

    return read_windows_security(path)


def _protect_windows_dacl(path: Path) -> None:
    from moco.runtime._windows_acl import protect_windows_dacl  # noqa: PLC0415

    protect_windows_dacl(path)


@contextmanager
def _hold_private_state_lock(path: Path, *, platform_name: str) -> Iterator[None]:
    with _hold_private_lock(
        path.parent / _PRIVATE_STATE_LOCK_NAME,
        platform_name=platform_name,
        blocking=True,
    ):
        yield


@contextmanager
def _hold_private_lock(
    lock_path: Path,
    *,
    platform_name: str,
    blocking: bool,
) -> Iterator[None]:
    if platform_name == "win32":
        with _hold_windows_state_lock(lock_path) as (handle, created):
            try:
                _validate_windows_state_lock(lock_path, handle, created=created)
            except (OSError, PrivateStateError):
                msg = "runtime state lock could not be acquired"
                raise PrivateStateError(msg) from None
            yield
        return

    descriptor = -1
    try:
        descriptor = _open_posix_state_lock(lock_path)
        import fcntl  # noqa: PLC0415 - unavailable on Windows

        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        _validate_posix_state_lock(descriptor, lock_path, platform_name=platform_name)
    except (OSError, PrivateStateError):
        if descriptor >= 0:
            os.close(descriptor)
        msg = "runtime state lock could not be acquired"
        raise PrivateStateError(msg) from None
    try:
        yield
    finally:
        import fcntl  # noqa: PLC0415 - unavailable on Windows

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _open_posix_state_lock(path: Path) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    except FileExistsError:
        flags &= ~(os.O_CREAT | os.O_EXCL)
        return os.open(path, flags)
    try:
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _validate_posix_state_lock(descriptor: int, path: Path, *, platform_name: str) -> None:
    metadata = os.fstat(descriptor)
    _validate_posix_file_metadata(metadata)
    if not stat.S_ISREG(metadata.st_mode):
        msg = "runtime state lock must be a regular file"
        raise PrivateStateError(msg)
    validate_private_state_file(path, platform_name=platform_name)
    if _path_identity(path) != _metadata_identity(metadata):
        msg = "runtime state lock changed during acquisition"
        raise PrivateStateError(msg)


def _validate_windows_state_lock(path: Path, handle: object, *, created: bool) -> None:
    if created:
        _protect_windows_handle_dacl(handle)
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        msg = "runtime state lock must be a regular file"
        raise PrivateStateError(msg)
    validate_windows_security(_read_windows_handle_security(handle))


def _hold_windows_state_lock(
    path: Path,
) -> AbstractContextManager[tuple[object, bool]]:
    from moco.runtime._windows_acl import hold_windows_state_lock  # noqa: PLC0415

    return hold_windows_state_lock(path)


def _protect_windows_handle_dacl(handle: object) -> None:
    from moco.runtime._windows_acl import protect_windows_handle_dacl  # noqa: PLC0415

    protect_windows_handle_dacl(handle)


def _read_windows_handle_security(handle: object) -> WindowsSecuritySnapshot:
    from moco.runtime._windows_acl import read_windows_handle_security  # noqa: PLC0415

    return read_windows_handle_security(handle)


def _best_effort_remove_owned_state(
    path: Path,
    expected: _PathIdentity,
    *,
    platform_name: str,
) -> None:
    try:
        if _path_identity(path) != expected:
            return
        path.unlink()
        _sync_directory(path.parent, platform_name=platform_name)
    except Exception:  # noqa: BLE001 - cleanup must preserve the primary failure
        return


@contextmanager
def _hold_private_namespace(path: Path, *, platform_name: str) -> Iterator[None]:
    if platform_name != "win32":
        yield
        return
    from moco.runtime._windows_acl import (  # noqa: PLC0415
        hold_windows_directory_namespace,
    )

    # The directory handles block namespace replacement while child operations run.
    # Python's child file APIs remain name-based, so every acquired namespace is
    # revalidated before use and every created child is independently ACL-checked.
    with hold_windows_directory_namespace(path):
        yield


def _sync_directory(path: Path, *, platform_name: str) -> None:
    if platform_name == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
