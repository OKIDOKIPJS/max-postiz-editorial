"""Выбор файлов проекта, импорт внешних материалов и открытие Проводника из панели."""

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .config import AgentError, inside, parse_json

SUFFIXES = {".mp4", ".png", ".jpg", ".jpeg", ".webp", ".json", ".md", ".txt",
            ".yaml", ".yml", ".onnx", ".safetensors", ".bin", ".pt", ".pth", ".gguf"}
HIDDEN_NAMES = {"__pycache__", "panel-access-key.txt", "secrets.env", "opencode.private.json"}
DIALOG_LOCK = threading.Lock()


# Операции рабочего стола относятся к компьютеру проекта и разрешены только локальному владельцу.
def local_client(request):
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


# Возвращаем только пути внутри проекта; скрытые каталоги и файлы ключей не доступны в списке.
def project_path(service, value):
    if not isinstance(value, str) or len(value) > 2048 or any(char in value for char in "\0\r\n"):
        raise AgentError("FILE_PATH_INVALID")
    root = service.settings.root.resolve()
    path = inside(root, value or ".")
    relative = path.relative_to(root)
    if any(part.startswith(".") or part.casefold() in HIDDEN_NAMES for part in relative.parts):
        raise AgentError("FILE_PATH_FORBIDDEN", 403)
    if path.suffix.lower() in {".key", ".pem", ".enc", ".db", ".sqlite", ".sqlite3"}:
        raise AgentError("FILE_PATH_FORBIDDEN", 403)
    return path


# Приватность зависит от настроенных каталогов, а не от их расположения под data/.
def private_roots(root, app, purpose="file"):
    # Стандартный каталог остаётся приватным и после переноса активного профиля.
    paths = ["data/max-browser", app.get("max", {}).get("browser", {}).get("profile_dir"),
             "data/route-admin-private", "data/owner-browser-selection.json", "data/owner-browser-result.json",
             "data/editorial-restart-request.json", "data/editorial-restart-claims",
             "data/monitor", "data/monitor-browser", "data/vk-browser", "data/pikabu-browser",
             "config/monitor.json"]
    if purpose != "private":
        paths.extend(["data/opencode", app.get("opencode", {}).get("isolated_state_dir"), "data/private-imports"])
    # Профиль отключённого адресата тоже содержит авторизацию. Читаем только
    # текущую конфигурацию маршрутов, не содержимое профилей или monitor runtime.
    try:
        publishing = inside(root, "config/publishing.json")
        if publishing.exists():
            if not publishing.is_file() or publishing.stat().st_size > 1024 * 1024:
                raise ValueError
            document = parse_json(publishing.read_text(encoding="utf-8-sig"))
            if not isinstance(document, dict) or not isinstance(document.get("targets"), list):
                raise ValueError
            for target in document["targets"]:
                if not isinstance(target, dict) or not isinstance(target.get("options", {}), dict):
                    raise ValueError
                profile = target.get("options", {}).get("profile_dir")
                if profile is not None:
                    if not isinstance(profile, str) or not profile:
                        raise ValueError
                    paths.append(profile)
        return tuple(dict.fromkeys(inside(root, value) for value in paths if value))
    except (AgentError, OSError, ValueError, TypeError, RuntimeError):
        # Неверная конфигурация не делает неизвестный каталог публичным.
        raise AgentError("FILE_PRIVATE_PATH_CONFIG_INVALID", 403) from None


# Каталоги авторизации не перечисляем через HTTP; открытие их папки в Проводнике остаётся локальным.
def browsable(service, path, purpose="file", *, roots=None):
    roots = private_roots(service.settings.root, service.settings.app, purpose) if roots is None else roots
    # VK разрешает только data/vk-browser[-имя]. Весь этот namespace остаётся
    # приватным и после смены/отключения выбранного профиля, без чтения его файлов.
    try:
        parts = path.relative_to(service.settings.root.resolve()).parts
    except ValueError:
        parts = ()
    if (len(parts) >= 2 and parts[0].casefold() == "data"
            and re.fullmatch(r"vk-browser(?:-[A-Za-z0-9_-]+)?", parts[1], re.IGNORECASE)):
        return False
    return not any(path.is_relative_to(directory)
                   for directory in roots)


