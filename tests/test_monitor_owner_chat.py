"""Own-account command mode: explicit identity evidence, no invented numeric sender."""

import copy
import html
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.config import Settings
from max_postiz_agent import browser_runtime
from max_postiz_agent.monitor_browser import MonitorBrowser
from max_postiz_agent.monitor_config import default_config, validate_config
from max_postiz_agent.monitor_command_manifest import OWNER_ACTIONS
from max_postiz_agent.monitor_owner_chat import (
    AUTH_MODE, OWNER_CONTRACT, OWNER_CONTRACT_V1, is_owner_command, owner_contract,
    owner_contract_hash, owner_message_id, phone_fingerprint,
)


PHONE_HASH = phone_fingerprint("+7 (999) 123-45-67")
PRINCIPAL = "max-account-sha256:" + PHONE_HASH
MINUTE = 1_800_000_000


def owner_config():
    value = default_config()
    value.update(enabled=True, report_chat_id="-909", report_chat_name="Отчёты",
                 report_private_confirmed=True)
    value["commands"].update(enabled=True, auth_mode=AUTH_MODE, account_sha256=PHONE_HASH,
        control_chat_id="-908", control_chat_name="Команды", allowed_actions=["status", "reserve", "refill"],
        dom_contract=owner_contract(), dom_contract_verified=True)
    value["commands"]["dom_contract_sha256"] = fingerprint(value["commands"]["dom_contract"])
    return value


def test_phone_fingerprint_normalizes_only_visible_phone_digits():
    assert PHONE_HASH == phone_fingerprint("79991234567") == phone_fingerprint("+7 999 123 45 67")
    assert len(PHONE_HASH) == 64 and "79991234567" not in PHONE_HASH
    assert PHONE_HASH != phone_fingerprint("79991234568")


@pytest.mark.parametrize("phone", ["Телефон +79991234567", "123", "" , "79991234567 PRIVATE", None])
def test_invalid_phone_never_appears_in_error(phone):
    with pytest.raises(AgentError, match="MONITOR_OWNER_ACCOUNT_PHONE_INVALID") as failure:
        phone_fingerprint(phone)
    assert str(failure.value) == "MONITOR_OWNER_ACCOUNT_PHONE_INVALID"


def test_minute_fingerprint_is_explicit_and_preserves_literal_text():
    ident = owner_message_id("-908", PRINCIPAL, MINUTE, "статус")
    assert ident == owner_message_id("-908", PRINCIPAL, MINUTE, "статус")
    assert ident == "owner-" + fingerprint({"contract": "max-owner-chat-event-v1", "chat_id": "-908",
        "principal": PRINCIPAL, "created_at": MINUTE, "text": "статус"})
    assert ident != owner_message_id("-908", PRINCIPAL, MINUTE, "статус ")
    assert ident != owner_message_id("-908", PRINCIPAL, MINUTE + 60, "статус")
    with pytest.raises(AgentError, match="MONITOR_OWNER_EVENT_INVALID"):
        owner_message_id("-908", PRINCIPAL, MINUTE + 1, "статус")


def test_old_config_without_new_fields_retains_numeric_authentication():
    legacy = default_config()
    del legacy["commands"]["auth_mode"]
    del legacy["commands"]["account_sha256"]
    normalized = validate_config(legacy)
    assert normalized == default_config()
    assert "auth_mode" not in legacy["commands"]


def test_outgoing_mode_has_no_requirement_to_fabricate_numeric_owner_ids():
    value = owner_config()
    checked = validate_config(value)
    assert checked["commands"]["owner_ids"] == [] and checked["commands"]["account_user_id"] == ""
    assert checked["commands"]["account_sha256"] == PHONE_HASH


@pytest.mark.parametrize("changed", [{"allowed_actions": ["status", "run_shell"]},
                                     {"account_sha256": ""}, {"account_sha256": "not-a-hash"},
                                     {"auth_mode": []}])
def test_outgoing_mode_never_broadens_to_other_commands_or_invalid_identity(changed):
    value = copy.deepcopy(owner_config())
    value["commands"].update(changed)
    with pytest.raises(AgentError, match="MONITOR_(OWNER_COMMAND_SCOPE_INVALID|COMMAND_CONFIG_INVALID)"):
        validate_config(value)


