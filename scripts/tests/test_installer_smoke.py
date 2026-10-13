"""desktop/scripts/installer-smoke.ps1: it parses, its helpers work, and it refuses bad arguments.

The smoke test itself installs the app, so it only runs in the release
workflow. These tests run its tree hash (which must equal the Python one) and
its stale-file helper on their own, and the script only up to its argument
guards, which refuse before anything is installed. They need PowerShell (pwsh,
or Windows PowerShell) and are skipped without it.
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


def test_stale_backend_files_are_what_an_older_version_leaves(tmp_path):
    result = powershell(
        "$ErrorActionPreference = 'Stop'; $OlderVersion = '0.0.1'; "
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SMOKE_SCRIPT, [ref]$null, [ref]$null); "
        "$function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        " -and $node.Name -eq 'Add-StaleBackendFiles' }, $true); "
        ". ([scriptblock]::Create($function.Extent.Text)); "
        "Add-StaleBackendFiles $env:SMOKE_TREE",
        SMOKE_SCRIPT=str(SCRIPT),
        SMOKE_TREE=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    info = tmp_path / "_internal" / "rootmind_backend-0.0.1.dist-info"
    assert result.stdout.splitlines() == [str(info), str(tmp_path / "stale.txt")]
    assert "Version: 0.0.1" in (info / "METADATA").read_text(encoding="utf-8")


def test_wrong_arguments_are_refused_before_anything_is_installed(tmp_path):
    result = powershell(
        "& $env:SMOKE_SCRIPT -Setup $env:SMOKE_SETUP -Version 1.2.0 -BackendTreeSha256 ('0' * 64) "
        "-Publisher x -Identifier '..'; exit $LASTEXITCODE",
        SMOKE_SCRIPT=str(SCRIPT),
        SMOKE_SETUP=str(tmp_path / "missing-setup.exe"),
        CI="true",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "-Identifier" in result.stdout
