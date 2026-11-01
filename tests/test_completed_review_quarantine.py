"""Known complete protocol failures release only their owned pre-DRAFT source."""

import copy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.adapters import OpenCode
from max_postiz_agent.article_quality import ArticleQuality, REVIEW_BOUNDS_V2_CONTRACT
from max_postiz_agent.completed_review import completed_review_proof
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Material, Story
from max_postiz_agent.hub_models import BridgePost, Dispatch, TargetApproval, Variant
from max_postiz_agent.server_models import EditorialPreparationClaim
from test_automatic_policy import Model
from test_reserve_cancelled_slot_recovery import database_snapshot

pytest_plugins = ["test_hourly_reserve_controller"]
RAW = json.dumps(
    {
        "approved": False,
        "topic": "science",
        "issues": ["Полный отказ " + str(i) for i in range(17)],
        "warnings": [],
    },
    ensure_ascii=False,
)


def configure(value, monkeypatch, raw=RAW, *, status=200):
    cycle, core = value.cycle, value.core
    policy = cycle._policy()
    policy["review_rubric"] = "hourly-practical-v2"
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    cycle.review_protocol_contract = REVIEW_BOUNDS_V2_CONTRACT
    for source in (Path(__file__).resolve().parents[1] / "prompts").glob("ARTICLE*.txt"):
        (core.settings.root / "prompts" / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr("max_postiz_agent.opencode_journal.time.time", lambda: value.stamp[0])
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "sesCompletedProof"}})
        if request.url.path.endswith("/generate"):
            if isinstance(raw, Exception):
                raise raw
            return httpx.Response(status, json={"data": {"text": raw() if callable(raw) else raw}})
        return httpx.Response(200, json={})

    def quality(record_id=None, review_only=False):
        reviewer = OpenCode(core.settings, transport=httpx.MockTransport(handler))
        reviewer.mode, reviewer.base_url, reviewer.model = (
            "live",
            "http://local.invalid",
            "local/ministral",
        )
        reviewer.options = {
            **reviewer.options,
            "generate_transport": "session",
            "runtime_dir": "data/opencode",
        }
        return ArticleQuality(
            Model(
                {
                    "title": "Космический ветер",
                    "body": "Учёные изучили необычные процессы в атмосфере далёкой звезды.",
                },
                "hy-mt2-7b-q8",
            ),
            reviewer,
            prompt_dir=core.settings.root / "prompts",
            trace=cycle._trace_writer(record_id),
            review_rubric="hourly-practical-v2",
            review_protocol_contract=REVIEW_BOUNDS_V2_CONTRACT,
            transport=httpx.MockTransport(
                lambda _: pytest.fail("Protocol refusal must not call LanguageTool")
            ),
        )

    monkeypatch.setattr(cycle, "_quality", quality)
    value.reviewer_calls = calls
    return value


def prepare_failure(value, monkeypatch, raw=RAW, *, status=200, error="ARTICLE_QUALITY_REJECTED"):
    configure(value, monkeypatch, raw, status=status)
    value.claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match=error):
        value.cycle.prepare(value.article, reserve_claim=value.claim)
    value.record = copy.deepcopy(value.cycle._load()["records"][0])
    return value


@pytest.fixture
def complete(reserve_controller, monkeypatch):
    return prepare_failure(reserve_controller, monkeypatch)


def close(value, **kwargs):
    return value.store.quarantine_completed_review(
        kwargs.pop("claim", value.claim),
        kwargs.pop("record_id", value.record["id"]),
        cycle=value.cycle,
        **kwargs,
    )


def change_record(value, change):
    ledger = value.cycle._load()
    change(ledger["records"][0])
    value.cycle._save(ledger)


def raw_files(value):
    return {
        str(path.relative_to(value.core.settings.root)): path.read_bytes()
        for directory in ("hourly-articles", "opencode-requests")
        for path in (value.core.settings.root / "data" / directory).rglob("*")
        if path.is_file()
    }


