"""VK browser contract against a local native DOM fixture; no real account or external calls."""

import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from PIL import Image

from max_postiz_agent import vk_browser_worker as worker
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.vk_browser import OBSERVED_DOM, VKBrowser, dom_contract, prepare, wall_url
from vk_browser_test_clock import semantic_fence_clock as semantic_fence_clock

pytest_plugins = ["test_four_platform_hub"]


OWNER = "123456"
SELECTORS = {
    "wall_ready": "#wall-ready", "uploaded_images": "#uploaded img",
    "recipient": "#recipient", "preview_author": "#preview-author",
    "preview_text": "#preview-text", "preview_images": "#preview-images img",
    "publish": "#publish", "publish_label": "Опубликовать", "post_images": ".approved-photo",
    "forbidden_settings": ".forbidden-setting", "extra_attachments": ".extra-attachment",
    "post_extra_attachments": ".unexpected-post-attachment", "blocked": ".captcha",
    "errors": ".editor-error",
}


@pytest.fixture
def sample(tmp_path, request):
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "navy").save(output, format="PNG")
    raw = output.getvalue()
    (tmp_path / "approved.png").write_bytes(raw)
    options = {"owner": OWNER, "dom_contract_verified": True, "dom_contract": dict(SELECTORS),
               "media_contract_verified": True, "profile_dir": "data/vk-browser", "operation_timeout_seconds": 1}
    snapshot = {"title": "Новая звезда", "body": "Полный научный текст.\n\nИсточник: https://science.example.test/a",
                "destination": OWNER, "media": [{"path": "approved.png", "mime": "image/png",
                "sha256": hashlib.sha256(raw).hexdigest()}], "metadata": {}}
    payload = {"action": "publish", "destination": OWNER, "snapshot": snapshot, "request_id": "vk-fixture"}
    return SimpleNamespace(root=tmp_path, raw=raw, options=options, snapshot=snapshot, payload=payload,
                           user_properties=request.node.user_properties)


def post_html(state, text, ident, *, readback=False):
    body = html.escape(text + (" changed" if state.corrupt_readback else ""))
    if readback and getattr(state, "rich_readback", False):
        body = "<strong>" + body + "</strong>"
    href = getattr(state, "post_body_link_href", None)
    if href is not None:
        word = html.escape(text.split(" ", 1)[0])
        body = body.replace(word, '<a href="' + html.escape(href, quote=True) + '">' + word + '</a>', 1)
    photo = '<img class="approved-photo" src="/photo.png">' if state.uploads else ""
    author = state.post_author_override or state.destination
    author_path = '/club' + author[1:] if author.startswith('-') else '/id' + author
    return ('<article data-testid="post"><a data-testid="post-header-title" href="' + author_path
            + getattr(state, "post_author_tracking", "")
            + '">Автор</a><span data-testid="post_text" style="white-space:pre-wrap">'
            + body + '</span><a data-testid="post_date_block_preview" href="/wall'
            + state.destination + '_' + str(ident) + '">Сейчас</a>' + photo + '</article>')


def fixture_html(state):
    posts = getattr(state, "old_rich_posts", "") + "".join(
        post_html(state, text, ident, readback=True) for text, ident in state.posts)
    owner = "999999" if state.wrong_owner else OWNER
    code = r"""
const show=document.getElementById('create');
show.onclick=()=>{document.getElementById('composer').hidden=false;window.opens++;};
const editor=document.getElementById('editor'),file=document.getElementById('upload');
editor.addEventListener('input',()=>window.fills++);
file.onchange=async()=>{
 const data=new FormData();data.append('photo',file.files[0]);
 const response=await fetch('/upload',{method:'POST',body:data});
 if(response.ok)document.getElementById('uploaded').innerHTML='<img src="/photo.png">';
};
document.getElementById('next').onclick=()=>{
 document.getElementById('preview').hidden=false;
 document.getElementById('preview-text').textContent=editor.innerText;
 if(window.personal){
  const preview=document.getElementById('preview-text');
  preview.textContent='';
  const root=document.createElement('div');
  const parts=editor.innerText.split('\n\n');
  parts.forEach((text,i)=>{
   if(i){const gap=document.createElement('div');gap.append(document.createElement('br'),' ');root.append(gap);}
   root.append(document.createTextNode(text));
  });preview.append(root);
 }
 document.getElementById('preview-images').innerHTML=document.getElementById('uploaded').innerHTML;
 document.getElementById('base').hidden=true;window.nexts++;
};
document.getElementById('publish').onclick=async()=>{
 window.clicks++;
 const text=window.personal?editor.innerText:document.getElementById('preview-text').innerText;
 const story=window.personal&&document.getElementById('story').checked;
 const response=await fetch('/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
  text,story,fixture_ready:window.spaEvents||[],fixture_waiting:window.spaWaiting||null,
  fixture_revoked:window.spaRevoked===true})});
 const data=await response.json();
 document.getElementById('posts').innerHTML=data.html;
 document.getElementById('composer').hidden=true;
};
window.clicks=0;window.fills=0;window.opens=0;window.nexts=0;
"""
    if getattr(state, "preview_photo_delay_ms", 0) or getattr(state, "preview_photo_invalid", False):
        code = code.replace(
            "document.getElementById('preview-images').innerHTML=document.getElementById('uploaded').innerHTML;",
            "document.getElementById('preview-images').innerHTML=document.getElementById('uploaded').innerHTML.replace('/photo.png','/photo.png?preview-load=1');")
    preview_change = getattr(state, "preview_wait_change", None)
    if preview_change:
        mutation = {
            "account": "document.querySelector('[data-testid=header-profile-menu-button]').href='/id999999';",
            "text": "document.getElementById('preview-text').textContent='Unapproved changed preview';",
            "image": "document.querySelector('#preview-images img').src='/changed-preview.png';",
        }[preview_change]
        code += ("new MutationObserver(()=>setTimeout(()=>{" + mutation
                 + "},50)).observe(document.getElementById('preview-images'),{childList:true});")
    if any(getattr(state, name, 0) for name in (
            "editor_mount_delay_ms", "next_ready_delay_ms", "story_aria_delay_ms")):
        code += r"""
window.spaEvents=[];window.spaWaiting=null;window.spaRevoked=false;
const transition=(name,delay,finish)=>{
 window.spaWaiting=name;window.spaEvents.push(name+'_start');
 if(name===window.fixturePauseAt)setTimeout(()=>{
  window.spaRevoked=true;window.spaEvents.push(name+'_paused');
 },100);
 setTimeout(()=>{finish();window.spaEvents.push(name+'_ready');window.spaWaiting=null;},delay);
};
"""
        code += "window.fixturePauseAt=" + json.dumps(getattr(state, "pause_transition", None)) + ";"
        if getattr(state, "next_ready_delay_ms", 0):
            mode = getattr(state, "next_delay_mode", "disabled")
            code += "const delayedNext=document.getElementById('next');"
            if mode == "absent":
                code += "delayedNext.remove();"
                finish = "document.getElementById('base').append(delayedNext);"
            elif mode == "hidden":
                code += "delayedNext.hidden=true;"
                finish = "delayedNext.hidden=false;"
            else:
                assert mode == "disabled"
                code += "delayedNext.disabled=true;"
                finish = "delayedNext.disabled=false;"
            code += ("editor.addEventListener('input',()=>transition('next',"
                     + str(state.next_ready_delay_ms) + ",()=>{" + finish + "}));")
        if getattr(state, "story_aria_delay_ms", 0):
            code += ("const delayedStory=document.getElementById('story');delayedStory.onchange=()=>{"
                     "window.storyChanges++;const checked=String(delayedStory.checked);transition('story',"
                     + str(state.story_aria_delay_ms)
                     + ",()=>delayedStory.setAttribute('aria-checked',checked));};")
        if getattr(state, "editor_mount_delay_ms", 0):
            # Register child-control handlers before detaching the whole subtree.
            code += ("const delayedComposer=document.getElementById('composer');delayedComposer.remove();"
                     "show.onclick=()=>{window.opens++;transition('editor',"
                     + str(state.editor_mount_delay_ms)
                     + ",()=>{document.body.append(delayedComposer);delayedComposer.hidden=false;});};")
    if getattr(state, "create_delay_ms", 0):
        code += ("const delayedCreate=document.getElementById('create');delayedCreate.remove();"
                 "setTimeout(()=>document.getElementById('wall-ready').prepend(delayedCreate),"
                 + str(state.create_delay_ms) + ");")
    if getattr(state, "account_href_delay_ms", 0):
        code += ("const account=document.querySelector('[data-testid=header-profile-menu-button]');"
                 "const accountHref=account.getAttribute('href');account.setAttribute('href','');"
                 "setTimeout(()=>account.setAttribute('href',accountHref),"
                 + str(state.account_href_delay_ms) + ");")
    personal = getattr(state, "personal", False)
    settings = ('<div data-testid="posting_coowners_cell">Пригласить соавторов&nbsp;</div>'
                '<div data-testid="posting_settings_visibility">Кто увидит этот пост<br>&nbsp;<br>Все<br>&nbsp;</div>'
                '<input id="story" type="checkbox" role="switch" '
                'data-testid="posting_settings_repost_to_story_switch" aria-checked="true" checked '
                'onchange="this.setAttribute(\'aria-checked\',String(this.checked));window.storyChanges++">') if personal else ''
    if personal and getattr(state, "native_story_label", False):
        settings = settings.replace('<input id="story"', '<label id="native-story-label" style="display:block;width:32px;height:20px;position:relative"><input style="opacity:0;pointer-events:none;width:32px;height:20px;position:absolute" id="story"')
        settings += '<span style="display:block;width:32px;height:20px">Story</span></label>'
    identity = ('<a id="recipient" href="/'
                + ('club' + state.destination[1:] if state.destination.startswith('-') else 'id' + state.destination)
                + '" data-owner-id="' + state.destination + '">Адресат</a>'
                '<a id="preview-author" href="/'
                + ('club' + state.destination[1:] if state.destination.startswith('-') else 'id' + state.destination)
                + '">Автор публикации</a>') if not personal else ''
    return ('<!doctype html><meta charset="utf-8"><title>VK local fixture</title>'
            '<style>[hidden]{display:none!important}img{width:320px;height:240px}'
            '#editor{display:block;min-height:70px;white-space:pre-wrap;width:600px}</style>'
            '<a data-testid="header-profile-menu-button" href="/id' + owner + '">Профиль</a>'
            '<section id="wall-ready"><button id="create" data-testid="posting_create_post_button">Создать пост</button>'
            '<div id="posts">' + posts + '</div></section><div id="composer" role="dialog" '
            'data-testid="posting_modal_box" aria-label="Форма постинга" hidden>'
            '<div id="base"><span id="editor" role="textbox" contenteditable="true" '
            'data-testid="posting_base_screen_input_message"></span><input type="file" id="upload" '
            'data-testid="posting_base_screen_download_from_device"><div id="uploaded"></div>'
            '<button id="next" data-testid="posting_base_screen_next">Далее</button></div>'
            '<div id="preview" hidden>' + identity + settings +
            '<span id="preview-text" data-testid="showmoretext-in" style="white-space:pre-wrap"></span>'
            '<div id="preview-images"></div><button id="publish" data-testid="posting_submit_button">'
            'Опубликовать</button></div></div><script>window.personal=' + str(personal).lower()
            + ';window.storyChanges=0;' + code + '</script>').encode()


