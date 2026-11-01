"""Отдельный durable outbox наблюдателя, не редакционная очередь публикаций."""

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager

from .config import AgentError, fingerprint
from .monitor_config import private_path


class MonitorStore:
    def __init__(self, root):
        self.root = root
        self.path = private_path(root, "data/monitor/monitor.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_reports (
                    id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                    config_hash TEXT NOT NULL, chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', token TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL, evidence TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS monitor_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO monitor_meta(key,value) VALUES ('instance_id',?)",
                       (json.dumps(str(uuid.uuid4())),))

    @contextmanager
    def connection(self):
        path = private_path(self.root, "data/monitor/monitor.sqlite")
        for suffix in ("-wal", "-shm", "-journal"):
            private_path(self.root, "data/monitor/monitor.sqlite" + suffix)
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, key, kind, text, config_hash, chat_id, *, at, expires_at):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise AgentError("MONITOR_REPORT_INVALID")
        ident = "mon-" + fingerprint({"key": key, "config": config_hash})[:24]
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO monitor_reports "
                "(id,event_key,config_hash,chat_id,kind,text,text_hash,created_at,updated_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", (ident, key, config_hash, chat_id, kind, text,
                                              fingerprint(text), at, at, expires_at))
            row = db.execute("SELECT * FROM monitor_reports WHERE event_key=?", (key,)).fetchone()
            return dict(row)

    def claim(self, config_hash, chat_id, *, at):
        chats = (chat_id,) if isinstance(chat_id, str) else chat_id
        if (not isinstance(chats, tuple) or not 1 <= len(chats) <= 2
                or any(not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,22}", value) for value in chats)
                or len(set(chats)) != len(chats)):
            raise AgentError("MONITOR_DESTINATION_INVALID")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Смена канала или добавление управляющего чата не обходят unknown.
            if db.execute("SELECT id FROM monitor_reports WHERE "
                          "state IN ('sending','unknown') LIMIT 1").fetchone():
                return None
            db.execute("UPDATE monitor_reports SET state='expired',updated_at=? "
                       "WHERE state='pending' AND expires_at<=?", (at, at))
            db.execute("UPDATE monitor_reports SET state='cancelled',updated_at=? "
                       "WHERE state='pending' AND config_hash!=?", (at, config_hash))
            row = db.execute("SELECT * FROM monitor_reports WHERE state='pending' "
                             "AND config_hash=? AND chat_id IN (?,?) ORDER BY created_at,id LIMIT 1",
                             (config_hash, chats[0], chats[-1])).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE monitor_reports SET state='sending',token=?,updated_at=? WHERE id=?",
                       (token, at, row["id"]))
            return {**dict(row), "state": "sending", "token": token}

    def authorized(self, row):
        current = self.get(row["id"])
        return (current is not None and current["state"] == "sending"
                and current["token"] == row["token"]
                and current["text_hash"] == fingerprint(row["text"])
                and current["text"] == row["text"]
                and current["chat_id"] == row["chat_id"]
                and current["kind"] == row["kind"]
                and current["expires_at"] == row["expires_at"]
                and current["config_hash"] == row["config_hash"])

    def finish(self, row, state, evidence, *, at):
        if state not in {"confirmed", "unknown", "failed"}:
            raise AgentError("MONITOR_REPORT_STATE_INVALID")
        # Транспортные ошибки не включают stderr, cookies, URL или исходные сообщения.
        safe = {"state": state}
        if evidence.get("verification") == "browser_readback":
            safe["verification"] = "browser_readback"
        if type(evidence.get("independent_readback")) is bool:
            safe["independent_readback"] = evidence["independent_readback"]
        with self.connection() as db:
            changed = db.execute("UPDATE monitor_reports SET state=?,evidence=?,updated_at=? "
                "WHERE id=? AND token=? AND state IN ('sending','unknown')",
                (state, json.dumps(safe), at, row["id"], row["token"])).rowcount
            if changed != 1:
                raise AgentError("MONITOR_REPORT_CLAIM_LOST")

    def get(self, ident):
        with self.connection() as db:
            row = db.execute("SELECT * FROM monitor_reports WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def recent(self, limit=20):
        with self.connection() as db:
            rows = db.execute("SELECT id,kind,state,created_at,updated_at,chat_id,evidence "
                              "FROM monitor_reports ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in rows]

    def unresolved(self):
        with self.connection() as db:
            return db.execute("SELECT id FROM monitor_reports WHERE state IN ('sending','unknown') LIMIT 1").fetchone() is not None

    def meta(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM monitor_meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connection() as db:
            db.execute("INSERT INTO monitor_meta(key,value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))
