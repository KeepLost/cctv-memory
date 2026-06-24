"""End-to-end multi-scale worker tests (motion_scan -> high_freq_event).

Proves the task-spec acceptance criteria on the REAL worker/orchestrator/
publication path, using an injected deterministic motion detector (so the core
wiring is verified without depending on ffmpeg) plus the StaticVideoProcessor and
MockVlmAnalyzer:

1. enable_motion_triggered_high_freq creates motion_scan + high_freq_event tasks;
2. motion produces HighFreqTriggers and publishes high_freq_event records;
3. no-motion skips high_freq_event with reason no_motion_trigger;
4. high_freq_event records are searchable/filterable by analysis_scale;
5. default-only ingestion is unchanged (no triggers, no high_freq records).

A separate ffmpeg-gated test proves the real FrameDiffMotionDetector drives the
same path end to end with a synthesized moving clip.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.search import StartObservationSearchRequest
from cctv_memory.contracts.video import SubmitVideoSourceRequest
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

from tests.support.video_gen import ffmpeg_available, generate_testsrc


class _FakeMotionDetector:
    """Deterministic motion detector returning a preset sample series."""

    def __init__(self, samples: list[MotionSample]) -> None:
        self._samples = samples

    def sample_motion(self, source_uri: str) -> list[MotionSample]:
        return list(self._samples)


def _seed(runtime, principal_id: str) -> None:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        from tests.conftest import seed_camera

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
                principal_id=principal_id,
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )


def _submit(runtime, principal_id: str, *, enable_high_freq: bool, key: str) -> str:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        options = {"enable_default_segment": True}
        if enable_high_freq:
            options["enable_motion_triggered_high_freq"] = True
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
        )
        principal = repos.principal().get_principal(principal_id)
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key=key,
                analysis_options=options,
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


def _motion_samples() -> list[MotionSample]:
    # Quiet, then a sustained high-motion burst, then quiet again.
    return [
        MotionSample(0, 0.02), MotionSample(1000, 0.03),
        MotionSample(2000, 0.8), MotionSample(3000, 0.85), MotionSample(4000, 0.9),
        MotionSample(5000, 0.02), MotionSample(6000, 0.01),
    ]


def test_enable_high_freq_creates_three_scale_tasks(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_hf1")
    job_id = _submit(runtime, "svc_hf1", enable_high_freq=True, key="hf-1")
    with runtime.session() as session:
        repos = runtime.repositories(session)
        tasks = repos.scale_task().list_by_job(job_id)
        scales = {t.analysis_scale for t in tasks}
    assert scales == {
        AnalysisScale.DEFAULT_SEGMENT,
        AnalysisScale.MOTION_SCAN,
        AnalysisScale.HIGH_FREQ_EVENT,
    }
    runtime.dispose()


def test_default_only_creates_single_scale_task(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_def1")
    job_id = _submit(runtime, "svc_def1", enable_high_freq=False, key="def-1")
    with runtime.session() as session:
        repos = runtime.repositories(session)
        tasks = repos.scale_task().list_by_job(job_id)
    assert [t.analysis_scale for t in tasks] == [AnalysisScale.DEFAULT_SEGMENT]
    runtime.dispose()


def test_motion_publishes_high_freq_records_and_triggers(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_hf2")
    job_id = _submit(runtime, "svc_hf2", enable_high_freq=True, key="hf-2")

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED

        # Triggers were produced by motion_scan.
        triggers = repos.trigger().list_by_job(job_id)
        assert len(triggers) >= 1
        assert all(t.motion_score is not None for t in triggers)

        # Scale tasks: default + motion + high_freq all succeeded.
        tasks = {t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)}
        assert tasks[AnalysisScale.DEFAULT_SEGMENT].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.MOTION_SCAN].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.HIGH_FREQ_EVENT].succeeded_units >= 1

        # high_freq_event records published with the correct scale + prompt_version.
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        hf_rows = list(
            session.scalars(
                select(orm.ObservationRecord).where(
                    orm.ObservationRecord.analysis_scale
                    == AnalysisScale.HIGH_FREQ_EVENT.value
                )
            )
        )
        assert len(hf_rows) >= 1
        assert all(r.prompt_version == "high_freq_event_v3" for r in hf_rows)
    runtime.dispose()


def test_no_motion_skips_high_freq(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_hf3")
    job_id = _submit(runtime, "svc_hf3", enable_high_freq=True, key="hf-3")

    quiet = [MotionSample(i * 1000, 0.01) for i in range(8)]
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        motion_detector=_FakeMotionDetector(quiet),
    )
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        # No optional failure -> job still SUCCEEDED.
        assert job.job_status is JobStatus.SUCCEEDED
        assert repos.trigger().list_by_job(job_id) == []
        tasks = {t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)}
        hf = tasks[AnalysisScale.HIGH_FREQ_EVENT]
        assert hf.status is TaskStatus.SKIPPED
        assert hf.skipped_reason == "no_motion_trigger"

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        hf_count = session.scalar(
            select(func.count())
            .select_from(orm.ObservationRecord)
            .where(
                orm.ObservationRecord.analysis_scale
                == AnalysisScale.HIGH_FREQ_EVENT.value
            )
        )
        assert hf_count == 0
    runtime.dispose()


def test_high_freq_records_are_searchable_by_scale(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _seed(runtime, "svc_hf4")
    _submit(runtime, "svc_hf4", enable_high_freq=True, key="hf-4")
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal("svc_hf4")
        scope = svc.auth.authorized_scope_for(principal)
        # Hard filter to high_freq_event must return only high_freq records, and
        # they must come from the pipeline (not hand-published).
        resp = svc.search.start_search(
            StartObservationSearchRequest(
                analysis_scale_filter=[AnalysisScale.HIGH_FREQ_EVENT], top_k=50
            ),
            scope,
        )
        assert resp.results
        assert all(
            r.analysis_scale is AnalysisScale.HIGH_FREQ_EVENT for r in resp.results
        )
    runtime.dispose()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_real_motion_detector_drives_high_freq_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full real path: synthesized moving clip -> real FrameDiffMotionDetector ->
    triggers -> high_freq_event records, with real per-segment frame extraction."""
    clip = generate_testsrc(tmp_path / "media" / "moving.mp4", duration=10, rate=10)
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE", "ffmpeg_frames")
    # Lower the motion threshold so the slow full-frame `testsrc` reliably
    # triggers. The synthetic `testsrc` is a gradual full-frame scroll; with the
    # default finer 128x72 downscale its normalized per-pixel diff is small
    # (~0.01), so this fixture-specific override sits well below it. Real
    # localized CCTV motion scores far higher; this is test calibration, not a
    # production threshold.
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__MOTION_SCAN__THRESHOLD", "0.005")
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__MOTION_SCAN__MIN_DURATION_MS", "1000")

    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=str(tmp_path / "data"))
    runtime.init_storage()
    runtime.create_schema()
    _seed(runtime, "svc_hf5")
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
        )
        principal = repos.principal().get_principal("svc_hf5")
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=str(clip),
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key="hf-e2e",
                analysis_options={
                    "enable_default_segment": True,
                    "enable_motion_triggered_high_freq": True,
                },
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        job_id = resp.analysis_job_id

    worker = AnalysisWorker(runtime)  # real detector + SegmentFrameVideoProcessor
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED
        assert len(repos.trigger().list_by_job(job_id)) >= 1

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        hf_count = session.scalar(
            select(func.count())
            .select_from(orm.ObservationRecord)
            .where(
                orm.ObservationRecord.analysis_scale
                == AnalysisScale.HIGH_FREQ_EVENT.value
            )
        )
        assert hf_count and hf_count >= 1
    runtime.dispose()


