"""Owner acceptance of exact media in ONE existing post; never a send permission."""

import hashlib
import math
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import AgentError, fingerprint, parse_json
from .vk_media import allowed_cdn, image_identity, validate_upload


CONTRACT = "accepted-actual-media-v1"
ACTION = "vk_actual_media_acceptance"
REVOKED = "vk_actual_media_acceptance_revoked"
VERIFICATION = "independent_wall_reload_exact_text_recipient_author_owner_accepted_actual_photo_v1"
FIELDS = {"contract", "request_id", "snapshot_hash", "snapshot_sha256", "original_approval_id", "original_due_at",
          "owner", "destination", "external_id", "permalink", "asset_id", "source_sha256", "source_dimensions",
          "actual_sha256", "actual_dimensions", "native_crop_box", "consent_path", "consent_sha256", "approved_at",
          "existing_post_only", "new_publications_authorized", "future_policy_change_authorized",
          "original_policy_satisfied"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _invalid():
    raise AgentError("VK_BROWSER_ACTUAL_MEDIA_ACCEPTANCE_INVALID", 409)


def _number(value):
    return type(value) in {int, float} and 0 < value <= 1e12 and math.isfinite(value)


def _dimensions(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(n) is int and 0 < n <= 40000000 for n in value) and value[0] * value[1] <= 40000000)


def reference(review):
    return {"contract": CONTRACT, "review_id": review["id"], "review_sha256": fingerprint(review),
            "original_policy_satisfied": False}


def validate_review(root, event, context, plan, journal, *, timestamp=None):
    """Pure authority validation except the bound, private consent artifact read."""
    from .vk_browser import ORIGIN, private_path
    stamp = time.time() if timestamp is None else timestamp
    try:
        details = event["details"]
        if (not isinstance(details, dict) or set(details) != FIELDS or details["contract"] != CONTRACT
                or event["actor"] != "owner" or event["action"] != ACTION or event["entity"] != context["request_id"]
                or not isinstance(event["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event["id"])
                or not _number(event["at"]) or not _number(details["approved_at"])
                or not details["approved_at"] <= event["at"] <= stamp + 5
                or details["existing_post_only"] is not True or details["new_publications_authorized"] is not False
                or details["future_policy_change_authorized"] is not False or details["original_policy_satisfied"] is not False):
            _invalid()
        expected = {key: context[key] for key in ("request_id", "snapshot_hash", "original_approval_id", "original_due_at")}
        expected.update(snapshot_sha256=plan["snapshot_sha256"], owner=plan["owner"], destination=plan["destination"],
                        source_sha256=plan["source_image"]["source_sha256"], source_dimensions=plan["source_image"]["dimensions"])
        if (any(details[key] != value for key, value in expected.items()) or not _number(details["original_due_at"])
                or any(not isinstance(details[key], str) or not _HASH.fullmatch(details[key])
                       for key in ("snapshot_hash", "snapshot_sha256", "source_sha256", "actual_sha256", "consent_sha256"))
                or not _dimensions(details["actual_dimensions"]) or not _dimensions(details["source_dimensions"])
                or details["actual_dimensions"] == details["source_dimensions"]
                or not isinstance(details["native_crop_box"], list) or len(details["native_crop_box"]) != 4
                or any(type(n) is not int or n < 0 for n in details["native_crop_box"])
                or details["native_crop_box"] != [0, 0, *details["actual_dimensions"]]
                or not re.fullmatch(re.escape(plan["destination"]) + r"_[1-9][0-9]{0,18}", details["external_id"])
                or details["permalink"] != ORIGIN + "/wall" + details["external_id"]
                or journal.get("external_id") not in {None, details["external_id"]}
                or context.get("external_id") not in {None, details["external_id"]}
                or journal.get("permalink") not in {None, details["permalink"]}
                or context.get("permalink") not in {None, details["permalink"]}
                or type(journal.get("clicks")) is not int or journal["clicks"] != 1
                or not validate_upload(journal.get("upload_proof"), plan, context["request_id"])
                or journal["upload_proof"]["asset_id"] != details["asset_id"]):
            _invalid()
        path = details["consent_path"]
        if not isinstance(path, str) or not re.fullmatch(r"data/[A-Za-z0-9_./-]+\.json", path):
            _invalid()
        consent_path = private_path(root, path)
        if not 0 < consent_path.stat().st_size <= 65536:
            _invalid()
        raw = consent_path.read_bytes()
        consent = parse_json(raw.decode("utf-8"))
        if hashlib.sha256(raw).hexdigest() != details["consent_sha256"] or not isinstance(consent, dict):
            _invalid()
        consent_expected = {key: details[key] for key in ("external_id", "permalink", "asset_id", "source_sha256",
                            "source_dimensions", "actual_sha256", "actual_dimensions", "native_crop_box")}
        consent_expected.update(contract="owner-actual-vk-image-consent-v1", dispatch_id=context["request_id"],
            acceptance_of_existing_post_only=True, new_publication_authorized=False, future_crop_policy_change_authorized=False)
        if (any(consent.get(key) != value or type(consent.get(key)) is not type(value) for key, value in consent_expected.items())
                or not isinstance(consent.get("owner_reply"), str) or not consent["owner_reply"].strip()
                or len(consent["owner_reply"]) > 4096 or not isinstance(consent.get("question_id"), str)
                or not consent["question_id"]):
            _invalid()
        moment = datetime.fromisoformat(consent["recorded_at_utc"])
        if moment.tzinfo is None or moment.timestamp() != details["approved_at"]:
            _invalid()
        return {"id": event["id"], "actor": event["actor"], "action": event["action"], "entity": event["entity"],
                "at": event["at"], "details": details}
    except (KeyError, TypeError, ValueError, OSError, UnicodeError, OverflowError):
        _invalid()


def _review_in_session(session, root, options, destination, request_id, plan, journal):
    from sqlalchemy import select
    from .db import Audit
    from .hub import HubService
    from .hub_models import Dispatch, Target, TargetApproval, Variant
    events = list(session.scalars(select(Audit).where(Audit.entity == request_id, Audit.action.in_({ACTION, REVOKED}))))
    if not events:
        return None
    if len(events) != 1 or events[0].action != ACTION:
        _invalid()
    dispatch = session.get(Dispatch, request_id)
    approval = session.get(TargetApproval, dispatch.approval_id) if dispatch else None
    variant = session.get(Variant, dispatch.variant_id) if dispatch else None
    target = session.get(Target, variant.target_id) if variant else None
    if (dispatch is None or dispatch.state != "unknown" or dispatch.owner is not None
            or not math.isfinite(dispatch.lease_until) or dispatch.lease_until != 0
            or approval is None or approval.revoked or variant is None or target is None
            or target.platform != "vk" or target.route != "vk_browser" or target.destination != destination
            or fingerprint(target.options) != fingerprint(options)
            or dispatch.snapshot != journal.get("snapshot") or fingerprint(dispatch.snapshot) != plan["snapshot_sha256"]
            or HubService.snapshot(None, variant, target) != dispatch.snapshot
            or dispatch.revision != variant.revision or approval.revision != dispatch.revision
            or approval.variant_id != variant.id or approval.scheduled_at != dispatch.due_at
            or approval.snapshot_hash != dispatch.snapshot_hash
            or fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at}) != dispatch.snapshot_hash):
        _invalid()
    context = {"request_id": request_id, "snapshot_hash": dispatch.snapshot_hash,
               "original_approval_id": approval.id, "original_due_at": dispatch.due_at,
               "external_id": dispatch.external_id, "permalink": dispatch.permalink}
    event = events[0]
    return validate_review(root, {"id": event.id, "actor": event.actor, "action": event.action,
        "entity": event.entity, "at": event.at, "details": event.details}, context, plan, journal)


