"""Pure artifact/host guards; never start Docker, systemd, WSL or an HTTP client."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def native(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "deployment/linux"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("native_readiness_test", folder / "native_readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform,uid,changes", [
    ("win32", 0, {}), ("linux", 1000, {}),
    ("linux", 0, {"RUNNER_ENVIRONMENT": "self-hosted"}),
    ("linux", 0, {"GITHUB_ACTIONS": "false"}),
    ("linux", 0, {"GITHUB_SHA": "branch-name"}),
    ("linux", 0, {"GITHUB_SHA": "a" * 39}),
])
def test_every_ambient_host_is_rejected_before_any_process(native, monkeypatch, platform, uid, changes):
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: pytest.fail("process forbidden"))
    environment = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_SHA": "a" * 40}
    with pytest.raises(native.ReadinessError, match="EPHEMERAL_GITHUB_RUNNER_REQUIRED"):
        native.github_guard({**environment, **changes}, platform=platform, uid=uid)


def test_explicit_hosted_root_with_exact_commit_passes_pure_guard(native):
    native.github_guard({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_SHA": "a" * 40},
                        platform="linux", uid=0)


def test_private_state_is_exclusive_and_never_overwrites_existing_bytes(native, tmp_path):
    path = tmp_path / "private" / "state.json"
    native.private_write(path, {"value": "first"})
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        native.private_write(path, {"value": "second"})
    assert path.read_bytes() == before and json.loads(before) == {"value": "first"}


def test_decoded_database_password_and_nested_keys_cannot_escape_public_scan(native, tmp_path):
    value = {"postiz_dsn": "postgresql://fixture:opaque%2Fdatabase%2Fpassword@127.0.0.1:5432/ci",
             "nested": {"internal_key": "opaque-internal-fixture-key"}}
    needles = native.synthetic_secrets(value)
    assert "opaque/database/password" in needles
    assert "opaque-internal-fixture-key" in needles
    public = tmp_path / "public"
    public.mkdir()
    (public / "proof.json").write_text(json.dumps({"accidental": "opaque/database/password"}), encoding="utf-8")
    with pytest.raises(native.ReadinessError, match="PUBLIC_ARTIFACT_SECRET_MATCH") as error:
        native.scan_public(public, needles)
    assert "opaque/database/password" not in str(error.value)


@pytest.mark.parametrize("name", ["runtime.env", "cookies.har", "unfiltered.log", "state.sqlite"])
def test_unknown_public_artifact_types_fail_closed(native, tmp_path, name):
    (tmp_path / name).write_bytes(b"synthetic non-secret fixture")
    with pytest.raises(native.ReadinessError, match="PUBLIC_ARTIFACT_TYPE_INVALID"):
        native.scan_public(tmp_path, set())


def test_sanitized_proof_and_binary_screenshot_are_accepted(native, tmp_path):
    (tmp_path / "proof.json").write_text('{"accepted": false}', encoding="utf-8")
    (tmp_path / "synthetic.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    assert native.scan_public(tmp_path, {"a-secret-that-is-not-present"}) == 0


def test_disk_capacity_failure_cannot_start_a_build_or_delete_ambient_data(native, monkeypatch, tmp_path):
    monkeypatch.setattr(native, "github_guard", lambda: None)
    monkeypatch.setattr(native, "EPHEMERAL_UNUSED_SDKS", ())
    monkeypatch.setattr(native.shutil, "disk_usage", lambda path: SimpleNamespace(free=native.REQUIRED_DISK_BYTES - 1))
    monkeypatch.setattr(native.shutil, "rmtree", lambda *a, **k: pytest.fail("ambient deletion forbidden"))
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: pytest.fail("process forbidden"))
    with pytest.raises(native.ReadinessError, match="NATIVE_IMAGE_DISK_CAPACITY_REQUIRED"):
        native.disk_preflight(tmp_path, tmp_path / "workspace")


def test_sufficient_measured_disk_keeps_all_ambient_directories(native, monkeypatch, tmp_path):
    monkeypatch.setattr(native, "github_guard", lambda: None)
    monkeypatch.setattr(native.shutil, "disk_usage", lambda path: SimpleNamespace(free=native.REQUIRED_DISK_BYTES))
    monkeypatch.setattr(native.shutil, "rmtree", lambda *a, **k: pytest.fail("unnecessary deletion forbidden"))
    result = native.disk_preflight(tmp_path, tmp_path / "workspace")
    assert result["free_after_bytes"] == native.REQUIRED_DISK_BYTES
    assert result["ephemeral_sdk_dirs_removed"] == [] and result["docker_prune_performed"] is False


def test_failed_privacy_scan_publishes_only_fixed_failure_and_quarantines_payload(native, tmp_path):
    staging, validated = tmp_path / "unvalidated", tmp_path / "validated"
    staging.mkdir()
    secret = "opaque-fixture-secret-never-upload"
    (staging / "unexpected.json").write_text(json.dumps({"value": secret}), encoding="utf-8")
    assert native.finalize_public(staging, {"accepted": True}, {secret}, validated) is False
    assert not staging.exists()
    assert sorted(path.name for path in validated.iterdir()) == [".privacy-verified.json", "acceptance.json"]
    public = (validated / "acceptance.json").read_text(encoding="utf-8")
    assert secret not in public and json.loads(public)["accepted"] is False
    receipt = json.loads((validated / ".privacy-verified.json").read_text(encoding="utf-8"))
    assert receipt["secret_matches"] == 0 and list(receipt["files"]) == ["acceptance.json"]
    assert receipt["files"]["acceptance.json"] == native.digest(public.encode())
    quarantine = list(tmp_path.glob("unvalidated-quarantine-*"))
    assert len(quarantine) == 1 and (quarantine[0] / "unexpected.json").is_file()


def test_only_scanned_proof_set_is_promoted_with_checksums(native, tmp_path):
    staging, validated = tmp_path / "unvalidated", tmp_path / "validated"
    staging.mkdir()
    (staging / "proof.json").write_text('{"passed": true}', encoding="utf-8")
    assert native.finalize_public(staging, {"accepted": True}, {"absent-secret"}, validated) is True
    assert not staging.exists()
    manifest = (validated / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "  acceptance.json\n" in manifest and "  proof.json\n" in manifest
    assert json.loads((validated / "acceptance.json").read_text())["privacy_scan_completed"] is True


def test_finalization_never_overwrites_a_previous_validated_directory(native, tmp_path):
    staging, validated = tmp_path / "unvalidated", tmp_path / "validated"
    staging.mkdir()
    validated.mkdir()
    original = validated / "preserved.json"
    original.write_text('{"previous": true}', encoding="utf-8")
    with pytest.raises(native.ReadinessError, match="PUBLIC_FINALIZATION_PATH_INVALID"):
        native.finalize_public(staging, {"accepted": True}, set(), validated)
    assert original.read_text(encoding="utf-8") == '{"previous": true}' and list(staging.iterdir()) == []


def failed_acceptance(native, tmp_path):
    acceptance = native.Acceptance.__new__(native.Acceptance)
    acceptance.private, acceptance.public = tmp_path / "private", tmp_path / "public"
    acceptance.private.mkdir()
    acceptance.public.mkdir()
    acceptance.report = {}
    acceptance.current_phase = "preflight"
    acceptance.runtime_passwords = {"token": "opaque-fixture-credential-redacted"}
    return acceptance


def test_engine_failure_collector_exposes_only_typed_metadata_and_source_hash(native, tmp_path):
    acceptance = failed_acceptance(native, tmp_path)
    path = acceptance.private / "engine-failure.json"
    secret = acceptance.runtime_passwords["token"]
    path.write_text(json.dumps({"contract": "github-owned-engine-failure-v1", "stage": "UNIT_FILE_LIST_QUERY",
        "error_kind": "exit", "returncode": 1, "stdout_sha256": "a" * 64, "stderr_sha256": "b" * 64,
        "stdout_tail": secret, "stderr_tail": secret[-12:]}), encoding="utf-8")
    report, _ = acceptance.final_report(RuntimeError("ENGINE_FAILED"))
    diagnostic = (acceptance.public / "diagnostic-summary.json").read_text(encoding="utf-8")
    assert report["accepted"] is False and acceptance.runtime_passwords["token"] not in diagnostic
    assert secret[-12:] not in diagnostic and native.digest(path.read_bytes()) in diagnostic
    record = json.loads(diagnostic)[0]
    assert "tail" not in record and record["engine"]["stage"] == "UNIT_FILE_LIST_QUERY"
    assert record["engine"]["returncode"] == 1 and "stderr_tail" not in record["engine"]


def test_regular_log_redaction_happens_before_tail_truncation(native, tmp_path):
    acceptance = failed_acceptance(native, tmp_path)
    secret = acceptance.runtime_passwords["token"]
    raw = ("begin" + secret + "x" * 15990).encode()
    assert secret[-10:].encode() in raw[-16000:] and secret.encode() not in raw[-16000:]
    (acceptance.private / "build.log").write_bytes(raw)
    acceptance.final_report(RuntimeError("ENGINE_FAILED"))
    diagnostic = (acceptance.public / "diagnostic-summary.json").read_text(encoding="utf-8")
    assert secret[-10:] not in diagnostic and "[REDACTED]" in diagnostic
    assert native.digest(raw) in diagnostic


def test_oversized_engine_failure_cannot_enter_public_diagnostics(native, tmp_path):
    acceptance = failed_acceptance(native, tmp_path)
    (acceptance.private / "engine-failure.json").write_bytes(b"unbounded-engine-diagnostic" * 4096)
    acceptance.final_report(RuntimeError("ENGINE_FAILED"))
    diagnostic = (acceptance.public / "diagnostic-summary.json").read_text(encoding="utf-8")
    assert "unbounded-engine-diagnostic" not in diagnostic


def test_symlink_engine_failure_is_not_read_or_exposed(native, tmp_path):
    acceptance = failed_acceptance(native, tmp_path)
    outside = tmp_path / "unrelated-private.json"
    outside.write_text('{"value": "unrelated-sensitive-source"}', encoding="utf-8")
    try:
        (acceptance.private / "engine-failure.json").symlink_to(outside)
    except OSError:
        pytest.skip("Windows account cannot create a symbolic link")
    acceptance.final_report(RuntimeError("ENGINE_FAILED"))
    diagnostic = (acceptance.public / "diagnostic-summary.json").read_text(encoding="utf-8")
    assert "unrelated-sensitive-source" not in diagnostic


@pytest.mark.parametrize("record", [
    "2026-11-02 UTC [42] LOG:  statement: CREATE TABLE ci_probe(id int)",
    "2026-11-02 UTC [42] LOG:  execute <unnamed>: CREATE TABLE ci_probe(id int)",
    "2026-11-02 UTC [42] LOG:  execute s7: ALTER TABLE ci_probe ADD COLUMN value text",
    "2026-11-02 UTC [42] LOG:  statement: -- client comment\nCREATE TABLE ci_probe(id int)",
])
def test_ddl_logger_records_also_detect_prepared_or_commented_statements(native, record):
    assert native.count_logged_schema_statements(record) == 1


def test_ddl_logger_counter_ignores_unrelated_errors_and_details(native):
    unrelated = "2026-11-02 UTC [42] ERROR: syntax error\nSTATEMENT: SELECT missing\nDETAIL: execute client failed\n"
    assert native.count_logged_schema_statements(unrelated) == 0
