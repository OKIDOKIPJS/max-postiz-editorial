"""Иллюстрированный пост подтверждается только с крупным изображением до подписи."""
import copy
import hashlib
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

from max_postiz_agent import browser_worker
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.browser_publisher import BrowserMax
from test_browser_publishing import browser_config

pytest_plugins = ["test_hub"]

CONTRACT = "article-illustrated-v1"
TITLE = "Точный заголовок"
CAPTION = TITLE + "\n\nТочная подпись статьи."


@pytest.fixture(params=["article-illustrated-v1", "article-illustrated-v2"])
def illustrated(project, request):
    settings = browser_config(project)
    path = project / "data/media/article.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), "navy").save(path)
    attachment = BrowserMax(settings).upload_image(path)
    contract = request.param
    illustration = {"contract": contract, "width": 1600, "height": 900, "credit": "Example / CC BY 4.0"}
    metadata = {"presentation_contract": contract, "illustration": illustration,
        "illustration_proof_hash": fingerprint(illustration)}
    media = [{"path": path.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mime": "image/png"}]
    payload = {"request_id": "illustrated-article", "chat_id": "-123", "text": CAPTION,
        "attachment": attachment}
    browser_worker._bind_presentation(settings, payload, {"metadata": metadata, "media": media,
        "title": TITLE, "body": CAPTION})
    return SimpleNamespace(settings=settings, path=path, payload=payload, media=media, metadata=metadata)


def test_authoritative_dispatch_derives_image_flags_and_legacy_drops_untrusted_flags(illustrated):
    value = illustrated
    payload = copy.deepcopy(value.payload)
    payload.update(presentation_contract="forged", illustration_proof_hash="0" * 64,
        illustration_dimensions=[10, 10])
    browser_worker._bind_presentation(value.settings, payload,
        {"metadata": value.metadata, "media": value.media, "title": TITLE, "body": CAPTION})
    assert payload["presentation_contract"] == value.metadata["presentation_contract"]
    assert payload["illustration_dimensions"] == [1600, 900]
    assert payload["illustration_proof_hash"] == fingerprint(value.metadata["illustration"])
    browser_worker._bind_presentation(value.settings, payload, {"metadata": {}, "media": []})
    assert all(field not in payload for field in browser_worker._PRESENTATION_FIELDS)


@pytest.mark.parametrize("change", ["marker", "proof", "missing_proof_marker"])
def test_signed_presentation_and_image_proof_versions_must_match_even_with_rebound_hash(illustrated, change):
    value = illustrated
    metadata = copy.deepcopy(value.metadata)
    other = "article-illustrated-v2" if metadata["presentation_contract"] == CONTRACT else CONTRACT
    if change == "marker":
        metadata["presentation_contract"] = other
    elif change == "proof":
        metadata["illustration"]["contract"] = other
    else:
        metadata["illustration"].pop("contract")
    metadata["illustration_proof_hash"] = fingerprint(metadata["illustration"])
    with pytest.raises(AgentError, match="BROWSER_ARTICLE_IMAGE_INVALID"):
        browser_worker._bind_presentation(value.settings, value.payload,
            {"metadata": metadata, "media": value.media, "title": TITLE, "body": CAPTION})


@pytest.mark.parametrize("change", ["missing_media", "video", "wrong_proof", "wrong_dimensions", "small_file", "unknown_contract"])
def test_illustrated_binding_never_accepts_missing_or_changed_image(illustrated, change):
    value = illustrated
    metadata, media = copy.deepcopy(value.metadata), copy.deepcopy(value.media)
    if change == "missing_media":
        media = []
    elif change == "video":
        media[0]["mime"] = "video/mp4"
    elif change == "wrong_proof":
        metadata["illustration_proof_hash"] = "0" * 64
    elif change == "wrong_dimensions":
        metadata["illustration"]["width"] = 2000
        metadata["illustration_proof_hash"] = fingerprint(metadata["illustration"])
    elif change == "small_file":
        Image.new("RGB", (640, 360), "navy").save(value.path)
    else:
        metadata["presentation_contract"] = "unknown"
    with pytest.raises(AgentError, match="BROWSER_ARTICLE_IMAGE"):
        browser_worker._bind_presentation(value.settings, value.payload,
            {"metadata": metadata, "media": media, "title": TITLE, "body": CAPTION})


@pytest.mark.parametrize("change", ["missing_image", "wrong_type", "missing_proof", "missing_dimensions", "small_dimensions"])
def test_illustrated_post_has_no_text_only_fallback_before_opening_browser(illustrated, monkeypatch, change):
    payload = copy.deepcopy(illustrated.payload)
    if change == "missing_image":
        payload["attachment"] = None
    elif change == "wrong_type":
        payload["attachment"]["type"] = "browser-video"
    elif change == "missing_proof":
        payload.pop("illustration_proof_hash")
    elif change == "missing_dimensions":
        payload.pop("illustration_dimensions")
    else:
        payload["illustration_dimensions"] = [800, 450]
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("browser opened"))
    with pytest.raises(AgentError, match="BROWSER_ARTICLE_IMAGE_INVALID"):
        browser_worker.publish(illustrated.settings, payload)


class Nodes:
    def __init__(self, nodes):
        self.nodes = nodes

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]

    def evaluate_all(self, _script, _text):
        return list(range(len(self.nodes)))


