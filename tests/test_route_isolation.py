"""Копия отказывается читать чужое окружение и открывать рабочую БД."""

import json
from pathlib import Path

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.isolated_workspace import assert_isolated, mark_isolated
from max_postiz_agent.routes_cli import main


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "TEST_DATABASE_URL", "POSTIZ_BASE_URL",
                 "MAX_EXISTING_BROWSER_PROFILE", "OTHER_DATABASE", "OTHER_POSTIZ"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "copy"
    root.mkdir()
    (root / "config").mkdir()
    mark_isolated(root, tmp_path / "source")
    return root


@pytest.mark.parametrize("relation", ["same", "inside", "outside"])
def test_source_and_nested_roots_are_never_marked(isolated, relation):
    source = {"same": isolated, "inside": isolated / "child", "outside": isolated.parent}[relation]
    with pytest.raises(AgentError, match="ISOLATED_COPY_REQUIRED"):
        mark_isolated(isolated, source)


@pytest.mark.parametrize("name,value,code", [
    ("DATABASE_URL", "postgresql://test@remote.example.test/db", "ISOLATED_DATABASE_REQUIRED"),
    ("DATABASE_URL", "postgresql://test@localhost:5432/editorial_hub", "ISOLATED_DATABASE_REQUIRED"),
    ("TEST_DATABASE_URL", "postgresql://test@127.0.0.1:55448/production", "ISOLATED_DATABASE_REQUIRED"),
    ("POSTIZ_BASE_URL", "https://remote.example.test/api", "ISOLATED_POSTIZ_REQUIRED"),
    ("POSTIZ_BASE_URL", "http://localhost:4007/api/public/v1", "ISOLATED_POSTIZ_REQUIRED"),
    ("MAX_EXISTING_BROWSER_PROFILE", "source/data/max-browser", "ISOLATED_EXTERNAL_BROWSER_FORBIDDEN"),
])
def test_external_runtime_configuration_is_blocked(isolated, monkeypatch, name, value, code):
    monkeypatch.setenv(name, value)
    with pytest.raises(AgentError, match=code):
        assert_isolated(isolated)


@pytest.mark.parametrize("kind", ["database", "postiz"])
def test_custom_effective_environment_names_cannot_bypass_fence(isolated, monkeypatch, kind):
    if kind == "database":
        (isolated / "config/app.json").write_text(json.dumps({"database_url_env": "OTHER_DATABASE"}))
        monkeypatch.setenv("OTHER_DATABASE", "postgresql://test@remote.example.test/production")
        code = "ISOLATED_DATABASE_REQUIRED"
    else:
        (isolated / "config/publishing.json").write_text(json.dumps({
            "postiz": {"base_url_env": "OTHER_POSTIZ"}}))
        monkeypatch.setenv("OTHER_POSTIZ", "https://remote.example.test/api")
        code = "ISOLATED_POSTIZ_REQUIRED"
    with pytest.raises(AgentError, match=code):
        assert_isolated(isolated)


def test_only_task_local_postgres_and_postiz_are_accepted(isolated, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://fixture@127.0.0.1:55448/editorial_hub")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://fixture@localhost:55448/editorial_hub_tests")
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1:14007/api/public/v1")
    assert_isolated(isolated)


def test_dotenv_link_is_blocked_before_loader_runs(isolated, monkeypatch, capsys):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == isolated / ".env.local" or original(path))
    calls = []
    monkeypatch.setattr("max_postiz_agent.routes_cli.load_env", lambda root: calls.append(root))
    assert main(["--root", str(isolated), "status"]) == 1
    assert calls == []
    assert json.loads(capsys.readouterr().out)["code"] == "ISOLATED_STATE_LINK_FORBIDDEN"


@pytest.mark.parametrize("filename", ["agent.db", "agent.db-wal", "agent.db-shm"])
def test_linked_sqlite_files_are_rejected_before_database_open(isolated, monkeypatch, filename):
    (isolated / "config/app.json").write_text(json.dumps({"state_dir": "data/state"}))
    original = Path.is_symlink
    linked = isolated / "data/state" / filename
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == linked or original(path))
    with pytest.raises(AgentError, match="ISOLATED_STATE_LINK_FORBIDDEN"):
        assert_isolated(isolated)


@pytest.mark.parametrize("field", ["state_dir", "media_dir", "logs_dir"])
def test_runtime_directory_cannot_escape_copy(isolated, field):
    (isolated / "config/app.json").write_text(json.dumps({field: "../../source"}))
    with pytest.raises(AgentError, match="ISOLATED_STATE_PATH_INVALID"):
        assert_isolated(isolated)
