"""Own one browser-worker tree; never deliver stdin before kernel assignment.

Workers must read their complete stdin request before opening a profile or
starting children. No breakaway flags or process-name based cleanup are used.
https://learn.microsoft.com/windows/win32/procthread/job-objects
Linux requires a delegated, non-root cgroup v2 with cgroup.kill (Linux 5.14+).
https://docs.kernel.org/admin-guide/cgroup-v2.html
"""

import ctypes
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes


_WINDOWS = os.name == "nt"
_CLEANUP_SECONDS = 2.0


class BrowserWorkerError(OSError):
    def __init__(self, code, *, winerror=None):
        super().__init__(code)
        self.code = code
        if winerror is not None:
            self.winerror = winerror


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _WindowsJob:
    def __init__(self):
        self.handle = None
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        handle = self.api.CreateJobObjectW(None, None)
        if not handle:
            raise BrowserWorkerError("BROWSER_WORKER_JOB_UNAVAILABLE", winerror=ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.api.CloseHandle(handle)
            raise BrowserWorkerError("BROWSER_WORKER_JOB_UNAVAILABLE", winerror=error)
        self.handle = handle

    def assign(self, process):
        # Borrow Popen's owned process handle, not a reopened PID which can be reused.
        handle = getattr(process, "_handle", None)
        if handle is None or not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(handle))):
            raise BrowserWorkerError("BROWSER_WORKER_JOB_ASSIGN_FAILED", winerror=ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise BrowserWorkerError("BROWSER_WORKER_CLEANUP_UNKNOWN", winerror=ctypes.get_last_error())


def _proc_read(name):
    with open("/proc/self/" + name, encoding="ascii") as stream:
        value = stream.read(262145)
    if len(value) > 262144:
        raise OSError("proc record too large")
    return value


def _directory(path):
    """Open every component without following links, retaining the final inode."""
    if not path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
        raise OSError("invalid cgroup path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for part in filter(None, path.split("/")):
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _cgroup2(descriptor):
    # fstatfs checks the opened inode, not a possibly replaced path/mount name.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fstatfs.restype = ctypes.c_int
    value = ctypes.create_string_buffer(256)
    return (libc.fstatfs(descriptor, value) == 0
            and ctypes.c_long.from_buffer(value).value == 0x63677270)


def _control_read(directory, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        value = os.read(descriptor, 262145).decode("ascii")
        if len(value) > 262144:
            raise OSError("cgroup record too large")
        return value
    finally:
        os.close(descriptor)


def _linux_root():
    """Discover only our current cgroup; never accept an ambient configurable path."""
    if sys.platform != "linux":
        raise OSError("Linux cgroup v2 required")
    memberships = [line[3:] for line in _proc_read("cgroup").splitlines() if line.startswith("0::")]
    if (len(memberships) != 1 or memberships[0] == "/" or not memberships[0].startswith("/")
            or any(part in {"", ".", ".."} for part in memberships[0][1:].split("/"))):
        raise OSError("non-root current cgroup required")
    mounts = []
    for line in _proc_read("mountinfo").splitlines():
        fields, separator, filesystem = line.partition(" - ")
        columns = fields.split()
        kind = filesystem.split()
        if separator and kind and kind[0] == "cgroup2" and len(columns) >= 6:
            # Deliberately reject subtree-bind and escaped/ambiguous mount layouts.
            if columns[3] == "/" and "\\" not in columns[4]:
                mounts.append(columns[4])
    if len(mounts) != 1:
        raise OSError("unambiguous cgroup2 mount required")
    descriptor = _directory(mounts[0].rstrip("/") + memberships[0])
    try:
        if (not _cgroup2(descriptor) or _control_read(descriptor, "cgroup.type").strip() != "domain"
                or str(os.getpid()) not in _control_read(descriptor, "cgroup.procs").splitlines()):
            raise OSError("current domain cgroup required")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def linux_runtime_status():
    """Read-only prerequisite hints; does not create/assign/kill a cgroup or process."""
    result = {"supported": False, "writable": False, "cgroup_kill": False,
              "delegation_exercised": False}
    descriptor = None
    try:
        descriptor = _linux_root()
        result["supported"] = True
        result["writable"] = (os.access(".", os.W_OK | os.X_OK, dir_fd=descriptor,
                                              follow_symlinks=False)
                              and os.access("cgroup.procs", os.W_OK, dir_fd=descriptor,
                                            follow_symlinks=False))
        # Parent kill is never opened for writing; only our new UUID child is killed.
        os.stat("cgroup.kill", dir_fd=descriptor, follow_symlinks=False)
        result["cgroup_kill"] = True
    except (OSError, ValueError, AttributeError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


class _LinuxCgroup:
    """Kernel-owned descendants survive setsid/reparenting without PID sampling."""
    def __init__(self):
        self.parent = self.directory = self.kill_descriptor = self.procs = None
        self.name = "browser-worker-" + uuid.uuid4().hex
        self.created = False
        try:
            self.parent = _linux_root()
            os.mkdir(self.name, 0o700, dir_fd=self.parent)  # Exclusive, never reuse another tree.
            self.created = True
            self.directory = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                     | os.O_CLOEXEC, dir_fd=self.parent)
            if (not _cgroup2(self.directory) or _control_read(self.directory, "cgroup.procs").strip()
                    or _control_read(self.directory, "cgroup.type").strip() != "domain"):
                raise OSError("empty owned domain cgroup required")
            self.kill_descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                           dir_fd=self.directory)
            self.procs = os.open("cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=self.directory)
        except Exception:
            self._release(remove=True)
            raise BrowserWorkerError("BROWSER_WORKER_CGROUP_UNAVAILABLE") from None

    def assign(self, process):
        # Popen's direct child has not been polled/reaped or given any stdin yet.
        # It cannot start a browser before the complete-request stdin barrier.
        try:
            value = str(process.pid).encode("ascii")
            if os.write(self.procs, value) != len(value):
                raise OSError("incomplete cgroup assignment")
            if _control_read(self.directory, "cgroup.procs").splitlines() != [str(process.pid)]:
                raise OSError("cgroup assignment not observed")
        except Exception:
            raise BrowserWorkerError("BROWSER_WORKER_CGROUP_ASSIGN_FAILED") from None

    def _release(self, *, remove):
        for name in ("procs", "kill_descriptor", "directory"):
            descriptor = getattr(self, name)
            setattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
        try:
            if remove and self.created:
                os.rmdir(self.name, dir_fd=self.parent)
                self.created = False
        finally:
            descriptor, self.parent = self.parent, None
            if descriptor is not None:
                os.close(descriptor)

    def close(self):
        if self.parent is None:
            return
        empty = False
        try:
            # Only this fresh private child. Kernel handles concurrent forks too.
            if os.write(self.kill_descriptor, b"1") != 1:
                raise OSError("incomplete cgroup kill")
            deadline = time.monotonic() + _CLEANUP_SECONDS
            while True:
                events = dict(line.split() for line in _control_read(self.directory, "cgroup.events").splitlines())
                if events.get("populated") == "0":
                    empty = True
                    break
                if time.monotonic() >= deadline:
                    raise OSError("owned cgroup remains populated")
                time.sleep(0.01)
        except Exception:
            raise BrowserWorkerError("BROWSER_WORKER_CLEANUP_UNKNOWN") from None
        finally:
            self._release(remove=empty)


def _stop_owned(process, job, *, assigned, communication):
    """Bounded cleanup, including interruption and assignment failure before stdin."""
    okay = True
    deadline = time.monotonic() + _CLEANUP_SECONDS
    if process is not None and not assigned:
        # Kill first: EOF on an unassigned worker must not release its stdin barrier.
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            okay = False
    if job is not None:
        try:
            job.close()
        except OSError:
            okay = False
    if process is not None:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            okay = False
    if communication is not None:
        try:
            communication.join(timeout=max(0, deadline - time.monotonic()))
        except RuntimeError:
            # Thread.start() can fail before the thread exists. The child has
            # already been stopped; there is then no reader thread to drain.
            okay = okay and not communication.is_alive()
        okay = okay and not communication.is_alive()
    # A live reader thread can hold a TextIO lock. Do not turn bounded cleanup
    # into an unbounded close() if the OS did not close all inherited pipe ends.
    if process is not None and (communication is None or not communication.is_alive()):
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    okay = False
    return okay


def run_browser_worker(command, *, input: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """UTF-8 request/receipt; timeout or interruption terminates only this tree."""
    if (not isinstance(command, (list, tuple)) or not command
            or any(not isinstance(part, str) or not part for part in command)
            or not isinstance(input, str) or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
        raise BrowserWorkerError("BROWSER_WORKER_INPUT_INVALID")
    process, job, communication, assigned, failure = None, None, None, False, None
    result = {}
    started = time.monotonic()
    try:
        if _WINDOWS:
            job = _WindowsJob()
        else:
            job = _LinuxCgroup()
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict", env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WINDOWS else 0,
            start_new_session=not _WINDOWS, close_fds=True)
        if job is not None:
            job.assign(process)
            assigned = True
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("browser-worker", timeout)

        def communicate():
            try:
                result["streams"] = process.communicate(input=input)
            except BaseException:
                # Do not retain an exception containing request/response contents.
                result["failed"] = True

        # On Windows communicate() writes stdin synchronously before its timeout
        # check. A dedicated daemon thread bounds even a worker not reading stdin.
        communication = threading.Thread(target=communicate, name="owned-browser-stdio", daemon=True)
        communication.start()
        communication.join(timeout=remaining)
        if communication.is_alive():
            raise subprocess.TimeoutExpired("browser-worker", timeout)
        if result.get("failed"):
            raise BrowserWorkerError("BROWSER_WORKER_COMMUNICATION_FAILED")
    except subprocess.TimeoutExpired:
        failure = subprocess.TimeoutExpired("browser-worker", timeout)
    except (KeyboardInterrupt, SystemExit) as error:
        failure = error
    except BrowserWorkerError as error:
        failure = error
    except Exception:
        failure = BrowserWorkerError("BROWSER_WORKER_START_FAILED")
    finally:
        if not _stop_owned(process, job, assigned=assigned, communication=communication):
            failure = BrowserWorkerError("BROWSER_WORKER_CLEANUP_UNKNOWN")
    if failure is not None:
        raise failure from None
    stdout, stderr = result["streams"]
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
