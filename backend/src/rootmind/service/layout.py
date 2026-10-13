"""Folders and permissions of the background service (Windows).

Program: ``%ProgramFiles%\\RootMind Service\\<version>-<8 hex>\\`` holds a copy of
the onedir build. Every install or update copies into a new folder that only
administrators can write, then deletes the older folders.

Data: ``%ProgramData%\\RootMind`` with this layout (``SY`` SYSTEM, ``BA``
Administrators, ``U`` the installing account, ``M`` the virtual account
``NT SERVICE\\RootMind-MCP-HTTP``; M's entries only while MCP over HTTP is on)::

    RootMind\\             owner BA, protected: SY (OI)(CI)F, BA (OI)(CI)F, U (OI)(CI)RX, M (OI)(NP)RX
      config.yaml          inherited (M may read it)
      data\\                inherited, M: nothing
        rest_token.txt     inherited (M: nothing)
        api_token.txt      inherited + M:R
        db\\                inherited + M (OI)(CI)M
      logs\\                inherited
        backend\\           inherited
        mcp-http\\          inherited + M (OI)(CI)M
        install.log        inherited
      tmp\\                 protected: SY F, BA F

A missing data folder is created with its final security descriptor, so there
is no moment with weaker permissions. An existing one is reused only when the
whole tree passes :func:`validate_data_tree`, and is then normalized to the
layout above.

Paths come from the known-folder API, never from environment variables: an
elevated process inherits the user's environment.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import secrets
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import DATA_DIR_NAME, MCP_SERVICE

# Well-known SIDs.
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
CREATOR_OWNER = "S-1-3-0"
CREATOR_GROUP = "S-1-3-1"

ADMIN_SIDS = frozenset({SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER})

# File access masks.
FULL_CONTROL = 0x001F01FF
MODIFY = 0x001301BF
READ_EXECUTE = 0x001200A9
READ = 0x00120089

# Rights that let a SID change a file or folder, or its permissions: write,
# append (add subfolder), write EA and attributes, delete child, delete,
# WRITE_DAC, WRITE_OWNER, GENERIC_WRITE, GENERIC_ALL and MAXIMUM_ALLOWED.
WRITE_RIGHTS = (
    (0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0100 | 0x00010000 | 0x00040000 | 0x00080000)
    | 0x40000000
    | 0x10000000
    | 0x02000000
)

# ACE types that deny access (denied, denied-object, denied-callback,
# denied-callback-object).
_DENY_ACE_TYPES = frozenset({0x1, 0x6, 0xA, 0xC})
_INHERIT_ONLY_ACE = 0x08

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete

# Subfolders of the data folder in which M may write (and own files).
MCP_WRITABLE = ("data/db", "logs/mcp-http")

DATA_SUBFOLDERS = ("data", "data/db", "logs", "logs/backend", "logs/mcp-http")
TMP_FOLDER = "tmp"
REST_TOKEN = "data/rest_token.txt"  # noqa: S105 (a file name, not a secret)
MCP_TOKEN = "data/api_token.txt"  # noqa: S105 (a file name, not a secret)
INSTALL_LOG = "logs/install.log"

NOT_PROTECTED = (
    "%ProgramData%\\RootMind exists but is not protected. Remove or rename it from an elevated "
    "prompt, then try again."
)

FOLDERID_PROGRAM_FILES = "905E63B6-C1BF-494E-B29C-65B732D3D21A"
FOLDERID_PROGRAM_DATA = "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97"


class LayoutError(Exception):
    """A folder cannot be used safely; the message is user-facing."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


# --- SIDs and SDDL ----------------------------------------------------------------


def mcp_sid() -> str:
    """SID of ``NT SERVICE\\RootMind-MCP-HTTP`` (computed from the service name)."""
    from ..winaccess import service_sid

    return service_sid(MCP_SERVICE)


@dataclass(frozen=True)
class Ace:
    """An allow ACE: inheritance flags in SDDL form ("OICI", "OINP", ""), mask, SID."""

    flags: str
    mask: int
    sid: str

    def sddl(self) -> str:
        return f"(A;{self.flags};0x{self.mask:x};;;{self.sid})"