def load_review(root, options, destination, request_id, plan, journal, *, database=None, session=None):
    """No writes. Final promotion may reuse its locked Dispatch/Variant/Target session.

    Every acceptance/revocation writer must acquire the same Dispatch row lock
    before appending Audit, so a new revocation cannot race this final check.
    """
    from .config import Settings
    from .db import Database
    from sqlalchemy.exc import SQLAlchemyError
    db = None
    try:
        if session is not None:
            return _review_in_session(session, root, options, destination, request_id, plan, journal)
        db = database or Database(Settings.load(Path(root)))
        with db.session() as session:
            return _review_in_session(session, root, options, destination, request_id, plan, journal)
    except SQLAlchemyError:
        raise AgentError("VK_BROWSER_ACTUAL_MEDIA_AUTHORITY_UNAVAILABLE", 503) from None
    finally:
        if database is None and db is not None:
            db.engine.dispose()


def _crop(url, expected):
    values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return values.get("crop") == [",".join(str(n) for n in expected)]


def reviewed_preview_matches(main, original_url, review, *, origin="https://vk.ru"):
    """A viewer may serve the same image at a smaller, explicit cs=WIDTHx0."""
    from urllib.parse import parse_qsl
    try:
        width, height = main["width"], main["height"]
        actual_width, actual_height = review["details"]["actual_dimensions"]
        if (main.get("complete") is not True or type(width) is not int or type(height) is not int
                or not 0 < width <= actual_width or not 0 < height <= actual_height
                or not allowed_cdn(main["url"], origin=origin) or not allowed_cdn(original_url, origin=origin)
                or not _crop(original_url, review["details"]["native_crop_box"])):
            return False
        current, original = urlsplit(main["url"]), urlsplit(original_url)
        if (current.scheme, current.netloc, current.path, current.fragment) != (original.scheme, original.netloc, original.path, original.fragment):
            return False
        queries = [parse_qsl(item.query, keep_blank_values=True, strict_parsing=True) for item in (current, original)]
        if any(len(pairs) != len({key for key, _ in pairs}) for pairs in queries):
            return False
        main_cs = [value for key, value in queries[0] if key == "cs"]
        original_cs = [value for key, value in queries[1] if key == "cs"]
        if main_cs and main_cs != [str(width) + "x0"] or original_cs and original_cs != [str(actual_width) + "x0"]:
            return False
        current_raw, original_raw = current.query.split("&"), original.query.split("&")
        if (main_cs and current_raw.count("cs=" + str(width) + "x0") != 1
                or original_cs and original_raw.count("cs=" + str(actual_width) + "x0") != 1):
            return False
        if main["url"] == original_url and [width, height] == [actual_width, actual_height]:
            return True
        if main_cs != [str(width) + "x0"] or original_cs != [str(actual_width) + "x0"]:
            return False
        if len(current_raw) != len(original_raw):
            return False
        differences = [(left, right) for left, right in zip(current_raw, original_raw, strict=True) if left != right]
        return (len(differences) == 1
            and differences[0] == ("cs=" + str(width) + "x0", "cs=" + str(actual_width) + "x0")
            and abs(height * actual_width - actual_height * width) <= actual_width)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def accepted_proof(raw, source, contract, asset, review, original_url, *, origin="https://vk.ru"):
    actual = image_identity(raw)
    details = review["details"]
    if (asset != details["asset_id"] or actual["source_sha256"] != details["actual_sha256"]
            or actual["dimensions"] != details["actual_dimensions"] or source["source_sha256"] != details["source_sha256"]
            or actual["dimensions"] == source["dimensions"]
            or source["dimensions"] != details["source_dimensions"] or not allowed_cdn(original_url, origin=origin)
            or not _crop(original_url, details["native_crop_box"])):
        _invalid()
    return {"contract": CONTRACT, "asset_id": asset, "source_sha256": source["source_sha256"],
        "source_dimensions": source["dimensions"], "actual_sha256": actual["source_sha256"],
        "actual_dimensions": actual["dimensions"], "actual_pixels_sha256": actual["pixels_sha256"],
        "original_contract": contract, "original_policy_satisfied": False, "media_review": reference(review),
        "native_crop_box": details["native_crop_box"], "original_url": original_url}


