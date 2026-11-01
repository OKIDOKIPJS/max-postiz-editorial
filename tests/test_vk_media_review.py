"""An owner can accept exact existing media, never enable another send."""
import hashlib
import io
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.adapters import Delivery
from max_postiz_agent.db import Audit, Database
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.vk_browser import VKBrowser, prepare
from max_postiz_agent import vk_browser_worker as worker
from max_postiz_agent.vk_media import derivative_proof, upload_binding
from max_postiz_agent.vk_media_review import (
    ACTION, CONTRACT, REVOKED, VERIFICATION, accepted_proof, load_review, reference, validate_accepted_proof,
    reviewed_preview_matches,
)
from max_postiz_agent.platform_management import reconcile as core_reconcile
from test_vk_media import image_case as image_case


@pytest.fixture
def reviewed(image_case):
    root, _, options, snapshot, _ = image_case
    settings = SimpleNamespace(root=root, app={"state_dir": "state", "database_url_env": "UNUSED_FIXTURE_DB"})
    db = Database(settings, url="sqlite:///" + (root / "review.db").as_posix())
    db.initialize()
    target = Target(id="vk-owner", profile_id="test", platform="vk", route="vk_browser", destination="123456",
        enabled=False, paused=True, binding_hash="b" * 64, options=options)
    variant = Variant(id="variant", draft_id="draft", target_id=target.id, revision=1, title=snapshot["title"],
        body=snapshot["body"], media=snapshot["media"], sources=[], metadata_json={}, source_revision=1,
        config_hash="c" * 64, binding_hash="b" * 64, state="unknown")
    snapshot = HubService.snapshot(None, variant, target)
    plan = prepare(root, options, "123456", snapshot)
    due = float(int(time.time()) - 120)
    frozen = fingerprint({"content": snapshot, "due_at": due})
    approval = TargetApproval(id="approval", variant_id=variant.id, revision=1, snapshot_hash=frozen,
        scheduled_at=due, actor="owner", revoked=False)
    dispatch = Dispatch(id="existing-image", variant_id=variant.id, approval_id=approval.id, revision=1,
        snapshot_hash=frozen, snapshot=snapshot, state="unknown", due_at=due, owner=None, lease_until=0)
    actual_file = io.BytesIO()
    Image.new("RGB", (63, 47), "navy").save(actual_file, "PNG")
    actual = actual_file.getvalue()
    approved_at = float(int(time.time()) - 1)
    consent = {"contract": "owner-actual-vk-image-consent-v1", "dispatch_id": dispatch.id,
        "external_id": "123456_51", "permalink": "https://vk.ru/wall123456_51", "asset_id": "123456_77",
        "source_sha256": plan["source_image"]["source_sha256"], "source_dimensions": [64, 48],
        "actual_sha256": hashlib.sha256(actual).hexdigest(), "actual_dimensions": [63, 47],
        "native_crop_box": [0, 0, 63, 47], "acceptance_of_existing_post_only": True,
        "new_publication_authorized": False, "future_crop_policy_change_authorized": False,
        "owner_reply": "Accept this exact existing image only", "question_id": "fixture-consent",
        "recorded_at_utc": datetime.fromtimestamp(approved_at, timezone.utc).isoformat()}
    consent_path = root / "data/consent.json"
    consent_path.write_text(json.dumps(consent), encoding="utf8")
    details = {key: consent[key] for key in ("external_id", "permalink", "asset_id", "source_sha256", "source_dimensions",
        "actual_sha256", "actual_dimensions", "native_crop_box")}
    details.update(contract=CONTRACT, request_id=dispatch.id, snapshot_hash=frozen, snapshot_sha256=plan["snapshot_sha256"],
        original_approval_id=approval.id, original_due_at=due, owner="123456", destination="123456",
        consent_path="data/consent.json", consent_sha256=hashlib.sha256(consent_path.read_bytes()).hexdigest(),
        approved_at=approved_at, existing_post_only=True, new_publications_authorized=False,
        future_policy_change_authorized=False, original_policy_satisfied=False)
    audit = Audit(id="review-event", actor="owner", action=ACTION, entity=dispatch.id, details=details, at=time.time())
    with db.session.begin() as session:
        session.add_all([target, variant, approval, dispatch, audit])
    journal = {"contract": worker.CONTRACT, "root": str(root.resolve()), "request_id": dispatch.id,
        "destination": "123456", "snapshot": snapshot, "snapshot_sha256": plan["snapshot_sha256"],
        "options_sha256": fingerprint(options), "dom_contract_sha256": plan["dom_contract_sha256"],
        "state": "unknown", "clicks": 1, "created_at": time.time() - 10, "fixture": False,
        "upload_proof": upload_binding(plan, dispatch.id, "123456_77", "https://sun1.vkuserphoto.ru/upload.png",
                                       plan["source_image"]["source_sha256"])}
    path = worker._path(root, dispatch.id)
    path.parent.mkdir(parents=True)
    worker._save(path, journal, "unknown")
    result = SimpleNamespace(root=root, options=options, snapshot=snapshot, plan=plan, db=db, journal=journal, path=path,
        actual=actual, details=details, consent=consent, consent_path=consent_path)
    try:
        yield result
    finally:
        db.engine.dispose()


