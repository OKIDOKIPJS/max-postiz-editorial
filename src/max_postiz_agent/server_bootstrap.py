"""Normal server startup checks the existing schema; migration is explicit."""

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from .config import AgentError
from .db import Base


def require_schema(db):
    # Register all declared tables without invoking initialize/create_all/upgrade.
    from . import hub_models as hub_models, server_models as server_models
    try:
        if db.engine.dialect.name == "sqlite" and db.engine.url.database != ":memory:":
            if not Path(db.engine.url.database).is_file():
                raise AgentError("SERVER_MIGRATION_REQUIRED", 503)
        inspector = inspect(db.engine)
        names = set(inspector.get_table_names())
        if not set(Base.metadata.tables) <= names:
            raise AgentError("SERVER_MIGRATION_REQUIRED", 503)
        for name, table in Base.metadata.tables.items():
            if not set(table.columns.keys()) <= {column["name"] for column in inspector.get_columns(name)}:
                raise AgentError("SERVER_MIGRATION_REQUIRED", 503)
        if db.engine.dialect.name == "postgresql":
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            root = db.root if (db.root / "migrations").is_dir() else Path(__file__).resolve().parents[2]
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            expected = set(ScriptDirectory.from_config(cfg).get_heads())
            with db.engine.connect() as connection:
                actual = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
            if actual != expected:
                raise AgentError("SERVER_MIGRATION_REQUIRED", 503)
    except (SQLAlchemyError, OSError, TypeError):
        raise AgentError("SERVER_MIGRATION_REQUIRED", 503) from None