@pytest.fixture
def local_vk(sample):
    state = SimpleNamespace(destination=OWNER, raw=sample.raw, uploads=0, publishes=0, posts=[],
                            gets=[], wrong_owner=False, corrupt_readback=False, bad_photo=False,
                            drop_reply=False, redirect_wrong_wall=False, post_author_override=None,
                            personal=False, stories=0, publish_readiness=[], preview_completions=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.gets.append(self.path)
            preview_done = None
            if state.redirect_wrong_wall and self.path == "/id" + OWNER:
                self.send_response(302)
                self.send_header("Location", "/id999999")
                self.end_headers()
                return
            if self.path.split("?", 1)[0] in {"/photo.png", "/changed-preview.png"}:
                if self.path == "/photo.png?preview-load=1":
                    preview_done = threading.Event()
                    state.preview_completions.append(preview_done)
                    time.sleep(getattr(state, "preview_photo_delay_ms", 0) / 1000)
                    raw = b"undecodable preview" if getattr(state, "preview_photo_invalid", False) else state.raw
                elif self.path == "/changed-preview.png":
                    raw = state.changed_preview_raw
                else:
                    raw = b"wrong photo" if state.bad_photo else state.raw
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
            else:
                raw = fixture_html(state)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            finally:
                if preview_done is not None:
                    preview_done.set()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/upload":
                assert state.raw in raw
                state.uploads += 1
                result = {}
            elif self.path == "/publish":
                state.publishes += 1
                posted = json.loads(raw)
                text = posted["text"]
                state.publish_readiness.append({"ready": posted.get("fixture_ready"),
                    "waiting": posted.get("fixture_waiting"), "revoked": posted.get("fixture_revoked")})
                state.stories += int(posted.get("story", False))
                state.posts.append((text, 51))
                if state.drop_reply:
                    self.close_connection = True
                    return
                delay = getattr(state, "publish_response_delay_seconds", 0)
                if delay:
                    time.sleep(delay)
                result = {"html": post_html(state, text, 51)}
            else:
                self.send_error(404)
                return
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.origin = "http://127.0.0.1:" + str(server.server_port)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        value = runtime.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        yield value
        value.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    value = context.new_page()
    value.set_default_timeout(2000)
    yield value
    context.close()


def run(page, sample, state, authorize=lambda: None):
    result, completed = None, False
    try:
        result = worker.run_publish(page, sample.root, sample.options, sample.payload, authorize,
                                    origin=state.origin, fixture=True)
        completed = True
        return result
    finally:
        _record_fixture_diagnostics(sample, state, result, None if completed else sys.exc_info()[1])


def _record_fixture_diagnostics(sample, state, result, raised):
    """Record only safe fixture fields; reporting must not alter an operation outcome."""
    try:
        states = {"intent", "editor_open_intent", "fill_intent", "upload_intent", "uploaded",
                  "next_intent", "story_disable_intent", "click_intent", "readback_pending",
                  "confirmed", "failed", "unknown"}
        error_types = {"AgentError", "TimeoutError", "Error", "OSError", "ValueError",
                       "TypeError", "RuntimeError", "AssertionError", "FileNotFoundError"}
        try:
            _, journal = worker._read(sample.root, sample.payload["request_id"])
        except Exception:
            journal = {}
        journal = journal if isinstance(journal, dict) else {}
        result = result if isinstance(result, dict) else {}
        evidence = result.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        error = journal.get("private_error")
        error = error if isinstance(error, dict) else {}
        code = journal.get("failure_code") or evidence.get("failure_code") or getattr(raised, "code", None)
        stage = journal.get("last_action_intent")
        status = result.get("state") or journal.get("state")
        record = {"fixture": True, "outcome": "raised" if raised is not None else "returned",
                  "state": status if isinstance(status, str) and status in states else None,
                  "last_action_intent": stage if isinstance(stage, str) and stage in states else None,
                  "failure_code": code if isinstance(code, str) and re.fullmatch(
                      r"(?:VK_BROWSER|TARGET|EDITORIAL)_[A-Z0-9_]{1,96}", code) else None,
                  "private_error_type": error.get("type") if isinstance(error.get("type"), str)
                      and error["type"] in error_types else None,
                  "raised_error_type": type(raised).__name__ if raised is not None
                      and type(raised).__name__ in error_types else None}
        for key, value in (("clicks", journal.get("clicks")), ("uploads", state.uploads),
                           ("publishes", state.publishes)):
            record[key] = value if type(value) is int and 0 <= value <= 100000 else None
        message = error.get("message")
        context = re.match(
            r"^(Locator\.(?:click|fill|set_input_files|uncheck|wait_for)|"
            r"Page\.(?:goto|reload|wait_for_function)|Frame\.wait_for_function): "
            r"Timeout ([0-9]{1,6})ms exceeded", message[:256]) if isinstance(message, str) else None
        if context and 1 <= int(context[2]) <= 180000:
            record.update(operation=context[1], timeout_ms=int(context[2]))
        sample.user_properties.append(("vk_browser_diagnostics", json.dumps(record, sort_keys=True)))
    except Exception:
        # The original return or exception wins even if fixture diagnostics fail.
        return None


def publish_diagnostics(sample, state, result):
    """Report synthetic fixture phase failures without dumping snapshot text."""
    details = {"failure_code": result["evidence"].get("failure_code"),
               "journal_state": result["evidence"].get("journal_state")}
    try:
        _, journal = worker._read(sample.root, sample.payload["request_id"])
        details.update(last_action_intent=journal.get("last_action_intent"),
                       private_error=journal.get("private_error"))
    except (AgentError, OSError, ValueError) as exc:
        details["diagnostic_error"] = type(exc).__name__
    details.update(uploads=state.uploads, publishes=state.publishes,
                   publish_readiness=state.publish_readiness, result=result)
    return details


def test_fixture_diagnostics_are_serialized_to_junit_without_private_fields(tmp_path):
    child = tmp_path / "test_diagnostic_case.py"
    child.write_text(r'''
from types import SimpleNamespace
import pytest
from test_vk_browser import sample as sample, run, worker

SECRET = "diagnostic-private-fixture-canary"
RAW_TEXT = "raw-snapshot-text-fixture-canary"

@pytest.mark.parametrize("mode", ["unknown", "confirmed", "raised", "malicious"])
def test_operation(sample, monkeypatch, mode):
    sample.snapshot["body"] = RAW_TEXT
    sample.options["private"] = SECRET
    expected = {"state": "confirmed" if mode == "confirmed" else "unknown",
                "evidence": {"failure_code": "VK_BROWSER_OPERATION_UNKNOWN"},
                "raw_journal": SECRET}
    journal = {"state": "unknown", "last_action_intent": "upload_intent", "clicks": 0,
               "failure_code": "VK_BROWSER_OPERATION_UNKNOWN",
               "snapshot": {"text": RAW_TEXT}, "credentials": SECRET,
               "private_error": {"type": "TimeoutError", "message":
                   "Locator.set_input_files: Timeout 1000ms exceeded.\nCall log: https://user:"
                   + SECRET + "@private.invalid/" + RAW_TEXT}}
    local = SimpleNamespace(origin="http://" + SECRET + "@private.invalid", uploads=0, publishes=0)
    if mode == "malicious":
        journal.update(state=SECRET, last_action_intent=SECRET, failure_code=SECRET,
                       clicks=float("nan"), private_error={"type": SECRET, "message": SECRET})
        local.uploads = local.publishes = SECRET
    monkeypatch.setattr(worker, "_read", lambda *args: (None, journal))
    planned_error = RuntimeError(SECRET)
    def operation(*args, **kwargs):
        if mode == "raised":
            raise planned_error
        return expected
    monkeypatch.setattr(worker, "run_publish", operation)
    if mode == "raised":
        with pytest.raises(RuntimeError) as actual:
            run(None, sample, local)
        assert actual.value is planned_error
    else:
        assert run(None, sample, local) is expected
    assert "user_properties" not in sample.options
    assert "user_properties" not in sample.payload
    assert sample.payload["snapshot"] is sample.snapshot
''', encoding="utf-8")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    junit = tmp_path / "diagnostics.xml"
    env = dict(os.environ)
    for key in ("TEST_DATABASE_URL", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    env.update(PYTHONPATH=str(Path(__file__).resolve().parent), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-m", "pytest", "-c", str(config), str(child),
         "-q", "--basetemp=" + str(tmp_path / "child-state"), "--junitxml=" + str(junit)],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30, check=False)
    (tmp_path / "child-output.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, "Isolated diagnostic pytest failed; inspect child-output.log"
    raw = junit.read_text(encoding="utf-8")
    assert "diagnostic-private-fixture-canary" not in raw
    assert "raw-snapshot-text-fixture-canary" not in raw
    assert "private.invalid" not in raw and "Call log:" not in raw
    cases = list(ElementTree.parse(junit).getroot().iter("testcase"))  # noqa: S314 -- local pytest report
    assert len(cases) == 4 and all(case.find("failure") is None and case.find("error") is None for case in cases)
    for case in cases:
        properties = [json.loads(prop.get("value")) for prop in case.findall("./properties/property")
                      if prop.get("name") == "vk_browser_diagnostics"]
        assert len(properties) == 1
        details = properties[0]
        assert details["fixture"] is True
        assert details["outcome"] == ("raised" if "raised" in case.get("name") else "returned")
        if "malicious" in case.get("name"):
            assert details["failure_code"] is details["last_action_intent"] is details["private_error_type"] is None
            assert details["clicks"] is details["uploads"] is details["publishes"] is None
            assert "operation" not in details and "timeout_ms" not in details
        else:
            assert details["failure_code"] == "VK_BROWSER_OPERATION_UNKNOWN"
            assert details["last_action_intent"] == "upload_intent"
            assert details["private_error_type"] == "TimeoutError"
            assert details["operation"] == "Locator.set_input_files" and details["timeout_ms"] == 1000


@pytest.mark.parametrize("fault", ["read", "serialize"])
@pytest.mark.parametrize("raised", [False, True])
def test_fixture_diagnostic_failure_does_not_change_return_or_original_exception(
        sample, monkeypatch, fault, raised):
    expected = {"state": "unknown", "evidence": {"failure_code": "VK_BROWSER_OPERATION_UNKNOWN"}}
    planned_error = RuntimeError("Original operation error")
    local = SimpleNamespace(origin="http://127.0.0.1", uploads=0, publishes=0)
    attempts = []

    def operation(*args, **kwargs):
        if raised:
            raise planned_error
        return expected

    def diagnostic_failure(*args, **kwargs):
        attempts.append(True)
        raise OSError("Fixture diagnostic storage failed")

    monkeypatch.setattr(worker, "run_publish", operation)
    monkeypatch.setattr(worker, "_read", lambda *args: (None, {}))
    with monkeypatch.context() as scoped:
        if fault == "read":
            scoped.setattr(worker, "_read", diagnostic_failure)
        else:
            scoped.setattr(json, "dumps", diagnostic_failure)
        if raised:
            with pytest.raises(RuntimeError) as actual:
                run(None, sample, local)
            assert actual.value is planned_error
        else:
            assert run(None, sample, local) is expected
    assert attempts == [True]


def personal_wall(sample, state):
    state.personal = True
    sample.snapshot["media"] = []
    sample.options["media_contract_verified"] = False
    selectors = sample.options["dom_contract"]
    selectors.pop("recipient")
    selectors.pop("preview_author")
    selectors.update(identity_contract="personal-wall-settings-v1", text_contract="vk-spacer-div-v1")


@pytest.mark.parametrize("composer_visible", [False, True])
def test_background_create_changes_are_ignored_only_inside_visible_composer(sample, page, composer_visible):
    selectors = dom_contract(sample.options)
    hidden = "" if composer_visible else " hidden"
    page.set_content('<a data-testid="header-profile-menu-button" href="/id123456">Профиль</a>'
                     '<button data-testid="posting_create_post_button">Создать</button>'
                     '<div role="dialog" data-testid="posting_modal_box"' + hidden + '>'
                     '<span data-testid="posting_base_screen_input_message">Точный текст</span></div>')
    before = worker._mutation_stamp(page, selectors)
    page.locator('[data-testid="posting_create_post_button"]').evaluate(
        "e=>e.innerHTML='<span>Фоновый черновик обновлён</span>'")
    assert (worker._mutation_stamp(page, selectors) == before) is composer_visible
    if composer_visible:
        page.locator('[data-testid="posting_base_screen_input_message"]').evaluate(
            "e=>e.textContent='Другой текст'")
        assert worker._mutation_stamp(page, selectors) != before
        before = worker._mutation_stamp(page, selectors)
        page.locator('[data-testid="header-profile-menu-button"]').evaluate(
            "e=>e.setAttribute('href','/id999999')")
        assert worker._mutation_stamp(page, selectors) != before
        before = worker._mutation_stamp(page, selectors)
        page.locator('[data-testid="posting_modal_box"]').evaluate("e=>e.hidden=true")
        assert worker._mutation_stamp(page, selectors) != before


def test_native_background_draft_update_does_not_block_exact_guarded_form(
        sample, local_vk, page, semantic_fence_clock, monkeypatch):
    # This positive DOM contract is not a one-second HTTP performance test.
    # The worker's separate fresh-authorization fence remains unchanged.
    sample.options["operation_timeout_seconds"] = 5
    personal_wall(sample, local_vk)
    original, changed = worker._all_preview_media, False

    def background_update(*args, **kwargs):
        nonlocal changed
        if not changed:
            page.locator('#create').evaluate("e=>e.innerHTML='<span>Обновлён черновик</span>'")
            changed = True
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, '_all_preview_media', background_update)
    result = run(page, sample, local_vk)
    _, journal = worker._read(sample.root, sample.payload['request_id'])
    diagnostic = {"failure_code": journal.get("failure_code"),
        "error_type": journal.get("private_error", {}).get("type"),
        "last_action_intent": journal.get("last_action_intent"),
        "clicks": journal.get("clicks", 0), "publishes": local_vk.publishes}
    assert result['state'] == 'confirmed', diagnostic
    assert changed and local_vk.publishes == 1 and local_vk.stories == local_vk.uploads == 0
    assert journal['worker_code_sha256'] == worker.WORKER_SOURCE_SHA256
    assert journal['last_action_intent'] == 'click_intent' and journal['clicks'] == 1


def test_personal_settings_exact_native_spacing_and_story_disabled_before_one_post(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert result["verified"] is False and result["evidence"]["fixture"] is True
    assert result["evidence"]["sender_settings"] == {
        "identity_contract": "personal-wall-settings-v1", "author_id": OWNER, "recipient_id": OWNER,
        "visibility": "Все", "coauthors": [], "story_checked": False,
        "basis": "own_wall_account_fresh_composer"}
    assert local_vk.publishes == 1 and local_vk.uploads == local_vk.stories == 0
    assert local_vk.posts[0][0] == prepare(sample.root, sample.options, OWNER, sample.snapshot)["text"]
    before = list(local_vk.gets)
    assert run(page, sample, local_vk) == result
    assert local_vk.gets == before and local_vk.publishes == 1


@pytest.mark.parametrize("budget,expected", [(1, "unknown"), (5, "confirmed")])
def test_persisted_publish_with_delayed_response_respects_result_budget_without_resending(
        sample, local_vk, page, semantic_fence_clock, budget, expected):
    personal_wall(sample, local_vk)
    sample.options["operation_timeout_seconds"] = budget
    local_vk.publish_response_delay_seconds = 1.5
    result = run(page, sample, local_vk)
    _, journal = worker._read(sample.root, sample.payload["request_id"])
    diagnostic = {"failure_code": journal.get("failure_code"),
        "error_type": journal.get("private_error", {}).get("type"),
        "last_action_intent": journal.get("last_action_intent"),
        "clicks": journal.get("clicks", 0), "publishes": local_vk.publishes}
    assert result["state"] == expected, diagnostic
    assert local_vk.publishes == len(local_vk.posts) == 1
    assert local_vk.stories == local_vk.uploads == 0
    assert journal["clicks"] == 1 and journal["last_action_intent"] == "click_intent"
    if budget == 1:
        assert result["evidence"]["failure_code"] == "VK_BROWSER_PUBLISH_RESULT_UNKNOWN"
        assert journal["state"] == "unknown" and result["evidence"]["proof"] is None
        # Even after the original response becomes visible, the unknown journal
        # remains authoritative until a separate read-only reconciliation.
        page.wait_for_function("document.querySelectorAll('[data-testid=post]').length===1", timeout=5000)
    else:
        assert len(result["evidence"]["proof"]["reloads"]) == 2
        assert journal["state"] == "confirmed"
    before = list(local_vk.gets)
    assert run(page, sample, local_vk) == result
    assert local_vk.gets == before and local_vk.publishes == 1


def _old_rich_post(markup):
    return ('<article data-testid="post"><a data-testid="post-header-title" href="/id' + OWNER
            + '">Автор</a><span data-testid="post_text" style="white-space:pre-wrap">' + markup
            + '</span><a data-testid="post_date_block_preview" href="/wall' + OWNER
            + '_17">Раньше</a></article>')


def test_unrelated_unsupported_rich_history_does_not_block_one_exact_post_or_reload(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)
    local_vk.old_rich_posts = _old_rich_post(
        '<strong>Другой старый пост</strong><div><img src="/photo.png">Постороннее описание.</div>')
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert result["verified"] is False and result["evidence"]["fixture"] is True
    assert len(result["evidence"]["proof"]["reloads"]) == 2
    assert local_vk.publishes == 1 and local_vk.uploads == local_vk.stories == 0
    assert local_vk.posts[0][0] == prepare(sample.root, sample.options, OWNER, sample.snapshot)["text"]
    before = list(local_vk.gets)
    assert run(page, sample, local_vk) == result
    assert local_vk.gets == before and local_vk.publishes == 1


@pytest.mark.parametrize("decoration", ["none", "hidden_suffix", "hidden_interleaved"])
def test_potential_matching_unsupported_history_still_blocks_before_editor(
        sample, local_vk, page, semantic_fence_clock, decoration):
    personal_wall(sample, local_vk)
    text = prepare(sample.root, sample.options, OWNER, sample.snapshot)["text"]
    if decoration == "none":
        markup = "<strong>" + html.escape(text) + "</strong>"
    elif decoration == "hidden_suffix":
        markup = html.escape(text) + '<span hidden>Старые скрытые сведения</span>'
    else:
        offset = 8
        markup = (html.escape(text[:offset]) + '<span hidden>Вставка посередине слова</span>'
                  + html.escape(text[offset:]))
    local_vk.old_rich_posts = _old_rich_post(markup)
    result = run(page, sample, local_vk)
    assert result["state"] == "failed", result
    assert result["evidence"]["failure_code"] == "VK_BROWSER_RENDERED_TEXT_UNSUPPORTED"
    assert local_vk.publishes == local_vk.uploads == local_vk.stories == 0
    assert page.evaluate("window.opens") == 0
    before = list(local_vk.gets)
    assert run(page, sample, local_vk) == result
    assert local_vk.gets == before and local_vk.publishes == 0


def test_matching_unsupported_target_on_independent_reload_is_unknown_never_confirmed(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)
    local_vk.rich_readback = True
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["verified"] is False, result
    assert result["external_id"] == OWNER + "_51"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_RENDERED_TEXT_UNSUPPORTED"
    assert result["evidence"]["proof"] is None
    assert local_vk.publishes == 1 and local_vk.uploads == local_vk.stories == 0
    before = list(local_vk.gets)
    repeated = run(page, sample, local_vk)
    assert repeated["state"] == "unknown" and repeated["verified"] is False
    assert repeated["external_id"] == result["external_id"]
    assert local_vk.gets == before and local_vk.publishes == 1


@pytest.mark.parametrize("markup,expected,possible", [
    ("Другая статья", "Согласованный текст", False),
    ("БА", "АБ", False),
    ("Начало", "НачалоКонец", False),
    ("Текст", "Текст", True),
    ("А\u00a0Б\u2009В\nГ", "А Б\n\nВ\tГ", True),
    ("Привет🙂 мир", "Привет🙂\nмир", True),
    ('А<span hidden>Вставка</span>Б<span hidden>Другое</span>В', "АБВ", True),
    ('<strong>Кандидат</strong><span hidden>Декорация</span>', "Кандидат", True),
])
def test_history_prefilter_preserves_order_unicode_whitespace_and_hidden_subsequences(page, markup, expected, possible):
    page.set_content('<span id="candidate">' + markup + '</span>')
    assert worker._could_match_text(page.locator("#candidate"), expected) is possible


def test_history_prefilter_refuses_unbounded_raw_text_instead_of_skipping_it(page):
    page.set_content('<span id="candidate"></span>')
    page.locator("#candidate").evaluate("e=>e.textContent='x'.repeat(200001)")
    with pytest.raises(AgentError):
        worker._could_match_text(page.locator("#candidate"), "Кандидат")


@pytest.mark.parametrize("change,code", [
    ("document.querySelector('[data-testid=posting_coowners_cell]').textContent='Другой автор'",
     "VK_BROWSER_COAUTHORS_NOT_EMPTY"),
    ("document.querySelector('[data-testid=posting_coowners_cell]').innerHTML='<a href=/id999>Другой</a>'",
     "VK_BROWSER_COAUTHORS_NOT_EMPTY"),
    ("document.querySelector('[data-testid=posting_settings_visibility]').textContent='Только друзья'",
     "VK_BROWSER_VISIBILITY_UNEXPECTED"),
    ("document.querySelector('#preview').insertAdjacentHTML('beforeend','<input type=checkbox checked>')",
     "VK_BROWSER_UNAPPROVED_SETTING"),
    ("document.querySelector('#preview').insertAdjacentHTML('beforeend','<div role=radio aria-checked=true>Другой режим</div>')",
     "VK_BROWSER_UNAPPROVED_SETTING"),
    ("document.querySelector('#preview').insertAdjacentHTML('beforeend','<img src=/photo.png>')",
     "VK_BROWSER_UNAPPROVED_MEDIA"),
    ("document.querySelector('#preview').insertAdjacentHTML('beforeend','<video></video>')",
     "VK_BROWSER_UNAPPROVED_MEDIA"),
])
def test_personal_settings_unapproved_identity_mode_or_media_prevent_click(
        sample, local_vk, page, semantic_fence_clock, monkeypatch, change, code):
    personal_wall(sample, local_vk)
    settings = worker._personal_settings
    changed = False

    def mutated(*args, **kwargs):
        nonlocal changed
        if not changed:
            page.evaluate(change)
            changed = True
        return settings(*args, **kwargs)

    monkeypatch.setattr(worker, "_personal_settings", mutated)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == code, result
    assert local_vk.publishes == local_vk.uploads == local_vk.stories == 0
    assert run(page, sample, local_vk)["state"] == "unknown" and local_vk.publishes == 0


def test_personal_contract_cannot_authorize_a_community_wall(sample, local_vk):
    personal_wall(sample, local_vk)
    sample.snapshot["destination"] = "-456789"
    with pytest.raises(AgentError, match="VK_BROWSER_PERSONAL_WALL_REQUIRED"):
        prepare(sample.root, sample.options, "-456789", sample.snapshot)
    assert not (sample.root / "data").exists()


@pytest.mark.parametrize("href", ["https://unapproved.example.test/", "javascript:alert(1)",
                                  "https://vk.ru/away.php?to=https%3A%2F%2Fscience.example.test%2Fa"])
def test_text_only_preview_unapproved_link_preserving_every_character_never_sends(
        sample, local_vk, page, semantic_fence_clock, monkeypatch, href):
    personal_wall(sample, local_vk)
    sample.snapshot["body"] = "Полный научный текст без внешних ссылок."
    final = worker._final_snapshot

    def linked(*args, **kwargs):
        page.locator("#preview-text").evaluate("""(root,href)=>{
          const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
          const node=walker.nextNode(),a=document.createElement('a');
          a.href=href;a.textContent=node.textContent;node.replaceWith(a);
        }""", href)
        return final(*args, **kwargs)

    monkeypatch.setattr(worker, "_final_snapshot", linked)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_UNAPPROVED_LINK"
    assert local_vk.publishes == local_vk.uploads == local_vk.stories == 0
    assert run(page, sample, local_vk)["state"] == "unknown" and local_vk.publishes == 0


@pytest.mark.parametrize("source", ["literal_body", "frozen_sources"])
def test_approved_text_link_is_allowed_without_broadening_author_or_permalink_scope(
        sample, local_vk, page, semantic_fence_clock, monkeypatch, source):
    personal_wall(sample, local_vk)
    href = "https://science.example.test/a"
    if source == "frozen_sources":
        sample.snapshot["body"] = "Полный научный текст с источником."
        sample.snapshot["sources"] = [{"url": href}]
    final = worker._final_snapshot

    def approved_link(*args, **kwargs):
        page.locator("#preview-text").evaluate("""(root,href)=>{
          if(root.querySelector('a'))return;
          const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),node=walker.nextNode();
          const a=document.createElement('a');a.href=href;a.textContent=node.textContent;node.replaceWith(a);
        }""", href)
        return final(*args, **kwargs)

    monkeypatch.setattr(worker, "_final_snapshot", approved_link)
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert result["verified"] is False and local_vk.publishes == 1 and local_vk.stories == 0
    assert all(item["text_links"] == [] for item in result["evidence"]["proof"]["reloads"])


def test_unapproved_native_post_link_is_unknown_with_known_id_and_no_resend(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)
    local_vk.post_body_link_href = "https://unapproved.example.test/"
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["external_id"] == OWNER + "_51"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_UNAPPROVED_LINK"
    assert local_vk.publishes == 1 and local_vk.stories == 0
    assert run(page, sample, local_vk)["state"] == "unknown" and local_vk.publishes == 1
    del local_vk.post_body_link_href
    current = worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"],
                               origin=local_vk.origin, fixture=True)
    assert current["state"] == "confirmed" and current["verified"] is False
    assert local_vk.publishes == 1


def test_personal_story_revoke_after_durable_intent_never_unchecks_or_publishes(
        sample, local_vk, page, semantic_fence_clock, monkeypatch):
    personal_wall(sample, local_vk)
    save = worker._save
    revoked = False

    def revoke_after_claim(path, journal, state, **fields):
        nonlocal revoked
        result = save(path, journal, state, **fields)
        if state == "story_disable_intent":
            revoked = True
        return result

    def authorize():
        if revoked:
            raise AgentError("TARGET_PAUSED", 409)

    monkeypatch.setattr(worker, "_save", revoke_after_claim)
    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "TARGET_PAUSED"
    assert page.locator("#story").is_checked() and page.evaluate("window.storyChanges") == 0
    assert local_vk.publishes == local_vk.stories == 0
    assert run(page, sample, local_vk, authorize)["state"] == "unknown"


def test_personal_story_reenabled_by_last_authorizer_prevents_click(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)

    def authorize():
        path = worker._path(sample.root, sample.payload["request_id"])
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] == "click_intent":
            page.locator("#story").evaluate("e=>{e.checked=true;e.setAttribute('aria-checked','true')}")

    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_STORY_SHARE_NOT_DISABLED"
    assert local_vk.publishes == local_vk.stories == 0


