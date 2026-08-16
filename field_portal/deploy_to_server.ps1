# Legacy wrapper — use deploy/deploy_to_server.ps1 (reads deploy.env, creates venv on server).
& (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "deploy\deploy_to_server.ps1") @args
