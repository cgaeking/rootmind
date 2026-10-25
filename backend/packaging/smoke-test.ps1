<#
.SYNOPSIS
    Smoke test of the packaged RootMind backend (the PyInstaller onedir folder).

.DESCRIPTION
    Runs the frozen rootmind-backend.exe (on Linux rootmind-backend) the way
    MCP clients and the CLI do (the background service runs the same code;
    backend/tests/e2e/service_e2e.ps1 and service_e2e_linux.sh test it), and
    fails on the first problem:

    - --version prints the package version;
    - api with a token: /health 200, /status 401 without and 200 with the
      token, /docs 404; the token never reaches the output;
    - the options of the removed built-in backend (--exit-with-parent,
      --import-config) are refused;
    - index over the evaluation corpus (.md .pdf .docx .xlsx .pptx .csv)
      without errors, and every format is listed by the API;
    - POST /llm/models for anthropic and openai with a dummy key against a
      closed port answers "could not reach", not a missing module;
    - GET /remote-access: the ngrok SDK (a native extension) imports in the
      build, and remote access is off by default (no tunnel is opened);
    - the client command search through that API (--url, --token-file);
    - mcp-http: 401 without and 200 with the token, the five tools;
    - mcp over stdio: initialize and tools/list, with JSON-only stdout.

    Works with Windows PowerShell 5.1 and PowerShell 7 (also on Linux, for
    the Linux build). Everything is written
    below -WorkDir; no API key or network access is needed. The time until
    the API is healthy goes to the GitHub step summary when there is one.

