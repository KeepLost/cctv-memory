"""WriteCoordinator port — backend-agnostic DB write-serialization boundary.

Task cctv-memory-20260616-1850 (Phase 3 / A1). Upper/business code (the worker)
must NOT own a SQLite-specific global write lock. Instead it wraps each DB write
critical section in ``coordinator.write()`` and lets the concrete database
adapter decide whether serialization is required:

- SQLite adapter: serializes writes (single-writer store; under WAL a write
  upgrade from two connections deadlocks and ``busy_timeout`` cannot resolve it).
- A future PostgreSQL adapter: ``write()`` can be a no-op because MVCC handles
  concurrent writers natively — and the worker needs no change.

This keeps the upper layer database-agnostic (database-capability-contract §13)
and puts the concurrency policy at the database boundary (ARCHITECTURE_CONSTITUTION
§7). VLM/provider calls MUST stay OUTSIDE this context so DB write critical
sections remain short (ARCHITECTURE_CONSTITUTION §9.1).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol, runtime_checkable


@runtime_checkable
class WriteCoordinator(Protocol):
    """Serialize a DB write critical section in a backend-appropriate way."""

    def write(self) -> AbstractContextManager[None]:
        """Return a context manager guarding one DB write critical section.

        The body should be a SHORT critical section (DB writes only); never run a
        VLM/provider call inside it (constitution §9.1).
        """
        ...


class _NoOpWriteCoordinator:
    """No-op coordinator (no serialization).

    Used only as a safe default on the legacy single-session serial path (no
    concurrency, so no serialization needed). The concurrent worker path always
    injects the backend's real coordinator (e.g. the runtime's
    ``SqliteWriteCoordinator``); this keeps the worker layer free of any
    infrastructure import or SQLite-specific lock.
    """

    @contextmanager
    def write(self) -> Iterator[None]:
        yield


NO_OP_WRITE_COORDINATOR: WriteCoordinator = _NoOpWriteCoordinator()
