"""Telegram Bot API с durable шагами; квитанция API не является независимым readback.

Контракт: https://core.telegram.org/bots/api#sendmessage и #sendphoto.
Бот не умеет getMessage. Неизвестную отправку сверяет оператор; здесь нет retry.
Длинный иллюстрированный пост требует явно выбранного auto/photo_then_text.
"""

import hashlib
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .adapters import Delivery
from .config import AgentError, fingerprint


API_ORIGIN = "https://api.telegram.org"
PHOTO_MAX_BYTES = 10 * 1024 * 1024
_USERNAME = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_NUMERIC_CHAT = re.compile(r"-?[1-9][0-9]{0,15}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON")
        result[key] = value
    return result


def _utf16_length(value):
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise AgentError("TELEGRAM_TEXT_INVALID") from None


def _configured_proxy(options):
    """Only an explicitly named dedicated env may enable a local HTTP proxy."""
    if "proxy_env" not in options:
        return None
    env = options["proxy_env"]
    if (not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env)
            or env in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}):
        raise AgentError("TELEGRAM_PROXY_ENV_INVALID")
    value = os.getenv(env, "")
    if not value:
        raise AgentError("TELEGRAM_PROXY_REQUIRED", 503)
    try:
        if len(value) > 256 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
        host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
        return "http://" + host + ":" + str(parsed.port)
    except (ValueError, TypeError):
        # Neither the URL nor a possible accidentally supplied password is echoed.
        raise AgentError("TELEGRAM_PROXY_INVALID") from None


