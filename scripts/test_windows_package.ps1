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
$icon = Join-Path $package "images\codex-watchdog.ico"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Packaged executable is missing: $executable"
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Packaged launcher is missing: $launcher"
}
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Packaged approved icon is missing: $icon"
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
    "CODEX_WATCHDOG_SMTP_AUTH",
    "CODEX_WATCHDOG_OUTLOOK_CLIENT_ID",
    "CODEX_WATCHDOG_SMTP_HOST",
    "CODEX_WATCHDOG_SMTP_PORT",
    "CODEX_WATCHDOG_SMTP_SECURITY",
    "CODEX_WATCHDOG_SMTP_USERNAME",
    "CODEX_WATCHDOG_SMTP_FROM",
    "CODEX_WATCHDOG_SMTP_TO",
    "CODEX_WATCHDOG_NOTIFICATION_TIMEOUT_SECONDS",
    "CODEX_WATCHDOG_SMTP_PASSWORD",
    "CODEX_WATCHDOG_PACKAGE_TEST_ONCE"
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
    $legacyRuntime = Join-Path $testRoot "legacy-v0.1.0-runtime"
    New-Item -ItemType Directory -Path $legacyRuntime -Force | Out-Null
    New-Item -ItemType Directory -Path $env:CODEX_HOME -Force | Out-Null
    $legacyHooks = [pscustomobject]@{
        hooks = [pscustomobject]@{
            Stop = @([pscustomobject]@{
                hooks = @([pscustomobject]@{
                    commandWindows = "C:\legacy\codex-watchdog.exe --runtime `"$legacyRuntime`" hook --grace-seconds 30"
                })
            })
        }
    }
    $legacyHooks | ConvertTo-Json -Depth 8 | Set-Content `
        -LiteralPath (Join-Path $env:CODEX_HOME "hooks.json") -Encoding UTF8
    $savedConfigRoot = Join-Path $env:LOCALAPPDATA "CodexWatchdog"
    New-Item -ItemType Directory -Path $savedConfigRoot -Force | Out-Null
    $testWebhook = "https://hooks.slack.com/services/T00000000/B00000000/package-test-secret"
    $testBotToken = "xoxb-package-test-secret"
    $testAppToken = "xapp-package-test-secret"
    [pscredential]::new(
        "slack-webhook",
        (ConvertTo-SecureString $testWebhook -AsPlainText -Force)
    ) | Export-Clixml -LiteralPath (Join-Path $savedConfigRoot "slack-webhook.clixml")
    [pscredential]::new(
        "slack-bot-token",
        (ConvertTo-SecureString $testBotToken -AsPlainText -Force)
    ) | Export-Clixml -LiteralPath (Join-Path $savedConfigRoot "slack-bot-token.clixml")
    [pscredential]::new(
        "slack-app-token",
        (ConvertTo-SecureString $testAppToken -AsPlainText -Force)
    ) | Export-Clixml -LiteralPath (Join-Path $savedConfigRoot "slack-app-token.clixml")
    [pscustomobject][ordered]@{
        schema_version = 1
        channel_id = "C12345678"
        allowed_user_ids = @("U12345678")
    } | ConvertTo-Json | Set-Content `
        -LiteralPath (Join-Path $savedConfigRoot "slack-relay.json") -Encoding UTF8
    $testPlink = Join-Path $testRoot "plink.exe"
    Set-Content -LiteralPath $testPlink -Value "placeholder" -Encoding ASCII
    [pscustomobject][ordered]@{
        schema_version = 1
        target = "package-test@example.invalid"
        plink_path = $testPlink
    } | ConvertTo-Json | Set-Content `
        -LiteralPath (Join-Path $savedConfigRoot "duo-fallback.json") -Encoding UTF8
    $env:CODEX_WATCHDOG_SMTP_AUTH = "outlook_oauth2"
    $env:CODEX_WATCHDOG_OUTLOOK_CLIENT_ID = "00000000-0000-4000-8000-000000000000"
    $env:CODEX_WATCHDOG_SMTP_HOST = "smtp-mail.outlook.com"
    $env:CODEX_WATCHDOG_SMTP_PORT = "587"
    $env:CODEX_WATCHDOG_SMTP_SECURITY = "starttls"
    $env:CODEX_WATCHDOG_SMTP_USERNAME = "package-test@example.invalid"
    $env:CODEX_WATCHDOG_SMTP_FROM = "package-test@example.invalid"
    $env:CODEX_WATCHDOG_SMTP_TO = "package-test@example.invalid"
    $env:CODEX_WATCHDOG_NOTIFICATION_TIMEOUT_SECONDS = "15"
    $env:CODEX_WATCHDOG_PACKAGE_TEST_ONCE = "1"
    $oneClickOutput = & $executable
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged no-argument one-click startup failed."
    }
    $profilePath = Join-Path $env:LOCALAPPDATA "CodexWatchdog\launcher-profile.json"
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        throw "One-click startup did not persist a launcher profile."
    }
    $profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
    if (
        $profile.schema_version -ne 1 -or
        [IO.Path]::GetFullPath([string]$profile.runtime_path) -ne [IO.Path]::GetFullPath($legacyRuntime)
    ) {
        throw "One-click upgrade did not preserve the previous release runtime."
    }
    $oneClickText = $oneClickOutput | Out-String
    if ($oneClickText -notmatch "runner\s*:\s*packaged_executable") {
        throw "One-click startup did not use the packaged executable bootstrap."
    }
    foreach ($expected in @(
        "slack\s*:\s*encrypted_store",
        "slack_reply\s*:\s*encrypted_store",
        "smtp_configured\s*:\s*True",
        "duo_fallback\s*:\s*saved_config"
    )) {
        if ($oneClickText -notmatch $expected) {
            throw "One-click upgrade did not reuse expected saved configuration: $expected"
        }
    }
    foreach ($secret in @($testWebhook, $testBotToken, $testAppToken)) {
        if ($oneClickText.IndexOf($secret, [StringComparison]::Ordinal) -ge 0) {
            throw "One-click upgrade exposed a saved Slack secret."
        }
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
        one_click_upgrade_runtime = [string]$profile.runtime_path
        one_click_saved_configuration = "reused_without_secret_output"
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
