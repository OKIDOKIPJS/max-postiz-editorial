"""Приёмка только нового CI-стенда в отдельном lo-only netns, без отправки постов."""

from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager, redirect_stdout
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import zlib

import httpx


CONTRACT = "three-platform-native-readiness-v1"
SCOPE = "ephemeral_github_hosted_no_external_publications"
ORIGIN = "http://10.20.30.45:4007"
POSTIZ = "http://127.0.0.1:14007/api/public/v1"
TARGETS = {"max": "ci_max", "telegram": "ci_tg", "vk": "ci_vk"}
MIB = 1024 * 1024
HTTP_PATHS = frozenset({"/api/auth/register", "/api/auth/login", "/api/user/self", "/api/editorial/route-setup",
    "/api/editorial/settings", "/api/editorial/control", "/api/editorial/route-admin", "/api/editorial/route-action"})
POST_SUCCESS_STATUS = {"/api/auth/register": 200, "/api/auth/login": 200,
    "/api/editorial/route-admin": 201, "/api/editorial/route-action": 201, "/api/editorial/control": 201}
HTTP_ACTIONS = frozenset({"configure_target", "set_secret", "upload_media", "create_original", "revise_original",
    "request_restart", "prepare_login", "preview", "preflight", "pause"})
HTTP_ERROR_CODES = frozenset({
    "EDITORIAL_CONTROL_INVALID", "EDITORIAL_COOKIE_SESSION_REQUIRED", "EDITORIAL_JSON_REQUIRED",
    "EDITORIAL_NOT_CONFIGURED", "EDITORIAL_ORIGIN_REQUIRED", "EDITORIAL_OWNER_REQUIRED",
    "EDITORIAL_RESPONSE_INVALID", "EDITORIAL_ROUTE_ACTION_INVALID", "EDITORIAL_ROUTE_ADMIN_INVALID",
    "EDITORIAL_ROUTE_LIST_INVALID", "EDITORIAL_SERVICE_UNAVAILABLE", "EDITORIAL_SETTINGS_INVALID",
    "EDITORIAL_REQUEST_FAILED", "INPUT_INVALID", "CREDENTIAL_FORMAT_INVALID", "CREDENTIAL_TARGET_INVALID",
    "CREDENTIAL_ENV_NOT_DEDICATED", "TARGET_BINDING_CONFLICT", "TARGET_PAUSE_REQUIRED",
    "CANCEL_OR_RECONCILE_FIRST", "BRIDGE_EDIT_IN_PROGRESS", "ROUTE_CONFIGURATION_CHANGED",
    "LOGIN_SELECTION_ACTIVE", "ROUTE_RUNTIME_RESTART_REQUIRED", "ROUTE_ADMIN_BUSY", "ROUTE_ADMIN_CONFLICT",
    "ROUTE_ADMIN_RECOVERY_REQUIRED", "EDITORIAL_PAUSE_BEFORE_SETTINGS", "EDITORIAL_REVISION_CONFLICT",
    "EDITORIAL_MAINTENANCE_BUSY", "POSTIZ_NEAR_QUEUE_RESTART_BLOCKED", "POSTIZ_NOT_CONFIGURED",
    "RESTART_ALREADY_REQUESTED", "RESTART_SELECTION_INVALID", "ROUTE_CONFIG_CONFLICT",
})
EXCEPTION_KINDS = frozenset({"ScenarioError", "KeyError", "TypeError", "ValueError", "AttributeError", "NameError",
    "RuntimeError", "OSError", "PermissionError", "FileNotFoundError", "JSONDecodeError", "UnicodeDecodeError",
    "TimeoutError", "Error", "OperationalError", "ProgrammingError", "InterfaceError", "IntegrityError",
    "InvalidRequestError", "NoSuchTableError", "MemoryError", "OverflowError", "AssertionError"})
SCENARIO_FUNCTIONS = frozenset({"main", "run", "exercise", "bootstrap", "login", "body", "call", "admin", "setup",
    "runtime", "browser", "engine", "hub_snapshot", "postiz_snapshot", "snapshot", "seed_unknown", "check", "save",
    "phase", "atomic", "digest", "png_bytes", "dsn", "plain", "bind_source", "validate_state", "namespace_guard", "require",
    "monitor_scope", "monitor_environment", "monitor_history", "monitor_ledger_history", "monitor_files",
    "monitor_round", "monitor_copy", "monitor_acceptance", "monitor_lanes", "monitor_advanced"})
MONITOR_TABLE_SELECTS = {
    "monitor_reports": "SELECT * FROM monitor_reports", "monitor_meta": "SELECT * FROM monitor_meta",
    "monitor_command_jobs": "SELECT * FROM monitor_command_jobs",
    "monitor_source_proposals": "SELECT * FROM monitor_source_proposals",
    "monitor_command_controls": "SELECT * FROM monitor_command_controls",
    "monitor_command_mutation_minutes": "SELECT * FROM monitor_command_mutation_minutes",
    "monitor_command_cancellations": "SELECT * FROM monitor_command_cancellations",
    "monitor_command_permission_migrations": "SELECT * FROM monitor_command_permission_migrations",
}


def monitor_scope(state, root, *, restored=False):
    """The production isolation guard retains its exact 55448/14007 contracts."""
    database = "editorial_hub_tests" if restored else "editorial_hub"
    value = dsn(state.get("monitor_dsn", ""), 55448)
    require(urlsplit(value).path == "/editorial_hub", "SCENARIO_MONITOR_DATABASE_INVALID")
    endpoint = value.rsplit("/", 1)[0] + "/" + database
    copy = plain(root.parent / "monitor-copies" / ("restored" if restored else "initial"))
    require(not copy.is_relative_to(root) and not root.is_relative_to(copy), "SCENARIO_MONITOR_COPY_OVERLAP")
    return copy, endpoint


@contextmanager
def monitor_environment(state, endpoint):
    """Only this ephemeral process receives synthetic copy credentials."""
    before = dict(os.environ)
    values = {"DATABASE_URL": endpoint, "TEST_DATABASE_URL": endpoint.rsplit("/", 1)[0] + "/editorial_hub_tests",
        "POSTIZ_BASE_URL": POSTIZ, "POSTIZ_API_KEY": state["postiz_api_key"],
        "POSTIZ_PUBLIC_ORIGIN": ORIGIN, "EDITORIAL_INTERNAL_KEY": state["internal_key"],
        "EDITORIAL_OWNER_USER_ID": state["owner_id"], "EDITORIAL_OWNER_ORGANIZATION_ID": state["org_id"],
        "MONITOR_WATCHDOG_KEY": state["internal_key"], "MONITOR_WATCHDOG_OWNER": state["owner_id"],
        "MONITOR_WATCHDOG_ORGANIZATION": state["org_id"]}
    try:
        os.environ.update(values)
        yield values
    finally:
        os.environ.clear()
        os.environ.update(before)


