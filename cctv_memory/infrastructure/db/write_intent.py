"""Thread-local SQLite write-intent flag (infrastructure/db).

Task cctv-memory-20260617-1441 (multi-process SQLite hardening). A process-local
``WriteCoordinator`` lock does NOT span processes; in a multi-process deployment
SQLite's OWN write-transaction lock is what serializes writers. Under WAL a
transaction that starts as a deferred READ (the default for the first SELECT) and
later issues a write must UPGRADE read->write; if two connections each hold a read
transaction and both try to upgrade, SQLite returns ``SQLITE_BUSY`` immediately and
``busy_timeout`` does NOT wait on an upgrade — producing ``database is locked`` (and,
empirically, lost updates / deadlocks).

The fix is to begin WRITE transactions as ``BEGIN IMMEDIATE`` so the write lock is
acquired up front and ``busy_timeout`` serializes writers cleanly. We must NOT do
this for read/search transactions (that would needlessly serialize reads). This
module provides a thread-local flag that the engine's ``begin`` event reads to
decide whether to override the transaction start with ``BEGIN IMMEDIATE``. When
the flag is absent, the hook emits nothing and read/search paths keep normal
pysqlite/SQLAlchemy deferred behavior.

The flag is set by ``SqliteWriteCoordinator.write()`` — i.e. "going through the
write coordinator" is exactly "this is a write critical section", so the same call
that serializes in-process also marks the SQLite transaction as write-first. Read
and search paths never enter the coordinator, so they stay unaffected.

This is SQLite-specific and stays inside the infrastructure/db boundary; upper
layers never see it (database-capability-contract §13). A future PostgreSQL adapter
ignores it entirely (MVCC handles concurrent writers).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


def is_write_intent() -> bool:
    """True if the current thread is inside a write critical section."""
    return getattr(_local, "write_intent", False)


@contextmanager
def write_intent() -> Iterator[None]:
    """Mark the current thread as intending to write (re-entrant).

    While active, the engine's ``begin`` hook issues ``BEGIN IMMEDIATE`` so the
    write transaction acquires the SQLite write lock up front. Re-entrant: nested
    ``write_intent()`` blocks restore the previous value on exit, so a coordinated
    write inside another coordinated write behaves correctly.
    """
    previous = getattr(_local, "write_intent", False)
    _local.write_intent = True
    try:
        yield
    finally:
        _local.write_intent = previous