def review(case):
    return load_review(case.root, case.options, "123456", case.journal["request_id"], case.plan, case.journal, database=case.db)


@pytest.mark.parametrize("damage", [None, "duplicate", "revoked", "actor", "request", "snapshot", "due", "source",
    "actual", "post", "asset", "new_send", "future", "consent_hash", "consent_file", "approval_revoked",
    "variant", "target", "sending", "published", "owner", "expired_lease", "live_lease", "approval_due", "approval_hash"])
def test_authority_is_unique_current_owner_consent_and_exact_frozen_dispatch(reviewed, damage):
    case = reviewed
    with case.db.session.begin() as session:
        event = session.get(Audit, "review-event")
        details = deepcopy(event.details)
        row = session.get(Dispatch, case.journal["request_id"])
        if damage in {"duplicate", "revoked"}:
            session.add(Audit(id="another", actor="owner", action=ACTION if damage == "duplicate" else REVOKED,
                              entity=row.id, details=details))
        elif damage == "actor":
            event.actor = "agent"
        elif damage in {"request", "snapshot", "due", "source", "actual", "post", "asset", "new_send", "future", "consent_hash"}:
            key, value = {"request": ("request_id", "other"), "snapshot": ("snapshot_hash", "0" * 64),
                "due": ("original_due_at", row.due_at + 1), "source": ("source_sha256", "0" * 64),
                "actual": ("actual_sha256", "0" * 64), "post": ("external_id", "123456_52"),
                "asset": ("asset_id", "123456_78"), "new_send": ("new_publications_authorized", True),
                "future": ("future_policy_change_authorized", True), "consent_hash": ("consent_sha256", "0" * 64)}[damage]
            details[key] = value
            event.details = details
        elif damage == "consent_file":
            case.consent_path.write_text("{}", encoding="utf8")
        elif damage in {"approval_revoked", "approval_due", "approval_hash"}:
            approval = session.get(TargetApproval, row.approval_id)
            if damage == "approval_revoked":
                approval.revoked = True
            elif damage == "approval_due":
                approval.scheduled_at += 1
            else:
                approval.snapshot_hash = "0" * 64
        elif damage == "variant":
            session.get(Variant, row.variant_id).body += "Extra"
        elif damage == "target":
            session.get(Target, "vk-owner").options = {}
        elif damage in {"sending", "published"}:
            row.state = damage
        elif damage == "owner":
            row.owner = "other"
        elif damage in {"expired_lease", "live_lease"}:
            row.lease_until = time.time() + (60 if damage == "live_lease" else -60)
    if damage is None:
        assert review(case)["details"] == case.details
    else:
        with pytest.raises(AgentError):
            review(case)


def test_acceptance_defaults_off_without_audit_even_if_consent_file_exists(reviewed):
    with reviewed.db.session.begin() as session:
        session.delete(session.get(Audit, "review-event"))
    assert review(reviewed) is None


def test_unavailable_authority_is_a_sanitized_error_never_implicit_absence(reviewed):
    with reviewed.db.engine.begin() as connection:
        Audit.__table__.drop(connection)
    with pytest.raises(AgentError, match="VK_BROWSER_ACTUAL_MEDIA_AUTHORITY_UNAVAILABLE"):
        review(reviewed)


@pytest.mark.parametrize("damage", [None, "full", "full_no_cs", "path", "host", "crop", "extra_query", "cs_duplicate",
    "original_cs_duplicate", "width", "height", "upscale", "cs_height", "missing_cs", "encoded_cs", "query_order",
    "not_complete", "bool_width", "full_wrong_cs"])
