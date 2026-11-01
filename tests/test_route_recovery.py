"""A crashed sender loses its fence; recovery only reads the frozen delivery."""

import pytest
from sqlalchemy import select

from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, now
from max_postiz_agent.hub_models import Dispatch, TargetApproval, Variant
from max_postiz_agent.platform_management import reconcile
from max_postiz_agent.server_state import ServerState
from test_hub import approve, variants
from test_route_dispatch_postgres import postgres_routes as postgres_routes

pytest_plugins = ["test_four_platform_hub"]


def crashed(core, *, state="sending", error=None, future=False):
    ident = approve(core, variants(core)["telegram"])
    with core.db.session.begin() as session:
        row = session.get(Dispatch, ident)
        row.state, row.error, row.owner = state, error, core.worker_id
        row.lease_until = now() + 600 if future else now() - 10
    with core.db.session() as session:
        return session.get(Dispatch, ident)


def expiry_audits(core, ident):
    with core.db.session() as session:
        return list(session.scalars(select(Audit).where(
            Audit.entity == ident, Audit.action == "dispatch_send_lease_expired")))


def test_worker_expires_owned_lease_once_and_fences_original_callback(four_hub):
    original = crashed(four_hub)
    assert four_hub.publish_due(platform_routes_only=True) == 0
    with four_hub.db.session() as session:
        row = session.get(Dispatch, original.id)
        assert (row.state, row.owner, row.lease_until, row.error) == (
            "unknown", None, 0, "SEND_LEASE_EXPIRED")
        assert row.snapshot_hash == original.snapshot_hash and row.attempts == original.attempts
        assert session.get(Variant, row.variant_id).state == "unknown"
    with pytest.raises(AgentError, match="ROUTE_DISPATCH_LEASE_LOST"):
        four_hub.routes._authorize(original)
    assert four_hub.publish_due(platform_routes_only=True) == 0
    events = expiry_audits(four_hub, original.id)
    assert len(events) == 1
    assert events[0].details["previous_owner"] == original.owner
    assert events[0].details["previous_lease_until"] == original.lease_until
    assert events[0].details["snapshot_hash"] == original.snapshot_hash
    assert events[0].details["external_mutations"] == 0
    assert four_hub.fixture_publishers["telegram"].publications == []


@pytest.mark.parametrize("state", ["sending", "unknown"])
def test_read_only_reconcile_can_recover_expiry_while_globally_and_target_paused(four_hub, state):
    original = crashed(four_hub, state=state, error="SEND_LEASE_EXPIRED" if state == "unknown" else None)
    ServerState(four_hub).initialize()
    four_hub.pause_target("telegram", True)
    assert four_hub.publish_due() == 0
    result = reconcile(four_hub, original.id)
    assert result["state"] == "published" and result["verified"] is False
    assert result["external_mutations"] == 0
    with four_hub.db.session() as session:
        row = session.get(Dispatch, original.id)
        assert row.owner is None and row.lease_until == 0 and row.snapshot_hash == original.snapshot_hash
    assert len(expiry_audits(four_hub, original.id)) == 1
    adapter = four_hub.fixture_publishers["telegram"]
    assert len(adapter.reconciliations) == 1 and adapter.publications == []


@pytest.mark.parametrize("state,error,future", [
    ("sending", None, True), ("unknown", "SEND_LEASE_EXPIRED", True),
    ("unknown", "TELEGRAM_UNKNOWN", False), ("unknown", None, False),
])
def test_recovery_never_clears_live_or_unrelated_unknown_owner(four_hub, state, error, future):
    original = crashed(four_hub, state=state, error=error, future=future)
    with pytest.raises(AgentError):
        reconcile(four_hub, original.id)
    with four_hub.db.session() as session:
        row = session.get(Dispatch, original.id)
        assert (row.state, row.owner, row.lease_until, row.error) == (
            state, original.owner, original.lease_until, error)
    assert expiry_audits(four_hub, original.id) == []
    adapter = four_hub.fixture_publishers["telegram"]
    assert adapter.publications == adapter.reconciliations == []


