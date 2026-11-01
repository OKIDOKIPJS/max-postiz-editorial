"""Контракты VK проверяются MockTransport; это не живая доставка."""

import copy
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image

from max_postiz_agent.config import AgentError
from max_postiz_agent.vk_publisher import API_BASE, VKPublisher, _guid


DESTINATION = "-123456"
REQUEST_ID = "dispatch-123"


def snapshot(media=None):
    return {"title": "Звёзды", "body": "Звёзды\n\nПолный текст и источник https://example.org/a",
            "media": media or [], "platform": "vk", "destination": DESTINATION}


def wall(text=None, attachments=None, **changes):
    return {"id": 77, "owner_id": -123456, "from_id": -123456, "post_type": "post",
            "text": text if text is not None else snapshot()["body"],
            "attachments": attachments or [], **changes}


def exact_response(post=None):
    return {"response": {"items": [post if post is not None else wall()]}}


@pytest.fixture
def publishers(tmp_path, monkeypatch):
    monkeypatch.setenv("VK_TEST_TOKEN", "fixture-secret-DO-NOT-EXPOSE")
    opened = []

    def create(script, *, root=None, options=None):
        seen, pending = [], list(script)

        def handler(request):
            seen.append(request)
            assert pending, "Unexpected extra API call"
            expected, reply = pending.pop(0)
            assert str(request.url).startswith(API_BASE + expected) if expected != "upload" \
                else request.url.host == "pu.vk.com"
            if callable(reply):
                reply = reply(request)
            if isinstance(reply, Exception):
                raise reply
            return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

        publisher = VKPublisher(root or tmp_path, {"adapter_mode": "live",
            "token_env": "VK_TEST_TOKEN", **(options or {})}, httpx.MockTransport(handler))
        opened.append(publisher)
        return publisher, seen, pending

    yield create
    for publisher in opened:
        publisher.close()


def form(request):
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def image_media(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(stream, "PNG")
    raw = stream.getvalue()
    path = tmp_path / "image.png"
    path.write_bytes(raw)
    return {"path": "image.png", "mime": "image/png",
            "sha256": hashlib.sha256(raw).hexdigest()}, raw, path


def image_script(post=None):
    attachment = {"type": "photo", "photo": {"owner_id": -123456, "id": 88}}
    return [("photos.getWallUploadServer", {"response": {
                "upload_url": "https://pu.vk.com/c1/upload.php?act=do_add&hash=server-signature"}}),
            ("upload", {"server": 55, "photo": '[{"photo":"opaque-upload-data"}]',
                        "hash": "upload-signature"}),
            ("photos.saveWallPhoto", {"response": [{"owner_id": -123456, "id": 88}]}),
            ("wall.post", {"response": {"post_id": 77}}),
            ("wall.getById", exact_response(post or wall(attachments=[attachment])))]


def test_text_post_preserves_content_and_requires_independent_exact_readback(publishers):
    publisher, seen, pending = publishers([
        ("wall.post", {"response": {"post_id": 77}}), ("wall.getById", exact_response())])
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "confirmed" and result.verified is False
    assert result.external_id == "-123456_77" and result.permalink == "https://vk.com/wall-123456_77"
    assert result.evidence["fixture"] is True and result.evidence["readback_exact"] is True
    assert form(seen[0])["message"] == snapshot()["body"]
    assert form(seen[0])["owner_id"] == DESTINATION and form(seen[0])["from_group"] == "1"
    assert form(seen[0])["guid"] == _guid(REQUEST_ID)
    assert form(seen[1])["posts"] == "-123456_77"
    assert not pending and all(not request.url.query for request in seen)


def test_revoked_authorization_before_claim_never_calls_api_or_creates_journal(publishers, tmp_path):
    publisher, seen, _ = publishers([])

    def revoked():
        raise AgentError("TARGET_PAUSED", 409)

    with pytest.raises(AgentError, match="TARGET_PAUSED"):
        publisher.publish(DESTINATION, snapshot(), REQUEST_ID, authorize=revoked)
    assert seen == [] and not (tmp_path / "data/vk-deliveries").exists()


@pytest.mark.parametrize("deny_stage,expected_calls", [
    ("upload_sending", ["photos.getWallUploadServer"]),
    ("save_sending", ["photos.getWallUploadServer", "upload.php"]),
    ("post_sending", ["photos.getWallUploadServer", "upload.php", "photos.saveWallPhoto"]),
])
def test_authorization_is_rechecked_after_each_durable_mutation_intent(
        publishers, tmp_path, deny_stage, expected_calls):
    media, _, _ = image_media(tmp_path)
    publisher, seen, _ = publishers(image_script())

    def authorize():
        path = tmp_path / "data/vk-deliveries" / (REQUEST_ID + ".json")
        if path.exists() and json.loads(path.read_text())["stage"] == deny_stage:
            raise AgentError("APPROVAL_STALE", 409)

    result = publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID, authorize=authorize)
    assert result.state == "unknown"
    assert result.evidence["code"] == "APPROVAL_STALE"
    assert [str(request.url).split("/")[-1].split("?")[0] for request in seen] == expected_calls
    count = len(seen)
    assert publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID).state == "unknown"
    assert len(seen) == count


