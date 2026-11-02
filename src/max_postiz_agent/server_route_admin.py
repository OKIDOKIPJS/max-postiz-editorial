"""Адресная настройка из owner API; без синхронизации всех каналов и отправки."""

import base64
import copy
import hashlib
import io
import math
import os
from pathlib import Path
import re
import uuid

from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .browser_runtime import profile_lock
from .config import AgentError, Settings, fingerprint, parse_json, validate_schema
from .db import Audit, Draft, DraftRevision, Story, now
from .hub import ACTIVE
from .hub_models import BridgePost, Dispatch, Target, Variant
from .owner_original import ACTOR, CONTRACT, ORIGIN, _input, _story_hash, attestation_hash
from .server_models import EditorialControl


ROUTES = {"max": "max_browser", "telegram": "telegram_api", "vk": "vk_browser"}
EDITABLE = {"max": {"max_chars"}, "telegram": {"max_chars", "media_layout", "request_timeout_seconds"},
            "vk": {"owner", "max_chars", "operation_timeout_seconds"}}
MEDIA_LIMIT = 10 * 1024 * 1024
LOGIN_PATH = "data/owner-browser-selection.json"
PRIVATE_DIR = "data/route-admin-private"
PENDING_PATH = PRIVATE_DIR + "/pending.json"
INTENT_CONTRACT = "route-admin-file-db-v1"


def plain(root, relative):
    """Проверяем каждый компонент до I/O; профили и секреты не разыменовываем."""
    root = Path(root).resolve()
    value = Path(relative)
    if value.is_absolute() or ":" in str(value) or ".." in value.parts:
        raise AgentError("ROUTE_ADMIN_PATH_INVALID", 409)
    current = root
    for part in value.parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise AgentError("ROUTE_ADMIN_PATH_INVALID", 409)
    if not current.resolve().is_relative_to(root):
        raise AgentError("ROUTE_ADMIN_PATH_INVALID", 409)
    return current


def read_local(root, relative, limit=1024 * 1024):
    path = plain(root, relative)
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise OSError
        return raw
    except OSError:
        raise AgentError("ROUTE_ADMIN_FILE_INVALID", 409) from None


def atomic_write(root, relative, raw):
    path = plain(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plain(root, str(path.relative_to(root)) + ".tmp-" + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Проверяем путь повторно непосредственно перед заменой.
        plain(root, relative)
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise AgentError("ROUTE_ADMIN_WRITE_FAILED", 503) from None
    finally:
        temporary.unlink(missing_ok=True)


def _json(value):
    import json
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def guard_pending(core):
    """A pending file/DB operation never grants permission to start a worker."""
    if plain(core.settings.root, PENDING_PATH).exists():
        raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)


def _target_digest(target):
    if target is None:
        return None
    return fingerprint({key: getattr(target, key) for key in (
        "id", "profile_id", "platform", "route", "destination", "enabled", "paused", "binding_hash", "options")})


def _audit_digest(rows):
    return fingerprint([{key: getattr(row, key) for key in ("id", "actor", "action", "entity", "details")}
                        for row in sorted(rows, key=lambda row: row.id)])


def _file_digest(root, relative):
    path = plain(root, relative)
    return hashlib.sha256(read_local(root, relative)).hexdigest() if path.exists() else None


def _settings_digest(core):
    settings = Settings.load(core.settings.root)
    return fingerprint({"settings": settings.hash, "app": settings.app})


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _backup_path(nonce, kind, which):
    return PRIVATE_DIR + "/" + nonce + "/" + which + (".env" if kind == "set_secret" else ".bytes")


def _load_pending(root):
    if not plain(root, PENDING_PATH).exists():
        return None
    try:
        pointer = parse_json(read_local(root, PENDING_PATH, 4096).decode("utf-8"))
        if (not isinstance(pointer, dict) or set(pointer) != {"contract", "nonce", "intent_sha256"}
                or pointer["contract"] != INTENT_CONTRACT or not re.fullmatch(r"[a-f0-9]{32}", pointer["nonce"])):
            raise ValueError
        base = PRIVATE_DIR + "/" + pointer["nonce"]
        raw = read_local(root, base + "/intent.json", 65536)
        if hashlib.sha256(raw).hexdigest() != pointer["intent_sha256"]:
            raise ValueError
        value = parse_json(raw.decode("utf-8"))
        fields = {"contract", "nonce", "root", "kind", "file", "target_id", "old_sha256", "new_sha256",
                  "old_db_sha256", "new_db_sha256", "settings_sha256", "audit_before_sha256", "audit", "request_sha256", "response"}
        if (not isinstance(value, dict) or set(value) != fields or value["contract"] != INTENT_CONTRACT
                or value["nonce"] != pointer["nonce"] or value["root"] != str(root.resolve())
                or value["kind"] not in {"configure_target", "set_secret"}
                or value["file"] != {"configure_target": "config/publishing.json", "set_secret": ".env.local"}[value["kind"]]
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value["target_id"])
                or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                       for key in ("new_sha256", "new_db_sha256", "settings_sha256", "audit_before_sha256", "request_sha256"))
                or any(value[key] is not None and (not isinstance(value[key], str)
                       or not re.fullmatch(r"[a-f0-9]{64}", value[key])) for key in ("old_sha256", "old_db_sha256"))
                or not isinstance(value["audit"], dict)
                or set(value["audit"]) != {"actor", "action", "entity", "details_sha256"}
                or value["audit"]["entity"] != value["target_id"]
                or value["audit"]["action"] != {"configure_target": "route_target_configured",
                                                    "set_secret": "route_credential_change_paused"}[value["kind"]]
                or not isinstance(value["audit"]["actor"], str) or not value["audit"]["actor"]
                or not isinstance(value["audit"]["details_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["audit"]["details_sha256"])
                or not isinstance(value["response"], dict) or value["response"].get("target_id") != value["target_id"]):
            raise ValueError
        for kind in ("old", "new"):
            backup = read_local(root, _backup_path(value["nonce"], value["kind"], kind))
            expected = value[kind + "_sha256"]
            if expected is None and backup or expected is not None and hashlib.sha256(backup).hexdigest() != expected:
                raise ValueError
        return value
    except (AgentError, OSError, TypeError, ValueError, UnicodeError, KeyError):
        raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409) from None


