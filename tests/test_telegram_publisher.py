"""HTTP-контракт Telegram проверяется без внешних запросов и без live credentials."""

import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from PIL import Image

from max_postiz_agent.config import AgentError
from max_postiz_agent.telegram_publisher import TelegramPublisher


TOKEN = "123456:isolated_fixture_token_1234567890"
DESTINATION = "-1001234567890"


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)


def snapshot(title="Новая звезда", body="Полный текст. Источник: https://example.test/story"):
    return {"title": title, "body": body, "media": [], "destination": DESTINATION}


def image_snapshot(tmp_path, *, format="JPEG", size=(640, 480)):
    output = io.BytesIO()
    Image.new("RGB", size, "navy").save(output, format=format)
    raw = output.getvalue()
    filename = "photo.jpg" if format == "JPEG" else "photo.png"
    (tmp_path / filename).write_bytes(raw)
    value = snapshot()
    value["media"] = [{"path": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                       "mime": "image/jpeg" if format == "JPEG" else "image/png"}]
    return value, raw


def receipt(value, *, chat=None, message_id=51):
    text = value["body"]
    if text != value["title"] and not text.startswith(value["title"] + "\n"):
        text = value["title"] + "\n\n" + text
    message = {"message_id": message_id,
               "chat": chat or {"id": int(DESTINATION), "type": "channel", "username": "science_test"}}
    text_field, entity_field = ("caption", "caption_entities") if value.get("media") else ("text", "entities")
    message[text_field] = text
    message[entity_field] = [{"type": "bold", "offset": 0,
                              "length": len(value["title"].encode("utf-16-le")) // 2}]
    if value.get("media"):
        message["photo"] = [{"file_id": "fixture_photo", "width": 640, "height": 480}]
    return message


def publisher(tmp_path, value, *, response=None, status=200, handler=None, options=None):
    calls = []

    def send(request):
        calls.append(request)
        assert request.url.host == "api.telegram.org"
        assert request.url.scheme == "https"
        if handler:
            return handler(request)
        result = response if response is not None else {"ok": True, "result": receipt(value)}
        return httpx.Response(status, json=result)

    adapter = TelegramPublisher(tmp_path, options or {}, transport=httpx.MockTransport(send))
    return adapter, calls


def multipart(request):
    raw = ("Content-Type: " + request.headers["content-type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
    message = BytesParser(policy=policy.default).parsebytes(raw + request.content)
    return {part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()}


def test_text_single_request_exact_payload_and_honest_receipt(tmp_path):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    try:
        delivery = adapter.publish(DESTINATION, value, "dispatch_1")
        assert len(calls) == 1 and calls[0].method == "POST"
        assert calls[0].url.path.endswith("/sendMessage")
        payload = json.loads(calls[0].content)
        assert payload == {"chat_id": DESTINATION, "text": receipt(value)["text"],
                           "entities": receipt(value)["entities"],
                           "link_preview_options": {"is_disabled": True}}
        assert "parse_mode" not in payload and "**" not in payload["text"]
        assert delivery.state == "confirmed" and delivery.external_id == "51"
        assert delivery.permalink == "https://t.me/science_test/51"
        assert delivery.verified is False
        assert delivery.evidence["verification"] == "api_receipt"
        assert delivery.evidence["fixture"] is True
        assert delivery.evidence["independent_readback"] is False
        assert delivery.evidence["live_verified"] is False
        assert TOKEN not in repr(delivery)
    finally:
        adapter.close()


@pytest.mark.parametrize("format", ["JPEG", "PNG"])
def test_photo_single_atomic_multipart_contains_checked_bytes(tmp_path, format):
    value, raw = image_snapshot(tmp_path, format=format)
    adapter, calls = publisher(tmp_path, value)
    with adapter.client:
        delivery = adapter.publish(DESTINATION, value, "dispatch-photo")
    assert len(calls) == 1 and calls[0].url.path.endswith("/sendPhoto")
    parts = multipart(calls[0])
    assert parts["photo"].get_payload(decode=True) == raw
    assert parts["photo"].get_content_type() == value["media"][0]["mime"]
    assert parts["caption"].get_payload(decode=True).decode() == receipt(value)["caption"]
    assert json.loads(parts["caption_entities"].get_payload(decode=True)) == receipt(value)["caption_entities"]
    assert parts["show_caption_above_media"].get_payload(decode=True) == b"false"
    assert "parse_mode" not in parts
    assert delivery.evidence["uploaded_photo_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("title,body", [
    ("Тема", "Тема\n\nОригинальное тело."),
    ("Тема", "Тема"),
    ("🔭 Космос", "Полное тело \U0001f680 и точная ссылка https://example.test/a?b=1&c=2"),
    ("Тема", "Строка 1.\n\nСтрока 2 **буквальные знаки** & <данные>."),
])
def test_exact_text_and_utf16_bold_title(tmp_path, title, body):
    value = snapshot(title, body)
    adapter, calls = publisher(tmp_path, value)
    with adapter.client:
        adapter.publish(DESTINATION, value, "dispatch")
    payload = json.loads(calls[0].content)
    assert payload["text"] == receipt(value)["text"]
    assert payload["entities"] == [{"type": "bold", "offset": 0,
                                   "length": len(title.encode("utf-16-le")) // 2}]


@pytest.mark.parametrize("photo,boundary", [(False, 4096), (True, 1024)])
def test_utf16_limit_boundary_and_rejection_before_http(tmp_path, photo, boundary):
    value = image_snapshot(tmp_path)[0] if photo else snapshot()
    value.update(title="🔭", body="🔭\n\n" + "а" * (boundary - 4))
    adapter, calls = publisher(tmp_path, value)
    with adapter.client:
        assert adapter.validate(DESTINATION, value)["utf16_length"] == boundary
        adapter.publish(DESTINATION, value, "exact-boundary")
        value["body"] += "🚀"
        error = "TELEGRAM_PHOTO_CAPTION_ATOMIC_UNSUPPORTED" if photo else "TELEGRAM_TEXT_TOO_LONG"
        with pytest.raises(AgentError, match=error):
            adapter.publish(DESTINATION, value, "too-long")
    assert len(calls) == 1


def test_validate_is_pure_and_requires_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    adapter, calls = publisher(tmp_path, snapshot())
    with adapter.client:
        result = adapter.validate(DESTINATION, snapshot())
    assert result["valid"] and result["verification"] == "preflight_only"
    assert calls == []


@pytest.mark.parametrize("change,error", [
    (lambda value: value.update(destination="-1009999999999"), "TELEGRAM_SNAPSHOT_DESTINATION_CHANGED"),
    (lambda value: value.update(title=""), "TELEGRAM_TEXT_INVALID"),
    (lambda value: value.update(title="Заголовок\nНовая строка"), "TELEGRAM_TEXT_INVALID"),
    (lambda value: value.update(body="\x00"), "TELEGRAM_TEXT_INVALID"),
    (lambda value: value.update(body="\ud800"), "TELEGRAM_SNAPSHOT_INVALID"),
    (lambda value: value.update(media=[{}, {}]), "TELEGRAM_SINGLE_PHOTO_REQUIRED"),
    (lambda value: value.update(media={}), "TELEGRAM_SINGLE_PHOTO_REQUIRED"),
])
def test_invalid_snapshot_never_calls_api(tmp_path, change, error):
    value = snapshot()
    change(value)
    adapter, calls = publisher(tmp_path, value)
    with adapter.client, pytest.raises(AgentError, match=error):
        adapter.publish(DESTINATION, value, "dispatch")
    assert calls == []


@pytest.mark.parametrize("destination", ["", "0", "@bad", "https://t.me/science_test", "1/../../getMe", 123, "-0123"])
def test_invalid_destination_never_calls_api(tmp_path, destination):
    adapter, calls = publisher(tmp_path, snapshot())
    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_DESTINATION_INVALID"):
        adapter.publish(destination, snapshot(), "dispatch")
    assert calls == []


@pytest.mark.parametrize("request_id", ["", "../../other", "request\nsecret", "x" * 129, None])
def test_request_id_validation_before_http(tmp_path, request_id):
    adapter, calls = publisher(tmp_path, snapshot())
    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_REQUEST_ID_INVALID"):
        adapter.publish(DESTINATION, snapshot(), request_id)
    assert calls == []


@pytest.mark.parametrize("case,error", [
    ("hash", "TELEGRAM_MEDIA_HASH_CHANGED"),
    ("outside", "TELEGRAM_MEDIA_OUTSIDE_PROJECT"),
    ("absolute", "TELEGRAM_MEDIA_INVALID"),
    ("mime", "TELEGRAM_PHOTO_FORMAT_INVALID"),
    ("video", "TELEGRAM_MEDIA_INVALID"),
    ("missing", "TELEGRAM_MEDIA_NOT_READY"),
    ("invalid_bytes", "TELEGRAM_PHOTO_FORMAT_INVALID"),
])
def test_media_confinement_hash_and_decoding_before_http(tmp_path, case, error):
    root = tmp_path / "project"
    root.mkdir()
    value, raw = image_snapshot(root)
    media = value["media"][0]
    if case == "hash":
        media["sha256"] = "0" * 64
    elif case == "outside":
        (tmp_path / "outside.jpg").write_bytes(raw)
        media["path"] = "../outside.jpg"
    elif case == "absolute":
        media["path"] = str(root / "photo.jpg")
    elif case == "mime":
        media["mime"] = "image/png"
    elif case == "video":
        media["mime"] = "video/mp4"
    elif case == "missing":
        media["path"] = "missing.jpg"
    elif case == "invalid_bytes":
        (root / "photo.jpg").write_bytes(b"not an image")
        media["sha256"] = hashlib.sha256(b"not an image").hexdigest()
    adapter, calls = publisher(root, value)
    with adapter.client, pytest.raises(AgentError, match=error):
        adapter.publish(DESTINATION, value, "dispatch")
    assert calls == []


@pytest.mark.parametrize("size", [(100, 5000), (5001, 5000)])
def test_photo_dimensions_guard(tmp_path, size):
    value, _ = image_snapshot(tmp_path, size=size)
    adapter, calls = publisher(tmp_path, value)
    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_PHOTO_FORMAT_INVALID"):
        adapter.publish(DESTINATION, value, "dispatch")
    assert calls == []


@pytest.mark.parametrize("status,payload,error", [
    (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 42}}, "TELEGRAM_RATE_LIMIT"),
    (403, {"ok": False, "error_code": 403}, "TELEGRAM_ACCESS_DENIED"),
    (401, {"ok": False, "error_code": 401}, "TELEGRAM_ACCESS_DENIED"),
    (400, {"ok": False, "error_code": 400}, "TELEGRAM_REJECTED"),
    (200, {"ok": False, "error_code": 429}, "TELEGRAM_RATE_LIMIT"),
    (500, {"ok": False, "error_code": 400}, "TELEGRAM_UNKNOWN"),
    (200, {"ok": False}, "TELEGRAM_UNKNOWN"),
    (200, {"ok": True, "result": []}, "TELEGRAM_UNKNOWN"),
    (200, {"ok": 1, "result": {}}, "TELEGRAM_UNKNOWN"),
])
def test_known_failures_and_unknown_never_retry(tmp_path, status, payload, error):
    adapter, calls = publisher(tmp_path, snapshot(), response=payload, status=status)
    with adapter.client:
        if error == "TELEGRAM_UNKNOWN":
            delivery = adapter.publish(DESTINATION, snapshot(), "dispatch")
            assert delivery.state == "unknown" and delivery.evidence["failure_code"] == error
            assert TOKEN not in repr(delivery)
        else:
            with pytest.raises(AgentError, match=error) as failure:
                adapter.publish(DESTINATION, snapshot(), "dispatch")
            assert TOKEN not in str(failure.value)
            assert failure.value.__suppress_context__ or failure.value.__context__ is None
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["timeout", "connection", "malformed", "redirect"])
def test_transport_and_ambiguous_response_never_retry_or_reveal_token(tmp_path, kind):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(str(request.url), request=request)
        if kind == "connection":
            raise httpx.ConnectError(str(request.url), request=request)
        if kind == "malformed":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(307, headers={"location": "https://other.invalid/stolen"})

    adapter, calls = publisher(tmp_path, snapshot(), handler=handler)
    with adapter.client:
        delivery = adapter.publish(DESTINATION, snapshot(), "dispatch")
        assert delivery.state == "unknown"
    assert len(calls) == 1 and TOKEN not in repr(delivery)


@pytest.mark.parametrize("change", [
    lambda result: result.update(message_id=0),
    lambda result: result.update(message_id=True),
    lambda result: result["chat"].update(id=-1009876543210),
    lambda result: result.update(text="изменённый текст"),
    lambda result: result.update(entities=[]),
    lambda result: result["entities"][0].update(length=1),
    lambda result: result["entities"].append({"type": "bold", "offset": 20, "length": 5}),
    lambda result: result["entities"].append({"type": "italic", "offset": 20, "length": 5}),
    lambda result: result.update(photo=[{"file_id": "unexpected"}]),
])
def test_exact_receipt_mismatches_are_unknown_not_failed(tmp_path, change):
    value = snapshot()
    result = receipt(value)
    change(result)
    adapter, calls = publisher(tmp_path, value, response={"ok": True, "result": result})
    with adapter.client:
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    lambda result: result.update(photo=[]),
    lambda result: result.update(caption="другой текст"),
    lambda result: result["photo"][0].update(width=0),
    lambda result: result.update(show_caption_above_media=True),
])
def test_photo_receipt_requires_caption_media_and_order(tmp_path, change):
    value, _ = image_snapshot(tmp_path)
    result = receipt(value)
    change(result)
    adapter, calls = publisher(tmp_path, value, response={"ok": True, "result": result})
    with adapter.client:
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
    assert len(calls) == 1


