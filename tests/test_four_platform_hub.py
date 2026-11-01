"""Четыре маршрута проходят exact approvals; все транспорты здесь изолированные fixtures."""

import hashlib
import io
import json
from copy import deepcopy

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from conftest import write_topic
from test_hub import approve, variants
from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import AgentError, Settings, fingerprint
from max_postiz_agent.db import Audit, now
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.platform_management import evidence, inspect_target, preflight, reconcile
from max_postiz_agent.telegram_publisher import TelegramPublisher


DIRECT = {"telegram": "telegram_api", "vk": "vk_api", "pikabu": "pikabu_browser"}


class IsolatedPublisher:
    mode = "fixture"

    def __init__(self, platform):
        self.platform = platform
        self.validations, self.inspections, self.publications, self.reconciliations = [], [], [], []
        self.validation_error = self.publication_error = None
        self.result_state = "confirmed"

    def validate(self, destination, snapshot):
        self.validations.append((destination, deepcopy(snapshot)))
        if self.validation_error:
            raise AgentError(self.validation_error)
        assert snapshot["platform"] == self.platform
        assert snapshot["route"] == DIRECT[self.platform]
        assert snapshot["destination"] == destination
        return {"preflight_only": True, "fixture": True, "message_count": 1}

    def inspect(self, destination):
        self.inspections.append(destination)
        return {"publish": True, "verification": "access_only", "fixture": True, "live_verified": False}

    def publish(self, destination, snapshot, request_id, *, authorize=None):
        if authorize is not None:
            authorize()
        self.publications.append((destination, deepcopy(snapshot), request_id))
        if self.publication_error:
            raise AgentError(self.publication_error, 503)
        return Delivery(self.result_state, self.platform + "-message",
                        "https://example.test/" + self.platform + "/message", verified=False,
                        evidence={"verification": "api_receipt", "fixture": True,
                                  "platform": self.platform, "independent_readback": False,
                                  "message_ids": [self.platform + "-message"]})

    def reconcile(self, destination, *args, **kwargs):
        self.reconciliations.append((destination, args, kwargs))
        return Delivery("confirmed", self.platform + "-message", verified=False,
                        evidence={"verification": "api_receipt", "fixture": True,
                                  "platform": self.platform, "independent_readback": False})

    def close(self):
        pass


@pytest.fixture
def four_hub(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "isolated-four-platform-fixture")
    topic = service.settings.topic
    topic["channels"][0]["publication"]["mode"] = "review"
    write_topic(service.settings.root, topic)
    service.activate(Settings.load(service.settings.root))
    publishers = {platform: IsolatedPublisher(platform) for platform in DIRECT}
    targets = [{"id": "max", "profile_id": "main", "platform": "max", "route": "max_api",
                "enabled": True, "destination": "-123", "options": {"max_chars": 3500}}]
    destinations = {"telegram": "-1001234567890", "vk": "-12345", "pikabu": "fixture_owner"}
    targets.extend({"id": platform, "profile_id": "main", "platform": platform, "route": route,
                    "enabled": True, "destination": destinations[platform],
                    "options": {"max_chars": 3500, "adapter_mode": "fake"}}
                   for platform, route in DIRECT.items())
    config = {"schema_version": "1.0", "postiz": {}, "tasks": [], "targets": targets}
    (service.settings.root / "config/publishing.json").write_text(json.dumps(config), encoding="utf-8")
    value = HubService(service.settings, service.db, editor=service.editor,
                       publisher=service.publisher, http=service.http, route_publishers=publishers)
    value.activate()
    for target in targets:
        value.pause_target(target["id"], False)
    value.fixture_publishers = publishers
    yield value
    value.routes.close()
    value.postiz.close()


def test_four_targets_bootstrap_without_network_or_automatic_sends(four_hub):
    with four_hub.db.session() as session:
        assert set(session.scalars(select(Target.id))) == {"max", *DIRECT}
        assert list(session.scalars(select(Dispatch))) == []
    for adapter in four_hub.fixture_publishers.values():
        assert adapter.validations == adapter.inspections == adapter.publications == []


@pytest.mark.parametrize("platform", DIRECT)
def test_corrupted_frozen_dispatch_body_never_reaches_transport(four_hub, platform):
    dispatch_id = approve(four_hub, variants(four_hub)[platform])
    with four_hub.db.session.begin() as session:
        row = session.get(Dispatch, dispatch_id)
        row.snapshot = {**row.snapshot, "body": "Неподтверждённый текст в повреждённом snapshot."}
    assert four_hub.publish_due() == 0
    assert four_hub.fixture_publishers[platform].publications == []
    with four_hub.db.session() as session:
        row = session.get(Dispatch, dispatch_id)
        assert row.state == "cancelled" and row.error == "APPROVAL_STALE"


