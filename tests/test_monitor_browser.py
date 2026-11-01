"""Offline browser contracts: no MAX account, model call or production network."""

import copy
import html
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from max_postiz_agent import browser_runtime, browser_worker
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.monitor_browser import (
    CONTROL_CONTRACT, MonitorBrowser, control_contract_hash, control_contract_template,
)


REPORT = "-909"
CONTROL = "-908"
CONTRACT = {
    "version": CONTROL_CONTRACT, "chat_root": "#chat", "chat_id_attribute": "data-chat-id",
    "chat_kind_attribute": "data-chat-kind", "header": "#main-header-title",
    "account": "#account", "account_id_attribute": "data-user-id", "messages": ".message",
    "message_id_attribute": "data-message-id", "sender": ".sender",
    "sender_id_attribute": "data-user-id", "time": "time", "time_attribute": "datetime",
    "timestamp_format": "iso8601", "text": ".text", "reply": ".reply",
    "reply_id_attribute": "data-reply-id", "forwarded": ".forwarded",
}


@pytest.fixture
def monitor(project, monkeypatch):
    monkeypatch.delenv("MAX_EXISTING_BROWSER_PROFILE", raising=False)
    options = {"report_chat_id": REPORT, "report_chat_name": "Мониторинг",
        "control_chat_id": CONTROL, "control_chat_name": "Управление",
        "account_user_id": "100", "dom_contract": copy.deepcopy(CONTRACT),
        "dom_contract_verified": True, "dom_contract_sha256": control_contract_hash(CONTRACT),
        "operation_timeout_seconds": 1}
    return MonitorBrowser(Settings.load(project), options)


@pytest.fixture(scope="module")
def fixture_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        yield browser
        browser.close()


@pytest.fixture
def dom_page(fixture_browser, monkeypatch):
    context = fixture_browser.new_context()
    page = context.new_page()
    page.set_default_timeout(1000)

    @contextmanager
    def opened(settings, **kwargs):
        assert settings.app["max"]["browser"]["profile_dir"] == "data/monitor-browser"
        assert settings.root.parts[-2:] == ("monitor", "browser-runtime")
        yield page

    monkeypatch.setattr(browser_runtime, "session", opened)
    monkeypatch.setattr(browser_worker, "session", opened)
    yield page
    context.close()


def serve(page, content, status=200):
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(body=content, status=status, content_type="text/html")

    # Every browser request is intercepted; nothing reaches the public service.
    page.route("**/*", respond)
    return requests


def message(ident="msg-1", *, sender="200", text="статус", timestamp=None, reply=None, forwarded=False):
    timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    reply_html = f'<span class="reply" data-reply-id="{html.escape(reply)}">Цитата</span>' if reply else ""
    return (f'<article class="message" data-message-id="{html.escape(ident)}">'
        f'<span class="sender" data-user-id="{html.escape(sender)}">Владелец</span>'
        f'<time datetime="{html.escape(timestamp)}">Сегодня</time>'
        f'<span class="text">{html.escape(text)}</span>{reply_html}'
        f'{"<span class=forwarded>Переслано</span>" if forwarded else ""}</article>')


def control_html(messages="", *, chat_id=CONTROL, kind="chat", account="100", name="Управление"):
    return (f'<meta charset="utf-8"><h2 id="main-header-title">Окно чата с {name}</h2>'
        f'<div id="account" data-user-id="{account}">Аккаунт</div>'
        f'<div id="chat" data-chat-id="{chat_id}" data-chat-kind="{kind}">{messages}</div>'
        '<div contenteditable role="textbox">Несохранённый черновик владельца</div>'
        '<button onclick="window.sendCalls++">Отправить</button><script>window.sendCalls=0;</script>')


def report_html():
    return '''<meta charset="utf-8"><h2 id="main-header-title">Окно чата с Мониторинг</h2>
    <div id="posts"></div><div contenteditable role="textbox" aria-placeholder="Пост" style="min-height:24px"></div>
    <button aria-label="Отправить сообщение" onclick="send()">Send</button>
    <script>
    function render(){ document.querySelector('#posts').innerHTML='';
      for(const text of JSON.parse(localStorage.getItem('fixture_posts')||'[]')){
        const item=document.createElement('div');item.setAttribute('role','listitem');
        const body=document.createElement('span');body.textContent=text;item.append(body);
        document.querySelector('#posts').append(item);}}
    function send(){const editor=document.querySelector('[contenteditable]');
      const posts=JSON.parse(localStorage.getItem('fixture_posts')||'[]');
      posts.push(editor.innerText);localStorage.setItem('fixture_posts',JSON.stringify(posts));
      editor.textContent='';render();}render();
    </script>'''


