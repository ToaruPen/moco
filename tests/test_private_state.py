from __future__ import annotations

import importlib
import os
import stat
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from moco.errors import PrivateStateError
from moco.runtime import private_state
from moco.runtime.private_state import (
    PrivateStateIdentity,
    WindowsSecuritySnapshot,
    prepare_private_runtime_directory,
    read_private_state,
    remove_private_state,
    validate_private_runtime_directory,
    validate_private_state_file,
    validate_windows_security,
    write_private_state,
)

requires_posix = pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX ownership")


@pytest.fixture(autouse=True)
def _inject_simulated_posix_user_id_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if hasattr(os, "getuid"):
        return
    current_user_id = os.lstat(tmp_path).st_uid
    monkeypatch.setattr(private_state, "_current_posix_user_id", lambda: current_user_id)
    monkeypatch.setattr(
        private_state,
        "_posix_permissions",
        lambda metadata: 0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600,
        raising=False,
    )


def test_windows_snapshot_requires_current_owner_and_trusted_allow_aces() -> None:
    safe = WindowsSecuritySnapshot(
        owner_sid="S-1-user",
        current_user_sid="S-1-user",
        allowed_sids=frozenset({"S-1-user", "S-1-system", "S-1-admins"}),
        trusted_sids=frozenset({"S-1-user", "S-1-system", "S-1-admins"}),
        null_dacl=False,
        reparse_point=False,
        dacl_protected=True,
    )

    validate_windows_security(safe)
    with pytest.raises(PrivateStateError, match="owner"):
        validate_windows_security(replace(safe, owner_sid="S-1-sandbox"))
    with pytest.raises(PrivateStateError, match="access"):
        validate_windows_security(replace(safe, allowed_sids=safe.allowed_sids | {"S-1-sandbox"}))
    with pytest.raises(PrivateStateError, match="access"):
        validate_windows_security(replace(safe, null_dacl=True))
    with pytest.raises(PrivateStateError, match="reparse"):
        validate_windows_security(replace(safe, reparse_point=True))
    with pytest.raises(PrivateStateError, match="protected"):
        validate_windows_security(replace(safe, dacl_protected=False))


@pytest.mark.parametrize(
    "snapshot",
    [
        WindowsSecuritySnapshot(
            owner_sid="",
            current_user_sid="",
            allowed_sids=frozenset(),
            trusted_sids=frozenset(),
            null_dacl=False,
            reparse_point=False,
            dacl_protected=True,
        ),
        WindowsSecuritySnapshot(
            owner_sid="S-1-user",
            current_user_sid="S-1-user",
            allowed_sids=frozenset({""}),
            trusted_sids=frozenset({""}),
            null_dacl=False,
            reparse_point=False,
            dacl_protected=True,
        ),
    ],
)
def test_windows_snapshot_rejects_unknown_security_identities(
    snapshot: WindowsSecuritySnapshot,
) -> None:
    with pytest.raises(PrivateStateError, match="identity"):
        validate_windows_security(snapshot)


@pytest.mark.skipif(sys.platform == "win32", reason="uses injected pywin32 bindings")
def test_windows_binding_rejects_unknown_ace_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeToken:
        def Close(self) -> None:  # noqa: N802 - mirrors pywin32
            return None

    class FakeDacl:
        def GetAceCount(self) -> int:  # noqa: N802 - mirrors pywin32
            return 1

        def GetAce(self, _index: int) -> tuple[tuple[int, int], int, str]:  # noqa: N802
            return ((999, 0), 0, "S-1-unknown")

    class FakeDescriptor:
        def GetSecurityDescriptorOwner(self) -> str:  # noqa: N802 - mirrors pywin32
            return "S-1-user"

        def GetSecurityDescriptorDacl(self) -> FakeDacl:  # noqa: N802 - mirrors pywin32
            return FakeDacl()

        def GetSecurityDescriptorControl(self) -> tuple[int, int]:  # noqa: N802
            return (0x1000, 1)

    fake_security = SimpleNamespace(
        ACCESS_ALLOWED_ACE_TYPE=0,
        ACCESS_ALLOWED_OBJECT_ACE_TYPE=5,
        ACCESS_DENIED_ACE_TYPE=1,
        ACCESS_DENIED_OBJECT_ACE_TYPE=6,
        DACL_SECURITY_INFORMATION=4,
        OWNER_SECURITY_INFORMATION=1,
        SE_DACL_PROTECTED=0x1000,
        SE_FILE_OBJECT=1,
        TOKEN_QUERY=8,
        TokenUser=1,
        WinBuiltinAdministratorsSid=26,
        WinLocalSystemSid=22,
        ConvertSidToStringSid=str,
        CreateWellKnownSid=lambda kind, _domain: f"S-1-trusted-{kind}",
        GetNamedSecurityInfo=lambda *_args: FakeDescriptor(),
        GetTokenInformation=lambda _token, _kind: ("S-1-user",),
        OpenProcessToken=lambda *_args: FakeToken(),
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetCurrentProcess=lambda: 1))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(TOKEN_QUERY=8))
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32security", fake_security)
    monkeypatch.setitem(sys.modules, "ntsecuritycon", SimpleNamespace())
    sys.modules.pop("moco.runtime._windows_acl", None)
    module = importlib.import_module("moco.runtime._windows_acl")
    reader = cast(
        Callable[[Path], WindowsSecuritySnapshot],  # noqa: TC006 - keeps runtime import visible
        module.read_windows_security,
    )
    try:
        with pytest.raises(PrivateStateError, match="unknown ACE"):
            reader(tmp_path)
    finally:
        sys.modules.pop("moco.runtime._windows_acl", None)


