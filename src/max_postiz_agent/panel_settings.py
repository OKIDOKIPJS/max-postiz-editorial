"""Просмотр и проверенное сохранение всех настроек панели; ключи доступны только для замены."""

import copy
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PureWindowsPath

from sqlalchemy import select

from .channel_profiles import PROFILE_FILE, atomic_json, profiles
from .config import AgentError, Settings, inside, parse_json, fingerprint
from .db import Intent
from .panel_files import private_roots

DEFAULT_ENV = {"DATABASE_URL", "APP_AUTH_SECRET", "APP_EDITOR_SECRET", "APP_VIEWER_SECRET",
               "MAX_AGENT_BACKUP_KEY", "PIXABAY_API_KEY", "MAX_BOT_TOKEN", "MAX_WEBHOOK_SECRET",
               "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "COMFYUI_BASE_URL",
               "OPENCODE_BASE_URL", "OPENCODE_AUTH_SECRET", "OPENCODE_MODEL_ID", "LLM_PROVIDER_API_KEY"}
SECTIONS = {"app", "topic", "sources", "image_sources", "image_channels", "workflow", "holidays"}


# Возвращаем только имена переменных и факт настройки; значения ключей не входят в ответ API.
def credential_names(settings):
    names = set(DEFAULT_ENV)
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_env") and isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", item):
                    names.add(item)
                visit(item)
    visit(settings.app)
    return names - {"PATH", "HOME", "CODEX_HOME", "PYTHONPATH", "PYTHONHOME"}


# Пути формы должны оставаться в своём разделе, а не только внутри общего проекта.
def configuration_path(root, value, directory, suffix, *, fixed=None, error="SETTINGS_PATH_INVALID"):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(character in value for character in "\\:\0\r\n")
            or any(not part or part in {".", ".."} or part.endswith((".", " "))
                   or PureWindowsPath(part).is_reserved() for part in value.split("/"))):
        raise AgentError(error)
    root = Path(root).resolve()
    exact = value == fixed
    if not exact and not (value.startswith(directory + "/") and value.endswith(suffix)):
        raise AgentError(error)
    try:
        target = inside(root, value)
    except (AgentError, OSError, RuntimeError):
        raise AgentError(error) from None
    if ((exact and target != root / fixed)
            or (not exact and not target.is_relative_to(root / directory))):
        raise AgentError(error)
    return target


# Workflow не может подменить другой уже проверенный документ или файл доступа.
def workflow_target(root, app):
    value = app.get("video", {}).get("workflow_path")
    if value is None or value == "":
        return None
    target = configuration_path(root, value, "config", ".json", error="WORKFLOW_PATH_INVALID")
    if any(target.is_relative_to(directory) for directory in private_roots(root, app, "workflow")):
        raise AgentError("WORKFLOW_PATH_INVALID")
    if target.name.casefold() == "opencode.private.json":
        raise AgentError("WORKFLOW_PATH_INVALID")
    protected = {"config/app.json", "config/sources.json", "config/image_sources.json",
                 PROFILE_FILE, "config/holidays.json", "config/publishing.json",
                 "config/reddit_intake.json", "config/translation.json",
                 "config/translation-service.json", "config/opencode.private.json", "config/monitor.json",
                 app["topic_path"], app["sources_path"]}
    protected_targets = {Path(root).resolve() / name for name in protected}
    for name in protected:
        try:
            protected_targets.add(inside(root, name))
        except (AgentError, OSError, RuntimeError):
            # Недоступная посторонняя ссылка не даёт разрешения читать её через workflow.
            continue
    if target in protected_targets:
        raise AgentError("WORKFLOW_PATH_INVALID")
    return target


# Полная конфигурация имеет один контракт для формы и расширенного JSON-редактора.
def snapshot(service):
    root, settings = service.settings.root, service.settings
    path = inside(root, "config/image_sources.json")
    if not path.exists():
        path = inside(root, "config/image_sources.example.json")
    workflow = workflow_target(root, settings.app)
    from .holidays import catalog
    return {"configuration": copy.deepcopy({"app": settings.app, "topic": settings.topic,
            "sources": settings.sources, "image_sources": parse_json(path.read_text(encoding="utf-8-sig")),
            "image_channels": profiles(root), "holidays": catalog(root),
            "workflow": parse_json(workflow.read_text(encoding="utf-8-sig")) if workflow and workflow.exists() else None}),
            "credentials": [{"name": name, "configured": bool(os.getenv(name))}
                            for name in sorted(credential_names(settings))],
            "config_hash": settings.hash}


