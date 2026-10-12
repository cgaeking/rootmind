<#
.SYNOPSIS
    Smoke test of the Windows installer: silent install, reinstall and uninstall.

.DESCRIPTION
    Runs the per-user NSIS installer the way winget does ("/S") and fails on
    the first problem:

    - "/S" installs and exits with 0;
    - the "Installed apps" entry (HKCU) names the version and the publisher,
      and its install folder is %LOCALAPPDATA%\RootMind;
    - $INSTDIR\backend has the tree hash of the tested backend build, and
      rootmind-backend.exe --version prints the backend version;
    - the installed backend starts the way the app starts it (api, a token,
      stdin as the lifeline, RLM_HOME below -WorkDir) and answers with and
      without the token;
    - "/S" again while that backend runs exits with 0, the backend process is
      gone, and the backend tree is unchanged;
    - "/S" over a backend folder with files the new version does not ship (as
      an older version leaves them, including its package metadata) removes
      them: the tree hash and --version are those of the tested build;
    - "uninstall.exe /S" removes the entry, the app and the backend.

    The tree hash is SHA-256 over the sorted lines "<relative path>`t<sha256>`n"
    of every file, with "/" separators and UTF-8 text, the same as
    scripts/prepare_backend.py.

    The test installs RootMind for the current user, so it refuses to run
    unless CI=true (GitHub Actions) or -Force is given, and it never touches an
    existing installation. Works with Windows PowerShell 5.1 and PowerShell 7.
    Exit codes: 0 passed, 1 failed, 2 refused.

