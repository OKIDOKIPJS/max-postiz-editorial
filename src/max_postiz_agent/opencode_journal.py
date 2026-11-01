"""Приватные доказательства одного запроса; журнал не разрешает его повтор."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .config import AgentError, inside


OPENCODE_REQUEST_CONTRACT = "opencode-request-v1"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _sync_directory(path):
    # Windows не поддерживает fsync каталога; содержимое файлов синхронизируем везде.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class OpenCodeRequestJournal:
    """Не принимает URL, headers или credentials: только точный вход и ответ."""

    def __init__(self, root, prompt, model):
        self.root = Path(root).resolve()
        self.ident = str(uuid.uuid4())
        self.relative = "data/opencode-requests/" + self.ident
        self.prompt_hash = _sha(prompt.encode("utf-8"))
        self.session_id = None
        try:
            for relative in ("data", "data/opencode-requests", self.relative):
                path = self._path(relative)
                if not path.exists():
                    path.mkdir(mode=0o700)
                    _sync_directory(path.parent)
                elif not path.is_dir():
                    raise OSError("Journal directory unavailable")
                if relative != "data":
                    path.chmod(0o700)
            self._json("intent.json", {
                "contract": OPENCODE_REQUEST_CONTRACT,
                "id": self.ident,
                "created_at": time.time(),
                "transport": "session",
                "model": model,
                "prompt": prompt,
                "prompt_sha256": self.prompt_hash,
            })
        except OSError as exc:
            raise AgentError("OPENCODE_JOURNAL_FAILED", 503) from exc

    def _path(self, relative):
        path = inside(self.root, relative)
        # Внутренний symlink/junction тоже опасен: может вывести raw в неигнорируемый src/.
        if path != self.root / relative:
            raise AgentError("OPENCODE_JOURNAL_FAILED", 503)
        return path

    def _write(self, name, raw):
        # Имена задаёт только модуль; fixed files не перезаписывают старое доказательство.
        path = self._path(self.relative + "/" + name)
        temporary = self._path(self.relative + "/." + name + ".tmp")
        try:
            if path.exists():
                raise FileExistsError("Journal evidence already exists")
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                 | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
            _sync_directory(path.parent)
        except OSError as exc:
            raise AgentError("OPENCODE_JOURNAL_FAILED", 503) from exc

    def _json(self, name, value):
        self._write(name, json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     indent=2).encode("utf-8"))

    def response(self, stage, status, raw):
        """Полные bytes должны стать durable раньше статуса, JSON и cleanup."""
        if stage not in {"create", "response"}:
            raise AgentError("OPENCODE_JOURNAL_FAILED", 503)
        self._write(stage + ".raw", raw)
        self._json(stage + ".json", {
            "status_code": status,
            "received_at": time.time(),
            "body_sha256": _sha(raw),
            "body_bytes": len(raw),
            "prompt_sha256": self.prompt_hash,
            "session_id": self.session_id,
        })

    def bind_session(self, session_id):
        self._json("session.json", {
            "session_id": session_id,
            "bound_at": time.time(),
            "prompt_sha256": self.prompt_hash,
        })
        self.session_id = session_id

    def validated(self, text):
        self._json("validated.json", {
            "session_id": self.session_id,
            "validated_at": time.time(),
            "prompt_sha256": self.prompt_hash,
            "text_sha256": _sha(text.encode("utf-8")),
            "text_bytes": len(text.encode("utf-8")),
        })

    def completed_generation(self):
        """Привязка полного полученного ответа; не разрешение повторить запрос."""
        names = ("intent.json", "session.json", "create.json", "create.raw",
                 "response.json", "response.raw", "validated.json")
        try:
            files = {}
            for name in names:
                path = self._path(self.relative + "/" + name)
                if not path.is_file() or path.is_symlink():
                    raise OSError("Completion evidence unavailable")
                files[name] = _sha(path.read_bytes())
            validated = json.loads(self._path(self.relative + "/validated.json").read_bytes())
            return {"contract": "opencode-completed-generation-v1", "directory": self.relative,
                    "prompt_sha256": self.prompt_hash, "text_sha256": validated["text_sha256"],
                    "files": files}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError("OPENCODE_JOURNAL_FAILED", 503) from exc

    def failed(self, code, stage):
        # Исключения httpx могут включать URL/credentials, поэтому их текст не сохраняем.
        self._json("failure.json", {
            "session_id": self.session_id,
            "failed_at": time.time(),
            "prompt_sha256": self.prompt_hash,
            "error": code,
            "stage": stage,
            "outcome": "unresolved",
        })
