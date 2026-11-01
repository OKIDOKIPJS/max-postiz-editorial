"""Закрытый GET мониторинга не создаёт очередь и не читает приватные тела."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from max_postiz_agent import monitor_api
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.monitor_config import default_config
from max_postiz_agent.config import fingerprint
from max_postiz_agent.monitor_owner_chat import owner_contract
from max_postiz_agent.server_api import editorial_app

pytest_plugins = ["test_four_platform_hub"]
NOW = 1793520000.0
INSTANCE = "12345678-1234-4234-8234-123456789012"


@pytest.fixture
def monitor_client(four_hub, monkeypatch):
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    root = four_hub.settings.root
    mark_isolated(root, root.parent / "original-source")
    monkeypatch.setenv("EDITORIAL_INTERNAL_KEY", "fixture-editorial-key-00000000000000000000")
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "owner")
    monkeypatch.setenv("EDITORIAL_OWNER_ORGANIZATION_ID", "org")
    headers = {"X-Editorial-Key": "fixture-editorial-key-00000000000000000000",
               "X-Editorial-Owner": "owner", "X-Editorial-Organization": "org"}
    return TestClient(editorial_app(four_hub)), headers, root


def configure(root):
    (root / "config/monitor.json").write_text(json.dumps(default_config()), encoding="utf-8")


def ledger(root, *, observer=None, sender=None, instance=INSTANCE):
    path = root / "data/monitor/monitor.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE monitor_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE monitor_reports(id TEXT,kind TEXT,state TEXT,created_at REAL,updated_at REAL,
                                         text TEXT,evidence TEXT);
        """)
        values = {"instance_id": instance, "observer": observer if observer is not None else {
            "heartbeat_at": NOW - 10, "source_collected_at": NOW - 15,
            "source_heartbeat_at": NOW - 20, "source_paused": False,
            "private_cookie": "never-show-this-cookie"},
            "sender": sender if sender is not None else {"heartbeat_at": NOW - 5, "state": "idle"}}
        connection.executemany("INSERT INTO monitor_meta VALUES(?,?)",
                               [(key, json.dumps(value)) for key, value in values.items()])
        connection.execute("INSERT INTO monitor_reports VALUES(?,?,?,?,?,?,?)", (
            "mon-test", "hourly", "unknown", NOW - 40, NOW - 30,
            "private-report-body", "private-transport-evidence"))
    return path


@pytest.mark.parametrize("route", ["monitor", "monitor-preview", "monitor-heartbeat"])
def test_owner_auth_runs_before_any_private_read(monitor_client, monkeypatch, route):
    client, headers, _ = monitor_client
    def forbidden(*args, **kwargs):
        raise AssertionError("authorization must run first")
    monkeypatch.setattr(monitor_api, "_config", forbidden)
    assert client.get("/internal/editorial/" + route).status_code == 403
    assert client.get("/internal/editorial/" + route, headers={**headers,
        "X-Editorial-Owner": "intruder"}).status_code == 403


def test_missing_config_status_is_safe_and_creates_no_monitor_directory(monitor_client):
    client, headers, root = monitor_client
    for route in ("monitor", "monitor-preview", "monitor-heartbeat"):
        response = client.get("/internal/editorial/" + route, headers=headers)
        assert response.status_code == 200
        assert response.json()["available"] is False
        assert response.headers["cache-control"] == "no-store"
    assert not (root / "data/monitor").exists()
    assert not (root / "config/monitor.json").exists()


def test_configured_status_does_not_create_outbox_when_worker_has_not_started(monitor_client):
    client, headers, root = monitor_client
    configure(root)
    response = client.get("/internal/editorial/monitor", headers=headers).json()
    assert response["available"] is True
    assert response["ledger_available"] is False
    assert response["enabled"] is False
    assert response["freshness"] == "unobserved"
    assert response["commands"]["receiver_state"] == "unobserved"
    assert response["commands"]["receiver_heartbeat_at"] is None
    assert response["reports"] == {"counts": {}, "last": None}
    assert not (root / "data/monitor").exists()


