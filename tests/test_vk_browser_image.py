"""VK native-image fixtures: loopback only, never a real account or publication."""
import hashlib
import html
import io
import json
import re
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from PIL import Image

from max_postiz_agent import vk_browser_worker as worker
from max_postiz_agent.config import AgentError
from max_postiz_agent.vk_browser import VKBrowser, dom_contract, prepare
from max_postiz_agent.vk_media import derivative_proof, upload_binding
from test_vk_browser import browser as browser, page as page
from test_vk_media import image_case as image_case


NASA_URLS = ["https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html",
             "https://nssdc.gsfc.nasa.gov/planetary/factsheet/moonfact.html"]


def native_caption_markup(text, *, expanded=False):
    content = html.escape(text)
    for url in set(re.findall(r'https?://[^\s<>"\']+', text)):
        label = "nssdc.gsfc.nasa.gov/planetary/..." if url in NASA_URLS else url.split("://", 1)[1]
        anchor = '<a href="/away.php?to=' + quote(url, safe="") + '&amp;utf=1">' + html.escape(label) + '</a>'
        content = content.replace(html.escape(url), anchor)
    content = content.replace("\n\n", '<div style="display:block;width:0"><br> </div>').replace("\n", "<br>")
    control = ('' if expanded else '<div data-testid="showmoretext-after" style="display:block">'
               '<span role="button" tabindex="0" data-id="showMoreButton">Показать ещё</span></div>')
    return ('<span data-testid="post_text" style="display:block"><div data-testid="showmoretext" style="display:block">'
        '<div data-testid="' + ('showmoretext-in-expanded' if expanded else 'showmoretext-in')
        + '" style="display:flow-root"><div style="display:inline">' + content + '</div></div>' + control + '</div></span>')


@pytest.mark.parametrize("damage", [None, "expanded", "native_inline", "control_label", "control_extra", "hidden_link_child",
    "alias", "wrong_to", "extra_query", "sibling_text", "extra_media", "extra_wrapper", "hidden_body",
    "control_comment", "expanded_control"])
def test_native_caption_observed_show_more_and_short_urls_are_exact_and_fail_closed(image_case, page, damage):
    root, _, options, snapshot, _ = image_case
    snapshot["body"] = "NASA data.\n" + "\n".join(NASA_URLS)
    plan = prepare(root, options, "123456", snapshot)
    markup = native_caption_markup(plan["text"], expanded=damage == "expanded")
    if damage == "native_inline":
        markup = markup.replace('style="display:flow-root"', 'style="display:inline"', 1)
    elif damage == "control_label":
        markup = markup.replace("Показать ещё", "Another control")
    elif damage == "control_extra":
        markup = markup.replace("Показать ещё</span>", "Показать ещё</span>Extra")
    elif damage == "hidden_link_child":
        markup = markup.replace("nssdc.gsfc.nasa.gov/planetary/...</a>",
            'nssdc.gsfc.nasa.gov/planetary/...<span style="display:none">hidden</span></a>', 1)
    elif damage == "alias":
        markup = markup.replace("nssdc.gsfc.nasa.gov/planetary/...</a>", "Earth</a>", 1)
    elif damage == "wrong_to":
        markup = markup.replace("earthfact.html", "other.html", 1)
    elif damage == "extra_query":
        markup = markup.replace("&amp;utf=1", "&amp;utf=1&amp;unsafe=1", 1)
    elif damage == "sibling_text":
        markup = markup.replace('<div data-testid="showmoretext-after"', 'Extra<div data-testid="showmoretext-after"', 1)
    elif damage == "extra_media":
        markup = markup.replace("NASA data.", 'NASA data.<img src="data:image/png,not-an-image">', 1)
    elif damage == "extra_wrapper":
        markup = markup.replace("NASA data.", '<div>NASA data.</div>', 1)
    elif damage == "hidden_body":
        markup = markup.replace('style="display:inline"', 'style="display:inline;opacity:0"', 1)
    elif damage == "control_comment":
        markup = markup.replace("Показать ещё</span>", "Показать ещё<!--unknown--></span>", 1)
    elif damage == "expanded_control":
        markup = markup.replace('data-testid="showmoretext-in"', 'data-testid="showmoretext-in-expanded"', 1)
    page.context.route("**/*", lambda route: route.abort())
    page.set_content(markup)
    text = page.locator('[data-testid="post_text"]')
    if damage in {None, "expanded", "native_inline"}:
        assert worker._caption_text(text, plan, dom_contract(options)) == plan["text"]
        assert worker._text_links(text, plan) == NASA_URLS
    else:
        with pytest.raises(AgentError):
            worker._caption_text(text, plan, dom_contract(options))


def transforms():
    return {"ratio": 4 / 3, "width": 320, "height": 240, "scale": 1 + 2e-16,
            "x": 1e-14, "y": -1e-14, "container_width": 320, "container_height": 240}


