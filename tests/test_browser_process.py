"""Owned worker trees only: real local children, no browser/profile/account traffic."""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import pytest

from max_postiz_agent import browser_process as runner


@pytest.fixture
def kernel_ownership():
    if os.name != "nt":
        state = runner.linux_runtime_status()
        if not all(state[key] for key in ("supported", "writable", "cgroup_kill")):
            pytest.skip("Linux delegated cgroup v2/cgroup.kill unavailable; no worker started")


def gone(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            from pathlib import Path
            status = Path(f"/proc/{pid}/stat")
            return status.exists() and status.read_text().split(") ", 1)[1].startswith("Z ")
        except ProcessLookupError:
            return True
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, never terminate by PID
    if not handle:
        return ctypes.get_last_error() in {87, 1168}
    try:
        return api.WaitForSingleObject(handle, 0) == 0
    finally:
        api.CloseHandle(handle)


def eventually_gone(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if gone(pid):
            return True
        time.sleep(0.02)
    return gone(pid)


DUMMY = r"""
import json,os,subprocess,sys,time,warnings
from pathlib import Path
warnings.simplefilter('ignore',ResourceWarning)
request=sys.stdin.read()
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
Path(sys.argv[1]).write_text(json.dumps({'worker':os.getpid(),'child':child.pid}),encoding='utf-8')
sys.stdout.write('Ответ ✓');sys.stdout.flush()
sys.stderr.write('Диагностика');sys.stderr.flush()
if sys.argv[2]=='sleep':time.sleep(60)
"""


def test_normal_response_is_utf8_and_also_cleans_owned_orphan(tmp_path, kernel_ownership):
    pids = tmp_path / "pids.json"
    command = [sys.executable, "-c", DUMMY, str(pids), "complete"]
    result = runner.run_browser_worker(command, input='{"текст":"точный"}', timeout=10)
    assert isinstance(result, subprocess.CompletedProcess) and result.args == command
    assert result.returncode == 0 and result.stdout == "Ответ ✓" and result.stderr == "Диагностика"
    values = json.loads(pids.read_text(encoding="utf-8"))
    assert eventually_gone(values["worker"]) and eventually_gone(values["child"])


def test_timeout_kills_own_tree_and_preserves_unrelated_process(tmp_path, kernel_ownership):
    pids = tmp_path / "pids.json"
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with pytest.raises(subprocess.TimeoutExpired) as error:
            runner.run_browser_worker([sys.executable, "-c", DUMMY, str(pids), "sleep"],
                                      input="private-input-not-for-error", timeout=2)
        values = json.loads(pids.read_text(encoding="utf-8"))
        assert eventually_gone(values["worker"]) and eventually_gone(values["child"])
        assert unrelated.poll() is None
        assert error.value.cmd == "browser-worker" and error.value.output is None and error.value.stderr is None
        assert "private-input-not-for-error" not in str(error.value)
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_timeout_includes_blocked_stdin_write_and_discards_secret_output(tmp_path, capsys, kernel_ownership):
    pids = tmp_path / "worker.pid"
    script = ("import os,sys,time;from pathlib import Path;"
              "Path(sys.argv[1]).write_text(str(os.getpid()));"
              "sys.stderr.write('private-child-output');sys.stderr.flush();time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as error:
        runner.run_browser_worker([sys.executable, "-c", script, str(pids)],
                                  input="private-input" * 200000, timeout=2)
    assert time.monotonic() - started < 6
    assert eventually_gone(int(pids.read_text()))
    assert error.value.output is None and error.value.stderr is None
    assert "private-child-output" not in str(error.value)
    assert "private-input" not in str(error.value)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


class Pipe:
    def __init__(self, name, events):
        self.name, self.events = name, events

    def close(self):
        self.events.append("close-" + self.name)


class Process:
    pid = 123456

    def __init__(self, events):
        self.events, self.returncode = events, None
        self.stdin, self.stdout, self.stderr = (Pipe(name, events) for name in ("stdin", "stdout", "stderr"))

    def communicate(self, *, input):
        self.events.append("payload")
        self.returncode = 0
        return "reply", ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.events.append("kill-own-child")
        self.returncode = -1

    def wait(self, *, timeout):
        assert 0 <= timeout <= runner._CLEANUP_SECONDS
        self.events.append("wait-own-child")
        return self.returncode


@pytest.mark.parametrize("assign_fails", [False, True])
def test_windows_payload_barrier_and_assignment_failure(monkeypatch, assign_fails):
    events, options = [], {}
    process = Process(events)

    class Job:
        def __init__(self):
            events.append("create-job")

        def assign(self, child):
            assert child is process
            events.append("assign-own-child")
            if assign_fails:
                raise runner.BrowserWorkerError("BROWSER_WORKER_JOB_ASSIGN_FAILED", winerror=5)

        def close(self):
            events.append("close-own-job")

    def popen(command, **kwargs):
        options.update(kwargs)
        events.append("create-own-child")
        return process

    monkeypatch.setattr(runner, "_WINDOWS", True)
    monkeypatch.setattr(runner, "_WindowsJob", Job)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    if assign_fails:
        with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_JOB_ASSIGN_FAILED"):
            runner.run_browser_worker(["fixture-worker"], input="private-payload", timeout=1)
        assert "payload" not in events
        assert events.index("kill-own-child") < events.index("close-stdin")
    else:
        result = runner.run_browser_worker(["fixture-worker"], input="private-payload", timeout=1)
        assert result.stdout == "reply" and result.returncode == 0
        assert events.index("assign-own-child") < events.index("payload") < events.index("close-own-job")
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert options["stdin"] == subprocess.PIPE and options["close_fds"] is True
    assert options["encoding"] == "utf-8" and options["env"]["PYTHONIOENCODING"] == "utf-8"


def test_job_creation_failure_never_starts_child_or_exposes_input(monkeypatch):
    def failed():
        raise runner.BrowserWorkerError("BROWSER_WORKER_JOB_UNAVAILABLE", winerror=5)

    def forbidden(*args, **kwargs):
        raise AssertionError("No worker may start without an owned Job Object")

    monkeypatch.setattr(runner, "_WINDOWS", True)
    monkeypatch.setattr(runner, "_WindowsJob", failed)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    with pytest.raises(runner.BrowserWorkerError) as error:
        runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=1)
    assert "private-input" not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_timeout_never_starts_process(monkeypatch, timeout):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid request must not start a worker")

    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_INPUT_INVALID"):
        runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=timeout)


def test_nonzero_worker_exit_is_a_completed_process(kernel_ownership):
    result = runner.run_browser_worker([sys.executable, "-c", "import sys;sys.stdin.read();sys.stderr.write('failed');sys.exit(7)"],
                                      input="{}", timeout=10)
    assert result.returncode == 7 and result.stdout == "" and result.stderr == "failed"


def test_interruption_after_assignment_still_closes_only_owned_tree(monkeypatch):
    events = []
    process = Process(events)

    class Job:
        def assign(self, child):
            events.append("assign")

        def close(self):
            events.append("close-own-job")

    class InterruptedThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise KeyboardInterrupt

        def join(self, *, timeout):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(runner, "_WINDOWS", True)
    monkeypatch.setattr(runner, "_WindowsJob", Job)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(threading, "Thread", InterruptedThread)
    with pytest.raises(KeyboardInterrupt):
        runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=1)
    assert "close-own-job" in events and "kill-own-child" in events and "payload" not in events


