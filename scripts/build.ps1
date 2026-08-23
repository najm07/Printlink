# PrintLink release build: bundle the agent with PyInstaller, then build the
# Inno Setup installer. VERSION lives ONLY in agent/config.py; this script
# propagates it to the installer via /DMyAppVersion so there is no second
# place to keep in sync.
#
# Usage:  pwsh scripts/build.ps1
#         pwsh scripts/build.ps1 -SkipInstaller   # exe bundle only
#
# Prerequisites: .venv with requirements-dev.txt installed (PyInstaller comes
# from PyInstaller's own install — see README), Inno Setup 6 in the default
# location (override with -Iscc).
#Requires -Version 7
param(
    [string]$Python = ".venv\Scripts\python.exe",
    [string]$Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$config = Get-Content agent\config.py -Raw
if ($config -notmatch '(?m)^\s*VERSION\s*=\s*"([^"]+)"') {
    throw "Could not read VERSION from agent/config.py"
}
$version = $Matches[1]
Write-Host "== Building PrintLink $version =="

if (-not (Test-Path $Python)) { throw "python not found: $Python" }
& $Python -m PyInstaller --noconfirm PrintLinkAgent.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path dist\PrintLinkAgent.exe)) { throw "dist\PrintLinkAgent.exe missing after build" }

if ($SkipInstaller) {
    Write-Host "== Done: dist\PrintLinkAgent.exe =="
    return
}

if (-not (Test-Path $Iscc)) { throw "Inno Setup compiler not found: $Iscc" }
& $Iscc "/DMyAppVersion=$version" installer\printlink.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

Write-Host "== Done: installer\output\PrintLinkSetup-$version.exe =="
