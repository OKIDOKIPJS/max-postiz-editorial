"""Pure CI scenario guards/wire/PNG tests: no namespace, DB, browser or HTTP server."""

import base64
import copy
import importlib.util
import io
from pathlib import Path
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
        lambda request: calls.append(request) or httpx.Response(200, json={})))
    return instance, calls


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
