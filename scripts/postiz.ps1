param([ValidateSet('start', 'stop', 'status', 'check')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (Test-Path -LiteralPath (Join-Path $projectRoot 'data\four-platform\isolation.json')) {
    throw 'ISOLATED_LEGACY_POSTIZ_FORBIDDEN: this command owns the legacy max-postiz project and port 4007'
}
$dockerPaths = @(
    "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe",
    "$env:LOCALAPPDATA\Docker\resources\bin\docker.exe",
    'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
)
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerBinary = if ($dockerCommand) { $dockerCommand.Source } else {
    $dockerPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $dockerBinary) { throw 'Docker CLI is not installed' }
$env:PATH = (Split-Path $dockerBinary -Parent) + [IO.Path]::PathSeparator + $env:PATH
$composePath = Join-Path $projectRoot 'deployment\postiz\compose.yaml'
$envPath = Join-Path $projectRoot 'deployment\postiz\.env'
if (-not (Test-Path -LiteralPath $envPath)) { throw 'Private deployment/postiz/.env is required' }
$composeArgs = @('compose', '--project-name', 'max-postiz', '--env-file', $envPath, '-f', $composePath)
$imageLock = Join-Path $projectRoot 'deployment\postiz\images.lock.yaml'
if (Test-Path -LiteralPath $imageLock) { $composeArgs += @('-f', $imageLock) }
$bridgeMarker = Join-Path $projectRoot 'data\postiz-max-bridge-enabled'
if (Test-Path -LiteralPath $bridgeMarker) {
    $composeArgs += @('-f', (Join-Path $projectRoot 'deployment\postiz-max\compose.bridge.yaml'))
}
switch ($Action) {
    'start' { & $dockerBinary @composeArgs up -d --wait --wait-timeout 600 }
    'stop' { & $dockerBinary @composeArgs stop }
    'status' { & $dockerBinary @composeArgs ps }
    'check' { & $dockerBinary @composeArgs config --quiet }
}
if ($LASTEXITCODE -ne 0) { throw "Docker compose $Action failed with exit $LASTEXITCODE" }