def test_personal_already_disabled_story_is_never_toggled(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    personal_wall(sample, local_vk)
    settings = worker._personal_settings
    save = worker._save
    states = []

    def remember(path, journal, state, **fields):
        states.append(state)
        return save(path, journal, state, **fields)

    def already_disabled(*args, **kwargs):
        page.locator("#story").evaluate("e=>{e.checked=false;e.setAttribute('aria-checked','false')}")
        return settings(*args, **kwargs)

    monkeypatch.setattr(worker, "_personal_settings", already_disabled)
    monkeypatch.setattr(worker, "_save", remember)
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    journal = json.loads(worker._path(sample.root, sample.payload["request_id"]).read_text(encoding="utf-8"))
    assert journal["personal_settings"]["story_checked"] is False
    assert "story_disable_intent" not in states
    assert local_vk.publishes == 1 and local_vk.stories == 0


def test_personal_story_refuses_to_disable_never_clicks_or_retries(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    personal_wall(sample, local_vk)
    settings = worker._personal_settings
    installed = False

    def sticky(*args, **kwargs):
        nonlocal installed
        if not installed:
            page.locator("#story").evaluate("e=>e.onchange=()=>{e.checked=true;e.setAttribute('aria-checked','true')}")
            installed = True
        return settings(*args, **kwargs)

    monkeypatch.setattr(worker, "_personal_settings", sticky)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and local_vk.publishes == local_vk.stories == 0
    assert run(page, sample, local_vk)["state"] == "unknown"


@pytest.mark.parametrize("phase,change,code", [
    ("fill_intent", "document.querySelector('#editor').textContent='Чужой черновик'",
     "VK_BROWSER_EDITOR_NOT_EMPTY"),
    ("fill_intent", "document.querySelector('[data-testid=header-profile-menu-button]').href='/id999'",
     "VK_BROWSER_ACCOUNT_MISMATCH"),
    ("story_disable_intent", "document.querySelector('[data-testid=header-profile-menu-button]').href='/id999'",
     "VK_BROWSER_ACCOUNT_MISMATCH"),
])
def test_personal_last_authorizer_mutation_never_changes_wrong_editor_or_story(
        sample, local_vk, page, semantic_fence_clock, phase, change, code):
    personal_wall(sample, local_vk)

    def authorize():
        path = worker._path(sample.root, sample.payload["request_id"])
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] == phase:
            page.evaluate(change)

    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == code, result
    assert local_vk.publishes == local_vk.stories == local_vk.uploads == 0
    assert page.evaluate("window.storyChanges") == 0


@pytest.mark.parametrize("markup,expected", [
    ('<div>Заголовок<div><br> </div>Текст</div>', "Заголовок\n\nТекст"),
    ('<div>Заголовок<br><br><br>Текст</div>', "Заголовок\n\n\nТекст"),
])
def test_native_spacing_parser_preserves_exact_known_paragraph_boundary(page, markup, expected):
    page.set_content('<span id="text">' + markup + '</span>')
    assert worker._rendered_text(page.locator("#text"), {"text_contract": "vk-spacer-div-v1"}) == expected


@pytest.mark.parametrize("markup", ['<div>Текст<b>другой</b></div>',
    '<div>Текст<span style="display:none">скрытый</span></div>',
    '<div>First</div><div>Second</div>', '<div><br style="display:none"></div>',
    '<span style="display:block">First</span><span style="display:block">Second</span>'])
def test_native_spacing_parser_rejects_unproven_or_hidden_content(page, markup):
    page.set_content('<span id="text">' + markup + '</span>')
    with pytest.raises(AgentError, match="VK_BROWSER_RENDERED_TEXT_UNSUPPORTED"):
        worker._rendered_text(page.locator("#text"), {"text_contract": "vk-spacer-div-v1"})


@pytest.mark.parametrize("markup", ['<div>Новая </div><div>звезда</div>',
                                  '<div>Новая звезда<div><br style="display:none"></div></div>'])
def test_unproven_native_block_structure_never_clicks_publish(sample, local_vk, page, semantic_fence_clock, monkeypatch, markup):
    personal_wall(sample, local_vk)
    final = worker._final_snapshot

    def unproven(*args, **kwargs):
        page.locator("#preview-text").evaluate("(e,markup)=>e.innerHTML=markup", markup)
        return final(*args, **kwargs)

    monkeypatch.setattr(worker, "_final_snapshot", unproven)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_RENDERED_TEXT_UNSUPPORTED"
    assert local_vk.publishes == local_vk.stories == 0


def test_native_post_author_single_tracking_query_preserves_numeric_identity(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)
    local_vk.post_author_tracking = "?trackcode=Native_Abc-123"
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert all(item["author"]["href"] == "/id" + OWNER + local_vk.post_author_tracking
               for item in result["evidence"]["proof"]["reloads"])
    assert local_vk.publishes == 1 and local_vk.stories == 0


@pytest.mark.parametrize("href", ["javascript:/id123456", "data:/id123456", "https:/id123456",
    "//vk.ru/id123456", "https://evil.example/id123456", "/id123456?trackcode=",
    "/id123456?trackcode=ok&trackcode=other", "/id123456?trackcode=ok&owner=999",
    "/id123456?trackcode=ab%63", "/id123456?trackcode=ok#fragment", "id123456"])
def test_native_links_reject_noncanonical_schemes_or_author_query(href):
    with pytest.raises(AgentError):
        worker._link_path(href, "https://vk.ru", author_tracking=True)


def test_author_tracking_exception_never_applies_to_account_or_recipient_links():
    assert worker._link_path("/id123456?trackcode=Abc_123-", "https://vk.ru",
                             author_tracking=True) == "/id123456"
    with pytest.raises(AgentError, match="VK_BROWSER_LINK_INVALID"):
        worker._link_path("/id123456?trackcode=Abc_123-", "https://vk.ru")


def test_default_contract_cannot_publish_and_does_not_fabricate_final_selectors(sample):
    assert "recipient" not in OBSERVED_DOM and "preview_author" not in OBSERVED_DOM
    assert OBSERVED_DOM["publish"] == '[data-testid="posting_submit_button"]'
    for options in ({"owner": OWNER}, {"owner": OWNER, "dom_contract_verified": True}):
        adapter = VKBrowser(sample.root, options)
        with pytest.raises(AgentError, match="VK_BROWSER_DOM_CONTRACT_UNVERIFIED"):
            adapter.validate(OWNER, sample.snapshot)
    assert not (sample.root / "data").exists()


def test_validate_is_local_and_preserves_plain_full_text(sample):
    original = json.loads(json.dumps(sample.snapshot))
    result = VKBrowser(sample.root, sample.options).validate(OWNER, sample.snapshot)
    assert result["preflight_only"] and result["live_verified"] is False
    assert result["title_format"] == "plain" and result["text"] == "Новая звезда\n\n" + original["body"]
    assert sample.snapshot == original and not (sample.root / "data").exists()


@pytest.mark.parametrize("destination,valid", [(OWNER, True), ("-456789", True), ("123", False),
    ("0", False), ("@name", False), ("https://vk.ru/id123456", False), ("../123", False)])
def test_numeric_owner_and_wall_binding_only(sample, destination, valid):
    if valid:
        assert wall_url(destination, sample.options).startswith("https://vk.ru/")
    else:
        with pytest.raises(AgentError):
            wall_url(destination, sample.options)


def test_full_native_fixture_upload_next_single_click_two_reload_and_replay(sample, local_vk, page, semantic_fence_clock):
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert result["verified"] is False and result["evidence"]["fixture"] is True
    assert result["external_id"] == OWNER + "_51"
    assert result["evidence"]["proof"]["verification"].startswith("independent_wall_reload")
    assert len(result["evidence"]["proof"]["reloads"]) == 2
    assert local_vk.uploads == local_vk.publishes == 1
    before = list(local_vk.gets)
    repeated = run(page, sample, local_vk)
    assert repeated == result and local_vk.gets == before
    assert local_vk.uploads == local_vk.publishes == 1


def test_wrong_owner_fails_before_editor_or_upload(sample, local_vk, page, semantic_fence_clock):
    local_vk.wrong_owner = True
    result = run(page, sample, local_vk)
    assert result["state"] == "failed" and result["evidence"]["failure_code"] == "VK_BROWSER_ACCOUNT_MISMATCH"
    assert local_vk.uploads == local_vk.publishes == 0


def test_lost_publish_reply_is_unknown_and_never_reuploads_or_reclicks(sample, local_vk, page, semantic_fence_clock):
    local_vk.drop_reply = True
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown", result
    assert local_vk.publishes == local_vk.uploads == 1
    assert run(page, sample, local_vk)["state"] == "unknown"
    assert local_vk.publishes == local_vk.uploads == 1


def test_wrong_recipient_after_next_never_clicks_publish(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    final = worker._final_snapshot

    def wrong_recipient(*args, **kwargs):
        page.locator("#recipient").evaluate("e=>e.setAttribute('data-owner-id','-999')")
        return final(*args, **kwargs)

    monkeypatch.setattr(worker, "_final_snapshot", wrong_recipient)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_RECIPIENT_MISMATCH"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_pause_after_image_get_prevents_send(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    image_read = worker._image_proof
    paused = []

    def read_then_pause(*args, **kwargs):
        result = image_read(*args, **kwargs)
        paused.append(True)
        return result

    def authorize():
        if paused:
            raise AgentError("TARGET_PAUSED", 409)

    monkeypatch.setattr(worker, "_image_proof", read_then_pause)
    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "TARGET_PAUSED"
    assert local_vk.uploads == 1 and local_vk.publishes == 0
    assert run(page, sample, local_vk, authorize)["state"] == "unknown"
    assert local_vk.uploads == 1


def test_dom_change_during_image_get_prevents_send(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    image_read = worker._image_proof

    def mutate_after_read(*args, **kwargs):
        result = image_read(*args, **kwargs)
        page.locator("#preview-text").evaluate("e=>e.textContent='Неодобренный текст'")
        return result

    monkeypatch.setattr(worker, "_image_proof", mutate_after_read)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and local_vk.publishes == 0
    assert result["evidence"]["failure_code"] == "VK_BROWSER_PREVIEW_TEXT_MISMATCH"


def test_changed_snapshot_cannot_reuse_durable_request(sample, local_vk, page, semantic_fence_clock):
    assert run(page, sample, local_vk)["state"] == "confirmed"
    sample.snapshot["body"] += " Новое содержание."
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_CONFLICT"):
        run(page, sample, local_vk)
    assert local_vk.uploads == local_vk.publishes == 1


def test_fixture_journal_cannot_be_promoted_to_live_verified(sample, local_vk, page, semantic_fence_clock):
    assert run(page, sample, local_vk)["state"] == "confirmed"
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_CONFLICT"):
        worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"])


def test_image_changed_after_approval_fails_before_browser_mutation(sample, local_vk, page, semantic_fence_clock):
    (sample.root / "approved.png").write_bytes(b"changed")
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_HASH_CHANGED"):
        run(page, sample, local_vk)
    assert local_vk.uploads == local_vk.publishes == 0


def test_missing_journal_claim_never_reopens_existing_request(sample, local_vk, page, semantic_fence_clock):
    path = worker._path(sample.root, sample.payload["request_id"])
    path.parent.mkdir(parents=True)
    path.write_text("{partial", encoding="utf-8")
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_INVALID"):
        run(page, sample, local_vk)
    assert local_vk.uploads == local_vk.publishes == 0


def test_readback_mismatch_keeps_known_native_id_for_read_only_reconciliation(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    readback = worker._readback

    def mismatch(*args, **kwargs):
        local_vk.corrupt_readback = True
        return readback(*args, **kwargs)

    monkeypatch.setattr(worker, "_readback", mismatch)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["external_id"] == OWNER + "_51"
    local_vk.corrupt_readback = False
    monkeypatch.setattr(worker, "_readback", readback)
    result = worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"],
                              origin=local_vk.origin, fixture=True)
    assert result["state"] == "confirmed" and result["verified"] is False
    assert local_vk.uploads == local_vk.publishes == 1


def test_community_wall_keeps_authenticated_account_separate_from_recipient(sample, local_vk, page, semantic_fence_clock):
    local_vk.destination = "-456789"
    sample.snapshot["destination"] = sample.payload["destination"] = local_vk.destination
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed" and result["external_id"] == "-456789_51"
    assert local_vk.uploads == local_vk.publishes == 1


def test_plain_text_only_is_one_native_post_without_upload(sample, local_vk, page, semantic_fence_clock):
    sample.snapshot["media"] = []
    sample.options["media_contract_verified"] = False
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed" and local_vk.publishes == 1 and local_vk.uploads == 0


def test_full_long_article_and_unicode_title_survive_native_flow_without_truncation(sample, local_vk, page, semantic_fence_clock):
    sample.snapshot["title"] = "🔭 Космическое открытие"
    sample.snapshot["body"] = "Полный научный текст с точными наблюдениями. " * 60 + "\n\nИсточник: https://science.example.test/a"
    expected = sample.snapshot["title"] + "\n\n" + sample.snapshot["body"]
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed" and local_vk.posts == [(expected, 51)], publish_diagnostics(sample, local_vk, result)
    assert len(expected) > 2500 and result["evidence"]["proof"]["title_format"] == "plain"


@pytest.mark.parametrize("long_article", [False, True])
def test_preview_photo_decode_is_awaited_within_existing_budget_before_one_publish(
        sample, local_vk, page, semantic_fence_clock, long_article):
    local_vk.preview_photo_delay_ms = 400
    if long_article:
        sample.snapshot["title"] = "🔭 Космическое открытие"
        sample.snapshot["body"] = "Полный научный текст с точными наблюдениями. " * 60
    expected = sample.snapshot["title"] + "\n\n" + sample.snapshot["body"]
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", publish_diagnostics(sample, local_vk, result)
    assert result["external_id"] == OWNER + "_51" and result["verified"] is False
    assert len(result["evidence"]["proof"]["reloads"]) == 2
    assert local_vk.posts == [(expected, 51)] and local_vk.uploads == local_vk.publishes == 1
    assert local_vk.preview_completions and all(done.wait(2) for done in local_vk.preview_completions)
    assert sample.options["operation_timeout_seconds"] == 1


def test_permanently_undecodable_preview_blocks_publish_and_never_reopens_unknown(sample, local_vk, page, semantic_fence_clock):
    local_vk.preview_photo_invalid = True
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown", publish_diagnostics(sample, local_vk, result)
    assert result["evidence"]["failure_code"] == "VK_BROWSER_IMAGE_NOT_READY"
    assert result["external_id"] is None and result["evidence"]["clicks"] == 0
    assert local_vk.uploads == 1 and local_vk.publishes == 0 and not local_vk.publish_readiness
    before = list(local_vk.gets)
    repeated = run(page, sample, local_vk)
    assert repeated["state"] == "unknown" and repeated["external_id"] is None
    assert local_vk.gets == before and local_vk.uploads == 1 and local_vk.publishes == 0
    assert sample.options["operation_timeout_seconds"] == 1


@pytest.mark.parametrize("change,code", [
    ("account", "VK_BROWSER_ACCOUNT_MISMATCH"),
    ("text", "VK_BROWSER_PREVIEW_TEXT_MISMATCH"),
    ("image", "VK_BROWSER_IMAGE_READBACK_MISMATCH"),
])
def test_preview_ready_wait_does_not_approve_changed_account_text_or_photo(
        sample, local_vk, page, semantic_fence_clock, change, code):
    local_vk.preview_photo_delay_ms = 400
    local_vk.preview_wait_change = change
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "red").save(output, format="PNG")
    local_vk.changed_preview_raw = output.getvalue()
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown", publish_diagnostics(sample, local_vk, result)
    assert result["evidence"]["failure_code"] == code, publish_diagnostics(sample, local_vk, result)
    assert result["external_id"] is None and result["evidence"]["clicks"] == 0
    assert local_vk.uploads == 1 and local_vk.publishes == 0 and not local_vk.publish_readiness
    assert all(done.wait(2) for done in local_vk.preview_completions)
    before = list(local_vk.gets)
    assert run(page, sample, local_vk)["state"] == "unknown"
    assert local_vk.gets == before and local_vk.uploads == 1 and local_vk.publishes == 0


def test_wrong_wall_redirect_is_blocked_before_editor_mutation(sample, local_vk, page, semantic_fence_clock):
    local_vk.redirect_wrong_wall = True
    result = run(page, sample, local_vk)
    assert result["state"] == "failed" and result["evidence"]["failure_code"] == "VK_BROWSER_WALL_MISMATCH"
    assert local_vk.uploads == local_vk.publishes == 0


def test_image_bytes_must_match_frozen_sha_before_publish(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    image_read = worker._image_proof

    def wrong_bytes(*args, **kwargs):
        local_vk.bad_photo = True
        return image_read(*args, **kwargs)

    monkeypatch.setattr(worker, "_image_proof", wrong_bytes)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_IMAGE_READBACK_MISMATCH"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_lease_loss_during_durable_click_claim_prevents_click(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    save = worker._save
    expired = []

    def persist_then_expire(path, journal, state, **fields):
        save(path, journal, state, **fields)
        if state == "click_intent":
            expired.append(True)

    def authorize():
        if expired:
            raise AgentError("VK_BROWSER_DISPATCH_NOT_APPROVED", 403)

    monkeypatch.setattr(worker, "_save", persist_then_expire)
    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_DISPATCH_NOT_APPROVED"
    assert local_vk.uploads == 1 and local_vk.publishes == 0
    assert run(page, sample, local_vk, authorize)["state"] == "unknown"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_read_only_inspection_does_not_open_composer_or_upload(sample, local_vk, page):
    result = worker.inspect_page(page, sample.options, OWNER, origin=local_vk.origin, fixture=True)
    assert result["state"] == "access_only" and result["live_verified"] is False
    assert result["publish"] is True and result["external_mutations"] == 0
    assert page.evaluate("window.opens") == 0 and local_vk.uploads == local_vk.publishes == 0


def test_inspection_waits_for_native_spa_editor_after_account_header(sample, local_vk, page, monkeypatch):
    ready = worker._ready
    observed = []

    def shell_ready(*args):
        ready(*args)
        observed.append(page.evaluate("""() => {
          const button=document.querySelector('[data-testid="posting_create_post_button"]');
          button.remove();setTimeout(()=>document.getElementById('wall-ready').prepend(button),350);
          return document.querySelectorAll('[data-testid="posting_create_post_button"]').length;
        }"""))

    monkeypatch.setattr(worker, "_ready", shell_ready)
    result = worker.inspect_page(page, sample.options, OWNER, origin=local_vk.origin, fixture=True)
    assert observed == [0]
    assert result["editor_ready"] is True and result["publish"] is True
    assert result["create_button_count"] == 1 and result["blockers"] == []
    assert page.evaluate("window.opens") == 0 and local_vk.uploads == local_vk.publishes == 0


def test_publication_waits_for_native_spa_editor(sample, local_vk, page, semantic_fence_clock):
    local_vk.create_delay_ms = 350
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert local_vk.publishes == local_vk.uploads == 1


@pytest.mark.parametrize("transitions,next_mode", [
    (("editor",), "disabled"),
    (("next",), "absent"),
    (("next",), "hidden"),
    (("next",), "disabled"),
    (("story",), "disabled"),
    (("editor", "next", "story"), "absent"),
])
def test_personal_spa_transitions_wait_before_one_currently_authorized_publish(
        sample, local_vk, page, semantic_fence_clock, transitions, next_mode):
    personal_wall(sample, local_vk)
    for name in transitions:
        setattr(local_vk, {"editor": "editor_mount_delay_ms", "next": "next_ready_delay_ms",
                          "story": "story_aria_delay_ms"}[name], 300)
    local_vk.next_delay_mode = next_mode
    final_authorizations = []
    journal = worker._path(sample.root, sample.payload["request_id"])

    def authorize():
        if journal.exists() and json.loads(journal.read_text(encoding="utf-8"))["state"] == "click_intent":
            state = page.evaluate("""() => ({waiting:window.spaWaiting,revoked:window.spaRevoked,
              ready:window.spaEvents,story:document.getElementById('story').checked,
              aria:document.getElementById('story').getAttribute('aria-checked'),nexts:window.nexts})""")
            assert state["waiting"] is None and state["revoked"] is False
            assert state["story"] is False and state["aria"] == "false" and state["nexts"] == 1
            assert all(name + "_ready" in state["ready"] for name in transitions)
            final_authorizations.append(state)

    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "confirmed", result
    assert result["verified"] is False and result["evidence"]["fixture"] is True
    assert final_authorizations, "The publish action needs fresh authorization after every SPA wait"
    assert local_vk.publishes == 1 and local_vk.uploads == local_vk.stories == 0
    assert local_vk.posts[0][0] == prepare(sample.root, sample.options, OWNER, sample.snapshot)["text"]
    assert len(local_vk.publish_readiness) == 1
    receipt = local_vk.publish_readiness[0]
    assert receipt["waiting"] is None and receipt["revoked"] is False
    assert all(name + "_ready" in receipt["ready"] for name in transitions)
    before = list(local_vk.gets)
    assert run(page, sample, local_vk, authorize) == result
    assert local_vk.gets == before and local_vk.publishes == 1


@pytest.mark.parametrize("transition", ["editor", "next", "story"])
def test_pause_during_personal_spa_wait_prevents_publish_and_repeat(
        sample, local_vk, page, semantic_fence_clock, transition):
    personal_wall(sample, local_vk)
    setattr(local_vk, {"editor": "editor_mount_delay_ms", "next": "next_ready_delay_ms",
                      "story": "story_aria_delay_ms"}[transition], 300)
    local_vk.next_delay_mode = "absent"
    local_vk.pause_transition = transition
    rejected = []

    def authorize():
        if page.evaluate("window.spaRevoked===true"):
            rejected.append(True)
            raise AgentError("EDITORIAL_PAUSED", 409)

    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown", result
    assert result["evidence"]["failure_code"] == "EDITORIAL_PAUSED" and rejected
    assert local_vk.publishes == local_vk.uploads == local_vk.stories == 0
    events = page.evaluate("window.spaEvents")
    assert events.index(transition + "_paused") < events.index(transition + "_ready")
    assert not local_vk.publish_readiness
    before = list(local_vk.gets)
    repeated = run(page, sample, local_vk, authorize)
    assert repeated["state"] == "unknown" and repeated["evidence"]["failure_code"] == "EDITORIAL_PAUSED"
    assert local_vk.gets == before and local_vk.publishes == 0


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_inspection_waits_for_account_href_hydration_without_accepting_wrong_owner(
        sample, local_vk, page, wrong_owner):
    local_vk.account_href_delay_ms = 700
    local_vk.wrong_owner = wrong_owner
    if wrong_owner:
        with pytest.raises(AgentError, match="VK_BROWSER_ACCOUNT_MISMATCH"):
            worker.inspect_page(page, sample.options, OWNER, origin=local_vk.origin, fixture=True)
    else:
        result = worker.inspect_page(page, sample.options, OWNER, origin=local_vk.origin, fixture=True)
        assert result["editor_ready"] is True
    assert page.evaluate("window.opens") == 0
    assert local_vk.publishes == local_vk.uploads == 0


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_login_feed_redirect_returns_only_proven_owner_to_personal_wall(sample, local_vk, page, wrong_owner):
    local_vk.wrong_owner = wrong_owner
    page.goto(local_vk.origin + "/feed")
    plan = {"wall_url": wall_url(OWNER, sample.options), "owner": OWNER}
    result = worker._restore_login_wall(page, plan, dom_contract(sample.options), origin=local_vk.origin)
    assert result is not wrong_owner
    assert page.url == local_vk.origin + ("/feed" if wrong_owner else "/id" + OWNER)
    assert worker._restore_login_wall(page, plan, dom_contract(sample.options), origin=local_vk.origin) is False
    assert page.evaluate("window.opens") == 0 and local_vk.uploads == local_vk.publishes == 0


def test_personal_text_profile_provides_a_complete_opt_in_contract(sample):
    options = {**sample.options, "dom_contract": {}, "dom_contract_profile": "personal-text-v1"}
    snapshot = {**sample.snapshot, "media": [], "body": "Полный согласованный текст без вложений."}
    result = VKBrowser(sample.root, options).validate(OWNER, snapshot)
    assert result["preflight_only"] is True and result["media"] == []
    with pytest.raises(AgentError, match="VK_BROWSER_PERSONAL_WALL_REQUIRED"):
        prepare(sample.root, options, "-" + OWNER, {**snapshot, "destination": "-" + OWNER})


def test_personal_text_profile_does_not_enable_media_or_unverified_dom(sample):
    options = {**sample.options, "dom_contract": {}, "dom_contract_profile": "personal-text-v1"}
    with pytest.raises(AgentError, match="VK_BROWSER_TEXT_PROFILE_MEDIA_UNSUPPORTED"):
        prepare(sample.root, options, OWNER, sample.snapshot)
    options["dom_contract_verified"] = False
    with pytest.raises(AgentError, match="VK_BROWSER_DOM_CONTRACT_UNVERIFIED"):
        prepare(sample.root, options, OWNER, {**sample.snapshot, "media": []})
    with pytest.raises(AgentError, match="VK_BROWSER_DOM_PROFILE_UNSUPPORTED"):
        dom_contract({"dom_contract_profile": "unknown"})


def test_pause_during_final_dom_guard_is_checked_before_native_publish(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    final = worker._final_snapshot
    revoked = False
    journal = worker._path(sample.root, sample.payload["request_id"])

    def final_guard(*args, **kwargs):
        nonlocal revoked
        result = final(*args, **kwargs)
        if journal.exists() and json.loads(journal.read_text(encoding="utf-8"))["state"] == "click_intent":
            revoked = True
        return result

    def authorize():
        if revoked:
            raise AgentError("VK_BROWSER_DISPATCH_NOT_APPROVED")

    monkeypatch.setattr(worker, "_final_snapshot", final_guard)
    result = run(page, sample, local_vk, authorize)
    assert revoked is True
    assert result["state"] == "unknown"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_DISPATCH_NOT_APPROVED"
    assert local_vk.publishes == 0


def test_slow_fresh_authorization_expires_before_native_publish(sample, local_vk, page):
    journal = worker._path(sample.root, sample.payload["request_id"])

    def authorize():
        if journal.exists() and json.loads(journal.read_text(encoding="utf-8"))["state"] == "click_intent":
            time.sleep(1.1)

    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_ACTION_AUTHORIZATION_EXPIRED"
    assert local_vk.publishes == 0


@pytest.mark.parametrize("phase,elapsed", [("authorize", 1.0), ("authorize", 1.25), ("final_stamp", 0.9995)])
def test_injected_authorization_clock_expiry_prevents_native_action(monkeypatch, phase, elapsed):
    current, actions = [10.0], []
    stamps = []

    def stamp(*_):
        stamps.append(True)
        if phase == "final_stamp" and len(stamps) == 3:
            current[0] += elapsed
        return "same-state"

    monkeypatch.setattr(worker, "_mutation_stamp", stamp)
    monkeypatch.setattr(worker, "_context", lambda *_a, **_k: None)

    def authorize():
        actions.append("authorize")
        if phase == "authorize":
            current[0] += elapsed

    with pytest.raises(AgentError, match="VK_BROWSER_ACTION_AUTHORIZATION_EXPIRED"):
        worker._mutate(None, {}, {}, authorize, lambda: actions.append("check"),
            lambda timeout: actions.append("native"), origin="https://vk.ru", _clock=lambda: current[0])
    assert actions == ["check", "authorize"]
    assert len(stamps) == (3 if phase == "final_stamp" else 2)


def test_native_story_label_is_clicked_without_forcing_hidden_input(sample, local_vk, page, semantic_fence_clock):
    personal_wall(sample, local_vk)
    local_vk.native_story_label = True
    sample.options["dom_contract"]["story_toggle_label"] = "#native-story-label"
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert local_vk.publishes == 1 and local_vk.stories == 0
    assert result["evidence"]["sender_settings"]["story_checked"] is False


@pytest.mark.parametrize("kind", ["preview", "post"])
def test_native_v2_rendering_accepts_only_observed_inline_unary_wrappers(page, kind):
    content = 'Заголовок<div style="display:block;width:0;height:6px"><br> </div>Полный согласованный текст.'
    native = '<div style="display:inline">' + content + '</div>'
    if kind == "preview":
        markup = '<div id="native" data-testid="showmoretext-in" style="display:flow-root;overflow:hidden;max-height:38px">' + native + '</div>'
    else:
        markup = '<span id="native" data-testid="post_text" style="display:block"><div><div data-testid="showmoretext-in-expanded" style="display:inline">' + native + '</div></div></span>'
    page.set_content(markup)
    selectors = {"text_contract": "vk-native-text-v2"}
    assert worker._rendered_text(page.locator("#native"), selectors) == "Заголовок\n\nПолный согласованный текст."
    with pytest.raises(AgentError, match="VK_BROWSER_RENDERED_TEXT_UNSUPPORTED"):
        worker._rendered_text(page.locator("#native"), {"text_contract": "vk-spacer-div-v1"})


@pytest.mark.parametrize("markup", [
    '<div id="native"><div style="display:inline">Подмена</div></div>',
    '<div id="native" data-testid="showmoretext-in"><div style="display:inline">Первая<div>Другая граница</div></div></div>',
    '<div id="native" data-testid="showmoretext-in"><div style="display:inline">Текст<div><br style="opacity:0"></div></div></div>',
])
def test_native_v2_does_not_flatten_unknown_blocks_or_hidden_breaks(page, markup):
    page.set_content(markup)
    with pytest.raises(AgentError, match="VK_BROWSER_RENDERED_TEXT_UNSUPPORTED"):
        worker._rendered_text(page.locator("#native"), {"text_contract": "vk-native-text-v2"})


def test_context_is_validated_between_atomic_stamps_before_authorization(monkeypatch):
    current = {"owner": "approved"}
    actions = []

    def stamp(*args):
        current["owner"] = "wrong"
        return "wrong-owner-stamp"

    def context(*args, **kwargs):
        if current["owner"] != "approved":
            raise AgentError("VK_BROWSER_ACCOUNT_MISMATCH")

    monkeypatch.setattr(worker, "_mutation_stamp", stamp)
    monkeypatch.setattr(worker, "_context", context)
    with pytest.raises(AgentError, match="VK_BROWSER_ACCOUNT_MISMATCH"):
        worker._mutate(None, {}, {}, lambda: actions.append("authorize"), lambda: None,
                       lambda timeout: actions.append("native"), origin="https://vk.ru")
    assert actions == []


def test_native_actions_do_not_issue_unbounded_locator_queries_after_sql_fence(sample, local_vk, page, monkeypatch):
    original_mutate, original_one = worker._mutate, worker._one
    fenced = False
    late_queries = []
    action_budgets = []

    def mutate(page, plan, selectors, authorize, check, action, *, origin):
        nonlocal fenced

        def fresh():
            nonlocal fenced
            authorize()
            fenced = True

        def timed_action(timeout):
            action_budgets.append(timeout)
            return action(timeout)

        try:
            return original_mutate(page, plan, selectors, fresh, check, timed_action, origin=origin)
        finally:
            fenced = False

    def one(*args):
        if fenced:
            late_queries.append(args[1])
            time.sleep(1.1)
        return original_one(*args)

    monkeypatch.setattr(worker, "_mutate", mutate)
    monkeypatch.setattr(worker, "_one", one)
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert late_queries == []
    assert action_budgets and all(type(value) is int and 1 <= value <= 1000 for value in action_budgets)
    assert local_vk.publishes == 1


@pytest.mark.parametrize("change,code", [
    (lambda value: value["media"][0].update(path="../outside.png"), "VK_BROWSER_PATH_UNSAFE"),
    (lambda value: value["media"][0].update(mime="video/mp4"), "VK_BROWSER_MEDIA_INVALID"),
    (lambda value: value["media"][0].update(sha256="0" * 64), "VK_BROWSER_IMAGE_HASH_CHANGED"),
    (lambda value: value.update(media=[{}, {}]), "VK_BROWSER_SINGLE_PHOTO_REQUIRED"),
    (lambda value: value.update(body="a" * 16385), "VK_BROWSER_TEXT_TOO_LONG"),
    (lambda value: value.update(body="\x00"), "VK_BROWSER_TEXT_INVALID"),
])
def test_local_snapshot_gates_never_open_browser(sample, change, code):
    change(sample.snapshot)
    with pytest.raises(AgentError, match=code):
        prepare(sample.root, sample.options, OWNER, sample.snapshot)
    assert not (sample.root / "data").exists()


@pytest.mark.parametrize("missing", ["recipient", "preview_author", "preview_images", "wall_ready", "post_images"])
def test_missing_final_native_contract_never_enables_publication(sample, missing):
    sample.options["dom_contract"].pop(missing)
    with pytest.raises(AgentError, match="VK_BROWSER_DOM_CONTRACT_UNVERIFIED"):
        dom_contract(sample.options, publish=True)


def test_browser_cannot_use_fixture_origin_without_fixture_marker(sample, local_vk, page):
    with pytest.raises(AgentError, match="VK_BROWSER_FIXTURE_ORIGIN_FORBIDDEN"):
        worker.run_publish(page, sample.root, sample.options, sample.payload, lambda: None,
                           origin=local_vk.origin)
    assert local_vk.gets == [] and local_vk.uploads == local_vk.publishes == 0


@pytest.fixture
def leased_vk(four_hub, sample):
    from max_postiz_agent.db import now
    from max_postiz_agent.hub_models import Dispatch
    from test_hub import approve, variants

    core = four_hub
    options = {**sample.options, "operation_timeout_seconds": 30}
    target = next(row for row in core.publishing["targets"] if row["id"] == "vk")
    target.update(route="vk_browser", destination=OWNER, options=options)
    (core.settings.root / "config/publishing.json").write_text(json.dumps(core.publishing), encoding="utf-8")
    core.sync_targets()
    core.pause_target("vk", False)
    core.routes.injected["vk"] = VKBrowser(core.settings.root, options)
    dispatch_id = approve(core, variants(core)["vk"])
    with core.db.session.begin() as session:
        row = session.get(Dispatch, dispatch_id)
        row.state, row.owner, row.lease_until = "sending", "vk-fixture-worker", now() + 120
        payload = {"request_id": row.id, "destination": OWNER, "snapshot": row.snapshot}
    return core, options, payload


def test_database_authorizer_requires_exact_active_sending_dispatch(leased_vk):
    core, options, payload = leased_vk
    assert worker.authorize(core.settings.root, payload, options) == "vk-fixture-worker"


@pytest.mark.parametrize("change", ["app", "env"])
def test_persistent_browser_authority_rejects_runtime_rotation_between_callbacks(leased_vk, change):
    core, options, payload = leased_vk
    authority = worker._authority_core(core.settings.root)
    try:
        assert worker.authorize(core.settings.root, payload, options, authority_core=authority) == "vk-fixture-worker"
        if change == "app":
            path = core.settings.root / "config/app.json"
            app = json.loads(path.read_text(encoding="utf-8"))
            app["api"]["port"] += 1
            path.write_text(json.dumps(app), encoding="utf-8")
        else:
            (core.settings.root / ".env.local").write_text("TEST_RUNTIME_GENERATION=rotated\n", encoding="utf-8")
        with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
            worker.authorize(core.settings.root, payload, options, authority_core=authority,
                             expected_lease_owner="vk-fixture-worker")
    finally:
        authority.close()
        authority.db.engine.dispose()


def test_child_rejects_changed_parent_runtime_before_database_or_browser(sample, monkeypatch, capsys):
    calls = []
    fake = SimpleNamespace(route_runtime_generation=lambda: "a" * 64,
        close=lambda: calls.append("close"),
        db=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: calls.append("dispose"))))
    monkeypatch.setattr(worker, "_authority_core", lambda root: fake)
    monkeypatch.setattr(worker, "target_options", lambda root, destination: sample.options)
    monkeypatch.setattr(worker, "authorize", lambda *args, **kwargs: pytest.fail("DB must not be queried"))
    monkeypatch.setattr(worker, "session", lambda *args, **kwargs: pytest.fail("browser must not be opened"))
    payload = {**sample.payload, "options_sha256": fingerprint(sample.options), "runtime_generation": "b" * 64}
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert worker.main(["--root", str(sample.root)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "VK_BROWSER_RUNTIME_GENERATION_CHANGED"
    assert calls == ["close", "dispose"]


def test_owned_worker_ipc_includes_private_parent_generation(sample, monkeypatch):
    import max_postiz_agent.vk_browser as adapter_module

    observed = []

    def supervisor(command, *, input, timeout):
        payload = json.loads(input)
        observed.append(payload)
        assert command[-2:] == ["--root", str(sample.root)]
        assert timeout == 61
        return subprocess.CompletedProcess(command, 0, json.dumps({"state": "access_only"}), "")

    monkeypatch.setattr(adapter_module, "run_browser_worker", supervisor)
    adapter = VKBrowser(sample.root, sample.options)
    adapter.runtime_generation = "a" * 64
    result = adapter._call("publish", OWNER, snapshot=sample.snapshot, request_id="vk-fixture")
    assert observed[0]["runtime_generation"] == "a" * 64
    assert observed[0]["options_sha256"] == fingerprint(sample.options)
    assert "runtime_generation" not in result


@pytest.mark.parametrize("failure", ["timeout", "cleanup", "runtime", "object_code", "array_code"])
def test_owned_worker_errors_preserve_only_safe_unknown_codes(sample, monkeypatch, failure):
    import max_postiz_agent.vk_browser as adapter_module
    from max_postiz_agent.browser_process import BrowserWorkerError

    def supervisor(command, *, input, timeout):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("PRIVATE_REQUEST", timeout, output="PRIVATE_RESPONSE")
        if failure == "cleanup":
            raise BrowserWorkerError("BROWSER_WORKER_CLEANUP_UNKNOWN")
        if failure in {"object_code", "array_code"}:
            return subprocess.CompletedProcess(command, 1, json.dumps({"code": {} if failure == "object_code" else []}), "")
        return subprocess.CompletedProcess(command, 1, json.dumps({"code": "ROUTE_RUNTIME_RESTART_REQUIRED"}), "")

    monkeypatch.setattr(adapter_module, "run_browser_worker", supervisor)
    result = VKBrowser(sample.root, sample.options).publish(OWNER, sample.snapshot, "vk-fixture")
    assert result.state == "unknown" and result.verified is False
    assert result.evidence["failure_code"] == (
        "VK_BROWSER_RUNTIME_GENERATION_CHANGED" if failure == "runtime" else "VK_BROWSER_OPERATION_UNKNOWN")
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("damage", ["unknown", "expired", "owner", "pause", "snapshot", "approval", "future", "global_pause"])
def test_database_authorizer_rejects_stale_lease_owner_snapshot_or_pause(leased_vk, damage):
    from max_postiz_agent.db import now
    from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval
    from max_postiz_agent.server_state import ServerState

    core, options, payload = leased_vk
    with core.db.session.begin() as session:
        row = session.get(Dispatch, payload["request_id"])
        if damage == "unknown":
            row.state = "unknown"
        elif damage == "expired":
            row.lease_until = now() + 1
        elif damage == "owner":
            row.owner = "different-worker"
        elif damage == "pause":
            session.get(Target, "vk").paused = True
        elif damage == "snapshot":
            row.snapshot = {**row.snapshot, "body": "Неодобренный текст"}
        elif damage == "approval":
            session.get(TargetApproval, row.approval_id).revoked = True
        elif damage == "future":
            row.due_at = now() + 3600
    if damage == "global_pause":
        ServerState(core).initialize()
    with pytest.raises(AgentError):
        worker.authorize(core.settings.root, payload, options, expected_lease_owner="vk-fixture-worker")


@pytest.mark.parametrize("profile", ["data/max-browser", "data/pikabu-browser", "../source/browser", "config", "data"])
def test_pure_preflight_rejects_shared_or_external_profile(sample, profile):
    sample.options["profile_dir"] = profile
    with pytest.raises(AgentError, match="VK_BROWSER_(PROFILE_INVALID|PATH_UNSAFE)"):
        prepare(sample.root, sample.options, OWNER, sample.snapshot)
    assert not (sample.root / "data").exists()


@pytest.mark.parametrize("field,value", [("platform", "telegram"), ("route", "vk_api")])
def test_snapshot_cannot_change_route_or_platform(sample, field, value):
    sample.snapshot[field] = value
    with pytest.raises(AgentError, match="VK_BROWSER_SNAPSHOT_INVALID"):
        prepare(sample.root, sample.options, OWNER, sample.snapshot)


def test_subprocess_success_without_exact_receipt_is_unknown(sample, monkeypatch):
    adapter = VKBrowser(sample.root, sample.options)
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: {
        "state": "confirmed", "verified": True, "external_id": OWNER + "_51",
        "permalink": "https://vk.ru/wall" + OWNER + "_51"})
    result = adapter.publish(OWNER, sample.snapshot, "request")
    assert result.state == "unknown" and result.verified is False
    assert result.evidence["failure_code"] == "VK_BROWSER_RECEIPT_INVALID"


@pytest.mark.parametrize("action", ["publish", "reconcile"])
@pytest.mark.parametrize("damage", [None, "valid_numeric", "missing_text", "wrong_text", "missing_image", "wrong_image",
    "wrong_second_text", "missing_links", "unapproved_links", "missing_author_href", "wrong_author_href", "proof_url", "title_format",
    "nonfinite_time", "oversized_time", "string_time", "bool_clicks", "fixture_promoted", "fixture_flip",
    "stale_readback", "future_readback", "snapshot", "proof_type"])
def test_adapter_receipt_boundary_requires_both_exact_hashes_and_typed_proof(sample, monkeypatch, action, damage):
    from max_postiz_agent.config import fingerprint
    from max_postiz_agent.vk_browser import CONTRACT

    monkeypatch.setattr("max_postiz_agent.vk_browser.time.time", lambda: 1.0)
    if damage == "valid_numeric":
        sample.options["dom_contract"]["author_id_attribute"] = "data-author-id"
    plan = prepare(sample.root, sample.options, OWNER, sample.snapshot)
    ident, request_id = OWNER + "_51", sample.payload["request_id"]
    permalink = "https://vk.ru/wall" + ident
    reloads = [{"external_id": ident, "text_sha256": plan["text_sha256"],
                "image_sha256": [item["sha256"] for item in plan["media"]],
                "text_links": [],
                "author": {"id": OWNER, "href": "/id" + OWNER, "id_attribute": None,
                           "numeric_id": None}} for _ in range(2)]
    receipt = {"state": "confirmed", "verified": False, "external_id": ident, "permalink": permalink,
        "evidence": {"verification": "browser_readback", "contract": CONTRACT, "fixture": True,
            "independent_readback": True, "request_id": request_id, "snapshot_sha256": plan["snapshot_sha256"],
            "journal_state": "confirmed", "clicks": 1, "proof": {"external_id": ident, "permalink": permalink,
                "verification": "independent_wall_reload_exact_text_recipient_author_image_sha256",
                "title_format": "plain", "observed_at": 1.0, "reloads": reloads}}}
    evidence, proof = receipt["evidence"], receipt["evidence"]["proof"]
    if damage == "valid_numeric":
        for item in reloads:
            item["author"].update(href="/named_profile", id_attribute="data-author-id", numeric_id=OWNER)
    elif damage == "missing_text":
        reloads[0].pop("text_sha256")
    elif damage == "wrong_text":
        reloads[0]["text_sha256"] = "0" * 64
    elif damage == "missing_image":
        reloads[0].pop("image_sha256")
    elif damage == "wrong_image":
        reloads[0]["image_sha256"] = []
    elif damage == "wrong_second_text":
        reloads[1]["text_sha256"] = "0" * 64
    elif damage == "missing_links":
        reloads[0].pop("text_links")
    elif damage == "unapproved_links":
        reloads[1]["text_links"] = ["https://unapproved.example.test/"]
    elif damage == "missing_author_href":
        reloads[1]["author"].pop("href")
    elif damage == "wrong_author_href":
        reloads[1]["author"]["href"] = "javascript:/id" + OWNER
    elif damage == "proof_url":
        proof["permalink"] = "https://vk.ru/wall999_51"
    elif damage == "title_format":
        proof["title_format"] = "bold"
    elif damage == "nonfinite_time":
        proof["observed_at"] = float("nan")
    elif damage == "oversized_time":
        proof["observed_at"] = 10 ** 1000
    elif damage == "string_time":
        proof["observed_at"] = "1.0"
    elif damage == "bool_clicks":
        evidence["clicks"] = True
    elif damage == "fixture_promoted":
        receipt["verified"] = True
    elif damage == "fixture_flip":
        receipt["verified"], evidence["fixture"] = True, False
    elif damage == "stale_readback":
        proof["observed_at"] = 0.5
    elif damage == "future_readback":
        proof["observed_at"] = 10.0
    elif damage == "snapshot":
        evidence["snapshot_sha256"] = "0" * 64
    elif damage == "proof_type":
        evidence["proof"] = []
    journal = {"contract": CONTRACT, "root": str(sample.root.resolve()), "request_id": request_id,
               "destination": OWNER, "snapshot": sample.snapshot, "snapshot_sha256": plan["snapshot_sha256"],
               "options_sha256": fingerprint(sample.options), "fixture": True, "created_at": 0.1,
               "external_id": ident, "permalink": permalink, "proof": evidence["proof"], "clicks": 1}
    path = worker._path(sample.root, request_id)
    path.parent.mkdir(parents=True)
    worker._save(path, journal, "confirmed")
    adapter = VKBrowser(sample.root, sample.options)
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: receipt)
    result = (adapter.publish(OWNER, sample.snapshot, request_id) if action == "publish"
              else adapter.reconcile(OWNER, request_id, snapshot=sample.snapshot))
    assert result.verified is False
    if damage in {None, "valid_numeric"} or damage == "stale_readback" and action == "publish":
        assert result.state == "confirmed" and result.evidence["fixture"] is True
    else:
        assert result.state == "unknown" and result.external_id == ident
        assert result.evidence["failure_code"] == "VK_BROWSER_RECEIPT_INVALID"


@pytest.mark.parametrize("state", [[], {}, None, 1, True])
def test_malformed_worker_state_is_controlled_unknown(sample, monkeypatch, state):
    adapter = VKBrowser(sample.root, sample.options)
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: {"state": state})
    result = adapter.publish(OWNER, sample.snapshot, sample.payload["request_id"])
    assert result.state == "unknown" and result.verified is False


@pytest.mark.parametrize("supplied", [None, "different_db_snapshot"])
def test_reconcile_requires_exact_frozen_database_snapshot_before_worker(sample, monkeypatch, supplied):
    from max_postiz_agent.config import fingerprint
    from max_postiz_agent.vk_browser import CONTRACT

    request_id = sample.payload["request_id"]
    plan = prepare(sample.root, sample.options, OWNER, sample.snapshot)
    journal = {"contract": CONTRACT, "root": str(sample.root.resolve()), "request_id": request_id,
               "destination": OWNER, "snapshot": sample.snapshot, "snapshot_sha256": plan["snapshot_sha256"],
               "options_sha256": fingerprint(sample.options), "fixture": True}
    path = worker._path(sample.root, request_id)
    path.parent.mkdir(parents=True)
    worker._save(path, journal, "confirmed")
    adapter = VKBrowser(sample.root, sample.options)
    calls = []
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: calls.append((args, kwargs)))
    frozen = None if supplied is None else {**sample.snapshot, "body": "Другая согласованная версия"}
    result = adapter.reconcile(OWNER, request_id, snapshot=frozen)
    assert result.state == "unknown" and result.verified is False and not calls
    expected = "VK_BROWSER_RECONCILE_SNAPSHOT_REQUIRED" if supplied is None else "VK_BROWSER_RECONCILE_SNAPSHOT_MISMATCH"
    assert result.evidence["failure_code"] == expected


