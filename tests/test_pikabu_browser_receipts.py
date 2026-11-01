"""Mocked child-process receipts are protocol tests, never live evidence."""

import hashlib
import json
import time
from copy import deepcopy

import pytest
from PIL import Image

from max_postiz_agent.config import fingerprint
from max_postiz_agent.pikabu_browser import PikabuBrowser
from max_postiz_agent.pikabu_browser_worker import CONTRACT, ORIGIN, _atomic, _dom, _result
from max_postiz_agent.pikabu_delivery import parse_public_story
from test_pikabu_delivery import _story_html


@pytest.fixture
def receipt(tmp_path):
    media = tmp_path / "data/media/image.png"
    media.parent.mkdir(parents=True)
    Image.new("RGB", (32, 24), "blue").save(media)
    snapshot = {"title": "Космический объект", "body": "Туманность видна на снимке.\n\n"
        "Источник: https://example.org/article", "destination": "fixture_owner", "platform": "pikabu",
        "route": "pikabu_browser", "metadata": {"tags": ["Космос", "Астрономия", "Контент нейросетей"]},
        "sources": [{"url": "https://example.org/article"}],
        "media": [{"path": "data/media/image.png", "mime": "image/png",
                   "sha256": hashlib.sha256(media.read_bytes()).hexdigest()}]}
    options = {"owner": "fixture_owner", "dom_contract_verified": True, "media_contract_verified": True}
    url = ORIGIN + "/story/test_123"
    proof = parse_public_story(_story_html(ORIGIN, snapshot).encode(), url, snapshot, "fixture_owner")
    proof.update(image_sha256=[snapshot["media"][0]["sha256"]], public_dom_sha256="a" * 64,
                 observed_at=time.time(),
                 verification="independent_reload_exact_text_owner_links_tags_image_sha256")
    journal = {"contract": CONTRACT, "root": str(tmp_path.resolve()), "request_id": "receipt-test",
        "snapshot": snapshot, "snapshot_sha256": fingerprint(snapshot), "options_sha256": fingerprint(options),
        "dom_contract_sha256": fingerprint(_dom(options)), "destination": "fixture_owner", "owner": "fixture_owner",
        "execution_origin": ORIGIN, "fixture": False, "state": "confirmed", "clicks": 1,
        "external_id": "123", "permalink": url, "readback_url": url, "proof": proof,
        "created_at": time.time() - 10}
    path = tmp_path / "data/pikabu-operations/receipt-test.json"
    path.parent.mkdir(parents=True)
    def save():
        _atomic(path, journal)
        return _result(journal)
    return PikabuBrowser(tmp_path, options), snapshot, journal, path, save


def test_exact_sealed_receipt_is_accepted_only_with_bound_snapshot(receipt, monkeypatch):
    adapter, snapshot, _, _, save = receipt
    value = save()
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: value)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == "confirmed" and result.verified is True
    assert result.external_id == "123"


@pytest.mark.parametrize("damage", [None, "omit_added", "auto_empty", "effective_changed", "auto_extra", "policy_wrong", "source_wrong"])
def test_text_only_native_label_receipt_is_sealed_and_fully_bound(receipt, monkeypatch, damage):
    adapter, snapshot, journal, _, save = receipt
    snapshot["media"] = []
    raw = _story_html(ORIGIN, snapshot).replace('</div></article>',
        '<a class="tags__tag">Текст</a></div></article>').encode()
    proof = parse_public_story(raw, ORIGIN + "/story/test_123", snapshot, "fixture_owner")
    proof.update(image_sha256=[], public_dom_sha256="a" * 64, observed_at=time.time(),
                 verification="independent_reload_exact_text_owner_links_tags_image_sha256")
    if damage == "omit_added":
        proof.pop("platform_added_tags")
    elif damage == "auto_empty":
        proof["platform_added_tags"] = []
    elif damage == "effective_changed":
        proof["effective_tags_sha256"] = "b" * 64
    elif damage == "auto_extra":
        proof["platform_added_tags"] = ["Текст", "Политика"]
    elif damage == "policy_wrong":
        proof["tag_policy"] = "unreviewed-policy"
    elif damage == "source_wrong":
        proof["tag_policy_source"] = "https://example.org/unreviewed"
    journal.update(snapshot_sha256=fingerprint(snapshot), proof=proof)
    value = save()
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: value)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == ("confirmed" if damage is None else "unknown")
    assert result.verified is (damage is None)