# Фильтр файлов одинаков для встроенного выбора, системного диалога и проверки результата.
def extensions(purpose):
    if purpose == "video":
        return {".mp4"}
    if purpose == "workflow":
        return {".json"}
    if purpose == "file":
        return SUFFIXES
    if purpose == "private":
        return {".env", ".json", ".txt"}
    raise AgentError("FILE_PURPOSE_INVALID")


# Встроенный выбор работает на Windows и на сервере без графического рабочего стола.
def browse(service, value="", purpose="file"):
    allowed = extensions(purpose)
    root = service.settings.root.resolve()
    roots = private_roots(root, service.settings.app, purpose)
    # Canonical containment is explicit before the first filesystem query.
    # Include the separator so a sibling named like the project is not accepted.
    checked = os.path.normcase(str(project_path(service, value)))
    root_text = os.path.normcase(str(root))
    if checked != root_text and not checked.startswith(root_text.rstrip(os.sep) + os.sep):
        raise AgentError("PATH_OUTSIDE_PROJECT", 403)
    path = Path(checked)
    while not path.is_dir() and path != root:
        path = path.parent
    if not browsable(service, path, purpose, roots=roots):
        raise AgentError("FILE_DIRECTORY_PRIVATE", 403)
    entries = []
    try:
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            try:
                resolved = project_path(service, child.relative_to(root).as_posix())
                if not browsable(service, resolved, purpose, roots=roots):
                    continue
                directory = resolved.is_dir()
                if not directory and (not resolved.is_file() or resolved.suffix.lower() not in allowed):
                    continue
                entries.append({"name": child.name, "path": resolved.relative_to(root).as_posix(),
                                "directory": directory, "bytes": None if directory else resolved.stat().st_size})
            except (AgentError, OSError):
                continue
            if len(entries) == 1000:
                break
    except OSError as exc:
        raise AgentError("FILE_DIRECTORY_UNAVAILABLE") from exc
    return {"path": path.relative_to(root).as_posix(),
            "parent": path.parent.relative_to(root).as_posix() if path != root else None,
            "entries": entries, "truncated": len(entries) == 1000}


# Выбранный путь проверяем до подстановки; workflow возвращаем вместе с графом для согласованного сохранения.
def selected(service, value, kind="file", purpose="file"):
    allowed = extensions(purpose)
    path = project_path(service, value)
    if not browsable(service, path, purpose):
        raise AgentError("FILE_DIRECTORY_PRIVATE", 403)
    if kind == "directory":
        if not path.is_dir():
            raise AgentError("FILE_DIRECTORY_REQUIRED")
    elif kind == "file":
        if not path.is_file() or path.suffix.lower() not in allowed:
            raise AgentError("FILE_TYPE_INVALID")
    else:
        raise AgentError("FILE_KIND_INVALID")
    result = {"state": "selected", "path": path.relative_to(service.settings.root.resolve()).as_posix(),
              "directory": kind == "directory"}
    if purpose == "workflow" and kind == "file":
        if path.stat().st_size > 5 * 1024 * 1024:
            raise AgentError("FILE_TOO_LARGE")
        try:
            document = parse_json(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, OSError) as exc:
            raise AgentError("INVALID_JSON") from exc
        if not isinstance(document, dict):
            raise AgentError("WORKFLOW_SCHEMA_INVALID")
        from .backup import check_config_secrets
        check_config_secrets(document)
        result["document"] = document
    return result


