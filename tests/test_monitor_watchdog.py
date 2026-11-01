"""Bounded health checks and incident transitions using only injected transports."""

import copy
import json

import httpx
import pytest

from max_postiz_agent import monitor_watchdog
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.monitor_store import MonitorStore
from max_postiz_agent.monitor_watchdog import MAX_HEALTH_BYTES, Watchdog, report_changes


NOW = 1_800_000_000.0
ENDPOINT = "https://observer.example/health/monitor"
INSTANCE = "fixture-instance"


def payload():
    return {"schema": "monitor-heartbeat-v1", "instance_id": INSTANCE, "available": True,
        "observer": {"heartbeat_at": NOW - 1, "source_collected_at": NOW - 1,
            "source_heartbeat_at": NOW - 2, "source_paused": False,
            "sender_heartbeat_at": NOW - 3, "sender_state": "confirmed"}}


def check(value=None, *, response=None, handler=None):
    requests = []

    def transport(request):
        requests.append(request)
        if handler is not None:
            return handler(request)
        return response if response is not None else httpx.Response(200, json=payload() if value is None else value)

    with httpx.Client(transport=httpx.MockTransport(transport), headers={"X-Internal-Key": "fixture-private"}) as client:
        result = Watchdog(client, ENDPOINT, expected_instance=INSTANCE, clock=lambda: NOW).check()
    return result, requests


def test_healthy_checks_exact_endpoint_once_and_does_not_disclose_credentials_or_body():
    value = payload()
    value["private_error"] = "PRIVATE_RESPONSE_MARKER"
    result, requests = check(value)
    assert result["state"] == "healthy" and result["observed_at"] == NOW
    assert len(requests) == 1 and requests[0].method == "GET" and str(requests[0].url) == ENDPOINT
    assert requests[0].headers["X-Internal-Key"] == "fixture-private"
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert requests[0].extensions["timeout"] == {"connect": 5, "read": 10, "write": 10, "pool": 10}
    assert all(marker not in str(result) for marker in ("fixture-private", "PRIVATE_RESPONSE_MARKER", ENDPOINT))


def test_age_boundary_is_inclusive_and_stale_observer_is_detected():
    for age, expected in ((180, "healthy"), (181, "stale")):
        value = payload()
        value["observer"]["heartbeat_at"] = NOW - age
        result, _ = check(value)
        assert result["state"] == expected
        if expected == "stale":
            assert result["incident_key"].endswith(":observer_stale")


def test_editorial_pause_allows_old_or_missing_source_heartbeat():
    for heartbeat in (NOW - 86_400, None):
        value = payload()
        value["observer"].update(source_paused=True, source_heartbeat_at=heartbeat)
        assert check(value)[0]["state"] == "healthy"
        value["observer"]["source_paused"] = False
        assert check(value)[0]["incident_key"].endswith(":source_stale")


def test_fresh_observer_does_not_hide_stale_snapshot_or_failed_sender():
    cases = (("source_collected_at", NOW - 181, "snapshot_stale"),
             ("sender_heartbeat_at", NOW - 181, "sender_stale"),
             ("sender_state", "unknown", "sender_unknown"),
             ("sender_state", "blocked", "sender_blocked"))
    for key, changed, reason in cases:
        value = payload()
        value["observer"][key] = changed
        result, _ = check(value)
        assert result["state"] == "stale" and result["incident_key"].endswith(":" + reason)


def test_intentionally_disabled_sender_can_have_no_heartbeat():
    value = payload()
    value["observer"].update(sender_state="disabled", sender_heartbeat_at=None)
    assert check(value)[0]["state"] == "healthy"


def test_wrong_instance_or_schema_never_becomes_healthy():
    for key, changed, reason in (("instance_id", "different", "instance_mismatch"),
                                 ("schema", "other-contract", "invalid_schema")):
        value = payload()
        value[key] = changed
        result, _ = check(value)
        assert result["state"] == "invalid" and result["incident_key"].endswith(":" + reason)


def test_available_false_is_unavailable_even_when_web_server_responds():
    value = {"schema": "monitor-heartbeat-v1", "instance_id": INSTANCE, "available": False}
    result, _ = check(value)
    assert result["state"] == "unavailable" and result["incident_key"].endswith(":not_available")


