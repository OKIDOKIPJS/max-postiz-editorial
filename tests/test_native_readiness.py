"""Pure artifact/host guards; never start Docker, systemd, WSL or an HTTP client."""
import importlib.util
import json
from pathlib import Path, PurePosixPath
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


def test_readonly_proxy_confines_all_module_temporary_files_to_private_tmpfs(native):
    import re
    outer = "server{listen 10.20.30.45:4007;location /owner-browser/{auth_request /_owner;}}"
    result = native.proxy_configuration(outer)
    paths = dict(re.findall(r"(client_body|proxy|fastcgi|uwsgi|scgi)_temp_path\s+([^;]+);", result))
    assert set(paths) == {"client_body", "proxy", "fastcgi", "uwsgi", "scgi"}
    assert all(PurePosixPath(value).parts[:2] == ("/", "tmp") and ".." not in value for value in paths.values())
    assert outer in result and "pid /tmp/nginx.pid;" in result


def diagnostic_engine(native, supplied):
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        assert args[0] == "inspect"
        return SimpleNamespace(stdout=json.dumps([supplied]))
    engine = SimpleNamespace(label_value="owned-fixture", cgroup_parent="/owned-fixture.slice", docker=docker,
        docker_binary=Path("/owned-fixture/docker"), socket="unix:///owned-fixture/docker.sock", config_dir=Path("/owned-fixture/client"))
    return engine, calls


def diagnostic_container(native):
    return {"Config": {"Labels": {native.LABEL_KEY: "owned-fixture", "editorial.readiness.role": "proxy"},
                       "Env": ["UNRELATED_SECRET=not-for-diagnostics"]},
        "HostConfig": {"CgroupParent": "/owned-fixture.slice"},
        "State": {"Status": "exited", "Running": False, "ExitCode": 1, "OOMKilled": False}}


@pytest.mark.parametrize("mismatch", ["owner", "cgroup", "role"])
def test_dependency_diagnostic_refuses_foreign_scope_before_reading_logs(native, tmp_path, monkeypatch, mismatch):
    acceptance = failed_acceptance(native, tmp_path)
    supplied = diagnostic_container(native)
    if mismatch == "owner":
        supplied["Config"]["Labels"][native.LABEL_KEY] = "unrelated-owner"
    elif mismatch == "cgroup":
        supplied["HostConfig"]["CgroupParent"] = "/unrelated.slice"
    else:
        supplied["Config"]["Labels"]["editorial.readiness.role"] = "../outside"
    acceptance.engine, calls = diagnostic_engine(native, supplied)
    monkeypatch.setattr(native, "bounded_diagnostic_output", lambda *a, **k: pytest.fail("foreign logs forbidden"))
    with pytest.raises(native.ReadinessError, match="DEPENDENCY_DIAGNOSTIC_OWNER_MISMATCH"):
        acceptance.dependency_diagnostic("owned-container-fixture")
    assert calls == [("inspect", "owned-container-fixture")] and list(acceptance.private.iterdir()) == []


def test_owned_dependency_diagnostic_keeps_only_typed_state_and_redacts_public_logs(native, tmp_path, monkeypatch):
    acceptance = failed_acceptance(native, tmp_path)
    acceptance.engine, _ = diagnostic_engine(native, diagnostic_container(native))
    secret = acceptance.runtime_passwords["token"]
    raw = ("fixture error " + secret + "\n").encode()
    def capture(command, environment):
        assert command[-4:] == ["logs", "--tail", "128", "owned-container-fixture"]
        return raw, {"captured_bytes": len(raw), "truncated": False, "timed_out": False}
    acceptance.engine.environment = {}
    monkeypatch.setattr(native, "bounded_diagnostic_output", capture)
    acceptance.dependency_diagnostic("owned-container-fixture")
    assert acceptance.report["failed_dependency"]["exit_code"] == 1
    assert (acceptance.private / "dependency-proxy.log").read_bytes() == raw
    acceptance.final_report(RuntimeError("DEPENDENCY_FAILED"))
    public = (acceptance.public / "diagnostic-summary.json").read_text(encoding="utf-8")
    assert secret not in public and "not-for-diagnostics" not in public and "[REDACTED]" in public


