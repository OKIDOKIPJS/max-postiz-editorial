"""Явный режим исходящих сообщений своего MAX-аккаунта в отдельной solo-группе.

Числовой sender ID не выдумывается. Участие аккаунта сверяется по хешу видимого
телефона в штатных настройках; само значение телефона не журналируется.
"""

import hashlib
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import browser_runtime
from .config import AgentError, fingerprint
from .monitor_command_manifest import classify_command, parse_command_text, source_url_is_safe


OWNER_CONTRACT_V1 = "max-owner-chat-dom-v1"
OWNER_CONTRACT = "max-owner-chat-dom-v2"
OWNER_CONTRACT_VERSIONS = frozenset({OWNER_CONTRACT_V1, OWNER_CONTRACT})
OWNER_EVENT_CONTRACT = "max-owner-chat-event-v1"
AUTH_MODE = "authenticated_account_outgoing"


def is_owner_command(text):
    return classify_command(text) is not None


def owner_contract(*, timezone_name="Europe/Moscow", version=OWNER_CONTRACT):
    """Наблюдавшийся профиль MAX: меняется только явно проверенный часовой пояс UI."""
    if not isinstance(version, str) or version not in OWNER_CONTRACT_VERSIONS:
        raise AgentError("MONITOR_OWNER_CONTRACT_INVALID")
    result = {"version": version, "timezone": timezone_name,
        "settings_button_name": "Настройки",
        "phone": ".settingsTab button.profile span.phone", "root": ".openedChat",
        "header": "#main-header-title", "profile_label_prefix": "Открыть профиль ",
        "solo_marker": "Тут только вы", "date": ".capsuleSeparator",
        "date_text": "span.capsule", "message": ".block[role=listitem]",
        "bubble": '[data-bubbles-variant="outgoing"]', "content": ".bubbleContent",
        "caption": "span.text[data-message-double-click-ignore]", "time": ".meta [role=button]",
        "delivery_icons": ["#icon_status_delivered", "#icon_status_read"]}
    if version == OWNER_CONTRACT:
        result["source_anchor"] = {"tag": "A", "class_token": "link",
            "scope_class_pattern": "svelte-[a-z0-9]+",
            "fixed_attributes": {"aria-haspopup": "dialog", "aria-controls": "", "aria-expanded": "false"},
            "attribute_names": ["class", "href", "aria-haspopup", "aria-controls", "aria-expanded"]}
    return result