def test_title_is_preserved_once_in_plain_text_without_markdown_stars(tmp_path):
    publisher = VKPublisher(tmp_path, {})
    try:
        value = snapshot()
        value["body"] = "Полный текст"
        rendered = publisher.validate(DESTINATION, value)
        assert rendered["text"] == "Звёзды\n\nПолный текст"
        assert rendered["native_bold"] is False and rendered["title_format"] == "plain"
        assert publisher.validate(DESTINATION, snapshot())["text"] == snapshot()["body"]
        assert not (tmp_path / "data").exists()
    finally:
        publisher.close()


def test_guid_is_stable_bound_to_dispatch_not_process():
    assert _guid("dispatch-123") == _guid("dispatch-123")
    assert _guid("dispatch-124") != _guid("dispatch-123")
    assert len(_guid(REQUEST_ID)) == 32


def test_image_upload_binds_approved_bytes_and_saved_photo(publishers, tmp_path):
    media, raw, path = image_media(tmp_path)
    script = image_script()

    def server_response(_request):
        path.write_bytes(b"changed after local preparation")
        return script[0][1]

    publisher, seen, pending = publishers([(script[0][0], server_response), *script[1:]])
    result = publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID)
    assert result.state == "confirmed" and result.verified is False
    assert not pending and raw in seen[1].content
    assert b"fixture-secret" not in seen[1].content and "authorization" not in seen[1].headers
    assert form(seen[0])["group_id"] == "123456"
    assert form(seen[2])["group_id"] == "123456" and form(seen[2])["server"] == "55"
    assert form(seen[3])["attachments"] == "photo-123456_88"
    record = json.loads((tmp_path / "data/vk-deliveries/dispatch-123.json").read_text())
    assert record["photo"] == [-123456, 88] and record["external_id"] == "-123456_77"
    assert "fixture-secret" not in json.dumps(record) and "opaque-upload-data" not in json.dumps(record)


def test_api_cookies_are_not_sent_to_photo_upload_server(publishers, tmp_path):
    media, _, _ = image_media(tmp_path)
    script = image_script()
    server_response = httpx.Response(200, json=script[0][1],
        headers={"Set-Cookie": "private-session=fixture-only; Domain=vk.com; Secure; Path=/"})
    publisher, seen, _ = publishers([(script[0][0], server_response), *script[1:]])
    assert publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID).state == "confirmed"
    assert "cookie" not in seen[1].headers


@pytest.mark.parametrize("change", [
    {"text": "Изменённый текст"}, {"owner_id": -2}, {"from_id": 2}, {"id": 78},
    {"post_type": "suggest"}, {"is_deleted": 1}, {"copy_history": [{"id": 77}]},
    {"attachments": [{"type": "link", "link": {"url": "https://example.org"}}]},
])
def test_mismatched_readback_keeps_known_post_and_blocks_second_send(publishers, change):
    publisher, seen, _ = publishers([
        ("wall.post", {"response": {"post_id": 77}}),
        ("wall.getById", exact_response(wall(**change)))])
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "unknown" and result.external_id == "-123456_77"
    assert result.evidence["code"] == "VK_READBACK_MISMATCH"
    again = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert again.state == "unknown" and again.external_id == result.external_id and len(seen) == 2


