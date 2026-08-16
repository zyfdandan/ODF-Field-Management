# Deploy Field Portal to Linux server via PuTTY plink/pscp.
# Config: deploy/deploy.env (copy from deploy.env.example)
param(
    [string]$HostName = "",
    [string]$User = "",
    [string]$Password = "",
    [string]$RemoteDir = "",
    [string]$HostKey = "",
    [switch]$SkipNodeRed
)

$ErrorActionPreference = "Stop"
$Deploy = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Deploy
$EnvFile = Join-Path $Deploy "deploy.env"

function Read-EnvFile($path) {
    $vars = @{}
    if (-not (Test-Path $path)) { return $vars }
    Get-Content $path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $v = $Matches[2].Trim().Trim('"').Trim("'")
            $vars[$Matches[1]] = $v
        }
    }
    return $vars
}

$env = Read-EnvFile $EnvFile
$HostName = if ($HostName) { $HostName } else { $env["DEPLOY_HOST"] }
$User = if ($User) { $User } else { $env["DEPLOY_USER"] }
$Password = if ($Password) { $Password } else { $env["DEPLOY_PASSWORD"] }
$RemoteDir = if ($RemoteDir) { $RemoteDir } else { $env["DEPLOY_REMOTE_DIR"] }
$HostKey = if ($HostKey) { $HostKey } else { $env["DEPLOY_HOSTKEY"] }

if (-not $HostName -or -not $User) {
    Write-Error "Set DEPLOY_HOST and DEPLOY_USER in deploy.env or pass -HostName/-User"
}
if (-not $Password) {
    $sec = Read-Host "SSH password for ${User}@${HostName}" -AsSecureString
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if (-not $HostKey) {
    Write-Warning "DEPLOY_HOSTKEY not set — plink may prompt on first connect"
}

$Plink = (Get-Command plink -ErrorAction SilentlyContinue).Source
$Pscp = (Get-Command pscp -ErrorAction SilentlyContinue).Source
if (-not $Plink -or -not $Pscp) {
    Write-Error "Install PuTTY (plink/pscp) and add to PATH"
}

$plinkArgs = @("-batch")
$pscpArgs = @("-batch")
if ($HostKey) {
    $plinkArgs += @("-hostkey", $HostKey)
    $pscpArgs += @("-hostkey", $HostKey)
}
$plinkArgs += @("-pw", $Password, "${User}@${HostName}")
$pscpArgs += @("-pw", $Password)

Write-Host "==> Upload to ${User}@${HostName}:$RemoteDir"
& $Plink @plinkArgs "mkdir -p $RemoteDir/field_portal $RemoteDir/deploy"

$coreFiles = @(
    "field_api.py", "field_service.py", "field_browser.py", "field_cache.py", "field_warmup.py", "field_audit.py", "room_layout.py", "trunk_infra.py",
    "index.html", "form.html", "trunk_form.html", "audit.html", "nodered_flow.json", "portal_config.example.json", "portal_settings.py", "reset_test_lab.py"
)
$FpRoot = Join-Path $Root "field_portal"
foreach ($f in $coreFiles) {
    & $Pscp @pscpArgs "$FpRoot\$f" "${User}@${HostName}:$RemoteDir/field_portal/"
}

$installSh = Join-Path $Deploy "install_server.sh"
& $Pscp @pscpArgs $installSh "${User}@${HostName}:$RemoteDir/field_portal/install_server.sh"
& $Pscp @pscpArgs $installSh "${User}@${HostName}:$RemoteDir/deploy/install_server.sh"

& $Pscp @pscpArgs (Join-Path $Deploy "requirements.txt") "${User}@${HostName}:$RemoteDir/requirements.txt"
& $Pscp @pscpArgs (Join-Path $Deploy "install_from_package.sh") "${User}@${HostName}:$RemoteDir/deploy/"

foreach ($f in @("fiber_infra.py", "import_from_excel.py", "naming_rules.py", "unified_sheet.py", "bootstrap_from_sheet.py", "fix_odf_port_names.py", "netbox_config.example.json")) {
    $src = Join-Path $Root $f
    if (Test-Path $src) {
        & $Pscp @pscpArgs $src "${User}@${HostName}:$RemoteDir/"
    }
}
if (Test-Path (Join-Path $Root "netbox_config.json")) {
    & $Pscp @pscpArgs (Join-Path $Root "netbox_config.json") "${User}@${HostName}:$RemoteDir/"
}
if (Test-Path (Join-Path $FpRoot "portal_config.json")) {
    & $Pscp @pscpArgs (Join-Path $FpRoot "portal_config.json") "${User}@${HostName}:$RemoteDir/field_portal/"
}

& $Plink @plinkArgs "test -f $RemoteDir/field_portal/portal_config.json || cp $RemoteDir/field_portal/portal_config.example.json $RemoteDir/field_portal/portal_config.json"
& $Plink @plinkArgs "test -f $RemoteDir/netbox_config.json || (test -f $RemoteDir/netbox_config.example.json && cp $RemoteDir/netbox_config.example.json $RemoteDir/netbox_config.json || true)"

$Tar = Join-Path $FpRoot "node-red-local.tar.gz"
if (-not $SkipNodeRed -and $env["SKIP_NODERED"] -ne "1" -and (Test-Path $Tar)) {
    $mb = [math]::Round((Get-Item $Tar).Length / 1MB, 1)
    Write-Host "==> Upload Node-RED bundle ($mb MB)"
    & $Pscp @pscpArgs $Tar "${User}@${HostName}:$RemoteDir/"
    & $Plink @plinkArgs "cd $RemoteDir && rm -rf node-red-local && tar -xzf node-red-local.tar.gz && chmod +x node-red-local/node_modules/.bin/node-red 2>/dev/null || true"
} else {
    Write-Host "WARN: node-red-local.tar.gz not uploaded. Run deploy/build_nodered_bundle.ps1 first or use -SkipNodeRed"
}

Write-Host "==> Install & start services"
& $Plink @plinkArgs "sed -i 's/\r$//' $RemoteDir/deploy/install_server.sh $RemoteDir/field_portal/install_server.sh; chmod +x $RemoteDir/deploy/install_server.sh $RemoteDir/field_portal/install_server.sh; APP_DIR='$RemoteDir' SUDO_PASS='$Password' bash $RemoteDir/deploy/install_server.sh"

Write-Host "==> Done"
Write-Host "    Portal: http://${HostName}:1880/"
Write-Host "    Health: curl http://${HostName}:1880/odf/api/health  (via Node-RED proxy)"
