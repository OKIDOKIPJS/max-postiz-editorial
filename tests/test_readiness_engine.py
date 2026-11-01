"""Pure/mock tests: no Docker daemon, systemd, Linux runtime or browser starts."""

import importlib.util
import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[1] / "deployment/linux/readiness_engine.py"
SPEC = importlib.util.spec_from_file_location("readiness_engine", PATH)
engine_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine_module)
Engine, EngineError, GIB = engine_module.Engine, engine_module.EngineError, engine_module.GIB


def test_construction_and_plan_are_pure_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module.subprocess, "run", lambda *a, **k: pytest.fail("spawn forbidden"))
    engine = Engine(tmp_path / "new-engine", tmp_path / "evidence")
    plan = engine.plan(14 * GIB)
    assert not engine.root.exists()
    assert plan["limits"]["memory_bytes"] == 10 * GIB
    assert plan["limits"]["native_memory_bytes"] == 6 * GIB
    assert plan["limits"]["daemon_memory_bytes"] == GIB
    assert plan["cgroup_parent"] == "/" + engine.slice
    assert "--data-root=" + str(engine.data_root) in plan["daemon"]
    assert "--exec-root=" + str(engine.exec_root) in plan["daemon"]
    assert "--cgroup-parent=" + engine.cgroup_parent in plan["daemon"]
    for flag in ("--bridge=none", "--iptables=false", "--ip-forward=false", "--ip-masq=false"):
        assert flag in plan["daemon"]
    assert all("/var/run/docker.sock" not in flag for flag in plan["daemon"])


@pytest.mark.parametrize("platform,uid,environment", [
    ("win32", 0, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}),
    ("linux", 1000, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}),
    ("linux", 0, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}),
    ("linux", 0, {"GITHUB_ACTIONS": "false", "RUNNER_ENVIRONMENT": "github-hosted"}),
    ("linux", 0, {}),
])
def test_ephemeral_guard_rejects_every_ambient_or_nonroot_host(platform, uid, environment):
    with pytest.raises(EngineError, match="OWN_ENGINE_EPHEMERAL_RUNNER_REQUIRED"):
        engine_module._ephemeral_environment(platform, uid, environment)


def test_ephemeral_guard_accepts_only_explicit_hosted_linux_root():
    engine_module._ephemeral_environment("linux", 0, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"})


def test_memory_budget_reserves_two_gib_and_rejects_low_host():
    limits = engine_module._limits(7 * GIB)
    assert limits["memory_bytes"] == 5 * GIB and limits["native_memory_bytes"] == 3 * GIB
    assert limits["swap_bytes"] == 8 * GIB
    for value in (6 * GIB, -1, True, float(14 * GIB)):
        with pytest.raises(EngineError):
            engine_module._limits(value)


def test_paths_must_be_new_plain_children_of_runner_temp(tmp_path, monkeypatch):
    assert engine_module._inside_plain(tmp_path / "owned" / "proof.json", tmp_path) == (tmp_path / "owned/proof.json").resolve()
    for path in (tmp_path, tmp_path / "../outside", Path("relative")):
        with pytest.raises(EngineError):
            engine_module._inside_plain(path, tmp_path)
    original = Path.is_symlink
    linked = tmp_path / "linked"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == linked or original(self))
    with pytest.raises(EngineError):
        engine_module._inside_plain(linked / "engine", tmp_path)


def test_docker_environment_uses_fresh_private_config_without_ambient_host_or_credentials(tmp_path):
    value = engine_module._environment({"DOCKER_HOST": "tcp://other:2375", "DOCKER_CONTEXT": "other",
        "DOCKER_AUTH_CONFIG": "secret", "DOCKER_CONFIG": "/ambient", "BUILDX_BUILDER": "ambient", "BUILDKIT_HOST": "tcp://other", "SAFE": "yes"},
        config_dir=tmp_path / "private-client")
    assert value["DOCKER_CONFIG"] == str(tmp_path / "private-client")
    assert value["SAFE"] == "yes"
    assert "DOCKER_HOST" not in value and "DOCKER_CONTEXT" not in value and "DOCKER_AUTH_CONFIG" not in value
    assert "BUILDX_BUILDER" not in value and "secret" not in json.dumps(value)
    assert "BUILDKIT_HOST" not in value


def test_every_cli_call_pins_private_socket_and_config(tmp_path, monkeypatch):
    calls = []
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    engine._created = True
    monkeypatch.setattr(engine_module.subprocess, "run", lambda command, **kwargs:
        calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0, "result", ""))
    result = engine.docker("version", env={"DOCKER_HOST": "unix:///var/run/docker.sock"})
    assert result.stdout == "result"
    command, kwargs = calls[0]
    assert command[:5] == ["/usr/bin/docker", "--host", engine.socket, "--config", str(engine.config_dir)]
    assert "DOCKER_HOST" not in kwargs["env"]
    assert kwargs["timeout"] == 60 and kwargs["capture_output"] is True and kwargs["check"] is True
    engine.docker("load", input=b"image", text=False, timeout=180)
    assert calls[1][1]["input"] == b"image" and calls[1][1]["text"] is False
    for args in (("--host", "unix:///var/run/docker.sock"), ("--context=default",), ("--config=/ambient",)):
        with pytest.raises(EngineError):
            engine.docker(*args)
    with pytest.raises(EngineError):
        engine.docker("info", shell=True)  # noqa: S604 - rejection test; never executes a shell.
    assert len(calls) == 2


