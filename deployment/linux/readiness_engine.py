"""Disposable GitHub-hosted Docker daemon; never uses the host Docker context.

Construction/plan() are pure. Only entering the context on an explicitly guarded
GitHub-hosted Linux root runner starts the owned units. Runtime evidence contains
identifiers and resource limits, never environment values or container output.
"""

from __future__ import annotations

import json
import ctypes
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
import uuid


GIB = 1024 ** 3
LABEL_KEY = "editorial.readiness.owner"
BUNDLE_CONTRACT = "owned-docker-static-bundle-v1"
BUNDLE_VERSION = "29.9.2"
BUNDLE_SHA256 = "9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb"
BUNDLE_BYTES = 86374273
BUNDLE_FILES = frozenset({"containerd", "runc", "containerd-shim-runc-v2", "docker", "docker-proxy", "ctr", "dockerd", "docker-init"})
BUNDLE_BINARY_SHA256 = {
    "containerd": "5e970f05f9feef7b89e6f38965ea1e91224644864e60bd6f5f3242d84e094228",
    "runc": "6043541aca928890ab8ad0630cafd2b272c3880779d8ada1b3fe81bc22e80d20",
    "containerd-shim-runc-v2": "7a0ed4065fc701913aabb26b73b7dd07d67b51b1cfe7721bd287161b3b26e865",
    "docker": "6c71cfe71faff949a602946a4df4799b563232e79d0b8b6fd343d0ef081c9621",
    "docker-proxy": "e29981721256f9bde91cdffd23e1b5c6a50b9949e17a3aaa027d8112d0d97717",
    "ctr": "2ac4aa817c12480c3d53f639753540ffc3fc6fca8eef248d56f920aea07e4b33",
    "dockerd": "3a6132d4d99c1437e3422599f802207a3def9ba4de00ec1004a8e40d2740b9b0",
    "docker-init": "5ccb076690ac3d060511c63d02194bd5cefaba8dbf225fd60b26283eb1055e4b",
}


class EngineError(RuntimeError):
    """A stable code, without command arguments/output or environment values."""

    def __init__(self, code, *, stage=None, returncode=None):
        super().__init__(code)
        self.stage = stage
        self.returncode = returncode


