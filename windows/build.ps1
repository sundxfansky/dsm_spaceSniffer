# Builds dist\DSMStorageInspector\DSMStorageInspector.exe with PyInstaller.
# Usage (from the repository root):  powershell -ExecutionPolicy Bypass -File windows\build.ps1
param(
    [string]$Python = "python",
    [string]$Index = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Venv = Join-Path $Root ".buildvenv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    & $Python -m venv $Venv
}
$Py = Join-Path $Venv "Scripts\python.exe"
$PipArgs = @("-m", "pip", "install", "-q", "-r", "requirements.txt", "-r", "windows\requirements-build.txt")
if ($Index) { $PipArgs += @("-i", $Index) }
& $Py @PipArgs
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $Py -m PyInstaller --noconfirm --clean windows\DSMStorageInspector.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Zip = Join-Path $Root "dist\DSMStorageInspector-windows.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path (Join-Path $Root "dist\DSMStorageInspector") -DestinationPath $Zip
Write-Host "Built: dist\DSMStorageInspector\DSMStorageInspector.exe"
Write-Host "Zip:   $Zip"
