"""Ordinary serve/status must not migrate, activate, synchronize or reset history."""

import json

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Database
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_cli import build
from max_postiz_agent.server_bootstrap import require_schema
from max_postiz_agent.server_models import EditorialPreparationClaim
from max_postiz_agent.service import Service
from test_server_route_admin import admin_core as admin_core
from test_server_route_admin import seed_dispatch


def test_readonly_bootstrap_preserves_unknown_approvals_pauses_and_claims(admin_core, monkeypatch):
    seed_dispatch(admin_core, "unknown", attempts=1)
    with admin_core.db.session.begin() as session:
        session.add(EditorialPreparationClaim(control_revision=1, active_key="models", state="preparing"))
    before = admin_core.db.export()
    def forbidden(*args, **kwargs):
        pytest.fail("Normal server startup must not mutate schema/configuration/history")
    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(HubService, "sync_targets", forbidden)
    monkeypatch.setattr(Service, "activate", forbidden)
    core = build(admin_core.settings.root, activate=False, initialize=False)
    try:
        assert core.db.export() == before
        assert core.settings.hash == admin_core.settings.hash
    finally:
        core.close()


def test_missing_sqlite_database_is_not_silently_created_or_migrated(admin_core):
    root = admin_core.settings.root
    path = root / "config/app.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state_dir"] = "data/new-empty-server-state"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(AgentError, match="SERVER_MIGRATION_REQUIRED"):
        build(root, activate=False, initialize=False)
    assert not (root / value["state_dir"] / "agent.db").exists()


def test_missing_column_is_reported_without_ddl_or_automatic_repair(admin_core):
    with admin_core.db.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE hub_dispatches DROP COLUMN verified")
    with pytest.raises(AgentError, match="SERVER_MIGRATION_REQUIRED"):
        require_schema(admin_core.db)
