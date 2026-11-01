"""Ограниченные списки не скрывают старый unknown; полный просмотр не согласует."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from test_hub import approve, variants
from max_postiz_agent.config import fingerprint
from max_postiz_agent.db import Audit, Draft
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant

pytest_plugins = ["test_routes_api"]


def test_full_details_preserve_failed_candidate_and_all_posting_metadata_without_actions(
        four_hub, route_client, monkeypatch):
    client, headers = route_client
    ident = variants(four_hub)["pikabu"]
    body = "Точный длинный русский текст. " * 1000 + "КОНЕЦ БЕЗ ОБРЕЗКИ"
    metadata = {"tags": ["Первый тег", "Второй тег"], "community": "точное сообщество",
                "ai_generated": True, "origin": "owner_original"}
    with four_hub.db.session.begin() as session:
        variant = session.get(Variant, ident)
        variant.body, variant.metadata_json, variant.state = body, metadata, "failed"
        expected = four_hub.snapshot(variant, session.get(Target, variant.target_id))
        counts = [session.scalar(select(func.count()).select_from(model))
                  for model in (Audit, TargetApproval, Dispatch)]
    def forbidden(*args, **kwargs):
        pytest.fail("Details must not quality-check, inspect, validate or publish")
    monkeypatch.setattr(four_hub, "_quality", forbidden)
    monkeypatch.setattr(four_hub.routes, "publisher", forbidden)
    response = client.post("/internal/editorial/route-action", headers=headers,
                           json={"action": "details", "id": ident})
    assert response.status_code == 200
    detail = response.json()
    assert detail["body"] == body and detail["metadata"] == metadata
    assert detail["media"] == expected["media"] and detail["sources"] == expected["sources"]
    assert detail["content_hash"] == fingerprint(expected)
    assert detail["revision"] == expected["revision"] and detail["read_only"] is True
    with four_hub.db.session() as session:
        assert counts == [session.scalar(select(func.count()).select_from(model))
                          for model in (Audit, TargetApproval, Dispatch)]


@pytest.mark.parametrize("field,value", [
    ("title", "Другой заголовок"), ("body", "Другой точный русский текст."),
    ("metadata_json", {"tags": ["Другой тег"], "ai_generated": False}),
    ("media", [{"path": "data/media/other.jpg", "mime": "image/jpeg", "sha256": "f" * 64}]),
])
def test_detail_hash_changes_with_every_posting_field(four_hub, route_client, field, value):
    client, headers = route_client
    ident = variants(four_hub)["pikabu"]
    def read():
        return client.post("/internal/editorial/route-action", headers=headers,
                           json={"action": "details", "id": ident}).json()
    before = read()
    with four_hub.db.session.begin() as session:
        setattr(session.get(Variant, ident), field, value)
    after = read()
    assert before["content_hash"] != after["content_hash"]
    assert after["metadata" if field == "metadata_json" else field] == value


def test_details_reject_oversize_without_truncation_and_owner_is_required(four_hub, route_client):
    client, headers = route_client
    ident = variants(four_hub)["telegram"]
    assert client.post("/internal/editorial/route-action",
                       json={"action": "details", "id": ident}).status_code == 403
    with four_hub.db.session.begin() as session:
        session.get(Variant, ident).metadata_json = {"large": "я" * 450000}
    response = client.post("/internal/editorial/route-action", headers=headers,
                           json={"action": "details", "id": ident})
    assert response.status_code == 422 and response.json()["code"] == "ROUTE_DETAILS_SIZE_INVALID"


def test_large_inventory_is_bounded_paginated_and_old_unknown_remains_reachable(four_hub, route_client):
    client, headers = route_client
    ident = variants(four_hub)["telegram"]
    approved = approve(four_hub, ident)
    with four_hub.db.session.begin() as session:
        template = session.get(Variant, ident)
        original = session.get(Dispatch, approved)
        original.state, original.due_at = "unknown", 0
        for index in range(120):
            draft = Draft(channel_id="main", story_id=str(uuid.uuid4()), current={}, config_id=template.config_hash)
            session.add(draft)
            session.flush()
            session.add(Variant(draft_id=draft.id, target_id=template.target_id, title=f"Материал {index:03d}",
                body="я" * 40000, metadata_json={"large_private_fixture": "я" * 40000}, media=[], sources=[],
                source_revision=1, config_hash=template.config_hash, binding_hash=template.binding_hash,
                updated_at=index + 1))
            session.add(Dispatch(variant_id=template.id, approval_id=original.approval_id,
                revision=1, snapshot_hash=fingerprint({"fixture_index": index}),
                snapshot={"title": f"История {index:03d}", "body": "я" * 40000}, state="published", due_at=index + 1))
    seen_variants, seen_dispatches = set(), set()
    for page in range(3):
        response = client.get(f"/internal/editorial/routes?variant_page={page}&dispatch_page={page}", headers=headers)
        assert response.status_code == 200 and len(response.content) < 900000
        result = response.json()
        assert len(result["variants"]) <= 50 and len(result["dispatches"]) <= 50
        assert all("body" not in item and "metadata" not in item for item in result["variants"])
        assert "large_private_fixture" not in response.text
        assert result["pagination"]["variant_count"] == 124
        assert result["pagination"]["dispatch_count"] == 121
        seen_variants.update(item["id"] for item in result["variants"])
        seen_dispatches.update(item["id"] for item in result["dispatches"])
    assert len(seen_variants) == 124 and len(seen_dispatches) == 121 and approved in seen_dispatches
    unresolved = client.get("/internal/editorial/routes?dispatch_filter=unresolved", headers=headers).json()
    assert [item["id"] for item in unresolved["dispatches"]] == [approved]
    assert unresolved["pagination"]["dispatch_active_count"] == 1
    assert len(json.dumps(unresolved, ensure_ascii=False).encode()) < 900000


@pytest.mark.parametrize("query", ["url=http://other", "limit=1000", "variant_page=-1", "dispatch_page=1000001",
    "variant_page=01", "variant_page=1.0", "variant_page=true", "dispatch_filter=unknown",
    "dispatch_page=0&dispatch_page=1"])
def test_list_rejects_unknown_keys_ambiguous_or_unbounded_pages(route_client, query):
    client, headers = route_client
    response = client.get("/internal/editorial/routes?" + query, headers=headers)
    assert response.status_code == 422 and response.json()["code"] == "ROUTE_LIST_INVALID"
