"""A delayed MAX DOM never authorizes another channel or an extra request."""
import time

import pytest

from max_postiz_agent import browser_runtime
from max_postiz_agent.config import AgentError
from test_browser_publishing import browser_config

pytest_plugins = ["test_browser_publishing"]


@pytest.fixture
def channel(project, browser_page, monkeypatch):
    settings = browser_config(project)
    browser_page.set_default_timeout(40)
    requests = []
    browser_page.on("request", lambda request: requests.append(request.url))
    return settings, browser_page, requests


def html(*, heading="Окно чата с Тест", editor=True, script="", editor_attributes=""):
    return f'''<meta charset="utf-8"><h2 id="main-header-title">{heading}</h2>
    {f'<div contenteditable role="textbox" aria-placeholder="Пост" {editor_attributes}>Чужой черновик</div>' if editor else ''}
    <button onclick="window.sendCalls++">Отправить</button>
    <script>window.sendCalls=0;{script}</script>'''


def route(page, content, **options):
    page.route("https://web.max.ru/**", lambda request: request.fulfill(
        body=content, content_type="text/html", **options))


def unchanged(page, requests):
    assert requests == ["https://web.max.ru/-123"]
    assert page.evaluate("window.sendCalls") == 0
    editor = page.locator(browser_runtime.COMPOSER)
    if editor.count():
        assert editor.inner_text(timeout=1000) == "Чужой черновик"


@pytest.mark.parametrize("initial", ["", "Окно чата с Тест старый"])
def test_delayed_exact_heading_survives_old_default_timeout_without_second_get(channel, initial):
    settings, page, requests = channel
    route(page, html(heading=initial, script="""
      setTimeout(() => document.querySelector('h2').innerText='Окно чата с Тест', 160);
    """))
    browser_runtime.ensure_channel(page, settings, "-123")
    unchanged(page, requests)


def test_delayed_editor_uses_same_bounded_readiness_budget(channel):
    settings, page, requests = channel
    route(page, html(editor_attributes='style="display:none"', script="""
      setTimeout(() => document.querySelector('[contenteditable]').style.display='block', 160);
    """))
    browser_runtime.ensure_channel(page, settings, "-123")
    unchanged(page, requests)


@pytest.mark.parametrize("content,code", [
    (html(heading="", editor=False, script="document.body.insertAdjacentHTML('beforeend','<div data-testid=qr-login>Войти по QR</div>')"),
     "BROWSER_LOGIN_OR_CHANNEL_REQUIRED"),
    (html(heading="Окно чата с Другой"), "BROWSER_WRONG_CHANNEL"),
    (html(editor=False), "BROWSER_PUBLISH_PERMISSION_REQUIRED"),
    (html(editor_attributes='style="display:none"'), "BROWSER_PUBLISH_PERMISSION_REQUIRED"),
    (html(editor_attributes='aria-disabled="true"'), "BROWSER_PUBLISH_PERMISSION_REQUIRED"),
    (html(script="document.querySelector('[contenteditable]').contentEditable='false'"),
     "BROWSER_PUBLISH_PERMISSION_REQUIRED"),
    (html(script="document.body.insertAdjacentHTML('afterbegin','<h2 id=main-header-title>Окно чата с Тест</h2>')"),
     "BROWSER_LOGIN_OR_CHANNEL_REQUIRED"),
])
def test_login_wrong_heading_missing_permission_or_ambiguous_header_fail_without_retry(channel, monkeypatch, content, code):
    settings, page, requests = channel
    monkeypatch.setattr(browser_runtime, "_CHANNEL_READY_TIMEOUT_MS", 150)
    route(page, content)
    started = time.monotonic()
    with pytest.raises(AgentError, match="^" + code + "$"):
        browser_runtime.ensure_channel(page, settings, "-123")
    assert time.monotonic() - started < 2
    unchanged(page, requests)


@pytest.mark.parametrize("address", [
    "http://web.max.ru/-123", "https://other.invalid/-123", "https://web.max.ru/-456",
    "https://web.max.ru/-123?other=1", "https://web.max.ru/-123#other",
])
@pytest.mark.parametrize("require_publish", [True, False])
def test_noncanonical_address_is_rejected_before_any_dom_wait_or_navigation(
        channel, address, require_publish):
    settings, page, requests = channel
    page.route("**/*", lambda request: request.fulfill(body=html(), content_type="text/html"))
    page.goto(address, timeout=30_000)
    before = list(requests)
    with pytest.raises(AgentError, match="^BROWSER_WRONG_CHANNEL$"):
        browser_runtime.ensure_channel(page, settings, "-123", navigate=False,
                                       require_publish=require_publish)
    assert requests == before and page.evaluate("window.sendCalls") == 0


