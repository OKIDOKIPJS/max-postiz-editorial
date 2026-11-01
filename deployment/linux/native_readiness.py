"""Full native acceptance in a disposable GitHub VM; never a server deployment.

The only application network is one loopback namespace. Its retained LAN origin
is an alias on that namespace's lo, not a connection to the working server.
Only explicitly selected, sanitized evidence leaves this disposable environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

from readiness_engine import Engine, LABEL_KEY
from readiness_docker import prepare as prepare_docker

GIB = 1024 ** 3
ORIGIN = "http://10.20.30.45:4007"
PROFILE = "legacy-private-lan-http-v1"
STARTUP_CONTRACT = "postiz-no-implicit-schema-v1"
SAFE_COMMAND = ["/bin/sh", "/adapter/start-production.sh"]
ENGINE_FAILURE_STAGES = frozenset({
    "NOT_STARTED", "GUARD", "UNIT_LIST_QUERY", "UNIT_FILE_LIST_QUERY", "ANCHOR_START", "SLICE_LIMITS",
    "SLICE_IDENTITY", "CGROUP_CAPTURE", "DAEMON_START", "DAEMON_READY", "EMPTY_ENGINE_CHECK",
    "CLEANUP_CONTAINERS", "CLEANUP_DAEMON_STOP", "CLEANUP_ANCHOR_STOP", "CLEANUP_CGROUP",
    "CLEANUP_SLICE_STOP", "CLEANUP_SLICE_REVERT",
})
BASE = "ghcr.io/gitroomhq/postiz-app@sha256:d2b37802e96757729fdea2d3d71287f841c2b619faa1cb4c21dac07df657fde1"
PINS = {
    "postgres": "postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24",
    "redis": "redis@sha256:46f15cf64fdef63e483dcda99e74e3d51c017b1175547b6bb874152ee2391aad",
    "temporal-postgres": "postgres@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d",
    "temporal": "temporalio/auto-setup@sha256:607d68caa111338d754771efb876c92dfcdae06d056e4530bb31cd0f37406e6a",
    "elasticsearch": "elasticsearch@sha256:9a6443f55243f6acbfeb4a112d15eb3b9aac74bf25e0e39fa19b3ddd3a6879d0",
}
# Measured on the earlier isolated image of this same pinned upstream. Reserve
# two native layer sets plus dependencies/safety, rather than assuming 14 GB fits.
PREVIOUS_NATIVE_IMAGE_BYTES = 11804114796
REQUIRED_DISK_BYTES = 2 * PREVIOUS_NATIVE_IMAGE_BYTES + 8 * GIB
EPHEMERAL_UNUSED_SDKS = (
    "/usr/local/lib/android", "/usr/share/dotnet", "/opt/ghc", "/usr/local/.ghcup",
    "/usr/local/swift", "/usr/local/share/powershell",
)
QUEUES = "bluesky,devto,discord,dribbble,email,email-bulk,facebook,gmb,hashnode,instagram,kick,lemmy,linkedin,listmonk,mastodon,medium,mewe,moltbook,nostr,pinterest,reddit,skool,slack,telegram,threads,tiktok,tumblr,twitch,vk,whop,wordpress,wrapcast,x,youtube,main,max"


class ReadinessError(RuntimeError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def count_logged_schema_statements(text):
    """Count SQL events after PostgreSQL itself classifies log_statement=ddl.

    Simple and extended protocols use different prefixes. SQL can begin with
    comments or span lines, so neither command words nor SQL content are parsed.
    ERROR/DETAIL/STATEMENT records are not LOG events.
    """
    event = re.compile(r"^(?:[^\r\n]*\]\s+)?LOG:[ \t]+(?:statement:|execute[ \t]+[^:\r\n]+:)")
    return sum(bool(event.match(line)) for line in text.splitlines())


def github_guard(environment=None, platform=None, uid=None):
    env = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    uid = os.geteuid() if uid is None and hasattr(os, "geteuid") else uid
    if (platform != "linux" or uid != 0 or env.get("GITHUB_ACTIONS") != "true"
            or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or not re.fullmatch(r"[a-f0-9]{40}", env.get("GITHUB_SHA", ""))):
        raise ReadinessError("EPHEMERAL_GITHUB_RUNNER_REQUIRED")


def private_write(path, value, *, exclusive=True):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def disk_preflight(path, workspace):
    github_guard()
    path, workspace = Path(path).resolve(), Path(workspace).resolve()
    initial = shutil.disk_usage(path).free
    removed = []
    for name in EPHEMERAL_UNUSED_SDKS:
        if shutil.disk_usage(path).free >= REQUIRED_DISK_BYTES:
            break
        target = Path(name)
        if target.is_symlink() or target.resolve() != target or workspace.is_relative_to(target):
            raise ReadinessError("EPHEMERAL_SDK_PATH_INVALID")
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(name)
    free = shutil.disk_usage(path).free
    if free < REQUIRED_DISK_BYTES:
        raise ReadinessError("NATIVE_IMAGE_DISK_CAPACITY_REQUIRED")
    return {"free_before_bytes": initial, "free_after_bytes": free, "required_bytes": REQUIRED_DISK_BYTES,
            "estimate_native_image_bytes": PREVIOUS_NATIVE_IMAGE_BYTES, "ephemeral_sdk_dirs_removed": removed,
            "docker_prune_performed": False}


def synthetic_secrets(value):
    result = set()
    for key, item in value.items():
        if isinstance(item, dict):
            result.update(synthetic_secrets(item))
        elif isinstance(item, str) and any(word in key.lower() for word in ("password", "secret", "token", "key", "cookie", "dsn")):
            if len(item) >= 8:
                result.add(item)
            try:
                password = urlsplit(item).password
                if password and len(password) >= 8:
                    result.add(unquote(password))
            except ValueError:
                pass
    return result


def scan_public(folder, secrets_to_find):
    matches = 0
    for path in Path(folder).rglob("*"):
        if path.is_symlink():
            raise ReadinessError("PUBLIC_ARTIFACT_SYMLINK_FORBIDDEN")
        if path.is_file():
            if path.suffix not in {".json", ".png", ".txt", ".xml"}:
                raise ReadinessError("PUBLIC_ARTIFACT_TYPE_INVALID")
            raw = path.read_bytes()
            matches += sum(secret.encode() in raw or secret in path.relative_to(folder).as_posix() for secret in secrets_to_find)
    if matches:
        raise ReadinessError("PUBLIC_ARTIFACT_SECRET_MATCH")
    return matches


def finalize_public(staging, report, secrets_to_find, validated):
    """Publish a directory only after the entire candidate set passes privacy."""
    staging, validated = Path(staging), Path(validated)
    if (staging.is_symlink() or validated.exists() or validated.is_symlink()
            or staging.parent.resolve() != validated.parent.resolve() or staging == validated):
        raise ReadinessError("PUBLIC_FINALIZATION_PATH_INVALID")
    candidate = dict(report, privacy_scan_completed=True, public_secret_matches=0)
    (staging / "acceptance.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
    try:
        scan_public(staging, secrets_to_find)
    except BaseException:
        quarantine = staging.with_name(staging.name + "-quarantine-" + uuid.uuid4().hex)
        staging.rename(quarantine)
        quarantine.chmod(0o700)
        validated.mkdir(mode=0o755)
        (validated / "acceptance.json").write_text(json.dumps({
            "accepted": False, "code": "PUBLIC_ARTIFACT_PRIVACY_FAILED", "unvalidated_files_quarantined": True,
            "external_publication_performed_by_release_checks": False}))
        (validated / ".privacy-verified.json").write_text(json.dumps({"contract": "native-public-privacy-v1",
            "files": {"acceptance.json": digest((validated / "acceptance.json").read_bytes())}, "secret_matches": 0}))
        return False
    hashes = {path.name: digest(path.read_bytes()) for path in staging.iterdir() if path.is_file()}
    (staging / "SHA256SUMS.txt").write_text("".join(value + "  " + name + "\n" for name, value in sorted(hashes.items())))
    (staging / ".privacy-verified.json").write_text(json.dumps({"contract": "native-public-privacy-v1",
        "files": {path.name: digest(path.read_bytes()) for path in staging.iterdir() if path.is_file()}, "secret_matches": 0}))
    staging.rename(validated)
    for path in validated.iterdir():
        if path.is_file():
            path.chmod(0o644)
    validated.chmod(0o755)
    return True


class Acceptance:
    def __init__(self, workspace, evidence):
        self.workspace, self.public = Path(workspace).resolve(), Path(evidence)
        self.job = Path(tempfile.mkdtemp(prefix="mp-ready-", dir=os.environ["RUNNER_TEMP"]))
        self.private = self.job / "private"
        self.private.mkdir(mode=0o700)
        self.engine = None
        self.unit_names = []
        self.created_opt = None
        self.current_phase = "preflight"
        self.report = {"contract": "three-platform-native-acceptance-v1", "accepted": False,
                       "source_commit": os.environ["GITHUB_SHA"], "origin": ORIGIN, "origin_profile": PROFILE,
                       "scope": "ephemeral_github_hosted_native_full_stack_no_external_publications",
                       "external_publication_performed_by_release_checks": False,
                       "working_server_contacted": False, "pika_actions": 0,
                       "https_transport_tested": False, "checks": {}}

    def phase(self, name):
        self.current_phase = name
        print(json.dumps({"phase": name}), flush=True)

    def run(self, command, *, name, timeout=300, check=True, input=None, env=None):
        logfile = self.private / (name + ".log")
        with logfile.open("xb") as log:
            result = subprocess.run(command, input=input, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False, env=env)
        if check and result.returncode != 0:
            raise ReadinessError("NATIVE_STAGE_FAILED_" + name.upper().replace("-", "_"))
        return result

    def source_copy(self):
        commit = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.workspace),
            "-C", str(self.workspace), "rev-parse", "HEAD"], text=True).strip()
        if commit != os.environ["GITHUB_SHA"]:
            raise ReadinessError("SOURCE_COMMIT_MISMATCH")
        clean = subprocess.run(["git", "-c", "safe.directory=" + str(self.workspace), "-C", str(self.workspace),
                                "diff", "--quiet", "HEAD", "--"], check=False)
        if clean.returncode != 0:
            raise ReadinessError("SOURCE_WORKTREE_MODIFIED")
        tree = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.workspace),
            "-C", str(self.workspace), "rev-parse", "HEAD^{tree}"], text=True).strip()
        if not re.fullmatch(r"[a-f0-9]{40}", tree):
            raise ReadinessError("SOURCE_TREE_INVALID")
        names = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.workspace),
            "-C", str(self.workspace), "ls-files", "-z"]).decode().split("\0")
        self.created_opt = Path("/opt/max-postiz-readiness-" + self.engine.label_value[:12])
        if self.created_opt.exists() or self.created_opt.is_symlink():
            raise ReadinessError("OWN_PROJECT_ALREADY_EXISTS")
        self.created_opt.mkdir(mode=0o755)
        self.project = self.created_opt / "project"
        self.project.mkdir(mode=0o755)
        hashes = {}
        for name in filter(None, names):
            relative = Path(name)
            source = self.workspace / relative
            if relative.is_absolute() or ".." in relative.parts or source.is_symlink():
                raise ReadinessError("SOURCE_PATH_INVALID")
            raw = source.read_bytes()
            target = self.project / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o755 if name.endswith(".sh") else 0o644)
            hashes[name] = digest(raw)
        version = tomllib.loads((self.project / "pyproject.toml").read_text())["project"]["version"]
        if version != "0.14.0":
            raise ReadinessError("READINESS_CANDIDATE_VERSION_INVALID")
        self.report.update(version=version, source_tree=tree, source_file_count=len(hashes),
            source_manifest_sha256=digest(json.dumps(hashes, sort_keys=True).encode()))
        private_write(self.private / "source-files.json", hashes)

    def dependencies(self):
        self.phase("native_dependencies")
        self.run(["apt-get", "update", "-qq"], name="apt-update", timeout=300)
        self.run(["apt-get", "install", "-y", "--no-install-recommends", "xvfb", "x11vnc", "websockify",
                  "novnc", "polkitd", "iproute2", "util-linux", "ca-certificates", "curl"],
                 name="apt-native", timeout=600)
        self.python = self.project / ".venv/bin/python"
        self.run([sys.executable, "-m", "venv", str(self.project / ".venv")], name="venv", timeout=60)
        self.run([str(self.python), "-m", "pip", "install", "--disable-pip-version-check", "-r",
                  str(self.project / "requirements.lock"), "-r", str(self.project / "requirements-dev.lock"),
                  "-r", str(self.project / "requirements-browser.lock")], name="pip-locked", timeout=600)
        self.run([str(self.python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e",
                  str(self.project)], name="pip-project", timeout=120)
        self.run([str(self.python), "-m", "pip", "check"], name="pip-check", timeout=60)
        self.browsers = self.project / ".playwright"
        self.browser_env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(self.browsers), PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="120000")
        self.run([str(self.python), "-m", "playwright", "install", "--with-deps", "chrome"],
                 name="playwright-native", timeout=600, env=self.browser_env)
        self.report["checks"]["locked_native_dependencies"] = True

    def image_pull(self):
        self.phase("pinned_images")
        for role, image in {"postiz": BASE, **PINS}.items():
            with (self.private / ("pull-" + role + ".log")).open("wb") as log:
                self.engine.docker("pull", image, timeout=900, stdout=log, text=False)
            value = json.loads(self.engine.docker("image", "inspect", image).stdout)[0]
            if image not in value.get("RepoDigests", []):
                raise ReadinessError("PINNED_IMAGE_DIGEST_MISMATCH")
        helptext = self.engine.docker("build", "--help").stdout
        if "--resource" not in helptext:
            raise ReadinessError("NATIVE_DOCKER_BUILD_RESOURCE_UNSUPPORTED")

    def docker_versions(self):
        value = json.loads(self.engine.docker("version", "--format", "{{json .}}").stdout)
        if value["Client"]["Version"] != "29.9.2" or value["Server"]["Version"] != "29.9.2":
            raise ReadinessError("NATIVE_DOCKER_PIN_VERSION_MISMATCH")
        self.run(self.engine.namespace_command([str(self.engine.docker_binary), "buildx", "version"]),
                 name="buildx-version", timeout=30, env=self.engine.environment)
        self.run(self.engine.namespace_command([str(self.engine.docker_binary), "buildx", "inspect", "default"]),
                 name="buildkit-version", timeout=30, env=self.engine.environment)
        buildx = (self.private / "buildx-version.log").read_text()
        buildkit = (self.private / "buildkit-version.log").read_text()
        plugin = re.search(r"\bv(\d+\.\d+\.\d+)(?:\s|$)", buildx)
        backend = re.search(r"(?mi)^\s*BuildKit(?:\s+version)?:\s+v?(\d+\.\d+\.\d+)(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)", buildkit)
        if not plugin or not backend or tuple(map(int, backend[1].split("."))) < (0, 31, 0):
            raise ReadinessError("NATIVE_BUILDKIT_RESOURCE_CAPABILITY_REQUIRED")
        self.report["checks"]["actual_docker_versions"] = {"client": value["Client"]["Version"],
            "server": value["Server"]["Version"], "buildx": plugin[1], "buildkit": backend[1],
            "buildkit_inspect_sha256": digest(buildkit.encode()), "host_binaries_changed": False}

    def build_flags(self):
        limits = self.engine.commonlimits
        return ["--resource", "memory=" + str(limits["native_memory_bytes"]),
                "--resource", "memory-swap=" + str(limits["native_memory_bytes"] + limits["native_swap_bytes"]),
                "--resource", "cpu-period=100000", "--resource", "cpu-quota=400000",
                "--cgroup-parent=" + self.engine.cgroup_parent, "--network=none", "--progress=plain",
                "--label", LABEL_KEY + "=" + self.engine.label_value]

    def build(self):
        self.phase("native_build")
        context = self.project / "deployment/postiz-max"
        self.tag = "max-postiz-linux:0.14.0-native-" + self.engine.label_value[:12]
        files = {path.relative_to(context).as_posix(): digest(path.read_bytes())
                 for path in context.rglob("*") if path.is_file()}
        self.report["node_context_sha256"] = digest(json.dumps(files, sort_keys=True).encode())
        self.report["node_context_files"] = len(files)
        self.report["node_context_source_sha256"] = files
        # A real resource/network probe on the same pinned base precedes heavy RUNs.
        probe = self.engine.root / "probe"
        probe.mkdir()
        script = "const fs=require('node:fs');console.log('RESOURCE_PROOF '+JSON.stringify({memory:fs.readFileSync('/sys/fs/cgroup/memory.max','utf8').trim(),swap:fs.readFileSync('/sys/fs/cgroup/memory.swap.max','utf8').trim(),cpu:fs.readFileSync('/sys/fs/cgroup/cpu.max','utf8').trim(),devices:fs.readFileSync('/proc/net/dev','utf8').split('\\n').slice(2).filter(Boolean).map(x=>x.split(':')[0].trim()),routes:fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').slice(1),cgroup:fs.readFileSync('/proc/self/cgroup','utf8'),cgroup_mounts:fs.readFileSync('/proc/self/mountinfo','utf8').split('\\n').filter(x=>x.includes(' - cgroup2 '))}))"
        (probe / "Dockerfile").write_text("FROM " + BASE + "\nRUN " + json.dumps(["node", "-e", script]) + "\n")
        self.run(self.engine.namespace_command([str(self.engine.docker_binary), "build", "--builder", "default", *self.build_flags(), "--tag",
            "max-postiz-linux:resource-" + self.engine.label_value[:12], str(probe)]),
            name="resource-probe", timeout=180, env=self.engine.environment)
        raw = (self.private / "resource-probe.log").read_text()
        match = re.search(r"RESOURCE_PROOF (\{[^\n]+\})", raw)
        if not match:
            raise ReadinessError("NATIVE_RESOURCE_PROOF_MISSING")
        observed = json.loads(match[1])
        limits = self.engine.commonlimits
        self.report["checks"]["build_resource_probe_observed"] = observed
        expected = {"memory": str(limits["native_memory_bytes"]), "swap": str(limits["native_swap_bytes"]),
                    "cpu": "400000 100000", "devices": ["lo"], "routes": []}
        if any(observed.get(key) != value for key, value in expected.items()):
            raise ReadinessError("NATIVE_RESOURCE_OR_NETWORK_FENCE_INVALID")
        self.report["checks"]["actual_build_resource_and_network_fence"] = observed
        wrapper = self.engine.root / "build.cjs"
        wrapper.write_text("const{spawnSync}=require('node:child_process');const{main}=require(" +
            json.dumps(str(context / "build-production-image.cjs")) + ");const flags=" + json.dumps([flag for flag in self.build_flags() if flag != "--network=none"]) +
            ";function run(binary,args,options){if(args[0]==='--context'&&args[2]==='build')args=[...args.slice(0,3),...flags,...args.slice(3)];return spawnSync(binary,args,options);}try{console.log(JSON.stringify(main(" +
            json.dumps(["--build", "--origin", ORIGIN, "--origin-profile", PROFILE, "--tag", self.tag]) +
            ",run," + json.dumps(str(context)) + ",process.env)));}catch(error){console.error(error.message);process.exitCode=1;}")
        command = self.engine.namespace_command(["node", str(wrapper)])
        self.run(command, name="full-native-build", timeout=7200, env=self.engine.environment)
        if any(digest((context / name).read_bytes()) != sha for name, sha in files.items()):
            raise ReadinessError("NATIVE_CONTEXT_CHANGED")
        image = json.loads(self.engine.docker("image", "inspect", self.tag).stdout)[0]
        self.image = image["Id"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image):
            raise ReadinessError("NATIVE_IMAGE_ID_INVALID")
        self.report.update(image_id=self.image, image_tag=self.tag, full_native_build_completed=True,
                           default_cmd_executed=False, build_network="none")

    def inspect_image(self):
        self.phase("compiled_artifacts")
        configuration = json.loads(self.engine.docker("image", "inspect", self.image).stdout)[0]["Config"]
        if configuration.get("Entrypoint") not in (None, []) or configuration.get("Cmd") != SAFE_COMMAND:
            raise ReadinessError("IMAGE_STARTUP_CONTRACT_INVALID")
        script = r"""const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),cp=require('node:child_process');
