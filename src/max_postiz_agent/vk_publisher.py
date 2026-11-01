"""Публикация в сообщество VK без повторов и с отдельным чтением результата.

Контракт: официальная схема VKCOM/vk-api-schema, версия 5.199, методы
photos.getWallUploadServer, photos.saveWallPhoto, wall.post и wall.getById.
Для этого маршрута нужен пользовательский OAuth token с wall/photos и
правами редактора/администратора сообщества. Проверка доступа не публикует пост.
"""

import hashlib
import io
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .adapters import Delivery
from .config import AgentError, fingerprint, inside


API_BASE = "https://api.vk.com/method/"
API_VERSION = "5.199"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_UNITS = 16384
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Только серверы загрузки VK: произвольные домены и CDN для чтения не подходят.
UPLOAD_HOST = re.compile(r"pu[0-9]*\.(?:vk\.com|vkuserphoto\.ru|vkuserphoto\.net)\Z")
_API_METHODS = frozenset({"groups.getById", "account.getAppPermissions",
                          "photos.getWallUploadServer", "photos.saveWallPhoto",
                          "wall.post", "wall.getById"})


class _Failure(Exception):
    """Внутренняя ошибка содержит только код, без token, URL и ответа сервера."""

    def __init__(self, code, *, uncertain=False, api_code=None):
        self.code, self.uncertain, self.api_code = code, uncertain, api_code
        super().__init__(code)


@dataclass(frozen=True)
class _Prepared:
    owner_id: int
    text: str
    media: dict | None
    image: bytes | None


def _integer(value, *, positive=False):
    return (type(value) is int and abs(value) < 2**63
            and (value > 0 if positive else value != 0))


def _community(destination):
    if (not isinstance(destination, str)
            or not re.fullmatch(r"-[1-9][0-9]{0,18}", destination)
            or not _integer(int(destination))):
        raise AgentError("VK_COMMUNITY_ID_INVALID")
    return int(destination)


def _guid(request_id):
    if (not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)):
        raise AgentError("VK_REQUEST_ID_INVALID")
    return hashlib.sha256(b"vk-wall-post-v1\0" + request_id.encode()).hexdigest()[:32]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("invalid number")


def _json(raw):
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_constant)


def _upload_url(value):
    """Не передаём авторизацию в URL и не следуем перенаправлениям загрузчика."""
    try:
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in unquote(value))):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or not UPLOAD_HOST.fullmatch(parsed.hostname)
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443} or parsed.fragment
                or not parsed.path.startswith("/") or "\\" in value):
            raise ValueError
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        names = [name.lower() for name, _ in query]
        if (len(names) != len(set(names))
                or any(not re.fullmatch(r"[a-z0-9_]+", name) for name in names)
                or set(names) & {"access_token", "authorization", "password", "redirect_uri",
                                "redirect_url", "callback", "url"}):
            raise ValueError
    except (ValueError, UnicodeError):
        raise _Failure("VK_UPLOAD_URL_INVALID") from None
    return value


