"""Postiz owner API управляет точными согласованиями без произвольного Send."""

import base64
import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from test_hub import variants
from max_postiz_agent.db import now
from max_postiz_agent.hub_models import Dispatch
from max_postiz_agent.hub_models import Variant
from max_postiz_agent.server_api import editorial_app

pytest_plugins = ["test_four_platform_hub"]


@pytest.fixture
def route_client(four_hub, monkeypatch):
    values = {"EDITORIAL_INTERNAL_KEY": "fixture-editorial-internal-key-000000000000000",
              "EDITORIAL_OWNER_USER_ID": "fixture_owner", "EDITORIAL_OWNER_ORGANIZATION_ID": "fixture_org"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    headers = {"X-Editorial-Key": values["EDITORIAL_INTERNAL_KEY"],
               "X-Editorial-Owner": "fixture_owner", "X-Editorial-Organization": "fixture_org"}
    return TestClient(editorial_app(four_hub)), headers


def test_route_api_requires_owner_before_access_or_mutation(four_hub, route_client):
    client, headers = route_client
    assert client.get("/internal/editorial/routes").status_code == 403
    wrong = {**headers, "X-Editorial-Owner": "someone_else"}
    assert client.post("/internal/editorial/route-action", headers=wrong,
                       json={"action": "inspect", "id": "telegram"}).status_code == 403
    assert four_hub.fixture_publishers["telegram"].inspections == []


def test_state_and_preflight_do_not_publish(four_hub, route_client):
    client, headers = route_client
    ident = variants(four_hub)["telegram"]
    state = client.get("/internal/editorial/routes", headers=headers)
    assert state.status_code == 200
    assert len(state.json()["targets"]) == 4
    result = client.post("/internal/editorial/route-action", headers=headers,
                         json={"action": "preflight", "id": ident})
    assert result.status_code == 200 and result.json()["preflight_only"] is True
    assert all(adapter.publications == [] for adapter in four_hub.fixture_publishers.values())


def test_approval_is_exact_future_schedule_and_creates_no_http(four_hub, route_client):
    client, headers = route_client
    ident = variants(four_hub)["vk"]
    proof = client.post("/internal/editorial/route-action", headers=headers,
                        json={"action": "preflight", "id": ident}).json()
    body = {"action": "approve", "id": ident, "revision": proof["revision"],
            "content_hash": proof["content_hash"], "due_at": now() + 3600}
    result = client.post("/internal/editorial/route-action", headers=headers, json=body)
    assert result.status_code == 200
    with four_hub.db.session() as session:
        assert len(list(session.scalars(select(Dispatch)))) == 1
    assert four_hub.fixture_publishers["vk"].publications == []


@pytest.mark.parametrize("body", [
    {"action": "publish", "id": "telegram"},
    {"action": "inspect", "id": "telegram", "due_at": 1},
    {"action": "inspect", "id": "../telegram"},
    {"action": "approve", "id": "telegram", "revision": True,
     "content_hash": "0" * 64, "due_at": 4000000000},
    {"action": "approve", "id": "telegram"},
])
def test_unknown_commands_and_extra_fields_fail_closed(four_hub, route_client, body):
    client, headers = route_client
    assert client.post("/internal/editorial/route-action", headers=headers, json=body).status_code == 422
    assert all(adapter.publications == [] for adapter in four_hub.fixture_publishers.values())


def test_pause_and_resume_are_explicit_owner_actions(four_hub, route_client):
    client, headers = route_client
    for action, expected in (("pause", True), ("resume", False)):
        result = client.post("/internal/editorial/route-action", headers=headers,
                             json={"action": action, "id": "telegram"})
        assert result.status_code == 200 and result.json()["paused"] is expected


def test_image_preview_is_bound_to_exact_local_original_and_does_not_send(four_hub, route_client):
    client, headers = route_client
    ident = variants(four_hub)["telegram"]
    root = four_hub.settings.root
    path = root / "data/media/preview.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    output = io.BytesIO()
    Image.new("RGB", (1000, 700), "navy").save(output, "PNG")
    raw = output.getvalue()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with four_hub.db.session.begin() as session:
        session.get(Variant, ident).media = [{"path": "data/media/preview.png",
            "mime": "image/png", "sha256": digest}]
    result = client.post("/internal/editorial/route-action", headers=headers,
                         json={"action": "preview", "id": ident})
    assert result.status_code == 200
    image = result.json()["images"][0]
    assert image["sha256"] == digest and image["mime"] == "image/jpeg"
    assert image["source_mime"] == "image/png" and image["preview"] is True
    thumbnail = base64.b64decode(image["data_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(thumbnail)) as decoded:
        assert decoded.format == "JPEG" and max(decoded.size) <= 640
    assert path.read_bytes() == raw
    assert all(adapter.publications == [] for adapter in four_hub.fixture_publishers.values())
    path.write_bytes(b"changed")
    rejected = client.post("/internal/editorial/route-action", headers=headers,
                           json={"action": "preview", "id": ident})
    assert rejected.status_code == 409 and rejected.json()["code"] == "MEDIA_HASH_CHANGED"