@pytest.fixture
def reader(project, monkeypatch):
    monkeypatch.delenv("MAX_EXISTING_BROWSER_PROFILE", raising=False)
    contract = owner_contract()
    return MonitorBrowser(Settings.load(project), {"report_chat_id": "-909", "report_chat_name": "Отчёты",
        "control_chat_id": "-908", "control_chat_name": "Команды", "auth_mode": AUTH_MODE,
        "account_sha256": PHONE_HASH, "dom_contract": contract, "dom_contract_verified": True,
        "dom_contract_sha256": owner_contract_hash(contract)})


@pytest.fixture(scope="module")
def owner_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        yield browser
        browser.close()


@pytest.fixture
def page(owner_browser, monkeypatch):
    context = owner_browser.new_context(timezone_id="Europe/Moscow")
    page = context.new_page()

    @contextmanager
    def opened(settings, **kwargs):
        assert settings.root.name == "browser-runtime"
        yield page

    monkeypatch.setattr(browser_runtime, "session", opened)
    yield page
    context.close()


def owner_message(text="статус", *, variant="outgoing", time_text=None, extra="", icon="#icon_status_delivered"):
    time_text = time_text or datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
    return ('<div class="item"><div class="block" role="listitem"><div class="messageWrapper">'
        f'<div data-bubbles-variant="{variant}"><div class="bubble"><div class="bubbleContent">'
        f'<span class="text" data-message-double-click-ignore="">{html.escape(text)}</span>{extra}'
        '<span class="meta"><div class="meta"><span role="button">'
        f'{time_text}</span><div class="indicators"><svg width="16" height="16"><use href="{icon}"></use>'
        '</svg></div></div></span></div></div></div></div><div class="messageControls"></div></div></div>')


def group_html(messages="", *, day="Сегодня", phone="+7 (999) 123-45-67", solo="Тут только вы",
               name="Команды", settings_visible=False):
    date = (f'<div class="item"><div class="capsuleSeparator"><span class="capsule">{day}</span></div></div>'
            if day is not None else "")
    return ('<meta charset="utf-8"><button onclick="document.querySelector(\'.settingsTab\').style.display=\'block\'">'
        'Настройки</button>'
        f'<div class="settingsTab" style="display:{"block" if settings_visible else "none"}">'
        f'<button class="profile"><span class="profileInfo"><span class="phone">{phone}</span></span></button></div>'
        '<h2 id="main-header-title" style="position:absolute;width:1px;height:1px;clip:rect(0,0,0,0);overflow:hidden">'
        f'Окно чата с {name}</h2><button aria-label="Открыть профиль Команды"><span>{name}</span> '
        f'<span>{solo}</span></button><div class="openedChat" style="min-height:30px">{date}{messages}</div>'
        '<div contenteditable role="textbox">Чужой черновик</div>')


def serve(page, content):
    requests = []

    def respond(route):
        requests.append(route.request.url)
        route.fulfill(body=content, content_type="text/html")

    # No test request reaches MAX or another public host.
    page.route("**/*", respond)
    return requests


def test_observed_plain_outgoing_shape_authenticates_without_numeric_sender(reader, page):
    requests = serve(page, group_html(owner_message()))
    events = reader.read_commands()
    assert len(events) == 1
    event = events[0]
    assert event["principal"] == PRINCIPAL and event["account_sha256"] == PHONE_HASH
    assert event["auth_mode"] == AUTH_MODE and "sender_id" not in event
    assert event["outgoing"] is True and event["forwarded"] is False
    assert event["quoted"] is False and event["edited"] is False
    assert event["created_at_precision_seconds"] == 60 and event["created_at"] % 60 == 0
    assert event["message_id"] == owner_message_id("-908", PRINCIPAL, event["created_at"], "статус")
    assert event["id_kind"] == "content_minute_fingerprint" and event["reply_to_message_id"] is None
    assert requests == ["https://web.max.ru/-908", "https://web.max.ru/-908"]
    assert page.locator("[contenteditable]").inner_text() == "Чужой черновик"


def test_visible_settings_identity_is_read_without_toggling_tab(reader, page, monkeypatch):
    serve(page, group_html(owner_message(), settings_visible=True))
    original = page.get_by_role

    def role(*args, **kwargs):
        if kwargs.get("name") == "Настройки":
            pytest.fail("already-visible phone must not toggle settings")
        return original(*args, **kwargs)

    monkeypatch.setattr(page, "get_by_role", role)
    assert len(reader.read_commands()) == 1


