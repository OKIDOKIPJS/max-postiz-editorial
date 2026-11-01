"""Независимая проверка одного health URL без переходов, повторов и приватных ответов.

Для обнаружения выключения VM процесс размещают на другой машине. Отказ всей
домашней сети обнаруживается наблюдателем вне этой сети. Модуль не запускает
браузер и не выполняет редакционные действия.
"""

import math
import re
import time
from urllib.parse import urlsplit

import httpx

from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, parse_json
from .monitor_config import private_path


HEARTBEAT_SCHEMA = "monitor-heartbeat-v1"
MAX_HEALTH_BYTES = 65_536
_STATES = {"healthy", "stale", "unavailable", "invalid"}
_SENDERS = {"idle", "confirmed", "unknown", "blocked", "disabled"}
_MESSAGES = {
    "healthy": "Мониторинг доступен. Обязательные сигналы состояния актуальны.",
    "timeout": "Сервер мониторинга не ответил за отведённое время.",
    "network_error": "Не удалось установить соединение с сервером мониторинга.",
    "not_available": "Сервер отвечает, но данные мониторинга пока недоступны.",
    "redirect": "Health URL возвращает перенаправление; проверка остановлена без перехода.",
    "body_too_large": "Ответ health превышает допустимый размер.",
    "invalid_body": "Ответ health имеет неверный формат.",
    "invalid_schema": "Ответ health не соответствует согласованному контракту.",
    "instance_mismatch": "Health ответил от другой установки; состояние проекта не подтверждено.",
    "invalid_time": "Временные отметки мониторинга некорректны.",
    "observer_stale": "Наблюдатель на рабочем сервере давно не обновлял heartbeat.",
    "snapshot_stale": "Состояние редакции давно не собиралось, хотя сервер отвечает.",
    "source_stale": "Редакционный обработчик давно не обновлял heartbeat и не поставлен на паузу.",
    "sender_stale": "Обработчик служебных сообщений давно не обновлял heartbeat.",
    "sender_unknown": "Исход служебной отправки MAX неизвестен; новые отчёты заблокированы до сверки.",
    "sender_blocked": "Доставка служебных сообщений MAX заблокирована; требуется проверка.",
}


def _endpoint(value):
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 2048
                or any(character.isspace() or ord(character) < 32 for character in value)
                or any(character in value for character in "\\?#")):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0):
            raise ValueError
        httpx.URL(value)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise AgentError("MONITOR_WATCHDOG_ENDPOINT_INVALID") from exc
    return value


def _stamp(value):
    try:
        return (float(value) if type(value) in {int, float} and math.isfinite(value)
                and value > 0 else None)
    except (ValueError, OverflowError):
        return None