@pytest.mark.parametrize("platform", DIRECT)
def test_preflight_is_pure_and_preserves_exact_variant(four_hub, platform):
    ident = variants(four_hub)[platform]
    with four_hub.db.session() as session:
        variant = session.get(Variant, ident)
        frozen = four_hub.snapshot(variant, session.get(Target, platform))
    result = preflight(four_hub, ident)
    assert result["preflight_only"] is True and result["live_verified"] is False
    assert result["content_hash"] == fingerprint(frozen)
    assert result["route"] == DIRECT[platform]
    adapter = four_hub.fixture_publishers[platform]
    assert adapter.validations and all(snapshot == frozen for _, snapshot in adapter.validations)
    assert adapter.publications == adapter.inspections == []
    with four_hub.db.session() as session:
        assert list(session.scalars(select(Dispatch))) == []
        assert list(session.scalars(select(TargetApproval))) == []


def test_four_platform_dispatch_uses_each_exact_snapshot_once(four_hub, monkeypatch):
    ids = variants(four_hub)
    dispatches = {platform: approve(four_hub, ident) for platform, ident in ids.items()}
    max_calls = []

    def max_publish(*args, **kwargs):
        max_calls.append((args, kwargs))
        return Delivery("confirmed", "max-fixture-message")

    monkeypatch.setattr(four_hub.publisher, "publish", max_publish)
    assert four_hub.publish_due() == 4
    assert four_hub.publish_due() == 0
    assert len(max_calls) == 1
    with four_hub.db.session() as session:
        for platform, dispatch_id in dispatches.items():
            dispatch = session.get(Dispatch, dispatch_id)
            assert dispatch.state == "published" and dispatch.attempts == 1 and dispatch.verified is False
            if platform in DIRECT:
                adapter = four_hub.fixture_publishers[platform]
                assert adapter.publications == [(dispatch.snapshot["destination"], dispatch.snapshot, dispatch_id)]
                events = list(session.scalars(select(Audit).where(
                    Audit.entity == dispatch_id, Audit.action == "delivery_evidence")))
                assert len(events) == 1
                assert events[0].details["evidence"]["platform"] == platform
                assert events[0].details["evidence"]["fixture"] is True
                assert events[0].details["verified"] is False
                assert events[0].details["snapshot_hash"] == dispatch.snapshot_hash


@pytest.mark.parametrize("platform", DIRECT)
def test_api_fixture_verification_never_inherits_live_max_mode(four_hub, platform, monkeypatch):
    ident = variants(four_hub)[platform]
    dispatch_id = approve(four_hub, ident)
    monkeypatch.setattr(four_hub.publisher, "mode", "live")
    assert four_hub.publish_due() == 1
    with four_hub.db.session() as session:
        assert session.get(Dispatch, dispatch_id).verified is False
    rows = evidence(four_hub, dispatch_id)
    assert len(rows) == 1 and rows[0]["details"]["verified"] is False


@pytest.mark.parametrize("platform", DIRECT)
def test_future_dispatch_waits_for_approved_date(four_hub, platform, monkeypatch):
    import max_postiz_agent.hub as hub_module

    due_at = now() + 7200
    dispatch_id = approve(four_hub, variants(four_hub)[platform], due_at)
    assert four_hub.publish_due() == 0
    assert four_hub.fixture_publishers[platform].publications == []
    monkeypatch.setattr(hub_module, "now", lambda: due_at + 1)
    # Отдельный transport-authorizer использует тот же фактический момент.
    monkeypatch.setattr("max_postiz_agent.db.now", lambda: due_at + 1)
    assert four_hub.publish_due() == 1
    with four_hub.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        assert dispatch.state == "published" and dispatch.due_at == due_at


@pytest.mark.parametrize("platform", DIRECT)
def test_paused_route_blocks_approval_and_send(four_hub, platform):
    ident = variants(four_hub)[platform]
    four_hub.pause_target(platform, True)
    with pytest.raises(AgentError, match="TARGET_PAUSED"):
        approve(four_hub, ident)
    four_hub.pause_target(platform, False)
    dispatch_id = approve(four_hub, ident)
    four_hub.pause_target(platform, True)
    assert four_hub.publish_due() == 0
    assert four_hub.fixture_publishers[platform].publications == []
    with four_hub.db.session() as session:
        assert session.get(Dispatch, dispatch_id).state == "pending"