def _prepare_intent(core, session, value, target, old_db, filename, old_raw, new_raw, *, actor, action, details, response):
    root = core.settings.root
    if len(new_raw) > 1024 * 1024:
        raise AgentError("ROUTE_ADMIN_FILE_INVALID", 422)
    nonce = uuid.uuid4().hex
    base = PRIVATE_DIR + "/" + nonce
    plain(root, PRIVATE_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    plain(root, base).mkdir(mode=0o700)
    _sync_directory(plain(root, PRIVATE_DIR))
    rows = list(session.scalars(select(Audit).where(Audit.entity == target.id, Audit.action == action)))
    old_sha = hashlib.sha256(old_raw).hexdigest() if old_raw is not None else None
    new_sha, new_db = hashlib.sha256(new_raw).hexdigest(), _target_digest(target)
    settings_sha = _settings_digest(core)
    details = {**details, "operation_nonce": nonce, "old_file_sha256": old_sha, "new_file_sha256": new_sha,
               "old_target_sha256": old_db, "new_target_sha256": new_db, "settings_sha256": settings_sha}
    intent = {"contract": INTENT_CONTRACT, "nonce": nonce, "root": str(root.resolve()),
              "kind": value["action"], "file": filename, "target_id": target.id,
              "old_sha256": old_sha, "new_sha256": new_sha, "old_db_sha256": old_db,
              "new_db_sha256": new_db, "settings_sha256": settings_sha, "audit_before_sha256": _audit_digest(rows),
              "audit": {"actor": actor, "action": action, "entity": target.id, "details_sha256": fingerprint(details)},
              "request_sha256": fingerprint(value), "response": response}
    # Secret-bearing bytes exist only in private mode-0600 backups, never JSON.
    atomic_write(root, _backup_path(nonce, value["action"], "old"), old_raw if old_raw is not None else b"")
    atomic_write(root, _backup_path(nonce, value["action"], "new"), new_raw)
    raw = _json(intent)
    atomic_write(root, base + "/intent.json", raw)
    atomic_write(root, PENDING_PATH, _json({"contract": INTENT_CONTRACT, "nonce": nonce,
                                        "intent_sha256": hashlib.sha256(raw).hexdigest()}))
    core.db.audit(session, actor, action, target.id, **details)
    return intent


def _finish_intent(core, intent, resolution):
    root = core.settings.root
    current = _load_pending(root)
    if current != intent:
        raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
    atomic_write(root, PRIVATE_DIR + "/" + intent["nonce"] + "/outcome.json",
                 _json({"contract": INTENT_CONTRACT, "nonce": intent["nonce"], "resolution": resolution,
                        "intent_sha256": hashlib.sha256(_json(intent)).hexdigest()}))
    plain(root, PENDING_PATH).unlink()
    _sync_directory(plain(root, PRIVATE_DIR))


def reconcile_pending(core, *, actor):
    """Only explicit owner actions call this while holding route-admin.lock.

    No old Session/identity map is reused after a failed/uncertain commit. A
    SQLite writer barrier or PostgreSQL row locks wait for its final outcome.
    """
    from .server_maintenance import maintenance_guard
    root = core.settings.root
    intent = _load_pending(root)
    if intent is None:
        return None
    try:
        with core.db.session() as session, session.begin():
            if core.db.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("PRAGMA busy_timeout=2000")
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            elif core.db.engine.dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
            else:
                raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
            maintenance_guard(core, session)
            if intent["settings_sha256"] != _settings_digest(core):
                raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
            target = session.scalar(select(Target).where(Target.id == intent["target_id"]).with_for_update())
            if target is not None:
                _managed(target)
                _idle(session, target)
            elif session.scalar(select(Variant.id).where(Variant.target_id == intent["target_id"])):
                raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
            expected = intent["audit"]
            rows = list(session.scalars(select(Audit).where(Audit.entity == expected["entity"], Audit.action == expected["action"])))
            nonce_rows = [row for row in rows if isinstance(row.details, dict)
                          and row.details.get("operation_nonce") == intent["nonce"]]
            prior = [row for row in rows if row not in nonce_rows]
            if _audit_digest(prior) != intent["audit_before_sha256"]:
                raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
            committed = (len(nonce_rows) == 1 and nonce_rows[0].actor == expected["actor"]
                         and fingerprint(nonce_rows[0].details) == expected["details_sha256"]
                         and all(nonce_rows[0].details.get(key) == intent[field] for key, field in (
                             ("old_file_sha256", "old_sha256"), ("new_file_sha256", "new_sha256"),
                             ("old_target_sha256", "old_db_sha256"), ("new_target_sha256", "new_db_sha256"),
                             ("settings_sha256", "settings_sha256")))
                         and _target_digest(target) == intent["new_db_sha256"])
            aborted = not nonce_rows and _target_digest(target) == intent["old_db_sha256"]
            disk = _file_digest(root, intent["file"])
            if committed and disk == intent["new_sha256"]:
                resolution = "committed"
                response = intent["response"]
                if (response.get("paused") is not True or response.get("restart_required") is not True
                        or intent["kind"] == "configure_target" and (
                            response.get("binding_hash") != target.binding_hash
                            or response.get("config_hash") != _configuration(core)[2])
                        or intent["kind"] == "set_secret" and response.get("configured") is not True):
                    raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
            elif aborted and disk in {intent["old_sha256"], intent["new_sha256"]}:
                resolution = "aborted"
                if disk != intent["old_sha256"]:
                    # This exact candidate belongs to this intent. Never replace
                    # an externally edited file or infer an uncertain DB result.
                    old = read_local(root, _backup_path(intent["nonce"], intent["kind"], "old"))
                    if _file_digest(root, intent["file"]) != intent["new_sha256"]:
                        raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
                    if intent["old_sha256"] is None:
                        plain(root, intent["file"]).unlink()
                        _sync_directory(plain(root, intent["file"]).parent)
                    else:
                        atomic_write(root, intent["file"], old)
            else:
                raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
            proof = {"operation_nonce": intent["nonce"], "resolution": resolution,
                     "old_sha256": intent["old_sha256"], "new_sha256": intent["new_sha256"]}
            recoveries = list(session.scalars(select(Audit).where(Audit.entity == intent["nonce"],
                                               Audit.action == "route_admin_file_db_reconciled")))
            if not recoveries:
                core.db.audit(session, actor, "route_admin_file_db_reconciled", intent["nonce"], **proof)
            elif len(recoveries) != 1 or recoveries[0].details != proof:
                raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
        _finish_intent(core, intent, resolution)
        return {"resolution": resolution, "request_sha256": intent["request_sha256"], "response": intent["response"]}
    except (AgentError, SQLAlchemyError, OSError, TypeError, ValueError):
        raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409) from None


