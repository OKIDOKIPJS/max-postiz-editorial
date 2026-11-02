"""Команды MAX: настоящие локальные claims, фиктивные внешние операции."""

import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
import httpx
from sqlalchemy import select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub import HubService
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, Variant
from max_postiz_agent.monitor_commands import CommandGateway, parse_command, refill_preflight, _refill_service_checks, _editorial_actor
from max_postiz_agent.monitor_command_manifest import COMMAND_SPECS, OWNER_ACTIONS
from max_postiz_agent.monitor_owner_chat import owner_contract, OWNER_CONTRACT_V1
from max_postiz_agent.server_state import ServerState
from max_postiz_agent.server_models import EditorialPreparationClaim, EditorialLedger


STAMP = 1804000000.0


@pytest.fixture
def gateway(service, monkeypatch):
    # Этот fixture использует собственную SQLite, а не PG соседних тестов.
    # Явный небезопасный URL, выставленный самим тестом позже, guard отклоняет.
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    mark_isolated(service.settings.root, service.settings.root.parent / "untouched-source")
    # Штатный монитор использует HubService и его настоящий runtime-generation
    # guard. Конфигурация fixture сохранена на диске до захвата поколения.
    publishing = {"schema_version": "1.0", "postiz": {}, "targets": [], "tasks": []}
    (service.settings.root / "config/publishing.json").write_text(json.dumps(publishing), encoding="utf-8")
    core = HubService(service.settings, service.db, editor=service.editor, publisher=service.publisher,
                      http=service.http, bootstrap_targets=False)
    state = ServerState(core)
    state.initialize()
    contract = {"messages": "[data-message-id]", "sender": "data-sender-id"}
    options = {"enabled": True, "control_chat_id": "-123", "owner_ids": ["321"],
               "self_ids": ["654"], "max_age_seconds": 300,
               "allowed_actions": ["status", "reserve", "errors", "sources", "refill", "pause",
                                   "reconcile", "unblock", "source_propose", "source_settings"],
               "dom_contract_verified": True, "dom_contract": contract,
               "dom_contract_sha256": fingerprint(contract)}
    try:
        yield CommandGateway(core, options, core.settings.root / "data/monitor/control.sqlite")
    finally:
        core.postiz.close()


def event(text="статус", **changes):
    return {"message_id": "message_1", "chat_id": "-123", "sender_id": "321", "created_at": STAMP - 1,
            "text": text, "reply_to_message_id": None, "forwarded": False, **changes}


def count_jobs(gateway):
    with sqlite3.connect(gateway.path) as connection:
        return connection.execute("SELECT count(*) FROM monitor_command_jobs").fetchone()[0]


@pytest.mark.parametrize(("text", "action"), [
    ("/статус", "status"), ("Покажи резерв", "reserve"), ("ошибки", "errors"),
    ("Покажи источники", "sources"), ("Пополни резерв до 6", "refill"),
    ("поставь проект на паузу", "pause"), ("пауза vk_test", "pause"),
    ("Проверь отправку dispatch_1", "reconcile"), ("Сними блокировку vk_test", "unblock"),
    ("Добавь источник https://example.org/science/", "source_propose"),
    ("Измени фильтр ключевые слова: космос, звезды", "source_settings"),
])
def test_bounded_russian_dictionary(text, action):
    assert parse_command(text)["action"] == action


@pytest.mark.parametrize("text", ["rm -rf /", "выполни python print(1)", "пополни резерв до 13",
    "пополни резерв до 0", "разреши все источники", "отправь сейчас", "сбрось unknown",
    "измени фильтр авторы: a;curl", "добавь источник https://localhost/a",
    "добавь источник https://127.0.0.1/a", "добавь источник https://example.org/a?token=secret",
    "добавь источник https://user:secret@example.org/a"])
def test_arbitrary_execution_source_secrets_and_oversized_requests_denied(text):
    with pytest.raises(AgentError):
        parse_command(text)


@pytest.mark.parametrize("change", [
    {"sender_id": "999"}, {"sender_id": "654"}, {"chat_id": "-999"}, {"forwarded": True},
    {"forwarded": None}, {"message_id": ""}, {"message_id": "../message"},
    {"created_at": STAMP - 301}, {"created_at": STAMP + 1}, {"created_at": float("nan")},
    {"created_at": True}, {"reply_to_message_id": "../../message"},
])
def test_unauthorized_events_never_create_claim(gateway, change):
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACCESS_DENIED"):
        gateway.handle(event(**change), now_at=STAMP)
    assert count_jobs(gateway) == 0