class Element:
    def __init__(self, box, *, visible=True, width=1280, height=720, complete=True):
        self.box, self.visible = box, visible
        self.natural = {"width": width, "height": height, "complete": complete,
            "source": "https://static.max.ru/fixture.png"}
        self.formatting = {"title_text": TITLE, "title_bold": True, "body_unbold": True,
            "body_characters": 22, "title_top": 380}

    def is_visible(self):
        return self.visible

    def bounding_box(self):
        return self.box

    def evaluate(self, script, *_args):
        return self.natural if "naturalWidth" in script else self.formatting


class Post:
    def __init__(self, images, captions):
        self.images, self.captions = Nodes(images), Nodes(captions)

    def locator(self, _selector):
        return self.captions if _selector == "*" else self.images

    def evaluate(self, _script, _text):
        return False

    def get_by_text(self, text, exact):
        assert text == CAPTION and exact is True
        return self.captions


@pytest.fixture
def receipt_state(illustrated):
    return {"request_id": "illustrated-article", "text": illustrated.payload["text"],
        "attachment": True, "attachment_type": "browser-image",
        "presentation_contract": illustrated.payload["presentation_contract"],
        "illustration_proof_hash": illustrated.payload["illustration_proof_hash"],
        "illustration_dimensions": [1600, 900],
        "title_text": TITLE, "title_format": "bold",
        "illustration_image_sha256": illustrated.payload["attachment"]["payload"]["sha256"]}


def good_post():
    return Post([Element({"x": 0, "y": 10, "width": 640, "height": 360})],
        [Element({"x": 0, "y": 380, "width": 640, "height": 100})])


def test_new_receipt_proves_large_image_above_exact_caption_but_not_original_pixel_sha(receipt_state):
    proof = browser_worker.illustrated_receipt(good_post(), receipt_state)
    assert proof["contract"] == receipt_state["presentation_contract"]
    assert proof["image_before_caption"] is True
    assert proof["natural_width"] == 1280 and proof["natural_height"] == 720
    assert proof["uploaded_sha256"] == receipt_state["illustration_image_sha256"]
    assert proof["illustration_proof_hash"] == receipt_state["illustration_proof_hash"]
    assert proof["delivered_pixels_sha256_verified"] is False
    assert proof["title_proof"]["title_bold"] is True
    assert proof["title_proof"]["body_unbold"] is True


@pytest.mark.parametrize("source", [
    "https://i.oneme.ru/fixture-photo.jpg?token=private-fixture-query&expires=123",
    "https://static.max.ru/fixture.png", "blob:https://web.max.ru/fixture-blob",
])
def test_receipt_accepts_only_known_image_origins_and_never_records_signed_query(receipt_state, source):
    post = good_post()
    post.images.nth(0).natural["source"] = source
    proof = browser_worker.illustrated_receipt(post, receipt_state)
    assert proof is not None and proof["uploaded_sha256"] == receipt_state["illustration_image_sha256"]
    assert "private-fixture-query" not in json.dumps(proof)


@pytest.mark.parametrize("source", ["http://i.oneme.ru/photo.jpg", "https://i.oneme.ru.evil.invalid/photo.jpg",
    "https://evil.invalid/i.oneme.ru/photo.jpg", "https://evil@i.oneme.ru/photo.jpg",
    "https://i.oneme.ru:8443/photo.jpg", "https://static.max.ru.evil.invalid/photo.jpg",
    "data:image/png;base64,AAAA", "blob:https://evil.invalid/fixture", None])
def test_receipt_rejects_untrusted_image_origin_even_with_good_pixels_and_bold_caption(receipt_state, source):
    post = good_post()
    post.images.nth(0).natural["source"] = source
    assert browser_worker.illustrated_receipt(post, receipt_state) is None


@pytest.mark.parametrize("marker", ["article-illustrated-v3", "unknown", [], {}, True])
def test_unknown_or_non_string_presentation_never_falls_back_to_text_only(illustrated, receipt_state, monkeypatch, marker):
    payload = {**illustrated.payload, "presentation_contract": marker}
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("invalid marker opened browser"))
    with pytest.raises(AgentError, match="BROWSER_ARTICLE_IMAGE_INVALID"):
        browser_worker.publish(illustrated.settings, payload)
    receipt_state["presentation_contract"] = marker
    assert browser_worker.illustrated_receipt(good_post(), receipt_state) is None
    assert browser_worker.receipt(None, receipt_state) is None


@pytest.mark.parametrize("change", ["plain_title", "bold_body", "wrong_style", "missing_title"])
def test_receipt_rejects_unformatted_heading_or_bold_body(receipt_state, change):
    post = good_post()
    if change == "plain_title":
        post.captions.nth(0).formatting["title_bold"] = False
    elif change == "bold_body":
        post.captions.nth(0).formatting["body_unbold"] = False
    elif change == "wrong_style":
        receipt_state["title_format"] = "markdown"
    else:
        receipt_state.pop("title_text")
    assert browser_worker.illustrated_receipt(post, receipt_state) is None