def mock_engine(tmp_path, monkeypatch, *, dirty=False, identity_override=None):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    calls = []
    def guard():
        engine.commonlimits = engine_module._limits(14 * GIB)
        engine._evidence_file = tmp_path / "evidence/engine.json"
    engine._guard = guard
    engine._capture_cgroup = lambda: None
    engine._kill_owned_cgroup = lambda: None
    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = ""
        if command[0] == "/usr/bin/systemctl" and "--property=ControlGroup" in command:
            output = engine.cgroup_parent
        if command[0] == "/usr/bin/docker":
            args = command[5:]
            if args[0] == "info":
                output = json.dumps({"ID": "own-engine-id", "DockerRootDir": str(engine.data_root),
                    "CgroupDriver": "cgroupfs", "Labels": [engine_module.LABEL_KEY + "=" + engine.label_value],
                    **(identity_override or {})})
            elif args[:2] == ["ps", "-aq"]:
                output = "a" * 64 if dirty and "--filter" not in args else ""
        return subprocess.CompletedProcess(command, 0, output, "")
    engine._run = run
    return engine, calls


def test_mock_lifecycle_caps_slice_before_daemon_and_preserves_host_resources(tmp_path, monkeypatch):
    engine, calls = mock_engine(tmp_path, monkeypatch)
    with engine as entered:
        assert entered.engine_id == "own-engine-id"
        assert json.loads((tmp_path / "evidence/engine.json").read_text())["initial_volumes"] == 0
    commands = [command for command, _ in calls]
    cap = next(index for index, command in enumerate(commands) if "set-property" in command)
    daemon = next(index for index, command in enumerate(commands) if "/usr/bin/dockerd" in command)
    assert cap < daemon
    assert all("prune" not in command and "volume rm" not in " ".join(command) for command in commands)
    stops = [command[-1] for command in commands if command[:2] == ["/usr/bin/systemctl", "stop"]]
    assert stops == [engine.daemon_unit, engine.anchor_unit, engine.slice]
    assert all("docker.service" != item for item in stops)
    proof = json.loads((tmp_path / "evidence/engine.json").read_text())
    assert proof["state"] == "closed" and proof["host_daemon_used"] is False
    assert engine.data_root.parent.is_dir()  # No recursive deletion/pruning.
    before = len(calls)
    engine.close()
    assert len(calls) == before


def test_nonempty_new_engine_fails_before_any_native_work_and_closes_units(tmp_path, monkeypatch):
    engine, calls = mock_engine(tmp_path, monkeypatch, dirty=True)
    with pytest.raises(EngineError, match="OWN_ENGINE_NOT_EMPTY"):
        engine.__enter__()
    assert engine._closed
    assert any(command[:2] == ["/usr/bin/systemctl", "stop"] for command, _ in calls)


def test_wrong_daemon_identity_is_never_validated_or_used_for_container_cleanup(tmp_path, monkeypatch):
    engine, calls = mock_engine(tmp_path, monkeypatch, identity_override={"DockerRootDir": "/var/lib/docker"})
    clock = iter([0, 41])
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(EngineError, match="OWN_ENGINE_START_TIMEOUT"):
        engine.__enter__()
    assert not engine._validated
    assert not any(command[5:6] == ["stop"] for command, _ in calls if command[0] == "/usr/bin/docker")


def test_cleanup_stops_only_labelled_owned_containers_under_the_owned_cgroup(tmp_path, monkeypatch):
    engine, calls = mock_engine(tmp_path, monkeypatch)
    engine.__enter__()
    original = engine._run
    ident = "f" * 64
    def run(command, **kwargs):
        args = command[5:] if command[0] == "/usr/bin/docker" else []
        if args[:2] == ["ps", "-aq"] and "--filter" in args:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, ident, "")
        if args[:1] == ["inspect"]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps([{"Config": {"Labels": {engine_module.LABEL_KEY: engine.label_value}},
                "HostConfig": {"CgroupParent": engine.cgroup_parent}}]), "")
        return original(command, **kwargs)
    engine._run = run
    engine.close()
    stops = [command[5:] for command, _ in calls if command[0] == "/usr/bin/docker" and command[5:6] == ["stop"]]
    assert stops == [["stop", "--time=10", ident]]


def test_namespace_wrapper_binds_only_inside_private_namespace_and_keeps_argv_literal(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    engine._validated = True
    original = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *args, **kwargs:
        type("SocketStat", (), {"st_mode": stat.S_IFSOCK})() if self == Path("/var/run/docker.sock")
        else original(self, *args, **kwargs))
    command = ["node", "script with spaces.cjs", "$(do-not-run); literally"]
    result = engine.namespace_command(command)
    assert result[:4] == ["/usr/bin/unshare", "--mount", "--propagation", "private"]
    assert result[-3:] == command
    assert str(engine.socket_path) not in result[7]  # Source path is argv, never shell interpolation.
    assert 'exec "$@"' in result[7] and '"$1" /var/run/docker.sock' in result[7]


