"""Серверный owner API: три канала, synthetic SQLite/media, без транспорта."""

import base64
import hashlib
import io
import json

import pytest
import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, DraftRevision, Story, now
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant, VariantRevision
from max_postiz_agent.owner_original import require_attestation
from max_postiz_agent.server_api import editorial_app
from max_postiz_agent.server_route_admin import LOGIN_PATH, setup, validate_login_selection
from max_postiz_agent.server_state import ServerState
from conftest import write_topic


@pytest.fixture
def admin_core(service, monkeypatch):
    topic = service.settings.topic
    topic["channels"][0]["publication"]["mode"] = "review"
    write_topic(service.settings.root, topic)
    from max_postiz_agent.config import Settings
    service.activate(Settings.load(service.settings.root))
    config = {"schema_version": "1.0", "postiz": {"maintenance_offline_fixture": True}, "tasks": [], "targets": [
        {"id": "max", "profile_id": "main", "platform": "max", "route": "max_browser", "enabled": True,
         "destination": "-123", "options": {"max_chars": 3500}},
        {"id": "telegram", "profile_id": "main", "platform": "telegram", "route": "telegram_api", "enabled": True,
         "destination": "-100123", "options": {"adapter_mode": "fake", "max_chars": 4096}},
        {"id": "vk", "profile_id": "main", "platform": "vk", "route": "vk_browser", "enabled": True,
         "destination": "123456", "options": {"owner": "123456", "profile_dir": "data/vk-browser-test",
          "dom_contract_profile": "personal-text-v1", "dom_contract_verified": False, "max_chars": 4096}}]}
    (service.settings.root / "config/publishing.json").write_text(json.dumps(config), encoding="utf-8")
    core = HubService(service.settings, service.db, editor=service.editor, publisher=service.publisher, http=service.http)
    ServerState(core).initialize()
    monkeypatch.setenv("EDITORIAL_INTERNAL_KEY", "fixture-owner-admin-key-000000000000000000000")
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "fixture_owner")
    monkeypatch.setenv("EDITORIAL_OWNER_ORGANIZATION_ID", "fixture_org")
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_PROXY_URL", "MAX_EXISTING_BROWSER_PROFILE",
                 "POSTIZ_BASE_URL", "POSTIZ_API_KEY", "POSTIZ_PUBLIC_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    yield core
    core.postiz.close()


@pytest.fixture
def client(admin_core):
    return TestClient(editorial_app(admin_core)), {"X-Editorial-Key": "fixture-owner-admin-key-000000000000000000000",
        "X-Editorial-Owner": "fixture_owner", "X-Editorial-Organization": "fixture_org"}


def binding(core, ident):
    with core.db.session() as session:
        return session.get(Target, ident).binding_hash


def request(core, action, ident="telegram", **extra):
    return {"action": action, "id": ident, "expected_binding_hash": binding(core, ident), **extra}


def post(client, value):
    app, headers = client
    return app.post("/internal/editorial/route-admin", headers=headers, json=value)


def original(body="Оригинальный текст владельца о научной работе и новых открытиях. Это собственная заметка, а не перевод."):
    return {"title": "Авторская заметка", "body": body, "media": [], "metadata": {}}


def create(core, client, ident="telegram"):
    response = post(client, request(core, "create_original", ident, original=original(), attest_original=True))
    assert response.status_code == 200, response.text
    return response.json()


def configure(core, ident="telegram", **updates):
    values = json.loads((core.settings.root / "config/publishing.json").read_text())
    current = next(row for row in values["targets"] if row["id"] == ident)
    target = {key: current[key] for key in ("profile_id", "platform", "route", "destination", "enabled")}
    target["options"] = {"max_chars": 3500}
    target.update(updates)
    return request(core, "configure_target", ident, expected_config_hash=setup(core)["config_hash"], target=target)


def postiz_get(core):
    calls = []
    def respond(request):
        assert request.method == "GET" and request.url.path == "/api/public/v1/posts"
        calls.append(request)
        return httpx.Response(200, json={"posts": []})
    core.postiz.client.close()
    core.postiz.client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False)
    core.postiz.base, core.postiz.key = "http://127.0.0.1:18099/api/public/v1", "fixture-public-api-key"
    return calls


