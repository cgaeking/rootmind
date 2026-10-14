"""Put the packaged backend where the release build of the app picks it up.

The release overlay ``desktop/src-tauri/tauri.release.conf.json`` maps
``desktop/src-tauri/binaries/rootmind-backend/`` to the app's ``backend``
folder (``$INSTDIR\\backend\\`` on Windows, ``/usr/lib/RootMind/backend/``
in the Linux packages). This script copies a PyInstaller onedir build
(``rootmind-backend.exe``, on Linux ``rootmind-backend``, and ``_internal/``)
there and checks that the copy is the build that was tested:

- the tree hash of the copy equals the source's, and ``--expect-tree-sha256``
  when given (the hash ``backend-exe.yml`` reports for its build);
- the tree is not a placeholder: at least ``--min-size-mb`` megabytes;
- ``rootmind-backend[.exe] --version`` runs and prints the version in
  ``backend/pyproject.toml``.

The tree hash is SHA-256 over the sorted lines ``<relative path>\\t<sha256 of
the file>\\n`` of every file, with ``/`` as the separator and UTF-8 text, the
same as ``backend/packaging/tree_hash.py``. Standard library only::

    python scripts/prepare_backend.py                      # from backend/dist/rootmind-backend
    python scripts/prepare_backend.py --from DIR --expect-tree-sha256 HASH
    python scripts/prepare_backend.py --tree-hash DIR      # print the hash and exit
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from check_versions import ROOT, VersionError, normalize_version

EXE_NAME = "rootmind-backend.exe"
LINUX_EXE_NAME = "rootmind-backend"
DEFAULT_SOURCE = ROOT / "backend" / "dist" / "rootmind-backend"
DEFAULT_DEST = ROOT / "desktop" / "src-tauri" / "binaries" / "rootmind-backend"
# A real build is about 80 MB; anything far below that is a placeholder or a
# partial copy.
DEFAULT_MIN_SIZE_MB = 20.0
VERSION_TIMEOUT_SECONDS = 120


class PrepareError(Exception):
    """A check failed; the message says which."""


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
            lines.append(f"{path.relative_to(root).as_posix()}\t{file_sha256(path)}\n")
    return sorted(lines)


def tree_sha256(root: Path) -> str:
    """The tree hash of ``root`` (directories themselves do not count)."""
    return hashlib.sha256("".join(tree_lines(root)).encode("utf-8")).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(path)


def exe_name(root: Path) -> str:
    """The executable's name in the build ``root``: Windows' if present, else Linux'."""
    if (root / EXE_NAME).is_file() or not (root / LINUX_EXE_NAME).is_file():
        return EXE_NAME
    return LINUX_EXE_NAME


def check_tree(root: Path) -> tuple[int, int]:
    """Check that ``root`` is a onedir build without links; returns (files, bytes)."""
    if not root.is_dir() or _is_link(root):
        raise PrepareError(f"{root} is not a folder")
    if not (root / exe_name(root)).is_file():
        raise PrepareError(f"{root} has no {EXE_NAME} (Linux: {LINUX_EXE_NAME})")
    if not (root / "_internal").is_dir():
        raise PrepareError(f"{root} is not a onedir build (no _internal folder)")
    count = size = 0
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            if _is_link(Path(folder) / name):
                raise PrepareError(f"{Path(folder) / name} is a link; a build has none")
        for name in files:
            count += 1
            size += (Path(folder) / name).stat().st_size
    return count, size


def expected_version(root: Path = ROOT) -> str:
    """The backend version from ``backend/pyproject.toml``, in PEP 440 normal form."""
    pyproject = tomllib.loads((root / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    return normalize_version(pyproject["project"]["version"])


def exe_version(exe: Path) -> str:
    """Run ``exe --version`` and return the last token it prints."""
    try:
        # A fixed argument list and no shell.
        result = subprocess.run(  # noqa: S603
            [str(exe), "--version"],
            cwd=exe.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PrepareError(f"{exe} --version could not run: {error}") from error
    if result.returncode != 0:
        raise PrepareError(
            f"{exe} --version exited with {result.returncode}: {result.stderr.strip()}"
        )
    tokens = result.stdout.split()
    if not tokens:
        raise PrepareError(f"{exe} --version printed nothing")
    return tokens[-1]


def replace_destination(dest: Path) -> None:
    """Remove a previous copy at ``dest``; refuse anything that is not one."""
    if _is_link(dest):
        raise PrepareError(f"{dest} is a link; remove it by hand")
    if not dest.exists():
        return
    if not dest.is_dir():
        raise PrepareError(f"{dest} exists and is not a folder")
    if any(dest.iterdir()) and not (dest / exe_name(dest)).is_file():
        raise PrepareError(
            f"{dest} is not empty and holds no {EXE_NAME} (Linux: {LINUX_EXE_NAME}); refusing "
            "to replace it"
        )
    shutil.rmtree(dest)


def prepare(
    source: Path,
    dest: Path,
    *,
    expect_tree_sha256: str | None,
    min_size_mb: float,
    version: str,
) -> str:
    """Copy ``source`` to ``dest`` and run every check; returns the tree hash."""
    count, size = check_tree(source)
    if size < min_size_mb * 1_000_000:
        raise PrepareError(
            f"{source} holds {size / 1_000_000:.1f} MB in {count} files, less than {min_size_mb:g} MB"
        )
    source_hash = tree_sha256(source)
    if expect_tree_sha256 and source_hash != expect_tree_sha256.lower():
        raise PrepareError(f"tree hash of {source} is {source_hash}, expected {expect_tree_sha256}")

    replace_destination(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    copied_hash = tree_sha256(dest)
    if copied_hash != source_hash:
        raise PrepareError(
            f"the copy at {dest} has tree hash {copied_hash}, the source {source_hash}"
        )

    name = exe_name(dest)
    printed = exe_version(dest / name)
    try:
        matches = normalize_version(printed) == version
    except VersionError:
        matches = False
    if not matches:
        raise PrepareError(f"{name} --version printed {printed!r}, expected {version}")

    print(f"Copied {count} files ({size / 1_000_000:.1f} MB) to {dest}")
    print(f"Tree SHA-256: {copied_hash}")
    print(f"Version: {printed}")
    return copied_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy the onedir backend build into the app's release resources and check it."
    )
    parser.add_argument(
        "--tree-hash", type=Path, metavar="DIR", help="print the tree hash of DIR and exit"
    )
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="the onedir build (default: %(default)s)",
    )
    parser.add_argument(
        "--expect-tree-sha256", metavar="HASH", help="the tree hash the build must have"
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=argparse.SUPPRESS)
    parser.add_argument(
        "--min-size-mb", type=float, default=DEFAULT_MIN_SIZE_MB, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    if args.tree_hash is not None:
        if not args.tree_hash.is_dir():
            print(f"prepare_backend: {args.tree_hash} is not a folder", file=sys.stderr)
            return 2
        print(tree_sha256(args.tree_hash))
        return 0

    try:
        # absolute(), not resolve(): a link at the destination must be seen, not followed.
        prepare(
            args.source.absolute(),
            args.dest.absolute(),
            expect_tree_sha256=args.expect_tree_sha256,
            min_size_mb=args.min_size_mb,
            version=expected_version(),
        )
    except (PrepareError, VersionError, OSError) as error:
        print(f"prepare_backend: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