class FakeCgroup:
    """Fake kernel interface: no host cgroup paths or process IDs are written."""
    def __init__(self, monkeypatch, *, assignment_fails=False, populated=False):
        self.events, self.assignment_fails, self.populated = [], assignment_fails, populated
        self.descriptors = {101: "parent", 102: "child", 103: "cgroup.kill", 104: "cgroup.procs"}
        self.assigned = None
        monkeypatch.setattr(runner, "_linux_root", lambda: 101)
        monkeypatch.setattr(runner, "_cgroup2", lambda fd: fd == 102)
        monkeypatch.setattr(runner, "_control_read", self.read)
        monkeypatch.setattr(runner.os, "mkdir", self.mkdir)
        monkeypatch.setattr(runner.os, "open", self.open)
        monkeypatch.setattr(runner.os, "write", self.write)
        monkeypatch.setattr(runner.os, "close", lambda fd: self.events.append(("close", fd)))
        monkeypatch.setattr(runner.os, "rmdir", self.rmdir)
        for name, value in (("O_DIRECTORY", 0x10000), ("O_NOFOLLOW", 0x20000), ("O_CLOEXEC", 0x80000)):
            monkeypatch.setattr(runner.os, name, value, raising=False)

    def mkdir(self, name, mode, *, dir_fd):
        assert dir_fd == 101 and mode == 0o700
        assert name.startswith("browser-worker-") and len(name) == len("browser-worker-") + 32
        self.name = name
        self.events.append(("mkdir-private", name))

    def open(self, name, flags, *, dir_fd):
        assert flags & runner.os.O_NOFOLLOW and flags & runner.os.O_CLOEXEC
        if dir_fd == 101:
            assert name == self.name and flags & runner.os.O_DIRECTORY
            return 102
        assert dir_fd == 102 and name in {"cgroup.kill", "cgroup.procs"}
        self.events.append(("open-private", name))
        return 103 if name == "cgroup.kill" else 104

    def write(self, fd, value):
        assert fd in {103, 104}, "Never write to parent/foreign controls"
        self.events.append(("write", fd, value))
        if fd == 104:
            if self.assignment_fails:
                raise PermissionError("private-error-do-not-log")
            self.assigned = value.decode("ascii")
        else:
            assert value == b"1"
        return len(value)

    def read(self, directory, name):
        assert directory == 102
        if name == "cgroup.type":
            return "domain\n"
        if name == "cgroup.procs":
            return self.assigned + "\n" if self.assigned else ""
        assert name == "cgroup.events"
        return "populated " + ("1" if self.populated else "0") + "\nfrozen 0\n"

    def rmdir(self, name, *, dir_fd):
        assert dir_fd == 101 and name == self.name
        self.events.append(("rmdir-private", name))


