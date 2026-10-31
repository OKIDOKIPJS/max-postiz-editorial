"""Управление четырьмя маршрутами без автоматического включения новых адресатов."""

from sqlalchemy import select

from .config import AgentError, fingerprint
from .db import Audit, now
from .hub_models import Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from .platform_routes import DIRECT_ROUTES


def _evidence_kind(dispatch, audits):
    """Короткая метка только сохранённого proof, связанного с exact snapshot."""
    if fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at}) != dispatch.snapshot_hash:
        return "unverified"
    for audit in audits:
        details = audit.details
        if not isinstance(details, dict) or details.get("snapshot_hash") != dispatch.snapshot_hash:
            continue
        value = details.get("evidence")
        if not isinstance(value, dict):
            return "unverified"
        if value.get("fixture") is True:
            return "fixture"
        if value.get("fixture") is not False:
            return "unverified"
        independent = ((value.get("verification") == "browser_readback"
                        and value.get("independent_readback") is True) or (
            value.get("provider") == "vk" and value.get("code") == "VK_EXACT_READBACK"
            and value.get("readback_exact") is True))
        if dispatch.verified is True and details.get("verified") is True and independent:
            return "independent_readback"
        if (value.get("verification") == "api_receipt"
                and value.get("independent_readback") is False):
            return "api_receipt"
        return "unverified"
    return "unverified"


def route_state(core):
    with core.db.session() as session:
        targets = [core.routes.status(target) for target in
                   session.scalars(select(Target).order_by(Target.id))]
        variants = []
        for variant in session.scalars(select(Variant).order_by(Variant.updated_at.desc()).limit(100)):
            target = core._target(session, variant.target_id)
            snapshot = core.snapshot(variant, target)
            variants.append({"id": variant.id, "target_id": variant.target_id,
                "title": variant.title, "body": variant.body, "revision": variant.revision,
                "state": variant.state, "content_hash": fingerprint(snapshot),
                "media_count": len(variant.media), "source_count": len(variant.sources),
                "origin": variant.metadata_json.get("origin"),
                "approval_mode": variant.metadata_json.get("approval_mode"),
                "manual_approval_allowed": variant.metadata_json.get("origin") != "hourly_article_test"})
        dispatches = []
        recent = list(session.scalars(select(Dispatch).order_by(Dispatch.due_at.desc()).limit(100)))
        audits = {}
        if recent:
            for audit in session.scalars(select(Audit).where(
                    Audit.entity.in_([row.id for row in recent]),
                    Audit.action.in_({"delivery_evidence", "route_reconcile_read_only"}))
                    .order_by(Audit.at.desc(), Audit.id.desc())):
                audits.setdefault(audit.entity, []).append(audit)
        for dispatch in recent:
            dispatches.append({"id": dispatch.id, "target_id": dispatch.snapshot.get("target_id"),
                "destination": dispatch.snapshot.get("destination"),
                "platform": dispatch.snapshot.get("platform"), "route": dispatch.snapshot.get("route"),
                "title": dispatch.snapshot.get("title"), "state": dispatch.state,
                "due_at": dispatch.due_at, "verified": dispatch.verified,
                "external_id": dispatch.external_id, "permalink": dispatch.permalink,
                "error": dispatch.error, "attempts": dispatch.attempts,
                "snapshot_hash": dispatch.snapshot_hash,
                "evidence_kind": _evidence_kind(dispatch, audits.get(dispatch.id, []))})
        return {"targets": targets, "variants": variants, "dispatches": dispatches,
                "configuration_only": True}


def preflight(core, variant_id):
    """Проверки содержимого и файлов не выполняют ни одного HTTP-запроса."""
    with core.db.session() as session:
        variant = core._variant(session, variant_id)
        target = core._target(session, variant.target_id)
        core._quality(session, variant, target)
        snapshot = core.snapshot(variant, target)
        return {"variant_id": variant.id, "revision": variant.revision,
                "content_hash": fingerprint(snapshot), "target_id": target.id,
                "destination": target.destination, "route": target.route,
                "plan": core.routes.validate(target, snapshot),
                "preflight_only": True, "live_verified": False}


def inspect_target(core, target_id):
    with core.db.session() as session:
        target = core._target(session, target_id)
    result = core.routes.inspect(target)
    # Доступ к площадке никогда не является подтверждением доставки нового поста.
    return {"target_id": target.id, "access": result, "delivery_verified": False}