def monitor_history(core):
    """Compare all editorial rows and durable monitor rows, excluding heartbeat only."""
    from sqlalchemy import MetaData, select
    metadata = MetaData()
    metadata.reflect(bind=core.db.engine)
    editorial = {}
    with core.db.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        for table in metadata.sorted_tables:
            rows = [dict(row._mapping) for row in connection.execute(select(table))]
            editorial[table.name] = {"count": len(rows), "sha256": digest(sorted(rows, key=digest))}
    return {"editorial": editorial, "monitor": monitor_ledger_history(core.settings.root)}


def monitor_ledger_history(root):
    path = plain(root / "data/monitor/monitor.sqlite")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        monitor = {}
        for name in tables:
            require(name in MONITOR_TABLE_SELECTS, "SCENARIO_MONITOR_TABLE_INVALID")
            rows = [dict(row) for row in connection.execute(MONITOR_TABLE_SELECTS[name])]
            if name == "monitor_meta":
                rows = [row for row in rows if row["key"] not in {"observer", "sender", "command_receiver"}]
            monitor[name] = {"count": len(rows), "sha256": digest(sorted(rows, key=digest))}
    return monitor


def monitor_files(root):
    files = {}
    for folder in ("config", "data"):
        for path in sorted((plain(root) / folder).rglob("*")):
            require(not path.is_symlink(), "SCENARIO_MONITOR_BACKUP_ENTRY_INVALID")
            if path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            if relative == "data/four-platform/isolation.json":
                continue
            require(path.is_file() and path.stat().st_nlink == 1, "SCENARIO_MONITOR_BACKUP_ENTRY_INVALID")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def monitor_lanes():
    return [thread for thread in threading.enumerate() if thread.name in {
        "monitor-reports", "monitor-receive", "monitor-tasks"} and thread.is_alive()]


def monitor_advanced(status, baseline, started_at):
    observer = status.get("observer", {})
    for field in ("heartbeat_at", "source_collected_at", "sender_heartbeat_at"):
        stamp, previous = observer.get(field), baseline.get(field)
        if (type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < started_at
                or previous is not None and (type(previous) not in {int, float}
                    or not math.isfinite(previous) or stamp <= previous)):
            return False
    return True


def monitor_round(core, state):
    """Real loopback HTTP and real monitor lanes; any browser construction fails."""
    import uvicorn
    from max_postiz_agent.monitor_api import monitor_status
    from max_postiz_agent.monitor_cli import run, watchdog_once
    from max_postiz_agent.monitor_config import MonitorConfig
    from max_postiz_agent.monitor_service import MonitorService
    from max_postiz_agent.server_api import editorial_app
    require(not monitor_lanes(), "SCENARIO_MONITOR_LANES_NOT_STOPPED")
    stop = threading.Event()
    server = uvicorn.Server(uvicorn.Config(editorial_app(core), host="127.0.0.1", port=18092,
        access_log=False, log_level="warning"))
    api = threading.Thread(target=server.run, name="ci-monitor-api")
    worker = threading.Thread(target=run, args=(core, stop), name="ci-monitor-run")
    calls = []
    original = MonitorService._transport

    def forbidden_transport(self):
        calls.append("browser")
        raise ScenarioError("SCENARIO_MONITOR_EXTERNAL_TRANSPORT_FORBIDDEN")

    before = monitor_history(core)
    baseline = monitor_status(core).get("observer", {})
    started_at = time.time()
    MonitorService._transport = forbidden_transport
    try:
        api.start()
        worker.start()
        deadline = time.monotonic() + 25
        status = {}
        while time.monotonic() < deadline:
            status = monitor_status(core)
            advanced = monitor_advanced(status, baseline, started_at)
            if (server.started and advanced and status.get("freshness") == "fresh"
                    and status.get("observer", {}).get("sender_state") == "disabled"):
                break
            if not worker.is_alive():
                break
            stop.wait(0.05)
        require(server.started and monitor_advanced(status, baseline, started_at) and status.get("freshness") == "fresh"
                and status.get("enabled") is False and status.get("destination_configured") is False
                and status.get("observer", {}).get("source_paused") is True
                and status.get("observer", {}).get("sender_state") == "disabled", "SCENARIO_MONITOR_HEARTBEAT_INVALID")
        headers = {"X-Editorial-Key": state["internal_key"], "X-Editorial-Owner": state["owner_id"],
                   "X-Editorial-Organization": state["org_id"]}
        with httpx.Client(base_url="http://127.0.0.1:18092", trust_env=False, timeout=10, follow_redirects=False) as client:
            for route in ("monitor", "monitor-heartbeat", "monitor-preview"):
                path = "/internal/editorial/" + route
                for bad in ({}, {**headers, "X-Editorial-Key": "wrong"},
                            {**headers, "X-Editorial-Owner": "wrong"},
                            {**headers, "X-Editorial-Organization": "wrong"}):
                    require(client.get(path, headers=bad).status_code == 403, "SCENARIO_MONITOR_AUTH_FAILED")
                response = client.get(path, headers=headers)
                require(response.status_code == 200 and response.headers.get("cache-control") == "no-store"
                        and response.json().get("read_only") is True
                        and response.json().get("external_mutations") == 0, "SCENARIO_MONITOR_OWNER_API_FAILED")
                require(client.get(path + "?unused=1", headers=headers).status_code == 422,
                        "SCENARIO_MONITOR_QUERY_NOT_REJECTED")
        service = MonitorService(core, MonitorConfig.load(core.settings.root))
        result = watchdog_once(service, "http://127.0.0.1:18092/internal/editorial/monitor-heartbeat",
                               status["instance_id"], notify=False)
        require(result.get("state") == "healthy", "SCENARIO_MONITOR_WATCHDOG_FAILED")
    finally:
        stop.set()
        server.should_exit = True
        if worker.ident is not None:
            worker.join(timeout=15)
        if api.ident is not None:
            api.join(timeout=15)
        # run() itself has bounded joins: its wrapper can finish while a real
        # daemon lane still owns the SQLite ledger. Never snapshot that state.
        lanes = monitor_lanes()
        if not lanes and not worker.is_alive() and not api.is_alive():
            MonitorService._transport = original
        # These checks belong in finally so an HTTP/watchdog failure cannot
        # bypass quiescence. On leaks the deny-transport sentinel stays armed
        # until the bounded scenario process/cgroup is stopped by its owner.
        require(not lanes, "SCENARIO_MONITOR_LANES_NOT_STOPPED")
        require(not worker.is_alive() and not api.is_alive(), "SCENARIO_MONITOR_THREADS_NOT_STOPPED")
    require(not calls, "SCENARIO_MONITOR_EXTERNAL_TRANSPORT_ATTEMPTED")
    require(monitor_history(core) == before, "SCENARIO_MONITOR_HISTORY_CHANGED")
    return {"instance_id": status["instance_id"], "configured_disabled": True, "owner_api_verified": True,
        "fresh_heartbeat": True, "heartbeat_advanced": True, "actual_lanes_stopped": True,
        "sender_disabled": True, "watchdog_no_notify": True,
        "history_preserved": True, "no_browser_or_send": True}