def _inside_plain(path: Path, parent: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or not parent.is_absolute():
        raise EngineError("OWN_ENGINE_PATH_INVALID")
    for current in (path, *path.parents):
        if current.is_symlink():
            raise EngineError("OWN_ENGINE_PATH_INVALID")
    resolved, base = path.resolve(), parent.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise EngineError("OWN_ENGINE_PATH_INVALID")
    return resolved


def _environment(environment=None, *, config_dir: Path, binary_root: Path | None = None) -> dict[str, str]:
    # DOCKER_HOST/CONTEXT, auth helpers and inherited Buildx context cannot select
    # an ambient daemon. A new empty config directory is always authoritative.
    value = dict(os.environ if environment is None else environment)
    value = {key: item for key, item in value.items()
             if not key.startswith("DOCKER_") and key not in {"BUILDX_CONFIG", "BUILDX_BUILDER", "BUILDKIT_HOST"}}
    value.update(DOCKER_CONFIG=str(config_dir), DOCKER_CLI_HINTS="false",
                 BUILDX_CONFIG=str(config_dir / "buildx"))
    if binary_root is not None:
        value["PATH"] = str(binary_root) + ":/usr/bin:/bin"
    return value


def _bundle_stat(value, *, directory=False, executable=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o6022
            or not directory and value.st_nlink != 1
            or executable and not value.st_mode & 0o111):
        raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")


def _bundle_bytes(path: Path, *, maximum: int, executable=False):
    before = path.lstat()
    _bundle_stat(before, executable=executable)
    if before.st_size <= 0 or before.st_size > maximum:
        raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        _bundle_stat(opened, executable=executable)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(1024 * 1024, maximum + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
        after = os.fstat(descriptor)
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != opened.st_size:
            raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_binary_root(root: Path, temporary: Path) -> Path:
    try:
        root = _inside_plain(root, temporary)
        for current in (root, *root.parents):
            if current == temporary.resolve():
                break
            _bundle_stat(current.lstat(), directory=True)
        if {path.name for path in root.iterdir()} != BUNDLE_FILES | {"bundle.json"}:
            raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        marker = json.loads(_bundle_bytes(root / "bundle.json", maximum=16384), object_pairs_hook=pairs)
        if (not isinstance(marker, dict) or set(marker) != {"contract", "version", "archive_sha256", "archive_bytes", "binaries"}
                or marker["contract"] != BUNDLE_CONTRACT or marker["version"] != BUNDLE_VERSION
                or marker["archive_sha256"] != BUNDLE_SHA256 or type(marker["archive_bytes"]) is not int
                or marker["archive_bytes"] != BUNDLE_BYTES or not isinstance(marker["binaries"], dict)
                or marker["binaries"] != BUNDLE_BINARY_SHA256):
            raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
        for name, expected in marker["binaries"].items():
            if (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)
                    or hashlib.sha256(_bundle_bytes(root / name, maximum=150 * 1024 * 1024, executable=True)).hexdigest() != expected):
                raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID")
        return root
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise EngineError("OWN_ENGINE_BINARY_BUNDLE_INVALID") from None


def _limits(available: int) -> dict:
    if type(available) is not int or available < 7 * GIB:
        raise EngineError("OWN_ENGINE_MEMORY_INSUFFICIENT")
    # At least 2 GiB remain outside this entire slice; the daemon has its own cap.
    total = min(10 * GIB, available - 2 * GIB)
    native = min(6 * GIB, total - 2 * GIB)
    if native < 3 * GIB:
        raise EngineError("OWN_ENGINE_MEMORY_INSUFFICIENT")
    return {"memory_bytes": total, "swap_bytes": 8 * GIB, "cpus": 4,
            "daemon_memory_bytes": GIB, "native_memory_bytes": native,
            "native_swap_bytes": 4 * GIB, "host_reserve_bytes": 2 * GIB}


def _ephemeral_environment(platform: str, uid: int, environment: dict):
    if (platform != "linux" or uid != 0 or environment.get("GITHUB_ACTIONS") != "true"
            or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise EngineError("OWN_ENGINE_EPHEMERAL_RUNNER_REQUIRED")


def _available_memory() -> int:
    try:
        rows = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        fields = rows["MemAvailable"].split()
        if len(fields) != 2 or fields[1] != "kB":
            raise ValueError
        return int(fields[0]) * 1024
    except (OSError, ValueError, KeyError):
        raise EngineError("OWN_ENGINE_MEMORY_UNAVAILABLE") from None


class Engine:
    def __init__(self, root: Path, evidence: Path, *, binary_root: Path | None = None):
        self.root = Path(root)
        self.evidence = Path(evidence)
        self.binary_root = Path(binary_root) if binary_root is not None else None
        self.label_value = uuid.uuid4().hex
        self.slice = "editorial_readiness_" + self.label_value + ".slice"
        self.daemon_unit = "editorial_readiness_daemon_" + self.label_value + ".service"
        self.anchor_unit = "editorial_readiness_anchor_" + self.label_value + ".service"
        self.socket_path = self.root / "docker.sock"
        self.socket = "unix://" + str(self.socket_path)
        self.config_dir = self.root / "client"
        self.data_root = self.root / "data"
        self.exec_root = self.root / "exec"
        self.cgroup_parent = "/" + self.slice
        self.engine_id = ""
        self.commonlimits = {}
        self._created = False
        self._anchor_started = False
        self._daemon_started = False
        self._closed = False
        self._validated = False
        self._cgroup_fd = None
        self._cgroup_inode = None
        self.observed_cgroup_limits = {}
        self._stage = "NOT_STARTED"
        self._transient_failure = None
        self.last_failure = None

    @property
    def environment(self):
        return _environment(config_dir=self.config_dir, binary_root=self.binary_root)

    @property
    def docker_binary(self):
        return self.binary_root / "docker" if self.binary_root is not None else PurePosixPath("/usr/bin/docker")

    @property
    def dockerd_binary(self):
        return self.binary_root / "dockerd" if self.binary_root is not None else PurePosixPath("/usr/bin/dockerd")

    @property
    def common_limits(self):
        return dict(self.commonlimits)

    def plan(self, available_bytes: int) -> dict:
        """Pure inspection, including on Windows; never creates paths/processes."""
        limits = _limits(available_bytes)
        daemon = [str(self.dockerd_binary), "--host=" + self.socket,
                  "--data-root=" + str(self.data_root), "--exec-root=" + str(self.exec_root),
                  "--pidfile=" + str(self.root / "dockerd.pid"),
                  "--config-file=" + str(self.root / "daemon.json"),
                  "--iptables=false", "--ip6tables=false", "--ip-forward=false", "--ip-masq=false",
                  "--bridge=none", "--userland-proxy=false", "--live-restore=false",
                  "--exec-opt=native.cgroupdriver=cgroupfs",
                  "--cgroup-parent=" + self.cgroup_parent,
                  "--containerd-namespace=readiness_" + self.label_value,
                  "--containerd-plugins-namespace=readiness_plugins_" + self.label_value,
                  "--label=" + LABEL_KEY + "=" + self.label_value]
        return {"socket": self.socket, "slice": self.slice, "daemon_unit": self.daemon_unit,
                "anchor_unit": self.anchor_unit, "cgroup_parent": self.cgroup_parent,
                "limits": limits, "daemon": daemon}

    def _guard(self):
        _ephemeral_environment(sys.platform, os.geteuid() if hasattr(os, "geteuid") else -1, os.environ)
        temporary = Path(os.environ.get("RUNNER_TEMP", ""))
        if not temporary.is_absolute() or not temporary.is_dir():
            raise EngineError("OWN_ENGINE_PATH_INVALID")
        self.root = _inside_plain(self.root, temporary)
        evidence_file = self.evidence if self.evidence.suffix == ".json" else self.evidence / "engine.json"
        self._evidence_file = _inside_plain(evidence_file, temporary)
        if self.root.exists() or self._evidence_file.exists():
            raise EngineError("OWN_ENGINE_PATH_ALREADY_EXISTS")
        if len(os.fsencode(self.socket_path)) > 100:
            raise EngineError("OWN_ENGINE_SOCKET_PATH_TOO_LONG")
        if self.binary_root is not None:
            self.binary_root = _validate_binary_root(self.binary_root, temporary)
        executables = ("systemctl", "systemd-run", "unshare", "mount", "sleep")
        if self.binary_root is None:
            executables += ("docker", "dockerd")
        for executable in executables:
            path = Path("/usr/bin") / executable
            if not path.is_file() or not os.access(path, os.X_OK):
                raise EngineError("OWN_ENGINE_DEPENDENCY_MISSING")
        self.commonlimits = _limits(_available_memory())

    def _failure(self, error, *, persist):
        def output(value):
            if isinstance(value, bytes):
                return value
            return value.encode("utf-8", errors="replace") if isinstance(value, str) else b""
        stdout, stderr = output(getattr(error, "stdout", None)), output(getattr(error, "stderr", None))
        returncode = getattr(error, "returncode", None)
        failure = {"contract": "github-owned-engine-failure-v1", "stage": self._stage,
                   "error_kind": "timeout" if isinstance(error, subprocess.TimeoutExpired) else
                   "exit" if isinstance(error, subprocess.CalledProcessError) else "os_error",
                   "returncode": returncode if type(returncode) is int else None,
                   "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                   "stdout_tail": stdout[-4096:].decode("utf-8", errors="replace"),
                   "stderr_tail": stderr[-4096:].decode("utf-8", errors="replace")}
        self._transient_failure = failure
        if persist:
            self._persist_failure(failure)
        return failure

    def _persist_failure(self, failure):
        # First fatal failure survives cleanup errors. Raw output is private;
        # the outer collector redacts it before its separate public privacy scan.
        if self.last_failure is None:
            self.last_failure = failure
        if not self._created or not hasattr(self, "_evidence_file"):
            return
        try:
            target = self._evidence_file.parent / "engine-failure.json"
            _inside_plain(target, Path(os.environ["RUNNER_TEMP"]))
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(json.dumps(self.last_failure, sort_keys=True).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, KeyError):
            pass  # Diagnostic I/O must not hide the command's stable failure.

    def _run(self, command, *, record_failure=True, **kwargs):
        options = {"check": True, "capture_output": True, "text": True, "timeout": 60}
        options.update(kwargs)
        if (not isinstance(options["timeout"], (int, float)) or isinstance(options["timeout"], bool)
                or not math.isfinite(options["timeout"]) or options["timeout"] <= 0):
            raise EngineError("OWN_ENGINE_TIMEOUT_REQUIRED")
        if ("stdout" in kwargs or "stderr" in kwargs) and "capture_output" not in kwargs:
            options["capture_output"] = False
        try:
            return subprocess.run(command, **options)
        except (OSError, subprocess.SubprocessError) as error:
            failure = self._failure(error, persist=record_failure)
            raise EngineError("OWN_ENGINE_COMMAND_FAILED", stage=self._stage,
                              returncode=failure["returncode"]) from None

    def _assert_unused_units(self):
        expected = {self.slice, self.daemon_unit, self.anchor_unit}
        environment = dict(self.environment, LC_ALL="C", SYSTEMD_COLORS="0", SYSTEMD_URLIFY="0")
        # No patterns: systemd 262 returns ENOENT for a no-match filtered list.
        # A failed full inventory is never interpreted as absence.
        for verb, stage in (("list-units", "UNIT_LIST_QUERY"), ("list-unit-files", "UNIT_FILE_LIST_QUERY")):
            self._stage = stage
            result = self._run(["/usr/bin/systemctl", verb, "--all", "--full", "--plain", "--no-legend", "--no-pager"],
                               env=environment)
            if result.returncode != 0:
                self._failure(subprocess.CalledProcessError(result.returncode, "unit-inventory",
                              output=result.stdout, stderr=result.stderr), persist=True)
                raise EngineError("OWN_ENGINE_COMMAND_FAILED", stage=stage, returncode=result.returncode)
            if not isinstance(result.stdout, str):
                raise EngineError("OWN_ENGINE_UNIT_INVENTORY_INVALID")
            names = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
            if names & expected:
                raise EngineError("OWN_ENGINE_UNIT_ALREADY_EXISTS")

    def docker(self, *args, **kwargs):
        if not self._created or self._closed:
            raise EngineError("OWN_ENGINE_NOT_RUNNING")
        if any(not isinstance(arg, str) or "\0" in arg or arg in {"-H", "--host", "--context", "--config"}
               or arg.startswith(("--host=", "--context=", "--config=", "-H")) for arg in args):
            raise EngineError("OWN_ENGINE_DOCKER_OVERRIDE_FORBIDDEN")
        if kwargs.get("shell") or "executable" in kwargs:
            raise EngineError("OWN_ENGINE_DOCKER_OVERRIDE_FORBIDDEN")
        kwargs.pop("shell", None)
        supplied_environment = kwargs.pop("env", None)
        environment = _environment(supplied_environment, config_dir=self.config_dir, binary_root=self.binary_root)
        return self._run([str(self.docker_binary), "--host", self.socket, "--config", str(self.config_dir), *args],
                         env=environment, **kwargs)

    def _info(self, *, record_failure=True):
        try:
            value = json.loads(self.docker("info", "--format", "{{json .}}", timeout=5,
                                           record_failure=record_failure).stdout)
            labels = value.get("Labels", [])
            if (not isinstance(value, dict) or value.get("DockerRootDir") != str(self.data_root)
                    or not isinstance(labels, list) or labels.count(LABEL_KEY + "=" + self.label_value) != 1
                    or value.get("CgroupDriver") != "cgroupfs"
                    or not isinstance(value.get("ID"), str) or not value["ID"]
                    or self.engine_id and value["ID"] != self.engine_id):
                raise ValueError
            return value
        except (ValueError, TypeError, AttributeError):
            raise EngineError("OWN_ENGINE_IDENTITY_MISMATCH") from None

    def _cgroup_read(self, name):
        if self._cgroup_fd is None or name not in {"cgroup.events", "memory.max", "memory.swap.max", "cpu.max"}:
            raise EngineError("OWN_ENGINE_CGROUP_INVALID")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._cgroup_fd)
        try:
            value = os.read(descriptor, 4096)
            if len(value) >= 4096:
                raise EngineError("OWN_ENGINE_CGROUP_INVALID")
            return value.decode("ascii").strip()
        finally:
            os.close(descriptor)

    def _capture_cgroup(self):
        base = Path("/sys/fs/cgroup")
        if (base.resolve() != base or not re.fullmatch(r"editorial_readiness_[a-f0-9]{32}\.slice", self.slice)
                or self.cgroup_parent != "/" + self.slice):
            raise EngineError("OWN_ENGINE_CGROUP_INVALID")
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._cgroup_fd = os.open(self.slice, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        # Linux fstatfs writes its platform struct into a generously sized buffer;
        # only its first native-long field (filesystem magic) is inspected.
        buffer = ctypes.create_string_buffer(4096)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.fstatfs(self._cgroup_fd, ctypes.byref(buffer)) != 0 or ctypes.c_long.from_buffer(buffer).value != 0x63677270:
            raise EngineError("OWN_ENGINE_CGROUP_V2_REQUIRED")
        self._cgroup_inode = os.fstat(self._cgroup_fd).st_ino
        measured = {name: self._cgroup_read(name) for name in ("memory.max", "memory.swap.max", "cpu.max")}
        try:
            quota, period = map(int, measured["cpu.max"].split())
            if (int(measured["memory.max"]) != self.commonlimits["memory_bytes"]
                    or int(measured["memory.swap.max"]) != self.commonlimits["swap_bytes"]
                    or period <= 0 or quota != period * self.commonlimits["cpus"]):
                raise ValueError
        except (ValueError, KeyError):
            raise EngineError("OWN_ENGINE_CGROUP_LIMIT_MISMATCH") from None
        self.observed_cgroup_limits = measured

    def _kill_owned_cgroup(self):
        if self._cgroup_fd is None:
            raise EngineError("OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED")
        try:
            # A held unlinked cgroup directory cannot have surviving processes.
            if os.fstat(self._cgroup_fd).st_nlink == 0:
                return
            def populated():
                rows = [line.split() for line in self._cgroup_read("cgroup.events").splitlines()]
                values = [row[1] for row in rows if len(row) == 2 and row[0] == "populated"]
                if len(values) != 1 or values[0] not in {"0", "1"}:
                    raise EngineError("OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED")
                return values[0] == "1"
            if populated():
                descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=self._cgroup_fd)
                try:
                    if os.write(descriptor, b"1") != 1:
                        raise EngineError("OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED")
                finally:
                    os.close(descriptor)
            deadline = time.monotonic() + 10
            while populated():
                if time.monotonic() >= deadline:
                    raise EngineError("OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED")
                time.sleep(0.05)
        except (OSError, ValueError, UnicodeError):
            raise EngineError("OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED") from None

    def _record(self, state, **fields):
        if not self._created:
            return
        target = self._evidence_file
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _inside_plain(target, Path(os.environ["RUNNER_TEMP"]))
        raw = json.dumps({"contract": "github-owned-docker-v1", "state": state,
                          "engine_id": self.engine_id, "label": self.label_value,
                          "root": str(self.root), "socket": self.socket, "cgroup_parent": self.cgroup_parent,
                          "limits": self.commonlimits, "observed_cgroup_limits": self.observed_cgroup_limits,
                          "cgroup_inode": self._cgroup_inode, **fields}, sort_keys=True).encode()
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def __enter__(self):
        self._stage = "GUARD"
        self._guard()
        self.root.mkdir(mode=0o700)
        self._created = True
        try:
            self.config_dir.mkdir(mode=0o700)
            (self.root / "daemon.json").write_text("{}\n", encoding="ascii")
            os.chmod(self.root / "daemon.json", 0o600)
            self._assert_unused_units()
            # A harmless owned child creates the implicit slice; cap it before
            # any daemon/container starts. No existing host slice is modified.
            self._anchor_started = True
            self._stage = "ANCHOR_START"
            self._run(["/usr/bin/systemd-run", "--unit=" + self.anchor_unit, "--slice=" + self.slice,
                       "--service-type=exec", "--collect", "--property=MemoryMax=16777216",
                       "--property=KillMode=control-group", "/usr/bin/sleep", "infinity"])
            self._stage = "SLICE_LIMITS"
            self._run(["/usr/bin/systemctl", "set-property", "--runtime", self.slice,
                       "MemoryMax=" + str(self.commonlimits["memory_bytes"]),
                       "MemorySwapMax=" + str(self.commonlimits["swap_bytes"]), "CPUQuota=400%"])
            self._stage = "SLICE_IDENTITY"
            group = self._run(["/usr/bin/systemctl", "show", "--property=ControlGroup", "--value", self.slice]).stdout.strip()
            if group != self.cgroup_parent:
                raise EngineError("OWN_ENGINE_CGROUP_MISMATCH")
            self._stage = "CGROUP_CAPTURE"
            self._capture_cgroup()
            self._daemon_started = True
            self._stage = "DAEMON_START"
            daemon_environment = ["--setenv=PATH=" + self.environment["PATH"]] if self.binary_root is not None else []
            self._run(["/usr/bin/systemd-run", "--unit=" + self.daemon_unit, "--slice=" + self.slice,
                       "--service-type=exec", "--collect", "--property=Delegate=yes",
                       "--property=MemoryMax=" + str(GIB), "--property=MemorySwapMax=0",
                       "--property=KillMode=control-group", "--property=TimeoutStopSec=20",
                       *daemon_environment,
                       *self.plan(self.commonlimits["memory_bytes"] + 2 * GIB)["daemon"]])
            deadline = time.monotonic() + 40
            self._stage = "DAEMON_READY"
            while True:
                try:
                    value = self._info(record_failure=False)
                    break
                except EngineError:
                    if time.monotonic() >= deadline:
                        if self._transient_failure is not None:
                            self._persist_failure(self._transient_failure)
                        raise EngineError("OWN_ENGINE_START_TIMEOUT", stage=self._stage) from None
                    time.sleep(0.25)
            self.engine_id = value["ID"]
            self._stage = "EMPTY_ENGINE_CHECK"
            containers = self.docker("ps", "-aq").stdout.strip()
            volumes = self.docker("volume", "ls", "-q").stdout.strip()
            if containers or volumes:
                raise EngineError("OWN_ENGINE_NOT_EMPTY")
            self._validated = True
            self._record("ready", initial_containers=0, initial_volumes=0)
            return self
        except BaseException:
            self.close()
            raise

    def namespace_command(self, command: list[str]) -> list[str]:
        if (not self._validated or self._closed or not isinstance(command, list) or not command
                or any(not isinstance(value, str) or "\0" in value for value in command)):
            raise EngineError("OWN_ENGINE_NAMESPACE_INVALID")
        # Do not create/replace a host path when there is no existing socket.
        try:
            if not stat.S_ISSOCK(Path("/var/run/docker.sock").stat().st_mode):
                raise OSError
        except OSError:
            raise EngineError("OWN_ENGINE_HOST_SOCKET_MISSING") from None
        script = 'test -S "$1"; test -S /var/run/docker.sock; /usr/bin/mount --bind -- "$1" /var/run/docker.sock; shift; exec "$@"'
        return ["/usr/bin/unshare", "--mount", "--propagation", "private", "/bin/sh", "-eu", "-c", script,
                "readiness-private-docker", str(self.socket_path), *command]

    def close(self):
        if self._closed or not self._created:
            return
        errors = []
        if self._validated:
            try:
                self._stage = "CLEANUP_CONTAINERS"
                self._info()
                ids = self.docker("ps", "-aq", "--filter", "label=" + LABEL_KEY + "=" + self.label_value).stdout.split()
                if any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in ids):
                    raise EngineError("OWN_ENGINE_CONTAINER_ID_INVALID")
                for ident in ids:
                    inspected = json.loads(self.docker("inspect", ident).stdout)
                    if (not isinstance(inspected, list) or len(inspected) != 1
                            or inspected[0].get("Config", {}).get("Labels", {}).get(LABEL_KEY) != self.label_value
                            or inspected[0].get("HostConfig", {}).get("CgroupParent") != self.cgroup_parent):
                        raise EngineError("OWN_ENGINE_CONTAINER_IDENTITY_MISMATCH")
                    self.docker("stop", "--time=10", ident, timeout=20)
            except (EngineError, ValueError, TypeError, AttributeError):
                errors.append("owned_container_cleanup_unconfirmed")
        for started, unit in ((self._daemon_started, self.daemon_unit), (self._anchor_started, self.anchor_unit)):
            if started:
                try:
                    self._stage = "CLEANUP_DAEMON_STOP" if unit == self.daemon_unit else "CLEANUP_ANCHOR_STOP"
                    self._run(["/usr/bin/systemctl", "stop", unit], timeout=30)
                except EngineError:
                    errors.append("owned_unit_stop_unconfirmed")
        cgroup_empty = False
        if self._daemon_started:
            try:
                self._stage = "CLEANUP_CGROUP"
                self._kill_owned_cgroup()
                cgroup_empty = True
            except EngineError:
                errors.append("owned_cgroup_cleanup_unconfirmed")
        if self._cgroup_fd is not None:
            os.close(self._cgroup_fd)
            self._cgroup_fd = None
        if self._anchor_started:
            try:
                self._stage = "CLEANUP_SLICE_STOP"
                self._run(["/usr/bin/systemctl", "stop", self.slice], timeout=30)
                self._stage = "CLEANUP_SLICE_REVERT"
                self._run(["/usr/bin/systemctl", "revert", self.slice])
            except EngineError:
                errors.append("owned_slice_stop_unconfirmed")
        self._closed = True
        self._record("closed" if not errors else "cleanup_unconfirmed", cleanup_errors=errors,
                     images_pruned=False, volumes_pruned=False, host_daemon_used=False, cgroup_empty=cgroup_empty)
        if errors:
            raise EngineError("OWN_ENGINE_CLEANUP_UNCONFIRMED")

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