def test_partial_receipt_keeps_known_id_unknown_and_never_resends(four_hub, monkeypatch):
    original = crashed(four_hub)
    ServerState(four_hub).initialize()
    adapter, calls = four_hub.fixture_publishers["telegram"], []

    def partial(*args, **kwargs):
        calls.append((args, kwargs))
        return Delivery("unknown", "photo-receipt", verified=False,
                        evidence={"fixture": True, "message_ids": ["photo-receipt"]})

    monkeypatch.setattr(adapter, "reconcile", partial)
    for _ in range(2):
        result = reconcile(four_hub, original.id)
        assert result["state"] == "unknown" and result["external_id"] == "photo-receipt"
    assert len(calls) == 2 and adapter.publications == []
    assert len(expiry_audits(four_hub, original.id)) == 1


def test_fence_is_durable_even_if_approval_is_stale_and_no_readback_is_started(four_hub):
    original = crashed(four_hub)
    with four_hub.db.session.begin() as session:
        session.get(TargetApproval, original.approval_id).revoked = True
    with pytest.raises(AgentError, match="APPROVAL_STALE"):
        reconcile(four_hub, original.id)
    with four_hub.db.session() as session:
        row = session.get(Dispatch, original.id)
        assert row.state == "unknown" and row.owner is None and row.lease_until == 0
    assert len(expiry_audits(four_hub, original.id)) == 1
    assert four_hub.fixture_publishers["telegram"].reconciliations == []


@pytest.mark.parametrize("outcome", ["expired", "expiry_cleanup_skipped", "reconciled", "other_live_owner"])
def test_late_sender_cannot_promote_or_overwrite_a_fenced_result(four_hub, monkeypatch, outcome):
    ident = approve(four_hub, variants(four_hub)["telegram"])
    adapter, calls = four_hub.fixture_publishers["telegram"], []
    if outcome == "expiry_cleanup_skipped":
        monkeypatch.setattr(four_hub, "_expire_send_leases", lambda **kwargs: 0)

    def delayed(destination, snapshot, request_id, *, authorize):
        authorize()
        calls.append(request_id)
        with four_hub.db.session.begin() as session:
            row = session.get(Dispatch, request_id)
            row.lease_until = now() - 10
        four_hub._expire_send_leases(dispatch_id=request_id)
        with pytest.raises(AgentError, match="ROUTE_DISPATCH_LEASE_LOST"):
            authorize()
        if outcome == "reconciled":
            assert reconcile(four_hub, request_id)["state"] == "published"
        elif outcome == "other_live_owner":
            with four_hub.db.session.begin() as session:
                row = session.get(Dispatch, request_id)
                row.state, row.owner, row.lease_until = "sending", "other-worker", now() + 600
        return Delivery("confirmed", "late-receipt", "https://example.test/late", verified=True,
                        evidence={"fixture": True, "independent_readback": True})

    monkeypatch.setattr(adapter, "publish", delayed)
    assert four_hub.publish_due(platform_routes_only=True) == 0
    assert calls == [ident]
    with four_hub.db.session() as session:
        row = session.get(Dispatch, ident)
        if outcome in {"expired", "expiry_cleanup_skipped"}:
            assert (row.state, row.owner, row.verified, row.external_id) == (
                "unknown", None, False, "late-receipt")
            events = list(session.scalars(select(Audit).where(
                Audit.entity == ident, Audit.action == "late_delivery_evidence")))
            assert len(events) == 1 and events[0].details["promoted"] is False
        elif outcome == "reconciled":
            assert row.state == "published" and row.external_id == "telegram-message" and row.verified is False
        else:
            assert row.state == "sending" and row.owner == "other-worker" and row.external_id is None
    assert four_hub.publish_due(platform_routes_only=True) == 0 and calls == [ident]


def test_postgres_recovery_skips_locked_sender_then_reconciles_during_pause(postgres_routes):
    core = postgres_routes
    original = crashed(core)
    ServerState(core).initialize()
    with core.db.session.begin() as locked:
        row = locked.scalar(select(Dispatch).where(Dispatch.id == original.id).with_for_update())
        assert core._expire_send_leases(dispatch_id=original.id) == 0
        assert row.state == "sending" and row.owner == original.owner
    result = reconcile(core, original.id)
    assert result["state"] == "published" and result["verified"] is False
    assert len(expiry_audits(core, original.id)) == 1
    assert core.fixture_publishers["telegram"].publications == []
