"""Real PostgreSQL migrations, locking, restart and backup restoration."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, text

from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import Settings
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Database, uid
from max_postiz_agent.hub import HubService
from conftest import write_topic
from test_hub import approve, variants


@pytest.fixture
def pg_hub(project):
    # Use a dedicated database and a random isolated schema, never the runtime schema.
    # Только явное тестовое окружение; runtime .env.local здесь не читаем.
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    admin = create_engine(url)
    schema = "hub_test_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = url + ("&" if "?" in url else "?") + urlencode({"options": "-csearch_path=" + schema})
    topic = Settings.load(project).topic
    topic["channels"][0]["publication"]["mode"] = "review"
    write_topic(project, topic)
    targets = [{"id": "max", "profile_id": "main", "platform": "max", "route": "max_api",
                "enabled": True, "destination": "-123", "options": {"max_chars": 3500}}]
    (project / "config/publishing.json").write_text(json.dumps({"schema_version": "1.0", "postiz": {},
                                                             "targets": targets, "tasks": []}), encoding="utf-8")
    settings = Settings.load(project)
    db = Database(settings, url=isolated)
    db.initialize()
    service = HubService(settings, db)
    service.activate()
    service.pause_target("max", False)
    yield service, isolated
    service.close()
    with admin.begin() as connection:
        connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
    admin.dispose()


def test_cancel_and_edit_cannot_race_inflight_send(pg_hub):
    hub, _ = pg_hub
    ident = variants(hub)["max"]
    dispatch = approve(hub, ident)
    entered, release = threading.Event(), threading.Event()
    def publish(*a, **k):
        entered.set()
        assert release.wait(10)
        return Delivery("confirmed", "simulated-inflight")
    hub.publisher.publish = publish
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(hub.publish_due)
        assert entered.wait(10)
        try:
            with pytest.raises(AgentError, match="DISPATCH_CANNOT_CANCEL"):
                hub.cancel_dispatch(dispatch)
            with pytest.raises(AgentError, match="CANCEL_OR_RECONCILE_FIRST"):
                hub.revise_variant(ident, 1, "Правка во время отправки")
        finally:
            release.set()
        assert worker.result(10) == 1


def test_real_postgres_migration_and_no_double_send(pg_hub):
    hub, isolated = pg_hub
    ident = variants(hub)["max"]
    approve(hub, ident)
    calls = []
    lock = threading.Lock()
    def publish(*a, **k):
        with lock:
            calls.append(a)
        return Delivery("confirmed", "simulated-post")
    hub.publisher.publish = publish
    other_db = Database(hub.settings, url=isolated)
    other = HubService(hub.settings, other_db, editor=hub.editor, publisher=hub.publisher)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: s.publish_due(), [hub, other]))
    assert sum(results) == 1 and len(calls) == 1
    with hub.db.engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0008"
        assert conn.execute(text("SELECT COUNT(*) FROM editorial_control")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM hub_dispatch_attempts")).scalar_one() == 1
    other.postiz.close()
    other_db.engine.dispose()


def test_reddit_inputs_share_unique_keys_on_postgres(pg_hub):
    hub, _ = pg_hub
    values = dict(title='A garden story', original='A garden full of flowers and friendly neighbors.',
                  url='https://www.reddit.com/r/stories/comments/abc123/garden/', target_id='max')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda origin: hub.reddit_intake.submit(**values, origin=origin),
                               ['manual', 'ifttt']))
    assert results[0]['id'] == results[1]['id']
    assert sum(not r['duplicate'] for r in results) == 1
    with hub.db.engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM hub_reddit_submissions')) == 1


def test_postgres_backup_snapshot_restores_and_search(pg_hub, project):
    hub, isolated = pg_hub
    variants(hub)
    snapshot = hub.db.export()
    schema = "hub_restore_" + uid().replace("-", "")
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    with admin.begin() as conn:
        conn.execute(text('CREATE SCHEMA "' + schema + '"'))
    base_url = os.environ["TEST_DATABASE_URL"]
    restored_url = base_url + ("&" if "?" in base_url else "?") + urlencode({"options": "-csearch_path=" + schema})
    db = Database(hub.settings, url=restored_url)
    try:
        db.initialize()
        db.restore(snapshot)
        restored = HubService(hub.settings, db, editor=hub.editor, publisher=hub.publisher)
        assert len(restored.search_archive("видео")) == 1
        assert restored.db.export()["hub_variants"] == snapshot["hub_variants"]
        restored.postiz.close()
    finally:
        db.engine.dispose()
        with admin.begin() as conn:
            conn.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()
