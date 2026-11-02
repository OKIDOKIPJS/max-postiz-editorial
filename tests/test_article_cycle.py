"""Scoped article phases never bypass Postiz or repeat an uncertain publication."""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft, Material, Story, now
from max_postiz_agent.hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant, VariantRevision
from max_postiz_agent.reddit import text_hash
from conftest import write_topic

pytest_plugins = ["test_max_bridge"]


def seed_legacy_dispatch(core, prepared):
    """Historical persisted state for refusal/reconciliation tests, never an approval path."""
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared['variant_id'])
        target = session.get(Target, variant.target_id)
        due = now() + 180
        snapshot = core.snapshot(variant, target)
        hashed = fingerprint({'content': snapshot, 'due_at': due})
        approval = TargetApproval(variant_id=variant.id, revision=variant.revision,
            snapshot_hash=hashed, scheduled_at=due, actor='hourly_article_policy')
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=variant.id, approval_id=approval.id,
            revision=variant.revision, snapshot_hash=hashed, snapshot=snapshot, due_at=due)
        session.add(dispatch)
        session.flush()
        return dispatch.id


class Quality:
    def prepare(self, title, original, url):
        translation = "Космический ветер\n\nУчёные изучили необычные процессы в атмосфере далёкой звезды."
        return {"title": "Космический ветер", "body": translation + "\n\nИсточник: " + url,
                "translation": translation, "original": title + "\n\n" + original, "url": url, "topic": "science"}

    def close(self):
        pass