def test_settings_and_profile_are_separate_from_editorial(monitor):
    assert monitor.settings.root == monitor.project_root / "data/monitor/browser-runtime"
    assert monitor.settings.app["max"]["browser"]["profile_dir"] == "data/monitor-browser"
    assert monitor.settings.app["max"]["browser"]["channels"] == {REPORT: "Мониторинг"}
    assert monitor.source_settings.hash == monitor.source_hash
    assert monitor.settings.app is not monitor.source_settings.app
    monitor.settings.app["max"]["browser"]["channels"][REPORT] = "Чужой"
    with pytest.raises(AgentError, match="MONITOR_BROWSER_CONFIG_CHANGED"):
        monitor.inspect()


def test_external_profile_is_never_reused_even_if_set_after_construction(monitor, monkeypatch):
    monkeypatch.setenv("MAX_EXISTING_BROWSER_PROFILE", "data/max-browser")
    monkeypatch.setattr(browser_worker, "inspect", lambda *args: pytest.fail("browser must not open"))
    with pytest.raises(AgentError, match="MONITOR_EXTERNAL_BROWSER_FORBIDDEN"):
        monitor.inspect()
    with pytest.raises(AgentError, match="MONITOR_EXTERNAL_BROWSER_FORBIDDEN"):
        MonitorBrowser(monitor.source_settings, monitor.options)


def test_runtime_reparse_component_is_rejected(monitor, monkeypatch):
    from pathlib import Path

    original = Path.is_junction
    suspect = monitor.project_root / "data/monitor"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == suspect or original(path))
    with pytest.raises(AgentError, match="MONITOR_BROWSER_LINK_FORBIDDEN"):
        monitor.inspect()


def test_source_config_change_blocks_transport_before_session(monitor):
    monitor.source_settings.topic["channels"][0]["name"] = "Изменено"
    with pytest.raises(AgentError, match="MONITOR_BROWSER_CONFIG_CHANGED"):
        monitor.inspect()


def test_monitor_journal_cannot_alias_another_private_file(monitor, tmp_path):
    payload = monitor._report_request(REPORT, "Отчёт", "hardlink")
    path = browser_worker.ledger_path(monitor.settings, payload["request_id"])
    other = tmp_path / "other-private.json"
    other.write_text("{}", encoding="utf-8")
    os.link(other, path)
    with pytest.raises(AgentError, match="MONITOR_BROWSER_LINK_FORBIDDEN"):
        monitor.send_report(REPORT, "Отчёт", "hardlink", authorize=lambda _: True)


def test_authorization_callback_cannot_redirect_runtime_root(monitor, monkeypatch):
    monkeypatch.setattr(browser_worker, "publish", lambda *args, **kwargs: pytest.fail("send attempted"))

    def changed(envelope):
        monitor.settings.root = monitor.project_root
        return True

    with pytest.raises(AgentError, match="MONITOR_BROWSER_CONFIG_CHANGED"):
        monitor.send_report(REPORT, "Отчёт", "root-change", authorize=changed)


@pytest.mark.parametrize("chat_id,text,request_id,code", [
    ("-123", "Статус", "report-1", "CHAT_FORBIDDEN"),
    (REPORT, " ", "report-1", "TEXT_INVALID"),
    (REPORT, "a" * 4001, "report-1", "TEXT_INVALID"),
    (REPORT, "Статус\x00", "report-1", "TEXT_INVALID"),
    (REPORT, "Статус", "../outside", "REQUEST_INVALID"),
])
def test_report_request_is_bounded_before_authorization(monitor, chat_id, text, request_id, code):
    with pytest.raises(AgentError, match="MONITOR_BROWSER_" + code):
        monitor.send_report(chat_id, text, request_id, authorize=lambda _: pytest.fail("not authorized"))


