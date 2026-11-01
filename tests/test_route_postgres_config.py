"""Route PostgreSQL tests accept explicit test databases without workstation pins."""

import pytest

import test_route_dispatch_postgres as routes


@pytest.fixture(autouse=True)
def portable_database_environment(monkeypatch):
    """URL-validator examples are independent of an inherited owned PG opt-in."""
    monkeypatch.delenv(routes.CONTRACT_ENV, raising=False)


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


@pytest.mark.parametrize("contract,url", [
    ("monitor-main-integration-v1", "postgresql+psycopg://qa:private-fixture-marker@127.0.0.1:5432/test_routes"),
    ("accept-any-postgres", "postgresql+psycopg://qa:private-fixture-marker@127.0.0.1:55450/editorial_hub_tests"),
])
def test_owned_contract_never_falls_back_to_portable_url_validation(monkeypatch, contract, url):
    monkeypatch.setenv(routes.CONTRACT_ENV, contract)
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    monkeypatch.setenv("MONITOR_OWNED_PG_TEST_URL",
                       "postgresql+psycopg://qa:private-fixture-marker@127.0.0.1:55451/editorial_hub_tests")
    calls = []
    monkeypatch.setattr(routes, "create_engine", lambda value: calls.append(value))
    probe = routes.postgres_routes.__wrapped__(object())
    with pytest.raises(ValueError, match="^PG_TEST_") as error:
        next(probe)
    assert "private-fixture-marker" not in str(error.value)
    assert calls == []


@pytest.mark.parametrize("identity_accepted", [False, True])
def test_owned_contract_checks_identity_and_closes_read_before_any_schema_ddl(
    monkeypatch, identity_accepted
):
    monkeypatch.setenv(routes.CONTRACT_ENV, "monitor-main-integration-v1")
    monkeypatch.setenv("TEST_DATABASE_URL",
                       "postgresql+psycopg://qa:private-fixture-marker@127.0.0.1:55450/editorial_hub_tests")
    monkeypatch.setenv("MONITOR_OWNED_PG_TEST_URL",
                       "postgresql+psycopg://qa:private-fixture-marker@127.0.0.1:55451/editorial_hub_tests")
    calls = []

    class IdentityRejected(Exception):
        pass

    class ProbeEngine:
        def connect(self):
            calls.append("connect")
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append("close")

        def begin(self):
            calls.append("begin")
            raise DatabaseBoundaryReached

        def dispose(self):
            calls.append("dispose")

    engine = ProbeEngine()

    def factory(_url):
        calls.append("engine")
        return engine

    def verify(connection, spec):
        assert connection is engine
        assert spec["contract"] == "monitor-main-integration-v1"
        assert spec["role"] == "routes" and spec["port"] == 55450
        calls.append("identity")
        if not identity_accepted:
            raise IdentityRejected

    monkeypatch.setattr(routes, "create_engine", factory)
    monkeypatch.setattr(routes, "verify_identity", verify)
    probe = routes.postgres_routes.__wrapped__(object())
    with pytest.raises(DatabaseBoundaryReached if identity_accepted else IdentityRejected):
        next(probe)
    assert calls == ["engine", "connect", "identity", "close",
                     "begin" if identity_accepted else "dispose"]
