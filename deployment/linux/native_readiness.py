"""Full native acceptance in a disposable GitHub VM; never a server deployment.

The only application network is one loopback namespace. Its retained LAN origin
is an alias on that namespace's lo, not a connection to the working server.
Only explicitly selected, sanitized evidence leaves this disposable environment.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

from readiness_engine import Engine, LABEL_KEY
from readiness_docker import prepare as prepare_docker

GIB = 1024 ** 3
ORIGIN = "http://10.20.30.45:4007"
PROFILE = "legacy-private-lan-http-v1"
STARTUP_CONTRACT = "postiz-no-implicit-schema-v1"
SAFE_COMMAND = ["/bin/sh", "/adapter/start-production.sh"]
ENGINE_FAILURE_STAGES = frozenset({
    "NOT_STARTED", "GUARD", "UNIT_LIST_QUERY", "UNIT_FILE_LIST_QUERY", "ANCHOR_START", "SLICE_LIMITS",
    "SLICE_IDENTITY", "CGROUP_CAPTURE", "DAEMON_START", "DAEMON_READY", "EMPTY_ENGINE_CHECK",
    "CLEANUP_CONTAINERS", "CLEANUP_DAEMON_STOP", "CLEANUP_ANCHOR_STOP", "CLEANUP_CGROUP",
    "CLEANUP_SLICE_STOP", "CLEANUP_SLICE_REVERT",
})
SCENARIO_ARTIFACTS = frozenset({"bootstrap-owner.json", "bootstrap-owner-failed.json", "exercise.json",
    "exercise-failed.json", "snapshot.json", "snapshot-failed.json", "verify-restored.json",
    "verify-restored-failed.json", "portal.json", "default-cmd.json", "editorial-synthetic.png",
    "monitor-snapshot.json", "monitor-snapshot-failed.json", "monitor-restore.json", "monitor-restore-failed.json"})
SCENARIO_FILE_LIMIT = 8 * 1024 * 1024
SCENARIO_TOTAL_LIMIT = 16 * 1024 * 1024
TEST_CLIENT_MEMORY = "2G"
COLLECTION_FAILURE_CODES = frozenset({"SCENARIO_PRODUCER_CLEANUP_UNCONFIRMED", "SCENARIO_EVIDENCE_PATH_INVALID",
    "SCENARIO_EVIDENCE_OWNER_INVALID", "SCENARIO_EVIDENCE_ENTRY_INVALID", "SCENARIO_EVIDENCE_SIZE_INVALID",
    "SCENARIO_EVIDENCE_CHANGED", "SCENARIO_EVIDENCE_STAGING_INVALID"})
KNOWN_CLEANUP_CODES = frozenset({"owned_container_cleanup_unconfirmed", "owned_unit_stop_unconfirmed",
    "owned_cgroup_cleanup_unconfirmed", "owned_slice_stop_unconfirmed"})
BASE = "ghcr.io/gitroomhq/postiz-app@sha256:d2b37802e96757729fdea2d3d71287f841c2b619faa1cb4c21dac07df657fde1"
PINS = {
    "postgres": "postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24",
    "redis": "redis@sha256:46f15cf64fdef63e483dcda99e74e3d51c017b1175547b6bb874152ee2391aad",
    "temporal-postgres": "postgres@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d",
    "temporal": "temporalio/auto-setup@sha256:607d68caa111338d754771efb876c92dfcdae06d056e4530bb31cd0f37406e6a",
    "elasticsearch": "elasticsearch@sha256:9a6443f55243f6acbfeb4a112d15eb3b9aac74bf25e0e39fa19b3ddd3a6879d0",
}
# Measured on the earlier isolated image of this same pinned upstream. Reserve
# two native layer sets plus dependencies/safety, rather than assuming 14 GB fits.
PREVIOUS_NATIVE_IMAGE_BYTES = 11804114796
REQUIRED_DISK_BYTES = 2 * PREVIOUS_NATIVE_IMAGE_BYTES + 8 * GIB
EPHEMERAL_UNUSED_SDKS = (
    "/usr/local/lib/android", "/usr/share/dotnet", "/opt/ghc", "/usr/local/.ghcup",
    "/usr/local/swift", "/usr/local/share/powershell",
)
QUEUES = "bluesky,devto,discord,dribbble,email,email-bulk,facebook,gmb,hashnode,instagram,kick,lemmy,linkedin,listmonk,mastodon,medium,mewe,moltbook,nostr,pinterest,reddit,skool,slack,telegram,threads,tiktok,tumblr,twitch,vk,whop,wordpress,wrapcast,x,youtube,main,max"


class ReadinessError(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def count_logged_schema_statements(text):
    """Count SQL events after PostgreSQL itself classifies log_statement=ddl.

    Simple and extended protocols use different prefixes. SQL can begin with
    comments or span lines, so neither command words nor SQL content are parsed.
    ERROR/DETAIL/STATEMENT records are not LOG events.
    """
    event = re.compile(r"^(?:[^\r\n]*\]\s+)?LOG:[ \t]+(?:statement:|execute[ \t]+[^:\r\n]+:)")
    return sum(bool(event.match(line)) for line in text.splitlines())


def proxy_configuration(outer):
    return ("user www;worker_processes 1;pid /tmp/nginx.pid;error_log /tmp/nginx-error.log warn;"
        "events{worker_connections 512;}http{include /etc/nginx/mime.types;access_log off;"
        "client_body_temp_path /tmp/client-body;proxy_temp_path /tmp/proxy;"
        "fastcgi_temp_path /tmp/fastcgi;uwsgi_temp_path /tmp/uwsgi;scgi_temp_path /tmp/scgi;" + outer +
        "server{listen 127.0.0.1:14007;client_max_body_size 15m;location /api/ {proxy_pass http://127.0.0.1:3000/;"
        "proxy_set_header Host $http_host;proxy_set_header X-Forwarded-Proto $scheme;}"
        "location / {proxy_pass http://127.0.0.1:4200;proxy_set_header Host $http_host;"
        "proxy_set_header X-Forwarded-Proto $scheme;}}}")


def redact_proxy_error(raw, values):
    text = raw.decode("utf-8", errors="replace")
    for value in sorted(values, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?im)^.*(?:cookie|authorization).*$", "[REDACTED_AUTH_LINE]", text)
    return re.sub(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "[REDACTED_JWT]", text).encode()


def collection_failure_metadata(error, closed):
    code = str(error) if isinstance(error, ReadinessError) and str(error) in COLLECTION_FAILURE_CODES else (
        "SCENARIO_EVIDENCE_IO_FAILED" if isinstance(error, OSError) else "SCENARIO_EVIDENCE_METADATA_INVALID")
    result = {"code": code}
    if isinstance(closed, dict):
        result["cleanup"] = {"closed": closed.get("state") == "closed", "cgroup_empty": closed.get("cgroup_empty") is True}
        if isinstance(closed.get("cleanup_errors"), list) and len(closed["cleanup_errors"]) <= 100:
            result["cleanup"]["error_count"] = len(closed["cleanup_errors"])
            known = sorted({value for value in closed["cleanup_errors"] if isinstance(value, str) and value in KNOWN_CLEANUP_CODES})
            if known:
                result["cleanup"]["known_codes"] = known
    entry = getattr(error, "evidence_entry", None)
    if (isinstance(entry, dict) and set(entry) == {"name", "regular", "single_link", "owner_matches", "mode", "bytes"}
            and isinstance(entry.get("name"), str) and entry["name"] in SCENARIO_ARTIFACTS
            and all(type(entry.get(key)) is bool for key in ("regular", "single_link", "owner_matches"))
            and type(entry.get("mode")) is int and 0 <= entry["mode"] <= 0o7777
            and type(entry.get("bytes")) is int and 0 <= entry["bytes"] <= SCENARIO_FILE_LIMIT + 1):
        result["entry"] = dict(entry)
    return result


def portal_receive(wire, initial=b"", *, marker=None, size=None, cap, deadline, clock=None):
    clock = clock or time.monotonic
    value = bytearray(initial)
    while True:
        if len(value) > cap:
            raise ReadinessError("PORTAL_WIRE_CAP_EXCEEDED")
        remaining = deadline - clock()
        if remaining <= 0:
            raise ReadinessError("PORTAL_WIRE_TIMEOUT")
        if (marker is not None and marker in value) or (size is not None and len(value) >= size):
            return bytes(value)
        if len(value) == cap:
            raise ReadinessError("PORTAL_WIRE_CAP_EXCEEDED")
        wire.settimeout(min(2, remaining))
        try:
            chunk = wire.recv(min(4096, cap - len(value)))
        except (TimeoutError, OSError):
            raise ReadinessError("PORTAL_WIRE_UNAVAILABLE") from None
        if not chunk:
            raise ReadinessError("PORTAL_WIRE_EOF")
        value.extend(chunk)


def wait_portal_rfb(*, connect=None, clock=None, sleep=None):
    connect, clock, sleep = connect or socket.create_connection, clock or time.monotonic, sleep or time.sleep
    deadline = clock() + 15
    while clock() < deadline:
        try:
            with connect(("127.0.0.1", 5900), timeout=min(1, max(0.05, deadline - clock()))) as wire:
                banner = portal_receive(wire, marker=b"\n", cap=64, deadline=min(deadline, clock() + 2), clock=clock)
            if not re.fullmatch(rb"RFB [0-9]{3}\.[0-9]{3}\n", banner):
                raise ReadinessError("PORTAL_RFB_PROTOCOL_INVALID")
            return
        except (OSError, TimeoutError):
            pass
        except ReadinessError as error:
            if str(error) not in {"PORTAL_WIRE_EOF", "PORTAL_WIRE_TIMEOUT", "PORTAL_WIRE_UNAVAILABLE"}:
                raise
        sleep(min(0.1, max(0, deadline - clock())))
    raise ReadinessError("PORTAL_RFB_NOT_READY")


def portal_websocket(cookie, *, connect=None, clock=None):
    connect, clock = connect or socket.create_connection, clock or time.monotonic
    deadline = clock() + 10
    key = base64.b64encode(os.urandom(16)).decode()
    message = ('GET /owner-browser/websockify HTTP/1.1\r\nHost: 10.20.30.45:4007\r\nOrigin: ' + ORIGIN +
        '\r\nCookie: auth=' + cookie + '\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n'
        'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\nSec-WebSocket-Key: ' + key + '\r\n\r\n')
    try:
        with connect(("10.20.30.45", 4007), timeout=2) as wire:
            wire.sendall(message.encode())
            raw = portal_receive(wire, marker=b"\r\n\r\n", cap=8192, deadline=deadline, clock=clock)
            header, body = raw.split(b"\r\n\r\n", 1)
            lines = header.split(b"\r\n")
            if not re.fullmatch(rb"HTTP/1\.1 101(?: [^\r\n]*)?", lines[0]):
                raise ReadinessError("PORTAL_WEBSOCKET_STATUS_INVALID")
            headers = {}
            for line in lines[1:]:
                name, separator, value = line.partition(b":")
                name = name.strip().lower()
                if not separator or name in headers:
                    raise ReadinessError("PORTAL_WEBSOCKET_HEADER_INVALID")
                headers[name] = value.strip()
            # RFC 6455 mandates this nonce digest; it is not a password/signature hash.
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode(), usedforsecurity=False).digest())
            if (headers.get(b"sec-websocket-accept") != expected or headers.get(b"upgrade", b"").lower() != b"websocket"
                    or b"upgrade" not in [part.strip().lower() for part in headers.get(b"connection", b"").split(b",")]):
                raise ReadinessError("PORTAL_WEBSOCKET_HEADER_INVALID")
            banner, consumed, fragmented = b"", 0, False
            for _ in range(32):
                body = portal_receive(wire, body, size=2, cap=512, deadline=deadline, clock=clock)
                fin, opcode = bool(body[0] & 0x80), body[0] & 0x0f
                if (body[0] & 0x70 or body[1] & 0x80 or body[1] & 0x7f >= 126
                        or opcode != (0 if fragmented else 2)):
                    raise ReadinessError("PORTAL_WEBSOCKET_FRAME_INVALID")
                length = body[1] & 0x7f
                consumed += 2 + length
                if consumed > 512:
                    raise ReadinessError("PORTAL_WIRE_CAP_EXCEEDED")
                body = portal_receive(wire, body, size=2 + length, cap=512, deadline=deadline, clock=clock)
                banner, body = banner + body[2:2 + length], body[2 + length:]
                fragmented = not fin
                if len(banner) > 12:
                    raise ReadinessError("PORTAL_RFB_PROTOCOL_INVALID")
                if len(banner) == 12 and not fragmented:
                    if not re.fullmatch(rb"RFB [0-9]{3}\.[0-9]{3}\n", banner):
                        raise ReadinessError("PORTAL_RFB_PROTOCOL_INVALID")
                    return
            raise ReadinessError("PORTAL_WIRE_CAP_EXCEEDED")
    except (OSError, TimeoutError):
        raise ReadinessError("PORTAL_WIRE_UNAVAILABLE") from None


def helper_execstart_override(unit, project):
    helpers = {"max-postiz-login.service": "login-browser.py",
               "max-postiz-editorial-restart.service": "restart-editorial.py"}
    root = str(project)
    if unit not in helpers or not re.fullmatch(r"/[A-Za-z0-9_/-]+", root) or ".." in root.split("/"):
        raise ReadinessError("NATIVE_HELPER_UNIT_OVERRIDE_INVALID")
    return ("ExecStart=\nExecStart=" + root + "/.venv/bin/python " + root +
            "/deployment/linux/" + helpers[unit] + " --root " + root + "\n")


def wait_restart_expiry(value, project, *, clock=None, monotonic=None, sleep=None):
    clock, monotonic, sleep = clock or time.time, monotonic or time.monotonic, sleep or time.sleep
    fields = {"contract", "request_id", "root", "issued_at", "expires_at", "runtime_generation", "previous_worker_id"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("contract") != "editorial-restart-request-v1" or value.get("root") != str(project)
            or not isinstance(value.get("request_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", value["request_id"])
            or any(type(value.get(key)) is not int for key in ("issued_at", "expires_at"))
            or not 0 < value["expires_at"] - value["issued_at"] <= 60
            or value["issued_at"] > clock()
            or not isinstance(value.get("runtime_generation"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["runtime_generation"])):
        raise ReadinessError("NATIVE_RESTART_LEASE_INVALID")
    try:
        if str(uuid.UUID(value["previous_worker_id"])) != value["previous_worker_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ReadinessError("NATIVE_RESTART_LEASE_INVALID") from None
    start = monotonic()
    while clock() <= value["expires_at"]:
        if monotonic() - start >= 65:
            raise ReadinessError("NATIVE_RESTART_LEASE_WAIT_EXCEEDED")
        sleep(min(1, max(0.05, value["expires_at"] - clock() + 0.05)))
    if monotonic() - start > 65:
        raise ReadinessError("NATIVE_RESTART_LEASE_WAIT_EXCEEDED")
    return {"waited_seconds": round(monotonic() - start, 3), "lease_mutated": False}


def read_restart_lease(project, owner_uid):
    folder = os.open(Path(project) / "data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        info = os.fstat(folder)
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise ReadinessError("NATIVE_RESTART_LEASE_PRIVATE_REQUIRED")
        descriptor = os.open("editorial-restart-request.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 4096):
            raise ReadinessError("NATIVE_RESTART_LEASE_PRIVATE_REQUIRED")
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        if len(raw) != info.st_size or (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ReadinessError("NATIVE_RESTART_LEASE_CHANGED")
        return raw
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(folder)


def validate_scenario_entry(name, info, owner_uid):
    if (name not in SCENARIO_ARTIFACTS or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or info.st_uid != owner_uid or info.st_mode & 0o022
            or not 0 <= info.st_size <= SCENARIO_FILE_LIMIT):
        raise ReadinessError("SCENARIO_EVIDENCE_ENTRY_INVALID")


def disabled_policy_template(raw):
    required = {"enabled", "approval_mode", "review_rubric", "target_id", "source_id", "allowed_hosts",
                "rights_evidence", "owner_instruction", "min_gap_seconds", "max_per_day", "max_source_age_hours"}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ReadinessError("NATIVE_DISABLED_POLICY_INVALID") from None
    if not isinstance(value, dict) or set(value) != required or value.get("enabled") is not False:
        raise ReadinessError("NATIVE_DISABLED_POLICY_INVALID")
    return value


def initialize_disabled_policy(project, owner_uid, owner_gid):
    """One new private fixture, exact public template, never a policy update."""
    project = Path(project)
    if (not project.is_absolute() or ".." in project.parts
            or any(path.is_symlink() for path in (project, *project.parents))):
        raise ReadinessError("NATIVE_POLICY_PATH_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    project_fd = os.open(project, flags)
    descriptors = [project_fd]
    try:
        config_fd = os.open("config", flags, dir_fd=project_fd)
        descriptors.append(config_fd)
        source = os.open("hourly_article_policy.example.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=config_fd)
        descriptors.append(source)
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 65536:
            raise ReadinessError("NATIVE_DISABLED_POLICY_INVALID")
        raw = os.read(source, 65537)
        disabled_policy_template(raw)
        data_fd = os.open("data", flags, dir_fd=project_fd)
        descriptors.append(data_fd)
        private = os.fstat(data_fd)
        if private.st_uid != owner_uid or private.st_mode & 0o077:
            raise ReadinessError("NATIVE_POLICY_PRIVATE_DIRECTORY_REQUIRED")
        try:
            target = os.open("hourly-article-policy.local.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=data_fd)
        except FileExistsError:
            raise ReadinessError("NATIVE_POLICY_ALREADY_EXISTS") from None
        with os.fdopen(target, "wb") as output:
            os.fchown(output.fileno(), owner_uid, owner_gid)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        return {"created": True, "enabled": False, "exact_template_sha256": digest(raw), "targets_created": 0,
                "approvals_created": 0, "external_mutations": 0}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def collect_scenario_evidence(project, staging, owner_uid):
    """Copy only finished child evidence through held directory/file descriptors."""
    project, staging = Path(project), Path(staging)
    if (not project.is_absolute() or not staging.is_absolute() or ".." in project.parts
            or ".." in staging.parts or staging.is_relative_to(project)
            or any(path.is_symlink() for base in (project, staging) for path in (base, *base.parents))):
        raise ReadinessError("SCENARIO_EVIDENCE_PATH_INVALID")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    project_fd = os.open(project, directory_flags)
    descriptors = [project_fd]
    try:
        data_fd = os.open("data", directory_flags, dir_fd=project_fd)
        descriptors.append(data_fd)
        source_fd = os.open("ci-public", directory_flags, dir_fd=data_fd)
        descriptors.append(source_fd)
        for descriptor in (data_fd, source_fd):
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
                raise ReadinessError("SCENARIO_EVIDENCE_OWNER_INVALID")
        names = sorted(os.listdir(source_fd))
        if len(names) > len(SCENARIO_ARTIFACTS):
            raise ReadinessError("SCENARIO_EVIDENCE_ENTRY_INVALID")
        content, total = {}, 0
        for name in names:
            if name not in SCENARIO_ARTIFACTS:
                raise ReadinessError("SCENARIO_EVIDENCE_ENTRY_INVALID")
            # O_NONBLOCK ensures a substituted FIFO cannot block root collection.
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
            try:
                before = os.fstat(descriptor)
                try:
                    validate_scenario_entry(name, before, owner_uid)
                except ReadinessError as error:
                    error.evidence_entry = {"name": name, "regular": stat.S_ISREG(before.st_mode),
                        "single_link": before.st_nlink == 1, "owner_matches": before.st_uid == owner_uid,
                        "mode": stat.S_IMODE(before.st_mode), "bytes": min(max(before.st_size, 0), SCENARIO_FILE_LIMIT + 1)}
                    raise
                raw = bytearray()
                while chunk := os.read(descriptor, min(1024 * 1024, SCENARIO_FILE_LIMIT + 1 - len(raw))):
                    raw.extend(chunk)
                    if len(raw) > SCENARIO_FILE_LIMIT:
                        raise ReadinessError("SCENARIO_EVIDENCE_SIZE_INVALID")
                after = os.fstat(descriptor)
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != before.st_size):
                    raise ReadinessError("SCENARIO_EVIDENCE_CHANGED")
                total += len(raw)
                if total > SCENARIO_TOTAL_LIMIT:
                    raise ReadinessError("SCENARIO_EVIDENCE_SIZE_INVALID")
                content[name] = bytes(raw)
            finally:
                os.close(descriptor)
        if sorted(os.listdir(source_fd)) != names:
            raise ReadinessError("SCENARIO_EVIDENCE_CHANGED")
        destination_fd = os.open(staging, directory_flags)
        descriptors.append(destination_fd)
        target = os.fstat(destination_fd)
        if not stat.S_ISDIR(target.st_mode) or target.st_uid != os.geteuid():
            raise ReadinessError("SCENARIO_EVIDENCE_STAGING_INVALID")
        for name, raw in content.items():
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=destination_fd)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        return {"file_count": len(content), "bytes": total, "owner_verified": True,
                "files": {name: digest(raw) for name, raw in content.items()}, "privacy_scan_still_required": True}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def bounded_diagnostic_output(command, environment, *, limit=65536, timeout=10):
    """Only an owned read-only Docker log client; never retain a cut final line."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
    captured = []

    def read():
        try:
            captured.append(process.stdout.read(limit + 1))
        except OSError:
            captured.append(b"")

    worker, started, timed_out, failed, cleanup_failed = None, False, False, False, False
    try:
        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        started = True
        worker.join(timeout=timeout)
        timed_out = worker.is_alive()
    except BaseException:
        failed = True
    finally:
        try:
            try:
                if process.poll() is None:
                    process.terminate()
            except OSError:
                pass  # The wait/kill path still verifies this owned client.
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_failed = True
        finally:
            try:
                if started:
                    worker.join(timeout=2)
                    cleanup_failed = cleanup_failed or worker.is_alive()
            finally:
                process.stdout.close()
    if failed or cleanup_failed:
        raise ReadinessError("DEPENDENCY_DIAGNOSTIC_CAPTURE_FAILED")
    raw = captured[0] if captured else b""
    truncated = len(raw) > limit or timed_out
    if truncated:
        raw = raw[:limit]
        raw = raw.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in raw else b""
    return raw, {"byte_limit": limit, "captured_bytes": len(raw), "truncated": truncated, "timed_out": timed_out}


