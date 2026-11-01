"""Резерв: настоящие локальные транзакции, фиктивные модели и внешние сервисы."""
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import create_engine, text

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.article_reserve import ArticleReserveStore
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Base, Database, uid
from max_postiz_agent.hourly_reserve import ReserveServerCycle
from max_postiz_agent.hub_models import Dispatch, TargetApproval
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState
from test_automatic_policy import Model

pytest_plugins = ["test_automatic_policy"]


@pytest.fixture
def reserve_controller(automatic_cycle, monkeypatch):
    (cycle, core, article, posts, writes, stamp, _), review = automatic_cycle
    cycle.reserve_mode = True
    cycle._save({"records": []})
    DatabaseArticleLedger(cycle).migrate()
    state = ServerState(core)
    state.initialize()
    value = state.read()
    settings = copy.deepcopy(value["settings"])
    settings["reserve"] = {"enabled": True, "target": 6, "low": 3, "maximum": 12, "batch": 3}
    state.settings(settings, value["revision"], "test-owner")
    state.control("start", state.read()["revision"], "test-owner")
    monkeypatch.setattr("max_postiz_agent.article_reserve.now", lambda: stamp[0])
    serial = [0]
    model_calls = []

    def quality(record_id=None, review_only=False):
        serial[0] += 1
        ordinal = ["первый", "второй", "третий", "четвёртый", "пятый", "шестой",
                   "седьмой", "восьмой", "девятый", "десятый", "одиннадцатый", "двенадцатый"]
        title = "Космический ветер " + ordinal[(serial[0] - 1) % len(ordinal)]
        model_calls.append(record_id)
        return ArticleQuality(
            Model({"title": title,
                "body": "Учёные изучили необычные процессы в атмосфере далёкой звезды."}, "hy-mt2-7b-q8"),
            Model(copy.deepcopy(review), "ministral"), prompt_dir=core.settings.root / "prompts",
            trace=cycle._trace_writer(record_id), review_rubric="meaning-first-v1",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))

    monkeypatch.setattr(cycle, "_quality", quality)

    def selector(_settings, *, ledger, **_):
        return copy.deepcopy(article) if not any(r.get("url") == article["url"] for r in ledger["records"]) else None

    store = ArticleReserveStore(core)
    controller = ReserveServerCycle(core, state=state, selector=selector,
        cycle=cycle, clock=lambda: stamp[0], reserve_store=store)
    return SimpleNamespace(controller=controller, cycle=cycle, core=core, article=article,
        posts=posts, writes=writes, stamp=stamp, state=state, store=store,
        model_calls=model_calls, quality=quality)


def test_verified_reserve_stages_approves_and_delivers_exactly_once(reserve_controller):
    value = reserve_controller
    value.controller.prepare_once()
    ready = value.cycle._load()["records"]
    assert len(ready) == 1 and ready[0]["state"] == "quality_ready"
    assert not value.posts and not value.writes
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0

    value.controller.run_once()
    scheduled = value.cycle._load()["records"][0]
    assert scheduled["state"] == "pending"
    assert len(value.posts) == len(value.writes) == 1
    value.controller.run_once()
    assert len(value.writes) == 1
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1

    value.core.publisher.mode = "browser"
    value.stamp[0] = scheduled["due_at"] + 1
    assert value.core.dispatch_tick() == 1
    assert value.core.dispatch_tick() == 0
    value.posts[scheduled["postiz_id"]]["state"] = "PUBLISHED"
    value.controller.run_once()
    delivered = value.cycle._load()["records"][0]
    assert delivered["state"] == "published" and delivered["delivery_verified"]
    assert len(value.writes) == 1
    with value.core.db.session() as session:
        dispatch = session.query(Dispatch).one()
        assert dispatch.attempts == 1 and dispatch.state == "published"