@pytest.mark.parametrize("failure", ["thread_start", "terminate", "wait"])
def test_diagnostic_client_is_reaped_and_pipe_closed_when_thread_start_or_cleanup_raises(native, monkeypatch, failure):
    import io
    calls = []
    class Process:
        stdout = io.BytesIO(b"fixture private output")
        def poll(self):
            return None
        def terminate(self):
            calls.append("terminate")
            if failure == "terminate":
                raise OSError("private terminate detail")
        def wait(self, *, timeout):
            calls.append("wait")
            if failure == "wait" and calls.count("wait") == 1:
                raise OSError("private wait detail")
            return 0
        def kill(self):
            calls.append("kill")
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("private thread detail")
        def join(self, **kwargs):
            pytest.fail("Unstarted diagnostic reader must never be joined")
    process = Process()
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(native.threading, "Thread", Thread)
    with pytest.raises(native.ReadinessError, match="^DEPENDENCY_DIAGNOSTIC_CAPTURE_FAILED$"):
        native.bounded_diagnostic_output(["owned-fixture-client"], {})
    assert calls[0] == "terminate" and "wait" in calls and process.stdout.closed
    if failure == "wait":
        assert calls == ["terminate", "wait", "kill", "wait"]


@pytest.mark.parametrize("changed", ["name", "directory", "symlink", "fifo", "hardlink", "owner", "size", "writable"])
def test_scenario_collector_entry_rejects_wrong_name_owner_type_links_and_bounds(native, changed):
    import stat
    name = "exercise.json"
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=123, st_size=20)
    if changed == "name":
        name = "../private.env"
    elif changed in {"directory", "symlink", "fifo"}:
        info.st_mode = {"directory": stat.S_IFDIR, "symlink": stat.S_IFLNK, "fifo": stat.S_IFIFO}[changed] | 0o600
    elif changed == "hardlink":
        info.st_nlink = 2
    elif changed == "owner":
        info.st_uid = 456
    elif changed == "size":
        info.st_size = native.SCENARIO_FILE_LIMIT + 1
    else:
        info.st_mode |= 0o020
    with pytest.raises(native.ReadinessError, match="SCENARIO_EVIDENCE_ENTRY_INVALID"):
        native.validate_scenario_entry(name, info, 123)


@pytest.mark.skipif(__import__("os").name != "posix", reason="Held directory descriptor API is a Linux collector prerequisite")
def test_scenario_collector_copies_only_owned_bounded_regular_files_before_privacy(native, tmp_path):
    import os
    project, staging = tmp_path / "project", tmp_path / "root-staging"
    source = project / "data/ci-public"
    source.mkdir(parents=True, mode=0o700)
    source.parent.chmod(0o700)
    staging.mkdir()
    raw = b'{"fixture":"requires later privacy scan"}'
    (source / "exercise.json").write_bytes(raw)
    result = native.collect_scenario_evidence(project, staging, os.getuid())
    assert result["owner_verified"] is True and result["privacy_scan_still_required"] is True
    assert result["files"] == {"exercise.json": native.digest(raw)}
    assert (staging / "exercise.json").read_bytes() == raw


