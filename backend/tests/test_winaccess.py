"""Windows folder access policy (``rootmind.winaccess``).

The pure helpers run everywhere. Everything that calls Windows runs only on
Windows, with the current account's SID (unelevated locally, elevated in CI):
the tests build folders and files in a temporary folder, give some of them DACLs
that only SYSTEM and Administrators can read, and check that the policy refuses
them under the UAC-filtered context even though the test account is an
administrator.

A file that the test process itself cannot open would make such a test pass for
the wrong reason. Where it matters, the DACL also grants read access to this
process's logon SID: the process can then open the file, an AuthZ context built
from the account's SID never contains that SID, and only the policy can refuse.
"""

from __future__ import annotations

import importlib
import ntpath
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from rootmind import winaccess
from rootmind.access import FolderNotAllowed
from rootmind.winaccess import (
    READ_ACCESS,
    AccessContextError,
    WindowsUserAccess,
    service_sid,
)

windows = pytest.mark.skipif(sys.platform != "win32", reason="calls Windows security APIs")

BS = "\\"
SE_GROUP_ENABLED = 0x4
SE_GROUP_LOGON_ID = 0xC0000000
ADMINS_ONLY = ("(A;;FA;;;SY)", "(A;;FA;;;BA)")  # SYSTEM and BUILTIN\Administrators
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


# --- Pure helpers (every platform) ---------------------------------------------


def test_module_imports_on_every_platform():
    module = importlib.import_module("rootmind.winaccess")
    for name in module.__all__:
        assert hasattr(module, name)


def test_service_sid_known_vector():
    assert service_sid("TrustedInstaller") == TRUSTED_INSTALLER_SID


def test_service_sid_ignores_case():
    sid = service_sid("RootMind-MCP-HTTP")
    assert sid == service_sid("rootmind-mcp-http")
    assert re.fullmatch(r"S-1-5-80(-\d+){5}", sid)


def test_policy_is_refused_where_windows_apis_are_missing(monkeypatch):
    def unavailable():
        raise OSError("the folder access policy is only available on Windows")

    monkeypatch.setattr(winaccess, "_win", unavailable)
    with pytest.raises(AccessContextError, match="only available on Windows"):
        WindowsUserAccess("S-1-5-21-1-2-3-1001")


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (BS * 2 + r"localhost\c$\x", winaccess.NETWORK_LOCATION),
        ("//server/share/docs", winaccess.NETWORK_LOCATION),
        (BS * 2 + r"?\UNC\server\share\docs", winaccess.NETWORK_LOCATION),
        (BS * 2 + r"?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\x", winaccess.DEVICE_PATH),
        (BS * 2 + r".\PhysicalDrive0", winaccess.DEVICE_PATH),
        (BS * 2 + r"?\C:\Users\me", winaccess.DEVICE_PATH),
        ("docs", winaccess.RELATIVE_PATH),
        ("C:docs", winaccess.RELATIVE_PATH),
        (r"\docs", winaccess.RELATIVE_PATH),
        ("", winaccess.NOT_A_LOCAL_FOLDER),
        ("C:\\docs\x00", winaccess.NOT_A_LOCAL_FOLDER),
        (r"C:\docs:stream", winaccess.NOT_A_LOCAL_FOLDER),
    ],
)
def test_path_text_is_refused_before_any_access(path, reason):
    with pytest.raises(FolderNotAllowed) as excinfo:
        winaccess._local_absolute(path)
    assert str(excinfo.value) == reason


def test_path_text_is_normalized():
    assert winaccess._local_absolute("C:/data/../docs/./notes") == r"C:\docs\notes"
    # A path object is accepted too (a Windows path, so the test also runs elsewhere).
    assert winaccess._local_absolute(PureWindowsPath("D:/")) == "D:" + BS


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        (BS * 2 + r"?\C:\Users\me", r"C:\Users\me"),
        (BS * 2 + "?" + BS + "D:" + BS, "D:" + BS),
        (BS * 2 + r"?\UNC\server\share\x", FolderNotAllowed(winaccess.NETWORK_LOCATION)),
        (BS * 2 + r"?\unc\server\share", FolderNotAllowed(winaccess.NETWORK_LOCATION)),
        (
            BS * 2 + r"?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\x",
            FolderNotAllowed(winaccess.NOT_A_LOCAL_FOLDER),
        ),
        (
            BS * 2 + r"?\Volume{01234567-89ab-cdef-0123-456789abcdef}\x",
            FolderNotAllowed(winaccess.NOT_A_LOCAL_FOLDER),
        ),
        (r"C:\no\prefix", FolderNotAllowed(winaccess.NOT_A_LOCAL_FOLDER)),
    ],
)
def test_final_paths_need_a_drive_letter(final, expected):
    if isinstance(expected, Exception):
        with pytest.raises(FolderNotAllowed) as excinfo:
            winaccess._dos_path(final)
        assert str(excinfo.value) == str(expected)
    else:
        assert winaccess._dos_path(final) == expected