def preview(core, variant_id):
    """Миниатюры только проверенных локальных bytes; оригиналы не изменяются."""
    import base64
    import hashlib
    import io
    import json

    from PIL import Image, ImageOps

    from .config import inside

    with core.db.session() as session:
        variant = core._variant(session, variant_id)
        target = core._target(session, variant.target_id)
        core.check_media(variant.media, target)
        snapshot = core.snapshot(variant, target)
        images = []
        for item in variant.media:
            if item["mime"] not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            with inside(core.settings.root, item["path"]).open("rb") as stream:
                raw = stream.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise AgentError("ROUTE_PREVIEW_SIZE_INVALID")
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise AgentError("MEDIA_HASH_CHANGED", 409)
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                width, height = image.size
                image.thumbnail((640, 640))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=75)
                while output.tell() > 50000 and min(image.size) > 128:
                    image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=70)
                if output.tell() > 50000:
                    raise AgentError("ROUTE_PREVIEW_SIZE_INVALID")
                images.append({"data_url": "data:image/jpeg;base64," + base64.b64encode(
                    output.getvalue()).decode("ascii"), "sha256": item["sha256"],
                    "mime": "image/jpeg", "source_mime": item["mime"],
                    "width": width, "height": height, "preview": True})
        result = {"variant_id": variant.id, "revision": variant.revision,
                  "content_hash": fingerprint(snapshot), "images": images, "preflight_only": True}
        if len(json.dumps(result).encode("utf-8")) > 900000:
            raise AgentError("ROUTE_PREVIEW_SIZE_INVALID")
        return result


def reconcile(core, dispatch_id, actor="owner"):
    """Только чтение внешнего результата, без повторной загрузки или отправки."""
    with core.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        if dispatch is None or dispatch.state != "unknown":
            raise AgentError("ROUTE_RECONCILE_STATE_INVALID", 409)
        variant = core._variant(session, dispatch.variant_id)
        target = core._target(session, variant.target_id)
        if target.route not in DIRECT_ROUTES:
            raise AgentError("TARGET_ROUTE_UNSUPPORTED", 409)
        approval = session.get(TargetApproval, dispatch.approval_id)
        current = fingerprint({"content": core.snapshot(variant, target), "due_at": dispatch.due_at})
        stored = fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
        if (approval is None or approval.revoked or approval.snapshot_hash != current
                or dispatch.snapshot_hash != current or stored != current or dispatch.owner is not None):
            raise AgentError("APPROVAL_STALE", 409)
        frozen = dispatch.snapshot_hash
    delivery = core.routes.reconcile(dispatch, target)
    with core.db.session.begin() as session:
        # Редактор также начинает с Variant: не инвертируем порядок блокировок.
        variant = session.scalar(select(Variant).where(
            Variant.id == dispatch.variant_id).with_for_update())
        current_target = session.scalar(select(Target).where(
            Target.id == variant.target_id).with_for_update())
        row = session.scalar(select(Dispatch).where(Dispatch.id == dispatch_id).with_for_update())
        current = fingerprint({"content": core.snapshot(variant, current_target), "due_at": row.due_at})
        stored = fingerprint({"content": row.snapshot, "due_at": row.due_at})
        approval = session.scalar(select(TargetApproval).where(
            TargetApproval.id == row.approval_id).with_for_update())
        if (row.state != "unknown" or row.owner is not None or row.snapshot_hash != frozen
                or current != frozen or stored != frozen or approval is None or approval.revoked):
            raise AgentError("APPROVAL_STALE", 409)
        if delivery.external_id:
            row.external_id = delivery.external_id
        if delivery.state == "confirmed":
            row.state, row.error = "published", None
            row.permalink, row.verified = delivery.permalink, delivery.verified is True
            variant.state = row.state
        session.add(DispatchAttempt(dispatch_id=row.id, result=row.state,
                                    error=row.error, created_at=now()))
        core.db.audit(session, actor, "route_reconcile_read_only", row.id,
                      snapshot_hash=frozen, result=row.state, verified=row.verified,
                      evidence=delivery.evidence or {})
        return {"id": row.id, "state": row.state, "verified": row.verified,
                "external_id": row.external_id, "permalink": row.permalink,
                "external_mutations": 0}


def evidence(core, dispatch_id):
    with core.db.session() as session:
        if session.get(Dispatch, dispatch_id) is None:
            raise AgentError("DISPATCH_NOT_FOUND", 404)
        return [{"action": row.action, "details": row.details, "created_at": row.at}
                for row in session.scalars(select(Audit).where(Audit.entity == dispatch_id,
                    Audit.action.in_({"delivery_evidence", "route_reconcile_read_only"})))]