.EXAMPLE
    pwsh -File backend/packaging/smoke-test.ps1 -ExeDir backend/dist/rootmind-backend `
        -ApiPort 18765 -McpPort 18766 -WorkDir $env:RUNNER_TEMP/smoke
#>
param(
    [Parameter(Mandatory = $true)] [string] $ExeDir,
    [int] $ApiPort = 18765,
    [int] $McpPort = 18766,
    [Parameter(Mandatory = $true)] [string] $WorkDir,
    # Default: the version in backend/pyproject.toml.
    [string] $ExpectedVersion = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$ToolNames = @("search_documents", "search_fuzzy", "list_documents", "read_document", "get_statistics")
$CorpusFormats = @(".md", ".pdf", ".docx", ".xlsx", ".pptx", ".csv")
$Started = New-Object System.Collections.ArrayList

function Fail([string] $Message) { throw "SMOKE FAILED: $Message" }
function Pass([string] $Message) { Write-Host "ok   $Message" }

# PEP 440 normal form of a release or prerelease version as this project writes
# them ("1.2.0", "1.2.0-rc.1", "1.2.0-beta.2", "1.2.0rc1"); anything else is
# returned lower-cased and otherwise unchanged.
function ConvertTo-Pep440([string] $Version) {
    $v = $Version.Trim().ToLowerInvariant()
    if ($v -match '^v?(\d+(?:\.\d+)*)(?:[-_.]?(a|alpha|b|beta|c|rc|pre|preview)[-_.]?(\d*))?$') {
        $release = $Matches[1]
        if (-not $Matches[2]) { return $release }
        $tag = switch ($Matches[2]) {
            { $_ -in @("a", "alpha") } { "a" }
            { $_ -in @("b", "beta") } { "b" }
            default { "rc" }
        }
        $number = if ($Matches[3]) { [string][int]$Matches[3] } else { "0" }
        return "$release$tag$number"
    }
    return $v
}

foreach ($port in @($ApiPort, $McpPort)) {
    if ($port -in @(8000, 8010) -or $port -lt 1024 -or $port -gt 65535) {
        Fail "port $port is not allowed here (use a free port from 18000-18999)"
    }
}
if ($ApiPort -eq $McpPort) { Fail "-ApiPort and -McpPort must differ" }

$ExeDir = (Resolve-Path -LiteralPath $ExeDir).Path
# $IsLinux does not exist in Windows PowerShell 5.1 (and strict mode refuses it).
$OnUnix = [Environment]::OSVersion.Platform -eq [PlatformID]::Unix
$Exe = Join-Path $ExeDir $(if ($OnUnix) { "rootmind-backend" } else { "rootmind-backend.exe" })
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { Fail "$Exe not found" }
if (-not (Test-Path -LiteralPath (Join-Path $ExeDir "_internal") -PathType Container)) {
    Fail "$ExeDir is not a onedir build (no _internal folder)"
}
$BackendDir = Split-Path -Parent $PSScriptRoot
$Corpus = Join-Path (Join-Path $BackendDir "evals") "corpus"
if (-not $ExpectedVersion) {
    $match = Select-String -LiteralPath (Join-Path $BackendDir "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $match) { Fail "no version in pyproject.toml" }
    $ExpectedVersion = $match.Matches[0].Groups[1].Value
}
# The executable prints the PEP 440 normal form (1.2.0-rc.1 -> 1.2.0rc1).
$ExpectedVersion = ConvertTo-Pep440 $ExpectedVersion

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path

Add-Type -AssemblyName System.Net.Http
$HttpHandler = New-Object System.Net.Http.HttpClientHandler
$HttpHandler.UseProxy = $false
$Http = New-Object System.Net.Http.HttpClient($HttpHandler)
$Http.Timeout = [TimeSpan]::FromSeconds(30)

function New-Token { return ([Guid]::NewGuid().ToString("N") + [Guid]::NewGuid().ToString("N")) }

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
    if ($Result.Status -ne $Expected) { Fail "$What answered $($Result.Status), expected $Expected" }
    Pass "$What -> $Expected"
}

function Format-Argument([string] $Value) {
    if ($Value -match '[\s"]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

# Start the backend with redirected stdio. $Environment: name -> value ($null removes it).
function Start-Backend {
    param([string[]] $Arguments, [hashtable] $Environment = @{})
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = ($Arguments | ForEach-Object { Format-Argument $_ }) -join " "
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $info.WorkingDirectory = $WorkDir
    # Nothing from the caller's environment may change what is tested.
    foreach ($name in @("RLM_API_TOKEN", "MCP_API_TOKEN", "RLM_HOST", "RLM_PORT", "CONFIG_PATH", "RLM_DEV",
            "RLM_DISABLE_SCHEDULER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "MCP_ALLOWED_ORIGINS")) {
        $info.EnvironmentVariables.Remove($name)
    }
    # A provider request can never leave the machine.
    $info.EnvironmentVariables["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
    $info.EnvironmentVariables["OPENAI_BASE_URL"] = "http://127.0.0.1:9"
    foreach ($name in $Environment.Keys) {
        if ($null -eq $Environment[$name]) { $info.EnvironmentVariables.Remove($name) }
        else { $info.EnvironmentVariables[$name] = [string]$Environment[$name] }
    }
    $process = [System.Diagnostics.Process]::Start($info)
    [void]$Started.Add($process)
    $stderr = $process.StandardError.ReadToEndAsync()
    return [pscustomobject]@{
        Process = $process; Stderr = $stderr
        Pending = $null; Started = [DateTime]::UtcNow
    }
}

function Send-Stdin($Backend, [string] $Text) {
    $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes($Text)
    $Backend.Process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $Backend.Process.StandardInput.BaseStream.Flush()
}

# Collect what the backend writes to stdout from now on (read it after Stop-Backend).
function Start-Draining($Backend) {
    $Backend.Pending = $Backend.Process.StandardOutput.ReadToEndAsync()
}

function Stop-Backend($Backend) {
    if (-not $Backend.Process.HasExited) { $Backend.Process.Kill() }
    if (-not $Backend.Process.WaitForExit(10000)) { Fail "process $($Backend.Process.Id) did not stop" }
}

function Wait-Exit($Backend, [int] $TimeoutSeconds, [string] $What) {
    if (-not $Backend.Process.WaitForExit($TimeoutSeconds * 1000)) { Fail "$What did not exit within $TimeoutSeconds s" }
    Pass "$What -> exited with $($Backend.Process.ExitCode)"
}

function Wait-Healthy([string] $Url, [int] $TimeoutSeconds = 90) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ((Invoke-Http GET "$Url/health").Status -eq 200) { return }
        Start-Sleep -Milliseconds 100
    }
    Fail "$Url/health not healthy within $TimeoutSeconds s"
}

$Summary = New-Object System.Collections.ArrayList

try {
    # --- version -------------------------------------------------------------------
    $versionOutput = (& $Exe --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Fail "--version exited with $LASTEXITCODE" }
    $version = ($versionOutput -split '\s+')[-1]
    if ($version -ne $ExpectedVersion) { Fail "--version says '$versionOutput', expected $ExpectedVersion" }
    Pass "--version -> $versionOutput"

    # --- api with a token --------------------------------------------------------------
    $apiHome = Join-Path $WorkDir "api-home"
    New-Item -ItemType Directory -Force -Path $apiHome | Out-Null
    $token = New-Token
    $api = Start-Backend -Arguments @("api", "--host", "127.0.0.1", "--port", "$ApiPort") `
        -Environment @{ RLM_HOME = $apiHome; RLM_API_TOKEN = $token }
    Start-Draining $api
    $url = "http://127.0.0.1:$ApiPort"
    Wait-Healthy $url
    $healthySeconds = ([DateTime]::UtcNow - $api.Started).TotalSeconds
    Pass ("api healthy after {0:N1} s" -f $healthySeconds)
    [void]$Summary.Add(("Time to healthy: {0:N1} s" -f $healthySeconds))
    $root = Invoke-Http GET "$url/"
    Assert-Status $root 200 "GET /"
    if ($root.Json.auth -ne "bearer" -or $root.Json.version -ne $ExpectedVersion) { Fail "GET / says $($root.Text)" }
    Assert-Status (Invoke-Http GET "$url/status") 401 "GET /status without the token"
    Assert-Status (Invoke-Http GET "$url/status" -Token "wrong-token") 401 "GET /status with a wrong token"
    Assert-Status (Invoke-Http GET "$url/status" -Token $token) 200 "GET /status with the token"
    Assert-Status (Invoke-Http GET "$url/docs" -Token $token) 404 "GET /docs"
    Assert-Status (Invoke-Http GET "$url/openapi.json" -Token $token) 404 "GET /openapi.json"
    Stop-Backend $api
    $output = $api.Pending.Result + "`n" + $api.Stderr.Result
    if ($output.Contains($token)) { Fail "the token appears in the backend's output" }
    if ($output.Contains("ROOTMIND_LISTENING")) { Fail "the backend printed the ready line of the removed built-in backend" }
    Pass "the token is not in the output"

    # --- options of the removed built-in backend ---------------------------------------
    foreach ($removed in @(@("--exit-with-parent"), @("--import-config", "x.yaml"))) {
        $run = Start-Backend -Arguments (@("api", "--port", "$ApiPort") + $removed) -Environment @{ RLM_HOME = $apiHome }
        Start-Draining $run
        if (-not $run.Process.WaitForExit(60000)) { Fail "api $($removed[0]) did not exit" }
        if ($run.Process.ExitCode -ne 2) { Fail "api $($removed[0]) exited with $($run.Process.ExitCode), expected 2" }
        Pass "api $($removed[0]) is refused (exit 2)"
    }

    # --- index over the corpus -----------------------------------------------------------
    $indexHome = Join-Path $WorkDir "index-home"
    New-Item -ItemType Directory -Force -Path $indexHome | Out-Null
    $include = ($CorpusFormats | ForEach-Object { "'*$_'" }) -join ", "
    $config = @(
        "connectors:",
        "  - name: corpus",
        "    type: local",
        "    path: '$((Resolve-Path -LiteralPath $Corpus).Path)'",
        "indexer:",
        "  include: [$include]",
        "database:",
        "  path: ./data/index.db"
    ) -join "`n"
    [IO.File]::WriteAllText((Join-Path $indexHome "config.yaml"), $config + "`n", (New-Object System.Text.UTF8Encoding $false))
    $expectedFiles = @(Get-ChildItem -LiteralPath $Corpus -File -Recurse | Where-Object { $CorpusFormats -contains $_.Extension.ToLower() })
    $index = Start-Backend -Arguments @("index", "--full") -Environment @{ RLM_HOME = $indexHome; RLM_DISABLE_SCHEDULER = "1" }
    $index.Process.StandardInput.Close()
    $indexOutput = $index.Process.StandardOutput.ReadToEnd()
    $index.Process.WaitForExit()
    if ($index.Process.ExitCode -ne 0) { Fail "index exited with $($index.Process.ExitCode): $($index.Stderr.Result)" }
    if ($indexOutput -notmatch 'corpus: (\d+) indexed, (\d+) skipped, (\d+) errors') { Fail "unexpected index output: $indexOutput" }
    $indexed = [int]$Matches[1]; $errors = [int]$Matches[3]
    if ($errors -ne 0 -or $indexed -ne $expectedFiles.Count) {
        Fail "index: $indexed indexed and $errors errors, expected $($expectedFiles.Count) and 0"
    }
    Pass "index -> $indexed files ($($CorpusFormats -join ' ')) without errors"

    # --- the indexed formats and the LLM SDKs through the API ------------------------------
    $token = New-Token
    $api = Start-Backend -Arguments @("api", "--host", "127.0.0.1", "--port", "$ApiPort") `
        -Environment @{ RLM_HOME = $indexHome; RLM_API_TOKEN = $token; RLM_DISABLE_SCHEDULER = "1" }
    Start-Draining $api
    Wait-Healthy $url
    $documents = Invoke-Http GET "$url/documents?limit=1000" -Token $token
    Assert-Status $documents 200 "GET /documents"
    $names = @($documents.Json.documents | ForEach-Object { $_.file_name })
    foreach ($format in $CorpusFormats) {
        if (-not ($names | Where-Object { $_.ToLower().EndsWith($format) })) { Fail "no $format document in the index" }
    }
    Pass "every format is in the index"
    foreach ($provider in @("anthropic", "openai")) {
        $models = Invoke-Http POST "$url/llm/models" -Token $token -Body @{ provider = $provider; api_key = "smoke-test-dummy-key" }  # pragma: allowlist secret
        if ($models.Status -ne 502 -or $models.Text -notmatch "Could not reach") {
            Fail "POST /llm/models ($provider) answered $($models.Status): $($models.Text)"
        }
        Pass "POST /llm/models ($provider) -> 502 could not reach the provider"
    }
    $remote = Invoke-Http GET "$url/remote-access" -Token $token
    Assert-Status $remote 200 "GET /remote-access"
    if (-not $remote.Json.sdk.available -or -not $remote.Json.sdk.version) {
        Fail "the ngrok SDK cannot be imported from the build: $($remote.Text)"
    }
    if ($remote.Json.enabled -or $remote.Json.status.state -ne "off") {
        Fail "remote access is not off by default: $($remote.Text)"
    }
    Pass "the ngrok SDK $($remote.Json.sdk.version) is bundled; remote access is off by default"

    # The command-line client (rootmind-backend search) against the same API.
    $tokenFile = Join-Path $WorkDir "rest_token.txt"
    [IO.File]::WriteAllText($tokenFile, $token, (New-Object System.Text.UTF8Encoding $false))
    $client = Start-Backend -Arguments @("search", "invoice", "--json", "--url", $url, "--token-file", $tokenFile) `
        -Environment @{ RLM_HOME = $indexHome }
    $client.Process.StandardInput.Close()
    $clientOutput = $client.Process.StandardOutput.ReadToEnd()
    $client.Process.WaitForExit()
    if ($client.Process.ExitCode -ne 0) { Fail "search exited with $($client.Process.ExitCode): $($client.Stderr.Result)" }
    $found = $clientOutput | ConvertFrom-Json
    if ($found.count -lt 1) { Fail "search through the client found nothing: $clientOutput" }
    Pass "client command search -> $($found.count) result(s) through the API"
    Stop-Backend $api

    # --- mcp-http -----------------------------------------------------------------------------
    $mcpToken = New-Token
    $mcp = Start-Backend -Arguments @("mcp-http", "--host", "127.0.0.1", "--port", "$McpPort") `
        -Environment @{ RLM_HOME = $indexHome; MCP_API_TOKEN = $mcpToken }
    $mcp.Pending = $mcp.Process.StandardOutput.ReadToEndAsync()
    $mcpUrl = "http://127.0.0.1:$McpPort"
    Wait-Healthy $mcpUrl
    $initialize = @{ jsonrpc = "2.0"; id = 1; method = "initialize"; params = @{
            protocolVersion = "2025-06-18"; capabilities = @{}; clientInfo = @{ name = "smoke-test"; version = "1" } } }
    Assert-Status (Invoke-Http POST "$mcpUrl/mcp" -Body $initialize) 401 "mcp-http initialize without the token"
    Assert-Status (Invoke-Http POST "$mcpUrl/mcp" -Token "wrong-token" -Body $initialize) 401 "mcp-http initialize with a wrong token"
    Assert-Status (Invoke-Http POST "$mcpUrl/mcp" -Token $mcpToken -Body $initialize) 200 "mcp-http initialize with the token"
    $list = Invoke-Http POST "$mcpUrl/mcp" -Token $mcpToken -Body @{ jsonrpc = "2.0"; id = 2; method = "tools/list"; params = @{} }
    Assert-Status $list 200 "mcp-http tools/list"
    $tools = @($list.Json.result.tools | ForEach-Object { $_.name })
    foreach ($name in $ToolNames) { if ($tools -notcontains $name) { Fail "mcp-http tools/list lacks $name (got $($tools -join ', '))" } }
    Pass "mcp-http lists the five tools"
    $mcp.Process.Kill()
    $mcp.Process.WaitForExit(10000) | Out-Null

    # --- mcp over stdio -----------------------------------------------------------------------
    $stdio = Start-Backend -Arguments @("mcp") -Environment @{ RLM_HOME = $indexHome }
    $requests = @(
        (@{ jsonrpc = "2.0"; id = 1; method = "initialize"; params = @{
                protocolVersion = "2025-06-18"; capabilities = @{}; clientInfo = @{ name = "smoke-test"; version = "1" } } }),
        (@{ jsonrpc = "2.0"; method = "notifications/initialized" }),
        (@{ jsonrpc = "2.0"; id = 2; method = "tools/list"; params = @{} })
    )
    $responses = @{}
    foreach ($message in $requests) {
        Send-Stdin $stdio (($message | ConvertTo-Json -Depth 10 -Compress) + "`n")
        if (-not $message.ContainsKey("id")) { continue }
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        while (-not $responses.ContainsKey($message.id)) {
            $pending = $stdio.Process.StandardOutput.ReadLineAsync()
            $left = [int]([Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds))
            if (-not $pending.Wait($left)) { Fail "mcp stdio: no answer to request $($message.id)" }
            $line = $pending.Result
            if ($null -eq $line) { Fail "mcp stdio exited early: $($stdio.Stderr.Result)" }
            if (-not $line.Trim()) { continue }
            try { $parsed = $line | ConvertFrom-Json } catch { Fail "mcp stdio wrote a line that is not JSON: $line" }
            if ($parsed.PSObject.Properties.Name -contains "id") { $responses[[int]$parsed.id] = $parsed }
        }
    }
    if (-not $responses[1].result.serverInfo) { Fail "mcp stdio initialize failed: $($responses[1] | ConvertTo-Json -Compress)" }
    $stdioTools = @($responses[2].result.tools | ForEach-Object { $_.name })
    foreach ($name in $ToolNames) { if ($stdioTools -notcontains $name) { Fail "mcp stdio tools/list lacks $name" } }
    $stdio.Process.StandardInput.Close()
    $rest = $stdio.Process.StandardOutput.ReadToEnd()
    foreach ($line in ($rest -split "`r?`n" | Where-Object { $_.Trim() })) {
        try { $line | ConvertFrom-Json | Out-Null } catch { Fail "mcp stdio wrote a line that is not JSON: $line" }
    }
    Wait-Exit $stdio 10 "mcp stdio after stdin was closed"
    Pass "mcp stdio: initialize and tools/list, JSON-only stdout"
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    foreach ($process in $Started) {
        if (-not $process.HasExited) {
            try { $process.Kill() } catch { Write-Host "could not stop process $($process.Id)" }
        }
    }
    exit 1
} finally {
    foreach ($process in $Started) {
        if (-not $process.HasExited) {
            try { $process.Kill() } catch { Write-Host "could not stop process $($process.Id)" }
        }
    }
}

if ($env:GITHUB_STEP_SUMMARY) {
    $lines = @("### Backend smoke test", "", "Version $ExpectedVersion") + ($Summary | ForEach-Object { "- $_" })
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value ($lines -join "`n") -Encoding utf8
}
$Summary | ForEach-Object { Write-Host $_ }
Write-Host "Smoke test passed."
exit 0
