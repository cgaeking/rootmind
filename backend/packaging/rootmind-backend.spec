# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build the RootMind backend into a onedir folder.

Run from the backend directory (the release build sets PYTHONHASHSEED=0 and
SOURCE_DATE_EPOCH, and installs with ``uv sync --locked --no-dev --group build``):

    uv run --no-dev --group build pyinstaller packaging/rootmind-backend.spec --noconfirm --clean

Output: ``dist/rootmind-backend/`` with ``rootmind-backend.exe`` and ``_internal/``.
The desktop installer ships this folder as ``backend\\``, and the release zip
``rootmind-backend-windows-x64.zip`` contains it.

The executable carries the whole command line: the REST API (``api``, the
default), ``index``, ``rebuild-trigrams``, both MCP servers (``mcp`` over
stdio, ``mcp-http``) and, on Windows, the background service (``service``,
hosted with pywin32). Smoke test: ``packaging/smoke-test.ps1``.
"""

import os
import re
import sys
from importlib.metadata import metadata

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

DIST_NAME = "rootmind-backend"

datas = []
binaries = []
hiddenimports = []

# Packages with data files, dynamic libraries or lazy imports PyInstaller can
# miss. Both LLM SDKs are needed: openai also serves the xAI provider. Source
# files are left out (include_py_files=False); the bytecode is enough.
for pkg in ("anthropic", "openai", "apscheduler", "pydantic"):
    d, b, h = collect_all(pkg, include_py_files=False)
    datas += d
    binaries += b
    hiddenimports += h

# The MCP SDK serves `mcp` (stdio). Its CLI (and typer) is not needed.
hiddenimports += collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))

# Every RootMind module: some are imported by name at run time (the `service`
# command, the folder access policy).
hiddenimports += collect_submodules("rootmind")

# rootmind.__version__ reads the installed package metadata; a build without
# it would report "0+unknown", so a failure here fails the build.
datas += copy_metadata(DIST_NAME)

# uvicorn resolves its loop/protocol implementations dynamically at runtime.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
]

# The Windows service host (rootmind.service) uses pywin32.
if sys.platform == "win32":
    hiddenimports += [
        "win32timezone",
        "servicemanager",
        "win32serviceutil",
        "win32service",
        "win32event",
    ]

# Break import chains we never take so their heavy deps are not bundled.
# watchfiles is uvicorn's auto-reload (development only).
excludes = [
    "mcp.cli",
    "typer",
    "watchfiles",
    "matplotlib",
    "tkinter",
    "pytest",
    "IPython",
    "notebook",
    "pandas",
    "scipy",
]


def _version_resource():
    """Windows version resource from the installed package's metadata."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    meta = metadata(DIST_NAME)
    version = meta["Version"]
    publisher = meta.get("Author") or ""
    if not publisher:
        # PEP 621 authors given as name only end up in Author; with an email
        # they end up in Author-email as "Name <address>".
        publisher = re.sub(r"\s*<[^>]*>", "", meta.get("Author-email") or "").strip()
    if not publisher:
        raise SystemExit(f"{DIST_NAME} has no author in its metadata (the version resource)")
    numbers = [int(n) for n in re.findall(r"\d+", version.split("+")[0])[:3]]
    numbers += [0] * (4 - len(numbers))
    filevers = tuple(numbers[:4])
    strings = [
        StringStruct("CompanyName", publisher),
        StringStruct("FileDescription", "RootMind backend"),
        StringStruct("FileVersion", version),
        StringStruct("InternalName", DIST_NAME),
        StringStruct("LegalCopyright", f"Copyright (c) {publisher}. MIT License."),
        StringStruct("OriginalFilename", f"{DIST_NAME}.exe"),
        StringStruct("ProductName", "RootMind"),
        StringStruct("ProductVersion", version),
    ]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=filevers, prodvers=filevers),
        kids=[
            StringFileInfo([StringTable("040904B0", strings)]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],  # noqa: F821 (SPECPATH is set by PyInstaller)
    # so `import rootmind` resolves even without an editable install
    pathex=[os.path.join(SPECPATH, "..", "src")],  # noqa: F821
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    # UTF-8 mode for the frozen interpreter: logs and pipes are UTF-8 whatever
    # the environment says (the frozen interpreter ignores PYTHON* variables).
    [("X utf8_mode=1", None, "OPTION")],
    exclude_binaries=True,
    name=DIST_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_resource() if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=DIST_NAME,
)