def test_native_fixture_result_remains_unverified_through_adapter(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    native = run(page, sample, local_vk)
    assert native["state"] == "confirmed"
    adapter = VKBrowser(sample.root, sample.options)
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: native)
    result = adapter.publish(OWNER, sample.snapshot, sample.payload["request_id"])
    assert result.state == "confirmed" and result.verified is False and result.evidence["fixture"] is True


def test_pause_during_upload_claim_prevents_upload_and_never_reopens(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    save = worker._save
    paused = []

    def persist_then_pause(path, journal, state, **fields):
        save(path, journal, state, **fields)
        if state == "upload_intent":
            paused.append(True)

    def authorize():
        if paused:
            raise AgentError("TARGET_PAUSED", 409)

    monkeypatch.setattr(worker, "_save", persist_then_pause)
    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "TARGET_PAUSED"
    before = list(local_vk.gets)
    assert run(page, sample, local_vk, authorize)["state"] == "unknown"
    assert local_vk.uploads == local_vk.publishes == 0 and local_vk.gets == before


def test_click_journal_write_failure_cannot_send_or_retry_upload(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    save = worker._save

    def fail_click_claim(path, journal, state, **fields):
        if state == "click_intent":
            raise OSError("simulated disk failure")
        save(path, journal, state, **fields)

    monkeypatch.setattr(worker, "_save", fail_click_claim)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and local_vk.uploads == 1 and local_vk.publishes == 0
    assert run(page, sample, local_vk)["state"] == "unknown"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_unknown_without_native_post_id_reconcile_does_not_speculate_or_mutate(sample, local_vk, page, semantic_fence_clock):
    local_vk.drop_reply = True
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["external_id"] is None
    before = list(local_vk.gets)
    result = worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"],
                              origin=local_vk.origin, fixture=True)
    assert result["state"] == "unknown" and local_vk.gets == before
    assert local_vk.uploads == local_vk.publishes == 1


@pytest.mark.parametrize("reload_pass", [1, 2])
@pytest.mark.parametrize("change", ["image_url", "image_count", "extra_attachment", "account", "wall_context", "text", "post_recipient", "post_author"])
def test_readback_rechecks_complete_post_after_image_get_on_each_reload(
        sample, local_vk, page, semantic_fence_clock, monkeypatch, change, reload_pass):
    image_read = worker._image_proof
    readback_passes = []

    def read_old_image_then_swap(*args, **kwargs):
        result = image_read(*args, **kwargs)
        posts = page.locator('[data-testid="post"]')
        if posts.count():
            readback_passes.append(True)
            if len(readback_passes) == reload_pass:
                post = posts.first
                if change == "image_url":
                    post.locator(".approved-photo").evaluate(
                        "e=>e.src=location.origin+'/photo.png?unverified-replacement=1'")
                elif change == "image_count":
                    post.locator(".approved-photo").evaluate("e=>e.remove()")
                elif change == "extra_attachment":
                    post.evaluate("""e=>{const extra=document.createElement('div');
                        extra.className='unexpected-post-attachment';extra.textContent='New attachment';
                        e.append(extra);} """)
                elif change == "account":
                    page.locator('[data-testid="header-profile-menu-button"]').evaluate(
                        "e=>e.setAttribute('href','/id999999')")
                elif change == "wall_context":
                    page.evaluate("history.replaceState(null,'','/id999999')")
                elif change == "text":
                    post.locator('[data-testid="post_text"]').evaluate("e=>e.textContent='Changed post'")
                elif change == "post_author":
                    post.locator('[data-testid="post-header-title"]').evaluate(
                        "e=>e.setAttribute('href','/id999999')")
                else:
                    post.locator('[data-testid="post_date_block_preview"]').evaluate(
                        "e=>e.setAttribute('href','/wall-999999_51')")
        return result

    monkeypatch.setattr(worker, "_image_proof", read_old_image_then_swap)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["external_id"] == OWNER + "_51", publish_diagnostics(sample, local_vk, result)
    assert result["evidence"]["proof"] is None
    assert len(readback_passes) == reload_pass
    assert local_vk.uploads == local_vk.publishes == 1
    before = list(local_vk.gets)
    repeated = run(page, sample, local_vk)
    assert repeated["state"] == "unknown" and repeated["verified"] is False
    assert repeated["external_id"] == result["external_id"] and local_vk.gets == before
    assert local_vk.uploads == local_vk.publishes == 1


@pytest.mark.parametrize("community", [False, True])
def test_identical_post_on_correct_wall_from_wrong_author_is_unknown(sample, local_vk, page, semantic_fence_clock, community):
    if community:
        local_vk.destination = "-456789"
        sample.snapshot["destination"] = sample.payload["destination"] = local_vk.destination
        local_vk.post_author_override = OWNER
    else:
        local_vk.post_author_override = "999999"
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["external_id"] == local_vk.destination + "_51"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_AUTHOR_MISMATCH"
    assert run(page, sample, local_vk)["state"] == "unknown"
    assert local_vk.uploads == local_vk.publishes == 1


def test_preview_publishing_identity_is_checked_before_send(sample, local_vk, page, semantic_fence_clock, monkeypatch):
    final = worker._final_snapshot

    def wrong_author(*args, **kwargs):
        page.locator("#preview-author").evaluate("e=>e.setAttribute('href','/id999999')")
        return final(*args, **kwargs)

    monkeypatch.setattr(worker, "_final_snapshot", wrong_author)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_AUTHOR_MISMATCH"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_unproven_community_publishing_identity_cannot_pass_preflight(sample):
    sample.snapshot["destination"] = "-456789"
    sample.options["dom_contract"].pop("preview_author")
    with pytest.raises(AgentError, match="VK_BROWSER_DOM_CONTRACT_UNVERIFIED"):
        prepare(sample.root, sample.options, "-456789", sample.snapshot)


@pytest.mark.parametrize("attribute,href,numeric,valid", [
    (None, "/named_profile", OWNER, False),
    ("data-author-id", "/named_profile", OWNER, True),
    ("data-author-id", "/named_profile", "999999", False),
    ("data-author-id", "/id999999", OWNER, False),
    ("data-author-id", "https://outside.example.test/id123456", OWNER, False),
])
def test_author_numeric_dom_field_requires_explicit_contract_and_consistency(
        sample, page, attribute, href, numeric, valid):
    selectors = dom_contract(sample.options, publish=True)
    if attribute:
        selectors["author_id_attribute"] = attribute
    plan = prepare(sample.root, sample.options, OWNER, sample.snapshot)
    page.set_content('<a id="author" href="' + html.escape(href) + '" data-author-id="'
                     + numeric + '">Visible author</a>')
    if valid:
        assert worker._author(page, "#author", plan, selectors, origin="http://127.0.0.1")["id"] == OWNER
    else:
        with pytest.raises(AgentError):
            worker._author(page, "#author", plan, selectors, origin="http://127.0.0.1")


@pytest.mark.parametrize("title,body,expected", [
    ("Марс", "Марсианская атмосфера меняется.", "Марс\n\nМарсианская атмосфера меняется."),
    ("Марс", "Марс\nАтмосфера меняется.", "Марс\nАтмосфера меняется."),
    ("Марс", "Марс\n\nАтмосфера меняется.", "Марс\n\nАтмосфера меняется."),
    ("Марс", "Марс", "Марс"),
])
def test_title_is_deduplicated_only_at_exact_line_boundary(sample, title, body, expected):
    sample.snapshot.update(title=title, body=body)
    assert prepare(sample.root, sample.options, OWNER, sample.snapshot)["text"] == expected


def test_confirmed_journal_reconciliation_performs_two_new_wall_reads(sample, local_vk, page, semantic_fence_clock):
    initial = run(page, sample, local_vk)
    assert initial["state"] == "confirmed"
    before = local_vk.gets.count("/id" + OWNER)
    observed = initial["evidence"]["proof"]["observed_at"]
    current = worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"],
                               origin=local_vk.origin, fixture=True)
    assert current["state"] == "confirmed" and current["verified"] is False
    assert local_vk.gets.count("/id" + OWNER) == before + 2
    assert current["evidence"]["proof"]["observed_at"] > observed
    assert local_vk.uploads == local_vk.publishes == 1


