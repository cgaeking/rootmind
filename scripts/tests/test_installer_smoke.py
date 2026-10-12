"""desktop/scripts/installer-smoke.ps1: it parses, and its tree hash equals the Python one.

The smoke test itself installs the app, so it only runs in the release
workflow. These tests need PowerShell (pwsh, or Windows PowerShell) and are
skipped without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import prepare_backend
import pytest
from conftest import ROOT

SCRIPT = ROOT / "desktop" / "scripts" / "installer-smoke.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")


def powershell(command: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **env},
        timeout=120,
        check=False,
    )


def test_script_parses():
    result = powershell(
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile($env:SMOKE_SCRIPT, [ref]$null, [ref]$errors)"
        " | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { $_.ToString() }; exit 1 }",
        SMOKE_SCRIPT=str(SCRIPT),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_script_is_ascii_with_crlf():
    """Windows PowerShell 5.1 reads a file without a byte order mark in the ANSI code page."""
    data = SCRIPT.read_bytes()
    assert data.isascii()
    assert data.count(b"\n") == data.count(b"\r\n")


def test_tree_hash_matches_python(onedir):
    (onedir / "_internal" / "données-ä.txt").write_bytes("é".encode())
    result = powershell(
        "$ErrorActionPreference = 'Stop'; "
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SMOKE_SCRIPT, [ref]$null, [ref]$null); "
        "$function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        " -and $node.Name -eq 'Get-TreeSha256' }, $true); "
        ". ([scriptblock]::Create($function.Extent.Text)); "
        "Get-TreeSha256 $env:SMOKE_TREE",
        SMOKE_SCRIPT=str(SCRIPT),
        SMOKE_TREE=str(onedir),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == prepare_backend.tree_sha256(onedir)