def github_guard(environment=None, platform=None, uid=None):
    env = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    uid = os.geteuid() if uid is None and hasattr(os, "geteuid") else uid
    if (platform != "linux" or uid != 0 or env.get("GITHUB_ACTIONS") != "true"
            or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or not re.fullmatch(r"[a-f0-9]{40}", env.get("GITHUB_SHA", ""))):
        raise ReadinessError("EPHEMERAL_GITHUB_RUNNER_REQUIRED")


def private_write(path, value, *, exclusive=True):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def disk_preflight(path, workspace):
    github_guard()
    path, workspace = Path(path).resolve(), Path(workspace).resolve()
    initial = shutil.disk_usage(path).free
    removed = []
    for name in EPHEMERAL_UNUSED_SDKS:
        if shutil.disk_usage(path).free >= REQUIRED_DISK_BYTES:
            break
        target = Path(name)
        if target.is_symlink() or target.resolve() != target or workspace.is_relative_to(target):
            raise ReadinessError("EPHEMERAL_SDK_PATH_INVALID")
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(name)
    free = shutil.disk_usage(path).free
    if free < REQUIRED_DISK_BYTES:
        raise ReadinessError("NATIVE_IMAGE_DISK_CAPACITY_REQUIRED")
    return {"free_before_bytes": initial, "free_after_bytes": free, "required_bytes": REQUIRED_DISK_BYTES,
            "estimate_native_image_bytes": PREVIOUS_NATIVE_IMAGE_BYTES, "ephemeral_sdk_dirs_removed": removed,
            "docker_prune_performed": False}


def synthetic_secrets(value):
    result = set()
    for key, item in value.items():
        if isinstance(item, dict):
            result.update(synthetic_secrets(item))
        elif isinstance(item, str) and any(word in key.lower() for word in ("password", "secret", "token", "key", "cookie", "dsn")):
            if len(item) >= 8:
                result.add(item)
            try:
                password = urlsplit(item).password
                if password and len(password) >= 8:
                    result.add(unquote(password))
            except ValueError:
                pass
    return result


def scan_public(folder, secrets_to_find):
    matches = 0
    for path in Path(folder).rglob("*"):
        if path.is_symlink():
            raise ReadinessError("PUBLIC_ARTIFACT_SYMLINK_FORBIDDEN")
        if path.is_file():
            if path.suffix not in {".json", ".png", ".txt", ".xml"}:
                raise ReadinessError("PUBLIC_ARTIFACT_TYPE_INVALID")
            raw = path.read_bytes()
            matches += sum(secret.encode() in raw or secret in path.relative_to(folder).as_posix() for secret in secrets_to_find)
    if matches:
        raise ReadinessError("PUBLIC_ARTIFACT_SECRET_MATCH")
    return matches


