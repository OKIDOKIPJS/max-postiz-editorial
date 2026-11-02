"""A separate opt-in policy proves only a native crop of uniform opaque edges."""
import copy
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.vk_media import (
    EDGE_POLICY, derivative_proof, image_identity, source_identity, validate_proof,
)


def photo(*, right=True, bottom=True, alpha=255):
    image = Image.new("RGBA", (64, 48), (244, 247, 251, alpha))
    image.paste((30, 90, 150, 255), (0, 0, 50, 35))
    if not right:
        image.putpixel((63, 5), (1, 2, 3, 255))
    if not bottom:
        image.putpixel((4, 47), (1, 2, 3, 255))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def derivative(raw, size):
    with Image.open(io.BytesIO(raw)) as image:
        stream = io.BytesIO()
        image.crop((0, 0, *size)).convert("RGB").save(stream, "JPEG", quality=90)
        return stream.getvalue()


def url(width, height, *, x=0, y=0):
    return f"https://sun1.vkuserphoto.ru/original.jpg?crop={x},{y},{width},{height}&cs={width}x0"


CONTRACT = {"policy": EDGE_POLICY, "evidence_sha256": "a" * 64}
pytest_plugins = ["test_vk_media"]


def test_example_uses_approved_policy_but_cannot_enable_live_without_observations():
    example = json.loads((Path(__file__).resolve().parents[1] /
        "config/vk_browser_image.target.example.json").read_text(encoding="utf-8"))
    assert example["enabled"] is False
    assert example["options"]["media_proof_policy"] == EDGE_POLICY
    assert example["options"]["media_contract_verified"] is False
    assert example["options"]["dom_contract_verified"] is False


def test_new_policy_requires_its_own_observed_contract_and_preserves_old_plan(image_case):
    from max_postiz_agent.vk_browser import prepare
    root, raw, options, snapshot, seal = image_case
    original_snapshot = copy.deepcopy(snapshot)
    old = prepare(root, options, "123456", snapshot)
    options["media_proof_policy"] = EDGE_POLICY
    with pytest.raises(AgentError, match="VK_BROWSER_MEDIA_EVIDENCE_INVALID"):
        prepare(root, options, "123456", snapshot)
    seal()
    new = prepare(root, options, "123456", snapshot)
    assert new["source_image"] == source_identity(raw, EDGE_POLICY)
    assert "uniform_edge_guard" not in old["source_image"]
    assert new["image_contract"]["policy"] == EDGE_POLICY
    assert snapshot == original_snapshot


@pytest.mark.parametrize("policy", ["exact-bytes-v1", "exact-pixels-v1", "native-asset-lineage-v1"])
def test_historical_source_identity_shape_is_exactly_preserved(policy):
    raw = photo()
    assert source_identity(raw, policy) == image_identity(raw)
    assert "uniform_edge_guard" not in source_identity(raw, policy)


@pytest.mark.parametrize("size", [[64, 48], [63, 48], [64, 47], [63, 47]])
def test_only_one_uniform_right_bottom_pixel_may_be_removed(size):
    raw = photo()
    source = source_identity(raw, EDGE_POLICY)
    original_url = url(*size)
    proof = derivative_proof(derivative(raw, size), source, "123456_77", CONTRACT, native_url=original_url)
    proof.update(original_url=original_url, viewer_sha256="b" * 64)
    assert proof["uniform_edge_crop"]["crop_box"] == [0, 0, *size]
    assert validate_proof(proof, source, CONTRACT, "123456", "123456_77")
    assert proof["bytes_equal"] is False


@pytest.mark.parametrize("size", [[62, 48], [64, 46], [32, 24], [65, 48], [48, 64]])
def test_resize_rotation_and_more_than_one_pixel_always_fail(size):
    raw = photo()
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(derivative(raw, size), source_identity(raw, EDGE_POLICY), "123456_77", CONTRACT,
                         native_url=url(*size))


@pytest.mark.parametrize("options,size", [({"right": False}, [63, 48]),
    ({"bottom": False}, [64, 47]), ({"alpha": 0}, [63, 47]), ({"alpha": 254}, [63, 47])])
def test_content_or_transparency_on_removed_edge_blocks_crop(options, size):
    raw = photo(**options)
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(derivative(raw, size), source_identity(raw, EDGE_POLICY), "123456_77", CONTRACT,
                         native_url=url(*size))


@pytest.mark.parametrize("native_url", [None, "https://sun1.vkuserphoto.ru/a.jpg",
    url(63, 47, x=1), url(63, 47, y=1), url(62, 47),
    url(63, 47) + "&crop=0,0,63,47", url(63, 47) + "&cs=63x0",
    "https://sun1.vkuserphoto.ru/a.jpg?crop=0,0,63,47&cs=32x0"])
def test_crop_requires_exact_unique_native_box_and_no_scaling(native_url):
    raw = photo()
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(derivative(raw, [63, 47]), source_identity(raw, EDGE_POLICY), "123456_77", CONTRACT,
                         native_url=native_url)


@pytest.mark.parametrize("damage", ["source", "guard", "dimensions", "box", "policy", "asset", "url"])
def test_parent_revalidates_every_field_in_worker_stdout(damage):
    raw = photo()
    source = source_identity(raw, EDGE_POLICY)
    proof = derivative_proof(derivative(raw, [63, 47]), source, "123456_77", CONTRACT, native_url=url(63, 47))
    proof.update(original_url=url(63, 47), viewer_sha256="b" * 64)
    assert validate_proof(proof, source, CONTRACT, "123456", "123456_77")
    corrupt = copy.deepcopy(proof)
    if damage == "source":
        corrupt["source_sha256"] = "0" * 64
    elif damage == "guard":
        corrupt["uniform_edge_crop"]["source_guard_sha256"] = "0" * 64
    elif damage == "dimensions":
        corrupt["derivative_dimensions"] = [62, 47]
    elif damage == "box":
        corrupt["uniform_edge_crop"]["crop_box"][0] = 1
    elif damage == "policy":
        corrupt["policy"] = "native-asset-lineage-v1"
    elif damage == "asset":
        corrupt["asset_id"] = "123456_78"
    else:
        corrupt["original_url"] = "file:///a?crop=0,0,63,47"
    assert not validate_proof(corrupt, source, CONTRACT, "123456", "123456_77")


def test_old_lineage_policy_still_rejects_exactly_the_new_edge_case():
    raw = photo()
    with pytest.raises(AgentError, match="VK_BROWSER_IMAGE_READBACK_MISMATCH"):
        derivative_proof(derivative(raw, [63, 47]), image_identity(raw), "123456_77",
                         {"policy": "native-asset-lineage-v1", "evidence_sha256": "a" * 64})
