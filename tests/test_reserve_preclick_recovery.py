"""Новый positive pre-click proof закрывает слот, сохраняя failed историю."""
import copy
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import httpx

from max_postiz_agent.browser_worker import _request_record, _snapshot_request, save
from max_postiz_agent.article_reserve import _postiz_due_matches
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from max_postiz_agent.server_models import EditorialLedger, EditorialPreparationClaim, PublicationSlot
from test_reserve_cancelled_slot_recovery import database_snapshot, other_dispatch, second_candidate

pytest_plugins = ["test_hourly_reserve_controller", "test_illustrated_article_flow"]


def positive_failure(value, monkeypatch, *, error_code="BROWSER_COMPOSER_TEXT_MISMATCH", phase="composer_ready"):
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    value.failed_slot = value.store.get(value.store.status()["active"][0]["id"])
    value.stamp[0] = record["due_at"] + 1
    if hasattr(value, "media"):
        monkeypatch.setattr(value.core.publisher, "upload_image", lambda _path: {
            "type": "browser-image", "payload": {key: value.media[key] for key in ("path", "sha256")}}, raising=False)

    def failed_before_click(chat_id, text, attachment=None, *, request_id):
        with value.core.db.session() as session:
            snapshot = copy.deepcopy(session.get(Dispatch, request_id).snapshot)
        base, presentation = _snapshot_request(value.core.settings, request_id, snapshot, chat_id)
        payload = {**base, **presentation, "dispatch_snapshot_hash": fingerprint(snapshot)}
        assert payload["text"] == text and payload["attachment"] == attachment
        journal = {**_request_record(value.core.settings, payload),
            "contract": "browser-preclick-failure-v1", "state": "preclick_failed",
            "send_started": False, "phase": phase, "failed_at": value.stamp[0],
            "error": {"code": error_code, "type": "AgentError",
                      "message": "exact private diagnostic is preserved"}}
        path = value.core.settings.root / "data/browser-publications" / (request_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, journal, exclusive=True)
        value.browser_proof_path = path
        raise AgentError("BROWSER_PRECLICK_FAILED")

    monkeypatch.setattr(value.core.publisher, "publish", failed_before_click)
    assert value.core.dispatch_tick() == 0
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, record["dispatch_id"])
        assert dispatch.state == "failed" and dispatch.attempts == 1
        before_variant = value.core.snapshot(session.get(Variant, record["variant_id"]), session.get(Target, "max"))
    value.posts[record["postiz_id"]]["state"] = "ERROR"
    value.core.bridge.sync(force=True)
    with value.core.db.session() as session:
        assert value.core.snapshot(session.get(Variant, record["variant_id"]), session.get(Target, "max")) == before_variant
        dispatch = session.get(Dispatch, record["dispatch_id"])
        assert not session.get(TargetApproval, dispatch.approval_id).revoked
    assert value.cycle.status(record["id"])["state"] == "failed"
    value.failed_record = copy.deepcopy(value.cycle._load()["records"][0])
    value.remote_error = copy.deepcopy(value.posts[record["postiz_id"]])
    return value


@pytest.fixture
def preclick_slot(reserve_controller, monkeypatch):
    return positive_failure(reserve_controller, monkeypatch)


@pytest.fixture
def millisecond_preclick_slot(illustrated, monkeypatch):
    value = illustrated
    # Set the production-like fraction BEFORE preparation, staging and approval.
    value.stamp[0] = int(value.stamp[0]) + .308235
    original_create, original_request = value.core.postiz.create, value.core.postiz.request

    def milliseconds(date):
        return datetime.fromisoformat(date.replace("Z", "+00:00")).isoformat(timespec="milliseconds")

    def create(*args, **kwargs):
        ident = original_create(*args, **kwargs)
        value.posts[ident]["publishDate"] = milliseconds(value.posts[ident]["publishDate"])
        return ident

    def request(method, path, **kwargs):
        kwargs = copy.deepcopy(kwargs)
        if method == "POST" and path == "/posts":
            kwargs["json"]["date"] = milliseconds(kwargs["json"]["date"])
        return original_request(method, path, **kwargs)

    monkeypatch.setattr(value.core.postiz, "create", create)
    monkeypatch.setattr(value.core.postiz, "request", request)
    return positive_failure(value, monkeypatch)


