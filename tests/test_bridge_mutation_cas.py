"""Native expected-hash guards run before the Postiz controller can overwrite."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import BridgePost

pytest_plugins = ["test_max_bridge"]


def draft(bridge):
    core, records, _, _, _ = bridge
    records["post-max"]["state"] = "DRAFT"
    core.bridge.sync(force=True)
    return core, records, core.bridge.normalize(records["post-max"])[3]


def owner(core):
    with core.db.session() as session:
        return session.get(BridgePost, "post-max").mutation_owner


def test_compare_does_not_take_an_orphan_mutation_lease(bridge, monkeypatch):
    core, _, expected = draft(bridge)
    with core.db.session.begin() as session:
        session.get(BridgePost, "post-max").mutation_until = 1
    reads = []
    monkeypatch.setattr(core.postiz, "get_post", lambda ident: reads.append(ident))
    with pytest.raises(AgentError, match="BRIDGE_EDIT_IN_PROGRESS"):
        core.bridge.mutation(["post-max"], expected_hash=expected, expected_post_id="post-max")
    assert not reads and owner(core) is None


def test_compare_reads_fresh_post_only_after_single_native_lease(bridge, monkeypatch):
    core, _, expected = draft(bridge)
    original = core.postiz.get_post
    reads = []

    def get_post(ident):
        reads.append(ident)
        assert owner(core)
        with pytest.raises(AgentError, match="BRIDGE_EDIT_IN_PROGRESS"):
            core.bridge.mutation([ident])
        return original(ident)

    monkeypatch.setattr(core.postiz, "get_post", get_post)
    lease = core.bridge.mutation(["post-max"], expected_hash=expected, expected_post_id="post-max")
    assert lease == {"owner": owner(core), "cas_verified": True,
                     "expected_hash": expected, "expected_post_id": "post-max"}
    assert reads == ["post-max"]
    core.bridge.mutation(["post-max"], release=lease["owner"])
    assert owner(core) is None


def test_edit_between_initial_sync_and_lease_is_not_overwritten(bridge, monkeypatch):
    core, records, expected = draft(bridge)
    original_sync = core.bridge.sync

    def sync(*args, **kwargs):
        result = original_sync(*args, **kwargs)
        records["post-max"]["content"] = "Конкурентная правка до получения lease."
        return result

    monkeypatch.setattr(core.bridge, "sync", sync)
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_CHANGED"):
        core.bridge.mutation(["post-max"], expected_hash=expected, expected_post_id="post-max")
    assert records["post-max"]["content"] == "Конкурентная правка до получения lease."
    assert owner(core) is None


@pytest.mark.parametrize("field,value", [
    ("id", "different-post"), ("state", "QUEUE"), ("content", "Конкурентный текст."),
    ("group", "other-group"), ("settings", '{"changed":true}'),
    ("publishDate", "2030-01-01T00:00:00+00:00"),
    ("integration", {"id": "other-integration"}),
])
def test_compare_rejects_any_changed_remote_before_controller(bridge, monkeypatch, field, value):
    core, records, expected = draft(bridge)
    remote = copy.deepcopy(records["post-max"])
    remote[field] = value
    monkeypatch.setattr(core.postiz, "get_post", lambda _: remote)
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_CHANGED"):
        core.bridge.mutation(["post-max"], expected_hash=expected, expected_post_id="post-max")
    assert owner(core) is None


def test_failed_fresh_read_releases_only_known_pre_controller_lease(bridge, monkeypatch):
    core, _, expected = draft(bridge)

    def unavailable(_):
        assert owner(core)
        raise AgentError("POSTIZ_UNAVAILABLE", 503)

    monkeypatch.setattr(core.postiz, "get_post", unavailable)
    with pytest.raises(AgentError, match="POSTIZ_UNAVAILABLE"):
        core.bridge.mutation(["post-max"], expected_hash=expected, expected_post_id="post-max")
    assert owner(core) is None


def test_cleanup_never_releases_another_owner(bridge, monkeypatch):
    core, records, expected = draft(bridge)

    def changed_owner(_):
        with core.db.session.begin() as session:
            session.get(BridgePost, "post-max").mutation_owner = "another-owner"
        return copy.deepcopy(records["post-max"])

    monkeypatch.setattr(core.postiz, "get_post", changed_owner)
    with pytest.raises(AgentError, match="BRIDGE_EDIT_IN_PROGRESS"):
        core.bridge.mutation(["post-max"], expected_hash=expected, expected_post_id="post-max")
    assert owner(core) == "another-owner"


@pytest.mark.parametrize("identifiers,expected,post", [
    (["post-max"], "a" * 64, None), (["post-max"], None, "post-max"),
    (["post-max"], "A" * 64, "post-max"), (["group-max"], "a" * 64, "post-max"),
    (["post-max", "other"], "a" * 64, "post-max"),
    (["post-max"], True, "post-max"),
])
def test_bad_cas_contract_is_rejected_without_sync(bridge, monkeypatch, identifiers, expected, post):
    core, _, _ = draft(bridge)
    monkeypatch.setattr(core.bridge, "sync", lambda **_: pytest.fail("invalid CAS must not sync"))
    with pytest.raises(AgentError, match="BRIDGE_MUTATION_CAS_INVALID"):
        core.bridge.mutation(identifiers, expected_hash=expected, expected_post_id=post)
    assert owner(core) is None


def test_api_forwards_cas_and_finish_keeps_original_contract(bridge):
    core, _, expected = draft(bridge)
    client = TestClient(hub_app(core))
    headers = {"X-Max-Bridge-Key": core.bridge.key}
    payload = {"identifiers": ["post-max"], "expected_hash": expected, "expected_post_id": "post-max"}
    response = client.post("/api/max-bridge/mutation/start", json=payload, headers=headers)
    assert response.status_code == 200
    lease = response.json()
    assert lease["cas_verified"] is True and owner(core) == lease["owner"]
    response = client.post("/api/max-bridge/mutation/finish",
                           json={**payload, "owner": lease["owner"]}, headers=headers)
    assert response.status_code == 409 and owner(core) == lease["owner"]
    response = client.post("/api/max-bridge/mutation/finish",
                           json={"identifiers": ["post-max"], "owner": lease["owner"]}, headers=headers)
    assert response.status_code == 200 and owner(core) is None
    response = client.post("/api/max-bridge/mutation/start",
                           json={**payload, "owner": "caller-must-not-own-cas"}, headers=headers)
    assert response.status_code == 409 and owner(core) is None
    # Requests without CAS retain the earlier start/finish contract.
    response = client.post("/api/max-bridge/mutation/start",
                           json={"identifiers": ["post-max"], "owner": "ignored-legacy-field"}, headers=headers)
    assert response.status_code == 200 and owner(core) != "ignored-legacy-field"
    response = client.post("/api/max-bridge/mutation/finish",
                           json={"identifiers": ["post-max"], "owner": response.json()["owner"]}, headers=headers)
    assert response.status_code == 200 and owner(core) is None
    for malformed in (True, "A" * 64, [expected]):
        response = client.post("/api/max-bridge/mutation/start",
                               json={**payload, "expected_hash": malformed}, headers=headers)
        assert response.status_code == 422 and owner(core) is None


def test_native_typescript_guard_mock_controls(tmp_path):
    """Execute the guard body in Node with only its TypeScript syntax removed."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the native guard controls")
    source = Path(__file__).resolve().parents[1] / "deployment/postiz-max/max-browser.guard.ts"
    runner = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let source = fs.readFileSync(process.argv[1], 'utf8')
 .replace(/^import .*;\r?\n/gm, '').replace('@Injectable()', '')
 .replace('export class MaxBrowserMutationGuard implements CanActivate', 'class MaxBrowserMutationGuard')
 .replace('constructor(private prisma: PrismaService) {}', 'constructor(prisma) { this.prisma = prisma; }')
 .replace('context: ExecutionContext', 'context').replace(': Promise<boolean>', '')
 .replace('Set<string>', 'Set').replace(/: string/g, '').replace(/: any/g, '');
