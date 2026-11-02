"""CI guards and local synthetic DOM: no namespace, DB or HTTP server."""

import base64
import copy
import importlib.util
import io
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest


@pytest.fixture
def scenario():
    path = Path(__file__).resolve().parents[1] / "deployment/linux/readiness_scenario.py"
    spec = importlib.util.spec_from_file_location("readiness_scenario_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("duplicate_inside", [False, True], ids=["upstream-heading", "duplicate-panel-heading"])
def test_editorial_heading_is_unique_inside_the_main_panel(scenario, duplicate_inside):
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", chromium_sandbox=True, headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content("<header><h1>Редакция</h1></header><main><h1>Редакция</h1>"
                             + ("<h1>Редакция</h1>" if duplicate_inside else "") + "</main>")
            assert page.get_by_role("heading", name="Редакция", exact=True, level=1).count() == (
                3 if duplicate_inside else 2)
            if duplicate_inside:
                with pytest.raises((scenario.ScenarioError, Error)):
                    scenario.wait_editorial_panel(page)
            else:
                assert scenario.wait_editorial_panel(page).count() == 1
        finally:
            browser.close()


def screenshot_folder(root):
    folder = root / "data/ci-public"
    folder.mkdir(parents=True)
    folder.parent.chmod(0o700)
    folder.chmod(0o700)
    return folder


