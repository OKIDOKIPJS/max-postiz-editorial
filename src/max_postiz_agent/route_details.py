"""Полная точная версия для просмотра владельцем, без quality/transport действий."""

import json

from .config import AgentError, fingerprint


def details(core, variant_id):
    """Чтение кандидата не согласует его и доступно при отказе preflight."""
    with core.db.session() as session:
        variant = core._variant(session, variant_id)
        target = core._target(session, variant.target_id)
        snapshot = core.snapshot(variant, target)
        result = {**snapshot, "content_hash": fingerprint(snapshot),
                  "state": variant.state, "read_only": True,
                  "manual_approval_allowed": variant.metadata_json.get("origin") != "hourly_article_test"}
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise AgentError("ROUTE_DETAILS_INVALID", 409) from None
        if len(encoded) > 900000:
            raise AgentError("ROUTE_DETAILS_SIZE_INVALID", 422)
        return result