def test_explicit_database_override_after_sqlite_fixture_is_still_rejected(gateway, monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://fixture:synthetic@127.0.0.1:55450/editorial_hub_tests")
    with pytest.raises(AgentError, match="ISOLATED_DATABASE_REQUIRED"):
        gateway.handle(event(), now_at=STAMP)
    assert count_jobs(gateway) == 0


def test_missing_or_changed_live_dom_acceptance_denies_commands(gateway):
    gateway.options["dom_contract"]["sender"] = "name-only"
    with pytest.raises(AgentError, match="MONITOR_COMMANDS_NOT_VERIFIED"):
        gateway.handle(event(), now_at=STAMP)
    assert count_jobs(gateway) == 0


def test_action_permission_denied_before_claim(gateway):
    gateway.options["allowed_actions"] = ["status"]
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACTION_DENIED"):
        gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    assert count_jobs(gateway) == 0


def test_duplicate_command_does_not_execute_twice_and_changed_event_denied(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "_execute", lambda *args: calls.append(args) or {"message": "готово"})
    first = gateway.handle(event(), now_at=STAMP)
    second = gateway.handle(event(), now_at=STAMP)
    assert first["command_id"] == second["command_id"] and second["duplicate"] is True
    assert second["message"] == "готово" and len(calls) == 1
    with pytest.raises(AgentError, match="MONITOR_COMMAND_EVENT_CHANGED"):
        gateway.handle(event("резерв"), now_at=STAMP)
    assert len(calls) == 1 and count_jobs(gateway) == 1


def test_claim_is_committed_before_mutation_and_lost_result_is_not_retried(gateway, monkeypatch):
    calls = []

    def interrupted(*args):
        with sqlite3.connect(gateway.path) as connection:
            assert connection.execute("SELECT state FROM monitor_command_jobs").fetchone()[0] == "running"
        calls.append(args)
        raise RuntimeError("private-password-must-not-leak")

    monkeypatch.setattr(gateway, "_execute", interrupted)
    result = gateway.handle(event("пауза"), now_at=STAMP)
    assert result["state"] == "unknown" and "private-password" not in json.dumps(result)
    restarted = CommandGateway(gateway.core, gateway.options, gateway.path)
    assert restarted.handle(event("пауза"), now_at=STAMP)["state"] == "unknown"
    assert len(calls) == 1


def test_running_command_survives_restart_as_unresolved_without_new_preparation(gateway, monkeypatch):
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    with sqlite3.connect(gateway.path) as connection:
        connection.execute("UPDATE monitor_command_jobs SET state='running'")
    restarted = CommandGateway(gateway.core, gateway.options, gateway.path)
    monkeypatch.setattr(restarted, "_refill", lambda *_: pytest.fail("unknown command repeated"))
    result = restarted.run_one(now_at=STAMP)
    assert result["state"] == "unknown" and result["command_id"] == accepted["command_id"]
    restarted.handle(event("пополни резерв до 3", message_id="new"), now_at=STAMP)
    assert restarted.run_one(now_at=STAMP)["state"] == "unknown"


def test_refill_is_queued_then_runs_bounded_passes_to_goal(gateway, monkeypatch):
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    counts = iter([3, 6])
    calls = []

    def prepare(desired, _):
        calls.append(desired)
        return {"phase": "reserve_refilled", "ready": next(counts), "message": "подготовлено"}

    monkeypatch.setattr(gateway, "_refill", prepare)
    assert accepted["state"] == "queued" and calls == []
    assert gateway.run_one(now_at=STAMP)["state"] == "queued"
    assert gateway.run_one(now_at=STAMP)["state"] == "idle"
    assert gateway.run_one(now_at=STAMP + 31)["state"] == "done"
    assert gateway.run_one(now_at=STAMP + 62)["state"] == "idle"
    assert calls == [6, 6]


def test_busy_worker_leaves_refill_queued_without_consuming_pass(gateway, monkeypatch):
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    monkeypatch.setattr(gateway, "_refill", lambda *_: {"phase": "worker_busy", "message": "занято"})
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "queued" and result["phase"] == "worker_busy"
    with sqlite3.connect(gateway.path) as connection:
        assert connection.execute("SELECT passes FROM monitor_command_jobs").fetchone()[0] == 0


def test_unknown_preparation_does_not_start_again(gateway, monkeypatch):
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    calls = []

    def uncertain(*_):
        calls.append(True)
        raise OSError("private-dsn")

    monkeypatch.setattr(gateway, "_refill", uncertain)
    first = gateway.run_one(now_at=STAMP)
    assert first["state"] == "unknown" and "private-dsn" not in json.dumps(first)
    assert gateway.run_one(now_at=STAMP + 9999)["state"] == "unknown" and calls == [True]


def test_command_configuration_change_revokes_queued_refill(gateway, monkeypatch):
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    gateway.options["max_age_seconds"] = 90
    monkeypatch.setattr(gateway, "_refill", lambda *_: pytest.fail("revoked command reached models"))
    assert gateway.run_one(now_at=STAMP)["code"] == "MONITOR_COMMAND_CONFIG_CHANGED"


def test_paused_policy_does_not_resume_and_disabled_reserve_is_not_enabled(gateway):
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    result = gateway.run_one(now_at=STAMP)
    assert result["command_id"] == accepted["command_id"] and result["phase"] == "paused"
    state = ServerState(gateway.core)
    assert state.read()["paused"] is True
    state.control("start", state.read()["revision"], "fixture")
    gateway.handle(event("пополни резерв до 6", message_id="next"), now_at=STAMP)
    assert gateway.run_one(now_at=STAMP)["phase"] == "reserve_disabled"
    assert state.read()["settings"]["reserve"]["enabled"] is False


def test_preparation_pause_retains_queue_and_explicit_resume_preserves_goal(gateway, monkeypatch):
    gateway.options["allowed_actions"].append("resume_refill")
    first = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    result = gateway.handle(event("пауза подготовки", message_id="pause"), now_at=STAMP)
    assert "Общий серверный цикл не менялся" in result["message"]
    assert gateway.run_one(now_at=STAMP)["state"] == "paused"
    with sqlite3.connect(gateway.path) as connection:
        assert connection.execute("SELECT state FROM monitor_command_jobs WHERE id=?", (first["command_id"],)).fetchone()[0] == "queued"
    gateway.handle(event("продолжи подготовку", message_id="resume"), now_at=STAMP)
    monkeypatch.setattr(gateway, "_refill", lambda *_: {"phase": "reserve_satisfied", "ready": 6})
    assert gateway.run_one(now_at=STAMP)["state"] == "done"


def test_global_pause_calls_existing_revision_control(gateway):
    state = ServerState(gateway.core)
    state.control("start", state.read()["revision"], "fixture")
    before = state.read()["revision"]
    result = gateway.handle(event("поставь проект на паузу"), now_at=STAMP)
    assert result["paused"] is True and state.read()["revision"] == before + 1


def test_source_candidate_stays_pending_rights_and_does_not_change_sources(gateway):
    before = copy.deepcopy(gateway.core.settings.sources)
    result = gateway.handle(event("добавь источник https://example.org/science/"), now_at=STAMP)
    assert result["proposal_state"] == "pending_rights" and gateway.core.settings.sources == before


def test_filter_proposal_preview_and_exact_confirmation_applies_existing_settings(gateway):
    state = ServerState(gateway.core)
    before = state.read()
    proposed = gateway.handle(event("измени фильтр ключевые слова: космос, звезды"), now_at=STAMP)
    assert proposed["proposal_state"] == "awaiting_confirmation" and proposed["before"] == []
    assert state.read() == before
    result = gateway.handle(event("подтверждаю " + proposed["proposal_id"], message_id="confirm"), now_at=STAMP + 1)
    assert result.get("proposal_state") == "applied", result
    after = state.read()
    assert after["revision"] == before["revision"] + 1 and after["paused"] is True
    assert after["settings"]["nasa"]["keywords"] == ["космос", "звезды"]
    assert after["settings"]["reserve"] == before["settings"]["reserve"]


def test_filter_confirmation_cannot_apply_after_another_settings_revision(gateway):
    proposed = gateway.handle(event("измени фильтр авторы: NASA"), now_at=STAMP)
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["nasa"]["keywords"] = ["чужая правка"]
    state.settings(settings, current["revision"], "other-owner")
    result = gateway.handle(event("подтверждаю " + proposed["proposal_id"], message_id="confirm"), now_at=STAMP + 1)
    assert result["state"] == "blocked" and result["code"] == "EDITORIAL_REVISION_CONFLICT"
    assert state.read()["settings"]["nasa"]["authors"] == []


def test_filter_confirmation_expires_and_requires_same_owner(gateway):
    proposed = gateway.handle(event("измени фильтр авторы: NASA"), now_at=STAMP)
    gateway.options["owner_ids"].append("111")
    other = gateway.handle(event("подтверждаю " + proposed["proposal_id"], message_id="other", sender_id="111"), now_at=STAMP)
    assert other["code"] == "MONITOR_PROPOSAL_INVALID"
    expired = gateway.handle(event("подтверждаю " + proposed["proposal_id"], message_id="expired", created_at=STAMP + 601), now_at=STAMP + 601)
    assert expired["code"] == "MONITOR_PROPOSAL_INVALID"


@pytest.mark.parametrize("late_blocker", ["selecting", "preparing", "blocked_unknown", "failed", "dispatch_unknown", "mutation"])
def test_filter_atomic_setter_blocks_conflict_after_precheck_and_preserves_proposal_audit(gateway, monkeypatch, late_blocker):
    state = ServerState(gateway.core)
    proposed = gateway.handle(event("измени фильтр авторы: NASA"), now_at=STAMP)
    before = state.read()
    with gateway.core.db.session() as session:
        audit_before = list(session.scalars(select(Audit.id)))
    setter = ServerState.settings
    calls = []

    def conflict_after_precheck(current_state, settings, revision, actor, *, require_quiescent=False):
        calls.append(require_quiescent)
        assert require_quiescent is True
        if late_blocker in {"dispatch_unknown", "mutation"}:
            insert_unresolved(gateway, mutation=late_blocker == "mutation")
        else:
            with gateway.core.db.session.begin() as session:
                session.add(EditorialPreparationClaim(id="late-preparation", token="late-token",
                    control_revision=before["revision"], state=late_blocker,
                    active_key="models" if late_blocker in {"selecting", "preparing"} else None))
        return setter(current_state, settings, revision, actor, require_quiescent=require_quiescent)

    monkeypatch.setattr(ServerState, "settings", conflict_after_precheck)
    confirmation = event("подтверждаю " + proposed["proposal_id"], message_id="late_confirm")
    result = gateway.handle(confirmation, now_at=STAMP + 1)
    assert result["state"] == "blocked" and result["code"] == "ARTICLE_RECONCILIATION_REQUIRED"
    assert state.read() == before
    assert calls == [True]
    with gateway.core.db.session() as session:
        assert list(session.scalars(select(Audit.id))) == audit_before
        if late_blocker not in {"dispatch_unknown", "mutation"}:
            claim = session.get(EditorialPreparationClaim, "late-preparation")
            assert claim.state == late_blocker
            assert claim.active_key == ("models" if late_blocker in {"selecting", "preparing"} else None)
    with gateway._connection() as connection:
        assert connection.execute("SELECT state FROM monitor_source_proposals WHERE id=?",
                                  (proposed["proposal_id"],)).fetchone()[0] == "blocked"
    duplicate = gateway.handle(confirmation, now_at=STAMP + 2)
    assert duplicate["duplicate"] is True and duplicate["message"] == result["message"] and calls == [True]


def test_sources_command_lists_configured_rights_without_enabling_anything(gateway):
    before = copy.deepcopy(gateway.core.settings.sources)
    result = gateway.handle(event("источники"), now_at=STAMP)
    assert result["sources"] and gateway.core.settings.sources == before
    assert "не новая проверка лицензий" in result["message"]


def test_real_refill_uses_preparation_only_with_manual_refilling_and_no_settings_write(gateway, monkeypatch):
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["reserve"]["enabled"] = True
    state.settings(settings, current["revision"], "fixture")
    state.control("start", state.read()["revision"], "fixture")
    before = state.read()
    records = [{"ready": True} for _ in range(4)]
    cycle = SimpleNamespace(_policy=lambda: {"approval_mode": "automatic"}, _load=lambda: {"records": records})
    calls = []
    monkeypatch.setattr("max_postiz_agent.monitor_commands.refill_preflight", lambda *_a, **_k: {"ready": True})

    class Controller:
        def __init__(self, core, *, state, cycle, clock):
            self.state, self.cycle = state, cycle

        def prepare_once(self):
            assert self._refilling is True
            options = self.state.read()["settings"]["reserve"]
            assert options["target"] == 6 and options["batch"] <= 3
            self.state.assert_running()
            records.extend([{"ready": True}, {"ready": True}])
            calls.append("prepare_only")
            return {"phase": "reserve_refilled", "source_url": "private-runtime-detail"}

    monkeypatch.setattr("max_postiz_agent.monitor_commands.HourlyArticleCycle", lambda *_args, **_kwargs: cycle)
    monkeypatch.setattr("max_postiz_agent.monitor_commands.ReserveServerCycle", Controller)
    monkeypatch.setattr("max_postiz_agent.monitor_commands.ready_for_policy", lambda record, _: record["ready"])
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "done" and result["ready_before"] == 4 and result["ready"] == 6
    assert calls == ["prepare_only"] and state.read() == before
    assert "private-runtime-detail" not in json.dumps(result)


def test_existing_server_leader_fence_is_verified_instead_of_competing_lock(gateway, monkeypatch):
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["reserve"]["enabled"] = True
    state.settings(settings, current["revision"], "fixture")
    state.control("start", state.read()["revision"], "fixture")
    calls = []
    monkeypatch.setattr("max_postiz_agent.monitor_commands.refill_preflight", lambda *_a, **_k: {"ready": True})
    gateway.core.editorial_leader_check = lambda: calls.append("fence")
    cycle = SimpleNamespace(_policy=lambda: {}, _load=lambda: {"records": []})
    monkeypatch.setattr("max_postiz_agent.monitor_commands.HourlyArticleCycle", lambda *_args, **_kwargs: cycle)
    monkeypatch.setattr(ServerState, "worker_lock", lambda *_: pytest.fail("leader lock acquired twice"))
    monkeypatch.setattr("max_postiz_agent.monitor_commands.ReserveServerCycle", lambda *_args, **_kwargs:
        SimpleNamespace(prepare_once=lambda: {"phase": "blocked"}))
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    assert gateway.run_one(now_at=STAMP, worker_owned=True)["state"] == "blocked" and calls == ["fence"]


def test_bounded_source_pass_budget_stops_known_nonprogress(gateway, monkeypatch):
    gateway.handle(event("пополни резерв до 12"), now_at=STAMP)
    monkeypatch.setattr(gateway, "_refill", lambda *_: {"phase": "no_source", "ready": 0})
    for step in range(3):
        assert gateway.run_one(now_at=STAMP + step * 31)["state"] == "queued"
    result = gateway.run_one(now_at=STAMP + 93)
    assert result["state"] == "blocked" and result["goal_reached"] is False
    assert "Цель не достигнута" in result["message"]


def test_ledger_outside_ignored_monitor_directory_is_rejected(gateway, tmp_path):
    with pytest.raises(AgentError, match="MONITOR_COMMAND_LEDGER_INVALID"):
        CommandGateway(gateway.core, gateway.options, tmp_path / "elsewhere.sqlite")


def test_dom_message_ids_with_dots_colons_and_long_values_are_preserved(gateway):
    value = "msg.part:" + "x" * 150
    result = gateway.handle(event("источники", message_id=value, reply_to_message_id="parent:part.1"), now_at=STAMP)
    assert result["state"] == "done"


@pytest.mark.parametrize("changes", [{"sender_id": 321}, {"chat_id": -123}, {"message_id": 123},
                                    {"reply_to_message_id": 123}])
def test_numeric_coercion_is_not_authentication(gateway, changes):
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACCESS_DENIED"):
        gateway.handle(event(**changes), now_at=STAMP)


def test_real_worker_file_lock_busy_keeps_request_queued(gateway, monkeypatch):
    from max_postiz_agent.browser_runtime import profile_lock
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["reserve"]["enabled"] = True
    state.settings(settings, current["revision"], "fixture")
    state.control("start", state.read()["revision"], "fixture")
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    monkeypatch.setattr("max_postiz_agent.monitor_commands.HourlyArticleCycle", lambda *_a, **_k: pytest.fail("busy lock reached models"))
    with profile_lock(gateway.core.settings.root / "data/editorial-worker.lock"):
        result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "queued" and result["phase"] == "worker_busy"
    with sqlite3.connect(gateway.path) as connection:
        assert connection.execute("SELECT passes FROM monitor_command_jobs").fetchone()[0] == 0


def test_unknown_model_agent_error_blocks_following_refill(gateway, monkeypatch):
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    def unknown(*_):
        raise AgentError("MODEL_REQUEST_TIMEOUT_PRIVATE_CONTEXT", 503)
    monkeypatch.setattr(gateway, "_refill", unknown)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "unknown" and "PRIVATE_CONTEXT" not in json.dumps(result)
    gateway.handle(event("пополни резерв до 6", message_id="next"), now_at=STAMP)
    assert gateway.run_one(now_at=STAMP)["state"] == "unknown"


def insert_unresolved(gateway, *, mutation=False):
    with gateway.core.db.session.begin() as session:
        session.add(Target(id="test", profile_id="main", platform="max", route="max_browser",
                           destination="-999", enabled=True, paused=True, binding_hash="a" * 64))
        session.add(Variant(id="variant", draft_id="missing-draft", target_id="test", revision=1,
            body="fixture", source_revision="1", config_hash="a" * 64, binding_hash="b" * 64))
        if mutation:
            session.add(BridgePost(post_id="post", integration_id="integration", group_id="group",
                target_id="test", variant_id="variant", remote_hash="c" * 64, remote_state="DRAFT",
                due_at=STAMP, mutation_owner="owner", mutation_until=STAMP + 100))
        else:
            session.add(Dispatch(id="dispatch", variant_id="variant", approval_id="approval", revision=1,
                snapshot_hash="d" * 64, snapshot={"target_id": "test"}, due_at=STAMP,
                state="unknown", owner="owner", lease_until=STAMP + 100))


@pytest.mark.parametrize("mutation", [False, True])
def test_unknown_or_mutation_blocks_source_edit_and_unblock_preserves_state(gateway, mutation):
    insert_unresolved(gateway, mutation=mutation)
    proposal = gateway.handle(event("измени фильтр авторы: NASA"), now_at=STAMP)
    result = gateway.handle(event("подтверждаю " + proposal["proposal_id"], message_id="confirm"), now_at=STAMP)
    assert result["state"] == "blocked" and result["code"] == "ARTICLE_RECONCILIATION_REQUIRED"
    diagnosis = gateway.handle(event("сними блокировку test", message_id="unblock"), now_at=STAMP)
    assert diagnosis["state"] == "blocked" and diagnosis["changed"] is False and diagnosis["problems"]
    with gateway.core.db.session() as session:
        assert session.get(Target, "test").paused is True
        if mutation:
            assert session.get(BridgePost, "post").mutation_owner == "owner"
        else:
            assert session.get(Dispatch, "dispatch").state == "unknown"


def test_reconcile_calls_only_saved_attempt_reconciler_once(gateway, monkeypatch):
    calls = []
    def reconcile(core, ident, actor):
        calls.append((core, ident, actor))
        return {"state": "published", "verified": False, "external_mutations": 0,
                "external_id": "private-provider-detail"}
    monkeypatch.setattr("max_postiz_agent.platform_management.reconcile", reconcile)
    result = gateway.handle(event("сверь dispatch_1"), now_at=STAMP)
    assert result["reconciliation_state"] == "published" and "private-provider" not in json.dumps(result)
    gateway.handle(event("сверь dispatch_1"), now_at=STAMP)
    assert len(calls) == 1


def test_missing_isolation_marker_blocks_commands_even_with_valid_owners(gateway):
    marker = gateway.core.settings.root / "data/four-platform/isolation.json"
    marker.unlink()
    with pytest.raises(AgentError, match="ISOLATED_COPY_REQUIRED"):
        gateway.handle(event("пауза"), now_at=STAMP)


def test_live_refill_preflight_lists_all_current_blockers_without_network_or_claims(gateway, monkeypatch):
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("preflight attempted network"))
    result = refill_preflight(gateway.core, check_services=True)
    codes = {value["code"] for value in result["blockers"]}
    assert {"EDITORIAL_PAUSED", "MONITOR_RESERVE_DISABLED", "MONITOR_LIVE_REVIEW_REQUIRED",
        "MONITOR_OPENCODE_ENDPOINT_REQUIRED", "MONITOR_MADLAD_CONFIG_REQUIRED", "MONITOR_POLICY_REQUIRED",
        "MONITOR_SOURCE_RIGHTS_REQUIRED", "MONITOR_TARGET_CONFIGURATION_REQUIRED"} <= codes
    assert result["ready"] is result["inference_verified"] is result["service_readiness_checked"] is False
    assert result["external_mutations"] == 0
    with gateway.core.db.session() as session:
        assert session.query(Dispatch).count() == 0
    assert ServerState(gateway.core).read()["paused"] is True


