"""Команды маршрутов из проверенной owner-сессии Postiz, без произвольной отправки."""

import re
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .config import AgentError
from .db import now
from .platform_management import inspect_target, preflight, preview, reconcile, route_state
from .route_details import details


class RouteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inspect", "details", "preflight", "preview", "approve", "pause", "resume", "reconcile"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    revision: StrictInt | None = Field(default=None, ge=1)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    due_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


def attach_route_api(app, core, owner):
    from .server_route_api import attach_admin_api
    attach_admin_api(app, core, owner)
    owner_dependency = Depends(owner)
    @app.get("/internal/editorial/routes")
    def routes(request: Request, actor=owner_dependency):
        keys = {"variant_page", "dispatch_page", "dispatch_filter"}
        if any(key not in keys or len(request.query_params.getlist(key)) != 1
               for key in request.query_params):
            raise AgentError("ROUTE_LIST_INVALID", 422)
        pages = {}
        for key in ("variant_page", "dispatch_page"):
            raw = request.query_params.get(key, "0")
            if not re.fullmatch(r"0|[1-9][0-9]{0,6}", raw) or int(raw) > 1000000:
                raise AgentError("ROUTE_LIST_INVALID", 422)
            pages[key] = int(raw)
        dispatch_filter = request.query_params.get("dispatch_filter", "all")
        if dispatch_filter not in {"all", "unresolved"}:
            raise AgentError("ROUTE_LIST_INVALID", 422)
        return route_state(core, **pages, dispatch_filter=dispatch_filter)

    @app.post("/internal/editorial/route-action")
    def route_action(value: RouteAction, actor=owner_dependency):
        fields = value.model_fields_set
        expected = ({"action", "id", "revision", "content_hash", "due_at"}
                    if value.action == "approve" else {"action", "id"})
        if fields != expected:
            raise AgentError("ROUTE_ACTION_INVALID", 422)
        if value.action == "inspect":
            return inspect_target(core, value.id)
        if value.action == "details":
            return details(core, value.id)
        if value.action == "preflight":
            return preflight(core, value.id)
        if value.action == "preview":
            return preview(core, value.id)
        if value.action in {"pause", "resume"}:
            core.pause_target(value.id, value.action == "pause", actor=actor)
            return {"id": value.id, "paused": value.action == "pause"}
        if value.action == "reconcile":
            return reconcile(core, value.id, actor=actor)
        if value.revision is None or value.content_hash is None or value.due_at is None:
            raise AgentError("ROUTE_ACTION_INVALID", 422)
        if value.due_at <= now():
            raise AgentError("SCHEDULE_FUTURE_REQUIRED", 422)
        with core.db.session() as session:
            if core._variant(session, value.id).metadata_json.get("origin") == "hourly_article_test":
                raise AgentError("USE_HOURLY_EDITORIAL_POLICY", 409)
        ident = core.approve_variant(value.id, value.revision, value.content_hash,
                                     value.due_at, actor=actor)
        return {"dispatch_id": ident, "state": "approved"}
