<#
.SYNOPSIS
    Smoke test of the Windows installer: install, update, reinstall and uninstall, all unattended.

.DESCRIPTION
    Runs the per-machine NSIS installer silently ("/S", the way winget runs it)
    and passively ("/P", which runs the installer's page logic without
    showing the pages), and fails on the first problem:

    - another install folder ("/S /D=...") is refused with exit code 10
      before anything is installed: the service runs as SYSTEM from a copy of
      the backend in the install folder, which must be admin-only;
    - fresh install: "/S" exits with 0; the "Installed apps" entry (HKLM)
      names the version and the publisher, and the install folder is
      %ProgramFiles%\RootMind, which standard accounts cannot change;
      $INSTDIR\backend has the tree hash of the tested backend build and
      rootmind-backend.exe --version prints the backend version;
    - the services RootMind-Backend and RootMind-MCP-HTTP (MCP over HTTP is on
      by default) are installed for this account, run from the admin-only
      copy under %ProgramFiles%\RootMind Service and are running; REST answers
      401 without and 200 with its token, MCP over HTTP likewise; a folder
      added through the REST API is kept in the service's config;
      "service status --json" reports no warnings;
    - an installed version is updated in place without questions: a silent
      update from an older version and a passive reinstall of the same
      version each exit with 0 and keep the install folder, the uninstall
      entry (a value only this test writes survives, so the installed
      version's uninstaller never ran) and the app data (a marker file in
      %LOCALAPPDATA%\<identifier>). Each removes the backend files the tested
      build does not ship (as an older version leaves them, including its
      package metadata). The services run a new program copy, keep their
      ports, both tokens and the data, and answer with the old tokens. "An
      older version" is the installed one with DisplayVersion 0.0.1, which is
      what the installer compares;
    - "uninstall.exe /S" removes the services (their ports close), the
      service's program copy, the entry, the app and the backend; the
      service's data folder stays (a silent uninstall never deletes data), but
      not the REST token.

    Not covered here: the refusal of an installer approved by another account
    than the signed-in one (the runner has a single account); the hooks are
    checked statically by scripts/tests/test_nsis_template.py.

    The tree hash is SHA-256 over the sorted lines "<relative path>`t<sha256>`n"
    of every file, with "/" separators and UTF-8 text, the same as
    scripts/prepare_backend.py.

    The test installs RootMind for the whole machine and Windows services, so
    it refuses to run unless CI=true (GitHub Actions) or -Force is given and
    the process is elevated, and it never touches an existing installation,
    service or data folder. Works with Windows PowerShell 5.1 and PowerShell
    7. Exit codes: 0 passed, 1 failed, 2 refused.

.EXAMPLE
    pwsh -File desktop/scripts/installer-smoke.ps1 -Setup RootMind-Setup-x64.exe -Version 2.0.0 `
        -BackendTreeSha256 <hash> -Publisher $publisher -Identifier io.github.cgaeking.rootmind `
        -WorkDir $env:RUNNER_TEMP/installer-smoke
