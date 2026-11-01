"""Чистый словарь MAX-команд: разбор текста не даёт прав и ничего не исполняет."""

import ipaddress
import re
from urllib.parse import urlsplit


COMMAND_SPECS = (
    {"action": "help", "syntax": "команды", "description": "разрешённые команды и ограничения",
     "aliases": ("команды", "помощь")},
    {"action": "status", "syntax": "статус", "description": "сохранённое состояние проекта",
     "aliases": ("статус", "покажи состояние", "состояние")},
    {"action": "reserve", "syntax": "резерв", "description": "готовые статьи и цель резерва",
     "aliases": ("резерв", "покажи резерв")},
    {"action": "errors", "syntax": "ошибки", "description": "сохранённые причины блокировок",
     "aliases": ("ошибки", "покажи ошибки")},
    {"action": "sources", "syntax": "источники", "description": "источники и действующие фильтры",
     "aliases": ("источники", "покажи источники")},
    {"action": "refill", "syntax": "пополни резерв до N", "description": "подготовка до цели от 1 до 12 без отправки статей",
     "aliases": ("пополни резерв до N", "пополнить резерв до N")},
    {"action": "tasks", "syntax": "задачи", "description": "последние 10 заданий пополнения",
     "aliases": ("задачи", "покажи задачи")},
    {"action": "task", "syntax": "задача ID", "description": "состояние задания по ID",
     "aliases": ("задача ID",)},
    {"action": "cancel", "syntax": "отмени задачу ID",
     "description": "отмена ожидающего задания; у выполняющегося — запрос остановки будущих проходов",
     "aliases": ("отмени задачу ID",)},
    {"action": "resume_refill", "syntax": "продолжи подготовку",
     "description": "возобновить только разрешённые будущие проходы пополнения",
     "aliases": ("продолжи подготовку", "возобнови подготовку")},
    {"action": "resume_project", "syntax": "продолжи проект",
     "description": "снять общую паузу только при паузе всех адресатов, без включения отправки",
     "aliases": ("продолжи проект", "сними общую паузу")},
    {"action": "diagnostics", "syntax": "диагностика", "description": "готовность зависимостей без запуска перевода/отправки",
     "aliases": ("диагностика", "проверка")},
    {"action": "pause", "syntax": "пауза [подготовки|TARGET_ID]", "description": "пауза проекта, пополнения или адресата",
     "aliases": ("пауза", "поставь проект на паузу", "останови проект", "пауза подготовки",
                 "останови подготовку", "поставь подготовку на паузу", "пауза TARGET_ID")},
    {"action": "reconcile", "syntax": "сверь DISPATCH_ID", "description": "сверить прежнюю попытку без новой отправки",
     "aliases": ("сверь DISPATCH_ID", "проверь отправку DISPATCH_ID", "сверка DISPATCH_ID")},
    {"action": "unblock", "syntax": "сними блокировку TARGET_ID", "description": "диагностика без сброса защиты и неизвестного исхода",
     "aliases": ("сними блокировку TARGET_ID", "почему заблокирован TARGET_ID", "блокировка TARGET_ID")},
    {"action": "source_propose", "syntax": "добавь источник https://example.org/section/",
     "description": "кандидат на проверку прав; URL не загружается и источник не включается",
     "aliases": ("добавь источник URL", "добавить источник URL")},
    {"action": "source_settings", "syntax": "измени фильтр ключевые слова: слова",
     "description": "точное предложение фильтра с отдельным подтверждением на паузе",
     "aliases": ("измени фильтр ключевые слова: слова", "измени фильтр авторы: имена",
                 "очисти фильтр ключевые слова", "очисти фильтр авторы", "подтверждаю TOKEN")},
)

OWNER_ACTIONS = frozenset(spec["action"] for spec in COMMAND_SPECS)
_FIXED = {alias: spec["action"] for spec in COMMAND_SPECS
    if spec["action"] in {"help", "status", "reserve", "errors", "sources", "tasks", "resume_refill", "resume_project", "diagnostics"}
    for alias in spec["aliases"]}


def parse_command_text(text):
    """Только лексический payload; gateway отдельно проверяет права и значения."""
    if not isinstance(text, str) or not 1 <= len(text) <= 1200:
        return None
    normalized = " ".join(text.strip().split()).removeprefix("/")
    lower = normalized.lower().replace("ё", "е")
    if lower in _FIXED:
        return {"action": _FIXED[lower]}
    match = re.fullmatch(r"(?:пополни|пополнить) резерв до ([0-9]{1,2})", lower)
    if match and 1 <= int(match[1]) <= 12:
        return {"action": "refill", "target": int(match[1])}
    match = re.fullmatch(r"задача ([a-f0-9]{32})", lower)
    if match:
        return {"action": "task", "command_id": match[1]}
    match = re.fullmatch(r"отмени задачу ([a-f0-9]{32})", lower)
    if match:
        return {"action": "cancel", "command_id": match[1]}
    if lower in {"пауза", "поставь проект на паузу", "останови проект"}:
        return {"action": "pause", "scope": "project"}
    if lower in {"пауза подготовки", "останови подготовку", "поставь подготовку на паузу"}:
        return {"action": "pause", "scope": "monitor_refill"}
    match = re.fullmatch(r"пауза ([A-Za-z0-9_-]{1,80})", normalized, re.IGNORECASE)
    if match:
        return {"action": "pause", "scope": "target", "target_id": match[1]}
    match = re.fullmatch(r"(?:сверь|проверь отправку|сверка) ([A-Za-z0-9_-]{1,80})", normalized, re.IGNORECASE)
    if match:
        return {"action": "reconcile", "dispatch_id": match[1]}
    match = re.fullmatch(r"(?:сними блокировку|почему заблокирован|блокировка) ([A-Za-z0-9_-]{1,80})",
                         normalized, re.IGNORECASE)
    if match:
        return {"action": "unblock", "target_id": match[1]}
    match = re.fullmatch(r"(?:добавь источник|добавить источник) (https://\S+)", normalized, re.IGNORECASE)
    if match:
        return {"action": "source_propose", "url": match[1]}
    match = re.fullmatch(r"очисти фильтр (ключевые слова|авторы)", lower)
    if match:
        return {"action": "source_settings", "field": "authors" if match[1] == "авторы" else "keywords",
                "values": []}
    match = re.fullmatch(r"измени фильтр (ключевые слова|авторы):\s*(.+)", normalized, re.IGNORECASE)
    if match:
        return {"action": "source_settings", "field": "authors" if match[1].lower() == "авторы" else "keywords",
                "values": [value.strip() for value in match[2].split(",")]}
    match = re.fullmatch(r"подтверждаю ([a-f0-9]{32})", lower)
    if match:
        return {"action": "source_settings", "confirm": match[1]}
    return None


def classify_command(text):
    parsed = parse_command_text(text)
    return parsed["action"] if parsed is not None else None


def source_url_is_safe(value):
    """Локальная проверка URL-кандидата: ни DNS, ни HTTP, ни загрузки источника."""
    try:
        if not isinstance(value, str):
            return False
        parsed = urlsplit(value)
        host = parsed.hostname
        if (len(value) > 1000 or parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443} or parsed.query or parsed.fragment
                or any(character in value for character in "\\?#")
                or host.lower() in {"localhost", "localhost.localdomain"} or "." not in host
                or host.endswith((".local", ".localhost", ".internal"))
                or any(character.isspace() or ord(character) < 32 for character in value)):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        return False
    except (TypeError, ValueError):
        return False