@pytest.mark.parametrize("authorize", [None, lambda _: False, lambda _: None])
def test_report_requires_positive_explicit_authorization(monitor, monkeypatch, authorize):
    monkeypatch.setattr(browser_worker, "publish", lambda *args, **kwargs: pytest.fail("send attempted"))
    with pytest.raises(AgentError, match="MONITOR_REPORT_(AUTHORIZATION_REQUIRED|NOT_APPROVED)"):
        monitor.send_report(REPORT, "Статус", "report-1", authorize=authorize)


def test_real_worker_sends_once_and_verifies_report_after_reload(monitor, dom_page):
    requests = serve(dom_page, report_html())
    approvals = []

    def authorize(envelope):
        approvals.append(envelope)
        assert envelope["chat_id"] == REPORT and envelope["text"] == "Резерв: 6"
        assert envelope["config_hash"] == monitor.transport_hash
        assert envelope["monitor_config_hash"] == fingerprint(monitor.options)
        return True

    result = monitor.send_report(REPORT, "Резерв: 6", "hour-1", authorize=authorize)
    assert result["state"] == "confirmed"
    assert result["verification"] == "browser_readback" and result["independent_readback"] is True
    assert len(approvals) == 2
    assert requests == ["https://web.max.ru/" + REPORT] * 2
    assert dom_page.get_by_role("listitem").count() == 1
    assert monitor.send_report(REPORT, "Резерв: 6", "hour-1", authorize=authorize) == result
    assert len(requests) == 2 and dom_page.get_by_role("listitem").count() == 1
    assert monitor.reconcile_report(REPORT, "hour-1", text="Резерв: 6") == result
    with pytest.raises(AgentError, match="MONITOR_BROWSER_JOURNAL_INVALID"):
        monitor.reconcile_report(REPORT, "hour-1", text="Изменённый текст")


def test_revoked_final_approval_never_clicks_send(monitor, dom_page):
    requests = serve(dom_page, report_html())
    decisions = iter((True, False))
    result = monitor.send_report(REPORT, "Статус", "revoked", authorize=lambda _: next(decisions))
    assert result["state"] == "preclick_failed"
    assert dom_page.get_by_role("listitem").count() == 0
    assert requests == ["https://web.max.ru/" + REPORT]


def test_unknown_report_never_reopens_browser_or_repeats_send(monitor, monkeypatch):
    payload = monitor._report_request(REPORT, "Статус", "unknown-1")
    path = browser_worker.ledger_path(monitor.settings, payload["request_id"])
    state = {**browser_worker._request_record(monitor.settings, payload),
             "state": "unknown", "text": payload["text"], "send_started": True}
    browser_worker.save(path, state)
    monkeypatch.setattr(browser_worker, "session", lambda *args: pytest.fail("browser reopened"))
    assert monitor.send_report(REPORT, "Статус", "unknown-1", authorize=lambda _: True) == {"state": "unknown"}
    with pytest.raises(AgentError, match="BROWSER_INTENT_CONTENT_CHANGED"):
        monitor.send_report(REPORT, "Новый статус", "unknown-1", authorize=lambda _: True)


def test_reconcile_unknown_does_not_create_missing_journal(monitor):
    assert monitor.reconcile_report(REPORT, "missing") == {"state": "unknown"}
    assert not (monitor.settings.root / "data/browser-publications").exists()


def test_reconcile_rejects_changed_top_level_text_before_browser(monitor, monkeypatch):
    payload = monitor._report_request(REPORT, "Точный отчёт", "changed-proof")
    path = browser_worker.ledger_path(monitor.settings, payload["request_id"])
    state = {**browser_worker._request_record(monitor.settings, payload),
             "state": "unknown", "text": "Другой отчёт", "send_started": True}
    browser_worker.save(path, state)
    monkeypatch.setattr(browser_worker, "reconcile", lambda *args: pytest.fail("invalid proof read"))
    with pytest.raises(AgentError, match="MONITOR_BROWSER_JOURNAL_INVALID"):
        monitor.reconcile_report(REPORT, "changed-proof")


def test_template_is_only_an_unconfigured_shape():
    template = control_contract_template()
    assert template["version"] == CONTROL_CONTRACT
    assert set(template) == set(CONTRACT)
    with pytest.raises(AgentError, match="MONITOR_CONTROL_CONTRACT_INVALID"):
        control_contract_hash(template)


