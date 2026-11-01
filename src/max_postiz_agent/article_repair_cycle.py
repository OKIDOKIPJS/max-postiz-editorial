"""Однократные исправления известного pre-DRAFT отказа; согласования здесь нет."""
import copy
import hashlib
import json
import math
import os
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .article_attribution import presentation_binding
from .article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACTS
from .article_repair import (COMPACT_FEEDBACK_CONTRACT, COMPACT_REPAIR_PROMPT,
                             REPAIR_CONTRACT, REPAIR_CONTRACTS, REPAIR_MODEL, compact_repair_input,
                             feedback_prompt_name, repair_context, repair_prompt_name,
                             repair_reference_contract, repair_replay_hash)
from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside, parse_json
from .db import Audit
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, Target
from .reddit import text_hash
from .server_state import require_running

MAX_AUTOMATIC_REPAIRS = 2
QUARANTINE_ERRORS = {"ARTICLE_REPAIR_RESPONSE_INVALID", "ARTICLE_REPAIR_PATCH_INVALID",
                     "TRANSLATION_REFERENCE_CHANGED", "TRANSLATION_NUMBER_CHANGED",
                     "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"}
_BINDING = ("id", "number", "contract", "model", "input_hash", "policy_hash", "slot",
            "parent_trace_hash", "parent_trace_bytes", "snapshot_hash", "snapshot_path", "prompts", "target_binding_hash",
            "source_config_hash", "reference_contract", "review_rubric", "presentation_contract", "image_credit",
            "feedback_contract", "raw_feedback_hash", "model_input", "review_protocol_contract")


def _now():
    # Единые часы с существующим протоколом и его проверками границы часа.
    from .article_cycle import now
    return now()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _claim_id(ident, number):
    return str(uuid5(NAMESPACE_URL, f"max-postiz:automatic-repair-v1:{ident}:{number}"))


