# RootMind backend - Windows service manager.
#
# Installs the REST API (http://127.0.0.1:8000) as the background service
# "RootMind-Backend" that the desktop app talks to.
#
# EXPERIMENTAL. Not recommended on machines shared with other users; see the
# security notes below and SECURITY.md.
#
# Two run modes, detected automatically:
#   - Packaged: if rootmind-backend.exe (PyInstaller build) sits in the backend
#     folder (or in backend\dist), the service runs that exe. No Python needed.
#   - Source checkout (development only): otherwise it looks for a Python with
#     the dependencies installed and runs `python -m rootmind.main api`.
#
# Security: the service runs as LocalSystem. Its data folder
# (%ProgramData%\RootMind: config.yaml with API keys, index.db, logs) is
# restricted to SYSTEM and Administrators. The REST API has no authentication
# and binds to 127.0.0.1, so this setup assumes a single-user machine: any
# local process can query the API, including POST /config, which decides
# which folders the service reads with SYSTEM rights. See SECURITY.md.
#
# Everything the service runs as SYSTEM (the exe, NSSM, the Python
# interpreter and the checkout in source mode) must be writable by
# administrators only; -Install refuses otherwise.
#
# Requires: Administrator rights, and tools\nssm.exe (run fetch-nssm.ps1 first;
# it downloads NSSM and verifies its SHA-256).

param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$ServiceName = "RootMind-Backend"
$DisplayName = "RootMind Backend Service"
$Description = "RootMind REST API (127.0.0.1:8000) and document indexing for the desktop app."
$Port = 8000

# This script lives at <backend>\deploy\windows\. <backend> is either a source
# checkout or the installer's bundled backend\ resource folder; both keep the
# same relative layout.
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$NssmPath = Join-Path $PSScriptRoot "tools\nssm.exe"
$ExeCandidates = @(
    (Join-Path $ProjectDir "rootmind-backend.exe"),
    (Join-Path $ProjectDir "dist\rootmind-backend.exe")
)

# Machine-wide, not per-user: the service runs whether or not a user is logged in.
$RlmHome = Join-Path $env:ProgramData "RootMind"
$LogDir = Join-Path $RlmHome "logs"

function Test-Nssm {
    if (Test-Path $NssmPath) { return }
    Write-Host "[ERROR] NSSM not found at $NssmPath" -ForegroundColor Red
    Write-Host "        Run: powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\fetch-nssm.ps1`"" -ForegroundColor Yellow
    exit 1
}

function Test-Administrator {
    $u = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($u)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Microsoft Store Python (WindowsApps) is user-scoped and fails under LocalSystem.
function Test-ServiceSafeVenv {
    param([string]$VenvPython)
    if (-not (Test-Path $VenvPython)) { return $false }
    $cfgFile = Join-Path (Split-Path -Parent (Split-Path -Parent $VenvPython)) "pyvenv.cfg"
    if (-not (Test-Path $cfgFile)) { return $true }
    $cfg = Get-Content $cfgFile -Raw
    if ($cfg -match "WindowsApps" -or $cfg -match "PythonSoftwareFoundation\.Python") {
        Write-Host "[!] The project venv uses Microsoft Store Python, which cannot run as a service." -ForegroundColor Yellow
        return $false
    }
    return $true
}

# SIDs that may write to anything the service runs: SYSTEM, Administrators,
# TrustedInstaller and CREATOR OWNER (which only applies to new child objects).
$TrustedWriterSids = @(
    "S-1-5-18",
    "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    "S-1-3-0"
)

function Test-AdminOnlyWritable {
    # True if only trusted principals may write to $Path. Anything that runs as
    # SYSTEM and can be changed by a normal user hands that user SYSTEM rights.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $writeRights = [Security.AccessControl.FileSystemRights]"WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, ChangePermissions, TakeOwnership"
    # Plus the generic rights GENERIC_WRITE and GENERIC_ALL, which inherited
    # entries can carry instead of the specific bits.
    $writeMask = [int]$writeRights -bor 0x40000000 -bor 0x10000000
    $acl = Get-Acl -LiteralPath $Path
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne "Allow") { continue }
        if (([int]$rule.FileSystemRights -band $writeMask) -eq 0) { continue }
        if ($TrustedWriterSids -notcontains $rule.IdentityReference.Value) { return $false }
    }
    return $true
}