def finalize_public(staging, report, secrets_to_find, validated):
    """Publish a directory only after the entire candidate set passes privacy."""
    staging, validated = Path(staging), Path(validated)
    if (staging.is_symlink() or validated.exists() or validated.is_symlink()
            or staging.parent.resolve() != validated.parent.resolve() or staging == validated):
        raise ReadinessError("PUBLIC_FINALIZATION_PATH_INVALID")
    candidate = dict(report, privacy_scan_completed=True, public_secret_matches=0)
    (staging / "acceptance.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
    try:
        scan_public(staging, secrets_to_find)
    except BaseException:
        quarantine = staging.with_name(staging.name + "-quarantine-" + uuid.uuid4().hex)
        staging.rename(quarantine)
        quarantine.chmod(0o700)
        validated.mkdir(mode=0o755)
        (validated / "acceptance.json").write_text(json.dumps({
            "accepted": False, "code": "PUBLIC_ARTIFACT_PRIVACY_FAILED", "unvalidated_files_quarantined": True,
            "external_publication_performed_by_release_checks": False}))
        (validated / ".privacy-verified.json").write_text(json.dumps({"contract": "native-public-privacy-v1",
            "files": {"acceptance.json": digest((validated / "acceptance.json").read_bytes())}, "secret_matches": 0}))
        return False
    hashes = {path.name: digest(path.read_bytes()) for path in staging.iterdir() if path.is_file()}
    (staging / "SHA256SUMS.txt").write_text("".join(value + "  " + name + "\n" for name, value in sorted(hashes.items())))
    (staging / ".privacy-verified.json").write_text(json.dumps({"contract": "native-public-privacy-v1",
        "files": {path.name: digest(path.read_bytes()) for path in staging.iterdir() if path.is_file()}, "secret_matches": 0}))
    staging.rename(validated)
    for path in validated.iterdir():
        if path.is_file():
            path.chmod(0o644)
    validated.chmod(0o755)
    return True


class Acceptance:
    def __init__(self, workspace, evidence):
        self.workspace, self.public = Path(workspace).resolve(), Path(evidence)
        self.job = Path(tempfile.mkdtemp(prefix="mp-ready-", dir=os.environ["RUNNER_TEMP"]))
        self.private = self.job / "private"
        self.private.mkdir(mode=0o700)
        self.engine = None
        self.unit_names = []
        self.created_opt = None
        self.current_phase = "preflight"
        self.report = {"contract": "three-platform-native-acceptance-v1", "accepted": False,
                       "source_commit": os.environ["GITHUB_SHA"], "origin": ORIGIN, "origin_profile": PROFILE,
                       "scope": "ephemeral_github_hosted_native_full_stack_no_external_publications",
                       "external_publication_performed_by_release_checks": False,
                       "working_server_contacted": False, "pika_actions": 0,
                       "https_transport_tested": False, "checks": {}}

    def phase(self, name):
        self.current_phase = name
        print(json.dumps({"phase": name}), flush=True)

    def run(self, command, *, name, timeout=300, check=True, input=None, env=None):
        logfile = self.private / (name + ".log")
        with logfile.open("xb") as log:
            result = subprocess.run(command, input=input, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False, env=env)
        if check and result.returncode != 0:
            raise ReadinessError("NATIVE_STAGE_FAILED_" + name.upper().replace("-", "_"))
        return result

    def source_copy(self):
        commit = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.workspace),
            "-C", str(self.workspace), "rev-parse", "HEAD"], text=True).strip()
        if commit != os.environ["GITHUB_SHA"]:
            raise ReadinessError("SOURCE_COMMIT_MISMATCH")
        clean = subprocess.run(["git", "-c", "safe.directory=" + str(self.workspace), "-C", str(self.workspace),
                                "diff", "--quiet", "HEAD", "--"], check=False)
        if clean.returncode != 0:
            raise ReadinessError("SOURCE_WORKTREE_MODIFIED")
        tree = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.workspace),
            "-C", str(self.workspace), "rev-parse", "HEAD^{tree}"], text=True).strip()
        if not re.fullmatch(r"[a-f0-9]{40}", tree):
            raise ReadinessError("SOURCE_TREE_INVALID")
        names = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.workspace),
            "-C", str(self.workspace), "ls-files", "-z"]).decode().split("\0")
        self.created_opt = Path("/opt/max-postiz-readiness-" + self.engine.label_value[:12])
        if self.created_opt.exists() or self.created_opt.is_symlink():
            raise ReadinessError("OWN_PROJECT_ALREADY_EXISTS")
        self.created_opt.mkdir(mode=0o755)
        self.project = self.created_opt / "project"
        self.project.mkdir(mode=0o755)
        hashes = {}
        for name in filter(None, names):
            relative = Path(name)
            source = self.workspace / relative
            if relative.is_absolute() or ".." in relative.parts or source.is_symlink():
                raise ReadinessError("SOURCE_PATH_INVALID")
            raw = source.read_bytes()
            target = self.project / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o755 if name.endswith(".sh") else 0o644)
            hashes[name] = digest(raw)
        version = tomllib.loads((self.project / "pyproject.toml").read_text())["project"]["version"]
        if version != "0.14.0":
            raise ReadinessError("READINESS_CANDIDATE_VERSION_INVALID")
        self.report.update(version=version, source_tree=tree, source_file_count=len(hashes),
            source_manifest_sha256=digest(json.dumps(hashes, sort_keys=True).encode()))
        private_write(self.private / "source-files.json", hashes)

    def dependencies(self):
        self.phase("native_dependencies")
        self.run(["apt-get", "update", "-qq"], name="apt-update", timeout=300)
        self.run(["apt-get", "install", "-y", "--no-install-recommends", "xvfb", "x11vnc", "websockify",
                  "novnc", "polkitd", "iproute2", "util-linux", "ca-certificates", "curl"],
                 name="apt-native", timeout=600)
        self.python = self.project / ".venv/bin/python"
        self.run([sys.executable, "-m", "venv", str(self.project / ".venv")], name="venv", timeout=60)
        self.run([str(self.python), "-m", "pip", "install", "--disable-pip-version-check", "-r",
                  str(self.project / "requirements.lock"), "-r", str(self.project / "requirements-dev.lock"),
                  "-r", str(self.project / "requirements-browser.lock")], name="pip-locked", timeout=600)
        self.run([str(self.python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e",
                  str(self.project)], name="pip-project", timeout=120)
        self.run([str(self.python), "-m", "pip", "check"], name="pip-check", timeout=60)
        self.browsers = self.project / ".playwright"
        self.browser_env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(self.browsers), PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="120000")
        self.run([str(self.python), "-m", "playwright", "install", "--with-deps", "chrome"],
                 name="playwright-native", timeout=600, env=self.browser_env)
        self.report["checks"]["locked_native_dependencies"] = True

    def image_pull(self):
        self.phase("pinned_images")
        for role, image in {"postiz": BASE, **PINS}.items():
            with (self.private / ("pull-" + role + ".log")).open("wb") as log:
                self.engine.docker("pull", image, timeout=900, stdout=log, text=False)
            value = json.loads(self.engine.docker("image", "inspect", image).stdout)[0]
            if image not in value.get("RepoDigests", []):
                raise ReadinessError("PINNED_IMAGE_DIGEST_MISMATCH")
        helptext = self.engine.docker("build", "--help").stdout
        if "--resource" not in helptext:
            raise ReadinessError("NATIVE_DOCKER_BUILD_RESOURCE_UNSUPPORTED")

    def docker_versions(self):
        value = json.loads(self.engine.docker("version", "--format", "{{json .}}").stdout)
        if value["Client"]["Version"] != "29.9.2" or value["Server"]["Version"] != "29.9.2":
            raise ReadinessError("NATIVE_DOCKER_PIN_VERSION_MISMATCH")
        self.run(self.engine.namespace_command([str(self.engine.docker_binary), "buildx", "version"]),
                 name="buildx-version", timeout=30, env=self.engine.environment)
        self.run(self.engine.namespace_command([str(self.engine.docker_binary), "buildx", "inspect", "default"]),
                 name="buildkit-version", timeout=30, env=self.engine.environment)
        buildx = (self.private / "buildx-version.log").read_text()
        buildkit = (self.private / "buildkit-version.log").read_text()
        plugin = re.search(r"\bv(\d+\.\d+\.\d+)(?:\s|$)", buildx)
        backend = re.search(r"(?mi)^\s*BuildKit(?:\s+version)?:\s+v?(\d+\.\d+\.\d+)(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)", buildkit)
        if not plugin or not backend or tuple(map(int, backend[1].split("."))) < (0, 31, 0):
            raise ReadinessError("NATIVE_BUILDKIT_RESOURCE_CAPABILITY_REQUIRED")
        self.report["checks"]["actual_docker_versions"] = {"client": value["Client"]["Version"],
            "server": value["Server"]["Version"], "buildx": plugin[1], "buildkit": backend[1],
            "buildkit_inspect_sha256": digest(buildkit.encode()), "host_binaries_changed": False}

    def build_flags(self):
        limits = self.engine.commonlimits
        return ["--resource", "memory=" + str(limits["native_memory_bytes"]),
                "--resource", "memory-swap=" + str(limits["native_memory_bytes"] + limits["native_swap_bytes"]),
                "--resource", "cpu-period=100000", "--resource", "cpu-quota=400000",
                "--cgroup-parent=" + self.engine.cgroup_parent, "--network=none", "--progress=plain",
                "--label", LABEL_KEY + "=" + self.engine.label_value]

    def build(self):
        self.phase("native_build")
        context = self.project / "deployment/postiz-max"
        self.tag = "max-postiz-linux:0.14.0-native-" + self.engine.label_value[:12]
        files = {path.relative_to(context).as_posix(): digest(path.read_bytes())
                 for path in context.rglob("*") if path.is_file()}
        self.report["node_context_sha256"] = digest(json.dumps(files, sort_keys=True).encode())
        self.report["node_context_files"] = len(files)
        self.report["node_context_source_sha256"] = files
        # A real resource/network probe on the same pinned base precedes heavy RUNs.
        probe = self.engine.root / "probe"
        probe.mkdir()
        script = "const fs=require('node:fs');console.log('RESOURCE_PROOF '+JSON.stringify({memory:fs.readFileSync('/sys/fs/cgroup/memory.max','utf8').trim(),swap:fs.readFileSync('/sys/fs/cgroup/memory.swap.max','utf8').trim(),cpu:fs.readFileSync('/sys/fs/cgroup/cpu.max','utf8').trim(),devices:fs.readFileSync('/proc/net/dev','utf8').split('\\n').slice(2).filter(Boolean).map(x=>x.split(':')[0].trim()),routes:fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').slice(1),cgroup:fs.readFileSync('/proc/self/cgroup','utf8'),cgroup_mounts:fs.readFileSync('/proc/self/mountinfo','utf8').split('\\n').filter(x=>x.includes(' - cgroup2 '))}))"
        (probe / "Dockerfile").write_text("FROM " + BASE + "\nRUN " + json.dumps(["node", "-e", script]) + "\n")
        self.run(self.engine.namespace_command([str(self.engine.docker_binary), "build", "--builder", "default", *self.build_flags(), "--tag",
            "max-postiz-linux:resource-" + self.engine.label_value[:12], str(probe)]),
            name="resource-probe", timeout=180, env=self.engine.environment)
        raw = (self.private / "resource-probe.log").read_text()
        match = re.search(r"RESOURCE_PROOF (\{[^\n]+\})", raw)
        if not match:
            raise ReadinessError("NATIVE_RESOURCE_PROOF_MISSING")
        observed = json.loads(match[1])
        limits = self.engine.commonlimits
        self.report["checks"]["build_resource_probe_observed"] = observed
        expected = {"memory": str(limits["native_memory_bytes"]), "swap": str(limits["native_swap_bytes"]),
                    "cpu": "400000 100000", "devices": ["lo"], "routes": []}
        if any(observed.get(key) != value for key, value in expected.items()):
            raise ReadinessError("NATIVE_RESOURCE_OR_NETWORK_FENCE_INVALID")
        self.report["checks"]["actual_build_resource_and_network_fence"] = observed
        wrapper = self.engine.root / "build.cjs"
        wrapper.write_text("const{spawnSync}=require('node:child_process');const{main}=require(" +
            json.dumps(str(context / "build-production-image.cjs")) + ");const flags=" + json.dumps([flag for flag in self.build_flags() if flag != "--network=none"]) +
            ";function run(binary,args,options){if(args[0]==='--context'&&args[2]==='build')args=[...args.slice(0,3),...flags,...args.slice(3)];return spawnSync(binary,args,options);}try{console.log(JSON.stringify(main(" +
            json.dumps(["--build", "--origin", ORIGIN, "--origin-profile", PROFILE, "--tag", self.tag]) +
            ",run," + json.dumps(str(context)) + ",process.env)));}catch(error){console.error(error.message);process.exitCode=1;}")
        command = self.engine.namespace_command(["node", str(wrapper)])
        self.run(command, name="full-native-build", timeout=7200, env=self.engine.environment)
        if any(digest((context / name).read_bytes()) != sha for name, sha in files.items()):
            raise ReadinessError("NATIVE_CONTEXT_CHANGED")
        image = json.loads(self.engine.docker("image", "inspect", self.tag).stdout)[0]
        self.image = image["Id"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image):
            raise ReadinessError("NATIVE_IMAGE_ID_INVALID")
        self.report.update(image_id=self.image, image_tag=self.tag, full_native_build_completed=True,
                           default_cmd_executed=False, build_network="none")

    def inspect_image(self):
        self.phase("compiled_artifacts")
        configuration = json.loads(self.engine.docker("image", "inspect", self.image).stdout)[0]["Config"]
        if configuration.get("Entrypoint") not in (None, []) or configuration.get("Cmd") != SAFE_COMMAND:
            raise ReadinessError("IMAGE_STARTUP_CONTRACT_INVALID")
        script = r"""const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),cp=require('node:child_process');
const required=[];for(const app of ['backend','orchestrator']){
 const root='/app/apps/'+app+'/dist/';required.push(root+'apps/'+app+'/src/main.js');
 for(const file of ['integration.manager.js','social/max-browser.provider.js'])required.push(root+'libraries/nestjs-libraries/src/integrations/'+file);
 required.push(root+'libraries/nestjs-libraries/src/temporal/temporal.module.js');}
for(const file of ['max-browser.guard.js','api/routes/editorial.controller.js','api/api.module.js'])required.push('/app/apps/backend/dist/apps/backend/src/'+file);
for(const file of required){if(!fs.statSync(file).isFile())throw Error('COMPILED_NATIVE_MISSING');}
for(const app of ['backend','orchestrator']){const p='/app/apps/'+app+'/dist/apps/'+app+'/src/main.js';if(cp.spawnSync(process.execPath,['--check',p]).status!==0)throw Error('NATIVE_SYNTAX_INVALID');}
const main=fs.readFileSync('/app/apps/backend/dist/apps/backend/src/main.js','utf8');
if(!main.includes('/editorial/route-admin')||!main.includes('15mb'))throw Error('SCOPED_BODY_PARSER_MISSING');
const controller=fs.readFileSync('/app/apps/backend/dist/apps/backend/src/api/routes/editorial.controller.js','utf8');
for(const value of ['route-admin','route-setup','EDITORIAL_COOKIE_SESSION_REQUIRED','EDITORIAL_OWNER_REQUIRED'])if(!controller.includes(value))throw Error('EDITORIAL_CONTROLLER_MISSING');
const next='/app/apps/frontend/.next';if(!fs.readFileSync(next+'/BUILD_ID','utf8').trim())throw Error('NEXT_BUILD_ID_MISSING');
const paths=JSON.parse(fs.readFileSync(next+'/server/app-paths-manifest.json'));
const key='/(app)/(site)/editorial/page';if(!Object.hasOwn(paths,key))throw Error('EDITORIAL_ROUTE_MISSING');
const expected='app/(app)/(site)/editorial/page.js';if(paths[key]!==expected||!fs.statSync(next+'/server/'+expected).isFile())throw Error('EDITORIAL_ROUTE_WRONG');
const refs=fs.readFileSync(next+'/server/app/(app)/(site)/editorial/page_client-reference-manifest.js','utf8');
if(!refs.includes('editorial-panel'))throw Error('EDITORIAL_CLIENT_NOT_COMPILED');
const css=[...refs.matchAll(/static\/css\/[A-Za-z0-9_.-]+\.css/g)].map(x=>x[0]);
if(!css.length||css.some(file=>!fs.statSync(next+'/'+file).isFile()))throw Error('EDITORIAL_CSS_MISSING');
const startup=require('/adapter/production-startup.cjs').validateStartup('/app');
console.log(JSON.stringify({passed:true,compiled_files:required.length,full_next_route:true,client_reference:true,css_files:new Set(css).size,
 scoped_body_parser:true,entrypoints_syntax_checked:true,default_cmd_executed:false,startup}));"""
        inspected = self.engine.docker("run", "--rm", "--label", LABEL_KEY + "=" + self.engine.label_value,
            "--cgroup-parent", self.engine.cgroup_parent, "--network", "none", "--read-only", "--memory", "512m",
            "--entrypoint", "node", self.image, "-e", script, timeout=60)
        proof = json.loads(inspected.stdout)
        if proof.get("passed") is not True:
            raise ReadinessError("NATIVE_ARTIFACTS_NOT_VERIFIED")
        if proof.get("startup", {}).get("startup_contract") != STARTUP_CONTRACT:
            raise ReadinessError("IMAGE_STARTUP_CONTRACT_INVALID")
        self.report["startup_contract"] = STARTUP_CONTRACT
        self.report["checks"]["compiled_artifacts"] = proof

    def volume(self, role):
        name = "mp-native-" + self.engine.label_value[:12] + "-" + role
        if self.engine.docker("volume", "ls", "-q", "--filter", "name=^" + name + "$").stdout.strip():
            raise ReadinessError("FRESH_VOLUME_REQUIRED")
        self.engine.docker("volume", "create", "--label", LABEL_KEY + "=" + self.engine.label_value, name)
        return name

    def container(self, role, image, *, env=None, volumes=(), command=(), entrypoint=None,
                  memory="512m", swap="1536m", extra=(), read_only=True, join=True):
        name = "mp-native-" + self.engine.label_value[:12] + "-" + role
        if self.engine.docker("ps", "-aq", "--filter", "name=^/" + name + "$").stdout.strip():
            raise ReadinessError("FRESH_CONTAINER_REQUIRED")
        args = ["run", "--detach", "--name", name, "--label", LABEL_KEY + "=" + self.engine.label_value,
                "--label", "editorial.readiness.role=" + role, "--cgroup-parent", self.engine.cgroup_parent,
                "--network", "container:" + self.anchor if join else "none", "--security-opt", "no-new-privileges:true",
                "--memory", memory, "--memory-swap", swap, "--cpus", "2", "--pids-limit", "1024",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", *extra]  # noqa: S108 - a private container tmpfs, not host storage.
        if read_only:
            args.append("--read-only")
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        for key, value in (env or {}).items():
            args += ["--env", key + "=" + str(value)]
        for value in volumes:
            args += ["--volume", value]
        value = self.engine.docker(*args, image, *command).stdout.strip()
        inspected = json.loads(self.engine.docker("inspect", value).stdout)[0]
        if (inspected["Config"]["Labels"].get(LABEL_KEY) != self.engine.label_value
                or inspected["HostConfig"]["CgroupParent"] != self.engine.cgroup_parent
                or inspected["HostConfig"]["PortBindings"]):
            raise ReadinessError("OWN_CONTAINER_SCOPE_INVALID")
        return value

    def namespace(self, command):
        return ["/usr/bin/nsenter", "--target", str(self.anchor_pid), "--net", *command]

    def namespace_proof(self):
        rows = json.loads(subprocess.check_output(self.namespace(["ip", "-j", "address"])))
        routes = json.loads(subprocess.check_output(self.namespace(["ip", "-j", "route"])))
        if ([row["ifname"] for row in rows] != ["lo"] or routes
                or not any(row["local"] == "10.20.30.45" and row["prefixlen"] == 32 for row in rows[0]["addr_info"])):
            raise ReadinessError("RUNTIME_NETWORK_FENCE_INVALID")
        return {"interfaces": ["lo"], "default_routes": 0,
                "namespace_inode": os.stat("/proc/" + str(self.anchor_pid) + "/ns/net").st_ino,
                "origin_alias_is_loopback_only": True, "working_server_contacted": False}

    def wait_exec(self, ident, command, *, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = self.engine.docker("exec", ident, *command, check=False, timeout=10)
            if result.returncode == 0:
                return result.stdout
            state = json.loads(self.engine.docker("inspect", ident).stdout)[0]["State"]
            if not state["Running"]:
                self.dependency_diagnostic(ident)
                raise ReadinessError("DEPENDENCY_EXITED_BEFORE_READY")
            time.sleep(1)
        self.dependency_diagnostic(ident)
        raise ReadinessError("DEPENDENCY_READINESS_TIMEOUT")

    def dependency_diagnostic(self, ident):
        value = json.loads(self.engine.docker("inspect", ident).stdout)[0]
        labels = value.get("Config", {}).get("Labels", {})
        role = labels.get("editorial.readiness.role", "")
        if (labels.get(LABEL_KEY) != self.engine.label_value
                or value.get("HostConfig", {}).get("CgroupParent") != self.engine.cgroup_parent
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,60}", role)):
            raise ReadinessError("DEPENDENCY_DIAGNOSTIC_OWNER_MISMATCH")
        state = value["State"]
        summary = {"role": role, "status": state.get("Status"), "running": state.get("Running"),
                   "exit_code": state.get("ExitCode"), "oom_killed": state.get("OOMKilled")}
        if (summary["status"] not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
                or type(summary["running"]) is not bool or type(summary["exit_code"]) is not int
                or type(summary["oom_killed"]) is not bool):
            raise ReadinessError("DEPENDENCY_DIAGNOSTIC_STATE_INVALID")
        command = [str(self.engine.docker_binary), "--host", self.engine.socket, "--config", str(self.engine.config_dir),
                   "logs", "--tail", "128", ident]
        raw, bounded = bounded_diagnostic_output(command, self.engine.environment)
        logfile = self.private / ("dependency-" + role + ".log")
        with logfile.open("xb") as output:
            output.write(raw)
        summary.update(log_sha256=digest(raw), **bounded)
        self.report["failed_dependency"] = summary
        private_write(self.private / ("dependency-" + role + ".json"), summary)

    def network_anchor(self):
        if hasattr(self, "anchor"):
            self.namespace_proof()
            return
        self.anchor = self.container("network", BASE, entrypoint="/bin/sleep", command=["infinity"],
                                     join=False, memory="32m", swap="96m")
        self.anchor_pid = json.loads(self.engine.docker("inspect", self.anchor).stdout)[0]["State"]["Pid"]
        subprocess.run(self.namespace(["ip", "address", "add", "10.20.30.45/32", "dev", "lo"]), check=True, timeout=10)
        self.report["checks"]["runtime_namespace"] = self.namespace_proof()

    def proxy_error_diagnostic(self, ident):
        value = json.loads(self.engine.docker("inspect", ident).stdout)[0]
        labels = value.get("Config", {}).get("Labels", {})
        role = labels.get("editorial.readiness.role")
        if (labels.get(LABEL_KEY) != self.engine.label_value or role not in {"proxy", "proxy-preflight"}
                or value.get("HostConfig", {}).get("CgroupParent") != self.engine.cgroup_parent):
            raise ReadinessError("PROXY_DIAGNOSTIC_OWNER_MISMATCH")
        script = "const fs=require('fs');const p='/tmp/nginx-error.log';const f=fs.openSync(p,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);try{const s=fs.fstatSync(f);if(!s.isFile()||s.nlink!==1)throw Error('INVALID');const b=Buffer.alloc(65537);const n=fs.readSync(f,b,0,b.length,0);process.stdout.write(b.subarray(0,n));}finally{fs.closeSync(f)}"
        command = [str(self.engine.docker_binary), "--host", self.engine.socket, "--config", str(self.engine.config_dir),
                   "exec", ident, "node", "-e", script]
        raw, bounded = bounded_diagnostic_output(command, self.engine.environment)
        if raw and not raw.endswith(b"\n"):
            raw = raw[:raw.rfind(b"\n") + 1]
        private_write(self.private / (role + "-nginx-error-metadata.json"), {"sha256": digest(raw), **bounded})
        (self.private / (role + "-nginx-error.raw")).write_bytes(raw)
        values = set(getattr(self, "runtime_passwords", {}).values())
        if hasattr(self, "state_path") and self.state_path.exists():
            values.update(synthetic_secrets(json.loads(self.state_path.read_text())))
        (self.private / (role + "-nginx-error.log")).write_bytes(redact_proxy_error(raw, values))
        self.report["checks"][role + "_error_capture"] = {"fixed_path": "/tmp/nginx-error.log",  # noqa: S108 - fixed file inside an identity-verified private container.
            "owner_verified": True, "raw_sha256": digest(raw), "captured_bytes": len(raw), **bounded}

    def proxy_preflight(self):
        self.phase("early_proxy_preflight")
        self.network_anchor()
        self.proxy_dir = self.engine.root / "proxy"
        self.proxy_dir.mkdir()
        outer = (self.project / "deployment/linux/editorial-postiz.nginx.conf").read_text()
        raw = proxy_configuration(outer)
        (self.proxy_dir / "nginx.conf").write_text(raw)
        self.proxy_sha256 = digest(raw.encode())
        proxy = self.container("proxy-preflight", BASE, entrypoint="/usr/sbin/nginx", memory="96m", swap="256m",
            volumes=[str(self.proxy_dir) + ":/readiness:ro"], command=["-c", "/readiness/nginx.conf", "-g", "daemon off;"])
        probe = "Promise.all(['http://127.0.0.1:14007/','http://10.20.30.45:4007/'].map(url=>new Promise(resolve=>require('http').get(url,r=>resolve(r.statusCode===502&&String(r.headers.server).includes('nginx'))).on('error',()=>resolve(false))))).then(values=>process.exit(values.every(Boolean)?0:1))"
        self.wait_exec(proxy, ["node", "-e", probe], seconds=30)
        stub_script = "require('http').createServer((q,r)=>{if(q.url==='/editorial/browser-authorize'){r.writeHead(q.headers.cookie==='ci-transport=1'?204:403);r.end();return;}if(q.method!=='POST'||q.url!=='/editorial/route-admin'){r.writeHead(404);r.end();return;}let n=0;q.on('data',b=>n+=b.length);q.on('end',()=>{r.writeHead(201,{'content-type':'application/json'});r.end(JSON.stringify({bytes:n,transport_fixture:true}));});}).listen(3000,'127.0.0.1')"
        stub = self.container("proxy-transport-stub", BASE, entrypoint="node", memory="128m", swap="256m", command=["-e", stub_script])
        client = None
        try:
            self.wait_exec(stub, ["node", "-e", "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode===404?0:1)).on('error',()=>process.exit(1))"], seconds=30)
            client_script = "const h=require('http');const b=Buffer.alloc(14*1024*1024,32);b[0]=34;b[b.length-1]=34;const q=h.request('http://10.20.30.45:4007/api/editorial/route-admin',{method:'POST',headers:{Cookie:'ci-transport=1',Origin:'" + ORIGIN + "','Content-Type':'application/json','Content-Length':b.length}},r=>{let out='';r.on('data',v=>{out+=v;if(out.length>4096)q.destroy();});r.on('end',()=>{let v;try{v=JSON.parse(out)}catch{}const ok=r.statusCode===201&&v?.bytes===b.length&&v?.transport_fixture===true;console.log(JSON.stringify({passed:ok,status:r.statusCode,bytes:v?.bytes,real_owner_authentication:false}));process.exit(ok?0:1);});});q.setTimeout(15000,()=>q.destroy());q.on('error',()=>process.exit(1));q.end(b);"
            client = self.container("proxy-transport-client", BASE, entrypoint="node", memory="128m", swap="256m", command=["-e", client_script])
            code = self.engine.docker("wait", client, timeout=30).stdout.strip()
            outcome = self.engine.docker("logs", client).stdout
            (self.private / "proxy-transport.log").write_text(outcome)
            if code != "0" or json.loads(outcome).get("passed") is not True:
                raise ReadinessError("PROXY_LARGE_BODY_TRANSPORT_FAILED")
        except BaseException:
            try:
                self.proxy_error_diagnostic(proxy)
            except Exception:
                self.report["proxy_error_capture_failed"] = True
            raise
        finally:
            for ident in (client, stub, proxy):
                if ident is not None:
                    self.engine.docker("stop", "--time", "10", ident)
                    if json.loads(self.engine.docker("inspect", ident).stdout)[0]["State"]["Running"]:
                        raise ReadinessError("PROXY_PREFLIGHT_NOT_STOPPED")
        release = "import socket; handles=[]\nfor host,port in [('127.0.0.1',3000),('127.0.0.1',14007),('10.20.30.45',4007)]:\n s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((host,port));handles.append(s)\nfor s in handles:s.close()"
        self.run(self.namespace([sys.executable, "-c", release]), name="proxy-ports-released", timeout=10)
        self.report["checks"]["early_proxy_preflight"] = {"passed": True, "read_only": True,
            "read_only_profile": "five-private-tmpfs-paths-v1",
            "pinned_base_used": True, "both_listeners_reached": True, "ports_released": True,
            "large_body_transport_bytes": 14 * 1024 * 1024, "real_owner_authentication": False,
            "database_started": False, "default_cmd_executed": False, "configuration_sha256": self.proxy_sha256}

    def runtime_dependencies(self):
        self.phase("real_database_runtime")
        self.network_anchor()
        self.runtime_passwords = {key: secrets.token_hex(32) for key in ("postiz", "hub", "temporal", "redis", "jwt", "internal")}
        self.pg = {}
        for role, image, port, user, database in (
                ("postiz", PINS["postgres"], 5432, "postiz_user", "postiz_readiness"),
                ("temporal", PINS["temporal-postgres"], 5433, "temporal", "temporal"),
                ("hub", PINS["postgres"], 5434, "hub_admin", "editorial_hub")):
            ident = self.container(role + "-pg", image, env={"POSTGRES_USER": user, "POSTGRES_DB": database,
                "POSTGRES_PASSWORD": self.runtime_passwords[role], "PGDATA": "/var/lib/postgresql/data/pgdata"},
                volumes=[self.volume(role + "-pg") + ":/var/lib/postgresql/data"], memory="192m", swap="768m",
                extra=["--user", "postgres", "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,mode=1777"],
                command=["postgres", "-p", str(port), "-c", "listen_addresses=127.0.0.1", "-c", "shared_buffers=32MB", "-c", "max_connections=50", "-c", "log_statement=ddl"])
            self.pg[role] = (ident, port, user, database)
            self.wait_exec(ident, ["pg_isready", "-h", "127.0.0.1", "-p", str(port), "-U", user, "-d", database])
            count = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", database,
                "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").stdout.strip()
            if count != "0":
                raise ReadinessError("INITIAL_EMPTY_DATABASE_REQUIRED")
        self.redis = self.container("redis", PINS["redis"], extra=["--user", "redis"],
            volumes=[self.volume("redis") + ":/data"], memory="96m", swap="256m",
            command=["redis-server", "--bind", "127.0.0.1", "--appendonly", "yes", "--maxmemory", "32mb",
                     "--maxmemory-policy", "noeviction", "--requirepass", self.runtime_passwords["redis"]])
        if self.wait_exec(self.redis, ["redis-cli", "-a", self.runtime_passwords["redis"], "PING"]).strip() != "PONG":
            raise ReadinessError("REDIS_AUTHENTICATED_HEALTH_FAILED")
        self.es = self.container("elasticsearch", PINS["elasticsearch"], read_only=False,
            volumes=[self.volume("elasticsearch") + ":/usr/share/elasticsearch/data"],
            env={"discovery.type": "single-node", "xpack.security.enabled": "false", "ES_JAVA_OPTS": "-Xms128m -Xmx256m",
                 "network.host": "127.0.0.1"}, extra=["--ulimit", "nofile=65536:65536"])
        self.wait_exec(self.es, ["curl", "--fail", "--silent", "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=3s"])
        self.temporal = self.container("temporal", PINS["temporal"], read_only=False, env={
            "DB": "postgres12", "DB_PORT": "5433", "POSTGRES_USER": "temporal", "POSTGRES_PWD": self.runtime_passwords["temporal"],
            "POSTGRES_SEEDS": "127.0.0.1", "BIND_ON_IP": "127.0.0.1", "TEMPORAL_BROADCAST_ADDRESS": "127.0.0.1",
            "ENABLE_ES": "true", "ES_SEEDS": "127.0.0.1", "ES_VERSION": "v7", "TEMPORAL_NAMESPACE": "default"})
        self.wait_exec(self.temporal, ["temporal", "operator", "cluster", "health", "--address", "127.0.0.1:7233", "--command-timeout", "4s"], seconds=240)
        self.report["checks"]["real_databases_redis_temporal"] = {"postgres_instances": 3, "initially_empty": True,
            "temporal_ready": True, "elasticsearch_ready": True, "network": "loopback_namespace_only"}

    def chrome_prerequisite(self):
        self.phase("sandboxed_chrome_prerequisite")
        self.namespace_proof()
        owner = self.workspace.stat()
        if owner.st_uid <= 0:
            raise ReadinessError("CHROME_PREREQUISITE_NONROOT_OWNER_REQUIRED")
        home = self.created_opt / "chrome-prerequisite-home"
        home.mkdir(mode=0o700)
        os.chown(home, owner.st_uid, owner.st_gid)
        script = self.created_opt / "chrome-prerequisite.py"
        script.write_text(r'''
import json,os,pathlib,socket,sys
from playwright.sync_api import sync_playwright
result={'passed':False,'sandbox_enabled':True,'owner_authentication':False}
try:
 if [name for _,name in socket.if_nameindex()]!=['lo']:raise RuntimeError('network_scope')
 with sync_playwright() as p:
  browser=p.chromium.launch(channel='chrome',headless=True,chromium_sandbox=True,timeout=30000)
  try:
   page=browser.new_page();page.goto('about:blank');result['passed']=True
  finally:browser.close()
except Exception as error:
 text=str(error).lower()
 path=pathlib.Path(os.environ['HOME'])/'failure.private.txt'
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as output:output.write(str(error)[:65536])
 result.update(sandbox_denied=any(v in text for v in ('no usable sandbox','operation not permitted','failed to move to new namespace')),
  executable_missing=any(v in text for v in ('executable doesn\'t exist','executable does not exist')),
  shared_library_missing='error while loading shared libraries' in text)
print(json.dumps(result));sys.exit(0 if result['passed'] else 1)
''')
        unit = "mp-native-chrome-prerequisite-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(unit)
        command = ["systemd-run", "--wait", "--pipe", "--collect", "--unit=" + unit,
            "--uid=" + str(owner.st_uid), "--service-type=exec", "--slice=" + self.engine.slice,
            "--property=NoNewPrivileges=yes", "--property=Delegate=yes", "--property=KillMode=control-group",
            "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
            "--property=RuntimeMaxSec=45", "--property=TimeoutStopSec=10", "--property=MemoryMax=" + TEST_CLIENT_MEMORY,
            "--property=MemorySwapMax=2G", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "HOME=" + str(home),
            "TMPDIR=" + str(home), "PYTHONUTF8=1", "LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib"),
            "PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers), str(self.python), str(script)]
        self.run(command, name="chrome-prerequisite", timeout=65)
        self.namespace_proof()
        self.report["checks"]["sandboxed_chrome_prerequisite"] = {"passed": True,
            "sandbox_enabled": True, "nonroot": True, "owner_authentication": False, "url": "about:blank"}

    def user_environment(self):
        return ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
                "HOME=" + str(self.project / "data/ci-home"), "PYTHONUTF8=1",
                "LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib"),
                "GITHUB_ACTIONS=true", "RUNNER_ENVIRONMENT=github-hosted",
                "PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers)]

    def nonroot(self):
        return ["/usr/sbin/runuser", "--user", "editorial", "--", *self.user_environment()]

    def user_run(self, command, *, name, timeout):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,60}", name) or type(timeout) is not int or not 1 <= timeout <= 1800:
            raise ReadinessError("SCENARIO_OWNED_UNIT_INVALID")
        unit = "mp-native-" + name + "-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(unit)
        argv = ["systemd-run", "--wait", "--pipe", "--collect", "--unit=" + unit, "--uid=editorial",
            "--service-type=exec", "--slice=" + self.engine.slice, "--property=Delegate=yes",
            "--property=NoNewPrivileges=yes", "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=" + str(timeout), "--property=TimeoutStopSec=15",
            "--property=MemoryMax=" + TEST_CLIENT_MEMORY, "--property=MemorySwapMax=2G", "--property=TasksMax=1024",
            "--property=WorkingDirectory=" + str(self.project),
            "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
            *self.user_environment(), *command]
        return self.run(argv, name=name, timeout=timeout + 25)

    def scenario(self, mode):
        self.namespace_proof()
        try:
            result = self.user_run([str(self.python),
                str(self.project / "deployment/linux/readiness_scenario.py"), "--root", str(self.project),
                "--state", str(self.state_path), "--evidence", str(self.scenario_public), "--mode", mode],
                name="scenario-" + mode, timeout=600)
        except BaseException:
            if hasattr(self, "proxy"):
                try:
                    self.proxy_error_diagnostic(self.proxy)
                except Exception:
                    self.report["proxy_error_capture_failed"] = True
            raise
        self.namespace_proof()
        return result

    def create_user_and_state(self):
        import pwd

        try:
            pwd.getpwnam("editorial")
        except KeyError:
            pass
        else:
            raise ReadinessError("FRESH_EDITORIAL_USER_REQUIRED")
        subprocess.run(["/usr/sbin/useradd", "--system", "--user-group", "--home-dir",
                        str(self.project / "data/ci-home"), "--shell", "/usr/sbin/nologin", "editorial"], check=True, timeout=30)
        self.user = pwd.getpwnam("editorial")
        # Atomic credential replacement creates a sibling .env.local temporary.
        # Production helpers stay non-root, including when the app owns this dir.
        os.chown(self.project, self.user.pw_uid, self.user.pw_gid)
        sentinel = "mp-native-sentinel-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(sentinel)
        subprocess.run(["systemd-run", "--unit=" + sentinel, "--uid=editorial", "--slice=" + self.engine.slice,
                        "--service-type=exec", "--collect", "--property=NoNewPrivileges=yes",
                        "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
                        "--property=MemoryMax=16M", "/bin/sleep", "infinity"], check=True, timeout=30,
                       capture_output=True)
        sentinel_pid = int(subprocess.check_output(["systemctl", "show", sentinel, "-p", "MainPID", "--value"], text=True))
        for name in ("data", "data/ci-private", "data/ci-home"):
            path = self.project / name
            path.mkdir(mode=0o700, exist_ok=True)
            os.chown(path, self.user.pw_uid, self.user.pw_gid)
        self.scenario_public = self.project / "data/ci-public"
        self.scenario_public.mkdir(mode=0o700)
        os.chown(self.scenario_public, self.user.pw_uid, self.user.pw_gid)
        self.state_path = self.project / "data/ci-private/state.json"
        state = {"contract": "three-platform-native-readiness-v1",
            "scope": "ephemeral_github_hosted_no_external_publications", "new_empty_runtime": True,
            "runtime_id": self.engine.label_value, "engine_id": self.engine.engine_id, "root": str(self.project),
            "namespace_owner_pid": sentinel_pid, "namespace_inode": self.namespace_proof()["namespace_inode"],
            "public_origin": ORIGIN, "private_postiz_base": "http://127.0.0.1:14007/api/public/v1",
            "postiz_dsn": "postgresql://postiz_user:" + self.runtime_passwords["postiz"] + "@127.0.0.1:5432/postiz_readiness",
            "hub_dsn": "postgresql+psycopg://hub_admin:" + self.runtime_passwords["hub"] + "@127.0.0.1:5434/editorial_hub",
            "owner_email": "native-owner@example.invalid", "owner_password": secrets.token_urlsafe(24),
            "jwt_secret": self.runtime_passwords["jwt"], "internal_key": self.runtime_passwords["internal"],
            "profile_id": "test", "vk_owner": "123456789", "service_unit": "max-postiz-editorial.service",
            "login_service_unit": "max-postiz-login.service"}
        private_write(self.state_path, state)
        os.chown(self.state_path, self.user.pw_uid, self.user.pw_gid)

    def bootstrap_project(self):
        self.phase("fresh_editorial_bootstrap")
        self.run([str(self.python), "-m", "max_postiz_agent.hub_cli", "--root", str(self.project), "init"],
                 name="initial-config", timeout=60)
        app_path = self.project / "config/app.json"
        app = json.loads(app_path.read_text())
        app["require_postgresql"] = True
        app["opencode"]["adapter_mode"] = "fake"
        app["max"]["adapter_mode"] = "browser"
        app["max"]["browser"] = {"profile_dir": "data/max-browser-ci", "channel": "chrome", "headless": True,
                                   "operation_timeout_seconds": 30, "channels": {"-123": "CI MAX"}}
        app["video"]["backend"] = "disabled"
        app_path.write_text(json.dumps(app, ensure_ascii=False, indent=2))
        sources = self.project / "config/sources.json"
        value = json.loads(sources.read_text())
        for source in value["sources"]:
            source["enabled"] = False
        sources.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        publishing = {"schema_version": "1.0", "postiz": {"base_url_env": "POSTIZ_BASE_URL", "api_key_env": "POSTIZ_API_KEY",
                      "approval_guard_verified": False, "max_video_verified": False}, "targets": [], "tasks": []}
        (self.project / "config/publishing.json").write_text(json.dumps(publishing))
        # Runtime configuration of the copied project only; the Git checkout remains unchanged.
        topic_path = self.project / "TOPIC.md"
        topic = topic_path.read_text()
        match = re.search(r"(<!-- TOPIC_CONFIG_BEGIN -->\s*```json\s*)(.*?)(\s*```\s*<!-- TOPIC_CONFIG_END -->)", topic, re.S)
        if not match:
            raise ReadinessError("TOPIC_CONFIG_MARKER_MISSING")
        payload = json.loads(match[2])
        for channel in payload["channels"]:
            channel["enabled"] = channel["id"] == "test"
            if channel["id"] == "test":
                channel["publication"]["mode"] = "review"
        topic_path.write_text(topic[:match.start(2)] + json.dumps(payload, ensure_ascii=False, indent=2) + topic[match.end(2):])
        for path in (self.project / "config").rglob("*"):
            if path.is_file() and path.name.endswith(".json"):
                os.chown(path, self.user.pw_uid, self.user.pw_gid)
                path.chmod(0o600)
        os.chown(topic_path, self.user.pw_uid, self.user.pw_gid)
        os.chown(self.project / "config", self.user.pw_uid, self.user.pw_gid)
        self.report["checks"]["disabled_hourly_fixture"] = initialize_disabled_policy(
            self.project, self.user.pw_uid, self.user.pw_gid)

    def app_environment(self, state, *, database=None):
        result = {"MAIN_URL": ORIGIN, "FRONTEND_URL": ORIGIN, "NEXT_PUBLIC_BACKEND_URL": ORIGIN + "/api",
            "DATABASE_URL": database or state["postiz_dsn"],
            "REDIS_URL": "redis://:" + self.runtime_passwords["redis"] + "@127.0.0.1:6379",
            "JWT_SECRET": state["jwt_secret"], "TEMPORAL_ADDRESS": "127.0.0.1:7233", "TEMPORAL_NAMESPACE": "default",
            "BACKEND_INTERNAL_URL": "http://127.0.0.1:3000", "NOT_SECURED": "true", "IS_GENERAL": "true",
            "DISABLE_REGISTRATION": "true", "RUN_CRON": "", "EXCLUDE_QUEUE": QUEUES,
            "STORAGE_PROVIDER": "local", "UPLOAD_DIRECTORY": "/uploads", "NEXT_PUBLIC_UPLOAD_DIRECTORY": "/uploads",
            "NODE_OPTIONS": "--max-old-space-size=1024", "NEXT_TELEMETRY_DISABLED": "1",
            "EDITORIAL_INTERNAL_URL": "http://127.0.0.1:8092/internal/editorial", "EDITORIAL_INTERNAL_PORT": "8092",
            "EDITORIAL_INTERNAL_KEY": state["internal_key"],
            "EDITORIAL_OWNER_USER_ID": state.get("owner_id", "not-configured"),
            "EDITORIAL_OWNER_ORGANIZATION_ID": state.get("org_id", "not-configured")}
        return result

    def start_postiz(self):
        self.phase("postiz_owner_authentication")
        state = json.loads(self.state_path.read_text())
        environment = self.app_environment(state)
        # Explicit schema operation against the already verified new empty database only.
        schema = self.container("prisma-init", self.image, env=environment, entrypoint="pnpm", memory="1g", swap="2g",
            command=["exec", "prisma", "db", "push", "--skip-generate", "--schema",
                     "./libraries/nestjs-libraries/src/database/prisma/schema.prisma"], read_only=False)
        code = self.engine.docker("wait", schema, timeout=180).stdout.strip()
        if code != "0":
            raise ReadinessError("FRESH_POSTIZ_SCHEMA_FAILED")
        self.upload_volume = self.volume("uploads")
        self.backend = self.container("backend-bootstrap", self.image, env=environment, entrypoint="node", memory="1536m", swap="3g",
            volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/backend/dist/apps/backend/src/main.js"])
        self.frontend = self.container("frontend", self.image, env=environment, entrypoint="node", memory="1536m", swap="3g",
            extra=["--tmpfs", "/app/apps/frontend/.next/cache:rw,nosuid,nodev,size=128m"],
            volumes=[self.upload_volume + ":/uploads"],
            command=["/app/node_modules/next/dist/bin/next", "start", "/app/apps/frontend", "-p", "4200", "--hostname", "127.0.0.1"])
        if digest((self.proxy_dir / "nginx.conf").read_bytes()) != self.proxy_sha256:
            raise ReadinessError("PROXY_CONFIGURATION_CHANGED_AFTER_PREFLIGHT")
        self.proxy = self.container("proxy", self.image, entrypoint="/usr/sbin/nginx", memory="96m", swap="256m",
            volumes=[str(self.proxy_dir) + ":/readiness:ro"], command=["-c", "/readiness/nginx.conf", "-g", "daemon off;"])
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:14007/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"], seconds=180)
        self.scenario("bootstrap-owner")
        state = json.loads(self.state_path.read_text())
        if not all(isinstance(state.get(key), str) and state[key] for key in ("owner_id", "org_id", "postiz_api_key")):
            raise ReadinessError("SYNTHETIC_POSTIZ_OWNER_REQUIRED")
        self.engine.docker("stop", "--time", "20", self.backend)
        self.backend = self.container("backend", self.image, env=self.app_environment(state), entrypoint="node",
            memory="1536m", swap="3g", volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/backend/dist/apps/backend/src/main.js"])
        self.orchestrator = self.container("orchestrator", self.image, env=self.app_environment(state), entrypoint="node",
            memory="768m", swap="2g", volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/orchestrator/dist/apps/orchestrator/src/main.js"])
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:14007/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"])

    def editorial_environment(self, state, *, hub_dsn=None):
        path = self.project / ".env.local"
        values = {}
        for line in path.read_text().splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        values.update(DATABASE_URL=hub_dsn or state["hub_dsn"], POSTIZ_BASE_URL=state["private_postiz_base"],
            POSTIZ_API_KEY=state["postiz_api_key"], POSTIZ_PUBLIC_ORIGIN=ORIGIN,
            EDITORIAL_INTERNAL_KEY=state["internal_key"], EDITORIAL_OWNER_USER_ID=state["owner_id"],
            EDITORIAL_OWNER_ORGANIZATION_ID=state["org_id"], EDITORIAL_INTERNAL_PORT="8092")
        path.write_text("\n".join(key + "=" + value for key, value in values.items()) + "\n")
        os.chown(path, self.user.pw_uid, self.user.pw_gid)
        path.chmod(0o600)

    def install_editorial(self):
        self.phase("production_editorial_entrypoint")
        state = json.loads(self.state_path.read_text())
        self.editorial_environment(state)
        self.user_run([str(self.python), "-m", "max_postiz_agent.server_cli", "--root",
            str(self.project), "migrate"], name="explicit-agent-migrate", timeout=120)
        unit = "max-postiz-editorial.service"
        destination = Path("/etc/systemd/system") / unit
        if destination.exists() or destination.is_symlink():
            raise ReadinessError("OWN_SERVICE_NAME_ALREADY_EXISTS")
        source = (self.project / "deployment/linux" / unit).read_text().replace("/opt/max-postiz", str(self.project))
        destination.write_text(source)
        self.unit_names.append(unit)
        dropin = destination.with_name(unit + ".d")
        dropin.mkdir()
        (dropin / "readiness.conf").write_text("[Unit]\nWants=\n[Service]\nNetworkNamespacePath=/proc/" + str(self.anchor_pid) +
            "/ns/net\nSlice=" + self.engine.slice + "\nEnvironment=HOME=" + str(self.project / "data/ci-home") +
            "\nEnvironment=PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers) +
            "\nEnvironment=LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib") + "\nMemoryMax=1G\nMemorySwapMax=2G\n")
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        self.run(["bash", str(self.project / "deployment/linux/install-owner-portals.sh"), "--check", "--root", str(self.project)],
                 name="portal-installer-check", timeout=30)
        self.run(["bash", str(self.project / "deployment/linux/install-owner-portals.sh"), "--install", "--root", str(self.project)],
                 name="portal-installer-install", timeout=60)
        self.unit_names += ["max-postiz-login.path", "max-postiz-editorial-restart.path", "max-postiz-login.service",
                            "max-postiz-editorial-restart.service", "max-postiz-novnc.service", "max-postiz-vnc.service", "max-postiz-display.service"]
        for name in self.unit_names[1:]:
            if not name.endswith(".service") or name == unit:
                continue
            folder = Path("/etc/systemd/system") / (name + ".d")
            folder.mkdir(exist_ok=True)
            helper = (helper_execstart_override(name, self.project) if name in {
                "max-postiz-login.service", "max-postiz-editorial-restart.service"} else "")
            (folder / "readiness.conf").write_text("[Service]\nNetworkNamespacePath=/proc/" + str(self.anchor_pid) +
                "/ns/net\nSlice=" + self.engine.slice + "\nEnvironment=HOME=" + str(self.project / "data/ci-home") +
                "\nEnvironment=PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers) +
                "\nEnvironment=LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib") + "\n" + helper)
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "start", unit], check=True, timeout=60)
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:8092/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"])
        self.scenario("exercise")

    def supervisor_and_single_worker(self):
        self.phase("native_supervisor_and_single_worker")
        unit = "mp-native-supervisor-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(unit)
        xml = self.project / "data/ci-private/supervisor.xml"
        command = ["systemd-run", "--wait", "--pipe", "--collect", "--unit=" + unit, "--uid=editorial",
            "--slice=" + self.engine.slice, "--property=Delegate=yes", "--property=NoNewPrivileges=yes",
            "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
            "--property=WorkingDirectory=" + str(self.project), "--property=RuntimeMaxSec=180",
            "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "PYTHONUTF8=1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib"),
            str(self.python), "-m", "pytest", "--noconftest", "tests/test_browser_process.py",
            "--basetemp=" + str(self.project / "data/ci-private/supervisor-temp"), "--junitxml=" + str(xml)]
        self.run(command, name="native-supervisor", timeout=200)
        suites = ET.parse(xml).getroot()  # noqa: S314 - produced by this exact owned pytest process, not uploaded XML.
        results = [row for row in suites.iter("testcase")]
        if not results or any(row.find("failure") is not None or row.find("error") is not None or row.find("skipped") is not None for row in results):
            raise ReadinessError("NATIVE_SUPERVISOR_NOT_ALL_PASSED")
        second = "mp-native-second-worker-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(second)
        duplicate = self.run(["systemd-run", "--wait", "--pipe", "--collect", "--unit=" + second, "--uid=editorial",
            "--slice=" + self.engine.slice, "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
            "--property=WorkingDirectory=" + str(self.project), "--property=RuntimeMaxSec=30",
            str(self.python), "-m", "max_postiz_agent.server_cli", "--root", str(self.project),
            "serve", "--host", "127.0.0.1", "--port", "8093"], name="second-worker", timeout=40, check=False)
        if duplicate.returncode == 0 or "EDITORIAL_WORKER_ALREADY_RUNNING" not in (self.private / "second-worker.log").read_text():
            raise ReadinessError("SINGLE_WORKER_FENCE_NOT_CONFIRMED")
        self.report["checks"]["native_supervisor"] = {"passed": len(results), "skipped": 0, "single_worker_fence": True}

    def portal(self):
        self.phase("owner_portal_native_offline")
        lease = read_restart_lease(self.project, self.user.pw_uid)
        waited = wait_restart_expiry(json.loads(lease), self.project)
        if read_restart_lease(self.project, self.user.pw_uid) != lease:
            raise ReadinessError("NATIVE_RESTART_LEASE_CHANGED")
        self.report["checks"]["restart_lease_expired_before_portal"] = {**waited, "request_sha256": digest(lease)}
        # This is a private probe, not an alternate product login URL. The
        # production helper opens its real allowlisted URL in a network with no
        # external interface. Account authentication is deliberately not claimed.
        script = self.project / "data/ci-private/portal-probe.py"
        code = r'''
import base64,hashlib,json,os,pathlib,socket,subprocess,sys,time
sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'deployment/linux'))
from readiness_scenario import Scenario,ScenarioError,atomic,require
from native_readiness import wait_portal_rfb,portal_websocket,ReadinessError
s=Scenario(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]));s.login()
result=[];platform='max';stage='prepare_login'
try:
 for platform in ('max','vk'):
  stage='prepare_login'
  setup=s.setup();target=next(x for x in setup['targets'] if x['id']=={'max':'ci_max','vk':'ci_vk'}[platform])
  value=s.admin({'action':'prepare_login','id':target['id'],'expected_binding_hash':target['binding_hash']})
  require(value['login_url']=='/owner-browser/' and value['live_verified'] is False,'PORTAL_SELECTION_INVALID')
  stage='observe_chrome'
  deadline=time.monotonic()+60;seen=False;group=None;started=False
  while time.monotonic()<deadline:
   state=subprocess.check_output(['systemctl','show','max-postiz-login.service','-p','ControlGroup','-p','ActiveState'],text=True)
   values=dict(line.split('=',1) for line in state.splitlines() if '=' in line)
   if values.get('ControlGroup'):
    group=pathlib.Path('/sys/fs/cgroup'+values['ControlGroup'])
    for item in group.rglob('cgroup.procs'):
     try:
      for pid in item.read_text().split():
       try:seen=seen or pathlib.Path('/proc/'+pid+'/comm').read_text().startswith('chrome')
       except FileNotFoundError:pass
     except FileNotFoundError:pass
   # A previous platform's failed unit is not the new path activation.
   started=started or values.get('ActiveState') in {'active','activating'} or seen
   if started and values.get('ActiveState') in {'failed','inactive'}:break
   time.sleep(.02)
  require(seen,'PORTAL_NATIVE_CHROME_NOT_OBSERVED')
  stage='profile_check'
  selection=json.loads((s.root/'data/owner-browser-selection.json').read_text())
  require(selection['target_id']==target['id'],'PORTAL_WRONG_PROFILE')
  profile=s.root/selection['profile_dir'];require(profile.is_dir(),'PORTAL_PROFILE_NOT_CREATED')
  if group is not None and group.exists():
   require(all(not item.read_text().strip() for item in group.rglob('cgroup.procs')),'PORTAL_WORKER_NOT_CLEANED')
  stage='owner_vnc_http'
  response=s.client.get('/owner-browser/vnc.html',follow_redirects=False)
  require(response.status_code==200 and 'novnc' in response.text.lower(),'PORTAL_OWNER_VNC_PAGE_FAILED')
  import httpx
  with httpx.Client(trust_env=False,follow_redirects=False) as anon:
   require(anon.get(s.state['public_origin']+'/owner-browser/vnc.html').status_code in (401,403),'PORTAL_ANONYMOUS_ACCESS')
  cookie=s.client.cookies.get('auth');require(bool(cookie),'PORTAL_COOKIE_MISSING')
  stage='rfb_readiness';wait_portal_rfb()
  stage='owner_websocket';portal_websocket(cookie)
  result.append({'platform':platform,'owner_request':True,'native_chrome_observed':True,'dedicated_profile_created':True,
    'supervised_cleanup':True,'vnc_owner_guard':True,'websocket_rfb':True,'account_authenticated':False,'external_delivery_verified':False})
  if platform=='max':
   stage='selection_expiry'
   # The actual product intentionally retains its ten-minute selection lease.
   require(selection['expires_at']<=time.time()+601,'PORTAL_EXPIRY_INVALID')
   while time.time()<=selection['expires_at']:time.sleep(1)
 s.check();atomic(s.evidence/'portal.json',{'passed':True,'checks':result,'fixture_scope':'native_mechanism_with_external_navigation_unavailable','external_publications':0},exclusive=True)
except Exception as error:
 allowed={'PORTAL_WIRE_CAP_EXCEEDED','PORTAL_WIRE_TIMEOUT','PORTAL_WIRE_UNAVAILABLE','PORTAL_WIRE_EOF',
  'PORTAL_RFB_PROTOCOL_INVALID','PORTAL_RFB_NOT_READY','PORTAL_WEBSOCKET_STATUS_INVALID',
  'PORTAL_WEBSOCKET_HEADER_INVALID','PORTAL_WEBSOCKET_FRAME_INVALID','PORTAL_SELECTION_INVALID',
  'PORTAL_NATIVE_CHROME_NOT_OBSERVED','PORTAL_WRONG_PROFILE','PORTAL_PROFILE_NOT_CREATED',
  'PORTAL_WORKER_NOT_CLEANED','PORTAL_OWNER_VNC_PAGE_FAILED','PORTAL_ANONYMOUS_ACCESS','PORTAL_COOKIE_MISSING','PORTAL_EXPIRY_INVALID'}
 code=str(error) if isinstance(error,(ReadinessError,ScenarioError)) and str(error) in allowed else 'PORTAL_NATIVE_PROBE_FAILED'
 failure={'platform':platform,'stage':stage,'code':code}
 atomic(s.evidence/'portal.json',{'passed':False,'failure':failure,'checks':result,'external_publications':0},exclusive=True)
 print(json.dumps({'portal_failed':failure}));raise ReadinessError(code) from None
finally:s.client.close()
'''
        script.write_text(code)
        os.chown(script, self.user.pw_uid, self.user.pw_gid)
        self.user_run([str(self.python), str(script), str(self.project), str(self.state_path), str(self.scenario_public)],
                 name="native-owner-portal", timeout=780)
        self.report["checks"]["owner_portal"] = {"max_and_vk_native_mechanism": True, "vnc_owner_cookie_guard": True,
            "account_authenticated": False, "external_delivery_verified": False, "selection_expiry_seconds": 600}

    def clone_database(self, role, new_database):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", new_database):
            raise ReadinessError("RESTORE_DATABASE_NAME_INVALID")
        ident, port, user, database = self.pg[role]
        exists = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", database,
            "-tAc", "SELECT datname FROM pg_database").stdout.splitlines()
        if new_database in exists:
            raise ReadinessError("RESTORE_DESTINATION_ALREADY_EXISTS")
        self.engine.docker("exec", ident, "createdb", "-p", str(port), "-U", user, new_database)
        count = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", new_database,
            "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").stdout.strip()
        if count != "0":
            raise ReadinessError("RESTORE_DESTINATION_NOT_EMPTY")
        dump = self.engine.docker("exec", ident, "pg_dump", "-Fc", "-p", str(port), "-U", user,
                                 "--no-owner", "--no-privileges", database, text=False, timeout=120).stdout
        path = self.private / (role + "-" + new_database + ".dump")
        with path.open("xb") as stream:
            stream.write(dump)
        self.engine.docker("exec", "-i", ident, "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
            "-p", str(port), "-U", user, "-d", new_database, input=dump, text=False, timeout=120)
        return {"sha256": digest(dump), "bytes": len(dump), "empty_destination_verified": True}

    def backup_restore(self):
        self.phase("backup_restore_new_clones")
        self.scenario("snapshot")
        subprocess.run(["systemctl", "stop", "max-postiz-editorial.service"], check=True, timeout=45)
        files = {}
        for folder in (self.project / "config", self.project / "data"):
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink() and "ci-private" not in path.parts:
                    files[path.relative_to(self.project).as_posix()] = digest(path.read_bytes())
        files[".env.local"] = digest((self.project / ".env.local").read_bytes())
        private_write(self.private / "runtime-backup-files.json", files)
        self.engine.docker("stop", "--time", "20", self.backend)
        self.engine.docker("stop", "--time", "20", self.orchestrator)
        restored = {"postiz": "postiz_readiness_restore", "hub": "editorial_hub_restore"}
        backup = {role: self.clone_database(role, name) for role, name in restored.items()}
        state = json.loads(self.state_path.read_text())
        state["postiz_dsn"] = state["postiz_dsn"].rsplit("/", 1)[0] + "/postiz_readiness_restore"
        state["hub_dsn"] = state["hub_dsn"].rsplit("/", 1)[0] + "/editorial_hub_restore"
        private_write(self.state_path, state, exclusive=False)
        self.editorial_environment(state)
        self.backend = self.container("backend-restored", self.image, env=self.app_environment(state), entrypoint="node",
            memory="1536m", swap="3g", volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/backend/dist/apps/backend/src/main.js"])
        self.wait_postiz_backend()
        subprocess.run(["systemctl", "start", "max-postiz-editorial.service"], check=True, timeout=60)
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:8092/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"])
        self.scenario("verify-restored")
        # DSNs are deliberately changed in .env.local. Other original runtime
        # configuration, journals and history files remain byte-identical.
        if any(digest((self.project / name).read_bytes()) != sha for name, sha in files.items()
               if name != ".env.local"):
            raise ReadinessError("RESTORE_RUNTIME_FILES_CHANGED")
        for role, database in restored.items():
            ident, port, user, _ = self.pg[role]
            self.pg[role] = (ident, port, user, database)
        self.report["checks"]["backup_restore"] = {"databases": backup, "history_and_unknown_preserved": True,
            "runtime_files_preserved_in_place": True, "runtime_files_checked": len(files) - 1,
            "all_paused": True, "claims_not_reset": True}

    def wait_postiz_backend(self):
        # Read-only readiness precedes the one-shot authentication verification.
        return self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:14007/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"], seconds=180)

    def default_boot(self):
        self.phase("default_entrypoint_fresh_clone")
        # Test the exact release CMD, with cleared inherited ENTRYPOINT. Initial
        # schema creation was an explicit operation; normal boot performs no DDL.
        self.clone_database("postiz", "postiz_default_smoke")
        for ident in (self.backend, self.frontend):
            self.engine.docker("stop", "--time", "20", ident)
        state = json.loads(self.state_path.read_text())
        database = state["postiz_dsn"].rsplit("/", 1)[0] + "/postiz_default_smoke"
        logging = self.engine.docker("exec", self.pg["postiz"][0], "psql", "-U", "postiz_user",
            "-d", "postiz_default_smoke", "-tAc", "SHOW log_statement").stdout.strip()
        if logging != "ddl":
            raise ReadinessError("DEFAULT_STARTUP_DDL_OBSERVATION_UNAVAILABLE")
        second, nanosecond = divmod(time.time_ns(), 1_000_000_000)
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(second)) + f".{nanosecond:09d}Z"
        unified = self.container("default-smoke", self.image, env=self.app_environment(state, database=database),
            memory="3g", swap="5g", volumes=[self.upload_volume + ":/uploads"], read_only=False)
        self.wait_exec(unified, ["node", "-e", "require('http').get('http://127.0.0.1:5000/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"], seconds=240)
        # The public proxy is now exercised through the actual unified nginx5000.
        proxy_path = self.engine.root / "proxy/nginx.conf"
        source = proxy_path.read_text()
        source = re.sub(r"server\{listen 127\.0\.0\.1:14007;.*\}\}$",
            "server{listen 127.0.0.1:14007;client_max_body_size 15m;location / {proxy_pass http://127.0.0.1:5000;proxy_set_header Host $http_host;proxy_set_header X-Forwarded-Proto $scheme;}}}", source)
        if "proxy_pass http://127.0.0.1:5000" not in source:
            raise ReadinessError("DEFAULT_PROXY_SWITCH_INVALID")
        proxy_path.write_text(source)
        self.engine.docker("exec", self.proxy, "/usr/sbin/nginx", "-s", "reload", "-c", "/readiness/nginx.conf")
        script = self.project / "data/ci-private/default-smoke.py"
        script.write_text("import sys,pathlib,json;sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'deployment/linux'));from readiness_scenario import Scenario,atomic,require;s=Scenario(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]));s.login();actual=s.postiz_snapshot();require(actual==s.state['postiz_snapshot'],'DEFAULT_IDENTITY_CHANGED');require(s.hub_snapshot()==s.state['hub_snapshot'],'DEFAULT_HUB_HISTORY_CHANGED');atomic(s.evidence/'default-cmd.json',{'passed':True,'owner_login':True,'identity_and_history_preserved':True,'external_publications':0},exclusive=True);s.client.close()")
        os.chown(script, self.user.pw_uid, self.user.pw_gid)
        state["postiz_dsn"] = database
        private_write(self.state_path, state, exclusive=False)
        self.user_run([str(self.python), str(script), str(self.project), str(self.state_path), str(self.scenario_public)],
                 name="default-container-owner", timeout=90)
        logs = self.engine.docker("logs", "--since", since, self.pg["postiz"][0])
        pglog = logs.stdout + logs.stderr
        # No other component is allowed to perform schema operations during this
        # window. log_statement=ddl records even a redundant CREATE/ALTER call.
        ddl_count = count_logged_schema_statements(pglog)
        if ddl_count:
            raise ReadinessError("DEFAULT_STARTUP_IMPLICIT_SCHEMA_CHANGE")
        self.report["checks"]["default_cmd_fresh_clone"] = {"passed": True, "bounded": True,
            "startup_contract": STARTUP_CONTRACT, "implicit_schema_statements": 0,
            "database_scope": "new_restored_synthetic_postiz_clone_only", "owner_cookie_login": True,
            "main_and_provider_queues_excluded": True, "all_agent_targets_paused": True}
        self.report["default_cmd_executed"] = True

    def export_image(self):
        import gzip

        self.phase("image_export")
        state = json.loads(self.state_path.read_text())
        value = json.loads(self.engine.docker("image", "inspect", self.image).stdout)[0]
        environment = "\n".join(value["Config"].get("Env", []))
        if any(secret in environment for secret in synthetic_secrets(state)):
            raise ReadinessError("IMAGE_CONTAINS_RUNTIME_SECRET")
        folder = Path(os.environ["RUNNER_TEMP"]) / "native-readiness-image"
        folder.mkdir(mode=0o755)
        target = folder / "max-postiz-0.14.0.tar.gz"
        command = [str(self.engine.docker_binary), "--host", self.engine.socket, "--config", str(self.engine.config_dir), "image", "save", self.image]
        with target.open("xb") as output, (self.private / "image-save.log").open("xb") as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, env=self.engine.environment)
            completed = []

            def compress():
                try:
                    with gzip.GzipFile(filename="", fileobj=output, mode="wb", compresslevel=3, mtime=0) as stream:
                        shutil.copyfileobj(process.stdout, stream, 1024 * 1024)
                    completed.append(True)
                except Exception:
                    completed.append(False)

            worker = threading.Thread(target=compress, daemon=True)
            worker.start()
            worker.join(timeout=600)
            if worker.is_alive():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                worker.join(timeout=5)
                raise ReadinessError("IMAGE_EXPORT_TIMEOUT")
            if completed != [True] or process.wait(timeout=10) != 0:
                raise ReadinessError("IMAGE_EXPORT_FAILED")
        with target.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {"contract": "tested-postiz-image-v1", "image_id": self.image, "source_commit": self.report["source_commit"],
            "source_tree": self.report["source_tree"], "source_manifest_sha256": self.report["source_manifest_sha256"],
            "startup_contract": STARTUP_CONTRACT,
            "node_context_sha256": self.report["node_context_sha256"], "origin": ORIGIN, "origin_profile": PROFILE,
            "filename": target.name, "sha256": sha, "bytes": target.stat().st_size, "runtime_credentials_included": False}
        (folder / "image-verification.json").write_text(json.dumps(manifest, indent=2))
        (folder / "SHA256SUMS.txt").write_text(sha + "  " + target.name + "\n")
        self.report["portable_image"] = manifest

    def cleanup_units(self):
        for unit in reversed(self.unit_names):
            subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=30, check=False)

    def monitor_copy_acceptance(self):
        """A genuine marked copy owns its DB; the main native project stays unmarked."""
        self.phase("isolated_monitor_0133_backup_restore")
        parent = self.created_opt / "monitor-copies"
        parent.mkdir(mode=0o700)
        os.chown(parent, self.user.pw_uid, self.user.pw_gid)
        self.runtime_passwords["monitor"] = secrets.token_urlsafe(36)
        user, port, database = "monitor_admin", 55448, "editorial_hub"
        ident = self.container("monitor-pg", PINS["postgres"], env={"POSTGRES_USER": user,
            "POSTGRES_DB": database, "POSTGRES_PASSWORD": self.runtime_passwords["monitor"],
            "PGDATA": "/var/lib/postgresql/data/pgdata"},
            volumes=[self.volume("monitor-pg") + ":/var/lib/postgresql/data"], memory="192m", swap="768m",
            extra=["--user", "postgres", "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,mode=1777"],
            command=["postgres", "-p", str(port), "-c", "listen_addresses=127.0.0.1",
                     "-c", "shared_buffers=32MB", "-c", "max_connections=50", "-c", "log_statement=ddl"])
        self.pg["monitor"] = (ident, port, user, database)
        self.wait_exec(ident, ["pg_isready", "-h", "127.0.0.1", "-p", str(port), "-U", user, "-d", database])
        count = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", database,
            "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").stdout.strip()
        if count != "0":
            raise ReadinessError("MONITOR_INITIAL_DATABASE_NOT_EMPTY")
        state = json.loads(self.state_path.read_text())
        state["monitor_dsn"] = ("postgresql+psycopg://" + user + ":" + self.runtime_passwords["monitor"]
                                + "@127.0.0.1:55448/editorial_hub")
        private_write(self.state_path, state, exclusive=False)
        os.chown(self.state_path, self.user.pw_uid, self.user.pw_gid)
        self.scenario("monitor-snapshot")
        backup = self.clone_database("monitor", "editorial_hub_tests")
        self.scenario("monitor-restore")
        for mode in ("monitor-snapshot", "monitor-restore"):
            value = json.loads((self.scenario_public / (mode + ".json")).read_text())
            if value.get("passed") is not True or not all(value.get(key) is True for key in (
                    "configured_disabled", "owner_api_verified", "fresh_heartbeat", "heartbeat_advanced",
                    "actual_lanes_stopped", "sender_disabled",
                    "watchdog_no_notify", "history_preserved", "no_browser_or_send", "main_history_unchanged")):
                raise ReadinessError("MONITOR_NATIVE_ACCEPTANCE_INCOMPLETE")
        self.report["checks"]["monitor_0133_native_copy"] = {"passed": True,
            "separate_marker_and_database": True, "postgres_port": port, "initially_empty": True,
            "restart_and_restore": True, "unknown_and_commands_not_replayed": True,
            "watchdog_no_notify": True, "actual_lanes_stopped": True, "heartbeat_advanced_each_start": True,
            "browser_or_send_calls": 0, "backup": backup,
            "scope": "synthetic_disabled_monitor_no_live_delivery"}

    def execute(self):
        self.report["disk"] = disk_preflight(self.job, self.workspace)
        self.phase("pinned_docker_tools")
        binaries, provenance = prepare_docker(self.job / "docker-tools")
        self.report["checks"]["pinned_docker_tools"] = provenance
        self.phase("owned_engine_bootstrap")
        with Engine(self.job / "engine", self.private / "engine.json", binary_root=binaries) as engine:
            self.engine = engine
            try:
                self.docker_versions()
                self.source_copy()
                self.dependencies()
                self.image_pull()
                self.proxy_preflight()
                self.chrome_prerequisite()
                self.build()
                self.inspect_image()
                self.runtime_dependencies()
                self.create_user_and_state()
                self.bootstrap_project()
                self.start_postiz()
                self.install_editorial()
                self.supervisor_and_single_worker()
                self.portal()
                self.backup_restore()
                self.monitor_copy_acceptance()
                self.default_boot()
                self.namespace_proof()
                self.report["native_checks_passed"] = True
                self.export_image()
            finally:
                self.cleanup_units()
        engine_state = json.loads((self.private / "engine.json").read_text())
        if engine_state["state"] != "closed" or engine_state.get("cleanup_errors"):
            raise ReadinessError("OWN_ENGINE_CLEANUP_UNCONFIRMED")
        self.report["checks"]["owned_engine_cleanup"] = {"closed": True, "host_daemon_used": False,
            "cgroup_empty": engine_state["cgroup_empty"], "images_pruned": False, "volumes_pruned": False}
        self.report["accepted"] = True

    def final_report(self, error=None):
        if hasattr(self, "scenario_public"):
            closed = None
            try:
                closed = json.loads((self.private / "engine.json").read_text())
                if (closed.get("state") != "closed" or closed.get("cgroup_empty") is not True or closed.get("cleanup_errors")):
                    raise ReadinessError("SCENARIO_PRODUCER_CLEANUP_UNCONFIRMED")
                self.report["checks"]["scenario_artifact_collection"] = collect_scenario_evidence(
                    self.project, self.public, self.user.pw_uid)
            except (OSError, ValueError, ReadinessError) as collection_error:
                self.report["scenario_artifact_collection_failed"] = True
                self.report["scenario_artifact_collection_failure"] = collection_failure_metadata(collection_error, closed)
                error = error or ReadinessError("SCENARIO_EVIDENCE_COLLECTION_FAILED")
        self.report["finished_at_utc_ns"] = time.time_ns()
        if error is not None:
            message = str(error)
            self.report.update(accepted=False, failed_phase=self.current_phase,
                error_code=message if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", message) else "NATIVE_ACCEPTANCE_FAILED")
        values = set(getattr(self, "runtime_passwords", {}).values())
        if hasattr(self, "state_path") and self.state_path.exists():
            values.update(synthetic_secrets(json.loads(self.state_path.read_text())))
        if hasattr(self, "project") and (self.project / ".env.local").exists():
            for line in (self.project / ".env.local").read_text().splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values.update(synthetic_secrets({key: value}))
        if getattr(self, "created_opt", None) is not None:
            for copy in ("initial", "restored"):
                path = self.created_opt / "monitor-copies" / copy / ".env.local"
                if path.is_file() and not path.is_symlink():
                    for line in path.read_text().splitlines():
                        if line and not line.lstrip().startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            values.update(synthetic_secrets({key: value}))
        # Only fixed codes and redacted bounded tails are exposed, never whole
        # logs, environment, cookie jars, browser profiles or database dumps.
        if error is not None:
            diagnostics = []
            for path in sorted(self.private.glob("*.log")):
                raw = path.read_bytes()
                text = raw.decode("utf-8", errors="replace")
                for value in sorted(values, key=len, reverse=True):
                    text = text.replace(value, "[REDACTED]")
                diagnostics.append({"stage": path.stem, "sha256": digest(raw), "tail": text[-16000:]})
            # An early daemon/unit probe may fail before any stage log exists.
            # Private tails may start midway through a secret. Publish no output
            # fragments at all: only typed enum/number fields and content hashes.
            engine_failure = self.private / "engine-failure.json"
            try:
                info = engine_failure.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                    raise ValueError
                raw = engine_failure.read_bytes()
                if len(raw) > 64 * 1024:
                    raise ValueError
                value = json.loads(raw)
                if (not isinstance(value, dict) or value.get("contract") != "github-owned-engine-failure-v1"
                        or not isinstance(value.get("stage"), str) or value["stage"] not in ENGINE_FAILURE_STAGES
                        or value.get("error_kind") not in ("exit", "timeout", "os_error")
                        or "returncode" not in value or not (value["returncode"] is None or type(value["returncode"]) is int)
                        or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                               for key in ("stdout_sha256", "stderr_sha256"))):
                    raise ValueError
                fields = ("contract", "stage", "error_kind", "returncode", "stdout_sha256", "stderr_sha256")
                diagnostics.append({"stage": "engine_failure", "sha256": digest(raw),
                                    "engine": {key: value[key] for key in fields}})
            except (OSError, ValueError, UnicodeError):
                pass
            (self.public / "diagnostic-summary.json").write_text(json.dumps(diagnostics, ensure_ascii=False))
        self.report["platform_account_login_performed"] = False
        self.report["artifact_upload_is_a_separate_workflow_result"] = True
        return self.report, values


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-hosted", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    github_guard()
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    if (not temporary.is_dir() or not workspace.is_dir() or args.evidence != temporary / "native-readiness-public"
            or args.evidence.exists() or args.evidence.is_symlink()):
        raise ReadinessError("NATIVE_OUTPUT_PATH_INVALID")
    os.environ["RUNNER_TEMP"] = str(temporary)
    staging = temporary / ("native-readiness-unvalidated-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o755)
    acceptance = Acceptance(workspace, staging)
    error = None
    try:
        acceptance.execute()
    except BaseException as caught:
        error = caught
    report, values = acceptance.final_report(error)
    safe = finalize_public(staging, report, values, args.evidence)
    print(json.dumps({"accepted": report["accepted"] and safe, "phase": acceptance.current_phase,
                      "privacy_verified": safe, "external_publications": 0}), flush=True)
    return 0 if report["accepted"] and safe else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as failure:
        code = str(failure)
        print(code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", code) else "NATIVE_ACCEPTANCE_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
