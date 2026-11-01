"""Неизвестный запрос нельзя автоматически прервать и удалить вместе с proof."""

import hashlib
import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.adapters import OpenCode
from max_postiz_agent.config import AgentError
from max_postiz_agent.config import inside as real_inside
from max_postiz_agent.opencode_journal import OPENCODE_REQUEST_CONTRACT, OpenCodeRequestJournal


PROMPT = "Точный запрос\nUNTRUSTED_JSON_DATA:\n{\"original\": \"Hubble 2026\"}"
SESSION_ID = "sesJournalOne"


def editor(tmp_path, monkeypatch, handler):
    monkeypatch.setenv("JOURNAL_TEST_BASE", "http://url-user:url-password@127.0.0.1:4108")
    monkeypatch.setenv("JOURNAL_TEST_MODEL", "proxmox/qwen35-9b")
    monkeypatch.setenv("JOURNAL_TEST_SECRET", "private-auth-value")
    options = {"adapter_mode": "live", "base_url_env": "JOURNAL_TEST_BASE",
               "model_id_env": "JOURNAL_TEST_MODEL", "auth_secret_env": "JOURNAL_TEST_SECRET",
               "task_timeout_seconds": 600, "generate_transport": "session",
               "runtime_dir": "data/opencode-runtime"}
    return OpenCode(SimpleNamespace(root=tmp_path, app={"opencode": options}),
                    httpx.MockTransport(handler))


def journal_directory(root):
    paths = list((root / "data/opencode-requests").iterdir())
    assert len(paths) == 1
    return paths[0]


def load(path, name):
    return json.loads((path / name).read_bytes())


def created():
    return httpx.Response(200, json={"data": {"id": SESSION_ID}})


def test_timeout_retains_bound_session_and_exact_input_without_interrupt_delete_or_retry(tmp_path, monkeypatch):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        directory = journal_directory(tmp_path)
        intent = load(directory, "intent.json")
        assert intent["prompt"] == PROMPT
        assert intent["prompt_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
        if request.url.path == "/api/session":
            return created()
        assert request.url.path == "/api/session/" + SESSION_ID + "/generate"
        assert load(directory, "session.json")["session_id"] == SESSION_ID
        assert load(directory, "create.json")["status_code"] == 200
        raise httpx.ReadTimeout("timeout containing credentials must not enter receipt", request=request)

    model = editor(tmp_path, monkeypatch, handle)
    model.last_completed_generation = {"stale": True}
    try:
        with pytest.raises(AgentError, match="OPENCODE_GENERATION_FAILED"):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    assert calls == [("POST", "/api/session"), ("POST", "/api/session/" + SESSION_ID + "/generate")]
    directory = journal_directory(tmp_path)
    assert load(directory, "failure.json")["outcome"] == "unresolved"
    assert load(directory, "failure.json")["session_id"] == SESSION_ID
    assert not (directory / "validated.json").exists()
    assert not (directory / "response.raw").exists()
    assert model.last_completed_generation is None


def test_success_saves_exact_raw_status_and_validation_before_any_cleanup(tmp_path, monkeypatch):
    raw = b'{"data":{"text":"valid\\nanswer"},"usage":{"inputTokens":12}}\r\n'
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        directory = journal_directory(tmp_path)
        if request.url.path == "/api/session":
            return created()
        if request.url.path.endswith("/generate"):
            assert load(directory, "session.json")["session_id"] == SESSION_ID
            assert (directory / "create.raw").read_bytes() == created().content
            return httpx.Response(200, content=raw)
        # Прежний finally делал cleanup даже до чтения/сохранения результата.
        assert (directory / "response.raw").read_bytes() == raw
        receipt = load(directory, "response.json")
        assert receipt["status_code"] == 200
        assert receipt["body_sha256"] == hashlib.sha256(raw).hexdigest()
        assert receipt["body_bytes"] == len(raw)
        assert load(directory, "validated.json")["text_sha256"] == hashlib.sha256(b"valid\nanswer").hexdigest()
        return httpx.Response(200, json={})

    model = editor(tmp_path, monkeypatch, handle)
    try:
        assert model.generate_text(PROMPT) == ("valid\nanswer", {"inputTokens": 12})
    finally:
        model.client.close()
    assert calls[-2:] == [("POST", "/api/session/" + SESSION_ID + "/interrupt"),
                          ("DELETE", "/api/session/" + SESSION_ID)]
    assert not (journal_directory(tmp_path) / "failure.json").exists()
    directory = journal_directory(tmp_path)
    receipt = model.last_completed_generation
    assert receipt == {
        "contract": "opencode-completed-generation-v1",
        "directory": directory.relative_to(tmp_path).as_posix(),
        "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
        "text_sha256": hashlib.sha256(b"valid\nanswer").hexdigest(),
        "files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                  for name in ("intent.json", "session.json", "create.json", "create.raw",
                               "response.json", "response.raw", "validated.json")},
    }


