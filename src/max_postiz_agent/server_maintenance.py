"""Перезапуск фиксированного сервиса: пауза и отсутствие незавершённых действий."""

from datetime import UTC, datetime
import re
import uuid

from sqlalchemy import or_, select

from .config import AgentError
from .db import Intent, Job, now
from .hub_models import BridgePost, Dispatch, RecurringTask, RemoteDraft, Target
from .server_models import EditorialControl, EditorialLedger, EditorialPreparationClaim


RESTART_PATH = "data/editorial-restart-request.json"


def guard_restart_operation(core):
    from .config import parse_json
    from .server_route_admin import guard_pending, plain, read_local
    guard_pending(core)
    if not plain(core.settings.root, RESTART_PATH).exists():
        return
    value = parse_json(read_local(core.settings.root, RESTART_PATH).decode("utf-8-sig"))
    if not isinstance(value, dict) or type(value.get("expires_at")) is not int:
        raise AgentError("RESTART_SELECTION_INVALID", 409)
    if value["expires_at"] > now():
        raise AgentError("RESTART_ALREADY_REQUESTED", 409)


def maintenance_guard(core, session):
    """Не освобождает claims, не отменяет неизвестные исходы, не запускает sync."""
    control = session.scalar(select(EditorialControl).where(EditorialControl.id == "main").with_for_update())
    if control is None or not control.paused:
        raise AgentError("EDITORIAL_PAUSE_BEFORE_SETTINGS", 409)
    if session.scalar(select(Target.id).where(Target.platform.in_({"max", "telegram", "vk"}), Target.paused.is_(False))):
        raise AgentError("TARGET_PAUSE_REQUIRED", 409)
    stamp = now()
    checks = [
        select(Dispatch.id).where(or_(Dispatch.state == "sending", Dispatch.owner.is_not(None), Dispatch.lease_until > stamp)),
        select(BridgePost.post_id).where(or_(BridgePost.mutation_owner.is_not(None), BridgePost.mutation_until > stamp)),
        select(Job.id).where(or_(Job.state == "leased", Job.owner.is_not(None), Job.lease_until > stamp)),
        select(RecurringTask.id).where(or_(RecurringTask.owner.is_not(None), RecurringTask.lease_until > stamp)),
        select(EditorialPreparationClaim.id).where(EditorialPreparationClaim.active_key.is_not(None)),
        select(RemoteDraft.variant_id).where(RemoteDraft.state.in_({"creating", "uploading", "scheduling"})),
        select(Intent.id).where(Intent.state == "sending"),
    ]
    if any(session.scalar(query.limit(1)) is not None for query in checks):
        raise AgentError("EDITORIAL_MAINTENANCE_BUSY", 409)
    ledger = session.get(EditorialLedger, "hourly")
    active = {"preparing", "staging_human_draft", "staging_automatic_draft", "recovering_rejected",
              "recovering_review_protocol", "correcting", "checking"}
    if ledger is not None:
        records = ledger.payload.get("records") if isinstance(ledger.payload, dict) else None
        if not isinstance(records, list) or any(not isinstance(record, dict) or record.get("state") in active for record in records):
            raise AgentError("EDITORIAL_MAINTENANCE_BUSY", 409)
    preparation = control.runtime.get("preparation", {}) if isinstance(control.runtime, dict) else None
    if not isinstance(preparation, dict) or preparation.get("phase") in {"selecting", "preparing", "translating", "checking", "reviewing", "repairing"}:
        raise AgentError("EDITORIAL_MAINTENANCE_BUSY", 409)
    return control


def offline_maintenance_fixture(core, proposed=None):
    """Только свежий явно объявленный стенд; это не проверка очереди Postiz."""
    from .server_route_admin import _configuration
    settings, publishing, _ = _configuration(core)
    if (publishing["postiz"].get("maintenance_offline_fixture") is not True
            or core.postiz.base or core.postiz.key or settings.app["max"]["adapter_mode"] != "fake"):
        return False
    targets = publishing["targets"]
    if proposed is not None:
        current = next((target for target in targets if target["id"] == proposed["id"]), None)
        supplied = proposed["target"]
        options = dict(current.get("options", {}) if current else
                       {"adapter_mode": "live"} if supplied["route"] == "telegram_api" else {})
        options.update(supplied["options"])
        candidate = {"id": proposed["id"], **supplied, "options": options}
        targets = [target for target in targets if target["id"] != proposed["id"]] + [candidate]
    for target in targets:
        route, options = target["route"], target.get("options", {})
        if route in {"manual", "disabled", "max_browser", "max_api"}:
            continue
        if route in {"telegram_api", "vk_api"} and options.get("adapter_mode", "fake") == "fake":
            continue
        if route == "vk_browser" and options.get("dom_contract_verified") is not True:
            continue
        return False
    return True