def release(value, **kwargs):
    return value.store.release_preclick_failed_no_send(value.failed_slot["id"],
        token=kwargs.pop("token", value.failed_slot["token"]),
        remote=kwargs.pop("remote", copy.deepcopy(value.remote_error)),
        expected_revision=kwargs.pop("expected_revision", value.state.read()["revision"]), **kwargs)


@pytest.mark.parametrize("delta,expected", [("0", True), ("-0.000235", True),
    ("0.000999", True), ("-0.000999", True), ("0.001", False), ("-0.001", False),
    ("0.001001", False), ("-0.001001", False)])
def test_postiz_timestamp_requires_strictly_less_than_one_millisecond(delta, expected):
    signed = 1793360548.308235
    observed = float(Decimal(str(signed)) + Decimal(delta))
    assert _postiz_due_matches(observed, signed) is expected


@pytest.mark.parametrize("invalid", [None, True, False, "1793360548.308", float("nan"),
    float("inf"), -float("inf"), -1, 253402300800, 10 ** 1000, 1e300])
def test_invalid_or_out_of_range_timestamp_never_matches(invalid):
    assert _postiz_due_matches(invalid, 1793360548.308235) is False
    assert _postiz_due_matches(1793360548.308235, invalid) is False


def test_millisecond_postiz_failure_releases_without_changing_signed_evidence(
        millisecond_preclick_slot, monkeypatch):
    value = millisecond_preclick_slot
    before = database_snapshot(value.core)
    signed_due = value.failed_slot["due_at"]
    dispatch_before = next(row for row in before["hub_dispatches"] if row["id"] == value.failed_record["dispatch_id"])
    approval_before = next(row for row in before["hub_approvals"] if row["id"] == dispatch_before["approval_id"])
    metadata_due = dispatch_before["snapshot"]["metadata"]["postiz_due_at"]
    assert metadata_due != signed_due
    assert 0 < Decimal(str(signed_due)) - Decimal(str(metadata_due)) < Decimal("0.001")
    assert dispatch_before["due_at"] == approval_before["scheduled_at"] == value.failed_record["due_at"] == signed_due
    raw_paths = [value.browser_proof_path, value.image_path,
        value.core.settings.root / value.failed_record["quality_trace"],
        value.core.settings.root / value.failed_record["input_file"]]
    raw_before = {path: path.read_bytes() for path in raw_paths}
    def canonical_bytes(rows):
        return json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
    attempts_before = canonical_bytes(before["hub_dispatch_attempts"])
    claims_before = canonical_bytes(before["editorial_preparation_claims"])

    assert value.controller.run_once()["slot_state"] == "missed"
    after = database_snapshot(value.core)
    assert canonical_bytes(after["hub_dispatch_attempts"]) == attempts_before
    assert canonical_bytes(after["editorial_preparation_claims"]) == claims_before
    assert after["hub_variants"] == before["hub_variants"] and after["drafts"] == before["drafts"]
    assert {path: path.read_bytes() for path in raw_paths} == raw_before
    dispatch_after = next(row for row in after["hub_dispatches"] if row["id"] == dispatch_before["id"])
    approval_after = next(row for row in after["hub_approvals"] if row["id"] == approval_before["id"])
    assert dispatch_after == {**dispatch_before, "state": "cancelled"}
    assert approval_after == {**approval_before, "revoked": True}
    slot_after = value.store.get(value.failed_slot["id"])
    for key in ("due_at", "token", "binding", "text_hash", "evidence_hash", "dispatch_id"):
        assert slot_after[key] == value.failed_slot[key]
    record = value.cycle._load()["records"][0]
    assert record["state"] == "cancelled" and record["error"] == "BROWSER_PRECLICK_FAILED"
    assert record["no_send_disposition"]["source_remains_used"] is True
    assert (record["url"], record["original_hash"]) == (value.failed_record["url"], value.failed_record["original_hash"])
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PRECLICK_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == after
    monkeypatch.setattr(value.core.publisher, "publish", lambda *_a, **_k: pytest.fail("Never retry failed source"))
    assert value.core.dispatch_tick() == 0
    old_model_events = list(value.events)
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare(copy.deepcopy(value.article), reserve_claim=claim)
    assert value.events == old_model_events and len(value.writes) == 1
    assert {path: path.read_bytes() for path in raw_paths} == raw_before