@pytest.mark.parametrize("owner_version", ["max-owner-chat-dom-v1", "max-owner-chat-dom-v2"])
def test_owner_outgoing_pairing_needs_accepted_contract_but_no_numeric_sender_ids(monitor_client, owner_version):
    client, headers, root = monitor_client
    config = default_config()
    commands = config["commands"]
    commands.update(auth_mode="authenticated_account_outgoing", account_sha256="a" * 64,
        control_chat_id="-123", control_chat_name="Команды", account_user_id="", owner_ids=[],
        allowed_actions=["status", "reserve", "refill"], dom_contract=owner_contract(version=owner_version),
        dom_contract_verified=True, dom_contract_sha256=fingerprint(owner_contract(version=owner_version)))
    path = root / "config/monitor.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    value = client.get("/internal/editorial/monitor", headers=headers).json()
    assert value["commands"]["paired"] is True
    assert value["commands"]["receiver_state"] == "unobserved"
    assert value["commands"]["owner_count"] == 0
    assert "a" * 64 not in json.dumps(value)
    commands["dom_contract_verified"] = False
    path.write_text(json.dumps(config), encoding="utf-8")
    assert client.get("/internal/editorial/monitor", headers=headers).json()["commands"]["paired"] is False
    commands.update(auth_mode="numeric_sender", account_sha256="", owner_ids=["123"], account_user_id="456")
    path.write_text(json.dumps(config), encoding="utf-8")
    assert client.get("/internal/editorial/monitor", headers=headers).json()["commands"]["paired"] is True
    commands["owner_ids"] = []
    path.write_text(json.dumps(config), encoding="utf-8")
    assert client.get("/internal/editorial/monitor", headers=headers).json()["commands"]["paired"] is False


def test_readonly_status_and_heartbeat_expose_only_safe_fields_and_keep_file_unchanged(monitor_client, monkeypatch):
    client, headers, root = monitor_client
    configure(root)
    path = ledger(root)
    before = path.read_bytes()
    monkeypatch.setattr(monitor_api.time, "time", lambda: NOW)
    statuses = [client.get("/internal/editorial/" + route, headers=headers).json()
                for route in ("monitor", "monitor-heartbeat")]
    status, heartbeat = statuses
    assert status["ledger_available"] is True and status["freshness"] == "fresh"
    assert status["reports"]["last"]["state"] == "unknown"
    assert status["reports"]["counts"] == {"unknown": 1}
    assert status["commands"]["history_available"] is False
    assert status["commands"]["recent"] == []
    assert heartbeat["available"] is True and heartbeat["schema"] == "monitor-heartbeat-v1"
    assert heartbeat["instance_id"] == INSTANCE
    assert heartbeat["observer"] == {"heartbeat_at": NOW - 10, "source_collected_at": NOW - 15,
        "source_heartbeat_at": NOW - 20, "source_paused": False,
        "sender_heartbeat_at": NOW - 5, "sender_state": "idle"}
    serialized = json.dumps(statuses)
    assert all(private not in serialized for private in (
        "never-show-this-cookie", "private-report-body", "private-transport-evidence"))
    assert path.read_bytes() == before
    assert not path.with_suffix(".sqlite-journal").exists()


