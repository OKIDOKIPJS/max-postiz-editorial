"""Проверки переноса истории, конкурентных изменений и общей паузы."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.server_api import editorial_app
from max_postiz_agent.server_models import EditorialLedger
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState, default_settings

pytest_plugins = ["test_article_cycle"]


def test_migration_preserves_source_history_and_legacy_file(cycle):
    old, core, article, _, _, _, _ = cycle
    prepared = old.prepare(article)
    before = old.ledger_path.read_bytes()
    payload = json.loads(before)
    proof = DatabaseArticleLedger(old).migrate()
    assert proof["records"] == 1 and proof["hash"] == fingerprint(payload)
    assert old.ledger_path.read_bytes() == before
    fresh = HourlyArticleCycle(core)
    assert fresh._load() == payload
    assert fresh._load()["records"][0]["id"] == prepared["id"]
    changed = fresh._load()
    changed["records"][0]["state"] = "cancelled"
    fresh._save(changed)
    assert fresh.ledger_path.read_bytes() == before
    assert HourlyArticleCycle(core)._load()["records"][0]["state"] == "cancelled"


def test_stale_ledger_writer_cannot_overwrite_history(cycle):
    old, core, *_ = cycle
    DatabaseArticleLedger(old).migrate()
    first, second = HourlyArticleCycle(core), HourlyArticleCycle(core)
    a, b = first._load(), second._load()
    a["records"].append({"id": "one", "slot": 1, "state": "cancelled"})
    b["records"].append({"id": "two", "slot": 2, "state": "cancelled"})
    first._save(a)
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_REVISION_CONFLICT"):
        second._save(b)
    assert HourlyArticleCycle(core)._load() == a


def test_missing_database_never_falls_back_to_old_json(cycle):
    old, core, *_ = cycle
    DatabaseArticleLedger(old).migrate()
    with core.db.session.begin() as session:
        session.execute(delete(EditorialLedger))
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_DATABASE_MISSING"):
        HourlyArticleCycle(core)._load()


def test_common_pause_blocks_even_direct_legacy_cli(cycle):
    old, core, article, _, calls, *_ = cycle
    store = ServerState(core)
    store.initialize()
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        old.prepare(article)
    assert calls == [] and old._load() == {"records": []}
    assert core.publish_due() == 0


def test_lost_leader_fences_the_next_mutation(cycle):
    old, core, article, _, calls, *_ = cycle
    def lost():
        raise AgentError("EDITORIAL_LEADERSHIP_LOST", 409)
    core.editorial_leader_check = lost
    with pytest.raises(AgentError, match="EDITORIAL_LEADERSHIP_LOST"):
        old.prepare(article)
    assert calls == [] and old._load() == {"records": []}


def test_settings_revision_is_independent_from_worker_heartbeat(cycle):
    _, core, *_ = cycle
    store = ServerState(core)
    first = store.initialize()
    store.phase("translating", record_id="record")
    assert store.read()["revision"] == first["revision"]
    settings = default_settings()
    settings["nasa"]["keywords"] = ["ocean"]
    second = store.settings(settings, first["revision"], "postiz:owner")
    assert second["revision"] == first["revision"] + 1
    with pytest.raises(AgentError, match="EDITORIAL_REVISION_CONFLICT"):
        store.settings(default_settings(), first["revision"], "postiz:owner")
    store.control("start", second["revision"], "postiz:owner")
    with pytest.raises(AgentError, match="EDITORIAL_PAUSE_BEFORE_SETTINGS"):
        store.settings(settings, store.read()["revision"], "postiz:owner")


@pytest.mark.parametrize("blocker", [None, "target", "sending", "unknown", "dispatch_owner",
                                    "dispatch_lease", "bridge_owner", "bridge_lease", "preparation"])
def test_prepare_only_resume_is_fenced_and_never_resumes_targets(cycle, blocker):
    from sqlalchemy import select
    from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval
    from max_postiz_agent.server_models import EditorialPreparationClaim

    old, core, article, _, calls, *_ = cycle
    record = old.prepare(article)
    store = ServerState(core)
    initial = store.initialize()
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
        if blocker == "target":
            session.get(Target, "max").paused = False
        if blocker in {"sending", "unknown", "dispatch_owner", "dispatch_lease"}:
            approval = TargetApproval(variant_id=record["variant_id"], revision=1,
                snapshot_hash="a" * 64, scheduled_at=1, actor="test")
            session.add(approval)
            session.flush()
            session.add(Dispatch(variant_id=record["variant_id"], approval_id=approval.id,
                revision=1, snapshot_hash="a" * 64, snapshot={}, due_at=1,
                state=blocker if blocker in {"sending", "unknown"} else "pending",
                owner="owner" if blocker == "dispatch_owner" else None,
                lease_until=1 if blocker == "dispatch_lease" else 0))
        if blocker in {"bridge_owner", "bridge_lease"}:
            session.add(BridgePost(post_id="test", integration_id="test", group_id="test", target_id="max",
                variant_id=record["variant_id"], remote_hash="a" * 64, remote_state="DRAFT", due_at=1,
                mutation_owner="owner" if blocker == "bridge_owner" else None,
                mutation_until=1 if blocker == "bridge_lease" else 0))
        if blocker == "preparation":
            session.add(EditorialPreparationClaim(control_revision=initial["revision"], active_key="models",
                                                 state="preparing"))
    if blocker:
        with pytest.raises(AgentError, match="MONITOR_COPY_TARGETS_NOT_PAUSED" if blocker == "target"
                           else "ARTICLE_RECONCILIATION_REQUIRED"):
            store.control("start", initial["revision"], "monitor-owner", prepare_only=True)
        assert store.read()["paused"] is True and store.read()["revision"] == initial["revision"]
    else:
        value = store.control("start", initial["revision"], "monitor-owner", prepare_only=True)
        assert value["paused"] is False and value["revision"] == initial["revision"] + 1
        with core.db.session() as session:
            assert all(target.paused for target in session.scalars(select(Target)))
    assert calls == []


@pytest.fixture
def api(cycle, monkeypatch):
    _, core, *_ = cycle
    store = ServerState(core)
    store.initialize()
    monkeypatch.setenv("EDITORIAL_INTERNAL_KEY", "a" * 48)
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "owner-id")
    monkeypatch.setenv("EDITORIAL_OWNER_ORGANIZATION_ID", "org-id")
    headers = {"X-Editorial-Key": "a" * 48, "X-Editorial-Owner": "owner-id",
               "X-Editorial-Organization": "org-id"}
    return TestClient(editorial_app(core, store)), store, headers


def test_postiz_owner_identity_and_machine_key_are_both_required(api):
    client, _, headers = api
    assert client.get("/internal/editorial/state").status_code == 403
    assert client.get("/internal/editorial/state", headers={**headers,
        "X-Editorial-Owner": "other"}).status_code == 403
    response = client.get("/internal/editorial/state", headers=headers)
    assert response.status_code == 200 and response.json()["paused"] is True
    assert response.json()["capabilities"]["reddit"]["available"] is False
    assert "a" * 48 not in response.text
    assert client.get("/hub").status_code == 404
    assert client.post("/api/hub/variants/example/approve").status_code == 404


def test_postiz_settings_preserve_structured_filters_and_reject_stale_forms(api):
    client, store, headers = api
    value = copy.deepcopy(client.get("/internal/editorial/settings", headers=headers).json())
    value["settings"]["reddit"].update(authors=["scientist"], keywords=["ocean"], min_score=100)
    response = client.put("/internal/editorial/settings", headers=headers, json=value)
    assert response.status_code == 200
    assert store.read()["settings"]["reddit"]["min_score"] == 100
    assert client.put("/internal/editorial/settings", headers=headers, json=value).status_code == 409
    value = response.json()
    value["settings"]["source"] = "reddit"
    assert client.put("/internal/editorial/settings", headers=headers, json=value).status_code == 409


def test_input_validation_does_not_echo_private_values(api):
    client, _, headers = api
    response = client.post("/internal/editorial/control", headers=headers,
        json={"action": "start", "revision": "private-input"})
    assert response.status_code == 422 and "private-input" not in response.text
    assert client.post("/internal/editorial/control", headers=headers,
        json={"action": "pause", "revision": True}).status_code == 422