class TelegramPublisher:
    """Отправка в назначенный chat_id без изменения текста, адресата или числа постов."""

    def __init__(self, root: Path, options: dict, transport=None):
        self.root = Path(root).resolve()
        self.options = dict(options)
        self._fixture = transport is not None
        adapter_mode = self.options.get("adapter_mode", "fake")
        if adapter_mode not in {"fake", "live"}:
            raise AgentError("TELEGRAM_ADAPTER_MODE_INVALID")
        self.mode = "fixture" if self._fixture else (
            "telegram_api" if adapter_mode == "live" else "fake")
        self._layout = self.options.get("media_layout", "atomic")
        if self._layout not in {"atomic", "photo_then_text", "auto"}:
            raise AgentError("TELEGRAM_MEDIA_LAYOUT_INVALID")
        env = self.options.get("token_env", "TELEGRAM_BOT_TOKEN")
        if not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
            raise AgentError("TELEGRAM_TOKEN_ENV_INVALID")
        # Токен читается только из окружения, не из snapshot или параметров адресата.
        self._token = os.getenv(env, "")
        timeout = self.options.get("request_timeout_seconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise AgentError("TELEGRAM_TIMEOUT_INVALID")
        self._proxy_url = _configured_proxy(self.options)
        # httpx proxy mounts override an injected transport. A fixture must never
        # fall through to a live proxy/network, even when local config names one.
        proxy = None if self._fixture else self._proxy_url
        self.client = httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                                   verify=True, proxy=proxy, transport=transport)

    @staticmethod
    def _destination(destination):
        if not isinstance(destination, str) or not (
                _NUMERIC_CHAT.fullmatch(destination) or _USERNAME.fullmatch(destination)):
            raise AgentError("TELEGRAM_DESTINATION_INVALID")
        return destination

    @staticmethod
    def _chat_matches(chat, destination):
        if not isinstance(chat, dict) or type(chat.get("id")) is not int or not chat["id"]:
            return False
        if destination.startswith("@"):
            username = chat.get("username")
            return isinstance(username, str) and username.casefold() == destination[1:].casefold()
        return chat["id"] == int(destination)

    def _image(self, value):
        if not isinstance(value, dict) or not {"path", "sha256", "mime"} <= value.keys():
            raise AgentError("TELEGRAM_MEDIA_INVALID")
        relative, expected, mime = (value[key] for key in ("path", "sha256", "mime"))
        if (not isinstance(relative, str) or not relative or ":" in relative or Path(relative).is_absolute()
                or not isinstance(expected, str) or not _SHA256.fullmatch(expected)
                or mime not in {"image/jpeg", "image/png"}):
            raise AgentError("TELEGRAM_MEDIA_INVALID")
        try:
            path = (self.root / relative).resolve(strict=True)
            if not path.is_relative_to(self.root) or not path.is_file():
                raise AgentError("TELEGRAM_MEDIA_OUTSIDE_PROJECT")
            # Проверяем и отправляем одни байты: повторное открытие пути не меняет upload.
            with path.open("rb") as stream:
                raw = stream.read(PHOTO_MAX_BYTES + 1)
        except OSError:
            raise AgentError("TELEGRAM_MEDIA_NOT_READY") from None
        if not raw or len(raw) > PHOTO_MAX_BYTES:
            raise AgentError("TELEGRAM_PHOTO_SIZE_INVALID")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise AgentError("TELEGRAM_MEDIA_HASH_CHANGED", 409)
        try:
            with Image.open(io.BytesIO(raw)) as image:
                actual = {"JPEG": "image/jpeg", "PNG": "image/png"}.get(image.format)
                width, height = image.size
                if (actual != mime or width <= 0 or height <= 0
                        or width + height > 10000 or max(width, height) / min(width, height) > 20
                        or getattr(image, "n_frames", 1) != 1):
                    raise AgentError("TELEGRAM_PHOTO_FORMAT_INVALID")
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise AgentError("TELEGRAM_PHOTO_FORMAT_INVALID") from None
        return {"bytes": raw, "mime": mime, "sha256": expected,
                "filename": "photo.jpg" if mime == "image/jpeg" else "photo.png"}

    def _prepare(self, destination, snapshot, *, read_image=True):
        self._destination(destination)
        if not isinstance(snapshot, dict):
            raise AgentError("TELEGRAM_SNAPSHOT_INVALID")
        if "destination" in snapshot and snapshot["destination"] != destination:
            raise AgentError("TELEGRAM_SNAPSHOT_DESTINATION_CHANGED", 409)
        title, body = snapshot.get("title"), snapshot.get("body")
        if (not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title
                or not isinstance(body, str) or not body.strip() or "\x00" in title + body):
            raise AgentError("TELEGRAM_TEXT_INVALID")
        # Уже включённый заголовок остаётся частью exact body и не дублируется.
        text = body if body == title or body.startswith(title + "\n") else title + "\n\n" + body
        entities = [{"type": "bold", "offset": 0, "length": _utf16_length(title)}]
        media = snapshot.get("media", [])
        if not isinstance(media, list) or len(media) > 1:
            raise AgentError("TELEGRAM_SINGLE_PHOTO_REQUIRED")
        if media and (not isinstance(media[0], dict)
                      or not {"path", "sha256", "mime"} <= media[0].keys()):
            raise AgentError("TELEGRAM_MEDIA_INVALID")
        length = _utf16_length(text)
        if media and length > 1024 and self._layout == "atomic":
            # Не делим на фото и текст: второй внешний эффект требует durable journal.
            raise AgentError("TELEGRAM_PHOTO_CAPTION_ATOMIC_UNSUPPORTED")
        if length > 4096:
            raise AgentError("TELEGRAM_TEXT_TOO_LONG")
        image = (self._image(media[0]) if read_image else dict(media[0])) if media else None
        common = {"destination": destination, "text": text, "entities": entities,
                  "utf16_length": length, "image": image}
        separate = image and (self._layout == "photo_then_text" or length > 1024)
        if separate:
            steps = [{**common, "text": "", "entities": [], "utf16_length": 0},
                     {**common, "image": None}]
        else:
            steps = [common]
        return {**common, "steps": steps}

    def validate(self, destination, snapshot):
        """Локальная проверка байтов и лимитов; не обращается к Telegram и не отправляет."""
        prepared = self._prepare(destination, snapshot)
        return {"valid": True, "method": "sendPhoto" if prepared["image"] else "sendMessage",
                "utf16_length": prepared["utf16_length"], "message_count": len(prepared["steps"]),
                "verification": "preflight_only"}

    def _request(self, method, parameters, *, mutation=False, files=None):
        if self.mode == "fake":
            raise AgentError("TELEGRAM_LIVE_MODE_REQUIRED", 503)
        if not self._token:
            raise AgentError("TELEGRAM_TOKEN_REQUIRED", 503)
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", self._token):
            raise AgentError("TELEGRAM_TOKEN_INVALID", 503)
        url = API_ORIGIN + "/bot" + self._token + "/" + method
        unknown = "TELEGRAM_UNKNOWN" if mutation else "TELEGRAM_INSPECTION_UNAVAILABLE"
        try:
            if not mutation:
                response = self.client.get(url, params=parameters)
            elif files is not None:
                response = self.client.post(url, data=parameters, files=files)
            else:
                response = self.client.post(url, json=parameters)
        except httpx.HTTPError:
            # В цепочку исключения не попадает Request с URL, содержащим токен.
            raise AgentError(unknown, 503) from None
        if response.is_redirect or response.status_code >= 500:
            raise AgentError(unknown, 503)
        if response.status_code == 429:
            raise AgentError("TELEGRAM_RATE_LIMIT", 429)
        if response.status_code in {401, 403}:
            raise AgentError("TELEGRAM_ACCESS_DENIED", 403)
        try:
            if len(response.content) > 1024 * 1024:
                raise ValueError
            payload = json.loads(response.content, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            raise AgentError(unknown, 503) from None
        if not isinstance(payload, dict):
            raise AgentError(unknown, 503)
        if payload.get("ok") is False and type(payload.get("error_code")) is int:
            code = payload["error_code"]
            if code == 429:
                raise AgentError("TELEGRAM_RATE_LIMIT", 429)
            if code in {401, 403}:
                raise AgentError("TELEGRAM_ACCESS_DENIED", 403)
            if 400 <= code < 500:
                raise AgentError("TELEGRAM_REJECTED")
            raise AgentError(unknown, 503)
        if response.status_code != 200 or payload.get("ok") is not True:
            raise AgentError(unknown, 503)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AgentError(unknown, 503)
        return result

    def inspect(self, destination):
        """Явная read-only проверка бота и его права писать; не доказательство доставки."""
        self._destination(destination)
        bot = self._request("getMe", {})
        bot_id = bot.get("id")
        if type(bot_id) is not int or bot_id <= 0 or bot.get("is_bot") is not True:
            raise AgentError("TELEGRAM_INSPECTION_INVALID", 503)
        chat = self._request("getChat", {"chat_id": destination})
        if not self._chat_matches(chat, destination):
            raise AgentError("TELEGRAM_INSPECTION_INVALID", 503)
        member = self._request("getChatMember", {"chat_id": destination, "user_id": bot_id})
        user = member.get("user")
        if not isinstance(user, dict) or user.get("id") != bot_id or user.get("is_bot") is not True:
            raise AgentError("TELEGRAM_INSPECTION_INVALID", 503)
        # В этой версии инспекция разрешает только канал с явным правом публикации.
        allowed = chat.get("type") == "channel" and (
            member.get("status") == "creator" or (
                member.get("status") == "administrator" and member.get("can_post_messages") is True))
        return {"platform": "telegram", "destination": destination, "chat_id": chat["id"],
                "can_publish": allowed, "publish": allowed, "verification": "access_only",
                "live_verified": False, "fixture": self._fixture,
                "proxy_configured": self._proxy_url is not None}

    @staticmethod
    def _receipt_matches(result, prepared):
        if (not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] <= 0
                or not TelegramPublisher._chat_matches(result.get("chat"), prepared["destination"])):
            return False
        image = prepared["image"]
        field, entity_field = ("caption", "caption_entities") if image else ("text", "entities")
        if result.get(field, "" if image else None) != prepared["text"]:
            return False
        entities = result.get(entity_field, [] if image and not prepared["text"] else None)
        if not isinstance(entities, list) or any(not isinstance(item, dict) for item in entities):
            return False
        if any(type(item.get("offset")) is not int or type(item.get("length")) is not int
               or item["offset"] < 0 or item["length"] <= 0
               or item["offset"] + item["length"] > prepared["utf16_length"] for item in entities):
            return False
        # Telegram вправе добавить url/mention, но не оформлять обычное тело жирным.
        bold = [item for item in entities if item.get("type") == "bold"]
        if bold != prepared["entities"]:
            return False
        allowed = {"bold", "mention", "hashtag", "cashtag", "bot_command", "url", "email",
                   "phone_number", "bank_card"}
        if any(item.get("type") not in allowed for item in entities):
            return False
        if image:
            photos = result.get("photo")
            if not isinstance(photos, list) or not photos:
                return False
            for photo in photos:
                if (not isinstance(photo, dict) or not isinstance(photo.get("file_id"), str)
                        or not photo["file_id"] or type(photo.get("width")) is not int
                        or type(photo.get("height")) is not int or min(photo["width"], photo["height"]) <= 0):
                    return False
            if result.get("show_caption_above_media") is True:
                return False
        elif result.get("photo"):
            return False
        return True

    def _journal_path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise AgentError("TELEGRAM_REQUEST_ID_INVALID")
        path = (self.root / "data" / "telegram-deliveries" / request_id / "journal.json").resolve()
        if not path.is_relative_to(self.root):
            raise AgentError("TELEGRAM_JOURNAL_OUTSIDE_PROJECT")
        return path

    @staticmethod
    def _write_journal(path, journal):
        """Сначала fsync полного claim, затем атомарная замена; без повторной мутации API."""
        temporary = path.with_name("journal.pending.json")
        try:
            raw = json.dumps(journal, ensure_ascii=False, sort_keys=True).encode()
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            raise AgentError("TELEGRAM_JOURNAL_UNAVAILABLE", 503) from None

    def _binding(self, destination, snapshot):
        self._destination(destination)
        try:
            return fingerprint({"destination": destination, "snapshot": snapshot,
                                "media_layout": self._layout, "fixture": self._fixture})
        except (TypeError, ValueError, UnicodeError):
            raise AgentError("TELEGRAM_SNAPSHOT_INVALID") from None

    @staticmethod
    def _plan(prepared):
        return [{"method": "sendPhoto" if step["image"] else "sendMessage",
                 "text_sha256": hashlib.sha256(step["text"].encode()).hexdigest(),
                 "entities": step["entities"],
                 "photo_sha256": step["image"]["sha256"] if step["image"] else None}
                for step in prepared["steps"]]

    def _delivery(self, journal, state):
        confirmed = [step for step in journal.get("steps", []) if step.get("state") == "confirmed"]
        receipts = [step["receipt"] for step in confirmed]
        final = receipts[-1] if receipts else None
        message_id = final["message_id"] if final else None
        username = final.get("chat", {}).get("username") if final else None
        permalink = ("https://t.me/" + username + "/" + str(message_id)
                     if isinstance(username, str) and _USERNAME.fullmatch("@" + username) else None)
        evidence = {"platform": "telegram", "verification": "api_receipt",
                    "fixture": self._fixture, "independent_readback": False, "live_verified": False,
                    "request_id": journal["request_id"], "binding_hash": journal["binding_hash"],
                    "message_ids": [item["message_id"] for item in receipts],
                    "message_count": len(journal.get("plan", [])),
                    "journal_state": journal["state"], "plan": journal.get("plan", []),
                    "failure_code": journal.get("failure_code")}
        if final:
            evidence["chat_id"] = final["chat"]["id"]
            evidence["message_id"] = message_id
        if len(journal.get("plan", [])) == 1:
            evidence.update(journal["plan"][0])
            if journal["plan"][0]["photo_sha256"]:
                evidence["uploaded_photo_sha256"] = journal["plan"][0]["photo_sha256"]
        return Delivery(state, str(message_id) if message_id else None, permalink,
                        verified=False, evidence=evidence)

    def reconcile(self, destination, snapshot, request_id):
        """Только чтение exact journal: незавершённую последовательность не продолжаем."""
        path = self._journal_path(request_id)
        binding = self._binding(destination, snapshot)
        fallback = {"schema_version": 1, "request_id": request_id, "binding_hash": binding,
                    "state": "unknown", "steps": [], "plan": []}
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError
            journal = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        except (OSError, ValueError, UnicodeError):
            return self._delivery(fallback, "unknown")
        if not isinstance(journal, dict):
            return self._delivery(fallback, "unknown")
        if journal.get("binding_hash") != binding:
            raise AgentError("TELEGRAM_REQUEST_BINDING_CHANGED", 409)
        try:
            prepared = self._prepare(destination, snapshot, read_image=False)
            expected_plan = self._plan(prepared)
            if (journal.get("schema_version") != 1 or journal.get("request_id") != request_id
                    or journal.get("destination") != destination or journal.get("plan") != expected_plan
                    or not isinstance(journal.get("steps"), list)
                    or len(journal["steps"]) != len(expected_plan)):
                raise ValueError
            for index, step in enumerate(journal["steps"]):
                if not isinstance(step, dict) or step.get("state") not in {
                        "pending", "claimed", "confirmed", "failed", "unknown"}:
                    raise ValueError
                if step["state"] == "confirmed" and not self._receipt_matches(
                        step.get("receipt", {}), prepared["steps"][index]):
                    raise ValueError
            identifiers = [step["receipt"]["message_id"] for step in journal["steps"]
                           if step["state"] == "confirmed"]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError
            done = journal.get("state") == "done" and all(
                step["state"] == "confirmed" for step in journal["steps"])
            return self._delivery(journal, "confirmed" if done else "unknown")
        except (ValueError, TypeError, KeyError, AgentError):
            return self._delivery(fallback, "unknown")

    def _send_step(self, prepared):
        parameters = {"chat_id": prepared["destination"]}
        image = prepared["image"]
        if image:
            method = "sendPhoto"
            parameters.update(show_caption_above_media="false")
            if prepared["text"]:
                parameters.update(caption=prepared["text"],
                                  caption_entities=json.dumps(prepared["entities"]))
            files = {"photo": (image["filename"], image["bytes"], image["mime"])}
            result = self._request(method, parameters, mutation=True, files=files)
        else:
            method = "sendMessage"
            parameters.update(text=prepared["text"], entities=prepared["entities"],
                              link_preview_options={"is_disabled": True})
            result = self._request(method, parameters, mutation=True)
        if not self._receipt_matches(result, prepared):
            raise AgentError("TELEGRAM_UNKNOWN", 503)
        return result

    @staticmethod
    def _authorize(authorize):
        if authorize is None:
            return
        try:
            authorize()
        except AgentError:
            raise
        except Exception:
            # Database/guard failures must stop the next step without leaking
            # exception text or losing already persisted receipt identifiers.
            raise AgentError("TELEGRAM_AUTHORIZATION_UNAVAILABLE", 503) from None

    def publish(self, destination, snapshot, request_id, *, authorize=None):
        """Durable claims разрешают одну последовательность; повтор никогда не досылает шаги."""
        if authorize is not None and not callable(authorize):
            raise AgentError("TELEGRAM_AUTHORIZER_INVALID")
        path = self._journal_path(request_id)
        binding = self._binding(destination, snapshot)
        # Существующий request_id только сверяем, даже если процесс упал между mkdir и claim.
        if path.parent.exists():
            return self.reconcile(destination, snapshot, request_id)
        prepared = self._prepare(destination, snapshot)
        # Ошибки конфигурации должны завершиться прежде резервирования request_id.
        if self.mode == "fake":
            raise AgentError("TELEGRAM_LIVE_MODE_REQUIRED", 503)
        if not self._token:
            raise AgentError("TELEGRAM_TOKEN_REQUIRED", 503)
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", self._token):
            raise AgentError("TELEGRAM_TOKEN_INVALID", 503)
        # Production registry supplies an exact lease/approval/pause check.
        # The callback closes its transaction before any local claim or HTTP.
        self._authorize(authorize)
        try:
            path.parent.parent.mkdir(parents=True, exist_ok=True)
            path.parent.mkdir()
        except FileExistsError:
            return self.reconcile(destination, snapshot, request_id)
        except OSError:
            raise AgentError("TELEGRAM_JOURNAL_UNAVAILABLE", 503) from None
        journal = {"schema_version": 1, "request_id": request_id, "binding_hash": binding,
                   "destination": destination, "state": "sending", "media_layout": self._layout,
                   "fixture": self._fixture, "plan": self._plan(prepared),
                   "steps": [{"state": "pending"} for _ in prepared["steps"]]}
        self._write_journal(path, journal)
        for index, step in enumerate(prepared["steps"]):
            try:
                self._authorize(authorize)
                journal["steps"][index]["state"] = "claimed"
                self._write_journal(path, journal)
                # Pause, approval and lease can change during durable fsync.
                # Recheck after the claim and immediately before this step's HTTP.
                self._authorize(authorize)
            except AgentError as exc:
                journal["steps"][index]["state"] = "failed"
                journal["state"] = "failed" if index == 0 else "unknown"
                journal["failure_code"] = exc.code
                try:
                    self._write_journal(path, journal)
                except AgentError:
                    journal["state"] = "unknown"
                if index == 0:
                    raise
                return self._delivery(journal, "unknown")
            try:
                result = self._send_step(step)
                if any(previous.get("receipt", {}).get("message_id") == result["message_id"]
                       for previous in journal["steps"][:index]):
                    raise AgentError("TELEGRAM_UNKNOWN", 503)
            except AgentError as exc:
                known = exc.code in {"TELEGRAM_RATE_LIMIT", "TELEGRAM_ACCESS_DENIED", "TELEGRAM_REJECTED"}
                journal["steps"][index]["state"] = "failed" if known else "unknown"
                journal["state"] = "failed" if known and index == 0 else "unknown"
                journal["failure_code"] = exc.code
                try:
                    self._write_journal(path, journal)
                except AgentError:
                    journal["state"] = "unknown"
                if known and index == 0:
                    raise
                return self._delivery(journal, "unknown")
            journal["steps"][index] = {"state": "confirmed", "receipt": result}
            try:
                self._write_journal(path, journal)
            except AgentError:
                journal["state"] = "unknown"
                journal["failure_code"] = "TELEGRAM_JOURNAL_UNAVAILABLE"
                return self._delivery(journal, "unknown")
        journal["state"] = "done"
        try:
            self._write_journal(path, journal)
        except AgentError:
            journal["state"] = "unknown"
            journal["failure_code"] = "TELEGRAM_JOURNAL_UNAVAILABLE"
            return self._delivery(journal, "unknown")
        return self._delivery(journal, "confirmed")

    def close(self):
        self.client.close()
