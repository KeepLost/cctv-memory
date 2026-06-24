"""Tests for task cctv-memory-20260612-1854: VLM frame-extraction / near-EOF /
orphan-running fix.

Covers the acceptance criteria:
- zero usable frames near EOF => unit skipped(insufficient_frames) (not failed);
- some frames but fewer than requested => still sent to VLM (record produced);
- frame-extraction exception => unit failed(frame_extraction_failed), no stuck running;
- cross-scale near-EOF extraction failure => job finalizes (no stuck running),
  earlier successes preserved;
- sequential path does not roll back earlier successful units on a late failure;
- near-EOF window clamp (pure domain function);
- bounded, index-backed orphan-running recovery finalizes stuck units/scale/job;
- no recoverable_running state is introduced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cctv_memory.application.ingestion import IngestionService
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
)
from cctv_memory.domain.exceptions import InsufficientFramesError
from cctv_memory.domain.policies import (
    MotionSample,
    plan_high_freq_windows,
    plan_motion_triggers,
)
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.services.video_processor import VideoMetadata
from cctv_memory.workers.analysis_worker import AnalysisWorker

# ---------------------------------------------------------------------------
# Pure domain: near-EOF window clamp
# ---------------------------------------------------------------------------


def test_high_freq_windows_clamped_to_duration() -> None:
    # Trigger extends 1s past a 30s video; windows must not exceed duration.
    windows = plan_high_freq_windows(
        28_000, 31_000, window_seconds=3, overlap_ratio=0.5, duration_ms=30_000
    )
    assert windows, "expected at least one clamped window"
    assert all(w.end_ms <= 30_000 for w in windows)
    assert all(w.start_ms < w.end_ms for w in windows)


def test_high_freq_window_fully_past_eof_dropped() -> None:
    # A trigger starting at/after EOF yields no windows.
    assert (
        plan_high_freq_windows(
            31_000, 35_000, window_seconds=3, overlap_ratio=0.5, duration_ms=30_000
        )
        == []
    )


def test_motion_triggers_clamped_to_duration() -> None:
    samples = [MotionSample(t, 0.02) for t in range(0, 28_000, 1000)]
    samples += [MotionSample(28_000, 0.9), MotionSample(29_000, 0.95)]
    triggers = plan_motion_triggers(
        samples,
        threshold=0.5,
        min_duration_ms=3000,
        merge_gap_ms=800,
        duration_ms=30_000,
    )
    assert triggers
    assert all(t.end_ms <= 30_000 for t in triggers)
    assert all(t.start_ms < t.end_ms for t in triggers)


def test_motion_triggers_without_duration_unchanged() -> None:
    # Backwards-compatible: omitting duration_ms keeps the prior behavior.
    samples = [MotionSample(28_000, 0.9), MotionSample(29_000, 0.95)]
    triggers = plan_motion_triggers(
        samples, threshold=0.5, min_duration_ms=3000, merge_gap_ms=800
    )
    assert triggers and triggers[0].end_ms > 30_000


# ---------------------------------------------------------------------------
# Worker harness helpers
# ---------------------------------------------------------------------------


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s",
        dynamic="d",
        tags=["person"],
        quality={"reason": "", "score": 0.9},
        attr={"alert": False},
    )


class _Vlm:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def analyze_segment(self, request):  # type: ignore[no-untyped-def]
        self.calls.append(len(request.frame_uris))
        return _vlm_output()


class _ZeroFrameNearEofProcessor:
    """Probes fine (playable) but yields ZERO frames for the last window.

    Mirrors the OpenCV near-EOF zero-frame case: returns an empty list so the
    worker marks the unit skipped(insufficient_frames).
    """

    def __init__(self, duration_ms: int) -> None:
        self._duration_ms = duration_ms

    def probe(self, source_uri: str) -> VideoMetadata:
        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(self, source_uri, start_ms, end_ms, frame_count, *, unit_key=None):  # type: ignore[no-untyped-def]
        if end_ms >= self._duration_ms:
            return []  # zero usable frames near EOF
        return [f"/tmp/f/{start_ms}_{end_ms}/f{i}.jpg" for i in range(frame_count)]


class _InsufficientFramesProcessor:
    """Raises InsufficientFramesError for the last window (alternate zero-frame signal)."""

    def __init__(self, duration_ms: int) -> None:
        self._duration_ms = duration_ms

    def probe(self, source_uri: str) -> VideoMetadata:
        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(self, source_uri, start_ms, end_ms, frame_count, *, unit_key=None):  # type: ignore[no-untyped-def]
        if end_ms >= self._duration_ms:
            raise InsufficientFramesError("no frames near EOF")
        return [f"/tmp/f/{start_ms}_{end_ms}/f{i}.jpg" for i in range(frame_count)]


class _RaisingProcessor:
    """Raises a hard RuntimeError on frame extraction for the last window."""

    def __init__(self, duration_ms: int) -> None:
        self._duration_ms = duration_ms

    def probe(self, source_uri: str) -> VideoMetadata:
        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(self, source_uri, start_ms, end_ms, frame_count, *, unit_key=None):  # type: ignore[no-untyped-def]
        if end_ms >= self._duration_ms:
            raise RuntimeError("opencv re-seek failed to read selected frame")
        return [f"/tmp/f/{start_ms}_{end_ms}/f{i}.jpg" for i in range(frame_count)]


class _PartialFramesProcessor:
    """Returns FEWER frames than requested for every window (never zero)."""

    def __init__(self, duration_ms: int, *, n: int = 1) -> None:
        self._duration_ms = duration_ms
        self._n = n

    def probe(self, source_uri: str) -> VideoMetadata:
        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(self, source_uri, start_ms, end_ms, frame_count, *, unit_key=None):  # type: ignore[no-untyped-def]
        return [f"/tmp/f/{start_ms}_{end_ms}/f{i}.jpg" for i in range(self._n)]


def _seed(runtime, pid: str) -> None:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        repos.principal().create_principal(
            Principal(
                principal_id=pid,
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )


def _submit(runtime, pid: str, key: str) -> str:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        principal = repos.principal().get_principal(pid)
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 11, 15, 0, tzinfo=UTC),
                idempotency_key=key,
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


def _units_for_job(runtime, job_id: str):  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        scale_tasks = repos.scale_task().list_by_job(job_id)
        units = []
        for st in scale_tasks:
            units.extend(repos.analysis_unit().list_by_scale_task(st.scale_task_id))
        job = repos.analysis_job().get_job(job_id)
        return job, scale_tasks, units


# ---------------------------------------------------------------------------
# Per-unit terminal handling (cross-scale default path)
# ---------------------------------------------------------------------------


def test_zero_frames_near_eof_marks_unit_skipped(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_skip")
    job_id = _submit(runtime, "svc_skip", "k_skip")
    worker = AnalysisWorker(
        runtime,
        video_processor=_ZeroFrameNearEofProcessor(duration_ms=30_000),
        vlm=_Vlm(),
    )
    worker.process_one()

    job, scale_tasks, units = _units_for_job(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    skipped = [u for u in units if u.status is TaskStatus.SKIPPED]
    assert len(skipped) == 1
    assert skipped[0].last_error_code == "insufficient_frames"
    # earlier windows still succeeded; job is not failed.
    assert any(u.status is TaskStatus.SUCCEEDED for u in units)
    assert job.job_status in (JobStatus.SUCCEEDED, JobStatus.PARTIAL_FAILED)


def test_insufficient_frames_error_marks_unit_skipped(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_ife")
    job_id = _submit(runtime, "svc_ife", "k_ife")
    worker = AnalysisWorker(
        runtime,
        video_processor=_InsufficientFramesProcessor(duration_ms=30_000),
        vlm=_Vlm(),
    )
    worker.process_one()

    _job, _st, units = _units_for_job(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    assert any(
        u.status is TaskStatus.SKIPPED and u.last_error_code == "insufficient_frames"
        for u in units
    )


def test_extraction_exception_marks_unit_failed_no_stuck_running(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_fail")
    job_id = _submit(runtime, "svc_fail", "k_fail")
    worker = AnalysisWorker(
        runtime,
        video_processor=_RaisingProcessor(duration_ms=30_000),
        vlm=_Vlm(),
    )
    # The worker must NOT abort with an exception.
    assert worker.process_one() is not None

    job, scale_tasks, units = _units_for_job(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units), "unit stuck running"
    failed = [u for u in units if u.status is TaskStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].last_error_code == "frame_extraction_failed"
    # earlier units published; partial_failed, not stuck running.
    assert job.job_status is JobStatus.PARTIAL_FAILED
    assert all(st.status is not TaskStatus.RUNNING for st in scale_tasks)


def test_partial_frames_still_sent_to_vlm(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_partial")
    job_id = _submit(runtime, "svc_partial", "k_partial")
    vlm = _Vlm()
    worker = AnalysisWorker(
        runtime,
        # 1 frame per window though more are requested: must still call VLM.
        video_processor=_PartialFramesProcessor(duration_ms=30_000, n=1),
        vlm=vlm,
    )
    worker.process_one()

    job, _st, units = _units_for_job(runtime, job_id)
    assert vlm.calls and all(c == 1 for c in vlm.calls)
    assert all(u.status is TaskStatus.SUCCEEDED for u in units)
    assert job.job_status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Sequential path isolation (cross_scale disabled)
# ---------------------------------------------------------------------------


def test_sequential_path_preserves_earlier_successes(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    runtime.config.pipeline.cross_scale.enabled = False
    _seed(runtime, "svc_seq")
    job_id = _submit(runtime, "svc_seq", "k_seq")
    worker = AnalysisWorker(
        runtime,
        video_processor=_RaisingProcessor(duration_ms=30_000),
        vlm=_Vlm(),
    )
    assert worker.process_one() is not None

    job, scale_tasks, units = _units_for_job(runtime, job_id)
    # Earlier successful units must NOT be rolled back.
    assert any(u.status is TaskStatus.SUCCEEDED for u in units)
    assert any(
        u.status is TaskStatus.FAILED and u.last_error_code == "frame_extraction_failed"
        for u in units
    )
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    assert job.job_status is JobStatus.PARTIAL_FAILED


# ---------------------------------------------------------------------------
# Bounded, index-backed orphan-running recovery
# ---------------------------------------------------------------------------


def test_orphan_recovery_finalizes_stuck_units(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """A unit left RUNNING past the stale cutoff is terminalized failed(orphan_timeout)
    and its parent scale/job reconciled — no stuck running, no full scan."""
    runtime = runtime_factory()
    _seed(runtime, "svc_orphan")
    job_id = _submit(runtime, "svc_orphan", "k_orphan")
    # Run normally first so a job/scale/units exist and most units succeed.
    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    worker.process_one()

    # Inject an orphan: a unit stuck RUNNING with an old started_at, and reopen
    # its parent scale + job to RUNNING (simulating a crash mid-flight).
    job, scale_tasks, units = _units_for_job(runtime, job_id)
    stale_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    from cctv_memory.infrastructure.db.models import tables as orm

    target_unit = units[0]
    with runtime.session() as session:
        urow = session.get(orm.AnalysisUnit, target_unit.unit_id)
        urow.status = TaskStatus.RUNNING.value
        urow.started_at = stale_iso
        urow.finished_at = None
        srow = session.get(orm.AnalysisScaleTask, target_unit.scale_task_id)
        srow.status = TaskStatus.RUNNING.value
        jrow = session.get(orm.AnalysisJob, job_id)
        jrow.job_status = JobStatus.RUNNING.value

    recovered = worker.recover_orphans()
    assert recovered == 1

    job2, scale_tasks2, units2 = _units_for_job(runtime, job_id)
    swept = next(u for u in units2 if u.unit_id == target_unit.unit_id)
    assert swept.status is TaskStatus.FAILED
    assert swept.last_error_code == "orphan_timeout"
    assert not any(u.status is TaskStatus.RUNNING for u in units2)
    assert all(st.status is not TaskStatus.RUNNING for st in scale_tasks2)
    assert job2.job_status is not JobStatus.RUNNING


def test_orphan_recovery_ignores_fresh_running_units(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """A unit RUNNING but younger than the stale cutoff is NOT swept (bounded by
    stale-cutoff; a healthy in-flight unit is untouched)."""
    runtime = runtime_factory()
    _seed(runtime, "svc_fresh")
    job_id = _submit(runtime, "svc_fresh", "k_fresh")
    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    worker.process_one()

    _job, _st, units = _units_for_job(runtime, job_id)
    from cctv_memory.infrastructure.db.models import tables as orm

    with runtime.session() as session:
        urow = session.get(orm.AnalysisUnit, units[0].unit_id)
        urow.status = TaskStatus.RUNNING.value
        urow.started_at = datetime.now(UTC).isoformat()  # fresh
        urow.finished_at = None

    assert worker.recover_orphans() == 0


def test_orphan_recovery_respects_batch_limit(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """The sweep terminalizes at most orphan_batch_limit units per pass (bounded)."""
    runtime = runtime_factory()
    runtime.config.worker.orphan_batch_limit = 1
    _seed(runtime, "svc_batch")
    job_id = _submit(runtime, "svc_batch", "k_batch")
    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    worker.process_one()

    _job, _st, units = _units_for_job(runtime, job_id)
    assert len(units) >= 2, "need >=2 units to test the batch limit"
    stale_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    from cctv_memory.infrastructure.db.models import tables as orm

    with runtime.session() as session:
        for u in units:
            urow = session.get(orm.AnalysisUnit, u.unit_id)
            urow.status = TaskStatus.RUNNING.value
            urow.started_at = stale_iso
            urow.finished_at = None

    assert worker.recover_orphans() == 1  # capped at batch limit, not all units


def test_orphan_recovery_clean_db_is_noop(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_clean")
    _submit(runtime, "svc_clean", "k_clean")
    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    worker.process_one()
    assert worker.recover_orphans() == 0


def test_no_recoverable_running_state_exists() -> None:
    """Guard: this task must NOT introduce a recoverable_running status."""
    from cctv_memory.domain import enums

    for enum_cls in (enums.JobStatus, enums.TaskStatus):
        assert "recoverable_running" not in {e.value for e in enum_cls}


def test_orphan_recovery_runs_before_each_drain(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Task §B3: orphan recovery runs at the START of drain (contract §7.1
    "startup AND before each drain"), not only at process startup.

    A unit stranded RUNNING past the stale cutoff is reconciled by ``drain()``
    itself — without any explicit ``recover_orphans()`` call — proving the sweep
    is wired into the drain entry.
    """
    runtime = runtime_factory()
    _seed(runtime, "svc_drain_orphan")
    job_id = _submit(runtime, "svc_drain_orphan", "k_drain_orphan")
    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    worker.process_one()  # produce a finished job/units

    # Strand one unit RUNNING with a stale started_at, reopen its scale+job.
    job, _st, units = _units_for_job(runtime, job_id)
    stale_iso = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    from cctv_memory.infrastructure.db.models import tables as orm

    target = units[0]
    with runtime.session() as session:
        urow = session.get(orm.AnalysisUnit, target.unit_id)
        urow.status = TaskStatus.RUNNING.value
        urow.started_at = stale_iso
        urow.finished_at = None
        srow = session.get(orm.AnalysisScaleTask, target.scale_task_id)
        srow.status = TaskStatus.RUNNING.value
        jrow = session.get(orm.AnalysisJob, job_id)
        jrow.job_status = JobStatus.RUNNING.value

    # drain() with no claimable tasks must STILL run the pre-drain orphan sweep.
    worker.drain()

    _job2, _st2, units2 = _units_for_job(runtime, job_id)
    swept = next(u for u in units2 if u.unit_id == target.unit_id)
    assert swept.status is TaskStatus.FAILED
    assert swept.last_error_code == "orphan_timeout"
    assert not any(u.status is TaskStatus.RUNNING for u in units2)

