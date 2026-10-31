"""Inspect the real editor in a disposable offline context, without publishing.

Cookies/localStorage are transferred only in memory. The owned persistent profile
stays on about:blank; all synthetic edits belong to a separate nonpersistent
context which is permanently offline before the first edit and always closed.
"""

import argparse
import base64
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, inside, parse_json
from max_postiz_agent.pikabu_browser_worker import ORIGIN, _account, _blocked, _dom
from max_postiz_agent.pikabu_delivery import owner_name


TITLE = "Локальная проверка редактора"
BODY = "Этот синтетический текст существует только в памяти отключённого от сети браузера."
TAGS = ("Космос", "Астрономия")


class NetworkGuard:
    """Monotonic deny policy: no method can restore access after freeze()."""

    def __init__(self, origin=ORIGIN, progress=None):
        self.origin = origin
        self.offline = False
        self.allowed_reads = 0
        self.blocked_mutations = 0
        self.blocked_reads = 0
        self.blocked_websockets = 0
        self.progress = progress
        self.last_progress = 0

    def checkpoint(self):
        if self.progress is not None and time.monotonic() - self.last_progress >= 0.5:
            self.last_progress = time.monotonic()
            self.progress()

    def freeze(self):
        self.offline = True

    def request(self, route):
        request = route.request
        if request.method not in {"GET", "HEAD"}:
            self.blocked_mutations += 1
            self.checkpoint()
            route.abort("blockedbyclient")
            return
        parsed, expected = urlsplit(request.url), urlsplit(self.origin)
        main_document = (request.resource_type == "document" and request.url == self.origin + "/add")
        same_test_origin = self.origin != ORIGIN and parsed.netloc == expected.netloc
        static_host = (parsed.scheme == "https" and (
            parsed.netloc == "pikabu.ru" or re.fullmatch(r"cs\d*\.pikabu\.ru", parsed.netloc)))
        extension = Path(parsed.path).suffix.casefold()
        static = (request.resource_type in {"script", "stylesheet", "image", "font"}
                  and (static_host or same_test_origin)
                  and extension in {".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                                    ".webp", ".avif", ".woff", ".woff2", ".ttf", ".ico"})
        if not self.offline and (main_document or static):
            self.allowed_reads += 1
            self.checkpoint()
            route.continue_()
            return
        self.blocked_reads += 1
        self.checkpoint()
        route.abort("blockedbyclient")

    def websocket(self, route):
        self.blocked_websockets += 1
        self.checkpoint()
        # No connect_to_server: even the upstream handshake is never initiated.
        route.close(code=1000, reason="Offline selector inspection")

    def proof(self):
        return {"allowed_read_requests": self.allowed_reads,
                "blocked_mutation_attempts": self.blocked_mutations,
                "blocked_read_requests": self.blocked_reads,
                "blocked_websocket_attempts": self.blocked_websockets,
                "external_mutations": 0, "live_uploads": 0,
                "all_non_get_head_aborted": True, "service_workers_blocked": True,
                "offline_before_first_edit": self.offline, "network_restored": False}


def _shape_script():
    return """known => {
      const shape=n=>({tag:n.tagName.toLowerCase(),classes:[...n.classList].slice(0,8),
        role:n.getAttribute('role')||'',placeholder:n.getAttribute('placeholder')||'',
        data_test:n.getAttribute('data-test')||'',
        visible:!!(n.getBoundingClientRect().width&&n.getBoundingClientRect().height)});
      const ancestors=n=>{const rows=[];for(let p=n.parentElement;p&&rows.length<5;p=p.parentElement)
        rows.push(shape(p));return rows;};
      const leaves=[...document.querySelectorAll('span,div,a,button')].filter(n=>
        known.tags.includes(n.textContent.trim()) && ![...n.children].some(c=>c.textContent.trim()===n.textContent.trim()));
      return {tag_chip_candidates:leaves.slice(0,16).map(n=>({...shape(n),
        synthetic_tag:known.tags.find(t=>t===n.textContent.trim()),ancestors:ancestors(n)})),
        image_preview_candidates:[...document.querySelectorAll(known.body+' img')].slice(0,8)
          .map(n=>({...shape(n),blob_preview:(n.currentSrc||n.src||'').startsWith('blob:'),ancestors:ancestors(n)})),
        community_control_candidates:[...document.querySelectorAll('input[placeholder="Введите название сообщества"]')]
          .map(n=>({...shape(n),ancestors:ancestors(n)}))};
    }"""


def _synthetic_image():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), "#385580").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _offline_edit(page, selectors, *, include_image, progress=None):
    def checkpoint(stage):
        if progress is not None:
            progress(stage)
    checkpoint("offline_fill_title")
    page.locator(selectors["title"]).fill(TITLE)
    editor = page.locator(selectors["body"])
    checkpoint("offline_fill_body")
    editor.fill(BODY)
    for index, tag in enumerate(TAGS):
        checkpoint("offline_tag_" + str(index) + "_fill")
        field = page.locator(selectors["tags_input"])
        if field.count() != 1:
            raise AgentError("PIKABU_OFFLINE_TAG_INPUT_AMBIGUOUS")
        field.fill(tag)
        checkpoint("offline_tag_" + str(index) + "_enter")
        field.press("Enter")
    if include_image:
        checkpoint("offline_image_paste")
        # This is a synthetic clipboard event after network is irrevocably off.
        # Any upload attempted by the site's paste handler is aborted by the guard.
        editor.evaluate("""(node, encoded) => {
          const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
          const data=new DataTransfer();data.items.add(new File([bytes],'offline-fixture.png',{type:'image/png'}));
          node.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
        }""", _synthetic_image())
    checkpoint("offline_dom_settle")
    page.wait_for_timeout(1800)