def test_reviewed_viewer_preview_is_same_image_url_with_only_observed_cs_downscale(damage):
    review = {"details": {"actual_dimensions": [1399, 999], "native_crop_box": [0, 0, 1399, 999]}}
    original = "https://sun1.vkuserphoto.ru/photo.jpg?quality=95&crop=0,0,1399,999&u=fixed&cs=1399x0"
    main = {"url": original.replace("cs=1399x0", "cs=1280x0"), "width": 1280, "height": 914, "complete": True}
    if damage == "full":
        main.update(url=original, width=1399, height=999)
    elif damage == "full_no_cs":
        original = original.replace("&cs=1399x0", "")
        main.update(url=original, width=1399, height=999)
    elif damage == "path":
        main["url"] = main["url"].replace("/photo.jpg", "/other.jpg")
    elif damage == "host":
        main["url"] = main["url"].replace("sun1.", "sun2.")
    elif damage == "crop":
        main["url"] = main["url"].replace("1399,999", "1398,999")
    elif damage == "extra_query":
        main["url"] += "&unobserved=1"
    elif damage == "cs_duplicate":
        main["url"] += "&cs=1280x0"
    elif damage == "original_cs_duplicate":
        original += "&cs=1399x0"
    elif damage == "width":
        main["width"] = 1279
    elif damage == "height":
        main["height"] = 910
    elif damage == "upscale":
        main.update(url=original.replace("cs=1399x0", "cs=1400x0"), width=1400, height=1000)
    elif damage == "cs_height":
        main["url"] = main["url"].replace("1280x0", "1280x914")
    elif damage == "missing_cs":
        main["url"] = main["url"].replace("&cs=1280x0", "")
    elif damage == "encoded_cs":
        main["url"] = main["url"].replace("&cs=", "&%63s=")
    elif damage == "query_order":
        main["url"] = main["url"].replace("quality=95&crop=0,0,1399,999", "crop=0,0,1399,999&quality=95")
    elif damage == "not_complete":
        main["complete"] = False
    elif damage == "bool_width":
        main["width"] = True
    elif damage == "full_wrong_cs":
        original = main["url"]
        main.update(width=1399, height=999)
    assert reviewed_preview_matches(main, original, review) is (damage in {None, "full", "full_no_cs"})


def accepted_receipt(case):
    authority = review(case)
    proof = accepted_proof(case.actual, case.plan["source_image"], case.plan["image_contract"], "123456_77", authority,
                           "https://sun1.vkuserphoto.ru/original.png?crop=0,0,63,47")
    proof["viewer_sha256"] = "a" * 64
    reload = {"external_id": "123456_51", "text_sha256": case.plan["text_sha256"], "image_proofs": [proof],
        "text_links": [], "link_contract": case.plan["link_contract"],
        "author": {"id": "123456", "href": "/id123456", "id_attribute": None, "numeric_id": None}}
    receipt = {"verification": VERIFICATION, "external_id": "123456_51", "permalink": "https://vk.ru/wall123456_51",
        "title_format": "plain", "observed_at": time.time(), "upload": case.journal["upload_proof"],
        "reloads": [deepcopy(reload), deepcopy(reload)], "media_review": reference(authority), "original_policy_satisfied": False}
    value = {"state": "confirmed", "external_id": receipt["external_id"], "permalink": receipt["permalink"], "verified": True,
        "evidence": {"verification": "browser_readback", "fixture": False, "independent_readback": True,
            "contract": worker.CONTRACT, "request_id": case.journal["request_id"], "snapshot_sha256": case.plan["snapshot_sha256"],
            "journal_state": "confirmed", "clicks": 1, "proof": receipt}}
    return authority, proof, value


@pytest.mark.parametrize("damage", [None, "bytes", "crop", "asset", "dimensions", "review", "pretend_original", "fixture"])
def test_exact_accepted_image_is_distinct_from_original_policy_and_bound_to_authority(reviewed, damage):
    case = reviewed
    authority, proof, value = accepted_receipt(case)
    with pytest.raises(AgentError):
        derivative_proof(case.actual, case.plan["source_image"], "123456_77", case.plan["image_contract"])
    if damage == "bytes":
        proof["actual_sha256"] = "0" * 64
    elif damage == "crop":
        proof["original_url"] = proof["original_url"].replace("63,47", "62,47")
    elif damage == "asset":
        proof["asset_id"] = "123456_78"
    elif damage == "dimensions":
        proof["actual_dimensions"] = [64, 48]
    elif damage == "review":
        proof["media_review"]["review_id"] = "other"
    elif damage == "pretend_original":
        proof["original_policy_satisfied"] = True
    elif damage == "fixture":
        value["evidence"]["fixture"] = True
    value["evidence"]["proof"]["reloads"][1]["image_proofs"] = [proof]
    result = VKBrowser._delivery(value, "123456", case.journal["request_id"], case.plan,
                                expected_fixture=False, expected_media_review=authority)
    assert result.state == ("confirmed" if damage is None else "unknown")
    assert result.verified is (damage is None)
    if damage is None:
        assert validate_accepted_proof(proof, case.plan["source_image"], case.plan["image_contract"], "123456_77", authority)
        assert VKBrowser._delivery(value, "123456", case.journal["request_id"], case.plan, expected_fixture=False).state == "unknown"