#>
param(
    [Parameter(Mandatory = $true)] [string] $Setup,
    # The app version, as package.json spells it (the installer's DisplayVersion).
    [Parameter(Mandatory = $true)] [string] $Version,
    [Parameter(Mandatory = $true)] [string] $BackendTreeSha256,
    # bundle.publisher from tauri.conf.json.
    [Parameter(Mandatory = $true)] [string] $Publisher,
    # identifier from tauri.conf.json, the name of the app data folders.
    [Parameter(Mandatory = $true)] [string] $Identifier,
    # What rootmind-backend.exe --version prints last (PEP 440 spelling). Default: -Version.
    [string] $BackendVersion = "",
    [string] $WorkDir = "",
    [switch] $Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$ProductName = "RootMind"
$UninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductName"
$V1UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductName"
$ServiceNames = @("RootMind-Backend", "RootMind-MCP-HTTP")
# A value the installer never writes. An uninstaller deletes the whole
# uninstall entry, so the value survives only when none ran.
$MarkerName = "RootMindSmokeMarker"
$MarkerValue = "kept"
$OlderVersion = "0.0.1"
$Initialize = @{ protocolVersion = "2025-06-18"; capabilities = @{}; clientInfo = @{ name = "installer-smoke"; version = "1" } }

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
    Refuse "this test installs and removes RootMind and its Windows services; it runs only with CI=true or -Force"
}
if ($BackendTreeSha256 -notmatch '^[0-9a-fA-F]{64}$') { Refuse "-BackendTreeSha256 must be 64 hexadecimal digits" }
$BackendTreeSha256 = $BackendTreeSha256.ToLowerInvariant()
if ($Identifier -notmatch '^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$') { Refuse "-Identifier must be a name like io.github.owner.app" }
if (-not $BackendVersion) { $BackendVersion = $Version }
if (-not (Test-Path -LiteralPath $Setup -PathType Leaf)) { Refuse "$Setup not found" }
$Setup = (Resolve-Path -LiteralPath $Setup).Path
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Refuse "the installer is per machine and installs Windows services; run this test elevated"
}
$UserSid = $identity.User.Value
$ProgramFiles = [Environment]::GetFolderPath("ProgramFiles")
$ExpectedDir = Join-Path $ProgramFiles $ProductName
$ProgramBase = Join-Path $ProgramFiles "RootMind Service"
$DataRoot = Join-Path ([Environment]::GetFolderPath("CommonApplicationData")) "RootMind"
$OtherDir = Join-Path $env:SystemDrive "RootMind-installer-smoke-elsewhere"
$AppDataDir = Join-Path $env:LOCALAPPDATA $Identifier
$AppDataMarker = Join-Path $AppDataDir "installer-smoke-marker.txt"
foreach ($key in @($UninstallKey, $V1UninstallKey)) {
    if (Test-Path -LiteralPath $key) { Refuse "RootMind is installed ($key); this test never touches an installation" }
}
foreach ($name in $ServiceNames) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) { Refuse "the service $name exists; this test never touches it" }
}
foreach ($path in @($ExpectedDir, $ProgramBase, $DataRoot, $AppDataDir, $OtherDir)) {
    if (Test-Path -LiteralPath $path) { Refuse "$path exists; this test never touches an installation or its data" }
}
if (-not $WorkDir) { $WorkDir = Join-Path ([IO.Path]::GetTempPath()) "rootmind-installer-smoke" }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path

Add-Type -AssemblyName System.Net.Http
$HttpHandler = New-Object System.Net.Http.HttpClientHandler
$HttpHandler.UseProxy = $false
$Http = New-Object System.Net.Http.HttpClient($HttpHandler)
$Http.Timeout = [TimeSpan]::FromSeconds(30)

function Invoke-Http {
    param([string] $Method, [string] $Url, [string] $Token = "", $Body = $null)
    $request = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::new($Method)), $Url
    if ($Token) {
        $request.Headers.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue "Bearer", $Token
    }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 10 -Compress
        $request.Content = New-Object System.Net.Http.StringContent $json, ([Text.Encoding]::UTF8), "application/json"
    }
    try {
        $response = $Http.SendAsync($request).GetAwaiter().GetResult()
    } catch {
        return [pscustomobject]@{ Status = 0; Text = ""; Json = $null }
    }
    $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    $parsed = $null
    if ($text) { try { $parsed = $text | ConvertFrom-Json } catch { $parsed = $null } }
    return [pscustomobject]@{ Status = [int]$response.StatusCode; Text = $text; Json = $parsed }
}

function Assert-Status($Result, [int] $Expected, [string] $What) {
    if ($Result.Status -ne $Expected) { Fail "$What answered $($Result.Status), expected $Expected ($($Result.Text))" }
    Pass "$What -> $Expected"
}

