"""Ingestion use case (application/ingestion.py).

Submit a local video source: canonicalize the source_uri, create/reuse the
VideoSource, create the AnalysisJob (idempotent), create the default_segment
AnalysisScaleTask, and enqueue a queue task for the embedded/standalone worker.

Application layer: orchestrates ports + domain, owns the transaction boundary,
and never imports infrastructure concretes (ARCHITECTURE_CONSTITUTION §3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from cctv_memory.contracts.analysis import AnalysisJob, AnalysisScaleTask
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import Principal
from cctv_memory.contracts.task import Task
from cctv_memory.contracts.video import (
    SubmitVideoSourceRequest,
    SubmitVideoSourceResponse,
)
from cctv_memory.domain.enums import AnalysisScale, Capability, JobStatus, TaskStatus
from cctv_memory.domain.exceptions import CapabilityDeniedError
from cctv_memory.repositories.analysis import (
    AnalysisJobRepository,
    AnalysisScaleTaskRepository,
)
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.task_queue import TaskQueueRepository
from cctv_memory.repositories.video_source import VideoSourceRepository

DEFAULT_PIPELINE_VERSION = "pipeline-v1"
# The OpenCV streaming-decode + metric-selection path produces DIFFERENT frames
# than the legacy ffmpeg uniform-seek path, so it carries a distinct
# pipeline_version for reproducibility (pipeline-experiment-contract §3.2).
OPENCV_PIPELINE_VERSION = "pipeline-v2-opencv-selector"
DEFAULT_PROMPT_VERSION = "prompt-v1"
DEFAULT_MODEL_VERSION = "mock-vlm-v1"
TASK_TYPE_ANALYZE_VIDEO = "analyze_video"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class IngestionService:
    """Submit-video-source use case."""

    def __init__(
        self,
        video_sources: VideoSourceRepository,
        jobs: AnalysisJobRepository,
        scale_tasks: AnalysisScaleTaskRepository,
        task_queue: TaskQueueRepository,
        audit: AuditRepository,
        *,
        model_version: str = DEFAULT_MODEL_VERSION,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    ) -> None:
        self._video_sources = video_sources
        self._jobs = jobs
        self._scale_tasks = scale_tasks
        self._task_queue = task_queue
        self._audit = audit
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._pipeline_version = pipeline_version

    def submit(
        self,
        request: SubmitVideoSourceRequest,
        principal: Principal,
        *,
        capabilities: list[Capability],
    ) -> SubmitVideoSourceResponse:
        """Create/reuse the video source + job and enqueue analysis.

        Requires ``analysis.submit`` capability (capability_denied otherwise).
        Idempotent on ``(camera_id, video_start_time)`` for the source and on the
        job idempotency key.
        """
        if Capability.ANALYSIS_SUBMIT not in capabilities:
            raise CapabilityDeniedError("analysis.submit required")

        video_id = _new_id("video")
        source = self._video_sources.create_or_get_by_idempotency(request, video_id=video_id)

        idempotency_key = request.idempotency_key or request.external_source_id or source.video_id

        existing_job = self._jobs.get_by_idempotency_key(idempotency_key)
        if existing_job is not None:
            return SubmitVideoSourceResponse(
                video_id=source.video_id,
                source_status=source.source_status,
                analysis_job_id=existing_job.analysis_job_id,
                accepted=True,
            )

        options = request.analysis_options or {"enable_default_segment": True}
        job = AnalysisJob(
            analysis_job_id=_new_id("job"),
            video_id=source.video_id,
            job_status=JobStatus.QUEUED,
            idempotency_key=idempotency_key,
            analysis_options=options,
            model_version=self._model_version,
            prompt_version=self._prompt_version,
            pipeline_version=self._pipeline_version,
        )
        job = self._jobs.create_job(job)

        # default_segment scale task (the MVP semantic path) — always created.
        scale_task = AnalysisScaleTask(
            scale_task_id=_new_id("scale"),
            analysis_job_id=job.analysis_job_id,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            status=TaskStatus.PENDING,
        )
        self._scale_tasks.create_scale_task(scale_task)

        # Opt-in motion-triggered high-frequency path (schema-contracts §3.4
        # analysis_options.enable_motion_triggered_high_freq). When enabled we add
        # a motion_scan task (produces HighFreqTriggers, no records) and a
        # high_freq_event task (produces event records around triggers). The
        # single analyze_video queue task drives all of a job's scale tasks.
        if options.get("enable_motion_triggered_high_freq"):
            self._scale_tasks.create_scale_task(
                AnalysisScaleTask(
                    scale_task_id=_new_id("scale"),
                    analysis_job_id=job.analysis_job_id,
                    analysis_scale=AnalysisScale.MOTION_SCAN,
                    status=TaskStatus.PENDING,
                )
            )
            self._scale_tasks.create_scale_task(
                AnalysisScaleTask(
                    scale_task_id=_new_id("scale"),
                    analysis_job_id=job.analysis_job_id,
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    status=TaskStatus.PENDING,
                )
            )

        # Enqueue a queue task carrying the analyze-video payload.
        task = Task(
            task_id=_new_id("task"),
            task_type=TASK_TYPE_ANALYZE_VIDEO,
            payload={
                "analysis_job_id": job.analysis_job_id,
                "video_id": source.video_id,
                "scale_task_id": scale_task.scale_task_id,
            },
            status="queued",
            next_run_at=datetime.now(UTC),
        )
        self._task_queue.enqueue_task(task)

        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type="analysis_job_created",
                principal_id=principal.principal_id,
                video_id=source.video_id,
                camera_id=source.camera_id,
                metadata={"analysis_job_id": job.analysis_job_id},
            )
        )

        return SubmitVideoSourceResponse(
            video_id=source.video_id,
            source_status=source.source_status,
            analysis_job_id=job.analysis_job_id,
            accepted=True,
        )
