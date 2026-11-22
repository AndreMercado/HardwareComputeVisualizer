# Builds the HCV Hot Spots extension into dist/hcv-hotspots-<version>.vsix.
# The desktop app (main.py ...) and the profiler are copied in from the repo, so the .vsix carries both:
# the editor features and "HCV: Open the Hardware Visualizer".
#   powershell -ExecutionPolicy Bypass -File vscode-extension/build.ps1
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$repo = Split-Path $src -Parent
$old = Join-Path $src "python"
if (Test-Path $old) { Remove-Item $old -Recurse -Force }
$app = Join-Path $src "app"
if (Test-Path $app) { Remove-Item $app -Recurse -Force }
New-Item -ItemType Directory -Force $app | Out-Null
$files = "main.py", "telemetry.py", "nvgpu.py", "workloads.py", "control.py",
         "agent.py", "sandbox_run.py", "analysis.py", "profiler.py", "mcp_server.py", "setup_lhm.ps1", "requirements.txt"
foreach ($f in $files) { Copy-Item (Join-Path $repo $f) $app -Force }
New-Item -ItemType Directory -Force (Join-Path $src "dist") | Out-Null
$version = (Get-Content (Join-Path $src "package.json") -Raw | ConvertFrom-Json).version
Push-Location $src
try {
    npx --yes @vscode/vsce@3.2.1 package --out "dist/hcv-hotspots-$version.vsix"
    if ($LASTEXITCODE -ne 0) { throw "vsce package failed" }
} finally { Pop-Location }
Write-Host "Built dist/hcv-hotspots-$version.vsix"
