"""Локальный ввод авторского текста: права подтверждает оператор, отправки нет."""

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .collectors import detect_language
from .config import AgentError, fingerprint, parse_json
from .db import Audit, Draft, DraftRevision, Story
from .hub_models import Variant, VariantRevision
from .isolated_workspace import assert_isolated
from .platform_routes import DIRECT_ROUTES


CONTRACT = "owner-original-v1"
ORIGIN = "owner_original"
ACTOR = "owner_local_original"
_INTERNAL = {"origin", "owner_original_contract", "owner_original_attested_hash"}
_METADATA = {"tags", "community", "ai_generated"}


def read_original_file(root, filename):
    """Читаем только ограниченный JSON внутри копии, без ссылок и вывода текста."""
    root = Path(root).resolve()
    assert_isolated(root, environment=False)
    try:
        path = Path(filename)
        path = path if path.is_absolute() else root / path
        if not path.resolve().is_relative_to(root):
            raise ValueError
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise ValueError
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        return parse_json(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise AgentError("OWNER_ORIGINAL_FILE_INVALID") from None


def _input(value):
    if not isinstance(value, dict) or set(value) != {"title", "body", "media", "metadata"}:
        raise AgentError("OWNER_ORIGINAL_INPUT_INVALID")
    title, body, media, metadata = (value[key] for key in ("title", "body", "media", "metadata"))
    if (not isinstance(title, str) or not title.strip() or len(title) > 300
            or any(char in title for char in "\0\r\n")
            or not isinstance(body, str) or not body.strip() or "\0" in body or len(body) > 40000):
        raise AgentError("OWNER_ORIGINAL_TEXT_INVALID")
    try:
        (title + body).encode("utf-8")
    except UnicodeError:
        raise AgentError("OWNER_ORIGINAL_TEXT_INVALID") from None
    if detect_language(body) != "ru":
        raise AgentError("OWNER_ORIGINAL_RUSSIAN_REQUIRED")
    if not isinstance(media, list) or len(media) > 1:
        raise AgentError("OWNER_ORIGINAL_MEDIA_INVALID")
    for item in media:
        if (not isinstance(item, dict) or set(item) != {"path", "mime", "sha256"}
                or not isinstance(item["path"], str) or not item["path"]
                or Path(item["path"]).is_absolute() or ":" in item["path"]
                or not isinstance(item["mime"], str) or item["mime"] not in {"image/jpeg", "image/png"}
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise AgentError("OWNER_ORIGINAL_MEDIA_INVALID")
    if not isinstance(metadata, dict) or set(metadata) - _METADATA:
        raise AgentError("OWNER_ORIGINAL_METADATA_INVALID")
    if "tags" in metadata and (not isinstance(metadata["tags"], list) or len(metadata["tags"]) > 10
            or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 50 for tag in metadata["tags"])):
        raise AgentError("OWNER_ORIGINAL_METADATA_INVALID")
    if "community" in metadata and (not isinstance(metadata["community"], str) or len(metadata["community"]) > 200):
        raise AgentError("OWNER_ORIGINAL_METADATA_INVALID")
    if "ai_generated" in metadata and type(metadata["ai_generated"]) is not bool:
        raise AgentError("OWNER_ORIGINAL_METADATA_INVALID")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
        return json.loads(encoded)
    except (ValueError, TypeError, UnicodeError):
        raise AgentError("OWNER_ORIGINAL_INPUT_INVALID") from None


def _route(target):
    routes = {**DIRECT_ROUTES, "max_browser": "max"}
    if target.route not in routes or routes[target.route] != target.platform:
        raise AgentError("OWNER_ORIGINAL_ROUTE_REQUIRED", 409)


def attestation_hash(title, body, media):
    return fingerprint({"title": title, "body": body, "media": media})


def _story_hash(target_id, digest):
    return fingerprint({"contract": CONTRACT, "target_id": target_id, "attestation_hash": digest})


def create_original(core, target_id, value, *, attest_original=False):
    """Одна транзакция; никакого approval, enable/resume, transport или модели."""
    assert_isolated(core.settings.root, environment=False)
    return _create_original(core, target_id, value, attest_original=attest_original)


def _create_original(core, target_id, value, *, attest_original=False, expected_binding=None, owner_actor=None):
    """Общий canonical writer; сервер вызывает его только после owner dependency."""
    if attest_original is not True:
        raise AgentError("OWNER_ORIGINAL_ATTESTATION_REQUIRED", 403)
    supplied = _input(value)
    title, body, media, metadata = (supplied[key] for key in ("title", "body", "media", "metadata"))
    digest = attestation_hash(title, body, media)
    try:
        with core.db.session.begin() as session:
            target = core._target(session, target_id)
            # Сериализуем повторные создания для одного адресата в PostgreSQL.
            session.refresh(target, with_for_update=True)
            if expected_binding is not None and target.binding_hash != expected_binding:
                raise AgentError("TARGET_BINDING_CONFLICT", 409)
            _route(target)
            profile = core.settings.channel(target.profile_id)
            if not profile["enabled"] or profile["publication"]["mode"] == "dry_run":
                raise AgentError("PROFILE_NOT_PUBLISHABLE", 409)
            if not target.enabled:
                raise AgentError("TARGET_DISABLED", 409)
            if target.platform == "pikabu":
                from .pikabu_metadata import metadata as pikabu_metadata
                if pikabu_metadata(title, metadata) != metadata:
                    raise AgentError("OWNER_ORIGINAL_PIKABU_METADATA_NOT_EXACT")
            core.check_media(media, target)
            story_hash = _story_hash(target.id, digest)
            if session.scalar(select(Story.id).where(Story.text_hash == story_hash)):
                raise AgentError("OWNER_ORIGINAL_ALREADY_EXISTS", 409)
            story = Story(text_hash=story_hash, title=title, text=body)
            session.add(story)
            session.flush()
            payload = {"origin": ORIGIN, "contract": CONTRACT, "target_id": target.id,
                       "attestation_hash": digest, "media": media, "metadata": metadata}
            current = {"title": title, "body": body, "language": "ru", "source_ids": [],
                       "decision": "review", "reason_codes": ["OWNER_ORIGINAL_ATTESTED"],
                       "claims": [], "confidence": 0.0, "payload": payload}
            draft = Draft(channel_id=target.profile_id, story_id=story.id, revision=1,
                          state="review_pending", current=current, config_id=core.settings.hash)
            session.add(draft)
            session.flush()
            session.add(DraftRevision(draft_id=draft.id, revision=1, payload=current,
                                      config_id=core.settings.hash))
            variant = Variant(draft_id=draft.id, target_id=target.id, revision=1,
                title=title, body=body, media=media, sources=[], source_revision=1,
                config_hash=core.settings.hash, binding_hash=target.binding_hash,
                metadata_json={**metadata, "origin": ORIGIN, "owner_original_contract": CONTRACT,
                               "owner_original_attested_hash": digest}, state="review_pending")
            session.add(variant)
            session.flush()
            core._history(session, variant, target, ACTOR)
            core.db.audit(session, ACTOR, "owner_original_attested", variant.id,
                          contract=CONTRACT, attestation_hash=digest, target_id=target.id,
                          draft_id=draft.id, revision=1, metadata_hash=fingerprint(metadata),
                          original_rights_attested=True, approval_granted=False, model_verified=False)
            if owner_actor is not None:
                core.db.audit(session, owner_actor, "server_owner_original_created", variant.id,
                              revision=1, content_hash=fingerprint(core.snapshot(variant, target)))
            return {"story_id": story.id, "draft_id": draft.id, "variant_id": variant.id,
                    "target_id": target.id, "revision": 1, "state": "review_pending",
                    "content_hash": fingerprint(core.snapshot(variant, target)),
                    "attestation_hash": digest, "approval_created": False, "external_mutations": 0}
    except IntegrityError:
        raise AgentError("OWNER_ORIGINAL_ALREADY_EXISTS", 409) from None


def require_attestation(core, session, variant, target):
    """Узкий gate для Hub._quality; старые source/bridge/hourly пути не меняет."""
    _route(target)
    metadata = variant.metadata_json
    if (not isinstance(metadata, dict) or metadata.get("origin") != ORIGIN
            or metadata.get("owner_original_contract") != CONTRACT or variant.sources != []):
        raise AgentError("OWNER_ORIGINAL_ATTESTATION_REQUIRED", 409)
    supplied_metadata = {key: value for key, value in metadata.items() if key not in _INTERNAL}
    _input({"title": variant.title, "body": variant.body,
            "media": variant.media, "metadata": supplied_metadata})
    digest = attestation_hash(variant.title, variant.body, variant.media)
    if metadata.get("owner_original_attested_hash") != digest:
        raise AgentError("OWNER_ORIGINAL_ATTESTATION_STALE", 409)
    draft = session.get(Draft, variant.draft_id)
    story = session.get(Story, draft.story_id) if draft is not None else None
    expected_payload = {"origin": ORIGIN, "contract": CONTRACT, "target_id": target.id,
                        "attestation_hash": digest, "media": variant.media, "metadata": supplied_metadata}
    expected_current = {"title": variant.title, "body": variant.body, "language": "ru", "source_ids": [],
                        "decision": "review", "reason_codes": ["OWNER_ORIGINAL_ATTESTED"],
                        "claims": [], "confidence": 0.0, "payload": expected_payload}
    if (draft is None or story is None or draft.revision != variant.source_revision
            or draft.channel_id != target.profile_id or draft.config_id != variant.config_hash
            or draft.current != expected_current
            or story.title != variant.title or story.text != variant.body
            or story.text_hash != _story_hash(target.id, digest)):
        raise AgentError("OWNER_ORIGINAL_CANONICAL_RECORD_INVALID", 409)
    revisions = list(session.scalars(select(DraftRevision).where(
        DraftRevision.draft_id == draft.id, DraftRevision.revision == draft.revision)))
    history = list(session.scalars(select(VariantRevision).where(
        VariantRevision.variant_id == variant.id, VariantRevision.revision == variant.revision)))
    if (len(revisions) != 1 or fingerprint(revisions[0].payload) != fingerprint(draft.current)
            or revisions[0].config_id != variant.config_hash or len(history) != 1
            or history[0].actor != ACTOR
            or fingerprint(history[0].payload) != fingerprint(core.snapshot(variant, target))):
        raise AgentError("OWNER_ORIGINAL_CANONICAL_RECORD_INVALID", 409)
    audits = list(session.scalars(select(Audit).where(
        Audit.entity == variant.id, Audit.action == "owner_original_attested")))
    expected = {"contract": CONTRACT, "attestation_hash": digest, "target_id": target.id,
                "draft_id": draft.id, "revision": variant.revision,
                "metadata_hash": fingerprint(supplied_metadata), "original_rights_attested": True,
                "approval_granted": False, "model_verified": False}
    audits = [audit for audit in audits if audit.details.get("revision") == variant.revision]
    if len(audits) != 1 or audits[0].actor != ACTOR or audits[0].details != expected:
        raise AgentError("OWNER_ORIGINAL_AUDIT_REQUIRED", 409)
    return True
