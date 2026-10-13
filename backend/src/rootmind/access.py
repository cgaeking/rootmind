"""Folder access policy: which folders and files the indexer may read.

The local connector asks a :class:`FolderAccess` policy before it uses an
indexed folder, before the walk enters a subfolder, and to open every file it
reads. The command line (``rootmind api``, ``index``) and the MCP servers use
:data:`UNRESTRICTED`, which is plain filesystem access with the process's own rights. The Windows
background service runs as LocalSystem and passes a policy that confines reads
to what the installing account could read without elevation
(``rootmind.winaccess.WindowsUserAccess``).

Contract for implementations:

- ``check_root(path)`` returns the canonical absolute path of an indexed
  folder, in the form :meth:`pathlib.Path.resolve` gives (on Windows a plain
  drive path such as ``C:\\Users\\me\\Docs``, without a ``\\\\?\\`` prefix). It
  raises :class:`FolderNotAllowed` with a short user-facing reason when the
  policy refuses the folder, :class:`FileNotFoundError` when it does not
  exist, or another :class:`OSError` when it cannot be opened. The connector
  stores the returned path as its root and passes it back as ``root`` below.
- ``may_descend(directory, root)`` says whether the walk enters ``directory``,
  a folder found below ``root``. It must not raise; return False for anything
  that cannot be checked.
- ``open_file(path, root)`` opens ``path`` for binary reading and returns the
  open file. It raises :class:`PermissionError` when the policy does not allow
  reading it, and :class:`ValueError` when the opened file lies outside
  ``root``. The caller closes the returned file.

Implementations must be safe to call from several threads.
"""

from pathlib import Path
from typing import BinaryIO, Protocol


class FolderNotAllowed(ValueError):  # noqa: N818 (reads as a reason, like PermissionError)
    """The access policy refuses a folder. ``str(e)`` is a short, user-facing reason."""


class FolderAccess(Protocol):
    """Decides which folders and files the indexer may read (see the module docstring)."""

    def check_root(self, path: str | Path) -> Path:
        """Canonical path of an indexed folder; raises FolderNotAllowed, FileNotFoundError or OSError."""
        ...

    def may_descend(self, directory: Path, root: Path) -> bool:
        """True if the walk may enter ``directory`` (below ``root``)."""
        ...

    def open_file(self, path: Path, root: Path) -> BinaryIO:
        """Open ``path`` for binary reading; PermissionError if not allowed, ValueError if outside ``root``."""
        ...


class Unrestricted:
    """No policy: the process reads with its own rights (command line and MCP servers)."""

    def check_root(self, path: str | Path) -> Path:
        return Path(path).resolve()

    def may_descend(self, directory: Path, root: Path) -> bool:
        return True

    def open_file(self, path: Path, root: Path) -> BinaryIO:
        # The caller closes the file.
        return open(path, "rb")

    def __repr__(self) -> str:
        return "UNRESTRICTED"


UNRESTRICTED = Unrestricted()