def test_paused_live_command_returns_exact_missing_dependencies_without_fake_success(gateway, monkeypatch):
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("blocked command attempted network"))
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "blocked" and result["code"] == "MONITOR_LIVE_REFILL_NOT_READY"
    assert result["inference_verified"] is False
    assert "fake" in result["message"] and "MADLAD" in result["message"] and "OpenCode" in result["message"]
    assert "Задание завершено" not in result["message"] and "Готово: 6" not in result["message"]


def test_unpaused_copy_with_fake_models_does_not_reach_controller(gateway, monkeypatch):
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["reserve"]["enabled"] = True
    state.settings(settings, current["revision"], "fixture")
    state.control("start", state.read()["revision"], "fixture")
    monkeypatch.setattr("max_postiz_agent.monitor_commands.ReserveServerCycle", lambda *_a, **_k:
                        pytest.fail("fake configuration reached preparation"))
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("invalid setup reached service health"))
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "blocked" and result["phase"] == "blocked"
    assert any(value["code"] == "MONITOR_LIVE_REVIEW_REQUIRED" for value in result["blockers"])


def test_preflight_rejects_old_madlad_alias_and_secrets_in_endpoint(gateway, monkeypatch):
    path = gateway.core.settings.root / "config/translation.json"
    for value in [
        {"enabled": True, "model": "madlad400-3b-q4k", "base_url": "http://127.0.0.1:8094"},
        {"enabled": True, "model": "madlad400-3b-q4k-causal-v2", "base_url": "http://user:private@127.0.0.1:8094"},
        {"enabled": True, "model": "madlad400-3b-q4k-causal-v2", "base_url": "http://10.20.30.45:8094"},
        {"enabled": True, "model": "madlad400-3b-q4k-causal-v2", "base_url": "http://127.0.0.1:8094", "segment_chars": True},
    ]:
        path.write_text(json.dumps(value), encoding="utf-8")
        result = refill_preflight(gateway.core)
        assert any(row["code"] == "MONITOR_MADLAD_CONFIG_REQUIRED" for row in result["blockers"])
        assert "private" not in json.dumps(result)
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://10.20.30.45:4108")
    result = refill_preflight(gateway.core)
    assert any(row["code"] == "MONITOR_OPENCODE_ENDPOINT_REQUIRED" for row in result["blockers"])