def test_incoming_messages_do_not_receive_own_account_authority(reader, page):
    serve(page, group_html(owner_message(variant="incoming")))
    assert reader.read_commands() == []


def test_same_account_and_same_minute_content_is_conservatively_deduplicated(reader, page):
    serve(page, group_html(owner_message() + owner_message()))
    assert len(reader.read_commands()) == 1


@pytest.mark.parametrize("changed,code", [({"solo": "2 участника"}, "MONITOR_OWNER_NOT_SOLO"),
                                       ({"name": "Другая группа"}, "MONITOR_OWNER_WRONG_CHAT"),
                                       ({"phone": "+7 999 123-45-68"}, "MONITOR_OWNER_ACCOUNT_MISMATCH")])
def test_solo_group_and_account_context_cannot_be_substituted(reader, page, changed, code):
    requests = serve(page, group_html(owner_message(), **changed))
    with pytest.raises(AgentError, match=code) as failure:
        reader.read_commands()
    assert "7999" not in str(failure.value) and len(requests) == 1


@pytest.mark.parametrize("extra", [
    '<span class="forwarded">Переслано</span>', '<div class="quote">Цитата</div>',
    '<span aria-label="Изменено"></span>', '<img src="data:image/png;base64,AA==">',
    '<svg><use href="#icon_reply"></use></svg>',
])
def test_forward_quote_edit_media_and_extra_icons_are_not_commands(reader, page, extra):
    serve(page, group_html(owner_message(extra=extra)))
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        reader.read_commands()


def test_caption_with_markup_is_rejected(reader, page):
    content = group_html(owner_message()).replace('data-message-double-click-ignore="">статус</span>',
        'data-message-double-click-ignore=""><b>статус</b></span>')
    serve(page, content)
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        reader.read_commands()


def test_unrecognised_delivery_icon_is_not_a_command(reader, page):
    serve(page, group_html(owner_message(icon="#icon_status_pending")))
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        reader.read_commands()


def test_ambiguous_day_never_becomes_today_and_missing_day_fails_closed(reader, page):
    serve(page, group_html(owner_message(), day="1 ноября"))
    assert reader.read_commands() == []
    page.unroute("**/*")
    serve(page, group_html(owner_message(), day=None))
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        reader.read_commands()


def test_contract_cannot_change_outgoing_selector_to_incoming(reader):
    changed = owner_contract()
    changed["bubble"] = '[data-bubbles-variant="incoming"]'
    with pytest.raises(AgentError, match="MONITOR_OWNER_CONTRACT_INVALID"):
        owner_contract_hash(changed)


def test_identity_acceptance_inspection_does_not_enable_commands_or_return_text(reader, page):
    options = copy.deepcopy(reader.options)
    options.update(dom_contract_verified=False, dom_contract_sha256="")
    unaccepted = MonitorBrowser(reader.source_settings, options)
    serve(page, group_html(owner_message(text="резерв")))
    result = unaccepted.inspect_control()
    assert result["account_verified"] is True and result["commands_enabled"] is False
    assert result["id_kind"] == "content_minute_fingerprint" and result["message_count"] == 1
    assert "резерв" not in str(result) and "+7" not in str(result)
    with pytest.raises(AgentError, match="MONITOR_OWNER_CONTRACT_UNVERIFIED"):
        unaccepted.read_commands()


def test_ui_timezone_must_match_accepted_contract(reader, page):
    options = copy.deepcopy(reader.options)
    options["dom_contract"] = owner_contract(timezone_name="UTC")
    options["dom_contract_sha256"] = owner_contract_hash(options["dom_contract"])
    changed = MonitorBrowser(reader.source_settings, options)
    serve(page, group_html(owner_message()))
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        changed.read_commands()


def test_long_rich_own_reports_are_skipped_without_blocking_plain_commands(reader, page):
    report = owner_message(text="Копия проекта: результат\nРезерв: 6")
    report = report.replace('data-message-double-click-ignore="">Копия проекта: результат\nРезерв: 6</span>',
        'data-message-double-click-ignore="">Копия проекта: результат<br>Резерв: 6</span>')
    serve(page, group_html(report + owner_message(text="статус")))
    assert [event["text"] for event in reader.read_commands()] == ["статус"]