def test_containment_is_component_wise():
    assert winaccess._at_or_below(r"c:\users\bob", r"c:\users\bob")
    assert winaccess._at_or_below(r"c:\users\bob\docs", r"c:\users\bob")
    assert winaccess._at_or_below(r"c:\users\bob\docs", "c:" + BS)
    assert not winaccess._at_or_below(r"c:\users\bob2", r"c:\users\bob")
    assert not winaccess._at_or_below(r"c:\users", r"c:\users\bob")


@pytest.mark.parametrize(
    ("sid", "filtered"),
    [
        ("S-1-5-32-544", True),  # Administrators
        ("S-1-5-32-551", True),  # Backup Operators
        ("S-1-5-114", True),  # Local account and member of Administrators
        ("S-1-5-21-1-2-3-512", True),  # Domain Admins
        ("S-1-5-21-1-2-3-519", True),  # Enterprise Admins
        ("S-1-5-21-1-2-3-498", True),  # Enterprise Read-only Domain Controllers
        ("S-1-5-21-1-2-3-513", False),  # Domain Users
        ("S-1-5-21-1-2-3-1001", False),  # a user
        ("S-1-5-32-545", False),  # Users
        ("S-1-5-11", False),  # Authenticated Users
        ("S-1-1-0", False),  # Everyone
    ],
)
def test_uac_filtered_groups(sid, filtered):
    assert winaccess._uac_filtered(sid) is filtered


def test_profile_paths_are_expanded_without_the_environment(monkeypatch):
    monkeypatch.setenv("SystemDrive", "Z:")
    monkeypatch.setenv("SystemRoot", r"Z:\Elsewhere")
    windows_dir = r"C:\Windows"
    assert winaccess._expand_profile_path(r"%SystemDrive%\Users\bob", windows_dir) == (
        r"C:\Users\bob"
    )
    assert winaccess._expand_profile_path(
        r"%systemroot%\ServiceProfiles\LocalService", windows_dir
    ) == (r"C:\Windows\ServiceProfiles\LocalService")
    assert winaccess._expand_profile_path(r"D:\Profiles\eve", windows_dir) == r"D:\Profiles\eve"
    assert winaccess._expand_profile_path(r"%HOMEDRIVE%\Users\x", windows_dir) is None


# --- Windows fixtures and helpers ---------------------------------------------


def _token_groups() -> list[tuple[str, int]]:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
    finally:
        token.Close()
    return [(win32security.ConvertSidToStringSid(sid), attrs & 0xFFFFFFFF) for sid, attrs in groups]


def _descriptor_of(path: Path) -> bytes:
    """Self-relative security descriptor (owner, group, DACL) of a file or folder.

    GetFileSecurity asks only for READ_CONTROL, which the owner always gets.
    CreateFileW would also ask for SYNCHRONIZE, which an admins-only DACL does
    not grant to the unelevated test account.
    """
    import win32security

    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.GROUP_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
    )
    return bytes(memoryview(win32security.GetFileSecurity(str(path), info)))


def _sd(sddl: str) -> bytes:
    return winaccess._sd_from_sddl(sddl)


@pytest.fixture
def user_sid() -> str:
    return winaccess.current_user_sid()


@pytest.fixture
def is_admin() -> bool:
    """Whether the test account is a member of Administrators (enabled or deny-only)."""
    return any(sid == winaccess.ADMINISTRATORS for sid, _attrs in _token_groups())


