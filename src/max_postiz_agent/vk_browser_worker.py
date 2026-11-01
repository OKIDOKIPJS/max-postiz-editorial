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
from urllib.parse import parse_qs, urlsplit

from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, parse_json
from .vk_browser import (
    CONTRACT, ORIGIN, dom_contract, owner_id, prepare, private_path, profile_path, wall_url,
)


WORKER_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


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
    if state.endswith("_intent"):
        journal["last_action_intent"] = state
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
    if selectors.get("text_contract") not in {"vk-spacer-div-v1", "vk-native-text-v2"}:
        return _text(node)
    result = node.evaluate("""(root,nativeV2) => {
      let invalid=false;
      const nativeText=n=>!!n&&['showmoretext-in','showmoretext-in-expanded'].includes(n.getAttribute('data-testid'));
      if(nativeV2){
        for(let n=root;n;n=n.parentElement){const s=getComputedStyle(n);
          if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)===0)invalid=true;}
      }
      const read=(n,allowBlock=false)=>{
        if(n.nodeType===Node.TEXT_NODE)return n.textContent;
        if(n.nodeType!==Node.ELEMENT_NODE){invalid=true;return '';}
        const style=getComputedStyle(n);
        if(style.display==='none'||style.visibility==='hidden'
           ||nativeV2&&(style.visibility!=='visible'||Number(style.opacity)===0)){invalid=true;return '';}
        if(n.tagName==='BR')return '\\n';
        if(!['DIV','SPAN','A'].includes(n.tagName)){invalid=true;return '';}
        const nativeWrapper=nativeV2&&allowBlock&&n.tagName==='DIV'&&(
          nativeText(n)&&['inline','flow-root'].includes(style.display)
          ||nativeText(n.parentElement)&&n.parentElement.children.length===1&&style.display==='inline');
        const nativePost=nativeV2&&n===root&&n.tagName==='SPAN'
          &&n.getAttribute('data-testid')==='post_text'&&style.display==='block';
        if(!nativeWrapper&&!nativePost&&(n.tagName==='DIV'&&style.display!=='block'
           ||n.tagName!=='DIV'&&style.display!=='inline')){invalid=true;return '';}
        const children=[...n.childNodes];
        const br=children.filter(c=>c.nodeType===Node.ELEMENT_NODE&&c.tagName==='BR');
        // Native VK settings renderer inserts exactly this empty spacing DIV
        // between paragraphs. Its innerText has three NL, but source has two.
        if(n.tagName==='DIV'&&br.length===1&&children.every(c=>c===br[0]
          ||c.nodeType===Node.TEXT_NODE&&!c.textContent.trim())){
          const brStyle=getComputedStyle(br[0]);
          if(brStyle.display==='none'||brStyle.visibility==='hidden'
             ||nativeV2&&(brStyle.visibility!=='visible'||Number(brStyle.opacity)===0)){invalid=true;return '';}
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
    }""", selectors.get("text_contract") == "vk-native-text-v2")
    if result["invalid"]:
        raise AgentError("VK_BROWSER_RENDERED_TEXT_UNSUPPORTED")
    return result["text"]


def _text_links(node, plan):
    links = node.evaluate("""root=>[...(root.matches('a')?[root]:[]),
      ...root.querySelectorAll('a')].map(n=>n.getAttribute('href'))""")
    if plan.get("image_contract"):
        from .vk_media import LINK_CONTRACT, approved_link
        if plan.get("link_contract") != LINK_CONTRACT:
            raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
        return [approved_link(href, plan["allowed_text_urls"]) for href in links]
    if any(not isinstance(href, str) or href not in plan["allowed_text_urls"] for href in links):
        raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
    return links


def _caption_text(node, plan, selectors):
    """The image route alone projects the observed native caption/link grammar."""
    if not plan.get("image_contract"):
        return _rendered_text(node, selectors)
    from .vk_media import LINK_CONTRACT, rendered_approved_link
    if plan.get("link_contract") != LINK_CONTRACT or selectors.get("text_contract") != "vk-native-text-v2":
        raise AgentError("VK_BROWSER_RENDERED_TEXT_UNSUPPORTED")
    result = node.evaluate("""root=>{
      let invalid=false;
      const id=e=>e.getAttribute('data-testid');
      const visible=e=>{for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
        if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)===0)return false;}return true;};
      const bare=(e,expected)=>[...e.childNodes].every(n=>expected.includes(n)||n.nodeType===Node.TEXT_NODE&&n.textContent==='');
      const style=(e,allowed)=>visible(e)&&allowed.includes(getComputedStyle(e).display);
      let content=root;
      if(root.tagName==='SPAN'&&id(root)==='post_text'){
        if(!style(root,['block']))invalid=true;
        const children=[...root.children];
        if(children.length===1&&children[0].tagName==='DIV'&&id(children[0])==='showmoretext'){
          const host=children[0],parts=[...host.children];
          if(!bare(root,[host])||!style(host,['block','flow-root'])||!bare(host,parts))invalid=true;
          const bodies=parts.filter(n=>n.tagName==='DIV'&&['showmoretext-in','showmoretext-in-expanded'].includes(id(n)));
          const after=parts.filter(n=>n.tagName==='DIV'&&id(n)==='showmoretext-after');
          if(bodies.length!==1||after.length>1||parts.length!==bodies.length+after.length
             ||parts[0]!==bodies[0])invalid=true;
          if(bodies.length===1){content=bodies[0];
            if(after.length){const control=after[0],buttons=[...control.children];
              if(id(content)!=='showmoretext-in'||parts[1]!==control||!style(control,['block'])
                 ||buttons.length!==1||!bare(control,buttons))invalid=true;
              else {const b=buttons[0];if(b.tagName!=='SPAN'||b.getAttribute('role')!=='button'
                ||b.getAttribute('data-id')!=='showMoreButton'||b.getAttribute('tabindex')!=='0'
                ||b.children.length||b.textContent!=='Показать ещё'||!style(b,['inline'])
                ||[...b.childNodes].some(n=>n.nodeType!==Node.TEXT_NODE))invalid=true;}
            }
          }
        }
      }else if(root.tagName!=='DIV'||!['showmoretext-in','showmoretext-in-expanded'].includes(id(root)))invalid=true;
      if(content!==root&&!style(content,['block','flow-root','inline']))invalid=true;
      if(content===root&&root.tagName==='DIV'&&!style(root,['block','flow-root','inline']))invalid=true;
      const inline=(n,inLink=false)=>{
        if(n.nodeType===Node.TEXT_NODE)return[n.textContent];
        if(n.nodeType!==Node.ELEMENT_NODE||!visible(n)){invalid=true;return[];}
        if(n.tagName==='BR'&&!inLink)return['\\n'];
        if(!['SPAN','A'].includes(n.tagName)||!style(n,['inline'])){invalid=true;return[];}
        if(n.tagName==='A'){
          if(inLink){invalid=true;return[];}
          const pieces=[...n.childNodes].flatMap(c=>inline(c,true));
          if(pieces.some(p=>typeof p!=='string')){invalid=true;return[];}
          return[{href:n.getAttribute('href'),label:pieces.join('')}];
        }
        return[...n.childNodes].flatMap(c=>inline(c,inLink));
      };
      const spacer=n=>{const parts=[...n.childNodes],br=parts.filter(c=>c.nodeType===Node.ELEMENT_NODE&&c.tagName==='BR');
        return br.length===1&&parts.every(c=>c===br[0]||c.nodeType===Node.TEXT_NODE&&['',' '].includes(c.textContent));};
      const body=e=>[...e.childNodes].flatMap(n=>{
        if(n.nodeType===Node.ELEMENT_NODE&&n.tagName==='DIV'){
          const parts=[...n.childNodes],br=parts.filter(c=>c.nodeType===Node.ELEMENT_NODE&&c.tagName==='BR');
          if(!style(n,['block'])||br.length!==1||!visible(br[0])
             ||!parts.every(c=>c===br[0]||c.nodeType===Node.TEXT_NODE&&['',' '].includes(c.textContent)))invalid=true;
          return['\\n\\n'];
        }
        return inline(n);
      });
      // Exactly the observed unary content wrapper; arbitrary nested blocks are rejected.
      const kids=[...content.children];let textRoot=content;
      if(['showmoretext-in','showmoretext-in-expanded'].includes(id(content))
         &&kids.length===1&&kids[0].tagName==='DIV'&&bare(content,kids)&&!spacer(kids[0])){
        textRoot=kids[0];if(!style(textRoot,['inline','block']))invalid=true;
      }
      return {tokens:body(textRoot),invalid};
    }""")
    if result["invalid"]:
        raise AgentError("VK_BROWSER_RENDERED_TEXT_UNSUPPORTED")
    return "".join(token if isinstance(token, str) else rendered_approved_link(
        token["href"], token["label"], plan["allowed_text_urls"]) for token in result["tokens"])


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
    # The native SPA first renders a visible BUTTON with an empty href, then
    # hydrates the account link. Visibility alone does not prove identity.
    page.wait_for_function("""selector => {
      const nodes=document.querySelectorAll(selector);
      return nodes.length===1&&typeof nodes[0].getAttribute('href')==='string'
        &&nodes[0].getAttribute('href').trim()!==''&&nodes[0].getAttribute('href')!=='#';
    }""", arg=selectors["account"], timeout=10000)
    if "wall_ready" in selectors:
        page.locator(selectors["wall_ready"]).wait_for(state="visible", timeout=10000)


