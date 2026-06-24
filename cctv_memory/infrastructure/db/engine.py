"""SQLite engine and session helpers (infrastructure layer).

Applies the SQLite pragmas required by database-adapter-contract §3.1:
WAL journal mode, foreign_keys=ON, busy_timeout. This module is infrastructure
only and must never be imported by application/domain code.

Performance/durability tuning (task cctv-memory-20260616-1850): under WAL,
``synchronous=NORMAL`` removes the per-commit fsync of the FULL default while
remaining crash-safe (no corruption); only the last few committed transactions
can be lost on OS crash / power loss, which Eric has explicitly accepted for this
project. This is a SQLite-specific decision and stays here in the adapter layer —
upper/business code never sees it (database-capability-contract §13).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from cctv_memory.infrastructure.db.write_intent import is_write_intent

DEFAULT_BUSY_TIMEOUT_MS = 5000
# WAL pages between automatic checkpoints. The SQLite default is 1000; keeping it
# explicit documents intent and bounds WAL growth under sustained write load.
DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 1000


def _configure_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
    """Set per-connection SQLite pragmas (WAL, FK, busy timeout, synchronous).

    ``synchronous=NORMAL`` (safe under WAL) is the key write-throughput tuning:
    it drops the per-commit fsync without risking database corruption. See module
    docstring for the durability trade-off (owner-approved).

    Write transactions are made write-first by the ``begin`` event below, only when
    the current thread is inside ``write_intent``. Read paths keep pysqlite's normal
    deferred/autocommit behavior so SELECT-only sessions do not hold an avoidable
    read snapshot or serialize readers.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA wal_autocheckpoint={DEFAULT_WAL_AUTOCHECKPOINT_PAGES}")
    finally:
        cursor.close()


def _emit_begin(conn: Any) -> None:
    """Begin write transactions with ``BEGIN IMMEDIATE``.

    Multi-process SQLite hardening (task cctv-memory-20260617-1441,
    database-adapter-contract §3.1/§8): a deferred read transaction that later
    upgrades read->write deadlocks across connections/processes and ``busy_timeout``
    does not wait on the upgrade. A write critical section (marked via
    ``write_intent`` by ``SqliteWriteCoordinator.write()``) therefore acquires the
    write lock UP FRONT with ``BEGIN IMMEDIATE`` so ``busy_timeout`` serializes
    writers cleanly. Read/search paths never set the flag, so this hook emits
    nothing for them and they keep SQLAlchemy/pysqlite's normal deferred behavior
    (no added read serialization and no long-lived read snapshot from this hook).

    ``conn`` is the SQLAlchemy ``Connection`` (the ``begin`` event argument); we
    issue the literal BEGIN via ``exec_driver_sql`` so it reaches SQLite directly.
    """
    if is_write_intent():
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def sqlite_url(sqlite_path: str | Path) -> str:
    """Build a SQLAlchemy sync SQLite URL from a filesystem path."""
    return f"sqlite:///{Path(sqlite_path)}"


def create_sqlite_engine(sqlite_path: str | Path, *, echo: bool = False) -> Engine:
    """Create a sync SQLite engine with required pragmas applied per connection."""
    engine = create_engine(sqlite_url(sqlite_path), echo=echo, future=True)
    event.listen(engine, "connect", _configure_sqlite_pragmas)
    event.listen(engine, "begin", _emit_begin)
    return engine


def create_postgres_engine(
    dsn: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
) -> Engine:
    """Create a sync PostgreSQL engine.

    PostgreSQL uses normal MVCC transactions. Do not install SQLite PRAGMAs or
    ``BEGIN IMMEDIATE`` hooks here.
    """
    return create_engine(
        dsn,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional session scope: commit on success, rollback on error."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
