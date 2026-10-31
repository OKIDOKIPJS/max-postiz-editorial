# Адаптеры внешних сервисов и безопасная обработка результатов отправки.
"""OpenCode stateless generation and explicit MAX delivery outcomes."""

import json
import os
import re
import uuid
from dataclasses import dataclass

import httpx

from .config import AgentError, inside, parse_json, unique_keys
from .opencode_journal import OpenCodeRequestJournal


# Извлекаем структурированный ответ и проверяем его формат.
def extract_object(text):
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    # Выполняем операцию с отдельной обработкой возможных ошибок.
    try:
        value = parse_json(stripped)
    # Обрабатываем AgentError и выбираем допустимый исход.
    except AgentError as exc:
        raise AgentError("INVALID_EDITOR_RESULT") from exc
    # Обрабатываем ветвь при условии: not isinstance(value, dict).
    if not isinstance(value, dict):
        raise AgentError("INVALID_EDITOR_RESULT")
    return value


def _session_json(raw):
    """Неоднозначный protocol response сохраняем, но не считаем успехом."""
    def invalid_constant(_):
        raise ValueError("Invalid JSON number")

    try:
        return json.loads(raw, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except (AgentError, ValueError, TypeError) as exc:
        raise AgentError("OPENCODE_GENERATION_FAILED") from exc


# Структура OpenCode объединяет связанные данные и операции модуля.
class OpenCode:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, settings, transport=None):
        self.options = settings.app["opencode"]
        self.mode = self.options["adapter_mode"]
        self.root = settings.root
        self.base_url = os.getenv(self.options["base_url_env"], "").rstrip("/")
        self.model = os.getenv(self.options["model_id_env"], self.options.get("model_id", ""))
        self.secret = os.getenv(self.options["auth_secret_env"], "")
        self.explicit_secret = bool(self.secret)
        access_path = self.options.get("local_access_path")
        self.access_path = inside(self.root, access_path) if access_path else None
        self.refresh_local_access()
        self.client = httpx.Client(
            timeout=self.options["task_timeout_seconds"], trust_env=False, transport=transport
        )
        self.path = self.options.get("generate_path", "/api/experimental/generate")

    def refresh_local_access(self):
        """The managed server rotates credentials on restart; keep explicit env overrides."""
        if self.access_path and self.access_path.exists():
            access = parse_json(self.access_path.read_text(encoding='utf-8'))
            self.base_url = self.base_url or access['base_url']
            if not self.explicit_secret:
                self.secret = access['password']
        self.auth = httpx.BasicAuth('opencode', self.secret) if self.secret else None

    # Операция probe: адаптеры внешних сервисов и безопасная обработка результатов отправки.
    def probe(self):
        # Обрабатываем ветвь при условии: self.mode == 'fake'.
        if self.mode == "fake":
            return {"state": "offline", "runtime": "fake", "live_verified": False}
        self.refresh_local_access()
        # Обрабатываем ветвь при условии: not self.base_url.
        if not self.base_url:
            return {"state": "needs_configuration", "live_verified": False}
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            response = self.client.get(self.base_url + "/api/info", auth=self.auth)
            response.raise_for_status()
            info = response.json()
            return {
                "state": "available",
                "live_verified": False,
                "required_version": self.options["required_version"],
                "reported_version": info.get("version") if isinstance(info, dict) else None,
            }
        # Обрабатываем (httpx.HTTPError, ValueError) и выбираем допустимый исход.
        except (httpx.HTTPError, ValueError):
            return {"state": "unavailable", "live_verified": False}

    # Запрашиваем текст по заданным редакционным ограничениям.
    def generate_text(self, prompt):
        self.last_completed_generation = None
        if self.mode == 'live':
            self.refresh_local_access()
        # Обрабатываем ветвь при условии: self.mode != 'live' or not self.base_url.
        if self.mode != "live" or not self.base_url:
            raise AgentError("OPENCODE_NOT_CONFIGURED")
        payload = {"prompt": prompt}
        # Обрабатываем ветвь при условии: self.model.
        if self.model:
            provider, model = self.model.split("/", 1)
            payload["model"] = {"providerID": provider, "id": model}
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            if self.options.get("generate_transport") == "session":
                return self._generate_session(prompt, payload.get("model"))
            response = self.client.post(self.base_url + self.path, json=payload, auth=self.auth)
            # Обрабатываем ветвь при условии: response.status_code in {401, 403}.
            if response.status_code in {401, 403}:
                raise AgentError("OPENCODE_ACCESS_DENIED")
            # Обрабатываем ветвь при условии: response.status_code == 503.
            if response.status_code == 503:
                raise AgentError("OPENCODE_PROVIDER_UNAVAILABLE")
            response.raise_for_status()
            data = response.json()
            text = data.get("data", data).get("text", "")
            # Обрабатываем ветвь при условии: not isinstance(text, str) or not text.strip().
            if not isinstance(text, str) or not text.strip():
                raise AgentError("OPENCODE_EMPTY_RESPONSE")
            return text, data.get("usage", {})
        # Обрабатываем (httpx.HTTPError, ValueError, TypeError) и выбираем допустимый исход.
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AgentError("OPENCODE_GENERATION_FAILED") from exc

    def _generate_session(self, prompt, model):
        """Сбой удерживает точную сессию: interrupt/delete уничтожали доказательства."""
        self.last_completed_generation = None
        journal = OpenCodeRequestJournal(self.root, prompt, model)
        stage = "create"
        try:
            created = self.client.post(
                self.base_url + "/api/session", auth=self.auth,
                json={"title": "MAX editorial task", "model": model,
                      "location": {"directory": str(inside(self.root, self.options["runtime_dir"]))},
                      "permissions": [{"action": "*", "resource": "*", "effect": "deny"}]},
            )
            journal.response("create", created.status_code, created.content)
            created.raise_for_status()
            value = _session_json(created.content)
            data = value.get("data") if isinstance(value, dict) else None
            session_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(session_id, str) or not re.fullmatch(r"ses[a-zA-Z0-9_-]+", session_id):
                raise AgentError("OPENCODE_SESSION_INVALID")
            journal.bind_session(session_id)
            stage = "generate"
            response = self.client.post(self.base_url + f"/api/session/{session_id}/generate",
                                        json={"prompt": prompt}, auth=self.auth)
            journal.response("response", response.status_code, response.content)
            stage = "validate"
            if response.status_code in {401, 403}:
                raise AgentError("OPENCODE_ACCESS_DENIED")
            if response.status_code == 503:
                raise AgentError("OPENCODE_PROVIDER_UNAVAILABLE")
            response.raise_for_status()
            data = _session_json(response.content)
            value = data.get("data", data) if isinstance(data, dict) else None
            if not isinstance(value, dict):
                raise AgentError("OPENCODE_GENERATION_FAILED")
            text = value.get("text", "")
            if not isinstance(text, str) or not text.strip():
                raise AgentError("OPENCODE_EMPTY_RESPONSE")
            journal.validated(text)
            self.last_completed_generation = journal.completed_generation()
        except AgentError as exc:
            journal.failed(exc.code, stage)
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            journal.failed("OPENCODE_GENERATION_FAILED", stage)
            raise AgentError("OPENCODE_GENERATION_FAILED") from exc
        # Только проверенный непустой ответ с durable raw допускает прежний cleanup.
        try:
            self.client.post(self.base_url + f"/api/session/{session_id}/interrupt",
                             auth=self.auth, timeout=5)
            self.client.delete(self.base_url + f"/api/session/{session_id}", auth=self.auth, timeout=5)
        except httpx.HTTPError:
            pass
        return text, data.get("usage", {})

    # Операция run: адаптеры внешних сервисов и безопасная обработка результатов отправки.
    def run(self, task_type, context):
        # Обрабатываем ветвь при условии: self.mode == 'fake'.
        if self.mode == "fake":
            # Обрабатываем ветвь при условии: task_type == 'draft'.
            if task_type == "draft":
                material = context["materials"][0]
                text = material["title"] + "\n\n" + material["text"]
                # Обрабатываем ветвь при условии: material['url'].
                if material["url"]:
                    text += "\n\nИсточник: " + material["url"]
                return {
                    "title": material["title"],
                    "body": text,
                    "language": "ru",
                    "source_ids": [material["id"]],
                    "decision": "review",
                    "reason_codes": ["OFFLINE_FIXTURE"],
                    "claims": [],
                    "confidence": 0,
                    "payload": {},
                }, {}
            # Обрабатываем ветвь при условии: task_type == 'cluster'.
            if task_type == "cluster":
                return {"same_event": False}, {}
            # Обрабатываем ветвь при условии: task_type == 'classify'.
            if task_type == "classify":
                return {"relevance": 1, "decision": "review"}, {}
            # Обрабатываем ветвь при условии: task_type == 'verify'.
            if task_type == "verify":
                return {"verified": False, "issues": ["OFFLINE_FIXTURE"]}, {}
            # Обрабатываем ветвь при условии: task_type == 'translate'.
            if task_type == "translate":
                raise AgentError("OFFLINE_TRANSLATION_UNAVAILABLE")
            if task_type == "adapt":
                raise AgentError("OFFLINE_ADAPTATION_UNAVAILABLE")
            # Обрабатываем ветвь при условии: task_type == 'video_plan'.
            if task_type == "video_plan":
                raise AgentError("OFFLINE_STORYBOARD_UNAVAILABLE")
            # Обрабатываем ветвь при условии: task_type == 'bootstrap'.
            if task_type == "bootstrap":
                return {
                    "name": context["channel"]["name"],
                    "description": context["channel"]["theme"],
                    "calendar": context["channel"]["content_pillars"],
                    "note": "Локальный шаблон; не результат модели",
                }, {}
            return {"note": "Локальный план; генерация моделью выключена"}, {}
        instruction = (self.root / "prompts/EDITOR_AGENT_PROMPT.md").read_text(encoding="utf-8")
        output = {
            "adapt": '{"title":"Русское название","body":"Текст для выбранной платформы с сохранением фактов и ссылок"}',
            "video_plan": '{"title":"Русское название","caption":"Русская подпись без вымышленных фактических утверждений","rights_basis":"Собственные сценарий и материалы; проверка редактором","scenes":[{"prompt":"English visual description and camera motion","negative_prompt":"visual artifacts to avoid","duration_seconds":5}]}',
            "draft": '{"title":"...","body":"...","language":"ru","source_ids":[],"decision":"accept|reject|review","reason_codes":[],"claims":[{"text":"...","material_id":"...","quote":"exact original fragment"}],"confidence":0.8,"payload":{}}',
            "cluster": '{"same_event":true,"reason":"..."}',
            "translate": '{"text":"Russian translation"}',
            "classify": '{"relevance":0.8,"decision":"accept|reject|review","reason":"..."}',
            "verify": '{"verified":true,"issues":[]}',
            "bootstrap": '{"name":"...","description":"...","welcome":"...","calendar":[]}',
            "growth_plan": '{"hypothesis":"...","action":"...","metric":"...","stop_condition":"..."}',
        }[task_type]
        prompt = instruction + "\nFor this service task use this exact JSON shape:\n" + output
        prompt += "\nTask type: " + task_type + "\nUNTRUSTED MATERIALS AND TRUSTED CONFIG:\n"
        prompt += json.dumps(context, ensure_ascii=False)
        # Последовательно обрабатываем элементы: range(self.options['max_format_repairs'] + 1).
        for attempt in range(self.options["max_format_repairs"] + 1):
            text, usage = self.generate_text(prompt)
            # Выполняем операцию с отдельной обработкой возможных ошибок.
            try:
                return extract_object(text), usage
            # Обрабатываем AgentError и выбираем допустимый исход.
            except AgentError:
                # Обрабатываем ветвь при условии: attempt == self.options['max_format_repairs'].
                if attempt == self.options["max_format_repairs"]:
                    raise
                # Do not send the failed output back; it may contain injection or invented secrets.
                prompt += "\nReturn valid JSON only. No markdown fences or commentary."
        raise AgentError("INVALID_EDITOR_RESULT")


