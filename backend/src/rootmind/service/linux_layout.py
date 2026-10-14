"""Folders and file modes of the background service on Linux (systemd user units).

The services run as the account that installed them, so file ownership and
modes are the whole access control (no access lists, no second account)::

    $XDG_DATA_HOME/rootmind/       (default ~/.local/share/rootmind), 0700
      config.yaml                  0600
      service.json                 0600, where the program copy is (no secret)
      data/                        0700
        rest_token.txt             0600  REST bearer token
        api_token.txt              0600  MCP bearer token
        ngrok_authtoken.txt        0600  remote access
        remote_access.json         the tunnel's status (no secret)
        db/index.db                the index
      logs/                        0700  backend/, mcp-http/, install.log
      tmp/                         0700  the backend's temporary files
      runtime/<version>-<8 hex>/   0700  the program copy the units run

Every folder is 0700 and no file grants anything to the group or others. A
missing folder or file is created with its final mode (``os.mkdir``/``os.open``
with the mode, then ``fchmod`` against a lax umask), so there is no moment
with weaker permissions. An existing data folder is reused only when it and
everything below it is owned by the account and holds no symbolic link and
no file with a second hard link; the folder itself, ``data/`` and the secret
files must be writable by nobody else, and the folders above it must not let
another account replace it (:func:`validate_data_tree`). It is then
normalized to the modes above (a group write bit from a umask of 002 on an
item further down is removed there, not refused: nobody else could reach it
through the folders above). "Writable by nobody else" counts the account's
user private group (:func:`group_is_private`) as the account's own.

The program copy: an AppImage runs from a temporary mount that disappears
when the app closes, so ``service install`` copies the bundled backend into
``runtime/`` (for every package format, the same way) and the units run that
copy. Each install or update copies into a new folder and deletes the older
ones once the units run the new one.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import posixpath
import secrets
import shutil
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path

from . import LINUX_DATA_DIR_NAME, LINUX_EXE_NAME, RUNTIME_DIR_NAME

DATA_SUBFOLDERS = ("data", "data/db", "logs", "logs/backend", "logs/mcp-http", "tmp")
SECRET_FILES = ("data/rest_token.txt", "data/api_token.txt", "data/ngrok_authtoken.txt")
DISCOVERY_FILE = "service.json"

DIR_MODE = 0o700
FILE_MODE = 0o600
# Bits nobody but the owner may hold in the data folder.
_OTHERS = 0o077

NOT_PROTECTED = (
    "The data folder exists but is not protected (owned by this account, writable by nobody "
    "else, without links). Fix or remove it, then try again."
)


class LayoutError(Exception):
    """A folder cannot be used safely; the message is user-facing."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


# --- where things are -------------------------------------------------------------


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    if home and posixpath.isabs(home):
        return Path(home)
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)  # type: ignore[attr-defined]


def _xdg(env: Mapping[str, str], name: str, default: str) -> Path:
    """An XDG base folder; a relative value is invalid and ignored (XDG spec)."""
    value = env.get(name)
    if value and posixpath.isabs(value):
        return Path(value)
    return _home(env) / default