def test_screenshot_bytes_are_exclusive_and_use_explicit_private_mode(scenario, tmp_path, monkeypatch):
    folder = screenshot_folder(tmp_path)
    raw = scenario.png_bytes()
    original_open, original_fsync = os.open, os.fsync
    opened, synced = [], []

    def tracked_open(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            opened.append((flags, mode))
        return original_open(path, flags, mode, **kwargs)

    def tracked_fsync(descriptor):
        synced.append(descriptor)
        return original_fsync(descriptor)

    monkeypatch.setattr(scenario.os, "open", tracked_open)
    monkeypatch.setattr(scenario.os, "fsync", tracked_fsync)
    scenario.save_screenshot(tmp_path, folder, raw)
    path = folder / "editorial-synthetic.png"
    assert path.read_bytes() == raw
    assert len(opened) == 1 and opened[0][1] == 0o600
    assert opened[0][0] & os.O_CREAT and opened[0][0] & os.O_EXCL
    if os.name != "nt":
        assert opened[0][0] & os.O_NOFOLLOW
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(synced) == 2
    else:
        assert len(synced) == 1
    with pytest.raises(scenario.ScenarioError, match="SCENARIO_EVIDENCE_ALREADY_EXISTS"):
        scenario.save_screenshot(tmp_path, folder, b"replacement")
    assert path.read_bytes() == raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask and permission bits")
@pytest.mark.parametrize("mask", [0o002, 0o777])
def test_screenshot_mode_is_0600_independent_of_process_umask(scenario, tmp_path, mask):
    folder = screenshot_folder(tmp_path)
    previous = os.umask(mask)
    try:
        scenario.save_screenshot(tmp_path, folder, scenario.png_bytes())
    finally:
        os.umask(previous)
    assert stat.S_IMODE((folder / "editorial-synthetic.png").stat().st_mode) == 0o600


def test_screenshot_destination_must_be_exact_project_private_output(scenario, tmp_path):
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    with pytest.raises(scenario.ScenarioError, match="SCENARIO_SCREENSHOT_PATH_INVALID"):
        scenario.save_screenshot(tmp_path, folder, scenario.png_bytes())
    assert list(folder.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory and symlink guards")
@pytest.mark.parametrize("bad_directory", ["permissions", "symlink"])
def test_screenshot_rejects_nonprivate_or_linked_output(scenario, tmp_path, bad_directory):
    folder = screenshot_folder(tmp_path)
    if bad_directory == "permissions":
        folder.chmod(0o755)
    else:
        folder.rmdir()
        other = tmp_path / "outside"
        other.mkdir(mode=0o700)
        folder.symlink_to(other, target_is_directory=True)
    with pytest.raises(scenario.ScenarioError):
        scenario.save_screenshot(tmp_path, folder, scenario.png_bytes())
    assert not (folder / "editorial-synthetic.png").exists()


def state(root):
    return {"contract": "three-platform-native-readiness-v1", "scope": "ephemeral_github_hosted_no_external_publications",
        "runtime_id": "a" * 32, "new_empty_runtime": True, "root": str(root.resolve()),
        "namespace_owner_pid": 12345, "namespace_inode": 1234567, "public_origin": "http://10.20.30.45:4007",
        "private_postiz_base": "http://127.0.0.1:14007/api/public/v1",
        "postiz_dsn": "postgresql://fixture:fixture-password@127.0.0.1:5432/postiz_fixture",
        "hub_dsn": "postgresql://fixture:fixture-password@127.0.0.1:5434/hub_fixture",
        "internal_key": "i" * 40, "jwt_secret": "j" * 40, "owner_password": "p" * 40,
        "owner_email": "ci@example.invalid", "profile_id": "test", "vk_owner": "123456789",
        "service_unit": "max-postiz-editorial.service", "login_service_unit": "max-postiz-login.service"}


def test_state_contract_has_exact_runtime_origin_ports_and_no_arbitrary_service(scenario, tmp_path):
    supplied = state(tmp_path)
    assert scenario.validate_state(supplied, tmp_path) is supplied
    for key, value in (("scope", "production"), ("new_empty_runtime", False), ("root", str(tmp_path.parent)),
        ("runtime_id", "../not-owned"), ("public_origin", "http://10.20.30.45:4008"),
        ("private_postiz_base", "http://10.20.30.45:14007/api/public/v1"), ("namespace_inode", True),
        ("service_unit", "other.service"), ("login_service_unit", "ssh.service"), ("owner_email", "real@personal.test")):
        with pytest.raises(scenario.ScenarioError):
            scenario.validate_state({**supplied, key: value}, tmp_path)


@pytest.mark.parametrize("url", ["postgresql://fixture:private@10.20.30.45:5434/hub", "sqlite:///agent.db",
    "postgresql://fixture:private@127.0.0.1:55448/hub", "postgresql://fixture:private@127.0.0.1:5434/hub?host=outside",
    "postgresql://fixture:private@127.0.0.1:bad/hub", "postgresql://fixture@127.0.0.1:5434/hub"])
def test_database_dsn_scope_errors_never_echo_credentials(scenario, url):
    with pytest.raises(scenario.ScenarioError) as error:
        scenario.dsn(url, 5434)
    assert str(error.value) == "SCENARIO_DATABASE_SCOPE_INVALID" and "private" not in str(error.value)


def test_exact_decoded_image_boundary_is_valid_png_with_same_base64_length(scenario):
    exact, excess = scenario.png_bytes(10 * scenario.MIB), scenario.png_bytes(10 * scenario.MIB + 1)
    assert len(exact) == 10 * scenario.MIB and len(excess) == 10 * scenario.MIB + 1
    assert len(base64.b64encode(exact)) == len(base64.b64encode(excess))
    for raw in (exact, excess):
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (96, 64) and image.format == "PNG"
            image.verify()


def fake_client(scenario):
    instance = object.__new__(scenario.Scenario)
    instance.check = lambda: None
    calls = []
    instance.client = httpx.Client(base_url=scenario.ORIGIN, transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(201, json={})))
    return instance, calls


def test_failed_http_evidence_keeps_only_fixed_route_status_action_and_body_digest(scenario, tmp_path, monkeypatch, capsys):
    import hashlib
    import json
    fixture_canary = "private-fixture-must-not-enter-evidence"
    response_body = json.dumps({"token": fixture_canary}).encode()
    instance = object.__new__(scenario.Scenario)
    instance.check = lambda: None
    instance.client = httpx.Client(base_url=scenario.ORIGIN, cookies={"auth": fixture_canary},
        transport=httpx.MockTransport(lambda request: httpx.Response(502, content=response_body,
            headers={"Set-Cookie": "auth=" + fixture_canary})))
    try:
        with pytest.raises(scenario.ScenarioError, match="^SCENARIO_HTTP_STATUS_INVALID$") as caught:
            instance.admin({"action": "set_secret", "id": "ci_tg", "value": fixture_canary})
        expected = {"method": "POST", "path": "/api/editorial/route-admin", "status": 502,
                    "action": "set_secret", "body_sha256": hashlib.sha256(response_body).hexdigest()}
        assert scenario.safe_http_metadata(caught.value.http) == expected
        instance.run = lambda mode: (_ for _ in ()).throw(caught.value)
        monkeypatch.setattr(scenario, "Scenario", lambda *args: instance)
        writes = []
        monkeypatch.setattr(scenario, "atomic", lambda path, value, **kwargs: writes.append((path, value, kwargs)))
        assert scenario.main(["--root", str(tmp_path), "--state", str(tmp_path / "private.json"),
                              "--evidence", str(tmp_path / "public"), "--mode", "exercise"]) == 1
        printed = capsys.readouterr().out
        result = json.loads(printed)
        assert {key: value for key, value in result.items() if key != "exception"} == {
            "mode": "exercise", "passed": False, "code": "SCENARIO_HTTP_STATUS_INVALID", "http": expected}
        assert result["exception"]["kind"] == "ScenarioError"
        assert all(frame["function"] in {"main", "admin", "call"} and type(frame["line"]) is int
                   for frame in result["exception"]["frames"])
        assert writes == [(tmp_path / "public/exercise-failed.json", result, {"exclusive": True})]
        assert fixture_canary not in printed and fixture_canary not in json.dumps(writes, default=str)
    finally:
        instance.client.close()


@pytest.mark.parametrize("change", [
    {"path": "https://private.invalid/token"}, {"path": []}, {"action": "private-fixture-value"},
    {"method": "DELETE"}, {"status": True}, {"body_sha256": "private-fixture-value"}, {"headers": "private-fixture-value"},
    {"response_code": "PRIVATE_FIXTURE_CANARY"},
])
def test_http_diagnostic_projection_rejects_untrusted_or_private_fields(scenario, change):
    value = {"method": "GET", "path": "/api/editorial/route-setup", "status": 403, "body_sha256": "a" * 64}
    assert scenario.safe_http_metadata({**value, **change}) is None


@pytest.mark.parametrize("body,expected", [
    (b'{"code":"EDITORIAL_OWNER_REQUIRED","private":"fixture-private-value"}', "EDITORIAL_OWNER_REQUIRED"),
    (b'{"code":"PRIVATE_FIXTURE_CANARY"}', None),
    (b'{"code":["EDITORIAL_OWNER_REQUIRED"]}', None),
    (b'non-json fixture-private-value', None),
    (b'{"code":"EDITORIAL_OWNER_REQUIRED"}' + b' ' * 65536, None),
], ids=["known", "unknown", "non-string", "non-json", "oversized"])
def test_http_response_code_uses_only_finite_known_constants_and_bounded_json(scenario, body, expected):
    instance = object.__new__(scenario.Scenario)
    instance.check = lambda: None
    instance.client = httpx.Client(base_url=scenario.ORIGIN,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, content=body)))
    try:
        with pytest.raises(scenario.ScenarioError) as caught:
            instance.call("GET", "/api/editorial/route-setup")
        metadata = scenario.safe_http_metadata(caught.value.http)
        assert metadata["status"] == 403 and metadata.get("response_code") == expected
        serialized = scenario.json.dumps(metadata)
        assert "fixture-private-value" not in serialized and "PRIVATE_FIXTURE_CANARY" not in serialized
        assert ("response_code" in metadata) is (expected is not None)
    finally:
        instance.client.close()