def _change(core, value, *, actor, apply):
    """A file and its DB audit are recoverable across either commit boundary."""
    root = core.settings.root
    with profile_lock(plain(root, "data/route-admin.lock")):
        recovered = reconcile_pending(core, actor=actor)
        if (recovered and recovered["resolution"] == "committed"
                and recovered["request_sha256"] == fingerprint(value)):
            return recovered["response"]
        try:
            with core.db.session.begin() as session:
                response, intent = apply(session)
            _finish_intent(core, intent, "committed")
            return response
        except Exception as error:
            # The previous context has closed/rolled back before the fresh read.
            recovered = reconcile_pending(core, actor=actor)
            if (recovered and recovered["resolution"] == "committed"
                    and recovered["request_sha256"] == fingerprint(value)):
                return recovered["response"]
            if isinstance(error, AgentError):
                raise error
            raise AgentError("ROUTE_ADMIN_WRITE_FAILED", 503) from None


def _configuration(core):
    root = core.settings.root
    # Paths are checked before Settings.load reads app/TOPIC/source files.
    for relative in ("config/app.json", core.settings.app["topic_path"], core.settings.app["sources_path"]):
        plain(root, relative)
    settings = Settings.load(root)
    publishing = parse_json(read_local(root, "config/publishing.json").decode("utf-8-sig"))
    validate_schema(root, "publishing.schema.json", publishing)
    if len({target["id"] for target in publishing["targets"]}) != len(publishing["targets"]):
        raise AgentError("DUPLICATE_TARGET_ID", 409)
    return settings, publishing, fingerprint({"settings": settings.hash, "app": settings.app,
                                             "publishing": publishing})


