# Installs the HCV Hot Spots extension into VS Code (copies this folder into
# ~/.vscode/extensions and remembers where profiler.py lives). Reload VS Code afterwards.
#   powershell -ExecutionPolicy Bypass -File vscode-extension/install.ps1
$src = $PSScriptRoot
$hcv = Split-Path $src -Parent
$dest = Join-Path $env:USERPROFILE ".vscode\extensions\hcv.hcv-hotspots-0.1.0"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory $dest | Out-Null
Copy-Item (Join-Path $src "package.json"), (Join-Path $src "extension.js"), (Join-Path $src "README.md") $dest
Set-Content (Join-Path $dest "hcv-path.txt") $hcv -NoNewline -Encoding UTF8
Write-Host "Installed to $dest (profiler: $hcv\profiler.py). Reload VS Code: Ctrl+Shift+P > Developer: Reload Window."