# Все взаимосвязанные настройки проверяем в изолированном каталоге до изменения установки.
def validate_configuration(root, value):
    if not isinstance(value, dict) or set(value) != SECTIONS:
        raise AgentError("SETTINGS_DOCUMENT_INVALID")
    app = value["app"]
    # Редактор конфигурации не перезаписывает исходники, схемы или служебные файлы проекта.
    configuration_path(root, app["topic_path"], "config/topics", ".md", fixed="TOPIC.md")
    configuration_path(root, app["sources_path"], "config/sources", ".json", fixed="config/sources.json")
    workflow = workflow_target(root, app)
    if value["workflow"] is not None:
        if not isinstance(value["workflow"], dict):
            raise AgentError("WORKFLOW_SCHEMA_INVALID")
        if workflow is None:
            raise AgentError("WORKFLOW_PATH_INVALID")
    from .backup import check_config_secrets
    check_config_secrets(value)
    with tempfile.TemporaryDirectory(prefix="max-settings-") as directory:
        trial = Path(directory)
        shutil.copytree(root / "schemas", trial / "schemas")
        files = {"config/app.json": app, app["sources_path"]: value["sources"],
                 "config/image_sources.json": value["image_sources"], PROFILE_FILE: value["image_channels"], "config/holidays.json": value["holidays"]}
        for name, data in files.items():
            atomic_json(trial, name, data)
        if value["workflow"] is not None:
            atomic_json(trial, app["video"]["workflow_path"], value["workflow"])
        topic = inside(trial, app["topic_path"])
        topic.parent.mkdir(parents=True, exist_ok=True)
        topic.write_text("<!-- TOPIC_CONFIG_BEGIN -->\n```json\n" + json.dumps(value["topic"], ensure_ascii=False)
                         + "\n```\n<!-- TOPIC_CONFIG_END -->", encoding="utf-8")
        candidate = Settings.load(trial)
        from .holidays import catalog
        catalog(trial)
        from .image_pipeline import source_config
        from .calendar_schedule import hours_and_days
        source_config(trial)
        for channel_id in profiles(trial)["channels"]:
            cfg = source_config(trial, channel_id)
            policy = candidate.channel(channel_id)["publication"]
            times, _ = hours_and_days(cfg)
            if len(times) > policy["max_posts_per_day"]:
                raise AgentError("SCHEDULE_DAILY_CAP_EXCEEDED")
            minutes = sorted(int(time[:2]) * 60 + int(time[3:]) for time in times)
            if any(b - a < policy["min_gap_minutes"] for a, b in zip(minutes, minutes[1:], strict=False)):
                raise AgentError("SCHEDULE_GAP_TOO_SMALL")
    return True