@pytest.mark.parametrize("status", [200, 401, 403])
def test_mixed_owner_cookie_and_bearer_probe_keeps_existing_rejection_expectations(scenario, status):
    instance = object.__new__(scenario.Scenario)
    instance.check = lambda: None
    requests = []
    instance.client = httpx.Client(base_url=scenario.ORIGIN, cookies={"auth": "fixture-owner-cookie"},
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(status, json={})))
    try:
        if status == 200:
            with pytest.raises(scenario.ScenarioError, match="SCENARIO_HTTP_STATUS_INVALID"):
                instance.call("GET", "/api/editorial/route-setup", allowed=(401, 403),
                              headers={"Authorization": "Bearer ci-invalid"})
        else:
            assert instance.call("GET", "/api/editorial/route-setup", allowed=(401, 403),
                                 headers={"Authorization": "Bearer ci-invalid"}).status_code == status
        assert len(requests) == 1 and requests[0].headers["Authorization"] == "Bearer ci-invalid"
        assert "auth=fixture-owner-cookie" in requests[0].headers["Cookie"]
    finally:
        instance.client.close()


@pytest.mark.parametrize("method,path,status,accepted", [
    ("POST", "/api/editorial/route-admin", 201, True),
    ("POST", "/api/editorial/route-admin", 200, False),
    ("POST", "/api/editorial/route-admin", 202, False),
    ("POST", "/api/editorial/route-action", 201, True),
    ("POST", "/api/editorial/route-action", 200, False),
    ("POST", "/api/editorial/control", 201, True),
    ("POST", "/api/editorial/control", 202, False),
    ("POST", "/api/auth/register", 200, True),
    ("POST", "/api/auth/register", 201, False),
    ("POST", "/api/auth/login", 200, True),
    ("POST", "/api/auth/login", 201, False),
    ("GET", "/api/editorial/route-setup", 200, True),
    ("GET", "/api/editorial/route-setup", 201, False),
])
def test_native_success_status_matches_exact_auth_and_nest_bff_contract(scenario, method, path, status, accepted):
    instance = object.__new__(scenario.Scenario)
    instance.check = lambda: None
    requests = []
    instance.client = httpx.Client(base_url=scenario.ORIGIN, transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(status, json={})))
    kwargs = {"json": {"action": "preflight" if path.endswith("route-action") else "pause"}}
    try:
        if accepted:
            assert instance.call(method, path, **kwargs).status_code == status
        else:
            with pytest.raises(scenario.ScenarioError, match="SCENARIO_HTTP_STATUS_INVALID"):
                instance.call(method, path, **kwargs)
        assert len(requests) == 1
    finally:
        instance.client.close()