def test_telegram_auto_detected_url_entity_is_allowed(tmp_path):
    value = snapshot()
    result = receipt(value)
    result["entities"].append({"type": "url", "offset": 30, "length": 26})
    adapter, _ = publisher(tmp_path, value, response={"ok": True, "result": result})
    with adapter.client:
        assert adapter.publish(DESTINATION, value, "dispatch").state == "confirmed"


@pytest.mark.parametrize("returned_username,expected", [("SCIENCE_TEST", "confirmed"), ("other_channel", "unknown")])
def test_username_destination_compares_returned_chat(tmp_path, returned_username, expected):
    value = snapshot()
    value["destination"] = "@science_test"
    result = receipt(value)
    result["chat"]["username"] = returned_username
    adapter, _ = publisher(tmp_path, value, response={"ok": True, "result": result})
    with adapter.client:
        if expected == "unknown":
            assert adapter.publish("@science_test", value, "dispatch").state == "unknown"
        else:
            assert adapter.publish("@science_test", value, "dispatch").state == expected


@pytest.mark.parametrize("allowed", [True, False])
def test_inspect_is_explicit_read_only_and_never_delivery(tmp_path, allowed):
    calls = []
    responses = {
        "getMe": {"id": 123456, "is_bot": True},
        "getChat": {"id": int(DESTINATION), "type": "channel"},
        "getChatMember": {"user": {"id": 123456, "is_bot": True},
                          "status": "administrator", "can_post_messages": allowed},
    }

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True, "result": responses[request.url.path.rsplit("/", 1)[-1]]})

    adapter = TelegramPublisher(tmp_path, {}, httpx.MockTransport(handler))
    with adapter.client:
        result = adapter.inspect(DESTINATION)
    assert result["can_publish"] is allowed and result["verification"] == "access_only"
    assert result["live_verified"] is False and result["fixture"] is True
    assert len(calls) == 3
    assert calls[2].url.params["user_id"] == "123456"


