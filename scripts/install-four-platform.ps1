[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [string]$Python = 'py',
    [switch]$Browser,
    [switch]$Check
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent)).TrimEnd('\', '/')
$sourcePath = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\', '/')
if (-not (Test-Path -LiteralPath $sourcePath -PathType Container) -or
    $projectRoot -eq $sourcePath -or $projectRoot.StartsWith($sourcePath + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $sourcePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'ISOLATED_COPY_REQUIRED'
}
foreach ($relative in @('data', 'config', '.env.local', '.venv', 'data\four-platform', 'data\four-platform\isolation.json')) {
    $path = Join-Path $projectRoot $relative
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'ISOLATED_STATE_LINK_FORBIDDEN'
    }
}
$marker = Join-Path $projectRoot 'data\four-platform\isolation.json'
$app = Join-Path $projectRoot 'config\app.json'
if ((Test-Path -LiteralPath $app) -and -not (Test-Path -LiteralPath $marker)) {
    throw 'ISOLATED_EXISTING_CONFIGURATION_FORBIDDEN'
}
if (Test-Path -LiteralPath $marker) {
    $saved = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($saved.contract -ne 'four-platform-copy-v1' -or $saved.production_access -ne $false -or
        [IO.Path]::GetFullPath($saved.root).TrimEnd('\', '/') -ne $projectRoot -or
        [IO.Path]::GetFullPath($saved.source_root).TrimEnd('\', '/') -ne $sourcePath) {
        throw 'ISOLATED_COPY_REQUIRED'
    }
}
if ($Check) {
    Write-Output 'FOUR_PLATFORM_INSTALL_CHECK_OK: isolated paths checked; no install, initialization or services performed'
    return
}

$previousLocation = Get-Location
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
try {
    Set-Location -LiteralPath $projectRoot
    $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
        if ($Python -eq 'py') { & py -3.12 -m venv .venv } else { & $Python -m venv .venv }
        if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_VENV_FAILED' }
    }
    & $localPython -c 'import sys; assert sys.version_info >= (3,12), "PYTHON_312_REQUIRED"'
    if ($LASTEXITCODE -ne 0) { throw 'PYTHON_312_REQUIRED' }
    & $localPython -m pip --isolated install --index-url https://pypi.org/simple --no-input -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_DEPENDENCIES_FAILED' }
    & $localPython -m pip --isolated install --index-url https://pypi.org/simple --no-input --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_PACKAGE_FAILED' }
    if ($Browser) {
        & $localPython -m pip --isolated install --index-url https://pypi.org/simple --no-input -r requirements-browser.lock
        if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_BROWSER_DEPENDENCIES_FAILED' }
    }
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    if (-not (Test-Path -LiteralPath $app)) {
        & $localPython -m max_postiz_agent.routes_cli --root $projectRoot init --source-root $sourcePath
        if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_INITIALIZATION_FAILED' }
    }
    & $localPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_DEPENDENCY_CHECK_FAILED' }
    & $localPython -m max_postiz_agent.routes_cli --root $projectRoot status
    if ($LASTEXITCODE -ne 0) { throw 'FOUR_PLATFORM_STATUS_FAILED' }
    Write-Output 'Installed without enabling routes, approving drafts or starting services. Browser binaries and account login remain separate operator steps.'
} finally {
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
    Set-Location -LiteralPath $previousLocation
}