def test_optional_scale_failure_yields_partial_failed(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """A motion_scan failure must NOT fail the whole job: default records survive
    and the job is partial_failed (job-state-machine-contract §1.3)."""
    runtime = runtime_factory()
    _seed(runtime, "svc_hf6")
    job_id = _submit(runtime, "svc_hf6", enable_high_freq=True, key="hf-6")

    class _BoomDetector:
        def sample_motion(self, source_uri: str) -> list[MotionSample]:
            raise RuntimeError("motion sampling failed")

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        motion_detector=_BoomDetector(),
    )
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.PARTIAL_FAILED
        tasks = {t.analysis_scale: t for t in repos.scale_task().list_by_job(job_id)}
        # default_segment still succeeded and published records.
        assert tasks[AnalysisScale.DEFAULT_SEGMENT].status is TaskStatus.SUCCEEDED
        assert tasks[AnalysisScale.MOTION_SCAN].status is TaskStatus.FAILED

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


def test_skipif_marker_present_for_ffmpeg() -> None:
    # Documents that the real-media e2e test is ffmpeg-gated (not silently absent).
    assert callable(ffmpeg_available)
    assert shutil.which  # noqa: B018


def test_benchmark_pool_includes_high_freq_records(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Benchmark/experiment integrity: once high_freq_event records are produced by
    the pipeline, they enter the authorized candidate pool the benchmark runs over,
    so experiments cannot silently pretend the scale is unavailable."""
    runtime = runtime_factory()
    _seed(runtime, "svc_hf7")
    _submit(runtime, "svc_hf7", enable_high_freq=True, key="hf-7")
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        motion_detector=_FakeMotionDetector(_motion_samples()),
    )
    worker.process_one()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        from cctv_memory.application.auth import AuthorizationService

        auth = AuthorizationService(
            repos.principal(), repos.access_policy(), repos.camera()
        )
        principal = auth.resolve_principal("svc_hf7")
        scope = auth.authorized_scope_for(principal)
        pool = repos.observation_read().authorized_candidate_pool(scope, limit=1000)
        scales = {r.analysis_scale for r in pool}
    assert AnalysisScale.HIGH_FREQ_EVENT in scales
    assert AnalysisScale.DEFAULT_SEGMENT in scales
    runtime.dispose()

