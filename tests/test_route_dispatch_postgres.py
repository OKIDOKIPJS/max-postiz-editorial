"""Реальный локальный PostgreSQL: один durable claim для двух обработчиков."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from sqlalchemy import create_engine, text

from test_hub import approve, variants
from max_postiz_agent.adapters import Delivery
from max_postiz_agent.db import Database, uid
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import Dispatch
from pg_test_contract import CONTRACT_ENV, endpoint_spec, verify_identity

pytest_plugins = ["test_four_platform_hub"]


@pytest.fixture
def postgres_routes(four_hub):
    url = os.getenv("TEST_DATABASE_URL", "")
    explicit = os.getenv(CONTRACT_ENV)
    contract = endpoint_spec("routes", url, Path(__file__).resolve().parents[1]) if explicit else None
    if contract is None:
        parsed = urlsplit(url)
        if (not url or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.port != 55448 or parsed.path != "/editorial_hub_tests"):
            pytest.skip("Dedicated copy-local PostgreSQL 55448 is not configured")
    admin = create_engine(url)
    if contract is not None:
        try:
            with admin.connect() as connection:
                verify_identity(connection, contract)
        except Exception:
            admin.dispose()
            raise
    schema = "routes_test_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated_url = url + "?" + urlencode({"options": "-csearch_path=" + schema})
    original = four_hub.db
    database = Database(four_hub.settings, url=isolated_url)
    try:
        database.initialize()
        database.restore(original.export())
        four_hub.db = database
        yield four_hub
    finally:
        four_hub.db = original
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_two_workers_can_publish_one_approved_route_only_once(postgres_routes):
    core = postgres_routes
    dispatch_id = approve(core, variants(core)["telegram"])
    adapter = core.fixture_publishers["telegram"]
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocking_publish(destination, snapshot, request_id, *, authorize=None):
        authorize()
        calls.append(request_id)
        entered.set()
        assert release.wait(10)
        return Delivery("confirmed", "fixture-message", verified=False,
                        evidence={"fixture": True, "verification": "api_receipt"})

    adapter.publish = blocking_publish
    other = HubService(core.settings, core.db, editor=core.editor, publisher=core.publisher,
                       http=core.http, route_publishers=core.fixture_publishers,
                       bootstrap_targets=False)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(core.publish_due, platform_routes_only=True)
            assert entered.wait(10)
            try:
                second = pool.submit(other.publish_due, platform_routes_only=True)
                assert second.result(timeout=10) == 0
            finally:
                release.set()
            assert first.result(timeout=10) == 1
        assert calls == [dispatch_id]
        with core.db.session() as session:
            row = session.get(Dispatch, dispatch_id)
            assert row.state == "published" and row.attempts == 1 and row.verified is False
    finally:
        release.set()
        other.routes.close()
        other.postiz.close()