@pytest.fixture
def policy(user_sid):
    """The policy as the service builds it on a computer with UAC on."""
    with WindowsUserAccess(user_sid, uac_filter=True) as access:
        yield access


@pytest.fixture
def unfiltered(user_sid):
    """The same account without the UAC filter: what an elevated process could read."""
    with WindowsUserAccess(user_sid, uac_filter=False) as access:
        yield access


@pytest.fixture
def acl():
    """``acl(path, *aces)`` sets a protected DACL; the inherited one comes back afterwards."""
    import win32security

    changed: list[Path] = []

    def set_dacl(path: Path, *aces: str) -> None:
        _protect(path, *aces)
        changed.append(path)

    yield set_dacl
    for path in reversed(changed):
        # The test account (or, when elevated, Administrators) owns the file and
        # may therefore always rewrite its DACL.
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.UNPROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            win32security.ACL(),
            None,
        )


@pytest.fixture
def process_only_sid(policy) -> str:
    """A SID that lets this process open a file, but that the policy never grants through.

    The logon SID is in every logon session's token and never in a context built
    from an account SID.
    """
    granting = {text for text, _sid, attrs in policy._context_groups() if attrs & SE_GROUP_ENABLED}
    enabled = [(sid, attrs) for sid, attrs in _token_groups() if attrs & SE_GROUP_ENABLED]
    for sid, attrs in enabled:
        if attrs & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
            return sid
    for sid, _attrs in enabled:
        if sid not in granting and not winaccess._uac_filtered(sid):
            return sid
    raise AssertionError("the process token has no SID that the access check lacks")


def _protect(path: Path, *aces: str) -> None:
    """Give ``path`` a protected DACL made of these SDDL ACEs."""
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:P" + "".join(aces), win32security.SDDL_REVISION_1
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def _set_owner(path: Path, sid: str) -> None:
    import win32security

    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION,
        win32security.ConvertStringSidToSid(sid),
        None,
        None,
        None,
    )


@pytest.fixture
def workspace(tmp_path, user_sid) -> Path:
    """``tmp_path`` with the owner and ACL of a normal profile folder.

    pytest creates its folders with mode 0o700, which Python turns into a DACL of
    SYSTEM, Administrators and OWNER RIGHTS, and an elevated process (as in CI)
    creates files owned by Administrators. Under the filtered context such files
    are correctly unreadable, so the tests use what a profile folder has: owner
    and full control for the account, plus SYSTEM and Administrators.
    """
    _set_owner(tmp_path, user_sid)
    _protect(tmp_path, "(A;OICI;FA;;;SY)", "(A;OICI;FA;;;BA)", f"(A;OICI;FA;;;{user_sid})")
    return tmp_path


def _read_ace(sid: str) -> str:
    return f"(A;;FR;;;{sid})"


@pytest.fixture
def root_dir(workspace) -> Path:
    folder = workspace / "root"
    folder.mkdir()
    return folder


def _known(folder_id: str) -> str:
    return winaccess._known_folder(folder_id)


# --- Account, SIDs and the UAC filter ----------------------------------------


@windows
def test_current_user_sid_matches_the_process_token(user_sid):
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        token.Close()
    assert user_sid == win32security.ConvertSidToStringSid(sid)


@windows
def test_service_sid_matches_windows():
    import win32security

    sid, _domain, _kind = win32security.LookupAccountName(None, r"NT SERVICE\TrustedInstaller")
    assert service_sid("TrustedInstaller") == win32security.ConvertSidToStringSid(sid)


@windows
def test_uac_enabled_reads_the_policy(monkeypatch):
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, winaccess._UAC_POLICY_KEY) as key:
            expected = winreg.QueryValueEx(key, "EnableLUA")[0] != 0
    except OSError:
        expected = True
    assert winaccess.uac_enabled() is expected

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(winreg, "OpenKey", missing)
    assert winaccess.uac_enabled() is True


@windows
def test_default_filter_follows_the_uac_policy(monkeypatch, user_sid, is_admin):
    monkeypatch.setattr(winaccess, "uac_enabled", lambda: False)
    with WindowsUserAccess(user_sid) as access:
        assert access.filter_mode == "off"
    monkeypatch.setattr(winaccess, "uac_enabled", lambda: True)
    with WindowsUserAccess(user_sid) as access:
        assert access.filter_mode == ("deny-only" if is_admin else "none")


