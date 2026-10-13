"""Local filesystem connector.

Without a folder access policy (the command line and the MCP servers) the
connector uses the file system like any program of the user.

Under a policy (the Windows background service, which runs as LocalSystem)
it never follows a link: paths are resolved lexically instead of with
``Path.resolve()``, every component below the indexed folder is examined with
``lstat`` before a file is used, and the walk lists folders with ``scandir``
without looking at the targets of links. Links, junctions and other
redirecting reparse points are skipped. Following one could make the service
open a network share, which authenticates with the computer's account.
"""

import errno
import fnmatch
import hashlib
import logging
import os
import stat
from collections.abc import Callable, Collection, Iterator
from datetime import datetime
from pathlib import Path

from ..access import UNRESTRICTED, FolderAccess
from .base import BaseConnector, FileInfo, FileMetadata

logger = logging.getLogger(__name__)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_NAME_SURROGATE_BIT = 0x20000000  # IsReparseTagNameSurrogate: links, junctions, mount points

LINK_NOT_FOLLOWED = "links are not followed by the background service"


class LinkNotFollowed(PermissionError):  # noqa: N818 (reads as a reason, like PermissionError)
    """A path passes through a link, which is not followed under a folder access policy."""


def _is_link(info: os.stat_result) -> bool:
    """Whether an ``lstat`` result is a link: a symbolic link, junction or mount point."""
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    tag = getattr(info, "st_reparse_tag", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and bool(tag & _NAME_SURROGATE_BIT)


def _entry_lstat(entry: os.DirEntry) -> os.stat_result:
    """``lstat`` of a directory entry; on Windows it comes from the listing itself."""
    return entry.stat(follow_symlinks=False)


Walk = Iterator[tuple[Path, list[str], list[tuple[str, os.stat_result | None]]]]


class LocalConnector(BaseConnector):
    """Connector for local filesystem documents."""

    def __init__(
        self,
        name: str,
        root_path: str | Path,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        protected_dirs: Collection[str | Path] = (),
        access: FolderAccess = UNRESTRICTED,
    ):
        """Initialize local connector.

        Args:
            name: Unique name for this connector.
            root_path: Root directory path for documents.
            include_patterns: Glob patterns for files to include.
            exclude_patterns: Glob patterns for files and folders to exclude.
            protected_dirs: Folders that are never indexed, such as RootMind's
                own data folder (index, MCP token). A root inside one of them
                is rejected; one inside the root is skipped while walking.
            access: Folder access policy (see :mod:`rootmind.access`). It
                checks the root here, every subfolder before the walk enters
                it, and opens every file that is read. The default reads with
                the process's own rights.

        Raises:
            FolderNotAllowed: the policy refuses the folder.
            FileNotFoundError, OSError: the policy could not open the folder.
            ValueError: the folder does not exist, is not a directory, or lies
                inside RootMind's data folder.
        """
        super().__init__(name, include_patterns, exclude_patterns)
        self.access = access
        self.root_path = access.check_root(root_path)

        if not self._exists(self.root_path):
            raise ValueError(f"Root path does not exist: {self.root_path}")
        if not self._is_folder(self.root_path):
            raise ValueError(f"Root path is not a directory: {self.root_path}")

        protected = [Path(p).resolve() for p in protected_dirs]
        for folder in protected:
            if self.root_path.is_relative_to(folder):
                raise ValueError(f"Root path is inside RootMind's data folder: {self.root_path}")
        self._protected = {os.path.normcase(str(p)) for p in protected}

    @property
    def _restricted(self) -> bool:
        return self.access is not UNRESTRICTED

    # --- paths ----------------------------------------------------------------------

    def _resolve_path(self, path: str) -> Path:
        """Resolve a relative path to absolute path within root."""
        if not path:
            return self.root_path

        if self._restricted:
            # Lexical only: resolve() opens the path and follows links. The
            # components are examined with lstat before the path is used.
            resolved = Path(os.path.normpath(self.root_path / path))
        else:
            # resolve() follows symlinks and collapses "..", so the containment
            # check below sees the real target.
            resolved = (self.root_path / path).resolve()

        # A component-wise check: a plain string-prefix test would accept a
        # sibling such as "../docs-secret" for the root ".../docs".
        if not resolved.is_relative_to(self.root_path):
            raise ValueError(f"Path {path} is outside root directory")

        return resolved

    def resolve_path(self, path: str) -> Path:
        """Resolve a stored relative document path to its absolute location on disk.

        Raises ``ValueError`` if the resolved path would escape the connector root.
        """
        return self._resolve_path(path)

    def _lstat_below_root(self, target: Path) -> os.stat_result:
        """``lstat`` of ``target`` (at or below the root), refusing links on the way.

        Each component below the root is examined in order, without following
        it, so no link is ever passed through. The root itself was checked by
        the policy; only a symbolic link there is refused (a junction or mount
        point as the root is allowed by the policy).

        Raises:
            LinkNotFollowed: a component is a link.
            FileNotFoundError, NotADirectoryError: a component does not exist.
            OSError: a component cannot be examined.
        """
        info = os.lstat(self.root_path)
        if stat.S_ISLNK(info.st_mode):
            raise LinkNotFollowed(errno.EACCES, LINK_NOT_FOLLOWED, str(self.root_path))
        current = self.root_path
        for part in target.relative_to(self.root_path).parts:
            current = current / part
            info = os.lstat(current)
            if _is_link(info):
                raise LinkNotFollowed(errno.EACCES, LINK_NOT_FOLLOWED, str(current))
        return info

    def _exists(self, path: Path) -> bool:
        if not self._restricted:
            return path.exists()
        try:
            self._lstat_below_root(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    def _is_folder(self, path: Path) -> bool:
        if not self._restricted:
            return path.is_dir()
        try:
            return stat.S_ISDIR(self._lstat_below_root(path).st_mode)
        except OSError:
            return False

    def _file_info(self, resolved: Path, path: str) -> os.stat_result:
        """``lstat`` of a file under a policy.

        Raises:
            FileNotFoundError: it does not exist.
            LinkNotFollowed: it, or a folder on the way, is a link.
            ValueError: it is not a regular file.
        """
        try:
            info = self._lstat_below_root(resolved)
        except (FileNotFoundError, NotADirectoryError):
            raise FileNotFoundError(f"File not found: {path}") from None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Not a file: {path}")
        return info

    def _get_relative_path(self, absolute_path: Path) -> str:
        """Get relative path from root."""
        return str(absolute_path.relative_to(self.root_path))

    def is_available(self) -> bool:
        """True while the root folder exists (False for an unplugged drive or offline share)."""
        try:
            return self._is_folder(self.root_path)
        except OSError:
            return False

    # --- listing ----------------------------------------------------------------------

    def _keep_dir(self, path: Path) -> bool:
        """Whether the walk descends into ``path``."""
        name = path.name
        if any(fnmatch.fnmatch(name, pattern.rstrip("/\\")) for pattern in self.exclude_patterns):
            return False
        # os.walk lists symlinks and NTFS junctions as directories. Following
        # them would list files outside the root (which reads then reject) or,
        # for a link to an ancestor, re-walk the tree at every level.
        if os.path.islink(path) or os.path.isjunction(path):
            return False
        if os.path.normcase(str(path)) in self._protected:
            return False
        return self.access.may_descend(path, self.root_path)

    def _walk(self, start: Path, on_error: Callable[[OSError], None]) -> Walk:
        """Yield ``(folder, subfolder names, [(file name, lstat or None)])``, top-down.

        Like :func:`os.walk`, the caller prunes the subfolder list in place.
        Without a policy this is :func:`os.walk` (file information is read
        later). Under a policy every entry is examined without following it,
        and links are left out; :func:`os.walk` would look at their targets.
        """
        if not self._restricted:
            for root, dirs, files in os.walk(start, onerror=on_error):
                yield Path(root), dirs, [(name, None) for name in files]
            return

        pending = [start]
        while pending:
            folder = pending.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = list(listing)
            except OSError as error:
                on_error(error)
                continue
            dirs: list[str] = []
            files: list[tuple[str, os.stat_result | None]] = []
            for entry in entries:
                try:
                    info = _entry_lstat(entry)
                except OSError:
                    continue
                if _is_link(info):
                    continue  # never followed, never listed
                if stat.S_ISDIR(info.st_mode):
                    dirs.append(entry.name)
                else:
                    files.append((entry.name, info))
            yield folder, dirs, files
            pending.extend(folder / name for name in reversed(dirs))

    def _unrestricted_file_info(self, file_path: Path) -> os.stat_result | None:
        """``stat`` of a listed file without a policy; None to leave it out."""
        try:
            # A file symlink that points outside the root is not listed;
            # reading it would be rejected anyway.
            if os.path.islink(file_path) and not file_path.resolve().is_relative_to(self.root_path):
                return None
            return file_path.stat()
        except OSError:
            return None  # skip files we can't access

    def list_files_recursive(self, path: str = "") -> Iterator[FileInfo]:
        """Recursively list all files matching the include and exclude patterns.

        Raises:
            FileNotFoundError: the folder does not exist (any more), for example
                an unplugged drive or an offline network share.
            OSError: the folder itself cannot be read.

        A folder that cannot be listed must never look empty: the sync would
        then remove its whole index. Unreadable subfolders are logged and
        skipped.
        """
        start = self._resolve_path(path)
        if not self._is_folder(start):
            raise FileNotFoundError(f"Folder not found: {start}")

        def on_error(error: OSError) -> None:
            if error.filename is not None and Path(error.filename) == start:
                raise error
            logger.warning("Skipping unreadable folder %s: %s", error.filename, error)

        for root_path, dirs, files in self._walk(start, on_error):
            dirs[:] = [d for d in dirs if self._keep_dir(root_path / d)]

            for file_name, listed in files:
                file_path = root_path / file_name
                relative_path = self._get_relative_path(file_path)

                if not self.should_include(relative_path):
                    continue

                info = listed if listed is not None else self._unrestricted_file_info(file_path)
                if info is None or not stat.S_ISREG(info.st_mode):
                    continue
                yield FileInfo(
                    path=relative_path,
                    name=file_name,
                    size_bytes=info.st_size,
                    modified_at=datetime.fromtimestamp(info.st_mtime),
                    is_directory=False,
                )

    # --- files ----------------------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        """Read file contents."""
        resolved = self._resolve_path(path)

        if self._restricted:
            self._file_info(resolved, path)
        else:
            if not resolved.exists():
                raise FileNotFoundError(f"File not found: {path}")
            if not resolved.is_file():
                raise ValueError(f"Not a file: {path}")

        with self.access.open_file(resolved, self.root_path) as f:
            return f.read()

    def get_metadata(self, path: str) -> FileMetadata:
        """Get extended metadata for a file."""
        resolved = self._resolve_path(path)

        if self._restricted:
            info = self._file_info(resolved, path)
        else:
            if not resolved.exists():
                raise FileNotFoundError(f"File not found: {path}")
            info = resolved.stat()

        # Calculate content hash
        content_hash = self._calculate_hash(resolved)

        return FileMetadata(
            path=self._get_relative_path(resolved),
            name=resolved.name,
            extension=resolved.suffix,
            size_bytes=info.st_size,
            created_at=datetime.fromtimestamp(info.st_ctime),
            modified_at=datetime.fromtimestamp(info.st_mtime),
            content_hash=content_hash,
        )

    def _calculate_hash(self, file_path: Path, chunk_size: int = 8192) -> str:
        """Calculate SHA256 hash of file contents."""
        sha256 = hashlib.sha256()

        with self.access.open_file(file_path, self.root_path) as f:
            while chunk := f.read(chunk_size):
                sha256.update(chunk)

        return sha256.hexdigest()

    def file_exists(self, path: str) -> bool:
        """Check if a file exists.

        Under a policy a link (or a file reached through one) counts as
        missing: the walk never lists it, so its index entry is stale.
        """
        try:
            resolved = self._resolve_path(path)
        except ValueError:
            return False
        if not self._restricted:
            return resolved.exists() and resolved.is_file()
        try:
            return stat.S_ISREG(self._lstat_below_root(resolved).st_mode)
        except (FileNotFoundError, NotADirectoryError, LinkNotFollowed):
            return False

    def status(self) -> dict:
        """Get connector status information."""
        base_status = super().status()

        # Count files
        try:
            file_count = sum(1 for _ in self.list_files_recursive())
        except Exception:
            file_count = -1

        return {
            **base_status,
            "root_path": str(self.root_path),
            "exists": self.is_available(),
            "file_count": file_count,
        }
