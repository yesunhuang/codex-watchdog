[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PlinkPath,

    [Parameter(Mandatory = $true)]
    [string]$Target
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PlinkPath -PathType Leaf)) {
    throw "Plink executable is missing: $PlinkPath"
}
if ($Target -notmatch (
    '^[A-Za-z0-9._-]+@' +
    '[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$'
)) {
    throw "Target must have the form user@host."
}

$Host.UI.RawUI.WindowTitle = "Codex Watchdog Duo SSH"
Write-Host "Authenticate $Target, approve Duo, and keep this window open."
Write-Host "Close this window when you want to disable the Duo fallback."
& $PlinkPath -ssh -share -N $Target
$exitCode = $LASTEXITCODE
Write-Warning "The shared SSH connection ended with code $exitCode."
Read-Host "Press Enter to close this window"
