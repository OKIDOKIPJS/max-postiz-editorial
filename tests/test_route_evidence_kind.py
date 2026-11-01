"""Исторический fixture/API proof не становится новой живой сверкой в панели."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_hub import approve, variants
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Dispatch, Target
from max_postiz_agent.platform_management import _evidence_kind, route_state
from max_postiz_agent.platform_routes import PlatformRoutes


pytest_plugins = ["test_four_platform_hub"]


def dispatch(verified=False):
    content = {"title": "Точный текст", "body": "Исходная версия"}
    return SimpleNamespace(snapshot=content, due_at=100.0, verified=verified,
        snapshot_hash=fingerprint({"content": content, "due_at": 100.0}))


def audit(row, value, *, verified=None, snapshot_hash=None):
    return SimpleNamespace(details={"snapshot_hash": snapshot_hash or row.snapshot_hash,
        "verified": row.verified if verified is None else verified, "evidence": value})


@pytest.mark.parametrize("value,verified,expected", [
    ({"fixture": True, "verification": "api_receipt"}, False, "fixture"),
    ({"fixture": True, "independent_readback": True}, True, "fixture"),
    ({"fixture": False, "verification": "api_receipt", "independent_readback": False},
     False, "api_receipt"),
    ({"fixture": False, "verification": "browser_readback", "independent_readback": True},
     True, "independent_readback"),
    ({"fixture": False, "provider": "vk", "code": "VK_EXACT_READBACK", "readback_exact": True},
     True, "independent_readback"),
    ({"fixture": False, "verification": "access_only", "independent_readback": True},
     True, "unverified"),
    ({"fixture": False, "verification": "browser_readback", "independent_readback": True},
     False, "unverified"),
    ({"verification": "api_receipt", "independent_readback": False}, False, "unverified"),
    ({"fixture": "false", "verification": "api_receipt", "independent_readback": False},
     False, "unverified"),
    ({"fixture": False, "verification": "api_receipt", "independent_readback": True},
     False, "unverified"),
    (None, False, "unverified"),
])
def test_only_explicit_bound_evidence_gets_a_proof_label(value, verified, expected):
    row = dispatch(verified)
    assert _evidence_kind(row, [audit(row, value)]) == expected


def test_corrupted_snapshot_or_another_approval_hash_cannot_reuse_proof():
    row = dispatch(True)
    proof = {"fixture": False, "verification": "browser_readback", "independent_readback": True}
    assert _evidence_kind(row, [audit(row, proof, snapshot_hash="a" * 64)]) == "unverified"
    bound = audit(row, proof)
    row.snapshot["body"] = "Изменённый текст"
    assert _evidence_kind(row, [bound]) == "unverified"


def test_missing_or_latest_incomplete_proof_does_not_inherit_older_success():
    row = dispatch(True)
    old = audit(row, {"fixture": False, "verification": "browser_readback",
                      "independent_readback": True})
    assert _evidence_kind(row, []) == "unverified"
    assert _evidence_kind(row, [audit(row, {}), old]) == "unverified"


def test_api_inventory_preserves_fixture_provenance_after_configuration_change(four_hub):
    ident = approve(four_hub, variants(four_hub)["telegram"])
    assert four_hub.publish_due() == 1
    with four_hub.db.session.begin() as session:
        target = session.get(Target, "telegram")
        target.options = {**target.options, "adapter_mode": "live"}
        target.destination = "-1007777777777"
        record = session.scalar(select(Audit).where(Audit.entity == ident,
                                                    Audit.action == "delivery_evidence"))
        record.details = {**record.details, "private_runtime_note": "never-show-this-proof-marker"}
    inventory = route_state(four_hub)
    result = next(row for row in inventory["dispatches"] if row["id"] == ident)
    assert result["state"] == "published" and result["evidence_kind"] == "fixture"
    assert result["destination"] == "-1001234567890"
    assert result["platform"] == "telegram" and result["route"] == "telegram_api"
    assert "never-show-this-proof-marker" not in json.dumps(inventory)
    with four_hub.db.session() as session:
        assert session.get(Dispatch, ident).verified is False


@pytest.mark.parametrize("route", ["pikabu_browser", "vk_browser"])
def test_browser_reconcile_receives_the_database_frozen_snapshot(route):
    calls, returned = [], object()
    adapter = SimpleNamespace(reconcile=lambda *args, **kwargs:
        (calls.append((args, kwargs)), returned)[1])
    registry = PlatformRoutes(None, {"target": adapter})
    target = SimpleNamespace(id="target", route=route, destination="exact-owner")
    frozen = SimpleNamespace(id="dispatch", snapshot={"body": "Согласованная версия"})
    assert registry.reconcile(frozen, target) is returned
    assert calls == [(("exact-owner", "dispatch"), {"snapshot": frozen.snapshot})]
    assert calls[0][1]["snapshot"] is frozen.snapshot
