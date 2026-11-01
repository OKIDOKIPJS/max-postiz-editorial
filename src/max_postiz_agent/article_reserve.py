"""Долговечные claims резерва и назначения доставки без сетевых операций.

Каждый claim живёт до известного завершения. Таймаут, рестарт и потеря владельца
не освобождают модели или адресата и не дают права повторить внешний запрос.
"""
import copy
import hashlib
import math
import re
from collections import Counter
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .article_attribution import illustrated_presentation
from .config import AgentError, fingerprint
from .db import Audit, now, uid
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
from .server_models import EditorialControl, EditorialLedger, EditorialPreparationClaim, PublicationSlot
from .server_state import CONTROL_ID, LEDGER_ID, require_running

RESERVE_CONTRACT = "article-reserve-v1"
MASS_RELATION_CONTRACT = "milky-way-small-mass-ratio-inversion-v1"
KNOWN_IMAGE_REJECTIONS = frozenset({
    "ARTICLE_IMAGE_REQUIRED", "ARTICLE_IMAGE_SOURCE_NOT_SUPPORTED", "ARTICLE_IMAGE_RIGHTS_REQUIRED",
    "ARTICLE_IMAGE_DOWNLOAD_REQUIRED", "ARTICLE_IMAGE_QUALITY_REQUIRED", "ARTICLE_IMAGE_SIZE_INVALID",
    "ARTICLE_IMAGE_FORMAT_INVALID", "ARTICLE_IMAGE_SOURCE_UNAVAILABLE", "IMAGE_FORMAT_INVALID", "IMAGE_SIZE_INVALID",
})
ACTIVE_SLOT_STATES = {"assigned", "pending", "blocked_unknown", "delivered"}
_IDENTITY = ("id", "target_id", "source_id", "url", "original_hash", "input_hash", "policy_hash",
             "source_published_at", "variant_id", "reserve_contract", "reserve_prepared_hash")
_TRANSITIONS = {
    "assigned": {"assigned", "pending", "blocked_unknown", "delivered"},
    "pending": {"pending", "blocked_unknown", "delivered", "published"},
    "blocked_unknown": {"blocked_unknown", "delivered", "published"},
    "delivered": {"delivered", "published"},
    "published": {"published"},
    "missed": {"missed"},
}


def ready_for_policy(record, policy):
    """Прежний резерв сохраняется, но не заменяет обязательную новую иллюстрацию."""
    marker = policy.get("presentation_contract")
    return (record.get("state") == "quality_ready"
            and (marker is None or record.get("presentation_contract") == marker))


def mass_relation_finding(source, candidate):
    """Only the unique, explicit fraction -> percent-more Milky Way template."""
    if (not isinstance(source, dict) or not isinstance(candidate, dict)
            or not all(isinstance(source.get(k), str) for k in ("title", "body", "url"))
            or not all(isinstance(candidate.get(k), str) for k in ("title", "body"))):
        return None
    en, ru = source["body"], candidate["body"]
    def interval(decimal):
        number = rf"(?:0|[1-9][0-9]*)(?:{decimal}[0-9]+)?"
        return rf"(?P<lower>{number})\s*[-–—]\s*(?P<upper>{number})\s*%"

    end = r"(?=\s*(?:[,.;!?]|$))"
    english = re.compile(rf"\bDespite\s+being\s+only\s+{interval('[.]')}\s+as\s+massive\s+as\s+the\s+Milky\s+Way\s+galaxy\b{end}", re.I)
    russian = re.compile(rf"\bНесмотря\s+на\s+то\s*,\s*что\s+оно\s+только\s+на\s+{interval('[.,]')}\s+массивнее\s+Млечного\s+Пути\b{end}", re.I)
    left, right = list(english.finditer(en)), list(russian.finditer(ru))
    if len(left) != 1 or len(right) != 1:
        return None
    a, b = left[0], right[0]
    ranges = [tuple(Decimal(m[k].replace(",", ".")) for k in ("lower", "upper")) for m in (a, b)]
    if ranges[0] != ranges[1] or not 0 < ranges[0][0] <= ranges[0][1] < 100:
        return None

    def unambiguous(text, match, *, english):
        prefix, suffix = text[:match.start()], text[match.end():]
        start = max((prefix.rfind(c) for c in ".!?\n"), default=-1) + 1
        end = min((match.end() + i for i, c in enumerate(suffix) if c in ".!?\n"), default=len(text))
        clause = text[start:end].strip()
        if not clause.startswith(match[0]):
            return False
        # Quoted/meta/negated or multiple comparative clauses provide no proof.
        quoted = re.sub(r"(?<=[A-Za-zА-Яа-я])['’](?=[A-Za-zА-Яа-я])", "", clause)
        if any(c in quoted for c in "\"'‘’“”«»„‟"):
            return False
        exclusions = (r"\b(?:not|no|never|neither|nor|without|false|incorrect|if|whether|or|however|but)\b" if english
                      else r"\b(?:не|нет|ни|никогда|неверно|ошибочно|если|ли|или|якобы|но|однако)\b")
        return re.search(exclusions, clause, re.I) is None

    if (not unambiguous(en, a, english=True) or not unambiguous(ru, b, english=False)
            or len(re.findall(r"\bas\s+massive\s+as\b", en, re.I)) != 1
            or len(re.findall(r"\bмассивнее\b", ru, re.I)) != 1):
        return None
    return {"contract": MASS_RELATION_CONTRACT, "source_hash": fingerprint(source),
            "candidate_hash": fingerprint(candidate), "lower_percent": str(ranges[0][0]),
            "upper_percent": str(ranges[0][1]), "source_quote": a[0], "candidate_quote": b[0],
            "source_codepoint_span": [a.start(), a.end()], "candidate_codepoint_span": [b.start(), b.end()],
            "source_relation": "fraction_of_milky_way_mass", "candidate_relation": "percent_more_massive_than_milky_way"}


def _claim_view(row):
    return {key: getattr(row, key) for key in (
        "id", "token", "control_revision", "record_id", "state", "error", "created_at", "updated_at")}


def _slot_view(row, *, private=True):
    keys = ("id", "target_id", "slot_at", "record_id", "state", "due_at", "dispatch_id",
            "delivered_at", "error", "created_at", "updated_at", "text_hash", "evidence_hash")
    result = {key: getattr(row, key) for key in keys}
    if private:
        result.update(token=row.token, control_revision=row.control_revision, binding=copy.deepcopy(row.binding))
    return result


