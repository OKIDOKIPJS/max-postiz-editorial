"""Owner-delegated article tests; preparation and exact-version approval are separate."""
import copy
import hashlib
import json
import math
import os
import re
from contextlib import ExitStack
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .adapters import OpenCode
from .article_attribution import article_footer, illustrated_presentation
from .article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT, REFERENCE_CONTRACTS
from .browser_runtime import profile_lock
from .config import AgentError, fingerprint, inside, parse_json
from .db import Audit, Draft, DraftRevision, Material, Story, now, uid
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
from .reddit import text_hash


def automatic_review_rubric(record):
    """Старый журнал без marker сохраняет прежние критерии при смене настройки."""
    from .article_quality import AUTOMATIC_REVIEW_RUBRICS, MEANING_FIRST_REVIEW_RUBRIC
    rubric = record.get("review_rubric", MEANING_FIRST_REVIEW_RUBRIC)
    if not isinstance(rubric, str) or rubric not in AUTOMATIC_REVIEW_RUBRICS:
        raise AgentError("ARTICLE_REVIEW_RUBRIC_INVALID", 409)
    return rubric


def automatic_review_prompt_name(record):
    from .article_quality import review_prompt_name
    return review_prompt_name(automatic_review_rubric(record),
                              review_protocol_contract=record.get("review_protocol_contract"))


def review_protocol_binding(core, session, record, variant=None):
    """Новые bounds закреплены в записи/Audit до модели; старые не мигрируются."""
    from .article_quality import REVIEW_BOUNDS_V2_CONTRACT, review_protocol_marker
    protocol = record.get("review_protocol_contract")
    review_protocol_marker(protocol, automatic_review_rubric(record))
    attempts = record.get("preparation_attempts", [])
    audits = list(session.scalars(select(Audit).where(
        Audit.entity == record["id"], Audit.action == "hourly_article_prepare_attempt")))
    if (not isinstance(attempts, list) or any(not isinstance(a, dict) for a in attempts)
            or any(not isinstance(a.details, dict) for a in audits)):
        raise AgentError("ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID", 409)
    markers = [protocol, *(a.get("review_protocol_contract") for a in attempts),
               *(a.details.get("review_protocol_contract") for a in audits)]
    if variant is not None:
        evidence = variant.metadata_json.get("machine_review_evidence", {})
        if not isinstance(evidence, dict):
            raise AgentError("ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID", 409)
        markers.extend((variant.metadata_json.get("review_protocol_contract"),
                        evidence.get("review_protocol_contract")))
    if not any(value is not None for value in markers):
        return None
    if (protocol != REVIEW_BOUNDS_V2_CONTRACT or not attempts or len(audits) != len(attempts)
            or any(value != protocol for value in markers)):
        raise AgentError("ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID", 409)
    for number, attempt in enumerate(attempts, 1):
        matching = [a for a in audits if type(a.details.get("attempt")) is int
                    and a.details["attempt"] == number]
        actor = ("automatic_article_repair" if attempt.get("automatic_repair_claim") else
                 "hourly_article_operator" if attempt.get("operator_retry") else "article_quality")
        if (type(attempt.get("number")) is not int or attempt["number"] != number or len(matching) != 1
                or matching[0].actor != actor
                or any(matching[0].details.get(key) != record.get(key) for key in ("input_hash", "policy_hash"))
                or matching[0].details.get("source_hash") != record.get("original_hash")):
            raise AgentError("ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID", 409)
    return protocol


def review_rubric_binding(core, session, record, variant=None):
    """Практические критерии требуют marker в записи, attempts и durable Audit."""
    from .article_quality import PRACTICAL_REVIEW_RUBRICS, MEANING_FIRST_REVIEW_RUBRIC
    review_protocol_binding(core, session, record, variant)
    rubric = automatic_review_rubric(record)
    attempts = record.get("preparation_attempts", [])
    audits = list(session.scalars(select(Audit).where(
        Audit.entity == record["id"], Audit.action == "hourly_article_prepare_attempt")))
    if not isinstance(attempts, list) or any(not isinstance(a, dict) for a in attempts):
        raise AgentError("ARTICLE_REVIEW_RUBRIC_BINDING_INVALID", 409)
    markers = [record.get("review_rubric"), *(a.get("review_rubric") for a in attempts)]
    if any(not isinstance(a.details, dict) for a in audits):
        raise AgentError("ARTICLE_REVIEW_RUBRIC_BINDING_INVALID", 409)
    markers.extend(a.details.get("review_rubric") for a in audits)
    if variant is not None:
        markers.append(variant.metadata_json.get("review_rubric"))
    if not any(marker in PRACTICAL_REVIEW_RUBRICS for marker in markers if isinstance(marker, str)):
        if rubric != MEANING_FIRST_REVIEW_RUBRIC or any(
                marker is not None and marker != MEANING_FIRST_REVIEW_RUBRIC for marker in markers):
            raise AgentError("ARTICLE_REVIEW_RUBRIC_BINDING_INVALID", 409)
        return rubric
    if (rubric not in PRACTICAL_REVIEW_RUBRICS or not attempts
            or any(marker != rubric for marker in markers)):
        raise AgentError("ARTICLE_REVIEW_RUBRIC_BINDING_INVALID", 409)
    for number, attempt in enumerate(attempts, 1):
        matched = [a for a in audits if type(a.details.get("attempt")) is int and a.details["attempt"] == number]
        if (type(attempt.get("number")) is not int or attempt["number"] != number
                or len(matched) != 1 or matched[0].details.get("policy_hash") != record.get("policy_hash")
                or matched[0].details.get("input_hash") != record.get("input_hash")
                or matched[0].details.get("source_hash") != record.get("original_hash")):
            raise AgentError("ARTICLE_REVIEW_RUBRIC_BINDING_INVALID", 409)
    return rubric


def scientific_contract_binding(core, session, record, variant=None):
    """Новую словарную семантику закрепляем до модели, без переоценки истории."""
    from .scientific_terms import SCIENTIFIC_SPELLING_CONTRACT
    attempts = record.get("preparation_attempts", [])
    audits = list(session.scalars(select(Audit).where(
        Audit.entity == record["id"], Audit.action == "hourly_article_prepare_attempt")))
    if (not isinstance(attempts, list) or any(not isinstance(a, dict) for a in attempts)
            or any(not isinstance(a.details, dict) for a in audits)):
        raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID", 409)
    marker = record.get("scientific_spelling_contract")
    markers = [marker, *(a.get("scientific_spelling_contract") for a in attempts),
               *(a.details.get("scientific_spelling_contract") for a in audits)]
    evidence = variant.metadata_json.get("machine_review_evidence", {}) if variant else {}
    if variant:
        markers.extend((variant.metadata_json.get("scientific_spelling_contract"),
                        evidence.get("scientific_spelling_contract") if isinstance(evidence, dict) else None))
    if not any(value is not None for value in markers):
        return None
    if (marker != SCIENTIFIC_SPELLING_CONTRACT or not attempts or any(value != marker for value in markers)
            or any(a.get("number") != number or type(a.get("number")) is not int
                   or a.get("scientific_spelling_contract") != marker
                   for number, a in enumerate(attempts, 1))):
        raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID", 409)
    for attempt in attempts:
        matching = [a for a in audits if type(a.details.get("attempt")) is int
                    and a.details["attempt"] == attempt["number"]]
        expected = {"scientific_spelling_contract": marker, "source_hash": record.get("original_hash"),
                    "input_hash": record.get("input_hash"), "policy_hash": record.get("policy_hash"),
                    "candidate_hash": attempt.get("candidate_hash"), "operator_retry": attempt.get("operator_retry")}
        if (len(matching) != 1
                or any(matching[0].details.get(key) != value for key, value in expected.items())):
            raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID", 409)
    if variant:
        if (not isinstance(evidence, dict)
                or variant.metadata_json.get("scientific_spelling_contract") != marker
                or evidence.get("scientific_spelling_contract") != marker):
            raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID", 409)
        from .automatic_policy import scientific_trace_binding
        trace = inside(core.settings.root, record.get("quality_trace", ""))
        scientific_trace_binding(trace, marker, expected_hash=evidence.get("trace_hash"), expected_evidence=evidence,
                                 expected_record_id=record["id"])
    return marker


