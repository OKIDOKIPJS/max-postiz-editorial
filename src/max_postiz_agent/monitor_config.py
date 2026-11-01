"""Локальные настройки наблюдателя: отдельные профиль, журнал и адресат."""

import copy
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentError, fingerprint, parse_json
from .isolated_workspace import assert_isolated
from .monitor_command_manifest import OWNER_ACTIONS

COMMAND_ACTIONS = set(OWNER_ACTIONS)


def default_config():
    return {
        "schema_version": "monitor-browser-v1", "enabled": False,
        "report_chat_id": "", "report_chat_name": "", "report_private_confirmed": False,
        "channel": "chrome", "headless": True, "panel_url": "",
        "hourly_enabled": True, "hourly_delay_seconds": 180,
        "daily_enabled": True, "daily_hour": 9, "poll_seconds": 30,
        "report_max_age_seconds": 900,
        "commands": {"enabled": False, "control_chat_id": "", "control_chat_name": "",
            "account_user_id": "", "owner_ids": [], "max_age_seconds": 300,
            "allowed_actions": sorted(COMMAND_ACTIONS), "dom_contract_verified": False,
            "dom_contract_sha256": "", "dom_contract": {},
            "auth_mode": "numeric_sender", "account_sha256": "", "reply_in_control_chat": False},
    }


def private_path(root, relative):
    """Не позволяем локальным данным оказаться в чужом профиле через link/junction."""
    root = Path(root).resolve()
    if not isinstance(relative, str) or ":" in relative or Path(relative).is_absolute():
        raise AgentError("MONITOR_PATH_INVALID")
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise AgentError("MONITOR_PATH_INVALID")
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise AgentError("MONITOR_STATE_LINK_FORBIDDEN")
        if current.is_file() and current.stat().st_nlink > 1:
            raise AgentError("MONITOR_STATE_LINK_FORBIDDEN")
    return path


def validate_config(value):
    defaults = default_config()
    if not isinstance(value, dict) or set(value) != set(defaults):
        raise AgentError("MONITOR_CONFIG_INVALID")
    for field in ("enabled", "report_private_confirmed", "headless", "hourly_enabled", "daily_enabled"):
        if type(value[field]) is not bool:
            raise AgentError("MONITOR_CONFIG_INVALID")
    limits = {"hourly_delay_seconds": (0, 1800), "daily_hour": (0, 23),
              "poll_seconds": (10, 300), "report_max_age_seconds": (60, 3600)}
    for field, (low, high) in limits.items():
        if type(value[field]) is not int or not low <= value[field] <= high:
            raise AgentError("MONITOR_CONFIG_INVALID")
    if value["schema_version"] != defaults["schema_version"] or value["channel"] not in {"chrome", "chromium"}:
        raise AgentError("MONITOR_CONFIG_INVALID")
    for field in ("report_chat_id", "report_chat_name", "panel_url"):
        if not isinstance(value[field], str) or len(value[field]) > 512:
            raise AgentError("MONITOR_CONFIG_INVALID")
    if len(value["report_chat_name"]) > 200:
        raise AgentError("MONITOR_CONFIG_INVALID")
    if value["report_chat_id"] and not re.fullmatch(r"-?[0-9]{1,22}", value["report_chat_id"]):
        raise AgentError("MONITOR_CONFIG_INVALID")
    if any(ord(c) < 32 for c in value["report_chat_name"]):
        raise AgentError("MONITOR_CONFIG_INVALID")
    if value["panel_url"]:
        if any(c.isspace() or ord(c) < 32 for c in value["panel_url"]):
            raise AgentError("MONITOR_CONFIG_INVALID")
        try:
            parsed = urlsplit(value["panel_url"])
        except ValueError as exc:
            raise AgentError("MONITOR_CONFIG_INVALID") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise AgentError("MONITOR_CONFIG_INVALID")
    if value["enabled"] and (not value["report_chat_id"] or not value["report_chat_name"].strip()
                              or value["report_private_confirmed"] is not True):
        raise AgentError("MONITOR_PRIVATE_DESTINATION_REQUIRED")
    # Старые JSON не требуют переписывания: два новых поля имеют прежний numeric режим.
    value = copy.deepcopy(value)
    commands = value["commands"]
    if isinstance(commands, dict):
        commands.setdefault("auth_mode", "numeric_sender")
        commands.setdefault("account_sha256", "")
        commands.setdefault("reply_in_control_chat", False)
    if not isinstance(commands, dict) or set(commands) != set(defaults["commands"]):
        raise AgentError("MONITOR_COMMAND_CONFIG_INVALID")
    if any(type(commands[k]) is not bool for k in ("enabled", "dom_contract_verified", "reply_in_control_chat")):
        raise AgentError("MONITOR_COMMAND_CONFIG_INVALID")
    if (not isinstance(commands["auth_mode"], str)
            or commands["auth_mode"] not in {"numeric_sender", "authenticated_account_outgoing"}
            or not isinstance(commands["account_sha256"], str)
            or commands["account_sha256"] and not re.fullmatch(r"[a-f0-9]{64}", commands["account_sha256"])):
        raise AgentError("MONITOR_COMMAND_CONFIG_INVALID")
    for field in ("control_chat_id", "account_user_id"):
        pattern = r"[0-9]{1,22}" if field == "account_user_id" else r"-?[0-9]{1,22}"
        if (not isinstance(commands[field], str) or commands[field]
                and not re.fullmatch(pattern, commands[field])):
            raise AgentError("MONITOR_COMMAND_CONFIG_INVALID")
    if (not isinstance(commands["control_chat_name"], str)
            or len(commands["control_chat_name"]) > 200
            or any(ord(c) < 32 for c in commands["control_chat_name"])):
        raise AgentError("MONITOR_COMMAND_CONFIG_INVALID")
    owners, actions = commands["owner_ids"], commands["allowed_actions"]
    if (not isinstance(owners, list) or len(owners) > 10
            or any(not isinstance(x, str) or not re.fullmatch(r"[0-9]{1,22}", x) for x in owners)
            or len(set(owners)) != len(owners) or not isinstance(actions, list)
            or any(not isinstance(x, str) or x not in COMMAND_ACTIONS for x in actions)
            or len(set(actions)) != len(actions)
            or type(commands["max_age_seconds"]) is not int
            or not 30 <= commands["max_age_seconds"] <= 900
            or not isinstance(commands["dom_contract"], dict)
            or not isinstance(commands["dom_contract_sha256"], str)):
        raise AgentError("MONITOR_COMMAND_CONFIG_INVALID")
    if commands["dom_contract_verified"] and (
            commands["dom_contract_sha256"] != fingerprint(commands["dom_contract"])):
        raise AgentError("MONITOR_COMMAND_CONTRACT_CHANGED")
    outgoing = commands["auth_mode"] == "authenticated_account_outgoing"
    if outgoing and (not commands["account_sha256"]
            or not set(actions) <= OWNER_ACTIONS):
        raise AgentError("MONITOR_OWNER_COMMAND_SCOPE_INVALID")
    reply_in_control = commands["reply_in_control_chat"]
    same_chat = bool(value["report_chat_id"] and commands["control_chat_id"]
                     and commands["control_chat_id"] == value["report_chat_id"])
    accepted_owner = False
    if outgoing and commands["dom_contract_verified"]:
        from .monitor_owner_chat import owner_contract_hash
        accepted_owner = owner_contract_hash(commands["dom_contract"]) == commands["dom_contract_sha256"]
    if same_chat and (not outgoing or not reply_in_control or not accepted_owner
            or value["report_private_confirmed"] is not True
            or value["report_chat_name"] != commands["control_chat_name"]
            or not value["report_chat_name"].strip()):
        raise AgentError("MONITOR_COMMAND_PAIRING_REQUIRED")
    if reply_in_control and (not commands["control_chat_id"] or not commands["control_chat_name"].strip()
            or not outgoing or not accepted_owner or not commands["dom_contract_verified"]):
        raise AgentError("MONITOR_COMMAND_PAIRING_REQUIRED")
    if commands["enabled"] and (not value["enabled"] or (not owners and not outgoing)
            or not commands["control_chat_id"] or not commands["control_chat_name"].strip()
            or (not commands["account_user_id"] and not outgoing) or not commands["dom_contract_verified"]):
        raise AgentError("MONITOR_COMMAND_PAIRING_REQUIRED")
    return copy.deepcopy(value)


