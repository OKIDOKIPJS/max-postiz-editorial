"""Закрытый просмотр наблюдателя в Postiz: GET не создаёт outbox или браузер."""

import json
import re
import sqlite3
import time
import uuid

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from .config import AgentError
from .isolated_workspace import assert_isolated
from .monitor_config import COMMAND_ACTIONS, MonitorConfig, private_path
from .monitor_reports import FRESH_SECONDS, _ident, _stamp, collect_snapshot, render_report

_REPORT_STATES = {"pending", "sending", "unknown", "confirmed", "failed", "cancelled", "expired"}
_REPORT_KINDS = {"hourly", "daily", "incident", "command"}
_COMMAND_STATES = {"queued", "running", "done", "blocked", "unknown", "cancelled"}
_COMMAND_PHASES = {"paused", "reserve_disabled", "blocked", "worker_busy", "waiting_source",
    "reserve_satisfied", "reserve_refilled", "no_source", "preparation_deferred", "selecting",
    "translating", "reviewing", "repairing", "preparing", "quality_ready", "reserve_empty", "waiting_interval",
    "waiting_services", "cancelled", "cancellation_requested"}


def _unavailable(reason):
    return {"available": False, "read_only": True, "reason": reason,
            "external_mutations": 0, "interface": "postiz_only"}


def _config(core):
    try:
        assert_isolated(core.settings.root)
        return MonitorConfig.load(core.settings.root)
    except (AgentError, OSError, ValueError, TypeError):
        return None


