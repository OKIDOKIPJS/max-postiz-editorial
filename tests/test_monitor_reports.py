"""Отчёты не превращают журнал/API receipt в новую живую доставку."""

import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from max_postiz_agent import monitor_reports as monitor
from max_postiz_agent.server_state import ServerState

pytest_plugins = ["test_four_platform_hub"]

NOW = 1793520000.0


def state():
    return {"version": "0.12.1", "paused": False, "phase": "waiting_dispatch", "heartbeat_at": NOW - 5,
        "last_error": None, "reserve": {"enabled": True, "ready": 6, "target": 6, "low": 3,
            "active": [], "slots": {"published": 12}, "next_delivery_at": NOW + 3600},
        "preparation": {"phase": "reserve_satisfied", "heartbeat_at": NOW - 10}}


def routes():
    return {"targets": [{"id": platform, "platform": platform, "enabled": True, "paused": True,
                          "destination": "-12345"} for platform in ("max", "pikabu", "vk", "telegram")],
            "dispatches": [], "pagination": {"dispatch_count": 0, "dispatch_active_count": 0,
                                              "dispatch_has_next": False, "unresolved_complete": True}}


def snap(*rows):
    value = {"schema": monitor.SCHEMA, "collected_at": NOW, "read_only": True,
             "editorial": state(), "routes": routes()}
    value["routes"]["dispatches"] = list(rows)
    return value


def dispatch(platform="max", *, ident=None, state="published", verified=True,
             proof="independent_readback", **extra):
    return {"id": ident or "post-" + platform, "target_id": platform, "platform": platform,
            "title": "Туманность и новые звёзды", "state": state, "verified": verified,
            "evidence_kind": proof, "due_at": NOW - 60, "attempts": 1, **extra}


def report(value, **kwargs):
    return monitor.render_report(value, report_id="report-1", now_at=NOW, **kwargs)


def test_partial_success_is_reported_per_target_and_never_as_all_confirmed():
    value = snap(dispatch(), dispatch("telegram", verified=False, proof="api_receipt"),
                 dispatch("pikabu", state="unknown", verified=False, proof="unverified"),
                 dispatch("vk", state="failed", verified=False, proof="unverified"))
    text = report(value)
    assert "доставка подтверждена сохранённой независимой сверкой" in text
    assert "API принял; независимого подтверждения нет" in text
    assert "исход неизвестен; повтор запрещён" in text
    assert "ошибка отправки" in text
    assert "новой сверки площадок не выполнялось" in text
    assert "активных ошибок и блокировок не найдено" not in text
    assert all(name in text for name in ("MAX", "Пикабу", "VK", "Telegram"))


def test_historical_recipient_and_platform_come_from_dispatch_not_current_target_binding():
    value = snap(dispatch("telegram", target_id="max", destination="-1001111111111"))
    value["routes"]["targets"][0]["destination"] = "-99999"
    text = report(value)
    assert "Telegram → -1001111111111" in text
    assert "MAX → -99999" not in text
    missing = report(snap(dispatch()))
    assert "MAX → адресат не сохранён" in missing


@pytest.mark.parametrize("proof,verified,fragment", [
    ("api_receipt", True, "API принял; независимого подтверждения нет"),
    ("fixture", True, "тестовая квитанция; живая доставка не подтверждена"),
    ("unverified", True, "отмечено как published; независимого подтверждения нет"),
    ("independent_readback", False, "отмечено как published; независимого подтверждения нет"),
])
def test_only_independent_bound_proof_and_verified_confirm_delivery(proof, verified, fragment):
    text = report(snap(dispatch(proof=proof, verified=verified)))
    assert fragment in text
    assert "доставка подтверждена сохранённой независимой сверкой" not in text


def test_old_snapshot_and_old_heartbeat_are_explicit_without_claiming_server_down():
    value = snap()
    text = monitor.render_report(value, report_id="old", now_at=NOW + 600)
    assert "Снимок устарел" in text
    assert "активных ошибок и блокировок не найдено" not in text
    value["editorial"]["heartbeat_at"] = NOW - 600
    assert "Свежий heartbeat обработчика не подтверждён" in report(value)
    assert "сервер выключен" not in report(value)