class ScenarioError(RuntimeError):
    def __init__(self, code, *, http=None):
        super().__init__(code)
        self.http = http


def safe_http_metadata(value):
    """Only fixed routing metadata and a response digest may enter public evidence."""
    fields = {"method", "path", "status", "body_sha256"}
    if (not isinstance(value, dict) or not fields.issubset(value) or set(value) - fields - {"action", "response_code"}
            or value.get("method") not in ("GET", "POST")
            or not isinstance(value.get("path"), str) or value["path"] not in HTTP_PATHS
            or type(value.get("status")) is not int or not 100 <= value["status"] <= 599
            or not isinstance(value.get("body_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["body_sha256"])
            or ("action" in value and (not isinstance(value["action"], str) or value["action"] not in HTTP_ACTIONS))
            or ("response_code" in value and (not isinstance(value["response_code"], str)
                or value["response_code"] not in HTTP_ERROR_CODES))):
        return None
    return dict(value)


def safe_exception_metadata(error):
    kind = type(error).__name__
    frames, trace = [], error.__traceback__
    source = os.path.normcase(os.path.abspath(__file__))
    while trace is not None:
        code = trace.tb_frame.f_code
        if (os.path.normcase(os.path.abspath(code.co_filename)) == source and code.co_name in SCENARIO_FUNCTIONS
                and type(trace.tb_lineno) is int and 1 <= trace.tb_lineno <= 10000):
            frames.append({"function": code.co_name, "line": trace.tb_lineno})
        trace = trace.tb_next
    flags = {"sandbox_blocked": False, "executable_missing": False, "strict_mode": False, "err_blocked": False}
    if type(error).__module__.startswith("playwright."):
        message = str(error)[:65536].lower()
        flags.update(sandbox_blocked=any(pattern in message for pattern in (
            "no usable sandbox", "failed to move to new namespace", "suid sandbox helper binary")),
            executable_missing="executable doesn't exist" in message,
            strict_mode="strict mode violation" in message,
            err_blocked=any(pattern in message for pattern in (
                "net::err_blocked_by_client", "net::err_blocked_by_response", "net::err_blocked_by_csp", "net::err_blocked_by_orb")))
    return {"kind": kind if kind in EXCEPTION_KINDS else "OtherError", "frames": frames[-8:], "playwright_flags": flags}


def require(condition, code):
    if not condition:
        raise ScenarioError(code)


def plain(path):
    value = Path(path)
    require(value.is_absolute() and ".." not in value.parts, "SCENARIO_PATH_INVALID")
    require(not any(part.is_symlink() for part in (value, *value.parents)), "SCENARIO_PATH_INVALID")
    return value


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                     default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item)).encode()
    return hashlib.sha256(raw).hexdigest()


def dsn(value, port):
    try:
        parsed = urlsplit(value)
        require(parsed.scheme in {"postgresql", "postgresql+psycopg"} and parsed.hostname == "127.0.0.1"
                and parsed.port == port and parsed.username and parsed.password and parsed.path not in {"", "/"}
                and not parsed.query and not parsed.fragment, "SCENARIO_DATABASE_SCOPE_INVALID")
        return "postgresql+psycopg" + value[len("postgresql"):] if parsed.scheme == "postgresql" else value
    except (TypeError, ValueError):
        raise ScenarioError("SCENARIO_DATABASE_SCOPE_INVALID") from None


def validate_state(value, root):
    require(isinstance(value, dict) and value.get("contract") == CONTRACT and value.get("scope") == SCOPE
            and value.get("new_empty_runtime") is True and value.get("root") == str(root.resolve())
            and isinstance(value.get("runtime_id"), str) and re.fullmatch(r"[a-f0-9]{32}", value["runtime_id"])
            and value.get("public_origin") == ORIGIN and value.get("private_postiz_base") == POSTIZ
            and type(value.get("namespace_owner_pid")) is int and value["namespace_owner_pid"] > 1
            and type(value.get("namespace_inode")) is int and value["namespace_inode"] > 0
            and value.get("service_unit") == "max-postiz-editorial.service"
            and value.get("login_service_unit") == "max-postiz-login.service"
            and value.get("profile_id") == "test" and value.get("vk_owner") == "123456789",
            "SCENARIO_ATTESTATION_INVALID")
    for key in ("internal_key", "jwt_secret", "owner_password"):
        require(isinstance(value.get(key), str) and 24 <= len(value[key]) <= (64 if key == "owner_password" else 4096),
                "SCENARIO_SECRET_INVALID")
    require(len(value["internal_key"]) >= 32 and len(value["jwt_secret"]) >= 32,
            "SCENARIO_SECRET_INVALID")
    require(isinstance(value.get("owner_email"), str) and value["owner_email"].endswith(
        ("@example.invalid", "@example.test", "@example.com")), "SCENARIO_OWNER_INVALID")
    dsn(value.get("postiz_dsn", ""), 5432)
    dsn(value.get("hub_dsn", ""), 5434)
    return value


def namespace_guard(state):
    require(sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() != 0
            and os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("RUNNER_ENVIRONMENT") == "github-hosted",
            "SCENARIO_GITHUB_NONROOT_REQUIRED")
    import pwd
    require(pwd.getpwuid(os.geteuid()).pw_name == "editorial", "SCENARIO_SERVICE_USER_REQUIRED")
    inode = os.stat("/proc/self/ns/net").st_ino
    require(inode == state["namespace_inode"] and os.stat(
        f"/proc/{state['namespace_owner_pid']}/ns/net").st_ino == inode, "SCENARIO_NAMESPACE_CHANGED")
    require(sorted(name for _, name in socket.if_nameindex()) == ["lo"], "SCENARIO_NETWORK_NOT_ISOLATED")
    require(len(Path("/proc/net/route").read_text().splitlines()) == 1, "SCENARIO_NETWORK_ROUTE_PRESENT")


def atomic(path, value, *, exclusive=False, private=False):
    path = plain(path)
    path.parent.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
    if exclusive:
        require(not path.exists(), "SCENARIO_EVIDENCE_ALREADY_EXISTS")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # The phase marker and parent own this evidence path exclusively.
            require(not path.exists(), "SCENARIO_EVIDENCE_ALREADY_EXISTS")
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def png_bytes(size=None):
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (96, 64), (32, 84, 126)).save(stream, "PNG")
    raw = stream.getvalue()
    if size is None:
        return raw
    require(type(size) is int and size >= len(raw) + 14, "SCENARIO_PNG_SIZE_INVALID")
    # A valid inert ancillary chunk, rather than malformed trailing padding.
    payload = b"ci\0" + b"x" * (size - len(raw) - 15)
    chunk = b"tEXt" + payload
    return raw[:-12] + struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk)) + raw[-12:]