def test_inspect_is_access_evidence_and_login_uses_monitor_profile(monitor, monkeypatch):
    monkeypatch.setattr(browser_worker, "inspect", lambda settings, chat: {"publish": True, "live_verified": True})
    result = monitor.inspect()
    assert result["state"] == "access_only" and result["live_verified"] is False
    assert result["report_delivery_verified"] is False

    def login(settings, chat, *, timeout):
        assert settings.root == monitor.settings.root and chat == REPORT and timeout == 30
        return {"state": "available", "chat_id": chat}

    monkeypatch.setattr(browser_runtime, "login", login)
    assert monitor.login(timeout=30)["state"] == "available"


@pytest.mark.parametrize("change", [
    {"dom_contract_verified": False}, {"dom_contract_sha256": "0" * 64},
])
def test_command_receive_requires_accepted_exact_contract(monitor, monkeypatch, change):
    options = {**monitor.options, **change}
    reader = MonitorBrowser(monitor.source_settings, options)
    monkeypatch.setattr(browser_runtime, "session", lambda *args: pytest.fail("browser opened"))
    with pytest.raises(AgentError, match="MONITOR_CONTROL_CONTRACT_UNVERIFIED"):
        reader.read_commands()


def test_missing_contract_field_disables_receive(monitor):
    options = copy.deepcopy(monitor.options)
    del options["dom_contract"]["sender_id_attribute"]
    reader = MonitorBrowser(monitor.source_settings, options)
    with pytest.raises(AgentError, match="MONITOR_CONTROL_CONTRACT_INVALID"):
        reader.read_commands()


def test_public_dom_read_preserves_exact_command_identity_reply_and_draft(monitor, dom_page):
    requests = serve(dom_page, control_html(message(text="пополнить до 6", reply="report-77") +
        message("msg-2", sender="201", text="источники", forwarded=True)))
    events = monitor.read_commands()
    assert events[0]["message_id"] == "msg-1" and events[0]["sender_id"] == "200"
    assert events[0]["chat_id"] == CONTROL and events[0]["text"] == "пополнить до 6"
    assert events[0]["reply_to_message_id"] == "report-77"
    assert events[0]["created_at"] > 0 and events[0]["forwarded"] is False
    assert events[1]["forwarded"] is True
    assert requests == ["https://web.max.ru/" + CONTROL]
    assert dom_page.locator("[contenteditable]").inner_text() == "Несохранённый черновик владельца"
    assert dom_page.evaluate("window.sendCalls") == 0


@pytest.mark.parametrize("options", [
    {"kind": "channel"}, {"chat_id": "-123"}, {"account": "999"}, {"name": "Другой чат"},
])
def test_changed_chat_kind_account_or_title_blocks_whole_batch(monitor, dom_page, options):
    requests = serve(dom_page, control_html(message(), **options))
    with pytest.raises(AgentError, match="MONITOR_CONTROL_DOM_INVALID"):
        monitor.read_commands()
    assert requests == ["https://web.max.ru/" + CONTROL]
    assert dom_page.evaluate("window.sendCalls") == 0


@pytest.mark.parametrize("content,code", [
    (message() + message(), "DUPLICATE_MESSAGE"),
    (message(sender="username"), "SENDER_INVALID"),
    (message(timestamp="01:00"), "TIMESTAMP_INVALID"),
    (message(timestamp="2026-11-01T01:00:00"), "TIMESTAMP_INVALID"),
    (message(timestamp="9999-12-31T00:00:00+00:00"), "TIMESTAMP_INVALID"),
    (message(reply="invalid/../id"), "REPLY_INVALID"),
    (message(ident="../id"), "MESSAGE_INVALID"),
])
def test_unstable_identity_time_or_reply_is_not_exported(monitor, dom_page, content, code):
    serve(dom_page, control_html(content))
    with pytest.raises(AgentError, match="MONITOR_CONTROL_" + code):
        monitor.read_commands()


@pytest.mark.parametrize("content", [
    message().replace('<span class="sender"', '<span class="sender" data-user-id="200">Duplicate</span><span class="sender"'),
    message().replace('class="text"', 'class="text" style="display:none"'),
    message().replace('class="message"', 'class="message" style="display:none"'),
    message() + '<div id="chat" data-chat-id="-908" data-chat-kind="chat"></div>',
])
def test_ambiguous_or_hidden_dom_fails_closed(monitor, dom_page, content):
    serve(dom_page, control_html(content))
    with pytest.raises(AgentError, match="MONITOR_CONTROL_DOM_INVALID"):
        monitor.read_commands()


