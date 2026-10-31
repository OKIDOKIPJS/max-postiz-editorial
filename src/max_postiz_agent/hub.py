"""Reuse MAX editorial core; publish exclusively through per-target durable dispatches."""
import hashlib
import json
import math
import re

from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from .adapters import Delivery
from .article_attribution import illustrated_presentation
from .config import AgentError, fingerprint, inside, parse_json, validate_schema
from .db import Audit, Cursor, Draft, DraftRevision, Job, Material, Story, now, serialize, uid
from .editorial import validate_draft
from .hub_models import (Dispatch, DispatchAttempt, RecurringTask, RemoteDraft, Target,
                         TargetApproval, Variant, VariantRevision, SourceArchive)
from .postiz import PostizClient
from .service import Service


ACTIVE = {"pending", "sending", "unknown", "scheduled", "awaiting_manual", "retry_wait"}
FINAL = {"published", "owner_attested", "cancelled", "failed"}


def _machine_exception_notes(value):
    if not isinstance(value, str):
        raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOTES_REQUIRED", 409)
    notes = value.strip()
    words = re.findall(r"[^\W\d_]+", notes.casefold())
    if not 40 <= len(notes) <= 4000 or len(words) < 6 or len(set(words)) < 4:
        raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOTES_REQUIRED", 409)
    return notes