def test_command_filter_matches_shared_gateway_actions():
    from max_postiz_agent.monitor_commands import parse_command

    for text in ("статус", "/СТАТУС", "покажи состояние", "состояние", "резерв", "покажи резерв",
                 "пополни резерв до 6", "пополнить резерв до 12", "пополнить резерв до 0",
                 "ошибки", "пауза", "Копия проекта\nРезерв: 6", "статус" + " " * 1201):
        try:
            parsed = parse_command(text)
            expected = parsed["action"] in OWNER_ACTIONS
        except AgentError:
            expected = False
        assert is_owner_command(text) is expected


def source_message(url="https://example.org/monitor-source/", *, href=None, extra="", class_name="link svelte-q2ybpg"):
    anchor = (f'<a class="{class_name}" href="{html.escape(href or url)}" aria-haspopup="dialog" '
        f'aria-controls="" aria-expanded="false" {extra}>{html.escape(url)}</a>')
    return owner_message(text="добавь источник " + url).replace(
        "добавь источник " + html.escape(url) + "</span>", "добавь источник " + anchor + "</span>")


def test_v2_source_anchor_matches_observed_dom_without_opening_source(reader, page):
    requests = serve(page, group_html(source_message()))
    event = reader.read_commands()[0]
    assert event["text"] == "добавь источник https://example.org/monitor-source/"
    assert event["dom_contract_sha256"] == owner_contract_hash(owner_contract())
    assert reader.options["dom_contract"]["version"] == OWNER_CONTRACT
    assert requests == ["https://web.max.ru/-908", "https://web.max.ru/-908"]


def test_v1_contract_keeps_plain_history_but_rejects_new_anchor_structure(reader, page):
    contract = owner_contract(version=OWNER_CONTRACT_V1)
    options = {**reader.options, "dom_contract": contract, "dom_contract_sha256": owner_contract_hash(contract)}
    legacy = MonitorBrowser(reader.source_settings, options)
    serve(page, group_html(source_message()))
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        legacy.read_commands()
    assert contract["version"] == OWNER_CONTRACT_V1 and "source_anchor" not in contract


@pytest.mark.parametrize("href,extra,class_name", [
    ("https://evil.example/", "", "link svelte-q2ybpg"),
    (None, 'onclick="void(0)"', "link svelte-q2ybpg"),
    (None, 'target="_blank"', "link svelte-q2ybpg"),
    (None, "", "foreign svelte-q2ybpg"),
    (None, "", "link unexpected-scope"),
])
def test_source_anchor_does_not_accept_disguised_href_or_foreign_attributes(reader, page, href, extra, class_name):
    serve(page, group_html(source_message(href=href, extra=extra, class_name=class_name)))
    with pytest.raises(AgentError, match="MONITOR_OWNER_DOM_INVALID"):
        reader.read_commands()


@pytest.mark.parametrize("url", ["https://127.0.0.1/", "https://example.org/?token=value",
                                "https://user@example.org/", "https://@example.org/"])
def test_invalid_source_url_never_becomes_authenticated_source_proposal(reader, page, url):
    requests = serve(page, group_html(source_message(url)))
    assert reader.read_commands() == []
    assert all(address == "https://web.max.ru/-908" for address in requests)


def test_v1_hash_is_not_silent_acceptance_of_v2_structure(reader, monkeypatch):
    legacy = owner_contract(version=OWNER_CONTRACT_V1)
    options = {**reader.options, "dom_contract_sha256": owner_contract_hash(legacy)}
    changed = MonitorBrowser(reader.source_settings, options)
    monkeypatch.setattr(browser_runtime, "session", lambda *args: pytest.fail("unaccepted profile opened"))
    with pytest.raises(AgentError, match="MONITOR_OWNER_CONTRACT_UNVERIFIED"):
        changed.read_commands()


def test_expanded_plain_command_set_is_read_from_same_manifest(reader, page):
    from max_postiz_agent.monitor_command_manifest import classify_command

    texts = ("команды", "ошибки", "источники", "задачи", "задача " + "a" * 32,
        "отмени задачу " + "b" * 32, "диагностика", "продолжи подготовку", "продолжи проект",
        "пауза подготовки", "сверь dispatch-1", "сними блокировку max_test",
        "измени фильтр ключевые слова: звёзды, галактики", "очисти фильтр ключевые слова",
        "очисти фильтр авторы", "подтверждаю " + "c" * 32)
    serve(page, group_html("".join(owner_message(text=text) for text in texts)))
    observed = reader.read_commands()
    assert [event["text"] for event in observed] == list(texts)
    assert all(classify_command(event["text"]) in OWNER_ACTIONS for event in observed)