def bind_source(root):
    source = (root / "src").resolve()
    for name, module in tuple(sys.modules.items()):
        if name == "max_postiz_agent" or name.startswith("max_postiz_agent."):
            filename = getattr(module, "__file__", None)
            require(filename and Path(filename).resolve().is_relative_to(source), "SCENARIO_SOURCE_ORIGIN_INVALID")
    sys.path.insert(0, str(source))


def wait_editorial_panel(page):
    panel = page.get_by_role("main")
    panel.wait_for(state="visible", timeout=45000)
    require(panel.count() == 1, "SCENARIO_BROWSER_PANEL_AMBIGUOUS")
    heading = panel.get_by_role("heading", name="Редакция", exact=True, level=1)
    heading.wait_for(state="visible", timeout=45000)
    require(heading.count() == 1, "SCENARIO_BROWSER_HEADING_AMBIGUOUS")
    return panel


def save_screenshot(root, folder, raw):
    root, folder = plain(root), plain(folder)
    require(folder == root / "data/ci-public", "SCENARIO_SCREENSHOT_PATH_INVALID")
    require(type(raw) is bytes and 0 < len(raw) <= 8 * MIB, "SCENARIO_SCREENSHOT_BYTES_INVALID")
    descriptors = []
    try:
        if os.name != "nt":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            project = os.open(root, flags)
            descriptors.append(project)
            data = os.open("data", flags, dir_fd=project)
            descriptors.append(data)
            directory = os.open("ci-public", flags, dir_fd=data)
            descriptors.append(directory)
            for descriptor in (data, directory):
                info = os.fstat(descriptor)
                require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                        and not info.st_mode & 0o077, "SCENARIO_SCREENSHOT_DIRECTORY_INVALID")
        else:
            require(folder.is_dir(), "SCENARIO_SCREENSHOT_DIRECTORY_INVALID")
            directory = None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            target = os.open("editorial-synthetic.png" if directory is not None else folder / "editorial-synthetic.png",
                             flags, 0o600, **({"dir_fd": directory} if directory is not None else {}))
        except FileExistsError:
            raise ScenarioError("SCENARIO_EVIDENCE_ALREADY_EXISTS") from None
        with os.fdopen(target, "wb") as output:
            if os.name != "nt":
                os.fchmod(output.fileno(), 0o600)
            info = os.fstat(output.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "SCENARIO_SCREENSHOT_ENTRY_INVALID")
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if directory is not None:
            os.fsync(directory)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class Scenario:
    def __init__(self, root, state_path, evidence):
        self.root, self.state_path, self.evidence = plain(root), plain(state_path), plain(evidence)
        require(not self.state_path.is_relative_to(self.evidence), "SCENARIO_PRIVATE_PUBLIC_OVERLAP")
        info = self.state_path.stat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and (not hasattr(os, "geteuid") or info.st_uid == os.geteuid()), "SCENARIO_STATE_PERMISSIONS_INVALID")
        require(info.st_size <= MIB, "SCENARIO_STATE_TOO_LARGE")
        self.state = validate_state(json.loads(self.state_path.read_text(encoding="utf-8")), self.root)
        self.check()
        bind_source(self.root)
        self.client = httpx.Client(base_url=ORIGIN, trust_env=False, follow_redirects=False, timeout=30,
                                   headers={"Origin": ORIGIN, "Referer": ORIGIN + "/editorial", "Sec-Fetch-Site": "same-origin"})
        self.last_http = None

    def check(self):
        namespace_guard(self.state)

    def save(self):
        atomic(self.state_path, self.state, private=True)

    def phase(self, mode):
        phases = self.state.setdefault("phases", {})
        require(mode not in phases, "SCENARIO_PHASE_ALREADY_STARTED")
        phases[mode] = {"state": "started", "at": time.time()}
        self.save()

    def call(self, method, path, *, allowed=None, **kwargs):
        self.check()
        require(method in {"GET", "POST"}, "SCENARIO_HTTP_SCOPE_INVALID")
        require(path in HTTP_PATHS, "SCENARIO_HTTP_SCOPE_INVALID")
        if path == "/api/editorial/route-action":
            require(kwargs.get("json", {}).get("action") in {"preview", "preflight"}, "SCENARIO_EXTERNAL_ACTION_FORBIDDEN")
        if path == "/api/editorial/control" and "json" in kwargs:
            require(kwargs["json"].get("action") == "pause", "SCENARIO_EXTERNAL_ACTION_FORBIDDEN")
        if allowed is None:
            expected = 200 if method == "GET" else POST_SUCCESS_STATUS.get(path)
            require(expected is not None, "SCENARIO_HTTP_SCOPE_INVALID")
            allowed = (expected,)
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ScenarioError("SCENARIO_HTTP_UNAVAILABLE") from None
        if response.status_code not in allowed or response.is_redirect:
            metadata = {"method": method, "path": path, "status": response.status_code,
                        "body_sha256": hashlib.sha256(response.content).hexdigest()}
            payload = kwargs.get("json")
            action = payload.get("action") if isinstance(payload, dict) else None
            if isinstance(action, str) and action in HTTP_ACTIONS:
                metadata["action"] = action
            if len(response.content) <= 65536:
                try:
                    body = json.loads(response.content)
                except (ValueError, UnicodeError):
                    body = None
                code = body.get("code") if isinstance(body, dict) else None
                if isinstance(code, str) and code in HTTP_ERROR_CODES:
                    metadata["response_code"] = code
            raise ScenarioError("SCENARIO_HTTP_STATUS_INVALID", http=metadata)
        self.last_http = {"method": method, "path": path, "status": response.status_code,
                          "body_sha256": hashlib.sha256(response.content).hexdigest()}
        payload = kwargs.get("json")
        action = payload.get("action") if isinstance(payload, dict) else None
        if isinstance(action, str) and action in HTTP_ACTIONS:
            self.last_http["action"] = action
        return response

    def body(self, response):
        require(len(response.content) <= 2 * MIB, "SCENARIO_RESPONSE_TOO_LARGE")
        value = response.json()
        require(isinstance(value, dict), "SCENARIO_RESPONSE_INVALID")
        return value

    def login(self):
        self.call("POST", "/api/auth/login", json={"email": self.state["owner_email"],
            "password": self.state["owner_password"], "provider": "LOCAL"})
        require(any(cookie.name == "auth" for cookie in self.client.cookies.jar), "SCENARIO_COOKIE_MISSING")
        user = self.body(self.call("GET", "/api/user/self"))
        require(user.get("id") == self.state.get("owner_id") and user.get("orgId") == self.state.get("org_id")
                and user.get("activated") is True and user.get("role") == "SUPERADMIN"
                and user.get("publicApi") == self.state.get("postiz_api_key"), "SCENARIO_OWNER_CHANGED")
        return user

    def engine(self, key, port):
        from sqlalchemy import create_engine
        self.check()
        return create_engine(dsn(self.state[key], port), pool_pre_ping=True)

    def bootstrap(self):
        from sqlalchemy import text
        engine = self.engine("postiz_dsn", 5432)
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                require(all(connection.scalar(text(query)) == 0 for query in (
                    'SELECT count(*) FROM "User"', 'SELECT count(*) FROM "Organization"',
                    'SELECT count(*) FROM "Post"')), "SCENARIO_DATABASE_NOT_EMPTY")
        finally:
            engine.dispose()
        self.call("POST", "/api/auth/register", json={"email": self.state["owner_email"],
            "password": self.state["owner_password"], "provider": "LOCAL", "company": "CI Readiness"})
        self.call("POST", "/api/auth/login", json={"email": self.state["owner_email"],
            "password": self.state["owner_password"], "provider": "LOCAL"})
        user = self.body(self.call("GET", "/api/user/self"))
        require(user.get("activated") is True and user.get("role") == "SUPERADMIN"
                and all(isinstance(user.get(key), str) and user[key] for key in ("id", "orgId", "publicApi")),
                "SCENARIO_OWNER_INVALID")
        self.state.update(owner_id=user["id"], org_id=user["orgId"], postiz_api_key=user["publicApi"])
        self.save()
        return {"fresh_database": True, "registered": True, "cookie_login": True, "owner_verified": True}

    def admin(self, value, allowed=None):
        require(value.get("action") in {"configure_target", "set_secret", "upload_media", "create_original",
                "revise_original", "request_restart", "prepare_login"}, "SCENARIO_EXTERNAL_ACTION_FORBIDDEN")
        return self.body(self.call("POST", "/api/editorial/route-admin", json=value, allowed=allowed))

    def setup(self):
        return self.body(self.call("GET", "/api/editorial/route-setup"))

    def runtime(self):
        self.check()
        port = self.state.get("agent_port", 8092)
        require(port in {8092, 18092}, "SCENARIO_AGENT_SCOPE_INVALID")
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
            response = client.get(f"http://127.0.0.1:{port}/internal/editorial/runtime", headers={
                "X-Editorial-Key": self.state["internal_key"], "X-Editorial-Owner": self.state["owner_id"],
                "X-Editorial-Organization": self.state["org_id"]})
        require(response.status_code == 200, "SCENARIO_RUNTIME_UNAVAILABLE")
        value = self.body(response)
        require(value.get("paused") is True and isinstance(value.get("worker_id"), str)
                and str(uuid.UUID(value["worker_id"])) == value["worker_id"], "SCENARIO_RUNTIME_INVALID")
        return value

    def exercise(self):
        self.login()
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as anonymous:
            response = anonymous.get(ORIGIN + "/api/editorial/route-setup", headers={"Origin": ORIGIN})
            require(response.status_code in {401, 403}, "SCENARIO_ANONYMOUS_OWNER_ACCESS")
        self.call("GET", "/api/editorial/route-setup", allowed=(401, 403), headers={"Authorization": "Bearer ci-invalid"})
        control = self.body(self.call("GET", "/api/editorial/settings"))
        self.call("POST", "/api/editorial/control", allowed=(403,), headers={"Origin": "https://wrong-origin.invalid"},
                  json={"action": "pause", "revision": control["revision"]})
        setup = self.setup()
        require(setup["targets"] == [] and any(row["id"] == "-123" for row in setup["max_channels"]),
                "SCENARIO_INITIAL_TARGETS_INVALID")
        targets = {}
        for platform, ident in TARGETS.items():
            setup = self.setup()
            options = {"max_chars": 3500}
            if platform == "telegram":
                options["media_layout"] = "atomic"
            if platform == "vk":
                options["owner"] = self.state["vk_owner"]
            response = self.admin({"action": "configure_target", "id": ident, "expected_binding_hash": None,
                "expected_config_hash": setup["config_hash"], "target": {"profile_id": "test", "platform": platform,
                    "route": {"max": "max_browser", "telegram": "telegram_api", "vk": "vk_browser"}[platform],
                    "destination": {"max": "-123", "telegram": "-1001234567890", "vk": self.state["vk_owner"]}[platform],
                    "enabled": True, "options": options}})
            require(response.get("paused") is True, "SCENARIO_TARGET_NOT_PAUSED")
            targets[platform] = {"id": ident, "binding_hash": response["binding_hash"]}
        tg = targets["telegram"]
        token = self.state.get("telegram_fixture_token", "123456789:CI_readiness_fixture_no_external_1234")
        self.admin({"action": "set_secret", "id": tg["id"], "expected_binding_hash": tg["binding_hash"], "value": token})
        require(token not in self.call("GET", "/api/editorial/route-setup").text, "SCENARIO_SECRET_REFLECTED")
        upload = {"action": "upload_media", "id": tg["id"], "expected_binding_hash": tg["binding_hash"], "mime": "image/png"}
        boundary = png_bytes(10 * MIB)
        require(len(boundary) == 10 * MIB, "SCENARIO_PNG_SIZE_INVALID")
        self.admin({**upload, "sha256": hashlib.sha256(boundary).hexdigest(), "data_base64": base64.b64encode(boundary).decode()})
        oversized = png_bytes(10 * MIB + 1)
        self.admin({**upload, "sha256": hashlib.sha256(oversized).hexdigest(), "data_base64": base64.b64encode(oversized).decode()}, allowed=(422,))
        # Nginx's 15 MiB admin gate and ordinary 1 MiB gate remain independent.
        for path, size in (("/api/editorial/route-admin", 15 * MIB + 512), ("/api/editorial/control", MIB + 512)):
            rejected = self.call("POST", path, allowed=(413,), content=b" " * size,
                                 headers={"Content-Type": "application/json"})
            require("nginx" in rejected.text.lower() and "text/html" in rejected.headers.get("content-type", ""),
                    "SCENARIO_NGINX_LIMIT_NOT_ATTRIBUTED")
        self.call("POST", "/api/editorial/control", allowed=(413,), content=b" " * (200 * 1024),
                  headers={"Content-Type": "application/json"})
        raw = png_bytes()
        media = self.admin({**upload, "sha256": hashlib.sha256(raw).hexdigest(), "data_base64": base64.b64encode(raw).decode()})["media"]
        originals = {}
        for platform, target in targets.items():
            original = {"title": "Научная заметка стенда " + platform,
                "body": "Это собственный научный текст для проверки редактора. Внешняя публикация не выполняется.",
                "media": [media] if platform == "telegram" else [], "metadata": {}}
            result = self.admin({"action": "create_original", "id": target["id"],
                "expected_binding_hash": target["binding_hash"], "original": original, "attest_original": True})
            require(result.get("approval_created") is False and result.get("external_mutations") == 0,
                    "SCENARIO_ORIGINAL_APPROVAL_CREATED")
            if platform == "telegram":
                original["body"] += " Редакция уточнена владельцем синтетического стенда."
                result = self.admin({"action": "revise_original", "id": result["variant_id"],
                    "expected_binding_hash": target["binding_hash"], "revision": result["revision"],
                    "content_hash": result["content_hash"], "original": original, "attest_original": True})
                preview = self.body(self.call("POST", "/api/editorial/route-action", json={"action": "preview", "id": result["variant_id"]}))
                require(len(preview.get("images", [])) == 1 and preview["images"][0].get("sha256") == media["sha256"],
                        "SCENARIO_PREVIEW_NOT_EXACT")
            originals[platform] = result
        self.state.update(targets=targets, originals=originals)
        self.save()
        before = self.runtime()
        old_history = self.hub_snapshot(restart_worker=before["worker_id"], expected_restart=0)
        setup = self.setup()
        try:
            self.admin({"action": "request_restart", "id": "server", "expected_binding_hash": None,
                        "expected_config_hash": setup["config_hash"]})
        except ScenarioError:
            # A successful restart can close the old HTTP connection before its
            # response arrives. Never POST again: read the new worker and audit.
            pass
        deadline = time.monotonic() + 90
        changed = False
        while time.monotonic() < deadline:
            try:
                current = self.runtime()
                if current["worker_id"] != before["worker_id"] and current.get("restart_pending") is False:
                    changed = True
                    break
            except (ScenarioError, httpx.HTTPError):
                pass
            time.sleep(0.5)
        require(changed, "SCENARIO_RESTART_NOT_CONFIRMED")
        require(old_history == self.hub_snapshot(restart_worker=before["worker_id"], expected_restart=1),
                "SCENARIO_RESTART_HISTORY_CHANGED")
        checked = self.body(self.call("POST", "/api/editorial/route-action", json={"action": "preflight",
            "id": originals["telegram"]["variant_id"]}))
        require(checked.get("preflight_only") is True and checked.get("live_verified") is False,
                "SCENARIO_PREFLIGHT_PROOF_INVALID")
        browser = self.browser()
        require(all(target["paused"] for target in self.setup()["targets"]), "SCENARIO_TARGET_RESUMED")
        return {"targets_configured": 3, "owner_cookie_guards": True, "write_only_secret": True,
                "exact_upload_limit": True, "nginx_and_ordinary_limits": True, "originals_created": 3,
                "original_edited": True, "exact_preview": True, "restart_worker_changed": True,
                "restart_history_preserved": True, "browser": browser, "all_paused": True, "external_publications": 0}

    def browser(self):
        from playwright.sync_api import sync_playwright
        self.check()
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", chromium_sandbox=True, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                def scope(route):
                    parsed = urlsplit(route.request.url)
                    if f"{parsed.scheme}://{parsed.netloc}" == ORIGIN:
                        route.continue_()
                    else:
                        route.abort()
                context.route("**/*", scope)
                response = context.request.post(ORIGIN + "/api/auth/login", data={"email": self.state["owner_email"],
                    "password": self.state["owner_password"], "provider": "LOCAL"}, headers={"Origin": ORIGIN})
                require(response.status == 200, "SCENARIO_BROWSER_LOGIN_FAILED")
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(ORIGIN + "/editorial", wait_until="domcontentloaded", timeout=45000)
                wait_editorial_panel(page)
                page.wait_for_function("ids=>ids.every(id=>[...document.querySelectorAll('option')].some(e=>e.value===id))",
                                       arg=list(TARGETS.values()), timeout=45000)
                require(not errors, "SCENARIO_BROWSER_JAVASCRIPT_ERROR")
                screenshot = page.screenshot(full_page=True)
                save_screenshot(self.root, self.evidence, screenshot)
                context.close()
                return {"screenshot": "editorial-synthetic.png", "sha256": hashlib.sha256(screenshot).hexdigest(),
                        "javascript_errors": 0, "channels_rendered": 3}
            finally:
                browser.close()

    def hub_snapshot(self, restart_worker=None, expected_restart=0):
        from sqlalchemy import MetaData, select
        engine = self.engine("hub_dsn", 5434)
        try:
            metadata = MetaData()
            metadata.reflect(bind=engine)
            result = {}
            with engine.connect() as connection:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                for table in metadata.sorted_tables:
                    rows = [dict(row._mapping) for row in connection.execute(select(table))]
                    if table.name == "audit_events" and restart_worker is not None:
                        allowed = [row for row in rows if row.get("action") == "editorial_restart_requested"
                            and row.get("actor") == "postiz:" + self.state["owner_id"]
                            and isinstance(row.get("details"), dict)
                            and row["details"].get("previous_worker_id") == restart_worker
                            and row["details"].get("claims_cleared") is False and row["details"].get("paused") is True]
                        require(len(allowed) == expected_restart, "SCENARIO_RESTART_AUDIT_INVALID")
                        rows = [row for row in rows if row not in allowed]
                    if table.name == "editorial_control":
                        for row in rows:
                            row.pop("runtime", None)  # Worker heartbeat is intentionally volatile.
                    result[table.name] = {"count": len(rows), "sha256": digest(sorted(rows, key=digest))}
            return result
        finally:
            engine.dispose()

    def postiz_snapshot(self):
        from sqlalchemy import text
        self.check()
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
            response = client.get(POSTIZ + "/posts", params={"startDate": "2000-01-01T00:00:00Z",
                "endDate": "2100-01-01T00:00:00Z"}, headers={"Authorization": self.state["postiz_api_key"]})
        require(response.status_code == 200 and isinstance(response.json(), dict)
                and response.json().get("posts") == [], "SCENARIO_POSTIZ_POSTS_NOT_EMPTY")
        engine = self.engine("postiz_dsn", 5432)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                users = [dict(row._mapping) for row in connection.execute(text(
                    'SELECT id, email, password, "providerName", activated FROM "User" ORDER BY id'))]
                organizations = [dict(row._mapping) for row in connection.execute(text(
                    'SELECT id, name, "apiKey" FROM "Organization" ORDER BY id'))]
                require(len(users) == len(organizations) == 1 and users[0]["id"] == self.state["owner_id"]
                        and organizations[0]["id"] == self.state["org_id"]
                        and organizations[0]["apiKey"] == self.state["postiz_api_key"], "SCENARIO_POSTIZ_IDENTITY_CHANGED")
            return {"users": 1, "organizations": 1, "posts": 0, "identity_sha256": digest([users, organizations])}
        finally:
            engine.dispose()

    def seed_unknown(self):
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from max_postiz_agent.config import fingerprint
        from max_postiz_agent.db import Audit
        from max_postiz_agent.hub import HubService
        from max_postiz_agent.hub_models import Dispatch, DispatchAttempt, Target, TargetApproval, Variant
        from max_postiz_agent.server_models import EditorialControl
        engine = self.engine("hub_dsn", 5434)
        try:
            with Session(engine) as session, session.begin():
                control = session.scalar(select(EditorialControl).where(EditorialControl.id == "main").with_for_update())
                targets = list(session.scalars(select(Target).with_for_update()))
                require(control is not None and control.paused and len(targets) == 3
                        and {target.id for target in targets} == set(TARGETS.values()) and all(target.paused for target in targets)
                        and session.scalar(select(Dispatch.id)) is None, "SCENARIO_SEED_NOT_EMPTY_PAUSED")
                variant = session.get(Variant, self.state["originals"]["vk"]["variant_id"])
                target = session.get(Target, TARGETS["vk"])
                require(variant is not None and variant.target_id == target.id and target.platform == "vk",
                        "SCENARIO_SEED_TARGET_INVALID")
                snapshot = HubService.snapshot(None, variant, target)
                due = time.time() - 3600
                hashed = fingerprint({"content": snapshot, "due_at": due})
                approval = TargetApproval(variant_id=variant.id, revision=variant.revision, snapshot_hash=hashed,
                                          scheduled_at=due, actor="ci_readiness_fixture", revoked=True)
                session.add(approval)
                session.flush()
                dispatch = Dispatch(variant_id=variant.id, approval_id=approval.id, revision=variant.revision,
                    snapshot_hash=hashed, snapshot=snapshot, state="unknown", due_at=due, owner=None, lease_until=0,
                    attempts=1, verified=False, error="CI_FIXTURE_UNKNOWN_NO_EXTERNAL_SEND")
                session.add(dispatch)
                session.flush()
                session.add(DispatchAttempt(dispatch_id=dispatch.id, result="unknown", error=dispatch.error))
                session.add(Audit(actor="ci_readiness_fixture", action="ci_unknown_restore_fixture", entity=dispatch.id,
                    details={"fixture": True, "runtime_id": self.state["runtime_id"], "external_mutations": 0,
                             "approval_revoked": True, "snapshot_sha256": hashed}))
                self.state["unknown_fixture_id"] = dispatch.id
            self.save()
        finally:
            engine.dispose()

    def snapshot(self, restored=False):
        self.login()
        if not restored:
            self.seed_unknown()
        value = self.hub_snapshot()
        postiz = self.postiz_snapshot()
        if restored:
            require(value == self.state.get("hub_snapshot") and postiz == self.state.get("postiz_snapshot"),
                    "SCENARIO_RESTORED_HISTORY_CHANGED")
        else:
            self.state["hub_snapshot"] = value
            self.state["postiz_snapshot"] = postiz
            self.save()
        require(all(row["paused"] for row in self.setup()["targets"]), "SCENARIO_RESTORED_TARGET_RESUMED")
        require(self.runtime()["paused"] is True, "SCENARIO_RESTORED_GLOBAL_RESUMED")
        return {"history_sha256": digest(value), "tables": value, "postiz": postiz, "all_paused": True,
                "unknown_fixture_preserved": True, "owner_identity_preserved": True, "external_publications": 0}

    def monitor_copy(self, root, endpoint, values, *, restored):
        from max_postiz_agent.isolated_workspace import assert_isolated
        from max_postiz_agent.monitor_config import initialize_monitor_config
        from max_postiz_agent.routes_cli import initialize_copy
        from max_postiz_agent.server_cli import main as server_main
        require(not root.exists(), "SCENARIO_MONITOR_COPY_ALREADY_EXISTS")
        root.mkdir(mode=0o700)
        (root / "config").mkdir(mode=0o700)
        # Public templates only: no primary runtime, browser profile or credentials.
        for template in (self.root / "config").glob("*.example.json"):
            require(template.is_file() and not template.is_symlink(), "SCENARIO_MONITOR_TEMPLATE_INVALID")
            shutil.copy2(template, root / "config" / template.name)
        for folder in ("schemas",):
            for source in (self.root / folder).rglob("*"):
                require(not source.is_symlink(), "SCENARIO_MONITOR_TEMPLATE_INVALID")
            shutil.copytree(self.root / folder, root / folder)
        shutil.copy2(self.root / "TOPIC.md", root / "TOPIC.md")
        initialize_copy(root, self.root)
        app = json.loads((root / "config/app.json").read_text())
        app["require_postgresql"] = True
        atomic(root / "config/app.json", app, private=True)
        publishing = json.loads((root / "config/publishing.json").read_text())
        publishing["targets"] = []
        atomic(root / "config/publishing.json", publishing, private=True)
        path = root / ".env.local"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n" + "\n".join(key + "=" + value for key, value in values.items()) + "\n")
        path.chmod(0o600)
        assert_isolated(root)
        initialize_monitor_config(root)
        if not restored:
            policy = json.loads((root / "config/hourly_article_policy.example.json").read_text(encoding="utf-8-sig"))
            policy["enabled"] = False
            atomic(root / "data/hourly-article-policy.local.json", policy, private=True)
            # The only explicit migration; monitor build/run/status never initialize PG.
            with redirect_stdout(io.StringIO()):
                server_main(["--root", str(root), "migrate"])
        else:
            backup = self.root / "data/ci-private/monitor-backup"
            require(backup.is_dir(), "SCENARIO_MONITOR_BACKUP_ABSENT")
            for folder in ("config", "data"):
                for source in sorted((backup / folder).rglob("*")):
                    if source.is_dir():
                        continue
                    relative = source.relative_to(backup)
                    require(source.is_file() and not source.is_symlink() and source.stat().st_nlink == 1
                            and relative.as_posix() != "data/four-platform/isolation.json",
                            "SCENARIO_MONITOR_BACKUP_ENTRY_INVALID")
                    target = root / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            assert_isolated(root)
            require(monitor_files(root) == self.state.get("monitor_backup_files"),
                    "SCENARIO_MONITOR_RESTORE_FILES_CHANGED")
        require(urlsplit(endpoint).port == 55448, "SCENARIO_MONITOR_DATABASE_INVALID")

    def monitor_acceptance(self, *, restored=False):
        from max_postiz_agent.monitor_cli import build_monitor
        from max_postiz_agent.monitor_commands import CommandGateway
        from max_postiz_agent.monitor_config import MonitorConfig
        from max_postiz_agent.monitor_store import MonitorStore
        self.check()
        before_main = self.hub_snapshot()
        root, endpoint = monitor_scope(self.state, self.root, restored=restored)
        with monitor_environment(self.state, endpoint) as values:
            self.monitor_copy(root, endpoint, values, restored=restored)
            core = build_monitor(root)
            try:
                store = MonitorStore(root)
                config = MonitorConfig.load(root)
                if not restored:
                    # Unknown and queued synthetic history exist before observation;
                    # they must survive restarts/restore without claiming or replay.
                    at = time.time()
                    store.enqueue("ci-unknown", "incident", "Synthetic unknown; never sent", config.hash,
                                  "123456789", at=at, expires_at=at + 3600)
                    claimed = store.claim(config.hash, "123456789", at=at)
                    require(claimed is not None, "SCENARIO_MONITOR_FIXTURE_CLAIM_FAILED")
                    store.finish(claimed, "unknown", {}, at=at)
                    store.enqueue("ci-pending", "hourly", "Synthetic queued; never sent", config.hash,
                                  "123456789", at=at + 1, expires_at=at + 3600)
                    gateway = CommandGateway(core, config.command_options(), store.path)
                    with gateway._connection() as connection:
                        for index, state in enumerate(("unknown", "queued")):
                            connection.execute("INSERT INTO monitor_command_jobs "
                                "(id,event_key,event_hash,actor,chat_id,payload,config_hash,state,result,created_at,updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"{index + 1:032x}", "ci-" + state,
                                digest(state), "ci_fixture", "123456789", '{"action":"status"}', config.hash,
                                state, '{"fixture":true,"external_mutations":0}', at, at))
                initial = monitor_history(core)
                require(initial["monitor"]["monitor_reports"]["count"] == 2
                        and initial["monitor"]["monitor_command_jobs"]["count"] == 2,
                        "SCENARIO_MONITOR_HISTORY_FIXTURES_ABSENT")
                if restored:
                    require(initial == self.state.get("monitor_history"), "SCENARIO_MONITOR_RESTORE_CHANGED_HISTORY")
                result = monitor_round(core, self.state)
                require(result["instance_id"] == store.meta("instance_id"), "SCENARIO_MONITOR_INSTANCE_CHANGED")
                if not restored:
                    # A fresh core simulates process restart, not only another GET.
                    core.close()
                    core = build_monitor(root)
                    again = monitor_round(core, self.state)
                    require(again["instance_id"] == result["instance_id"] and monitor_history(core) == initial,
                            "SCENARIO_MONITOR_RESTART_CHANGED_HISTORY")
                    self.state["monitor_history"] = initial
                    self.state["monitor_instance_id"] = result["instance_id"]
                    self.save()
                    backup = self.root / "data/ci-private/monitor-backup"
                    backup.mkdir(mode=0o700)
                    for folder in ("config", "data"):
                        for source in sorted((root / folder).rglob("*")):
                            if source.is_dir():
                                continue
                            relative = source.relative_to(root)
                            if relative.as_posix() == "data/four-platform/isolation.json":
                                continue
                            require(source.is_file() and not source.is_symlink() and source.stat().st_nlink == 1,
                                    "SCENARIO_MONITOR_BACKUP_ENTRY_INVALID")
                            target = backup / relative
                            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            if relative.as_posix() == "data/monitor/monitor.sqlite":
                                with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as destination:
                                    original.backup(destination)
                                    destination.commit()
                                target.chmod(0o600)
                            else:
                                shutil.copy2(source, target)
                    self.state["monitor_backup_files"] = monitor_files(backup)
                    self.save()
                else:
                    require(result["instance_id"] == self.state.get("monitor_instance_id"),
                            "SCENARIO_MONITOR_RESTORE_INSTANCE_CHANGED")
                require(monitor_history(core) == initial, "SCENARIO_MONITOR_HISTORY_CHANGED")
                result.update(history_sha256=digest(initial), unknown_reports=1, pending_reports=1,
                    unknown_commands=1, queued_commands=1, restart_verified=not restored,
                    restored=restored, external_publications=0,
                    runtime_files_sha256=digest(self.state["monitor_backup_files"]))
            finally:
                core.close()
        self.check()
        require(self.hub_snapshot() == before_main, "SCENARIO_MONITOR_CHANGED_MAIN_HISTORY")
        result["main_history_unchanged"] = True
        return result

    def run(self, mode):
        prerequisites = {"exercise": "bootstrap-owner", "snapshot": "exercise", "verify-restored": "snapshot",
                         "monitor-snapshot": "verify-restored", "monitor-restore": "monitor-snapshot"}
        if mode in prerequisites:
            require(self.state.get("phases", {}).get(prerequisites[mode], {}).get("state") == "passed"
                    and all(isinstance(self.state.get(key), str) and self.state[key]
                            for key in ("owner_id", "org_id", "postiz_api_key")), "SCENARIO_PHASE_PREREQUISITE_MISSING")
        self.phase(mode)
        result = {"bootstrap-owner": self.bootstrap, "exercise": self.exercise,
                  "snapshot": self.snapshot, "verify-restored": lambda: self.snapshot(restored=True),
                  "monitor-snapshot": self.monitor_acceptance,
                  "monitor-restore": lambda: self.monitor_acceptance(restored=True)}[mode]()
        self.check()
        self.state["phases"][mode]["state"] = "passed"
        self.save()
        return {"contract": CONTRACT, "mode": mode, "passed": True, "network_interfaces": ["lo"], **result}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--mode", choices=("bootstrap-owner", "exercise", "snapshot", "verify-restored",
                                         "monitor-snapshot", "monitor-restore"), required=True)
    args = parser.parse_args(argv)
    scenario = None
    try:
        scenario = Scenario(args.root, args.state, args.evidence)
        result = scenario.run(args.mode)
        atomic(args.evidence / (args.mode + ".json"), result, exclusive=True)
        print(json.dumps({"mode": args.mode, "passed": True, "external_publications": 0}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ScenarioError) else "SCENARIO_FAILED"
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", code):
            code = "SCENARIO_FAILED"
        # Never print an HTTP response, DSN, exception text, cookie or secret.
        result = {"mode": args.mode, "passed": False, "code": code, "exception": safe_exception_metadata(error)}
        metadata = safe_http_metadata(getattr(error, "http", None)) if isinstance(error, ScenarioError) else None
        if metadata is not None:
            result["http"] = metadata
        previous = safe_http_metadata(getattr(scenario, "last_http", None))
        if previous is not None:
            result["last_http"] = previous
        atomic(args.evidence / (args.mode + "-failed.json"), result, exclusive=True)
        print(json.dumps(result))
        return 1
    finally:
        if scenario is not None:
            scenario.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