@pytest.mark.parametrize("change", ["missing_title", "different_title", "wrong_style"])
def test_caption_title_contract_fails_before_opening_browser(illustrated, monkeypatch, change):
    payload = copy.deepcopy(illustrated.payload)
    if change == "missing_title":
        payload.pop("title_text")
    elif change == "different_title":
        payload["title_text"] = "Подменённый заголовок"
    else:
        payload["title_format"] = "markdown"
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("browser opened"))
    with pytest.raises(AgentError, match="BROWSER_ARTICLE_TITLE_INVALID"):
        browser_worker.publish(illustrated.settings, payload)


@pytest.mark.parametrize("change", ["missing", "avatar", "hidden", "incomplete", "small_natural", "small_rendered", "wrong_aspect", "below_caption", "missing_caption"])
def test_new_receipt_rejects_text_only_avatar_small_or_wrong_order(receipt_state, change):
    post = good_post()
    if change == "missing":
        post.images = Nodes([])
    elif change == "missing_caption":
        post.captions = Nodes([])
    else:
        image = post.images.nth(0)
        if change == "avatar":
            image.natural.update(width=96, height=96)
            image.box.update(width=48, height=48)
        elif change == "hidden":
            image.visible = False
        elif change == "incomplete":
            image.natural["complete"] = False
        elif change == "small_natural":
            image.natural.update(width=400, height=225)
        elif change == "small_rendered":
            image.box.update(width=100, height=56)
        elif change == "wrong_aspect":
            image.natural.update(width=900, height=1600)
        else:
            image.box["y"] = 500
    assert browser_worker.illustrated_receipt(post, receipt_state) is None


def test_unknown_illustrated_intent_never_reopens_browser_or_repeats_send(illustrated, monkeypatch):
    payload = illustrated.payload
    path = browser_worker.ledger_path(illustrated.settings, payload["request_id"])
    browser_worker.save(path, {"state": "sending", "content_hash": fingerprint(payload)})
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("browser reopened"))
    assert browser_worker.publish(illustrated.settings, payload) == {"state": "unknown"}
    payload["illustration_proof_hash"] = "0" * 64
    with pytest.raises(AgentError, match="BROWSER_INTENT_CONTENT_CHANGED"):
        browser_worker.publish(illustrated.settings, payload)


def test_other_supported_contract_cannot_replay_unknown_intent_or_send_again(illustrated, monkeypatch):
    payload = illustrated.payload
    path = browser_worker.ledger_path(illustrated.settings, payload["request_id"])
    browser_worker.save(path, {"state": "sending", "content_hash": fingerprint(payload)})
    payload["presentation_contract"] = "article-illustrated-v2" if payload["presentation_contract"] == CONTRACT else CONTRACT
    monkeypatch.setattr(browser_worker, "session", lambda *_: pytest.fail("cross-contract replay opened browser"))
    with pytest.raises(AgentError, match="BROWSER_INTENT_CONTENT_CHANGED"):
        browser_worker.publish(illustrated.settings, payload)


def test_modified_image_bytes_are_not_uploaded(illustrated):
    illustrated.path.write_bytes(illustrated.path.read_bytes() + b"changed")
    with pytest.raises(AgentError, match="IMAGE_HASH_CHANGED"):
        browser_worker.snapshot_attachment(illustrated.settings, illustrated.payload["attachment"], "changed-image")