.EXAMPLE
    pwsh -File desktop/scripts/installer-smoke.ps1 -Setup RootMind-Setup-x64.exe -Version 1.2.0 `
        -BackendTreeSha256 <hash> -Publisher $publisher -WorkDir $env:RUNNER_TEMP/installer-smoke
#>
param(
    [Parameter(Mandatory = $true)] [string] $Setup,
    # The app version, as package.json spells it (the installer's DisplayVersion).
    [Parameter(Mandatory = $true)] [string] $Version,
    [Parameter(Mandatory = $true)] [string] $BackendTreeSha256,
    # bundle.publisher from tauri.conf.json.
    [Parameter(Mandatory = $true)] [string] $Publisher,
    # What rootmind-backend.exe --version prints last (PEP 440 spelling). Default: -Version.
    [string] $BackendVersion = "",
    [string] $WorkDir = "",
    [int] $Port = 18770,
    [switch] $Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$ProductName = "RootMind"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductName"
$ReadyPattern = '^ROOTMIND_LISTENING \{"port":(\d+)\}$'

function Fail([string] $Message) { throw "INSTALLER SMOKE FAILED: $Message" }
function Pass([string] $Message) { Write-Host "ok   $Message" }
function Refuse([string] $Message) {
    Write-Host "installer-smoke: $Message" -ForegroundColor Yellow
    exit 2
}

# SHA-256 over the sorted "<relative path>`t<sha256 of the file>`n" lines of
# every file below $Root ("/" separators, UTF-8); folders do not count.
function Get-TreeSha256([string] $Root) {
    $Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\', '/')
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $lines = New-Object System.Collections.Generic.List[string]
        foreach ($file in (Get-ChildItem -LiteralPath $Root -Recurse -File -Force)) {
            $relative = $file.FullName.Substring($Root.Length + 1).Replace('\', '/')
            $stream = [System.IO.File]::OpenRead($file.FullName)
            try { $digest = $sha.ComputeHash($stream) } finally { $stream.Dispose() }
            $hex = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
            $lines.Add("$relative`t$hex`n")
        }
        $sorted = $lines.ToArray()
        [Array]::Sort($sorted, [StringComparer]::Ordinal)
        $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes([string]::Concat($sorted))
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

# --- guards: nothing below runs on a machine that has RootMind or is not CI -------------------
if ($env:CI -ne "true" -and -not $Force) {
    Refuse "this test installs and removes RootMind for the current user; it runs only with CI=true or -Force"
}
if ($Port -in @(8000, 8010) -or $Port -lt 1024 -or $Port -gt 65535) {
    Refuse "port $Port is not allowed here (use a free port from 18000-18999)"
}
if ($BackendTreeSha256 -notmatch '^[0-9a-fA-F]{64}$') { Refuse "-BackendTreeSha256 must be 64 hexadecimal digits" }
$BackendTreeSha256 = $BackendTreeSha256.ToLowerInvariant()
if (-not $BackendVersion) { $BackendVersion = $Version }
if (-not (Test-Path -LiteralPath $Setup -PathType Leaf)) { Refuse "$Setup not found" }
$Setup = (Resolve-Path -LiteralPath $Setup).Path
$ExpectedDir = Join-Path $env:LOCALAPPDATA $ProductName
if (Test-Path -LiteralPath $UninstallKey) { Refuse "RootMind is installed for this user; this test never touches an installation" }
if (Test-Path -LiteralPath $ExpectedDir) { Refuse "$ExpectedDir exists; this test never touches an installation" }
if (-not $WorkDir) { $WorkDir = Join-Path ([IO.Path]::GetTempPath()) "rootmind-installer-smoke" }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path

Add-Type -AssemblyName System.Net.Http
$HttpHandler = New-Object System.Net.Http.HttpClientHandler
$HttpHandler.UseProxy = $false
$Http = New-Object System.Net.Http.HttpClient($HttpHandler)
$Http.Timeout = [TimeSpan]::FromSeconds(30)

function Invoke-Http([string] $Url, [string] $Token = "") {
    $request = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::Get), $Url
    if ($Token) {
        $request.Headers.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue "Bearer", $Token
    }
    try {
        $response = $Http.SendAsync($request).GetAwaiter().GetResult()
    } catch {
        return [pscustomobject]@{ Status = 0; Json = $null }
    }
    $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    $parsed = $null
    if ($text) { try { $parsed = $text | ConvertFrom-Json } catch { $parsed = $null } }
    return [pscustomobject]@{ Status = [int]$response.StatusCode; Json = $parsed }
}

function Assert-Status($Result, [int] $Expected, [string] $What) {
    if ($Result.Status -ne $Expected) { Fail "$What answered $($Result.Status), expected $Expected" }
    Pass "$What -> $Expected"
}

# Run a program to its end; $Arguments is passed as it is (NSIS wants "_?=" unquoted).
function Invoke-Program([string] $File, [string] $Arguments, [int] $TimeoutSeconds) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $File
    $info.Arguments = $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.WorkingDirectory = $WorkDir
    $process = [System.Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { Write-Host "could not stop $File" }
        Fail "$File $Arguments did not finish within $TimeoutSeconds s"
    }
    $process.WaitForExit()
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout.Result; Stderr = $stderr.Result }
}

function Get-ArpValue([string] $Name) {
    $key = Get-Item -LiteralPath $UninstallKey -ErrorAction SilentlyContinue
    if ($null -eq $key) { return $null }
    return $key.GetValue($Name)
}

function Assert-BackendTree([string] $BackendDir, [string] $When) {
    $hash = Get-TreeSha256 $BackendDir
    if ($hash -ne $BackendTreeSha256) { Fail "$When the backend tree hash is $hash, expected $BackendTreeSha256" }
    Pass "$When the backend tree hash matches the tested build"
}

function Assert-BackendVersion([string] $Exe) {
    $run = Invoke-Program $Exe "--version" 120
    if ($run.ExitCode -ne 0) { Fail "rootmind-backend.exe --version exited with $($run.ExitCode): $($run.Stderr)" }
    $tokens = @($run.Stdout.Trim() -split '\s+')
    if ($tokens[-1] -ne $BackendVersion) { Fail "rootmind-backend.exe --version says '$($run.Stdout.Trim())', expected $BackendVersion" }
    Pass "rootmind-backend.exe --version -> $($run.Stdout.Trim())"
}

# The installed backend, started the way the app starts it.
function Start-InstalledBackend([string] $Exe, [string] $HomeDir, [string] $Token) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = "api --host 127.0.0.1 --port $Port --exit-with-parent"
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $info.WorkingDirectory = $HomeDir
    foreach ($name in @("RLM_API_TOKEN", "MCP_API_TOKEN", "RLM_HOST", "RLM_PORT", "CONFIG_PATH", "RLM_DEV",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY")) {
        $info.EnvironmentVariables.Remove($name)
    }
    $info.EnvironmentVariables["RLM_HOME"] = $HomeDir
    $info.EnvironmentVariables["RLM_API_TOKEN"] = $Token
    $info.EnvironmentVariables["RLM_DISABLE_SCHEDULER"] = "1"
    # A provider request can never leave the machine.
    $info.EnvironmentVariables["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
    $info.EnvironmentVariables["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
    $process = [System.Diagnostics.Process]::Start($info)
    $stderr = $process.StandardError.ReadToEndAsync()
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ($true) {
        $pending = $process.StandardOutput.ReadLineAsync()
        $left = [int]([Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds))
        if (-not $pending.Wait($left)) { Fail "the installed backend printed no ready line within 90 s" }
        $line = $pending.Result
        if ($null -eq $line) {
            $process.WaitForExit(5000) | Out-Null
            Fail "the installed backend exited before the ready line: $($stderr.Result)"
        }
        if ($line -match $ReadyPattern) {
            if ([int]$Matches[1] -ne $Port) { Fail "the ready line names port $($Matches[1]), expected $Port" }
            break
        }
    }
    # Keep draining stdout so the backend never blocks on a full pipe.
    $stdout = $process.StandardOutput.ReadToEndAsync()
    return [pscustomobject]@{ Process = $process; Stdout = $stdout; Stderr = $stderr }
}

$Summary = New-Object System.Collections.ArrayList
$Backend = $null
$InstallDir = $null
$failed = $false

try {
    $size = (Get-Item -LiteralPath $Setup).Length
    [void]$Summary.Add(("Installer: {0} ({1:N1} MB)" -f (Split-Path -Leaf $Setup), ($size / 1MB)))

    # --- silent install ---------------------------------------------------------------------
    $started = [DateTime]::UtcNow
    $install = Invoke-Program $Setup "/S" 600
    if ($install.ExitCode -ne 0) { Fail "the silent install exited with $($install.ExitCode)" }
    [void]$Summary.Add(("Silent install: {0:N1} s" -f ([DateTime]::UtcNow - $started).TotalSeconds))
    Pass "silent install -> 0"

    if (-not (Test-Path -LiteralPath $UninstallKey)) { Fail "no uninstall entry at $UninstallKey" }
    $InstallDir = ([string](Get-ArpValue "InstallLocation")).Trim('"')
    if ((Get-ArpValue "DisplayName") -ne $ProductName) { Fail "DisplayName is '$(Get-ArpValue "DisplayName")'" }
    if ((Get-ArpValue "DisplayVersion") -ne $Version) {
        Fail "DisplayVersion is '$(Get-ArpValue "DisplayVersion")', expected $Version"
    }
    if (-not [string]::Equals([string](Get-ArpValue "Publisher"), $Publisher, [StringComparison]::Ordinal)) {
        Fail "Publisher is '$(Get-ArpValue "Publisher")', expected '$Publisher'"
    }
    if (-not [string]::Equals($InstallDir.TrimEnd('\'), $ExpectedDir, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "InstallLocation is '$InstallDir', expected $ExpectedDir"
    }
    if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) { Fail "the install folder $InstallDir does not exist" }
    Pass "uninstall entry: $ProductName $Version by $Publisher in $InstallDir"

    $mainName = [string](Get-ArpValue "MainBinaryName")
    $mainExe = Join-Path $InstallDir $mainName
    if (-not $mainName -or -not (Test-Path -LiteralPath $mainExe -PathType Leaf)) { Fail "the app ($mainName) is not installed" }
    if ($mainName -ieq "rootmind-backend.exe") { Fail "the app and the backend have the same file name" }
    $BackendDir = Join-Path $InstallDir "backend"
    $BackendExe = Join-Path $BackendDir "rootmind-backend.exe"
    if (-not (Test-Path -LiteralPath $BackendExe -PathType Leaf)) { Fail "$BackendExe is missing" }
    if (-not (Test-Path -LiteralPath (Join-Path $BackendDir "_internal") -PathType Container)) {
        Fail "$BackendDir has no _internal folder"
    }
    Pass "the app ($mainName) and backend\rootmind-backend.exe are installed"
    Assert-BackendTree $BackendDir "after the install,"
    Assert-BackendVersion $BackendExe

    # --- the installed backend ----------------------------------------------------------------
    $homeDir = Join-Path $WorkDir "home"
    New-Item -ItemType Directory -Force -Path $homeDir | Out-Null
    $token = [Guid]::NewGuid().ToString("N") + [Guid]::NewGuid().ToString("N")
    $started = [DateTime]::UtcNow
    $Backend = Start-InstalledBackend $BackendExe $homeDir $token
    [void]$Summary.Add(("Installed backend ready after {0:N1} s" -f ([DateTime]::UtcNow - $started).TotalSeconds))
    $url = "http://127.0.0.1:$Port"
    Assert-Status (Invoke-Http "$url/health") 200 "GET /health"
    $root = Invoke-Http "$url/"
    Assert-Status $root 200 "GET /"
    if ($null -eq $root.Json -or $root.Json.name -ne "RootMind API" -or $root.Json.auth -ne "bearer") {
        Fail "GET / does not describe a RootMind API with bearer authentication"
    }
    Assert-Status (Invoke-Http "$url/status") 401 "GET /status without the token"
    Assert-Status (Invoke-Http "$url/status" $token) 200 "GET /status with the token"

    # --- reinstall while the backend runs (an update, or a backend left behind) ----------------
    $reinstall = Invoke-Program $Setup "/S" 600
    if ($reinstall.ExitCode -ne 0) { Fail "the silent reinstall exited with $($reinstall.ExitCode)" }
    Pass "silent reinstall while the backend runs -> 0"
    if (-not $Backend.Process.WaitForExit(15000)) { Fail "the backend still runs after the reinstall" }
    Pass "the installer closed the running backend"
    if ((Get-ArpValue "DisplayVersion") -ne $Version) { Fail "DisplayVersion changed to '$(Get-ArpValue "DisplayVersion")'" }
    Assert-BackendTree $BackendDir "after the reinstall,"
    Assert-BackendVersion $BackendExe

    # --- upgrade over an older backend: files no longer shipped must go -------------------------
    # An older version's metadata would otherwise be found first and report its version.
    $staleInfo = Join-Path $BackendDir "_internal\rootmind_backend-0.0.1.dist-info"
    New-Item -ItemType Directory -Force -Path $staleInfo | Out-Null
    $metadata = "Metadata-Version: 2.1`nName: rootmind-backend`nVersion: 0.0.1`n"
    [IO.File]::WriteAllText((Join-Path $staleInfo "METADATA"), $metadata)
    [IO.File]::WriteAllText((Join-Path $BackendDir "stale.txt"), "left by an older version")
    $upgrade = Invoke-Program $Setup "/S" 600
    if ($upgrade.ExitCode -ne 0) { Fail "the silent install over an older backend exited with $($upgrade.ExitCode)" }
    if ((Test-Path -LiteralPath $staleInfo) -or (Test-Path -LiteralPath (Join-Path $BackendDir "stale.txt"))) {
        Fail "files of an older backend are still in $BackendDir"
    }
    Pass "an install over an older backend removes the files it no longer ships"
    Assert-BackendTree $BackendDir "after the install over an older backend,"
    Assert-BackendVersion $BackendExe

    # --- silent uninstall, as winget runs it ---------------------------------------------------
    # "_?=" runs the uninstaller in place, so it can be waited for; it cannot delete itself then.
    $uninstaller = Join-Path $InstallDir "uninstall.exe"
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) { Fail "$uninstaller is missing" }
    $uninstall = Invoke-Program $uninstaller "/S _?=$InstallDir" 600
    if ($uninstall.ExitCode -ne 0) { Fail "the silent uninstall exited with $($uninstall.ExitCode)" }
    if (Test-Path -LiteralPath $UninstallKey) { Fail "the uninstall entry is still there" }
    if (Test-Path -LiteralPath $mainExe) { Fail "the app is still there" }
    if (Test-Path -LiteralPath $BackendDir) {
        $left = @(Get-ChildItem -LiteralPath $BackendDir -Recurse -Force | Select-Object -First 10 | ForEach-Object { $_.FullName })
        Fail "the backend folder is still there: $($left -join ', ')"
    }
    Pass "silent uninstall -> 0; the entry, the app and the backend are gone"
} catch {
    $failed = $true
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($null -ne $Backend) {
        $Backend.Process.WaitForExit(2000) | Out-Null
        if ($Backend.Process.HasExited) {
            Write-Host "Backend stderr (last lines):"
            ($Backend.Stderr.Result -split "`r?`n" | Select-Object -Last 40) | ForEach-Object { Write-Host "  $_" }
        }
    }
} finally {
    if ($null -ne $Backend -and -not $Backend.Process.HasExited) {
        try { $Backend.Process.Kill() } catch { Write-Host "could not stop the backend (process $($Backend.Process.Id))" }
    }
    # Leave the runner as it was, even after a failure.
    if (Test-Path -LiteralPath $UninstallKey) {
        $cleanupDir = ([string](Get-ArpValue "InstallLocation")).Trim('"')
        $cleanupUninstaller = Join-Path $cleanupDir "uninstall.exe"
        if ($cleanupDir -and (Test-Path -LiteralPath $cleanupUninstaller)) {
            try { Invoke-Program $cleanupUninstaller "/S _?=$cleanupDir" 600 | Out-Null } catch { Write-Host "cleanup: $_" }
        }
    }
    if (Test-Path -LiteralPath $ExpectedDir) {
        Remove-Item -LiteralPath $ExpectedDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($env:GITHUB_STEP_SUMMARY) {
    $lines = @("### Installer smoke test", "") + ($Summary | ForEach-Object { "- $_" })
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value ($lines -join "`n") -Encoding utf8
}
$Summary | ForEach-Object { Write-Host $_ }
if ($failed) { exit 1 }
Write-Host "Installer smoke test passed."
exit 0
