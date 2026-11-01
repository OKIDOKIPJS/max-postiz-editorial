"""Opaque runtime fencing and one authority lifetime, without browser or network."""

import io
import json
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.pikabu_browser import PikabuBrowser
import max_postiz_agent.pikabu_browser_worker as worker
from test_hub import approve, hub as hub, variants


@pytest.mark.parametrize("action", ["publish", "prepare"])
def test_write_without_parent_runtime_generation_never_starts_process(tmp_path, monkeypatch, action):
    adapter = PikabuBrowser(tmp_path, {"owner": "fixture_owner"})
    def forbidden(*args, **kwargs):
        pytest.fail("Missing runtime generation must not spawn a child")
    monkeypatch.setattr("max_postiz_agent.pikabu_browser.run_browser_worker", forbidden)
    with pytest.raises(AgentError, match="PIKABU_RUNTIME_RESTART_REQUIRED"):
        adapter._call(action, "fixture_owner")


@pytest.mark.parametrize("action", ["publish", "prepare", "inspect", "reconcile"])
def test_generation_is_private_ipc_only_for_write_actions(tmp_path, monkeypatch, action):
    adapter = PikabuBrowser(tmp_path, {"owner": "fixture_owner"})
    adapter.runtime_generation = "a" * 64
    sent = []
    def child(command, *, input, timeout):
        assert command[-2:] == ["--root", str(tmp_path.resolve())]
        assert command[1:3] == ["-m", "max_postiz_agent.pikabu_browser_worker"]
        assert timeout == 240
        sent.append(json.loads(input))
        return SimpleNamespace(stdout='{"state":"unknown"}')
    monkeypatch.setattr("max_postiz_agent.pikabu_browser.run_browser_worker", child)
    result = adapter._call(action, "fixture_owner")
    assert ("runtime_generation" in sent[0]) is (action in {"publish", "prepare"})
    if action in {"publish", "prepare"}:
        assert sent[0]["runtime_generation"] == adapter.runtime_generation
    assert adapter.runtime_generation not in json.dumps(result)


@pytest.mark.parametrize("generation", [None, "b" * 64])
def test_worker_runtime_mismatch_rejects_before_authorization_or_browser(
        tmp_path, monkeypatch, capsys, generation):
    options, counts = {"owner": "fixture_owner"}, {"cores": 0, "closed": 0}
    @contextmanager
    def core(*args):
        counts["cores"] += 1
        try:
            yield SimpleNamespace(route_runtime_generation=lambda: "a" * 64)
        finally:
            counts["closed"] += 1
    def forbidden(*args, **kwargs):
        pytest.fail("Mismatch must be checked before SQL authorization or browser")
    payload = {"action": "publish", "destination": "fixture_owner", "request_id": "operation",
               "snapshot": {}, "options_sha256": fingerprint(options)}
    if generation is not None:
        payload["runtime_generation"] = generation
    monkeypatch.setattr(worker, "_authority_runtime", core)
    monkeypatch.setattr(worker, "target_options", lambda *args: options)
    monkeypatch.setattr(worker, "authorize", forbidden)
    monkeypatch.setattr(worker, "session", forbidden)
    monkeypatch.setattr("max_postiz_agent.cli.load_env", lambda *args: None)
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert worker.main(["--root", str(tmp_path)]) == 1
    printed = capsys.readouterr().out
    assert json.loads(printed)["code"] == "PIKABU_RUNTIME_RESTART_REQUIRED"
    assert "a" * 64 not in printed and "b" * 64 not in printed
    assert counts == {"cores": int(generation is not None), "closed": int(generation is not None)}


