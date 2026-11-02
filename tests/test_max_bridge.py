import copy
import httpx
import io
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import now
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant

pytest_plugins = ["test_hub"]


@pytest.fixture
def bridge(hub, monkeypatch):
    hub.bridge.key = "fixture-bridge-key"
    hub.publishing["postiz"]["browser_bridge_enabled"] = True
    # Model a configured bridge, not a worker whose in-memory config drifted.
    (hub.settings.root / "config/publishing.json").write_text(json.dumps(hub.publishing), encoding="utf-8")
    with hub.db.session.begin() as s:
        target = s.get(Target, "max")
        target.route = "max_browser"
        target.options = {"max_chars": 3500, "postiz_integration_id": "integration-max"}
    due = now() - 1
    remote = {"id": "post-max", "content": "Проверка работы редакции. Это авторский тестовый текст.",
              "publishDate": datetime.fromtimestamp(due, UTC).isoformat(), "state": "QUEUE",
              "image": "[]", "childrenPost": [], "intervalInDays": None, "group": "group-max",
              "integration": {"id": "integration-max", "providerIdentifier": "max-browser"},
              "settings": "{}"}
    records = {remote["id"]: remote}
    integrations = [{"id": "integration-max", "identifier": "max-browser", "disabled": False,
                     "accountId": "-123", "refreshNeeded": False}]
    monkeypatch.setattr(hub.postiz, "posts", lambda *args: copy.deepcopy(list(records.values())))
    monkeypatch.setattr(hub.postiz, "integrations", lambda: copy.deepcopy(integrations))
    def get_post(ident):
        if ident not in records:
            raise AgentError("POSTIZ_POST_NOT_FOUND", 409)
        return copy.deepcopy(records[ident])
    monkeypatch.setattr(hub.postiz, "get_post", get_post)
    monkeypatch.setattr(hub.publisher, "inspect", lambda _: {"publish": True, "live_verified": True})
    sent = []
    def publish(*args, **kwargs):
        sent.append((args, kwargs))
        return Delivery("confirmed", "fixture-mid", "https://example.org/fixture-only")
    monkeypatch.setattr(hub.publisher, "publish", publish)
    return hub, records, integrations, sent, due


def import_variant(value):
    core, _, _, _, _ = value
    core.bridge.sync(force=True)
    with core.db.session() as s:
        return s.scalar(select(BridgePost)).variant_id


def approve(value, ident):
    core, _, _, _, due = value
    with core.db.session() as s:
        v, t = s.get(Variant, ident), s.get(Target, "max")
        revision, hashed = v.revision, fingerprint(core.snapshot(v, t))
    core.bridge.attest_original(ident, revision, hashed, "owner")
    with core.db.session() as s:
        v, t = s.get(Variant, ident), s.get(Target, "max")
        return core.approve_variant(ident, v.revision, fingerprint(core.snapshot(v, t)), due, "owner")


def test_import_is_idempotent_and_never_approves(bridge):
    core, _, _, sent, _ = bridge
    ident = import_variant(bridge)
    assert import_variant(bridge) == ident
    assert core.publish_due() == 0
    with core.db.session() as s:
        assert s.query(BridgePost).count() == 1
        assert s.query(Variant).count() == 1
        assert s.query(TargetApproval).count() == 0
    assert sent == []


def test_original_content_needs_separate_owner_attestation(bridge):
    core, _, _, _, due = bridge
    ident = import_variant(bridge)
    with core.db.session() as s:
        v, t = s.get(Variant, ident), s.get(Target, "max")
        with pytest.raises(AgentError, match="BRIDGE_ORIGINAL_ATTESTATION_REQUIRED"):
            core.approve_variant(ident, v.revision, fingerprint(core.snapshot(v, t)), due)


def test_approved_once_and_fake_send_is_not_reported_as_live(bridge):
    core, _, _, sent, _ = bridge
    ident = import_variant(bridge)
    approve(bridge, ident)
    assert core.publish_due() == 1
    assert core.publish_due() == 0
    assert len(sent) == 1
    token = core.bridge._seal({"kind": "channel", "target": "max", "destination": "-123", "expires": now() + 60})
    assert core.bridge.status(token, "integration-max", "post-max")["state"] == "unverified"