@pytest.mark.parametrize("assignment_fails", [False, True])
def test_linux_private_cgroup_barrier_and_no_foreign_write(monkeypatch, assignment_fails):
    kernel = FakeCgroup(monkeypatch, assignment_fails=assignment_fails)
    process = Process(kernel.events)
    monkeypatch.setattr(runner, "_WINDOWS", False)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)

    def no_pid_kill(*args, **kwargs):
        raise AssertionError("Kernel cgroup ownership must not fall back to PID/process-group discovery")

    monkeypatch.setattr(runner.os, "kill", no_pid_kill)
    if assignment_fails:
        with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_CGROUP_ASSIGN_FAILED"):
            runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=2)
        assert "payload" not in kernel.events
        assert kernel.events.index("kill-own-child") < kernel.events.index("close-stdin")
    else:
        result = runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=2)
        assert result.stdout == "reply"
        assert kernel.events.index(("write", 104, b"123456")) < kernel.events.index("payload")
        assert kernel.events.index("payload") < kernel.events.index(("write", 103, b"1"))
    assert ("rmdir-private", kernel.name) in kernel.events
    assert not any(isinstance(event, tuple) and event[:2] == ("write", 101) for event in kernel.events)


def test_linux_without_delegation_never_starts_worker_or_sends_input(monkeypatch):
    monkeypatch.setattr(runner, "_WINDOWS", False)

    def unavailable():
        raise PermissionError("private-host-path")

    def forbidden(*args, **kwargs):
        raise AssertionError("No worker before kernel ownership")

    monkeypatch.setattr(runner, "_linux_root", unavailable)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_CGROUP_UNAVAILABLE") as error:
        runner.run_browser_worker(["fixture-worker"], input="private-payload", timeout=1)
    assert "private" not in str(error.value)


def test_linux_populated_cleanup_is_bounded_unknown_not_success(monkeypatch):
    kernel = FakeCgroup(monkeypatch, populated=True)
    process = Process(kernel.events)
    monkeypatch.setattr(runner, "_WINDOWS", False)
    monkeypatch.setattr(runner, "_CLEANUP_SECONDS", 0.03)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    started = time.monotonic()
    with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_CLEANUP_UNKNOWN"):
        runner.run_browser_worker(["fixture-worker"], input="private-payload", timeout=1)
    assert time.monotonic() - started < 1
    assert ("rmdir-private", kernel.name) not in kernel.events


def test_linux_private_uuid_collision_never_reuses_or_removes_foreign_group(monkeypatch):
    kernel = FakeCgroup(monkeypatch)

    def collision(*args, **kwargs):
        raise FileExistsError("already owned by another request")

    monkeypatch.setattr(runner.os, "mkdir", collision)
    with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_CGROUP_UNAVAILABLE"):
        runner._LinuxCgroup()
    assert kernel.events == [("close", 101)]


@pytest.mark.parametrize("member, filesystem, mounts", [
    ("0::/\n", True, ""),
    ("0::/../foreign\n", True, ""),
    ("0::/own\n", False, "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"),
    ("0::/own\n", True, "36 25 0:32 /foreign /sys/fs/cgroup rw - cgroup2 cgroup rw\n"),
])
def test_linux_root_rejects_root_traversal_wrong_filesystem_and_bind_mount(monkeypatch, member, filesystem, mounts):
    opened = []
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_proc_read", lambda name: member if name == "cgroup" else mounts)
    monkeypatch.setattr(runner, "_directory", lambda path: opened.append(path) or 101)
    monkeypatch.setattr(runner, "_cgroup2", lambda fd: filesystem)
    monkeypatch.setattr(runner.os, "close", lambda fd: None)
    with pytest.raises(OSError):
        runner._linux_root()
    assert all(".." not in path for path in opened)