def test_paused_route_does_not_block_other_platform(four_hub):
    ids = variants(four_hub)
    telegram = approve(four_hub, ids["telegram"])
    vk = approve(four_hub, ids["vk"])
    four_hub.pause_target("telegram", True)
    assert four_hub.publish_due() == 1
    with four_hub.db.session() as session:
        assert session.get(Dispatch, telegram).state == "pending"
        assert session.get(Dispatch, vk).state == "published"


@pytest.mark.parametrize("platform", DIRECT)
def test_edited_variant_revokes_approval_without_publication(four_hub, platform):
    ident = variants(four_hub)[platform]
    dispatch_id = approve(four_hub, ident)
    with four_hub.db.session() as session:
        variant = session.get(Variant, ident)
        revision, body = variant.revision, variant.body
    four_hub.revise_variant(ident, revision, body + "\nРедакторская правка.")
    assert four_hub.publish_due() == 0
    with four_hub.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        assert dispatch.state == "cancelled"
        assert session.get(TargetApproval, dispatch.approval_id).revoked is True
    assert four_hub.fixture_publishers[platform].publications == []


@pytest.mark.parametrize("platform", DIRECT)
def test_changed_destination_invalidates_exact_approval(four_hub, platform):
    dispatch_id = approve(four_hub, variants(four_hub)[platform])
    with four_hub.db.session.begin() as session:
        session.get(Target, platform).destination = "changed-after-approval"
    assert four_hub.publish_due() == 0
    with four_hub.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        assert dispatch.state == "cancelled" and dispatch.error == "APPROVAL_STALE"
    assert four_hub.fixture_publishers[platform].publications == []


@pytest.mark.parametrize("platform", DIRECT)
def test_route_preflight_failure_rolls_back_approval(four_hub, platform):
    ident = variants(four_hub)[platform]
    four_hub.fixture_publishers[platform].validation_error = "FIXTURE_CONTENT_REJECTED"
    with pytest.raises(AgentError, match="FIXTURE_CONTENT_REJECTED"):
        approve(four_hub, ident)
    with four_hub.db.session() as session:
        assert list(session.scalars(select(TargetApproval))) == []
        assert list(session.scalars(select(Dispatch))) == []
    assert four_hub.fixture_publishers[platform].publications == []


@pytest.mark.parametrize("platform", DIRECT)
def test_unknown_delivery_keeps_ids_evidence_and_never_resends(four_hub, platform):
    adapter = four_hub.fixture_publishers[platform]
    adapter.result_state = "unknown"
    dispatch_id = approve(four_hub, variants(four_hub)[platform])
    assert four_hub.publish_due() == 0
    assert four_hub.publish_due() == 0
    assert len(adapter.publications) == 1
    with four_hub.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        assert dispatch.state == "unknown" and dispatch.external_id == platform + "-message"
        assert dispatch.attempts == 1 and dispatch.verified is False
    rows = evidence(four_hub, dispatch_id)
    assert rows[0]["details"]["evidence"]["message_ids"] == [platform + "-message"]


@pytest.mark.parametrize("platform,error", [
    ("telegram", "TELEGRAM_UNKNOWN"), ("vk", "VK_UNKNOWN"),
    ("pikabu", "PIKABU_BROWSER_UNKNOWN"),
])
def test_uncertain_transport_error_never_retries(four_hub, platform, error):
    adapter = four_hub.fixture_publishers[platform]
    adapter.publication_error = error
    dispatch_id = approve(four_hub, variants(four_hub)[platform])
    assert four_hub.publish_due() == four_hub.publish_due() == 0
    assert len(adapter.publications) == 1
    with four_hub.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        assert dispatch.state == "unknown" and dispatch.error == error


@pytest.mark.parametrize("platform", DIRECT)
def test_explicit_reconcile_reads_original_result_without_resend(four_hub, platform):
    adapter = four_hub.fixture_publishers[platform]
    adapter.result_state = "unknown"
    dispatch_id = approve(four_hub, variants(four_hub)[platform])
    four_hub.publish_due()
    result = reconcile(four_hub, dispatch_id)
    assert result["state"] == "published" and result["verified"] is False
    assert result["external_mutations"] == 0
    assert len(adapter.publications) == len(adapter.reconciliations) == 1
    assert four_hub.publish_due() == 0
    assert len(evidence(four_hub, dispatch_id)) == 2


@pytest.mark.parametrize("platform", DIRECT)
def test_stale_unknown_binding_blocks_reconciliation_before_adapter(four_hub, platform):
    adapter = four_hub.fixture_publishers[platform]
    adapter.result_state = "unknown"
    dispatch_id = approve(four_hub, variants(four_hub)[platform])
    four_hub.publish_due()
    with four_hub.db.session.begin() as session:
        session.get(Target, platform).destination = "changed-after-send"
    with pytest.raises(AgentError, match="APPROVAL_STALE"):
        reconcile(four_hub, dispatch_id)
    assert adapter.reconciliations == [] and len(adapter.publications) == 1


