"""Ограждение тестовой копии от рабочего checkout, БД и браузерного профиля."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentError, inside


MARKER = "data/four-platform/isolation.json"


def _check_state_paths(root):
    for name in ("data", "config", ".env.local"):
        path = root / name
        if path.is_symlink() or path.is_junction():
            raise AgentError("ISOLATED_STATE_LINK_FORBIDDEN")


def _plain_path(root, relative):
    if not isinstance(relative, str) or ":" in relative or Path(relative).is_absolute():
        raise AgentError("ISOLATED_STATE_PATH_INVALID")
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise AgentError("ISOLATED_STATE_PATH_INVALID")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise AgentError("ISOLATED_STATE_LINK_FORBIDDEN")
    return path


def mark_isolated(root, source_root):
    root, source = Path(root).resolve(), Path(source_root).resolve()
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise AgentError("ISOLATED_COPY_REQUIRED")
    _check_state_paths(root)
    _plain_path(root, MARKER)
    path = inside(root, MARKER)
    if path.exists():
        assert_isolated(root)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"contract": "four-platform-copy-v1", "root": str(root),
                   "source_root": str(source), "production_access": False}, stream, indent=2)


def assert_isolated(root, *, environment=True):
    root = Path(root).resolve()
    # Файлы проверяются до чтения marker, dotenv или настройки подключения.
    _check_state_paths(root)
    _plain_path(root, MARKER)
    try:
        value = json.loads(inside(root, MARKER).read_text(encoding="utf-8"))
        source = Path(value["source_root"]).resolve()
        if (value["contract"] != "four-platform-copy-v1" or value["production_access"] is not False
                or Path(value["root"]).resolve() != root or source == root
                or root.is_relative_to(source) or source.is_relative_to(root)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AgentError("ISOLATED_COPY_REQUIRED") from exc
    if not environment:
        return None
    if os.getenv("MAX_EXISTING_BROWSER_PROFILE"):
        raise AgentError("ISOLATED_EXTERNAL_BROWSER_FORBIDDEN")
    database_names = {"DATABASE_URL", "TEST_DATABASE_URL"}
    postiz_names = {"POSTIZ_BASE_URL"}
    # Проверяем именно используемые имена env, включая пользовательскую конфигурацию.
    try:
        app = root / "config/app.json"
        _plain_path(root, "config/app.json")
        if app.exists():
            configuration = json.loads(app.read_text(encoding="utf-8-sig"))
            database_names.add(configuration.get("database_url_env", "DATABASE_URL"))
            for name in ("state_dir", "media_dir", "logs_dir"):
                if name in configuration:
                    directory = _plain_path(root, configuration[name])
                    if name == "state_dir":
                        for filename in ("agent.db", "agent.db-wal", "agent.db-shm"):
                            _plain_path(root, (directory / filename).relative_to(root).as_posix())
            browser = configuration.get("max", {}).get("browser", {})
            if "profile_dir" in browser:
                _plain_path(root, browser["profile_dir"])
        publishing = root / "config/publishing.json"
        _plain_path(root, "config/publishing.json")
        if publishing.exists():
            configuration = json.loads(publishing.read_text(encoding="utf-8-sig"))
            postiz_names.add(configuration.get("postiz", {}).get("base_url_env", "POSTIZ_BASE_URL"))
            for target in configuration.get("targets", []):
                if "profile_dir" in target.get("options", {}):
                    _plain_path(root, target["options"]["profile_dir"])
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise AgentError("ISOLATED_CONFIG_INVALID") from exc
    if any(not isinstance(name, str) or not name for name in database_names | postiz_names):
        raise AgentError("ISOLATED_CONFIG_INVALID")
    for name in database_names:
        value = os.getenv(name, "")
        if not value:
            continue
        parsed = urlsplit(value)
        if (parsed.scheme not in {"postgresql", "postgresql+psycopg"}
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.port != 55448
                or parsed.path not in {"/editorial_hub", "/editorial_hub_tests"}
                or parsed.query or parsed.fragment):
            raise AgentError("ISOLATED_DATABASE_REQUIRED")
    for name in postiz_names:
        value = os.getenv(name, "")
        if not value:
            continue
        parsed = urlsplit(value)
        if (parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 14007
                or parsed.scheme != "http" or parsed.username or parsed.password):
            raise AgentError("ISOLATED_POSTIZ_REQUIRED")
    return None
