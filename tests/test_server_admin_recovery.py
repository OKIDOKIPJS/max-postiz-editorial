"""Exact recovery across file rename / DB commit, using only synthetic state."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from max_postiz_agent import server_route_admin as admin
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Target
from max_postiz_agent.panel_files import browse, selected
from max_postiz_agent.server_state import ServerState
from test_server_route_admin import admin_core as admin_core, client as client
from test_server_route_admin import binding, configure, post, postiz_get, request


TOKEN = "123456789:fixture_secret_00000000000000000"


def change(core, kind):
    return (configure(core, destination="-100999") if kind == "configure_target"
            else request(core, "set_secret", value=TOKEN))


def filename(core, kind):
    return core.settings.root / ("config/publishing.json" if kind == "configure_target" else ".env.local")


def contents(path):
    return path.read_bytes() if path.exists() else None


def fail_commit_once(core, phase):
    armed = [True]
    def fail(session):
        if armed[0]:
            armed[0] = False
            raise RuntimeError("synthetic commit boundary")
    factory = core.db.session
    event.listen(factory, phase, fail)
    return factory, fail


@pytest.mark.parametrize("kind", ["configure_target", "set_secret"])
def test_commit_failure_before_apply_restores_exact_old_file_and_keeps_history(admin_core, client, monkeypatch, kind):
    core = admin_core
    value = change(core, kind)
    path = filename(core, kind)
    old_bytes, old_binding = contents(path), binding(core, "telegram")
    factory, failure = fail_commit_once(core, "before_commit")
    try:
        response = post(client, value)
    finally:
        event.remove(factory, "before_commit", failure)
    assert response.status_code == 503 and response.json()["code"] == "ROUTE_ADMIN_WRITE_FAILED"
    assert contents(path) == old_bytes and binding(core, "telegram") == old_binding
    assert not (core.settings.root / admin.PENDING_PATH).exists()
    with core.db.session() as session:
        assert not list(session.scalars(select(Audit).where(Audit.action.in_(
            {"route_target_configured", "route_credential_change_paused"}))))
        proof = session.scalar(select(Audit).where(Audit.action == "route_admin_file_db_reconciled"))
        assert proof.details["resolution"] == "aborted"
        assert all(target.paused for target in session.scalars(select(Target)))


@pytest.mark.parametrize("kind", ["configure_target", "set_secret"])
def test_exception_after_actual_db_commit_returns_exact_confirmed_result_without_second_write(admin_core, client, monkeypatch, kind):
    core = admin_core
    value = change(core, kind)
    path = filename(core, kind)
    writes = []
    real_write = admin.atomic_write
    def counted(root, relative, raw):
        if relative == str(path.relative_to(root)).replace("\\", "/"):
            writes.append(hashlib.sha256(raw).hexdigest())
        return real_write(root, relative, raw)
    monkeypatch.setattr(admin, "atomic_write", counted)
    factory, failure = fail_commit_once(core, "after_commit")
    try:
        response = post(client, value)
    finally:
        event.remove(factory, "after_commit", failure)
    assert response.status_code == 200, response.text
    assert response.json()["target_id"] == "telegram" and len(writes) == 1
    assert not (core.settings.root / admin.PENDING_PATH).exists()
    with core.db.session() as session:
        audits = list(session.scalars(select(Audit).where(Audit.action.in_(
            {"route_target_configured", "route_credential_change_paused"}))))
        assert len(audits) == 1 and len(audits[0].details["operation_nonce"]) == 32
        assert session.scalar(select(Audit).where(Audit.action == "route_admin_file_db_reconciled")).details["resolution"] == "committed"


@pytest.mark.parametrize("kind", ["configure_target", "set_secret"])
def test_failure_after_real_rename_is_recovered_without_guessing_old_db_state(admin_core, client, monkeypatch, kind):
    core = admin_core
    value = change(core, kind)
    path = filename(core, kind)
    old_bytes, old_binding = contents(path), binding(core, "telegram")
    replace = admin.os.replace
    armed = [True]
    def after_rename(source, destination):
        result = replace(source, destination)
        if Path(destination) == path and armed[0]:
            armed[0] = False
            raise OSError("synthetic directory fsync failure after rename")
        return result
    monkeypatch.setattr(admin.os, "replace", after_rename)
    response = post(client, value)
    assert response.status_code == 503, response.text
    assert contents(path) == old_bytes and binding(core, "telegram") == old_binding
    assert not (core.settings.root / admin.PENDING_PATH).exists()


def leave_unresolved(core, client, monkeypatch, kind):
    value = change(core, kind)
    original = core.db.session
    armed = [True]
    def fail(session):
        if armed[0]:
            armed[0] = False
            raise RuntimeError("synthetic original commit failure")
    class UnavailableRecovery:
        def begin(self):
            return original.begin()
        def __call__(self):
            raise OperationalError("synthetic", {}, RuntimeError("offline"))
    event.listen(original, "before_commit", fail)
    monkeypatch.setattr(core.db, "session", UnavailableRecovery())
    try:
        response = post(client, value)
    finally:
        event.remove(original, "before_commit", fail)
        monkeypatch.setattr(core.db, "session", original)
    assert response.status_code == 409 and response.json()["code"] == "ROUTE_ADMIN_RECOVERY_REQUIRED"
    assert (core.settings.root / admin.PENDING_PATH).is_file()
    return value


@pytest.mark.parametrize("kind", ["configure_target", "set_secret"])
def test_unavailable_fresh_db_preserves_intent_then_next_explicit_action_repairs_exactly(admin_core, client, monkeypatch, kind):
    core = admin_core
    value = leave_unresolved(core, client, monkeypatch, kind)
    pending = (core.settings.root / admin.PENDING_PATH).read_bytes()
    before_setup = core.db.export()
    status = admin.setup(core)
    assert status["restart"]["required"] is True
    assert "ROUTE_ADMIN_RECOVERY_REQUIRED" in status["restart"]["blocked_reasons"]
    assert (core.settings.root / admin.PENDING_PATH).read_bytes() == pending
    assert core.db.export() == before_setup
    with pytest.raises(AgentError, match="ROUTE_ADMIN_RECOVERY_REQUIRED"):
        core.pause_target("telegram", False)
    store = ServerState(core)
    with pytest.raises(AgentError, match="ROUTE_ADMIN_RECOVERY_REQUIRED"):
        store.control("start", store.read()["revision"], "fixture-owner")
    response = post(client, value)
    assert response.status_code == 200, response.text
    assert not (core.settings.root / admin.PENDING_PATH).exists()
    with core.db.session() as session:
        assert len(list(session.scalars(select(Audit).where(Audit.action.in_(
            {"route_target_configured", "route_credential_change_paused"}))))) == 1
    # No token enters any intent/outcome/pointer JSON, including request cache.
    for path in (core.settings.root / admin.PRIVATE_DIR).rglob("*.json"):
        assert TOKEN not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["configure_target", "set_secret"])
def test_external_file_change_is_never_overwritten_or_removed_during_recovery(admin_core, client, monkeypatch, kind):
    core = admin_core
    value = leave_unresolved(core, client, monkeypatch, kind)
    path = filename(core, kind)
    if kind == "configure_target":
        content = json.loads(path.read_text(encoding="utf-8"))
        content["targets"][0]["destination"] = "-999999"
        external = json.dumps(content).encode()
    else:
        external = path.read_bytes() + b"EXTERNAL_OWNER_VALUE=preserve\n"
    path.write_bytes(external)
    before = core.db.export()
    for _ in range(2):
        response = post(client, value)
        assert response.status_code == 409 and response.json()["code"] == "ROUTE_ADMIN_RECOVERY_REQUIRED"
        assert path.read_bytes() == external
        assert (core.settings.root / admin.PENDING_PATH).exists()
        assert core.db.export() == before


def test_restart_request_can_repair_secret_intent_without_resending_or_reapplying_secret(admin_core, client, monkeypatch):
    core = admin_core
    leave_unresolved(core, client, monkeypatch, "set_secret")
    value = {"action": "request_restart", "id": "server", "expected_binding_hash": None,
             "expected_config_hash": admin.setup(core)["config_hash"]}
    response = post(client, value)
    assert response.status_code == 200, response.text
    assert not (core.settings.root / ".env.local").exists()
    assert not (core.settings.root / admin.PENDING_PATH).exists()
    assert response.json()["state"] == "restart_requested"


def test_identical_secret_bytes_and_target_state_are_disambiguated_by_exact_nonce_audit(admin_core, client, monkeypatch):
    core = admin_core
    path = core.settings.root / ".env.local"
    before = ("TELEGRAM_BOT_TOKEN=" + TOKEN + "\n").encode()
    path.write_bytes(before)
    value = change(core, "set_secret")
    factory, failure = fail_commit_once(core, "after_commit")
    try:
        response = post(client, value)
    finally:
        event.remove(factory, "after_commit", failure)
    assert response.status_code == 200 and path.read_bytes() == before
    with core.db.session() as session:
        proof = session.scalar(select(Audit).where(Audit.action == "route_admin_file_db_reconciled"))
        assert proof.details["resolution"] == "committed"


def test_first_target_creation_commit_failure_restores_file_and_does_not_create_target(admin_core, client, monkeypatch):
    core = admin_core
    postiz_get(core)
    path = core.settings.root / "config/publishing.json"
    before = path.read_bytes()
    value = {"action": "configure_target", "id": "new_target", "expected_binding_hash": None,
        "expected_config_hash": admin.setup(core)["config_hash"], "target": {"platform": "telegram", "route": "telegram_api",
            "profile_id": "main", "destination": "-100789", "enabled": True, "options": {}}}
    factory, failure = fail_commit_once(core, "before_commit")
    try:
        response = post(client, value)
    finally:
        event.remove(factory, "before_commit", failure)
    assert response.status_code == 503 and path.read_bytes() == before
    with core.db.session() as session:
        assert session.get(Target, "new_target") is None
    assert not (core.settings.root / admin.PENDING_PATH).exists()


def test_durable_backup_corruption_blocks_recovery_without_touching_candidate(admin_core, client, monkeypatch):
    core = admin_core
    value = leave_unresolved(core, client, monkeypatch, "configure_target")
    path = filename(core, "configure_target")
    candidate = path.read_bytes()
    pointer = json.loads((core.settings.root / admin.PENDING_PATH).read_text(encoding="utf-8"))
    backup = core.settings.root / admin.PRIVATE_DIR / pointer["nonce"] / "old.bytes"
    backup.write_bytes(b"untrusted replacement")
    response = post(client, value)
    assert response.status_code == 409 and response.json()["code"] == "ROUTE_ADMIN_RECOVERY_REQUIRED"
    assert path.read_bytes() == candidate and (core.settings.root / admin.PENDING_PATH).exists()


def test_exact_nonce_with_changed_audit_payload_cannot_confirm_or_roll_back(admin_core, client, monkeypatch):
    core = admin_core
    value = change(core, "configure_target")
    original = admin._finish_intent
    def unavailable(*args, **kwargs):
        raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
    monkeypatch.setattr(admin, "_finish_intent", unavailable)
    response = post(client, value)
    assert response.status_code == 409
    monkeypatch.setattr(admin, "_finish_intent", original)
    with core.db.session.begin() as session:
        row = session.scalar(select(Audit).where(Audit.action == "route_target_configured"))
        row.details = {**row.details, "new_file_sha256": "f" * 64}
    candidate = filename(core, "configure_target").read_bytes()
    response = post(client, value)
    assert response.status_code == 409 and response.json()["code"] == "ROUTE_ADMIN_RECOVERY_REQUIRED"
    assert filename(core, "configure_target").read_bytes() == candidate
    assert (core.settings.root / admin.PENDING_PATH).exists()


@pytest.mark.parametrize("purpose", ["file", "workflow", "private", "video"])
def test_new_control_and_private_backup_paths_cannot_be_browsed_or_selected(admin_core, purpose):
    root = admin_core.settings.root
    for relative in (admin.PRIVATE_DIR + "/fixture/intent.json", "data/owner-browser-selection.json",
                     "data/editorial-restart-claims/fixture.json"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
            selected(admin_core, relative, purpose=purpose)
    with pytest.raises(AgentError, match="FILE_DIRECTORY_PRIVATE"):
        browse(admin_core, admin.PRIVATE_DIR, purpose=purpose)