def test_command_receiver_health_is_separate_from_configuration_and_excludes_dom_errors(monitor_client, monkeypatch):
    client, headers, root = monitor_client
    configure(root)
    path = ledger(root)
    monkeypatch.setattr(monitor_api.time, "time", lambda: NOW)
    for receiver, state, freshness, heartbeat in (
        ({"state": "ready", "heartbeat_at": NOW - 10}, "ready", "fresh", NOW - 10),
        ({"state": "blocked", "heartbeat_at": NOW - 5, "error": "private DOM cookie and URL"},
         "blocked", "fresh", NOW - 5),
        ({"state": "ready", "heartbeat_at": NOW - 600}, "ready", "stale", NOW - 600),
        ({"state": "ready", "heartbeat_at": NOW + 60}, "ready", "stale", NOW + 60),
        ({"state": ["private-error"], "heartbeat_at": "private-time"}, "unobserved", "unobserved", None),
    ):
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT OR REPLACE INTO monitor_meta VALUES('command_receiver',?)",
                               (json.dumps(receiver),))
        before = path.read_bytes()
        response = client.get("/internal/editorial/monitor", headers=headers).json()
        commands = response["commands"]
        assert commands["enabled"] is False
        assert commands["receiver_state"] == state
        assert commands["receiver_freshness"] == freshness
        assert commands["receiver_heartbeat_at"] == heartbeat
        assert "private" not in json.dumps(response)
        assert path.read_bytes() == before


def test_recent_command_history_is_bounded_private_and_shows_final_progress_update(monitor_client, monkeypatch):
    client, headers, root = monitor_client
    configure(root)
    path = ledger(root)
    monkeypatch.setattr(monitor_api.time, "time", lambda: NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE monitor_command_jobs (id TEXT PRIMARY KEY,actor TEXT,chat_id TEXT,"
                           "payload TEXT,state TEXT,result TEXT,created_at REAL,updated_at REAL)")
        for index in range(13):
            payload = {"action": "refill", "target": 6, "raw": "private-payload"} if index == 0 else {"action": "status"}
            result = {"phase": "reserve_refilled", "ready": 3, "message": "private-result-text",
                      "error": "private-raw-error", "actor": "private-nested-actor"}
            connection.execute("INSERT INTO monitor_command_jobs VALUES (?,?,?,?,?,?,?,?)", (
                f"{index:032x}", "private-owner-id", "private-chat-id", json.dumps(payload),
                "queued" if index == 0 else "done", json.dumps(result), NOW - index, NOW - index))
    before = path.read_bytes()
    response = client.get("/internal/editorial/monitor", headers=headers).json()
    history = response["commands"]["recent"]
    assert response["commands"]["history_available"] is True
    assert len(history) == 10
    assert history[0] == {"command_id": "0" * 32, "action": "refill", "state": "queued",
        "phase": "reserve_refilled", "ready": 3, "goal": 6, "created_at": NOW, "updated_at": NOW}
    assert history[-1]["command_id"] == f"{9:032x}"
    assert "private" not in json.dumps(response)
    assert not any(key in history[0] for key in ("actor", "chat_id", "payload", "result", "message", "error"))
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE monitor_command_jobs SET state='done',result=?,updated_at=? WHERE id=?",
            (json.dumps({"phase": "reserve_satisfied", "ready": 6, "message": "private-final-result"}),
             NOW + 1, "0" * 32))
    final_before = path.read_bytes()
    final = client.get("/internal/editorial/monitor", headers=headers).json()
    latest = final["commands"]["recent"][0]
    assert latest["state"] == "done" and latest["phase"] == "reserve_satisfied"
    assert latest["ready"] == latest["goal"] == 6
    assert len(final["commands"]["recent"]) == 10
    assert final["reports"]["last"]["state"] == "unknown"
    assert "private" not in json.dumps(final)
    assert path.read_bytes() == final_before


