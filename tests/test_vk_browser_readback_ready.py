"""Read-only known-receipt reconciliation while the native wall hydrates."""
import time

import pytest

from max_postiz_agent import vk_browser_worker as worker
from max_postiz_agent.config import fingerprint
from max_postiz_agent.vk_browser import CONTRACT, dom_contract, prepare
from test_vk_browser import (OWNER, _old_rich_post, personal_wall,
    sample as sample, local_vk as local_vk, browser as browser, page as page)


def receipt(sample, state):
    personal_wall(sample, state)
    plan = prepare(sample.root, sample.options, OWNER, sample.snapshot)
    state.posts = [(plan["text"], 51)]
    state.old_rich_posts = _old_rich_post("Посторонняя старая запись.")
    journal = {"contract": CONTRACT, "root": str(sample.root.resolve()),
        "request_id": sample.payload["request_id"], "destination": OWNER, "snapshot": sample.snapshot,
        "snapshot_sha256": fingerprint(sample.snapshot), "options_sha256": fingerprint(sample.options),
        "dom_contract_sha256": fingerprint(dom_contract(sample.options, publish=True)),
        "state": "unknown", "fixture": True, "clicks": 1, "last_action_intent": "click_intent",
        "worker_code_sha256": worker.WORKER_SOURCE_SHA256, "created_at": time.time() - 5,
        "external_id": OWNER + "_51", "permalink": worker.ORIGIN + "/wall" + OWNER + "_51"}
    path = worker._path(sample.root, sample.payload["request_id"])
    path.parent.mkdir(parents=True)
    worker._save(path, journal, "unknown")
    return plan


def reconcile(page, sample, state):
    return worker.reconcile(page, sample.root, sample.options, OWNER, sample.payload["request_id"],
                            origin=state.origin, fixture=True)


def delayed_wall(page, monkeypatch, *, mode="post", change=None, never_reload=None):
    ready = worker._ready
    initial = []

    def hydrate_after_header(*args):
        ready(*args)
        index = len(initial) + 1
        values = page.evaluate("""({mode,change,never,owner})=>{
          const host=document.getElementById('posts');
          const post=[...host.querySelectorAll('[data-testid=post]')].find(n=>
            n.querySelector('[data-testid=post_date_block_preview]').getAttribute('href')==='/wall'+owner+'_51');
          const text=post.querySelector('[data-testid=post_text]');
          window.readbackHydrated=false;
          const html=text.innerHTML;
          if(mode==='text')text.textContent='';else post.remove();
          const count=host.querySelectorAll('[data-testid=post]').length;
          if(change==='account')setTimeout(()=>document.querySelector('[data-testid=header-profile-menu-button]').href='/id999999',100);
          if(!never)setTimeout(()=>{
            if(mode==='text')text.innerHTML=html;else host.append(post);
            if(change==='text')text.textContent='Другой полный текст.';
            if(change==='author')post.querySelector('[data-testid=post-header-title]').href='/id999999';
            if(change==='image')post.insertAdjacentHTML('beforeend','<img class=approved-photo src=/photo.png>');
            if(change==='attachment')post.insertAdjacentHTML('beforeend','<div class=unexpected-post-attachment>Вложение</div>');
            window.readbackHydrated=true;
          },300);
          return {oldPostCount:count,targetPresent:!!host.querySelector('[href="/wall'+owner+'_51"]')};
        }""", {"mode": mode, "change": change, "never": index == never_reload, "owner": OWNER})
        initial.append(values)

    monkeypatch.setattr(worker, "_ready", hydrate_after_header)
    return initial