def test_complete_invalid_reply_is_anchored_and_atomically_quarantined_without_replay(complete):
    value = complete
    proof = completed_review_proof(value.cycle, value.record)
    assert proof["kind"] == "initial" and proof["classification"]["approval_permitted"] is False
    assert proof == value.record["preparation_attempts"][-1]["completed_review_protocol"]
    before, files, calls = (
        database_snapshot(value.core),
        raw_files(value),
        list(value.reviewer_calls),
    )
    closed = close(value)
    after = database_snapshot(value.core)
    assert closed == {
        "record_id": value.record["id"],
        "claim_id": value.claim["id"],
        "state": "cancelled",
        "proof_hash": fingerprint(proof),
    }
    changed = {table for table in before if before[table] != after[table]}
    assert changed == {"editorial_article_ledger", "editorial_preparation_claims", "audit_events"}
    record = value.cycle._load()["records"][0]
    assert record["preparation_attempts"] == value.record["preparation_attempts"]
    assert record["error"] == value.record["error"] == "ARTICLE_QUALITY_REJECTED"
    assert record["protocol_guard"]["source_remains_used"] and record["protocol_guard"]["no_retry"]
    assert {key: record[key] for key in value.record if key != "state"} == {
        key: val for key, val in value.record.items() if key != "state"
    }
    assert not value.store.active_preparation() and not value.posts and not value.writes
    assert raw_files(value) == files and value.reviewer_calls == calls
    with value.core.db.session() as session:
        assert (
            session.query(Dispatch).count()
            == session.query(TargetApproval).count()
            == session.query(Variant).count()
            == 0
        )
        audits = list(
            session.scalars(
                select(Audit).where(Audit.action == "reserve_completed_review_quarantined")
            )
        )
        assert (
            len(audits) == 1
            and audits[0].details["model_calls"] == audits[0].details["external_operations"] == 0
        )
    with pytest.raises(AgentError):
        close(value)
    with pytest.raises(AgentError):
        value.cycle.prepare(value.article, reserve_claim=value.store.begin_preparation())
    assert value.reviewer_calls == calls and not value.posts and not value.writes


def test_restart_blocked_unknown_releases_only_positive_anchored_proof(complete):
    value = complete
    with value.core.db.session.begin() as session:
        claim = session.get(EditorialPreparationClaim, value.claim["id"])
        claim.state, claim.error = "blocked_unknown", "ARTICLE_RECONCILIATION_REQUIRED"
    assert close(value)["state"] == "cancelled"
    with value.core.db.session() as session:
        assert (
            session.get(EditorialPreparationClaim, value.claim["id"]).error
            == "ARTICLE_RECONCILIATION_REQUIRED"
        )


def test_fresh_controller_recovers_completed_claim_and_never_reselects_old_source(complete):
    from max_postiz_agent.article_cycle import HourlyArticleCycle
    from max_postiz_agent.hourly_reserve import ReserveServerCycle

    value = complete
    with value.core.db.session.begin() as session:
        claim = session.get(EditorialPreparationClaim, value.claim["id"])
        claim.state, claim.error = "blocked_unknown", "ARTICLE_RECONCILIATION_REQUIRED"
    files, calls = raw_files(value), list(value.reviewer_calls)
    selected = []

    def no_source(_settings, *, ledger, **_):
        selected.append(copy.deepcopy(ledger))
        assert ledger["records"][0]["id"] == value.record["id"]
        assert ledger["records"][0]["state"] == "cancelled"
        return None

    fresh = ReserveServerCycle(
        value.core,
        state=value.state,
        selector=no_source,
        cycle=HourlyArticleCycle(value.core, reserve_mode=True),
        clock=lambda: value.stamp[0],
        reserve_store=value.store,
    )
    assert fresh.prepare_once()["phase"] == "no_source"
    assert fresh.prepare_once()["phase"] == "no_source"
    assert len(selected) == 2 and len(value.cycle._load()["records"]) == 1
    assert (
        not value.store.active_preparation()
        and value.reviewer_calls == calls
        and raw_files(value) == files
    )
    with value.core.db.session() as session:
        assert (
            session.query(Audit).filter_by(action="reserve_completed_review_quarantined").count()
            == 1
        )
    assert not value.writes and not value.posts