# Структура Delivery объединяет связанные данные и операции модуля.
@dataclass
class Delivery:
    state: str
    external_id: str | None = None
    permalink: str | None = None
    retry_after: float = 0
    # Новые маршруты явно различают квитанцию API, fixture и независимую сверку.
    # None сохраняет прежний контракт браузерного MAX без переоценки истории.
    verified: bool | None = None
    evidence: dict | None = None


# Структура MaxAPI объединяет связанные данные и операции модуля.
class MaxAPI:
    # Создаём объект и сохраняем зависимости для последующих операций.
    def __init__(self, settings, transport=None):
        self.options = settings.app["max"]
        self.mode = self.options["adapter_mode"]
        self.base_url = self.options["api_base_url"].rstrip("/")
        self.token = os.getenv(self.options["token_env"], "")
        self.client = httpx.Client(
            timeout=self.options["request_timeout_seconds"], trust_env=False, transport=transport
        )

    # Проверяем запрос и обрабатываем безопасный результат транспорта.
    def request(self, method, path, **kwargs):
        # Обрабатываем ветвь при условии: not self.token.
        if not self.token:
            raise AgentError("MAX_TOKEN_REQUIRED")
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            return self.client.request(
                method, self.base_url + path, headers={"Authorization": self.token}, **kwargs
            )
        # Обрабатываем httpx.HTTPError и выбираем допустимый исход.
        except httpx.HTTPError as exc:
            raise AgentError("MAX_TRANSPORT_UNKNOWN") from exc

    # Проверяем доступность ресурса и его разрешённые свойства.
    def inspect(self, chat_id):
        # Обрабатываем ветвь при условии: self.mode == 'fake'.
        if self.mode == "fake":
            return {
                "publish": True,
                "read": True,
                "edit": True,
                "remove": True,
                "create_channel": False,
                "live_verified": False,
            }
        response = self.request("GET", f"/chats/{chat_id}/members/me")
        # Обрабатываем ветвь при условии: response.status_code != 200.
        if response.status_code != 200:
            raise AgentError("MAX_CHANNEL_ACCESS_DENIED")
        membership = response.json()
        admin = bool(membership.get("is_admin") or membership.get("is_owner"))
        permissions = membership.get("permissions", [])
        return {
            "publish": admin and (not permissions or "write" in permissions),
            "read": admin,
            "edit": admin,
            "remove": admin,
            "create_channel": False,
            "live_verified": True,
        }

    # Выполняем отправку и возвращаем подтверждённый либо неопределённый результат.
    def publish(self, chat_id, text, attachment=None):
        # Обрабатываем ветвь при условии: self.mode == 'fake'.
        if self.mode == "fake":
            return Delivery("confirmed", "offline-" + str(uuid.uuid4()), None)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            payload = {"text": text, "notify": True}
            # Обрабатываем ветвь при условии: attachment.
            if attachment:
                payload["attachments"] = [attachment]
            response = self.request("POST", "/messages", params={"chat_id": chat_id}, json=payload)
            # Обрабатываем ветвь при условии: response.status_code == 400 and response.json().get('code') == 'attachment.not.ready'.
            if (
                response.status_code == 400
                and response.json().get("code") == "attachment.not.ready"
            ):
                return Delivery("retry_wait", retry_after=30)
            # Обрабатываем ветвь при условии: response.status_code in {401, 403, 400, 404}.
            if response.status_code in {401, 403, 400, 404}:
                return Delivery("rejected")
            # Обрабатываем ветвь при условии: response.status_code == 429.
            if response.status_code == 429:
                # Выполняем операцию с отдельной обработкой возможных ошибок.
                try:
                    delay = max(1, float(response.headers.get("Retry-After", "60")))
                # Обрабатываем ValueError и выбираем допустимый исход.
                except ValueError:
                    delay = 60
                return Delivery("retry_wait", retry_after=delay)
            # Обрабатываем ветвь при условии: response.status_code != 200.
            if response.status_code != 200:
                return Delivery("unknown")
            message = response.json().get("message", {})
            mid = message.get("body", {}).get("mid")
            # Обрабатываем ветвь при условии: not mid.
            if not mid:
                return Delivery("unknown")
            return Delivery("confirmed", str(mid), message.get("url"))
        # Обрабатываем (AgentError, ValueError, TypeError) и выбираем допустимый исход.
        except (AgentError, ValueError, TypeError):
            return Delivery("unknown")

    # Операция messages: адаптеры внешних сервисов и безопасная обработка результатов отправки.
    def messages(self, chat_id, **params):
        # Обрабатываем ветвь при условии: self.mode != 'live'.
        if self.mode != "live":
            return []
        response = self.request("GET", "/messages", params={"chat_id": chat_id, **params})
        # Обрабатываем ветвь при условии: response.status_code != 200.
        if response.status_code != 200:
            raise AgentError("MAX_READ_FAILED")
        return response.json().get("messages", [])

    # Подготавливаем проверенный ролик для выбранного адаптера.
    def upload_video(self, path):
        # Обрабатываем ветвь при условии: self.mode == 'fake'.
        if self.mode == "fake":
            return {"type": "video", "payload": {"token": "offline-video"}}
        response = self.request("POST", "/uploads", params={"type": "video"})
        # Обрабатываем ветвь при условии: response.status_code != 200.
        if response.status_code != 200:
            raise AgentError("VIDEO_UPLOAD_UNAVAILABLE")
        data = response.json()
        from .network import validate_url

        url = data.get("url", "")
        host, addresses = validate_url(url)
        original = httpx.URL(url)
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with path.open("rb") as stream:
            uploaded = self.client.post(
                original.copy_with(host=addresses[0]),
                headers={"Host": original.netloc.decode()},
                extensions={"sni_hostname": host},
                files={"data": (path.name, stream, "video/mp4")},
            )
        # Обрабатываем ветвь при условии: uploaded.status_code != 200.
        if uploaded.status_code != 200:
            raise AgentError("VIDEO_UPLOAD_UNCONFIRMED")
        token = data.get("token") or uploaded.json().get("token")
        # Обрабатываем ветвь при условии: not token.
        if not token:
            raise AgentError("VIDEO_UPLOAD_UNCONFIRMED")
        return {"type": "video", "payload": {"token": token}}

    # Операция correct: адаптеры внешних сервисов и безопасная обработка результатов отправки.
    def correct(self, message_id, text=None, remove=False):
        # Обрабатываем ветвь при условии: self.mode == 'fake'.
        if self.mode == "fake":
            return True
        response = self.request(
            "DELETE" if remove else "PUT",
            "/messages",
            params={"message_id": message_id},
            **({} if remove else {"json": {"text": text}}),
        )
        # Обрабатываем ветвь при условии: response.status_code != 200 or response.json().get('success') is not True.
        if response.status_code != 200 or response.json().get("success") is not True:
            raise AgentError("MAX_CORRECTION_UNCONFIRMED")
        return True

    # Освобождаем соединения и принадлежащие объекту ресурсы.
    def close(self):
        self.client.close()
