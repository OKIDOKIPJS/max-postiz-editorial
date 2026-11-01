"""Внутренний API для Postiz и MAX-провайдера; отдельной панели здесь нет."""
import hmac
import json
import os

from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from . import __version__
from .article_cycle import HourlyArticleCycle
from .config import AgentError
from .hub_api import BridgeConnectInput, BridgeExchangeInput, BridgeMutationInput, BridgePostInput
from .hub_models import Dispatch, Variant
from .server_state import ServerState


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    settings: dict


class ControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    revision: StrictInt = Field(ge=1)


def reddit_capability(core):
    path = core.settings.root / "config/reddit_automation.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        options = policy.get("reddit", {})
        approved = (options.get("access_approved") is True
                    and options.get("translation_and_redistribution_approved") is True)
        configured = all(os.getenv("REDDIT_" + key) for key in (
            "CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "USER_AGENT"))
        return {"available": approved and configured,
                "reason": None if approved and configured else
                    "Ожидаются одобрение Reddit, права на переводы и OAuth-настройки"}
    except (OSError, ValueError, TypeError):
        return {"available": False, "reason": "Настройки доступа Reddit требуют проверки"}


def public_state(core, store):
    from .article_reserve import ArticleReserveStore
    from .server_state import validate_settings
    value = store.read()
    runtime = value["runtime"]
    cycle = HourlyArticleCycle(core)
    records = cycle._load()["records"]
    with core.db.session() as session:
        recent_ids = [record["variant_id"] for record in records[-30:] if record.get("variant_id")]
        variants = {v.id: v for v in session.scalars(select(Variant).where(Variant.id.in_(recent_ids)))}
        recent = []
        for record in reversed(records[-30:]):
            variant = variants.get(record.get("variant_id"))
            recent.append({"id": record["id"], "title": variant.title if variant else record.get("url", "Статья"),
                "source": record.get("source_id"), "state": record["state"],
                "updated_at": record.get("finished_at", record.get("claimed_at")),
                "url": record.get("url"), "postiz_id": record.get("postiz_id"), "error": record.get("error")})
        active = list(session.scalars(select(Dispatch).where(
            Dispatch.state.in_({"pending", "sending", "unknown", "scheduled", "retry_wait"}))))
    reserve = ArticleReserveStore(core).status()
    options = validate_settings(value["settings"])["reserve"]
    reserve.update({**options, "ready": sum(r["state"] == "quality_ready" for r in records)})
    preparation = runtime.get("preparation", {})
    return {"version": __version__, "revision": value["revision"], "paused": value["paused"],
        "phase": "paused" if value["paused"] else runtime.get("phase", "idle"),
        "updated_at": value["updated_at"], "heartbeat_at": runtime.get("heartbeat_at"),
        "next_run_at": runtime.get("next_run_at"), "last_error": runtime.get("last_error"),
        "recent": recent, "active_dispatches": len(active),
        "reserve": reserve,
        "preparation": {k: preparation.get(k) for k in ("phase", "heartbeat_at", "last_error", "next_run_at")},
        "capabilities": {"reddit": reddit_capability(core)}}


def editorial_app(core, store=None):
    store = store or ServerState(core)
    app = FastAPI(title="Автономная редакция Postiz", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(AgentError)
    async def known_error(request, error):
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Не возвращаем присланные значения: они могут содержать приватные данные.
        return JSONResponse({"code": "INPUT_INVALID"}, status_code=422)

    def owner(x_editorial_key: str | None = Header(default=None),
              x_editorial_owner: str | None = Header(default=None),
              x_editorial_organization: str | None = Header(default=None)):
        expected = (os.getenv("EDITORIAL_INTERNAL_KEY", ""), os.getenv("EDITORIAL_OWNER_USER_ID", ""),
                    os.getenv("EDITORIAL_OWNER_ORGANIZATION_ID", ""))
        received = (x_editorial_key, x_editorial_owner, x_editorial_organization)
        if (not all(expected) or len(expected[0]) < 32 or any(
                not isinstance(value, str) or not hmac.compare_digest(value, wanted)
                for value, wanted in zip(received, expected, strict=True))):
            raise AgentError("EDITORIAL_ACCESS_DENIED", 403)
        return "postiz:" + expected[1]

    from .routes_api import attach_route_api
    attach_route_api(app, core, owner)
    from .monitor_api import attach_monitor_api
    attach_monitor_api(app, core, owner)

    @app.get("/health")
    def health():
        return {"status": "ready", "version": __version__, "interface": "postiz_only",
                "worker_mode": getattr(app.state, "worker_mode", "api_only")}

    @app.get("/internal/editorial/state", dependencies=[Depends(owner)])
    def state():
        return public_state(core, store)

    @app.get("/internal/editorial/settings", dependencies=[Depends(owner)])
    def settings():
        from .server_state import validate_settings
        value = store.read()
        return {"revision": value["revision"], "settings": validate_settings(value["settings"])}

    owner_dependency = Depends(owner)

    @app.put("/internal/editorial/settings")
    def save_settings(value: SettingsInput, actor=owner_dependency):
        if value.settings.get("source") == "reddit" and not reddit_capability(core)["available"]:
            raise AgentError("REDDIT_APPROVAL_REQUIRED", 409)
        result = store.settings(value.settings, value.revision, actor)
        return {"revision": result["revision"], "settings": result["settings"]}

    @app.post("/internal/editorial/control")
    def control(value: ControlInput, actor=owner_dependency):
        if value.action == "start":
            policy = HourlyArticleCycle(core)._policy()
            if policy.get("approval_mode") != "automatic":
                raise AgentError("EDITORIAL_AUTOMATIC_POLICY_REQUIRED", 409)
            if store.read()["settings"]["source"] == "reddit" and not reddit_capability(core)["available"]:
                raise AgentError("REDDIT_APPROVAL_REQUIRED", 409)
        store.control(value.action, value.revision, actor)
        return public_state(core, store)

    def bridge_auth(x_max_bridge_key: str | None = Header(default=None)):
        core.bridge.authorize_request(x_max_bridge_key)

    @app.post("/api/max-bridge/connect", dependencies=[Depends(bridge_auth)])
    def bridge_connect(value: BridgeConnectInput):
        return core.bridge.connect(value.target_id, value.state, value.verifier)

    @app.post("/api/max-bridge/exchange", dependencies=[Depends(bridge_auth)])
    def bridge_exchange(value: BridgeExchangeInput):
        return core.bridge.exchange(value.code, value.verifier)

    @app.post("/api/max-bridge/submit", dependencies=[Depends(bridge_auth)])
    def bridge_submit(value: BridgePostInput):
        return core.bridge.submit(value.channel_token, value.integration_id, value.post_id)

    @app.post("/api/max-bridge/status", dependencies=[Depends(bridge_auth)])
    def bridge_status(value: BridgePostInput):
        return core.bridge.status(value.channel_token, value.integration_id, value.post_id)

    @app.post("/api/max-bridge/mutation/start", dependencies=[Depends(bridge_auth)])
    def bridge_mutation_start(value: BridgeMutationInput):
        if value.owner is not None:
            raise AgentError("BRIDGE_MUTATION_CAS_INVALID", 409)
        return core.bridge.mutation(value.identifiers, expected_hash=value.expected_hash,
                                    expected_post_id=value.expected_post_id)

    @app.post("/api/max-bridge/mutation/finish", dependencies=[Depends(bridge_auth)])
    def bridge_mutation_finish(value: BridgeMutationInput):
        if not value.owner:
            raise AgentError("BRIDGE_MUTATION_OWNER_REQUIRED", 422)
        if value.expected_hash is not None or value.expected_post_id is not None:
            raise AgentError("BRIDGE_MUTATION_CAS_INVALID", 409)
        return core.bridge.mutation(value.identifiers, value.owner)

    return app