def _editor_ready(page, selectors, *, timeout=10000):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.locator(selectors["open_editor"]).wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        return False
    node = _one(page, selectors["open_editor"], "VK_BROWSER_EDITOR_OPEN_AMBIGUOUS")
    return node.is_visible() and node.is_enabled()


def _wait_control_ready(page, selector, code, *, timeout=10000):
    """Wait for a native SPA transition without granting another mutation."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    node = page.locator(selector)
    if node.count() > 1:
        raise AgentError(code)
    try:
        node.wait_for(state="visible", timeout=timeout)
        page.wait_for_function("""selector => {
          const nodes=document.querySelectorAll(selector);
          return nodes.length===1&&!nodes[0].matches(':disabled')
            &&nodes[0].getAttribute('aria-disabled')!=='true';
        }""", arg=selector, timeout=timeout)
    except PlaywrightTimeoutError:
        raise AgentError(code) from None
    return _one(page, selector, code)


def _wait_story_disabled(page, selectors, *, timeout=10000):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_function("""selector => {
          const nodes=document.querySelectorAll(selector);
          return nodes.length===1&&nodes[0].checked===false
            &&nodes[0].getAttribute('aria-checked')==='false';
        }""", arg=selectors["story_switch"], timeout=timeout)
    except PlaywrightTimeoutError:
        raise AgentError("VK_BROWSER_STORY_SHARE_NOT_DISABLED") from None


def _mutation_stamp(page, selectors):
    """One read-only DOM capture; no locator actionability waits after SQL fencing."""
    keys = ("account", "open_editor", "editor", "modal", "upload_input", "uploaded_images",
            "next", "recipient", "preview_author", "preview_text", "preview_images", "publish",
            "story_switch", "story_toggle_label", "coauthors", "visibility", "forbidden_settings", "extra_attachments",
            "uploaded_asset", "preview_asset", "image_edit_active", "blocked", "errors")
    try:
        result = page.evaluate("""selectors => {
          const state = e => {
            const s=getComputedStyle(e), r=e.getBoundingClientRect();
            return {value:'value' in e?e.value:null,checked:'checked' in e?e.checked:null,
              disabled:'disabled' in e?e.disabled:null,inert:e.inert,
              files:e.files?[...e.files].map(f=>[f.name,f.size,f.lastModified]):null,
              image:e.tagName==='IMG'?{url:e.currentSrc||e.src,complete:e.complete,
                width:e.naturalWidth,height:e.naturalHeight}:null,
              visible:r.width>0&&r.height>0,display:s.display,visibility:s.visibility,
              opaque:Number(s.opacity)>0,pointerEvents:s.pointerEvents,clip:s.clip,clipPath:s.clipPath};
          };
          const capture = e => {
            const nodes=[e,...e.querySelectorAll('*')];
            if(nodes.length>2000)throw new Error('unbounded');
            const parents=[];for(let n=e.parentElement;n;n=n.parentElement)parents.push(state(n));
            return {html:e.outerHTML,states:nodes.map(state),parents};
          };
          const modals=selectors.modal?[...document.querySelectorAll(selectors.modal)]:[];
          const inComposer=modals.length===1&&state(modals[0]).visible
            &&state(modals[0]).display!=='none'&&state(modals[0]).visibility==='visible'
            &&state(modals[0]).opaque;
          const result={url:location.href,title:document.title,
            phase:inComposer?'composer':'wall',nodes:{}};
          for(const [key,selector] of Object.entries(selectors)) {
            // Фоновая CTA после открытия формы обновляется независимо от неё.
            // Сама форма, содержимое, настройки и текущий аккаунт остаются в снимке.
            if(inComposer&&key==='open_editor')continue;
            const nodes=[...document.querySelectorAll(selector)];
            if(nodes.length>100)throw new Error('unbounded');
            result.nodes[key]=nodes.map(capture);
          }
          if(JSON.stringify(result).length>2000000)throw new Error('unbounded');
          return result;
        }""", {key: selectors[key] for key in keys if key in selectors})
        return fingerprint(result)
    except Exception:
        raise AgentError("VK_BROWSER_MUTATION_STATE_UNAVAILABLE") from None


def _mutate(page, plan, selectors, authorize, check, action, *, origin, _clock=None):
    """Expensive guards precede a fresh SQL fence and a bounded native action."""
    stamp = _mutation_stamp(page, selectors)
    _context(page, plan, selectors, origin=origin)
    check()
    if stamp != _mutation_stamp(page, selectors):
        raise AgentError("VK_BROWSER_STATE_CHANGED_DURING_GUARD")
    clock = time.monotonic if _clock is None else _clock
    deadline = clock() + 1
    authorize()
    if clock() >= deadline:
        raise AgentError("VK_BROWSER_ACTION_AUTHORIZATION_EXPIRED")
    if stamp != _mutation_stamp(page, selectors):
        # Keep precise account/text rejection when a callback changed the DOM.
        _context(page, plan, selectors, origin=origin)
        check()
        raise AgentError("VK_BROWSER_STATE_CHANGED_AFTER_AUTHORIZATION")
    remaining = int((deadline - clock()) * 1000)
    if remaining < 1:
        raise AgentError("VK_BROWSER_ACTION_AUTHORIZATION_EXPIRED")
    return action(min(1000, remaining))


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


def _restore_login_wall(page, plan, selectors, *, origin):
    """The owner login may redirect once to /feed; return only a proven owner."""
    _origin(page, origin)
    _blocked(page, selectors)
    if urlsplit(page.url).path != "/feed":
        return False
    account = page.locator(selectors["account"])
    if account.count() != 1 or not account.is_visible():
        return False
    href = account.get_attribute("href")
    if not href:
        return False
    try:
        if _link_path(href, origin) != "/id" + plan["owner"]:
            return False
    except AgentError:
        return False
    _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
    return True


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
    if "story_toggle_label" in selectors:
        label = _one(modal, selectors["story_toggle_label"], "VK_BROWSER_STORY_LABEL_AMBIGUOUS")
        if (not label.is_visible() or not label.evaluate("""(e,selector)=>e.tagName==='LABEL'
                &&e.querySelectorAll(selector).length===1&&e.querySelector(selector).parentElement===e""",
                selectors["story_switch"])):
            raise AgentError("VK_BROWSER_STORY_LABEL_INVALID")
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
    if plan.get("image_contract"):
        from .vk_media import upload_filename
        expected = sum(len(image["representations"]) for image in images)
        if scope.locator("img,svg image").count() != expected:
            raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
        if scope.locator("video,audio,iframe,object,embed,canvas,a[download],[data-testid=chip-attachment]").count():
            raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
        files = scope.locator('input[type="file"]').evaluate_all(
            "nodes=>nodes.flatMap(e=>[...e.files].map(f=>({name:f.name,bytes:f.size,type:f.type})))")
        expected_file = {"name": upload_filename(plan["media"][0]), "bytes": plan["source_image"]["bytes"],
                         "type": plan["media"][0]["mime"]}
        if files and (not images or files != [expected_file]):
            raise AgentError("VK_BROWSER_UNAPPROVED_MEDIA")
        return {"all_image_count": expected, "selected_files": len(files)}
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


def _native_images(scope, plan, selectors, stage, *, origin):
    """Atomic image/asset containment; preview proves URL lineage, never an ID."""
    from .vk_media import allowed_cdn, native_asset_id, validate_transform
    key = {"uploaded": "uploaded_images", "preview": "preview_images", "post": "post_images"}[stage]
    root = scope if stage == "post" else scope.locator("html")
    captured = root.evaluate(r"""(root,a)=>{
      const visible=e=>{const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
        for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n),b=n.getBoundingClientRect();
          if(s.display==='none'||s.visibility!=='visible'||Number(s.opacity)===0)return false;
          if(['hidden','clip'].includes(s.overflowX)&&(r.left<b.left-1||r.right>b.right+1))return false;
          if(['hidden','clip'].includes(s.overflowY)&&(r.top<b.top-1||r.bottom>b.bottom+1))return false;
        }return true;};
      const assets=[...root.querySelectorAll(a.asset)],nodes=[...root.querySelectorAll(a.images)];
      const asset=assets.length===1?assets[0]:null;
      const grids=a.grid?[...root.querySelectorAll(a.grid)]:[];
      const transforms=a.transform?[...root.querySelectorAll(a.transform)]:[];
      let semanticAnchor=false;
      if(a.post&&asset&&asset.tagName==='A'&&asset.getAttribute('data-testid')==='primary-attachment-interactive-wrapper'
         &&asset.getAttribute('aria-label')==='Фотография'&&nodes.length===1&&nodes[0].parentElement===asset
         &&asset.children.length===1&&asset.firstElementChild===nodes[0]
         &&[...asset.childNodes].every(n=>n===nodes[0]||n.nodeType===Node.TEXT_NODE&&n.textContent==='')){
        const s=getComputedStyle(asset),r=asset.getBoundingClientRect();
        semanticAnchor=s.display==='inline'&&r.width===0&&r.height===0&&s.overflowX==='visible'&&s.overflowY==='visible'
          &&s.clip==='auto'&&s.clipPath==='none'&&s.transform==='none'&&s.visibility==='visible'&&Number(s.opacity)===1
          &&visible(nodes[0]);
      }
      let transform=null;
      if(transforms.length===1){const s=getComputedStyle(transforms[0]);
        transform=Object.fromEntries(Object.entries({ratio:'--photo-primary-ratio',width:'--photo-width',
          height:'--photo-height',scale:'--photo-scale',x:'--photo-translate-x',y:'--photo-translate-y',
          container_width:'--photo-container-width',container_height:'--photo-container-height'})
          .map(([key,value])=>{const raw=s.getPropertyValue(value).trim(),unit=['ratio','scale'].includes(key)?'':'px';
            const pattern=unit?/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?px$/i:
              /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
            return[key,pattern.test(raw)?Number(unit?raw.slice(0,-2):raw):null];}));}
      return {asset_count:assets.length,asset_visible:!!asset&&(visible(asset)||semanticAnchor),semantic_anchor:semanticAnchor,
        asset_value:asset&&a.attribute?asset.getAttribute(a.attribute):null,
        asset_image_count:asset?asset.querySelectorAll('img').length:0,
        unsupported:!!asset&&asset.querySelectorAll('video,audio,iframe,object,embed,canvas,svg image').length>0,
        grid_ok:!a.grid||(grids.length===1&&!!asset&&grids[0].contains(asset)),transform,
        images:nodes.map(n=>{const r=n.getBoundingClientRect();return{url:n.currentSrc||n.src||'',
          complete:n.complete===true,width:n.naturalWidth,height:n.naturalHeight,
          visible:visible(n),contained:!!asset&&asset.contains(n),box_width:r.width,box_height:r.height};})};
    }""", {"asset": selectors[stage + "_asset"], "images": selectors[key], "post": stage == "post",
            "attribute": selectors.get(stage + "_asset_id_attribute") if stage != "preview" else None,
            "transform": selectors.get(stage + "_transform") if stage != "post" else None,
            "grid": selectors.get("preview_grid") if stage == "preview" else None})
    count = 2 if stage == "preview" else 1
    images = captured["images"]
    if (captured["asset_count"] != 1 or not captured["asset_visible"] or captured["unsupported"]
            or not captured["grid_ok"] or captured["asset_image_count"] != count or len(images) != count
            or any(not image["contained"] for image in images)):
        raise AgentError("VK_BROWSER_PHOTO_ASSET_AMBIGUOUS")
    if (any(not image["complete"] or min(image["width"], image["height"]) <= 0
            or not allowed_cdn(image["url"], origin=origin) for image in images)
            or len({(image["url"], image["width"], image["height"]) for image in images}) != 1):
        raise AgentError("VK_BROWSER_IMAGE_NOT_READY")
    if stage != "post" and [images[0]["width"], images[0]["height"]] != plan["source_image"]["dimensions"]:
        raise AgentError("VK_BROWSER_PHOTO_TRANSFORM_UNAPPROVED")
    transform = None
    if stage != "post":
        transform = validate_transform(captured["transform"], plan["source_image"]["dimensions"])
        if not any(image["visible"] and abs(image["box_width"] - transform["width"]) <= 1
                   and abs(image["box_height"] - transform["height"]) <= 1 for image in images):
            raise AgentError("VK_BROWSER_IMAGE_NOT_VISIBLE")
    elif not images[0]["visible"]:
        raise AgentError("VK_BROWSER_IMAGE_NOT_VISIBLE")
    result = {key: images[0][key] for key in ("url", "complete", "width", "height")}
    result.update(representations=images, transform=transform)
    if stage == "preview":
        result["binding"] = "uploaded-cdn-url-v1"
    else:
        result.update(binding="native-photo-id-v1", asset_id=native_asset_id(captured["asset_value"],
            plan["owner"], selectors[stage + "_asset_id_attribute"], origin=origin))
    return [result]


def _photo_context(page, plan, selectors, expected_asset, *, origin):
    from .vk_media import asset_id
    _origin(page, origin)
    _blocked(page, selectors)
    account = _one(page, selectors["account"], "VK_BROWSER_ACCOUNT_AMBIGUOUS")
    if not account.is_visible() or _link_path(account.get_attribute("href"), origin) != "/id" + plan["owner"]:
        raise AgentError("VK_BROWSER_ACCOUNT_MISMATCH")
    parsed = urlsplit(page.url)
    direct = parsed.path == "/photo" + expected_asset and not parsed.query
    query = parse_qs(parsed.query, keep_blank_values=True)
    album = "/album" + plan["owner"] + "_00"
    redirected = parsed.path == album and query == {"z": ["photo" + expected_asset + album]}
    if parsed.fragment or not (direct or redirected) or asset_id(expected_asset, plan["owner"]) != expected_asset:
        raise AgentError("VK_BROWSER_PHOTO_VIEWER_ID_MISMATCH")


def _photo_original(page, plan, selectors, expected_asset, *, origin, media_review=None):
    """Read only the known photo viewer's Original link, never edit/crop/delete."""
    from .vk_media import allowed_cdn, derivative_proof
    viewer = page.context.new_page()
    try:
        viewer.set_default_timeout(10000)
        _navigate(viewer, origin + "/photo" + expected_asset)
        viewer.locator(selectors["account"]).wait_for(state="visible", timeout=10000)
        viewer.wait_for_function("""selector=>{const e=document.querySelector(selector);
          return e&&e.getAttribute('href')&&e.getAttribute('href')!=='#';}""", arg=selectors["account"], timeout=10000)
        _photo_context(viewer, plan, selectors, expected_asset, origin=origin)
        viewer.locator(selectors["photo_viewer"]).wait_for(state="visible", timeout=10000)
        container = _one(viewer, selectors["photo_viewer"], "VK_BROWSER_PHOTO_VIEWER_AMBIGUOUS")
        more = container.get_by_role("button", name=re.compile(r"^Ещё\s*$"))
        more.wait_for(state="visible", timeout=10000)
        if more.count() != 1 or not more.is_visible() or not more.is_enabled():
            raise AgentError("VK_BROWSER_PHOTO_ORIGINAL_UNAVAILABLE")
        _photo_context(viewer, plan, selectors, expected_asset, origin=origin)
        more.click(timeout=1000, no_wait_after=True)
        viewer.locator(selectors["photo_original"]).wait_for(state="visible", timeout=10000)
        original = _one(viewer, selectors["photo_original"], "VK_BROWSER_PHOTO_ORIGINAL_AMBIGUOUS")
        viewer.locator(selectors["photo_viewer_image"]).wait_for(state="visible", timeout=10000)
        image = _one(viewer, selectors["photo_viewer_image"], "VK_BROWSER_PHOTO_VIEWER_AMBIGUOUS")
        def capture():
            _photo_context(viewer, plan, selectors, expected_asset, origin=origin)
            current = original.evaluate("e=>({url:e.href,label:e.innerText,visible:!!e.getClientRects().length})")
            if (not current["visible"] or current["label"].strip() != "Открыть оригинал"
                    or not allowed_cdn(current["url"], origin=origin)):
                raise AgentError("VK_BROWSER_PHOTO_ORIGINAL_UNAVAILABLE")
            main = image.evaluate("e=>({url:e.currentSrc||e.src,complete:e.complete,width:e.naturalWidth,height:e.naturalHeight})")
            if not main["complete"] or min(main["width"], main["height"]) <= 0 or not allowed_cdn(main["url"], origin=origin):
                raise AgentError("VK_BROWSER_IMAGE_NOT_READY")
            if media_review:
                from .vk_media_review import reviewed_preview_matches
                if not reviewed_preview_matches(main, current["url"], media_review, origin=origin):
                    raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_INVALID")
            return {"url": viewer.url, "original": current["url"], "main": main, "asset_id": expected_asset}
        before = capture()
        response = viewer.request.get(before["original"], fail_on_status_code=False, max_redirects=0)
        if response.status in {401, 403, 429}:
            raise AgentError("VK_BROWSER_ACCESS_BLOCKED")
        if response.status != 200:
            raise AgentError("VK_BROWSER_IMAGE_READ_UNAVAILABLE")
        if media_review:
            from .vk_media_review import accepted_proof
            proof = accepted_proof(response.body(), plan["source_image"], plan["image_contract"], expected_asset,
                                   media_review, before["original"], origin=origin)
        else:
            proof = derivative_proof(response.body(), plan["source_image"], expected_asset, plan["image_contract"])
        after = capture()
        if before != after:
            raise AgentError("VK_BROWSER_PHOTO_VIEWER_CHANGED_DURING_READ")
        proof.update(original_url=before["original"], viewer_sha256=fingerprint(after))
        return proof
    finally:
        viewer.close()