@dataclass(frozen=True)
class Entry:
    """The explicit permissions of one item of the data folder."""

    aces: tuple[Ace, ...] = ()
    protected: bool = False
    is_dir: bool = True

    def dacl_sddl(self) -> str:
        return "D:" + ("P" if self.protected else "") + "".join(a.sddl() for a in self.aces)


def data_layout(user_sid: str, mcp: str | None) -> dict[str, Entry]:
    """Explicit permissions per relative path (``/`` separators; ``""`` is the root).

    ``mcp`` is the MCP account's SID while MCP over HTTP is enabled, else None.
    Everything not listed only inherits.
    """
    root = [
        Ace("OICI", FULL_CONTROL, SYSTEM),
        Ace("OICI", FULL_CONTROL, ADMINISTRATORS),
        Ace("OICI", READ_EXECUTE, user_sid),
    ]
    if mcp:
        root.append(Ace("OINP", READ_EXECUTE, mcp))
    mcp_modify = (Ace("OICI", MODIFY, mcp),) if mcp else ()
    return {
        "": Entry(tuple(root), protected=True),
        "data": Entry(),
        "data/db": Entry(mcp_modify),
        "logs": Entry(),
        "logs/backend": Entry(),
        "logs/mcp-http": Entry(mcp_modify),
        TMP_FOLDER: Entry(
            (Ace("OICI", FULL_CONTROL, SYSTEM), Ace("OICI", FULL_CONTROL, ADMINISTRATORS)),
            protected=True,
        ),
        MCP_TOKEN: Entry((Ace("", READ, mcp),) if mcp else (), is_dir=False),
    }


def root_sddl(user_sid: str, mcp: str | None) -> str:
    """Security descriptor of ``%ProgramData%\\RootMind`` (owner Administrators)."""
    return "O:BA" + data_layout(user_sid, mcp)[""].dacl_sddl()


def tmp_sddl() -> str:
    return "O:BA" + data_layout(SYSTEM, None)[TMP_FOLDER].dacl_sddl()


def program_sddl() -> str:
    """Security descriptor of the program folders: administrators write, users read."""
    aces = (
        Ace("OICI", FULL_CONTROL, SYSTEM),
        Ace("OICI", FULL_CONTROL, ADMINISTRATORS),
        Ace("OICI", READ_EXECUTE, USERS),
    )
    return "O:BAD:P" + "".join(a.sddl() for a in aces)


# --- Known folders and directory creation (ctypes) -------------------------------------


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


def _dll(name: str):
    # System32 only (LOAD_LIBRARY_SEARCH_SYSTEM32).
    return ctypes.WinDLL(f"{name}.dll", use_last_error=True, winmode=0x00000800)


def known_folder(folder_id: str) -> Path:
    """A known folder's path (``SHGetKnownFolderPath``), never from the environment."""
    if sys.platform != "win32":
        raise OSError(errno.ENOSYS, "known folders exist only on Windows")
    shell32 = _dll("shell32")
    ole32 = _dll("ole32")
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.POINTER(_Guid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    guid = _Guid.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    pointer = ctypes.c_void_p()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    try:
        if result != 0:
            raise OSError(errno.ENOENT, f"known folder {{{folder_id}}} not found")
        return Path(ctypes.wstring_at(pointer))
    finally:
        ole32.CoTaskMemFree(pointer)


def program_files() -> Path:
    return known_folder(FOLDERID_PROGRAM_FILES)


def program_data() -> Path:
    return known_folder(FOLDERID_PROGRAM_DATA)


def data_root() -> Path:
    """``%ProgramData%\\RootMind`` (from the known folder)."""
    return program_data() / DATA_DIR_NAME


def create_directory(path: Path, sddl: str) -> None:
    """Create a folder with exactly the given security descriptor.

    Raises:
        FileExistsError: the folder (or a file) already exists.
        OSError: any other failure.
    """
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1
    )
    raw = bytes(memoryview(descriptor))
    buffer = ctypes.create_string_buffer(raw, len(raw))
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), ctypes.cast(buffer, ctypes.c_void_p), 0
    )
    kernel32 = _dll("kernel32")
    kernel32.CreateDirectoryW.restype = ctypes.c_int
    kernel32.CreateDirectoryW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(_SecurityAttributes))
    if not kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
        code = ctypes.get_last_error()
        if code == 183:  # ERROR_ALREADY_EXISTS
            raise FileExistsError(errno.EEXIST, "already exists", str(path))
        raise OSError(None, ctypes.FormatError(code).strip(), str(path), code)


