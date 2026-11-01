"""Ограниченные команды владельца из публичного DOM MAX, без Bot API.

Команда получает durable claim до изменения проекта. Неизвестный результат
не повторяется после рестарта. Произвольный текст не становится shell/SQL.
"""

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .article_cycle import HourlyArticleCycle
from .article_reserve import ready_for_policy
from .config import AgentError, fingerprint
from .hourly_reserve import PreparationState, ReserveServerCycle
from .hub_models import BridgePost, Dispatch, Target, Variant
from .isolated_workspace import assert_isolated
from .server_state import ServerState, validate_settings
from .server_models import EditorialPreparationClaim
from .translation import MADLAD_BACKEND_CONTRACT, MADLAD_MODEL


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_ERROR_CODES = {"ARTICLE_POLICY_INVALID", "ARTICLE_POLICY_DISABLED", "EDITORIAL_PAUSED",
    "ARTICLE_RECONCILIATION_REQUIRED", "EDITORIAL_REVISION_CONFLICT",
    "EDITORIAL_PAUSE_BEFORE_SETTINGS", "ARTICLE_RESERVE_PREPARATION_ACTIVE",
    "EDITORIAL_SOURCE_POLICY_REQUIRED", "ARTICLE_RIGHTS_REQUIRED", "APPROVAL_STALE",
    "TARGET_NOT_FOUND", "TARGET_DISABLED", "BROWSER_PROFILE_BUSY", "ROUTE_RECONCILE_STATE_INVALID",
    "MONITOR_PROPOSAL_INVALID", "MONITOR_COMMAND_CONFIG_CHANGED", "MONITOR_REFILL_LIMIT",
    "MONITOR_FILTER_PREVIEW_TOO_LARGE", "ARTICLE_AUTOMATIC_POLICY_REQUIRED", "EDITORIAL_LEADERSHIP_LOST",
    "MONITOR_TASK_NOT_FOUND", "MONITOR_TASK_NOT_CANCELLABLE", "MONITOR_PERMISSION_MIGRATION_INVALID",
    "MONITOR_PERMISSION_MIGRATION_NOT_QUIESCENT", "MONITOR_COPY_TARGETS_NOT_PAUSED",
    "MONITOR_COMMAND_MINUTE_ALREADY_USED", "MONITOR_TASK_RESULT_UNKNOWN"}

_TASK_STATES = {"queued": "в очереди", "running": "выполняется; результат ещё не подтверждён",
    "unknown": "неизвестный исход; повтор запрещён", "done": "завершена", "blocked": "заблокирована",
    "cancelled": "отменена"}
_NEW_WORK_ACTIONS = {"refill", "resume_refill", "resume_project", "source_propose", "source_settings"}
_CANDIDATE_STATES = {"cancelled": "отменён", "rejected": "отклонён", "failed": "ошибка",
    "needs_attention": "нужна проверка", "blocked_unknown": "неизвестный исход", "unknown": "неизвестный исход"}
_CANDIDATE_REASONS = {
    "ARTICLE_IMAGE_RIGHTS_REQUIRED": "Права или credit изображения не подтверждены.",
    "ARTICLE_IMAGE_SOURCE_UNAVAILABLE": "Изображение источника недоступно.",
    "ARTICLE_IMAGE_REQUIRED": "Нет проверенного изображения.",
    "ARTICLE_RIGHTS_REQUIRED": "Права источника не подтверждены.",
    "ARTICLE_QUALITY_REJECTED": "Кандидат не прошёл обязательные проверки качества.",
    "TRANSLATION_REFERENCE_CHANGED": "В переводе изменены или утрачены ссылки.",
    "TRANSLATION_NUMBER_CHANGED": "В переводе изменены числа.",
    "TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT": "Текст нельзя безопасно разделить для переводчика; модель не запускалась.",
    "TRANSLATION_SERVICE_UNAVAILABLE": "Ответ переводчика не подтверждён; нужна сверка, повтор запрещён.",
    "TRANSLATION_RESPONSE_INVALID": "Ответ переводчика не прошёл проверку; нужна сверка, повтор запрещён.",
    "ARTICLE_PREPARATION_INTERRUPTED": "Исход подготовки неизвестен; нужна сверка, повтор запрещён.",
    "ARTICLE_RESERVE_PREPARATION_INTERRUPTED": "Исход подготовки неизвестен; нужна сверка, повтор запрещён.",
    "ARTICLE_RECONCILIATION_REQUIRED": "Требуется сверка сохранённого исхода; повтор запрещён.",
    "ARTICLE_SPELLING_UNAVAILABLE": "Проверка LanguageTool не завершена успешно.",
    "MONITOR_CANDIDATE_REASON_HIDDEN": "Подробности не раскрываются; нужна сверка, автоматический повтор не разрешён.",
}
_CANDIDATE_UNKNOWN_CODES = {"TRANSLATION_SERVICE_UNAVAILABLE", "TRANSLATION_RESPONSE_INVALID",
    "ARTICLE_PREPARATION_INTERRUPTED", "ARTICLE_RESERVE_PREPARATION_INTERRUPTED",
    "ARTICLE_RECONCILIATION_REQUIRED", "MONITOR_CANDIDATE_REASON_HIDDEN"}

_REFILL_BLOCKERS = {
    "EDITORIAL_PAUSED": "Редакция копии на паузе; команда не включает цикл автоматически.",
    "MONITOR_RESERVE_DISABLED": "Политика резерва в копии выключена.",
    "MONITOR_LIVE_REVIEW_REQUIRED": "Независимый OpenCode/Ministral настроен как fake, а не live.",
    "MONITOR_OPENCODE_ENDPOINT_REQUIRED": "Не настроен отдельный локальный OpenCode на 127.0.0.1:4108.",
    "MONITOR_MADLAD_CONFIG_REQUIRED": "Не настроен исправленный MADLAD с точным alias и отдельным loopback endpoint.",
    "MONITOR_POLICY_REQUIRED": "Отсутствует включённая точная automatic-политика подготовки.",
    "MONITOR_SOURCE_RIGHTS_REQUIRED": "Источник политики не включён или не имеет проверенных прав.",
    "MONITOR_TARGET_CONFIGURATION_REQUIRED": "Для подготовки не настроены max_test, профиль и локальная привязка bridge.",
    "MONITOR_COPY_TARGETS_NOT_PAUSED": "Все редакционные адресаты копии должны оставаться на паузе.",
    "MONITOR_UNKNOWN_OPERATION_PRESENT": "Есть неизвестная отправка, mutation lease или занятая подготовка; нужна сверка.",
    "MONITOR_REFILL_LIMIT": "Запрошенный запас превышает разрешённый максимум копии.",
    "MONITOR_OPENCODE_UNAVAILABLE": "Локальный OpenCode недоступен или его версия не совпадает с настройкой.",
    "MONITOR_MADLAD_UNAVAILABLE": "MADLAD health не подтвердил исправленный backend и проверенные артефакты.",
    "MONITOR_MADLAD_BUSY": "Исправленный MADLAD занят другой работой; запрос ждёт свободного ресурса без нового inference.",
    "MONITOR_LANGUAGETOOL_UNAVAILABLE": "Локальный LanguageTool на 127.0.0.1:8010 не подтвердил русский язык.",
}