def _native_image_proof(page, images, plan, selectors, upload, request_id, *, origin, media_review=None):
    from .vk_media import validate_upload
    if (not validate_upload(upload, plan, request_id, origin=origin) or len(images) != 1
            or images[0].get("asset_id") != upload["asset_id"]):
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    if media_review and upload["asset_id"] != media_review["details"]["asset_id"]:
        raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_INVALID")
    return [_photo_original(page, plan, selectors, upload["asset_id"], origin=origin, media_review=media_review)]


def _cdn_bytes(page, url, *, origin):
    from .vk_media import MAX_BYTES, allowed_cdn
    if not allowed_cdn(url, origin=origin):
        raise AgentError("VK_BROWSER_IMAGE_ORIGIN_INVALID")
    response = page.request.get(url, fail_on_status_code=False, max_redirects=0)
    if response.status in {401, 403, 429}:
        raise AgentError("VK_BROWSER_ACCESS_BLOCKED")
    if response.status != 200:
        raise AgentError("VK_BROWSER_IMAGE_READ_UNAVAILABLE")
    raw = response.body()
    if not 0 < len(raw) <= MAX_BYTES:
        raise AgentError("VK_BROWSER_PHOTO_SIZE_INVALID")
    return raw


def _native_preview_proof(page, images, plan, upload, request_id, *, origin):
    from .vk_media import derivative_proof, validate_upload
    if (not validate_upload(upload, plan, request_id, origin=origin) or len(images) != 1
            or images[0].get("binding") != "uploaded-cdn-url-v1"
            or images[0]["url"] != upload["uploaded_url"]):
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    raw = _cdn_bytes(page, images[0]["url"], origin=origin)
    if hashlib.sha256(raw).hexdigest() != upload["uploaded_derivative_sha256"]:
        raise AgentError("VK_BROWSER_PHOTO_PREVIEW_CHANGED")
    return derivative_proof(raw, plan["source_image"], upload["asset_id"], plan["image_contract"])


