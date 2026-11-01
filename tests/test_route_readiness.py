"""Doctor observes local prerequisites without opening adapters, DB or networks."""

import hashlib
import json
import socket
import subprocess

import httpx
import pytest

from conftest import ROOT, write_topic
from max_postiz_agent.config import AgentError, read_topic
from max_postiz_agent.db import Database
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.route_readiness import _linux_support as real_linux_support, readiness


@pytest.fixture
def local_routes(project, monkeypatch):
    monkeypatch.setattr("max_postiz_agent.route_readiness._linux_support", lambda: {
        "supported": True, "writable": True, "cgroup_kill": True, "delegation_exercised": False})
    source = project.parent / (project.name + "-original")
    source.mkdir()
    mark_isolated(project, source)
    publishing = json.loads((ROOT / "config/four_platform.publishing.example.json").read_text())
    (project / "config/publishing.json").write_text(json.dumps(publishing))
    topic = read_topic(project)
    for channel in topic["channels"]:
        channel["enabled"] = True
        channel["publication"]["mode"] = "review"
    write_topic(project, topic)
    return project, publishing


def configure(project, publishing, target):
    publishing["targets"] = [target]
    (project / "config/publishing.json").write_text(json.dumps(publishing))


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_doctor_no_db_adapter_subprocess_network_or_file_writes(local_routes, monkeypatch):
    project, _ = local_routes
    def forbidden(*args, **kwargs):
        pytest.fail("Doctor must only read local configuration")
    monkeypatch.setattr(Database, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = snapshot(project)
    result = readiness(project)
    assert snapshot(project) == before
    assert result["configuration_valid"] is True and len(result["targets"]) == 4
    assert result["production_ready"] is False and result["local_prerequisites_passed"] is False
    assert result["access"] == result["delivery"] == result["database_state"] == "not_checked"
    assert result["network_requests"] == result["external_mutations"] == 0
    assert not any(item["local_prerequisites_passed"] for item in result["targets"])


def test_telegram_local_configuration_never_proves_permissions_or_delivery(local_routes, monkeypatch):
    project, publishing = local_routes
    target = publishing["targets"][1]
    target.update(enabled=True, destination="@private_target_name")
    target["options"]["adapter_mode"] = "live"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-secret-value-never-print")
    configure(project, publishing, target)
    result = readiness(project)
    row = result["targets"][0]
    assert row["credentials_present"] is True and row["configured"] is True
    assert row["local_prerequisites_passed"] is True and result["production_ready"] is False
    encoded = json.dumps(result)
    assert "fixture-secret-value" not in encoded and "private_target_name" not in encoded
    assert row["access"] == row["delivery"] == row["unresolved_dispatches"] == "not_checked"


def test_dom_flag_does_not_complete_missing_vk_identity_contract(local_routes, monkeypatch):
    project, publishing = local_routes
    target = publishing["targets"][2]
    target.update(enabled=True, destination="123456")
    target["options"].update(owner="123456", dom_contract_verified=True)
    target["options"].pop("dom_contract_profile", None)
    (project / "data/vk-browser").mkdir()
    monkeypatch.setattr("max_postiz_agent.route_readiness.importlib.util.find_spec", lambda name: object())
    configure(project, publishing, target)
    row = readiness(project)["targets"][0]
    assert row["dom_contract_verified"] is True and row["dom_contract_complete"] is False
    assert "VK_BROWSER_DOM_CONTRACT_INCOMPLETE" in row["blockers"]
    assert row["local_prerequisites_passed"] is False


def test_vk_preset_is_local_text_only_even_with_media_flag(local_routes, monkeypatch):
    project, publishing = local_routes
    target = publishing["targets"][2]
    target.update(enabled=True, destination="123456")
    target["options"].update(owner="123456", dom_contract_profile="personal-text-v1",
                             dom_contract_verified=True, media_contract_verified=True)
    (project / "data/vk-browser").mkdir()
    monkeypatch.setattr("max_postiz_agent.route_readiness.importlib.util.find_spec", lambda name: object())
    configure(project, publishing, target)
    result = readiness(project)
    row = result["targets"][0]
    assert row["dom_contract_complete"] is True and row["local_prerequisites_passed"] is True
    assert row["media_contract_verified"] is False and "VK_BROWSER_PROFILE_TEXT_ONLY" in row["warnings"]
    assert result["production_ready"] is False
    encoded = json.dumps(result)
    assert "123456" not in encoded and str(project) not in encoded
    assert row["browser_login"] == row["browser_binary"] == "not_checked"


def test_pika_empty_dom_selector_is_blocked_despite_flag(local_routes):
    project, publishing = local_routes
    target = publishing["targets"][3]
    target.update(enabled=True, destination="private_owner")
    target["options"].update(owner="private_owner", dom_contract_verified=True, dom_contract={"title": ""})
    (project / "data/pikabu-browser").mkdir()
    configure(project, publishing, target)
    row = readiness(project)["targets"][0]
    assert row["dom_contract_complete"] is False and row["local_prerequisites_passed"] is False
    assert "PIKABU_DOM_CONTRACT_INCOMPLETE" in row["blockers"]
    assert "private_owner" not in json.dumps(row)


def test_profile_missing_and_disabled_profile_are_separate_local_blockers(local_routes):
    project, publishing = local_routes
    target = publishing["targets"][3]
    target.update(enabled=False, destination="owner")
    target["options"].update(owner="owner", dom_contract_verified=False)
    configure(project, publishing, target)
    topic = read_topic(project)
    next(channel for channel in topic["channels"] if channel["id"] == target["profile_id"])["enabled"] = False
    write_topic(project, topic)
    row = readiness(project)["targets"][0]
    assert {"TARGET_DISABLED", "PROFILE_DISABLED", "BROWSER_PROFILE_MISSING", "DOM_CONTRACT_NOT_VERIFIED"} <= set(row["blockers"])


def test_malformed_config_reports_safe_code_and_duplicate_targets_fail(local_routes):
    project, publishing = local_routes
    publishing["targets"].append(publishing["targets"][0])
    (project / "config/publishing.json").write_text(json.dumps(publishing))
    assert readiness(project)["blockers"] == ["DUPLICATE_TARGET_ID"]
    publishing["targets"][0]["options"]["secret-value-that-must-not-echo"] = "PRIVATE-VALUE"
    (project / "config/publishing.json").write_text(json.dumps(publishing))
    result = readiness(project)
    assert result["configuration_valid"] is False and result["blockers"] == ["CONFIG_SCHEMA_INVALID"]
    assert "PRIVATE" not in json.dumps(result)


def test_readiness_rejects_unsafe_environment_before_any_local_report(local_routes, monkeypatch):
    project, _ = local_routes
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@otherhost:5432/editorial_hub")
    with pytest.raises(AgentError, match="ISOLATED_DATABASE_REQUIRED"):
        readiness(project)


@pytest.mark.parametrize("support", [
    {"supported": False, "writable": True, "cgroup_kill": True},
    {"supported": True, "writable": False, "cgroup_kill": True},
    {"supported": True, "writable": True, "cgroup_kill": False},
    {"supported": "true", "writable": True, "cgroup_kill": True, "private_path": "/private/service/cgroup"},
])
def test_linux_browser_requires_all_readonly_cgroup_hints_without_exposing_paths(
        local_routes, monkeypatch, support):
    from max_postiz_agent import route_readiness
    from max_postiz_agent import browser_process

    project, publishing = local_routes
    target = publishing["targets"][2]
    target.update(enabled=True, destination="123456")
    target["options"].update(owner="123456", dom_contract_profile="personal-text-v1", dom_contract_verified=True)
    (project / "data/vk-browser").mkdir()
    configure(project, publishing, target)
    monkeypatch.setattr(route_readiness, "_LINUX", True)
    # Restore the real filtering wrapper; the platform status itself is a local fixture.
    monkeypatch.setattr(route_readiness, "_linux_support", real_linux_support)
    monkeypatch.setattr(browser_process, "linux_runtime_status", lambda: support)
    monkeypatch.setattr(route_readiness.importlib.util, "find_spec", lambda name: object())
    before = snapshot(project)
    result = readiness(project)
    assert snapshot(project) == before
    row = result["targets"][0]
    assert "BROWSER_CGROUP_DELEGATION_REQUIRED" in row["blockers"]
    assert row["local_prerequisites_passed"] is False and result["production_ready"] is False
    assert set(row["linux_runtime"]) == {"supported", "writable", "cgroup_kill", "delegation_exercised"}
    assert all(type(value) is bool for value in row["linux_runtime"].values())
    assert row["linux_runtime"]["delegation_exercised"] is False
    assert "/private" not in json.dumps(result)


def test_linux_readonly_hints_never_claim_exercised_cleanup_or_production_ready(local_routes, monkeypatch):
    from max_postiz_agent import route_readiness
    from max_postiz_agent import browser_process

    project, publishing = local_routes
    target = publishing["targets"][2]
    target.update(enabled=True, destination="123456")
    target["options"].update(owner="123456", dom_contract_profile="personal-text-v1", dom_contract_verified=True)
    (project / "data/vk-browser").mkdir()
    configure(project, publishing, target)
    monkeypatch.setattr(route_readiness, "_LINUX", True)
    monkeypatch.setattr(route_readiness, "_linux_support", real_linux_support)
    monkeypatch.setattr(browser_process, "linux_runtime_status", lambda: {
        "supported": True, "writable": True, "cgroup_kill": True,
        "delegation_exercised": True, "private_path": "/private/service/cgroup"})
    monkeypatch.setattr(route_readiness.importlib.util, "find_spec", lambda name: object())
    result = readiness(project)
    row = result["targets"][0]
    assert row["local_prerequisites_passed"] is True
    assert row["linux_runtime"]["delegation_exercised"] is False
    assert row["browser_process_cleanup"] == "not_exercised"
    assert row["access"] == row["delivery"] == "not_checked" and result["production_ready"] is False
    assert "/private" not in json.dumps(result)


def test_linux_status_io_failure_is_a_safe_false_hint(monkeypatch):
    from max_postiz_agent import browser_process

    def unavailable():
        raise OSError("/private/service/cgroup")
    monkeypatch.setattr(browser_process, "linux_runtime_status", unavailable)
    assert real_linux_support() == {
        "supported": False, "writable": False, "cgroup_kill": False, "delegation_exercised": False}
