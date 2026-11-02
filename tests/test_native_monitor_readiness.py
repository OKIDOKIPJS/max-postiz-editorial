"""Monitor acceptance exercises real disabled lanes and loopback HTTP, never Send."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time

import pytest

from max_postiz_agent.config import Settings
from max_postiz_agent.isolated_workspace import assert_isolated, mark_isolated
from max_postiz_agent.monitor_config import initialize_monitor_config
from max_postiz_agent.monitor_store import MonitorStore
from max_postiz_agent.monitor_service import MonitorService
from max_postiz_agent.server_state import ServerState

pytest_plugins = ["test_four_platform_hub"]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario():
    spec = importlib.util.spec_from_file_location("native_monitor_scenario_test", ROOT / "deployment/linux/readiness_scenario.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state():
    return {"monitor_dsn": "postgresql+psycopg://fixture:fixture-password@127.0.0.1:55448/editorial_hub",
        "postiz_api_key": "synthetic-api-key", "internal_key": "synthetic-internal-key-00000000000000000000",
        "owner_id": "synthetic-owner", "org_id": "synthetic-org"}


@pytest.mark.parametrize("endpoint", [
    "postgresql+psycopg://fixture:fixture@127.0.0.1:5434/editorial_hub",
    "postgresql+psycopg://fixture:fixture@10.20.30.45:55448/editorial_hub",
    "postgresql+psycopg://fixture:fixture@127.0.0.1:55448/editorial_hub_restore",
    "postgresql+psycopg://fixture:fixture@127.0.0.1:55448/editorial_hub?options=unsafe",
])
def test_monitor_scope_rejects_primary_database_and_unowned_endpoints(scenario, tmp_path, endpoint):
    with pytest.raises(scenario.ScenarioError):
        scenario.monitor_scope({**state(), "monitor_dsn": endpoint}, tmp_path / "project")


def test_monitor_scope_uses_separate_roots_and_exact_allowed_restore_database(scenario, tmp_path):
    source = tmp_path / "project"
    initial, endpoint = scenario.monitor_scope(state(), source)
    restored, restored_endpoint = scenario.monitor_scope(state(), source, restored=True)
    assert initial == tmp_path / "monitor-copies/initial"
    assert restored == tmp_path / "monitor-copies/restored"
    assert not initial.is_relative_to(source) and not restored.is_relative_to(source)
    assert endpoint.endswith(":55448/editorial_hub")
    assert restored_endpoint.endswith(":55448/editorial_hub_tests")


def test_monitor_environment_restores_every_value_after_failure_without_faking_ci(scenario, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "previous-private-value")
    before = dict(os.environ)
    with pytest.raises(RuntimeError), scenario.monitor_environment(state(), state()["monitor_dsn"]):
        assert os.environ["DATABASE_URL"] == state()["monitor_dsn"]
        assert os.environ.get("GITHUB_ACTIONS") == before.get("GITHUB_ACTIONS")
        assert os.environ.get("RUNNER_ENVIRONMENT") == before.get("RUNNER_ENVIRONMENT")
        os.environ["NEW_COPY_VALUE"] = "ephemeral"
        raise RuntimeError("fixture")
    assert dict(os.environ) == before


def test_copy_bootstrap_uses_honest_marker_valid_settings_and_only_explicit_migration(scenario, tmp_path, monkeypatch):
    source = tmp_path / "project"
    source.mkdir()
    shutil.copytree(ROOT / "schemas", source / "schemas")
    (source / "config").mkdir()
    for path in (ROOT / "config").glob("*.example.json"):
        shutil.copy2(path, source / "config" / path.name)
    shutil.copy2(ROOT / "TOPIC.md", source / "TOPIC.md")
    (source / "private-marker").write_text("must-not-copy")
    parent = tmp_path / "monitor-copies"
    parent.mkdir()
    root = parent / "initial"
    value = state()
    calls = []

    def migrate(argv):
        assert argv == ["--root", str(root), "migrate"]
        assert_isolated(root)
        settings = Settings.load(root)
        assert settings.app["require_postgresql"] is True
        assert settings.app["max"]["adapter_mode"] == "fake"
        assert settings.app["opencode"]["adapter_mode"] == "fake"
        assert not any(row["enabled"] for row in settings.sources["sources"])
        assert json.loads((root / "config/publishing.json").read_text())["targets"] == []
        assert json.loads((root / "data/hourly-article-policy.local.json").read_text())["enabled"] is False
        calls.append("explicit")

    monkeypatch.setattr("max_postiz_agent.server_cli.main", migrate)
    instance = object.__new__(scenario.Scenario)
    instance.root = source
    with scenario.monitor_environment(value, value["monitor_dsn"]) as values:
        instance.monitor_copy(root, value["monitor_dsn"], values, restored=False)
        assert_isolated(root)
    marker = json.loads((root / "data/four-platform/isolation.json").read_text())
    assert marker["source_root"] == str(source.resolve()) and marker["root"] == str(root.resolve())
    assert marker["production_access"] is False
    assert not (source / "data/four-platform/isolation.json").exists()
    assert not (root / "private-marker").exists()
    assert calls == ["explicit"]


def test_monitor_history_covers_unknown_text_token_and_instance_but_excludes_heartbeat(scenario, tmp_path):
    store = MonitorStore(tmp_path)
    store.enqueue("unknown", "incident", "synthetic history", "a" * 64, "123", at=1, expires_at=100)
    claimed = store.claim("a" * 64, "123", at=1)
    store.finish(claimed, "unknown", {}, at=2)
    original = scenario.monitor_ledger_history(tmp_path)
    store.set_meta("observer", {"heartbeat_at": 10})
    store.set_meta("sender", {"heartbeat_at": 11, "state": "disabled"})
    assert scenario.monitor_ledger_history(tmp_path) == original
    with store.connection() as connection:
        connection.execute("UPDATE monitor_reports SET token='corrupted'")
    assert scenario.monitor_ledger_history(tmp_path) != original
    with store.connection() as connection:
        connection.execute("UPDATE monitor_reports SET token=?", (claimed["token"],))
    assert scenario.monitor_ledger_history(tmp_path) == original
    store.set_meta("instance_id", "different-instance")
    assert scenario.monitor_ledger_history(tmp_path) != original


def test_history_reader_rejects_unexpected_tables_and_never_creates_missing_ledger(scenario, tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        scenario.monitor_ledger_history(tmp_path)
    assert not (tmp_path / "data").exists()
    store = MonitorStore(tmp_path)
    with store.connection() as connection:
        connection.execute("CREATE TABLE unexpected(private_value TEXT)")
    with pytest.raises(scenario.ScenarioError, match="SCENARIO_MONITOR_TABLE_INVALID"):
        scenario.monitor_ledger_history(tmp_path)


def test_restore_keeps_own_marker_unknown_instance_and_private_runtime_bytes_without_migration(scenario, tmp_path, monkeypatch):
    source = tmp_path / "project"
    source.mkdir()
    shutil.copytree(ROOT / "schemas", source / "schemas")
    (source / "config").mkdir()
    for path in (ROOT / "config").glob("*.example.json"):
        shutil.copy2(path, source / "config" / path.name)
    shutil.copy2(ROOT / "TOPIC.md", source / "TOPIC.md")
    parent = tmp_path / "monitor-copies"
    parent.mkdir()
    backup = source / "data/ci-private/monitor-backup"
    (backup / "config").mkdir(parents=True)
    # A complete generated template set is part of the exact backup manifest.
    original = parent / "initial"
    instance = object.__new__(scenario.Scenario)
    instance.root = source
    value = state()
    monkeypatch.setattr("max_postiz_agent.server_cli.main", lambda args: None)
    with scenario.monitor_environment(value, value["monitor_dsn"]) as values:
        instance.monitor_copy(original, value["monitor_dsn"], values, restored=False)
    store = MonitorStore(original)
    row = store.enqueue("unknown", "incident", "never replay", "a" * 64, "123", at=1, expires_at=100)
    store.finish(store.claim("a" * 64, "123", at=1), "unknown", {}, at=2)
    sentinel = original / "data/hourly-articles/cancelled.local.json"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b'{"state":"cancelled","must_not_restore_to_pending":true}\n')
    for folder in ("config", "data"):
        shutil.copytree(original / folder, backup / folder, dirs_exist_ok=True)
    (backup / "data/four-platform/isolation.json").unlink()
    instance.state = {"monitor_backup_files": scenario.monitor_files(backup)}
    expected = scenario.monitor_ledger_history(original)

    def migration_forbidden(args):
        pytest.fail("Restoration/observation must not implicitly migrate")

    monkeypatch.setattr("max_postiz_agent.server_cli.main", migration_forbidden)
    restored, endpoint = scenario.monitor_scope(value, source, restored=True)
    with scenario.monitor_environment(value, endpoint) as values:
        instance.monitor_copy(restored, endpoint, values, restored=True)
        assert_isolated(restored)
    assert scenario.monitor_ledger_history(restored) == expected
    assert MonitorStore(restored).get(row["id"])["state"] == "unknown"
    assert MonitorStore(restored).meta("instance_id") == store.meta("instance_id")
    assert (restored / sentinel.relative_to(original)).read_bytes() == sentinel.read_bytes()
    marker = json.loads((restored / "data/four-platform/isolation.json").read_text())
    assert marker["root"] == str(restored.resolve()) and marker["root"] != str(original.resolve())


@pytest.fixture
def monitor_round_core(scenario, four_hub, monkeypatch):
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    root = four_hub.settings.root
    mark_isolated(root, root.parent / "source")
    initialize_monitor_config(root)
    ServerState(four_hub).initialize()
    (root / "data/hourly-articles").mkdir(exist_ok=True)
    store = MonitorStore(root)
    store.enqueue("unknown", "incident", "never sent", "a" * 64, "123", at=1, expires_at=100)
    store.finish(store.claim("a" * 64, "123", at=1), "unknown", {}, at=2)
    value = state()
    monkeypatch.setattr(scenario, "monitor_history", lambda core: scenario.monitor_ledger_history(core.settings.root))
    original = scenario.monitor_ledger_history(root)
    for suffix, name in (("KEY", "internal_key"), ("OWNER", "owner_id"), ("ORGANIZATION", "org_id")):
        monkeypatch.setenv("MONITOR_WATCHDOG_" + suffix, value[name])
    for key, name in (("KEY", "internal_key"), ("USER_ID", "owner_id"), ("ORGANIZATION_ID", "org_id")):
        monkeypatch.setenv("EDITORIAL_" + ("INTERNAL_" if key == "KEY" else "OWNER_") + key, value[name])
    return four_hub, value, store, original


def test_real_disabled_monitor_lanes_http_and_watchdog_leave_unknown_history_unchanged(scenario, monitor_round_core):
    from max_postiz_agent.monitor_api import monitor_status
    from max_postiz_agent.monitor_config import MonitorConfig
    four_hub, value, store, original = monitor_round_core
    # Restart/restore can inherit a perfectly fresh ledger. This invocation must
    # update all three timestamps rather than accepting those saved observations.
    service = MonitorService(four_hub, MonitorConfig.load(four_hub.settings.root))
    service.produce()
    service.deliver_once()
    baseline = monitor_status(four_hub)["observer"]
    result = scenario.monitor_round(four_hub, value)
    assert result["configured_disabled"] and result["fresh_heartbeat"] and result["sender_disabled"]
    assert result["owner_api_verified"] and result["watchdog_no_notify"] and result["no_browser_or_send"]
    assert result["heartbeat_advanced"] and result["actual_lanes_stopped"]
    observer = monitor_status(four_hub)["observer"]
    assert all(observer[field] > baseline[field] for field in (
        "heartbeat_at", "source_collected_at", "sender_heartbeat_at"))
    assert scenario.monitor_ledger_history(four_hub.settings.root) == original
    assert store.get(store.recent()[0]["id"])["state"] == "unknown"
    assert not scenario.monitor_lanes()
    assert not any(thread.name in {"ci-monitor-run", "ci-monitor-api"} for thread in threading.enumerate())


def test_fresh_saved_heartbeat_does_not_accept_noop_restarted_monitor(scenario, monitor_round_core, monkeypatch):
    from max_postiz_agent.monitor_api import monitor_status
    from max_postiz_agent.monitor_config import MonitorConfig
    core, value, store, original = monitor_round_core
    service = MonitorService(core, MonitorConfig.load(core.settings.root))
    service.produce()
    service.deliver_once()
    baseline = monitor_status(core)
    assert baseline["freshness"] == "fresh" and baseline["observer"]["sender_state"] == "disabled"
    monkeypatch.setattr("max_postiz_agent.monitor_cli.run", lambda *_: None)
    with pytest.raises(scenario.ScenarioError, match="SCENARIO_MONITOR_HEARTBEAT_INVALID"):
        scenario.monitor_round(core, value)
    assert monitor_status(core)["observer"] == baseline["observer"]
    assert scenario.monitor_ledger_history(core.settings.root) == original
    assert store.unresolved()
    assert not scenario.monitor_lanes()


@pytest.mark.parametrize("fail_watchdog", [False, True], ids=["successful-http", "watchdog-failure"])
def test_real_slow_lane_blocks_history_and_backup_on_success_and_failure_paths(
        scenario, monitor_round_core, monkeypatch, fail_watchdog):
    core, value, _, _ = monitor_round_core
    entered, release = threading.Event(), threading.Event()
    original_join = threading.Thread.join
    original_transport = MonitorService._transport
    history_calls = []
    original_history = scenario.monitor_history

    def history(core):
        history_calls.append(True)
        return original_history(core)

    def receive(self):
        entered.set()
        assert release.wait(30), "test must release its real monitor lane"
        return {"state": "disabled"}

    def bounded_join(thread, timeout=None):
        # The production run() still owns/spawns every lane. Shorten only its
        # existing bounded receive join so the regression need not sleep 10s.
        return original_join(thread, timeout=0.01 if thread.name == "monitor-receive" else timeout)

    def watchdog(*args, **kwargs):
        assert entered.wait(1)
        raise scenario.ScenarioError("SCENARIO_TEST_WATCHDOG_FAILURE")

    monkeypatch.setattr(scenario, "monitor_history", history)
    monkeypatch.setattr(MonitorService, "receive_once", receive)
    monkeypatch.setattr(threading.Thread, "join", bounded_join)
    if fail_watchdog:
        monkeypatch.setattr("max_postiz_agent.monitor_cli.watchdog_once", watchdog)
    try:
        with pytest.raises(scenario.ScenarioError, match="SCENARIO_MONITOR_LANES_NOT_STOPPED"):
            scenario.monitor_round(core, value)
        assert entered.is_set()
        assert any(thread.name == "monitor-receive" for thread in scenario.monitor_lanes())
        assert MonitorService._transport is not original_transport  # Deny transport remains armed.
        assert len(history_calls) == 1  # No post-run history/snapshot/backup while a lane is live.
    finally:
        release.set()
        for thread in scenario.monitor_lanes():
            original_join(thread, timeout=5)
        MonitorService._transport = original_transport
    assert not scenario.monitor_lanes()
    assert not any(thread.name in {"ci-monitor-run", "ci-monitor-api"} for thread in threading.enumerate())


@pytest.mark.parametrize("field", ["heartbeat_at", "source_collected_at", "sender_heartbeat_at"])
def test_every_heartbeat_must_advance_from_this_round(scenario, field):
    before = {name: time.time() for name in ("heartbeat_at", "source_collected_at", "sender_heartbeat_at")}
    started = max(before.values()) + 0.1
    current = {name: started + 0.1 for name in before}
    assert scenario.monitor_advanced({"observer": current}, before, started)
    current[field] = before[field]
    assert not scenario.monitor_advanced({"observer": current}, before, started)
