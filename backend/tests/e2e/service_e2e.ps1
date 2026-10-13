<#
.SYNOPSIS
    End-to-end test of the RootMind Windows background service.

.DESCRIPTION
    Installs, checks, updates and removes the real services with the packaged
    rootmind-backend.exe (onedir build). It changes the machine: it creates
    and deletes the services RootMind-Backend and RootMind-MCP-HTTP, the folder
    "%ProgramFiles%\RootMind Service" and "%ProgramData%\RootMind".

    It therefore refuses to run unless the process is elevated and CI=true
    (GitHub's Windows runners), or -Force is given, and unless none of those
    services and folders exist yet. It always cleans up, also after a failure.

    1. Refusals: a foreign RootMind-Backend (3), a busy port (4), another
       account (8), an unprotected data folder (6), a program folder that
       Users may change (10).
    2. Install (MCP over HTTP is on by default): services, accounts,
       privileges, quoted ImagePath, service key permissions.
    3. Permissions of the program and data folders.
    4. Tokens: present, different, not in the registry; 401/200 on both.
    5. Confinement: the service reads only what the account can read.
    6. MCP sees a newly added folder without a restart.
    7. Restart, status --json, an update from another copy, MCP off and on
       again (as the app's settings switch it).
    8. Uninstall from the Program Files copy, again, and with --purge.

.EXAMPLE
    pwsh -File backend/tests/e2e/service_e2e.ps1 -ExeDir backend/dist/rootmind-backend `
        -ApiPort 18080 -McpPort 18081 -DiagnosticsDir $env:RUNNER_TEMP/service-e2e-diagnostics
#>
param(
    [Parameter(Mandatory = $true)] [string] $ExeDir,
    [int] $ApiPort = 18080,
    [int] $McpPort = 18081,
    # Scratch folder for the copies and test files (default: a new temporary folder).
    [string] $WorkDir = "",
    # Where logs and service details go when a step fails.
    [string] $DiagnosticsDir = "",
    # Run outside CI (an elevated prompt on a throwaway machine).
    [switch] $Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$ServiceNames = @("RootMind-Backend", "RootMind-MCP-HTTP")
$McpSid = "S-1-5-80-298245856-1475029704-2427701759-2504033446-509022668"  # NT SERVICE\RootMind-MCP-HTTP
$AdminSids = @("S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
$ToolNames = @("search_documents", "search_fuzzy", "list_documents", "read_document", "get_statistics")

# --- guard: refuse before any change ------------------------------------------------------

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "service_e2e: refusing to run: this test installs Windows services and needs an elevated process."
    exit 2
}
if ($env:CI -ne "true" -and -not $Force) {
    Write-Host "service_e2e: refusing to run: it installs and removes Windows services. It runs in CI (CI=true); pass -Force to run it on a throwaway machine."
    exit 2
}
foreach ($port in @($ApiPort, $McpPort)) {
    if ($port -in @(8000, 8010) -or $port -lt 1024 -or $port -gt 65535) {
        Write-Host "service_e2e: refusing to run: port $port is not allowed here."
        exit 2
    }
}
$ProgramFiles = [Environment]::GetFolderPath("ProgramFiles")
$ProgramBase = Join-Path $ProgramFiles "RootMind Service"
# Two admin-only copies of the build stand in for two installed app versions:
# "service install" copies only from a folder non-administrators cannot change.
$Sources = Join-Path $ProgramFiles "RootMind E2E Sources"
$DataRoot = Join-Path ([Environment]::GetFolderPath("CommonApplicationData")) "RootMind"
foreach ($name in $ServiceNames) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "service_e2e: refusing to run: the service $name exists already."
        exit 2
    }
}
foreach ($path in @($ProgramBase, $DataRoot, $Sources)) {
    if (Test-Path -LiteralPath $path) {
        Write-Host "service_e2e: refusing to run: $path exists already."
        exit 2
    }
}

$ExeDir = (Resolve-Path -LiteralPath $ExeDir).Path
if (-not (Test-Path -LiteralPath (Join-Path $ExeDir "rootmind-backend.exe") -PathType Leaf)) {
    Write-Host "service_e2e: $ExeDir has no rootmind-backend.exe"
    exit 2
}
if (-not $WorkDir) { $WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("rootmind-service-e2e-" + [Guid]::NewGuid().ToString("N")) }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path
$UserSid = $identity.User.Value

# --- helpers ------------------------------------------------------------------------------

function Fail([string] $Message) { throw "E2E FAILED: $Message" }
function Pass([string] $Message) { Write-Host "ok   $Message" }
function Assert-True([bool] $Condition, [string] $Message) { if (-not $Condition) { Fail $Message }; Pass $Message }

function Invoke-Exe([string] $Exe, [string[]] $Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = (& $Exe @Arguments 2>&1 | ForEach-Object { "$_" }) -join "`n"
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    Write-Host "> $(Split-Path -Leaf $Exe) $($Arguments -join ' ')  [exit $code]"
    if ($output.Trim()) { Write-Host ($output.TrimEnd() -replace '(?m)^', '    ') }
    return [pscustomobject]@{ Code = $code; Output = $output }
}

function Assert-Exit($Result, [int] $Expected, [string] $What) {
    if ($Result.Code -ne $Expected) { Fail "$What exited with $($Result.Code), expected $Expected" }
    Pass "$What -> exit $Expected"
}

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

function Wait-Healthy([string] $Url, [int] $TimeoutSeconds = 90) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ((Invoke-Http GET "$Url/health").Status -eq 200) { return }
        Start-Sleep -Milliseconds 250
    }
    Fail "$Url/health not healthy within $TimeoutSeconds s"
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