@pytest.mark.parametrize("field", ["content", "publishDate", "state", "settings"])
def test_remote_edit_invalidates_approval(bridge, field):
    core, records, _, sent, _ = bridge
    ident = import_variant(bridge)
    dispatch = approve(bridge, ident)
    records["post-max"][field] = {"content": "Отредактированный текст для проверки.",
        "publishDate": datetime.fromtimestamp(now() + 500, UTC).isoformat(),
        "state": "DRAFT", "settings": json.dumps({"__type": "max-browser", "changed": True})}[field]
    core.bridge.sync(force=True)
    assert core.publish_due() == 0
    with core.db.session() as s:
        assert s.get(Dispatch, dispatch).state == "cancelled"
        assert s.query(TargetApproval).one().revoked
    assert sent == []


def test_last_check_catches_edit_even_without_polling(bridge):
    core, records, _, sent, _ = bridge
    ident = import_variant(bridge)
    dispatch = approve(bridge, ident)
    records["post-max"]["content"] = "Правка между согласованием и отправкой."
    assert core.publish_due() == 0
    with core.db.session() as s:
        assert s.get(Dispatch, dispatch).error == "BRIDGE_REMOTE_CHANGED"
    assert sent == []


def test_mutation_lease_blocks_send_and_never_expires_into_permission(bridge):
    core, _, _, sent, _ = bridge
    ident = import_variant(bridge)
    dispatch = approve(bridge, ident)
    lease = core.bridge.mutation(["group-max"])
    with core.db.session.begin() as s:
        s.get(BridgePost, "post-max").mutation_until = now() - 1
    assert core.publish_due() == 0
    with core.db.session() as s:
        assert s.get(Dispatch, dispatch).state == "pending"
    core.bridge.mutation(["post-max"], lease["owner"])
    assert core.publish_due() == 1
    assert len(sent) == 1


def test_mutation_and_delete_are_blocked_during_unknown_outcome(bridge):
    core, _, _, sent, _ = bridge
    ident = import_variant(bridge)
    dispatch = approve(bridge, ident)
    with core.db.session.begin() as s:
        s.get(Dispatch, dispatch).state = "unknown"
    with pytest.raises(AgentError, match="BRIDGE_DELIVERY_LOCKED"):
        core.bridge.mutation(["post-max"])
    assert sent == []


def test_delete_revokes_pending_send(bridge):
    core, records, _, sent, _ = bridge
    ident = import_variant(bridge)
    dispatch = approve(bridge, ident)
    records.clear()
    core.bridge.sync(force=True)
    assert core.publish_due() == 0
    with core.db.session() as s:
        assert s.get(Dispatch, dispatch).state == "cancelled"
    assert not sent


def test_schedule_and_thread_constraints(bridge):
    core, records, _, _, _ = bridge
    ident = import_variant(bridge)
    records["post-max"]["childrenPost"] = [{"id": "reply"}]
    with pytest.raises(AgentError, match="BRIDGE_THREADS_OR_REPEAT_UNSUPPORTED"):
        core.bridge.normalize(records["post-max"])
    records["post-max"]["childrenPost"] = []
    with core.db.session() as s:
        v = s.get(Variant, ident)
        with pytest.raises(AgentError, match="BRIDGE_SCHEDULE_MISMATCH"):
            core.bridge.check_approval(s, v, now() + 999)


def test_media_cannot_fetch_external_or_traverse_uploads(bridge):
    core, _, _, _, _ = bridge
    core.postiz.base = "http://localhost:4007/api/public/v1"
    t = core.bridge.target("max")
    for value, code in [("https://other.example/image.png", "BRIDGE_MEDIA_ORIGIN_DENIED"),
                        ("/uploads/%2e%2e/private.png", "BRIDGE_MEDIA_PATH_DENIED")]:
        with pytest.raises(AgentError, match=code):
            core.bridge._media([{"path": value}], t)


