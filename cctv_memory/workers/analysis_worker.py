"""Analysis worker (workers/analysis_worker.py).

Claims a queued task, processes ALL of the job's analysis scale tasks
(default_segment, and optionally motion_scan -> high_freq_event), advances
job/scale state through the orchestrator (validated transitions), and marks the
queue task succeeded/failed. Designed for embedded (in-process) or standalone
single-shot use (``process_one``).

Scale ordering (job-state-machine-contract): ``default_segment`` is the required
semantic baseline; ``motion_scan`` produces HighFreqTriggers (no records) and
MUST run before ``high_freq_event`` (which consumes those triggers). A failure of
the REQUIRED default_segment scale fails the whole job (legal running->failed). A
partial failure of default_segment (some units fail, some publish) yields
``partial_failed`` on that scale and on the job. A failure of an OPTIONAL scale
(motion_scan/high_freq_event) downgrades the job to ``partial_failed`` while
keeping the published default_segment records (job-state-machine-contract §1.3).

State-commit ordering: the RUNNING job transition is committed in its OWN session
*before* processing begins so a processing failure yields a legal
``running -> failed`` (not ``queued -> failed``).

The worker receives a Runtime (composition root) and constructs per-session
services. It never bypasses repositories or the publication path.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from cctv_memory.application.analysis_orchestrator import AnalysisOrchestrator
from cctv_memory.application.publication import PublicationService
from cctv_memory.contracts.analysis import AnalysisScaleTask, AnalysisUnit
from cctv_memory.contracts.pre_vlm_gate import GateProfile, GateRule, PreVlmGateLog
from cctv_memory.contracts.task import Task
from cctv_memory.domain.enums import AnalysisScale, JobStatus, TaskStatus
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.infrastructure.object_detection.google_vision_adapter import (
    GoogleVisionObjectDetectionAdapter,
)
from cctv_memory.infrastructure.object_detection.mock_adapter import MockObjectDetectionAdapter
from cctv_memory.infrastructure.pre_vlm_gate.gate_runner import PreVlmGateRunner
from cctv_memory.infrastructure.video.ffprobe_adapter import (
    FfprobeVideoProcessor,
    SegmentFrameVideoProcessor,
    StaticVideoProcessor,
    WholeClipVideoProcessor,
)
from cctv_memory.infrastructure.video.mock_detector_gate import MockDetectorGate
from cctv_memory.infrastructure.video.motion_detector_factory import (
    build_motion_detector,
)
from cctv_memory.infrastructure.video.opencv_frame_stream import (
    OpenCvFrameStreamVideoProcessor,
)
from cctv_memory.infrastructure.video.opencv_import import (
    OpenCvImportError,
    warmup_opencv,
)
from cctv_memory.infrastructure.vlm.mock_adapter import MockVlmAnalyzer
from cctv_memory.infrastructure.vlm.real_adapter import RealVlmAnalyzer
from cctv_memory.services.detector_gate import DetectorGatePort
from cctv_memory.services.motion_detector import MotionDetectorPort
from cctv_memory.services.object_detection import ObjectDetectionPort
from cctv_memory.services.pre_vlm_gate import PreVlmGatePort
from cctv_memory.services.timeline_recorder import TimelineRecorder
from cctv_memory.services.video_processor import VideoProcessorPort
from cctv_memory.services.vlm_analyzer import VlmAnalyzerPort
from cctv_memory.workers.cross_scale_scheduler import (
    CrossScaleUnitScheduler,
    PlannedUnit,
)
from cctv_memory.workers.default_segment import DefaultSegmentProcessor
from cctv_memory.workers.high_freq_event import HighFreqEventProcessor
from cctv_memory.workers.lease_heartbeat import LeaseHeartbeat
from cctv_memory.workers.motion_scan import MotionScanProcessor
from cctv_memory.workers.retry import RetryPolicy, run_db_write_with_retry
from cctv_memory.workers.unit_result import ScaleProcessResult
from cctv_memory.workers.vlm_scheduler import VlmScheduler

# Scale processing order: default_segment first (required baseline), then
# motion_scan (produces triggers), then high_freq_event (consumes triggers).
_SCALE_ORDER: dict[AnalysisScale, int] = {
    AnalysisScale.DEFAULT_SEGMENT: 0,
    AnalysisScale.MOTION_SCAN: 1,
    AnalysisScale.HIGH_FREQ_EVENT: 2,
    AnalysisScale.LOW_FREQ_SUMMARY: 3,
}

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_video_processor(runtime: Runtime) -> VideoProcessorPort:
    cfg = runtime.config
    if cfg.pipeline.video_metadata_mode == "static":
        return StaticVideoProcessor(
            duration_ms=cfg.pipeline.static_duration_ms,
            frame_root=cfg.storage.frame_root,
        )
    if cfg.pipeline.video_metadata_mode == "ffmpeg_frames":
        return SegmentFrameVideoProcessor(
            frame_root=cfg.storage.frame_root,
            frame_strategy=cfg.pipeline.default_segment.frame_strategy,
        )
    if cfg.vlm.provider == "real":
        if cfg.vlm.media_input == "video":
            return WholeClipVideoProcessor(
                frame_root=cfg.storage.frame_root,
                include_audio=cfg.vlm.include_audio,
            )
        return _frames_processor(runtime)
    return FfprobeVideoProcessor(frame_root=cfg.storage.frame_root)


def _frames_processor(runtime: Runtime) -> VideoProcessorPort:
    """Build the per-segment frame processor honoring ``pipeline.decode_backend``.

    ``opencv`` (default) -> OpenCvFrameStreamVideoProcessor (stream once, bounded
    ring buffer, metric selection). ``ffmpeg`` -> legacy SegmentFrameVideoProcessor.
    """
    cfg = runtime.config
    if cfg.pipeline.decode_backend == "ffmpeg":
        return SegmentFrameVideoProcessor(
            frame_root=cfg.storage.frame_root,
            frame_strategy=cfg.pipeline.default_segment.frame_strategy,
        )
    fs = cfg.pipeline.frame_stream
    return OpenCvFrameStreamVideoProcessor(
        frame_root=cfg.storage.frame_root,
        sample_fps=fs.sample_fps,
        buffer_seconds=fs.buffer_seconds,
        max_buffer_bytes=fs.max_buffer_bytes,
        scoring_scale=fs.scoring_scale,
        selection_strategy=fs.selection_strategy,
        selected_jpeg_quality=fs.selected_jpeg_quality,
        w_motion=fs.w_motion,
        w_scene=fs.w_scene,
        w_quality=fs.w_quality,
        min_blur=fs.min_blur,
        decode_fallback_to_ffmpeg=cfg.pipeline.decode_fallback_to_ffmpeg,
        frame_strategy=cfg.pipeline.default_segment.frame_strategy,
    )


def _default_vlm(runtime: Runtime) -> VlmAnalyzerPort:
    """Select the VLM analyzer from config. ``mock`` is the default for CI."""
    cfg = runtime.config
    if cfg.vlm.provider != "real":
        return MockVlmAnalyzer()
    api_key = os.environ.get(cfg.vlm.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"VLM provider=real but env var {cfg.vlm.api_key_env} is not set"
        )
    base_url = os.environ.get(cfg.vlm.base_url_env, cfg.vlm.default_base_url)
    return RealVlmAnalyzer(
        base_url=base_url,
        api_key=api_key,
        model_id=cfg.vlm.model_id,
        timeout_seconds=cfg.vlm.timeout_seconds,
        max_retries=cfg.vlm.max_retries,
        media_input=cfg.vlm.media_input,
        extra_body=cfg.vlm.extra_body,
    )


def _default_motion_detector(runtime: Runtime) -> MotionDetectorPort:
    return build_motion_detector(runtime.config.pipeline.motion_scan)


class AnalysisWorker:
    """Claim-and-process worker for analyze_video tasks."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        video_processor: VideoProcessorPort | None = None,
        vlm: VlmAnalyzerPort | None = None,
        motion_detector: MotionDetectorPort | None = None,
        detector_gate: DetectorGatePort | None = None,
        object_detection: ObjectDetectionPort | None = None,
        pre_vlm_gate: PreVlmGatePort | None = None,
    ) -> None:
        self._runtime = runtime
        self._video_processor = video_processor or _default_video_processor(runtime)
        self._vlm = vlm or _default_vlm(runtime)
        self._motion_detector = motion_detector
        self._detector_gate = detector_gate or self._default_detector_gate()
        self._object_detection = object_detection or self._default_object_detection()
        self._pre_vlm_gate = pre_vlm_gate or self._default_pre_vlm_gate()
        # cv2 cold-start race fix (task cctv-memory-20260617-1441): import + sanity-
        # warm OpenCV ONCE, single-threaded, HERE in __init__ — before any unit/job
        # ThreadPoolExecutor fans out. opencv-python's bootstrap() is not thread
        # safe (it pop/re-pushes sys.modules["cv2"]); concurrent first imports can
        # see a half-initialized module (``cv2.__spec__ is None``). Warming up now
        # means every later worker thread reuses the cached module and never races
        # the bootstrap. Only when the OpenCV decode backend is actually in effect;
        # a missing/broken OpenCV with ffmpeg fallback enabled degrades gracefully
        # (warmup returns False) instead of failing construction.
        self._warmup_decode_backend()
        # Stage C1: ONE shared VlmScheduler enforces provider concurrency + min
        # request interval GLOBALLY across every VLM unit/scale in this job/worker
        # (not per-processor). default_segment and high_freq_event both run their
        # VLM calls through this single limiter, so concurrent cross-scale work can
        # never exceed the configured provider cap or violate the global interval.
        self._vlm_scheduler = VlmScheduler(
            max_concurrent=max(1, int(runtime.config.vlm.max_concurrent_requests)),
            min_interval_ms=runtime.config.vlm.min_request_interval_ms,
        )
        # Stage C2: DB write-serialization is now owned by the database adapter
        # boundary (task cctv-memory-20260616-1850, Phase 3). The worker no longer
        # holds a SQLite-specific ``threading.Lock``; it routes every DB write
        # critical section through the runtime's backend WriteCoordinator (SQLite
        # serializes on one process-global writer; a future PG runtime would
        # no-op). Shared across both scales' units so cross-scale concurrent units
        # serialize on ONE writer. VLM calls stay outside it (§9.1).
        self._write_coordinator = runtime.write_coordinator
        self._timeline: TimelineRecorder = runtime.timeline_recorder()

    def _default_object_detection(self) -> ObjectDetectionPort | None:
        cfg = getattr(self._runtime.config.pipeline, "pre_vlm_gate", None)
        legacy = self._runtime.config.pipeline.detector_gate
        provider = getattr(cfg, "provider", legacy.provider if legacy.enabled else "mock")
        if provider in ("mock", "object_detection_mock"):
            source = getattr(cfg, "mock", None)
            return MockObjectDetectionAdapter(
                positive_labels=getattr(source, "positive_labels", legacy.mock_positive_labels),
                positive_frame_ratio=getattr(
                    source, "positive_frame_ratio", legacy.mock_positive_frame_ratio
                ),
                confidence=getattr(source, "confidence", legacy.mock_confidence),
                model_id=getattr(cfg, "model_id", legacy.model_id),
            )
        if provider == "google_vision":
            return GoogleVisionObjectDetectionAdapter(
                base_url=getattr(
                    cfg,
                    "google_vision_url",
                    "http://nginx:7070/api/google/v1/images:annotate",
                ),
                timeout_seconds=getattr(cfg, "timeout_seconds", 30.0),
                model_id=getattr(cfg, "model_id", "google-vision-object-localization"),
            )
        if legacy.enabled:
            return None
        return None

    def _default_pre_vlm_gate(self) -> PreVlmGatePort | None:
        if self._object_detection is None:
            return None
        cfg = getattr(self._runtime.config.pipeline, "pre_vlm_gate", None)

        def _write_log(log: PreVlmGateLog) -> PreVlmGateLog:
            with self._write_coordinator.write(), self._runtime.session() as session:
                return self._runtime.repositories(session).pre_vlm_gate_log().create_log(log)

        return PreVlmGateRunner(
            object_detection=self._object_detection,
            log_writer=_write_log,
            max_results=getattr(cfg, "max_results", 10),
            min_confidence=getattr(cfg, "min_confidence", None),
        )

    def _pre_vlm_gate_profile(self, scale: AnalysisScale) -> GateProfile | None:
        cfg = getattr(self._runtime.config.pipeline, "pre_vlm_gate", None)
        if cfg is None:
            return None
        if not getattr(cfg, "enabled", False):
            return None
        section_name = "default_segment" if scale is AnalysisScale.DEFAULT_SEGMENT else "high_freq_event"
        section = getattr(cfg, section_name, None)
        if section is None or not getattr(section, "enabled", False):
            return None
        default_policy = (
            "publish_gate_only_record"
            if scale is AnalysisScale.DEFAULT_SEGMENT
            else "skip_without_record"
        )
        return GateProfile(
            profile_name=getattr(section, "profile_name", section_name),
            enabled=True,
            analysis_scale=scale,
            suppression_policy=getattr(section, "suppression_policy", default_policy),
            provider=getattr(cfg, "provider", "mock"),
            model_id=getattr(cfg, "model_id", None),
            rules=[
                GateRule(
                    rule_id=getattr(r, "rule_id", None),
                    signal_type=getattr(r, "signal_type", "object_detection"),
                    label=r.label,
                    min_positive_frame_ratio=r.min_positive_frame_ratio,
                    min_confidence=r.min_confidence,
                    action=r.action,
                )
                for r in getattr(section, "rules", [])
            ],
            force_vlm_on_trigger_reasons=list(
                getattr(section, "force_vlm_on_trigger_reasons", [])
            ),
        )

    def _default_detector_gate(self) -> DetectorGatePort | None:
        cfg = self._runtime.config.pipeline.detector_gate
        if not cfg.enabled:
            return None
        if cfg.provider != "mock":
            raise ValueError("only detector_gate.provider='mock' is implemented")
        return MockDetectorGate(
            positive_labels=cfg.mock_positive_labels,
            positive_frame_ratio=cfg.mock_positive_frame_ratio,
            confidence=cfg.mock_confidence,
        )

    def _warmup_decode_backend(self) -> None:
        """Eager, single-threaded OpenCV import + sanity warmup before fan-out.

        Runs once at worker construction. Only when ``pipeline.decode_backend`` is
        ``opencv`` (the configured OpenCV streaming decode path). If OpenCV is
        unavailable/broken: when the ffmpeg fallback is enabled we log and continue
        (the decode adapter will fall back honestly); when fallback is DISABLED we
        raise so the misconfiguration fails fast and clearly here, rather than as N
        random per-unit ``frame_extraction_failed`` once threads fan out.
        """
        cfg = self._runtime.config
        if cfg.pipeline.decode_backend != "opencv":
            return
        fallback_enabled = cfg.pipeline.decode_fallback_to_ffmpeg
        try:
            ready = warmup_opencv(required=not fallback_enabled)
        except OpenCvImportError:
            logger.exception(
                "OpenCV decode backend selected but warmup failed and ffmpeg "
                "fallback is DISABLED; failing fast"
            )
            raise
        if not ready:
            logger.warning(
                "OpenCV decode backend selected but OpenCV is unavailable/broken; "
                "ffmpeg fallback will be used for frame extraction"
            )

    def _unit_retry_policy(self) -> RetryPolicy:
        """Build the unit-level transient VLM retry policy from config (task 1447)."""
        vlm = self._runtime.config.vlm
        return RetryPolicy(
            max_attempts=vlm.unit_max_attempts,
            backoff_base_ms=vlm.retry_backoff_base_ms,
            backoff_cap_ms=vlm.retry_backoff_cap_ms,
            jitter=vlm.retry_jitter,
        )

    def _db_write[T](self, write: Callable[[], T]) -> T:
        """Run a short worker lifecycle DB write with coordination + BUSY retry.

        The body MUST contain only DB work (state transitions, counters, queue
        terminal writes, recovery). VLM/frame extraction/probe work stays outside
        this helper. For SQLite the coordinator sets write-intent, so the engine
        starts the transaction with ``BEGIN IMMEDIATE``; bounded retry absorbs
        transient cross-process BUSY/locked errors.
        """
        cfg = self._runtime.config.worker
        return run_db_write_with_retry(
            write,
            max_attempts=cfg.db_write_max_attempts,
            backoff_ms=cfg.db_write_backoff_ms,
        )

    def _write_session[T](self, work: Callable[[Any], T]) -> T:
        """Open one coordinated write session and run bounded BUSY retry."""

        def _write() -> T:
            with self._write_coordinator.write(), self._runtime.session() as session:
                return work(self._runtime.repositories(session))

        return self._db_write(_write)

    def _per_job_unit_workers(self) -> int:
        """Per-job unit thread-pool size (task cctv-memory-20260615-1620).

        The per-job unit pool is controlled solely by
        ``worker.max_unit_workers_per_job``. The actual in-flight VLM call count is
        capped independently by the single shared VlmScheduler at
        ``vlm.max_concurrent_requests``, so ``max_concurrent_jobs x per-job pool``
        can never multiply the provider cap.
        """
        cfg = self._runtime.config
        return max(1, int(cfg.worker.max_unit_workers_per_job))

    def process_one(self) -> str | None:
        """Claim and process a single queued task. Returns the task id or None.

        While the claimed task is processed, a ``LeaseHeartbeat`` renews its queue
        lease (task §B4) so a long job under raised ``max_concurrent_jobs`` is not
        re-claimed by another worker mid-flight. The heartbeat is ownership-guarded
        (only the current lease_owner renews) and stops when processing returns.
        """
        cfg = self._runtime.config
        worker_id = cfg.worker.worker_id

        task = self._write_session(
            lambda repos: repos.task_queue().claim_task(
                worker_id, datetime.now(UTC), cfg.worker.lease_seconds
            )
        )
        if task is None:
            return None
        self._timeline.event(
            "task_claimed",
            analysis_job_id=str(task.payload.get("analysis_job_id", "")) or None,
            task_id=task.task_id,
            video_id=str(task.payload.get("video_id", "")) or None,
            status="running",
            metadata={"worker_id": worker_id, "lease_seconds": cfg.worker.lease_seconds},
        )

        with LeaseHeartbeat(
            task_id=task.task_id,
            worker_id=worker_id,
            lease_seconds=cfg.worker.lease_seconds,
            renew_seconds=cfg.worker.lease_renew_seconds,
            renew=self._renew_lease,
        ):
            return self._process_claimed_task(task)

    def _renew_lease(self, task_id: str, worker_id: str, lease_until: datetime) -> None:
        """Renew one task's lease in a short coordinated write (heartbeat callback)."""
        self._write_session(
            lambda repos: repos.task_queue().refresh_lease(
                task_id, worker_id, lease_until
            )
        )

    def _process_claimed_task(self, task: Task) -> str | None:
        """Process an already-claimed task (lease renewed by the caller's heartbeat)."""
        payload = task.payload
        analysis_job_id = str(payload["analysis_job_id"])
        video_id = str(payload["video_id"])

        def _mark_job_running(repos: Any) -> list[AnalysisScaleTask]:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            orchestrator.transition_job(analysis_job_id, JobStatus.RUNNING)
            return cast(list[AnalysisScaleTask], repos.scale_task().list_by_job(analysis_job_id))

        scale_tasks = self._write_session(_mark_job_running)
        self._timeline.event(
            "job_running",
            analysis_job_id=analysis_job_id,
            task_id=task.task_id,
            video_id=video_id,
            status=JobStatus.RUNNING.value,
        )
        scale_tasks.sort(key=lambda t: _SCALE_ORDER.get(t.analysis_scale, 99))

        use_cross_scale = self._runtime.config.pipeline.cross_scale.enabled
        if use_cross_scale:
            return self._process_cross_scale(
                task.task_id, analysis_job_id, video_id, scale_tasks
            )

        # Track overall job outcome:
        # required_failed  -> whole job fails (default_segment total failure)
        # any_partial      -> job is partial_failed
        required_failed: Exception | None = None
        any_partial = False

        for st in scale_tasks:
            try:
                result = self._process_scale_task(analysis_job_id, video_id, st)
                if result is not None and st.analysis_scale is AnalysisScale.DEFAULT_SEGMENT:
                    # Required baseline produced NO records (all units failed/
                    # skipped) -> whole job fails (consistent with cross-scale
                    # Phase 6). Per-unit isolation means earlier successes survive.
                    if result.total > 0 and result.succeeded == 0:
                        reason = (
                            "all default_segment units failed"
                            if result.failed
                            else "default_segment produced no usable frames"
                        )
                        required_failed = RuntimeError(reason)
                        break
                # A partial result (some units failed) counts as job-level partial.
                if result is not None and result.failed:
                    any_partial = True
            except Exception as exc:  # noqa: BLE001
                if st.analysis_scale is AnalysisScale.DEFAULT_SEGMENT:
                    required_failed = exc
                    break
                any_partial = True
                self._fail_scale_task(
                    st.scale_task_id,
                    exc,
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    analysis_scale=st.analysis_scale,
                )

        if required_failed is not None:
            self._handle_required_failure(
                task.task_id, analysis_job_id, required_failed, video_id=video_id
            )
            return task.task_id

        # Reconcile residual running units from DB truth before finalize (§B2).
        self._reconcile_running_units_for_job(analysis_job_id)

        final_status = JobStatus.PARTIAL_FAILED if any_partial else JobStatus.SUCCEEDED
        def _finalize_job(repos: Any) -> None:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            orchestrator.transition_job(analysis_job_id, final_status)
            repos.video_source().mark_status(video_id, "ready")
            repos.task_queue().mark_succeeded(task.task_id)

        self._write_session(_finalize_job)
        self._timeline.event(
            "job_finished",
            analysis_job_id=analysis_job_id,
            task_id=task.task_id,
            video_id=video_id,
            status=final_status.value,
        )
        self._timeline.event(
            "task_finished",
            analysis_job_id=analysis_job_id,
            task_id=task.task_id,
            video_id=video_id,
            status="succeeded",
        )
        return task.task_id

    def _process_cross_scale(  # type: ignore[no-untyped-def]
        self, task_id: str, analysis_job_id: str, video_id: str, scale_tasks
    ) -> str | None:
        """Stage C2 main path: run motion_scan first (hard dependency), then
        dispatch default_segment + high_freq_event units from ONE unified,
        priority-ordered, starvation-free queue. Each scale is finalized on its
        OWN units reaching terminal states (not a sequential block ending); the
        job outcome aggregates all scale outcomes.

        motion_scan -> high_freq_event dependency is preserved: high_freq units are
        only planned after motion_scan has produced triggers. default_segment and
        high_freq_event have no mutual data dependency, so cross-scale interleaving
        is correct; per-unit idempotency + immediate publication keep out-of-order
        completion safe.
        """
        by_scale = {st.analysis_scale: st for st in scale_tasks}
        any_partial = False

        # --- Phase 1: motion_scan first (produces triggers; no records) ---------
        motion_task = by_scale.get(AnalysisScale.MOTION_SCAN)
        if motion_task is not None:
            try:
                self._process_scale_task(analysis_job_id, video_id, motion_task)
            except Exception as exc:  # noqa: BLE001
                # Optional scale failure -> job partial_failed, default still runs.
                any_partial = True
                self._fail_scale_task(
                    motion_task.scale_task_id,
                    exc,
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    analysis_scale=AnalysisScale.MOTION_SCAN,
                )

        # --- Phase 2: skip/transition default + high_freq scale tasks -----------
        default_task = by_scale.get(AnalysisScale.DEFAULT_SEGMENT)
        high_freq_task = by_scale.get(AnalysisScale.HIGH_FREQ_EVENT)

        # high_freq is skipped (no records) when motion produced no triggers.
        high_freq_active = high_freq_task is not None and self._has_triggers(
            analysis_job_id, video_id
        )
        if high_freq_task is not None and not high_freq_active:
            self._skip_scale_task(
                high_freq_task.scale_task_id,
                "no_motion_trigger",
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
            )

        # Any other scales (e.g. low_freq_summary) -> skipped not_enabled.
        for st in scale_tasks:
            if st.analysis_scale not in (
                AnalysisScale.DEFAULT_SEGMENT,
                AnalysisScale.MOTION_SCAN,
                AnalysisScale.HIGH_FREQ_EVENT,
            ):
                self._skip_scale_task(
                    st.scale_task_id,
                    "not_enabled",
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    analysis_scale=st.analysis_scale,
                )

        # --- Phase 3: plan default + high_freq units (default is REQUIRED) ------
        default_units: list[PlannedUnit] = []
        high_freq_units: list[PlannedUnit] = []
        try:
            if default_task is not None:
                self._transition_running(default_task.scale_task_id)
                self._timeline.event(
                    "scale_running",
                    analysis_job_id=analysis_job_id,
                    scale_task_id=default_task.scale_task_id,
                    video_id=video_id,
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    status=TaskStatus.RUNNING.value,
                )
                default_units = self._plan_default_units(
                    analysis_job_id, video_id, default_task.scale_task_id
                )
        except Exception as exc:  # noqa: BLE001
            # Required scale planning/probe failure fails the whole job.
            self._handle_required_failure(task_id, analysis_job_id, exc, video_id=video_id)
            return task_id

        if high_freq_active and high_freq_task is not None:
            try:
                self._transition_running(high_freq_task.scale_task_id)
                self._timeline.event(
                    "scale_running",
                    analysis_job_id=analysis_job_id,
                    scale_task_id=high_freq_task.scale_task_id,
                    video_id=video_id,
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    status=TaskStatus.RUNNING.value,
                )
                high_freq_units = self._plan_high_freq_units(
                    analysis_job_id, video_id, high_freq_task.scale_task_id
                )
            except Exception as exc:  # noqa: BLE001
                any_partial = True
                self._fail_scale_task(
                    high_freq_task.scale_task_id,
                    exc,
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                )
                high_freq_active = False
                high_freq_units = []

        # --- Phase 4: unified cross-scale dispatch ------------------------------
        cfg = self._runtime.config
        scheduler = CrossScaleUnitScheduler(
            max_workers=self._per_job_unit_workers(),
            high_freq_quota=cfg.pipeline.cross_scale.high_freq_quota,
        )
        results = scheduler.run(
            high_freq_units=high_freq_units, default_units=default_units
        )

        # --- Phase 4.5: reconcile THIS job's residual running units (DB truth) --
        # The scheduler returns once every dispatched unit is terminal in memory
        # (its `_safe_run` converts any escape to a FAILED tally). But if a unit's
        # OWN terminal DB write failed (e.g. exhausted retry) it could still be
        # `running` in the DB while counted FAILED in the tally — a tally/DB
        # divergence (task §B2). The thread pool has joined, so no legitimate unit
        # of this job is still in flight: any remaining `running` unit of THIS job
        # is residual and is reconciled to FAILED from DB truth BEFORE finalize, so
        # finalize never finalizes a job that still has `running` rows and never
        # depends solely on the in-memory tally.
        self._reconcile_running_units_for_job(analysis_job_id)

        # --- Phase 5: finalize EACH scale on its own units' terminal counts -----
        def _finalize_scales(repos: Any) -> None:
            if default_task is not None:
                ds = results[AnalysisScale.DEFAULT_SEGMENT]
                self._finalize_scale_task(
                    repos,
                    default_task.scale_task_id,
                    total=ds.total,
                    succeeded=ds.succeeded,
                    failed=ds.failed,
                    skipped=ds.skipped,
                )
            if high_freq_active and high_freq_task is not None:
                hf = results[AnalysisScale.HIGH_FREQ_EVENT]
                self._finalize_scale_task(
                    repos,
                    high_freq_task.scale_task_id,
                    total=hf.total,
                    succeeded=hf.succeeded,
                    failed=hf.failed,
                    skipped=hf.skipped,
                )

        self._write_session(_finalize_scales)
        if default_task is not None:
            ds = results[AnalysisScale.DEFAULT_SEGMENT]
            self._record_scale_finished(
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                scale_task_id=default_task.scale_task_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                result=ds,
            )
        if high_freq_active and high_freq_task is not None:
            hf = results[AnalysisScale.HIGH_FREQ_EVENT]
            self._record_scale_finished(
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                scale_task_id=high_freq_task.scale_task_id,
                analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                result=hf,
            )

        # --- Phase 6: job outcome -----------------------------------------------
        ds_result = results[AnalysisScale.DEFAULT_SEGMENT]
        # Required default_segment: if it planned units but produced NO records at
        # all (every unit failed and/or skipped) the baseline yielded nothing ->
        # job FAILED. A near-EOF skip ALONGSIDE at least one success is NOT failure
        # (that path is partial_failed/succeeded below).
        if (
            default_task is not None
            and ds_result.total > 0
            and ds_result.succeeded == 0
        ):
            reason = (
                "all default_segment units failed"
                if ds_result.failed
                else "default_segment produced no usable frames"
            )
            self._handle_required_failure(
                task_id, analysis_job_id, RuntimeError(reason), video_id=video_id
            )
            return task_id
        if ds_result.failed:
            any_partial = True
        if high_freq_active and results[AnalysisScale.HIGH_FREQ_EVENT].failed:
            any_partial = True

        final_status = JobStatus.PARTIAL_FAILED if any_partial else JobStatus.SUCCEEDED
        def _finalize_job(repos: Any) -> None:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            orchestrator.transition_job(analysis_job_id, final_status)
            repos.video_source().mark_status(video_id, "ready")
            repos.task_queue().mark_succeeded(task_id)

        self._write_session(_finalize_job)
        self._timeline.event(
            "job_finished",
            analysis_job_id=analysis_job_id,
            task_id=task_id,
            video_id=video_id,
            status=final_status.value,
        )
        self._timeline.event(
            "task_finished",
            analysis_job_id=analysis_job_id,
            task_id=task_id,
            video_id=video_id,
            status="succeeded",
        )
        return task_id

    def _transition_running(self, scale_task_id: str) -> None:
        def _transition(repos: Any) -> None:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            orchestrator.transition_scale_task(scale_task_id, TaskStatus.RUNNING)

        self._write_session(_transition)

    def _plan_default_units(
        self, analysis_job_id: str, video_id: str, scale_task_id: str
    ) -> list[PlannedUnit]:
        """Build the default_segment processor and plan its units (no VLM yet).

        The processor is built against a planning session; each PlannedUnit.run
        opens its OWN fresh session (the existing concurrent discipline), so the
        planning session can close immediately after planning.
        """
        with self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            processor = self._build_default_processor(repos, analysis_job_id, scale_task_id)
            units = processor.plan_units(analysis_job_id, video_id)
        return units

    def _plan_high_freq_units(
        self, analysis_job_id: str, video_id: str, scale_task_id: str
    ) -> list[PlannedUnit]:
        with self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            processor = self._build_high_freq_processor(
                repos, analysis_job_id, scale_task_id
            )
            units = processor.plan_units(analysis_job_id, video_id)
        return units

    def _build_default_processor(  # type: ignore[no-untyped-def]
        self, repos, analysis_job_id: str, scale_task_id: str
    ) -> DefaultSegmentProcessor:
        cfg = self._runtime.config
        return DefaultSegmentProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            video_processor=self._video_processor,
            vlm=self._vlm,
            timeline=self._timeline,
            detector_gate=self._detector_gate,
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scale_task_id=scale_task_id,
            provider=cfg.vlm.provider,
            model_id=cfg.vlm.model_id if cfg.vlm.provider == "real" else "mock-vlm-v1",
            pipeline_version=self._pipeline_version(analysis_job_id),
            window_seconds=cfg.pipeline.default_segment.window_seconds,
            overlap_seconds=cfg.pipeline.default_segment.overlap_seconds,
            frames_per_segment=cfg.pipeline.default_segment.frames_per_segment,
            max_concurrent_requests=self._per_job_unit_workers(),
            min_request_interval_ms=cfg.vlm.min_request_interval_ms,
            debug_media_retention=cfg.vlm.debug_media_retention,
            artifact_root=cfg.storage.artifact_root,
            cleanup_selected_on_success=cfg.pipeline.frame_stream.cleanup_selected_on_success,
            runtime=self._runtime,
            scheduler=self._vlm_scheduler,
            write_coordinator=self._write_coordinator,
            retry_policy=self._unit_retry_policy(),
            terminal_write_max_attempts=cfg.vlm.terminal_write_max_attempts,
            terminal_write_backoff_ms=cfg.vlm.terminal_write_backoff_ms,
            provider_options=cfg.vlm.extra_body,
            detector_gate_enabled=cfg.pipeline.detector_gate.enabled,
            detector_gate_provider=cfg.pipeline.detector_gate.provider,
            detector_gate_model_id=cfg.pipeline.detector_gate.model_id,
            detector_gate_rules=cfg.pipeline.detector_gate.rules,
            pre_vlm_gate=self._pre_vlm_gate,
            pre_vlm_gate_profile=self._pre_vlm_gate_profile(AnalysisScale.DEFAULT_SEGMENT),
        )

    def _build_high_freq_processor(  # type: ignore[no-untyped-def]
        self, repos, analysis_job_id: str, scale_task_id: str
    ) -> HighFreqEventProcessor:
        cfg = self._runtime.config
        return HighFreqEventProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            triggers=repos.trigger(),
            video_processor=self._video_processor,
            vlm=self._vlm,
            timeline=self._timeline,
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scale_task_id=scale_task_id,
            provider=cfg.vlm.provider,
            model_id=cfg.vlm.model_id if cfg.vlm.provider == "real" else "mock-vlm-v1",
            pipeline_version=self._pipeline_version(analysis_job_id),
            window_seconds=cfg.pipeline.high_freq_event.window_seconds,
            overlap_ratio=cfg.pipeline.high_freq_event.overlap_ratio,
            frames_per_segment=cfg.pipeline.high_freq_event.frames_per_segment,
            max_concurrent_requests=self._per_job_unit_workers(),
            min_request_interval_ms=cfg.vlm.min_request_interval_ms,
            debug_media_retention=cfg.vlm.debug_media_retention,
            artifact_root=cfg.storage.artifact_root,
            cleanup_selected_on_success=(
                cfg.pipeline.frame_stream.cleanup_selected_on_success
            ),
            runtime=self._runtime,
            scheduler=self._vlm_scheduler,
            write_coordinator=self._write_coordinator,
            retry_policy=self._unit_retry_policy(),
            terminal_write_max_attempts=cfg.vlm.terminal_write_max_attempts,
            terminal_write_backoff_ms=cfg.vlm.terminal_write_backoff_ms,
            provider_options=cfg.vlm.extra_body,
            pre_vlm_gate=self._pre_vlm_gate,
            pre_vlm_gate_profile=self._pre_vlm_gate_profile(AnalysisScale.HIGH_FREQ_EVENT),
        )

    def _process_scale_task(  # type: ignore[no-untyped-def]
        self, analysis_job_id: str, video_id: str, scale_task
    ) -> ScaleProcessResult | None:
        """Run one scale task end to end. Returns ScaleProcessResult or None (skipped)."""
        scale = scale_task.analysis_scale
        scale_task_id = scale_task.scale_task_id

        if scale not in (
            AnalysisScale.DEFAULT_SEGMENT,
            AnalysisScale.MOTION_SCAN,
            AnalysisScale.HIGH_FREQ_EVENT,
        ):
            self._skip_scale_task(
                scale_task_id,
                "not_enabled",
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                analysis_scale=scale,
            )
            return None
        if scale is AnalysisScale.HIGH_FREQ_EVENT and not self._has_triggers(
            analysis_job_id, video_id
        ):
            self._skip_scale_task(
                scale_task_id,
                "no_motion_trigger",
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                analysis_scale=scale,
            )
            return None

        self._transition_running(scale_task_id)
        self._timeline.event(
            "scale_running",
            analysis_job_id=analysis_job_id,
            scale_task_id=scale_task_id,
            video_id=video_id,
            analysis_scale=scale,
            status=TaskStatus.RUNNING.value,
        )

        with self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            if scale is AnalysisScale.HIGH_FREQ_EVENT:
                result = self._run_high_freq_event(
                    repos,
                    analysis_job_id,
                    video_id,
                    commit_before_concurrent=session.commit,
                )
            elif scale is AnalysisScale.MOTION_SCAN:
                produced = self._run_motion_scan(repos, analysis_job_id, video_id, scale_task_id)
                result = ScaleProcessResult(total=produced, succeeded=produced)
            else:  # DEFAULT_SEGMENT
                result = self._run_default_segment(
                    repos,
                    analysis_job_id,
                    video_id,
                    scale_task_id,
                    commit_before_concurrent=session.commit,
                )
        self._write_session(
            lambda repos: self._finalize_scale_task(
                repos,
                scale_task_id,
                total=result.total,
                succeeded=result.succeeded,
                failed=result.failed,
                skipped=result.skipped,
            )
        )
        scale_status = (
            TaskStatus.PARTIAL_FAILED.value
            if result.failed and result.succeeded
            else TaskStatus.FAILED.value
            if result.failed
            else TaskStatus.SUCCEEDED.value
        )
        self._timeline.event(
            "scale_finished",
            analysis_job_id=analysis_job_id,
            scale_task_id=scale_task_id,
            video_id=video_id,
            analysis_scale=scale,
            status=scale_status,
            metadata={
                "total_units": result.total,
                "succeeded_units": result.succeeded,
                "failed_units": result.failed,
                "skipped_units": result.skipped,
            },
        )
        return result

    def _has_triggers(self, analysis_job_id: str, video_id: str) -> bool:
        with self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            triggers = repos.trigger().list_by_job(analysis_job_id)
        return any(t.video_id == video_id for t in triggers)

    def _record_scale_finished(
        self,
        *,
        analysis_job_id: str,
        video_id: str,
        scale_task_id: str,
        analysis_scale: AnalysisScale,
        result: ScaleProcessResult,
    ) -> None:
        scale_status = (
            TaskStatus.PARTIAL_FAILED.value
            if result.failed and result.succeeded
            else TaskStatus.FAILED.value
            if result.failed
            else TaskStatus.SUCCEEDED.value
        )
        self._timeline.event(
            "scale_finished",
            analysis_job_id=analysis_job_id,
            scale_task_id=scale_task_id,
            video_id=video_id,
            analysis_scale=analysis_scale,
            status=scale_status,
            metadata={
                "total_units": result.total,
                "succeeded_units": result.succeeded,
                "failed_units": result.failed,
                "skipped_units": result.skipped,
            },
        )

    def _skip_scale_task(
        self,
        scale_task_id: str,
        reason: str,
        *,
        analysis_job_id: str | None = None,
        video_id: str | None = None,
        analysis_scale: AnalysisScale | None = None,
    ) -> None:
        def _skip(repos: Any) -> None:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            orchestrator.transition_scale_task(
                scale_task_id, TaskStatus.SKIPPED, skipped_reason=reason
            )

        self._write_session(_skip)
        self._timeline.event(
            "scale_skipped",
            analysis_job_id=analysis_job_id,
            scale_task_id=scale_task_id,
            video_id=video_id,
            analysis_scale=analysis_scale,
            status=TaskStatus.SKIPPED.value,
            metadata={"reason": reason},
        )

    @staticmethod
    def _finalize_scale_task(  # type: ignore[no-untyped-def]
        repos,
        scale_task_id: str,
        *,
        total: int,
        succeeded: int,
        failed: int = 0,
        skipped: int = 0,
    ) -> None:
        """Finalize a scale task from its units' terminal counts.

        ``skipped`` units (e.g. near-EOF insufficient_frames) are benign: they are
        neither successes nor failures, so they never force partial_failed/failed.
        A RUNNING scale must reach SUCCEEDED/PARTIAL_FAILED/FAILED (state machine),
        so an all-skipped scale becomes SUCCEEDED (it ran, produced no records).
        Counters persist succeeded/failed; skipped is reflected in ``total``.
        """
        orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
        repos.scale_task().update_counters(
            scale_task_id, total=total, succeeded=succeeded, failed=failed
        )
        if failed and succeeded:
            orchestrator.transition_scale_task(scale_task_id, TaskStatus.PARTIAL_FAILED)
        elif failed:
            orchestrator.transition_scale_task(scale_task_id, TaskStatus.FAILED)
        else:
            orchestrator.transition_scale_task(scale_task_id, TaskStatus.SUCCEEDED)

    def _reconcile_running_units_for_job(self, analysis_job_id: str) -> None:
        """Terminalize THIS job's residual ``running`` units before finalize (§B2).

        Called after the unit scheduler has joined (so no legitimate unit of this
        job is still in flight). Any unit still ``running`` here is residual — its
        own terminal write failed and the lifecycle guard's best-effort write also
        could not persist (tally/DB divergence). We mark each as FAILED
        (``analysis_unit_failed``) from DB truth, scoped to THIS job's scale tasks
        only, so finalize sees a consistent DB and other jobs' in-flight units are
        never touched. DB writes go through the backend write coordinator. Bounded
        to this job's units; idempotent (no residual => no-op).
        """
        def _reconcile(repos: Any) -> None:
            scale_tasks = repos.scale_task().list_by_job(analysis_job_id)
            for st in scale_tasks:
                units = repos.analysis_unit().list_by_scale_task(st.scale_task_id)
                for unit in units:
                    if unit.status is TaskStatus.RUNNING:
                        repos.analysis_unit().mark_failed(
                            unit.unit_id,
                            error_code="analysis_unit_failed",
                            error_message=(
                                "residual running reconciled at job finalize "
                                "(terminal write did not persist)"
                            ),
                            model_call_id=unit.latest_model_call_id,
                        )

        self._write_session(_reconcile)

    def _pipeline_version(self, analysis_job_id: str) -> str:
        """Authoritative pipeline_version from the job row (set at ingestion from
        the effective decode backend). Used for ModelCallLog so it never drifts
        from the ObservationRecord's pipeline_version (both come from the job)."""
        with self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            job = repos.analysis_job().get_job(analysis_job_id)
        if job is not None and job.pipeline_version is not None:
            return job.pipeline_version
        return "pipeline-v1"

    def _run_default_segment(  # type: ignore[no-untyped-def]
        self,
        repos,
        analysis_job_id: str,
        video_id: str,
        scale_task_id: str,
        *,
        commit_before_concurrent=None,
    ) -> ScaleProcessResult:
        cfg = self._runtime.config
        processor = DefaultSegmentProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            video_processor=self._video_processor,
            vlm=self._vlm,
            timeline=self._timeline,
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scale_task_id=scale_task_id,
            provider=cfg.vlm.provider,
            model_id=cfg.vlm.model_id if cfg.vlm.provider == "real" else "mock-vlm-v1",
            pipeline_version=self._pipeline_version(analysis_job_id),
            window_seconds=cfg.pipeline.default_segment.window_seconds,
            overlap_seconds=cfg.pipeline.default_segment.overlap_seconds,
            frames_per_segment=cfg.pipeline.default_segment.frames_per_segment,
            max_concurrent_requests=self._per_job_unit_workers(),
            min_request_interval_ms=cfg.vlm.min_request_interval_ms,
            debug_media_retention=cfg.vlm.debug_media_retention,
            artifact_root=cfg.storage.artifact_root,
            cleanup_selected_on_success=cfg.pipeline.frame_stream.cleanup_selected_on_success,
            runtime=self._runtime,
            commit_before_concurrent=commit_before_concurrent,
            scheduler=self._vlm_scheduler,
            write_coordinator=self._write_coordinator,
            retry_policy=self._unit_retry_policy(),
            terminal_write_max_attempts=cfg.vlm.terminal_write_max_attempts,
            terminal_write_backoff_ms=cfg.vlm.terminal_write_backoff_ms,
            provider_options=cfg.vlm.extra_body,
            detector_gate_enabled=cfg.pipeline.detector_gate.enabled,
            detector_gate_provider=cfg.pipeline.detector_gate.provider,
            detector_gate_model_id=cfg.pipeline.detector_gate.model_id,
            detector_gate_rules=cfg.pipeline.detector_gate.rules,
            pre_vlm_gate=self._pre_vlm_gate,
            pre_vlm_gate_profile=self._pre_vlm_gate_profile(AnalysisScale.DEFAULT_SEGMENT),
        )
        return processor.process(analysis_job_id, video_id)

    def _run_motion_scan(  # type: ignore[no-untyped-def]
        self, repos, analysis_job_id: str, video_id: str, scale_task_id: str
    ) -> int:
        cfg = self._runtime.config.pipeline.motion_scan
        if self._motion_detector is None:
            self._motion_detector = _default_motion_detector(self._runtime)
        high_freq_task = repos.scale_task().get_by_job_and_scale(
            analysis_job_id, AnalysisScale.HIGH_FREQ_EVENT.value
        )
        high_freq_task_id = (
            high_freq_task.scale_task_id if high_freq_task is not None else scale_task_id
        )
        processor = MotionScanProcessor(
            video_sources=repos.video_source(),
            triggers=repos.trigger(),
            motion_detector=self._motion_detector,
            high_freq_scale_task_id=high_freq_task_id,
            threshold=cfg.threshold,
            min_duration_ms=cfg.min_duration_ms,
            merge_gap_ms=cfg.merge_gap_ms,
        )
        return processor.process(analysis_job_id, video_id)

    def _run_high_freq_event(  # type: ignore[no-untyped-def]
        self, repos, analysis_job_id: str, video_id: str, *, commit_before_concurrent=None
    ) -> ScaleProcessResult:
        cfg = self._runtime.config.pipeline.high_freq_event
        processor = HighFreqEventProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            triggers=repos.trigger(),
            video_processor=self._video_processor,
            vlm=self._vlm,
            timeline=self._timeline,
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            provider=self._runtime.config.vlm.provider,
            model_id=(
                self._runtime.config.vlm.model_id
                if self._runtime.config.vlm.provider == "real"
                else "mock-vlm-v1"
            ),
            pipeline_version=self._pipeline_version(analysis_job_id),
            window_seconds=cfg.window_seconds,
            overlap_ratio=cfg.overlap_ratio,
            frames_per_segment=cfg.frames_per_segment,
            max_concurrent_requests=self._per_job_unit_workers(),
            min_request_interval_ms=self._runtime.config.vlm.min_request_interval_ms,
            debug_media_retention=self._runtime.config.vlm.debug_media_retention,
            artifact_root=self._runtime.config.storage.artifact_root,
            cleanup_selected_on_success=(
                self._runtime.config.pipeline.frame_stream.cleanup_selected_on_success
            ),
            runtime=self._runtime,
            commit_before_concurrent=commit_before_concurrent,
            scheduler=self._vlm_scheduler,
            write_coordinator=self._write_coordinator,
            retry_policy=self._unit_retry_policy(),
            terminal_write_max_attempts=self._runtime.config.vlm.terminal_write_max_attempts,
            terminal_write_backoff_ms=self._runtime.config.vlm.terminal_write_backoff_ms,
            pre_vlm_gate=self._pre_vlm_gate,
            pre_vlm_gate_profile=self._pre_vlm_gate_profile(AnalysisScale.HIGH_FREQ_EVENT),
        )
        return processor.process(analysis_job_id, video_id)

    def _fail_scale_task(
        self,
        scale_task_id: str,
        exc: Exception,
        *,
        analysis_job_id: str | None = None,
        video_id: str | None = None,
        analysis_scale: AnalysisScale | None = None,
    ) -> None:
        error_code = self._classify_error(exc)
        def _fail(repos: Any) -> None:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            orchestrator.transition_scale_task(
                scale_task_id, TaskStatus.FAILED, error_code=error_code
            )

        self._write_session(_fail)
        self._timeline.event(
            "scale_failed",
            analysis_job_id=analysis_job_id,
            scale_task_id=scale_task_id,
            video_id=video_id,
            analysis_scale=analysis_scale,
            status=TaskStatus.FAILED.value,
            error_code=error_code,
            error_message=exc,
        )

    def _handle_required_failure(
        self,
        task_id: str,
        analysis_job_id: str,
        exc: Exception,
        *,
        video_id: str | None = None,
    ) -> None:
        error_code = self._classify_error(exc)
        message = f"{type(exc).__name__}: {str(exc)[:200]}"
        def _handle(repos: Any) -> str | None:
            orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
            default_task = repos.scale_task().get_by_job_and_scale(
                analysis_job_id, AnalysisScale.DEFAULT_SEGMENT.value
            )
            failed_scale_task_id: str | None = None
            if default_task is not None and default_task.status is TaskStatus.RUNNING:
                orchestrator.transition_scale_task(
                    default_task.scale_task_id, TaskStatus.FAILED, error_code=error_code
                )
                failed_scale_task_id = default_task.scale_task_id
            orchestrator.transition_job(
                analysis_job_id,
                JobStatus.FAILED,
                error_code=error_code,
                error_message=message,
            )
            repos.task_queue().mark_failed(task_id, error_code, message)
            return failed_scale_task_id

        failed_scale_task_id = self._write_session(_handle)
        if failed_scale_task_id is not None:
            self._timeline.event(
                "scale_failed",
                analysis_job_id=analysis_job_id,
                task_id=task_id,
                scale_task_id=failed_scale_task_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                status=TaskStatus.FAILED.value,
                error_code=error_code,
                error_message=message,
            )
        self._timeline.event(
            "job_failed",
            analysis_job_id=analysis_job_id,
            task_id=task_id,
            status=JobStatus.FAILED.value,
            error_code=error_code,
            error_message=message,
        )
        self._timeline.event(
            "task_finished",
            analysis_job_id=analysis_job_id,
            task_id=task_id,
            status="failed",
            error_code=error_code,
            error_message=message,
        )

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        from cctv_memory.domain.exceptions import VlmSchemaValidationError
        from cctv_memory.infrastructure.vlm.real_adapter import VlmProviderError

        if isinstance(exc, VlmSchemaValidationError):
            return "vlm_schema_validation_failed"
        if isinstance(exc, VlmProviderError):
            return "vlm_provider_error"
        if isinstance(exc, RuntimeError):
            return "video_decode_error"
        return "analysis_unit_failed"

    def drain(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Process queued tasks until the queue is drained; return count processed.

        Concurrency is controlled solely by ``worker.max_concurrent_jobs`` (the
        number of analysis jobs/videos handled AT ONCE). There is no batch-size
        cap: a drain pass keeps claiming until no claimable task remains.

        ``worker.max_concurrent_jobs == 1`` (default) keeps the strictly-serial
        behavior: claim+process one job at a time. ``> 1`` runs an in-process
        thread pool of exactly ``max_concurrent_jobs`` slots where each slot
        independently claims and processes a job; the atomic ``claim_task``
        guarantees no task is processed twice, and the shared VlmScheduler keeps
        the global provider-call cap (``vlm.max_concurrent_requests``). One job's
        failure is isolated (``process_one`` terminalizes its own job state and any
        unexpected escape is contained) so it never blocks the other slots.

        ``should_stop`` (optional) enables graceful shutdown: when it returns True
        no NEW job is claimed; in-flight jobs are allowed to finish. The crash/kill
        window remains covered by bounded orphan recovery.

        Orphan recovery (task §B3, job-state-machine-contract §7.1): a bounded,
        index-backed, stale-cutoff sweep runs BEFORE each drain pass (in addition
        to the startup pass), so units stranded ``running`` by a prior crash/kill
        are reconciled promptly instead of only at process startup. It is a no-op
        on a clean database.
        """
        if should_stop is None or not should_stop():
            try:
                self.recover_orphans()
            except Exception:  # noqa: BLE001 - recovery must never break draining
                logger.exception("orphan recovery before drain failed; continuing")
        max_jobs = max(1, int(self._runtime.config.worker.max_concurrent_jobs))
        if max_jobs == 1:
            count = 0
            while True:
                if should_stop is not None and should_stop():
                    break
                task_id = self.process_one()
                if task_id is None:
                    break
                count += 1
            return count
        return self._drain_concurrent(max_jobs=max_jobs, should_stop=should_stop)

    def _drain_concurrent(
        self,
        *,
        max_jobs: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Concurrent job pool: exactly ``max_jobs`` slots process jobs at once.

        Each slot keeps claiming+processing until the queue is drained.
        ``_process_one_safe`` contains any unexpected escape so a single rogue job
        cannot strand the pool; the job's own DB state is terminalized by
        ``process_one``/orphan recovery, never left silently running. When
        ``should_stop`` flips True, workers stop claiming NEW jobs; the
        ThreadPoolExecutor context still lets in-flight jobs finish.
        """
        count_lock = threading.Lock()
        processed = 0
        n_workers = max_jobs

        def _worker() -> int:
            nonlocal processed
            local_done = 0
            while True:
                if should_stop is not None and should_stop():
                    break
                task_id = self._process_one_safe()
                if task_id is None:
                    # No claimable task right now; drain pass is complete.
                    break
                with count_lock:
                    processed += 1
                local_done += 1
            return local_done

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = [executor.submit(_worker) for _ in range(n_workers)]
            for future in as_completed(futures):
                future.result()  # _worker never raises (process is contained)
        return processed

    def _process_one_safe(self) -> str | None:
        """``process_one`` with a last-resort guard so a pool worker never dies.

        ``process_one`` already terminalizes job/scale state for handled failures.
        This guard only prevents an unforeseen escape from killing the worker
        thread; the affected job's units fall back to bounded orphan recovery
        rather than being silently lost. Returns the task id, or None when nothing
        was claimable / an unexpected error was contained.
        """
        try:
            return self.process_one()
        except Exception:  # noqa: BLE001 - last-resort pool guard; see docstring
            return None

    def recover_orphans(self) -> int:
        """Reconcile units stuck ``running`` past the stale cutoff (bounded sweep).

        Index-backed (``idx_units_status_started``), stale-cutoff, batch-limited:
        selects at most ``orphan_batch_limit`` units with ``status='running' AND
        started_at < (now - orphan_stale_seconds)`` and terminalizes each as
        ``failed(orphan_timeout)`` (never left running, no full-table scan, no new
        status). Only the parent scale tasks / jobs of the swept units are then
        reconciled (recompute counts from their units, finalize scale, finalize
        job). Returns the number of units recovered. Idempotent: a clean DB sweeps
        zero rows. Task cctv-memory-20260612-1854 §E.
        """
        cfg = self._runtime.config.worker
        if not cfg.orphan_recovery_enabled:
            return 0
        cutoff = datetime.now(UTC) - timedelta(seconds=cfg.orphan_stale_seconds)
        def _recover(repos: Any) -> list[AnalysisUnit]:
            stale = repos.analysis_unit().list_stale_running(
                cutoff=cutoff, limit=cfg.orphan_batch_limit
            )
            for unit in stale:
                repos.analysis_unit().mark_failed(
                    unit.unit_id,
                    error_code="orphan_timeout",
                    error_message="unit running past stale cutoff; recovered by sweep",
                )
            # Reconcile ONLY the parent scale tasks of the swept units.
            scale_task_ids = {u.scale_task_id for u in stale}
            for scale_task_id in scale_task_ids:
                self._reconcile_scale_task(repos, scale_task_id)
            # Reconcile ONLY the parent jobs of the affected scale tasks.
            job_ids = {u.analysis_job_id for u in stale}
            for job_id in job_ids:
                self._reconcile_job(repos, job_id)
            return cast(list[AnalysisUnit], stale)

        stale = self._write_session(_recover)
        return len(stale)

    @staticmethod
    def _reconcile_scale_task(repos, scale_task_id: str) -> None:  # type: ignore[no-untyped-def]
        """Recompute a scale task's counts from its units and finalize it.

        Only acts on a scale task still in RUNNING (a finalized scale is left
        untouched). Counts are derived from the unit rows, so a swept orphan unit
        (now failed) correctly drives partial_failed/failed/succeeded.
        """
        task = repos.scale_task().get_scale_task(scale_task_id)
        if task is None or task.status is not TaskStatus.RUNNING:
            return
        units = repos.analysis_unit().list_by_scale_task(scale_task_id)
        # If any unit is still non-terminal, the scale is genuinely in progress;
        # do not finalize it (the owning worker will).
        if any(u.status is TaskStatus.RUNNING or u.status is TaskStatus.PENDING for u in units):
            return
        succeeded = sum(1 for u in units if u.status is TaskStatus.SUCCEEDED)
        failed = sum(1 for u in units if u.status is TaskStatus.FAILED)
        skipped = sum(1 for u in units if u.status is TaskStatus.SKIPPED)
        AnalysisWorker._finalize_scale_task(
            repos,
            scale_task_id,
            total=len(units),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )

    @staticmethod
    def _reconcile_job(repos, analysis_job_id: str) -> None:  # type: ignore[no-untyped-def]
        """Finalize a RUNNING job once all its scale tasks are terminal.

        Mirrors the cross-scale Phase 6 rule: required default_segment with no
        successful unit -> FAILED; any failed scale -> PARTIAL_FAILED; else
        SUCCEEDED. A job with any still-RUNNING/PENDING scale is left alone.
        """
        job = repos.analysis_job().get_job(analysis_job_id)
        if job is None or job.job_status is not JobStatus.RUNNING:
            return
        scale_tasks = repos.scale_task().list_by_job(analysis_job_id)
        non_terminal = {TaskStatus.RUNNING, TaskStatus.PENDING}
        if any(st.status in non_terminal for st in scale_tasks):
            return
        orchestrator = AnalysisOrchestrator(repos.analysis_job(), repos.scale_task())
        default = next(
            (st for st in scale_tasks if st.analysis_scale is AnalysisScale.DEFAULT_SEGMENT),
            None,
        )
        if (
            default is not None
            and default.total_units > 0
            and default.succeeded_units == 0
        ):
            orchestrator.transition_job(
                analysis_job_id,
                JobStatus.FAILED,
                error_code="analysis_unit_failed",
                error_message="default_segment produced no records (orphan recovery)",
            )
            return
        any_failed = any(
            st.status in (TaskStatus.FAILED, TaskStatus.PARTIAL_FAILED)
            for st in scale_tasks
        )
        target = JobStatus.PARTIAL_FAILED if any_failed else JobStatus.SUCCEEDED
        orchestrator.transition_job(analysis_job_id, target)
