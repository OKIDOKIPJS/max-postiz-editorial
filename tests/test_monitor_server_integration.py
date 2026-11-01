"""Наблюдатель не конкурирует с PG leader и не останавливает обычный preparer."""

from types import SimpleNamespace

import pytest

from max_postiz_agent import monitor_config, monitor_service
from max_postiz_agent.server_cli import prepare_with_monitor


def core(tmp_path, backend="postgresql"):
    return SimpleNamespace(settings=SimpleNamespace(root=tmp_path),
        db=SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name=backend))))


class Preparer:
    calls = 0

    def prepare_once(self):
        self.calls += 1
        return {"phase": "normal_preparation"}


def test_absent_monitor_does_not_change_preparation(tmp_path):
    preparer = Preparer()
    assert prepare_with_monitor(core(tmp_path), preparer)["phase"] == "normal_preparation"
    assert preparer.calls == 1


@pytest.mark.parametrize("failure", [OSError("private details"), ValueError("private details")])
def test_invalid_monitor_config_never_starves_independent_preparer(tmp_path, monkeypatch, caplog, failure):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/monitor.json").write_text("invalid")
    def broken(root):
        raise failure
    monkeypatch.setattr(monitor_config.MonitorConfig, "load", broken)
    preparer = Preparer()
    assert prepare_with_monitor(core(tmp_path), preparer)["phase"] == "normal_preparation"
    assert preparer.calls == 1 and "private details" not in caplog.text


@pytest.mark.parametrize("state,expected_calls", [("idle", 1), ("disabled", 1), ("queued", 0), ("unknown", 0)])
def test_existing_leader_consumes_one_preparation_path_only(tmp_path, monkeypatch, state, expected_calls):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/monitor.json").write_text("{}")
    monkeypatch.setattr(monitor_config.MonitorConfig, "load", lambda root: object())
    received = []
    class Service:
        def __init__(self, project, config):
            pass

        def run_command_once(self, *, worker_owned=False):
            received.append(worker_owned)
            return {"state": state}
    monkeypatch.setattr(monitor_service, "MonitorService", Service)
    preparer = Preparer()
    prepare_with_monitor(core(tmp_path), preparer)
    assert received == [True] and preparer.calls == expected_calls


def test_standalone_pg_observer_never_claims_preparation_job(tmp_path, monkeypatch):
    config = SimpleNamespace(command_options=lambda: {"enabled": True})
    service = monitor_service.MonitorService(core(tmp_path), config, store=object())
    monkeypatch.setattr(service, "_guard", lambda: None)
    monkeypatch.setattr(service, "gateway", lambda: pytest.fail("Observer tried to claim PG job"))
    assert service.run_command_once() == {"state": "waiting_worker"}