def seed_dispatch(core, state="unknown", **updates):
    # A frozen synthetic row is sufficient for the administrative interlock;
    # no quality, approval, transport, or account is involved.
    with core.db.session.begin() as session:
        target = session.get(Target, "telegram")
        variant = Variant(id="fixture-variant", draft_id="fixture-draft", target_id=target.id,
            title="fixture", body="fixture", source_revision=1, config_hash=core.settings.hash,
            binding_hash=target.binding_hash, sources=[], media=[])
        session.add(variant)
        approval = TargetApproval(id="fixture-approval", variant_id=variant.id, revision=1,
            snapshot_hash="a" * 64, scheduled_at=now() + 3600, actor="fixture", revoked=False)
        session.add(approval)
        session.add(Dispatch(id="fixture-dispatch", variant_id=variant.id, approval_id=approval.id,
            revision=1, snapshot_hash="a" * 64, snapshot={"frozen": True}, state=state,
            due_at=now() + 3600, **updates))


def test_owner_guard_precedes_secret_parsing_and_never_echoes_values(admin_core, client):
    app, headers = client
    value = request(admin_core, "set_secret", value="123456789:fixture_secret_00000000000000000")
    assert app.post("/internal/editorial/route-admin", json=value).status_code == 403
    assert app.get("/internal/editorial/route-setup").status_code == 403
    wrong = {**headers, "X-Editorial-Organization": "other"}
    assert app.post("/internal/editorial/route-admin", headers=wrong, json=value).status_code == 403
    rejected = app.post("/internal/editorial/route-admin", headers=headers, json={**value, "password": value["value"]})
    assert rejected.status_code == 422 and rejected.json() == {"code": "INPUT_INVALID"}
    assert not (admin_core.settings.root / ".env.local").exists()


def test_server_setup_does_not_require_isolated_copy_port_or_marker(admin_core, client):
    app, headers = client
    response = app.get("/internal/editorial/route-setup", headers=headers)
    assert response.status_code == 200
    value = response.json()
    assert {row["platform"] for row in value["targets"]} == {"max", "telegram", "vk"}
    assert value["external_mutations"] == 0
    assert all(set(row["options"]) <= {"owner", "max_chars", "operation_timeout_seconds", "media_layout",
                                       "request_timeout_seconds"} for row in value["targets"])
    assert "dom_contract_verified" not in response.text
    assert not (admin_core.settings.root / "data/isolated-workspace.json").exists()


def test_targeted_configuration_preserves_every_other_target_and_draft_history(admin_core, client):
    core = admin_core
    existing = create(core, client)
    with core.db.session() as session:
        old_revision = session.scalar(select(VariantRevision).where(VariantRevision.variant_id == existing["variant_id"]))
        frozen = json.loads(json.dumps(old_revision.payload))
        other = {row.id: (row.binding_hash, row.paused, row.options) for row in session.scalars(
            select(Target).where(Target.id != "telegram"))}
    value = configure(core, destination="-100456")
    response = post(client, value)
    assert response.status_code == 200, response.text
    assert response.json()["paused"] is True and response.json()["restart_required"] is True
    assert response.json()["binding_hash"] != value["expected_binding_hash"]
    with core.db.session() as session:
        assert other == {row.id: (row.binding_hash, row.paused, row.options) for row in session.scalars(
            select(Target).where(Target.id != "telegram"))}
        assert session.scalar(select(VariantRevision).where(VariantRevision.variant_id == existing["variant_id"])).payload == frozen
    assert post(client, value).json()["code"] == "ROUTE_CONFIG_CONFLICT"
    with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
        core._assert_current_route_runtime()


@pytest.mark.parametrize("options", [{"token_env": "EDITORIAL_INTERNAL_KEY"}, {"dom_contract_verified": True},
    {"profile_dir": "data/max-browser"}, {"dom_contract": {"publish": "button"}}, {"settings": {"token": "secret"}}])
def test_ui_cannot_set_proof_flags_selectors_profiles_or_secret_names(admin_core, client, options):
    response = post(client, configure(admin_core, options=options))
    assert response.status_code == 422 and response.json()["code"] == "ROUTE_ADMIN_OPTIONS_INVALID"


@pytest.mark.parametrize("state,updates", [("sending", {}), ("unknown", {}), ("pending", {}),
    ("failed", {"owner": "stale-owner"}), ("failed", {"lease_until": 9999999999})])
@pytest.mark.parametrize("action", ["configure_target", "set_secret"])
def test_config_and_credentials_fail_closed_with_pending_unknown_or_lease(admin_core, client, state, updates, action):
    core = admin_core
    seed_dispatch(core, state, **updates)
    path = core.settings.root / "config/publishing.json"
    before = path.read_bytes()
    value = configure(core) if action == "configure_target" else request(core, action, value="123456789:fixture_secret_00000000000000000")
    response = post(client, value)
    assert response.status_code == 409 and response.json()["code"] == "CANCEL_OR_RECONCILE_FIRST"
    assert path.read_bytes() == before and not (core.settings.root / ".env.local").exists()
    with core.db.session() as session:
        row = session.get(Dispatch, "fixture-dispatch")
        assert row.state == state and row.snapshot == {"frozen": True}


