"""Проверки реальных границ пяти CodeQL flows без моделей, сети и рабочего стола."""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from max_postiz_agent import image_commands, image_pipeline, panel_auth, panel_files
from max_postiz_agent.api import create_app
from max_postiz_agent.config import AgentError


def test_missing_source_key_report_never_stores_or_logs_an_existing_key(tmp_path, monkeypatch, capsys):
    """CodeQL 1/2: source api_key_env содержит имя, значение используется только как bool."""
    secret = "synthetic-present-key-not-a-real-credential"
    monkeypatch.setenv("SECURITY_PRESENT_API_KEY", secret)
    monkeypatch.delenv("SECURITY_MISSING_API_KEY", raising=False)
    config = {"target_channel": "test", "sources": [
        {"id": "missing", "type": "pixabay_api", "enabled": True,
         "api_key_env": "SECURITY_MISSING_API_KEY"},
        {"id": "present", "type": "pixabay_api", "enabled": True,
         "api_key_env": "SECURITY_PRESENT_API_KEY"},
    ]}
    forbidden = Mock(side_effect=AssertionError("The missing-key branch must not use models or HTTP"))
    monkeypatch.setattr(image_pipeline, "LocalCLIP", forbidden)
    monkeypatch.setattr(image_pipeline, "SafeHTTP", forbidden)
    report = image_pipeline.scan(tmp_path, cfg=config)
    assert report["state"] == "needs_source_key"
    assert report["missing_env_names"] == ["SECURITY_MISSING_API_KEY"]
    reports = list(tmp_path.rglob("latest.json"))
    assert len(reports) == 1
    stored = reports[0].read_text(encoding="utf-8")
    assert json.loads(stored) == report and secret not in stored
    monkeypatch.setattr(image_pipeline, "scan", lambda *args, **kwargs: report)
    monkeypatch.setattr("max_postiz_agent.image_schedule.lock", lambda *args: nullcontext())
    args = argparse.Namespace(image_command="scan", all_sources=False, unused=False, channel="test")
    assert image_commands.execute(args, tmp_path) == 1
    logged = capsys.readouterr().out
    assert json.loads(logged)["missing_env_names"] == ["SECURITY_MISSING_API_KEY"]
    assert secret not in logged
    forbidden.assert_not_called()


@pytest.mark.parametrize("supplied_role", ["viewer", "owner\r\nSet-Cookie: injected=1", "owner; injected=1"])
def test_login_cookie_comes_from_authenticated_role_not_request_body(service, monkeypatch, supplied_role):
    """CodeQL 3: проверяем настоящий set-cookie HTTP-маршрута, а не копию его реализации."""
    secret = "synthetic-cookie-owner-key-not-a-real-credential"
    monkeypatch.setenv(service.settings.app["api"]["auth_secret_env"], secret)
    monkeypatch.setenv("APP_EDITOR_SECRET", "synthetic-cookie-editor-key-not-a-real-credential")
    monkeypatch.setenv("APP_VIEWER_SECRET", "synthetic-cookie-viewer-key-not-a-real-credential")
    with TestClient(create_app(service)) as client:
        response = client.post("/api/v1/session/login", headers={"Authorization": "Bearer " + secret},
                               json={"role": supplied_role})
        assert response.status_code == 200 and response.json()["role"] == "owner"
        token = client.cookies.get(panel_auth.COOKIE)
        assert re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", token)
        assert panel_auth.decode_session(service.settings, token) == "owner"
        cookie = response.headers.get_list("set-cookie")
        assert len(cookie) == 1 and "\r" not in cookie[0] and "\n" not in cookie[0]
        assert secret not in cookie[0] and "HttpOnly" in cookie[0] and "SameSite=strict" in cookie[0]


