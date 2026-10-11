"""Local filesystem connector."""

import fnmatch
import hashlib
import logging
import os
import stat
from collections.abc import Collection, Iterator
from datetime import datetime
from pathlib import Path

from .base import BaseConnector, FileInfo, FileMetadata

logger = logging.getLogger(__name__)


class LocalConnector(BaseConnector):
    """Connector for local filesystem documents."""

    def __init__(
        self,
        name: str,
        root_path: str | Path,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        protected_dirs: Collection[str | Path] = (),
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
        """
        super().__init__(name, include_patterns, exclude_patterns)
        self.root_path = Path(root_path).resolve()

        if not self.root_path.exists():
            raise ValueError(f"Root path does not exist: {self.root_path}")
        if not self.root_path.is_dir():
            raise ValueError(f"Root path is not a directory: {self.root_path}")

        protected = [Path(p).resolve() for p in protected_dirs]
        for folder in protected:
            if self.root_path.is_relative_to(folder):
                raise ValueError(f"Root path is inside RootMind's data folder: {self.root_path}")
        self._protected = {os.path.normcase(str(p)) for p in protected}

    def _resolve_path(self, path: str) -> Path:
        """Resolve a relative path to absolute path within root."""
        if not path:
            return self.root_path

        # resolve() follows symlinks and collapses "..", so the containment
        # check below sees the real target. A component-wise check is needed: a
        # plain string-prefix test would accept a sibling such as "../docs-secret"
        # for the root ".../docs".
        resolved = (self.root_path / path).resolve()

        if not resolved.is_relative_to(self.root_path):
            raise ValueError(f"Path {path} is outside root directory")

        return resolved

    def resolve_path(self, path: str) -> Path:
        """Resolve a stored relative document path to its absolute location on disk.

        Raises ``ValueError`` if the resolved path would escape the connector root.
        """
        return self._resolve_path(path)

    def _get_relative_path(self, absolute_path: Path) -> str:
        """Get relative path from root."""
        return str(absolute_path.relative_to(self.root_path))

    def is_available(self) -> bool:
        """True while the root folder exists (False for an unplugged drive or offline share)."""
        try:
            return self.root_path.is_dir()
        except OSError:
            return False

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
        return os.path.normcase(str(path)) not in self._protected

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
        if not start.is_dir():
            raise FileNotFoundError(f"Folder not found: {start}")

        def on_error(error: OSError) -> None:
            if error.filename is not None and Path(error.filename) == start:
                raise error
            logger.warning("Skipping unreadable folder %s: %s", error.filename, error)

        for root, dirs, files in os.walk(start, onerror=on_error):
            root_path = Path(root)
            dirs[:] = [d for d in dirs if self._keep_dir(root_path / d)]

            for file_name in files:
                file_path = root_path / file_name
                relative_path = self._get_relative_path(file_path)

                if not self.should_include(relative_path):
                    continue

                try:
                    # A file symlink that points outside the root is not listed;
                    # reading it would be rejected anyway.
                    if os.path.islink(file_path) and not file_path.resolve().is_relative_to(
                        self.root_path
                    ):
                        continue
                    info = file_path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    yield FileInfo(
                        path=relative_path,
                        name=file_name,
                        size_bytes=info.st_size,
                        modified_at=datetime.fromtimestamp(info.st_mtime),
                        is_directory=False,
                    )
                except OSError:
                    # Skip files we can't access
                    continue

    def read_file(self, path: str) -> bytes:
        """Read file contents."""
        resolved = self._resolve_path(path)

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {path}")

        if not resolved.is_file():
            raise ValueError(f"Not a file: {path}")

        return resolved.read_bytes()

    def get_metadata(self, path: str) -> FileMetadata:
        """Get extended metadata for a file."""
        resolved = self._resolve_path(path)

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

        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                sha256.update(chunk)

        return sha256.hexdigest()

    def file_exists(self, path: str) -> bool:
        """Check if a file exists."""
        try:
            resolved = self._resolve_path(path)
            return resolved.exists() and resolved.is_file()
        except ValueError:
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
