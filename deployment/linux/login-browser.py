"""Ручной вход MAX/VK: owner manifest, отдельный профиль, без публикаций."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from max_postiz_agent.browser_process import run_browser_worker
from max_postiz_agent.cli import load_env
from max_postiz_agent.config import AgentError, Settings, parse_json
from max_postiz_agent.db import Database
from max_postiz_agent.hub import HubService
from max_postiz_agent.server_route_admin import (
    LOGIN_PATH, atomic_write, plain, read_local, validate_login_selection,
)


@contextmanager
def authority(root):
    load_env(root)
    if os.environ.get("MAX_EXISTING_BROWSER_PROFILE"):
        raise AgentError("BROWSER_EXTERNAL_PROFILE_INVALID", 409)
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
    path = plain(core.settings.root, LOGIN_PATH)
    info = path.stat()
    if os.name != "nt" and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid()):
        raise AgentError("LOGIN_SELECTION_PERMISSIONS_INVALID", 409)
    raw = read_local(core.settings.root, LOGIN_PATH, 16 * 1024)
    return validate_login_selection(core, parse_json(raw.decode("utf-8")))


def run_window(core, expected):
    if selection(core) != expected:
        raise AgentError("LOGIN_SELECTION_STALE", 409)
    if expected["platform"] == "max":
        from max_postiz_agent.browser_runtime import session
        opened = session(core.settings, visible=True)
    else:
        from max_postiz_agent.vk_browser_worker import session
        target = next(row for row in core.publishing["targets"] if row["id"] == expected["target_id"])
        opened = session(core.settings.root, target.get("options", {}), visible=True)
    with opened as page:
        if selection(core) != expected:
            raise AgentError("LOGIN_SELECTION_STALE", 409)
        page.goto(expected["url"], wait_until="domcontentloaded", timeout=min(
            30000, max(1, int((expected["expires_at"] - time.time()) * 1000))))
        while time.time() < expected["expires_at"] and page.context.pages:
            if selection(core) != expected:
                raise AgentError("LOGIN_SELECTION_STALE", 409)
            page.wait_for_timeout(500)
    return {"state": "manual_login_window_closed", "target_id": expected["target_id"],
            "selection_id": expected["selection_id"], "platform": expected["platform"],
            "access_verified": False, "live_delivery_verified": False, "external_publications": 0}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/max-postiz"))
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args(argv)
    with authority(args.root.resolve()) as core:
        expected = selection(core)
        if args.child:
            if parse_json(sys.stdin.read(16 * 1024)) != expected:
                raise AgentError("LOGIN_SELECTION_STALE", 409)
            result = run_window(core, expected)
        else:
            completed = run_browser_worker([sys.executable, str(Path(__file__).resolve()),
                "--root", str(core.settings.root), "--child"],
                input=json.dumps(expected, ensure_ascii=False),
                timeout=max(1, expected["expires_at"] - time.time()) + 10)
            if completed.returncode != 0:
                raise AgentError("OWNER_BROWSER_WORKER_FAILED", 409)
            result = parse_json(completed.stdout)
            required = {"state", "target_id", "selection_id", "platform", "access_verified",
                        "live_delivery_verified", "external_publications"}
            if (not isinstance(result, dict) or set(result) != required
                    or result["state"] != "manual_login_window_closed"
                    or any(result[key] != expected[key] for key in ("target_id", "selection_id", "platform"))
                    or result["access_verified"] is not False or result["live_delivery_verified"] is not False
                    or type(result["external_publications"]) is not int or result["external_publications"] != 0):
                raise AgentError("OWNER_BROWSER_RECEIPT_INVALID", 409)
            atomic_write(core.settings.root, "data/owner-browser-result.json",
                         (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        print(json.dumps(result, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        code = getattr(error, "code", "OWNER_BROWSER_FAILED")
        print(code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,99}", code)
              else "OWNER_BROWSER_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