@pytest.mark.parametrize("role", ["owner\r\nSet-Cookie: injected=1", "owner; injected=1", "owner,viewer"])
def test_session_encoder_rejects_unrecognized_roles_and_tampered_hmac(monkeypatch, role):
    settings = SimpleNamespace(app={"api": {"auth_secret_env": "SECURITY_OWNER_KEY"}})
    monkeypatch.setenv("SECURITY_OWNER_KEY", "synthetic-owner-key-not-a-real-credential")
    monkeypatch.setenv("APP_EDITOR_SECRET", "synthetic-editor-key-not-a-real-credential")
    monkeypatch.setenv("APP_VIEWER_SECRET", "synthetic-viewer-key-not-a-real-credential")
    with pytest.raises(AgentError, match="AUTH_INVALID"):
        panel_auth.encode_session(settings, role, at=100)
    token = panel_auth.encode_session(settings, "owner", at=100)
    replacement = "0" if token[-1] != "0" else "1"
    with pytest.raises(AgentError, match="AUTH_INVALID"):
        panel_auth.decode_session(settings, token[:-1] + replacement, at=101)


def test_desktop_helpers_keep_fixed_executables_and_scalar_path_arguments(tmp_path, monkeypatch):
    """CodeQL 4/5: фиксируем только argv; Explorer/Tk не запускаются ни на одной ОС."""
    settings = SimpleNamespace(root=tmp_path, app={"max": {"browser": {}}, "opencode": {}})
    service = SimpleNamespace(settings=settings)
    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    fixture = tmp_path / "fixture, & echo not-a-command;.txt"
    fixture.write_text("synthetic fixture", encoding="utf-8")
    system_root = tmp_path / "synthetic-system-root"
    # Заменяем ссылку os только у этого модуля, не меняя платформу всего Python.
    monkeypatch.setattr(panel_files, "os", SimpleNamespace(name="nt", environ={"SystemRoot": str(system_root)}))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"state":"cancelled"}'))
    monkeypatch.setattr(panel_files.subprocess, "run", run)
    assert panel_files.native_pick(fixture, "file", "file") == {"state": "cancelled"}
    command, options = run.call_args.args[0], run.call_args.kwargs
    assert isinstance(command, list) and Path(command[0]).is_absolute() and command[0] == sys.executable
    assert Path(command[1]) == Path(panel_files.__file__).with_name("file_dialog.py")
    assert command[2:4] == [str(fixture), "file"] and len(command) == 5
    assert options.get("shell", False) is False
    launch = Mock()
    monkeypatch.setattr(panel_files.subprocess, "Popen", launch)
    assert panel_files.explorer(service, local, {"path": fixture.name})["state"] == "explorer_opened"
    command, options = launch.call_args.args[0], launch.call_args.kwargs
    assert command == [str(system_root / "explorer.exe"), "/select," + str(fixture.resolve())]
    assert Path(command[0]).is_absolute() and options.get("shell", False) is False


@pytest.mark.parametrize("value,host", [
    ("../outside.txt", "127.0.0.1"), ("\r\n", "127.0.0.1"), ("\0", "127.0.0.1"),
    ("fixture.txt", "192.0.2.1"),
])
def test_explorer_rejects_remote_or_invalid_paths_before_launch(tmp_path, monkeypatch, value, host):
    settings = SimpleNamespace(root=tmp_path, app={"max": {"browser": {}}, "opencode": {}})
    service = SimpleNamespace(settings=settings)
    request = SimpleNamespace(client=SimpleNamespace(host=host))
    (tmp_path / "fixture.txt").write_text("synthetic fixture", encoding="utf-8")
    monkeypatch.setattr(panel_files, "os", SimpleNamespace(name="nt", environ={}))
    launch = Mock(side_effect=AssertionError("Invalid or remote requests must not launch programs"))
    monkeypatch.setattr(panel_files.subprocess, "Popen", launch)
    with pytest.raises(AgentError):
        panel_files.explorer(service, request, {"path": value})
    launch.assert_not_called()


def test_python_argv_metacharacters_are_not_expanded_by_a_shell():
    """Фиксированный Python echo-процесс проверяет реальную сериализацию argv ОС."""
    values = ["literal & no-command", "literal; no-command", "literal, /select, no-command",
              'literal"quote', "literal $(no-command)", "literal %SECURITY_PRESENT_API_KEY%"]
    result = subprocess.run([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values],
                            shell=False, capture_output=True, text=True, encoding="utf-8", check=True, timeout=15)
    assert json.loads(result.stdout) == values
