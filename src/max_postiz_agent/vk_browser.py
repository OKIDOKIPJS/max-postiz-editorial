"""VK через отдельный пользовательский браузер; VK ID token не подменяет право wall/photos."""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import Delivery
from .config import AgentError, fingerprint, inside


ORIGIN = "https://vk.ru"
CONTRACT = "vk-browser-single-click-v1"
# Только элементы, наблюдавшиеся в авторизованной вкладке с разрешения владельца
# при чтении профиля, composer и settings с подготовленным частным черновиком
# без отправки 31.10.2026. Согласование текста владельцем ещё ожидается.
# Это наблюдение UI, не human-attestation или доказательство живой публикации.
# Отдельные recipient/author anchors в settings не наблюдались и не угадываются.
OBSERVED_DOM = {
    "account": '[data-testid="header-profile-menu-button"]',
    "open_editor": '[data-testid="posting_create_post_button"]',
    "editor": '[data-testid="posting_base_screen_input_message"]',
    "upload_input": '[data-testid="posting_base_screen_download_from_device"]',
    "next": '[data-testid="posting_base_screen_next"]',
    "posts": '[data-testid="post"]',
    "post_text": '[data-testid="post_text"]',
    "post_link": '[data-testid="post_date_block_preview"]',
    "post_author": '[data-testid="post-header-title"]',
    "modal": '[role="dialog"][data-testid="posting_modal_box"]',
    "preview_scope": '[data-testid="posting_modal_box"]',
    "preview_text": '[data-testid="posting_modal_box"] [data-testid="showmoretext-in"]',
    "publish": '[data-testid="posting_submit_button"]',
    "publish_label": "Опубликовать",
    "story_switch": '[data-testid="posting_settings_repost_to_story_switch"]',
    "coauthors": '[data-testid="posting_coowners_cell"]',
    "visibility": '[data-testid="posting_settings_visibility"]',
}
REQUIRED_DOM = {"account", "open_editor", "editor", "upload_input", "uploaded_images",
                "recipient", "preview_author", "preview_text", "preview_images", "publish", "publish_label",
                "posts", "post_text", "post_link", "post_author", "post_images", "forbidden_settings",
                "extra_attachments", "post_extra_attachments", "blocked", "errors", "wall_ready"}
_ID = re.compile(r"[1-9][0-9]{0,18}\Z")
_DESTINATION = re.compile(r"-?[1-9][0-9]{0,18}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def approved_text_urls(text, snapshot):
    sources = snapshot.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise AgentError("VK_BROWSER_SNAPSHOT_INVALID")
    candidates = re.findall(r"https?://[^\s<>\"']+", text)
    candidates.extend(item["url"] for item in sources if isinstance(item.get("url"), str))
    result = set()
    for value in candidates:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme in {"https", "http"} and parsed.netloc and parsed.hostname
                    and not parsed.username and not parsed.password
                    and not any(ord(char) < 33 for char in value)):
                result.add(value)
        except ValueError:
            continue
    return sorted(result)


def owner_id(options):
    value = options.get("owner")
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AgentError("VK_BROWSER_OWNER_REQUIRED")
    return value


def wall_url(destination, options):
    owner = owner_id(options)
    if not isinstance(destination, str) or not _DESTINATION.fullmatch(destination):
        raise AgentError("VK_BROWSER_DESTINATION_INVALID")
    if not destination.startswith("-") and destination != owner:
        raise AgentError("VK_BROWSER_DESTINATION_NOT_OWNED")
    return ORIGIN + ("/club" + destination[1:] if destination.startswith("-") else "/id" + destination)


def dom_contract(options, *, publish=False):
    extra = options.get("dom_contract", {})
    if (not isinstance(extra, dict) or any(not isinstance(key, str) or not isinstance(value, str)
            or not value.strip() or len(value) > 1000 for key, value in extra.items())):
        raise AgentError("VK_BROWSER_DOM_CONTRACT_INVALID")
    selectors = {**OBSERVED_DOM, **extra}
    identity = selectors.get("identity_contract", "explicit-v1")
    if identity not in {"explicit-v1", "personal-wall-settings-v1"}:
        raise AgentError("VK_BROWSER_IDENTITY_CONTRACT_INVALID")
    required = REQUIRED_DOM
    if identity == "personal-wall-settings-v1":
        required = (REQUIRED_DOM - {"recipient", "preview_author"}) | {
            "modal", "preview_scope", "story_switch", "coauthors", "visibility", "text_contract"}
        if selectors.get("text_contract") != "vk-spacer-div-v1":
            raise AgentError("VK_BROWSER_TEXT_CONTRACT_UNVERIFIED")
    if publish and (options.get("dom_contract_verified") is not True
                    or not required <= selectors.keys()):
        raise AgentError("VK_BROWSER_DOM_CONTRACT_UNVERIFIED")
    return selectors


