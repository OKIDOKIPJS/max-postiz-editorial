"""Ordinary monitor commands read an existing application schema, never migrate it."""

import json

import pytest

from max_postiz_agent import monitor_cli
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Database
from max_postiz_agent.hub import HubService
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.monitor_config import initialize_monitor_config
from max_postiz_agent.server_models import EditorialPreparationClaim
from max_postiz_agent.service import Service
from test_server_route_admin import admin_core as admin_core
from test_server_route_admin import seed_dispatch


@pytest.fixture
def isolated_admin(admin_core, monkeypatch):
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    root = admin_core.settings.root
    mark_isolated(root, root.parent / "monitor-bootstrap-source")
    initialize_monitor_config(root)
    return admin_core


@pytest.mark.parametrize("command", ["status", "preview", "run", "watchdog-once"])
def test_monitor_commands_do_not_migrate_activate_or_rewrite_application_history(
    isolated_admin, monkeypatch, command, capsys,
):
    core = isolated_admin
    seed_dispatch(core, "unknown", attempts=1)
    with core.db.session.begin() as session:
        session.add(EditorialPreparationClaim(control_revision=1, active_key="models", state="preparing"))
    before = core.db.export()

    def forbidden(*args, **kwargs):
        pytest.fail("Ordinary monitor startup attempted schema/configuration/history initialization")

    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(HubService, "sync_targets", forbidden)
    monkeypatch.setattr(Service, "activate", forbidden)
    # This regression tests the real CLI/bootstrap, not a browser, worker loop or HTTP probe.
    runtime_calls = []
    monkeypatch.setattr(monitor_cli, "run", lambda project: runtime_calls.append("run"))
    monkeypatch.setattr(monitor_cli, "watchdog_once", lambda *args, **kwargs: runtime_calls.append("watchdog") or {})
    arguments = ["--root", str(core.settings.root), command]
    if command == "watchdog-once":
        arguments += ["--url", "https://observer.example/heartbeat", "--instance", "fixture-instance"]
    assert monitor_cli.main(arguments) == 0
    assert core.db.export() == before
    assert runtime_calls == ({"run": ["run"], "watchdog-once": ["watchdog"]}.get(command, []))
    if command != "run":
        assert capsys.readouterr().out


def test_monitor_missing_sqlite_schema_is_not_created(isolated_admin):
    root = isolated_admin.settings.root
    path = root / "config/app.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state_dir"] = "data/absent-monitor-application-state"
    path.write_text(json.dumps(value), encoding="utf-8")
    started = None
    try:
        with pytest.raises(AgentError, match="^SERVER_MIGRATION_REQUIRED$"):
            started = monitor_cli.build_monitor(root)
    finally:
        if started is not None:
            started.close()
    assert not (root / value["state_dir"] / "agent.db").exists()


def test_monitor_missing_column_is_rejected_without_automatic_repair(isolated_admin):
    core = isolated_admin
    with core.db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE hub_dispatches DROP COLUMN verified")
    with core.db.engine.connect() as connection:
        before = connection.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
    started = None
    try:
        with pytest.raises(AgentError, match="^SERVER_MIGRATION_REQUIRED$"):
            started = monitor_cli.build_monitor(core.settings.root)
    finally:
        if started is not None:
            started.close()
    with core.db.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all() == before
