"""Opt-in PostgreSQL 17 proof in a newly owned loopback QA cluster only.

MONITOR_OWNED_PG_TEST_URL is deliberately separate from every existing PG fixture.
No production instance, peer test database, browser or real publishing transport.
"""

import copy
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from sqlalchemy import create_engine, func, select, text

from max_postiz_agent.article_reserve import ArticleReserveStore
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Database, uid
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.monitor_commands import _editorial_actor
from max_postiz_agent.server_models import EditorialControl, EditorialLedger, EditorialPreparationClaim
from max_postiz_agent.server_state import (
    CONFIGURATION_LOCK, CONTROL_ID, LEDGER_ID, ServerState, fence_editorial_configuration,
)
from pg_test_contract import CONTRACT_ENV, endpoint_spec, verify_identity

pytest_plugins = ["test_max_bridge"]


@pytest.fixture
def owned_pg_bridge(bridge):
    url = os.getenv("MONITOR_OWNED_PG_TEST_URL", "")
    explicit = os.getenv(CONTRACT_ENV)
    if not url and not explicit:
        pytest.skip("Owned monitor PostgreSQL QA is not configured")
    contract = endpoint_spec("monitor", url, Path(__file__).resolve().parents[1]) if explicit else None
    if contract is None:
        parsed = urlsplit(url)
        if (parsed.scheme != "postgresql+psycopg" or parsed.hostname != "127.0.0.1"
                or parsed.port != 55449 or parsed.path != "/editorial_hub_tests"
                or parsed.query or parsed.fragment):
            pytest.fail("Owned QA endpoint does not match the dedicated loopback test cluster")
    admin = create_engine(url, pool_pre_ping=True)
    try:
        with admin.connect() as connection:
            if contract is not None:
                verify_identity(connection, contract)
            else:
                assert connection.scalar(text("SELECT current_setting('cluster_name')")) == "codex-max-monitor-qa"
                assert 170000 <= int(connection.scalar(text("SHOW server_version_num"))) < 180000
    except Exception:
        admin.dispose()
        raise
    schema = "monitor_configuration_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = url + "?" + urlencode({"options": "-csearch_path=" + schema})
    core = bridge[0]
    original = core.db
    database = Database(core.settings, url=isolated)
    try:
        database.initialize()
        database.restore(original.export())
        core.db = database
        yield bridge
    finally:
        core.db = original
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def draft(bridge):
    core, records, *_ = bridge
    records["post-max"]["state"] = "DRAFT"
    core.bridge.sync(force=True)
    return core


def all_targets_paused(core):
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True


@pytest.mark.parametrize("control_exists", [True, False])
def test_real_pg_advisory_fence_blocks_new_bridge_lease_before_variant(owned_pg_bridge, monkeypatch, control_exists):
    from max_postiz_agent import server_state

    core = draft(owned_pg_bridge)
    state = ServerState(core)
    initial = state.initialize() if control_exists else None
    original_variant = core._variant
    waiting, touched_variant = threading.Event(), threading.Event()
    revisions = []
    caller = threading.get_ident()

    def fenced(session):
        if threading.get_ident() != caller:
            waiting.set()
        return fence_editorial_configuration(session)

    def variant(session, ident, lock=False):
        if lock:
            row = session.get(EditorialControl, CONTROL_ID)
            revisions.append(row.revision if row else None)
            touched_variant.set()
        return original_variant(session, ident, lock)

    monkeypatch.setattr(server_state, "fence_editorial_configuration", fenced)
    monkeypatch.setattr(core, "_variant", variant)
    monkeypatch.setattr(core.bridge, "sync", lambda **kwargs: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with core.db.session.begin() as configuration:
            fence_editorial_configuration(configuration)
            row = configuration.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            if control_exists:
                row.revision += 1
                configuration.flush()
            else:
                assert row is None
            with core.db.engine.connect() as competing:
                assert competing.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                        {"key": CONFIGURATION_LOCK}) is False
            pending = pool.submit(core.bridge.mutation, ["post-max"])
            assert waiting.wait(5)
            assert not touched_variant.wait(0.2)
            with core.db.session() as inspect:
                assert inspect.get(BridgePost, "post-max").mutation_owner is None
        lease = pending.result(timeout=5)
    assert revisions == [initial["revision"] + 1 if control_exists else None]
    with core.db.session() as inspect:
        assert inspect.get(BridgePost, "post-max").mutation_owner == lease["owner"]
    core.bridge.mutation(["post-max"], release=lease["owner"])