@pytest.fixture
def image_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        page = browser.new_page()
        html = r'''<meta charset="utf-8"><h2 id="main-header-title">Окно чата с Тест</h2>
        <div id="posts"></div><div contenteditable role="textbox" aria-placeholder="Пост" style="white-space:pre-wrap"></div>
        <button aria-label="Загрузить файл" onclick="document.querySelector('#menu').hidden=false">+</button>
        <div id="menu" hidden><button onclick="document.querySelector('input').click()">Фото или видео</button></div>
        <input type="file" hidden><button aria-label="Отправить сообщение" onclick="send()">Send</button>
        <script>
        window.events=JSON.parse(localStorage.getItem('events')||'[]');
        function logEvent(name){events.push(name);localStorage.setItem('events',JSON.stringify(events))}
        document.querySelector('input').onchange=()=>logEvent('upload');
        document.querySelector('[contenteditable]').oninput=(input)=>{
          logEvent('caption');
          if(input.inputType!=='formatBold'){
            const editor=input.currentTarget, text=editor.innerText.replace(/\n{3,}/g,'\n\n');
            editor.innerHTML=text.split('\n').map(line=>line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')).join('<br>');
          }
        };
        function render(){document.querySelector('#posts').innerHTML='';
          for(const post of JSON.parse(localStorage.getItem('posts')||'[]')){
            const item=document.createElement('div');item.setAttribute('role','listitem');
            const media=document.createElement('img');media.src='https://static.max.ru/fixture.png';
            media.style.cssText='display:block;width:640px;height:360px';item.append(media);
            const text=document.createElement('span');text.style.cssText='display:block;white-space:pre-wrap';text.innerHTML=post.html;item.append(text);
            document.querySelector('#posts').append(item);}}
        function send(){logEvent('send');const editor=document.querySelector('[contenteditable]');
          const posts=JSON.parse(localStorage.getItem('posts')||'[]');
          posts.push({text:editor.innerText,html:editor.innerHTML});localStorage.setItem('posts',JSON.stringify(posts));editor.textContent='';render();}render();
        </script>'''
        import io
        data = io.BytesIO()
        Image.new("RGB", (1280, 720), "navy").save(data, format="PNG")
        page.route("https://static.max.ru/fixture.png", lambda route: route.fulfill(body=data.getvalue(), content_type="image/png"))
        page.route("https://web.max.ru/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        yield page
        browser.close()


@pytest.fixture
def lazy_page(image_page):
    def configure(mode="photo", dimensions=(1920, 1440), *, caption_delay_ms=0, image_delay_ms=0):
        import io
        width, height = (96, 96) if mode == "avatar" else (400, 225) if mode == "lowres" else dimensions
        data = io.BytesIO()
        Image.new("RGB", (width, height), "navy").save(data, format="JPEG")
        source = "https://i.oneme.ru/fixture-photo.jpg?token=private-fixture-query"
        if mode == "lookalike":
            source = source.replace("i.oneme.ru/", "i.oneme.ru.evil.invalid/")
        elif mode == "http":
            source = source.replace("https:", "http:")
        options = {"mode": mode, "source": source, "height": round(464 * dimensions[1] / dimensions[0]),
            "caption_delay_ms": caption_delay_ms, "image_delay_ms": image_delay_ms}
        html = r'''<meta charset="utf-8"><h2 id="main-header-title">Окно чата с Тест</h2>
        <main id="history" style="height:420px;overflow:auto"></main>
        <div contenteditable role="textbox" aria-placeholder="Пост" style="white-space:pre-wrap"></div>
        <button aria-label="Загрузить файл" onclick="document.querySelector('#menu').hidden=false">+</button>
        <div id="menu" hidden><button onclick="document.querySelector('input').click()">Фото или видео</button></div>
        <input type="file" hidden><button aria-label="Отправить сообщение" onclick="send()">Send</button>
        <script>
        const options=__OPTIONS__;
        window.events=JSON.parse(localStorage.getItem('events')||'[]');
        function logEvent(name){events.push(name);localStorage.setItem('events',JSON.stringify(events))}
        document.querySelector('input').onchange=()=>logEvent('upload');
        document.querySelector('[contenteditable]').oninput=(input)=>{
          logEvent('caption');
          if(input.inputType!=='formatBold'){
            const editor=input.currentTarget,text=editor.innerText.replace(/\n{3,}/g,'\n\n');
            editor.innerHTML=text.split('\n').map(line=>line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')).join('<br>');
          }
        };
        function render(){const history=document.querySelector('#history');
          history.innerHTML='<div role="listitem" id="decoy"><div class="image-placeholder" style="width:464px;height:200px"></div>Соседний пост</div><div style="height:900px"></div>';
          for(const post of JSON.parse(localStorage.getItem('posts')||'[]')){
            const item=document.createElement('div');item.setAttribute('role','listitem');item.id='exact-post';item.style.width='464px';
            const placeholder=document.createElement('div');placeholder.className='image-placeholder';
            placeholder.style.cssText='width:464px;height:'+options.height+'px;background-image:url(data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==)';
            const caption=document.createElement('span');caption.style.cssText='display:block;white-space:pre-wrap';caption.innerHTML=post.html;
            if(options.mode==='below'){item.append(caption);item.append(placeholder)}else{item.append(placeholder);item.append(caption)}
            history.append(item);
            const observer=new IntersectionObserver(entries=>{
              if(!entries.some(entry=>entry.isIntersecting)||options.mode==='placeholder')return;
              observer.disconnect();const load=()=>{const image=document.createElement('img');image.src=options.source;
                image.style.cssText=options.mode==='avatar'?'display:block;width:48px;height:48px':'display:block;width:464px;height:'+options.height+'px';
                placeholder.replaceWith(image);logEvent('lazy-photo');};
              if(options.image_delay_ms){setTimeout(load,options.image_delay_ms)}else{load();}
            },{root:history});observer.observe(placeholder);
          }history.scrollTop=history.scrollHeight;
        }
        function send(){logEvent('send');const editor=document.querySelector('[contenteditable]');
          const posts=JSON.parse(localStorage.getItem('posts')||'[]');posts.push({text:editor.innerText,html:editor.innerHTML});
          localStorage.setItem('posts',JSON.stringify(posts));editor.textContent='';render();}
        if(options.caption_delay_ms){setTimeout(render,options.caption_delay_ms)}else{render();}
        </script>'''.replace("__OPTIONS__", json.dumps(options))
        image_page.unroute("https://web.max.ru/**")
        image_page.route("https://web.max.ru/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        image_page.route("**/fixture-photo.jpg*", lambda route: route.fulfill(body=data.getvalue(), content_type="image/jpeg"))
        return image_page
    return configure


def long_caption():
    return TITLE + "\n\n" + "Подробный научный текст остаётся обычным и полностью сохраняется. " * 44


def delivered_lazy_post(page, state, mode="photo"):
    from html import escape
    state.update(text=long_caption(), illustration_dimensions=[2048, 1536], before_count=0)
    text = state["text"]
    title = escape(TITLE)
    html = ("<b>" + title + "</b>" + escape(text[len(TITLE):]))
    if mode == "plain_title":
        html = escape(text)
    elif mode == "bold_body":
        html = "<b>" + escape(text) + "</b>"
    page.goto("https://web.max.ru/-123", wait_until="domcontentloaded")
    page.evaluate("value=>{localStorage.setItem('posts',JSON.stringify([{text:value.text,html:value.html}]));localStorage.setItem('events',JSON.stringify(['send']))}",
        {"text": text, "html": html})
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(50)


def test_direct_receipt_scrolls_only_exact_post_and_loads_signed_oneme_photo(receipt_state, lazy_page):
    page = lazy_page(image_delay_ms=2400)
    delivered_lazy_post(page, receipt_state)
    assert page.locator("#exact-post img").count() == 0
    deadline = time.monotonic() + 15
    # This direct-observation case starts from a fully laid out exact caption.
    # Delayed caption hydration is exercised separately through _loaded_receipt.
    title_ready = None
    while time.monotonic() < deadline:
        posts = browser_worker._matched_posts(page, receipt_state["text"], illustrated=True)
        title_ready = browser_worker._illustrated_title(posts.last, receipt_state) if posts.count() == 1 else None
        if title_ready:
            break
        page.wait_for_timeout(100)
    assert title_ready and title_ready["title_bold"] is True and title_ready["body_unbold"] is True
    before_scroll = page.evaluate("""() => ({top:document.querySelector('#history').scrollTop,
        media:[...document.querySelectorAll('#exact-post .image-placeholder,#exact-post img')]
            .map(image=>({tag:image.tagName,box:image.getBoundingClientRect().toJSON()}))})""")
    # A direct receipt is a short observation, not a promise to finish loading
    # within two seconds. It must trigger the exact post's real lazy loader.
    browser_worker.receipt(page, receipt_state)
    assert page.locator("#decoy img").count() == 0
    loaded = False
    while time.monotonic() < deadline:
        loaded = page.locator("#exact-post img").evaluate_all("""images => images.some(image =>
            image.complete && image.naturalWidth === 1920 && image.naturalHeight === 1440)""")
        if loaded:
            break
        # Pump Playwright callbacks while the real routed image loads/decodes.
        page.wait_for_timeout(100)
    assert loaded, (before_scroll, title_ready, page.evaluate("""() => ({scrollTop:document.querySelector('#history').scrollTop,
        events, images:[...document.querySelectorAll('#exact-post img')].map(image =>
            ({width:image.naturalWidth,height:image.naturalHeight,complete:image.complete}))})"""))
    proof = browser_worker.receipt(page, receipt_state)
    assert proof["state"] == "confirmed"
    photo = proof["image_proof"]
    assert photo["natural_width"] == 1920 and photo["natural_height"] == 1440
    assert photo["image_bottom"] <= photo["caption_top"] + 4
    assert photo["title_proof"]["title_bold"] is True and photo["title_proof"]["body_unbold"] is True
    assert photo["uploaded_sha256"] == receipt_state["illustration_image_sha256"]
    assert "private-fixture-query" not in json.dumps(proof)
    assert page.locator("#decoy img").count() == 0
    assert page.evaluate("events.filter(event=>event==='send').length") == 1
    page.reload(wait_until="domcontentloaded")
    assert browser_worker._loaded_receipt(page, receipt_state)["state"] == "confirmed"
    assert page.evaluate("events.filter(event=>event==='send').length") == 1


def test_loaded_receipt_waits_for_caption_hydration_after_each_reload_without_send(receipt_state, lazy_page):
    page = lazy_page(caption_delay_ms=600)
    delivered_lazy_post(page, receipt_state)
    assert browser_worker._matched_posts(page, receipt_state["text"], illustrated=True).count() == 0
    for number in range(2):
        if number:
            page.reload(wait_until="domcontentloaded")
            assert browser_worker._matched_posts(page, receipt_state["text"], illustrated=True).count() == 0
        proof = browser_worker._loaded_receipt(page, receipt_state)
        assert proof["state"] == "confirmed"
        assert proof["image_proof"]["natural_width"] == 1920
        assert proof["image_proof"]["image_before_caption"] is True
        assert page.evaluate("events.filter(event=>event==='send').length") == 1


def test_loaded_receipt_rejects_extra_exact_post_without_waiting(receipt_state, lazy_page, monkeypatch):
    page = lazy_page()
    delivered_lazy_post(page, receipt_state)
    page.locator("#exact-post").evaluate("post => post.parentElement.append(post.cloneNode(true))")
    assert browser_worker._matched_posts(page, receipt_state["text"], illustrated=True).count() == 2
    monkeypatch.setattr(page, "wait_for_timeout", lambda *_: pytest.fail("duplicate must fail immediately"))
    assert browser_worker._loaded_receipt(page, receipt_state) is None
    assert page.evaluate("events.filter(event=>event==='send').length") == 1


def test_loaded_receipt_missing_caption_has_one_bounded_deadline(receipt_state, monkeypatch):
    clock = SimpleNamespace(now=0.0, waits=0, receipts=0)
    receipt_state["before_count"] = 0

    def absent(*_args, **_kwargs):
        clock.receipts += 1
        return None

    def wait(milliseconds):
        clock.waits += 1
        clock.now += milliseconds / 1000

    monkeypatch.setattr(browser_worker.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(browser_worker, "receipt", absent)
    monkeypatch.setattr(browser_worker, "_matched_posts", lambda *_args, **_kwargs: Nodes([]))
    assert browser_worker._loaded_receipt(SimpleNamespace(wait_for_timeout=wait), receipt_state) is None
    assert 15 <= clock.now < 15.21
    assert 74 <= clock.waits <= 76 and clock.receipts == clock.waits + 1


@pytest.mark.parametrize("mode", ["lookalike", "http", "placeholder", "avatar", "lowres", "below", "plain_title", "bold_body"])
def test_lazy_receipt_rejects_bad_origin_placeholder_avatar_order_resolution_or_format(receipt_state, lazy_page, mode):
    page = lazy_page(mode)
    delivered_lazy_post(page, receipt_state, mode)
    assert browser_worker.receipt(page, receipt_state, _load_deadline=time.monotonic() + .5) is None
    assert page.evaluate("events.filter(event=>event==='send').length") == 1


def test_lazy_oneme_photo_after_reload_confirms_first_send_and_never_sends_twice(illustrated, lazy_page, monkeypatch):
    page = lazy_page(dimensions=(1920, 1080))
    payload = {**illustrated.payload, "text": long_caption()}

    @contextmanager
    def opened(*_args):
        yield page

    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.publish(illustrated.settings, payload)
    assert result["state"] == "confirmed" and result["image_proof"]["image_before_caption"] is True
    assert page.evaluate("events.filter(event=>event==='send').length") == 1
    assert browser_worker.publish(illustrated.settings, payload) == result
    assert page.evaluate("events.filter(event=>event==='send').length") == 1


def test_post_send_reload_confirms_existing_image_without_publish_composer(
        illustrated, image_page, monkeypatch):
    image_page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
      if(JSON.parse(localStorage.getItem('posts') || '[]').length)
        document.querySelector('[contenteditable]').remove();
    })""")

    @contextmanager
    def opened(*_args):
        yield image_page

    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.publish(illustrated.settings, illustrated.payload)
    assert result["state"] == "confirmed"
    assert result["image_proof"]["image_before_caption"] is True
    assert result["image_proof"]["title_proof"]["title_bold"] is True
    assert result["image_proof"]["title_proof"]["body_unbold"] is True
    assert image_page.locator("[contenteditable]").count() == 0
    assert image_page.evaluate("events").count("send") == 1
    assert browser_worker.publish(illustrated.settings, illustrated.payload) == result
    assert image_page.evaluate("events").count("send") == 1


def test_unknown_image_reconcile_reads_existing_post_without_publish_composer(
        illustrated, receipt_state, image_page, monkeypatch):
    from html import escape

    image_page.goto("https://web.max.ru/-123", wait_until="domcontentloaded")
    image_page.evaluate("""post => {
      localStorage.setItem('posts', JSON.stringify([post]));
      localStorage.setItem('events', '[]');
    }""", {"text": CAPTION, "html": "<b>" + escape(TITLE) + "</b>" + escape(CAPTION[len(TITLE):])})
    image_page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
      document.querySelector('[contenteditable]').remove();
    })""")
    state = {**receipt_state, "chat_id": "-123", "state": "unknown", "before_count": 0,
             "send_started": True}
    path = browser_worker.ledger_path(illustrated.settings, state["request_id"])
    browser_worker.save(path, state)

    @contextmanager
    def opened(*_args):
        yield image_page

    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.reconcile(illustrated.settings, "-123", state["request_id"])
    assert result["state"] == "confirmed"
    assert result["image_proof"]["image_before_caption"] is True
    assert result["image_proof"]["title_proof"]["title_bold"] is True
    assert result["image_proof"]["title_proof"]["body_unbold"] is True
    assert image_page.evaluate("events").count("send") == 0
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "confirmed"