def test_malformed_json_duplicate_keys_and_non_utf8_fail_closed():
    for body in (b"not JSON PRIVATE_BODY", b'{"schema":"first","schema":"second"}', b"\xff", b"[]"):
        result, requests = check(response=httpx.Response(200, content=body))
        assert result["state"] == "invalid" and len(requests) == 1
        assert "PRIVATE_BODY" not in str(result)


def test_invalid_types_missing_fields_and_future_timestamps_are_sanitized():
    cases = (("sender_state", []), ("source_paused", "false"), ("heartbeat_at", True),
             ("heartbeat_at", NOW + 31), ("heartbeat_at", 10 ** 1000))
    for key, changed in cases:
        value = payload()
        value["observer"][key] = changed
        assert check(value)[0]["state"] == "invalid"
    value = payload()
    del value["observer"]["sender_heartbeat_at"]
    assert check(value)[0]["incident_key"].endswith(":invalid_schema")


def test_large_declared_body_is_rejected_without_consuming_stream():
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("oversized body must not be read")

    response = httpx.Response(200, headers={"content-length": str(MAX_HEALTH_BYTES + 1)}, stream=Unreadable())
    result, _ = check(response=response)
    assert result["state"] == "invalid" and result["incident_key"].endswith(":body_too_large")


def test_streaming_body_cap_stops_before_unbounded_tail():
    consumed = []

    class TooLarge(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(10):
                consumed.append(index)
                yield b"x" * 8192

    result, _ = check(response=httpx.Response(200, stream=TooLarge()))
    assert result["state"] == "invalid" and result["incident_key"].endswith(":body_too_large")
    assert len(consumed) == 9


def test_redirect_is_not_followed_or_disclosed():
    result, requests = check(response=httpx.Response(302, headers={"location": "https://private.example/SECRET"}))
    assert result["state"] == "invalid" and result["incident_key"].endswith(":redirect")
    assert len(requests) == 1 and "SECRET" not in str(result)


def test_timeout_never_retries_or_leaks_exception_url():
    def timed_out(request):
        raise httpx.ReadTimeout("PRIVATE_TIMEOUT " + str(request.url), request=request)

    result, requests = check(handler=timed_out)
    assert result["state"] == "unavailable" and result["incident_key"].endswith(":timeout")
    assert len(requests) == 1 and "PRIVATE_TIMEOUT" not in str(result)


def test_http_error_and_connection_failure_are_bounded_and_sanitized():
    result, requests = check(response=httpx.Response(503, text="PRIVATE_ERROR_BODY"))
    assert result["incident_key"].endswith(":http_503") and len(requests) == 1
    assert "PRIVATE_ERROR_BODY" not in str(result)

    def failed(request):
        raise httpx.ConnectError("PRIVATE_CONNECTION " + str(request.url), request=request)

    result, requests = check(handler=failed)
    assert result["incident_key"].endswith(":network_error") and len(requests) == 1
    assert "PRIVATE_CONNECTION" not in str(result)


def test_endpoint_credentials_query_fragment_and_invalid_options_rejected_before_get():
    def unexpected(request):
        pytest.fail("invalid endpoint must not be requested")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        for endpoint in ("ftp://host/health", "https://user:secret@host/health", "https://host/health?key=secret",
                         "https://host/health#secret", "https://host/health?", "https://host:0/health",
                         "https://host\\other/health", "https://host/health\n"):
            with pytest.raises(AgentError, match="MONITOR_WATCHDOG_ENDPOINT_INVALID"):
                Watchdog(client, endpoint, expected_instance=INSTANCE)
        with pytest.raises(AgentError, match="MONITOR_WATCHDOG_INSTANCE_INVALID"):
            Watchdog(client, ENDPOINT, expected_instance="../wrong")
        with pytest.raises(AgentError, match="MONITOR_WATCHDOG_AGE_INVALID"):
            Watchdog(client, ENDPOINT, expected_instance=INSTANCE, max_age_seconds=True)


def test_identity_encoding_is_required_before_reading_compressed_body():
    result, _ = check(response=httpx.Response(200, headers={"content-encoding": "br"},
        stream=httpx.ByteStream(b"compressed data")))
    assert result["state"] == "invalid" and result["incident_key"].endswith(":invalid_body")


def test_slow_stream_is_stopped_by_total_deadline(monkeypatch):
    moments = iter((100.0, 100.0, 111.0))
    monkeypatch.setattr(monitor_watchdog.time, "monotonic", lambda: next(moments))
    response = httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload()).encode()))
    result, requests = check(response=response)
    assert result["incident_key"].endswith(":timeout") and len(requests) == 1


