"""Общая пауза, настройки и единственный владелец серверного цикла."""
import copy
import json
import os
import threading
from contextlib import contextmanager

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from .browser_runtime import profile_lock
from .config import AgentError, fingerprint
from .db import now
from .server_models import EditorialControl, EditorialLedger

CONTROL_ID = "main"
LEDGER_ID = "hourly"
WORKER_LOCK = 599916633322113


def default_settings():
    return {"source": "nasa_svs", "interval_seconds": 3600,
        "reserve": {"enabled": False, "target": 6, "low": 3, "maximum": 12, "batch": 3},
        "nasa": {"authors": [], "keywords": [], "min_rating": 0,
                 "max_candidates": 30, "max_words": 300},
        "reddit": {"subreddits": [], "authors": [], "keywords": [], "min_score": 0,
                   "min_comments": 0, "max_age_hours": 72, "limit": 25, "sort": "top",
                   "time_filter": "week", "max_pages": 3}}


def validate_settings(value):
    # Старые настройки читаются без включения новой политики задним числом.
    if isinstance(value, dict) and set(value) == set(default_settings()) - {"reserve"}:
        value = {**value, "reserve": default_settings()["reserve"]}
    if (not isinstance(value, dict) or set(value) != set(default_settings())
            or value["source"] not in {"nasa_svs", "open_science", "reddit"}
            or type(value["interval_seconds"]) is not int
            or not 3600 <= value["interval_seconds"] <= 86400):
        raise AgentError("EDITORIAL_SETTINGS_INVALID", 422)
    nasa = value["nasa"]
    reserve = value["reserve"]
    if (not isinstance(reserve, dict) or set(reserve) != set(default_settings()["reserve"])
            or type(reserve["enabled"]) is not bool
            or any(type(reserve[k]) is not int for k in ("target", "low", "maximum", "batch"))
            or not 1 <= reserve["low"] < reserve["target"] <= reserve["maximum"] <= 24
            or not 1 <= reserve["batch"] <= 3):
        raise AgentError("EDITORIAL_SETTINGS_INVALID", 422)
    if (not isinstance(nasa, dict) or set(nasa) != set(default_settings()["nasa"])
            or any(not isinstance(nasa[k], list) or len(nasa[k]) > 50
                   or any(not isinstance(x, str) or not x.strip() or len(x) > 120 for x in nasa[k])
                   for k in ("authors", "keywords"))
            or type(nasa["min_rating"]) is not int or not 0 <= nasa["min_rating"] <= 1000000
            or type(nasa["max_candidates"]) is not int or not 1 <= nasa["max_candidates"] <= 100
            or type(nasa["max_words"]) is not int or not 80 <= nasa["max_words"] <= 1000):
        raise AgentError("EDITORIAL_SETTINGS_INVALID", 422)
    from .server_sources import normalize_reddit_settings
    result = copy.deepcopy(value)
    # Пустой неактивный профиль можно подготовить до получения доступа Reddit.
    result["reddit"] = normalize_reddit_settings(value["reddit"], require_scope=value["source"] == "reddit")
    return result


def require_running(core, session=None, *, lock=False):
    """После миграции общая пауза действует даже при прямом вызове старого CLI."""
    if session is None:
        with core.db.session() as own:
            return require_running(core, own, lock=lock)
    fence = getattr(core, "editorial_leader_check", None)
    if fence is not None:
        fence()
    row = (session.scalar(select(EditorialControl).where(EditorialControl.id == CONTROL_ID).with_for_update())
           if lock else session.get(EditorialControl, CONTROL_ID))
    if row is not None and row.paused:
        raise AgentError("EDITORIAL_PAUSED", 409)


