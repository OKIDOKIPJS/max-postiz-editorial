"""Readiness reports empty/supported state without exposing or changing a draft."""

import json

import pytest

import max_postiz_agent.pikabu_browser_worker as worker
from test_pikabu_delivery import browser_fixture as browser_fixture, post as post


@pytest.mark.parametrize("draft,ready", [
    ("blank", True), ("title", False), ("body", False), ("pending_tag", False), ("unsupported", False),
])
def test_inspection_never_calls_nonempty_editor_publish_ready_or_reports_private_values(
        browser_fixture, monkeypatch, draft, ready):
    page, origin, options, state = browser_fixture
    page.goto(origin + "/add")
    private = "PRIVATE_INSPECTION_VALUE"
    if draft == "title":
        page.locator("textarea").fill(private)
    elif draft == "body":
        page.locator(".editor p").first.evaluate("(node,value)=>node.textContent=value", private)
    elif draft == "pending_tag":
        page.locator("#tags").fill(private)
    elif draft == "unsupported":
        page.locator(".editor").evaluate("(node,value)=>{const div=document.createElement('div');"
                                       "div.textContent=value;node.append(div)}", private)
    monkeypatch.setattr(worker, "_navigate", lambda *args: None)
    report = worker.inspect_page(page, options, origin=origin)
    assert report["state"] == "available" and report["live_verified"] is False
    assert report["publish"] is ready and report["editor_empty"] is ready
    assert report["pending_tag"] is (draft == "pending_tag")
    assert report["editor_supported"] is (draft != "unsupported")
    assert private not in json.dumps(report)
    assert state["posts"] == state["uploads"] == 0
