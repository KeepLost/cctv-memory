"""Job / task state-machine rules (job-state-machine-contract).

Pure domain logic enumerating legal transitions. Workers/orchestrators must
consult these rather than inventing state semantics (contract §0).
"""

from __future__ import annotations

from cctv_memory.domain.enums import JobStatus, TaskStatus

_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.PARTIAL_FAILED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PARTIAL_FAILED: frozenset({JobStatus.RUNNING, JobStatus.SUCCEEDED}),
    JobStatus.FAILED: frozenset({JobStatus.RUNNING}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.SKIPPED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL_FAILED, TaskStatus.FAILED}
    ),
    TaskStatus.PARTIAL_FAILED: frozenset({TaskStatus.RUNNING}),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNING}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
}


def can_transition_job(current: JobStatus, target: JobStatus) -> bool:
    """Return True if a job may move from ``current`` to ``target`` (contract §1.1)."""
    return target in _JOB_TRANSITIONS.get(current, frozenset())


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    """Return True if a scale task may move from ``current`` to ``target`` (§2)."""
    return target in _TASK_TRANSITIONS.get(current, frozenset())
