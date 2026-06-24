"""WriteCoordinator implementations (infrastructure/db).

Task cctv-memory-20260616-1850 (Phase 3 / A1). The concrete database adapter owns
the write-serialization policy so the worker/business layer stays backend-agnostic.

- ``SqliteWriteCoordinator`` serializes writes with a process-local lock: SQLite is
  a single-writer store and under WAL a concurrent write upgrade from two
  connections deadlocks (``busy_timeout`` cannot resolve it). One coordinator
  instance is shared across the worker's units/scales/jobs so every DB write
  critical section is serialized on ONE writer — exactly what the worker's old
  ``threading.Lock`` did, now living at the database boundary.
- ``NullWriteCoordinator`` performs no serialization. It documents (and is ready
  for) a future PostgreSQL adapter where MVCC handles concurrent writers natively;
  the worker would use it unchanged. Not wired into the SQLite MVP runtime.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from cctv_memory.infrastructure.db.write_intent import write_intent


class SqliteWriteCoordinator:
    """Serialize DB writes through one process-local lock (SQLite single-writer).

    Also marks the current thread with ``write_intent`` for the duration of the
    critical section so the engine begins the transaction with ``BEGIN IMMEDIATE``
    (write-first). The lock serializes writers WITHIN a process; ``BEGIN IMMEDIATE``
    + ``busy_timeout`` serializes writers ACROSS processes (the process-local lock
    cannot, task cctv-memory-20260617-1441). Both layers matter for a multi-process
    SQLite deployment.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._lock, write_intent():
            yield


class NullWriteCoordinator:
    """No-op write coordinator for backends with native concurrent writes (e.g. PG).

    Performs no in-process serialization AND sets no write-intent: a PostgreSQL
    adapter relies on MVCC, and the SQLite-specific ``BEGIN IMMEDIATE`` write-first
    behavior does not apply. The worker uses it unchanged.
    """

    @contextmanager
    def write(self) -> Iterator[None]:
        yield
