"""Windows folder access policy for the background service.

The ``RootMind-Backend`` service runs as LocalSystem, which can read almost
every file on the computer. :class:`WindowsUserAccess` confines the indexer to
what the account that installed the service can read **without elevation**:

- It builds an AuthZ client context from that account's SID. When User Account
  Control is on, the groups that UAC filters out of an administrator's standard
  token (Administrators and similar) become deny-only in that context, exactly
  as in the account's normal, unelevated token.
- :meth:`WindowsUserAccess.check_root` refuses network locations, device paths,
  drive roots, the Windows, program and ProgramData folders, the folder that
  holds all user profiles, other users' profiles, and folders the account
  cannot list.
- :meth:`WindowsUserAccess.may_descend` and :meth:`WindowsUserAccess.open_file`
  check every subfolder and file on its open handle: the final path must lie
  under the indexed folder, and the account must be allowed to list or read it.
  A hard link shares its file's security descriptor, so it gains nothing.

Every check fails closed. A context that cannot be built raises
:class:`AccessContextError`, and the service then refuses to start. Symbolic
links are never followed: one can point to a network share, and opening it as
LocalSystem would authenticate to that share with the computer's account.

The module uses ctypes only and imports on every platform; the policy itself
works only on Windows. The command line and the MCP servers do not use it (see
:mod:`rootmind.access`).
"""

from __future__ import annotations

import collections
import ctypes
import errno
import hashlib
import logging
import ntpath
import os
import re
import stat
import struct
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

from .access import FolderNotAllowed

logger = logging.getLogger(__name__)

__all__ = [
    "AccessContextError",
    "WindowsUserAccess",
    "current_user_sid",
    "service_sid",
    "uac_enabled",
]

# Refusal reasons. The API shows them after "Cannot use <path>: ".
NETWORK_LOCATION = "network drives and shares are not available to the background service"
DEVICE_PATH = (
    "device paths are not available to the background service; "
    "use a folder path such as D:\\Documents"
)
NOT_A_LOCAL_FOLDER = (
    "only folders on a local drive with a drive letter are available to the background service"
)
RELATIVE_PATH = "the background service needs a full folder path, such as D:\\Documents"
DRIVE_NOT_AVAILABLE = "this drive is not available to the background service"
SYMBOLIC_LINK = (
    "folders reached through a symbolic link are not available to the background service; "
    "use the folder the link points to"
)
DRIVE_ROOT = "a whole drive cannot be indexed by the background service; choose a folder on it"
SYSTEM_FOLDER = (
    "the Windows, program and ProgramData folders are not available to the background service"
)
PROFILES_ROOT = "the folder that holds all user profiles is not available to the background service"
OTHER_PROFILE = "other users' folders are not available to the background service"
NOT_READABLE = "your account cannot open this folder without administrator rights"
PROFILES_UNREADABLE = "the list of user profiles could not be read"
# Reason of a PermissionError for a file that is a link.
LINK_NOT_FOLLOWED = "symbolic links are not followed by the background service"

# Access rights checked with AuthZ.
FILE_READ_DATA = 0x0001
FILE_LIST_DIRECTORY = 0x0001
FILE_TRAVERSE = 0x0020
LIST_ACCESS = FILE_LIST_DIRECTORY | FILE_TRAVERSE
READ_ACCESS = FILE_READ_DATA

# Size of the access-result cache (security descriptors, see _allowed).
CACHE_SIZE = 4096

# Groups that UAC makes deny-only in an administrator's standard token. A
# context built from the SID alone would let them grant access, and only
# administrators may install the service, so without this the check would
# allow nearly everything. Extra entries can only make the check stricter.
ADMINISTRATORS = "S-1-5-32-544"
LOCAL_ADMIN_ACCOUNT = "S-1-5-114"  # "Local account and member of Administrators group"
_UAC_FILTERED_SIDS = frozenset(
    {
        ADMINISTRATORS,
        "S-1-5-32-547",  # Power Users
        "S-1-5-32-548",  # Account Operators
        "S-1-5-32-549",  # Server Operators
        "S-1-5-32-550",  # Print Operators
        "S-1-5-32-551",  # Backup Operators
        "S-1-5-32-553",  # RAS and IAS Servers
        "S-1-5-32-554",  # Pre-Windows 2000 Compatible Access
        "S-1-5-32-556",  # Network Configuration Operators
        "S-1-5-32-569",  # Cryptographic Operators
        LOCAL_ADMIN_ACCOUNT,
    }
)
# Domain groups, by relative ID under S-1-5-21-<domain>.
_UAC_FILTERED_DOMAIN_RIDS = frozenset(
    {
        498,  # Enterprise Read-only Domain Controllers
        512,  # Domain Admins
        516,  # Domain Controllers
        517,  # Cert Publishers
        518,  # Schema Admins
        519,  # Enterprise Admins
        520,  # Group Policy Creator Owners
        521,  # Read-only Domain Controllers
    }
)

# Groups that every interactive logon has. A context built from the SID alone
# lacks them, so folders shared through them (such as C:\Users\Public, which
# grants INTERACTIVE) would be refused although the account can read them.
_LOGON_GROUPS = (
    "S-1-5-4",  # INTERACTIVE
    "S-1-2-0",  # LOCAL
)

# Logon-type groups that a context built through an S4U logon may carry, but
# the account's interactive token does not. They become deny-only.
_NON_INTERACTIVE_GROUPS = frozenset(
    {
        "S-1-5-2",  # NETWORK
        "S-1-5-3",  # BATCH
        "S-1-5-6",  # SERVICE
    }
)