def _write_json(path, value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if path.exists():
        if path.read_bytes() != raw:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_FILE_CHANGED", 409)
        return _sha(raw)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return _sha(raw)


def capture_input(cycle, record, article):
    """Первичный вход сохраняется до переводчика, включая пробелы и тип даты."""
    relative = f"data/hourly-articles/input-{record['id']}.local.json"
    record.update(input_file=relative, input_file_hash=_write_json(inside(cycle.core.settings.root, relative), article))


class AutomaticArticleRepair:
    def __init__(self, cycle):
        self.cycle, self.core = cycle, cycle.core
        self._reserve_context = False

    def _history(self, record):
        history = record.get("automatic_repair_attempts", [])
        if not isinstance(history, list) or len(history) > MAX_AUTOMATIC_REPAIRS:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_STATE_INVALID", 409)
        with self.core.db.session() as session:
            for number in range(1, MAX_AUTOMATIC_REPAIRS + 1):
                audit = session.get(Audit, _claim_id(record["id"], number))
                claim = history[number - 1] if number <= len(history) else None
                if number <= len(history) and (not isinstance(claim, dict) or not claim):
                    return history, "CLAIM_LOST"
                # Audit PK переживает потерю/откат ledger и не выдаёт второй запрос.
                if bool(audit) != bool(claim):
                    return history, "CLAIM_LOST"
                if claim and (not isinstance(claim, dict) or claim.get("number") != number
                        or claim.get("id") != _claim_id(record["id"], number)
                        or audit.actor != "automatic_article_repair"
                        or audit.entity != record["id"] or audit.action != "automatic_article_repair_claim"
                        or not isinstance(audit.details, dict)
                        or any(claim.get(key) != audit.details.get(key) for key in _BINDING)):
                    return history, "CLAIM_LOST"
        for claim in history:
            state = claim.get("state")
            if state in {"correcting", "checking"}:
                return history, "IN_PROGRESS"
            if state not in {"passed", "rejected", "not_repairable", "no_change", "expired"}:
                return history, "FAILED"
            if (type(claim.get("finished_at")) not in {int, float}
                    or not math.isfinite(claim["finished_at"])):
                return history, "FAILED"
        return history, None

    def status(self, record):
        history, blocked = self._history(record)
        result = {"eligible": False, "reason": blocked or "NOT_ELIGIBLE", "attempts": len(history)}
        if blocked:
            return result
        reserved = self.cycle.reserve_mode and record.get("reserve_contract") == "article-reserve-v1"
        if reserved:
            try:
                self.cycle._reserve_record(record, active=True)
            except AgentError:
                return {**result, "reason": "CLAIM_LOST"}
        self._reserve_context = reserved
        if history and history[-1].get("outcome_origin") == "protocol_guard":
            try:
                self._quarantine_proof(record, history[-1], saved=True)
            except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError):
                return {**result, "reason": "TRACE_INVALID"}
        if history and history[-1].get("outcome_origin") == "review_protocol_guard":
            try:
                self._review_protocol_guard_proof(record, history[-1], saved=True)
            except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                return {**result, "reason": "TRACE_INVALID"}
            return {**result, "reason": "NOT_REPAIRABLE"}
        if record.get("state") != "needs_attention" or record.get("error") != "ARTICLE_QUALITY_REJECTED":
            return result
        try:
            payload = self._payload(record)
            self.cycle._assert_pre_draft(record)
        except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return {**result, "reason": "TRACE_INVALID"}
        if not reserved and record.get("slot") < int(_now() // 3600):
            return {**result, "reason": "EXPIRED"}
        if not reserved and record.get("slot") != int(_now() // 3600):
            return result
        if history and history[-1]["state"] in {"not_repairable", "no_change", "expired", "passed"}:
            return {**result, "reason": {"not_repairable": "NOT_REPAIRABLE", "no_change": "NO_CHANGE"}.get(history[-1]["state"], "EXHAUSTED")}
        if len(history) >= MAX_AUTOMATIC_REPAIRS:
            return {**result, "reason": "EXHAUSTED"}
        if self.cycle._policy_for_record(record).get("approval_mode") != "automatic":
            return result
        if payload.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != repair_reference_contract(REPAIR_CONTRACT):
            return {**result, "reason": "REFERENCE_CONTRACT_CHANGED"}
        if payload.get("feedback_contract") == COMPACT_FEEDBACK_CONTRACT:
            if not payload["model_input"]["fits"]:
                return {**result, "reason": "MODEL_INPUT_BUDGET", "model_input": payload["model_input"]}
        return {**result, "eligible": True, "reason": "READY"}

    @staticmethod
    def _claim_reference(claim):
        """V6/V7 хранят marker; прежние claims остаются без нового поля."""
        expected = repair_reference_contract(claim["contract"])
        if ((claim["contract"] in {"article-repair-v6", "article-repair-v7", "article-repair-v8"}) != ("reference_contract" in claim)
                or claim.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != expected):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        return expected

    def _prompts(self, contract=None, rubric="meaning-first-v1", *, review_protocol_contract=None):
        # Claim привязан к своей версии промпта: новая редакция не переоценивает
        # сохранённые v3/v4 и не меняет их имена полей или hashes в истории.
        repair_prompt = repair_prompt_name(REPAIR_CONTRACT if contract is None else contract)
        from .article_quality import review_prompt_name
        root = self.core.settings.root / "prompts"
        from .article_quality import HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
        names = ["ARTICLE_TRANSLATE.txt", review_prompt_name(rubric,
                 review_protocol_contract=review_protocol_contract), repair_prompt]
        if rubric == HOURLY_PRACTICAL_V2_REVIEW_RUBRIC:
            names.append(COMPACT_REPAIR_PROMPT)
        return {name: _sha((root / name).read_bytes()) for name in names}

    def _model_input(self, payload):
        instruction = (self.core.settings.root / "prompts" / COMPACT_REPAIR_PROMPT).read_text(encoding="utf-8")
        return compact_repair_input(payload["article"], payload["candidate"], payload["feedback"], instruction)

    @staticmethod
    def _feedback_kwargs(claim):
        return ({"feedback_contract": claim["feedback_contract"], "model_input_proof": claim["model_input"]}
                if claim.get("feedback_contract") is not None else {})

    @staticmethod
    def _feedback_binding(claim):
        return ({"feedback_contract": claim["feedback_contract"], "raw_feedback_hash": claim["raw_feedback_hash"],
                 "model_input_hash": fingerprint(claim["model_input"])}
                if claim.get("feedback_contract") is not None else {})

    def _quiet(self, record, policy):
        require_running(self.core)
        self.cycle._assert_pre_draft(record)
        reserved = self.cycle.reserve_mode and record.get("reserve_contract") == "article-reserve-v1"
        if reserved:
            self.cycle._reserve_record(record, active=True)
        self._reserve_context = reserved
        if ((not reserved and record["slot"] != int(_now() // 3600))
                or fingerprint(self.cycle._policy_for_record(record)) != record["policy_hash"]
                or record["policy_hash"] != fingerprint(policy)):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        source, target = self.cycle._source_target(policy)
        if record.get("target_id") != target.id or record.get("source_id") != source["id"]:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        with self.core.db.session() as session:
            self._quiet_session(session)
        return source, target

    def _quiet_session(self, session):
        if self._reserve_context:
            # Claim/исходник/политика и отсутствие внешнего состояния ЭТОГО
            # кандидата уже проверены _quiet/_quarantine_proof. Готовый текст
            # сохраняется локально, даже если независимый отправитель начал Send.
            # Право stage/approval этим не выдаётся: там свои target/lease gates.
            return
        if (any(not target.paused for target in session.scalars(select(Target)))
                or session.scalar(select(Dispatch).where(Dispatch.state.in_(ACTIVE)
                    | Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
                or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                    | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_NOT_QUIET", 409)

    def _payload(self, record):
        """Читаем полный завершённый отказ, не принимая замечания за инструкции."""
        from .adapters import extract_object
        from .article_attribution import article_footer
        from .article_quality import (_numeric_feedback, article_review_context, decode_rubric_review,
                                      normalize_article_links, review_protocol_marker,
                                      rubric_spelling, rubric_translation_gate, TOPICS)
        from .editorial import numbers

        def require(value):
            if not value:
                raise ValueError

        def timestamp(value):
            require(type(value) in {int, float} and math.isfinite(value) and value >= 0)
            return value

        def fields(value):
            require(isinstance(value, dict) and set(value) == {"title", "body"}
                    and all(isinstance(v, str) and v.strip() for v in value.values()))
            return {key: normalize_article_links(value[key].strip()) for key in ("title", "body")}

        try:
            from .article_cycle import automatic_review_rubric, review_rubric_binding, scientific_contract_binding
            rubric = automatic_review_rubric(record)
            protocol = record.get("review_protocol_contract")
            protocol_marker = review_protocol_marker(protocol, rubric)
            presentation = presentation_binding(record.get("presentation_contract"), record.get("image_credit"))
            with self.core.db.session() as session:
                scientific_contract = scientific_contract_binding(self.core, session, record)
                review_rubric_binding(self.core, session, record)
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            require(record.get("quality_trace") == expected and type(record.get("slot")) is int)
            raw = inside(self.core.settings.root, expected).read_bytes()
            require(0 < len(raw) <= 2 * 1024 * 1024)
            lines = raw.decode("utf-8").splitlines()
            require(0 < len(lines) <= 10000 and all(line.strip() for line in lines))
            events, previous = [], -1
            for line in lines:
                event = parse_json(line)
                require(isinstance(event, dict) and event.get("record_id") == record["id"]
                        and isinstance(event.get("phase"), str)
                        and event.get("review_protocol_contract") == protocol
                        and type(event.get("attempt")) is int and 1 <= event["attempt"] <= 3)
                require(presentation_binding(event.get("presentation_contract"), event.get("image_credit")) == presentation)
                stamp = timestamp(event.get("at"))
                require(stamp >= previous)
                previous = stamp
                events.append(event)
            attempts = record.get("preparation_attempts")
            require(isinstance(attempts, list) and attempts and all(isinstance(a, dict) for a in attempts))
            attempt = attempts[-1]
            require(type(attempt.get("number")) is int and attempt["number"] == len(attempts)
                    and attempt.get("state") in {"needs_attention", "rejected", "failed"}
                    and attempt.get("error") == "ARTICLE_QUALITY_REJECTED")
            started, finished = timestamp(attempt.get("started_at")), timestamp(attempt.get("finished_at"))
            require(timestamp(record.get("claimed_at")) <= started <= finished)
            first = next(e for e in events if e["phase"] == "translation_started")
            first_source = first["context"]["source_untrusted"]
            require(isinstance(first_source, dict) and set(first_source) == {"url", "title", "body"})
            article = {"url": first_source["url"], "title": first_source["title"],
                       "original": first_source["body"], "published_at": record["source_published_at"]}
            if record.get("input_file"):
                expected_input = f"data/hourly-articles/input-{record['id']}.local.json"
                require(record["input_file"] == expected_input)
                saved = inside(self.core.settings.root, expected_input).read_bytes()
                require(0 < len(saved) <= 40000 and _sha(saved) == record.get("input_file_hash"))
                article = parse_json(saved.decode("utf-8"))
            require(isinstance(article, dict) and set(article) == {"url", "title", "original", "published_at"}
                    and all(isinstance(article[k], str) and article[k].strip() for k in ("url", "title", "original")))
            timestamp(article["published_at"])
            require(fingerprint(article) == record["input_hash"] and article["url"] == record["url"]
                    and text_hash(article["original"]) == record["original_hash"]
                    and article["published_at"] == record["source_published_at"])
            source = {"title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]}
            require(first_source == source)
            for old in attempts:
                if not old.get("candidate_hash") and old.get("error") == "ARTICLE_QUALITY_REJECTED":
                    self.cycle._load_canonical_initial_candidates(record, old, article)
            # Два последовательных вызова могут иметь одинаковые timestamp.
            # Последний вызов отделяется фазой attempt=1, а не только временем.
            call_starts = [i for i, e in enumerate(events) if e["phase"] in {
                "translation_started", "candidate_started"} and e["attempt"] == 1]
            require(call_starts)
            segment = events[call_starts[-1]:]
            require(segment and all(started <= e["at"] <= finished for e in segment))
            terminal = segment[-1]
            initial = segment[0]["phase"] == "translation_started"
            require(initial == (not bool(attempt.get("candidate_hash"))))
            start_context = segment[0].get("context") if initial else segment[0]
            require(isinstance(start_context, dict)
                    and start_context.get("scientific_spelling_contract") == scientific_contract)
            reference_contract = start_context.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
            require(reference_contract in REFERENCE_CONTRACTS
                    and attempt.get("reference_contract", LEGACY_REFERENCE_CONTRACT) == reference_contract
                    and record.get("reference_contract", LEGACY_REFERENCE_CONTRACT) == reference_contract)
            require(terminal["phase"] == "rejected" and terminal.get("error") == "ARTICLE_QUALITY_REJECTED"
                    and terminal.get("scientific_spelling_contract") == scientific_contract
                    and terminal.get("mode") == (None if initial else "candidate"))
            starts = [i for i, e in enumerate(segment[:-1]) if e["phase"] in {"translation_started", "candidate_started"}]
            require(starts and starts[0] == 0 and len(starts) == terminal["attempt"]
                    and (initial or len(starts) == 1))
            translation_prompt = (self.core.settings.root / "prompts/ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8")
            from .article_quality import review_prompt_name
            review_prompt = (self.core.settings.root / "prompts" / review_prompt_name(rubric,
                             review_protocol_contract=protocol)).read_text(encoding="utf-8")
            review_hash = _sha(review_prompt.encode("utf-8"))
            original = source["title"] + "\n\n" + source["body"]
            prior_feedback = {}
            candidate = None
            for number, left in enumerate(starts, 1):
                right = starts[number] if number < len(starts) else len(segment) - 1
                block = segment[left:right]
                require(block and all(e["attempt"] == number for e in block))
                phases = [e["phase"] for e in block]
                if initial:
                    require(phases[:3] == ["translation_started", "translation_returned", "translation_parsed"])
                    context = block[0].get("context")
                    require(isinstance(context, dict) and context.get("source_untrusted") == source
                            and context.get("instruction") == translation_prompt
                            and context.get("reference_contract", LEGACY_REFERENCE_CONTRACT) == reference_contract
                            and context.get("review_protocol_contract") == protocol
                            and context.get("scientific_spelling_contract") == scientific_contract
                            and context.get("numeric_tokens_must_match_exactly") == sorted(numbers(original))
                            and isinstance(block[1].get("raw"), str))
                    candidate = fields(extract_object(block[1]["raw"]))
                    require(block[2].get("translation") == candidate)
                    tail = block[3:]
                    require(tail and tail[-1]["phase"] == "retry")
                    retry = tail.pop()
                else:
                    require(phases[0] == "candidate_started" and block[0].get("mode") == "candidate"
                            and block[0].get("scientific_spelling_contract") == scientific_contract
                            and block[0].get("reference_contract", LEGACY_REFERENCE_CONTRACT) == reference_contract
                            and block[0].get("source_untrusted") == source)
                    candidate = fields(block[0].get("candidate"))
                    require(block[0]["candidate"] == candidate)
                    supplied = block[0].get("candidate_raw", candidate)
                    require(fields(supplied) == candidate
                            and self.cycle._candidate_hash(candidate) == attempt.get("candidate_hash"))
                    if attempt.get("candidate_raw_hash") is not None:
                        require(fingerprint(supplied) == attempt["candidate_raw_hash"])
                    tail, retry = block[1:], None
                translated = candidate["title"] + "\n\n" + candidate["body"]
                full = translated + "\n\n" + article_footer(article["url"], **presentation)
                gate_error = None
                try:
                    rubric_translation_gate(original, translated, rubric,
                                            reference_contract=reference_contract)
                    if len(full) > 3500:
                        raise AgentError("ARTICLE_POST_TOO_LONG")
                except AgentError as exc:
                    gate_error = exc.code
                require(tail and tail[0].get("characters") == len(full))
                if gate_error:
                    require([e["phase"] for e in tail] == ["gate_rejected"] and tail[0].get("error") == gate_error)
                    feedback = _numeric_feedback(original, translated) if gate_error == "TRANSLATION_NUMBER_CHANGED" else gate_error
                else:
                    require(tail[0]["phase"] == "gate_passed")
                    replay = repair_replay_hash(candidate)
                    if [e["phase"] for e in tail] == ["gate_passed", "candidate_repeated"]:
                        require(replay in prior_feedback and tail[1].get("feedback") == prior_feedback[replay])
                        feedback = prior_feedback[replay]
                    else:
                        phases = [e["phase"] for e in tail]
                        expected_phases = ["gate_passed", "review_started", "review_returned", "review_parsed",
                                           "spelling_started", "spelling_returned", "spelling_adjudicated"]
                        if "review_normalized" in phases:
                            expected_phases.insert(3, "review_normalized")
                        require(phases == expected_phases)
                        context = tail[1].get("context")
                        require(isinstance(context, dict) and context == article_review_context(
                            review_prompt, original, translated, rubric, review_hash,
                            reference_contract=reference_contract, review_protocol_contract=protocol))
                        require(isinstance(tail[2].get("raw"), str))
                        reviewed = decode_rubric_review(tail[2]["raw"], rubric, review_protocol_contract=protocol)
                        raw_review = extract_object(tail[2]["raw"])
                        normalized = [e for e in tail if e["phase"] == "review_normalized"]
                        require(bool(normalized) == (reviewed != raw_review))
                        if normalized:
                            require(normalized[0].get("raw_review") == raw_review and normalized[0].get("review") == reviewed)
                        parsed = next(e for e in tail if e["phase"] == "review_parsed")
                        require(parsed.get("review") == reviewed)
                        matches = next(e for e in tail if e["phase"] == "spelling_returned").get("matches")
                        require(isinstance(matches, list) and all(isinstance(m, dict) for m in matches))
                        adjudicated, unresolved = rubric_spelling(original, translated, matches, rubric,
                            allow_spacetime=True, allow_registered_terms=scientific_contract is not None)
                        spelling = tail[-1]
                        require(spelling.get("scientific_spelling_contract") == scientific_contract
                                and spelling.get("adjudicated") == adjudicated and spelling.get("unresolved") == unresolved
                                and not (reviewed["approved"] is True and not reviewed["issues"]
                                         and reviewed["topic"] in TOPICS and not unresolved))
                        feedback = {"review_approved": reviewed["approved"], "topic": reviewed["topic"],
                                    "review_issues": reviewed["issues"], "spelling_matches": unresolved[:20]}
                if retry is not None:
                    require(retry.get("feedback") == feedback)
                prior_feedback[repair_replay_hash(candidate)] = feedback
            require(terminal.get("feedback") == feedback)
            rejected = set()
            for event in events:
                if event["phase"] in {"translation_parsed", "candidate_started"}:
                    value = event.get("translation") if event["phase"] == "translation_parsed" else event.get("candidate")
                    require(fields(value) == value)
                    rejected.add(repair_replay_hash(value))
            feedback = {"events": [copy.deepcopy(e) for e in segment if e["phase"] in {
                "gate_rejected", "review_returned", "review_parsed", "spelling_returned", "spelling_adjudicated", "retry", "rejected"}]}
            payload = {"article": article, "candidate": candidate, "feedback": feedback,
                       "parent_trace_hash": _sha(raw), "rejected_replay_hashes": sorted(rejected)}
            payload.update(presentation)
            payload.update(protocol_marker)
            if reference_contract != LEGACY_REFERENCE_CONTRACT:
                payload["reference_contract"] = reference_contract
            if scientific_contract is not None:
                payload["scientific_spelling_contract"] = scientific_contract
            if "review_rubric" in record:
                payload["review_rubric"] = rubric
            from .article_quality import HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
            if rubric == HOURLY_PRACTICAL_V2_REVIEW_RUBRIC:
                payload.update(feedback_contract=COMPACT_FEEDBACK_CONTRACT,
                    raw_feedback_hash=fingerprint(feedback), model_input=self._model_input(payload)["proof"])
            return payload
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, StopIteration, AgentError, OverflowError) as exc:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409) from exc

    def _trace(self, record, claim):
        path = inside(self.core.settings.root, claim["repair_trace"])

        def write(event):
            raw = json.dumps({**event, "record_id": record["id"], "repair_claim": claim["id"], "at": _now(),
                **({"review_protocol_contract": claim["review_protocol_contract"]}
                   if "review_protocol_contract" in claim else {})}, ensure_ascii=False) + "\n"
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return write

    def _quarantine_proof(self, record, claim, *, saved=False):
        """Полный заведомо негодный ответ можно отложить; неизвестный запрос — нельзя."""
        code = claim.get("error")
        if code not in QUARANTINE_ERRORS:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        self.cycle._assert_pre_draft(record)
        snapshot = parse_json(inside(self.core.settings.root, claim["snapshot_path"]).read_text(encoding="utf-8"))
        self._files(record, claim, snapshot["article"])
        policy = self.cycle._policy_for_record(record)
        source, target = self.cycle._source_target(policy)
        if (self._prompts(claim["contract"], claim.get("review_rubric", "meaning-first-v1"),
                review_protocol_contract=claim.get("review_protocol_contract")) != claim["prompts"] or fingerprint(policy) != claim["policy_hash"]
                or fingerprint(source) != claim["source_config_hash"] or target.binding_hash != claim["target_binding_hash"]):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        with self.core.db.session() as session:
            self._quiet_session(session)
        raw = inside(self.core.settings.root, claim["repair_trace"]).read_bytes()
        if not 0 < len(raw) <= 512 * 1024:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        events = [parse_json(line) for line in raw.decode("utf-8").splitlines()]
        expected = {"contract": claim["contract"], "model": claim["model"],
            "prompt_hash": claim["prompts"][feedback_prompt_name(claim["contract"], claim.get("feedback_contract"))], "input_hash": claim["input_hash"],
            "old_candidate_hash": fingerprint(snapshot["candidate"]),
            "old_replay_hash": repair_replay_hash(snapshot["candidate"]),
            "feedback_hash": fingerprint(snapshot["feedback"])}
        expected.update(presentation_binding(claim.get("presentation_contract"), claim.get("image_credit")))
        expected.update(self._feedback_binding(claim))
        if claim["contract"] in {"article-repair-v6", "article-repair-v7", "article-repair-v8"}:
            expected["reference_contract"] = self._claim_reference(claim)
        article = snapshot["article"]
        expected_context = repair_context(article, snapshot["candidate"], snapshot["feedback"],
                                          contract=claim["contract"], feedback_contract=claim.get("feedback_contract"))
        if ([e.get("phase") for e in events] != ["repair_started", "repair_returned", "repair_failed"]
                or any(e.get("record_id") != record["id"] or e.get("repair_claim") != claim["id"]
                       or e.get("review_protocol_contract") != claim.get("review_protocol_contract")
                       or e.get("attempt") != 1 or e.get("binding") != expected for e in events)
                or any(type(e.get("at")) not in {int, float} or not math.isfinite(e["at"])
                       or e["at"] < claim["started_at"] for e in events)
                or [e["at"] for e in events] != sorted(e["at"] for e in events)
                or events[0].get("context") != expected_context
                or claim.get("feedback_contract") is not None and (
                    events[0].get("raw_feedback") != snapshot["feedback"]
                    or events[0].get("model_input") != claim["model_input"])
                or claim.get("finished_at") is not None and (
                    type(claim["finished_at"]) not in {int, float}
                    or not math.isfinite(claim["finished_at"])
                    or any(e["at"] > claim["finished_at"] for e in events))
                or events[-1].get("error") != code or not isinstance(events[1].get("raw"), str)
                or not events[1]["raw"].strip() or len(events[1]["raw"].encode("utf-8")) > 128 * 1024):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        # Повторяем только чистую проверку сохранённого ответа. Редактор ниже
        # возвращает уже записанный raw: модели, HTTP и trace writer отсутствуют.
        # Одной надписи repair_failed недостаточно для снятия блокировки claim.
        from .article_repair import ArticleRepair
        if (claim["contract"] not in REPAIR_CONTRACTS or claim["model"] != REPAIR_MODEL
                or fingerprint(self._payload(record)) != fingerprint(snapshot)):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)

        class SavedResponse:
            model = REPAIR_MODEL

            def generate_text(self, _prompt):
                return events[1]["raw"], {}

        replay = ArticleRepair(SavedResponse(), prompt_dir=self.core.settings.root / "prompts",
                               contract=claim["contract"],
                               **presentation_binding(claim.get("presentation_contract"), claim.get("image_credit")))
        try:
            replay.propose(article, snapshot["candidate"], snapshot["feedback"],
                rejected_replay_hashes=snapshot["rejected_replay_hashes"], **self._feedback_kwargs(claim))
        except AgentError as exc:
            if exc.code != code:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409) from exc
        else:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
        if saved:
            with self.core.db.session() as session:
                audit = session.get(Audit, str(uuid5(NAMESPACE_URL, claim["id"] + ":quarantine")))
                if (not audit or audit.action != "automatic_article_repair_quarantined"
                        or audit.actor != "automatic_article_repair" or audit.entity != record["id"]
                        or audit.details != {"claim_id": claim["id"], "error": code, "trace_hash": _sha(raw),
                                             "snapshot_hash": claim["snapshot_hash"]}):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        return _sha(raw)

    def _review_protocol_guard_proof(self, record, claim, *, saved=False):
        """Завершённый неверный Min-response после Qwen не становится новым запросом."""
        from .article_quality import REVIEW_BOUNDS_V2_CONTRACT
        from .completed_review import completed_review_proof
        code = "ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID"
        history, blocked = self._history(record)
        expected_state = "not_repairable" if saved else "checking"
        if (record.get("review_protocol_contract") != REVIEW_BOUNDS_V2_CONTRACT
                or claim.get("review_protocol_contract") != REVIEW_BOUNDS_V2_CONTRACT
                or not history or history[-1] != claim or len(history) > MAX_AUTOMATIC_REPAIRS
                or any(previous.get("state") != "rejected" for previous in history[:-1])
                or blocked != (None if saved else "IN_PROGRESS")
                or claim.get("state") != expected_state or claim.get("model") != REPAIR_MODEL
                or record.get("state") != "needs_attention" or record.get("error") != "ARTICLE_QUALITY_REJECTED"
                or saved and claim.get("outcome_origin") != "review_protocol_guard"
                or not saved and claim.get("outcome_origin") is not None):
            raise AgentError(code, 409)
        self.cycle._assert_pre_draft(record)
        snapshot = parse_json(inside(self.core.settings.root, claim["snapshot_path"]).read_text(encoding="utf-8"))
        proposal = parse_json(inside(self.core.settings.root, claim["proposal_path"]).read_text(encoding="utf-8"))
        candidate = proposal.get("candidate")
        if (proposal.get("repairable") is False or proposal.get("evidence", {}).get("outcome") != "candidate"
                or not isinstance(candidate, dict)
                or fingerprint(candidate) != claim.get("candidate_hash")
                or self._prompts(claim["contract"], claim.get("review_rubric", "meaning-first-v1"),
                    review_protocol_contract=claim.get("review_protocol_contract")) != claim["prompts"]
                or fingerprint(self.cycle._policy_for_record(record)) != claim["policy_hash"]):
            raise AgentError(code, 409)
        self._files(record, claim, snapshot["article"], candidate)
        proof = completed_review_proof(self.cycle, record, snapshot["article"], require_anchor=True)
        if saved:
            details = {"claim_id": claim["id"], "candidate_hash": claim["candidate_hash"], "proof_hash": fingerprint(proof)}
            with self.core.db.session() as session:
                audit = session.get(Audit, str(uuid5(NAMESPACE_URL, claim["id"] + ":review-protocol-quarantine")))
                if (claim.get("completed_review_protocol") != proof
                        or claim.get("completed_review_protocol_hash") != fingerprint(proof)
                        or not audit or audit.actor != "automatic_article_repair"
                        or audit.action != "automatic_article_review_protocol_quarantined"
                        or audit.entity != record["id"] or audit.details != details):
                    raise AgentError(code, 409)
        return proof

    def quarantine_failed_reference(self, ident, reason):
        """Адресный одноразовый карантин старого полного отказа по потере ссылки.

        Ничего не переводит и не отправляет. Неизвестный исход, другое нарушение
        или потерянный durable claim сохраняют блокировку. Запуск возможен только
        на общей паузе; исходные failed-поля и доказательства остаются в истории.
        """
        return self._quarantine_failed(ident, reason, profile="reference")

    def quarantine_failed_number(self, ident, reason):
        """Адресный карантин полного ответа с доказанным нарушением чисел."""
        return self._quarantine_failed(ident, reason, profile="number")

    def quarantine_failed_latin_mask(self, ident, reason):
        """Отложить полный ответ с подменой алфавита, сохранив прежний failed claim.

        Чистый replay обязан подтвердить тот же запрет. Нового перевода, QA,
        согласования и отправки нет; неполный или неизвестный исход не снимается.
        """
        return self._quarantine_failed(ident, reason, profile="latin_mask")

    def _quarantine_failed(self, ident, reason, *, profile):
        from .server_models import EditorialControl
        from .server_state import CONTROL_ID
        codes = {"reference": "TRANSLATION_REFERENCE_CHANGED", "number": "TRANSLATION_NUMBER_CHANGED",
                 "latin_mask": "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"}
        if profile not in codes:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_NOT_SAFE", 409)
        code = codes[profile]
        error = "ARTICLE_AUTOMATIC_REPAIR_" + profile.upper() + "_QUARANTINE_NOT_SAFE"
        if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_REASON_REQUIRED", 409)
        reason = reason.strip()
        with profile_lock(self.cycle.directory / "cycle.lock"):
            ledger = self.cycle._load()
            record = self.cycle._record(ledger, ident)
            history, blocked = self._history(record)
            if (blocked != "FAILED" or not history or record.get("state") != "needs_attention"
                    or record.get("error") != code
                    or any(claim.get("state") != "rejected" for claim in history[:-1])):
                raise AgentError(error, 409)
            claim = history[-1]
            if (claim.get("state") != "failed" or claim.get("error") != code
                    or claim.get("outcome_origin") is not None
                    or any(claim.get(key) is not None for key in ("proposal_path", "candidate_hash"))
                    or any(type(claim.get(key)) not in {int, float} or not math.isfinite(claim[key])
                           for key in ("started_at", "finished_at"))
                    or claim["finished_at"] < claim["started_at"]
                    or record.get("policy_hash") != claim["policy_hash"]
                    or record.get("input_hash") != claim["input_hash"]
                    or record.get("slot") != claim["slot"]):
                raise AgentError(error, 409)
            policy = self.cycle._policy_for_record(record)
            if (policy.get("approval_mode") != "automatic"
                    or record.get("target_id") != policy["target_id"]
                    or record.get("source_id") != policy["source_id"]):
                raise AgentError(error, 409)
            key = str(uuid5(NAMESPACE_URL, claim["id"] + ":quarantine"))
            receipt = str(uuid5(NAMESPACE_URL, claim["id"] + ":" + profile + "-quarantine"))
            with self.core.db.session() as session:
                control = session.get(EditorialControl, CONTROL_ID)
                if control is None or not control.paused:
                    raise AgentError("EDITORIAL_PAUSE_BEFORE_QUARANTINE", 409)
                if session.get(Audit, key) or session.get(Audit, receipt):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_ALREADY_ATTEMPTED", 409)
            hashed = self._quarantine_proof(record, claim)
            previous = copy.deepcopy(claim)
            action = {"at": _now(), "action": "quarantine_failed_" + profile, "reason": reason,
                "claim_id": claim["id"], "previous_record_state": record["state"],
                "previous_record_error": record["error"], "previous_claim_hash": fingerprint(previous),
                "trace_hash": hashed, "snapshot_hash": claim["snapshot_hash"]}
            try:
                with self.core.db.session.begin() as session:
                    # Control блокируется до фиксации claims: параллельное start
                    # не может разрешить отправку во время адресного восстановления.
                    control = session.scalar(select(EditorialControl).where(
                        EditorialControl.id == CONTROL_ID).with_for_update())
                    if control is None or not control.paused:
                        raise AgentError("EDITORIAL_PAUSE_BEFORE_QUARANTINE", 409)
                    self._quiet_session(session)
                    session.add(Audit(id=key, actor="automatic_article_repair",
                        action="automatic_article_repair_quarantined", entity=ident,
                        details={"claim_id": claim["id"], "error": claim["error"], "trace_hash": hashed,
                                 "snapshot_hash": claim["snapshot_hash"]}))
                    session.add(Audit(id=receipt, actor="automatic_article_repair",
                        action="automatic_article_repair_" + profile + "_quarantine", entity=ident,
                        details={**action, "previous_failure": previous}))
            except IntegrityError as exc:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_ALREADY_ATTEMPTED", 409) from exc
            # Audit PK уже durable. Неуспешный save не разрешает повтор операции.
            claim.update(state="not_repairable", outcome_origin="protocol_guard",
                repair_trace_hash=hashed, quarantine_previous_failure=previous,
                quarantined_at=action["at"], quarantine_reason=reason)
            record.setdefault("preparation_actions", []).append(action)
            record.update(state="needs_attention", error="ARTICLE_QUALITY_REJECTED")
            self.cycle._save(ledger)
            return {"id": ident, "claim_id": claim["id"], "state": "not_repairable", "trace_hash": hashed}

    def authorize(self, record, claim_id, candidate, article):
        """Проверка перехода в штатный prepare после освобождения cycle.lock."""
        history, blocked = self._history(record)
        if blocked and not (blocked == "IN_PROGRESS" and claim_id):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + blocked, 409)
        active = history and history[-1].get("state") in {"correcting", "checking", "unknown", "failed"}
        if claim_id is None:
            if active:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_IN_PROGRESS", 409)
            return None
        if (not history or history[-1].get("id") != claim_id or history[-1].get("state") != "checking"):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        claim = history[-1]
        with self.core.db.session() as session:
            audit = session.get(Audit, claim_id)
            if not audit or any(claim.get(key) != audit.details.get(key) for key in _BINDING):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        if (fingerprint(article) != claim["input_hash"] or fingerprint(candidate) != claim.get("candidate_hash")
                or self._prompts(claim["contract"], claim.get("review_rubric", "meaning-first-v1"),
                    review_protocol_contract=claim.get("review_protocol_contract")) != claim["prompts"]
                or _sha(inside(self.core.settings.root, record["quality_trace"]).read_bytes()) != claim["parent_trace_hash"]):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        self._files(record, claim, article, candidate)
        self._quiet(record, self.cycle._policy_for_record(record))
        return claim

    def _files(self, record, claim, article, candidate=None):
        snapshot_raw = inside(self.core.settings.root, claim["snapshot_path"]).read_bytes()
        snapshot = parse_json(snapshot_raw.decode("utf-8"))
        references = self._claim_reference(claim)
        presentation = presentation_binding(record.get("presentation_contract"), record.get("image_credit"))
        if (_sha(snapshot_raw) != claim["snapshot_hash"] or snapshot["article"] != article
                or presentation_binding(snapshot.get("presentation_contract"), snapshot.get("image_credit")) != presentation
                or presentation_binding(claim.get("presentation_contract"), claim.get("image_credit")) != presentation
                or snapshot.get("review_rubric") != record.get("review_rubric")
                or claim.get("review_rubric") != record.get("review_rubric")
                or snapshot.get("review_protocol_contract") != record.get("review_protocol_contract")
                or claim.get("review_protocol_contract") != record.get("review_protocol_contract")
                or snapshot.get("scientific_spelling_contract") != record.get("scientific_spelling_contract")
                or snapshot.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != references
                or record.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != references):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        from .article_quality import HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
        compact = record.get("review_rubric") == HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
        if compact:
            expected = self._model_input(snapshot)["proof"]
            if (snapshot.get("feedback_contract") != COMPACT_FEEDBACK_CONTRACT
                    or claim.get("feedback_contract") != COMPACT_FEEDBACK_CONTRACT
                    or snapshot.get("raw_feedback_hash") != fingerprint(snapshot["feedback"])
                    or claim.get("raw_feedback_hash") != snapshot["raw_feedback_hash"]
                    or snapshot.get("model_input") != expected or claim.get("model_input") != expected
                    or not expected["fits"]):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        elif any(key in value for value in (snapshot, claim) for key in ("feedback_contract", "raw_feedback_hash", "model_input")):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        if record.get("input_file"):
            original = inside(self.core.settings.root, record["input_file"]).read_bytes()
            if _sha(original) != record["input_file_hash"] or parse_json(original.decode("utf-8")) != article:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        if claim.get("proposal_path"):
            raw = inside(self.core.settings.root, claim["proposal_path"]).read_bytes()
            proposal = parse_json(raw.decode("utf-8"))
            if (_sha(raw) != claim["proposal_hash"] or proposal["evidence"] != claim["evidence"]
                    or candidate is not None and proposal["candidate"] != candidate
                    or fingerprint(proposal["candidate"]) != claim["candidate_hash"]):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        if claim.get("repair_trace_hash"):
            raw = inside(self.core.settings.root, claim["repair_trace"]).read_bytes()
            if _sha(raw) != claim["repair_trace_hash"]:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        trace = inside(self.core.settings.root, record["quality_trace"]).read_bytes()
        # Новая проверка только дописывает события; прежний отказ остаётся побайтно.
        prefix = trace[:claim["parent_trace_bytes"]]
        if _sha(prefix) != claim["parent_trace_hash"]:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)

    def assert_complete(self, record, session):
        """Незавершённая правка не получает policy approval даже при известном DRAFT."""
        history, blocked = self._history(record)
        if blocked or history and history[-1]["state"] != "passed":
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + (blocked or "IN_PROGRESS"), 409)
        if not history:
            return
        claim = history[-1]
        rows = list(session.scalars(select(Audit).where(Audit.entity == record["id"],
            Audit.actor == "automatic_article_repair", Audit.action == "automatic_article_repair_passed")))
        if not any(isinstance(row.details, dict) and row.details.get("claim_id") == claim["id"]
                   and row.details.get("prepared_hash") == claim.get("prepared_hash") for row in rows):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        snapshot = parse_json(inside(self.core.settings.root, claim["snapshot_path"]).read_text(encoding="utf-8"))
        self._files(record, claim, snapshot["article"])

    def after_quality(self, record, claim, article):
        source, target = self._quiet(record, self.cycle._policy_for_record(record))
        if (self._prompts(claim["contract"], claim.get("review_rubric", "meaning-first-v1"),
                review_protocol_contract=claim.get("review_protocol_contract")) != claim["prompts"] or fingerprint(article) != claim["input_hash"]
                or target.binding_hash != claim["target_binding_hash"] or fingerprint(source) != claim["source_config_hash"]):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
        self._files(record, claim, article)

    def _matching(self, ledger, ident, claim_id):
        record = self.cycle._record(ledger, ident)
        history = record.get("automatic_repair_attempts", [])
        if not history or history[-1].get("id") != claim_id:
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        with self.core.db.session() as session:
            audit = session.get(Audit, claim_id)
            if not audit or any(history[-1].get(key) != audit.details.get(key) for key in _BINDING):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        return record, history[-1]

    def record_input_budget(self, ident, expected):
        """Полный известный отказ остаётся; это не editor claim и не model retry."""
        with profile_lock(self.cycle._preparation_lock()):
            record = self.cycle._record(self.cycle._load(), ident)
            status = self.status(record)  # Неизвестные/потерянные claims проверяются первыми.
            if status["reason"] != "MODEL_INPUT_BUDGET" or status.get("model_input") != expected:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
            payload = self._payload(record)
            self._quiet(record, self.cycle._policy_for_record(record))
            for claim in record.get("automatic_repair_attempts", []):
                self._files(record, claim, payload["article"])
            relative = f"data/hourly-articles/repair-budget-{ident}.local.json"
            hashed = _write_json(inside(self.core.settings.root, relative), payload)
            audit_id = str(uuid5(NAMESPACE_URL, "max-postiz:model-input-budget:" + ident))
            details = {"feedback_contract": COMPACT_FEEDBACK_CONTRACT, "snapshot_path": relative,
                "snapshot_hash": hashed, "model_input": expected, "parent_trace_hash": payload["parent_trace_hash"],
                "input_hash": record["input_hash"], "policy_hash": record["policy_hash"],
                "editor_claim_created": False, "model_request_sent": False}
            with self.core.db.session.begin() as session:
                require_running(self.core, session, lock=True)
                self._quiet_session(session)
                audit = session.get(Audit, audit_id)
                if audit is not None:
                    if (audit.actor != "automatic_article_repair" or audit.entity != ident
                            or audit.action != "automatic_article_repair_model_input_budget" or audit.details != details):
                        raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
                else:
                    session.add(Audit(id=audit_id, actor="automatic_article_repair",
                        action="automatic_article_repair_model_input_budget", entity=ident, details=details))
            return details

    def run(self, ident):
        cycle, editor, claim = self.cycle, None, None
        with profile_lock(cycle._preparation_lock()):
            ledger = cycle._load()
            record = cycle._record(ledger, ident)
            if cycle.reserve_mode:
                cycle._reserve_record(record, active=True)
            policy = cycle._policy_for_record(record)
            status = self.status(record)
            if not status["eligible"]:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + status["reason"], 409)
            source, target = self._quiet(record, policy)
            payload = self._payload(record)
            if payload.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != repair_reference_contract(REPAIR_CONTRACT):
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
            cycle._article(payload["article"], policy, source, target)
            cycle._protocol_source_novel(record, ledger, payload["article"])
            number = status["attempts"] + 1
            claim_id = _claim_id(ident, number)
            path = f"data/hourly-articles/repair-input-{claim_id}.local.json"
            claim = {"id": claim_id, "number": number, "state": "correcting", "started_at": _now(),
                "contract": REPAIR_CONTRACT, "model": REPAIR_MODEL, "slot": record["slot"],
                "input_hash": record["input_hash"], "policy_hash": record["policy_hash"],
                "parent_trace_hash": payload["parent_trace_hash"], "snapshot_path": path,
                "parent_trace_bytes": len(inside(self.core.settings.root, record["quality_trace"]).read_bytes()),
                "snapshot_hash": _write_json(inside(self.core.settings.root, path), payload),
                "prompts": self._prompts(REPAIR_CONTRACT, record.get("review_rubric", "meaning-first-v1"),
                    review_protocol_contract=record.get("review_protocol_contract")), "target_binding_hash": target.binding_hash,
                "source_config_hash": fingerprint(source),
                "repair_trace": f"data/hourly-articles/repair-{claim_id}.local.jsonl",
                "owner_authorization": "2026-10-22 bounded automatic translation corrections"}
            claim.update(presentation_binding(record.get("presentation_contract"), record.get("image_credit")))
            if REPAIR_CONTRACT in {"article-repair-v6", "article-repair-v7", "article-repair-v8"}:
                claim["reference_contract"] = payload["reference_contract"]
            if "review_rubric" in record:
                claim["review_rubric"] = record["review_rubric"]
            if "review_protocol_contract" in record:
                claim["review_protocol_contract"] = record["review_protocol_contract"]
            if payload.get("feedback_contract") is not None:
                claim.update({key: copy.deepcopy(payload[key]) for key in ("feedback_contract", "raw_feedback_hash", "model_input")})
            try:
                with self.core.db.session.begin() as session:
                    require_running(self.core, session, lock=True)
                    self._quiet_session(session)
                    session.add(Audit(id=claim_id, actor="automatic_article_repair", action="automatic_article_repair_claim",
                                      entity=ident, details=copy.deepcopy(claim)))
            except IntegrityError as exc:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409) from exc
            # PK уже зафиксирован: даже падение следующего save не разрешает повтор LLM.
            record.setdefault("automatic_repair_attempts", []).append(claim)
            record["state"] = "preparing"
            cycle._save(ledger)
            expected = fingerprint(record)
            try:
                editor = cycle._repair(self._trace(record, claim),
                    **presentation_binding(claim.get("presentation_contract"), claim.get("image_credit")))
                if (editor.contract != claim["contract"]
                        or editor.prompt_hash != claim["prompts"][repair_prompt_name(claim["contract"])]):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
                proposal = editor.propose(payload["article"], payload["candidate"], payload["feedback"],
                    rejected_replay_hashes=payload["rejected_replay_hashes"], **self._feedback_kwargs(claim))
                fresh = cycle._load()
                if fingerprint(cycle._record(fresh, ident)) != expected:
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
                # _load обновляет CAS revision. Сохраняем свежий ledger целиком,
                # чтобы не стереть изменения других записей старым снимком.
                ledger = fresh
                record, claim = self._matching(ledger, ident, claim_id)
                if (_sha(inside(self.core.settings.root, record["quality_trace"]).read_bytes()) != claim["parent_trace_hash"]
                        or _sha(inside(self.core.settings.root, path).read_bytes()) != claim["snapshot_hash"]):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED", 409)
                self.after_quality(record, claim, payload["article"])
                repair_raw = inside(self.core.settings.root, claim["repair_trace"]).read_bytes()
                repair_events = [parse_json(line) for line in repair_raw.decode("utf-8").splitlines()]
                terminal = "repair_not_repairable" if proposal.get("repairable") is False else "repair_candidate"
                expected_binding = {"contract": claim["contract"], "model": REPAIR_MODEL,
                    "prompt_hash": claim["prompts"][feedback_prompt_name(claim["contract"], claim.get("feedback_contract"))], "input_hash": claim["input_hash"],
                    "old_candidate_hash": fingerprint(payload["candidate"]),
                    "old_replay_hash": repair_replay_hash(payload["candidate"]),
                    "feedback_hash": fingerprint(payload["feedback"])}
                expected_binding.update(presentation_binding(claim.get("presentation_contract"), claim.get("image_credit")))
                expected_binding.update(self._feedback_binding(claim))
                if claim["contract"] in {"article-repair-v6", "article-repair-v7", "article-repair-v8"}:
                    expected_binding["reference_contract"] = self._claim_reference(claim)
                times = [e.get("at") for e in repair_events]
                if (not 0 < len(repair_raw) <= 512 * 1024
                        or [e.get("phase") for e in repair_events] != ["repair_started", "repair_returned", terminal]
                        or any(e.get("record_id") != ident or e.get("repair_claim") != claim_id
                               or e.get("review_protocol_contract") != claim.get("review_protocol_contract")
                               or e.get("attempt") != 1 for e in repair_events)
                        or repair_events[-1].get("result") != proposal
                        or repair_events[1].get("raw") != proposal.get("raw")
                        or _sha(proposal["raw"].encode("utf-8")) != proposal["evidence"].get("raw_sha256")
                        or proposal["evidence"].get("input_hash") != claim["input_hash"]
                        or proposal["evidence"].get("model") != REPAIR_MODEL
                        or proposal["evidence"].get("prompt_hash") != claim["prompts"][feedback_prompt_name(claim["contract"], claim.get("feedback_contract"))]
                        or any(type(t) not in {int, float} or not math.isfinite(t) or t < claim["started_at"] for t in times)
                        or times != sorted(times)
                        or repair_events[0].get("binding") != expected_binding
                        or repair_events[1].get("binding") != expected_binding
                        or any(proposal["evidence"].get(k) != v for k, v in expected_binding.items())
                        or repair_events[0].get("context") != repair_context(payload["article"],
                            payload["candidate"], payload["feedback"], contract=claim["contract"],
                            feedback_contract=claim.get("feedback_contract"))
                        or claim.get("feedback_contract") is not None and (
                            repair_events[0].get("raw_feedback") != payload["feedback"]
                            or repair_events[0].get("model_input") != claim["model_input"])):
                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID", 409)
                proposal_path = f"data/hourly-articles/repair-proposal-{claim_id}.local.json"
                claim.update(proposal_path=proposal_path,
                    proposal_hash=_write_json(inside(self.core.settings.root, proposal_path), proposal),
                    repair_trace_hash=_sha(repair_raw), evidence=proposal["evidence"])
                if proposal.get("repairable") is False:
                    claim.update(state="not_repairable", reason=proposal["reason"], evidence=proposal["evidence"], finished_at=_now())
                    record["state"] = "needs_attention"
                    cycle._save(ledger)
                    raise AgentError("ARTICLE_QUALITY_REJECTED", 409)
                candidate = proposal["candidate"]
                cycle._validate_correction(candidate)
                cycle._guard_candidate_replay(ledger, record, candidate, "automatic_repair", payload["article"])
                claim.update(state="checking", candidate_hash=fingerprint(candidate),
                             proposal_path=proposal_path, proposal_hash=_write_json(inside(self.core.settings.root, proposal_path), proposal),
                             evidence=proposal["evidence"])
                record["state"] = "needs_attention"
                cycle._save(ledger)
            except Exception as exc:
                if claim["state"] not in {"not_repairable", "no_change"}:
                    code = exc.code if isinstance(exc, AgentError) else "ARTICLE_AUTOMATIC_REPAIR_INTERRUPTED"
                    safe_no_change = code in {"ARTICLE_REPAIR_UNCHANGED", "ARTICLE_REPAIR_PREVIOUSLY_REJECTED", "ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"}
                    fresh = cycle._load()
                    current, current_claim = self._matching(fresh, ident, claim_id)
                    if fingerprint(current) == expected:
                        current_claim.update(error=code)
                        quarantined = False
                        if code in QUARANTINE_ERRORS:
                            try:
                                hashed = self._quarantine_proof(current, current_claim)
                                with self.core.db.session.begin() as session:
                                    session.add(Audit(id=str(uuid5(NAMESPACE_URL, claim_id + ":quarantine")),
                                        actor="automatic_article_repair", action="automatic_article_repair_quarantined",
                                        entity=ident, details={"claim_id": claim_id, "error": code, "trace_hash": hashed,
                                                               "snapshot_hash": current_claim["snapshot_hash"]}))
                                current_claim.update(outcome_origin="protocol_guard", repair_trace_hash=hashed,
                                    reason="Completed invalid editor response; original rejection retained")
                                quarantined = True
                            except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError):
                                pass
                        current_claim.update(state="not_repairable" if quarantined else "no_change" if safe_no_change else "failed",
                                             finished_at=_now())
                        current.update(state="needs_attention", error="ARTICLE_QUALITY_REJECTED" if safe_no_change or quarantined else code)
                        cycle._save(fresh)
                    # Чужой изменённый ledger не перезаписываем старым снимком.
                    # Незавершённый Audit claim продолжает блокировать повтор.
                raise
            finally:
                if editor is not None and not cycle.repair_factory:
                    editor.editor.client.close()
        # Новый lock и повторная проверка claim обязательны: flock не reentrant.
        try:
            result = cycle.prepare(payload["article"], retry_record=ident, candidate=candidate, automatic_repair_claim=claim_id)
        except Exception as exc:
            with profile_lock(cycle._preparation_lock()):
                ledger = cycle._load()
                record, claim = self._matching(ledger, ident, claim_id)
                code = exc.code if isinstance(exc, AgentError) else "ARTICLE_AUTOMATIC_REPAIR_INTERRUPTED"
                from .server_cycle import ServerCycle
                known = code == "ARTICLE_QUALITY_REJECTED" and ServerCycle(self.core, cycle=cycle)._known_rejection(record)
                if known and record.get("review_protocol_contract") is not None:
                    # Общий classifier может распознать завершённый protocol отказ.
                    # Обычный rejected здесь требует именно полного semantic replay.
                    try:
                        self._payload(record)
                    except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                        known = False
                protocol = None
                if code == "ARTICLE_QUALITY_REJECTED" and record.get("review_protocol_contract") is not None:
                    try:
                        protocol = self._review_protocol_guard_proof(record, claim)
                        details = {"claim_id": claim["id"], "candidate_hash": claim["candidate_hash"],
                                   "proof_hash": fingerprint(protocol)}
                        with self.core.db.session.begin() as session:
                            audit_id = str(uuid5(NAMESPACE_URL, claim["id"] + ":review-protocol-quarantine"))
                            audit = session.get(Audit, audit_id)
                            if audit is not None:
                                if (audit.actor != "automatic_article_repair"
                                        or audit.action != "automatic_article_review_protocol_quarantined"
                                        or audit.entity != ident or audit.details != details):
                                    raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
                            else:
                                session.add(Audit(id=audit_id, actor="automatic_article_repair",
                                    action="automatic_article_review_protocol_quarantined", entity=ident, details=details))
                    except (AgentError, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                        protocol = None
                claim.update(state="not_repairable" if protocol is not None else "rejected" if known else "failed",
                             error=code, finished_at=_now())
                if protocol is not None:
                    claim.update(outcome_origin="review_protocol_guard", completed_review_protocol=copy.deepcopy(protocol),
                        completed_review_protocol_hash=fingerprint(protocol),
                        reason="Completed invalid independent review; original candidate and rejection retained")
                cycle._save(ledger)
            raise
        with profile_lock(cycle._preparation_lock()):
            ledger = cycle._load()
            record, claim = self._matching(ledger, ident, claim_id)
            claim.update(state="passed", finished_at=_now(), prepared_hash=record["prepared_hash"])
            cycle._save(ledger)
            with self.core.db.session.begin() as session:
                self.core.db.audit(session, "automatic_article_repair", "automatic_article_repair_passed", ident,
                                   claim_id=claim_id, prepared_hash=record["prepared_hash"])
        return result