def test_control_limit_is_not_silent_partial_history(monitor, dom_page):
    serve(dom_page, control_html(message() + message("msg-2")))
    with pytest.raises(AgentError, match="MONITOR_CONTROL_DOM_INVALID"):
        monitor.read_commands(limit=1)


@pytest.mark.parametrize("status,code", [(429, "RATE_LIMITED"), (403, "UNAVAILABLE"), (503, "UNAVAILABLE")])
def test_rate_limit_and_unavailable_chat_have_no_retry(monitor, dom_page, status, code):
    requests = serve(dom_page, control_html(message()), status=status)
    with pytest.raises(AgentError, match="MONITOR_CONTROL_" + code):
        monitor.read_commands()
    assert len(requests) == 1


def test_control_acceptance_inspection_discloses_no_messages_and_does_not_enable(monitor, dom_page):
    options = {**monitor.options, "dom_contract_verified": False, "dom_contract_sha256": ""}
    reader = MonitorBrowser(monitor.source_settings, options)
    serve(dom_page, control_html(message(text="приватная команда")))
    result = reader.inspect_control()
    assert result == {"state": "contract_matches_dom", "message_count": 1,
        "dom_contract_sha256": control_contract_hash(CONTRACT),
        "commands_enabled": False, "live_verified": False}
    assert "приватная" not in str(result)
    with pytest.raises(AgentError, match="MONITOR_CONTROL_CONTRACT_UNVERIFIED"):
        reader.read_commands()


@pytest.mark.parametrize("format_name,value", [("unix_seconds", "1577836800"),
                                              ("unix_milliseconds", "1577836800000")])
def test_absolute_numeric_dom_timestamps_are_supported(monitor, dom_page, format_name, value):
    options = copy.deepcopy(monitor.options)
    options["dom_contract"]["timestamp_format"] = format_name
    options["dom_contract_sha256"] = control_contract_hash(options["dom_contract"])
    reader = MonitorBrowser(monitor.source_settings, options)
    serve(dom_page, control_html(message(timestamp=value)))
    assert reader.read_commands()[0]["created_at"] == 1577836800


@pytest.fixture
def group_monitor(project, monkeypatch):
    from max_postiz_agent.monitor_owner_chat import owner_contract, owner_contract_hash, phone_fingerprint

    monkeypatch.delenv("MAX_EXISTING_BROWSER_PROFILE", raising=False)
    contract = owner_contract()
    options = {"report_chat_id": REPORT, "report_chat_name": "Мониторинг",
        "control_chat_id": CONTROL, "control_chat_name": "Управление",
        "reply_in_control_chat": True, "auth_mode": "authenticated_account_outgoing",
        "account_sha256": phone_fingerprint("+7 999 123 45 67"),
        "dom_contract": contract, "dom_contract_verified": True,
        "dom_contract_sha256": owner_contract_hash(contract), "operation_timeout_seconds": 1}
    return MonitorBrowser(Settings.load(project), options)


@pytest.fixture
def group_page(fixture_browser, monkeypatch):
    context = fixture_browser.new_context(timezone_id="Europe/Moscow")
    page = context.new_page()

    @contextmanager
    def opened(settings, **kwargs):
        assert settings.root.name == "browser-runtime"
        yield page

    monkeypatch.setattr(browser_runtime, "session", opened)
    yield page
    context.close()


def single_group(monitor):
    options = {**monitor.options, "report_chat_id": CONTROL, "report_chat_name": "Управление"}
    return MonitorBrowser(monitor.source_settings, options)


