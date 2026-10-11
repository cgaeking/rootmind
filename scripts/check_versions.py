"""Check that the backend and the desktop app carry the same version number.

The version lives in three files: ``backend/pyproject.toml``,
``desktop/package.json`` (``tauri.conf.json`` reads it from there) and
``desktop/src-tauri/Cargo.toml``. Exits 1 and names the files when they
differ. Standard library only, so it runs with any Python 3.11+::

    python scripts/check_versions.py
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def versions() -> dict[str, str]:
    """The version declared in each file, keyed by its path relative to the repository."""
    pyproject = tomllib.loads((ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((ROOT / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
    return {
        "backend/pyproject.toml": pyproject["project"]["version"],
        "desktop/package.json": package["version"],
        "desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
    }


def main() -> int:
    found = versions()
    if len(set(found.values())) == 1:
        print(f"All versions match: {next(iter(found.values()))}")
        return 0
    print("Version mismatch:", file=sys.stderr)
    for path, version in found.items():
        print(f"  {path}: {version}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