@pytest.mark.parametrize("status,raw,code", [
    (200, b"not json", "OPENCODE_GENERATION_FAILED"),
    (200, b'{"data":{"text":""},"data":{"text":"answer"}}', "OPENCODE_GENERATION_FAILED"),
    (200, b'{"text":"answer","usage":{"tokens":NaN}}', "OPENCODE_GENERATION_FAILED"),
    (200, b'{"text":"answer","usage":{"tokens":Infinity}}', "OPENCODE_GENERATION_FAILED"),
    (200, b"[]", "OPENCODE_GENERATION_FAILED"),
    (200, b'{"data":[]}', "OPENCODE_GENERATION_FAILED"),
    (200, b'{"data":{"text":"  "}}', "OPENCODE_EMPTY_RESPONSE"),
    (200, b'{"data":{"text":7}}', "OPENCODE_EMPTY_RESPONSE"),
    (200, b"{}", "OPENCODE_EMPTY_RESPONSE"),
    (401, b"access denied", "OPENCODE_ACCESS_DENIED"),
    (403, b"access denied", "OPENCODE_ACCESS_DENIED"),
    (503, b"unavailable", "OPENCODE_PROVIDER_UNAVAILABLE"),
    (500, b"server error", "OPENCODE_GENERATION_FAILED"),
])
def test_invalid_or_http_response_preserves_raw_and_session_without_cleanup(tmp_path, monkeypatch, status, raw, code):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        return created() if request.url.path == "/api/session" else httpx.Response(status, content=raw)

    model = editor(tmp_path, monkeypatch, handle)
    try:
        with pytest.raises(AgentError, match=code):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    directory = journal_directory(tmp_path)
    assert len(calls) == 2
    assert (directory / "response.raw").read_bytes() == raw
    assert load(directory, "response.json")["status_code"] == status
    assert load(directory, "failure.json")["error"] == code
    assert not (directory / "validated.json").exists()
    assert model.last_completed_generation is None


@pytest.mark.parametrize("raw", [b"broken json", b"[]", b'{"data":{"id":"../escape"}}', b'{"data":{"id":7}}',
                                 b'{"data":{"id":"sesWrong"},"data":{"id":"sesJournalOne"}}',
                                 b'{"data":{"id":"sesJournalOne"},"tokens":NaN}'])
def test_invalid_session_creation_preserves_raw_and_never_generates(tmp_path, monkeypatch, raw):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=raw)

    model = editor(tmp_path, monkeypatch, handle)
    try:
        with pytest.raises(AgentError):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    directory = journal_directory(tmp_path)
    assert calls == ["/api/session"]
    assert (directory / "create.raw").read_bytes() == raw
    assert not (directory / "session.json").exists()


def test_intent_disk_failure_happens_before_all_network(tmp_path, monkeypatch):
    calls = []

    def fail_sync(_):
        raise OSError("disk unavailable")

    monkeypatch.setattr("max_postiz_agent.opencode_journal.os.fsync", fail_sync)
    model = editor(tmp_path, monkeypatch, lambda request: calls.append(request))
    try:
        with pytest.raises(AgentError, match="OPENCODE_JOURNAL_FAILED"):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    assert calls == []


@pytest.mark.parametrize("name,expected_calls", [("create.raw", 1), ("create.json", 1),
                                                ("session.json", 1), ("response.raw", 2),
                                                ("response.json", 2), ("validated.json", 2)])
def test_disk_failure_after_session_creation_never_triggers_destructive_cleanup(tmp_path, monkeypatch, name, expected_calls):
    original = OpenCodeRequestJournal._write
    calls = []

    def fail_write(self, filename, raw):
        if filename == name:
            raise AgentError("OPENCODE_JOURNAL_FAILED", 503)
        return original(self, filename, raw)

    monkeypatch.setattr(OpenCodeRequestJournal, "_write", fail_write)

    def handle(request):
        calls.append((request.method, request.url.path))
        return created() if request.url.path == "/api/session" else httpx.Response(200, json={"text": "valid"})

    model = editor(tmp_path, monkeypatch, handle)
    try:
        with pytest.raises(AgentError, match="OPENCODE_JOURNAL_FAILED"):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    assert len(calls) == expected_calls
    assert all(method == "POST" and not path.endswith("interrupt") for method, path in calls)
    directory = journal_directory(tmp_path)
    if name in {"response.json", "validated.json"}:
        assert (directory / "response.raw").exists()


