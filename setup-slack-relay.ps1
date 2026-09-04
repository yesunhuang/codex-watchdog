[CmdletBinding()]
param(
    [string]$ChannelId,

    [string[]]$AllowedUserId = @(),

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-SlackChannelId {
    param([Parameter(Mandatory = $true)][string]$Value)

    return $Value -cmatch '^[CG][A-Z0-9]{8,}$'
}

function Test-SlackUserId {
    param([Parameter(Mandatory = $true)][string]$Value)

    return $Value -cmatch '^[UW][A-Z0-9]{8,}$'
}

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "LOCALAPPDATA is required for the Windows DPAPI credential store."
}

$directory = Join-Path $env:LOCALAPPDATA "CodexWatchdog"
$botStore = Join-Path $directory "slack-bot-token.clixml"
$appStore = Join-Path $directory "slack-app-token.clixml"
$relayStore = Join-Path $directory "slack-relay.json"
$existing = @(
    @($botStore, $appStore, $relayStore) | Where-Object {
        Test-Path -LiteralPath $_
    }
)
if ($existing.Count -gt 0 -and -not $Force) {
    throw "Slack relay configuration already exists. Re-run with -Force to replace it."
}

if ([string]::IsNullOrWhiteSpace($ChannelId)) {
    $ChannelId = (Read-Host "Slack channel ID").Trim()
}
if (-not (Test-SlackChannelId $ChannelId)) {
    throw "Slack channel ID must begin with C or G and contain only uppercase letters and digits. Direct-message D IDs are not supported."
}

if ($AllowedUserId.Count -eq 0) {
    $AllowedUserId = @(
        (Read-Host "Allowed Slack member ID(s), comma-separated") -split ',' |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}
$AllowedUserId = @($AllowedUserId | Select-Object -Unique)
if (
    $AllowedUserId.Count -eq 0 -or
    @($AllowedUserId | Where-Object { -not (Test-SlackUserId $_) }).Count -gt 0
) {
    throw "At least one valid Slack member ID beginning with U or W is required."
}

$botToken = Read-Host "Slack bot token (xoxb-...)" -AsSecureString
$appToken = Read-Host "Slack app token (xapp-...)" -AsSecureString
$botPlain = [pscredential]::new("temporary", $botToken).GetNetworkCredential().Password
$appPlain = [pscredential]::new("temporary", $appToken).GetNetworkCredential().Password
try {
    if ($botPlain -cnotmatch '^xoxb-') {
        throw "The Slack bot token must begin with xoxb-."
    }
    if ($appPlain -cnotmatch '^xapp-') {
        throw "The Slack app token must begin with xapp-."
    }
} finally {
    Remove-Variable botPlain,appPlain -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $directory | Out-Null
[pscredential]::new("slack-bot-token", $botToken) |
    Export-Clixml -LiteralPath $botStore
[pscredential]::new("slack-app-token", $appToken) |
    Export-Clixml -LiteralPath $appStore
[pscustomobject][ordered]@{
    schema_version = 1
    channel_id = $ChannelId
    allowed_user_ids = $AllowedUserId
} | ConvertTo-Json | Set-Content -LiteralPath $relayStore -Encoding UTF8

Remove-Variable botToken,appToken
[pscustomobject][ordered]@{
    status = "saved"
    allowed_user_count = $AllowedUserId.Count
    secret_protection = "windows_dpapi_current_user"
} | ConvertTo-Json -Compress