def test_intentional_pause_is_not_a_fault_and_does_not_trigger_empty_reserve_alert():
    value = snap()
    value["editorial"].update(paused=True, heartbeat_at=NOW - 10000)
    value["editorial"]["reserve"]["ready"] = 0
    assert monitor.alert_signatures(value) == {}
    text = report(value)
    assert "Редакция: пауза" in text
    assert "активных ошибок и блокировок не найдено" in text
    assert "Сервер работает" not in text


@pytest.mark.parametrize("paused,setting", [(False, "работа разрешена"), (True, "пауза")])
def test_permission_is_separate_from_stale_paused_phase_and_active_preparation(paused, setting):
    value = snap()
    value["editorial"].update(paused=paused, phase="paused", heartbeat_at=None)
    value["editorial"]["preparation"].update(phase="preparing", heartbeat_at=NOW - 2)
    before = copy.deepcopy(value)
    text = report(value)
    assert f"Редакция: {setting}. Последняя стадия: пауза. Heartbeat: нет данных." in text
    assert "Пополнение: подготовка." in text
    if not paused:
        assert "Редакция: пауза." not in text
        assert "Свежий heartbeat обработчика не подтверждён" in text
    assert value == before


def test_low_ready_pool_has_one_stable_alert_and_does_not_claim_per_channel_ready():
    value = snap()
    value["editorial"]["reserve"]["ready"] = 2
    original = monitor.alert_signatures(value)
    newer = copy.deepcopy(value)
    newer["collected_at"] += 20
    newer["editorial"]["heartbeat_at"] += 20
    assert monitor.alert_signatures(newer) == original
    assert len(original) == 1
    text = report(value)
    assert "2 quality_ready по учёту" in text
    assert "общий пул" in text and "для каждого канала отдельно не подтверждена" in text
    assert "2 статьи для каждого" not in text
    newer["editorial"]["reserve"]["ready"] = 6
    assert monitor.alert_signatures(newer) == {}


def test_unknown_older_than_new_success_still_alerts_until_reconciliation():
    old = dispatch("vk", ident="old-unknown", state="unknown", verified=False, due_at=NOW - 500)
    value = snap(old, dispatch("vk", ident="new-good"))
    alerts = monitor.alert_signatures(value)
    assert len(alerts) == 1
    assert "исход неизвестен" in next(iter(alerts.values()))
    value["routes"]["dispatches"][0]["state"] = "published"
    value["routes"]["dispatches"][0]["verified"] = True
    assert monitor.alert_signatures(value) == {}


def test_future_schedule_does_not_hide_previous_confirmed_hour_or_previous_api_receipt():
    value = snap(dispatch(), dispatch(ident="future", state="scheduled", verified=False,
                                     proof="unverified", due_at=NOW + 3600))
    text = report(value)
    assert "доставка подтверждена сохранённой независимой сверкой" in text
    assert "Следующий план:" in text
    value["routes"]["dispatches"][0].update(verified=False, evidence_kind="api_receipt")
    assert any("независимое подтверждение доставки отсутствует" in reason
               for reason in monitor.alert_signatures(value).values())


@pytest.mark.parametrize("pending_state", ["pending", "scheduled", "sending"])
def test_due_slot_without_confirmation_is_an_incident_after_five_minutes(pending_state):
    value = snap(dispatch(state=pending_state, verified=False, proof="unverified", due_at=NOW - 301))
    assert any("плановая дата прошла более 5 минут" in reason
               for reason in monitor.alert_signatures(value).values())
    value["routes"]["dispatches"][0]["due_at"] = NOW - 299
    assert monitor.alert_signatures(value) == {}


def test_daily_is_explicit_snapshot_without_fabricated_totals():
    text = report(snap(), kind="daily")
    assert "Ежедневный снимок состояния" in text
    assert "всего за сутки" not in text and "публикаций за сутки" not in text


def test_reserve_active_slots_show_target_and_are_not_counted_as_unassigned_ready():
    value = snap()
    value["editorial"]["reserve"]["active"] = [
        {"id": "slot-max", "target_id": "max", "state": "pending", "due_at": NOW + 60},
        {"id": "slot-vk", "target_id": "vk", "state": "blocked_unknown", "due_at": NOW - 60}]
    text = report(value)
    assert "Назначено из резерва: max: 1" in text
    assert "Слот vk: неизвестный исход занимает место; замена запрещена" in text
    assert "6 quality_ready" in text


