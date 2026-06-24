"""Lease heartbeat — renew a claimed task's lease while it is being processed.

Task cctv-memory-20260616-1850 §B4 (job-state-machine-contract §4: "worker must
refresh lease for long tasks"). Under raised ``worker.max_concurrent_jobs`` a job
that runs longer than ``lease_seconds`` would otherwise have its queue task
re-claimed by another worker (lease-expiry reclaim, database-adapter-contract
§3.5), causing duplicate processing and a finalize race.

A ``LeaseHeartbeat`` runs a daemon timer that periodically calls
``refresh_lease(task_id, worker_id, now + lease_seconds)`` in its own short DB
write (through the backend write coordinator) until stopped. ``refresh_lease`` is
ownership-guarded (only the current ``lease_owner`` renews), so a worker that has
already lost the lease cannot extend someone else's claim. Renewal failures are
logged and never raise into the processing thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    """Background lease renewer for one in-flight task (context manager)."""

    def __init__(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
        renew_seconds: int,
        renew: Callable[[str, str, datetime], None],
    ) -> None:
        self._task_id = task_id
        self._worker_id = worker_id
        self._lease_seconds = max(1, int(lease_seconds))
        # Renew strictly more often than the lease expires; clamp to < lease.
        self._renew_seconds = max(1, min(int(renew_seconds), self._lease_seconds - 1))
        self._renew = renew
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self._renew_seconds):
            lease_until = datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
            try:
                self._renew(self._task_id, self._worker_id, lease_until)
            except Exception:  # noqa: BLE001 - renewal must not crash processing
                logger.exception(
                    "lease renewal failed for task %s (worker %s); will retry next tick",
                    self._task_id, self._worker_id,
                )

    def __enter__(self) -> LeaseHeartbeat:
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self._task_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