function Test-PortOpen([int] $Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return ($task.Wait(2000) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

# Whether a path is registered for deletion at the next restart.
function Test-PendingDelete([string] $Path) {
    $key = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager"
    $pending = (Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).PendingFileRenameOperations
    if (-not $pending) { return $false }
    return [bool](@($pending) | Where-Object { $_ -like "*$Path*" })
}

# A program copy of the service is gone, or deleted at the next restart.
function Assert-ProgramGone([string] $Dir, [string] $When) {
    if ((Test-Path -LiteralPath (Join-Path $Dir "rootmind-backend.exe")) -and -not (Test-PendingDelete $Dir)) {
        Fail "$When the service's program copy $Dir is still there"
    }
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

function Get-InstallLocation { return ([string](Get-ArpValue "InstallLocation")).Trim('"') }

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

# Files an older version leaves in the backend folder that the tested build
# does not ship, its package metadata among them: found first, it would report
# the older version. Returns their paths.
function Add-StaleBackendFiles([string] $BackendDir) {
    $staleInfo = Join-Path (Join-Path $BackendDir "_internal") "rootmind_backend-$OlderVersion.dist-info"
    New-Item -ItemType Directory -Force -Path $staleInfo | Out-Null
    $metadata = "Metadata-Version: 2.1`nName: rootmind-backend`nVersion: $OlderVersion`n"
    [IO.File]::WriteAllText((Join-Path $staleInfo "METADATA"), $metadata)
    $staleFile = Join-Path $BackendDir "stale.txt"
    [IO.File]::WriteAllText($staleFile, "left by an older version")
    return @($staleInfo, $staleFile)
}

function Get-ImagePath([string] $Name) {
    $key = Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$Name" -ErrorAction SilentlyContinue
    if ($null -eq $key) { return $null }
    return [string]$key.ImagePath
}

# The two services as the installer registered them: program, ports, account
# and tokens. Both must run and start automatically.
function Get-ServiceState {
    $base = [regex]::Escape($ProgramBase)
    $backend = Get-ImagePath "RootMind-Backend"
    $mcp = Get-ImagePath "RootMind-MCP-HTTP"
    $pattern = "^`"($base\\[^\\`"]+\\rootmind-backend\.exe)`" service run backend --port (\d+) --user-sid (S-1-[0-9-]+)$"
    if ($backend -notmatch $pattern) { Fail "unexpected RootMind-Backend ImagePath: $backend" }
    $exe = $Matches[1]; $apiPort = [int]$Matches[2]; $sid = $Matches[3]
    if ($mcp -notmatch "^`"([^`"]+)`" service run mcp-http --port (\d+)$") { Fail "unexpected RootMind-MCP-HTTP ImagePath: $mcp" }
    if ($Matches[1] -ne $exe) { Fail "the services run different programs: $exe and $($Matches[1])" }
    $mcpPort = [int]$Matches[2]
    foreach ($name in $ServiceNames) {
        $service = Get-Service -Name $name
        if ($service.Status -ne "Running") { Fail "$name is $($service.Status)" }
        $start = (Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$name").Start
        if ($start -ne 2) { Fail "$name does not start automatically (Start = $start)" }
    }
    return [pscustomobject]@{
        Exe = $exe; ApiPort = $apiPort; McpPort = $mcpPort; Sid = $sid
        RestToken = (Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\rest_token.txt")).Trim()
        McpToken = (Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\api_token.txt")).Trim()
    }
}

function Invoke-Mcp($State, [string] $Token) {
    return Invoke-Http POST "http://127.0.0.1:$($State.McpPort)/mcp" -Token $Token -Body @{ jsonrpc = "2.0"; id = 1; method = "initialize"; params = $Initialize }
}

# The services answer, each only with its own token.
function Assert-ServicesAnswer($State, [string] $When) {
    $api = "http://127.0.0.1:$($State.ApiPort)"
    Assert-Status (Invoke-Http GET "$api/health") 200 "$When REST /health"
    Assert-Status (Invoke-Http GET "$api/status") 401 "$When REST /status without the token"
    Assert-Status (Invoke-Http GET "$api/status" -Token $State.RestToken) 200 "$When REST /status with the token"
    Assert-Status (Invoke-Mcp $State "") 401 "$When MCP initialize without the token"
    Assert-Status (Invoke-Mcp $State $State.RestToken) 401 "$When MCP initialize with the REST token"
    Assert-Status (Invoke-Mcp $State $State.McpToken) 200 "$When MCP initialize with the token"
}

function Get-ConnectorPaths($State) {
    $config = Invoke-Http GET "http://127.0.0.1:$($State.ApiPort)/config" -Token $State.RestToken
    if ($config.Status -ne 200) { Fail "GET /config answered $($config.Status)" }
    return @($config.Json.connectors | ForEach-Object { $_.path })
}

# An install over the installed version ran without questions (exit 0), in
# place, without the installed version's uninstaller, left nothing of the
# previous backend, and updated the services without losing their state.
# Returns the new service state.
function Assert-UpdatedInPlace($Run, [string] $What, [string[]] $Stale, $Before) {
    if ($Run.ExitCode -ne 0) { Fail "the $What exited with $($Run.ExitCode)" }
    Pass "$What -> 0"
    if ([string](Get-ArpValue $MarkerName) -ne $MarkerValue) {
        Fail "after the ${What}, $MarkerName is gone from the uninstall entry: the installed version's uninstaller ran"
    }
    $location = Get-InstallLocation
    if (-not [string]::Equals($location.TrimEnd('\'), $InstallDir.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        Fail "after the ${What}, InstallLocation is '$location', expected $InstallDir"
    }
    if ((Get-ArpValue "DisplayVersion") -ne $Version) {
        Fail "after the ${What}, DisplayVersion is '$(Get-ArpValue "DisplayVersion")', expected $Version"
    }
    if (-not (Test-Path -LiteralPath $mainExe -PathType Leaf)) { Fail "after the ${What}, the app is missing" }
    if (-not (Test-Path -LiteralPath $AppDataMarker -PathType Leaf)) { Fail "after the ${What}, the app data is gone" }
    Pass "$What in place: same folder, uninstall entry and app data kept, DisplayVersion $Version"
    foreach ($path in $Stale) {
        if (Test-Path -LiteralPath $path) { Fail "after the ${What}, $path of the previous backend is still there" }
    }
    Pass "$What removed the backend files the tested build does not ship"
    Assert-BackendTree $BackendDir "after the ${What},"
    Assert-BackendVersion $BackendExe

    $after = Get-ServiceState
    if ($after.Exe -eq $Before.Exe) { Fail "after the ${What}, the services still run $($Before.Exe)" }
    Assert-ProgramGone (Split-Path -Parent $Before.Exe) "after the ${What},"
    if ($after.ApiPort -ne $Before.ApiPort -or $after.McpPort -ne $Before.McpPort) {
        Fail "after the ${What}, the ports changed from $($Before.ApiPort)/$($Before.McpPort) to $($after.ApiPort)/$($after.McpPort)"
    }
    if ($after.Sid -ne $Before.Sid) { Fail "after the ${What}, the service belongs to $($after.Sid)" }
    if ($after.RestToken -ne $Before.RestToken -or $after.McpToken -ne $Before.McpToken) { Fail "after the ${What}, a token changed" }
    Pass "$What updated the services: new program copy, same ports, account and tokens"
    Assert-ServicesAnswer $after "after the ${What}:"
    if ((Get-ConnectorPaths $after) -notcontains $DocsDir) { Fail "after the ${What}, the folder $DocsDir is gone from the config" }
    Pass "$What kept the service's data (the configured folder)"
    return $after
}

$Summary = New-Object System.Collections.ArrayList
$InstallDir = $null
$CreatedAppData = $false
$DocsDir = Join-Path $WorkDir "docs"
$failed = $false

try {
    $size = (Get-Item -LiteralPath $Setup).Length
    [void]$Summary.Add(("Installer: {0} ({1:N1} MB)" -f (Split-Path -Leaf $Setup), ($size / 1MB)))

    # --- another install folder is refused ------------------------------------------------------
    # /D= must come last and unquoted.
    $refused = Invoke-Program $Setup "/S /D=$OtherDir" 600
    if ($refused.ExitCode -ne 10) { Fail "an install into $OtherDir exited with $($refused.ExitCode), expected 10" }
    if (Test-Path -LiteralPath $UninstallKey) { Fail "the refused install left an uninstall entry" }
    foreach ($name in $ServiceNames) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) { Fail "the refused install created $name" }
    }
    $copied = @(Get-ChildItem -LiteralPath $OtherDir -Recurse -Force -ErrorAction SilentlyContinue)
    if ($copied.Count) { Fail "the refused install copied files into $OtherDir" }
    if (Test-Path -LiteralPath $DataRoot) { Fail "the refused install created $DataRoot" }
    Pass "an install into another folder is refused with exit code 10; nothing was installed"

    # --- silent install ---------------------------------------------------------------------
    $started = [DateTime]::UtcNow
    $install = Invoke-Program $Setup "/S" 900
    if ($install.ExitCode -ne 0) { Fail "the silent install exited with $($install.ExitCode)" }
    [void]$Summary.Add(("Silent install (with the services): {0:N1} s" -f ([DateTime]::UtcNow - $started).TotalSeconds))
    Pass "silent install -> 0"

    if (-not (Test-Path -LiteralPath $UninstallKey)) { Fail "no uninstall entry at $UninstallKey" }
    $InstallDir = Get-InstallLocation
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

    # Standard accounts cannot change the app or the backend it ships.
    $writeRights = 0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x40000000 -bor 0x10000000
    foreach ($path in @($InstallDir, $BackendDir, $BackendExe)) {
        $acl = Get-Acl -LiteralPath $path
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            $sid = $rule.IdentityReference.Value
            if ($rule.AccessControlType -eq "Allow" -and $sid -in @("S-1-5-32-545", "S-1-5-11", "S-1-5-4", "S-1-1-0", $UserSid) -and
                (([int64]$rule.FileSystemRights) -band $writeRights)) {
                Fail "$sid may change $path"
            }
        }
    }
    Pass "the install folder is writable by administrators only"

    # --- the services ---------------------------------------------------------------------------
    $state = Get-ServiceState
    if ($state.Sid -ne $UserSid) { Fail "the service belongs to $($state.Sid), expected this account ($UserSid)" }
    if ($state.RestToken.Length -lt 32 -or $state.McpToken.Length -lt 32 -or $state.RestToken -eq $state.McpToken) {
        Fail "the tokens are missing, short or equal"
    }
    Pass "both services run from $(Split-Path -Parent $state.Exe) for this account (REST port $($state.ApiPort), MCP port $($state.McpPort))"
    [void]$Summary.Add("Ports chosen by the installer: REST $($state.ApiPort), MCP $($state.McpPort)")
    Assert-ServicesAnswer $state "after the install:"

    $statusRun = Invoke-Program $BackendExe "service status --json" 120
    if ($statusRun.ExitCode -ne 0) { Fail "service status --json exited with $($statusRun.ExitCode): $($statusRun.Stderr)" }
    $status = $statusRun.Stdout | ConvertFrom-Json
    if (-not ($status.installed -and $status.ours -and $status.state -eq "running" -and $status.token_readable)) {
        Fail "service status: $($statusRun.Stdout)"
    }
    if (@($status.warnings).Count -ne 0) { Fail "service status warns: $($status.warnings -join '; ')" }
    Pass "service status --json: installed, ours, running, no warnings"

    # Data that updates must keep: a folder in the service's config.
    New-Item -ItemType Directory -Force -Path $DocsDir | Out-Null
    [IO.File]::WriteAllText((Join-Path $DocsDir "note.md"), "# Note`n`nThe word quartzinstallersmoke.`n")
    $api = "http://127.0.0.1:$($state.ApiPort)"
    $current = Invoke-Http GET "$api/config" -Token $state.RestToken
    Assert-Status $current 200 "GET /config"
    $body = @{ llm = $current.Json.llm; connectors = @(@{ name = "smoke"; type = "local"; path = $DocsDir }) }
    Assert-Status (Invoke-Http POST "$api/config" -Token $state.RestToken -Body $body) 200 "POST /config adds a folder"
    if ((Get-ConnectorPaths $state) -notcontains $DocsDir) { Fail "the folder is not in the config" }

    # --- in-place updates: what must survive them ----------------------------------------------
    New-ItemProperty -LiteralPath $UninstallKey -Name $MarkerName -Value $MarkerValue -PropertyType String -Force | Out-Null
    # App data, where the app keeps it (%LOCALAPPDATA%\<identifier>).
    $CreatedAppData = $true
    New-Item -ItemType Directory -Force -Path $AppDataDir | Out-Null
    [IO.File]::WriteAllText($AppDataMarker, "app data")

    # --- silent update from an older version, as winget runs it ---------------------------------
    Set-ItemProperty -LiteralPath $UninstallKey -Name "DisplayVersion" -Value $OlderVersion
    $stale = Add-StaleBackendFiles $BackendDir
    $started = [DateTime]::UtcNow
    $run = Invoke-Program $Setup "/S" 900
    [void]$Summary.Add(("Silent update from {0}: {1:N1} s" -f $OlderVersion, ([DateTime]::UtcNow - $started).TotalSeconds))
    $state = Assert-UpdatedInPlace $run "silent update from $OlderVersion" $stale $state

    # --- passive reinstall of the same version --------------------------------------------------
    # "/P" updates in place like a double-clicked installer, without showing pages. With
    # Tauri's own template it would run the installed version's uninstaller here. That
    # there is no folder page at all is checked statically (release.yml and
    # scripts/tests/test_nsis_template.py): /S and /P show no pages.
    $stale = Add-StaleBackendFiles $BackendDir
    $started = [DateTime]::UtcNow
    $run = Invoke-Program $Setup "/P" 900
    [void]$Summary.Add(("Passive reinstall: {0:N1} s" -f ([DateTime]::UtcNow - $started).TotalSeconds))
    $state = Assert-UpdatedInPlace $run "passive reinstall of $Version" $stale $state

    # --- silent uninstall, as winget runs it ---------------------------------------------------
    # "_?=" runs the uninstaller in place, so it can be waited for; it cannot delete itself then.
    $uninstaller = Join-Path $InstallDir "uninstall.exe"
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) { Fail "$uninstaller is missing" }
    $uninstall = Invoke-Program $uninstaller "/S _?=$InstallDir" 900
    if ($uninstall.ExitCode -ne 0) { Fail "the silent uninstall exited with $($uninstall.ExitCode)" }
    foreach ($name in $ServiceNames) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) { Fail "the service $name is still there" }
    }
    if ((Test-PortOpen $state.ApiPort) -or (Test-PortOpen $state.McpPort)) { Fail "a service port is still open" }
    Assert-ProgramGone (Split-Path -Parent $state.Exe) "after the uninstall,"
    Pass "silent uninstall removed both services and their program copy"
    if (Test-Path -LiteralPath $UninstallKey) { Fail "the uninstall entry is still there" }
    if (Test-Path -LiteralPath $mainExe) { Fail "the app is still there" }
    if (Test-Path -LiteralPath $BackendDir) {
        $left = @(Get-ChildItem -LiteralPath $BackendDir -Recurse -Force | Select-Object -First 10 | ForEach-Object { $_.FullName })
        Fail "the backend folder is still there: $($left -join ', ')"
    }
    Pass "silent uninstall -> 0; the entry, the app and the backend are gone"
    if (Test-Path -LiteralPath (Join-Path $DataRoot "data\rest_token.txt")) { Fail "the REST token is still there" }
    if (-not (Test-Path -LiteralPath (Join-Path $DataRoot "config.yaml"))) { Fail "a silent uninstall deleted the service's data" }
    Pass "the service's data was kept, without the REST token"
} catch {
    $failed = $true
    Write-Host $_.Exception.Message -ForegroundColor Red
    $log = Join-Path $DataRoot "logs\install.log"
    if (Test-Path -LiteralPath $log) {
        Write-Host "install.log (last lines):"
        Get-Content -LiteralPath $log -Tail 40 | ForEach-Object { Write-Host "  $_" }
    }
} finally {
    # Leave the runner as it was, even after a failure.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    if (Test-Path -LiteralPath $UninstallKey) {
        $cleanupDir = Get-InstallLocation
        $cleanupUninstaller = Join-Path $cleanupDir "uninstall.exe"
        if ($cleanupDir -and (Test-Path -LiteralPath $cleanupUninstaller)) {
            try { Invoke-Program $cleanupUninstaller "/S _?=$cleanupDir" 900 | Out-Null } catch { Write-Host "cleanup: $_" }
        }
    }
    foreach ($name in $ServiceNames) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
            & sc.exe stop $name 2>&1 | Out-Null
            & sc.exe delete $name 2>&1 | Out-Null
        }
    }
    # The guards refused existing folders, so these are the test's.
    foreach ($path in @($ExpectedDir, $ProgramBase, $DataRoot, $DocsDir, $OtherDir)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
    }
    if ($CreatedAppData -and (Test-Path -LiteralPath $AppDataDir)) {
        Remove-Item -LiteralPath $AppDataDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    $ErrorActionPreference = $previous
}

if ($env:GITHUB_STEP_SUMMARY) {
    $lines = @("### Installer smoke test", "") + ($Summary | ForEach-Object { "- $_" })
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value ($lines -join "`n") -Encoding utf8
}
$Summary | ForEach-Object { Write-Host $_ }
if ($failed) { exit 1 }
Write-Host "Installer smoke test passed."
exit 0