def data_root(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_DATA_HOME/rootmind`` (default ``~/.local/share/rootmind``)."""
    return _xdg(os.environ if env is None else env, "XDG_DATA_HOME", ".local/share") / (
        LINUX_DATA_DIR_NAME
    )


def unit_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/systemd/user`` (default ``~/.config/systemd/user``)."""
    return _xdg(os.environ if env is None else env, "XDG_CONFIG_HOME", ".config") / ("systemd/user")


def runtime_base(root: Path) -> Path:
    return root / RUNTIME_DIR_NAME


def home_of_program(executable: str) -> Path | None:
    """The data folder of a program copy ``<root>/runtime/<folder>/rootmind-backend``.

    The service finds its data folder next to its program, so it never
    depends on the environment of the user's service manager.
    """
    path = Path(executable)
    if path.name != LINUX_EXE_NAME or len(path.parents) < 3:
        return None
    if path.parent.parent.name != RUNTIME_DIR_NAME:
        return None
    return path.parent.parent.parent


def exe_is_ours(exe: str, root: Path) -> bool:
    """Whether ``exe`` is exactly ``<root>/runtime/<folder>/rootmind-backend``."""
    if posixpath.normpath(exe) != exe:
        return False
    prefix = runtime_base(root).as_posix() + "/"
    if not exe.startswith(prefix):
        return False
    parts = exe[len(prefix) :].split("/")
    return len(parts) == 2 and parts[0] not in ("", ".", "..") and parts[1] == LINUX_EXE_NAME


# --- creating files and folders ------------------------------------------------------


def private_mkdir(path: Path) -> bool:
    """Create ``path`` with mode 0700; False when it exists already."""
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        return False
    os.chmod(path, DIR_MODE)
    return True


def write_private(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Replace ``path`` with ``data`` atomically; the new file has ``mode`` from the start."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --- checking the data folder ----------------------------------------------------------


def group_is_private(gid: int, uid: int) -> bool:
    """Whether ``gid`` is the account's own user private group.

    Debian, Ubuntu and Fedora give every account a group of its own name as
    its primary group, with a umask of 002, so the folders it creates are
    group-writable. Such a group has no other member and grants nobody else
    anything.
    """
    try:
        import grp
        import pwd

        user = pwd.getpwuid(uid)  # type: ignore[attr-defined]
        group = grp.getgrgid(gid)  # type: ignore[attr-defined]
    except (ImportError, KeyError):
        return False
    return (
        group.gr_name == user.pw_name and gid == user.pw_gid and set(group.gr_mem) <= {user.pw_name}
    )


def writable_by_others(info: os.stat_result, uid: int) -> bool:
    """Whether accounts other than ``uid`` may write it (its private group does not count)."""
    if info.st_mode & stat.S_IWOTH:
        return True
    return bool(info.st_mode & stat.S_IWGRP) and not group_is_private(info.st_gid, uid)


def walk(root: Path) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Every item below ``root`` (root first) as ``(path, relative path, lstat)``.

    Symbolic links are yielded but never followed. Items that disappear while
    the tree is walked (a running service's temporary files) are skipped.
    """
    info = os.lstat(root)
    yield root, "", info
    if not stat.S_ISDIR(info.st_mode):
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
            try:
                child = os.lstat(path)
            except FileNotFoundError:
                continue
            yield path, rel, child
            if stat.S_ISDIR(child.st_mode):
                pending.append((path, rel))


# The items whose write bits for the group or others refuse the folder
# (elsewhere they are removed by normalize_data_tree).
STRICT = ("", "data", *SECRET_FILES)


def item_problems(shown: str, info: os.stat_result, uid: int, *, strict: bool = True) -> list[str]:
    """Why one item of the data folder must not be used; empty when it may.

    ``strict``: write bits for the group or others are a problem too.
    """
    if stat.S_ISLNK(info.st_mode):
        return [f"{shown}: is a symbolic link"]
    problems = []
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        problems.append(f"{shown}: is neither a folder nor a regular file")
    if info.st_uid != uid:
        problems.append(f"{shown}: owned by uid {info.st_uid}, not {uid}")
    if strict and writable_by_others(info, uid):
        problems.append(f"{shown}: writable by the group or others ({stat.filemode(info.st_mode)})")
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        problems.append(f"{shown}: has {info.st_nlink} hard links")
    return problems


def ancestor_problems(path: Path, uid: int) -> list[str]:
    """Why the folders above ``path`` would let another account replace it.

    Each must be owned by the account or root, and writable by other accounts
    (a group other than the account's private group, or everyone) only with
    the sticky bit (as ``/tmp``), which stops them from renaming or deleting
    what they do not own. Folders that do not exist yet
    are skipped (they are created owner-only).
    """
    problems = []
    for ancestor in path.parents:
        try:
            info = os.stat(ancestor)
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append(f"{ancestor}: cannot be checked ({e.strerror or e})")
            continue
        if info.st_uid not in (0, uid):
            problems.append(f"{ancestor}: owned by uid {info.st_uid}")
        if writable_by_others(info, uid) and not info.st_mode & stat.S_ISVTX:
            problems.append(f"{ancestor}: writable by the group or others without the sticky bit")
    return problems


def validate_data_tree(root: Path, uid: int) -> list[str]:
    """Why an existing data folder must not be reused; empty when it may.

    For the folder and every item below it: no symbolic link, owned by
    ``uid``, a folder or a regular file, and no file with more than one hard
    link. The folder, ``data/`` and the secret files must be writable by
    nobody else (:data:`STRICT`), and the folders above it must not let
    another account replace it (:func:`ancestor_problems`).
    """
    problems = ancestor_problems(root, uid)
    try:
        items = list(walk(root))
    except OSError as e:
        return [*problems, f"cannot list {e.filename or root}: {e.strerror or e}"]
    for _path, relative, info in items:
        problems.extend(item_problems(relative or ".", info, uid, strict=relative in STRICT))
    return problems


def check_service_home(root: Path, uid: int) -> list[str]:
    """The check the service runs when it starts: the folder, ``data/`` and the secret files.

    Cheaper than :func:`validate_data_tree`, which ``service install`` runs.
    """
    problems = []
    for relative in ("", "data", *SECRET_FILES):
        path = root / relative if relative else root
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if relative in SECRET_FILES:
                continue
            problems.append(f"{relative or root}: is missing")
            continue
        except OSError as e:
            problems.append(f"{relative or root}: cannot be checked ({e.strerror or e})")
            continue
        problems.extend(item_problems(relative or str(root), info, uid))
        if relative in SECRET_FILES and info.st_mode & _OTHERS:
            problems.append(f"{relative}: readable by the group or others")
    return problems


def normalize_data_tree(root: Path) -> None:
    """Folders 0700; files lose every group and other bit (secrets: 0600).

    Run it only after :func:`validate_data_tree` accepted the tree (or on a
    tree this run created).
    """
    for path, relative, info in walk(root):
        if stat.S_ISLNK(info.st_mode):
            raise LayoutError(f"{relative or '.'} is a symbolic link")
        if stat.S_ISDIR(info.st_mode):
            mode = DIR_MODE
        elif relative in SECRET_FILES:
            mode = FILE_MODE
        else:
            mode = stat.S_IMODE(info.st_mode) & ~_OTHERS
        if stat.S_IMODE(info.st_mode) == mode:
            continue
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except NotImplementedError:  # no lchmod on Linux; the item is not a link (checked)
            os.chmod(path, mode)
        except FileNotFoundError:
            continue


def prepare_data_tree(root: Path, uid: int) -> bool:
    """Create the data folder, or validate and normalize the existing one.

    Returns:
        True when the folder was created.

    Raises:
        LayoutError: an existing folder is not protected (exit 6).
    """
    problems = ancestor_problems(root, uid)
    if problems:
        raise LayoutError(NOT_PROTECTED, problems)
    root.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    created = private_mkdir(root)
    if not created:
        problems = validate_data_tree(root, uid)
        if problems:
            raise LayoutError(NOT_PROTECTED, problems)
    for sub in (*DATA_SUBFOLDERS, RUNTIME_DIR_NAME):
        private_mkdir(root / sub)
    normalize_data_tree(root)
    return created


def remove_tree(path: Path) -> None:
    """Delete a folder tree without following symbolic links (a link is removed itself)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    shutil.rmtree(path)  # the fd-based rmtree never follows links


# --- the program copy --------------------------------------------------------------------


def program_source_problems(folder: Path, uid: int) -> list[str]:
    """Why ``service install`` must not copy the program from ``folder``; empty when it may.

    The services run the copy as this account, so whoever could change the
    source before or while it is copied could run code as this account. The
    folder and everything below it must be owned by root or the account,
    writable by nobody else, and hold no symbolic link; the folders above it
    must not let another account replace it.
    """
    problems = []
    try:
        items = list(walk(folder))
    except OSError as e:
        return [f"{folder}: cannot be checked ({e.strerror or e})"]
    for _path, relative, info in items:
        shown = relative or str(folder)
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"{shown}: is a symbolic link")
            continue
        if info.st_uid not in (0, uid):
            problems.append(f"{shown}: owned by uid {info.st_uid}")
        if writable_by_others(info, uid):
            problems.append(f"{shown}: writable by the group or others")
    return problems + ancestor_problems(folder, uid)


def _read_source_file(path: Path, expected: os.stat_result) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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


def _write_new_file(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def copy_tree_verified(source: Path, target: Path) -> int:
    """Copy the onedir tree ``source`` into the existing, empty folder ``target``.

    Symbolic links and files with more than one hard link are refused, so
    nothing outside ``source`` is copied. Folders become 0700; a file keeps
    its owner bits (the executable stays executable) and gets no group or
    other bits. Every file is read once, written, read back and compared by
    SHA-256 with the bytes that were read.

    Returns:
        The number of files copied.
    """
    root_info = os.lstat(source)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise LayoutError(f"{source} is not a plain folder")
    count = 0
    for path, relative, info in walk(source):
        if not relative:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise LayoutError(f"{relative} in {source} is a symbolic link")
        destination = target.joinpath(*relative.split("/"))
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(destination, DIR_MODE)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise LayoutError(f"{relative} in {source} is not a regular file")
        if info.st_nlink > 1:
            raise LayoutError(f"{relative} in {source} has more than one hard link")
        data = _read_source_file(path, info)
        digest = hashlib.sha256(data).digest()
        _write_new_file(destination, data, (stat.S_IMODE(info.st_mode) & 0o700) | 0o400)
        if hashlib.sha256(destination.read_bytes()).digest() != digest:
            raise LayoutError(f"the copy of {relative} does not match the original")
        count += 1
    return count


def new_version_folder(base: Path, version: str) -> Path:
    """Create ``<base>/<version>-<8 hex>`` (0700), never reusing a name."""
    safe = "".join(c if c.isalnum() or c in ".-+" else "_" for c in version) or "0"
    for _attempt in range(16):
        folder = base / f"{safe}-{secrets.token_hex(4)}"
        if private_mkdir(folder):
            return folder
    raise LayoutError(f"could not create a new folder in {base}")


def install_program(source: Path, base: Path, version: str) -> Path:
    """Copy the onedir build into a new version folder below ``base``; returns the folder."""
    base.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    private_mkdir(base)
    info = os.lstat(base)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LayoutError(f"{base} is not a plain folder")
    folder = new_version_folder(base, version)
    try:
        copy_tree_verified(source, folder)
        if not os.access(folder / LINUX_EXE_NAME, os.X_OK):
            raise LayoutError(f"the copy of {LINUX_EXE_NAME} is not executable")
    except BaseException:
        remove_tree(folder)
        raise
    return folder


def remove_version_folders(base: Path, keep: Path | None = None) -> list[str]:
    """Delete every version folder in ``base`` except ``keep``; returns what could not go."""
    left: list[str] = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except FileNotFoundError:
        return left
    for entry in entries:
        path = Path(entry.path)
        if keep is not None and path == keep:
            continue
        try:
            remove_tree(path)
        except OSError as e:
            if e.errno != errno.ENOENT:
                left.append(str(path))
    return left


def remove_program_base(base: Path) -> list[str]:
    """Delete every version folder and then ``base`` itself."""
    left = remove_version_folders(base)
    with contextlib.suppress(FileNotFoundError, OSError):
        os.rmdir(base)
    return left