@pytest.mark.parametrize(
    "kind", ["bridge_owner", "bridge_until", "dispatch_owner", "dispatch_until", "dispatch_unknown"]
)
def test_other_owner_or_orphan_lease_is_preserved_before_quarantine(complete, kind):
    from test_automatic_article_repair import add_unrelated_lease

    value = complete
    add_unrelated_lease(value, owner=kind == "bridge_owner")
    with value.core.db.session.begin() as session:
        link = session.get(BridgePost, "other-draft")
        if kind == "bridge_owner":
            link.mutation_until = 0
        elif kind == "bridge_until":
            link.mutation_until = value.stamp[0] - 1
        else:
            link.mutation_until = 0
            variant = session.get(Variant, link.variant_id)
            approval = TargetApproval(
                variant_id=variant.id,
                revision=variant.revision,
                snapshot_hash="b" * 64,
                scheduled_at=value.stamp[0],
                actor="test-owner",
            )
            session.add(approval)
            session.flush()
            session.add(
                Dispatch(
                    variant_id=variant.id,
                    approval_id=approval.id,
                    revision=variant.revision,
                    snapshot_hash="b" * 64,
                    snapshot={},
                    due_at=value.stamp[0],
                    owner="other-owner" if kind == "dispatch_owner" else None,
                    lease_until=value.stamp[0] - 1 if kind == "dispatch_until" else 0,
                    state="unknown" if kind == "dispatch_unknown" else "pending",
                )
            )
    before, calls = database_snapshot(value.core), list(value.reviewer_calls)
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value)
    assert database_snapshot(value.core) == before and value.reviewer_calls == calls


@pytest.mark.parametrize(
    "raw",
    [
        "This is the complete non-JSON answer.",
        "{broken",
        "[]",
        '{"approved":true,"topic":"science","issues":["Contradiction"],"warnings":[]}',
        '{"approved":NaN,"topic":"science","issues":[],"warnings":[]}',
        '{"approved":false,"topic":"outside_contract","issues":["Full refusal"],"warnings":[]}',
        '{"approved":true,"approved":false,"topic":"science","issues":[],"warnings":[]}',
    ],
)
def test_full_successful_invalid_format_can_only_quarantine(reserve_controller, monkeypatch, raw):
    value = prepare_failure(reserve_controller, monkeypatch, raw)
    assert close(value)["state"] == "cancelled" and not value.writes