@windows
def test_filtered_groups_are_deny_only(policy, unfiltered, user_sid, is_admin):
    admins_only = _sd("O:SYG:SYD:P" + "".join(ADMINS_ONLY))
    assert policy._allowed(admins_only, READ_ACCESS) is False
    assert unfiltered._allowed(admins_only, READ_ACCESS) is is_admin

    assert policy._allowed(_sd(f"O:SYG:SYD:P(A;;FR;;;{user_sid})"), READ_ACCESS)
    assert policy._allowed(_sd("O:SYG:SYD:P(A;;FR;;;WD)"), READ_ACCESS)
    assert policy._allowed(_sd("O:SYG:SYD:P(A;;FR;;;AU)"), READ_ACCESS)
    # Every interactive logon has INTERACTIVE (C:\Users\Public grants it).
    assert policy._allowed(_sd("O:SYG:SYD:P(A;;FR;;;IU)"), READ_ACCESS)
    assert unfiltered._allowed(_sd("O:SYG:SYD:P(A;;FR;;;IU)"), READ_ACCESS)
    if is_admin:
        assert policy.filter_mode == "deny-only"
        # As in a standard token, a deny-only group still matches deny ACEs ...
        assert not policy._allowed(_sd("O:SYG:SYD:P(D;;FA;;;BA)(A;;FA;;;WD)"), READ_ACCESS)
        # ... and the standard token's deny-only "local administrator" group is there too.
        assert not policy._allowed(_sd("O:SYG:SYD:P(A;;FA;;;S-1-5-114)"), READ_ACCESS)
    else:
        assert policy.filter_mode == "none"


@windows
def test_non_interactive_logon_groups_never_grant(monkeypatch, user_sid):
    """A context built through an S4U logon may carry NETWORK; the user's own token does not."""
    original = WindowsUserAccess._context_groups
    calls = []

    def with_network(self):
        groups = original(self)
        calls.append(len(groups))
        if len(calls) == 1 and all(text != "S-1-5-2" for text, _sid, _attrs in groups):
            groups.append(("S-1-5-2", winaccess._sid_bytes("S-1-5-2"), 0x7))
        return groups

    monkeypatch.setattr(WindowsUserAccess, "_context_groups", with_network)
    with WindowsUserAccess(user_sid, uac_filter=True) as access:
        network = [attrs for text, _sid, attrs in original(access) if text == "S-1-5-2"]
        assert network == [0x10]  # SE_GROUP_USE_FOR_DENY_ONLY
        assert not access._allowed(_sd("O:SYG:SYD:P(A;;FR;;;NU)"), READ_ACCESS)


@windows
@pytest.mark.parametrize(
    "sid",
    [
        "not-a-sid",
        "",
        "S-1-5-21-1-2-3-4",  # well formed, but no such account
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # a group
        TRUSTED_INSTALLER_SID,  # a service
    ],
)
def test_invalid_sid_raises_access_context_error(sid):
    with pytest.raises(AccessContextError):
        WindowsUserAccess(sid)


@windows
def test_failing_context_raises_access_context_error(monkeypatch, user_sid):
    monkeypatch.setattr(winaccess._win(), "AuthzInitializeContextFromSid", lambda *args: 0)
    with pytest.raises(AccessContextError, match="cannot build the access check"):
        WindowsUserAccess(user_sid)


@windows
def test_filter_that_does_not_take_effect_raises(monkeypatch, user_sid, is_admin):
    monkeypatch.setattr(WindowsUserAccess, "_replace_groups", lambda self, groups: True)
    if is_admin:
        with pytest.raises(AccessContextError, match="User Account Control"):
            WindowsUserAccess(user_sid, uac_filter=True)
    else:
        with WindowsUserAccess(user_sid, uac_filter=True) as access:
            assert access.filter_mode == "none"