@pytest.fixture
def outgoing_gateway(gateway):
    account = "a" * 64
    contract = owner_contract(version=OWNER_CONTRACT_V1)
    gateway.options.update(auth_mode="authenticated_account_outgoing", account_sha256=account,
        principal="max-account-sha256:" + account, owner_ids=[], allowed_actions=["status", "reserve", "refill"],
        dom_contract=contract, dom_contract_sha256=fingerprint(contract))
    return gateway


def outgoing_event(gateway, text="статус", **changes):
    result = {"auth_mode": "authenticated_account_outgoing", "principal": gateway.options["principal"],
        "account_sha256": gateway.options["account_sha256"], "outgoing": True, "forwarded": False,
        "quoted": False, "edited": False, "id_kind": "content_minute_fingerprint",
        "dom_contract_sha256": gateway.options["dom_contract_sha256"], "chat_id": "-123",
        "created_at": int(STAMP // 60) * 60, "created_at_precision_seconds": 60,
        "text": text, "reply_to_message_id": None, "observed_at": STAMP - 1, **changes}
    result["message_id"] = "owner-" + fingerprint({"contract": "max-owner-chat-event-v1",
        "chat_id": result["chat_id"], "principal": result["principal"],
        "created_at": result["created_at"], "text": result["text"]})
    return result


def test_outgoing_account_principal_accepts_fresh_exact_command_without_fabricated_sender(outgoing_gateway, monkeypatch):
    gateway = outgoing_gateway
    calls = []
    monkeypatch.setattr(gateway, "_execute", lambda payload, actor, chat, stamp:
        calls.append((actor, chat, payload)) or {"message": "Состояние прочитано"})
    received = outgoing_event(gateway)
    first = gateway.handle(received, now_at=STAMP)
    received["observed_at"] = STAMP
    second = gateway.handle(received, now_at=STAMP)
    assert first["state"] == "done" and second["duplicate"] is True
    assert calls == [(gateway.options["principal"], "-123", {"action": "status"})]
    assert "sender_id" not in received


@pytest.mark.parametrize("changes", [
    {"auth_mode": "numeric_sender"}, {"account_sha256": "b" * 64}, {"principal": "max-account-sha256:" + "b" * 64},
    {"outgoing": False}, {"forwarded": True}, {"quoted": True}, {"edited": True}, {"edited": None},
    {"id_kind": "native_message_id"}, {"dom_contract_sha256": "b" * 64}, {"chat_id": "-999"},
    {"created_at": int(STAMP // 60) * 60 + 1}, {"created_at_precision_seconds": 60.0},
    {"created_at_precision_seconds": 1}, {"observed_at": STAMP + 1}, {"observed_at": float("nan")},
    {"reply_to_message_id": "a-quote"}, {"sender_id": "321"},
])
def test_outgoing_mode_rejects_unproven_identity_or_ambiguous_rendered_messages(outgoing_gateway, changes):
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACCESS_DENIED"):
        outgoing_gateway.handle(outgoing_event(outgoing_gateway, **changes), now_at=STAMP)
    assert count_jobs(outgoing_gateway) == 0


def test_outgoing_mode_rejects_missing_edit_evidence_and_forged_mid(outgoing_gateway):
    received = outgoing_event(outgoing_gateway)
    del received["edited"]
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACCESS_DENIED"):
        outgoing_gateway.handle(received, now_at=STAMP)
    received = outgoing_event(outgoing_gateway)
    received["message_id"] = "owner-" + "b" * 64
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACCESS_DENIED"):
        outgoing_gateway.handle(received, now_at=STAMP)


def test_one_mutating_command_per_account_chat_rendered_minute_survives_restart(outgoing_gateway):
    gateway = outgoing_gateway
    first = gateway.handle(outgoing_event(gateway, "пополни резерв до 6"), now_at=STAMP)
    assert first["state"] == "queued"
    restarted = CommandGateway(gateway.core, gateway.options, gateway.path)
    blocked = restarted.handle(outgoing_event(gateway, "пополни резерв до 3"), now_at=STAMP)
    assert blocked["state"] == "blocked" and blocked["code"] == "MONITOR_COMMAND_MINUTE_ALREADY_USED"
    duplicate = restarted.handle(outgoing_event(gateway, "пополни резерв до 3"), now_at=STAMP)
    assert duplicate["command_id"] == blocked["command_id"] and duplicate["duplicate"] is True
    assert "следующую минуту" in blocked["message"] and count_jobs(gateway) == 2
    with gateway._connection() as connection:
        assert connection.execute("SELECT count(*) FROM monitor_command_mutation_minutes").fetchone()[0] == 1
    assert restarted.handle(outgoing_event(gateway, "резерв"), now_at=STAMP)["state"] == "done"


def test_outgoing_refill_queue_uses_principal_not_numeric_owner_list(outgoing_gateway, monkeypatch):
    gateway = outgoing_gateway
    gateway.handle(outgoing_event(gateway, "пополни резерв до 6"), now_at=STAMP)
    monkeypatch.setattr(gateway, "_refill", lambda *_: {"phase": "paused", "message": "Копия на паузе."})
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "blocked" and result["phase"] == "paused"
    with sqlite3.connect(gateway.path) as connection:
        assert connection.execute("SELECT actor FROM monitor_command_jobs").fetchone()[0] == gateway.options["principal"]


def test_old_three_action_outgoing_configuration_still_denies_unlisted_management(outgoing_gateway):
    gateway = outgoing_gateway
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACTION_DENIED"):
        gateway.handle(outgoing_event(gateway, "пауза"), now_at=STAMP)
    assert count_jobs(gateway) == 0


@pytest.mark.parametrize(("change", "code"), [
    (None, None), ("wrong_backend", "MONITOR_MADLAD_UNAVAILABLE"),
    ("wrong_alias", "MONITOR_MADLAD_UNAVAILABLE"), ("artifacts_unverified", "MONITOR_MADLAD_UNAVAILABLE"),
    ("quarantined", "MONITOR_MADLAD_UNAVAILABLE"), ("busy", "MONITOR_MADLAD_BUSY"),
    ("lt_no_russian", "MONITOR_LANGUAGETOOL_UNAVAILABLE"),
    ("opencode_version", "MONITOR_OPENCODE_UNAVAILABLE"),
])
def test_readiness_uses_only_fixed_gets_and_does_not_claim_inference(gateway, monkeypatch, change, code):
    calls = []
    required = gateway.core.settings.app["opencode"]["required_version"]
    class Reviewer:
        def __init__(self, settings):
            self.client = SimpleNamespace(close=lambda: None)
        def probe(self):
            return {"state": "available", "reported_version": "wrong" if change == "opencode_version" else required}
    monkeypatch.setattr("max_postiz_agent.adapters.OpenCode", Reviewer)
    health = {"model": "madlad400-3b-q4k-causal-v2", "backend_contract": "madlad400-candle-causal-v2",
        "artifacts_verified": True, "quarantined": False, "busy": False, "status": "configured",
        "inference_verified": False}
    if change == "wrong_backend":
        health["backend_contract"] = "obsolete-backend"
    elif change == "wrong_alias":
        health["model"] = "madlad400-3b-q4k"
    elif change == "artifacts_unverified":
        health["artifacts_verified"] = False
    elif change == "quarantined":
        health["quarantined"] = True
    elif change == "busy":
        health["busy"] = True
    languages = [{"code": "en" if change == "lt_no_russian" else "ru"}]
    def respond(request):
        calls.append((request.method, str(request.url)))
        assert request.method == "GET" and request.url.host == "127.0.0.1"
        if request.url.path == "/health":
            return httpx.Response(200, json=health)
        assert request.url.path == "/v2/languages"
        return httpx.Response(200, json=languages)
    client = httpx.Client
    monkeypatch.setattr("max_postiz_agent.monitor_commands.httpx.Client", lambda **kwargs:
        client(**kwargs, transport=httpx.MockTransport(respond)))
    errors = _refill_service_checks(gateway.core, "http://127.0.0.1:18099")
    assert errors == ([] if code is None else [code])
    assert calls == [("GET", "http://127.0.0.1:18099/health"), ("GET", "http://127.0.0.1:8010/v2/languages")]


def test_fully_configured_preflight_is_still_not_live_inference_or_rights_verification(gateway, monkeypatch):
    root = gateway.core.settings.root
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["reserve"]["enabled"] = True
    state.settings(settings, current["revision"], "fixture")
    state.control("start", state.read()["revision"], "fixture")
    gateway.core.settings.app["opencode"]["adapter_mode"] = "live"
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4108")
    policy = {"enabled": True, "approval_mode": "automatic", "review_rubric": "hourly-practical-v1",
        "target_id": "max_test", "source_id": "nasa_svs_articles", "allowed_hosts": ["svs.gsfc.nasa.gov"],
        "rights_evidence": "https://svs.gsfc.nasa.gov/help/", "owner_instruction": "Fixture preparation only; no editorial sends",
        "min_gap_seconds": 3600, "max_per_day": 24, "max_source_age_hours": 8760}
    (root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy), encoding="utf-8")
    (root / "config/translation.json").write_text(json.dumps({"enabled": True,
        "model": "madlad400-3b-q4k-causal-v2", "base_url": "http://127.0.0.1:18099"}), encoding="utf-8")
    source = copy.deepcopy(gateway.core.settings.sources["sources"][0])
    source.update(id="nasa_svs_articles", enabled=True, channel_ids=["main"])
    source["rights"].update(verified=True, allow_storage=True, allow_derivative=True,
                            evidence_ref=policy["rights_evidence"])
    gateway.core.settings.sources["sources"].append(source)
    gateway.core.settings.channel("main")["publication"]["mode"] = "review"
    with gateway.core.db.session.begin() as session:
        session.add(Target(id="max_test", profile_id="main", platform="max", route="max_browser", destination="-999",
            enabled=True, paused=True, binding_hash="a" * 64, options={"postiz_integration_id": "fixture-integration"}))
    monkeypatch.setattr(gateway.core, "bridge", SimpleNamespace(enabled=True), raising=False)
    monkeypatch.setattr(gateway.core, "_target", lambda session, ident: session.get(Target, ident), raising=False)
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("pure preflight requested network"))
    result = refill_preflight(gateway.core)
    assert result["ready"] is True and result["service_readiness_checked"] is False
    assert result["inference_verified"] is result["source_rights_live_verified"] is False
    checks = []
    monkeypatch.setattr("max_postiz_agent.monitor_commands._refill_service_checks", lambda core, url:
        checks.append(url) or ["MONITOR_LANGUAGETOOL_UNAVAILABLE"])
    checked = refill_preflight(gateway.core, check_services=True)
    assert checked["ready"] is False and checked["service_readiness_checked"] is True
    assert checked["blockers"] == [{"code": "MONITOR_LANGUAGETOOL_UNAVAILABLE",
        "message": "Локальный LanguageTool на 127.0.0.1:8010 не подтвердил русский язык."}]
    assert checks == ["http://127.0.0.1:18099"]
    state.control("pause", state.read()["revision"], "fixture")
    paused = refill_preflight(gateway.core, check_services=True, diagnostic_services=True)
    assert paused["service_readiness_checked"] is True
    assert {value["code"] for value in paused["blockers"]} == {"EDITORIAL_PAUSED", "MONITOR_LANGUAGETOOL_UNAVAILABLE"}
    assert checks == ["http://127.0.0.1:18099"] * 2


def test_busy_shared_translator_queues_without_model_claim_or_consumed_pass(gateway, monkeypatch):
    state = ServerState(gateway.core)
    current = state.read()
    settings = current["settings"]
    settings["reserve"]["enabled"] = True
    state.settings(settings, current["revision"], "fixture")
    state.control("start", state.read()["revision"], "fixture")
    monkeypatch.setattr("max_postiz_agent.monitor_commands.refill_preflight", lambda *_a, **_k:
        {"ready": False, "blockers": [{"code": "MONITOR_MADLAD_BUSY", "message": "MADLAD занят"}]})
    monkeypatch.setattr("max_postiz_agent.monitor_commands.ReserveServerCycle", lambda *_a, **_k:
                        pytest.fail("busy model reached source/model claim"))
    gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "queued" and result["phase"] == "waiting_services"
    assert result["inference_verified"] is False
    with sqlite3.connect(gateway.path) as connection:
        assert connection.execute("SELECT passes FROM monitor_command_jobs").fetchone()[0] == 0


@pytest.fixture
def full_gateway(outgoing_gateway):
    outgoing_gateway.options["allowed_actions"] = sorted(OWNER_ACTIONS)
    return outgoing_gateway


def saved_job(gateway, command_id):
    with gateway._connection() as connection:
        return dict(connection.execute("SELECT * FROM monitor_command_jobs WHERE id=?", (command_id,)).fetchone())


@pytest.mark.parametrize(("text", "action"), [
    ("команды", "help"), ("/Помощь", "help"), ("задачи", "tasks"), ("покажи задачи", "tasks"),
    ("задача " + "a" * 32, "task"), ("отмени задачу " + "a" * 32, "cancel"),
    ("продолжи подготовку", "resume_refill"), ("возобнови подготовку", "resume_refill"),
    ("диагностика", "diagnostics"), ("проверка", "diagnostics"),
])
def test_expanded_fixed_dictionary(text, action):
    assert parse_command(text)["action"] == action


def test_help_uses_exact_active_catalog_and_all_aliases(full_gateway):
    result = full_gateway.handle(outgoing_event(full_gateway, "команды"), now_at=STAMP)
    assert set(result["actions"]) == OWNER_ACTIONS
    assert len(result["message"]) < 3500
    for spec in COMMAND_SPECS:
        assert spec["syntax"] in result["message"]
        for alias in spec["aliases"]:
            assert alias in result["message"]
    full_gateway.options["allowed_actions"] = ["help", "status", "reserve"]
    result = full_gateway.handle(outgoing_event(full_gateway, "помощь"), now_at=STAMP)
    assert result["actions"] == ["help", "reserve", "status"]
    assert "пополни резерв до N" not in result["message"]
    assert "отмени задачу ID" not in result["message"]


def test_editorial_actor_fits_postgres_schema_and_preserves_numeric_legacy():
    first, second = "max-account-sha256:" + "a" * 64, "max-account-sha256:" + "b" * 64
    compact = _editorial_actor(first)
    assert len(compact) == Audit.__table__.c.actor.type.length == 64
    assert compact.startswith("max-monitor:") and compact == _editorial_actor(first)
    assert compact != _editorial_actor(second)
    assert _editorial_actor("321") == "max-monitor:321"


def test_owner_core_audit_compacts_actor_but_inbox_and_source_proposal_keep_full_identity(full_gateway):
    principal = full_gateway.options["principal"]
    compact = _editorial_actor(principal)
    paused = full_gateway.handle(outgoing_event(full_gateway, "пауза"), now_at=STAMP)
    assert paused["state"] == "done"
    proposed = full_gateway.handle(outgoing_event(full_gateway, "измени фильтр авторы: NASA"), now_at=STAMP)
    next_minute = int(STAMP // 60) * 60 + 60
    confirmed = full_gateway.handle(outgoing_event(full_gateway, "подтверждаю " + proposed["proposal_id"],
        created_at=next_minute, observed_at=next_minute + 1), now_at=next_minute + 1)
    assert confirmed["proposal_state"] == "applied"
    with full_gateway.core.db.session() as session:
        actors = list(session.scalars(select(Audit.actor).where(Audit.action.in_({"editorial_control", "editorial_settings"}))))
        assert actors == [compact, compact]
    assert saved_job(full_gateway, paused["command_id"])["actor"] == principal
    assert saved_job(full_gateway, confirmed["command_id"])["actor"] == principal
    with full_gateway._connection() as connection:
        assert connection.execute("SELECT actor FROM monitor_source_proposals WHERE id=?",
            (proposed["proposal_id"],)).fetchone()[0] == principal
    assert compact not in json.dumps(paused) + json.dumps(proposed) + json.dumps(confirmed)


@pytest.mark.parametrize(("field", "label", "value"), [("keywords", "ключевые слова", "космос"), ("authors", "авторы", "NASA")])
def test_confirmed_filter_set_then_explicit_clear_restores_settings_and_preserves_ready_history(full_gateway, monkeypatch, field, label, value):
    gateway = full_gateway
    state = ServerState(gateway.core)
    before = state.read()
    original_settings_hash = fingerprint(before["settings"])
    history = {"records": [{"id": "saved-ready", "state": "quality_ready", "text_hash": "e" * 64,
                             "reserve_contract": "article-reserve-v1"},
                            {"id": "saved-refusal", "state": "cancelled", "error": "ARTICLE_QUALITY_REJECTED"}]}
    with gateway.core.db.session.begin() as session:
        session.add(EditorialLedger(id="hourly", revision=7, payload=history, updated_at=STAMP))
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("filter change fetched a source or model"))

    def command(text, minute):
        created = int(STAMP // 60) * 60 + minute * 60
        return gateway.handle(outgoing_event(gateway, text, created_at=created, observed_at=created + 5), now_at=created + 5)

    proposed = command("измени фильтр " + label + ": " + value, 0)
    assert proposed["proposal_state"] == "awaiting_confirmation"
    assert fingerprint(state.read()["settings"]) == original_settings_hash
    applied = command("подтверждаю " + proposed["proposal_id"], 1)
    assert applied["proposal_state"] == "applied" and state.read()["settings"]["nasa"][field] == [value]
    clear = command("очисти фильтр " + label, 2)
    assert clear["before"] == [value] and clear["after"] == []
    assert "без фильтра (список будет очищен)" in clear["message"]
    assert state.read()["settings"]["nasa"][field] == [value]
    cleared = command("подтверждаю " + clear["proposal_id"], 3)
    assert cleared["proposal_state"] == "applied" and "Фильтр очищен" in cleared["message"]
    after = state.read()
    assert after["paused"] is before["paused"] is True
    assert after["revision"] == before["revision"] + 2
    assert fingerprint(after["settings"]) == original_settings_hash
    with gateway.core.db.session() as session:
        saved = session.get(EditorialLedger, "hourly")
        assert saved.payload == history and saved.revision == 7 and saved.updated_at == STAMP


@pytest.mark.parametrize("text", ["измени фильтр авторы:", "измени фильтр ключевые слова: ,", "измени фильтр авторы: , ,"])
def test_empty_csv_does_not_implicitly_clear_filters(text):
    with pytest.raises(AgentError):
        parse_command(text)


def test_existing_management_actions_now_use_authenticated_owner_and_exact_allowlist(full_gateway):
    result = full_gateway.handle(outgoing_event(full_gateway, "пауза"), now_at=STAMP)
    assert result["state"] == "done" and result["paused"] is True
    result = full_gateway.handle(outgoing_event(full_gateway, "источники"), now_at=STAMP)
    assert result["state"] == "done" and "sources" in result
    full_gateway.options["allowed_actions"].remove("sources")
    with pytest.raises(AgentError, match="MONITOR_COMMAND_ACTION_DENIED"):
        full_gateway.handle(outgoing_event(full_gateway, "покажи источники"), now_at=STAMP)


def test_urgent_pause_and_cancel_work_in_refill_minute_and_new_work_remains_bounded(full_gateway, monkeypatch):
    first = full_gateway.handle(outgoing_event(full_gateway, "пополни резерв до 6"), now_at=STAMP)
    full_gateway.handle(outgoing_event(full_gateway, "пауза подготовки"), now_at=STAMP)
    cancelled = full_gateway.handle(outgoing_event(full_gateway, "отмени задачу " + first["command_id"]), now_at=STAMP)
    assert cancelled["task_state"] == "cancelled"
    monkeypatch.setattr(full_gateway, "_refill", lambda *_: pytest.fail("cancelled job started inference"))
    assert full_gateway.run_one(now_at=STAMP)["state"] == "paused"
    blocked = full_gateway.handle(outgoing_event(full_gateway, "продолжи подготовку"), now_at=STAMP)
    assert blocked["state"] == "blocked" and blocked["code"] == "MONITOR_COMMAND_MINUTE_ALREADY_USED"
    assert full_gateway._refill_paused() is True
    next_minute = int(STAMP // 60) * 60 + 60
    result = full_gateway.handle(outgoing_event(full_gateway, "продолжи подготовку", created_at=next_minute,
        observed_at=next_minute + 1), now_at=next_minute + 1)
    assert result["monitor_refill_paused"] is False
    assert ServerState(full_gateway.core).read()["paused"] is True
    assert full_gateway.run_one(now_at=next_minute + 1)["state"] == "idle"


def test_minute_limited_source_command_is_persisted_replied_once_and_never_executed(full_gateway, monkeypatch):
    first = full_gateway.handle(outgoing_event(full_gateway, "пополни резерв до 6"), now_at=STAMP)
    monkeypatch.setattr(full_gateway, "_execute", lambda *_: pytest.fail("minute-blocked command executed"))
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("blocked source URL fetched"))
    received = outgoing_event(full_gateway, "добавь источник https://example.org/science/")
    blocked = full_gateway.handle(received, now_at=STAMP)
    duplicate = full_gateway.handle(received, now_at=STAMP + 1)
    assert blocked["state"] == "blocked" and blocked["code"] == "MONITOR_COMMAND_MINUTE_ALREADY_USED"
    assert "следующую минуту" in blocked["message"] and blocked["code"] in blocked["message"]
    assert duplicate["duplicate"] is True and duplicate["command_id"] == blocked["command_id"]
    assert duplicate["message"] == blocked["message"]
    assert saved_job(full_gateway, first["command_id"])["state"] == "queued"
    assert saved_job(full_gateway, blocked["command_id"])["state"] == "blocked"
    with full_gateway._connection() as connection:
        assert connection.execute("SELECT count(*) FROM monitor_command_jobs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM monitor_command_mutation_minutes").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM monitor_source_proposals").fetchone()[0] == 0


@pytest.mark.parametrize(("code", "reason"), [
    ("MONITOR_TASK_NOT_FOUND", "Проверьте «задачи»"),
    ("EDITORIAL_PAUSE_BEFORE_SETTINGS", "только на общей паузе"),
    ("EDITORIAL_REVISION_CONFLICT", "Настройки изменились"),
    ("MONITOR_COPY_TARGETS_NOT_PAUSED", "адресаты копии должны оставаться на паузе"),
    ("ARTICLE_RECONCILIATION_REQUIRED", "сначала нужна сверка"),
    ("MONITOR_PROPOSAL_INVALID", "просрочено"),
    ("MONITOR_COMMAND_CONFIG_CHANGED", "разрешения команды изменились"),
])
def test_known_blocked_commands_include_safe_reason_and_code_in_reply(full_gateway, monkeypatch, code, reason):
    def blocked(*_):
        raise AgentError(code, 409)

    monkeypatch.setattr(full_gateway, "_execute", blocked)
    first = full_gateway.handle(outgoing_event(full_gateway, "статус"), now_at=STAMP)
    duplicate = full_gateway.handle(outgoing_event(full_gateway, "статус"), now_at=STAMP + 1)
    assert first["state"] == "blocked" and first["code"] == code
    assert reason in first["message"] and code in first["message"]
    assert duplicate["duplicate"] is True and duplicate["message"] == first["message"]


def test_unknown_internal_error_reason_never_exposes_raw_code_or_paths(full_gateway, monkeypatch):
    def private(*_):
        raise AgentError("PRIVATE_SERVER_TOKEN_CREDENTIAL_PATH", 503)

    monkeypatch.setattr(full_gateway, "_execute", private)
    result = full_gateway.handle(outgoing_event(full_gateway, "статус"), now_at=STAMP)
    assert result["code"] == "MONITOR_TASK_BLOCKED" and "MONITOR_TASK_BLOCKED" in result["message"]
    assert "PRIVATE" not in json.dumps(result) and "CREDENTIAL" not in json.dumps(result)


def test_errors_include_bounded_safe_candidate_history_without_initialization_or_mutation(gateway, monkeypatch):
    directory = gateway.core.settings.root / "data/hourly-articles"
    assert not directory.exists()
    source_id = gateway.core.settings.sources["sources"][0]["id"]
    private_hash = "f" * 64
    records = [{"id": f"{index:08x}-1111-4111-8111-111111111111", "state": "cancelled", "source_id": source_id,
        "claimed_at": index, "slot": 0, "error": "PRIVATE_OLD_REASON", "raw": "PRIVATE_MODEL_TEXT"} for index in range(1, 13)]
    records[7]["error"] = "ARTICLE_IMAGE_RIGHTS_REQUIRED"
    records[8]["error"] = "ARTICLE_IMAGE_SOURCE_UNAVAILABLE"
    records[9].update(error="ARTICLE_QUALITY_REJECTED", automatic_repair_attempts=[
        {"error": "TRANSLATION_REFERENCE_CHANGED", "raw": "PRIVATE_REPAIR_TEXT"}])
    records[10].update(state="needs_attention", error="TRANSLATION_SERVICE_UNAVAILABLE")
    records[11].update(id=private_hash, state="unknown", source_id="PRIVATE_UNCONFIGURED_SOURCE",
        error="PRIVATE_TOKEN_REASON", title="PRIVATE_TITLE", url="https://private.example/secret?token=PRIVATE_TOKEN",
        principal="max-account-sha256:" + private_hash, quality_trace="C:/PRIVATE/trace.local.jsonl", input_hash=private_hash)
    payload = {"records": records}
    with gateway.core.db.session.begin() as session:
        session.add(EditorialLedger(id="hourly", revision=9, payload=payload, updated_at=STAMP))
    with gateway.core.db.session() as session:
        audit_before = list(session.scalars(select(Audit.id)))
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("history requested HTTP/model"))
    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle.__init__",
                        lambda *_a, **_k: pytest.fail("history initialized an absent runtime"))
    result = gateway.handle(event("ошибки"), now_at=STAMP)
    assert result["state"] == "done" and result["candidate_history_available"] is True
    history = result["candidate_history"]
    assert len(history) == 5
    assert [value["record_id"] for value in history] == ["скрыт", "0000000b", "0000000a", "00000009", "00000008"]
    assert history[0]["source_id"] is None and history[0]["reconciliation_required"] is True
    assert history[1]["reconciliation_required"] is True
    codes = {code for value in history for code in value["codes"]}
    assert {"ARTICLE_IMAGE_RIGHTS_REQUIRED", "ARTICLE_IMAGE_SOURCE_UNAVAILABLE", "ARTICLE_QUALITY_REJECTED",
            "TRANSLATION_REFERENCE_CHANGED"} <= codes
    assert "история, не активная авария" in result["message"] and "повтор запрещён" in result["message"]
    assert len(result["message"]) <= 3500
    visible = json.dumps(result, ensure_ascii=False)
    assert all(secret not in visible for secret in ("PRIVATE", private_hash, "https://", "C:/", "max-account-sha256:"))
    assert "TRANSLATION_REFERENCE_CHANGED" not in json.dumps(result["incidents"])
    assert not directory.exists()
    with gateway.core.db.session() as session:
        ledger = session.get(EditorialLedger, "hourly")
        assert ledger.payload == payload and ledger.revision == 9 and ledger.updated_at == STAMP
        assert list(session.scalars(select(Audit.id))) == audit_before