class VKPublisher:
    """Самостоятельный транспорт; durable claim и approval остаются у Hub."""

    def __init__(self, root: Path, options: dict, transport=None):
        self.root, self.options = Path(root), dict(options)
        self.mode = self.options.get("adapter_mode", "fake")
        self.api_version = self.options.get("api_version", API_VERSION)
        token_env = self.options.get("token_env", "VK_ACCESS_TOKEN")
        timeout = self.options.get("request_timeout_seconds", 30)
        if (self.mode not in {"fake", "live"}
                or not isinstance(self.api_version, str)
                or not re.fullmatch(r"5\.[1-9][0-9]{0,2}", self.api_version)
                or not isinstance(token_env, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", token_env)
                or type(timeout) not in {int, float} or not math.isfinite(timeout)
                or not 0 < timeout <= 300):
            raise AgentError("VK_CONFIG_INVALID")
        self.token = os.getenv(token_env, "")
        self.fixture = transport is not None
        client_options = {"timeout": timeout, "trust_env": False, "follow_redirects": False}
        self.client = httpx.Client(transport=transport or httpx.HTTPTransport(retries=0),
                                   **client_options)
        # Cookies API и заголовки авторизации не переходят на сервер фото.
        self.upload_client = httpx.Client(transport=transport or httpx.HTTPTransport(retries=0),
                                          **client_options)
        self._attempted = {}

    def _prepare(self, destination, snapshot):
        owner = _community(destination)
        if (not isinstance(snapshot, dict) or snapshot.get("platform", "vk") != "vk"
                or snapshot.get("destination", destination) != destination
                or not isinstance(snapshot.get("body"), str)
                or not snapshot["body"].strip()
                or not isinstance(snapshot.get("title", ""), str)):
            raise AgentError("VK_SNAPSHOT_INVALID")
        title, body = snapshot.get("title", ""), snapshot["body"]
        # VK wall не поддерживает настоящее жирное начертание: заголовок обычный.
        # Уже присутствующий точный заголовок не дублируем; текст не обрезаем.
        text = (body if not title or body == title or body.startswith(title + "\n")
                else title + "\n\n" + body)
        try:
            units = len(text.encode("utf-16-le")) // 2
        except UnicodeError:
            raise AgentError("VK_TEXT_INVALID") from None
        if "\0" in text or units > MAX_MESSAGE_UNITS:
            raise AgentError("VK_TEXT_TOO_LONG" if units > MAX_MESSAGE_UNITS
                             else "VK_TEXT_INVALID")
        media = snapshot.get("media", [])
        if not isinstance(media, list) or len(media) > 1:
            raise AgentError("VK_SINGLE_IMAGE_REQUIRED")
        if not media:
            return _Prepared(owner, text, None, None)
        item = media[0]
        if (not isinstance(item, dict) or set(item) != {"path", "mime", "sha256"}
                or not isinstance(item["path"], str) or not item["path"]
                or Path(item["path"]).is_absolute()
                or not isinstance(item["mime"], str)
                or item["mime"] not in {"image/jpeg", "image/png"}
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise AgentError("VK_IMAGE_INVALID")
        path = inside(self.root, item["path"])
        allowed = {"image/jpeg": {".jpg", ".jpeg"}, "image/png": {".png"}}
        if path.suffix.lower() not in allowed[item["mime"]]:
            raise AgentError("VK_IMAGE_INVALID")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_IMAGE_BYTES + 1)
            if not 0 < len(raw) <= MAX_IMAGE_BYTES:
                raise AgentError("VK_IMAGE_SIZE_INVALID")
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise AgentError("VK_IMAGE_HASH_MISMATCH")
            # Проверяем те же байты, которые затем уйдут в multipart; повторного
            # открытия изменяемого файла между проверкой и загрузкой нет.
            with Image.open(io.BytesIO(raw)) as image:
                expected = "JPEG" if item["mime"] == "image/jpeg" else "PNG"
                if (image.format != expected or image.width * image.height > 40_000_000
                        or getattr(image, "n_frames", 1) != 1):
                    raise AgentError("VK_IMAGE_INVALID")
                image.verify()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise AgentError("VK_IMAGE_INVALID") from None
        return _Prepared(owner, text, dict(item), raw)

    def validate(self, destination, snapshot):
        """Локальная проверка: без API, секретов, записи файлов и публикации."""
        prepared = self._prepare(destination, snapshot)
        return {"destination": str(prepared.owner_id), "text": prepared.text,
                "media": [prepared.media] if prepared.media else [],
                "title_format": "plain", "native_bold": False}

    @staticmethod
    def _decode(response, *, mutation):
        if response.status_code == 429:
            raise _Failure("VK_RATE_LIMIT")
        if response.status_code in {401, 403}:
            raise _Failure("VK_ACCESS_DENIED")
        if response.status_code != 200:
            raise _Failure("VK_TRANSPORT_UNKNOWN" if mutation else "VK_UNAVAILABLE",
                           uncertain=mutation)
        try:
            data = _json(response.content)
        except (ValueError, TypeError, UnicodeError):
            raise _Failure("VK_RESPONSE_INVALID", uncertain=mutation) from None
        if not isinstance(data, dict):
            raise _Failure("VK_RESPONSE_INVALID", uncertain=mutation)
        error = data.get("error")
        if error is not None:
            if not isinstance(error, dict) or type(error.get("error_code")) is not int:
                raise _Failure("VK_RESPONSE_INVALID", uncertain=mutation)
            number = error["error_code"]
            code = ({14: "VK_CAPTCHA_REQUIRED", 17: "VK_VALIDATION_REQUIRED",
                     6: "VK_RATE_LIMIT", 9: "VK_RATE_LIMIT", 29: "VK_RATE_LIMIT",
                     5: "VK_AUTH_REQUIRED", 7: "VK_ACCESS_DENIED", 15: "VK_ACCESS_DENIED",
                     27: "VK_ACCESS_DENIED", 28: "VK_ACCESS_DENIED"}.get(number,
                                                                            "VK_API_REJECTED"))
            raise _Failure(code, uncertain=mutation and number in {1, 10, 13},
                           api_code=number)
        return data

    def _api(self, method, params, *, mutation=False):
        if method not in _API_METHODS:
            raise _Failure("VK_METHOD_INVALID")
        if self.mode != "live" or not self.token:
            raise _Failure("VK_LIVE_MODE_REQUIRED" if self.mode != "live"
                           else "VK_TOKEN_REQUIRED")
        try:
            response = self.client.post(API_BASE + method,
                                        data={**params, "access_token": self.token,
                                              "v": self.api_version})
        except httpx.HTTPError:
            raise _Failure("VK_TRANSPORT_UNKNOWN" if mutation else "VK_UNAVAILABLE",
                           uncertain=mutation) from None
        value = self._decode(response, mutation=mutation)
        if "response" not in value:
            raise _Failure("VK_RESPONSE_INVALID", uncertain=mutation)
        return value["response"]

    def inspect(self, destination):
        """Только чтение: права приложения и права пользователя в сообществе."""
        owner = _community(destination)
        empty = {"publish": False, "read": False, "edit": False, "remove": False,
                 "create_channel": False, "live_verified": False,
                 "native_bold": False, "image": False, "fixture": self.fixture}
        if self.mode != "live":
            return {**empty, "state": "offline"}
        try:
            value = self._api("groups.getById", {"group_ids": -owner,
                              "fields": "can_post,is_admin,admin_level"})
            groups = value.get("groups") if isinstance(value, dict) else value
            if (not isinstance(groups, list) or len(groups) != 1
                    or not isinstance(groups[0], dict) or groups[0].get("id") != -owner):
                raise _Failure("VK_COMMUNITY_ACCESS_UNCONFIRMED")
            group = groups[0]
            scopes = self._api("account.getAppPermissions", {})
            if type(scopes) is not int or scopes < 0:
                raise _Failure("VK_PERMISSION_RESPONSE_INVALID")
            admin = (group.get("is_admin") == 1 and type(group.get("admin_level")) is int
                     and group["admin_level"] >= 2 and not group.get("deactivated"))
            allowed = admin and group.get("can_post") == 1 and bool(scopes & 8192)
            return {**empty, "state": "available", "read": True, "publish": allowed,
                    "image": allowed and bool(scopes & 4),
                    "live_verified": not self.fixture}
        except _Failure as exc:
            raise AgentError(exc.code, 503) from None

    def _journal_path(self, request_id):
        _guid(request_id)
        # Проверяем исходные компоненты до resolve(): resolve скрывал бы ссылку
        # на другой файл проекта и позволял заменить его при os.replace.
        parent = self.root.resolve()
        try:
            for name in ("data", "vk-deliveries", request_id + ".json"):
                parent = parent / name
                if (parent.is_symlink()
                        or getattr(parent, "is_junction", lambda: False)()):
                    raise _Failure("VK_JOURNAL_PATH_UNSAFE", uncertain=True)
            return inside(self.root, "data/vk-deliveries/" + request_id + ".json")
        except (OSError, ValueError, AgentError):
            raise _Failure("VK_JOURNAL_PATH_UNSAFE", uncertain=True) from None

    def _read_journal(self, request_id, binding):
        try:
            path = self._journal_path(request_id)
            if path.is_symlink() or path.stat().st_size > 16384:
                raise ValueError
            value = _json(path.read_bytes())
            if (not isinstance(value, dict) or value.get("binding") != binding
                    or value.get("request_id") != request_id
                    or type(value.get("version")) is not int or value["version"] != 1
                    or set(value) - {"version", "request_id", "binding", "stage", "state",
                                     "external_id", "photo", "evidence", "record_hash"}
                    or value.get("record_hash") != fingerprint(
                        {k: v for k, v in value.items() if k != "record_hash"})
                    or value.get("stage") not in {"claimed", "upload_sending", "save_sending",
                        "photo_saved", "post_sending", "post_accepted", "completed", "reconciled"}
                    or value.get("state") not in {"sending", "unknown", "rejected", "confirmed"}):
                raise ValueError
            photo = value.get("photo")
            if photo is not None and (not isinstance(photo, list) or len(photo) != 2
                    or not _integer(photo[0]) or not _integer(photo[1], positive=True)):
                raise ValueError
            external_id = value.get("external_id")
            if external_id is not None and (not isinstance(external_id, str)
                    or not re.fullmatch(r"-[1-9][0-9]{0,18}_[1-9][0-9]{0,18}", external_id)):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError, UnicodeError, AgentError):
            raise _Failure("VK_JOURNAL_UNVERIFIED", uncertain=True) from None

    def _claim(self, request_id, binding):
        path = self._journal_path(request_id)
        value = {"version": 1, "request_id": request_id, "binding": binding, "stage": "claimed",
                 "state": "sending", "external_id": None, "photo": None}
        value["record_hash"] = fingerprint(value)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL — другой процесс не может начать второй upload/post.
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self._read_journal(request_id, binding)
            raise _Failure("VK_REQUEST_ALREADY_ATTEMPTED", uncertain=True) from None
        except OSError:
            raise _Failure("VK_JOURNAL_UNAVAILABLE", uncertain=True) from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=True))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise _Failure("VK_JOURNAL_UNAVAILABLE", uncertain=True) from None
        return value

    def _record(self, request_id, record, **changes):
        path = self._journal_path(request_id)
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        updated = {**record, **changes}
        updated["record_hash"] = fingerprint({k: v for k, v in updated.items()
                                               if k != "record_hash"})
        try:
            # Неполная или подменённая запись никогда не разрешает продолжение.
            current = self._read_journal(request_id, record["binding"])
            if current["record_hash"] != record["record_hash"]:
                raise _Failure("VK_JOURNAL_CHANGED", uncertain=True)
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(updated, ensure_ascii=True))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError):
            raise _Failure("VK_JOURNAL_UNAVAILABLE", uncertain=True) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        record.update(updated)

    @staticmethod
    def _authorize(authorize):
        if authorize is not None:
            try:
                authorize()
            except AgentError as exc:
                raise _Failure(exc.code, uncertain=True) from None

    def _upload(self, prepared, request_id, record, authorize=None):
        response = self._api("photos.getWallUploadServer", {"group_id": -prepared.owner_id})
        if not isinstance(response, dict):
            raise _Failure("VK_UPLOAD_SERVER_INVALID")
        url = _upload_url(response.get("upload_url"))
        filename = "approved.png" if prepared.media["mime"] == "image/png" else "approved.jpg"
        self._record(request_id, record, stage="upload_sending")
        self._authorize(authorize)
        try:
            uploaded = self.upload_client.post(url, files={"photo":
                (filename, prepared.image, prepared.media["mime"])})
        except httpx.HTTPError:
            raise _Failure("VK_UPLOAD_UNKNOWN", uncertain=True) from None
        value = self._decode(uploaded, mutation=True)
        server, photo, photo_hash = value.get("server"), value.get("photo"), value.get("hash")
        try:
            photos = _json(photo) if isinstance(photo, str) else None
        except (ValueError, TypeError, UnicodeError):
            photos = None
        if (not _integer(server, positive=True) or not isinstance(photos, list)
                or len(photos) != 1 or not isinstance(photos[0], dict)
                or not isinstance(photo_hash, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", photo_hash)):
            raise _Failure("VK_UPLOAD_RESPONSE_INVALID", uncertain=True)
        self._record(request_id, record, stage="save_sending")
        self._authorize(authorize)
        saved = self._api("photos.saveWallPhoto", {"group_id": -prepared.owner_id,
                          "server": server, "photo": photo, "hash": photo_hash}, mutation=True)
        if (not isinstance(saved, list) or len(saved) != 1 or not isinstance(saved[0], dict)
                or not _integer(saved[0].get("owner_id"))
                or not _integer(saved[0].get("id"), positive=True)):
            raise _Failure("VK_SAVE_RESPONSE_INVALID", uncertain=True)
        photo = saved[0]["owner_id"], saved[0]["id"]
        self._record(request_id, record, stage="photo_saved", photo=list(photo))
        return photo

    @staticmethod
    def _matches(value, prepared, post_id, photo):
        posts = value.get("items") if isinstance(value, dict) else value
        if not isinstance(posts, list) or len(posts) != 1 or not isinstance(posts[0], dict):
            return False
        post = posts[0]
        if (type(post.get("id")) is not int or post["id"] != post_id
                or type(post.get("owner_id")) is not int
                or post["owner_id"] != prepared.owner_id
                or type(post.get("from_id")) is not int
                or post["from_id"] != prepared.owner_id
                or post.get("text") != prepared.text or post.get("is_deleted")
                or post.get("post_type") != "post" or post.get("copy_history")):
            return False
        attachments = post.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) != (1 if photo else 0):
            return False
        if photo:
            attachment = attachments[0]
            if not isinstance(attachment, dict) or attachment.get("type") != "photo":
                return False
            actual = attachment.get("photo")
            if (not isinstance(actual, dict) or type(actual.get("owner_id")) is not int
                    or type(actual.get("id")) is not int
                    or (actual["owner_id"], actual["id"]) != photo):
                return False
        return True

    def publish(self, destination, snapshot, request_id, *, authorize=None):
        prepared = self._prepare(destination, snapshot)
        guid = _guid(request_id)
        evidence = {"provider": "vk", "fixture": self.fixture, "api_version": self.api_version,
                    "snapshot_hash": fingerprint(snapshot),
                    "text_sha256": hashlib.sha256(prepared.text.encode()).hexdigest(),
                    "title_format": "plain", "request_guid": guid}
        if prepared.media:
            evidence["image_sha256"] = prepared.media["sha256"]
        binding = fingerprint({"destination": destination, "snapshot": snapshot})
        if request_id in self._attempted:
            previous = self._attempted[request_id]
            return Delivery("unknown", previous.external_id if previous else None,
                            previous.permalink if previous else None, verified=False,
                            evidence={**evidence, "code": "VK_REQUEST_ALREADY_ATTEMPTED"})
        if self.mode != "live" or not self.token:
            return Delivery("rejected", verified=False, evidence={**evidence, "code":
                            "VK_LIVE_MODE_REQUIRED" if self.mode != "live" else "VK_TOKEN_REQUIRED"})
        if authorize is not None:
            authorize()
        # Вторая защита в памяти; после перезапуска действует durable Dispatch Hub.
        self._attempted[request_id] = None
        stage, external_id, permalink = "upload", None, None
        record = None
        try:
            record = self._claim(request_id, binding)
            photo = self._upload(prepared, request_id, record, authorize) if prepared.media else None
            stage = "post"
            params = {"owner_id": prepared.owner_id, "from_group": 1,
                      "message": prepared.text, "guid": guid}
            if photo:
                params["attachments"] = f"photo{photo[0]}_{photo[1]}"
            self._record(request_id, record, stage="post_sending")
            self._authorize(authorize)
            value = self._api("wall.post", params, mutation=True)
            if not isinstance(value, dict) or not _integer(value.get("post_id"), positive=True):
                raise _Failure("VK_POST_RESPONSE_INVALID", uncertain=True)
            post_id = value["post_id"]
            external_id = f"{prepared.owner_id}_{post_id}"
            permalink = "https://vk.com/wall" + external_id
            self._record(request_id, record, stage="post_accepted", external_id=external_id)
            stage = "readback"
            found = self._api("wall.getById", {"posts": external_id, "extended": 0})
            if not self._matches(found, prepared, post_id, photo):
                raise _Failure("VK_READBACK_MISMATCH", uncertain=True)
            evidence.update({"code": "VK_EXACT_READBACK", "stage": stage,
                             "readback_exact": True})
            if photo:
                evidence["photo_id"] = f"{photo[0]}_{photo[1]}"
            result = Delivery("confirmed", external_id, permalink,
                              verified=not self.fixture, evidence=evidence)
        except _Failure as exc:
            # После ответа wall.post любая ошибка чтения оставляет известный ID
            # и неизвестный исход проверки. Повторная отправка запрещена.
            code = "VK_READBACK_UNAVAILABLE" if stage == "readback" else exc.code
            if exc.code == "VK_READBACK_MISMATCH":
                code = exc.code
            proof = {**evidence, "code": code, "cause_code": exc.code,
                     "stage": (record.get("stage", stage) if record and stage == "upload"
                               else stage)}
            if exc.api_code is not None:
                proof["api_error_code"] = exc.api_code
            result = Delivery("unknown" if external_id or exc.uncertain else "rejected",
                              external_id, permalink, verified=False, evidence=proof)
            if record is None and exc.code == "VK_REQUEST_ALREADY_ATTEMPTED":
                try:
                    previous = self._read_journal(request_id, binding)
                    known = previous.get("external_id")
                    if (isinstance(known, str)
                            and re.fullmatch(re.escape(destination) + r"_[1-9][0-9]*", known)):
                        result.external_id, result.permalink = known, "https://vk.com/wall" + known
                except _Failure:
                    pass
        if record is not None:
            try:
                self._record(request_id, record, stage="completed", state=result.state,
                             external_id=result.external_id, evidence=result.evidence)
            except _Failure:
                result = Delivery("unknown", result.external_id, result.permalink, verified=False,
                                  evidence={**evidence, "code": "VK_JOURNAL_UNAVAILABLE"})
        self._attempted[request_id] = result
        return result

    def reconcile(self, destination, snapshot, external_id, request_id=None):
        """Отдельное чтение известного ID; не вызывает upload, save или wall.post."""
        _community(destination)
        evidence = {"provider": "vk", "fixture": self.fixture, "stage": "reconcile",
                    "snapshot_hash": fingerprint(snapshot), "title_format": "plain"}
        binding = fingerprint({"destination": destination, "snapshot": snapshot})
        record = None
        if external_id is None and request_id is not None:
            try:
                # После потери финализации БД ID уже может быть надёжно сохранён
                # до readback. Извлекаем только запись точной версии и адресата.
                record = self._read_journal(request_id, binding)
                external_id = record.get("external_id")
                evidence["recovered_from_journal"] = external_id is not None
            except _Failure as exc:
                return Delivery("unknown", verified=False,
                    evidence={**evidence, "code": exc.code, "failure_code": exc.code})
        known = (isinstance(external_id, str) and re.fullmatch(
            re.escape(destination) + r"_([1-9][0-9]{0,18})", external_id))
        if not known or not _integer(int(known[1]), positive=True):
            return Delivery("unknown", verified=False,
                            evidence={**evidence, "code": "VK_EXTERNAL_ID_REQUIRED",
                                      "failure_code": "VK_EXTERNAL_ID_REQUIRED"})
        permalink = "https://vk.com/wall" + external_id
        photo = None
        try:
            prepared = self._prepare(destination, snapshot)
            if request_id is not None:
                record = record if record is not None else self._read_journal(request_id, binding)
                if record.get("external_id") not in {None, external_id}:
                    raise _Failure("VK_RECONCILE_BINDING_MISMATCH", uncertain=True)
                raw_photo = record.get("photo")
                if prepared.media:
                    if (not isinstance(raw_photo, list) or len(raw_photo) != 2
                            or not _integer(raw_photo[0])
                            or not _integer(raw_photo[1], positive=True)):
                        raise _Failure("VK_RECONCILE_EVIDENCE_REQUIRED", uncertain=True)
                    photo = tuple(raw_photo)
                elif raw_photo is not None:
                    raise _Failure("VK_RECONCILE_BINDING_MISMATCH", uncertain=True)
            elif prepared.media:
                raise _Failure("VK_RECONCILE_EVIDENCE_REQUIRED", uncertain=True)
            found = self._api("wall.getById", {"posts": external_id, "extended": 0})
            if not self._matches(found, prepared, int(known[1]), photo):
                raise _Failure("VK_READBACK_MISMATCH", uncertain=True)
            evidence.update({"code": "VK_EXACT_READBACK", "readback_exact": True})
            result = Delivery("confirmed", external_id, permalink, verified=not self.fixture,
                              evidence=evidence)
            if record is not None:
                self._record(request_id, record, stage="reconciled", state=result.state,
                             external_id=external_id, evidence=evidence)
                self._attempted[request_id] = result
            return result
        except _Failure as exc:
            return Delivery("unknown", external_id, permalink, verified=False,
                            evidence={**evidence, "code": exc.code, "failure_code": exc.code})
        except AgentError as exc:
            return Delivery("unknown", external_id, permalink, verified=False,
                            evidence={**evidence, "code": exc.code, "failure_code": exc.code})

    def close(self):
        self.client.close()
        self.upload_client.close()
