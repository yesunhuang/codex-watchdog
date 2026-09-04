[CmdletBinding()]
param(
    [string]$Python = "python",

    [string]$OutputDirectory = "dist"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pyproject = Join-Path $repoRoot "pyproject.toml"
Push-Location $repoRoot
try {
    $pyprojectText = Get-Content -Raw -LiteralPath $pyproject
    $projectMatch = [regex]::Match(
        $pyprojectText,
        '(?ms)^\[project\]\s*(.*?)(?=^\[|\z)'
    )
    if (-not $projectMatch.Success) {
        throw "Unable to find [project] in pyproject.toml."
    }
    $versionMatch = [regex]::Match(
        $projectMatch.Groups[1].Value,
        '(?m)^version\s*=\s*"([^"]+)"\s*$'
    )
    if (-not $versionMatch.Success) {
        throw "Unable to read the project version."
    }
    $version = $versionMatch.Groups[1].Value
    if ($version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$') {
        throw "Project version is not valid SemVer: $version"
    }
    $architecture = (& $Python -c "import struct; print(struct.calcsize('P') * 8)" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $architecture -ne "64") {
        throw "The Windows release must be built with 64-bit Python."
    }
    $pyinstallerVersion = (& $Python -m PyInstaller --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is unavailable in the selected Python environment."
    }
    if ($pyinstallerVersion -ne "6.22.2") {
        throw "The verified recipe requires PyInstaller 6.22.2; found $pyinstallerVersion."
    }

    $buildRoot = Join-Path $repoRoot "build\windows-release-$version"
    $outputRoot = if ([IO.Path]::IsPathRooted($OutputDirectory)) {
        [IO.Path]::GetFullPath($OutputDirectory)
    } else {
        [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
    }
    $packageName = "codex-watchdog-v$version-windows-x64"
    $packageDirectory = Join-Path $outputRoot $packageName
    $zipPath = Join-Path $outputRoot "$packageName.zip"
    $releaseChecksums = Join-Path $outputRoot "SHA256SUMS.txt"
    foreach ($path in @($buildRoot, $packageDirectory)) {
        $full = [IO.Path]::GetFullPath($path)
        if (-not $full.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a path outside the repository: $full"
        }
        if (Test-Path -LiteralPath $full) {
            Remove-Item -LiteralPath $full -Recurse -Force
        }
    }
    foreach ($path in @($zipPath, $releaseChecksums)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }
    $binaryDirectory = Join-Path $buildRoot "binary"
    $workDirectory = Join-Path $buildRoot "work"
    $specDirectory = Join-Path $buildRoot "spec"
    New-Item -ItemType Directory -Path $binaryDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

    $pyinstallerArguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name", "codex-watchdog",
        "--paths", (Join-Path $repoRoot "src"),
        "--distpath", $binaryDirectory,
        "--workpath", $workDirectory,
        "--specpath", $specDirectory,
        "--copy-metadata", "codex-watchdog",
        "--collect-submodules", "msal_extensions",
        "--collect-submodules", "slack_bolt",
        "--collect-submodules", "slack_sdk",
        "--collect-data", "certifi",
        "--exclude-module", "tkinter",
        (Join-Path $repoRoot "packaging\windows_entry.py")
    )
    & $Python @pyinstallerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }
    $executable = Join-Path $binaryDirectory "codex-watchdog.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce codex-watchdog.exe."
    }
    $reportedVersion = (& $executable --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $reportedVersion -ne "codex-watchdog $version") {
        throw "Packaged version mismatch: $reportedVersion"
    }
    & $executable --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged --help smoke test failed."
    }

    New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
    Copy-Item -LiteralPath $executable -Destination $packageDirectory
    foreach ($name in @(
        "watchdog.ps1",
        "setup-slack-relay.ps1",
        "duo-upstream.ps1",
        "WINDOWS_PACKAGE.md",
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "ASSETS.md"
    )) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $name) -Destination $packageDirectory
    }
    $packageImages = Join-Path $packageDirectory "images"
    New-Item -ItemType Directory -Path $packageImages -Force | Out-Null
    foreach ($name in @(
        "parrotDogLogo.png",
        "watchdog_workflow_en.png",
        "parrot_workflow_en.png"
    )) {
        Copy-Item -LiteralPath (Join-Path $repoRoot "images\$name") -Destination $packageImages
    }
    & $Python (Join-Path $repoRoot "tools\generate_dependency_licenses.py") `
        --output (Join-Path $packageDirectory "THIRD_PARTY_LICENSES") `
        --include-distribution pyinstaller `
        --include-distribution importlib-metadata `
        --include-distribution packaging `
        --include-distribution setuptools `
        --include-distribution zipp
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency-license generation failed."
    }

    $packageChecksums = Get-ChildItem -LiteralPath $packageDirectory -File -Recurse |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($packageDirectory.Length + 1).Replace("\", "/")
            $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$hash  $relative"
        }
    $packageChecksums | Set-Content -LiteralPath (Join-Path $packageDirectory "SHA256SUMS.txt") -Encoding ASCII

    Compress-Archive -LiteralPath $packageDirectory -DestinationPath $zipPath -CompressionLevel Optimal
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$zipHash  $packageName.zip" | Set-Content -LiteralPath $releaseChecksums -Encoding ASCII
    [pscustomobject][ordered]@{
        status = "built"
        version = $version
        python = (& $Python --version 2>&1 | Out-String).Trim()
        pyinstaller = $pyinstallerVersion
        executable = Join-Path $packageDirectory "codex-watchdog.exe"
        executable_bytes = (Get-Item -LiteralPath (Join-Path $packageDirectory "codex-watchdog.exe")).Length
        executable_sha256 = (Get-FileHash -LiteralPath (Join-Path $packageDirectory "codex-watchdog.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
        package_directory = $packageDirectory
        zip = $zipPath
        zip_bytes = (Get-Item -LiteralPath $zipPath).Length
        zip_sha256 = $zipHash
        release_checksums = $releaseChecksums
    } | ConvertTo-Json -Compress
} finally {
    Pop-Location
}