def test_candidate_history_reads_legacy_ledger_without_rewriting_or_new_files(gateway, monkeypatch):
    directory = gateway.core.settings.root / "data/hourly-articles"
    directory.mkdir()
    path = directory / "ledger.local.json"
    payload = {"records": [{"id": "12345678-1111-4111-8111-111111111111", "slot": 0, "state": "cancelled",
        "source_id": gateway.core.settings.sources["sources"][0]["id"], "error": "ARTICLE_IMAGE_RIGHTS_REQUIRED"}]}
    text = json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    files = sorted(value.name for value in directory.iterdir())
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("legacy history requested network"))
    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle.__init__",
                        lambda *_a, **_k: pytest.fail("legacy history called cycle init"))
    history, available = gateway._candidate_refusals()
    assert available is True and history[0]["record_id"] == "12345678"
    assert history[0]["codes"] == ["ARTICLE_IMAGE_RIGHTS_REQUIRED"]
    assert path.read_text(encoding="utf-8") == text
    assert sorted(value.name for value in directory.iterdir()) == files
    with gateway.core.db.session() as session:
        assert session.get(EditorialLedger, "hourly") is None


def test_errors_without_candidate_runtime_do_not_bootstrap_ledger(gateway, monkeypatch):
    directory = gateway.core.settings.root / "data/hourly-articles"
    assert not directory.exists()
    monkeypatch.setattr("httpx.Client.request", lambda *_a, **_k: pytest.fail("empty history requested network"))
    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle.__init__",
                        lambda *_a, **_k: pytest.fail("empty history bootstrapped cycle"))
    result = gateway.handle(event("ошибки"), now_at=STAMP)
    assert result["state"] == "done" and result["candidate_history"] == []
    assert result["candidate_history_available"] is True and not directory.exists()
    with gateway.core.db.session() as session:
        assert session.get(EditorialLedger, "hourly") is None