def test_unknown_send_keeps_slot_and_does_not_publish_replacement(reserve_controller, monkeypatch):
    value = reserve_controller
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    value.stamp[0] = record["due_at"] + 1
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(args)
        raise AgentError("BROWSER_OPERATION_UNCONFIRMED", 503)

    monkeypatch.setattr(value.core.publisher, "publish", uncertain)
    value.core.dispatch_tick()
    value.controller.run_once()
    value.stamp[0] += 7200
    value.controller.run_once()
    value.core.dispatch_tick()
    with value.core.db.session() as session:
        dispatch = session.query(Dispatch).one()
        assert dispatch.state == "unknown" and dispatch.attempts == 1
        assert session.query(TargetApproval).count() == 1
    assert len(calls) == len(value.writes) == 1


def test_changed_evidence_never_creates_remote_draft(reserve_controller):
    value = reserve_controller
    value.controller.prepare_once()
    from max_postiz_agent.hub_models import Variant
    record = value.cycle._load()["records"][0]
    with value.core.db.session.begin() as session:
        variant = session.get(Variant, record["variant_id"])
        variant.metadata_json = {**variant.metadata_json, "machine_review_evidence_hash": "0" * 64}
    try:
        value.controller.run_once()
    except AgentError:
        pass
    assert not value.posts and not value.writes
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


def test_paused_reserve_does_not_call_models_or_create_claims(reserve_controller):
    value = reserve_controller
    value.state.control("pause", value.state.read()["revision"], "test-owner")
    assert value.controller.prepare_once()["phase"] == "paused"
    assert value.controller.run_once()["phase"] == "paused"
    assert not value.model_calls and not value.writes
    assert value.cycle._load()["records"] == []


def test_inflight_preparation_does_not_block_ready_dispatch(reserve_controller, monkeypatch):
    exercise_inflight(reserve_controller, monkeypatch)