_UAC_POLICY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
_PROFILE_LIST_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"

# Known folders (SHGetKnownFolderPath); never taken from environment variables.
FOLDERID_WINDOWS = "F38BF404-1D43-42F2-9305-67DE0B28FC23"
FOLDERID_PROGRAM_FILES = "905E63B6-C1BF-494E-B29C-65B732D3D21A"
FOLDERID_PROGRAM_FILES_X86 = "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E"
FOLDERID_PROGRAM_DATA = "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97"
FOLDERID_USER_PROFILES = "0762D272-C50A-4BB0-A382-697DCD729B80"

# Win32 constants.
_GENERIC_READ = 0x80000000  # includes READ_CONTROL, needed for the access check
_READ_CONTROL = 0x00020000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a folder
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_TAG_INFO = 9  # FILE_INFO_BY_HANDLE_CLASS
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # junctions and volume mount points
_NAME_SURROGATE_BIT = 0x20000000  # IsReparseTagNameSurrogate
_FILE_NAME_NORMALIZED = 0x0
_VOLUME_NAME_DOS = 0x0
_SE_FILE_OBJECT = 1
_OWNER_GROUP_DACL = 0x1 | 0x2 | 0x4  # OWNER, GROUP and DACL_SECURITY_INFORMATION
_SE_SELF_RELATIVE = 0x8000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS
_SID_TYPE_USER = 1  # SID_NAME_USE
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_LOCAL_DRIVE_TYPES = frozenset({_DRIVE_REMOVABLE, _DRIVE_FIXED, _DRIVE_CDROM})
_AUTHZ_RM_FLAG_NO_AUDIT = 0x1
_AUTHZ_CONTEXT_INFO_GROUPS_SIDS = 2  # AuthzContextInfoGroupsSids
_AUTHZ_SID_OPERATION_REPLACE_ALL = 1
_SE_GROUP_ENABLED = 0x4
_SE_GROUP_USE_FOR_DENY_ONLY = 0x10

_DOS_FINAL_PATH = re.compile(r"\\\\\?\\([A-Za-z]:\\.*)", re.DOTALL)
_DRIVE_ROOT_PATH = re.compile(r"[A-Za-z]:\\")

# ctypes types with fixed sizes, so the structures below also import on other
# platforms (ctypes.wintypes is only used on Windows).
_DWORD = ctypes.c_uint32


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", _DWORD), ("HighPart", ctypes.c_int32)]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", _DWORD)]


class _TokenGroupsHeader(ctypes.Structure):
    # TOKEN_GROUPS: the Groups array follows, aligned like SID_AND_ATTRIBUTES.
    _fields_ = [("GroupCount", _DWORD), ("Groups", _SidAndAttributes * 0)]


class _AuthzAccessRequest(ctypes.Structure):
    _fields_ = [
        ("DesiredAccess", _DWORD),
        ("PrincipalSelfSid", ctypes.c_void_p),
        ("ObjectTypeList", ctypes.c_void_p),
        ("ObjectTypeListLength", _DWORD),
        ("OptionalArguments", ctypes.c_void_p),
    ]


class _AuthzAccessReply(ctypes.Structure):
    _fields_ = [
        ("ResultListLength", _DWORD),
        ("GrantedAccessMask", ctypes.POINTER(_DWORD)),
        ("SaclEvaluationResults", ctypes.POINTER(_DWORD)),
        ("Error", ctypes.POINTER(_DWORD)),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", _DWORD), ("ReparseTag", _DWORD)]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class AccessContextError(RuntimeError):
    """The access check for the account cannot be set up; nothing may be read."""


# --- Win32 bindings ---------------------------------------------------------

_api: SimpleNamespace | None = None
_api_lock = threading.Lock()


def _win() -> SimpleNamespace:
    """The Win32 functions this module calls, bound once (private DLL objects)."""
    global _api
    if _api is not None:
        return _api
    with _api_lock:
        if _api is None:
            _api = _bind()
        return _api


def _bind() -> SimpleNamespace:
    if sys.platform != "win32":
        raise OSError(errno.ENOSYS, "the folder access policy is only available on Windows")
    from ctypes import wintypes as wt

    # Loaded from System32 only (LOAD_LIBRARY_SEARCH_SYSTEM32), never from the
    # application or current folder.
    dlls = {
        name: ctypes.WinDLL(f"{name}.dll", use_last_error=True, winmode=0x00000800)
        for name in ("authz", "advapi32", "kernel32", "shell32", "ole32")
    }
    p = ctypes.c_void_p
    pp = ctypes.POINTER(ctypes.c_void_p)
    pdw = ctypes.POINTER(_DWORD)
    dw = _DWORD
    i = ctypes.c_int
    signatures = {
        # name: (DLL, return type, argument types)
        "AuthzInitializeResourceManager": ("authz", wt.BOOL, (dw, p, p, p, wt.LPCWSTR, pp)),
        "AuthzInitializeContextFromSid": ("authz", wt.BOOL, (dw, p, p, p, _Luid, p, pp)),
        "AuthzGetInformationFromContext": ("authz", wt.BOOL, (p, i, dw, pdw, p)),
        "AuthzModifySids": ("authz", wt.BOOL, (p, i, ctypes.POINTER(i), p)),
        "AuthzAccessCheck": (
            "authz",
            wt.BOOL,
            (
                dw,
                p,
                ctypes.POINTER(_AuthzAccessRequest),
                p,
                p,
                p,
                dw,
                ctypes.POINTER(_AuthzAccessReply),
                p,
            ),
        ),
        "AuthzFreeContext": ("authz", wt.BOOL, (p,)),
        "AuthzFreeResourceManager": ("authz", wt.BOOL, (p,)),
        "ConvertStringSidToSidW": ("advapi32", wt.BOOL, (wt.LPCWSTR, pp)),
        "ConvertSidToStringSidW": ("advapi32", wt.BOOL, (p, pp)),
        "IsValidSid": ("advapi32", wt.BOOL, (p,)),
        "GetLengthSid": ("advapi32", dw, (p,)),
        "LookupAccountSidW": (
            "advapi32",
            wt.BOOL,
            (wt.LPCWSTR, p, wt.LPWSTR, pdw, wt.LPWSTR, pdw, ctypes.POINTER(i)),
        ),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            "advapi32",
            wt.BOOL,
            (wt.LPCWSTR, dw, pp, pdw),
        ),
        "GetSecurityInfo": ("advapi32", dw, (p, i, dw, pp, pp, pp, pp, pp)),
        "GetSecurityDescriptorLength": ("advapi32", dw, (p,)),
        "OpenProcessToken": ("advapi32", wt.BOOL, (p, dw, pp)),
        "GetTokenInformation": ("advapi32", wt.BOOL, (p, i, p, dw, pdw)),
        "CreateFileW": ("kernel32", p, (wt.LPCWSTR, dw, dw, p, dw, dw, p)),
        "CloseHandle": ("kernel32", wt.BOOL, (p,)),
        "GetFinalPathNameByHandleW": ("kernel32", dw, (p, wt.LPWSTR, dw, dw)),
        "GetFileInformationByHandleEx": ("kernel32", wt.BOOL, (p, i, p, dw)),
        "GetDriveTypeW": ("kernel32", wt.UINT, (wt.LPCWSTR,)),
        "GetCurrentProcess": ("kernel32", p, ()),
        "LocalFree": ("kernel32", p, (p,)),
        "SHGetKnownFolderPath": ("shell32", ctypes.c_long, (ctypes.POINTER(_Guid), dw, p, pp)),
        "CoTaskMemFree": ("ole32", None, (p,)),
    }
    functions = {}
    for name, (dll, restype, argtypes) in signatures.items():
        function = getattr(dlls[dll], name)
        function.restype = restype
        function.argtypes = argtypes
        functions[name] = function
    return SimpleNamespace(**functions, INVALID_HANDLE_VALUE=ctypes.c_void_p(-1).value)


