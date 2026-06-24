"""Alembic environment for CCTV Memory.

The SQLite database path is resolved from the ``CCTV_MEMORY_SQLITE_PATH``
environment variable when present, otherwise from the Alembic config URL. The
engine applies the same SQLite pragmas as the runtime engine.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from cctv_memory.infrastructure.db.engine import _configure_sqlite_pragmas, sqlite_url
from cctv_memory.infrastructure.db.models import Base

config = context.config

target_metadata = Base.metadata


def _resolve_url() -> str:
    sqlite_path = os.environ.get("CCTV_MEMORY_SQLITE_PATH")
    if sqlite_path:
        return sqlite_url(sqlite_path)
    return config.get_main_option("sqlalchemy.url", "sqlite:///data/cctv_memory.sqlite3")


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    event.listen(connectable, "connect", _configure_sqlite_pragmas)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
