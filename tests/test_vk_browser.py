"""VK browser contract against a local native DOM fixture; no real account or external calls."""

import hashlib
import html
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image

from max_postiz_agent import vk_browser_worker as worker
from max_postiz_agent.config import AgentError
from max_postiz_agent.vk_browser import OBSERVED_DOM, VKBrowser, dom_contract, prepare, wall_url

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
def sample(tmp_path):
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
    return SimpleNamespace(root=tmp_path, raw=raw, options=options, snapshot=snapshot, payload=payload)


def post_html(state, text, ident):
    body = html.escape(text + (" changed" if state.corrupt_readback else ""))
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
    posts = "".join(post_html(state, text, ident) for text, ident in state.posts)
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
 const response=await fetch('/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,story})});
 const data=await response.json();
 document.getElementById('posts').innerHTML=data.html;
 document.getElementById('composer').hidden=true;
};
window.clicks=0;window.fills=0;window.opens=0;window.nexts=0;
"""
    personal = getattr(state, "personal", False)
    settings = ('<div data-testid="posting_coowners_cell">Пригласить соавторов&nbsp;</div>'
                '<div data-testid="posting_settings_visibility">Кто увидит этот пост<br>&nbsp;<br>Все<br>&nbsp;</div>'
                '<input id="story" type="checkbox" role="switch" '
                'data-testid="posting_settings_repost_to_story_switch" aria-checked="true" checked '
                'onchange="this.setAttribute(\'aria-checked\',String(this.checked));window.storyChanges++">') if personal else ''
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
                            personal=False, stories=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.gets.append(self.path)
            if state.redirect_wrong_wall and self.path == "/id" + OWNER:
                self.send_response(302)
                self.send_header("Location", "/id999999")
                self.end_headers()
                return
            if self.path.split("?", 1)[0] == "/photo.png":
                raw = b"wrong photo" if state.bad_photo else state.raw
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
            else:
                raw = fixture_html(state)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

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
                state.stories += int(posted.get("story", False))
                state.posts.append((text, 51))
                if state.drop_reply:
                    self.close_connection = True
                    return
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
    return worker.run_publish(page, sample.root, sample.options, sample.payload, authorize,
                              origin=state.origin, fixture=True)


def personal_wall(sample, state):
    state.personal = True
    sample.snapshot["media"] = []
    sample.options["media_contract_verified"] = False
    selectors = sample.options["dom_contract"]
    selectors.pop("recipient")
    selectors.pop("preview_author")
    selectors.update(identity_contract="personal-wall-settings-v1", text_contract="vk-spacer-div-v1")


def test_personal_settings_exact_native_spacing_and_story_disabled_before_one_post(sample, local_vk, page):
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
        sample, local_vk, page, monkeypatch, change, code):
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
        sample, local_vk, page, monkeypatch, href):
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
        sample, local_vk, page, monkeypatch, source):
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


def test_unapproved_native_post_link_is_unknown_with_known_id_and_no_resend(sample, local_vk, page):
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
        sample, local_vk, page, monkeypatch):
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


def test_personal_story_reenabled_by_last_authorizer_prevents_click(sample, local_vk, page):
    personal_wall(sample, local_vk)

    def authorize():
        path = worker._path(sample.root, sample.payload["request_id"])
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] == "click_intent":
            page.locator("#story").evaluate("e=>{e.checked=true;e.setAttribute('aria-checked','true')}")

    result = run(page, sample, local_vk, authorize)
    assert result["state"] == "unknown"
    assert result["evidence"]["failure_code"] == "VK_BROWSER_STORY_SHARE_NOT_DISABLED"
    assert local_vk.publishes == local_vk.stories == 0


def test_personal_already_disabled_story_is_never_toggled(sample, local_vk, page, monkeypatch):
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


def test_personal_story_refuses_to_disable_never_clicks_or_retries(sample, local_vk, page, monkeypatch):
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
        sample, local_vk, page, phase, change, code):
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
def test_unproven_native_block_structure_never_clicks_publish(sample, local_vk, page, monkeypatch, markup):
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


def test_native_post_author_single_tracking_query_preserves_numeric_identity(sample, local_vk, page):
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


def test_full_native_fixture_upload_next_single_click_two_reload_and_replay(sample, local_vk, page):
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


def test_wrong_owner_fails_before_editor_or_upload(sample, local_vk, page):
    local_vk.wrong_owner = True
    result = run(page, sample, local_vk)
    assert result["state"] == "failed" and result["evidence"]["failure_code"] == "VK_BROWSER_ACCOUNT_MISMATCH"
    assert local_vk.uploads == local_vk.publishes == 0


def test_lost_publish_reply_is_unknown_and_never_reuploads_or_reclicks(sample, local_vk, page):
    local_vk.drop_reply = True
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown", result
    assert local_vk.publishes == local_vk.uploads == 1
    assert run(page, sample, local_vk)["state"] == "unknown"
    assert local_vk.publishes == local_vk.uploads == 1


def test_wrong_recipient_after_next_never_clicks_publish(sample, local_vk, page, monkeypatch):
    final = worker._final_snapshot

    def wrong_recipient(*args, **kwargs):
        page.locator("#recipient").evaluate("e=>e.setAttribute('data-owner-id','-999')")
        return final(*args, **kwargs)

    monkeypatch.setattr(worker, "_final_snapshot", wrong_recipient)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_RECIPIENT_MISMATCH"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_pause_after_image_get_prevents_send(sample, local_vk, page, monkeypatch):
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


def test_dom_change_during_image_get_prevents_send(sample, local_vk, page, monkeypatch):
    image_read = worker._image_proof

    def mutate_after_read(*args, **kwargs):
        result = image_read(*args, **kwargs)
        page.locator("#preview-text").evaluate("e=>e.textContent='Неодобренный текст'")
        return result

    monkeypatch.setattr(worker, "_image_proof", mutate_after_read)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and local_vk.publishes == 0
    assert result["evidence"]["failure_code"] == "VK_BROWSER_PREVIEW_TEXT_MISMATCH"


def test_changed_snapshot_cannot_reuse_durable_request(sample, local_vk, page):
    assert run(page, sample, local_vk)["state"] == "confirmed"
    sample.snapshot["body"] += " Новое содержание."
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_CONFLICT"):
        run(page, sample, local_vk)
    assert local_vk.uploads == local_vk.publishes == 1


def test_fixture_journal_cannot_be_promoted_to_live_verified(sample, local_vk, page):
    assert run(page, sample, local_vk)["state"] == "confirmed"
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_CONFLICT"):
        worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"])


def test_image_changed_after_approval_fails_before_browser_mutation(sample, local_vk, page):
    (sample.root / "approved.png").write_bytes(b"changed")
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_HASH_CHANGED"):
        run(page, sample, local_vk)
    assert local_vk.uploads == local_vk.publishes == 0


def test_missing_journal_claim_never_reopens_existing_request(sample, local_vk, page):
    path = worker._path(sample.root, sample.payload["request_id"])
    path.parent.mkdir(parents=True)
    path.write_text("{partial", encoding="utf-8")
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_INVALID"):
        run(page, sample, local_vk)
    assert local_vk.uploads == local_vk.publishes == 0


def test_readback_mismatch_keeps_known_native_id_for_read_only_reconciliation(sample, local_vk, page, monkeypatch):
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


def test_community_wall_keeps_authenticated_account_separate_from_recipient(sample, local_vk, page):
    local_vk.destination = "-456789"
    sample.snapshot["destination"] = sample.payload["destination"] = local_vk.destination
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed" and result["external_id"] == "-456789_51"
    assert local_vk.uploads == local_vk.publishes == 1


def test_plain_text_only_is_one_native_post_without_upload(sample, local_vk, page):
    sample.snapshot["media"] = []
    sample.options["media_contract_verified"] = False
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed" and local_vk.publishes == 1 and local_vk.uploads == 0


def test_full_long_article_and_unicode_title_survive_native_flow_without_truncation(sample, local_vk, page):
    sample.snapshot["title"] = "🔭 Космическое открытие"
    sample.snapshot["body"] = "Полный научный текст с точными наблюдениями. " * 60 + "\n\nИсточник: https://science.example.test/a"
    expected = sample.snapshot["title"] + "\n\n" + sample.snapshot["body"]
    result = run(page, sample, local_vk)
    assert result["state"] == "confirmed" and local_vk.posts == [(expected, 51)]
    assert len(expected) > 2500 and result["evidence"]["proof"]["title_format"] == "plain"


def test_wrong_wall_redirect_is_blocked_before_editor_mutation(sample, local_vk, page):
    local_vk.redirect_wrong_wall = True
    result = run(page, sample, local_vk)
    assert result["state"] == "failed" and result["evidence"]["failure_code"] == "VK_BROWSER_WALL_MISMATCH"
    assert local_vk.uploads == local_vk.publishes == 0


def test_image_bytes_must_match_frozen_sha_before_publish(sample, local_vk, page, monkeypatch):
    image_read = worker._image_proof

    def wrong_bytes(*args, **kwargs):
        local_vk.bad_photo = True
        return image_read(*args, **kwargs)

    monkeypatch.setattr(worker, "_image_proof", wrong_bytes)
    result = run(page, sample, local_vk)
    assert result["state"] == "unknown" and result["evidence"]["failure_code"] == "VK_BROWSER_IMAGE_READBACK_MISMATCH"
    assert local_vk.uploads == 1 and local_vk.publishes == 0


def test_lease_loss_during_durable_click_claim_prevents_click(sample, local_vk, page, monkeypatch):
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


def test_native_fixture_result_remains_unverified_through_adapter(sample, local_vk, page, monkeypatch):
    native = run(page, sample, local_vk)
    assert native["state"] == "confirmed"
    adapter = VKBrowser(sample.root, sample.options)
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: native)
    result = adapter.publish(OWNER, sample.snapshot, sample.payload["request_id"])
    assert result.state == "confirmed" and result.verified is False and result.evidence["fixture"] is True


def test_pause_during_upload_claim_prevents_upload_and_never_reopens(sample, local_vk, page, monkeypatch):
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


def test_click_journal_write_failure_cannot_send_or_retry_upload(sample, local_vk, page, monkeypatch):
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


def test_unknown_without_native_post_id_reconcile_does_not_speculate_or_mutate(sample, local_vk, page):
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
        sample, local_vk, page, monkeypatch, change, reload_pass):
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
    assert result["external_id"] == OWNER + "_51"
    assert result["evidence"]["proof"] is None
    assert len(readback_passes) == reload_pass
    assert local_vk.uploads == local_vk.publishes == 1
    before = list(local_vk.gets)
    repeated = run(page, sample, local_vk)
    assert repeated["state"] == "unknown" and repeated["verified"] is False
    assert repeated["external_id"] == result["external_id"] and local_vk.gets == before
    assert local_vk.uploads == local_vk.publishes == 1


@pytest.mark.parametrize("community", [False, True])
def test_identical_post_on_correct_wall_from_wrong_author_is_unknown(sample, local_vk, page, community):
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


def test_preview_publishing_identity_is_checked_before_send(sample, local_vk, page, monkeypatch):
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


def test_confirmed_journal_reconciliation_performs_two_new_wall_reads(sample, local_vk, page):
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
def test_confirmed_journal_never_substitutes_old_proof_for_changed_live_post(sample, local_vk, page, change):
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
def test_unverified_image_flow_stops_before_editor_upload_or_journal(sample, local_vk, page, flag):
    if flag is None:
        sample.options.pop("media_contract_verified")
    else:
        sample.options["media_contract_verified"] = flag
    with pytest.raises(AgentError, match="VK_BROWSER_MEDIA_CONTRACT_UNVERIFIED"):
        run(page, sample, local_vk)
    assert local_vk.gets == [] and local_vk.uploads == local_vk.publishes == 0
    assert not (sample.root / "data").exists()
