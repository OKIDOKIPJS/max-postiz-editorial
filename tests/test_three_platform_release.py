"""Pure release guard checks. No Docker, VM, browser or database is accessed."""

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("three_platform_image", ROOT / "deployment/linux/verify-three-platform-image.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
IDENT = "sha256:" + "a" * 64
COMMIT = "b" * 40


def image():
    return {"Id": IDENT, "Config": {"Env": [key + "=" + value for key, value in module.EXPECTED_ENV.items()],
                                    "Entrypoint": [], "Cmd": list(module.SAFE_CMD)}}


def acceptance(archive):
    context = {"Dockerfile.production": "a" * 64, "start-production.sh": module.STARTUP_SCRIPT_SHA256}
    digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    return {"contract": "three-platform-native-acceptance-v1", "accepted": True, "native_checks_passed": True,
            "privacy_scan_completed": True, "public_secret_matches": 0,
            "checks": {"owned_engine_cleanup": {"closed": True, "cgroup_empty": True, "host_daemon_used": False},
                       "default_cmd_fresh_clone": {"passed": True, "startup_contract": module.STARTUP_CONTRACT,
                                                   "implicit_schema_statements": 0, "owner_cookie_login": True}},
            "source_tree": "d" * 40, "source_manifest_sha256": "e" * 64,
            "source_commit": COMMIT, "image_id": IDENT, "origin": module.ORIGIN, "origin_profile": module.PROFILE,
            "startup_contract": module.STARTUP_CONTRACT,
            "node_context_source_sha256": context, "node_context_sha256": digest,
            "portable_image": {"contract": "tested-postiz-image-v1", "source_commit": COMMIT, "image_id": IDENT,
                "source_tree": "d" * 40,
                "source_manifest_sha256": "e" * 64,
                "node_context_sha256": digest, "origin": module.ORIGIN, "origin_profile": module.PROFILE,
                "startup_contract": module.STARTUP_CONTRACT,
                "filename": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "bytes": archive.stat().st_size, "runtime_credentials_included": False}}


def test_loaded_release_requires_exact_id_and_same_baked_lan_origin_profile():
    module.validate_image(image(), IDENT)
    for name in module.EXPECTED_ENV:
        value = image()
        value["Config"]["Env"] = [row for row in value["Config"]["Env"] if not row.startswith(name + "=")]
        with pytest.raises(ValueError, match="RELEASE_IMAGE_ORIGIN_PROFILE_MISMATCH"):
            module.validate_image(value, IDENT)
    with pytest.raises(ValueError, match="RELEASE_IMAGE_ID_MISMATCH"):
        module.validate_image(image(), "sha256:" + "b" * 64)


@pytest.mark.parametrize("replacement", ["http://127.0.0.1:14007", "http://10.20.30.45:4008", "https://10.20.30.45:4007"])
def test_loopback_or_different_origin_cannot_be_fixed_by_runtime_environment(replacement):
    value = image()
    value["Config"]["Env"][0] = "MAIN_URL=" + replacement
    with pytest.raises(ValueError):
        module.validate_image(value, IDENT)


def test_duplicate_or_malformed_image_env_is_rejected():
    value = image()
    value["Config"]["Env"].append("MAIN_URL=" + module.ORIGIN)
    with pytest.raises(ValueError):
        module.validate_image(value, IDENT)
    with pytest.raises(ValueError):
        module.validate_image({"Id": IDENT, "Config": None}, IDENT)


def test_inherited_entrypoint_or_root_pm2_startup_cannot_run_on_existing_database():
    for patch in ({"Entrypoint": ["docker-entrypoint.sh"]}, {"Cmd": ["sh", "-c", "nginx && pnpm run pm2"]},
                  {"Cmd": ["/bin/sh", "-c", "prisma db push"]}):
        value = image()
        value["Config"].update(patch)
        with pytest.raises(ValueError, match="RELEASE_SAFE_STARTUP_REQUIRED"):
            module.validate_image(value, IDENT)
    value = image()
    value["Config"]["Entrypoint"] = None
    module.validate_image(value, IDENT)


def test_private_env_origin_must_be_unique_and_ambient_override_cannot_change_it(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("POSTIZ_PUBLIC_ORIGIN=" + module.ORIGIN + "\nPRIVATE_PASSWORD=never-print-me\n")
    monkeypatch.delenv("POSTIZ_PUBLIC_ORIGIN", raising=False)
    module.validate_origin_file(path)
    monkeypatch.setenv("POSTIZ_PUBLIC_ORIGIN", "http://other.invalid")
    with pytest.raises(ValueError):
        module.validate_origin_file(path)
    monkeypatch.delenv("POSTIZ_PUBLIC_ORIGIN")
    path.write_text("POSTIZ_PUBLIC_ORIGIN=" + module.ORIGIN + "\nPOSTIZ_PUBLIC_ORIGIN=" + module.ORIGIN + "\n")
    with pytest.raises(ValueError):
        module.validate_origin_file(path)


def test_readonly_main_verifies_archive_and_uses_only_fixed_inspect_calls(tmp_path, monkeypatch, capsys):
    archive, env = tmp_path / "image.tar.gz", tmp_path / ".env"
    archive.write_bytes(b"synthetic-image-archive")
    env.write_text("POSTIZ_PUBLIC_ORIGIN=" + module.ORIGIN)
    proof = tmp_path / "acceptance.json"
    proof.write_text(json.dumps(acceptance(archive)))
    monkeypatch.setattr(module, "validate_source", lambda *a: None)
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "BUILDKIT_HOST", "POSTIZ_PUBLIC_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["docker", "--context", "default"]
        assert kwargs["capture_output"] is True and kwargs["timeout"] == 30
        value = "unix:///var/run/docker.sock" if command[3] == "context" else image()
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
    monkeypatch.setattr(module.subprocess, "run", run)
    arguments = ["--image-id", IDENT, "--archive", str(archive), "--archive-sha256", hashlib.sha256(archive.read_bytes()).hexdigest(),
                 "--env-file", str(env), "--acceptance", str(proof), "--source-commit", COMMIT]
    assert module.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["deployment_performed"] is False
    assert report["ci_acceptance_record_bound"] is True
    assert report["ci_run_authenticity_checked_by_this_script"] is False
    assert [call[3:5] for call in calls] == [["context", "inspect"], ["image", "inspect"]]
    calls.clear()
    archive.write_bytes(b"changed")
    assert module.main(arguments) == 2
    assert not calls


@pytest.mark.parametrize("key,value", [("accepted", False), ("native_checks_passed", False),
    ("source_commit", "c" * 40), ("image_id", "sha256:" + "d" * 64),
    ("origin", "http://127.0.0.1:14007"), ("node_context_sha256", "e" * 64),
    ("privacy_scan_completed", False), ("public_secret_matches", 1),
    ("checks", {"owned_engine_cleanup": {"closed": True, "cgroup_empty": False, "host_daemon_used": False}}),
    ("startup_contract", "legacy-root-pm2"), ("source_tree", "invalid")])
def test_acceptance_must_be_successful_and_bind_this_exact_release(tmp_path, key, value):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"archive")
    proof = acceptance(archive)
    proof[key] = value
    with pytest.raises(ValueError):
        module.validate_acceptance(proof, commit=COMMIT, ident=IDENT, archive=archive,
                                   digest=hashlib.sha256(archive.read_bytes()).hexdigest())


def test_portable_image_and_source_manifest_tampering_fail_closed(tmp_path):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"archive")
    proof = acceptance(archive)
    proof["portable_image"]["runtime_credentials_included"] = True
    with pytest.raises(ValueError):
        module.validate_acceptance(proof, commit=COMMIT, ident=IDENT, archive=archive,
                                   digest=hashlib.sha256(archive.read_bytes()).hexdigest())
    source = tmp_path / "source"
    source.mkdir()
    target = source / "public.py"
    target.write_bytes(b"original")
    (source / "package-manifest.json").write_text(json.dumps({"contract": "exact-git-source-package-v1", "commit": COMMIT,
        "hashes": {"public.py": hashlib.sha256(target.read_bytes()).hexdigest()}}))
    hashes = {"public.py": hashlib.sha256(target.read_bytes()).hexdigest()}
    sha = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    module.validate_source(source, COMMIT, "d" * 40, sha)
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="RELEASE_SOURCE_HASH_MISMATCH"):
        module.validate_source(source, COMMIT, "d" * 40, sha)


