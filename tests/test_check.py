"""Проверяем код возврата и изоляцию проверки, не запускаем полную suite рекурсивно."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "project_check", Path(__file__).resolve().parents[1] / "scripts/check.py"
)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / "src/max_postiz_agent").mkdir(parents=True)
    (tmp_path / "src/max_postiz_agent/__init__.py").write_text('__version__ = "1.2.3"')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("def test_sample(): pass\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check.py").touch()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion="1.2.3"\nrequires-python=">=3.12"\n'
        '[tool.max-postiz.check]\nformat-paths=["scripts/check.py"]\n'
    )
    for name in check.LOCKS:
        (tmp_path / name).write_text("sample==1.0\n")
    monkeypatch.setattr(
        check.importlib.metadata,
        "version",
        lambda name: "1.2.3" if name == "max-postiz-agent" else "1.0",
    )
    monkeypatch.setattr(
        check.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(tmp_path / "src/max_postiz_agent/__init__.py")),
    )
    monkeypatch.setattr(check.shutil, "which", lambda name: "/fake/" + name)
    return tmp_path


def test_local_mode_does_not_forward_runtime_or_test_database_or_pytest_overrides():
    original = {
        "PATH": "tools",
        "DATABASE_URL": "runtime-private",
        "TEST_DATABASE_URL": "test-private",
        "PYTHONPATH": "other-checkout",
        "PYTEST_ADDOPTS": "--collect-only",
        "PYTEST_PLUGINS": "injected",
    }
    result = check.test_environment(original, postgres=False)
    assert result["PATH"] == "tools" and result["PYTHONUTF8"] == "1"
    assert result["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert not set(original).difference({"PATH"}).intersection(result)
    assert original["DATABASE_URL"] == "runtime-private"


def test_postgres_mode_forwards_only_explicit_test_url():
    result = check.test_environment(
        {"DATABASE_URL": "runtime", "TEST_DATABASE_URL": "tests"}, postgres=True
    )
    assert result["TEST_DATABASE_URL"] == "tests" and "DATABASE_URL" not in result


def test_contracts_verify_current_checkout_and_all_locks(checkout):
    assert check.contracts(checkout) == {"package_version": "1.2.3", "locked_dependencies": 1}


@pytest.mark.parametrize(
    "damage", ["version", "lock", "no_tests", "wrong_checkout", "missing_tool"]
)
def test_contracts_fail_instead_of_silently_skipping_prerequisites(checkout, monkeypatch, damage):
    if damage == "version":
        (checkout / "src/max_postiz_agent/__init__.py").write_text('__version__ = "0.0.1"')
    elif damage == "lock":
        (checkout / check.LOCKS[0]).write_text("sample==2.0\n")
    elif damage == "no_tests":
        (checkout / "tests/test_sample.py").unlink()
    elif damage == "wrong_checkout":
        monkeypatch.setattr(
            check.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=__file__)
        )
    else:
        monkeypatch.setattr(check.shutil, "which", lambda name: None)
    with pytest.raises(ValueError):
        check.contracts(checkout)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "invalid",
        "sqlite:///private-secret",
        "postgresql+psycopg://user:private-secret@localhost/runtime",
        "postgresql+psycopg://user:private-secret@localhost/app_tests?options=-csearch_path=public",
    ],
)
def test_postgres_preflight_rejects_runtime_or_ambiguous_urls_without_exposing_them(url):
    with pytest.raises(ValueError) as failed:
        check.postgres_ready(url)
    assert "private-secret" not in str(failed.value)


def test_stage_turns_timeout_into_failed_result(checkout, monkeypatch):
    def timeout(*args, **kwargs):
        raise check.subprocess.TimeoutExpired("private-args", 1)

    monkeypatch.setattr(check.subprocess, "run", timeout)
    directory = checkout / "data/run"
    directory.mkdir(parents=True)
    checks = check.Checks(checkout, directory, {})
    result = checks.stage("timeout", ["tool"])
    assert result["status"] == "failed" and result["exit_code"] != 0
    assert result["error"] == "TimeoutExpired" and "private-args" not in json.dumps(result)


@pytest.mark.parametrize(
    "case, expected",
    [
        ('<testcase name="ok"/>', 0),
        ('<testcase name="bad"><failure>failure</failure></testcase>', 1),
        ("", 1),
    ],
)
def test_unified_check_exit_code_reflects_real_test_result(checkout, monkeypatch, case, expected):
    def execute(command, **kwargs):
        xml = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        if xml:
            Path(xml).write_text("<testsuites><testsuite>" + case + "</testsuite></testsuites>")
            return SimpleNamespace(returncode=1 if "failure" in case else 0)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == expected
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == ("passed" if expected == 0 else "failed")
    assert report["live_delivery_checked"] is False


@pytest.mark.parametrize(
    "suite_seconds, expected",
    [(3601, 0), (7201, 1)],
    ids=["long_complete_suite", "suite_exceeds_bounded_budget"],
)
def test_full_suite_budget_preserves_long_success_and_bounded_timeout(
    checkout, monkeypatch, suite_seconds, expected
):
    clock = SimpleNamespace(now=0.0)
    executed = {}
    monkeypatch.setattr(check.time, "monotonic", lambda: clock.now)

    def execute(command, **kwargs):
        stage = Path(kwargs["stdout"].name).stem
        executed[stage] = kwargs["timeout"]
        xml = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        duration = suite_seconds if "pytest" in command else 0.1
        clock.now += min(duration, kwargs["timeout"])
        if duration > kwargs["timeout"]:
            raise check.subprocess.TimeoutExpired(command, kwargs["timeout"])
        if xml:
            Path(xml).write_text(
                '<testsuites><testsuite><testcase name="virtual_complete_suite" '
                f'time="{suite_seconds}"/></testsuite></testsuites>'
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == expected
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == ("passed" if expected == 0 else "failed")
    assert list(executed) == [
        "dependencies",
        "lint",
        "format",
        "javascript-dashboard",
        "javascript-app",
        "browser",
        "tests",
        "diff",
    ]
    stages = {stage["name"]: stage for stage in report["stages"]}
    assert all(stage["timeout_seconds"] == executed[name] for name, stage in stages.items())
    assert executed["tests"] == 7200 and executed["browser"] == 60
    assert all(
        budget == 3600 for name, budget in executed.items() if name not in {"tests", "browser"}
    )
    assert all(stage["status"] == "passed" for name, stage in stages.items() if name != "tests")
    assert stages["tests"]["seconds"] == min(suite_seconds, 7200)
    if expected == 0:
        assert stages["tests"]["status"] == "passed" and report["tests"]["passed"] == 1
    else:
        assert stages["tests"]["status"] == "failed" and stages["tests"]["exit_code"] != 0
        assert stages["tests"]["error"] == "TimeoutExpired"
        assert "tests" not in report


def test_lint_failure_does_not_hide_other_checks(checkout, monkeypatch):
    executed = []

    def execute(command, **kwargs):
        executed.append(command)
        xml = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        if xml:
            Path(xml).write_text(
                '<testsuites><testsuite><testcase name="ok"/></testsuite></testsuites>'
            )
        failed = "ruff" in command and "check" in command
        return SimpleNamespace(returncode=int(failed))

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    assert any("pytest" in command for command in executed)
    assert any("diff" in command for command in executed)


def test_environment_failure_still_writes_report_and_launches_nothing(checkout, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No stage may run with an invalid environment")

    monkeypatch.setattr(check.subprocess, "run", forbidden)
    (checkout / "tests/test_sample.py").unlink()
    assert check.run(checkout, postgres=False) == 1
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == "failed" and "No tests" in report["environment_error"]


@pytest.mark.parametrize("fixture_name", ["pg_hub", "postgres_reserve"])
def test_postgres_fixtures_never_read_runtime_configuration(tmp_path, monkeypatch, fixture_name):
    import test_hourly_reserve_controller
    import test_hub_postgres

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    local = tmp_path / "data/reserve-20261026/test-db.local.env"
    local.parent.mkdir(parents=True)
    value = "TEST_DATABASE_URL=postgresql+psycopg://private@localhost/runtime\n"
    local.write_text(value)
    (tmp_path / ".env.local").write_text(value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit runtime database must never be contacted")

    if fixture_name == "pg_hub":
        module = test_hub_postgres
        monkeypatch.setattr(module, "ROOT", tmp_path, raising=False)
        args = [tmp_path]
    else:
        module = test_hourly_reserve_controller
        monkeypatch.setattr(module, "__file__", str(tmp_path / "tests/test_controller.py"))
        args = [SimpleNamespace(), monkeypatch]
    monkeypatch.setattr(module, "create_engine", forbidden)
    generator = getattr(module, fixture_name).__wrapped__(*args)
    with pytest.raises(pytest.skip.Exception, match="TEST_DATABASE_URL"):
        next(generator)


@pytest.mark.parametrize(
    "xml",
    [
        "broken XML",
        '<testsuites><testsuite><testcase name="ok"/><testcase name="bad"><error/></testcase></testsuite></testsuites>',
    ],
)
def test_invalid_junit_or_false_success_exit_code_cannot_make_report_green(
    checkout, monkeypatch, xml
):
    def execute(command, **kwargs):
        path = next(
            (arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None
        )
        if path:
            Path(path).write_text(xml)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert (
        next(stage for stage in report["stages"] if stage["name"] == "tests")["status"] == "failed"
    )


def test_launcher_uses_this_checkouts_venv_and_preserves_failure_code(checkout, monkeypatch):
    target = check.interpreter(checkout)
    target.parent.mkdir(parents=True)
    target.touch()
    monkeypatch.setattr(check, "ROOT", checkout)
    monkeypatch.setattr(check.sys, "prefix", str(checkout / "different-environment"))
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.main(["--postgres"]) == 7
    assert calls[0][0][0] == str(target) and calls[0][0][-1] == "--postgres"
    assert calls[0][1]["cwd"] == checkout


def test_browser_failure_blocks_tests_but_still_reports_other_stages(checkout, monkeypatch):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        browser = any("chromium.launch" in arg for arg in command)
        return SimpleNamespace(returncode=int(browser))

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.run(checkout, postgres=False) == 1
    assert not any("pytest" in command for command in calls)
    assert any("diff" in command for command in calls)
    report = json.loads(
        next((checkout / "data/check-runs").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert (
        next(stage for stage in report["stages"] if stage["name"] == "tests")["status"] == "blocked"
    )
