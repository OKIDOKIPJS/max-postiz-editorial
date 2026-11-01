"""Offline selector exploration must never reach remote mutation handlers."""

import json
import subprocess
from types import SimpleNamespace

from scripts.probe_pikabu_offline import NetworkGuard, probe_ephemeral
from test_pikabu_delivery import browser_fixture as browser_fixture, post as post


class Route:
    def __init__(self, method="GET", url="https://pikabu.ru/add", kind="document"):
        self.request = SimpleNamespace(method=method, url=url, resource_type=kind)
        self.actions = []

    def abort(self, reason):
        self.actions.append(("abort", reason))

    def continue_(self):
        self.actions.append(("continue",))


def test_network_guard_blocks_mutations_before_and_after_offline():
    guard = NetworkGuard()
    main = Route()
    guard.request(main)
    assert main.actions == [("continue",)]
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        route = Route(method=method)
        guard.request(route)
        assert route.actions == [("abort", "blockedbyclient")]
    xhr = Route(kind="fetch", url="https://pikabu.ru/ajax.php?mode=save_story")
    guard.request(xhr)
    assert xhr.actions[0][0] == "abort"
    guard.freeze()
    static = Route(kind="script", url="https://cs.pikabu.ru/editor.js")
    guard.request(static)
    assert static.actions[0][0] == "abort"
    assert guard.proof()["external_mutations"] == 0
    assert guard.proof()["blocked_mutation_attempts"] == 5
    assert guard.proof()["network_restored"] is False


def test_offline_real_browser_context_closes_without_network_writes_or_saved_state(tmp_path, browser_fixture):
    page, origin, options, state = browser_fixture
    browser = page.context.browser
    initial_contexts = len(browser.contexts)
    private_state = {"cookies": [], "origins": [{"origin": origin, "localStorage": [
        {"name": "PRIVATE_SENTINEL", "value": "must-never-be-reported-or-written"}]}]}
    report = probe_ephemeral(browser, private_state, options, origin=origin)
    assert report["state"] == "offline_selector_probe_complete", report
    assert report["ephemeral_context_closed"] is True
    assert report["offline_before_first_edit"] is True
    assert report["blocked_mutation_attempts"] >= 1
    assert report["external_mutations"] == report["live_uploads"] == report["publish_clicks"] == 0
    assert state["uploads"] == state["posts"] == 0
    assert report["storage_state_written"] is False
    assert "must-never-be-reported-or-written" not in json.dumps(report)
    assert len(browser.contexts) == initial_contexts
    assert {item["synthetic_tag"] for item in report["tag_chip_candidates"]} == {"Космос", "Астрономия"}
    assert not list(tmp_path.rglob("*storage*.json"))
    # The separately owned source context was never navigated or edited.
    assert page.url == "about:blank"


def test_offline_context_closes_on_editor_error_and_never_restores_network(browser_fixture):
    page, origin, options, state = browser_fixture
    browser = page.context.browser
    initial_contexts = len(browser.contexts)
    seen = []
    def fail_after_edit(ephemeral_page, selectors, **kwargs):
        seen.append(ephemeral_page.context)
        ephemeral_page.locator(selectors["title"]).fill("Синтетический offline-заголовок")
        raise RuntimeError("Do not include private state in the error report")
    report = probe_ephemeral(browser, {"cookies": [], "origins": []}, options,
                             origin=origin, mutator=fail_after_edit)
    assert seen
    assert report["state"] == "stopped"
    assert report["ephemeral_context_closed"] is True
    assert report["offline_before_first_edit"] is True
    assert report["network_restored"] is False
    assert report["code"] == "PIKABU_OFFLINE_PROBE_STOPPED"
    assert len(browser.contexts) == initial_contexts
    assert state["uploads"] == state["posts"] == 0
    assert page.url == "about:blank"


def test_unsupported_ephemeral_browser_has_no_live_fallback():
    report = probe_ephemeral(None, {"cookies": [], "origins": []}, {"owner": "fixture_owner"})
    assert report["state"] == "unsupported_ephemeral_context"
    assert report["publish_clicks"] == report["external_mutations"] == 0
    assert report["storage_state_written"] is False


def test_budget_controller_stops_only_its_own_child_tree(tmp_path, monkeypatch):
    import scripts.probe_pikabu_offline as probe
    class Process:
        pid = 123456
        calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("offline-probe", timeout)
            return "", ""
        def kill(self):
            raise AssertionError("The whole owned tree should have been stopped")
    process = Process()
    launched, stopped = [], []
    def popen(command, **kwargs):
        launched.append(command)
        return process
    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    monkeypatch.setattr(probe, "_stop_owned_tree", lambda child: stopped.append(child.pid) or True)
    directory = tmp_path / "data/pikabu-login"
    directory.mkdir(parents=True)
    (directory / "offline-dom-progress.json").write_text(json.dumps({
        "stage": "offline_fill_title", "external_mutations": 0, "live_uploads": 0,
        "offline_before_first_edit": True, "all_non_get_head_aborted": True}), encoding="utf-8")
    result = probe.run_bounded(tmp_path, "pikabu_test", timeout=1)
    assert "--internal-run" in launched[0]
    assert stopped == [process.pid]
    assert result["code"] == "PIKABU_OFFLINE_BUDGET_EXPIRED"
    assert result["process_tree_terminated"] is True
    assert result["normal_context_close_confirmed"] is False
    assert result["external_mutations"] == 0