def test_task_views_are_bounded_and_scoped_to_actor_and_chat(gateway):
    gateway.options["allowed_actions"] += ["tasks", "task", "cancel"]
    ids = [gateway.handle(event("пополни резерв до 6", message_id="refill_" + str(index)),
                          now_at=STAMP + index)["command_id"] for index in range(12)]
    with gateway._connection() as connection:
        connection.execute("UPDATE monitor_command_jobs SET actor='999' WHERE id=?", (ids[-1],))
        connection.execute("UPDATE monitor_command_jobs SET chat_id='-999' WHERE id=?", (ids[-2],))
        connection.execute("UPDATE monitor_command_jobs SET result=? WHERE id=?",
            (json.dumps({"ready": 2, "raw": "PRIVATE_RAW_SECRET", "message": "PRIVATE_MESSAGE_SECRET"}), ids[-3]))
    result = gateway.handle(event("задачи", message_id="tasks"), now_at=STAMP + 20)
    assert len(result["tasks"]) == 10
    assert {value["id"] for value in result["tasks"]} == set(ids[:-2])
    assert all(command_id in result["message"] for command_id in ids[:-2])
    assert "PRIVATE" not in json.dumps(result)
    details = gateway.handle(event("задача " + ids[-3], message_id="details"), now_at=STAMP + 20)
    assert details["tasks"][0]["target"] == 6 and details["tasks"][0]["ready"] == 2
    for index, hidden in enumerate(ids[-2:]):
        read = gateway.handle(event("задача " + hidden, message_id="hidden_" + str(index)), now_at=STAMP + 20)
        cancel = gateway.handle(event("отмени задачу " + hidden, message_id="cancel_hidden_" + str(index)), now_at=STAMP + 20)
        assert read["code"] == cancel["code"] == "MONITOR_TASK_NOT_FOUND"
        assert saved_job(gateway, hidden)["state"] == "queued"


