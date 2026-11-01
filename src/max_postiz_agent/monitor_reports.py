"""Краткие отчёты из сохранённого состояния, без моделей и внешней сверки.

Свежая дата снимка означает чтение журнала, а не новую проверку публикации.
Секреты, тела статей, transport options и исходные ошибки в снимок не входят.
"""

import hashlib
import math
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .platform_management import route_state
from .server_api import public_state
from .server_state import ServerState

SCHEMA = "monitor-snapshot-v1"
MAX_REPORT_CHARS = 4000
FRESH_SECONDS = 300
_MOSCOW = timezone(timedelta(hours=3))
_PLATFORMS = {"max": "MAX", "pikabu": "Пикабу", "vk": "VK", "telegram": "Telegram"}
_DISPATCH_STATES = {"pending", "sending", "unknown", "published", "published_unverified",
                    "scheduled", "awaiting_manual", "owner_attested", "failed", "retry_wait", "cancelled"}
_PHASES = {"paused", "idle", "selecting", "translating", "reviewing", "repairing", "scheduling",
           "waiting_dispatch", "waiting_interval", "waiting_slot", "blocked", "preparing",
           "reserve_satisfied", "reserve_refilled", "quality_ready", "verified", "published"}
_PHASE_LABELS = {"paused": "пауза", "idle": "ожидание", "selecting": "выбор источника",
                 "translating": "перевод", "reviewing": "проверка", "repairing": "редактура",
                 "scheduling": "назначение слота", "waiting_dispatch": "ожидание отправки",
                 "waiting_interval": "ожидание интервала", "waiting_slot": "ожидание слота",
                 "blocked": "блокировка", "preparing": "подготовка", "reserve_satisfied": "резерв полон",
                 "reserve_refilled": "резерв пополнен", "quality_ready": "проверено",
                 "verified": "подтверждено", "published": "опубликовано"}
_SLOT_STATES = {"assigned", "pending", "blocked_unknown", "delivered", "published", "missed"}
_ERRORS = {
    "ARTICLE_RECONCILIATION_REQUIRED": "Нужна сверка неизвестного исхода; повторная отправка запрещена",
    "ARTICLE_RESERVE_EMPTY": "Нет готовых статей для следующего слота",
    "ARTICLE_RESERVE_PREPARATION_ACTIVE": "Предыдущая подготовка ещё занимает claim",
    "SOURCE_HTTP_401": "Источник требует авторизации",
    "SOURCE_HTTP_403": "Источник запретил доступ",
    "SOURCE_HTTP_429": "Источник ограничил частоту запросов; нужен период ожидания",
    "SOURCE_CAPTCHA": "Источник требует CAPTCHA; нужен владелец",
    "ARTICLE_RIGHTS_REQUIRED": "Права источника не подтверждены",
    "ARTICLE_IMAGE_RIGHTS_REQUIRED": "Права изображения не подтверждены",
    "ARTICLE_IMAGE_REQUIRED": "Нет проверенного изображения",
    "ARTICLE_QUALITY_REJECTED": "Кандидат отклонён проверкой качества",
    "TRANSLATION_NUMBER_CHANGED": "Проверка обнаружила изменение чисел",
    "EDITORIAL_LEADERSHIP_LOST": "Обработчик потерял право владения",
    "ROUTE_DISPATCH_LEASE_LOST": "Потеряно право текущей отправки; нужна сверка",
    "APPROVAL_STALE": "Согласование устарело после изменения версии",
    "TARGET_PAUSED": "Адресат поставлен на паузу",
    "SCHEDULE_GAP_TOO_SHORT": "Слот нарушает минимальный интервал публикаций",
    "VK_BROWSER_OPERATION_UNKNOWN": "Исход операции VK неизвестен; нужна сверка",
    "PIKABU_BROWSER_OPERATION_UNKNOWN": "Исход операции Пикабу неизвестен; нужна сверка",
    "BROWSER_OPERATION_UNKNOWN": "Исход браузерной операции неизвестен; нужна сверка",
    "MAX_HTTP_429": "MAX ограничил частоту запросов; повторная отправка не разрешена",
    "TELEGRAM_HTTP_429": "Telegram ограничил частоту запросов",
    "MONITOR_STATE_UNAVAILABLE": "Состояние редакции прочитать не удалось",
    "MONITOR_ROUTES_UNAVAILABLE": "Состояние маршрутов прочитать не удалось",
    "MONITOR_UNCLASSIFIED_ERROR": "Есть ошибка; подробности доступны в закрытом журнале",
}