@pytest.mark.parametrize("damage", ["unknown_attempt", "proof_send_started", "revoked", "lease",
    "remote_body", "signed_dispatch_due", "signed_approval_due", "signed_slot_due"])
def test_millisecond_tolerance_never_bypasses_other_positive_gates(millisecond_preclick_slot, damage):
    value = millisecond_preclick_slot
    remote = copy.deepcopy(value.remote_error)
    with value.core.db.session.begin() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        if damage == "unknown_attempt":
            session.query(DispatchAttempt).one().result = "unknown"
        elif damage == "revoked":
            session.get(TargetApproval, dispatch.approval_id).revoked = True
        elif damage == "lease":
            dispatch.lease_until = value.stamp[0] + 60
        elif damage == "signed_dispatch_due":
            dispatch.due_at += .0001
        elif damage == "signed_approval_due":
            session.get(TargetApproval, dispatch.approval_id).scheduled_at += .0001
        elif damage == "signed_slot_due":
            session.get(PublicationSlot, value.failed_slot["id"]).due_at += .0001
    if damage == "proof_send_started":
        proof = json.loads(value.browser_proof_path.read_text(encoding="utf-8"))
        proof["send_started"] = True
        value.browser_proof_path.write_text(json.dumps(proof), encoding="utf-8")
    elif damage == "remote_body":
        remote["content"] += " changed"
    before, raw = database_snapshot(value.core), value.browser_proof_path.read_bytes()
    with pytest.raises(AgentError):
        release(value, remote=remote)
    assert database_snapshot(value.core) == before
    assert value.browser_proof_path.read_bytes() == raw
    assert value.store.status()["active"] and len(value.writes) == 1


def test_preclick_no_send_closes_atomically_and_preserves_attempt_source_qa_claims(preclick_slot):
    value = preclick_slot
    before, journal = database_snapshot(value.core), value.browser_proof_path.read_bytes()
    assert release(value)["state"] == "missed"
    after = database_snapshot(value.core)
    assert {name for name in before if before[name] != after[name]} == {
        "hub_dispatches", "hub_approvals", "editorial_article_ledger", "publication_slots", "audit_events"}
    assert after["hub_dispatch_attempts"] == before["hub_dispatch_attempts"]
    assert after["editorial_preparation_claims"] == before["editorial_preparation_claims"]
    assert value.browser_proof_path.read_bytes() == journal
    record = value.cycle._load()["records"][0]
    disposition = record.pop("no_send_disposition")
    assert record == {**value.failed_record, "state": "cancelled", "dispatch_state": "cancelled"}
    assert disposition["source_remains_used"] is True
    assert disposition["contract"] == "reserve-preclick-no-send-v1"
    assert value.store.status()["active"] == []
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        assert dispatch.state == "cancelled" and dispatch.attempts == 1
        assert dispatch.error == "BROWSER_PRECLICK_FAILED"
        assert session.get(TargetApproval, dispatch.approval_id).revoked
        assert session.get(Target, "max").paused
        assert session.query(DispatchAttempt).one().result == "failed"
        assert session.query(Audit).filter_by(action="reserve_preclick_no_send_missed").count() == 1
    stable = database_snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PRECLICK_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == stable
    assert len(value.writes) == 1