@pytest.mark.parametrize("status", [403, 413, 422])
def test_explicit_negative_status_is_not_replaced_by_bff_success_default(scenario, status):
    instance = object.__new__(scenario.Scenario)
    instance.check = lambda: None
    instance.client = httpx.Client(base_url=scenario.ORIGIN,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={})))
    try:
        assert instance.admin({"action": "upload_media"}, allowed=(status,)) == {}
    finally:
        instance.client.close()


def test_generic_exception_metadata_excludes_foreign_frames_messages_and_arguments(scenario):
    import json
    fixture_canary = "private-exception-fixture-must-never-be-logged"
    try:
        scenario.require(False, fixture_canary)
    except scenario.ScenarioError as error:
        result = scenario.safe_exception_metadata(error)
    assert result["kind"] == "ScenarioError" and result["frames"]
    assert all(frame["function"] == "require" and 1 <= frame["line"] <= 10000 for frame in result["frames"])
    serialized = json.dumps(result)
    assert fixture_canary not in serialized and str(Path(__file__).parent) not in serialized
    assert "test_generic_exception_metadata" not in serialized


@pytest.mark.parametrize("message,flag", [
    ("No usable sandbox! private-canary", "sandbox_blocked"),
    ("Failed to move to new namespace private-canary", "sandbox_blocked"),
    ("Executable doesn't exist at private-canary", "executable_missing"),
    ("strict mode violation: private-canary", "strict_mode"),
    ("net::ERR_BLOCKED_BY_CLIENT private-canary", "err_blocked"),
    ("unlisted private-canary", None),
])
def test_playwright_exception_flags_are_only_finite_booleans_never_raw_text(scenario, message, flag):
    error_type = type("Error", (RuntimeError,), {"__module__": "playwright._impl._errors"})
    result = scenario.safe_exception_metadata(error_type(message))
    assert result["kind"] == "Error" and result["frames"] == []
    assert {key for key, value in result["playwright_flags"].items() if value} == ({flag} if flag else set())
    assert "private-canary" not in scenario.json.dumps(result)
    assert not any(scenario.safe_exception_metadata(RuntimeError(message))["playwright_flags"].values())


