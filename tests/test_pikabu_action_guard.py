"""The final SQL/DOM window bounds when a native mutation may be issued."""

from types import SimpleNamespace
import re

import pytest

from max_postiz_agent.config import AgentError
import max_postiz_agent.pikabu_browser_worker as worker


def state(snapshot):
    return {"url": worker.ORIGIN + "/add", "document_title": "Редактор", "snapshot": snapshot,
        "account": {"visible": True, "href": "/@fixture_owner"}, "captcha": False, "errors": False,
        "selected_mode": False, "editor_focus": True, "tag_focus": True, "tag_value": "",
        "button": {"visible": True, "enabled": True, "label": "Опубликовать пост"}}


@pytest.mark.parametrize("clock", [[100, 101.1], [100, 100.1, 101.1]])
def test_slow_sql_callback_or_atomic_capture_never_issues_native_action(monkeypatch, clock):
    readings = iter(clock)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    snapshot = {"body": "Точный текст"}
    captures, commands = [], []
    def capture(*args):
        captures.append(True)
        return state(snapshot)
    monkeypatch.setattr(worker, "_atomic_action_state", capture)
    with pytest.raises(AgentError, match="PIKABU_ACTION_GUARD_EXPIRED"):
        worker._action_guard(None, {"owner": "fixture_owner"}, {}, lambda: None, snapshot,
            origin=worker.ORIGIN, publish=True, action=lambda budget: commands.append(budget))
    assert not commands
    assert len(captures) == (0 if len(clock) == 2 else 1)


@pytest.mark.parametrize("error,expected", [
    ("TARGET_PAUSED", "PIKABU_TARGET_PAUSED"),
    ("ROUTE_DISPATCH_LEASE_LOST", "PIKABU_DISPATCH_LEASE_LOST"),
    ("PIKABU_ACTION_GUARD_EXPIRED", "PIKABU_ACTION_GUARD_EXPIRED"),
    ("private token / cookie / traceback", "PIKABU_OPERATION_UNCONFIRMED"),
    ({"private": "value"}, "PIKABU_OPERATION_UNCONFIRMED"),
])
def test_failure_code_is_a_known_constant_without_raw_error_text(error, expected):
    journal = {"state": "unknown", "fixture": False, "execution_origin": worker.ORIGIN,
        "request_id": "operation", "root": "isolated", "destination": "fixture_owner", "owner": "fixture_owner",
        "options_sha256": "a" * 64, "dom_contract_sha256": "b" * 64, "snapshot_sha256": "c" * 64,
        "error": error}
    code = worker._result(journal)["evidence"]["failure_code"]
    assert code == expected and re.fullmatch(r"PIKABU_[A-Z_]+", code)


def test_native_locator_action_gets_only_remaining_budget(monkeypatch):
    readings = iter([100, 100.2, 100.4])
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    snapshot = {"body": "Точный текст"}
    monkeypatch.setattr(worker, "_atomic_action_state", lambda *args: state(snapshot))
    commands = []
    worker._action_guard(None, {"owner": "fixture_owner"}, {}, lambda: None, snapshot,
        origin=worker.ORIGIN, publish=True, action=lambda budget: commands.append(budget))
    assert len(commands) == 1 and 0 < commands[0] <= 600


@pytest.mark.parametrize("change,code", [
    ("body", "PIKABU_EDITOR_CHANGED_BEFORE_PUBLISH"), ("url", "PIKABU_EDITOR_URL_CHANGED"),
    ("owner", "PIKABU_ACCOUNT_MISMATCH"), ("focus", "PIKABU_EDITOR_FOCUS_CHANGED"),
    ("tag", "PIKABU_EDITOR_PENDING_TAG"), ("mode", "PIKABU_PUBLISH_MODE_UNEXPECTED"),
    ("captcha", "PIKABU_CAPTCHA_OR_ACCESS_BLOCKED"), ("disabled", "PIKABU_PUBLISH_BUTTON_DISABLED"),
])
def test_callback_changes_are_captured_after_authorization_without_native_action(monkeypatch, change, code):
    snapshot = {"body": "Точный текст"}
    actual = state(snapshot)
    def authorize():
        if change == "body":
            actual["snapshot"] = {"body": "Подмена"}
        elif change == "url":
            actual["url"] = worker.ORIGIN + "/story/old_123"
        elif change == "owner":
            actual["account"]["href"] = "/@someone_else"
        elif change == "focus":
            actual["editor_focus"] = False
        elif change == "tag":
            actual["tag_value"] = "Несогласованный тег"
        elif change == "mode":
            actual["selected_mode"] = True
        elif change == "captcha":
            actual["captcha"] = True
        else:
            actual["button"]["enabled"] = False
    monkeypatch.setattr(worker, "_atomic_action_state", lambda *args: actual)
    commands = []
    with pytest.raises(AgentError, match=code):
        worker._action_guard(None, {"owner": "fixture_owner"}, {}, authorize, snapshot,
            origin=worker.ORIGIN, publish=True, focus="editor", action=lambda budget: commands.append(budget))
    assert not commands