@pytest.mark.skipif(__import__("os").name != "posix", reason="No-follow/FIFO descriptor guard is Linux only")
@pytest.mark.parametrize("kind", ["symlink", "fifo", "nested"])
def test_scenario_collector_never_follows_links_opens_blocking_fifo_or_copies_nested_content(native, tmp_path, kind):
    import os
    project, staging = tmp_path / "project", tmp_path / "root-staging"
    source = project / "data/ci-public"
    source.mkdir(parents=True, mode=0o700)
    source.parent.chmod(0o700)
    staging.mkdir()
    if kind == "symlink":
        unrelated = tmp_path / "unrelated.json"
        unrelated.write_text('{"private":"outside fixture"}')
        (source / "exercise.json").symlink_to(unrelated)
    elif kind == "fifo":
        os.mkfifo(source / "exercise.json")
    else:
        (source / "exercise.json").mkdir()
    with pytest.raises((native.ReadinessError, OSError)):
        native.collect_scenario_evidence(project, staging, os.getuid())
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize("timeout_failure", [False, True])
def test_private_user_producers_are_owned_units_even_when_parent_wait_times_out(native, tmp_path, timeout_failure):
    acceptance = native.Acceptance.__new__(native.Acceptance)
    acceptance.project, acceptance.browsers = tmp_path / "project", tmp_path / "browsers"
    acceptance.engine = SimpleNamespace(label_value="a" * 32, slice="editorial_readiness_fixture.slice")
    acceptance.anchor_pid, acceptance.unit_names = 123, []
    calls = []
    def run(command, **options):
        calls.append((command, options))
        if timeout_failure:
            raise native.subprocess.TimeoutExpired("owned-systemd-client", options["timeout"])
        return SimpleNamespace(returncode=0)
    acceptance.run = run
    command = ["/owned/python", "/owned/readiness_scenario.py"]
    if timeout_failure:
        with pytest.raises(native.subprocess.TimeoutExpired):
            acceptance.user_run(command, name="scenario-exercise", timeout=60)
    else:
        acceptance.user_run(command, name="scenario-exercise", timeout=60)
    argv, options = calls[0]
    assert "--uid=editorial" in argv and "--slice=editorial_readiness_fixture.slice" in argv
    assert "--property=NetworkNamespacePath=/proc/123/ns/net" in argv
    assert "--property=KillMode=control-group" in argv and "--property=RuntimeMaxSec=60" in argv
    assert "--property=NoNewPrivileges=yes" in argv and "--property=Delegate=yes" in argv
    assert "--property=MemoryMax=2G" in argv and "--property=MemorySwapMax=2G" in argv
    assert argv[-2:] == command and "/usr/sbin/runuser" not in argv and options["timeout"] == 85
    assert acceptance.unit_names == ["mp-native-scenario-exercise-aaaaaaaaaaaa.service"]


@pytest.mark.parametrize("closed", [False, True])
def test_scenario_artifacts_are_read_only_after_kernel_cleanup_confirmation(native, tmp_path, monkeypatch, closed):
    acceptance = failed_acceptance(native, tmp_path)
    acceptance.report = {"accepted": True, "checks": {}}
    acceptance.project = tmp_path / "project"
    acceptance.scenario_public = acceptance.project / "data/ci-public"
    acceptance.user = SimpleNamespace(pw_uid=123)
    (acceptance.private / "engine.json").write_text(json.dumps({"state": "closed", "cgroup_empty": closed,
                                                                "cleanup_errors": []}))
    copied = []
    def collect(project, staging, owner):
        assert closed, "Never inspect child output while producers may still run"
        copied.append((project, staging, owner))
        return {"file_count": 0, "owner_verified": True, "privacy_scan_still_required": True}
    monkeypatch.setattr(native, "collect_scenario_evidence", collect)
    report, _ = acceptance.final_report()
    assert bool(copied) is closed and report["accepted"] is closed
    if not closed:
        assert report["scenario_artifact_collection_failed"] is True


def test_disabled_hourly_template_matches_actual_policy_parser_without_enabling_or_external_calls(native, tmp_path):
    from max_postiz_agent.article_cycle import HourlyArticleCycle
    raw = (Path(__file__).resolve().parents[1] / "config/hourly_article_policy.example.json").read_bytes()
    value = native.disabled_policy_template(raw)
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "hourly-article-policy.local.json").write_bytes(raw)
    cycle = HourlyArticleCycle.__new__(HourlyArticleCycle)
    cycle.core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    assert cycle._policy(enabled=False) == value and value["enabled"] is False
    assert (folder / "hourly-article-policy.local.json").read_bytes() == raw


@pytest.mark.parametrize("change", ["enabled", "extra", "missing"])
def test_native_policy_template_rejects_enabled_or_wrong_shape_instead_of_reconstructing(native, change):
    value = json.loads((Path(__file__).resolve().parents[1] / "config/hourly_article_policy.example.json").read_text())
    if change == "enabled":
        value["enabled"] = True
    elif change == "extra":
        value["unreviewed"] = True
    else:
        value.pop("target_id")
    with pytest.raises(native.ReadinessError, match="NATIVE_DISABLED_POLICY_INVALID"):
        native.disabled_policy_template(json.dumps(value))