class HourlyArticleCycle:
    """A private durable ledger stops interrupted operations instead of replaying them."""

    def __init__(self, core, quality_factory=None, repair_factory=None, *, reserve_mode=False,
                 scientific_spelling_contract=None, review_protocol_contract=None):
        self.core, self.quality_factory, self.repair_factory = core, quality_factory, repair_factory
        self.reserve_mode = reserve_mode
        from .article_quality import REVIEW_BOUNDS_V2_CONTRACT
        if review_protocol_contract not in {None, REVIEW_BOUNDS_V2_CONTRACT}:
            raise AgentError("ARTICLE_REVIEW_PROTOCOL_CONTRACT_INVALID", 409)
        self.review_protocol_contract = review_protocol_contract
        if scientific_spelling_contract is not None:
            from .scientific_terms import SCIENTIFIC_SPELLING_CONTRACT
            if scientific_spelling_contract != SCIENTIFIC_SPELLING_CONTRACT:
                raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_CONTRACT_INVALID", 409)
        self.scientific_spelling_contract = scientific_spelling_contract
        self.directory = inside(core.settings.root, "data/hourly-articles")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.directory / "ledger.local.json"

    def _preparation_lock(self):
        # Долгие модели резерва не удерживают короткую блокировку отправителя.
        return self.directory / ("reserve-preparation.lock" if self.reserve_mode else "cycle.lock")

    def _reserve_record(self, record, *, active=False, session=None):
        """Marker не даёт прав сам по себе: проверяем постоянный claim и Audit."""
        if session is None:
            with self.core.db.session.begin() as own:
                return self._reserve_record(record, active=active, session=own)
        if record.get("reserve_contract") != "article-reserve-v1":
            raise AgentError("ARTICLE_RESERVE_BINDING_INVALID", 409)
        if active and not self.reserve_mode:
            raise AgentError("ARTICLE_RESERVE_MODE_REQUIRED", 409)
        from .article_reserve import ArticleReserveStore
        claim = record.get("reserve_claim")
        ArticleReserveStore(self.core).require_preparation(claim, record["id"], active=active, session=session)
        binding = {key: record.get(key) for key in ("id", "reserve_contract", "reserve_claim", "input_hash",
            "policy_hash", "target_id", "source_id", "original_hash", "source_published_at")}
        hashed = fingerprint(binding)
        audit = session.get(Audit, str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record["id"])))
        if (not audit or audit.actor != "article_reserve" or audit.entity != record["id"]
                or audit.action != "article_reserve_preparation_binding"
                or audit.details != {"binding": binding, "binding_hash": hashed}
                or record.get("reserve_binding_hash") != hashed):
            raise AgentError("ARTICLE_RESERVE_BINDING_INVALID", 409)
        return claim

    def _reserve_other_safe(self, record):
        # Неизвестные модели/внешние операции не превращаются в свободный источник.
        if record.get("reserve_contract") != "article-reserve-v1":
            return False
        self._reserve_record(record)
        return record.get("state") in {"quality_ready", "automatic_ready", "pending", "published",
                                        "failed", "cancelled", "rejected"}

    def _policy(self, enabled=True):
        path = inside(self.core.settings.root, "data/hourly-article-policy.local.json")
        try:
            policy = json.loads(path.read_text(encoding="utf-8-sig"))
            required = {"enabled", "target_id", "source_id", "allowed_hosts", "rights_evidence",
                        "owner_instruction", "min_gap_seconds", "max_per_day", "max_source_age_hours"}
            mode = policy.get("approval_mode", "legacy")
            from .article_quality import AUTOMATIC_REVIEW_RUBRICS
            keys = set(policy) - {"presentation_contract", "presentation_owner_instruction"}
            presentation = policy.get("presentation_contract")
            presentation_keys = set(policy) & {"presentation_contract", "presentation_owner_instruction"}
            valid_keys = (keys == required | {"approval_mode", "review_rubric"}
                          and policy.get("review_rubric") in AUTOMATIC_REVIEW_RUBRICS) if mode == "automatic" else (
                              keys in (required, required | {"approval_mode"}))
            if (not valid_keys
                    or presentation_keys and (presentation_keys != {"presentation_contract", "presentation_owner_instruction"}
                        or mode != "automatic" or not illustrated_presentation(presentation)
                        or not isinstance(policy["presentation_owner_instruction"], str)
                        or len(policy["presentation_owner_instruction"].strip()) < 10)
                    or policy.get("approval_mode", "legacy") not in {"legacy", "human", "automatic"}
                    or type(policy["enabled"]) is not bool
                    or not all(isinstance(policy[k], str) and policy[k].strip()
                               for k in ("target_id", "source_id", "rights_evidence", "owner_instruction"))
                    or not isinstance(policy["allowed_hosts"], list) or not policy["allowed_hosts"]
                    or any(not isinstance(x, str) or not x or x != x.lower()
                           or any(c in x for c in "/:@?# ") for x in policy["allowed_hosts"])
                    or type(policy["min_gap_seconds"]) is not int or policy["min_gap_seconds"] < 3600
                    or type(policy["max_per_day"]) is not int or not 1 <= policy["max_per_day"] <= 24
                    or type(policy["max_source_age_hours"]) is not int
                    or not 72 <= policy["max_source_age_hours"] <= 43800):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AgentError("ARTICLE_POLICY_INVALID", 409) from exc
        if enabled and not policy["enabled"]:
            raise AgentError("ARTICLE_POLICY_DISABLED", 409)
        return policy

    def _load(self):
        from .server_state import DatabaseArticleLedger
        database = DatabaseArticleLedger(self).load()
        if database is not None:
            return database
        try:
            value = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.exists() else {"records": []}
            if set(value) != {"records"} or not isinstance(value["records"], list):
                raise ValueError
            if any(not isinstance(x, dict) or not {"id", "slot", "state"} <= set(x) for x in value["records"]):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError("ARTICLE_LEDGER_INVALID", 409) from exc

    def _policies(self, enabled=True):
        from .article_sources import registered_policies
        return registered_policies(self._policy(enabled=enabled), self.core.settings)

    def _policy_for_article(self, article):
        from .article_sources import policy_for_article
        return policy_for_article(self._policy(), self.core.settings, article)

    def _policy_for_record(self, record):
        from .article_sources import policy_for_record
        return policy_for_record(self._policy(), self.core.settings, record)

    def _policy_for_source(self, source_id, enabled=True):
        for policy in self._policies(enabled=enabled):
            if policy["source_id"] == source_id:
                return policy
        raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)

    def _save(self, ledger):
        from .server_state import DatabaseArticleLedger
        if DatabaseArticleLedger(self).save(ledger):
            return
        temporary = self.ledger_path.with_suffix(".new")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(ledger, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.ledger_path)
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _source_target(self, policy):
        source = self.core.settings.source(policy["source_id"])
        rights = source["rights"]
        if (not source["enabled"] or not rights["verified"] or not rights["allow_storage"]
                or not rights["allow_derivative"] or rights["evidence_ref"] != policy["rights_evidence"]):
            raise AgentError("ARTICLE_RIGHTS_REQUIRED", 409)
        with self.core.db.session() as session:
            target = self.core._target(session, policy["target_id"])
            if (not target.enabled or target.platform != "max" or target.route != "max_browser"
                    or not self.core.bridge.enabled or not target.options.get("postiz_integration_id")
                    or target.profile_id not in source["channel_ids"]):
                raise AgentError("ARTICLE_TARGET_INVALID", 409)
            profile = self.core.settings.channel(target.profile_id)
            if not profile["enabled"] or profile["publication"]["mode"] == "dry_run":
                raise AgentError("PROFILE_NOT_PUBLISHABLE", 409)
            return source, target

    def _article(self, article, policy, source, target):
        from .article_sources import source_spec, validate_source_url
        if policy["source_id"] in {"noirlab_iotw", "esahubble_potw"}:
            spec = source_spec(policy["source_id"])
            if (policy["allowed_hosts"] != [spec["host"]]
                    or policy["rights_evidence"] != spec["rights_evidence"]
                    or source["id"] != policy["source_id"]):
                raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
            validate_source_url(policy["source_id"], article.get("url"))
        try:
            if set(article) != {"url", "title", "original", "published_at"}:
                raise ValueError
            parsed = urlsplit(article["url"])
            if (parsed.scheme != "https" or parsed.hostname not in policy["allowed_hosts"]
                    or parsed.username or parsed.password or parsed.port not in {None, 443}
                    or parsed.query or parsed.fragment or not parsed.path):
                raise ValueError
            if (not all(isinstance(article[k], str) and article[k].strip() for k in ("title", "original"))
                    or len(article["title"] + article["original"]) > 9000
                    or type(article["published_at"]) not in {int, float}
                    or not math.isfinite(article["published_at"])):
                raise ValueError
            url = urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AgentError("ARTICLE_INPUT_INVALID") from exc
        if (article["published_at"] > now() + 300
                or article["published_at"] < now() - policy["max_source_age_hours"] * 3600):
            raise AgentError("STALE_SOURCE", 409)
        return {**article, "url": url}

    def _clear_outstanding(self, target_id, own_variant=None):
        with self.core.db.session() as session:
            query = select(Dispatch).join(Variant).where(Variant.target_id == target_id,
                                                       Dispatch.state.in_(ACTIVE))
            if own_variant:
                query = query.where(Dispatch.variant_id != own_variant)
            if session.scalar(query) or session.scalar(select(BridgePost).where(
                    BridgePost.target_id == target_id, BridgePost.mutation_owner.is_not(None))):
                raise AgentError("ARTICLE_OUTSTANDING_DELIVERY", 409)

    def _cadence(self, policy, stamp, check_gap=True):
        with self.core.db.session() as session:
            attempts = list(session.scalars(select(DispatchAttempt).join(Dispatch).join(Variant).where(
                Variant.target_id == policy["target_id"], DispatchAttempt.result == "published")))
            # Include verified deliveries whose historical attempt row is unavailable.
            deliveries = list(session.scalars(select(Dispatch).join(Variant).where(
                Variant.target_id == policy["target_id"], Dispatch.state == "published")))
            known = {a.dispatch_id for a in attempts}
            if any(d.id not in known for d in deliveries):
                raise AgentError("ARTICLE_PUBLISH_TIME_UNKNOWN", 409)
        times = [a.created_at for a in attempts]
        earliest = max(times) + policy["min_gap_seconds"] + 1 if times else 0
        if check_gap and stamp < earliest:
            raise AgentError("ARTICLE_HOURLY_GAP", 409)
        zone = ZoneInfo("Europe/Moscow")
        day = datetime.fromtimestamp(stamp, zone).date()
        if sum(datetime.fromtimestamp(value, zone).date() == day for value in times) >= policy["max_per_day"]:
            raise AgentError("ARTICLE_DAILY_LIMIT", 409)
        return earliest

    def _trace_writer(self, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", record_id):
            raise AgentError("ARTICLE_RECORD_ID_INVALID")
        path = inside(self.core.settings.root, f"data/hourly-articles/quality-{record_id}.local.jsonl")

        def write_event(event):
            if not isinstance(event, dict):
                raise AgentError("ARTICLE_TRACE_INVALID")
            encoded = json.dumps({**event, "at": now(), "record_id": record_id}, ensure_ascii=False) + "\n"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())

        return write_event

    def _quality_reference_contract(self, trace_id):
        """Найти версию только по durable владельцу записи или отдельного trace."""
        expected = f"data/hourly-articles/quality-{trace_id}.local.jsonl"
        owners = []
        for item in self._load()["records"]:
            owned = trace_id is not None and (item["id"] == trace_id or item.get("quality_trace") == expected)
            for name in ("review_protocol_recoveries", "automatic_corrections",
                         "automatic_spelling_recoveries", "automatic_roni_spelling_recoveries"):
                history = item.get(name, [])
                if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
                    raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                owned |= trace_id is not None and any(entry.get("quality_trace") == expected for entry in history)
            if owned:
                owners.append(item)
        if len(owners) > 1:
            raise AgentError("ARTICLE_REFERENCE_CONTRACT_INVALID", 409)
        # Старые записи и отдельные recovery traces не получают новую семантику
        # ссылок только из-за обновления приложения.
        record = owners[0] if owners else {}
        reference_contract = record.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
        if not isinstance(reference_contract, str) or reference_contract not in REFERENCE_CONTRACTS:
            raise AgentError("ARTICLE_REFERENCE_CONTRACT_INVALID", 409)
        return reference_contract

    def _quality(self, record_id=None, review_only=False):
        if self.quality_factory:
            return self.quality_factory()
        from .article_quality import ArticleQuality
        from .translation import AUTOMATIC_TRANSLATION_MODELS, DedicatedTranslator, configured_translator
        reference_contract = self._quality_reference_contract(record_id)
        records = [r for r in self._load()["records"] if r.get("id") == record_id]
        if len(records) > 1:
            raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID", 409)
        scientific_contract = None
        if records:
            with self.core.db.session() as session:
                scientific_contract = scientific_contract_binding(self.core, session, records[0])
                review_rubric_binding(self.core, session, records[0])
        with ExitStack() as cleanup:
            if review_only:
                from .human_review import CandidateOnlyTranslator
                translator = CandidateOnlyTranslator()
            else:
                translator = configured_translator(self.core.settings)
            if translator is not None:
                cleanup.callback(translator.client.close)
            policy_path = self.core.settings.root / "data/hourly-article-policy.local.json"
            mode = self._policy(enabled=False).get("approval_mode") if policy_path.exists() else "legacy"
            human_mode = mode == "human"
            if mode in {"human", "automatic"} and not review_only:
                # Новый автоматический маршрут требует явной конфигурации исправленного
                # MADLAD либо прежнего Hy; человеческий маршрут сохраняет свой контракт.
                allowed = AUTOMATIC_TRANSLATION_MODELS if mode == "automatic" else {"hy-mt2-7b-q8"}
                if not isinstance(translator, DedicatedTranslator) or translator.model not in allowed:
                    code = ("ARTICLE_AUTOMATIC_TRANSLATOR_REQUIRED" if mode == "automatic"
                            else "HUMAN_HY_TRANSLATOR_REQUIRED")
                    raise AgentError(code, 409)
            if translator is None:
                translator = OpenCode(self.core.settings)
                translator.model = "proxmox/qwen35-9b"
                cleanup.callback(translator.client.close)
            reviewer = OpenCode(self.core.settings)
            cleanup.callback(reviewer.client.close)
            reviewer.model = "proxmox/qwen25-coder-7b"
            quality = ArticleQuality(translator, reviewer, prompt_dir=self.core.settings.root / "prompts",
                                     trace=self._trace_writer(record_id) if record_id else None,
                                     allow_human_rejection=human_mode,
                                     review_rubric=(automatic_review_rubric(records[0]) if records else
                                         self._policy(enabled=False)["review_rubric"]) if mode == "automatic" else "legacy",
                                     reference_contract=reference_contract,
                                     review_protocol_contract=(records[0].get("review_protocol_contract")
                                                               if records else None),
                                     allow_registered_terms=scientific_contract is not None,
                                     **({"presentation_contract": records[0]["presentation_contract"],
                                         "image_credit": records[0]["image_credit"]}
                                        if records and records[0].get("presentation_contract") else {}))
            cleanup.pop_all()
            return quality

    @staticmethod
    def _quality_provenance(quality, operator_candidate=False):
        from .translation import DedicatedTranslator
        if operator_candidate:
            return {"quality": "operator_ministral_languagetool"}
        translator = getattr(quality, "translator", None)
        if isinstance(translator, DedicatedTranslator):
            return {"quality": "dedicated_translation_independent_review_languagetool",
                    "translation_model": translator.model,
                    "review_model": getattr(quality.reviewer, "model", None)}
        return {"quality": "qwen_ministral_languagetool"}

    def automatic_repair_status(self, record):
        from .article_repair_cycle import AutomaticArticleRepair
        return AutomaticArticleRepair(self).status(record)

    def repair_rejected(self, ident):
        from .article_repair_cycle import AutomaticArticleRepair
        return AutomaticArticleRepair(self).run(ident)

    def _repair(self, trace, *, presentation_contract=None, image_credit=None):
        from .article_repair import ArticleRepair, REPAIR_MODEL
        from .article_attribution import presentation_binding
        presentation = presentation_binding(presentation_contract, image_credit)
        if self.repair_factory:
            return self.repair_factory(trace, **presentation)
        editor = OpenCode(self.core.settings)
        editor.model = REPAIR_MODEL
        return ArticleRepair(editor, prompt_dir=self.core.settings.root / "prompts", trace=trace, **presentation)

    def _assert_pre_draft(self, record):
        if any(record.get(key) for key in ("variant_id", "postiz_id", "dispatch_id", "due_at", "prepared_hash")):
            raise AgentError("ARTICLE_PREPARATION_HAS_EXTERNAL_STATE", 409)
        with self.core.db.session() as session:
            if (session.scalar(select(Story).where(Story.text_hash == fingerprint({"hourly_article": record["id"]})))
                    or any(v.metadata_json.get("record_id") == record["id"]
                           for v in session.scalars(select(Variant)))):
                raise AgentError("ARTICLE_PREPARATION_HAS_EXTERNAL_STATE", 409)

    def _unpublished_variant(self, session, record):
        variant = self.core._variant(session, record["variant_id"], True)
        target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
        if (variant.target_id != record["target_id"]
                or variant.metadata_json.get("origin") != "hourly_article_test"
                or variant.metadata_json.get("record_id") != record["id"]
                or session.get(RemoteDraft, variant.id)
                or session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
                or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
                or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id,
                                                              TargetApproval.revoked.is_(False)))
                or session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
                                                          BridgePost.mutation_owner.is_not(None)))):
            raise AgentError("ARTICLE_PREPARED_HAS_EXTERNAL_STATE", 409)
        return variant, target

    def _record(self, ledger, ident):
        record = next((item for item in ledger["records"] if item["id"] == ident), None)
        if not record:
            raise AgentError("ARTICLE_RECORD_NOT_FOUND", 404)
        return record

    def _view(self, record):
        result = copy.deepcopy(record)
        if record.get("variant_id"):
            with self.core.db.session() as session:
                variant = self.core._variant(session, record["variant_id"])
                target = self.core._target(session, variant.target_id)
                result.update(title=variant.title, body=variant.body, revision=variant.revision,
                              content_hash=fingerprint(self.core.snapshot(variant, target)))
        return result

    def _snapshot(self, record):
        with self.core.db.session() as session:
            variant = self.core._variant(session, record["variant_id"])
            target = self.core._target(session, variant.target_id)
            return copy.deepcopy(self.core.snapshot(variant, target))

    @staticmethod
    def _reviewed_fields(snapshot):
        value = copy.deepcopy(snapshot)
        value.pop("revision")
        for key in ("postiz_post_id", "postiz_hash", "postiz_due_at", "postiz_state"):
            value["metadata"].pop(key, None)
        return value

    @staticmethod
    def _validate_correction(correction):
        if (not isinstance(correction, dict) or set(correction) != {"title", "body"}
                or any(not isinstance(v, str) or not v.strip() for v in correction.values())
                or len(correction["title"]) > 500 or len(correction["body"]) > 3500
                or "Источник:" in correction["body"]
                or "Перевод и редактура с помощью нейросетей." in correction["body"]):
            raise AgentError("ARTICLE_CORRECTION_INVALID")

    @staticmethod
    def _candidate_hash(candidate):
        from .article_quality import normalize_article_links
        return fingerprint({key: normalize_article_links(candidate[key].strip()) for key in ("title", "body")})

    def _legacy_candidate_hashes(self, record, attempt, hashes, *, replay=False):
        """Read one old checked candidate; missing provenance cannot permit a retry."""
        try:
            reference_contract = record.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
            if (not isinstance(reference_contract, str) or reference_contract not in REFERENCE_CONTRACTS
                    or attempt.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract):
                raise ValueError
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            if record.get("quality_trace") != expected:
                raise ValueError
            raw = inside(self.core.settings.root, expected).read_bytes()
            if not 0 < len(raw) <= 2 * 1024 * 1024:
                raise ValueError
            started, finished = attempt["started_at"], attempt["finished_at"]
            if any(type(value) not in {int, float} or not math.isfinite(value) for value in (started, finished)) or started > finished:
                raise ValueError
            found = set()
            for line in raw.decode("utf-8").splitlines():
                event = parse_json(line)
                if (not isinstance(event, dict) or event.get("record_id") != record["id"]
                        or event.get("phase") != "candidate_started"
                        or type(event.get("at")) not in {int, float}
                        or not started <= event["at"] <= finished):
                    continue
                candidate, source = event.get("candidate"), event.get("source_untrusted", {})
                if (not isinstance(candidate, dict) or set(candidate) != {"title", "body"}
                        or any(not isinstance(value, str) or not value.strip() for value in candidate.values())
                        or event.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract):
                    raise ValueError
                supplied = event.get("candidate_raw", candidate)
                if fingerprint(candidate) not in hashes and fingerprint(supplied) not in hashes:
                    continue
                if (not isinstance(supplied, dict) or set(supplied) != {"title", "body"}
                        or any(not isinstance(value, str) or not value.strip() for value in supplied.values())
                        or self._candidate_hash(supplied) != self._candidate_hash(candidate)
                        or not isinstance(source, dict) or source.get("url") != record["url"]
                        or not isinstance(source.get("body"), str) or text_hash(source["body"]) != record["original_hash"]):
                    raise ValueError
                found.add(self._initial_replay_hash(candidate) if replay else self._candidate_hash(candidate))
            if len(found) != 1:
                raise ValueError
            return found
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, AgentError) as exc:
            raise AgentError("ARTICLE_REJECTED_CANDIDATE_TRACE_REQUIRED", 409) from exc

    @staticmethod
    def _initial_replay_hash(candidate):
        from .article_quality import normalize_article_links
        # Это только запрет повтора: сохранённые quality/content hashes не меняем.
        return fingerprint({key: " ".join(normalize_article_links(candidate[key].strip()).split())
                            for key in ("title", "body")})

    def _load_canonical_initial_candidates(self, record, attempt, article=None):
        """Доказать исходные отклонённые тексты, не запрашивая модель повторно."""
        from .adapters import extract_object
        from .article_quality import article_review_context, normalize_article_links
        from .editorial import numbers

        try:
            reference_contract = record.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
            if (not isinstance(reference_contract, str) or reference_contract not in REFERENCE_CONTRACTS
                    or attempt.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract):
                raise ValueError
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            if (record.get("quality_trace") != expected
                    or not re.fullmatch(r"[a-f0-9-]{36}", record["id"])
                    or attempt.get("state") not in {"needs_attention", "rejected", "failed"}
                    or attempt.get("error") != "ARTICLE_QUALITY_REJECTED"
                    or type(attempt.get("number")) is not int or attempt["number"] < 1):
                raise ValueError
            attempts = record.get("preparation_attempts")
            if (not isinstance(attempts, list) or attempt["number"] > len(attempts)
                    or attempts[attempt["number"] - 1] != attempt):
                raise ValueError
            started, finished = attempt["started_at"], attempt["finished_at"]
            if (any(type(value) not in {int, float} or not math.isfinite(value) or value < 0
                    for value in (started, finished)) or started > finished):
                raise ValueError
            if article is None and record.get("variant_id"):
                # После staging исходный input берём из его собственного Material,
                # а не из текста нового оператора или несвязанного trace.
                with self.core.db.session() as session:
                    variant = session.get(Variant, record["variant_id"])
                    if (not variant or variant.metadata_json.get("record_id") != record["id"]
                            or len(variant.sources) != 1):
                        raise ValueError
                    material = session.get(Material, variant.sources[0]["id"])
                    if not material or material.published_at != record["source_published_at"]:
                        raise ValueError
                    article = {"url": material.url, "title": material.title, "original": material.text,
                               "published_at": record["source_published_at"]}
            raw = inside(self.core.settings.root, expected).read_bytes()
            if not 0 < len(raw) <= 2 * 1024 * 1024:
                raise ValueError
            lines = raw.decode("utf-8").splitlines()
            if not lines or len(lines) > 10000 or any(not line.strip() for line in lines):
                raise ValueError
            events, previous = [], -1
            for line in lines:
                event = parse_json(line)
                if (not isinstance(event, dict) or event.get("record_id") != record["id"]
                        or type(event.get("at")) not in {int, float}
                        or not math.isfinite(event["at"]) or event["at"] < previous
                        or type(event.get("attempt")) is not int or event["attempt"] < 1):
                    raise ValueError
                previous = event["at"]
                events.append(event)
            # Счётчик внутренних попыток начинается заново у каждого вызова.
            # Границы по фазам нужны даже при одинаковых timestamp в двух вызовах.
            segments = []
            for index, event in enumerate(events):
                if (event.get("phase") != "translation_started" or event["attempt"] != 1
                        or not started <= event["at"] <= finished):
                    continue
                segment = []
                for current in events[index:]:
                    if segment and current.get("phase") in {"translation_started", "candidate_started"} and current["attempt"] == 1:
                        break
                    if not started <= current["at"] <= finished:
                        break
                    segment.append(current)
                    if current.get("phase") in {"accepted", "rejected_for_human", "rejected"}:
                        break
                if segment[-1].get("phase") == "rejected":
                    segments.append(segment)
            if len(segments) != 1:
                raise ValueError
            segment = segments[0]
            terminal = segment[-1]
            if (terminal.get("error") != "ARTICLE_QUALITY_REJECTED" or terminal.get("mode") is not None
                    or terminal.get("scientific_spelling_contract") != record.get("scientific_spelling_contract")
                    or terminal["attempt"] > 3):
                raise ValueError
            prompt = (self.core.settings.root / "prompts/ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8")
            found, position = set(), 0
            for number in range(1, terminal["attempt"] + 1):
                block = []
                while position < len(segment) - 1 and segment[position]["attempt"] == number:
                    block.append(segment[position])
                    position += 1
                phases = [event.get("phase") for event in block]
                if phases[:3] != ["translation_started", "translation_returned", "translation_parsed"]:
                    raise ValueError
                context = block[0].get("context")
                source = context.get("source_untrusted") if isinstance(context, dict) else None
                if (not isinstance(source, dict) or set(source) != {"title", "body", "url"}
                        or any(not isinstance(value, str) or not value.strip() for value in source.values())):
                    raise ValueError
                bound_article = article if article is not None else {
                    "url": source["url"], "title": source["title"], "original": source["body"],
                    "published_at": record["source_published_at"]}
                if (set(bound_article) != {"url", "title", "original", "published_at"}
                        or fingerprint(bound_article) != record["input_hash"]
                        or bound_article["url"] != record["url"]
                        or bound_article["published_at"] != record["source_published_at"]
                        or text_hash(bound_article["original"]) != record["original_hash"]
                        or source != {"url": bound_article["url"], "title": bound_article["title"].strip(),
                                      "body": bound_article["original"].strip()}
                        or context.get("instruction") != prompt
                        or context.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract
                        or context.get("scientific_spelling_contract") != record.get("scientific_spelling_contract")
                        or context.get("numeric_tokens_must_match_exactly") != sorted(numbers(source["title"] + "\n\n" + source["body"]))):
                    raise ValueError
                if not isinstance(block[1].get("raw"), str):
                    raise ValueError
                translated = extract_object(block[1]["raw"])
                if (set(translated) != {"title", "body"}
                        or any(not isinstance(value, str) or not value.strip() for value in translated.values())):
                    raise ValueError
                translated = {key: normalize_article_links(value.strip()) for key, value in translated.items()}
                if block[2].get("translation") != translated or phases[-1] != "retry":
                    raise ValueError
                tail = phases[3:]
                if tail not in (["gate_rejected", "retry"], ["gate_passed", "candidate_repeated", "retry"]):
                    # Здесь устанавливается только факт отказа, а не истинность
                    # машинных замечаний. Raw review и LT не меняются и не одобряются.
                    if "review_normalized" in tail:
                        tail = [phase for phase in tail if phase != "review_normalized"]
                    if tail not in (
                        ["gate_passed", "review_started", "review_invalid", "retry"],
                        ["gate_passed", "review_started", "review_returned", "review_invalid", "retry"],
                        ["gate_passed", "review_started", "review_returned", "review_parsed", "spelling_started",
                         "spelling_returned", "spelling_adjudicated", "retry"],
                    ):
                        raise ValueError
                    review = next(event.get("context") for event in block if event.get("phase") == "review_started")
                    if (not isinstance(review, dict) or review.get("source_untrusted") != source["title"] + "\n\n" + source["body"]
                            or review.get("translation_untrusted") != translated["title"] + "\n\n" + translated["body"]):
                        raise ValueError
                    if reference_contract == REFERENCE_CONTRACT:
                        review_prompt = (self.core.settings.root / "prompts" / automatic_review_prompt_name(record)).read_text(encoding="utf-8")
                        if review != article_review_context(review_prompt,
                                source["title"] + "\n\n" + source["body"],
                                translated["title"] + "\n\n" + translated["body"], automatic_review_rubric(record),
                                hashlib.sha256(review_prompt.encode("utf-8")).hexdigest(),
                                reference_contract=reference_contract,
                                review_protocol_contract=record.get("review_protocol_contract")):
                            raise ValueError
                if terminal["attempt"] == number and terminal.get("feedback") != block[-1].get("feedback"):
                    raise ValueError
                found.add(self._initial_replay_hash(translated))
            if position != len(segment) - 1 or not found:
                raise ValueError
            return found
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, AgentError, RecursionError) as exc:
            raise AgentError("ARTICLE_REJECTED_CANDIDATE_TRACE_REQUIRED", 409) from exc

    def _guard_candidate_replay(self, ledger, record, candidate, operation, article=None):
        """A persisted substantive refusal cannot be replaced by a fresh model sample."""
        candidate_hash = self._candidate_hash(candidate)
        supplied_hash = fingerprint(candidate)
        rejected = False
        for name in ("preparation_attempts", "correction_attempts"):
            attempts = record.get(name, [])
            if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                raise AgentError("ARTICLE_LEDGER_INVALID", 409)
            for attempt in attempts:
                # Infrastructure errors, including LanguageTool unavailability, are
                # retryable. Their state may say 'rejected'; only this code is final.
                if attempt.get("error") != "ARTICLE_QUALITY_REJECTED":
                    continue
                hashes = [attempt[key] for key in ("candidate_hash", "candidate_normalized_hash", "candidate_raw_hash")
                          if key in attempt]
                if not hashes:
                    if name == "correction_attempts" or attempt.get("operator_correction"):
                        raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                    # Первичный Hy/Qwen не получал operator candidate_hash. Отказ
                    # всё равно связан с фактическим текстом канонического trace.
                    rejected |= self._initial_replay_hash(candidate) in self._load_canonical_initial_candidates(record, attempt, article)
                    continue
                if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                       for value in hashes):
                    raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                if candidate_hash in hashes or supplied_hash in hashes:
                    rejected = True
                normalization = attempt.get("candidate_normalization")
                if normalization not in {None, "article-links-v1"}:
                    raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                replay_hash = attempt.get("candidate_replay_hash")
                if replay_hash is not None and (not isinstance(replay_hash, str)
                                               or not re.fullmatch(r"[a-f0-9]{64}", replay_hash)):
                    raise AgentError("ARTICLE_LEDGER_INVALID", 409)
                if normalization is None and not rejected:
                    rejected = candidate_hash in self._legacy_candidate_hashes(record, attempt, hashes)
                if not rejected:
                    replay_hashes = ({replay_hash} if normalization == "article-links-v1" and replay_hash else
                                     self._legacy_candidate_hashes(record, attempt, hashes, replay=True))
                    rejected = self._initial_replay_hash(candidate) in replay_hashes
        if rejected:
            record.setdefault("preparation_actions", []).append({
                "at": now(), "action": "rejected_candidate_replay_blocked", "operation": operation,
                "candidate_normalized_hash": candidate_hash, "error": "ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"})
            self._save(ledger)
            with self.core.db.session.begin() as session:
                self.core.db.audit(session, "article_quality", "hourly_article_candidate_replay_blocked",
                    record["id"], operation=operation, candidate_normalized_hash=candidate_hash,
                    source_hash=record["original_hash"], error="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED")
            raise AgentError("ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED", 409)

    def prepare(self, article, retry_record=None, candidate=None, automatic_repair_claim=None,
                *, reserve_claim=None):
        if reserve_claim is not None and not self.reserve_mode:
            raise AgentError("ARTICLE_RESERVE_MODE_REQUIRED", 409)
        if automatic_repair_claim and (not retry_record or candidate is None):
            raise AgentError("ARTICLE_AUTOMATIC_REPAIR_CLAIM_LOST", 409)
        if candidate is not None:
            if not retry_record:
                raise AgentError("ARTICLE_CANDIDATE_RETRY_REQUIRED", 409)
            self._validate_correction(candidate)
        with profile_lock(self._preparation_lock()):
            from .server_state import require_running
            require_running(self.core)
            policy = self._policy_for_article(article)
            source, target = self._source_target(policy)
            article = self._article(article, policy, source, target)
            if self.reserve_mode:
                from .article_reserve import ArticleReserveStore
                if policy.get("approval_mode") != "automatic":
                    raise AgentError("ARTICLE_AUTOMATIC_POLICY_REQUIRED", 409)
                if retry_record:
                    old = self._record(self._load(), retry_record)
                    reserve_claim = self._reserve_record(old, active=True)
                ArticleReserveStore(self.core).require_preparation(reserve_claim)
                # Подготовка не назначает дату и может идти при известной QUEUE.
                if not retry_record:
                    with self.core.db.session() as session:
                        if (session.scalar(select(Dispatch).where(Dispatch.state.in_({"sending", "unknown"})
                                | Dispatch.owner.is_not(None) | (Dispatch.lease_until != 0)))
                                or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                                    | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))):
                            raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            else:
                self._clear_outstanding(target.id)
                self._cadence(policy, now(), check_gap=False)
            ledger, slot = self._load(), int(now() // 3600)
            original_hash = text_hash(article["original"])
            material_hash = fingerprint(" ".join(article["original"].casefold().split()))
            if retry_record:
                record = self._record(ledger, retry_record)
                if record.get("reserve_contract") is not None and not self.reserve_mode:
                    raise AgentError("ARTICLE_RESERVE_MODE_REQUIRED", 409)
                if (record["state"] not in {"needs_attention", "rejected", "failed"}
                        or any(record.get(key) for key in ("variant_id", "postiz_id", "dispatch_id", "due_at", "prepared_hash"))):
                    raise AgentError("ARTICLE_RETRY_NOT_SAFE", 409)
                if record["slot"] != slot and not self.reserve_mode:
                    raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                if (record["url"] != article["url"] or record["original_hash"] != original_hash
                        or record["policy_hash"] != fingerprint(policy)
                        or record["target_id"] != target.id or record["source_id"] != source["id"]
                        or record["source_published_at"] != article["published_at"]
                        or (record.get("input_hash") and record["input_hash"] != fingerprint(article))):
                    raise AgentError("ARTICLE_RETRY_INPUT_MISMATCH", 409)
                try:
                    self._assert_pre_draft(record)
                except AgentError as exc:
                    raise AgentError("ARTICLE_RETRY_NOT_SAFE", 409) from exc
                if candidate is None and (record.get("error") == "ARTICLE_QUALITY_REJECTED" or any(
                        item.get("error") == "ARTICLE_QUALITY_REJECTED"
                        for item in record.get("preparation_attempts", []))):
                    raise AgentError("ARTICLE_CORRECTION_CANDIDATE_REQUIRED", 409)
            else:
                record = None
                if not self.reserve_mode and any(item["slot"] == slot or item.get("automatic_slot") == slot for item in ledger["records"]):
                    raise AgentError("ARTICLE_SLOT_ALREADY_CLAIMED", 409)
            other_records = [item for item in ledger["records"] if item is not record]
            if (policy.get("approval_mode") in {"human", "automatic"}
                    and sum(item["state"] in {"awaiting_human_review", "quality_ready", "automatic_ready", "pending", "sending",
                                              "unknown", "awaiting_postiz"} for item in other_records) >= 24):
                raise AgentError("ARTICLE_AUTOMATIC_BACKLOG" if policy["approval_mode"] == "automatic"
                                 else "ARTICLE_HUMAN_REVIEW_BACKLOG", 409)
            if any(item["state"] not in {"published", "failed", "cancelled", "rejected", "awaiting_human_review"}
                   and not (item["state"] == "automatic_ready" and item.get("automatic_enrolled"))
                   and not (self.reserve_mode and self._reserve_other_safe(item))
                   for item in other_records):
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            if any(item.get("url") == article["url"] or item.get("original_hash") == original_hash for item in other_records):
                raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            with self.core.db.session() as session:
                if session.scalar(select(Material).where(or_(Material.url == article["url"],
                        Material.text_hash.in_({original_hash, material_hash})))):
                    raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            if record is None:
                record = {"id": uid(), "slot": slot, "state": "preparing", "claimed_at": now(),
                          "url": article["url"], "original_hash": original_hash, "policy_hash": fingerprint(policy),
                          "target_id": target.id, "source_id": source["id"], "source_published_at": article["published_at"],
                          "input_hash": fingerprint(article), "preparation_attempts": []}
                if illustrated_presentation(policy.get("presentation_contract")):
                    # Формат закреплён до загрузки изображения и первого вызова модели.
                    record["presentation_contract"] = policy["presentation_contract"]
                from .article_quality import PRACTICAL_REVIEW_RUBRICS
                if policy.get("review_rubric") in PRACTICAL_REVIEW_RUBRICS:
                    # Новая рубрика выбирается только явной политикой до первой модели.
                    record["review_rubric"] = policy["review_rubric"]
                from .article_quality import HOURLY_PRACTICAL_V2_REVIEW_RUBRIC, REVIEW_BOUNDS_V2_CONTRACT
                if (policy.get("approval_mode") == "automatic"
                        and policy.get("review_rubric") == HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
                        and (getattr(self, "review_protocol_contract", None) == REVIEW_BOUNDS_V2_CONTRACT
                             or not self.quality_factory and getattr(self._quality, "__func__", None)
                             is HourlyArticleCycle._quality)):
                    record["review_protocol_contract"] = REVIEW_BOUNDS_V2_CONTRACT
                if self.reserve_mode:
                    from .article_reserve import ArticleReserveStore
                    ArticleReserveStore(self.core).bind_preparation(reserve_claim, record["id"])
                    record.update(reserve_contract="article-reserve-v1", reserve_claim=copy.deepcopy(reserve_claim))
                    binding = {key: record.get(key) for key in ("id", "reserve_contract", "reserve_claim", "input_hash",
                        "policy_hash", "target_id", "source_id", "original_hash", "source_published_at")}
                    record["reserve_binding_hash"] = fingerprint(binding)
                    with self.core.db.session.begin() as session:
                        session.add(Audit(id=str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record["id"])),
                            actor="article_reserve", action="article_reserve_preparation_binding", entity=record["id"],
                            details={"binding": binding, "binding_hash": record["reserve_binding_hash"]}))
                # Версия контракта закрепляется до Hy и сохраняется при любой
                # последующей коррекции; старые записи без поля остаются V1.
                if policy.get("approval_mode") == "automatic" and REFERENCE_CONTRACT != LEGACY_REFERENCE_CONTRACT:
                    record["reference_contract"] = REFERENCE_CONTRACT
                if policy.get("approval_mode") == "automatic" and (
                        self.scientific_spelling_contract is not None
                        or not self.quality_factory and getattr(self._quality, "__func__", None) is HourlyArticleCycle._quality):
                    from .scientific_terms import SCIENTIFIC_SPELLING_CONTRACT
                    record["scientific_spelling_contract"] = SCIENTIFIC_SPELLING_CONTRACT
                ledger["records"].append(record)
                from .article_repair_cycle import capture_input
                capture_input(self, record, article)
            elif "preparation_attempts" not in record:
                # Preserve older failed ledgers without inventing an unrecorded completion time.
                record["preparation_attempts"] = [{"number": 1, "started_at": record["claimed_at"],
                    "state": record["state"], "error": record.get("error"), "legacy_record": True}]
            from .article_repair_cycle import AutomaticArticleRepair
            repair_protocol = AutomaticArticleRepair(self)
            repair_claim = repair_protocol.authorize(record, automatic_repair_claim, candidate, article)
            if candidate is not None:
                self._guard_candidate_replay(ledger, record, candidate, "prepare_candidate", article)
            attempt = {"number": len(record["preparation_attempts"]) + 1,
                       "started_at": now(), "state": "preparing", "operator_retry": bool(retry_record) and not repair_claim}
            if "review_rubric" in record:
                attempt["review_rubric"] = automatic_review_rubric(record)
            if "review_protocol_contract" in record:
                attempt["review_protocol_contract"] = record["review_protocol_contract"]
            if "reference_contract" in record:
                if (not isinstance(record["reference_contract"], str)
                        or record["reference_contract"] not in REFERENCE_CONTRACTS):
                    raise AgentError("ARTICLE_REFERENCE_CONTRACT_INVALID", 409)
                attempt["reference_contract"] = record["reference_contract"]
            if "scientific_spelling_contract" in record:
                from .scientific_terms import SCIENTIFIC_SPELLING_CONTRACT
                if record["scientific_spelling_contract"] != SCIENTIFIC_SPELLING_CONTRACT:
                    raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_CONTRACT_INVALID", 409)
                attempt["scientific_spelling_contract"] = record["scientific_spelling_contract"]
            if "presentation_contract" in record:
                if record["presentation_contract"] != policy.get("presentation_contract"):
                    raise AgentError("ARTICLE_PRESENTATION_BINDING_INVALID", 409)
                attempt["presentation_contract"] = record["presentation_contract"]
            if candidate is not None:
                attempt.update(candidate_hash=self._candidate_hash(candidate), operator_correction=not bool(repair_claim),
                               candidate_normalization="article-links-v1",
                               candidate_replay_hash=self._initial_replay_hash(candidate),
                               candidate_raw_hash=fingerprint(candidate))
                if repair_claim:
                    attempt["automatic_repair_claim"] = repair_claim["id"]
            record["preparation_attempts"].append(attempt)
            record.update(state="preparing", input_hash=fingerprint(article))
            record["quality_trace"] = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            self._save(ledger)
            quality = None
            try:
                if illustrated_presentation(record.get("presentation_contract")):
                    from .article_images import prepare_article_image, validate_article_image
                    attempt["image_preparation_state"] = "preparing"
                    self._save(ledger)
                    binding = prepare_article_image(self.core, article, record, policy)
                    record.update(illustration=copy.deepcopy(binding), image_credit=binding["credit"],
                                  illustration_proof_hash=fingerprint(binding))
                    validate_article_image(self.core, record)
                    attempt.update(image_credit=record["image_credit"],
                                   illustration_proof_hash=record["illustration_proof_hash"],
                                   image_preparation_state="ready")
                    self._save(ledger)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "automatic_article_repair" if repair_claim else
                        ("hourly_article_operator" if retry_record else "article_quality"),
                        "hourly_article_prepare_attempt", record["id"], attempt=attempt["number"],
                        operator_retry=attempt["operator_retry"], source_hash=original_hash,
                        input_hash=record["input_hash"], policy_hash=record["policy_hash"],
                        candidate_hash=attempt.get("candidate_hash"), operator_correction=attempt.get("operator_correction", False),
                        automatic_repair_claim=automatic_repair_claim,
                        **({"review_rubric": attempt["review_rubric"]} if "review_rubric" in attempt else {}),
                        **({"review_protocol_contract": attempt["review_protocol_contract"]}
                           if "review_protocol_contract" in attempt else {}),
                        **({"reference_contract": attempt["reference_contract"]}
                           if "reference_contract" in attempt else {}),
                        **({"scientific_spelling_contract": attempt["scientific_spelling_contract"]}
                           if "scientific_spelling_contract" in attempt else {}),
                        **({key: attempt[key] for key in ("presentation_contract", "image_credit",
                            "illustration_proof_hash")} if "presentation_contract" in attempt else {}))
                quality = self._quality(record["id"])
                if illustrated_presentation(record.get("presentation_contract")):
                    attempt["model_started_at"] = now()
                    self._save(ledger)
                if getattr(quality, "scientific_spelling_contract", None) != record.get("scientific_spelling_contract"):
                    raise AgentError("ARTICLE_SCIENTIFIC_SPELLING_BINDING_INVALID", 409)
                prepared = (quality.check_candidate(candidate["title"], candidate["body"], article["title"],
                    article["original"], article["url"]) if candidate is not None else
                    quality.prepare(article["title"], article["original"], article["url"]))
                if self.reserve_mode:
                    self._reserve_record(record, active=True)
                elif record["slot"] != int(now() // 3600):
                    raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                provenance = self._quality_provenance(quality, operator_candidate=candidate is not None)
                if repair_claim:
                    repair_protocol.after_quality(record, repair_claim, article)
                    provenance = {"quality": "automatic_correction_ministral_languagetool",
                        "automatic_repair_claim": repair_claim["id"], "repair_contract": repair_claim["contract"],
                        "repair_model": repair_claim["model"], "repair_evidence": copy.deepcopy(repair_claim["evidence"])}
                return self._store_prepared(article, prepared, record, ledger, policy, source, target,
                    provenance, attempt, check_slot=not self.reserve_mode)
            except Exception as exc:
                record.update(state="needs_attention", error=exc.code if isinstance(exc, AgentError) else "ARTICLE_PREPARATION_INTERRUPTED")
                if attempt.get("image_preparation_state") == "preparing" and quality is None:
                    attempt["image_preparation_state"] = "failed"
                attempt.update(state="needs_attention", error=record["error"], finished_at=now())
                self._save(ledger)
                completion = None
                if (record["error"] == "ARTICLE_QUALITY_REJECTED"
                        and record.get("review_protocol_contract") == "article-review-bounds-v2"):
                    from .completed_review import completed_review_proof
                    try:
                        completion = completed_review_proof(self, record, article, require_anchor=False)
                    except AgentError:
                        # Неполный trace/таймаут не получают положительного terminal proof.
                        pass
                    else:
                        attempt["completed_review_protocol"] = completion
                        self._save(ledger)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "article_quality", "hourly_article_prepare_error", record["id"],
                        attempt=attempt["number"], error=record["error"],
                        **({"completed_review_protocol_hash": fingerprint(completion)} if completion else {}))
                raise
            finally:
                if quality:
                    from .human_review import close_quality
                    close_quality(quality, close_models=not self.quality_factory)

    def _store_prepared(self, article, prepared, record, ledger, policy, source, target,
                        provenance, attempt=None, *, check_slot=True):
        from .server_state import require_running
        require_running(self.core)
        attempt = attempt or {}
        if fingerprint(self._policy_for_record(record)) != record["policy_hash"]:
            raise AgentError("ARTICLE_POLICY_CHANGED", 409)
        self._source_target(policy)
        original_hash = record["original_hash"]
        material_hash = fingerprint(" ".join(article["original"].casefold().split()))
        if prepared["url"] != article["url"]:
            raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
        if (policy.get("approval_mode") == "automatic"
                and prepared.get("review_rubric") != automatic_review_rubric(record)):
            raise AgentError("ARTICLE_REVIEW_RUBRIC_BINDING_INVALID", 409)
        if prepared.get("review_protocol_contract") != record.get("review_protocol_contract"):
            raise AgentError("ARTICLE_REVIEW_PROTOCOL_BINDING_INVALID", 409)
        if (record.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
                != prepared.get("reference_contract", LEGACY_REFERENCE_CONTRACT)):
            raise AgentError("ARTICLE_REFERENCE_CONTRACT_INVALID", 409)
        presentation = record.get("presentation_contract")
        media = []
        if presentation is not None:
            from .article_images import validate_article_image
            if (not illustrated_presentation(presentation) or presentation != policy.get("presentation_contract")
                    or prepared.get("presentation_contract") != presentation
                    or prepared.get("image_credit") != record.get("image_credit")):
                raise AgentError("ARTICLE_PRESENTATION_BINDING_INVALID", 409)
            media = validate_article_image(self.core, record)
        elif any(prepared.get(key) is not None for key in ("presentation_contract", "image_credit")):
            raise AgentError("ARTICLE_PRESENTATION_BINDING_INVALID", 409)
        translation_hash = text_hash(prepared["translation"])
        if any(item.get("translation_hash") == translation_hash for item in ledger["records"]):
            raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
        with self.core.db.session.begin() as session:
            duplicates = session.scalars(select(Variant).where(Variant.target_id == target.id))
            if any(v.metadata_json.get("translation_hash") == translation_hash for v in duplicates):
                raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
            story = Story(title=article["title"], text=article["original"],
                          text_hash=fingerprint({"hourly_article": record["id"]}))
            session.add(story)
            session.flush()
            material = Material(source_id=source["id"], external_id=article["url"], story_id=story.id,
                title=article["title"], text=article["original"], url=article["url"],
                text_hash=material_hash, published_at=article["published_at"], language="en")
            session.add(material)
            session.flush()
            payload = {"title": prepared["title"], "body": prepared["body"], "language": "ru",
                "source_ids": [material.id], "decision": "review", "reason_codes": [],
                "claims": [], "confidence": 1.0, "payload": {}}
            draft = Draft(channel_id=target.profile_id, story_id=story.id, current=payload,
                config_id=self.core.settings.hash, state="article_review")
            session.add(draft)
            session.flush()
            session.add(DraftRevision(draft_id=draft.id, revision=1, payload=copy.deepcopy(payload),
                                     config_id=self.core.settings.hash))
            from .human_review import candidate_body, machine_metadata, text_hash as machine_text_hash
            if policy.get("approval_mode") == "automatic":
                from .automatic_policy import machine_metadata
            variant = Variant(draft_id=draft.id, target_id=target.id, title=prepared["title"],
                body=prepared["body"], sources=[{"id": material.id, "url": material.url, "source_id": source["id"]}],
                media=media, source_revision=1, config_hash=self.core.settings.hash,
                binding_hash=target.binding_hash, metadata_json={"origin": "hourly_article_test",
                    "record_id": record["id"], "rights_evidence": policy["rights_evidence"],
                    **({"reserve_contract": record["reserve_contract"],
                        "reserve_binding_hash": record["reserve_binding_hash"]}
                       if record.get("reserve_contract") == "article-reserve-v1" else {}),
                    **provenance,
                    "approval_mode": policy.get("approval_mode", "legacy"),
                    "policy_hash": record["policy_hash"],
                    **({"presentation_contract": presentation, "image_credit": record["image_credit"],
                        "illustration": copy.deepcopy(record["illustration"]),
                        "illustration_proof_hash": record["illustration_proof_hash"]} if presentation else {}),
                    "machine_text_hash": machine_text_hash(prepared["title"], prepared["body"]),
                    "machine_source_hash": fingerprint({"title": article["title"],
                        "body": article["original"], "url": article["url"]}),
                    "machine_reviewed_at": now(),
                    **machine_metadata(prepared, {"title": article["title"], "body": article["original"],
                        "url": article["url"]}, self.core.settings.root / record["quality_trace"]),
                    "operator_correction_hash": attempt.get("candidate_hash") if attempt.get("operator_correction") else None,
                    "automatic_correction_hash": attempt.get("candidate_hash") if attempt.get("automatic_repair_claim") else None,
                    "topic": prepared["topic"],
                    "translation_hash": translation_hash})
            session.add(variant)
            session.flush()
            if policy.get("approval_mode") == "automatic":
                review_rubric_binding(self.core, session, record, variant)
            scientific_contract_binding(self.core, session, record, variant)
            self.core._history(session, variant, target, "article_quality")
            # Generation and database work may cross the reserved hour.
            # Raising inside the transaction rolls back every generated entity.
            if check_slot and record["slot"] != int(now() // 3600):
                raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
            record.update(variant_id=variant.id, translation_hash=translation_hash,
                          prepared_hash=fingerprint(self.core.snapshot(variant, target)), state="prepared")
            self.core.db.audit(session, "article_quality", "hourly_article_prepared", variant.id,
                source_hash=original_hash, policy_hash=record["policy_hash"],
                candidate_hash=self._candidate_hash({"title": variant.title,
                    "body": candidate_body(variant, {"url": article["url"]})}),
                machine_review_status=variant.metadata_json.get("machine_review_status", "passed"),
                machine_review_evidence_hash=variant.metadata_json.get("machine_review_evidence_hash"),
                operator_correction=bool(attempt.get("operator_correction")),
                **({"reference_contract": record["reference_contract"]}
                   if "reference_contract" in record else {}))
        if self.reserve_mode and record.get("reserve_contract") == "article-reserve-v1":
            self._reserve_record(record, active=True)
            record.update(state="quality_ready", quality_ready_at=now(), reserve_prepared_hash=record["prepared_hash"])
        elif policy.get("approval_mode") in {"human", "automatic"}:
            reviewed = self._snapshot(record)
            record["state"] = "staging_human_draft" if policy["approval_mode"] == "human" else "staging_automatic_draft"
            self._save(ledger)
            external = self.core.stage_remote(record["variant_id"])
            fresh = self._snapshot(record)
            if (self._reviewed_fields(fresh) != self._reviewed_fields(reviewed)
                    or fresh["revision"] != reviewed["revision"] + 1
                    or fresh["metadata"].get("postiz_state") != "DRAFT"):
                raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
            record.update(state="awaiting_human_review" if policy["approval_mode"] == "human" else "automatic_ready", postiz_id=external,
                          prepared_hash=fingerprint(fresh))
        if attempt:
            attempt.update(state=record["state"], finished_at=now())
        record.pop("error", None)
        self._save(ledger)
        return self._view(record)

    def _validate_reserve(self, record, *, due_at=None):
        """Повторная локальная проверка перед единственным внешним DRAFT."""
        from .article_reserve import ArticleReserveStore
        from .automatic_policy import _eligible_record, automatic_evidence
        from .human_review import source_data
        from .server_state import require_running
        require_running(self.core)
        self._reserve_record(record)
        claim = ArticleReserveStore(self.core).require_preparation(record["reserve_claim"], record["id"], active=False)
        if (claim["state"] != "quality_ready" or record.get("state") != "quality_ready"
                or any(record.get(key) is not None for key in ("postiz_id", "dispatch_id", "due_at"))):
            raise AgentError("ARTICLE_RESERVE_NOT_READY", 409)
        policy = self._policy_for_record(record)
        if due_at is not None:
            if (type(due_at) not in {int, float} or not math.isfinite(due_at)
                    or not now() + 30 <= due_at <= now() + 3700):
                raise AgentError("ARTICLE_AUTOMATIC_DATE_INVALID", 409)
            self._cadence(policy, due_at)
        with self.core.db.session() as session:
            require_running(self.core, session, lock=True)
            variant, target = self._unpublished_variant(session, record)
            snapshot = copy.deepcopy(self.core.snapshot(variant, target))
            if (record.get("prepared_hash") != fingerprint(snapshot)
                    or variant.metadata_json.get("reserve_contract") != record["reserve_contract"]
                    or variant.metadata_json.get("reserve_binding_hash") != record["reserve_binding_hash"]
                    or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))):
                raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
            _eligible_record(self, session, variant, policy, {**record, "state": "automatic_ready"},
                             reserve_unassigned=True)
            evidence = automatic_evidence(session, variant)
            source = source_data(session, variant)
            material = session.get(Material, variant.sources[0]["id"])
            if due_at is not None and material.published_at < due_at - policy["max_source_age_hours"] * 3600:
                raise AgentError("STALE_SOURCE", 409)
            article = {"title": source["title"], "original": source["body"], "url": source["url"],
                       "published_at": material.published_at}
            # Сравниваем сохранённый исходник, не сокращая и не нормализуя его.
            raw = inside(self.core.settings.root, record.get("input_file", "")).read_bytes()
            saved = parse_json(raw.decode("utf-8"))
            if (hashlib.sha256(raw).hexdigest() != record.get("input_file_hash")
                    or fingerprint(saved) != record.get("input_hash")
                    or saved != article or text_hash(article["original"]) != record.get("original_hash")):
                raise AgentError("ARTICLE_RESERVE_INPUT_CHANGED", 409)
            configured_source, configured_target = self._source_target(policy)
            self._article(saved, policy, configured_source, configured_target)
            trace = inside(self.core.settings.root, record.get("quality_trace", ""))
            if (trace != self.directory / ("quality-" + record["id"] + ".local.jsonl")
                    or hashlib.sha256(trace.read_bytes()).hexdigest()
                        != variant.metadata_json["machine_review_evidence"]["trace_hash"]):
                raise AgentError("ARTICLE_AUTOMATIC_EVIDENCE_INVALID", 409)
            self.core._quality(session, variant, target)
            if due_at is not None:
                self.core._check_schedule_limits(session, variant, target, due_at, automatic_policy=policy)
        return snapshot, evidence

    def validate_reserve(self, ident, *, due_at=None):
        record = self._record(self._load(), ident)
        _, evidence = self._validate_reserve(record, due_at=due_at)
        return {**self._view(record), "reserve_evidence_hash": evidence["evidence_hash"]}

    def invalidate_reserve(self, ident, reason, *, due_at=None):
        """Убрать только доказанно просроченный локальный текст, сохранив QA/raw."""
        from .automatic_policy import automatic_evidence
        from .article_repair_cycle import AutomaticArticleRepair
        from .human_review import source_data
        from .server_models import PublicationSlot
        from .server_state import require_running
        if reason != "STALE_SOURCE":
            raise AgentError("ARTICLE_RESERVE_INVALIDATION_NOT_SAFE", 409)
        if due_at is not None and (type(due_at) not in {int, float} or not math.isfinite(due_at)
                or not now() + 30 <= due_at <= now() + 3700):
            raise AgentError("ARTICLE_RESERVE_INVALIDATION_NOT_SAFE", 409)
        with profile_lock(self.directory / "cycle.lock"):
            ledger = self._load()
            record = self._record(ledger, ident)
            policy = self._policy_for_record(record)
            if due_at is not None:
                self._cadence(policy, due_at)
            with self.core.db.session.begin() as session:
                require_running(self.core, session, lock=True)
                self._reserve_record(record, session=session)
                variant = self.core._variant(session, record.get("variant_id"), True)
                target = session.scalar(select(Target).where(Target.id == variant.target_id).with_for_update())
                evidence = automatic_evidence(session, variant)
                source = source_data(session, variant)
                material = session.get(Material, variant.sources[0]["id"])
                snapshot = self.core.snapshot(variant, target)
                unsafe = (record.get("state") != "quality_ready"
                    or any(record.get(key) is not None for key in ("postiz_id", "dispatch_id", "due_at"))
                    or session.scalar(select(PublicationSlot).where(PublicationSlot.record_id == ident))
                    or session.get(RemoteDraft, variant.id)
                    or session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
                    or session.scalar(select(Dispatch).where(Dispatch.variant_id == variant.id))
                    or session.scalar(select(TargetApproval).where(TargetApproval.variant_id == variant.id))
                    or target.id != record.get("target_id")
                    or variant.metadata_json.get("record_id") != ident
                    or variant.metadata_json.get("reserve_contract") != record.get("reserve_contract")
                    or variant.metadata_json.get("reserve_binding_hash") != record.get("reserve_binding_hash")
                    or fingerprint(policy) != record.get("policy_hash")
                    or fingerprint(snapshot) != record.get("prepared_hash")
                    or fingerprint(snapshot) != record.get("reserve_prepared_hash")
                    or variant.metadata_json.get("review_prompt_hash") != hashlib.sha256(
                        (self.core.settings.root / "prompts" / automatic_review_prompt_name(record))
                        .read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                    or evidence is None or material.published_at is None
                    or material.published_at != record.get("source_published_at")
                    or material.published_at >= (now() if due_at is None else due_at)
                        - policy["max_source_age_hours"] * 3600)
                if unsafe:
                    raise AgentError("ARTICLE_RESERVE_INVALIDATION_NOT_SAFE", 409)
                raw = inside(self.core.settings.root, record.get("input_file", "")).read_bytes()
                article = {"title": source["title"], "original": source["body"], "url": source["url"],
                           "published_at": material.published_at}
                saved_input = parse_json(raw.decode("utf-8"))
                trace = inside(self.core.settings.root, record.get("quality_trace", ""))
                if (hashlib.sha256(raw).hexdigest() != record.get("input_file_hash")
                        or saved_input != article or fingerprint(saved_input) != record.get("input_hash")
                        or text_hash(source["body"]) != record.get("original_hash")
                        or trace != self.directory / ("quality-" + ident + ".local.jsonl")
                        or hashlib.sha256(trace.read_bytes()).hexdigest()
                            != variant.metadata_json["machine_review_evidence"]["trace_hash"]):
                    raise AgentError("ARTICLE_RESERVE_INVALIDATION_NOT_SAFE", 409)
                self._source_target(policy)
                self.core._quality(session, variant, target)
                if due_at is not None:
                    self.core._check_schedule_limits(session, variant, target, due_at, automatic_policy=policy)
                AutomaticArticleRepair(self).assert_complete(record, session)
                action = {"at": now(), "action": "reserve_expired", "reason": reason,
                          "due_at": due_at,
                          "previous_state": record["state"], "prepared_hash": record["prepared_hash"],
                          "evidence_hash": evidence["evidence_hash"]}
                audit_id = str(uuid5(NAMESPACE_URL, "max-postiz:reserve-expired:" + ident))
                previous = session.get(Audit, audit_id)
                if previous is not None:
                    if (previous.actor != "article_reserve" or previous.action != "article_reserve_expired"
                            or previous.entity != ident or not isinstance(previous.details, dict)
                            or {k: v for k, v in previous.details.items() if k != "at"}
                                != {k: v for k, v in action.items() if k != "at"}):
                        raise AgentError("ARTICLE_RESERVE_INVALIDATION_NOT_SAFE", 409)
                    action = copy.deepcopy(previous.details)
                else:
                    session.add(Audit(id=audit_id, actor="article_reserve", action="article_reserve_expired",
                                      entity=ident, details=copy.deepcopy(action)))
            record.setdefault("preparation_actions", []).append(action)
            record.update(state="cancelled", error=reason, cancelled_at=action["at"])
            self._save(ledger)
            return self._view(record)

    def stage_reserve(self, ident):
        """Резерв не получает DRAFT до постоянного назначения слота отправителем."""
        from .article_reserve import ArticleReserveStore
        with profile_lock(self.directory / "cycle.lock"):
            ledger = self._load()
            record = self._record(ledger, ident)
            assignment = ArticleReserveStore(self.core).require_assignment(ident)
            snapshot, evidence = self._validate_reserve(record, due_at=assignment["due_at"])
            if (assignment["text_hash"] != fingerprint(snapshot)
                    or assignment["evidence_hash"] != evidence["evidence_hash"]):
                raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_CHANGED", 409)
            self._clear_outstanding(record["target_id"])
            record.update(state="staging_reserve_draft", publication_slot_id=assignment["id"])
            self._save(ledger)
            # Marker durable до запроса. Timeout/падение оставляет сверяемую операцию;
            # повтор stage_reserve уже не пройдет проверку quality_ready.
            try:
                external = self.core.stage_remote(record["variant_id"])
                fresh = self._snapshot(record)
                if (self._reviewed_fields(fresh) != self._reviewed_fields(snapshot)
                        or fresh["revision"] != snapshot["revision"] + 1
                        or fresh["metadata"].get("postiz_state") != "DRAFT"):
                    raise AgentError("ARTICLE_REMOTE_CHANGED", 409)
                record.update(state="automatic_ready", postiz_id=external, prepared_hash=fingerprint(fresh))
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                record.update(state="needs_attention", error=exc.code if isinstance(exc, AgentError)
                              else "ARTICLE_RESERVE_STAGE_INTERRUPTED")
                self._save(ledger)
                raise

    def stage_rejected(self, ident, article, *, restore_cancelled=False, reason=""):
        """Recover exact completed negative evidence, never re-run or approve the model."""
        from .rejected_trace import recover_rejected
        if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 500:
            raise AgentError("ARTICLE_RECOVERY_REASON_REQUIRED", 409)
        with profile_lock(self.directory / "cycle.lock"):
            policy, ledger = self._policy(), self._load()
            if policy.get("approval_mode") != "human":
                raise AgentError("ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE", 409)
            record = self._record(ledger, ident)
            from .article_sources import require_legacy_source
            require_legacy_source(record)
            if (record["state"] not in {"needs_attention", "rejected", "failed", "cancelled"}
                    or (record["state"] == "cancelled" and restore_cancelled is not True)):
                raise AgentError("ARTICLE_REJECTED_RECOVERY_NOT_SAFE", 409)
            self._assert_pre_draft(record)
            source, target = self._source_target(policy)
            article = self._article(article, policy, source, target)
            if (record.get("input_hash") != fingerprint(article)
                    or record.get("original_hash") != text_hash(article["original"])
                    or record.get("url") != article["url"]
                    or record.get("source_published_at") != article["published_at"]
                    or record.get("policy_hash") != fingerprint(policy)
                    or record.get("target_id") != target.id or record.get("source_id") != source["id"]):
                raise AgentError("ARTICLE_RETRY_INPUT_MISMATCH", 409)
            self._clear_outstanding(target.id)
            others = [r for r in ledger["records"] if r is not record]
            if sum(r["state"] == "awaiting_human_review" for r in others) >= 24:
                raise AgentError("ARTICLE_HUMAN_REVIEW_BACKLOG", 409)
            if any(r["state"] not in {"published", "failed", "cancelled", "rejected", "awaiting_human_review"}
                   for r in others):
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            if any(r.get("url") == article["url"] or r.get("original_hash") == record["original_hash"] for r in others):
                raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            material_hash = fingerprint(" ".join(article["original"].casefold().split()))
            with self.core.db.session() as session:
                if session.scalar(select(Material).where(or_(Material.url == article["url"],
                        Material.text_hash.in_({record["original_hash"], material_hash})))):
                    raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            if record.get("quality_trace") != expected:
                raise AgentError("ARTICLE_MACHINE_EVIDENCE_INVALID", 409)
            raw = inside(self.core.settings.root, expected).read_bytes()
            prepared = recover_rejected(record, article, raw)
            action = {"at": now(), "action": "operator_stage_rejected", "reason": reason.strip(),
                      "previous_state": record["state"], "previous_error": record.get("error"),
                      "policy_hash": fingerprint(policy), "trace_hash": hashlib.sha256(raw).hexdigest(),
                      "state": "staging", "no_new_inference": True}
            record.setdefault("preparation_actions", []).append(action)
            record["state"] = "recovering_rejected"
            self._save(ledger)
            try:
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_operator", "hourly_article_rejected_recovery_started",
                        ident, recovery_action=action)
                self._store_prepared(article, prepared, record, ledger, policy, source, target,
                    {"quality": "preserved_ministral_rejection_languagetool", "recovered_rejection": True},
                    check_slot=False)
                action.update(state="staged_for_human", finished_at=now(), variant_id=record["variant_id"])
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                error = exc.code if isinstance(exc, AgentError) else "ARTICLE_REJECTED_RECOVERY_INTERRUPTED"
                action.update(state="needs_attention", finished_at=now(), error=error)
                record.update(state="needs_attention", error=error)
                self._save(ledger)
                raise

    def cancel_preparation(self, ident, reason="Operator reconciled and abandoned pre-draft preparation"):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise AgentError("ARTICLE_CANCELLATION_REASON_INVALID")
        with profile_lock(self._preparation_lock()):
            ledger = self._load()
            record = self._record(ledger, ident)
            if self.reserve_mode:
                self._reserve_record(record, active=True)
            from .article_repair_cycle import AutomaticArticleRepair
            _, repair_block = AutomaticArticleRepair(self)._history(record)
            if repair_block:
                raise AgentError("ARTICLE_AUTOMATIC_REPAIR_" + repair_block, 409)
            if record["state"] not in {"needs_attention", "preparing", "rejected", "failed"}:
                raise AgentError("ARTICLE_PREPARATION_NOT_CANCELLABLE", 409)
            self._assert_pre_draft(record)
            action = {"at": now(), "action": "operator_cancelled", "previous_state": record["state"],
                      "reason": reason.strip()}
            record.setdefault("preparation_actions", []).append(action)
            with self.core.db.session.begin() as session:
                self.core.db.audit(session, "hourly_article_operator", "hourly_article_cancel_preparation", record["id"],
                    policy_hash=record["policy_hash"], source_hash=record["original_hash"],
                    previous_state=record["state"], reason=reason.strip())
            record.update(state="cancelled", cancelled_at=action["at"])
            self._save(ledger)
            return self._view(record)

    def abandon_prepared(self, ident, reason="Operator rejected an unpublished local article draft"):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise AgentError("ARTICLE_CANCELLATION_REASON_INVALID")
        with profile_lock(self.directory / "cycle.lock"):
            ledger = self._load()
            record = self._record(ledger, ident)
            if (record["state"] not in {"prepared", "needs_attention", "rejected", "failed", "revising"}
                    or not record.get("variant_id")
                    or any(record.get(key) for key in ("postiz_id", "dispatch_id", "due_at"))):
                raise AgentError("ARTICLE_PREPARED_NOT_ABANDONABLE", 409)
            with self.core.db.session.begin() as session:
                variant, target = self._unpublished_variant(session, record)
                variant.state, variant.revision = "abandoned", variant.revision + 1
                session.get(Draft, variant.draft_id).state = "article_rejected"
                self.core._history(session, variant, target, "hourly_article_operator")
                self.core.db.audit(session, "hourly_article_operator", "hourly_article_abandon_prepared", variant.id,
                    record_id=record["id"], policy_hash=record["policy_hash"], reason=reason.strip())
            action = {"at": now(), "action": "operator_abandoned_prepared", "previous_state": record["state"],
                      "reason": reason.strip()}
            record.setdefault("preparation_actions", []).append(action)
            record.update(state="cancelled", cancelled_at=action["at"])
            self._save(ledger)
            return self._view(record)

    def revise_prepared(self, ident, correction):
        self._validate_correction(correction)
        with profile_lock(self.directory / "cycle.lock"):
            policy, ledger = self._policy(), self._load()
            record = self._record(ledger, ident)
            from .article_sources import require_legacy_source
            require_legacy_source(record)
            if policy.get("approval_mode") == "automatic" and record.get("postiz_id"):
                raise AgentError("ARTICLE_EDIT_IN_POSTIZ_REQUIRED", 409)
            if (record["state"] != "prepared" or not record.get("variant_id")
                    or record["policy_hash"] != fingerprint(policy)):
                raise AgentError("ARTICLE_CORRECTION_NOT_ALLOWED", 409)
            if record["slot"] != int(now() // 3600):
                raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
            self._source_target(policy)
            with self.core.db.session.begin() as session:
                variant, target = self._unpublished_variant(session, record)
                prior_hash = fingerprint(self.core.snapshot(variant, target))
                if prior_hash != record["prepared_hash"] or len(variant.sources) != 1:
                    raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
                material = session.get(Material, variant.sources[0]["id"])
                if (not material or material.url != record["url"] or material.source_id != record["source_id"]
                        or text_hash(material.text) != record["original_hash"]):
                    raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
                original_title, original = material.title, material.text
            self._guard_candidate_replay(ledger, record, correction, "revise_prepared")
            attempt = {"started_at": now(), "candidate_hash": self._candidate_hash(correction), "state": "checking",
                       "candidate_normalization": "article-links-v1",
                       "candidate_replay_hash": self._initial_replay_hash(correction),
                       "candidate_raw_hash": fingerprint(correction)}
            if "reference_contract" in record:
                attempt["reference_contract"] = record["reference_contract"]
            record.setdefault("correction_attempts", []).append(attempt)
            record.update(state="revising", correction_blocked=True)
            self._save(ledger)
            quality = None
            try:
                quality = self._quality(record["id"])
                checked = quality.check_candidate(correction["title"], correction["body"], original_title,
                                                  original, record["url"])
                if record["slot"] != int(now() // 3600):
                    raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                self._source_target(self._policy())
                if fingerprint(self._policy()) != record["policy_hash"] or checked["url"] != record["url"]:
                    raise AgentError("ARTICLE_SOURCE_CHANGED", 409)
                translation_hash = text_hash(checked["translation"])
                if any(item is not record and item.get("translation_hash") == translation_hash for item in ledger["records"]):
                    raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
                with self.core.db.session.begin() as session:
                    variant, target = self._unpublished_variant(session, record)
                    if fingerprint(self.core.snapshot(variant, target)) != prior_hash:
                        raise AgentError("ARTICLE_REVIEW_HASH_MISMATCH", 409)
                    duplicates = session.scalars(select(Variant).where(Variant.target_id == target.id, Variant.id != variant.id))
                    if any(v.metadata_json.get("translation_hash") == translation_hash for v in duplicates):
                        raise AgentError("ARTICLE_DUPLICATE_TRANSLATION", 409)
                    draft = session.get(Draft, variant.draft_id)
                    draft.current = {**draft.current, "title": checked["title"], "body": checked["body"]}
                    draft.revision += 1
                    draft.state, draft.config_id = "article_review", self.core.settings.hash
                    session.add(DraftRevision(draft_id=draft.id, revision=draft.revision,
                        payload=copy.deepcopy(draft.current), config_id=draft.config_id))
                    variant.title, variant.body = checked["title"], checked["body"]
                    variant.revision += 1
                    variant.source_revision, variant.config_hash = draft.revision, draft.config_id
                    variant.binding_hash, variant.state, variant.updated_at = target.binding_hash, "review_pending", now()
                    metadata = {**variant.metadata_json, "translation_hash": translation_hash,
                        "quality": "operator_ministral_languagetool",
                        "topic": checked["topic"], "editorial_correction_hash": attempt["candidate_hash"]}
                    metadata.pop("translation_model", None)
                    metadata.pop("review_model", None)
                    from .human_review import text_hash as machine_text_hash
                    metadata["machine_text_hash"] = machine_text_hash(variant.title, variant.body)
                    variant.metadata_json = metadata
                    self.core._history(session, variant, target, "hourly_article_operator")
                    if record["slot"] != int(now() // 3600):
                        raise AgentError("ARTICLE_SLOT_EXPIRED", 409)
                    record.update(prepared_hash=fingerprint(self.core.snapshot(variant, target)),
                                  translation_hash=translation_hash, state="prepared", correction_blocked=False)
                    self.core.db.audit(session, "hourly_article_operator", "hourly_article_revised_prepared", variant.id,
                        record_id=record["id"], previous_hash=prior_hash, prepared_hash=record["prepared_hash"],
                        candidate_hash=attempt["candidate_hash"], policy_hash=record["policy_hash"])
                attempt.update(state="accepted", finished_at=now())
                record.pop("error", None)
                self._save(ledger)
                return self._view(record)
            except Exception as exc:
                error = exc.code if isinstance(exc, AgentError) else "ARTICLE_CORRECTION_INTERRUPTED"
                attempt.update(state="rejected", error=error, finished_at=now())
                # Keep the original version intact, but block publication until an
                # explicit corrected candidate passes. A fresh operator revision can retry.
                record.update(state="prepared", correction_blocked=True, error=error)
                self._save(ledger)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_operator", "hourly_article_correction_error", ident,
                        error=error, candidate_hash=attempt["candidate_hash"])
                raise
            finally:
                if quality:
                    from .human_review import close_quality
                    close_quality(quality, close_models=not self.quality_factory)

    def publish(self, ident, reviewed_hash, due_at=None):
        """Only the explicit automatic policy may grant a policy approval."""
        with profile_lock(self.directory / "cycle.lock"):
            if self._policy().get("approval_mode") != "automatic":
                raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
            from .automatic_policy import publish
            ledger = self._load()
            return publish(self, self._record(ledger, ident), ledger, reviewed_hash, due_at)

    def migrate_automatic(self, ident, reason):
        from .automatic_migration import migrate
        return migrate(self, ident, reason)

    def correct_staged(self, ident, correction, reviewed_hash, reason):
        from .automatic_correction import correct_staged
        return correct_staged(self, ident, correction, reviewed_hash, reason)

    def recover_staged_spelling(self, ident, correction, reviewed_hash, trace_sha256, reason):
        from .automatic_correction import recover_staged_spelling
        return recover_staged_spelling(self, ident, correction, reviewed_hash, trace_sha256, reason)

    def recover_staged_roni_spelling(self, ident, correction, reviewed_hash, trace_sha256, reason):
        from .automatic_correction import recover_staged_roni_spelling
        return recover_staged_roni_spelling(self, ident, correction, reviewed_hash, trace_sha256, reason)

    def reconcile_approval(self, ident, reviewed_hash, due_at, reason):
        from .automatic_policy import reconcile_approval
        with profile_lock(self.directory / "cycle.lock"):
            ledger = self._load()
            return reconcile_approval(self, self._record(ledger, ident), ledger, reviewed_hash, due_at, reason)

    def _saved_review_protocol(self, record, article, candidate, trace_sha256):
        from .adapters import extract_object
        from .article_quality import article_review_context, normalize_article_links, protocol_recovery_review
        error = "ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE"
        try:
            expected = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
            if record.get("quality_trace") != expected or not re.fullmatch(r"[a-f0-9]{64}", trace_sha256):
                raise ValueError
            raw = inside(self.core.settings.root, expected).read_bytes()
            if not 0 < len(raw) <= 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != trace_sha256:
                raise ValueError
            attempts = record.get("preparation_attempts")
            if not isinstance(attempts, list) or not attempts or not all(isinstance(item, dict) for item in attempts):
                raise ValueError
            attempt = attempts[-1]
            reference_contract = record.get("reference_contract", LEGACY_REFERENCE_CONTRACT)
            if (not isinstance(reference_contract, str) or reference_contract not in REFERENCE_CONTRACTS
                    or attempt.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract):
                raise ValueError
            if (type(attempt.get("number")) is not int or attempt["number"] != len(attempts)
                    or attempt.get("operator_correction") is not True
                    or attempt.get("error") != "ARTICLE_QUALITY_REJECTED"
                    or attempt.get("state") not in {"needs_attention", "rejected", "failed"}
                    or attempt.get("candidate_normalization") != "article-links-v1"
                    or attempt.get("candidate_hash") != self._candidate_hash(candidate)
                    or attempt.get("candidate_raw_hash") != fingerprint(candidate)):
                raise ValueError
            started, finished = attempt["started_at"], attempt["finished_at"]
            if (any(type(value) not in {int, float} or not math.isfinite(value) for value in (started, finished))
                    or not record["claimed_at"] <= started <= finished):
                raise ValueError
            events, previous = [], -1
            for line in raw.decode("utf-8").splitlines():
                event = parse_json(line)
                if (not isinstance(event, dict) or event.get("record_id") != record["id"]
                        or type(event.get("at")) not in {int, float} or not math.isfinite(event["at"])
                        or not previous <= event["at"] <= finished):
                    raise ValueError
                previous = event["at"]
                events.append(event)
            segment = [event for event in events if event["at"] >= started]
            phases = ["candidate_started", "gate_passed", "review_started", "review_returned", "review_invalid", "rejected"]
            if ([event.get("phase") for event in segment] != phases
                    or any(type(event.get("attempt")) is not int or event["attempt"] != 1 for event in segment)):
                raise ValueError
            canonical = {key: normalize_article_links(candidate[key].strip()) for key in ("title", "body")}
            source = {"title": article["title"].strip(), "body": article["original"].strip(), "url": article["url"]}
            first, gate, review_start, returned, invalid, rejected = segment
            original = source["title"] + "\n\n" + source["body"]
            translation = canonical["title"] + "\n\n" + canonical["body"]
            prompt = (self.core.settings.root / "prompts" / automatic_review_prompt_name(record)).read_text(encoding="utf-8")
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            context = review_start.get("context", {})
            if not isinstance(context, dict):
                raise ValueError
            if (first.get("mode") != "candidate" or first.get("candidate") != canonical
                    or first.get("candidate_raw", first.get("candidate")) != candidate
                    or first.get("source_untrusted") != source
                    or first.get("reference_contract", LEGACY_REFERENCE_CONTRACT) != reference_contract
                    or gate.get("characters") != len(translation + "\n\n" + article_footer(article["url"]))
                    or context.get("instruction") != prompt or context.get("review_prompt_hash") != prompt_hash
                    or context.get("review_rubric") != automatic_review_rubric(record)
                    or context.get("source_untrusted") != original or context.get("translation_untrusted") != translation
                    or invalid.get("error") != "ARTICLE_REVIEW_SCHEMA_INVALID"
                    or rejected.get("mode") != "candidate" or rejected.get("error") != "ARTICLE_QUALITY_REJECTED"
                    or rejected.get("feedback")):
                raise ValueError
            if reference_contract == REFERENCE_CONTRACT and context != article_review_context(
                    prompt, original, translation, automatic_review_rubric(record), prompt_hash,
                    reference_contract=reference_contract):
                raise ValueError
            # Another verdict for this exact EN/RU pair is never upgraded by a
            # protocol repair, including an earlier substantive refusal.
            matching = [event for event in events if event.get("phase") == "review_started"
                        and isinstance(event.get("context"), dict)
                        and event.get("context", {}).get("source_untrusted") == original
                        and event.get("context", {}).get("translation_untrusted") == translation]
            if len(matching) != 1:
                raise ValueError
            current_hashes = {self._candidate_hash(candidate), fingerprint(candidate)}
            corrections = record.get("correction_attempts", [])
            if not isinstance(corrections, list) or any(not isinstance(item, dict) for item in corrections):
                raise ValueError
            prior = attempts[:-1] + corrections
            if any(item.get("error") == "ARTICLE_QUALITY_REJECTED" and any(item.get(key) in current_hashes
                   for key in ("candidate_hash", "candidate_raw_hash", "candidate_normalized_hash")) for item in prior):
                raise ValueError
            raw_review = returned["raw"]
            reviewed = protocol_recovery_review(raw_review)
            if fingerprint(invalid.get("review")) != fingerprint(extract_object(raw_review)):
                raise ValueError
            return {"raw": raw, "canonical": canonical, "raw_review": raw_review,
                    "raw_review_sha256": hashlib.sha256(raw_review.encode("utf-8")).hexdigest(),
                    "review_prompt_hash": prompt_hash, "review": reviewed, "trace": expected,
                    "segment_hash": fingerprint(segment)}
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, AgentError) as exc:
            raise AgentError(error, 409) from exc

    def _protocol_pre_draft(self, record, target_id):
        self._assert_pre_draft(record)
        with self.core.db.session() as session:
            target = self.core._target(session, target_id)
            if (not target.paused or session.scalar(select(Dispatch).where(Dispatch.state.in_(ACTIVE)))
                    or session.scalar(select(BridgePost).where(BridgePost.mutation_owner.is_not(None)
                        | (BridgePost.mutation_until != 0) | BridgePost.mutation_until.is_(None)))):
                raise AgentError("ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE", 409)

    def _protocol_source_novel(self, record, ledger, article):
        others = [item for item in ledger["records"] if item.get("id") != record["id"]]
        reserved = self.reserve_mode and record.get("reserve_contract") == "article-reserve-v1"
        if reserved:
            self._reserve_record(record, active=True)
        if not reserved and any(item["slot"] == record["slot"] or item.get("automatic_slot") == record["slot"] for item in others):
            raise AgentError("ARTICLE_SLOT_ALREADY_CLAIMED", 409)
        if sum(item["state"] in {"awaiting_human_review", "quality_ready", "automatic_ready", "pending", "sending", "unknown", "awaiting_postiz"}
               for item in others) >= 24:
            raise AgentError("ARTICLE_AUTOMATIC_BACKLOG", 409)
        if any(item["state"] not in {"published", "failed", "cancelled", "rejected", "awaiting_human_review"}
               and not (item["state"] == "automatic_ready" and item.get("automatic_enrolled"))
               and not (reserved and self._reserve_other_safe(item)) for item in others):
            raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
        if any(item.get("url") == article["url"] or item.get("original_hash") == record["original_hash"] for item in others):
            raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)
        material_hash = fingerprint(" ".join(article["original"].casefold().split()))
        with self.core.db.session() as session:
            if session.scalar(select(Material).where(or_(Material.url == article["url"],
                    Material.text_hash.in_({record["original_hash"], material_hash})))):
                raise AgentError("ARTICLE_DUPLICATE_SOURCE", 409)

    def recover_review_protocol(self, ident, article, candidate, trace_sha256, reason):
        from .article_quality import (MAX_REVIEW_ISSUES, MAX_REVIEW_ISSUE_CHARS, MAX_REVIEW_WARNINGS,
            MAX_REVIEW_WARNINGS_BYTES, REVIEW_PROTOCOL_CONTRACT)
        from .human_review import close_quality
        if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
            raise AgentError("ARTICLE_REVIEW_PROTOCOL_REASON_REQUIRED", 409)
        self._validate_correction(candidate)
        with profile_lock(self.directory / "cycle.lock"):
            policy, ledger = self._policy(), self._load()
            record = self._record(ledger, ident)
            if automatic_review_rubric(record) != "meaning-first-v1":
                # Историческое исключение протокола не переносим на новую рубрику.
                raise AgentError("ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE", 409)
            from .article_sources import require_legacy_source
            require_legacy_source(record)
            if (policy.get("approval_mode") != "automatic" or record.get("state") != "needs_attention"
                    or record.get("error") != "ARTICLE_QUALITY_REJECTED" or record["slot"] != int(now() // 3600)
                    or record.get("policy_hash") != fingerprint(policy)):
                raise AgentError("ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE", 409)
            source, target = self._source_target(policy)
            article = self._article(article, policy, source, target)
            if (record.get("input_hash") != fingerprint(article) or record.get("url") != article["url"]
                    or record.get("original_hash") != text_hash(article["original"])
                    or record.get("source_published_at") != article["published_at"]
                    or record.get("source_id") != source["id"] or record.get("target_id") != target.id):
                raise AgentError("ARTICLE_RETRY_INPUT_MISMATCH", 409)
            self._protocol_pre_draft(record, target.id)
            self._protocol_source_novel(record, ledger, article)
            saved = self._saved_review_protocol(record, article, candidate, trace_sha256)
            claim_id = str(uuid5(NAMESPACE_URL, "max-postiz:review-protocol-recovery:" + ident))
            history = record.get("review_protocol_recoveries", [])
            if not isinstance(history, list):
                raise AgentError("ARTICLE_LEDGER_INVALID", 409)
            with self.core.db.session() as session:
                if history or session.get(Audit, claim_id):
                    raise AgentError("ARTICLE_REVIEW_PROTOCOL_ALREADY_ATTEMPTED", 409)
            contract = {"id": REVIEW_PROTOCOL_CONTRACT, "issues_max": MAX_REVIEW_ISSUES,
                        "warnings_max": MAX_REVIEW_WARNINGS, "item_chars_max": MAX_REVIEW_ISSUE_CHARS,
                        "warnings_utf8_max": MAX_REVIEW_WARNINGS_BYTES}
            trace_id = uid()
            claim = {"id": claim_id, "at": now(), "state": "checking", "reason": reason.strip(),
                     "original_error": record["error"], "old_trace": saved["trace"], "old_trace_sha256": trace_sha256,
                     "old_segment_hash": saved["segment_hash"], "raw_review_sha256": saved["raw_review_sha256"],
                     "candidate_hash": self._candidate_hash(candidate), "candidate_raw_hash": fingerprint(candidate),
                     "source_hash": record["input_hash"], "policy_hash": fingerprint(policy),
                     "review_prompt_hash": saved["review_prompt_hash"], "review_rubric": "meaning-first-v1",
                     "parser_contract": contract, "parser_contract_hash": fingerprint(contract),
                     "quality_trace": f"data/hourly-articles/quality-{trace_id}.local.jsonl", "operator_correction": True}
            try:
                with self.core.db.session.begin() as session:
                    session.add(Audit(id=claim_id, actor="hourly_article_protocol_recovery",
                        action="hourly_article_review_protocol_started", entity=ident, details=copy.deepcopy(claim)))
            except IntegrityError as exc:
                raise AgentError("ARTICLE_REVIEW_PROTOCOL_ALREADY_ATTEMPTED", 409) from exc
            quality = None
            try:
                record["review_protocol_recoveries"] = [*history, claim]
                record["state"] = "recovering_review_protocol"
                self._save(ledger)
                quality = self._quality(trace_id, review_only=True)
                canonical = saved["canonical"]
                prepared = quality.recover_review_protocol(canonical["title"], canonical["body"],
                    article["title"].strip(), article["original"].strip(), article["url"], raw_review=saved["raw_review"],
                    expected_review_prompt_hash=saved["review_prompt_hash"])
                expected_record_hash = fingerprint(record)
                ledger = self._load()
                record = self._record(ledger, ident)
                if fingerprint(record) != expected_record_hash:
                    raise AgentError("ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE", 409)
                claim = record["review_protocol_recoveries"][-1]
                self._protocol_pre_draft(record, target.id)
                if (fingerprint(self._policy()) != fingerprint(policy)
                        or (self.core.settings.root / saved["trace"]).read_bytes() != saved["raw"]
                        or prepared.get("title") != canonical["title"]
                        or prepared.get("translation") != canonical["title"] + "\n\n" + canonical["body"]):
                    raise AgentError("ARTICLE_REVIEW_PROTOCOL_NOT_RECOVERABLE", 409)
                source, target = self._source_target(policy)
                self._article(article, policy, source, target)
                self._protocol_source_novel(record, ledger, article)
                record["quality_trace"] = claim["quality_trace"]
                result = self._store_prepared(article, prepared, record, ledger, policy, source, target,
                    {"quality": "saved_ministral_protocol_recovery_fresh_languagetool",
                     "review_protocol_recovery": copy.deepcopy(prepared["review_protocol_recovery"])}, claim)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_protocol_recovery",
                        "hourly_article_review_protocol_completed", ident, **claim)
                return result
            except Exception as exc:
                claim.update(state="failed", finished_at=now(), error=exc.code if isinstance(exc, AgentError)
                             else "ARTICLE_REVIEW_PROTOCOL_INTERRUPTED")
                record.update(state="needs_attention", protocol_recovery_error=claim["error"])
                self._save(ledger)
                with self.core.db.session.begin() as session:
                    self.core.db.audit(session, "hourly_article_protocol_recovery",
                        "hourly_article_review_protocol_failed", ident, **claim)
                raise
            finally:
                if quality:
                    close_quality(quality, close_models=not self.quality_factory)

    def status(self, ident=None):
        with profile_lock(self.directory / "cycle.lock"):
            self._policy(enabled=False)
            ledger = self._load()
            if not ledger["records"]:
                return {"state": "empty"}
            record = self._record(ledger, ident) if ident else ledger["records"][-1]
            if ident is None:
                by_variant = {item.get("variant_id"): item for item in ledger["records"] if item.get("variant_id")}
                with self.core.db.session() as session:
                    active = session.scalar(select(Dispatch).where(Dispatch.variant_id.in_(by_variant),
                        Dispatch.state.in_(ACTIVE)).order_by(
                            Dispatch.state.in_({"sending", "unknown"}).desc(), Dispatch.due_at.asc()))
                    if active:
                        record = by_variant[active.variant_id]
                    else:
                        unfinished = {key for key, value in by_variant.items() if value["state"] in {
                            "pending", "sending", "unknown", "awaiting_postiz", "automatic_pending_approval", "needs_attention"}}
                        completed = session.scalar(select(Dispatch).where(Dispatch.variant_id.in_(unfinished))
                            .order_by((Dispatch.state == "published").desc(), Dispatch.due_at.asc()))
                        if completed:
                            record = by_variant[completed.variant_id]
            if record.get("variant_id"):
                with self.core.db.session() as session:
                    delivery = session.scalar(select(Dispatch).where(Dispatch.variant_id == record["variant_id"])
                                              .order_by(Dispatch.state.in_(ACTIVE).desc(),
                                                        (Dispatch.state == "published").desc(), Dispatch.due_at.desc()))
                    link = session.scalar(select(BridgePost).where(BridgePost.variant_id == record["variant_id"]))
                    if delivery:
                        record.update(dispatch_id=delivery.id, dispatch_state=delivery.state,
                                      delivery_verified=delivery.verified, error=delivery.error)
                        if delivery.state in {"sending", "unknown"}:
                            record["state"] = delivery.state
                        elif delivery.state == "published" and delivery.verified:
                            record["state"] = "awaiting_postiz"
                            attempt = session.scalar(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == delivery.id,
                                DispatchAttempt.result == "published").order_by(DispatchAttempt.created_at.desc()))
                            record["published_at"] = attempt.created_at if attempt else now()
                        elif delivery.state == "published":
                            record["state"] = "published_unverified"
                        elif delivery.state in {"failed", "cancelled"}:
                            variant = session.get(Variant, record["variant_id"])
                            record["state"] = ("awaiting_human_review" if
                                variant.metadata_json.get("approval_mode") == "human" else delivery.state)
                        elif record["state"] not in {"needs_attention", "approving"}:
                            record["state"] = delivery.state
                    if link:
                        record["postiz_id"] = link.post_id
            if record["state"] == "awaiting_postiz":
                remote = self.core.postiz.get_post(record["postiz_id"])
                record["postiz_state"] = remote.get("state")
                if remote.get("state") == "PUBLISHED":
                    record["state"] = "published"
            if record["state"] in {"published", "failed", "cancelled"}:
                self._clear_outstanding(record["target_id"], record.get("variant_id"))
                self.core.pause_target(record["target_id"], True, actor="hourly_article_policy")
            self._save(ledger)
            return self._view(record)
