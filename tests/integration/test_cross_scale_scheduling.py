"""Stage C2 — cross-scale unit scheduling tests.

Covers:
- pure dispatch_order priority + anti-starvation determinism;
- high_freq not scheduled before motion_scan triggers exist;
- after triggers, high_freq units are prioritized ahead of default units;
- default_segment is never starved;
- out-of-order unit completion still finalizes scale + job correctly;
- partial_failed semantics with mixed default/high_freq successes/failures;
- rerun idempotency: no duplicate units or publications;
- selected-frame cleanup/debug retention/model-call refs intact under cross-scale;
- global concurrency cap holds when default + high_freq genuinely overlap.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from cctv_memory.application.ingestion import IngestionService
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
)
from cctv_memory.domain.policies import MotionSample
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.workers.analysis_worker import AnalysisWorker
from cctv_memory.workers.cross_scale_scheduler import (
    CrossScaleUnitScheduler,
    PlannedUnit,
    dispatch_order,
)
from cctv_memory.workers.unit_result import UnitOutcome

from tests.conftest import seed_camera

# ---------------------------------------------------------------------------
# Pure dispatch-order policy
# ---------------------------------------------------------------------------


def _pu(scale: AnalysisScale, tag: str) -> PlannedUnit:
    return PlannedUnit(scale=scale, run=lambda: True)  # noqa: ARG005


def test_dispatch_order_prioritizes_high_freq_with_quota() -> None:
    hf = [_pu(AnalysisScale.HIGH_FREQ_EVENT, f"h{i}") for i in range(5)]
    df = [_pu(AnalysisScale.DEFAULT_SEGMENT, f"d{i}") for i in range(5)]
    order = dispatch_order(hf, df, high_freq_quota=2)
    scales = [u.scale for u in order]
    H, D = AnalysisScale.HIGH_FREQ_EVENT, AnalysisScale.DEFAULT_SEGMENT
    # quota=2 -> HH D HH D H ... then drain remaining default.
    assert scales == [H, H, D, H, H, D, H, D, D, D]
    assert scales.count(H) == 5
    assert scales.count(D) == 5


def test_dispatch_order_no_starvation_default_always_appears() -> None:
    hf = [_pu(AnalysisScale.HIGH_FREQ_EVENT, f"h{i}") for i in range(20)]
    df = [_pu(AnalysisScale.DEFAULT_SEGMENT, f"d{i}") for i in range(2)]
    order = dispatch_order(hf, df, high_freq_quota=3)
    scales = [u.scale for u in order]
    # Both default units must be dispatched within the first few slots, not last.
    first_default = scales.index(AnalysisScale.DEFAULT_SEGMENT)
    assert first_default <= 3, "default starved: appears too late"
    assert scales.count(AnalysisScale.DEFAULT_SEGMENT) == 2
    assert scales.count(AnalysisScale.HIGH_FREQ_EVENT) == 20


def test_dispatch_order_handles_empty_sides() -> None:
    df = [_pu(AnalysisScale.DEFAULT_SEGMENT, "d0")]
    assert [u.scale for u in dispatch_order([], df, high_freq_quota=3)] == [
        AnalysisScale.DEFAULT_SEGMENT
    ]
    hf = [_pu(AnalysisScale.HIGH_FREQ_EVENT, "h0")]
    assert [u.scale for u in dispatch_order(hf, [], high_freq_quota=3)] == [
        AnalysisScale.HIGH_FREQ_EVENT
    ]
    assert dispatch_order([], [], high_freq_quota=3) == []


def test_cross_scale_scheduler_serial_runs_in_priority_order() -> None:
    seen: list[AnalysisScale] = []

    def mk(scale: AnalysisScale) -> PlannedUnit:
        def _run() -> UnitOutcome:
            seen.append(scale)
            return UnitOutcome.SUCCEEDED

        return PlannedUnit(scale=scale, run=_run)

    hf = [mk(AnalysisScale.HIGH_FREQ_EVENT) for _ in range(3)]
    df = [mk(AnalysisScale.DEFAULT_SEGMENT) for _ in range(3)]
    sched = CrossScaleUnitScheduler(max_workers=1, high_freq_quota=2)
    results = sched.run(high_freq_units=hf, default_units=df)
    H, D = AnalysisScale.HIGH_FREQ_EVENT, AnalysisScale.DEFAULT_SEGMENT
    assert seen == [H, H, D, H, D, D]
    assert results[H].succeeded == 3 and results[H].failed == 0
    assert results[D].succeeded == 3 and results[D].failed == 0


def test_cross_scale_scheduler_counts_failures_per_scale() -> None:
    def ok(scale: AnalysisScale) -> PlannedUnit:
        return PlannedUnit(scale=scale, run=lambda: UnitOutcome.SUCCEEDED)  # noqa: ARG005

    def bad(scale: AnalysisScale) -> PlannedUnit:
        return PlannedUnit(scale=scale, run=lambda: UnitOutcome.FAILED)  # noqa: ARG005

    hf = [ok(AnalysisScale.HIGH_FREQ_EVENT), bad(AnalysisScale.HIGH_FREQ_EVENT)]
    df = [ok(AnalysisScale.DEFAULT_SEGMENT)]
    results = CrossScaleUnitScheduler(max_workers=1).run(
        high_freq_units=hf, default_units=df
    )
    assert results[AnalysisScale.HIGH_FREQ_EVENT].succeeded == 1
    assert results[AnalysisScale.HIGH_FREQ_EVENT].failed == 1
    assert results[AnalysisScale.DEFAULT_SEGMENT].succeeded == 1


def test_cross_scale_scheduler_counts_skipped_and_guards_unexpected_raise() -> None:
    """A unit returning SKIPPED is tallied as skipped (not failed); a unit that
    unexpectedly RAISES is counted as a failed unit (defensive guard) and never
    strands the scheduler."""

    def skip(scale: AnalysisScale) -> PlannedUnit:
        return PlannedUnit(scale=scale, run=lambda: UnitOutcome.SKIPPED)  # noqa: ARG005

    def boom(scale: AnalysisScale) -> PlannedUnit:
        def _run() -> UnitOutcome:
            raise RuntimeError("unexpected escape")

        return PlannedUnit(scale=scale, run=_run)

    hf = [skip(AnalysisScale.HIGH_FREQ_EVENT), boom(AnalysisScale.HIGH_FREQ_EVENT)]
    df = [PlannedUnit(scale=AnalysisScale.DEFAULT_SEGMENT, run=lambda: UnitOutcome.SUCCEEDED)]  # noqa: ARG005
    results = CrossScaleUnitScheduler(max_workers=1).run(
        high_freq_units=hf, default_units=df
    )
    hf_res = results[AnalysisScale.HIGH_FREQ_EVENT]
    assert hf_res.skipped == 1
    assert hf_res.failed == 1  # the unexpected raise counted as failed, not stranded
    assert hf_res.succeeded == 0
    assert results[AnalysisScale.DEFAULT_SEGMENT].succeeded == 1


# ---------------------------------------------------------------------------
# End-to-end worker integration helpers
# ---------------------------------------------------------------------------


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s",
        dynamic="d",
        tags=["person"],
        quality={"reason": "", "score": 0.9},
        attr={"alert": False},
    )


def _seed(runtime, pid: str) -> None:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_camera(repos)
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


def _submit(runtime, pid: str, *, enable_high_freq: bool, key: str) -> str:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        options = {"enable_default_segment": True}
        if enable_high_freq:
            options["enable_motion_triggered_high_freq"] = True
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
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
                analysis_options=options,
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


class _FakeMotionDetector:
    def __init__(self, samples: list[MotionSample]) -> None:
        self._samples = samples

    def sample_motion(self, source_uri: str) -> list[MotionSample]:
        return list(self._samples)


def _motion_samples() -> list[MotionSample]:
    return [
        MotionSample(0, 0.02), MotionSample(1000, 0.03),
        MotionSample(2000, 0.8), MotionSample(3000, 0.85), MotionSample(4000, 0.9),
        MotionSample(5000, 0.02), MotionSample(6000, 0.01),
    ]


# ---------------------------------------------------------------------------
# Cross-scale main-path behavior
# ---------------------------------------------------------------------------


def test_high_freq_not_scheduled_without_triggers(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """No motion triggers -> high_freq scale SKIPPED(no_motion_trigger), no
    high_freq units/records, default still succeeds."""
    runtime = runtime_factory()
    _seed(runtime, "svc_c2_notrig")
    job_id = _submit(runtime, "svc_c2_notrig", enable_high_freq=True, key="c2-notrig")

    quiet = [MotionSample(i * 1000, 0.01) for i in range(8)]
    captured: list[AnalysisScale] = []

    class _Probe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            captured.append(request.analysis_scale)
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        vlm=_Probe(),
        motion_detector=_FakeMotionDetector(quiet),
    )
    worker.process_one()

    assert AnalysisScale.HIGH_FREQ_EVENT not in captured
    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None and job.job_status is JobStatus.SUCCEEDED
        tasks = {t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)}
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].status is TaskStatus.SKIPPED
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].skipped_reason == "no_motion_trigger"
    runtime.dispose()


def test_high_freq_prioritized_after_triggers(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """After motion triggers exist, high_freq units are dispatched ahead of
    default_segment units (serial mode = strict priority order)."""
    runtime = runtime_factory()
    runtime.config.vlm.max_concurrent_requests = 1
    runtime.config.pipeline.cross_scale.high_freq_quota = 3
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 1
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_c2_prio")
    _submit(runtime, "svc_c2_prio", enable_high_freq=True, key="c2-prio")

    order: list[AnalysisScale] = []
    lock = threading.Lock()

    class _Probe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            with lock:
                order.append(request.analysis_scale)
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_Probe(),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    assert AnalysisScale.HIGH_FREQ_EVENT in order
    assert AnalysisScale.DEFAULT_SEGMENT in order
    # The very first dispatched unit must be high_freq (priority).
    assert order[0] is AnalysisScale.HIGH_FREQ_EVENT
    # default is not starved: it still runs.
    assert order.count(AnalysisScale.DEFAULT_SEGMENT) >= 1
    runtime.dispose()


def test_out_of_order_completion_finalizes_correctly(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """With concurrency, units complete out of order across scales; both scales
    and the job must still finalize SUCCEEDED with correct unit counts."""
    runtime = runtime_factory()
    runtime.config.vlm.max_concurrent_requests = 4
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 1
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_c2_ooo")
    job_id = _submit(runtime, "svc_c2_ooo", enable_high_freq=True, key="c2-ooo")

    class _JitterVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            # high_freq finishes slower so it completes after later-started default.
            if request.analysis_scale is AnalysisScale.HIGH_FREQ_EVENT:
                time.sleep(0.03)
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_JitterVlm(),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None and job.job_status is JobStatus.SUCCEEDED
        tasks = {t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)}
        assert tasks[AnalysisScale.DEFAULT_SEGMENT].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.DEFAULT_SEGMENT].succeeded_units >= 1
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].succeeded_units >= 1
    runtime.dispose()


def test_partial_failed_with_mixed_scale_failures(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Some high_freq units fail after default units published -> high_freq scale
    partial_failed/failed, job partial_failed, default records survive."""
    runtime = runtime_factory()
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 1
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_c2_partial")
    job_id = _submit(runtime, "svc_c2_partial", enable_high_freq=True, key="c2-partial")

    class _HfFailsVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            if request.analysis_scale is AnalysisScale.HIGH_FREQ_EVENT:
                raise RuntimeError("provider boom on high_freq")
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_HfFailsVlm(),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.PARTIAL_FAILED
        tasks = {t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)}
        assert tasks[AnalysisScale.DEFAULT_SEGMENT].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].status in (
            TaskStatus.FAILED,
            TaskStatus.PARTIAL_FAILED,
        )

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        default_count = session.scalar(
            select(func.count())
            .select_from(orm.ObservationRecord)
            .where(
                orm.ObservationRecord.analysis_scale
                == AnalysisScale.DEFAULT_SEGMENT.value
            )
        )
        assert default_count and default_count >= 1
    runtime.dispose()


