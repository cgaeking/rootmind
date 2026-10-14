"""Shared setup for the tests of the release scripts in ``scripts/``.

The scripts are standalone files, not a package. Their folder goes on
``sys.path``, so the tests can import them by name, the same way the scripts
import each other. Run from the backend's development environment::

    cd backend && uv run pytest ../scripts/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def make_repo(tmp_path):
    """Build a minimal repository with the four files that carry the version."""

    def make(
        backend: str = "1.2.0", package: str = "1.2.0", cargo: str = "1.2.0", mcpb: str = "1.2.0"
    ) -> Path:
        root = tmp_path / "repo"
        (root / "backend").mkdir(parents=True, exist_ok=True)
        (root / "desktop" / "src-tauri").mkdir(parents=True, exist_ok=True)
        (root / "backend" / "pyproject.toml").write_text(
            f'[project]\nname = "rootmind-backend"\nversion = "{backend}"\n', encoding="utf-8"
        )
        (root / "desktop" / "package.json").write_text(
            json.dumps({"name": "rootmind-desktop", "version": package}), encoding="utf-8"
        )
        (root / "desktop" / "src-tauri" / "Cargo.toml").write_text(
            f'[package]\nname = "rootmind-desktop"\nversion = "{cargo}"\n', encoding="utf-8"
        )
        (root / "packaging" / "mcpb").mkdir(parents=True, exist_ok=True)
        (root / "packaging" / "mcpb" / "manifest.json").write_text(
            json.dumps({"manifest_version": "0.3", "name": "rootmind", "version": mcpb}),
            encoding="utf-8",
        )
        return root

    return make


@pytest.fixture
def onedir(tmp_path) -> Path:
    """A small stand-in for the PyInstaller onedir build, with the dot files a real one has."""
    root = tmp_path / "dist" / "rootmind-backend"
    files = {
        "rootmind-backend.exe": b"MZ" + bytes(range(256)) * 8,
        "_internal/python312.dll": b"dll" * 1000,
        "_internal/base_library.zip": b"PK" * 500,
        "_internal/docx/templates/default-docx-template/_rels/.rels": b"<Relationships/>",
        "_internal/anthropic/lib/.keep": b"",
        "_internal/openai/lib/.keep": b"",
        "_internal/certifi/cacert.pem": b"certificates\n",
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "_internal" / "empty").mkdir()
    return root
