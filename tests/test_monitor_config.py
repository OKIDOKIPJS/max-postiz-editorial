"""Конфигурация не открывает production и не включает неподтверждённые команды."""

import copy

import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.monitor_config import default_config, validate_config
from max_postiz_agent.monitor_owner_chat import owner_contract


def test_defaults_disabled_and_do_not_contain_actual_chat_or_identity():
    value = validate_config(default_config())
    assert value["enabled"] is False and value["commands"]["enabled"] is False
    assert value["report_chat_id"] == "" and value["commands"]["owner_ids"] == []
    assert value["commands"]["reply_in_control_chat"] is False


def one_chat_config():
    value = default_config()
    value.update(enabled=True, report_chat_id="-1", report_chat_name="Управление", report_private_confirmed=True)
    value["commands"].update(enabled=True, control_chat_id="-1", control_chat_name="Управление",
        auth_mode="authenticated_account_outgoing", account_sha256="a" * 64,
        allowed_actions=["status", "reserve", "refill"], dom_contract=owner_contract(),
        dom_contract_verified=True, dom_contract_sha256=fingerprint(owner_contract()), reply_in_control_chat=True)
    return value


def test_legacy_config_missing_reply_flag_keeps_old_destination_rule():
    value = default_config()
    del value["commands"]["reply_in_control_chat"]
    assert validate_config(value)["commands"]["reply_in_control_chat"] is False


def test_one_private_solo_group_can_receive_reports_and_replies_with_exact_owner_contract():
    value = validate_config(one_chat_config())
    assert value["report_chat_id"] == value["commands"]["control_chat_id"]
    assert value["commands"]["reply_in_control_chat"] is True
    value["commands"]["enabled"] = False
    assert validate_config(value)["commands"]["enabled"] is False


@pytest.mark.parametrize("change", [
    {"reply_in_control_chat": False}, {"reply_in_control_chat": "true"},
    {"control_chat_name": "Другое имя"}, {"control_chat_name": "Управление "},
    {"auth_mode": "numeric_sender", "account_sha256": "", "account_user_id": "1", "owner_ids": ["1"]},
    {"dom_contract_verified": False}, {"control_chat_id": ""},
])
def test_shared_group_rejects_unsafe_scope_or_mismatched_name(change):
    value = one_chat_config()
    value["commands"].update(change)
    with pytest.raises(AgentError):
        validate_config(value)


def test_shared_group_needs_private_proof_even_when_disabled():
    value = one_chat_config()
    value.update(enabled=False, report_private_confirmed=False)
    value["commands"]["enabled"] = False
    with pytest.raises(AgentError):
        validate_config(value)


def test_reply_in_separate_control_group_also_requires_accepted_outgoing_contract():
    value = one_chat_config()
    value["commands"]["control_chat_id"] = "-2"
    assert validate_config(value)["commands"]["reply_in_control_chat"] is True
    value["commands"].update(auth_mode="numeric_sender", account_sha256="", account_user_id="1", owner_ids=["1"])
    with pytest.raises(AgentError):
        validate_config(value)


@pytest.mark.parametrize("patch", [
    {"enabled": "true"}, {"daily_hour": True}, {"poll_seconds": 1},
    {"report_chat_id": "https://max.ru/foo"}, {"panel_url": "http://user:pass@example.com"},
    {"panel_url": "https://example.com/?token=private"}, {"unknown": True},
    {"report_chat_name": "Заголовок\nНовая строка"}, {"enabled": True},
])
def test_unsafe_or_malformed_config_fails_closed(patch):
    with pytest.raises(AgentError):
        validate_config({**default_config(), **patch})


def test_commands_require_distinct_chat_verified_contract_and_owner():
    value = default_config()
    value.update(enabled=True, report_chat_id="-1", report_chat_name="Отчёты",
                 report_private_confirmed=True)
    value["commands"].update(enabled=True, control_chat_id="-2", control_chat_name="Команды",
                             account_user_id="1", owner_ids=["1"])
    with pytest.raises(AgentError, match="MONITOR_COMMAND_PAIRING_REQUIRED"):
        validate_config(value)
    value["commands"].update(dom_contract={"test": "offline"}, dom_contract_verified=True,
                             dom_contract_sha256=fingerprint({"test": "offline"}))
    assert validate_config(value)["commands"]["enabled"] is True
    changed = copy.deepcopy(value)
    changed["commands"]["control_chat_id"] = "-1"
    with pytest.raises(AgentError, match="MONITOR_COMMAND_PAIRING_REQUIRED"):
        validate_config(changed)
    value["commands"]["dom_contract"]["test"] = "changed"
    with pytest.raises(AgentError, match="MONITOR_COMMAND_CONTRACT_CHANGED"):
        validate_config(value)
