"""Независимые пополнение проверенного резерва и короткий проход отправителя.

Модели работают только в prepare_once. Отправитель использует прежние exact
approval/dispatch и не освобождает слот при неизвестном внешнем результате.
"""
from sqlalchemy import select

from .article_cycle import HourlyArticleCycle
from .article_reserve import ArticleReserveStore, KNOWN_IMAGE_REJECTIONS, RESERVE_CONTRACT, ready_for_policy
from .config import AgentError
from .hub_models import Dispatch, DispatchAttempt
from .server_cycle import ServerCycle, TERMINAL
from .server_state import validate_settings


class PreparationState:
    """Одна панель показывает подготовку отдельно от отправки."""
    def __init__(self, state):
        self.state = state

    def read(self):
        return self.state.read()

    def assert_running(self):
        self.state.assert_running()

    def phase(self, phase, **details):
        self.state.progress("preparation", phase, **details)


class ReserveServerCycle(ServerCycle):
    def __init__(self, core, state=None, selector=None, *, cycle=None, clock=None,
                 reserve_store=None):
        super().__init__(core, state, selector,
            cycle=cycle or HourlyArticleCycle(core, reserve_mode=True), clock=clock)
        self.cycle.reserve_mode = True
        self.reserve = reserve_store or ArticleReserveStore(core)
        self._refilling = False

    def _phase(self, phase, **details):
        if "next_run_at" in details:
            self.state.phase(phase, **details)
            return {"phase": phase, **details}
        return super()._phase(phase, **details)

    def _configuration(self):
        control = self.state.read()
        settings = validate_settings(control["settings"])
        return control, settings

    def _due(self, settings, policy):
        interval = max(settings["interval_seconds"], policy["min_gap_seconds"])
        earliest = self.cycle._cadence(policy, self.clock(), check_gap=False)
        delivered = self.reserve.status(policy["target_id"])["last_delivered_at"]
        if delivered is not None:
            earliest = max(earliest, delivered + interval + 1)
        return self.reserve.next_available_due(policy["target_id"], max(self.clock() + 120, earliest))

    def _observe_slot(self, slot, record):
        # MAX delivery сохраняется до GET Postiz: сбой его API не разрешает замену.
        if record.get("variant_id"):
            with self.core.db.session() as session:
                dispatch = session.scalar(select(Dispatch).where(
                    Dispatch.variant_id == record["variant_id"]))
                attempt = session.scalar(select(DispatchAttempt).where(
                    DispatchAttempt.dispatch_id == dispatch.id,
                    DispatchAttempt.result == "published").order_by(
                        DispatchAttempt.created_at.desc())) if dispatch else None
                if dispatch and dispatch.state == "published" and dispatch.verified and attempt:
                    slot = self.reserve.update(slot["id"], "delivered", token=slot["token"],
                        dispatch_id=dispatch.id, delivered_at=attempt.created_at)
        result = self.cycle.status(record["id"])
        if (result.get("state") == "published" and result.get("delivery_verified") is True
                and result.get("postiz_state") == "PUBLISHED"):
            self.reserve.update(slot["id"], "published", token=slot["token"],
                dispatch_id=result["dispatch_id"])
            return self._observed_phase("published", record, result)
        if result.get("state") == "failed" and result.get("error") == "BROWSER_PRECLICK_FAILED":
            # Только новый durable proof допускает отдельное закрытие без send.
            # Старые failed/unknown и отказы без журнала остаются заблокированы.
            revision = self.state.read()["revision"]
            remote = self.core.postiz.get_post(result.get("postiz_id") or record.get("postiz_id"))
            try:
                self.reserve.release_preclick_failed_no_send(slot["id"], token=slot["token"],
                    remote=remote, expected_revision=revision)
            except AgentError as exc:
                if exc.code != "ARTICLE_RESERVE_PRECLICK_NOT_SAFE":
                    raise
                return self._phase("blocked", record_id=record["id"],
                    last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
            return self._phase("reconciling", record_id=record["id"], slot_id=slot["id"],
                slot_state="missed", last_error=None)
        if result.get("state") == "cancelled":
            # ERROR до единственной попытки MAX — известная отмена, а не
            # неизвестная доставка. Свежий GET и текущий fence позволяют
            # закрыть только этот слот; отменённый источник остаётся занятым.
            revision = self.state.read()["revision"]
            remote = self.core.postiz.get_post(result.get("postiz_id") or record.get("postiz_id"))
            try:
                self.reserve.release_cancelled_no_send(slot["id"], token=slot["token"],
                    remote=remote, expected_revision=revision)
            except AgentError as exc:
                if exc.code != "ARTICLE_RESERVE_CANCELLED_NOT_SAFE":
                    raise
                return self._phase("blocked", record_id=record["id"],
                    last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
            # Следующий проход выберет другой источник; промежуточный статус
            # использует существующую русскую подпись сверки в Postiz.
            return self._phase("reconciling", record_id=record["id"], slot_id=slot["id"],
                slot_state="missed", last_error=None)
        if result.get("state") in {"unknown", "failed", "published_unverified"}:
            self.reserve.update(slot["id"], "blocked_unknown", token=slot["token"],
                dispatch_id=result.get("dispatch_id"), error="ARTICLE_RECONCILIATION_REQUIRED")
            return self._phase("blocked", record_id=record["id"],
                last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        return self._observed_phase("waiting_dispatch", record, result, due_at=slot["due_at"])

    def _schedule(self, slot, record):
        self.state.assert_running()
        try:
            if record["state"] == "quality_ready":
                self.cycle.stage_reserve(record["id"])
                record = self.cycle._record(self.cycle._load(), record["id"])
            if record["state"] != "automatic_ready":
                raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)
            self._phase("scheduling", record_id=record["id"], due_at=slot["due_at"], last_error=None)
            result = self.cycle.publish(record["id"], record["prepared_hash"], due_at=slot["due_at"])
            self.reserve.update(slot["id"], "pending", token=slot["token"],
                dispatch_id=result["dispatch_id"])
        except Exception:
            self.reserve.update(slot["id"], "blocked_unknown", token=slot["token"],
                error="ARTICLE_RECONCILIATION_REQUIRED")
            raise
        return self._phase("waiting_dispatch", record_id=record["id"], result=result,
            due_at=slot["due_at"], last_error=None)

    def run_once(self):
        control, settings = self._configuration()
        if control["paused"]:
            return {"phase": "paused"}
        ledger = self.cycle._load()
        if (not settings["reserve"]["enabled"] and not self.reserve.status()["active"]
                and self.reserve.active_preparation() is None
                and not any(r.get("reserve_contract") == RESERVE_CONTRACT
                    and r["state"] not in TERMINAL for r in ledger["records"])):
            # Старый режим сохраняет собственные locks, час и контракт подготовки.
            return ServerCycle(self.core, self.state, self.selector,
                cycle=HourlyArticleCycle(self.core), clock=self.clock).run_once()
        self.state.assert_running()
        policy = self.cycle._policy()
        if policy.get("approval_mode") != "automatic":
            raise AgentError("ARTICLE_AUTOMATIC_POLICY_REQUIRED", 409)
        active, leased = self._inventory()
        slots = self.reserve.status(policy["target_id"])["active"]
        if leased or any(d["state"] in {"sending", "unknown"} for d in active):
            return self._phase("blocked", last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        if slots:
            if len(slots) != 1:
                raise AgentError("ARTICLE_RESERVE_ASSIGNMENT_LOST", 409)
            slot = self.reserve.get(slots[0]["id"])
            record = self.cycle._record(ledger, slot["record_id"])
            if slot["state"] == "assigned" and record["state"] in {"quality_ready", "automatic_ready"}:
                # Сохранённый staging marker отделяет ещё не начатый вызов от неизвестного.
                return self._schedule(slot, record)
            if record.get("dispatch_id") or slot.get("dispatch_id"):
                if len(active) == 1 and active[0]["state"] == "pending":
                    self._resume_pending(record, active[0]["id"], settings, policy)
                    slot = self.reserve.get(slot["id"])
                return self._observe_slot(slot, record)
            return self._phase("blocked", record_id=record["id"],
                last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        # Исторический exact dispatch заканчивается прежним протоколом; не мигрируем его в резерв.
        if active:
            record = next((r for r in ledger["records"] if any(
                d["variant_id"] == r.get("variant_id") for d in active)), None)
            if not record:
                raise AgentError("ARTICLE_OUTSTANDING_DELIVERY", 409)
            if len(active) == 1 and active[0]["state"] == "pending":
                self._resume_pending(record, active[0]["id"], settings, policy)
            return self._observed_phase("waiting_dispatch", record, self.cycle.status(record["id"]))
        for record in ledger["records"]:
            if record["state"] in TERMINAL:
                continue
            if (record.get("reserve_contract") == RESERVE_CONTRACT and not record.get("postiz_id")
                    and (record["state"] == "quality_ready" or
                         not any(record.get(k) for k in ("variant_id", "dispatch_id", "due_at"))
                         and self.reserve.active_preparation() is not None)):
                # Незавершённые модели изолированы своим claim; готовая очередь не ждёт inference.
                continue
            if record["state"] == "automatic_ready":
                return self._ready(record, settings, policy)
            if record.get("dispatch_id"):
                return self._observed_phase("reconciling", record, self.cycle.status(record["id"]))
            if self._close_known_rejection(record):
                return self._phase("rejected", record_id=record["id"])
            return self._phase("blocked", record_id=record["id"],
                last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        candidates = [r for r in ledger["records"] if ready_for_policy(r, policy)]
        if not settings["reserve"]["enabled"]:
            return self._phase("reserve_disabled")
        for record in candidates:
            claim = self.reserve.active_preparation()
            if claim and claim.get("record_id") == record["id"]:
                continue
            if self.reserve.disqualify_semantic_ready(record["id"], cycle=self.cycle):
                continue
            # Любая ошибка exact доказательства блокирует этот материал до отдельной сверки.
            due = self._due(settings, self.cycle._policy_for_record(record))
            if due > self.clock() + 3700:
                return self._phase("waiting_interval", earliest_due_at=due, last_error=None)
            try:
                validated = self.cycle.validate_reserve(record["id"], due_at=due)
            except AgentError as exc:
                if exc.code != "STALE_SOURCE":
                    raise
                # Только доказанное истечение возраста при целых исходных QA/trace.
                # Повреждённое evidence таким исключением не снимается.
                self.cycle.invalidate_reserve(record["id"], "STALE_SOURCE", due_at=due)
                continue
            fresh = self.cycle._record(self.cycle._load(), record["id"])
            slot = self.reserve.assign(fresh, due, text_hash=fresh["reserve_prepared_hash"],
                evidence_hash=validated["reserve_evidence_hash"],
                min_gap_seconds=self.cycle._policy_for_record(record)["min_gap_seconds"])
            return self._schedule(slot, fresh)
        return self._phase("reserve_empty", next_run_at=self.clock() + 30,
            last_error={"code": "ARTICLE_RESERVE_EMPTY"})

    def _quarantine_completed_review(self, claim, record):
        # Положительный durable proof отделён от обычного отказа и от unknown.
        # Проверка повторяется внутри короткой транзакции, без новых моделей.
        if (record.get("review_protocol_contract") == "article-review-bounds-v2"
                and record.get("preparation_attempts")
                and record["preparation_attempts"][-1].get("completed_review_protocol")):
            self.reserve.quarantine_completed_review(claim, record["id"], cycle=self.cycle)
            return True
        return False

    def _finish_candidate(self, claim, record):
        for _ in range(2):
            if self._quarantine_completed_review(claim, record):
                return "protocol_rejected"
            if record["state"] == "quality_ready":
                if self.reserve.disqualify_semantic_ready(record["id"], cycle=self.cycle, claim=claim):
                    return "semantic_rejected"
                self.reserve.finish_preparation(claim, "quality_ready")
                return "quality_ready"
            status = self._automatic_repair_status(record)
            if not status["eligible"]:
                break
            self.state.assert_running()
            try:
                self.cycle.repair_rejected(record["id"])
            except AgentError as exc:
                if exc.code != "ARTICLE_QUALITY_REJECTED":
                    fresh = self.cycle._record(self.cycle._load(), record["id"])
                    stopped = self._automatic_repair_status(fresh)
                    if (stopped["reason"] not in {"NOT_REPAIRABLE", "NO_CHANGE"}
                            or not self._known_rejection(fresh)):
                        raise
            record = self.cycle._record(self.cycle._load(), record["id"])
        if self._quarantine_completed_review(claim, record):
            return "protocol_rejected"
        if record["state"] == "quality_ready":
            if self.reserve.disqualify_semantic_ready(record["id"], cycle=self.cycle, claim=claim):
                return "semantic_rejected"
            self.reserve.finish_preparation(claim, "quality_ready")
            return "quality_ready"
        status = self._automatic_repair_status(record)
        if (not status["eligible"] and status["reason"] in {"EXHAUSTED", "NOT_REPAIRABLE", "NO_CHANGE", "MODEL_INPUT_BUDGET"}
                and self._known_rejection(record)):
            if status["reason"] == "MODEL_INPUT_BUDGET":
                from .article_repair_cycle import AutomaticArticleRepair
                AutomaticArticleRepair(self.cycle).record_input_budget(record["id"], status["model_input"])
            self.cycle.cancel_preparation(record["id"],
                "Reserve policy preserves the known rejection; compact editor input exceeds its pre-request budget"
                if status["reason"] == "MODEL_INPUT_BUDGET" else
                "Reserve policy preserves the completed rejection and both durable repair claims")
            self.reserve.finish_preparation(claim, "rejected", "ARTICLE_QUALITY_REJECTED")
            return "rejected"
        raise AgentError("ARTICLE_RECONCILIATION_REQUIRED", 409)

    def prepare_once(self):
        control, settings = self._configuration()
        if control["paused"]:
            return {"phase": "paused"}
        options = settings["reserve"]
        if not options["enabled"]:
            return {"phase": "reserve_disabled"}
        self.state.assert_running()
        for ready in self.cycle._load()["records"]:
            if ready.get("state") == "quality_ready" and ready.get("reserve_contract") == RESERVE_CONTRACT:
                if self.reserve.disqualify_semantic_ready(ready["id"], cycle=self.cycle):
                    self._refilling = True
        active_claim = self.reserve.active_preparation()
        if active_claim:
            record = next((r for r in self.cycle._load()["records"]
                           if r.get("id") == active_claim.get("record_id")), None)
            # После рестарта можно завершить только тот же законченный verdict,
            # закреплённый до ошибки. Возраст claim ничего не разрешает.
            original_claim = record.get("reserve_claim") if record else None
            if (not isinstance(original_claim, dict)
                    or active_claim.get("record_id") != record["id"]
                    or any(original_claim.get(key) != active_claim.get(key)
                           for key in ("id", "token", "control_revision"))
                    or not self._quarantine_completed_review(original_claim, record)):
                return self._phase("blocked", last_error={"code": "ARTICLE_RESERVE_PREPARATION_ACTIVE"})
        active, leased = self._inventory()
        if leased or any(d["state"] in {"sending", "unknown"} for d in active):
            return self._phase("blocked", last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        policy = self.cycle._policy()
        if policy.get("approval_mode") != "automatic":
            raise AgentError("ARTICLE_AUTOMATIC_POLICY_REQUIRED", 409)
        for previous in self.cycle._load()["records"]:
            if previous["state"] in TERMINAL:
                continue
            if (previous.get("reserve_contract") == RESERVE_CONTRACT
                    and self.cycle._reserve_other_safe(previous)):
                continue
            if previous.get("reserve_contract") != RESERVE_CONTRACT and self._close_known_rejection(previous):
                continue
            return self._phase("blocked", record_id=previous["id"],
                last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
        count = sum(ready_for_policy(r, policy) for r in self.cycle._load()["records"])
        runtime = control.get("runtime", {}).get("preparation", {})
        if runtime.get("next_run_at", 0) and runtime["next_run_at"] > self.clock():
            return {"phase": "waiting_source", "next_run_at": runtime["next_run_at"]}
        self._refilling = self._refilling or runtime.get("refilling", False) or count <= options["low"]
        if count >= options["maximum"] or count >= options["target"] or not self._refilling:
            self._refilling = False
            return self._phase("reserve_satisfied", reserve_count=count, refilling=False, last_error=None)
        completed = []
        for _ in range(options["batch"]):
            self.state.assert_running()
            active, leased = self._inventory()
            if leased or any(d["state"] in {"sending", "unknown"} for d in active):
                return self._phase("blocked", refilling=True,
                    last_error={"code": "ARTICLE_RECONCILIATION_REQUIRED"})
            if sum(ready_for_policy(r, policy) for r in self.cycle._load()["records"]) >= options["target"]:
                break
            claim = self.reserve.begin_preparation(expected_revision=control["revision"])
            try:
                self._phase("selecting", refilling=True, claim_id=claim["id"], last_error=None)
                article = self._select(settings, self.cycle._load(), policy)
                if article is None:
                    self.reserve.finish_preparation(claim, "no_source")
                    return self._phase("no_source", refilling=True, next_run_at=self.clock() + 300)
                self._phase("preparing", refilling=True, source_url=article["url"])
                try:
                    result = self.cycle.prepare(article, reserve_claim=claim)
                    record = self.cycle._record(self.cycle._load(), result["id"])
                except AgentError as exc:
                    bound = self.reserve.active_preparation()
                    if (bound and bound["id"] == claim["id"] and bound.get("record_id") is None
                            and exc.code in {"ARTICLE_OUTSTANDING_DELIVERY", "ARTICLE_RECONCILIATION_REQUIRED"}):
                        # Отправитель мог начать известную мутацию во время GET источника.
                        # prepare ещё не привязал запись и не вызвал модель: это отсрочка,
                        # а не неизвестный inference. Внешний lease здесь не меняется.
                        self.reserve.finish_preparation(claim, "no_source", exc.code)
                        return self._phase("preparation_deferred", refilling=True,
                            next_run_at=self.clock() + 30, last_error={"code": exc.code})
                    if (exc.code in KNOWN_IMAGE_REJECTIONS and bound
                            and bound["id"] == claim["id"] and bound.get("record_id")):
                        self.reserve.reject_image_preparation(claim, bound["record_id"], exc.code)
                        completed.append("image_rejected")
                        continue
                    if (exc.code == "TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT" and bound
                            and bound["id"] == claim["id"] and bound.get("record_id")):
                        self.reserve.reject_translation_preflight(claim, bound["record_id"], exc, cycle=self.cycle)
                        completed.append("translation_preflight_rejected")
                        continue
                    if exc.code != "ARTICLE_QUALITY_REJECTED" or not bound or not bound.get("record_id"):
                        raise
                    record = self.cycle._record(self.cycle._load(), bound["record_id"])
                completed.append(self._finish_candidate(claim, record))
            except Exception as exc:
                # Claim остаётся занят: ни рестарт, ни новый источник не повторят неизвестный запрос.
                code = exc.code if isinstance(exc, AgentError) else "ARTICLE_RESERVE_PREPARATION_INTERRUPTED"
                if code == "EDITORIAL_PAUSED":
                    unbound = self.reserve.active_preparation()
                    if (unbound and unbound["id"] == claim["id"] and unbound["state"] == "selecting"
                            and unbound["record_id"] is None):
                        self.reserve.finish_unbound_selection_on_pause(claim,
                            expected_revision=self.state.read()["revision"])
                        return {"phase": "paused"}
                self.reserve.finish_preparation(claim, "blocked_unknown", code)
                raise
        return self._phase("reserve_refilled", refilling=True, completed=completed, last_error=None)