def test_inspection_wrong_recipient_stops_before_membership(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        data = {"id": 123456, "is_bot": True} if len(calls) == 1 else {"id": -222, "type": "channel"}
        return httpx.Response(200, json={"ok": True, "result": data})

    adapter = TelegramPublisher(tmp_path, {}, httpx.MockTransport(handler))
    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_INSPECTION_INVALID"):
        adapter.inspect(DESTINATION)
    assert len(calls) == 2


def test_default_fake_mode_cannot_send_or_inspect(tmp_path):
    adapter = TelegramPublisher(tmp_path, {})
    with adapter.client:
        assert adapter.mode == "fake"
        assert adapter.validate(DESTINATION, snapshot())["valid"]
        with pytest.raises(AgentError, match="TELEGRAM_LIVE_MODE_REQUIRED"):
            adapter.publish(DESTINATION, snapshot(), "dispatch")
        with pytest.raises(AgentError, match="TELEGRAM_LIVE_MODE_REQUIRED"):
            adapter.inspect(DESTINATION)


@pytest.mark.parametrize("token_value,error", [(None, "TELEGRAM_TOKEN_REQUIRED"), ("bad/token?secret", "TELEGRAM_TOKEN_INVALID")])
def test_missing_or_invalid_credentials_block_before_http(tmp_path, monkeypatch, token_value, error):
    if token_value is None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    else:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token_value)
    adapter, calls = publisher(tmp_path, snapshot())
    with adapter.client, pytest.raises(AgentError, match=error):
        adapter.publish(DESTINATION, snapshot(), "dispatch")
    assert calls == []