@pytest.mark.parametrize(("phase", "ready", "expected"), [
    ("reserve_refilled", 2, "cancelled"), ("reserve_satisfied", 6, "done"), ("waiting_services", 0, "cancelled"),
])
def test_running_cancellation_waits_for_known_pass_without_cancelling_network(gateway, monkeypatch, phase, ready, expected):
    gateway.options["allowed_actions"].append("cancel")
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    calls = []

    def prepare(*_):
        calls.append("network_started")
        cancellation = gateway.handle(event("отмени задачу " + accepted["command_id"], message_id="cancel"), now_at=STAMP)
        assert cancellation["task_state"] == "running"
        assert saved_job(gateway, accepted["command_id"])["state"] == "running"
        calls.append("network_completed")
        return {"phase": phase, "ready": ready}

    monkeypatch.setattr(gateway, "_refill", prepare)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == expected and result["cancellation_requested"] is True
    assert calls == ["network_started", "network_completed"]
    assert gateway.run_one(now_at=STAMP + 60)["state"] == "idle"


def test_cancellation_between_pass_check_and_finish_is_atomic_and_never_claimed_again(gateway, monkeypatch):
    gateway.options["allowed_actions"].append("cancel")
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    finish = gateway._finish
    calls = []

    def prepare(*_):
        calls.append("inference")
        return {"phase": "reserve_refilled", "ready": 1}

    def race(command_id, state, result, stamp, *, next_at=0):
        if command_id == accepted["command_id"] and state == "queued":
            cancellation = gateway.handle(event("отмени задачу " + command_id, message_id="late_cancel"), now_at=STAMP)
            assert cancellation["task_state"] == "running"
        return finish(command_id, state, result, stamp, next_at=next_at)

    monkeypatch.setattr(gateway, "_refill", prepare)
    monkeypatch.setattr(gateway, "_finish", race)
    assert gateway.run_one(now_at=STAMP)["state"] == "cancelled"
    assert gateway.run_one(now_at=STAMP + 60)["state"] == "idle"
    assert calls == ["inference"]


def test_recovered_queued_cancel_request_is_terminalized_before_claim(gateway, monkeypatch):
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    with gateway._connection() as connection:
        connection.execute("INSERT INTO monitor_command_cancellations VALUES(?,?,?,?)",
            (accepted["command_id"], "321", "-123", STAMP))
    monkeypatch.setattr(gateway, "_refill", lambda *_: pytest.fail("persisted cancelled job started inference"))
    assert gateway.run_one(now_at=STAMP)["state"] == "idle"
    assert saved_job(gateway, accepted["command_id"])["state"] == "cancelled"


def test_cancel_request_never_converts_unknown_inference_or_releases_it(gateway, monkeypatch):
    gateway.options["allowed_actions"].append("cancel")
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)

    def uncertain(*_):
        gateway.handle(event("отмени задачу " + accepted["command_id"], message_id="cancel"), now_at=STAMP)
        raise RuntimeError("private unknown network outcome")

    monkeypatch.setattr(gateway, "_refill", uncertain)
    result = gateway.run_one(now_at=STAMP)
    assert result["state"] == "unknown"
    again = gateway.handle(event("отмени задачу " + accepted["command_id"], message_id="cancel_again"), now_at=STAMP)
    assert again["task_state"] == "unknown" and again["changed"] is False
    assert again["code"] == "MONITOR_TASK_RESULT_UNKNOWN" and again["code"] in again["message"]
    assert saved_job(gateway, accepted["command_id"])["state"] == "unknown"
    assert gateway.run_one(now_at=STAMP + 9999)["state"] == "unknown"


def test_resume_only_clears_chat_pause_and_does_not_revive_terminal_tasks(gateway):
    gateway.options["allowed_actions"].append("resume_refill")
    accepted = gateway.handle(event("пополни резерв до 6"), now_at=STAMP)
    with gateway._connection() as connection:
        connection.execute("UPDATE monitor_command_jobs SET state='blocked' WHERE id=?", (accepted["command_id"],))
    gateway.handle(event("пауза подготовки", message_id="pause"), now_at=STAMP)
    before = ServerState(gateway.core).read()
    result = gateway.handle(event("продолжи подготовку", message_id="resume"), now_at=STAMP)
    assert result["monitor_refill_paused"] is False
    assert ServerState(gateway.core).read() == before
    assert saved_job(gateway, accepted["command_id"])["state"] == "blocked"


def test_remote_project_resume_requires_prepare_only_api_and_preserves_refill_flag(full_gateway, monkeypatch):
    before = ServerState(full_gateway.core).read()
    with full_gateway._connection() as connection:
        connection.execute("INSERT INTO monitor_command_controls VALUES('refill_paused','true')")
    calls = []

    def guarded_start(state, action, revision, actor, *, prepare_only):
        calls.append((action, revision, actor, prepare_only))
        return {**before, "paused": False}

    monkeypatch.setattr(ServerState, "control", guarded_start)
    result = full_gateway.handle(outgoing_event(full_gateway, "продолжи проект"), now_at=STAMP)
    assert result["project_paused"] is False
    assert calls == [("start", before["revision"], _editorial_actor(full_gateway.options["principal"]), True)]
    assert full_gateway._refill_paused() is True
    assert ServerState(full_gateway.core).read() == before


