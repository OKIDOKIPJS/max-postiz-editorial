"""Подготовка закрытого канала: локальные конфиги и полностью перехваченный DOM."""

import copy
import json
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from max_postiz_agent import browser_channels, monitor_cli
from max_postiz_agent.browser_channels import create_channel
from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_models import Target
from max_postiz_agent.isolated_workspace import assert_isolated, mark_isolated
from max_postiz_agent.monitor_browser import MonitorBrowser
from max_postiz_agent.monitor_config import (
    MonitorConfig, bind_created_channel, initialize_monitor_config,
)
from test_browser_channels import creation_browser as _creation_browser_fixture


NAME = "monitoring_proekts"
creation_browser = _creation_browser_fixture


@pytest.fixture
def setup_config(project, monkeypatch):
    for key in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    mark_isolated(project, project.parent / "untouched-source")
    config = initialize_monitor_config(project)
    value = copy.deepcopy(config.value)
    value["report_chat_name"] = NAME
    (project / "config/monitor.json").write_text(json.dumps(value), encoding="utf-8")
    return MonitorConfig.load(project)


def setup_transport(settings):
    return MonitorBrowser(settings, {"report_chat_id": "0", "report_chat_name": NAME,
                                    "operation_timeout_seconds": 1})


def test_creation_checks_authority_before_click_and_does_not_bind_editorial_config(creation_browser):
    settings, page = creation_browser
    original = (settings.root / "config/app.json").read_bytes()
    calls = []

    def approve():
        calls.append(page.evaluate("localStorage.count"))
        return True

    result = create_channel(settings, NAME, authorize_before_create=approve)
    assert result["state"] == "confirmed" and result["chat_id"] == "-456"
    assert calls == [None] and page.evaluate("localStorage.count") == "1"
    assert (settings.root / "config/app.json").read_bytes() == original
    assert not (settings.root / "data/browser-publications").exists()


@pytest.mark.parametrize("authorization", [False, None, "true"])
def test_denied_creation_does_not_click_or_save_creation_intent(creation_browser, authorization):
    settings, page = creation_browser
    with pytest.raises(AgentError, match="BROWSER_CONFIG_CHANGED"):
        create_channel(settings, NAME, authorize_before_create=lambda: authorization)
    assert page.evaluate("localStorage.count") is None
    assert not list((settings.root / "data/browser-channels").glob("*.json"))


def test_custom_creation_authority_cannot_bind_editorial_configuration(creation_browser):
    settings, page = creation_browser
    with pytest.raises(AgentError, match="BROWSER_CONFIG_CHANGED"):
        create_channel(settings, NAME, bind_channel=True, authorize_before_create=lambda: True)
    # Этот отказ теперь происходит до открытия браузерной сессии/навигации.
    assert page.url == "about:blank"


def test_monitor_setup_authorizes_exact_name_twice_and_uses_separate_runtime(creation_browser):
    settings, page = creation_browser
    original = (settings.root / "config/app.json").read_bytes()
    transport = setup_transport(settings)
    names = []
    result = transport.create_private_channel(authorize=lambda name: names.append(name) or name == NAME)
    assert result["state"] == "confirmed" and result["name"] == NAME
    assert names == [NAME, NAME] and page.evaluate("localStorage.count") == "1"
    assert (settings.root / "config/app.json").read_bytes() == original
    assert list((transport.settings.root / "data/browser-channels").glob("*.json"))
    assert not (transport.settings.root / "data/browser-publications").exists()


def test_withdrawn_precreate_authority_preserves_empty_channel_journal(creation_browser):
    settings, page = creation_browser
    transport = setup_transport(settings)
    decisions = iter([True, False])
    with pytest.raises(AgentError, match="BROWSER_CONFIG_CHANGED"):
        transport.create_private_channel(authorize=lambda _: next(decisions))
    assert page.evaluate("localStorage.count") is None
    assert not list((transport.settings.root / "data/browser-channels").glob("*.json"))


def test_precreate_callback_cannot_change_runtime_after_last_guard(creation_browser):
    settings, page = creation_browser
    transport = setup_transport(settings)
    calls = []

    def changed(_name):
        calls.append(True)
        if len(calls) == 2:
            transport.settings.app["max"]["monitor_config_hash"] = "changed-after-guard"
        return True

    with pytest.raises(AgentError, match="MONITOR_BROWSER_CONFIG_CHANGED"):
        transport.create_private_channel(authorize=changed)
    assert page.evaluate("localStorage.count") is None


def test_unknown_monitor_create_is_not_repeated_or_bound(creation_browser, monkeypatch):
    settings, page = creation_browser
    transport = setup_transport(settings)

    def lost(*args):
        raise RuntimeError("fixture interruption after create")

    monkeypatch.setattr(browser_channels, "verify_created", lost)
    first = transport.create_private_channel(authorize=lambda _: True)
    assert first["state"] == "unknown" and page.evaluate("localStorage.count") == "1"
    path = next((transport.settings.root / "data/browser-channels").glob("*.json"))
    journal = json.loads(path.read_text(encoding="utf-8"))
    journal.pop("chat_id", None)
    path.write_text(json.dumps(journal), encoding="utf-8")
    second = transport.create_private_channel(authorize=lambda _: True)
    assert second["state"] == "unknown" and page.evaluate("localStorage.count") == "1"
    assert not (settings.root / "config/monitor.json").exists()