@pytest.mark.skipif(__import__("os").name != "posix", reason="Secure private fixture creation uses Linux directory descriptors")
def test_native_policy_creation_is_private_exact_disabled_and_never_overwrites(native, tmp_path):
    import os
    import stat
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    data.mkdir(mode=0o700)
    raw = (Path(__file__).resolve().parents[1] / "config/hourly_article_policy.example.json").read_bytes()
    (config / "hourly_article_policy.example.json").write_bytes(raw)
    result = native.initialize_disabled_policy(tmp_path, os.getuid(), os.getgid())
    target = data / "hourly-article-policy.local.json"
    assert target.read_bytes() == raw and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_uid == os.getuid() and result["enabled"] is False
    assert result["targets_created"] == result["approvals_created"] == result["external_mutations"] == 0
    with pytest.raises(native.ReadinessError, match="NATIVE_POLICY_ALREADY_EXISTS"):
        native.initialize_disabled_policy(tmp_path, os.getuid(), os.getgid())
    assert target.read_bytes() == raw


@pytest.mark.skipif(__import__("os").name != "posix", reason="Private-directory permission enforcement is Linux only")
def test_native_policy_creation_rejects_public_data_directory(native, tmp_path):
    import os
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    data.mkdir(mode=0o755)
    (config / "hourly_article_policy.example.json").write_bytes(
        (Path(__file__).resolve().parents[1] / "config/hourly_article_policy.example.json").read_bytes())
    with pytest.raises(native.ReadinessError, match="NATIVE_POLICY_PRIVATE_DIRECTORY_REQUIRED"):
        native.initialize_disabled_policy(tmp_path, os.getuid(), os.getgid())
    assert not (data / "hourly-article-policy.local.json").exists()


@pytest.mark.parametrize("unit,helper", [
    ("max-postiz-login.service", "login-browser.py"),
    ("max-postiz-editorial-restart.service", "restart-editorial.py"),
])
def test_relocated_owner_helper_resets_only_execstart_and_passes_exact_root(native, unit, helper):
    import shlex
    project = PurePosixPath("/opt/max-postiz-readiness-abc123/project")
    lines = native.helper_execstart_override(unit, project).splitlines()
    assert lines[0] == "ExecStart=" and len(lines) == 2
    assert shlex.split(lines[1].removeprefix("ExecStart=")) == [
        str(project / ".venv/bin/python"), str(project / "deployment/linux" / helper), "--root", str(project)]
    assert all(not line.startswith(("User=", "Environment=", "Slice=", "NoNewPrivileges=")) for line in lines)


@pytest.mark.parametrize("unit,project", [
    ("max-postiz-editorial.service", "/opt/max-postiz-readiness-abc123/project"),
    ("max-postiz-display.service", "/opt/max-postiz-readiness-abc123/project"),
    ("arbitrary.service", "/opt/max-postiz-readiness-abc123/project"),
    ("max-postiz-login.service", "relative/project"),
    ("max-postiz-login.service", "/opt/project\nUser=root"),
    ("max-postiz-editorial-restart.service", "/opt/%n/project"),
])
def test_relocated_helper_override_refuses_other_units_or_systemd_expansion(native, unit, project):
    with pytest.raises(native.ReadinessError, match="NATIVE_HELPER_UNIT_OVERRIDE_INVALID"):
        native.helper_execstart_override(unit, project)


def restart_lease(project):
    return {"contract": "editorial-restart-request-v1", "request_id": "a" * 32, "root": str(project),
            "issued_at": 100, "expires_at": 160, "runtime_generation": "b" * 64,
            "previous_worker_id": "11111111-1111-4111-8111-111111111111"}


@pytest.mark.parametrize("now", [100, 160, 161])
def test_portal_waits_for_exact_restart_expiry_without_mutating_request(native, now):
    project = PurePosixPath("/opt/native/project")
    value = restart_lease(project)
    before = json.dumps(value, sort_keys=True)
    ticks = [float(now), 0.0]
    def sleep(seconds):
        ticks[0] += seconds
        ticks[1] += seconds
    result = native.wait_restart_expiry(value, project, clock=lambda: ticks[0], monotonic=lambda: ticks[1], sleep=sleep)
    assert ticks[0] > 160 and result["waited_seconds"] <= 61
    assert result["lease_mutated"] is False and json.dumps(value, sort_keys=True) == before


