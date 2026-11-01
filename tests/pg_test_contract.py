"""Exact opt-in test endpoints; never a replacement for runtime database guards."""

import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


CONTRACT_ENV = "MAX_POSTIZ_PG_TEST_CONTRACT"
LOCAL_CONTRACT = "monitor-main-integration-v1"
CI_CONTRACT = "github-actions-isolated-pg-v1"
URL_ENVS = {"routes": "TEST_DATABASE_URL", "monitor": "MONITOR_OWNED_PG_TEST_URL"}
IDENTITY_SQL = (
    "SELECT current_database(), current_user, current_setting('server_version_num')::integer, "
    "current_setting('cluster_name'), current_setting('data_directory'), "
    "system_identifier::text, current_setting('port')::integer FROM pg_control_system()"
)


class PostgresTestContractError(ValueError):
    """Messages are fixed codes: URLs, credentials and server paths stay private."""


def endpoint_spec(role, url, root, *, environment=None):
    env = os.environ if environment is None else environment
    contract = env.get(CONTRACT_ENV)
    if role not in URL_ENVS or contract not in {LOCAL_CONTRACT, CI_CONTRACT}:
        raise PostgresTestContractError("PG_TEST_CONTRACT_REQUIRED")
    if contract == CI_CONTRACT and (
        env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_OS") != "Linux"
        or re.fullmatch(r"[0-9]+", env.get("GITHUB_RUN_ID", "")) is None
    ):
        raise PostgresTestContractError("PG_TEST_CI_CONTEXT_INVALID")
    local = contract == LOCAL_CONTRACT
    ports = {"routes": 55450 if local else 55448, "monitor": 55451 if local else 55449}
    for name, key in URL_ENVS.items():
        value = env.get(key)
        try:
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError
            parsed = urlsplit(value)
            if (
                parsed.scheme != "postgresql+psycopg" or parsed.hostname != "127.0.0.1"
                or parsed.port != ports[name] or parsed.path != "/editorial_hub_tests"
                or parsed.query or parsed.fragment or not parsed.username or not parsed.password
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise PostgresTestContractError("PG_TEST_ENDPOINT_INVALID") from None
    if url != env[URL_ENVS[role]]:
        raise PostgresTestContractError("PG_TEST_ENDPOINT_BINDING_CHANGED")
    parsed = urlsplit(url)
    directory = (Path(root).resolve() / ("data/postgres-qa-" + role) / "cluster") if local else Path("/var/lib/postgresql/data")
    if local:
        current = Path(root).resolve()
        for part in directory.relative_to(current).parts:
            current /= part
            if current.is_symlink() or current.is_junction():
                raise PostgresTestContractError("PG_TEST_CLUSTER_LINK_FORBIDDEN")
    cluster = ("codex-max-integration-" + role + "-qa") if local else (
        "codex-max-routes-ci" if role == "routes" else "codex-max-monitor-qa"
    )
    return {"contract": contract, "role": role, "port": ports[role],
            "database": "editorial_hub_tests", "user": unquote(parsed.username),
            "cluster_name": cluster, "data_directory": directory,
            "server_port": ports[role] if local else 5432}


def verify_identity(connection, spec):
    """Fixed SELECT only; callers may create schemas after this succeeds."""
    row = connection.exec_driver_sql(IDENTITY_SQL).one()
    try:
        database, user, version, cluster, directory, system_id, port = row
        if (
            database != spec["database"] or user != spec["user"] or type(version) is not int
            or not 170000 <= version < 180000 or cluster != spec["cluster_name"]
            or not isinstance(directory, str) or Path(directory).resolve() != spec["data_directory"].resolve()
            or not isinstance(system_id, str) or re.fullmatch(r"[0-9]{10,30}", system_id) is None
            or port != spec["server_port"]
        ):
            raise ValueError
    except (ValueError, TypeError, OSError, RuntimeError):
        raise PostgresTestContractError("PG_TEST_SERVER_IDENTITY_INVALID") from None
    return system_id