def _managed(target):
    if target.platform not in ROUTES or target.route != ROUTES[target.platform]:
        raise AgentError("ROUTE_ADMIN_TARGET_UNSUPPORTED", 409)


def _lock(session):
    # Same first lock as approve/publish_due; no send can be claimed during update.
    session.scalar(select(EditorialControl).where(EditorialControl.id == "main").with_for_update())


def _target(core, session, ident, binding):
    target = session.scalar(select(Target).where(Target.id == ident).with_for_update())
    if target is None:
        raise AgentError("TARGET_NOT_FOUND", 404)
    _managed(target)
    if target.binding_hash != binding:
        raise AgentError("TARGET_BINDING_CONFLICT", 409)
    return target


def _idle(session, target, *, require_paused=True):
    if require_paused and not target.paused:
        raise AgentError("TARGET_PAUSE_REQUIRED", 409)
    rows = list(session.scalars(select(Dispatch).join(Variant).where(
        Variant.target_id == target.id).with_for_update(of=Dispatch)))
    if any(row.state in ACTIVE or row.owner is not None or row.lease_until > now() for row in rows):
        raise AgentError("CANCEL_OR_RECONCILE_FIRST", 409)
    if session.scalar(select(BridgePost).where(BridgePost.target_id == target.id,
        (BridgePost.mutation_owner.is_not(None)) | (BridgePost.mutation_until > now()))):
        raise AgentError("BRIDGE_EDIT_IN_PROGRESS", 409)


def _binding(settings, value):
    return fingerprint({"target": value, "editorial_config": settings.hash})


def _configured_target(settings, publishing, target):
    matches = [item for item in publishing["targets"] if item["id"] == target.id]
    if (len(matches) != 1 or _binding(settings, matches[0]) != target.binding_hash
            or any(getattr(target, key) != matches[0].get(key, {} if key == "options" else None)
                   for key in ("profile_id", "platform", "route", "destination", "enabled", "options"))):
        raise AgentError("ROUTE_CONFIGURATION_CHANGED", 409)
    return matches[0]


def _selection_active(root):
    path = plain(root, LOGIN_PATH)
    if not path.exists():
        return False
    value = parse_json(read_local(root, LOGIN_PATH).decode("utf-8-sig"))
    if (not isinstance(value, dict) or any(type(value.get(key)) not in {int, float}
            or not math.isfinite(value[key]) or not 0 < value[key] < 1e12 for key in ("issued_at", "expires_at"))
            or not value["issued_at"] < value["expires_at"] <= value["issued_at"] + 600):
        raise AgentError("LOGIN_SELECTION_INVALID", 409)
    return value["expires_at"] > now()


def _validate_destination(settings, platform, destination, options):
    if platform == "telegram":
        from .telegram_publisher import TelegramPublisher
        TelegramPublisher._destination(destination)
    elif platform == "vk":
        from .vk_browser import owner_id, profile_path, wall_url
        owner = owner_id(options)
        profile_path(settings.root, options)
        wall_url(destination, options)
        if destination != owner:
            raise AgentError("VK_BROWSER_PERSONAL_WALL_REQUIRED", 409)
    else:
        from .browser_runtime import channel_name
        channel_name(settings, destination)


def setup(core):
    settings, publishing, digest = _configuration(core)
    pending = plain(core.settings.root, PENDING_PATH).exists()
    targets = []
    with core.db.session() as session:
        for target in session.scalars(select(Target).where(Target.platform.in_(ROUTES)).order_by(Target.id)):
            if target.route != ROUTES[target.platform]:
                continue
            options = {key: value for key, value in target.options.items() if key in EDITABLE[target.platform]}
            diagnostics = {"local_prerequisites": False, "blocked_reasons": []}
            try:
                _configured_target(settings, publishing, target)
                from .route_readiness import _target as local_target
                row = {}
                local_target(settings.root, settings, next(v for v in publishing["targets"]
                             if v["id"] == target.id), row)
                diagnostics.update(local_prerequisites=not row["blockers"], blocked_reasons=row["blockers"])
                for key in ("credentials_present", "profile_exists"):
                    if key in row:
                        diagnostics[{"credentials_present": "credentials_configured"}.get(key, key)] = row[key]
            except AgentError as error:
                diagnostics["blocked_reasons"].append(error.code)
            capabilities = {"can_configure": True, "can_set_secret": target.platform == "telegram",
                            "can_create_original": target.enabled, "can_upload_media": True,
                            "can_login": target.platform in {"max", "vk"}}
            if pending:
                diagnostics["local_prerequisites"] = False
                diagnostics["blocked_reasons"].append("ROUTE_ADMIN_RECOVERY_REQUIRED")
            targets.append({"id": target.id, "profile_id": target.profile_id, "platform": target.platform,
                "route": target.route, "destination": target.destination, "enabled": target.enabled,
                "paused": target.paused, "binding_hash": target.binding_hash, "options": options,
                "capabilities": capabilities, "diagnostics": diagnostics})
    from .hub import route_runtime_generation
    from .server_maintenance import maintenance_guard, offline_maintenance_fixture
    offline = offline_maintenance_fixture(core)
    restart = {"required": pending or core.route_runtime_generation() != route_runtime_generation(settings.root),
               "can_request": True, "blocked_reasons": ["ROUTE_ADMIN_RECOVERY_REQUIRED"] if pending else []}
    if offline:
        restart["blocked_reasons"].append("POSTIZ_OFFLINE_FIXTURE_QUEUE_NOT_CHECKED")
    try:
        with core.db.session() as session:
            maintenance_guard(core, session)
        if _selection_active(settings.root):
            raise AgentError("LOGIN_SELECTION_ACTIVE", 409)
    except AgentError as error:
        restart.update(can_request=False, blocked_reasons=[error.code])
    return {"config_hash": digest, "targets": targets, "profiles": [
        {"id": row["id"], "name": row["name"], "enabled": row["enabled"],
         "publication_mode": row["publication"]["mode"]} for row in settings.topic["channels"]],
        "external_mutations": 0, "restart": restart,
        "max_channels": [{"id": ident, "name": name} for ident, name in
                         settings.app["max"].get("browser", {}).get("channels", {}).items()]}