function Test-PythonRuntime {
    param([string]$PythonPath)
    if (-not (Test-Path $PythonPath)) { return $false }
    # The interpreter runs elevated during this check and as SYSTEM later.
    if (-not (Test-AdminOnlyWritable -Path (Split-Path -Parent $PythonPath))) {
        Write-Host "[!] Skipping $PythonPath : its folder is writable by non-administrators." -ForegroundColor Yellow
        return $false
    }
    & $PythonPath -c "import fastapi, uvicorn, pydantic_settings, apscheduler, sqlalchemy, anthropic, openai" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-Python {
    # Only interpreters in admin-only locations: the checkout's venv (the
    # checkout itself is checked in Install-Service) or an all-users install.
    # Per-user installs (%LOCALAPPDATA%) and whatever "python" is on PATH are
    # writable by the user and would run as SYSTEM.
    $venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if ((Test-ServiceSafeVenv -VenvPython $venvPython) -and (Test-PythonRuntime -PythonPath $venvPython)) {
        Write-Host "[i] Using the project venv Python" -ForegroundColor Cyan
        return $venvPython
    }
    $candidates = @(
        "$env:ProgramFiles\Python314\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "C:\Python314\python.exe", "C:\Python313\python.exe", "C:\Python312\python.exe"
    )
    foreach ($p in $candidates) { if ((Test-Path $p) -and (Test-PythonRuntime -PythonPath $p)) { return $p } }
    return $null
}

function Test-ReparsePoint {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-NoReparsePoints {
    # A junction or symlink in the data folder would make NSSM (as SYSTEM)
    # write and rotate its log files somewhere else.
    foreach ($p in @($RlmHome, $LogDir, (Join-Path $RlmHome "data"))) {
        if (Test-ReparsePoint $p) {
            Write-Host "[ERROR] $p is a junction or symbolic link. Remove it and run -Install again." -ForegroundColor Red
            exit 1
        }
    }
    if (Test-Path -LiteralPath $RlmHome) {
        $links = Get-ChildItem -LiteralPath $RlmHome -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue
        if ($links) {
            Write-Host "[ERROR] $RlmHome contains junctions or symbolic links ($($links[0].FullName)). Remove them and run -Install again." -ForegroundColor Red
            exit 1
        }
    }
}

function Protect-DataFolder {
    # Owner-only data folder: SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544),
    # no other access. SIDs keep this independent of the OS language.
    #
    # Standard users may create folders under %ProgramData%. A folder that
    # someone else created before the install keeps its owner, and an owner can
    # always change the permissions again, so such a folder is refused.
    Assert-NoReparsePoints
    if (Test-Path -LiteralPath $RlmHome) {
        $owner = (Get-Acl -LiteralPath $RlmHome).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if (@("S-1-5-18", "S-1-5-32-544") -notcontains $owner) {
            Write-Host "[ERROR] $RlmHome already exists and is owned by $owner, not by SYSTEM or Administrators." -ForegroundColor Red
            Write-Host "        Check who created it, remove it, and run -Install again." -ForegroundColor Yellow
            exit 1
        }
    } else {
        New-Item -ItemType Directory -Path $RlmHome | Out-Null
    }
    # Take ownership of the whole tree, drop every explicit permission, then
    # allow SYSTEM and Administrators only; children inherit that.
    & icacls "$RlmHome" /setowner "*S-1-5-32-544" /T /C /Q | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
    & icacls "$RlmHome" /reset /T /C /Q | Out-Null
    $ok = $ok -and ($LASTEXITCODE -eq 0)
    & icacls "$RlmHome" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    $ok = $ok -and ($LASTEXITCODE -eq 0)
    if (-not $ok) {
        Write-Host "[ERROR] Could not restrict permissions on $RlmHome" -ForegroundColor Red
        exit 1
    }
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $RlmHome "data") -Force | Out-Null
    Assert-NoReparsePoints
}

function Install-Service {
    if (-not (Test-Administrator)) { Write-Host "[ERROR] Run as Administrator." -ForegroundColor Red; exit 1 }
    Test-Nssm
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Host "[!] Service '$ServiceName' already exists. Remove it first with -Uninstall." -ForegroundColor Yellow; exit 1
    }

    Write-Host "[!] EXPERIMENTAL: the service runs as LocalSystem and its REST API has no authentication." -ForegroundColor Yellow
    Write-Host "    Use it only on a single-user machine (see SECURITY.md)." -ForegroundColor Yellow

    # NSSM itself runs as SYSTEM.
    if (-not (Test-AdminOnlyWritable -Path (Split-Path -Parent $NssmPath))) {
        Write-Host "[ERROR] $(Split-Path -Parent $NssmPath) is writable by non-administrators; install into Program Files." -ForegroundColor Red
        exit 1
    }

    Protect-DataFolder

    $exePath = $ExeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    $extraEnv = @("RLM_HOME=$RlmHome", "RLM_HOST=127.0.0.1", "RLM_PORT=$Port")

    if ($exePath) {
        if (-not (Test-AdminOnlyWritable -Path (Split-Path -Parent $exePath))) {
            Write-Host "[ERROR] $(Split-Path -Parent $exePath) is writable by non-administrators; install into Program Files." -ForegroundColor Red
            exit 1
        }
        Write-Host "[*] Installing '$ServiceName' from the packaged exe (no Python needed)" -ForegroundColor Cyan
        Write-Host "    Exe: $exePath" -ForegroundColor Gray
        & $NssmPath install $ServiceName $exePath
        & $NssmPath set $ServiceName AppDirectory (Split-Path -Parent $exePath)
    } else {
        Write-Host "[i] No rootmind-backend.exe found, using source mode (development only)" -ForegroundColor Cyan
        # The service imports the code in the checkout and reads its .env as SYSTEM.
        foreach ($dir in @($ProjectDir, (Join-Path $ProjectDir "src"))) {
            if (-not (Test-AdminOnlyWritable -Path $dir)) {
                Write-Host "[ERROR] $dir is writable by non-administrators, so its code would run as SYSTEM." -ForegroundColor Red
                Write-Host "  Use the packaged exe, or run the backend in a console with run-backend.bat." -ForegroundColor Gray
                exit 1
            }
        }
        $pythonPath = Resolve-Python
        if (-not $pythonPath) {
            Write-Host "[ERROR] No suitable Python with the backend dependencies found, and no packaged exe." -ForegroundColor Red
            Write-Host "  Either build rootmind-backend.exe (see packaging/), or install Python from python.org" -ForegroundColor Gray
            Write-Host "  for all users (not the Microsoft Store, not per user) and run: uv sync" -ForegroundColor Gray
            exit 1
        }
        Write-Host "[*] Installing '$ServiceName' from source" -ForegroundColor Cyan
        Write-Host "    Python:  $pythonPath" -ForegroundColor Gray
        Write-Host "    WorkDir: $ProjectDir" -ForegroundColor Gray
        & $NssmPath install $ServiceName $pythonPath "-m" "rootmind.main" "api" "--host" "127.0.0.1" "--port" "$Port"
        & $NssmPath set $ServiceName AppDirectory $ProjectDir
        # Lets a plain Python (without an editable install) import the package.
        $extraEnv += "PYTHONPATH=$(Join-Path $ProjectDir 'src')"
    }

    & $NssmPath set $ServiceName DisplayName $DisplayName
    & $NssmPath set $ServiceName Description $Description
    & $NssmPath set $ServiceName Start SERVICE_AUTO_START
    & $NssmPath set $ServiceName AppEnvironmentExtra @extraEnv
    & $NssmPath set $ServiceName AppStdout "$LogDir\backend-service.log"
    & $NssmPath set $ServiceName AppStderr "$LogDir\backend-service.log"
    & $NssmPath set $ServiceName AppRotateFiles 1
    & $NssmPath set $ServiceName AppRotateBytes 10485760

    Write-Host "[OK] Service installed." -ForegroundColor Green
    Write-Host "  Data:   $RlmHome (config.yaml, data\index.db, logs\; SYSTEM and Administrators only)"
    Write-Host "  Start:  .\_service-backend.ps1 -Start"
}

