"""Маршруты публикации: доступ и квитанция не подменяют точную сверку поста."""

import os

from sqlalchemy import select

from .config import AgentError, fingerprint, inside


DIRECT_ROUTES = {"telegram_api": "telegram", "vk_api": "vk", "vk_browser": "vk",
                 "pikabu_browser": "pikabu"}
ROUTE_PLATFORMS = {**DIRECT_ROUTES, "max_browser": "max", "max_api": "max"}


def validate_route(target):
    expected = ROUTE_PLATFORMS.get(target["route"])
    if expected is not None and expected != target["platform"]:
        raise AgentError("TARGET_ROUTE_INVALID")


class PlatformRoutes:
    """Создаём адаптер только при явной операции, без сетевых проб при старте."""

    def __init__(self, core, publishers=None):
        self.core = core
        self.injected = publishers or {}
        self.cache = {}

    def publisher(self, target):
        if target.id in self.injected:
            return self.injected[target.id]
        key = (target.id, target.binding_hash, fingerprint(target.options))
        if key not in self.cache:
            root, options = self.core.settings.root, target.options
            if target.route == "telegram_api":
                from .telegram_publisher import TelegramPublisher

                adapter = TelegramPublisher(root, options)
            elif target.route == "vk_api":
                from .vk_publisher import VKPublisher

                adapter = VKPublisher(root, options)
            elif target.route == "pikabu_browser":
                from .pikabu_browser import PikabuBrowser

                adapter = PikabuBrowser(root, options)
            elif target.route == "vk_browser":
                from .vk_browser import VKBrowser

                adapter = VKBrowser(root, options)
            else:
                raise AgentError("TARGET_ROUTE_UNSUPPORTED")
            self.cache[key] = adapter
        if target.route in {"vk_browser", "pikabu_browser"}:
            self.cache[key].runtime_generation = self.core.route_runtime_generation()
        return self.cache[key]

    def validate(self, target, snapshot):
        if target.route in DIRECT_ROUTES:
            return self.publisher(target).validate(target.destination, snapshot)
        return {"preflight_only": True, "route": target.route}

    def publish(self, dispatch, target):
        adapter = self.publisher(target)
        if target.route in {"telegram_api", "vk_api"}:
            return adapter.publish(target.destination, dispatch.snapshot, request_id=dispatch.id,
                                   authorize=lambda: self._authorize(dispatch))
        return adapter.publish(target.destination, dispatch.snapshot, request_id=dispatch.id)

    def _authorize(self, original):
        """Пауза/lease/exact approval проверяются снова перед каждым API-изменением."""
        from .db import now
        from .hub_models import Dispatch
        from .server_state import require_running

        with self.core.db.session() as session:
            require_running(self.core, session)
            dispatch = session.scalar(select(Dispatch).where(Dispatch.id == original.id))
            if (dispatch is None or dispatch.state != "sending" or not original.owner
                    or dispatch.owner != original.owner or dispatch.lease_until <= now() + 2
                    or dispatch.snapshot_hash != original.snapshot_hash or dispatch.due_at > now()
                    or fingerprint(dispatch.snapshot) != fingerprint(original.snapshot)):
                raise AgentError("ROUTE_DISPATCH_LEASE_LOST", 409)
            self.core._valid_dispatch(session, dispatch)
        return self.core.authorize_dispatch_fence(original.id, expected_owner=original.owner,
                                                  expected_snapshot_hash=original.snapshot_hash)

    def inspect(self, target):
        if target.route in DIRECT_ROUTES:
            self.core._assert_current_route_runtime()
            return self.publisher(target).inspect(target.destination)
        if target.route in {"max_browser", "max_api"}:
            return self.core.publisher.inspect(target.destination)
        if target.route == "manual":
            return {"state": "manual", "publish": False, "live_verified": False}
        raise AgentError("TARGET_ROUTE_UNSUPPORTED")

    def reconcile(self, dispatch, target):
        adapter = self.publisher(target)
        if target.route in {"pikabu_browser", "vk_browser"}:
            return adapter.reconcile(target.destination, dispatch.id, snapshot=dispatch.snapshot)
        if target.route == "vk_api":
            return adapter.reconcile(target.destination, dispatch.snapshot,
                                     dispatch.external_id, request_id=dispatch.id)
        if target.route == "telegram_api":
            return adapter.reconcile(target.destination, dispatch.snapshot, dispatch.id)
        raise AgentError("TARGET_ROUTE_UNSUPPORTED")

    def status(self, target):
        options = target.options
        result = {"id": target.id, "platform": target.platform, "route": target.route,
                  "destination": target.destination, "enabled": target.enabled,
                  "paused": target.paused, "binding_hash": target.binding_hash,
                  "live_verified": False, "status_kind": "configuration_only"}
        if target.route in {"telegram_api", "vk_api"}:
            default = "TELEGRAM_BOT_TOKEN" if target.route == "telegram_api" else "VK_ACCESS_TOKEN"
            result["credentials_configured"] = bool(os.getenv(options.get("token_env", default)))
            result["adapter_mode"] = options.get("adapter_mode", "fake")
        elif target.route in {"pikabu_browser", "vk_browser"}:
            result["owner_configured"] = bool(options.get("owner"))
            result["profile_exists"] = inside(self.core.settings.root,
                options.get("profile_dir", "data/" + target.platform + "-browser")).exists()
            result["dom_contract_verified"] = options.get("dom_contract_verified", False)
            result["media_contract_verified"] = options.get("media_contract_verified", False)
        return result

    def close(self):
        for adapter in self.cache.values():
            adapter.close()
        self.cache.clear()
