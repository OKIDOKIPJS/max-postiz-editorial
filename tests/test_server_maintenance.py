"""Maintenance/restart interlocks never clear uncertain deliveries or model claims."""

import copy
import json

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Job, now
from max_postiz_agent.hub_models import Dispatch, Target
from max_postiz_agent.server_models import EditorialControl, EditorialPreparationClaim
from max_postiz_agent.server_maintenance import RESTART_PATH, remote_queue_guard, validate_restart_selection
from max_postiz_agent.server_route_admin import setup
from max_postiz_agent.server_state import ServerState
from test_server_route_admin import admin_core as admin_core, client as client
from test_server_route_admin import configure, post, postiz_get, request, seed_dispatch


def restart(core):
    return {"action": "request_restart", "id": "server", "expected_binding_hash": None,
            "expected_config_hash": setup(core)["config_hash"]}


def test_settings_require_global_pause_before_file_write(admin_core, client):
    core = admin_core
    with core.db.session.begin() as session:
        session.get(EditorialControl, "main").paused = False
    before = (core.settings.root / "config/publishing.json").read_bytes()
    for body in (configure(core), request(core, "set_secret", value="123456789:fixture_secret_00000000000000000"), restart(core)):
        response = post(client, body)
        assert response.status_code == 409 and response.json()["code"] == "EDITORIAL_PAUSE_BEFORE_SETTINGS"
    assert (core.settings.root / "config/publishing.json").read_bytes() == before
    assert not (core.settings.root / ".env.local").exists()
    assert not (core.settings.root / RESTART_PATH).exists()


@pytest.mark.parametrize("kind", ["model_job", "model_claim", "sending_other", "other_lease"])
def test_global_active_claims_block_restart_and_secret_without_clearing_them(admin_core, client, kind):
    core = admin_core
    if kind in {"sending_other", "other_lease"}:
        seed_dispatch(core, "sending" if kind == "sending_other" else "unknown",
                      **({"owner": "fixture-owner", "lease_until": now() + 60} if kind == "other_lease" else {}))
    else:
        with core.db.session.begin() as session:
            if kind == "model_job":
                session.add(Job(key="fixture", kind="edit", subject_id="fixture", config_id=core.settings.hash,
                                state="leased", due_at=now(), owner="fixture-owner", lease_until=now() + 60))
            else:
                session.add(EditorialPreparationClaim(control_revision=1, active_key="models", state="preparing"))
    response = post(client, restart(core))
    assert response.status_code == 409 and response.json()["code"] == "EDITORIAL_MAINTENANCE_BUSY"
    assert not (core.settings.root / RESTART_PATH).exists()
    if kind in {"model_job", "model_claim"}:
        response = post(client, request(core, "set_secret", value="123456789:fixture_secret_00000000000000000"))
        assert response.status_code == 409 and not (core.settings.root / ".env.local").exists()
    with core.db.session() as session:
        if kind == "model_job":
            assert session.scalar(select(Job)).owner == "fixture-owner"
        elif kind == "model_claim":
            assert session.scalar(select(EditorialPreparationClaim)).active_key == "models"


def test_restart_preserves_closed_unknown_and_only_writes_fixed_private_manifest(admin_core, client):
    core = admin_core
    seed_dispatch(core, "unknown", attempts=1)
    with core.db.session() as session:
        row = session.get(Dispatch, "fixture-dispatch")
        before = copy.deepcopy({key: getattr(row, key) for key in ("state", "snapshot", "snapshot_hash", "attempts", "owner", "lease_until")})
        targets = {target.id: (target.binding_hash, target.paused, target.options) for target in session.scalars(select(Target))}
    response = post(client, restart(core))
    assert response.status_code == 200, response.text
    manifest = json.loads((core.settings.root / RESTART_PATH).read_text(encoding="utf-8"))
    assert manifest["previous_worker_id"] == core.worker_id and manifest["request_id"] == response.json()["request_id"]
    assert validate_restart_selection(core, manifest) == manifest
    with core.db.session() as session:
        row = session.get(Dispatch, "fixture-dispatch")
        assert before == {key: getattr(row, key) for key in before}
        assert targets == {target.id: (target.binding_hash, target.paused, target.options) for target in session.scalars(select(Target))}
    assert post(client, restart(core)).json()["code"] == "RESTART_ALREADY_REQUESTED"
    for changed in ({"root": str(core.settings.root.parent)}, {"runtime_generation": "a" * 64},
                    {"expires_at": int(now()) - 1}, {"previous_worker_id": "not-a-worker"}, {"unit": "other.service"}):
        with pytest.raises(AgentError):
            validate_restart_selection(core, {**manifest, **changed})


def test_readonly_postiz_near_queue_prevents_restart_and_config_without_remote_mutation(admin_core, client, monkeypatch):
    core = admin_core
    core.postiz.base, core.postiz.key = "http://127.0.0.1/fixture", "fixture"
    calls = []
    monkeypatch.setattr(core.postiz, "posts", lambda *args: calls.append(args) or [
        {"state": "QUEUE", "publishDate": "2020-01-01T00:00:00Z"}])
    before = (core.settings.root / "config/publishing.json").read_bytes()
    for value in (restart(core), configure(core)):
        response = post(client, value)
        assert response.status_code == 409 and response.json()["code"] == "POSTIZ_NEAR_QUEUE_RESTART_BLOCKED"
    assert len(calls) == 2
    assert (core.settings.root / "config/publishing.json").read_bytes() == before
    assert not (core.settings.root / RESTART_PATH).exists()


