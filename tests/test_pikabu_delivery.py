"""Pikabu package integrity and real-browser single-click publication fixtures."""

import hashlib
import html
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.pikabu_browser import PikabuBrowser
from max_postiz_agent.pikabu_browser_worker import (
    _account, _editor_snapshot, _read_journal, authorize, inspect_page, probe_page, reconcile,
    run_publish, session,
)
from max_postiz_agent.pikabu_delivery import PikabuDelivery, editor_body, parse_public_story
from test_hub import approve, hub as hub, variants


@pytest.fixture
def post(tmp_path):
    (tmp_path / "data/media").mkdir(parents=True)
    path = tmp_path / "data/media/fixture.png"
    Image.new("RGB", (32, 24), "blue").save(path)
    return {"title": "Космический объект", "body": "Туманность видна на снимке.\n\n"
            "Источник: https://example.org/article", "destination": "fixture_owner",
            "platform": "pikabu", "route": "pikabu_browser",
            "metadata": {"tags": ["Космос", "Астрономия", "Контент нейросетей"]},
            "sources": [{"url": "https://example.org/article"}],
            "media": [{"path": "data/media/fixture.png", "mime": "image/png",
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}


def test_exact_export_and_repeat_validate_without_overwrite(tmp_path, post):
    delivery = PikabuDelivery(tmp_path)
    hashed = fingerprint({"content": post, "due_at": 1234})
    result = delivery.prepare(post, "dispatch-1", hashed, 1234)
    assert result.state == "awaiting_manual"
    assert result.verified is False
    directory = tmp_path / "data/exports/dispatch-1"
    assert (directory / "package.zip").is_file()
    original = (directory / "manifest.json").read_bytes()
    assert delivery.prepare(post, "dispatch-1", hashed, 1234).state == "awaiting_manual"
    assert (directory / "manifest.json").read_bytes() == original
    assert delivery.verify_package(directory)["snapshot_hash"] == hashed


def test_export_tamper_and_stale_approval_block(tmp_path, post):
    delivery = PikabuDelivery(tmp_path)
    with pytest.raises(AgentError, match="PIKABU_APPROVAL_STALE"):
        delivery.prepare(post, "dispatch-1", "0" * 64, 1234)
    hashed = fingerprint({"content": post, "due_at": 1234})
    delivery.prepare(post, "dispatch-1", hashed, 1234)
    (tmp_path / "data/exports/dispatch-1/post.txt").write_text("подмена", encoding="utf-8")
    with pytest.raises(AgentError, match="PIKABU_PACKAGE_DAMAGED"):
        delivery.prepare(post, "dispatch-1", hashed, 1234)


def test_media_change_path_and_owner_block(tmp_path, post):
    delivery = PikabuDelivery(tmp_path)
    post["media"][0]["path"] = "../../outside.png"
    with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
        delivery.validate("fixture_owner", post)
    post["media"] = []
    browser = PikabuBrowser(tmp_path, {"owner": "fixture_owner"})
    with pytest.raises(AgentError, match="PIKABU_DESTINATION_OWNER_MISMATCH"):
        browser.validate("another_owner", post)


def test_exact_title_prefix_is_separated_once(post):
    post["body"] = post["title"] + "\n\n" + post["body"]
    assert editor_body(post).startswith("Туманность")
    assert post["body"].startswith(post["title"])


def test_limits_and_preapproval_resize_are_explicit(tmp_path, post):
    delivery = PikabuDelivery(tmp_path)
    post["title"] = "А" * 141
    with pytest.raises(AgentError, match="PIKABU_TITLE_INVALID"):
        delivery.validate("fixture_owner", post)
    post["title"] = "Космос"
    path = tmp_path / "data/media/wide.png"
    Image.new("RGB", (2049, 20), "blue").save(path)
    post["media"] = [{"path": "data/media/wide.png", "mime": "image/png",
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    with pytest.raises(AgentError, match="PIKABU_IMAGE_PREAPPROVAL_RESIZE_REQUIRED"):
        delivery.validate("fixture_owner", post)


def _story_html(origin, post, owner="fixture_owner", *, body=None, duplicate=False):
    url = origin + "/story/test_123"
    paragraphs = []
    for paragraph in (body if body is not None else editor_body(post)).split("\n\n"):
        value = html.escape(paragraph).replace("\n", "<br>")
        value = value.replace("https://example.org/article",
                              '<a href="https://example.org/article">https://example.org/article</a>')
        paragraphs.append('<div class="story-block story-block_type_text"><p>' + value + "</p></div>")
    images = '<figure class="story-image"><img class="story-image__image" '
    images += 'src="https://cs1.pikabu.ru/media.png" '
    images += 'data-large-image="https://cs1.pikabu.ru/media.png"></figure>'
    image_block = '<div class="story-block story-block_type_image">' + images + "</div>"
    article = ('<article class="story" data-story-id="123"><a class="story__user-link" '
               'href="' + origin + '/@' + owner + '">' + owner + '</a><h1 class="story__title">'
               '<span class="story__title-link">' + html.escape(post["title"])
               + '</span></h1><div class="story__content-inner">'
               + (image_block if post.get("media") else "")
               + "".join(paragraphs) + '</div><div class="tags">'
               + "".join('<a class="tags__tag">' + html.escape(tag) + "</a>"
                         for tag in post["metadata"]["tags"]) + "</div></article>")
    data = {"@type": "Article", "url": url, "headline": post["title"],
            "author": {"name": owner, "url": origin + "/@" + owner}}
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<link rel="canonical" href="' + url + '"><title>Пост</title>'
            '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False)
            + '</script></head><body><a class="account" href="/@fixture_owner">fixture_owner</a>'
            + article + (article if duplicate else "") + "</body></html>")


def test_public_readback_exact_scope_author_body_tags_sources(post):
    raw = _story_html("https://pikabu.ru", post).encode()
    proof = parse_public_story(raw, "https://pikabu.ru/story/test_123", post, "fixture_owner")
    assert proof["external_id"] == "123"
    assert proof["image_urls"] == ["https://cs1.pikabu.ru/media.png"]
    with pytest.raises(AgentError, match="PIKABU_READBACK_OWNER_MISMATCH"):
        parse_public_story(raw, "https://pikabu.ru/story/test_123", post, "another_owner")
    with pytest.raises(AgentError, match="PIKABU_READBACK_BODY_MISMATCH"):
        parse_public_story(_story_html("https://pikabu.ru", post, body="Подменённый текст").encode(),
                           "https://pikabu.ru/story/test_123", post, "fixture_owner")
    with pytest.raises(AgentError, match="PIKABU_READBACK_AMBIGUOUS"):
        parse_public_story(_story_html("https://pikabu.ru", post, duplicate=True).encode(),
                           "https://pikabu.ru/story/test_123", post, "fixture_owner")
    extra_tag = raw.replace(b'</div></article>',
                            '<a class="tags__tag">Несогласованный тег</a></div></article>'.encode())
    with pytest.raises(AgentError, match="PIKABU_READBACK_TAGS_MISMATCH"):
        parse_public_story(extra_tag, "https://pikabu.ru/story/test_123", post, "fixture_owner")
    unsafe_author = raw.replace(b'href="https://pikabu.ru/@fixture_owner"',
                                b'href="javascript:/@fixture_owner"', 1)
    with pytest.raises(AgentError, match="PIKABU_READBACK_OWNER_MISMATCH"):
        parse_public_story(unsafe_author, "https://pikabu.ru/story/test_123", post, "fixture_owner")
    extra_link = raw.decode().replace("Туманность видна", '<a href="https://example.net/unapproved">Туманность</a> видна')
    with pytest.raises(AgentError, match="PIKABU_UNAPPROVED_LINK"):
        parse_public_story(extra_link.encode(), "https://pikabu.ru/story/test_123", post, "fixture_owner")


def test_browser_unicode_dom_with_legacy_site_charset_stays_exact(post):
    text = _story_html("https://pikabu.ru", post).replace('charset="utf-8"', 'charset="windows-1251"')
    proof = parse_public_story(text.encode("utf-8"), "https://pikabu.ru/story/test_123",
                               post, "fixture_owner", encoding="utf-8")
    assert proof["owner"] == "fixture_owner"


@pytest.mark.parametrize("extra,code", [
    ('<img src="https://cs1.pikabu.ru/extra.png">', "PIKABU_READBACK_IMAGE_MISMATCH"),
    ('Несогласованный дополнительный текст', "PIKABU_READBACK_UNSUPPORTED_BLOCK"),
])
def test_public_parser_never_ignores_extra_image_or_text(post, extra, code):
    raw = _story_html("https://pikabu.ru", post).replace(
        '<div class="story-block story-block_type_text">',
        '<div class="story-block story-block_type_text">' + extra, 1)
    with pytest.raises(AgentError, match=code):
        parse_public_story(raw.encode(), "https://pikabu.ru/story/test_123", post, "fixture_owner")


@pytest.fixture
def browser_fixture(tmp_path, post):
    state = {"posts": 0, "uploads": 0, "readbacks": 0, "wrong_body": False, "bad_image": False,
             "wrong_account": False, "hang_after_click": False, "requests": [],
             "selected_community": False, "community_value": False,
             "missing_community_input": False, "renamed_community_input": False,
             "delayed_controls": False, "wrapped_paragraphs": False, "external_navigation_blocked": [],
             "native_public_tags": []}
    raw_image = (tmp_path / post["media"][0]["path"]).read_bytes()
    different = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(different, "PNG")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, raw, content_type="text/html; charset=utf-8"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state["requests"].append(self.path)
            if self.path == "/media.png":
                return self.send(different.getvalue() if state["bad_image"] else raw_image, "image/png")
            if self.path.startswith("/story/"):
                state["readbacks"] += 1
                body = "Подменённый текст" if state["wrong_body"] else None
                rendered = _story_html(origin, post, body=body)
                if state["native_public_tags"]:
                    rendered = rendered.replace('</div></article>', ''.join(
                        '<a class="tags__tag">' + html.escape(tag) + '</a>'
                        for tag in state["native_public_tags"]) + '</div></article>')
                return self.send(rendered.encode())
            owner = "someone_else" if state["wrong_account"] else "fixture_owner"
            template = EDITOR_HTML.replace("__OWNER__", owner)
            if state["wrapped_paragraphs"]:
                from test_pikabu_editor_contract import wrapped
                template = template.replace('<p><br class="ProseMirror-trailingBreak"></p>',
                    wrapped('<br class="ProseMirror-trailingBreak">'), 1)
                template = template.replace("e.append(p);", "e.append(wrap(p));")
                template = template.replace("const e=document.querySelector('.editor');", """
const e=document.querySelector('.editor');
function wrap(p){
 const host=document.createElement('div');host.className='node-wrapper-view__host--fixture';
 host.setAttribute('data-node-view-wrapper','');host.style.display='flex';host.style.flexDirection='column';
 const empty=document.createElement('div');empty.append(document.createComment('inert'));
 const content=document.createElement('div');content.className='node-wrapper-view__content--fixture';
 p.className='node-paragraph-view__content--fixture';content.append(p);
 host.append(document.createComment('inert'),empty,content);return host;
}
""")
            if state["selected_community"]:
                template = template.replace('<!--community-label-->',
                                            '<span class="unrecognized-community-label">Чужое сообщество</span>')
            if state["community_value"]:
                template = template.replace('placeholder="Введите название сообщества"',
                                            'placeholder="Введите название сообщества" value="Чужое сообщество"')
            if state["missing_community_input"]:
                template = template.replace('<input placeholder="Введите название сообщества">', "")
            if state["renamed_community_input"]:
                template = template.replace('placeholder="Введите название сообщества"',
                                            'placeholder="Выбрано сообщество"')
            if state["delayed_controls"]:
                template = template.replace('<textarea',
                    '<style id="hydrate-delay">textarea,.editor,#tags,[data-test="story-publish"],'
                    '.community-combo{display:none}</style><script>setTimeout(()=>'
                    'document.getElementById("hydrate-delay").remove(),250)</script><textarea', 1)
            self.send(template.encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path == "/upload":
                state["uploads"] += 1
                return self.send(json.dumps({"src": origin + "/media.png"}).encode(), "application/json")
            state["posts"] += 1
            self.send(json.dumps({"url": origin + "/story/test_123",
                                  "hang": state["hang_after_click"]}).encode(), "application/json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = "http://127.0.0.1:" + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    options = {"owner": "fixture_owner", "channel": "chrome", "headless": True,
               "operation_timeout_seconds": 2, "dom_contract_verified": True,
               "media_contract_verified": True,
               "dom_contract": {"account": ".account", "title": "textarea",
                                "body": ".editor", "tags_input": "#tags",
                                "tag_values": ".tagvalue", "uploaded_images": ".editor img",
                                "community": ".selected-community",
                                "community_input": 'input[placeholder="Введите название сообщества"]',
                                "publish": '[data-test="story-publish"]'}}
    with session(tmp_path, options) as page:
        # Browser creation and a first local navigation can be delayed while the
        # repository's full suite is using Chromium. Keep the production click
        # bound intact; only this fixture's read-only navigation gets headroom.
        page.set_default_navigation_timeout(10000)
        def local_only(route):
            if route.request.url.startswith(origin + "/"):
                return route.continue_()
            if route.request.url.startswith("https://cs1.pikabu.ru/"):
                return route.fulfill(status=200, content_type="image/png", body=raw_image)
            state["external_navigation_blocked"].append(route.request.url)
            route.abort()
        page.route("**/*", local_only)
        yield page, origin, options, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


EDITOR_HTML = r'''<!doctype html><html><meta charset="utf-8"><title>Редактор</title>
<a class="account" href="/@__OWNER__">__OWNER__</a>
<textarea placeholder="Заголовок"></textarea>
<div class="editor tiptap" contenteditable="true"><p><br class="ProseMirror-trailingBreak"></p></div>
<input id="tags" placeholder="Добавьте от 2 до 20 тегов"><div id="tagvalues"></div>
<div class="community-combo" role="combobox"><!--community-label--><input placeholder="Введите название сообщества"></div>
<button data-test="story-publish">Опубликовать пост</button>
<script>
const e=document.querySelector('.editor');
function focus(p){e.focus();const r=document.createRange();r.selectNodeContents(p);r.collapse(false);
 const s=getSelection();s.removeAllRanges();s.addRange(r);}
e.addEventListener('beforeinput',ev=>{if(ev.inputType==='insertParagraph'){
 ev.preventDefault();const p=document.createElement('p');p.innerHTML='<br class="ProseMirror-trailingBreak">';
 e.append(p);e.focus();const r=document.createRange();r.setStart(p,0);r.collapse(true);
 const s=getSelection();s.removeAllRanges();s.addRange(r);}});
e.addEventListener('paste',async ev=>{if(!ev.clipboardData.files.length)return;
 ev.preventDefault();const file=ev.clipboardData.files[0];
 const r=await fetch('/upload',{method:'POST',body:file});const data=await r.json();
 const fig=document.createElement('figure');const img=document.createElement('img');img.src=data.src;
 fig.append(img);e.prepend(fig);focus(e.querySelector('p'));});
document.querySelector('#tags').addEventListener('keydown',ev=>{if(ev.key==='Enter'){
 ev.preventDefault();const t=document.createElement('span');t.className='tagvalue';
 t.textContent=ev.target.value;document.querySelector('#tagvalues').append(t);ev.target.value='';}});
document.querySelector('[data-test="story-publish"]').onclick=async()=>{
 const r=await fetch('/publish',{method:'POST',body:'approved-exact-fixture'});
 const data=await r.json();if(!data.hang)location.href=data.url;};
</script></html>'''


def test_real_browser_full_automatic_single_click_reload_and_no_repeat(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    calls = []
    payload = {"destination": "fixture_owner", "request_id": "auto-1", "snapshot": post,
               "action": "publish"}
    result = run_publish(page, tmp_path, options, payload, lambda: calls.append(True),
                         origin=origin, fixture=True)
    assert result["state"] == "confirmed", (result, _editor_snapshot(page, options["dom_contract"]),
                                             page.locator(".editor").inner_html())
    assert result["verified"] is False
    assert result["evidence"]["kind"] == "fixture"
    assert state["posts"] == state["uploads"] == 1
    assert state["readbacks"] >= 2
    assert len(calls) >= 3
    again = run_publish(page, tmp_path, options, payload, lambda: calls.append(True),
                        origin=origin, fixture=True)
    assert again["state"] == "confirmed"
    assert state["posts"] == state["uploads"] == 1
    journal = _read_journal(tmp_path, "auto-1")[1]
    assert journal["clicks"] == 1
    assert journal["proof"]["image_sha256"] == [post["media"][0]["sha256"]]


def test_real_browser_text_only_automatic_publication_and_readback(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    post["media"] = []
    post["sources"] = []
    post["body"] = "Это техническая проверка автоматической публикации.\n\nТекст подготовлен владельцем."
    payload = {"destination": "fixture_owner", "request_id": "text-only-auto", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "confirmed" and result["verified"] is False
    assert state["posts"] == 1 and state["uploads"] == 0
    assert state["readbacks"] >= 2 and not state["external_navigation_blocked"]
    assert result["evidence"]["proof"]["image_sha256"] == []


def test_native_text_auto_tag_confirms_once_without_changing_declared_snapshot(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    post["media"], post["sources"] = [], []
    state["native_public_tags"] = ["Текст"]
    original = fingerprint(post)
    payload = {"destination": "fixture_owner", "request_id": "native-text-once", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "confirmed" and result["verified"] is False
    assert result["evidence"]["proof"]["platform_added_tags"] == ["Текст"]
    assert result["evidence"]["proof"]["declared_tags_sha256"] == result["evidence"]["proof"]["tags_sha256"]
    assert state["posts"] == 1 and state["uploads"] == 0
    assert fingerprint(post) == original
    before = list(state["requests"])
    assert run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True) == result
    assert state["requests"] == before and state["posts"] == 1


def test_native_tag_mismatch_reconcile_only_reads_existing_story_without_second_publish(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    post["media"], post["sources"] = [], []
    state["native_public_tags"] = ["Текст", "Несогласованный тег"]
    payload = {"destination": "fixture_owner", "request_id": "native-tag-reconcile", "snapshot": post}
    failed = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert failed["state"] == "unknown" and state["posts"] == 1
    state["native_public_tags"] = ["Текст"]
    result = reconcile(page, tmp_path, options, "fixture_owner", payload["request_id"], origin=origin, fixture=True)
    assert result["state"] == "confirmed" and result["verified"] is False
    assert result["evidence"]["proof"]["platform_added_tags"] == ["Текст"]
    assert state["posts"] == 1 and state["uploads"] == 0


@pytest.mark.parametrize("ending", ["\n", "\n\n"])
def test_ambiguous_terminal_linebreak_is_rejected_before_editor_or_journal(tmp_path, post, browser_fixture, ending):
    page, origin, options, state = browser_fixture
    post["body"] += ending
    with pytest.raises(AgentError, match="PIKABU_TRAILING_LINEBREAK_UNSUPPORTED"):
        PikabuBrowser(tmp_path, options).validate("fixture_owner", post)
    with pytest.raises(AgentError, match="PIKABU_TRAILING_LINEBREAK_UNSUPPORTED"):
        run_publish(page, tmp_path, options,
                    {"destination": "fixture_owner", "request_id": "trailing-reject", "snapshot": post},
                    lambda: None, origin=origin, fixture=True)
    assert not state["requests"] and state["posts"] == state["uploads"] == 0
    assert not (tmp_path / "data/pikabu-operations/trailing-reject.json").exists()


@pytest.mark.parametrize("change,code", [
    ("link", "PIKABU_UNAPPROVED_LINK"), ("image", "PIKABU_IMAGE_UPLOAD_UNCONFIRMED"),
])
def test_extra_semantic_link_or_hidden_image_never_reaches_publish(
        tmp_path, post, browser_fixture, monkeypatch, change, code):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    initial = worker._fill_editor
    def changed(*args, **kwargs):
        value = initial(*args, **kwargs)
        if change == "link":
            page.locator(".editor p").first.evaluate("""node=>{
              node.innerHTML='<a href="https://example.net/unapproved">Туманность</a> видна на снимке.';}
            """)
        else:
            page.locator(".editor img").evaluate("node=>node.style.opacity='0'")
        return value
    monkeypatch.setattr(worker, "_fill_editor", changed)
    payload = {"destination": "fixture_owner", "request_id": "unapproved-link-image", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["evidence"]["error"] == code
    assert state["posts"] == 0 and state["uploads"] == 1
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == 0 and state["uploads"] == 1


def test_native_focus_fills_observed_wrapped_paragraphs_and_publishes_once(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    state["wrapped_paragraphs"] = True
    post["media"] = []
    payload = {"destination": "fixture_owner", "request_id": "wrapped-native-fill", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "confirmed", result
    assert state["posts"] == 1 and state["uploads"] == 0
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "confirmed"
    assert state["posts"] == 1 and state["uploads"] == 0


@pytest.mark.parametrize("change,code", [
    ("focus", "PIKABU_EDITOR_FOCUS_CHANGED"),
    ("selection", "PIKABU_EDITOR_FOCUS_CHANGED"),
    ("account", "PIKABU_ACCOUNT_MISMATCH"),
    ("url", "PIKABU_EDITOR_URL_CHANGED"),
])
def test_authorizer_context_drift_stops_before_next_body_key_or_publish(
        tmp_path, post, browser_fixture, change, code):
    page, origin, options, state = browser_fixture
    state["wrapped_paragraphs"] = True
    post["media"] = []
    flags = {"changed": False}
    def authorizer():
        if page.url != origin + "/add" or flags["changed"]:
            return
        if not page.locator(".editor").evaluate("node=>document.activeElement===node"):
            return
        flags["changed"] = True
        page.evaluate("""() => {window.editorKeysAfterDrift=0;
          document.querySelector('.editor').addEventListener('beforeinput',()=>window.editorKeysAfterDrift++);}
        """)
        if change == "focus":
            page.locator("textarea").focus()
        elif change == "selection":
            page.evaluate("()=>getSelection().removeAllRanges()")
        elif change == "account":
            page.locator(".account").evaluate("node=>node.href='/@someone_else'")
        else:
            page.evaluate("()=>history.replaceState(null,'','/story/existing_456')")
    payload = {"destination": "fixture_owner", "request_id": "context-drift", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, authorizer, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == code
    assert page.evaluate("window.editorKeysAfterDrift") == 0
    assert state["posts"] == state["uploads"] == 0
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == state["uploads"] == 0


def test_real_browser_unknown_after_click_is_never_clicked_again(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    state["hang_after_click"] = True
    payload = {"destination": "fixture_owner", "request_id": "auto-unknown", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert state["posts"] == 1
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert reconcile(page, tmp_path, options, "fixture_owner", "auto-unknown",
                     origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == 1


def test_real_browser_wrong_owner_never_uploads_or_clicks(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    state["wrong_account"] = True
    result = run_publish(page, tmp_path, options,
                         {"destination": "fixture_owner", "request_id": "wrong-owner", "snapshot": post},
                         lambda: None, origin=origin, fixture=True)
    assert result["state"] == "failed"
    assert state["posts"] == state["uploads"] == 0
    assert result["evidence"]["error"] == "PIKABU_ACCOUNT_MISMATCH"


def test_real_browser_mismatched_readback_reconciles_without_second_click(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    state["wrong_body"] = True
    payload = {"destination": "fixture_owner", "request_id": "readback-unknown", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert state["posts"] == 1
    state["wrong_body"] = False
    result = reconcile(page, tmp_path, options, "fixture_owner", "readback-unknown",
                       origin=origin, fixture=True)
    assert result["state"] == "confirmed", result
    assert state["posts"] == 1


def test_unverified_dom_stops_before_navigation(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    options["dom_contract_verified"] = False
    with pytest.raises(AgentError, match="PIKABU_DOM_CONTRACT_UNVERIFIED"):
        run_publish(page, tmp_path, options,
                    {"destination": "fixture_owner", "request_id": "no-dom", "snapshot": post},
                    lambda: None, origin=origin, fixture=True)
    assert not state["requests"]


def test_lost_approval_before_click_preserves_upload_and_blocks_retry(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    calls = []
    def authorize():
        calls.append(True)
        if state["uploads"] and page.locator(".tagvalue").count() == len(post["metadata"]["tags"]):
            raise AgentError("PIKABU_DISPATCH_NOT_APPROVED", 403)
    payload = {"destination": "fixture_owner", "request_id": "lost-approval", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, authorize, origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert state["uploads"] == 1
    assert state["posts"] == 0
    assert result["evidence"]["error"] == "PIKABU_DISPATCH_NOT_APPROVED"
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["uploads"] == 1


def test_corrupted_durable_journal_cannot_be_reused(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    payload = {"destination": "fixture_owner", "request_id": "journal-tamper", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "confirmed", result
    path, journal = _read_journal(tmp_path, "journal-tamper")
    journal["clicks"] = 0
    path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(AgentError, match="PIKABU_JOURNAL_INVALID"):
        run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert state["posts"] == 1


def test_uploaded_wrong_image_never_reaches_publish(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    state["bad_image"] = True
    payload = {"destination": "fixture_owner", "request_id": "wrong-image", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert state["uploads"] == 1
    assert state["posts"] == 0
    assert result["evidence"]["error"] == "PIKABU_READBACK_IMAGE_HASH_MISMATCH"


def test_worker_rechecks_exact_database_dispatch_and_original_lease_owner(hub):
    from max_postiz_agent.db import now
    from max_postiz_agent.hub_models import Dispatch, TargetApproval
    options = {"owner": "fixture_owner", "dom_contract_verified": True,
               "dom_contract": {"tag_values": ".tag", "uploaded_images": ".image",
                                "community": ".selected-community"}}
    config = hub.publishing
    target = next(item for item in config["targets"] if item["id"] == "pikabu")
    target.update(route="pikabu_browser", destination="fixture_owner", options=options)
    (hub.settings.root / "config/publishing.json").write_text(json.dumps(config), encoding="utf-8")
    hub.activate()
    hub.pause_target("pikabu", False)
    ident = approve(hub, variants(hub)["pikabu"])
    with hub.db.session.begin() as transaction:
        row = transaction.get(Dispatch, ident)
        row.state, row.owner, row.lease_until = "sending", "worker-original", now() + 120
        snapshot = row.snapshot
    payload = {"destination": "fixture_owner", "request_id": ident, "snapshot": snapshot}
    assert authorize(hub.settings.root, payload, options) == "worker-original"
    from max_postiz_agent.server_models import EditorialControl
    from max_postiz_agent.server_state import ServerState
    ServerState(hub).initialize()
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        authorize(hub.settings.root, payload, options)
    with hub.db.session.begin() as transaction:
        transaction.get(EditorialControl, "main").paused = False
    assert authorize(hub.settings.root, payload, options) == "worker-original"
    with pytest.raises(AgentError, match="PIKABU_DISPATCH_NOT_APPROVED"):
        authorize(hub.settings.root, payload, options, expected_lease_owner="worker-other")
    altered = json.loads(json.dumps(payload))
    altered["snapshot"]["body"] += " Подмена."
    with pytest.raises(AgentError, match="PIKABU_DISPATCH_NOT_APPROVED"):
        authorize(hub.settings.root, altered, options)
    hub.pause_target("pikabu", True)
    with pytest.raises(AgentError, match="TARGET_PAUSED"):
        authorize(hub.settings.root, payload, options)
    hub.pause_target("pikabu", False)
    with hub.db.session.begin() as transaction:
        row = transaction.get(Dispatch, ident)
        transaction.get(TargetApproval, row.approval_id).revoked = True
    with pytest.raises(AgentError, match="APPROVAL_STALE"):
        authorize(hub.settings.root, payload, options)
    with hub.db.session.begin() as transaction:
        row = transaction.get(Dispatch, ident)
        transaction.get(TargetApproval, row.approval_id).revoked = False
        row.lease_until = now() + 2
    with pytest.raises(AgentError, match="PIKABU_DISPATCH_NOT_APPROVED"):
        authorize(hub.settings.root, payload, options)
    with hub.db.session.begin() as transaction:
        transaction.get(Dispatch, ident).lease_until = now() - 1
    with pytest.raises(AgentError, match="PIKABU_DISPATCH_NOT_APPROVED"):
        authorize(hub.settings.root, payload, options)


def test_final_fence_reads_pause_committed_during_expensive_quality_in_a_new_session(hub, monkeypatch):
    from max_postiz_agent.db import now
    from max_postiz_agent.hub import HubService
    from max_postiz_agent.hub_models import Dispatch
    options = {"owner": "fixture_owner", "dom_contract_verified": True}
    config = hub.publishing
    target = next(item for item in config["targets"] if item["id"] == "pikabu")
    target.update(route="pikabu_browser", destination="fixture_owner", options=options)
    (hub.settings.root / "config/publishing.json").write_text(json.dumps(config), encoding="utf-8")
    hub.activate()
    hub.pause_target("pikabu", False)
    ident = approve(hub, variants(hub)["pikabu"])
    with hub.db.session.begin() as session:
        row = session.get(Dispatch, ident)
        row.state, row.owner, row.lease_until = "sending", "original-owner", now() + 120
        snapshot = row.snapshot
    original = HubService._valid_dispatch
    def quality_and_pause(core, session, row):
        result = original(core, session, row)
        hub.pause_target("pikabu", True)
        return result
    monkeypatch.setattr(HubService, "_valid_dispatch", quality_and_pause)
    with pytest.raises(AgentError, match="TARGET_PAUSED"):
        authorize(hub.settings.root, {"request_id": ident, "destination": "fixture_owner",
                                     "snapshot": snapshot}, options)


def test_readonly_dom_probe_never_fills_uploads_or_publishes(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    report = probe_page(page, options, origin=origin)
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["mutations"] == 0
    assert report["owner_link_candidates"][0]["text_equals_owner"] is True
    assert post["body"] not in serialized
    assert all("value" not in control for control in report["controls"])
    options["dom_contract"]["account"] = ".unknown-current-account-class"
    inspected = inspect_page(page, options, origin=origin)
    assert inspected["state"] == "account_contract_required"
    assert inspected["probe"]["mutations"] == 0
    assert state["posts"] == state["uploads"] == 0


def test_avatar_only_account_link_requires_exact_visible_owner_url(browser_fixture):
    page, origin, options, state = browser_fixture
    page.goto(origin + "/add")
    link = page.locator(".account")
    link.evaluate("node=>node.innerHTML='<span style=\"display:inline-block;width:24px;height:24px\"></span>'")
    _account(page, options, options["dom_contract"], origin=origin)
    link.evaluate("node=>node.style.display='none'")
    with pytest.raises(AgentError, match="PIKABU_ACCOUNT_MISMATCH"):
        _account(page, options, options["dom_contract"], origin=origin)
    link.evaluate("node=>{node.style.display='block';node.href='/@another_owner'}")
    with pytest.raises(AgentError, match="PIKABU_ACCOUNT_MISMATCH"):
        _account(page, options, options["dom_contract"], origin=origin)
    assert state["posts"] == state["uploads"] == 0


def test_known_preparation_continues_automatically_without_second_upload(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    payload = {"destination": "fixture_owner", "request_id": "prepared-then-publish",
               "snapshot": post, "action": "prepare"}
    prepared = run_publish(page, tmp_path, options, payload, lambda: None,
                           origin=origin, fixture=True)
    assert prepared["state"] == "prepared"
    assert state["uploads"] == 1
    assert state["posts"] == 0
    payload["action"] = "publish"
    published = run_publish(page, tmp_path, options, payload, lambda: None,
                            origin=origin, fixture=True)
    assert published["state"] == "confirmed", published
    assert state["uploads"] == state["posts"] == 1


@pytest.mark.parametrize("changed_error", ["APPROVAL_STALE", "TARGET_PAUSED", "PIKABU_DISPATCH_NOT_APPROVED"])
def test_changed_authorization_during_final_image_readback_never_clicks(
        tmp_path, post, browser_fixture, monkeypatch, changed_error):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    initial = worker._image_readback
    flags = {"reads": 0, "revoked": False}
    def readback(*args, **kwargs):
        result = initial(*args, **kwargs)
        flags["reads"] += 1
        if flags["reads"] == 2:
            flags["revoked"] = True
        return result
    def authorize():
        if flags["revoked"]:
            raise AgentError(changed_error, 403)
    monkeypatch.setattr(worker, "_image_readback", readback)
    payload = {"destination": "fixture_owner", "request_id": "revoked-during-readback", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, authorize, origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert state["uploads"] == 1
    assert state["posts"] == 0
    assert result["evidence"]["error"] == changed_error
    flags["revoked"] = False
    assert run_publish(page, tmp_path, options, payload, authorize,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["uploads"] == 1
    assert state["posts"] == 0


def test_editor_changed_during_final_image_readback_never_clicks(tmp_path, post, browser_fixture, monkeypatch):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    initial = worker._image_readback
    calls = []
    def readback(*args, **kwargs):
        result = initial(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            page.locator(".editor p").first.evaluate("node=>node.textContent='Подменённый текст'")
        return result
    monkeypatch.setattr(worker, "_image_readback", readback)
    payload = {"destination": "fixture_owner", "request_id": "editor-mutated-during-readback", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert result["evidence"]["error"] == "PIKABU_EDITOR_CHANGED_DURING_READBACK"
    assert state["uploads"] == 1
    assert state["posts"] == 0
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["uploads"] == 1
    assert state["posts"] == 0


@pytest.mark.parametrize("selected", ["selected_community", "community_value"])
def test_any_selected_community_blocks_before_upload_or_publish(tmp_path, post, browser_fixture, selected):
    page, origin, options, state = browser_fixture
    state[selected] = True
    payload = {"destination": "fixture_owner", "request_id": "wrong-community", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "failed"
    assert result["evidence"]["error"] == "PIKABU_EDITOR_NOT_EMPTY"
    assert state["uploads"] == state["posts"] == 0


@pytest.mark.parametrize("changed", ["missing_community_input", "renamed_community_input"])
def test_missing_or_changed_community_control_is_never_treated_as_empty(tmp_path, post, browser_fixture, changed):
    page, origin, options, state = browser_fixture
    state[changed] = True
    payload = {"destination": "fixture_owner", "request_id": "missing-community-control", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "failed"
    assert result["evidence"]["error"] == "PIKABU_EDITOR_NOT_READY"
    assert state["uploads"] == state["posts"] == 0


def test_hydrating_editor_is_waited_for_without_premature_mutation(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    state["delayed_controls"] = True
    payload = {"destination": "fixture_owner", "request_id": "hydrated-editor", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "confirmed", result
    assert state["uploads"] == state["posts"] == 1


def test_community_changed_during_final_image_readback_never_clicks(tmp_path, post, browser_fixture, monkeypatch):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    initial = worker._image_readback
    calls = []
    def readback(*args, **kwargs):
        result = initial(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            page.locator(".community-combo").evaluate("""node=>{
              const label=document.createElement('span');label.className='unknown-label-class';
              label.textContent='Чужое сообщество';node.append(label);}
            """)
        return result
    monkeypatch.setattr(worker, "_image_readback", readback)
    payload = {"destination": "fixture_owner", "request_id": "community-mutated-during-readback", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None,
                         origin=origin, fixture=True)
    assert result["state"] == "unknown"
    assert result["evidence"]["error"] == "PIKABU_EDITOR_CHANGED_DURING_READBACK"
    assert state["uploads"] == 1
    assert state["posts"] == 0


def test_fixture_journal_is_bound_to_origin_and_never_promoted_to_live(tmp_path, post, browser_fixture):
    from max_postiz_agent.pikabu_browser_worker import ORIGIN
    page, origin, options, state = browser_fixture
    payload = {"destination": "fixture_owner", "request_id": "fixture-bound", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "confirmed" and result["verified"] is False
    _, journal = _read_journal(tmp_path, payload["request_id"])
    assert journal["fixture"] is True and journal["execution_origin"] == origin
    requests = len(state["requests"])
    for operation in ("publish", "reconcile"):
        with pytest.raises(AgentError, match="PIKABU_JOURNAL_CONFLICT"):
            if operation == "publish":
                run_publish(page, tmp_path, options, payload, lambda: None, origin=ORIGIN, fixture=False)
            else:
                reconcile(page, tmp_path, options, "fixture_owner", payload["request_id"])
    with pytest.raises(AgentError, match="PIKABU_JOURNAL_CONFLICT"):
        reconcile(page, tmp_path, options, "fixture_owner", payload["request_id"],
                  origin="http://127.0.0.1:1", fixture=True)
    assert len(state["requests"]) == requests
    assert state["posts"] == state["uploads"] == 1


@pytest.mark.parametrize("missing", ["fixture", "execution_origin"])
def test_journal_requires_durable_execution_context_even_with_valid_checksum(
        tmp_path, post, browser_fixture, missing):
    page, origin, options, _ = browser_fixture
    payload = {"destination": "fixture_owner", "request_id": "missing-context", "snapshot": post}
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "confirmed"
    path, journal = _read_journal(tmp_path, payload["request_id"])
    journal.pop(missing)
    journal["journal_sha256"] = fingerprint({key: value for key, value in journal.items()
                                           if key != "journal_sha256"})
    path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(AgentError, match="PIKABU_JOURNAL_INVALID"):
        _read_journal(tmp_path, payload["request_id"])


def test_live_execution_cannot_use_fixture_origin(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    with pytest.raises(AgentError, match="PIKABU_EXECUTION_CONTEXT_INVALID"):
        run_publish(page, tmp_path, options,
                    {"destination": "fixture_owner", "request_id": "live-local", "snapshot": post},
                    lambda: None, origin=origin)
    assert not state["requests"]
    assert state["posts"] == state["uploads"] == 0


@pytest.mark.parametrize("value", [None, False])
def test_unverified_media_contract_blocks_adapter_and_worker_before_actions(
        tmp_path, post, browser_fixture, value):
    page, origin, options, state = browser_fixture
    if value is None:
        options.pop("media_contract_verified")
    else:
        options["media_contract_verified"] = value
    with pytest.raises(AgentError, match="PIKABU_MEDIA_CONTRACT_UNVERIFIED"):
        PikabuBrowser(tmp_path, options).validate("fixture_owner", post)
    with pytest.raises(AgentError, match="PIKABU_MEDIA_CONTRACT_UNVERIFIED"):
        run_publish(page, tmp_path, options,
                    {"destination": "fixture_owner", "request_id": "media-disabled", "snapshot": post},
                    lambda: None, origin=origin, fixture=True)
    assert not state["requests"]
    assert state["posts"] == state["uploads"] == 0


def test_confirmed_reconcile_reloads_and_rejects_changed_public_post(tmp_path, post, browser_fixture):
    page, origin, options, state = browser_fixture
    payload = {"destination": "fixture_owner", "request_id": "fresh-confirmed", "snapshot": post}
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "confirmed"
    reads = state["readbacks"]
    state["wrong_body"] = True
    result = reconcile(page, tmp_path, options, "fixture_owner", payload["request_id"],
                       origin=origin, fixture=True)
    assert state["readbacks"] > reads
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == "PIKABU_READBACK_BODY_MISMATCH"
    assert state["posts"] == state["uploads"] == 1
    assert not state["external_navigation_blocked"]
    state["wrong_body"] = False
    assert reconcile(page, tmp_path, options, "fixture_owner", payload["request_id"],
                     origin=origin, fixture=True)["state"] == "confirmed"
    assert state["posts"] == state["uploads"] == 1


@pytest.mark.parametrize("extra", [
    "<div>Несогласованный текст вне абзаца</div>",
    "Прямой текст вне абзаца",
    '<img class="unrecognized-image" src="data:image/png;base64,AAAA">',
])
def test_editor_unparsed_content_never_reaches_publish(tmp_path, post, browser_fixture, monkeypatch, extra):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    options["dom_contract"]["uploaded_images"] = ".editor figure img"
    original = worker._fill_editor
    def filled(*args, **kwargs):
        result = original(*args, **kwargs)
        page.locator(".editor").evaluate("(node, extra)=>node.insertAdjacentHTML('beforeend',extra)", extra)
        return result
    monkeypatch.setattr(worker, "_fill_editor", filled)
    payload = {"destination": "fixture_owner", "request_id": "extra-editor-content", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == "PIKABU_EDITOR_UNSUPPORTED_CONTENT"
    assert state["uploads"] == 1 and state["posts"] == 0
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["uploads"] == 1 and state["posts"] == 0


def test_fixture_readback_never_navigates_to_public_parser_url(tmp_path, post, browser_fixture):
    from max_postiz_agent.pikabu_browser_worker import _readback
    page, origin, options, state = browser_fixture
    with pytest.raises(AgentError, match="PIKABU_READBACK_WRONG_STORY"):
        _readback(page, "https://pikabu.ru/story/test_123", {"snapshot": post}, options, origin=origin)
    assert not state["requests"] and not state["external_navigation_blocked"]


@pytest.mark.parametrize("change", [
    "body", "title", "author", "tags", "source", "image_original", "extra_image", "account", "hidden",
    "hidden_inline", "url",
])
def test_public_post_changed_during_image_readback_is_unknown_without_second_click(
        tmp_path, post, browser_fixture, monkeypatch, change):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    initial = worker._image_readback
    flags = {"reads": 0}
    changes = {
        "body": "document.querySelector('.story-block_type_text p').textContent='Подменённый текст'",
        "title": "document.querySelector('.story__title-link').textContent='Подменённый заголовок'",
        "author": "document.querySelector('.story__user-link').setAttribute('href','/@someone_else')",
        "tags": "document.querySelector('.tags').insertAdjacentHTML('beforeend','<a class=tags__tag>Чужой тег</a>')",
        "source": "document.querySelector('.story__content-inner a').href='https://example.org/different'",
        "image_original": "document.querySelector('.story-image__image').dataset.largeImage='https://cs1.pikabu.ru/other.png'",
        "extra_image": "document.querySelector('.story__content-inner').insertAdjacentHTML('beforeend','<img src=data:image/png;base64,AAAA>')",
        "account": "document.querySelector('.account').href='/@someone_else'",
        "hidden": "document.querySelector('.story-block_type_text p').style.display='none'",
        "hidden_inline": "document.querySelector('.story-block_type_text p').innerHTML='<span style=display:none>Туманность</span> видна на снимке.'",
        "url": "history.replaceState(null,'','/story/other_456')",
    }
    def readback(*args, **kwargs):
        result = initial(*args, **kwargs)
        flags["reads"] += 1
        if flags["reads"] == 3:
            page.evaluate("() => {" + changes[change] + ";}")
        return result
    monkeypatch.setattr(worker, "_image_readback", readback)
    payload = {"destination": "fixture_owner", "request_id": "post-mutated-during-readback", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == "PIKABU_READBACK_CHANGED_DURING_IMAGE_READBACK"
    assert state["posts"] == state["uploads"] == 1
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == state["uploads"] == 1


@pytest.mark.parametrize("phase", ["editor_mutation_intent", "image_upload_intent", "click_intent"])
def test_revoked_authorization_during_durable_intent_stops_next_external_action(
        tmp_path, post, browser_fixture, monkeypatch, phase):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    initial = worker._save
    flags = {"revoked": False}
    def save(path, journal, saved_state, **values):
        result = initial(path, journal, saved_state, **values)
        if saved_state == phase:
            flags["revoked"] = True
        return result
    def authorizer():
        if flags["revoked"]:
            raise AgentError("PIKABU_DISPATCH_NOT_APPROVED", 403)
    monkeypatch.setattr(worker, "_save", save)
    payload = {"destination": "fixture_owner", "request_id": "revoke-after-fsync", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, authorizer, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == "PIKABU_DISPATCH_NOT_APPROVED"
    assert state["posts"] == 0
    assert state["uploads"] == (1 if phase == "click_intent" else 0)
    if phase == "editor_mutation_intent":
        assert page.locator("textarea").input_value() == ""
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == 0
    assert state["uploads"] == (1 if phase == "click_intent" else 0)


@pytest.mark.parametrize("change,code", [
    ("body", "PIKABU_EDITOR_CHANGED_BEFORE_PUBLISH"),
    ("community", "PIKABU_EDITOR_CHANGED_BEFORE_PUBLISH"),
    ("mode", "PIKABU_PUBLISH_MODE_UNEXPECTED"),
    ("owner", "PIKABU_ACCOUNT_MISMATCH"),
    ("disabled", "PIKABU_PUBLISH_BUTTON_DISABLED"),
    ("pending_tag", "PIKABU_EDITOR_PENDING_TAG"),
])
def test_final_authorizer_dom_change_is_checked_before_single_publish_click(
        tmp_path, post, browser_fixture, change, code):
    page, origin, options, state = browser_fixture
    payload = {"destination": "fixture_owner", "request_id": "last-auth-dom-change", "snapshot": post}
    mutations = {
        "body": "document.querySelector('.editor p').textContent='Подменённый текст'",
        "community": "document.querySelector('.community-combo input').value='Чужое сообщество'",
        "mode": "document.body.insertAdjacentHTML('beforeend','<input type=checkbox data-test=setting-anonymous checked>')",
        "owner": "document.querySelector('.account').href='/@someone_else'",
        "disabled": "document.querySelector('[data-test=story-publish]').disabled=true",
        "pending_tag": "document.querySelector('#tags').value='Несогласованный тег'",
    }
    def authorizer():
        path = tmp_path / "data/pikabu-operations/last-auth-dom-change.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] == "click_intent":
            page.evaluate("() => {" + mutations[change] + ";}")
    result = run_publish(page, tmp_path, options, payload, authorizer, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == code
    assert state["uploads"] == 1 and state["posts"] == 0

    assert _read_journal(tmp_path, payload["request_id"])[1]["clicks"] == 1
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["uploads"] == 1 and state["posts"] == 0


@pytest.mark.parametrize("code", ["TARGET_PAUSED", "APPROVAL_STALE", "PIKABU_DISPATCH_NOT_APPROVED"])
def test_revocation_during_slow_final_dom_guard_is_authorized_again_before_publish(
        tmp_path, post, browser_fixture, monkeypatch, code):
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    post["media"] = []
    path = tmp_path / "data/pikabu-operations/slow-final-revoke.json"
    original = worker._final_editor
    flags = {"revoked": False, "checked_after_revoke": 0}
    def final(*args, **kwargs):
        button = original(*args, **kwargs)
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] == "click_intent":
            flags["revoked"] = True
        return button
    def authorizer():
        if flags["revoked"]:
            flags["checked_after_revoke"] += 1
            raise AgentError(code, 409)
    monkeypatch.setattr(worker, "_final_editor", final)
    payload = {"destination": "fixture_owner", "request_id": "slow-final-revoke", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, authorizer, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["evidence"]["error"] == code and flags["checked_after_revoke"] == 1
    assert state["posts"] == state["uploads"] == 0
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == state["uploads"] == 0


def test_slow_atomic_final_capture_expires_before_native_publish(tmp_path, post, browser_fixture, monkeypatch):
    import time
    import max_postiz_agent.pikabu_browser_worker as worker
    page, origin, options, state = browser_fixture
    post["media"] = []
    path = tmp_path / "data/pikabu-operations/slow-atomic.json"
    original = worker._atomic_action_state
    def delayed(*args, **kwargs):
        captured = original(*args, **kwargs)
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))["state"] == "click_intent":
            time.sleep(1.05)
        return captured
    monkeypatch.setattr(worker, "_atomic_action_state", delayed)
    payload = {"destination": "fixture_owner", "request_id": "slow-atomic", "snapshot": post}
    result = run_publish(page, tmp_path, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["evidence"]["error"] == "PIKABU_ACTION_GUARD_EXPIRED"
    assert state["posts"] == state["uploads"] == 0
    assert _read_journal(tmp_path, payload["request_id"])[1]["clicks"] == 1
    assert run_publish(page, tmp_path, options, payload, lambda: None,
                       origin=origin, fixture=True)["state"] == "unknown"
    assert state["posts"] == state["uploads"] == 0
