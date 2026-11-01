"""CI requires every collected Windows test exactly once and preserves pytest failures."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_ci_shards.py"
WRAPPER = ROOT / "scripts/ci_pytest_shard.py"


def checker():
    spec = importlib.util.spec_from_file_location("ci_shards_checker_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collection_hash(nodeids):
    return hashlib.sha256(json.dumps(sorted(nodeids), ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


@pytest.fixture
def receipts(tmp_path):
    nodeids = [f"tests/test_fixture.py::test_case[{number}]" for number in range(28)]
    paths = []
    for index in range(4):
        selected = [nodeid for nodeid in nodeids
                    if int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), "big") % 4 == index]
        value = {"schema_version": "pytest-shard-v1", "shard_index": index, "shard_count": 4,
                 "collection_completed": True, "full_nodeids": nodeids,
                 "selected_nodeids": selected, "full_collection_hash": collection_hash(nodeids),
                 "total_count": len(nodeids), "selected_count": len(selected),
                 "deselected_count": len(nodeids) - len(selected), "exit_code": 0, "tests_failed": 0,
                 "pytest_args": ["--basetemp=.test-state/pytest", "--junitxml=.test-state/tests.xml", "-q"]}
        path = tmp_path / f"part-{index}" / "shard.json"
        path.parent.mkdir()
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)
    return tmp_path, paths


def test_valid_partition_cli_needs_only_standard_library_and_prints_no_nodeids(receipts):
    root, _ = receipts
    result = subprocess.run([sys.executable, "-S", str(CHECKER), str(root)], capture_output=True,
                            text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["partition_verified"] and summary["total_count"] == 28
    assert sum(summary["selected_counts"]) == 28 and "test_case" not in result.stdout


@pytest.mark.parametrize("damage", ["missing", "duplicate", "overlap", "corrupt", "hash", "nonzero",
    "filtered", "wrong_assignment", "missing_selected", "different_full", "duplicate_nodeids",
    "bad_counts", "bool_index", "wrong_schema", "incomplete", "failed_tests", "collect_only"])
def test_incomplete_or_invalid_receipt_partition_fails_without_printing_nodeids(receipts, damage, capsys):
    root, paths = receipts
    value = json.loads(paths[0].read_text(encoding="utf-8"))
    if damage == "missing":
        paths[0].unlink()
    elif damage == "duplicate":
        duplicate = root / "duplicate/shard.json"
        duplicate.parent.mkdir()
        duplicate.write_bytes(paths[0].read_bytes())
    elif damage == "corrupt":
        paths[0].write_text('{"private-nodeid":', encoding="utf-8")
    else:
        if damage == "overlap":
            other = json.loads(paths[1].read_text(encoding="utf-8"))
            value["selected_nodeids"].append(other["selected_nodeids"][0])
            value["selected_count"] += 1
            value["deselected_count"] -= 1
        elif damage == "hash":
            value["full_collection_hash"] = "0" * 64
        elif damage == "nonzero":
            value["exit_code"] = 1
        elif damage == "filtered":
            value["pytest_args"].extend(["-k", "private-nodeid"])
        elif damage == "wrong_assignment":
            value["shard_index"] = 1
        elif damage == "missing_selected":
            value["selected_nodeids"].pop()
            value["selected_count"] -= 1
            value["deselected_count"] += 1
        elif damage == "different_full":
            value["full_nodeids"].append("private-nodeid")
            value["total_count"] += 1
            value["deselected_count"] += 1
            value["full_collection_hash"] = collection_hash(value["full_nodeids"])
        elif damage == "duplicate_nodeids":
            value["full_nodeids"].append(value["full_nodeids"][0])
            value["full_collection_hash"] = collection_hash(value["full_nodeids"])
        elif damage == "bad_counts":
            value["selected_count"] += 1
        elif damage == "bool_index":
            value["shard_index"] = False
        elif damage == "wrong_schema":
            value["schema_version"] = "unknown"
        elif damage == "incomplete":
            value["collection_completed"] = False
        elif damage == "failed_tests":
            value["tests_failed"] = 1
        elif damage == "collect_only":
            value["pytest_args"].append("--collect-only")
        paths[0].write_text(json.dumps(value), encoding="utf-8")
    assert checker().main([str(root)]) == 1
    output = capsys.readouterr()
    assert "CI shard validation failed" in output.err
    assert "private-nodeid" not in output.err and "test_case" not in output.err


def test_collection_order_does_not_change_the_partition_receipt(receipts):
    root, paths = receipts
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["full_nodeids"].reverse()
        value["selected_nodeids"].reverse()
        path.write_text(json.dumps(value), encoding="utf-8")
    assert checker().check_shards(root)["partition_verified"]


def test_duplicate_indices_cannot_replace_a_missing_shard(receipts):
    root, paths = receipts
    paths[3].write_bytes(paths[0].read_bytes())
    module = checker()
    with pytest.raises(module.ShardValidationError, match="indices"):
        module.check_shards(root)


@pytest.mark.parametrize("outcome,expected", [("pass", 0), ("fail", 1), ("empty", 5)])
def test_wrapper_propagates_actual_pytest_exit_status(tmp_path, outcome, expected):
    fixture = tmp_path / "test_synthetic.py"
    fixture.write_text("def test_synthetic():\n    assert " + str(outcome != "fail") + "\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    command = [sys.executable, str(WRAPPER), "--shard-index", "0", "--shard-count", "1",
               "--shard-receipt", str(receipt), "--rootdir", str(tmp_path), "--confcutdir", str(tmp_path),
               str(fixture), "-q"]
    if outcome == "empty":
        command.extend(["-k", "no_such_test"])
    result = subprocess.run(command, cwd=tmp_path,
                            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONUTF8": "1"},
                            capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert result.returncode == saved["exit_code"] == expected
    assert saved["collection_completed"] and saved["selected_count"] == (outcome != "empty")
    assert saved["full_collection_hash"] == collection_hash(saved["full_nodeids"])


def test_wrapper_imports_checkout_scripts_without_pythonpath_from_another_cwd(tmp_path):
    repository = tmp_path / "synthetic-checkout"
    scripts, tests = repository / "scripts", repository / "tests"
    scripts.mkdir(parents=True)
    tests.mkdir()
    wrapper = scripts / "ci_pytest_shard.py"
    wrapper.write_bytes(WRAPPER.read_bytes())
    (scripts / "helper.py").write_text("VALUE = 'trusted-checkout-helper'\n", encoding="utf-8")
    fixture = tests / "test_import.py"
    fixture.write_text("from scripts.helper import VALUE\n\ndef test_import_from_checkout():\n"
                       "    assert VALUE == 'trusted-checkout-helper'\n", encoding="utf-8")
    receipt = repository / ".test-state/receipt.json"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(wrapper), "--shard-index", "0", "--shard-count", "1",
        "--shard-receipt", str(receipt), "--rootdir", str(repository), "--confcutdir", str(repository),
        str(fixture), "-q"], cwd=tmp_path, env=environment, capture_output=True,
        text=True, encoding="utf-8", timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved["exit_code"] == saved["tests_failed"] == 0 and saved["collection_completed"] is True
    assert saved["total_count"] == saved["selected_count"] == 1
    assert saved["full_nodeids"] == saved["selected_nodeids"] == ["tests/test_import.py::test_import_from_checkout"]
    assert saved["full_collection_hash"] == collection_hash(saved["full_nodeids"])


def test_wrapper_imports_project_namespace_helpers_like_python_m_pytest(tmp_path):
    project = tmp_path / "namespace-project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    # No __init__.py: repository helpers are an ordinary namespace package.
    (scripts / "helper.py").write_text("VALUE = 'project-root-helper'\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    fixture = tests / "test_namespace_import.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "from scripts import helper\n\n"
        "def test_root_helper_namespace():\n"
        "    assert helper.VALUE == 'project-root-helper'\n"
        "    assert Path(helper.__file__).resolve() == "
        "Path(__file__).resolve().parents[1] / 'scripts/helper.py'\n",
        encoding="utf-8",
    )
    config = project / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    pytest_args = ["-c", str(config), "--confcutdir", str(project),
                   "tests/test_namespace_import.py", "-q"]
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
               "LOCALAPPDATA", "APPDATA", "COMSPEC", "PATHEXT"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUTF8="1")
    baseline = subprocess.run([sys.executable, "-m", "pytest", *pytest_args], cwd=project,
                              env=environment, capture_output=True, text=True, encoding="utf-8",
                              timeout=60, check=False)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    receipt = project / "receipt.json"
    wrapped = subprocess.run(
        [sys.executable, str(WRAPPER), "--shard-index", "0", "--shard-count", "1",
         "--shard-receipt", str(receipt), *pytest_args],
        cwd=project, env=environment, capture_output=True, text=True, encoding="utf-8",
        timeout=60, check=False,
    )
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    nodeids = ["tests/test_namespace_import.py::test_root_helper_namespace"]
    assert wrapped.returncode == saved["exit_code"] == baseline.returncode
    assert saved["collection_completed"] is True
    assert saved["full_nodeids"] == saved["selected_nodeids"] == nodeids
    assert saved["full_collection_hash"] == collection_hash(nodeids)
    assert (saved["total_count"], saved["selected_count"], saved["deselected_count"]) == (1, 1, 0)


def test_actual_monitor_browser_collection_is_stable_across_fresh_processes(tmp_path):
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
               "LOCALAPPDATA", "APPDATA", "COMSPEC", "PATHEXT"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUTF8="1")
    receipts = []
    for attempt in range(2):
        receipt = tmp_path / f"collection-{attempt}.json"
        # Collect the real module at independent import times; never run a browser fixture.
        completed = subprocess.run(
            [sys.executable, str(WRAPPER), "--shard-index", "0", "--shard-count", "4",
             "--shard-receipt", str(receipt), "--collect-only", "-q",
             "tests/test_monitor_browser.py"],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8",
            timeout=60, check=False,
        )
        (tmp_path / f"collection-{attempt}.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8")
        assert completed.returncode == 0, "Actual monitor-browser collection failed; inspect collection log"
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        assert saved["collection_completed"] and saved["exit_code"] == saved["tests_failed"] == 0
        assert saved["full_collection_hash"] == collection_hash(saved["full_nodeids"])
        expected = [nodeid for nodeid in saved["full_nodeids"]
                    if int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest(), "big") % 4 == 0]
        assert saved["selected_nodeids"] == expected
        assert saved["total_count"] == len(saved["full_nodeids"]) > 11
        assert saved["selected_count"] == len(expected)
        assert sum("::test_unstable_identity_time_or_reply_is_not_exported[" in nodeid
                   for nodeid in saved["full_nodeids"]) == 7
        assert sum("::test_ambiguous_or_hidden_dom_fails_closed[" in nodeid
                   for nodeid in saved["full_nodeids"]) == 4
        receipts.append(saved)
    first, second = receipts
    assert first["full_collection_hash"] == second["full_collection_hash"], (
        "Actual monitor-browser node IDs changed between independent imports")
    assert first["full_nodeids"] == second["full_nodeids"]
    assert first["selected_nodeids"] == second["selected_nodeids"]