class Watchdog:
    """Один bounded GET ровно к указанному владельцем адресу."""

    def __init__(self, client, endpoint, *, expected_instance, max_age_seconds=180, clock=None):
        self.client = client
        self.endpoint = _endpoint(endpoint)
        if (not isinstance(expected_instance, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", expected_instance)):
            raise AgentError("MONITOR_WATCHDOG_INSTANCE_INVALID")
        if (type(max_age_seconds) not in {int, float} or not math.isfinite(max_age_seconds)
                or not 1 <= max_age_seconds <= 86_400):
            raise AgentError("MONITOR_WATCHDOG_AGE_INVALID")
        if clock is not None and not callable(clock):
            raise AgentError("MONITOR_WATCHDOG_CLOCK_INVALID")
        self.expected_instance = expected_instance
        self.max_age_seconds = max_age_seconds
        self.clock = clock or time.time

    def _result(self, state, reason, observed_at):
        message = _MESSAGES.get(reason)
        if message is None and re.fullmatch(r"http_[1-5][0-9]{2}", reason):
            message = "Сервер мониторинга вернул HTTP " + reason[5:] + "."
        return {"state": state, "incident_key": "watchdog:" + self.expected_instance + ":" + reason,
                "message": message or _MESSAGES["invalid_schema"], "observed_at": observed_at}

    def check(self):
        try:
            observed_at = _stamp(self.clock())
        except Exception:
            observed_at = None
        if observed_at is None:
            return self._result("invalid", "invalid_time", 0.0)
        deadline = time.monotonic() + 10
        try:
            with self.client.stream("GET", self.endpoint, follow_redirects=False,
                    timeout=httpx.Timeout(10, connect=5),
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                if time.monotonic() > deadline:
                    return self._result("unavailable", "timeout", observed_at)
                if 300 <= response.status_code < 400:
                    return self._result("invalid", "redirect", observed_at)
                if response.status_code != 200:
                    return self._result("unavailable", "http_" + str(response.status_code), observed_at)
                length = response.headers.get("content-length")
                if length is not None:
                    if not re.fullmatch(r"[0-9]+", length) or len(length) > 16:
                        return self._result("invalid", "invalid_body", observed_at)
                    if int(length) > MAX_HEALTH_BYTES:
                        return self._result("invalid", "body_too_large", observed_at)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return self._result("invalid", "invalid_body", observed_at)
                body = bytearray()
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if time.monotonic() > deadline:
                        return self._result("unavailable", "timeout", observed_at)
                    body.extend(chunk)
                    if len(body) > MAX_HEALTH_BYTES:
                        return self._result("invalid", "body_too_large", observed_at)
        except httpx.TimeoutException:
            return self._result("unavailable", "timeout", observed_at)
        except Exception:
            # HTTP errors often embed URLs/headers. They never reach notifications.
            return self._result("unavailable", "network_error", observed_at)
        try:
            payload = parse_json(body.decode("utf-8"))
        except (AgentError, UnicodeDecodeError, ValueError):
            return self._result("invalid", "invalid_body", observed_at)
        return self._assess(payload, observed_at)

    def _assess(self, payload, observed_at):
        if not isinstance(payload, dict) or payload.get("schema") != HEARTBEAT_SCHEMA:
            return self._result("invalid", "invalid_schema", observed_at)
        if payload.get("instance_id") != self.expected_instance:
            return self._result("invalid", "instance_mismatch", observed_at)
        if "available" in payload and type(payload["available"]) is not bool:
            return self._result("invalid", "invalid_schema", observed_at)
        if payload.get("available") is False:
            return self._result("unavailable", "not_available", observed_at)
        observer = payload.get("observer")
        if not isinstance(observer, dict):
            return self._result("invalid", "invalid_schema", observed_at)
        paused = observer.get("source_paused", payload.get("source_paused"))
        sender_state = observer.get("sender_state")
        if (type(paused) is not bool or not isinstance(sender_state, str)
                or sender_state not in _SENDERS):
            return self._result("invalid", "invalid_schema", observed_at)
        if ("source_paused" in observer and "source_paused" in payload
                and observer["source_paused"] != payload["source_paused"]):
            return self._result("invalid", "invalid_schema", observed_at)
        fields = ("heartbeat_at", "source_collected_at", "source_heartbeat_at", "sender_heartbeat_at")
        if not set(fields) <= observer.keys():
            return self._result("invalid", "invalid_schema", observed_at)
        stamps = {}
        for field in fields:
            value = observer.get(field)
            nullable = field in {"source_heartbeat_at", "sender_heartbeat_at"}
            if value is None and nullable:
                stamps[field] = None
                continue
            stamp = _stamp(value)
            if stamp is None or stamp > observed_at + 30:
                return self._result("invalid", "invalid_time", observed_at)
            stamps[field] = stamp
        for field, reason in (("heartbeat_at", "observer_stale"),
                              ("source_collected_at", "snapshot_stale")):
            if observed_at - stamps[field] > self.max_age_seconds:
                return self._result("stale", reason, observed_at)
        if not paused and (stamps["source_heartbeat_at"] is None
                or observed_at - stamps["source_heartbeat_at"] > self.max_age_seconds):
            return self._result("stale", "source_stale", observed_at)
        if sender_state in {"unknown", "blocked"}:
            return self._result("stale", "sender_" + sender_state, observed_at)
        if sender_state != "disabled" and (stamps["sender_heartbeat_at"] is None
                or observed_at - stamps["sender_heartbeat_at"] > self.max_age_seconds):
            return self._result("stale", "sender_stale", observed_at)
        return self._result("healthy", "healthy", observed_at)


def report_changes(store, check, queue):
    """Очередь получает только смену состояния; повтор после сбоя имеет тот же event key.

    store — MonitorStore на машине наблюдателя; queue(key, kind, text) — его
    локальная очередь уведомлений. Первый здоровый ответ остаётся тихим.
    """
    result = check() if callable(check) else check
    if (not isinstance(result, dict) or not isinstance(result.get("state"), str)
            or result["state"] not in _STATES
            or not isinstance(result.get("incident_key"), str)
            or not re.fullmatch(r"watchdog:[A-Za-z0-9_-]{1,80}:[a-z0-9_]{1,48}", result["incident_key"])):
        raise AgentError("MONITOR_WATCHDOG_RESULT_INVALID")
    reason = result["incident_key"].rsplit(":", 1)[1]
    if reason not in _MESSAGES and not re.fullmatch(r"http_[1-5][0-9]{2}", reason):
        raise AgentError("MONITOR_WATCHDOG_RESULT_INVALID")
    namespace = result["incident_key"].rsplit(":", 1)[0]
    meta_key = "watchdog:" + fingerprint(namespace)[:24]
    with profile_lock(private_path(store.root, "data/monitor/watchdog.lock")):
        previous = store.meta(meta_key)
        if previous is not None and (not isinstance(previous, dict)
                or not isinstance(previous.get("state"), str) or previous["state"] not in _STATES
                or not isinstance(previous.get("incident_key"), str)
                or not previous["incident_key"].startswith(namespace + ":")
                or type(previous.get("generation")) is not int
                or not 0 <= previous["generation"] <= 10**9):
            raise AgentError("MONITOR_WATCHDOG_STATE_INVALID")
        current = {"state": result["state"], "incident_key": result["incident_key"]}
        if previous and all(previous.get(key) == value for key, value in current.items()):
            return {"changed": False, "queued": False, "state": result["state"]}
        generation = previous.get("generation", 0) + 1 if previous else 0
        should_queue = previous is not None or result["state"] != "healthy"
        if should_queue:
            # Текст берём из словаря, а не из необязательных полей удалённого ответа.
            message = _MESSAGES.get(reason)
            if message is None:
                message = "Сервер мониторинга вернул HTTP " + reason[5:] + "."
            if result["state"] == "healthy" and previous and previous["state"] != "healthy":
                message = "Связь восстановлена. " + message
            event_key = meta_key + ":" + str(generation) + ":" + fingerprint(current)[:12]
            queue(event_key, "watchdog", message)
        # После сбоя queue до set_meta повторяется тот же ключ, а не новая отправка.
        store.set_meta(meta_key, {**current, "generation": generation})
    return {"changed": True, "queued": should_queue, "state": result["state"]}