@pytest.mark.skipif(sys.platform == "win32", reason="uses injected pywin32 bindings")
def test_windows_binding_reads_and_protects_dacl_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeToken:
        def Close(self) -> None:  # noqa: N802 - mirrors pywin32
            return None

    class FakeDacl:
        def GetAceCount(self) -> int:  # noqa: N802 - mirrors pywin32
            return 0

    dacl = FakeDacl()

    class FakeDescriptor:
        def GetSecurityDescriptorOwner(self) -> str:  # noqa: N802 - mirrors pywin32
            return "S-1-user"

        def GetSecurityDescriptorDacl(self) -> FakeDacl:  # noqa: N802 - mirrors pywin32
            return dacl

        def GetSecurityDescriptorControl(self) -> tuple[int, int]:  # noqa: N802
            return (0x1000, 1)

    class FakeAcl:
        def __init__(self) -> None:
            self.allowed: list[tuple[int, int, int, str]] = []

        def AddAccessAllowedAceEx(  # noqa: N802 - mirrors pywin32
            self,
            revision: int,
            inheritance: int,
            access: int,
            sid: str,
        ) -> None:
            self.allowed.append((revision, inheritance, access, sid))

    set_calls: list[tuple[object, ...]] = []
    set_handle_calls: list[tuple[object, ...]] = []
    fake_security = SimpleNamespace(
        ACL=FakeAcl,
        ACL_REVISION=2,
        ACCESS_ALLOWED_ACE_TYPE=0,
        ACCESS_ALLOWED_OBJECT_ACE_TYPE=5,
        ACCESS_DENIED_ACE_TYPE=1,
        ACCESS_DENIED_OBJECT_ACE_TYPE=6,
        DACL_SECURITY_INFORMATION=4,
        OWNER_SECURITY_INFORMATION=1,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
        SE_DACL_PROTECTED=0x1000,
        SE_FILE_OBJECT=1,
        TOKEN_QUERY=8,
        TokenUser=1,
        WinBuiltinAdministratorsSid=26,
        WinLocalSystemSid=22,
        ConvertSidToStringSid=str,
        CreateWellKnownSid=lambda kind, _domain: f"S-1-trusted-{kind}",
        GetNamedSecurityInfo=lambda *_args: FakeDescriptor(),
        GetSecurityInfo=lambda *_args: FakeDescriptor(),
        GetTokenInformation=lambda _token, _kind: ("S-1-user",),
        OpenProcessToken=lambda *_args: FakeToken(),
        SetNamedSecurityInfo=lambda *args: set_calls.append(args),
        SetSecurityInfo=lambda *args: set_handle_calls.append(args),
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetCurrentProcess=lambda: 1))
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        SimpleNamespace(CONTAINER_INHERIT_ACE=2, OBJECT_INHERIT_ACE=1, TOKEN_QUERY=8),
    )
    monkeypatch.setitem(sys.modules, "win32file", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32security", fake_security)
    monkeypatch.setitem(sys.modules, "ntsecuritycon", SimpleNamespace(FILE_ALL_ACCESS=0x1F01FF))
    sys.modules.pop("moco.runtime._windows_acl", None)
    module = importlib.import_module("moco.runtime._windows_acl")
    try:
        snapshot = module.read_windows_security(tmp_path)
        module.protect_windows_dacl(tmp_path)
        module.protect_windows_handle_dacl("handle")
    finally:
        sys.modules.pop("moco.runtime._windows_acl", None)

    assert snapshot.dacl_protected
    assert [call[2] for call in set_calls] == [
        fake_security.DACL_SECURITY_INFORMATION | fake_security.PROTECTED_DACL_SECURITY_INFORMATION,
        fake_security.OWNER_SECURITY_INFORMATION,
    ]
    assert set_calls[0][3] is None
    assert set_calls[1][3] == "S-1-user"
    protected_value = set_calls[0][5]
    assert protected_value is not dacl
    protected_dacl = cast("FakeAcl", protected_value)
    assert protected_dacl.allowed == [
        (fake_security.ACL_REVISION, 3, 0x1F01FF, "S-1-user"),
        (fake_security.ACL_REVISION, 3, 0x1F01FF, "S-1-trusted-22"),
        (fake_security.ACL_REVISION, 3, 0x1F01FF, "S-1-trusted-26"),
    ]
    assert [call[2] for call in set_handle_calls] == [call[2] for call in set_calls]
    assert set_handle_calls[0][3] is None
    assert set_handle_calls[1][3] == "S-1-user"
    handle_dacl = cast("FakeAcl", set_handle_calls[0][5])
    assert handle_dacl.allowed == protected_dacl.allowed


@pytest.mark.skipif(sys.platform == "win32", reason="uses injected pywin32 bindings")
def test_windows_namespace_guard_holds_parent_and_leaf_without_share_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, int, int, int, int]] = []
    handles: list[SimpleNamespace] = []

    def create_file(
        path: str,
        access: int,
        share: int,
        _security: object,
        creation: int,
        flags: int,
        _template: object,
    ) -> SimpleNamespace:
        handle = SimpleNamespace(closed=False)
        handle.Close = lambda: setattr(handle, "closed", True)
        handles.append(handle)
        opened.append((path, access, share, creation, flags))
        return handle

    fake_con = SimpleNamespace(
        FILE_FLAG_BACKUP_SEMANTICS=0x02000000,
        FILE_SHARE_DELETE=4,
        FILE_SHARE_READ=1,
        FILE_SHARE_WRITE=2,
        OPEN_EXISTING=3,
        TOKEN_QUERY=8,
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    fake_file = SimpleNamespace(
        CreateFile=create_file,
        FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
    )
    monkeypatch.setitem(sys.modules, "win32file", fake_file)
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace())
    fake_ntsecuritycon = SimpleNamespace(FILE_READ_ATTRIBUTES=0x80)
    monkeypatch.setitem(sys.modules, "ntsecuritycon", fake_ntsecuritycon)
    sys.modules.pop("moco.runtime._windows_acl", None)
    module = importlib.import_module("moco.runtime._windows_acl")
    guard = cast("Callable[[Path], object]", module.hold_windows_directory_namespace)
    leaf = tmp_path / "moco" / "runtime-private"
    try:
        with guard(leaf):  # type: ignore[attr-defined]
            assert len(handles) == 2
            assert all(not handle.closed for handle in handles)
    finally:
        sys.modules.pop("moco.runtime._windows_acl", None)

    assert [Path(call[0]) for call in opened] == [leaf.parent, leaf]
    for _path, access, share, creation, flags in opened:
        assert access == fake_ntsecuritycon.FILE_READ_ATTRIBUTES
        assert share == fake_con.FILE_SHARE_READ | fake_con.FILE_SHARE_WRITE
        assert share & fake_con.FILE_SHARE_DELETE == 0
        assert creation == fake_con.OPEN_EXISTING
        assert flags == (
            fake_con.FILE_FLAG_BACKUP_SEMANTICS | fake_file.FILE_FLAG_OPEN_REPARSE_POINT
        )
    assert all(handle.closed for handle in handles)


