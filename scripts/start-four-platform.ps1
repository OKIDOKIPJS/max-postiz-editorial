[CmdletBinding()]
param(
    [switch]$Worker,
    [switch]$ConfigurationReviewed,
    [ValidateRange(2, 60)][int]$Interval = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$previousLocation = Get-Location
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')

if ($Worker -and -not $ConfigurationReviewed) {
    throw 'FOUR_PLATFORM_CONFIGURATION_REVIEW_REQUIRED: add -ConfigurationReviewed only after reviewing this copy.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'FOUR_PLATFORM_LOCAL_VENV_REQUIRED'
}
if ((Get-Item -LiteralPath (Join-Path $projectRoot '.venv') -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'FOUR_PLATFORM_LOCAL_VENV_REQUIRED'
}

try {
    Set-Location -LiteralPath $projectRoot
    # Импортируем только src этой копии. Значения ключей и DSN не выводим.
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $statusOutput = & $python -m max_postiz_agent.routes_cli --root $projectRoot status
    $statusExit = $LASTEXITCODE
    try {
        $status = ($statusOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'FOUR_PLATFORM_STATUS_RESPONSE_INVALID'
    }
    if ($statusExit -ne 0) {
        $safeCode = 'ISOLATED_ROUTE_OPERATION_FAILED'
        if ($null -ne $status.PSObject.Properties['code'] -and
                $status.code -is [string] -and $status.code -cmatch '^[A-Z][A-Z0-9_]{1,99}$') {
            $safeCode = $status.code
        }
        throw ('FOUR_PLATFORM_STATUS_FAILED: ' + $safeCode)
    }
    if ($null -eq $status.PSObject.Properties['targets']) {
        throw 'FOUR_PLATFORM_STATUS_RESPONSE_INVALID'
    }
    # routes_cli сначала проверяет marker, пути, собственные БД/Postiz и запрет
    # MAX_EXISTING_BROWSER_PROFILE. status не делает пробу моделей или площадок.
    @($status.targets) | Select-Object id, platform, route, enabled, paused | Format-Table -AutoSize
    Write-Output 'Configuration status only; account access and delivery were not tested.'
    if (-not $Worker) {
        Write-Output 'Worker not started. No target was enabled, resumed or approved.'
        return
    }
    # Повторный вход через routes_cli снова выполняет ограждение перед worker.
    # Сценарий не меняет конфигурацию, паузу, согласования или исход unknown.
    Write-Output 'Starting the isolated approved-dispatch worker in this console. Stop with Ctrl+C.'
    & $python -m max_postiz_agent.routes_cli --root $projectRoot worker --interval $Interval
    if ($LASTEXITCODE -ne 0) {
        throw 'FOUR_PLATFORM_WORKER_STOPPED'
    }
}
finally {
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
    Set-Location -LiteralPath $previousLocation.Path
}