def test_real_pg_paused_draft_lease_blocks_atomic_settings_and_prepare_resume(owned_pg_bridge):
    core = draft(owned_pg_bridge)
    state = ServerState(core)
    initial = state.initialize()
    all_targets_paused(core)
    lease = core.bridge.mutation(["post-max"])
    changed = copy.deepcopy(initial["settings"])
    changed["nasa"]["keywords"] = ["new-filter"]
    with core.db.session() as session:
        audit_count = session.scalar(select(func.count()).select_from(Audit))
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        state.settings(changed, initial["revision"], "owned-pg-qa", require_quiescent=True)
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        state.control("start", initial["revision"], "owned-pg-qa", prepare_only=True)
    assert state.read() == initial
    with core.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Audit)) == audit_count
    core.bridge.mutation(["post-max"], release=lease["owner"])
    final = state.settings(changed, initial["revision"], "owned-pg-qa", require_quiescent=True)
    assert final["settings"] == changed and final["revision"] == initial["revision"] + 1
    with core.db.session() as session:
        assert all(target.paused for target in session.scalars(select(Target)))
    assert owned_pg_bridge[3] == []


@pytest.mark.parametrize("claim_state,active_key", [("preparing", "models"), ("unknown", None),
                                                  ("blocked_unknown", None), ("failed", None)])
def test_real_pg_active_or_unknown_claim_blocks_settings_without_changing_revision(owned_pg_bridge, claim_state, active_key):
    core = owned_pg_bridge[0]
    state = ServerState(core)
    initial = state.initialize()
    all_targets_paused(core)
    claim = EditorialPreparationClaim(control_revision=initial["revision"], state=claim_state, active_key=active_key)
    with core.db.session.begin() as session:
        session.add(claim)
    changed = copy.deepcopy(initial["settings"])
    changed["nasa"]["authors"] = ["new-author"]
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        state.settings(changed, initial["revision"], "owned-pg-qa", require_quiescent=True)
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        state.control("start", initial["revision"], "owned-pg-qa", prepare_only=True)
    assert state.read() == initial
    with core.db.session() as session:
        stored = session.get(EditorialPreparationClaim, claim.id)
        assert stored.token == claim.token and stored.state == claim_state and stored.active_key == active_key


def test_real_pg_compact_owner_actor_fits_actual_varchar_and_core_audit_insert(owned_pg_bridge):
    core = owned_pg_bridge[0]
    state = ServerState(core)
    initial = state.initialize()
    all_targets_paused(core)
    principal = "max-account-sha256:" + "a" * 64
    actor = _editorial_actor(principal)
    assert len(actor) == 64 and actor != "max-monitor:" + principal
    with core.db.engine.connect() as connection:
        assert connection.scalar(text("SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='audit_events' AND column_name='actor'")) == 64
    paused = state.control("pause", initial["revision"], actor)
    changed = copy.deepcopy(paused["settings"])
    changed["nasa"]["keywords"] = ["audit-proof"]
    state.settings(changed, paused["revision"], actor, require_quiescent=True)
    with core.db.session() as session:
        events = list(session.scalars(select(Audit).where(Audit.actor == actor).order_by(Audit.at)))
        assert {event.action for event in events} == {"editorial_control", "editorial_settings"}
        assert len(events) == 2 and all(len(event.actor) == 64 for event in events)
    assert owned_pg_bridge[3] == []