def test_adapter_does_not_mutate_approved_snapshot(tmp_path):
    value, _ = image_snapshot(tmp_path)
    original = deepcopy(value)
    adapter, _ = publisher(tmp_path, value)
    with adapter.client:
        adapter.validate(DESTINATION, value)
        adapter.publish(DESTINATION, value, "dispatch")
    assert value == original


def split_publisher(tmp_path, value, *, fail_second=None):
    calls = []

    def handler(request):
        calls.append(request)
        journal = json.loads((tmp_path / "data/telegram-deliveries/split/journal.json").read_text(encoding="utf-8"))
        index = len(calls) - 1
        assert journal["steps"][index]["state"] == "claimed"
        if index:
            assert journal["steps"][0]["state"] == "confirmed"
            if fail_second == "timeout":
                raise httpx.ReadTimeout("isolated timeout", request=request)
            if fail_second == "rejected":
                return httpx.Response(400, json={"ok": False, "error_code": 400})
        result = receipt(value, message_id=100 + index)
        if not index:
            result.pop("caption")
            result.pop("caption_entities")
        else:
            result["text"] = result.pop("caption")
            result["entities"] = result.pop("caption_entities")
            result.pop("photo")
        return httpx.Response(200, json={"ok": True, "result": result})

    adapter = TelegramPublisher(tmp_path, {"media_layout": "auto"}, httpx.MockTransport(handler))
    return adapter, calls


def long_illustrated(tmp_path):
    value, _ = image_snapshot(tmp_path)
    value["body"] = "Полный научный текст. " * 80
    return value


def test_auto_layout_full_article_two_durable_exact_messages(tmp_path):
    value = long_illustrated(tmp_path)
    adapter, calls = split_publisher(tmp_path, value)
    with adapter.client:
        assert adapter.validate(DESTINATION, value)["message_count"] == 2
        delivery = adapter.publish(DESTINATION, value, "split")
    assert delivery.state == "confirmed" and delivery.verified is False
    assert delivery.evidence["message_ids"] == [100, 101]
    assert delivery.evidence["message_count"] == 2
    assert delivery.external_id == "101"
    assert [request.url.path.rsplit("/", 1)[-1] for request in calls] == ["sendPhoto", "sendMessage"]
    assert "caption" not in multipart(calls[0])
    assert json.loads(calls[1].content)["text"] == value["title"] + "\n\n" + value["body"]
    raw = (tmp_path / "data/telegram-deliveries/split/journal.json").read_text(encoding="utf-8")
    journal = json.loads(raw)
    assert journal["state"] == "done" and TOKEN not in raw
    assert [item["state"] for item in journal["steps"]] == ["confirmed", "confirmed"]


@pytest.mark.parametrize("failure", ["timeout", "rejected"])
def test_partial_delivery_preserves_first_id_and_never_continues(tmp_path, failure):
    value = long_illustrated(tmp_path)
    adapter, calls = split_publisher(tmp_path, value, fail_second=failure)
    with adapter.client:
        result = adapter.publish(DESTINATION, value, "split")
        repeated = adapter.publish(DESTINATION, value, "split")
        reconciled = adapter.reconcile(DESTINATION, value, "split")
    assert result.state == repeated.state == reconciled.state == "unknown"
    assert result.evidence["message_ids"] == repeated.evidence["message_ids"] == [100]
    assert len(calls) == 2


def test_completed_request_reenters_across_instances_without_http_or_media_read(tmp_path):
    value = long_illustrated(tmp_path)
    adapter, calls = split_publisher(tmp_path, value)
    with adapter.client:
        first = adapter.publish(DESTINATION, value, "split")
    (tmp_path / value["media"][0]["path"]).unlink()

    def forbidden(request):
        raise AssertionError("replay must not contact Telegram")

    second = TelegramPublisher(tmp_path, {"media_layout": "auto"}, httpx.MockTransport(forbidden))
    with second.client:
        repeated = second.publish(DESTINATION, value, "split")
        reconciled = second.reconcile(DESTINATION, value, "split")
    assert first == repeated == reconciled
    assert len(calls) == 2


