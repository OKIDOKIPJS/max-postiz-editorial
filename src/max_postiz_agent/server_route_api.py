"""Строгий owner-only DTO для настройки и авторских черновиков Postiz."""

from typing import Literal
import re

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from starlette.concurrency import run_in_threadpool

from .config import AgentError, parse_json
from . import server_route_admin as admin


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Original(Input):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=40000)
    media: list[dict] = Field(max_length=1)
    metadata: dict


class TargetInput(Input):
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    platform: Literal["max", "telegram", "vk"]
    route: Literal["max_browser", "telegram_api", "vk_browser"]
    destination: str = Field(min_length=1, max_length=200)
    enabled: bool
    options: dict


class AdminAction(Input):
    action: Literal["configure_target", "set_secret", "upload_media", "create_original", "revise_original", "prepare_login", "request_restart"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    expected_binding_hash: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    expected_config_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target: TargetInput | None = None
    value: str | None = Field(default=None, max_length=4096)
    mime: Literal["image/png", "image/jpeg"] | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    data_base64: str | None = Field(default=None, max_length=13981016)
    original: Original | None = None
    attest_original: bool | None = None
    revision: StrictInt | None = Field(default=None, ge=1)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


FIELDS = {"configure_target": {"expected_config_hash", "target"}, "set_secret": {"value"},
          "upload_media": {"mime", "sha256", "data_base64"},
          "create_original": {"original", "attest_original"},
          "revise_original": {"original", "attest_original", "revision", "content_hash"}, "prepare_login": set(),
          "request_restart": {"expected_config_hash"}}


def attach_admin_api(app, core, owner):
    dependency = Depends(owner)

    @app.get("/internal/editorial/route-setup")
    def setup(actor=dependency):
        return admin.setup(core)

    @app.post("/internal/editorial/route-admin")
    async def route_admin(request: Request, actor=dependency):
        # Owner dependency runs before any secret is read; never include submitted
        # values in validation errors. The bounded stream also covers chunked bodies.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 14 * 1024 * 1024:
                raise AgentError("INPUT_TOO_LARGE", 413)
        try:
            value = AdminAction.model_validate(parse_json(raw.decode("utf-8")))
        except (ValidationError, UnicodeError, AgentError):
            raise AgentError("INPUT_INVALID", 422) from None
        fields = {"action", "id", "expected_binding_hash"} | FIELDS[value.action]
        if value.model_fields_set != fields or any(
                getattr(value, key) is None for key in fields - {"expected_binding_hash"}):
            raise AgentError("INPUT_INVALID", 422)
        if value.action not in {"configure_target", "request_restart"} and value.expected_binding_hash is None:
            raise AgentError("INPUT_INVALID", 422)
        if value.action == "configure_target" and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value.id):
            raise AgentError("INPUT_INVALID", 422)
        return await run_in_threadpool(admin.execute, core, value.model_dump(exclude_unset=True), actor=actor)

    @app.get("/internal/editorial/runtime")
    def runtime(actor=dependency):
        from .server_maintenance import RESTART_PATH
        from .server_state import ServerState
        pending = False
        if admin.plain(core.settings.root, RESTART_PATH).exists():
            try:
                from .db import now
                manifest = parse_json(admin.read_local(core.settings.root, RESTART_PATH).decode("utf-8-sig"))
                pending = (isinstance(manifest, dict) and manifest.get("previous_worker_id") == core.worker_id
                           and type(manifest.get("expires_at")) is int and manifest["expires_at"] > now())
            except (AgentError, UnicodeError):
                pending = True
        return {"worker_id": core.worker_id, "paused": ServerState(core).read()["paused"],
                "restart_pending": pending}
