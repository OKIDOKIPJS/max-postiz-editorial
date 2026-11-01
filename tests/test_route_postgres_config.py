"""Route PostgreSQL tests accept explicit test databases without workstation pins."""

import pytest

import test_route_dispatch_postgres as routes


class DatabaseBoundaryReached(Exception):
    """Stop before creating an engine or accessing PostgreSQL."""


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://127.0.0.1:5432/test_routes",
    "postgresql+psycopg://localhost:55499/editorial_tests",
    "postgresql+psycopg://localhost/test_dedicated",
    "postgresql+psycopg://test-db.invalid:5433/routes_tests",
])
def test_explicit_dedicated_database_reaches_database_boundary(monkeypatch, url):
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    calls = []

    def boundary(value):
        calls.append(value)
        raise DatabaseBoundaryReached

    monkeypatch.setattr(routes, "create_engine", boundary)
    probe = routes.postgres_routes.__wrapped__(object())
    try:
        with pytest.raises(DatabaseBoundaryReached):
            next(probe)
    except pytest.skip.Exception:
        pytest.fail("Valid explicit test database was skipped before the database boundary")
    assert calls == [url]


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://user:private-fixture-marker@127.0.0.1/editorial",
    "postgresql+psycopg2://user:private-fixture-marker@127.0.0.1/test_routes",
    "postgresql+psycopg://user:private-fixture-marker@127.0.0.1/test_routes?options=-csearch_path=public",
    "postgresql+psycopg://user:private-fixture-marker@127.0.0.1/test_routes?sslmode=require",
    "postgresql+psycopg://user:private-fixture-marker@127.0.0.1/test_",
    "private-fixture-marker malformed URL",
    "postgresql+psycopg://user:private-fixture-marker@127.0.0.1:invalid/test_routes",
])
def test_invalid_explicit_database_fails_before_connect_without_disclosing_value(monkeypatch, url):
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    calls = []
    monkeypatch.setattr(routes, "create_engine", lambda value: calls.append(value))
    probe = routes.postgres_routes.__wrapped__(object())
    with pytest.raises(pytest.fail.Exception, match="^Invalid TEST_DATABASE_URL") as error:
        next(probe)
    assert "private-fixture-marker" not in str(error.value)
    assert error.value.__context__ is None
    assert calls == []


def test_missing_database_is_an_explicit_optional_postgres_skip(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    calls = []
    monkeypatch.setattr(routes, "create_engine", lambda value: calls.append(value))
    probe = routes.postgres_routes.__wrapped__(object())
    with pytest.raises(pytest.skip.Exception, match="^TEST_DATABASE_URL is not configured$"):
        next(probe)
    assert calls == []