function Get-ServiceKey([string] $Name) { return "HKLM:\SYSTEM\CurrentControlSet\Services\$Name" }

function Get-ImagePath([string] $Name) { return (Get-ItemProperty -LiteralPath (Get-ServiceKey $Name)).ImagePath }

function Get-ProgramExe {
    $image = Get-ImagePath "RootMind-Backend"
    if ($image -notmatch '^"([^"]+)"') { Fail "unquoted ImagePath: $image" }
    return $Matches[1]
}

$FileWriteRights = 0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x40000000 -bor 0x10000000
$RegistryWriteRights = 0x2 -bor 0x4 -bor 0x20 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x40000000 -bor 0x10000000

# Owner and allow entries of a file or folder: SID, rights (as a number), inherited, flags.
function Get-AllowEntries([string] $Path) {
    $acl = Get-Acl -LiteralPath $Path
    $entries = @()
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne "Allow") { continue }
        $entries += [pscustomobject]@{
            Sid = $rule.IdentityReference.Value
            Rights = [int64]$rule.FileSystemRights
            Inherited = $rule.IsInherited
            Inheritance = "$($rule.InheritanceFlags)"
            Propagation = "$($rule.PropagationFlags)"
        }
    }
    return [pscustomobject]@{
        Owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        Protected = $acl.AreAccessRulesProtected
        Entries = $entries
    }
}

# Whether a path is registered for deletion at the next restart.
function Test-PendingDelete([string] $Path) {
    $key = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager"
    $pending = (Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).PendingFileRenameOperations
    if (-not $pending) { return $false }
    return [bool](@($pending) | Where-Object { $_ -like "*$Path*" })
}

# --- the test ----------------------------------------------------------------------------------

$Copy1 = Join-Path $Sources "copy1\rootmind-backend"
$Copy2 = Join-Path $Sources "copy2\rootmind-backend"
$Unsafe = Join-Path $WorkDir "unsafe\rootmind-backend"
$Exe1 = Join-Path $Copy1 "rootmind-backend.exe"
$Exe2 = Join-Path $Copy2 "rootmind-backend.exe"
$ApiUrl = "http://127.0.0.1:$ApiPort"
$McpUrl = "http://127.0.0.1:$McpPort"
$Confine = Join-Path $WorkDir "confine"
$Failed = $false

