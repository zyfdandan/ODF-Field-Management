# Build offline release package: dist/netbox-field-portal-YYYYMMDD.zip
param(
    [switch]$IncludeNodeRed,
    [switch]$BuildNodeRed,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$Deploy = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Deploy
$Date = Get-Date -Format "yyyyMMdd"
$Name = "netbox-field-portal-$Date"
$DistRoot = if ($OutDir) { $OutDir } else { Join-Path $Root "dist" }
$Stage = Join-Path $DistRoot $Name
$Zip = Join-Path $DistRoot "$Name.zip"

if ($BuildNodeRed -or $IncludeNodeRed) {
    & (Join-Path $Deploy "build_nodered_bundle.ps1")
}

Write-Host "==> Stage release: $Stage"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path $Stage | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Stage "field_portal") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Stage "deploy") | Out-Null

# Root runtime modules
$RootFiles = @(
    "fiber_infra.py", "import_from_excel.py", "naming_rules.py", "unified_sheet.py",
    "bootstrap_from_sheet.py", "site_devices.py", "fix_odf_port_names.py",
    "netbox_config.example.json"
)
foreach ($f in $RootFiles) {
    $src = Join-Path $Root $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $Stage $f) }
}

Copy-Item (Join-Path $Deploy "requirements.txt") (Join-Path $Stage "requirements.txt")

# Field portal
$FpFiles = @(
    "field_api.py", "field_service.py", "field_browser.py", "field_cache.py",
    "field_warmup.py", "field_audit.py", "field_locks.py", "trunk_infra.py",
    "room_layout.py", "portal_settings.py", "reset_test_lab.py",
    "generate_odf_qr.py",
    "index.html", "form.html", "trunk_form.html", "audit.html",
    "nodered_flow.json", "portal_config.example.json",
    "install_server.sh", "README.md"
)
foreach ($f in $FpFiles) {
    $src = Join-Path $Root "field_portal\$f"
    if (Test-Path $src) { Copy-Item $src (Join-Path $Stage "field_portal\$f") }
}

# Deploy toolkit
$DeployFiles = @(
    "install_server.sh", "install_from_package.sh", "build_nodered_bundle.sh",
    "build_nodered_bundle.ps1", "build_package.ps1", "deploy_to_server.ps1",
    "deploy.env.example", "package.json", "requirements.txt", "requirements-dev.txt",
    "DEPLOYMENT.md", "AI-DEPLOY.md", "MANIFEST.txt",
    "netbox-field-api.service", "netbox-docker.service"
)
foreach ($f in $DeployFiles) {
    $src = Join-Path $Deploy $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $Stage "deploy\$f") }
}

# Sync install script into field_portal (deploy copy is canonical)
Copy-Item (Join-Path $Deploy "install_server.sh") (Join-Path $Stage "field_portal\install_server.sh") -Force

# Docs at package root
Copy-Item (Join-Path $Deploy "DEPLOYMENT.md") (Join-Path $Stage "README-DEPLOY.md") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Deploy "AI-DEPLOY.md") (Join-Path $Stage "AI-DEPLOY.md") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Root "README.md") (Join-Path $Stage "README-EXCEL-IMPORT.md") -ErrorAction SilentlyContinue

# Optional local restore (secrets + draft store). Installer does not auto-apply these.
$Restore = Join-Path $Stage "restore"
New-Item -ItemType Directory -Path $Restore | Out-Null
$RestoreNote = @"
把本目录文件拷到安装根目录后再启动服务：
  restore/netbox_config.json              ->  <APP_DIR>/netbox_config.json
  restore/portal_config.json              ->  <APP_DIR>/field_portal/portal_config.json
  restore/circuit_store.json              ->  <APP_DIR>/field_portal/circuit_store.json

含 NetBox Token，勿把 restore/ 发到公共位置。
"@
Set-Content -Path (Join-Path $Restore "README.txt") -Value $RestoreNote -Encoding UTF8
$Cfg = Join-Path $Root "netbox_config.json"
if (Test-Path $Cfg) { Copy-Item $Cfg (Join-Path $Restore "netbox_config.json") }
$Pcfg = Join-Path $Root "field_portal\portal_config.json"
if (Test-Path $Pcfg) { Copy-Item $Pcfg (Join-Path $Restore "portal_config.json") }
$Store = Join-Path $Root "field_portal\circuit_store.json"
if (Test-Path $Store) { Copy-Item $Store (Join-Path $Restore "circuit_store.json") }

# Optional Node-RED bundle
$Tar = Join-Path $Root "field_portal\node-red-local.tar.gz"
if ($IncludeNodeRed -and (Test-Path $Tar)) {
    Copy-Item $Tar (Join-Path $Stage "field_portal\node-red-local.tar.gz")
    Write-Host "    Included node-red-local.tar.gz"
} elseif ($IncludeNodeRed) {
    Write-Host "WARN: -IncludeNodeRed set but node-red-local.tar.gz not found. Run build_nodered_bundle.ps1 first."
}

# Version stamp
@{
    built_at = (Get-Date -Format "o")
    package = $Name
    includes_nodered = [bool](Test-Path (Join-Path $Stage "field_portal\node-red-local.tar.gz"))
    review = "occupancy-cache P1/P2 addressed 2026-08-14; no P0 remaining"
} | ConvertTo-Json | Set-Content (Join-Path $Stage "VERSION.json") -Encoding UTF8

Write-Host "==> Zip $Zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Compress-Archive -Path $Stage -DestinationPath $Zip -Force
$Mb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Host "Done: $Zip ($Mb MB)"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Copy netbox_config.json + field_portal/portal_config.json into $Stage (or configure on server)"
Write-Host "  2. Upload zip to server and: unzip + bash deploy/install_from_package.sh"
Write-Host "  Or from Windows: copy deploy.env.example -> deploy.env, then .\deploy\deploy_to_server.ps1"