def test_command_history_rejects_untrusted_fields_and_invalid_counters():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE monitor_command_jobs(id TEXT,state TEXT,payload TEXT,result TEXT,"
                           "created_at REAL,updated_at REAL)")
        values = [
            ("private-invalid-id", "private-state", {"action": ["private-action"]}, {"phase": "private-phase"}),
            ("1" * 32, "done", {"action": "refill", "target": True}, {"ready": True}),
            ("2" * 32, "queued", {"action": "refill", "target": 99}, {"ready": -1}),
            ("3" * 32, "done", {"action": "status"}, {"ready": 6, "goal": 6}),
            ("4" * 32, "done", {"action": "reserve"}, {"reserve": {"ready": 5, "cookie": "private-cookie"}}),
            ("5" * 32, "cancelled", {"action": "refill", "target": 6},
             {"phase": "cancelled", "ready": 2, "message": "private-cancellation-details"}),
        ]
        connection.executemany("INSERT INTO monitor_command_jobs VALUES (?,?,?,?,?,?)", [
            (ident, state, json.dumps(payload), json.dumps(result), NOW, NOW)
            for ident, state, payload, result in values])
        available, history = monitor_api._command_history(connection)
        assert available is True and len(history) == 6
        assert "private" not in json.dumps(history)
        by_id = {item["command_id"]: item for item in history}
        assert by_id["unknown"]["action"] == by_id["unknown"]["state"] == "unknown"
        assert by_id["unknown"]["phase"] is None
        for ident in ("1" * 32, "2" * 32, "3" * 32):
            assert "ready" not in by_id[ident] and "goal" not in by_id[ident]
        assert by_id["4" * 32]["ready"] == 5
        assert by_id["5" * 32]["state"] == by_id["5" * 32]["phase"] == "cancelled"
        assert by_id["5" * 32]["ready"] == 2 and by_id["5" * 32]["goal"] == 6
    finally:
        connection.close()


def test_heartbeat_missing_instance_or_stale_observer_is_never_fake_healthy(monitor_client, monkeypatch):
    client, headers, root = monitor_client
    configure(root)
    ledger(root, instance="not-a-uuid", observer={"heartbeat_at": NOW - 600,
                                               "source_collected_at": NOW - 600})
    monkeypatch.setattr(monitor_api.time, "time", lambda: NOW)
    result = client.get("/internal/editorial/monitor-heartbeat", headers=headers).json()
    assert result["available"] is False
    assert result["freshness"] == "stale"
    assert result["instance_id"] is None


def test_corrupt_ledger_does_not_leak_sqlite_paths_or_trigger_initialization(monitor_client):
    client, headers, root = monitor_client
    configure(root)
    path = root / "data/monitor/monitor.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private-invalid-database-contents")
    response = client.get("/internal/editorial/monitor", headers=headers).json()
    assert response["ledger_available"] is False
    assert response["ledger_reason"] == "MONITOR_LEDGER_UNAVAILABLE"
    assert "private" not in json.dumps(response)
    assert path.read_bytes() == b"private-invalid-database-contents"


def test_production_root_status_fails_closed_before_opening_any_ledger(tmp_path, monkeypatch):
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError("production root must not be opened")
    monkeypatch.setattr(monitor_api, "_read_ledger", forbidden)
    assert monitor_api.monitor_status(core)["available"] is False


def test_preview_is_only_a_read_never_constructs_or_sends_to_outbox(monitor_client, monkeypatch):
    client, headers, root = monitor_client
    configure(root)
    before = set(root.rglob("*"))
    calls = []
    def collect(core):
        calls.append(core)
        return {"collected_at": NOW, "editorial": {"paused": True},
                "routes": {"targets": [], "dispatches": [], "pagination": {"unresolved_complete": True}}}
    monkeypatch.setattr(monitor_api, "collect_snapshot", collect)
    result = client.get("/internal/editorial/monitor-preview", headers=headers).json()
    assert result["available"] is True and result["read_only"] is True
    assert result["delivery_verified"] is False and result["external_mutations"] == 0
    assert "новой сверки площадок не выполнялось" in result["text"]
    assert calls and set(root.rglob("*")) == before
    assert not (root / "data/monitor").exists()


@pytest.mark.parametrize("route", ["monitor", "monitor-preview", "monitor-heartbeat"])
def test_status_endpoints_do_not_accept_actions_or_mutations(monitor_client, route):
    client, headers, _ = monitor_client
    assert client.get("/internal/editorial/" + route + "?action=send", headers=headers).status_code == 422
    assert client.post("/internal/editorial/" + route, headers=headers, json={"action": "send"}).status_code == 405
