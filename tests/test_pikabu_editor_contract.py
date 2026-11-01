"""Observed Pikabu paragraph wrappers, parsed without ignoring adjacent content."""

import pytest

from max_postiz_agent.pikabu_browser_worker import _editor_content
from max_postiz_agent.pikabu_delivery import check_body_links
from max_postiz_agent.config import AgentError


def wrapped(text):
    return ('<div class="node-wrapper-view__host--x6bdZ_Cf" data-node-view-wrapper style="display:flex;flex-direction:column">'
            '<!--inert--><div><!--inert--></div>'
            '<div class="node-wrapper-view__content--YBpn9eXj">'
            '<p class="node-paragraph-view__content--hRxcjf1f">' + text + '</p></div></div>')


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        value = playwright.chromium.launch(channel="chrome", headless=True)
        yield value
        value.close()


@pytest.fixture
def capture(browser):
    context = browser.new_context(service_workers="block")
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    def parse(value, root_style=""):
        page.set_content('<div class="editor" contenteditable="true" style="white-space:pre-wrap;' + root_style + '">'
                         + value + '</div>')
        return _editor_content(page.locator(".editor"), ".editor img")
    yield parse
    context.close()


@pytest.mark.parametrize("markup,expected", [
    (wrapped('<br class="ProseMirror-trailingBreak">'), ""),
    (wrapped("  Точный текст  "), "  Точный текст  "),
    (wrapped("Первый") + wrapped("Второй"), "Первый\n\nВторой"),
    (wrapped("Первый") + wrapped('<br class="ProseMirror-trailingBreak">') + wrapped("Третий"),
     "Первый\n\n\n\nТретий"),
    (wrapped("Строка<br>Ещё строка"), "Строка\nЕщё строка"),
    (wrapped('Строка<br><br class="ProseMirror-trailingBreak">'), "Строка\n"),
    (wrapped("<strong>Точно</strong> <a href='https://example.org/'>ссылка</a>"), "Точно ссылка"),
    ("<p>Прежняя fixture</p>" + wrapped("Новый абзац"), "Прежняя fixture\n\nНовый абзац"),
])
def test_exact_observed_wrappers_preserve_paragraphs_breaks_and_whitespace(capture, markup, expected):
    result = capture(markup)
    assert result["structure"]["unsupported"] is False
    assert result["body"] == result["structure"]["rendered_body"] == expected
    assert result["images"] == []


@pytest.mark.parametrize("markup", [
    wrapped("Текст") + "Чужой текст",
    wrapped("Текст") + " ",
    wrapped("Текст") + "\n",
    wrapped("Текст").replace("<!--inert-->", "Чужой текст", 1),
    wrapped("Текст").replace("<!--inert-->", " ", 1),
    wrapped("Текст").replace("<div><!--inert--></div>", "<div style='display:none'>Чужой текст</div>"),
    wrapped("Текст").replace("<div><!--inert--></div>", "<div> </div>"),
    wrapped("Текст").replace("<div><!--inert--></div>", "<div><!--inert--></div><div><!--inert--></div>"),
    wrapped("Текст").replace(" data-node-view-wrapper", ""),
    wrapped("Текст").replace("node-wrapper-view__host--x6bdZ_Cf", "different-wrapper"),
    wrapped("Текст").replace("node-wrapper-view__content--YBpn9eXj", "different-content"),
    wrapped("Текст").replace("node-paragraph-view__content--hRxcjf1f", "different-paragraph"),
    wrapped("Текст").replace("</p>", "</p><p>Соседний абзац</p>"),
    wrapped("Текст").replace("</p>", "</p><img src='data:image/png;base64,AAAA'>"),
    wrapped("Текст").replace("</p>", "</p>Чужой текст"),
    wrapped("Текст").replace("</p>", "</p> "),
    wrapped("<span style='display:block'>Текст</span>"),
    wrapped("<div>Текст</div>"),
    wrapped("<img src='data:image/png;base64,AAAA'>Текст"),
    wrapped("<iframe></iframe>Текст"),
    wrapped('Первый<br class="ProseMirror-trailingBreak">Второй'),
    wrapped('Первый<br class="ProseMirror-trailingBreak extra">'),
    wrapped('Первый<br class="ProseMirror-trailingBreak"><br class="ProseMirror-trailingBreak">'),
    '<div><p>Скрытая произвольная обёртка</p></div>',
])
def test_unknown_wrapper_siblings_text_media_and_forged_trailing_breaks_fail_closed(capture, markup):
    assert capture(markup)["structure"]["unsupported"] is True