@pytest.mark.parametrize("mode", ["post", "text"])
def test_known_receipt_waits_for_exact_post_after_header_on_both_independent_reloads(
        sample, local_vk, page, monkeypatch, mode):
    plan = receipt(sample, local_vk)
    initial = delayed_wall(page, monkeypatch, mode=mode)
    snapshot = worker._post_snapshot
    checked = []

    def strict_after_hydration(*args, **kwargs):
        checked.append(page.evaluate("window.readbackHydrated"))
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(worker, "_post_snapshot", strict_after_hydration)
    result = reconcile(page, sample, local_vk)
    assert result["state"] == "confirmed", result
    assert result["verified"] is False and result["evidence"]["fixture"] is True
    assert len(initial) == 2 and all(item["oldPostCount"] >= 1 for item in initial)
    assert checked == [True, True, True, True]
    proof = result["evidence"]["proof"]
    assert len(proof["reloads"]) == 2
    assert all(row["external_id"] == OWNER + "_51" and row["text_sha256"] == plan["text_sha256"]
               and row["image_sha256"] == [] and row["author"]["id"] == OWNER for row in proof["reloads"])
    assert local_vk.publishes == local_vk.uploads == 0


@pytest.mark.parametrize("never_reload", [1, 2])
def test_header_and_old_posts_do_not_prove_a_missing_known_post(sample, local_vk, page, monkeypatch, never_reload):
    receipt(sample, local_vk)
    initial = delayed_wall(page, monkeypatch, never_reload=never_reload)
    result = reconcile(page, sample, local_vk)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["external_id"] == OWNER + "_51" and result["evidence"]["proof"] is None
    assert result["evidence"]["failure_code"] == "VK_BROWSER_READBACK_NOT_EXACTLY_ONE"
    assert len(initial) == never_reload
    assert local_vk.publishes == local_vk.uploads == 0


@pytest.mark.parametrize("change,code", [
    ("text", "VK_BROWSER_READBACK_NOT_EXACTLY_ONE"),
    ("account", "VK_BROWSER_ACCOUNT_MISMATCH"),
    ("author", "VK_BROWSER_AUTHOR_MISMATCH"),
    ("image", "VK_BROWSER_IMAGE_COUNT_MISMATCH"),
    ("attachment", "VK_BROWSER_READBACK_EXTRA_ATTACHMENT"),
])
def test_hydration_wait_cannot_relax_current_text_account_author_or_media_proof(
        sample, local_vk, page, monkeypatch, change, code):
    receipt(sample, local_vk)
    delayed_wall(page, monkeypatch, change=change)
    result = reconcile(page, sample, local_vk)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["external_id"] == OWNER + "_51" and result["evidence"]["proof"] is None
    assert result["evidence"]["failure_code"] == code
    assert local_vk.publishes == local_vk.uploads == 0


@pytest.mark.parametrize("include_expected", [False, True])
def test_global_wrong_id_or_duplicates_are_never_filtered_down_to_preferred_receipt(
        sample, local_vk, page, monkeypatch, include_expected):
    plan = receipt(sample, local_vk)
    local_vk.posts = [(plan["text"], 52)] + ([(plan["text"], 51)] if include_expected else [])
    waits = []
    native_wait = page.wait_for_timeout

    def observed_wait(milliseconds):
        waits.append(milliseconds)
        native_wait(milliseconds)

    monkeypatch.setattr(page, "wait_for_timeout", observed_wait)
    result = reconcile(page, sample, local_vk)
    assert result["state"] == "unknown" and result["verified"] is False
    assert result["external_id"] == OWNER + "_51" and result["evidence"]["proof"] is None
    assert result["evidence"]["failure_code"] == "VK_BROWSER_READBACK_NOT_EXACTLY_ONE"
    assert waits == []  # A wrong or duplicate exact candidate is not an empty SPA.
    assert local_vk.publishes == local_vk.uploads == 0


def test_failed_readback_does_not_turn_reentry_into_publish(sample, local_vk, page, monkeypatch):
    receipt(sample, local_vk)
    delayed_wall(page, monkeypatch, never_reload=1)
    result = reconcile(page, sample, local_vk)
    assert result["state"] == "unknown"
    gets = list(local_vk.gets)
    repeated = worker.run_publish(page, sample.root, sample.options, sample.payload,
        lambda: pytest.fail("No new dispatch authorization is permitted"), origin=local_vk.origin, fixture=True)
    assert repeated["state"] == "unknown" and repeated["external_id"] == OWNER + "_51"
    assert local_vk.gets == gets and local_vk.publishes == local_vk.uploads == 0
