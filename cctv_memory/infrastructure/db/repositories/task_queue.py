"""SQLite TaskQueue adapter.

Claim semantics (database-adapter-contract §3.5): only ``queued`` tasks with
``next_run_at <= now`` are claimed; a claim sets ``lease_owner`` and
``lease_expires_at``; a task whose lease has expired can be reclaimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from cctv_memory.contracts.task import Task
from cctv_memory.infrastructure.db import mappers
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.repositories.types import Page

# Bound on how many candidate rows a single claim attempt will probe before
# giving up (no unbounded/full-table scan). Each probe is a conditional UPDATE;
# a lost race (rowcount==0) advances to the next due candidate.
_CLAIM_MAX_PROBES = 50


class SqliteTaskQueueRepository:
    """TaskQueueRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue_task(self, task: Task) -> Task:
        self._session.add(mappers.task_to_orm(task))
        self._session.flush()
        return task

    def claim_task(self, worker_id: str, now: datetime, lease_seconds: int) -> Task | None:
        """Atomically claim one due task; safe under concurrent workers.

        A claimable task is either ``queued`` OR ``running`` with an expired lease,
        with ``next_run_at <= now``. Selection order is priority desc, then
        ``next_run_at`` (oldest first). The claim itself is a CONDITIONAL UPDATE
        guarded by the row's id AND its still-claimable status/lease, so two
        workers can never both win the same row: at most one UPDATE matches
        (``rowcount==1``); the loser sees ``rowcount==0`` and probes the next
        candidate. Bounded by ``_CLAIM_MAX_PROBES`` (no full-table scan).
        """
        # The canonical param is a datetime; SQLite stores timestamps as ISO
        # text, so convert at this adapter boundary for the column comparisons.
        now_iso = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()

        # Claimable predicate (depends only on now_iso, so build once): due AND
        # (queued OR running-with-expired-lease). Reused by both the candidate
        # SELECT and the conditional UPDATE so the write re-checks at write time.
        due = orm.AnalysisTask.next_run_at <= now_iso
        still_claimable = or_(
            orm.AnalysisTask.status == "queued",
            (orm.AnalysisTask.status == "running")
            & (orm.AnalysisTask.lease_expires_at.is_not(None))
            & (orm.AnalysisTask.lease_expires_at < now_iso),
        )

        for _ in range(_CLAIM_MAX_PROBES):
            row = self._session.scalar(
                select(orm.AnalysisTask)
                .where(due, still_claimable)
                .order_by(orm.AnalysisTask.priority.desc(), orm.AnalysisTask.next_run_at)
                .limit(1)
            )
            if row is None:
                return None
            task_id = row.task_id
            # Conditional UPDATE: only succeeds if the row is STILL claimable
            # (re-checks status/lease at write time). Atomic single-writer claim.
            result = self._session.execute(
                update(orm.AnalysisTask)
                .where(orm.AnalysisTask.task_id == task_id, due, still_claimable)
                .values(
                    status="running",
                    lease_owner=worker_id,
                    lease_expires_at=lease_until,
                    updated_at=now_iso,
                )
            )
            self._session.flush()
            if result.rowcount == 1:  # type: ignore[attr-defined]
                # Re-read the freshly-claimed row (expire the stale identity-map
                # copy first so we observe the committed claim values).
                self._session.expire(row)
                claimed = self._session.get(orm.AnalysisTask, task_id)
                if claimed is not None:
                    return mappers.task_to_dto(claimed)
                return None
            # Lost the race for this row; expire it so the next select sees the
            # updated state, then probe the next candidate.
            self._session.expire(row)
        return None

    def refresh_lease(self, task_id: str, worker_id: str, lease_until: datetime) -> None:
        row = self._session.get(orm.AnalysisTask, task_id)
        if row is not None and row.lease_owner == worker_id:
            row.lease_expires_at = lease_until.isoformat()
            self._session.flush()

    def mark_succeeded(self, task_id: str) -> None:
        row = self._session.get(orm.AnalysisTask, task_id)
        if row is not None:
            row.status = "succeeded"
            row.lease_owner = None
            row.lease_expires_at = None
            self._session.flush()

    def mark_failed(
        self, task_id: str, error_code: str, error_message: str | None = None
    ) -> None:
        row = self._session.get(orm.AnalysisTask, task_id)
        if row is None:
            return
        if row.retry_count < row.max_retries:
            row.status = "retry_scheduled"
        else:
            row.status = "failed"
        row.error_code = error_code
        row.error_message = error_message
        row.lease_owner = None
        row.lease_expires_at = None
        self._session.flush()

    def schedule_retry(self, task_id: str, next_run_at: datetime) -> None:
        row = self._session.get(orm.AnalysisTask, task_id)
        if row is None:
            return
        row.status = "queued"
        row.retry_count = row.retry_count + 1
        row.next_run_at = next_run_at.isoformat()
        row.lease_owner = None
        row.lease_expires_at = None
        self._session.flush()

    def list_pending(self, cursor: str | None = None, limit: int = 50) -> Page[Task]:
        rows = list(
            self._session.scalars(
                select(orm.AnalysisTask)
                .where(orm.AnalysisTask.status == "queued")
                .order_by(orm.AnalysisTask.next_run_at)
                .limit(limit)
            )
        )
        return Page(items=[mappers.task_to_dto(r) for r in rows])