@pytest.mark.parametrize("damage", ["outside", "hidden_asset", "wrong_owner", "extra_image", "crop", "url_pair"])
def test_atomic_native_capture_rejects_cross_asset_or_unknown_preview(image_case, damage):
    root, _, options, snapshot, _ = image_case
    plan = prepare(root, options, "123456", snapshot)
    item = {"url": "https://sun1.vkuserphoto.ru/a.jpg", "complete": True, "width": 64, "height": 48,
            "visible": True, "contained": True, "box_width": 320, "box_height": 240}
    captured = {"asset_count": 1, "asset_visible": True, "unsupported": False, "grid_ok": True,
        "asset_image_count": 1, "asset_value": "123456_77", "transform": transforms(), "images": [item]}
    stage = "uploaded"
    if damage == "outside":
        item["contained"] = False
    elif damage == "hidden_asset":
        captured["asset_visible"] = False
    elif damage == "wrong_owner":
        captured["asset_value"] = "999_77"
    elif damage == "extra_image":
        captured["asset_image_count"] = 2
    elif damage == "crop":
        captured["transform"]["scale"] = 1.1
    else:
        stage = "preview"
        captured.update(asset_image_count=2, images=[item, {**item, "url": "https://sun1.vkuserphoto.ru/b.jpg"}])
    class Scope:
        def locator(self, selector):
            assert selector == "html"
            return self
        def evaluate(self, script, argument):
            assert "asset.contains(n)" in script  # A single DOM task captures both objects.
            return captured
    with pytest.raises(AgentError):
        worker._native_images(Scope(), plan, dom_contract(options), stage, origin="https://vk.ru")


@pytest.mark.parametrize("damage", [None, "asset", "source", "upload_count", "second_reload", "original_url", "missing_viewer", "fake_source_sha"])
def test_native_image_adapter_requires_bound_upload_and_two_exact_original_proofs(image_case, damage):
    root, raw, options, snapshot, _ = image_case
    plan = prepare(root, options, "123456", snapshot)
    ident, request = "123456_51", "fixture-image-protocol"
    original = derivative_proof(raw, plan["source_image"], "123456_77", plan["image_contract"])
    original.update(original_url="https://sun1.vkuserphoto.ru/a.jpg", viewer_sha256="a" * 64)
    upload = upload_binding(plan, request, "123456_77", "https://sun1.vkuserphoto.ru/a.jpg", hashlib.sha256(raw).hexdigest())
    author = {"id": "123456", "href": "/id123456", "id_attribute": None, "numeric_id": None}
    reload = {"external_id": ident, "text_sha256": plan["text_sha256"], "image_proofs": [original],
              "author": author, "text_links": [], "link_contract": plan["link_contract"]}
    proof = {"external_id": ident, "permalink": "https://vk.ru/wall" + ident, "title_format": "plain",
        "observed_at": 1000, "upload": upload, "reloads": [deepcopy(reload), deepcopy(reload)],
        "verification": "independent_wall_reload_exact_text_recipient_author_native_photo_v1"}
    if damage == "asset":
        proof["reloads"][1]["image_proofs"][0]["asset_id"] = "123456_78"
    elif damage == "source":
        upload["source_sha256"] = "0" * 64
    elif damage == "upload_count":
        upload["uploads"] = True
    elif damage == "second_reload":
        proof["reloads"].pop()
    elif damage == "original_url":
        proof["reloads"][1]["image_proofs"][0]["original_url"] = "https://evil.test/a.jpg"
    elif damage == "missing_viewer":
        proof["reloads"][1]["image_proofs"][0].pop("viewer_sha256")
    elif damage == "fake_source_sha":
        proof["reloads"][1]["image_sha256"] = [plan["media"][0]["sha256"]]
    value = {"state": "confirmed", "verified": True, "external_id": ident, "permalink": proof["permalink"],
        "evidence": {"contract": "vk-browser-single-click-v1", "request_id": request, "fixture": False,
            "independent_readback": True, "verification": "browser_readback", "journal_state": "confirmed",
            "snapshot_sha256": plan["snapshot_sha256"], "clicks": 1, "proof": proof}}
    result = VKBrowser._delivery(value, "123456", request, plan, expected_fixture=False)
    assert result.state == ("confirmed" if damage is None else "unknown")
    assert result.verified is (damage is None)


STYLE = "--photo-primary-ratio:1.3333333333333333;--photo-width:320px;--photo-height:240px;--photo-scale:1;--photo-translate-x:0px;--photo-translate-y:0px;--photo-container-width:320px;--photo-container-height:240px;"


