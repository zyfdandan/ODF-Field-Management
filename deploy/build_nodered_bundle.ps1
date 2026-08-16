# Build node-red-local.tar.gz for offline server deploy (Windows, requires Node.js/npm).
param(
    [switch]$SkipIfExists
)

$ErrorActionPreference = "Stop"
$Deploy = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Deploy
$OutDir = Join-Path $Root "field_portal"
$BundleDir = Join-Path $OutDir "node-red-local"
$Tar = Join-Path $OutDir "node-red-local.tar.gz"

if ($SkipIfExists -and (Test-Path $Tar)) {
    Write-Host "Skip: $Tar already exists"
    exit 0
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Error "npm not found. Install Node.js LTS from https://nodejs.org/"
}

Write-Host "==> Build Node-RED bundle"
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Path $BundleDir | Out-Null
Copy-Item (Join-Path $Deploy "package.json") (Join-Path $BundleDir "package.json")
Push-Location $BundleDir
try {
    npm install --omit=dev --no-audit --no-fund
} finally {
    Pop-Location
}

$NrBin = Join-Path $BundleDir "node_modules\.bin\node-red.cmd"
if (-not (Test-Path $NrBin)) {
    Write-Error "node-red binary not found after npm install"
}

Write-Host "==> Pack $Tar"
if (Test-Path $Tar) { Remove-Item -Force $Tar }
Push-Location $OutDir
try {
    tar -czf "node-red-local.tar.gz" node-red-local
} finally {
    Pop-Location
}

$Mb = [math]::Round((Get-Item $Tar).Length / 1MB, 1)
Write-Host "Done: $Tar ($Mb MB)"
