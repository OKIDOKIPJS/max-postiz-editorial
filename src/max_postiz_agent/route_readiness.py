"""Локальные предпосылки маршрутов; не проверяет доступ, БД или доставку."""

import importlib.util
import os
from pathlib import Path
import re
import sys

from .config import AgentError, Settings, parse_json, validate_schema
from .isolated_workspace import _plain_path, assert_isolated
from .platform_routes import validate_route


_LINUX = sys.platform == "linux"


def _linux_support():
    from .browser_process import linux_runtime_status

    try:
        value = linux_runtime_status()
    except (OSError, TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    # Only boolean hints are public. Creation/assignment are never exercised by doctor.
    return {**{key: value.get(key) is True for key in ("supported", "writable", "cgroup_kill")},
            "delegation_exercised": False}


def _read(root, relative):
    path = _plain_path(root, relative)
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise AgentError("READINESS_CONFIG_SIZE_INVALID")
    return parse_json(raw.decode("utf-8-sig"))


def _profile(root, relative):
    path = _plain_path(root, relative)
    if not path.is_relative_to(root / "data") or path == root / "data":
        raise AgentError("READINESS_PROFILE_INVALID")
    return path


def _browser(row, root, options, platform):
    relative = options.get("profile_dir", "data/" + platform + "-browser")
    profile = _profile(root, relative)
    if platform in {"vk", "pikabu"} and (
            profile.parent != root / "data" or not profile.name.startswith(platform + "-browser")):
        raise AgentError("READINESS_PROFILE_INVALID")
    row["profile_exists"] = profile.is_dir()
    row["browser_dependency_present"] = importlib.util.find_spec("playwright") is not None
    if not row["profile_exists"]:
        row["blockers"].append("BROWSER_PROFILE_MISSING")
    if not row["browser_dependency_present"]:
        row["blockers"].append("BROWSER_DEPENDENCY_MISSING")
    row["browser_binary"] = "not_checked"
    row["browser_login"] = "not_checked"
    if _LINUX:
        runtime = _linux_support()
        row["linux_runtime"] = runtime
        row["browser_process_cleanup"] = "not_exercised"
        if not all(runtime[key] for key in ("supported", "writable", "cgroup_kill")):
            row["blockers"].append("BROWSER_CGROUP_DELEGATION_REQUIRED")


def _target(root, settings, value, row):
    options, route = value.get("options", {}), value["route"]
    row.update({"id": value["id"], "platform": value["platform"], "route": route,
           "enabled": value["enabled"], "configured": False, "local_prerequisites_passed": False,
           "access": "not_checked", "delivery": "not_checked", "pause_state": "not_checked",
           "approval": "not_checked", "mutation_lease": "not_checked", "unresolved_dispatches": "not_checked",
           "blockers": [], "warnings": []})
    validate_route(value)
    channel = settings.channel(value["profile_id"])
    row["profile_enabled"] = channel["enabled"]
    row["publication_mode"] = channel["publication"]["mode"]
    if not value["enabled"]:
        row["blockers"].append("TARGET_DISABLED")
    if not channel["enabled"]:
        row["blockers"].append("PROFILE_DISABLED")
    if channel["publication"]["mode"] == "dry_run":
        row["blockers"].append("PROFILE_DRY_RUN")
    destination = value["destination"]
    if not destination:
        raise AgentError("TARGET_DESTINATION_MISSING")
    if route in {"telegram_api", "vk_api", "max_api"}:
        default = {"telegram_api": "TELEGRAM_BOT_TOKEN", "vk_api": "VK_ACCESS_TOKEN", "max_api": "MAX_BOT_TOKEN"}[route]
        api_options = settings.app["max"] if route == "max_api" else options
        row["adapter_mode"] = api_options.get("adapter_mode", "fake")
        row["credentials_present"] = bool(os.getenv(api_options.get("token_env", default)))
        if row["adapter_mode"] != "live":
            row["blockers"].append("ADAPTER_NOT_LIVE")
        if not row["credentials_present"]:
            row["blockers"].append("CREDENTIAL_MISSING")
        if route == "telegram_api":
            from .telegram_publisher import TelegramPublisher, _configured_proxy
            TelegramPublisher._destination(destination)
            _configured_proxy(options)
            row["proxy_configured"] = "proxy_env" in options
        elif route == "vk_api":
            from .vk_publisher import _community
            _community(destination)
            row["warnings"].append("VK_USER_WALL_PHOTOS_PERMISSION_NOT_CHECKED")
        elif not re.fullmatch(r"-?[1-9][0-9]*", destination):
            raise AgentError("TARGET_DESTINATION_INVALID")
    elif route in {"vk_browser", "pikabu_browser"}:
        row["adapter_mode"] = "browser"
        row["owner_configured"] = bool(options.get("owner"))
        row["dom_contract_verified"] = options.get("dom_contract_verified") is True
        row["dom_contract_complete"] = False
        row["media_contract_verified"] = options.get("media_contract_verified") is True
        _browser(row, root, options, value["platform"])
        if route == "vk_browser":
            from .vk_browser import dom_contract, owner_id, profile_path, wall_url
            owner_id(options)
            profile_path(root, options)
            wall_url(destination, options)
            if options.get("dom_contract_profile") in {"personal-text-v1", "personal-image-v1"} and destination != options["owner"]:
                raise AgentError("VK_BROWSER_PERSONAL_WALL_REQUIRED")
            try:
                dom_contract({**options, "dom_contract_verified": True}, publish=True)
                row["dom_contract_complete"] = True
            except AgentError:
                row["blockers"].append("VK_BROWSER_DOM_CONTRACT_INCOMPLETE")
            if options.get("dom_contract_profile") == "personal-text-v1":
                row["media_contract_verified"] = False
                row["warnings"].append("VK_BROWSER_PROFILE_TEXT_ONLY")
            elif options.get("dom_contract_profile") == "personal-image-v1":
                from .vk_media import require_contract

                row["media_contract_verified"] = False
                try:
                    selectors = dom_contract(options, publish=True)
                    require_contract(root, options, selectors, options["owner"])
                    row["media_contract_verified"] = True
                except AgentError:
                    row["blockers"].append("VK_BROWSER_MEDIA_EVIDENCE_UNVERIFIED")
        else:
            from .pikabu_browser_worker import _dom
            from .pikabu_delivery import owner_name
            owner = owner_name(options.get("owner"))
            if destination not in {owner, "user:" + owner, "https://pikabu.ru/@" + owner}:
                raise AgentError("PIKABU_DESTINATION_OWNER_MISMATCH")
            try:
                _dom({**options, "dom_contract_verified": True}, publish=True)
                row["dom_contract_complete"] = True
            except AgentError:
                row["blockers"].append("PIKABU_DOM_CONTRACT_INCOMPLETE")
        if not row["dom_contract_verified"]:
            row["blockers"].append("DOM_CONTRACT_NOT_VERIFIED")
        if not row["media_contract_verified"]:
            row["warnings"].append("MEDIA_CONTRACT_NOT_VERIFIED")
    elif route == "max_browser":
        maximum = settings.app["max"]
        row["adapter_mode"] = maximum["adapter_mode"]
        if row["adapter_mode"] != "browser":
            row["blockers"].append("ADAPTER_NOT_BROWSER")
        browser = maximum.get("browser", {})
        _browser(row, root, browser, "max")
        if not re.fullmatch(r"-?[0-9]+", destination) or not browser.get("channels", {}).get(destination):
            raise AgentError("BROWSER_CHANNEL_NOT_ALLOWED")
        row["dom_contract_complete"] = True  # Built-in selectors; actual DOM is not checked here.
    else:
        raise AgentError("READINESS_ROUTE_UNSUPPORTED")
    row["configured"] = True
    row["local_prerequisites_passed"] = not row["blockers"]
    return row


def readiness(root):
    """Только чтение файлов/наличия env; не создаёт даже локальную БД/adapter."""
    root = Path(root).resolve()
    assert_isolated(root)
    result = {"contract": "four-platform-local-readiness-v1", "read_only": True,
              "configuration_valid": False, "configured": False, "local_prerequisites_passed": False,
              "production_ready": False, "access": "not_checked", "delivery": "not_checked",
              "database_state": "not_checked", "content_quality": "not_checked", "network_requests": 0,
              "external_mutations": 0, "targets": [], "blockers": []}
    try:
        app = _read(root, "config/app.json")
        for relative in (app["topic_path"], app["sources_path"], "schemas/topic.schema.json",
                         "schemas/sources.schema.json", "schemas/publishing.schema.json"):
            path = _plain_path(root, relative)
            if path.stat().st_size > 1024 * 1024:
                raise AgentError("READINESS_CONFIG_SIZE_INVALID")
        settings = Settings.load(root)
        publishing = _read(root, "config/publishing.json")
        validate_schema(root, "publishing.schema.json", publishing)
        identifiers = [value["id"] for value in publishing["targets"]]
        if len(set(identifiers)) != len(identifiers):
            raise AgentError("DUPLICATE_TARGET_ID")
        result["configuration_valid"] = True
        for value in publishing["targets"]:
            row = {}
            try:
                _target(root, settings, value, row)
            except AgentError as exc:
                row["blockers"].append(exc.code)
            result["targets"].append(row)
        result["configured"] = bool(result["targets"]) and all(row["configured"] for row in result["targets"])
        result["local_prerequisites_passed"] = bool(result["targets"]) and all(
            row["local_prerequisites_passed"] for row in result["targets"])
    except AgentError as exc:
        result["blockers"].append(exc.code)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["blockers"].append("READINESS_CONFIG_INVALID")
    return result