def configure_target(core, value, *, actor):
    from .server_maintenance import guard_restart_operation, maintenance_guard, remote_queue_guard
    root = core.settings.root
    remote_queue_guard(core, proposed=value)

    def apply(session):
        guard_restart_operation(core)
        settings, publishing, digest = _configuration(core)
        if digest != value["expected_config_hash"]:
            raise AgentError("ROUTE_CONFIG_CONFLICT", 409)
        if _selection_active(root):
            raise AgentError("LOGIN_SELECTION_ACTIVE", 409)
        supplied = value["target"]
        platform = supplied["platform"]
        if ROUTES.get(platform) != supplied["route"] or set(supplied["options"]) - EDITABLE[platform]:
            raise AgentError("ROUTE_ADMIN_OPTIONS_INVALID", 422)
        settings.channel(supplied["profile_id"])
        _lock(session)
        target = session.scalar(select(Target).where(Target.id == value["id"]).with_for_update())
        old_db = _target_digest(target)
        configured = [row for row in publishing["targets"] if row["id"] == value["id"]]
        if target is None:
            if value["expected_binding_hash"] is not None or configured:
                raise AgentError("TARGET_BINDING_CONFLICT", 409)
            options = ({"adapter_mode": "live", "token_env": "EDITORIAL_TELEGRAM_" +
                        hashlib.sha256(value["id"].encode()).hexdigest()[:16].upper() + "_TOKEN"}
                       if platform == "telegram" else {})
            if platform == "vk":
                options = {"profile_dir": "data/vk-browser-" + value["id"],
                           "dom_contract_verified": False, "media_contract_verified": False,
                           "dom_contract_profile": "personal-text-v1"}
            target = Target(id=value["id"], platform=platform, route=ROUTES[platform], paused=True)
            session.add(target)
        else:
            _managed(target)
            if target.platform != platform or target.binding_hash != value["expected_binding_hash"]:
                raise AgentError("TARGET_BINDING_CONFLICT", 409)
            _configured_target(settings, publishing, target)
            _idle(session, target)
            options = copy.deepcopy(target.options)
        with session.no_autoflush:
            maintenance_guard(core, session)
        options.update(supplied["options"])
        _validate_destination(settings, platform, supplied["destination"], options)
        changed = {"id": value["id"], **supplied, "options": options}
        candidate = copy.deepcopy(publishing)
        candidate["targets"] = [changed if row["id"] == target.id else row for row in candidate["targets"]]
        if not configured:
            candidate["targets"].append(changed)
        validate_schema(root, "publishing.schema.json", candidate)
        if _configuration(core)[2] != digest:
            raise AgentError("ROUTE_CONFIG_CONFLICT", 409)
        old_raw, new_raw = read_local(root, "config/publishing.json"), _json(candidate)
        for key in ("profile_id", "platform", "route", "destination", "enabled", "options"):
            setattr(target, key, changed[key])
        target.binding_hash, target.paused = _binding(settings, changed), True
        response = {"target_id": target.id, "binding_hash": target.binding_hash, "paused": True,
                    "config_hash": fingerprint({"settings": settings.hash, "app": settings.app,
                                                 "publishing": candidate}), "restart_required": True}
        intent = _prepare_intent(core, session, value, target, old_db, "config/publishing.json", old_raw, new_raw,
            actor=actor, action="route_target_configured", details={"previous_config_hash": digest,
            "binding_hash": target.binding_hash, "paused": True, "restart_required": True}, response=response)
        if _file_digest(root, intent["file"]) != intent["old_sha256"]:
            raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
        atomic_write(root, intent["file"], new_raw)
        return response, intent

    return _change(core, value, actor=actor, apply=apply)