def private_path(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or ":" in relative or Path(relative).is_absolute():
        raise AgentError("VK_BROWSER_PATH_UNSAFE")
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise AgentError("VK_BROWSER_PATH_UNSAFE")
    try:
        return inside(root, relative)
    except (AgentError, OSError):
        raise AgentError("VK_BROWSER_PATH_UNSAFE") from None


def profile_path(root, options):
    path = private_path(root, options.get("profile_dir", "data/vk-browser"))
    if path.parent != private_path(root, "data") or not re.fullmatch(r"vk-browser(?:-[A-Za-z0-9_-]+)?", path.name):
        raise AgentError("VK_BROWSER_PROFILE_INVALID")
    return path


def prepare(root, options, destination, snapshot):
    from PIL import Image, UnidentifiedImageError

    url = wall_url(destination, options)
    selectors = dom_contract(options, publish=True)
    if selectors.get("identity_contract") == "personal-wall-settings-v1" and destination != owner_id(options):
        raise AgentError("VK_BROWSER_PERSONAL_WALL_REQUIRED")
    profile_path(root, options)
    if (not isinstance(snapshot, dict) or snapshot.get("destination", destination) != destination
            or snapshot.get("platform", "vk") != "vk" or snapshot.get("route", "vk_browser") != "vk_browser"):
        raise AgentError("VK_BROWSER_SNAPSHOT_INVALID")
    title, body = snapshot.get("title"), snapshot.get("body")
    if (not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title
            or not isinstance(body, str) or not body.strip() or "\x00" in title + body):
        raise AgentError("VK_BROWSER_TEXT_INVALID")
    text = body if body == title or body.startswith(title + "\n") else title + "\n\n" + body
    try:
        units = len(text.encode("utf-16-le")) // 2
        snapshot_hash = fingerprint(snapshot)
    except (UnicodeError, ValueError, TypeError):
        raise AgentError("VK_BROWSER_SNAPSHOT_INVALID") from None
    if units > min(16384, options.get("max_chars", 16384)):
        raise AgentError("VK_BROWSER_TEXT_TOO_LONG")
    media = snapshot.get("media", [])
    if not isinstance(media, list) or len(media) > 1:
        raise AgentError("VK_BROWSER_SINGLE_PHOTO_REQUIRED")
    if media and options.get("media_contract_verified") is not True:
        raise AgentError("VK_BROWSER_MEDIA_CONTRACT_UNVERIFIED")
    checked = []
    for item in media:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or item.get("mime") not in {"image/jpeg", "image/png"}
                or not isinstance(item.get("sha256"), str) or not _HASH.fullmatch(item["sha256"])):
            raise AgentError("VK_BROWSER_MEDIA_INVALID")
        path = private_path(root, item["path"])
        try:
            if not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
                raise AgentError("VK_BROWSER_PHOTO_SIZE_INVALID")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise AgentError("VK_BROWSER_IMAGE_HASH_CHANGED", 409)
            with Image.open(path) as image:
                mime = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(image.format)
                if (mime != item["mime"] or image.width * image.height > 40_000_000
                        or getattr(image, "n_frames", 1) != 1):
                    raise AgentError("VK_BROWSER_PHOTO_FORMAT_INVALID")
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise AgentError("VK_BROWSER_PHOTO_FORMAT_INVALID") from None
        checked.append(dict(item))
    return {"destination": destination, "wall_url": url, "owner": owner_id(options),
            "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "media": checked, "snapshot_sha256": snapshot_hash,
            "dom_contract_sha256": fingerprint(selectors), "title_format": "plain",
            "author_id_attribute": selectors.get("author_id_attribute"),
            "allowed_text_urls": approved_text_urls(text, snapshot)}


def _receipt_author(value, destination, expected_attribute):
    if (not isinstance(value, dict) or value.get("id") != destination
            or not {"href", "id_attribute", "numeric_id"} <= value.keys()):
        return False
    attribute = value["id_attribute"]
    if attribute != expected_attribute:
        return False
    if attribute is None:
        if value["numeric_id"] is not None:
            return False
    elif (not isinstance(attribute, str) or not re.fullmatch(r"data-[a-z][a-z0-9_-]{0,63}", attribute)
          or value["numeric_id"] != destination):
        return False
    href = value["href"]
    if href:
        from .vk_browser_worker import _link_path
        try:
            expected = "/club" + destination[1:] if destination.startswith("-") else "/id" + destination
            path = _link_path(href, ORIGIN, author_tracking=True)
            if re.fullmatch(r"/(id|club)[1-9][0-9]{0,18}", path):
                return path == expected
            return attribute is not None
        except (AgentError, ValueError):
            return False
    return href is None and attribute is not None