@pytest.mark.parametrize("change", [
    "fixture", "untyped_fixture", "missing_proof", "body", "title", "images", "tags", "sources", "dom",
    "owner_attested", "request", "destination", "root", "options", "snapshot", "clicks_bool",
    "external_id", "permalink", "verified", "missing_evidence",
])
def test_malformed_stdout_never_becomes_verified(receipt, monkeypatch, change):
    adapter, snapshot, _, _, save = receipt
    value = deepcopy(save())
    proof = value["evidence"]["proof"]
    if change == "fixture":
        value["evidence"]["fixture"] = True
    elif change == "untyped_fixture":
        value["evidence"]["fixture"] = 0
    elif change == "missing_proof":
        value["evidence"].pop("proof")
    elif change in {"body", "title", "tags"}:
        proof[change + "_sha256"] = "b" * 64
    elif change == "sources":
        proof["source_urls_sha256"] = "b" * 64
    elif change == "images":
        proof["image_sha256"] = []
    elif change == "dom":
        proof.pop("public_dom_sha256")
    elif change == "owner_attested":
        proof["verification"] = "owner_attested"
    elif change == "request":
        value["evidence"]["request_id"] = "another"
    elif change in {"destination", "root"}:
        value["evidence"][change] = "another"
    elif change in {"options", "snapshot"}:
        value["evidence"][change + "_sha256"] = "b" * 64
    elif change == "clicks_bool":
        value["evidence"]["clicks"] = True
    elif change == "external_id":
        value["external_id"] = "456"
    elif change == "permalink":
        value["permalink"] = ORIGIN + "/story/other_456"
    elif change == "verified":
        value["verified"] = 1
    elif change == "missing_evidence":
        value.pop("evidence")
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: value)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == "unknown" and result.verified is False
    assert result.external_id is None


@pytest.mark.parametrize("change", ["body", "title", "images", "tags", "sources", "dom", "fixture", "readback_url",
    "declared_tags", "effective_tags", "platform_tags", "tag_policy", "tag_source", "old_contract"])
def test_invalid_proof_remains_unknown_even_when_stdout_matches_checksummed_journal(receipt, monkeypatch, change):
    adapter, snapshot, journal, _, save = receipt
    proof = journal["proof"]
    if change in {"body", "title", "tags"}:
        proof[change + "_sha256"] = "b" * 64
    elif change == "sources":
        proof["source_urls_sha256"] = "b" * 64
    elif change == "images":
        proof["image_sha256"] = ["b" * 64]
    elif change == "dom":
        proof["public_dom_sha256"] = "not-a-sha"
    elif change == "fixture":
        journal.update(fixture=True, execution_origin="http://127.0.0.1:1")
    elif change == "readback_url":
        journal["readback_url"] = "https://example.org/wrong"
    elif change in {"declared_tags", "effective_tags"}:
        proof[change + "_sha256"] = "b" * 64
    elif change == "platform_tags":
        proof["platform_added_tags"] = ["Текст"]  # Image snapshots cannot claim a text-only label.
    elif change == "tag_policy":
        proof["tag_policy"] = "unreviewed-policy"
    elif change == "tag_source":
        proof["tag_policy_source"] = "https://example.org/unreviewed"
    elif change == "old_contract":
        proof["contract"] = "pikabu-public-story-dom-v2"
    _atomic(receipt[3], journal)
    value = _result(journal, fixture=journal["fixture"])
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: value)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == "unknown" and result.verified is False


def test_reconcile_requires_frozen_database_snapshot_and_fresh_proof(receipt, monkeypatch):
    adapter, snapshot, journal, _, save = receipt
    value = save()
    calls = []
    def cached(*args, **kwargs):
        calls.append(True)
        return value
    monkeypatch.setattr(adapter, "_call", cached)
    assert adapter.reconcile("fixture_owner", "receipt-test").state == "unknown"
    altered = deepcopy(snapshot)
    altered["body"] += " Подмена."
    assert adapter.reconcile("fixture_owner", "receipt-test", snapshot=altered).state == "unknown"
    assert not calls
    assert adapter.reconcile("fixture_owner", "receipt-test", snapshot=snapshot).state == "unknown"
    def fresh(*args, **kwargs):
        journal["proof"]["observed_at"] = time.time()
        return save()
    monkeypatch.setattr(adapter, "_call", fresh)
    result = adapter.reconcile("fixture_owner", "receipt-test", snapshot=snapshot)
    assert result.state == "confirmed" and result.verified is True


def test_valid_stdout_without_durable_journal_never_confirms(receipt, monkeypatch):
    adapter, snapshot, _, path, save = receipt
    value = save()
    path.unlink()
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: value)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == "unknown" and result.verified is False


def test_sealed_journal_cannot_change_during_worker_call(receipt, monkeypatch):
    adapter, snapshot, _, path, save = receipt
    value = save()
    def changed(*args, **kwargs):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["destination"] = "another"
        _atomic(path, data)
        return value
    monkeypatch.setattr(adapter, "_call", changed)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == "unknown" and result.verified is False


@pytest.mark.parametrize("observed", [10 ** 1000, 0, -1, 1e12, True])
def test_malformed_timestamp_returns_unknown_without_overflow(receipt, monkeypatch, observed):
    adapter, snapshot, journal, _, save = receipt
    journal["proof"]["observed_at"] = observed
    value = save()
    monkeypatch.setattr(adapter, "_call", lambda *args, **kwargs: value)
    result = adapter.publish("fixture_owner", snapshot, "receipt-test")
    assert result.state == "unknown" and result.verified is False
