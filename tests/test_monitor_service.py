"""Поведение наблюдателя при рестарте, потере ответа и смене полномочий."""

import copy
import json
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.monitor_config import MonitorConfig, default_config
from max_postiz_agent.monitor_service import MonitorService
from max_postiz_agent.monitor_store import MonitorStore
from max_postiz_agent.browser_runtime import profile_lock
from test_monitor_reports import NOW, snap


class Transport:
    def __init__(self):
        self.sent, self.events = [], []
        self.destinations = []
        self.result = {"state": "confirmed", "verification": "browser_readback",
                       "independent_readback": True}

    def send_report(self, chat_id, text, request_id, *, authorize):
        assert authorize({"request_id": request_id, "chat_id": chat_id, "text": text})
        self.sent.append(request_id)
        self.destinations.append(chat_id)
        return self.result

    def reconcile_report(self, chat_id, request_id, *, text):
        return {"state": "confirmed", "verification": "browser_readback",
                "independent_readback": True}

    def read_commands(self):
        return self.events


@pytest.fixture
def setup_monitor(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL", "MAX_EXISTING_BROWSER_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "copy"
    root.mkdir()
    mark_isolated(root, tmp_path / "source")
    (root / "config").mkdir()
    value = default_config()
    value.update(enabled=True, report_chat_id="-123", report_chat_name="Мониторинг",
                 report_private_confirmed=True, hourly_delay_seconds=0, daily_enabled=False)

    def save(value):
        (root / "config/monitor.json").write_text(json.dumps(value), encoding="utf-8")
        return MonitorConfig.load(root)

    config = save(value)
    core = SimpleNamespace(settings=SimpleNamespace(root=root))
    transport = Transport()
    clock = [NOW]
    service = MonitorService(core, config, transport=transport, clock=lambda: clock[0],
                             collector=lambda core, now_at: {**snap(), "collected_at": now_at})
    return service, value, save, clock


def test_restart_does_not_repeat_hourly_or_catch_up_missed_hours(setup_monitor):
    service, _, _, clock = setup_monitor
    service.produce()
    assert service.deliver_once()["state"] == "confirmed"
    service.produce()
    assert service.deliver_once()["state"] == "idle_or_blocked"
    assert len(service.transport.sent) == 1
    clock[0] += 4 * 3600
    service.produce()
    rows = service.store.recent()
    assert len([r for r in rows if r["kind"] == "hourly"]) == 2


@pytest.mark.parametrize("result", [None, {"state": "confirmed"}, {"state": "unknown"}])
def test_unknown_or_unproved_receipt_blocks_lane_across_restart(setup_monitor, result):
    service, _, _, clock = setup_monitor
    service.transport.result = result
    service.queue("first", "hourly", "Отчёт 1")
    first = service.deliver_once()
    assert first["state"] == "unknown"
    service.queue("second", "hourly", "Отчёт 2")
    restarted = MonitorService(service.core, service.config, transport=service.transport,
                               clock=lambda: clock[0])
    assert restarted.deliver_once()["state"] == "idle_or_blocked"
    assert len(service.transport.sent) == 1
    assert restarted.reconcile(first["id"])["state"] == "confirmed"
    assert restarted.deliver_once()["state"] == "unknown"
    assert len(service.transport.sent) == 2


def test_interrupted_sender_claim_is_never_automatically_reissued(setup_monitor):
    service, _, _, _ = setup_monitor
    service.queue("first", "hourly", "Первый")
    claimed = service.store.claim(service.config.hash, "-123", at=NOW)
    assert claimed["state"] == "sending"
    assert service.deliver_once()["state"] == "idle_or_blocked"
    assert service.transport.sent == []


def test_expired_report_not_sent_and_new_current_report_allowed(setup_monitor):
    service, _, _, clock = setup_monitor
    service.queue("old", "hourly", "Старый")
    clock[0] += 901
    service.queue("new", "hourly", "Новый")
    assert service.deliver_once()["state"] == "confirmed"
    assert sorted(r["state"] for r in service.store.recent()) == ["confirmed", "expired"]


def test_config_change_revokes_old_producer_and_never_redirects_pending_report(setup_monitor):
    service, value, save, _ = setup_monitor
    service.queue("old", "hourly", "Прежний адресат")
    changed = copy.deepcopy(value)
    changed.update(report_chat_id="-456", report_chat_name="Другой")
    config = save(changed)
    with pytest.raises(AgentError, match="MONITOR_CONFIG_CHANGED"):
        service.deliver_once()
    replacement = MonitorService(service.core, config, transport=service.transport, clock=lambda: NOW)
    assert replacement.deliver_once()["state"] == "idle_or_blocked"
    assert service.transport.sent == []
    assert service.store.recent()[0]["state"] == "cancelled"


def test_disabled_monitor_does_not_open_browser_or_queue_messages(setup_monitor):
    service, value, save, _ = setup_monitor
    value["enabled"] = False
    service.config = save(value)
    assert service.produce()["state"] == "disabled"
    assert service.deliver_once()["state"] == "disabled"
    assert service.receive_once()["state"] == "disabled"
    assert service.store.recent() == []


def test_incident_is_reported_once_and_recovery_is_a_new_message(setup_monitor):
    service, _, _, _ = setup_monitor
    snapshot = snap()
    snapshot["editorial"]["reserve"]["ready"] = 0
    service.collector = lambda core, now_at: snapshot
    service.produce()
    service.produce()
    assert len([r for r in service.store.recent() if r["kind"] == "incident"]) == 1
    snapshot["editorial"]["reserve"]["ready"] = 6
    service.produce()
    assert len([r for r in service.store.recent() if r["kind"] == "incident"]) == 2


def test_exact_text_or_claim_tampering_denies_authority(setup_monitor):
    service, _, _, _ = setup_monitor
    service.queue("first", "hourly", "Точный отчёт")
    row = service.store.claim(service.config.hash, "-123", at=NOW)
    assert service.store.authorized(row)
    row["text"] += " изменён"
    assert not service.store.authorized(row)


def test_pending_unique_event_is_stable_despite_different_observation_text(setup_monitor):
    service, _, _, _ = setup_monitor
    first = service.queue("event", "hourly", "Первый точный отчёт")
    second = service.queue("event", "hourly", "Изменённое наблюдение")
    assert first["id"] == second["id"] and second["text"] == first["text"]


def test_hourly_report_cannot_cross_into_next_hour_as_catchup(setup_monitor):
    service, _, _, clock = setup_monitor
    service.collector = lambda core, now_at: {**snap(), "collected_at": now_at,
        "editorial": {**snap()["editorial"], "heartbeat_at": now_at}}
    clock[0] = int(NOW // 3600) * 3600 + 3599
    service.produce()
    clock[0] += 2
    assert service.deliver_once()["state"] == "idle_or_blocked"
    assert service.transport.sent == []
    assert service.store.recent()[0]["state"] == "expired"


def test_receiver_owns_browser_without_consuming_report_claim(setup_monitor):
    service, _, _, _ = setup_monitor
    service.queue("first", "hourly", "Отчёт")
    with profile_lock(service.core.settings.root / "data/monitor/transport.lock"):
        with pytest.raises(AgentError, match="BROWSER_PROFILE_BUSY"):
            service.deliver_once()
    assert service.store.recent()[0]["state"] == "pending"
    assert service.transport.sent == []


def test_sender_evidence_does_not_echo_arbitrary_transport_values(setup_monitor):
    service, _, _, _ = setup_monitor
    service.transport.result = {"state": "unknown", "verification": "secret-private-url",
                                "independent_readback": "secret"}
    service.queue("first", "hourly", "Отчёт")
    assert service.deliver_once()["state"] == "unknown"
    assert "secret" not in json.dumps(service.store.recent())


def test_control_chat_change_primes_new_history_instead_of_executing_it(setup_monitor):
    from test_monitor_browser import CONTRACT
    service, value, save, clock = setup_monitor
    value["commands"].update(enabled=True, control_chat_id="-2", control_chat_name="Чат",
        account_user_id="100", owner_ids=["100"], dom_contract=CONTRACT,
        dom_contract_verified=True)
    from max_postiz_agent.config import fingerprint
    value["commands"]["dom_contract_sha256"] = fingerprint(CONTRACT)
    service.config = save(value)
    service.transport.events = [{"message_id": "old", "created_at": NOW - 1}]
    assert service.receive_once()["state"] == "primed"
    clock[0] += 10
    value["commands"]["control_chat_id"] = "-3"
    service.config = save(value)
    service.transport.events = [{"message_id": "new-chat-history", "created_at": NOW + 1}]
    service.gateway = lambda: pytest.fail("Pre-pairing history reached gateway")
    assert service.receive_once()["state"] == "primed"


def test_private_state_cannot_follow_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "other"
    target.mkdir()
    try:
        (root / "data").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(AgentError, match="MONITOR_(STATE_LINK_FORBIDDEN|PATH_INVALID)"):
        MonitorStore(root)


def test_receive_failure_becomes_safe_incident_and_recovery(setup_monitor):
    from test_monitor_browser import CONTRACT
    from max_postiz_agent.config import fingerprint
    service, value, save, _ = setup_monitor
    value["commands"].update(enabled=True, control_chat_id="-2", control_chat_name="Чат",
        account_user_id="100", owner_ids=["100"], dom_contract=CONTRACT,
        dom_contract_verified=True, dom_contract_sha256=fingerprint(CONTRACT))
    service.config = save(value)
    def broken():
        raise ValueError("private DOM details")
    service.transport.read_commands = broken
    with pytest.raises(AgentError, match="MONITOR_COMMAND_RECEIVE_UNAVAILABLE"):
        service.receive_once()
    assert "private" not in json.dumps(service.store.meta("command_receiver"))
    service.produce()
    assert "inc-command-receiver" in service.store.meta("incidents")
    service.transport.read_commands = lambda: []
    assert service.receive_once()["state"] == "primed"
    service.produce()
    assert "inc-command-receiver" not in service.store.meta("incidents")


def test_benign_busy_receiver_does_not_report_failure(setup_monitor):
    from test_monitor_browser import CONTRACT
    from max_postiz_agent.config import fingerprint
    service, value, save, _ = setup_monitor
    value["commands"].update(enabled=True, control_chat_id="-2", control_chat_name="Чат",
        account_user_id="100", owner_ids=["100"], dom_contract=CONTRACT,
        dom_contract_verified=True, dom_contract_sha256=fingerprint(CONTRACT))
    service.config = save(value)
    with profile_lock(service.core.settings.root / "data/monitor/transport.lock"):
        assert service.receive_once()["state"] == "busy"
    assert service.store.meta("command_receiver") is None


def test_receiver_rate_limit_stops_repeated_network_reads_until_new_pairing(setup_monitor):
    from test_monitor_browser import CONTRACT
    from max_postiz_agent.config import fingerprint
    service, value, save, _ = setup_monitor
    value["commands"].update(enabled=True, control_chat_id="-2", control_chat_name="Чат",
        account_user_id="100", owner_ids=["100"], dom_contract=CONTRACT,
        dom_contract_verified=True, dom_contract_sha256=fingerprint(CONTRACT))
    service.config = save(value)
    calls = []
    def limited():
        calls.append(1)
        raise AgentError("MONITOR_OWNER_RATE_LIMITED")
    service.transport.read_commands = limited
    with pytest.raises(AgentError, match="MONITOR_OWNER_RATE_LIMITED"):
        service.receive_once()
    assert service.receive_once() == {"state": "blocked", "accepted": 0, "requires_owner": True}
    assert calls == [1]


def control_reply_config(setup_monitor, *, shared=False):
    from max_postiz_agent.monitor_owner_chat import owner_contract
    from max_postiz_agent.config import fingerprint
    service, value, save, _ = setup_monitor
    value["commands"].update(enabled=True, control_chat_id="-123" if shared else "-456",
        control_chat_name=value["report_chat_name"] if shared else "Управление",
        auth_mode="authenticated_account_outgoing", account_sha256="a" * 64,
        allowed_actions=["status", "reserve", "refill"], dom_contract=owner_contract(),
        dom_contract_verified=True, dom_contract_sha256=fingerprint(owner_contract()), reply_in_control_chat=True)
    service.config = save(value)
    return service


def test_command_replies_use_control_group_while_other_kinds_stay_at_report_destination(setup_monitor):
    service = control_reply_config(setup_monitor)
    command = service.queue("reply", "command", "Ответ команды")
    regular = service.queue("regular", "hourly", "Почасовой отчёт")
    assert command["chat_id"] == "-456" and regular["chat_id"] == "-123"
    assert service.deliver_once()["state"] == "confirmed"
    assert service.deliver_once()["state"] == "confirmed"
    assert sorted(service.transport.destinations) == ["-123", "-456"]


@pytest.mark.parametrize("kind", ["hourly", "daily", "incident", "watchdog", "command"])
def test_single_solo_group_receives_each_allowed_report_kind(setup_monitor, kind):
    service = control_reply_config(setup_monitor, shared=True)
    row = service.queue("one-" + kind, kind, "Служебное сообщение")
    assert row["chat_id"] == "-123"
    assert service._allowed_destinations() == ("-123",)
    assert service.deliver_once()["state"] == "confirmed"
    assert service.transport.destinations == ["-123"]


def test_default_flag_keeps_command_response_in_original_report_channel(setup_monitor):
    service, _, _, _ = setup_monitor
    service._queue_command_result({"command_id": "sample", "message": "Ответ"}, "sample")
    assert service.store.recent()[0]["chat_id"] == "-123"


def test_unknown_in_control_group_blocks_regular_report_and_readonly_reconcile_unblocks(setup_monitor):
    service = control_reply_config(setup_monitor)
    first = service.queue("reply", "command", "Ответ команды")
    service.transport.result = {"state": "unknown"}
    assert service.deliver_once()["state"] == "unknown"
    service.queue("regular", "hourly", "Обычный отчёт")
    assert service.deliver_once()["state"] == "idle_or_blocked"
    assert len(service.transport.sent) == 1
    assert service.reconcile(first["id"])["state"] == "confirmed"
    assert len(service.transport.sent) == 1
    service.transport.result = {"state": "confirmed", "verification": "browser_readback", "independent_readback": True}
    assert service.deliver_once()["state"] == "confirmed"
    assert service.transport.destinations == ["-456", "-123"]


def test_unknown_from_old_or_unlisted_destination_blocks_every_new_route(setup_monitor):
    service = control_reply_config(setup_monitor)
    old = service.store.enqueue("old-route", "hourly", "Прежняя попытка", service.config.hash, "-999", at=NOW, expires_at=NOW + 900)
    claim = service.store.claim(service.config.hash, "-999", at=NOW)
    service.store.finish(claim, "unknown", {"state": "unknown"}, at=NOW)
    service.queue("new-command", "command", "Ответ")
    service.queue("new-hourly", "hourly", "Отчёт")
    assert service.deliver_once()["state"] == "idle_or_blocked"
    assert service.transport.sent == []
    assert service.store.get(old["id"])["state"] == "unknown"


def test_routing_cannot_send_regular_report_to_control_chat_by_forged_kind_metadata(setup_monitor):
    service = control_reply_config(setup_monitor)
    row = service.store.enqueue("forged", "hourly", "Не тот маршрут", service.config.hash, "-456", at=NOW, expires_at=NOW + 900)
    with pytest.raises(AgentError, match="MONITOR_DESTINATION_CHANGED"):
        service.deliver_once()
    assert service.transport.sent == []
    assert service.store.get(row["id"])["state"] == "failed"


def test_wrong_name_or_chat_in_send_authorization_never_gets_permission(setup_monitor):
    service = control_reply_config(setup_monitor)
    def wrong_metadata(chat_id, text, request_id, *, authorize):
        assert authorize({"request_id": request_id, "chat_id": chat_id, "text": text, "chat_name": "Чужое имя"}) is False
        assert authorize({"request_id": request_id, "chat_id": "-999", "text": text}) is False
        assert authorize({"request_id": request_id, "chat_id": chat_id, "text": text,
                          "destination_kind": "channel"}) is False
        assert authorize({"request_id": request_id, "chat_id": chat_id, "text": text,
                          "chat_name": "Управление", "destination_kind": "solo_group"}) is True
        return {"state": "preclick_failed"}
    service.transport.send_report = wrong_metadata
    service.queue("reply", "command", "Ответ")
    assert service.deliver_once()["state"] == "failed"
    assert service.transport.sent == []


def test_report_channel_rejects_envelope_claiming_a_group_context(setup_monitor):
    service, _, _, _ = setup_monitor
    def metadata(chat_id, text, request_id, *, authorize):
        base = {"request_id": request_id, "chat_id": chat_id, "text": text}
        assert authorize({**base, "destination_kind": "solo_group"}) is False
        assert authorize({**base, "destination_kind": "channel"}) is True
        return {"state": "preclick_failed"}
    service.transport.send_report = metadata
    service.queue("report", "hourly", "Отчёт")
    assert service.deliver_once()["state"] == "failed"
    assert service.transport.sent == []


def test_in_memory_destination_name_mutation_is_detected_before_any_claim(setup_monitor):
    service = control_reply_config(setup_monitor)
    service.queue("reply", "command", "Ответ")
    service.config.value["commands"]["control_chat_name"] = "Другое имя"
    with pytest.raises(AgentError, match="MONITOR_CONFIG_CHANGED"):
        service.deliver_once()
    assert service.transport.sent == []
    assert service.store.recent()[0]["state"] == "pending"


def test_report_kind_and_expiration_are_part_of_claim_authority(setup_monitor):
    service, _, _, _ = setup_monitor
    service.queue("one", "hourly", "Отчёт")
    row = service.store.claim(service.config.hash, ("-123",), at=NOW)
    assert service.store.authorized(row) is True
    with service.store.connection() as connection:
        connection.execute("UPDATE monitor_reports SET kind='command' WHERE id=?", (row["id"],))
    assert service.store.authorized(row) is False


@pytest.mark.parametrize("destinations", [(), ("-1", "-1"), ("-1", "-2", "-3"), ["-1"], ("not-a-chat",)])
def test_atomic_sender_claim_accepts_only_bounded_destination_tuple(setup_monitor, destinations):
    service, _, _, _ = setup_monitor
    with pytest.raises(AgentError, match="MONITOR_DESTINATION_INVALID"):
        service.store.claim(service.config.hash, destinations, at=NOW)