def test_public_origin_uploads_are_fetched_only_over_internal_postiz(bridge, monkeypatch):
    from PIL import Image
    core, _, _, _, _ = bridge
    with core.db.session() as session:
        target = session.get(Target, 'max')
    image = io.BytesIO()
    Image.new('RGB', (24, 24), 'blue').save(image, format='PNG')
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        return httpx.Response(200, headers={'content-type':'image/png'}, content=image.getvalue())

    core.postiz.base = 'http://127.0.0.1:14007/api/public/v1'
    core.postiz.public_origin = 'http://10.20.30.45:4007'
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(core.postiz, 'client', client)
    assert len(core.bridge._media([{'path':'http://10.20.30.45:4007/uploads/test.png'}], target)) == 1
    assert fetched == ['http://127.0.0.1:14007/uploads/test.png']
    for path in ['//other.example/uploads/test.png', 'http://10.20.30.46:4007/uploads/test.png']:
        with pytest.raises(AgentError, match='BRIDGE_MEDIA_ORIGIN_DENIED'):
            core.bridge._media([{'path':path}], target)
    assert len(fetched) == 1
    client.close()


def test_delegate_cannot_approve_and_wrong_ticket_is_rejected(bridge):
    core, _, _, _, _ = bridge
    client = TestClient(hub_app(core))
    assert client.post("/api/max-bridge/connect", json={"target_id": "max", "state": "s" * 32,
        "verifier": "v" * 32}).status_code == 403
    headers = {"X-Max-Bridge-Key": core.bridge.key}
    code = client.post("/api/max-bridge/connect", json={"target_id": "max", "state": "s" * 32,
        "verifier": "v" * 32}, headers=headers).json()["code"]
    assert client.post("/api/max-bridge/exchange", json={"code": code, "verifier": "x" * 32}, headers=headers).status_code == 403
    assert client.post("/api/max-bridge/exchange", json={"code": code, "verifier": "v" * 32}, headers=headers).status_code == 200
    ident = import_variant(bridge)
    assert client.post(f"/api/hub/variants/{ident}/attest-original", json={"revision": 1,
        "content_hash": "a" * 64}, headers=headers).status_code == 401
    assert client.post(f"/api/hub/variants/{ident}/attest-original", json={"revision": 1,
        "content_hash": "a" * 64}, headers={"Authorization": "Bearer hub-test-viewer"}).status_code == 403


def test_unavailable_postiz_fails_closed_before_outbound(bridge, monkeypatch):
    core, _, _, sent, _ = bridge
    ident = import_variant(bridge)
    approve(bridge, ident)
    def unavailable(_):
        raise AgentError("POSTIZ_UNAVAILABLE", 503)
    monkeypatch.setattr(core.postiz, "get_post", unavailable)
    assert core.publish_due() == 0
    assert not sent


def test_mutable_remote_media_is_rechecked_before_publish(bridge):
    from PIL import Image
    core, records, _, sent, _ = bridge
    core.postiz.base = "http://localhost:4007/api/public/v1"
    def png(color):
        data = io.BytesIO()
        Image.new("RGB", (4, 4), color).save(data, format="PNG")
        return data.getvalue()
    content = [png("green")]
    original = core.postiz.client
    core.postiz.client = httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=content[0])))
    try:
        records["post-max"]["image"] = json.dumps([{"id": "media-fixture", "path": "/uploads/test.png"}])
        ident = import_variant(bridge)
        dispatch = approve(bridge, ident)
        content[0] = png("red")
        assert core.publish_due() == 0
        with core.db.session() as s:
            assert s.get(Dispatch, dispatch).error == "BRIDGE_REMOTE_MEDIA_CHANGED"
        assert not sent
    finally:
        core.postiz.client.close()
        core.postiz.client = original


@pytest.mark.parametrize("change", [{"accountId": "another-channel"}, {"disabled": True}, {"refreshNeeded": True}])
def test_connection_changes_block_outbound(bridge, change):
    core, _, integrations, sent, _ = bridge
    ident = import_variant(bridge)
    approve(bridge, ident)
    integrations[0].update(change)
    assert core.publish_due() == 0
    assert not sent