def set_secret(core, value, *, actor):
    from .server_maintenance import guard_restart_operation, maintenance_guard, remote_queue_guard
    root = core.settings.root
    secret = value["value"]
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,256}", secret):
        raise AgentError("CREDENTIAL_FORMAT_INVALID", 422)
    remote_queue_guard(core)

    def apply(session):
        guard_restart_operation(core)
        settings, publishing, _ = _configuration(core)
        if _selection_active(root):
            raise AgentError("LOGIN_SELECTION_ACTIVE", 409)
        _lock(session)
        target = _target(core, session, value["id"], value["expected_binding_hash"])
        old_db = _target_digest(target)
        if target.platform != "telegram":
            raise AgentError("CREDENTIAL_TARGET_INVALID", 409)
        configured = _configured_target(settings, publishing, target)
        _idle(session, target)
        maintenance_guard(core, session)
        default = "EDITORIAL_TELEGRAM_" + hashlib.sha256(target.id.encode()).hexdigest()[:16].upper() + "_TOKEN"
        name = target.options.get("token_env", "TELEGRAM_BOT_TOKEN")
        if name not in {default, "TELEGRAM_BOT_TOKEN"} or any(
            row["id"] != target.id and row["route"] == "telegram_api"
            and row.get("options", {}).get("token_env", "TELEGRAM_BOT_TOKEN") == name
            for row in publishing["targets"]):
            raise AgentError("CREDENTIAL_ENV_NOT_DEDICATED", 409)
        old_raw = read_local(root, ".env.local") if plain(root, ".env.local").exists() else None
        content = old_raw.decode("utf-8-sig") if old_raw is not None else ""
        lines = [line for line in content.splitlines() if line.split("=", 1)[0].strip() not in {name, "export " + name}]
        if configured != _configured_target(*_configuration(core)[:2], target):
            raise AgentError("ROUTE_CONFIGURATION_CHANGED", 409)
        new_raw = ("\n".join(lines + [name + "=" + secret]) + "\n").encode()
        response = {"target_id": target.id, "configured": True, "restart_required": True, "paused": True}
        intent = _prepare_intent(core, session, value, target, old_db, ".env.local", old_raw, new_raw,
            actor=actor, action="route_credential_change_paused", details={"value_recorded": False,
            "paused": True, "restart_required": True}, response=response)
        if _file_digest(root, intent["file"]) != intent["old_sha256"]:
            raise AgentError("ROUTE_ADMIN_RECOVERY_REQUIRED", 409)
        atomic_write(root, intent["file"], new_raw)
        return response, intent

    return _change(core, value, actor=actor, apply=apply)


def upload_media(core, value, *, actor):
    try:
        raw = base64.b64decode(value["data_base64"], validate=True)
    except (ValueError, TypeError):
        raise AgentError("MEDIA_CONTENT_INVALID", 422) from None
    if not raw or len(raw) > MEDIA_LIMIT or hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise AgentError("MEDIA_HASH_CHANGED", 409)
    try:
        with Image.open(io.BytesIO(raw)) as image:
            expected = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(image.format)
            if (expected != value["mime"] or image.width * image.height > 40_000_000
                    or image.width < 1 or image.height < 1 or getattr(image, "n_frames", 1) != 1):
                raise AgentError("MEDIA_TYPE_MISMATCH", 422)
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise AgentError("MEDIA_CONTENT_INVALID", 422) from None
    if core.settings.app["media_dir"] != "data/media":
        raise AgentError("MEDIA_STORAGE_UNSUPPORTED", 409)
    media = {"path": "data/media/" + value["sha256"] + (".png" if value["mime"] == "image/png" else ".jpg"),
             "mime": value["mime"], "sha256": value["sha256"]}
    with profile_lock(plain(core.settings.root, "data/route-admin.lock")), core.db.session.begin() as session:
        target = _target(core, session, value["id"], value["expected_binding_hash"])
        _configured_target(*_configuration(core)[:2], target)
        path = plain(core.settings.root, media["path"])
        if path.exists():
            if read_local(core.settings.root, media["path"], MEDIA_LIMIT) != raw:
                raise AgentError("MEDIA_HASH_CHANGED", 409)
        else:
            atomic_write(core.settings.root, media["path"], raw)
        core.db.audit(session, actor, "owner_media_uploaded", target.id,
                      sha256=media["sha256"], mime=media["mime"], external_mutations=0)
    return {"media": media, "external_mutations": 0}


def create_original(core, value, *, actor):
    from .owner_original import _create_original
    with profile_lock(plain(core.settings.root, "data/route-admin.lock")):
        with core.db.session() as session:
            target = _target(core, session, value["id"], value["expected_binding_hash"])
            _configured_target(*_configuration(core)[:2], target)
        if Settings.load(core.settings.root).hash != core.settings.hash:
            raise AgentError("ROUTE_RUNTIME_RESTART_REQUIRED", 409)
        result = _create_original(core, value["id"], value["original"],
            attest_original=value["attest_original"], expected_binding=value["expected_binding_hash"], owner_actor=actor)
        return {key: result[key] for key in ("variant_id", "target_id", "revision", "content_hash", "state",
                                            "approval_created", "external_mutations")}