# Один системный диалог открывается отдельным процессом, чтобы не затрагивать потоки планировщика.
def native_pick(initial, kind, purpose):
    if os.name != "nt":
        return {"state": "unavailable"}
    if not DIALOG_LOCK.acquire(blocking=False):
        raise AgentError("FILE_DIALOG_BUSY", 409)
    try:
        command = [sys.executable, str(Path(__file__).with_name("file_dialog.py")),
                   str(initial), kind, " ".join(sorted(extensions(purpose)))]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                timeout=180, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            return {"state": "unavailable"}
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unavailable"}
    finally:
        DIALOG_LOCK.release()


# Внешний файл копируем в папку медиа, включённую в перенос и резервную копию проекта.
def import_selected(service, value, kind, purpose):
    path = Path(value).resolve()
    root = service.settings.root.resolve()
    if path.is_relative_to(root):
        return selected(service, path.relative_to(root).as_posix(), kind, purpose)
    if kind == "directory":
        raise AgentError("FILE_DIRECTORY_OUTSIDE_PROJECT")
    if not path.is_file() or path.suffix.lower() not in extensions(purpose):
        raise AgentError("FILE_TYPE_INVALID")
    limit = ({"video": service.settings.app.get("video", {}).get("max_bytes", 104857600),
              "workflow": 5 * 1024 * 1024, "private": 1024 * 1024}.get(purpose, 2 * 1024**3))
    if not 0 < path.stat().st_size <= limit:
        raise AgentError("FILE_TOO_LARGE")
    # Приватные файлы не импортируем даже при совпадении расширения с текстовым материалом.
    if path.name.startswith(".") or path.name.casefold() in HIDDEN_NAMES:
        raise AgentError("FILE_PATH_FORBIDDEN", 403)
    # Файл доступа хранится отдельно от медиа и не попадает в переносимый архив с обычными материалами.
    directory = inside(root, "data/private-imports") if purpose == "private" else inside(root, service.settings.app["media_dir"]) / "imports"
    destination = directory / uuid.uuid4().hex / path.name
    destination.parent.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copyfile(path, destination)
        if purpose == "private" and os.name != "nt":
            destination.chmod(0o600)
        result = selected(service, destination.relative_to(root).as_posix(), kind, purpose)
    except (OSError, AgentError):
        # Удаляем только созданную этим вызовом копию, не исходный файл пользователя.
        destination.unlink(missing_ok=True)
        destination.parent.rmdir()
        raise
    result["imported"] = True
    return result


# Системный выбор подставляет относительный путь; отмена не меняет значение поля.
def choose(service, request, body):
    kind, purpose = body.get("kind", "file"), body.get("purpose", "file")
    if kind not in {"file", "directory"}:
        raise AgentError("FILE_KIND_INVALID")
    extensions(purpose)
    if not local_client(request):
        return {"state": "unavailable"}
    initial = project_path(service, body.get("path", ""))
    while not initial.exists() and initial != service.settings.root.resolve():
        initial = initial.parent
    result = native_pick(initial, kind, purpose)
    if result.get("state") != "selected":
        return {"state": result.get("state", "unavailable")}
    return import_selected(service, result["path"], kind, purpose)


# Проводник получает фиксированную команду и проверенный путь; файл выделяется, но не запускается.
def explorer(service, request, body):
    if not local_client(request):
        raise AgentError("FILE_DESKTOP_LOCAL_ONLY", 403)
    if os.name != "nt":
        raise AgentError("FILE_EXPLORER_UNAVAILABLE")
    path = project_path(service, body.get("path", ""))
    if not path.exists():
        raise AgentError("FILE_NOT_FOUND", 404)
    executable = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "explorer.exe"
    arguments = [str(executable), "/select," + str(path)] if path.is_file() else [str(executable), str(path)]
    try:
        subprocess.Popen(arguments, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)  # noqa: S603
    except OSError as exc:
        raise AgentError("FILE_EXPLORER_UNAVAILABLE") from exc
    return {"state": "explorer_opened", "path": path.relative_to(service.settings.root.resolve()).as_posix(),
            "directory": path.is_dir()}
