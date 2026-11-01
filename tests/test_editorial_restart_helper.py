"""A fixed-unit restart is one-shot, quiet and tied to the existing worker."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError

OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[1] / "deployment/linux/restart-editorial.py"
    spec = importlib.util.spec_from_file_location("restart_helper_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def request_state(helper, monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    value = {"request_id": "a" * 32, "previous_worker_id": OLD, "expires_at": 1060}
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(helper, "selection", lambda actual: value)
    monkeypatch.setattr(helper.time, "time", lambda: 1000)
    monkeypatch.setattr(helper.time, "sleep", lambda duration: None)
    return core, value, tmp_path / "data/editorial-restart-claims" / ("a" * 32 + ".jsonl")


def status(worker=OLD, pending=True):
    return {"worker_id": worker, "paused": True, "restart_pending": pending}


def test_exact_fixed_restart_once_changes_worker_preserves_pause_and_refuses_reentry(helper, request_state, monkeypatch):
    core, value, claim = request_state
    states = iter([status(), status(), status(NEW, False)])
    monkeypatch.setattr(helper, "runtime", lambda: next(states))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command == ["/usr/bin/systemctl", "--no-ask-password", "restart", "max-postiz-editorial.service"]
        assert kwargs == {"capture_output": True, "timeout": 45, "check": False}
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(helper.subprocess, "run", run)
    assert helper.execute(core) == {"state": "restart_completed", "request_id": value["request_id"],
                                    "paused": True, "worker_changed": True, "external_publications": 0}
    events = [json.loads(line) for line in claim.read_text().splitlines()]
    assert [row["state"] for row in events] == ["claimed", "restart_intent", "restart_completed"]
    monkeypatch.setattr(helper, "runtime", lambda: status())
    with pytest.raises(AgentError, match="^RESTART_ALREADY_CLAIMED$"):
        helper.execute(core)
    assert len(calls) == 1


def test_worker_already_changed_refuses_any_claim_or_restart(helper, request_state, monkeypatch):
    core, _, claim = request_state
    monkeypatch.setattr(helper, "runtime", lambda: status(NEW, False))
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: pytest.fail("Second restart"))
    with pytest.raises(AgentError, match="^RESTART_WORKER_CHANGED$"):
        helper.execute(core)
    assert not claim.exists()


def test_changed_manifest_after_durable_claim_never_restarts(helper, request_state, monkeypatch):
    core, value, claim = request_state
    values = iter([value, {**value, "request_id": "b" * 32}])
    monkeypatch.setattr(helper, "selection", lambda actual: next(values))
    monkeypatch.setattr(helper, "runtime", lambda: status())
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: pytest.fail("Stale restart"))
    with pytest.raises(AgentError, match="^RESTART_SELECTION_STALE$"):
        helper.execute(core)
    events = [json.loads(line) for line in claim.read_text().splitlines()]
    assert [row["state"] for row in events] == ["claimed", "unknown"]


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_unknown_systemctl_outcome_is_durable_and_never_retried(helper, request_state, monkeypatch, failure):
    core, _, claim = request_state
    monkeypatch.setattr(helper, "runtime", lambda: status())
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("systemctl", 45)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(helper.subprocess, "run", run)
    with pytest.raises((AgentError, subprocess.TimeoutExpired)):
        helper.execute(core)
    assert json.loads(claim.read_text().splitlines()[-1])["state"] == "unknown"
    with pytest.raises(AgentError, match="^RESTART_ALREADY_CLAIMED$"):
        helper.execute(core)
    assert len(calls) == 1


def test_expiry_after_final_http_check_stops_before_systemctl(helper, request_state, monkeypatch):
    core, _, claim = request_state
    calls = []

    def runtime():
        calls.append(True)
        if len(calls) == 2:
            monkeypatch.setattr(helper.time, "time", lambda: 1060)
        return status()

    monkeypatch.setattr(helper, "runtime", runtime)
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: pytest.fail("Expired restart"))
    with pytest.raises(AgentError, match="^RESTART_SELECTION_EXPIRED$"):
        helper.execute(core)
    assert "restart_intent" not in claim.read_text()


def test_authority_constructor_failure_disposes_database_without_masking_error(helper, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(helper, "load_env", lambda root: None)
    monkeypatch.setattr(helper, "Settings", SimpleNamespace(load=lambda root: "settings"))
    monkeypatch.setattr(helper, "Database", lambda settings: SimpleNamespace(
        engine=SimpleNamespace(dispose=lambda: calls.append("dispose"))))

    def broken(*args, **kwargs):
        assert kwargs == {"bootstrap_targets": False}
        raise AgentError("CONFIG_INVALID", 409)

    monkeypatch.setattr(helper, "HubService", broken)
    with pytest.raises(AgentError, match="^CONFIG_INVALID$"):
        with helper.authority(tmp_path):
            pytest.fail("Broken core accepted")
    assert calls == ["dispose"]
