"""Versioned native VK photo identity; perceptual similarity is never proof."""

import hashlib
import io
import math
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import AgentError, fingerprint, parse_json


PROFILE = "personal-image-v1"
CONTRACT = "vk-native-single-image-v1"
OBSERVATIONS = "vk-native-image-observations-v1"
PIXELS = "exif-transpose-rgba8-color-metadata-v1"
POLICIES = {"exact-bytes-v1", "exact-pixels-v1", "native-asset-lineage-v1"}
SCOPES = {"upload_input", "uploaded_asset", "preview_uploaded_cdn", "public_photo", "photo_original_viewer"}
REQUIRED_DOM = {"uploaded_asset", "uploaded_asset_id_attribute", "preview_asset", "preview_asset_binding",
                "post_asset", "post_asset_id_attribute", "uploaded_images", "preview_images", "post_images",
                "uploaded_transform", "preview_transform", "preview_grid", "photo_transform_contract"}
PHOTO_TRANSFORM = "native-photo-transform-v1"
LINK_CONTRACT = "vk-native-caption-away-prefix-v2"
MAX_BYTES = 10 * 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ASSET = re.compile(r"([1-9][0-9]{0,18})_([1-9][0-9]{0,18})\Z")


def require_contract(root, options, selectors, owner):
    """An unknown preview/input contract cannot be enabled by a boolean alone."""
    from .vk_browser import private_path

    if options.get("media_contract_verified") is not True or not REQUIRED_DOM <= selectors.keys():
        raise AgentError("VK_BROWSER_MEDIA_CONTRACT_UNVERIFIED")
    policy = options.get("media_proof_policy")
    if not isinstance(policy, str) or policy not in POLICIES:
        raise AgentError("VK_BROWSER_MEDIA_POLICY_REQUIRED")
    if selectors.get("preview_asset_binding") != "uploaded-cdn-url-v1":
        raise AgentError("VK_BROWSER_MEDIA_PREVIEW_BINDING_UNVERIFIED")
    if selectors.get("photo_transform_contract") != PHOTO_TRANSFORM:
        raise AgentError("VK_BROWSER_MEDIA_TRANSFORM_UNVERIFIED")
    for key in ("uploaded_asset_id_attribute", "post_asset_id_attribute"):
        if not re.fullmatch(r"href|data-[a-z][a-z0-9_-]{0,63}", selectors[key]):
            raise AgentError("VK_BROWSER_MEDIA_ASSET_CONTRACT_INVALID")
    name, expected = options.get("media_contract_evidence"), options.get("media_contract_evidence_sha256")
    if (not isinstance(name, str) or not re.fullmatch(r"data/vk-media-[A-Za-z0-9_-]+\.json", name)
            or not isinstance(expected, str) or not _HASH.fullmatch(expected)):
        raise AgentError("VK_BROWSER_MEDIA_EVIDENCE_REQUIRED")
    try:
        raw = private_path(root, name).read_bytes()
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError
        evidence = parse_json(raw.decode("utf-8-sig"))
        observed = evidence.get("observed_at")
        if (evidence.get("contract") != OBSERVATIONS or evidence.get("origin") != "https://vk.ru"
                or evidence.get("owner") != owner or evidence.get("policy") != policy
                or evidence.get("dom_contract_sha256") != fingerprint(selectors)
                or evidence.get("native_observed") is not True
                or not isinstance(evidence.get("scopes"), list) or set(evidence["scopes"]) != SCOPES
                or len(evidence["scopes"]) != len(SCOPES)
                or type(observed) not in {int, float} or not 0 < observed <= time.time() + 5
                or not math.isfinite(observed)):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError, AgentError):
        raise AgentError("VK_BROWSER_MEDIA_EVIDENCE_INVALID") from None
    return {"contract": CONTRACT, "policy": policy, "evidence_sha256": expected}