def test_login_setup_opens_only_home_and_creates_no_channel(creation_browser, monkeypatch):
    settings, page = creation_browser
    transport = setup_transport(settings)

    @contextmanager
    def opened(*args, **kwargs):
        yield page

    monkeypatch.setattr("max_postiz_agent.browser_runtime.session", opened)
    result = transport.login_setup(timeout=1)
    assert result == {"state": "login_available", "delivery_verified": False, "channel_created": False}
    assert page.url == "https://web.max.ru/" and page.evaluate("localStorage.count") is None
    assert not list((transport.settings.root / "data/browser-channels").glob("*.json"))
    assert not (transport.settings.root / "data/browser-publications").exists()


def test_confirmed_channel_binding_keeps_reports_and_commands_disabled(setup_config):
    root = setup_config.root
    source_before = {name: (root / name).read_bytes() for name in ("config/app.json", "config/sources.json", "TOPIC.md")}
    updated = bind_created_channel(setup_config, {"state": "confirmed", "name": NAME, "chat_id": "-456"})
    assert updated.value["report_chat_id"] == "-456" and updated.value["report_private_confirmed"] is True
    assert updated.value["enabled"] is False and updated.value["commands"]["enabled"] is False
    assert {name: (root / name).read_bytes() for name in source_before} == source_before


@pytest.mark.parametrize("changes", [
    {"state": "unknown"}, {"state": "creating"}, {"state": "pending"},
    {"name": "different_channel"}, {"chat_id": -456}, {"chat_id": "https://max.ru/anything"},
    {"chat_id": ""}, {"chat_id": "-" + "1" * 23},
])
def test_unconfirmed_mismatched_or_invalid_binding_preserves_configuration(setup_config, changes):
    path = setup_config.root / "config/monitor.json"
    before = path.read_bytes()
    result = {"state": "confirmed", "name": NAME, "chat_id": "-456", **changes}
    with pytest.raises(AgentError, match="MONITOR_CHANNEL_BINDING_INVALID"):
        bind_created_channel(setup_config, result)
    assert path.read_bytes() == before


def test_binding_rejects_stale_monitor_configuration(setup_config):
    path = setup_config.root / "config/monitor.json"
    changed = copy.deepcopy(setup_config.value)
    changed["poll_seconds"] = 60
    path.write_text(json.dumps(changed), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AgentError, match="MONITOR_CHANNEL_BINDING_INVALID"):
        bind_created_channel(setup_config, {"state": "confirmed", "name": NAME, "chat_id": "-456"})
    assert path.read_bytes() == before


def test_enabled_monitor_cannot_replace_bound_destination(setup_config):
    path = setup_config.root / "config/monitor.json"
    changed = copy.deepcopy(setup_config.value)
    changed.update(enabled=True, report_chat_id="-1", report_private_confirmed=True)
    path.write_text(json.dumps(changed), encoding="utf-8")
    current = MonitorConfig.load(setup_config.root)
    before = path.read_bytes()
    with pytest.raises(AgentError, match="MONITOR_CHANNEL_BINDING_INVALID"):
        bind_created_channel(current, {"state": "confirmed", "name": NAME, "chat_id": "-456"})
    assert path.read_bytes() == before


def test_cli_init_is_disabled_isolated_and_does_not_touch_source(project, monkeypatch, capsys):
    repository = Path(__file__).resolve().parents[1]
    for key in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    for name in ("app", "sources", "publishing", "four_platform.publishing"):
        shutil.copy2(repository / ("config/" + name + ".example.json"), project / ("config/" + name + ".example.json"))
    (project / "config/app.json").unlink()
    source = project.parent / "source-must-remain-unchanged"
    source.mkdir()
    marker = source / "state.txt"
    marker.write_bytes(b"source remains unchanged")
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("network call from init"))
    monkeypatch.setattr("max_postiz_agent.browser_runtime.session", lambda *_a, **_k: pytest.fail("browser from init"))
    assert monitor_cli.main(["--root", str(project), "init", "--source-root", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "initialized_disabled" and result["external_mutations"] == 0
    assert_isolated(project)
    config = MonitorConfig.load(project)
    assert config.value["enabled"] is False and config.value["commands"]["enabled"] is False
    assert config.value["report_chat_id"] == "" and config.value["report_private_confirmed"] is False
    assert marker.read_bytes() == b"source remains unchanged" and list(source.iterdir()) == [marker]
    core = monitor_cli.build_monitor(project)
    try:
        with core.db.session() as session:
            assert all(not target.enabled and target.paused for target in session.scalars(select(Target)))
    finally:
        core.close()


def test_cli_init_rejects_source_as_destination_before_copy_state_write(project, capsys):
    (project / "config/app.json").unlink()
    assert monitor_cli.main(["--root", str(project), "init", "--source-root", str(project)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "ISOLATED_COPY_REQUIRED"
    assert not (project / "config/app.json").exists()
    assert not (project / "data/four-platform/isolation.json").exists()