def _stamp(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _postiz_due_matches(observed, signed):
    """Postiz хранит миллисекунды; подписанная дата остаётся без округления."""
    # Верхняя граница — 10000-01-01 UTC, вне поддерживаемого ISO datetime.
    if not all(type(value) in {int, float} and 0 <= value < 253402300800
               and math.isfinite(value) for value in (observed, signed)):
        return False
    # На больших Unix timestamps float может представить ровно 1 мс как <1 мс.
    return abs(Decimal(str(observed)) - Decimal(str(signed))) < Decimal("0.001")


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class ArticleReserveStore:
    def __init__(self, core):
        self.core = core

    def _control(self, session, *, running=True):
        if running:
            require_running(self.core, session, lock=True)
        # Уже подтверждённая доставка — факт, а не разрешение отправки. Её
        # транзакция держит Variant/Target и не берёт Control в обратном порядке.
        row = (session.scalar(select(EditorialControl).where(
            EditorialControl.id == CONTROL_ID).with_for_update()) if running
            else session.get(EditorialControl, CONTROL_ID))
        if row is None:
            raise AgentError("EDITORIAL_NOT_INITIALIZED", 409)
        return row

    def begin_preparation(self, expected_revision=None):
        """Сначала записать claim, только затем выбирать источник и вызывать модели."""
        try:
            with self.core.db.session.begin() as session:
                control = self._control(session)
                if expected_revision is not None and (
                        type(expected_revision) is not int or control.revision != expected_revision):
                    raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
                if session.scalar(select(EditorialPreparationClaim).where(
                        EditorialPreparationClaim.active_key == "models")):
                    raise AgentError("ARTICLE_RESERVE_PREPARATION_ACTIVE", 409)
                row = EditorialPreparationClaim(id=uid(), token=uid(), control_revision=control.revision,
                    state="selecting", active_key="models")
                session.add(row)
                session.flush()
                self.core.db.audit(session, "article_reserve", "reserve_preparation_claim", row.id,
                    control_revision=control.revision, contract=RESERVE_CONTRACT)
                result = _claim_view(row)
            return result
        except IntegrityError as exc:
            raise AgentError("ARTICLE_RESERVE_PREPARATION_ACTIVE", 409) from exc

    def _preparation(self, session, claim, record_id=None, *, active=True):
        control = self._control(session)
        if (not isinstance(claim, dict)
                or not all(isinstance(claim.get(k), str) and claim[k] for k in ("id", "token"))):
            raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
        row = session.scalar(select(EditorialPreparationClaim).where(
            EditorialPreparationClaim.id == claim["id"]).with_for_update())
        if (row is None or row.token != claim["token"]
                or row.control_revision != claim.get("control_revision")
                or record_id is not None and row.record_id != record_id
                or active and (row.active_key != "models" or row.state not in {"selecting", "preparing"}
                               or row.control_revision != control.revision)
                or not active and (row.state not in {"quality_ready", "rejected"} or row.active_key is not None)):
            raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
        return row

    def require_preparation(self, claim, record_id=None, *, active=True, session=None):
        if session is None:
            with self.core.db.session.begin() as own:
                return self.require_preparation(claim, record_id, active=active, session=own)
        return _claim_view(self._preparation(session, claim, record_id, active=active))

    def bind_preparation(self, claim, record_id):
        if not isinstance(record_id, str) or not record_id:
            raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
        with self.core.db.session.begin() as session:
            row = self._preparation(session, claim)
            if row.record_id not in {None, record_id}:
                raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
            row.record_id, row.state, row.updated_at = record_id, "preparing", now()
            self.core.db.audit(session, "article_reserve", "reserve_preparation_bound", row.id,
                record_id=record_id, control_revision=row.control_revision)
            return _claim_view(row)

    def finish_preparation(self, claim, state, error=None):
        if state not in {"quality_ready", "rejected", "no_source", "blocked_unknown", "failed"}:
            raise AgentError("ARTICLE_RESERVE_CLAIM_STATE_INVALID", 409)
        with self.core.db.session.begin() as session:
            row = self._preparation(session, claim)
            if state in {"quality_ready", "rejected"} and row.record_id is None:
                raise AgentError("ARTICLE_RESERVE_CLAIM_LOST", 409)
            row.state, row.error, row.updated_at = state, error, now()
            # Инфраструктурная ошибка и неизвестный ответ не освобождают backend.
            if state in {"quality_ready", "rejected", "no_source"}:
                row.active_key = None
            self.core.db.audit(session, "article_reserve", "reserve_preparation_finished", row.id,
                record_id=row.record_id, state=state, error=error)
            return _claim_view(row)

    def active_preparation(self):
        with self.core.db.session() as session:
            row = session.scalar(select(EditorialPreparationClaim).where(
                EditorialPreparationClaim.active_key == "models"))
            return _claim_view(row) if row else None

    def quarantine_completed_review(self, claim, record_id, *, cycle):
        """Закрыть свой полный protocol refusal, не повторяя модель или статью.

        blocked_unknown снимается только положительным новым proof + Audit.
        Возраст, отсутствие HTTP-запросов сейчас и сам текст ошибки не доказательство.
        """
        from .completed_review import completed_review_proof, UNSAFE
        from .db import Material, Story

        def require(value):
            if not value:
                raise AgentError(UNSAFE, 409)

        require(cycle.core is self.core and cycle.reserve_mode and isinstance(claim, dict)
            and isinstance(record_id, str) and re.fullmatch(r"[a-f0-9-]{36}", record_id))
        with self.core.db.session.begin() as session:
            control = self._control(session)
            row = session.scalar(select(EditorialPreparationClaim).where(
                EditorialPreparationClaim.id == claim.get("id")).with_for_update())
            ledger = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            payload = copy.deepcopy(ledger.payload) if ledger else {}
            records = [r for r in payload.get("records", []) if isinstance(r, dict) and r.get("id") == record_id]
            record = records[0] if len(records) == 1 else {}
            require(row is not None and row.token == claim.get("token")
                and type(claim.get("control_revision")) is int
                and row.control_revision == claim["control_revision"] == control.revision
                and row.record_id == record_id and row.active_key == "models"
                and row.state in {"preparing", "blocked_unknown"}
                and row.error in {None, "ARTICLE_QUALITY_REJECTED", "ARTICLE_RECONCILIATION_REQUIRED"}
                and record.get("reserve_claim") == claim and record.get("reserve_contract") == RESERVE_CONTRACT
                and not record.get("protocol_guard") and not record.get("cancelled_at"))
            binding = {key: record.get(key) for key in ("id", "reserve_contract", "reserve_claim", "input_hash",
                "policy_hash", "target_id", "source_id", "original_hash", "source_published_at")}
            audit = session.get(Audit, str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record_id)))
            require(audit is not None and audit.actor == "article_reserve" and audit.entity == record_id
                and audit.action == "article_reserve_preparation_binding"
                and audit.details == {"binding": binding, "binding_hash": fingerprint(binding)}
                and record.get("reserve_binding_hash") == fingerprint(binding))
            policy = cycle._policy_for_record(record)
            require(policy.get("approval_mode") == "automatic" and fingerprint(policy) == record.get("policy_hash"))
            require(not any(record.get(key) is not None for key in ("variant_id", "postiz_id", "dispatch_id",
                "material_id", "story_id", "due_at", "prepared_hash", "reserve_prepared_hash", "publication_slot_id"))
                and not session.scalar(select(Story).where(Story.text_hash == fingerprint({"hourly_article": record_id})))
                and not session.scalar(select(Material).where(Material.url == record.get("url")))
                and not any(isinstance(v.metadata_json, dict) and v.metadata_json.get("record_id") == record_id
                    for v in session.scalars(select(Variant)))
                and not any(isinstance(d.snapshot, dict) and isinstance(d.snapshot.get("metadata"), dict)
                    and d.snapshot["metadata"].get("record_id") == record_id for d in session.scalars(select(Dispatch)))
                and not session.scalar(select(PublicationSlot).where(PublicationSlot.record_id == record_id)))
            # Чужая действующая внешняя мутация не закрывается этой операцией.
            require(not session.scalar(select(Dispatch).where(Dispatch.state.in_(["sending", "unknown"])))
                and not session.scalar(select(Dispatch).where(Dispatch.owner.is_not(None)))
                and not session.scalar(select(Dispatch).where(or_(Dispatch.lease_until != 0, Dispatch.lease_until.is_(None))))
                and not session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)))
                and not session.scalar(select(BridgePost).where(or_(BridgePost.mutation_until != 0, BridgePost.mutation_until.is_(None)))))
            proof = completed_review_proof(cycle, record, session=session)
            if proof["kind"] == "candidate":
                repair = record["automatic_repair_attempts"][-1]
                repair_audit = session.get(Audit, str(uuid5(NAMESPACE_URL, repair["id"] + ":review-protocol-quarantine")))
                require(repair.get("state") == "not_repairable" and repair.get("outcome_origin") == "review_protocol_guard"
                    and repair.get("completed_review_protocol") == proof
                    and repair.get("completed_review_protocol_hash") == fingerprint(proof)
                    and repair_audit is not None and repair_audit.actor == "automatic_article_repair"
                    and repair_audit.action == "automatic_article_review_protocol_quarantined"
                    and repair_audit.entity == record_id and repair_audit.details == {
                        "claim_id": repair["id"], "candidate_hash": repair["candidate_hash"], "proof_hash": fingerprint(proof)})
            # Re-read raw evidence before any mutation; the saved refusal stays byte-exact.
            require(completed_review_proof(cycle, record, session=session) == proof)
            stamp = now()
            disposition = {"contract": proof["contract"], "at": stamp, "proof_hash": fingerprint(proof),
                "trace_hash": proof["quality_trace_hash"], "raw_review_sha256": proof["raw_review_sha256"],
                "source_remains_used": True, "approval_permitted": False, "no_retry": True}
            record.update(state="cancelled", cancelled_at=stamp, protocol_guard=disposition)
            ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
            row.state, row.active_key, row.updated_at = "rejected", None, stamp
            self.core.db.audit(session, "article_reserve", "reserve_completed_review_quarantined", record_id,
                **disposition, claim_id=row.id, control_revision=control.revision,
                error_preserved=record.get("error"), model_calls=0, external_operations=0)
            return {"record_id": record_id, "claim_id": row.id, "state": "cancelled", "proof_hash": fingerprint(proof)}

    def finish_unbound_selection_on_pause(self, claim, *, expected_revision):
        """Пауза закрывает только выбор источника, ещё не привязавший запись.

        Control удерживается до commit: старый GET не сможет затем получить
        право bind_preparation и начать модель под прежним fence.
        """
        code = "ARTICLE_RESERVE_PAUSED_SELECTION_NOT_SAFE"
        if (not isinstance(claim, dict) or type(expected_revision) is not int
                or type(claim.get("control_revision")) is not int):
            raise AgentError(code, 409)
        with self.core.db.session.begin() as session:
            control = session.scalar(select(EditorialControl).where(
                EditorialControl.id == CONTROL_ID).with_for_update())
            row = session.scalar(select(EditorialPreparationClaim).where(
                EditorialPreparationClaim.id == claim.get("id")).with_for_update())
            ledger = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            records = ledger.payload.get("records") if ledger and isinstance(ledger.payload, dict) else None
            if (control is None or not control.paused or control.revision != expected_revision
                    or control.revision <= claim["control_revision"]
                    or row is None or row.token != claim.get("token")
                    or row.control_revision != claim["control_revision"] or row.record_id is not None
                    or row.state != "selecting" or row.active_key != "models" or row.error is not None
                    or not isinstance(records, list) or any(not isinstance(r, dict) for r in records)
                    or any(r.get("reserve_claim") is not None and (
                        not isinstance(r["reserve_claim"], dict) or r["reserve_claim"].get("id") == row.id)
                        for r in records)
                    or session.scalar(select(Audit).where(Audit.entity == row.id,
                        Audit.action == "reserve_preparation_bound"))):
                raise AgentError(code, 409)
            row.state, row.active_key, row.error, row.updated_at = "no_source", None, "EDITORIAL_PAUSED", now()
            self.core.db.audit(session, "article_reserve", "reserve_unbound_selection_paused", row.id,
                previous_revision=row.control_revision, control_revision=control.revision,
                error="EDITORIAL_PAUSED", model_calls=0, bound_records=0)
            return _claim_view(row)

    def reject_image_preparation(self, claim, record_id, error):
        """Атомарно закрыть только завершённый отказ изображения ДО первой модели.

        Нет retry GET, снятия model unknown, правки внешней очереди или старого
        источника. Отмена записи и освобождение claim фиксируются вместе.
        """
        from .db import Story
        code = "ARTICLE_IMAGE_REJECTION_NOT_SAFE"
        if (error not in KNOWN_IMAGE_REJECTIONS or not isinstance(record_id, str)
                or re.fullmatch(r"[A-Za-z0-9-]{1,80}", record_id) is None):
            raise AgentError(code, 409)
        with self.core.db.session.begin() as session:
            row = self._preparation(session, claim, record_id)
            ledger = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            payload = copy.deepcopy(ledger.payload) if ledger else {}
            records = [r for r in payload.get("records", []) if r.get("id") == record_id]
            record = records[0] if len(records) == 1 else {}
            attempts = record.get("preparation_attempts", [])
            attempt = attempts[0] if isinstance(attempts, list) and len(attempts) == 1 else {}
            if (not record or record.get("state") != "needs_attention" or record.get("error") != error
                    or not illustrated_presentation(record.get("presentation_contract"))
                    or record.get("reserve_contract") != RESERVE_CONTRACT
                    or record.get("reserve_claim") != claim or record.get("illustration") is not None
                    or record.get("model_result_unknown")
                    or any(record.get(k) for k in ("variant_id", "postiz_id", "dispatch_id", "due_at", "prepared_hash",
                        "automatic_repair_attempts", "automatic_corrections", "review_protocol_recoveries"))
                    or attempt.get("number") != 1 or attempt.get("state") != "needs_attention"
                    or attempt.get("error") != error or attempt.get("finished_at") is None
                    or attempt.get("image_preparation_state") != "failed"
                    or attempt.get("presentation_contract") != record.get("presentation_contract")
                    or attempt.get("model_started_at") is not None):
                raise AgentError(code, 409)
            expected = "data/hourly-articles/quality-" + record_id + ".local.jsonl"
            if record.get("quality_trace") != expected:
                raise AgentError(code, 409)
            trace = self.core.settings.root / expected
            if trace.exists() or trace.is_symlink():
                raise AgentError(code, 409)
            if (session.scalar(select(Story).where(Story.text_hash == fingerprint({"hourly_article": record_id})))
                    or any(v.metadata_json.get("record_id") == record_id for v in session.scalars(select(Variant)))
                    or session.scalar(select(PublicationSlot).where(PublicationSlot.record_id == record_id))):
                raise AgentError(code, 409)
            stamp = now()
            record.setdefault("preparation_actions", []).append({"at": stamp,
                "action": "image_rejected_before_models", "previous_state": record["state"], "error": error})
            record.update(state="cancelled", cancelled_at=stamp)
            ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
            row.state, row.error, row.updated_at, row.active_key = "rejected", error, stamp, None
            self.core.db.audit(session, "article_reserve", "reserve_image_rejected_before_models", record_id,
                claim_id=row.id, control_revision=row.control_revision, error=error,
                source_hash=record.get("original_hash"), policy_hash=record.get("policy_hash"),
                model_calls=0, source_remains_used=True)
            return _claim_view(row)

    def reject_translation_preflight(self, claim, record_id, rejection, *, cycle):
        """Только доказанный локальный отказ разбиения до первого HTTP модели."""
        from .article_quality import numbers, review_protocol_marker
        from .config import inside, parse_json
        from .db import Story
        from .reddit import text_hash
        from .translation import MADLAD_MODEL, TranslationPreflightError, preflight_segments
        code = "ARTICLE_TRANSLATION_PREFLIGHT_NOT_SAFE"
        error = "TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT"

        def require(value):
            if not value:
                raise AgentError(code, 409)

        def sha256(raw):
            return hashlib.sha256(raw).hexdigest()

        require(type(rejection) is TranslationPreflightError and rejection.code == error
            and cycle.core is self.core and cycle.quality_factory is None
            and isinstance(record_id, str) and re.fullmatch(r"[a-f0-9-]{36}", record_id))
        with self.core.db.session.begin() as session:
            row = self._preparation(session, claim, record_id)
            ledger = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            payload = copy.deepcopy(ledger.payload) if ledger else {}
            records = [r for r in payload.get("records", []) if r.get("id") == record_id]
            record = records[0] if len(records) == 1 else {}
            attempts = record.get("preparation_attempts", [])
            attempt = attempts[0] if isinstance(attempts, list) and len(attempts) == 1 else {}
            require(record.get("state") == "needs_attention" and record.get("error") == error
                and record.get("reserve_contract") == RESERVE_CONTRACT and record.get("reserve_claim") == claim
                and not record.get("model_result_unknown") and not any(record.get(k) for k in (
                    "variant_id", "postiz_id", "dispatch_id", "due_at", "prepared_hash", "material_id", "story_id",
                    "automatic_repair_attempts", "automatic_corrections", "review_protocol_recoveries", "preparation_actions"))
                and type(attempt.get("number")) is int and attempt["number"] == 1
                and attempt.get("state") == "needs_attention" and attempt.get("error") == error
                and attempt.get("operator_retry") is False and attempt.get("candidate_hash") is None
                and not attempt.get("automatic_repair_claim") and _stamp(attempt.get("started_at"))
                and _stamp(attempt.get("finished_at")) and attempt["finished_at"] >= attempt["started_at"])
            cycle._reserve_record(record, active=True, session=session)
            policy = cycle._policy_for_record(record)
            require(policy.get("approval_mode") == "automatic" and fingerprint(policy) == record.get("policy_hash"))
            root = self.core.settings.root
            paths = {"input": f"data/hourly-articles/input-{record_id}.local.json",
                     "trace": f"data/hourly-articles/quality-{record_id}.local.jsonl",
                     "config": "config/translation.json"}
            require(record.get("input_file") == paths["input"] and record.get("quality_trace") == paths["trace"])
            files = {}
            for name, relative in paths.items():
                path = root / relative
                require(path.resolve() == path and inside(root, relative) == path
                    and not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= 100000)
                files[name] = path.read_bytes()
            article, config = parse_json(files["input"].decode()), parse_json(files["config"].decode())
            require(isinstance(article, dict) and set(article) == {"title", "original", "url", "published_at"}
                and all(isinstance(article[k], str) and article[k].strip() for k in ("title", "original", "url"))
                and sha256(files["input"]) == record.get("input_file_hash")
                and fingerprint(article) == record.get("input_hash")
                and text_hash(article["original"]) == record.get("original_hash")
                and article["url"] == record.get("url") and article["published_at"] == record.get("source_published_at")
                and isinstance(config, dict) and config.get("enabled") is True
                and config.get("model") == MADLAD_MODEL and config.get("segment_chars") == 600)
            source = {"title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]}
            proof = copy.deepcopy(rejection.preflight_evidence)
            require(isinstance(proof, dict) and set(proof) == {"contract", "model", "segment_chars", "source_hash",
                "config_hash", "failed_field", "underlying_error", "no_model_requests"}
                and proof == {"contract": "dedicated-translation-preflight-v1", "model": MADLAD_MODEL,
                    "segment_chars": 600, "source_hash": fingerprint(source), "config_hash": fingerprint(config),
                    "failed_field": proof.get("failed_field"), "underlying_error": "TRANSLATION_UNSPLITTABLE_INPUT",
                    "no_model_requests": True} and proof["no_model_requests"] is True)
            try:
                preflight_segments(source["title"], source["body"], limit=600)
            except AgentError as exc:
                require(exc.code == error and getattr(exc, "failed_field", None) == proof["failed_field"])
            else:
                raise AgentError(code, 409)
            events = [parse_json(line) for line in files["trace"].decode().splitlines() if line]
            require(len(events) == 2 and all(isinstance(e, dict) for e in events)
                and [e.get("phase") for e in events] == ["translation_started", "translation_failed"])
            markers = ({key: record[key] for key in ("presentation_contract", "image_credit")}
                       if illustrated_presentation(record.get("presentation_contract")) else {})
            protocol = review_protocol_marker(record.get("review_protocol_contract"), record.get("review_rubric", "meaning-first-v1"))
            markers.update(protocol)
            context = {"instruction": (root / "prompts/ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8"),
                "source_untrusted": source, "numeric_tokens_must_match_exactly": sorted(numbers(source["title"] + "\n\n" + source["body"])),
                **protocol}
            if record.get("scientific_spelling_contract"):
                context["scientific_spelling_contract"] = record["scientific_spelling_contract"]
            if record.get("reference_contract") == "article-links-v2":
                context["reference_contract"] = record["reference_contract"]
            require(all(isinstance(e, dict) and e.get("record_id") == record_id and type(e.get("attempt")) is int
                and e["attempt"] == 1 and _stamp(e.get("at")) for e in events)
                and events[0] == {"phase": "translation_started", "attempt": 1, **markers,
                    "context": context, "at": events[0]["at"], "record_id": record_id}
                and events[1] == {"phase": "translation_failed", "attempt": 1, **markers,
                    "error": error, "at": events[1]["at"], "record_id": record_id}
                and attempt["started_at"] <= events[0]["at"] <= events[1]["at"] <= attempt["finished_at"])
            audits = list(session.scalars(select(Audit).where(Audit.entity == record_id)))
            started = [a for a in audits if a.action == "hourly_article_prepare_attempt"]
            failed = [a for a in audits if a.action == "hourly_article_prepare_error"]
            require(len(started) == len(failed) == 1 and started[0].actor == failed[0].actor == "article_quality"
                and all(started[0].details.get(k) == v for k, v in {"attempt": 1, "operator_retry": False,
                    "source_hash": record["original_hash"], "input_hash": record["input_hash"],
                    "policy_hash": record["policy_hash"], "candidate_hash": None, "automatic_repair_claim": None}.items())
                and failed[0].details == {"attempt": 1, "error": error}
                and not any(a.actor == "automatic_article_repair" for a in audits)
                and not session.scalar(select(Story).where(Story.text_hash == fingerprint({"hourly_article": record_id})))
                and not any(v.metadata_json.get("record_id") == record_id for v in session.scalars(select(Variant)))
                and not session.scalar(select(PublicationSlot).where(PublicationSlot.record_id == record_id)))
            if protocol:
                from .article_cycle import review_protocol_binding
                review_protocol_binding(self.core, session, record)
            if illustrated_presentation(record.get("presentation_contract")):
                from .article_images import validate_article_image
                validate_article_image(self.core, record)
                require(attempt.get("image_preparation_state") == "ready"
                    and all(attempt.get(k) == record[k] == started[0].details.get(k)
                        for k in ("presentation_contract", "image_credit", "illustration_proof_hash")))
            require(all((root / relative).read_bytes() == files[name] for name, relative in paths.items()))
            stamp = now()
            evidence = {"preflight": proof, "trace_hash": sha256(files["trace"]),
                        "input_file_hash": sha256(files["input"]), "config_file_hash": sha256(files["config"])}
            record.setdefault("preparation_actions", []).append({"at": stamp, "action": "translation_preflight_rejected",
                "previous_state": record["state"], "error": error, **evidence})
            record.update(state="cancelled", cancelled_at=stamp)
            ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
            row.state, row.error, row.updated_at, row.active_key = "rejected", error, stamp, None
            self.core.db.audit(session, "article_reserve", "reserve_translation_preflight_rejected", record_id,
                claim_id=row.id, control_revision=row.control_revision, error=error,
                source_hash=record["original_hash"], policy_hash=record["policy_hash"],
                dedicated_translation_http_requests=0, source_remains_used=True, **evidence)
            return _claim_view(row)

    def _record(self, session, record_id):
        ledger = session.get(EditorialLedger, LEDGER_ID)
        matches = [r for r in ledger.payload.get("records", []) if r.get("id") == record_id] if ledger else []
        if len(matches) != 1:
            raise AgentError("ARTICLE_RESERVE_RECORD_MISMATCH", 409)
        return matches[0]

    def disqualify_semantic_ready(self, record_id, *, cycle, claim=None):
        """Separate future eligibility decision; never rewrite machine QA/history."""
        from .automatic_policy import automatic_evidence, _reference_binding
        from .article_cycle import review_rubric_binding, scientific_contract_binding
        from .article_quality import rubric_translation_gate
        from .article_repair_cycle import AutomaticArticleRepair
        from .config import inside, parse_json
        from .human_review import candidate_body, source_data
        from .reddit import text_hash
        code = "ARTICLE_SEMANTIC_DISQUALIFICATION_NOT_SAFE"

        def require(value):
            if not value:
                raise AgentError(code, 409)

        with self.core.db.session() as probe:
            previous = copy.deepcopy(self._record(probe, record_id))
            if previous.get("state") != "quality_ready":
                return False  # Historical failures, publications and pending slots are untouched.
            variant = self.core._variant(probe, previous["variant_id"])
            source = source_data(probe, variant)
            candidate = {"title": variant.title, "body": candidate_body(variant, source)}
            finding = mass_relation_finding(source, candidate)
            if finding is None:
                return False
            snapshot_hash = fingerprint(self.core.snapshot(variant, self.core._target(probe, variant.target_id)))
        # The already closed reserve uses the complete native validator. An active
        # owned claim is validated below without exposing a quality_ready claim.
        closed_validation = cycle.validate_reserve(record_id) if claim is None else None
        with self.core.db.session.begin() as session:
            control = self._control(session)
            variant = self.core._variant(session, previous["variant_id"], True)
            target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
            ledger = session.scalar(select(EditorialLedger).where(EditorialLedger.id == LEDGER_ID).with_for_update())
            payload = copy.deepcopy(ledger.payload)
            record = next((r for r in payload["records"] if r.get("id") == record_id), {})
            require(record == previous and record.get("state") == "quality_ready"
                and record.get("reserve_contract") == RESERVE_CONTRACT and not record.get("model_result_unknown")
                and not any(record.get(k) is not None for k in ("postiz_id", "dispatch_id", "due_at", "publication_slot_id"))
                and record.get("prepared_hash") == record.get("reserve_prepared_hash") == snapshot_hash
                and fingerprint(self.core.snapshot(variant, target)) == snapshot_hash
                and not session.scalar(select(PublicationSlot).where(PublicationSlot.record_id == record_id))
                and not session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
                and not session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
                and not session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
                and session.get(RemoteDraft, variant.id) is None)
            require(claim is None or record["reserve_claim"] == claim)
            cycle._reserve_record(record, active=claim is not None, session=session)
            preparation = self._preparation(session, record["reserve_claim"], record_id, active=claim is not None)
            require(preparation.state == ("preparing" if claim is not None else "quality_ready"))
            policy = cycle._policy_for_record(record)
            require(policy.get("approval_mode") == "automatic" and fingerprint(policy) == record["policy_hash"])
            cycle._source_target(policy)
            AutomaticArticleRepair(cycle).assert_complete(record, session)
            source = source_data(session, variant)
            candidate = {"title": variant.title, "body": candidate_body(variant, source)}
            require(mass_relation_finding(source, candidate) == finding)
            evidence = automatic_evidence(session, variant)
            require(evidence is not None and evidence["review"]["approved"] is True
                and (closed_validation is None or closed_validation["reserve_evidence_hash"] == evidence["evidence_hash"]))
            _reference_binding(session, variant, record)
            review_rubric_binding(self.core, session, record, variant)
            scientific_contract_binding(self.core, session, record, variant)
            rubric_translation_gate(source["title"] + "\n\n" + source["body"], candidate["title"] + "\n\n" + candidate["body"],
                policy["review_rubric"], reference_contract=record.get("reference_contract", "article-links-v1"))
            raw = inside(self.core.settings.root, record["input_file"]).read_bytes()
            article = parse_json(raw.decode("utf-8"))
            require(hashlib.sha256(raw).hexdigest() == record["input_file_hash"]
                and fingerprint(article) == record["input_hash"] and article == {"title": source["title"], "original": source["body"],
                    "url": source["url"], "published_at": record["source_published_at"]}
                and text_hash(article["original"]) == record["original_hash"])
            trace = inside(self.core.settings.root, record["quality_trace"])
            trace_hash = hashlib.sha256(trace.read_bytes()).hexdigest()
            require(trace_hash == variant.metadata_json["machine_review_evidence"]["trace_hash"])
            if illustrated_presentation(record.get("presentation_contract")):
                from .article_images import validate_article_image
                validate_article_image(self.core, record, variant)
            self.core._quality(session, variant, target)
            stamp = now()
            disposition = {"contract": MASS_RELATION_CONTRACT, "finding": finding, "at": stamp,
                "machine_review_evidence_hash": evidence["evidence_hash"], "trace_hash": trace_hash,
                "source_remains_used": True, "machine_verdict_preserved": True}
            record.update(state="cancelled", cancelled_at=stamp, semantic_publication_ineligibility=disposition)
            ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
            if claim is not None:
                preparation.state, preparation.active_key, preparation.updated_at = "rejected", None, stamp
                preparation.error = "ARTICLE_COMPARATIVE_MASS_RELATION_INVERTED"
            self.core.db.audit(session, "article_reserve", "reserve_semantic_publication_ineligible", record_id,
                **disposition, control_revision=control.revision, preparation_claim_id=preparation.id,
                preparation_claim_changed=claim is not None, model_calls=0)
            return True

    def assign(self, record, due_at, *, text_hash, evidence_hash, min_gap_seconds=3600):
        """Назначить один готовый кандидат; никакой вызов Postiz здесь не выполняется."""
        if (not isinstance(record, dict) or record.get("state") != "quality_ready"
                or record.get("reserve_contract") != RESERVE_CONTRACT
                or not _stamp(due_at) or due_at < now()
                or type(min_gap_seconds) is not int or min_gap_seconds < 3600
                or not all(_hash(x) for x in (text_hash, evidence_hash, record.get("prepared_hash")))
                or text_hash != record.get("reserve_prepared_hash") or text_hash != record.get("prepared_hash")):
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_INVALID", 409)
        try:
            with self.core.db.session.begin() as session:
                control = self._control(session)
                target = session.scalar(select(Target).where(Target.id == record.get("target_id")).with_for_update())
                if target is None or not target.enabled:
                    raise AgentError("ARTICLE_TARGET_INVALID", 409)
                saved = self._record(session, record.get("id"))
                if fingerprint(saved) != fingerprint(record):
                    raise AgentError("ARTICLE_RESERVE_RECORD_MISMATCH", 409)
                self._preparation(session, record.get("reserve_claim"), record["id"], active=False)
                active = session.scalar(select(PublicationSlot).where(PublicationSlot.active_target == target.id))
                external = session.scalar(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, Dispatch.state.in_(ACTIVE)))
                owned = session.scalar(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id,
                    Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
                remote_unknown = session.scalar(select(RemoteDraft).join(Variant).where(
                    Variant.target_id == target.id, RemoteDraft.state.in_({"creating", "unknown"})))
                lease = session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                    BridgePost.mutation_owner.is_not(None) | (BridgePost.mutation_until != 0)
                    | BridgePost.mutation_until.is_(None)))
                if active or external or owned or remote_unknown or lease:
                    raise AgentError("ARTICLE_RESERVE_TARGET_BUSY", 409)
                interval = max(3600, min_gap_seconds, control.settings.get("interval_seconds", 3600))
                times = list(session.scalars(select(PublicationSlot.delivered_at).where(
                    PublicationSlot.target_id == target.id, PublicationSlot.delivered_at.is_not(None))))
                attempts = list(session.scalars(select(DispatchAttempt).join(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, DispatchAttempt.result == "published")))
                known = {attempt.dispatch_id for attempt in attempts}
                published = list(session.scalars(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, Dispatch.state == "published")))
                if any(dispatch.id not in known for dispatch in published):
                    raise AgentError("ARTICLE_PUBLISH_TIME_UNKNOWN", 409)
                times.extend(attempt.created_at for attempt in attempts)
                if times and due_at < max(times) + interval + 1:
                    raise AgentError("ARTICLE_HOURLY_GAP", 409)
                if (session.scalar(select(TargetApproval).where(TargetApproval.variant_id == record.get("variant_id")))
                        or session.scalar(select(Dispatch).where(Dispatch.variant_id == record.get("variant_id")))):
                    raise AgentError("ARTICLE_RESERVE_ALREADY_ASSIGNED", 409)
                slot_at = int(due_at // 3600) * 3600
                slot = PublicationSlot(id=str(uuid5(NAMESPACE_URL,
                    f"max-postiz:publication-slot:{target.id}:{slot_at}")), token=uid(), target_id=target.id,
                    slot_at=slot_at, record_id=record["id"], text_hash=text_hash,
                    evidence_hash=evidence_hash, binding={key: saved.get(key) for key in _IDENTITY},
                    control_revision=control.revision, active_target=target.id, due_at=due_at, state="assigned")
                session.add(slot)
                session.flush()
                self.core.db.audit(session, "article_reserve", "reserve_slot_assigned", slot.id,
                    target_id=target.id, record_id=record["id"], due_at=due_at,
                    text_hash=text_hash, evidence_hash=evidence_hash,
                    binding_hash=fingerprint(slot.binding), control_revision=control.revision)
                result = _slot_view(slot)
            return result
        except IntegrityError as exc:
            raise AgentError("ARTICLE_RESERVE_ALREADY_ASSIGNED", 409) from exc

    def require_assignment(self, record_id, *, slot_id=None, session=None):
        if session is None:
            with self.core.db.session.begin() as own:
                return self.require_assignment(record_id, slot_id=slot_id, session=own)
        control = self._control(session)
        query = select(PublicationSlot).where(PublicationSlot.record_id == record_id)
        if slot_id is not None:
            query = query.where(PublicationSlot.id == slot_id)
        rows = list(session.scalars(query))
        if len(rows) != 1:
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_LOST", 409)
        row = rows[0]
        record = self._record(session, record_id)
        if (row.state not in {"assigned", "pending"} or row.active_target != row.target_id
                or row.delivered_at is not None or row.control_revision != control.revision
                or row.binding != {key: record.get(key) for key in _IDENTITY}):
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_LOST", 409)
        return _slot_view(row)

    def rebind_pending(self, session, record_id, dispatch_id):
        """После полной GET-сверки вернуть fence прежнему QUEUE, без нового POST.

        Вызывается только из ServerCycle._resume_pending в той же транзакции,
        после сверки удалённого точного текста, даты, approval и quality evidence.
        Само наличие старого слота не разрешает возобновление.
        """
        control = self._control(session)
        record = self._record(session, record_id)
        row = session.scalar(select(PublicationSlot).where(
            PublicationSlot.record_id == record_id).with_for_update())
        dispatch = session.get(Dispatch, dispatch_id)
        variant = session.get(Variant, record.get("variant_id"))
        approval = session.get(TargetApproval, dispatch.approval_id) if dispatch else None
        link = session.get(BridgePost, record.get("postiz_id")) if record.get("postiz_id") else None
        unsafe = (
            row is None or dispatch is None or variant is None or approval is None or link is None
            or row.state not in {"assigned", "pending"} or row.active_target != row.target_id
            or row.delivered_at is not None or row.dispatch_id not in {None, dispatch_id}
            or row.binding != {key: record.get(key) for key in _IDENTITY}
            or record.get("dispatch_id") != dispatch_id or record.get("due_at") != row.due_at
            or dispatch.variant_id != variant.id or dispatch.state != "pending"
            or dispatch.owner is not None or dispatch.lease_until != 0 or dispatch.attempts != 0
            or dispatch.verified or dispatch.error or dispatch.external_id is not None or dispatch.permalink is not None
            or dispatch.due_at != row.due_at or dispatch.revision != variant.revision
            or variant.target_id != row.target_id
            or row.text_hash != record.get("reserve_prepared_hash")
            or variant.metadata_json.get("machine_review_evidence_hash") != row.evidence_hash
            or approval.variant_id != variant.id or approval.revision != variant.revision
            or approval.revoked or approval.actor != "hourly_article_policy"
            or approval.snapshot_hash != dispatch.snapshot_hash or approval.scheduled_at != row.due_at
            or link.variant_id != variant.id or not link.active or link.error or link.remote_state != "QUEUE"
            or link.mutation_owner is not None or link.mutation_until != 0
            or session.scalar(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch_id))
        )
        if unsafe:
            raise AgentError("ARTICLE_RESERVE_PENDING_RESUME_NOT_SAFE", 409)
        if session.scalar(select(Dispatch).join(Variant).where(Variant.target_id == row.target_id,
                Dispatch.id != dispatch_id, Dispatch.state.in_(ACTIVE))):
            raise AgentError("ARTICLE_RESERVE_PENDING_RESUME_NOT_SAFE", 409)
        previous = row.control_revision
        row.control_revision, row.dispatch_id, row.state = control.revision, dispatch_id, "pending"
        row.updated_at = now()
        self.core.db.audit(session, "article_reserve", "reserve_pending_refenced", row.id,
            previous_revision=previous, control_revision=control.revision,
            dispatch_id=dispatch_id, due_at=row.due_at, snapshot_hash=dispatch.snapshot_hash)
        return _slot_view(row)

    def release_cancelled_no_send(self, slot_id, *, token, remote, expected_revision):
        """Закрыть только доказанную отмену без отправки, сохранив прежний claim.

        remote — свежий GET Postiz, выполненный вызывающим кодом до транзакции.
        Старый fence не разрешает повтор отправки: текущий Control позволяет
        только терминальную классификацию. Запись, approval и dispatch не меняем.
        """
        from .max_bridge import MaxBridge

        code = "ARTICLE_RESERVE_CANCELLED_NOT_SAFE"
        if not isinstance(remote, dict) or remote.get("state") != "ERROR":
            raise AgentError(code, 409)
        body, media, observed_due, remote_hash = MaxBridge.normalize(remote)
        # Проверяем bytes изображения до транзакции, не удерживая SQL locks на GET.
        observed_media, image_proof_hash = None, None
        with self.core.db.session() as probe:
            assigned = probe.get(PublicationSlot, slot_id)
            ledger = probe.get(EditorialLedger, LEDGER_ID)
            matches = [r for r in ledger.payload.get("records", [])
                       if assigned and r.get("id") == assigned.record_id] if ledger else []
            pre_record = matches[0] if len(matches) == 1 else {}
            pre_record = copy.deepcopy(pre_record)
            target = probe.get(Target, assigned.target_id) if assigned else None
        if illustrated_presentation(pre_record.get("presentation_contract")):
            from .article_images import validate_article_image
            expected_media = validate_article_image(self.core, pre_record)
            observed_media = self.core.bridge._media(media, target)
            image_proof_hash = pre_record.get("illustration_proof_hash")
            if observed_media != expected_media:
                raise AgentError(code, 409)
        with self.core.db.session.begin() as session:
            control = self._control(session)
            if type(expected_revision) is not int or control.revision != expected_revision:
                raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
            # Сначала читаем идентичность, затем берём locks в обычном порядке
            # Control → Variant → Target → Dispatch → Approval → Bridge → Slot.
            candidate = session.get(PublicationSlot, slot_id)
            if candidate is None:
                raise AgentError(code, 409)
            identity = (candidate.record_id, candidate.binding.get("variant_id"),
                        candidate.target_id, candidate.dispatch_id)
            variant = session.scalar(select(Variant).where(Variant.id == identity[1]).with_for_update())
            target = session.scalar(select(Target).where(Target.id == identity[2]).with_for_update())
            dispatch = session.scalar(select(Dispatch).where(Dispatch.id == identity[3]).with_for_update())
            approval = session.scalar(select(TargetApproval).where(
                TargetApproval.id == dispatch.approval_id).with_for_update()) if dispatch else None
            links = list(session.scalars(select(BridgePost).where(
                BridgePost.variant_id == identity[1]).with_for_update()))
            link = links[0] if len(links) == 1 else None
            row = session.scalar(select(PublicationSlot).where(
                PublicationSlot.id == slot_id).with_for_update().execution_options(populate_existing=True))
            ledger = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            records = [r for r in ledger.payload.get("records", [])
                       if r.get("id") == identity[0]] if ledger else []
            record = records[0] if len(records) == 1 else {}
            snapshot = dispatch.snapshot if dispatch else {}
            metadata = snapshot.get("metadata", {}) if isinstance(snapshot, dict) else {}
            if not isinstance(snapshot, dict) or not isinstance(metadata, dict):
                raise AgentError(code, 409)
            illustrated = illustrated_presentation(record.get("presentation_contract"))
            if illustrated:
                if variant is None:
                    raise AgentError(code, 409)
                validate_article_image(self.core, record, variant)
                media_safe = (observed_media == variant.media == snapshot.get("media")
                    and record.get("illustration_proof_hash") == image_proof_hash)
            else:
                media_safe = not media and snapshot.get("media") == []
            unsafe = (
                row is None or variant is None or target is None or dispatch is None
                or approval is None or link is None or not record
                or row.token != token or row.state not in {"pending", "blocked_unknown"}
                or (row.record_id, row.binding.get("variant_id"), row.target_id, row.dispatch_id) != identity
                or row.active_target != row.target_id or row.delivered_at is not None
                or not _stamp(row.due_at) or row.due_at > now()
                or row.binding != {key: record.get(key) for key in _IDENTITY}
                or record.get("reserve_contract") != RESERVE_CONTRACT or record.get("state") != "cancelled"
                or record.get("dispatch_id") != dispatch.id or record.get("variant_id") != variant.id
                or record.get("target_id") != target.id or record.get("postiz_id") != link.post_id
                or record.get("due_at") != row.due_at or record.get("published_at") is not None
                or record.get("delivery_verified") is not False
                or dispatch.variant_id != variant.id or dispatch.state != "cancelled"
                or dispatch.attempts != 0 or dispatch.owner is not None or dispatch.lease_until != 0
                or dispatch.verified is not False or dispatch.error is not None
                or dispatch.external_id is not None or dispatch.permalink is not None
                or dispatch.due_at != row.due_at or dispatch.snapshot_hash != fingerprint(
                    {"content": snapshot, "due_at": row.due_at})
                or approval.variant_id != variant.id or approval.revision != dispatch.revision
                or approval.revoked is not True or approval.actor != "hourly_article_policy"
                or approval.scheduled_at != row.due_at or approval.snapshot_hash != dispatch.snapshot_hash
                or snapshot.get("variant_id") != variant.id or snapshot.get("revision") != dispatch.revision
                or snapshot.get("target_id") != target.id or snapshot.get("destination") != target.destination
                or snapshot.get("platform") != "max" or snapshot.get("route") != "max_browser"
                or metadata.get("origin") != "hourly_article_test" or metadata.get("approval_mode") != "automatic"
                or metadata.get("record_id") != record["id"] or metadata.get("policy_hash") != record.get("policy_hash")
                or target.platform != "max" or target.route != "max_browser" or not target.enabled
                or target.paused is not True
                or variant.target_id != target.id or variant.body != snapshot.get("body")
                or variant.media != snapshot.get("media") or not media_safe
                or row.text_hash != record.get("reserve_prepared_hash")
                or metadata.get("machine_review_evidence_hash") != row.evidence_hash
                or variant.metadata_json.get("machine_review_evidence_hash") != row.evidence_hash
                or link.target_id != target.id or not link.active or link.error is not None
                or link.remote_state != "ERROR" or link.remote_hash != remote_hash
                or not _stamp(link.due_at) or abs(link.due_at - row.due_at) >= .001
                or link.mutation_owner is not None or link.mutation_until != 0
                or remote.get("id") != link.post_id or body != snapshot.get("body")
                or not _stamp(observed_due) or abs(observed_due - row.due_at) >= .001
                or remote.get("group") != link.group_id
                or remote.get("integration", {}).get("id") != link.integration_id
                or remote.get("releaseId") not in (None, "") or remote.get("releaseURL") not in (None, "")
                or metadata.get("postiz_state") != "QUEUE"
            )
            if unsafe:
                raise AgentError(code, 409)
            # Журнал браузера записывается до клика. Любой прежний receipt,
            # включая sending, требует сверки даже при противоречивой БД.
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dispatch.id) is None:
                raise AgentError(code, 409)
            receipt = self.core.settings.root / "data/browser-publications" / (dispatch.id + ".json")
            if receipt.exists() or receipt.is_symlink():
                raise AgentError(code, 409)
            previous_remote = copy.deepcopy(remote)
            previous_remote["state"] = "QUEUE"
            if MaxBridge.normalize(previous_remote)[3] != metadata.get("postiz_hash"):
                raise AgentError(code, 409)
            if (session.scalar(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id))
                    or session.scalar(select(Dispatch).join(Variant).where(
                        Variant.target_id == target.id, Dispatch.id != dispatch.id,
                        Dispatch.state.in_(ACTIVE) | Dispatch.owner.is_not(None)
                        | (Dispatch.lease_until != 0) | Dispatch.lease_until.is_(None)))
                    or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id,
                        Dispatch.id != dispatch.id))
                    or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id,
                        TargetApproval.id != approval.id))
                    or session.scalar(select(RemoteDraft).join(Variant).where(
                        Variant.target_id == target.id, RemoteDraft.state.in_({"creating", "unknown"})))
                    or session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                        BridgePost.mutation_owner.is_not(None) | (BridgePost.mutation_until != 0)
                        | BridgePost.mutation_until.is_(None)))
                    or session.scalar(select(PublicationSlot).where(
                        PublicationSlot.active_target == target.id, PublicationSlot.id != row.id))):
                raise AgentError(code, 409)
            previous_revision = row.control_revision
            row.state, row.active_target = "missed", None
            row.error, row.updated_at = "KNOWN_CANCELLED_NO_SEND", now()
            self.core.db.audit(session, "article_reserve", "reserve_cancelled_no_send_missed", row.id,
                record_id=record["id"], dispatch_id=dispatch.id, postiz_id=link.post_id,
                previous_revision=previous_revision, control_revision=control.revision,
                due_at=row.due_at, snapshot_hash=dispatch.snapshot_hash,
                remote_hash=remote_hash, remote_proof_hash=fingerprint(remote),
                dispatch_attempts=0, source_remains_used=True)
            return _slot_view(row)

    def release_preclick_failed_no_send(self, slot_id, *, token, remote, expected_revision):
        """Закрыть будущий доказанный отказ ДО клика, не повторяя старый источник.

        Единственная failed-попытка и её raw journal остаются в истории. Свежий
        GET Postiz и проверка bytes выполняются до SQL locks; неизвестный исход
        или журнал прежнего протокола этим методом не снимаются.
        """
        from .article_images import validate_article_image
        from .browser_publisher import read_preclick_failure
        from .db import Draft
        from .max_bridge import MaxBridge

        code = "ARTICLE_RESERVE_PRECLICK_NOT_SAFE"

        def require(value):
            if not value:
                raise AgentError(code, 409)

        require(isinstance(remote, dict) and remote.get("state") == "ERROR")
        body, media, observed_due, remote_hash = MaxBridge.normalize(remote)
        with self.core.db.session() as probe:
            assigned = probe.get(PublicationSlot, slot_id)
            require(assigned is not None and assigned.dispatch_id is not None)
            previous_record = copy.deepcopy(self._record(probe, assigned.record_id))
            previous_dispatch = probe.get(Dispatch, assigned.dispatch_id)
            previous_target = probe.get(Target, assigned.target_id)
            require(previous_dispatch is not None and previous_target is not None)
            previous_snapshot = copy.deepcopy(previous_dispatch.snapshot)
            request_id, destination = previous_dispatch.id, previous_target.destination
        # Даже текстовый исторический формат должен доказать отсутствие медиа.
        expected_media = validate_article_image(self.core, previous_record)
        observed_media = self.core.bridge._media(media, previous_target)
        require(observed_media == expected_media)
        try:
            proof = read_preclick_failure(self.core.settings, request_id,
                snapshot=previous_snapshot, chat_id=destination)
        except AgentError as exc:
            raise AgentError(code, 409) from exc
        config_hash = self.core.settings.hash
        require(isinstance(proof, dict) and _hash(proof.get("sha256"))
            and isinstance(proof.get("proof"), dict) and _stamp(proof["proof"].get("failed_at")))
        # MAX 429 требует остановки: другой источник не разрешает новый GET канала.
        require(proof["proof"].get("error", {}).get("code") != "BROWSER_CHANNEL_RATE_LIMITED")
        with self.core.db.session.begin() as session:
            control = self._control(session)
            if type(expected_revision) is not int or control.revision != expected_revision:
                raise AgentError("EDITORIAL_REVISION_CONFLICT", 409)
            candidate = session.get(PublicationSlot, slot_id)
            require(candidate is not None)
            identity = (candidate.record_id, candidate.binding.get("variant_id"),
                        candidate.target_id, candidate.dispatch_id)
            # Тот же порядок, что у отправителя, approval и native mutation.
            # Control → Variant → Target → Dispatch → Approval → Bridge → Slot → Ledger.
            variant = session.scalar(select(Variant).where(Variant.id == identity[1]).with_for_update())
            target = session.scalar(select(Target).where(Target.id == identity[2]).with_for_update())
            dispatch = session.scalar(select(Dispatch).where(Dispatch.id == identity[3]).with_for_update())
            approval = session.scalar(select(TargetApproval).where(
                TargetApproval.id == dispatch.approval_id).with_for_update()) if dispatch else None
            links = list(session.scalars(select(BridgePost).where(
                BridgePost.variant_id == identity[1]).with_for_update()))
            link = links[0] if len(links) == 1 else None
            row = session.scalar(select(PublicationSlot).where(PublicationSlot.id == slot_id)
                .with_for_update().execution_options(populate_existing=True))
            ledger = session.scalar(select(EditorialLedger).where(
                EditorialLedger.id == LEDGER_ID).with_for_update())
            payload = copy.deepcopy(ledger.payload) if ledger else {}
            records = [r for r in payload.get("records", []) if r.get("id") == identity[0]]
            record = records[0] if len(records) == 1 else {}
            require(all(value is not None for value in (row, variant, target, dispatch, approval, link, ledger))
                and record == previous_record and bool(record))
            snapshot = dispatch.snapshot
            metadata = snapshot.get("metadata", {}) if isinstance(snapshot, dict) else {}
            require(isinstance(snapshot, dict) and isinstance(metadata, dict))
            draft = session.get(Draft, variant.draft_id)
            require(
                row.token == token and row.state in {"pending", "blocked_unknown"}
                and (row.record_id, row.binding.get("variant_id"), row.target_id, row.dispatch_id) == identity
                and row.active_target == row.target_id and row.delivered_at is None
                and _stamp(row.due_at) and row.due_at <= now()
                and row.binding == {key: record.get(key) for key in _IDENTITY}
                and record.get("reserve_contract") == RESERVE_CONTRACT and record.get("state") == "failed"
                and record.get("dispatch_state") == "failed" and record.get("error") == "BROWSER_PRECLICK_FAILED"
                and record.get("dispatch_id") == dispatch.id == request_id
                and record.get("variant_id") == variant.id and record.get("target_id") == target.id
                and record.get("postiz_id") == link.post_id and record.get("due_at") == row.due_at
                and record.get("published_at") is None and record.get("delivery_verified") is False
                and not record.get("model_result_unknown") and not record.get("no_send_disposition")
                and dispatch.variant_id == variant.id and dispatch.state == "failed"
                and dispatch.error == "BROWSER_PRECLICK_FAILED" and dispatch.attempts == 1
                and dispatch.owner is None and dispatch.lease_until == 0 and dispatch.verified is False
                and dispatch.external_id is None and dispatch.permalink is None
                and dispatch.due_at == row.due_at and dispatch.revision == variant.revision
                and snapshot == previous_snapshot == self.core.snapshot(variant, target)
                and dispatch.snapshot_hash == fingerprint({"content": snapshot, "due_at": row.due_at})
                and snapshot.get("destination") == target.destination == destination
                and snapshot.get("platform") == "max" and snapshot.get("route") == "max_browser"
                and self.core.settings.hash == config_hash == variant.config_hash
                and variant.binding_hash == target.binding_hash
                and draft is not None and draft.revision == variant.source_revision
                and draft.config_id == variant.config_hash
                and approval.variant_id == variant.id and approval.revision == dispatch.revision
                and approval.revoked is False and approval.actor == "hourly_article_policy"
                and approval.scheduled_at == row.due_at and approval.snapshot_hash == dispatch.snapshot_hash
                and metadata.get("origin") == "hourly_article_test" and metadata.get("approval_mode") == "automatic"
                and metadata.get("record_id") == record["id"] and metadata.get("policy_hash") == record.get("policy_hash")
                and metadata.get("reserve_contract") == RESERVE_CONTRACT
                and metadata.get("reserve_binding_hash") == record.get("reserve_binding_hash")
                and metadata.get("postiz_post_id") == link.post_id
                and _postiz_due_matches(metadata.get("postiz_due_at"), row.due_at)
                and metadata.get("postiz_state") == "QUEUE" and variant.target_id == target.id
                and target.platform == "max" and target.route == "max_browser" and target.enabled
                and row.text_hash == record.get("reserve_prepared_hash")
                and metadata.get("machine_review_evidence_hash") == row.evidence_hash
                and variant.media == expected_media == observed_media
                and link.target_id == target.id and link.active and link.error is None
                and link.remote_state == "ERROR" and link.remote_hash == remote_hash
                and _stamp(link.due_at) and abs(link.due_at - row.due_at) < .001
                and link.mutation_owner is None and link.mutation_until == 0
                and remote.get("id") == link.post_id and body == snapshot.get("body")
                and _stamp(observed_due) and abs(observed_due - row.due_at) < .001
                and remote.get("group") == link.group_id
                and remote.get("integration", {}).get("id") == link.integration_id
                and target.options.get("postiz_integration_id") == link.integration_id
                and remote.get("releaseId") in (None, "") and remote.get("releaseURL") in (None, "")
            )
            previous_remote = copy.deepcopy(remote)
            previous_remote["state"] = "QUEUE"
            require(MaxBridge.normalize(previous_remote)[3] == metadata.get("postiz_hash"))
            attempts = list(session.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id)))
            require(len(attempts) == 1 and attempts[0].result == "failed"
                and attempts[0].error == "BROWSER_PRECLICK_FAILED"
                and _stamp(attempts[0].created_at) and row.due_at <= attempts[0].created_at <= now())
            require(not session.scalar(select(Dispatch).join(Variant).where(
                    Variant.target_id == target.id, Dispatch.id != dispatch.id,
                    Dispatch.state.in_(ACTIVE) | Dispatch.owner.is_not(None)
                    | (Dispatch.lease_until != 0) | Dispatch.lease_until.is_(None)))
                and not session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id, Dispatch.id != dispatch.id))
                and not session.scalar(select(TargetApproval).where(
                    TargetApproval.variant_id == variant.id, TargetApproval.id != approval.id))
                and not session.scalar(select(RemoteDraft).join(Variant).where(
                    Variant.target_id == target.id, RemoteDraft.state.in_({"creating", "unknown"})))
                and not session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                    BridgePost.mutation_owner.is_not(None) | (BridgePost.mutation_until != 0)
                    | BridgePost.mutation_until.is_(None)))
                and not session.scalar(select(PublicationSlot).where(
                    PublicationSlot.active_target == target.id, PublicationSlot.id != row.id))
                and not session.scalar(select(EditorialPreparationClaim).where(
                    EditorialPreparationClaim.active_key.is_not(None))))
            # Существующий closed claim не восстанавливаем и не освобождаем заново.
            preparation = self._preparation(session, record.get("reserve_claim"), record["id"], active=False)
            require(preparation.state == "quality_ready" and preparation.error is None)
            validate_article_image(self.core, record, variant)
            # Только локальное чтение: повторная exact SHA-сверка закрывает смену
            # journal/config/image между медленной внешней проверкой и commit.
            try:
                require(read_preclick_failure(self.core.settings, dispatch.id,
                    snapshot=snapshot, chat_id=target.destination) == proof)
            except AgentError as exc:
                raise AgentError(code, 409) from exc
            stamp = now()
            disposition = {"contract": "reserve-preclick-no-send-v1", "closed_at": stamp,
                "dispatch_id": dispatch.id, "attempt_id": attempts[0].id,
                "variant_id": variant.id, "revision": dispatch.revision, "target_id": target.id,
                "due_at": row.due_at, "browser_proof_contract": proof["proof"]["contract"],
                "snapshot_hash": dispatch.snapshot_hash, "browser_proof_sha256": proof["sha256"],
                "remote_proof_hash": fingerprint(remote), "source_remains_used": True}
            dispatch.state, approval.revoked, target.paused = "cancelled", True, True
            # Variant/Draft/machine evidence/raw и исходная failed Attempt неизменны.
            record.update(state="cancelled", dispatch_state="cancelled", no_send_disposition=disposition)
            ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
            row.state, row.active_target, row.error, row.updated_at = "missed", None, "KNOWN_PRECLICK_NO_SEND", stamp
            self.core.db.audit(session, "article_reserve", "reserve_preclick_no_send_missed", row.id,
                record_id=record["id"], postiz_id=link.post_id,
                previous_revision=row.control_revision, control_revision=control.revision,
                dispatch_attempts=dispatch.attempts, original_error=dispatch.error, **disposition)
            return _slot_view(row)

    def update(self, slot_id, state, *, token, dispatch_id=None, delivered_at=None, error=None, session=None):
        if state not in _TRANSITIONS or delivered_at is not None and not _stamp(delivered_at):
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if session is None:
            with self.core.db.session.begin() as own:
                return self.update(slot_id, state, token=token, dispatch_id=dispatch_id,
                    delivered_at=delivered_at, error=error, session=own)
        # Подтверждённый факт доставки сохраняется даже при общей паузе.
        control = self._control(session, running=state not in {"delivered", "published"})
        row = session.scalar(select(PublicationSlot).where(PublicationSlot.id == slot_id).with_for_update())
        if (row is None or row.token != token or state not in _TRANSITIONS.get(row.state, set())
                or state not in {"delivered", "published"} and row.control_revision != control.revision
                or row.dispatch_id is not None and dispatch_id not in {None, row.dispatch_id}
                or row.delivered_at is not None and delivered_at not in {None, row.delivered_at}):
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        bound_dispatch = dispatch_id or row.dispatch_id
        dispatch = session.get(Dispatch, bound_dispatch) if bound_dispatch else None
        if bound_dispatch is not None and (dispatch is None or dispatch.variant_id != row.binding.get("variant_id")):
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if state in {"pending", "delivered", "published"} and not dispatch:
            raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        actual_time = delivered_at if delivered_at is not None else row.delivered_at
        if state in {"delivered", "published"}:
            attempt = session.scalar(select(DispatchAttempt).where(
                DispatchAttempt.dispatch_id == bound_dispatch, DispatchAttempt.result == "published",
                DispatchAttempt.created_at == actual_time))
            if (actual_time is None or dispatch.state != "published"
                    or dispatch.verified is not True or attempt is None):
                raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if state == "published":
            record = self._record(session, row.record_id)
            if (record.get("state") != "published" or record.get("postiz_state") != "PUBLISHED"
                    or record.get("dispatch_id") != bound_dispatch
                    or record.get("published_at") != actual_time or not record.get("postiz_id")):
                raise AgentError("ARTICLE_RESERVE_SLOT_STATE_INVALID", 409)
        if dispatch_id is not None:
            row.dispatch_id = dispatch_id
        if delivered_at is not None:
            row.delivered_at = delivered_at
        row.state, row.error, row.updated_at = state, error, now()
        row.active_target = row.target_id if state in ACTIVE_SLOT_STATES else None
        self.core.db.audit(session, "article_reserve", "reserve_slot_state", row.id,
            state=state, dispatch_id=row.dispatch_id, delivered_at=row.delivered_at, error=error)
        return _slot_view(row)

    def status(self, target_id=None):
        with self.core.db.session() as session:
            query = select(PublicationSlot)
            if target_id is not None:
                query = query.where(PublicationSlot.target_id == target_id)
            rows = list(session.scalars(query))
            active = [_slot_view(row, private=False) for row in rows if row.active_target]
            delivered = [row.delivered_at for row in rows if row.delivered_at is not None]
            return {"slots": dict(Counter(row.state for row in rows)), "active": active,
                "last_delivered_at": max(delivered) if delivered else None,
                "next_delivery_at": min(row["due_at"] for row in active) if active else None}

    def next_available_due(self, target_id, due_at):
        """Не переиспользовать час закрытого missed claim для другого текста.

        Это только чтение: окончательная уникальность и интервал снова
        проверяются в assign. Прошедшие часы никогда не догоняются.
        """
        if not _stamp(due_at):
            raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_INVALID", 409)
        with self.core.db.session() as session:
            occupied = set(session.scalars(select(PublicationSlot.slot_at).where(
                PublicationSlot.target_id == target_id)))
        for _ in range(len(occupied) + 1):
            hour = int(due_at // 3600) * 3600
            if hour not in occupied:
                return due_at
            due_at = hour + 3601
        raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_INVALID", 409)

    def get(self, slot_id):
        """Чтение прежнего claim для сверки, включая паузу и неизвестный исход."""
        with self.core.db.session() as session:
            row = session.get(PublicationSlot, slot_id)
            if row is None:
                raise AgentError("ARTICLE_RESERVE_SLOT_NOT_FOUND", 404)
            return _slot_view(row)