@pytest.mark.parametrize("url", [
    "http://vk.com/wall1_2", "https://vk.com.evil.test/wall1_2", "https://token@vk.com/wall1_2",
    "https://vk.com:443/wall1_2", "https://vk.com/wall1_2?token=private", "https://vk.com/wall1_2#private",
    "https://vk.com/wall1_2\npassword=private", "javascript:alert(1)", "https://127.0.0.1/wall1_2",
    "https://vk.com/untrusted", "https://t.me/channel/4",
])
def test_links_cannot_point_off_platform_or_carry_credentials(url):
    assert url not in report(snap(dispatch("vk", permalink=url)))


@pytest.mark.parametrize("platform,url", [
    ("vk", "https://vk.com/wall-123_456"), ("telegram", "https://t.me/c/123/456"),
    ("pikabu", "https://pikabu.ru/story/test_12345"), ("max", "https://max.ru/channel/123")])
def test_valid_platform_links_are_preserved(platform, url):
    assert url in report(snap(dispatch(platform, permalink=url)))


def test_collect_allowlist_removes_raw_bodies_options_credentials_and_free_error_details(monkeypatch):
    secret = "private-secret-value-987654"
    monkeypatch.setenv("SOME_BOT_TOKEN", secret)
    editorial = state()
    editorial.update(raw={"secret": secret}, last_error={"code": "SOURCE_HTTP_429", "raw": secret})
    editorial["recent"] = [{"body": secret, "url": "https://private.test/" + secret}]
    inventory = routes()
    inventory["targets"][0].update(options={"password": secret}, binding_hash=secret)
    inventory["dispatches"] = [dispatch(title="Звёзды " + secret, body=secret, external_id=secret,
                                        error="private exception " + secret)]
    monkeypatch.setattr(monitor, "public_state", lambda *_: editorial)
    monkeypatch.setattr(monitor, "route_state", lambda *_: inventory)
    result = monitor.collect_snapshot(object(), store=object(), now_at=NOW)
    serialized = json.dumps(result, ensure_ascii=False)
    text = report(result)
    assert secret not in serialized + text
    assert "private exception" not in serialized + text
    assert "body" not in serialized and "options" not in serialized and "external_id" not in serialized
    assert "Звёзды [скрыто]" in text
    assert "Источник ограничил частоту запросов" in text


@pytest.mark.parametrize("title", ["token='private secret contains spaces'", "Bearer opaque-private-token",
                                    "api_key=opaque123", "https://u:password@host/private"])
def test_title_cannot_leak_credential_assignments(title):
    text = report(snap(dispatch(title=title)))
    assert "private" not in text and "opaque" not in text and "password" not in text


def test_control_characters_and_bidi_cannot_inject_status_lines():
    text = report(snap(dispatch(title="Звезда\nВСЁ ПОДТВЕРЖДЕНО\u202e")))
    assert "\nВСЁ ПОДТВЕРЖДЕНО" not in text
    assert "\u202e" not in text


def test_collect_unresolved_second_page_keeps_old_unknown_without_network_or_bootstrap(monkeypatch):
    normal = routes()
    normal["pagination"].update(dispatch_active_count=1, dispatch_count=100, dispatch_has_next=True)
    normal["dispatches"] = [dispatch(ident="recent")]
    unresolved = routes()
    unresolved["dispatches"] = [dispatch("vk", ident="old", state="unknown", verified=False)]
    calls = []
    def read(core, **kwargs):
        calls.append(kwargs)
        return unresolved if kwargs else normal
    core = SimpleNamespace()
    monkeypatch.setattr(monitor, "public_state", lambda *_: state())
    monkeypatch.setattr(monitor, "route_state", read)
    result = monitor.collect_snapshot(core, store=object(), now_at=NOW)
    assert calls == [{}, {"dispatch_filter": "unresolved"}]
    assert {row["id"] for row in result["routes"]["dispatches"]} == {"recent", "old"}
    assert result["routes"]["pagination"]["unresolved_complete"] is True
    assert "исход неизвестен" in " ".join(monitor.alert_signatures(result).values())
    assert vars(core) == {}