def test_changed_exact_snapshot_cannot_reuse_completed_request_id(tmp_path):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    with adapter.client:
        adapter.publish(DESTINATION, value, "dispatch")
        changed = {**value, "body": value["body"] + " новое"}
        with pytest.raises(AgentError, match="TELEGRAM_REQUEST_BINDING_CHANGED"):
            adapter.publish(DESTINATION, changed, "dispatch")
        with pytest.raises(AgentError, match="TELEGRAM_REQUEST_BINDING_CHANGED"):
            adapter.reconcile(DESTINATION, changed, "dispatch")
    assert len(calls) == 1


def test_crash_after_http_before_receipt_never_repeats_claimed_step(tmp_path, monkeypatch):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    send = adapter._send_step

    def crash(prepared):
        send(prepared)
        raise RuntimeError("simulated process crash before receipt persistence")

    monkeypatch.setattr(adapter, "_send_step", crash)
    with adapter.client:
        with pytest.raises(RuntimeError, match="simulated process crash"):
            adapter.publish(DESTINATION, value, "dispatch")
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
        assert adapter.reconcile(DESTINATION, value, "dispatch").state == "unknown"
    assert len(calls) == 1
    journal = json.loads((tmp_path / "data/telegram-deliveries/dispatch/journal.json").read_text(encoding="utf-8"))
    assert journal["steps"][0]["state"] == "claimed"


def test_journal_write_failure_before_second_http_keeps_partial_unknown(tmp_path, monkeypatch):
    value = long_illustrated(tmp_path)
    adapter, calls = split_publisher(tmp_path, value)
    write = adapter._write_journal
    writes = []

    def fail_second_claim(path, journal):
        writes.append(deepcopy(journal))
        if len(writes) == 4:
            raise AgentError("TELEGRAM_JOURNAL_UNAVAILABLE", 503)
        write(path, journal)

    monkeypatch.setattr(adapter, "_write_journal", fail_second_claim)
    with adapter.client:
        result = adapter.publish(DESTINATION, value, "split")
        repeated = adapter.publish(DESTINATION, value, "split")
    assert result.state == repeated.state == "unknown"
    assert result.evidence["message_ids"] == [100] and len(calls) == 1


@pytest.mark.parametrize("corrupt", ["missing", "invalid_json", "wrong_receipt", "unfinished"])
def test_missing_or_corrupt_journal_never_restarts(tmp_path, corrupt):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    with adapter.client:
        adapter.publish(DESTINATION, value, "dispatch")
        path = tmp_path / "data/telegram-deliveries/dispatch/journal.json"
        if corrupt == "missing":
            path.unlink()
        elif corrupt == "invalid_json":
            path.write_text("{incomplete", encoding="utf-8")
        else:
            journal = json.loads(path.read_text(encoding="utf-8"))
            if corrupt == "wrong_receipt":
                journal["steps"][0]["receipt"]["text"] += "changed"
            else:
                journal["state"] = "sending"
            path.write_text(json.dumps(journal), encoding="utf-8")
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
    assert len(calls) == 1


def test_reconcile_absent_request_is_read_only_and_does_not_create_directories(tmp_path):
    adapter, calls = publisher(tmp_path, snapshot())
    with adapter.client:
        assert adapter.reconcile(DESTINATION, snapshot(), "absent").state == "unknown"
    assert not (tmp_path / "data").exists() and calls == []


def test_short_auto_layout_remains_single_photo_message(tmp_path):
    value, _ = image_snapshot(tmp_path)
    adapter, calls = publisher(tmp_path, value, options={"media_layout": "auto"})
    with adapter.client:
        assert adapter.validate(DESTINATION, value)["message_count"] == 1
        assert adapter.publish(DESTINATION, value, "dispatch").state == "confirmed"
    assert len(calls) == 1


def test_even_auto_layout_rejects_article_over_4096_before_http(tmp_path):
    value, _ = image_snapshot(tmp_path)
    value["body"] = "а" * 4097
    adapter, calls = publisher(tmp_path, value, options={"media_layout": "auto"})
    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_TEXT_TOO_LONG"):
        adapter.publish(DESTINATION, value, "dispatch")
    assert not (tmp_path / "data").exists() and calls == []


def test_concurrent_same_request_does_not_send_twice(tmp_path):
    value = snapshot()
    started, release = threading.Event(), threading.Event()

    def handler(request):
        started.set()
        assert release.wait(5), "test did not release sender"
        return httpx.Response(200, json={"ok": True, "result": receipt(value)})

    adapter, calls = publisher(tmp_path, value, handler=handler)
    with adapter.client, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(adapter.publish, DESTINATION, value, "dispatch")
        try:
            assert started.wait(5)
            second = executor.submit(adapter.publish, DESTINATION, value, "dispatch")
            assert second.result(timeout=5).state == "unknown"
        finally:
            release.set()
        assert first.result(timeout=5).state == "confirmed"
    assert len(calls) == 1


def test_initial_journal_failure_claims_id_without_any_http(tmp_path, monkeypatch):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)

    def unavailable(path, journal):
        raise AgentError("TELEGRAM_JOURNAL_UNAVAILABLE", 503)

    monkeypatch.setattr(adapter, "_write_journal", unavailable)
    with adapter.client:
        with pytest.raises(AgentError, match="TELEGRAM_JOURNAL_UNAVAILABLE"):
            adapter.publish(DESTINATION, value, "dispatch")
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
    assert calls == []


