"""Одноразовый owner restart только фиксированного editorial service, без root Python."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

import httpx

from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError, Settings, parse_json
from max_postiz_agent.db import Database
from max_postiz_agent.hub import HubService
from max_postiz_agent.server_maintenance import RESTART_PATH, validate_restart_selection
from max_postiz_agent.server_route_admin import plain, read_local

UNIT = "max-postiz-editorial.service"


@contextmanager
def authority(root):
    plain(root, ".env.local")
    load_env(root)
    settings = Settings.load(root)
    database = Database(settings)
    core = None
    try:
        core = HubService(settings, database, bootstrap_targets=False)
        yield core
    finally:
        if core is not None:
            core.close()
        else:
            database.engine.dispose()


def selection(core):
    info = plain(core.settings.root, RESTART_PATH).stat()
    if os.name != "nt" and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid()):
        raise AgentError("RESTART_SELECTION_PERMISSIONS_INVALID", 409)
    value = parse_json(read_local(core.settings.root, RESTART_PATH, 16 * 1024).decode("utf-8"))
    value = validate_restart_selection(core, value)
    if time.time() >= value["expires_at"]:
        raise AgentError("RESTART_SELECTION_EXPIRED", 409)
    return value


def runtime():
    port = os.environ.get("EDITORIAL_INTERNAL_PORT") or "8092"
    if port not in {"8092", "18092"}:
        raise AgentError("EDITORIAL_INTERNAL_CONFIGURATION_INVALID", 409)
    values = [os.environ.get(key, "") for key in ("EDITORIAL_INTERNAL_KEY", "EDITORIAL_OWNER_USER_ID",
                                                 "EDITORIAL_OWNER_ORGANIZATION_ID")]
    if len(values[0]) < 32 or not all(values):
        raise AgentError("EDITORIAL_INTERNAL_CONFIGURATION_INVALID", 409)
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=2) as client:
            response = client.get("http://127.0.0.1:" + port + "/internal/editorial/runtime",
                headers=dict(zip(("X-Editorial-Key", "X-Editorial-Owner", "X-Editorial-Organization"), values, strict=True)))
            if response.status_code != 200 or len(response.content) > 4096:
                raise ValueError
            value = response.json()
            if (not isinstance(value, dict) or set(value) != {"worker_id", "paused", "restart_pending"}
                    or not isinstance(value["worker_id"], str)
                    or str(uuid.UUID(value["worker_id"])) != value["worker_id"]
                    or value["paused"] is not True or type(value["restart_pending"]) is not bool):
                raise ValueError
            return value
    except (ValueError, httpx.HTTPError):
        raise AgentError("EDITORIAL_RESTART_HEALTH_UNAVAILABLE", 409) from None


def execute(core):
    expected = selection(core)
    before = runtime()
    if before["worker_id"] != expected["previous_worker_id"] or before["restart_pending"] is not True:
        raise AgentError("RESTART_WORKER_CHANGED", 409)
    folder = plain(core.settings.root, "data/editorial-restart-claims")
    folder.mkdir(mode=0o700, exist_ok=True)
    filename = plain(core.settings.root, "data/editorial-restart-claims/" + expected["request_id"] + ".jsonl")
    try:
        descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AgentError("RESTART_ALREADY_CLAIMED", 409) from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as claim:
        def record(value):
            claim.write(json.dumps(value, ensure_ascii=False) + "\n")
            claim.flush()
            os.fsync(claim.fileno())

        record({"state": "claimed", "request_id": expected["request_id"], "unit": UNIT,
                "selection_sha256": hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()})
        if os.name != "nt":
            directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        try:
            if selection(core) != expected or time.time() >= expected["expires_at"]:
                raise AgentError("RESTART_SELECTION_STALE", 409)
            current = runtime()
            if current["worker_id"] != before["worker_id"] or current["restart_pending"] is not True:
                raise AgentError("RESTART_WORKER_CHANGED", 409)
            if time.time() >= expected["expires_at"]:
                raise AgentError("RESTART_SELECTION_EXPIRED", 409)
            # Unit and verb are constants. Polkit permits this service identity only.
            record({"state": "restart_intent"})
            completed = subprocess.run(["/usr/bin/systemctl", "--no-ask-password", "restart", UNIT],
                                       capture_output=True, timeout=45, check=False)
            if completed.returncode != 0:
                raise AgentError("EDITORIAL_RESTART_RESULT_UNKNOWN", 409)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    after = runtime()
                    if after["worker_id"] != before["worker_id"] and after["restart_pending"] is False:
                        result = {"state": "restart_completed", "request_id": expected["request_id"],
                                  "paused": True, "worker_changed": True, "external_publications": 0}
                        record(result)
                        return result
                except AgentError:
                    pass
                time.sleep(0.5)
            raise AgentError("EDITORIAL_RESTART_RESULT_UNKNOWN", 409)
        except BaseException as error:
            code = getattr(error, "code", "EDITORIAL_RESTART_RESULT_UNKNOWN")
            record({"state": "unknown", "code": code if isinstance(code, str) and
                    re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", code) else "EDITORIAL_RESTART_RESULT_UNKNOWN"})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/max-postiz"))
    args = parser.parse_args(argv)
    with authority(args.root.resolve()) as core:
        print(json.dumps(execute(core)))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = getattr(error, "code", "EDITORIAL_RESTART_FAILED")
        print(code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", code)
              else "EDITORIAL_RESTART_FAILED", file=__import__("sys").stderr)
        raise SystemExit(1) from None