def group_send_html(*, phone="+7 999 123 45 67", solo="Тут только вы", draft=""):
    return f'''<meta charset="utf-8"><style>.bubbleContent > .text {{white-space:pre-wrap}}</style>
    <button onclick="document.querySelector('.settingsTab').hidden=false">Настройки</button>
    <div class="settingsTab" hidden><button class="profile"><span class="profileInfo"><span class="phone">{phone}</span></span></button></div>
    <h2 id="main-header-title">Окно чата с Управление</h2>
    <button id="group-profile" aria-label="Открыть профиль Управление">Управление {solo}</button>
    <div class="openedChat"><div class="capsuleSeparator"><span class="capsule">Сегодня</span></div><div id="posts"></div></div>
    <div contenteditable role="textbox" aria-placeholder="Сообщение" style="min-height:24px;white-space:pre-wrap">{draft}</div>
    <button aria-label="Отправить Enter" onclick="send()">Send</button>
    <script>
    function render(){{document.querySelector('#posts').innerHTML='';
      for(const text of JSON.parse(localStorage.getItem('fixture_group_posts')||'[]')){{
        const item=document.createElement('div');item.setAttribute('role','listitem');item.className='block';
        const bubble=document.createElement('div');bubble.setAttribute('data-bubbles-variant','outgoing');
        const content=document.createElement('div');content.className='bubbleContent';
        const caption=document.createElement('span');caption.className='text';caption.setAttribute('data-message-double-click-ignore','');caption.textContent=text;
        const meta=document.createElement('span');meta.className='meta';
        const minute=document.createElement('span');minute.setAttribute('role','button');
        minute.textContent=' '+new Intl.DateTimeFormat('en-GB',{{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'}}).format(new Date())+' ';
        meta.append(minute);const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
        const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#icon_status_delivered');svg.append(use);meta.append(svg);
        content.append(caption,meta);bubble.append(content);item.append(bubble);document.querySelector('#posts').append(item);
      }}
    }}
    function send(){{const editor=document.querySelector('[contenteditable]');
      const posts=JSON.parse(localStorage.getItem('fixture_group_posts')||'[]');posts.push(editor.innerText);
      localStorage.setItem('fixture_group_posts',JSON.stringify(posts));
      localStorage.setItem('fixture_send_count',String(Number(localStorage.getItem('fixture_send_count')||0)+1));
      editor.textContent='';render();}}render();
    </script>'''


def test_exact_dual_destination_map_and_group_kind_change_configuration_hash(group_monitor):
    assert group_monitor.destinations == {REPORT: {"name": "Мониторинг", "kind": "channel"},
        CONTROL: {"name": "Управление", "kind": "solo_group"}}
    assert group_monitor.settings.app["max"]["browser"]["channels"][CONTROL] == "Управление"
    assert single_group(group_monitor).transport_hash != group_monitor.transport_hash
    with pytest.raises(AgentError, match="MONITOR_CONTROL_GROUP_ROUTE_REQUIRED"):
        MonitorBrowser(group_monitor.source_settings, {**group_monitor.options, "reply_in_control_chat": False,
            "report_chat_id": CONTROL, "report_chat_name": "Управление"})


def test_single_group_long_report_sends_once_and_does_not_become_a_command(group_monitor, group_page):
    monitor = single_group(group_monitor)
    requests = serve(group_page, group_send_html())
    body = "Копия проекта\nЗадача принята.\nРезерв: 0; цель: 6.\nПроверки сохранены."
    approvals = []

    def approve(envelope):
        approvals.append(envelope)
        assert envelope["chat_id"] == CONTROL and envelope["chat_name"] == "Управление"
        assert envelope["destination_kind"] == "solo_group" and envelope["text"] == body
        return True

    result = monitor.send_report(CONTROL, body, "single-group-report", authorize=approve)
    assert result["state"] == "confirmed" and result["independent_readback"] is True
    assert len(approvals) == 2 and len(requests) == 4
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") == "1"
    assert monitor.send_report(CONTROL, body, "single-group-report", authorize=approve) == result
    assert len(requests) == 4
    assert monitor.reconcile_report(CONTROL, "single-group-report", text=body) == result
    assert monitor.read_commands() == []
    group_page.evaluate("""() => {const posts=JSON.parse(localStorage.getItem('fixture_group_posts'));
      posts.push('статус');localStorage.setItem('fixture_group_posts',JSON.stringify(posts));render();}""")
    assert [event["text"] for event in monitor.read_commands()] == ["статус"]
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") == "1"


@pytest.mark.parametrize("options,code", [({"phone": "+7 999 123 45 68"}, "MONITOR_OWNER_ACCOUNT_MISMATCH"),
                                         ({"solo": "2 участника"}, "MONITOR_OWNER_NOT_SOLO"),
                                         ({"draft": "Чужой черновик"}, "BROWSER_COMPOSER_NOT_EMPTY")])