def test_namespace_wrapper_never_creates_a_missing_host_socket(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    engine._validated = True
    monkeypatch.setattr(Path, "stat", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(EngineError, match="OWN_ENGINE_HOST_SOCKET_MISSING"):
        engine.namespace_command(["node", "runner.cjs"])


def test_command_errors_do_not_expose_captured_private_output(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    monkeypatch.setattr(engine_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["private"], output="secret", stderr="secret")))
    with pytest.raises(EngineError) as caught:
        engine._run(["/usr/bin/systemctl", "show", engine.slice])
    assert str(caught.value) == "OWN_ENGINE_COMMAND_FAILED"


def test_all_subprocess_timeouts_remain_bounded(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    monkeypatch.setattr(engine_module.subprocess, "run", lambda *a, **k: pytest.fail("unbounded call"))
    for timeout in (None, True, 0, -1, float("nan"), float("inf")):
        with pytest.raises(EngineError, match="OWN_ENGINE_TIMEOUT_REQUIRED"):
            engine._run(["/usr/bin/docker", "info"], timeout=timeout)


@pytest.mark.parametrize("foreign", ["label", "cgroup"])
def test_cleanup_never_stops_a_foreign_label_or_parent(tmp_path, monkeypatch, foreign):
    engine, calls = mock_engine(tmp_path, monkeypatch)
    engine.__enter__()
    original = engine._run
    ident = "f" * 64
    def run(command, **kwargs):
        args = command[5:] if command[0] == "/usr/bin/docker" else []
        if args[:2] == ["ps", "-aq"] and "--filter" in args:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, ident, "")
        if args[:1] == ["inspect"]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps([{
                "Config": {"Labels": {engine_module.LABEL_KEY: "foreign" if foreign == "label" else engine.label_value}},
                "HostConfig": {"CgroupParent": "/foreign.slice" if foreign == "cgroup" else engine.cgroup_parent}}]), "")
        return original(command, **kwargs)
    engine._run = run
    with pytest.raises(EngineError, match="OWN_ENGINE_CLEANUP_UNCONFIRMED"):
        engine.close()
    assert not any(command[5:6] == ["stop"] for command, _ in calls if command[0] == "/usr/bin/docker")
    assert any(command[:2] == ["/usr/bin/systemctl", "stop"] for command, _ in calls)


def fake_cgroup(tmp_path, monkeypatch, *, missing_kill=False, never_empty=False, populated=True):
    engine = Engine(tmp_path / "engine", tmp_path / "evidence")
    engine._cgroup_fd = 41
    state = {"populated": populated, "writes": [], "opens": []}
    def opened(name, flags, *, dir_fd):
        state["opens"].append((name, flags, dir_fd))
        assert name == "cgroup.kill" and dir_fd == 41
        if missing_kill:
            raise FileNotFoundError
        return 42
    def written(descriptor, value):
        assert descriptor == 42
        state["writes"].append(value)
        if not never_empty:
            state["populated"] = False
        return len(value)
    fake = SimpleNamespace(O_WRONLY=1, O_NOFOLLOW=0x20000, open=opened, write=written,
                           close=lambda fd: None, fstat=lambda fd: SimpleNamespace(st_nlink=1))
    monkeypatch.setattr(engine_module, "os", fake)
    engine._cgroup_read = lambda name: "populated " + ("1" if state["populated"] else "0") + "\nfrozen 0"
    return engine, state


def test_detached_buildkit_cleanup_kills_only_the_held_owned_cgroup_and_proves_empty(tmp_path, monkeypatch):
    engine, state = fake_cgroup(tmp_path, monkeypatch)
    engine._kill_owned_cgroup()
    assert state["writes"] == [b"1"]
    assert state["opens"] == [("cgroup.kill", 1 | 0x20000, 41)]
    assert not state["populated"]


def test_missing_cgroup_kill_remains_unconfirmed_without_any_fallback_pid_kill(tmp_path, monkeypatch):
    engine, state = fake_cgroup(tmp_path, monkeypatch, missing_kill=True)
    with pytest.raises(EngineError, match="OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED"):
        engine._kill_owned_cgroup()
    assert not state["writes"] and state["populated"]


def test_cgroup_kill_wait_is_bounded_and_never_repeats_the_kill(tmp_path, monkeypatch):
    engine, state = fake_cgroup(tmp_path, monkeypatch, never_empty=True)
    clock = iter([0, 11])
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(EngineError, match="OWN_ENGINE_CGROUP_CLEANUP_UNCONFIRMED"):
        engine._kill_owned_cgroup()
    assert state["writes"] == [b"1"]


def test_empty_held_cgroup_requires_no_kill(tmp_path, monkeypatch):
    engine, state = fake_cgroup(tmp_path, monkeypatch, populated=False)
    engine._kill_owned_cgroup()
    assert state["opens"] == [] and state["writes"] == []