@pytest.mark.skipif(sys.platform == "win32", reason="uses injected pywin32 bindings")
def test_windows_state_lock_handle_denies_all_sharing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, int, int, int, int]] = []
    handle = SimpleNamespace(closed=False)
    handle.Close = lambda: setattr(handle, "closed", True)

    def create_file(
        path: str,
        access: int,
        share: int,
        _security: object,
        creation: int,
        flags: int,
        _template: object,
    ) -> SimpleNamespace:
        opened.append((path, access, share, creation, flags))
        return handle

    fake_con = SimpleNamespace(
        FILE_ATTRIBUTE_NORMAL=0x80,
        GENERIC_READ=0x80000000,
        GENERIC_WRITE=0x40000000,
        OPEN_ALWAYS=4,
        READ_CONTROL=0x00020000,
        WRITE_DAC=0x00040000,
        WRITE_OWNER=0x00080000,
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetLastError=lambda: 0))
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    fake_file = SimpleNamespace(
        CreateFile=create_file,
        FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
    )
    monkeypatch.setitem(sys.modules, "win32file", fake_file)
    monkeypatch.setitem(sys.modules, "win32security", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "ntsecuritycon", SimpleNamespace())
    sys.modules.pop("moco.runtime._windows_acl", None)
    module = importlib.import_module("moco.runtime._windows_acl")
    holder = cast(
        "Callable[[Path], AbstractContextManager[tuple[object, bool]]]",
        module.hold_windows_state_lock,
    )
    lock_path = tmp_path / "runtime-private" / ".runtime-state.lock"
    try:
        with holder(lock_path) as (held, created):
            assert held is handle
            assert created
            assert not handle.closed
    finally:
        sys.modules.pop("moco.runtime._windows_acl", None)

    assert opened == [
        (
            str(lock_path),
            fake_con.GENERIC_READ
            | fake_con.GENERIC_WRITE
            | fake_con.READ_CONTROL
            | fake_con.WRITE_DAC
            | fake_con.WRITE_OWNER,
            0,
            fake_con.OPEN_ALWAYS,
            fake_con.FILE_ATTRIBUTE_NORMAL | fake_file.FILE_FLAG_OPEN_REPARSE_POINT,
        ),
    ]
    assert handle.closed