def test_group_send_requires_account_solo_and_empty_composer(group_monitor, group_page, options, code):
    monitor = single_group(group_monitor)
    serve(group_page, group_send_html(**options))
    result = monitor.send_report(CONTROL, "Копия: ответ задачи", "blocked-group", authorize=lambda _: True)
    assert result["state"] == "preclick_failed"
    worker_id = monitor._request_id("blocked-group")
    proof = browser_worker.read_preclick_failure(monitor.settings, worker_id, chat_id=CONTROL,
        text="Копия: ответ задачи", attachment=None)["proof"]
    assert proof["error"]["code"] == code and proof["send_started"] is False
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") is None
    if options.get("draft"):
        assert group_page.locator("[contenteditable]").inner_text() == options["draft"]


def test_group_authority_is_rechecked_before_the_only_click(group_monitor, group_page):
    monitor = single_group(group_monitor)
    serve(group_page, group_send_html())
    decisions = iter((True, False))
    result = monitor.send_report(CONTROL, "Копия: ответ", "revoked-group", authorize=lambda _: next(decisions))
    assert result["state"] == "preclick_failed"
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") is None


def test_group_context_changed_by_final_authorizer_never_clicks(group_monitor, group_page):
    monitor = single_group(group_monitor)
    serve(group_page, group_send_html())
    count = 0

    def approve(envelope):
        nonlocal count
        count += 1
        if count == 2:
            group_page.locator("#group-profile").evaluate("node => node.textContent='Управление 2 участника'")
        return True

    assert monitor.send_report(CONTROL, "Копия: ответ", "changed-group", authorize=approve)["state"] == "preclick_failed"
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") is None


def test_unknown_group_send_is_not_repeated_and_reconciles_without_send(group_monitor, group_page, monkeypatch):
    monitor = single_group(group_monitor)
    requests = serve(group_page, group_send_html())
    readback = monitor._group_readback
    monkeypatch.setattr(monitor, "_group_readback", lambda *args: None)
    body = "Копия: результат пока неизвестен"
    assert monitor.send_report(CONTROL, body, "unknown-group", authorize=lambda _: True) == {"state": "unknown"}
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") == "1"
    assert monitor.send_report(CONTROL, body, "unknown-group", authorize=lambda _: True) == {"state": "unknown"}
    assert len(requests) == 4
    monkeypatch.setattr(monitor, "_group_readback", readback)
    assert monitor.reconcile_report(CONTROL, "unknown-group", text=body)["independent_readback"] is True
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") == "1"


@pytest.mark.parametrize("body", ["статус", "команды", "пауза", "продолжи проект",
                                  "добавь источник https://example.org/", "подтверждаю " + "a" * 32])
def test_group_transport_never_sends_a_bare_command_as_its_own_reply(group_monitor, monkeypatch, body):
    monitor = single_group(group_monitor)
    monkeypatch.setattr(browser_runtime, "session", lambda *args: pytest.fail("browser must not open"))
    result = monitor.send_report(CONTROL, body, "would-echo", authorize=lambda _: True)
    assert result["state"] == "preclick_failed"
    proof = browser_worker.read_preclick_failure(monitor.settings, monitor._request_id("would-echo"),
        chat_id=CONTROL, text=body, attachment=None)["proof"]
    assert proof["error"]["code"] == "MONITOR_CONTROL_REPLY_LOOKS_LIKE_COMMAND"


def test_group_readback_requires_literal_text_not_locator_whitespace_normalization(group_monitor, group_page):
    monitor = single_group(group_monitor)
    content = group_send_html().replace('caption.textContent=text;',
        "caption.textContent=text.replaceAll('  ',' ');")
    serve(group_page, content)
    body = "Копия: точный  результат"
    assert monitor.send_report(CONTROL, body, "whitespace-group", authorize=lambda _: True) == {"state": "unknown"}
    assert group_page.evaluate("localStorage.getItem('fixture_send_count')") == "1"
    # The existing normalized locator would find it; literal group proof refuses it.
    assert browser_runtime.matching_posts(group_page, body).count() == 1
    assert monitor._group_exact_count(group_page, body) == 0