@pytest.fixture
def native_site(image_case):
    root, raw, options, snapshot, seal = image_case
    # Use the observed selectors themselves; only network/account data is local.
    options["dom_contract"] = {}
    options["operation_timeout_seconds"] = 10
    seal()
    state = {"uploads": 0, "publishes": 0, "viewer_before_publish": 0, "wall_reads": 0,
              "photo_views": 0, "wrong_public_asset": False, "cross_asset": False, "duplicate": False,
              "wrong_author": False, "post_delay_ms": 0, "account_change_ms": 0, "short_native": False,
              "old_history": False, "actual_raw": None, "original_crop": None, "main_preview_raw": None,
              "main_preview_width": None, "text": "", "reseal": seal}
    snapshot["body"] += "\n\nИсточник: https://science.example.test/a"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, value, content_type="text/html; charset=utf-8"):
            data = value.encode() if isinstance(value, str) else value
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/upload":
                assert payload == raw
                state["uploads"] += 1
                return self.send(json.dumps({"id": "photo123456_77", "url": origin + "/media.png"}), "application/json")
            assert self.path == "/publish"
            value = json.loads(payload)
            assert value["story"] is False
            state["publishes"] += 1
            state["text"] = value["text"]
            return self.send("{}", "application/json")
        def do_GET(self):
            if self.path.split("?", 1)[0] in {"/media.png", "/original.png"}:
                if (self.path.startswith("/original.png") and state["main_preview_raw"]
                        and parse_qs(urlsplit(self.path).query).get("cs") == [str(state["main_preview_width"]) + "x0"]):
                    return self.send(state["main_preview_raw"], "image/png")
                return self.send(state["actual_raw"] if self.path.startswith("/original.png") and state["actual_raw"] else raw, "image/png")
            account = '<a data-testid="header-profile-menu-button" href="/id123456">Автор</a>'
            if self.path.startswith("/photo"):
                state["photo_views"] += 1
                state["viewer_before_publish"] += int(state["publishes"] == 0)
                original = origin + '/original.png'
                if state["original_crop"]:
                    original += '?crop=' + ','.join(str(n) for n in state["original_crop"])
                main = original
                if state["main_preview_width"]:
                    original += '&cs=' + str(state["original_crop"][2]) + 'x0'
                    main += '&cs=' + str(state["main_preview_width"]) + 'x0'
                return self.send(account + '<div id="pv_box" class="_scroll_node pv_box">'
                    '<div id="pv_photo"><img data-testid="pv_photo_image" src="' + main + '"></div>'
                    '<div id="pv_tag_frame"><img src="' + origin + '/media.png"></div>'
                    '<button onclick="document.getElementById(\'pv_more_act_download\').hidden=false">Ещё </button>'
                    '<a id="pv_more_act_download" hidden href="' + original + '\n  ">Открыть оригинал</a></div>')
            state["wall_reads"] += 1
            post = ""
            if state["publishes"]:
                photo_id = "123456_78" if state["wrong_public_asset"] else "123456_77"
                post = '<article data-testid="post" data-post-id="123456_51"><a data-testid="post-header-title" href="/id123456">Автор</a>'
                text = html.escape(state["text"]).replace("https://science.example.test/a",
                    '<a href="/away.php?to=' + quote("https://science.example.test/a", safe="")
                    + '&amp;utf=1">https://science.example.test/a</a>')
                post += (native_caption_markup(state["text"]) if state["short_native"] else
                         '<span data-testid="post_text" style="display:block;white-space:pre-wrap">' + text + '</span>')
                post += '<a data-testid="post_date_block_preview" href="/wall123456_51">Сейчас</a><div data-testid="post-content-container">'
                post += '<a data-testid="primary-attachment-interactive-wrapper" href="/photo' + photo_id + '">'
                post += '<img data-testid="primary-attachment-image-content" style="width:64px;height:48px" src="' + origin + '/media.png"></a></div></article>'
                if state["cross_asset"]:
                    post = post.replace('<a data-testid="primary-attachment-interactive-wrapper" href="/photo' + photo_id + '">', '')
                    post = post.replace('data-testid="primary-attachment-image-content"', 'data-testid="body-photo"')
                    post = post.replace('</a></div></article>', '</div><a style="display:none" data-testid="primary-attachment-interactive-wrapper" href="/photo123456_77"><img src="'
                        + origin + '/media.png"></a></article>')
                if state["wrong_author"]:
                    post = post.replace('data-testid="post-header-title" href="/id123456"',
                                        'data-testid="post-header-title" href="/id999999"')
                if state["duplicate"]:
                    post += post.replace("123456_51", "123456_52")
                if state["post_delay_ms"]:
                    post = ('<div id="history"></div><script>setTimeout(()=>document.getElementById("history").innerHTML='
                            + json.dumps(post) + ',' + str(state["post_delay_ms"]) + ')</script>')
            if state["old_history"]:
                post = ('<article data-testid="post" data-post-id="123456_12"><span data-testid="post_text">'
                    '<custom-widget>Old unrelated history</custom-widget><a href="/away.php?to=http%3A%2F%2Fradio.example%2F">Radio</a>'
                    '</span><a data-testid="post_date_block_preview" href="/wall123456_12">Old date</a></article>') + post
            if state["account_change_ms"]:
                post += ('<script>setTimeout(()=>document.querySelector("[data-testid=header-profile-menu-button]").href="/id999999",'
                         + str(state["account_change_ms"]) + ')</script>')
            data = json.dumps({"origin": origin, "style": STYLE})
            # Bounded fixture layout represents the observed clipped underlay;
            # this CSS is test scaffolding, not a claimed native stylesheet capture.
            return self.send('<!doctype html><meta charset="utf-8"><style>[hidden]{display:none!important}#editor{display:block;white-space:pre-wrap;min-height:80px}.attachment{position:relative;overflow:hidden;width:320px;height:240px}img.main{display:block;position:relative;width:320px;height:240px}</style>'
                + account + post + '<button data-testid="posting_create_post_button" onclick="document.getElementById(\'modal\').hidden=false">Новая запись</button>'
                '<div id="modal" hidden role="dialog" data-testid="posting_modal_box" aria-label="Форма постинга">'
                '<div id="base"><span id="editor" contenteditable="true" role="textbox" data-testid="posting_base_screen_input_message"></span>'
                '<input type="file" multiple accept="video/*,image/jpeg,image/png" data-testid="posting_base_screen_download_from_device">'
                '<div id="uploaded"></div><button data-testid="posting_base_screen_next">Далее</button></div>'
                '<div id="settings" hidden><div data-testid="showmoretext-in" style="display:flow-root"></div>'
                '<div data-testid="grid-item" id="preview"></div><div data-testid="posting_coowners_cell">Пригласить соавторов</div>'
                '<div data-testid="posting_settings_visibility">Кто увидит этот пост Все</div>'
                '<label style="display:block;width:40px;height:24px;position:relative"><input type="checkbox" role="switch" checked aria-checked="true" data-testid="posting_settings_repost_to_story_switch" style="opacity:0;pointer-events:none;position:absolute;width:40px;height:24px"><span>Story</span></label>'
                '<button data-testid="posting_submit_button">Опубликовать</button></div></div>'
                '<script>const c=' + data + ';const modal=document.getElementById("modal"),e=document.getElementById("editor");'
                'const img=(klass="main")=>`<img class="${klass}" src="${c.origin}/media.png">`;'
                'document.querySelector("input[type=file]").onchange=async ev=>{const r=await fetch("/upload",{method:"POST",body:ev.target.files[0]});const d=await r.json();'
                'document.getElementById("uploaded").innerHTML=`<div data-sortable-item="true" data-sortable-id="${d.id}"><div class="attachment" data-testid="posting_attachment_item" style="${c.style}">${img()}</div></div>`;};'
                'document.querySelector("[data-testid=posting_base_screen_next]").onclick=()=>{window.caption=e.innerText;document.getElementById("base").remove();document.getElementById("settings").hidden=false;'
                'const out=document.querySelector("[data-testid=showmoretext-in]");window.caption.split(String.fromCharCode(10,10)).forEach((p,i)=>{if(i){const d=document.createElement("div");d.append(document.createElement("br"));out.append(d);}'
                'p.split(" ").forEach((word,j)=>{if(j)out.append(document.createTextNode(" "));if(word.startsWith("https://")){const a=document.createElement("a");a.href="/away.php?to="+encodeURIComponent(word)+"&utf=1";a.textContent=word;out.append(a);}else out.append(document.createTextNode(word));});});'
                'document.getElementById("preview").innerHTML=`<div class="attachment" data-testid="posting_preview_attachment_item" style="${c.style}"><img style="position:absolute;width:640px;height:480px" src="${c.origin}/media.png">${img()}</div>`;};'
                'document.querySelector("[role=switch]").onchange=ev=>ev.target.setAttribute("aria-checked",String(ev.target.checked));'
                'document.querySelector("[data-testid=posting_submit_button]").onclick=async()=>{await fetch("/publish",{method:"POST",body:JSON.stringify({text:window.caption,story:document.querySelector("[role=switch]").checked})});location.href="/id123456";};</script>')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = "http://127.0.0.1:" + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, options, snapshot, state, origin
    finally:
        server.shutdown()
        server.server_close()


