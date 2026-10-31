[CmdletBinding()]
param(
    [switch]$Full,
    [switch]$Ui,
    [string]$EditorialTooling = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$previousLocation = Get-Location
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'FOUR_PLATFORM_LOCAL_VENV_REQUIRED'
}
if ((Get-Item -LiteralPath (Join-Path $projectRoot '.venv') -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'FOUR_PLATFORM_LOCAL_VENV_REQUIRED'
}
if ($Ui -and -not $EditorialTooling) {
    $EditorialTooling = [Environment]::GetEnvironmentVariable('EDITORIAL_TOOLING', 'Process')
    if (-not $EditorialTooling) {
        $EditorialTooling = Join-Path $projectRoot 'data\postiz-editorial-tooling\node_modules'
    }
}

# Python получает путь, а не значения секретов в аргументах командной строки.
# dotenv не импортируется целиком: берём только TEST_DATABASE_URL этой копии.
$runner = @'
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from max_postiz_agent.config import AgentError
from max_postiz_agent.isolated_workspace import assert_isolated


def main():
    root = Path(sys.argv[1]).resolve()
    full, ui, tooling = sys.argv[2] == "1", sys.argv[3] == "1", sys.argv[4]
    assert_isolated(root, environment=False)
    # Только окружение дочернего процесса; родительские значения не меняются.
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL",
                 "MAX_EXISTING_BROWSER_PROFILE", "OPENCODE_BASE_URL",
                 "OPENCODE_AUTH_SECRET", "TELEGRAM_BOT_TOKEN", "VK_ACCESS_TOKEN", "MAX_BOT_TOKEN"):
        os.environ.pop(name, None)
    # Проверяем config и её собственные пути до чтения тестового dotenv.
    assert_isolated(root)
    configuration = json.loads((root / "config/app.json").read_text(encoding="utf-8-sig"))
    publishing = json.loads((root / "config/publishing.json").read_text(encoding="utf-8-sig"))

    def clear_configured_environment(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_env") and isinstance(item, str) and item != "TEST_DATABASE_URL":
                    os.environ.pop(item, None)
                else:
                    clear_configured_environment(item)
        elif isinstance(value, list):
            for item in value:
                clear_configured_environment(item)

    clear_configured_environment(configuration)
    clear_configured_environment(publishing)
    own_env = root / ".env.local"
    values = []
    if own_env.exists():
        for line in own_env.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() == "TEST_DATABASE_URL":
                    values.append(value.strip())
    if len(values) > 1:
        raise AgentError("FOUR_PLATFORM_TEST_DATABASE_AMBIGUOUS")
    test_url = values[0] if values else ""
    if test_url:
        parsed = urlsplit(test_url)
        if (parsed.scheme not in {"postgresql", "postgresql+psycopg"}
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.port != 55448 or parsed.path != "/editorial_hub_tests"
                or parsed.query or parsed.fragment):
            raise AgentError("FOUR_PLATFORM_TEST_DATABASE_REQUIRED")
        os.environ["TEST_DATABASE_URL"] = test_url
    elif full:
        raise AgentError("FOUR_PLATFORM_TEST_DATABASE_REQUIRED")
    assert_isolated(root)
    os.environ["PYTHONPATH"] = str(root / "src")
    os.environ["PYTHONUTF8"] = "1"
    if ui:
        tools = Path(tooling).resolve()
        if (not (tools / "typescript/package.json").is_file()
                or not (tools / "react/package.json").is_file() or not shutil.which("node")):
            raise AgentError("FOUR_PLATFORM_EDITORIAL_TOOLING_REQUIRED")
        os.environ["EDITORIAL_TOOLING"] = str(tools)
    # pytest удаляет старый basetemp: заранее запрещаем ссылки/выход из копии.
    for relative in (".test-state", ".test-state/four-platform-pytest",
                     ".test-state/four-platform-tests.xml"):
        candidate = root / relative
        if (candidate.is_symlink() or candidate.is_junction()
                or not candidate.resolve().is_relative_to(root)):
            raise AgentError("FOUR_PLATFORM_TEST_PATH_UNSAFE")
    (root / ".test-state").mkdir(exist_ok=True)

    def run(arguments, name):
        # Проверки выполняются в копии; никакого install, doctor, inspect или worker.
        result = subprocess.run(arguments, cwd=root, check=False)
        if result.returncode:
            raise AgentError("FOUR_PLATFORM_" + name + "_FAILED")

    run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"], "RUFF")
    tests = [] if full else ["tests/test_telegram_publisher.py", "tests/test_vk_publisher.py",
        "tests/test_pikabu_delivery.py", "tests/test_pikabu_offline_probe.py",
        "tests/test_pikabu_browser_receipts.py",
        "tests/test_pikabu_editor_contract.py",
        "tests/test_four_platform_hub.py", "tests/test_route_isolation.py", "tests/test_routes_api.py",
        "tests/test_owner_original.py", "tests/test_route_credentials.py",
        "tests/test_route_dispatch_postgres.py", "tests/test_route_evidence_kind.py",
        "tests/test_routes_cli_schedule.py"]
    if not full:
        tests.extend(path.relative_to(root).as_posix()
                     for path in sorted((root / "tests").glob("test_vk_browser*.py")))
    run([sys.executable, "-m", "pytest", *tests, "--basetemp=.test-state/four-platform-pytest",
         "--junitxml=.test-state/four-platform-tests.xml"], "PYTEST")
    if ui:
        run([shutil.which("node"), "--test", "deployment/postiz-max/test-editorial-contract.cjs"], "UI")
    run([sys.executable, "-m", "pip", "check"], "DEPENDENCIES")
    print("Four-platform checks passed; external delivery was not tested.")
    if not ui:
        print("Postiz UI checks not requested; add -Ui with installed read-only TypeScript/React tooling.")
    return 0


try:
    raise SystemExit(main())
except AgentError as exc:
    print(json.dumps({"code": exc.code}))
    raise SystemExit(1) from None
except Exception:
    # Не выводим exception/traceback: они могут содержать пароль внутри DSN.
    print(json.dumps({"code": "FOUR_PLATFORM_CHECK_SETUP_FAILED"}))
    raise SystemExit(1) from None
'@

try {
    Set-Location -LiteralPath $projectRoot
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $fullFlag = if ($Full) { '1' } else { '0' }
    $uiFlag = if ($Ui) { '1' } else { '0' }
    $toolingArgument = if ($EditorialTooling) { $EditorialTooling } else { '-' }
    $runner | & $python - $projectRoot $fullFlag $uiFlag $toolingArgument
    if ($LASTEXITCODE -ne 0) {
        throw 'FOUR_PLATFORM_CHECKS_FAILED'
    }
}
finally {
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
    Set-Location -LiteralPath $previousLocation.Path
}