def test_real_browser_image_is_uploaded_before_caption_and_receipt_survives_reload(illustrated, image_page, monkeypatch):
    @contextmanager
    def opened(*_args):
        yield image_page

    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.publish(illustrated.settings, illustrated.payload)
    assert result["image_proof"]["contract"] == illustrated.payload["presentation_contract"]
    assert result["state"] == "confirmed"
    assert result["image_proof"]["image_before_caption"] is True
    assert result["image_proof"]["natural_width"] == 1280
    assert result["image_proof"]["title_proof"]["title_bold"] is True
    assert result["image_proof"]["title_proof"]["body_unbold"] is True
    assert "**" not in image_page.get_by_role("listitem").inner_text()
    assert image_page.get_by_role("listitem").count() == 1
    events = image_page.evaluate("events")
    assert events.index("upload") < events.index("caption") < events.index("send")
    assert events.count("send") == 1
    state = __import__("json").loads(browser_worker.ledger_path(illustrated.settings,
        illustrated.payload["request_id"]).read_text(encoding="utf-8"))
    assert state["presentation_contract"] == illustrated.payload["presentation_contract"]
    assert state["illustration_proof_hash"] == illustrated.payload["illustration_proof_hash"]
    assert state["illustration_image_sha256"] == illustrated.payload["attachment"]["payload"]["sha256"]
    assert browser_worker.publish(illustrated.settings, illustrated.payload) == result
    assert image_page.get_by_role("listitem").count() == 1