@pytest.mark.parametrize("change", [
    "attempt0", "attempt2", "extra_failed_attempt", "unknown_attempt", "published_attempt", "other_error",
    "sending", "unknown", "published", "owner", "lease", "verified", "external_id", "permalink",
    "revoked", "approval_date", "approval_snapshot", "mutation_owner", "mutation_until", "inactive_bridge",
    "bridge_error", "slot_binding", "record_unknown", "changed_variant", "changed_snapshot", "active_claim",
    "proof_missing", "proof_legacy", "proof_unknown", "proof_send_started", "proof_bad_hash", "proof_partial",
    "proof_wrong_request", "proof_wrong_snapshot", "proof_wrong_body", "proof_wrong_chat",
    "remote_queue", "remote_published", "remote_body", "remote_group", "remote_integration", "remote_date",
    "remote_release", "future_due", "wrong_token", "stale_revision", "paused",
])
def test_invalid_or_uncertain_preclick_state_never_releases(preclick_slot, change):
    value, kwargs = preclick_slot, {}
    remote = copy.deepcopy(value.remote_error)
    with value.core.db.session.begin() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        approval = session.get(TargetApproval, dispatch.approval_id)
        bridge = session.get(BridgePost, value.failed_record["postiz_id"])
        if change in {"attempt0", "attempt2"}:
            dispatch.attempts = int(change[-1])
        elif change in {"extra_failed_attempt", "unknown_attempt", "published_attempt"}:
            session.add(DispatchAttempt(dispatch_id=dispatch.id, created_at=value.stamp[0],
                result={"extra_failed_attempt": "failed", "unknown_attempt": "unknown", "published_attempt": "published"}[change],
                error="BROWSER_PRECLICK_FAILED"))
        elif change in {"sending", "unknown", "published"}:
            dispatch.state = change
        elif change == "other_error":
            dispatch.error = "BROWSER_OPERATION_UNCONFIRMED"
        elif change == "owner":
            dispatch.owner = "worker"
        elif change == "lease":
            dispatch.lease_until = value.stamp[0] + 60
        elif change == "verified":
            dispatch.verified = True
        elif change in {"external_id", "permalink"}:
            setattr(dispatch, change, "receipt")
        elif change == "revoked":
            approval.revoked = True
        elif change == "approval_date":
            approval.scheduled_at += 1
        elif change == "approval_snapshot":
            approval.snapshot_hash = "0" * 64
        elif change == "mutation_owner":
            bridge.mutation_owner = "worker"
        elif change == "mutation_until":
            bridge.mutation_until = value.stamp[0] + 60
        elif change == "inactive_bridge":
            bridge.active = False
        elif change == "bridge_error":
            bridge.error = "REMOTE_EDIT_DURING_SEND"
        elif change == "slot_binding":
            slot = session.get(PublicationSlot, value.failed_slot["id"])
            slot.binding = {**slot.binding, "original_hash": "0" * 64}
        elif change == "record_unknown":
            ledger = session.get(EditorialLedger, "hourly")
            ledger.payload = {**ledger.payload, "records": [{**r, "state": "unknown"} for r in ledger.payload["records"]]}
        elif change == "changed_variant":
            session.get(Variant, dispatch.variant_id).body += " changed"
        elif change == "changed_snapshot":
            dispatch.snapshot = {**dispatch.snapshot, "title": "changed"}
        elif change == "active_claim":
            session.add(EditorialPreparationClaim(active_key="models", state="preparing", control_revision=1))
    if change.startswith("proof_"):
        path = value.browser_proof_path
        proof = json.loads(path.read_text(encoding="utf-8"))
        if change == "proof_missing":
            path.unlink()
        elif change == "proof_partial":
            path.with_suffix(".new").write_text("interrupted", encoding="utf-8")
        else:
            field, changed = {
                "proof_legacy": ("contract", "browser-send-journal-v1"),
                "proof_unknown": ("state", "sending"), "proof_send_started": ("send_started", True),
                "proof_bad_hash": ("content_hash", "0" * 64), "proof_wrong_request": ("request_id", "other"),
                "proof_wrong_snapshot": ("snapshot_hash", "0" * 64), "proof_wrong_body": ("body_sha256", "0" * 64),
                "proof_wrong_chat": ("chat_id", "other"),
            }[change]
            proof[field] = changed
            path.write_text(json.dumps(proof), encoding="utf-8")
    elif change in {"remote_queue", "remote_published"}:
        remote["state"] = "QUEUE" if change == "remote_queue" else "PUBLISHED"
    elif change == "remote_body":
        remote["content"] += " changed"
    elif change == "remote_group":
        remote["group"] = "other"
    elif change == "remote_integration":
        remote["integration"] = {"id": "other"}
    elif change == "remote_date":
        remote["publishDate"] = datetime.fromtimestamp(value.failed_slot["due_at"] + 1, UTC).isoformat()
    elif change == "remote_release":
        remote["releaseId"] = "published"
    elif change == "future_due":
        value.stamp[0] = value.failed_slot["due_at"] - 1
    elif change == "wrong_token":
        kwargs["token"] = "wrong"
    elif change == "stale_revision":
        kwargs["expected_revision"] = value.state.read()["revision"] - 1
    elif change == "paused":
        value.state.control("pause", value.state.read()["revision"], "test-owner")
    before = database_snapshot(value.core)
    with pytest.raises(AgentError):
        release(value, remote=remote, **kwargs)
    assert database_snapshot(value.core) == before
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    assert len(value.writes) == 1