def test_secret_is_write_only_and_old_runtime_cannot_use_rotated_credentials(admin_core, client):
    core = admin_core
    secret = "123456789:fixture_secret_00000000000000000"
    response = post(client, request(core, "set_secret", value=secret))
    assert response.status_code == 200 and secret not in response.text
    assert response.json() == {"target_id": "telegram", "configured": True, "restart_required": True, "paused": True}
    assert (core.settings.root / ".env.local").read_text() == "TELEGRAM_BOT_TOKEN=" + secret + "\n"
    with core.db.session() as session:
        assert secret not in repr([row.details for row in session.scalars(select(Audit))])
    with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
        core._assert_current_route_runtime()


def test_target_must_already_be_paused_before_credential_or_binding_change(admin_core, client):
    admin_core.pause_target("telegram", False)
    assert post(client, configure(admin_core)).json()["code"] == "TARGET_PAUSE_REQUIRED"
    assert post(client, request(admin_core, "set_secret", value="123456789:fixture_secret_00000000000000000")).json()["code"] == "TARGET_PAUSE_REQUIRED"


def test_upload_exact_content_addressed_bytes_and_no_approval(admin_core, client):
    stream = io.BytesIO()
    Image.new("RGB", (100, 80), "navy").save(stream, "PNG")
    raw = stream.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    response = post(client, request(admin_core, "upload_media", mime="image/png", sha256=digest,
                                   data_base64=base64.b64encode(raw).decode()))
    assert response.status_code == 200, response.text
    media = response.json()["media"]
    assert media == {"path": "data/media/" + digest + ".png", "mime": "image/png", "sha256": digest}
    assert (admin_core.settings.root / media["path"]).read_bytes() == raw
    value = {**original(), "media": [media]}
    created = post(client, request(admin_core, "create_original", original=value, attest_original=True))
    assert created.status_code == 200 and created.json()["approval_created"] is False
    with admin_core.db.session() as session:
        assert list(session.scalars(select(TargetApproval))) == [] and list(session.scalars(select(Dispatch))) == []


@pytest.mark.parametrize("ident", ["max", "telegram", "vk"])
def test_create_and_revise_original_preserve_full_canonical_history(admin_core, client, ident):
    core = admin_core
    created = create(core, client, ident)
    changed = original("Это новая точная редакция авторской заметки. Старый текст и подтверждение прав сохраняются в истории.")
    value = {"action": "revise_original", "id": created["variant_id"], "expected_binding_hash": binding(core, ident),
        "revision": 1, "content_hash": created["content_hash"], "original": changed, "attest_original": True}
    response = post(client, value)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2 and response.json()["approval_created"] is False
    with core.db.session() as session:
        variant = session.get(Variant, created["variant_id"])
        target = session.get(Target, ident)
        assert require_attestation(core, session, variant, target) is True
        histories = list(session.scalars(select(VariantRevision).where(VariantRevision.variant_id == variant.id).order_by(VariantRevision.revision)))
        assert len(histories) == 2 and histories[0].payload["body"] == original()["body"]
        assert histories[1].payload["body"] == changed["body"]
        assert len(list(session.scalars(select(DraftRevision).where(DraftRevision.draft_id == variant.draft_id)))) == 2
        assert len(list(session.scalars(select(Story)))) == 2
        assert not list(session.scalars(select(Dispatch)))
    assert post(client, value).json()["code"] == "REVISION_CONFLICT"


def test_revise_does_not_touch_externally_uncertain_version(admin_core, client):
    created = create(admin_core, client)
    with admin_core.db.session.begin() as session:
        approval = TargetApproval(variant_id=created["variant_id"], revision=1, snapshot_hash="a" * 64,
                                  scheduled_at=now(), actor="fixture")
        session.add(approval)
        session.flush()
        session.add(Dispatch(variant_id=created["variant_id"], approval_id=approval.id, revision=1,
            snapshot_hash="a" * 64, snapshot={"frozen": True}, state="unknown", due_at=now(), attempts=1))
    value = {"action": "revise_original", "id": created["variant_id"], "expected_binding_hash": binding(admin_core, "telegram"),
        "revision": 1, "content_hash": created["content_hash"], "original": original("Новый собственный текст для редактирования."), "attest_original": True}
    response = post(client, value)
    assert response.status_code == 409 and response.json()["code"] == "ORIGINAL_DISPATCH_HISTORY_IMMUTABLE"
    with admin_core.db.session() as session:
        assert session.get(Variant, created["variant_id"]).revision == 1
        assert session.scalar(select(Dispatch)).state == "unknown"