def owner_contract_hash(contract):
    if not isinstance(contract, dict) or not isinstance(contract.get("timezone"), str):
        raise AgentError("MONITOR_OWNER_CONTRACT_INVALID")
    try:
        ZoneInfo(contract["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise AgentError("MONITOR_OWNER_CONTRACT_INVALID") from error
    if contract != owner_contract(timezone_name=contract["timezone"], version=contract.get("version")):
        raise AgentError("MONITOR_OWNER_CONTRACT_INVALID")
    return fingerprint(contract)


def phone_fingerprint(value):
    """Хешируем нормализованные цифры видимого телефона, не возвращая номер."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 64
            or re.fullmatch(r"[0-9+()\s-]+", value) is None):
        raise AgentError("MONITOR_OWNER_ACCOUNT_PHONE_INVALID")
    digits = re.sub(r"[^0-9]", "", value)
    if not 10 <= len(digits) <= 15:
        raise AgentError("MONITOR_OWNER_ACCOUNT_PHONE_INVALID")
    return hashlib.sha256(digits.encode("ascii")).hexdigest()


def owner_message_id(chat_id, principal, created_at, text):
    """Консервативный ID содержимого/минуты; это не platform message ID."""
    if (not isinstance(chat_id, str) or not re.fullmatch(r"-?[0-9]{1,22}", chat_id)
            or not isinstance(principal, str)
            or not re.fullmatch(r"max-account-sha256:[a-f0-9]{64}", principal)
            or type(created_at) is not int or created_at <= 0 or created_at % 60
            or not isinstance(text, str) or not text.strip() or len(text) > 4000):
        raise AgentError("MONITOR_OWNER_EVENT_INVALID")
    return "owner-" + fingerprint({"contract": OWNER_EVENT_CONTRACT, "chat_id": chat_id,
        "principal": principal, "created_at": created_at, "text": text})


def _caption_shape_valid(message, parsed, contract):
    elements, anchors = message["caption_elements"], message["anchors"]
    if not isinstance(elements, list) or not isinstance(anchors, list):
        return False
    if not elements and not anchors:
        return True
    if (contract["version"] != OWNER_CONTRACT or parsed["action"] != "source_propose"
            or not source_url_is_safe(parsed.get("url")) or len(elements) != 1 or len(anchors) != 1):
        return False
    element, anchor = elements[0], anchors[0]
    profile = contract["source_anchor"]
    if (not isinstance(element, dict) or not isinstance(anchor, dict)
            or element.get("tag") != profile["tag"] or element.get("direct") is not True
            or anchor.get("direct") is not True or anchor.get("children") != 0
            or anchor.get("shown") is not True
            or anchor.get("href") != parsed["url"] or anchor.get("text") != parsed["url"]):
        return False
    pairs = element.get("attributes")
    if (not isinstance(pairs, list)
            or any(not isinstance(pair, list) or len(pair) != 2
                   or any(not isinstance(value, str) for value in pair) for pair in pairs)):
        return False
    attributes = dict(pairs)
    if (len(attributes) != len(pairs) or set(attributes) != set(profile["attribute_names"])
            or attributes.get("href") != parsed["url"]
            or any(attributes.get(key) != value for key, value in profile["fixed_attributes"].items())):
        return False
    classes = attributes["class"].split()
    return (len(classes) == 2 and profile["class_token"] in classes
            and sum(re.fullmatch(profile["scope_class_pattern"], value) is not None for value in classes) == 1)


_SOLO_CONTEXT = """({c, url, name}) => {
  if (location.href !== url) return 'wrong_chat';
  const roots = document.querySelectorAll(c.root), headers = document.querySelectorAll(c.header);
  const buttons = [...document.querySelectorAll('button')].filter(node =>
    node.getAttribute('aria-label') === c.profile_label_prefix + name);
  if (roots.length !== 1 || headers.length !== 1 || buttons.length !== 1) return false;
  if (headers[0].innerText.trim() !== 'Окно чата с ' + name) return 'wrong_chat';
  const shown = node => { const box = node.getBoundingClientRect();
    return node.isConnected && box.width > 0 && box.height > 0 &&
      getComputedStyle(node).visibility === 'visible'; };
  if (!shown(roots[0]) || !shown(buttons[0])) return false;
  const clean = text => text.replace(/\\s+/g, ' ').trim();
  return clean(buttons[0].innerText) === clean(name + ' ' + c.solo_marker) ? 'ready' : 'not_solo';
}"""

_CAPTURE = """({c, url, name, limit}) => {
  const fail = () => { throw new Error('MONITOR_OWNER_DOM_INVALID'); };
  const one = (root, selector) => { const nodes = root.querySelectorAll(selector);
    if (nodes.length !== 1) return fail(); return nodes[0]; };
  const clean = text => text.replace(/\\s+/g, ' ').trim();
  const shown = node => { const box = node.getBoundingClientRect();
    return node.isConnected && box.width > 0 && box.height > 0 &&
      getComputedStyle(node).visibility === 'visible'; };
  const context = () => {
    if (location.href !== url) return fail();
    const root = one(document, c.root), heading = one(document, c.header);
    const buttons = [...document.querySelectorAll('button')].filter(node =>
      node.getAttribute('aria-label') === c.profile_label_prefix + name);
    if (!shown(root) || heading.innerText.trim() !== 'Окно чата с ' + name ||
        buttons.length !== 1 || !shown(buttons[0]) ||
        clean(buttons[0].innerText) !== clean(name + ' ' + c.solo_marker)) return fail();
    return root;
  };
  const root = context();
  const actualZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (actualZone !== c.timezone) return fail();
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-US', {timeZone: c.timezone, year:'numeric',
    month:'2-digit', day:'2-digit'}).formatToParts(now);
  const calendar = Object.fromEntries(parts.filter(part => part.type !== 'literal')
    .map(part => [part.type, part.value]));
  const roots = [...root.querySelectorAll(c.message)];
  if (roots.length > limit || roots.some(node => roots.some(other =>
      node !== other && other.contains(node)))) return fail();
  let day = null; const messages = [];
  for (const node of root.querySelectorAll(c.date + ', ' + c.message)) {
    if (node.matches(c.date)) {
      day = one(node, c.date_text).innerText.trim();
      continue;
    }
    const bubbles = node.querySelectorAll(c.bubble);
    if (!bubbles.length) continue; // incoming/system messages never obtain authority.
    const captions = [...node.querySelectorAll(c.caption)];
    if (captions.length > 16) return fail();
    // Classification is performed by the SAME pure manifest as gateway/help in Python.
    const result = {date:day, caption_texts:captions.map(caption => caption.innerText),
      minute:null, text:captions.length === 1 ? captions[0].innerText : null,
      shape_valid:false, caption_elements:[], anchors:[]};
    messages.push(result);
    if (bubbles.length !== 1 || !shown(node) || !shown(bubbles[0])) continue;
    const bubble = bubbles[0];
    if (bubble.getAttribute('data-bubbles-variant') !== 'outgoing' ||
        node.querySelectorAll('[data-bubbles-variant]').length !== 1) continue;
    const contents = bubble.querySelectorAll(c.content);
    if (contents.length !== 1 || captions.length !== 1) continue;
    const content = contents[0], caption = captions[0], times = content.querySelectorAll(c.time);
    if (times.length !== 1) continue;
    const when = times[0];
    if (!shown(content) || !shown(caption) || !shown(when) || caption.parentElement !== content ||
        !day) continue;
    const minute = when.innerText.trim(), text = caption.innerText;
    result.minute = minute;
    result.caption_elements = [...caption.querySelectorAll('*')].map(element => ({
      tag:element.tagName, direct:element.parentElement === caption,
      attributes:[...element.attributes].map(attribute => [attribute.name,attribute.value])}));
    result.anchors = [...caption.querySelectorAll('a')].map(anchor => ({
      href:anchor.getAttribute('href'), text:anchor.innerText, direct:anchor.parentElement === caption,
      children:anchor.children.length, shown:shown(anchor)}));
    if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(minute) || !text.trim() || text.length > 4000 ||
        clean(node.innerText) !== clean(text + ' ' + minute)) continue;
    if (bubble.querySelector('img,video,audio,canvas,iframe,input,[contenteditable]')) continue;
    const markers = [node, ...node.querySelectorAll('[class],[aria-label],[title]')];
    const unsafe = /forward|quoted?|reply|edited?|пересла|цитат|измен[её]н|редактир/i;
    if (markers.some(mark => ['class','aria-label','title'].some(attribute =>
        unsafe.test(mark.getAttribute(attribute) || '')))) continue;
    const icons = [...bubble.querySelectorAll('svg')];
    if (icons.length !== 1 || icons.some(icon => {
      const uses = icon.querySelectorAll('use');
      const href = uses.length === 1 &&
        (uses[0].getAttribute('href') || uses[0].getAttributeNS('http://www.w3.org/1999/xlink','href'));
      return !c.delivery_icons.includes(href);
    })) continue;
    result.shape_valid = true;
  }
  context();
  return {messages, calendar, timezone:actualZone, browser_epoch_ms:now.getTime()};
}"""


def _navigate(page, address):
    response = page.goto(address, wait_until="domcontentloaded")
    if response is not None and response.status == 429:
        raise AgentError("MONITOR_OWNER_RATE_LIMITED")
    if response is not None and response.status >= 400:
        raise AgentError("MONITOR_OWNER_UNAVAILABLE")
    if page.url != address:
        raise AgentError("MONITOR_OWNER_WRONG_CHAT")


def _ensure_solo(page, contract, address, name):
    args = {"c": contract, "url": address, "name": name}
    try:
        handle = page.wait_for_function(_SOLO_CONTEXT, arg=args, timeout=15_000, polling=100)
        state = handle.json_value()
        handle.dispose()
    except Exception as error:
        raise AgentError("MONITOR_OWNER_CHAT_CONTEXT_INVALID") from error
    if state == "wrong_chat":
        raise AgentError("MONITOR_OWNER_WRONG_CHAT")
    if state == "not_solo":
        raise AgentError("MONITOR_OWNER_NOT_SOLO")
    final_state = page.evaluate(_SOLO_CONTEXT, args)
    if final_state != "ready":
        raise AgentError("MONITOR_OWNER_NOT_SOLO" if final_state == "not_solo"
                         else "MONITOR_OWNER_WRONG_CHAT")


def prepare_owner_chat(page, reader, chat_id, *, navigate=True, require_accepted=True):
    """Общий account/solo guard для чтения и текстового Send; телефон не сохраняется."""
    reader._guard()
    options = reader.options
    contract = options.get("dom_contract")
    contract_hash = owner_contract_hash(contract)
    account_hash = options.get("account_sha256")
    name = options.get("control_chat_name")
    if (options.get("auth_mode") != AUTH_MODE or chat_id != options.get("control_chat_id")
            or not isinstance(account_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", account_hash)
            or not isinstance(name, str) or not name.strip()):
        raise AgentError("MONITOR_OWNER_PAIRING_INVALID")
    if require_accepted and (options.get("dom_contract_verified") is not True
            or options.get("dom_contract_sha256") != contract_hash):
        raise AgentError("MONITOR_OWNER_CONTRACT_UNVERIFIED")
    address = browser_runtime.ORIGIN + "/" + chat_id
    if navigate:
        _navigate(page, address)
    _ensure_solo(page, contract, address, name)
    try:
        phone = page.locator(contract["phone"] + ":visible")
        if not phone.count():
            page.get_by_role("button", name=contract["settings_button_name"], exact=True).click()
        phone.wait_for(state="visible", timeout=15_000)
        if phone.count() != 1 or phone_fingerprint(phone.inner_text()) != account_hash:
            raise AgentError("MONITOR_OWNER_ACCOUNT_MISMATCH")
    except AgentError:
        raise
    except Exception as error:
        raise AgentError("MONITOR_OWNER_ACCOUNT_UNAVAILABLE") from error
    reader._guard()
    _navigate(page, address)
    _ensure_solo(page, contract, address, name)
    return contract


def check_owner_chat_now(page, reader, chat_id):
    """Без ожиданий/переходов после финального разрешения отправки."""
    reader._guard()
    if chat_id != reader.options.get("control_chat_id"):
        raise AgentError("MONITOR_OWNER_WRONG_CHAT")
    state = page.evaluate(_SOLO_CONTEXT, {"c": reader.options["dom_contract"],
        "url": browser_runtime.ORIGIN + "/" + chat_id, "name": reader.options["control_chat_name"]})
    if state != "ready":
        raise AgentError("MONITOR_OWNER_NOT_SOLO" if state == "not_solo" else "MONITOR_OWNER_WRONG_CHAT")


def _capture(reader, *, limit, require_accepted):
    reader._guard()
    options = reader.options
    if options.get("auth_mode") != AUTH_MODE:
        raise AgentError("MONITOR_OWNER_AUTH_MODE_INVALID")
    account_hash = options.get("account_sha256")
    chat_id, name = options.get("control_chat_id"), options.get("control_chat_name")
    if (not isinstance(account_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", account_hash)
            or not isinstance(chat_id, str) or not re.fullmatch(r"-?[0-9]{1,22}", chat_id)
            or not isinstance(name, str) or not name.strip() or len(name) > 200
            or any(ord(character) < 32 for character in name)):
        raise AgentError("MONITOR_OWNER_PAIRING_INVALID")
    contract = options.get("dom_contract")
    contract_hash = owner_contract_hash(contract)
    if require_accepted and (options.get("dom_contract_verified") is not True
            or options.get("dom_contract_sha256") != contract_hash):
        raise AgentError("MONITOR_OWNER_CONTRACT_UNVERIFIED")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise AgentError("MONITOR_OWNER_LIMIT_INVALID")
    address = browser_runtime.ORIGIN + "/" + chat_id
    with browser_runtime.session(reader.settings) as page:
        prepare_owner_chat(page, reader, chat_id, require_accepted=require_accepted)
        try:
            captured = page.evaluate(_CAPTURE, {"c": contract, "url": address,
                "name": name, "limit": limit})
        except Exception as error:
            raise AgentError("MONITOR_OWNER_DOM_INVALID") from error
    reader._guard()
    observed_at = time.time()
    try:
        if (not isinstance(captured, dict) or captured["timezone"] != contract["timezone"]
                or type(captured["browser_epoch_ms"]) not in {int, float}
                or abs(captured["browser_epoch_ms"] / 1000 - observed_at) > 60
                or not isinstance(captured["messages"], list) or len(captured["messages"]) > limit):
            raise ValueError
        calendar = captured["calendar"]
        day = datetime(int(calendar["year"]), int(calendar["month"]), int(calendar["day"])).date()
        zone = ZoneInfo(contract["timezone"])
        if day != datetime.fromtimestamp(observed_at, zone).date():
            raise ValueError
        result, identities = [], set()
        principal = "max-account-sha256:" + account_hash
        for message in captured["messages"]:
            texts = message["caption_texts"]
            if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
                raise ValueError
            candidates = [parse_command_text(text) for text in texts]
            if not any(candidate is not None for candidate in candidates):
                continue
            label = message["date"]
            if label is None:
                raise AgentError("MONITOR_OWNER_DOM_INVALID")
            # Любая более старая/неоднозначная дата не превращается в сегодняшний приказ.
            if label not in {"Сегодня", "Вчера"}:
                continue
            if message["shape_valid"] is not True or len(texts) != 1:
                raise AgentError("MONITOR_OWNER_DOM_INVALID")
            parsed = candidates[0]
            if parsed["action"] == "source_propose" and not source_url_is_safe(parsed["url"]):
                continue
            if not _caption_shape_valid(message, parsed, contract):
                raise AgentError("MONITOR_OWNER_DOM_INVALID")
            selected_day = day - timedelta(days=1 if label == "Вчера" else 0)
            minute = message["minute"]
            if not isinstance(minute, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", minute):
                raise ValueError
            hour, minute_number = map(int, minute.split(":"))
            created = int(datetime(selected_day.year, selected_day.month, selected_day.day,
                                   hour, minute_number, tzinfo=zone).timestamp())
            text = message["text"]
            if not isinstance(text, str) or "\x00" in text or created > observed_at:
                raise ValueError
            ident = owner_message_id(chat_id, principal, created, text)
            if ident in identities:
                # Одинаковый текст в одной минуте принимается не более одного раза.
                continue
            identities.add(ident)
            result.append({"auth_mode": AUTH_MODE, "principal": principal,
                "account_sha256": account_hash, "chat_id": chat_id, "message_id": ident,
                "id_kind": "content_minute_fingerprint", "created_at": created,
                "created_at_precision_seconds": 60, "observed_at": observed_at, "text": text,
                "reply_to_message_id": None, "outgoing": True, "forwarded": False,
                "quoted": False, "edited": False, "dom_contract_sha256": contract_hash})
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
        raise AgentError("MONITOR_OWNER_MESSAGE_INVALID") from error
    return result


def read_owner_commands(reader, *, limit=100):
    return _capture(reader, limit=limit, require_accepted=True)


def inspect_owner_chat(reader, *, limit=100):
    messages = _capture(reader, limit=limit, require_accepted=False)
    return {"state": "contract_matches_dom", "message_count": len(messages),
        "dom_contract_sha256": owner_contract_hash(reader.options["dom_contract"]),
        "auth_mode": AUTH_MODE, "account_verified": True, "commands_enabled": False,
        "live_verified": False, "id_kind": "content_minute_fingerprint"}