def test_real_native_image_upload_preview_original_two_reloads_and_no_repeat(native_site, page):
    root, options, snapshot, state, origin = native_site
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    payload = {"destination": "123456", "request_id": "native-image-full", "snapshot": snapshot}
    result = worker.run_publish(page, root, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "confirmed", {"result": result,
        "editor_text": worker._text(page.locator('[data-testid="posting_base_screen_input_message"]'))
            if page.locator('[data-testid="posting_base_screen_input_message"]').count() else "editor_unmounted",
        "expected": prepare(root, options, "123456", snapshot)["text"]}
    assert result["verified"] is False
    assert state["uploads"] == state["publishes"] == 1 and state["viewer_before_publish"] == 0
    assert state["photo_views"] == 2 and len(result["evidence"]["proof"]["reloads"]) == 2
    before = deepcopy(state)
    assert worker.run_publish(page, root, options, payload, lambda: None, origin=origin, fixture=True) == result
    assert state == before


def test_real_native_wrong_public_asset_is_unknown_and_never_reuploaded(native_site, page):
    root, options, snapshot, state, origin = native_site
    state["wrong_public_asset"] = True
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    payload = {"destination": "123456", "request_id": "native-image-wrong-photo", "snapshot": snapshot}
    result = worker.run_publish(page, root, options, payload, lambda: None, origin=origin, fixture=True)
    assert result["state"] == "unknown", result
    assert state["uploads"] == state["publishes"] == 1
    before = deepcopy(state)
    assert worker.run_publish(page, root, options, payload, lambda: None, origin=origin, fixture=True)["state"] == "unknown"
    assert state == before


def test_real_native_cross_asset_hidden_anchor_cannot_validate_visible_other_photo(native_site, page):
    root, options, snapshot, state, origin = native_site
    state["cross_asset"] = True
    options["dom_contract"]["post_images"] = '[data-testid="body-photo"]'
    state["reseal"]()
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    result = worker.run_publish(page, root, options,
        {"destination": "123456", "request_id": "native-cross-asset", "snapshot": snapshot}, lambda: None,
        origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["verified"] is False
    assert state["uploads"] == state["publishes"] == 1 and state["photo_views"] == 0
    assert result["evidence"]["proof"] is None


@pytest.mark.parametrize("change", ["caption", "crop", "url", "pause"])
def test_real_native_change_during_preview_read_prevents_publish(native_site, page, monkeypatch, change):
    root, options, snapshot, state, origin = native_site
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    read = worker._cdn_bytes
    flags = {"mutated": False}
    def cdn(*args, **kwargs):
        data = read(*args, **kwargs)
        if page.locator('[data-testid="posting_preview_attachment_item"]').count() and not flags["mutated"]:
            flags["mutated"] = True
            if change == "caption":
                page.locator('[data-testid="showmoretext-in"]').evaluate("e=>e.append('Extra')")
            elif change == "crop":
                page.locator('[data-testid="posting_preview_attachment_item"]').evaluate("e=>e.style.setProperty('--photo-scale','1.1')")
            elif change == "url":
                page.locator('[data-testid="posting_preview_attachment_item"] img').evaluate_all("nodes=>nodes.forEach(e=>e.src=e.src+'?changed=1')")
        return data
    def authorize():
        if change == "pause" and flags["mutated"]:
            raise AgentError("EDITORIAL_PAUSED")
    monkeypatch.setattr(worker, "_cdn_bytes", cdn)
    payload = {"destination": "123456", "request_id": "native-preview-" + change, "snapshot": snapshot}
    result = worker.run_publish(page, root, options, payload, authorize, origin=origin, fixture=True)
    assert flags["mutated"] is True and result["state"] == "unknown"
    assert state["uploads"] == 1 and state["publishes"] == state["photo_views"] == 0
    before = deepcopy(state)
    assert worker.run_publish(page, root, options, payload, lambda: None, origin=origin, fixture=True)["state"] == "unknown"
    assert state == before


def seed_unknown_image(root, options, snapshot, origin, request="native-image-no-id"):
    plan = prepare(root, options, "123456", snapshot)
    path = worker._path(root, request)
    path.parent.mkdir(parents=True, exist_ok=True)
    journal = {"contract": worker.CONTRACT, "root": str(root.resolve()), "request_id": request,
        "destination": "123456", "snapshot": snapshot, "snapshot_sha256": plan["snapshot_sha256"],
        "options_sha256": worker.fingerprint(options), "dom_contract_sha256": plan["dom_contract_sha256"],
        "state": "unknown", "clicks": 1, "created_at": time.time(), "fixture": True,
        "upload_proof": upload_binding(plan, request, "123456_77", origin + "/media.png",
                                       plan["source_image"]["source_sha256"], origin=origin)}
    worker._save(path, journal, "unknown")
    return path, journal, plan


@pytest.mark.parametrize("damage", [None, "missing", "source", "request", "origin", "click_zero", "click_bool",
                                    "dom", "no_post", "duplicate", "asset", "text", "author"])
def test_native_no_id_reconcile_discovers_only_exact_claimed_post_read_only(native_site, page, damage):
    root, options, snapshot, state, origin = native_site
    path, journal, plan = seed_unknown_image(root, options, snapshot, origin)
    state.update(publishes=1, text=plan["text"])
    if damage == "missing":
        journal.pop("upload_proof")
    elif damage in {"source", "request", "origin"}:
        field, value = {"source": ("source_sha256", "0" * 64), "request": ("request_id", "other"),
                        "origin": ("execution_origin", "http://127.0.0.1:1")}[damage]
        journal["upload_proof"][field] = value
    elif damage in {"click_zero", "click_bool"}:
        journal["clicks"] = 0 if damage == "click_zero" else True
    elif damage == "dom":
        journal["dom_contract_sha256"] = "0" * 64
    elif damage == "no_post":
        state["publishes"] = 0
    elif damage == "duplicate":
        state["duplicate"] = True
    elif damage == "asset":
        state["wrong_public_asset"] = True
    elif damage == "text":
        state["text"] = "Different caption"
    elif damage == "author":
        state["wrong_author"] = True
    worker._save(path, journal, "unknown")
    mutations = []
    def route(request):
        if request.request.method not in {"GET", "HEAD"}:
            mutations.append(request.request.method)
            request.abort()
        elif request.request.url.startswith(origin + "/"):
            request.continue_()
        else:
            request.abort()
    page.context.route("**/*", route)
    count = state["publishes"]
    result = worker.reconcile(page, root, options, "123456", journal["request_id"], origin=origin, fixture=True)
    assert result["state"] == ("confirmed" if damage is None else "unknown"), result
    assert result["verified"] is False and state["uploads"] == 0 and state["publishes"] == count
    assert mutations == []
    if damage is None:
        assert result["external_id"] == "123456_51" and state["photo_views"] == 2
        _, saved = worker._read(root, journal["request_id"])
        assert saved["external_id"] == saved["proof"]["external_id"] == "123456_51"
    else:
        assert result["external_id"] is None and state["photo_views"] == 0


@pytest.mark.parametrize("read_error", [False, True])
def test_native_no_id_reconcile_does_not_overwrite_changed_sealed_journal(native_site, page, monkeypatch, read_error):
    root, options, snapshot, state, origin = native_site
    path, journal, plan = seed_unknown_image(root, options, snapshot, origin)
    state.update(publishes=1, text=plan["text"])
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    check = worker._native_image_proof
    calls = []
    def changed(*args, **kwargs):
        result = check(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            worker._save(path, journal, "unknown", failure_code="VK_BROWSER_CONCURRENT_TEST")
            if read_error:
                raise AgentError("VK_BROWSER_READBACK_CHANGED")
        return result
    monkeypatch.setattr(worker, "_native_image_proof", changed)
    result = worker.reconcile(page, root, options, "123456", journal["request_id"], origin=origin, fixture=True)
    assert result["state"] == "unknown" and result["external_id"] is None
    _, saved = worker._read(root, journal["request_id"])
    assert saved["failure_code"] == "VK_BROWSER_CONCURRENT_TEST" and "external_id" not in saved
    assert state["uploads"] == 0 and state["publishes"] == 1


def test_native_no_id_reconcile_rejects_fixture_flip_before_browser_access(native_site):
    root, options, snapshot, state, origin = native_site
    path, journal, _ = seed_unknown_image(root, options, snapshot, origin)
    journal["fixture"] = False
    worker._save(path, journal, "unknown")
    with pytest.raises(AgentError, match="VK_BROWSER_JOURNAL_CONFLICT"):
        worker.reconcile(None, root, options, "123456", journal["request_id"], origin=origin, fixture=True)
    assert state["wall_reads"] == state["uploads"] == state["publishes"] == state["photo_views"] == 0


@pytest.mark.parametrize("case", ["delayed", "never", "account"])
def test_native_no_id_discovery_waits_only_for_absent_asset_and_rechecks_account(native_site, page, case):
    root, options, snapshot, state, origin = native_site
    options["operation_timeout_seconds"] = 1
    state["reseal"]()
    _, journal, plan = seed_unknown_image(root, options, snapshot, origin)
    state.update(publishes=1, text=plan["text"], post_delay_ms=300)
    if case == "never":
        state["publishes"] = 0
    elif case == "account":
        state.update(post_delay_ms=500, account_change_ms=100)
    mutations = []
    def route(request):
        if request.request.method not in {"GET", "HEAD"}:
            mutations.append(request.request.method)
            request.abort()
        elif request.request.url.startswith(origin + "/"):
            request.continue_()
        else:
            request.abort()
    page.context.route("**/*", route)
    published = state["publishes"]
    result = worker.reconcile(page, root, options, "123456", journal["request_id"], origin=origin, fixture=True)
    assert result["state"] == ("confirmed" if case == "delayed" else "unknown"), result
    assert result["verified"] is False and state["uploads"] == 0 and state["publishes"] == published
    assert mutations == [] and state["wall_reads"] == (3 if case == "delayed" else 1)
    if case == "account":
        assert result["evidence"]["failure_code"] == "VK_BROWSER_ACCOUNT_MISMATCH"


def test_native_no_id_read_only_recovery_short_nasa_links_and_unrelated_history(native_site, page):
    root, options, snapshot, state, origin = native_site
    snapshot["body"] = "NASA data.\n" + "\n".join(NASA_URLS)
    _, journal, plan = seed_unknown_image(root, options, snapshot, origin)
    state.update(publishes=1, text=plan["text"], short_native=True, old_history=True)
    mutations = []
    def route(request):
        if request.request.method not in {"GET", "HEAD"}:
            mutations.append(request.request.method)
            request.abort()
        elif request.request.url.startswith(origin + "/"):
            request.continue_()
        else:
            request.abort()
    page.context.route("**/*", route)
    result = worker.reconcile(page, root, options, "123456", journal["request_id"], origin=origin, fixture=True)
    assert result["state"] == "confirmed" and result["verified"] is False, result
    assert result["external_id"] == "123456_51" and state["uploads"] == 0 and state["publishes"] == 1
    assert state["photo_views"] == 2 and state["wall_reads"] == 3 and mutations == []
    proof = result["evidence"]["proof"]
    assert len(proof["reloads"]) == 2
    assert all(item["text_links"] == NASA_URLS and item["text_sha256"] == plan["text_sha256"]
               and item["link_contract"] == "vk-native-caption-away-prefix-v2" for item in proof["reloads"])


@pytest.mark.parametrize("extra", ['<span style="display:none">Extra</span>', '<span>Extra</span>', '<custom-widget></custom-widget>'])
def test_native_image_matcher_never_skips_possible_caption_with_unapproved_insertions(image_case, page, extra):
    root, _, options, snapshot, _ = image_case
    snapshot["body"] = "NASA data.\n" + "\n".join(NASA_URLS)
    plan = prepare(root, options, "123456", snapshot)
    markup = native_caption_markup(plan["text"]).replace("NASA data.", "NASA data." + extra)
    page.context.route("**/*", lambda route: route.abort())
    page.set_content('<article data-testid="post" data-post-id="123456_51">' + markup
                     + '<a data-testid="post_date_block_preview" href="/wall123456_51">Date</a></article>')
    if "<span>" in extra:
        assert worker._matches(page, plan, dom_contract(options), origin="https://vk.ru") == []
    else:
        with pytest.raises(AgentError):
            worker._matches(page, plan, dom_contract(options), origin="https://vk.ru")


def test_native_final_post_caption_capture_must_still_equal_frozen_text(native_site, page, monkeypatch):
    root, options, snapshot, state, origin = native_site
    plan = prepare(root, options, "123456", snapshot)
    state.update(publishes=1, text=plan["text"])
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    worker._navigate(page, origin + "/id123456")
    calls = []
    read = worker._caption_text
    def caption(*args):
        calls.append(True)
        return read(*args) if len(calls) == 1 else "Changed after candidate match"
    monkeypatch.setattr(worker, "_caption_text", caption)
    with pytest.raises(AgentError, match="VK_BROWSER_READBACK_TEXT_MISMATCH"):
        worker._post_snapshot(page, plan, dom_contract(options), "123456_51", origin=origin)
    assert state["uploads"] == 0 and state["publishes"] == 1 and state["photo_views"] == 0


@pytest.mark.parametrize("damage", [None, "responsive", "no_review", "actual_bytes", "crop", "review_change", "second_bytes", "post_id", "revoked",
                                    "known_concurrent", "known_concurrent_error"])
def test_native_existing_actual_image_acceptance_is_exact_read_only_and_fixture_unverified(native_site, page, monkeypatch, damage):
    from max_postiz_agent.vk_media_review import ACTION, CONTRACT, VERIFICATION
    root, options, snapshot, state, origin = native_site
    snapshot["body"] = "NASA data.\n" + "\n".join(NASA_URLS)
    path, journal, plan = seed_unknown_image(root, options, snapshot, origin)
    known = damage in {"known_concurrent", "known_concurrent_error"}
    if known:
        worker._save(path, journal, "unknown", external_id="123456_51", permalink="https://vk.ru/wall123456_51")
    def png(color):
        target = io.BytesIO()
        Image.new("RGB", (63, 47), color).save(target, "PNG")
        return target.getvalue()
    actual = png("navy")
    details = {"contract": CONTRACT, "request_id": journal["request_id"], "snapshot_hash": "f" * 64,
        "snapshot_sha256": plan["snapshot_sha256"], "original_approval_id": "fixture-only",
        "original_due_at": time.time() - 120, "owner": "123456", "destination": "123456",
        "external_id": "123456_51", "permalink": "https://vk.ru/wall123456_51", "asset_id": "123456_77",
        "source_sha256": plan["source_image"]["source_sha256"], "source_dimensions": [64, 48],
        "actual_sha256": hashlib.sha256(actual).hexdigest(), "actual_dimensions": [63, 47],
        "native_crop_box": [0, 0, 63, 47], "consent_path": "data/fixture-only.json", "consent_sha256": "0" * 64,
        "approved_at": time.time() - 1, "existing_post_only": True, "new_publications_authorized": False,
        "future_policy_change_authorized": False, "original_policy_satisfied": False}
    authority = {"id": "fixture-owner-review", "actor": "owner", "action": ACTION,
        "entity": journal["request_id"], "at": time.time(), "details": details}
    state.update(publishes=1, text=plan["text"], short_native=True, old_history=True,
                 actual_raw=actual, original_crop=[0, 0, 63, 47])
    if damage == "responsive":
        preview = io.BytesIO()
        Image.new("RGB", (60, 45), "navy").save(preview, "PNG")
        state.update(main_preview_raw=preview.getvalue(), main_preview_width=60)
    elif damage == "actual_bytes":
        state["actual_raw"] = png("red")
    elif damage == "crop":
        state["original_crop"] = [0, 0, 62, 47]
    elif damage == "post_id":
        authority["details"]["external_id"] = "123456_52"
    loads = []
    def loader():
        loads.append(True)
        if len(loads) > 1 and damage == "review_change":
            return {**authority, "id": "changed-owner-review"}
        if len(loads) > 1 and damage == "revoked":
            raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_INVALID")
        return authority
    original = worker._photo_original
    checks = []
    def read_original(*args, **kwargs):
        result = original(*args, **kwargs)
        checks.append(True)
        if len(checks) == 1 and damage == "second_bytes":
            state["actual_raw"] = png("red")
        if len(checks) == 2 and known:
            worker._save(path, journal, "unknown", failure_code="VK_BROWSER_CONCURRENT_TEST")
            if damage == "known_concurrent_error":
                raise AgentError("VK_BROWSER_READBACK_CHANGED")
        return result
    monkeypatch.setattr(worker, "_photo_original", read_original)
    mutations = []
    def route(request):
        if request.request.method not in {"GET", "HEAD"}:
            mutations.append(request.request.method)
            request.abort()
        elif request.request.url.startswith(origin + "/"):
            request.continue_()
        else:
            request.abort()
    page.context.route("**/*", route)
    result = worker.reconcile(page, root, options, "123456", journal["request_id"], origin=origin, fixture=True,
        media_review_loader=None if damage == "no_review" else loader)
    assert result["state"] == ("confirmed" if damage in {None, "responsive"} else "unknown"), result
    assert result["verified"] is False and mutations == [] and state["uploads"] == 0 and state["publishes"] == 1
    if damage in {None, "responsive"}:
        assert result["external_id"] == "123456_51" and state["photo_views"] == 2 and len(loads) == 2
        proof = result["evidence"]["proof"]
        assert proof["verification"] == VERIFICATION and proof["original_policy_satisfied"] is False
        assert proof["reloads"][0]["image_proofs"][0]["actual_sha256"] == details["actual_sha256"]
        delivery = VKBrowser._delivery(result, "123456", journal["request_id"], plan,
                                      expected_fixture=True, expected_media_review=authority)
        assert delivery.state == "confirmed" and delivery.verified is False
        with pytest.raises(AgentError, match="VK_BROWSER_ACTUAL_MEDIA_READ_ONLY"):
            worker.run_publish(None, root, options, {"request_id": journal["request_id"], "destination": "123456",
                "snapshot": snapshot}, lambda: pytest.fail("No write authorization"), origin=origin, fixture=True)
    else:
        _, saved = worker._read(root, journal["request_id"])
        assert saved.get("external_id") == ("123456_51" if known else None) and result["evidence"]["proof"] is None
        if known:
            assert saved["failure_code"] == "VK_BROWSER_CONCURRENT_TEST"


@pytest.mark.parametrize("damage", [None, "opacity", "hidden_ancestor", "clip_path", "transform", "overflow",
    "text", "hidden_extra", "image_extra", "nested", "outside", "wrong_element", "wrong_label", "nonpost"])
def test_observed_post_semantic_inline_zero_anchor_requires_one_visible_direct_native_image(native_site, page, damage):
    root, options, snapshot, _, origin = native_site
    plan = prepare(root, options, "123456", snapshot)
    selectors = dom_contract(options)
    page.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
    # Fixture layout reproduces the observed computed inline 0x0 anchor and
    # positive, unclipped image box. It does not claim to copy VK's stylesheet.
    image = '<img data-testid="primary-attachment-image-content" src="' + origin + '/media.png" style="position:absolute;left:0;top:0;width:320px;height:240px">'
    markup = ('<article id="post" data-testid="post"><div id="photo" data-testid="post-content-container" '
        'style="position:relative;width:320px;height:240px;overflow:hidden;font-size:0;line-height:0">'
        '<a data-testid="primary-attachment-interactive-wrapper" aria-label="Фотография" href="/photo123456_77">'
        + image + '</a></div></article>')
    page.set_content(markup)
    anchor = page.locator('a[data-testid="primary-attachment-interactive-wrapper"]')
    img = page.locator('img[data-testid="primary-attachment-image-content"]')
    page.wait_for_function("()=>document.querySelector('img').complete&&document.querySelector('img').naturalWidth>0")
    assert anchor.evaluate("e=>{const r=e.getBoundingClientRect();return[getComputedStyle(e).display,r.width,r.height]}") == ["inline", 0, 0]
    if damage == "opacity":
        anchor.evaluate("e=>e.style.opacity='0'")
    elif damage == "hidden_ancestor":
        page.locator('#photo').evaluate("e=>e.style.opacity='0'")
    elif damage == "clip_path":
        anchor.evaluate("e=>e.style.clipPath='inset(20%)'")
    elif damage == "transform":
        anchor.evaluate("e=>e.style.transform='translateX(0px)'")
    elif damage == "overflow":
        anchor.evaluate("e=>e.style.overflow='hidden'")
    elif damage == "text":
        anchor.evaluate("e=>e.append('unapproved')")
    elif damage == "hidden_extra":
        anchor.evaluate("e=>{const n=document.createElement('span');n.hidden=true;e.append(n)}")
    elif damage == "image_extra":
        anchor.evaluate("e=>e.append(e.querySelector('img').cloneNode())")
    elif damage == "nested":
        anchor.evaluate("e=>{const w=document.createElement('span');w.append(e.querySelector('img'));e.append(w)}")
    elif damage == "outside":
        img.evaluate("e=>e.parentElement.parentElement.append(e)")
    elif damage == "wrong_element":
        anchor.evaluate("e=>{const w=document.createElement('span');for(const a of e.attributes)w.setAttribute(a.name,a.value);w.append(...e.childNodes);e.replaceWith(w)}")
        selectors["post_asset"] = '[data-testid="primary-attachment-interactive-wrapper"]'
    elif damage == "wrong_label":
        anchor.evaluate("e=>e.setAttribute('aria-label','Other media')")
    captured = {}
    class Tap:
        def evaluate(self, script, argument):
            result = page.locator('#post').evaluate(script, argument)
            captured.update(result)
            return result
    if damage == "nonpost":
        selectors.update(uploaded_asset=selectors["post_asset"], uploaded_images=selectors["post_images"])
        with pytest.raises(AgentError):
            worker._native_images(page, plan, selectors, "uploaded", origin=origin)
    elif damage is None:
        images = worker._native_images(Tap(), plan, selectors, "post", origin=origin)
        assert captured["semantic_anchor"] is True and captured["asset_visible"] is True
        assert images[0]["asset_id"] == "123456_77" and images[0]["representations"][0]["visible"] is True
    else:
        with pytest.raises(AgentError):
            worker._native_images(Tap(), plan, selectors, "post", origin=origin)
