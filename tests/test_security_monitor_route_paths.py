"""Новые браузерные маршруты не открывают приватные файлы через выбор workflow."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from max_postiz_agent import panel_files
from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.panel_settings import snapshot, validate_configuration, workflow_target


def service_for(project):
    return SimpleNamespace(settings=Settings.load(project))


def publishing(project, route, profile):
    value = {"targets": [{"route": route, "enabled": False, "options": {"profile_dir": profile}}]}
    (project / "config/publishing.json").write_text(json.dumps(value), encoding="utf-8")


def prohibit_private_read(monkeypatch, target):
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == target:
            pytest.fail("A private fixture must be rejected before its contents are read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


@pytest.mark.parametrize("directory", ["data/monitor", "data/monitor-browser", "data/vk-browser", "data/pikabu-browser"])
@pytest.mark.parametrize("purpose", ["file", "workflow", "private"])
def test_new_default_private_roots_cannot_be_browsed_selected_or_imported(project, monkeypatch, directory, purpose):
    service = service_for(project)
    target = project / directory / "synthetic-state.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"metadata":"synthetic-private-fixture"}', encoding="utf-8")
    prohibit_private_read(monkeypatch, target)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.browse(service, directory, purpose=purpose)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, target.relative_to(project).as_posix(), purpose=purpose)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.import_selected(service, str(target), "file", purpose)


@pytest.mark.parametrize("route", ["vk_browser", "pikabu_browser"])
@pytest.mark.parametrize("purpose", ["file", "workflow", "private"])
def test_disabled_relocated_route_profiles_remain_private(project, monkeypatch, route, purpose):
    service = service_for(project)
    directory = "config/relocated-" + route
    publishing(project, route, directory)
    target = project / directory / "synthetic-session.json"
    target.parent.mkdir()
    target.write_text('{"metadata":"synthetic-private-fixture"}', encoding="utf-8")
    prohibit_private_read(monkeypatch, target)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, target.relative_to(project).as_posix(), purpose=purpose)
    assert target.parent.name not in {item["name"] for item in panel_files.browse(service, "config", purpose)["entries"]}
    service.settings.app["video"]["workflow_path"] = target.relative_to(project).as_posix()
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        snapshot(service)


@pytest.mark.parametrize("purpose", ["file", "workflow", "private"])
def test_monitor_configuration_is_not_a_file_picker_document(project, monkeypatch, purpose):
    service = service_for(project)
    target = project / "config/monitor.json"
    target.write_text('{"metadata":"synthetic-private-monitor-configuration"}', encoding="utf-8")
    prohibit_private_read(monkeypatch, target)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, "config/monitor.json", purpose=purpose)
    assert "monitor.json" not in {item["name"] for item in panel_files.browse(service, "config", purpose)["entries"]}
    service.settings.app["video"]["workflow_path"] = "config/monitor.json"
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        snapshot(service)


@pytest.mark.parametrize("document", [None, {"1": {"class_type": "Fixture", "inputs": {}}}])
def test_monitor_workflow_collision_is_rejected_before_configuration_validation_writes(project, document):
    service = service_for(project)
    value = snapshot(service)["configuration"]
    value["app"]["video"]["workflow_path"] = "config/monitor.json"
    value["workflow"] = document
    with pytest.raises(AgentError, match="WORKFLOW_PATH_INVALID"):
        validate_configuration(project, value)
    assert not (project / "config/monitor.json").exists()


@pytest.mark.parametrize("profile", ["../synthetic-outside", 123, ""])
def test_invalid_route_profile_configuration_fails_closed_without_exposing_paths(project, profile):
    service = service_for(project)
    publishing(project, "vk_browser", profile)
    with pytest.raises(AgentError, match="FILE_PRIVATE_PATH_CONFIG_INVALID") as failure:
        panel_files.browse(service, "config", purpose="workflow")
    assert str(failure.value) == "FILE_PRIVATE_PATH_CONFIG_INVALID"


def test_profile_configuration_is_refreshed_and_public_siblings_stay_usable(project):
    service = service_for(project)
    target = project / "config/browser-state-sibling/workflow.json"
    target.parent.mkdir()
    target.write_text('{"1":{"class_type":"Fixture","inputs":{}}}', encoding="utf-8")
    publishing(project, "vk_browser", "config/browser-state")
    assert panel_files.selected(service, target.relative_to(project).as_posix(), purpose="workflow")["document"]
    publishing(project, "vk_browser", "config/browser-state-sibling")
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, target.relative_to(project).as_posix(), purpose="workflow")
    publishing(project, "vk_browser", "config/browser-state")
    service.settings.app["video"]["workflow_path"] = target.relative_to(project).as_posix()
    assert workflow_target(project, service.settings.app) == target


def test_file_alias_cannot_read_a_relocated_route_profile(project, monkeypatch):
    service = service_for(project)
    publishing(project, "pikabu_browser", "config/relocated-profile")
    target = project / "config/relocated-profile/session.json"
    target.parent.mkdir()
    target.write_text('{"metadata":"synthetic-private-fixture"}', encoding="utf-8")
    alias = project / "config/visible-workflow.json"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this host")
    prohibit_private_read(monkeypatch, target)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        panel_files.selected(service, "config/visible-workflow.json", purpose="workflow")