def _stamp(value):
    try:
        return float(value) if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _number(value, default=0):
    return value if type(value) is int and 0 <= value <= 10**9 else default


def _choice(value, allowed, default="unknown"):
    return value if isinstance(value, str) and value in allowed else default


def _error(value):
    if isinstance(value, dict):
        value = value.get("code")
    if value is None or value == "":
        return None
    return value if isinstance(value, str) and value in _ERRORS else "MONITOR_UNCLASSIFIED_ERROR"


def _text(value, limit=100):
    """Свободный текст минимален; известные секреты и credential-паттерны скрываем."""
    if not isinstance(value, str):
        return ""
    if len(value) > 65536:
        return "[текст скрыт: превышен размер]"
    for key, secret in os.environ.items():
        if (re.search(r"TOKEN|SECRET|PASSWORD|COOKIE|API_KEY|DATABASE_URL|DSN", key, re.I)
                and len(secret) >= 6):
            value = value.replace(secret, "[скрыто]")
    value = re.sub(r"(?i)(?:https?|postgres(?:ql)?|redis)://\S+", "[ссылка]", value)
    if re.search(r"(?i)\b(?:bearer\s+|(?:token|password|secret|cookie|api[_-]?key)\s*[:=])", value):
        return "[скрыто]"
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|[0-9]{6,}:[A-Za-z0-9_-]{15,}|"
                   r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", "[скрыто]", value)
    value = "".join(c if not unicodedata.category(c).startswith("C") else " " for c in value)
    value = " ".join(value.split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _ident(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value if _text(value, 128) == value else "hidden"
    return "unknown"


def _link(value, platform):
    """Ссылка на пост: HTTPS нужной площадки, без учётных данных и параметров."""
    if not isinstance(value, str) or len(value) > 350 or any(c.isspace() for c in value):
        return None
    try:
        url = urlsplit(value)
        hosts = {"max": {"max.ru", "web.max.ru"}, "telegram": {"t.me"},
                 "vk": {"vk.com", "www.vk.com"}, "pikabu": {"pikabu.ru", "www.pikabu.ru"}}
        if (url.scheme != "https" or url.netloc not in hosts.get(platform, set())
                or url.username or url.password or url.query or url.fragment
                or not re.fullmatch(r"/[A-Za-z0-9_/-]+", url.path)):
            return None
        if platform == "vk" and not re.fullmatch(r"/wall-?[0-9]+_[0-9]+", url.path):
            return None
        if platform == "telegram" and not re.fullmatch(r"/(?:c/[0-9]+|[A-Za-z0-9_]+)/[0-9]+", url.path):
            return None
        if platform == "pikabu" and not re.fullmatch(r"/story/[A-Za-z0-9_-]+_[0-9]+", url.path):
            return None
        if any(secret in value for key, secret in os.environ.items()
               if len(secret) >= 6 and re.search(r"TOKEN|SECRET|PASSWORD|COOKIE|API_KEY", key, re.I)):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _destination(value, platform):
    if isinstance(value, str) and re.fullmatch(r"@?[A-Za-z0-9_-]{1,80}", value):
        return _text(value, 80)
    return _link(value, platform) or "адрес скрыт"


def _editorial(value):
    value = value if isinstance(value, dict) else {}
    reserve = value.get("reserve") if isinstance(value.get("reserve"), dict) else {}
    preparation = value.get("preparation") if isinstance(value.get("preparation"), dict) else {}
    active = []
    for slot in reserve.get("active", [])[:100] if isinstance(reserve.get("active"), list) else []:
        if isinstance(slot, dict):
            active.append({"id": _ident(slot.get("id")), "target_id": _ident(slot.get("target_id")),
                "state": _choice(slot.get("state"), _SLOT_STATES),
                "due_at": _stamp(slot.get("due_at")), "delivered_at": _stamp(slot.get("delivered_at")),
                "error": _error(slot.get("error"))})
    slots = reserve.get("slots", {}) if isinstance(reserve.get("slots"), dict) else {}
    return {"available": value.get("available", True) is True,
        "version": _text(value.get("version"), 24), "paused": value.get("paused") is True,
        "phase": _choice(value.get("phase"), _PHASES),
        "heartbeat_at": _stamp(value.get("heartbeat_at")), "next_run_at": _stamp(value.get("next_run_at")),
        "last_error": _error(value.get("last_error")),
        "reserve": {"enabled": reserve.get("enabled") is True, "ready": _number(reserve.get("ready")),
            "target": _number(reserve.get("target")), "low": _number(reserve.get("low")),
            "active": active, "slots": {k: _number(v) for k, v in slots.items() if k in _SLOT_STATES},
            "last_delivered_at": _stamp(reserve.get("last_delivered_at")),
            "next_delivery_at": _stamp(reserve.get("next_delivery_at"))},
        "preparation": {"phase": _choice(preparation.get("phase"), _PHASES),
            "heartbeat_at": _stamp(preparation.get("heartbeat_at")),
            "last_error": _error(preparation.get("last_error"))}}


def _routes(value):
    value = value if isinstance(value, dict) else {}
    targets, dispatches = [], []
    for target in value.get("targets", [])[:100] if isinstance(value.get("targets"), list) else []:
        if not isinstance(target, dict):
            continue
        platform = _choice(target.get("platform"), _PLATFORMS)
        targets.append({"id": _ident(target.get("id")), "platform": platform,
            "destination": _destination(target.get("destination"), platform),
            "enabled": target.get("enabled") is True, "paused": target.get("paused") is True})
    for row in value.get("dispatches", [])[:150] if isinstance(value.get("dispatches"), list) else []:
        if not isinstance(row, dict):
            continue
        platform = _choice(row.get("platform"), _PLATFORMS)
        proof = row.get("evidence_kind")
        dispatches.append({"id": _ident(row.get("id")), "target_id": _ident(row.get("target_id")),
            "platform": platform, "title": _text(row.get("title"), 72),
            "destination": _destination(row.get("destination"), platform)
                if isinstance(row.get("destination"), str) and row["destination"] else None,
            "state": _choice(row.get("state"), _DISPATCH_STATES),
            "due_at": _stamp(row.get("due_at")), "verified": row.get("verified") is True,
            "evidence_kind": _choice(proof, {"fixture", "api_receipt", "independent_readback"}, "unverified"),
            "permalink": _link(row.get("permalink"), platform), "error": _error(row.get("error")),
            "attempts": _number(row.get("attempts"))})
    page = value.get("pagination") if isinstance(value.get("pagination"), dict) else {}
    return {"available": value.get("available", True) is True, "error": _error(value.get("error")),
        "targets": targets, "dispatches": dispatches,
        "pagination": {"dispatch_active_count": _number(page.get("dispatch_active_count")),
            "dispatch_count": _number(page.get("dispatch_count")),
            "dispatch_has_next": page.get("dispatch_has_next") is True,
            "unresolved_complete": page.get("unresolved_complete", False) is True}}


def collect_snapshot(core, store=None, *, now_at=None):
    """Читаем уже существующие БД/ledger; не инициализируем и не проверяем сеть."""
    stamp = _stamp(time.time() if now_at is None else now_at)
    if stamp is None:
        raise ValueError("MONITOR_TIME_INVALID")
    try:
        # Существующий public_state создаёт HourlyArticleCycle с mkdir(exist_ok).
        # Не допускаем bootstrap отсутствующего runtime из операции просмотра.
        root = getattr(getattr(core, "settings", None), "root", None)
        if root is not None and not (Path(root) / "data" / "hourly-articles").is_dir():
            editorial = {"available": False, "last_error": "MONITOR_STATE_UNAVAILABLE"}
        else:
            editorial = public_state(core, store if store is not None else ServerState(core))
    except Exception:
        editorial = {"available": False, "last_error": "MONITOR_STATE_UNAVAILABLE"}
    try:
        routes = route_state(core)
        routes = {**routes, "pagination": dict(routes.get("pagination", {}))}
        active_count = _number(routes["pagination"].get("dispatch_active_count"))
        visible = sum(row.get("state") in {"sending", "unknown"} for row in routes.get("dispatches", []))
        if active_count > visible:
            unresolved = route_state(core, dispatch_filter="unresolved")
            merged = {row["id"]: row for row in routes.get("dispatches", [])}
            merged.update({row["id"]: row for row in unresolved.get("dispatches", [])})
            routes["dispatches"] = list(merged.values())
            routes["pagination"]["unresolved_complete"] = not unresolved.get("pagination", {}).get("dispatch_has_next", True)
        else:
            routes["pagination"]["unresolved_complete"] = True
    except Exception:
        routes = {"available": False, "error": "MONITOR_ROUTES_UNAVAILABLE"}
    return {"schema": SCHEMA, "collected_at": stamp, "read_only": True,
            "editorial": _editorial(editorial), "routes": _routes(routes)}


def _clean(snapshot):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    return {"collected_at": _stamp(snapshot.get("collected_at")),
            "editorial": _editorial(snapshot.get("editorial")), "routes": _routes(snapshot.get("routes"))}


def _stale(snapshot, at):
    collected = snapshot["collected_at"]
    if collected is None or at - collected > FRESH_SECONDS or collected - at > 30:
        return "Снимок устарел или его время недостоверно"
    editorial = snapshot["editorial"]
    beat = editorial["heartbeat_at"]
    if editorial["available"] and not editorial["paused"] and (beat is None or at - beat > FRESH_SECONDS or beat - at > 30):
        return "Свежий heartbeat обработчика не подтверждён; нужна диагностика"
    return None


def _incident(category, entity, code):
    value = hashlib.sha256(f"{category}:{entity}:{code}".encode()).hexdigest()[:16]
    return "inc-" + value


def _latest(routes, target_id):
    rows = [row for row in routes["dispatches"] if row["target_id"] == target_id]
    return sorted(rows, key=lambda row: (row["due_at"] or 0, row["id"]), reverse=True)


def alert_signatures(snapshot):
    """Стабильные идентификаторы причин: меняется состояние, а не время опроса."""
    snapshot = _clean(snapshot)
    editorial, routes = snapshot["editorial"], snapshot["routes"]
    result = {}
    def add(category, entity, code, reason):
        result[_incident(category, entity, code)] = reason
    stale = _stale(snapshot, snapshot["collected_at"] or time.time())
    if stale:
        add("monitor", "heartbeat", "stale", stale)
    for channel, code in (("editorial", editorial["last_error"]),
                          ("preparation", editorial["preparation"]["last_error"]), ("routes", routes["error"])):
        if code:
            add("error", channel, code, _ERRORS[code])
    if not editorial["available"] and editorial["last_error"] != "MONITOR_STATE_UNAVAILABLE":
        add("error", "editorial", "unavailable", _ERRORS["MONITOR_STATE_UNAVAILABLE"])
    if not routes["available"] and routes["error"] != "MONITOR_ROUTES_UNAVAILABLE":
        add("error", "routes", "unavailable", _ERRORS["MONITOR_ROUTES_UNAVAILABLE"])
    if routes["available"] and not routes["pagination"]["unresolved_complete"]:
        add("monitor", "routes", "partial", "Список неизвестных исходов неполон; отсутствие блокировок не подтверждено")
    for row in routes["dispatches"]:
        if row["state"] == "unknown":
            add("dispatch", row["id"], "unknown", f"{_PLATFORMS.get(row['platform'], 'Маршрут')} {row['target_id']}: исход неизвестен; нужен разбор без повтора")
        elif (row["state"] in {"pending", "scheduled", "sending", "retry_wait"}
              and row["due_at"] is not None and snapshot["collected_at"] is not None
              and snapshot["collected_at"] - row["due_at"] > FRESH_SECONDS):
            add("dispatch", row["id"], "overdue", f"{_PLATFORMS.get(row['platform'], 'Маршрут')} {row['target_id']}: плановая дата прошла более 5 минут назад; итог не подтверждён")
    for target in routes["targets"]:
        latest = _latest(routes, target["id"])
        if not latest:
            continue
        # Будущий pending не скрывает последний известный отказ/API receipt.
        observed = [row for row in latest if row["state"] not in {"pending", "scheduled"}]
        row = observed[0] if observed else latest[0]
        if row["state"] in {"failed", "retry_wait"}:
            reason = _ERRORS.get(row["error"], "Отправка завершилась ошибкой; требуется диагностика")
            add("dispatch", row["id"], row["state"], f"{_PLATFORMS.get(target['platform'], 'Маршрут')} {target['id']}: {reason}")
        elif row["state"] in {"published", "published_unverified"} and not (
                row["verified"] and row["evidence_kind"] == "independent_readback"):
            add("dispatch", row["id"], "unverified", f"{_PLATFORMS.get(target['platform'], 'Маршрут')} {target['id']}: независимое подтверждение доставки отсутствует")
    for slot in editorial["reserve"]["active"]:
        if slot["state"] == "blocked_unknown":
            add("slot", slot["id"], "unknown", f"Слот {slot['target_id']}: неизвестный исход занимает место; замена запрещена")
    reserve = editorial["reserve"]
    if editorial["available"] and not editorial["paused"] and reserve["enabled"] and reserve["ready"] <= reserve["low"]:
        add("reserve", "shared", "low", "Готовый общий резерв достиг нижнего порога; требуется пополнение")
    return dict(sorted(result.items()))


def _when(value):
    stamp = _stamp(value)
    if stamp is None:
        return "не назначено"
    try:
        return datetime.fromtimestamp(stamp, _MOSCOW).strftime("%d.%m %H:%M:%S") + " МСК"
    except (OverflowError, ValueError, OSError):
        return "неизвестно"


def _delivery(row):
    if row["state"] == "published" and row["verified"] and row["evidence_kind"] == "independent_readback":
        return "доставка подтверждена сохранённой независимой сверкой"
    if row["state"] in {"published", "published_unverified"}:
        if row["evidence_kind"] == "api_receipt":
            return "API принял; независимого подтверждения нет"
        if row["evidence_kind"] == "fixture":
            return "тестовая квитанция; живая доставка не подтверждена"
        return "отмечено как published; независимого подтверждения нет"
    return {"unknown": "исход неизвестен; повтор запрещён", "sending": "отправка выполняется; итог ещё неизвестен",
        "pending": "ожидает отправки", "scheduled": "запланировано; доставка ещё не подтверждена",
        "failed": "ошибка отправки", "retry_wait": "ожидание после ошибки",
        "cancelled": "отменено", "awaiting_manual": "ожидает ручного действия",
        "owner_attested": "отметка владельца; независимого подтверждения нет"}.get(row["state"], "состояние неизвестно")


def render_report(snapshot, *, report_id, now_at=None, kind="hourly"):
    """Один обычный текст MAX; без HTML/Markdown и без обещания новой проверки."""
    snapshot = _clean(snapshot)
    at = _stamp(time.time() if now_at is None else now_at)
    if at is None:
        raise ValueError("MONITOR_TIME_INVALID")
    editorial, routes = snapshot["editorial"], snapshot["routes"]
    heading = {"hourly": "Почасовой отчёт", "status": "Состояние проекта", "incident": "События проекта",
               "daily": "Ежедневный снимок состояния", "recovery": "Восстановление"}.get(kind, "Отчёт проекта")
    lines = [f"{heading} #{_ident(report_id)}", f"Срез: {_when(snapshot['collected_at'])}",
             "Данные журнала; новой сверки площадок не выполнялось."]
    stale = _stale(snapshot, at)
    lines.append("⚠ " + stale if stale else "Свежесть среза: до 5 минут.")
    if editorial["available"]:
        permission = "пауза" if editorial["paused"] else "работа разрешена"
        phase = _PHASE_LABELS.get(editorial["phase"], "фаза неизвестна")
        heartbeat = _when(editorial["heartbeat_at"]) if editorial["heartbeat_at"] is not None else "нет данных"
        lines.append(f"Редакция: {permission}. Последняя стадия: {phase}. Heartbeat: {heartbeat}.")
    else:
        lines.append("⚠ Редакция: состояние недоступно.")
    reserve = editorial["reserve"]
    if editorial["available"]:
        lines.append(f"Резерв: {reserve['ready']} quality_ready по учёту, цель {reserve['target']}, порог {reserve['low']}.")
        lines.append("Это общий пул; готовность для каждого канала отдельно не подтверждена.")
        assigned = Counter(row["target_id"] for row in reserve["active"] if row["state"] in {"assigned", "pending"})
        if assigned:
            lines.append("Назначено из резерва: " + "; ".join(f"{key}: {count}" for key, count in sorted(assigned.items())[:8]) + ".")
        lines.append("Пополнение: " + _PHASE_LABELS.get(editorial["preparation"]["phase"], "нет свежей стадии") + ".")
        lines.append("Следующая доставка: " + _when(reserve["next_delivery_at"]) + ".")
    scheduled = [row["due_at"] for row in routes["dispatches"] if row["due_at"] is not None
                 and row["state"] in {"pending", "scheduled", "retry_wait"}]
    if scheduled:
        lines.append("Ближайшая плановая дата в журнале маршрутов: " + _when(min(scheduled)) + ".")
    incidents = alert_signatures(snapshot)
    if stale and not _stale(snapshot, snapshot["collected_at"] or at):
        incidents[_incident("monitor", "snapshot", "stale")] = stale
    if incidents:
        lines.append(f"Требуют внимания: {len(incidents)}.")
        lines.extend(f"• {ident}: {reason}" for ident, reason in list(incidents.items())[:5])
        if len(incidents) > 5:
            lines.append(f"Ещё {len(incidents) - 5} причин: запросите подробный статус.")
    elif editorial["available"] and routes["available"]:
        lines.append("В доступном срезе активных ошибок и блокировок не найдено.")
    lines.append("Площадки:")
    for target in routes["targets"][:10]:
        status = "выключен" if not target["enabled"] else "пауза" if target["paused"] else "включён"
        label = f"{_PLATFORMS.get(target['platform'], 'Маршрут')} / {target['id']} ({target['destination']})"
        rows = _latest(routes, target["id"])
        lines.append(f"• {label}: {status}.")
        if rows:
            past = [row for row in rows if row["due_at"] is not None and row["due_at"] <= at]
            future = [row for row in rows if row["due_at"] is not None and row["due_at"] > at
                      and row["state"] in {"pending", "scheduled"}]
            row = past[0] if past else rows[0]
            title = f" «{row['title']}»" if row["title"] else ""
            recipient = row["destination"] or "адресат не сохранён"
            platform = _PLATFORMS.get(row["platform"], "площадка не сохранена")
            lines.append(f"  По плану {_when(row['due_at'])}, {platform} → {recipient}:{title} {_delivery(row)}.")
            if row["permalink"]:
                lines.append("  " + row["permalink"])
            if past and future:
                lines.append("  Следующий план: " + _when(min(item["due_at"] for item in future)) + ".")
        else:
            lines.append("  Записей в доступном окне нет; доставка не подтверждена.")
    if not routes["available"]:
        lines.append("⚠ Сведения о площадках недоступны.")
    if len(routes["targets"]) > 10:
        lines.append(f"Ещё адресатов: {len(routes['targets']) - 10}; подробности в панели.")
    if routes["pagination"]["dispatch_has_next"]:
        lines.append("История показана частично; старые доставки здесь не перечислены.")
    result = "\n".join(lines)
    return result if len(result) <= MAX_REPORT_CHARS else result[:MAX_REPORT_CHARS - 66] + "\n…Отчёт сокращён. Полные сведения запросите командой статуса."
