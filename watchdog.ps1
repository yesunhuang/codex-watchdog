[CmdletBinding()]
param(
    [ValidateRange(1, 86400)]
    [double]$IntervalSeconds = 120,

    [string[]]$Exclude = @(),

    [switch]$Once,

    [switch]$ReplayLatestStop,

    [switch]$ManualOnly,

    [switch]$DryRun,

    [switch]$SlackRelayTest,

    [string]$SlackRelayWorkspace,

    [string]$Runtime = ".codex-watchdog",

    [string]$DuoTarget,

    [string]$PlinkPath,

    [switch]$SaveDuoConfig,

    [switch]$NoDuo,

    [ValidateRange(10, 600)]
    [int]$DuoWaitSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-SlackWebhookUrl {
    param([Parameter(Mandatory = $true)][string]$Value)

    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) {
        return $false
    }
    $validHosts = @("hooks.slack.com", "hooks.slack-gov.com")
    return (
        $uri.Scheme -eq "https" -and
        $uri.Host -in $validHosts -and
        $uri.AbsolutePath.StartsWith("/services/", [StringComparison]::Ordinal)
    )
}

function Test-SlackChannelId {
    param([Parameter(Mandatory = $true)][string]$Value)

    return $Value -cmatch '^[CG][A-Z0-9]{8,}$'
}

function Test-SlackUserId {
    param([Parameter(Mandatory = $true)][string]$Value)

    return $Value -cmatch '^[UW][A-Z0-9]{8,}$'
}

function Import-DpapiSecret {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedUserName,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $credential = Import-Clixml -LiteralPath $Path
    if (
        $credential -isnot [pscredential] -or
        $credential.UserName -ne $ExpectedUserName
    ) {
        throw "The encrypted $Description credential has an unexpected format."
    }
    return $credential.GetNetworkCredential().Password
}

function Test-DuoTarget {
    param([Parameter(Mandatory = $true)][string]$Value)

    return $Value -match (
        '^[A-Za-z0-9._-]+@' +
        '[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$'
    )
}

function Resolve-PlinkExecutable {
    param(
        [string]$Requested,
        [Parameter(Mandatory = $true)][string]$RuntimePath
    )

    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        $resolved = if ([IO.Path]::IsPathRooted($Requested)) {
            [IO.Path]::GetFullPath($Requested)
        } else {
            [IO.Path]::GetFullPath((Join-Path (Get-Location) $Requested))
        }
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Plink executable is missing: $resolved"
        }
        return $resolved
    }

    $command = Get-Command plink.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $command) {
        return $command.Source
    }
    $candidates = @(
        (Join-Path $RuntimePath "bin\plink.exe"),
        "C:\Program Files\PuTTY\plink.exe",
        "C:\Program Files (x86)\PuTTY\plink.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw (
        "Plink is required for the Duo fallback. Install PuTTY or place the " +
        "official plink.exe at '$RuntimePath\bin\plink.exe'."
    )
}

function Test-PlinkUpstream {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$Target
    )

    & $Executable -batch -ssh -shareexists $Target 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Test-PlinkUpstreamReady {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$Target
    )

    if (-not (Test-PlinkUpstream $Executable $Target)) {
        return $false
    }
    $probe = Start-Process -FilePath $Executable -ArgumentList @(
        "-batch", "-ssh", "-share", $Target, "true"
    ) -WindowStyle Hidden -PassThru
    if (-not $probe.WaitForExit(5000)) {
        Stop-Process -Id $probe.Id -ErrorAction SilentlyContinue
        Wait-Process -Id $probe.Id -Timeout 5 -ErrorAction SilentlyContinue
        return $false
    }
    return $probe.ExitCode -eq 0
}

$repoRoot = $PSScriptRoot
$packagedExecutable = Join-Path $repoRoot "codex-watchdog.exe"
$watchdogPrefixArguments = @()
if (Test-Path -LiteralPath $packagedExecutable -PathType Leaf) {
    $watchdogRunner = $packagedExecutable
    $watchdogRunnerSource = "packaged_executable"
} else {
    $launcher = Join-Path $repoRoot "tools\codex_watchdog.py"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw (
            "Codex WatchDog is incomplete: expected codex-watchdog.exe or " +
            "the source-checkout launcher at $launcher"
        )
    }
    $python = Get-Command python -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $watchdogRunner = $python.Source
    $watchdogPrefixArguments = @($launcher)
    $watchdogRunnerSource = "python_source_checkout"
}
$runtimePath = if ([IO.Path]::IsPathRooted($Runtime)) {
    [IO.Path]::GetFullPath($Runtime)
} else {
    [IO.Path]::GetFullPath((Join-Path $repoRoot $Runtime))
}