@pytest.mark.parametrize(
    "field",
    [
        "record_marker",
        "attempt_marker",
        "model_unknown",
        "attempt_unfinished",
        "missing_anchor",
        "wrong_anchor",
        "lost_claim",
        "wrong_fence",
        "wrong_record",
        "claim_failed",
        "input_changed",
        "trace_changed",
        "completion_missing",
        "completion_failure",
        "response_changed",
        "validated_changed",
        "response_non200",
        "prompt_changed",
        "session_changed",
        "external_story",
        "external_material",
    ],
)
def test_uncertain_or_changed_evidence_never_changes_database_or_history(complete, field):
    value = complete
    record = value.record
    completion = record["preparation_attempts"][-1]["completed_review_protocol"]["completion"]
    directory = value.core.settings.root / completion["directory"]

    def mutate(r):
        attempt = r["preparation_attempts"][-1]
        if field == "record_marker":
            r.pop("review_protocol_contract")
        if field == "attempt_marker":
            attempt.pop("review_protocol_contract")
        if field == "model_unknown":
            r["model_result_unknown"] = True
        if field == "attempt_unfinished":
            attempt.pop("finished_at")
        if field == "missing_anchor":
            attempt.pop("completed_review_protocol")
        if field == "wrong_anchor":
            attempt["completed_review_protocol"]["raw_review_sha256"] = "0" * 64

    change_record(value, mutate)
    argument = copy.deepcopy(value.claim)
    if field == "lost_claim":
        argument["token"] = "lost-token"
    if field == "wrong_fence":
        argument["control_revision"] -= 1
    if field == "wrong_record":
        record = {**record, "id": "0" * 36}
    if field == "claim_failed":
        with value.core.db.session.begin() as session:
            session.get(EditorialPreparationClaim, value.claim["id"]).state = "failed"
    if field == "input_changed":
        (value.core.settings.root / value.record["input_file"]).write_text("{}")
    if field == "trace_changed":
        path = value.core.settings.root / value.record["quality_trace"]
        path.write_bytes(path.read_bytes() + b"{}\n")
    if field == "completion_missing":
        (directory / "validated.json").unlink()
    if field == "completion_failure":
        (directory / "failure.json").write_text("{}")
    if field == "response_changed":
        (directory / "response.raw").write_text("{}")
    if field in {"validated_changed", "response_non200", "prompt_changed", "session_changed"}:
        filename, key, changed = {
            "validated_changed": ("validated.json", "text_sha256", "0" * 64),
            "response_non200": ("response.json", "status_code", 503),
            "prompt_changed": ("intent.json", "prompt", "other prompt"),
            "session_changed": ("session.json", "session_id", "sesOther"),
        }[field]
        path = directory / filename
        obj = json.loads(path.read_bytes())
        obj[key] = changed
        path.write_text(json.dumps(obj))
    if field in {"external_story", "external_material"}:
        with value.core.db.session.begin() as session:
            if field == "external_story":
                session.add(
                    Story(
                        title="Existing",
                        text="Existing",
                        text_hash=fingerprint({"hourly_article": value.record["id"]}),
                    )
                )
            else:
                session.add(
                    Material(
                        source_id="example_rss",
                        external_id="existing",
                        story_id="existing",
                        title="Existing",
                        text="Existing",
                        url=value.record["url"],
                        text_hash="a" * 64,
                        language="en",
                    )
                )
    before, files, calls = (
        database_snapshot(value.core),
        raw_files(value),
        list(value.reviewer_calls),
    )
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value, claim=argument, record_id=record["id"])
    assert database_snapshot(value.core) == before and raw_files(value) == files
    assert value.reviewer_calls == calls and not value.posts and not value.writes


@pytest.mark.parametrize(
    "failure,status,error",
    [("timeout", 200, "OPENCODE_GENERATION_FAILED"), ("503", 503, "OPENCODE_PROVIDER_UNAVAILABLE")],
)
def test_timeout_or_failed_http_never_gets_completion_or_release(
    reserve_controller, monkeypatch, failure, status, error
):
    raw = httpx.ReadTimeout("no completed response") if failure == "timeout" else RAW
    value = prepare_failure(reserve_controller, monkeypatch, raw, status=status, error=error)
    assert "completed_review_protocol" not in value.record["preparation_attempts"][-1]
    before = database_snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value)
    assert database_snapshot(value.core) == before and not value.writes


def test_completion_beyond_quarantine_raw_bound_stays_blocked_with_full_raw_preserved(
    reserve_controller, monkeypatch
):
    value = prepare_failure(reserve_controller, monkeypatch, "x" * 262145)
    assert "completed_review_protocol" not in value.record["preparation_attempts"][-1]
    before, files = database_snapshot(value.core), raw_files(value)
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value)
    assert database_snapshot(value.core) == before and raw_files(value) == files


@pytest.mark.parametrize("damage", ["error_hash", "error_missing", "attempt_marker"])
def test_both_durable_prepare_audits_are_required(complete, damage):
    value = complete
    with value.core.db.session.begin() as session:
        audit = session.scalar(
            select(Audit).where(
                Audit.entity == value.record["id"],
                Audit.action
                == (
                    "hourly_article_prepare_attempt"
                    if damage == "attempt_marker"
                    else "hourly_article_prepare_error"
                ),
            )
        )
        if damage == "error_missing":
            session.delete(audit)
        elif damage == "error_hash":
            audit.details = {**audit.details, "completed_review_protocol_hash": "0" * 64}
        else:
            audit.details = {
                key: val for key, val in audit.details.items() if key != "review_protocol_contract"
            }
    before, files = database_snapshot(value.core), raw_files(value)
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value)
    assert database_snapshot(value.core) == before and raw_files(value) == files


