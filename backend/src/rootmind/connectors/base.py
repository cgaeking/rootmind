"""Base connector interface for document sources."""

import fnmatch
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class FileInfo:
    """Basic file information."""

    path: str
    name: str
    size_bytes: int
    modified_at: datetime
    is_directory: bool


@dataclass
class FileMetadata:
    """Extended file metadata."""

    path: str
    name: str
    extension: str
    size_bytes: int
    created_at: datetime | None
    modified_at: datetime
    content_hash: str | None = None

    @property
    def file_type(self) -> str:
        """Get normalized file type from extension."""
        return self.extension.lower().lstrip(".")


class BaseConnector(ABC):
    """Abstract base class for document connectors."""

    def __init__(
        self,
        name: str,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ):
        """Initialize the connector.

        Args:
            name: Unique name for this connector.
            include_patterns: Glob patterns for files to include.
            exclude_patterns: Glob patterns for files to exclude.
        """
        self.name = name
        self.include_patterns = include_patterns or ["*"]
        self.exclude_patterns = exclude_patterns or []

    def is_available(self) -> bool:
        """True when the source can be read right now (e.g. the folder exists).

        A sync must not remove the index entries of a source that is only
        temporarily unreachable, such as an unplugged drive.
        """
        return True

    @abstractmethod
    def list_files_recursive(self, path: str = "") -> Iterator[FileInfo]:
        """Recursively list all files.

        Args:
            path: Starting path within the connector's root.

        Yields:
            FileInfo for each file (not directories).

        Raises:
            OSError: the source itself cannot be listed. Implementations must
                raise instead of yielding nothing, so that an unreachable source
                is never mistaken for an empty one.
        """
        pass

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read file contents.

        Args:
            path: Relative path to the file.

        Returns:
            File contents as bytes.
        """
        pass

    @abstractmethod
    def get_metadata(self, path: str) -> FileMetadata:
        """Get extended metadata for a file.

        Args:
            path: Relative path to the file.

        Returns:
            FileMetadata instance.
        """
        pass

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """Check if a file exists.

        Args:
            path: Relative path to the file.

        Returns:
            True if file exists.
        """
        pass

    def should_include(self, file_path: str) -> bool:
        """Check if a file should be included based on patterns.

        Args:
            file_path: Path to check.

        Returns:
            True if file should be included.
        """
        file_name = Path(file_path).name

        # Check exclude patterns first
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(file_name, pattern):
                return False
            # Also check full path for directory patterns
            if fnmatch.fnmatch(file_path, pattern):
                return False

        # Check include patterns
        for pattern in self.include_patterns:
            if fnmatch.fnmatch(file_name, pattern):
                return True

        return False

    def status(self) -> dict:
        """Get connector status information."""
        return {
            "name": self.name,
            "type": self.__class__.__name__,
            "include_patterns": self.include_patterns,
            "exclude_patterns": self.exclude_patterns,
        }