def image_identity(raw):
    """Exact decoded samples, explicit EXIF orientation; no lossy tolerance."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise AgentError("VK_BROWSER_PHOTO_SIZE_INVALID")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        position = 8
        while position + 12 <= len(raw):
            length = int.from_bytes(raw[position:position + 4], "big")
            kind = raw[position + 4:position + 8]
            end = position + 12 + length
            if end > len(raw):
                raise AgentError("VK_BROWSER_PHOTO_FORMAT_INVALID")
            if kind == b"IHDR" and length != 13:
                raise AgentError("VK_BROWSER_PHOTO_FORMAT_INVALID")
            if kind in {b"cICP", b"mDCV", b"cLLI"} or kind == b"IHDR" and raw[position + 16] > 8:
                raise AgentError("VK_BROWSER_PHOTO_PIXEL_CONTRACT_UNSUPPORTED")
            position = end
            if kind == b"IEND":
                break
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (image.format not in {"PNG", "JPEG"} or image.width * image.height > 40_000_000
                    or getattr(image, "n_frames", 1) != 1 or image.mode not in {"RGB", "RGBA", "L", "LA", "P"}):
                raise AgentError("VK_BROWSER_PHOTO_PIXEL_CONTRACT_UNSUPPORTED")
            profile = image.info.get("icc_profile")
            if profile is not None and (not isinstance(profile, bytes) or len(profile) > 1024 * 1024):
                raise AgentError("VK_BROWSER_PHOTO_PIXEL_CONTRACT_UNSUPPORTED")
            # Equal sample values are not equal rendering when transfer/color
            # interpretation differs. Bind gAMA/cHRM/sRGB/ICC and EXIF ColorSpace.
            color = {key: image.info.get(key) for key in ("gamma", "chromaticity", "srgb", "background")}
            exif = image.getexif()
            nested = exif.get_ifd(34665) if 34665 in exif else {}
            if not isinstance(nested, dict):
                raise AgentError("VK_BROWSER_PHOTO_PIXEL_CONTRACT_UNSUPPORTED")
            for value in (exif.get(40961), nested.get(40961)):
                if value is not None and (type(value) is not int or not 0 <= value <= 65535):
                    raise AgentError("VK_BROWSER_PHOTO_PIXEL_CONTRACT_UNSUPPORTED")
            color.update(mode=image.mode, icc_sha256=hashlib.sha256(profile).hexdigest() if profile else None,
                         exif_color_space_top=exif.get(40961), exif_color_space=nested.get(40961),
                         adobe_transform=image.info.get("adobe_transform"))
            color_hash = fingerprint(color)
            image.load()
            canonical = ImageOps.exif_transpose(image).convert("RGBA")
            dimensions = [canonical.width, canonical.height]
            digest = hashlib.sha256(f"{canonical.width}x{canonical.height}:RGBA:{color_hash}:".encode()
                                    + canonical.tobytes()).hexdigest()
    except (OSError, ValueError, TypeError, IndexError, UnidentifiedImageError, Image.DecompressionBombError):
        raise AgentError("VK_BROWSER_PHOTO_FORMAT_INVALID") from None
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "pixels_sha256": digest,
            "dimensions": dimensions, "color_metadata_sha256": color_hash,
            "pixel_contract": PIXELS, "bytes": len(raw)}


def asset_id(value, owner, *, origin="https://vk.ru"):
    if not isinstance(value, str):
        raise AgentError("VK_BROWSER_PHOTO_ASSET_INVALID")
    parsed = urlsplit(value)
    expected = urlsplit(origin)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme and (parsed.scheme != expected.scheme or not parsed.netloc)
            or parsed.netloc and parsed.netloc != expected.netloc):
        raise AgentError("VK_BROWSER_PHOTO_ASSET_INVALID")
    path = parsed.path
    candidate = path[6:] if path.startswith("/photo") else path
    match = _ASSET.fullmatch(candidate)
    if match is None or match.group(1) != owner:
        raise AgentError("VK_BROWSER_PHOTO_ASSET_OWNER_MISMATCH")
    return candidate


def native_asset_id(value, owner, attribute, *, origin="https://vk.ru"):
    # The native sortable attribute has an observed literal photo prefix.
    # It does not extend accepted href grammar or invent an absent preview ID.
    if attribute == "data-sortable-id":
        if not isinstance(value, str) or not re.fullmatch(r"photo[1-9][0-9]{0,18}_[1-9][0-9]{0,18}", value):
            raise AgentError("VK_BROWSER_PHOTO_ASSET_INVALID")
        return asset_id(value[5:], owner)
    return asset_id(value, owner, origin=origin)


def allowed_cdn(value, *, origin="https://vk.ru"):
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
            return False
        parsed = urlsplit(value)
        if origin != "https://vk.ru":
            return (parsed.scheme, parsed.netloc) == (urlsplit(origin).scheme, urlsplit(origin).netloc)
        return (parsed.scheme == "https" and parsed.hostname is not None and not parsed.fragment
            and not parsed.username and not parsed.password and parsed.port in {None, 443}
            and (parsed.hostname == "vk.ru" or parsed.hostname.endswith(".userapi.com")
                 or parsed.hostname.endswith(".vkuserphoto.ru")))
    except ValueError:
        return False


def derivative_proof(raw, source, remote_asset, contract):
    actual = image_identity(raw)
    same_bytes = actual["source_sha256"] == source["source_sha256"]
    same_pixels = actual["pixels_sha256"] == source["pixels_sha256"] and actual["dimensions"] == source["dimensions"]
    policy = contract["policy"]
    if (policy not in POLICIES or policy == "exact-bytes-v1" and not same_bytes
            or policy == "exact-pixels-v1" and not same_pixels
            or actual["dimensions"] != source["dimensions"]):
        raise AgentError("VK_BROWSER_IMAGE_READBACK_MISMATCH")
    if _ASSET.fullmatch(remote_asset or "") is None:
        raise AgentError("VK_BROWSER_PHOTO_ASSET_INVALID")
    return {"contract": CONTRACT, "policy": policy, "evidence_sha256": contract["evidence_sha256"],
        "asset_id": remote_asset, "source_sha256": source["source_sha256"],
        "source_pixels_sha256": source["pixels_sha256"], "source_dimensions": source["dimensions"],
        "derivative_sha256": actual["source_sha256"], "derivative_pixels_sha256": actual["pixels_sha256"],
        "derivative_dimensions": actual["dimensions"], "pixel_contract": PIXELS,
        "bytes_equal": same_bytes, "pixels_equal": same_pixels,
        "comparison": {"exact-bytes-v1": "exact_bytes", "exact-pixels-v1": "exact_pixels",
                       "native-asset-lineage-v1": "native_asset_lineage"}[policy]}


def validate_proof(proof, source, contract, owner, expected_asset=None, *, origin="https://vk.ru"):
    """Adapter validation rebinds every source/policy field, never trusts stdout."""
    if not isinstance(proof, dict):
        return False
    try:
        ident = asset_id(proof.get("asset_id"), owner)
        expected = {"contract": CONTRACT, "policy": contract["policy"],
            "evidence_sha256": contract["evidence_sha256"], "source_sha256": source["source_sha256"],
            "source_pixels_sha256": source["pixels_sha256"], "source_dimensions": source["dimensions"],
            "pixel_contract": PIXELS, "comparison": {"exact-bytes-v1": "exact_bytes",
                "exact-pixels-v1": "exact_pixels", "native-asset-lineage-v1": "native_asset_lineage"}[contract["policy"]]}
        if any(proof.get(key) != value for key, value in expected.items()) or expected_asset and ident != expected_asset:
            return False
        if any(not isinstance(proof.get(key), str) or not _HASH.fullmatch(proof[key])
               for key in ("derivative_sha256", "derivative_pixels_sha256")):
            return False
        size = proof.get("derivative_dimensions")
        if (not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size)
                or size != source["dimensions"] or size[0] * size[1] > 40_000_000 or type(proof.get("bytes_equal")) is not bool
                or type(proof.get("pixels_equal")) is not bool):
            return False
        same_bytes = proof["derivative_sha256"] == source["source_sha256"]
        same_pixels = proof["derivative_pixels_sha256"] == source["pixels_sha256"] and size == source["dimensions"]
        if same_bytes and not same_pixels:
            return False
        if (not allowed_cdn(proof.get("original_url"), origin=origin) or not isinstance(proof.get("viewer_sha256"), str)
                or not _HASH.fullmatch(proof["viewer_sha256"])):
            return False
        return (proof["bytes_equal"] is same_bytes and proof["pixels_equal"] is same_pixels
            and (contract["policy"] != "exact-bytes-v1" or same_bytes)
            and (contract["policy"] != "exact-pixels-v1" or same_pixels))
    except (AgentError, TypeError, KeyError, ValueError):
        return False


def validate_transform(value, dimensions):
    """Observed uncropped native CSS variables; only floating-point epsilon."""
    keys = {"ratio", "width", "height", "scale", "x", "y", "container_width", "container_height"}
    if (not isinstance(value, dict) or set(value) != keys or any(type(v) not in {int, float}
            or not math.isfinite(v) for v in value.values())):
        raise AgentError("VK_BROWSER_PHOTO_TRANSFORM_UNAPPROVED")
    ratio = dimensions[0] / dimensions[1]
    if (min(value[k] for k in ("width", "height", "container_width", "container_height")) <= 0
            or abs(value["scale"] - 1) > 1e-9 or max(abs(value["x"]), abs(value["y"])) > 1e-6
            or abs(value["ratio"] - ratio) > 1e-9 or abs(value["width"] / value["height"] - ratio) > 1e-9
            or abs(value["container_width"] / value["container_height"] - ratio) > 1e-9
            or abs(value["width"] - value["container_width"]) > 1e-6
            or abs(value["height"] - value["container_height"]) > 1e-6):
        raise AgentError("VK_BROWSER_PHOTO_TRANSFORM_UNAPPROVED")
    return {"contract": PHOTO_TRANSFORM, **value}


def upload_binding(plan, request_id, remote_asset, uploaded_url, derivative_sha256, *, origin="https://vk.ru"):
    remote_asset = asset_id(remote_asset, plan["owner"])
    if not allowed_cdn(uploaded_url, origin=origin) or not isinstance(derivative_sha256, str) or not _HASH.fullmatch(derivative_sha256):
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    return {"contract": CONTRACT, "request_id": request_id, "owner": plan["owner"], "asset_id": remote_asset,
        "source_sha256": plan["source_image"]["source_sha256"], "snapshot_sha256": plan["snapshot_sha256"],
        "policy": plan["image_contract"]["policy"], "evidence_sha256": plan["image_contract"]["evidence_sha256"],
        "uploaded_url": uploaded_url, "uploaded_derivative_sha256": derivative_sha256,
        "execution_origin": origin,
        "uploads": 1}


def validate_upload(value, plan, request_id, *, origin="https://vk.ru"):
    try:
        return (isinstance(value, dict) and type(value.get("uploads")) is int
                and value == upload_binding(plan, request_id, value.get("asset_id"),
                                            value.get("uploaded_url"), value.get("uploaded_derivative_sha256"), origin=origin))
    except (AgentError, KeyError, TypeError):
        return False


def upload_filename(media):
    return "approved-" + media["sha256"][:16] + (".jpg" if media["mime"] == "image/jpeg" else ".png")


def receipt_origin(upload, fixture):
    if not fixture:
        return "https://vk.ru"
    value = upload.get("execution_origin") if isinstance(upload, dict) else None
    if not isinstance(value, str):
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise AgentError("VK_BROWSER_PHOTO_UPLOAD_BINDING_INVALID")
    return value


def approved_link(href, allowed, *, origin="https://vk.ru"):
    if href in allowed:
        return href
    if not isinstance(href, str) or any(ord(char) <= 32 for char in href):
        raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
    value = urlsplit(urljoin(origin, href))
    expected = urlsplit(origin)
    query = parse_qs(value.query, keep_blank_values=True)
    if ((value.scheme, value.netloc) == (expected.scheme, expected.netloc) and not value.fragment
            and value.path == "/away.php" and set(query) == {"to", "utf"} and query["utf"] == ["1"]
            and len(query["to"]) == 1 and query["to"][0] in allowed):
        return query["to"][0]
    raise AgentError("VK_BROWSER_UNAPPROVED_LINK")


def rendered_approved_link(href, label, allowed):
    """Project only an approved native Away link's observed URL label grammar."""
    target = approved_link(href, allowed)
    if not isinstance(label, str):
        raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
    if label == target:
        return target
    # Direct links do not have the observed native display-shortening contract.
    if href in allowed:
        raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
    schemeless = target.split("://", 1)[1]
    if label == schemeless:
        return target
    prefix = label[:-3] if label.endswith("...") else ""
    host_path = urlsplit(target).netloc + "/"
    if (prefix.startswith(host_path) and len(host_path) < len(prefix) < len(schemeless)
            and schemeless.startswith(prefix)):
        return target
    raise AgentError("VK_BROWSER_UNAPPROVED_LINK")