@pytest.mark.parametrize("title", ["Длинный заголовок переносится визуально на несколько строк и выделяется целиком",
                                    "🌌 Звёзды, числа 42 и новое наблюдение"])
def test_native_bold_selection_covers_wrapped_and_unicode_title_without_changing_body(image_page, title):
    image_page.goto("https://web.max.ru/-123", wait_until="domcontentloaded")
    composer = image_page.get_by_role("textbox")
    composer.evaluate("element => element.style.width='140px'")
    text = title + "\n\nНеизменённый текст подписи."
    composer.fill(text)
    proof = browser_worker._bold_title(image_page, composer, title, text)
    assert proof["title_bold"] is True and proof["body_unbold"] is True
    assert composer.inner_text() == text
    assert composer.locator("b, strong").inner_text() == title
    assert "**" not in composer.inner_text()
    assert image_page.get_by_role("listitem").count() == 0


def test_native_formatting_failure_stops_before_send_and_durable_sending_intent(illustrated, image_page, monkeypatch):
    @contextmanager
    def opened(*_args):
        yield image_page

    monkeypatch.setattr(browser_worker, "session", opened)
    keys = []
    monkeypatch.setattr(image_page.keyboard, "press", lambda key: keys.append(key))
    result = browser_worker.publish(illustrated.settings, illustrated.payload)
    assert result["state"] == "preclick_failed"
    assert keys == ["Control+b", "ArrowRight"]
    assert image_page.get_by_role("listitem").count() == 0
    assert "send" not in image_page.evaluate("events")
    proof = browser_worker.read_preclick_failure(illustrated.settings,
        illustrated.payload["request_id"], chat_id=illustrated.payload["chat_id"],
        text=illustrated.payload["text"], attachment=illustrated.payload["attachment"])
    assert proof["sha256"] == result["proof_sha256"]
    assert proof["proof"]["send_started"] is False
    assert proof["proof"]["error"]["code"] == "BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE"