@windows
def test_falls_back_to_removing_groups(monkeypatch, user_sid, is_admin):
    original = WindowsUserAccess._replace_groups
    attempts = []

    def reject_first(self, groups):
        attempts.append(groups)
        return False if len(attempts) == 1 else original(self, groups)

    monkeypatch.setattr(WindowsUserAccess, "_replace_groups", reject_first)
    if not is_admin:
        # Without filtered groups there is no fallback: a rejected group list fails closed.
        with pytest.raises(AccessContextError):
            WindowsUserAccess(user_sid, uac_filter=True)
        return
    with WindowsUserAccess(user_sid, uac_filter=True) as access:
        assert access.filter_mode == "removed"
        present = {text for text, _sid, _attrs in access._context_groups()}
        assert winaccess.ADMINISTRATORS not in present
        assert not access._allowed(_sd("O:SYG:SYD:P" + "".join(ADMINS_ONLY)), READ_ACCESS)


# --- Files -------------------------------------------------------------------


@windows
def test_own_file_with_inherited_dacl_is_readable(policy, root_dir, user_sid):
    note = root_dir / "note.txt"
    note.write_bytes(b"hello")
    _set_owner(note, user_sid)  # an elevated process (CI) creates files owned by Administrators
    root = policy.check_root(root_dir)
    with policy.open_file(note, root) as file:
        assert file.read() == b"hello"


@windows
@pytest.mark.parametrize("openable", [False, True], ids=["admins-only", "process-can-open"])
def test_file_only_admins_can_read_is_denied(
    policy, unfiltered, root_dir, acl, process_only_sid, is_admin, openable
):
    secret = root_dir / "secret.txt"
    secret.write_bytes(b"secret")
    root = policy.check_root(root_dir)
    acl(secret, *ADMINS_ONLY, *([_read_ace(process_only_sid)] if openable else []))

    descriptor = _descriptor_of(secret)
    assert policy._allowed(descriptor, READ_ACCESS) is False
    assert unfiltered._allowed(descriptor, READ_ACCESS) is is_admin
    if openable:
        with open(secret, "rb") as file:  # the process itself may read it ...
            assert file.read() == b"secret"
    with pytest.raises(PermissionError):  # ... but the account may not without elevation
        policy.open_file(secret, root)
    if openable and is_admin:
        with unfiltered.open_file(secret, unfiltered.check_root(root_dir)) as file:
            assert file.read() == b"secret"


@windows
@pytest.mark.parametrize("openable", [False, True], ids=["admins-only", "process-can-open"])
def test_hard_link_to_protected_file_is_denied(
    policy, workspace, root_dir, acl, process_only_sid, openable
):
    outside = workspace / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_bytes(b"secret")
    link = root_dir / "innocent.txt"
    os.link(target, link)
    acl(target, *ADMINS_ONLY, *([_read_ace(process_only_sid)] if openable else []))
    root = policy.check_root(root_dir)

    assert policy._allowed(_descriptor_of(link), READ_ACCESS) is False
    with pytest.raises(PermissionError):
        policy.open_file(link, root)


@windows
def test_junction_inside_the_root_pointing_outside_is_refused(policy, workspace, root_dir):
    import _winapi

    outside = workspace / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    junction = root_dir / "shortcut"
    _winapi.CreateJunction(str(outside), str(junction))  # same as mklink /J, no admin needed
    root = policy.check_root(root_dir)

    assert policy.may_descend(junction, root) is False
    assert policy.may_descend(outside, root) is False
    with pytest.raises(ValueError, match="outside the indexed folder"):
        policy.open_file(junction / "secret.txt", root)


@windows
def test_open_file_outside_the_root_raises_value_error(policy, workspace, root_dir):
    stray = workspace / "stray.txt"
    stray.write_bytes(b"x")
    with pytest.raises(ValueError):
        policy.open_file(stray, policy.check_root(root_dir))


@windows
def test_open_file_never_follows_a_symbolic_link(policy, root_dir, monkeypatch):
    link = root_dir / "link.txt"
    link.write_bytes(b"x")
    root = policy.check_root(root_dir)
    _pretend_symbolic_link(monkeypatch, link)
    with pytest.raises(PermissionError, match="symbolic links"):
        policy.open_file(link, root)


@windows
def test_open_file_missing_raises_file_not_found(policy, root_dir):
    with pytest.raises(FileNotFoundError):
        policy.open_file(root_dir / "gone.txt", policy.check_root(root_dir))


# --- check_root --------------------------------------------------------------


def _profiles_root() -> str:
    return _known(winaccess.FOLDERID_USER_PROFILES)