if ($SlackRelayTest -and [string]::IsNullOrWhiteSpace($SlackRelayWorkspace)) {
    throw "-SlackRelayTest requires -SlackRelayWorkspace."
}
if (-not $SlackRelayTest -and -not [string]::IsNullOrWhiteSpace($SlackRelayWorkspace)) {
    throw "-SlackRelayWorkspace is valid only with -SlackRelayTest."
}
if ($SlackRelayTest -and ($Once -or $DryRun)) {
    throw "-SlackRelayTest cannot be combined with -Once or -DryRun."
}

$duoSource = "not_configured"
$duoConfigPath = $null
$resolvedPlink = $null
if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $duoConfigPath = Join-Path $env:LOCALAPPDATA "CodexWatchdog\duo-fallback.json"
}
if ($NoDuo -and ($SaveDuoConfig -or -not [string]::IsNullOrWhiteSpace($DuoTarget))) {
    throw "-NoDuo cannot be combined with -DuoTarget or -SaveDuoConfig."
}
if (-not $NoDuo -and [string]::IsNullOrWhiteSpace($DuoTarget) -and $null -ne $duoConfigPath) {
    if (Test-Path -LiteralPath $duoConfigPath -PathType Leaf) {
        $storedDuo = Get-Content -LiteralPath $duoConfigPath -Raw | ConvertFrom-Json
        if (
            $storedDuo.schema_version -ne 1 -or
            [string]::IsNullOrWhiteSpace([string]$storedDuo.target)
        ) {
            throw "The saved Duo fallback configuration has an unexpected format."
        }
        $DuoTarget = [string]$storedDuo.target
        if ([string]::IsNullOrWhiteSpace($PlinkPath)) {
            $PlinkPath = [string]$storedDuo.plink_path
        }
        $duoSource = "saved_config"
    }
}
if (-not $NoDuo -and -not [string]::IsNullOrWhiteSpace($DuoTarget)) {
    if (-not (Test-DuoTarget $DuoTarget)) {
        throw "DuoTarget must have the form user@host."
    }
    $resolvedPlink = Resolve-PlinkExecutable -Requested $PlinkPath -RuntimePath $runtimePath
    $env:CODEX_WATCHDOG_DUO_PLINK_TARGET = $DuoTarget
    $env:CODEX_WATCHDOG_PLINK_EXE = $resolvedPlink
    if ($duoSource -eq "not_configured") {
        $duoSource = "command_line"
    }
    if ($SaveDuoConfig) {
        if ($null -eq $duoConfigPath) {
            throw "LOCALAPPDATA is required to save the Duo fallback configuration."
        }
        $duoConfigDirectory = Split-Path -Parent $duoConfigPath
        New-Item -ItemType Directory -Path $duoConfigDirectory -Force | Out-Null
        [pscustomobject][ordered]@{
            schema_version = 1
            target = $DuoTarget
            plink_path = $resolvedPlink
        } | ConvertTo-Json | Set-Content -LiteralPath $duoConfigPath -Encoding UTF8
        $duoSource = "saved_config"
    }
} else {
    Remove-Item Env:CODEX_WATCHDOG_DUO_PLINK_TARGET -ErrorAction SilentlyContinue
    Remove-Item Env:CODEX_WATCHDOG_PLINK_EXE -ErrorAction SilentlyContinue
}

$slackSource = "not_configured"
$configuredWebhook = $env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL
if (-not [string]::IsNullOrWhiteSpace($configuredWebhook)) {
    if (-not (Test-SlackWebhookUrl $configuredWebhook)) {
        throw "CODEX_WATCHDOG_SLACK_WEBHOOK_URL is not a valid Slack webhook URL."
    }
    $slackSource = "environment"
} elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $slackStore = Join-Path $env:LOCALAPPDATA "CodexWatchdog\slack-webhook.clixml"
    if (Test-Path -LiteralPath $slackStore -PathType Leaf) {
        $credential = Import-Clixml -LiteralPath $slackStore
        if (
            $credential -isnot [pscredential] -or
            $credential.UserName -ne "slack-webhook"
        ) {
            throw "The encrypted Slack credential has an unexpected format."
        }
        $configuredWebhook = $credential.GetNetworkCredential().Password
        if (-not (Test-SlackWebhookUrl $configuredWebhook)) {
            throw "The encrypted Slack credential does not contain a valid webhook URL."
        }
        $env:CODEX_WATCHDOG_SLACK_WEBHOOK_URL = $configuredWebhook
        $slackSource = "encrypted_store"
        Remove-Variable credential
    }
}
Remove-Variable configuredWebhook

