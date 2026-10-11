# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build the RootMind REST API into a single executable.

Run from the backend directory:
    uv run --group build pyinstaller packaging/rootmind-backend.spec --noconfirm --clean --distpath dist
Output: dist/rootmind-backend.exe (Windows) / dist/rootmind-backend (POSIX).

The MCP servers are not part of the executable (the desktop app only needs the
REST API); run them from a source checkout with `uv run rootmind-mcp`.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas = []
binaries = []
hiddenimports = []

# Packages with data files / dynamic libs / lazy imports PyInstaller can miss.
# Both LLM SDKs are needed: openai also serves the xAI provider.
for pkg in ("anthropic", "openai", "apscheduler", "pydantic", "email_validator"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# rootmind.__version__ reads the installed package metadata.
try:
    datas += copy_metadata("rootmind-backend")
except Exception:
    pass

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

# Break import chains we never take so their heavy deps are not bundled.
excludes = [
    "mcp",
    "matplotlib",
    "tkinter",
    "pytest",
    "IPython",
    "notebook",
    "pandas",
    "scipy",
]

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
    a.binaries,
    a.datas,
    [],
    name="rootmind-backend",
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
)
