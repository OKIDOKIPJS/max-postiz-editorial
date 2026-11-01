"""Авторский ввод не выдаёт согласование и не наследует source/hourly исключения."""

import hashlib
import json

import pytest
from PIL import Image
from sqlalchemy import func, select

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Draft, DraftRevision, Material, Story
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant, VariantRevision
from max_postiz_agent.isolated_workspace import mark_isolated
from max_postiz_agent.owner_original import (
    ACTOR, CONTRACT, ORIGIN, attestation_hash, create_original, read_original_file,
)
from max_postiz_agent.platform_management import preflight
from max_postiz_agent.routes_cli import main
from test_hub import variants

pytest_plugins = ["test_four_platform_hub"]


@pytest.fixture
def original_core(four_hub, monkeypatch):
    # SQLite-копия не наследует endpoint реальных PG tests из общего запуска.
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    root = four_hub.settings.root
    mark_isolated(root, root.parent / "separate-source-reference")
    return four_hub


def content(**changes):
    return {"title": "Авторская заметка", "body": "Оригинальный текст владельца. В этой записи 123 — собственный пример, а не перевод внешней статьи.",
            "media": [], "metadata": {}, **changes}


def counts(core):
    with core.db.session() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (Story, Draft, DraftRevision, Variant, VariantRevision, Material,
                              TargetApproval, Dispatch)}