$slackRelayNames = @(
    "CODEX_WATCHDOG_SLACK_BOT_TOKEN",
    "CODEX_WATCHDOG_SLACK_APP_TOKEN",
    "CODEX_WATCHDOG_SLACK_CHANNEL_ID",
    "CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS"
)
$slackRelayInitiallySet = @(
    $slackRelayNames | Where-Object {
        -not [string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable($_, "Process")
        )
    }
).Count
$slackRelaySource = "not_configured"
if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $slackDirectory = Join-Path $env:LOCALAPPDATA "CodexWatchdog"
    $slackBotStore = Join-Path $slackDirectory "slack-bot-token.clixml"
    $slackAppStore = Join-Path $slackDirectory "slack-app-token.clixml"
    $slackRelayStore = Join-Path $slackDirectory "slack-relay.json"
    if (
        [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_BOT_TOKEN) -and
        (Test-Path -LiteralPath $slackBotStore -PathType Leaf)
    ) {
        $env:CODEX_WATCHDOG_SLACK_BOT_TOKEN = Import-DpapiSecret `
            -Path $slackBotStore -ExpectedUserName "slack-bot-token" `
            -Description "Slack bot token"
    }
    if (
        [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_APP_TOKEN) -and
        (Test-Path -LiteralPath $slackAppStore -PathType Leaf)
    ) {
        $env:CODEX_WATCHDOG_SLACK_APP_TOKEN = Import-DpapiSecret `
            -Path $slackAppStore -ExpectedUserName "slack-app-token" `
            -Description "Slack app token"
    }
    if (
        (
            [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_CHANNEL_ID) -or
            [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS)
        ) -and
        (Test-Path -LiteralPath $slackRelayStore -PathType Leaf)
    ) {
        $storedRelay = Get-Content -LiteralPath $slackRelayStore -Raw |
            ConvertFrom-Json
        if (
            $storedRelay.schema_version -ne 1 -or
            [string]::IsNullOrWhiteSpace([string]$storedRelay.channel_id) -or
            $null -eq $storedRelay.allowed_user_ids
        ) {
            throw "The saved Slack relay configuration has an unexpected format."
        }
        if ([string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_CHANNEL_ID)) {
            $env:CODEX_WATCHDOG_SLACK_CHANNEL_ID = [string]$storedRelay.channel_id
        }
        if ([string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS)) {
            $env:CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS = @(
                $storedRelay.allowed_user_ids
            ) -join ","
        }
        Remove-Variable storedRelay
    }
}