def test_upload_editor_rerender_finishes_before_single_native_bold_toggle(illustrated, image_page, monkeypatch):
    image_page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
      const input=document.querySelector('input[type=file]'), original=input.onchange;
      input.onchange=()=>{
        original(); const send=document.querySelector('[aria-label="Отправить сообщение"]');
        // MAX может включить отправку до завершения перерисовки редактора после загрузки.
        send.disabled=false; const cancel=document.createElement('button');
        cancel.setAttribute('aria-label','Отменить загрузку');document.body.append(cancel);
        setTimeout(()=>{const editor=document.querySelector('[contenteditable]');
          for(const node of editor.querySelectorAll('b,strong')) node.replaceWith(...node.childNodes);
          send.disabled=false;cancel.remove();logEvent('upload-ready');},350);
      };
      document.addEventListener('keydown', event=>{if(event.ctrlKey&&event.key.toLowerCase()==='b')logEvent('bold-toggle')});
    })""")

    @contextmanager
    def opened(*_args):
        yield image_page

    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.publish(illustrated.settings, illustrated.payload)
    assert result["state"] == "confirmed"
    events = image_page.evaluate("events")
    assert events.index("upload-ready") < events.index("bold-toggle") < events.index("send")
    assert events.count("bold-toggle") == 1 and events.count("send") == 1


def test_native_bold_proof_waits_for_delayed_dom_without_retoggling(image_page):
    image_page.goto("https://web.max.ru/-123", wait_until="domcontentloaded")
    composer = image_page.get_by_role("textbox")
    composer.fill(CAPTION)
    image_page.evaluate("""() => {
      window.boldInvocations=0;
      document.addEventListener('keydown', event=>{
        if(!event.ctrlKey||event.key.toLowerCase()!=='b')return;
        event.preventDefault();window.boldInvocations++;
        const range=window.getSelection().getRangeAt(0).cloneRange();
        setTimeout(()=>{const selection=window.getSelection();selection.removeAllRanges();
          selection.addRange(range);document.execCommand('bold');},350);
      });
    }""")
    proof = browser_worker._bold_title(image_page, composer, TITLE, CAPTION)
    assert proof["title_bold"] is True and proof["body_unbold"] is True
    assert image_page.evaluate("boldInvocations") == 1
    assert image_page.get_by_role("listitem").count() == 0


def test_send_can_only_begin_after_durable_sending_journal(illustrated, image_page, monkeypatch):
    @contextmanager
    def opened(*_args):
        yield image_page

    calls = []
    role = image_page.get_by_role

    def get_by_role(role_name, **kwargs):
        locator = role(role_name, **kwargs)
        if kwargs.get("name") == "Отправить сообщение":
            def interrupted_click(*_args, **_kwargs):
                path = browser_worker.ledger_path(illustrated.settings, illustrated.payload["request_id"])
                state = json.loads(path.read_text(encoding="utf-8"))
                assert state["state"] == "sending" and state["send_started"] is True
                assert not path.with_suffix(".new").exists()
                calls.append(state)
                raise RuntimeError("unknown click outcome")
            monkeypatch.setattr(locator, "click", interrupted_click)
        return locator

    monkeypatch.setattr(browser_worker, "session", opened)
    monkeypatch.setattr(image_page, "get_by_role", get_by_role)
    assert browser_worker.publish(illustrated.settings, illustrated.payload) == {"state": "unknown"}
    assert len(calls) == 1
    with pytest.raises(AgentError, match="BROWSER_PRECLICK_PROOF_INVALID"):
        browser_worker.read_preclick_failure(illustrated.settings, illustrated.payload["request_id"],
            chat_id=illustrated.payload["chat_id"], text=CAPTION, attachment=illustrated.payload["attachment"])
    assert image_page.get_by_role("listitem").count() == 0


@pytest.mark.parametrize("change", ["pause", "body", "format"])
def test_final_authorization_follows_all_waits_and_changed_authority_or_dom_stops_send(
        illustrated, image_page, monkeypatch, change):
    @contextmanager
    def opened(*_args):
        yield image_page

    events = []
    for name in ("_wait_composer_text", "_wait_title_proof"):
        original = getattr(browser_worker, name)

        def wait(*args, _original=original, _name=name, **kwargs):
            assert "authorize" not in events, "Ожидание после последней авторизации запрещено"
            events.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(browser_worker, name, wait)

    def authorize():
        assert events and events[-1] == "_wait_title_proof"
        events.append("authorize")
        if change == "pause":
            raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
        composer = image_page.get_by_role("textbox")
        if change == "body":
            composer.fill("Текст изменён во время проверки полномочий")
        else:
            composer.locator("b, strong").evaluate_all("nodes => nodes.forEach(node => node.replaceWith(...node.childNodes))")

    monkeypatch.setattr(browser_worker, "session", opened)
    result = browser_worker.publish(illustrated.settings, illustrated.payload, authorize_before_click=authorize)
    assert result["state"] == "preclick_failed" and events[-1] == "authorize"
    proof = browser_worker.read_preclick_failure(illustrated.settings, illustrated.payload["request_id"],
        chat_id=illustrated.payload["chat_id"], text=CAPTION, attachment=illustrated.payload["attachment"])
    assert proof["proof"]["send_started"] is False
    assert proof["proof"]["error"]["code"] == {"pause": "BROWSER_DISPATCH_NOT_APPROVED",
        "body": "BROWSER_COMPOSER_TEXT_NOT_READY", "format": "BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE"}[change]
    assert "send" not in image_page.evaluate("events")


@pytest.mark.parametrize("contract", ["article-illustrated-v1", "article-illustrated-v2"])
def test_native_authorization_derives_flags_from_signed_dispatch_not_caller(hub, contract):
    from max_postiz_agent.db import now
    from max_postiz_agent.hub_models import Dispatch, Target, Variant
    from test_hub import approve, variants

    with hub.db.session.begin() as session:
        session.get(Target, "max").route = "max_browser"
    ident = variants(hub)["max"]
    image = hub.settings.root / "data/media/exact.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), "navy").save(image)
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    illustration = {"contract": contract, "width": 1600, "height": 900, "credit": "Example / CC BY 4.0"}
    with hub.db.session.begin() as session:
        variant = session.get(Variant, ident)
        variant.media = [{"path": "data/media/exact.png", "sha256": sha, "mime": "image/png"}]
        variant.body = variant.title + "\n\n" + variant.body
        variant.metadata_json = {**variant.metadata_json, "presentation_contract": contract,
            "illustration": illustration, "illustration_proof_hash": fingerprint(illustration)}
    dispatch_id = approve(hub, ident)
    with hub.db.session.begin() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        dispatch.state, dispatch.owner, dispatch.lease_until = "sending", "worker", now() + 60
        payload = {"request_id": dispatch_id, "chat_id": "-123", "text": dispatch.snapshot["body"],
            "attachment": {"type": "browser-image", "payload": {"path": "data/media/exact.png", "sha256": sha}},
            "presentation_contract": "forged", "illustration_proof_hash": "0" * 64,
            "illustration_dimensions": [1, 1]}
    assert browser_worker.authorize_dispatch(hub.settings, hub.db, payload)
    assert payload["presentation_contract"] == contract
    assert payload["illustration_proof_hash"] == fingerprint(illustration)
    assert payload["illustration_dimensions"] == [1600, 900]
