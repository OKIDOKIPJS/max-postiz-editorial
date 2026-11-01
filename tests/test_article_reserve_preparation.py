"""Резерв использует прежнюю QA-цепочку, но не создаёт внешних черновиков."""
import copy
import json
import os
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, select, text

from max_postiz_agent.article_reserve import ArticleReserveStore
from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Database, uid
from max_postiz_agent.hub_models import Dispatch, RemoteDraft, Target, TargetApproval, Variant
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState

pytest_plugins = ["test_automatic_policy", "test_illustrated_article_flow"]


@pytest.fixture
def reserve(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    cycle, core, article, records, writes, stamp, policy = values
    cycle.reserve_mode = True
    DatabaseArticleLedger(cycle).migrate()
    state = ServerState(core)
    state.initialize()
    state.control("start", state.read()["revision"], "test")
    monkeypatch.setattr("max_postiz_agent.article_reserve.now", lambda: stamp[0])
    return SimpleNamespace(cycle=cycle, core=core, article=article, records=records, writes=writes,
                           stamp=stamp, policy=policy, review=review, store=ArticleReserveStore(core))


def prepared(value):
    claim = value.store.begin_preparation()
    result = value.cycle.prepare(value.article, reserve_claim=claim)
    value.store.finish_preparation(claim, "quality_ready")
    return result


def assignment(value, result):
    record = value.cycle._record(value.cycle._load(), result["id"])
    validated = value.cycle.validate_reserve(result["id"])
    return value.store.assign(record, value.stamp[0] + 180,
        text_hash=record["reserve_prepared_hash"], evidence_hash=validated["reserve_evidence_hash"])


def test_quality_ready_keeps_exact_evidence_locally_without_postiz_or_approval(reserve):
    value = reserve
    result = prepared(value)
    assert result["state"] == "quality_ready"
    assert result["reserve_prepared_hash"] == result["content_hash"]
    assert not value.records and not value.writes
    with value.core.db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert variant.metadata_json["machine_review_status"] == "passed"
        assert variant.metadata_json["reserve_binding_hash"] == result["reserve_binding_hash"]
        assert not session.scalar(select(RemoteDraft))
        assert not session.scalar(select(TargetApproval))
        assert not session.scalar(select(Dispatch))


def test_preparation_does_not_hold_sender_lock_and_can_cross_hour(reserve, monkeypatch):
    value = reserve
    original = value.cycle._quality

    def quality(*args, **kwargs):
        instance = original(*args, **kwargs)
        prepare = instance.prepare

        def slow(*a, **k):
            with profile_lock(value.cycle.directory / "cycle.lock"):
                result = prepare(*a, **k)
                value.stamp[0] += 3600
                return result

        instance.prepare = slow
        return instance

    monkeypatch.setattr(value.cycle, "_quality", quality)
    result = prepared(value)
    assert result["state"] == "quality_ready"
    assert result["slot"] < int(value.stamp[0] // 3600)
    assert not value.writes


def test_distinct_sources_can_fill_same_hour_but_history_still_blocks_duplicates(reserve, monkeypatch):
    value = reserve
    first = prepared(value)
    value.article = {**value.article, "url": "https://science.example.org/article/second/",
                     "original": value.article["original"] + "The study opens new possibilities."}
    original = value.cycle._quality

    def quality(*a, **k):
        instance = original(*a, **k)
        instance.translator.value = copy.deepcopy(instance.translator.value)
        instance.translator.value["body"] += " Исследование открывает новые возможности."
        return instance

    monkeypatch.setattr(value.cycle, "_quality", quality)
    second = prepared(value)
    assert first["slot"] == second["slot"] and first["id"] != second["id"]
    assert len(value.cycle._load()["records"]) == 2
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare(value.article, reserve_claim=claim)
    assert not value.writes


def test_stage_requires_exact_assignment_and_is_never_replayed(reserve):
    value = reserve
    result = prepared(value)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_LOST"):
        value.cycle.stage_reserve(result["id"])
    slot = assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    assert staged["state"] == "automatic_ready"
    assert staged["publication_slot_id"] == slot["id"]
    assert len(value.writes) == 1 and value.writes[0][3] == "draft"
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_NOT_READY"):
        value.cycle.stage_reserve(result["id"])
    with value.core.db.session() as session:
        assert not session.scalar(select(TargetApproval)) and not session.scalar(select(Dispatch))


@pytest.mark.parametrize("mutation", ["trace", "input", "text", "claim"])
def test_stale_or_changed_evidence_blocks_before_any_external_mutation(reserve, mutation):
    value = reserve
    result = prepared(value)
    if mutation == "trace":
        path = value.core.settings.root / result["quality_trace"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "input":
        path = value.core.settings.root / result["input_file"]
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "text":
        with value.core.db.session.begin() as session:
            variant = session.get(Variant, result["variant_id"])
            variant.body += " Изменение."
    else:
        ledger = value.cycle._load()
        ledger["records"][0]["reserve_claim"]["token"] = "replaced"
        value.cycle._save(ledger)
    with pytest.raises(AgentError):
        value.cycle.validate_reserve(result["id"])
    assert not value.writes


def test_unknown_stage_has_one_remote_attempt_and_retains_reconciliation_state(reserve, monkeypatch):
    value = reserve
    result = prepared(value)
    assignment(value, result)
    calls = []

    def unknown(*args, **kwargs):
        calls.append(args)
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(value.core.postiz, "create", unknown)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.cycle.stage_reserve(result["id"])
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_NOT_READY"):
        value.cycle.stage_reserve(result["id"])
    saved = value.cycle._record(value.cycle._load(), result["id"])
    assert saved["state"] == "needs_attention" and saved["error"] == "POSTIZ_UNKNOWN"
    assert len(calls) == 1


def test_exact_assigned_date_is_required_before_remote_schedule(reserve):
    value = reserve
    result = prepared(value)
    slot = assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_CHANGED"):
        value.cycle.publish(staged["id"], staged["content_hash"], due_at=slot["due_at"] + 1)
    assert len(value.writes) == 1
    with value.core.db.session() as session:
        assert not session.scalar(select(TargetApproval)) and not session.scalar(select(Dispatch))
    saved = value.cycle._record(value.cycle._load(), result["id"])
    assert fingerprint(value.cycle._snapshot(saved)) == staged["content_hash"]


def test_removing_both_reserve_markers_cannot_downgrade_durable_candidate_to_legacy(reserve):
    from max_postiz_agent.automatic_policy import eligible
    value = reserve
    result = prepared(value)
    assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    ledger = value.cycle._load()
    record = value.cycle._record(ledger, result["id"])
    record.pop("reserve_contract")
    value.cycle._save(ledger)
    with value.core.db.session.begin() as session:
        variant = session.get(Variant, staged["variant_id"])
        metadata = copy.deepcopy(variant.metadata_json)
        metadata.pop("reserve_contract")
        variant.metadata_json = metadata
    with value.core.db.session() as session:
        with pytest.raises(AgentError, match="ARTICLE_RESERVE_BINDING_INVALID"):
            eligible(value.core, session, session.get(Variant, staged["variant_id"]))


def test_only_intact_unassigned_expired_source_can_leave_ready_reserve(reserve):
    value = reserve
    value.article["published_at"] = value.stamp[0] - value.policy["max_source_age_hours"] * 3600 + 60
    result = prepared(value)
    trace = value.core.settings.root / result["quality_trace"]
    original_trace = trace.read_bytes()
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_INVALIDATION_NOT_SAFE"):
        value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_INVALIDATION_NOT_SAFE"):
        value.cycle.invalidate_reserve(result["id"], "ARTICLE_AUTOMATIC_RECHECK_REQUIRED")
    value.stamp[0] += 120
    with pytest.raises(AgentError, match="STALE_SOURCE"):
        value.cycle.validate_reserve(result["id"])
    cancelled = value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE")
    assert cancelled["state"] == "cancelled" and cancelled["error"] == "STALE_SOURCE"
    assert cancelled["prepared_hash"] == result["prepared_hash"]
    assert trace.read_bytes() == original_trace and not value.writes
    assert cancelled["preparation_attempts"] == result["preparation_attempts"]
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare({**value.article, "published_at": value.stamp[0] - 1}, reserve_claim=claim)


@pytest.mark.parametrize("blocker", ["slot", "trace"])
def test_expiry_does_not_hide_changed_proof_or_an_assigned_slot(reserve, blocker):
    value = reserve
    value.article["published_at"] = value.stamp[0] - value.policy["max_source_age_hours"] * 3600 + 60
    result = prepared(value)
    if blocker == "slot":
        assignment(value, result)
    else:
        trace = value.core.settings.root / result["quality_trace"]
        trace.write_bytes(trace.read_bytes() + b"\n")
    value.stamp[0] += 120
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_INVALIDATION_NOT_SAFE"):
        value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE")
    saved = value.cycle._record(value.cycle._load(), result["id"])
    assert saved["state"] == "quality_ready" and not value.writes


def test_source_expiring_before_exact_due_is_excluded_before_assignment_or_draft(reserve):
    value = reserve
    value.article["published_at"] = value.stamp[0] - value.policy["max_source_age_hours"] * 3600 + 60
    result = prepared(value)
    due = value.stamp[0] + 180
    assert value.cycle.validate_reserve(result["id"])["state"] == "quality_ready"
    with pytest.raises(AgentError, match="STALE_SOURCE"):
        value.cycle.validate_reserve(result["id"], due_at=due)
    excluded = value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE", due_at=due)
    assert excluded["state"] == "cancelled" and excluded["error"] == "STALE_SOURCE"
    assert excluded["preparation_actions"][-1]["due_at"] == due
    assert not value.writes and value.store.status()["active"] == []


def rejected(value):
    value.review.update(approved=False, issues=["The translation omits the discovery of the stellar wind."])
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.cycle.prepare(value.article, reserve_claim=claim)
    record = copy.deepcopy(value.cycle._load()["records"][-1])
    trace = (value.core.settings.root / record["quality_trace"]).read_bytes()
    return claim, record, trace


def editor(value, bodies, hook=None):
    from test_automatic_article_repair import patched
    calls = []

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            context = json.loads(prompt.split("UNTRUSTED_JSON_DATA:\n", 1)[1])
            calls.append(context)
            if hook:
                hook()
            return json.dumps(patched(context, bodies[len(calls) - 1]), ensure_ascii=False), {}

    value.cycle.repair_factory = lambda trace: ArticleRepair(Editor(),
        prompt_dir=value.core.settings.root / "prompts", trace=trace)
    return calls


def test_changed_repair_crosses_hour_with_new_independent_qa_and_keeps_original_refusal(reserve):
    from test_automatic_article_repair import CORRECT_BODY
    value = reserve
    claim, original, trace = rejected(value)
    value.stamp[0] += 3600
    calls = editor(value, [CORRECT_BODY])
    value.review.update(approved=True, issues=[])
    result = value.cycle.repair_rejected(original["id"])
    assert result["state"] == "quality_ready" and len(calls) == 1
    saved = value.cycle._record(value.cycle._load(), original["id"])
    assert saved["preparation_attempts"][0] == original["preparation_attempts"][0]
    assert saved["automatic_repair_attempts"][0]["state"] == "passed"
    assert (value.core.settings.root / saved["quality_trace"]).read_bytes().startswith(trace)
    value.store.finish_preparation(claim, "quality_ready")
    assert value.cycle.validate_reserve(original["id"])["state"] == "quality_ready"
    assert not value.writes


def test_two_failed_repairs_preserve_claims_and_can_close_known_candidate_without_publication(reserve):
    from test_automatic_article_repair import CORRECT_BODY, SECOND_BODY
    value = reserve
    claim, original, trace = rejected(value)
    calls = editor(value, [CORRECT_BODY, SECOND_BODY])
    for _ in range(2):
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            value.cycle.repair_rejected(original["id"])
    saved = value.cycle._record(value.cycle._load(), original["id"])
    assert value.cycle.automatic_repair_status(saved)["reason"] == "EXHAUSTED"
    assert [attempt["state"] for attempt in saved["automatic_repair_attempts"]] == ["rejected", "rejected"]
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_EXHAUSTED"):
        value.cycle.repair_rejected(original["id"])
    assert len(calls) == 2
    previous_claims = copy.deepcopy(saved["automatic_repair_attempts"])
    value.cycle.cancel_preparation(original["id"], "Known quality rejection exhausted two durable repair attempts")
    value.store.finish_preparation(claim, "rejected", "ARTICLE_QUALITY_REJECTED")
    final = value.cycle._record(value.cycle._load(), original["id"])
    assert final["state"] == "cancelled" and final["error"] == "ARTICLE_QUALITY_REJECTED"
    assert final["automatic_repair_attempts"] == previous_claims
    assert (value.core.settings.root / final["quality_trace"]).read_bytes().startswith(trace)
    assert not value.writes and value.store.active_preparation() is None


def test_sender_starting_during_editor_does_not_interrupt_local_quality_result(reserve):
    from test_article_cycle import seed_legacy_dispatch
    from test_automatic_article_repair import CORRECT_BODY
    value = reserve
    first = prepared(value)
    value.article = {**value.article, "url": "https://science.example.org/article/second/",
                     "original": value.article["original"] + "A second independent observation followed."}
    claim, original, _ = rejected(value)

    def sending():
        dispatch_id = seed_legacy_dispatch(value.core, first)
        with value.core.db.session.begin() as session:
            dispatch = session.get(Dispatch, dispatch_id)
            dispatch.state, dispatch.owner, dispatch.lease_until = "sending", "simulated-sender", value.stamp[0] + 600
            session.get(Target, "max").paused = False

    calls = editor(value, [CORRECT_BODY], hook=sending)
    value.review.update(approved=True, issues=[])
    result = value.cycle.repair_rejected(original["id"])
    assert result["state"] == "quality_ready" and len(calls) == 1
    value.store.finish_preparation(claim, "quality_ready")
    assert not value.writes
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_LOST"):
        value.cycle.stage_reserve(result["id"])
    new_claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        value.cycle.prepare({**value.article, "url": "https://science.example.org/article/third/"},
                            reserve_claim=new_claim)


@pytest.fixture
def reserve_pg(reserve):
    value = reserve
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    admin = create_engine(url)
    schema = "reserve_pipeline_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = url + ("&" if "?" in url else "?") + urlencode({"options": "-csearch_path=" + schema})
    original = value.core.db
    database = Database(value.core.settings, url=isolated)
    try:
        database.initialize()
        database.restore(original.export())
        value.core.db = database
        yield value
    finally:
        value.core.db = original
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_postgres_pipeline_preserves_evidence_through_single_draft_and_exact_approval(reserve_pg):
    value = reserve_pg
    result = prepared(value)
    slot = assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    pending = value.cycle.publish(result["id"], staged["content_hash"], due_at=slot["due_at"])
    value.store.update(slot["id"], "pending", token=slot["token"], dispatch_id=pending["dispatch_id"])
    assert pending["state"] == "pending" and len(value.writes) == 1
    assert value.records[pending["postiz_id"]]["state"] == "QUEUE"
    with value.core.db.session() as session:
        approval = session.scalar(select(TargetApproval))
        dispatch = session.scalar(select(Dispatch))
        assert approval.actor == "hourly_article_policy"
        assert dispatch.due_at == approval.scheduled_at == slot["due_at"]
        assert dispatch.snapshot_hash == approval.snapshot_hash
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1
    with pytest.raises(AgentError):
        value.cycle.publish(result["id"], pending["content_hash"], due_at=slot["due_at"])


def preflight_quality(value, monkeypatch):
    """Real dedicated generate_text; transport/model reviewers must stay unused."""
    import httpx
    from max_postiz_agent.article_quality import ArticleQuality
    from max_postiz_agent.translation import DedicatedTranslator, MADLAD_MODEL
    from test_automatic_policy import Model
    config = {"enabled": True, "model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8765", "segment_chars": 600}
    (value.core.settings.root / "config/translation.json").write_text(json.dumps(config), encoding="utf-8")
    calls = []

    def forbidden(request):
        calls.append(str(request.url))
        raise AssertionError("No translation/review/spelling HTTP may precede local input rejection")

    def quality(record_id=None, review_only=False):
        record = value.cycle._record(value.cycle._load(), record_id)
        reviewer = Model({}, "proxmox/qwen25-coder-7b")
        reviewer.generate_text = lambda *_: forbidden(SimpleNamespace(url="reviewer"))
        presentation = ({k: record[k] for k in ("presentation_contract", "image_credit")}
                        if record.get("presentation_contract") else {})
        return ArticleQuality(DedicatedTranslator(config, transport=httpx.MockTransport(forbidden)), reviewer,
            prompt_dir=value.core.settings.root / "prompts", trace=value.cycle._trace_writer(record_id),
            review_rubric="meaning-first-v1", transport=httpx.MockTransport(forbidden), **presentation)

    monkeypatch.setattr(value.cycle, "_quality", quality)
    return calls, quality


def captured_preflight(value, monkeypatch):
    from max_postiz_agent.translation import TranslationPreflightError
    calls, _ = preflight_quality(value, monkeypatch)
    article = {**value.article, "original": value.article["original"] + " " + "Z" * 601}
    claim = value.store.begin_preparation()
    with pytest.raises(TranslationPreflightError) as caught:
        value.cycle.prepare(article, reserve_claim=claim)
    record = value.cycle._load()["records"][-1]
    return claim, record, caught.value, calls


def assert_no_preflight_external(value):
    from max_postiz_agent.server_models import PublicationSlot
    with value.core.db.session() as session:
        assert all(session.query(model).count() == 0 for model in (Variant, Dispatch, TargetApproval, RemoteDraft, PublicationSlot))
    assert not getattr(value, "writes", [])


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_exact_native_translation_preflight_rejection_preserves_source_trace_and_releases_claim(request, backend, monkeypatch):
    from max_postiz_agent.db import Audit
    value = request.getfixturevalue("reserve_pg" if backend == "postgres" else "reserve")
    claim, original, error, calls = captured_preflight(value, monkeypatch)
    trace = value.core.settings.root / original["quality_trace"]
    before = trace.read_bytes()
    finished = value.store.reject_translation_preflight(claim, original["id"], error, cycle=value.cycle)
    final = value.cycle._record(value.cycle._load(), original["id"])
    assert final["state"] == "cancelled" and final["error"] == error.code and finished["state"] == "rejected"
    assert final["preparation_attempts"] == original["preparation_attempts"] and trace.read_bytes() == before
    assert final["input_hash"] == original["input_hash"] and final["original_hash"] == original["original_hash"]
    assert value.store.active_preparation() is None and calls == []
    from max_postiz_agent.server_nasa import _used_narratives
    raw = json.loads((value.core.settings.root / original["input_file"]).read_bytes())
    assert _used_narratives(value.core, value.cycle._load()) == [raw["original"]]
    with value.core.db.session() as session:
        audit = session.query(Audit).filter_by(action="reserve_translation_preflight_rejected").one()
        assert audit.details["preflight"] == error.preflight_evidence
        assert audit.details["dedicated_translation_http_requests"] == 0 and audit.details["source_remains_used"] is True
    assert_no_preflight_external(value)
    next_claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare(raw, reserve_claim=next_claim)


def test_illustrated_preflight_keeps_verified_photo_and_does_not_treat_model_started_marker_as_http(illustrated, monkeypatch):
    value = illustrated
    claim, original, error, calls = captured_preflight(value, monkeypatch)
    assert original["preparation_attempts"][0]["model_started_at"] is not None
    value.store.reject_translation_preflight(claim, original["id"], error, cycle=value.cycle)
    final = value.cycle._record(value.cycle._load(), original["id"])
    assert final["illustration"] == original["illustration"] and value.image_path.is_file()
    assert calls == [] and value.store.active_preparation() is None
    assert_no_preflight_external(value)


@pytest.mark.parametrize("damage", ["old_generic", "code_only", "alien_model", "unknown_requests", "proof_source",
    "proof_config", "failed_field", "config", "input", "trace_returned", "trace_error", "trace_time",
    "claim", "blocked_claim", "factory", "native_binding", "native_attempt", "repair", "external"])
def test_preflight_classification_never_waives_other_routes_unknowns_or_changed_proof(reserve, monkeypatch, damage):
    from max_postiz_agent.db import Audit
    from max_postiz_agent.server_models import EditorialPreparationClaim
    value = reserve
    claim, original, error, calls = captured_preflight(value, monkeypatch)
    path = value.core.settings.root / original["quality_trace"]
    if damage == "old_generic":
        error = AgentError("TRANSLATION_UNSPLITTABLE_INPUT")
    elif damage == "code_only":
        error = AgentError("TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT")
    elif damage == "alien_model":
        error.preflight_evidence["model"] = "hy-mt2-7b-q8"
    elif damage == "unknown_requests":
        error.preflight_evidence["no_model_requests"] = False
    elif damage == "proof_source":
        error.preflight_evidence["source_hash"] = "0" * 64
    elif damage == "proof_config":
        error.preflight_evidence["config_hash"] = "0" * 64
    elif damage == "failed_field":
        error.preflight_evidence["failed_field"] = "title"
    elif damage == "config":
        (value.core.settings.root / "config/translation.json").write_text("{}")
    elif damage == "input":
        (value.core.settings.root / original["input_file"]).write_bytes(b"{}")
    elif damage.startswith("trace_"):
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if damage == "trace_returned":
            events.insert(1, {**events[0], "phase": "translation_returned", "raw": "unexpected"})
        elif damage == "trace_error":
            events[1]["error"] = "TRANSLATION_SERVICE_UNAVAILABLE"
        else:
            events[0]["at"] = events[1]["at"] + 1
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    elif damage == "claim":
        claim = {**claim, "token": "changed"}
    elif damage == "blocked_claim":
        with value.core.db.session.begin() as session:
            session.get(EditorialPreparationClaim, claim["id"]).state = "blocked_unknown"
    elif damage == "factory":
        value.cycle.quality_factory = lambda: None
    elif damage in {"native_binding", "native_attempt"}:
        with value.core.db.session.begin() as session:
            action = "article_reserve_preparation_binding" if damage == "native_binding" else "hourly_article_prepare_attempt"
            session.query(Audit).filter_by(entity=original["id"], action=action).one().details = {}
    else:
        ledger = value.cycle._load()
        record = value.cycle._record(ledger, original["id"])
        if damage == "repair":
            record["automatic_repair_attempts"] = [{"state": "failed"}]
        else:
            record["postiz_id"] = "unexpected"
        value.cycle._save(ledger)
    before = copy.deepcopy(value.cycle._load())
    with pytest.raises(AgentError):
        value.store.reject_translation_preflight(claim, original["id"], error, cycle=value.cycle)
    assert value.cycle._load() == before and value.store.active_preparation() is not None and calls == []


def test_preflight_rejection_continues_same_reserve_batch_with_distinct_source(reserve_controller, monkeypatch):
    value = reserve_controller
    bad = {**value.article, "original": value.article["original"] + " " + "Z" * 601}
    good = {**value.article, "url": "https://science.example.org/article/after-local-preflight/"}
    calls, local_quality = preflight_quality(value, monkeypatch)

    def quality(record_id=None, review_only=False):
        record = value.cycle._record(value.cycle._load(), record_id)
        return local_quality(record_id) if record["url"] == bad["url"] else value.quality(record_id)

    monkeypatch.setattr(value.cycle, "_quality", quality)
    value.controller.selector = lambda *_args, **kw: next((copy.deepcopy(a) for a in (bad, good)
        if not any(r["url"] == a["url"] for r in kw["ledger"]["records"])), None)
    assert value.controller.prepare_once()["phase"] == "no_source"
    records = value.cycle._load()["records"]
    assert [r["state"] for r in records] == ["cancelled", "quality_ready"] and value.store.active_preparation() is None
    assert records[0]["error"] == "TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT" and calls == []
    assert len(value.model_calls) == 1 and not value.posts and not value.writes


def test_legacy_generic_unsplittable_error_keeps_unknown_claim_and_never_tries_another_source(reserve_controller, monkeypatch):
    from max_postiz_agent.translation import DedicatedTranslator
    value = reserve_controller
    calls, _ = preflight_quality(value, monkeypatch)
    attempts = []

    def old_error(*_args):
        attempts.append("old generic output has no preflight proof")
        raise AgentError("TRANSLATION_UNSPLITTABLE_INPUT")

    monkeypatch.setattr(DedicatedTranslator, "generate_text", old_error)
    with pytest.raises(AgentError, match="^TRANSLATION_UNSPLITTABLE_INPUT$"):
        value.controller.prepare_once()
    assert value.controller.prepare_once()["phase"] == "blocked"
    record = value.cycle._load()["records"][0]
    assert record["state"] == "needs_attention" and record["error"] == "TRANSLATION_UNSPLITTABLE_INPUT"
    assert value.store.active_preparation()["state"] == "blocked_unknown" and len(attempts) == 1 and calls == []
    assert not value.posts and not value.writes


def test_preflight_with_changed_illustration_evidence_cannot_release_claim(illustrated, monkeypatch):
    value = illustrated
    claim, original, error, calls = captured_preflight(value, monkeypatch)
    ledger = value.cycle._load()
    record = value.cycle._record(ledger, original["id"])
    record["illustration_proof_hash"] = "0" * 64
    value.cycle._save(ledger)
    with pytest.raises((AgentError, AssertionError)):
        value.store.reject_translation_preflight(claim, original["id"], error, cycle=value.cycle)
    assert value.store.active_preparation() is not None and value.cycle._load()["records"][0]["state"] == "needs_attention"
    assert calls == [] and not value.posts and not value.writes


MASS_EN = ("A small galaxy contains many bright stars and active star forming regions. "
    "Despite being only 10–20% as massive as the Milky Way galaxy, the small galaxy forms many new stars in a nearby region.")
MASS_RU = ("Небольшая галактика содержит много ярких звёзд и активных областей звездообразования. "
    "Несмотря на то, что оно только на 10-20% массивнее Млечного Пути, эта небольшая галактика образует много новых звёзд в соседней области.")


def mass_finding(english=MASS_EN, russian=MASS_RU):
    from max_postiz_agent.article_reserve import mass_relation_finding
    return mass_relation_finding({"title": "A galaxy comparison", "body": english, "url": "https://science.example.org/article/"},
        {"title": "Сравнение галактик", "body": russian})


def test_unique_small_fraction_to_percent_more_finding_binds_exact_quotes_codepoints_and_full_hashes():
    result = mass_finding()
    assert result["lower_percent"] == "10" and result["upper_percent"] == "20"
    a, b = result["source_codepoint_span"], result["candidate_codepoint_span"]
    assert MASS_EN[a[0]:a[1]] == result["source_quote"]
    assert MASS_RU[b[0]:b[1]] == result["candidate_quote"]
    assert result["candidate_hash"] == fingerprint({"title": "Сравнение галактик", "body": MASS_RU})


@pytest.mark.parametrize("english,russian", [
    (MASS_EN, MASS_RU.replace("только на 10-20% массивнее", "имеет только 10-20% массы")),
    (MASS_EN, MASS_RU.replace("10-20%", "11-20%")),
    (MASS_EN.replace("10–20%", "100–120%"), MASS_RU.replace("10-20%", "100-120%")),
    (MASS_EN.replace("10–20%", "0–20%"), MASS_RU.replace("10-20%", "0-20%")),
    (MASS_EN.replace("10–20%", "20–10%"), MASS_RU.replace("10-20%", "20-10%")),
    (MASS_EN.replace("Milky Way", "Andromeda"), MASS_RU),
    (MASS_EN, MASS_RU.replace("Млечного Пути", "галактики Андромеды")),
    (MASS_EN.replace("Despite", "It is false that despite"), MASS_RU),
    (MASS_EN, MASS_RU.replace("Несмотря", "Неверно, что несмотря")),
    (MASS_EN, MASS_RU.replace("массивнее", "не массивнее")),
    ('"' + MASS_EN[MASS_EN.index("Despite"):] + '"', MASS_RU),
    (MASS_EN, '«' + MASS_RU[MASS_RU.index("Несмотря"):] + '»'),
    (MASS_EN + " " + MASS_EN, MASS_RU),
    (MASS_EN, MASS_RU + " " + MASS_RU),
    (MASS_EN.replace("nearby region", "nearby region, but the statement is uncertain"), MASS_RU),
    (MASS_EN, MASS_RU.replace("соседней области", "соседней области, но это лишь предположение")),
    (MASS_EN.replace("galaxy,", "galaxy cluster,"), MASS_RU),
    (MASS_EN.replace("galaxy,", "galaxy's central bulge,"), MASS_RU),
    (MASS_EN + " Another object is as massive as a star.", MASS_RU),
])
def test_ambiguous_changed_correct_or_other_mass_relations_produce_no_disqualification(english, russian):
    assert mass_finding(english, russian) is None


def accepted_mass_candidate(value, monkeypatch, *, close_claim):
    import httpx
    from max_postiz_agent.article_quality import ArticleQuality
    from test_automatic_policy import Model
    article = {**value.article, "url": value.article["url"].rstrip("/") + "/mass-comparison/",
               "title": "A galaxy comparison", "original": MASS_EN}
    calls = []

    def quality(record_id=None, review_only=False):
        calls.append("initial fixture QA only")
        return ArticleQuality(Model({"title": "Сравнение галактик", "body": MASS_RU}, "hy-mt2-7b-q8"),
            Model({"approved": True, "topic": "science", "issues": [], "warnings": []}, "ministral"),
            prompt_dir=value.core.settings.root / "prompts", trace=value.cycle._trace_writer(record_id),
            review_rubric="meaning-first-v1", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))

    monkeypatch.setattr(value.cycle, "_quality", quality)
    claim = value.store.begin_preparation()
    result = value.cycle.prepare(article, reserve_claim=claim)
    if close_claim:
        value.store.finish_preparation(claim, "quality_ready")
    record = value.cycle._record(value.cycle._load(), result["id"])
    return claim, record, calls


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("active", [False, True])
def test_semantic_eligibility_cancel_preserves_true_machine_verdict_raw_variant_and_closed_history(request, backend, active, monkeypatch):
    from max_postiz_agent.db import Audit
    from max_postiz_agent.server_models import EditorialPreparationClaim
    value = request.getfixturevalue("reserve_pg" if backend == "postgres" else "reserve")
    claim, record, calls = accepted_mass_candidate(value, monkeypatch, close_claim=not active)
    trace = value.core.settings.root / record["quality_trace"]
    raw = trace.read_bytes()
    with value.core.db.session() as s:
        before = copy.deepcopy(s.get(Variant, record["variant_id"]).metadata_json)
        body = s.get(Variant, record["variant_id"]).body
        closed = {k: getattr(s.get(EditorialPreparationClaim, claim["id"]), k) for k in ("state", "active_key", "error", "updated_at")}
    assert value.store.disqualify_semantic_ready(record["id"], cycle=value.cycle, claim=claim if active else None) is True
    final = value.cycle._record(value.cycle._load(), record["id"])
    assert final["state"] == "cancelled" and final.get("error") == record.get("error")
    assert trace.read_bytes() == raw and final["preparation_attempts"] == record["preparation_attempts"]
    assert len(calls) == 1 and not value.writes
    with value.core.db.session() as s:
        v = s.get(Variant, record["variant_id"])
        p = s.get(EditorialPreparationClaim, claim["id"])
        assert v.body == body and v.metadata_json == before and before["machine_review_evidence"]["review"]["approved"] is True
        if active:
            assert p.state == "rejected" and p.active_key is None
        else:
            assert {k: getattr(p, k) for k in closed} == closed
        a = s.query(Audit).filter_by(entity=record["id"], action="reserve_semantic_publication_ineligible").one()
        assert a.details["machine_verdict_preserved"] is True and a.details["model_calls"] == 0
        assert s.query(Dispatch).count() == s.query(TargetApproval).count() == s.query(RemoteDraft).count() == 0


@pytest.mark.parametrize("damage", ["trace", "input", "claim", "unknown", "repair", "postiz", "snapshot"])
def test_semantic_disqualification_cannot_waive_unknown_or_stale_binding(reserve, monkeypatch, damage):
    from max_postiz_agent.server_models import EditorialPreparationClaim
    value = reserve
    claim, record, calls = accepted_mass_candidate(value, monkeypatch, close_claim=True)
    if damage in {"trace", "input"}:
        name = record["quality_trace"] if damage == "trace" else record["input_file"]
        path = value.core.settings.root / name
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "claim":
        with value.core.db.session.begin() as s:
            s.get(EditorialPreparationClaim, claim["id"]).state = "blocked_unknown"
    elif damage == "snapshot":
        with value.core.db.session.begin() as s:
            v = s.get(Variant, record["variant_id"])
            v.metadata_json = {**v.metadata_json, "changed": True}
    else:
        ledger = value.cycle._load()
        r = value.cycle._record(ledger, record["id"])
        if damage == "unknown":
            r["model_result_unknown"] = True
        elif damage == "repair":
            r["automatic_repair_attempts"] = [{"state": "correcting"}]
        else:
            r["postiz_id"] = "unexpected"
        value.cycle._save(ledger)
    before = copy.deepcopy(value.cycle._load())
    with pytest.raises(AgentError):
        value.store.disqualify_semantic_ready(record["id"], cycle=value.cycle)
    assert value.cycle._load() == before and len(calls) == 1 and not value.writes


def test_fresh_mass_inversion_is_rejected_before_claim_can_become_available_ready(reserve, monkeypatch):
    from max_postiz_agent.hourly_reserve import ReserveServerCycle
    from max_postiz_agent.db import Audit
    value = reserve
    claim, record, calls = accepted_mass_candidate(value, monkeypatch, close_claim=False)
    controller = ReserveServerCycle(value.core, state=ServerState(value.core), cycle=value.cycle, reserve_store=value.store)
    assert controller._finish_candidate(claim, record) == "semantic_rejected"
    assert value.store.active_preparation() is None and len(calls) == 1 and not value.writes
    with value.core.db.session() as s:
        assert not s.query(Audit).filter_by(entity=claim["id"], action="reserve_preparation_finished").first()


def test_ready_cleanup_precedes_capacity_and_preserves_another_pending_dispatch_and_good_stock(reserve_controller, monkeypatch):
    from max_postiz_agent.server_models import EditorialControl, PublicationSlot
    value = reserve_controller
    value.controller.prepare_once()
    value.controller.run_once()
    extra = {**value.article, "url": value.article["url"].rstrip("/") + "/extra-ready/",
             "original": value.article["original"] + " Additional observations are retained in the complete report."}
    extra_claim = value.store.begin_preparation()
    good = value.cycle.prepare(extra, reserve_claim=extra_claim)
    value.store.finish_preparation(extra_claim, "quality_ready")
    claim, bad, calls = accepted_mass_candidate(value, monkeypatch, close_claim=True)
    with value.core.db.session.begin() as s:
        control = s.get(EditorialControl, "main")
        control.settings = {**control.settings, "reserve": {**control.settings["reserve"], "target": 2, "low": 1}}
        control.runtime = {**control.runtime, "preparation": {"next_run_at": 0, "refilling": False}}
    with value.core.db.session() as s:
        pending = s.query(Dispatch).filter_by(state="pending").one()
        pending_before = {k: copy.deepcopy(getattr(pending, k)) for k in ("id", "state", "snapshot", "due_at", "attempts")}
        slot = s.query(PublicationSlot).filter_by(dispatch_id=pending.id).one()
        slot_before = {k: copy.deepcopy(getattr(slot, k)) for k in ("id", "record_id", "due_at", "state", "token")}
    writes = copy.deepcopy(value.writes)
    value.controller._refilling = False
    value.controller.selector = lambda *_args, **_kwargs: None
    assert value.controller.prepare_once()["phase"] == "no_source"
    records = value.cycle._load()["records"]
    assert value.cycle._record({"records": records}, bad["id"])["state"] == "cancelled"
    assert value.cycle._record({"records": records}, good["id"])["state"] == "quality_ready"
    assert value.writes == writes and len(calls) == 1
    with value.core.db.session() as s:
        d = s.get(Dispatch, pending_before["id"])
        slot = s.get(PublicationSlot, slot_before["id"])
        assert {k: getattr(d, k) for k in pending_before} == pending_before
        assert {k: getattr(slot, k) for k in slot_before} == slot_before


def test_sender_skips_owned_ready_candidate_before_closed_validation_then_producer_disqualifies(reserve_controller, monkeypatch):
    from max_postiz_agent.server_models import PublicationSlot
    value = reserve_controller
    claim, record, calls = accepted_mass_candidate(value, monkeypatch, close_claim=False)
    owned = copy.deepcopy(value.store.active_preparation())
    trace = value.core.settings.root / record["quality_trace"]
    raw = trace.read_bytes()
    assert value.controller.run_once()["phase"] == "reserve_empty"
    assert value.store.active_preparation() == owned and trace.read_bytes() == raw
    with value.core.db.session() as s:
        assert s.query(PublicationSlot).count() == s.query(Dispatch).count() == s.query(TargetApproval).count() == 0
    assert value.controller._finish_candidate(claim, record) == "semantic_rejected"
    assert value.store.active_preparation() is None and len(calls) == 1 and not value.writes
    assert value.cycle._record(value.cycle._load(), record["id"])["state"] == "cancelled"


def test_removing_known_bad_stock_refills_four_above_low_three_to_target_six(reserve_controller, monkeypatch):
    from max_postiz_agent.server_models import EditorialControl
    value = reserve_controller

    def article(word):
        return {**value.article, "url": value.article["url"].rstrip("/") + "/" + word + "/",
                "original": value.article["original"] + " The " + word + " observation was recorded."}

    for word in ("alpha", "beta", "gamma", "delta"):
        claim = value.store.begin_preparation()
        value.cycle.prepare(article(word), reserve_claim=claim)
        value.store.finish_preparation(claim, "quality_ready")
    _, bad, _ = accepted_mass_candidate(value, monkeypatch, close_claim=True)
    monkeypatch.setattr(value.cycle, "_quality", value.quality)
    with value.core.db.session.begin() as s:
        c = s.get(EditorialControl, "main")
        c.runtime = {**c.runtime, "preparation": {"next_run_at": 0, "refilling": False}}
    fresh = [article(word) for word in ("epsilon", "zeta")]
    value.controller.selector = lambda *_args, **kw: next((copy.deepcopy(a) for a in fresh
        if not any(r["url"] == a["url"] for r in kw["ledger"]["records"])), None)
    value.controller._refilling = False  # New process starts without a refill intent.
    before_calls = len(value.model_calls)
    result = value.controller.prepare_once()
    records = value.cycle._load()["records"]
    assert result["phase"] == "reserve_refilled" and result["completed"] == ["quality_ready", "quality_ready"]
    assert sum(r["state"] == "quality_ready" for r in records) == 6
    assert value.cycle._record({"records": records}, bad["id"])["state"] == "cancelled"
    assert len(value.model_calls) == before_calls + 2 and not value.posts and not value.writes
    assert value.state.read()["settings"]["reserve"]["low"] == 3