$slackAllowedUsers = @(
    ([string]$env:CODEX_WATCHDOG_SLACK_ALLOWED_USER_IDS) -split '[,;\s]+' |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
$slackRelayValuesSet = @(
    $slackRelayNames | Where-Object {
        -not [string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable($_, "Process")
        )
    }
).Count
$slackRelayConfigured = (
    $env:CODEX_WATCHDOG_SLACK_BOT_TOKEN -cmatch '^xoxb-' -and
    $env:CODEX_WATCHDOG_SLACK_APP_TOKEN -cmatch '^xapp-' -and
    -not [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SLACK_CHANNEL_ID) -and
    (Test-SlackChannelId $env:CODEX_WATCHDOG_SLACK_CHANNEL_ID) -and
    $slackAllowedUsers.Count -gt 0 -and
    @($slackAllowedUsers | Where-Object { -not (Test-SlackUserId $_) }).Count -eq 0
)
if ($slackRelayValuesSet -gt 0 -and -not $slackRelayConfigured) {
    throw (
        "Slack reply relay configuration is incomplete or invalid. Configure " +
        "both tokens, one channel ID, and at least one allowed user ID."
    )
}
if ($slackRelayConfigured) {
    $slackRelaySource = if ($slackRelayInitiallySet -eq $slackRelayNames.Count) {
        "environment"
    } elseif ($slackRelayInitiallySet -gt 0) {
        "mixed"
    } else {
        "encrypted_store"
    }
}
Remove-Variable slackAllowedUsers

$smtpConfigured = (
    -not [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SMTP_HOST) -and
    -not [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SMTP_FROM) -and
    -not [string]::IsNullOrWhiteSpace($env:CODEX_WATCHDOG_SMTP_TO)
)

$arguments = @(
    "--runtime",
    $runtimePath,
    "run",
    "--interval",
    $IntervalSeconds.ToString([Globalization.CultureInfo]::InvariantCulture)
)
if ($Once) {
    $arguments += "--once"
}
if ($ReplayLatestStop) {
    $arguments += "--replay-latest-stop"
}
if ($ManualOnly) {
    $arguments += "--manual-only"
}
foreach ($item in $Exclude) {
    if (-not [string]::IsNullOrWhiteSpace($item)) {
        $arguments += @("--exclude", $item)
    }
}

$summary = [pscustomobject][ordered]@{
    status = "ready"
    runtime = $runtimePath
    interval_seconds = $IntervalSeconds
    once = [bool]$Once
    excluded = @($Exclude | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    slack = $slackSource
    slack_reply = $slackRelaySource
    smtp_configured = $smtpConfigured
    duo_fallback = $duoSource
    runner = $watchdogRunnerSource
}

if ($DryRun) {
    $summary | ConvertTo-Json -Compress
    return
}

if ($SlackRelayTest) {
    if (-not $slackRelayConfigured) {
        throw "Slack reply relay is not configured."
    }
    $testId = "slack-relay-" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $summary | Format-List | Out-Host
    $relayArguments = @($watchdogPrefixArguments) + @(
        "--runtime", $runtimePath, "slack-relay-test",
        "--id", $testId, "--workspace", $SlackRelayWorkspace
    )
    & $watchdogRunner @relayArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Slack relay test exited with code $LASTEXITCODE."
    }
    return
}

$duoWorkspaceOpen = $false
if ($null -ne $resolvedPlink -and -not $ManualOnly) {
    $discoveryArguments = @(
        "--runtime",
        $runtimePath,
        "workspace-discover"
    )
    foreach ($item in $Exclude) {
        if (-not [string]::IsNullOrWhiteSpace($item)) {
            $discoveryArguments += @("--exclude", $item)
        }
    }
    $discoveryInvokeArguments = @($watchdogPrefixArguments) + $discoveryArguments
    $discoveryOutput = & $watchdogRunner @discoveryInvokeArguments
    if ($LASTEXITCODE -notin @(0, 1)) {
        throw "Workspace discovery failed before the Duo fallback bootstrap."
    }
    $discovery = $discoveryOutput | ConvertFrom-Json
    $duoHost = ($DuoTarget -split "@", 2)[1]
    $expectedAuthority = "ssh-remote+$duoHost"
    $duoWorkspaceOpen = @(
        $discovery.windows | Where-Object {
            $_.remote_authority -ieq $expectedAuthority -and
            $_.tracking_status -eq "remote_adapter"
        }
    ).Count -gt 0
}

if ($duoWorkspaceOpen) {
    if (-not (Test-PlinkUpstreamReady $resolvedPlink $DuoTarget)) {
        Write-Host (
            "No shared SSH connection exists for $DuoTarget. " +
            "Opening an interactive Plink window for password and Duo approval..."
        )
        $bootstrapLauncher = Join-Path $repoRoot "duo-upstream.ps1"
        $bootstrapArguments = (
            '-NoProfile -File "{0}" -PlinkPath "{1}" -Target {2}' -f
            $bootstrapLauncher.Replace('"', '\"'),
            $resolvedPlink.Replace('"', '\"'),
            $DuoTarget
        )
        $bootstrap = Start-Process -FilePath "powershell.exe" `
            -ArgumentList $bootstrapArguments -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds($DuoWaitSeconds)
        $upstreamReady = $false
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($bootstrap.HasExited) {
                throw "The interactive Plink bootstrap exited before sharing was ready."
            }
            if (Test-PlinkUpstreamReady $resolvedPlink $DuoTarget) {
                $upstreamReady = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $upstreamReady) {
            throw "Timed out waiting for the operator-authenticated Plink connection."
        }
        Write-Host "Duo-approved shared SSH connection is ready."
    }
}

$summary | Format-List | Out-Host
$invokeArguments = @($watchdogPrefixArguments) + $arguments
& $watchdogRunner @invokeArguments
if ($LASTEXITCODE -ne 0) {
    throw "Codex Watchdog exited with code $LASTEXITCODE."
}
