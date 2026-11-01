"""Read-only identity check, not CI acceptance, deployment or database access."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ORIGIN = "http://10.20.30.45:4007"
PROFILE = "legacy-private-lan-http-v1"
STARTUP_CONTRACT = "postiz-no-implicit-schema-v1"
SAFE_CMD = ["/bin/sh", "/adapter/start-production.sh"]
STARTUP_SCRIPT_SHA256 = "9272bf9f32b883f02269248962bf656966819015fd3f36f57f0a5774a445af41"
EXPECTED_ENV = {"MAIN_URL": ORIGIN, "FRONTEND_URL": ORIGIN,
                "NEXT_PUBLIC_BACKEND_URL": ORIGIN + "/api", "NOT_SECURED": "true",
                "PRODUCTION_ORIGIN_PROFILE": PROFILE}


def require(value, code):
    if not value:
        raise ValueError(code)


def inspect_json(arguments):
    result = subprocess.run(["docker", "--context", "default", *arguments],
                            capture_output=True, text=True, timeout=30, check=False)
    require(result.returncode == 0, "RELEASE_DOCKER_READ_FAILED")
    return json.loads(result.stdout)


def validate_image(value, ident):
    require(isinstance(value, dict) and value.get("Id") == ident, "RELEASE_IMAGE_ID_MISMATCH")
    require(isinstance(value.get("Config"), dict), "RELEASE_IMAGE_ENV_INVALID")
    require(value["Config"].get("Entrypoint") in (None, []) and value["Config"].get("Cmd") == SAFE_CMD,
            "RELEASE_SAFE_STARTUP_REQUIRED")
    rows = value["Config"].get("Env")
    require(isinstance(rows, list) and all(isinstance(row, str) for row in rows), "RELEASE_IMAGE_ENV_INVALID")
    for key, expected in EXPECTED_ENV.items():
        selected = [row.split("=", 1)[1] for row in rows if row.startswith(key + "=")]
        require(selected == [expected], "RELEASE_IMAGE_ORIGIN_PROFILE_MISMATCH")


def validate_origin_file(path):
    require(path.is_file() and not path.is_symlink(), "RELEASE_ENV_FILE_INVALID")
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() == "POSTIZ_PUBLIC_ORIGIN":
                values.append(value.strip().strip("\"'"))
    require(values == [ORIGIN] and os.environ.get("POSTIZ_PUBLIC_ORIGIN", ORIGIN) == ORIGIN,
            "RELEASE_PUBLIC_ORIGIN_MISMATCH")


def validate_acceptance(value, *, commit, ident, archive, digest):
    require(isinstance(value, dict) and value.get("contract") == "three-platform-native-acceptance-v1"
            and value.get("accepted") is True and value.get("native_checks_passed") is True,
            "RELEASE_NATIVE_ACCEPTANCE_REQUIRED")
    require(value.get("privacy_scan_completed") is True and type(value.get("public_secret_matches")) is int
            and value["public_secret_matches"] == 0, "RELEASE_PRIVACY_ACCEPTANCE_REQUIRED")
    cleanup = value.get("checks", {}).get("owned_engine_cleanup") if isinstance(value.get("checks"), dict) else None
    require(isinstance(cleanup, dict) and cleanup.get("closed") is True and cleanup.get("cgroup_empty") is True
            and cleanup.get("host_daemon_used") is False, "RELEASE_CLEANUP_ACCEPTANCE_REQUIRED")
    startup = value.get("checks", {}).get("default_cmd_fresh_clone")
    require(isinstance(startup, dict) and startup.get("passed") is True
            and startup.get("startup_contract") == STARTUP_CONTRACT
            and type(startup.get("implicit_schema_statements")) is int and startup["implicit_schema_statements"] == 0
            and startup.get("owner_cookie_login") is True, "RELEASE_NO_DDL_ACCEPTANCE_REQUIRED")
    require(isinstance(value.get("source_tree"), str) and re.fullmatch(r"[a-f0-9]{40}", value["source_tree"])
            and isinstance(value.get("source_manifest_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", value["source_manifest_sha256"]), "RELEASE_SOURCE_BINDING_REQUIRED")
    context = value.get("node_context_source_sha256")
    require(isinstance(context, dict) and context and all(isinstance(name, str) and isinstance(sha, str)
            and re.fullmatch(r"[a-f0-9]{64}", sha) for name, sha in context.items()), "RELEASE_CONTEXT_INVALID")
    require(context.get("start-production.sh") == STARTUP_SCRIPT_SHA256, "RELEASE_CONTEXT_STARTUP_MISMATCH")
    context_sha = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    require(value.get("source_commit") == commit and value.get("image_id") == ident
            and value.get("origin") == ORIGIN and value.get("origin_profile") == PROFILE
            and value.get("startup_contract") == STARTUP_CONTRACT
            and value.get("node_context_sha256") == context_sha, "RELEASE_ACCEPTANCE_BINDING_MISMATCH")
    portable = value.get("portable_image")
    require(isinstance(portable, dict) and portable.get("contract") == "tested-postiz-image-v1"
            and portable.get("source_commit") == commit and portable.get("image_id") == ident
            and portable.get("source_tree") == value["source_tree"]
            and portable.get("source_manifest_sha256") == value["source_manifest_sha256"]
            and portable.get("node_context_sha256") == context_sha and portable.get("origin") == ORIGIN
            and portable.get("startup_contract") == STARTUP_CONTRACT
            and portable.get("origin_profile") == PROFILE and portable.get("sha256") == digest
            and portable.get("filename") == archive.name and type(portable.get("bytes")) is int
            and portable["bytes"] > 0 and portable["bytes"] == archive.stat().st_size
            and portable.get("runtime_credentials_included") is False, "RELEASE_PORTABLE_IMAGE_MISMATCH")


def validate_source(root, commit, source_tree, source_manifest_sha256):
    manifest = root / "package-manifest.json"
    if manifest.exists():
        value = json.loads(manifest.read_text(encoding="utf-8"))
        require(isinstance(value, dict) and value.get("contract") == "exact-git-source-package-v1"
                and isinstance(value.get("commit"), str) and re.fullmatch(r"[a-f0-9]{40}", value["commit"])
                and isinstance(value.get("hashes"), dict) and value["hashes"],
                "RELEASE_SOURCE_COMMIT_MISMATCH")
        require(hashlib.sha256(json.dumps(value["hashes"], sort_keys=True).encode()).hexdigest() == source_manifest_sha256,
                "RELEASE_SOURCE_MANIFEST_MISMATCH")
        for name, expected in value["hashes"].items():
            require(isinstance(name, str) and isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected),
                    "RELEASE_SOURCE_MANIFEST_INVALID")
            path = root / name
            require(not Path(name).is_absolute() and ".." not in Path(name).parts and path.resolve().is_relative_to(root.resolve())
                    and path.is_file() and not any(part.is_symlink() for part in (path, *path.parents)), "RELEASE_SOURCE_PATH_INVALID")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, "RELEASE_SOURCE_HASH_MISMATCH")
        return
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, timeout=15, check=False)
    require(result.returncode == 0 and result.stdout.strip() == source_tree, "RELEASE_SOURCE_TREE_MISMATCH")
    result = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--"], capture_output=True, timeout=15, check=False)
    require(result.returncode == 0, "RELEASE_SOURCE_DIRTY")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args(argv)
    try:
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", arguments.image_id), "RELEASE_IMMUTABLE_IMAGE_REQUIRED")
        require(re.fullmatch(r"[a-f0-9]{64}", arguments.archive_sha256), "RELEASE_ARCHIVE_SHA_REQUIRED")
        require(re.fullmatch(r"[a-f0-9]{40}", arguments.source_commit), "RELEASE_SOURCE_COMMIT_REQUIRED")
        require(not any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "BUILDKIT_HOST")),
                "RELEASE_AMBIENT_DOCKER_FORBIDDEN")
        validate_origin_file(arguments.env_file)
        require(arguments.archive.is_file() and not arguments.archive.is_symlink(), "RELEASE_ARCHIVE_INVALID")
        digest = hashlib.sha256()
        with arguments.archive.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        require(digest.hexdigest() == arguments.archive_sha256, "RELEASE_ARCHIVE_SHA_MISMATCH")
        require(arguments.acceptance.is_file() and not arguments.acceptance.is_symlink()
                and arguments.acceptance.stat().st_size <= 2 * 1024 * 1024, "RELEASE_ACCEPTANCE_FILE_INVALID")
        acceptance = json.loads(arguments.acceptance.read_text(encoding="utf-8"))
        validate_acceptance(acceptance, commit=arguments.source_commit, ident=arguments.image_id,
                            archive=arguments.archive, digest=arguments.archive_sha256)
        validate_source(Path(__file__).resolve().parents[2], arguments.source_commit,
                        acceptance["source_tree"], acceptance["source_manifest_sha256"])
        endpoint = inspect_json(["context", "inspect", "default", "--format", "{{json .Endpoints.docker.Host}}"])
        require(endpoint == "unix:///var/run/docker.sock", "RELEASE_LOCAL_DOCKER_REQUIRED")
        image = inspect_json(["image", "inspect", arguments.image_id, "--format", "{{json .}}"])
        validate_image(image, arguments.image_id)
        print(json.dumps({"verification": "local_loaded_image_identity", "image_id": arguments.image_id,
                          "archive_sha256": arguments.archive_sha256, "origin": ORIGIN, "origin_profile": PROFILE,
                          "source_commit": arguments.source_commit, "deployment_performed": False,
                          "startup_contract": STARTUP_CONTRACT,
                          "ci_acceptance_record_bound": True, "ci_run_authenticity_checked_by_this_script": False}))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"RELEASE_[A-Z_]+", str(error)) else "RELEASE_CHECK_FAILED"
        print(code)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