# Изменение конфигурации сохраняет историю; расписания нужно явно подтвердить для новой версии.
def save_configuration(service, value, *, validate_only=False):
    from .image_schedule import lock, read, write
    root = service.settings.root
    value = copy.deepcopy(value)
    if isinstance(value, dict) and isinstance(value.get("workflow"), dict):
        # Сохранение владельцем новой схемы обновляет её отпечаток вместе с файлом.
        value["app"]["video"]["workflow_sha256"] = fingerprint(value["workflow"])
    try:
        validate_configuration(root, value)
    except (KeyError, TypeError, OSError) as exc:
        raise AgentError("SETTINGS_DOCUMENT_INVALID") from exc
    if validate_only:
        return {"state": "valid"}
    with lock(root, "run"), lock(root, "control"):
        with service.db.session() as session:
            if session.scalar(select(Intent).where(Intent.state.in_(["pending", "sending", "unknown", "retry_wait"]))):
                raise AgentError("CHANNEL_UNRESOLVED_PUBLICATION", 409)
        app = value["app"]
        if not os.getenv(app["api"]["auth_secret_env"]):
            raise AgentError("PANEL_SECRET_REQUIRED")
        restart_fields = [name for name in ("state_dir", "media_dir", "logs_dir", "database_url_env")
                          if app.get(name) != service.settings.app.get(name)]
        restart_fields.extend("api." + name for name in ("host", "port")
                              if app["api"].get(name) != service.settings.app["api"].get(name))
        topic_path = configuration_path(root, app["topic_path"], "config/topics", ".md", fixed="TOPIC.md")
        configuration_path(root, app["sources_path"], "config/sources", ".json", fixed="config/sources.json")
        workflow = workflow_target(root, app)
        old_topic = inside(root, service.settings.app["topic_path"]).read_text(encoding="utf-8-sig")
        pattern = r"(<!-- TOPIC_CONFIG_BEGIN -->\s*```json\s*)(.*?)(\s*```\s*<!-- TOPIC_CONFIG_END -->)"
        text = re.sub(pattern, lambda m: m[1] + json.dumps(value["topic"], ensure_ascii=False, indent=2) + m[3], old_topic, flags=re.S)
        files = {"config/app.json": app, app["sources_path"]: value["sources"],
                 "config/image_sources.json": value["image_sources"], PROFILE_FILE: value["image_channels"], "config/holidays.json": value["holidays"]}
        workflow_path = app.get("video", {}).get("workflow_path")
        if value["workflow"] is not None:
            if workflow is None:
                raise AgentError("WORKFLOW_PATH_INVALID")
            files[workflow_path] = value["workflow"]
        targets = [inside(root, name) for name in files] + [topic_path]
        previous = {path: path.read_bytes() if path.exists() else None for path in targets}
        try:
            for name, data in files.items():
                atomic_json(root, name, data)
            topic_path.parent.mkdir(parents=True, exist_ok=True)
            topic_path.write_text(text, encoding="utf-8")
            service.activate(Settings.load(root))
        except Exception:
            for path, content in previous.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            raise
        for channel in service.settings.topic["channels"]:
            service.pause(channel["id"], True, actor="owner")
            control = read(root, "control", {}, channel["id"])
            write(root, "control", {**control, "enabled": False, "reason": "configuration_changed"}, channel["id"])
        if restart_fields:
            service.config_error = "RESTART_REQUIRED"
        return {"state": "saved", "channels_paused": True, "schedules_paused": True,
                "restart_required": restart_fields, "config_hash": service.settings.hash}


# Замена ключей — отдельное действие владельца; пустое поле означает оставить прежнее значение.
def save_credentials(service, values):
    from .image_schedule import lock, read, write
    if not isinstance(values, dict) or set(values) - credential_names(service.settings):
        raise AgentError("CREDENTIAL_NAME_INVALID")
    updates = {name: value for name, value in values.items() if value}
    if any(not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\x00")
           for value in updates.values()):
        raise AgentError("CREDENTIAL_VALUE_INVALID")
    with lock(service.settings.root, "run"):
        path = service.settings.root / ".env.local"
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        lines = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
        lines.extend(f"{name}={value}" for name, value in updates.items())
        temporary = path.with_suffix(".new")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
        os.environ.update(updates)
        service.activate(service.settings)
        # Адаптеры перечитывают новые значения; история и файлы базы остаются на месте.
        if any(name.startswith("OPENCODE_") or name == "LLM_PROVIDER_API_KEY" for name in updates):
            from .adapters import OpenCode
            service.editor.client.close()
            service.editor = OpenCode(service.settings)
        if service.settings.app["max"]["token_env"] in updates:
            service.publisher.close()
            service.publisher = service.make_publisher(service.settings)
        if "COMFYUI_BASE_URL" in updates:
            from .hybrid import Hybrid
            service.hybrid.close()
            service.hybrid = Hybrid(service)
        owner_key = service.settings.app["api"]["auth_secret_env"]
        key_file = service.settings.root / "data/panel-access-key.txt"
        if owner_key in updates and key_file.exists():
            key_file.write_text(updates[owner_key] + "\n", encoding="utf-8")
        restart = service.settings.app["database_url_env"] in updates
        if restart:
            for channel in service.settings.topic["channels"]:
                service.pause(channel["id"], True, actor="owner")
                control = read(service.settings.root, "control", {}, channel["id"])
                write(service.settings.root, "control", {**control, "enabled": False}, channel["id"])
            service.config_error = "RESTART_REQUIRED"
        return {"state": "saved", "updated_names": sorted(updates), "values_returned": False,
                "restart_required": restart}
