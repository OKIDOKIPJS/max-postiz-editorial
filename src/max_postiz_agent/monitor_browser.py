"""Отдельный MAX-профиль: служебные отчёты и чтение закрытого чата через DOM.

Транспорт не получает доступ к editorial approvals, cookies или скрытому API.
Отчёт использует прежний журнал Send/reload; неизвестная отправка не повторяется.
Команды доступны только после явной приёмки точного DOM-контракта владельцем.
"""

import copy
import hashlib
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import browser_runtime, browser_worker
from .config import AgentError, Settings, fingerprint, parse_json


CONTROL_CONTRACT = "max-control-chat-dom-v1"
REPORT_CONTRACT = "max-monitor-report-v1"
RUNTIME_DIRECTORY = "data/monitor/browser-runtime"
_DOM_FIELDS = {
    "version", "chat_root", "chat_id_attribute", "chat_kind_attribute", "header",
    "account", "account_id_attribute", "messages", "message_id_attribute", "sender",
    "sender_id_attribute", "time", "time_attribute", "timestamp_format", "text", "reply",
    "reply_id_attribute", "forwarded",
}
_ATTRIBUTES = {
    "chat_id_attribute", "chat_kind_attribute", "account_id_attribute",
    "message_id_attribute", "sender_id_attribute", "time_attribute", "reply_id_attribute",
}
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_NUMERIC_ID = re.compile(r"-?[0-9]{1,22}")
_USER_ID = re.compile(r"[0-9]{1,22}")
_GROUP_COMPOSER = '[contenteditable][role="textbox"][aria-placeholder="Сообщение"]'
_GROUP_SEND_LABEL = re.compile(r"^Отправить(?:\s|$)")


def _plain_path(root, relative):
    """Проверяем каждый компонент до mkdir, чтения журнала или открытия профиля."""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
        raise AgentError("MONITOR_BROWSER_PATH_INVALID")
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        if current.is_symlink() or current.is_junction():
            raise AgentError("MONITOR_BROWSER_LINK_FORBIDDEN")
        if current.is_file() and current.stat().st_nlink > 1:
            raise AgentError("MONITOR_BROWSER_LINK_FORBIDDEN")
    if not current.resolve().is_relative_to(root.resolve()):
        raise AgentError("MONITOR_BROWSER_PATH_INVALID")
    return current


def _identity(value, pattern, error):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AgentError(error)
    return value


def _chat(options, prefix):
    ident = _identity(options.get(prefix + "_chat_id"), _NUMERIC_ID,
                      "MONITOR_BROWSER_CHAT_INVALID")
    name = options.get(prefix + "_chat_name")
    if (not isinstance(name, str) or not name.strip() or len(name) > 256
            or any(char in name for char in "\r\n\x00")):
        raise AgentError("MONITOR_BROWSER_CHAT_INVALID")
    return ident, name


def control_contract_hash(contract):
    """Хеш считается только для полного поддерживаемого контракта, без селекторов по умолчанию."""
    if not isinstance(contract, dict) or set(contract) != _DOM_FIELDS:
        raise AgentError("MONITOR_CONTROL_CONTRACT_INVALID")
    if (contract["version"] != CONTROL_CONTRACT
            or contract["timestamp_format"] not in {"iso8601", "unix_seconds", "unix_milliseconds"}):
        raise AgentError("MONITOR_CONTROL_CONTRACT_INVALID")
    for key, value in contract.items():
        if (not isinstance(value, str) or not value.strip() or len(value) > 512
                or any(char in value for char in "\r\n\x00")):
            raise AgentError("MONITOR_CONTROL_CONTRACT_INVALID")
        if key in _ATTRIBUTES and not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_.:-]*", value):
            raise AgentError("MONITOR_CONTROL_CONTRACT_INVALID")
    return fingerprint(contract)


def control_contract_template():
    """Пустые поля заполняет владелец по наблюдаемому DOM; шаблон не разрешает чтение."""
    result = dict.fromkeys(sorted(_DOM_FIELDS), "")
    result.update(version=CONTROL_CONTRACT, timestamp_format="iso8601")
    return result


