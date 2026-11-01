"""The portal opens only a fresh selection and never claims account or delivery proof."""
from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError


@pytest.fixture
def portal():
    path = Path(__file__).resolve().parents[1] / "deployment/linux/login-browser.py"
    spec = importlib.util.spec_from_file_location("owner_portal_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selected(platform="max"):
    return {"target_id": "test", "selection_id": "a" * 32, "platform": platform,
            "expires_at": 2000, "profile_dir": "data/" + platform + "-browser",
            "url": "https://web.max.ru/" if platform == "max" else "https://vk.ru/id123"}


@pytest.mark.parametrize("platform", ["max", "vk"])
def test_selected_transport_is_visible_locked_and_produces_no_access_or_delivery_claim(portal, monkeypatch, tmp_path, platform):
    from max_postiz_agent import browser_runtime, vk_browser_worker

    value = selected(platform)
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path),
                           publishing={"targets": [{"id": "test", "options": {"profile_dir": value["profile_dir"]}}]})
    calls = []
    page = SimpleNamespace(context=SimpleNamespace(pages=[1]))
    page.goto = lambda url, **kwargs: calls.append(("goto", url, kwargs))
    page.wait_for_timeout = lambda amount: page.context.pages.clear()

    @contextmanager
    def session(*args, **kwargs):
        calls.append(("open", args, kwargs))
        try:
            yield page
        finally:
            calls.append(("close",))

    monkeypatch.setattr(portal.time, "time", lambda: 1000)
    monkeypatch.setattr(portal, "selection", lambda actual: value)
    monkeypatch.setattr(browser_runtime if platform == "max" else vk_browser_worker, "session", session)
    result = portal.run_window(core, value)
    assert calls[0][0] == "open" and calls[0][2] == {"visible": True}
    assert [call[1] for call in calls if call[0] == "goto"] == [value["url"]]
    assert calls[-1] == ("close",)
    assert result == {"state": "manual_login_window_closed", "target_id": "test",
                      "selection_id": value["selection_id"], "platform": platform,
                      "access_verified": False, "live_delivery_verified": False, "external_publications": 0}


def test_changed_selection_after_profile_lock_closes_without_any_navigation(portal, monkeypatch, tmp_path):
    from max_postiz_agent import browser_runtime

    value = selected()
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    values = iter([value, {**value, "selection_id": "b" * 32}])
    monkeypatch.setattr(portal, "selection", lambda actual: next(values))
    closed = []

    @contextmanager
    def session(*args, **kwargs):
        try:
            yield SimpleNamespace(goto=lambda *a, **k: pytest.fail("Navigation after changed selection"))
        finally:
            closed.append(True)

    monkeypatch.setattr(browser_runtime, "session", session)
    with pytest.raises(AgentError, match="^LOGIN_SELECTION_STALE$"):
        portal.run_window(core, value)
    assert closed == [True]


def test_external_max_profile_is_rejected_before_database_or_browser(portal, monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "load_env", lambda root: None)
    monkeypatch.setenv("MAX_EXISTING_BROWSER_PROFILE", "forbidden-borrowed-profile")
    monkeypatch.setattr(portal, "Database", lambda settings: pytest.fail("Database opened"))
    with pytest.raises(AgentError, match="^BROWSER_EXTERNAL_PROFILE_INVALID$"):
        with portal.authority(tmp_path):
            pytest.fail("Borrowed profile accepted")


def test_authority_never_initializes_or_synchronizes_database(portal, monkeypatch, tmp_path):
    calls = []
    monkeypatch.delenv("MAX_EXISTING_BROWSER_PROFILE", raising=False)
    monkeypatch.setattr(portal, "load_env", lambda root: calls.append("load_env"))
    monkeypatch.setattr(portal, "Settings", SimpleNamespace(load=lambda root: "settings"))
    database = SimpleNamespace(close=lambda: calls.append("database_close"))
    monkeypatch.setattr(portal, "Database", lambda settings: database)

    def build(settings, actual_database, **kwargs):
        assert settings == "settings" and actual_database is database
        assert kwargs == {"bootstrap_targets": False}
        return SimpleNamespace(close=lambda: calls.append("core_close"))

    monkeypatch.setattr(portal, "HubService", build)
    with portal.authority(tmp_path):
        pass
    assert calls == ["load_env", "core_close"]


@pytest.mark.parametrize("changed", [{"access_verified": True}, {"live_delivery_verified": True},
                                     {"external_publications": False}, {"target_id": "other"},
                                     {"selection_id": "b" * 32}, {"extra": "unexpected"}])
def test_supervised_worker_receipt_cannot_create_false_access_evidence(portal, monkeypatch, tmp_path, changed):
    value = selected()
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))

    @contextmanager
    def authority(root):
        yield core

    receipt = {"state": "manual_login_window_closed", "target_id": "test",
               "selection_id": value["selection_id"], "platform": "max", "access_verified": False,
               "live_delivery_verified": False, "external_publications": 0, **changed}
    monkeypatch.setattr(portal, "authority", authority)
    monkeypatch.setattr(portal, "selection", lambda actual: value)
    monkeypatch.setattr(portal.time, "time", lambda: 1000)
    calls = []

    def worker(command, **kwargs):
        calls.append((command, kwargs))
        assert json.loads(kwargs["input"]) == value
        assert kwargs["timeout"] == 1010
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt))

    monkeypatch.setattr(portal, "run_browser_worker", worker)
    monkeypatch.setattr(portal, "atomic_write", lambda *args: pytest.fail("False evidence persisted"))
    with pytest.raises(AgentError, match="^OWNER_BROWSER_RECEIPT_INVALID$"):
        portal.main(["--root", str(tmp_path)])
    assert len(calls) == 1 and "--child" in calls[0][0]


def test_child_cannot_replace_approved_selection_from_stdin(portal, monkeypatch, tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))

    @contextmanager
    def authority(root):
        yield core

    monkeypatch.setattr(portal, "authority", authority)
    monkeypatch.setattr(portal, "selection", lambda actual: selected())
    monkeypatch.setattr(portal.sys, "stdin", io.StringIO(json.dumps(selected("vk"))))
    monkeypatch.setattr(portal, "run_window", lambda *args: pytest.fail("Changed selection opened browser"))
    with pytest.raises(AgentError, match="^LOGIN_SELECTION_STALE$"):
        portal.main(["--root", str(tmp_path), "--child"])
