"""CI database setup is exact, read-only and cannot turn missing PG cases green."""

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import yaml

from pg_test_contract import CI_CONTRACT, CONTRACT_ENV, LOCAL_CONTRACT, endpoint_spec, verify_identity, IDENTITY_SQL


ROOT = Path(__file__).resolve().parents[1]


def helper():
    spec = importlib.util.spec_from_file_location("ci_postgres_checker", ROOT / "scripts/check_ci_postgres.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(ci=False):
    ports = (55448, 55449) if ci else (55450, 55451)
    result = {CONTRACT_ENV: CI_CONTRACT if ci else LOCAL_CONTRACT,
        "TEST_DATABASE_URL": f"postgresql+psycopg://qa:synthetic-private@127.0.0.1:{ports[0]}/editorial_hub_tests",
        "MONITOR_OWNED_PG_TEST_URL": f"postgresql+psycopg://qa:synthetic-private@127.0.0.1:{ports[1]}/editorial_hub_tests"}
    if ci:
        result.update(GITHUB_ACTIONS="true", RUNNER_OS="Linux", GITHUB_RUN_ID="123456")
    return result


def identity(spec, system_id):
    return [spec["database"], spec["user"], 170006, spec["cluster_name"], str(spec["data_directory"]), system_id, spec["server_port"]]


class Connection:
    def __init__(self, row):
        self.row, self.queries, self.closed = row, [], False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def exec_driver_sql(self, statement):
        assert statement == IDENTITY_SQL and statement.startswith("SELECT ")
        self.queries.append(statement)
        return SimpleNamespace(one=lambda: self.row)


@pytest.mark.parametrize("ci", [False, True])
def test_exact_contract_reads_distinct_identity_only_and_closes_engines(tmp_path, ci):
    env = environment(ci)
    rows = {env[key]: identity(endpoint_spec(role, env[key], tmp_path, environment=env), str(123456789012 + index))
            for index, (role, key) in enumerate((("routes", "TEST_DATABASE_URL"), ("monitor", "MONITOR_OWNED_PG_TEST_URL")))}
    engines = []

    def factory(url, **kwargs):
        assert kwargs == {"connect_args": {"connect_timeout": 5}, "pool_pre_ping": True}
        connection = Connection(rows[url])
        engine = SimpleNamespace(connection=connection, disposed=False)
        engine.connect = lambda: connection
        engine.dispose = lambda: setattr(engine, "disposed", True)
        engines.append(engine)
        return engine

    result = helper().check(tmp_path, environment=env, engine_factory=factory)
    assert result["instances"] == 2 and result["identities_distinct"] and result["read_only"]
    assert all(engine.disposed and engine.connection.closed and engine.connection.queries == [IDENTITY_SQL] for engine in engines)
    assert all(value not in json.dumps(result) for value in ("synthetic-private", "postgresql", "123456789012", str(tmp_path)))


@pytest.mark.parametrize("damage", ["missing", "unknown", "host", "port", "database", "query", "fragment", "scheme", "missing_monitor", "ci_context"])
def test_invalid_contract_never_connects_or_accepts_a_generic_fallback(tmp_path, damage):
    env = environment()
    if damage == "missing":
        del env[CONTRACT_ENV]
    elif damage == "unknown":
        env[CONTRACT_ENV] = "accept-any-postgres"
    elif damage == "missing_monitor":
        del env["MONITOR_OWNED_PG_TEST_URL"]
    elif damage == "ci_context":
        env = environment(True)
        env["GITHUB_ACTIONS"] = "false"
    else:
        changes = {"host": ("127.0.0.1", "10.20.30.45"), "port": ("55450", "55448"),
            "database": ("editorial_hub_tests", "editorial_hub"), "query": ("editorial_hub_tests", "editorial_hub_tests?options=unsafe"),
            "fragment": ("editorial_hub_tests", "editorial_hub_tests#unsafe"), "scheme": ("postgresql+psycopg", "postgresql")}
        env["TEST_DATABASE_URL"] = env["TEST_DATABASE_URL"].replace(*changes[damage])
    with pytest.raises(ValueError, match="PG_TEST_"):
        helper().check(tmp_path, environment=env, engine_factory=lambda *_a, **_k: pytest.fail("invalid contract connected"))


@pytest.mark.parametrize("field,value", [(0, "editorial_hub"), (1, "other"), (2, 180000), (3, "other-cluster"),
    (4, "different/data/cluster"), (5, "not-an-identifier"), (6, 5432)])
def test_wrong_live_identity_is_rejected_before_any_mutation(tmp_path, field, value):
    env = environment()
    spec = endpoint_spec("routes", env["TEST_DATABASE_URL"], tmp_path, environment=env)
    row = identity(spec, "123456789012")
    row[field] = value
    connection = Connection(row)
    with pytest.raises(ValueError, match="PG_TEST_SERVER_IDENTITY_INVALID"):
        verify_identity(connection, spec)
    assert connection.queries == [IDENTITY_SQL]


def test_same_cluster_cannot_masquerade_as_two_owned_instances(tmp_path):
    env = environment()

    def factory(url, **_):
        role = "routes" if url == env["TEST_DATABASE_URL"] else "monitor"
        connection = Connection(identity(endpoint_spec(role, url, tmp_path, environment=env), "123456789012"))
        return SimpleNamespace(connect=lambda: connection, dispose=lambda: None)

    with pytest.raises(ValueError, match="PG_TEST_INSTANCES_NOT_DISTINCT"):
        helper().check(tmp_path, environment=env, engine_factory=factory)


def test_local_cluster_links_are_rejected_before_connecting(tmp_path, monkeypatch):
    env = environment()
    peer_link = tmp_path / "data/postgres-qa-routes"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == peer_link)
    with pytest.raises(ValueError, match="PG_TEST_CLUSTER_LINK_FORBIDDEN"):
        helper().check(tmp_path, environment=env, engine_factory=lambda *_a, **_k: pytest.fail("peer cluster link connected"))