def test_controller_moves_to_distinct_ready_source_and_preserves_old_claim(preclick_slot):
    value = preclick_slot
    ready = second_candidate(value)
    assert ready["state"] == "quality_ready"
    assert value.controller.run_once()["slot_state"] == "missed"
    value.stamp[0] += 7200  # No catch-up slot or repeat of the failed source.
    current = value.stamp[0]
    assert value.controller.run_once()["phase"] == "waiting_dispatch"
    records = value.cycle._load()["records"]
    assert records[0]["state"] == "cancelled" and records[0]["error"] == "BROWSER_PRECLICK_FAILED"
    assert records[1]["due_at"] >= current + 120
    assert records[1]["url"] != records[0]["url"] and records[1]["postiz_id"] != records[0]["postiz_id"]
    assert len(value.writes) == 2


@pytest.mark.parametrize("state", ["pending", "sending", "unknown", "lease"])
def test_other_dispatch_or_lease_blocks_new_preclick_disposal(preclick_slot, state):
    value = preclick_slot
    other = second_candidate(value)
    ident = other_dispatch(value, other, "cancelled" if state == "lease" else state)
    if state == "lease":
        with value.core.db.session.begin() as session:
            session.get(Dispatch, ident).lease_until = value.stamp[0] + 1
    before = database_snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PRECLICK_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == before


def test_legacy_unknown_without_positive_proof_remains_blocked(preclick_slot):
    value = preclick_slot
    value.browser_proof_path.unlink()
    with value.core.db.session.begin() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        dispatch.state, dispatch.error = "unknown", None
    before = database_snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PRECLICK_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == before
    assert value.controller.run_once()["phase"] == "blocked"
    assert value.store.status()["active"]


def test_controller_failed_code_without_durable_proof_keeps_slot_blocked(preclick_slot):
    value = preclick_slot
    value.browser_proof_path.unlink()
    assert value.controller.run_once()["phase"] == "blocked"
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        assert dispatch.state == "failed" and dispatch.attempts == 1
        assert session.query(DispatchAttempt).one().result == "failed"
    assert len(value.writes) == 1


def test_channel_429_keeps_failed_slot_and_never_selects_or_sends_replacement(reserve_controller, monkeypatch):
    value = positive_failure(reserve_controller, monkeypatch,
        error_code="BROWSER_CHANNEL_RATE_LIMITED", phase="channel")
    ready = second_candidate(value)
    assert ready["state"] == "quality_ready"
    before = database_snapshot(value.core)
    records_before = copy.deepcopy(value.cycle._load()["records"])
    raw_paths = [value.browser_proof_path, value.core.settings.root / value.failed_record["quality_trace"],
        value.core.settings.root / value.failed_record["input_file"]]
    raw_before = {path: path.read_bytes() for path in raw_paths}
    model_calls, posts = list(value.model_calls), copy.deepcopy(value.posts)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PRECLICK_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == before
    assert {path: path.read_bytes() for path in raw_paths} == raw_before

    def forbidden(*_args, **_kwargs):
        pytest.fail("MAX 429 must not select, stage or send a replacement")

    monkeypatch.setattr(value.controller, "selector", forbidden)
    monkeypatch.setattr(value.core, "stage_remote", forbidden)
    monkeypatch.setattr(value.core.publisher, "publish", forbidden)
    for _ in range(2):
        assert value.controller.run_once()["phase"] == "blocked"
        assert value.core.dispatch_tick() == 0
    after = database_snapshot(value.core)
    for table in ("hub_dispatches", "hub_dispatch_attempts", "hub_approvals", "hub_variants", "drafts",
                  "editorial_preparation_claims", "publication_slots", "materials", "stories"):
        assert after[table] == before[table]
    assert value.cycle._load()["records"] == records_before
    assert {path: path.read_bytes() for path in raw_paths} == raw_before
    assert value.model_calls == model_calls and value.posts == posts and len(value.writes) == 1
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    assert not any(row["action"] == "reserve_preclick_no_send_missed" for row in after["audit_events"])


