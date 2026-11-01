# Устойчивый журнал браузерной отправки и сверка неопределённых результатов.
"""One isolated browser operation. Durable intent is written before the send click."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .article_attribution import illustrated_presentation
from .browser_runtime import COMPOSER, ensure_channel, matching_posts, session
from .config import AgentError, Settings, fingerprint, inside, parse_json

ILLUSTRATED_CONTRACT = "article-illustrated-v1"
_PRESENTATION_FIELDS = ("presentation_contract", "illustration_proof_hash", "illustration_dimensions",
                        "title_text", "title_format")
PRECLICK_CONTRACT = "browser-preclick-failure-v1"
_PRECLICK_PHASES = {"session", "channel", "composer", "attachment_snapshot", "attachment_upload",
                    "composer_fill", "composer_ready", "title_format", "authorization", "send_intent"}


class _MatchedPosts:
    def __init__(self, values):
        self.values = values

    def count(self):
        return len(self.values)

    @property
    def last(self):
        return self.values[-1]


def _matched_posts(page, text, *, illustrated=False):
    if not illustrated:
        return matching_posts(page, text)
    posts = page.get_by_role("listitem")
    indices = posts.evaluate_all("""(posts, text) => posts.flatMap((post, index) => {
      const nodes = [post, ...post.querySelectorAll('*')];
      const found = nodes.some(node => (node.innerText === text || node.textContent === text) &&
        node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0);
      return found ? [index] : [];
    })""", text)
    return _MatchedPosts([posts.nth(index) for index in indices])


def _caption_nodes(post, text):
    nodes = post.locator("*")
    indices = nodes.evaluate_all("""(nodes, text) => nodes.flatMap((node, index) => {
      const exact = value => value.innerText === text || value.textContent === text;
      return exact(node) && ![...node.children].some(exact) &&
        node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0 ? [index] : [];
    })""", text)
    if indices:
        return [nodes.nth(index) for index in indices]
    exact_root = post.evaluate("(element, text) => element.innerText === text || element.textContent === text", text)
    return [post] if exact_root else []


def _image_dimensions(path):
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    if max(width, height) < 1600 or min(width, height) < 800:
        raise AgentError("BROWSER_ARTICLE_IMAGE_QUALITY_INVALID")
    return [width, height]


def _bind_presentation(settings, payload, snapshot):
    """Служебные признаки берём из подписанного dispatch, а не из запроса."""
    metadata = snapshot.get("metadata", {})
    contract = metadata.get("presentation_contract")
    if contract is None:
        for field in _PRESENTATION_FIELDS:
            payload.pop(field, None)
        return
    illustration = metadata.get("illustration")
    media = snapshot.get("media", [])
    if (not illustrated_presentation(contract) or not isinstance(illustration, dict)
            or illustration.get("contract") != contract
            or metadata.get("illustration_proof_hash") != fingerprint(illustration)
            or len(media) != 1 or media[0].get("mime") not in {"image/png", "image/jpeg"}):
        raise AgentError("BROWSER_ARTICLE_IMAGE_INVALID")
    dimensions = [illustration.get("width"), illustration.get("height")]
    if (any(type(value) is not int for value in dimensions)
            or _image_dimensions(inside(settings.root, media[0]["path"])) != dimensions):
        raise AgentError("BROWSER_ARTICLE_IMAGE_QUALITY_INVALID")
    title = snapshot.get("title")
    if (not isinstance(title, str) or not title.strip() or "\n" in title
            or not isinstance(snapshot.get("body"), str) or not snapshot["body"].startswith(title + "\n")):
        raise AgentError("BROWSER_ARTICLE_TITLE_INVALID")
    payload.update(presentation_contract=contract,
        illustration_proof_hash=metadata["illustration_proof_hash"],
        illustration_dimensions=dimensions, title_text=title, title_format="bold")


def authorize_dispatch(settings, db, payload):
    """Check the hub outbox without consulting or enabling the legacy outbox."""
    from sqlalchemy import inspect
    from .db import now
    from .hub import HubService
    from .hub_models import Dispatch

    if not inspect(db.engine).has_table("hub_dispatches"):
        return False
    with db.session() as s:
        dispatch = s.get(Dispatch, payload["request_id"])
        if dispatch is None:
            return False
        if dispatch.state != "sending" or not dispatch.owner or dispatch.lease_until <= now() or dispatch.due_at > now():
            raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
        core = HubService(settings, db, bootstrap_targets=False)
        try:
            variant, target = core._valid_dispatch(s, dispatch)
            if (target.platform != "max" or target.route != "max_browser"
                    or target.destination != payload["chat_id"]
                    or dispatch.snapshot["body"] != payload["text"]
                    or fingerprint(dispatch.snapshot) != fingerprint(core.snapshot(variant, target))):
                raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
            media, attachment = dispatch.snapshot["media"], payload.get("attachment")
            if len(media) > 1 or bool(media) != bool(attachment):
                raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
            if media:
                expected = "browser-video" if media[0]["mime"] == "video/mp4" else "browser-image"
                if (attachment.get("type") != expected
                        or attachment.get("payload", {}).get("sha256") != media[0]["sha256"]
                        or attachment.get("payload", {}).get("path") != media[0]["path"]):
                    raise AgentError("BROWSER_DISPATCH_NOT_APPROVED")
            # Repeat the authoritative Postiz check immediately before the browser click.
            core.bridge.guard_send(dispatch, target)
            _bind_presentation(settings, payload, dispatch.snapshot)
            payload["dispatch_snapshot_hash"] = fingerprint(dispatch.snapshot)
            return True
        finally:
            core.close()


# Повторно проверяем разрешение операции перед внешним действием.
def authorize(settings, payload):
    from sqlalchemy import select
    from .db import Approval, Channel, Database, Draft, Intent

    # Обрабатываем ветвь при условии: Settings.load(settings.root).hash != settings.hash.
    if Settings.load(settings.root).hash != settings.hash:
        raise AgentError("BROWSER_CONFIG_CHANGED")
    db = Database(settings)
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        if authorize_dispatch(settings, db, payload):
            return
        for field in _PRESENTATION_FIELDS:
            payload.pop(field, None)
        payload.pop("dispatch_snapshot_hash", None)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with db.session() as session_db:
            intent = session_db.get(Intent, payload["request_id"])
            # Обрабатываем ветвь при условии: not intent or intent.state != 'sending'.
            if not intent or intent.state != "sending":
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
            draft = session_db.get(Draft, intent.draft_id)
            channel = session_db.get(Channel, intent.channel_id)
            # Обрабатываем ветвь при условии: not draft or not channel or channel.paused or (channel.chat_id != payload['chat_id']).
            if not draft or not channel or channel.paused or channel.chat_id != payload["chat_id"]:
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
            profile = settings.channel(channel.id)
            approval = session_db.scalar(select(Approval).where(
                Approval.draft_id == draft.id, Approval.revision == draft.revision,
                Approval.revoked.is_(False), Approval.config_id == settings.hash,
                Approval.content_hash == fingerprint(draft.current),
            ))
            # Обрабатываем ветвь при условии: not approval or not profile['enabled'] or profile['publication']['mode'] != 'review' or (draft.current['bod….
            if not approval or not profile["enabled"] or profile["publication"]["mode"] != "review" or draft.current["body"] != payload["text"]:
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
            video, image, attachment = draft.current.get("video"), draft.current.get("image"), payload.get("attachment")
            media = video or image
            expected_type = "browser-video" if video else "browser-image"
            # Обрабатываем ветвь при условии: video and image or bool(media) != bool(attachment) or (media and (attachment.get('type') != expected_type o….
            if (video and image) or bool(media) != bool(attachment) or (media and (
                attachment.get("type") != expected_type
                or attachment.get("payload", {}).get("sha256") != media["sha256"]
                or attachment.get("payload", {}).get("path") != media["path"]
            )):
                raise AgentError("BROWSER_INTENT_NOT_APPROVED")
    finally:
        # Завершаем работу и освобождаем ресурсы независимо от результата.
        db.engine.dispose()


# Сохраняем журнал операции перед следующим внешним действием.
def save(path, data, *, exclusive=False):
    """Сбрасываем файл и каталог на диск до следующего внешнего действия."""
    temporary = path.with_suffix(".new")
    # Оставшийся частичный файл означает прерванную запись и не считается доказательством.
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False))
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "nt":
        import ctypes

        # Windows не даёт fsync каталога через os.open; используем переименование со сбросом на диск.
        flags = 0x8 | (0 if exclusive else 0x1)  # MOVEFILE_WRITE_THROUGH / REPLACE_EXISTING
        if not ctypes.windll.kernel32.MoveFileExW(str(temporary), str(path), flags):
            raise ctypes.WinError()
    else:
        if exclusive:
            os.link(temporary, path)
            temporary.unlink()
        else:
            temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _base_payload(settings, request_id, chat_id, text, attachment):
    return {"action": "publish", "config_hash": settings.hash, "request_id": request_id,
            "chat_id": chat_id, "text": text, "attachment": attachment}


def _request_record(settings, payload):
    base = _base_payload(settings, payload["request_id"], payload["chat_id"],
                         payload["text"], payload.get("attachment"))
    return {"request_id": payload["request_id"], "chat_id": payload["chat_id"],
            "config_hash": settings.hash, "base_payload": base, "base_payload_hash": fingerprint(base),
            "request_payload": json.loads(json.dumps(payload)), "content_hash": fingerprint(payload),
            "body_sha256": hashlib.sha256(payload["text"].encode("utf-8")).hexdigest(),
            "request_attachment": payload.get("attachment"),
            "presentation": {field: payload[field] for field in _PRESENTATION_FIELDS if field in payload},
            "snapshot_hash": payload.get("dispatch_snapshot_hash")}


def _snapshot_request(settings, request_id, snapshot, chat_id):
    if (snapshot.get("platform") != "max" or snapshot.get("route") != "max_browser"
            or snapshot.get("destination") != chat_id or snapshot.get("config_hash") != settings.hash):
        raise AgentError("BROWSER_PRECLICK_PROOF_INVALID")
    media = snapshot["media"]
    if not isinstance(media, list) or len(media) > 1:
        raise AgentError("BROWSER_PRECLICK_PROOF_INVALID")
    attachment = None
    if media:
        kind = {"video/mp4": "browser-video", "image/png": "browser-image",
                "image/jpeg": "browser-image"}.get(media[0]["mime"])
        if not kind:
            raise AgentError("BROWSER_PRECLICK_PROOF_INVALID")
        attachment = {"type": kind, "payload": {"path": media[0]["path"], "sha256": media[0]["sha256"]}}
    expected = _base_payload(settings, request_id, chat_id, snapshot["body"], attachment)
    metadata = snapshot.get("metadata", {})
    contract = metadata.get("presentation_contract")
    presentation = {}
    if contract is not None:
        illustration = metadata.get("illustration")
        if (not illustrated_presentation(contract) or not isinstance(illustration, dict)
                or illustration.get("contract") != contract
                or metadata.get("illustration_proof_hash") != fingerprint(illustration)
                or not media or attachment["type"] != "browser-image"):
            raise AgentError("BROWSER_PRECLICK_PROOF_INVALID")
        presentation = {"presentation_contract": contract,
            "illustration_proof_hash": metadata["illustration_proof_hash"],
            "illustration_dimensions": [illustration["width"], illustration["height"]],
            "title_text": snapshot["title"], "title_format": "bold"}
    return expected, presentation


def read_preclick_failure(settings, request_id, *, snapshot=None, chat_id=None, text=None, attachment=None):
    """Только читаем: завершённый точный журнал доказывает, что отправка ещё не начиналась."""
    try:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("request")
        relative = "data/browser-publications/" + request_id + ".json"
        journal = settings.root / relative
        if journal.is_symlink() or not journal.is_file() or journal.stat().st_size > 8_388_608:
            raise ValueError("journal file")
        path = inside(settings.root, relative)
        if path.with_suffix(".new").exists():
            raise ValueError("interrupted write")
        raw = path.read_bytes()
        proof = parse_json(raw.decode("utf-8"))
        if (proof.get("contract") != PRECLICK_CONTRACT or proof.get("state") != "preclick_failed"
                or proof.get("send_started") is not False or proof.get("request_id") != request_id
                or proof.get("config_hash") != settings.hash or proof.get("phase") not in _PRECLICK_PHASES
                or not isinstance(proof.get("error"), dict)
                or not isinstance(proof["error"].get("code"), str) or not proof["error"]["code"]
                or not isinstance(proof["error"].get("message"), str)
                or not isinstance(proof["error"].get("type"), str)
                or type(proof.get("failed_at")) not in {int, float} or not math.isfinite(proof["failed_at"])
                or any(proof.get(field) is not None for field in
                       ("delivery", "external_id", "permalink", "receipt", "clicked_at", "sent_at", "confirmed_at"))):
            raise ValueError("terminal record")
        if snapshot is not None:
            if chat_id is None:
                chat_id = snapshot["destination"]
            expected, presentation = _snapshot_request(settings, request_id, snapshot, chat_id)
            if (proof.get("snapshot_hash") != fingerprint(snapshot)
                    or proof.get("presentation") != presentation):
                raise ValueError("snapshot")
        else:
            if chat_id is None or not isinstance(text, str):
                raise ValueError("expected request")
            expected = _base_payload(settings, request_id, chat_id, text, attachment)
        payload = proof["request_payload"]
        allowed = {"action", "config_hash", "request_id", "chat_id", "text", "attachment",
                   "dispatch_snapshot_hash", *_PRESENTATION_FIELDS}
        if (not isinstance(payload, dict) or set(payload) - allowed
                or proof.get("chat_id") != chat_id or proof.get("base_payload") != expected
                or proof.get("base_payload_hash") != fingerprint(expected)
                or proof.get("body_sha256") != hashlib.sha256(expected["text"].encode("utf-8")).hexdigest()
                or proof.get("request_attachment") != expected["attachment"]
                or proof.get("content_hash") != fingerprint(payload)
                or any(payload.get(field) != value for field, value in expected.items()
                       if field not in {"action", "config_hash"})
                or payload.get("config_hash", settings.hash) != settings.hash
                or payload.get("action", "publish") != "publish"
                or proof.get("snapshot_hash") != payload.get("dispatch_snapshot_hash")
                or proof.get("presentation") != {field: payload[field] for field in _PRESENTATION_FIELDS if field in payload}):
            raise ValueError("request binding")
        return {"proof": proof, "sha256": hashlib.sha256(raw).hexdigest()}
    except (AgentError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AgentError("BROWSER_PRECLICK_PROOF_INVALID") from exc


def _preclick_result(settings, payload):
    proof = read_preclick_failure(settings, payload["request_id"], chat_id=payload["chat_id"],
        text=payload["text"], attachment=payload.get("attachment"))
    if proof["proof"]["content_hash"] != fingerprint(payload):
        raise AgentError("BROWSER_INTENT_CONTENT_CHANGED")
    return {"state": "preclick_failed", "proof_sha256": proof["sha256"]}


# Строим путь журнала по проверенному идентификатору попытки.
def ledger_path(settings, request_id):
    # Обрабатываем ветвь при условии: not re.fullmatch('[A-Za-z0-9_-]{1,128}', request_id).
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise AgentError("BROWSER_REQUEST_ID_INVALID")
    path = inside(settings.root, "data/browser-publications/" + request_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# Фиксируем неизменный файл вложения перед отправкой.
def snapshot_attachment(settings, attachment, request_id):
    # Обрабатываем ветвь при условии: not attachment.
    if not attachment:
        return None
    kind = attachment.get("type")
    # Обрабатываем ветвь при условии: kind not in {'browser-video', 'browser-image'}.
    if kind not in {"browser-video", "browser-image"}:
        raise AgentError("BROWSER_ATTACHMENT_INVALID")
    source = inside(settings.root, attachment["payload"]["path"])
    # Обрабатываем ветвь при условии: kind == 'browser-video'.
    if kind == "browser-video":
        # Обрабатываем ветвь при условии: source.suffix.lower() != '.mp4' or source.stat().st_size > settings.app.get('video', {}).get('max_bytes', 1….
        if source.suffix.lower() != ".mp4" or source.stat().st_size > settings.app.get("video", {}).get("max_bytes", 104857600):
            raise AgentError("BROWSER_ATTACHMENT_INVALID")
    else:
        # Альтернативная ветвь после проверки основного условия.
        from .media import inspect_image

        inspect_image(settings.root, attachment["payload"]["path"])
    target = ledger_path(settings, request_id).with_suffix(source.suffix.lower())
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with source.open("rb") as original, target.open("wb") as copy:
        shutil.copyfileobj(original, copy)
    # Обрабатываем ветвь при условии: hashlib.sha256(target.read_bytes()).hexdigest() != attachment['payload']['sha256'].
    if hashlib.sha256(target.read_bytes()).hexdigest() != attachment["payload"]["sha256"]:
        raise AgentError("VIDEO_HASH_CHANGED" if kind == "browser-video" else "IMAGE_HASH_CHANGED")
    return target


# Сохраняем проверенный снимок видео для устойчивой отправки.
def snapshot_video(settings, attachment, request_id):
    """Compatibility entrypoint for existing video checks."""
    # Обрабатываем ветвь при условии: attachment and attachment.get('type') != 'browser-video'.
    if attachment and attachment.get("type") != "browser-video":
        raise AgentError("BROWSER_ATTACHMENT_INVALID")
    return snapshot_attachment(settings, attachment, request_id)


# Проверяем признаки успешной операции в интерфейсе MAX.
def receipt(page, state, *, _load_deadline=None):
    marker = state.get("presentation_contract")
    if marker is not None and not illustrated_presentation(marker):
        return None
    posts = _matched_posts(page, state["text"],
        illustrated=illustrated_presentation(marker))
    # Обрабатываем ветвь при условии: posts.count() != state['before_count'] + 1.
    if posts.count() != state["before_count"] + 1:
        return None
    result = {"state": "confirmed", "external_id": "browser-" + state["request_id"],
              "permalink": None}
    if illustrated_presentation(marker):
        scrolled = _scroll_illustration(posts.last, state)
        image_proof = illustrated_receipt(posts.last, state)
        deadline = time.monotonic() + 2
        if _load_deadline is not None:
            deadline = min(deadline, _load_deadline)
        while image_proof is None and scrolled and time.monotonic() < deadline:
            page.wait_for_timeout(100)
            posts = _matched_posts(page, state["text"], illustrated=True)
            if posts.count() != state["before_count"] + 1:
                return None
            image_proof = illustrated_receipt(posts.last, state)
        if image_proof is None:
            return None
        result["image_proof"] = image_proof
    else:
        selector = ('video, [aria-label^="Прикрепленные"]' if state.get("attachment_type") == "browser-video"
                    else 'img[src^="blob:"], img[src*="static.max"], [aria-label^="Прикрепленные"]')
        if state.get("attachment") and not posts.last.locator(selector).count():
            return None
    return result


def _max_image_source(source):
    """Проверяем origin, не сохраняем подписанный URL или его query в proof."""
    if not isinstance(source, str):
        return False
    try:
        parsed = urlsplit(source)
        if parsed.scheme == "blob":
            parsed = urlsplit(source[5:])
            hosts = {"web.max.ru", "max.ru"}
        else:
            hosts = {"static.max.ru", "i.oneme.ru"}
        return (parsed.scheme == "https" and parsed.hostname in hosts
                and not parsed.username and not parsed.password and parsed.port in {None, 443})
    except ValueError:
        return False


def _illustrated_title(post, state):
    if state.get("title_format") != "bold":
        return None
    for caption in _caption_nodes(post, state["text"]):
        if caption.is_visible():
            proof = _text_formatting_proof(caption, state.get("title_text"), state["text"])
            if proof.get("title_bold") is True and proof.get("body_unbold") is True:
                return proof
    return None


def _scroll_illustration(post, state):
    """Только чтение/прокрутка фото выше точной подписи в одном найденном посте."""
    from playwright.sync_api import Error as PlaywrightError
    title = _illustrated_title(post, state)
    if not title or type(title.get("title_top")) not in {int, float}:
        return False
    elements = post.locator(".image-placeholder, img")
    for index in range(elements.count()):
        element = elements.nth(index)
        try:
            box = element.bounding_box()
            if (not element.is_visible() or not box or box["width"] < 120 or box["height"] < 60
                    or box["y"] + box["height"] > title["title_top"] + 4):
                continue
            info = element.evaluate("element => ({placeholder: element.classList.contains('image-placeholder'), source: element.currentSrc || element.src || ''})")
            if not info.get("placeholder") and not _max_image_source(info.get("source")):
                continue
            element.scroll_into_view_if_needed(timeout=1000)
            return True
        except PlaywrightError:
            # Lazy replacement can detach the placeholder; the caller re-reads
            # the exact post and never treats the placeholder itself as proof.
            return True
    return False


def illustrated_receipt(post, state):
    """Проверяем крупное изображение перед подписью; DOM не доказывает SHA пикселей."""
    dimensions = state.get("illustration_dimensions")
    if (not illustrated_presentation(state.get("presentation_contract"))
            or state.get("attachment_type") != "browser-image" or not state.get("attachment")
            or not isinstance(dimensions, list) or len(dimensions) != 2
            or any(type(value) is not int or value <= 0 for value in dimensions)):
        return None
    title_proof = _illustrated_title(post, state)
    if title_proof is None:
        return None
    caption_top = title_proof.get("title_top")
    if type(caption_top) not in {int, float}:
        return None
    expected_ratio = dimensions[0] / dimensions[1]
    images = post.locator('img')
    for index in range(images.count()):
        image = images.nth(index)
        if not image.is_visible():
            continue
        box = image.bounding_box()
        natural = image.evaluate("element => ({width: element.naturalWidth, height: element.naturalHeight, complete: element.complete, source: element.currentSrc || element.src, declared_source: element.src})")
        if (not _max_image_source(natural.get("source"))
                or not _max_image_source(natural.get("declared_source", natural.get("source")))
                or not box or not natural.get("complete") or box["width"] < 120 or box["height"] < 60
                or max(natural["width"], natural["height"]) < 640
                or min(natural["width"], natural["height"]) < 320
                or abs((natural["width"] / natural["height"]) / expected_ratio - 1) > .2
                or box["y"] + box["height"] > caption_top + 4):
            continue
        return {"contract": state["presentation_contract"],
            "illustration_proof_hash": state.get("illustration_proof_hash"),
            "uploaded_sha256": state.get("illustration_image_sha256"),
            "natural_width": natural["width"], "natural_height": natural["height"],
            "rendered_width": box["width"], "rendered_height": box["height"],
            "image_bottom": box["y"] + box["height"], "caption_top": caption_top,
            "image_before_caption": True, "delivered_pixels_sha256_verified": False,
            "title_format": "bold", "title_proof": title_proof}
    return None


def _text_formatting_proof(element, title, text):
    """Проверяем реальную жирность текстовых узлов, не Markdown-символы."""
    if not isinstance(title, str) or not title or not text.startswith(title + "\n"):
        return {}
    return element.evaluate("""(element, expected) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      const nodes = []; let node;
      while ((node = walker.nextNode())) nodes.push(node);
      const joined = nodes.map(n => n.textContent).join('');
      if (!joined.startsWith(expected.title) ||
          (element.innerText !== expected.text && element.textContent !== expected.text)) return {};
      let offset = 0, heading = '', body = '', titleBold = true, bodyUnbold = true, lastTitle = null, lastOffset = 0;
      for (const node of nodes) {
        const value = node.textContent, length = Math.max(0, Math.min(value.length, expected.title.length - offset));
        const first = value.slice(0, length), rest = value.slice(length);
        const weight = Number.parseFloat(getComputedStyle(node.parentElement).fontWeight);
        heading += first; body += rest;
        if (first) {lastTitle = node; lastOffset = first.length;}
        if (first.trim() && !(weight >= 600)) titleBold = false;
        if (rest.trim() && !(weight < 600)) bodyUnbold = false;
        offset += value.length;
      }
      if (!lastTitle || !nodes.length) return {};
      const range = document.createRange(); range.setStart(nodes[0], 0); range.setEnd(lastTitle, lastOffset);
      return {title_text: expected.title, title_bold: heading === expected.title && titleBold,
              body_unbold: Boolean(body.trim()) && bodyUnbold, body_characters: body.trim().length,
              title_top: range.getBoundingClientRect().top};
    }""", {"title": title, "text": text})


def _wait_composer_text(page, composer, text, *, timeout_seconds=5):
    """Ждём точный доступный текст на последовательных перерисовках, ничего не меняя."""
    deadline = time.monotonic() + timeout_seconds
    exact_observations = 0
    while time.monotonic() < deadline:
        ready = composer.evaluate("element => element.isConnected && element.isContentEditable && !element.closest('[aria-busy=true]')")
        exact_observations = exact_observations + 1 if ready and composer.inner_text() == text else 0
        if exact_observations >= 2:
            return
        page.wait_for_timeout(100)
    raise AgentError("BROWSER_COMPOSER_TEXT_NOT_READY")


def _wait_composer_ready(page, composer, text, *, timeout_seconds=30):
    """Ждём завершение загрузки и готовность точного текста, не отправляем и не меняем его."""
    send = page.get_by_role("button", name="Отправить сообщение", exact=True)
    send.wait_for(state="visible", timeout=timeout_seconds * 1000)
    deadline = time.monotonic() + timeout_seconds
    pending_upload = page.get_by_role("button", name="Отменить загрузку", exact=True)
    while (not send.is_enabled() or pending_upload.count()) and time.monotonic() < deadline:
        page.wait_for_timeout(200)
    if not send.is_enabled() or pending_upload.count():
        raise AgentError("BROWSER_ATTACHMENT_NOT_READY")
    _wait_composer_text(page, composer, text)
    return send


def _wait_title_proof(page, composer, title, text, *, require_bold=True, timeout_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        proof = _text_formatting_proof(composer, title, text)
        if (composer.inner_text() == text and proof.get("body_unbold") is True
                and (not require_bold or proof.get("title_bold") is True)):
            return proof
        page.wait_for_timeout(100)
    raise AgentError("BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE")


def _bold_title(page, composer, title, text):
    """Выделяем весь логический заголовок и применяем один нативный Ctrl+B."""
    _wait_composer_text(page, composer, text)
    before = _wait_title_proof(page, composer, title, text, require_bold=False)
    if before.get("title_bold") and before.get("body_unbold"):
        return before
    if not before.get("body_unbold"):
        raise AgentError("BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE")
    selected = composer.evaluate("""(element, title) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      const nodes = []; let node;
      while ((node = walker.nextNode())) if (node.textContent) nodes.push(node);
      if (!nodes.length || !nodes.map(n => n.textContent).join('').startsWith(title)) return false;
      let remaining = title.length, end = null, endOffset = 0;
      for (const node of nodes) {
        if (remaining <= node.textContent.length) {end = node; endOffset = remaining; break;}
        remaining -= node.textContent.length;
      }
      if (!end) return false;
      element.focus();
      const range = document.createRange(); range.setStart(nodes[0], 0); range.setEnd(end, endOffset);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      return selection.toString() === title;
    }""", title)
    if selected is not True:
        raise AgentError("BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE")
    page.keyboard.press("Control+b")
    page.keyboard.press("ArrowRight")
    return _wait_title_proof(page, composer, title, text)


def _loaded_receipt(page, state):
    """После reload ждём загрузку картинки, не делая второго Send."""
    deadline = time.monotonic() + 15
    found = receipt(page, state, _load_deadline=deadline)
    if found or not illustrated_presentation(state.get("presentation_contract")):
        return found
    while time.monotonic() < deadline:
        # Header/composer могут загрузиться раньше истории. Отсутствующая
        # подпись ждёт тот же deadline; лишнее совпадение сразу блокирует proof.
        if _matched_posts(page, state["text"], illustrated=True).count() > state["before_count"] + 1:
            return None
        page.wait_for_timeout(200)
        found = receipt(page, state, _load_deadline=deadline)
        if found:
            return found
    return None


# Проверяем доступность ресурса и его разрешённые свойства.
def inspect(settings, chat_id):
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        ensure_channel(page, settings, chat_id)
        return {"publish": True, "read": False, "edit": False, "remove": False,
                "create_channel": False, "live_verified": True, "transport": "browser"}


# Сверяем сохранённую попытку без повторного нажатия отправки.
def reconcile(settings, chat_id, request_id):
    path = ledger_path(settings, request_id)
    # Обрабатываем ветвь при условии: not path.exists().
    if not path.exists():
        return {"state": "unknown"}
    state = json.loads(path.read_text(encoding="utf-8"))
    # Обрабатываем ветвь при условии: state['chat_id'] != chat_id.
    if state["chat_id"] != chat_id:
        raise AgentError("BROWSER_WRONG_CHANNEL")
    # Обрабатываем ветвь при условии: state.get('state') == 'confirmed'.
    if state.get("state") == "confirmed":
        return state["delivery"]
    if state.get("state") in {"preparing", "preclick_failed"}:
        # Журналы новой подготовки или отказа до нажатия не подтверждают доставку.
        return {"state": "unknown"}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        ensure_channel(page, settings, chat_id)
        found = _loaded_receipt(page, state)
        # Обрабатываем ветвь при условии: found.
        if found:
            page.screenshot(path=str(path.with_suffix(".png")))
            state.update(state="confirmed", delivery=found)
            save(path, state)
            return found
    return {"state": "unknown"}


# Выполняем отправку и возвращаем подтверждённый либо неопределённый результат.
def publish(settings, payload, *, authorize_before_click=None):
    request_id, _chat_id, text = payload["request_id"], payload["chat_id"], payload["text"]
    # Обрабатываем ветвь при условии: not isinstance(text, str) or not text.strip() or len(text) > 4000.
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise AgentError("BROWSER_TEXT_INVALID")
    marker = payload.get("presentation_contract")
    illustrated = illustrated_presentation(marker)
    if marker is not None and not illustrated:
        raise AgentError("BROWSER_ARTICLE_IMAGE_INVALID")
    if illustrated:
        attachment = payload.get("attachment")
        dimensions = payload.get("illustration_dimensions")
        if (not isinstance(attachment, dict) or attachment.get("type") != "browser-image"
                or not re.fullmatch(r"[a-f0-9]{64}", str(payload.get("illustration_proof_hash", "")))
                or not isinstance(dimensions, list) or len(dimensions) != 2
                or any(type(value) is not int or value <= 0 for value in dimensions)
                or max(dimensions) < 1600 or min(dimensions) < 800):
            raise AgentError("BROWSER_ARTICLE_IMAGE_INVALID")
        if (payload.get("title_format") != "bold" or not isinstance(payload.get("title_text"), str)
                or not payload["title_text"] or "\n" in payload["title_text"]
                or not text.startswith(payload["title_text"] + "\n")):
            raise AgentError("BROWSER_ARTICLE_TITLE_INVALID")
    path = ledger_path(settings, request_id)
    content_hash = fingerprint(payload)
    # Обрабатываем ветвь при условии: path.exists().
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        # Обрабатываем ветвь при условии: state['content_hash'] != content_hash.
        if state["content_hash"] != content_hash:
            raise AgentError("BROWSER_INTENT_CONTENT_CHANGED")
        if state.get("state") == "preclick_failed":
            return _preclick_result(settings, payload)
        return state["delivery"] if state.get("state") == "confirmed" else {"state": "unknown"}
    if path.with_suffix(".new").exists():
        return {"state": "unknown"}
    state = {**_request_record(settings, payload), "contract": "browser-send-journal-v1",
             "state": "preparing", "send_started": False, "phase": "session", "started_at": time.time()}
    try:
        save(path, state, exclusive=True)
    except OSError:
        # Запрос уже занят другим процессом или частичным журналом; повторно браузер не открываем.
        return {"state": "unknown"}
    try:
        return _perform_publish(settings, payload, path, state,
                                authorize_before_click=authorize_before_click)
    except Exception as exc:
        if state["send_started"]:
            return {"state": "unknown"}
        try:
            # Только наш полный журнал подготовки может подтвердить отказ до нажатия отправки.
            saved = parse_json(path.read_text(encoding="utf-8"))
            if (saved.get("state") != "preparing" or saved.get("send_started") is not False
                    or saved.get("content_hash") != content_hash
                    or saved.get("started_at") != state["started_at"]):
                return {"state": "unknown"}
            state.update(contract=PRECLICK_CONTRACT, state="preclick_failed", failed_at=time.time(),
                error={"code": exc.code if isinstance(exc, AgentError) else "BROWSER_OPERATION_FAILED",
                       "type": type(exc).__name__, "message": str(exc)})
            save(path, state)
            return _preclick_result(settings, payload)
        except Exception:
            # Потерянная или частичная запись не доказывает отсутствие внешнего действия.
            return {"state": "unknown"}


def _perform_publish(settings, payload, path, state, *, authorize_before_click):
    request_id, chat_id, text = payload["request_id"], payload["chat_id"], payload["text"]
    illustrated = illustrated_presentation(payload.get("presentation_contract"))
    with session(settings) as page:
        state["phase"] = "channel"
        ensure_channel(page, settings, chat_id)
        state["phase"] = "composer"
        composer = page.locator(COMPOSER)
        # Обрабатываем ветвь при условии: composer.inner_text().strip().
        if composer.inner_text().strip():
            raise AgentError("BROWSER_COMPOSER_NOT_EMPTY")
        # Pending attachments belonging to a human are preserved as well.
        # Обрабатываем ветвь при условии: page.get_by_role('button', name='Отменить загрузку', exact=True).count().
        if page.get_by_role("button", name="Отменить загрузку", exact=True).count():
            raise AgentError("BROWSER_COMPOSER_NOT_EMPTY")
        before = _matched_posts(page, text, illustrated=illustrated).count()
        state["phase"] = "attachment_snapshot"
        upload = snapshot_attachment(settings, payload.get("attachment"), request_id)
        if illustrated and _image_dimensions(upload) != payload["illustration_dimensions"]:
            raise AgentError("BROWSER_ARTICLE_IMAGE_QUALITY_INVALID")
        # Обрабатываем ветвь при условии: upload.
        if upload:
            state["phase"] = "attachment_upload"
            page.get_by_role("button", name="Загрузить файл", exact=True).click()
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with page.expect_file_chooser() as chooser:
                page.get_by_text("Фото или видео", exact=True).click()
            chooser.value.set_files(str(upload))
        state["phase"] = "composer_fill"
        composer.fill(text)
        state["phase"] = "composer_ready"
        send = _wait_composer_ready(page, composer, text)
        state["phase"] = "title_format"
        title_proof = _bold_title(page, composer, payload["title_text"], text) if illustrated else None
        state["phase"] = "authorization"
        ensure_channel(page, settings, chat_id, navigate=False)
        # Все ожидания завершаем до последней проверки полномочий отправителя.
        _wait_composer_text(page, composer, text)
        if illustrated:
            title_proof = _wait_title_proof(page, composer, payload["title_text"], text)
        saved = parse_json(path.read_text(encoding="utf-8"))
        if (saved.get("state") != "preparing" or saved.get("send_started") is not False
                or saved.get("content_hash") != state["content_hash"]
                or saved.get("started_at") != state["started_at"]):
            raise AgentError("BROWSER_JOURNAL_OWNERSHIP_LOST")
        if authorize_before_click:
            authorize_before_click()
        if fingerprint(payload) != state["content_hash"]:
            raise AgentError("BROWSER_INTENT_CONTENT_CHANGED")
        # После авторизации только немедленные проверки: ждать или менять текст уже нельзя.
        if (composer.inner_text() != text or not composer.evaluate(
                "element => element.isConnected && element.isContentEditable && !element.closest('[aria-busy=true]')")):
            raise AgentError("BROWSER_COMPOSER_TEXT_NOT_READY")
        if illustrated:
            title_proof = _text_formatting_proof(composer, payload["title_text"], text)
            if title_proof.get("title_bold") is not True or title_proof.get("body_unbold") is not True:
                raise AgentError("BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE")
        if not send.is_enabled() or page.get_by_role("button", name="Отменить загрузку", exact=True).count():
            raise AgentError("BROWSER_ATTACHMENT_NOT_READY")
        state.update(state="sending", send_started=True, phase="send_intent",
                     text=text, attachment=bool(upload),
                     attachment_type=payload.get("attachment", {}).get("type") if upload else None,
                     before_count=before)
        if illustrated:
            state.update({field: payload[field] for field in _PRESENTATION_FIELDS})
            state["illustration_image_sha256"] = payload["attachment"]["payload"]["sha256"]
            state["composer_title_proof"] = title_proof
        # An interrupted click must never allow another automatic click.
        save(path, state)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            state["phase"] = "send_click"
            send.click()
            state["phase"] = "receipt_observation"
            deadline = time.monotonic() + settings.app["max"]["browser"].get("operation_timeout_seconds", 120)
            # Продолжаем цикл, пока выполняется условие: time.monotonic() < deadline.
            while time.monotonic() < deadline:
                # Обрабатываем ветвь при условии: matching_posts(page, text).count() == before + 1 and (not composer.inner_text().strip()).
                if _matched_posts(page, text, illustrated=illustrated).count() == before + 1 and not composer.inner_text().strip():
                    # MAX first renders an optimistic post. Reloading immediately can
                    # abort its in-flight upload/send request and discard the post.
                    page.wait_for_timeout(5000)
                    # Обрабатываем ветвь при условии: matching_posts(page, text).count() != before + 1.
                    if _matched_posts(page, text, illustrated=illustrated).count() != before + 1:
                        continue
                    page.screenshot(path=str(path.with_suffix(".before-reload.png")))
                    state["phase"] = "receipt_reload"
                    page.reload(wait_until="domcontentloaded")
                    ensure_channel(page, settings, chat_id, navigate=False)
                    found = _loaded_receipt(page, state)
                    # Обрабатываем ветвь при условии: found.
                    if found:
                        page.screenshot(path=str(path.with_suffix(".png")))
                        state.update(state="confirmed", delivery=found)
                        save(path, state)
                        return found
                    state["error"] = "BROWSER_POST_DISAPPEARED_AFTER_RELOAD"
                    break
                page.wait_for_timeout(500)
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception as exc:
            state["error"] = "BROWSER_SEND_UNCONFIRMED"
            state["error_detail"] = {"code": exc.code if isinstance(exc, AgentError) else "BROWSER_OPERATION_FAILED",
                                     "type": type(exc).__name__, "message": str(exc)}
        # Обрабатываем ветвь при условии: not state.get('error').
        if not state.get("error"):
            state["error"] = "BROWSER_POST_NOT_OBSERVED"
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            page.screenshot(path=str(path.with_suffix(".unknown.png")))
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            state["proof_capture_failed"] = True
        state["state"] = "unknown"
        save(path, state)
        return {"state": "unknown"}


# Разбираем параметры запуска и выбираем сценарий выполнения.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        settings = Settings.load(args.root)
        # Обрабатываем ветвь при условии: settings.app['max']['adapter_mode'] != 'browser'.
        if settings.app["max"]["adapter_mode"] != "browser":
            raise AgentError("BROWSER_MODE_NOT_ENABLED")
        payload = json.loads(sys.stdin.read(1_048_577))
        # Обрабатываем ветвь при условии: payload.get('config_hash') != settings.hash.
        if payload.get("config_hash") != settings.hash:
            raise AgentError("BROWSER_CONFIG_CHANGED")
        action = payload["action"]
        # Обрабатываем ветвь при условии: action == 'inspect'.
        if action == "inspect":
            result = inspect(settings, payload["chat_id"])
        # Обрабатываем ветвь при условии: action == 'publish'.
        elif action == "publish":
            authorize(settings, payload)
            result = publish(settings, payload, authorize_before_click=lambda: authorize(settings, payload))
        # Обрабатываем ветвь при условии: action == 'reconcile'.
        elif action == "reconcile":
            result = reconcile(settings, payload["chat_id"], payload["request_id"])
        # Обрабатываем ветвь при условии: action == 'correct-caption'.
        elif action == "correct-caption":
            from .browser_corrections import edit

            result = edit(settings, payload)
        else:
            # Альтернативная ветвь после проверки основного условия.
            raise AgentError("BROWSER_ACTION_INVALID")
        print(json.dumps(result, ensure_ascii=True))
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        print(json.dumps({"code": exc.code}))
    # Обрабатываем Exception и выбираем допустимый исход.
    except Exception:
        # Raw Playwright exceptions can contain page text or browser state.
        print(json.dumps({"code": "BROWSER_OPERATION_FAILED"}))


# Обрабатываем ветвь при условии: __name__ == '__main__'.
if __name__ == "__main__":
    main()