def test_rerun_does_not_duplicate_units_or_publications(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Re-planning + re-running the same job's units (idempotent units) must not
    duplicate analysis units or active ObservationRecords."""
    from cctv_memory.infrastructure.db.models import tables as orm
    from sqlalchemy import func, select

    runtime = runtime_factory()
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 1
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_c2_rerun")
    job_id = _submit(runtime, "svc_c2_rerun", enable_high_freq=True, key="c2-rerun")

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    def _counts() -> tuple[int, int]:
        with runtime.session() as session:
            units = session.scalar(
                select(func.count()).select_from(orm.AnalysisUnit)
            )
            recs = session.scalar(
                select(func.count()).select_from(orm.ObservationRecord)
            )
        return int(units or 0), int(recs or 0)

    units_first, recs_first = _counts()
    assert units_first >= 2 and recs_first >= 2

    # Re-plan + re-run every unit for both scales (simulates a crash-recovery
    # rerun). create_or_get_by_idempotency must short-circuit already-succeeded
    # units, so no new units/records appear.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        scale_tasks = {
            t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)
        }
        dproc = worker._build_default_processor(  # type: ignore[attr-defined]
            repos, job_id, scale_tasks[AnalysisScale.DEFAULT_SEGMENT].scale_task_id
        )
        video_id = repos.analysis_job().get_job(job_id).video_id  # type: ignore[union-attr]
    default_units = dproc.plan_units(job_id, video_id)
    with runtime.session() as session:
        repos = runtime.repositories(session)
        hproc = worker._build_high_freq_processor(  # type: ignore[attr-defined]
            repos, job_id, scale_tasks[AnalysisScale.HIGH_FREQ_EVENT].scale_task_id
        )
    high_freq_units = hproc.plan_units(job_id, video_id)
    for u in default_units + high_freq_units:
        u.run()

    units_second, recs_second = _counts()
    assert units_second == units_first, "rerun duplicated analysis units"
    assert recs_second == recs_first, "rerun duplicated observation records"
    runtime.dispose()


def test_metadata_only_no_base64_under_cross_scale(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Under cross-scale scheduling, ModelCallLog media_refs must still contain no
    base64/source_uri for either scale (security regression guard)."""
    import json as _json

    runtime = runtime_factory()
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 1
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_c2_b64")
    _submit(runtime, "svc_c2_b64", enable_high_freq=True, key="c2-b64")

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    with runtime.session() as session:
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        logs = list(session.scalars(select(orm.ModelCallLog)))
        assert logs
        for log in logs:
            for ref in _json.loads(log.media_refs_json):
                ref_str = _json.dumps(ref).lower()
                assert "base64" not in ref_str
                assert "source_uri" not in ref_str
    runtime.dispose()


def test_all_default_units_fail_yields_failed_job(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """If every default_segment unit fails (no record published), the REQUIRED
    scale total-failure must fail the whole job (job-state-machine §1.3/§1.4)."""
    runtime = runtime_factory()
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    _seed(runtime, "svc_c2_allfail")
    job_id = _submit(runtime, "svc_c2_allfail", enable_high_freq=False, key="c2-allfail")

    class _AlwaysFailVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_AlwaysFailVlm(),
    )
    worker.process_one()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.FAILED
    runtime.dispose()


def test_global_concurrency_cap_with_real_overlap(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """When default + high_freq units genuinely overlap under cross-scale
    dispatch, the GLOBAL VLM concurrency never exceeds max_concurrent_requests."""
    runtime = runtime_factory()
    runtime.config.vlm.max_concurrent_requests = 2
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 1
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_c2_cap")
    _submit(runtime, "svc_c2_cap", enable_high_freq=True, key="c2-cap")

    peak = 0
    current = 0
    scales_seen: set = set()
    lock = threading.Lock()

    class _Probe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
                scales_seen.add(request.analysis_scale)
            time.sleep(0.02)
            with lock:
                current -= 1
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_Probe(),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    assert peak <= 2, f"global peak {peak} exceeded cap 2 under cross-scale overlap"
    assert AnalysisScale.DEFAULT_SEGMENT in scales_seen
    assert AnalysisScale.HIGH_FREQ_EVENT in scales_seen
    runtime.dispose()