def image(core, name="original.png", color="blue"):
    path = core.settings.root / "data/media" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path)
    return {"path": path.relative_to(core.settings.root).as_posix(), "mime": "image/png",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_create_preserves_exact_text_and_stores_complete_canonical_chain_without_approval(original_core):
    core = original_core
    core.pause_target("telegram", True)
    before = counts(core)
    supplied = content(media=[image(core)])
    result = create_original(core, "telegram", supplied, attest_original=True)
    after = counts(core)
    for name in ("stories", "drafts", "draft_revisions", "hub_variants", "hub_variant_revisions"):
        assert after[name] == before[name] + 1
    for name in ("materials", "hub_approvals", "hub_dispatches"):
        assert after[name] == before[name]
    assert result["approval_created"] is False and result["external_mutations"] == 0
    with core.db.session() as session:
        value = session.get(Variant, result["variant_id"])
        assert (value.title, value.body, value.media) == (supplied["title"], supplied["body"], supplied["media"])
        assert value.sources == [] and value.state == "review_pending"
        assert value.metadata_json["origin"] == ORIGIN
        assert "original_attested_hash" not in value.metadata_json
        assert value.metadata_json["owner_original_attested_hash"] == attestation_hash(value.title, value.body, value.media)
        assert session.get(Target, "telegram").paused is True
        audit = session.scalar(select(Audit).where(Audit.entity == value.id, Audit.action == "owner_original_attested"))
        assert audit.actor == ACTOR and audit.details["original_rights_attested"] is True
        assert audit.details["approval_granted"] is False and audit.details["model_verified"] is False
    assert all(not p.publications and not p.inspections and not p.validations for p in core.fixture_publishers.values())


def test_original_preflight_uses_new_attestation_and_preserves_common_gates(original_core):
    core = original_core
    result = create_original(core, "telegram", content(), attest_original=True)
    checked = preflight(core, result["variant_id"])
    assert checked["preflight_only"] is True and checked["live_verified"] is False
    assert counts(core)["hub_approvals"] == 0 and counts(core)["hub_dispatches"] == 0
    excluded = create_original(core, "telegram", content(body="Оригинальный материал содержит казино — исключённую тему этого профиля."), attest_original=True)
    with pytest.raises(AgentError, match="QUALITY_GATE_BLOCKED"):
        preflight(core, excluded["variant_id"])


def test_disabled_target_cannot_create_stale_original_records(original_core):
    core = original_core
    with core.db.session.begin() as session:
        target = session.get(Target, "telegram")
        target.enabled, target.paused = False, True
    before = counts(core)
    with pytest.raises(AgentError, match="TARGET_DISABLED"):
        create_original(core, "telegram", content(), attest_original=True)
    assert counts(core) == before
    with core.db.session() as session:
        target = session.get(Target, "telegram")
        assert target.enabled is False and target.paused is True


@pytest.mark.parametrize("change", ["disabled", "dry_run"])
def test_nonpublishable_profile_cannot_create_original_records(original_core, change):
    profile = original_core.settings.channel("main")
    if change == "disabled":
        profile["enabled"] = False
    else:
        profile["publication"]["mode"] = "dry_run"
    before = counts(original_core)
    with pytest.raises(AgentError, match="PROFILE_NOT_PUBLISHABLE"):
        create_original(original_core, "telegram", content(), attest_original=True)
    assert counts(original_core) == before


@pytest.mark.parametrize("target", ["max"])
def test_max_legacy_route_cannot_use_original_input(original_core, target):
    before = counts(original_core)
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_ROUTE_REQUIRED"):
        create_original(original_core, target, content(), attest_original=True)
    assert counts(original_core) == before


def test_explicit_rights_flag_is_required_and_duplicate_never_restores_history(original_core):
    before = counts(original_core)
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_ATTESTATION_REQUIRED"):
        create_original(original_core, "telegram", content())
    assert counts(original_core) == before
    create_original(original_core, "telegram", content(), attest_original=True)
    saved = counts(original_core)
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_ALREADY_EXISTS"):
        create_original(original_core, "telegram", content(), attest_original=True)
    assert counts(original_core) == saved


@pytest.mark.parametrize("metadata", [{"origin": ORIGIN}, {"approval_mode": "automatic"},
    {"policy_hash": "a" * 64}, {"owner_original_attested_hash": "a" * 64}, {"source_ids": []}])
def test_metadata_cannot_inject_attestation_or_hourly_policy(original_core, metadata):
    before = counts(original_core)
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_METADATA_INVALID"):
        create_original(original_core, "telegram", content(metadata=metadata), attest_original=True)
    assert counts(original_core) == before


@pytest.mark.parametrize("field", ["title", "body", "media"])
def test_changed_title_body_or_media_require_new_original_attestation(original_core, field):
    result = create_original(original_core, "telegram", content(), attest_original=True)
    with original_core.db.session.begin() as session:
        value = session.get(Variant, result["variant_id"])
        if field == "media":
            value.media = [image(original_core)]
        else:
            setattr(value, field, getattr(value, field) + " Изменение")
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_ATTESTATION_STALE"):
        preflight(original_core, result["variant_id"])


@pytest.mark.parametrize("part", ["draft", "story", "draft_revision", "variant_revision", "audit",
    "audit_actor", "audit_details", "both_draft_decisions"])
def test_changed_canonical_records_or_removed_audit_fail_closed(original_core, part):
    result = create_original(original_core, "telegram", content(), attest_original=True)
    with original_core.db.session.begin() as session:
        value = session.get(Variant, result["variant_id"])
        if part == "draft":
            draft = session.get(Draft, value.draft_id)
            draft.current = {**draft.current, "body": "Подменённый исходник"}
        elif part == "story":
            session.get(Story, result["story_id"]).text = "Подменённый сюжет"
        elif part == "draft_revision":
            revision = session.scalar(select(DraftRevision).where(DraftRevision.draft_id == value.draft_id))
            revision.payload = {**revision.payload, "body": "Подменённая история"}
        elif part == "variant_revision":
            revision = session.scalar(select(VariantRevision).where(VariantRevision.variant_id == value.id))
            revision.payload = {**revision.payload, "body": "Подменённая история"}
        elif part == "both_draft_decisions":
            draft = session.get(Draft, value.draft_id)
            draft.current = {**draft.current, "decision": "passed"}
            revision = session.scalar(select(DraftRevision).where(DraftRevision.draft_id == value.draft_id))
            revision.payload = draft.current
        else:
            audit = session.scalar(select(Audit).where(Audit.entity == value.id, Audit.action == "owner_original_attested"))
            if part == "audit_actor":
                audit.actor = "pretend_machine_attestation"
            elif part == "audit_details":
                audit.details = {**audit.details, "model_verified": True}
            else:
                session.delete(audit)
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_(CANONICAL_RECORD_INVALID|AUDIT_REQUIRED)"):
        preflight(original_core, result["variant_id"])


def test_forged_origin_without_canonical_original_chain_does_not_pass(original_core):
    ident = variants(original_core)["telegram"]
    with original_core.db.session.begin() as session:
        value = session.get(Variant, ident)
        value.sources = []
        value.metadata_json = {"origin": ORIGIN, "owner_original_contract": CONTRACT,
            "owner_original_attested_hash": attestation_hash(value.title, value.body, value.media)}
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_CANONICAL_RECORD_INVALID"):
        preflight(original_core, ident)


def test_metadata_edit_cannot_reuse_original_audit(original_core):
    result = create_original(original_core, "telegram", content(), attest_original=True)
    with original_core.db.session.begin() as session:
        value = session.get(Variant, result["variant_id"])
        value.metadata_json = {**value.metadata_json, "tags": ["Подмена"]}
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_CANONICAL_RECORD_INVALID"):
        preflight(original_core, result["variant_id"])


def test_hourly_origin_does_not_inherit_original_rights_gate(original_core):
    result = create_original(original_core, "telegram", content(), attest_original=True)
    with original_core.db.session.begin() as session:
        value = session.get(Variant, result["variant_id"])
        value.metadata_json = {**value.metadata_json, "origin": "hourly_article_test"}
    with pytest.raises(AgentError, match="EDITOR_UNKNOWN_SOURCE"):
        preflight(original_core, result["variant_id"])


def test_pikabu_metadata_is_exact_instead_of_silently_adding_tags(original_core):
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_PIKABU_METADATA_NOT_EXACT"):
        create_original(original_core, "pikabu", content(), attest_original=True)
    metadata = {"tags": ["Наука", "Контент нейросетей"], "community": "", "ai_generated": True}
    result = create_original(original_core, "pikabu", content(metadata=metadata), attest_original=True)
    assert preflight(original_core, result["variant_id"])["preflight_only"] is True


def test_json_file_is_local_bounded_and_rejects_duplicate_keys(original_core):
    root = original_core.settings.root
    file = root / "data/input.json"
    file.write_text(json.dumps(content(), ensure_ascii=False), encoding="utf-8")
    assert read_original_file(root, file) == content()
    with pytest.raises(AgentError, match="OWNER_ORIGINAL_FILE_INVALID"):
        read_original_file(root, root.parent / "outside.json")
    file.write_text('{"title":"один","title":"два"}', encoding="utf-8")
    with pytest.raises(AgentError, match="DUPLICATE_JSON_KEY"):
        read_original_file(root, file)


def test_cli_create_requires_flag_and_prints_no_text_or_approval(original_core, monkeypatch, capsys):
    root = original_core.settings.root
    path = root / "data/input.json"
    supplied = content()
    path.write_text(json.dumps(supplied, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("max_postiz_agent.routes_cli.build", lambda _root: original_core)
    command = ["--root", str(root), "create-original", "telegram", "--file", str(path)]
    assert main(command) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "OWNER_ORIGINAL_ATTESTATION_REQUIRED"
    assert main([*command, "--attest-original"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["approval_created"] is False and result["state"] == "review_pending"
    assert supplied["title"] not in output and supplied["body"] not in output
    assert counts(original_core)["hub_approvals"] == 0 and counts(original_core)["hub_dispatches"] == 0


def test_cli_rejects_explicit_database_override_after_sqlite_fixture(original_core, monkeypatch, capsys):
    root = original_core.settings.root
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://fixture:synthetic@127.0.0.1:55450/editorial_hub_tests")
    monkeypatch.setattr("max_postiz_agent.routes_cli.build", lambda _root: pytest.fail("Invalid endpoint reached build"))
    before = counts(original_core)
    assert main(["--root", str(root), "create-original", "telegram", "--file", str(root / "data/input.json"),
                 "--attest-original"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "ISOLATED_DATABASE_REQUIRED"
    assert counts(original_core) == before