def exercise_inflight(value, monkeypatch):
    value.controller.prepare_once()
    from max_postiz_agent.article_cycle import HourlyArticleCycle
    other = HourlyArticleCycle(value.core, reserve_mode=True)
    entered, release = Event(), Event()
    original = value.quality

    def blocked_quality(record_id=None, review_only=False):
        quality = original(record_id, review_only)
        prepare = quality.prepare

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return prepare(*args, **kwargs)

        quality.prepare = blocked
        return quality

    monkeypatch.setattr(other, "_quality", blocked_quality)
    second = {**value.article, "url": "https://science.example.org/article/second/",
        "original": value.article["original"] + " Another atmospheric observation."}
    other_controller = ReserveServerCycle(value.core, state=value.state,
        selector=lambda *_args, **kwargs: copy.deepcopy(second) if not any(
            r.get("url") == second["url"] for r in kwargs["ledger"]["records"]) else None,
        cycle=other, clock=lambda: value.stamp[0], reserve_store=value.store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(other_controller.prepare_once)
        assert entered.wait(10)
        try:
            value.controller.run_once()
            assert len(value.writes) == 1
            assert value.cycle._load()["records"][0]["state"] == "pending"
        finally:
            release.set()
        task.result(15)
    records = value.cycle._load()["records"]
    assert len(records) == 2
    assert records[0]["state"] == "pending" and records[1]["state"] == "quality_ready"


@pytest.fixture
def postgres_reserve(reserve_controller, monkeypatch):
    """Отдельная схема реального PG; рабочая БД не затрагивается."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    value = reserve_controller
    admin = create_engine(url)
    schema = "reserve_race_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = url + ("&" if "?" in url else "?") + urlencode({"options": "-csearch_path=" + schema})
    previous = value.core.db
    database = Database(value.core.settings, url=isolated)
    database.initialize()
    with previous.engine.connect() as source, database.engine.begin() as target:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row) for row in source.execute(table.select()).mappings()]
            if rows:
                target.execute(table.insert(), rows)
    value.core.db = database
    try:
        yield value
    finally:
        value.core.db = previous
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_postgres_inflight_models_and_dispatch_use_independent_claims(postgres_reserve, monkeypatch):
    exercise_inflight(postgres_reserve, monkeypatch)
    value = postgres_reserve
    first = value.cycle._load()["records"][0]
    value.stamp[0] = first["due_at"] + 1
    value.core.publisher.mode = "browser"
    assert value.core.dispatch_tick() == 1
    status = value.store.status()
    assert status["last_delivered_at"] == value.stamp[0]
    assert status["active"][0]["state"] == "delivered"
    assert value.posts[first["postiz_id"]]["state"] == "QUEUE"


def test_two_rejected_sources_then_third_ready_keep_all_claims_same_hour(reserve_controller, monkeypatch):
    from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
    from test_automatic_article_repair import CORRECT_BODY, SECOND_BODY, patched
    value = reserve_controller
    sources = [
        {**value.article, "url": f"https://science.example.org/article/{name}/",
         "original": value.article["original"] + sentence}
        for name, sentence in (("first", " A first observation."),
                               ("second", " An independent observation."),
                               ("third", " A further discovery."))]
    value.controller.selector = lambda *_args, **kwargs: next((copy.deepcopy(a) for a in sources
        if not any(r.get("url") == a["url"] for r in kwargs["ledger"]["records"])), None)
    original = value.quality

    def quality(record_id=None, review_only=False):
        result = original(record_id, review_only)
        record = value.cycle._record(value.cycle._load(), record_id)
        if record["url"] != sources[-1]["url"]:
            result.reviewer.value = {"approved": False, "topic": "science",
                "issues": ["The translation omits the discovery of the stellar wind."], "warnings": []}
        return result

    monkeypatch.setattr(value.cycle, "_quality", quality)
    editor_calls = []

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            context = json.loads(prompt.split("UNTRUSTED_JSON_DATA:\n", 1)[1])
            editor_calls.append(context)
            body = CORRECT_BODY if len(editor_calls) % 2 else SECOND_BODY
            return json.dumps(patched(context, body), ensure_ascii=False), {}

    value.cycle.repair_factory = lambda trace: ArticleRepair(
        Editor(), prompt_dir=value.core.settings.root / "prompts", trace=trace)
    value.controller.prepare_once()
    records = value.cycle._load()["records"]
    assert len(records) == 3 and len({r["slot"] for r in records}) == 1
    assert [r["state"] for r in records] == ["cancelled", "cancelled", "quality_ready"]
    assert len(editor_calls) == 4
    for rejected in records[:2]:
        assert len(rejected["automatic_repair_attempts"]) == 2
        assert all(r["state"] == "rejected" for r in rejected["automatic_repair_attempts"])
        trace = value.core.settings.root / rejected["quality_trace"]
        assert trace.is_file() and '"approved": false' in trace.read_text(encoding="utf-8")
    assert not value.posts and not value.writes
    value.controller.run_once()
    assert len(value.writes) == 1
    assert value.cycle._load()["records"][2]["state"] == "pending"


def test_unknown_model_blocks_refill_but_does_not_block_ready_delivery(reserve_controller, monkeypatch):
    value = reserve_controller
    value.controller.prepare_once()
    second = {**value.article, "url": "https://science.example.org/article/unknown/",
        "original": value.article["original"] + " A separate observation."}
    value.controller.selector = lambda *_args, **_kwargs: copy.deepcopy(second)
    calls = []

    def uncertain(*_args, **_kwargs):
        calls.append(True)
        raise AgentError("LLM_REQUEST_UNKNOWN", 503)

    monkeypatch.setattr(value.cycle, "_quality", uncertain)
    with pytest.raises(AgentError, match="LLM_REQUEST_UNKNOWN"):
        value.controller.prepare_once()
    assert value.store.active_preparation()["state"] == "blocked_unknown"
    value.controller.prepare_once()
    assert len(calls) == 1
    value.controller.run_once()
    assert len(value.writes) == 1
    assert value.cycle._load()["records"][0]["state"] == "pending"


def test_reserve_above_low_water_does_not_start_new_models(reserve_controller, monkeypatch):
    value = reserve_controller
    sources = [{**value.article, "url": f"https://science.example.org/article/{name}/",
        "original": value.article["original"] + " " + name + " independent observation."}
        for name in ("first", "second", "third", "fourth")]
    value.controller.selector = lambda *_args, **kwargs: next((copy.deepcopy(a) for a in sources
        if not any(r.get("url") == a["url"] for r in kwargs["ledger"]["records"])), None)
    value.controller.prepare_once()
    value.controller.prepare_once()
    assert len(value.model_calls) == 4
    restarted = restart_dispatcher(value)
    assert restarted.prepare_once()["phase"] == "reserve_satisfied"
    assert len(value.model_calls) == 4 and value.store.active_preparation() is None


def assign_without_external(value):
    value.controller.prepare_once()
    record = value.cycle._load()["records"][0]
    verified = value.cycle.validate_reserve(record["id"])
    slot = value.store.assign(record, value.stamp[0] + 120,
        text_hash=record["reserve_prepared_hash"], evidence_hash=verified["reserve_evidence_hash"])
    return record, slot


def restart_dispatcher(value):
    from max_postiz_agent.article_cycle import HourlyArticleCycle
    return ReserveServerCycle(value.core, state=value.state,
        selector=lambda *_args, **_kwargs: pytest.fail("Отправитель не выбирает новый источник"),
        cycle=HourlyArticleCycle(value.core, reserve_mode=True),
        clock=lambda: value.stamp[0], reserve_store=value.store)


@pytest.mark.parametrize("staged", [False, True])
def test_restart_after_assignment_or_draft_keeps_exact_one_dispatch(reserve_controller, staged):
    value = reserve_controller
    record, _ = assign_without_external(value)
    if staged:
        value.cycle.stage_reserve(record["id"])
    restarted = restart_dispatcher(value)
    restarted.run_once()
    restarted.run_once()
    assert len(value.writes) == 1
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1
    assert value.cycle._load()["records"][0]["state"] == "pending"


def test_restart_during_unknown_draft_does_not_repeat_postiz_create(reserve_controller, monkeypatch):
    value = reserve_controller
    record, _ = assign_without_external(value)
    requests = []

    def unknown_create(*args, **kwargs):
        requests.append(args)
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(value.core.postiz, "create", unknown_create)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.cycle.stage_reserve(record["id"])
    restarted = restart_dispatcher(value)
    restarted.run_once()
    restarted.run_once()
    assert len(requests) == 1
    assert value.cycle._load()["records"][0]["state"] == "needs_attention"
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


def test_queue_without_approval_after_crash_blocks_replacement(reserve_controller, monkeypatch):
    value = reserve_controller
    value.controller.prepare_once()

    def interrupted_approval(*args, **kwargs):
        raise AgentError("SIMULATED_APPROVAL_INTERRUPTED", 503)

    monkeypatch.setattr(value.core, "approve_hourly_policy", interrupted_approval)
    with pytest.raises(AgentError, match="SIMULATED_APPROVAL_INTERRUPTED"):
        value.controller.run_once()
    assert list(value.posts.values())[0]["state"] == "QUEUE"
    restarted = restart_dispatcher(value)
    restarted.run_once()
    restarted.run_once()
    assert len(value.writes) == 1
    with value.core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


def test_second_delivery_uses_actual_first_delivery_time(reserve_controller):
    value = reserve_controller
    value.controller.prepare_once()
    value.controller.run_once()
    first = value.cycle._load()["records"][0]
    value.core.publisher.mode = "browser"
    value.stamp[0] = first["due_at"] + 35
    actual_first = value.stamp[0]
    assert value.core.dispatch_tick() == 1
    value.posts[first["postiz_id"]]["state"] = "PUBLISHED"
    value.controller.run_once()
    second = {**value.article, "url": "https://science.example.org/article/next/",
        "original": value.article["original"] + " Another independent discovery."}
    value.controller.selector = lambda *_args, **kwargs: copy.deepcopy(second) if not any(
        r.get("url") == second["url"] for r in kwargs["ledger"]["records"]) else None
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][1]
    assert record["due_at"] >= actual_first + 3600
    value.stamp[0] = actual_first + 3599
    assert value.core.dispatch_tick() == 0
    value.stamp[0] = record["due_at"] + 1
    assert value.core.dispatch_tick() == 1
    with value.core.db.session() as session:
        from max_postiz_agent.hub_models import DispatchAttempt
        attempts = session.query(DispatchAttempt).order_by(DispatchAttempt.created_at).all()
        assert len(attempts) == 2 and attempts[1].created_at - attempts[0].created_at >= 3600


@pytest.mark.parametrize("editor_result", ["invalid_protocol", "no_change"])
def test_known_editor_stop_preserves_raw_and_selects_next_source(reserve_controller, monkeypatch, editor_result):
    from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
    from test_automatic_article_repair import patched
    value = reserve_controller
    second = {**value.article, "url": "https://science.example.org/article/healthy/",
        "original": value.article["original"] + " A separate observation."}
    sources = [value.article, second]
    value.controller.selector = lambda *_args, **kwargs: next((copy.deepcopy(a) for a in sources
        if not any(r.get("url") == a["url"] for r in kwargs["ledger"]["records"])), None)
    original = value.quality

    def quality(record_id=None, review_only=False):
        result = original(record_id, review_only)
        record = value.cycle._record(value.cycle._load(), record_id)
        if record["url"] == value.article["url"]:
            result.reviewer.value = {"approved": False, "topic": "science",
                "issues": ["The translation omits the discovery of the stellar wind."], "warnings": []}
        return result

    monkeypatch.setattr(value.cycle, "_quality", quality)
    calls = []

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            context = json.loads(prompt.split("UNTRUSTED_JSON_DATA:\n", 1)[1])
            calls.append(context)
            if editor_result == "invalid_protocol":
                return "not json", {}
            output = patched(context, context["candidate_untrusted"]["body"].replace(" ", "  ", 1))
            return json.dumps(output, ensure_ascii=False), {}

    value.cycle.repair_factory = lambda trace: ArticleRepair(
        Editor(), prompt_dir=value.core.settings.root / "prompts", trace=trace)
    value.controller.prepare_once()
    records = value.cycle._load()["records"]
    assert [r["state"] for r in records] == ["cancelled", "quality_ready"]
    assert len(calls) == 1
    claim = records[0]["automatic_repair_attempts"][0]
    assert claim["state"] == ("not_repairable" if editor_result == "invalid_protocol" else "no_change")
    assert (value.core.settings.root / claim["repair_trace"]).is_file()
    assert not value.writes
    value.controller.run_once()
    assert len(value.writes) == 1 and value.cycle._load()["records"][1]["state"] == "pending"


@pytest.mark.parametrize("advance", [0, 300])
def test_expired_source_is_retained_and_healthy_reserve_is_sent(reserve_controller, advance):
    value = reserve_controller
    policy = value.cycle._policy()
    old = {**value.article, "published_at": value.stamp[0] - policy["max_source_age_hours"] * 3600 + 60}
    healthy = {**value.article, "url": "https://science.example.org/article/current/",
        "original": value.article["original"] + " A separate observation."}
    value.controller.selector = lambda *_args, **kwargs: next((copy.deepcopy(a) for a in (old, healthy)
        if not any(r.get("url") == a["url"] for r in kwargs["ledger"]["records"])), None)
    value.controller.prepare_once()
    before = copy.deepcopy(value.cycle._load()["records"])
    assert len(before) == 2 and all(r["state"] == "quality_ready" for r in before)
    first = before[0]
    raw = (value.core.settings.root / first["input_file"]).read_bytes()
    trace = (value.core.settings.root / first["quality_trace"]).read_bytes()
    value.stamp[0] += advance
    value.controller.run_once()
    records = value.cycle._load()["records"]
    assert records[0]["state"] != "quality_ready" and records[1]["state"] == "pending"
    assert records[0]["preparation_attempts"] == before[0]["preparation_attempts"]
    assert records[0]["quality_trace"] == before[0]["quality_trace"]
    assert (value.core.settings.root / first["input_file"]).read_bytes() == raw
    assert (value.core.settings.root / first["quality_trace"]).read_bytes() == trace
    assert len(value.model_calls) == 2 and len(value.writes) == 1
    assert healthy["url"] in value.writes[0][1] and old["url"] not in value.writes[0][1]


def test_postiz_pause_and_start_resume_exact_pending_reserve(reserve_controller):
    from max_postiz_agent.hub_models import Target
    value = reserve_controller
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    value.state.control("pause", value.state.read()["revision"], "test-owner")
    value.state.control("start", value.state.read()["revision"], "test-owner")
    value.controller.run_once()
    with value.core.db.session() as session:
        assert session.get(Target, "max").paused is False
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1
    value.core.publisher.mode = "browser"
    value.stamp[0] = record["due_at"] + 1
    assert value.core.dispatch_tick() == 1
    assert len(value.writes) == 1


def test_disabling_reserve_does_not_release_unknown_unbound_preparation(reserve_controller):
    value = reserve_controller
    claim = value.store.begin_preparation()
    value.store.finish_preparation(claim, "blocked_unknown", "SOURCE_REQUEST_UNKNOWN")
    value.state.control("pause", value.state.read()["revision"], "test-owner")
    control = value.state.read()
    settings = copy.deepcopy(control["settings"])
    settings["reserve"]["enabled"] = False
    value.state.settings(settings, control["revision"], "test-owner")
    value.state.control("start", value.state.read()["revision"], "test-owner")
    value.controller.selector = lambda *_args, **_kwargs: pytest.fail(
        "Смена режима не разрешает новую подготовку при неизвестном исходе")
    assert value.controller.run_once()["phase"] in {"blocked", "reserve_disabled"}
    assert value.store.active_preparation()["id"] == claim["id"]
    assert not value.writes and not value.model_calls


def test_dispatch_lease_appearing_after_selection_defers_unbound_preparation(reserve_controller):
    from max_postiz_agent.hub_models import BridgePost
    value = reserve_controller
    value.controller.prepare_once()
    first = value.cycle._load()["records"][0]
    second = {**value.article, "url": "https://science.example.org/article/after-lease/",
        "original": value.article["original"] + " A separate observation."}

    def selector(*_args, **_kwargs):
        # Известная мутация другого этапа началась, пока GET источника уже шёл.
        with value.core.db.session.begin() as session:
            session.add(BridgePost(post_id="inflight-post", integration_id="integration-max",
                group_id="inflight-group", target_id="max", variant_id=first["variant_id"],
                remote_hash="1" * 64, remote_state="DRAFT", due_at=value.stamp[0],
                mutation_owner="existing-mutation", mutation_until=value.stamp[0] + 90))
        return copy.deepcopy(second)

    value.controller.selector = selector
    result = value.controller.prepare_once()
    assert result["phase"] == "preparation_deferred"
    assert value.store.active_preparation() is None
    assert len(value.model_calls) == 1 and len(value.cycle._load()["records"]) == 1
    with value.core.db.session() as session:
        lease = session.get(BridgePost, "inflight-post")
        assert lease.mutation_owner == "existing-mutation" and lease.mutation_until == value.stamp[0] + 90
    assert not value.writes