def test_wrong_saved_photo_readback_is_unknown(publishers, tmp_path):
    media, _, _ = image_media(tmp_path)
    post = wall(attachments=[{"type": "photo", "photo": {"owner_id": -123456, "id": 99}}])
    publisher, _, _ = publishers(image_script(post))
    assert publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID).state == "unknown"


@pytest.mark.parametrize("stage", ["upload", "save", "post"])
def test_unknown_mutation_is_never_retried_even_by_new_instance(publishers, tmp_path, stage):
    media, _, _ = image_media(tmp_path)
    script = image_script()
    index = {"upload": 1, "save": 2, "post": 3}[stage]
    script = [*script[:index], (script[index][0], httpx.ReadTimeout("private URL and token"))]
    publisher, seen, _ = publishers(script)
    result = publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID)
    assert result.state == "unknown" and result.verified is False
    other, other_seen, _ = publishers([])
    retried = other.publish(DESTINATION, snapshot([media]), REQUEST_ID)
    assert retried.state == "unknown" and retried.evidence["code"] == "VK_REQUEST_ALREADY_ATTEMPTED"
    assert len(seen) == index + 1 and not other_seen
    assert "private URL" not in json.dumps(result.evidence)


@pytest.mark.parametrize("url", [
    "http://pu.vk.com/upload", "https://pu.vk.com.evil.example/upload", "https://127.0.0.1/upload",
    "https://api.vk.com/method/wall.post", "https://user:secret@pu.vk.com/upload",
    "https://pu.vk.com:444/upload", "https://pu.vk.com/upload#fragment",
    "https://pu.vk.com/upload?access_token=leak", "https://pu.vk.com/upload?url=https://evil.example",
    "https://pu.vk.com/upload?hash=one&hash=two", "https://pu.vk.com/upload?x=%0D%0Aheader",
])
def test_unsafe_upload_urls_stop_before_binary_upload(publishers, tmp_path, url):
    media, _, _ = image_media(tmp_path)
    publisher, seen, _ = publishers([
        ("photos.getWallUploadServer", {"response": {"upload_url": url}})])
    result = publisher.publish(DESTINATION, snapshot([media]), REQUEST_ID)
    assert result.state == "rejected" and result.evidence["code"] == "VK_UPLOAD_URL_INVALID"
    assert len(seen) == 1 and url not in json.dumps(result.evidence)


def test_redirect_is_not_followed_and_becomes_unknown(publishers):
    publisher, seen, _ = publishers([("wall.post", httpx.Response(307,
        headers={"Location": "https://example.org/secret"}))])
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "unknown" and len(seen) == 1


@pytest.mark.parametrize("error_code,expected", [
    (14, "VK_CAPTCHA_REQUIRED"), (6, "VK_RATE_LIMIT"), (29, "VK_RATE_LIMIT"),
    (5, "VK_AUTH_REQUIRED"), (15, "VK_ACCESS_DENIED"), (100, "VK_API_REJECTED"),
])
def test_api_rejections_stop_without_retry_or_secret_leak(publishers, error_code, expected):
    publisher, seen, _ = publishers([("wall.post", {"error": {"error_code": error_code,
        "error_msg": "fixture-secret-DO-NOT-EXPOSE https://secret.example/?token=private",
        "captcha_img": "https://secret.example/image"}})])
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "rejected" and result.evidence["code"] == expected
    assert len(seen) == 1 and "secret.example" not in json.dumps(result.evidence)
    assert "fixture-secret-DO-NOT-EXPOSE" not in json.dumps(result.evidence)