def test_runtime_lease_excludes_a_second_daemon_and_is_reusable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "runtime-private" / "runtime.json"
    holder = private_state.hold_private_runtime_lease

    with holder(state_path):
        write_private_state(state_path, b"first")
        with pytest.raises(PrivateStateError), holder(state_path):
            pytest.fail("a second daemon must not acquire the runtime lease")
        assert read_private_state(state_path) == b"first"

    with holder(state_path):
        assert read_private_state(state_path) == b"first"
    remove_private_state(state_path)


def test_windows_new_directory_is_protected_but_existing_directory_is_not_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected: set[Path] = set()

    def snapshot(path: Path) -> WindowsSecuritySnapshot:
        return WindowsSecuritySnapshot(
            owner_sid="S-1-user",
            current_user_sid="S-1-user",
            allowed_sids=frozenset({"S-1-user"}),
            trusted_sids=frozenset({"S-1-user"}),
            null_dacl=False,
            reparse_point=False,
            dacl_protected=path in protected,
        )

    monkeypatch.setattr(private_state, "_read_windows_security", snapshot)
    monkeypatch.setattr(
        private_state,
        "_protect_windows_dacl",
        protected.add,
        raising=False,
    )
    created = tmp_path / "new" / "runtime-private"

    prepare_private_runtime_directory(created, platform_name="win32")

    assert protected == {created}
    existing = tmp_path / "existing" / "runtime-private"
    existing.mkdir(parents=True)
    with pytest.raises(PrivateStateError, match="protected"):
        prepare_private_runtime_directory(existing, platform_name="win32")
    assert existing not in protected


def test_posix_validation_uses_injected_metadata_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o755)
    path = private / "runtime.json"
    path.write_bytes(b"secret")
    path.chmod(0o644)
    current_user_id = private.stat().st_uid
    monkeypatch.setattr(
        private_state,
        "_current_posix_user_id",
        lambda: current_user_id,
        raising=False,
    )
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setattr(
        private_state,
        "_posix_permissions",
        lambda metadata: 0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600,
        raising=False,
    )

    validate_private_runtime_directory(private, platform_name="darwin")
    validate_private_state_file(path, platform_name="darwin")