def _program_data_subfolder() -> str:
    program_data = Path(_known(winaccess.FOLDERID_PROGRAM_DATA))
    preferred = program_data / "Microsoft"
    if preferred.is_dir():
        return str(preferred)
    return str(next(entry for entry in program_data.iterdir() if entry.is_dir()))


REFUSED_LOCATIONS = {
    "unc-share": (lambda: BS * 2 + r"localhost\c$\x", winaccess.NETWORK_LOCATION),
    "drive-root": (
        lambda: ntpath.splitdrive(_known(winaccess.FOLDERID_WINDOWS))[0] + BS,
        winaccess.DRIVE_ROOT,
    ),
    "windows": (lambda: _known(winaccess.FOLDERID_WINDOWS), winaccess.SYSTEM_FOLDER),
    "windows-subfolder": (
        lambda: str(Path(_known(winaccess.FOLDERID_WINDOWS)) / "System32"),
        winaccess.SYSTEM_FOLDER,
    ),
    "program-files": (lambda: _known(winaccess.FOLDERID_PROGRAM_FILES), winaccess.SYSTEM_FOLDER),
    "program-files-x86": (
        lambda: _known(winaccess.FOLDERID_PROGRAM_FILES_X86),
        winaccess.SYSTEM_FOLDER,
    ),
    "programdata-subfolder": (_program_data_subfolder, winaccess.SYSTEM_FOLDER),
    "profiles-root": (_profiles_root, winaccess.PROFILES_ROOT),
}


@windows
@pytest.mark.parametrize("name", list(REFUSED_LOCATIONS))
def test_check_root_refuses_system_locations(policy, name):
    path, reason = REFUSED_LOCATIONS[name]
    with pytest.raises(FolderNotAllowed) as excinfo:
        policy.check_root(path())
    assert str(excinfo.value) == reason


@windows
def test_unc_path_is_refused_without_touching_it(policy, monkeypatch):
    touched = []
    monkeypatch.setattr(winaccess, "_open_handle", lambda *args: touched.append(args))
    monkeypatch.setattr(winaccess, "_drive_type", lambda *args: touched.append(args))
    with pytest.raises(FolderNotAllowed, match="network"):
        policy.check_root(BS * 2 + r"localhost\c$\x")
    assert touched == []


@windows
def test_check_root_refuses_other_users_profiles(policy, user_sid, workspace, monkeypatch):
    mine = workspace / "mine"
    other = workspace / "other"
    sibling = workspace / "other2"
    expanded = workspace / "expanded"
    for folder in (mine, other / "Documents", sibling, expanded):
        folder.mkdir(parents=True)
    # ProfileList stores most paths as %SystemDrive%\Users\<name>.
    drive, rest = ntpath.splitdrive(str(expanded))
    system_drive = ntpath.splitdrive(_known(winaccess.FOLDERID_WINDOWS))[0]
    raw_expanded = (
        "%SystemDrive%" + rest if drive.upper() == system_drive.upper() else str(expanded)
    )
    monkeypatch.setattr(
        winaccess,
        "_profile_list",
        lambda: {
            f"{user_sid}.bak": str(mine),
            "S-1-5-21-1-2-3-1001": str(other),
            "S-1-5-21-1-2-3-1002": raw_expanded,
        },
    )

    for refused in (other, other / "Documents", expanded):
        with pytest.raises(FolderNotAllowed) as excinfo:
            policy.check_root(refused)
        assert str(excinfo.value) == winaccess.OTHER_PROFILE
    assert policy.check_root(sibling) == sibling.resolve()
    assert policy.check_root(mine) == mine.resolve()


@windows
def test_check_root_refuses_the_real_profiles_of_other_accounts(policy, user_sid):
    own = user_sid.upper()
    windows_dir = _known(winaccess.FOLDERID_WINDOWS)
    for sid, raw in winaccess._profile_list().items():
        path = winaccess._expand_profile_path(raw, windows_dir)
        if sid.upper().removesuffix(".BAK") == own or path is None or not os.path.isdir(path):
            continue
        with pytest.raises(FolderNotAllowed) as excinfo:
            policy.check_root(path)
        assert str(excinfo.value) in (winaccess.OTHER_PROFILE, winaccess.SYSTEM_FOLDER)