def test_known_rejected_request_id_is_never_automatically_retried(tmp_path):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value, status=400, response={"ok": False, "error_code": 400})
    with adapter.client:
        with pytest.raises(AgentError, match="TELEGRAM_REJECTED"):
            adapter.publish(DESTINATION, value, "dispatch")
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
    assert len(calls) == 1


def test_explicit_photo_then_text_layout_uses_two_messages_for_short_article(tmp_path):
    value, _ = image_snapshot(tmp_path)
    adapter, calls = split_publisher(tmp_path, value)
    adapter._layout = "photo_then_text"
    with adapter.client:
        assert adapter.validate(DESTINATION, value)["message_count"] == 2
        assert adapter.publish(DESTINATION, value, "split").state == "confirmed"
    assert len(calls) == 2


def test_changed_layout_cannot_replan_existing_request(tmp_path):
    value, _ = image_snapshot(tmp_path)
    adapter, calls = publisher(tmp_path, value)
    with adapter.client:
        adapter.publish(DESTINATION, value, "dispatch")
        adapter._layout = "auto"
        with pytest.raises(AgentError, match="TELEGRAM_REQUEST_BINDING_CHANGED"):
            adapter.publish(DESTINATION, value, "dispatch")
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    lambda result: result["entities"][0].update(offset=False),
    lambda result: result["entities"][0].update(length=11.0),
    lambda result: result["entities"].append({"type": "url", "offset": 0, "length": 100000}),
    lambda result: result["entities"].append({"type": "unsupported_entity", "offset": 0, "length": 1}),
])
def test_malformed_receipt_entities_are_unknown(tmp_path, change):
    value = snapshot()
    result = receipt(value)
    change(result)
    adapter, calls = publisher(tmp_path, value, response={"ok": True, "result": result})
    with adapter.client:
        assert adapter.publish(DESTINATION, value, "dispatch").state == "unknown"
    assert len(calls) == 1


def test_duplicate_api_json_keys_are_unknown(tmp_path):
    adapter, calls = publisher(tmp_path, snapshot(), handler=lambda request:
        httpx.Response(200, content=b'{"ok":false,"ok":true,"result":{}}'))
    with adapter.client:
        assert adapter.publish(DESTINATION, snapshot(), "dispatch").state == "unknown"
    assert len(calls) == 1


def test_reconcile_refuses_corrupt_photo_receipt_without_reading_image(tmp_path):
    value = long_illustrated(tmp_path)
    adapter, calls = split_publisher(tmp_path, value)
    with adapter.client:
        adapter.publish(DESTINATION, value, "split")
        path = tmp_path / "data/telegram-deliveries/split/journal.json"
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal["steps"][0]["receipt"] = []
        path.write_text(json.dumps(journal), encoding="utf-8")
        assert adapter.reconcile(DESTINATION, value, "split").state == "unknown"
    assert len(calls) == 2


def test_photograph_is_never_retried_when_text_reply_reuses_photo_message_id(tmp_path):
    value = long_illustrated(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        result = receipt(value, message_id=100)
        if len(calls) == 1:
            result.pop("caption")
            result.pop("caption_entities")
        else:
            result["text"] = result.pop("caption")
            result["entities"] = result.pop("caption_entities")
            result.pop("photo")
        return httpx.Response(200, json={"ok": True, "result": result})

    adapter = TelegramPublisher(tmp_path, {"media_layout": "auto"}, httpx.MockTransport(handler))
    with adapter.client:
        result = adapter.publish(DESTINATION, value, "split")
        assert result.state == "unknown" and result.evidence["message_ids"] == [100]
        assert adapter.publish(DESTINATION, value, "split").state == "unknown"
    assert len(calls) == 2


def test_authorization_surrounds_initial_claim_and_step_fsync(tmp_path):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    observed = []
    path = tmp_path / "data/telegram-deliveries/dispatch/journal.json"

    def authorize():
        journal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        observed.append(journal["steps"][0]["state"] if journal else "before_initial_claim")
        assert calls == []

    with adapter.client:
        assert adapter.publish(DESTINATION, value, "dispatch", authorize=authorize).state == "confirmed"
    assert observed == ["before_initial_claim", "pending", "claimed"] and len(calls) == 1


def test_initial_authorization_rejection_has_no_claim_or_http(tmp_path):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)

    def reject():
        raise AgentError("APPROVAL_STALE", 409)

    with adapter.client, pytest.raises(AgentError, match="APPROVAL_STALE") as error:
        adapter.publish(DESTINATION, value, "dispatch", authorize=reject)
    assert error.value.status == 409 and calls == []
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("rejected_call", [2, 3])
def test_authorization_rejection_before_first_http_preserves_known_error_and_blocks_replay(tmp_path, rejected_call):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    checks = []

    def authorize():
        checks.append(True)
        if len(checks) == rejected_call:
            raise AgentError("TARGET_PAUSED", 409)

    with adapter.client:
        with pytest.raises(AgentError, match="TARGET_PAUSED") as error:
            adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
        assert error.value.status == 409
        repeated = adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
        assert repeated.state == "unknown" and repeated.evidence["message_ids"] == []
    assert calls == [] and len(checks) == rejected_call
    journal = json.loads((tmp_path / "data/telegram-deliveries/dispatch/journal.json").read_text(encoding="utf-8"))
    assert journal["failure_code"] == "TARGET_PAUSED" and journal["steps"][0]["state"] == "failed"