def _last_error(what: str, filename: str | None = None) -> OSError:
    """An OSError for the calling thread's last Win32 error (subclass picked by Python)."""
    code = ctypes.get_last_error()
    return _win_error(code, what, filename)


def _win_error(code: int, what: str, filename: str | None = None) -> OSError:
    return OSError(None, f"{what}: {ctypes.FormatError(code).strip()}", filename, code)


# --- SIDs and accounts --------------------------------------------------------


def _sid_bytes(text: str) -> bytes:
    """Binary form of a SID string; OSError if it is not a valid SID."""
    api = _win()
    pointer = ctypes.c_void_p()
    if not api.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
        raise _last_error("not a valid SID")
    try:
        if not api.IsValidSid(pointer):
            raise OSError(errno.EINVAL, "not a valid SID")
        return ctypes.string_at(pointer, api.GetLengthSid(pointer))
    finally:
        api.LocalFree(pointer)


def _sid_string(sid_pointer) -> str:
    api = _win()
    text = ctypes.c_void_p()
    if not api.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
        raise _last_error("ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(text)
    finally:
        api.LocalFree(text)


def _account_type(sid: bytes) -> int:
    """SID_NAME_USE of an account (1 = user); OSError if it cannot be looked up."""
    api = _win()
    buffer = ctypes.create_string_buffer(sid, len(sid))
    name_length = _DWORD(0)
    domain_length = _DWORD(0)
    use = ctypes.c_int(0)
    api.LookupAccountSidW(
        None,
        buffer,
        None,
        ctypes.byref(name_length),
        None,
        ctypes.byref(domain_length),
        ctypes.byref(use),
    )
    if not name_length.value:
        raise _last_error("LookupAccountSidW")
    name = ctypes.create_unicode_buffer(name_length.value)
    domain = ctypes.create_unicode_buffer(max(domain_length.value, 1))
    if not api.LookupAccountSidW(
        None,
        buffer,
        name,
        ctypes.byref(name_length),
        domain,
        ctypes.byref(domain_length),
        ctypes.byref(use),
    ):
        raise _last_error("LookupAccountSidW")
    return use.value


def current_user_sid() -> str:
    """SID of the account this process runs as, e.g. ``S-1-5-21-…-1001``."""
    api = _win()
    token = ctypes.c_void_p()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _last_error("OpenProcessToken")
    try:
        needed = _DWORD(0)
        api.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise _last_error("GetTokenInformation")
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.GetTokenInformation(
            token, _TOKEN_USER, buffer, needed.value, ctypes.byref(needed)
        ):
            raise _last_error("GetTokenInformation")
        return _sid_string(_SidAndAttributes.from_buffer(buffer).Sid)
    finally:
        api.CloseHandle(token)


def service_sid(service_name: str) -> str:
    """SID of the virtual account ``NT SERVICE\\<service_name>``.

    Windows derives it from the name alone: ``S-1-5-80-`` followed by the
    SHA-1 of the upper-cased name in UTF-16LE, read as five little-endian
    32-bit numbers. Service names are ASCII, so ``str.upper`` matches Windows.
    """
    digest = hashlib.sha1(service_name.upper().encode("utf-16-le"), usedforsecurity=False)
    parts = struct.unpack("<5I", digest.digest())
    return "S-1-5-80-" + "-".join(str(part) for part in parts)


def uac_enabled() -> bool:
    """True when User Account Control is on (``EnableLUA`` is not 0).

    A missing or unreadable value counts as on, Windows' default, which keeps
    the access check strict. Always False on other platforms.
    """
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _UAC_POLICY_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, kind = winreg.QueryValueEx(key, "EnableLUA")
    except OSError:
        return True
    return not (kind == winreg.REG_DWORD and value == 0)


def _uac_filtered(sid: str) -> bool:
    """Whether UAC makes this group deny-only in an administrator's standard token."""
    if sid in _UAC_FILTERED_SIDS:
        return True
    parts = sid.split("-")
    return (
        len(parts) > 4
        and parts[:4] == ["S", "1", "5", "21"]
        and parts[-1].isdigit()
        and int(parts[-1]) in _UAC_FILTERED_DOMAIN_RIDS
    )


# --- Paths ------------------------------------------------------------------


def _local_absolute(path: str | os.PathLike[str]) -> str:
    """Check the path text before any file system access and normalize it.

    Network and device paths are refused here, so the service never opens them:
    opening a share as LocalSystem would authenticate with the computer account.
    """
    text = os.fspath(path)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    text = text.replace("/", "\\")
    if not text or "\x00" in text:
        raise FolderNotAllowed(NOT_A_LOCAL_FOLDER)
    if text.startswith("\\\\"):
        if text.startswith(("\\\\?\\", "\\\\.\\")) and text[4:8].upper() != "UNC\\":
            raise FolderNotAllowed(DEVICE_PATH)
        raise FolderNotAllowed(NETWORK_LOCATION)
    drive, rest = ntpath.splitdrive(text)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not rest.startswith("\\"):
        raise FolderNotAllowed(RELATIVE_PATH)
    if ":" in rest:  # alternate data streams
        raise FolderNotAllowed(NOT_A_LOCAL_FOLDER)
    return ntpath.normpath(text)


def _dos_path(final: str) -> str:
    """Drive-letter form of a final path (``\\\\?\\C:\\x`` → ``C:\\x``), or a refusal."""
    if final[:8].upper() == "\\\\?\\UNC\\":
        raise FolderNotAllowed(NETWORK_LOCATION)
    match = _DOS_FINAL_PATH.fullmatch(final)
    if match is None:  # GLOBALROOT, volume GUID paths, devices
        raise FolderNotAllowed(NOT_A_LOCAL_FOLDER)
    return match.group(1)


def _at_or_below(path: str, base: str) -> bool:
    """Component-wise containment of normcased paths (``C:\\a`` is not below ``C:\\ab``)."""
    path = path.rstrip("\\")
    base = base.rstrip("\\")
    return path == base or path.startswith(base + "\\")


def _normcase(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _is_name_surrogate(attributes: int, tag: int) -> bool:
    """A reparse point that redirects to another path (symbolic link, junction, …)."""
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and bool(tag & _NAME_SURROGATE_BIT)


def _stat_is_name_surrogate(info: os.stat_result) -> bool:
    return _is_name_surrogate(
        getattr(info, "st_file_attributes", 0), getattr(info, "st_reparse_tag", 0)
    )


def _expand_profile_path(raw: str, windows_dir: str) -> str | None:
    """Expand ``%SystemDrive%``/``%SystemRoot%``/``%windir%`` in a ProfileList path.

    The values come from the known Windows folder, never from the environment.
    Returns None if another variable remains.
    """
    system_drive = ntpath.splitdrive(windows_dir)[0]
    values = {"systemdrive": system_drive, "systemroot": windows_dir, "windir": windows_dir}
    expanded = re.sub(
        r"%([^%]+)%", lambda m: values.get(m.group(1).lower(), m.group(0)), raw.strip()
    )
    return None if "%" in expanded else expanded


# --- Handles ----------------------------------------------------------------


def _open_handle(path: str, access: int, flags: int) -> int:
    api = _win()
    handle = api.CreateFileW(path, access, _FILE_SHARE_ALL, None, _OPEN_EXISTING, flags, None)
    if handle is None or handle == api.INVALID_HANDLE_VALUE:
        raise _last_error("cannot open", path)
    return handle


def _close_handle(handle: int) -> None:
    _win().CloseHandle(handle)


def _attribute_tag(handle: int) -> tuple[int, int]:
    """File attributes and reparse tag of an open handle."""
    info = _FileAttributeTagInfo()
    if not _win().GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise _last_error("GetFileInformationByHandleEx")
    return info.FileAttributes, info.ReparseTag


def _open_file_for_reading(path: str) -> int:
    """Open a file for reading without following a link at the path itself.

    A symbolic link or junction (perhaps swapped in after an earlier check)
    is refused on the handle, before anything it points to is opened. Other
    reparse points, such as cloud placeholders or deduplicated files, are
    opened again normally so their file system filter supplies the data.
    """
    handle = _open_handle(path, _GENERIC_READ, _FILE_FLAG_OPEN_REPARSE_POINT)
    try:
        attributes, tag = _attribute_tag(handle)
    except BaseException:
        _close_handle(handle)
        raise
    if not attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return handle
    _close_handle(handle)
    if _is_name_surrogate(attributes, tag):
        raise PermissionError(errno.EACCES, LINK_NOT_FOLLOWED, path)
    return _open_handle(path, _GENERIC_READ, 0)


def _final_path_name(handle: int) -> str:
    """``GetFinalPathNameByHandleW(VOLUME_NAME_DOS)``, e.g. ``\\\\?\\C:\\Users\\me``."""
    api = _win()
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = api.GetFinalPathNameByHandleW(
            handle, buffer, size, _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS
        )
        if length == 0:
            raise _last_error("GetFinalPathNameByHandleW")
        if length < size:
            return buffer.value
        size = length + 1


def _final_dos_path(handle: int) -> str:
    """Drive-letter final path of a handle; FolderNotAllowed if it has none."""
    try:
        final = _final_path_name(handle)
    except OSError as exc:
        # Volumes without a drive letter (including shadow copies) have no DOS name.
        raise FolderNotAllowed(NOT_A_LOCAL_FOLDER) from exc
    return _dos_path(final)


def _security_descriptor(handle: int) -> bytes:
    """Self-relative security descriptor (owner, group, DACL) of an open handle."""
    api = _win()
    descriptor = ctypes.c_void_p()
    code = api.GetSecurityInfo(
        handle, _SE_FILE_OBJECT, _OWNER_GROUP_DACL, None, None, None, None, ctypes.byref(descriptor)
    )
    if code:
        raise _win_error(code, "GetSecurityInfo")
    try:
        data = ctypes.string_at(descriptor, api.GetSecurityDescriptorLength(descriptor))
    finally:
        api.LocalFree(descriptor)
    if len(data) < 4 or not struct.unpack_from("<H", data, 2)[0] & _SE_SELF_RELATIVE:
        raise OSError(errno.EINVAL, "unexpected security descriptor format")
    return data


def _sd_from_sddl(sddl: str) -> bytes:
    """Self-relative security descriptor for an SDDL string."""
    api = _win()
    descriptor = ctypes.c_void_p()
    size = _DWORD(0)
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(size)
    ):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        return ctypes.string_at(descriptor, size.value)
    finally:
        api.LocalFree(descriptor)


def _known_folder(folder_id: str) -> str:
    api = _win()
    guid = _Guid.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    path = ctypes.c_void_p()
    result = api.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path))
    try:
        if result != 0:
            raise OSError(
                errno.ENOENT,
                f"known folder {{{folder_id}}} not found (0x{result & 0xFFFFFFFF:08X})",
            )
        return ctypes.wstring_at(path)
    finally:
        api.CoTaskMemFree(path)