def test_portal_restart_expiry_has_bounded_monotonic_wait_even_if_wallclock_stalls(native):
    project = PurePosixPath("/opt/native/project")
    elapsed = [0.0]
    with pytest.raises(native.ReadinessError, match="NATIVE_RESTART_LEASE_WAIT_EXCEEDED"):
        native.wait_restart_expiry(restart_lease(project), project, clock=lambda: 101,
            monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    assert elapsed[0] == 65


@pytest.mark.parametrize("key,value", [("expires_at", 166), ("root", "/other"), ("request_id", "invalid"), ("issued_at", 170)])
def test_portal_restart_expiry_rejects_wrong_nonce_root_or_expiry_instead_of_shortening(native, key, value):
    project = PurePosixPath("/opt/native/project")
    lease = {**restart_lease(project), key: value}
    with pytest.raises(native.ReadinessError, match="NATIVE_RESTART_LEASE_INVALID"):
        native.wait_restart_expiry(lease, project, clock=lambda: 101,
            sleep=lambda seconds: pytest.fail("Must not sleep or mutate an invalid lease"))


def test_portal_restart_expiry_rejects_an_overslept_deadline(native):
    project = PurePosixPath("/opt/native/project")
    ticks = [101.0, 0.0]
    def sleep(seconds):
        ticks[:] = [201.0, 100.0]
    with pytest.raises(native.ReadinessError, match="NATIVE_RESTART_LEASE_WAIT_EXCEEDED"):
        native.wait_restart_expiry(restart_lease(project), project,
            clock=lambda: ticks[0], monotonic=lambda: ticks[1], sleep=sleep)


@pytest.mark.skipif(__import__("os").name != "posix", reason="Private restart lease reads require Linux directory descriptors")
def test_portal_reads_only_private_owned_bounded_restart_manifest_without_writing(native, tmp_path):
    import os
    folder = tmp_path / "data"
    folder.mkdir(mode=0o700)
    target = folder / "editorial-restart-request.json"
    raw = json.dumps(restart_lease(tmp_path)).encode()
    target.write_bytes(raw)
    target.chmod(0o600)
    before = target.stat()
    assert native.read_restart_lease(tmp_path, os.getuid()) == raw
    assert target.stat().st_mtime_ns == before.st_mtime_ns and target.read_bytes() == raw
    target.write_bytes(b"x" * 4097)
    with pytest.raises(native.ReadinessError, match="NATIVE_RESTART_LEASE_PRIVATE_REQUIRED"):
        native.read_restart_lease(tmp_path, os.getuid())


def test_proxy_error_redaction_removes_whole_auth_lines_jwts_and_known_secrets(native):
    raw = b'open failed fixture-secret\nCookie: unknown-cookie-value\nAuthorization: Bearer private-value\nother abcdefghijk.abcdefghijkl.abcdefghijklm\n'
    value = native.redact_proxy_error(raw, {"fixture-secret"})
    assert b'open failed [REDACTED]' in value and b'[REDACTED_JWT]' in value
    assert all(secret not in value for secret in (b'fixture-secret', b'unknown-cookie-value', b'private-value', b'abcdefghijk.'))


@pytest.mark.parametrize("changed", ["label", "role", "parent"])
def test_nginx_private_log_reader_rejects_foreign_container_before_exec(native, tmp_path, changed):
    instance = native.Acceptance.__new__(native.Acceptance)
    value = {"Config": {"Labels": {native.LABEL_KEY: "owned", "editorial.readiness.role": "proxy"}},
             "HostConfig": {"CgroupParent": "/owned.slice"}}
    if changed == "label":
        value["Config"]["Labels"][native.LABEL_KEY] = "foreign"
    elif changed == "role":
        value["Config"]["Labels"]["editorial.readiness.role"] = "backend"
    else:
        value["HostConfig"]["CgroupParent"] = "/foreign.slice"
    calls = []
    def docker(*argv, **kwargs):
        calls.append(argv)
        assert argv == ("inspect", "container"), "Must not read logs before exact owner validation"
        return SimpleNamespace(stdout=json.dumps([value]))
    instance.engine = SimpleNamespace(docker=docker, label_value="owned", cgroup_parent="/owned.slice")
    with pytest.raises(native.ReadinessError, match="PROXY_DIAGNOSTIC_OWNER_MISMATCH"):
        instance.proxy_error_diagnostic("container")
    assert calls == [("inspect", "container")]


def test_early_large_proxy_transport_uses_separate_capped_client_and_releases_all_ports(native, tmp_path):
    instance = native.Acceptance.__new__(native.Acceptance)
    instance.project, instance.private = tmp_path / "project", tmp_path / "private"
    (instance.project / "deployment/linux").mkdir(parents=True)
    instance.private.mkdir()
    (instance.project / "deployment/linux/editorial-postiz.nginx.conf").write_text("server{listen 4007;}")
    calls, containers, commands = [], {}, []
    def docker(*argv, **kwargs):
        calls.append(argv)
        if argv[0] == "wait":
            return SimpleNamespace(stdout="0\n")
        if argv[0] == "logs":
            return SimpleNamespace(stdout=json.dumps({"passed": True, "status": 201, "bytes": 14 * 1024 * 1024}))
        if argv[0] == "inspect":
            return SimpleNamespace(stdout=json.dumps([{"State": {"Running": False}}]))
        assert argv[0] == "stop"
        return SimpleNamespace(stdout="")
    instance.engine = SimpleNamespace(root=tmp_path, docker=docker)
    instance.anchor_pid = 123
    instance.report = {"checks": {}}
    instance.phase = lambda value: None
    instance.network_anchor = lambda: None
    instance.container = lambda role, image, **kwargs: containers.setdefault(role, kwargs) and role
    instance.wait_exec = lambda *args, **kwargs: None
    instance.namespace = lambda command: command
    instance.run = lambda command, **kwargs: commands.append(command)
    instance.proxy_preflight()
    assert containers["proxy-preflight"]["memory"] == "96m"
    assert containers["proxy-transport-client"]["memory"] == "128m"
    assert "Buffer.alloc(14*1024*1024" in containers["proxy-transport-client"]["command"][1]
    assert "createServer" in containers["proxy-transport-stub"]["command"][1]
    assert all(port in commands[0][-1] for port in ("3000", "14007", "4007"))
    assert {args[-1] for args in calls if args[0] == "stop"} == set(containers)
    proof = instance.report["checks"]["early_proxy_preflight"]
    assert proof["large_body_transport_bytes"] == 14 * 1024 * 1024 and proof["real_owner_authentication"] is False


def test_owned_nginx_error_capture_discards_partial_line_and_never_publishes_auth(native, tmp_path, monkeypatch):
    instance = native.Acceptance.__new__(native.Acceptance)
    instance.private, instance.report = tmp_path, {"checks": {}}
    instance.runtime_passwords = {"internal": "fixture-private-key"}
    inspected = {"Config": {"Labels": {native.LABEL_KEY: "owned", "editorial.readiness.role": "proxy"}},
                 "HostConfig": {"CgroupParent": "/owned.slice"}}
    instance.engine = SimpleNamespace(label_value="owned", cgroup_parent="/owned.slice",
        docker=lambda *args: SimpleNamespace(stdout=json.dumps([inspected])), docker_binary=PurePosixPath("/own/docker"),
        socket="unix:///own/engine.sock", config_dir=PurePosixPath("/own/config"), environment={})
    def output(command, environment):
        assert command[:5] == ["/own/docker", "--host", "unix:///own/engine.sock", "--config", "/own/config"]
        assert command[5:9] == ["exec", "container", "node", "-e"]
        assert "'/tmp/nginx-error.log'" in command[9] and "O_NOFOLLOW" in command[9]
        return b'open failed fixture-private-key\nCookie: unknown-cookie\nprivate-key-fragment', {"truncated": False}
    monkeypatch.setattr(native, "bounded_diagnostic_output", output)
    instance.proxy_error_diagnostic("container")
    raw, published = (tmp_path / "proxy-nginx-error.raw").read_bytes(), (tmp_path / "proxy-nginx-error.log").read_bytes()
    assert raw.endswith(b'\n') and b'private-key-fragment' not in raw
    assert b'fixture-private-key' not in published and b'unknown-cookie' not in published
    assert instance.report["checks"]["proxy_error_capture"]["owner_verified"] is True


def test_only_internal_owner_auth_and_exact_admin_scopes_allow_bounded_image_envelope():
    import re
    text = (Path(__file__).resolve().parents[1] / "deployment/linux/editorial-postiz.nginx.conf").read_text()
    locations = dict(re.findall(r"location\s+(=\s+\S+|\S+)\s*\{(.*?)\n    \}", text, re.S))
    enlarged = {name for name, body in locations.items() if "client_max_body_size 15m;" in body}
    assert enlarged == {"= /_editorial-owner-auth", "= /api/editorial/route-admin"}
    assert text.count("client_max_body_size 15m;") == 2 and text.count("client_max_body_size 1m;") == 1
    auth = locations["= /_editorial-owner-auth"]
    assert "internal;" in auth and "proxy_pass_request_body off;" in auth and 'proxy_set_header Content-Length "";' in auth
    assert "auth_request /_editorial-owner-auth;" in locations["= /api/editorial/route-admin"]


def test_early_chrome_prerequisite_is_nonroot_sandboxed_private_and_kernel_owned(native, tmp_path, monkeypatch):
    import ast
    import inspect
    instance = native.Acceptance.__new__(native.Acceptance)
    instance.workspace = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1001, st_gid=1001))
    instance.created_opt, instance.python, instance.browsers = tmp_path, tmp_path / "python", tmp_path / "browsers"
    instance.engine = SimpleNamespace(label_value="a" * 32, slice="owned.slice")
    instance.anchor_pid, instance.unit_names, instance.report = 123, [], {"checks": {}}
    calls, namespaces = [], []
    instance.phase = lambda value: None
    instance.namespace_proof = lambda: namespaces.append(True)
    instance.run = lambda argv, **kwargs: calls.append((argv, kwargs))
    monkeypatch.setattr(native.os, "chown", lambda *args: None, raising=False)
    instance.chrome_prerequisite()
    argv, options = calls[0]
    assert "--uid=1001" in argv and "--slice=owned.slice" in argv
    assert all(flag in argv for flag in ("--property=NoNewPrivileges=yes", "--property=Delegate=yes",
        "--property=KillMode=control-group", "--property=RuntimeMaxSec=45", "--property=MemoryMax=2G",
        "--property=NetworkNamespacePath=/proc/123/ns/net"))
    assert "TMPDIR=" + str(tmp_path / "chrome-prerequisite-home") in argv
    assert options["timeout"] == 65 and len(instance.unit_names) == 1 and len(namespaces) == 2
    script = (tmp_path / "chrome-prerequisite.py").read_text()
    ast.parse(script)
    assert "chromium_sandbox=True" in script and "about:blank" in script and "--no-sandbox" not in script
    assert instance.report["checks"]["sandboxed_chrome_prerequisite"]["owner_authentication"] is False
    order = inspect.getsource(native.Acceptance.execute)
    assert order.index("self.chrome_prerequisite()") < order.index("self.build()")