@pytest.mark.parametrize("code", ["TARGET_PAUSED", "APPROVAL_STALE", "ROUTE_SEND_LEASE_EXPIRED"])
def test_authorization_change_during_claim_fsync_prevents_http(tmp_path, monkeypatch, code):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)
    write = adapter._write_journal
    revoked = []

    def persist_then_revoke(path, journal):
        write(path, journal)
        if journal["steps"][0]["state"] == "claimed":
            revoked.append(True)

    def authorize():
        if revoked:
            raise AgentError(code, 409)

    monkeypatch.setattr(adapter, "_write_journal", persist_then_revoke)
    with adapter.client:
        with pytest.raises(AgentError, match=code):
            adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
        assert adapter.publish(DESTINATION, value, "dispatch", authorize=authorize).state == "unknown"
    assert calls == [] and revoked == [True]


def test_revocation_during_photo_receipt_stops_text_and_preserves_first_id(tmp_path):
    value = long_illustrated(tmp_path)
    revoked, checks = [], []

    def handler(request):
        assert request.url.path.endswith("/sendPhoto")
        result = receipt(value, message_id=301)
        result.pop("caption")
        result.pop("caption_entities")
        revoked.append(True)
        return httpx.Response(200, json={"ok": True, "result": result})

    def authorize():
        checks.append(True)
        if revoked:
            raise AgentError("APPROVAL_STALE", 409)

    adapter, calls = publisher(tmp_path, value, handler=handler, options={"media_layout": "auto"})
    with adapter.client:
        delivery = adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
        repeated = adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
        reconciled = adapter.reconcile(DESTINATION, value, "dispatch")
    assert delivery.state == repeated.state == reconciled.state == "unknown"
    assert delivery.evidence["message_ids"] == repeated.evidence["message_ids"] == [301]
    assert delivery.evidence["failure_code"] == "APPROVAL_STALE"
    assert delivery.external_id == "301" and len(calls) == 1 and len(checks) == 4


def test_revocation_after_second_claim_never_sends_text_or_repeats_photo(tmp_path, monkeypatch):
    value = long_illustrated(tmp_path)
    adapter, calls = split_publisher(tmp_path, value)
    write = adapter._write_journal
    revoked = []

    def persist_then_revoke(path, journal):
        write(path, journal)
        if journal["steps"][1]["state"] == "claimed":
            assert journal["steps"][0]["state"] == "confirmed"
            revoked.append(True)

    def authorize():
        if revoked:
            raise AgentError("ROUTE_SEND_LEASE_EXPIRED", 409)

    monkeypatch.setattr(adapter, "_write_journal", persist_then_revoke)
    with adapter.client:
        delivery = adapter.publish(DESTINATION, value, "split", authorize=authorize)
        repeated = adapter.publish(DESTINATION, value, "split", authorize=authorize)
    assert delivery.state == repeated.state == "unknown"
    assert delivery.evidence["message_ids"] == repeated.evidence["message_ids"] == [100]
    assert delivery.evidence["failure_code"] == "ROUTE_SEND_LEASE_EXPIRED"
    assert revoked == [True] and len(calls) == 1


def test_completed_replay_and_reconcile_do_not_invoke_mutation_authorizer(tmp_path):
    value = snapshot()
    adapter, calls = publisher(tmp_path, value)

    def forbidden():
        raise AssertionError("read-only replay must not authorize a new mutation")

    with adapter.client:
        completed = adapter.publish(DESTINATION, value, "dispatch")
        assert adapter.publish(DESTINATION, value, "dispatch", authorize=forbidden) == completed
        assert adapter.reconcile(DESTINATION, value, "dispatch") == completed
    assert len(calls) == 1


def test_invalid_authorizer_fails_before_any_mutation(tmp_path):
    adapter, calls = publisher(tmp_path, snapshot())
    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_AUTHORIZER_INVALID"):
        adapter.publish(DESTINATION, snapshot(), "dispatch", authorize=False)
    assert not (tmp_path / "data").exists() and calls == []


def test_authorization_infrastructure_failure_after_photo_keeps_partial_receipt(tmp_path):
    value = long_illustrated(tmp_path)
    received_photo = []
    private_error = "private database connection details must remain undisclosed"

    def handler(request):
        assert request.url.path.endswith("/sendPhoto")
        result = receipt(value, message_id=401)
        result.pop("caption")
        result.pop("caption_entities")
        received_photo.append(True)
        return httpx.Response(200, json={"ok": True, "result": result})

    def authorize():
        if received_photo:
            raise RuntimeError(private_error)

    adapter, calls = publisher(tmp_path, value, handler=handler, options={"media_layout": "auto"})
    with adapter.client:
        result = adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
        repeated = adapter.publish(DESTINATION, value, "dispatch", authorize=authorize)
    assert result.state == repeated.state == "unknown" and result.evidence["message_ids"] == [401]
    assert result.evidence["failure_code"] == "TELEGRAM_AUTHORIZATION_UNAVAILABLE"
    assert private_error not in repr(result) and len(calls) == 1


def test_initial_authorization_infrastructure_failure_is_redacted_and_sends_nothing(tmp_path):
    adapter, calls = publisher(tmp_path, snapshot())

    def authorize():
        raise RuntimeError("private database connection details")

    with adapter.client, pytest.raises(AgentError, match="TELEGRAM_AUTHORIZATION_UNAVAILABLE") as error:
        adapter.publish(DESTINATION, snapshot(), "dispatch", authorize=authorize)
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert not (tmp_path / "data").exists() and calls == []