def test_private_operations_hold_namespace_guard_for_the_entire_file_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = False
    guarded_operations: list[str] = []

    @contextmanager
    def guard(_directory: Path, *, platform_name: str) -> Generator[None]:
        nonlocal active
        assert platform_name == "darwin"
        assert not active
        active = True
        try:
            yield
        finally:
            active = False

    @contextmanager
    def state_lock(_path: Path, *, platform_name: str) -> Generator[None]:
        assert platform_name == "darwin"
        assert active
        yield

    def sync_directory(_path: Path, *, platform_name: str) -> None:
        assert platform_name == "darwin"

    real_mkstemp = tempfile.mkstemp
    real_open = os.open
    real_unlink = Path.unlink

    def checked_mkstemp(**kwargs: str | Path | None) -> tuple[int, str]:
        assert active
        guarded_operations.append("mkstemp")
        directory = kwargs.get("dir")
        prefix = kwargs.get("prefix")
        assert isinstance(prefix, (str, type(None)))
        return real_mkstemp(prefix=prefix, dir=directory)

    def checked_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        assert active
        guarded_operations.append("open")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def checked_unlink(path: Path, missing_ok: bool = False) -> None:  # noqa: FBT002
        assert active
        guarded_operations.append("unlink")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(private_state, "_hold_private_namespace", guard, raising=False)
    monkeypatch.setattr(private_state, "_hold_private_state_lock", state_lock, raising=False)
    monkeypatch.setattr(private_state, "_sync_directory", sync_directory)
    monkeypatch.setattr(tempfile, "mkstemp", checked_mkstemp)
    monkeypatch.setattr(os, "open", checked_open)
    monkeypatch.setattr(Path, "unlink", checked_unlink)
    path = tmp_path / "runtime-private" / "runtime.json"

    identity = write_private_state(path, b"secret", platform_name="darwin")
    assert read_private_state(path, platform_name="darwin") == b"secret"
    remove_private_state(path, expected_identity=identity, platform_name="darwin")

    assert not active
    assert "mkstemp" in guarded_operations
    assert "open" in guarded_operations
    assert "unlink" in guarded_operations


def test_namespace_guard_failure_does_not_persist_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def reject_guard(_directory: Path, *, platform_name: str) -> Generator[None]:
        if platform_name == "darwin":
            msg = "runtime-private namespace could not be secured"
            raise PrivateStateError(msg)
        yield

    monkeypatch.setattr(private_state, "_hold_private_namespace", reject_guard, raising=False)
    path = tmp_path / "runtime-private" / "runtime.json"

    with pytest.raises(PrivateStateError, match="namespace"):
        write_private_state(path, b"RUNTIME_SECRET", platform_name="darwin")

    assert not path.exists()
    assert all(b"RUNTIME_SECRET" not in item.read_bytes() for item in path.parent.iterdir())


