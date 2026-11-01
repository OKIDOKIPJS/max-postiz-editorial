"""Explicit future bounds retain every finding; historical review parsing is unchanged."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.article_quality import (
    ArticleQuality,
    HOURLY_PRACTICAL_V2_REVIEW_RUBRIC,
    REVIEW_BOUNDS_V2_CONTRACT,
    REVIEW_PROTOCOL_CONTRACT,
    article_review_context,
    completed_review_rejection,
    decode_rubric_review,
    normalize_rubric_review,
    review_parser_contract,
    review_prompt_name,
    review_protocol_marker,
)
from max_postiz_agent.automatic_policy import _validate, machine_metadata
from max_postiz_agent.article_repair_cycle import AutomaticArticleRepair
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Variant
from test_article_quality import Model, ORIGINAL, TITLE, TRANSLATION, URL

pytest_plugins = ["test_hourly_reserve_controller"]

RUBRIC = HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
CONTRACT = REVIEW_BOUNDS_V2_CONTRACT
SOURCE = {"title": TITLE, "body": ORIGINAL, "url": URL}
PASS = {"approved": True, "topic": "science", "issues": [], "warnings": []}


class ImmutableModel(Model):
    immutable_candidates = True


def normalized(value, contract=CONTRACT):
    return normalize_rubric_review(value, RUBRIC, review_protocol_contract=contract)


def decoded(raw, contract=CONTRACT):
    return decode_rubric_review(raw, RUBRIC, review_protocol_contract=contract)


def test_new_topic_enum_is_schema_bound_without_reinterpreting_historical_parser():
    value = {**PASS, "topic": "outside_contract"}
    assert normalized(value, contract=None) == value
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
        normalized(value)


def quality(project, review, *, contract=CONTRACT, events=None):
    translator, reviewer, checks = ImmutableModel(TRANSLATION), Model(review), []

    def spelling(request):
        checks.append(request.content)
        return httpx.Response(200, json={"matches": []})

    check = ArticleQuality(translator, reviewer, prompt_dir=project / "prompts", review_rubric=RUBRIC,
        review_protocol_contract=contract, trace=None if events is None else events.append,
        transport=httpx.MockTransport(spelling))
    return check, translator, reviewer, checks


def candidate(check):
    return check.check_candidate(**TRANSLATION, original_title=TITLE, original=ORIGINAL, url=URL)


def test_none_and_explicit_legacy_keep_original_limits_contract_fingerprint_and_trace(project):
    raw = json.dumps(PASS)
    limits = {"contract": "meaning-first-advisory-v2", "max_issues": 3, "max_issue_chars": 1024,
              "max_warnings": 8, "max_warning_chars": 1024, "max_warnings_utf8_bytes": 8192}
    expected = {**limits, "contract_hash": fingerprint(limits),
                "raw_review_sha256": hashlib.sha256(raw.encode()).hexdigest()}
    assert review_parser_contract(raw) == review_parser_contract(raw, review_protocol_contract=REVIEW_PROTOCOL_CONTRACT) == expected
    assert review_prompt_name(RUBRIC) == review_prompt_name(RUBRIC, review_protocol_contract=REVIEW_PROTOCOL_CONTRACT)
    traces = []
    for contract in (None, REVIEW_PROTOCOL_CONTRACT):
        events = []
        check, _, _, _ = quality(project, raw, contract=contract, events=events)
        try:
            prepared = candidate(check)
        finally:
            check.close()
        assert "review_protocol_contract" not in prepared
        assert "review_protocol_contract" not in prepared["machine_review_evidence"]
        assert all("review_protocol_contract" not in event for event in events)
        traces.append(events)
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
            normalized({**PASS, "approved": False, "issues": ["Ошибка"] * 4}, contract)
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
            normalized({**PASS, "warnings": ["Совет"] * 9}, contract)
    assert traces[0] == traces[1]


def test_sixteen_issues_and_warnings_retain_all_characters_and_false_verdict():
    issues = ["  Существенная ошибка " + str(i) + ".\nНужна сверка.  " for i in range(16)]
    value = {**PASS, "approved": False, "issues": issues, "warnings": ["Совет " + str(i) for i in range(16)]}
    before = copy.deepcopy(value)
    result = decoded(json.dumps(value, ensure_ascii=False))
    assert result == before and result["approved"] is False
    result["issues"].clear()
    result["warnings"].clear()
    assert value == before


@pytest.mark.parametrize("key", ["issues", "warnings"])
def test_count_and_individual_item_limits_fail_without_truncation(key):
    base = {**PASS, "approved": False}
    valid = {**base, key: ["я" * 1024] * 16}
    assert normalized(valid) == valid
    for invalid in ({**base, key: ["x"] * 17}, {**base, key: ["x" * 1025]}, {**base, key: [" "]}):
        before = copy.deepcopy(invalid)
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
            normalized(invalid)
        assert invalid == before


def test_combined_findings_utf8_budget_is_independent_of_each_array_count():
    value = {**PASS, "approved": False, "issues": ["я" * 1024] * 8, "warnings": ["я" * 1024] * 8}
    assert sum(len(s.encode()) for s in value["issues"] + value["warnings"]) == 32768
    assert normalized(value) == value
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
        normalized({**value, "warnings": [*value["warnings"], "x"]})


def test_raw_utf8_limit_includes_whitespace_and_is_bound_to_the_complete_response():
    raw = json.dumps(PASS)
    limit = raw + " " * (65536 - len(raw.encode()))
    assert decoded(limit) == PASS
    parser = review_parser_contract(limit, review_protocol_contract=CONTRACT)
    assert parser["raw_review_sha256"] == hashlib.sha256(limit.encode()).hexdigest()
    assert parser["max_issues"] == parser["max_warnings"] == 16
    assert parser["max_findings_utf8_bytes"] == 32768 and parser["max_raw_utf8_bytes"] == 65536
    assert parser["contract"] == CONTRACT
    for invalid in (limit + " ", "я" * 32769, "\ud800"):
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
            decoded(invalid)


@pytest.mark.parametrize("value", [
    {**PASS, "approved": "false"}, {**PASS, "approved": 0},
    {**PASS, "issues": ["Главный факт изменён."]},
    {**PASS, "approved": False, "issues": ["\ud800"]},
    {**PASS, "warnings": ["\ud800"]},
    {**PASS, "approved": False, "issues": [{"issue": "Ошибка", "ignored": "нельзя потерять"}]},
    {**PASS, "extra": "unsupported"},
])
def test_invalid_schema_or_contradictory_positive_never_becomes_approval(value):
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
        normalized(value)


@pytest.mark.parametrize("raw", [
    '{"approved":false,"approved":true,"topic":"science","issues":[],"warnings":[]}',
    json.dumps(PASS) + json.dumps(PASS), "[]", "null", "not JSON",
])
def test_ambiguous_or_invalid_raw_never_replaces_a_negative_verdict(raw):
    with pytest.raises(AgentError):
        decoded(raw)


@pytest.mark.parametrize("contract", ["unknown", "", True, 1, {}, []])
def test_protocol_requires_an_explicit_known_identifier(contract):
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_CONTRACT_INVALID"):
        normalized(PASS, contract)


@pytest.mark.parametrize("rubric", ["legacy", "meaning-first-v1", "hourly-practical-v1"])
def test_new_bounds_cannot_silently_change_another_historical_rubric(rubric):
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_CONTRACT_INVALID"):
        normalize_rubric_review(PASS, rubric, review_protocol_contract=CONTRACT)


def test_future_prompt_changes_only_response_bounds_and_context_is_explicit(project):
    prompts = project / "prompts"
    old = (prompts / review_prompt_name(RUBRIC)).read_text(encoding="utf-8")
    new = (prompts / review_prompt_name(RUBRIC, review_protocol_contract=CONTRACT)).read_text(encoding="utf-8")
    assert old.split("Return ONE complete JSON object", 1)[0] == new.split("Return ONE complete JSON object", 1)[0]
    assert "At most 3 short Russian issues and 2 short" in old
    assert all(value in new for value in (CONTRACT, "16 Russian issues and 16 Russian warnings", "1024", "32768", "65536"))
    kwargs = (old, "English original", "Русский текст", RUBRIC, "a" * 64)
    legacy = article_review_context(*kwargs)
    future = article_review_context(*kwargs, review_protocol_contract=CONTRACT)
    assert future == {**legacy, "review_protocol_contract": CONTRACT}
    assert review_protocol_marker(None, RUBRIC) == {}


def test_durable_repair_prompt_set_selects_the_saved_protocol_without_changing_old_hashes(project):
    repair = AutomaticArticleRepair(SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(root=project))))
    legacy = repair._prompts("article-repair-v8", RUBRIC)
    explicit_legacy = repair._prompts("article-repair-v8", RUBRIC, review_protocol_contract=REVIEW_PROTOCOL_CONTRACT)
    future = repair._prompts("article-repair-v8", RUBRIC, review_protocol_contract=CONTRACT)
    old_name, new_name = review_prompt_name(RUBRIC), review_prompt_name(RUBRIC, review_protocol_contract=CONTRACT)
    assert legacy == explicit_legacy and old_name in legacy and new_name not in legacy
    assert new_name in future and old_name not in future
    assert {k: v for k, v in legacy.items() if k != old_name} == {k: v for k, v in future.items() if k != new_name}


@pytest.mark.parametrize("mode", ["prepare", "candidate"])
def test_four_substantive_issues_are_known_rejection_once_with_full_raw_and_all_findings(project, mode):
    review = {**PASS, "approved": False, "issues": ["Существенная ошибка " + str(i) for i in range(4)],
              "warnings": ["Стилистический совет " + str(i) for i in range(10)]}
    raw = json.dumps(review, ensure_ascii=False)
    events = []
    check, translator, reviewer, spelling = quality(project, raw, events=events)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            if mode == "prepare":
                check.prepare(TITLE, ORIGINAL, URL)
            else:
                candidate(check)
    finally:
        check.close()
    assert len(reviewer.prompts) == 1 and len(translator.prompts) == (1 if mode == "prepare" else 0)
    assert len(spelling) == 1
    assert next(e for e in events if e["phase"] == "review_returned")["raw"] == raw
    assert next(e for e in events if e["phase"] == "review_parsed")["review"] == review
    assert events[-1]["feedback"]["review_issues"] == review["issues"]
    assert not any(e["phase"] in {"accepted", "review_invalid"} for e in events)
    assert all(e["review_protocol_contract"] == CONTRACT for e in events)
    assert reviewer.prompts[0]["review_protocol_contract"] == CONTRACT


@pytest.mark.parametrize("contract", [None, CONTRACT], ids=["historical", "explicit_future"])
def test_recorded_four_finding_response_keeps_every_original_finding_and_negative_verdict(project, contract):
    raw = (Path(__file__).parent / "fixtures/reviewer_negative_four_findings.json").read_text(encoding="utf-8")
    original = json.loads(raw)
    assert original["approved"] is False and len(original["issues"]) == 4
    events = []
    check, translator, reviewer, spelling = quality(project, raw, contract=contract, events=events)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == len(reviewer.prompts) == 1
    assert next(e for e in events if e["phase"] == "review_returned")["raw"] == raw
    assert not any(e["phase"] == "accepted" for e in events)
    if contract == CONTRACT:
        assert next(e for e in events if e["phase"] == "review_parsed")["review"] == original
        assert len(spelling) == 1 and events[-1]["feedback"]["review_issues"] == original["issues"]
        with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
            completed_review_rejection(raw, RUBRIC, review_protocol_contract=CONTRACT)
    else:
        assert not spelling and any(e["phase"] == "review_invalid" for e in events)


@pytest.mark.parametrize("mode", ["prepare", "candidate"])
@pytest.mark.parametrize("raw", ["not JSON", json.dumps({**PASS, "approved": False, "issues": ["Ошибка"] * 17}),
                                 json.dumps(PASS) + " " * 65536], ids=["json", "count", "raw_bytes"])
def test_completed_invalid_response_is_saved_and_rejected_without_resampling_or_lt(project, mode, raw):
    events = []
    check, translator, reviewer, spelling = quality(project, raw, events=events)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            if mode == "prepare":
                check.prepare(TITLE, ORIGINAL, URL)
            else:
                candidate(check)
    finally:
        check.close()
    assert len(reviewer.prompts) == 1 and len(translator.prompts) == (1 if mode == "prepare" else 0)
    assert not spelling and not any(e["phase"] == "accepted" for e in events)
    assert next(e for e in events if e["phase"] == "review_returned")["raw"] == raw
    assert any(e["phase"] == "review_invalid" for e in events)
    assert events[-1]["phase"] == "rejected"


@pytest.mark.parametrize("raw", [
    "not JSON", "{", json.dumps({**PASS, "approved": "true"}),
    json.dumps({**PASS, "issues": ["Содержательная ошибка"]}),
    json.dumps({**PASS, "approved": False, "issues": ["Ошибка"] * 17}),
    '{"approved":false,"approved":true,"topic":"science","issues":[],"warnings":[]}',
    json.dumps(PASS) + " " * 65536,
], ids=["non_json", "incomplete_json", "boolean_type", "contradictory_positive", "count", "duplicate_key", "raw_bytes"])
def test_quarantine_classifier_preserves_invalid_raw_without_inventing_a_verdict(raw):
    proof = completed_review_rejection(raw, RUBRIC, review_protocol_contract=CONTRACT)
    assert proof["contract"] == "completed-review-protocol-v1" and proof["approval_permitted"] is False
    assert proof["parser_error"] in {"INVALID_EDITOR_RESULT", "ARTICLE_REVIEW_SCHEMA_INVALID"}
    assert proof["raw_review_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert proof["parser_contract"] == review_parser_contract(raw, review_protocol_contract=CONTRACT)
    assert proof["quarantine_max_raw_utf8_bytes"] == 262144
    if '"approved": "true"' in raw:
        assert proof["review_untrusted"]["approved"] == "true"
    if '"approved": true' in raw and '"issues": [' in raw and len(raw) < 65536:
        assert proof["review_untrusted"]["approved"] is True


def test_quarantine_classifier_obeys_original_parser_and_does_not_quarantine_valid_negative_four():
    value = {**PASS, "approved": False, "issues": ["Ошибка " + str(i) for i in range(4)]}
    raw = json.dumps(value, ensure_ascii=False)
    legacy = completed_review_rejection(raw, RUBRIC)
    assert legacy["review_untrusted"] == value and legacy["review_protocol_contract"] == REVIEW_PROTOCOL_CONTRACT
    assert legacy["parser_contract"] == review_parser_contract(raw)
    for valid in (raw, json.dumps(PASS)):
        with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
            completed_review_rejection(valid, RUBRIC, review_protocol_contract=CONTRACT)
    for invalid in ("", " " * 262145, "x" * 262145, "\ud800"):
        with pytest.raises(AgentError, match="ARTICLE_COMPLETED_REVIEW_NOT_SAFE"):
            completed_review_rejection(invalid, RUBRIC, review_protocol_contract=CONTRACT)


@pytest.mark.parametrize("raw", [
    '{"approved":NaN,"topic":"science","issues":[],"warnings":[]}',
    '{"approved":Infinity,"topic":"science","issues":[],"warnings":[]}',
    '{"approved":1e309,"topic":"science","issues":[],"warnings":[]}',
    '{"approved":false,"topic":"science","issues":["\\ud800"],"warnings":[]}',
    '{"approved":false,"topic":"science","issues":[],"warnings":[],"extra":' + "[" * 1500 + "0" + "]" * 1500 + "}",
], ids=["nan", "infinity", "float_overflow", "surrogate", "excessive_nesting"])
def test_completed_invalid_values_keep_raw_hash_but_cannot_poison_a_json_database_proof(raw):
    proof = completed_review_rejection(raw, RUBRIC, review_protocol_contract=CONTRACT)
    assert proof["review_untrusted"] is None and proof["approval_permitted"] is False
    assert proof["raw_review_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    json.dumps(proof, ensure_ascii=False, allow_nan=False).encode("utf-8")
    with pytest.raises(AgentError):
        decoded(raw)


def test_completed_invalid_object_copy_failure_preserves_only_the_exact_raw(monkeypatch):
    from max_postiz_agent import article_quality as module
    raw = json.dumps({**PASS, "extra": "simulate_deep_copy_recursion"})
    original = module.copy.deepcopy

    def bounded_copy(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("extra") == "simulate_deep_copy_recursion":
            raise RecursionError("Untrusted object cannot be copied")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(module.copy, "deepcopy", bounded_copy)
    proof = completed_review_rejection(raw, RUBRIC, review_protocol_contract=CONTRACT)
    assert proof["review_untrusted"] is None and proof["approval_permitted"] is False
    assert proof["raw_review_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    json.dumps(proof, ensure_ascii=False, allow_nan=False).encode("utf-8")


@pytest.mark.parametrize("contract", [None, REVIEW_PROTOCOL_CONTRACT, CONTRACT])
def test_only_new_review_returned_copies_the_completed_transport_marker(project, contract):
    events = []
    check, _, reviewer, _ = quality(project, PASS, contract=contract, events=events)
    completion = {"contract": "test-validated-generation", "response_sha256": "a" * 64,
                  "nested": {"immutable": True}}
    reviewer.last_completed_generation = completion
    try:
        candidate(check)
    finally:
        check.close()
    returned = next(e for e in events if e["phase"] == "review_returned")
    if contract == CONTRACT:
        assert returned["completion"] == completion
        completion["nested"]["immutable"] = False
        assert returned["completion"]["nested"]["immutable"] is True
    else:
        assert "completion" not in returned


def prepare_trace(project, warnings=10):
    events = []
    check, translator, reviewer, spelling = quality(project,
        {**PASS, "warnings": ["Допустимый совет " + str(i) for i in range(warnings)]}, events=events)
    try:
        prepared = candidate(check)
    finally:
        check.close()
    trace = project / "data/hourly-articles/quality-bounds-test.local.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    bound = [{**e, "record_id": "bounds-test", "at": index} for index, e in enumerate(events)]
    trace.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in bound), encoding="utf-8")
    assert not translator.prompts and len(reviewer.prompts) == len(spelling) == 1
    return prepared, trace, bound


def test_future_positive_evidence_replays_with_its_exact_contract_not_global_defaults(project):
    prepared, trace, _ = prepare_trace(project)
    metadata = machine_metadata(prepared, SOURCE, trace)
    assert metadata["review_protocol_contract"] == CONTRACT
    evidence = metadata["machine_review_evidence"]
    assert _validate(evidence, prepared["title"], prepared["body"], SOURCE)["warnings"] == prepared["warnings"]
    assert evidence["parser_contract"]["contract"] == CONTRACT
    assert evidence["trace_hash"] == hashlib.sha256(trace.read_bytes()).hexdigest()
    for change in ("marker", "parser"):
        invalid = copy.deepcopy(evidence)
        invalid.pop("review_protocol_contract" if change == "marker" else "parser_contract")
        with pytest.raises(AgentError):
            _validate(invalid, prepared["title"], prepared["body"], SOURCE)


@pytest.mark.parametrize("damage", ["event", "context", "accepted", "prepared", "parser"])
def test_even_rehashed_trace_cannot_drop_or_change_the_explicit_parser_binding(project, damage):
    prepared, trace, events = prepare_trace(project, warnings=2)
    if damage == "event":
        events[0].pop("review_protocol_contract")
    elif damage == "context":
        next(e for e in events if e["phase"] == "review_started")["context"].pop("review_protocol_contract")
    elif damage == "accepted":
        events[-1]["result"].pop("review_protocol_contract")
    elif damage == "prepared":
        prepared.pop("review_protocol_contract")
    else:
        prepared["machine_review_evidence"]["parser_contract"] = review_parser_contract(
            prepared["machine_review_evidence"]["review_raw"])
    trace.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    with pytest.raises(AgentError):
        machine_metadata(prepared, SOURCE, trace)


def future_reserve(value, monkeypatch):
    from test_automatic_policy import Model as CycleModel
    from test_automatic_article_repair import CORRECT_BODY
    policy = value.cycle._policy()
    policy["review_rubric"] = RUBRIC
    (value.core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    value.cycle.review_protocol_contract = CONTRACT

    def checked(record_id=None, review_only=False):
        return ArticleQuality(CycleModel({"title": "Космический ветер", "body": CORRECT_BODY}, "hy-mt2-7b-q8"),
            CycleModel(PASS, "ministral"), prompt_dir=value.core.settings.root / "prompts",
            review_rubric=RUBRIC, review_protocol_contract=CONTRACT, trace=value.cycle._trace_writer(record_id),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))

    monkeypatch.setattr(value.cycle, "_quality", checked)
    value.controller.prepare_once()
    record = value.cycle._load()["records"][0]
    assert record["state"] == "quality_ready" and record["review_protocol_contract"] == CONTRACT
    return record


def test_explicit_future_contract_survives_reserve_approval_delivery_and_status(reserve_controller, monkeypatch):
    value = reserve_controller
    record = future_reserve(value, monkeypatch)
    with value.core.db.session() as session:
        variant = session.get(Variant, record["variant_id"])
        assert variant.metadata_json["review_protocol_contract"] == CONTRACT
        assert variant.metadata_json["machine_review_evidence"]["review_protocol_contract"] == CONTRACT
    value.controller.run_once()
    scheduled = value.cycle._load()["records"][0]
    assert scheduled["state"] == "pending" and scheduled["review_protocol_contract"] == CONTRACT
    value.core.publisher.mode = "browser"
    value.stamp[0] = scheduled["due_at"] + 1
    assert value.core.dispatch_tick() == 1
    value.posts[scheduled["postiz_id"]]["state"] = "PUBLISHED"
    value.controller.run_once()
    status = value.cycle.status(record["id"])
    assert status["state"] == "published" and status["delivery_verified"] is True
    assert len(value.posts) == len(value.writes) == 1


@pytest.mark.parametrize("damage", ["record", "attempt", "audit", "variant", "evidence",
                                    "audit_actor", "orphan_audit", "attempt_bool"])
def test_rehashed_marker_downgrade_cannot_reinterpret_an_explicit_future_record(reserve_controller, monkeypatch, damage):
    from max_postiz_agent.article_cycle import review_protocol_binding
    value = reserve_controller
    record = copy.deepcopy(future_reserve(value, monkeypatch))
    with value.core.db.session.begin() as session:
        variant = session.get(Variant, record["variant_id"])
        if damage == "record":
            record.pop("review_protocol_contract")
        elif damage == "attempt":
            record["preparation_attempts"][0].pop("review_protocol_contract")
        elif damage == "audit":
            audit = session.query(Audit).filter_by(entity=record["id"], action="hourly_article_prepare_attempt").one()
            audit.details = {key: val for key, val in audit.details.items() if key != "review_protocol_contract"}
        elif damage == "audit_actor":
            audit = session.query(Audit).filter_by(entity=record["id"], action="hourly_article_prepare_attempt").one()
            audit.actor = "unrelated_actor"
        elif damage == "orphan_audit":
            audit = session.query(Audit).filter_by(entity=record["id"], action="hourly_article_prepare_attempt").one()
            session.add(Audit(actor=audit.actor, action=audit.action, entity=audit.entity,
                              details={**audit.details, "attempt": 999}))
            session.flush()
        elif damage == "attempt_bool":
            record["preparation_attempts"][0]["number"] = True
        else:
            metadata = copy.deepcopy(variant.metadata_json)
            if damage == "variant":
                metadata.pop("review_protocol_contract")
            else:
                evidence = copy.deepcopy(metadata["machine_review_evidence"])
                evidence.pop("review_protocol_contract")
                evidence["parser_contract"] = review_parser_contract(evidence["review_raw"])
                legacy_prompt = value.core.settings.root / "prompts" / review_prompt_name(RUBRIC)
                evidence["review_prompt_hash"] = hashlib.sha256(legacy_prompt.read_text(encoding="utf-8").encode()).hexdigest()
                source = {"title": value.article["title"], "body": value.article["original"], "url": value.article["url"]}
                # The forged payload really is valid under legacy parsing; the bound record must still reject it.
                assert _validate(evidence, variant.title, variant.body, source)["approved"] is True
                metadata.update(machine_review_evidence=evidence, machine_review_evidence_hash=fingerprint(evidence),
                    review_prompt_hash=evidence["review_prompt_hash"])
            variant.metadata_json = metadata
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID"):
            review_protocol_binding(value.core, session, record, variant)
    assert not value.posts and not value.writes
