"""CLI не согласует прошлую дату и сохраняет точный будущий часовой пояс."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from max_postiz_agent import routes_cli


@pytest.mark.parametrize("at,expected", [
    ("2026-10-31T12:00:00+03:00", "SCHEDULE_FUTURE_REQUIRED"),
    ("2026-10-31T13:00:00+03:00", "SCHEDULE_FUTURE_REQUIRED"),
    ("2026-10-31T14:00:00", "SCHEDULE_TIMEZONE_REQUIRED"),
    ("2026-10-31T13:00:01+03:00", None),
])
def test_cli_approval_requires_strict_future_and_explicit_timezone(tmp_path, monkeypatch, capsys, at, expected):
    calls, closed = [], []

    def approve(*args, **kwargs):
        calls.append((args, kwargs))
        return "future-dispatch"

    core = SimpleNamespace(approve_variant=approve, close=lambda: closed.append(True))
    monkeypatch.setattr(routes_cli, "assert_isolated", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes_cli, "load_env", lambda root: None)
    monkeypatch.setattr(routes_cli, "build", lambda root: core)
    monkeypatch.setattr(routes_cli, "now", lambda: datetime.fromisoformat("2026-10-31T10:00:00+00:00").timestamp())
    result = routes_cli.main(["--root", str(tmp_path), "approve", "variant-123", "--revision", "7",
        "--content-hash", "a" * 64, "--at", at])
    output = json.loads(capsys.readouterr().out)
    assert closed == [True]
    if expected:
        assert result == 1 and output == {"code": expected} and calls == []
    else:
        assert result == 0 and output == {"dispatch_id": "future-dispatch", "state": "approved"}
        assert calls == [(("variant-123", 7, "a" * 64, datetime.fromisoformat(at).timestamp()), {"actor": "owner"})]