@pytest.mark.parametrize("kind", ["timeout", "http"])
def test_creation_error_keeps_intent_without_any_generate_or_cleanup(tmp_path, monkeypatch, kind):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert load(journal_directory(tmp_path), "intent.json")["prompt"] == PROMPT
        if kind == "timeout":
            raise httpx.ReadTimeout("create outcome unknown", request=request)
        return httpx.Response(500, content=b"creation failed")

    model = editor(tmp_path, monkeypatch, handle)
    try:
        with pytest.raises(AgentError, match="OPENCODE_GENERATION_FAILED"):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    assert calls == ["/api/session"]
    directory = journal_directory(tmp_path)
    assert load(directory, "failure.json")["session_id"] is None
    assert not (directory / "session.json").exists()
    if kind == "http":
        assert (directory / "create.raw").read_bytes() == b"creation failed"


def test_outside_runtime_path_is_rejected_before_network_and_not_hidden_in_receipt(tmp_path, monkeypatch):
    calls = []
    model = editor(tmp_path, monkeypatch, lambda request: calls.append(request))
    model.options["runtime_dir"] = "../outside"
    try:
        with pytest.raises(AgentError, match="PATH_OUTSIDE_PROJECT"):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    assert calls == []
    assert load(journal_directory(tmp_path), "failure.json")["error"] == "PATH_OUTSIDE_PROJECT"


def test_resolved_internal_redirect_cannot_write_private_prompts_into_public_source(tmp_path, monkeypatch):
    public = tmp_path / "src"
    public.mkdir()
    before_mode = public.stat().st_mode
    calls = []

    def redirected(root, relative):
        # Эмулируем inside().resolve для внутреннего symlink/junction без прав Windows symlink.
        if relative == "data/opencode-requests":
            return public
        return real_inside(root, relative)

    monkeypatch.setattr("max_postiz_agent.opencode_journal.inside", redirected)
    model = editor(tmp_path, monkeypatch, lambda request: calls.append(request))
    try:
        with pytest.raises(AgentError, match="OPENCODE_JOURNAL_FAILED"):
            model.generate_text(PROMPT)
    finally:
        model.client.close()
    assert calls == [] and list(public.iterdir()) == []
    assert public.stat().st_mode == before_mode


def test_journal_contains_no_auth_secret_headers_or_base_url_credentials_and_has_private_modes(tmp_path, monkeypatch):
    model = editor(tmp_path, monkeypatch, lambda request: created() if request.url.path == "/api/session"
                   else httpx.Response(200, json={"text": "answer"}))
    try:
        assert model.generate_text(PROMPT) == ("answer", {})
    finally:
        model.client.close()
    directory = journal_directory(tmp_path)
    intent = load(directory, "intent.json")
    assert intent["contract"] == OPENCODE_REQUEST_CONTRACT
    assert intent["model"] == {"providerID": "proxmox", "id": "qwen35-9b"}
    for path in directory.iterdir():
        raw = path.read_bytes().lower()
        assert all(secret not in raw for secret in (b"private-auth-value", b"url-user", b"url-password", b"authorization"))
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if os.name != "nt":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_cleanup_http_failure_after_durable_success_preserves_existing_tuple_interface(tmp_path, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/session":
            return created()
        if request.url.path.endswith("/generate"):
            return httpx.Response(200, json={"data": {"text": "answer"}, "usage": {"legacy": 3}})
        assert (journal_directory(tmp_path) / "validated.json").exists()
        raise httpx.ConnectError("cleanup unavailable", request=request)

    model = editor(tmp_path, monkeypatch, handle)
    try:
        assert model.generate_text(PROMPT) == ("answer", {"legacy": 3})
    finally:
        model.client.close()
    assert len(calls) == 3


def test_calls_get_distinct_immutable_intents_without_overwriting_evidence(tmp_path, monkeypatch):
    model = editor(tmp_path, monkeypatch, lambda request: created() if request.url.path == "/api/session"
                   else httpx.Response(200, json={"text": "answer"}))
    try:
        model.generate_text("first")
        model.generate_text("second")
    finally:
        model.client.close()
    directories = list((tmp_path / "data/opencode-requests").iterdir())
    assert len(directories) == 2
    assert {load(path, "intent.json")["prompt"] for path in directories} == {"first", "second"}