def remote_queue_guard(core, *, proposed=None):
    """Read-only Postiz GET before the final local fence, never bridge sync/POST."""
    if not core.postiz.base and not core.postiz.key:
        if offline_maintenance_fixture(core, proposed):
            return False
        raise AgentError("POSTIZ_NOT_CONFIGURED", 503)
    stamp = now()
    rows = core.postiz.posts(datetime.fromtimestamp(stamp - 86400, UTC),
                             datetime.fromtimestamp(stamp + 300, UTC))
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("state"), str):
            raise AgentError("POSTIZ_RESPONSE_INVALID", 409)
        if row["state"] == "QUEUE":
            try:
                date = datetime.fromisoformat(row["publishDate"].replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError
                due = date.timestamp()
            except (ValueError, TypeError, KeyError):
                raise AgentError("POSTIZ_RESPONSE_INVALID", 409) from None
            if due <= now() + 300:
                raise AgentError("POSTIZ_NEAR_QUEUE_RESTART_BLOCKED", 409)
    return True


def validate_restart_selection(core, value):
    from .hub import route_runtime_generation
    from .server_route_admin import _configuration, guard_pending
    guard_pending(core)
    fields = {"contract", "request_id", "root", "issued_at", "expires_at", "runtime_generation", "previous_worker_id"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("contract") != "editorial-restart-request-v1"
            or not isinstance(value.get("request_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", value["request_id"])
            or value.get("root") != str(core.settings.root.resolve())
            or any(type(value.get(key)) is not int for key in ("issued_at", "expires_at"))
            or not value["issued_at"] <= now() < value["expires_at"] <= value["issued_at"] + 60
            or value.get("runtime_generation") != route_runtime_generation(core.settings.root)):
        raise AgentError("RESTART_SELECTION_INVALID", 409)
    try:
        if str(uuid.UUID(value["previous_worker_id"])) != value["previous_worker_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AgentError("RESTART_SELECTION_INVALID", 409) from None
    _configuration(core)
    remote_queue_guard(core)
    with core.db.session() as session:
        maintenance_guard(core, session)
    if value["runtime_generation"] != route_runtime_generation(core.settings.root) or now() >= value["expires_at"]:
        raise AgentError("RESTART_SELECTION_INVALID", 409)
    return value


def request_restart(core, value, *, actor):
    from .hub import route_runtime_generation
    from .server_route_admin import (_configuration, _json, _selection_active, atomic_write, plain,
                                     read_local, reconcile_pending)
    from .browser_runtime import profile_lock
    from .config import parse_json
    root = core.settings.root
    if value["id"] != "server" or value["expected_binding_hash"] is not None:
        raise AgentError("INPUT_INVALID", 422)
    remote_queue_guard(core)
    with profile_lock(plain(root, "data/route-admin.lock")), core.db.session.begin() as session:
        # The outer Session has not queried anything yet; recovery uses its own
        # fresh serialized connection before this request takes any DB snapshot.
        reconcile_pending(core, actor=actor)
        maintenance_guard(core, session)
        if _configuration(core)[2] != value["expected_config_hash"]:
            raise AgentError("ROUTE_CONFIG_CONFLICT", 409)
        if _selection_active(root):
            raise AgentError("LOGIN_SELECTION_ACTIVE", 409)
        if plain(root, RESTART_PATH).exists():
            old = parse_json(read_local(root, RESTART_PATH).decode("utf-8-sig"))
            if not isinstance(old, dict) or type(old.get("expires_at")) is not int or old["expires_at"] > now():
                raise AgentError("RESTART_ALREADY_REQUESTED", 409)
        stamp = int(now())
        manifest = {"contract": "editorial-restart-request-v1", "request_id": uuid.uuid4().hex,
                    "root": str(root.resolve()), "issued_at": stamp, "expires_at": stamp + 60,
                    "runtime_generation": route_runtime_generation(root), "previous_worker_id": core.worker_id}
        core.db.audit(session, actor, "editorial_restart_requested", manifest["request_id"],
                      paused=True, claims_cleared=False, previous_worker_id=core.worker_id)
        atomic_write(root, RESTART_PATH, _json(manifest))
    return {"request_id": manifest["request_id"], "state": "restart_requested", "restart_required": True}
