"""default_segment processing path — per-unit publication, model-call logging,
rate-limited/bounded-concurrent VLM calls, and debug media artifact retention.

Each window becomes an AnalysisUnit.  VLM calls go through ``VlmScheduler``
(concurrency cap + min interval).  Media refs are built by ``build_media_refs``
(metadata_only or debug_full_media depending on config).  Each successful unit
is published immediately via PublicationService.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cctv_memory.application.publication import PublicationService
from cctv_memory.config.settings import DetectorGateRuleSection
from cctv_memory.contracts.analysis import AnalysisUnit, DetectorGateLog, ModelCallLog
from cctv_memory.contracts.pre_vlm_gate import GateProfile
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain import policies
from cctv_memory.domain.enums import AnalysisScale, ModelCallStatus, TaskStatus
from cctv_memory.domain.exceptions import InsufficientFramesError, NotFoundError
from cctv_memory.repositories.analysis import (
    AnalysisJobRepository,
    AnalysisUnitRepository,
    ModelCallLogRepository,
)
from cctv_memory.repositories.camera import CameraRepository
from cctv_memory.repositories.principal import AccessPolicyRepository
from cctv_memory.repositories.video_source import VideoSourceRepository
from cctv_memory.services.detector_gate import DetectorGatePort
from cctv_memory.services.pre_vlm_gate import PreVlmGatePort
from cctv_memory.services.timeline_recorder import TimelineRecorder
from cctv_memory.services.video_processor import VideoProcessorPort
from cctv_memory.services.vlm_analyzer import VlmAnalyzerPort
from cctv_memory.services.write_coordinator import (
    NO_OP_WRITE_COORDINATOR,
    WriteCoordinator,
)
from cctv_memory.workers.common import (
    DEFAULT_POLICY_ID,
    build_detector_only_observation_record,
    build_observation_record,
    new_id,
    resolve_video_context,
    video_end_iso,
)
from cctv_memory.workers.cross_scale_scheduler import PlannedUnit
from cctv_memory.workers.debug_media import build_media_refs
from cctv_memory.workers.detector_gate import (
    build_detector_frame_inputs,
    decide_detector_gate,
)
from cctv_memory.workers.frame_selection import (
    cleanup_selected_frames,
    select_frames_for_unit,
)
from cctv_memory.workers.pre_vlm_gate import run_pre_vlm_gate
from cctv_memory.workers.retry import (
    RetryPolicy,
    VlmAttempt,
    execute_vlm_with_retry,
    run_db_write_with_retry,
    vlm_failure_error_code,
)
from cctv_memory.workers.unit_result import ScaleProcessResult, UnitOutcome, UnitPhase
from cctv_memory.workers.vlm_input_manifest import (
    attach_manifest_to_attempts,
    build_vlm_input_manifest,
)
from cctv_memory.workers.vlm_scheduler import VlmScheduler

if TYPE_CHECKING:
    from cctv_memory.infrastructure.runtime import Runtime
    from cctv_memory.workers.common import VideoContext


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class DefaultSegmentProcessor:
    """Process the default_segment scale — per-unit publication + model-call logs
    + rate-limited/bounded-concurrent VLM scheduling."""

    def __init__(
        self,
        *,
        video_sources: VideoSourceRepository,
        jobs: AnalysisJobRepository,
        cameras: CameraRepository,
        policies_repo: AccessPolicyRepository,
        video_processor: VideoProcessorPort,
        vlm: VlmAnalyzerPort,
        timeline: TimelineRecorder | None = None,
        detector_gate: DetectorGatePort | None = None,
        publication: PublicationService,
        units: AnalysisUnitRepository,
        model_calls: ModelCallLogRepository,
        scale_task_id: str,
        provider: str = "mock",
        model_id: str = "mock-vlm-v1",
        pipeline_version: str = "pipeline-v1",
        window_seconds: int = 12,
        overlap_seconds: int = 3,
        frames_per_segment: int = 6,
        max_concurrent_requests: int = 1,
        min_request_interval_ms: int = 0,
        debug_media_retention: bool = False,
        artifact_root: str = "./data/artifacts",
        cleanup_selected_on_success: bool = True,
        default_policy_id: str = DEFAULT_POLICY_ID,
        runtime: Runtime | None = None,
        commit_before_concurrent: Callable[[], None] | None = None,
        scheduler: VlmScheduler | None = None,
        write_coordinator: WriteCoordinator | None = None,
        retry_policy: RetryPolicy | None = None,
        terminal_write_max_attempts: int = 1,
        terminal_write_backoff_ms: int = 100,
        provider_options: dict[str, object] | None = None,
        detector_gate_enabled: bool = False,
        detector_gate_provider: str = "mock",
        detector_gate_model_id: str = "mock-detector-v1",
        detector_gate_rules: list[DetectorGateRuleSection] | None = None,
        pre_vlm_gate: PreVlmGatePort | None = None,
        pre_vlm_gate_profile: GateProfile | None = None,
    ) -> None:
        self._video_sources = video_sources
        self._jobs = jobs
        self._cameras = cameras
        self._policies = policies_repo
        self._video_processor = video_processor
        self._vlm = vlm
        self._timeline = timeline or TimelineRecorder.disabled()
        self._detector_gate = detector_gate
        self._publication = publication
        self._units = units
        self._model_calls = model_calls
        self._scale_task_id = scale_task_id
        self._provider = provider
        self._model_id = model_id
        self._pipeline_version = pipeline_version
        self._window_seconds = window_seconds
        self._overlap_seconds = overlap_seconds
        self._frames_per_segment = frames_per_segment
        self._max_concurrent_requests = max(1, int(max_concurrent_requests))
        # A shared (global) VlmScheduler may be injected so provider concurrency +
        # min-request-interval limits are enforced GLOBALLY across all scales/units
        # of the job/worker (Stage C1), not per-processor. When absent, build a
        # local one (preserves standalone/legacy behavior).
        self._scheduler = scheduler or VlmScheduler(
            max_concurrent=self._max_concurrent_requests,
            min_interval_ms=min_request_interval_ms,
        )
        self._debug_media = debug_media_retention
        self._artifact_root = artifact_root
        self._cleanup_selected_on_success = cleanup_selected_on_success
        self._default_policy_id = default_policy_id
        self._runtime = runtime
        self._commit_before_concurrent = commit_before_concurrent
        # DB write-serialization is a backend concern owned by the database adapter
        # (ARCHITECTURE_CONSTITUTION §7), not by this worker. The concurrent path
        # injects the runtime's backend coordinator (SQLite serializes via one
        # process-global lock; a future PG backend would no-op). The legacy serial
        # path has no concurrency, so a no-op default is safe. VLM calls stay
        # OUTSIDE ``write_coordinator.write()`` so DB critical sections stay short
        # (§9.1). This replaces the previous worker-owned ``threading.Lock``.
        self._write = write_coordinator or NO_OP_WRITE_COORDINATOR
        # Unit-level transient retry policy (default: 1 attempt = no retry, prior
        # behavior). The worker injects a configured policy on the real path.
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=1)
        self._terminal_write_max_attempts = max(1, int(terminal_write_max_attempts))
        self._terminal_write_backoff_ms = max(0, int(terminal_write_backoff_ms))
        self._provider_options = dict(provider_options or {})
        self._detector_gate_enabled = detector_gate_enabled
        self._detector_gate_provider = detector_gate_provider
        self._detector_gate_model_id = detector_gate_model_id
        self._detector_gate_rules = list(detector_gate_rules or [])
        self._pre_vlm_gate = pre_vlm_gate
        self._pre_vlm_gate_profile = pre_vlm_gate_profile

    def _db_write(self, write: Callable[[], None]) -> None:
        """Run a terminal DB write with bounded transient-lock retry (state hardening)."""
        run_db_write_with_retry(
            write,
            max_attempts=self._terminal_write_max_attempts,
            backoff_ms=self._terminal_write_backoff_ms,
        )

    def _timeline_event(
        self,
        event_name: str,
        *,
        event_phase: str = "instant",
        unit_id: str,
        analysis_job_id: str,
        video_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str | None = None,
        status: str | None = None,
        attempt_count: int | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_message: BaseException | str | None = None,
        correlation: Mapping[str, object] | None = None,
        metadata: Mapping[str, object] | None = None,
        span_id: str | None = None,
    ) -> None:
        self._timeline.event(
            event_name,
            event_phase=event_phase,  # type: ignore[arg-type]
            span_id=span_id,
            analysis_job_id=analysis_job_id,
            scale_task_id=self._scale_task_id,
            unit_id=unit_id,
            model_call_id=mcall_id,
            video_id=video_id,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            unit_kind="default_segment_window",
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            status=status,
            attempt_count=attempt_count,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message,
            correlation=dict(correlation) if correlation is not None else None,
            metadata=dict(metadata) if metadata is not None else None,
        )

    def _scheduler_run_with_timeline(
        self,
        *,
        unit_id: str,
        analysis_job_id: str,
        video_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str,
        vlm_request: VlmSegmentRequest,
    ) -> Callable[[Callable[[], VlmObservationOutput]], VlmObservationOutput]:
        def _run(call: Callable[[], VlmObservationOutput]) -> VlmObservationOutput:
            wait_span_id: str | None = None
            interval_span_id: str | None = None
            provider_span_id: str | None = None
            correlation = {"vlm_request_id": vlm_request.request_id}

            def _on_event(name: str, info: dict[str, int]) -> None:
                nonlocal wait_span_id, interval_span_id, provider_span_id
                if name == "wait_start":
                    wait_span_id = new_id("span")
                    self._timeline_event(
                        "vlm_scheduler_wait",
                        event_phase="start",
                        span_id=wait_span_id,
                        unit_id=unit_id,
                        analysis_job_id=analysis_job_id,
                        video_id=video_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        mcall_id=mcall_id,
                        correlation=correlation,
                        metadata={"max_concurrent": info.get("max_concurrent", 0)},
                    )
                elif name == "wait_finish":
                    self._timeline_event(
                        "vlm_scheduler_wait",
                        event_phase="finish",
                        span_id=wait_span_id,
                        unit_id=unit_id,
                        analysis_job_id=analysis_job_id,
                        video_id=video_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        mcall_id=mcall_id,
                        duration_ms=info.get("duration_ms", 0),
                        correlation=correlation,
                        metadata={"max_concurrent": info.get("max_concurrent", 0)},
                    )
                elif name == "interval_start":
                    interval_span_id = new_id("span")
                    self._timeline_event(
                        "vlm_scheduler_interval",
                        event_phase="start",
                        span_id=interval_span_id,
                        unit_id=unit_id,
                        analysis_job_id=analysis_job_id,
                        video_id=video_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        mcall_id=mcall_id,
                        correlation=correlation,
                    )
                elif name == "interval_finish":
                    self._timeline_event(
                        "vlm_scheduler_interval",
                        event_phase="finish",
                        span_id=interval_span_id,
                        unit_id=unit_id,
                        analysis_job_id=analysis_job_id,
                        video_id=video_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        mcall_id=mcall_id,
                        duration_ms=info.get("duration_ms", 0),
                        correlation=correlation,
                    )
                elif name == "call_start":
                    provider_span_id = new_id("span")
                    self._timeline_event(
                        "vlm_provider_call",
                        event_phase="start",
                        span_id=provider_span_id,
                        unit_id=unit_id,
                        analysis_job_id=analysis_job_id,
                        video_id=video_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        mcall_id=mcall_id,
                        correlation=correlation,
                        metadata={"provider": self._provider, "model_id": self._model_id},
                    )
                elif name in ("call_finish", "call_fail"):
                    self._timeline_event(
                        "vlm_provider_call",
                        event_phase="finish" if name == "call_finish" else "fail",
                        span_id=provider_span_id,
                        unit_id=unit_id,
                        analysis_job_id=analysis_job_id,
                        video_id=video_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        mcall_id=mcall_id,
                        duration_ms=info.get("duration_ms", 0),
                        correlation=correlation,
                        metadata={"provider": self._provider, "model_id": self._model_id},
                    )

            return self._scheduler.run(call, on_event=_on_event)

        return _run

    def process(self, analysis_job_id: str, video_id: str) -> ScaleProcessResult:
        """Run the pipeline; each successful unit is published immediately."""
        job = self._jobs.get_job(analysis_job_id)
        if job is None:
            raise NotFoundError(f"job {analysis_job_id} not found")
        ctx = resolve_video_context(
            video_id,
            video_sources=self._video_sources,
            cameras=self._cameras,
            policies_repo=self._policies,
            default_policy_id=self._default_policy_id,
        )

        metadata = self._video_processor.probe(ctx.source.source_uri)
        end_dt, _ = video_end_iso(ctx.source, metadata.duration_ms)
        self._video_sources.update_probe_metadata(
            video_id, duration_ms=metadata.duration_ms, video_end_time=end_dt
        )

        windows = policies.plan_default_segments(
            metadata.duration_ms,
            window_seconds=self._window_seconds,
            overlap_seconds=self._overlap_seconds,
        )

        # Plan all units before executing any VLM calls. In concurrent mode each
        # unit is created inside its worker session so threads do not depend on
        # uncommitted rows from this session.
        planned: list[tuple[AnalysisUnit, tuple[int, int]]] = []
        # Per-unit isolation: when a runtime is injected, every unit runs in its
        # OWN fresh session (create/claim -> frame select -> VLM -> publish -> mark
        # terminal), so a late unit failure/skip can NEVER roll back earlier
        # committed successes (task cctv-memory-20260612-1854 §D). The legacy
        # shared-session path (_run_unit) is used only when no runtime is available.
        isolated = self._runtime is not None
        concurrent = self._max_concurrent_requests > 1 and self._runtime is not None
        for idx, window in enumerate(windows):
            idem_key = (
                f"{analysis_job_id}:{self._scale_task_id}"
                f":default_segment:{window.start_ms}:{window.end_ms}"
            )
            unit = AnalysisUnit(
                unit_id=new_id("unit"),
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                unit_kind="default_segment_window",
                segment_start_ms=window.start_ms,
                segment_end_ms=window.end_ms,
                window_index=idx,
                idempotency_key=idem_key,
            )
            if not isolated:
                unit = self._units.create_or_get_by_idempotency(unit)
            planned.append((unit, (window.start_ms, window.end_ms)))

        total = len(planned)
        succeeded = 0
        failed = 0
        skipped = 0

        if concurrent:
            if self._commit_before_concurrent is not None:
                self._commit_before_concurrent()
            with ThreadPoolExecutor(max_workers=self._max_concurrent_requests) as executor:
                futures = [
                    executor.submit(
                        self._run_unit_in_fresh_session,
                        unit,
                        start_ms,
                        end_ms,
                        analysis_job_id,
                        video_id,
                        ctx,
                    )
                    for unit, (start_ms, end_ms) in planned
                ]
                for future in as_completed(futures):
                    outcome = future.result()
                    if outcome is UnitOutcome.SUCCEEDED:
                        succeeded += 1
                    elif outcome is UnitOutcome.SKIPPED:
                        skipped += 1
                    else:
                        failed += 1
        elif isolated:
            # Serial but per-unit isolated sessions (no shared-session rollback).
            if self._commit_before_concurrent is not None:
                self._commit_before_concurrent()
            for unit, (start_ms, end_ms) in planned:
                outcome = self._run_unit_in_fresh_session(
                    unit, start_ms, end_ms, analysis_job_id, video_id, ctx
                )
                if outcome is UnitOutcome.SUCCEEDED:
                    succeeded += 1
                elif outcome is UnitOutcome.SKIPPED:
                    skipped += 1
                else:
                    failed += 1
        else:
            for unit, (start_ms, end_ms) in planned:
                outcome = self._run_unit(
                    unit,
                    start_ms,
                    end_ms,
                    analysis_job_id,
                    video_id,
                    job,
                    ctx=ctx,
                    publication=self._publication,
                    units=self._units,
                    model_calls=self._model_calls,
                )
                if outcome is UnitOutcome.SUCCEEDED:
                    succeeded += 1
                elif outcome is UnitOutcome.SKIPPED:
                    skipped += 1
                else:
                    failed += 1

        return ScaleProcessResult(
            total=total, succeeded=succeeded, failed=failed, skipped=skipped
        )

    def plan_units(
        self, analysis_job_id: str, video_id: str
    ) -> list[PlannedUnit]:
        """Plan (but do not run) this scale's units for cross-scale scheduling.

        Probes the video, plans windows, and returns one ``PlannedUnit`` per
        window whose ``run`` executes the unit in its OWN fresh session (frame
        selection -> VLM via the shared scheduler -> per-unit publication). The
        caller (CrossScaleUnitScheduler) decides dispatch order; each unit is
        idempotent and publishes independently, so out-of-order completion across
        scales is safe (vlm-analysis-contract §0.1).

        Requires an injected runtime (fresh per-unit sessions). The probe metadata
        write happens in the caller's planning session.
        """
        assert self._runtime is not None, "plan_units requires an injected runtime"
        job = self._jobs.get_job(analysis_job_id)
        if job is None:
            raise NotFoundError(f"job {analysis_job_id} not found")
        ctx = resolve_video_context(
            video_id,
            video_sources=self._video_sources,
            cameras=self._cameras,
            policies_repo=self._policies,
            default_policy_id=self._default_policy_id,
        )
        metadata = self._video_processor.probe(ctx.source.source_uri)
        end_dt, _ = video_end_iso(ctx.source, metadata.duration_ms)
        self._video_sources.update_probe_metadata(
            video_id, duration_ms=metadata.duration_ms, video_end_time=end_dt
        )
        windows = policies.plan_default_segments(
            metadata.duration_ms,
            window_seconds=self._window_seconds,
            overlap_seconds=self._overlap_seconds,
        )
        planned: list[PlannedUnit] = []
        for idx, window in enumerate(windows):
            idem_key = (
                f"{analysis_job_id}:{self._scale_task_id}"
                f":default_segment:{window.start_ms}:{window.end_ms}"
            )
            unit = AnalysisUnit(
                unit_id=new_id("unit"),
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                unit_kind="default_segment_window",
                segment_start_ms=window.start_ms,
                segment_end_ms=window.end_ms,
                window_index=idx,
                idempotency_key=idem_key,
            )

            def _run(
                u: AnalysisUnit = unit,
                s: int = window.start_ms,
                e: int = window.end_ms,
            ) -> UnitOutcome:
                return self._run_unit_in_fresh_session(
                    u, s, e, analysis_job_id, video_id, ctx
                )

            planned.append(
                PlannedUnit(scale=AnalysisScale.DEFAULT_SEGMENT, run=_run)
            )
        return planned

    def _run_unit_in_fresh_session(
        self,
        unit_template: AnalysisUnit,
        start_ms: int,
        end_ms: int,
        analysis_job_id: str,
        video_id: str,
        ctx: VideoContext,
    ) -> UnitOutcome:
        """Run one unit with its own session. VLM runs concurrently; DB writes
        are serialized via the backend write coordinator (SQLite single-writer).

        Returns a ``UnitOutcome``; NEVER leaves the unit ``running``: frame
        extraction / media-ref building and the VLM call are all inside terminal
        handling (task cctv-memory-20260612-1854 §A). Zero usable frames =>
        ``skipped(insufficient_frames)``; any other extraction/VLM error =>
        ``failed``; some frames (even < requested) => proceed to VLM.

        Lifecycle guard (task cctv-memory-20260616-1850 §B1): once the unit is
        ``mark_running``, the entire post-running body runs under an outer
        try/except. Every handled path already terminalizes the unit, but if an
        UNFORESEEN exception (including a terminal-write that exhausted its bounded
        retry and re-raised) escapes, the guard force-terminalizes the unit to
        FAILED with phase-tagged durable evidence (pre_vlm/vlm/post_vlm) so the
        unit can never remain ``running`` with no diagnosis. No new durable state.
        """
        assert self._runtime is not None

        # --- create/claim the unit (DB write, serialized) ---------------------
        mcall_id = new_id("mcall")
        with self._write.write(), self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            job = repos.analysis_job().get_job(analysis_job_id)
            if job is None:
                return UnitOutcome.FAILED
            prompt_version = job.prompt_version
            model_version = job.model_version
            unit = repos.analysis_unit().create_or_get_by_idempotency(unit_template)
            if unit.status is TaskStatus.SUCCEEDED:
                return UnitOutcome.SUCCEEDED
            if unit.status is TaskStatus.SKIPPED:
                return UnitOutcome.SKIPPED
            repos.analysis_unit().mark_running(unit.unit_id, model_call_id=mcall_id)
        unit_id = unit.unit_id
        self._timeline_event(
            "unit_running",
            unit_id=unit_id,
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            start_ms=start_ms,
            end_ms=end_ms,
            mcall_id=mcall_id,
            status=TaskStatus.RUNNING.value,
            attempt_count=unit.attempt_count + 1,
        )

        # Phase tracker for the lifecycle guard: where did we die if something
        # unforeseen escapes? Always durable evidence (no "running, no log").
        phase = UnitPhase()
        try:
            return self._execute_running_unit(
                unit_id=unit_id,
                mcall_id=mcall_id,
                start_ms=start_ms,
                end_ms=end_ms,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                ctx=ctx,
                prompt_version=prompt_version,
                model_version=model_version,
                phase=phase,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort lifecycle guard
            # Every normal path above already terminalized the unit. Reaching here
            # means an UNFORESEEN escape (e.g. terminal write exhausted its bounded
            # retry and re-raised). Force the unit to a terminal state with
            # phase-tagged evidence; never leave it ``running`` silently. If even
            # this best-effort write fails, the bounded orphan/job reconciliation
            # (§7.1 / §B2) is the backstop — we still re-surface nothing as success.
            self._force_terminalize_running(
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                phase=phase.name,
                exc=exc,
            )
            return UnitOutcome.FAILED

    def _execute_running_unit(
        self,
        *,
        unit_id: str,
        mcall_id: str,
        start_ms: int,
        end_ms: int,
        analysis_job_id: str,
        video_id: str,
        ctx: VideoContext,
        prompt_version: str | None,
        model_version: str | None,
        phase: UnitPhase,
    ) -> UnitOutcome:
        """Body of a running unit (frame select -> VLM -> publish). See guard above."""
        # --- frame selection + media refs (terminalized, NO db lock) ----------
        # These run AFTER mark_running and BEFORE the VLM call, so they MUST be
        # inside terminal handling or a raise here would strand the unit running.
        phase.name = "pre_vlm"
        try:
            with self._timeline.span(
                "frame_select",
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                unit_id=unit_id,
                model_call_id=mcall_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                unit_kind="default_segment_window",
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                metadata={"frames_requested": self._frames_per_segment},
            ):
                selection = select_frames_for_unit(
                    self._video_processor,
                    ctx.source.source_uri,
                    start_ms,
                    end_ms,
                    self._frames_per_segment,
                    unit_key=mcall_id,
                )
            frame_uris = selection.frame_uris
            if not frame_uris:
                # Zero usable frames (e.g. near-EOF window): skip, do not fail.
                self._terminalize_unit_skipped(
                    unit_id, analysis_job_id, start_ms, end_ms, mcall_id
                )
                self._timeline_event(
                    "unit_finished",
                    unit_id=unit_id,
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    mcall_id=mcall_id,
                    status=TaskStatus.SKIPPED.value,
                    error_code="insufficient_frames",
                )
                return UnitOutcome.SKIPPED
            with self._timeline.span(
                "media_refs_built",
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                unit_id=unit_id,
                model_call_id=mcall_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                unit_kind="default_segment_window",
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                metadata={"frame_count": len(frame_uris)},
            ):
                media_refs = build_media_refs(
                    selection.media_refs_input,
                    model_call_id=mcall_id,
                    debug_media_retention=self._debug_media,
                    artifact_root=self._artifact_root,
                )
        except InsufficientFramesError:
            self._terminalize_unit_skipped(
                unit_id, analysis_job_id, start_ms, end_ms, mcall_id
            )
            self._timeline_event(
                "unit_finished",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                status=TaskStatus.SKIPPED.value,
                error_code="insufficient_frames",
            )
            return UnitOutcome.SKIPPED
        except Exception as exc:  # noqa: BLE001
            self._terminalize_unit_failed(
                unit_id, analysis_job_id, start_ms, end_ms, mcall_id,
                error_code="frame_extraction_failed", exc=exc,
            )
            self._timeline_event(
                "unit_finished",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                status=TaskStatus.FAILED.value,
                error_code="frame_extraction_failed",
                error_message=exc,
            )
            return UnitOutcome.FAILED

        detector_summary: dict[str, object] | None = None
        gate_bundle = run_pre_vlm_gate(
            gate=self._pre_vlm_gate,
            profile=self._pre_vlm_gate_profile,
            media_refs_input=selection.media_refs_input,
            analysis_job_id=analysis_job_id,
            scale_task_id=self._scale_task_id,
            unit_id=unit_id,
            video_id=video_id,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            unit_kind="default_segment_window",
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            provider=(self._pre_vlm_gate_profile.provider if self._pre_vlm_gate_profile else ""),
            model_id=self._pre_vlm_gate_profile.model_id if self._pre_vlm_gate_profile else None,
        )
        if gate_bundle is not None:
            detector_summary = gate_bundle.summary
            if not gate_bundle.triggered_vlm:
                self._timeline_event(
                    "pre_vlm_gate_suppressed",
                    unit_id=unit_id,
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    mcall_id=mcall_id,
                    status="suppressed_vlm",
                    metadata={"decision": gate_bundle.decision.model_dump(mode="json")},
                )
                return self._publish_detector_only_unit(
                    unit_id=unit_id,
                    analysis_job_id=analysis_job_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    ctx=ctx,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    pipeline_version=self._pipeline_version,
                    detector_summary=gate_bundle.summary,
                    frame_uris=frame_uris,
                )
        elif self._detector_gate_enabled and self._detector_gate is not None:
            gate_log_id = new_id("gate")
            gate_started = datetime.now(UTC)
            frame_inputs = build_detector_frame_inputs(selection.media_refs_input)
            with self._timeline.span(
                "detector_gate",
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                unit_id=unit_id,
                model_call_id=mcall_id,
                video_id=video_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                unit_kind="default_segment_window",
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                metadata={"gate_log_id": gate_log_id, "frame_count": len(frame_inputs)},
            ):
                gate_results = self._detector_gate.detect_frames(frame_inputs)
                gate_finished = datetime.now(UTC)
                gate = decide_detector_gate(
                    results=gate_results,
                    rules=self._detector_gate_rules,
                    provider=self._detector_gate_provider,
                    model_id=self._detector_gate_model_id,
                    gate_log_id=gate_log_id,
                )
            detector_summary = gate.summary

            def _persist_gate_log() -> None:
                with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                    repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                    repos.detector_gate_log().create_log(
                        DetectorGateLog(
                            gate_log_id=gate_log_id,
                            analysis_job_id=analysis_job_id,
                            scale_task_id=self._scale_task_id,
                            unit_id=unit_id,
                            video_id=video_id,
                            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                            segment_start_ms=start_ms,
                            segment_end_ms=end_ms,
                            provider=self._detector_gate_provider,
                            model_id=self._detector_gate_model_id,
                            status="succeeded",
                            decision=gate.decision,
                            frame_evidence=gate.frame_evidence,
                            evidence_hash=gate.evidence_hash,
                            rule_config_hash=gate.rule_config_hash,
                            media_refs=[],
                            started_at=gate_started,
                            finished_at=gate_finished,
                            duration_ms=int(
                                (gate_finished - gate_started).total_seconds() * 1000
                            ),
                        )
                    )

            self._db_write(_persist_gate_log)
            if not gate.triggered_vlm:
                self._timeline_event(
                    "detector_gate_suppressed",
                    unit_id=unit_id,
                    analysis_job_id=analysis_job_id,
                    video_id=video_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    mcall_id=mcall_id,
                    status="suppressed_vlm",
                    metadata={"gate_log_id": gate_log_id, "decision": gate.decision},
                )
                return self._publish_detector_only_unit(
                    unit_id=unit_id,
                    analysis_job_id=analysis_job_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    ctx=ctx,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    pipeline_version=self._pipeline_version,
                    detector_summary=gate.summary,
                    frame_uris=frame_uris,
                )

        vlm_request = VlmSegmentRequest(
            request_id=new_id("vlm_req"),
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            camera_id=ctx.source.camera_id,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            frame_uris=frame_uris,
            prompt_version=prompt_version,
            model_version=model_version,
        )
        input_manifest_hash, input_manifest = build_vlm_input_manifest(
            request=vlm_request,
            media_refs=media_refs,
            provider_options=self._provider_options,
            pipeline_version=self._pipeline_version,
        )
        started = _now_iso()
        phase.name = "vlm"

        def _log_attempt_started(attempt: int) -> None:
            self._timeline_event(
                "vlm_attempt",
                event_phase="start",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                status="running",
                attempt_count=attempt,
                correlation={"vlm_request_id": vlm_request.request_id},
            )

        def _log_attempt_succeeded(rec: VlmAttempt) -> None:
            self._timeline_event(
                "vlm_attempt",
                event_phase="finish",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                status="succeeded",
                attempt_count=rec.attempt,
                correlation={"vlm_request_id": vlm_request.request_id},
            )

        def _log_failed_attempt(rec: VlmAttempt) -> None:
            # One FAILED ModelCallLog per failed attempt (real attempt_count); the unit
            # stays ``running`` across attempts and is terminalized below.
            attempt_mcall_id = mcall_id if rec.attempt == 1 else new_id("mcall")
            now = _now_iso()

            def _w() -> None:
                with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                    repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                    repos.model_call_log().create_log(
                        ModelCallLog(
                            model_call_id=attempt_mcall_id,
                            analysis_job_id=analysis_job_id,
                            scale_task_id=self._scale_task_id,
                            unit_id=unit_id,
                            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                            segment_start_ms=start_ms,
                            segment_end_ms=end_ms,
                            provider=self._provider,
                            model_id=self._model_id,
                            pipeline_version=self._pipeline_version,
                            status=ModelCallStatus.FAILED,
                            attempt_count=rec.attempt,
                            error_type=rec.error_type,
                            error_message=rec.error_message,
                            media_refs=media_refs,
                            payload_hash=input_manifest_hash,
                            attempt_details=attach_manifest_to_attempts(
                                [rec.to_dict()],
                                input_manifest_hash=input_manifest_hash,
                                input_manifest=input_manifest,
                            ),
                            started_at=datetime.fromisoformat(started),
                            finished_at=datetime.fromisoformat(now),
                        )
                    )

            self._db_write(_w)
            self._timeline_event(
                "vlm_attempt",
                event_phase="fail",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=attempt_mcall_id,
                status="failed",
                attempt_count=rec.attempt,
                error_code=rec.error_type,
                error_message=rec.error_message,
                correlation={"vlm_request_id": vlm_request.request_id},
                metadata={
                    "transient": bool(rec.transient),
                    "backoff_ms": rec.backoff_ms,
                    "will_retry": rec.backoff_ms is not None,
                },
            )

        result = execute_vlm_with_retry(
            request=vlm_request,
            analyze=self._vlm.analyze_segment,
            scheduler_run=self._scheduler_run_with_timeline(
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                vlm_request=vlm_request,
            ),
            policy=self._retry_policy,
            on_attempt_started=_log_attempt_started,
            on_attempt_succeeded=_log_attempt_succeeded,
            on_attempt_failed=_log_failed_attempt,
        )
        if result.error is not None:
            err = result.error
            error_code = vlm_failure_error_code(err)
            # Every failed attempt was already logged via _log_failed_attempt; the
            # terminal write only marks the unit failed (no duplicate ModelCallLog).
            self._db_write(
                lambda: self._mark_unit_failed(
                    unit_id=unit_id,
                    error_code=error_code,
                    exc=err,
                    attempts=result.attempts,
                    mcall_id=mcall_id,
                )
            )
            self._timeline_event(
                "unit_finished",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                status=TaskStatus.FAILED.value,
                attempt_count=result.attempts,
                error_code=error_code,
                error_message=err,
            )
            return UnitOutcome.FAILED

        output = result.output
        assert output is not None
        success_mcall_id = mcall_id if result.attempts == 1 else new_id("mcall")
        finished = _now_iso()
        raw_out = json.dumps(output.model_dump())

        # --- publish + log + mark succeeded (DB writes, serialized) -----------
        phase.name = "post_vlm"
        outcome = UnitOutcome.SUCCEEDED
        produced_ids: list[str] = []

        def _publish_and_mark() -> None:
            nonlocal outcome, produced_ids
            with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                job = repos.analysis_job().get_job(analysis_job_id)
                if job is None:
                    outcome = UnitOutcome.FAILED
                    return
                record = build_observation_record(
                    ctx=ctx,
                    analysis_job_id=analysis_job_id,
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    output=output,
                    model_version=job.model_version,
                    prompt_version=job.prompt_version,
                    pipeline_version=job.pipeline_version,
                    extra_attributes=(
                        {"detector_gate": detector_summary}
                        if detector_summary is not None
                        else None
                    ),
                )
                pub_result = PublicationService(repos.publication()).publish(
                    PublishObservationRecordsCommand(
                        command_id=new_id("pub"),
                        analysis_job_id=analysis_job_id,
                        records=[record],
                    )
                )
                produced_ids = (
                    pub_result.created_record_ids + pub_result.updated_record_ids
                )
                repos.model_call_log().create_log(
                    ModelCallLog(
                        model_call_id=success_mcall_id,
                        analysis_job_id=analysis_job_id,
                        scale_task_id=self._scale_task_id,
                        unit_id=unit_id,
                        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                        segment_start_ms=start_ms,
                        segment_end_ms=end_ms,
                        provider=self._provider,
                        model_id=self._model_id,
                        prompt_version=job.prompt_version,
                        pipeline_version=self._pipeline_version,
                        status=ModelCallStatus.SUCCEEDED,
                        attempt_count=result.attempts,
                        raw_text_output=raw_out,
                        response_hash=_sha256_short(raw_out),
                        payload_hash=input_manifest_hash,
                        parsed_output=output.model_dump(),
                        validation_status="passed",
                        media_refs=media_refs,
                        attempt_details=attach_manifest_to_attempts(
                            result.attempt_details,
                            input_manifest_hash=input_manifest_hash,
                            input_manifest=input_manifest,
                        ),
                        started_at=datetime.fromisoformat(started),
                        finished_at=datetime.fromisoformat(finished),
                    )
                )
                repos.analysis_unit().mark_succeeded(
                    unit_id,
                    model_call_id=success_mcall_id,
                    record_ids=produced_ids,
                    attempt_count=result.attempts,
                )

        self._db_write(_publish_and_mark)
        if outcome is UnitOutcome.FAILED:
            return UnitOutcome.FAILED
        self._timeline_event(
            "publication_finished",
            unit_id=unit_id,
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            start_ms=start_ms,
            end_ms=end_ms,
            mcall_id=success_mcall_id,
            status="succeeded",
            metadata={"record_count": len(produced_ids), "record_ids": produced_ids},
        )
        self._timeline_event(
            "unit_finished",
            unit_id=unit_id,
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            start_ms=start_ms,
            end_ms=end_ms,
            mcall_id=success_mcall_id,
            status=TaskStatus.SUCCEEDED.value,
            attempt_count=result.attempts,
        )
        # Unit succeeded: drop working selected frames unless debug retention is on
        # (debug artifacts live under artifact_root and are untouched).
        if self._cleanup_selected_on_success and not self._debug_media:
            cleanup_selected_frames(frame_uris)
        return UnitOutcome.SUCCEEDED

    def _publish_detector_only_unit(
        self,
        *,
        unit_id: str,
        analysis_job_id: str,
        start_ms: int,
        end_ms: int,
        ctx: VideoContext,
        model_version: str | None,
        prompt_version: str | None,
        pipeline_version: str | None,
        detector_summary: dict[str, object],
        frame_uris: list[str],
    ) -> UnitOutcome:
        outcome = UnitOutcome.SUCCEEDED
        produced_ids: list[str] = []

        def _publish_and_mark() -> None:
            nonlocal produced_ids
            with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                record = build_detector_only_observation_record(
                    ctx=ctx,
                    analysis_job_id=analysis_job_id,
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    pipeline_version=pipeline_version,
                    detector_gate_summary=detector_summary,
                )
                pub_result = PublicationService(repos.publication()).publish(
                    PublishObservationRecordsCommand(
                        command_id=new_id("pub"),
                        analysis_job_id=analysis_job_id,
                        records=[record],
                    )
                )
                produced_ids = (
                    pub_result.created_record_ids + pub_result.updated_record_ids
                )
                repos.analysis_unit().mark_succeeded(
                    unit_id,
                    model_call_id=None,
                    record_ids=produced_ids,
                    attempt_count=0,
                )

        self._db_write(_publish_and_mark)
        if outcome is UnitOutcome.SUCCEEDED:
            self._timeline_event(
                "publication_finished",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=ctx.source.video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                status="succeeded",
                metadata={
                    "record_count": len(produced_ids),
                    "record_ids": produced_ids,
                    "detector_only": True,
                },
            )
            self._timeline_event(
                "unit_finished",
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                video_id=ctx.source.video_id,
                start_ms=start_ms,
                end_ms=end_ms,
                status=TaskStatus.SUCCEEDED.value,
                attempt_count=0,
                metadata={"detector_only": True},
            )
            if self._cleanup_selected_on_success and not self._debug_media:
                cleanup_selected_frames(frame_uris)
        return outcome

    def _force_terminalize_running(
        self,
        *,
        unit_id: str,
        analysis_job_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str,
        phase: str,
        exc: BaseException,
    ) -> None:
        """Last-resort terminalization for the lifecycle guard (task §B1).

        Reached only when an UNFORESEEN exception escaped the running-unit body
        (every normal path already terminalizes). Writes a FAILED ModelCallLog with
        phase-tagged evidence (``error_type='lifecycle_guard:<phase>'``) AND marks
        the unit FAILED, both under the backend write coordinator with bounded
        transient-lock retry. error_code ``analysis_unit_failed`` (error-code-contract
        §4). If even this best-effort write fails, we log loudly and swallow so the
        outer scheduler still records a FAILED tally; the bounded orphan/job
        reconciliation (§7.1 / §B2) remains the backstop. We never claim success.
        """
        logger.error(
            "unit %s escaped running body at phase=%s: %s: %s",
            unit_id, phase, type(exc).__name__, str(exc)[:300],
        )
        now = _now_iso()

        def _w() -> None:
            with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                repos.model_call_log().create_log(
                    ModelCallLog(
                        model_call_id=new_id("mcall"),
                        analysis_job_id=analysis_job_id,
                        scale_task_id=self._scale_task_id,
                        unit_id=unit_id,
                        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                        segment_start_ms=start_ms,
                        segment_end_ms=end_ms,
                        provider=self._provider,
                        model_id=self._model_id,
                        pipeline_version=self._pipeline_version,
                        status=ModelCallStatus.FAILED,
                        attempt_count=1,
                        error_type=f"lifecycle_guard:{phase}",
                        error_message=f"{type(exc).__name__}: {str(exc)[:400]}",
                        started_at=datetime.fromisoformat(now),
                        finished_at=datetime.fromisoformat(now),
                    )
                )
                repos.analysis_unit().mark_failed(
                    unit_id,
                    error_code="analysis_unit_failed",
                    error_message=f"lifecycle_guard:{phase}: {str(exc)[:400]}",
                    model_call_id=mcall_id,
                )

        try:
            self._db_write(_w)
        except Exception:  # noqa: BLE001 - backstop is orphan/job reconciliation
            logger.exception(
                "force-terminalize DB write failed for unit %s (phase=%s); "
                "relying on orphan/job reconciliation backstop",
                unit_id, phase,
            )

    def _mark_unit_failed(
        self,
        *,
        unit_id: str,
        error_code: str,
        exc: BaseException,
        attempts: int,
        mcall_id: str,
    ) -> None:
        """Mark a unit FAILED after the VLM retry budget is exhausted.

        Each failed attempt already wrote its own FAILED ModelCallLog (via the retry
        callback), so this only flips the unit to its terminal state with the real
        attempt count. Wrapped by ``_db_write`` so a transient lock cannot strand the
        unit running.
        """
        with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
            repos = self._runtime.repositories(session)  # type: ignore[union-attr]
            repos.analysis_unit().mark_failed(
                unit_id,
                error_code=error_code,
                error_message=str(exc)[:500],
                model_call_id=mcall_id,
                attempt_count=attempts,
            )

    def _terminalize_unit_failed(
        self,
        unit_id: str,
        analysis_job_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str,
        *,
        error_code: str,
        exc: Exception,
    ) -> None:
        """Mark a unit FAILED + log a FAILED ModelCallLog (own serialized session).

        Used for frame-extraction/media-ref failures that occur after mark_running
        and outside the VLM call, so the unit never stays ``running``. Wrapped by
        ``_db_write`` so a transient DB lock cannot strand the unit running.
        """
        now = _now_iso()

        def _w() -> None:
            with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                repos.model_call_log().create_log(
                    ModelCallLog(
                        model_call_id=mcall_id,
                        analysis_job_id=analysis_job_id,
                        scale_task_id=self._scale_task_id,
                        unit_id=unit_id,
                        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                        segment_start_ms=start_ms,
                        segment_end_ms=end_ms,
                        provider=self._provider,
                        model_id=self._model_id,
                        pipeline_version=self._pipeline_version,
                        status=ModelCallStatus.FAILED,
                        attempt_count=1,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:500],
                        started_at=datetime.fromisoformat(now),
                        finished_at=datetime.fromisoformat(now),
                    )
                )
                repos.analysis_unit().mark_failed(
                    unit_id,
                    error_code=error_code,
                    error_message=str(exc)[:500],
                    model_call_id=mcall_id,
                )

        self._db_write(_w)

    def _terminalize_unit_skipped(
        self,
        unit_id: str,
        analysis_job_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str,
    ) -> None:
        """Mark a unit SKIPPED(insufficient_frames) (own serialized session).

        Zero usable frames near EOF is a benign, expected condition (owner
        decision): no record, no failure — the unit is skipped with a structured
        reason and never left ``running``. Wrapped by ``_db_write`` (state hardening).
        """

        def _w() -> None:
            with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                repos.analysis_unit().mark_skipped(
                    unit_id,
                    skipped_reason="insufficient_frames",
                    model_call_id=mcall_id,
                )

        self._db_write(_w)

    def _run_unit(  # type: ignore[no-untyped-def]
        self,
        unit,
        start_ms: int,
        end_ms: int,
        analysis_job_id: str,
        video_id: str,
        job,
        *,
        ctx: VideoContext,
        publication,
        units,
        model_calls,
        commit_after_mark_running=None,
    ) -> UnitOutcome:
        """Execute one VLM unit via the scheduler (legacy shared-session path).

        Used only when no runtime is injected (runtime=None). Frame extraction is
        terminalized like the isolated path: zero usable frames =>
        skipped(insufficient_frames); other extraction errors => failed; never
        leaves the unit running.
        """
        if unit.status is TaskStatus.SUCCEEDED:
            return UnitOutcome.SUCCEEDED
        if unit.status is TaskStatus.SKIPPED:
            return UnitOutcome.SKIPPED

        mcall_id = new_id("mcall")
        units.mark_running(unit.unit_id, model_call_id=mcall_id)
        if commit_after_mark_running is not None:
            commit_after_mark_running()

        # Frame extraction + media refs are terminalized so a raise here cannot
        # leave the unit running (task cctv-memory-20260612-1854 §A).
        try:
            selection = select_frames_for_unit(
                self._video_processor,
                # source_uri is not exposed externally; it's internal worker use
                ctx.source.source_uri,
                start_ms,
                end_ms,
                self._frames_per_segment,
                unit_key=mcall_id,
            )
            frame_uris = selection.frame_uris
            if not frame_uris:
                units.mark_skipped(
                    unit.unit_id,
                    skipped_reason="insufficient_frames",
                    model_call_id=mcall_id,
                )
                return UnitOutcome.SKIPPED
            media_refs = build_media_refs(
                selection.media_refs_input,
                model_call_id=mcall_id,
                debug_media_retention=self._debug_media,
                artifact_root=self._artifact_root,
            )
        except InsufficientFramesError:
            units.mark_skipped(
                unit.unit_id,
                skipped_reason="insufficient_frames",
                model_call_id=mcall_id,
            )
            return UnitOutcome.SKIPPED
        except Exception as exc:  # noqa: BLE001
            now = _now_iso()
            model_calls.create_log(
                ModelCallLog(
                    model_call_id=mcall_id,
                    analysis_job_id=analysis_job_id,
                    scale_task_id=self._scale_task_id,
                    unit_id=unit.unit_id,
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    provider=self._provider,
                    model_id=self._model_id,
                    prompt_version=job.prompt_version,
                    pipeline_version=self._pipeline_version,
                    status=ModelCallStatus.FAILED,
                    attempt_count=1,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    started_at=datetime.fromisoformat(now),
                    finished_at=datetime.fromisoformat(now),
                )
            )
            units.mark_failed(
                unit.unit_id,
                error_code="frame_extraction_failed",
                error_message=str(exc)[:500],
                model_call_id=mcall_id,
            )
            return UnitOutcome.FAILED

        vlm_request = VlmSegmentRequest(
            request_id=new_id("vlm_req"),
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            camera_id=ctx.source.camera_id,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            frame_uris=frame_uris,
            prompt_version=job.prompt_version,
            model_version=job.model_version,
        )
        input_manifest_hash, input_manifest = build_vlm_input_manifest(
            request=vlm_request,
            media_refs=media_refs,
            provider_options=self._provider_options,
            pipeline_version=self._pipeline_version,
        )

        started = _now_iso()

        def _log_failed_attempt(rec: VlmAttempt) -> None:
            attempt_mcall_id = mcall_id if rec.attempt == 1 else new_id("mcall")
            now = _now_iso()
            model_calls.create_log(
                ModelCallLog(
                    model_call_id=attempt_mcall_id,
                    analysis_job_id=analysis_job_id,
                    scale_task_id=self._scale_task_id,
                    unit_id=unit.unit_id,
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    provider=self._provider,
                    model_id=self._model_id,
                    prompt_version=job.prompt_version,
                    pipeline_version=self._pipeline_version,
                    status=ModelCallStatus.FAILED,
                    attempt_count=rec.attempt,
                    error_type=rec.error_type,
                    error_message=rec.error_message,
                    media_refs=media_refs,
                    payload_hash=input_manifest_hash,
                    attempt_details=attach_manifest_to_attempts(
                        [rec.to_dict()],
                        input_manifest_hash=input_manifest_hash,
                        input_manifest=input_manifest,
                    ),
                    started_at=datetime.fromisoformat(started),
                    finished_at=datetime.fromisoformat(now),
                )
            )

        result = execute_vlm_with_retry(
            request=vlm_request,
            analyze=self._vlm.analyze_segment,
            scheduler_run=self._scheduler.run,
            policy=self._retry_policy,
            on_attempt_failed=_log_failed_attempt,
        )
        if result.error is not None:
            err = result.error
            error_code = vlm_failure_error_code(err)
            # Failed attempts already logged via _log_failed_attempt; just terminalize.
            units.mark_failed(
                unit.unit_id,
                error_code=error_code,
                error_message=str(err)[:500],
                model_call_id=mcall_id,
                attempt_count=result.attempts,
            )
            return UnitOutcome.FAILED

        output = result.output
        assert output is not None
        success_mcall_id = mcall_id if result.attempts == 1 else new_id("mcall")
        finished = _now_iso()
        raw_out = json.dumps(output.model_dump())
        record = build_observation_record(
            ctx=ctx,
            analysis_job_id=analysis_job_id,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            output=output,
            model_version=job.model_version,
            prompt_version=job.prompt_version,
            pipeline_version=job.pipeline_version,
        )
        pub_result = publication.publish(
            PublishObservationRecordsCommand(
                command_id=new_id("pub"),
                analysis_job_id=analysis_job_id,
                records=[record],
            )
        )
        produced_ids = pub_result.created_record_ids + pub_result.updated_record_ids

        model_calls.create_log(
            ModelCallLog(
                model_call_id=success_mcall_id,
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                unit_id=unit.unit_id,
                analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                provider=self._provider,
                model_id=self._model_id,
                prompt_version=job.prompt_version,
                pipeline_version=self._pipeline_version,
                status=ModelCallStatus.SUCCEEDED,
                attempt_count=result.attempts,
                raw_text_output=raw_out,
                response_hash=_sha256_short(raw_out),
                payload_hash=input_manifest_hash,
                parsed_output=output.model_dump(),
                validation_status="passed",
                media_refs=media_refs,
                attempt_details=attach_manifest_to_attempts(
                    result.attempt_details,
                    input_manifest_hash=input_manifest_hash,
                    input_manifest=input_manifest,
                ),
                started_at=datetime.fromisoformat(started),
                finished_at=datetime.fromisoformat(finished),
            )
        )
        units.mark_succeeded(
            unit.unit_id,
            model_call_id=success_mcall_id,
            record_ids=produced_ids,
            attempt_count=result.attempts,
        )
        if self._cleanup_selected_on_success and not self._debug_media:
            cleanup_selected_frames(frame_uris)
        return UnitOutcome.SUCCEEDED
