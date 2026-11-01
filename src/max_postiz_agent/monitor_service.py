"""Периодические отчёты, изменения инцидентов и отдельная полоса MAX-команд."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .browser_runtime import profile_lock
from .config import AgentError, fingerprint
from .isolated_workspace import assert_isolated
from .monitor_config import private_path
from .monitor_store import MonitorStore

MOSCOW = ZoneInfo("Europe/Moscow")


class MonitorService:
    def __init__(self, core, config, *, store=None, transport=None, collector=None, clock=None):
        self.core, self.config = core, config
        self.store = store or MonitorStore(core.settings.root)
        self.transport = transport
        self.collector = collector
        self.clock = clock or time.time

    def _guard(self):
        assert_isolated(self.core.settings.root)
        if fingerprint(self.config.value) != self.config.hash or not self.config.current():
            raise AgentError("MONITOR_CONFIG_CHANGED")

    def _destination(self, kind):
        if not isinstance(kind, str) or kind not in {"hourly", "daily", "incident", "command", "watchdog"}:
            raise AgentError("MONITOR_REPORT_KIND_INVALID")
        options = self.config.value
        commands = options["commands"]
        if kind == "command" and commands.get("reply_in_control_chat") is True:
            return commands["control_chat_id"], commands["control_chat_name"]
        return options["report_chat_id"], options["report_chat_name"]

    def _allowed_destinations(self):
        return tuple(dict.fromkeys((self._destination("hourly")[0], self._destination("command")[0])))

    def _valid_destination(self, row):
        destination, name = self._destination(row["kind"])
        if row["config_hash"] != self.config.hash or row["chat_id"] != destination:
            raise AgentError("MONITOR_DESTINATION_CHANGED")
        return destination, name

    def _transport(self):
        if self.transport is None:
            from .monitor_browser import MonitorBrowser
            self.transport = MonitorBrowser(self.core.settings, self.config.browser_options())
        return self.transport

    def snapshot(self):
        from .monitor_reports import collect_snapshot
        self._guard()
        return (self.collector or collect_snapshot)(self.core, now_at=self.clock())

    def queue(self, key, kind, text, *, at=None, expires_at=None):
        self._guard()
        at = self.clock() if at is None else at
        options = self.config.value
        destination, _ = self._destination(kind)
        if options["panel_url"]:
            suffix = "\nПодробности: " + options["panel_url"]
            text = text[:4000 - len(suffix)] + suffix
        return self.store.enqueue(key + "@" + self.config.hash[:16], kind, text, self.config.hash,
            destination, at=at,
            expires_at=min(at + options["report_max_age_seconds"], expires_at)
                if expires_at is not None else at + options["report_max_age_seconds"])

    def produce(self):
        """Только текущий час/день: пропущенные периоды не порождают пачку отчётов."""
        from .monitor_reports import alert_signatures, render_report
        self._guard()
        at, options = self.clock(), self.config.value
        with profile_lock(private_path(self.core.settings.root, "data/monitor/producer.lock")):
            snapshot = self.snapshot()
            self.store.set_meta("observer", {"heartbeat_at": at, "config_hash": self.config.hash,
                "source_collected_at": snapshot.get("collected_at"),
                "source_heartbeat_at": snapshot.get("editorial", {}).get("heartbeat_at"),
                "source_paused": snapshot.get("editorial", {}).get("paused") is True})
            if not options["enabled"]:
                return {"state": "disabled", "queued": [], "external_mutations": 0}
            queued = []
            hour = int(at // 3600)
            if options["hourly_enabled"] and at % 3600 >= options["hourly_delay_seconds"]:
                key = "hourly:" + str(hour)
                report_id = fingerprint({"key": key, "config": self.config.hash})[:12]
                text = render_report(snapshot, report_id=report_id, now_at=at, kind="hourly")
                queued.append(self.queue(key, "hourly", text, at=at,
                                         expires_at=(hour + 1) * 3600)["id"])
            local = datetime.fromtimestamp(at, MOSCOW)
            if options["daily_enabled"] and local.hour >= options["daily_hour"]:
                key = "daily:" + local.strftime("%Y-%m-%d")
                report_id = fingerprint({"key": key, "config": self.config.hash})[:12]
                text = render_report(snapshot, report_id=report_id, now_at=at, kind="daily")
                queued.append(self.queue(key, "daily", text, at=at)["id"])
            current = alert_signatures(snapshot)
            receiver = self.store.meta("command_receiver", {})
            if options["commands"]["enabled"] and (receiver.get("state") == "blocked"
                    or receiver.get("state") == "ready"
                    and at - receiver.get("heartbeat_at", 0) > 300):
                current["inc-command-receiver"] = (
                    "Приём команд MAX остановлен или устарел; проверьте вход и DOM-контракт")
            previous = self.store.meta("incidents", {})
            opened = {k: v for k, v in current.items() if k not in previous}
            closed = {k: v for k, v in previous.items() if k not in current}
            if opened or closed:
                generation = self.store.meta("incident_generation", 0) + 1
                lines = ["Состояние редакции изменилось", local.strftime("%d.%m.%Y %H:%M МСК")]
                lines += ["⚠ " + value for value in opened.values()]
                # Снятие сигнала не подтверждает доставку и не возобновляет площадку.
                lines += ["Сигнал больше не наблюдается: " + value for value in closed.values()]
                lines.append("Данные из сохранённого состояния; сверка площадок выполняется отдельно.")
                queued.append(self.queue("incident:" + str(generation), "incident",
                                         "\n".join(lines)[:4000], at=at)["id"])
                self.store.set_meta("incident_generation", generation)
            self.store.set_meta("incidents", current)
            return {"state": "observed", "queued": queued, "external_mutations": 0}

    @staticmethod
    def _confirmed(result):
        return (isinstance(result, dict) and result.get("state") == "confirmed"
                and result.get("independent_readback") is True
                and result.get("verification") == "browser_readback")

    def deliver_once(self):
        self._guard()
        if not self.config.value["enabled"]:
            self.store.set_meta("sender", {"heartbeat_at": self.clock(), "state": "disabled"})
            return {"state": "disabled", "external_mutations": 0}
        with profile_lock(private_path(self.core.settings.root, "data/monitor/sender.lock")), \
                profile_lock(private_path(self.core.settings.root, "data/monitor/transport.lock")):
            row = self.store.claim(self.config.hash, self._allowed_destinations(), at=self.clock())
            if row is None:
                self.store.set_meta("sender", {"heartbeat_at": self.clock(),
                    "state": "blocked" if self.store.unresolved() else "idle"})
                return {"state": "idle_or_blocked", "external_mutations": 0}
            try:
                _, expected_name = self._valid_destination(row)
            except AgentError:
                self.store.finish(row, "failed", {"state": "preclick_failed"}, at=self.clock())
                raise
            commands = self.config.value["commands"]
            expected_kind = ("solo_group" if commands.get("reply_in_control_chat") is True
                             and row["chat_id"] == commands["control_chat_id"] else "channel")

            def authorize(envelope):
                self._guard()
                self._valid_destination(row)
                return (self.store.authorized(row) and row["expires_at"] > self.clock()
                        and envelope.get("request_id") == row["id"]
                        and envelope.get("chat_id") == row["chat_id"]
                        and envelope.get("chat_name", expected_name) == expected_name
                        and envelope.get("destination_kind", expected_kind) == expected_kind
                        and envelope.get("text") == row["text"])

            try:
                result = self._transport().send_report(row["chat_id"], row["text"], row["id"],
                                                       authorize=authorize)
            except Exception:
                result = {"state": "unknown"}
            if not isinstance(result, dict):
                result = {"state": "unknown"}
            state = ("confirmed" if self._confirmed(result) else
                     "failed" if result.get("state") == "preclick_failed" else "unknown")
            self.store.finish(row, state, result, at=self.clock())
            self.store.set_meta("sender", {"heartbeat_at": self.clock(), "state": state})
            return {"id": row["id"], "state": state,
                    "delivery_verified": state == "confirmed"}

    def reconcile(self, ident):
        self._guard()
        with profile_lock(private_path(self.core.settings.root, "data/monitor/sender.lock")), \
                profile_lock(private_path(self.core.settings.root, "data/monitor/transport.lock")):
            row = self.store.get(ident)
            if row is None or row["state"] not in {"sending", "unknown"}:
                raise AgentError("MONITOR_RECONCILE_STATE_INVALID")
            self._valid_destination(row)
            result = self._transport().reconcile_report(row["chat_id"], row["id"], text=row["text"])
            if not isinstance(result, dict):
                result = {"state": "unknown"}
            state = "confirmed" if self._confirmed(result) else "unknown"
            self.store.finish(row, state, result, at=self.clock())
            return {"id": ident, "state": state, "read_only": True}

    def gateway(self):
        from .monitor_commands import CommandGateway
        return CommandGateway(self.core, self.config.command_options(), self.store.path)

    def receive_once(self):
        self._guard()
        if not self.config.command_options()["enabled"]:
            return {"state": "disabled", "accepted": 0}
        previous = self.store.meta("command_receiver", {})
        if previous.get("requires_owner") is True and previous.get("pairing_hash") == self.config.hash:
            return {"state": "blocked", "accepted": 0, "requires_owner": True}
        with profile_lock(private_path(self.core.settings.root, "data/monitor/receiver.lock")):
            try:
                with profile_lock(private_path(self.core.settings.root, "data/monitor/transport.lock")):
                    events = self._transport().read_commands()
            except AgentError as error:
                if error.code == "BROWSER_PROFILE_BUSY":
                    return {"state": "busy", "accepted": 0}
                self.store.set_meta("command_receiver", {"heartbeat_at": self.clock(),
                    "state": "blocked", "error": "MONITOR_COMMAND_RECEIVE_UNAVAILABLE",
                    "pairing_hash": self.config.hash,
                    "requires_owner": any(marker in error.code for marker in (
                        "RATE_LIMIT", "CAPTCHA", "ACCOUNT_MISMATCH", "WRONG_CHAT", "NOT_SOLO",
                        "MONITOR_OWNER_DOM_INVALID", "MONITOR_OWNER_CHAT_CONTEXT_INVALID"))})
                raise
            except Exception as error:
                self.store.set_meta("command_receiver", {"heartbeat_at": self.clock(),
                    "state": "blocked", "error": "MONITOR_COMMAND_RECEIVE_UNAVAILABLE"})
                raise AgentError("MONITOR_COMMAND_RECEIVE_UNAVAILABLE") from error
            self.store.set_meta("command_receiver", {"heartbeat_at": self.clock(), "state": "ready"})
            cutoff = self.store.meta("commands_started_at")
            if cutoff is None or self.store.meta("commands_pairing_hash") != self.config.hash:
                # Первое чтение создаёт границу. Видимая история не становится поручением.
                self.store.set_meta("commands_baseline_ids", [e["message_id"] for e in events])
                self.store.set_meta("commands_started_at", self.clock())
                self.store.set_meta("commands_pairing_hash", self.config.hash)
                return {"state": "primed", "accepted": 0}
            baseline = self.store.meta("commands_baseline_ids", [])
            results = []
            for event in events:
                if event["message_id"] in baseline or event["created_at"] < cutoff:
                    continue
                try:
                    result = self.gateway().handle(event, now_at=self.clock())
                except AgentError:
                    # Чужое сообщение не вызывает ответа и не выдаёт приватные сведения.
                    continue
                if result.get("duplicate") is True:
                    continue
                results.append(result)
                self._queue_command_result(result, "accepted:" + event["message_id"])
            return {"state": "received", "accepted": len(results)}

    def _queue_command_result(self, result, key):
        # Сам gateway возвращает безопасный текст. Никогда не печатаем входное сообщение.
        text = result.get("message")
        if not isinstance(text, str):
            text = "Команда обработана. Состояние: " + str(result.get("state", "blocked"))
        ident = str(result.get("command_id", result.get("id", result.get("job_id", ""))))[:80]
        self.queue("command:" + key, "command", ("Управление тестовой копией\nЗадача " + ident + "\n" + text)[:4000])

    def run_command_once(self, *, worker_owned=False):
        self._guard()
        if not self.config.command_options()["enabled"]:
            return {"state": "disabled"}
        if not worker_owned and self.core.db.engine.dialect.name == "postgresql":
            # Единственный серверный preparer потребляет эту же очередь. Наблюдатель
            # не делает claim, который живой leader мог бы принять за потерянный.
            return {"state": "waiting_worker"}
        result = self.gateway().run_one(now_at=self.clock(), worker_owned=worker_owned)
        if result and result.get("state") not in {"idle", "disabled", "busy", "paused", "waiting_worker"}:
            ident = str(result.get("command_id", result.get("id", result.get("job_id", ""))))
            # О промежуточном и конечном результате сообщаем отдельно, одинаковые
            # наблюдения одного queued/unknown состояния не создают новый отчёт.
            key = fingerprint({"id": ident, "state": result.get("state"),
                               "phase": result.get("phase"), "ready": result.get("ready")})
            self._queue_command_result(result, "result:" + key)
        return result

    def status(self):
        return {"enabled": self.config.value["enabled"],
                "commands_enabled": self.config.command_options()["enabled"],
                "observer": self.store.meta("observer", {}), "reports": self.store.recent(),
                "production_access": False, "interface": "postiz_only",
                "transport": "max_browser", "bot_api": False}