def test_real_pg_closed_claim_and_ledger_survive_revision_changes_without_active_replay(owned_pg_bridge):
    """Structural history proof; this fixture does not claim full article QA eligibility."""
    core = owned_pg_bridge[0]
    state = ServerState(core)
    initial = state.initialize()
    all_targets_paused(core)
    record_id, claim_id, token = uid(), uid(), uid()
    binding = {"id": claim_id, "token": token, "control_revision": initial["revision"]}
    payload = {"records": [{"id": record_id, "state": "quality_ready", "reserve_claim": binding}]}
    with core.db.session.begin() as session:
        session.add(EditorialPreparationClaim(id=claim_id, token=token, record_id=record_id,
            control_revision=initial["revision"], state="quality_ready", active_key=None))
        session.add(EditorialLedger(id=LEDGER_ID, revision=1, payload=copy.deepcopy(payload)))
    running = state.control("start", initial["revision"], "owned-pg-qa", prepare_only=True)
    paused = state.control("pause", running["revision"], "owned-pg-qa")
    changed = copy.deepcopy(paused["settings"])
    changed["nasa"]["keywords"] = ["future-only"]
    configured = state.settings(changed, paused["revision"], "owned-pg-qa", require_quiescent=True)
    state.control("start", configured["revision"], "owned-pg-qa", prepare_only=True)
    reserve = ArticleReserveStore(core)
    assert reserve.require_preparation(binding, record_id, active=False)["state"] == "quality_ready"
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_CLAIM_LOST"):
        reserve.require_preparation(binding, record_id, active=True)
    with core.db.session() as session:
        ledger = session.get(EditorialLedger, LEDGER_ID)
        assert ledger.payload == payload and ledger.revision == 1
        stored = session.get(EditorialPreparationClaim, claim_id)
        assert stored.token == token and stored.control_revision == initial["revision"]
        assert stored.state == "quality_ready" and stored.active_key is None
        assert session.scalar(select(func.count()).select_from(Dispatch)) == 0
    assert owned_pg_bridge[3] == []


def _monitor_pg_bootstrap(core, monkeypatch):
    """Inject only this fixture's verified PG schema; runtime isolation guards stay real.

    QA uses its dedicated cluster rather than runtime's port 55448. No environment
    URL or runtime allowlist is broadened to make this schema-check proof possible.
    """
    from max_postiz_agent import hub_cli, monitor_cli

    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    root = core.settings.root
    mark_isolated(root, root.parent / "owned-monitor-bootstrap-source")
    owned_url = core.db.engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(hub_cli, "Database", lambda settings: Database(settings, url=owned_url))
    return lambda: monitor_cli.build_monitor(root)


def test_real_pg_monitor_bootstrap_checks_schema_without_migration_or_history_changes(owned_pg_bridge, monkeypatch):
    from max_postiz_agent.hub import HubService
    from max_postiz_agent.service import Service

    core = draft(owned_pg_bridge)
    state = ServerState(core)
    initial = state.initialize()
    all_targets_paused(core)
    with core.db.session.begin() as session:
        session.add(EditorialPreparationClaim(control_revision=initial["revision"], state="unknown"))
        session.add(EditorialLedger(id=LEDGER_ID, revision=7,
            payload={"records": [{"id": "historical-monitor-bootstrap", "state": "cancelled"}]}))
    before = core.db.export()
    bootstrap = _monitor_pg_bootstrap(core, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Ordinary monitor bootstrap attempted PostgreSQL migration or activation")

    monkeypatch.setattr(Database, "initialize", forbidden)
    monkeypatch.setattr(HubService, "sync_targets", forbidden)
    monkeypatch.setattr(Service, "activate", forbidden)
    started = bootstrap()
    try:
        assert started.db.engine.dialect.name == "postgresql"
        assert started.db.export() == before
    finally:
        started.close()
    assert core.db.export() == before
    assert owned_pg_bridge[3] == []


@pytest.mark.parametrize("change", ["missing_column", "unapproved_revision"])
def test_real_pg_monitor_bootstrap_rejects_schema_drift_without_ddl(owned_pg_bridge, monkeypatch, change):
    core = owned_pg_bridge[0]
    with core.db.engine.begin() as connection:
        if change == "missing_column":
            connection.execute(text("ALTER TABLE hub_dispatches DROP COLUMN verified"))
        else:
            connection.execute(text("UPDATE alembic_version SET version_num='unapproved_fixture_revision'"))

    def schema_snapshot():
        with core.db.engine.connect() as connection:
            columns = connection.execute(text(
                "SELECT table_name,column_name,data_type,is_nullable FROM information_schema.columns "
                "WHERE table_schema=current_schema() ORDER BY table_name,ordinal_position")).all()
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).all()
        return columns, revision

    before = schema_snapshot()
    bootstrap = _monitor_pg_bootstrap(core, monkeypatch)
    started = None
    try:
        with pytest.raises(AgentError, match="^SERVER_MIGRATION_REQUIRED$"):
            started = bootstrap()
    finally:
        if started is not None:
            started.close()
    assert schema_snapshot() == before
    assert owned_pg_bridge[3] == []