def revise_original(core, value, *, actor):
    if value["attest_original"] is not True:
        raise AgentError("OWNER_ORIGINAL_ATTESTATION_REQUIRED", 403)
    supplied = _input(value["original"])
    with profile_lock(plain(core.settings.root, "data/route-admin.lock")), core.db.session.begin() as session:
        _lock(session)
        variant = core._variant(session, value["id"], True)
        target = _target(core, session, variant.target_id, value["expected_binding_hash"])
        _configured_target(*_configuration(core)[:2], target)
        if variant.revision != value["revision"] or fingerprint(core.snapshot(variant, target)) != value["content_hash"]:
            raise AgentError("REVISION_CONFLICT", 409)
        if variant.metadata_json.get("origin") != ORIGIN:
            raise AgentError("OWNER_ORIGINAL_REQUIRED", 409)
        # A pending approval is revoked by _invalidate; an externally used version
        # must be preserved. The frozen payload of a cancelled dispatch is retained.
        previous = list(session.scalars(select(Dispatch).where(Dispatch.variant_id == variant.id).with_for_update()))
        if any(row.state not in {"pending", "retry_wait", "cancelled"} or row.owner is not None
               or row.lease_until > now() or row.attempts != 0 or row.external_id for row in previous):
            raise AgentError("ORIGINAL_DISPATCH_HISTORY_IMMUTABLE", 409)
        if session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id)):
            raise AgentError("ARTICLE_EDIT_IN_POSTIZ_REQUIRED", 409)
        if core.settings.hash != Settings.load(core.settings.root).hash:
            raise AgentError("ROUTE_RUNTIME_RESTART_REQUIRED", 409)
        core.check_media(supplied["media"], target)
        draft = session.get(Draft, variant.draft_id)
        if draft is None or draft.revision != variant.source_revision:
            raise AgentError("SOURCE_REVISION_STALE", 409)
        digest = attestation_hash(supplied["title"], supplied["body"], supplied["media"])
        story_hash = _story_hash(target.id, digest)
        if session.scalar(select(Story.id).where(Story.text_hash == story_hash)):
            raise AgentError("OWNER_ORIGINAL_ALREADY_EXISTS", 409)
        story = Story(text_hash=story_hash, title=supplied["title"], text=supplied["body"])
        session.add(story)
        session.flush()
        core._invalidate(session, variant)
        variant.revision += 1
        draft.revision += 1
        draft.story_id, draft.config_id, draft.state = story.id, core.settings.hash, "review_pending"
        metadata, media = supplied["metadata"], supplied["media"]
        draft.current = {"title": supplied["title"], "body": supplied["body"], "language": "ru", "source_ids": [],
            "decision": "review", "reason_codes": ["OWNER_ORIGINAL_ATTESTED"], "claims": [], "confidence": 0.0,
            "payload": {"origin": ORIGIN, "contract": CONTRACT, "target_id": target.id,
                        "attestation_hash": digest, "media": media, "metadata": metadata}}
        session.add(DraftRevision(draft_id=draft.id, revision=draft.revision,
                                 payload=draft.current, config_id=core.settings.hash))
        variant.title, variant.body, variant.media = supplied["title"], supplied["body"], media
        variant.metadata_json = {**metadata, "origin": ORIGIN, "owner_original_contract": CONTRACT,
                                 "owner_original_attested_hash": digest}
        variant.source_revision, variant.config_hash = draft.revision, core.settings.hash
        variant.binding_hash, variant.state, variant.updated_at = target.binding_hash, "review_pending", now()
        core._history(session, variant, target, ACTOR)
        core.db.audit(session, ACTOR, "owner_original_attested", variant.id, contract=CONTRACT,
                      attestation_hash=digest, target_id=target.id, draft_id=draft.id, revision=variant.revision,
                      metadata_hash=fingerprint(metadata), original_rights_attested=True,
                      approval_granted=False, model_verified=False)
        content_hash = fingerprint(core.snapshot(variant, target))
        core.db.audit(session, actor, "server_owner_original_revised", variant.id,
                      revision=variant.revision, content_hash=content_hash)
        return {"variant_id": variant.id, "target_id": target.id, "revision": variant.revision,
                "content_hash": content_hash, "state": variant.state, "approval_created": False, "external_mutations": 0}