@windows
def test_check_root_fails_closed_without_the_profile_list(policy, root_dir, monkeypatch):
    def unreadable():
        raise PermissionError("denied")

    monkeypatch.setattr(winaccess, "_profile_list", unreadable)
    with pytest.raises(FolderNotAllowed, match="profiles"):
        policy.check_root(root_dir)


@windows
def test_check_root_refuses_a_folder_the_account_cannot_list(
    policy, root_dir, acl, process_only_sid
):
    locked = root_dir / "locked"
    locked.mkdir()
    acl(locked, *ADMINS_ONLY, _read_ace(process_only_sid))
    with pytest.raises(FolderNotAllowed) as excinfo:
        policy.check_root(locked)
    assert str(excinfo.value) == winaccess.NOT_READABLE


@windows
def test_check_root_returns_the_final_path(policy, workspace, root_dir):
    import _winapi

    junction = workspace / "via-junction"
    _winapi.CreateJunction(str(root_dir), str(junction))
    assert policy.check_root(junction) == root_dir.resolve()
    assert policy.check_root(str(root_dir).replace(BS, "/")) == root_dir.resolve()
    assert policy.check_root(root_dir / "sub" / "..") == root_dir.resolve()


@windows
def test_check_root_on_missing_folders_and_files(policy, root_dir):
    with pytest.raises(FileNotFoundError):
        policy.check_root(root_dir / "missing")
    note = root_dir / "note.txt"
    note.write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        policy.check_root(note)


def _pretend_symbolic_link(monkeypatch, link: Path) -> None:
    """Make lstat report ``link`` as a symbolic link (creating one needs a privilege)."""
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if os.path.normcase(os.fspath(path)) != os.path.normcase(str(link)):
            return result
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_file_attributes=result.st_file_attributes | 0x400,
            st_reparse_tag=0xA000000C,  # IO_REPARSE_TAG_SYMLINK
        )

    monkeypatch.setattr(winaccess.os, "lstat", lstat)


@windows
def test_check_root_refuses_a_path_through_a_symbolic_link(policy, root_dir, monkeypatch):
    nested = root_dir / "linked" / "docs"
    nested.mkdir(parents=True)
    _pretend_symbolic_link(monkeypatch, root_dir / "linked")
    with pytest.raises(FolderNotAllowed) as excinfo:
        policy.check_root(nested)
    assert str(excinfo.value) == winaccess.SYMBOLIC_LINK


@windows
@pytest.mark.parametrize(
    ("drive_type", "reason"),
    [
        (4, winaccess.NETWORK_LOCATION),  # DRIVE_REMOTE (a mapped network drive)
        (1, winaccess.DRIVE_NOT_AVAILABLE),  # DRIVE_NO_ROOT_DIR
        (0, winaccess.DRIVE_NOT_AVAILABLE),  # DRIVE_UNKNOWN
        (6, winaccess.DRIVE_NOT_AVAILABLE),  # DRIVE_RAMDISK
    ],
)
def test_check_root_refuses_other_drive_types(policy, root_dir, monkeypatch, drive_type, reason):
    monkeypatch.setattr(winaccess, "_drive_type", lambda root: drive_type)
    with pytest.raises(FolderNotAllowed) as excinfo:
        policy.check_root(root_dir)
    assert str(excinfo.value) == reason


@windows
@pytest.mark.parametrize(
    ("final", "reason"),
    [
        (OSError(None, "no DOS name", None, 3), winaccess.NOT_A_LOCAL_FOLDER),
        (BS * 2 + r"?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\x", winaccess.NOT_A_LOCAL_FOLDER),
        (BS * 2 + r"?\UNC\server\share\x", winaccess.NETWORK_LOCATION),
    ],
)
def test_check_root_refuses_final_paths_without_a_drive_letter(
    policy, root_dir, monkeypatch, final, reason
):
    def final_path_name(handle):
        if isinstance(final, Exception):
            raise final
        return final

    monkeypatch.setattr(winaccess, "_final_path_name", final_path_name)
    with pytest.raises(FolderNotAllowed) as excinfo:
        policy.check_root(root_dir)
    assert str(excinfo.value) == reason


# --- may_descend ---------------------------------------------------------------


