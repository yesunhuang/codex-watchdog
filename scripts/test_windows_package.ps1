[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PackageDirectory,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedVersion
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$package = [IO.Path]::GetFullPath($PackageDirectory)
$executable = Join-Path $package "codex-watchdog.exe"
$launcher = Join-Path $package "watchdog.ps1"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Packaged executable is missing: $executable"
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Packaged launcher is missing: $launcher"
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("codex-watchdog-package-test-" + [guid]::NewGuid().ToString("N"))
$savedEnvironment = @{}
$environmentNames = @(
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "LOCALAPPDATA",
    "APPDATA",
    "CODEX_HOME",
    "CODEX_WATCHDOG_SLACK_WEBHOOK_URL",
    "CODEX_WATCHDOG_SLACK_BOT_TOKEN",
    "CODEX_WATCHDOG_SLACK_APP_TOKEN",
    "CODEX_WATCHDOG_SLACK_CHANNEL_ID",
    "CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS",
    "CODEX_WATCHDOG_SMTP_PASSWORD"
)
foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:LOCALAPPDATA = Join-Path $testRoot "LocalAppData"
    $env:APPDATA = Join-Path $testRoot "AppData"
    $env:CODEX_HOME = Join-Path $testRoot "codex-home"
    foreach ($name in $environmentNames | Where-Object { $_ -notin @("PATH", "LOCALAPPDATA", "APPDATA", "CODEX_HOME") }) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    if ($null -ne (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "The no-Python smoke environment still resolves python."
    }

    $versionOutput = (& $executable --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionOutput -ne "codex-watchdog $ExpectedVersion") {
        throw "Packaged --version failed: $versionOutput"
    }
    & $executable --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged --help failed."
    }
    $runtime = Join-Path $testRoot "runtime"
    $discoveryOutput = & $executable --runtime $runtime --codex-home $env:CODEX_HOME workspace-discover
    if ($LASTEXITCODE -notin @(0, 1)) {
        throw "Packaged workspace discovery failed to produce a supported result."
    }
    $discovery = $discoveryOutput | ConvertFrom-Json
    if ($discovery.status -notin @("ok", "partial", "blocked", "error")) {
        throw "Unexpected packaged discovery status: $($discovery.status)"
    }
    if ($null -eq $discovery.windows -or $null -eq $discovery.issues) {
        throw "Packaged discovery did not return the expected structured result."
    }
    $onceOutput = & $executable --runtime $runtime --codex-home $env:CODEX_HOME run --once --manual-only
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged one-cycle startup failed."
    }
    $once = $onceOutput | ConvertFrom-Json
    if ($once.status -ne "completed" -or $once.workspace_count -ne 0) {
        throw "Unexpected packaged one-cycle result."
    }
    $hooksOutput = & $executable --runtime $runtime --codex-home $env:CODEX_HOME install-user-hooks
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged hook rendering failed."
    }
    $hooks = $hooksOutput | ConvertFrom-Json
    $hookCommand = [string]$hooks.hooks.Stop[0].hooks[0].commandWindows
    if (
        -not $hookCommand.StartsWith($executable, [StringComparison]::OrdinalIgnoreCase) -or
        $hookCommand -match 'python|codex_watchdog_hook\.py'
    ) {
        throw "Packaged hook does not invoke only the packaged executable."
    }
    $hookInstallOutput = & $executable --runtime $runtime --codex-home $env:CODEX_HOME install-user-hooks --install
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged hook installation failed."
    }
    $hookInstall = $hookInstallOutput | ConvertFrom-Json
    if ($hookInstall.status -ne "installed") {
        throw "Unexpected packaged hook installation status: $($hookInstall.status)"
    }
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $dryRunOutput = & $powershell -NoProfile -File $launcher -DryRun -NoDuo -Runtime $runtime
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged PowerShell dry run failed."
    }
    $dryRun = $dryRunOutput | ConvertFrom-Json
    if ($dryRun.runner -ne "packaged_executable") {
        throw "Packaged launcher did not select codex-watchdog.exe."
    }
    [pscustomobject][ordered]@{
        status = "passed"
        version = $versionOutput
        python_resolvable = $false
        help = "passed"
        discovery_status = $discovery.status
        one_cycle_workspace_count = $once.workspace_count
        packaged_hook_install = $hookInstall.status
        launcher_runner = $dryRun.runner
    } | ConvertTo-Json -Compress
} finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    $testFull = [IO.Path]::GetFullPath($testRoot)
    $tempFull = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (
        $testFull.StartsWith($tempFull, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $testFull).StartsWith("codex-watchdog-package-test-", [StringComparison]::Ordinal)
    ) {
        Remove-Item -LiteralPath $testFull -Recurse -Force -ErrorAction SilentlyContinue
    }
}