function Invoke-Mcp([string] $Token, [string] $Method, $Params, [int] $Id = 1) {
    return Invoke-Http POST "$McpUrl/mcp" -Token $Token -Body @{ jsonrpc = "2.0"; id = $Id; method = $Method; params = $Params }
}

$Initialize = @{ protocolVersion = "2025-06-18"; capabilities = @{}; clientInfo = @{ name = "service-e2e"; version = "1" } }

try {
    # Two copies of the build stand in for two installed app versions: a
    # Program Files subfolder created by this elevated administrator, with the
    # permissions and owner it gets that way, as the installer creates the
    # app's folder. "service install" must accept it as a source.
    foreach ($copy in @($Copy1, $Copy2, $Unsafe)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null
        Copy-Item -LiteralPath $ExeDir -Destination $copy -Recurse
    }
    # A copy that Users may change, as in a folder chosen outside Program Files.
    & icacls.exe $Unsafe /grant "*S-1-5-32-545:(OI)(CI)M" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "could not open $Unsafe to Users" }
    $Version = ((Invoke-Exe $Exe1 @("--version")).Output.Trim() -split '\s+')[-1]
    Pass "testing version $Version as $UserSid"

    # --- 1. refusals --------------------------------------------------------------------------
    $foreignImage = "C:\Windows\System32\cmd.exe"
    & sc.exe create RootMind-Backend binPath= $foreignImage start= demand | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "could not create the foreign test service" }
    Assert-Exit (Invoke-Exe $Exe1 @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort")) 3 "install next to a foreign RootMind-Backend"
    Assert-True ((Get-ImagePath "RootMind-Backend") -eq $foreignImage) "the foreign service was not changed"
    & sc.exe delete RootMind-Backend | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $DataRoot)) "nothing was created"

    $listener = New-Object System.Net.Sockets.TcpListener ([Net.IPAddress]::Loopback), $ApiPort
    $listener.Start()
    try {
        Assert-Exit (Invoke-Exe $Exe1 @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort")) 4 "install on a busy port"
    } finally {
        $listener.Stop()
    }
    Assert-Exit (Invoke-Exe $Exe1 @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort", "--user-sid", "S-1-5-32-545")) 8 "install for another account"
    New-Item -ItemType Directory -Path $DataRoot | Out-Null  # inherits "Users: create" from ProgramData
    Assert-Exit (Invoke-Exe $Exe1 @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort")) 6 "install over an unprotected data folder"
    Remove-Item -LiteralPath $DataRoot -Recurse -Force
    $unsafeRun = Invoke-Exe (Join-Path $Unsafe "rootmind-backend.exe") @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort")
    Assert-Exit $unsafeRun 10 "install from a program folder that Users may change"
    Assert-True ($unsafeRun.Output -match "S-1-5-32-545 may change it") "the refusal names the account that may change the program"
    Assert-True (-not (Test-Path -LiteralPath $DataRoot)) "nothing was created for the unsafe program folder"
    Assert-True (-not (Get-Service -Name "RootMind-Backend" -ErrorAction SilentlyContinue)) "no service after the refusals"
    Assert-True (-not (Test-Path -LiteralPath $ProgramBase)) "no program folder after the refusals"

    # --- 2. install (MCP over HTTP is on by default) --------------------------------------------
    Assert-Exit (Invoke-Exe $Exe1 @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort")) 0 "install"
    foreach ($name in $ServiceNames) {
        $service = Get-Service -Name $name
        Assert-True ($service.Status -eq "Running") "$name is running"
        $key = Get-ItemProperty -LiteralPath (Get-ServiceKey $name)
        Assert-True ($key.Start -eq 2) "$name starts automatically"
    }
    $backendKey = Get-ItemProperty -LiteralPath (Get-ServiceKey "RootMind-Backend")
    $mcpKey = Get-ItemProperty -LiteralPath (Get-ServiceKey "RootMind-MCP-HTTP")
    Assert-True ($backendKey.ObjectName -eq "LocalSystem") "the backend runs as LocalSystem"
    Assert-True ($mcpKey.ObjectName -eq "NT SERVICE\RootMind-MCP-HTTP") "MCP runs as NT SERVICE\RootMind-MCP-HTTP"
    Assert-True ((@($mcpKey.RequiredPrivileges) -join ",") -eq "SeChangeNotifyPrivilege") "MCP holds only SeChangeNotifyPrivilege"
    Assert-True ($mcpKey.ServiceSidType -eq 1) "MCP has an unrestricted service SID"
    $base = [regex]::Escape($ProgramBase)
    Assert-True ($backendKey.ImagePath -match "^`"$base\\$([regex]::Escape($Version))-[0-9a-f]{8}\\rootmind-backend\.exe`" service run backend --port $ApiPort --user-sid $([regex]::Escape($UserSid))$") "the backend ImagePath is quoted and under RootMind Service"
    Assert-True ($mcpKey.ImagePath -match "^`"$base\\$([regex]::Escape($Version))-[0-9a-f]{8}\\rootmind-backend\.exe`" service run mcp-http --port $McpPort$") "the MCP ImagePath is quoted and under RootMind Service"
    $ProgramDir = Split-Path -Parent (Get-ProgramExe)
    foreach ($name in $ServiceNames) {
        $keyAcl = (Get-Item -LiteralPath (Get-ServiceKey $name)).GetAccessControl()
        foreach ($rule in $keyAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            $sid = $rule.IdentityReference.Value
            if ($rule.AccessControlType -eq "Allow" -and $sid -in @("S-1-5-32-545", "S-1-5-11", "S-1-5-4") -and
                (([int64]$rule.RegistryRights) -band $RegistryWriteRights)) {
                Fail "$sid may write the service key of $name"
            }
        }
        Pass "Users, Authenticated Users and INTERACTIVE cannot write the key of $name"
    }

    # --- 3. permissions --------------------------------------------------------------------
    foreach ($item in @((Get-Item -LiteralPath $ProgramBase), (Get-Item -LiteralPath $ProgramDir)) + @(Get-ChildItem -LiteralPath $ProgramDir -Recurse -Force)) {
        $info = Get-AllowEntries $item.FullName
        if ($info.Owner -notin $AdminSids) { Fail "$($item.FullName) is owned by $($info.Owner)" }
        foreach ($entry in $info.Entries) {
            if ($entry.Sid -notin $AdminSids -and ($entry.Rights -band $FileWriteRights)) { Fail "$($entry.Sid) may change $($item.FullName)" }
        }
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "$($item.FullName) is a reparse point" }
    }
    Pass "the program folder is writable by administrators only"

    $rootInfo = Get-AllowEntries $DataRoot
    Assert-True $rootInfo.Protected "the data folder does not inherit"
    Assert-True ($rootInfo.Owner -eq "S-1-5-32-544") "Administrators own the data folder"
    $rootEntries = @($rootInfo.Entries | ForEach-Object { "$($_.Sid)|$('0x{0:x}' -f $_.Rights)|$($_.Inheritance)|$($_.Propagation)" } | Sort-Object)
    $expectedRoot = @(
        "S-1-5-18|0x1f01ff|ContainerInherit, ObjectInherit|None",
        "S-1-5-32-544|0x1f01ff|ContainerInherit, ObjectInherit|None",
        "$UserSid|0x1200a9|ContainerInherit, ObjectInherit|None",
        "$McpSid|0x1200a9|ObjectInherit|NoPropagateInherit"
    ) | Sort-Object
    Assert-True (($rootEntries -join ";") -eq ($expectedRoot -join ";")) "the data folder has exactly the layout's entries ($($rootEntries -join '; '))"
    $tmpInfo = Get-AllowEntries (Join-Path $DataRoot "tmp")
    Assert-True ($tmpInfo.Protected -and (@($tmpInfo.Entries | ForEach-Object { $_.Sid } | Sort-Object) -join ",") -eq "S-1-5-18,S-1-5-32-544") "tmp is SYSTEM and Administrators only"
    foreach ($item in @(Get-Item -LiteralPath $DataRoot) + @(Get-ChildItem -LiteralPath $DataRoot -Recurse -Force)) {
        $relative = $item.FullName.Substring($DataRoot.Length).TrimStart("\")
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "$relative is a reparse point" }
        $info = Get-AllowEntries $item.FullName
        foreach ($entry in $info.Entries) {
            if ($entry.Sid -in @("S-1-5-32-545", "S-1-1-0", "S-1-5-11", "S-1-5-4")) { Fail "$($entry.Sid) has an entry on $relative" }
            if ($entry.Sid -eq $McpSid) {
                # Listed: the root (and through it the files directly in it),
                # api_token.txt, and data\db and logs\mcp-http with everything below.
                $listed = ($relative -eq "") -or ($relative -eq "data\api_token.txt") -or
                    ($relative -eq "data\db") -or ($relative -like "data\db\*") -or
                    ($relative -eq "logs\mcp-http") -or ($relative -like "logs\mcp-http\*") -or
                    (-not $item.PSIsContainer -and -not $relative.Contains("\"))
                if (-not $listed) { Fail "the MCP account has an entry on $relative" }
            }
        }
    }
    Pass "no Users or Everyone entries, no reparse points, the MCP account only where listed"
    $restEntries = (Get-AllowEntries (Join-Path $DataRoot "data\rest_token.txt")).Entries
    Assert-True (-not ($restEntries | Where-Object { $_.Sid -eq $McpSid })) "the MCP account has no entry on rest_token.txt"
    Assert-True ([bool]($restEntries | Where-Object { $_.Sid -eq $UserSid })) "the account may read rest_token.txt"

    # --- 4. tokens -------------------------------------------------------------------------------
    $RestToken = (Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\rest_token.txt")).Trim()
    $McpToken = (Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\api_token.txt")).Trim()
    Assert-True ($RestToken.Length -ge 32 -and $McpToken.Length -ge 32 -and $RestToken -ne $McpToken) "both tokens exist and differ"
    foreach ($name in $ServiceNames) {
        foreach ($key in @(Get-Item -LiteralPath (Get-ServiceKey $name)) + @(Get-ChildItem -LiteralPath (Get-ServiceKey $name) -Recurse)) {
            foreach ($valueName in $key.GetValueNames()) {
                $value = "$($key.GetValue($valueName))"
                if ($value.Contains($RestToken) -or $value.Contains($McpToken)) { Fail "a token is stored in $($key.Name)\$valueName" }
            }
        }
    }
    Pass "no token in the service registry keys"
    Assert-Status (Invoke-Http GET "$ApiUrl/status") 401 "REST /status without the token"
    Assert-Status (Invoke-Http GET "$ApiUrl/status" -Token $RestToken) 200 "REST /status with the token"
    Assert-Status (Invoke-Http POST "$ApiUrl/config" -Body @{ connectors = @() }) 401 "REST POST /config without the token"
    Assert-Status (Invoke-Http GET "$ApiUrl/docs" -Token $RestToken) 404 "REST /docs"
    Assert-Status (Invoke-Mcp "" "initialize" $Initialize) 401 "MCP initialize without the token"
    Assert-Status (Invoke-Mcp $McpToken "initialize" $Initialize) 200 "MCP initialize with the token"
    $tools = Invoke-Mcp $McpToken "tools/list" @{} 2
    $names = @($tools.Json.result.tools | ForEach-Object { $_.name })
    foreach ($name in $ToolNames) { if ($names -notcontains $name) { Fail "MCP tools/list lacks $name" } }
    Pass "MCP lists the five tools"
    Assert-Status (Invoke-Http GET "$ApiUrl/status" -Token $McpToken) 401 "REST with the MCP token"
    Assert-Status (Invoke-Mcp $RestToken "initialize" $Initialize) 401 "MCP with the REST token"

    # --- 5. confinement ----------------------------------------------------------------------------
    New-Item -ItemType Directory -Path $Confine | Out-Null
    & icacls.exe $Confine /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*$($UserSid):(OI)(CI)F" | Out-Null
    $fileA = Join-Path $Confine "a.md"; $fileB = Join-Path $Confine "b.md"; $fileC = Join-Path $Confine "c.md"
    [IO.File]::WriteAllText($fileA, "# A`n`nThe word quartzlanternalpha is readable by the account.`n")
    [IO.File]::WriteAllText($fileB, "# B`n`nThe word quartzlanternbravo is for administrators only.`n")
    [IO.File]::WriteAllText($fileC, "# C`n`nThe word quartzlanterncharlie is for SYSTEM only.`n")
    & icacls.exe $fileB /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
    & icacls.exe $fileC /inheritance:r /grant:r "*S-1-5-18:F" | Out-Null
    $uacOn = $true
    $policy = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -ErrorAction SilentlyContinue
    if ($policy -and ($policy.PSObject.Properties.Name -contains "EnableLUA") -and $policy.EnableLUA -eq 0) { $uacOn = $false }
    Write-Host "UAC is $(if ($uacOn) { 'on' } else { 'off' })"

    # POST /config takes the editable config with its llm section, as the app
    # sends it: read the current one, then replace the folder list.
    $current = Invoke-Http GET "$ApiUrl/config" -Token $RestToken
    Assert-Status $current 200 "GET /config"
    $llm = $current.Json.llm
    Assert-Status (Invoke-Http POST "$ApiUrl/config" -Token $RestToken -Body @{ llm = $llm; connectors = @(@{ name = "e2e"; type = "local"; path = $Confine }) }) 200 "POST /config adds the test folder"
    $refresh = Invoke-Http POST "$ApiUrl/index/refresh" -Token $RestToken -Body @{ full = $true }
    if ($refresh.Status -notin @(200, 409)) { Fail "POST /index/refresh answered $($refresh.Status)" }
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ($true) {
        $found = Invoke-Http GET "$ApiUrl/search?q=quartzlanternalpha" -Token $RestToken
        $state = Invoke-Http GET "$ApiUrl/index/status" -Token $RestToken
        if ($found.Status -eq 200 -and $found.Json.count -ge 1 -and $state.Json -and -not $state.Json.is_running) { break }
        if ([DateTime]::UtcNow -gt $deadline) { Fail "file A was not indexed within 120 s ($($found.Text))" }
        Start-Sleep -Seconds 1
    }
    Pass "the service indexed file A"
    $bravo = Invoke-Http GET "$ApiUrl/search?q=quartzlanternbravo" -Token $RestToken
    $charlie = Invoke-Http GET "$ApiUrl/search?q=quartzlanterncharlie" -Token $RestToken
    Assert-True ($charlie.Status -eq 200 -and $charlie.Json.count -eq 0) "a file only SYSTEM can read is not indexed"
    if ($uacOn) {
        Assert-True ($bravo.Status -eq 200 -and $bravo.Json.count -eq 0) "a file only administrators can read is not indexed (UAC filters the account)"
    } else {
        Write-Host "note: UAC is off, so the account itself reads administrator-only files; file B is not checked"
    }
    foreach ($bad in @("C:\Windows", "C:\", "\\localhost\c$\x")) {
        Assert-Status (Invoke-Http POST "$ApiUrl/config" -Token $RestToken -Body @{ llm = $llm; connectors = @(@{ name = "bad"; type = "local"; path = $bad }) }) 400 "POST /config with $bad"
    }

    # --- 6. MCP reads the new folder without a restart -----------------------------------------
    $search = Invoke-Mcp $McpToken "tools/call" @{ name = "search_documents"; arguments = @{ query = "quartzlanternalpha" } } 3
    Assert-Status $search 200 "MCP search_documents"
    $text = ($search.Json.result.content | ForEach-Object { $_.text }) -join "`n"
    Assert-True ($text -match "a\.md") "MCP finds file A without a restart"
    $search = Invoke-Mcp $McpToken "tools/call" @{ name = "search_documents"; arguments = @{ query = "quartzlanterncharlie" } } 4
    Assert-True ((($search.Json.result.content | ForEach-Object { $_.text }) -join "`n") -notmatch "c\.md") "MCP does not find file C"

    # --- 7. restart, status, update ---------------------------------------------------------------
    Restart-Service -Name "RootMind-Backend"
    Wait-Healthy $ApiUrl
    Assert-True ((Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\rest_token.txt")).Trim() -eq $RestToken) "the token survives a restart"
    Assert-Status (Invoke-Http GET "$ApiUrl/status" -Token $RestToken) 200 "REST after the restart"

    $statusRun = Invoke-Exe $Exe1 @("service", "status", "--json")
    Assert-Exit $statusRun 0 "status --json"
    $status = $statusRun.Output | ConvertFrom-Json
    Assert-True ($status.installed -and $status.ours -and $status.state -eq "running") "status: installed, ours, running"
    Assert-True ($status.port -eq $ApiPort -and $status.version -eq $Version -and $status.user_sid -eq $UserSid) "status: port, version and account"
    Assert-True ($status.token_readable -and $status.binary -eq (Get-ProgramExe)) "status: token readable, binary"
    Assert-True ($status.mcp_http.installed -and $status.mcp_http.state -eq "running" -and $status.mcp_http.port -eq $McpPort) "status: MCP"
    $unexpected = @($status.warnings | Where-Object { $_ -notlike "RootMind is not installed*" })
    Assert-True ($unexpected.Count -eq 0) "status: no warnings besides the missing app ($($status.warnings -join '; '))"

    $oldDir = $ProgramDir
    Assert-Exit (Invoke-Exe $Exe2 @("service", "install", "--port", "auto", "--user-sid", $UserSid)) 0 "update from another copy"
    $ProgramDir = Split-Path -Parent (Get-ProgramExe)
    Assert-True ($ProgramDir -ne $oldDir) "the update uses a new version folder"
    Assert-True ((-not (Test-Path -LiteralPath $oldDir)) -or (Test-PendingDelete $oldDir)) "the old version folder is gone (or deleted at the next restart)"
    Assert-True ((Get-ImagePath "RootMind-Backend") -match " --port $ApiPort " -and (Get-ImagePath "RootMind-MCP-HTTP") -match " --port $McpPort$") "the ports are unchanged"
    Assert-True ((Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\rest_token.txt")).Trim() -eq $RestToken) "the REST token is unchanged"
    Assert-True ((Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\api_token.txt")).Trim() -eq $McpToken) "the MCP token is unchanged"
    Assert-Status (Invoke-Http GET "$ApiUrl/status" -Token $RestToken) 200 "REST after the update"
    Assert-Status (Invoke-Mcp $McpToken "initialize" $Initialize) 200 "MCP after the update"

    # MCP over HTTP off and on again, from the Program Files copy (the app's settings do this).
    $programExe = Get-ProgramExe
    Assert-Exit (Invoke-Exe $programExe @("service", "install", "--no-mcp-http")) 0 "MCP over HTTP off"
    & sc.exe query RootMind-MCP-HTTP | Out-Null
    Assert-True ($LASTEXITCODE -eq 1060) "the MCP service is gone"
    Assert-True (-not (Test-PortOpen $McpPort)) "the MCP port is closed"
    Assert-Status (Invoke-Http GET "$ApiUrl/status" -Token $RestToken) 200 "REST without MCP"
    Assert-Exit (Invoke-Exe $programExe @("service", "install", "--mcp-http", "--mcp-port", "$McpPort")) 0 "MCP over HTTP on"
    Assert-True ((Get-Content -Raw -LiteralPath (Join-Path $DataRoot "data\api_token.txt")).Trim() -eq $McpToken) "the MCP token is unchanged"
    Assert-Status (Invoke-Mcp $McpToken "initialize" $Initialize) 200 "MCP after turning it on again"

    # --- 8. uninstall ----------------------------------------------------------------------------
    $programExe = Get-ProgramExe
    Assert-Exit (Invoke-Exe $programExe @("service", "uninstall")) 0 "uninstall from the Program Files copy"
    foreach ($name in $ServiceNames) {
        & sc.exe query $name | Out-Null
        Assert-True ($LASTEXITCODE -eq 1060) "$name is gone"
    }
    Assert-True (-not (Test-PortOpen $ApiPort) -and -not (Test-PortOpen $McpPort)) "the ports are closed"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $DataRoot "data\rest_token.txt"))) "rest_token.txt was deleted"
    Assert-True ((Test-Path -LiteralPath (Join-Path $DataRoot "data\api_token.txt")) -and (Test-Path -LiteralPath (Join-Path $DataRoot "config.yaml"))) "the MCP token and the data were kept"
    Assert-Exit (Invoke-Exe $Exe1 @("service", "uninstall")) 0 "uninstall again"
    Assert-Exit (Invoke-Exe $Exe1 @("service", "install", "--port", "$ApiPort", "--mcp-port", "$McpPort")) 0 "a fresh install over the kept data"
    Assert-Exit (Invoke-Exe $Exe1 @("service", "uninstall", "--purge")) 0 "uninstall --purge"
    Assert-True (-not (Test-Path -LiteralPath $DataRoot)) "--purge deleted the data folder"
    Write-Host "Service e2e test passed."
} catch {
    $Failed = $true
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    if ($DiagnosticsDir) {
        try {
            New-Item -ItemType Directory -Force -Path $DiagnosticsDir | Out-Null
            if (Test-Path -LiteralPath (Join-Path $DataRoot "logs")) {
                Copy-Item -LiteralPath (Join-Path $DataRoot "logs") -Destination (Join-Path $DiagnosticsDir "logs") -Recurse -Force
            }
            foreach ($name in $ServiceNames) {
                (& sc.exe qc $name 2>&1 | Out-String) + (& sc.exe query $name 2>&1 | Out-String) |
                    Set-Content -LiteralPath (Join-Path $DiagnosticsDir "sc-$name.txt") -Encoding utf8
            }
            Get-WinEvent -FilterHashtable @{ LogName = "System"; ProviderName = "Service Control Manager" } -MaxEvents 50 -ErrorAction SilentlyContinue |
                Format-List TimeCreated, Id, LevelDisplayName, Message | Out-String -Width 200 |
                Set-Content -LiteralPath (Join-Path $DiagnosticsDir "scm-events.txt") -Encoding utf8
            Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = (Get-Date).AddHours(-1) } -MaxEvents 100 -ErrorAction SilentlyContinue |
                Where-Object { $_.ProviderName -match "Python|RootMind|Application Error|Windows Error Reporting" } |
                Format-List TimeCreated, ProviderName, Id, Message | Out-String -Width 200 |
                Set-Content -LiteralPath (Join-Path $DiagnosticsDir "application-events.txt") -Encoding utf8
        } catch {
            Write-Host "could not collect all diagnostics: $($_.Exception.Message)"
        }
    }
} finally {
    # Cleanup, always: remove the services and both folders.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $uninstaller = $null
    try { $uninstaller = Get-ProgramExe } catch { $uninstaller = $null }
    foreach ($candidate in @($uninstaller, $Exe1)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate) -and ((Get-Service -Name $ServiceNames -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $DataRoot))) {
            & $candidate service uninstall --purge 2>&1 | Out-Null
        }
    }
    foreach ($name in $ServiceNames) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
            & sc.exe stop $name 2>&1 | Out-Null
            & sc.exe delete $name 2>&1 | Out-Null
        }
    }
    foreach ($path in @($DataRoot, $ProgramBase, $Sources)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
    }
    if (Test-Path -LiteralPath $Confine) {
        & icacls.exe $Confine /reset /T /C /Q 2>&1 | Out-Null
        Remove-Item -LiteralPath $Confine -Recurse -Force -ErrorAction SilentlyContinue
    }
    $ErrorActionPreference = $previous
}
if ($Failed) { exit 1 }
exit 0