def _editor_check(page, plan, selectors, *, origin=ORIGIN):
    editor = _one(page, selectors["editor"], "VK_BROWSER_EDITOR_AMBIGUOUS")
    if not editor.is_visible() or _text(editor) != plan["text"]:
        raise AgentError("VK_BROWSER_EDITOR_TEXT_MISMATCH")
    images = _native_images(page, plan, selectors, "uploaded", origin=origin) if plan.get("image_contract") else _images(page, selectors["uploaded_images"])
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
    if not preview.is_visible() or _caption_text(preview, plan, selectors) != plan["text"]:
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
    images = _native_images(page, plan, selectors, "preview", origin=origin) if plan.get("image_contract") else _images(page, selectors["preview_images"])
    if len(images) != len(plan["media"]):
        raise AgentError("VK_BROWSER_IMAGE_COUNT_MISMATCH")
    all_media = _all_preview_media(page, plan, selectors, images) if personal else None
    caption = _caption_text(preview, plan, selectors)
    if caption != plan["text"]:
        raise AgentError("VK_BROWSER_PREVIEW_TEXT_MISMATCH")
    return {"text": caption, "images": images, "author": author,
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


def _could_match_text(node, expected):
    """Only exclude different history; hidden/decorative text stays conservative."""
    raw = node.text_content()
    if not isinstance(raw, str) or len(raw) > 200000:
        raise AgentError("VK_BROWSER_POST_TEXT_UNBOUNDED")
    needle = "".join(expected.split())
    if not needle:
        return True
    position = 0
    # A supported renderer emits text-node characters plus whitespace. Keep
    # even unsupported nodes whose raw text could contain the exact candidate,
    # including hidden additions interleaved between its visible characters.
    for char in raw:
        if char == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


def _matches(page, plan, selectors, *, origin):
    posts = page.locator(selectors["posts"])
    if posts.count() > 200:
        raise AgentError("VK_BROWSER_POST_LIST_UNBOUNDED")
    matches = []
    for post in posts.all():
        text = post.locator(selectors["post_text"])
        if text.count() != 1 or not text.is_visible():
            continue
        candidate = (re.sub(r"https?://[^\s<>\"']+", "", plan["text"])
                     if plan.get("image_contract") else plan["text"])
        if (selectors.get("text_contract") in {"vk-spacer-div-v1", "vk-native-text-v2"}
                and not _could_match_text(text, candidate)):
            continue
        if _caption_text(text, plan, selectors) == plan["text"]:
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
    if plan.get("image_contract"):
        if post.get_attribute("data-post-id") != expected_id:
            raise AgentError("VK_BROWSER_POST_RECIPIENT_MISMATCH")
        if (post.locator('[data-testid="post-content-container"] img').count() != 1
                or post.locator(selectors["post_extra_attachments"]).count()):
            raise AgentError("VK_BROWSER_READBACK_EXTRA_ATTACHMENT")
    images = _native_images(post, plan, selectors, "post", origin=origin) if plan.get("image_contract") else _images(post, selectors["post_images"])
    if len(images) != len(plan["media"]):
        raise AgentError("VK_BROWSER_IMAGE_COUNT_MISMATCH")
    caption = _caption_text(post.locator(selectors["post_text"]), plan, selectors)
    if caption != plan["text"]:
        raise AgentError("VK_BROWSER_READBACK_TEXT_MISMATCH")
    return {"external_id": ident, "text": caption,
            "images": images, "extra_attachments": len(extras), "wall_url": page.url,
            "text_links": text_links,
            "author": author,
            "account_href": page.locator(selectors["account"]).get_attribute("href"),
            "post_href": post.locator(selectors["post_link"]).get_attribute("href")}


def _wait_readback_post(page, plan, selectors, expected_id, *, origin, timeout=10000):
    """Read-only SPA hydration wait; readiness never replaces the full proof."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        _context(page, plan, selectors, origin=origin)
        matches = _matches(page, plan, selectors, origin=origin)
        if matches:
            # Check ALL exact native candidates. Never filter down to a preferred ID.
            if len(matches) != 1 or matches[0][1] != expected_id:
                raise AgentError("VK_BROWSER_READBACK_NOT_EXACTLY_ONE")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("VK_BROWSER_READBACK_NOT_EXACTLY_ONE")
        page.wait_for_timeout(min(100, max(1, int(remaining * 1000))))


def _readback(page, plan, selectors, expected_id, *, origin, timeout=10000, upload=None, request_id=None, media_review=None):
    if media_review and expected_id != media_review["details"]["external_id"]:
        raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_INVALID")
    proofs = []
    for _ in range(2):
        _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
        _ready(page, selectors)
        _wait_readback_post(page, plan, selectors, expected_id, origin=origin, timeout=timeout)
        before = _post_snapshot(page, plan, selectors, expected_id, origin=origin)
        hashes = (_native_image_proof(page, before["images"], plan, selectors, upload, request_id, origin=origin,
                                     **({"media_review": media_review} if media_review else {}))
                  if plan.get("image_contract") else _image_proof(page, before["images"], plan["media"], origin=origin))
        # Rechecking only text would miss an image/attachment/account swap while
        # the old public image bytes were being downloaded.
        if before != _post_snapshot(page, plan, selectors, expected_id, origin=origin):
            raise AgentError("VK_BROWSER_READBACK_CHANGED")
        proofs.append({"external_id": expected_id, "text_sha256": plan["text_sha256"],
                       "image_proofs" if plan.get("image_contract") else "image_sha256": hashes,
                       "author": before["author"], "text_links": before["text_links"],
                       **({"link_contract": plan["link_contract"]} if plan.get("image_contract") else {})})
    from .vk_media_review import VERIFICATION, reference
    return {"verification": (VERIFICATION if media_review else "independent_wall_reload_exact_text_recipient_author_native_photo_v1" if plan.get("image_contract")
                             else "independent_wall_reload_exact_text_recipient_author_image_sha256"),
            "reloads": proofs, "external_id": expected_id,
            "permalink": ORIGIN + "/wall" + expected_id, "title_format": "plain",
            **({"upload": upload} if plan.get("image_contract") else {}),
            **({"media_review": reference(media_review), "original_policy_satisfied": False} if media_review else {}),
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
        from .vk_media_review import VERIFICATION
        if isinstance(journal.get("proof"), dict) and ("media_review" in journal["proof"]
                or journal["proof"].get("verification") == VERIFICATION):
            raise AgentError("VK_BROWSER_ACTUAL_MEDIA_READ_ONLY", 409)
        return _result(journal, fixture=fixture)
    selectors = dom_contract(options, publish=True)
    authorize()
    journal = {"contract": CONTRACT, "root": str(root), "request_id": payload["request_id"],
        "worker_code_sha256": WORKER_SOURCE_SHA256,
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
        if not _editor_ready(page, selectors):
            raise AgentError("VK_BROWSER_EDITOR_UNAVAILABLE")
        _context(page, plan, selectors, origin=origin)
        if _matches(page, plan, selectors, origin=origin):
            raise AgentError("VK_BROWSER_DUPLICATE_POST")
        authorize()
        _save(path, journal, "editor_open_intent")
        open_button = _one(page, selectors["open_editor"], "VK_BROWSER_EDITOR_OPEN_AMBIGUOUS")
        def check_open():
            if selectors.get("identity_contract") == "personal-wall-settings-v1":
                if any(node.is_visible() for node in page.locator(selectors["modal"]).all()):
                    raise AgentError("VK_BROWSER_EDITOR_ALREADY_OPEN")

        _mutate(page, plan, selectors, authorize, check_open,
                lambda timeout: open_button.click(timeout=timeout), origin=origin)
        transition_timeout = min(10000, options.get("operation_timeout_seconds", 180) * 1000)
        editor = _wait_control_ready(page, selectors["editor"], "VK_BROWSER_EDITOR_AMBIGUOUS",
                                     timeout=transition_timeout)
        _context(page, plan, selectors, origin=origin)
        if _text(editor) or _images(page, selectors["uploaded_images"]):
            raise AgentError("VK_BROWSER_EDITOR_NOT_EMPTY")
        authorize()
        _save(path, journal, "fill_intent")
        def check_fill():
            if not editor.is_visible() or _text(editor) or _images(page, selectors["uploaded_images"]):
                raise AgentError("VK_BROWSER_EDITOR_NOT_EMPTY")
            if selectors.get("identity_contract") == "personal-wall-settings-v1":
                modal = _one(page, selectors["modal"], "VK_BROWSER_MODAL_AMBIGUOUS")
                if not modal.is_visible() or modal.get_attribute("aria-label") != "Форма постинга":
                    raise AgentError("VK_BROWSER_MODAL_CONTRACT_INVALID")
                _all_preview_media(page, plan, selectors, [])

        _mutate(page, plan, selectors, authorize, check_fill,
                lambda timeout: editor.fill(plan["text"], timeout=timeout), origin=origin)
        for media in plan["media"]:
            authorize()
            raw = private_path(root, media["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != media["sha256"]:
                raise AgentError("VK_BROWSER_IMAGE_HASH_CHANGED")
            _save(path, journal, "upload_intent")
            upload_input = _one(page, selectors["upload_input"], "VK_BROWSER_UPLOAD_AMBIGUOUS")
            def check_upload(upload_input=upload_input, media=media):
                if not editor.is_visible() or _text(editor) != plan["text"] or _images(page, selectors["uploaded_images"]):
                    raise AgentError("VK_BROWSER_EDITOR_CHANGED_BEFORE_UPLOAD")
                if plan.get("image_contract"):
                    state = upload_input.evaluate("e=>({tag:e.tagName,type:e.type,multiple:e.multiple,"
                                                  "accept:e.accept,files:e.files?e.files.length:-1,disabled:e.disabled})")
                    accepts = [value.strip() for value in state["accept"].split(",")]
                    if (state["tag"] != "INPUT" or state["type"] != "file" or state["multiple"] is not True
                            or state["files"] != 0 or state["disabled"] is not False
                            or media["mime"] not in accepts and "image/*" not in accepts):
                        raise AgentError("VK_BROWSER_PHOTO_INPUT_CONTRACT_INVALID")

            if plan.get("image_contract"):
                from .vk_media import upload_filename
                filename = upload_filename(media)
            else:
                filename = "approved.jpg" if media["mime"] == "image/jpeg" else "approved.png"
            _mutate(page, plan, selectors, authorize, check_upload,
                    lambda timeout, media=media, raw=raw, upload_input=upload_input, filename=filename: upload_input.set_input_files(
                        {"name": filename,
                         "mimeType": media["mime"], "buffer": raw}, timeout=timeout), origin=origin)
            page.wait_for_function("""selector => {
              const images=[...document.querySelectorAll(selector)];
              return images.length===1 && images.every(n=>n.complete && n.naturalWidth>0
                && n.src && !n.src.startsWith('blob:') && !n.src.startsWith('data:'));
            }""", arg=selectors["uploaded_images"])
            if plan.get("image_contract"):
                from .vk_media import derivative_proof, upload_binding
                _wait_control_ready(page, selectors["uploaded_asset"], "VK_BROWSER_PHOTO_ASSET_AMBIGUOUS",
                                    timeout=transition_timeout)
                uploaded = _native_images(page, plan, selectors, "uploaded", origin=origin)
                derivative = _cdn_bytes(page, uploaded[0]["url"], origin=origin)
                derivative_proof(derivative, plan["source_image"], uploaded[0]["asset_id"], plan["image_contract"])
                if uploaded != _native_images(page, plan, selectors, "uploaded", origin=origin):
                    raise AgentError("VK_BROWSER_PHOTO_PREVIEW_CHANGED")
                binding = upload_binding(plan, journal["request_id"], uploaded[0]["asset_id"], uploaded[0]["url"],
                                         hashlib.sha256(derivative).hexdigest(), origin=origin)
                _save(path, journal, "uploaded", upload_proof=binding)
            else:
                _save(path, journal, "uploaded")
        _editor_check(page, plan, selectors, origin=origin)
        next_button = page.locator(selectors.get("next", "[data-vk-never-match]"))
        if selectors.get("identity_contract") == "personal-wall-settings-v1":
            next_button = _wait_control_ready(page, selectors["next"], "VK_BROWSER_NEXT_AMBIGUOUS",
                                             timeout=transition_timeout)
        if next_button.count():
            if next_button.count() != 1 or not next_button.is_visible() or not next_button.is_enabled():
                raise AgentError("VK_BROWSER_NEXT_AMBIGUOUS")
            authorize()
            _save(path, journal, "next_intent")
            _mutate(page, plan, selectors, authorize, lambda: _editor_check(page, plan, selectors, origin=origin),
                    lambda timeout: next_button.click(timeout=timeout), origin=origin)
        page.locator(selectors["preview_text"]).wait_for(state="visible", timeout=10000)
        if selectors.get("identity_contract") == "personal-wall-settings-v1":
            settings = _personal_settings(page, plan, selectors, origin=origin, require_story_disabled=False)
            if settings["story_checked"]:
                authorize()
                _save(path, journal, "story_disable_intent")
                story_switch = _one(page, selectors["story_switch"], "VK_BROWSER_STORY_SWITCH_AMBIGUOUS")
                story_label = (_one(page, selectors["story_toggle_label"], "VK_BROWSER_STORY_LABEL_AMBIGUOUS")
                               if "story_toggle_label" in selectors else None)
                _mutate(page, plan, selectors, authorize,
                        lambda: _personal_settings(page, plan, selectors, origin=origin, require_story_disabled=False),
                        lambda timeout: (story_label.click(timeout=timeout, no_wait_after=True) if story_label is not None
                                         else story_switch.uncheck(timeout=timeout)), origin=origin)
                _wait_story_disabled(page, selectors, timeout=transition_timeout)
            _personal_settings(page, plan, selectors, origin=origin)
        before = _final_snapshot(page, plan, selectors, origin=origin)
        if plan.get("image_contract"):
            _native_preview_proof(page, before["images"], plan, journal.get("upload_proof"), journal["request_id"], origin=origin)
        else:
            _image_proof(page, before["images"], plan["media"], origin=origin)
        if before != _final_snapshot(page, plan, selectors, origin=origin):
            raise AgentError("VK_BROWSER_PREVIEW_CHANGED_DURING_IMAGE_READ")
        authorize()
        _save(path, journal, "click_intent", clicks=1, personal_settings=before.get("personal_settings"))
        publish_button = _one(page, selectors["publish"], "VK_BROWSER_PUBLISH_AMBIGUOUS")
        def check_final():
            if before != _final_snapshot(page, plan, selectors, origin=origin):
                raise AgentError("VK_BROWSER_PREVIEW_CHANGED_BEFORE_CLICK")

        _mutate(page, plan, selectors, authorize, check_final,
                lambda timeout: publish_button.click(timeout=timeout, no_wait_after=True), origin=origin)
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
        proof = _readback(page, plan, selectors, external_id, origin=origin,
                          timeout=min(10000, options.get("operation_timeout_seconds", 180) * 1000),
                          **({"upload": journal.get("upload_proof"), "request_id": journal["request_id"]}
                             if plan.get("image_contract") else {}))
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


def _discover_native_post(page, plan, selectors, upload, request_id, *, origin, timeout=10000):
    """Locate one previously claimed asset; exact text/author proof is still mandatory."""
    from .vk_media import native_asset_id, validate_upload
    if not validate_upload(upload, plan, request_id, origin=origin):
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
    _ready(page, selectors)
    deadline = time.monotonic() + min(10000, timeout) / 1000
    while True:
        _context(page, plan, selectors, origin=origin)
        posts = page.locator(selectors["posts"])
        if posts.count() > 200:
            raise AgentError("VK_BROWSER_POST_LIST_UNBOUNDED")
        candidates = []
        for post in posts.all():
            for asset in post.locator(selectors["post_asset"]).all():
                try:
                    ident = native_asset_id(asset.get_attribute(selectors["post_asset_id_attribute"]),
                                            plan["owner"], selectors["post_asset_id_attribute"], origin=origin)
                except AgentError:
                    continue
                if ident == upload["asset_id"]:
                    candidates.append(post)
        if len(candidates) == 1:
            break
        remaining = deadline - time.monotonic()
        if len(candidates) > 1 or remaining <= 0:
            raise AgentError("VK_BROWSER_READBACK_NOT_EXACTLY_ONE")
        page.wait_for_timeout(min(100, max(1, int(remaining * 1000))))
    expected_id = _post_id(candidates[0], plan, selectors, origin=origin)
    snapshot = _post_snapshot(page, plan, selectors, expected_id, origin=origin)
    if len(snapshot["images"]) != 1 or snapshot["images"][0].get("asset_id") != upload["asset_id"]:
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    return expected_id


def reconcile(page, root, options, destination, request_id, *, origin=ORIGIN, fixture=False, media_review_loader=None):
    if origin != ORIGIN and not fixture:
        raise AgentError("VK_BROWSER_FIXTURE_ORIGIN_FORBIDDEN")
    if media_review_loader is not None and not fixture:
        raise AgentError("VK_BROWSER_FIXTURE_AUTHORITY_FORBIDDEN")
    path, journal = _read(root, request_id)
    if (journal.get("destination") != destination or journal.get("options_sha256") != fingerprint(options)
            or journal.get("fixture") is not fixture):
        raise AgentError("VK_BROWSER_JOURNAL_CONFLICT", 409)
    discovery = not journal.get("external_id")
    if discovery and options.get("dom_contract_profile") != "personal-image-v1":
        return _result(journal, fixture=fixture)
    # A completed journal proves the earlier delivery. Reconciliation of an
    # unknown Hub dispatch must independently read today's visible native post;
    # it must never promote cached evidence to a current readback.
    plan = prepare(root, options, destination, journal["snapshot"])
    save_failure = True
    guarded_journal = discovery or bool(plan.get("image_contract"))
    try:
        selectors = dom_contract(options, publish=True)
        def current_review():
            if not plan.get("image_contract"):
                return None
            if fixture:
                return media_review_loader() if media_review_loader else None
            from .vk_media_review import load_review
            return load_review(root, options, destination, request_id, plan, journal)
        media_review = current_review()
        expected_id = journal.get("external_id")
        if discovery:
            from .vk_media import receipt_origin, validate_upload
            if (not plan.get("image_contract") or type(journal.get("clicks")) is not int or journal["clicks"] != 1
                    or journal.get("dom_contract_sha256") != plan["dom_contract_sha256"]
                    or receipt_origin(journal.get("upload_proof"), fixture) != origin
                    or not validate_upload(journal.get("upload_proof"), plan, request_id, origin=origin)):
                raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
            expected_id = _discover_native_post(page, plan, selectors, journal["upload_proof"], request_id, origin=origin,
                timeout=min(10000, options.get("operation_timeout_seconds", 180) * 1000))
        if media_review and expected_id != media_review["details"]["external_id"]:
            raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_INVALID")
        proof = _readback(page, plan, selectors, expected_id, origin=origin,
                          timeout=min(10000, options.get("operation_timeout_seconds", 180) * 1000),
                          **({"upload": journal.get("upload_proof"), "request_id": journal["request_id"]}
                              if plan.get("image_contract") else {}),
                          **({"media_review": media_review} if media_review else {}))
        if media_review and current_review() != media_review:
            raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_CHANGED")
        if guarded_journal:
            _, current = _read(root, request_id)
            if (current["journal_sha256"] != journal["journal_sha256"]
                    or prepare(root, options, destination, journal["snapshot"]) != plan):
                save_failure = False
                raise AgentError("VK_BROWSER_JOURNAL_CONFLICT", 409)
        _save(path, journal, "confirmed", proof=proof, failure_code=None,
              **({"external_id": expected_id, "permalink": ORIGIN + "/wall" + expected_id} if discovery else {}))
    except Exception as exc:
        failure_code = exc.code if isinstance(exc, AgentError) else "VK_BROWSER_OPERATION_UNKNOWN"
        if guarded_journal and save_failure:
            try:
                _, current = _read(root, request_id)
                save_failure = current["journal_sha256"] == journal["journal_sha256"]
            except AgentError:
                save_failure = False
            if not save_failure:
                failure_code = "VK_BROWSER_JOURNAL_CONFLICT"
        if save_failure:
            _save(path, journal, "unknown", failure_code=failure_code)
        else:
            journal.update(state="unknown", failure_code=failure_code)
    return _result(journal, fixture=fixture)


def inspect_page(page, options, destination, *, origin=ORIGIN, fixture=False):
    if origin != ORIGIN and not fixture:
        raise AgentError("VK_BROWSER_FIXTURE_ORIGIN_FORBIDDEN")
    plan = {"destination": destination, "wall_url": wall_url(destination, options), "owner": owner_id(options)}
    selectors = dom_contract(options)
    _navigate(page, plan["wall_url"].replace(ORIGIN, origin, 1))
    _ready(page, selectors)
    _context(page, plan, selectors, origin=origin)
    editor_ready = _editor_ready(page, selectors)
    _context(page, plan, selectors, origin=origin)
    create = page.locator(selectors["open_editor"])
    try:
        dom_contract(options, publish=True)
        contract_valid = selectors.get("identity_contract") != "personal-wall-settings-v1" or destination == owner_id(options)
    except AgentError:
        contract_valid = False
    blockers = ([] if editor_ready else ["VK_BROWSER_EDITOR_UNAVAILABLE"])
    if not contract_valid:
        blockers.append("VK_BROWSER_DOM_CONTRACT_UNVERIFIED")
    return {"state": "access_only", "publish": contract_valid and editor_ready,
            "owner": plan["owner"], "destination": destination,
            "live_verified": False, "external_mutations": 0, "fixture": fixture,
            "create_button_count": create.count(), "editor_ready": editor_ready,
            "contract_verified": contract_valid, "blockers": blockers}
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


def _authority_core(root):
    from .config import Settings
    from .db import Database
    from .hub import HubService

    settings = Settings.load(Path(root))
    db = Database(settings)
    try:
        return HubService(settings, db, bootstrap_targets=False)
    except Exception:
        db.engine.dispose()
        raise


def authorize(root, payload, options, *, expected_lease_owner=None, authority_core=None):
    from .db import now
    from .hub_models import Dispatch
    from .server_state import require_running

    core = authority_core or _authority_core(root)
    db = core.db
    try:
        with db.session() as transaction:
            row = transaction.get(Dispatch, payload.get("request_id"))
            if (row is None or row.state != "sending" or not row.owner
                    or expected_lease_owner is not None and row.owner != expected_lease_owner
                    or not math.isfinite(row.lease_until) or row.lease_until <= now() + 3
                    or row.due_at > now()):
                raise AgentError("VK_BROWSER_DISPATCH_NOT_APPROVED", 403)
            original_owner, original_snapshot_hash = row.owner, row.snapshot_hash
            require_running(core, transaction)
            variant, target = core._valid_dispatch(transaction, row)
            if (target.platform != "vk" or target.route != "vk_browser"
                    or target.destination != payload.get("destination")
                    or fingerprint(target.options) != fingerprint(options)
                    or fingerprint(row.snapshot) != fingerprint(payload.get("snapshot"))
                    or fingerprint(core.snapshot(variant, target)) != fingerprint(row.snapshot)):
                raise AgentError("VK_BROWSER_DISPATCH_NOT_APPROVED", 403)
        return core.authorize_dispatch_fence(payload.get("request_id"), expected_owner=original_owner,
                                              expected_snapshot_hash=original_snapshot_hash)
    finally:
        if authority_core is None:
            core.close()
            db.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Отдельный пользовательский браузер VK")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--login")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    authority = None
    try:
        from .cli import load_env
        load_env(root)
        if args.login:
            options = target_options(root, args.login)
            with session(root, options, visible=True) as page:
                _navigate(page, wall_url(args.login, options))
                deadline = time.monotonic() + 900
                restored_wall = False
                while time.monotonic() < deadline:
                    try:
                        plan = {"wall_url": wall_url(args.login, options), "owner": owner_id(options)}
                        _context(page, plan, dom_contract(options), origin=ORIGIN)
                        print(json.dumps({"state": "authenticated", "live_verified": False}))
                        return 0
                    except AgentError as exc:
                        if exc.code not in {"VK_BROWSER_ACCOUNT_AMBIGUOUS", "VK_BROWSER_ACCOUNT_MISMATCH",
                                            "VK_BROWSER_WRONG_ORIGIN", "VK_BROWSER_WALL_MISMATCH",
                                            "VK_BROWSER_LINK_INVALID"}:
                            raise
                        if not restored_wall and exc.code == "VK_BROWSER_WALL_MISMATCH":
                            restored_wall = _restore_login_wall(page, plan, dom_contract(options), origin=ORIGIN)
                        page.wait_for_timeout(500)
                raise AgentError("VK_BROWSER_LOGIN_TIMEOUT")
        payload = parse_json(sys.stdin.read(4 * 1024 * 1024))
        if not isinstance(payload, dict) or payload.get("action") not in {"inspect", "publish", "reconcile"}:
            raise AgentError("VK_BROWSER_REQUEST_INVALID")
        options = target_options(root, payload.get("destination"))
        if payload.get("options_sha256") != fingerprint(options):
            raise AgentError("VK_BROWSER_CONFIG_CHANGED")
        if payload["action"] == "publish":
            authority = _authority_core(root)
            if payload.get("runtime_generation") != authority.route_runtime_generation():
                raise AgentError("VK_BROWSER_RUNTIME_GENERATION_CHANGED", 409)
            lease_owner = authorize(root, payload, options, authority_core=authority)
        else:
            lease_owner = None
        with session(root, options) as page:
            if payload["action"] == "inspect":
                result = inspect_page(page, options, payload["destination"])
            elif payload["action"] == "reconcile":
                result = reconcile(page, root, options, payload["destination"], payload["request_id"])
            else:
                result = run_publish(page, root, options, payload,
                    lambda: authorize(root, payload, options, expected_lease_owner=lease_owner,
                                      authority_core=authority))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({"code": exc.code if isinstance(exc, AgentError) else "VK_BROWSER_OPERATION_UNKNOWN"}))
        return 1
    finally:
        if authority is not None:
            authority.close()
            authority.db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
