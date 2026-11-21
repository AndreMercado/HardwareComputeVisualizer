# Builds the extension (.vsix) and installs it into VS Code. Reload VS Code afterwards.
#   powershell -ExecutionPolicy Bypass -File vscode-extension/install.ps1
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
& (Join-Path $src "build.ps1")
$version = (Get-Content (Join-Path $src "package.json") -Raw | ConvertFrom-Json).version
# the old copy-folder install (0.1.0) would sit next to the packaged one
$old = Join-Path $env:USERPROFILE ".vscode\extensions\hcv.hcv-hotspots-0.1.0"
if (Test-Path $old) { Remove-Item $old -Recurse -Force }
code --install-extension (Join-Path $src "dist\hcv-hotspots-$version.vsix") --force
Write-Host "Installed. Reload VS Code: Ctrl+Shift+P > Developer: Reload Window."
