"""One Pikabu operation, explicit public DOM, no private API or click retries."""

import argparse
import base64
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
from .config import AgentError, fingerprint, inside, parse_json
from .pikabu_delivery import PikabuDelivery, check_body_links, owner_name, parse_public_story
from .pikabu_metadata import story_url


ORIGIN = "https://pikabu.ru"
CONTRACT = "pikabu-browser-single-click-v2"
_REQUEST = re.compile(r"[A-Za-z0-9_-]{1,128}")
DEFAULT_DOM = {
    "account": '.header-right-menu > a.header-right-menu__item.avatar[href*="/@"]',
    "title": 'textarea[placeholder="Заголовок"]',
    "body": '[class*="story-editor-view__content--"] > div.tiptap.ProseMirror[contenteditable="true"][role="textbox"]',
    "tags_input": '[role="combobox"][class*="ComboBox__host_multiple--"] input',
    "tag_values": '[role="combobox"][class*="ComboBox__host_multiple--"] [class*="ComboBoxContent__label--"]',
    "uploaded_images": '.tiptap [class*="node-image-view__figure--"] img',
    "community_input": 'input[placeholder="Введите название сообщества"]',
    "publish": '[data-test="story-publish"]',
    "captcha": 'iframe[src*="captcha"],.g-recaptcha,.cf-challenge-running,#ddg-challenge',
    "errors": '[role="alert"]',
}
_REQUIRED = {"account", "title", "body", "tags_input", "tag_values", "publish", "uploaded_images",
             "community_input"}


def _dom(options, *, publish=False):
    extra = options.get("dom_contract", {})
    if not isinstance(extra, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value or len(value) > 500
            for key, value in extra.items()):
        raise AgentError("PIKABU_DOM_CONTRACT_INVALID")
    result = {**DEFAULT_DOM, **extra}
    if publish and (options.get("dom_contract_verified") is not True or not _REQUIRED <= result.keys()):
        raise AgentError("PIKABU_DOM_CONTRACT_UNVERIFIED")
    return result


def _media_contract(options, plan):
    if plan["media"] and options.get("media_contract_verified") is not True:
        raise AgentError("PIKABU_MEDIA_CONTRACT_UNVERIFIED")


def _body_contract(plan):
    if plan["body"].endswith("\n"):
        raise AgentError("PIKABU_TRAILING_LINEBREAK_UNSUPPORTED")


def _atomic(path, value):
    value["journal_sha256"] = fingerprint({key: item for key, item in value.items()
                                           if key != "journal_sha256"})
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _journal_path(root, request_id):
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise AgentError("PIKABU_REQUEST_ID_INVALID")
    return inside(root, "data/pikabu-operations/" + request_id + ".json")


def _execution(origin, fixture):
    """The CLI always uses production; test execution is confined to loopback."""
    if not isinstance(fixture, bool) or not isinstance(origin, str):
        raise AgentError("PIKABU_EXECUTION_CONTEXT_INVALID")
    parsed = urlsplit(origin)
    if fixture:
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise AgentError("PIKABU_EXECUTION_CONTEXT_INVALID")
    elif origin != ORIGIN:
        raise AgentError("PIKABU_EXECUTION_CONTEXT_INVALID")
    return {"execution_origin": origin, "fixture": fixture}


def _journal_execution(journal, origin, fixture):
    expected = _execution(origin, fixture)
    if any(journal.get(key) != value for key, value in expected.items()):
        raise AgentError("PIKABU_JOURNAL_CONFLICT", 409)


def _read_journal(root, request_id):
    path = _journal_path(root, request_id)
    try:
        value = parse_json(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("contract") != CONTRACT
                or value.get("request_id") != request_id
                or value.get("root") != str(Path(root).resolve())
                or value.get("journal_sha256") != fingerprint(
                    {key: item for key, item in value.items() if key != "journal_sha256"})
                or value.get("snapshot_sha256") != fingerprint(value.get("snapshot"))):
            raise ValueError
        _execution(value.get("execution_origin"), value.get("fixture"))
        return path, value
    except (OSError, ValueError, TypeError, AgentError) as exc:
        raise AgentError("PIKABU_JOURNAL_INVALID") from exc


def _save(path, journal, state, **values):
    journal.update(state=state, updated_at=time.time(), **values)
    _atomic(path, journal)


def _blocked(page, selectors):
    for selector in (selectors.get("captcha"),):
        if selector and any(node.is_visible() for node in page.locator(selector).all()):
            raise AgentError("PIKABU_CAPTCHA_OR_ACCESS_BLOCKED", 403)
    text = page.title().casefold()
    if any(value in text for value in ("captcha", "just a moment", "доступ ограничен")):
        raise AgentError("PIKABU_CAPTCHA_OR_ACCESS_BLOCKED", 403)


def _navigate(page, url):
    response = page.goto(url, wait_until="domcontentloaded")
    if response is not None and response.status in {401, 403, 429}:
        raise AgentError("PIKABU_CAPTCHA_OR_ACCESS_BLOCKED", 403)
    if response is not None and response.status >= 400:
        raise AgentError("PIKABU_PAGE_UNAVAILABLE", 503)


def _one(page, selector, code):
    node = page.locator(selector)
    if node.count() != 1:
        raise AgentError(code)
    return node


def _ready_editor(page, selectors, options):
    """Readonly hydration wait; a blank disabled Publish button is expected."""
    timeout = max(1, min(20000, int(options.get("operation_timeout_seconds", 180)) * 1000))
    for key in ("title", "body", "tags_input", "community_input", "publish"):
        try:
            page.locator(selectors[key]).wait_for(state="visible", timeout=timeout)
        except Exception as exc:
            raise AgentError("PIKABU_EDITOR_NOT_READY") from exc