source += '\n globalThis.Guard = MaxBrowserMutationGuard;';
const hash = 'a'.repeat(64), post = 'post-max', group = 'fixture-group', integrationId = 'fixture-max';
const storedPost = {id: post, parentPostId: null, state:'DRAFT', group, integrationId};
async function run(change = {}, ack = 'valid', rows = [{...storedPost}]) {
 const calls = [], events = [], headers = {}, request = {method:'POST', path:'/public/v1/posts',
  headers:{'x-max-postiz-expected-hash':hash}, body:{type:'draft',republish:false,
  posts:[{group,integration:{id:integrationId},value:[{id:post,content:'corrected'}]}]}, ...change};
 const context = {switchToHttp:()=>({getRequest:()=>request,
  getResponse:()=>({once:(...a)=>events.push(a),setHeader:(k,v)=>headers[k]=v})})};
 const scope = {process:{env:{MAX_POSTIZ_BRIDGE_KEY:'mock-key'}},
  ConflictException:class extends Error {}, maxBridgeRequest:async(path,data)=>{
   calls.push({path,data});
   if (ack === 'unknown') throw new Error('MAX_BROWSER_AGENT_UNAVAILABLE');
   return {owner:'mock-owner',...(ack==='valid'?{cas_verified:true,expected_hash:hash,expected_post_id:post}:{})};
  }};
 vm.runInNewContext(source, scope);
 const guard = new scope.Guard({post:{findMany:async()=>rows}});
 let error = null;
 try { await guard.canActivate(context); } catch(e) { error=e.message; }
 return {calls,events,headers,error};
}
(async()=>{
 let result = await run();
 assert.equal(result.error,null); assert.equal(result.calls.length,1);
 assert.equal(result.calls[0].data.expected_hash,hash); assert.equal(result.calls[0].data.expected_post_id,post);
 assert.equal(result.headers['X-Max-Postiz-CAS-Verified'],hash); assert.equal(result.events.length,1);
 result = await run({},'legacy');
 assert.equal(result.error,'MAX_BROWSER_CAS_UNCONFIRMED'); assert.equal(result.calls.length,2);
 assert.equal(result.calls[1].path,'/mutation/finish'); assert.equal(result.events.length,0);
 result = await run({},'unknown');
 assert.equal(result.error,'MAX_BROWSER_AGENT_UNAVAILABLE'); assert.equal(result.calls.length,1);
 assert.equal(result.events.length,0); assert.equal(Object.keys(result.headers).length,0);
 for (const header of ['bad',[hash],hash.toUpperCase()]) {
  result = await run({headers:{'x-max-postiz-expected-hash':header}});
  assert.equal(result.error,'MAX_BROWSER_CAS_INVALID'); assert.equal(result.calls.length,0);
 }
 for (const change of [{method:'PUT'}, {body:{type:'schedule',posts:[{value:[{id:post}]}]}},
  {body:{type:'draft',posts:[{value:[{id:post},{id:'other'}]}]}},
  {body:{type:'draft',posts:[{value:[{id:post}]},{value:[{id:'new'}]}]}}]) {
  result = await run(change); assert.equal(result.error,'MAX_BROWSER_CAS_INVALID');
  assert.equal(result.calls.length,0);
 }
 result = await run({},'valid',[]); assert.equal(result.error,'MAX_BROWSER_CAS_INVALID');
 for (const change of [{state:'QUEUE'},{group:'other-group'},{integrationId:'other-integration'},
  {state:undefined},{group:undefined},{integrationId:undefined}]) {
  result = await run({},'valid',[{...storedPost,...change}]);
  assert.equal(result.error,'MAX_BROWSER_CAS_INVALID'); assert.equal(result.calls.length,0);
 }
 for (const entry of [
  {integration:{id:integrationId},value:[{id:post,content:'corrected'}]},
  {group,value:[{id:post,content:'corrected'}]},
  {group:'other-group',integration:{id:integrationId},value:[{id:post,content:'corrected'}]},
  {group,integration:{id:'other-integration'},value:[{id:post,content:'corrected'}]},
 ]) {
  result = await run({body:{type:'draft',republish:false,posts:[entry]}});
  assert.equal(result.error,'MAX_BROWSER_CAS_INVALID'); assert.equal(result.calls.length,0);
 }
 result = await run({headers:{}}); assert.equal(result.error,null);
 assert.equal(result.calls[0].data.expected_hash,undefined); assert.equal(result.events.length,1);
 console.log(JSON.stringify({controls:22,real_mutations:0}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", runner, str(source)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"controls": 22, "real_mutations": 0}