def test_change_reporting_is_quiet_when_healthy_and_deduplicates_incident_and_recovery(tmp_path):
    store = MonitorStore(tmp_path)
    queued = []

    def queue(*args):
        queued.append(args)
    healthy = check()[0]
    stale_value = payload()
    stale_value["observer"]["heartbeat_at"] = NOW - 181
    stale = check(stale_value)[0]
    assert report_changes(store, healthy, queue) == {"changed": True, "queued": False, "state": "healthy"}
    assert report_changes(store, healthy, queue)["changed"] is False
    assert queued == []
    assert report_changes(store, stale, queue)["queued"] is True
    assert report_changes(store, {**stale, "observed_at": NOW + 30}, queue)["changed"] is False
    assert report_changes(store, lambda: healthy, queue)["queued"] is True
    assert len(queued) == 2 and queued[0][0] != queued[1][0]
    assert "Связь восстановлена" in queued[1][2]


def test_first_failure_is_reported_and_queue_crash_uses_same_durable_event_key(tmp_path, monkeypatch):
    store = MonitorStore(tmp_path)
    failed = check(response=httpx.Response(503))[0]

    def queue(key, kind, text):
        return store.enqueue(key, kind, text, "fixture-config", "-999", at=NOW, expires_at=NOW + 600)

    save_meta = store.set_meta

    def interrupted(*args):
        raise OSError("PRIVATE_STATE_FAILURE")

    monkeypatch.setattr(store, "set_meta", interrupted)
    with pytest.raises(OSError):
        report_changes(store, failed, queue)
    first = store.recent()[0]["id"]
    monkeypatch.setattr(store, "set_meta", save_meta)
    assert report_changes(store, failed, queue)["queued"] is True
    assert len(store.recent()) == 1 and store.recent()[0]["id"] == first


def test_notification_ignores_untrusted_result_message_and_rejects_unknown_keys(tmp_path):
    store = MonitorStore(tmp_path)
    result = check(response=httpx.Response(503))[0]
    result["message"] = "PRIVATE_INJECTED_TEXT"
    messages = []
    report_changes(store, result, lambda key, kind, text: messages.append(text))
    assert messages == ["Сервер мониторинга вернул HTTP 503."]
    malformed = copy.deepcopy(result)
    malformed["incident_key"] = "watchdog:" + INSTANCE + ":injected_unknown"
    with pytest.raises(AgentError, match="MONITOR_WATCHDOG_RESULT_INVALID"):
        report_changes(store, malformed, lambda *args: pytest.fail("bad result queued"))


def test_invalid_local_clock_does_not_request_health_endpoint():
    def unexpected(request):
        pytest.fail("invalid local clock must fail before request")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        result = Watchdog(client, ENDPOINT, expected_instance=INSTANCE, clock=lambda: float("nan")).check()
    assert result["state"] == "invalid" and result["observed_at"] == 0


def test_malformed_result_types_are_rejected_without_notification(tmp_path):
    store = MonitorStore(tmp_path)
    result = {"state": [], "incident_key": "watchdog:" + INSTANCE + ":invalid_body"}
    with pytest.raises(AgentError, match="MONITOR_WATCHDOG_RESULT_INVALID"):
        report_changes(store, result, lambda *args: pytest.fail("malformed result queued"))


def test_corrupted_dedup_state_does_not_reset_history_or_send_new_alert(tmp_path):
    store = MonitorStore(tmp_path)
    key = "watchdog:" + fingerprint("watchdog:" + INSTANCE)[:24]
    store.set_meta(key, {"state": [], "incident_key": "watchdog:" + INSTANCE + ":healthy", "generation": 0})
    with pytest.raises(AgentError, match="MONITOR_WATCHDOG_STATE_INVALID"):
        report_changes(store, check()[0], lambda *args: pytest.fail("corrupt state reset"))
