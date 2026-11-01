"""Положительное доказательство завершённого protocol refusal, без запросов/записей.

Невалидный ответ не становится approved. Только новый contract, durable полный
HTTP-ответ и точная цепочка исходника/кандидата разрешают карантин источника.
"""
import copy
import hashlib
import json
import math
import re
from contextlib import nullcontext

from sqlalchemy import select

from .config import AgentError, fingerprint, inside, parse_json
from .db import Audit

COMPLETED_REVIEW_CONTRACT = "completed-review-protocol-quarantine-v1"
UNSAFE = "ARTICLE_COMPLETED_REVIEW_NOT_SAFE"
MAX_TRACE_BYTES = 2 * 1024 * 1024


def _require(value):
    if not value:
        raise AgentError(UNSAFE, 409)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _stamp(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


class _Files:
    def __init__(self, root):
        self.root, self.raw = root.resolve(), {}

    def read(self, relative, limit=MAX_TRACE_BYTES):
        _require(isinstance(relative, str) and relative.startswith("data/"))
        path = inside(self.root, relative)
        _require(path == self.root / relative and not path.is_symlink() and path.is_file())
        raw = path.read_bytes()
        _require(0 < len(raw) <= limit)
        _require(relative not in self.raw or self.raw[relative] == raw)
        self.raw[relative] = raw
        return raw

    def json(self, relative, limit=MAX_TRACE_BYTES):
        return parse_json(self.read(relative, limit).decode("utf-8"))

    def verify(self):
        for relative, raw in self.raw.items():
            _require(self.read(relative, max(len(raw), 1)) == raw)


def _completion(files, completion, raw_review, context, started, finished):
    from .adapters import _session_json
    names = ("intent.json", "session.json", "create.json", "create.raw",
             "response.json", "response.raw", "validated.json")
    _require(isinstance(completion, dict) and set(completion) == {
        "contract", "directory", "prompt_sha256", "text_sha256", "files"}
        and completion["contract"] == "opencode-completed-generation-v1"
        and isinstance(completion["directory"], str)
        and re.fullmatch(r"data/opencode-requests/[a-f0-9-]{36}", completion["directory"])
        and isinstance(completion["files"], dict) and set(completion["files"]) == set(names))
    directory = completion["directory"]
    path = inside(files.root, directory)
    _require(path == files.root / directory and path.is_dir() and not path.is_symlink()
        and set(p.name for p in path.iterdir()) == set(names))
    raw = {name: files.read(directory + "/" + name, 2 * MAX_TRACE_BYTES) for name in names}
    _require(all(_sha(raw[name]) == completion["files"][name] for name in names))
    objects = {name: parse_json(raw[name].decode("utf-8")) for name in names if name.endswith(".json")}
    _require(all(isinstance(value, dict) for value in objects.values()))
    intent, bound, created, response, validated = (objects[name] for name in (
        "intent.json", "session.json", "create.json", "response.json", "validated.json"))
    prompt = json.dumps(context, ensure_ascii=False)
    prompt_hash, text_hash = _sha(prompt.encode("utf-8")), _sha(raw_review.encode("utf-8"))
    session_id = bound.get("session_id")
    _require(isinstance(session_id, str) and re.fullmatch(r"ses[a-zA-Z0-9_-]+", session_id)
        and intent.get("contract") == "opencode-request-v1" and intent.get("transport") == "session"
        and intent.get("id") == directory.rsplit("/", 1)[1] and intent.get("prompt") == prompt
        and all(value.get("prompt_sha256") == prompt_hash for value in objects.values())
        and completion["prompt_sha256"] == prompt_hash and completion["text_sha256"] == text_hash
        and created.get("session_id") is None
        and response.get("session_id") == validated.get("session_id") == session_id
        and type(created.get("status_code")) is int and 200 <= created["status_code"] < 300
        and type(response.get("status_code")) is int and response["status_code"] == 200
        and created.get("body_sha256") == _sha(raw["create.raw"])
        and type(created.get("body_bytes")) is int and created["body_bytes"] == len(raw["create.raw"])
        and response.get("body_sha256") == _sha(raw["response.raw"])
        and type(response.get("body_bytes")) is int and response["body_bytes"] == len(raw["response.raw"])
        and validated.get("text_sha256") == text_hash
        and type(validated.get("text_bytes")) is int
        and validated["text_bytes"] == len(raw_review.encode("utf-8")))
    times = [intent.get("created_at"), created.get("received_at"), bound.get("bound_at"),
             response.get("received_at"), validated.get("validated_at")]
    _require(all(_stamp(value) and started <= value <= finished for value in times)
        and times == sorted(times))
    create = _session_json(raw["create.raw"])
    _require(isinstance(create, dict) and isinstance(create.get("data"), dict)
        and create["data"].get("id") == session_id)
    received = _session_json(raw["response.raw"])
    value = received.get("data", received) if isinstance(received, dict) else None
    _require(isinstance(value, dict) and value.get("text") == raw_review)
    return copy.deepcopy(completion)


def _repair(cycle, record, attempt, article, candidate, files, session):
    # Не вызываем _history/status: их новая terminal-ветка сама читает этот proof.
    from .article_repair_cycle import AutomaticArticleRepair, _BINDING, _claim_id
    from .article_attribution import presentation_binding
    from .article_repair import REPAIR_MODEL, feedback_prompt_name, repair_context, repair_replay_hash
    history = record.get("automatic_repair_attempts")
    _require(isinstance(history, list) and 1 <= len(history) <= 2
        and all(isinstance(claim, dict) for claim in history))
    repair = AutomaticArticleRepair(cycle)
    for number, claim in enumerate(history, 1):
        audit = session.get(Audit, _claim_id(record["id"], number))
        _require(claim.get("id") == _claim_id(record["id"], number) and claim.get("number") == number
            and audit is not None and audit.actor == "automatic_article_repair"
            and audit.entity == record["id"] and audit.action == "automatic_article_repair_claim"
            and isinstance(audit.details, dict)
            and all(claim.get(key) == audit.details.get(key) for key in _BINDING))
        if claim is not history[-1]:
            _require(claim.get("state") == "rejected" and _stamp(claim.get("finished_at")))
        else:
            _require(claim.get("state") == "checking" and claim.get("outcome_origin") is None
                or claim.get("state") == "not_repairable"
                and claim.get("outcome_origin") == "review_protocol_guard")
        repair._files(record, claim, article, candidate if claim is history[-1] else None)
        for path_key, hash_key in (("snapshot_path", "snapshot_hash"), ("proposal_path", "proposal_hash"),
                                   ("repair_trace", "repair_trace_hash")):
            _require(_sha(files.read(claim[path_key])) == claim[hash_key])
    claim = history[-1]
    _require(attempt.get("automatic_repair_claim") == claim["id"]
        and fingerprint(candidate) == claim.get("candidate_hash")
        and claim.get("review_protocol_contract") == record["review_protocol_contract"]
        and repair._prompts(claim["contract"], record["review_rubric"],
            review_protocol_contract=record["review_protocol_contract"]) == claim.get("prompts"))
    snapshot = files.json(claim["snapshot_path"])
    proposal_raw = files.read(claim["proposal_path"])
    proposal = parse_json(proposal_raw.decode("utf-8"))
    trace_raw = files.read(claim["repair_trace"], 512 * 1024)
    events = [parse_json(line) for line in trace_raw.decode("utf-8").splitlines()]
    expected = {"contract": claim["contract"], "model": REPAIR_MODEL,
        "prompt_hash": claim["prompts"][feedback_prompt_name(claim["contract"], claim.get("feedback_contract"))],
        "input_hash": claim["input_hash"], "old_candidate_hash": fingerprint(snapshot["candidate"]),
        "old_replay_hash": repair_replay_hash(snapshot["candidate"]),
        "feedback_hash": fingerprint(snapshot["feedback"]),
        **presentation_binding(claim.get("presentation_contract"), claim.get("image_credit")),
        **repair._feedback_binding(claim), "reference_contract": repair._claim_reference(claim)}
    times = [event.get("at") for event in events]
    _require(_sha(proposal_raw) == claim["proposal_hash"] and _sha(trace_raw) == claim["repair_trace_hash"]
        and proposal.get("candidate") == candidate and proposal.get("evidence") == claim["evidence"]
        and proposal.get("repairable") is not False and isinstance(proposal.get("raw"), str)
        and _sha(proposal["raw"].encode("utf-8")) == proposal["evidence"].get("raw_sha256")
        and [event.get("phase") for event in events] == ["repair_started", "repair_returned", "repair_candidate"]
        and all(event.get("record_id") == record["id"] and event.get("repair_claim") == claim["id"]
            and event.get("attempt") == 1 and event.get("review_protocol_contract") == record["review_protocol_contract"]
            for event in events)
        and all(_stamp(value) and claim["started_at"] <= value <= attempt["started_at"] for value in times)
        and times == sorted(times) and events[-1].get("result") == proposal
        and events[1].get("raw") == proposal["raw"]
        and all(events[index].get("binding") == expected for index in (0, 1))
        and all(proposal["evidence"].get(key) == value for key, value in expected.items())
        and events[0].get("context") == repair_context(article, snapshot["candidate"], snapshot["feedback"],
            contract=claim["contract"], feedback_contract=claim.get("feedback_contract"))
        and (claim.get("feedback_contract") is None or events[0].get("raw_feedback") == snapshot["feedback"]
             and events[0].get("model_input") == claim["model_input"]))
    return {"claim_id": claim["id"], "binding_hash": fingerprint({key: claim.get(key) for key in _BINDING}),
        "candidate_hash": claim["candidate_hash"], "proposal_hash": claim["proposal_hash"],
        "repair_trace_hash": claim["repair_trace_hash"], "snapshot_hash": claim["snapshot_hash"]}


def completed_review_proof(cycle, record, article=None, *, require_anchor=True, session=None):
    """Read exact durable evidence; timeout, partial reply and legacy fail closed."""
    from .adapters import extract_object
    from .article_attribution import article_footer, presentation_binding
    from .article_cycle import review_protocol_binding
    from .article_quality import (REVIEW_BOUNDS_V2_CONTRACT, HOURLY_PRACTICAL_V2_REVIEW_RUBRIC,
        article_review_context, completed_review_rejection, normalize_article_links,
        review_prompt_name, rubric_translation_gate, numbers)
    from .reddit import text_hash
    try:
        _require(isinstance(record, dict) and re.fullmatch(r"[a-f0-9-]{36}", record.get("id", ""))
            and record.get("review_protocol_contract") == REVIEW_BOUNDS_V2_CONTRACT
            and record.get("review_rubric") == HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
            and record.get("state") in {"needs_attention", "rejected", "failed"}
            and record.get("error") == "ARTICLE_QUALITY_REJECTED" and not record.get("model_result_unknown")
            and not any(record.get(key) for key in ("variant_id", "postiz_id", "dispatch_id", "material_id",
                "story_id", "due_at", "prepared_hash", "publication_slot_id")))
        attempts = record.get("preparation_attempts")
        _require(isinstance(attempts, list) and attempts and all(isinstance(a, dict) for a in attempts))
        attempt = attempts[-1]
        _require(type(attempt.get("number")) is int and attempt["number"] == len(attempts)
            and attempt.get("state") in {"needs_attention", "rejected", "failed"}
            and attempt.get("error") == "ARTICLE_QUALITY_REJECTED"
            and attempt.get("review_protocol_contract") == REVIEW_BOUNDS_V2_CONTRACT
            and _stamp(attempt.get("started_at")) and _stamp(attempt.get("finished_at"))
            and attempt["started_at"] <= attempt["finished_at"])
        files = _Files(cycle.core.settings.root)
        expected = f"data/hourly-articles/input-{record['id']}.local.json"
        _require(record.get("input_file") == expected)
        input_raw = files.read(expected)
        saved = parse_json(input_raw.decode("utf-8"))
        _require(isinstance(saved, dict) and set(saved) == {"title", "original", "url", "published_at"}
            and all(isinstance(saved[key], str) and saved[key].strip() for key in ("title", "original", "url"))
            and (article is None or saved == article) and _sha(input_raw) == record.get("input_file_hash")
            and fingerprint(saved) == record.get("input_hash") and text_hash(saved["original"]) == record.get("original_hash")
            and saved["url"] == record.get("url") and saved["published_at"] == record.get("source_published_at"))
        article = saved
        source = {"title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]}
        expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
        _require(record.get("quality_trace") == expected)
        trace_raw = files.read(expected)
        lines = trace_raw.decode("utf-8").splitlines()
        _require(lines and len(lines) <= 10000 and all(line.strip() for line in lines))
        events = [parse_json(line) for line in lines]
        _require(all(isinstance(event, dict) and event.get("record_id") == record["id"]
            and _stamp(event.get("at")) and type(event.get("attempt")) is int and event["attempt"] >= 1 for event in events))
        _require([event["at"] for event in events] == sorted(event["at"] for event in events))
        starts = [index for index, event in enumerate(events) if event.get("phase") in {
            "translation_started", "candidate_started"} and event["attempt"] == 1]
        _require(len(starts) == len(attempts))
        segment = events[starts[-1]:]
        _require(all(attempt["started_at"] <= event["at"] <= attempt["finished_at"] and event["attempt"] == 1
            and event.get("review_protocol_contract") == REVIEW_BOUNDS_V2_CONTRACT for event in segment))
        phases = [event.get("phase") for event in segment]
        initial = phases == ["translation_started", "translation_returned", "translation_parsed", "gate_passed",
                            "review_started", "review_returned", "review_invalid", "retry", "rejected"]
        candidate_mode = phases == ["candidate_started", "gate_passed", "review_started", "review_returned", "review_invalid", "rejected"]
        _require(initial or candidate_mode)
        markers = presentation_binding(record.get("presentation_contract"), record.get("image_credit"))
        _require(all(all(event.get(key) == value for key, value in markers.items()) for event in segment))
        if initial:
            _require(len(attempts) == 1 and not record.get("automatic_repair_attempts")
                and not attempt.get("candidate_hash") and not attempt.get("automatic_repair_claim")
                and attempt.get("operator_retry") is False)
            candidate = extract_object(segment[1].get("raw"))
            _require(set(candidate) == {"title", "body"} and all(isinstance(v, str) and v.strip() for v in candidate.values()))
            candidate = {key: normalize_article_links(value.strip()) for key, value in candidate.items()}
            _require(segment[2].get("translation") == candidate)
            context = {"instruction": (files.root / "prompts/ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8"),
                "source_untrusted": source, "numeric_tokens_must_match_exactly": sorted(numbers(source["title"] + "\n\n" + source["body"])),
                "review_protocol_contract": REVIEW_BOUNDS_V2_CONTRACT}
            if record.get("scientific_spelling_contract"):
                context["scientific_spelling_contract"] = record["scientific_spelling_contract"]
            if record.get("reference_contract") == "article-links-v2":
                context["reference_contract"] = record["reference_contract"]
            _require(segment[0].get("context") == context and segment[-1].get("mode") is None
                and segment[-1].get("feedback") == segment[-2].get("feedback"))
        else:
            candidate = segment[0].get("candidate")
            _require(isinstance(candidate, dict) and set(candidate) == {"title", "body"}
                and all(isinstance(v, str) and v.strip() == v and v for v in candidate.values())
                and segment[0].get("source_untrusted") == source and segment[0].get("mode") == "candidate"
                and segment[-1].get("mode") == "candidate" and attempt.get("operator_correction") is False
                and attempt.get("candidate_hash") == cycle._candidate_hash(candidate)
                and attempt.get("candidate_raw_hash") == fingerprint(segment[0].get("candidate_raw", candidate)))
        _require(segment[-1].get("error") == "ARTICLE_QUALITY_REJECTED")
        text, original = candidate["title"] + "\n\n" + candidate["body"], source["title"] + "\n\n" + source["body"]
        references = record.get("reference_contract", "article-links-v1")
        rubric_translation_gate(original, text, record["review_rubric"], reference_contract=references)
        full = text + "\n\n" + article_footer(article["url"], **markers)
        gate, review_start, returned, invalid = (segment[i] for i in ((3, 4, 5, 6) if initial else (1, 2, 3, 4)))
        _require(len(full) <= 3500 and gate.get("characters") == len(full))
        prompt = (files.root / "prompts" / review_prompt_name(record["review_rubric"],
            review_protocol_contract=REVIEW_BOUNDS_V2_CONTRACT)).read_text(encoding="utf-8")
        context = article_review_context(prompt, original, text, record["review_rubric"], _sha(prompt.encode("utf-8")),
            reference_contract=references, review_protocol_contract=REVIEW_BOUNDS_V2_CONTRACT)
        _require(review_start.get("context") == context and isinstance(returned.get("raw"), str))
        raw_review = returned["raw"]
        classification = completed_review_rejection(raw_review, record["review_rubric"],
            review_protocol_contract=REVIEW_BOUNDS_V2_CONTRACT)
        _require(invalid.get("error") == classification["parser_error"]
            and (invalid.get("raw") == raw_review or "review" in invalid
                 and fingerprint(invalid["review"]) == fingerprint(extract_object(raw_review))))
        completion = _completion(files, returned.get("completion"), raw_review, context,
                                 review_start["at"], returned["at"])
        with (cycle.core.db.session() if session is None else nullcontext(session)) as own:
            _require(review_protocol_binding(cycle.core, own, record) == REVIEW_BOUNDS_V2_CONTRACT)
            repair = _repair(cycle, record, attempt, article, candidate, files, own) if candidate_mode else None
            proof = {"contract": COMPLETED_REVIEW_CONTRACT, "review_protocol_contract": REVIEW_BOUNDS_V2_CONTRACT,
                "decision": "quarantine_not_passed", "record_id": record["id"], "kind": "candidate" if candidate_mode else "initial",
                "attempt": attempt["number"], "started_at": attempt["started_at"], "finished_at": attempt["finished_at"],
                "input_hash": record["input_hash"], "input_file_hash": record["input_file_hash"],
                "source_hash": record["original_hash"], "policy_hash": record["policy_hash"],
                "candidate_hash": fingerprint(candidate), "quality_trace_hash": _sha(trace_raw), "quality_trace_bytes": len(trace_raw),
                "segment_hash": fingerprint(segment), "review_prompt_hash": _sha(prompt.encode("utf-8")),
                "raw_review_sha256": _sha(raw_review.encode("utf-8")), "classification": classification,
                "completion": completion, "repair": repair}
            if require_anchor:
                _require(attempt.get("completed_review_protocol") == proof)
                audits = list(own.scalars(select(Audit).where(Audit.entity == record["id"],
                    Audit.actor == "article_quality", Audit.action == "hourly_article_prepare_error")))
                matching = [audit for audit in audits if isinstance(audit.details, dict)
                    and audit.details.get("attempt") == attempt["number"]]
                _require(len(matching) == 1 and matching[0].details.get("error") == "ARTICLE_QUALITY_REJECTED"
                    and matching[0].details.get("completed_review_protocol_hash") == fingerprint(proof))
        files.verify()
        return proof
    except (AgentError, OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
        raise AgentError(UNSAFE, 409) from exc