class HubService(Service):
    def __init__(self, settings, db, editor=None, publisher=None, http=None, postiz=None, bootstrap_targets=True,
                 route_publishers=None):
        super().__init__(settings, db, editor, publisher, http)
        path = settings.root / "config/publishing.json"
        self.publishing = parse_json(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": "1.0", "postiz": {}, "targets": [], "tasks": []}
        validate_schema(settings.root, "publishing.schema.json", self.publishing)
        self.postiz = postiz or PostizClient(self.publishing["postiz"])
        from .platform_routes import PlatformRoutes
        self.routes = PlatformRoutes(self, route_publishers)
        from .max_bridge import MaxBridge
        self.bridge = MaxBridge(self)
        from .reddit_intake import RedditIntake
        from .reddit_mail import RedditMail
        self.reddit_intake, self.reddit_mail = RedditIntake(self), RedditMail(self)
        if bootstrap_targets:
            self.sync_targets()

    def _approve(self, session, draft, actor):
        # Never let the legacy MAX outbox race the new dispatch worker.
        raise AgentError("USE_PLATFORM_VARIANT_APPROVAL", 409)

    def activate(self, settings=None, actor="owner"):
        candidate = settings or self.settings
        path = candidate.root / "config/publishing.json"
        publishing = parse_json(path.read_text(encoding="utf-8")) if path.exists() else self.publishing
        validate_schema(candidate.root, "publishing.schema.json", publishing)
        for target in publishing["targets"]:
            candidate.channel(target["profile_id"])
        previous = self.publishing["postiz"]
        super().activate(candidate, actor)
        self.publishing = publishing
        if previous != publishing["postiz"]:
            self.postiz.close()
            self.postiz = PostizClient(publishing["postiz"])
        self.sync_targets()

    def sync_targets(self):
        ids = set()
        with self.db.session.begin() as s:
            for value in self.publishing["targets"]:
                if value["id"] in ids:
                    raise AgentError("DUPLICATE_TARGET_ID")
                from .platform_routes import validate_route
                validate_route(value)
                if value["enabled"] and (not value["destination"] or value["route"] == "disabled"):
                    raise AgentError("TARGET_DESTINATION_REQUIRED")
                self.settings.channel(value["profile_id"])
                ids.add(value["id"])
                target = s.get(Target, value["id"])
                # Persist policy identity so older workers see configuration changes.
                binding = fingerprint({"target": value, "editorial_config": self.settings.hash})
                if target is None:
                    target = Target(id=value["id"], paused=True)
                    s.add(target)
                changed = target.binding_hash != binding
                for field in ("profile_id", "platform", "route", "destination", "enabled"):
                    setattr(target, field, value[field])
                target.options = value.get("options", {})
                target.binding_hash = binding
                if changed:
                    target.paused = True
            for target in s.scalars(select(Target)):
                if target.id not in ids:
                    target.enabled, target.paused = False, True
            task_ids = set()
            for task in self.publishing.get("tasks", []):
                if task["id"] in task_ids:
                    raise AgentError("DUPLICATE_TASK_ID")
                task_ids.add(task["id"])
                self.settings.channel(task["profile_id"])
                row = s.get(RecurringTask, task["id"])
                if row is None:
                    row = RecurringTask(id=task["id"], due_at=now() + task["interval_seconds"])
                    s.add(row)
                for k in ("profile_id", "interval_seconds", "lookback_hours", "enabled"):
                    setattr(row, k, task[k])
            for row in s.scalars(select(RecurringTask)):
                if row.id not in task_ids:
                    row.enabled = False

    def pause_target(self, target_id, paused, actor="owner"):
        with self.db.session.begin() as s:
            t = self._target(s, target_id)
            if not paused and not t.enabled:
                raise AgentError("TARGET_DISABLED", 409)
            t.paused = paused
            self.db.audit(s, actor, "target_pause", t.id, paused=paused)

    def _target(self, s, ident):
        target = s.get(Target, ident)
        if not target:
            raise AgentError("TARGET_NOT_FOUND", 404)
        return target

    def _variant(self, s, ident, lock=False):
        query = select(Variant).where(Variant.id == ident)
        if lock:
            query = query.with_for_update()
        v = s.scalar(query)
        if not v:
            raise AgentError("VARIANT_NOT_FOUND", 404)
        return v

    def snapshot(self, v, target):
        return {"variant_id": v.id, "revision": v.revision, "title": v.title, "body": v.body,
                "metadata": v.metadata_json,
                "media": v.media, "sources": v.sources, "source_revision": v.source_revision,
                "config_hash": v.config_hash, "binding_hash": v.binding_hash,
                "target_id": target.id, "platform": target.platform,
                "route": target.route, "destination": target.destination}

    def _history(self, s, v, t, actor):
        s.add(VariantRevision(variant_id=v.id, revision=v.revision,
                             payload=self.snapshot(v, t), actor=actor))

    def make_variants(self, draft_id):
        result = []
        with self.db.session.begin() as s:
            draft = s.get(Draft, draft_id)
            if not draft or draft.state == "rejected":
                raise AgentError("DRAFT_NOT_READY", 409)
            material_ids = draft.current.get("source_ids", [])
            materials = list(s.scalars(select(Material).where(Material.id.in_(material_ids))))
            if not materials:
                materials = list(s.scalars(select(Material).where(Material.story_id == draft.story_id)))
            sources = [{"id": m.id, "url": m.url, "source_id": m.source_id} for m in materials]
            for t in s.scalars(select(Target).where(Target.profile_id == draft.channel_id,
                                                   Target.enabled.is_(True))):
                v = s.scalar(select(Variant).where(Variant.draft_id == draft.id, Variant.target_id == t.id))
                if not v:
                    v = Variant(draft_id=draft.id, target_id=t.id, title=draft.current.get("title", ""),
                                body=draft.current["body"], sources=sources, media=[],
                                source_revision=draft.revision, config_hash=draft.config_id,
                                binding_hash=t.binding_hash)
                    s.add(v)
                    s.flush()
                    if t.platform == "pikabu":
                        from .pikabu_metadata import metadata
                        v.metadata_json = metadata(v.title)
                    self._history(s, v, t, "editorial_core")
                result.append(v.id)
        return result

    def _invalidate(self, s, v):
        active = list(s.scalars(select(Dispatch).where(Dispatch.variant_id == v.id,
                                                      Dispatch.state.in_(ACTIVE)).with_for_update()))
        if any(d.state in {"sending", "unknown", "scheduled"} for d in active):
            raise AgentError("CANCEL_OR_RECONCILE_FIRST", 409)
        for approval in s.scalars(select(TargetApproval).where(TargetApproval.variant_id == v.id)):
            approval.revoked = True
        for dispatch in active:
            dispatch.state = "cancelled"

    def revise_variant(self, ident, revision, body, title=None, media=None, actor="editor", metadata_payload=None):
        if not isinstance(body, str) or not body.strip():
            raise AgentError("BODY_REQUIRED")
        with self.db.session.begin() as s:
            v = self._variant(s, ident, True)
            if v.revision != revision:
                raise AgentError("REVISION_CONFLICT", 409)
            from .human_review import hourly
            from .hub_models import BridgePost
            if hourly(v) and (v.metadata_json.get("postiz_post_id") or s.get(RemoteDraft, v.id)
                              or s.scalar(select(BridgePost).where(BridgePost.variant_id == v.id))):
                # A local edit would diverge from the canonical Postiz editor;
                # unchanged remote hashes must not make that edit sendable.
                raise AgentError("ARTICLE_EDIT_IN_POSTIZ_REQUIRED", 409)
            self._invalidate(s, v)
            draft = s.get(Draft, v.draft_id)
            t = self._target(s, v.target_id)
            v.body = body
            if title is not None:
                v.title = title
            if t.platform == "pikabu":
                from .pikabu_metadata import metadata
                v.metadata_json = metadata(v.title, metadata_payload if metadata_payload is not None else v.metadata_json)
            if media is not None:
                v.media = self.check_media(media, t)
            v.revision += 1
            v.source_revision, v.config_hash, v.binding_hash = draft.revision, self.settings.hash, t.binding_hash
            v.state, v.updated_at = "review_pending", now()
            self._history(s, v, t, actor)
            self.db.audit(s, actor, "variant_revision", v.id, revision=v.revision)
            return serialize(v)

    def check_media(self, media, target):
        if not isinstance(media, list) or len(media) > 10:
            raise AgentError("MEDIA_INVALID")
        checked = []
        for m in media:
            if not isinstance(m, dict) or set(m) != {"path", "sha256", "mime"}:
                raise AgentError("MEDIA_INVALID")
            path = inside(self.settings.root, m["path"])
            if not path.is_relative_to(inside(self.settings.root, self.settings.app["media_dir"])):
                raise AgentError("MEDIA_OUTSIDE_STORAGE")
            if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
                raise AgentError("MEDIA_NOT_READY")
            if hashlib.sha256(path.read_bytes()).hexdigest() != m["sha256"]:
                raise AgentError("MEDIA_HASH_CHANGED", 409)
            if m["mime"] not in {"image/png", "image/jpeg", "image/webp", "video/mp4"}:
                raise AgentError("MEDIA_TYPE_UNSUPPORTED")
            if m["mime"] == "video/mp4":
                from .media import inspect_video
                inspect_video(self.settings.root, m["path"])
            else:
                try:
                    from PIL import Image
                    with Image.open(path) as image:
                        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
                        if mime != m["mime"] or image.width * image.height > 40_000_000:
                            raise AgentError("MEDIA_TYPE_MISMATCH")
                        image.verify()
                except ImportError as exc:
                    raise AgentError("IMAGE_DEPENDENCY_REQUIRED") from exc
                except (OSError, ValueError) as exc:
                    raise AgentError("MEDIA_CONTENT_INVALID") from exc
            if target.route == "postiz" and target.platform == "max" and m["mime"] == "video/mp4":
                if not self.postiz.max_video:
                    raise AgentError("MAX_VIDEO_NOT_VERIFIED")
            checked.append(dict(m))
        return checked

    def _quality(self, s, v, t):
        draft = s.get(Draft, v.draft_id)
        if draft.revision != v.source_revision or draft.config_id != v.config_hash:
            raise AgentError("SOURCE_REVISION_STALE", 409)
        if v.config_hash != self.settings.hash or v.binding_hash != t.binding_hash:
            raise AgentError("VARIANT_CONFIG_STALE", 409)
        profile = self.settings.channel(t.profile_id)
        if not profile["enabled"] or profile["publication"]["mode"] == "dry_run":
            raise AgentError("PROFILE_NOT_PUBLISHABLE", 409)
        if not t.enabled:
            raise AgentError("TARGET_DISABLED", 409)
        if len(v.body) > t.options.get("max_chars", 3500):
            raise AgentError("LENGTH_EXCEEDED")
        materials = [serialize(m) for m in s.scalars(select(Material).where(Material.id.in_([m["id"] for m in v.sources])))]
        result = {k: draft.current[k] for k in ("title", "body", "language", "source_ids", "decision",
                  "reason_codes", "claims", "confidence", "payload")}
        result.update(body=v.body, title=v.title)
        policy = json.loads(json.dumps(profile))
        policy["publication"]["max_chars"] = t.options.get("max_chars", 3500)
        owner_original = v.metadata_json.get("origin") == "owner_original"
        if owner_original:
            from .owner_original import require_attestation
            require_attestation(self, s, v, t)
        original = v.metadata_json.get("origin") == "postiz_max_bridge" and not v.sources
        attested = v.metadata_json.get("original_attested_hash") == fingerprint({"body": v.body, "media": v.media})
        if original and not attested:
            raise AgentError("BRIDGE_ORIGINAL_ATTESTATION_REQUIRED", 409)
        licensed_content = None
        if v.metadata_json.get("origin") == "hourly_article_test" and len(materials) == 1:
            from .article_attribution import requires_licensed_footer
            from .human_review import candidate_body, source_data
            source = source_data(s, v)
            if (requires_licensed_footer(source["url"])
                    or illustrated_presentation(v.metadata_json.get("presentation_contract"))):
                # Проверка обязательной лицензии действует и при отсутствии чисел.
                licensed_content = {**result, "body": candidate_body(v, source)
                                    + "\n\nИсточник: " + source["url"]}
        checks = validate_draft(result, materials, policy, model_verified=False,
                                allow_original=(original and attested) or owner_original)
        if "UNSUPPORTED_NUMBER" in checks and licensed_content is not None:
            # Версия CC BY 4.0 — фиксированная атрибуция, не числовой факт
            # статьи. Убираем только точный проверенный footer; числа самого
            # перевода и URL проходят прежний детерминированный gate.
            content_checks = validate_draft(licensed_content, materials, policy,
                model_verified=False, allow_original=False)
            if "UNSUPPORTED_NUMBER" not in content_checks:
                checks = [value for value in checks if value != "UNSUPPORTED_NUMBER"]
        blocking = {"EXCLUDED_TOPIC", "OUTPUT_NOT_RUSSIAN", "LENGTH_EXCEEDED", "UNSUPPORTED_NUMBER",
                    "MISSING_SOURCE_LINK", "INSUFFICIENT_EVIDENCE", "CLAIM_NOT_IN_BODY"}
        if blocking.intersection(checks):
            raise AgentError("QUALITY_GATE_BLOCKED")
        self.check_media(v.media, t)
        if t.platform == "pikabu":
            from .pikabu_metadata import metadata
            metadata(v.title, v.metadata_json)
        self.routes.validate(t, self.snapshot(v, t))

    def hourly_review_details(self, variant_id):
        from .human_review import details
        return details(self, variant_id)

    def recheck_hourly_variant(self, variant_id, revision, content_hash):
        from .human_review import recheck
        return recheck(self, variant_id, revision, content_hash)

    @staticmethod
    def _hourly_machine_exception(session, variant):
        from .human_review import machine_current, rejected_evidence
        if (machine_current(session, variant)
                and variant.metadata_json.get("machine_review_status", "passed") == "passed"):
            return None
        evidence = rejected_evidence(session, variant)
        if (not isinstance(evidence, dict) or not isinstance(evidence.get("evidence_hash"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence["evidence_hash"])):
            raise AgentError("ARTICLE_MACHINE_RECHECK_REQUIRED", 409)
        return evidence

    def _check_machine_exception_approval(self, session, variant, target, dispatch, approval, evidence):
        records = list(session.scalars(select(Audit).where(
            Audit.entity == approval.id, Audit.action == "hourly_article_machine_exception")))
        if len(records) != 1 or records[0].actor != approval.actor:
            raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED", 409)
        record = records[0].details
        if not isinstance(record, dict):
            raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED", 409)
        expected = {"approval_id": approval.id, "variant_id": variant.id, "revision": variant.revision,
                    "content_hash": fingerprint(self.snapshot(variant, target)), "snapshot_hash": dispatch.snapshot_hash,
                    "due_at": dispatch.due_at, "target_id": target.id, "destination": target.destination,
                    "evidence_hash": evidence["evidence_hash"], "machine_exception_confirmed": True}
        if fingerprint({key: record.get(key) for key in expected}) != fingerprint(expected):
            raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED", 409)
        try:
            notes = _machine_exception_notes(record.get("notes"))
        except AgentError:
            raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED", 409) from None
        if record.get("notes_hash") != fingerprint(notes):
            raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED", 409)

    def approve_variant(self, ident, revision, body_hash, due_at, actor="editor", *, human_confirmed=False,
                        machine_exception_confirmed=False, machine_review_notes=""):
        return self._approve_variant(ident, revision, body_hash, due_at, actor,
                                     human_confirmed=human_confirmed,
                                     machine_exception_confirmed=machine_exception_confirmed,
                                     machine_review_notes=machine_review_notes)

    def approve_hourly_policy(self, ident, revision, body_hash, due_at):
        from .automatic_policy import POLICY_ACTOR
        from .server_state import require_running
        require_running(self)
        return self._approve_variant(ident, revision, body_hash, due_at, POLICY_ACTOR, automatic=True)

    def _check_schedule_limits(self, session, variant, target, due_at, *, automatic_policy=None):
        """Read-only shared limits for preflight and the final approval transaction."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if automatic_policy is None:
            publication = self.settings.channel(target.profile_id)["publication"]
            min_gap = publication["min_gap_minutes"] * 60
            max_per_day = publication["max_posts_per_day"]
            tz = ZoneInfo(publication["timezone"])
        else:
            from .article_quality import AUTOMATIC_REVIEW_RUBRICS
            if (not isinstance(automatic_policy, dict)
                    or automatic_policy.get("approval_mode") != "automatic"
                    or automatic_policy.get("target_id") != target.id
                    or not isinstance(automatic_policy.get("review_rubric"), str)
                    or automatic_policy.get("review_rubric") not in AUTOMATIC_REVIEW_RUBRICS
                    or type(automatic_policy.get("min_gap_seconds")) is not int
                    or automatic_policy["min_gap_seconds"] < 3600
                    or type(automatic_policy.get("max_per_day")) is not int
                    or not 1 <= automatic_policy["max_per_day"] <= 24):
                raise AgentError("ARTICLE_POLICY_INVALID", 409)
            min_gap = automatic_policy["min_gap_seconds"]
            max_per_day = automatic_policy["max_per_day"]
            tz = ZoneInfo("Europe/Moscow")
        previous = list(session.scalars(select(Dispatch).join(Variant).where(
            Variant.target_id == target.id,
            Dispatch.state.in_(ACTIVE | {"published", "owner_attested"}))))
        day = datetime.fromtimestamp(due_at, tz).date()
        on_day = [dispatch for dispatch in previous
                  if datetime.fromtimestamp(dispatch.due_at, tz).date() == day]
        if len(on_day) >= max_per_day:
            raise AgentError("DAILY_LIMIT_REACHED", 409)
        if any(abs(dispatch.due_at - due_at) < min_gap for dispatch in previous):
            raise AgentError("SCHEDULE_GAP_TOO_SHORT", 409)

    def _approve_variant(self, ident, revision, body_hash, due_at, actor, *, human_confirmed=False,
                         machine_exception_confirmed=False, machine_review_notes="", automatic=False):
        from .human_review import eligible, hourly
        # Authorization must precede bridge synchronization or any other mutation.
        with self.db.session() as check:
            variant = self._variant(check, ident)
            if automatic:
                from .automatic_policy import POLICY_ACTOR, eligible as automatic_eligible
                if actor != POLICY_ACTOR:
                    raise AgentError("ARTICLE_AUTOMATIC_NOT_ELIGIBLE", 403)
                automatic_eligible(self, check, variant)
            elif hourly(variant) and (human_confirmed is not True or actor not in {"owner", "editor"}):
                raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
        self.bridge.sync(force=True)
        if type(due_at) not in {int, float} or not math.isfinite(due_at) or due_at < 0:
            raise AgentError("SCHEDULE_INVALID")
        due_at = float(due_at)
        with self.db.session.begin() as s:
            from .server_state import require_running
            # Общая пауза и согласование берут Control раньше Variant/Target.
            require_running(self, s, lock=True)
            v = self._variant(s, ident, True)
            t = s.scalar(select(Target).where(Target.id == v.target_id).with_for_update())
            snapshot = self.snapshot(v, t)
            if revision != v.revision or body_hash != fingerprint(snapshot):
                raise AgentError("REVISION_CONFLICT", 409)
            if automatic and (s.scalar(select(TargetApproval).where(TargetApproval.variant_id == v.id))
                    or s.scalar(select(Dispatch).where(Dispatch.variant_id == v.id))
                    or s.scalar(select(Audit).where(Audit.entity == v.id, Audit.action == "target_approve"))):
                # Preflight is advisory: even revoked/cancelled history inserted
                # before this locked transaction makes automatic approval unsafe.
                raise AgentError("ARTICLE_AUTOMATIC_NOT_READY", 409)
            self._quality(s, v, t)
            self.bridge.check_approval(s, v, due_at)
            human_hourly = hourly(v)
            exception_evidence, exception_notes = None, None
            if human_hourly:
                if automatic:
                    from .automatic_policy import automatic_evidence
                    policy = automatic_eligible(self, s, v)
                    policy_evidence = automatic_evidence(s, v)
                else:
                    policy = eligible(self, s, v)
                    exception_evidence = self._hourly_machine_exception(s, v)
                    if exception_evidence is not None:
                        if machine_exception_confirmed is not True:
                            raise AgentError("ARTICLE_MACHINE_EXCEPTION_CONFIRMATION_REQUIRED", 403)
                        exception_notes = _machine_exception_notes(machine_review_notes)
                    elif machine_exception_confirmed is not False or machine_review_notes != "":
                        raise AgentError("ARTICLE_MACHINE_EXCEPTION_NOT_ALLOWED", 409)
                if due_at < now():
                    raise AgentError("ARTICLE_AUTOMATIC_DATE_INVALID" if automatic else "HUMAN_RESCHEDULE_REQUIRED", 409)
                if s.scalar(select(Dispatch).join(Variant).where(Variant.target_id == t.id,
                                                               Dispatch.state.in_(ACTIVE))):
                    raise AgentError("ARTICLE_OUTSTANDING_DELIVERY", 409)
                from .hub_models import BridgePost
                if s.scalar(select(BridgePost).where(BridgePost.target_id == t.id,
                                                     BridgePost.mutation_owner.is_not(None))):
                    raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
                from .article_cycle import HourlyArticleCycle
                HourlyArticleCycle(self)._cadence(policy, due_at)
                if not automatic:
                    actor = "human_editor:" + actor
            if t.paused and not human_hourly:
                raise AgentError("TARGET_PAUSED", 409)
            if s.scalar(select(Dispatch).where(Dispatch.variant_id == ident, Dispatch.state.in_(ACTIVE | {"published", "owner_attested"}))):
                raise AgentError("DELIVERY_ALREADY_EXISTS", 409)
            self._check_schedule_limits(s, v, t, due_at,
                                        automatic_policy=policy if human_hourly and automatic else None)
            if human_hourly:
                t.paused = False
            # Snapshot plus schedule form one immutable approval.
            h = fingerprint({"content": snapshot, "due_at": due_at})
            a = TargetApproval(variant_id=v.id, revision=v.revision, snapshot_hash=h,
                               scheduled_at=due_at, actor=actor)
            s.add(a)
            s.flush()
            d = Dispatch(variant_id=v.id, approval_id=a.id, revision=v.revision,
                         snapshot_hash=h, snapshot=snapshot, due_at=due_at)
            s.add(d)
            v.state = "approved"
            self.db.audit(s, actor, "target_approve", v.id, revision=v.revision, hash=h)
            if human_hourly and automatic:
                self.db.audit(s, actor, "hourly_article_policy_review", a.id,
                              approval_id=a.id, variant_id=v.id, revision=v.revision,
                              content_hash=body_hash, snapshot_hash=h, due_at=due_at,
                              target_id=t.id, destination=t.destination,
                              evidence_hash=policy_evidence["evidence_hash"], policy_hash=fingerprint(policy),
                              review_rubric=v.metadata_json["review_rubric"],
                              approval_mode="automatic", owner_instruction=policy["owner_instruction"])
            elif human_hourly:
                self.db.audit(s, actor, "hourly_article_human_review", v.id,
                              revision=v.revision, content_hash=body_hash, due_at=due_at,
                              approval_id=a.id, machine_text_hash=v.metadata_json["machine_text_hash"],
                              machine_review_status="rejected" if exception_evidence is not None else "passed")
                if exception_evidence is not None:
                    self.db.audit(s, actor, "hourly_article_machine_exception", a.id,
                                  approval_id=a.id, variant_id=v.id, revision=v.revision,
                                  content_hash=body_hash, snapshot_hash=h, due_at=due_at,
                                  target_id=t.id, destination=t.destination,
                                  evidence_hash=exception_evidence["evidence_hash"],
                                  machine_exception_confirmed=True, notes=exception_notes,
                                  notes_hash=fingerprint(exception_notes))
            s.flush()
            return d.id

    def _valid_dispatch(self, s, d):
        from .human_review import HUMAN_ACTORS, eligible, hourly
        v = self._variant(s, d.variant_id)
        self.bridge.check_approval(s, v, d.due_at)
        t = self._target(s, v.target_id)
        a = s.get(TargetApproval, d.approval_id)
        current = fingerprint({"content": self.snapshot(v, t), "due_at": d.due_at})
        frozen = fingerprint({"content": d.snapshot, "due_at": d.due_at})
        if (not a or a.revoked or a.snapshot_hash != current or d.snapshot_hash != current
                or frozen != current or d.revision != v.revision or a.revision != v.revision):
            raise AgentError("APPROVAL_STALE", 409)
        if hourly(v):
            automatic = v.metadata_json.get("approval_mode") == "automatic"
            if automatic:
                from .automatic_policy import POLICY_ACTOR, automatic_evidence, check_approval
                from .automatic_policy import eligible as automatic_eligible
                if a.actor != POLICY_ACTOR:
                    raise AgentError("ARTICLE_POLICY_APPROVAL_STALE", 403)
                policy = automatic_eligible(self, s, v)
                check_approval(self, s, v, t, d, a, automatic_evidence(s, v), policy)
            else:
                if a.actor not in HUMAN_ACTORS:
                    raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
                policy = eligible(self, s, v)
                exception_evidence = self._hourly_machine_exception(s, v)
                if exception_evidence is not None:
                    self._check_machine_exception_approval(s, v, t, d, a, exception_evidence)
            if now() - d.due_at > 3600:
                raise AgentError("ARTICLE_AUTOMATIC_SLOT_EXPIRED" if automatic else "HUMAN_RESCHEDULE_REQUIRED", 409)
            if s.scalar(select(Dispatch).join(Variant).where(
                    Variant.target_id == t.id, Dispatch.id != d.id, Dispatch.state.in_(ACTIVE))):
                raise AgentError("ARTICLE_OUTSTANDING_DELIVERY", 409)
            from .hub_models import BridgePost
            if s.scalar(select(BridgePost).where(BridgePost.target_id == t.id,
                                                BridgePost.mutation_owner.is_not(None))):
                raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)
            from .article_cycle import HourlyArticleCycle
            HourlyArticleCycle(self)._cadence(policy, now())
        if t.paused:
            raise AgentError("TARGET_PAUSED", 409)
        self._quality(s, v, t)
        return v, t

    def export_manual(self, d, target):
        root = inside(self.settings.root, "data/exports/" + d.id)
        root.mkdir(parents=True, exist_ok=True)
        package = {**d.snapshot, "dispatch_id": d.id, "snapshot_hash": d.snapshot_hash,
                   "due_at": d.due_at, "state": "awaiting_manual"}
        (root / "post.json").write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        tags = ", ".join(d.snapshot.get("metadata", {}).get("tags", []))
        (root / "post.txt").write_text(d.snapshot["title"] + "\n\n" + d.snapshot["body"] + "\n\nТеги: " + tags, encoding="utf-8")
        import html
        (root / "post.html").write_text("<h1>" + html.escape(d.snapshot["title"]) + "</h1><p>" +
                                         html.escape(d.snapshot["body"]).replace("\n", "<br>") + "</p>", encoding="utf-8")
        import shutil
        for index, m in enumerate(d.snapshot["media"]):
            path = inside(self.settings.root, m["path"])
            shutil.copy2(path, root / f"media-{index}{path.suffix}")
        return Delivery("awaiting_manual")

    def _send(self, d, t):
        from .server_state import require_running
        require_running(self)
        self.bridge.guard_send(d, t)
        snap = d.snapshot
        from .platform_routes import DIRECT_ROUTES
        if t.route in DIRECT_ROUTES:
            return self.routes.publish(d, t)
        if t.route == "manual":
            return self.export_manual(d, t)
        if t.route in {"max_browser", "max_api"}:
            attachment = None
            if len(snap["media"]) > 1:
                raise AgentError("MAX_SINGLE_MEDIA_REQUIRED")
            if snap["media"]:
                m = snap["media"][0]
                path = inside(self.settings.root, m["path"])
                attachment = (self.publisher.upload_video(path) if m["mime"] == "video/mp4"
                              else self.publisher.upload_image(path))
            if t.route == "max_browser":
                return self.publisher.publish(t.destination, snap["body"], attachment, request_id=d.id)
            return self.publisher.publish(t.destination, snap["body"], attachment)
        if t.route == "postiz":
            # Public Postiz editors can bypass external approval without an ACL/guard.
            # Fail closed until an operator verifies a restricted editorial installation.
            if not self.publishing["postiz"].get("approval_guard_verified", False):
                raise AgentError("POSTIZ_APPROVAL_GUARD_REQUIRED")
            media = [self.postiz.upload(inside(self.settings.root, m["path"]), m["mime"])
                     for m in snap["media"]]
            ident = self.postiz.create(t, snap["body"], media, "schedule", d.due_at)
            return Delivery("scheduled", ident)
        raise AgentError("TARGET_ROUTE_UNSUPPORTED")

    def publish_due(self, *, human_hourly_only=False, hourly_only=False, platform_routes_only=False):
        from .server_state import require_running
        try:
            require_running(self)
        except AgentError as exc:
            if exc.code == "EDITORIAL_PAUSED":
                return 0
            raise
        completed = 0
        scope = None
        if platform_routes_only:
            from .platform_routes import DIRECT_ROUTES
            if human_hourly_only or hourly_only:
                raise AgentError("DISPATCH_SCOPE_INVALID")
            scope = (select(Dispatch.id).join(Variant).join(Target)
                     .where(Target.route.in_(DIRECT_ROUTES)))
        if human_hourly_only or hourly_only:
            from .article_cycle import HourlyArticleCycle
            from .human_review import HUMAN_ACTORS
            article_cycle = HourlyArticleCycle(self)
            policy = article_cycle._policy()
            mode = policy.get("approval_mode")
            if mode not in ({"human"} if human_hourly_only else {"human", "automatic"}):
                raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
            from .automatic_policy import POLICY_ACTOR
            actors = HUMAN_ACTORS if mode == "human" else {POLICY_ACTOR}
            scope = (select(Dispatch.id).join(Variant).join(Target)
                     .join(TargetApproval, TargetApproval.id == Dispatch.approval_id).where(
                         Target.id == policy["target_id"], Target.platform == "max", Target.route == "max_browser",
                         Variant.metadata_json["origin"].as_string() == "hourly_article_test",
                         Variant.metadata_json["approval_mode"].as_string() == mode,
                         # Каждый hash выводится из той же базовой политики и
                         # явно разрешённого source; чужой маршрут не включаем.
                         Variant.metadata_json["policy_hash"].as_string().in_(
                             [fingerprint(value) for value in article_cycle._policies()]),
                         TargetApproval.actor.in_(actors), TargetApproval.revoked.is_(False)))
        # Expired send leases are uncertain; never select them for a second send.
        with self.db.session.begin() as s:
            expired = update(Dispatch).where(Dispatch.state == "sending", Dispatch.lease_until < now())
            if scope is not None:
                expired = expired.where(Dispatch.id.in_(scope))
            s.execute(expired.values(state="unknown", error="SEND_LEASE_EXPIRED"))
        for _ in range(50):
            with self.db.session.begin() as s:
                try:
                    require_running(self, s, lock=True)
                except AgentError as exc:
                    if exc.code == "EDITORIAL_PAUSED":
                        break
                    raise
                query = select(Dispatch).join(Variant).join(Target).where(
                    Dispatch.state.in_({"pending", "retry_wait"}), Target.paused.is_(False),
                    or_(Dispatch.due_at <= now(), Target.route == "postiz")).order_by(Dispatch.due_at)
                if scope is not None:
                    query = query.where(Dispatch.id.in_(scope))
                # Lock content and dispatch together; editors take Variant first.
                # SKIP LOCKED avoids waiting on a concurrently edited variant.
                d = s.scalar(query.with_for_update(skip_locked=True, of=(Variant, Dispatch, Target)))
                if not d:
                    break
                try:
                    _, t = self._valid_dispatch(s, d)
                except AgentError as exc:
                    if exc.code in {"TARGET_PAUSED", "BRIDGE_EDIT_IN_PROGRESS", "ARTICLE_HOURLY_GAP",
                                    "ARTICLE_OUTSTANDING_DELIVERY"}:
                        # Pausing freezes work without invalidating its approval.
                        break
                    d.state, d.error = "cancelled", exc.code
                    continue
                d.state, d.owner, d.lease_until = "sending", self.worker_id, now() + 600
                d.attempts += 1
                ident = d.id
            error = None
            try:
                delivery = self._send(d, t)
                if delivery.state == "unknown" and delivery.evidence:
                    candidate = delivery.evidence.get("failure_code")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", candidate):
                        error = candidate
            except AgentError as exc:
                error = exc.code
                uncertain = {"POSTIZ_UNKNOWN", "BROWSER_OPERATION_UNCONFIRMED",
                             "TELEGRAM_UNKNOWN", "VK_UNKNOWN", "PIKABU_BROWSER_UNKNOWN",
                             "PIKABU_OPERATION_UNCONFIRMED", "PIKABU_BROWSER_OPERATION_UNKNOWN",
                             "VK_BROWSER_OPERATION_UNKNOWN"}
                delivery = Delivery("unknown" if exc.code in uncertain else "failed")
            except Exception:
                error, delivery = "DISPATCH_UNCONFIRMED", Delivery("unknown")
            with self.db.session.begin() as s:
                row = s.get(Dispatch, ident)
                if row.owner != self.worker_id:
                    continue
                row.state, row.external_id, row.permalink = delivery.state, delivery.external_id, delivery.permalink
                if row.state == "confirmed":
                    row.state = "published"
                    row.verified = (delivery.verified if delivery.verified is not None else
                                    getattr(self.publisher, "mode", "fake") != "fake")
                    completed += 1
                if row.state == "retry_wait":
                    if row.attempts >= 3:
                        row.state = "failed"
                    else:
                        row.state = "unknown"
                        error = "RETRY_REQUIRES_OPERATOR"
                row.error, row.owner, row.lease_until = error, None, 0
                self._variant(s, row.variant_id).state = row.state
                attempt = DispatchAttempt(dispatch_id=row.id, result=row.state, error=error, created_at=now())
                s.add(attempt)
                if delivery.evidence is not None:
                    self.db.audit(s, "platform_transport", "delivery_evidence", row.id,
                                  evidence=delivery.evidence, verified=row.verified,
                                  snapshot_hash=row.snapshot_hash)
                from .human_review import hourly
                if row.state == "published" and row.verified and hourly(self._variant(s, row.variant_id)):
                    self._target(s, self._variant(s, row.variant_id).target_id).paused = True
                    # Факт MAX и время попытки сохраняются вместе со слотом, даже
                    # если последующий GET Postiz ещё не подтвердил PUBLISHED.
                    s.flush()
                    self._record_reserve_delivery(s, row, attempt)
        return completed

    def _record_reserve_delivery(self, session, dispatch, attempt):
        variant = self._variant(session, dispatch.variant_id)
        if variant.metadata_json.get("reserve_contract") != "article-reserve-v1":
            return
        from .article_reserve import ArticleReserveStore
        from .server_models import PublicationSlot
        try:
            # Ошибка учёта резерва не должна откатить уже известную доставку.
            # Активный слот остаётся занят; контроллер сверит ту же отправку.
            with session.begin_nested():
                slot = session.scalar(select(PublicationSlot).where(
                    PublicationSlot.record_id == variant.metadata_json.get("record_id"),
                    PublicationSlot.target_id == variant.target_id).with_for_update())
                if slot is None:
                    raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_LOST", 409)
                ArticleReserveStore(self).update(slot.id, "delivered", token=slot.token,
                    dispatch_id=dispatch.id, delivered_at=attempt.created_at, session=session)
        except (AgentError, SQLAlchemyError) as exc:
            self.db.audit(session, "article_reserve", "reserve_delivery_persistence_failed", dispatch.id,
                error=exc.code if isinstance(exc, AgentError) else "ARTICLE_RESERVE_STATE_UNAVAILABLE",
                published_at=attempt.created_at, variant_id=variant.id)

    def stage_remote(self, variant_id, due_at=None):
        if due_at is not None and (
            not isinstance(due_at, (int, float)) or isinstance(due_at, bool)
            or not math.isfinite(due_at) or due_at < 0
        ):
            raise AgentError("SCHEDULE_INVALID")
        with self.db.session.begin() as s:
            v = self._variant(s, variant_id, True)
            t = self._target(s, v.target_id)
            bridge_target = t.route == "max_browser" and self.bridge.enabled and t.options.get("postiz_integration_id")
            if t.route != "postiz" and not bridge_target:
                raise AgentError("TARGET_NOT_POSTIZ")
            if due_at is not None and not bridge_target:
                raise AgentError("BRIDGE_SCHEDULE_REQUIRED")
            link = s.get(RemoteDraft, v.id)
            if link:
                raise AgentError("REMOTE_DRAFT_ALREADY_TRACKED", 409)
            link = RemoteDraft(variant_id=v.id, synced_revision=v.revision, correlation=uid())
            s.add(link)
        try:
            media = [self.postiz.upload(inside(self.settings.root, m["path"]), m["mime"])
                     for m in self.check_media(v.media, t)]
            if bridge_target:
                from types import SimpleNamespace
                remote_target = SimpleNamespace(platform="max-browser", destination=t.options["postiz_integration_id"],
                                                options={"settings": {"hub_variant_id": v.id, "hub_correlation": link.correlation}})
            else:
                remote_target = t
            ident = self.postiz.create(remote_target, v.body, media,
                                       "draft" if due_at is None else "schedule", due_at)
        except AgentError:
            with self.db.session.begin() as s:
                s.get(RemoteDraft, variant_id).state = "unknown"
            raise
        with self.db.session.begin() as s:
            link = s.get(RemoteDraft, variant_id)
            link.external_id, link.state = ident, "draft"
            link.remote_hash = fingerprint({"body": v.body})
        if bridge_target:
            self.bridge.sync(force=True)
        return ident

    def sync_remote(self, variant_id):
        with self.db.session() as s:
            v = self._variant(s, variant_id)
            link = s.get(RemoteDraft, variant_id)
            if not link or not link.external_id:
                raise AgentError("REMOTE_DRAFT_UNCONFIRMED", 409)
            old_revision, old_hash, external_id = link.synced_revision, link.remote_hash, link.external_id
        remote = self.postiz.get_post(external_id)
        if remote.get("state") != "DRAFT":
            raise AgentError("REMOTE_SCHEDULED_OUTSIDE_APPROVAL", 409)
        body = remote.get("content")
        if not isinstance(body, str):
            raise AgentError("POSTIZ_RESPONSE_INVALID")
        new_hash = fingerprint({"body": body})
        if new_hash == old_hash:
            return {"state": "unchanged"}
        if v.revision != old_revision:
            raise AgentError("REMOTE_EDIT_CONFLICT", 409)
        if v.media:
            raise AgentError("REMOTE_MEDIA_REVIEW_REQUIRED", 409)
        result = self.revise_variant(v.id, v.revision, body, actor="postiz_editor")
        with self.db.session.begin() as s:
            link = s.get(RemoteDraft, variant_id)
            link.remote_hash, link.synced_revision, link.updated_at = new_hash, result["revision"], now()
        return result

    def reconcile_dispatch(self, ident):
        with self.db.session() as s:
            d = s.get(Dispatch, ident)
            if not d or d.state not in {"unknown", "scheduled"}:
                raise AgentError("DISPATCH_NOT_RECONCILABLE", 409)
            t = self._target(s, d.snapshot["target_id"])
        if t.route == "max_browser":
            result = self.publisher.reconcile(t.destination, d.id)
        elif t.route == "postiz" and d.external_id:
            remote = self.postiz.get_post(d.external_id)
            state = remote.get("state")
            if remote.get("content") != d.snapshot["body"]:
                raise AgentError("REMOTE_CONTENT_CHANGED", 409)
            result = Delivery("confirmed" if state == "PUBLISHED" else "scheduled" if state == "QUEUE" else "unknown",
                              d.external_id, remote.get("releaseURL"))
        else:
            raise AgentError("MANUAL_RECONCILIATION_REQUIRED", 409)
        with self.db.session.begin() as s:
            row = s.get(Dispatch, ident)
            row.state = "published" if result.state == "confirmed" else result.state
            row.external_id, row.permalink = result.external_id, result.permalink
            row.verified = result.state == "confirmed"
            s.add(DispatchAttempt(dispatch_id=ident, result=row.state))
        return {"state": row.state, "verified": row.verified}

    def cancel_dispatch(self, ident):
        error = None
        with self.db.session.begin() as s:
            value = s.get(Dispatch, ident)
            if not value:
                raise AgentError("DISPATCH_CANNOT_CANCEL", 409)
            self._variant(s, value.variant_id, True)
            d = s.scalar(select(Dispatch).where(Dispatch.id == ident).with_for_update())
            if d.state not in {"pending", "retry_wait", "awaiting_manual", "scheduled", "cancelled", "failed"}:
                raise AgentError("DISPATCH_CANNOT_CANCEL", 409)
            if d.state == "scheduled":
                try:
                    self.postiz.delete(d.external_id)
                except AgentError as exc:
                    error = exc
                    d.state, d.error = "unknown", "REMOTE_CANCEL_UNCONFIRMED"
            if error is None:
                d.state = "cancelled"
                s.get(TargetApproval, d.approval_id).revoked = True
        if error:
            raise AgentError("REMOTE_CANCEL_UNCONFIRMED", 409) from error
        return {"state": "cancelled"}

    def attest_manual(self, ident, revision, snapshot_hash, url, actor="owner"):
        from .pikabu_metadata import story_url
        url = story_url(url)
        with self.db.session.begin() as s:
            d = s.get(Dispatch, ident)
            if not d or d.state != "awaiting_manual" or d.revision != revision or d.snapshot_hash != snapshot_hash:
                raise AgentError("MANUAL_CONFIRMATION_STALE", 409)
            self._valid_dispatch(s, d)
            if s.scalar(select(Dispatch).where(Dispatch.permalink == url, Dispatch.id != ident)):
                raise AgentError("PIKABU_STORY_ALREADY_RECORDED", 409)
            d.state, d.permalink, d.verified = "owner_attested", url, False
            self.db.audit(s, actor, "manual_attestation", d.id, url=url, live_verified=False)
        return {"state": "owner_attested", "live_verified": False}

    def search_archive(self, query):
        with self.db.session() as s:
            if self.db.engine.dialect.name == "postgresql":
                from sqlalchemy import func
                vector = func.to_tsvector("russian", Material.title + " " + Material.text)
                q = select(Material).where(vector.op("@@")(func.plainto_tsquery("russian", query)))
            else:
                q = select(Material).where(Material.text.contains(query, autoescape=True))
            rows = [serialize(m) for m in s.scalars(q.limit(100))]
            # Imported/manual stories may have no source Material; preserve searchability.
            standalone = select(Story).where(~select(Material.id).where(Material.story_id == Story.id).exists())
            if self.db.engine.dialect.name == "postgresql":
                standalone = standalone.where(func.to_tsvector("russian", Story.title + " " + Story.text).op("@@")(func.plainto_tsquery("russian", query)))
            else:
                standalone = standalone.where(Story.text.contains(query, autoescape=True))
            rows.extend({**serialize(story), "kind": "standalone_story"} for story in s.scalars(standalone.limit(max(1, 100 - len(rows)))))
            return rows[:100]

    def collect(self, source_id):
        from .collectors import collect_web
        source = self.settings.source(source_id)
        with self.db.session() as s:
            cursor = dict(s.get(Cursor, source_id).data)
        if cursor.get("blocked"):
            raise AgentError("SOURCE_PAUSED", 409)
        if source["type"] not in {"rss", "html", "json_api"}:
            return super().collect(source_id)
        if not source["enabled"]:
            raise AgentError("SOURCE_DISABLED")
        if not source["rights"]["verified"] or not source["rights"]["allow_storage"]:
            raise AgentError("SOURCE_STORAGE_NOT_AUTHORIZED")
        core = self

        class RecordingHTTP:
            def get(self, url, headers=None):
                result = core.http.get(url, headers)
                status, raw, returned, actual = result
                if status == 200:
                    digest = hashlib.sha256(raw).hexdigest()
                    rel = f"data/archive/{source_id}/{digest}.bin"
                    path = inside(core.settings.root, rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        path.write_bytes(raw)
                    with core.db.session.begin() as s:
                        existing = s.scalar(select(SourceArchive).where(SourceArchive.source_id == source_id,
                                                                       SourceArchive.response_hash == digest))
                        if not existing:
                            s.add(SourceArchive(source_id=source_id, response_hash=digest, blob_path=rel,
                              metadata_json={"status": status, "content_type": returned.get("content-type", ""),
                                             "etag": returned.get("etag"), "parser": "max-postiz-0.3"}))
                return result
        try:
            items, new_cursor = collect_web(source, cursor, RecordingHTTP(), self.settings.root)
            return self.ingest(source_id, items, new_cursor)
        except AgentError as exc:
            if exc.code in {"SOURCE_HTTP_401", "SOURCE_HTTP_403", "SOURCE_HTTP_429", "SOURCE_CAPTCHA"}:
                with self.db.session.begin() as s:
                    row = s.get(Cursor, source_id)
                    row.data = {**row.data, "blocked": True, "blocked_reason": exc.code,
                                "retry_after": getattr(exc, "retry_after", None)}
                    row.status = exc.code
                    s.execute(update(Job).where(Job.kind == "collect", Job.subject_id == source_id,
                                                Job.state.in_({"queued", "retry_wait"})).values(state="cancelled"))
            raise

    def schedule_sources(self):
        with self.db.session.begin() as s:
            for source in self.settings.sources["sources"]:
                cursor = s.get(Cursor, source["id"])
                if cursor and cursor.data.get("blocked"):
                    cursor.next_due = max(cursor.next_due, now() + 86400)
        return super().schedule_sources()

    def resume_source(self, ident):
        with self.db.session.begin() as s:
            cursor = s.get(Cursor, ident)
            if not cursor:
                raise AgentError("SOURCE_NOT_FOUND", 404)
            cursor.data = {k: v for k, v in cursor.data.items() if k not in {"blocked", "blocked_reason", "retry_after"}}
            cursor.next_due, cursor.status = now(), "resumed"

    def adapt_variant(self, ident):
        with self.db.session() as s:
            v = self._variant(s, ident)
            t = self._target(s, v.target_id)
            context = {"platform": t.platform, "body": v.body, "title": v.title, "sources": v.sources,
                       "max_chars": t.options.get("max_chars", 3500), "instructions":
                       "Adapt for this platform. Preserve facts and source links. Return JSON with title and body strings only."}
        result = self.model_task("adapt", t.profile_id, context)
        if set(result) != {"body", "title"} or not all(isinstance(x, str) for x in result.values()):
            raise AgentError("ADAPT_RESULT_INVALID")
        return self.revise_variant(v.id, v.revision, result["body"], result["title"], actor="local_llm")

    def probe_llm(self, draft=False):
        context = {"materials": [{"id": "probe", "title": "Проверка редакции", "text": "Локальная нейросеть подготовила короткое видео.",
                                  "url": "https://example.org/editorial-probe", "source_id": "fixture"}],
                   "channel": self.settings.topic["channels"][0]}
        text, usage = self.editor.generate_text('Верни только JSON: {"local": true, "text": "Проверка связи"}. Не вызывай инструменты.')
        from .adapters import extract_object
        result = extract_object(text)
        if result.get("local") is not True:
            raise AgentError("LLM_PROBE_INVALID")
        if draft:
            result = self.model_task("draft", self.settings.topic["channels"][0]["id"], context)
            if not isinstance(result.get("body"), str) or not result["body"].strip():
                raise AgentError("LLM_DRAFT_INVALID")
        return {"live_verified": True, "model": self.editor.model, "through_opencode": True,
                "result": result, "usage": usage}

    def recurring_due(self):
        for _ in range(10):
            with self.db.session.begin() as s:
                task = s.scalar(select(RecurringTask).where(RecurringTask.enabled.is_(True),
                                    RecurringTask.due_at <= now(), RecurringTask.lease_until <= now(),
                                    RecurringTask.attempts < 3).with_for_update(skip_locked=True))
                if not task:
                    break
                task.owner, task.lease_until = self.worker_id, now() + 900
                task.attempts += 1
                if not task.last_slot:
                    task.last_slot = str(int(task.due_at))
                slot = task.last_slot
                ident, profile_id = task.id, task.profile_id
                materials = [serialize(m) for m in s.scalars(select(Material).where(
                    Material.created_at >= now() - task.lookback_hours * 3600).order_by(Material.created_at.desc()).limit(100))
                    if profile_id in self.settings.source(m.source_id)["channel_ids"]]
            error = None
            try:
                if not materials:
                    raise AgentError("DIGEST_NO_MATERIALS")
                by_story = {}
                for m in materials:
                    by_story.setdefault(m["story_id"], m)
                materials = list(by_story.values())[:20]
                context = {"channel": self.settings.channel(profile_id), "materials": materials,
                           "instructions": "Prepare a digest of the supplied materials, with source links. Do not invent facts."}
                result = self.model_task("draft", profile_id, context)
                checks = validate_draft(result, materials, self.settings.channel(profile_id))
                result["checks"] = checks
                with self.db.session.begin() as s:
                    current = s.get(RecurringTask, ident)
                    if current.owner != self.worker_id:
                        continue
                    hashed = fingerprint({"task": ident, "slot": slot})
                    story = s.scalar(select(Story).where(Story.text_hash == hashed))
                    if not story:
                        story = Story(text_hash=hashed, title="Дайджест", text="\n".join(m["text"] for m in materials))
                        s.add(story)
                        s.flush()
                    if not s.scalar(select(Draft).where(Draft.channel_id == profile_id, Draft.story_id == story.id)):
                        d = Draft(channel_id=profile_id, story_id=story.id, current=result,
                                  config_id=self.settings.hash, state="review_pending")
                        s.add(d)
                        s.flush()
                        s.add(DraftRevision(draft_id=d.id, revision=1, payload=result, config_id=self.settings.hash))
            except AgentError as exc:
                error = exc.code
            except Exception:
                error = "DIGEST_FAILED"
            with self.db.session.begin() as s:
                task = s.get(RecurringTask, ident)
                if task.owner != self.worker_id:
                    continue
                task.owner, task.lease_until, task.error = None, 0, error
                task.due_at = now() + (60 * task.attempts if error else task.interval_seconds)
                if not error:
                    task.attempts, task.last_slot = 0, None

    def resume_task(self, ident, actor="owner"):
        with self.db.session.begin() as s:
            task = s.scalar(select(RecurringTask).where(RecurringTask.id == ident).with_for_update())
            if not task:
                raise AgentError("TASK_NOT_FOUND", 404)
            if not task.enabled or (task.owner and task.lease_until > now()):
                raise AgentError("TASK_NOT_RESUMABLE", 409)
            task.attempts, task.error, task.due_at = 0, None, now()
            task.owner, task.lease_until = None, 0
            self.db.audit(s, actor, "task_resume", ident)

    def tick(self):
        try:
            self.bridge.sync()
        except AgentError as exc:
            self.bridge.last_error = exc.code
        self.recurring_due()
        value = super().tick()
        with self.db.session() as s:
            drafts = list(s.scalars(select(Draft.id).where(Draft.state == "review_pending")))
        for ident in drafts:
            self.make_variants(ident)
        try:
            self.reddit_mail.poll()
        except AgentError as exc:
            # Dedicated mailbox status exposes the safe error without stopping dispatches.
            self.reddit_mail_error = exc.code
        self.reddit_intake.process_one()
        return value

    def dispatch_tick(self):
        """One existing worker, restricted to the human-approved hourly route."""
        from .article_cycle import HourlyArticleCycle
        try:
            policy = HourlyArticleCycle(self)._policy()
        except AgentError as exc:
            if exc.code == "ARTICLE_POLICY_DISABLED":
                return 0
            raise
        if policy.get("approval_mode") not in {"human", "automatic"}:
            raise AgentError("HUMAN_REVIEW_CONFIRMATION_REQUIRED", 403)
        try:
            self.bridge.sync(target_id=policy["target_id"])
        except AgentError as exc:
            self.bridge.last_error = exc.code
            return 0
        return self.publish_due(hourly_only=True)

    def close(self):
        self.routes.close()
        self.postiz.close()
        super().close()