def test_restart_required_survives_setup_reload_after_rotation(admin_core, client):
    assert setup(admin_core)["restart"]["required"] is False
    response = post(client, request(admin_core, "set_secret", value="123456789:fixture_secret_00000000000000000"))
    assert response.status_code == 200
    assert setup(admin_core)["restart"] == {"required": True, "can_request": True,
        "blocked_reasons": ["POSTIZ_OFFLINE_FIXTURE_QUEUE_NOT_CHECKED"]}
    store = ServerState(admin_core)
    with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
        store.control("start", store.read()["revision"], "fixture-owner")
    assert store.read()["paused"] is True


def test_restart_helper_rechecks_pause_after_request_was_persisted(admin_core, client):
    assert post(client, restart(admin_core)).status_code == 200
    manifest = json.loads((admin_core.settings.root / RESTART_PATH).read_text(encoding="utf-8"))
    with admin_core.db.session.begin() as session:
        session.get(EditorialControl, "main").paused = False
    with pytest.raises(AgentError, match="EDITORIAL_PAUSE_BEFORE_SETTINGS"):
        validate_restart_selection(admin_core, manifest)


def test_restart_request_fences_start_resume_and_reconfiguration(admin_core, client):
    assert post(client, restart(admin_core)).status_code == 200
    store = ServerState(admin_core)
    with pytest.raises(AgentError, match="RESTART_ALREADY_REQUESTED"):
        store.control("start", store.read()["revision"], "fixture-owner")
    with pytest.raises(AgentError, match="RESTART_ALREADY_REQUESTED"):
        admin_core.pause_target("telegram", False)
    response = post(client, configure(admin_core))
    assert response.status_code == 409 and response.json()["code"] == "RESTART_ALREADY_REQUESTED"
    assert store.read()["paused"] is True


def test_offline_queue_skip_requires_explicit_fresh_fixture_marker_and_has_no_clear_claim(admin_core):
    core = admin_core
    assert remote_queue_guard(core) is False
    assert "POSTIZ_OFFLINE_FIXTURE_QUEUE_NOT_CHECKED" in setup(core)["restart"]["blocked_reasons"]
    path = core.settings.root / "config/publishing.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["postiz"].pop("maintenance_offline_fixture")
    path.write_text(json.dumps(value), encoding="utf-8")
    # Captured publishing still has the flag; the disk configuration is authority.
    assert core.publishing["postiz"]["maintenance_offline_fixture"] is True
    with pytest.raises(AgentError, match="POSTIZ_NOT_CONFIGURED"):
        remote_queue_guard(core)


@pytest.mark.parametrize("target_id,option", [("telegram", {"adapter_mode": "live"}),
                                            ("vk", {"dom_contract_verified": True})])
def test_fresh_live_target_cannot_inherit_an_offline_queue_skip(admin_core, target_id, option):
    path = admin_core.settings.root / "config/publishing.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    next(target for target in value["targets"] if target["id"] == target_id)["options"].update(option)
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(AgentError, match="POSTIZ_NOT_CONFIGURED"):
        remote_queue_guard(admin_core)


def test_requested_new_live_target_is_checked_before_first_configuration_write(admin_core, client):
    core = admin_core
    value = {"action": "configure_target", "id": "new_live", "expected_binding_hash": None,
        "expected_config_hash": setup(core)["config_hash"], "target": {"platform": "telegram", "route": "telegram_api",
            "profile_id": "main", "destination": "-100789", "enabled": True, "options": {}}}
    path = core.settings.root / "config/publishing.json"
    before = path.read_bytes()
    response = post(client, value)
    assert response.status_code == 503 and response.json()["code"] == "POSTIZ_NOT_CONFIGURED"
    assert path.read_bytes() == before
    with core.db.session() as session:
        assert session.get(Target, "new_live") is None


@pytest.mark.parametrize("base,key", [("http://127.0.0.1:18099/api/public/v1", ""), ("", "fixture")])
def test_partial_postiz_credentials_never_skip_even_in_explicit_offline_fixture(admin_core, monkeypatch, base, key):
    admin_core.postiz.base, admin_core.postiz.key = base, key
    def no_network(*args, **kwargs):
        pytest.fail("An incomplete configuration must fail before HTTP")
    monkeypatch.setattr(admin_core.postiz.client, "request", no_network)
    with pytest.raises(AgentError, match="POSTIZ_NOT_CONFIGURED"):
        remote_queue_guard(admin_core)


def test_configured_postiz_guard_performs_real_client_get_through_mock_transport(admin_core):
    calls = postiz_get(admin_core)
    assert remote_queue_guard(admin_core) is True
    assert len(calls) == 1 and calls[0].method == "GET"


def test_invalid_postiz_queue_response_blocks_before_any_write(admin_core, client, monkeypatch):
    admin_core.postiz.base, admin_core.postiz.key = "http://127.0.0.1:18099", "fixture"
    monkeypatch.setattr(admin_core.postiz, "posts", lambda *args: [{"state": "QUEUE", "publishDate": "not-a-date"}])
    path = admin_core.settings.root / "config/publishing.json"
    before = path.read_bytes()
    response = post(client, configure(admin_core))
    assert response.status_code == 409 and response.json()["code"] == "POSTIZ_RESPONSE_INVALID"
    assert path.read_bytes() == before