# --- Reading permissions -------------------------------------------------------------


@dataclass
class Security:
    """Owner and DACL of a file or folder (SID strings)."""

    owner: str
    protected: bool
    # (ace type, ace flags, mask, sid); None when the object has no DACL at all.
    aces: list[tuple[int, int, int, str]] | None = field(default_factory=list)


def read_security(path: Path) -> Security:
    """Owner and DACL of ``path`` (not following a link at the path itself)."""
    import win32security

    info = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT, info)
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    control, _revision = descriptor.GetSecurityDescriptorControl()
    protected = bool(control & win32security.SE_DACL_PROTECTED)
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        return Security(owner, protected, None)
    aces = []
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        (ace_type, ace_flags), mask = ace[0], ace[1]
        sid = win32security.ConvertSidToStringSid(ace[-1])
        aces.append((ace_type, ace_flags, mask & 0xFFFFFFFF, sid))
    return Security(owner, protected, aces)


def write_grants(security: Security, allowed: frozenset[str] | set[str]) -> list[str]:
    """SIDs outside ``allowed`` with an allow ACE that grants a write right."""
    if security.aces is None:
        return ["Everyone (no access list)"]
    found = []
    for ace_type, _flags, mask, sid in security.aces:
        # Deny entries never grant anything. Any other type counts as a grant
        # (fail closed), not only the known allow types.
        if ace_type in _DENY_ACE_TYPES or not mask & WRITE_RIGHTS:
            continue
        if sid not in allowed:
            found.append(sid)
    return found


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def walk(root: Path) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Every item below ``root`` (root first) as ``(path, relative path, lstat)``.

    Reparse points are yielded but never entered. Items that disappear while
    the tree is walked (a running service's temporary files) are skipped.
    """
    info = os.lstat(root)
    yield root, "", info
    if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        return
    pending = [(root, "")]
    while pending:
        folder, relative = pending.pop()
        try:
            with os.scandir(folder) as entries:
                children = sorted(entries, key=lambda e: e.name)
        except FileNotFoundError:
            continue
        for entry in children:
            path = Path(entry.path)
            rel = f"{relative}/{entry.name}" if relative else entry.name
            # os.lstat, not entry.stat(): on Windows the latter has no inode,
            # device or link count.
            try:
                child = os.lstat(path)
            except FileNotFoundError:
                continue
            yield path, rel, child
            if stat.S_ISDIR(child.st_mode) and not _is_reparse(child):
                pending.append((path, rel))


def _vanished(error: BaseException) -> bool:
    """Whether ``error`` says the file or folder no longer exists."""
    code = getattr(error, "winerror", None)
    return isinstance(error, FileNotFoundError) or code in (2, 3)  # file, path not found


def _in_zone(relative: str, zones: tuple[str, ...]) -> bool:
    return any(relative == zone or relative.startswith(zone + "/") for zone in zones)


def validate_data_tree(
    root: Path,
    *,
    mcp: str,
    owners: frozenset[str] = frozenset({SYSTEM, ADMINISTRATORS}),
    writers: frozenset[str] = ADMIN_SIDS,
) -> list[str]:
    """Why an existing data folder must not be reused; empty when it may.

    For the root and every item below it:

    - the owner is SYSTEM or Administrators (M also inside ``data\\db`` and
      ``logs\\mcp-http``, where it creates files);
    - no allow ACE grants a write right (write, append, delete, delete child,
      WRITE_DAC, WRITE_OWNER, generic write or all) to a SID other than
      SYSTEM, Administrators, TrustedInstaller, and M in those two folders;
    - no reparse points (links, junctions, mount points);
    - no file with more than one hard link.

    ``owners`` and ``writers`` are parameters only so tests can run unelevated.
    """
    problems: list[str] = []
    try:
        items = list(walk(root))
    except OSError as e:
        return [f"cannot list {e.filename or root}: {e.strerror or e}"]
    for path, relative, info in items:
        shown = relative or "."
        if _is_reparse(info):
            problems.append(f"{shown}: is a link or reparse point")
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            problems.append(f"{shown}: has {info.st_nlink} hard links")
        try:
            security = read_security(path)
        except Exception as e:  # pywintypes.error, OSError
            if _vanished(e):
                continue
            problems.append(f"{shown}: cannot read its permissions ({e})")
            continue
        in_zone = _in_zone(relative, MCP_WRITABLE)
        allowed_owners = owners | {mcp} if in_zone else owners
        if security.owner not in allowed_owners:
            problems.append(f"{shown}: owned by {security.owner}")
        allowed_writers = writers | {mcp} if in_zone else writers
        problems.extend(
            f"{shown}: {sid} may change it" for sid in write_grants(security, allowed_writers)
        )
    return problems


def find_reparse_points(root: Path) -> list[str]:
    """Relative paths of links, junctions and mount points below ``root``."""
    return [rel or "." for _path, rel, info in walk(root) if _is_reparse(info)]


# --- Writing permissions --------------------------------------------------------------


def _set_dacl(path: Path, entry: Entry, *, owner_admins: bool = False) -> None:
    """Set the access list (and owner) of ``path`` through a handle of the item itself.

    The handle is opened without following a reparse point, and the item is
    refused when it is one. A folder swapped for a link after the walk saw
    it therefore never passes the new permissions on to the link's target.
    """
    import win32file
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        entry.dacl_sddl(), win32security.SDDL_REVISION_1
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    info = win32security.DACL_SECURITY_INFORMATION
    info |= (
        win32security.PROTECTED_DACL_SECURITY_INFORMATION
        if entry.protected
        else win32security.UNPROTECTED_DACL_SECURITY_INFORMATION
    )
    access = _READ_CONTROL | _WRITE_DAC
    owner = None
    if owner_admins:
        info |= win32security.OWNER_SECURITY_INFORMATION
        owner = win32security.ConvertStringSidToSid(ADMINISTRATORS)
        access |= _WRITE_OWNER
    handle = win32file.CreateFile(
        str(path),
        access,
        _FILE_SHARE_ALL,
        None,
        win32file.OPEN_EXISTING,
        win32file.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    try:
        tag = win32file.GetFileInformationByHandleEx(handle, win32file.FileAttributeTagInfo)
        if tag["FileAttributes"] & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise LayoutError(f"{path} is a link or reparse point")
        win32security.SetSecurityInfo(
            handle, win32security.SE_FILE_OBJECT, info, owner, None, dacl, None
        )
    finally:
        handle.Close()


def normalize_data_tree(
    root: Path, user_sid: str, mcp: str | None, *, set_owner: bool = True
) -> None:
    """Apply the layout to an existing (validated) data folder.

    The root and ``tmp`` get their protected access lists (and, with
    ``set_owner``, the owner Administrators), the listed items their explicit
    entries, and every other item inherits only. Run it only after
    :func:`validate_data_tree` accepted the tree.
    """
    layout = data_layout(user_sid, mcp)
    plain = Entry()
    for path, relative, info in walk(root):
        if _is_reparse(info):
            raise LayoutError(f"{relative or '.'} is a link or reparse point")
        owner = set_owner and relative in ("", TMP_FOLDER)
        try:
            _set_dacl(path, layout.get(relative, plain), owner_admins=owner)
        except Exception as e:
            if relative and _vanished(e):
                continue  # a temporary file of a running service
            raise


def prepare_data_tree(root: Path, user_sid: str, mcp: str | None, *, mcp_account: str) -> bool:
    """Create the data folder, or validate and normalize the existing one.

    Args:
        root: ``%ProgramData%\\RootMind``.
        user_sid: The installing account.
        mcp: The MCP account's SID while MCP over HTTP is enabled, else None.
        mcp_account: The MCP account's SID (for validation: its entries from
            an earlier install are expected).

    Returns:
        True when the folder was created.

    Raises:
        LayoutError: an existing folder is not protected (exit 6).
    """
    created = False
    try:
        create_directory(root, root_sddl(user_sid, mcp))
        created = True
    except FileExistsError:
        problems = validate_data_tree(root, mcp=mcp_account)
        if problems:
            raise LayoutError(NOT_PROTECTED, problems) from None
    for sub in DATA_SUBFOLDERS:
        with contextlib.suppress(FileExistsError):
            os.mkdir(root / sub)
    with contextlib.suppress(FileExistsError):
        create_directory(root / TMP_FOLDER, tmp_sddl())
    # Only our own entries exist now (created inside the protected root, or
    # validated); normalizing sets the exact layout, including M's entries.
    normalize_data_tree(root, user_sid, mcp)
    return created


def remove_data_tree(root: Path) -> None:
    """Delete the data folder after checking it holds no link or reparse point."""
    links = find_reparse_points(root)
    if links:
        raise LayoutError(
            f"{root} contains links or reparse points ({', '.join(links[:5])}); "
            "it was not deleted. Remove it by hand from an elevated prompt."
        )
    shutil.rmtree(root, onexc=_clear_readonly_and_retry)


def _clear_readonly_and_retry(function, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


# --- Program folder ------------------------------------------------------------------


def check_admin_only(
    path: Path,
    *,
    recursive: bool,
    owners: frozenset[str] = ADMIN_SIDS,
    writers: frozenset[str] = ADMIN_SIDS,
) -> list[str]:
    """Why ``path`` (and with ``recursive`` everything below) is not admin-only writable.

    The owner must be SYSTEM, Administrators or TrustedInstaller, and no other
    SID may hold a write right. Inherit-only entries for CREATOR OWNER and
    CREATOR GROUP are ignored: they grant nothing on the folder itself, and
    only administrators can create anything in it.
    """
    problems: list[str] = []
    items = walk(path) if recursive else [(path, "", os.lstat(path))]
    for item, relative, info in items:
        shown = relative or str(path)
        if _is_reparse(info):
            problems.append(f"{shown}: is a link or reparse point")
            continue
        try:
            security = read_security(item)
        except Exception as e:
            problems.append(f"{shown}: cannot read its permissions ({e})")
            continue
        if security.owner not in owners:
            problems.append(f"{shown}: owned by {security.owner}")
        if security.aces is not None:
            creator_templates = (CREATOR_OWNER, CREATOR_GROUP)
            security.aces = [
                ace
                for ace in security.aces
                if not (ace[1] & _INHERIT_ONLY_ACE and ace[3] in creator_templates)
            ]
        problems.extend(f"{shown}: {sid} may change it" for sid in write_grants(security, writers))
    return problems


def prepare_program_base(base: Path) -> None:
    """Create ``%ProgramFiles%\\RootMind Service``, or check and re-protect it.

    Raises:
        LayoutError: an existing folder is a link or can be changed by non-administrators.
    """
    try:
        create_directory(base, program_sddl())
        return
    except FileExistsError:
        pass
    problems = check_admin_only(base, recursive=False)
    if problems:
        raise LayoutError(
            f"{base} exists but is not protected. Remove it from an elevated prompt, then try "
            "again.",
            problems,
        )
    descriptor_entry = Entry(
        (
            Ace("OICI", FULL_CONTROL, SYSTEM),
            Ace("OICI", FULL_CONTROL, ADMINISTRATORS),
            Ace("OICI", READ_EXECUTE, USERS),
        ),
        protected=True,
    )
    _set_dacl(base, descriptor_entry, owner_admins=True)


def _read_source_file(path: Path, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise LayoutError(f"{path} changed while it was being copied")
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_new_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def copy_tree_verified(source: Path, target: Path) -> int:
    """Copy the onedir tree ``source`` into the existing, empty folder ``target``.

    Links, reparse points and files with more than one hard link are refused,
    so nothing outside ``source`` is copied. Every file is read once, written,
    read back and compared by SHA-256 with the bytes that were read.

    Returns:
        The number of files copied.

    Raises:
        LayoutError: the source tree is refused or a copy does not match.
    """
    root_info = os.lstat(source)
    if _is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise LayoutError(f"{source} is not a plain folder")
    count = 0
    for path, relative, info in walk(source):
        if not relative:
            continue
        if _is_reparse(info):
            raise LayoutError(f"{relative} in {source} is a link or reparse point")
        destination = target.joinpath(*relative.split("/"))
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(destination)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise LayoutError(f"{relative} in {source} is not a regular file")
        if info.st_nlink > 1:
            raise LayoutError(f"{relative} in {source} has more than one hard link")
        data = _read_source_file(path, info)
        digest = hashlib.sha256(data).digest()
        _write_new_file(destination, data)
        if hashlib.sha256(destination.read_bytes()).digest() != digest:
            raise LayoutError(f"the copy of {relative} does not match the original")
        count += 1
    return count


def new_version_folder(base: Path, version: str) -> Path:
    """Create ``<base>\\<version>-<8 hex>`` (admin-only), never reusing a name."""
    safe = "".join(c if c.isalnum() or c in ".-+" else "_" for c in version) or "0"
    for _attempt in range(16):
        folder = base / f"{safe}-{secrets.token_hex(4)}"
        try:
            create_directory(folder, program_sddl())
        except FileExistsError:
            continue
        return folder
    raise LayoutError(f"could not create a new folder in {base}")


def install_program(source: Path, base: Path, version: str) -> Path:
    """Copy the onedir build into a new admin-only version folder; returns the folder."""
    prepare_program_base(base)
    folder = new_version_folder(base, version)
    try:
        copy_tree_verified(source, folder)
        problems = check_admin_only(folder, recursive=True)
        if problems:
            raise LayoutError(f"{folder} is not protected after the copy", problems)
    except BaseException:
        remove_tree(folder)
        raise
    return folder


def _delay_delete(path: Path) -> bool:
    import win32file

    try:
        win32file.MoveFileEx(str(path), None, win32file.MOVEFILE_DELAY_UNTIL_REBOOT)
    except Exception:
        return False
    return True


def remove_tree(path: Path) -> list[str]:
    """Delete a folder tree without following links.

    Files that are in use (a running executable and its DLLs) are deleted at
    the next restart (``MoveFileEx(DELAY_UNTIL_REBOOT)``), and so are the
    folders that still hold them.

    Returns:
        The paths left for deletion at restart.
    """
    pending: list[str] = []
    try:
        items = list(walk(path))
    except FileNotFoundError:
        return pending
    # Children before their folders. A junction or folder link is removed
    # with rmdir, which deletes the link and never its target.
    for item, _relative, info in reversed(items):
        try:
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(item)
            else:
                _unlink(item)
        except FileNotFoundError:
            continue
        except OSError:
            if sys.platform == "win32" and _delay_delete(item):
                pending.append(str(item))
            else:
                raise
    return pending


def _unlink(path: Path) -> None:
    """Delete a file, clearing read-only; one more try after a moment (a scanner's handle)."""
    try:
        os.unlink(path)
        return
    except PermissionError:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IWRITE)
    try:
        os.unlink(path)
    except PermissionError:
        time.sleep(0.25)
        os.unlink(path)