@pytest.mark.parametrize("status,code", [(429, "BROWSER_CHANNEL_RATE_LIMITED"),
                                        (403, "BROWSER_CHANNEL_UNAVAILABLE"),
                                        (503, "BROWSER_CHANNEL_UNAVAILABLE")])
@pytest.mark.parametrize("require_publish", [True, False])
def test_http_rate_limit_or_channel_error_never_reloads_or_retries(
        channel, status, code, require_publish):
    settings, page, requests = channel
    route(page, html(), status=status)
    with pytest.raises(AgentError, match="^" + code + "$"):
        browser_runtime.ensure_channel(page, settings, "-123", require_publish=require_publish)
    unchanged(page, requests)


def test_route_change_during_wait_is_terminal_without_return_navigation(channel):
    settings, page, requests = channel
    route(page, html(heading="", script="setTimeout(() => history.replaceState(null,'','/-456'), 100)"))
    with pytest.raises(AgentError, match="^BROWSER_WRONG_CHANNEL$"):
        browser_runtime.ensure_channel(page, settings, "-123")
    unchanged(page, requests)


@pytest.mark.parametrize("mutation,code", [
    ("document.querySelector('h2').innerText='Окно чата с Другой'", "BROWSER_WRONG_CHANNEL"),
    ("document.querySelector('[contenteditable]').contentEditable='false'", "BROWSER_PUBLISH_PERMISSION_REQUIRED"),
    ("history.replaceState(null,'','/-456')", "BROWSER_WRONG_CHANNEL"),
])
def test_last_immediate_check_rejects_change_after_readiness(channel, monkeypatch, mutation, code):
    settings, page, requests = channel
    route(page, html())
    wait = page.wait_for_function

    def changed(*args, **kwargs):
        result = wait(*args, **kwargs)
        page.evaluate(mutation)
        return result

    monkeypatch.setattr(page, "wait_for_function", changed)
    with pytest.raises(AgentError, match="^" + code + "$"):
        browser_runtime.ensure_channel(page, settings, "-123")
    unchanged(page, requests)


@pytest.mark.parametrize("content", [
    html(editor=False),
    html(editor_attributes='style="display:none"'),
    html(editor_attributes='aria-disabled="true"'),
    html(script="document.querySelector('[contenteditable]').contentEditable='false'"),
], ids=["missing", "hidden", "disabled", "not_editable"])
def test_read_identity_does_not_require_or_change_publish_composer(channel, content):
    settings, page, requests = channel
    route(page, content)
    browser_runtime.ensure_channel(page, settings, "-123", require_publish=False)
    unchanged(page, requests)


@pytest.mark.parametrize("content,code", [
    (html(heading="", editor=False), "BROWSER_LOGIN_OR_CHANNEL_REQUIRED"),
    (html(heading="Окно чата с Другой", editor=False), "BROWSER_WRONG_CHANNEL"),
    (html(editor=False, script="document.body.insertAdjacentHTML('afterbegin','<h2 id=main-header-title>Окно чата с Тест</h2>')"),
     "BROWSER_LOGIN_OR_CHANNEL_REQUIRED"),
], ids=["login", "wrong_header", "ambiguous_header"])
def test_read_identity_still_rejects_login_or_wrong_header(channel, monkeypatch, content, code):
    settings, page, requests = channel
    monkeypatch.setattr(browser_runtime, "_CHANNEL_READY_TIMEOUT_MS", 150)
    route(page, content)
    with pytest.raises(AgentError, match="^" + code + "$"):
        browser_runtime.ensure_channel(page, settings, "-123", require_publish=False)
    unchanged(page, requests)


@pytest.mark.parametrize("mutation,code", [
    ("document.querySelector('h2').innerText='Окно чата с Другой'", "BROWSER_WRONG_CHANNEL"),
    ("history.replaceState(null,'','/-456')", "BROWSER_WRONG_CHANNEL"),
    ("document.body.insertAdjacentHTML('afterbegin','<h2 id=main-header-title>Окно чата с Тест</h2>')",
     "BROWSER_LOGIN_OR_CHANNEL_REQUIRED"),
])
def test_read_identity_last_check_rejects_changed_or_ambiguous_channel(
        channel, monkeypatch, mutation, code):
    settings, page, requests = channel
    route(page, html(editor=False))
    wait = page.wait_for_function

    def changed(*args, **kwargs):
        result = wait(*args, **kwargs)
        page.evaluate(mutation)
        return result

    monkeypatch.setattr(page, "wait_for_function", changed)
    with pytest.raises(AgentError, match="^" + code + "$"):
        browser_runtime.ensure_channel(page, settings, "-123", require_publish=False)
    unchanged(page, requests)
