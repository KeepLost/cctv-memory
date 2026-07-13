"""high_freq_event processing path — per-unit publication and model-call logs."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cctv_memory.application.publication import PublicationService
from cctv_memory.contracts.analysis import AnalysisUnit, ModelCallLog
from cctv_memory.contracts.pre_vlm_gate import GateProfile
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain import policies
from cctv_memory.domain.enums import AnalysisScale, ModelCallStatus, TaskStatus
from cctv_memory.domain.exceptions import InsufficientFramesError, NotFoundError
from cctv_memory.infrastructure.vlm.prompts import prompt_version_for_scale
from cctv_memory.repositories.analysis import (
    AnalysisJobRepository,
    AnalysisUnitRepository,
    HighFreqTriggerRepository,
    ModelCallLogRepository,
)
from cctv_memory.repositories.camera import CameraRepository
from cctv_memory.repositories.principal import AccessPolicyRepository
from cctv_memory.repositories.video_source import VideoSourceRepository
from cctv_memory.services.timeline_recorder import TimelineRecorder
from cctv_memory.services.pre_vlm_gate import PreVlmGatePort
from cctv_memory.services.video_processor import VideoProcessorPort
from cctv_memory.services.vlm_analyzer import VlmAnalyzerPort
from cctv_memory.services.write_coordinator import (
    NO_OP_WRITE_COORDINATOR,
    WriteCoordinator,
)
from cctv_memory.workers.common import (
    DEFAULT_POLICY_ID,
    build_observation_record,
    new_id,
    resolve_video_context,
)
from cctv_memory.workers.cross_scale_scheduler import PlannedUnit
from cctv_memory.workers.debug_media import build_media_refs
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

_SCALE = AnalysisScale.HIGH_FREQ_EVENT

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class HighFreqEventProcessor:
    """Process the high_freq_event scale — per-unit publication + model-call logs."""

    def __init__(
        self,
        *,
        video_sources: VideoSourceRepository,
        jobs: AnalysisJobRepository,
        cameras: CameraRepository,
        policies_repo: AccessPolicyRepository,
        triggers: HighFreqTriggerRepository,
        video_processor: VideoProcessorPort,
        vlm: VlmAnalyzerPort,
        timeline: TimelineRecorder | None = None,
        publication: PublicationService,
        units: AnalysisUnitRepository,
        model_calls: ModelCallLogRepository,
        scale_task_id: str = "",
        provider: str = "mock",
        model_id: str = "mock-vlm-v1",
        pipeline_version: str = "pipeline-v1",
        window_seconds: int = 3,
        overlap_ratio: float = 0.5,
        frames_per_segment: int = 8,
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
        pre_vlm_gate: PreVlmGatePort | None = None,
        pre_vlm_gate_profile: GateProfile | None = None,
    ) -> None:
        self._video_sources = video_sources
        self._jobs = jobs
        self._cameras = cameras
        self._policies = policies_repo
        self._triggers = triggers
        self._video_processor = video_processor
        self._vlm = vlm
        self._timeline = timeline or TimelineRecorder.disabled()
        self._publication = publication
        self._units = units
        self._model_calls = model_calls
        self._scale_task_id = scale_task_id
        self._provider = provider
        self._model_id = model_id
        self._pipeline_version = pipeline_version
        self._window_seconds = window_seconds
        self._overlap_ratio = overlap_ratio
        self._frames_per_segment = frames_per_segment
        self._max_concurrent_requests = max(1, int(max_concurrent_requests))
        # Shared (global) VlmScheduler injected by the worker so provider limits are
        # GLOBAL across scales/units (Stage C1); local fallback preserves legacy use.
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
        # (constitution §7), not this worker. The concurrent path injects the
        # runtime's backend coordinator (SQLite single-writer serialization); the
        # legacy serial path uses a no-op default. VLM calls stay OUTSIDE
        # ``write_coordinator.write()`` (§9.1). Replaces the old worker-owned Lock.
        self._write = write_coordinator or NO_OP_WRITE_COORDINATOR
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=1)
        self._terminal_write_max_attempts = max(1, int(terminal_write_max_attempts))
        self._terminal_write_backoff_ms = max(0, int(terminal_write_backoff_ms))
        self._provider_options = dict(provider_options or {})
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
            analysis_scale=_SCALE,
            unit_kind="high_freq_event_window",
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
        job = self._jobs.get_job(analysis_job_id)
        if job is None:
            raise NotFoundError(f"job {analysis_job_id} not found")
        triggers = self._triggers.list_by_job(analysis_job_id)
        triggers = [t for t in triggers if t.video_id == video_id]
        if not triggers:
            return ScaleProcessResult(total=0, succeeded=0)

        ctx = resolve_video_context(
            video_id,
            video_sources=self._video_sources,
            cameras=self._cameras,
            policies_repo=self._policies,
            default_policy_id=self._default_policy_id,
        )
        prompt_version = prompt_version_for_scale(_SCALE)

        seen: set[tuple[int, int]] = set()
        planned: list[tuple[AnalysisUnit, int, int]] = []
        # Per-unit isolation when a runtime is injected: a late failure/skip cannot
        # roll back earlier committed successes (task cctv-memory-20260612-1854 §D).
        isolated = self._runtime is not None
        concurrent = self._max_concurrent_requests > 1 and self._runtime is not None

        for trig_idx, trigger in enumerate(triggers):
            windows = policies.plan_high_freq_windows(
                trigger.trigger_start_ms,
                trigger.trigger_end_ms,
                window_seconds=self._window_seconds,
                overlap_ratio=self._overlap_ratio,
                duration_ms=ctx.source.duration_ms,
            )
            for win_idx, window in enumerate(windows):
                key = (window.start_ms, window.end_ms)
                if key in seen:
                    continue
                seen.add(key)

                idem_key = (
                    f"{analysis_job_id}:{self._scale_task_id}"
                    f":high_freq_event:{window.start_ms}:{window.end_ms}"
                )
                unit = AnalysisUnit(
                    unit_id=new_id("unit"),
                    analysis_job_id=analysis_job_id,
                    scale_task_id=self._scale_task_id,
                    video_id=video_id,
                    analysis_scale=_SCALE,
                    unit_kind="high_freq_event_window",
                    segment_start_ms=window.start_ms,
                    segment_end_ms=window.end_ms,
                    window_index=trig_idx * 1000 + win_idx,
                    trigger_id=trigger.trigger_id,
                    idempotency_key=idem_key,
                )
                if not isolated:
                    unit = self._units.create_or_get_by_idempotency(unit)
                planned.append((unit, window.start_ms, window.end_ms))

        total = len(planned)
        succeeded = failed = skipped = 0

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
                        prompt_version,
                    )
                    for unit, start_ms, end_ms in planned
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
            if self._commit_before_concurrent is not None:
                self._commit_before_concurrent()
            for unit, start_ms, end_ms in planned:
                outcome = self._run_unit_in_fresh_session(
                    unit, start_ms, end_ms, analysis_job_id, video_id, ctx,
                    prompt_version,
                )
                if outcome is UnitOutcome.SUCCEEDED:
                    succeeded += 1
                elif outcome is UnitOutcome.SKIPPED:
                    skipped += 1
                else:
                    failed += 1
        else:
            for unit, start_ms, end_ms in planned:
                outcome = self._run_unit(
                    unit,
                    start_ms,
                    end_ms,
                    analysis_job_id,
                    video_id,
                    job,
                    ctx=ctx,
                    prompt_version=prompt_version,
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

        Reads this job's HighFreqTriggers (the hard motion_scan dependency: no
        triggers -> no units), plans short windows per trigger, and returns one
        ``PlannedUnit`` per unique window whose ``run`` executes the unit in its
        OWN fresh session. Idempotent + per-unit publication, so out-of-order
        completion across scales is safe.
        """
        assert self._runtime is not None, "plan_units requires an injected runtime"
        job = self._jobs.get_job(analysis_job_id)
        if job is None:
            raise NotFoundError(f"job {analysis_job_id} not found")
        triggers = self._triggers.list_by_job(analysis_job_id)
        triggers = [t for t in triggers if t.video_id == video_id]
        if not triggers:
            return []

        ctx = resolve_video_context(
            video_id,
            video_sources=self._video_sources,
            cameras=self._cameras,
            policies_repo=self._policies,
            default_policy_id=self._default_policy_id,
        )
        prompt_version = prompt_version_for_scale(_SCALE)

        seen: set[tuple[int, int]] = set()
        planned: list[PlannedUnit] = []
        for trig_idx, trigger in enumerate(triggers):
            windows = policies.plan_high_freq_windows(
                trigger.trigger_start_ms,
                trigger.trigger_end_ms,
                window_seconds=self._window_seconds,
                overlap_ratio=self._overlap_ratio,
                duration_ms=ctx.source.duration_ms,
            )
            for win_idx, window in enumerate(windows):
                key = (window.start_ms, window.end_ms)
                if key in seen:
                    continue
                seen.add(key)
                idem_key = (
                    f"{analysis_job_id}:{self._scale_task_id}"
                    f":high_freq_event:{window.start_ms}:{window.end_ms}"
                )
                unit = AnalysisUnit(
                    unit_id=new_id("unit"),
                    analysis_job_id=analysis_job_id,
                    scale_task_id=self._scale_task_id,
                    video_id=video_id,
                    analysis_scale=_SCALE,
                    unit_kind="high_freq_event_window",
                    segment_start_ms=window.start_ms,
                    segment_end_ms=window.end_ms,
                    window_index=trig_idx * 1000 + win_idx,
                    trigger_id=trigger.trigger_id,
                    idempotency_key=idem_key,
                )

                def _run(
                    u: AnalysisUnit = unit,
                    s: int = window.start_ms,
                    e: int = window.end_ms,
                    trigger_context: dict[str, object] = {
                        "trigger_id": trigger.trigger_id,
                        "trigger_reason": trigger.trigger_reason,
                        "motion_score": trigger.motion_score,
                        "change_score": trigger.change_score,
                    },
                ) -> UnitOutcome:
                    return self._run_unit_in_fresh_session(
                        u, s, e, analysis_job_id, video_id, ctx, prompt_version, trigger_context
                    )

                planned.append(PlannedUnit(scale=_SCALE, run=_run))
        return planned

    def _run_unit_in_fresh_session(
        self,
        unit_template: AnalysisUnit,
        start_ms: int,
        end_ms: int,
        analysis_job_id: str,
        video_id: str,
        ctx: VideoContext,
        prompt_version: str,
        trigger_context: dict[str, object] | None = None,
    ) -> UnitOutcome:
        """Run one unit with its own session. VLM runs concurrently; DB writes
        are serialized via the backend write coordinator (SQLite single-writer).

        Returns a ``UnitOutcome``; never leaves the unit ``running`` (frame
        extraction / media-ref building are terminalized; zero usable frames =>
        skipped(insufficient_frames)). Task cctv-memory-20260612-1854 §A.

        Lifecycle guard (task cctv-memory-20260616-1850 §B1): the post-running body
        runs under an outer try/except; any UNFORESEEN escape force-terminalizes the
        unit to FAILED with phase-tagged evidence so it can never stay ``running``.
        """
        assert self._runtime is not None

        mcall_id = new_id("mcall")
        with self._write.write(), self._runtime.session() as session:
            repos = self._runtime.repositories(session)
            job = repos.analysis_job().get_job(analysis_job_id)
            if job is None:
                return UnitOutcome.FAILED
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
                trigger_context=trigger_context,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort lifecycle guard
            self._force_terminalize_running(
                unit_id=unit_id,
                analysis_job_id=analysis_job_id,
                start_ms=start_ms,
                end_ms=end_ms,
                mcall_id=mcall_id,
                prompt_version=prompt_version,
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
        prompt_version: str,
        model_version: str | None,
        phase: UnitPhase,
        trigger_context: dict[str, object] | None = None,
    ) -> UnitOutcome:
        """Body of a running unit (frame select -> VLM -> publish). See guard above."""
        # Frame selection + media refs are terminalized (run after mark_running,
        # before the VLM call) so a raise here cannot strand the unit running.
        phase.name = "pre_vlm"
        try:
            with self._timeline.span(
                "frame_select",
                analysis_job_id=analysis_job_id,
                scale_task_id=self._scale_task_id,
                unit_id=unit_id,
                model_call_id=mcall_id,
                video_id=video_id,
                analysis_scale=_SCALE,
                unit_kind="high_freq_event_window",
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                metadata={"frames_requested": self._frames_per_segment},
            ):
                frame_selection = select_frames_for_unit(
                    self._video_processor,
                    ctx.source.source_uri,
                    start_ms,
                    end_ms,
                    self._frames_per_segment,
                    unit_key=mcall_id,
                )
            frame_uris = frame_selection.frame_uris
            if not frame_uris:
                self._terminalize_unit_skipped(
                    unit_id, analysis_job_id, start_ms, end_ms, mcall_id, prompt_version
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
                analysis_scale=_SCALE,
                unit_kind="high_freq_event_window",
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                metadata={"frame_count": len(frame_uris)},
            ):
                media_refs = build_media_refs(
                    frame_selection.media_refs_input,
                    model_call_id=mcall_id,
                    debug_media_retention=self._debug_media,
                    artifact_root=self._artifact_root,
                )
        except InsufficientFramesError:
            self._terminalize_unit_skipped(
                unit_id, analysis_job_id, start_ms, end_ms, mcall_id, prompt_version
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
                unit_id, analysis_job_id, start_ms, end_ms, mcall_id, prompt_version,
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

        gate_bundle = run_pre_vlm_gate(
            gate=self._pre_vlm_gate,
            profile=self._pre_vlm_gate_profile,
            media_refs_input=frame_selection.media_refs_input,
            analysis_job_id=analysis_job_id,
            scale_task_id=self._scale_task_id,
            unit_id=unit_id,
            video_id=video_id,
            analysis_scale=_SCALE,
            unit_kind="high_freq_event_window",
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            provider=(self._pre_vlm_gate_profile.provider if self._pre_vlm_gate_profile else ""),
            model_id=self._pre_vlm_gate_profile.model_id if self._pre_vlm_gate_profile else None,
            trigger_context=dict(trigger_context or {}),
        )
        if gate_bundle is not None and not gate_bundle.triggered_vlm:
            self._terminalize_unit_skipped(
                unit_id,
                analysis_job_id,
                start_ms,
                end_ms,
                mcall_id,
                prompt_version,
                skipped_reason="pre_vlm_gate_suppressed",
            )
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
            return UnitOutcome.SKIPPED

        vlm_request = VlmSegmentRequest(
            request_id=new_id("vlm_req"),
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            camera_id=ctx.source.camera_id,
            analysis_scale=_SCALE,
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
                            analysis_scale=_SCALE,
                            segment_start_ms=start_ms,
                            segment_end_ms=end_ms,
                            provider=self._provider,
                            model_id=self._model_id,
                            prompt_version=prompt_version,
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
            # Failed attempts already logged via _log_failed_attempt; just terminalize.
            def _w_fail() -> None:
                with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                    repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                    repos.analysis_unit().mark_failed(
                        unit_id,
                        error_code=error_code,
                        error_message=str(err)[:500],
                        model_call_id=mcall_id,
                        attempt_count=result.attempts,
                    )

            self._db_write(_w_fail)
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
                    analysis_scale=_SCALE,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    output=output,
                    model_version=job.model_version,
                    prompt_version=prompt_version,
                    pipeline_version=job.pipeline_version,
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
                        analysis_scale=_SCALE,
                        segment_start_ms=start_ms,
                        segment_end_ms=end_ms,
                        provider=self._provider,
                        model_id=self._model_id,
                        prompt_version=prompt_version,
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
        if self._cleanup_selected_on_success and not self._debug_media:
            cleanup_selected_frames(frame_uris)
        return UnitOutcome.SUCCEEDED

    def _force_terminalize_running(
        self,
        *,
        unit_id: str,
        analysis_job_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str,
        prompt_version: str,
        phase: str,
        exc: BaseException,
    ) -> None:
        """Last-resort terminalization for the lifecycle guard (task §B1).

        Reached only when an UNFORESEEN exception escaped the running-unit body.
        Writes a FAILED ModelCallLog with phase-tagged evidence and marks the unit
        FAILED (error_code ``analysis_unit_failed``), under the backend write
        coordinator with bounded retry. If even this fails, log loudly and rely on
        the bounded orphan/job reconciliation backstop (§7.1 / §B2); never claim
        success.
        """
        logger.error(
            "high_freq unit %s escaped running body at phase=%s: %s: %s",
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
                        analysis_scale=_SCALE,
                        segment_start_ms=start_ms,
                        segment_end_ms=end_ms,
                        provider=self._provider,
                        model_id=self._model_id,
                        prompt_version=prompt_version,
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
                "force-terminalize DB write failed for high_freq unit %s (phase=%s); "
                "relying on orphan/job reconciliation backstop",
                unit_id, phase,
            )

    def _terminalize_unit_failed(
        self,
        unit_id: str,
        analysis_job_id: str,
        start_ms: int,
        end_ms: int,
        mcall_id: str,
        prompt_version: str,
        *,
        error_code: str,
        exc: Exception,
    ) -> None:
        """Mark a unit FAILED + log a FAILED ModelCallLog (own serialized session).

        For frame-extraction/media-ref failures after mark_running and outside the
        VLM call, so the unit never stays ``running``.
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
                        analysis_scale=_SCALE,
                        segment_start_ms=start_ms,
                        segment_end_ms=end_ms,
                        provider=self._provider,
                        model_id=self._model_id,
                        prompt_version=prompt_version,
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
        prompt_version: str,
        skipped_reason: str = "insufficient_frames",
    ) -> None:
        """Mark a unit SKIPPED with the provided reason (own serialized session)."""

        def _w() -> None:
            with self._write.write(), self._runtime.session() as session:  # type: ignore[union-attr]
                repos = self._runtime.repositories(session)  # type: ignore[union-attr]
                repos.analysis_unit().mark_skipped(
                    unit_id,
                    skipped_reason=skipped_reason,
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
        prompt_version: str,
        publication,
        units,
        model_calls,
        commit_after_mark_running=None,
    ) -> UnitOutcome:
        if unit.status is TaskStatus.SUCCEEDED:
            return UnitOutcome.SUCCEEDED
        if unit.status is TaskStatus.SKIPPED:
            return UnitOutcome.SKIPPED

        mcall_id = new_id("mcall")
        units.mark_running(unit.unit_id, model_call_id=mcall_id)
        if commit_after_mark_running is not None:
            commit_after_mark_running()

        # Frame extraction + media refs terminalized (legacy shared-session path).
        try:
            frame_selection = select_frames_for_unit(
                self._video_processor,
                ctx.source.source_uri,
                start_ms,
                end_ms,
                self._frames_per_segment,
                unit_key=mcall_id,
            )
            frame_uris = frame_selection.frame_uris
            if not frame_uris:
                units.mark_skipped(
                    unit.unit_id,
                    skipped_reason="insufficient_frames",
                    model_call_id=mcall_id,
                )
                return UnitOutcome.SKIPPED
            media_refs = build_media_refs(
                frame_selection.media_refs_input,
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
                    analysis_scale=_SCALE,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    provider=self._provider,
                    model_id=self._model_id,
                    prompt_version=prompt_version,
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

        gate_bundle = run_pre_vlm_gate(
            gate=self._pre_vlm_gate,
            profile=self._pre_vlm_gate_profile,
            media_refs_input=frame_selection.media_refs_input,
            analysis_job_id=analysis_job_id,
            scale_task_id=self._scale_task_id,
            unit_id=unit.unit_id,
            video_id=video_id,
            analysis_scale=_SCALE,
            unit_kind="high_freq_event_window",
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            provider=(self._pre_vlm_gate_profile.provider if self._pre_vlm_gate_profile else ""),
            model_id=self._pre_vlm_gate_profile.model_id if self._pre_vlm_gate_profile else None,
        )
        if gate_bundle is not None and not gate_bundle.triggered_vlm:
            units.mark_skipped(
                unit.unit_id,
                skipped_reason="pre_vlm_gate_suppressed",
                model_call_id=mcall_id,
            )
            return UnitOutcome.SKIPPED

        vlm_request = VlmSegmentRequest(
            request_id=new_id("vlm_req"),
            analysis_job_id=analysis_job_id,
            video_id=video_id,
            camera_id=ctx.source.camera_id,
            analysis_scale=_SCALE,
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            frame_uris=frame_uris,
            prompt_version=prompt_version,
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
                    analysis_scale=_SCALE,
                    segment_start_ms=start_ms,
                    segment_end_ms=end_ms,
                    provider=self._provider,
                    model_id=self._model_id,
                    prompt_version=prompt_version,
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
            analysis_scale=_SCALE,
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            output=output,
            model_version=job.model_version,
            prompt_version=prompt_version,
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
                analysis_scale=_SCALE,
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                provider=self._provider,
                model_id=self._model_id,
                prompt_version=prompt_version,
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