def test_early_chrome_prerequisite_rejects_root_uid_before_home_or_unit(native, tmp_path):
    instance = native.Acceptance.__new__(native.Acceptance)
    instance.workspace = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0, st_gid=0))
    instance.phase, instance.namespace_proof = lambda value: None, lambda: None
    instance.created_opt = tmp_path
    with pytest.raises(native.ReadinessError, match="CHROME_PREREQUISITE_NONROOT_OWNER_REQUIRED"):
        instance.chrome_prerequisite()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("verification_fails", [False, True, "fingerprint"])
def test_restore_waits_for_read_only_backend_and_rebinds_source_only_after_exact_validation(native, tmp_path, monkeypatch, verification_fails):
    instance = native.Acceptance.__new__(native.Acceptance)
    instance.project, instance.private = tmp_path / "project", tmp_path / "private"
    instance.private.mkdir()
    (instance.project / "config").mkdir(parents=True)
    preserved = instance.project / "config/preserved.json"
    preserved.write_text('{"unchanged":true}')
    (instance.project / "data").mkdir()
    (instance.project / ".env.local").write_text("fixture\n")
    instance.state_path = instance.private / "state.json"
    instance.state_path.write_text(json.dumps({"postiz_dsn": "postgresql://fixture@127.0.0.1:5432/original",
                                               "hub_dsn": "postgresql://fixture@127.0.0.1:5434/original"}))
    instance.backend, instance.orchestrator, instance.proxy = "old-backend", "old-orchestrator", "own-proxy"
    instance.image, instance.upload_volume, instance.report = "own-image", "own-uploads", {"checks": {}}
    original = {"postiz": ("postiz-id", 5432, "postiz-user", "original-postiz"),
                "hub": ("hub-id", 5434, "hub-user", "original-hub")}
    instance.pg = dict(original)
    events = []
    instance.phase = lambda value: None
    instance.engine = SimpleNamespace(docker=lambda *args, **kwargs: events.append(("docker", args)))
    instance.clone_database = lambda role, database: {"empty_destination_verified": True}
    instance.editorial_environment = lambda state: None
    instance.app_environment = lambda state: {}
    instance.container = lambda role, *args, **kwargs: events.append(("container", role)) or "new-backend"
    def wait(ident, command, **kwargs):
        events.append(("wait", command[-1], kwargs))
        assert ident == "own-proxy"
    instance.wait_exec = wait
    def scenario(mode):
        events.append(("scenario", mode))
        if mode == "verify-restored":
            assert instance.pg == original, "Never accept restored source before validation"
            if verification_fails == "fingerprint":
                preserved.write_text('{"unchanged":false}')
            elif verification_fails:
                raise native.ReadinessError("FIXTURE_VERIFY_FAILED")
    instance.scenario = scenario
    monkeypatch.setattr(native.subprocess, "run", lambda argv, **kwargs: events.append(("systemctl", argv)))
    if verification_fails:
        with pytest.raises(native.ReadinessError, match=("RESTORE_RUNTIME_FILES_CHANGED" if verification_fails == "fingerprint" else "FIXTURE_VERIFY_FAILED")):
            instance.backup_restore()
        assert instance.pg == original
    else:
        instance.backup_restore()
        for role, database in (("postiz", "postiz_readiness_restore"), ("hub", "editorial_hub_restore")):
            assert instance.pg[role] == (*original[role][:3], database)
    backend_wait = next(index for index, item in enumerate(events) if item[0] == "wait" and "can-register" in item[1])
    verify = events.index(("scenario", "verify-restored"))
    start = next(index for index, item in enumerate(events) if item == ("systemctl", ["systemctl", "start", "max-postiz-editorial.service"]))
    assert backend_wait < start < verify and events[backend_wait][2] == {"seconds": 180}
    assert "http://127.0.0.1:14007/api/auth/can-register" in events[backend_wait][1]
    assert "auth/login" not in events[backend_wait][1] and ".get(" in events[backend_wait][1]


