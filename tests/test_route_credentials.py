"""Локальный ввод ключа не отправляет секрет и не снимает защиту очереди."""

import json

import pytest
from sqlalchemy import select

from test_hub import approve, variants
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Target
from max_postiz_agent.routes_cli import store_token

pytest_plugins = ["test_four_platform_hub"]
TOKEN = "123456:fixture_only_not_real_bot_token"


def test_secret_is_saved_only_in_local_dotenv_and_target_is_paused(four_hub):
    result = store_token(four_hub, "telegram", TOKEN)
    path = four_hub.settings.root / ".env.local"
    assert "TELEGRAM_BOT_TOKEN=" + TOKEN in path.read_text(encoding="utf-8")
    assert result["paused"] is True and result["external_mutations"] == 0
    assert TOKEN not in json.dumps(result)
    with four_hub.db.session() as session:
        assert session.get(Target, "telegram").paused is True
        events = list(session.scalars(select(Audit).where(
            Audit.action == "route_credential_change_paused")))
        assert len(events) == 1 and TOKEN not in json.dumps(events[0].details)
    assert all(adapter.publications == [] for adapter in four_hub.fixture_publishers.values())


def test_unknown_delivery_prevents_credential_change(four_hub):
    adapter = four_hub.fixture_publishers["telegram"]
    adapter.result_state = "unknown"
    approve(four_hub, variants(four_hub)["telegram"])
    four_hub.publish_due()
    with pytest.raises(AgentError, match="CANCEL_OR_RECONCILE_FIRST"):
        store_token(four_hub, "telegram", TOKEN)
    assert not (four_hub.settings.root / ".env.local").exists()


@pytest.mark.parametrize("value", ["short", "a" * 20 + "\nDATABASE_URL=wrong", "a" * 20 + "=evil"])
def test_invalid_secret_cannot_inject_another_environment_key(four_hub, value):
    with pytest.raises(AgentError, match="CREDENTIAL_FORMAT_INVALID"):
        store_token(four_hub, "telegram", value)
    assert not (four_hub.settings.root / ".env.local").exists()