@pytest.mark.parametrize("member_matches", [False, True])
def test_linux_root_requires_our_current_membership_before_any_child(monkeypatch, member_matches):
    closed, opened = [], []
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_proc_read", lambda name: "0::/system.slice/worker.service\n"
        if name == "cgroup" else "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
    monkeypatch.setattr(runner, "_directory", lambda path: opened.append(path) or 101)
    monkeypatch.setattr(runner, "_cgroup2", lambda fd: True)
    monkeypatch.setattr(runner, "_control_read", lambda fd, name: "domain\n" if name == "cgroup.type"
        else str(os.getpid() if member_matches else os.getpid() + 1) + "\n")
    monkeypatch.setattr(runner.os, "close", closed.append)
    if member_matches:
        assert runner._linux_root() == 101
        assert not closed
    else:
        with pytest.raises(OSError):
            runner._linux_root()
        assert closed == [101]
    assert opened == ["/sys/fs/cgroup/system.slice/worker.service"]


def test_linux_missing_kernel_kill_fails_before_child_creation(monkeypatch):
    kernel = FakeCgroup(monkeypatch)
    original_open = kernel.open

    def no_kernel_kill(name, flags, *, dir_fd):
        if name == "cgroup.kill":
            raise FileNotFoundError("old kernel without cgroup.kill")
        return original_open(name, flags, dir_fd=dir_fd)

    def forbidden(*args, **kwargs):
        raise AssertionError("No worker without kernel tree kill")

    monkeypatch.setattr(runner, "_WINDOWS", False)
    monkeypatch.setattr(runner.os, "open", no_kernel_kill)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)
    with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_CGROUP_UNAVAILABLE"):
        runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=1)
    assert ("rmdir-private", kernel.name) in kernel.events
    assert not any(isinstance(event, tuple) and event[0] == "write" for event in kernel.events)


def test_linux_readonly_status_has_no_paths_or_mutations(monkeypatch):
    events = []
    monkeypatch.setattr(runner, "_linux_root", lambda: 101)
    monkeypatch.setattr(runner.os, "access", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner.os, "stat", lambda *args, **kwargs: events.append("stat"))
    monkeypatch.setattr(runner.os, "close", lambda fd: events.append("close"))
    value = runner.linux_runtime_status()
    assert value == {"supported": True, "writable": True, "cgroup_kill": True, "delegation_exercised": False}
    assert events == ["stat", "close"]


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux cgroup/setsid cannot run on Windows")
@pytest.mark.parametrize("mode", ["complete", "sleep"])
def test_linux_cgroup_cleans_setsid_detached_descendant(tmp_path, kernel_ownership, mode):
    pids = tmp_path / "setsid-pids.json"
    detached = DUMMY.replace("creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)", "start_new_session=True")
    command = [sys.executable, "-c", detached, str(pids), mode]
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if mode == "sleep":
            with pytest.raises(subprocess.TimeoutExpired):
                runner.run_browser_worker(command, input="{}", timeout=2)
        else:
            assert runner.run_browser_worker(command, input="{}", timeout=10).returncode == 0
        values = json.loads(pids.read_text(encoding="utf-8"))
        assert eventually_gone(values["worker"]) and eventually_gone(values["child"])
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_stdio_thread_start_failure_is_redacted_and_still_cleans_child(monkeypatch):
    events = []
    process = Process(events)

    class Job:
        def assign(self, child):
            events.append("assign")

        def close(self):
            events.append("close-own-job")

    class UnstartedThread(threading.Thread):
        def start(self):
            raise RuntimeError("private-payload-must-not-leak")

    monkeypatch.setattr(runner, "_WINDOWS", True)
    monkeypatch.setattr(runner, "_WindowsJob", Job)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(threading, "Thread", UnstartedThread)
    with pytest.raises(runner.BrowserWorkerError, match="BROWSER_WORKER_START_FAILED") as error:
        runner.run_browser_worker(["fixture-worker"], input="private-input", timeout=1)
    assert "private-payload" not in str(error.value)
    assert "close-own-job" in events and "kill-own-child" in events and "payload" not in events