@windows
def test_may_descend(policy, root_dir, acl, process_only_sid, monkeypatch):
    (root_dir / "plain").mkdir()
    (root_dir / "locked").mkdir()
    (root_dir / "note.txt").write_bytes(b"x")
    acl(root_dir / "locked", *ADMINS_ONLY, _read_ace(process_only_sid))
    root = policy.check_root(root_dir)

    assert policy.may_descend(root_dir / "plain", root) is True
    assert policy.may_descend(root_dir, root) is True
    assert policy.may_descend(root_dir / "locked", root) is False
    assert policy.may_descend(root_dir / "missing", root) is False
    assert policy.may_descend(root_dir / "note.txt", root) is False

    _pretend_symbolic_link(monkeypatch, root_dir / "plain")
    assert policy.may_descend(root_dir / "plain", root) is False


# --- Cache, threads, lifetime ------------------------------------------------


@windows
def test_second_check_of_an_identical_descriptor_hits_the_cache(user_sid, root_dir, monkeypatch):
    (root_dir / "a.txt").write_bytes(b"a")
    (root_dir / "b.txt").write_bytes(b"b")
    assert _descriptor_of(root_dir / "a.txt") == _descriptor_of(root_dir / "b.txt")
    with WindowsUserAccess(user_sid, uac_filter=True) as access:
        calls: list[int] = []
        original = access._check_uncached

        def spy(descriptor, desired):
            calls.append(desired)
            return original(descriptor, desired)

        monkeypatch.setattr(access, "_check_uncached", spy)
        root = access.check_root(root_dir)
        for name in ("a.txt", "b.txt", "a.txt"):
            with access.open_file(root_dir / name, root) as file:
                assert file.read() == name[0].encode()
        assert calls.count(READ_ACCESS) == 1
        assert calls.count(winaccess.LIST_ACCESS) == 1


@windows
def test_cache_is_bounded(policy):
    policy._cache_limit = 2
    for sddl in ("(A;;FR;;;WD)", "(A;;FR;;;AU)", "(A;;FR;;;BU)"):
        assert policy._allowed(_sd("O:SYG:SYD:P" + sddl), READ_ACCESS)
    assert len(policy._cache) == 2


@windows
def test_concurrent_reads(policy, root_dir):
    for index in range(32):
        (root_dir / f"{index}.txt").write_text(str(index), encoding="utf-8")
    root = policy.check_root(root_dir)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def read(index: int) -> str:
        try:
            with policy.open_file(root_dir / f"{index}.txt", root) as file:
                return file.read().decode()
        except BaseException as exc:
            with lock:
                errors.append(exc)
            raise

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(32)))
    assert errors == []
    assert results == [str(index) for index in range(32)]


@windows
def test_closed_policy_denies_everything(user_sid, root_dir):
    (root_dir / "note.txt").write_bytes(b"x")
    access = WindowsUserAccess(user_sid, uac_filter=True)
    root = access.check_root(root_dir)
    access.close()
    access.close()
    with pytest.raises(PermissionError):
        access.open_file(root_dir / "note.txt", root)
    assert access.may_descend(root_dir, root) is False
    with pytest.raises(FolderNotAllowed):
        access.check_root(root_dir)


# --- With the local connector ----------------------------------------------------


@windows
def test_local_connector_reads_only_what_the_account_may_read(
    policy, root_dir, acl, process_only_sid
):
    from rootmind.connectors import LocalConnector

    (root_dir / "open.txt").write_bytes(b"visible")
    (root_dir / "locked.txt").write_bytes(b"secret")
    hidden = root_dir / "hidden"
    hidden.mkdir()
    (hidden / "inner.txt").write_bytes(b"secret")
    acl(root_dir / "locked.txt", *ADMINS_ONLY, _read_ace(process_only_sid))
    acl(hidden, *ADMINS_ONLY, _read_ace(process_only_sid))

    connector = LocalConnector("docs", root_dir, access=policy)
    listed = {info.path for info in connector.list_files_recursive()}
    assert listed == {"open.txt", "locked.txt"}
    assert connector.read_file("open.txt") == b"visible"
    with pytest.raises(PermissionError):
        connector.read_file("locked.txt")
    with pytest.raises(PermissionError):
        connector.get_metadata("locked.txt")