def test_last_completed_http_is_kept_in_memory_and_strictly_projected_on_generic_failure(scenario, tmp_path, monkeypatch, capsys):
    instance, requests = fake_client(scenario)
    try:
        instance.admin({"action": "upload_media", "value": "private-request-fixture"})
        metadata = scenario.safe_http_metadata(instance.last_http)
        assert metadata["action"] == "upload_media" and metadata["status"] == 201
        instance.run = lambda mode: (_ for _ in ()).throw(KeyError("private-exception-fixture"))
        monkeypatch.setattr(scenario, "Scenario", lambda *args: instance)
        writes = []
        monkeypatch.setattr(scenario, "atomic", lambda path, value, **kwargs: writes.append(value))
        assert scenario.main(["--root", str(tmp_path), "--state", str(tmp_path / "state"),
            "--evidence", str(tmp_path / "evidence"), "--mode", "exercise"]) == 1
        output = capsys.readouterr().out
        result = scenario.json.loads(output)
        assert result["code"] == "SCENARIO_FAILED" and result["exception"]["kind"] == "KeyError"
        assert result["last_http"] == metadata and writes == [result] and len(requests) == 1
        assert "private-request-fixture" not in output and "private-exception-fixture" not in output
    finally:
        instance.client.close()


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/editorial/route-action", {"action": "approve"}),
    ("POST", "/api/editorial/route-action", {"action": "inspect"}),
    ("POST", "/api/editorial/route-action", {"action": "resume"}),
    ("POST", "/api/editorial/control", {"action": "start"}),
    ("POST", "/api/posts", {}), ("DELETE", "/api/editorial/route-setup", {}),
])
def test_external_mutations_and_unlisted_routes_are_blocked_before_http(scenario, method, path, payload):
    instance, calls = fake_client(scenario)
    try:
        with pytest.raises(scenario.ScenarioError):
            instance.call(method, path, json=payload)
        assert calls == []
    finally:
        instance.client.close()


@pytest.mark.parametrize("action", ["publish", "dispatch", "approve", "resume", "reconcile"])
def test_admin_actions_remain_preparation_only(scenario, action):
    instance, calls = fake_client(scenario)
    try:
        with pytest.raises(scenario.ScenarioError, match="SCENARIO_EXTERNAL_ACTION_FORBIDDEN"):
            instance.admin({"action": action})
        assert calls == []
    finally:
        instance.client.close()


def test_prepare_login_reaches_only_owner_administration_route(scenario):
    instance, calls = fake_client(scenario)
    payload = {"action": "prepare_login", "id": "ci_vk", "expected_binding_hash": "b" * 64}
    try:
        assert instance.admin(payload) == {}
        assert len(calls) == 1
        assert calls[0].method == "POST"
        assert str(calls[0].url) == scenario.ORIGIN + "/api/editorial/route-admin"
        assert calls[0].content == scenario.json.dumps(payload, separators=(",", ":")).encode()
    finally:
        instance.client.close()


def test_namespace_failure_precedes_any_http(scenario):
    instance, calls = fake_client(scenario)
    def denied():
        raise scenario.ScenarioError("SCENARIO_NETWORK_NOT_ISOLATED")
    instance.check = denied
    try:
        with pytest.raises(scenario.ScenarioError, match="SCENARIO_NETWORK_NOT_ISOLATED"):
            instance.call("GET", "/api/editorial/route-setup")
        assert calls == []
    finally:
        instance.client.close()


def test_phase_marker_is_durable_before_actions_and_never_reentered(scenario):
    instance = object.__new__(scenario.Scenario)
    instance.state = {"phases": {}}
    saved = []
    instance.save = lambda: saved.append(copy.deepcopy(instance.state))
    instance.phase("exercise")
    assert saved[0]["phases"]["exercise"]["state"] == "started"
    with pytest.raises(scenario.ScenarioError, match="SCENARIO_PHASE_ALREADY_STARTED"):
        instance.phase("exercise")
    assert len(saved) == 1


def test_canonical_history_hash_ignores_row_order_only_when_caller_sorts(scenario):
    rows = [{"id": "b", "state": "unknown", "attempts": 1}, {"id": "a", "state": "pending"}]
    assert scenario.digest(sorted(rows, key=scenario.digest)) == scenario.digest(sorted(reversed(rows), key=scenario.digest))
    damaged = copy.deepcopy(rows)
    damaged[0]["state"] = "pending"
    assert scenario.digest(sorted(damaged, key=scenario.digest)) != scenario.digest(sorted(rows, key=scenario.digest))


def test_bind_source_rejects_stale_loaded_other_worktree_module(scenario, monkeypatch, tmp_path):
    monkeypatch.setitem(scenario.sys.modules, "max_postiz_agent", SimpleNamespace(__file__=str(tmp_path.parent / "other/core.py")))
    with pytest.raises(scenario.ScenarioError, match="SCENARIO_SOURCE_ORIGIN_INVALID"):
        scenario.bind_source(tmp_path)