def _account_href(href, expected, origin):
    parsed = urlsplit(href)
    if ((parsed.netloc and parsed.netloc != urlsplit(origin).netloc)
            or parsed.scheme not in {"", urlsplit(origin).scheme}
            or parsed.path != "/@" + expected or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise AgentError("PIKABU_ACCOUNT_MISMATCH", 403)


def _account(page, options, selectors, *, origin=ORIGIN):
    _blocked(page, selectors)
    expected = owner_name(options.get("owner"))
    if (urlsplit(page.url).netloc != urlsplit(origin).netloc
            or urlsplit(page.url).scheme != urlsplit(origin).scheme):
        raise AgentError("PIKABU_WRONG_ORIGIN")
    node = _one(page, selectors["account"], "PIKABU_LOGIN_REQUIRED")
    # The authenticated header uses an avatar-only anchor; its visible text is
    # intentionally empty. The exact visible profile URL is the account binding.
    if not node.is_visible():
        raise AgentError("PIKABU_ACCOUNT_MISMATCH", 403)
    _account_href(node.get_attribute("href") or "", expected, origin)


def probe_page(page, options, *, origin=ORIGIN, navigate=True):
    """DOM shape only: no values, body text, account storage or network mutation."""
    if navigate:
        _navigate(page, origin + "/add")
    selectors = _dom(options)
    _blocked(page, selectors)
    if (urlsplit(page.url).netloc != urlsplit(origin).netloc
            or urlsplit(page.url).scheme != urlsplit(origin).scheme):
        raise AgentError("PIKABU_WRONG_ORIGIN")
    report = page.evaluate("""owner => {
      const shape = n => ({tag:n.tagName.toLowerCase(),id:n.id||'',
        classes:[...n.classList].slice(0,8),role:n.getAttribute('role')||'',
        placeholder:n.getAttribute('placeholder')||'',
        data_test:n.getAttribute('data-test')||'',
        editable:n.getAttribute('contenteditable')||'',
        visible:!!(n.getBoundingClientRect().width&&n.getBoundingClientRect().height)});
      const ancestors = n => {const rows=[];for(let p=n.parentElement;p&&rows.length<4;p=p.parentElement)
        rows.push(shape(p));return rows;};
      const owners=[...document.querySelectorAll('a[href]')].filter(n=>{
        try{return new URL(n.href,location.href).pathname==='/@'+owner;}catch(e){return false;}
      }).slice(0,12).map(n=>({...shape(n),text_equals_owner:n.innerText.trim()===owner,
        ancestors:ancestors(n)}));
      return {owner_link_candidates:owners,
        controls:[...document.querySelectorAll('textarea,input[type=text],[contenteditable=true],'
          +'[data-test],button')].slice(0,100).map(n=>({...shape(n),ancestors:ancestors(n)}))};
    }""", owner_name(options.get("owner")))
    return {"state": "read_only_dom_probe", "live_verified": False, "mutations": 0,
            "owner": options["owner"], "dom_contract_sha256": fingerprint(selectors), **report}


def inspect_page(page, options, *, origin=ORIGIN):
    selectors = _dom(options)
    _navigate(page, origin + "/add")
    try:
        _account(page, options, selectors, origin=origin)
    except AgentError as exc:
        if exc.code not in {"PIKABU_LOGIN_REQUIRED", "PIKABU_ACCOUNT_MISMATCH"}:
            raise
        return {"state": "account_contract_required", "publish": False, "live_verified": False,
                "error": exc.code, "probe": probe_page(page, options, origin=origin, navigate=False)}
    _ready_editor(page, selectors, options)
    counts = {key: page.locator(selector).count() for key, selector in selectors.items()
              if key in _REQUIRED}
    ready = (_REQUIRED <= selectors.keys()
             and all(counts.get(key) == 1 for key in {
                 "account", "title", "body", "tags_input", "publish", "community_input"}))
    community_empty = False
    if ready:
        community = _community_state(page, selectors)
        community_empty = not community["value"] and not community["label"]
        ready = community_empty
    return {"state": "available" if ready else "dom_contract_required",
            "publish": ready and options.get("dom_contract_verified") is True,
            "owner": options["owner"], "live_verified": False,
            "dom_contract_sha256": fingerprint(selectors), "selector_counts": counts,
            "community_empty": community_empty,
            "media_contract_verified": options.get("media_contract_verified") is True,
            "contract_verified": options.get("dom_contract_verified") is True}


def _editor_content(editor, image_selector):
    """One DOM tree: admit observed paragraph wrappers, never flatten arbitrary DIVs."""
    return editor.evaluate("""(root, selector) => {
      let unsupported=false;
      const selected=[...document.querySelectorAll(selector)];
      const images=[...root.querySelectorAll('img')],paragraphs=[],mappedImages=[],chromeNodes=new Set();
      const wrappedParagraphs=new Map();
      const prefix=(n,value)=>n.nodeType===Node.ELEMENT_NODE && [...n.classList].some(c=>c.startsWith(value));
      const inert=n=>n.nodeType===Node.COMMENT_NODE || (n.nodeType===Node.TEXT_NODE && n.textContent==='');
      const placeholder=n=>n.nodeType===Node.ELEMENT_NODE && n.tagName==='DIV'
        && !n.attributes.length && [...n.childNodes].every(inert);
      const oneChild=n=>{const parts=[...n.childNodes].filter(c=>!inert(c));return parts.length===1?parts[0]:null;};
      const chrome=n=>{
        if(n.nodeType!==Node.ELEMENT_NODE || n.tagName!=='DIV' || n.attributes.length || n.textContent!=='')return false;
        const drag=oneChild(n),button=drag && oneChild(drag),svg=button && oneChild(button),use=svg && oneChild(svg);
        if(!drag || drag.tagName!=='DIV' || !prefix(drag,'node-wrapper-view__dragHandleWrapper--')
            || drag.getAttribute('contenteditable')!=='false' || !button || button.tagName!=='DIV'
            || !prefix(button,'node-wrapper-view__button--') || !svg || svg.localName!=='svg'
            || !prefix(svg,'Icon__host--') || !use || use.localName!=='use'
            || (use.getAttribute('href')||use.getAttribute('xlink:href'))!=='#icon--ui-kit__drag_s'
            || [...use.childNodes].some(c=>!inert(c)))return false;
        const nodes=[n,drag,button,svg,use];
        if(nodes.some(node=>[...node.attributes].some(a=>a.name.toLowerCase().startsWith('on'))))return false;
        nodes.forEach(node=>chromeNodes.add(node));return true;
      };
      const visible=n=>{
        const box=n.getBoundingClientRect();
        if(box.height<=0 || (n.tagName!=='BR' && box.width<=0))return false;
        for(let current=n;current;current=current.parentElement){
          const style=getComputedStyle(current);
          if(style.display==='none' || style.visibility!=='visible' || Number(style.opacity)===0)return false;
        }return true;
      };
      const flow=(n,display,flex=null)=>{
        const s=getComputedStyle(n);
        return s.display===display && (!flex || s.flexDirection===flex)
          && ['static','relative'].includes(s.position) && s.order==='0' && s.transform==='none'
          && s.writingMode==='horizontal-tb' && s.float==='none'
          && s.overflowX==='visible' && s.overflowY==='visible'
          && ['top','right','bottom','left'].every(key=>['auto','0px'].includes(s[key]));
      };
      if(!flow(root,'block') || !visible(root))unsupported=true;
      const inline=new Set(['A','SPAN','STRONG','B','EM','I','U','S','CODE']);
      const paragraph=p=>{
        if(!flow(p,'block') || !visible(p))unsupported=true;
        for(const n of p.querySelectorAll('*')){
          if(n.tagName==='BR'){
            if(n.classList.contains('ProseMirror-trailingBreak') && (n.parentNode!==p
              || n.classList.length!==1 || n.attributes.length!==1
              || p.querySelectorAll('br.ProseMirror-trailingBreak').length!==1
              || [...p.childNodes].slice([...p.childNodes].indexOf(n)+1).some(c=>!inert(c))))unsupported=true;
          }else if(!inline.has(n.tagName) || !['inline','contents'].includes(getComputedStyle(n).display)){
            unsupported=true;
          }
        }
        paragraphs.push(p);
      };
      for(const node of root.childNodes){
        if(inert(node))continue;
        if(node.nodeType!==Node.ELEMENT_NODE){unsupported=true;continue;}
        if(node.tagName==='P'){paragraph(node);continue;}
        if(node.tagName==='DIV' && prefix(node,'node-wrapper-view__host--')
            && node.hasAttribute('data-node-view-wrapper')){
          const content=[...node.children].filter(n=>n.tagName==='DIV' && prefix(n,'node-wrapper-view__content--'));
          const other=[...node.childNodes].filter(n=>!content.includes(n));
          if(!flow(node,'flex','column') || content.length!==1 || !flow(content[0],'block')
              || other.some(n=>!inert(n) && !placeholder(n) && !chrome(n))
              || other.filter(n=>placeholder(n)||chrome(n)).length>1){unsupported=true;continue;}
          const parts=[...content[0].childNodes].filter(n=>!inert(n));
          if(parts.length!==1 || parts[0].tagName!=='P'
              || !prefix(parts[0],'node-paragraph-view__content--')){unsupported=true;continue;}
          wrappedParagraphs.set(parts[0],node);paragraph(parts[0]);continue;
        }
        // The existing local media fixture uses a simple image figure. Real
        // image-node wrappers remain behind their separate unverified media gate.
        if(node.tagName==='FIGURE' && node.querySelectorAll('img').length>0 && flow(node,'block')
            && [...node.childNodes].every(n=>inert(n)
            || (n.nodeType===Node.ELEMENT_NODE && n.tagName==='IMG' && selected.includes(n)))){
          mappedImages.push(...node.querySelectorAll('img'));continue;
        }
        unsupported=true;
      }
      if(images.length!==selected.length || selected.some(n=>!root.contains(n) || !mappedImages.includes(n))
          || [...root.querySelectorAll('script,style,iframe,video,audio,object,embed,canvas,svg,table,form,textarea')]
            .some(n=>!chromeNodes.has(n))){
        unsupported=true;
      }
      const text=(n,rendered=false)=>{
        if(n.nodeType===Node.TEXT_NODE)return n.textContent;
        if(n.nodeType!==Node.ELEMENT_NODE || (rendered && !visible(n)))return '';
        if(n.tagName==='BR')return n.classList.contains('ProseMirror-trailingBreak')?'':'\\n';
        return [...n.childNodes].map(c=>text(c,rendered)).join('');
      };
      if(paragraphs.some((p,index)=>index>0
          && p.getBoundingClientRect().top<paragraphs[index-1].getBoundingClientRect().bottom-0.5))unsupported=true;
      const last=paragraphs.at(-1),lastHost=wrappedParagraphs.get(last);
      const tailParts=last ? [...last.childNodes].filter(n=>!inert(n)) : [];
      const trailingAffordance=Boolean(paragraphs.length>1 && lastHost && text(last)===''
        && paragraphs.slice(0,-1).some(p=>text(p)!=='') && tailParts.length===1
        && tailParts[0].tagName==='BR' && tailParts[0].classList.length===1
        && tailParts[0].classList.contains('ProseMirror-trailingBreak')
        && [...root.childNodes].filter(n=>!inert(n)).at(-1)===lastHost
        && [...lastHost.childNodes].every(n=>inert(n) || placeholder(n)
          || (n.nodeType===Node.ELEMENT_NODE && prefix(n,'node-wrapper-view__content--'))));
      if(trailingAffordance)paragraphs.pop();
      const firstText=paragraphs.find(p=>text(p)!=='');
      const imageBeforeText=!images.length || Boolean(firstText && images.every(image=>
        Boolean(image.compareDocumentPosition(firstText)&Node.DOCUMENT_POSITION_FOLLOWING)));
      return {body:paragraphs.map(p=>text(p)).join('\\n\\n'),
        links:[...root.querySelectorAll('a[href]')].map(n=>n.getAttribute('href')),
        structure:{unsupported,images:images.length,selected_images:selected.length,
          rendered_body:paragraphs.map(p=>text(p,true)).join('\\n\\n'),images_before_text:imageBeforeText,
          trailing_editor_affordance:trailingAffordance},
        images:selected.map(n=>({src:n.currentSrc||n.src||'',complete:n.complete===true,
          visible:visible(n),
          original:n.getAttribute('data-large-image')||n.getAttribute('data-original')||n.currentSrc||n.src||'',
          width:n.naturalWidth||0,height:n.naturalHeight||0}))};
    }""", image_selector)


def _community_state(page, selectors):
    field = _one(page, selectors["community_input"], "PIKABU_COMMUNITY_CONTROL_UNVERIFIED")
    value = field.input_value()
    label = field.evaluate("""node => {
      const control=node.closest('[role="combobox"]');
      return control ? control.innerText.trim() : null;
    }""")
    if label is None:
        raise AgentError("PIKABU_COMMUNITY_CONTROL_UNVERIFIED")
    return {"value": value, "label": label}


def _editor_snapshot(page, selectors):
    title = _one(page, selectors["title"], "PIKABU_EDITOR_TITLE_AMBIGUOUS").input_value()
    editor = _one(page, selectors["body"], "PIKABU_EDITOR_BODY_AMBIGUOUS")
    content = _editor_content(editor, selectors["uploaded_images"])
    tags = page.locator(selectors["tag_values"]).all_text_contents()
    community = page.locator(selectors["community"]).count() if selectors.get("community") else 0
    selected = _community_state(page, selectors)
    return {"title": title, "body": content["body"], "tags": tags, "images": content["images"],
            "links": content["links"],
            "structure": content["structure"],
            "community": community,
            "community_value": selected["value"], "community_label": selected["label"]}


def _check_editor(page, plan, options, selectors, *, origin=ORIGIN):
    _editor_account(page, options, selectors, origin=origin)
    actual = _editor_snapshot(page, selectors)
    if actual["structure"]["unsupported"]:
        raise AgentError("PIKABU_EDITOR_UNSUPPORTED_CONTENT")
    check_body_links(actual["links"], plan["body"], base_url=origin + "/add")
    if (actual["title"] != plan["title"] or actual["body"] != plan["body"]
            or actual["structure"]["rendered_body"] != plan["body"]
            or actual["tags"] != plan["tags"] or len(actual["images"]) != len(plan["media"])
            or actual["community"] or actual["community_value"] or actual["community_label"]):
        raise AgentError("PIKABU_EDITOR_SNAPSHOT_MISMATCH")
    if not actual["structure"]["images_before_text"]:
        raise AgentError("PIKABU_IMAGE_POSITION_MISMATCH")
    for image in actual["images"]:
        for value in (image["src"], image["original"]):
            parsed = urlsplit(value)
            permitted = parsed.scheme == "https" and re.fullmatch(r"cs\d*\.pikabu\.ru", parsed.netloc)
            if origin != ORIGIN:
                permitted = value.startswith(origin + "/")
            if (not permitted or not image["complete"] or not image["visible"]
                    or min(image["width"], image["height"]) <= 0):
                raise AgentError("PIKABU_IMAGE_UPLOAD_UNCONFIRMED")
    _image_readback(page, [image["original"] for image in actual["images"]], plan["media"])
    # A network read does not freeze the live editor. Close that gap with a fresh
    # no-network capture; a changed title/body/tag/image/community cannot be sent
    # using the pre-read snapshot even when the database approval is still valid.
    _final_editor(page, actual, options, selectors, origin=origin,
                  changed_code="PIKABU_EDITOR_CHANGED_DURING_READBACK")
    return actual


def _final_editor(page, expected, options, selectors, *, origin, changed_code):
    """No network waits: recapture content, account, errors, modes and button."""
    _editor_account(page, options, selectors, origin=origin)
    if _editor_snapshot(page, selectors) != expected:
        raise AgentError(changed_code)
    if "errors" in selectors and any(n.is_visible() and n.inner_text().strip()
                                     for n in page.locator(selectors["errors"]).all()):
        raise AgentError("PIKABU_EDITOR_REJECTED")
    button = _one(page, selectors["publish"], "PIKABU_PUBLISH_BUTTON_AMBIGUOUS")
    if not button.is_visible() or not button.is_enabled():
        raise AgentError("PIKABU_PUBLISH_BUTTON_DISABLED")
    if button.inner_text().strip() != "Опубликовать пост":
        raise AgentError("PIKABU_PUBLISH_MODE_UNEXPECTED")
    # Never submit a stale scheduled/anonymous/adult/editor mode.
    for key in ("setting-scheduled", "setting-anonymous", "setting-nsfw", "setting-my",
                "setting-authors-content"):
        control = page.locator('[data-test="' + key + '"]')
        if control.count() and control.evaluate_all("""nodes=>nodes.some(n=>n.checked
          || n.getAttribute('aria-checked')==='true'
          || n.querySelector('input:checked,[aria-checked=true]'))"""):
            raise AgentError("PIKABU_PUBLISH_MODE_UNEXPECTED")
    _editor_account(page, options, selectors, origin=origin)
    return button


def _fill_editor(page, plan, options, selectors, journal_path, journal, authorize, *, origin):
    _editor_account(page, options, selectors, origin=origin)
    _ready_editor(page, selectors, options)
    initial = _editor_snapshot(page, selectors)
    if initial["structure"]["unsupported"]:
        raise AgentError("PIKABU_EDITOR_UNSUPPORTED_CONTENT")
    if (initial["title"] or initial["body"] or initial["tags"] or initial["images"]
            or initial["community"] or initial["community_value"] or initial["community_label"]):
        raise AgentError("PIKABU_EDITOR_NOT_EMPTY", 409)
    authorize()
    _save(journal_path, journal, "editor_mutation_intent")
    authorize()
    _editor_account(page, options, selectors, origin=origin)
    if _editor_snapshot(page, selectors) != initial:
        raise AgentError("PIKABU_EDITOR_CHANGED_BEFORE_EDIT")
    _one(page, selectors["title"], "PIKABU_EDITOR_TITLE_AMBIGUOUS").fill(plan["title"])
    editor = _one(page, selectors["body"], "PIKABU_EDITOR_BODY_AMBIGUOUS")
    # Plain HTML clipboard input uses the public paste handler and preserves exact
    # paragraph breaks without calling framework/private application APIs.
    # Focus the public contenteditable directly. A click at a wrapper's centre
    # can target node controls; the exact collapsed selection is checked before keys.
    editor.focus()
    for image_index, item in enumerate(plan["media"]):
        authorize()
        raw = inside(Path(journal["root"]), item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise AgentError("PIKABU_IMAGE_HASH_CHANGED")
        upload_before = _editor_snapshot(page, selectors)
        _save(journal_path, journal, "image_upload_intent", upload_sha256=item["sha256"])
        authorize()
        _editor_account(page, options, selectors, origin=origin)
        if _editor_snapshot(page, selectors) != upload_before:
            raise AgentError("PIKABU_EDITOR_CHANGED_BEFORE_UPLOAD")
        editor.evaluate("""(e, file) => {
          const bytes=Uint8Array.from(atob(file.bytes),c=>c.charCodeAt(0));
          const data=new DataTransfer();data.items.add(new File([bytes],file.name,{type:file.mime}));
          e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
        }""", {"name": item["name"], "mime": item["mime"],
               "bytes": base64.b64encode(raw).decode("ascii")})
        page.wait_for_function("""arg => {
          const images=[...document.querySelectorAll(arg.selector)];
          return images.length===arg.count && images.every(n=>n.complete && n.naturalWidth>0
            && n.src && !n.src.startsWith('blob:') && !n.src.startsWith('data:'));
        }""", arg={"selector": selectors["uploaded_images"],
                   "count": image_index + 1})
        _save(journal_path, journal, "image_uploaded")
    # The image precedes the text. The site's public paste handler focuses a
    # trailing paragraph after upload; never silently move an existing draft.
    for index, paragraph in enumerate(plan["body"].split("\n\n")):
        if index:
            authorize()
            _typing_context(page, editor, options, selectors, origin=origin)
            page.keyboard.press("Enter")
        for line_index, line in enumerate(paragraph.split("\n")):
            if line_index:
                authorize()
                _typing_context(page, editor, options, selectors, origin=origin)
                page.keyboard.press("Shift+Enter")
            authorize()
            _typing_context(page, editor, options, selectors, origin=origin)
            page.keyboard.insert_text(line)
    for tag in plan["tags"]:
        authorize()
        _editor_account(page, options, selectors, origin=origin)
        tag_input = _one(page, selectors["tags_input"], "PIKABU_TAG_INPUT_AMBIGUOUS")
        tag_input.fill(tag)
        authorize()
        _editor_account(page, options, selectors, origin=origin)
        if tag_input.input_value() != tag or not tag_input.evaluate("n=>document.activeElement===n"):
            raise AgentError("PIKABU_EDITOR_FOCUS_CHANGED")
        tag_input.press("Enter")
    _check_editor(page, plan, options, selectors, origin=origin)
    _save(journal_path, journal, "prepared", editor_snapshot_sha256=fingerprint(plan), editor_url=page.url)


def _editor_account(page, options, selectors, *, origin):
    if page.url != origin + "/add":
        raise AgentError("PIKABU_EDITOR_URL_CHANGED")
    _account(page, options, selectors, origin=origin)


def _typing_context(page, editor, options, selectors, *, origin):
    _editor_account(page, options, selectors, origin=origin)
    if not editor.evaluate("""node=>{
      const selected=getSelection(),active=document.activeElement;
      return (active===node || node.contains(active)) && selected && selected.rangeCount===1
        && selected.isCollapsed && node.contains(selected.anchorNode) && node.contains(selected.focusNode);
    }"""):
        raise AgentError("PIKABU_EDITOR_FOCUS_CHANGED")


def _image_readback(page, image_urls, media):
    hashes = []
    for url, expected in zip(image_urls, media, strict=True):
        # Public response bytes, no new upload and no remote API mutation.
        response = page.request.get(url, fail_on_status_code=False, max_redirects=0)
        if response.status in {401, 403, 429}:
            raise AgentError("PIKABU_CAPTCHA_OR_ACCESS_BLOCKED", 403)
        if response.status != 200:
            raise AgentError("PIKABU_READBACK_IMAGE_UNAVAILABLE")
        raw = response.body()
        if len(raw) > 10 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise AgentError("PIKABU_READBACK_IMAGE_HASH_MISMATCH")
        hashes.append(expected["sha256"])
    return hashes


def _canonical_story(value, origin):
    if origin == ORIGIN:
        return story_url(value)
    if not isinstance(value, str):
        raise AgentError("PIKABU_READBACK_WRONG_STORY")
    parsed, expected = urlsplit(value), urlsplit(origin)
    if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise AgentError("PIKABU_READBACK_WRONG_STORY")
    canonical = story_url(ORIGIN + parsed.path)
    return origin + canonical.removeprefix(ORIGIN)


def _public_capture(page, canonical, journal, options, *, origin):
    """Capture proof HTML, visible content and image DOM in one JS task."""
    selectors = _dom(options)
    _account(page, options, selectors, origin=origin)
    captured = page.evaluate("""args => {
      const visible=n=>{
        const box=n.getBoundingClientRect();
        if(box.height<=0 || (n.tagName!=='BR' && box.width<=0))return false;
        for(let current=n;current;current=current.parentElement){
          const s=getComputedStyle(current);
          if(s.display==='none' || s.visibility!=='visible' || Number(s.opacity)===0)return false;
        }return true;
      };
      const rendered=n=>n.nodeType===Node.TEXT_NODE ? n.textContent
        : n.nodeType!==Node.ELEMENT_NODE || !visible(n) ? '' : n.tagName==='BR' ? '\\n'
        : [...n.childNodes].map(rendered).join('');
      const articles=[...document.querySelectorAll(args.article)];
      const article=articles.length===1 ? articles[0] : null;
      const accounts=[...document.querySelectorAll(args.account)].map(n=>({
        href:n.getAttribute('href')||'',visible:visible(n)}));
      const relevant=article ? [...article.querySelectorAll(
        '.story__title .story__title-link,.story__user-link,.tags__tag,'+
        '.story__content-inner .story-block_type_text p,.story-image__image')] : [];
      return {url:location.href,html:document.documentElement.outerHTML,accounts,
        visible:relevant.map(visible),article_count:articles.length,
        rendered_paragraphs:article ? [...article.querySelectorAll(
          '.story__content-inner .story-block_type_text p')].map(rendered) : [],
        content:article ? [...article.querySelectorAll(
          '.story__title,.story__user-link,.tags,.story__content-inner')].map(n=>n.outerHTML) : [],
        images:article ? [...article.querySelectorAll('.story__content-inner img')].map(n=>({
          src:n.currentSrc||n.src||'',original:n.getAttribute('data-large-image')||
            n.getAttribute('data-src')||n.src||'',complete:n.complete===true,
          width:n.naturalWidth||0,height:n.naturalHeight||0,visible:visible(n)})) : []};
    }""", {"article": 'article.story[data-story-id="' + canonical.rsplit("_", 1)[1] + '"]',
            "account": selectors["account"]})
    current = _canonical_story(captured["url"], origin)
    if current != canonical:
        raise AgentError("PIKABU_READBACK_WRONG_STORY")
    if len(captured["accounts"]) != 1 or not captured["accounts"][0]["visible"]:
        raise AgentError("PIKABU_ACCOUNT_MISMATCH", 403)
    _account_href(captured["accounts"][0]["href"], options["owner"], origin)
    raw = captured.pop("html").encode("utf-8")
    parsed_url = canonical
    if origin != ORIGIN:
        raw = raw.replace(origin.encode(), ORIGIN.encode())
        parsed_url = canonical.replace(origin, ORIGIN)
    # Playwright serializes Unicode DOM. The site's original windows-1251 meta
    # must not make BeautifulSoup reinterpret these explicitly UTF-8 bytes.
    proof = parse_public_story(raw, parsed_url, journal["snapshot"], options["owner"], encoding="utf-8")
    from .pikabu_delivery import editor_body
    if "\n\n".join(captured["rendered_paragraphs"]) != editor_body(journal["snapshot"]):
        raise AgentError("PIKABU_READBACK_HIDDEN_CONTENT")
    if captured["article_count"] != 1 or not captured["visible"] or not all(captured["visible"]):
        raise AgentError("PIKABU_READBACK_HIDDEN_CONTENT")
    if len(captured["images"]) != len(journal["snapshot"].get("media", [])):
        raise AgentError("PIKABU_READBACK_IMAGE_MISMATCH")
    if any(not image["complete"] or not image["visible"]
           or min(image["width"], image["height"]) <= 0 for image in captured["images"]):
        raise AgentError("PIKABU_READBACK_IMAGE_UNCONFIRMED")
    return proof, captured


def _readback(page, url, journal, options, *, origin=ORIGIN):
    # Never navigate a fixture journal's normalized public parser URL. The
    # executed URL is bound to its durable origin before making any request.
    canonical = _canonical_story(url, origin)
    _navigate(page, canonical)
    # Let public images finish loading before the first atomic content capture.
    page.wait_for_function("""arg => {
      const images=[...document.querySelectorAll(arg.selector)];
      return images.length===arg.count && images.every(n=>n.complete && n.naturalWidth>0);
    }""", arg={"selector": 'article.story[data-story-id="' + canonical.rsplit("_", 1)[1]
                              + '"] .story__content-inner img',
               "count": len(journal["snapshot"].get("media", []))})
    proof, before = _public_capture(page, canonical, journal, options, origin=origin)
    image_urls = proof["image_urls"]
    if origin != ORIGIN:
        image_urls = [url.replace("https://cs1.pikabu.ru", origin) for url in image_urls]
    image_sha256 = _image_readback(page, image_urls, journal["snapshot"].get("media", []))
    # No stale proof survives a network wait: atomically recapture and reparse
    # canonical URL, logged owner, author, title, body, sources, tags and all images.
    try:
        proof, after = _public_capture(page, canonical, journal, options, origin=origin)
        if before != after:
            raise AgentError("PIKABU_READBACK_CHANGED_DURING_IMAGE_READBACK")
    except AgentError as exc:
        raise AgentError("PIKABU_READBACK_CHANGED_DURING_IMAGE_READBACK") from exc
    proof["image_sha256"] = image_sha256
    proof["public_dom_sha256"] = fingerprint(after)
    proof["verification"] = "independent_reload_exact_text_owner_links_tags_image_sha256"
    proof["observed_at"] = time.time()
    return proof


def _submit(page, plan, options, selectors, path, journal, authorize, *, origin):
    authorize()
    expected = _check_editor(page, plan, options, selectors, origin=origin)
    # Image reads may take long enough for approval, pause or lease to change.
    authorize()
    _save(path, journal, "click_intent", clicks=1)
    # The durable write itself is also outside the database transaction.
    authorize()
    button = _final_editor(page, expected, options, selectors, origin=origin,
                           changed_code="PIKABU_EDITOR_CHANGED_BEFORE_PUBLISH")
    button.click(timeout=1000, no_wait_after=True)
    page.wait_for_url(re.compile(re.escape(origin) + r"/story/[A-Za-z0-9_-]+_\d+$"))
    url = page.url
    _save(path, journal, "readback_pending", permalink=url, readback_url=url)
    proof = _readback(page, url, journal, options, origin=origin)
    _save(path, journal, "confirmed", proof=proof, external_id=proof["external_id"],
          permalink=url, readback_url=url)


def _failure(path, journal, exc):
    code = exc.code if isinstance(exc, AgentError) else "PIKABU_BROWSER_OPERATION_UNKNOWN"
    state = "unknown" if journal["state"] != "intent" else "failed"
    _save(path, journal, state, error=code)


def run_publish(page, root, options, payload, authorize, *, origin=ORIGIN, fixture=False):
    """The production entry always supplies its database authorizer and fixed origin."""
    root = Path(root).resolve()
    execution = _execution(origin, fixture)
    path = _journal_path(root, payload.get("request_id"))
    snapshot = payload.get("snapshot")
    plan = PikabuDelivery(root, options).validate(payload.get("destination"), snapshot)
    expected = owner_name(options.get("owner"))
    if payload.get("destination") not in {expected, "user:" + expected, ORIGIN + "/@" + expected}:
        raise AgentError("PIKABU_DESTINATION_OWNER_MISMATCH")
    if plan["community"]:
        raise AgentError("PIKABU_BROWSER_COMMUNITY_UNSUPPORTED")
    _media_contract(options, plan)
    _body_contract(plan)
    if path.exists():
        _, old = _read_journal(root, payload["request_id"])
        _journal_execution(old, origin, fixture)
        if (old.get("snapshot_sha256") != fingerprint(snapshot)
                or old.get("destination") != payload["destination"]
                or old.get("options_sha256") != fingerprint(options)):
            raise AgentError("PIKABU_JOURNAL_CONFLICT", 409)
        if old.get("state") == "prepared" and payload.get("action") != "prepare":
            # A known preparation may continue once after a fresh approval check.
            # Unknown uploads/clicks never enter this branch, and no file is pasted again.
            selectors = _dom(options, publish=True)
            try:
                if old.get("editor_url") != origin + "/add" or old.get("clicks") != 0:
                    raise AgentError("PIKABU_PREPARED_EDITOR_INVALID")
                authorize()
                if page.url != old["editor_url"]:
                    _navigate(page, old["editor_url"])
                _ready_editor(page, selectors, options)
                _submit(page, plan, options, selectors, path, old, authorize, origin=origin)
            except Exception as exc:
                _failure(path, old, exc)
        return _result(old, fixture=fixture)
    selectors = _dom(options, publish=True)
    authorize()
    journal = {"contract": CONTRACT, **execution, "root": str(root), "request_id": payload["request_id"],
               "destination": payload["destination"], "owner": expected, "snapshot": snapshot,
               "snapshot_sha256": fingerprint(snapshot), "options_sha256": fingerprint(options),
               "dom_contract_sha256": fingerprint(selectors), "state": "intent",
               "created_at": time.time(), "clicks": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive durable claim before opening the editor or generating an autosave.
    journal["journal_sha256"] = fingerprint(journal)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(journal, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _navigate(page, origin + "/add")
        _fill_editor(page, plan, options, selectors, path, journal, authorize, origin=origin)
        if payload.get("action") == "prepare":
            return _result(journal, fixture=fixture)
        _submit(page, plan, options, selectors, path, journal, authorize, origin=origin)
    except Exception as exc:
        _failure(path, journal, exc)
    return _result(journal, fixture=fixture)


def _result(journal, *, fixture=False):
    if journal.get("fixture") is not fixture:
        raise AgentError("PIKABU_JOURNAL_CONFLICT", 409)
    state = journal.get("state")
    return {"state": state if state in {"confirmed", "prepared", "failed"} else "unknown",
            "external_id": journal.get("external_id"), "permalink": journal.get("permalink"),
            "verified": state == "confirmed" and not fixture and journal.get("execution_origin") == ORIGIN,
            "evidence": {"kind": "fixture" if fixture else "browser_readback",
                         "contract": CONTRACT, "request_id": journal["request_id"],
                         "execution_origin": journal["execution_origin"], "fixture": fixture,
                         "root": journal["root"], "destination": journal["destination"],
                         "owner": journal["owner"], "options_sha256": journal["options_sha256"],
                         "dom_contract_sha256": journal["dom_contract_sha256"],
                         "snapshot_sha256": journal["snapshot_sha256"],
                         "state": state, "clicks": journal.get("clicks", 0),
                         "error": journal.get("error"), "proof": journal.get("proof")}}


def reconcile(page, root, options, destination, request_id, *, origin=ORIGIN, fixture=False):
    _execution(origin, fixture)
    path, journal = _read_journal(root, request_id)
    _journal_execution(journal, origin, fixture)
    if (journal.get("destination") != destination or journal.get("owner") != options.get("owner")
            or journal.get("options_sha256") != fingerprint(options)):
        raise AgentError("PIKABU_JOURNAL_CONFLICT", 409)
    url = journal.get("readback_url") or journal.get("permalink")
    if not url:
        # Without a unique URL the unknown attempt remains blocked. No second click,
        # no new draft, and no speculative search match counts as reconciliation.
        if journal.get("state") == "confirmed":
            _save(path, journal, "unknown", error="PIKABU_READBACK_URL_MISSING")
        return _result(journal, fixture=fixture)
    try:
        proof = _readback(page, url, journal, options, origin=origin)
        _save(path, journal, "confirmed", proof=proof, external_id=proof["external_id"],
              permalink=url, readback_url=url, error=None)
    except Exception as exc:
        _save(path, journal, "unknown", error=exc.code if isinstance(exc, AgentError)
              else "PIKABU_BROWSER_OPERATION_UNKNOWN")
    return _result(journal, fixture=fixture)


@contextmanager
def session(root, options, *, visible=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AgentError("PIKABU_BROWSER_DEPENDENCY_REQUIRED") from exc
    root = Path(root).resolve()
    profile = inside(root, options.get("profile_dir", "data/pikabu-browser"))
    if profile.parent != inside(root, "data") or not profile.name.startswith("pikabu-browser"):
        raise AgentError("PIKABU_BROWSER_PROFILE_INVALID")
    with profile_lock(profile.with_suffix(".lock")), sync_playwright() as playwright:
        browser_options = {"headless": False if visible else options.get("headless", True),
                           "locale": "ru-RU", "accept_downloads": False,
                           "viewport": {"width": 1280, "height": 900}, "chromium_sandbox": True}
        if options.get("channel", "chrome") != "chromium":
            browser_options["channel"] = options.get("channel", "chrome")
        context = playwright.chromium.launch_persistent_context(str(profile), **browser_options)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(min(180000, int(options.get("operation_timeout_seconds", 180)) * 1000))
            yield page
        finally:
            context.close()


def target_options(root, destination):
    path = inside(root, "config/publishing.json")
    config = parse_json(path.read_text(encoding="utf-8"))
    rows = [item for item in config.get("targets", []) if item.get("destination") == destination
            and item.get("platform") == "pikabu" and item.get("route") == "pikabu_browser"]
    if len(rows) != 1:
        raise AgentError("PIKABU_TARGET_NOT_CONFIGURED")
    return rows[0].get("options", {})


def authorize(root, payload, options, *, expected_lease_owner=None):
    """Only a currently leased exact dispatch can mutate the remote editor."""
    from .config import Settings
    from .db import Database, now
    from .hub import HubService
    from .hub_models import Dispatch
    from .server_state import require_running
    settings = Settings.load(root)
    db = Database(settings)
    service = None
    try:
        with db.session() as transaction:
            row = transaction.get(Dispatch, payload.get("request_id"))
            if (row is None or row.state != "sending" or not row.owner
                    or (expected_lease_owner is not None and row.owner != expected_lease_owner)
                    or not math.isfinite(row.lease_until) or row.lease_until <= now() + 3
                    or row.due_at > now()):
                raise AgentError("PIKABU_DISPATCH_NOT_APPROVED", 403)
            service = HubService(settings, db, bootstrap_targets=False)
            require_running(service, transaction)
            variant, target = service._valid_dispatch(transaction, row)
            if (target.platform != "pikabu" or target.route != "pikabu_browser"
                    or target.destination != payload.get("destination")
                    or fingerprint(target.options) != fingerprint(options)
                    or fingerprint(row.snapshot) != fingerprint(payload.get("snapshot"))
                    or fingerprint(service.snapshot(variant, target)) != fingerprint(row.snapshot)):
                raise AgentError("PIKABU_DISPATCH_NOT_APPROVED", 403)
            return row.owner
    finally:
        if service is not None:
            service.close()
        db.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Изолированный браузерный маршрут Пикабу")
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
                _navigate(page, ORIGIN + "/add")
                deadline = time.monotonic() + 900
                next_probe = 0
                while time.monotonic() < deadline:
                    if time.monotonic() >= next_probe:
                        report = probe_page(page, options, navigate=False)
                        directory = inside(root, "data/pikabu-login")
                        directory.mkdir(parents=True, exist_ok=True)
                        report_path = directory / "dom-inspection.json"
                        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                        next_probe = time.monotonic() + 5
                    try:
                        _account(page, options, _dom(options))
                        print(json.dumps({"state": "authenticated", "owner": options["owner"],
                                          "live_verified": False}))
                        return
                    except AgentError as exc:
                        if exc.code not in {"PIKABU_LOGIN_REQUIRED", "PIKABU_ACCOUNT_MISMATCH"}:
                            raise
                    page.wait_for_timeout(500)
                raise AgentError("PIKABU_LOGIN_TIMEOUT")
        payload = parse_json(sys.stdin.read(4 * 1024 * 1024))
        if not isinstance(payload, dict):
            raise AgentError("PIKABU_REQUEST_INVALID")
        options = target_options(root, payload.get("destination"))
        if payload.get("options_sha256") != fingerprint(options):
            raise AgentError("PIKABU_CONFIG_CHANGED")
        action = payload.get("action")
        if action not in {"inspect", "probe", "publish", "prepare", "reconcile"}:
            raise AgentError("PIKABU_ACTION_INVALID")
        lease_owner = None
        if action in {"publish", "prepare"}:
            lease_owner = authorize(root, payload, options)
        with session(root, options) as page:
            if action == "inspect":
                value = inspect_page(page, options)
            elif action == "probe":
                value = probe_page(page, options)
            elif action == "reconcile":
                value = reconcile(page, root, options, payload["destination"], payload["request_id"])
            else:
                value = run_publish(page, root, options, payload,
                                    lambda: authorize(root, payload, options,
                                                      expected_lease_owner=lease_owner))
        print(json.dumps(value, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"code": exc.code if isinstance(exc, AgentError)
                          else "PIKABU_BROWSER_OPERATION_UNKNOWN"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
