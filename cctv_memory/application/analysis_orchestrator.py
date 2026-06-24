"""Analysis orchestration use case (application/analysis_orchestrator.py).

Drives AnalysisJob / AnalysisScaleTask status changes, validating each
transition against the domain state machine (job-state-machine-contract). The
worker calls these to advance state; illegal transitions raise
InvalidStateTransitionError rather than being silently applied.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.domain.enums import JobStatus, TaskStatus
from cctv_memory.domain.exceptions import InvalidStateTransitionError, NotFoundError
from cctv_memory.domain.state_machine import can_transition_job, can_transition_task
from cctv_memory.repositories.analysis import (
    AnalysisJobRepository,
    AnalysisScaleTaskRepository,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AnalysisOrchestrator:
    """Validated state transitions for jobs and scale tasks."""

    def __init__(
        self, jobs: AnalysisJobRepository, scale_tasks: AnalysisScaleTaskRepository
    ) -> None:
        self._jobs = jobs
        self._scale_tasks = scale_tasks

    def transition_job(
        self,
        analysis_job_id: str,
        target: JobStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        job = self._jobs.get_job(analysis_job_id)
        if job is None:
            raise NotFoundError(f"job {analysis_job_id} not found")
        if job.job_status == target:
            return
        if not can_transition_job(job.job_status, target):
            raise InvalidStateTransitionError(
                f"job {analysis_job_id}: {job.job_status.value} -> {target.value}"
            )
        started_at = _now_iso() if target is JobStatus.RUNNING else None
        finished_at = (
            _now_iso()
            if target in (JobStatus.SUCCEEDED, JobStatus.PARTIAL_FAILED, JobStatus.FAILED)
            else None
        )
        self._jobs.update_status(
            analysis_job_id,
            target.value,
            started_at=started_at,
            finished_at=finished_at,
            error_code=error_code,
            error_message=error_message,
        )

    def transition_scale_task(
        self,
        scale_task_id: str,
        target: TaskStatus,
        *,
        error_code: str | None = None,
        skipped_reason: str | None = None,
    ) -> None:
        task = self._scale_tasks.get_scale_task(scale_task_id)
        if task is None:
            raise NotFoundError(f"scale task {scale_task_id} not found")
        if task.status == target:
            return
        if not can_transition_task(task.status, target):
            raise InvalidStateTransitionError(
                f"scale_task {scale_task_id}: {task.status.value} -> {target.value}"
            )
        self._scale_tasks.update_status(
            scale_task_id,
            target.value,
            error_code=error_code,
            skipped_reason=skipped_reason,
        )