def _readback_result(result):
    if (isinstance(result, dict) and result.get("state") == "confirmed"
            and isinstance(result.get("external_id"), str) and result["external_id"]):
        return {**result, "verification": "browser_readback", "independent_readback": True}
    return result


_READ_CONTROL = """({contract: c, url, chatId, title, accountId, limit}) => {
  const fail = () => { throw new Error('MONITOR_CONTROL_DOM_INVALID'); };
  const one = (root, selector) => {
    const matches = root.querySelectorAll(selector);
    if (matches.length !== 1) return fail();
    return matches[0];
  };
  const shown = node => {
    const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
    return node.isConnected && rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' && style.visibility === 'visible';
  };
  if (location.href !== url) return fail();
  const root = one(document, c.chat_root), header = one(document, c.header);
  const account = one(document, c.account);
  if (!shown(root) || !shown(header) || header.innerText.trim() !== title ||
      root.getAttribute(c.chat_id_attribute) !== chatId ||
      root.getAttribute(c.chat_kind_attribute) !== 'chat' ||
      account.getAttribute(c.account_id_attribute) !== accountId) return fail();
  const nodes = [...root.querySelectorAll(c.messages)];
  if (nodes.length > limit || nodes.some(node => nodes.some(other =>
      node !== other && other.contains(node)))) return fail();
  const result = nodes.map(node => {
    if (!shown(node)) return fail();
    const sender = one(node, c.sender), when = one(node, c.time), text = one(node, c.text);
    if (!shown(text)) return fail();
    const replies = node.querySelectorAll(c.reply);
    if (replies.length > 1) return fail();
    return {message_id: node.getAttribute(c.message_id_attribute),
      chat_id: chatId, sender_id: sender.getAttribute(c.sender_id_attribute),
      timestamp: when.getAttribute(c.time_attribute), text: text.innerText,
      reply_to_message_id: replies.length ? replies[0].getAttribute(c.reply_id_attribute) : null,
      forwarded: node.querySelectorAll(c.forwarded).length > 0};
  });
  if (location.href !== url || root.getAttribute(c.chat_id_attribute) !== chatId ||
      root.getAttribute(c.chat_kind_attribute) !== 'chat' ||
      header.innerText.trim() !== title ||
      account.getAttribute(c.account_id_attribute) !== accountId) return fail();
  return result;
}"""


def _created_at(value, format_name, observed_at):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        if format_name == "iso8601":
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError
            result = parsed.timestamp()
        else:
            if not re.fullmatch(r"[0-9]{1,16}", value):
                raise ValueError
            result = int(value) / (1000 if format_name == "unix_milliseconds" else 1)
        if not math.isfinite(result) or result <= 0 or result > observed_at + 300:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise AgentError("MONITOR_CONTROL_TIMESTAMP_INVALID") from exc