@pytest.mark.parametrize("error", [OSError("private/path/secret"), ValueError("private-input"), RuntimeError("SCENARIO_EVIDENCE_ENTRY_INVALID")])
def test_collection_diagnostic_never_echoes_unknown_error_text_or_raw_cleanup_errors(native, error):
    value = native.collection_failure_metadata(error, {"state": "closed", "cgroup_empty": True,
                                                       "cleanup_errors": ["private-cleanup-message"]})
    assert value["cleanup"] == {"closed": True, "cgroup_empty": True, "error_count": 1}
    assert "private" not in json.dumps(value)


def test_collection_diagnostic_has_finite_code_and_only_allowlisted_entry_metadata(native):
    error = native.ReadinessError("SCENARIO_EVIDENCE_ENTRY_INVALID")
    error.evidence_entry = {"name": "editorial-synthetic.png", "regular": True, "single_link": True,
                            "owner_matches": True, "mode": 0o664, "bytes": 100}
    value = native.collection_failure_metadata(error, None)
    assert value == {"code": "SCENARIO_EVIDENCE_ENTRY_INVALID", "entry": error.evidence_entry}
    error.evidence_entry = {**error.evidence_entry, "name": "private-secret-file"}
    assert native.collection_failure_metadata(error, None) == {"code": "SCENARIO_EVIDENCE_ENTRY_INVALID"}


def test_collection_cleanup_projection_emits_only_the_four_known_literal_codes(native):
    value = native.collection_failure_metadata(native.ReadinessError("SCENARIO_PRODUCER_CLEANUP_UNCONFIRMED"),
        {"state": "closed", "cgroup_empty": False,
         "cleanup_errors": ["owned_unit_stop_unconfirmed", "private-secret-error", {"raw": "private-value"},
                            "owned_cgroup_cleanup_unconfirmed"]})
    assert value["cleanup"]["error_count"] == 4
    assert value["cleanup"]["known_codes"] == ["owned_cgroup_cleanup_unconfirmed", "owned_unit_stop_unconfirmed"]
    assert "private" not in json.dumps(value)