def probe_ephemeral(browser, state, options, *, origin=ORIGIN, include_image=True, mutator=None,
                    progress=None):
    """The origin/mutator overrides are test seams; the CLI always uses fixed Pikabu."""
    report = {"state": "not_started", "storage_state_written": False,
              "cookies_or_storage_reported": False, "publish_clicks": 0,
              "ephemeral_context_closed": False, "selector_contract_verified": False,
              "stage": "create_ephemeral_context"}
    if browser is None or not callable(getattr(browser, "new_context", None)):
        return {**report, "state": "unsupported_ephemeral_context", "external_mutations": 0}
    context = None
    guard = NetworkGuard(origin)
    def checkpoint():
        if progress is not None:
            progress({**report, **guard.proof()})
    guard.progress = checkpoint
    try:
        checkpoint()
        context = browser.new_context(storage_state=state, service_workers="block",
                                      accept_downloads=False, locale="ru-RU")
        report["stage"] = "install_network_guards"
        checkpoint()
        context.route("**/*", guard.request)
        context.route_web_socket("**/*", guard.websocket)
        # A second guard forbids accidental submit interactions even offline.
        context.add_init_script("""document.addEventListener('click',e=>{
          if(e.target.closest('[data-test="story-publish"],[data-test="story-preview-publish"]')){
            e.preventDefault();e.stopImmediatePropagation();}
        },true);""")
        page = context.new_page()
        page.set_default_timeout(7000)
        page.clock.install(time=datetime.now(UTC))
        report["stage"] = "readonly_main_document"
        checkpoint()
        response = page.goto(origin + "/add", wait_until="domcontentloaded", timeout=30000)
        if response is None or response.status != 200:
            raise AgentError("PIKABU_OFFLINE_EDITOR_UNAVAILABLE")
        selectors = _dom(options)
        report["stage"] = "readonly_owner_and_blank_editor"
        checkpoint()
        page.locator(selectors["title"]).wait_for(state="visible")
        _account(page, options, selectors, origin=origin)
        _blocked(page, selectors)
        title = page.locator(selectors["title"])
        body = page.locator(selectors["body"])
        if (title.count() != 1 or body.count() != 1 or title.input_value()
                or body.evaluate("node=>node.textContent.trim()") or body.locator("img").count()):
            raise AgentError("PIKABU_OFFLINE_EDITOR_NOT_EMPTY")
        report["owner_verified"] = True
        report["blank_editor_verified"] = True
        # Change route policy first to close the gap before network emulation.
        guard.freeze()
        context.set_offline(True)
        page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
        report["autosave_timers_frozen"] = True
        report["state"], report["stage"] = "offline_before_edit", "offline_local_mutations"
        checkpoint()
        operation = mutator or _offline_edit
        def local_progress(stage):
            report["stage"] = stage
            checkpoint()
        operation(page, selectors, include_image=include_image, progress=local_progress)
        report["stage"] = "offline_selector_capture"
        checkpoint()
        report.update(page.evaluate(_shape_script(), {"tags": list(TAGS), "body": selectors["body"]}))
        report.update(state="offline_selector_probe_complete", synthetic_image_only=include_image,
                      live_upload_verified=False, community_selection_verified=False,
                      offline_editor_only=True)
    except Exception as exc:
        report.update(state="stopped", error_class=type(exc).__name__,
                      code=exc.code if isinstance(exc, AgentError)
                      else "PIKABU_OFFLINE_PROBE_STOPPED")
    finally:
        # Never call set_offline(False), unroute, or persist storage state here.
        if context is not None:
            try:
                context.close()
                report["ephemeral_context_closed"] = True
            except Exception:
                report["state"], report["code"] = "stopped", "PIKABU_EPHEMERAL_CLOSE_UNCONFIRMED"
        report.update(guard.proof())
        checkpoint()
    return report


