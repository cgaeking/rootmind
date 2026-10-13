"""Print the tree hash of a folder (the onedir build), for release checks.

The hash is SHA-256 over the sorted lines ``<relative path>\\t<sha256 of the
file>\\n`` of every file in the tree, with ``/`` as the path separator and the
text encoded as UTF-8. Folders themselves (and empty folders) do not count.
``scripts/prepare_backend.py`` computes the same hash to check that the
installer ships exactly the tested build. Standard library only::

    python packaging/tree_hash.py dist/rootmind-backend
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_lines(root: Path) -> list[str]:
    """The sorted ``<relative path>\\t<sha256>\\n`` lines of every file below ``root``."""
    lines = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            lines.append(f"{relative}\t{file_sha256(path)}\n")
    return sorted(lines)


def tree_sha256(root: Path) -> str:
    """The tree hash of ``root``."""
    return hashlib.sha256("".join(tree_lines(root)).encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not Path(args[0]).is_dir():
        print("usage: tree_hash.py FOLDER", file=sys.stderr)
        return 2
    print(tree_sha256(Path(args[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