function Uninstall-Service {
    if (-not (Test-Administrator)) { Write-Host "[ERROR] Run as Administrator." -ForegroundColor Red; exit 1 }
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { Write-Host "[!] '$ServiceName' not found." -ForegroundColor Yellow; return }
    if ($svc.Status -eq 'Running') { Stop-Service -Name $ServiceName }
    if (Test-Path $NssmPath) {
        & $NssmPath remove $ServiceName confirm
    } else {
        & sc.exe delete $ServiceName | Out-Null
    }
    Write-Host "[OK] Service removed. Data under $RlmHome was left in place." -ForegroundColor Green
}

function Start-RootMindService {
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        Write-Host "[ERROR] Not installed. Run -Install first." -ForegroundColor Red; exit 1
    }
    Start-Service -Name $ServiceName
    Start-Sleep -Seconds 2
    Show-Status
}

function Stop-RootMindService {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { Write-Host "[ERROR] Not installed." -ForegroundColor Red; exit 1 }
    if ($svc.Status -ne 'Stopped') { Stop-Service -Name $ServiceName }
    Write-Host "[OK] Service stopped." -ForegroundColor Green
}

function Show-Status {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { Write-Host "Service '$ServiceName': NOT INSTALLED" -ForegroundColor Yellow; return }
    $color = switch ($svc.Status) { 'Running' { 'Green' } 'Stopped' { 'Red' } default { 'Yellow' } }
    Write-Host "Service '$ServiceName': $($svc.Status)" -ForegroundColor $color
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        Write-Host "REST API: RESPONDING (http://127.0.0.1:$Port)" -ForegroundColor Green
    } catch { Write-Host "REST API: NOT RESPONDING" -ForegroundColor Red }
}

if ($Install) { Install-Service }
elseif ($Uninstall) { Uninstall-Service }
elseif ($Start) { Start-RootMindService }
elseif ($Stop) { Stop-RootMindService }
elseif ($Status) { Show-Status }
else {
    Write-Host "RootMind backend - Windows service manager" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Usage: .\_service-backend.ps1 [-Install|-Uninstall|-Start|-Stop|-Status]"
    Write-Host ""
    Show-Status
}