def run(root, target_id):
    from playwright.sync_api import sync_playwright
    root = Path(root).resolve()
    config = parse_json(inside(root, "config/publishing.json").read_text(encoding="utf-8"))
    targets = [target for target in config.get("targets", []) if target.get("id") == target_id
               and target.get("platform") == "pikabu" and target.get("route") == "pikabu_browser"]
    if len(targets) != 1:
        raise AgentError("PIKABU_TARGET_NOT_CONFIGURED")
    options = targets[0].get("options", {})
    owner_name(options.get("owner"))
    profile = inside(root, options.get("profile_dir", "data/pikabu-browser"))
    if (profile.parent != inside(root, "data") or not profile.name.startswith("pikabu-browser")
            or not profile.is_dir()):
        raise AgentError("PIKABU_EXISTING_ISOLATED_PROFILE_REQUIRED")
    result = {"state": "not_started", "owner_context_closed": False}
    directory = inside(root, "data/pikabu-login")
    directory.mkdir(parents=True, exist_ok=True)
    def progress(value):
        temporary = directory / "offline-dom-progress.tmp"
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, directory / "offline-dom-progress.json")
    progress({"state": "starting", "stage": "owner_context_launch", "storage_state_written": False})
    with profile_lock(profile.with_suffix(".lock")), sync_playwright() as playwright:
        launch = {"headless": True, "service_workers": "block", "accept_downloads": False,
                  "locale": "ru-RU", "chromium_sandbox": True}
        if options.get("channel", "chrome") != "chromium":
            launch["channel"] = options.get("channel", "chrome")
        try:
            persistent = playwright.chromium.launch_persistent_context(str(profile), **launch)
        except Exception as exc:
            return {"state": "stopped", "stage": "owner_context_launch", "error_class": type(exc).__name__,
                    "code": "PIKABU_OFFLINE_OWNER_START_FAILED", "external_mutations": 0,
                    "storage_state_written": False, "publish_clicks": 0,
                    "owner_context_created": False, "owner_context_closed": False}
        try:
            persistent.route("**/*", lambda route: route.abort("blockedbyclient"))
            persistent.route_web_socket("**/*", lambda route: route.close(code=1000))
            if any(page.url not in {"about:blank", "chrome://newtab/"} for page in persistent.pages):
                raise AgentError("PIKABU_OWNER_CONTEXT_NOT_BLANK")
            browser = persistent.browser
            if browser is None or not callable(getattr(browser, "new_context", None)):
                result = {"state": "unsupported_ephemeral_context", "external_mutations": 0,
                          "storage_state_written": False, "publish_clicks": 0}
            else:
                progress({"state": "starting", "stage": "memory_storage_state_read",
                          "storage_state_written": False})
                state = persistent.storage_state(indexed_db=False, opfs=False, credentials=False)
                try:
                    result = probe_ephemeral(browser, state, options, progress=progress)
                finally:
                    state = None
        except Exception as exc:
            result = {"state": "stopped", "code": exc.code if isinstance(exc, AgentError)
                      else "PIKABU_OFFLINE_PROBE_STOPPED", "external_mutations": 0,
                      "storage_state_written": False, "publish_clicks": 0}
        finally:
            try:
                persistent.close()
                result["owner_context_closed"] = True
            except Exception:
                result.update(state="stopped", code="PIKABU_OWNER_CONTEXT_CLOSE_UNCONFIRMED",
                              owner_context_closed=False)
    return result


def _stop_owned_tree(process):
    if os.name == "nt":
        result = subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=15, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def run_bounded(root, target_id, *, timeout=75):
    """A separate controller can stop only the child tree it created itself."""
    command = [sys.executable, str(Path(__file__).resolve()), "--root", str(root),
               "--target-id", target_id, "--internal-run"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", start_new_session=os.name != "nt",
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stopped = _stop_owned_tree(process)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stopped = False
                process.kill()
            progress_path = inside(Path(root).resolve(), "data/pikabu-login/offline-dom-progress.json")
            progress = parse_json(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
            checkpoint_proof = {key: progress.get(key) for key in (
                "external_mutations", "live_uploads", "blocked_mutation_attempts",
                "all_non_get_head_aborted", "offline_before_first_edit", "network_restored")}
            return {**checkpoint_proof, "state": "stopped", "code": "PIKABU_OFFLINE_BUDGET_EXPIRED",
                    "stage": progress.get("stage", "unknown"), "progress": progress,
                    "process_tree_terminated": stopped, "storage_state_written": False,
                    "publish_clicks": 0, "normal_context_close_confirmed": False,
                    "counters_checkpoint_only": True}
        try:
            result = parse_json(stdout)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (AgentError, ValueError) as exc:
            raise AgentError("PIKABU_OFFLINE_WORKER_RESPONSE_INVALID") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Одноразовая offline-копия редактора Пикабу")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target-id", default="pikabu_test")
    parser.add_argument("--internal-run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        result = run(root, args.target_id) if args.internal_run else run_bounded(root, args.target_id)
    except Exception as exc:
        result = {"state": "stopped", "code": exc.code if isinstance(exc, AgentError)
                  else "PIKABU_OFFLINE_PROBE_STOPPED", "external_mutations": 0,
                  "storage_state_written": False, "publish_clicks": 0}
    if args.internal_run:
        print(json.dumps(result, ensure_ascii=True))
        return 0
    directory = inside(root, "data/pikabu-login")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "offline-dom-probe.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result.get(key) for key in (
        "state", "code", "stage", "error_class", "external_mutations", "live_uploads", "publish_clicks",
        "blocked_mutation_attempts", "ephemeral_context_closed", "owner_context_closed",
        "storage_state_written")}, ensure_ascii=True))
    return 0 if result["state"] == "offline_selector_probe_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