def test_control_change_during_fresh_get_invalidates_preclick_disposal(preclick_slot, monkeypatch):
    value = preclick_slot

    def changed_control(_ident):
        value.state.control("pause", value.state.read()["revision"], "other-owner")
        value.state.control("start", value.state.read()["revision"], "other-owner")
        return copy.deepcopy(value.remote_error)

    monkeypatch.setattr(value.core.postiz, "get_post", changed_control)
    with pytest.raises(AgentError, match="EDITORIAL_REVISION_CONFLICT"):
        value.controller.run_once()
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    assert len(value.writes) == 1


@pytest.mark.parametrize("damage", ["missing", "legacy", "sending", "wrong_snapshot"])
def test_bridge_state_only_error_path_requires_new_exact_proof(preclick_slot, damage):
    value = preclick_slot
    path = value.browser_proof_path
    if damage == "missing":
        path.unlink()
    else:
        proof = json.loads(path.read_text(encoding="utf-8"))
        field, changed = {"legacy": ("contract", "browser-send-journal-v1"),
            "sending": ("send_started", True), "wrong_snapshot": ("snapshot_hash", "0" * 64)}[damage]
        proof[field] = changed
        path.write_text(json.dumps(proof), encoding="utf-8")
    with value.core.db.session.begin() as session:
        link = session.get(BridgePost, value.failed_record["postiz_id"])
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        link.remote_state, link.remote_hash = "QUEUE", dispatch.snapshot["metadata"]["postiz_hash"]
        revision = session.get(Variant, value.failed_record["variant_id"]).revision
        audits = session.query(Audit).filter_by(action="bridge_preclick_state_only").count()
    value.core.bridge.sync(force=True)
    with value.core.db.session() as session:
        assert session.query(Audit).filter_by(action="bridge_preclick_state_only").count() == audits
        # Existing conservative import remains in charge; it cannot create the new proof.
        assert session.get(Variant, value.failed_record["variant_id"]).revision == revision + 1
    with pytest.raises(AgentError):
        release(value)
    assert value.store.status()["active"]


def test_postgres_preclick_disposal_preserves_failed_attempt_and_blocks_replay(postgres_reserve, monkeypatch):
    value = positive_failure(postgres_reserve, monkeypatch)
    assert value.core.db.engine.dialect.name == "postgresql"
    before = database_snapshot(value.core)
    assert release(value)["state"] == "missed"
    after = database_snapshot(value.core)
    assert after["hub_dispatch_attempts"] == before["hub_dispatch_attempts"]
    assert after["hub_variants"] == before["hub_variants"]
    assert after["drafts"] == before["drafts"]
    assert after["editorial_preparation_claims"] == before["editorial_preparation_claims"]
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PRECLICK_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == after


def test_illustrated_preclick_proof_binds_image_title_and_presentation(illustrated, monkeypatch):
    value = positive_failure(illustrated, monkeypatch)
    proof = json.loads(value.browser_proof_path.read_text(encoding="utf-8"))
    assert proof["request_attachment"]["payload"]["sha256"] == value.media["sha256"]
    assert proof["presentation"]["illustration_proof_hash"] == fingerprint(value.binding)
    assert proof["presentation"]["title_format"] == "bold"
    assert release(value)["state"] == "missed"
    assert value.store.status()["active"] == []


@pytest.mark.parametrize("damage", ["remote_bytes", "attachment_sha", "image_url", "local_bytes"])
def test_illustrated_preclick_requires_exact_bytes_and_rights_binding(illustrated, monkeypatch, damage):
    value = positive_failure(illustrated, monkeypatch)
    remote = copy.deepcopy(value.remote_error)
    if damage == "remote_bytes":
        value.core.postiz.client.close()
        value.core.postiz.client = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"changed image", headers={"content-type": "image/jpeg"})))
    elif damage == "attachment_sha":
        proof = json.loads(value.browser_proof_path.read_text(encoding="utf-8"))
        proof["request_attachment"]["payload"]["sha256"] = "0" * 64
        value.browser_proof_path.write_text(json.dumps(proof), encoding="utf-8")
    elif damage == "image_url":
        remote["image"] = [{"id": "image-fixture", "path": "/uploads/other.jpg"}]
    else:
        value.image_path.write_bytes(b"changed locally")
    before = database_snapshot(value.core)
    with pytest.raises(AgentError):
        release(value, remote=remote)
    assert database_snapshot(value.core) == before
    assert value.store.status()["active"]