@pytest.mark.parametrize("change", ["deleted", "text", "author", "photo"])
def test_confirmed_journal_never_substitutes_old_proof_for_changed_live_post(
        sample, local_vk, page, semantic_fence_clock, change):
    initial = run(page, sample, local_vk)
    assert initial["state"] == "confirmed"
    if change == "deleted":
        local_vk.posts.clear()
    elif change == "text":
        local_vk.corrupt_readback = True
    elif change == "author":
        local_vk.post_author_override = "999999"
    else:
        local_vk.bad_photo = True
    before = local_vk.gets.count("/id" + OWNER)
    current = worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"],
                               origin=local_vk.origin, fixture=True)
    assert current["state"] == "unknown" and current["verified"] is False
    assert current["external_id"] == OWNER + "_51"
    assert current["evidence"]["proof"] is None
    assert current["evidence"]["historical_proof"] == initial["evidence"]["proof"]
    assert local_vk.gets.count("/id" + OWNER) > before
    assert local_vk.uploads == local_vk.publishes == 1
    before_all_gets = list(local_vk.gets)
    assert run(page, sample, local_vk)["state"] == "unknown"
    assert local_vk.gets == before_all_gets and local_vk.uploads == local_vk.publishes == 1


@pytest.mark.parametrize("flag", [None, False, "true"])
def test_unverified_image_flow_stops_before_editor_upload_or_journal(sample, local_vk, page, semantic_fence_clock, flag):
    if flag is None:
        sample.options.pop("media_contract_verified")
    else:
        sample.options["media_contract_verified"] = flag
    with pytest.raises(AgentError, match="VK_BROWSER_MEDIA_CONTRACT_UNVERIFIED"):
        run(page, sample, local_vk)
    assert local_vk.gets == [] and local_vk.uploads == local_vk.publishes == 0
    assert not (sample.root / "data").exists()