def test_valid_four_issue_negative_is_semantic_refusal_not_protocol_quarantine(
    reserve_controller, monkeypatch
):
    review = json.loads(RAW)
    review["issues"] = review["issues"][:4]
    value = configure(reserve_controller, monkeypatch, json.dumps(review, ensure_ascii=False))
    # A valid negative goes through the regular independent LanguageTool check.
    original = value.cycle._quality

    def quality(*args, **kwargs):
        checked = original(*args, **kwargs)
        checked.spelling.close()
        checked.spelling = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []}))
        )
        return checked

    monkeypatch.setattr(value.cycle, "_quality", quality)
    value.claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.cycle.prepare(value.article, reserve_claim=value.claim)
    value.record = value.cycle._load()["records"][0]
    assert "completed_review_protocol" not in value.record["preparation_attempts"][-1]
    before = database_snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value)
    assert database_snapshot(value.core) == before


@pytest.fixture
def candidate_failure(reserve_controller, monkeypatch):
    from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL

    value = reserve_controller
    phase = {"qwen": 0}
    semantic = json.dumps(
        {
            "approved": False,
            "topic": "science",
            "issues": ["The discovery of the stellar wind was omitted."],
            "warnings": [],
        }
    )
    configure(value, monkeypatch, lambda: RAW if phase["qwen"] else semantic)
    original = value.cycle._quality

    def quality(*args, **kwargs):
        checked = original(*args, **kwargs)
        checked.spelling.close()
        checked.spelling = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []}))
        )
        return checked

    monkeypatch.setattr(value.cycle, "_quality", quality)

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, _prompt):
            phase["qwen"] += 1
            return json.dumps(
                {
                    "title": "Космический ветер",
                    "body": "Учёные изучили необычную атмосферу далёкой звезды и обнаружили звёздный ветер.",
                },
                ensure_ascii=False,
            ), {}

    value.cycle.repair_factory = lambda trace: ArticleRepair(
        Editor(), trace=trace, prompt_dir=value.core.settings.root / "prompts"
    )
    value.claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.cycle.prepare(value.article, reserve_claim=value.claim)
    value.initial = copy.deepcopy(value.cycle._load()["records"][0])
    value.stamp[0] += 1
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.cycle.repair_rejected(value.initial["id"])
    value.record = copy.deepcopy(value.cycle._load()["records"][0])
    value.qwen = phase
    return value


def test_qwen_candidate_protocol_failure_keeps_its_completed_claim_and_never_repeats(
    candidate_failure,
):
    value = candidate_failure
    claim = value.record["automatic_repair_attempts"][-1]
    assert claim["state"] == "not_repairable" and claim["outcome_origin"] == "review_protocol_guard"
    proof = completed_review_proof(value.cycle, value.record)
    assert proof["kind"] == "candidate" and proof == claim["completed_review_protocol"]
    assert value.cycle.automatic_repair_status(value.record)["reason"] == "NOT_REPAIRABLE"
    before, files, calls = (
        database_snapshot(value.core),
        raw_files(value),
        list(value.reviewer_calls),
    )
    assert close(value)["state"] == "cancelled"
    record = value.cycle._load()["records"][0]
    assert record["automatic_repair_attempts"] == value.record["automatic_repair_attempts"]
    assert record["preparation_attempts"] == value.record["preparation_attempts"]
    assert record["preparation_attempts"][0] == value.initial["preparation_attempts"][0]
    assert record["protocol_guard"]["source_remains_used"] and value.qwen["qwen"] == 1
    assert raw_files(value) == files and value.reviewer_calls == calls
    after = database_snapshot(value.core)
    assert {table for table in before if before[table] != after[table]} == {
        "editorial_article_ledger",
        "editorial_preparation_claims",
        "audit_events",
    }
    with pytest.raises(AgentError):
        value.cycle.repair_rejected(record["id"])
    assert value.qwen["qwen"] == 1 and value.reviewer_calls == calls


