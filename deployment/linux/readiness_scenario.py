"""Приёмка только нового CI-стенда в отдельном lo-only netns, без отправки постов."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
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


class ScenarioError(RuntimeError):
    pass


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

    def check(self):
        namespace_guard(self.state)

    def save(self):
        atomic(self.state_path, self.state, private=True)

    def phase(self, mode):
        phases = self.state.setdefault("phases", {})
        require(mode not in phases, "SCENARIO_PHASE_ALREADY_STARTED")
        phases[mode] = {"state": "started", "at": time.time()}
        self.save()

    def call(self, method, path, *, allowed=(200,), **kwargs):
        self.check()
        require(method in {"GET", "POST"}, "SCENARIO_HTTP_SCOPE_INVALID")
        require(path in {"/api/auth/register", "/api/auth/login", "/api/user/self", "/api/editorial/route-setup",
                "/api/editorial/settings", "/api/editorial/control", "/api/editorial/route-admin", "/api/editorial/route-action"},
                "SCENARIO_HTTP_SCOPE_INVALID")
        if path == "/api/editorial/route-action":
            require(kwargs.get("json", {}).get("action") in {"preview", "preflight"}, "SCENARIO_EXTERNAL_ACTION_FORBIDDEN")
        if path == "/api/editorial/control" and "json" in kwargs:
            require(kwargs["json"].get("action") == "pause", "SCENARIO_EXTERNAL_ACTION_FORBIDDEN")
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ScenarioError("SCENARIO_HTTP_UNAVAILABLE") from None
        require(response.status_code in allowed and not response.is_redirect, "SCENARIO_HTTP_STATUS_INVALID")
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

    def admin(self, value, allowed=(200,)):
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
                page.get_by_role("heading", name="Редакция", exact=True).wait_for(state="visible", timeout=45000)
                page.wait_for_function("ids=>ids.every(id=>[...document.querySelectorAll('option')].some(e=>e.value===id))",
                                       arg=list(TARGETS.values()), timeout=45000)
                require(not errors, "SCENARIO_BROWSER_JAVASCRIPT_ERROR")
                self.evidence.mkdir(mode=0o755, parents=True, exist_ok=True)
                screenshot = page.screenshot(path=str(self.evidence / "editorial-synthetic.png"), full_page=True)
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

    def run(self, mode):
        prerequisites = {"exercise": "bootstrap-owner", "snapshot": "exercise", "verify-restored": "snapshot"}
        if mode in prerequisites:
            require(self.state.get("phases", {}).get(prerequisites[mode], {}).get("state") == "passed"
                    and all(isinstance(self.state.get(key), str) and self.state[key]
                            for key in ("owner_id", "org_id", "postiz_api_key")), "SCENARIO_PHASE_PREREQUISITE_MISSING")
        self.phase(mode)
        result = {"bootstrap-owner": self.bootstrap, "exercise": self.exercise,
                  "snapshot": self.snapshot, "verify-restored": lambda: self.snapshot(restored=True)}[mode]()
        self.check()
        self.state["phases"][mode]["state"] = "passed"
        self.save()
        return {"contract": CONTRACT, "mode": mode, "passed": True, "network_interfaces": ["lo"], **result}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--mode", choices=("bootstrap-owner", "exercise", "snapshot", "verify-restored"), required=True)
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
        atomic(args.evidence / (args.mode + "-failed.json"), {"mode": args.mode, "passed": False, "code": code}, exclusive=True)
        print(json.dumps({"mode": args.mode, "passed": False, "code": code}))
        return 1
    finally:
        if scenario is not None:
            scenario.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