class MonitorBrowser:
    """Операции ограничены двумя точными адресатами и независимым runtime."""

    def __init__(self, settings, options):
        if not isinstance(options, dict):
            raise AgentError("MONITOR_BROWSER_OPTIONS_INVALID")
        self.options = copy.deepcopy(options)
        self.source_settings = settings
        self.source_hash = settings.hash
        self.options_hash = fingerprint(self.options)
        self.project_root = settings.root.absolute()
        self.report_chat_id, self.report_chat_name = _chat(self.options, "report")
        self.destinations = {self.report_chat_id: {"name": self.report_chat_name, "kind": "channel"}}
        if self.options.get("reply_in_control_chat") is True:
            from .monitor_owner_chat import AUTH_MODE, owner_contract_hash
            control_id, control_name = _chat(self.options, "control")
            if (self.options.get("auth_mode") != AUTH_MODE
                    or self.options.get("dom_contract_verified") is not True
                    or self.options.get("dom_contract_sha256") != owner_contract_hash(self.options.get("dom_contract"))
                    or not re.fullmatch(r"[a-f0-9]{64}", str(self.options.get("account_sha256", "")))
                    or control_id == self.report_chat_id and control_name != self.report_chat_name):
                raise AgentError("MONITOR_CONTROL_GROUP_ROUTE_REQUIRED")
            self.destinations[control_id] = {"name": control_name, "kind": "solo_group"}
        elif self.options.get("control_chat_id") == self.report_chat_id:
            raise AgentError("MONITOR_CONTROL_GROUP_ROUTE_REQUIRED")
        root = _plain_path(self.project_root, RUNTIME_DIRECTORY)
        app = copy.deepcopy(settings.app)
        timeout = self.options.get("operation_timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise AgentError("MONITOR_BROWSER_OPTIONS_INVALID")
        headless = self.options.get("headless", True)
        channel = self.options.get("channel", "chrome")
        if type(headless) is not bool or channel not in {"chrome", "chromium", "msedge"}:
            raise AgentError("MONITOR_BROWSER_OPTIONS_INVALID")
        app.update(state_dir="data/state", media_dir="data/media", logs_dir="logs")
        app["max"] = {"adapter_mode": "browser", "monitor_contract": REPORT_CONTRACT,
            "monitor_config_hash": self.options_hash, "source_config_hash": self.source_hash,
            "monitor_destinations": self.destinations,
            "browser": {"profile_dir": "data/monitor-browser", "channel": channel,
                "headless": headless, "operation_timeout_seconds": timeout,
                "channels": {ident: value["name"] for ident, value in self.destinations.items()}}}
        self.settings = Settings(root, app, {"monitor_contract": REPORT_CONTRACT}, {})
        self.transport_hash = self.settings.hash
        self.app_hash = fingerprint(self.settings.app)
        self._guard()

    def _guard(self, request_id=None):
        if os.getenv("MAX_EXISTING_BROWSER_PROFILE"):
            raise AgentError("MONITOR_EXTERNAL_BROWSER_FORBIDDEN")
        if (fingerprint(self.options) != self.options_hash
                or self.source_settings.hash != self.source_hash
                or self.settings.hash != self.transport_hash
                or fingerprint(self.settings.app) != self.app_hash
                or self.settings.root != self.project_root / RUNTIME_DIRECTORY):
            raise AgentError("MONITOR_BROWSER_CONFIG_CHANGED")
        _plain_path(self.project_root, RUNTIME_DIRECTORY)
        for relative in ("data/monitor-browser", "data/monitor-browser.lock", "data/media",
                         "data/browser-publications"):
            _plain_path(self.settings.root, relative)
        if request_id is not None:
            for suffix in (".json", ".new", ".png", ".unknown.png", ".before-reload.png"):
                _plain_path(self.settings.root, "data/browser-publications/" + request_id + suffix)

    def _request_id(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise AgentError("MONITOR_BROWSER_REQUEST_INVALID")
        return "monitor-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()

    def _report_request(self, chat_id, text, request_id):
        if chat_id not in self.destinations:
            raise AgentError("MONITOR_BROWSER_CHAT_FORBIDDEN")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or "\x00" in text:
            raise AgentError("MONITOR_BROWSER_TEXT_INVALID")
        worker_id = self._request_id(request_id)
        self._guard(worker_id)
        return {"action": "publish", "config_hash": self.transport_hash,
            "request_id": worker_id, "chat_id": chat_id, "text": text, "attachment": None}

    def send_report(self, chat_id, text, request_id, *, authorize):
        """Точный отчёт требует разрешения до открытия окна и непосредственно до Send."""
        payload = self._report_request(chat_id, text, request_id)
        if not callable(authorize):
            raise AgentError("MONITOR_REPORT_AUTHORIZATION_REQUIRED")
        approval = {"contract": REPORT_CONTRACT, "request_id": request_id,
            "chat_id": chat_id, "text": text,
            "chat_name": self.destinations[chat_id]["name"],
            "destination_kind": self.destinations[chat_id]["kind"],
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "config_hash": self.transport_hash, "source_config_hash": self.source_hash,
            "monitor_config_hash": self.options_hash}
        expected = fingerprint(approval)

        def check():
            self._guard(payload["request_id"])
            if (fingerprint(approval) != expected
                    or authorize(copy.deepcopy(approval)) is not True):
                raise AgentError("MONITOR_REPORT_NOT_APPROVED")
            self._guard(payload["request_id"])

        check()
        # browser_worker сохраняет intent до клика и сверяет текст после reload.
        if self.destinations[chat_id]["kind"] == "solo_group":
            result = self._publish_group(payload, authorize_before_click=check)
        else:
            result = browser_worker.publish(self.settings, payload, authorize_before_click=check)
        return _readback_result(result)

    @staticmethod
    def _group_send_button(page):
        button = page.get_by_role("button", name=_GROUP_SEND_LABEL)
        if button.count() != 1 or not button.is_enabled():
            raise AgentError("MONITOR_GROUP_SEND_NOT_READY")
        return button

    def _wait_group_ready(self, page, composer, text):
        deadline = time.monotonic() + min(15, self.settings.app["max"]["browser"]["operation_timeout_seconds"])
        while time.monotonic() < deadline:
            if composer.inner_text() == text:
                button = page.get_by_role("button", name=_GROUP_SEND_LABEL)
                if button.count() == 1 and button.is_enabled():
                    return button
            page.wait_for_timeout(100)
        raise AgentError("MONITOR_GROUP_SEND_NOT_READY")

    def _group_exact_count(self, page, text):
        contract = self.options["dom_contract"]
        root = page.locator(contract["root"])
        if root.count() != 1:
            raise AgentError("MONITOR_OWNER_WRONG_CHAT")
        posts = root.locator(contract["message"])
        matches = 0
        for index in range(posts.count()):
            post = posts.nth(index)
            captions = post.locator(contract["caption"])
            bubbles = post.locator(contract["bubble"])
            if (captions.count() == 1 and bubbles.count() == 1
                    and captions.is_visible() and bubbles.is_visible()
                    and captions.inner_text() == text):
                matches += 1
        return matches

    def _group_readback(self, page, state):
        from .monitor_owner_chat import check_owner_chat_now
        deadline = time.monotonic() + min(15, self.settings.app["max"]["browser"]["operation_timeout_seconds"])
        while time.monotonic() < deadline:
            check_owner_chat_now(page, self, state["chat_id"])
            count = self._group_exact_count(page, state["text"])
            if count == state["before_count"] + 1:
                return {"state": "confirmed", "external_id": "browser-" + state["request_id"],
                        "permalink": None}
            if count > state["before_count"] + 1:
                return None
            page.wait_for_timeout(100)
        return None

    def _publish_group(self, payload, *, authorize_before_click):
        """Только plain text в принятую solo-группу; прежний worker MAX не меняется."""
        path = browser_worker.ledger_path(self.settings, payload["request_id"])
        content_hash = fingerprint(payload)
        if path.exists():
            saved = parse_json(path.read_text(encoding="utf-8"))
            if saved.get("content_hash") != content_hash:
                raise AgentError("BROWSER_INTENT_CONTENT_CHANGED")
            if saved.get("state") == "preclick_failed":
                return browser_worker._preclick_result(self.settings, payload)
            return saved["delivery"] if saved.get("state") == "confirmed" else {"state": "unknown"}
        if path.with_suffix(".new").exists():
            return {"state": "unknown"}
        state = {**browser_worker._request_record(self.settings, payload),
            "contract": "browser-send-journal-v1", "monitor_destination_kind": "solo_group",
            "state": "preparing", "send_started": False, "phase": "session", "started_at": time.time()}
        try:
            browser_worker.save(path, state, exclusive=True)
        except OSError:
            return {"state": "unknown"}
        try:
            return self._perform_group_send(payload, path, state, authorize_before_click)
        except Exception as error:
            code = error.code if isinstance(error, AgentError) else "BROWSER_OPERATION_FAILED"
            if state["send_started"]:
                state.update(state="unknown", error=code)
                try:
                    browser_worker.save(path, state)
                except Exception:
                    # Durable sending/partial journal still blocks every new click.
                    return {"state": "unknown"}
                return {"state": "unknown"}
            try:
                saved = parse_json(path.read_text(encoding="utf-8"))
                if (saved.get("state") != "preparing" or saved.get("send_started") is not False
                        or saved.get("content_hash") != content_hash
                        or saved.get("started_at") != state["started_at"]):
                    return {"state": "unknown"}
                state.update(contract=browser_worker.PRECLICK_CONTRACT, state="preclick_failed",
                    failed_at=time.time(), error={"code": code, "type": type(error).__name__, "message": code})
                browser_worker.save(path, state)
                return browser_worker._preclick_result(self.settings, payload)
            except Exception:
                return {"state": "unknown"}

    def _perform_group_send(self, payload, path, state, authorize_before_click):
        from .monitor_owner_chat import check_owner_chat_now, is_owner_command, prepare_owner_chat
        chat_id, text = payload["chat_id"], payload["text"]
        if is_owner_command(text):
            raise AgentError("MONITOR_CONTROL_REPLY_LOOKS_LIKE_COMMAND")
        with browser_runtime.session(self.settings) as page:
            state["phase"] = "channel"
            prepare_owner_chat(page, self, chat_id)
            state["phase"] = "composer"
            composer = page.locator(_GROUP_COMPOSER)
            if (composer.count() != 1 or not composer.is_visible() or not composer.is_editable()
                    or composer.inner_text().strip()
                    or page.get_by_role("button", name="Отменить загрузку", exact=True).count()):
                raise AgentError("BROWSER_COMPOSER_NOT_EMPTY")
            before = self._group_exact_count(page, text)
            state["phase"] = "composer_fill"
            composer.fill(text)
            state["phase"] = "composer_ready"
            self._wait_group_ready(page, composer, text)
            state["phase"] = "authorization"
            check_owner_chat_now(page, self, chat_id)
            saved = parse_json(path.read_text(encoding="utf-8"))
            if (saved.get("state") != "preparing" or saved.get("send_started") is not False
                    or saved.get("content_hash") != state["content_hash"]
                    or saved.get("started_at") != state["started_at"]):
                raise AgentError("BROWSER_JOURNAL_OWNERSHIP_LOST")
            authorize_before_click()
            check_owner_chat_now(page, self, chat_id)
            if (fingerprint(payload) != state["content_hash"] or composer.inner_text() != text
                    or not composer.evaluate("element => element.isConnected && element.isContentEditable")
                    or page.get_by_role("button", name="Отменить загрузку", exact=True).count()):
                raise AgentError("BROWSER_INTENT_CONTENT_CHANGED")
            button = self._group_send_button(page)
            state.update(state="sending", send_started=True, phase="send_intent", text=text,
                         attachment=False, attachment_type=None, before_count=before)
            browser_worker.save(path, state)
            state["phase"] = "send_click"
            button.click()
            state["phase"] = "receipt_observation"
            deadline = time.monotonic() + self.settings.app["max"]["browser"]["operation_timeout_seconds"]
            while time.monotonic() < deadline:
                if (self._group_exact_count(page, text) == before + 1
                        and not composer.inner_text().strip()):
                    # Preserve the established delay before reloading an optimistic message.
                    page.wait_for_timeout(5000)
                    if self._group_exact_count(page, text) != before + 1:
                        continue
                    page.screenshot(path=str(path.with_suffix(".before-reload.png")))
                    state["phase"] = "receipt_reload"
                    page.reload(wait_until="domcontentloaded")
                    prepare_owner_chat(page, self, chat_id, navigate=False)
                    found = self._group_readback(page, state)
                    if found:
                        page.screenshot(path=str(path.with_suffix(".png")))
                        state.update(state="confirmed", delivery=found)
                        browser_worker.save(path, state)
                        return found
                    break
                page.wait_for_timeout(100)
            state.update(state="unknown", error="BROWSER_SEND_UNCONFIRMED")
            browser_worker.save(path, state)
            return {"state": "unknown"}

    def _reconcile_group(self, chat_id, request_id, state):
        from .monitor_owner_chat import prepare_owner_chat
        if state.get("state") == "confirmed":
            return state["delivery"]
        if state.get("state") in {"preparing", "preclick_failed"}:
            return {"state": "unknown"}
        if type(state.get("before_count")) is not int or not 0 <= state["before_count"] <= 10_000:
            raise AgentError("MONITOR_BROWSER_JOURNAL_INVALID")
        with browser_runtime.session(self.settings) as page:
            prepare_owner_chat(page, self, chat_id)
            found = self._group_readback(page, state)
            if found:
                path = browser_worker.ledger_path(self.settings, request_id)
                page.screenshot(path=str(path.with_suffix(".png")))
                state.update(state="confirmed", delivery=found)
                browser_worker.save(path, state)
                return found
        return {"state": "unknown"}

    def reconcile_report(self, chat_id, request_id, *, text=None):
        """Только сверка прежнего журнала: неизвестный результат не создаёт новый Send."""
        if chat_id not in self.destinations:
            raise AgentError("MONITOR_BROWSER_CHAT_FORBIDDEN")
        worker_id = self._request_id(request_id)
        self._guard(worker_id)
        path = self.settings.root / "data/browser-publications" / (worker_id + ".json")
        if not path.exists():
            return {"state": "unknown"}
        if not path.is_file() or path.stat().st_size > 8_388_608:
            raise AgentError("MONITOR_BROWSER_JOURNAL_INVALID")
        try:
            journal = parse_json(path.read_text(encoding="utf-8"))
            payload = journal["request_payload"]
            if (payload["request_id"] != worker_id or payload["chat_id"] != chat_id
                    or payload["config_hash"] != self.transport_hash
                    or journal["request_id"] != worker_id or journal["chat_id"] != chat_id
                    or journal["content_hash"] != fingerprint(payload)
                    or journal["config_hash"] != self.transport_hash
                    or journal["base_payload"] != payload
                    or journal["base_payload_hash"] != fingerprint(payload)
                    or journal["body_sha256"] != hashlib.sha256(payload["text"].encode("utf-8")).hexdigest()
                    or ("text" in journal and journal["text"] != payload["text"])
                    or payload.get("attachment") is not None
                    or (text is not None and payload["text"] != text)):
                raise ValueError
        except (AgentError, KeyError, TypeError, ValueError, OSError) as exc:
            raise AgentError("MONITOR_BROWSER_JOURNAL_INVALID") from exc
        if self.destinations[chat_id]["kind"] == "solo_group":
            result = self._reconcile_group(chat_id, worker_id, journal)
        else:
            result = browser_worker.reconcile(self.settings, chat_id, worker_id)
        return _readback_result(result)

    def inspect(self):
        self._guard()
        if self.destinations[self.report_chat_id]["kind"] == "solo_group":
            from .monitor_owner_chat import prepare_owner_chat
            with browser_runtime.session(self.settings) as page:
                prepare_owner_chat(page, self, self.report_chat_id)
            result = {"publish": True, "transport": "browser"}
        else:
            result = browser_worker.inspect(self.settings, self.report_chat_id)
        return {**result, "state": "access_only", "live_verified": False,
                "report_delivery_verified": False, "chat_id": self.report_chat_id}

    def login(self, *, timeout=900):
        self._guard()
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise AgentError("MONITOR_BROWSER_OPTIONS_INVALID")
        if self.destinations[self.report_chat_id]["kind"] == "solo_group":
            # login-setup предоставляет ручной вход; inspect затем проверяет solo/account.
            return self.login_setup(timeout=timeout)
        return browser_runtime.login(self.settings, self.report_chat_id, timeout=timeout)

    def login_setup(self, *, timeout=900):
        """Первый вход до создания канала; никакого чтения чужих чатов/отправки."""
        self._guard()
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise AgentError("MONITOR_BROWSER_OPTIONS_INVALID")
        with browser_runtime.session(self.settings, visible=True) as page:
            page.goto(browser_runtime.ORIGIN, wait_until="domcontentloaded")
            page.get_by_role("button", name="Настройки", exact=True).wait_for(timeout=timeout * 1000)
            return {"state": "login_available", "delivery_verified": False,
                    "channel_created": False}

    def create_private_channel(self, *, authorize, recover_chat_id=None):
        """Создание имеет собственный durable журнал, неизвестный Create не повторяется."""
        from .browser_channels import create_channel
        self._guard()
        if self.destinations[self.report_chat_id]["kind"] != "channel":
            raise AgentError("MONITOR_CHANNEL_SETUP_KIND_INVALID")

        def check():
            self._guard()
            approved = callable(authorize) and authorize(self.report_chat_name) is True
            self._guard()
            return approved

        if not check():
            raise AgentError("MONITOR_CHANNEL_NOT_APPROVED")
        return create_channel(self.settings, self.report_chat_name,
            "Закрытый служебный канал мониторинга редакционного проекта.",
            authorize_before_create=check, recover_chat_id=recover_chat_id)

    def inspect_control(self, *, limit=100):
        """Проверка кандидата контракта не включает команды и не выдаёт содержимое чата."""
        if self.options.get("auth_mode") == "authenticated_account_outgoing":
            from .monitor_owner_chat import inspect_owner_chat
            return inspect_owner_chat(self, limit=limit)
        messages = self._read_command_batch(limit=limit, require_accepted=False)
        return {"state": "contract_matches_dom", "message_count": len(messages),
                "dom_contract_sha256": control_contract_hash(self.options["dom_contract"]),
                "commands_enabled": False, "live_verified": False}

    def read_commands(self, *, limit=100):
        """Читаем видимый DOM закрытого ЧАТА; не пишем в редактор и не листаем историю."""
        if self.options.get("auth_mode") == "authenticated_account_outgoing":
            from .monitor_owner_chat import read_owner_commands
            return read_owner_commands(self, limit=limit)
        return self._read_command_batch(limit=limit, require_accepted=True)

    def _read_command_batch(self, *, limit, require_accepted):
        self._guard()
        chat_id, name = _chat(self.options, "control")
        account_id = _identity(self.options.get("account_user_id"), _USER_ID,
                               "MONITOR_CONTROL_ACCOUNT_REQUIRED")
        contract = self.options.get("dom_contract")
        contract_hash = control_contract_hash(contract)
        if require_accepted and (self.options.get("dom_contract_verified") is not True
                or self.options.get("dom_contract_sha256") != contract_hash):
            raise AgentError("MONITOR_CONTROL_CONTRACT_UNVERIFIED")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise AgentError("MONITOR_CONTROL_LIMIT_INVALID")
        address = browser_runtime.ORIGIN + "/" + chat_id
        with browser_runtime.session(self.settings) as page:
            response = page.goto(address, wait_until="domcontentloaded")
            if response is not None and response.status == 429:
                raise AgentError("MONITOR_CONTROL_RATE_LIMITED")
            if response is not None and response.status >= 400:
                raise AgentError("MONITOR_CONTROL_UNAVAILABLE")
            self._guard()
            try:
                raw = page.evaluate(_READ_CONTROL, {"contract": contract, "url": address,
                    "chatId": chat_id, "title": "Окно чата с " + name,
                    "accountId": account_id, "limit": limit})
            except Exception as exc:
                raise AgentError("MONITOR_CONTROL_DOM_INVALID") from exc
        self._guard()
        if not isinstance(raw, list) or len(raw) > limit:
            raise AgentError("MONITOR_CONTROL_DOM_INVALID")
        observed_at = time.time()
        result, identities = [], set()
        for message in raw:
            if (not isinstance(message, dict) or message.get("chat_id") != chat_id
                    or not isinstance(message.get("text"), str) or not message["text"].strip()
                    or len(message["text"]) > 4000 or "\x00" in message["text"]
                    or type(message.get("forwarded")) is not bool):
                raise AgentError("MONITOR_CONTROL_MESSAGE_INVALID")
            ident = _identity(message.get("message_id"), _ID, "MONITOR_CONTROL_MESSAGE_INVALID")
            sender = _identity(message.get("sender_id"), _USER_ID, "MONITOR_CONTROL_SENDER_INVALID")
            reply = message.get("reply_to_message_id")
            if reply is not None:
                _identity(reply, _ID, "MONITOR_CONTROL_REPLY_INVALID")
            if ident in identities:
                raise AgentError("MONITOR_CONTROL_DUPLICATE_MESSAGE")
            identities.add(ident)
            created = _created_at(message.get("timestamp"), contract["timestamp_format"], observed_at)
            result.append({"message_id": ident, "chat_id": chat_id, "sender_id": sender,
                "created_at": created, "text": message["text"],
                "reply_to_message_id": reply, "forwarded": message["forwarded"],
                "observed_at": observed_at, "dom_contract_sha256": contract_hash})
        return result