def test_remote_project_resume_preserves_real_targets_paused(full_gateway):
    with full_gateway.core.db.session() as session:
        before_targets = {value.id: value.paused for value in session.scalars(select(Target))}
    assert all(before_targets.values())
    result = full_gateway.handle(outgoing_event(full_gateway, "сними общую паузу"), now_at=STAMP)
    assert result["state"] == "done" and result["project_paused"] is False
    assert ServerState(full_gateway.core).read()["paused"] is False
    with full_gateway.core.db.session() as session:
        assert {value.id: value.paused for value in session.scalars(select(Target))} == before_targets


@pytest.mark.parametrize("blocker", ["unpaused", "unknown", "mutation"])
def test_remote_project_resume_blocks_unsafe_editorial_state(full_gateway, blocker):
    if blocker == "unpaused":
        with full_gateway.core.db.session.begin() as session:
            session.add(Target(id="resume_test", profile_id="main", platform="max", route="max_browser",
                destination="-999", enabled=True, paused=False, binding_hash="a" * 64))
    else:
        insert_unresolved(full_gateway, mutation=blocker == "mutation")
    before = ServerState(full_gateway.core).read()
    result = full_gateway.handle(outgoing_event(full_gateway, "продолжи проект"), now_at=STAMP)
    assert result["state"] == "blocked"
    assert result["code"] == ("MONITOR_COPY_TARGETS_NOT_PAUSED" if blocker == "unpaused" else "ARTICLE_RECONCILIATION_REQUIRED")
    assert ServerState(full_gateway.core).read() == before


def test_diagnostics_requests_only_safe_preflight_and_never_enqueues_work(full_gateway, monkeypatch):
    calls = []

    def check(core, *, check_services, diagnostic_services):
        calls.append((core, check_services, diagnostic_services))
        return {"ready": False, "service_readiness_checked": True, "inference_verified": False,
                "blockers": [{"code": "MONITOR_MADLAD_BUSY", "message": "MADLAD занят"}]}

    monkeypatch.setattr("max_postiz_agent.monitor_commands.refill_preflight", check)
    monkeypatch.setattr(full_gateway, "_refill", lambda *_: pytest.fail("diagnostics began preparation"))
    result = full_gateway.handle(outgoing_event(full_gateway, "диагностика"), now_at=STAMP)
    assert calls == [(full_gateway.core, True, True)]
    assert result["state"] == "done" and result["diagnostics"]["inference_verified"] is False
    assert "MADLAD занят" in result["message"]
    assert full_gateway.run_one(now_at=STAMP)["state"] == "idle"


def test_permission_expansion_migration_preserves_original_job_and_audits(outgoing_gateway, monkeypatch):
    gateway = outgoing_gateway
    previous = copy.deepcopy(gateway.options)
    accepted = gateway.handle(outgoing_event(gateway, "пополни резерв до 6"), now_at=STAMP)
    before = saved_job(gateway, accepted["command_id"])
    gateway.options["allowed_actions"] = sorted(OWNER_ACTIONS)
    result = gateway.migrate_queued_permission_expansion(previous, actor=gateway.options["principal"], now_at=STAMP)
    assert result["migrated"] == [accepted["command_id"]]
    after = saved_job(gateway, accepted["command_id"])
    assert after == {**before, "config_hash": fingerprint(gateway.options)}
    with gateway._connection() as connection:
        audit = dict(connection.execute("SELECT * FROM monitor_command_permission_migrations").fetchone())
    assert audit["old_config_hash"] == before["config_hash"]
    assert audit["new_config_hash"] == after["config_hash"] and audit["event_hash"] == before["event_hash"]
    assert audit["payload_hash"] == fingerprint(json.loads(before["payload"]))
    assert gateway.migrate_queued_permission_expansion(previous, actor=gateway.options["principal"], now_at=STAMP)["migrated"] == []
    monkeypatch.setattr(gateway, "_refill", lambda *_: {"phase": "reserve_satisfied", "ready": 6})
    assert gateway.run_one(now_at=STAMP)["state"] == "done"


@pytest.mark.parametrize("state", ["running", "unknown", "blocked", "done", "cancelled"])
def test_permission_migration_never_restarts_nonqueued_jobs(outgoing_gateway, state):
    gateway = outgoing_gateway
    previous = copy.deepcopy(gateway.options)
    accepted = gateway.handle(outgoing_event(gateway, "пополни резерв до 6"), now_at=STAMP)
    with gateway._connection() as connection:
        connection.execute("UPDATE monitor_command_jobs SET state=? WHERE id=?", (state, accepted["command_id"]))
    before = saved_job(gateway, accepted["command_id"])
    gateway.options["allowed_actions"] = sorted(OWNER_ACTIONS)
    if state in {"running", "unknown"}:
        with pytest.raises(AgentError, match="MONITOR_PERMISSION_MIGRATION_NOT_QUIESCENT"):
            gateway.migrate_queued_permission_expansion(previous, actor=gateway.options["principal"], now_at=STAMP)
    else:
        assert gateway.migrate_queued_permission_expansion(previous, actor=gateway.options["principal"], now_at=STAMP)["migrated"] == []
    assert saved_job(gateway, accepted["command_id"]) == before


@pytest.mark.parametrize("change", ["account", "chat", "target", "dom", "age", "remove_action", "actor"])
def test_permission_migration_rejects_any_unapproved_boundary_change(outgoing_gateway, change):
    gateway = outgoing_gateway
    previous = copy.deepcopy(gateway.options)
    accepted = gateway.handle(outgoing_event(gateway, "пополни резерв до 6"), now_at=STAMP)
    before = saved_job(gateway, accepted["command_id"])
    gateway.options["allowed_actions"] = sorted(OWNER_ACTIONS)
    actor = gateway.options["principal"]
    if change == "account":
        gateway.options["account_sha256"] = "b" * 64
        gateway.options["principal"] = "max-account-sha256:" + "b" * 64
        actor = gateway.options["principal"]
    elif change == "chat":
        gateway.options["control_chat_id"] = "-999"
    elif change == "target":
        gateway.options["control_chat_name"] = "other"
    elif change == "dom":
        gateway.options["dom_contract"] = owner_contract()
        gateway.options["dom_contract_sha256"] = fingerprint(gateway.options["dom_contract"])
    elif change == "age":
        gateway.options["max_age_seconds"] = 301
    elif change == "remove_action":
        gateway.options["allowed_actions"].remove("reserve")
    elif change == "actor":
        actor = "other"
    with pytest.raises(AgentError, match="MONITOR_PERMISSION_MIGRATION_INVALID"):
        gateway.migrate_queued_permission_expansion(previous, actor=actor, now_at=STAMP)
    assert saved_job(gateway, accepted["command_id"]) == before


@pytest.mark.parametrize("proof_change", [None, "missing", "stale", "wrong_account", "wrong_hash", "not_verified"])
def test_dom_v1_upgrade_requires_explicit_fresh_bound_acceptance(outgoing_gateway, proof_change):
    gateway = outgoing_gateway
    previous = copy.deepcopy(gateway.options)
    accepted = gateway.handle(outgoing_event(gateway, "пополни резерв до 6"), now_at=STAMP)
    before = saved_job(gateway, accepted["command_id"])
    gateway.options["allowed_actions"] = sorted(OWNER_ACTIONS)
    gateway.options["dom_contract"] = owner_contract()
    gateway.options["dom_contract_sha256"] = fingerprint(gateway.options["dom_contract"])
    proof = {"contract": "max-owner-dom-upgrade-acceptance-v1", "old_dom_sha256": previous["dom_contract_sha256"],
        "new_dom_sha256": gateway.options["dom_contract_sha256"], "account_sha256": gateway.options["account_sha256"],
        "chat_id": gateway.options["control_chat_id"], "observed_at": STAMP - 1, "verified": True,
        "evidence_sha256": "d" * 64}
    if proof_change == "missing":
        proof = None
    elif proof_change == "stale":
        proof["observed_at"] = STAMP - 301
    elif proof_change == "wrong_account":
        proof["account_sha256"] = "b" * 64
    elif proof_change == "wrong_hash":
        proof["old_dom_sha256"] = "c" * 64
    elif proof_change == "not_verified":
        proof["verified"] = False
    if proof_change:
        with pytest.raises(AgentError, match="MONITOR_PERMISSION_MIGRATION_INVALID"):
            gateway.migrate_queued_permission_expansion(previous, actor=gateway.options["principal"], now_at=STAMP,
                allow_dom_upgrade=True, acceptance_proof=proof)
        assert saved_job(gateway, accepted["command_id"]) == before
    else:
        result = gateway.migrate_queued_permission_expansion(previous, actor=gateway.options["principal"], now_at=STAMP,
            allow_dom_upgrade=True, acceptance_proof=proof)
        assert result["migrated"] == [accepted["command_id"]]
        with gateway._connection() as connection:
            audit = dict(connection.execute("SELECT * FROM monitor_command_permission_migrations").fetchone())
        assert audit["old_dom_hash"] == previous["dom_contract_sha256"]
        assert audit["new_dom_hash"] == gateway.options["dom_contract_sha256"]
        assert audit["acceptance_proof_hash"] == fingerprint(proof)
        assert saved_job(gateway, accepted["command_id"])["event_hash"] == before["event_hash"]