def test_hidden_inline_text_is_not_claimed_as_rendered_exact_body(capture):
    result = capture(wrapped("<span style='visibility:hidden'>Скрыто</span> видно"))
    assert result["body"] == "Скрыто видно"
    assert result["structure"]["rendered_body"] != result["body"]


def test_one_native_tail_affordance_is_removed_and_intended_empty_paragraphs_survive(capture):
    empty = wrapped('<br class="ProseMirror-trailingBreak">')
    first = capture(wrapped("Первый") + empty)
    assert first["structure"]["unsupported"] is False
    assert first["body"] == "Первый" and first["structure"]["trailing_editor_affordance"] is True
    intended = capture(wrapped("Первый") + empty + empty)
    assert intended["structure"]["unsupported"] is False
    assert intended["body"] == "Первый\n\n"
    legacy = capture('<p>Первый</p><p><br class="ProseMirror-trailingBreak"></p>')
    assert legacy["body"] == "Первый\n\n" and legacy["structure"]["trailing_editor_affordance"] is False


CHROME = ('<div><div class="node-wrapper-view__dragHandleWrapper--SnnuCPei" contenteditable="false">'
          '<div class="node-wrapper-view__button--QFFfYqkA"><svg class="Icon__host--vo0ED8kr">'
          '<use href="#icon--ui-kit__drag_s"></use></svg></div></div></div>')


def test_only_observed_noneditable_drag_icon_chrome_is_admitted(capture):
    markup = wrapped("Первый").replace('<div><!--inert--></div>', CHROME)
    result = capture(markup)
    assert result["structure"]["unsupported"] is False and result["body"] == "Первый"
    for changed in (CHROME.replace('contenteditable="false"', 'contenteditable="true"'),
                    CHROME.replace('#icon--ui-kit__drag_s', 'https://example.org/foreign.svg'),
                    CHROME.replace('</use>', '</use><text>Чужой текст</text>'),
                    CHROME.replace('</svg>', '</svg><img src="data:image/png;base64,AAAA">')):
        assert capture(wrapped("Первый").replace('<div><!--inert--></div>', changed))["structure"]["unsupported"] is True


@pytest.mark.parametrize("style", [
    "opacity:0", "visibility:hidden", "display:none", "display:flex;flex-direction:column-reverse",
    "height:0;max-height:0;overflow:hidden", "height:1px;max-height:1px;overflow:hidden",
    "position:absolute", "transform:translateY(10px)",
])
def test_hidden_reversed_or_clipped_observed_wrappers_fail_closed(capture, style):
    markup = wrapped("Первый").replace('display:flex;flex-direction:column', 'display:flex;flex-direction:column;' + style)
    assert capture(markup)["structure"]["unsupported"] is True


@pytest.mark.parametrize("style", ["display:inline", "height:0;line-height:0;overflow:hidden;margin:0", "opacity:0"])
def test_every_paragraph_including_empty_ones_has_positive_visible_block_box(capture, style):
    middle = wrapped('<br class="ProseMirror-trailingBreak">').replace('<p class=', '<p style="' + style + '" class=')
    assert capture(wrapped("Первый") + middle + wrapped("Последний"))["structure"]["unsupported"] is True


def test_root_reverse_flow_is_rejected(capture):
    assert capture(wrapped("Первый") + wrapped("Последний"),
                   root_style="display:flex;flex-direction:column-reverse")["structure"]["unsupported"] is True


def test_link_targets_are_bound_to_explicit_approved_urls_and_exact_redirect():
    body = "Источник: https://example.org/article. Подробности https://example.org/article#section"
    base = "https://pikabu.ru/story/test_123"
    check_body_links(["https://example.org/article", "https://example.org/article#section",
                      "?u=https%3A%2F%2Fexample.org%2Farticle"], body, base_url=base)
    for url in ("https://example.net/unapproved", "javascript:alert(1)",
                "?u=https%3A%2F%2Fexample.org%2Farticle&extra=unapproved",
                "https://pikabu.ru/other?u=https%3A%2F%2Fexample.org%2Farticle"):
        with pytest.raises(AgentError, match="PIKABU_UNAPPROVED_LINK"):
            check_body_links([url], body, base_url=base)