def test_login_manifest_exact_paused_target_profile_and_expiry_without_browser(admin_core, client):
    core = admin_core
    response = post(client, request(core, "prepare_login", "vk"))
    assert response.status_code == 200, response.text
    assert response.json()["login_url"] == "/owner-browser/" and response.json()["live_verified"] is False
    manifest = json.loads((core.settings.root / LOGIN_PATH).read_text(encoding="utf-8"))
    assert manifest["profile_dir"] == "data/vk-browser-test" and manifest["url"] == "https://vk.ru/id123456"
    assert validate_login_selection(core, manifest) == manifest
    assert post(client, request(core, "prepare_login", "vk")).json()["code"] == "LOGIN_SELECTION_ACTIVE"
    assert post(client, configure(core)).json()["code"] == "LOGIN_SELECTION_ACTIVE"
    with pytest.raises(AgentError, match="LOGIN_SELECTION_ACTIVE"):
        core.pause_target("vk", False)
    for key, wrong in (("url", "https://attacker.test"), ("profile_dir", "data/max-browser"),
                       ("expires_at", now() - 1), ("binding_hash", "a" * 64)):
        with pytest.raises(AgentError):
            validate_login_selection(core, {**manifest, key: wrong})
    with core.db.session.begin() as session:
        session.get(Target, "vk").paused = False
    with pytest.raises(AgentError, match="TARGET_PAUSE_REQUIRED"):
        validate_login_selection(core, manifest)


def test_bridge_lease_is_rejected_without_private_reads(admin_core, client):
    core = admin_core
    with core.db.session.begin() as session:
        session.add(BridgePost(post_id="lease", integration_id="fixture", group_id="fixture", target_id="telegram",
            variant_id="fixture", remote_hash="a" * 64, remote_state="DRAFT", due_at=now(), active=True,
            mutation_owner="owner", mutation_until=now() + 60))
    assert post(client, configure(core)).json()["code"] == "BRIDGE_EDIT_IN_PROGRESS"


def test_create_target_uses_dedicated_service_owned_credentials_and_does_not_sync_others(admin_core, client):
    core = admin_core
    calls = postiz_get(core)
    with core.db.session() as session:
        before = {row.id: (row.binding_hash, row.paused, row.options) for row in session.scalars(select(Target))}
    value = {"action": "configure_target", "id": "telegram_new", "expected_binding_hash": None,
        "expected_config_hash": setup(core)["config_hash"], "target": {"platform": "telegram", "route": "telegram_api",
            "profile_id": "main", "destination": "-100789", "enabled": True, "options": {"media_layout": "atomic"}}}
    response = post(client, value)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    with core.db.session() as session:
        added = session.get(Target, "telegram_new")
        assert added.paused is True
        assert added.options["token_env"].startswith("EDITORIAL_TELEGRAM_")
        assert added.options["adapter_mode"] == "live"
        assert before == {row.id: (row.binding_hash, row.paused, row.options) for row in session.scalars(
            select(Target).where(Target.id != added.id))}


def test_pending_original_edit_revokes_only_old_approval_preserving_frozen_snapshot(admin_core, client):
    core = admin_core
    created = create(core, client)
    due = now() + 3600
    with core.db.session.begin() as session:
        variant = session.get(Variant, created["variant_id"])
        snapshot = core.snapshot(variant, session.get(Target, variant.target_id))
        hashed = fingerprint({"content": snapshot, "due_at": due})
        approval = TargetApproval(variant_id=variant.id, revision=1, snapshot_hash=hashed, scheduled_at=due, actor="fixture")
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=variant.id, approval_id=approval.id, revision=1, snapshot_hash=hashed,
                            snapshot=snapshot, state="pending", due_at=due)
        session.add(dispatch)
        session.flush()
        dispatch_id, approval_id = dispatch.id, approval.id
    value = {"action": "revise_original", "id": created["variant_id"], "expected_binding_hash": binding(core, "telegram"),
        "revision": 1, "content_hash": created["content_hash"], "original": original("Новая самостоятельная заметка владельца о научных открытиях и проверке результатов."),
        "attest_original": True}
    response = post(client, value)
    assert response.status_code == 200, response.text
    with core.db.session() as session:
        assert session.get(Dispatch, dispatch_id).snapshot == snapshot
        assert session.get(Dispatch, dispatch_id).state == "cancelled"
        assert session.get(TargetApproval, approval_id).revoked is True
        variant = session.get(Variant, created["variant_id"])
        core._quality(session, variant, session.get(Target, variant.target_id))


