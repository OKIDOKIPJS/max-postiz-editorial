"""Exact image samples and honest native asset lineage; no browser or network."""
import hashlib
import io
import json
import struct
import time
import zlib
from copy import deepcopy
from urllib.parse import quote

import pytest
from PIL import Image, PngImagePlugin

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.vk_browser import PERSONAL_IMAGE_DOM, dom_contract, prepare
from max_postiz_agent.vk_media import (
    OBSERVATIONS, SCOPES, allowed_cdn, asset_id, derivative_proof, image_identity,
    require_contract, upload_binding, validate_proof, validate_upload,
    native_asset_id, validate_transform, approved_link,
    rendered_approved_link,
)


@pytest.mark.parametrize("label", ["https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html",
    "nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html", "nssdc.gsfc.nasa.gov/planetary/..."])
def test_native_caption_link_projects_only_proven_url_label(label):
    url = "https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html"
    href = "/away.php?to=" + quote(url, safe="") + "&utf=1"
    assert rendered_approved_link(href, label, [url]) == url


@pytest.mark.parametrize("label", ["Earth", "nssdc.gsfc.nasa.gov/...", "nssdc.gsfc.nasa.gov...",
    "nssdc.gsfc.nasa.gov/other/...", "nssdc.gsfc.nasa.gov/planetary/…",
    " nssdc.gsfc.nasa.gov/planetary/...", "nssdc.gsfc.nasa.gov/planetary/... Extra"])
def test_native_caption_link_rejects_arbitrary_or_unobserved_labels(label):
    url = "https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html"
    with pytest.raises(AgentError, match="VK_BROWSER_UNAPPROVED_LINK"):
        rendered_approved_link("/away.php?to=" + quote(url, safe="") + "&utf=1", label, [url])


def test_native_caption_link_does_not_project_short_labels_for_direct_links():
    url = "https://nssdc.gsfc.nasa.gov/planetary/factsheet/earthfact.html"
    with pytest.raises(AgentError, match="VK_BROWSER_UNAPPROVED_LINK"):
        rendered_approved_link(url, "nssdc.gsfc.nasa.gov/planetary/...", [url])


def encoded(color="navy", *, metadata=False, jpeg=False):
    image = Image.new("RGB", (64, 48), color)
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "Different container, exactly equal pixels")
    image.save(output, "JPEG" if jpeg else "PNG", **({"pnginfo": info} if metadata else {}))
    return output.getvalue()