def remove_version_folders(base: Path, keep: Path | None = None) -> list[str]:
    """Delete every version folder in ``base`` except ``keep``; returns pending deletions."""
    pending: list[str] = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except FileNotFoundError:
        return pending
    for entry in entries:
        path = Path(entry.path)
        if keep is not None and os.path.normcase(str(path)) == os.path.normcase(str(keep)):
            continue
        pending.extend(remove_tree(path))
    return pending


def remove_program_base(base: Path) -> list[str]:
    """Delete every version folder and then ``base`` itself when it is empty."""
    pending = remove_version_folders(base)
    try:
        os.rmdir(base)
    except FileNotFoundError:
        pass
    except OSError:
        if sys.platform == "win32" and _delay_delete(base):
            pending.append(str(base))
    return pending


# --- Files in the data folder --------------------------------------------------------------


def safe_append(path: Path, text: str) -> bool:
    """Append ``text`` to a log file in the data folder, refusing links and odd files.

    Returns False (and writes nothing) when the file or its folder is a link
    or reparse point, or the file has more than one hard link.
    """
    try:
        folder = os.lstat(path.parent)
        if _is_reparse(folder) or not stat.S_ISDIR(folder.st_mode):
            return False
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            before = None
        if before is not None and (
            _is_reparse(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink > 1
        ):
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            opened = os.fstat(fd)
            if before is not None and (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                return False
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True