def _object(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _command_history(connection):
    """Только последние состояния задач, без авторов, адресатов и исходного текста."""
    if connection.execute("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='monitor_command_jobs'").fetchone() is None:
        return False, []
    recent = []
    for row in connection.execute("SELECT id,state,created_at,updated_at,"
            "CASE WHEN typeof(payload)='text' AND length(payload)<=8192 THEN payload ELSE '{}' END AS payload,"
            "CASE WHEN typeof(result)='text' AND length(result)<=8192 THEN result ELSE '{}' END AS result "
            "FROM monitor_command_jobs ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT 10"):
        payload, result = _object(row["payload"]), _object(row["result"])
        action = payload.get("action")
        action = action if isinstance(action, str) and action in COMMAND_ACTIONS else "unknown"
        phase = result.get("phase")
        summary = {"command_id": row["id"] if isinstance(row["id"], str)
                and re.fullmatch(r"[a-f0-9]{32}", row["id"]) else "unknown",
            "action": action, "state": row["state"] if row["state"] in _COMMAND_STATES else "unknown",
            "phase": phase if isinstance(phase, str) and phase in _COMMAND_PHASES else None,
            "created_at": _stamp(row["created_at"]), "updated_at": _stamp(row["updated_at"])}
        ready = result.get("ready")
        if action == "reserve" and isinstance(result.get("reserve"), dict):
            ready = result["reserve"].get("ready")
        if action in {"refill", "reserve"} and type(ready) is int and 0 <= ready <= 24:
            summary["ready"] = ready
        goal = payload.get("target")
        if action == "refill" and type(goal) is int and 1 <= goal <= 12:
            summary["goal"] = goal
        recent.append(summary)
    return True, recent


def _read_ledger(root):
    """mode=ro и authorizer запрещают даже случайный UPDATE при просмотре."""
    path = private_path(root, "data/monitor/monitor.sqlite")
    for suffix in ("-wal", "-shm", "-journal"):
        private_path(root, "data/monitor/monitor.sqlite" + suffix)
    if not path.is_file():
        return _unavailable("MONITOR_LEDGER_ABSENT")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
        deadline = time.monotonic() + 1
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        meta = {}
        for row in connection.execute("SELECT key,value FROM monitor_meta "
                "WHERE key IN ('observer','sender','instance_id','command_receiver') AND length(value)<=8192"):
            meta[row["key"]] = json.loads(row["value"])
        counts = {row["state"]: row["amount"] for row in connection.execute(
            "SELECT state,count(*) AS amount FROM monitor_reports GROUP BY state") if row["state"] in _REPORT_STATES}
        row = connection.execute("SELECT id,kind,state,created_at,updated_at FROM monitor_reports "
                                 "ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
        last = None
        if row is not None:
            last = {"id": _ident(row["id"]), "kind": row["kind"] if row["kind"] in _REPORT_KINDS else "unknown",
                "state": row["state"] if row["state"] in _REPORT_STATES else "unknown",
                "created_at": _stamp(row["created_at"]), "updated_at": _stamp(row["updated_at"])}
        history_available, history = _command_history(connection)
        observer = meta.get("observer") if isinstance(meta.get("observer"), dict) else {}
        clean = {key: _stamp(observer.get(key)) for key in (
            "heartbeat_at", "source_collected_at", "source_heartbeat_at")}
        clean["source_paused"] = observer.get("source_paused") if type(observer.get("source_paused")) is bool else None
        sender = meta.get("sender") if isinstance(meta.get("sender"), dict) else {}
        clean["sender_heartbeat_at"] = _stamp(sender.get("heartbeat_at"))
        clean["sender_state"] = sender.get("state") if isinstance(sender.get("state"), str) and sender["state"] in {
            "idle", "confirmed", "unknown", "blocked", "disabled"} else "unknown"
        receiver = meta.get("command_receiver") if isinstance(meta.get("command_receiver"), dict) else {}
        receiver_state = receiver.get("state")
        receiver = {"state": receiver_state if isinstance(receiver_state, str) and receiver_state in {
            "ready", "blocked"} else "unobserved", "heartbeat_at": _stamp(receiver.get("heartbeat_at"))}
        instance = meta.get("instance_id")
        try:
            instance = str(uuid.UUID(instance)) if isinstance(instance, str) else None
        except ValueError:
            instance = None
        return {"available": True, "observer": clean, "instance_id": instance, "command_receiver": receiver,
                "command_history_available": history_available, "command_history": history,
                "reports": {"counts": counts, "last": last}}
    finally:
        connection.close()


def monitor_status(core, *, now_at=None):
    config = _config(core)
    if config is None:
        return _unavailable("MONITOR_NOT_CONFIGURED")
    at = _stamp(time.time() if now_at is None else now_at)
    if at is None:
        raise AgentError("MONITOR_TIME_INVALID", 422)
    try:
        ledger = _read_ledger(core.settings.root)
    except (sqlite3.Error, AgentError, OSError, ValueError, TypeError):
        ledger = _unavailable("MONITOR_LEDGER_UNAVAILABLE")
    observer = ledger.get("observer", {})
    stamp = observer.get("heartbeat_at")
    source_stamp = observer.get("source_collected_at")
    fresh = all(value is not None and -30 <= at - value <= FRESH_SECONDS for value in (stamp, source_stamp))
    options = config.value
    commands = options["commands"]
    if commands.get("auth_mode") == "authenticated_account_outgoing":
        from .monitor_owner_chat import owner_contract_hash
        try:
            accepted_owner = (commands["dom_contract_verified"]
                and owner_contract_hash(commands["dom_contract"]) == commands["dom_contract_sha256"])
        except AgentError:
            accepted_owner = False
        paired = bool(commands["control_chat_id"] and commands.get("account_sha256")
            and accepted_owner)
    else:
        paired = bool(commands["control_chat_id"] and commands["account_user_id"] and commands["owner_ids"])
    receiver = ledger.get("command_receiver", {})
    receiver_stamp = receiver.get("heartbeat_at")
    receiver_fresh = receiver_stamp is not None and -30 <= at - receiver_stamp <= FRESH_SECONDS
    return {"available": True, "read_only": True, "interface": "postiz_only",
        "external_mutations": 0, "transport": "max_browser", "bot_api": False,
        "enabled": options["enabled"], "destination_configured": bool(options["report_chat_id"]
            and options["report_chat_name"].strip() and options["report_private_confirmed"]),
        "commands": {"enabled": bool(options["enabled"] and commands["enabled"]),
            "paired": paired,
            "contract_verified": commands["dom_contract_verified"], "owner_count": len(commands["owner_ids"]),
            "history_available": ledger.get("command_history_available", False),
            "recent": ledger.get("command_history", []),
            "receiver_state": receiver.get("state", "unobserved"), "receiver_heartbeat_at": receiver_stamp,
            "receiver_freshness": "fresh" if receiver_fresh else "stale" if receiver_stamp is not None else "unobserved"},
        "ledger_available": ledger["available"], "ledger_reason": ledger.get("reason"),
        "instance_id": ledger.get("instance_id"), "observer": observer,
        "freshness": "fresh" if fresh else "stale" if stamp is not None else "unobserved",
        "reports": ledger.get("reports", {"counts": {}, "last": None})}


def monitor_heartbeat(core, *, now_at=None):
    status = monitor_status(core, now_at=now_at)
    available = (status["available"] and status.get("ledger_available") is True
                 and status.get("instance_id") is not None
                 and status.get("observer", {}).get("heartbeat_at") is not None)
    return {"schema": "monitor-heartbeat-v1", "available": bool(available),
        "read_only": True, "instance_id": status.get("instance_id"),
        "enabled": status.get("enabled", False), "observer": status.get("observer", {}),
        "freshness": status.get("freshness", "unobserved"),
        "reason": status.get("reason") or status.get("ledger_reason"), "external_mutations": 0}


def attach_monitor_api(app, core, owner):
    owner_dependency = Depends(owner)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    def no_query(request):
        if request.query_params:
            raise AgentError("INPUT_INVALID", 422)

    @app.get("/internal/editorial/monitor")
    def status(request: Request, actor=owner_dependency):
        no_query(request)
        return response(monitor_status(core))

    @app.get("/internal/editorial/monitor-heartbeat")
    def heartbeat(request: Request, actor=owner_dependency):
        no_query(request)
        return response(monitor_heartbeat(core))

    @app.get("/internal/editorial/monitor-preview")
    def preview(request: Request, actor=owner_dependency):
        no_query(request)
        if _config(core) is None:
            return response(_unavailable("MONITOR_NOT_CONFIGURED"))
        snapshot = collect_snapshot(core)
        return response({"available": True, "read_only": True, "external_mutations": 0,
            "collected_at": snapshot["collected_at"], "delivery_verified": False,
            "text": render_report(snapshot, report_id="preview", kind="status")})
