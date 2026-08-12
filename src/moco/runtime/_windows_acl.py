from __future__ import annotations

import os
import stat
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, cast

import ntsecuritycon
import win32api
import win32con
import win32file
import win32security

from moco.errors import PrivateStateError
from moco.runtime.private_state import WindowsSecuritySnapshot

_STATE_LOCK_RETRY_DELAY_SECONDS = 0.01

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _WindowsHandle(Protocol):
    def Close(self) -> None:  # noqa: N802 - mirrors pywin32
        """Close the Windows kernel handle."""


class _WindowsDacl(Protocol):
    def GetAceCount(self) -> int:  # noqa: N802 - mirrors pywin32
        """Return the ACE count."""

    def GetAce(self, index: int) -> tuple[tuple[int, int], object, object]:  # noqa: N802
        """Return one ACE."""


class _MutableWindowsDacl(Protocol):
    def AddAccessAllowedAceEx(  # noqa: N802 - mirrors pywin32
        self,
        revision: int,
        inheritance: int,
        access: int,
        sid: object,
    ) -> None:
        """Add one allow ACE."""


class _SecurityDescriptor(Protocol):
    def GetSecurityDescriptorOwner(self) -> object:  # noqa: N802 - mirrors pywin32
        """Return the owner SID."""

    def GetSecurityDescriptorDacl(self) -> _WindowsDacl | None:  # noqa: N802
        """Return the DACL."""

    def GetSecurityDescriptorControl(self) -> tuple[int, int]:  # noqa: N802
        """Return descriptor control bits and revision."""


def read_windows_security(path: Path) -> WindowsSecuritySnapshot:
    try:
        return _read_windows_security(path)
    except PrivateStateError:
        raise
    except Exception:  # noqa: BLE001 - normalize the OS security boundary
        msg = "runtime-private security could not be inspected"
        raise PrivateStateError(msg) from None


def protect_windows_dacl(path: Path) -> None:
    try:
        current_user, dacl = _private_security()
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION,
            current_user,
            None,
            None,
            None,
        )
    except Exception:  # noqa: BLE001 - normalize the OS security boundary
        msg = "runtime-private access control could not be protected"
        raise PrivateStateError(msg) from None


def read_windows_handle_security(handle: object) -> WindowsSecuritySnapshot:
    try:
        descriptor = win32security.GetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        information = win32file.GetFileInformationByHandleEx(
            handle,
            win32file.FileAttributeTagInfo,
        )
        return _security_snapshot(descriptor, information["FileAttributes"])
    except PrivateStateError:
        raise
    except Exception:  # noqa: BLE001 - normalize the OS security boundary
        msg = "runtime state lock security could not be inspected"
        raise PrivateStateError(msg) from None


def protect_windows_handle_dacl(handle: object) -> None:
    try:
        current_user, dacl = _private_security()
        win32security.SetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        win32security.SetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION,
            current_user,
            None,
            None,
            None,
        )
    except Exception:  # noqa: BLE001 - normalize the OS security boundary
        msg = "runtime state lock access control could not be protected"
        raise PrivateStateError(msg) from None


@contextmanager
def hold_windows_state_lock(
    path: Path,
    *,
    blocking: bool,
) -> Iterator[tuple[object, bool]]:
    while True:
        try:
            handle = cast(
                "_WindowsHandle",
                win32file.CreateFile(
                    str(path),
                    win32con.GENERIC_READ
                    | win32con.GENERIC_WRITE
                    | win32con.READ_CONTROL
                    | win32con.WRITE_DAC
                    | win32con.WRITE_OWNER,
                    0,
                    None,
                    win32con.OPEN_ALWAYS,
                    win32con.FILE_ATTRIBUTE_NORMAL | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                ),
            )
            created = win32api.GetLastError() != getattr(
                win32con,
                "ERROR_ALREADY_EXISTS",
                183,
            )
            break
        except Exception as error:  # noqa: BLE001 - normalize the OS security boundary
            sharing_violation = getattr(win32con, "ERROR_SHARING_VIOLATION", 32)
            if not blocking or getattr(error, "winerror", None) != sharing_violation:
                msg = "runtime state lock could not be acquired"
                raise PrivateStateError(msg) from None
            time.sleep(_STATE_LOCK_RETRY_DELAY_SECONDS)
    try:
        yield handle, created
    finally:
        handle.Close()


@contextmanager
def hold_windows_directory_namespace(path: Path) -> Iterator[None]:
    handles: list[_WindowsHandle] = []
    try:
        for directory in (path.parent, path):
            handles.append(  # noqa: PERF401 - retain partial acquisition for cleanup
                cast(
                    "_WindowsHandle",
                    win32file.CreateFile(
                        str(directory),
                        ntsecuritycon.FILE_READ_ATTRIBUTES,
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                        None,
                        win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_BACKUP_SEMANTICS
                        | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                        None,
                    ),
                ),
            )
    except Exception:  # noqa: BLE001 - normalize the OS security boundary
        for handle in reversed(handles):
            handle.Close()
        msg = "runtime-private namespace could not be secured"
        raise PrivateStateError(msg) from None
    try:
        yield
    finally:
        for handle in reversed(handles):
            handle.Close()


def _read_windows_security(path: Path) -> WindowsSecuritySnapshot:
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return _security_snapshot(descriptor, attributes)


def _security_snapshot(descriptor: object, attributes: int) -> WindowsSecuritySnapshot:
    security_descriptor = cast("_SecurityDescriptor", descriptor)
    owner = security_descriptor.GetSecurityDescriptorOwner()
    dacl = security_descriptor.GetSecurityDescriptorDacl()
    control, _revision = security_descriptor.GetSecurityDescriptorControl()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        current_user = win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    administrators = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid,
        None,
    )
    trusted = frozenset(
        win32security.ConvertSidToStringSid(sid) for sid in (current_user, system, administrators)
    )
    allowed: set[str] = set()
    if dacl is not None:
        allowed_types = {
            win32security.ACCESS_ALLOWED_ACE_TYPE,
            win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        }
        denied_types = {
            win32security.ACCESS_DENIED_ACE_TYPE,
            win32security.ACCESS_DENIED_OBJECT_ACE_TYPE,
        }
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            ace_type = ace[0][0]
            if ace_type in allowed_types:
                allowed.add(win32security.ConvertSidToStringSid(ace[-1]))
            elif ace_type not in denied_types:
                msg = "runtime-private contains an unknown ACE type"
                raise PrivateStateError(msg)
    return WindowsSecuritySnapshot(
        owner_sid=win32security.ConvertSidToStringSid(owner),
        current_user_sid=win32security.ConvertSidToStringSid(current_user),
        allowed_sids=frozenset(allowed),
        trusted_sids=trusted,
        null_dacl=dacl is None,
        reparse_point=bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT),
        dacl_protected=bool(control & win32security.SE_DACL_PROTECTED),
    )


def _private_security() -> tuple[object, _MutableWindowsDacl]:
    current_user = _current_user_sid()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    administrators = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid,
        None,
    )
    dacl = cast("_MutableWindowsDacl", win32security.ACL())
    inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    for sid in (current_user, system, administrators):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    return current_user, dacl


def _current_user_sid() -> object:
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        return win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
    finally:
        token.Close()