@pytest.mark.parametrize(
    "damage",
    [
        "claim_unknown",
        "claim_lost",
        "claim_checking",
        "claim_failed",
        "repair_audit_missing",
        "repair_trace_changed",
        "proposal_changed",
        "completion_missing",
    ],
)
def test_candidate_unknown_or_lost_provenance_never_releases_source(candidate_failure, damage):
    value = candidate_failure
    claim = value.record["automatic_repair_attempts"][-1]
    if damage.startswith("claim_"):

        def mutate(record):
            checked = record["automatic_repair_attempts"][-1]
            if damage == "claim_lost":
                checked["input_hash"] = "0" * 64
            else:
                checked["state"] = damage.removeprefix("claim_")

        change_record(value, mutate)
    elif damage == "repair_audit_missing":
        with value.core.db.session.begin() as session:
            session.delete(session.get(Audit, claim["id"]))
    elif damage in {"repair_trace_changed", "proposal_changed"}:
        path = (
            value.core.settings.root
            / claim["repair_trace" if damage == "repair_trace_changed" else "proposal_path"]
        )
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "completion_missing":
        completion = value.record["preparation_attempts"][-1]["completed_review_protocol"][
            "completion"
        ]
        (value.core.settings.root / completion["directory"] / "validated.json").unlink()
    before, files, calls = (
        database_snapshot(value.core),
        raw_files(value),
        list(value.reviewer_calls),
    )
    with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
        close(value)
    assert database_snapshot(value.core) == before and raw_files(value) == files
    assert value.qwen["qwen"] == 1 and value.reviewer_calls == calls and not value.writes


def test_new_protocol_preserves_local_translation_preflight_zero_request_release(
    reserve_controller, monkeypatch
):
    from max_postiz_agent.translation import (
        DedicatedTranslator,
        MADLAD_MODEL,
        TranslationPreflightError,
    )

    value = configure(reserve_controller, monkeypatch)
    config = {
        "enabled": True,
        "model": MADLAD_MODEL,
        "base_url": "http://local.invalid",
        "segment_chars": 600,
    }
    (value.core.settings.root / "config/translation.json").write_text(json.dumps(config))
    calls = []

    def forbidden(request):
        calls.append(request)
        pytest.fail("Unsplittable source must not invoke any model or LanguageTool")

    def quality(record_id=None, review_only=False):
        reviewer = Model({}, "local/ministral")
        reviewer.generate_text = forbidden
        return ArticleQuality(
            DedicatedTranslator(config, transport=httpx.MockTransport(forbidden)),
            reviewer,
            prompt_dir=value.core.settings.root / "prompts",
            trace=value.cycle._trace_writer(record_id),
            review_rubric="hourly-practical-v2",
            review_protocol_contract=REVIEW_BOUNDS_V2_CONTRACT,
            transport=httpx.MockTransport(forbidden),
        )

    monkeypatch.setattr(value.cycle, "_quality", quality)
    value.claim = value.store.begin_preparation()
    article = {**value.article, "original": value.article["original"] + " " + "Z" * 601}
    with pytest.raises(TranslationPreflightError) as caught:
        value.cycle.prepare(article, reserve_claim=value.claim)
    value.record = copy.deepcopy(value.cycle._load()["records"][0])
    before_files = raw_files(value)
    assert (
        value.store.reject_translation_preflight(
            value.claim, value.record["id"], caught.value, cycle=value.cycle
        )["state"]
        == "rejected"
    )
    record = value.cycle._load()["records"][0]
    assert (
        record["state"] == "cancelled"
        and record["preparation_attempts"] == value.record["preparation_attempts"]
    )
    assert raw_files(value) == before_files and calls == [] and not value.writes