def test_workflow_matches_strict_fixture_contracts_and_runs_canonical_full_checks():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    job = jobs["postgres-tests"]
    assert job["name"] == "Canonical full checks (${{ matrix.mode }})"
    assert job["strategy"] == {"fail-fast": False, "matrix": {"mode": ["normal", "postgres"]}}
    assert jobs["required"]["needs"].count("postgres-tests") == 1
    assert job["env"][CONTRACT_ENV] == CI_CONTRACT
    for service, role, port in (("postgres", "routes", 55448), ("monitor-postgres", "monitor", 55449)):
        spec = endpoint_spec(role, job["env"]["TEST_DATABASE_URL" if role == "routes" else "MONITOR_OWNED_PG_TEST_URL"], ROOT,
                             environment={**job["env"], "GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux", "GITHUB_RUN_ID": "123456"})
        declared = job["services"][service]
        assert declared["ports"] == [f"127.0.0.1:{port}:5432"]
        assert declared["env"]["POSTGRES_DB"] == spec["database"]
        assert declared["env"]["POSTGRES_INITDB_ARGS"] == "--set=cluster_name=" + spec["cluster_name"]
    assert job["timeout-minutes"] == 45
    steps = job["steps"]
    named = {step["name"]: step for step in steps if "name" in step}
    locks = ["requirements.lock", "requirements-dev.lock", "requirements-browser.lock", "requirements-deployment.lock"]
    python = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    assert python["with"]["python-version"] == "3.12"
    assert python["with"]["cache-dependency-path"].splitlines() == locks
    node = next(step for step in steps if step.get("uses", "").startswith("actions/setup-node@"))
    assert node["with"]["node-version"] == "24"
    venv = named["Create checkout-owned virtual environment"]["run"]
    assert "python -m venv .venv" in venv
    assert 'echo "$GITHUB_WORKSPACE/.venv/bin" >> "$GITHUB_PATH"' in venv
    install = named["Install pinned dependencies"]["run"]
    tokens = shlex.split(install.splitlines()[0])
    assert [tokens[index + 1] for index, token in enumerate(tokens) if token == "-r"] == locks
    assert "python -m pip install --no-deps --no-build-isolation -e ." in install
    assert named["Install Chrome and browser dependencies"]["run"] == "python -m playwright install --with-deps chrome"
    assert named["Verify the two owned PostgreSQL identities without mutation"]["run"] == "python scripts/check_ci_postgres.py"
    assert named["Verify the two owned PostgreSQL identities without mutation"]["if"] == "matrix.mode == 'postgres'"
    assert named["Run canonical full checks without PostgreSQL"]["run"] == "python scripts/check.py"
    assert named["Run canonical full checks without PostgreSQL"]["if"] == "matrix.mode == 'normal'"
    assert named["Run canonical full checks with PostgreSQL"]["run"] == "python scripts/check.py --postgres"
    assert named["Run canonical full checks with PostgreSQL"]["if"] == "matrix.mode == 'postgres'"
    for mode in job["strategy"]["matrix"]["mode"]:
        selected = [step for step in steps if step.get("name", "").startswith("Run canonical full checks")
                    and step.get("if") == f"matrix.mode == '{mode}'"]
        assert len(selected) == 1
    assert not any("python -m pytest" in step.get("run", "") for step in steps)
    gate = named["Require route and monitor PostgreSQL cases with zero skips"]
    assert gate["if"] == "always() && matrix.mode == 'postgres'"
    ordered = [step.get("name") for step in steps]
    required_order = ["Create checkout-owned virtual environment", "Install pinned dependencies",
        "Install Chrome and browser dependencies", "Verify the two owned PostgreSQL identities without mutation",
        "Run canonical full checks without PostgreSQL", "Run canonical full checks with PostgreSQL",
        "Require route and monitor PostgreSQL cases with zero skips", "Save canonical check report"]
    assert [ordered.index(name) for name in required_order] == sorted(ordered.index(name) for name in required_order)
    assert steps.index(python) < ordered.index(required_order[0]) and steps.index(node) < ordered.index(required_order[0])
    upload = named["Save canonical check report"]
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "canonical-${{ matrix.mode }}-report"
    assert len({upload["with"]["name"].replace("${{ matrix.mode }}", mode)
                for mode in job["strategy"]["matrix"]["mode"]}) == 2
    assert upload["with"]["path"].splitlines() == ["data/check-runs/*/report.json", "data/check-runs/*/tests.xml", "data/check-runs/*/*.log"]
    assert upload["with"]["if-no-files-found"] == "error"
    for name in ("quality", "python-tests", "postgres-tests"):
        install = next(step["run"] for step in jobs[name]["steps"] if step.get("name") == "Install pinned dependencies")
        assert "-r requirements-deployment.lock" in install


