"""Полная точная версия для просмотра владельцем, без quality/transport действий."""

import json

from .config import AgentError, fingerprint


MEDIA_POLICIES = {
    "exact-bytes-v1": ("Точные байты", "Опубликованное изображение должно иметь SHA-256 согласованного файла."),
    "exact-pixels-v1": ("Точные пиксели", "Проверяются точные пиксели и цветовые метаданные; похожесть изображения не считается доказательством."),
    "native-asset-lineage-v1": ("Исходное изображение VK", "Допустимо пересжатие VK при сохранении размеров; проверяются родство единственного фото и два повторных чтения."),
    "native-uniform-edge-lineage-v1": ("Изображение VK с однородным краем", "Допустима только ограниченная обрезка проверенного однородного края по отдельному контракту; содержательная обрезка запрещена."),
}


def media_policy(core, variant, target):
    if target.route != "vk_browser" or not variant.media or variant.binding_hash != target.binding_hash:
        return None
    from .server_route_admin import _configuration, _configured_target
    try:
        configured = _configured_target(*_configuration(core)[:2], target)
    except AgentError:
        return None
    policy = configured.get("options", {}).get("media_proof_policy")
    if policy not in MEDIA_POLICIES:
        return None
    label, description = MEDIA_POLICIES[policy]
    return {"id": policy, "label": label, "description": description}


def details(core, variant_id):
    """Чтение кандидата не согласует его и доступно при отказе preflight."""
    with core.db.session() as session:
        variant = core._variant(session, variant_id)
        target = core._target(session, variant.target_id)
        snapshot = core.snapshot(variant, target)
        result = {**snapshot, "content_hash": fingerprint(snapshot),
                  "state": variant.state, "read_only": True,
                  "media_policy": media_policy(core, variant, target),
                  "manual_approval_allowed": variant.metadata_json.get("origin") != "hourly_article_test"}
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise AgentError("ROUTE_DETAILS_INVALID", 409) from None
        if len(encoded) > 900000:
            raise AgentError("ROUTE_DETAILS_SIZE_INVALID", 422)
        return result