class VKBrowser:
    mode = "browser"

    def __init__(self, root, options=None):
        self.root = Path(root).resolve()
        self.options = dict(options or {})

    def validate(self, destination, snapshot):
        plan = prepare(self.root, self.options, destination, snapshot)
        return {**plan, "preflight_only": True, "live_verified": False}

    def _call(self, action, destination, **payload):
        try:
            result = subprocess.run([sys.executable, "-m", "max_postiz_agent.vk_browser_worker",
                "--root", str(self.root)], input=json.dumps({"action": action,
                "destination": destination, "options_sha256": fingerprint(self.options), **payload}),
                text=True, encoding="utf-8", capture_output=True, check=False,
                timeout=min(360, self.options.get("operation_timeout_seconds", 180)) + 60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise AgentError("VK_BROWSER_OPERATION_UNKNOWN", 503) from None
        if "code" in value:
            code = value["code"]
            if not isinstance(code, str) or not re.fullmatch(r"VK_BROWSER_[A-Z_]{1,80}", code):
                code = "VK_BROWSER_OPERATION_UNKNOWN"
            raise AgentError(code, 409)
        if result.returncode != 0:
            raise AgentError("VK_BROWSER_OPERATION_UNKNOWN", 503)
        return value

    def inspect(self, destination):
        wall_url(destination, self.options)
        profile_path(self.root, self.options)
        return self._call("inspect", destination)

    def login(self, destination):
        wall_url(destination, self.options)
        profile_path(self.root, self.options)
        directory = private_path(self.root, "data/vk-browser-login")
        directory.mkdir(parents=True, exist_ok=True)
        with private_path(self.root, "data/vk-browser-login/stdout.log").open("w", encoding="utf-8") as out, \
                private_path(self.root, "data/vk-browser-login/stderr.log").open("w", encoding="utf-8") as err:
            process = subprocess.Popen([sys.executable, "-m", "max_postiz_agent.vk_browser_worker",
                "--root", str(self.root), "--login", destination], stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return {"state": "login_window_requested", "pid": process.pid, "live_verified": False,
                "profile": self.options.get("profile_dir", "data/vk-browser")}

    @staticmethod
    def _delivery(value, destination, request_id, plan=None, *, expected_fixture=None, minimum_observed_at=None):
        if not isinstance(value, dict):
            return Delivery("unknown", verified=False,
                            evidence={"failure_code": "VK_BROWSER_RECEIPT_INVALID"})
        state = value.get("state")
        receipt_evidence = value.get("evidence")
        if not isinstance(state, str) or state not in {"confirmed", "unknown", "failed"}:
            return Delivery("unknown", verified=False)
        if state == "confirmed":
            evidence = value.get("evidence")
            ident = value.get("external_id")
            proof = evidence.get("proof") if isinstance(evidence, dict) else None
            reloads = proof.get("reloads") if isinstance(proof, dict) else None
            expected_images = [item["sha256"] for item in plan["media"]] if isinstance(plan, dict) else None
            observed_at = proof.get("observed_at") if isinstance(proof, dict) else None
            if (not isinstance(ident, str) or not re.fullmatch(re.escape(destination) + r"_[1-9][0-9]{0,18}", ident)
                    or value.get("permalink") != ORIGIN + "/wall" + ident
                    or not isinstance(evidence, dict) or evidence.get("contract") != CONTRACT
                    or evidence.get("request_id") != request_id or type(evidence.get("fixture")) is not bool
                    or type(expected_fixture) is not bool or evidence.get("fixture") is not expected_fixture
                    or type(value.get("verified")) is not bool
                    or value.get("verified") is not (evidence.get("fixture") is False)
                    or evidence.get("independent_readback") is not True
                    or evidence.get("verification") != "browser_readback"
                    or evidence.get("journal_state") != "confirmed"
                    or type(evidence.get("clicks")) is not int or evidence.get("clicks") != 1
                    or not isinstance(plan, dict) or evidence.get("snapshot_sha256") != plan["snapshot_sha256"]
                    or not isinstance(proof, dict) or proof.get("external_id") != ident
                    or proof.get("permalink") != ORIGIN + "/wall" + ident
                    or proof.get("title_format") != "plain"
                    or type(observed_at) not in {int, float} or observed_at <= 0 or observed_at > 1e12
                    or not math.isfinite(observed_at)
                    or minimum_observed_at is not None and observed_at < minimum_observed_at
                    or proof.get("verification") != "independent_wall_reload_exact_text_recipient_author_image_sha256"
                    or not isinstance(reloads, list) or len(reloads) != 2
                    or any(not isinstance(item, dict) or item.get("external_id") != ident
                           or item.get("text_sha256") != plan["text_sha256"]
                           or not isinstance(item.get("image_sha256"), list)
                           or item.get("image_sha256") != expected_images
                           or not isinstance(item.get("text_links"), list)
                           or any(not isinstance(href, str) or href not in plan["allowed_text_urls"]
                                  for href in item["text_links"])
                           or not _receipt_author(item.get("author"), destination,
                                                  plan.get("author_id_attribute")) for item in reloads)):
                return Delivery("unknown", value.get("external_id"), value.get("permalink"),
                                verified=False, evidence={"failure_code": "VK_BROWSER_RECEIPT_INVALID"})
        return Delivery(state, value.get("external_id"), value.get("permalink"),
                        verified=state == "confirmed" and value.get("verified") is True
                        and isinstance(receipt_evidence, dict) and receipt_evidence.get("fixture") is False,
                        evidence=value.get("evidence"))

    def _confirmed_journal(self, value, destination, request_id, plan, *, expected_fixture=None):
        from .vk_browser_worker import _read
        try:
            _, journal = _read(self.root, request_id)
        except AgentError:
            raise AgentError("VK_BROWSER_RECEIPT_INVALID", 409) from None
        evidence = value.get("evidence")
        created_at = journal.get("created_at")
        observed_at = journal.get("proof", {}).get("observed_at") if isinstance(journal.get("proof"), dict) else None
        if (journal.get("destination") != destination
                or journal.get("options_sha256") != fingerprint(self.options)
                or journal.get("snapshot_sha256") != plan["snapshot_sha256"]
                or expected_fixture is not None and journal.get("fixture") is not expected_fixture
                or journal.get("state") != "confirmed"
                or journal.get("external_id") != value.get("external_id")
                or journal.get("permalink") != value.get("permalink")
                or type(created_at) not in {int, float} or not 0 < created_at <= 1e12
                or type(observed_at) not in {int, float} or not created_at <= observed_at <= time.time() + 5
                or not isinstance(evidence, dict) or journal.get("proof") != evidence.get("proof")
                or journal.get("clicks") != evidence.get("clicks")):
            raise AgentError("VK_BROWSER_RECEIPT_INVALID", 409)
        return journal["fixture"]

    def publish(self, destination, snapshot, request_id):
        plan = self.validate(destination, snapshot)
        value = None
        try:
            value = self._call("publish", destination, snapshot=snapshot, request_id=request_id)
            fixture = (self._confirmed_journal(value, destination, request_id, plan)
                       if isinstance(value, dict) and value.get("state") == "confirmed" else None)
            return self._delivery(value, destination, request_id, plan, expected_fixture=fixture)
        except AgentError as exc:
            return Delivery("unknown", value.get("external_id") if isinstance(value, dict) else None,
                            value.get("permalink") if isinstance(value, dict) else None,
                            verified=False, evidence={"failure_code": exc.code})

    def reconcile(self, destination, request_id, *, snapshot=None):
        wall_url(destination, self.options)
        value = None
        try:
            if not isinstance(snapshot, dict):
                raise AgentError("VK_BROWSER_RECONCILE_SNAPSHOT_REQUIRED", 409)
            plan = prepare(self.root, self.options, destination, snapshot)
            from .vk_browser_worker import _read
            _, journal = _read(self.root, request_id)
            if journal.get("destination") != destination or journal.get("options_sha256") != fingerprint(self.options):
                raise AgentError("VK_BROWSER_JOURNAL_CONFLICT", 409)
            if journal.get("snapshot_sha256") != plan["snapshot_sha256"]:
                raise AgentError("VK_BROWSER_RECONCILE_SNAPSHOT_MISMATCH", 409)
            started_at = time.time()
            value = self._call("reconcile", destination, request_id=request_id)
            fixture = (self._confirmed_journal(value, destination, request_id, plan,
                                               expected_fixture=journal["fixture"])
                       if isinstance(value, dict) and value.get("state") == "confirmed" else None)
            return self._delivery(value, destination, request_id, plan, expected_fixture=fixture,
                                  minimum_observed_at=started_at)
        except AgentError as exc:
            return Delivery("unknown", value.get("external_id") if isinstance(value, dict) else None,
                            value.get("permalink") if isinstance(value, dict) else None,
                            verified=False, evidence={"failure_code": exc.code})

    def close(self):
        pass
