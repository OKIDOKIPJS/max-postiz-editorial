"""The final mutation fence rereads disk config and live database authority."""

import json
from copy import deepcopy

import pytest

from conftest import write_topic
from max_postiz_agent.config import AgentError, Settings
from max_postiz_agent.db import Draft, now
from max_postiz_agent.hub import HubService, route_runtime_generation
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.platform_management import inspect_target
from max_postiz_agent.routes_cli import store_token
from max_postiz_agent.server_state import ServerState
from max_postiz_agent.telegram_publisher import TelegramPublisher
from test_hub import approve, variants

pytest_plugins = ["test_four_platform_hub"]


def active(core):
    ident = approve(core, variants(core)["telegram"])
    with core.db.session.begin() as session:
        row = session.get(Dispatch, ident)
        row.state, row.owner, row.lease_until = "sending", core.worker_id, now() + 600
    with core.db.session() as session:
        return session.get(Dispatch, ident)


def fence(core, original):
    return core.authorize_dispatch_fence(original.id, expected_owner=original.owner,
                                         expected_snapshot_hash=original.snapshot_hash)


def test_final_fence_is_read_only_and_never_repeats_quality_or_transport(four_hub, monkeypatch):
    original = active(four_hub)

    def forbidden(*args, **kwargs):
        raise AssertionError("Final authority reread must not perform expensive quality/media/transport")

    monkeypatch.setattr(four_hub, "_quality", forbidden)
    monkeypatch.setattr(four_hub.routes, "validate", forbidden)
    assert fence(four_hub, original) == original.owner
    with four_hub.db.session() as session:
        row = session.get(Dispatch, original.id)
        assert row.state == "sending" and row.owner == original.owner and row.attempts == original.attempts
    assert four_hub.fixture_publishers["telegram"].publications == []


@pytest.mark.parametrize("change", ["target_destination", "target_enabled", "target_options", "missing_target",
                                    "app", "topic", "sources", "invalid_config"])