def test_incomplete_unknown_inventory_never_reports_no_blockers(monkeypatch):
    normal = routes()
    normal["pagination"].update(dispatch_active_count=100, dispatch_has_next=True)
    monkeypatch.setattr(monitor, "public_state", lambda *_: state())
    monkeypatch.setattr(monitor, "route_state", lambda *_, **__: normal)
    result = monitor.collect_snapshot(object(), store=object(), now_at=NOW)
    text = report(result)
    assert "Список неизвестных исходов неполон" in text
    assert "активных ошибок и блокировок не найдено" not in text


def test_reader_failure_produces_safe_partial_report_without_exception_contents(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://owner:private-password@localhost/private-db")
    monkeypatch.setattr(monitor, "public_state", fail)
    monkeypatch.setattr(monitor, "route_state", lambda *_: routes())
    value = monitor.collect_snapshot(object(), store=object(), now_at=NOW)
    text = report(value)
    assert value["editorial"]["available"] is False
    assert "состояние недоступно" in text
    assert "private" not in text + json.dumps(value)
    assert "Резерв: 0" not in text


def test_missing_runtime_is_not_bootstrapped_by_status_preview(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("reader must not create missing hourly runtime")
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(monitor, "public_state", forbidden)
    monkeypatch.setattr(monitor, "route_state", lambda *_: routes())
    result = monitor.collect_snapshot(core, store=object(), now_at=NOW)
    assert not list(tmp_path.iterdir())
    assert result["editorial"]["available"] is False
    assert result["routes"]["available"] is True


def test_real_isolated_collector_executes_selects_only_and_never_uses_transport(four_hub, monkeypatch):
    core = four_hub
    store = ServerState(core)
    store.initialize()
    directory = core.settings.root / "data" / "hourly-articles"
    directory.mkdir(parents=True, exist_ok=True)
    statements = []
    def read_only(connection, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT"), statement
        statements.append(statement)
    def no_network(*args, **kwargs):
        raise AssertionError("monitor must not call a transport")
    monkeypatch.setattr(core.http, "get", no_network)
    for publisher in core.fixture_publishers.values():
        monkeypatch.setattr(publisher, "inspect", no_network)
        monkeypatch.setattr(publisher, "publish", no_network)
        monkeypatch.setattr(publisher, "reconcile", no_network)
    event.listen(core.db.engine, "before_cursor_execute", read_only)
    try:
        result = monitor.collect_snapshot(core, store=store, now_at=NOW)
    finally:
        event.remove(core.db.engine, "before_cursor_execute", read_only)
    assert statements
    assert result["editorial"]["available"] is True
    assert result["routes"]["available"] is True
    assert result["editorial"]["paused"] is True
    assert len(result["routes"]["targets"]) == 4
    assert list(directory.iterdir()) == []


def test_huge_and_malformed_values_do_not_escape_report_or_claim_success():
    value = snap(dispatch(state=[], proof={}, title="a" * 100000))
    value["editorial"].update(phase={}, heartbeat_at=10**1000)
    value["routes"]["targets"][0]["platform"] = []
    value["routes"]["targets"][0]["destination"] = "https://u:private@host"
    text = report(value)
    assert "private" not in text
    assert "доставка подтверждена" not in text
    assert len(text) <= monitor.MAX_REPORT_CHARS


def test_max_message_is_bounded_even_for_large_inventory():
    value = snap()
    value["routes"]["targets"] = [{"id": f"target-{i}", "platform": "max", "destination": "x" * 80,
                                     "enabled": True, "paused": False} for i in range(100)]
    value["routes"]["dispatches"] = [dispatch(ident=f"post-{i}", target_id=f"target-{i}",
                                              title="Длинный заголовок " * 20, state="unknown")
                                         for i in range(100)]
    assert len(report(value)) <= monitor.MAX_REPORT_CHARS


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, 10**1000])
def test_invalid_time_is_rejected(bad):
    with pytest.raises(ValueError, match="MONITOR_TIME_INVALID"):
        monitor.render_report(snap(), report_id="report", now_at=bad)