class MonitorConfig:
    def __init__(self, root, value):
        self.root = Path(root).resolve()
        self.value = validate_config(value)
        self.hash = fingerprint(self.value)

    @classmethod
    def load(cls, root):
        assert_isolated(root)
        try:
            path = private_path(root, "config/monitor.json")
            return cls(root, parse_json(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError("MONITOR_CONFIG_UNAVAILABLE") from exc

    def browser_options(self):
        return {**self.value, **self.value["commands"], "reports_enabled": self.value["enabled"],
                "monitor_config_hash": self.hash}

    def command_options(self):
        return {**self.value["commands"], "enabled": self.value["enabled"]
                and self.value["commands"]["enabled"]}

    def current(self):
        return self.load(self.root).hash == self.hash


def initialize_monitor_config(root):
    assert_isolated(root)
    path = private_path(root, "config/monitor.json")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(default_config(), stream, ensure_ascii=False, indent=2)
        if os.name != "nt":
            path.chmod(0o600)
    return MonitorConfig.load(root)


def bind_created_channel(config, result):
    """Сохраняем только сверенный Create; отчёты и команды остаются выключенными."""
    if (config.value["enabled"] or not config.current() or result.get("state") != "confirmed"
            or result.get("name") != config.value["report_chat_name"]
            or not isinstance(result.get("chat_id"), str)
            or not re.fullmatch(r"-?[0-9]{1,22}", result["chat_id"])):
        raise AgentError("MONITOR_CHANNEL_BINDING_INVALID")
    value = copy.deepcopy(config.value)
    value.update(report_chat_id=result["chat_id"], report_private_confirmed=True)
    validate_config(value)
    temporary = private_path(config.root, "data/monitor/config-" + uuid.uuid4().hex + ".json")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if not config.current():
            raise AgentError("MONITOR_CONFIG_CHANGED")
        os.replace(temporary, private_path(config.root, "config/monitor.json"))
    finally:
        temporary.unlink(missing_ok=True)
    return MonitorConfig.load(config.root)