@pytest.fixture
def cycle(bridge, monkeypatch):
    core, records, _, _, _ = bridge
    records.clear()
    core.settings.channel("main")["publication"].update(max_posts_per_day=24, min_gap_minutes=60)
    write_topic(core.settings.root, core.settings.topic)
    stamp = [int(now() // 3600) * 3600 + 1800]
    monkeypatch.setattr("max_postiz_agent.article_cycle.now", lambda: stamp[0])
    policy = {"enabled": True, "target_id": "max", "source_id": "example_rss",
              "allowed_hosts": ["science.example.org"], "rights_evidence": "test_permission",
              "owner_instruction": "2026-10-17 hourly translated article tests",
              "min_gap_seconds": 3600, "max_per_day": 24, "max_source_age_hours": 8760}
    data = core.settings.root / "data"
    data.mkdir(exist_ok=True)
    (data / "hourly-article-policy.local.json").write_text(json.dumps(policy))
    article = {"url": "https://science.example.org/article/first/", "title": "An unusual stellar wind",
        "original": "Scientists studied an unusual atmosphere and discovered a stellar wind in the distant star. " * 2,
        "published_at": stamp[0] - 3600}
    value = HourlyArticleCycle(core, Quality)
    calls = []

    def create(target, body, media, kind="draft", due_at=None):
        calls.append((target, body, media, kind, due_at))
        ident = "article-post-" + str(len(calls))
        records[ident] = {"id": ident, "content": body,
            "publishDate": datetime.fromtimestamp(due_at or stamp[0], UTC).isoformat(),
            "state": "QUEUE" if kind == "schedule" else "DRAFT", "image": "[]", "childrenPost": [],
            "intervalInDays": None, "group": "group-article",
            "integration": {"id": "integration-max", "providerIdentifier": "max-browser"},
            "settings": json.dumps(target.options.get("settings", {}))}
        return ident

    monkeypatch.setattr(core.postiz, "create", create)
    return value, core, article, records, calls, stamp, policy


def test_prepare_preserves_real_source_and_never_schedules_or_approves(cycle):
    value, core, article, records, calls, _, policy = cycle
    result = value.prepare(article)
    assert result["state"] == "prepared" and result["url"] == article["url"]
    assert not records and not calls
    with core.db.session() as session:
        material = session.scalar(select(Material))
        variant = session.get(Variant, result["variant_id"])
        assert material.url == article["url"] and material.source_id == policy["source_id"]
        assert material.published_at == article["published_at"]
        assert variant.sources[0]["id"] == material.id
        assert variant.metadata_json["rights_evidence"] == policy["rights_evidence"]
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


def test_hourly_policy_allows_archival_science_within_its_explicit_age_limit(cycle):
    value, _, article, _, calls, stamp, _ = cycle
    article['published_at'] = stamp[0] - 30 * 24 * 3600
    assert value.prepare(article)['state'] == 'prepared'
    assert not calls


def test_hourly_policy_rejects_source_age_above_its_limit(cycle):
    value, _, article, _, calls, stamp, policy = cycle
    policy["max_source_age_hours"] = 24 * 30
    article["published_at"] = stamp[0] - (24 * 30 + 1) * 3600
    source, target = value._source_target(policy)
    with pytest.raises(AgentError, match="STALE_SOURCE"):
        value._article(article, policy, source, target)
    assert not calls


def test_automatic_approval_is_disabled_even_with_the_correct_hash(cycle):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    for hashed in ('0' * 64, prepared['content_hash']):
        with pytest.raises(AgentError, match='HUMAN_REVIEW_CONFIRMATION_REQUIRED'):
            value.publish(prepared['id'], hashed)
    assert not calls
    with core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


def test_late_prepared_slot_is_not_caught_up(cycle):
    value, _, article, _, calls, stamp, _ = cycle
    prepared = value.prepare(article)
    stamp[0] += 3600
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    assert not calls


def test_claimed_slot_and_duplicate_original_are_durable(cycle):
    value, _, article, _, _, stamp, _ = cycle
    prepared = value.prepare(article)
    with pytest.raises(AgentError, match="ARTICLE_SLOT_ALREADY_CLAIMED"):
        value.prepare(article)
    ledger = value._load()
    ledger["records"][0]["state"] = "failed"
    value._save(ledger)
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.prepare({**article, "url": "https://science.example.org/article/second/"})
    assert value.status(prepared["id"])["variant_id"] == prepared["variant_id"]


def test_unknown_postiz_creation_requires_reconciliation(cycle, monkeypatch):
    value, core, article, _, _, stamp, policy = cycle
    policy['approval_mode'] = 'human'
    (core.settings.root / 'data/hourly-article-policy.local.json').write_text(json.dumps(policy))
    attempts = []
    def uncertain(*args):
        attempts.append(args)
        raise AgentError('POSTIZ_UNKNOWN', 503)
    monkeypatch.setattr(core.postiz, 'create', uncertain)
    with pytest.raises(AgentError, match='POSTIZ_UNKNOWN'):
        value.prepare(article)
    with core.db.session() as session:
        assert session.query(RemoteDraft).one().state == 'unknown'
        assert session.query(Dispatch).count() == 0
    stamp[0] += 3600
    with pytest.raises(AgentError, match='ARTICLE_RECONCILIATION_REQUIRED'):
        value.prepare({**article, 'url': 'https://science.example.org/second/'})
    assert len(attempts) == 1


def test_remote_text_change_during_draft_creation_requires_reconciliation(cycle, monkeypatch):
    value, core, article, records, calls, _, policy = cycle
    policy['approval_mode'] = 'human'
    (core.settings.root / 'data/hourly-article-policy.local.json').write_text(json.dumps(policy))
    original_create = core.postiz.create
    def edited(*args):
        ident = original_create(*args)
        records[ident]['content'] += ' Изменено в Postiz.'
        return ident
    monkeypatch.setattr(core.postiz, 'create', edited)
    with pytest.raises(AgentError, match='ARTICLE_REMOTE_CHANGED'):
        value.prepare(article)
    with core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
    assert len(calls) == 1




@pytest.mark.parametrize("change", [{"url": "https://other.example.org/"}, {"published_at": 1}, {"url": "https://science.example.org/article/?token=private"}])
def test_invalid_or_stale_inputs_never_claim_a_slot(cycle, change):
    value, _, article, _, _, _, _ = cycle
    with pytest.raises(AgentError):
        value.prepare({**article, **change})
    assert value._load()["records"] == []


def test_legacy_stage_default_still_creates_one_draft(cycle):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    core.stage_remote(prepared["variant_id"])
    assert len(calls) == 1 and calls[0][3] == "draft"
    with pytest.raises(AgentError, match="REMOTE_DRAFT_ALREADY_TRACKED"):
        core.stage_remote(prepared["variant_id"], due_at=now() + 60)


@pytest.mark.parametrize("due", [-1, float("nan"), float("inf"), True])
def test_stage_rejects_invalid_date_before_remote_mutation(cycle, due):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    with pytest.raises(AgentError, match="SCHEDULE_INVALID"):
        core.stage_remote(prepared["variant_id"], due_at=due)
    assert calls == []


def test_source_rights_are_required_before_generation(cycle):
    value, core, article, _, _, _, _ = cycle
    core.settings.source("example_rss")["rights"]["allow_derivative"] = False
    with pytest.raises(AgentError, match="ARTICLE_RIGHTS_REQUIRED"):
        value.prepare(article)
    assert value._load()["records"] == []


def test_generic_ingest_body_hash_prevents_new_headline_duplicate(cycle):
    value, core, article, _, _, _, _ = cycle
    with core.db.session.begin() as session:
        session.add(Material(source_id="example_rss", external_id="old-record", story_id="old-story",
            title="A completely different title", text=article["original"],
            url="https://science.example.org/old/", language="en",
            text_hash=fingerprint(" ".join(article["original"].casefold().split()))))
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.prepare(article)
    assert value._load()["records"] == []




def test_unknown_send_remains_nonterminal_and_cannot_publish_again(cycle):
    value, core, article, _, _, stamp, _ = cycle
    prepared = value.prepare(article)
    ident = seed_legacy_dispatch(core, prepared)
    with core.db.session.begin() as session:
        session.get(Dispatch, ident).state = 'unknown'
    assert value.status(prepared['id'])['state'] == 'unknown'
    with pytest.raises(AgentError, match='HUMAN_REVIEW_CONFIRMATION_REQUIRED'):
        value.publish(prepared['id'], prepared['content_hash'])
    stamp[0] += 3600
    with pytest.raises(AgentError, match='ARTICLE_OUTSTANDING_DELIVERY'):
        value.prepare({**article, 'url': 'https://science.example.org/other/'})


def failed_preparation(value, article):
    class FailingQuality(Quality):
        def prepare(self, *args):
            raise AgentError("OPENCODE_GENERATION_FAILED")
    value.quality_factory = FailingQuality
    with pytest.raises(AgentError, match="OPENCODE_GENERATION_FAILED"):
        value.prepare(article)
    record = value._load()["records"][-1]
    assert record["state"] == "needs_attention" and not record.get("variant_id")
    value.quality_factory = Quality
    return record


def test_explicit_retry_preserves_record_slot_source_and_attempt_history(cycle):
    value, core, article, records, calls, _, _ = cycle
    failed = failed_preparation(value, article)
    result = value.prepare(article, retry_record=failed["id"])
    assert result["state"] == "prepared"
    assert len(value._load()["records"]) == 1
    for key in ("id", "slot", "url", "original_hash", "policy_hash", "claimed_at"):
        assert result[key] == failed[key]
    assert result["preparation_attempts"][0]["error"] == "OPENCODE_GENERATION_FAILED"
    assert result["preparation_attempts"][1]["operator_retry"] is True
    assert result["preparation_attempts"][1]["state"] == "prepared"
    assert not records and not calls
    with core.db.session() as session:
        events = list(session.scalars(select(Audit).where(Audit.entity == failed["id"])))
        assert [event.action for event in events].count("hourly_article_prepare_attempt") == 2
        assert any(event.action == "hourly_article_prepare_error" for event in events)
        assert session.query(Variant).count() == 1 and session.query(Dispatch).count() == 0


def test_retry_migrates_failed_legacy_history_without_inventing_completion_time(cycle):
    value, _, article, _, _, _, _ = cycle
    failed = failed_preparation(value, article)
    ledger = value._load()
    ledger["records"][0].pop("preparation_attempts")
    ledger["records"][0].pop("input_hash")
    value._save(ledger)
    result = value.prepare(article, retry_record=failed["id"])
    prior = result["preparation_attempts"][0]
    assert prior["legacy_record"] and prior["error"] == "OPENCODE_GENERATION_FAILED"
    assert "finished_at" not in prior
    assert result["input_hash"] == fingerprint(article)


@pytest.mark.parametrize("change", [{"url": "https://science.example.org/another/"},
    {"original": "An entirely different article with a different source body about the distant stars and galaxies."},
    {"title": "A changed headline"}, {"published_at": -1}])
def test_retry_rejects_changed_input_without_new_attempt(cycle, change):
    value, _, article, _, _, _, _ = cycle
    failed = failed_preparation(value, article)
    with pytest.raises(AgentError):
        value.prepare({**article, **change}, retry_record=failed["id"])
    record = value._load()["records"][0]
    assert len(record["preparation_attempts"]) == 1 and record["state"] == "needs_attention"


def test_retry_rejects_policy_changes_and_expired_hour(cycle):
    value, core, article, _, _, stamp, policy = cycle
    failed = failed_preparation(value, article)
    path = core.settings.root / "data/hourly-article-policy.local.json"
    path.write_text(json.dumps({**policy, "owner_instruction": policy["owner_instruction"] + " changed"}))
    with pytest.raises(AgentError, match="ARTICLE_RETRY_INPUT_MISMATCH"):
        value.prepare(article, retry_record=failed["id"])
    path.write_text(json.dumps(policy))
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_SLOT_EXPIRED"):
        value.prepare(article, retry_record=failed["id"])
    assert len(value._load()["records"]) == 1


@pytest.mark.parametrize("phase", ["variant", "remote", "dispatch", "orphaned_ledger"])
def test_retry_never_replays_any_post_draft_phase(cycle, phase):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    if phase == "remote":
        core.stage_remote(prepared["variant_id"])
    elif phase == "dispatch":
        seed_legacy_dispatch(core, prepared)
    ledger = value._load()
    record = ledger["records"][0]
    record["state"] = "needs_attention"
    if phase == "orphaned_ledger":
        record.pop("variant_id")
        record.pop("prepared_hash")
    value._save(ledger)
    count = len(calls)
    with pytest.raises(AgentError, match="ARTICLE_RETRY_NOT_SAFE|ARTICLE_OUTSTANDING_DELIVERY"):
        value.prepare(article, retry_record=prepared["id"])
    assert len(calls) == count
    with core.db.session() as session:
        assert session.query(Variant).count() == 1


def test_private_trace_appends_only_in_ignored_data_without_stdout(cycle, capsys):
    value, core, _, _, _, _, _ = cycle
    ident = "11111111-2222-4333-8444-555555555555"
    trace = value._trace_writer(ident)
    trace({"phase": "translation_received", "raw_output": "private source and model output"})
    trace({"phase": "review_received", "raw_output": "private review"})
    path = core.settings.root / f"data/hourly-articles/quality-{ident}.local.jsonl"
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 2 and all(item["record_id"] == ident for item in entries)
    assert entries[0]["raw_output"] == "private source and model output"
    assert entries[1]["phase"] == "review_received"
    assert path.is_relative_to(core.settings.root / "data")
    assert not capsys.readouterr().out
    with pytest.raises(AgentError, match="ARTICLE_RECORD_ID_INVALID"):
        value._trace_writer("../../outside")


def test_default_quality_factory_wires_private_trace_without_model_requests(cycle, capsys):
    value, core, _, _, _, _, _ = cycle
    value.quality_factory = None
    ident = "11111111-2222-4333-8444-555555555555"
    quality = value._quality(ident)
    try:
        assert quality.translator.model == "proxmox/qwen35-9b"
        assert quality.reviewer.model == "proxmox/qwen25-coder-7b"
        quality._emit("translation_started", 1, context={"private": "source text"})
        path = core.settings.root / f"data/hourly-articles/quality-{ident}.local.jsonl"
        assert json.loads(path.read_text(encoding="utf-8"))["context"] == {"private": "source text"}
        assert not capsys.readouterr().out
    finally:
        quality.close()
        quality.translator.client.close()
        quality.reviewer.client.close()


def test_cancel_failed_preparation_preserves_history_and_consumes_same_slot(cycle):
    value, core, article, _, calls, stamp, _ = cycle
    failed = failed_preparation(value, article)
    result = value.cancel_preparation(failed["id"], "Owner reconciled the failed LLM call")
    assert result["state"] == "cancelled"
    for key in ("id", "slot", "url", "original_hash", "policy_hash", "claimed_at", "preparation_attempts"):
        assert result[key] == failed[key]
    assert result["preparation_actions"][-1]["previous_state"] == "needs_attention"
    assert calls == []
    with core.db.session() as session:
        assert session.query(Variant).count() == session.query(Dispatch).count() == 0
        assert session.scalar(select(Audit).where(Audit.action == "hourly_article_cancel_preparation"))
    with pytest.raises(AgentError, match="ARTICLE_SLOT_ALREADY_CLAIMED"):
        value.prepare({**article, "url": "https://science.example.org/next/", "original": article["original"] + " New discovery."})
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.prepare(article)
    assert value.prepare({**article, "url": "https://science.example.org/next/",
                          "original": article["original"] + " New discovery."})["state"] == "prepared"


@pytest.mark.parametrize("phase", ["prepared", "orphaned_ledger", "remote", "dispatch"])
def test_cancel_refuses_any_generated_or_external_state(cycle, phase):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    if phase == "remote":
        core.stage_remote(prepared["variant_id"])
    elif phase == "dispatch":
        seed_legacy_dispatch(core, prepared)
    ledger = value._load()
    if phase != "prepared":
        ledger["records"][0]["state"] = "needs_attention"
        if phase == "orphaned_ledger":
            ledger["records"][0].pop("variant_id")
            ledger["records"][0].pop("prepared_hash")
        value._save(ledger)
    count = len(calls)
    with pytest.raises(AgentError, match="ARTICLE_PREPARATION_NOT_CANCELLABLE|ARTICLE_PREPARATION_HAS_EXTERNAL_STATE"):
        value.cancel_preparation(prepared["id"])
    assert len(calls) == count


def test_cancel_cannot_interrupt_active_locked_generation(cycle):
    from max_postiz_agent.browser_runtime import profile_lock
    value, _, article, _, _, _, _ = cycle
    failed = failed_preparation(value, article)
    with profile_lock(value.directory / "cycle.lock"):
        with pytest.raises(AgentError, match="BROWSER_PROFILE_BUSY"):
            value.cancel_preparation(failed["id"])
    assert value._load()["records"][0]["state"] == "needs_attention"


@pytest.mark.parametrize("rollover", ["generation", "transaction"])
def test_preparation_crossing_hour_rolls_back_without_unusable_draft(cycle, monkeypatch, rollover):
    value, core, article, records, calls, stamp, _ = cycle
    if rollover == "generation":
        class SlowQuality(Quality):
            def prepare(self, *args):
                result = super().prepare(*args)
                stamp[0] += 3600
                return result
        value.quality_factory = SlowQuality
    else:
        original_history = core._history

        def slow_history(*args):
            original_history(*args)
            stamp[0] += 3600

        monkeypatch.setattr(core, "_history", slow_history)
    reserved_slot = int(stamp[0] // 3600)
    with pytest.raises(AgentError, match="ARTICLE_SLOT_EXPIRED"):
        value.prepare(article)
    record = value._load()["records"][0]
    assert record["slot"] == reserved_slot and record["state"] == "needs_attention"
    assert record["error"] == "ARTICLE_SLOT_EXPIRED" and not record.get("variant_id")
    assert record["preparation_attempts"][-1]["error"] == "ARTICLE_SLOT_EXPIRED"
    assert not records and not calls
    with core.db.session() as session:
        for model in (Story, Material, Draft, Variant, RemoteDraft, Dispatch, TargetApproval):
            assert session.query(model).count() == 0
    assert value.cancel_preparation(record["id"])["state"] == "cancelled"


def test_abandon_expired_prepared_retains_history_without_external_operations(cycle):
    value, core, article, records, calls, stamp, _ = cycle
    prepared = value.prepare(article)
    stamp[0] += 3600
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    abandoned = value.abandon_prepared(prepared["id"], "Expired after an operator review")
    assert abandoned["state"] == "cancelled" and abandoned["slot"] == prepared["slot"]
    assert abandoned["variant_id"] == prepared["variant_id"]
    assert abandoned["original_hash"] == prepared["original_hash"]
    assert not calls and not records
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert variant.state == "abandoned" and variant.revision == prepared["revision"] + 1
        assert session.get(Draft, variant.draft_id).state == "article_rejected"
        assert session.query(VariantRevision).count() == 2
        assert session.query(Story).count() == session.query(Material).count() == 1
        assert session.query(Dispatch).count() == session.query(RemoteDraft).count() == 0
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.prepare(article)
    class DifferentQuality(Quality):
        def prepare(self, *args):
            result = super().prepare(*args)
            result["translation"] += " Новая самостоятельная история."
            result["body"] += " Новая самостоятельная история."
            return result
    value.quality_factory = DifferentQuality
    assert value.prepare({**article, "url": "https://science.example.org/next/",
                          "original": article["original"] + " New scientific story."})["state"] == "prepared"


def test_abandon_keeps_consumed_slot_for_operator_rejected_text(cycle):
    value, _, article, _, _, _, _ = cycle
    prepared = value.prepare(article)
    value.abandon_prepared(prepared["id"], "Operator rejected the translated text")
    with pytest.raises(AgentError, match="ARTICLE_SLOT_ALREADY_CLAIMED"):
        value.prepare({**article, "url": "https://science.example.org/another/"})


@pytest.mark.parametrize("external", ["remote", "bridge", "dispatch", "mutation"])
def test_abandon_refuses_any_existing_external_state(cycle, external):
    value, core, article, _, _, _, _ = cycle
    prepared = value.prepare(article)
    if external == "dispatch":
        seed_legacy_dispatch(core, prepared)
    elif external == "remote":
        core.stage_remote(prepared["variant_id"])
    elif external in {"bridge", "mutation"}:
        with core.db.session.begin() as session:
            session.add(BridgePost(post_id="external-post", integration_id="integration-max",
                group_id="external-group", target_id="max", variant_id=prepared["variant_id"] if external == "bridge" else "other-variant",
                remote_hash="0" * 64, remote_state="DRAFT", due_at=now(),
                mutation_owner="operator-lease" if external == "mutation" else None))
    with pytest.raises(AgentError, match="ARTICLE_PREPARED_NOT_ABANDONABLE|ARTICLE_PREPARED_HAS_EXTERNAL_STATE"):
        value.abandon_prepared(prepared["id"])
    with core.db.session() as session:
        assert session.get(Variant, prepared["variant_id"]).state != "abandoned"


@pytest.mark.parametrize("changed", ["media", "sources", "target", "config", "binding", "metadata", "revision"])
def test_delegated_approval_rejects_any_unreviewed_content_bearing_change(cycle, monkeypatch, changed):
    value, core, article, _, calls, _, policy = cycle
    policy["approval_mode"] = "human"
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    sync = core.bridge.sync

    def changed_sync(*args, **kwargs):
        result = sync(*args, **kwargs)
        with core.db.session.begin() as session:
            variant = session.scalar(select(Variant))
            if changed == "media":
                variant.media = [{"path": "data/injected.png", "sha256": "0" * 64, "mime": "image/png"}]
            elif changed == "sources":
                variant.sources = [{**variant.sources[0], "url": "https://science.example.org/injected/"}]
            elif changed == "target":
                session.get(Target, "max").destination = "-456"
            elif changed == "config":
                variant.config_hash = "0" * 64
            elif changed == "binding":
                variant.binding_hash = "0" * 64
            elif changed == "metadata":
                variant.metadata_json = {**variant.metadata_json, "rights_evidence": "unreviewed permission"}
            else:
                variant.revision += 1
        return result

    monkeypatch.setattr(core.bridge, "sync", changed_sync)
    with pytest.raises(AgentError, match="ARTICLE_REMOTE_CHANGED"):
        value.prepare(article)
    assert len(calls) == 1
    with core.db.session() as session:
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0




class CandidateQuality(Quality):
    def check_candidate(self, title, body, original_title, original, url):
        translation = title + "\n\n" + body
        return {"title": title, "body": translation + "\n\nИсточник: " + url,
                "translation": translation, "original": original_title + "\n\n" + original,
                "url": url, "topic": "science"}


def test_editorial_correction_rechecks_and_preserves_identity_with_coherent_revisions(cycle):
    value, core, article, records, calls, _, _ = cycle
    prepared = value.prepare(article)
    value.quality_factory = CandidateQuality
    correction = {"title": "Наблюдения за звёздным ветром", "body": "Исследователи изучили атмосферу далёкой звезды и уточнили свойства звёздного ветра."}
    revised = value.revise_prepared(prepared["id"], correction)
    assert revised["state"] == "prepared" and revised["correction_blocked"] is False
    assert revised["id"] == prepared["id"] and revised["variant_id"] == prepared["variant_id"]
    assert revised["content_hash"] != prepared["content_hash"]
    assert revised["original_hash"] == prepared["original_hash"] and revised["slot"] == prepared["slot"]
    assert correction["body"] in revised["body"] and article["url"] in revised["body"]
    assert not calls and not records
    with core.db.session() as session:
        variant = session.get(Variant, revised["variant_id"])
        draft = session.get(Draft, variant.draft_id)
        material = session.get(Material, variant.sources[0]["id"])
        assert material.text == article["original"] and material.url == article["url"]
        assert variant.revision == draft.revision == variant.source_revision == 2
        assert draft.current["body"] == variant.body
        assert session.query(VariantRevision).count() == 2
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
        assert session.scalar(select(Audit).where(Audit.action == "hourly_article_revised_prepared"))
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        value.publish(revised["id"], prepared["content_hash"])
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        value.publish(revised["id"], revised["content_hash"])


def test_rejected_editorial_correction_keeps_original_but_blocks_publication_until_rechecked(cycle):
    value, _, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    class RejectedCandidate(CandidateQuality):
        def check_candidate(self, *args):
            raise AgentError("ARTICLE_QUALITY_REJECTED")
    value.quality_factory = RejectedCandidate
    correction = {"title": "Новый заголовок", "body": "Исправленный текст о звёздном ветре и свойствах атмосферы далёкой звезды."}
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.revise_prepared(prepared["id"], correction)
    rejected = value.status(prepared["id"])
    assert rejected["body"] == prepared["body"] and rejected["content_hash"] == prepared["content_hash"]
    assert rejected["correction_blocked"] and rejected["correction_attempts"][-1]["state"] == "rejected"
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    value.quality_factory = CandidateQuality
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        value.revise_prepared(prepared["id"], correction)
    revised = value.revise_prepared(prepared["id"], {**correction,
        "body": "Исследователи изучили необычную атмосферу далёкой звезды и обнаружили звёздный ветер."})
    assert revised["correction_blocked"] is False and len(revised["correction_attempts"]) == 2
    assert not calls


@pytest.mark.parametrize("external", ["remote", "dispatch", "mutation"])
def test_editorial_correction_refuses_existing_external_state_before_check(cycle, external):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    checked = []
    class SpyQuality(CandidateQuality):
        def check_candidate(self, *args):
            checked.append(args)
            return super().check_candidate(*args)
    value.quality_factory = SpyQuality
    if external == "remote":
        core.stage_remote(prepared["variant_id"])
    elif external == "dispatch":
        seed_legacy_dispatch(core, prepared)
    else:
        with core.db.session.begin() as session:
            session.add(BridgePost(post_id="external-edit", integration_id="integration-max", group_id="group-other",
                target_id="max", variant_id="other-variant", remote_hash="0" * 64, remote_state="DRAFT",
                due_at=now(), mutation_owner="operator-lease"))
    count = len(calls)
    with pytest.raises(AgentError, match="ARTICLE_CORRECTION_NOT_ALLOWED|ARTICLE_PREPARED_HAS_EXTERNAL_STATE"):
        value.revise_prepared(prepared["id"], {"title": "Правка", "body": "Корректный русский текст."})
    assert not checked and len(calls) == count


def test_editorial_correction_expiry_and_duplicate_do_not_mutate_prior_version(cycle):
    value, core, article, _, calls, stamp, _ = cycle
    prepared = value.prepare(article)
    correction = {"title": "Исправленный заголовок", "body": "Уточнённая история о наблюдениях учёных за звёздным ветром."}
    value.quality_factory = CandidateQuality
    ledger = value._load()
    ledger["records"].append({"id": "earlier", "slot": prepared["slot"] - 1, "state": "cancelled",
                              "translation_hash": text_hash(correction["title"] + "\n\n" + correction["body"])})
    value._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_TRANSLATION"):
        value.revise_prepared(prepared["id"], correction)
    class SlowCandidate(CandidateQuality):
        def check_candidate(self, *args):
            result = super().check_candidate(*args)
            stamp[0] += 3600
            return result
    value.quality_factory = SlowCandidate
    with pytest.raises(AgentError, match="ARTICLE_SLOT_EXPIRED"):
        value.revise_prepared(prepared["id"], {**correction, "body": correction["body"] + " Новый самостоятельный текст."})
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert variant.revision == 1 and variant.body == prepared["body"]
        assert session.get(Draft, variant.draft_id).revision == 1
    assert not calls
    assert value.abandon_prepared(prepared["id"])["state"] == "cancelled"


def test_operator_candidate_requires_existing_failed_pre_draft_retry(cycle):
    value, _, article, _, calls, _, _ = cycle
    candidate = {"title": "Проверенный русский заголовок", "body": "Проверенный русский текст о свойствах атмосферы и звёздного ветра."}
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_RETRY_REQUIRED"):
        value.prepare(article, candidate=candidate)
    assert not value._load()["records"] and not calls


def test_operator_candidate_retry_checks_original_without_retranslating(cycle):
    value, core, article, records, calls, _, _ = cycle
    failed = failed_preparation(value, article)
    checks = []
    class CheckOnly(CandidateQuality):
        def prepare(self, *args):
            pytest.fail("A supplied operator candidate must not be translated again")

        def check_candidate(self, *args):
            checks.append(args)
            return super().check_candidate(*args)
    value.quality_factory = CheckOnly
    candidate = {"title": "Уточнённый звёздный ветер", "body": "Исследователи уточнили особенности атмосферы далёкой звезды и свойства звёздного ветра."}
    result = value.prepare(article, retry_record=failed["id"], candidate=candidate)
    assert result["state"] == "prepared" and result["id"] == failed["id"] and result["slot"] == failed["slot"]
    assert result["original_hash"] == failed["original_hash"] and result["input_hash"] == failed["input_hash"]
    assert checks == [(candidate["title"], candidate["body"], article["title"], article["original"], article["url"])]
    attempt = result["preparation_attempts"][-1]
    assert attempt["operator_retry"] and attempt["operator_correction"]
    assert attempt["candidate_hash"] == fingerprint(candidate)
    assert not records and not calls
    with core.db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert variant.metadata_json["quality"] == "operator_ministral_languagetool"
        assert variant.metadata_json["operator_correction_hash"] == fingerprint(candidate)
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
        events = list(session.scalars(select(Audit).where(Audit.action == "hourly_article_prepare_attempt")))
        assert any(event.details.get("operator_correction") and event.details.get("candidate_hash") == fingerprint(candidate) for event in events)


def test_operator_candidate_rejection_keeps_slot_without_draft_or_automatic_retry(cycle):
    value, core, article, _, calls, _, _ = cycle
    failed = failed_preparation(value, article)
    checks = []
    class Rejected(CandidateQuality):
        def check_candidate(self, *args):
            checks.append(args)
            raise AgentError("TRANSLATION_NUMBER_CHANGED")
    value.quality_factory = Rejected
    with pytest.raises(AgentError, match="TRANSLATION_NUMBER_CHANGED"):
        value.prepare(article, retry_record=failed["id"], candidate={"title": "Правка", "body": "Неверный русский текст."})
    record = value._load()["records"][0]
    assert len(checks) == 1 and record["state"] == "needs_attention" and not record.get("variant_id")
    assert record["id"] == failed["id"] and record["slot"] == failed["slot"]
    assert len(record["preparation_attempts"]) == 2
    assert record["preparation_attempts"][-1]["error"] == "TRANSLATION_NUMBER_CHANGED"
    assert not calls
    with core.db.session() as session:
        assert session.query(Draft).count() == session.query(Variant).count() == 0


@pytest.mark.parametrize("phase", ["prepared", "remote", "dispatch"])
def test_operator_candidate_never_retries_generated_or_external_records(cycle, phase):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    if phase == "remote":
        core.stage_remote(prepared["variant_id"])
    elif phase == "dispatch":
        seed_legacy_dispatch(core, prepared)
    count = len(calls)
    with pytest.raises(AgentError, match="ARTICLE_RETRY_NOT_SAFE|ARTICLE_OUTSTANDING_DELIVERY"):
        value.prepare(article, retry_record=prepared["id"], candidate={"title": "Правка", "body": "Русский текст."})
    assert len(calls) == count


def candidate_failure(value, article, candidate, operation, error):
    record = failed_preparation(value, article) if operation == "prepare" else value.prepare(article)

    class Rejected(CandidateQuality):
        def check_candidate(self, *args):
            raise AgentError(error)

    value.quality_factory = Rejected
    with pytest.raises(AgentError, match=error):
        candidate_operation(value, article, record, candidate, operation)
    return value._load()["records"][0]


def candidate_operation(value, article, record, candidate, operation):
    if operation == "prepare":
        return value.prepare(article, retry_record=record["id"], candidate=candidate)
    return value.revise_prepared(record["id"], candidate)


@pytest.mark.parametrize("operation", ["prepare", "revise"])
@pytest.mark.parametrize("initial_padding", [False, True])
def test_substantive_candidate_refusal_is_durable_and_whitespace_cannot_resample_it(cycle, operation, initial_padding):
    value, core, article, records, calls, _, _ = cycle
    candidate = {"title": "Звёздный ветер", "body": "Учёные исследовали необычную атмосферу далёкой звезды."}
    supplied = {k: " \n" + v + "\n " for k, v in candidate.items()} if initial_padding else candidate
    failed = candidate_failure(value, article, supplied, operation, "ARTICLE_QUALITY_REJECTED")
    attempts_key = "preparation_attempts" if operation == "prepare" else "correction_attempts"
    before_attempts = failed[attempts_key]
    assert before_attempts[-1]["candidate_hash"] == fingerprint(candidate)
    assert before_attempts[-1]["candidate_raw_hash"] == fingerprint(supplied)
    checked = []

    def factory():
        checked.append(True)
        return CandidateQuality()

    # A new process-equivalent object reads refusal history from the private ledger.
    restarted = HourlyArticleCycle(core, factory)
    retry = candidate if initial_padding else {k: " " + v + " " for k, v in candidate.items()}
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        candidate_operation(restarted, article, failed, retry, operation)
    after = restarted._load()["records"][0]
    assert after[attempts_key] == before_attempts and after["state"] == failed["state"]
    assert after["preparation_actions"][-1]["action"] == "rejected_candidate_replay_blocked"
    assert after["preparation_actions"][-1]["candidate_normalized_hash"] == fingerprint(candidate)
    assert not checked and not calls and not records
    with core.db.session() as session:
        event = session.scalar(select(Audit).where(Audit.action == "hourly_article_candidate_replay_blocked"))
        assert event.details["candidate_normalized_hash"] == fingerprint(candidate)
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
        assert session.query(Variant).count() == (0 if operation == "prepare" else 1)


@pytest.mark.parametrize("operation", ["prepare", "revise"])
@pytest.mark.parametrize("first_markdown", [False, True])
def test_canonical_markdown_and_plaintext_candidate_refusal_cannot_resample(cycle, operation, first_markdown):
    from max_postiz_agent.article_quality import normalize_article_links
    value, core, article, records, calls, _, _ = cycle
    markdown = {"title": "Звёздный ветер", "body":
        "Учёные исследовали [атмосферу](https://science.example.org/atmosphere/) далёкой звезды."}
    plain = {key: normalize_article_links(text) for key, text in markdown.items()}
    assert markdown != plain
    supplied, equivalent = (markdown, plain) if first_markdown else (plain, markdown)
    failed = candidate_failure(value, article, supplied, operation, "ARTICLE_QUALITY_REJECTED")
    key = "preparation_attempts" if operation == "prepare" else "correction_attempts"
    assert failed[key][-1]["candidate_hash"] == fingerprint(plain)
    assert failed[key][-1]["candidate_raw_hash"] == fingerprint(supplied)

    def forbidden_factory():
        pytest.fail("An equivalent checked plaintext must not resample the reviewer")

    restarted = HourlyArticleCycle(core, forbidden_factory)
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        candidate_operation(restarted, article, failed, equivalent, operation)
    assert restarted._load()["records"][0][key] == failed[key]
    assert not records and not calls


def legacy_markdown_refusal(cycle):
    value, _, article, _, _, _, _ = cycle
    markdown = {"title": "Звёздный ветер", "body":
        "Учёные исследовали [атмосферу](https://science.example.org/atmosphere/) далёкой звезды."}
    candidate_failure(value, article, markdown, "prepare", "ARTICLE_QUALITY_REJECTED")
    ledger = value._load()
    record = ledger["records"][0]
    attempt = record["preparation_attempts"][-1]
    attempt.pop("candidate_normalization")
    attempt.pop("candidate_replay_hash")
    attempt.update(candidate_hash=fingerprint(markdown), candidate_raw_hash=fingerprint(markdown))
    value._save(ledger)
    trace = value.core.settings.root / record["quality_trace"]
    event = {"record_id": record["id"], "at": attempt["started_at"], "phase": "candidate_started",
             "candidate": markdown, "source_untrusted": {"title": article["title"],
             "body": article["original"], "url": article["url"]}}
    trace.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    return record, markdown, trace, event


@pytest.mark.parametrize("changed", [False, True])
def test_legacy_raw_markdown_refusal_uses_trace_for_canonical_comparison(cycle, changed):
    from max_postiz_agent.article_quality import normalize_article_links
    value, core, article, records, calls, _, _ = cycle
    failed, markdown, _, _ = legacy_markdown_refusal(cycle)
    candidate = {key: normalize_article_links(text) for key, text in markdown.items()}
    if changed:
        candidate["body"] += " Обнаружен звёздный ветер."
        result = HourlyArticleCycle(core, CandidateQuality).prepare(article,
            retry_record=failed["id"], candidate=candidate)
        assert result["state"] == "prepared"
        assert result["preparation_attempts"][-2]["candidate_hash"] == fingerprint(markdown)
    else:
        def forbidden_factory():
            pytest.fail("Historical canonical refusal must not resample")
        with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
            HourlyArticleCycle(core, forbidden_factory).prepare(article,
                retry_record=failed["id"], candidate=candidate)
    assert not records and not calls


@pytest.mark.parametrize("corruption", ["missing", "source", "candidate", "malformed"])
def test_unverifiable_legacy_candidate_refusal_fails_before_inference(cycle, corruption):
    from max_postiz_agent.article_quality import normalize_article_links
    value, core, article, records, calls, _, _ = cycle
    failed, markdown, trace, event = legacy_markdown_refusal(cycle)
    if corruption == "missing":
        trace.unlink()
    elif corruption == "malformed":
        trace.write_text("not json", encoding="utf-8")
    else:
        if corruption == "source":
            event["source_untrusted"]["body"] += " A different English source."
        else:
            event["candidate"]["body"] += " Другой русский текст."
        trace.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    candidate = {key: normalize_article_links(text) for key, text in markdown.items()}

    def forbidden_factory():
        pytest.fail("Missing historical evidence must not permit new inference")

    with pytest.raises(AgentError, match="ARTICLE_REJECTED_CANDIDATE_TRACE_REQUIRED"):
        HourlyArticleCycle(core, forbidden_factory).prepare(article, retry_record=failed["id"], candidate=candidate)
    assert value._load()["records"][0]["preparation_attempts"] == failed["preparation_attempts"]
    assert not records and not calls


@pytest.mark.parametrize("operation", ["prepare", "revise"])
@pytest.mark.parametrize("error", ["OPENCODE_GENERATION_FAILED", "ARTICLE_SPELLING_UNAVAILABLE", "ARTICLE_TRACE_FAILED"])
def test_infrastructure_failure_does_not_blacklist_the_same_candidate(cycle, operation, error):
    value, core, article, records, calls, _, _ = cycle
    candidate = {"title": "Звёздный ветер", "body": "Учёные исследовали необычную атмосферу далёкой звезды."}
    failed = candidate_failure(value, article, candidate, operation, error)
    restarted = HourlyArticleCycle(core, CandidateQuality)
    result = candidate_operation(restarted, article, failed, candidate, operation)
    assert result["state"] == "prepared" and not result.get("correction_blocked")
    attempts_key = "preparation_attempts" if operation == "prepare" else "correction_attempts"
    assert result[attempts_key][-2]["error"] == error
    assert result[attempts_key][-1]["candidate_hash"] == fingerprint(candidate)
    assert not any(x["action"] == "rejected_candidate_replay_blocked" for x in result.get("preparation_actions", []))
    assert not calls and not records


def test_preparation_refusal_also_blocks_later_revision_after_a_different_candidate_passed(cycle):
    value, core, article, _, calls, _, _ = cycle
    candidate = {"title": "Звёздный ветер", "body": "Учёные исследовали необычную атмосферу далёкой звезды."}
    failed = candidate_failure(value, article, candidate, "prepare", "ARTICLE_QUALITY_REJECTED")
    restarted = HourlyArticleCycle(core, CandidateQuality)
    corrected = {**candidate, "body": "Учёные изучили необычную атмосферу далёкой звезды и обнаружили звёздный ветер."}
    prepared = restarted.prepare(article, retry_record=failed["id"], candidate=corrected)
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        HourlyArticleCycle(core, CandidateQuality).revise_prepared(prepared["id"], candidate)
    with core.db.session() as session:
        assert session.get(Variant, prepared["variant_id"]).body == prepared["body"]
        assert session.query(VariantRevision).count() == 1
    assert not calls


@pytest.mark.parametrize("malformed", [False, True])
def test_legacy_refusal_hash_is_respected_and_corrupt_hash_fails_closed(cycle, malformed):
    value, core, article, _, calls, _, _ = cycle
    candidate = {"title": "Звёздный ветер", "body": "Учёные исследовали необычную атмосферу далёкой звезды."}
    failed = candidate_failure(value, article, candidate, "prepare", "ARTICLE_QUALITY_REJECTED")
    ledger = value._load()
    attempt = ledger["records"][0]["preparation_attempts"][-1]
    attempt.pop("candidate_raw_hash")
    if malformed:
        attempt["candidate_hash"] = "not-a-valid-content-hash"
    value._save(ledger)
    checked = []

    def factory():
        checked.append(True)
        return CandidateQuality()

    error = "ARTICLE_LEDGER_INVALID" if malformed else "ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"
    with pytest.raises(AgentError, match=error):
        HourlyArticleCycle(core, factory).prepare(article, retry_record=failed["id"], candidate=candidate)
    assert not checked and not calls


@pytest.mark.parametrize("origin", ["automatic", "candidate", "candidate_then_infrastructure"])
def test_quality_refusal_requires_an_exact_candidate_instead_of_blind_translation_retry(cycle, origin):
    value, core, article, _, calls, _, _ = cycle
    candidate = {"title": "Звёздный ветер", "body": "Учёные исследовали необычную атмосферу далёкой звезды."}
    if origin.startswith("candidate"):
        failed = candidate_failure(value, article, candidate, "prepare", "ARTICLE_QUALITY_REJECTED")
        if origin == "candidate_then_infrastructure":
            class Unavailable(CandidateQuality):
                def check_candidate(self, *args):
                    raise AgentError("ARTICLE_SPELLING_UNAVAILABLE")

            value.quality_factory = Unavailable
            changed = {**candidate, "body": candidate["body"] + " Обнаружен звёздный ветер."}
            with pytest.raises(AgentError, match="ARTICLE_SPELLING_UNAVAILABLE"):
                value.prepare(article, retry_record=failed["id"], candidate=changed)
            failed = value._load()["records"][0]
            assert failed["error"] == "ARTICLE_SPELLING_UNAVAILABLE"
    else:
        class Rejected(Quality):
            def prepare(self, *args):
                raise AgentError("ARTICLE_QUALITY_REJECTED")

        value.quality_factory = Rejected
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            value.prepare(article)
        failed = value._load()["records"][0]
    restarted = HourlyArticleCycle(core, CandidateQuality)
    with pytest.raises(AgentError, match="ARTICLE_CORRECTION_CANDIDATE_REQUIRED"):
        restarted.prepare(article, retry_record=failed["id"])
    assert restarted._load()["records"][0]["preparation_attempts"] == failed["preparation_attempts"]
    assert not calls