def _login_identity(core, settings, target):
    root = settings.root
    if target.platform == "max":
        from .browser_runtime import channel_name
        if settings.app["max"]["adapter_mode"] != "browser":
            raise AgentError("ADAPTER_NOT_BROWSER", 409)
        channel_name(settings, target.destination)
        profile = settings.app["max"].get("browser", {}).get("profile_dir", "data/max-browser")
        url = "https://web.max.ru/"
    elif target.platform == "vk":
        from .vk_browser import owner_id, profile_path
        profile_path(root, target.options)
        profile = target.options.get("profile_dir", "data/vk-browser")
        url = "https://vk.ru/id" + owner_id(target.options)
    else:
        raise AgentError("LOGIN_TARGET_UNSUPPORTED", 409)
    path = plain(root, profile)
    if path.parent != root / "data" or not re.fullmatch(r"(?:max|vk)-browser(?:-[A-Za-z0-9_-]+)?", path.name):
        raise AgentError("BROWSER_PROFILE_INVALID", 409)
    return profile, url


def _login_peers_idle(core, session, settings, target, profile):
    # MAX destinations share a browser; existing VK profiles can also be shared.
    # A login must not compete with another target using the same profile.
    for peer in session.scalars(select(Target).where(Target.platform == target.platform,
            Target.route == target.route).order_by(Target.id)):
        if _login_identity(core, settings, peer)[0] == profile:
            _idle(session, peer)


def guard_login_resume(core, target):
    """A live manual session cannot race a dispatcher resuming the same profile."""
    root = core.settings.root
    if not _selection_active(root):
        return
    manifest = parse_json(read_local(root, LOGIN_PATH).decode("utf-8-sig"))
    settings = _configuration(core)[0]
    if _login_identity(core, settings, target)[0] == manifest.get("profile_dir"):
        raise AgentError("LOGIN_SELECTION_ACTIVE", 409)


def validate_login_selection(core, value):
    """Входной gate системного login worker; не запускает браузер, не меняет БД."""
    required = {"contract", "selection_id", "root", "issued_at", "expires_at", "target_id", "binding_hash",
                "platform", "profile_dir", "url", "config_hash"}
    if (not isinstance(value, dict) or set(value) != required or value.get("contract") != "owner-browser-selection-v1"
            or not re.fullmatch(r"[a-f0-9]{32}", str(value.get("selection_id", "")))
            or value.get("root") != str(core.settings.root.resolve())
            or any(type(value.get(key)) not in {int, float} for key in ("issued_at", "expires_at"))
            or not value["issued_at"] <= now() < value["expires_at"] <= value["issued_at"] + 600):
        raise AgentError("LOGIN_SELECTION_INVALID", 409)
    settings, publishing, digest = _configuration(core)
    if value["config_hash"] != digest:
        raise AgentError("LOGIN_SELECTION_STALE", 409)
    with core.db.session() as session:
        target = _target(core, session, value["target_id"], value["binding_hash"])
        _configured_target(settings, publishing, target)
        _idle(session, target)
        profile, url = _login_identity(core, settings, target)
        _login_peers_idle(core, session, settings, target, profile)
        if (value["profile_dir"] != profile or value["url"] != url or value["platform"] != target.platform):
            raise AgentError("LOGIN_SELECTION_INVALID", 409)
    return value


def prepare_login(core, value, *, actor):
    from .server_maintenance import guard_restart_operation
    root = core.settings.root
    with profile_lock(plain(root, "data/route-admin.lock")), core.db.session.begin() as session:
        guard_restart_operation(core)
        settings, publishing, digest = _configuration(core)
        _lock(session)
        target = _target(core, session, value["id"], value["expected_binding_hash"])
        _configured_target(settings, publishing, target)
        _idle(session, target)
        if _selection_active(root):
            raise AgentError("LOGIN_SELECTION_ACTIVE", 409)
        profile, url = _login_identity(core, settings, target)
        _login_peers_idle(core, session, settings, target, profile)
        stamp = int(now())
        manifest = {"contract": "owner-browser-selection-v1", "selection_id": uuid.uuid4().hex,
                    "root": str(root.resolve()), "issued_at": stamp, "expires_at": stamp + 600,
                    "target_id": target.id, "binding_hash": target.binding_hash, "platform": target.platform,
                    "profile_dir": profile, "url": url, "config_hash": digest}
        core.db.audit(session, actor, "owner_browser_login_requested", target.id,
                      selection_id=manifest["selection_id"], expires_at=manifest["expires_at"], publication_allowed=False)
        atomic_write(root, LOGIN_PATH, _json(manifest))
        return {"target_id": target.id, "login_url": "/owner-browser/", "expires_at": manifest["expires_at"],
                "live_verified": False}


def execute(core, value, *, actor):
    from .server_maintenance import request_restart
    handlers = {"configure_target": configure_target, "set_secret": set_secret, "upload_media": upload_media,
                "create_original": create_original, "revise_original": revise_original, "prepare_login": prepare_login,
                "request_restart": request_restart}
    try:
        return handlers[value["action"]](core, value, actor=actor)
    except IntegrityError:
        raise AgentError("ROUTE_ADMIN_CONFLICT", 409) from None