def validate_accepted_proof(value, source, contract, asset, review, *, origin="https://vk.ru"):
    if not isinstance(value, dict) or not isinstance(review, dict):
        return False
    try:
        details = review["details"]
        expected = {"contract": CONTRACT, "asset_id": asset, "source_sha256": source["source_sha256"],
            "source_dimensions": source["dimensions"], "actual_sha256": details["actual_sha256"],
            "actual_dimensions": details["actual_dimensions"], "original_contract": contract,
            "original_policy_satisfied": False, "media_review": reference(review), "native_crop_box": details["native_crop_box"]}
        return (asset == details["asset_id"] and source["source_sha256"] == details["source_sha256"]
            and source["dimensions"] == details["source_dimensions"]
            and details["actual_dimensions"] != source["dimensions"]
            and all(value.get(key) == item and type(value.get(key)) is type(item) for key, item in expected.items())
            and all(isinstance(value.get(key), str) and _HASH.fullmatch(value[key]) for key in ("actual_pixels_sha256", "viewer_sha256"))
            and allowed_cdn(value.get("original_url"), origin=origin) and _crop(value["original_url"], details["native_crop_box"]))
    except (KeyError, TypeError, ValueError):
        return False


def final_acceptance_check(core, session, dispatch, target, delivery, *, not_before):
    """Recheck authority inside the final locked promotion transaction."""
    from .vk_browser import VKBrowser, prepare
    from .vk_browser_worker import _read
    if target.platform != "vk" or target.route != "vk_browser" or delivery.verified is not True:
        _invalid()
    root = core.settings.root
    plan = prepare(root, target.options, target.destination, dispatch.snapshot)
    _, journal = _read(root, dispatch.id)
    authority = load_review(root, target.options, target.destination, dispatch.id, plan, journal, session=session)
    if authority is None:
        _invalid()
    value = {"state": delivery.state, "external_id": delivery.external_id, "permalink": delivery.permalink,
             "verified": delivery.verified, "evidence": delivery.evidence}
    adapter = VKBrowser(root, target.options)
    adapter._confirmed_journal(value, target.destination, dispatch.id, plan, expected_fixture=False)
    checked = adapter._delivery(value, target.destination, dispatch.id, plan, expected_fixture=False,
                               minimum_observed_at=not_before, expected_media_review=authority)
    if checked.state != "confirmed" or checked.verified is not True:
        _invalid()