def _drive_type(root: str) -> int:
    return _win().GetDriveTypeW(root)


def _profile_list() -> dict[str, str]:
    """``{SID: ProfileImagePath}`` from ``HKLM\\…\\ProfileList`` (unexpanded)."""
    import winreg

    profiles: dict[str, str] = {}
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, _PROFILE_LIST_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    ) as key:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(key, name) as entry:
                    value, _kind = winreg.QueryValueEx(entry, "ProfileImagePath")
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                profiles[name] = value
    return profiles


def _path_forms(path: str) -> list[str]:
    """Normcased forms of a folder: as given and, if it can be opened, its final path."""
    forms = [_normcase(path)]
    try:
        handle = _open_handle(path, _FILE_READ_ATTRIBUTES, _FILE_FLAG_BACKUP_SEMANTICS)
    except OSError:
        return forms
    try:
        forms.append(_normcase(_dos_path(_final_path_name(handle))))
    except (OSError, FolderNotAllowed):
        pass
    finally:
        _close_handle(handle)
    return list(dict.fromkeys(forms))


# --- The policy ---------------------------------------------------------------


class WindowsUserAccess:
    """Folder access policy: what ``user_sid`` can read without elevation.

    Implements :class:`rootmind.access.FolderAccess`. The account must be a
    user account (not a group, a service or SYSTEM). ``uac_filter`` says whether
    the UAC-filtered groups become deny-only; the default follows the computer's
    ``EnableLUA`` policy (:func:`uac_enabled`).

    Raises:
        AccessContextError: not Windows, the SID is invalid or not a user
            account, the known folders cannot be found, or AuthZ cannot build
            the context or apply the UAC filter.

    All methods are thread-safe. AuthZ calls are serialized, and their results
    are cached per security descriptor. Call :meth:`close` (or use the policy as
    a context manager) to free the AuthZ context; after that everything is denied.
    """

    def __init__(self, user_sid: str, *, uac_filter: bool | None = None) -> None:
        self._lock = threading.Lock()
        self._cache: collections.OrderedDict[tuple[int, bytes], bool] = collections.OrderedDict()
        self._cache_limit = CACHE_SIZE
        self._resource_manager: ctypes.c_void_p | None = None
        self._context: ctypes.c_void_p | None = None
        self.user_sid = str(user_sid)
        #: UAC filter: "deny-only", "removed" (fallback), "none" (the account is in
        #: no filtered group) or "off" (UAC is off, or ``uac_filter=False``).
        self.filter_mode = "off"
        try:
            self._setup(user_sid, uac_filter)
        except BaseException:
            self.close()
            raise
        logger.info("Folder access policy for %s: UAC filter %s", self.user_sid, self.filter_mode)

    def _setup(self, user_sid: str, uac_filter: bool | None) -> None:
        try:
            _win()
        except OSError as exc:
            raise AccessContextError(str(exc)) from exc
        if not isinstance(user_sid, str) or not user_sid.strip():
            raise AccessContextError("no account SID given")
        try:
            sid = _sid_bytes(user_sid.strip())
        except OSError as exc:
            raise AccessContextError(f"{user_sid!r} is not a valid SID") from exc
        self.user_sid = _sid_string(ctypes.create_string_buffer(sid, len(sid)))
        try:
            account_type = _account_type(sid)
        except OSError as exc:
            raise AccessContextError(f"the account {self.user_sid} cannot be found: {exc}") from exc
        if account_type != _SID_TYPE_USER:
            raise AccessContextError(f"{self.user_sid} is not a user account")

        try:
            self._windows_dir = _known_folder(FOLDERID_WINDOWS)
            system = [
                self._windows_dir,
                _known_folder(FOLDERID_PROGRAM_FILES),
                _known_folder(FOLDERID_PROGRAM_FILES_X86),
                _known_folder(FOLDERID_PROGRAM_DATA),
            ]
            profiles_root = _known_folder(FOLDERID_USER_PROFILES)
        except OSError as exc:
            raise AccessContextError(f"cannot locate the system folders: {exc}") from exc
        self._system_folders = tuple(
            dict.fromkeys(form for folder in system for form in _path_forms(folder))
        )
        self._profiles_root = frozenset(_path_forms(profiles_root))

        self._build_context(sid)
        if uac_filter is None:
            uac_filter = uac_enabled()
        self.filter_mode = self._shape_groups(uac_filter=uac_filter)

    # -- AuthZ context ----------------------------------------------------------

    def _build_context(self, sid: bytes) -> None:
        api = _win()
        manager = ctypes.c_void_p()
        if not api.AuthzInitializeResourceManager(
            _AUTHZ_RM_FLAG_NO_AUDIT, None, None, None, None, ctypes.byref(manager)
        ):
            raise AccessContextError(f"AuthZ is not available: {_last_error('AuthZ')}")
        self._resource_manager = manager
        context = ctypes.c_void_p()
        # Flags 0: AuthZ computes the groups (S4U where available, otherwise from
        # the account databases). Requiring S4U would fail for some local accounts.
        if not api.AuthzInitializeContextFromSid(
            0,
            ctypes.create_string_buffer(sid, len(sid)),
            manager,
            None,
            _Luid(0, 0),
            None,
            ctypes.byref(context),
        ):
            raise AccessContextError(
                f"cannot build the access check for {self.user_sid}: {_last_error('AuthZ')}"
            )
        self._context = context

    def _context_groups(self) -> list[tuple[str, bytes, int]]:
        """``(SID string, SID bytes, attributes)`` of every group in the context."""
        api = _win()
        needed = _DWORD(0)
        api.AuthzGetInformationFromContext(
            self._context, _AUTHZ_CONTEXT_INFO_GROUPS_SIDS, 0, ctypes.byref(needed), None
        )
        if not needed.value:
            raise AccessContextError(f"cannot read the account's groups: {_last_error('AuthZ')}")
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.AuthzGetInformationFromContext(
            self._context,
            _AUTHZ_CONTEXT_INFO_GROUPS_SIDS,
            needed.value,
            ctypes.byref(needed),
            buffer,
        ):
            raise AccessContextError(f"cannot read the account's groups: {_last_error('AuthZ')}")
        count = _TokenGroupsHeader.from_buffer(buffer).GroupCount
        entries = (_SidAndAttributes * count).from_buffer(buffer, _TokenGroupsHeader.Groups.offset)
        return [
            (
                _sid_string(entry.Sid),
                ctypes.string_at(entry.Sid, api.GetLengthSid(entry.Sid)),
                entry.Attributes,
            )
            for entry in entries
        ]

    def _replace_groups(self, groups: list[tuple[bytes, int]]) -> bool:
        """Replace the context's groups (``AuthzModifySids``, REPLACE_ALL)."""

        class TokenGroups(ctypes.Structure):
            _fields_ = [("GroupCount", _DWORD), ("Groups", _SidAndAttributes * len(groups))]

        buffers = [ctypes.create_string_buffer(sid, len(sid)) for sid, _ in groups]
        token_groups = TokenGroups()
        token_groups.GroupCount = len(groups)
        for index, (buffer, (_sid, attributes)) in enumerate(zip(buffers, groups, strict=True)):
            token_groups.Groups[index].Sid = ctypes.addressof(buffer)
            token_groups.Groups[index].Attributes = attributes
        operation = (ctypes.c_int * 1)(_AUTHZ_SID_OPERATION_REPLACE_ALL)
        if _win().AuthzModifySids(
            self._context, _AUTHZ_CONTEXT_INFO_GROUPS_SIDS, operation, ctypes.byref(token_groups)
        ):
            return True
        logger.debug("AuthzModifySids failed with error %s", ctypes.get_last_error())
        return False

    def _shape_groups(self, *, uac_filter: bool) -> str:
        """Turn the context's groups into those of the account's standard token.

        Returns the UAC filter mode that took effect (see ``filter_mode``).
        """
        groups = self._context_groups()
        present = {text for text, _sid, _attributes in groups}
        filtered = sorted(text for text in present if uac_filter and _uac_filtered(text))
        never_grant = sorted(present & _NON_INTERACTIVE_GROUPS)

        # AuthzModifySids accepts only SE_GROUP_ENABLED or SE_GROUP_USE_FOR_DENY_ONLY
        # per group; it rejects SE_GROUP_MANDATORY and SE_GROUP_ENABLED_BY_DEFAULT
        # with ERROR_INVALID_PARAMETER. Only these two bits matter for an access
        # check. A group that is neither enabled nor deny-only never grants access,
        # so it is kept as deny-only.
        deny_only: list[tuple[bytes, int]] = []
        removed: list[tuple[bytes, int]] = []
        for text, sid, attributes in groups:
            if text in filtered:
                deny_only.append((sid, _SE_GROUP_USE_FOR_DENY_ONLY))
                continue
            if text in never_grant:
                deny_only.append((sid, _SE_GROUP_USE_FOR_DENY_ONLY))
                removed.append((sid, _SE_GROUP_USE_FOR_DENY_ONLY))
                continue
            enabled = attributes & _SE_GROUP_ENABLED and not (
                attributes & _SE_GROUP_USE_FOR_DENY_ONLY
            )
            entry = (sid, _SE_GROUP_ENABLED if enabled else _SE_GROUP_USE_FOR_DENY_ONLY)
            deny_only.append(entry)
            removed.append(entry)
        for text in _LOGON_GROUPS:
            if text not in present:
                entry = (_sid_bytes(text), _SE_GROUP_ENABLED)
                deny_only.append(entry)
                removed.append(entry)
        if filtered and ADMINISTRATORS in present and LOCAL_ADMIN_ACCOUNT not in present:
            # A real filtered token also carries this group, deny-only.
            deny_only.append((_sid_bytes(LOCAL_ADMIN_ACCOUNT), _SE_GROUP_USE_FOR_DENY_ONLY))
            filtered.append(LOCAL_ADMIN_ACCOUNT)

        if not filtered:
            attempts = [("off" if not uac_filter else "none", deny_only)]
        else:
            # Deny-only is what the account's own standard token has: these groups
            # never grant access, but deny ACEs for them still apply. If AuthZ
            # rejects it, or a check shows such a group still grants access, the
            # groups are removed instead. That is slightly weaker (a deny ACE for
            # one of them would no longer apply) but never lets them grant access.
            attempts = [("deny-only", deny_only), ("removed", removed)]
        for mode, entries in attempts:
            if self._replace_groups(entries) and self._filter_holds(filtered + never_grant):
                return mode
            logger.warning("The group list for the access check (%s) did not take effect", mode)
        if filtered:
            raise AccessContextError(
                "the groups that User Account Control filters could not be removed from the "
                f"access check for {self.user_sid}"
            )
        raise AccessContextError(
            f"cannot set up the groups of {self.user_sid} for the access check"
        )

    def _filter_holds(self, never_grant: list[str]) -> bool:
        """Verify that none of these groups is enabled or grants access any more."""
        try:
            for text, _sid, attributes in self._context_groups():
                if text in never_grant and attributes & _SE_GROUP_ENABLED:
                    return False
            for text in never_grant:
                if self._check_uncached(_sd_from_sddl(f"O:SYG:SYD:(A;;FA;;;{text})"), READ_ACCESS):
                    return False
        except (OSError, AccessContextError):
            logger.exception("Could not verify the groups of the access check")
            return False
        return True

    def _check_uncached(self, descriptor: bytes, desired: int) -> bool:
        """``AuthzAccessCheck``: does the context get all of ``desired``?"""
        buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
        request = _AuthzAccessRequest(desired, None, None, 0, None)
        granted = _DWORD(0)
        error = _DWORD(0)
        reply = _AuthzAccessReply(1, ctypes.pointer(granted), None, ctypes.pointer(error))
        if not _win().AuthzAccessCheck(
            0,
            self._context,
            ctypes.byref(request),
            None,
            buffer,
            None,
            0,
            ctypes.byref(reply),
            None,
        ):
            raise _last_error("AuthzAccessCheck")
        return error.value == 0 and granted.value & desired == desired

    def _allowed(self, descriptor: bytes, desired: int) -> bool:
        """Cached access check; anything that fails counts as denied."""
        key = (desired, descriptor)
        with self._lock:
            if self._context is None:
                return False
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            try:
                result = self._check_uncached(descriptor, desired)
            except OSError as exc:
                logger.warning("Access check failed; treating it as denied: %s", exc)
                return False
            self._cache[key] = result
            while len(self._cache) > self._cache_limit:
                self._cache.popitem(last=False)
            return result

    # -- FolderAccess -----------------------------------------------------------

    def check_root(self, path: str | Path) -> Path:
        """Canonical path of a folder the account may index.

        Raises:
            FolderNotAllowed: the folder is refused (see the module docstring).
            FileNotFoundError: it does not exist.
            NotADirectoryError: it is a file.
            OSError: it cannot be opened.
        """
        normalized = _local_absolute(path)
        self._check_drive(normalized)
        other_profiles = self._other_profiles()
        # The path as given is checked before it is opened, and the final path
        # (after junctions) again on the open handle.
        self._check_location(normalized, other_profiles)
        self._refuse_symbolic_links(normalized)

        access = _READ_CONTROL | _FILE_READ_ATTRIBUTES
        # Opened without following a link first: a folder swapped for a
        # symbolic link after the check above is refused, not followed.
        handle = _open_handle(
            normalized, access, _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
        )
        try:
            attributes, tag = _attribute_tag(handle)
        except BaseException:
            _close_handle(handle)
            raise
        if _is_name_surrogate(attributes, tag):
            _close_handle(handle)
            if tag != _IO_REPARSE_TAG_MOUNT_POINT:
                raise FolderNotAllowed(SYMBOLIC_LINK)
            # A junction or mount point: Windows lets it point only to a local
            # volume, and its target is checked on the handle below.
            handle = _open_handle(normalized, access, _FILE_FLAG_BACKUP_SEMANTICS)
        try:
            attributes, _tag = _attribute_tag(handle)
            if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
                raise NotADirectoryError(errno.ENOTDIR, "Not a folder", normalized)
            final = _final_dos_path(handle)
            self._check_drive(final)
            self._check_location(final, other_profiles)
            if not self._allowed(_security_descriptor(handle), LIST_ACCESS):
                raise FolderNotAllowed(NOT_READABLE)
        finally:
            _close_handle(handle)
        return Path(final)

    def may_descend(self, directory: Path, root: Path) -> bool:
        """True if ``directory`` is a real folder under ``root`` that the account may list."""
        try:
            info = os.lstat(directory)
            if _stat_is_name_surrogate(info) or not stat.S_ISDIR(info.st_mode):
                return False
            handle = _open_handle(
                str(directory),
                _READ_CONTROL | _FILE_READ_ATTRIBUTES,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            )
            try:
                # Checked again on the handle, in case the folder was swapped for a link.
                attributes, tag = _attribute_tag(handle)
                if (
                    _is_name_surrogate(attributes, tag)
                    or not attributes & _FILE_ATTRIBUTE_DIRECTORY
                ):
                    return False
                if not _at_or_below(_normcase(_final_dos_path(handle)), _normcase(str(root))):
                    return False
                return self._allowed(_security_descriptor(handle), LIST_ACCESS)
            finally:
                _close_handle(handle)
        except Exception:  # must not raise: an unchecked folder is skipped
            logger.debug("Not entering %s", directory, exc_info=True)
            return False

    def open_file(self, path: Path, root: Path) -> BinaryIO:
        """Open ``path`` for reading if the account may read it.

        Raises:
            PermissionError: the account may not read the file, or it is a
                symbolic link.
            ValueError: the opened file lies outside ``root``.
            OSError: it cannot be opened at all.
        """
        info = os.lstat(path)
        if _stat_is_name_surrogate(info):
            raise PermissionError(errno.EACCES, LINK_NOT_FOLLOWED, str(path))
        handle = _open_file_for_reading(str(path))
        try:
            try:
                final = _final_dos_path(handle)
            except FolderNotAllowed as exc:
                raise PermissionError(errno.EACCES, str(exc), str(path)) from exc
            if not _at_or_below(_normcase(final), _normcase(str(root))):
                raise ValueError(f"{path} lies outside the indexed folder")
            if not self._allowed(_security_descriptor(handle), READ_ACCESS):
                raise PermissionError(
                    errno.EACCES,
                    "your account cannot read this file without administrator rights",
                    str(path),
                )
            import msvcrt

            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except BaseException:
            _close_handle(handle)
            raise
        try:
            return os.fdopen(fd, "rb")  # returned to the caller, who closes it
        except BaseException:
            os.close(fd)
            raise

    # -- Location rules ---------------------------------------------------------

    def _check_drive(self, path: str) -> None:
        """Refuse a path whose drive is not a local fixed, removable or optical drive."""
        kind = _drive_type(path[:2].upper() + "\\")
        if kind == _DRIVE_REMOTE:
            raise FolderNotAllowed(NETWORK_LOCATION)
        if kind not in _LOCAL_DRIVE_TYPES:
            raise FolderNotAllowed(DRIVE_NOT_AVAILABLE)

    def _refuse_symbolic_links(self, normalized: str) -> None:
        """Refuse a path that passes through a symbolic link, without following it.

        Each prefix is examined with lstat, which does not follow a link in the
        last component. Junctions and mount points are allowed: Windows only
        lets them point to local volumes, and the final path is checked anyway.
        """
        drive, rest = ntpath.splitdrive(normalized)
        current = drive + "\\"
        for part in rest.split("\\"):
            if not part:
                continue
            current = ntpath.join(current, part)
            info = os.lstat(current)
            if _stat_is_name_surrogate(info) and info.st_reparse_tag != _IO_REPARSE_TAG_MOUNT_POINT:
                raise FolderNotAllowed(SYMBOLIC_LINK)

    def _check_location(self, path: str, other_profiles: list[str]) -> None:
        """Refuse drive roots, system folders, the profiles folder and other users' profiles."""
        if _DRIVE_ROOT_PATH.fullmatch(path):
            raise FolderNotAllowed(DRIVE_ROOT)
        candidate = _normcase(path)
        if any(_at_or_below(candidate, folder) for folder in self._system_folders):
            raise FolderNotAllowed(SYSTEM_FOLDER)
        if candidate.rstrip("\\") in {root.rstrip("\\") for root in self._profiles_root}:
            raise FolderNotAllowed(PROFILES_ROOT)
        if any(_at_or_below(candidate, profile) for profile in other_profiles):
            raise FolderNotAllowed(OTHER_PROFILE)

    def _other_profiles(self) -> list[str]:
        """Normcased profile folders of every account except this one (read each time)."""
        try:
            entries = _profile_list()
        except OSError as exc:
            raise FolderNotAllowed(PROFILES_UNREADABLE) from exc
        own = self.user_sid.upper()
        folders: list[str] = []
        for sid, raw in entries.items():
            if sid.upper().removesuffix(".BAK") == own:
                continue
            expanded = _expand_profile_path(raw, self._windows_dir)
            if expanded is None:
                logger.debug("Ignoring profile path %r of %s", raw, sid)
                continue
            folders.extend(_path_forms(expanded))
        return folders

    # -- Lifetime ---------------------------------------------------------------

    def close(self) -> None:
        """Free the AuthZ context. Afterwards every check is denied."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            context, self._context = self._context, None
            manager, self._resource_manager = self._resource_manager, None
            self._cache.clear()
        if context is not None:
            _win().AuthzFreeContext(context)
        if manager is not None:
            _win().AuthzFreeResourceManager(manager)

    def __enter__(self) -> WindowsUserAccess:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: S110 (interpreter shutdown; nothing left to report to)
            pass

    def __repr__(self) -> str:
        return f"WindowsUserAccess({self.user_sid})"