const required=[];for(const app of ['backend','orchestrator']){
 const root='/app/apps/'+app+'/dist/';required.push(root+'apps/'+app+'/src/main.js');
 for(const file of ['integration.manager.js','social/max-browser.provider.js'])required.push(root+'libraries/nestjs-libraries/src/integrations/'+file);
 required.push(root+'libraries/nestjs-libraries/src/temporal/temporal.module.js');}
for(const file of ['max-browser.guard.js','api/routes/editorial.controller.js','api/api.module.js'])required.push('/app/apps/backend/dist/apps/backend/src/'+file);
for(const file of required){if(!fs.statSync(file).isFile())throw Error('COMPILED_NATIVE_MISSING');}
for(const app of ['backend','orchestrator']){const p='/app/apps/'+app+'/dist/apps/'+app+'/src/main.js';if(cp.spawnSync(process.execPath,['--check',p]).status!==0)throw Error('NATIVE_SYNTAX_INVALID');}
const main=fs.readFileSync('/app/apps/backend/dist/apps/backend/src/main.js','utf8');
if(!main.includes('/editorial/route-admin')||!main.includes('15mb'))throw Error('SCOPED_BODY_PARSER_MISSING');
const controller=fs.readFileSync('/app/apps/backend/dist/apps/backend/src/api/routes/editorial.controller.js','utf8');
for(const value of ['route-admin','route-setup','EDITORIAL_COOKIE_SESSION_REQUIRED','EDITORIAL_OWNER_REQUIRED'])if(!controller.includes(value))throw Error('EDITORIAL_CONTROLLER_MISSING');
const next='/app/apps/frontend/.next';if(!fs.readFileSync(next+'/BUILD_ID','utf8').trim())throw Error('NEXT_BUILD_ID_MISSING');
const paths=JSON.parse(fs.readFileSync(next+'/server/app-paths-manifest.json'));
const key='/(app)/(site)/editorial/page';if(!Object.hasOwn(paths,key))throw Error('EDITORIAL_ROUTE_MISSING');
const expected='app/(app)/(site)/editorial/page.js';if(paths[key]!==expected||!fs.statSync(next+'/server/'+expected).isFile())throw Error('EDITORIAL_ROUTE_WRONG');
const refs=fs.readFileSync(next+'/server/app/(app)/(site)/editorial/page_client-reference-manifest.js','utf8');
if(!refs.includes('editorial-panel'))throw Error('EDITORIAL_CLIENT_NOT_COMPILED');
const css=[...refs.matchAll(/static\/css\/[A-Za-z0-9_.-]+\.css/g)].map(x=>x[0]);
if(!css.length||css.some(file=>!fs.statSync(next+'/'+file).isFile()))throw Error('EDITORIAL_CSS_MISSING');
const startup=require('/adapter/production-startup.cjs').validateStartup('/app');
console.log(JSON.stringify({passed:true,compiled_files:required.length,full_next_route:true,client_reference:true,css_files:new Set(css).size,
 scoped_body_parser:true,entrypoints_syntax_checked:true,default_cmd_executed:false,startup}));"""
        inspected = self.engine.docker("run", "--rm", "--label", LABEL_KEY + "=" + self.engine.label_value,
            "--cgroup-parent", self.engine.cgroup_parent, "--network", "none", "--read-only", "--memory", "512m",
            "--entrypoint", "node", self.image, "-e", script, timeout=60)
        proof = json.loads(inspected.stdout)
        if proof.get("passed") is not True:
            raise ReadinessError("NATIVE_ARTIFACTS_NOT_VERIFIED")
        if proof.get("startup", {}).get("startup_contract") != STARTUP_CONTRACT:
            raise ReadinessError("IMAGE_STARTUP_CONTRACT_INVALID")
        self.report["startup_contract"] = STARTUP_CONTRACT
        self.report["checks"]["compiled_artifacts"] = proof

    def volume(self, role):
        name = "mp-native-" + self.engine.label_value[:12] + "-" + role
        if self.engine.docker("volume", "ls", "-q", "--filter", "name=^" + name + "$").stdout.strip():
            raise ReadinessError("FRESH_VOLUME_REQUIRED")
        self.engine.docker("volume", "create", "--label", LABEL_KEY + "=" + self.engine.label_value, name)
        return name

    def container(self, role, image, *, env=None, volumes=(), command=(), entrypoint=None,
                  memory="512m", swap="1536m", extra=(), read_only=True, join=True):
        name = "mp-native-" + self.engine.label_value[:12] + "-" + role
        if self.engine.docker("ps", "-aq", "--filter", "name=^/" + name + "$").stdout.strip():
            raise ReadinessError("FRESH_CONTAINER_REQUIRED")
        args = ["run", "--detach", "--name", name, "--label", LABEL_KEY + "=" + self.engine.label_value,
                "--label", "editorial.readiness.role=" + role, "--cgroup-parent", self.engine.cgroup_parent,
                "--network", "container:" + self.anchor if join else "none", "--security-opt", "no-new-privileges:true",
                "--memory", memory, "--memory-swap", swap, "--cpus", "2", "--pids-limit", "1024",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", *extra]  # noqa: S108 - a private container tmpfs, not host storage.
        if read_only:
            args.append("--read-only")
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        for key, value in (env or {}).items():
            args += ["--env", key + "=" + str(value)]
        for value in volumes:
            args += ["--volume", value]
        value = self.engine.docker(*args, image, *command).stdout.strip()
        inspected = json.loads(self.engine.docker("inspect", value).stdout)[0]
        if (inspected["Config"]["Labels"].get(LABEL_KEY) != self.engine.label_value
                or inspected["HostConfig"]["CgroupParent"] != self.engine.cgroup_parent
                or inspected["HostConfig"]["PortBindings"]):
            raise ReadinessError("OWN_CONTAINER_SCOPE_INVALID")
        return value

    def namespace(self, command):
        return ["/usr/bin/nsenter", "--target", str(self.anchor_pid), "--net", *command]

    def namespace_proof(self):
        rows = json.loads(subprocess.check_output(self.namespace(["ip", "-j", "address"])))
        routes = json.loads(subprocess.check_output(self.namespace(["ip", "-j", "route"])))
        if ([row["ifname"] for row in rows] != ["lo"] or routes
                or not any(row["local"] == "10.20.30.45" and row["prefixlen"] == 32 for row in rows[0]["addr_info"])):
            raise ReadinessError("RUNTIME_NETWORK_FENCE_INVALID")
        return {"interfaces": ["lo"], "default_routes": 0,
                "namespace_inode": os.stat("/proc/" + str(self.anchor_pid) + "/ns/net").st_ino,
                "origin_alias_is_loopback_only": True, "working_server_contacted": False}

    def wait_exec(self, ident, command, *, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = self.engine.docker("exec", ident, *command, check=False, timeout=10)
            if result.returncode == 0:
                return result.stdout
            state = json.loads(self.engine.docker("inspect", ident).stdout)[0]["State"]
            if not state["Running"]:
                raise ReadinessError("DEPENDENCY_EXITED_BEFORE_READY")
            time.sleep(1)
        raise ReadinessError("DEPENDENCY_READINESS_TIMEOUT")

    def runtime_dependencies(self):
        self.phase("real_database_runtime")
        self.anchor = self.container("network", self.image, entrypoint="/bin/sleep", command=["infinity"],
                                     join=False, memory="32m", swap="96m")
        self.anchor_pid = json.loads(self.engine.docker("inspect", self.anchor).stdout)[0]["State"]["Pid"]
        subprocess.run(self.namespace(["ip", "address", "add", "10.20.30.45/32", "dev", "lo"]), check=True, timeout=10)
        self.report["checks"]["runtime_namespace"] = self.namespace_proof()
        self.runtime_passwords = {key: secrets.token_hex(32) for key in ("postiz", "hub", "temporal", "redis", "jwt", "internal")}
        self.pg = {}
        for role, image, port, user, database in (
                ("postiz", PINS["postgres"], 5432, "postiz_user", "postiz_readiness"),
                ("temporal", PINS["temporal-postgres"], 5433, "temporal", "temporal"),
                ("hub", PINS["postgres"], 5434, "hub_admin", "editorial_hub")):
            ident = self.container(role + "-pg", image, env={"POSTGRES_USER": user, "POSTGRES_DB": database,
                "POSTGRES_PASSWORD": self.runtime_passwords[role], "PGDATA": "/var/lib/postgresql/data/pgdata"},
                volumes=[self.volume(role + "-pg") + ":/var/lib/postgresql/data"], memory="192m", swap="768m",
                extra=["--user", "postgres", "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,mode=1777"],
                command=["postgres", "-p", str(port), "-c", "listen_addresses=127.0.0.1", "-c", "shared_buffers=32MB", "-c", "max_connections=50", "-c", "log_statement=ddl"])
            self.pg[role] = (ident, port, user, database)
            self.wait_exec(ident, ["pg_isready", "-h", "127.0.0.1", "-p", str(port), "-U", user, "-d", database])
            count = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", database,
                "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").stdout.strip()
            if count != "0":
                raise ReadinessError("INITIAL_EMPTY_DATABASE_REQUIRED")
        self.redis = self.container("redis", PINS["redis"], extra=["--user", "redis"],
            volumes=[self.volume("redis") + ":/data"], memory="96m", swap="256m",
            command=["redis-server", "--bind", "127.0.0.1", "--appendonly", "yes", "--maxmemory", "32mb",
                     "--maxmemory-policy", "noeviction", "--requirepass", self.runtime_passwords["redis"]])
        if self.wait_exec(self.redis, ["redis-cli", "-a", self.runtime_passwords["redis"], "PING"]).strip() != "PONG":
            raise ReadinessError("REDIS_AUTHENTICATED_HEALTH_FAILED")
        self.es = self.container("elasticsearch", PINS["elasticsearch"], read_only=False,
            volumes=[self.volume("elasticsearch") + ":/usr/share/elasticsearch/data"],
            env={"discovery.type": "single-node", "xpack.security.enabled": "false", "ES_JAVA_OPTS": "-Xms128m -Xmx256m",
                 "network.host": "127.0.0.1"}, extra=["--ulimit", "nofile=65536:65536"])
        self.wait_exec(self.es, ["curl", "--fail", "--silent", "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=3s"])
        self.temporal = self.container("temporal", PINS["temporal"], read_only=False, env={
            "DB": "postgres12", "DB_PORT": "5433", "POSTGRES_USER": "temporal", "POSTGRES_PWD": self.runtime_passwords["temporal"],
            "POSTGRES_SEEDS": "127.0.0.1", "BIND_ON_IP": "127.0.0.1", "TEMPORAL_BROADCAST_ADDRESS": "127.0.0.1",
            "ENABLE_ES": "true", "ES_SEEDS": "127.0.0.1", "ES_VERSION": "v7", "TEMPORAL_NAMESPACE": "default"})
        self.wait_exec(self.temporal, ["temporal", "operator", "cluster", "health", "--address", "127.0.0.1:7233", "--command-timeout", "4s"], seconds=240)
        self.report["checks"]["real_databases_redis_temporal"] = {"postgres_instances": 3, "initially_empty": True,
            "temporal_ready": True, "elasticsearch_ready": True, "network": "loopback_namespace_only"}

    def nonroot(self):
        return ["/usr/sbin/runuser", "--user", "editorial", "--", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
                "HOME=" + str(self.project / "data/ci-home"), "PYTHONUTF8=1",
                "LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib"),
                "GITHUB_ACTIONS=true", "RUNNER_ENVIRONMENT=github-hosted",
                "PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers)]

    def scenario(self, mode):
        self.namespace_proof()
        result = self.run(self.namespace([*self.nonroot(), str(self.python),
            str(self.project / "deployment/linux/readiness_scenario.py"), "--root", str(self.project),
            "--state", str(self.state_path), "--evidence", str(self.public), "--mode", mode]),
            name="scenario-" + mode, timeout=600)
        self.namespace_proof()
        return result

    def create_user_and_state(self):
        import pwd

        try:
            pwd.getpwnam("editorial")
        except KeyError:
            pass
        else:
            raise ReadinessError("FRESH_EDITORIAL_USER_REQUIRED")
        subprocess.run(["/usr/sbin/useradd", "--system", "--user-group", "--home-dir",
                        str(self.project / "data/ci-home"), "--shell", "/usr/sbin/nologin", "editorial"], check=True, timeout=30)
        self.user = pwd.getpwnam("editorial")
        # Atomic credential replacement creates a sibling .env.local temporary.
        # Production helpers stay non-root, including when the app owns this dir.
        os.chown(self.project, self.user.pw_uid, self.user.pw_gid)
        sentinel = "mp-native-sentinel-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(sentinel)
        subprocess.run(["systemd-run", "--unit=" + sentinel, "--uid=editorial", "--slice=" + self.engine.slice,
                        "--service-type=exec", "--collect", "--property=NoNewPrivileges=yes",
                        "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
                        "--property=MemoryMax=16M", "/bin/sleep", "infinity"], check=True, timeout=30,
                       capture_output=True)
        sentinel_pid = int(subprocess.check_output(["systemctl", "show", sentinel, "-p", "MainPID", "--value"], text=True))
        for name in ("data", "data/ci-private", "data/ci-home"):
            path = self.project / name
            path.mkdir(mode=0o700, exist_ok=True)
            os.chown(path, self.user.pw_uid, self.user.pw_gid)
        os.chown(self.public, self.user.pw_uid, self.user.pw_gid)
        self.state_path = self.project / "data/ci-private/state.json"
        state = {"contract": "three-platform-native-readiness-v1",
            "scope": "ephemeral_github_hosted_no_external_publications", "new_empty_runtime": True,
            "runtime_id": self.engine.label_value, "engine_id": self.engine.engine_id, "root": str(self.project),
            "namespace_owner_pid": sentinel_pid, "namespace_inode": self.namespace_proof()["namespace_inode"],
            "public_origin": ORIGIN, "private_postiz_base": "http://127.0.0.1:14007/api/public/v1",
            "postiz_dsn": "postgresql://postiz_user:" + self.runtime_passwords["postiz"] + "@127.0.0.1:5432/postiz_readiness",
            "hub_dsn": "postgresql+psycopg://hub_admin:" + self.runtime_passwords["hub"] + "@127.0.0.1:5434/editorial_hub",
            "owner_email": "native-owner@example.invalid", "owner_password": secrets.token_urlsafe(24),
            "jwt_secret": self.runtime_passwords["jwt"], "internal_key": self.runtime_passwords["internal"],
            "profile_id": "test", "vk_owner": "123456789", "service_unit": "max-postiz-editorial.service",
            "login_service_unit": "max-postiz-login.service"}
        private_write(self.state_path, state)
        os.chown(self.state_path, self.user.pw_uid, self.user.pw_gid)

    def bootstrap_project(self):
        self.phase("fresh_editorial_bootstrap")
        self.run([str(self.python), "-m", "max_postiz_agent.hub_cli", "--root", str(self.project), "init"],
                 name="initial-config", timeout=60)
        app_path = self.project / "config/app.json"
        app = json.loads(app_path.read_text())
        app["require_postgresql"] = True
        app["opencode"]["adapter_mode"] = "fake"
        app["max"]["adapter_mode"] = "browser"
        app["max"]["browser"] = {"profile_dir": "data/max-browser-ci", "channel": "chrome", "headless": True,
                                   "operation_timeout_seconds": 30, "channels": {"-123": "CI MAX"}}
        app["video"]["backend"] = "disabled"
        app_path.write_text(json.dumps(app, ensure_ascii=False, indent=2))
        sources = self.project / "config/sources.json"
        value = json.loads(sources.read_text())
        for source in value["sources"]:
            source["enabled"] = False
        sources.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        publishing = {"schema_version": "1.0", "postiz": {"base_url_env": "POSTIZ_BASE_URL", "api_key_env": "POSTIZ_API_KEY",
                      "approval_guard_verified": False, "max_video_verified": False}, "targets": [], "tasks": []}
        (self.project / "config/publishing.json").write_text(json.dumps(publishing))
        # Runtime configuration of the copied project only; the Git checkout remains unchanged.
        topic_path = self.project / "TOPIC.md"
        topic = topic_path.read_text()
        match = re.search(r"(<!-- TOPIC_CONFIG_BEGIN -->\s*```json\s*)(.*?)(\s*```\s*<!-- TOPIC_CONFIG_END -->)", topic, re.S)
        if not match:
            raise ReadinessError("TOPIC_CONFIG_MARKER_MISSING")
        payload = json.loads(match[2])
        for channel in payload["channels"]:
            channel["enabled"] = channel["id"] == "test"
            if channel["id"] == "test":
                channel["publication"]["mode"] = "review"
        topic_path.write_text(topic[:match.start(2)] + json.dumps(payload, ensure_ascii=False, indent=2) + topic[match.end(2):])
        for path in (self.project / "config").rglob("*"):
            if path.is_file() and path.name.endswith(".json"):
                os.chown(path, self.user.pw_uid, self.user.pw_gid)
                path.chmod(0o600)
        os.chown(topic_path, self.user.pw_uid, self.user.pw_gid)
        os.chown(self.project / "config", self.user.pw_uid, self.user.pw_gid)

    def app_environment(self, state, *, database=None):
        result = {"MAIN_URL": ORIGIN, "FRONTEND_URL": ORIGIN, "NEXT_PUBLIC_BACKEND_URL": ORIGIN + "/api",
            "DATABASE_URL": database or state["postiz_dsn"],
            "REDIS_URL": "redis://:" + self.runtime_passwords["redis"] + "@127.0.0.1:6379",
            "JWT_SECRET": state["jwt_secret"], "TEMPORAL_ADDRESS": "127.0.0.1:7233", "TEMPORAL_NAMESPACE": "default",
            "BACKEND_INTERNAL_URL": "http://127.0.0.1:3000", "NOT_SECURED": "true", "IS_GENERAL": "true",
            "DISABLE_REGISTRATION": "true", "RUN_CRON": "", "EXCLUDE_QUEUE": QUEUES,
            "STORAGE_PROVIDER": "local", "UPLOAD_DIRECTORY": "/uploads", "NEXT_PUBLIC_UPLOAD_DIRECTORY": "/uploads",
            "NODE_OPTIONS": "--max-old-space-size=1024", "NEXT_TELEMETRY_DISABLED": "1",
            "EDITORIAL_INTERNAL_URL": "http://127.0.0.1:8092/internal/editorial", "EDITORIAL_INTERNAL_PORT": "8092",
            "EDITORIAL_INTERNAL_KEY": state["internal_key"],
            "EDITORIAL_OWNER_USER_ID": state.get("owner_id", "not-configured"),
            "EDITORIAL_OWNER_ORGANIZATION_ID": state.get("org_id", "not-configured")}
        return result

    def start_postiz(self):
        self.phase("postiz_owner_authentication")
        state = json.loads(self.state_path.read_text())
        environment = self.app_environment(state)
        # Explicit schema operation against the already verified new empty database only.
        schema = self.container("prisma-init", self.image, env=environment, entrypoint="pnpm", memory="1g", swap="2g",
            command=["exec", "prisma", "db", "push", "--skip-generate", "--schema",
                     "./libraries/nestjs-libraries/src/database/prisma/schema.prisma"], read_only=False)
        code = self.engine.docker("wait", schema, timeout=180).stdout.strip()
        if code != "0":
            raise ReadinessError("FRESH_POSTIZ_SCHEMA_FAILED")
        self.upload_volume = self.volume("uploads")
        self.backend = self.container("backend-bootstrap", self.image, env=environment, entrypoint="node", memory="1536m", swap="3g",
            volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/backend/dist/apps/backend/src/main.js"])
        self.frontend = self.container("frontend", self.image, env=environment, entrypoint="node", memory="1536m", swap="3g",
            extra=["--tmpfs", "/app/apps/frontend/.next/cache:rw,nosuid,nodev,size=128m"],
            volumes=[self.upload_volume + ":/uploads"],
            command=["/app/node_modules/next/dist/bin/next", "start", "/app/apps/frontend", "-p", "4200", "-H", "127.0.0.1"])
        proxy_dir = self.engine.root / "proxy"
        proxy_dir.mkdir()
        outer = (self.project / "deployment/linux/editorial-postiz.nginx.conf").read_text()
        proxy = "user www;worker_processes 1;pid /tmp/nginx.pid;error_log /tmp/nginx-error.log warn;events{worker_connections 512;}http{include /etc/nginx/mime.types;access_log off;client_body_temp_path /tmp/client-body;proxy_temp_path /tmp/proxy;" + outer
        proxy += "server{listen 127.0.0.1:14007;client_max_body_size 15m;location /api/ {proxy_pass http://127.0.0.1:3000/;proxy_set_header Host $http_host;proxy_set_header X-Forwarded-Proto $scheme;}location / {proxy_pass http://127.0.0.1:4200;proxy_set_header Host $http_host;proxy_set_header X-Forwarded-Proto $scheme;}}}"
        (proxy_dir / "nginx.conf").write_text(proxy)
        self.proxy = self.container("proxy", self.image, entrypoint="/usr/sbin/nginx", memory="96m", swap="256m",
            volumes=[str(proxy_dir) + ":/readiness:ro"], command=["-c", "/readiness/nginx.conf", "-g", "daemon off;"])
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:14007/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"], seconds=180)
        self.scenario("bootstrap-owner")
        state = json.loads(self.state_path.read_text())
        if not all(isinstance(state.get(key), str) and state[key] for key in ("owner_id", "org_id", "postiz_api_key")):
            raise ReadinessError("SYNTHETIC_POSTIZ_OWNER_REQUIRED")
        self.engine.docker("stop", "--time", "20", self.backend)
        self.backend = self.container("backend", self.image, env=self.app_environment(state), entrypoint="node",
            memory="1536m", swap="3g", volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/backend/dist/apps/backend/src/main.js"])
        self.orchestrator = self.container("orchestrator", self.image, env=self.app_environment(state), entrypoint="node",
            memory="768m", swap="2g", volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/orchestrator/dist/apps/orchestrator/src/main.js"])
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:14007/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"])

    def editorial_environment(self, state, *, hub_dsn=None):
        path = self.project / ".env.local"
        values = {}
        for line in path.read_text().splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        values.update(DATABASE_URL=hub_dsn or state["hub_dsn"], POSTIZ_BASE_URL=state["private_postiz_base"],
            POSTIZ_API_KEY=state["postiz_api_key"], POSTIZ_PUBLIC_ORIGIN=ORIGIN,
            EDITORIAL_INTERNAL_KEY=state["internal_key"], EDITORIAL_OWNER_USER_ID=state["owner_id"],
            EDITORIAL_OWNER_ORGANIZATION_ID=state["org_id"], EDITORIAL_INTERNAL_PORT="8092")
        path.write_text("\n".join(key + "=" + value for key, value in values.items()) + "\n")
        os.chown(path, self.user.pw_uid, self.user.pw_gid)
        path.chmod(0o600)

    def install_editorial(self):
        self.phase("production_editorial_entrypoint")
        state = json.loads(self.state_path.read_text())
        self.editorial_environment(state)
        self.run(self.namespace([*self.nonroot(), str(self.python), "-m", "max_postiz_agent.server_cli", "--root",
            str(self.project), "migrate"]), name="explicit-agent-migrate", timeout=120)
        unit = "max-postiz-editorial.service"
        destination = Path("/etc/systemd/system") / unit
        if destination.exists() or destination.is_symlink():
            raise ReadinessError("OWN_SERVICE_NAME_ALREADY_EXISTS")
        source = (self.project / "deployment/linux" / unit).read_text().replace("/opt/max-postiz", str(self.project))
        destination.write_text(source)
        self.unit_names.append(unit)
        dropin = destination.with_name(unit + ".d")
        dropin.mkdir()
        (dropin / "readiness.conf").write_text("[Unit]\nWants=\n[Service]\nNetworkNamespacePath=/proc/" + str(self.anchor_pid) +
            "/ns/net\nSlice=" + self.engine.slice + "\nEnvironment=HOME=" + str(self.project / "data/ci-home") +
            "\nEnvironment=PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers) +
            "\nEnvironment=LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib") + "\nMemoryMax=1G\nMemorySwapMax=2G\n")
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        self.run(["bash", str(self.project / "deployment/linux/install-owner-portals.sh"), "--check", "--root", str(self.project)],
                 name="portal-installer-check", timeout=30)
        self.run(["bash", str(self.project / "deployment/linux/install-owner-portals.sh"), "--install", "--root", str(self.project)],
                 name="portal-installer-install", timeout=60)
        self.unit_names += ["max-postiz-login.path", "max-postiz-editorial-restart.path", "max-postiz-login.service",
                            "max-postiz-editorial-restart.service", "max-postiz-novnc.service", "max-postiz-vnc.service", "max-postiz-display.service"]
        for name in self.unit_names[1:]:
            if not name.endswith(".service") or name == unit:
                continue
            folder = Path("/etc/systemd/system") / (name + ".d")
            folder.mkdir(exist_ok=True)
            (folder / "readiness.conf").write_text("[Service]\nNetworkNamespacePath=/proc/" + str(self.anchor_pid) +
                "/ns/net\nSlice=" + self.engine.slice + "\nEnvironment=HOME=" + str(self.project / "data/ci-home") +
                "\nEnvironment=PLAYWRIGHT_BROWSERS_PATH=" + str(self.browsers) +
                "\nEnvironment=LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib") + "\n")
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "start", unit], check=True, timeout=60)
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:8092/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"])
        self.scenario("exercise")

    def supervisor_and_single_worker(self):
        self.phase("native_supervisor_and_single_worker")
        unit = "mp-native-supervisor-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(unit)
        xml = self.project / "data/ci-private/supervisor.xml"
        command = ["systemd-run", "--wait", "--pipe", "--collect", "--unit=" + unit, "--uid=editorial",
            "--slice=" + self.engine.slice, "--property=Delegate=yes", "--property=NoNewPrivileges=yes",
            "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
            "--property=WorkingDirectory=" + str(self.project), "--property=RuntimeMaxSec=180",
            "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "PYTHONUTF8=1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "LD_LIBRARY_PATH=" + str(Path(sys.base_prefix) / "lib"),
            str(self.python), "-m", "pytest", "--noconftest", "tests/test_browser_process.py",
            "--basetemp=" + str(self.project / "data/ci-private/supervisor-temp"), "--junitxml=" + str(xml)]
        self.run(command, name="native-supervisor", timeout=200)
        suites = ET.parse(xml).getroot()  # noqa: S314 - produced by this exact owned pytest process, not uploaded XML.
        results = [row for row in suites.iter("testcase")]
        if not results or any(row.find("failure") is not None or row.find("error") is not None or row.find("skipped") is not None for row in results):
            raise ReadinessError("NATIVE_SUPERVISOR_NOT_ALL_PASSED")
        second = "mp-native-second-worker-" + self.engine.label_value[:12] + ".service"
        self.unit_names.append(second)
        duplicate = self.run(["systemd-run", "--wait", "--pipe", "--collect", "--unit=" + second, "--uid=editorial",
            "--slice=" + self.engine.slice, "--property=NetworkNamespacePath=/proc/" + str(self.anchor_pid) + "/ns/net",
            "--property=WorkingDirectory=" + str(self.project), "--property=RuntimeMaxSec=30",
            str(self.python), "-m", "max_postiz_agent.server_cli", "--root", str(self.project),
            "serve", "--host", "127.0.0.1", "--port", "8093"], name="second-worker", timeout=40, check=False)
        if duplicate.returncode == 0 or "EDITORIAL_WORKER_ALREADY_RUNNING" not in (self.private / "second-worker.log").read_text():
            raise ReadinessError("SINGLE_WORKER_FENCE_NOT_CONFIRMED")
        self.report["checks"]["native_supervisor"] = {"passed": len(results), "skipped": 0, "single_worker_fence": True}

    def portal(self):
        self.phase("owner_portal_native_offline")
        # This is a private probe, not an alternate product login URL. The
        # production helper opens its real allowlisted URL in a network with no
        # external interface. Account authentication is deliberately not claimed.
        script = self.project / "data/ci-private/portal-probe.py"
        code = r'''
import base64,hashlib,json,os,pathlib,socket,subprocess,sys,time
sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'deployment/linux'))
from readiness_scenario import Scenario,atomic,require
s=Scenario(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]));s.login()
result=[]
try:
 for platform in ('max','vk'):
  setup=s.setup();target=next(x for x in setup['targets'] if x['id']=={'max':'ci_max','vk':'ci_vk'}[platform])
  value=s.admin({'action':'prepare_login','id':target['id'],'expected_binding_hash':target['binding_hash']})
  require(value['login_url']=='/owner-browser/' and value['live_verified'] is False,'PORTAL_SELECTION_INVALID')
  deadline=time.monotonic()+60;seen=False;group=None;started=False
  while time.monotonic()<deadline:
   state=subprocess.check_output(['systemctl','show','max-postiz-login.service','-p','ControlGroup','-p','ActiveState'],text=True)
   values=dict(line.split('=',1) for line in state.splitlines() if '=' in line)
   if values.get('ControlGroup'):
    group=pathlib.Path('/sys/fs/cgroup'+values['ControlGroup'])
    for item in group.rglob('cgroup.procs'):
     try:
      for pid in item.read_text().split():
       try:seen=seen or pathlib.Path('/proc/'+pid+'/comm').read_text().startswith('chrome')
       except FileNotFoundError:pass
     except FileNotFoundError:pass
   # A previous platform's failed unit is not the new path activation.
   started=started or values.get('ActiveState') in {'active','activating'} or seen
   if started and values.get('ActiveState') in {'failed','inactive'}:break
   time.sleep(.02)
  require(seen,'PORTAL_NATIVE_CHROME_NOT_OBSERVED')
  selection=json.loads((s.root/'data/owner-browser-selection.json').read_text())
  require(selection['target_id']==target['id'],'PORTAL_WRONG_PROFILE')
  profile=s.root/selection['profile_dir'];require(profile.is_dir(),'PORTAL_PROFILE_NOT_CREATED')
  if group is not None and group.exists():
   require(all(not item.read_text().strip() for item in group.rglob('cgroup.procs')),'PORTAL_WORKER_NOT_CLEANED')
  response=s.client.get('/owner-browser/vnc.html',follow_redirects=False)
  require(response.status_code==200 and 'novnc' in response.text.lower(),'PORTAL_OWNER_VNC_PAGE_FAILED')
  import httpx
  with httpx.Client(trust_env=False,follow_redirects=False) as anon:
   require(anon.get(s.state['public_origin']+'/owner-browser/vnc.html').status_code in (401,403),'PORTAL_ANONYMOUS_ACCESS')
  key=base64.b64encode(os.urandom(16)).decode();cookie=s.client.cookies.get('auth');require(bool(cookie),'PORTAL_COOKIE_MISSING')
  with socket.create_connection(('10.20.30.45',4007),timeout=5) as wire:
   message='GET /owner-browser/websockify HTTP/1.1\r\nHost: 10.20.30.45:4007\r\nOrigin: '+s.state['public_origin']+'\r\nCookie: auth='+cookie+'\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\nSec-WebSocket-Key: '+key+'\r\n\r\n'
   wire.sendall(message.encode());raw=b''
   while b'\r\n\r\n' not in raw and len(raw)<8192:raw+=wire.recv(1024)
   require(raw.startswith(b'HTTP/1.1 101'),'PORTAL_WEBSOCKET_FAILED')
   header,body=raw.split(b'\r\n\r\n',1)
   expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
   require(expected.lower() in header.lower(),'PORTAL_WEBSOCKET_ACCEPT_INVALID')
   while b'RFB ' not in body and len(body)<512:body+=wire.recv(128)
   require(b'RFB ' in body,'PORTAL_RFB_HANDSHAKE_FAILED')
  result.append({'platform':platform,'owner_request':True,'native_chrome_observed':True,'dedicated_profile_created':True,
    'supervised_cleanup':True,'vnc_owner_guard':True,'websocket_rfb':True,'account_authenticated':False,'external_delivery_verified':False})
  if platform=='max':
   # The actual product intentionally retains its ten-minute selection lease.
   require(selection['expires_at']<=time.time()+601,'PORTAL_EXPIRY_INVALID')
   while time.time()<=selection['expires_at']:time.sleep(1)
 s.check();atomic(s.evidence/'portal.json',{'passed':True,'checks':result,'fixture_scope':'native_mechanism_with_external_navigation_unavailable','external_publications':0},exclusive=True)
finally:s.client.close()
'''
        script.write_text(code)
        os.chown(script, self.user.pw_uid, self.user.pw_gid)
        self.run(self.namespace([*self.nonroot(), str(self.python), str(script), str(self.project), str(self.state_path), str(self.public)]),
                 name="native-owner-portal", timeout=780)
        self.report["checks"]["owner_portal"] = {"max_and_vk_native_mechanism": True, "vnc_owner_cookie_guard": True,
            "account_authenticated": False, "external_delivery_verified": False, "selection_expiry_seconds": 600}

    def clone_database(self, role, new_database):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", new_database):
            raise ReadinessError("RESTORE_DATABASE_NAME_INVALID")
        ident, port, user, database = self.pg[role]
        exists = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", database,
            "-tAc", "SELECT datname FROM pg_database").stdout.splitlines()
        if new_database in exists:
            raise ReadinessError("RESTORE_DESTINATION_ALREADY_EXISTS")
        self.engine.docker("exec", ident, "createdb", "-p", str(port), "-U", user, new_database)
        count = self.engine.docker("exec", ident, "psql", "-p", str(port), "-U", user, "-d", new_database,
            "-tAc", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").stdout.strip()
        if count != "0":
            raise ReadinessError("RESTORE_DESTINATION_NOT_EMPTY")
        dump = self.engine.docker("exec", ident, "pg_dump", "-Fc", "-p", str(port), "-U", user,
                                 "--no-owner", "--no-privileges", database, text=False, timeout=120).stdout
        path = self.private / (role + "-" + new_database + ".dump")
        with path.open("xb") as stream:
            stream.write(dump)
        self.engine.docker("exec", "-i", ident, "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
            "-p", str(port), "-U", user, "-d", new_database, input=dump, text=False, timeout=120)
        return {"sha256": digest(dump), "bytes": len(dump), "empty_destination_verified": True}

    def backup_restore(self):
        self.phase("backup_restore_new_clones")
        self.scenario("snapshot")
        subprocess.run(["systemctl", "stop", "max-postiz-editorial.service"], check=True, timeout=45)
        files = {}
        for folder in (self.project / "config", self.project / "data"):
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink() and "ci-private" not in path.parts:
                    files[path.relative_to(self.project).as_posix()] = digest(path.read_bytes())
        files[".env.local"] = digest((self.project / ".env.local").read_bytes())
        private_write(self.private / "runtime-backup-files.json", files)
        self.engine.docker("stop", "--time", "20", self.backend)
        self.engine.docker("stop", "--time", "20", self.orchestrator)
        backup = {role: self.clone_database(role, name) for role, name in
                  (("postiz", "postiz_readiness_restore"), ("hub", "editorial_hub_restore"))}
        state = json.loads(self.state_path.read_text())
        state["postiz_dsn"] = state["postiz_dsn"].rsplit("/", 1)[0] + "/postiz_readiness_restore"
        state["hub_dsn"] = state["hub_dsn"].rsplit("/", 1)[0] + "/editorial_hub_restore"
        private_write(self.state_path, state, exclusive=False)
        self.editorial_environment(state)
        self.backend = self.container("backend-restored", self.image, env=self.app_environment(state), entrypoint="node",
            memory="1536m", swap="3g", volumes=[self.upload_volume + ":/uploads"],
            command=["--experimental-require-module", "/app/apps/backend/dist/apps/backend/src/main.js"])
        subprocess.run(["systemctl", "start", "max-postiz-editorial.service"], check=True, timeout=60)
        self.wait_exec(self.proxy, ["node", "-e", "require('http').get('http://127.0.0.1:8092/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"])
        self.scenario("verify-restored")
        # DSNs are deliberately changed in .env.local. Other original runtime
        # configuration, journals and history files remain byte-identical.
        if any(digest((self.project / name).read_bytes()) != sha for name, sha in files.items()
               if name != ".env.local"):
            raise ReadinessError("RESTORE_RUNTIME_FILES_CHANGED")
        self.report["checks"]["backup_restore"] = {"databases": backup, "history_and_unknown_preserved": True,
            "runtime_files_preserved_in_place": True, "runtime_files_checked": len(files) - 1,
            "all_paused": True, "claims_not_reset": True}

    def default_boot(self):
        self.phase("default_entrypoint_fresh_clone")
        # Test the exact release CMD, with cleared inherited ENTRYPOINT. Initial
        # schema creation was an explicit operation; normal boot performs no DDL.
        self.clone_database("postiz", "postiz_default_smoke")
        for ident in (self.backend, self.frontend):
            self.engine.docker("stop", "--time", "20", ident)
        state = json.loads(self.state_path.read_text())
        database = state["postiz_dsn"].rsplit("/", 1)[0] + "/postiz_default_smoke"
        logging = self.engine.docker("exec", self.pg["postiz"][0], "psql", "-U", "postiz_user",
            "-d", "postiz_default_smoke", "-tAc", "SHOW log_statement").stdout.strip()
        if logging != "ddl":
            raise ReadinessError("DEFAULT_STARTUP_DDL_OBSERVATION_UNAVAILABLE")
        second, nanosecond = divmod(time.time_ns(), 1_000_000_000)
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(second)) + f".{nanosecond:09d}Z"
        unified = self.container("default-smoke", self.image, env=self.app_environment(state, database=database),
            memory="3g", swap="5g", volumes=[self.upload_volume + ":/uploads"], read_only=False)
        self.wait_exec(unified, ["node", "-e", "require('http').get('http://127.0.0.1:5000/api/auth/can-register',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"], seconds=240)
        # The public proxy is now exercised through the actual unified nginx5000.
        proxy_path = self.engine.root / "proxy/nginx.conf"
        source = proxy_path.read_text()
        source = re.sub(r"server\{listen 127\.0\.0\.1:14007;.*\}\}$",
            "server{listen 127.0.0.1:14007;client_max_body_size 15m;location / {proxy_pass http://127.0.0.1:5000;proxy_set_header Host $http_host;proxy_set_header X-Forwarded-Proto $scheme;}}}", source)
        if "proxy_pass http://127.0.0.1:5000" not in source:
            raise ReadinessError("DEFAULT_PROXY_SWITCH_INVALID")
        proxy_path.write_text(source)
        self.engine.docker("exec", self.proxy, "/usr/sbin/nginx", "-s", "reload", "-c", "/readiness/nginx.conf")
        script = self.project / "data/ci-private/default-smoke.py"
        script.write_text("import sys,pathlib,json;sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'deployment/linux'));from readiness_scenario import Scenario,atomic,require;s=Scenario(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]));s.login();actual=s.postiz_snapshot();require(actual==s.state['postiz_snapshot'],'DEFAULT_IDENTITY_CHANGED');require(s.hub_snapshot()==s.state['hub_snapshot'],'DEFAULT_HUB_HISTORY_CHANGED');atomic(s.evidence/'default-cmd.json',{'passed':True,'owner_login':True,'identity_and_history_preserved':True,'external_publications':0},exclusive=True);s.client.close()")
        os.chown(script, self.user.pw_uid, self.user.pw_gid)
        state["postiz_dsn"] = database
        private_write(self.state_path, state, exclusive=False)
        self.run(self.namespace([*self.nonroot(), str(self.python), str(script), str(self.project), str(self.state_path), str(self.public)]),
                 name="default-container-owner", timeout=90)
        logs = self.engine.docker("logs", "--since", since, self.pg["postiz"][0])
        pglog = logs.stdout + logs.stderr
        # No other component is allowed to perform schema operations during this
        # window. log_statement=ddl records even a redundant CREATE/ALTER call.
        ddl_count = count_logged_schema_statements(pglog)
        if ddl_count:
            raise ReadinessError("DEFAULT_STARTUP_IMPLICIT_SCHEMA_CHANGE")
        self.report["checks"]["default_cmd_fresh_clone"] = {"passed": True, "bounded": True,
            "startup_contract": STARTUP_CONTRACT, "implicit_schema_statements": 0,
            "database_scope": "new_restored_synthetic_postiz_clone_only", "owner_cookie_login": True,
            "main_and_provider_queues_excluded": True, "all_agent_targets_paused": True}
        self.report["default_cmd_executed"] = True

    def export_image(self):
        import gzip

        self.phase("image_export")
        state = json.loads(self.state_path.read_text())
        value = json.loads(self.engine.docker("image", "inspect", self.image).stdout)[0]
        environment = "\n".join(value["Config"].get("Env", []))
        if any(secret in environment for secret in synthetic_secrets(state)):
            raise ReadinessError("IMAGE_CONTAINS_RUNTIME_SECRET")
        folder = Path(os.environ["RUNNER_TEMP"]) / "native-readiness-image"
        folder.mkdir(mode=0o755)
        target = folder / "max-postiz-0.14.0.tar.gz"
        command = [str(self.engine.docker_binary), "--host", self.engine.socket, "--config", str(self.engine.config_dir), "image", "save", self.image]
        with target.open("xb") as output, (self.private / "image-save.log").open("xb") as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, env=self.engine.environment)
            completed = []

            def compress():
                try:
                    with gzip.GzipFile(filename="", fileobj=output, mode="wb", compresslevel=3, mtime=0) as stream:
                        shutil.copyfileobj(process.stdout, stream, 1024 * 1024)
                    completed.append(True)
                except Exception:
                    completed.append(False)

            worker = threading.Thread(target=compress, daemon=True)
            worker.start()
            worker.join(timeout=600)
            if worker.is_alive():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                worker.join(timeout=5)
                raise ReadinessError("IMAGE_EXPORT_TIMEOUT")
            if completed != [True] or process.wait(timeout=10) != 0:
                raise ReadinessError("IMAGE_EXPORT_FAILED")
        with target.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {"contract": "tested-postiz-image-v1", "image_id": self.image, "source_commit": self.report["source_commit"],
            "source_tree": self.report["source_tree"], "source_manifest_sha256": self.report["source_manifest_sha256"],
            "startup_contract": STARTUP_CONTRACT,
            "node_context_sha256": self.report["node_context_sha256"], "origin": ORIGIN, "origin_profile": PROFILE,
            "filename": target.name, "sha256": sha, "bytes": target.stat().st_size, "runtime_credentials_included": False}
        (folder / "image-verification.json").write_text(json.dumps(manifest, indent=2))
        (folder / "SHA256SUMS.txt").write_text(sha + "  " + target.name + "\n")
        self.report["portable_image"] = manifest

    def cleanup_units(self):
        for unit in reversed(self.unit_names):
            subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=30, check=False)

    def execute(self):
        self.report["disk"] = disk_preflight(self.job, self.workspace)
        self.phase("pinned_docker_tools")
        binaries, provenance = prepare_docker(self.job / "docker-tools")
        self.report["checks"]["pinned_docker_tools"] = provenance
        self.phase("owned_engine_bootstrap")
        with Engine(self.job / "engine", self.private / "engine.json", binary_root=binaries) as engine:
            self.engine = engine
            try:
                self.docker_versions()
                self.source_copy()
                self.dependencies()
                self.image_pull()
                self.build()
                self.inspect_image()
                self.runtime_dependencies()
                self.create_user_and_state()
                self.bootstrap_project()
                self.start_postiz()
                self.install_editorial()
                self.supervisor_and_single_worker()
                self.portal()
                self.backup_restore()
                self.default_boot()
                self.namespace_proof()
                self.report["native_checks_passed"] = True
                self.export_image()
            finally:
                self.cleanup_units()
        engine_state = json.loads((self.private / "engine.json").read_text())
        if engine_state["state"] != "closed" or engine_state.get("cleanup_errors"):
            raise ReadinessError("OWN_ENGINE_CLEANUP_UNCONFIRMED")
        self.report["checks"]["owned_engine_cleanup"] = {"closed": True, "host_daemon_used": False,
            "cgroup_empty": engine_state["cgroup_empty"], "images_pruned": False, "volumes_pruned": False}
        self.report["accepted"] = True

    def final_report(self, error=None):
        self.report["finished_at_utc_ns"] = time.time_ns()
        if error is not None:
            message = str(error)
            self.report.update(accepted=False, failed_phase=self.current_phase,
                error_code=message if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", message) else "NATIVE_ACCEPTANCE_FAILED")
        values = set(getattr(self, "runtime_passwords", {}).values())
        if hasattr(self, "state_path") and self.state_path.exists():
            values.update(synthetic_secrets(json.loads(self.state_path.read_text())))
        if hasattr(self, "project") and (self.project / ".env.local").exists():
            for line in (self.project / ".env.local").read_text().splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values.update(synthetic_secrets({key: value}))
        # Only fixed codes and redacted bounded tails are exposed, never whole
        # logs, environment, cookie jars, browser profiles or database dumps.
        if error is not None:
            diagnostics = []
            for path in sorted(self.private.glob("*.log")):
                raw = path.read_bytes()
                text = raw.decode("utf-8", errors="replace")
                for value in sorted(values, key=len, reverse=True):
                    text = text.replace(value, "[REDACTED]")
                diagnostics.append({"stage": path.stem, "sha256": digest(raw), "tail": text[-16000:]})
            # An early daemon/unit probe may fail before any stage log exists.
            # Private tails may start midway through a secret. Publish no output
            # fragments at all: only typed enum/number fields and content hashes.
            engine_failure = self.private / "engine-failure.json"
            try:
                info = engine_failure.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                    raise ValueError
                raw = engine_failure.read_bytes()
                if len(raw) > 64 * 1024:
                    raise ValueError
                value = json.loads(raw)
                if (not isinstance(value, dict) or value.get("contract") != "github-owned-engine-failure-v1"
                        or not isinstance(value.get("stage"), str) or value["stage"] not in ENGINE_FAILURE_STAGES
                        or value.get("error_kind") not in ("exit", "timeout", "os_error")
                        or "returncode" not in value or not (value["returncode"] is None or type(value["returncode"]) is int)
                        or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                               for key in ("stdout_sha256", "stderr_sha256"))):
                    raise ValueError
                fields = ("contract", "stage", "error_kind", "returncode", "stdout_sha256", "stderr_sha256")
                diagnostics.append({"stage": "engine_failure", "sha256": digest(raw),
                                    "engine": {key: value[key] for key in fields}})
            except (OSError, ValueError, UnicodeError):
                pass
            (self.public / "diagnostic-summary.json").write_text(json.dumps(diagnostics, ensure_ascii=False))
        self.report["platform_account_login_performed"] = False
        self.report["artifact_upload_is_a_separate_workflow_result"] = True
        return self.report, values


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-hosted", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    github_guard()
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    if (not temporary.is_dir() or not workspace.is_dir() or args.evidence != temporary / "native-readiness-public"
            or args.evidence.exists() or args.evidence.is_symlink()):
        raise ReadinessError("NATIVE_OUTPUT_PATH_INVALID")
    os.environ["RUNNER_TEMP"] = str(temporary)
    staging = temporary / ("native-readiness-unvalidated-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o755)
    acceptance = Acceptance(workspace, staging)
    error = None
    try:
        acceptance.execute()
    except BaseException as caught:
        error = caught
    report, values = acceptance.final_report(error)
    safe = finalize_public(staging, report, values, args.evidence)
    print(json.dumps({"accepted": report["accepted"] and safe, "phase": acceptance.current_phase,
                      "privacy_verified": safe, "external_publications": 0}), flush=True)
    return 0 if report["accepted"] and safe else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as failure:
        code = str(failure)
        print(code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", code) else "NATIVE_ACCEPTANCE_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
