"""TaskQueueRepository port (repository-port-contract §11)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cctv_memory.contracts.task import Task
from cctv_memory.repositories.types import Page


@runtime_checkable
class TaskQueueRepository(Protocol):
    """Task queue persistence port.

    Claim semantics: only ``status=queued`` tasks with ``next_run_at <= now``
    are claimed; a claim writes ``lease_owner`` and ``lease_expires_at``; an
    expired lease may be reclaimed (database-adapter-contract §3.5).

    Timestamp parameters use the canonical domain type ``datetime`` (not ISO
    strings); each adapter converts at its boundary (database-adapter-contract
    §4.0).
    """

    def enqueue_task(self, task: Task) -> Task: ...

    def claim_task(
        self, worker_id: str, now: datetime, lease_seconds: int
    ) -> Task | None: ...

    def refresh_lease(self, task_id: str, worker_id: str, lease_until: datetime) -> None: ...

    def mark_succeeded(self, task_id: str) -> None: ...

    def mark_failed(
        self, task_id: str, error_code: str, error_message: str | None = None
    ) -> None: ...

    def schedule_retry(self, task_id: str, next_run_at: datetime) -> None: ...

    def list_pending(self, cursor: str | None = None, limit: int = 50) -> Page[Task]: ...