@pytest.mark.parametrize("report_count", [0, 1, 2])
def test_full_postgres_job_requires_one_owned_report_and_ignores_nested_fixtures(tmp_path, report_count):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["postgres-tests"]["steps"]
                if step.get("name") == "Require route and monitor PostgreSQL cases with zero skips")
    code = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    for index in range(report_count):
        report = tmp_path / f"data/check-runs/run-{index}/tests.xml"
        report.parent.mkdir(parents=True)
        report.write_text("<testsuite/>", encoding="utf-8")
    nested = tmp_path / "data/check-runs/fixture/pytest/nested/tests.xml"
    nested.parent.mkdir(parents=True)
    nested.write_text("<testsuite/>", encoding="utf-8")
    recorder = tmp_path / "scripts/check_ci_postgres.py"
    recorder.parent.mkdir()
    recorder.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "Path('invocation.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, check=False, timeout=10)
    invocation = tmp_path / "invocation.json"
    if report_count != 1:
        assert result.returncode != 0
        assert "Expected exactly one canonical pytest report" in result.stderr
        assert not invocation.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(invocation.read_text(encoding="utf-8")) == [
            "--junit", str(Path("data/check-runs/run-0/tests.xml"))]


@pytest.mark.parametrize("skipped", [False, True])
def test_junit_gate_rejects_skips_and_requires_all_new_pg_cases(tmp_path, skipped, capsys):
    root = ET.Element("testsuite")
    for group, count in (("test_route_dispatch_postgres", 1), ("test_route_recovery", 1), ("test_monitor_configuration_postgres", 9)):
        for index in range(count):
            name = ("test_postgres_recovery_skips_locked_sender_then_reconciles_during_pause"
                    if group == "test_route_recovery" else "test_guard_" + str(index))
            case = ET.SubElement(root, "testcase", classname=group, name=name)
            if skipped and group == "test_monitor_configuration_postgres" and index == 0:
                ET.SubElement(case, "skipped", message="synthetic missing fixture")
    report = tmp_path / "tests.xml"
    ET.ElementTree(root).write(report)
    assert helper().main(["--junit", str(report)]) == (1 if skipped else 0)
    output = capsys.readouterr()
    assert "synthetic-private" not in output.out + output.err