def test_disk_config_change_without_sync_rejects_existing_authority(four_hub, change):
    core, original = four_hub, active(four_hub)
    root = core.settings.root
    with core.db.session() as session:
        original_binding = session.get(Target, "telegram").binding_hash
    if change.startswith("target_") or change == "missing_target":
        path = root / "config/publishing.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        target = next(item for item in value["targets"] if item["id"] == "telegram")
        if change == "target_destination":
            target["destination"] = "-1009999999999"
        elif change == "target_enabled":
            target["enabled"] = False
        elif change == "target_options":
            target["options"] = {**target["options"], "token_env": "NEW_FIXTURE_TOKEN"}
        else:
            value["targets"].remove(target)
        path.write_text(json.dumps(value), encoding="utf-8")
    elif change == "topic":
        value = deepcopy(core.settings.topic)
        value["channels"][0]["theme"] += " Новая тема"
        write_topic(root, value)
    else:
        path = root / ("config/sources.json" if change == "sources" else "config/app.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        if change == "sources":
            value["sources"][0]["enabled"] = False
        elif change == "app":
            value["api"]["port"] += 1
        else:
            path.write_text("{invalid configuration", encoding="utf-8")
        if change != "invalid_config":
            path.write_text(json.dumps(value), encoding="utf-8")
        if change == "app":
            # Settings.hash omits this app field; the separate app fingerprint is essential.
            assert Settings.load(root).hash == core.settings.hash
    with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
        fence(core, original)
    with core.db.session() as session:
        assert session.get(Target, "telegram").binding_hash == original_binding
        assert session.get(Dispatch, original.id).state == "sending"
    assert core.fixture_publishers["telegram"].publications == []


@pytest.mark.parametrize("change,code", [
    ("target_pause", "TARGET_PAUSED"), ("global_pause", "EDITORIAL_PAUSED"),
    ("approval_revoke", "APPROVAL_STALE"), ("lease_headroom", "ROUTE_DISPATCH_LEASE_LOST"),
    ("owner", "ROUTE_DISPATCH_LEASE_LOST"), ("state", "ROUTE_DISPATCH_LEASE_LOST"),
    ("snapshot", "APPROVAL_STALE"), ("draft_revision", "APPROVAL_STALE"),
])
def test_api_callback_rechecks_changes_that_happen_during_quality(four_hub, monkeypatch, change, code):
    core, original = four_hub, active(four_hub)
    validate = core._valid_dispatch

    def changed_after_quality(session, row):
        result = validate(session, row)
        if change == "global_pause":
            ServerState(core).initialize()
        else:
            with core.db.session.begin() as other:
                current = other.get(Dispatch, row.id)
                if change == "target_pause":
                    other.get(Target, "telegram").paused = True
                elif change == "approval_revoke":
                    other.get(TargetApproval, current.approval_id).revoked = True
                elif change == "lease_headroom":
                    current.lease_until = now() + 2.5
                elif change == "owner":
                    current.owner = "other-worker"
                elif change == "state":
                    current.state = "unknown"
                elif change == "snapshot":
                    current.snapshot = {**current.snapshot, "body": "Неодобренное тело"}
                else:
                    variant = other.get(Variant, current.variant_id)
                    other.get(Draft, variant.draft_id).revision += 1
        return result

    monkeypatch.setattr(core, "_valid_dispatch", changed_after_quality)
    with pytest.raises(AgentError, match=code):
        core.routes._authorize(original)
    assert core.fixture_publishers["telegram"].publications == []


def test_old_worker_does_not_cancel_new_configuration_approval(four_hub):
    old = four_hub
    topic = deepcopy(old.settings.topic)
    topic["channels"][0]["theme"] += " Новый профиль"
    write_topic(old.settings.root, topic)
    current = HubService(Settings.load(old.settings.root), old.db, editor=old.editor,
                         publisher=old.publisher, http=old.http, route_publishers=old.fixture_publishers)
    try:
        current.activate()
        current.pause_target("telegram", False)
        ident = approve(current, variants(current)["telegram"])
        with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
            old.publish_due(platform_routes_only=True)
        with old.db.session() as session:
            row = session.get(Dispatch, ident)
            assert row.state == "pending" and row.attempts == 0 and row.owner is None
            assert session.get(TargetApproval, row.approval_id).revoked is False
        assert old.fixture_publishers["telegram"].publications == []
        assert current.publish_due(platform_routes_only=True) == 1
    finally:
        current.routes.close()
        current.postiz.close()


def test_token_rotation_and_resume_cannot_reuse_cached_old_token(four_hub, monkeypatch):
    import httpx

    old_token = "123456:old_fixture_token_1234567890"
    new_token = "123456:new_fixture_token_1234567890"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", old_token)
    calls = []

    def forbidden(request):
        calls.append(request)
        raise AssertionError("An outdated credential generation must never reach HTTP")

    adapter = TelegramPublisher(four_hub.settings.root, {"adapter_mode": "live"},
                                 transport=httpx.MockTransport(forbidden))
    four_hub.routes.injected["telegram"] = adapter
    try:
        ident = approve(four_hub, variants(four_hub)["telegram"])
        result = store_token(four_hub, "telegram", new_token)
        assert result["paused"] is True and result["restart_required"] is True
        four_hub.pause_target("telegram", False)
        with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
            inspect_target(four_hub, "telegram")
        with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
            four_hub.publish_due(platform_routes_only=True)
        with four_hub.db.session() as session:
            row = session.get(Dispatch, ident)
            assert row.state == "pending" and row.owner is None and row.attempts == 0
        assert adapter._token == old_token and calls == []
        assert not (four_hub.settings.root / "data/telegram-deliveries").exists()
        # Config activation is not a process restart and must not reset this fence.
        four_hub.activate()
        four_hub.pause_target("telegram", False)
        with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
            four_hub.publish_due(platform_routes_only=True)
        assert calls == []
    finally:
        adapter.close()


def test_direct_runtime_restart_guard_does_not_change_legacy_max_dispatch(four_hub):
    ident = approve(four_hub, variants(four_hub)["max"])
    (four_hub.settings.root / ".env.local").write_text("FIXTURE_VALUE=changed\n", encoding="utf-8")
    assert four_hub.publish_due() == 1
    with four_hub.db.session() as session:
        assert session.get(Dispatch, ident).state == "published"


def test_opaque_runtime_generation_is_captured_and_not_reset_by_activation(four_hub):
    initial = four_hub.route_runtime_generation()
    assert len(initial) == 64 and int(initial, 16) >= 0
    assert route_runtime_generation(four_hub.settings.root) == initial
    (four_hub.settings.root / ".env.local").write_text(
        "FIXTURE_SECRET=private_generation_fixture_value\n", encoding="utf-8")
    changed = route_runtime_generation(four_hub.settings.root)
    assert changed != initial and four_hub.route_runtime_generation() == initial
    four_hub.activate()
    assert four_hub.route_runtime_generation() == initial
    with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
        four_hub._assert_current_route_runtime()


def test_registry_binds_actual_browser_adapter_to_parent_generation_without_inspection(four_hub):
    four_hub.routes.injected.pop("pikabu")
    with four_hub.db.session() as session:
        target = session.get(Target, "pikabu")
    adapter = four_hub.routes.publisher(target)
    assert adapter.runtime_generation == four_hub.route_runtime_generation()
    assert "runtime_generation" not in four_hub.routes.status(target)
