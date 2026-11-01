"""Единая локальная проверка без запуска редакции и чтения runtime-секретов."""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = (
    "requirements.lock",
    "requirements-dev.lock",
    "requirements-browser.lock",
    "requirements-deployment.lock",
)


def interpreter(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def configuration(root: Path) -> dict:
    # Импорт после bootstrap: системный Python может быть старше Python в .venv.
    import tomllib

    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def test_environment(environ: dict[str, str], *, postgres: bool) -> dict[str, str]:
    """URL тестовой БД передаётся только явным флагом; .env не читается."""
    env = dict(environ)
    for key in ("DATABASE_URL", "PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    if not postgres:
        for key in (
            "TEST_DATABASE_URL",
            "MONITOR_OWNED_PG_TEST_URL",
            "MAX_POSTIZ_PG_TEST_CONTRACT",
        ):
            env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def contracts(root: Path) -> dict:
    """Проверяем checkout и locks, не импортируя приложение/его конфигурацию."""
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    project = configuration(root)
    if not SpecifierSet(project["project"]["requires-python"]).contains(
        ".".join(map(str, sys.version_info[:3]))
    ):
        raise ValueError("Python does not satisfy pyproject.toml")
    tree = ast.parse((root / "src/max_postiz_agent/__init__.py").read_text(encoding="utf-8"))
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    version = project["project"]["version"]
    if versions != [version] or importlib.metadata.version("max-postiz-agent") != version:
        raise ValueError("Package versions differ; reinstall with pip install --no-deps -e .")
    spec = importlib.util.find_spec("max_postiz_agent")
    if (
        not spec
        or not spec.origin
        or Path(spec.origin).resolve().parent != root / "src/max_postiz_agent"
    ):
        raise ValueError("Installed package points to another checkout")
    checked = set()
    for name in LOCKS:
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            if not re.fullmatch(r"==[^,*]+", str(requirement.specifier)):
                raise ValueError(f"Unpinned dependency in {name}")
            installed = importlib.metadata.version(requirement.name)
            if installed not in requirement.specifier:
                raise ValueError(f"Lock mismatch: {requirement.name} ({name})")
            checked.add(requirement.name.lower())
    if not any((root / "tests").rglob("test_*.py")):
        raise ValueError("No tests found in tests/")
    paths = project.get("tool", {}).get("max-postiz", {}).get("check", {}).get("format-paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(
            not isinstance(name, str)
            or not (root / name).resolve().is_relative_to(root)
            or not (root / name).is_file()
            for name in paths
        )
    ):
        raise ValueError("Missing or invalid tool.max-postiz.check.format-paths")
    for tool in ("git", "node"):
        if not shutil.which(tool):
            raise ValueError(f"Required tool missing: {tool}")
    return {"package_version": version, "locked_dependencies": len(checked)}


def postgres_ready(url: str | None) -> None:
    """Допускаем только явно указанную отдельную тестовую БД; URL не печатаем."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    if not url:
        raise ValueError("--postgres requires TEST_DATABASE_URL (a dedicated test database)")
    try:
        parsed = make_url(url)
    except Exception:
        raise ValueError("Invalid TEST_DATABASE_URL; value withheld") from None
    if (
        parsed.drivername != "postgresql+psycopg"
        or not re.fullmatch(r"(?:test_[a-z0-9_]+|[a-z0-9_]+_tests)", parsed.database or "")
        or parsed.query
    ):
        raise ValueError("Use postgresql+psycopg, a test_*/*_tests database and no URL query")
    engine = create_engine(parsed, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        raise ValueError("Test PostgreSQL unavailable; connection details withheld") from None
    finally:
        engine.dispose()


def junit_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()  # noqa: S314 -- наш локальный отчёт pytest
    cases = list(root.iter("testcase"))
    failed = [case for case in cases if case.find("failure") is not None]
    errors = [case for case in cases if case.find("error") is not None]
    skipped = [case for case in cases if case.find("skipped") is not None]
    return {
        "total": len(cases),
        "passed": len(cases) - len(failed) - len(errors) - len(skipped),
        "failed": len(failed),
        "errors": len(errors),
        "skipped": len(skipped),
        "skip_reasons": sorted({case.find("skipped").get("message", "") for case in skipped}),
        "failed_tests": [
            case.get("classname", "") + "::" + case.get("name", "") for case in failed + errors
        ],
    }


def owned_postgres_summary(path: Path) -> dict:
    """Явный новый PG-контракт не может стать зелёным через пропуски его тестов."""
    cases = list(ET.parse(path).getroot().iter("testcase"))  # noqa: S314 -- локальный pytest
    groups = {}
    for name, minimum, required_test in (
        ("test_route_dispatch_postgres", 1, None),
        (
            "test_route_recovery",
            1,
            "test_postgres_recovery_skips_locked_sender_then_reconciles_during_pause",
        ),
        ("test_monitor_configuration_postgres", 9, None),
    ):
        selected = [
            case
            for case in cases
            if case.get("classname", "").split(".")[-1] == name
            and (required_test is None or case.get("name") == required_test)
        ]
        passed = sum(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for case in selected
        )
        if len(selected) < minimum or passed != len(selected):
            raise ValueError(
                "Required isolated route/monitor PostgreSQL cases missing or not passed"
            )
        groups[name] = {"total": len(selected), "passed": passed}
    return groups


class Checks:
    def __init__(self, root: Path, directory: Path, env: dict[str, str]):
        self.root, self.directory, self.env = root, directory, env
        self.results: list[dict] = []

    def stage(self, name: str, command: list[str], *, timeout: int = 3600) -> dict:
        log = self.directory / (name + ".log")
        print(f"[RUN] {name}", flush=True)
        started = time.monotonic()
        result = {
            "name": name,
            "log": log.relative_to(self.root).as_posix(),
            "timeout_seconds": timeout,
        }
        try:
            with log.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(
                    command,
                    cwd=self.root,
                    env=self.env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
            result.update(
                status="passed" if completed.returncode == 0 else "failed",
                exit_code=completed.returncode,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.update(status="failed", exit_code=1, error=type(exc).__name__)
        result["seconds"] = round(time.monotonic() - started, 3)
        self.results.append(result)
        print(
            f"[{result['status'].upper()}] {name} ({result['seconds']}s) — {result['log']}",
            flush=True,
        )
        return result


def run(root: Path, *, postgres: bool) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / "data/check-runs" / (stamp + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    env = test_environment(dict(os.environ), postgres=postgres)
    report = {
        "schema_version": 1,
        "root": str(root),
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "postgres_requested": postgres,
        "started_at_utc": stamp,
        "live_delivery_checked": False,
    }
    checks = Checks(root, directory, env)
    python = [sys.executable, "-X", "utf8", "-B"]
    try:
        report["environment"] = contracts(root)
        if postgres:
            postgres_ready(env.get("TEST_DATABASE_URL"))
        print("[PASSED] environment (locks, package, tests, Git, Node)", flush=True)
    except (ValueError, OSError, ImportError, importlib.metadata.PackageNotFoundError) as exc:
        # Сообщения выше контролируются нами; значения окружения не включаем.
        report.update(status="failed", environment_error=str(exc), stages=[])
        (directory / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[FAILED] environment: {exc}\nReport: {directory / 'report.json'}", flush=True)
        return 1
    checks.stage("dependencies", python + ["-m", "pip", "check"])
    checks.stage("lint", python + ["-m", "ruff", "check", "src", "tests", "scripts"])
    paths = configuration(root)["tool"]["max-postiz"]["check"]["format-paths"]
    checks.stage("format", python + ["-m", "ruff", "format", "--check", *paths])
    for asset in ("static/dashboard.js", "hub_static/app.js"):
        checks.stage(
            "javascript-" + Path(asset).stem,
            [shutil.which("node"), "--check", "src/max_postiz_agent/" + asset],
        )
    owned_preflight = None
    if postgres and env.get("MAX_POSTIZ_PG_TEST_CONTRACT"):
        owned_preflight = checks.stage(
            "owned-postgres-preflight", python + ["scripts/check_ci_postgres.py"], timeout=60
        )
    browser = checks.stage(
        "browser",
        python
        + [
            "-c",
            (
                "from playwright.sync_api import sync_playwright; "
                "p=sync_playwright().start(); "
                "b=p.chromium.launch(channel='chrome', headless=True, chromium_sandbox=True); "
                "b.close(); p.stop(); print('Isolated Chrome ready; no external page opened')"
            ),
        ],
        timeout=60,
    )
    xml = directory / "tests.xml"
    if browser["status"] == "passed" and (
        owned_preflight is None or owned_preflight["status"] == "passed"
    ):
        temporary = directory / "pytest"
        tests = checks.stage(
            "tests",
            python
            + [
                "-m",
                "pytest",
                "-q",
                "--basetemp=" + str(temporary),
                "--junitxml=" + str(xml),
                "-o",
                "faulthandler_timeout=120",
            ],
            timeout=7200,
        )
        if xml.is_file():
            try:
                summary = report["tests"] = junit_summary(xml)
                if postgres and env.get("MAX_POSTIZ_PG_TEST_CONTRACT"):
                    report["owned_postgres_tests"] = owned_postgres_summary(xml)
                if (
                    summary["failed"]
                    or summary["errors"]
                    or summary["passed"] == 0
                    or (
                        postgres
                        and any("TEST_DATABASE_URL" in reason for reason in summary["skip_reasons"])
                    )
                ):
                    tests.update(
                        status="failed",
                        exit_code=1,
                        error="Failed/empty test suite or PostgreSQL tests skipped",
                    )
                print("Tests: " + json.dumps(summary, ensure_ascii=False), flush=True)
            except (ET.ParseError, OSError, ValueError):
                tests.update(
                    status="failed",
                    exit_code=1,
                    error="Invalid pytest report or incomplete isolated PostgreSQL coverage",
                )
        elif tests["status"] == "passed":
            tests.update(status="failed", exit_code=1, error="Missing pytest report")
        if tests.get("error"):
            print("[FAILED] tests validation: " + tests["error"], flush=True)
    else:
        checks.results.append(
            {
                "name": "tests",
                "status": "blocked",
                "error": "Chrome or owned PostgreSQL preflight failed",
            }
        )
    checks.stage("diff", [shutil.which("git"), "diff", "--check"])
    report.update(
        status="passed" if all(s["status"] == "passed" for s in checks.results) else "failed",
        stages=checks.results,
    )
    report["limitations"] = [
        "Type checking is not configured; formatter scope is explicit in pyproject.toml",
        "MAX/Postiz/model/LanguageTool live services are not exercised",
    ]
    if not postgres:
        report["limitations"].append(
            "PostgreSQL tests are skipped; run with --postgres for database acceptance"
        )
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{report['status'].upper()}] Report: {path}", flush=True)
    return 0 if report["status"] == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="Require dedicated TEST_DATABASE_URL and PostgreSQL tests",
    )
    args = parser.parse_args(argv)
    target = interpreter(ROOT)
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        if not target.is_file():
            print(
                "[FAILED] Missing .venv; create Python 3.12 venv and install the documented lock files"
            )
            return 1
        return subprocess.run(
            [
                str(target),
                "-X",
                "utf8",
                "-B",
                str(Path(__file__).resolve()),
                *(argv if argv is not None else sys.argv[1:]),
            ],
            cwd=ROOT,
            check=False,
        ).returncode
    return run(ROOT, postgres=args.postgres)


if __name__ == "__main__":
    raise SystemExit(main())