@pytest.mark.parametrize("platform", DIRECT)
def test_access_inspection_is_separate_from_delivery(four_hub, platform):
    result = inspect_target(four_hub, platform)
    adapter = four_hub.fixture_publishers[platform]
    assert result["delivery_verified"] is False
    assert result["access"]["verification"] == "access_only"
    assert len(adapter.inspections) == 1 and adapter.publications == []


@pytest.mark.parametrize("platform,wrong_platform", [
    ("telegram", "vk"), ("vk", "pikabu"), ("pikabu", "telegram"), ("max", "telegram"),
])
def test_wrong_route_platform_configuration_is_rejected(four_hub, platform, wrong_platform):
    target = next(value for value in four_hub.publishing["targets"] if value["id"] == platform)
    target["platform"] = wrong_platform
    with pytest.raises(AgentError, match="TARGET_ROUTE_INVALID"):
        four_hub.sync_targets()
    assert all(adapter.publications == [] for adapter in four_hub.fixture_publishers.values())


def test_real_telegram_adapter_full_illustrated_hub_dispatch_and_journal_reconcile(four_hub, monkeypatch):
    """Одобрение → реальный код адаптера → HTTP fixture → durable journal → сверка Hub."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:isolated_fixture_token_1234567890")
    ident = variants(four_hub)["telegram"]
    with four_hub.db.session() as session:
        variant = session.get(Variant, ident)
        title, revision = variant.title, variant.revision
        body = (variant.body + "\n\n") * 10
    assert 1024 < len(body.encode("utf-16-le")) // 2 < 3500
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "navy").save(output, format="PNG")
    image_raw = output.getvalue()
    image_path = four_hub.settings.root / "data/media/telegram-fixture.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(image_raw)
    media = [{"path": "data/media/telegram-fixture.png", "mime": "image/png",
              "sha256": hashlib.sha256(image_raw).hexdigest()}]
    four_hub.revise_variant(ident, revision, body, media=media)
    calls, frozen = [], {}

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.telegram.org" and request.method == "POST"
        chat = {"id": -1001234567890, "type": "channel", "username": "science_test"}
        if len(calls) == 1:
            assert request.url.path.endswith("/sendPhoto") and image_raw in request.content
            result = {"message_id": 101, "chat": chat,
                      "photo": [{"file_id": "fixture_photo", "width": 640, "height": 480}]}
        else:
            expected = frozen["body"] if frozen["body"].startswith(title) else title + "\n\n" + frozen["body"]
            entities = [{"type": "bold", "offset": 0, "length": len(title.encode("utf-16-le")) // 2}]
            assert request.url.path.endswith("/sendMessage")
            payload = json.loads(request.content)
            assert payload["text"] == expected and payload["entities"] == entities
            result = {"message_id": 102, "chat": chat, "text": expected, "entities": entities}
        return httpx.Response(200, json={"ok": True, "result": result})

    adapter = TelegramPublisher(four_hub.settings.root, {"media_layout": "auto"}, httpx.MockTransport(handler))
    four_hub.routes.injected["telegram"] = adapter
    try:
        dispatch_id = approve(four_hub, ident)
        assert calls == []  # Approval/preflight не выполняют HTTP.
        with four_hub.db.session() as session:
            frozen.update(session.get(Dispatch, dispatch_id).snapshot)
        assert four_hub.publish_due() == 1
        assert four_hub.publish_due() == 0 and len(calls) == 2
        with four_hub.db.session.begin() as session:
            dispatch = session.get(Dispatch, dispatch_id)
            assert dispatch.state == "published" and dispatch.verified is False
            assert dispatch.external_id == "102"
            dispatch.state = "unknown"  # Имитируем потерю финальной фиксации Hub, не новую отправку.
            session.get(Variant, ident).state = "unknown"
        journal_path = four_hub.settings.root / "data/telegram-deliveries" / dispatch_id / "journal.json"
        assert json.loads(journal_path.read_text(encoding="utf-8"))["state"] == "done"
        result = reconcile(four_hub, dispatch_id)
        assert result["state"] == "published" and result["verified"] is False
        assert result["external_mutations"] == 0 and len(calls) == 2
        records = evidence(four_hub, dispatch_id)
        transport_record = next(row for row in records if row["action"] == "delivery_evidence")
        assert transport_record["details"]["evidence"]["message_ids"] == [101, 102]
        assert len(records) == 2
    finally:
        adapter.close()