class ServerState:
    def __init__(self, core):
        self.core = core

    def initialize(self):
        try:
            with self.core.db.session.begin() as session:
                if session.get(EditorialControl, CONTROL_ID) is None:
                    session.add(EditorialControl(id=CONTROL_ID, paused=True, revision=1,
                        settings=default_settings(), runtime={"phase": "paused"}))
        except IntegrityError:
            # Параллельный первый запуск уже создал ту же единственную запись.
            pass
        return self.read()

    def read(self):
        with self.core.db.session() as session:
            row = session.get(EditorialControl, CONTROL_ID)
            if row is None:
                raise AgentError("EDITORIAL_NOT_INITIALIZED", 409)
            return copy.deepcopy({"paused": row.paused, "revision": row.revision,
                "settings": row.settings, "runtime": row.runtime, "updated_at": row.updated_at})

    def phase(self, phase, **details):
        with self.core.db.session.begin() as session:
            row = session.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            if row is None:
                raise AgentError("EDITORIAL_NOT_INITIALIZED", 409)
            # Рабочие отметки не меняют revision формы настроек и управления.
            row.runtime = {**row.runtime, **details, "phase": phase, "heartbeat_at": now()}

    def assert_running(self):
        require_running(self.core)

    def progress(self, channel, phase, **details):
        """Подготовка обновляет свою строку статуса, не затирая состояние отправки."""
        if channel != "preparation":
            raise AgentError("EDITORIAL_PROGRESS_INVALID", 422)
        with self.core.db.session.begin() as session:
            row = session.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            if row is None:
                raise AgentError("EDITORIAL_NOT_INITIALIZED", 409)
            previous = row.runtime.get(channel, {})
            row.runtime = {**row.runtime, channel: {**previous, **details, "phase": phase, "heartbeat_at": now()}}

    def control(self, action, revision, actor):
        if action not in {"start", "pause"}:
            raise AgentError("EDITORIAL_CONTROL_INVALID", 422)
        with self.core.db.session.begin() as session:
            row = session.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            if row is None or row.revision != revision:
                raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
            if action == "start":
                from .server_maintenance import guard_restart_operation
                guard_restart_operation(self.core)
                self.core._assert_current_route_runtime()
            row.paused, row.revision = action == "pause", row.revision + 1
            row.updated_at, row.updated_by = now(), actor
            if row.paused:
                from .hub_models import Target
                for target in session.scalars(select(Target).with_for_update()):
                    target.paused = True
            self.core.db.audit(session, actor, "editorial_control", CONTROL_ID,
                               action_requested=action, revision=row.revision)
        return self.read()

    def settings(self, value, revision, actor):
        value = validate_settings(value)
        with self.core.db.session.begin() as session:
            row = session.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            if row is None or row.revision != revision:
                raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
            if not row.paused:
                raise AgentError("EDITORIAL_PAUSE_BEFORE_SETTINGS", 409)
            row.settings, row.revision = value, row.revision + 1
            row.updated_at, row.updated_by = now(), actor
            self.core.db.audit(session, actor, "editorial_settings", CONTROL_ID,
                               revision=row.revision, settings_hash=fingerprint(value))
        return self.read()

    @contextmanager
    def worker_lock(self):
        """Блокировка соединения не истекает во время долгого запроса модели."""
        if self.core.db.engine.dialect.name != "postgresql":
            with profile_lock(self.core.settings.root / "data/editorial-worker.lock"):
                yield True
            return
        with self.core.db.engine.connect() as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
            connection.commit()
            backend = connection.scalar(text("SELECT pg_backend_pid()")) if acquired else None
            connection.commit()
            mutex = threading.RLock()

            def check_owner():
                # Переподключение после потери БД не восстанавливает прежнее право отправки.
                with mutex:
                    if connection.invalidated or connection.scalar(text("SELECT pg_backend_pid()")) != backend:
                        raise AgentError("EDITORIAL_LEADERSHIP_LOST", 409)
                    held = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE "
                        "locktype='advisory' AND pid=pg_backend_pid() AND classid=:high "
                        "AND objid=:low AND objsubid=1 AND granted)"),
                        {"high": WORKER_LOCK >> 32, "low": WORKER_LOCK & 0xffffffff})
                    connection.commit()
                    if not held:
                        raise AgentError("EDITORIAL_LEADERSHIP_LOST", 409)

            if acquired:
                self.core.editorial_leader_check = check_owner
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    # Даже переживший остановку поток больше не сможет менять remote state.
                    def revoked():
                        raise AgentError("EDITORIAL_LEADERSHIP_LOST", 409)
                    self.core.editorial_leader_check = revoked
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
                    connection.commit()


