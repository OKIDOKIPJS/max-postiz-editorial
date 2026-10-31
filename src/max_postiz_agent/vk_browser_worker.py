"""VK public DOM only: separate owner profile, durable claims, one publish click."""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, parse_json
from .vk_browser import (
    CONTRACT, ORIGIN, dom_contract, owner_id, prepare, private_path, profile_path, wall_url,
)


def _path(root, request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise AgentError("VK_BROWSER_REQUEST_ID_INVALID")
    return private_path(root, "data/vk-browser-operations/" + request_id + ".json")


def _seal(journal):
    journal["journal_sha256"] = fingerprint({key: value for key, value in journal.items()
                                            if key != "journal_sha256"})
    return journal


def _save(path, journal, state, **fields):
    journal.update(state=state, updated_at=time.time(), **fields)
    _seal(journal)
    pending = path.with_suffix(".pending")
    with pending.open("x", encoding="utf-8") as stream:
        json.dump(journal, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def _read(root, request_id):
    path = _path(root, request_id)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        value = parse_json(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("contract") != CONTRACT
                or value.get("root") != str(Path(root).resolve())
                or value.get("request_id") != request_id
                or type(value.get("fixture")) is not bool
                or value.get("snapshot_sha256") != fingerprint(value.get("snapshot"))
                or value.get("journal_sha256") != fingerprint(
                    {key: item for key, item in value.items() if key != "journal_sha256"})):
            raise ValueError
        return path, value
    except (OSError, ValueError, TypeError, AgentError):
        raise AgentError("VK_BROWSER_JOURNAL_INVALID", 409) from None


def _one(scope, selector, code):
    node = scope.locator(selector)
    if node.count() != 1:
        raise AgentError(code)
    return node


def _text(node):
    return node.evaluate("e => ('value' in e ? e.value : e.innerText)")


def _rendered_text(node, selectors):
    if selectors.get("text_contract") != "vk-spacer-div-v1":
        return _text(node)
    result = node.evaluate("""root => {
      let invalid=false;
      const read=(n,allowBlock=false)=>{
        if(n.nodeType===Node.TEXT_NODE)return n.textContent;
        if(n.nodeType!==Node.ELEMENT_NODE){invalid=true;return '';}
        const style=getComputedStyle(n);
        if(style.display==='none'||style.visibility==='hidden'){invalid=true;return '';}
        if(n.tagName==='BR')return '\\n';
        if(!['DIV','SPAN','A'].includes(n.tagName)){invalid=true;return '';}
        if(n.tagName==='DIV'&&style.display!=='block'
           ||n.tagName!=='DIV'&&style.display!=='inline'){invalid=true;return '';}
        const children=[...n.childNodes];
        const br=children.filter(c=>c.nodeType===Node.ELEMENT_NODE&&c.tagName==='BR');
        // Native VK settings renderer inserts exactly this empty spacing DIV
        // between paragraphs. Its innerText has three NL, but source has two.
        if(n.tagName==='DIV'&&br.length===1&&children.every(c=>c===br[0]
          ||c.nodeType===Node.TEXT_NODE&&!c.textContent.trim())){
          const brStyle=getComputedStyle(br[0]);
          if(brStyle.display==='none'||brStyle.visibility==='hidden'){invalid=true;return '';}
          return '\\n\\n';
        }
        if(n.tagName==='DIV'&&!allowBlock){invalid=true;return '';}
        const elements=children.filter(c=>c.nodeType===Node.ELEMENT_NODE);
        // Observed block wrappers form one unary chain. Other block boundaries
        // have no proven source mapping and must never be flattened to text.
        const wrapper=allowBlock&&elements.length===1&&elements[0].tagName==='DIV'
          &&children.every(c=>c===elements[0]||c.nodeType===Node.TEXT_NODE&&!c.textContent.trim());
        return children.map(c=>read(c,wrapper)).join('');
      };
      return {text:read(root,true),invalid};
    }""")
    if result["invalid"]:
        raise AgentError("VK_BROWSER_RENDERED_TEXT_UNSUPPORTED")
    return result["text"]


def _text_links(node, plan):
    links = node.evaluate("""root=>[...(root.matches('a')?[root]:[]),
      ...root.querySelectorAll('a')].map(n=>n.getAttribute('href'))""")
    if any(not isinstance(href, str) or href not in plan["allowed_text_urls"] for href in links):
        raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
    return links


def _origin(page, origin):
    expected, current = urlsplit(origin), urlsplit(page.url)
    if (current.scheme, current.netloc) != (expected.scheme, expected.netloc):
        raise AgentError("VK_BROWSER_WRONG_ORIGIN")


def _blocked(page, selectors):
    if any(node.is_visible() for node in page.locator(selectors.get("blocked", "[data-vk-never-match]")).all()):
        raise AgentError("VK_BROWSER_ACCESS_BLOCKED", 403)
    if any(word in page.title().casefold() for word in ("captcha", "доступ ограничен", "just a moment")):
        raise AgentError("VK_BROWSER_ACCESS_BLOCKED", 403)


def _navigate(page, url):
    response = page.goto(url, wait_until="domcontentloaded")
    if response is not None and response.status in {401, 403, 429}:
        raise AgentError("VK_BROWSER_ACCESS_BLOCKED", 403)
    if response is not None and response.status >= 400:
        raise AgentError("VK_BROWSER_PAGE_UNAVAILABLE", 503)


def _ready(page, selectors):
    page.locator(selectors["account"]).wait_for(state="visible", timeout=10000)
    if "wall_ready" in selectors:
        page.locator(selectors["wall_ready"]).wait_for(state="visible", timeout=10000)


def _link_path(value, origin, *, author_tracking=False):
    if not isinstance(value, str):
        raise AgentError("VK_BROWSER_LINK_INVALID")
    parsed, expected = urlsplit(value), urlsplit(origin)
    if (parsed.scheme and parsed.scheme != expected.scheme
            or parsed.scheme and not parsed.netloc
            or parsed.netloc and (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc)):
        raise AgentError("VK_BROWSER_WRONG_ORIGIN")
    valid_query = author_tracking and re.fullmatch(r"trackcode=[A-Za-z0-9_-]{1,1000}", parsed.query)
    if (parsed.query and not valid_query) or parsed.fragment or not parsed.path.startswith("/"):
        raise AgentError("VK_BROWSER_LINK_INVALID")
    return parsed.path


def _context(page, plan, selectors, *, origin):
    _origin(page, origin)
    _blocked(page, selectors)
    if urlsplit(page.url).path != urlsplit(plan["wall_url"]).path:
        raise AgentError("VK_BROWSER_WALL_MISMATCH")
    account = _one(page, selectors["account"], "VK_BROWSER_ACCOUNT_AMBIGUOUS")
    if (not account.is_visible()
            or _link_path(account.get_attribute("href"), origin) != "/id" + plan["owner"]):
        raise AgentError("VK_BROWSER_ACCOUNT_MISMATCH", 403)


def _recipient(page, plan, selectors, *, origin):
    recipient = _one(page, selectors["recipient"], "VK_BROWSER_RECIPIENT_AMBIGUOUS")
    if not recipient.is_visible():
        raise AgentError("VK_BROWSER_RECIPIENT_MISMATCH")
    href, numeric = recipient.get_attribute("href"), recipient.get_attribute("data-owner-id")
    if (not href and numeric is None) or (numeric is not None and numeric != plan["destination"]):
        raise AgentError("VK_BROWSER_RECIPIENT_MISMATCH")
    if href and _link_path(href, origin) != urlsplit(plan["wall_url"]).path:
        raise AgentError("VK_BROWSER_RECIPIENT_MISMATCH")


def _personal_settings(page, plan, selectors, *, origin, require_story_disabled=True):
    if plan["destination"] != plan["owner"]:
        raise AgentError("VK_BROWSER_PERSONAL_WALL_REQUIRED")
    _context(page, plan, selectors, origin=origin)
    modal = _one(page, selectors["modal"], "VK_BROWSER_MODAL_AMBIGUOUS")
    if not modal.is_visible() or modal.get_attribute("aria-label") != "Форма постинга":
        raise AgentError("VK_BROWSER_MODAL_CONTRACT_INVALID")
    coauthors = _one(modal, selectors["coauthors"], "VK_BROWSER_COAUTHORS_AMBIGUOUS")
    if (not coauthors.is_visible() or " ".join(coauthors.inner_text().split()) != "Пригласить соавторов"
            or coauthors.locator("a").count()):
        raise AgentError("VK_BROWSER_COAUTHORS_NOT_EMPTY")
    visibility = _one(modal, selectors["visibility"], "VK_BROWSER_VISIBILITY_AMBIGUOUS")
    if (not visibility.is_visible()
            or " ".join(visibility.inner_text().split()) != "Кто увидит этот пост Все"):
        raise AgentError("VK_BROWSER_VISIBILITY_UNEXPECTED")
    story = _one(modal, selectors["story_switch"], "VK_BROWSER_STORY_SWITCH_AMBIGUOUS")
    state = story.evaluate("e=>({tag:e.tagName,type:e.type,role:e.getAttribute('role'),"
                           "checked:e.checked,aria:e.getAttribute('aria-checked')})")
    if (not story.is_visible() or not story.is_enabled() or state["tag"] != "INPUT"
            or state["type"] != "checkbox" or state["role"] != "switch"
            or type(state["checked"]) is not bool or state["aria"] != str(state["checked"]).lower()):
        raise AgentError("VK_BROWSER_STORY_SWITCH_INVALID")
    if require_story_disabled and state["checked"]:
        raise AgentError("VK_BROWSER_STORY_SHARE_NOT_DISABLED")
    other_checked = modal.evaluate("""(root, storySelector)=>[...root.querySelectorAll(
      'input[type=checkbox],input[type=radio],[role=switch],[role=checkbox],[role=radio]')].filter(n=>{
        const visible=n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
        return visible&&!n.matches(storySelector)&&(n.checked===true||n.getAttribute('aria-checked')==='true');
      }).length""", selectors["story_switch"])
    if other_checked:
        raise AgentError("VK_BROWSER_UNAPPROVED_SETTING")
    return {"identity_contract": "personal-wall-settings-v1", "author_id": plan["owner"],
            "recipient_id": plan["destination"], "visibility": "Все", "coauthors": [],
            "story_checked": state["checked"], "basis": "own_wall_account_fresh_composer"}


def _all_preview_media(page, plan, selectors, images):
    scope = _one(page, selectors["preview_scope"], "VK_BROWSER_MEDIA_SCOPE_AMBIGUOUS")
    if scope.locator("svg image").count() or scope.locator("img").count() != len(images):
        raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
    if images != _images(scope, "img"):
        raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
    if any(node.is_visible() for node in scope.locator("video,audio,iframe,object,embed,a[download]").all()):
        raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
    selected = scope.locator('input[type="file"]').evaluate_all("nodes=>nodes.reduce((n,e)=>n+e.files.length,0)")
    if selected > len(plan["media"]):
        raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
    return {"all_image_count": scope.locator("img,svg image").count(), "selected_files": selected}


def _author(scope, selector, plan, selectors, *, origin):
    """The native wall recipient and the visible publishing identity are separate."""
    expected = plan["destination"] if plan["destination"].startswith("-") else plan["owner"]
    node = _one(scope, selector, "VK_BROWSER_AUTHOR_AMBIGUOUS")
    if not node.is_visible():
        raise AgentError("VK_BROWSER_AUTHOR_MISMATCH")
    href = node.get_attribute("href") or None
    attribute = selectors.get("author_id_attribute")
    numeric = None
    if attribute is not None:
        if not re.fullmatch(r"data-[a-z][a-z0-9_-]{0,63}", attribute):
            raise AgentError("VK_BROWSER_AUTHOR_CONTRACT_INVALID")
        numeric = node.get_attribute(attribute)
        if numeric != expected:
            raise AgentError("VK_BROWSER_AUTHOR_MISMATCH")
    path = _link_path(href, origin, author_tracking=True) if href else None
    canonical = re.fullmatch(r"/(id|club)([1-9][0-9]{0,18})", path or "")
    if canonical is not None:
        actual = ("-" if canonical.group(1) == "club" else "") + canonical.group(2)
        if actual != expected:
            raise AgentError("VK_BROWSER_AUTHOR_MISMATCH")
    elif numeric is None:
        raise AgentError("VK_BROWSER_AUTHOR_ID_UNPROVEN")
    return {"id": expected, "href": href, "id_attribute": attribute, "numeric_id": numeric}


def _images(scope, selector):
    nodes = scope.locator(selector)
    result = []
    for node in nodes.all():
        if not node.is_visible():
            raise AgentError("VK_BROWSER_IMAGE_NOT_VISIBLE")
        image = node.evaluate("e => ({url:e.currentSrc||e.src||'',complete:e.complete===true,"
                              "width:e.naturalWidth||0,height:e.naturalHeight||0})")
        if not image["complete"] or min(image["width"], image["height"]) <= 0:
            raise AgentError("VK_BROWSER_IMAGE_NOT_READY")
        result.append(image)
    return result


def _image_proof(page, images, media, *, origin):
    if len(images) != len(media):
        raise AgentError("VK_BROWSER_IMAGE_COUNT_MISMATCH")
    hashes = []
    for image, expected in zip(images, media, strict=True):
        url = urlsplit(image["url"])
        allowed = (url.scheme == "https" and url.hostname is not None
                   and (url.hostname.endswith(".userapi.com") or url.hostname == "vk.ru")
                   and not url.username and not url.password and url.port in {None, 443})
        if origin != ORIGIN:
            allowed = image["url"].startswith(origin + "/")
        if not allowed:
            raise AgentError("VK_BROWSER_IMAGE_ORIGIN_INVALID")
        response = page.request.get(image["url"], fail_on_status_code=False, max_redirects=0)
        if response.status in {401, 403, 429}:
            raise AgentError("VK_BROWSER_ACCESS_BLOCKED", 403)
        if response.status != 200:
            raise AgentError("VK_BROWSER_IMAGE_READ_UNAVAILABLE")
        raw = response.body()
        if len(raw) > 10 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise AgentError("VK_BROWSER_IMAGE_READBACK_MISMATCH")
        hashes.append(expected["sha256"])
    return hashes


def _editor_check(page, plan, selectors):
    editor = _one(page, selectors["editor"], "VK_BROWSER_EDITOR_AMBIGUOUS")
    if not editor.is_visible() or _text(editor) != plan["text"]:
        raise AgentError("VK_BROWSER_EDITOR_TEXT_MISMATCH")
    images = _images(page, selectors["uploaded_images"])
    if len(images) != len(plan["media"]):
        raise AgentError("VK_BROWSER_IMAGE_COUNT_MISMATCH")
    return images


def _final_snapshot(page, plan, selectors, *, origin):
    _context(page, plan, selectors, origin=origin)
    personal = selectors.get("identity_contract") == "personal-wall-settings-v1"
    settings = _personal_settings(page, plan, selectors, origin=origin) if personal else None
    if personal:
        author = {"id": plan["owner"], "basis": "personal-wall-settings-v1"}
    else:
        _recipient(page, plan, selectors, origin=origin)
        author = _author(page, selectors["preview_author"], plan, selectors, origin=origin)
    preview = _one(page, selectors["preview_text"], "VK_BROWSER_PREVIEW_AMBIGUOUS")
    if not preview.is_visible() or _rendered_text(preview, selectors) != plan["text"]:
        raise AgentError("VK_BROWSER_PREVIEW_TEXT_MISMATCH")
    text_links = _text_links(preview, plan)
    for key in ("forbidden_settings", "extra_attachments"):
        if any(node.is_visible() for node in page.locator(selectors[key]).all()):
            raise AgentError("VK_BROWSER_UNAPPROVED_SETTING")
    if any(node.is_visible() and node.inner_text().strip()
           for node in page.locator(selectors["errors"]).all()):
        raise AgentError("VK_BROWSER_EDITOR_REJECTED")
    button = _one(page, selectors["publish"], "VK_BROWSER_PUBLISH_AMBIGUOUS")
    if (not button.is_visible() or not button.is_enabled()
            or button.inner_text().strip() != selectors["publish_label"]):
        raise AgentError("VK_BROWSER_PUBLISH_UNAVAILABLE")
    images = _images(page, selectors["preview_images"])
    if len(images) != len(plan["media"]):
        raise AgentError("VK_BROWSER_IMAGE_COUNT_MISMATCH")
    all_media = _all_preview_media(page, plan, selectors, images) if personal else None
    return {"text": _rendered_text(preview, selectors), "images": images, "author": author,
            "personal_settings": settings, "all_media": all_media, "text_links": text_links}


def _post_id(post, plan, selectors, *, origin):
    links = post.locator(selectors["post_link"])
    if links.count() != 1 or not links.is_visible():
        raise AgentError("VK_BROWSER_POST_LINK_AMBIGUOUS")
    path = _link_path(links.get_attribute("href"), origin)
    match = re.fullmatch(r"/wall(-?[1-9][0-9]{0,18})_([1-9][0-9]{0,18})", path)
    if match is None or match.group(1) != plan["destination"]:
        raise AgentError("VK_BROWSER_POST_RECIPIENT_MISMATCH")
    return match.group(1) + "_" + match.group(2)


def _matches(page, plan, selectors, *, origin):
    posts = page.locator(selectors["posts"])
    if posts.count() > 200:
        raise AgentError("VK_BROWSER_POST_LIST_UNBOUNDED")
    matches = []
    for post in posts.all():
        text = post.locator(selectors["post_text"])
        if text.count() == 1 and text.is_visible() and _rendered_text(text, selectors) == plan["text"]:
            matches.append((post, _post_id(post, plan, selectors, origin=origin)))
    return matches


def _post_snapshot(page, plan, selectors, expected_id, *, origin):
    """All visible post/recipient/media state is captured again after HTTP waits."""
    _context(page, plan, selectors, origin=origin)
    matches = _matches(page, plan, selectors, origin=origin)
    if len(matches) != 1 or matches[0][1] != expected_id:
        raise AgentError("VK_BROWSER_READBACK_NOT_EXACTLY_ONE")
    post, ident = matches[0]
    text_links = _text_links(post.locator(selectors["post_text"]), plan)
    author = _author(post, selectors["post_author"], plan, selectors, origin=origin)
    extras = [node for node in post.locator(selectors["post_extra_attachments"]).all() if node.is_visible()]
    if extras:
        raise AgentError("VK_BROWSER_READBACK_EXTRA_ATTACHMENT")
    images = _images(post, selectors["post_images"])
    if len(images) != len(plan["media"]):
        raise AgentError("VK_BROWSER_IMAGE_COUNT_MISMATCH")
    return {"external_id": ident, "text": _rendered_text(post.locator(selectors["post_text"]), selectors),
            "images": images, "extra_attachments": len(extras), "wall_url": page.url,
            "text_links": text_links,
            "author": author,
            "account_href": page.locator(selectors["account"]).get_attribute("href"),
            "post_href": post.locator(selectors["post_link"]).get_attribute("href")}


def _readback(page, plan, selectors, expected_id, *, origin):
    proofs = []
    for _ in range(2):
        _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
        _ready(page, selectors)
        before = _post_snapshot(page, plan, selectors, expected_id, origin=origin)
        hashes = _image_proof(page, before["images"], plan["media"], origin=origin)
        # Rechecking only text would miss an image/attachment/account swap while
        # the old public image bytes were being downloaded.
        if before != _post_snapshot(page, plan, selectors, expected_id, origin=origin):
            raise AgentError("VK_BROWSER_READBACK_CHANGED")
        proofs.append({"external_id": expected_id, "text_sha256": plan["text_sha256"],
                       "image_sha256": hashes, "author": before["author"], "text_links": before["text_links"]})
    return {"verification": "independent_wall_reload_exact_text_recipient_author_image_sha256",
            "reloads": proofs, "external_id": expected_id,
            "permalink": ORIGIN + "/wall" + expected_id, "title_format": "plain",
            "observed_at": time.time()}


def _result(journal, *, fixture):
    state = journal.get("state")
    fixture = fixture or journal.get("fixture") is not False
    return {"state": state if state in {"confirmed", "failed"} else "unknown",
            "external_id": journal.get("external_id"), "permalink": journal.get("permalink"),
            "verified": state == "confirmed" and not fixture,
            "evidence": {"verification": "browser_readback", "fixture": fixture,
                "independent_readback": state == "confirmed", "contract": CONTRACT,
                "request_id": journal["request_id"], "snapshot_sha256": journal["snapshot_sha256"],
                "journal_state": state, "clicks": journal.get("clicks", 0),
                "failure_code": journal.get("failure_code"),
                "sender_settings": journal.get("personal_settings"),
                "proof": journal.get("proof") if state == "confirmed" else None,
                "historical_proof": journal.get("proof") if state != "confirmed" else None}}


def run_publish(page, root, options, payload, authorize, *, origin=ORIGIN, fixture=False):
    if origin != ORIGIN and not fixture:
        raise AgentError("VK_BROWSER_FIXTURE_ORIGIN_FORBIDDEN")
    root = Path(root).resolve()
    path = _path(root, payload.get("request_id"))
    plan = prepare(root, options, payload.get("destination"), payload.get("snapshot"))
    if path.exists():
        _, journal = _read(root, payload["request_id"])
        if (journal.get("snapshot_sha256") != plan["snapshot_sha256"]
                or journal.get("destination") != plan["destination"]
                or journal.get("options_sha256") != fingerprint(options)
                or journal.get("fixture") is not fixture):
            raise AgentError("VK_BROWSER_JOURNAL_CONFLICT", 409)
        return _result(journal, fixture=fixture)
    selectors = dom_contract(options, publish=True)
    authorize()
    journal = {"contract": CONTRACT, "root": str(root), "request_id": payload["request_id"],
        "destination": plan["destination"], "snapshot": payload["snapshot"],
        "snapshot_sha256": plan["snapshot_sha256"], "options_sha256": fingerprint(options),
        "dom_contract_sha256": fingerprint(selectors), "state": "intent", "clicks": 0,
        "created_at": time.time(), "fixture": fixture}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(_seal(journal), stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
        _ready(page, selectors)
        _context(page, plan, selectors, origin=origin)
        if _matches(page, plan, selectors, origin=origin):
            raise AgentError("VK_BROWSER_DUPLICATE_POST")
        authorize()
        _save(path, journal, "editor_open_intent")
        authorize()
        _context(page, plan, selectors, origin=origin)
        if selectors.get("identity_contract") == "personal-wall-settings-v1":
            if any(node.is_visible() for node in page.locator(selectors["modal"]).all()):
                raise AgentError("VK_BROWSER_EDITOR_ALREADY_OPEN")
        _one(page, selectors["open_editor"], "VK_BROWSER_EDITOR_OPEN_AMBIGUOUS").click(timeout=1000)
        _context(page, plan, selectors, origin=origin)
        editor = _one(page, selectors["editor"], "VK_BROWSER_EDITOR_AMBIGUOUS")
        if _text(editor) or _images(page, selectors["uploaded_images"]):
            raise AgentError("VK_BROWSER_EDITOR_NOT_EMPTY")
        authorize()
        _save(path, journal, "fill_intent")
        authorize()
        _context(page, plan, selectors, origin=origin)
        if not editor.is_visible() or _text(editor) or _images(page, selectors["uploaded_images"]):
            raise AgentError("VK_BROWSER_EDITOR_NOT_EMPTY")
        if selectors.get("identity_contract") == "personal-wall-settings-v1":
            modal = _one(page, selectors["modal"], "VK_BROWSER_MODAL_AMBIGUOUS")
            if not modal.is_visible() or modal.get_attribute("aria-label") != "Форма постинга":
                raise AgentError("VK_BROWSER_MODAL_CONTRACT_INVALID")
            _all_preview_media(page, plan, selectors, [])
        editor.fill(plan["text"])
        for media in plan["media"]:
            authorize()
            raw = private_path(root, media["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != media["sha256"]:
                raise AgentError("VK_BROWSER_IMAGE_HASH_CHANGED")
            _save(path, journal, "upload_intent")
            authorize()
            _context(page, plan, selectors, origin=origin)
            if not editor.is_visible() or _text(editor) != plan["text"] or _images(page, selectors["uploaded_images"]):
                raise AgentError("VK_BROWSER_EDITOR_CHANGED_BEFORE_UPLOAD")
            _one(page, selectors["upload_input"], "VK_BROWSER_UPLOAD_AMBIGUOUS").set_input_files(
                {"name": "approved.jpg" if media["mime"] == "image/jpeg" else "approved.png",
                 "mimeType": media["mime"], "buffer": raw})
            page.wait_for_function("""selector => {
              const images=[...document.querySelectorAll(selector)];
              return images.length===1 && images.every(n=>n.complete && n.naturalWidth>0
                && n.src && !n.src.startsWith('blob:') && !n.src.startsWith('data:'));
            }""", arg=selectors["uploaded_images"])
            _save(path, journal, "uploaded")
        _editor_check(page, plan, selectors)
        next_button = page.locator(selectors.get("next", "[data-vk-never-match]"))
        if next_button.count():
            if next_button.count() != 1 or not next_button.is_visible() or not next_button.is_enabled():
                raise AgentError("VK_BROWSER_NEXT_AMBIGUOUS")
            authorize()
            _save(path, journal, "next_intent")
            authorize()
            _context(page, plan, selectors, origin=origin)
            _editor_check(page, plan, selectors)
            next_button.click(timeout=1000)
        page.locator(selectors["preview_text"]).wait_for(state="visible", timeout=10000)
        if selectors.get("identity_contract") == "personal-wall-settings-v1":
            settings = _personal_settings(page, plan, selectors, origin=origin, require_story_disabled=False)
            if settings["story_checked"]:
                authorize()
                _save(path, journal, "story_disable_intent")
                authorize()
                _personal_settings(page, plan, selectors, origin=origin, require_story_disabled=False)
                _one(page, selectors["story_switch"], "VK_BROWSER_STORY_SWITCH_AMBIGUOUS").uncheck(timeout=1000)
            _personal_settings(page, plan, selectors, origin=origin)
        before = _final_snapshot(page, plan, selectors, origin=origin)
        _image_proof(page, before["images"], plan["media"], origin=origin)
        if before != _final_snapshot(page, plan, selectors, origin=origin):
            raise AgentError("VK_BROWSER_PREVIEW_CHANGED_DURING_IMAGE_READ")
        authorize()
        _save(path, journal, "click_intent", clicks=1, personal_settings=before.get("personal_settings"))
        authorize()
        if before != _final_snapshot(page, plan, selectors, origin=origin):
            raise AgentError("VK_BROWSER_PREVIEW_CHANGED_BEFORE_CLICK")
        _one(page, selectors["publish"], "VK_BROWSER_PUBLISH_AMBIGUOUS").click(timeout=1000, no_wait_after=True)
        deadline = time.monotonic() + min(30, options.get("operation_timeout_seconds", 180))
        while True:
            matches = _matches(page, plan, selectors, origin=origin)
            if len(matches) == 1:
                external_id = matches[0][1]
                break
            if len(matches) > 1 or time.monotonic() >= deadline:
                raise AgentError("VK_BROWSER_PUBLISH_RESULT_UNKNOWN")
            page.wait_for_timeout(100)
        _save(path, journal, "readback_pending", external_id=external_id,
              permalink=ORIGIN + "/wall" + external_id)
        proof = _readback(page, plan, selectors, external_id, origin=origin)
        _save(path, journal, "confirmed", proof=proof)
    except Exception as exc:
        code = exc.code if isinstance(exc, AgentError) else "VK_BROWSER_OPERATION_UNKNOWN"
        state = "failed" if journal["state"] == "intent" else "unknown"
        try:
            _save(path, journal, state, failure_code=code,
                  private_error={"type": type(exc).__name__, "message": str(exc)})
        except (OSError, AgentError, ValueError):
            journal.update(state="unknown", failure_code="VK_BROWSER_JOURNAL_UNAVAILABLE")
    return _result(journal, fixture=fixture)


def reconcile(page, root, options, destination, request_id, *, origin=ORIGIN, fixture=False):
    if origin != ORIGIN and not fixture:
        raise AgentError("VK_BROWSER_FIXTURE_ORIGIN_FORBIDDEN")
    path, journal = _read(root, request_id)
    if (journal.get("destination") != destination or journal.get("options_sha256") != fingerprint(options)
            or journal.get("fixture") is not fixture):
        raise AgentError("VK_BROWSER_JOURNAL_CONFLICT", 409)
    if not journal.get("external_id"):
        return _result(journal, fixture=fixture)
    # A completed journal proves the earlier delivery. Reconciliation of an
    # unknown Hub dispatch must independently read today's visible native post;
    # it must never promote cached evidence to a current readback.
    plan = prepare(root, options, destination, journal["snapshot"])
    try:
        proof = _readback(page, plan, dom_contract(options, publish=True), journal["external_id"], origin=origin)
        _save(path, journal, "confirmed", proof=proof, failure_code=None)
    except Exception as exc:
        _save(path, journal, "unknown", failure_code=exc.code if isinstance(exc, AgentError)
              else "VK_BROWSER_OPERATION_UNKNOWN")
    return _result(journal, fixture=fixture)


def inspect_page(page, options, destination, *, origin=ORIGIN, fixture=False):
    if origin != ORIGIN and not fixture:
        raise AgentError("VK_BROWSER_FIXTURE_ORIGIN_FORBIDDEN")
    plan = {"destination": destination, "wall_url": wall_url(destination, options), "owner": owner_id(options)}
    selectors = dom_contract(options)
    _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
    _ready(page, selectors)
    _context(page, plan, selectors, origin=origin)
    create = page.locator(selectors["open_editor"])
    try:
        dom_contract(options, publish=True)
        contract_valid = selectors.get("identity_contract") != "personal-wall-settings-v1" or destination == owner_id(options)
    except AgentError:
        contract_valid = False
    return {"state": "access_only", "publish": contract_valid and create.count() == 1 and create.is_visible(),
            "owner": plan["owner"], "destination": destination,
            "live_verified": False, "external_mutations": 0, "fixture": fixture,
            "create_button_count": create.count()}
@contextmanager
def session(root, options, *, visible=False):
    from playwright.sync_api import sync_playwright

    profile = profile_path(root, options)
    lock = private_path(root, profile.relative_to(Path(root).resolve()).as_posix() + ".lock")
    with profile_lock(lock), sync_playwright() as runtime:
        kwargs = {"headless": False if visible else options.get("headless", True),
                  "locale": "ru-RU", "accept_downloads": False, "chromium_sandbox": True,
                  "viewport": {"width": 1280, "height": 900}}
        if options.get("channel", "chrome") != "chromium":
            kwargs["channel"] = options.get("channel", "chrome")
        context = runtime.chromium.launch_persistent_context(str(profile), **kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(min(180000, options.get("operation_timeout_seconds", 180) * 1000))
            yield page
        finally:
            context.close()


def target_options(root, destination):
    value = parse_json(private_path(root, "config/publishing.json").read_text(encoding="utf-8"))
    targets = [item for item in value.get("targets", []) if item.get("platform") == "vk"
               and item.get("route") == "vk_browser" and item.get("destination") == destination]
    if len(targets) != 1:
        raise AgentError("VK_BROWSER_TARGET_NOT_CONFIGURED")
    return targets[0].get("options", {})


def authorize(root, payload, options, *, expected_lease_owner=None):
    from .config import Settings
    from .db import Database, now
    from .hub import HubService
    from .hub_models import Dispatch
    from .server_state import require_running

    settings = Settings.load(Path(root))
    db, core = Database(settings), None
    try:
        with db.session() as transaction:
            row = transaction.get(Dispatch, payload.get("request_id"))
            if (row is None or row.state != "sending" or not row.owner
                    or expected_lease_owner is not None and row.owner != expected_lease_owner
                    or not math.isfinite(row.lease_until) or row.lease_until <= now() + 3
                    or row.due_at > now()):
                raise AgentError("VK_BROWSER_DISPATCH_NOT_APPROVED", 403)
            core = HubService(settings, db, bootstrap_targets=False)
            require_running(core, transaction)
            variant, target = core._valid_dispatch(transaction, row)
            if (target.platform != "vk" or target.route != "vk_browser"
                    or target.destination != payload.get("destination")
                    or fingerprint(target.options) != fingerprint(options)
                    or fingerprint(row.snapshot) != fingerprint(payload.get("snapshot"))
                    or fingerprint(core.snapshot(variant, target)) != fingerprint(row.snapshot)):
                raise AgentError("VK_BROWSER_DISPATCH_NOT_APPROVED", 403)
            return row.owner
    finally:
        if core is not None:
            core.close()
        db.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Отдельный пользовательский браузер VK")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--login")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        from .cli import load_env
        load_env(root)
        if args.login:
            options = target_options(root, args.login)
            with session(root, options, visible=True) as page:
                _navigate(page, wall_url(args.login, options))
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    try:
                        plan = {"wall_url": wall_url(args.login, options), "owner": owner_id(options)}
                        _context(page, plan, dom_contract(options), origin=ORIGIN)
                        print(json.dumps({"state": "authenticated", "live_verified": False}))
                        return 0
                    except AgentError as exc:
                        if exc.code not in {"VK_BROWSER_ACCOUNT_AMBIGUOUS", "VK_BROWSER_ACCOUNT_MISMATCH",
                                            "VK_BROWSER_WRONG_ORIGIN", "VK_BROWSER_WALL_MISMATCH"}:
                            raise
                        page.wait_for_timeout(500)
                raise AgentError("VK_BROWSER_LOGIN_TIMEOUT")
        payload = parse_json(sys.stdin.read(4 * 1024 * 1024))
        if not isinstance(payload, dict) or payload.get("action") not in {"inspect", "publish", "reconcile"}:
            raise AgentError("VK_BROWSER_REQUEST_INVALID")
        options = target_options(root, payload.get("destination"))
        if payload.get("options_sha256") != fingerprint(options):
            raise AgentError("VK_BROWSER_CONFIG_CHANGED")
        lease_owner = authorize(root, payload, options) if payload["action"] == "publish" else None
        with session(root, options) as page:
            if payload["action"] == "inspect":
                result = inspect_page(page, options, payload["destination"])
            elif payload["action"] == "reconcile":
                result = reconcile(page, root, options, payload["destination"], payload["request_id"])
            else:
                result = run_publish(page, root, options, payload,
                    lambda: authorize(root, payload, options, expected_lease_owner=lease_owner))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({"code": exc.code if isinstance(exc, AgentError) else "VK_BROWSER_OPERATION_UNKNOWN"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
