"""Команды маршрутов из проверенной owner-сессии Postiz, без произвольной отправки."""

from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .config import AgentError
from .db import now
from .platform_management import inspect_target, preflight, preview, reconcile, route_state


class RouteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inspect", "preflight", "preview", "approve", "pause", "resume", "reconcile"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    revision: StrictInt | None = Field(default=None, ge=1)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    due_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


def attach_route_api(app, core, owner):
    owner_dependency = Depends(owner)
    @app.get("/internal/editorial/routes")
    def routes(actor=owner_dependency):
        return route_state(core)

    @app.post("/internal/editorial/route-action")
    def route_action(value: RouteAction, actor=owner_dependency):
        fields = value.model_fields_set
        expected = ({"action", "id", "revision", "content_hash", "due_at"}
                    if value.action == "approve" else {"action", "id"})
        if fields != expected:
            raise AgentError("ROUTE_ACTION_INVALID", 422)
        if value.action == "inspect":
            return inspect_target(core, value.id)
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
