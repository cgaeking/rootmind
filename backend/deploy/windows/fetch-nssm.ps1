# Fetches NSSM 2.24 (the Windows service wrapper used by _service-backend.ps1)
# and verifies it against a pinned SHA-256 before it is used.
#
# NSSM is public domain (https://nssm.cc). The binary is not committed to the
# repository; the manual installer build (ARCHITECTURE.md#packaging) and
# install-backend-service.bat call this script instead.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File fetch-nssm.ps1 [-OutDir <dir>] [-FromFile <nssm.exe>]
#
#   -OutDir    Target folder for nssm.exe (default: .\tools next to this script)
#   -FromFile  Verify and copy an existing nssm.exe instead of downloading

param(
    [string]$OutDir = (Join-Path $PSScriptRoot "tools"),
    [string]$FromFile
)

$ErrorActionPreference = "Stop"

$Url = "https://nssm.cc/release/nssm-2.24.zip"
# Matched by suffix so the archive's top-level folder name does not matter.
$ZipEntrySuffix = "win64/nssm.exe"
$ExpectedSha256 = "f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97"  # pragma: allowlist secret (public NSSM hash)

function Test-Hash {
    param([string]$Path)
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
    return ($actual -eq $ExpectedSha256)
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$Target = Join-Path $OutDir "nssm.exe"
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("nssm-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Work -Force | Out-Null

try {
    if ($FromFile) {
        if (-not (Test-Path $FromFile)) { Write-Host "[ERROR] File not found: $FromFile" -ForegroundColor Red; exit 1 }
        $candidate = Join-Path $Work "nssm.exe"
        Copy-Item -Path $FromFile -Destination $candidate
    } else {
        $zip = Join-Path $Work "nssm.zip"
        Write-Host "[*] Downloading $Url"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
        try {
            $entry = $archive.Entries | Where-Object { $_.FullName.Replace('\', '/').EndsWith($ZipEntrySuffix) } | Select-Object -First 1
            if (-not $entry) { Write-Host "[ERROR] $ZipEntrySuffix not found in the archive" -ForegroundColor Red; exit 1 }
            $candidate = Join-Path $Work "nssm.exe"
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $candidate, $true)
        } finally {
            $archive.Dispose()
        }
    }

    if (-not (Test-Hash -Path $candidate)) {
        Remove-Item -Force $candidate
        Write-Host "[ERROR] SHA-256 mismatch for nssm.exe; the file was discarded." -ForegroundColor Red
        exit 1
    }

    Copy-Item -Path $candidate -Destination $Target -Force
    Write-Host "[OK] nssm.exe verified (SHA-256 $ExpectedSha256)" -ForegroundColor Green
    Write-Host "     $Target"
    exit 0
} finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