@pytest.mark.parametrize("marker", ["reference", "verification"])
def test_publish_cannot_reuse_an_existing_accepted_actual_journal(reviewed, marker, monkeypatch):
    case = reviewed
    _, _, value = accepted_receipt(case)
    if marker == "verification":
        value["evidence"]["proof"].pop("media_review")
    worker._save(case.path, case.journal, "confirmed", external_id=value["external_id"], permalink=value["permalink"],
                 proof=value["evidence"]["proof"])
    calls = []
    with pytest.raises(AgentError, match="VK_BROWSER_ACTUAL_MEDIA_READ_ONLY"):
        worker.run_publish(None, case.root, case.options, {"request_id": case.journal["request_id"],
            "destination": "123456", "snapshot": case.snapshot}, lambda: calls.append("authorize"))
    monkeypatch.setattr(VKBrowser, "_call", lambda *args, **kwargs: value)
    result = VKBrowser(case.root, case.options).publish("123456", case.snapshot, case.journal["request_id"])
    assert result.state == "unknown" and result.verified is False and calls == []


@pytest.mark.parametrize("change", [None, "revoked", "changed", "deleted", "consent"])
def test_parent_reloads_authority_after_child_before_accepting_actual_receipt(reviewed, monkeypatch, change):
    from max_postiz_agent import vk_media_review as review_module
    case = reviewed
    original = review_module.load_review
    monkeypatch.setattr(review_module, "load_review", lambda *args, **kwargs: original(*args, **kwargs, database=case.db))
    def call(*args, **kwargs):
        _, _, value = accepted_receipt(case)
        worker._save(case.path, case.journal, "confirmed", external_id=value["external_id"], permalink=value["permalink"],
                     proof=value["evidence"]["proof"])
        change_authority(case, change)
        return value
    monkeypatch.setattr(VKBrowser, "_call", call)
    result = VKBrowser(case.root, case.options).reconcile("123456", case.journal["request_id"], snapshot=case.snapshot)
    assert result.state == ("confirmed" if change is None else "unknown")
    assert result.verified is (change is None)


def change_authority(case, change):
    with case.db.session.begin() as session:
        event = session.get(Audit, "review-event")
        if change == "revoked":
            session.add(Audit(id="revocation", actor="owner", action=REVOKED, entity=case.journal["request_id"], details={}))
        elif change == "changed":
            event.at += 1
        elif change == "deleted":
            session.delete(event)
        elif change == "consent":
            case.consent_path.write_text("{}", encoding="utf8")


@pytest.mark.parametrize("change", [None, "revoked", "changed", "deleted", "consent"])
def test_final_locked_db_promotion_revalidates_acceptance_in_same_session(reviewed, change, monkeypatch):
    case = reviewed
    calls = []
    def route(*args):
        calls.append("read_only")
        _, _, value = accepted_receipt(case)
        worker._save(case.path, case.journal, "confirmed", external_id=value["external_id"], permalink=value["permalink"],
                     proof=value["evidence"]["proof"])
        change_authority(case, change)
        return Delivery("confirmed", value["external_id"], value["permalink"], verified=True, evidence=value["evidence"])
    core = SimpleNamespace(db=case.db, settings=SimpleNamespace(root=case.root),
        _variant=lambda session, ident: session.get(Variant, ident), _target=lambda session, ident: session.get(Target, ident),
        _expire_send_leases=lambda **kwargs: None, snapshot=lambda variant, target: HubService.snapshot(None, variant, target),
        routes=SimpleNamespace(reconcile=route))
    if change is None:
        from max_postiz_agent import db as db_module
        monkeypatch.setattr(db_module, "Database", lambda *args, **kwargs: pytest.fail("No nested Database/session"))
    if change is None:
        result = core_reconcile(core, case.journal["request_id"])
        assert result["state"] == "published" and result["verified"] is True and result["external_mutations"] == 0
    else:
        with pytest.raises(AgentError):
            core_reconcile(core, case.journal["request_id"])
    with case.db.session() as session:
        row = session.get(Dispatch, case.journal["request_id"])
        assert row.state == ("published" if change is None else "unknown")
    assert calls == ["read_only"]