def test_template_is_last_override_and_future_switch_has_no_database_or_cleanup_actions():
    overlay = (ROOT / "deployment/linux/compose.three-platform.yaml").read_text()
    script = (ROOT / "deployment/linux/postiz-release.sh").read_text()
    assert "image: ${EDITORIAL_POSTIZ_IMAGE:?" in overlay and "pull_policy: never" in overlay
    assert "RUN_CRON: ''" in overlay and ",main,max" in overlay
    assert script.index("-f deployment/linux/compose.yaml") < script.index("-f deployment/linux/compose.three-platform.yaml")
    assert 'mode=${1:---check}' in script and 'config --quiet' in script
    assert "up -d --no-deps --pull never postiz" in script
    for forbidden in ("down -v", "docker system prune", "pg_restore", "sync_targets", "configure-runtime.py"):
        assert forbidden not in script
    assert "max-postiz-linux:0.11.2-illustrated-cas" in (ROOT / "deployment/linux/compose.yaml").read_text()


def test_clean_main_merge_can_share_tested_tree_without_matching_feature_commit(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "d" * 40 + "\n" if "rev-parse" in command else "", "")
    monkeypatch.setattr(module.subprocess, "run", run)
    module.validate_source(tmp_path, COMMIT, "d" * 40, "e" * 64)
    assert "HEAD^{tree}" in calls[0] and calls[1][-3:] == ["--quiet", "HEAD", "--"]
    with pytest.raises(ValueError, match="RELEASE_SOURCE_TREE_MISMATCH"):
        module.validate_source(tmp_path, COMMIT, "f" * 40, "e" * 64)


def test_no_ddl_evidence_is_typed_and_source_startup_bytes_are_exact(tmp_path):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"archive")
    for invalid in (None, False, 1):
        proof = acceptance(archive)
        proof["checks"]["default_cmd_fresh_clone"]["implicit_schema_statements"] = invalid
        with pytest.raises(ValueError, match="RELEASE_NO_DDL_ACCEPTANCE_REQUIRED"):
            module.validate_acceptance(proof, commit=COMMIT, ident=IDENT, archive=archive,
                                       digest=proof["portable_image"]["sha256"])
    proof = acceptance(archive)
    proof["node_context_source_sha256"]["start-production.sh"] = "f" * 64
    with pytest.raises(ValueError, match="RELEASE_CONTEXT_STARTUP_MISMATCH"):
        module.validate_acceptance(proof, commit=COMMIT, ident=IDENT, archive=archive,
                                   digest=proof["portable_image"]["sha256"])