def test_original_isolation_guard_and_attestation_are_not_bypassed_by_new_server_entry(admin_core, client):
    from max_postiz_agent.owner_original import create_original
    with pytest.raises(AgentError, match="ISOLATED_COPY_REQUIRED"):
        create_original(admin_core, "telegram", original(), attest_original=True)
    response = post(client, request(admin_core, "create_original", original=original(), attest_original=False))
    assert response.status_code == 403 and response.json()["code"] == "OWNER_ORIGINAL_ATTESTATION_REQUIRED"
    with admin_core.db.session() as session:
        assert not list(session.scalars(select(Variant)))


def test_config_write_error_rolls_back_only_selected_database_change(admin_core, client, monkeypatch):
    from max_postiz_agent import server_route_admin
    core = admin_core
    before = (core.settings.root / "config/publishing.json").read_bytes()
    previous_binding = binding(core, "telegram")
    def failed(*args, **kwargs):
        raise AgentError("ROUTE_ADMIN_WRITE_FAILED", 503)
    monkeypatch.setattr(server_route_admin, "atomic_write", failed)
    response = post(client, configure(core, destination="-100999"))
    assert response.status_code == 503
    assert (core.settings.root / "config/publishing.json").read_bytes() == before
    assert binding(core, "telegram") == previous_binding
    with core.db.session() as session:
        assert session.get(Target, "telegram").paused is True


def test_upload_rejects_arbitrary_bytes_and_hash_mismatch(admin_core, client):
    raw = b"<html>not an image</html>"
    value = request(admin_core, "upload_media", mime="image/png", sha256=hashlib.sha256(raw).hexdigest(),
                    data_base64=base64.b64encode(raw).decode())
    response = post(client, value)
    assert response.status_code == 422 and response.json()["code"] == "MEDIA_CONTENT_INVALID"
    response = post(client, {**value, "sha256": "a" * 64})
    assert response.status_code == 409 and response.json()["code"] == "MEDIA_HASH_CHANGED"


def test_exact_media_policy_is_bound_to_current_target_and_stale_policy_cannot_be_approved(admin_core, client):
    from max_postiz_agent.route_details import details
    core = admin_core
    path = core.settings.root / "config/publishing.json"
    publishing = json.loads(path.read_text(encoding="utf-8"))
    configured = next(row for row in publishing["targets"] if row["id"] == "vk")
    configured["options"]["media_proof_policy"] = "native-uniform-edge-lineage-v1"
    path.write_text(json.dumps(publishing), encoding="utf-8")
    with core.db.session.begin() as session:
        target = session.get(Target, "vk")
        target.options = configured["options"]
        target.binding_hash = fingerprint({"target": configured, "editorial_config": core.settings.hash})
    stream = io.BytesIO()
    Image.new("RGB", (100, 80), "navy").save(stream, "PNG")
    raw = stream.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    uploaded = post(client, request(core, "upload_media", "vk", mime="image/png", sha256=digest,
                                    data_base64=base64.b64encode(raw).decode())).json()
    created = post(client, request(core, "create_original", "vk", original={**original(), "media": [uploaded["media"]]},
                                   attest_original=True)).json()
    exact = details(core, created["variant_id"])
    assert exact["media_policy"]["id"] == "native-uniform-edge-lineage-v1"
    assert exact["media_policy"]["label"] and exact["media_policy"]["description"]
    old_hash = exact["content_hash"]
    configured["options"]["media_proof_policy"] = "exact-bytes-v1"
    path.write_text(json.dumps(publishing), encoding="utf-8")
    with core.db.session.begin() as session:
        target = session.get(Target, "vk")
        target.options = configured["options"]
        target.binding_hash = fingerprint({"target": configured, "editorial_config": core.settings.hash})
    stale = details(core, created["variant_id"])
    # The historical revision/hash is preserved; it cannot inherit a new policy.
    assert stale["content_hash"] == old_hash and stale["media_policy"] is None
    with core.db.session() as session:
        with pytest.raises(AgentError, match="VARIANT_CONFIG_STALE"):
            core._quality(session, session.get(Variant, created["variant_id"]), session.get(Target, "vk"))