@pytest.mark.parametrize("proxy", ["http://127.0.0.1:10809", "http://localhost:10809/", "http://[::1]:10809"])
def test_explicit_loopback_proxy_reaches_real_client_without_ambient_trust(tmp_path, monkeypatch, proxy):
    monkeypatch.setenv("TELEGRAM_PROXY_URL", proxy)
    monkeypatch.setenv("HTTPS_PROXY", "http://outside.example.test:8080")
    constructor = httpx.Client
    captured = []

    def client(**kwargs):
        captured.append(kwargs)
        return constructor(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    adapter = TelegramPublisher(tmp_path, {"adapter_mode": "live", "proxy_env": "TELEGRAM_PROXY_URL"})
    try:
        assert adapter.mode == "telegram_api" and adapter._fixture is False
        assert captured[0]["proxy"] == proxy.rstrip("/")
        assert captured[0]["trust_env"] is False and captured[0]["follow_redirects"] is False
        assert captured[0]["verify"] is True and captured[0]["transport"] is None
    finally:
        adapter.close()


def test_ambient_proxy_and_unselected_dedicated_env_do_not_enable_proxy(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://outside.example.test:8080")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "http://127.0.0.1:10809")
    constructor = httpx.Client
    captured = []

    def client(**kwargs):
        captured.append(kwargs)
        return constructor(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    adapter = TelegramPublisher(tmp_path, {"adapter_mode": "live"})
    try:
        assert captured[0]["proxy"] is None and captured[0]["trust_env"] is False
        assert adapter._proxy_url is None
    finally:
        adapter.close()


def test_explicit_proxy_cannot_override_injected_fixture_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "http://127.0.0.1:10809")

    def forbidden_mount(*_args, **_kwargs):
        pytest.fail("proxy mount would bypass MockTransport and contact a live service")

    monkeypatch.setattr(httpx.Client, "_init_proxy_transport", forbidden_mount)
    value = snapshot()
    adapter, calls = publisher(tmp_path, value, options={"proxy_env": "TELEGRAM_PROXY_URL"})
    with adapter.client:
        result = adapter.publish(DESTINATION, value, "fixture-proxy")
    assert result.state == "confirmed" and result.verified is False
    assert result.evidence["fixture"] is True and len(calls) == 1
    assert adapter._proxy_url == "http://127.0.0.1:10809"


@pytest.mark.parametrize("proxy", [
    "https://127.0.0.1:10809", "socks5://127.0.0.1:10808", "http://outside.example.test:8080",
    "http://127.0.0.1.evil.example.test:10809", "http://0.0.0.0:10809", "http://127.1:10809",
    "http://user:private-password@127.0.0.1:10809", "http://@127.0.0.1:10809",
    "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
    "http://127.0.0.1:10809/path", "http://127.0.0.1:10809/?token=private",
    "http://127.0.0.1:10809/#private", " http://127.0.0.1:10809", "http://127.0.0.1:10809\n",
    "http://[2001:db8::1]:10809", "http://[invalid:10809", "not-a-url",
])
def test_unsafe_proxy_urls_fail_closed_without_echoing_url_or_credentials(tmp_path, monkeypatch, proxy):
    monkeypatch.setenv("TELEGRAM_PROXY_URL", proxy)
    with pytest.raises(AgentError, match="^TELEGRAM_PROXY_INVALID$") as failure:
        TelegramPublisher(tmp_path, {"proxy_env": "TELEGRAM_PROXY_URL"})
    assert failure.value.__cause__ is None and failure.value.__suppress_context__
    assert proxy not in str(failure.value) and "private-password" not in str(failure.value)


@pytest.mark.parametrize("env", [None, "", "lowercase", "1PROXY", "PROXY-NAME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"])
def test_proxy_requires_valid_dedicated_env_name(tmp_path, env):
    with pytest.raises(AgentError, match="^TELEGRAM_PROXY_ENV_INVALID$"):
        TelegramPublisher(tmp_path, {"proxy_env": env})


def test_explicit_missing_proxy_never_silently_falls_back_to_direct(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_PROXY_URL", raising=False)
    with pytest.raises(AgentError, match="^TELEGRAM_PROXY_REQUIRED$"):
        TelegramPublisher(tmp_path, {"proxy_env": "TELEGRAM_PROXY_URL"})


@pytest.mark.parametrize("body,expected", [
    ("Марсианская станция наблюдает небо.", "Марс\n\nМарсианская станция наблюдает небо."),
    ("Марс\n\nСтанция наблюдает небо.", "Марс\n\nСтанция наблюдает небо."),
    ("Марс\nСтанция наблюдает небо.", "Марс\nСтанция наблюдает небо."),
    ("Марс", "Марс"),
])
def test_heading_prefix_must_be_a_complete_heading_before_bold(tmp_path, body, expected):
    value = snapshot(title="Марс", body=body)

    def send(request):
        payload = json.loads(request.content)
        assert payload["text"] == expected
        assert payload["entities"] == [{"type": "bold", "offset": 0, "length": 4}]
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 42, "chat": {"id": int(DESTINATION), "type": "channel"},
            "text": expected, "entities": [{"type": "bold", "offset": 0, "length": 4}]}})

    adapter, calls = publisher(tmp_path, value, handler=send)
    with adapter.client:
        result = adapter.publish(DESTINATION, value, "heading-boundary")
    assert result.state == "confirmed" and result.verified is False and len(calls) == 1