@requires_posix
def test_existing_unsafe_state_lock_is_not_repaired_or_bypassed(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    lock = private / ".runtime-state.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    path = private / "runtime.json"

    with pytest.raises(PrivateStateError, match="lock"):
        write_private_state(path, b"RUNTIME_SECRET", platform_name="darwin")

    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    assert not path.exists()


@requires_posix
def test_state_lock_error_redacts_path_and_has_no_cause(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    private_marker = "LOCK_PATH_SECRET"
    target = tmp_path / private_marker
    target.write_bytes(b"")
    lock = private / ".runtime-state.lock"
    lock.symlink_to(target)

    with pytest.raises(PrivateStateError) as caught:
        write_private_state(private / "runtime.json", b"secret", platform_name="darwin")

    rendered = "".join(traceback.format_exception(caught.value))
    assert private_marker not in rendered
    assert caught.value.__cause__ is None


def test_windows_new_temporary_file_is_dacl_protected_before_secret_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected: set[tuple[int, int]] = set()
    protected_paths: list[Path] = []

    def protect(path: Path) -> None:
        protected.add((path.stat().st_dev, path.stat().st_ino))
        protected_paths.append(path)

    def snapshot(path: Path) -> WindowsSecuritySnapshot:
        identity = (path.stat().st_dev, path.stat().st_ino)
        return WindowsSecuritySnapshot(
            owner_sid="S-1-user",
            current_user_sid="S-1-user",
            allowed_sids=frozenset({"S-1-user"}),
            trusted_sids=frozenset({"S-1-user"}),
            null_dacl=False,
            reparse_point=False,
            dacl_protected=identity in protected,
        )

    @contextmanager
    def guard(_directory: Path, *, platform_name: str) -> Generator[None]:
        assert platform_name == "win32"
        yield

    @contextmanager
    def state_lock(lock_path: Path) -> Generator[tuple[object, bool]]:
        lock_path.touch()
        yield lock_path, True

    monkeypatch.setattr(private_state, "_read_windows_security", snapshot)
    monkeypatch.setattr(private_state, "_protect_windows_dacl", protect, raising=False)
    monkeypatch.setattr(private_state, "_hold_private_namespace", guard, raising=False)
    monkeypatch.setattr(private_state, "_hold_windows_state_lock", state_lock, raising=False)
    monkeypatch.setattr(private_state, "_protect_windows_handle_dacl", protect, raising=False)
    monkeypatch.setattr(private_state, "_read_windows_handle_security", snapshot, raising=False)
    path = tmp_path / "moco" / "runtime-private" / "runtime.json"

    write_private_state(path, b"secret", platform_name="win32")

    assert path.parent in protected_paths
    assert path.parent / ".runtime-state.lock" in protected_paths
    assert any(candidate.name.startswith(f".{path.name}.") for candidate in protected_paths)
    validate_private_state_file(path, platform_name="win32")


def test_windows_existing_unsafe_state_lock_is_not_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    lock = private / ".runtime-state.lock"
    lock.touch()
    path = private / "runtime.json"

    def snapshot(_path: Path) -> WindowsSecuritySnapshot:
        return WindowsSecuritySnapshot(
            owner_sid="S-1-user",
            current_user_sid="S-1-user",
            allowed_sids=frozenset({"S-1-user"}),
            trusted_sids=frozenset({"S-1-user"}),
            null_dacl=False,
            reparse_point=False,
            dacl_protected=True,
        )

    @contextmanager
    def guard(_directory: Path, *, platform_name: str) -> Generator[None]:
        assert platform_name == "win32"
        yield

    @contextmanager
    def existing_lock(lock_path: Path) -> Generator[tuple[object, bool]]:
        assert lock_path == lock
        yield lock_path, False

    unsafe_lock = replace(snapshot(lock), dacl_protected=False)
    monkeypatch.setattr(private_state, "_read_windows_security", snapshot)
    monkeypatch.setattr(private_state, "_hold_private_namespace", guard, raising=False)
    monkeypatch.setattr(private_state, "_hold_windows_state_lock", existing_lock, raising=False)
    monkeypatch.setattr(
        private_state,
        "_read_windows_handle_security",
        lambda _handle: unsafe_lock,
        raising=False,
    )
    monkeypatch.setattr(
        private_state,
        "_protect_windows_handle_dacl",
        lambda _handle: pytest.fail("existing lock must not be repaired"),
        raising=False,
    )

    with pytest.raises(PrivateStateError, match="lock"):
        write_private_state(path, b"secret", platform_name="win32")

    assert not path.exists()


def test_windows_existing_unprotected_file_is_not_repaired_or_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    path = private / "runtime.json"
    path.write_bytes(b"existing")
    protected = {(private.stat().st_dev, private.stat().st_ino)}
    repair_calls: list[Path] = []

    def snapshot(candidate: Path) -> WindowsSecuritySnapshot:
        identity = (candidate.stat().st_dev, candidate.stat().st_ino)
        return WindowsSecuritySnapshot(
            owner_sid="S-1-user",
            current_user_sid="S-1-user",
            allowed_sids=frozenset({"S-1-user"}),
            trusted_sids=frozenset({"S-1-user"}),
            null_dacl=False,
            reparse_point=False,
            dacl_protected=identity in protected,
        )

    @contextmanager
    def guard(_directory: Path, *, platform_name: str) -> Generator[None]:
        assert platform_name == "win32"
        yield

    monkeypatch.setattr(private_state, "_read_windows_security", snapshot)
    monkeypatch.setattr(private_state, "_protect_windows_dacl", repair_calls.append, raising=False)
    monkeypatch.setattr(private_state, "_hold_private_namespace", guard, raising=False)
    monkeypatch.setattr(private_state, "_hold_private_state_lock", guard, raising=False)

    with pytest.raises(PrivateStateError, match="protected"):
        write_private_state(path, b"replacement", platform_name="win32")

    assert path.read_bytes() == b"existing"
    assert repair_calls == []


@requires_posix
def test_prepare_posix_directory_sets_owner_private_mode(tmp_path: Path) -> None:
    private = tmp_path / "moco" / "runtime-private"

    prepare_private_runtime_directory(private, platform_name="darwin")

    assert private.is_dir()
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert private.stat().st_uid == os.getuid()
    validate_private_runtime_directory(private, platform_name="darwin")


@requires_posix
def test_unsafe_existing_directory_is_not_repaired(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o755)

    with pytest.raises(PrivateStateError):
        prepare_private_runtime_directory(private, platform_name="darwin")

    assert stat.S_IMODE(private.stat().st_mode) == 0o755


@requires_posix
def test_symlink_directory_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    private = tmp_path / "runtime-private"
    private.symlink_to(target, target_is_directory=True)

    with pytest.raises(PrivateStateError, match="symbolic link"):
        prepare_private_runtime_directory(private, platform_name="darwin")

    assert private.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@requires_posix
def test_atomic_write_read_and_remove_use_private_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime-private" / "runtime.json"

    write_private_state(path, b"first", platform_name="darwin")
    write_private_state(path, b"second", platform_name="darwin")

    assert read_private_state(path, platform_name="darwin") == b"second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.getuid()
    assert not list(path.parent.glob(f".{path.name}.*"))
    remove_private_state(path, platform_name="darwin")
    remove_private_state(path, platform_name="darwin")
    assert not path.exists()


@requires_posix
def test_remove_only_deletes_the_file_identity_returned_by_write(tmp_path: Path) -> None:
    path = tmp_path / "runtime-private" / "runtime.json"
    owned = write_private_state(path, b"owned", platform_name="darwin")
    assert isinstance(owned, PrivateStateIdentity)
    assert str(path) not in repr(owned)

    replacement = write_private_state(path, b"replacement", platform_name="darwin")
    remove_private_state(path, expected_identity=owned, platform_name="darwin")

    assert read_private_state(path, platform_name="darwin") == b"replacement"
    remove_private_state(path, expected_identity=replacement, platform_name="darwin")
    assert not path.exists()


@requires_posix
def test_identity_cleanup_serializes_with_a_waiting_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "runtime.json"
    owned = write_private_state(path, b"owned", platform_name="darwin")
    cleanup_at_unlink = threading.Event()
    allow_cleanup = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    failures: list[BaseException] = []
    real_unlink = Path.unlink

    def paused_unlink(candidate: Path, missing_ok: bool = False) -> None:  # noqa: FBT002
        if candidate == path and threading.current_thread().name == "state-cleanup":
            cleanup_at_unlink.set()
            assert allow_cleanup.wait(2)
        real_unlink(candidate, missing_ok=missing_ok)

    def cleanup() -> None:
        try:
            remove_private_state(path, expected_identity=owned, platform_name="darwin")
        except BaseException as error:  # noqa: BLE001 - relay thread assertion
            failures.append(error)

    def write_replacement() -> None:
        writer_started.set()
        try:
            write_private_state(path, b"replacement", platform_name="darwin")
        except BaseException as error:  # noqa: BLE001 - relay thread assertion
            failures.append(error)
        finally:
            writer_done.set()

    monkeypatch.setattr(Path, "unlink", paused_unlink)
    cleanup_thread = threading.Thread(target=cleanup, name="state-cleanup")
    writer_thread = threading.Thread(target=write_replacement, name="state-writer")
    cleanup_thread.start()
    assert cleanup_at_unlink.wait(2)
    writer_thread.start()
    assert writer_started.wait(2)
    try:
        assert not writer_done.wait(0.1)
    finally:
        allow_cleanup.set()
        cleanup_thread.join(2)
        writer_thread.join(2)

    assert not cleanup_thread.is_alive()
    assert not writer_thread.is_alive()
    assert failures == []
    assert read_private_state(path, platform_name="darwin") == b"replacement"


@requires_posix
def test_post_replace_validation_failure_removes_owned_destination_and_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "runtime.json"
    original = private_state.validate_private_state_file

    def fail_final_validation(candidate: Path, *, platform_name: str | None = None) -> None:
        if candidate == path:
            msg = "synthetic final validation failure"
            raise PrivateStateError(msg)
        original(candidate, platform_name=platform_name)

    monkeypatch.setattr(private_state, "validate_private_state_file", fail_final_validation)

    with pytest.raises(PrivateStateError, match="synthetic"):
        write_private_state(path, b"RUNTIME_SECRET", platform_name="darwin")

    assert not path.exists()
    assert all(b"RUNTIME_SECRET" not in item.read_bytes() for item in path.parent.iterdir())


@requires_posix
def test_post_replace_directory_sync_failure_removes_owned_destination_and_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "runtime.json"

    def fail_sync(_directory: Path, *, platform_name: str) -> None:
        del platform_name
        msg = "synthetic sync failure"
        raise OSError(msg)

    monkeypatch.setattr(private_state, "_sync_directory", fail_sync)

    with pytest.raises(OSError, match="synthetic"):
        write_private_state(path, b"RUNTIME_SECRET", platform_name="darwin")

    assert not path.exists()
    assert all(b"RUNTIME_SECRET" not in item.read_bytes() for item in path.parent.iterdir())


@requires_posix
def test_post_replace_failure_finishes_cleanup_before_waiting_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-private" / "runtime.json"
    failure_reached = threading.Event()
    release_failure = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    failures: list[BaseException] = []
    original_sync = private_state._sync_directory  # noqa: SLF001 - failure seam

    def controlled_sync(directory: Path, *, platform_name: str) -> None:
        if threading.current_thread().name == "failing-writer":
            failure_reached.set()
            assert release_failure.wait(2)
            msg = "synthetic post-replace failure"
            raise OSError(msg)
        original_sync(directory, platform_name=platform_name)

    def failing_write() -> None:
        try:
            write_private_state(path, b"failed-generation", platform_name="darwin")
        except OSError:
            return
        failures.append(AssertionError("failing writer unexpectedly succeeded"))

    def replacement_write() -> None:
        writer_started.set()
        try:
            write_private_state(path, b"replacement", platform_name="darwin")
        except BaseException as error:  # noqa: BLE001 - relay thread assertion
            failures.append(error)
        finally:
            writer_done.set()

    monkeypatch.setattr(private_state, "_sync_directory", controlled_sync)
    failing_thread = threading.Thread(target=failing_write, name="failing-writer")
    replacement_thread = threading.Thread(target=replacement_write, name="replacement-writer")
    failing_thread.start()
    assert failure_reached.wait(2)
    replacement_thread.start()
    assert writer_started.wait(2)
    try:
        assert not writer_done.wait(0.1)
    finally:
        release_failure.set()
        failing_thread.join(2)
        replacement_thread.join(2)

    assert failures == []
    assert read_private_state(path, platform_name="darwin") == b"replacement"


@requires_posix
def test_unsafe_existing_file_is_not_repaired_or_replaced(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    path = private / "runtime.json"
    path.write_bytes(b"existing-secret")
    path.chmod(0o644)

    with pytest.raises(PrivateStateError):
        write_private_state(path, b"new-secret", platform_name="darwin")

    assert path.read_bytes() == b"existing-secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@requires_posix
def test_symlink_file_is_rejected_for_read_write_and_remove(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    target = tmp_path / "outside.json"
    target.write_bytes(b"outside-secret")
    target.chmod(0o600)
    path = private / "runtime.json"
    path.symlink_to(target)

    operations = (
        lambda: read_private_state(path, platform_name="darwin"),
        lambda: write_private_state(path, b"replacement", platform_name="darwin"),
        lambda: remove_private_state(path, platform_name="darwin"),
    )
    for operation in operations:
        with pytest.raises(PrivateStateError, match="symbolic link"):
            operation()

    assert path.is_symlink()
    assert target.read_bytes() == b"outside-secret"


@requires_posix
def test_write_revalidates_parent_before_persisting_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    path = private / "runtime.json"
    original = private_state.validate_private_runtime_directory
    calls = 0

    def invalidate_after_first_check(directory: Path, *, platform_name: str | None = None) -> None:
        nonlocal calls
        calls += 1
        original(directory, platform_name=platform_name)
        if calls == 1:
            directory.chmod(0o755)

    monkeypatch.setattr(
        private_state,
        "validate_private_runtime_directory",
        invalidate_after_first_check,
    )

    with pytest.raises(PrivateStateError):
        write_private_state(path, b"RUNTIME_SECRET", platform_name="darwin")

    assert calls >= 2
    assert not path.exists()
    assert all(b"RUNTIME_SECRET" not in item.read_bytes() for item in private.iterdir())


@requires_posix
def test_remove_rejects_unsafe_existing_file_without_deleting_it(tmp_path: Path) -> None:
    private = tmp_path / "runtime-private"
    private.mkdir(mode=0o700)
    path = private / "runtime.json"
    path.write_bytes(b"secret")
    path.chmod(0o644)

    with pytest.raises(PrivateStateError):
        remove_private_state(path, platform_name="darwin")

    assert path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires a real Windows DACL")
def test_new_windows_directory_and_file_have_valid_real_dacls(tmp_path: Path) -> None:
    from moco.runtime._windows_acl import read_windows_security  # noqa: PLC0415

    private = tmp_path / "runtime-private"
    prepare_private_runtime_directory(private, platform_name="win32")
    path = private / "runtime.json"
    identity = write_private_state(path, b"secret", platform_name="win32")

    validate_windows_security(read_windows_security(private))
    validate_windows_security(read_windows_security(path))
    validate_private_runtime_directory(private, platform_name="win32")
    validate_private_state_file(path, platform_name="win32")
    remove_private_state(path, expected_identity=identity, platform_name="win32")