@pytest.fixture
def image_case(tmp_path):
    raw = encoded()
    (tmp_path / "data").mkdir()
    (tmp_path / "approved.png").write_bytes(raw)
    options = {"owner": "123456", "profile_dir": "data/vk-browser", "dom_contract_profile": "personal-image-v1",
        "dom_contract_verified": True, "media_contract_verified": True, "media_proof_policy": "exact-bytes-v1",
        "media_contract_evidence": "data/vk-media-fixture.json", "dom_contract": {
            "uploaded_images": "#uploaded img", "preview_images": "#preview img",
            "uploaded_asset": "#uploaded a", "uploaded_asset_id_attribute": "href",
            "preview_asset": "#preview", "preview_asset_binding": "uploaded-cdn-url-v1"}}
    def seal():
        selectors = dom_contract(options)
        evidence = {"contract": OBSERVATIONS, "origin": "https://vk.ru", "owner": "123456",
            "policy": options["media_proof_policy"], "dom_contract_sha256": fingerprint(selectors),
            "native_observed": True, "observed_at": time.time(), "scopes": sorted(SCOPES)}
        path = tmp_path / options["media_contract_evidence"]
        path.write_text(json.dumps(evidence), encoding="utf-8")
        options["media_contract_evidence_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path
    seal()
    snapshot = {"title": "Новая звезда", "body": "Полный согласованный текст.", "destination": "123456",
        "media": [{"path": "approved.png", "mime": "image/png", "sha256": hashlib.sha256(raw).hexdigest()}]}
    return tmp_path, raw, options, snapshot, seal


def test_default_image_profile_has_only_observed_controls_and_stays_unverified():
    assert PERSONAL_IMAGE_DOM["uploaded_asset_id_attribute"] == "data-sortable-id"
    assert PERSONAL_IMAGE_DOM["preview_asset_binding"] == "uploaded-cdn-url-v1"
    assert "preview_asset_id_attribute" not in PERSONAL_IMAGE_DOM
    assert 'input[type="file"]' in PERSONAL_IMAGE_DOM["upload_input"]
    with pytest.raises(AgentError, match="VK_BROWSER_DOM_CONTRACT_UNVERIFIED"):
        dom_contract({"dom_contract_profile": "personal-image-v1"}, publish=True)


def test_explicit_image_profile_binds_source_and_evidence_without_changing_snapshot(image_case):
    root, raw, options, snapshot, _ = image_case
    original = deepcopy(snapshot)
    plan = prepare(root, options, "123456", snapshot)
    assert plan["source_image"]["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert plan["image_contract"]["evidence_sha256"] == options["media_contract_evidence_sha256"]
    assert snapshot == original


@pytest.mark.parametrize("damage", ["missing_file", "wrong_hash", "wrong_owner", "wrong_dom", "missing_scope", "unobserved", "policy"])
def test_boolean_alone_or_modified_observations_never_enable_images(image_case, damage):
    root, _, options, _, _ = image_case
    path = root / options["media_contract_evidence"]
    value = json.loads(path.read_text())
    if damage == "missing_file":
        path.unlink()
    elif damage == "wrong_hash":
        options["media_contract_evidence_sha256"] = "0" * 64
    else:
        if damage == "wrong_owner":
            value["owner"] = "999"
        elif damage == "wrong_dom":
            value["dom_contract_sha256"] = "0" * 64
        elif damage == "missing_scope":
            value["scopes"].pop()
        elif damage == "unobserved":
            value["native_observed"] = False
        else:
            value["policy"] = "native-asset-lineage-v1"
        path.write_text(json.dumps(value))
        options["media_contract_evidence_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(AgentError):
        require_contract(root, options, dom_contract(options), "123456")


def test_text_preset_still_rejects_media_and_image_preset_requires_one(image_case):
    root, _, options, snapshot, _ = image_case
    with pytest.raises(AgentError, match="VK_BROWSER_TEXT_PROFILE_MEDIA_UNSUPPORTED"):
        prepare(root, {**options, "dom_contract_profile": "personal-text-v1"}, "123456", snapshot)
    with pytest.raises(AgentError, match="VK_BROWSER_SINGLE_PHOTO_REQUIRED"):
        prepare(root, options, "123456", {**snapshot, "media": []})


def test_metadata_only_reencoding_has_exact_pixels_but_never_claims_source_byte_sha():
    source = image_identity(encoded())
    raw = encoded(metadata=True)
    contract = {"policy": "exact-pixels-v1", "evidence_sha256": "a" * 64}
    proof = derivative_proof(raw, source, "123456_77", contract)
    proof.update(original_url="https://sun1.vkuserphoto.ru/a.jpg", viewer_sha256="a" * 64)
    assert proof["bytes_equal"] is False and proof["pixels_equal"] is True
    assert proof["derivative_sha256"] != proof["source_sha256"]
    assert validate_proof(proof, source, contract, "123456", "123456_77")
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(raw, source, "123456_77", {**contract, "policy": "exact-bytes-v1"})


def test_gamma_changes_are_not_exact_pixel_equivalence():
    def gamma(value):
        output = io.BytesIO()
        info = PngImagePlugin.PngInfo()
        info.add(b"gAMA", struct.pack(">I", value))
        Image.new("RGB", (64, 48), "navy").save(output, "PNG", pnginfo=info)
        return output.getvalue()
    first, second = gamma(45455), gamma(100000)
    source = image_identity(first)
    assert source["pixels_sha256"] != image_identity(second)["pixels_sha256"]
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(second, source, "123456_77", {"policy": "exact-pixels-v1", "evidence_sha256": "a" * 64})


def test_nested_exif_color_space_is_bound():
    def jpeg(space):
        output = io.BytesIO()
        exif = Image.Exif()
        exif[34665] = {40961: space}
        Image.new("RGB", (64, 48), "navy").save(output, "JPEG", exif=exif)
        return output.getvalue()
    assert image_identity(jpeg(1))["pixels_sha256"] != image_identity(jpeg(65535))["pixels_sha256"]


@pytest.mark.parametrize("value", [b"abc", (1, 2), 1.5])
def test_non_scalar_exif_color_space_is_rejected_without_raw_exception(value):
    out = io.BytesIO()
    exif = Image.Exif()
    exif[34665] = {40961: value}
    Image.new("RGB", (64, 48), "navy").save(out, "JPEG", exif=exif)
    with pytest.raises(AgentError):
        image_identity(out.getvalue())


def test_truncated_png_ihdr_is_sanitized_before_indexing():
    raw = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\0IHDR" + b"\0" * 4
    with pytest.raises(AgentError, match="VK_BROWSER_PHOTO_FORMAT_INVALID"):
        image_identity(raw)


@pytest.mark.parametrize("kind,data", [(b"cICP", bytes([1, 13, 0, 1])), (b"mDCV", b"\0" * 24), (b"cLLI", b"\0" * 8)])
def test_uninterpreted_hdr_color_chunks_cannot_claim_exact_pixels(kind, data):
    raw = encoded()
    chunk = struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = raw[:33] + chunk + raw[33:]
    assert kind in raw
    with pytest.raises(AgentError, match="VK_BROWSER_PHOTO_PIXEL_CONTRACT_UNSUPPORTED"):
        image_identity(raw)


def test_native_sortable_prefix_is_attribute_specific():
    assert native_asset_id("photo123456_77", "123456", "data-sortable-id") == "123456_77"
    with pytest.raises(AgentError):
        native_asset_id("photo123456_77", "123456", "href")


def test_prepare_uses_same_frozen_file_bytes_for_sha_and_pixels(image_case, monkeypatch):
    from pathlib import Path
    root, first, options, snapshot, _ = image_case
    second = encoded("red")
    original = Path.read_bytes
    reads = []
    def racing(path):
        if path == root / "approved.png":
            reads.append(True)
            return first if len(reads) == 1 else second
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", racing)
    plan = prepare(root, options, "123456", snapshot)
    assert len(reads) == 1
    assert plan["source_image"]["source_sha256"] == snapshot["media"][0]["sha256"]


def test_lossy_or_changed_image_requires_explicit_lineage_policy_not_perceptual_threshold():
    source = image_identity(encoded())
    raw = encoded("red", jpeg=True)
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(raw, source, "123456_77", {"policy": "exact-pixels-v1", "evidence_sha256": "a" * 64})
    contract = {"policy": "native-asset-lineage-v1", "evidence_sha256": "a" * 64}
    proof = derivative_proof(raw, source, "123456_77", contract)
    proof.update(original_url="https://sun1.vkuserphoto.ru/a.jpg", viewer_sha256="a" * 64)
    assert proof["comparison"] == "native_asset_lineage" and proof["pixels_equal"] is False
    assert proof["derivative_sha256"] == hashlib.sha256(raw).hexdigest()
    assert validate_proof(proof, source, contract, "123456", "123456_77")
    assert not validate_proof(proof, source, contract, "123456", "123456_78")


@pytest.mark.parametrize("size", [(64, 40), (32, 24), (48, 64)])
def test_same_asset_lineage_never_exempts_crop_resize_or_rotation(size):
    source = image_identity(encoded())
    out = io.BytesIO()
    Image.new("RGB", size, "navy").save(out, "PNG")
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(out.getvalue(), source, "123456_77", {"policy": "native-asset-lineage-v1", "evidence_sha256": "a" * 64})


@pytest.mark.parametrize("damage", [None, "scale", "x", "ratio", "container", "nan"])
def test_native_transform_accepts_only_observed_uncropped_geometry(damage):
    value = {"ratio": 1.4, "width": 224.0, "height": 160.0, "scale": 1.0000000000000002,
             "x": 1.0231815394945439e-14, "y": 5e-15, "container_width": 224.0, "container_height": 160.0}
    if damage == "scale":
        value["scale"] = 1.05
    elif damage == "x":
        value["x"] = 1.0
    elif damage == "ratio":
        value["ratio"] = 1.0
    elif damage == "container":
        value["container_width"] = 200.0
    elif damage == "nan":
        value["width"] = float("nan")
    if damage:
        with pytest.raises(AgentError, match="VK_BROWSER_PHOTO_TRANSFORM_UNAPPROVED"):
            validate_transform(value, [1400, 1000])
    else:
        assert validate_transform(value, [1400, 1000])["scale"] == value["scale"]


@pytest.mark.parametrize("value", ["https://evil.test/photo123456_77", "javascript:123456_77", "https:123456_77", "/photo999_77", "/photo123456_77?edit=1"])
def test_asset_owner_and_origin_cannot_be_substituted(value):
    with pytest.raises(AgentError):
        asset_id(value, "123456")


@pytest.mark.parametrize("url,allowed", [
    ("https://sun9-46.vkuserphoto.ru/s/photo.jpg?signature=test", True),
    ("https://sun1.userapi.com/photo.jpg", True),
    ("https://vkuserphoto.ru.evil.test/photo.jpg", False),
    ("https://evilvkuserphoto.ru/photo.jpg", False), ("http://sun9.vkuserphoto.ru/a.jpg", False),
    ("https://user:password@sun9.vkuserphoto.ru/a.jpg", False), ("https://sun9.vkuserphoto.ru:444/a.jpg", False)])
def test_observed_cdn_allowlist_is_not_a_substring_check(url, allowed):
    assert allowed_cdn(url) is allowed


@pytest.mark.parametrize("damage", [None, "extra", "duplicate", "wrong_target", "missing_utf", "wrong_utf", "external"])
def test_native_away_alias_has_one_exact_approved_target(damage):
    target = "https://science.example.test/complete/article"
    alias = "/away.php?to=" + quote(target, safe="") + "&utf=1"
    if damage == "extra":
        alias += "&other=1"
    elif damage == "duplicate":
        alias += "&to=" + quote(target, safe="")
    elif damage == "wrong_target":
        alias = alias.replace("complete", "unapproved")
    elif damage == "missing_utf":
        alias = alias.replace("&utf=1", "")
    elif damage == "wrong_utf":
        alias = alias.replace("utf=1", "utf=0")
    elif damage == "external":
        alias = "https://evil.test" + alias
    if damage:
        with pytest.raises(AgentError, match="VK_BROWSER_UNAPPROVED_LINK"):
            approved_link(alias, [target])
    else:
        assert approved_link(alias, [target]) == target


@pytest.mark.parametrize("damage", ["owner", "request_id", "source_sha256", "snapshot_sha256", "uploads", "asset_id"])
def test_upload_binding_is_exact_and_cannot_relabel_an_old_photo(image_case, damage):
    root, _, options, snapshot, _ = image_case
    plan = prepare(root, options, "123456", snapshot)
    binding = upload_binding(plan, "fixture-operation", "123456_77", "https://sun1.vkuserphoto.ru/a.jpg", "a" * 64)
    assert validate_upload(binding, plan, "fixture-operation")
    binding[damage] = True if damage == "uploads" else "unrelated"
    assert not validate_upload(binding, plan, "fixture-operation")