def test_worker_reuses_exact_one_authority_for_all_callbacks_and_closes_it_once(tmp_path, monkeypatch, capsys):
    options, counts, authorities, lease_owners = {"owner": "fixture_owner"}, [], [], []
    frozen = SimpleNamespace(route_runtime_generation=lambda: "a" * 64)
    @contextmanager
    def core(*args):
        counts.append("authority_open")
        try:
            yield frozen
        finally:
            counts.append("authority_closed")
    @contextmanager
    def browser(*args):
        counts.append("browser_open")
        try:
            yield object()
        finally:
            counts.append("browser_closed")
    def authorize(*args, **kwargs):
        authorities.append(kwargs["authority_core"])
        lease_owners.append(kwargs.get("expected_lease_owner"))
        return "original-owner"
    def publish(page, root, opts, payload, authorize):
        authorize()
        authorize()
        return {"state": "unknown", "verified": False}
    payload = {"action": "publish", "destination": "fixture_owner", "request_id": "operation",
        "snapshot": {}, "options_sha256": fingerprint(options), "runtime_generation": "a" * 64}
    monkeypatch.setattr(worker, "_authority_runtime", core)
    monkeypatch.setattr(worker, "target_options", lambda *args: options)
    monkeypatch.setattr(worker, "authorize", authorize)
    monkeypatch.setattr(worker, "session", browser)
    monkeypatch.setattr(worker, "run_publish", publish)
    monkeypatch.setattr("max_postiz_agent.cli.load_env", lambda *args: None)
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert worker.main(["--root", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["verified"] is False and "a" * 64 not in printed
    assert len(authorities) == 3 and all(value is frozen for value in authorities)
    assert lease_owners == [None, "original-owner", "original-owner"]
    assert counts == ["authority_open", "browser_open", "browser_closed", "authority_closed"]


@pytest.mark.parametrize("changed", ["app", "env"])
def test_persistent_authority_does_not_rebase_mid_dispatch_runtime_changes(hub, monkeypatch, changed):
    import os
    from max_postiz_agent.db import now
    from max_postiz_agent.hub_models import Dispatch
    options = {"owner": "fixture_owner", "dom_contract_verified": True}
    config = hub.publishing
    target = next(item for item in config["targets"] if item["id"] == "pikabu")
    target.update(route="pikabu_browser", destination="fixture_owner", options=options)
    (hub.settings.root / "config/publishing.json").write_text(json.dumps(config), encoding="utf-8")
    hub.activate()
    hub.pause_target("pikabu", False)
    ident = approve(hub, variants(hub)["pikabu"])
    with hub.db.session.begin() as session:
        row = session.get(Dispatch, ident)
        row.state, row.owner, row.lease_until = "sending", "original-owner", now() + 120
        snapshot = row.snapshot
    payload = {"destination": "fixture_owner", "request_id": ident, "snapshot": snapshot}
    monkeypatch.delenv("PIKA_TEST_NO_AUTOLOAD", raising=False)
    with worker._authority_runtime(hub.settings.root) as frozen:
        assert worker.authorize(hub.settings.root, payload, options, authority_core=frozen) == "original-owner"
        if changed == "app":
            path = hub.settings.root / "config/app.json"
            app = json.loads(path.read_text(encoding="utf-8"))
            app["api"]["port"] += 1
            path.write_text(json.dumps(app), encoding="utf-8")
        else:
            (hub.settings.root / ".env.local").write_text("PIKA_TEST_NO_AUTOLOAD=changed\n", encoding="utf-8")
        with pytest.raises(AgentError, match="ROUTE_RUNTIME_RESTART_REQUIRED"):
            worker.authorize(hub.settings.root, payload, options, authority_core=frozen)
        assert os.getenv("PIKA_TEST_NO_AUTOLOAD") is None


@pytest.mark.parametrize("code,expected", [
    ("ROUTE_DISPATCH_LEASE_LOST", "PIKABU_DISPATCH_LEASE_LOST"),
    ("ROUTE_RUNTIME_RESTART_REQUIRED", "PIKABU_RUNTIME_RESTART_REQUIRED"),
    ("PIKABU_EDITOR_FOCUS_CHANGED", "PIKABU_EDITOR_FOCUS_CHANGED"),
    ("private token / cookie / traceback", "PIKABU_OPERATION_UNCONFIRMED"),
])
def test_prejournal_worker_failure_preserves_only_known_sanitized_reason(tmp_path, monkeypatch, code, expected):
    adapter = PikabuBrowser(tmp_path)
    monkeypatch.setattr(adapter, "validate", lambda *args: {})
    def failed(*args, **kwargs):
        raise AgentError(code, 409)
    monkeypatch.setattr(adapter, "_call", failed)
    delivery = adapter.publish("fixture_owner", {}, "operation")
    assert delivery.state == "unknown" and delivery.verified is False
    assert delivery.evidence == {"kind": "browser_worker_error", "failure_code": expected}
    assert "private" not in json.dumps(delivery.evidence)


def test_nonprefix_worker_sql_error_is_sanitized_before_it_leaves_call(tmp_path, monkeypatch):
    adapter = PikabuBrowser(tmp_path)
    monkeypatch.setattr("max_postiz_agent.pikabu_browser.run_browser_worker",
        lambda *args, **kwargs: SimpleNamespace(stdout='{"code":"ROUTE_DISPATCH_LEASE_LOST"}'))
    with pytest.raises(AgentError, match="PIKABU_DISPATCH_LEASE_LOST"):
        adapter._call("inspect", "fixture_owner")


@pytest.mark.parametrize("failure", ["timeout", "cleanup"])
def test_owned_process_failure_is_unknown_without_payload_or_child_output(tmp_path, monkeypatch, failure):
    from max_postiz_agent.browser_process import BrowserWorkerError
    adapter = PikabuBrowser(tmp_path)
    adapter.runtime_generation = "a" * 64
    monkeypatch.setattr(adapter, "validate", lambda *args: {})
    def child(command, *, input, timeout):
        assert json.loads(input)["runtime_generation"] == adapter.runtime_generation
        if failure == "timeout":
            raise subprocess.TimeoutExpired("browser-worker", timeout,
                output="private-child-stdout", stderr="private-child-stderr")
        raise BrowserWorkerError("BROWSER_WORKER_CLEANUP_UNKNOWN")
    monkeypatch.setattr("max_postiz_agent.pikabu_browser.run_browser_worker", child)
    delivery = adapter.publish("fixture_owner", {"body": "private-approved-body"}, "operation")
    assert delivery.state == "unknown" and delivery.verified is False
    assert delivery.evidence == {"kind": "browser_worker_error",
        "failure_code": "PIKABU_OPERATION_UNCONFIRMED"}
    assert "private" not in json.dumps(delivery.evidence)
    assert adapter.runtime_generation not in json.dumps(delivery.evidence)