class DatabaseArticleLedger:
    """CAS сохраняет весь исторический документ и запрещает потерю чужой записи."""
    def __init__(self, cycle):
        self.cycle = cycle
        self.marker = cycle.directory / "postgresql-authoritative.local.json"

    def load(self):
        with self.cycle.core.db.session() as session:
            row = session.get(EditorialLedger, LEDGER_ID)
            if row is None:
                if self.marker.exists():
                    raise AgentError("ARTICLE_LEDGER_DATABASE_MISSING", 409)
                return None
            self.cycle._ledger_revision = row.revision
            value = copy.deepcopy(row.payload)
            if getattr(self.cycle, "reserve_mode", False):
                # Каждому снимку своя база: вложенный _load не подменяет revision
                # внешнего вызова, который ещё ждёт долгий ответ модели.
                bases = getattr(self.cycle, "_reserve_ledger_bases", {})
                bases[id(value)] = (value, row.revision, copy.deepcopy(value))
                self.cycle._reserve_ledger_bases = bases
                while len(bases) > 64:
                    del bases[next(iter(bases))]
            return value

    def save(self, payload):
        if getattr(self.cycle, "reserve_mode", False):
            return self._save_reserve(payload)
        with self.cycle.core.db.session.begin() as session:
            row = session.get(EditorialLedger, LEDGER_ID)
            if row is None:
                if self.marker.exists():
                    raise AgentError("ARTICLE_LEDGER_DATABASE_MISSING", 409)
                return False
            revision = getattr(self.cycle, "_ledger_revision", row.revision)
            result = session.execute(update(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID, EditorialLedger.revision == revision).values(
                    payload=copy.deepcopy(payload), revision=revision + 1, updated_at=now()))
            if result.rowcount != 1:
                raise AgentError("ARTICLE_LEDGER_REVISION_CONFLICT", 409)
            self.cycle._ledger_revision = revision + 1
        return True

    def _save_reserve(self, payload):
        """Сливать только независимые записи; изменение чужой версии запрещено."""
        bases = getattr(self.cycle, "_reserve_ledger_bases", {})
        loaded = bases.get(id(payload))
        with self.cycle.core.db.session.begin() as session:
            row = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            if row is None:
                if self.marker.exists():
                    raise AgentError("ARTICLE_LEDGER_DATABASE_MISSING", 409)
                return False
            if loaded is None or loaded[0] is not payload:
                raise AgentError("ARTICLE_LEDGER_REVISION_CONFLICT", 409)
            _, _, base = loaded
            merged = self._merge_records(base, payload, row.payload)
            saved_revision = row.revision + 1
            result = session.execute(update(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID, EditorialLedger.revision == row.revision).values(
                    payload=copy.deepcopy(merged), revision=saved_revision, updated_at=now()))
            if result.rowcount != 1:
                raise AgentError("ARTICLE_LEDGER_REVISION_CONFLICT", 409)
        # Сохраняем ссылки на локально изменённые records, которыми ещё пользуется
        # вызывающий код. Чужие новые/изменённые records добавляются свежими копиями.
        payload["records"][:] = merged["records"]
        self.cycle._ledger_revision = saved_revision
        bases[id(payload)] = (payload, saved_revision, copy.deepcopy(payload))
        return True

    @staticmethod
    def _merge_records(base, proposed, current):
        def records(value):
            if (not isinstance(value, dict) or set(value) != {"records"}
                    or not isinstance(value["records"], list)
                    or any(not isinstance(r, dict) or not isinstance(r.get("id"), str)
                           for r in value["records"])):
                raise AgentError("ARTICLE_LEDGER_INVALID", 409)
            result = {r["id"]: r for r in value["records"]}
            if len(result) != len(value["records"]):
                raise AgentError("ARTICLE_LEDGER_INVALID", 409)
            return result
        before, after, latest = records(base), records(proposed), records(current)
        if not set(before) <= set(after) or not set(before) <= set(latest):
            raise AgentError("ARTICLE_LEDGER_REVISION_CONFLICT", 409)
        changed = {key for key in after if key not in before or after[key] != before[key]}
        for key in changed:
            if (key in before and latest.get(key) != before[key]
                    or key not in before and key in latest):
                raise AgentError("ARTICLE_LEDGER_REVISION_CONFLICT", 409)
        merged = []
        for item in current["records"]:
            key = item["id"]
            if key in changed or key in after and after[key] == item:
                merged.append(after[key])
            else:
                merged.append(copy.deepcopy(item))
        merged.extend(after[key] for key in after if key not in latest)
        return {"records": merged}

    def migrate(self):
        with profile_lock(self.cycle.directory / "cycle.lock"):
            existing = self.load()
            if existing is not None:
                self._ensure_marker({"records": len(existing["records"]),
                                     "hash": fingerprint(existing), "at": now(), "recovered": True})
                return {"already_migrated": True, "records": len(existing["records"]),
                        "hash": fingerprint(existing)}
            payload = self.cycle._load()
            # Исходный JSON остаётся неизменяемой копией, а не вторым писателем.
            proof = {"records": len(payload["records"]), "hash": fingerprint(payload), "at": now()}
            with self.cycle.core.db.session.begin() as session:
                session.add(EditorialLedger(id=LEDGER_ID, revision=1, payload=copy.deepcopy(payload)))
                self.cycle.core.db.audit(session, "migration", "article_ledger_to_postgresql", LEDGER_ID, **proof)
            self._ensure_marker(proof)
            self.cycle._ledger_revision = 1
            return proof

    def _ensure_marker(self, proof):
        if not self.marker.exists():
            descriptor = os.open(self.marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(proof, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        if os.name != "nt":
            descriptor = os.open(self.cycle.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