_COMMAND_REASONS = {**_REFILL_BLOCKERS,
    "ARTICLE_POLICY_INVALID": "Политика подготовки не прошла проверку; нужна диагностика настроек.",
    "ARTICLE_POLICY_DISABLED": "Политика подготовки выключена; команда её не включает.",
    "ARTICLE_RECONCILIATION_REQUIRED": "Есть незавершённая операция, неизвестный исход или lease; сначала нужна сверка, повтор не разрешён.",
    "EDITORIAL_REVISION_CONFLICT": "Настройки изменились после предложения. Получите новое точное предложение и подтвердите его заново.",
    "EDITORIAL_PAUSE_BEFORE_SETTINGS": "Фильтры меняются только на общей паузе: сначала «пауза», затем проверьте и подтвердите предложение.",
    "ARTICLE_RESERVE_PREPARATION_ACTIVE": "Сохранён незавершённый claim подготовки; нужна сверка, новая подготовка заблокирована.",
    "EDITORIAL_SOURCE_POLICY_REQUIRED": "Текущий режим источников не поддерживает этот фильтр; проверьте «источники».",
    "ARTICLE_RIGHTS_REQUIRED": "Права источника не подтверждены; команда не снимает эту защиту.",
    "APPROVAL_STALE": "Точное согласование устарело; нужны актуальные доказательства и новая проверка версии.",
    "TARGET_NOT_FOUND": "Адресат с таким ID не найден; проверьте идентификатор проекта.",
    "TARGET_DISABLED": "Адресат выключен; команда не включает его автоматически.",
    "BROWSER_PROFILE_BUSY": "Браузер или рабочий цикл занят другим обработчиком; новое действие не запускалось.",
    "ROUTE_RECONCILE_STATE_INVALID": "Сохранённая попытка не подходит для такой сверки; проверьте ID и состояние отправки.",
    "MONITOR_PROPOSAL_INVALID": "Предложение не найдено, просрочено или больше не действует. После сверки создайте новое точное предложение.",
    "MONITOR_COMMAND_CONFIG_CHANGED": "Привязка или разрешения команды изменились; старое задание не возобновлялось автоматически.",
    "MONITOR_FILTER_PREVIEW_TOO_LARGE": "Предложение фильтра слишком большое для безопасного просмотра; сократите список.",
    "ARTICLE_AUTOMATIC_POLICY_REQUIRED": "Для подготовки нужна отдельно включённая automatic-политика; команда режим не меняет.",
    "EDITORIAL_LEADERSHIP_LOST": "Обработчик потерял право работы; нужна сверка владельца цикла, повтор запрещён.",
    "MONITOR_TASK_NOT_FOUND": "Задание пополнения с таким ID не найдено у текущего владельца в этом чате. Проверьте «задачи».",
    "MONITOR_TASK_NOT_CANCELLABLE": "Это задание нельзя отменить таким действием; его результат и claims сохранены.",
    "MONITOR_TASK_RESULT_UNKNOWN": "Исход задания неизвестен: отмена не снимает claim и не разрешает повтор. Требуется сверка.",
    "MONITOR_PERMISSION_MIGRATION_INVALID": "Миграция разрешений не прошла проверку точной привязки и доказательств; задание не менялось.",
    "MONITOR_PERMISSION_MIGRATION_NOT_QUIESCENT": "Есть выполняющаяся команда или неизвестный исход; миграция разрешений запрещена.",
    "MONITOR_COMMAND_MINUTE_ALREADY_USED": "В эту видимую минуту уже принята команда новой работы или изменения настроек. "
        "Повторите нужную команду в следующую минуту. Пауза и отмена доступны сразу.",
}


def _loopback_endpoint(value, *, port=None):
    try:
        parsed = urlsplit(value)
        return (isinstance(value, str) and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"} and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"} and parsed.port is not None
            and (port is None or parsed.port == port))
    except (TypeError, ValueError):
        return False


def _assert_copy_database(core):
    root = core.settings.root.resolve()
    assert_isolated(root)
    url = core.db.engine.url
    if url.drivername.startswith("sqlite"):
        if url.database != ":memory:" and (not url.database or not Path(url.database).resolve().is_relative_to(root / "data")):
            raise AgentError("MONITOR_COMMAND_DATABASE_INVALID", 409)
    elif (not url.drivername.startswith("postgresql") or url.host not in {"127.0.0.1", "localhost"}
            or url.port != 55448 or url.database not in {"editorial_hub", "editorial_hub_tests"}):
        raise AgentError("MONITOR_COMMAND_DATABASE_INVALID", 409)


def _opencode_endpoint(core):
    from .monitor_config import private_path
    options = core.settings.app["opencode"]
    value = os.getenv(options["base_url_env"], "")
    access = options.get("local_access_path")
    if access:
        try:
            path = private_path(core.settings.root, access)
            if not value:
                value = json.loads(path.read_text(encoding="utf-8")).get("base_url", "")
        except (AgentError, OSError, ValueError, TypeError, AttributeError):
            return None
    return value if _loopback_endpoint(value, port=4108) else None


def _refill_service_checks(core, translation_url):
    """Только GET readiness, без исходника, inference, cookies или retries."""
    from .adapters import OpenCode
    errors = []
    reviewer = None
    try:
        reviewer = OpenCode(core.settings)
        status = reviewer.probe()
        if (status.get("state") != "available"
                or status.get("reported_version") != core.settings.app["opencode"]["required_version"]):
            errors.append("MONITOR_OPENCODE_UNAVAILABLE")
    except Exception:
        errors.append("MONITOR_OPENCODE_UNAVAILABLE")
    finally:
        if reviewer is not None:
            reviewer.client.close()
    with httpx.Client(timeout=4, trust_env=False, follow_redirects=False) as client:
        try:
            response = client.get(translation_url.rstrip("/") + "/health")
            value = response.json() if len(response.content) <= 65536 else None
            if (response.status_code != 200 or not isinstance(value, dict)
                    or value.get("model") != MADLAD_MODEL or value.get("backend_contract") != MADLAD_BACKEND_CONTRACT
                    or value.get("artifacts_verified") is not True or value.get("quarantined") is not False
                    or type(value.get("busy")) is not bool or value.get("status") != "configured"):
                errors.append("MONITOR_MADLAD_UNAVAILABLE")
            elif value["busy"]:
                errors.append("MONITOR_MADLAD_BUSY")
        except Exception:
            errors.append("MONITOR_MADLAD_UNAVAILABLE")
        try:
            response = client.get("http://127.0.0.1:8010/v2/languages")
            value = response.json() if len(response.content) <= 65536 else None
            if (response.status_code != 200 or not isinstance(value, list)
                    or not any(isinstance(row, dict) and row.get("code") == "ru" for row in value)):
                errors.append("MONITOR_LANGUAGETOOL_UNAVAILABLE")
        except Exception:
            errors.append("MONITOR_LANGUAGETOOL_UNAVAILABLE")
    return errors


def refill_preflight(core, desired=6, *, check_services=False, diagnostic_services=False):
    """Честные prerequisites. По умолчанию ни одного HTTP/модели/изменения БД.

    Health не подтверждает inference: каждый новый перевод по-прежнему обязан
    пройти MADLAD contract/SHA, независимые Ministral/LanguageTool и hard gates.
    """
    from .monitor_config import private_path
    _assert_copy_database(core)
    codes = []
    def block(code):
        if code not in codes:
            codes.append(code)
    current = ServerState(core).read()
    options = current["settings"]["reserve"]
    if current["paused"]:
        block("EDITORIAL_PAUSED")
    if not options["enabled"]:
        block("MONITOR_RESERVE_DISABLED")
    if type(desired) is not int or not 1 <= desired <= min(12, options["maximum"]):
        block("MONITOR_REFILL_LIMIT")
    if core.settings.app["opencode"]["adapter_mode"] != "live":
        block("MONITOR_LIVE_REVIEW_REQUIRED")
    if _opencode_endpoint(core) is None:
        block("MONITOR_OPENCODE_ENDPOINT_REQUIRED")
    translation = None
    try:
        translation = json.loads(private_path(core.settings.root, "config/translation.json").read_text(encoding="utf-8"))
        if (not isinstance(translation, dict) or translation.get("enabled") is not True
                or translation.get("model") != MADLAD_MODEL
                or not _loopback_endpoint(translation.get("base_url"))
                or type(translation.get("segment_chars", 1400)) is not int
                or not 400 <= translation.get("segment_chars", 1400) <= 1400):
            raise ValueError
    except (AgentError, OSError, ValueError, TypeError):
        block("MONITOR_MADLAD_CONFIG_REQUIRED")
    cycle = object.__new__(HourlyArticleCycle)
    cycle.core = core  # _policy и _source_target только читают config/DB.
    policy = None
    try:
        policy = cycle._policy()
        if policy.get("approval_mode") != "automatic":
            raise AgentError("ARTICLE_AUTOMATIC_POLICY_REQUIRED", 409)
    except AgentError:
        block("MONITOR_POLICY_REQUIRED")
    if policy is not None:
        candidates = [policy]
        if current["settings"]["source"] == "open_science":
            try:
                from .article_attribution import illustrated_presentation
                candidates = [value for value in cycle._policies()
                    if not illustrated_presentation(value.get("presentation_contract"))
                    or value["source_id"] in {"noirlab_iotw", "esahubble_potw"}]
            except AgentError:
                candidates = []
        failures = []
        for candidate in candidates:
            try:
                cycle._source_target(candidate)
                break
            except AgentError as error:
                failures.append("MONITOR_SOURCE_RIGHTS_REQUIRED" if error.code in {"UNKNOWN_SOURCE", "ARTICLE_RIGHTS_REQUIRED"}
                                else "MONITOR_TARGET_CONFIGURATION_REQUIRED")
            except (AttributeError, KeyError, TypeError):
                failures.append("MONITOR_TARGET_CONFIGURATION_REQUIRED")
        else:
            for code in failures or ["MONITOR_SOURCE_RIGHTS_REQUIRED"]:
                block(code)
    if not any(value.get("id") in {"nasa_svs_articles", "noirlab_iotw", "esahubble_potw"}
               and value.get("enabled") is True and value.get("rights", {}).get("verified") is True
               for value in core.settings.sources.get("sources", [])):
        block("MONITOR_SOURCE_RIGHTS_REQUIRED")
    with core.db.session() as session:
        if not session.scalar(select(Target.id).where(Target.platform == "max", Target.route == "max_browser",
                                                      Target.enabled.is_(True))):
            block("MONITOR_TARGET_CONFIGURATION_REQUIRED")
        if session.scalar(select(Target.id).where(Target.paused.is_not(True))):
            block("MONITOR_COPY_TARGETS_NOT_PAUSED")
        if (session.scalar(select(Dispatch.id).where(Dispatch.state.in_({"sending", "unknown"})
                    | Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0) | Dispatch.lease_until.is_(None)))
                or session.scalar(select(BridgePost.post_id).where(BridgePost.mutation_owner.is_not(None)
                    | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))
                or session.scalar(select(EditorialPreparationClaim.id).where(EditorialPreparationClaim.active_key.is_not(None)))):
            block("MONITOR_UNKNOWN_OPERATION_PRESENT")
    checked = bool(check_services and (not codes or diagnostic_services
        and set(codes) <= {"EDITORIAL_PAUSED", "MONITOR_UNKNOWN_OPERATION_PRESENT"}))
    if checked:
        for code in _refill_service_checks(core, translation["base_url"]):
            block(code)
    return {"ready": not codes, "blockers": [{"code": code, "message": _REFILL_BLOCKERS[code]} for code in codes],
            "service_readiness_checked": checked,
            "inference_verified": False, "source_rights_live_verified": False,
            "required_checks": ["MADLAD exact backend/artifacts", "independent Ministral", "local LanguageTool", "source rights"],
            "copy_only": True, "external_mutations": 0}


