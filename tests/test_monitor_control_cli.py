"""Управление не вызывает producer, редакционный sender или новый общий цикл."""

from types import SimpleNamespace
import threading

import pytest

from max_postiz_agent import monitor_cli


@pytest.mark.parametrize("command", ["control-once", "control-run"])
def test_control_cli_uses_only_control_lanes(tmp_path, monkeypatch, command):
    calls = []
    core = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(monitor_cli, "build_monitor", lambda root: core)
    monkeypatch.setattr(monitor_cli.MonitorConfig, "load", lambda root: object())

    class Service:
        def __init__(self, core, config):
            pass

        def receive_once(self):
            calls.append("receive")
            return {"state": "received"}

        def run_command_once(self):
            calls.append("task")
            return {"state": "idle"}

        def deliver_once(self):
            calls.append("reply")
            return {"state": "idle"}

        def produce(self):
            pytest.fail("Control command attempted periodic report production")

    monkeypatch.setattr(monitor_cli, "MonitorService", Service)
    def run(project, *, control_only=False):
        assert project is core and control_only is True
        calls.append("control_lanes")
    monkeypatch.setattr(monitor_cli, "run", run)
    assert monitor_cli.main(["--root", str(tmp_path), command]) == 0
    assert calls == (["receive", "task", "reply", "close"] if command == "control-once"
                     else ["control_lanes", "close"])


def test_control_worker_wakes_sender_and_drains_replies(tmp_path, monkeypatch):
    """A reply queued after an idle sender must not wait a 300-second poll."""
    stop = threading.Event()
    sender_idle = threading.Event()
    queued = threading.Event()
    completed = threading.Event()
    received = threading.Event()
    delivered = []
    monkeypatch.setattr(monitor_cli.MonitorConfig, "load", lambda root: SimpleNamespace(
        value={"poll_seconds": 300}))

    class Service:
        def __init__(self, core, config):
            pass

        def produce(self):
            pytest.fail("control worker attempted periodic production")

        def receive_once(self):
            assert sender_idle.wait(2)
            if not received.is_set():
                received.set()
                queued.set()
                return {"state": "received", "accepted": 1}
            return {"state": "received", "accepted": 0}

        def run_command_once(self):
            return {"state": "idle"}

        def deliver_once(self):
            if not queued.is_set():
                sender_idle.set()
                return {"state": "idle_or_blocked"}
            delivered.append("confirmed")
            if len(delivered) == 2:
                completed.set()
                stop.set()
            return {"state": "confirmed"}

    monkeypatch.setattr(monitor_cli, "MonitorService", Service)
    thread = threading.Thread(target=monitor_cli.run,
        args=(SimpleNamespace(settings=SimpleNamespace(root=tmp_path)), stop),
        kwargs={"control_only": True})
    thread.start()
    try:
        assert completed.wait(3), "sender waited for poll instead of queued reply"
        assert delivered == ["confirmed", "confirmed"]
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
