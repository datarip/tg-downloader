<#
.SYNOPSIS
    Build the TDM Windows installer from the PyInstaller output.

.DESCRIPTION
    Runs Inno Setup over packaging\windows\tdm.iss and prints the SHA-256 of the
    finished setup executable. Expects packaging\build.py to have produced
    dist\TDM first. Inno Setup is preinstalled on GitHub's Windows runner
    images, so the normal path needs nothing extra. If the image ever loses it,
    this installs it with Chocolatey and fails only when that does not work.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# $PSScriptRoot is packaging\windows; the repository root is two levels up.
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Release = Join-Path $Repo "dist\release"
$AppTree = Join-Path $Repo "dist\TDM"

if (-not (Test-Path $AppTree)) {
    throw "dist\TDM is missing; run packaging/build.py first"
}

New-Item -ItemType Directory -Force -Path $Release | Out-Null

function Find-Iscc {
    $known = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($path in $known) {
        if (Test-Path $path) { return $path }
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

$iscc = Find-Iscc
if (-not $iscc) {
    # The image ships Inno Setup, so this is the rare path: it runs when a
    # runner image drops the compiler, and the build would otherwise stop with
    # no way forward. Chocolatey is preinstalled on the image too.
    Write-Host "ISCC.exe not found; installing Inno Setup with Chocolatey"
    choco install innosetup -y --no-progress
    if ($LASTEXITCODE -ne 0) {
        Write-Host "choco exited $LASTEXITCODE; checking for ISCC.exe anyway"
    }
    $iscc = Find-Iscc
}

if (-not $iscc) {
    throw @"
ISCC.exe was not found, and Chocolatey did not provide it.

Looked in:
  C:\Program Files (x86)\Inno Setup 6\ISCC.exe
  C:\Program Files\Inno Setup 6\ISCC.exe
  and on PATH.

If the runner image no longer ships Inno Setup, add an install step to
.github/workflows/build.yml that pins a specific installer version and
verifies its SHA-256, the way packaging/linux/make_appimage.sh pins
appimagetool.
"@
}

$isccVersion = (Get-Item $iscc).VersionInfo.ProductVersion
Write-Host "Using $iscc (version $isccVersion)"
$env:TDM_VERSION = $Version
Write-Host "TDM_VERSION=$($env:TDM_VERSION)"
& $iscc (Join-Path $PSScriptRoot "tdm.iss")
if ($LASTEXITCODE -ne 0) {
    throw "ISCC.exe failed with exit code $LASTEXITCODE"
}

$setup = Get-ChildItem -Path $Release -Filter "*Setup.exe" |
    Sort-Object LastWriteTime |
    Select-Object -Last 1
if (-not $setup) {
    throw "no Setup.exe in $Release"
}

$hash = Get-FileHash -Algorithm SHA256 -Path $setup.FullName
Write-Host ("{0}  {1}" -f $hash.Hash.ToLower(), $setup.Name)