def _safe_code(error):
    return error.code if isinstance(error, AgentError) and error.code in _ERROR_CODES else "MONITOR_TASK_BLOCKED"


def _error_reply(error):
    code = _safe_code(error)
    return {"code": code, "message": _COMMAND_REASONS.get(code,
        "Действие остановлено проверками проекта; нужна диагностика. История и ограничения сохранены.") + " Код: " + code + "."}


def _editorial_actor(actor):
    """Core Audit.actor ограничен 64; полный principal остаётся в inbox."""
    value = "max-monitor:" + actor
    return value if len(value) <= 64 else "max-monitor:" + hashlib.sha256(actor.encode("utf-8")).hexdigest()[:52]


def parse_command(text):
    """Разбираем только заранее определённый русский словарь."""
    from .monitor_command_manifest import parse_command_text
    if not isinstance(text, str) or not 1 <= len(text) <= 1200:
        raise AgentError("MONITOR_COMMAND_INVALID", 422)
    payload = parse_command_text(text)
    if payload is None:
        raise AgentError("MONITOR_COMMAND_UNSUPPORTED", 422)
    if payload["action"] == "source_propose":
        payload["url"] = _candidate_url(payload["url"])
    if payload["action"] == "source_settings" and "values" in payload:
        values = payload["values"]
        # Пустой список лексический parser выдаёт только явной команде очистки.
        if (len(values) > 20 or any(not 1 <= len(value) <= 80 or not re.fullmatch(
                r"[\w .\-а-яА-ЯёЁ]+", value) for value in values)):
            raise AgentError("MONITOR_COMMAND_INVALID", 422)
    return payload


def _candidate_url(value):
    from .monitor_command_manifest import source_url_is_safe
    if not source_url_is_safe(value):
        raise AgentError("MONITOR_SOURCE_URL_INVALID", 422)
    return value


class _RefillState:
    """Один запрос ограничивает объём, не записывая новую политику/настройки."""

    def __init__(self, state, desired, paused):
        self.state, self.desired, self.paused = state, desired, paused

    def read(self):
        value = copy.deepcopy(self.state.read())
        value["paused"] = value["paused"] or self.paused()
        options = value["settings"]["reserve"]
        # Схема требует low >= 1 и target > low. Для одного материала достаточно
        # batch=1; run_one завершит запрос сразу после достижения requested=1.
        options["target"] = max(2, self.desired)
        options["low"] = min(options["low"], options["target"] - 1)
        options["batch"] = min(options["batch"], self.desired)
        return value

    def assert_running(self):
        if self.paused():
            raise AgentError("EDITORIAL_PAUSED", 409)
        return self.state.assert_running()

    def phase(self, phase, **details):
        return self.state.phase(phase, **details)

    def progress(self, channel, phase, **details):
        return self.state.progress(channel, phase, **details)