@pytest.mark.parametrize("bad_reply", [
    httpx.Response(200, content=b'{"response":{"post_id":77,"post_id":78}}'),
    httpx.Response(200, json={"response": {"post_id": True}}),
    httpx.Response(200, json={"response": {}}),
])
def test_ambiguous_accepted_post_response_is_unknown(publishers, bad_reply):
    publisher, seen, _ = publishers([("wall.post", bad_reply)])
    assert publisher.publish(DESTINATION, snapshot(), REQUEST_ID).state == "unknown"
    assert len(seen) == 1


@pytest.mark.parametrize("destination", ["123456", "-0", "-01", "-123?token=private", "https://vk.com/test", -123])
def test_only_explicit_canonical_community_ids_are_accepted(publishers, destination):
    publisher, seen, _ = publishers([])
    with pytest.raises(AgentError, match="VK_COMMUNITY_ID_INVALID"):
        publisher.validate(destination, snapshot())
    assert not seen


def test_media_hash_path_format_and_length_are_checked_before_network(publishers, tmp_path):
    media, _, path = image_media(tmp_path)
    publisher, seen, _ = publishers([])
    wrong_hash = {**media, "sha256": "0" * 64}
    with pytest.raises(AgentError, match="VK_IMAGE_HASH_MISMATCH"):
        publisher.publish(DESTINATION, snapshot([wrong_hash]), REQUEST_ID)
    with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
        publisher.validate(DESTINATION, snapshot([{**media, "path": "../outside.png"}]))
    path.write_bytes(b"not really PNG")
    corrupt = {**media, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(AgentError, match="VK_IMAGE_INVALID"):
        publisher.validate(DESTINATION, snapshot([corrupt]))
    value = snapshot()
    value["body"] = "a" * 16385
    with pytest.raises(AgentError, match="VK_TEXT_TOO_LONG"):
        publisher.validate(DESTINATION, value)
    assert not seen and not (tmp_path / "data").exists()


def test_inspection_only_reads_permissions_and_cannot_confirm_delivery(publishers):
    publisher, seen, _ = publishers([
        ("groups.getById", {"response": {"groups": [{"id": 123456, "is_admin": 1,
                                                     "admin_level": 2, "can_post": 1}]}}),
        ("account.getAppPermissions", {"response": 8196})])
    access = publisher.inspect(DESTINATION)
    assert access["publish"] and access["image"] and access["read"]
    assert access["live_verified"] is False and access["fixture"] is True
    assert all(request.method == "POST" and not request.url.query for request in seen)


def test_readback_failure_recovers_by_read_only_reconcile_after_restart(publishers):
    publisher, _, _ = publishers([
        ("wall.post", {"response": {"post_id": 77}}),
        ("wall.getById", httpx.ReadTimeout("unavailable"))])
    first = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert first.state == "unknown" and first.external_id == "-123456_77"
    other, seen, _ = publishers([("wall.getById", exact_response())])
    result = other.reconcile(DESTINATION, snapshot(), first.external_id, REQUEST_ID)
    assert result.state == "confirmed" and result.verified is False
    assert len(seen) == 1 and seen[0].url.path.endswith("wall.getById")
    again = other.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert again.state == "unknown" and len(seen) == 1


@pytest.mark.parametrize("with_image", [False, True])
def test_db_finalization_loss_recovers_journal_id_with_read_only_get_by_id(
        publishers, tmp_path, with_image):
    candidate = snapshot()
    if with_image:
        media, _, _ = image_media(tmp_path)
        candidate = snapshot([media])
        script = image_script()
    else:
        script = [("wall.post", {"response": {"post_id": 77}}),
                  ("wall.getById", exact_response())]
    publisher, _, _ = publishers([*script[:-1], ("wall.getById", httpx.ReadTimeout("offline"))])
    assert publisher.publish(DESTINATION, candidate, REQUEST_ID).state == "unknown"
    other, seen, _ = publishers([script[-1]])
    result = other.reconcile(DESTINATION, candidate, None, REQUEST_ID)
    assert result.state == "confirmed" and result.external_id == "-123456_77"
    assert result.evidence["recovered_from_journal"] is True and result.verified is False
    assert len(seen) == 1 and seen[0].url.path.endswith("wall.getById")
    assert form(seen[0])["posts"] == "-123456_77"


def test_unknown_upload_journal_has_no_post_id_and_reconcile_makes_no_http(publishers, tmp_path):
    media, _, _ = image_media(tmp_path)
    script = image_script()
    publisher, _, _ = publishers([script[0], ("upload", httpx.ReadTimeout("unknown upload"))])
    candidate = snapshot([media])
    assert publisher.publish(DESTINATION, candidate, REQUEST_ID).state == "unknown"
    other, seen, _ = publishers([])
    result = other.reconcile(DESTINATION, candidate, None, REQUEST_ID)
    assert result.state == "unknown" and result.external_id is None
    assert result.evidence["failure_code"] == "VK_EXTERNAL_ID_REQUIRED" and not seen


def test_journal_id_recovery_rejects_missing_partial_and_different_snapshot(publishers, tmp_path):
    publisher, seen, _ = publishers([])
    result = publisher.reconcile(DESTINATION, snapshot(), None, REQUEST_ID)
    assert result.state == "unknown" and result.evidence["failure_code"] == "VK_JOURNAL_UNVERIFIED"
    folder = tmp_path / "data/vk-deliveries"
    folder.mkdir(parents=True)
    path = folder / "dispatch-123.json"
    path.write_text('{"request_id":', encoding="utf-8")
    result = publisher.reconcile(DESTINATION, snapshot(), None, REQUEST_ID)
    assert result.state == "unknown" and result.evidence["failure_code"] == "VK_JOURNAL_UNVERIFIED"
    path.unlink()
    sent, _, _ = publishers([("wall.post", {"response": {"post_id": 77}}),
                            ("wall.getById", httpx.ReadTimeout("offline"))])
    assert sent.publish(DESTINATION, snapshot(), REQUEST_ID).state == "unknown"
    changed = {**snapshot(), "body": "Different approved revision"}
    result = publisher.reconcile(DESTINATION, changed, None, REQUEST_ID)
    assert result.state == "unknown" and result.evidence["failure_code"] == "VK_JOURNAL_UNVERIFIED"
    assert not seen


def test_image_reconcile_uses_saved_photo_evidence_not_only_count(publishers, tmp_path):
    media, _, _ = image_media(tmp_path)
    script = image_script()
    publisher, _, _ = publishers([*script[:-1], ("wall.getById", httpx.ReadTimeout("offline"))])
    candidate = snapshot([media])
    assert publisher.publish(DESTINATION, candidate, REQUEST_ID).state == "unknown"
    other, seen, _ = publishers([script[-1]])
    assert other.reconcile(DESTINATION, candidate, "-123456_77", REQUEST_ID).state == "confirmed"
    without_journal, no_seen, _ = publishers([])
    refused = without_journal.reconcile(DESTINATION, candidate, "-123456_77")
    assert refused.state == "unknown" and refused.evidence["code"] == "VK_RECONCILE_EVIDENCE_REQUIRED"
    assert len(seen) == 1 and not no_seen


def test_reconcile_requires_known_id_and_never_retries_after_local_media_change(publishers, tmp_path):
    media, _, path = image_media(tmp_path)
    publisher, seen, _ = publishers([])
    candidate = snapshot([media])
    missing = publisher.reconcile(DESTINATION, candidate, None)
    assert missing.state == "unknown" and missing.evidence["code"] == "VK_EXTERNAL_ID_REQUIRED"
    path.write_bytes(b"changed")
    result = publisher.reconcile(DESTINATION, candidate, "-123456_77", REQUEST_ID)
    assert result.state == "unknown" and result.external_id == "-123456_77"
    assert result.evidence["code"] == "VK_IMAGE_HASH_MISMATCH" and not seen


def test_journal_binding_change_or_partial_file_never_reposts(publishers, tmp_path):
    publisher, _, _ = publishers([("wall.post", httpx.ReadTimeout("unknown"))])
    assert publisher.publish(DESTINATION, snapshot(), REQUEST_ID).state == "unknown"
    changed = copy.deepcopy(snapshot())
    changed["body"] += " changed"
    other, seen, _ = publishers([])
    result = other.publish(DESTINATION, changed, REQUEST_ID)
    assert result.state == "unknown" and result.evidence["code"] == "VK_JOURNAL_UNVERIFIED"
    path = tmp_path / "data/vk-deliveries/dispatch-123.json"
    path.write_text('{"request_id":', encoding="utf-8")
    third, third_seen, _ = publishers([])
    assert third.publish(DESTINATION, snapshot(), REQUEST_ID).state == "unknown"
    assert not seen and not third_seen


def test_journal_photo_tampering_prevents_readback_confirmation(publishers, tmp_path):
    media, _, _ = image_media(tmp_path)
    script = image_script()
    publisher, _, _ = publishers([*script[:-1], ("wall.getById", httpx.ReadTimeout("offline"))])
    candidate = snapshot([media])
    assert publisher.publish(DESTINATION, candidate, REQUEST_ID).state == "unknown"
    path = tmp_path / "data/vk-deliveries/dispatch-123.json"
    record = json.loads(path.read_text())
    record["photo"][1] = 999
    path.write_text(json.dumps(record), encoding="utf-8")
    other, seen, _ = publishers([])
    result = other.reconcile(DESTINATION, candidate, "-123456_77", REQUEST_ID)
    assert result.state == "unknown" and result.evidence["code"] == "VK_JOURNAL_UNVERIFIED"
    assert not seen


def test_journal_symlink_never_overwrites_or_follows_other_file(publishers, tmp_path):
    target = tmp_path / "preserved.txt"
    target.write_bytes(b"preserve original file")
    folder = tmp_path / "data/vk-deliveries"
    folder.mkdir(parents=True)
    alias = folder / "dispatch-123.json"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("Windows account cannot create symbolic links")
    publisher, seen, _ = publishers([])
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "unknown" and result.evidence["code"] == "VK_JOURNAL_PATH_UNSAFE"
    assert target.read_bytes() == b"preserve original file" and alias.is_symlink() and not seen


@pytest.mark.parametrize("component", ["data", "data/vk-deliveries",
                                      "data/vk-deliveries/dispatch-123.json"])
@pytest.mark.parametrize("metadata_method", ["is_symlink", "is_junction"])
def test_journal_reparse_metadata_blocks_before_any_network_or_write(
        publishers, tmp_path, monkeypatch, component, metadata_method):
    # Windows может запрещать создание реальных symlink. Проверяем также
    # контракт метаданных файловой системы для файла и каждого предка.
    guarded = tmp_path / component
    original = getattr(Path, metadata_method)

    def metadata(path):
        return path == guarded or original(path)

    monkeypatch.setattr(Path, metadata_method, metadata)
    publisher, seen, _ = publishers([])
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "unknown" and result.evidence["code"] == "VK_JOURNAL_PATH_UNSAFE"
    assert not seen and not (tmp_path / "data").exists()


@pytest.mark.parametrize("mime", [[], {}, None, 123])
def test_malformed_media_type_fails_closed_without_network(publishers, tmp_path, mime):
    media, _, _ = image_media(tmp_path)
    publisher, seen, _ = publishers([])
    with pytest.raises(AgentError, match="VK_IMAGE_INVALID"):
        publisher.validate(DESTINATION, snapshot([{**media, "mime": mime}]))
    assert not seen


def test_offline_mode_and_missing_credentials_never_claim_or_send(publishers, tmp_path, monkeypatch):
    publisher, seen, _ = publishers([], options={"adapter_mode": "fake"})
    result = publisher.publish(DESTINATION, snapshot(), REQUEST_ID)
    assert result.state == "rejected" and result.evidence["code"] == "VK_LIVE_MODE_REQUIRED"
    assert publisher.inspect(DESTINATION)["state"] == "offline"
    monkeypatch.delenv("VK_TEST_TOKEN")
    other, other_seen, _ = publishers([])
    assert other.publish(DESTINATION, snapshot(), REQUEST_ID).evidence["code"] == "VK_TOKEN_REQUIRED"
    assert not seen and not other_seen and not (tmp_path / "data").exists()