class CommandGateway:
    """Durable inbox и очередь bounded-команд в отдельной тестовой копии."""

    def __init__(self, core, options, path):
        self.core, self.options = core, copy.deepcopy(options)
        self.path = Path(path).absolute()
        self._guard_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_command_jobs (
                    id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, event_hash TEXT NOT NULL,
                    actor TEXT NOT NULL, chat_id TEXT NOT NULL, payload TEXT NOT NULL,
                    config_hash TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    next_at REAL NOT NULL DEFAULT 0, passes INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS monitor_source_proposals (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, chat_id TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_command_controls (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_command_mutation_minutes (
                    principal TEXT NOT NULL, chat_id TEXT NOT NULL, rendered_minute INTEGER NOT NULL,
                    command_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(principal,chat_id,rendered_minute));
                CREATE TABLE IF NOT EXISTS monitor_command_cancellations (
                    command_id TEXT PRIMARY KEY, actor TEXT NOT NULL, chat_id TEXT NOT NULL,
                    requested_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_command_permission_migrations (
                    id TEXT PRIMARY KEY, command_id TEXT NOT NULL, actor TEXT NOT NULL, chat_id TEXT NOT NULL,
                    old_config_hash TEXT NOT NULL, new_config_hash TEXT NOT NULL,
                    old_dom_hash TEXT NOT NULL, new_dom_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    permissions TEXT NOT NULL, acceptance_proof_hash TEXT,
                    created_at REAL NOT NULL, UNIQUE(command_id,new_config_hash));
            """)

    def _guard_path(self):
        root = self.core.settings.root.resolve()
        _assert_copy_database(self.core)
        if (not self.path.resolve().is_relative_to(root / "data" / "monitor")
                or self.path.suffix != ".sqlite"):
            raise AgentError("MONITOR_COMMAND_LEDGER_INVALID", 409)
        current = self.path
        while current != root:
            if current.is_symlink() or current.is_junction():
                raise AgentError("MONITOR_COMMAND_LEDGER_INVALID", 409)
            current = current.parent
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise AgentError("MONITOR_COMMAND_LEDGER_INVALID", 409)

    @contextmanager
    def _connection(self):
        self._guard_path()
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _enabled(self):
        owner_dom_valid = True
        if self.options.get("auth_mode", "numeric_sender") == "authenticated_account_outgoing":
            from .monitor_owner_chat import owner_contract_hash
            try:
                owner_dom_valid = owner_contract_hash(self.options.get("dom_contract")) == self.options.get("dom_contract_sha256")
            except AgentError:
                owner_dom_valid = False
        if (self.options.get("enabled") is not True or self.options.get("dom_contract_verified") is not True
                or not self.options.get("dom_contract") or self.options.get("dom_contract_sha256") != fingerprint(
                    self.options["dom_contract"])
                or not owner_dom_valid):
            raise AgentError("MONITOR_COMMANDS_NOT_VERIFIED", 409)

    def _account_principal(self):
        value = self.options.get("account_sha256")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            return None
        principal = "max-account-sha256:" + value
        if self.options.get("principal", principal) != principal:
            return None
        return principal

    def _actor_allowed(self, actor):
        if self.options.get("auth_mode", "numeric_sender") == "authenticated_account_outgoing":
            return actor is not None and actor == self._account_principal()
        return actor in self.options.get("owner_ids", [])

    def _owner_event(self, event, stamp):
        principal = self._account_principal()
        if (not isinstance(event, dict) or principal is None
                or event.get("auth_mode") != "authenticated_account_outgoing"
                or event.get("principal") != principal or event.get("account_sha256") != self.options.get("account_sha256")
                or event.get("dom_contract_sha256") != self.options.get("dom_contract_sha256")
                or event.get("outgoing") is not True or event.get("forwarded") is not False
                or event.get("quoted") is not False or event.get("edited") is not False
                or event.get("id_kind") != "content_minute_fingerprint"
                or type(event.get("created_at_precision_seconds")) is not int
                or event["created_at_precision_seconds"] != 60 or "sender_id" in event
                or not isinstance(event.get("chat_id"), str)
                or event["chat_id"] != self.options.get("control_chat_id")
                or not re.fullmatch(r"-?[0-9]{1,22}", event["chat_id"])
                or type(event.get("created_at")) is not int or event["created_at"] % 60 != 0
                or type(event.get("observed_at")) not in {int, float}
                or not math.isfinite(event["observed_at"])
                or not 0 <= stamp - event["observed_at"] <= self.options.get("max_age_seconds", 300)
                or not 0 <= stamp - event["created_at"] <= self.options.get("max_age_seconds", 300)
                or event["created_at"] > event["observed_at"]
                or event.get("reply_to_message_id") is not None
                or not isinstance(event.get("text"), str) or not 1 <= len(event["text"]) <= 1200):
            raise AgentError("MONITOR_COMMAND_ACCESS_DENIED", 403)
        expected = "owner-" + fingerprint({"contract": "max-owner-chat-event-v1", "chat_id": event["chat_id"],
            "principal": principal, "created_at": event["created_at"], "text": event["text"]})
        if event.get("message_id") != expected:
            raise AgentError("MONITOR_COMMAND_ACCESS_DENIED", 403)
        return principal

    def _authenticate(self, event, stamp):
        self._enabled()
        if not isinstance(event, dict):
            raise AgentError("MONITOR_COMMAND_ACCESS_DENIED", 403)
        mode = self.options.get("auth_mode", "numeric_sender")
        if mode == "authenticated_account_outgoing":
            return self._owner_event(event, stamp)
        if mode != "numeric_sender" or event.get("auth_mode", "numeric_sender") != mode:
            raise AgentError("MONITOR_COMMAND_ACCESS_DENIED", 403)
        if (not isinstance(event, dict) or event.get("forwarded") is not False
                or any(not isinstance(event.get(key), str) for key in ("chat_id", "sender_id", "message_id"))
                or not re.fullmatch(r"-?[0-9]{1,24}", event["chat_id"])
                or event["chat_id"] != self.options.get("control_chat_id")
                or not re.fullmatch(r"[0-9]{1,24}", event["sender_id"])
                or event["sender_id"] not in self.options.get("owner_ids", [])
                or event["sender_id"] in self.options.get("self_ids", [])
                or not _MESSAGE_ID.fullmatch(event["message_id"])
                or type(event.get("created_at")) not in {int, float}
                or not math.isfinite(event["created_at"])
                or not 0 <= stamp - event["created_at"] <= self.options.get("max_age_seconds", 300)
                or (event.get("reply_to_message_id") is not None
                    and (not isinstance(event["reply_to_message_id"], str)
                         or not _MESSAGE_ID.fullmatch(event["reply_to_message_id"])))):
            raise AgentError("MONITOR_COMMAND_ACCESS_DENIED", 403)
        return event["sender_id"]

    def handle(self, event, *, now_at=None):
        stamp = time.time() if now_at is None else now_at
        self._guard_path()
        actor = self._authenticate(event, stamp)
        payload = parse_command(event.get("text"))
        owner_mode = self.options.get("auth_mode", "numeric_sender") == "authenticated_account_outgoing"
        from .monitor_command_manifest import OWNER_ACTIONS
        if (payload["action"] not in self.options.get("allowed_actions", [])
                or owner_mode and payload["action"] not in OWNER_ACTIONS):
            raise AgentError("MONITOR_COMMAND_ACTION_DENIED", 403)
        event_key = str(event["chat_id"]) + ":" + str(event["message_id"])
        event_hash = fingerprint({key: event.get(key) for key in (
            "message_id", "chat_id", "sender_id", "created_at", "text", "reply_to_message_id",
            "auth_mode", "principal", "account_sha256", "outgoing", "quoted", "edited", "id_kind",
            "created_at_precision_seconds", "dom_contract_sha256")})
        ident = uuid.uuid4().hex
        state = "queued" if payload["action"] == "refill" else "running"
        blocked = None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM monitor_command_jobs WHERE event_key=?", (event_key,)).fetchone()
            if previous is not None:
                if previous["event_hash"] != event_hash:
                    raise AgentError("MONITOR_COMMAND_EVENT_CHANGED", 409)
                return self._result(previous, duplicate=True)
            if owner_mode and payload["action"] in _NEW_WORK_ACTIONS:
                try:
                    connection.execute("INSERT INTO monitor_command_mutation_minutes VALUES(?,?,?,?)",
                        (actor, event["chat_id"], event["created_at"], ident))
                except sqlite3.IntegrityError:
                    state, blocked = "blocked", _error_reply(AgentError("MONITOR_COMMAND_MINUTE_ALREADY_USED", 409))
            connection.execute("INSERT INTO monitor_command_jobs "
                "(id,event_key,event_hash,actor,chat_id,payload,config_hash,state,result,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident, event_key, event_hash, actor,
                    str(event["chat_id"]), json.dumps(payload), fingerprint(self.options), state,
                    json.dumps(blocked, ensure_ascii=False) if blocked is not None else "{}", stamp, stamp))
        if blocked is not None:
            return {"command_id": ident, "state": "blocked", "action": payload["action"], **blocked}
        if state == "queued":
            with self._connection() as connection:
                connection.execute("INSERT INTO monitor_command_controls(name,value) VALUES('refill_paused','false') "
                    "ON CONFLICT(name) DO UPDATE SET value='false'")
            return {"command_id": ident, "state": "queued", "action": "refill",
                    "message": "Пополнение до " + str(payload["target"])
                        + " статей принято в очередь; отправка публикаций не запускается."}
        try:
            result = self._execute(payload, actor, str(event["chat_id"]), stamp)
            result_state = result.get("state", "done")
        except AgentError as error:
            result_state, result = "blocked", _error_reply(error)
        except Exception:
            # После потери результата не утверждаем, что действие не произошло.
            result_state, result = "unknown", {"code": "MONITOR_COMMAND_RESULT_UNKNOWN",
                "message": "Исход команды неизвестен. Повтор автоматически не выполняется."}
        self._finish(ident, result_state, result, stamp)
        return {"command_id": ident, "state": result_state, "action": payload["action"], **result}

    def _finish(self, ident, state, result, stamp, *, next_at=0):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requested = connection.execute("SELECT 1 FROM monitor_command_cancellations WHERE command_id=?", (ident,)).fetchone()
            if requested:
                result["cancellation_requested"] = True
                if state == "queued":
                    state, next_at = "cancelled", 0
                    result["code"] = "MONITOR_TASK_CANCELLED"
                    result["message"] = "Проход завершён с известным результатом; будущие проходы отменены владельцем."
                    if type(result.get("ready")) is int:
                        result["message"] += " Готово: " + str(result["ready"]) + "."
            connection.execute("UPDATE monitor_command_jobs SET state=?,result=?,updated_at=?,next_at=? WHERE id=?",
                (state, json.dumps(result, ensure_ascii=False), stamp, next_at, ident))
        return state, result

    @staticmethod
    def _result(row, *, duplicate=False):
        state = "unknown" if row["state"] == "running" else row["state"]
        result = json.loads(row["result"])
        if state == "unknown" and not result:
            result = {"code": "MONITOR_COMMAND_RESULT_UNKNOWN",
                "message": "Ранее начатая команда требует сверки; повтор запрещён."}
        return {"command_id": row["id"], "state": state, "duplicate": duplicate, **result}

    def migrate_queued_permission_expansion(self, previous_options, *, actor, now_at=None,
                                            allow_dom_upgrade=False, acceptance_proof=None):
        """Явная локальная миграция разрешений, без нового claim или подготовки.

        Оператор передаёт сохранённую старую конфигурацию и доказательство
        независимо проверенного DOM, если его версия меняется. Исходные event,
        payload, результат и бюджет проходов остаются неизменными.
        """
        from .monitor_command_manifest import OWNER_ACTIONS
        stamp = time.time() if now_at is None else now_at
        self._enabled()
        if not isinstance(previous_options, dict) or not self._actor_allowed(actor):
            raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409)
        old, new = copy.deepcopy(previous_options), copy.deepcopy(self.options)
        before, after = old.pop("allowed_actions", None), new.pop("allowed_actions", None)
        if (not isinstance(before, list) or not isinstance(after, list)
                or any(not isinstance(value, str) for value in before + after)
                or len(set(before)) != len(before) or len(set(after)) != len(after)
                or not set(before) < set(after) or not set(after) <= OWNER_ACTIONS):
            raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409)
        old_dom, new_dom = old.get("dom_contract_sha256"), new.get("dom_contract_sha256")
        proof_hash = None
        if old_dom != new_dom:
            required = {"contract", "old_dom_sha256", "new_dom_sha256", "account_sha256", "chat_id",
                        "observed_at", "verified", "evidence_sha256"}
            proof = acceptance_proof
            if (allow_dom_upgrade is not True or not isinstance(proof, dict) or set(proof) != required
                    or old.get("auth_mode") != "authenticated_account_outgoing"
                    or old.get("dom_contract_verified") is not True
                    or not isinstance(old.get("dom_contract"), dict)
                    or old["dom_contract"].get("version") != "max-owner-chat-dom-v1"
                    or old_dom != fingerprint(old["dom_contract"])
                    or new["dom_contract"].get("version") != "max-owner-chat-dom-v2"
                    or proof["contract"] != "max-owner-dom-upgrade-acceptance-v1"
                    or proof["old_dom_sha256"] != old_dom or proof["new_dom_sha256"] != new_dom
                    or proof["account_sha256"] != new.get("account_sha256")
                    or proof["chat_id"] != new.get("control_chat_id") or proof["verified"] is not True
                    or type(proof["observed_at"]) not in {int, float} or not math.isfinite(proof["observed_at"])
                    or not 0 <= stamp - proof["observed_at"] <= 300
                    or not isinstance(proof["evidence_sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", proof["evidence_sha256"])):
                raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409)
            proof_hash = fingerprint(proof)
            for value in (old, new):
                value.pop("dom_contract")
                value.pop("dom_contract_sha256")
        elif allow_dom_upgrade or acceptance_proof is not None:
            raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409)
        if old != new:
            raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409)
        old_hash, new_hash = fingerprint(previous_options), fingerprint(self.options)
        migrated, audits = [], []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM monitor_command_jobs WHERE state IN ('running','unknown') LIMIT 1").fetchone():
                raise AgentError("MONITOR_PERMISSION_MIGRATION_NOT_QUIESCENT", 409)
            rows = connection.execute("SELECT * FROM monitor_command_jobs WHERE state='queued' AND config_hash=? "
                "AND actor=? AND chat_id=? AND id NOT IN (SELECT command_id FROM monitor_command_cancellations)",
                (old_hash, actor, self.options["control_chat_id"])).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except ValueError as error:
                    raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409) from error
                if (not isinstance(payload, dict) or set(payload) != {"action", "target"}
                        or payload["action"] != "refill" or type(payload["target"]) is not int
                        or not 1 <= payload["target"] <= 12):
                    raise AgentError("MONITOR_PERMISSION_MIGRATION_INVALID", 409)
                audit_id = uuid.uuid4().hex
                connection.execute("INSERT INTO monitor_command_permission_migrations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (audit_id, row["id"], actor, row["chat_id"], old_hash, new_hash, old_dom, new_dom,
                     row["event_hash"], fingerprint(payload), json.dumps({"before": before, "after": after}), proof_hash, stamp))
                connection.execute("UPDATE monitor_command_jobs SET config_hash=? WHERE id=? AND state='queued'",
                                   (new_hash, row["id"]))
                migrated.append(row["id"])
                audits.append(audit_id)
        return {"migrated": migrated, "audit_ids": audits, "old_config_hash": old_hash, "new_config_hash": new_hash}

    @staticmethod
    def _task_view(row, *, cancellation_requested=False):
        payload, result = json.loads(row["payload"]), json.loads(row["result"])
        action = payload.get("action") if isinstance(payload, dict) else None
        view = {"id": row["id"], "action": action if action == "refill" else "unknown",
                "state": row["state"] if row["state"] in _TASK_STATES else "unknown",
                "passes": max(0, min(4, row["passes"])), "cancellation_requested": cancellation_requested}
        if isinstance(payload, dict) and type(payload.get("target")) is int and 1 <= payload["target"] <= 12:
            view["target"] = payload["target"]
        if isinstance(result, dict):
            if type(result.get("ready")) is int and 0 <= result["ready"] <= 12:
                view["ready"] = result["ready"]
            if isinstance(result.get("code"), str) and result["code"] in _ERROR_CODES | set(_REFILL_BLOCKERS) | {"MONITOR_LIVE_REFILL_NOT_READY",
                    "MONITOR_REFILL_RESULT_UNKNOWN", "MONITOR_TASK_CANCELLED", "MONITOR_REFILL_PAUSED"}:
                view["code"] = result["code"]
            blockers = result.get("blockers")
            view["blockers"] = [value["code"] for value in (blockers if isinstance(blockers, list) else [])
                if isinstance(value, dict) and isinstance(value.get("code"), str) and value["code"] in _REFILL_BLOCKERS][:16]
        return view

    @staticmethod
    def _task_line(view, *, details=False):
        line = view["id"] + ": " + _TASK_STATES[view["state"]]
        if "target" in view:
            line += "; цель " + str(view["target"])
        if "ready" in view:
            line += "; готово при последнем завершённом проходе " + str(view["ready"])
        line += "; проходов " + str(view["passes"]) + "/4"
        if view["cancellation_requested"]:
            line += "; остановка будущих проходов запрошена"
        if view.get("code"):
            line += "; " + view["code"]
        if view.get("blockers"):
            line += "; " + (", ".join(view["blockers"]) if details else "препятствий " + str(len(view["blockers"])))
        return line

    def _tasks(self, actor, chat_id, command_id=None):
        with self._connection() as connection:
            if command_id is None:
                rows = connection.execute("SELECT * FROM monitor_command_jobs WHERE actor=? AND chat_id=? "
                    "AND json_extract(payload,'$.action')='refill' ORDER BY created_at DESC,id DESC LIMIT 10",
                    (actor, chat_id)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM monitor_command_jobs WHERE id=? AND actor=? AND chat_id=? "
                    "AND json_extract(payload,'$.action')='refill'", (command_id, actor, chat_id)).fetchall()
                if not rows:
                    raise AgentError("MONITOR_TASK_NOT_FOUND", 404)
            requested = {value[0] for value in connection.execute(
                "SELECT command_id FROM monitor_command_cancellations WHERE actor=? AND chat_id=?", (actor, chat_id))}
            views = [self._task_view(row, cancellation_requested=row["id"] in requested) for row in rows]
        lines = ["Последние задания пополнения:" if command_id is None else "Задание пополнения:"]
        lines.extend(self._task_line(value, details=command_id is not None) for value in views)
        if not views:
            lines.append("В этом чате у текущего владельца заданий нет.")
        return {"tasks": views, "message": "\n".join(lines)[:3500]}

    def _cancel_task(self, command_id, actor, chat_id, stamp):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM monitor_command_jobs WHERE id=? AND actor=? AND chat_id=? "
                "AND json_extract(payload,'$.action')='refill'", (command_id, actor, chat_id)).fetchone()
            if row is None:
                raise AgentError("MONITOR_TASK_NOT_FOUND", 404)
            if row["state"] == "queued":
                result = json.loads(row["result"])
                result.update(code="MONITOR_TASK_CANCELLED", cancellation_requested=True,
                    message="Ожидающее задание отменено владельцем; новая подготовка по нему не запускается.")
                connection.execute("UPDATE monitor_command_jobs SET state='cancelled',result=?,updated_at=?,next_at=0 WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), stamp, command_id))
                message = result["message"]
                task_state = "cancelled"
            elif row["state"] == "running":
                message = ("Запрос остановки сохранён. Начатая проверка/модель продолжает работу; "
                    "следующие проходы не начнутся после известного результата. Неизвестный исход требует сверки.")
                task_state = "running"
            elif row["state"] == "unknown":
                return {"task_id": command_id, "task_state": "unknown", "changed": False,
                    **_error_reply(AgentError("MONITOR_TASK_RESULT_UNKNOWN", 409))}
            else:
                return {"task_id": command_id, "task_state": row["state"], "changed": False,
                    "message": "Задание уже " + _TASK_STATES.get(row["state"], _TASK_STATES["unknown"])
                        + "; его результат и история сохранены."}
            connection.execute("INSERT INTO monitor_command_cancellations VALUES(?,?,?,?) ON CONFLICT(command_id) DO NOTHING",
                               (command_id, actor, chat_id, stamp))
        return {"task_id": command_id, "task_state": task_state, "cancellation_requested": True, "message": message}

    def _cancellation_requested(self, command_id):
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM monitor_command_cancellations WHERE command_id=?", (command_id,)).fetchone() is not None

    def _candidate_refusals(self):
        """Только чтение существующего ledger; ни bootstrap, ни trace/raw, ни HTTP."""
        from .monitor_config import private_path
        try:
            cycle = object.__new__(HourlyArticleCycle)
            cycle.core = self.core
            cycle.directory = private_path(self.core.settings.root, "data/hourly-articles")
            cycle.ledger_path = private_path(self.core.settings.root, "data/hourly-articles/ledger.local.json")
            private_path(self.core.settings.root, "data/hourly-articles/postgresql-authoritative.local.json")
            cycle.reserve_mode = False
            ledger = cycle._load()
            if not isinstance(ledger, dict) or not isinstance(ledger.get("records"), list):
                raise ValueError
        except Exception:
            return [], False
        configured = {value["id"] for value in self.core.settings.sources.get("sources", [])
            if isinstance(value, dict) and isinstance(value.get("id"), str) and _IDENTIFIER.fullmatch(value["id"])}
        ordered = []
        for index, record in enumerate(ledger["records"]):
            if (not isinstance(record, dict) or not isinstance(record.get("state"), str)
                    or record["state"] not in _CANDIDATE_STATES):
                continue
            attempts = [value for name in ("automatic_repair_attempts", "preparation_attempts")
                for value in (record.get(name, [])[-2:] if isinstance(record.get(name), list) else [])
                if isinstance(value, dict)]
            stamps = [value.get(key) for value in [record, *attempts]
                for key in ("claimed_at", "finished_at", "cancelled_at", "updated_at")]
            recent = max((value for value in stamps if type(value) in {int, float} and math.isfinite(value)), default=0)
            ordered.append((recent, index, record, attempts))
        result = []
        for _, _, record, attempts in sorted(ordered, key=lambda value: value[:2], reverse=True):
            raw_codes = [record.get("error"), *(value.get("error") for value in reversed(attempts))]
            codes = []
            for code in raw_codes:
                if isinstance(code, str) and code in _CANDIDATE_REASONS and code not in codes:
                    codes.append(code)
            if not codes:
                codes = ["MONITOR_CANDIDATE_REASON_HIDDEN"]
            try:
                ident = uuid.UUID(record.get("id", "")).hex[:8]
            except (ValueError, TypeError, AttributeError):
                ident = "скрыт"
            source = record.get("source_id")
            result.append({"record_id": ident, "source_id": source if isinstance(source, str) and source in configured else None,
                "state": record["state"], "codes": codes[:2],
                "reconciliation_required": record["state"] in {"unknown", "blocked_unknown"}
                    or bool(set(codes) & _CANDIDATE_UNKNOWN_CODES)
                    or any(code and (not isinstance(code, str) or code not in _CANDIDATE_REASONS) for code in raw_codes)})
            if len(result) == 5:
                break
        return result, True

    def _execute(self, payload, actor, chat_id, stamp):
        action = payload["action"]
        state = ServerState(self.core)
        if action == "help":
            from .monitor_command_manifest import COMMAND_SPECS, OWNER_ACTIONS
            allowed = set(self.options.get("allowed_actions", [])) & OWNER_ACTIONS
            lines = ["Доступные команды в этом чате:"]
            for spec in COMMAND_SPECS:
                if spec["action"] in allowed:
                    aliases = [value for value in spec.get("aliases", ()) if value != spec["syntax"]]
                    lines.append(spec["syntax"] + " — " + spec["description"]
                        + (" (также: " + ", ".join(aliases) + ")" if aliases else ""))
            lines.append("Параметры ID/URL/N заменяйте своими значениями; можно начинать команду с /.")
            lines.append("ID задания берите из «задачи»; TOKEN — из предложения фильтра. TARGET_ID/DISPATCH_ID — идентификаторы проекта.")
            if self.options.get("auth_mode") == "authenticated_account_outgoing":
                lines.append("Новая работа/изменение фильтров — не чаще одной команды в видимую минуту. Пауза и отмена доступны сразу.")
            return {"actions": sorted(allowed), "message": "\n".join(lines)[:3500]}
        if action in {"tasks", "task"}:
            return self._tasks(actor, chat_id, payload.get("command_id"))
        if action == "cancel":
            return self._cancel_task(payload["command_id"], actor, chat_id, stamp)
        if action == "resume_refill":
            with self._connection() as connection:
                connection.execute("INSERT INTO monitor_command_controls VALUES('refill_paused','false') "
                    "ON CONFLICT(name) DO UPDATE SET value='false'")
            return {"monitor_refill_paused": False,
                "message": "Очередь заданий пополнения из чата продолжена. Пауза редакции и адресатов сохранена; "
                    "заблокированные, отменённые и неизвестные задания не возобновлялись."}
        if action == "resume_project":
            current = state.read()
            result = state.control("start", current["revision"], _editorial_actor(actor), prepare_only=True)
            return {"project_paused": result["paused"],
                "message": "Общая пауза снята только для подготовки в копии. Все адресаты остались на паузе; "
                    "отправитель не запускался, прежние заблокированные задания не возобновлялись."}
        if action == "diagnostics":
            checked = refill_preflight(self.core, check_services=True, diagnostic_services=True)
            lines = ["Диагностика подготовки (без inference и публикаций):"]
            lines += [value["message"] for value in checked["blockers"]]
            if checked["ready"]:
                lines.append("Настройки и свежая доступность сервисов подтверждены; качество статьи ещё не проверено.")
            elif not checked["service_readiness_checked"]:
                lines.append("Сетевые GET не выполнялись: сначала нужно устранить препятствия конфигурации/состояния.")
            lines.append("Права конкретного материала и результаты моделей этим запросом не подтверждаются.")
            return {"diagnostics": checked, "message": "\n".join(lines)[:3500]}
        if action in {"status", "reserve", "errors"}:
            from .monitor_reports import collect_snapshot
            snapshot = collect_snapshot(self.core, state, now_at=stamp)
            if action == "status":
                from .monitor_reports import render_report
                return {"message": render_report(snapshot, report_id="command", now_at=stamp, kind="command")}
            if action == "errors":
                from .monitor_reports import alert_signatures
                errors = alert_signatures(snapshot)
                lines = ["Активные препятствия:"]
                for index, (key, reason) in enumerate(errors.items()):
                    line = key + ": " + reason
                    if len("\n".join([*lines, line])) > 1100:
                        lines.append("Ещё активных препятствий: " + str(len(errors) - index) + "; запросите статус.")
                        break
                    lines.append(line)
                if not errors:
                    lines.append("Зафиксированных препятствий нет; состояние площадок отдельно проверяется в статусе.")
                history, available = self._candidate_refusals()
                lines += ["", "Последние отказы кандидатов (история, не активная авария):"]
                for value in history:
                    line = value["record_id"] + " | " + (value["source_id"] or "источник скрыт") + " | " + _CANDIDATE_STATES[value["state"]]
                    line += ": " + "; ".join(code + " — " + _CANDIDATE_REASONS[code] for code in value["codes"])
                    if value["reconciliation_required"]:
                        line += " Нужна сверка; повтор запрещён."
                    lines.append(line)
                if not history:
                    lines.append("Сохранённых отказов не найдено." if available else "Историю прочитать не удалось; подробности не раскрываются.")
                return {"incidents": errors, "candidate_history": history, "candidate_history_available": available,
                        "message": "\n".join(lines)[:3500]}
            reserve = snapshot["editorial"].get("reserve", {})
            ready = reserve.get("ready")
            lines = ["Резерв: готово " + (str(ready) if type(ready) is int else "неизвестно") + " уникальных статей.",
                     "Цель: " + str(reserve.get("target", "неизвестно")) + "; порог пополнения: " + str(reserve.get("low", "неизвестно")) + "."]
            for key, value in reserve.get("slots", {}).items():
                if re.fullmatch(r"[a-z_]{1,40}", key) and type(value) is int:
                    lines.append("Слоты " + key + ": " + str(value))
            lines.append("Это общий резерв; готовность каждой площадки проверяется отдельно.")
            return {"reserve": reserve, "message": "\n".join(lines)[:3500]}
        if action == "sources":
            sources = self.core.settings.sources.get("sources", [])
            filters = state.read()["settings"]
            listed = [{"id": value["id"], "enabled": value.get("enabled") is True,
                                  "rights_verified": value.get("rights", {}).get("verified") is True}
                                 for value in sources if _IDENTIFIER.fullmatch(str(value.get("id", "")))]
            lines = ["Источники (настройки):"]
            lines += [value["id"] + ": " + ("включён" if value["enabled"] else "выключен")
                + "; права " + ("отмечены в конфигурации" if value["rights_verified"] else "не подтверждены")
                for value in listed]
            lines += ["Режим: " + filters["source"],
                "Авторы: " + (", ".join(filters["nasa"]["authors"]) or "без фильтра"),
                "Ключевые слова: " + (", ".join(filters["nasa"]["keywords"]) or "без фильтра"),
                "Это не новая проверка лицензий."]
            return {"sources": listed,
                    "source_mode": filters["source"], "filters": {key: filters["nasa"][key]
                        for key in ("authors", "keywords")},
                    "message": "\n".join(lines)[:3500]}
        if action == "pause":
            if payload["scope"] == "monitor_refill":
                with self._connection() as connection:
                    connection.execute("INSERT INTO monitor_command_controls(name,value) VALUES('refill_paused','true') "
                        "ON CONFLICT(name) DO UPDATE SET value='true'")
                return {"message": "Приостановлены задания пополнения из чата. Общий серверный цикл не менялся."}
            if payload["scope"] == "target":
                self.core.pause_target(payload["target_id"], True, actor=_editorial_actor(actor))
                return {"target_id": payload["target_id"], "paused": True,
                    "message": "Новые отправки адресату поставлены на паузу; начатые попытки сохранены."}
            current = state.read()
            state.control("pause", current["revision"], _editorial_actor(actor))
            return {"paused": True,
                "message": "Редакционный цикл и все адресаты на паузе. Начатые отправки требуют сверки."}
        if action == "unblock":
            return self._diagnose(payload["target_id"])
        if action == "reconcile":
            from .platform_management import reconcile
            result = reconcile(self.core, payload["dispatch_id"], actor=_editorial_actor(actor))
            return {"dispatch_id": payload["dispatch_id"], "reconciliation_state": result.get("state"),
                    "message": "Сверка " + payload["dispatch_id"] + ": "
                        + (result.get("state") if result.get("state") in {"published", "unknown", "sending", "failed"}
                           else "результат требует диагностики")
                        + ". Независимое подтверждение: " + ("есть" if result.get("verified") is True else "отсутствует")
                        + ". Повторная отправка не запускалась."}
        if action == "source_propose":
            ident = uuid.uuid4().hex
            with self._connection() as connection:
                connection.execute("INSERT INTO monitor_source_proposals VALUES(?,?,?,?,?,?,?)", (
                    ident, actor, chat_id, json.dumps(payload), "pending_rights", stamp, stamp + 86400))
            return {"proposal_id": ident, "proposal_state": "pending_rights", "url": payload["url"],
                "message": "Кандидат " + ident + " сохранён: " + payload["url"]
                    + "\nСтатус: ожидает проверки раздела и прав. Источник не активирован."}
        if action == "source_settings":
            if "confirm" in payload:
                return self._confirm_filters(payload["confirm"], actor, chat_id, stamp)
            current = state.read()
            if current["settings"]["source"] not in {"nasa_svs", "open_science"}:
                raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
            ident = uuid.uuid4().hex
            proposal = {**payload, "revision": current["revision"],
                        "settings_hash": fingerprint(current["settings"]), "source_mode": current["settings"]["source"]}
            message = ("Изменится общий фильтр режима " + current["settings"]["source"]
                + " (" + payload["field"] + ").\nБыло: "
                + (", ".join(current["settings"]["nasa"][payload["field"]]) or "без фильтра")
                + "\nБудет: " + (", ".join(payload["values"]) if payload["values"] else "без фильтра (список будет очищен)")
                + "\nДля применения на паузе в течение 10 минут: подтверждаю " + ident)
            if len(message) > 3500:
                raise AgentError("MONITOR_FILTER_PREVIEW_TOO_LARGE", 409)
            with self._connection() as connection:
                connection.execute("INSERT INTO monitor_source_proposals VALUES(?,?,?,?,?,?,?)", (
                    ident, actor, chat_id, json.dumps(proposal), "awaiting_confirmation", stamp, stamp + 600))
            return {"proposal_id": ident, "proposal_state": "awaiting_confirmation", "field": payload["field"],
                "before": current["settings"]["nasa"][payload["field"]], "after": payload["values"],
                "source_mode": current["settings"]["source"],
                "message": message}
        raise AgentError("MONITOR_COMMAND_UNSUPPORTED", 422)

    def _diagnose(self, target_id):
        with self.core.db.session() as session:
            target = session.get(Target, target_id)
            if target is None:
                raise AgentError("TARGET_NOT_FOUND", 404)
            dispatches = list(session.scalars(select(Dispatch).join(Variant).where(Variant.target_id == target_id)))
            problems = []
            if any(value.state in {"sending", "unknown"} for value in dispatches):
                problems.append("Есть отправка с неизвестным исходом: требуется сверка.")
            if any(value.owner is not None or value.lease_until != 0 for value in dispatches):
                problems.append("Сохранено право выполняющегося отправителя; оно не сброшено.")
            if session.scalar(select(BridgePost.post_id).where(BridgePost.target_id == target_id,
                    BridgePost.mutation_owner.is_not(None) | (BridgePost.mutation_until != 0)
                    | BridgePost.mutation_until.is_(None))):
                problems.append("Есть незавершённая мутация Postiz; требуется сверка.")
            return {"target_id": target_id, "enabled": target.enabled, "paused": target.paused,
                "problems": problems, "state": "blocked" if problems else "done", "changed": False,
                "message": "Адресат " + target_id + ": " + ("включён" if target.enabled else "выключен")
                    + ", " + ("на паузе" if target.paused else "пауза снята") + ".\n"
                    + "\n".join(problems or ["Сохранённых unknown/sending и mutation lease не найдено."])
                    + "\nПауза, права и отказы качества сохранены. Вход/модерация требуют отдельной проверки в панели."}

    def _confirm_filters(self, ident, actor, chat_id, stamp):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute("SELECT * FROM monitor_source_proposals WHERE id=?", (ident,)).fetchone()
            if (proposal is None or proposal["actor"] != actor or proposal["chat_id"] != chat_id
                    or proposal["state"] != "awaiting_confirmation" or proposal["expires_at"] < stamp):
                raise AgentError("MONITOR_PROPOSAL_INVALID", 409)
            connection.execute("UPDATE monitor_source_proposals SET state='running' WHERE id=?", (ident,))
        try:
            payload = json.loads(proposal["payload"])
            state = ServerState(self.core)
            current = state.read()
            if current["revision"] != payload["revision"] or fingerprint(current["settings"]) != payload["settings_hash"]:
                raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
            if not current["paused"]:
                raise AgentError("EDITORIAL_PAUSE_BEFORE_SETTINGS", 409)
            with self.core.db.session() as session:
                if (session.scalar(select(Dispatch.id).where(Dispatch.state.in_({"sending", "unknown"})
                        | Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0) | Dispatch.lease_until.is_(None)))
                        or session.scalar(select(BridgePost.post_id).where(BridgePost.mutation_owner.is_not(None)
                            | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))):
                    raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            settings = copy.deepcopy(current["settings"])
            settings["nasa"][payload["field"]] = payload["values"]
            state.settings(validate_settings(settings), current["revision"], _editorial_actor(actor),
                           require_quiescent=True)
        except AgentError:
            with self._connection() as connection:
                connection.execute("UPDATE monitor_source_proposals SET state='blocked' WHERE id=?", (ident,))
            raise
        except Exception:
            # running означает неизвестный исход и запрещает второе применение.
            raise
        with self._connection() as connection:
            connection.execute("UPDATE monitor_source_proposals SET state='applied' WHERE id=?", (ident,))
        return {"proposal_id": ident, "proposal_state": "applied", "field": payload["field"],
                "message": ("Фильтр обновлён" if payload["values"] else "Фильтр очищен")
                    + " для будущей подготовки; история и права не менялись."}

    def run_one(self, *, now_at=None, worker_owned=False):
        """Один проход подготовки; никогда не вызывает run_once/publish/dispatch."""
        stamp = time.time() if now_at is None else now_at
        self._guard_path()
        self._enabled()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = connection.execute("SELECT value FROM monitor_command_controls WHERE name='refill_paused'").fetchone()
            if control is not None and control[0] == "true":
                return {"state": "paused", "message": "Задания пополнения из чата приостановлены."}
            cancelled = connection.execute("SELECT j.id,j.result FROM monitor_command_jobs j "
                "JOIN monitor_command_cancellations c ON c.command_id=j.id WHERE j.state='queued'").fetchall()
            for previous in cancelled:
                result = json.loads(previous["result"])
                result.update(code="MONITOR_TASK_CANCELLED", cancellation_requested=True,
                    message="Задание отменено владельцем до начала следующего прохода.")
                connection.execute("UPDATE monitor_command_jobs SET state='cancelled',result=?,updated_at=?,next_at=0 WHERE id=?",
                                   (json.dumps(result, ensure_ascii=False), stamp, previous["id"]))
            unresolved = connection.execute("SELECT id FROM monitor_command_jobs WHERE state IN ('running','unknown') "
                                            "ORDER BY created_at,id LIMIT 1").fetchone()
            if unresolved is not None:
                return {"state": "unknown", "command_id": unresolved["id"],
                        "message": "Ранее начатая команда требует сверки; новое пополнение заблокировано."}
            row = connection.execute("SELECT * FROM monitor_command_jobs WHERE state='queued' AND next_at<=? "
                                     "ORDER BY created_at,id LIMIT 1", (stamp,)).fetchone()
            if row is None:
                running = connection.execute("SELECT id FROM monitor_command_jobs WHERE state='running' LIMIT 1").fetchone()
                return {"state": "unknown" if running is not None else "idle"}
            connection.execute("UPDATE monitor_command_jobs SET state='running',updated_at=?,passes=passes+1 WHERE id=?",
                               (stamp, row["id"]))
        payload = json.loads(row["payload"])
        try:
            if (row["config_hash"] != fingerprint(self.options) or not self._actor_allowed(row["actor"])
                    or "refill" not in self.options.get("allowed_actions", [])):
                raise AgentError("MONITOR_COMMAND_CONFIG_CHANGED", 409)
            result = (self._refill(payload["target"], stamp, worker_owned=True) if worker_owned
                      else self._refill(payload["target"], stamp))
            phase = result["phase"]
            next_at = result.get("next_run_at") or stamp + 30
            if result.get("ready", 0) >= payload["target"]:
                state = "done"
            elif phase in {"paused", "reserve_disabled", "blocked"}:
                state = "blocked"
            elif phase in {"worker_busy", "waiting_source", "waiting_services"}:
                state = "queued"
                with self._connection() as connection:
                    connection.execute("UPDATE monitor_command_jobs SET passes=passes-1 WHERE id=?", (row["id"],))
            elif row["passes"] >= 3:
                state = "blocked"
                result["message"] = "Лимит четырёх проходов достигнут; цель пополнения не достигнута, результаты сохранены."
            elif phase in {"reserve_refilled", "no_source", "preparation_deferred"}:
                state = "queued"
            else:
                state = "blocked"
            if self._cancellation_requested(row["id"]):
                result["cancellation_requested"] = True
                if state == "queued":
                    state = "cancelled"
                    result["code"] = "MONITOR_TASK_CANCELLED"
                    result["message"] = "Текущий проход завершён с известным результатом; будущие проходы отменены владельцем."
            if type(result.get("ready")) is int:
                result["goal_reached"] = result["ready"] >= payload["target"]
                result["message"] = (result.get("message", "Проход подготовки завершён.")
                    + "\nГотово: " + str(result["ready"]) + "; цель команды: " + str(payload["target"])
                    + ". " + ("Запрос остаётся в очереди." if state == "queued" else
                        "Цель достигнута." if result["goal_reached"] else "Цель не достигнута."))
            state, result = self._finish(row["id"], state, result, stamp, next_at=next_at if state == "queued" else 0)
        except AgentError as error:
            if error.code == "BROWSER_PROFILE_BUSY":
                state, result = "queued", {"phase": "worker_busy",
                    "message": "Другой обработчик владеет циклом; запрос остаётся в очереди."}
                with self._connection() as connection:
                    connection.execute("UPDATE monitor_command_jobs SET passes=passes-1 WHERE id=?", (row["id"],))
                if self._cancellation_requested(row["id"]):
                    state = "cancelled"
                    result.update(code="MONITOR_TASK_CANCELLED", cancellation_requested=True,
                        message="Задание отменено до начала подготовки; будущие проходы не запустятся.")
                state, result = self._finish(row["id"], state, result, stamp, next_at=stamp + 30 if state == "queued" else 0)
            else:
                known = error.code in _ERROR_CODES
                state = "blocked" if known else "unknown"
                result = (_error_reply(error) if known else {"code": _safe_code(error),
                    "message": "Исход подготовки требует сверки; повтор заблокирован. Отправка не запускалась."})
                state, result = self._finish(row["id"], state, result, stamp)
        except Exception:
            state, result = "unknown", {"code": "MONITOR_REFILL_RESULT_UNKNOWN",
                "message": "Исход подготовки неизвестен. Повтор автоматически запрещён."}
            state, result = self._finish(row["id"], state, result, stamp)
        return {"command_id": row["id"], "state": state, "action": "refill", **result}

    def _refill(self, desired, stamp, *, worker_owned=False):
        state = ServerState(self.core)
        current = state.read()
        options = current["settings"]["reserve"]
        if current["paused"] or not options["enabled"]:
            failed = refill_preflight(self.core, desired)
            return {"phase": "paused" if current["paused"] else "reserve_disabled",
                "code": "MONITOR_LIVE_REFILL_NOT_READY", "blockers": failed["blockers"],
                "inference_verified": False,
                "message": "Живое пополнение не запускалось:\n" + "\n".join(value["message"] for value in failed["blockers"])
                    + "\nОбязательны исправленный MADLAD, независимый Ministral и локальный LanguageTool. Проверки не обходились."}
        if desired > min(12, options["maximum"]):
            raise AgentError("MONITOR_REFILL_LIMIT", 409)
        # Серверный preparation worker уже держит PG lock: проверяем именно
        # сохранённый fence, а не доверяем флагу из сообщения. Отдельный монитор
        # должен сам получить штатный advisory/file lock.
        fence = getattr(self.core, "editorial_leader_check", None) if worker_owned else None
        if worker_owned:
            if not callable(fence):
                raise AgentError("EDITORIAL_LEADERSHIP_LOST", 409)
            fence()
        lock = nullcontext(True) if worker_owned else state.worker_lock()
        with lock as acquired:
            if not acquired:
                return {"phase": "worker_busy", "message": "Другой обработчик владеет циклом; запрос остаётся в очереди."}
            prerequisites = refill_preflight(self.core, desired, check_services=True)
            if not prerequisites["ready"]:
                waiting = {value["code"] for value in prerequisites["blockers"]} == {"MONITOR_MADLAD_BUSY"}
                return {"phase": "waiting_services" if waiting else "blocked", "code": "MONITOR_LIVE_REFILL_NOT_READY",
                    "blockers": prerequisites["blockers"], "inference_verified": False,
                    "message": "Живое пополнение не запускалось:\n"
                        + "\n".join(value["message"] for value in prerequisites["blockers"])}
            cycle = HourlyArticleCycle(self.core, reserve_mode=True)
            policy = cycle._policy()
            def count():
                return sum(ready_for_policy(record, policy) for record in cycle._load()["records"])
            before = count()
            if before >= desired:
                return {"phase": "reserve_satisfied", "ready": before,
                        "message": "Нужный запас уже готов. Новые источники не запрашивались."}
            view = _RefillState(state, desired, self._refill_paused)
            controller = ReserveServerCycle(self.core, state=PreparationState(view), cycle=cycle,
                                            clock=time.time)
            controller._refilling = True
            result = controller.prepare_once()
            # Только допустимые счётчики/phase: URL/исходные отказы моделей не
            # попадают в уведомление из raw результата контроллера.
            return {"phase": result["phase"], "ready": count(), "ready_before": before,
                    "next_run_at": result.get("next_run_at"),
                    "message": "Завершён ограниченный проход подготовки; отправка не запускалась."}

    def _refill_paused(self):
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM monitor_command_controls WHERE name='refill_paused'").fetchone()
            return row is not None and row[0] == "true"
