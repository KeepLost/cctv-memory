"""Multi-job concurrency tests (task cctv-memory-20260615-1620).

Staged guard/diagnostic tests driving the multi-job worker pool implementation:

Stage 0/1 — atomic task claim:
  - concurrent claim of the same queued task yields exactly one winner;
  - existing lease/expiry/priority semantics preserved.

Stage 2 — config + drain refactor:
  - default config remains serial;
  - per-job unit concurrency is controlled only by ``max_unit_workers_per_job``.

Stage 3 — multi-job processing + global VLM cap:
  - multiple queued one-unit jobs process concurrently when max_concurrent_jobs>1;
  - provider calls from different jobs overlap when the global cap permits;
  - global in-flight VLM calls never exceed ``vlm.max_concurrent_requests`` across
    all jobs/units/retries;
  - the per-job unit worker limit does not multiply the global provider cap.

Stage 4 — failure isolation:
  - one job failing does not prevent other concurrent jobs from succeeding;
  - publication remains exactly-once (no duplicate active ObservationRecord).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.task import Task
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.workers.analysis_worker import AnalysisWorker

from tests.conftest import dt_in, iso_in, seed_camera

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mj_runtime(tmp_path: object) -> Iterator[Runtime]:
    """Static-mode runtime (no ffprobe/subprocess) with schema + seed."""
    config = AppConfig().with_data_dir(str(tmp_path))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.static_duration_ms = 8_000
    # One default_segment window per video => one VLM unit per job (the
    # "many short one-unit videos" scenario that motivates multi-job concurrency).
    config.pipeline.default_segment.window_seconds = 30
    config.pipeline.default_segment.overlap_seconds = 0
    runtime = Runtime(config)
    runtime.init_storage()
    runtime.create_schema()
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
                principal_id="svc_mj",
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )
    yield runtime
    runtime.dispose()


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s",
        dynamic="d",
        tags=["person"],
        quality={"reason": "", "score": 0.9},
        attr={"alert": False},
    )


def _submit_job(runtime: Runtime, *, key: str, minute: int = 0) -> str:
    """Submit one analyze job (one video => one default_segment unit).

    Each job uses a distinct ``video_start_time`` so the (camera_id,
    video_start_time) idempotency key is unique per job.
    """
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        principal = repos.principal().get_principal("svc_mj")
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=f"/data/videos/{key}.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 15, 16, minute % 60, tzinfo=UTC),
                idempotency_key=key,
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


# ---------------------------------------------------------------------------
# Stage 0/1 — atomic task claim
# ---------------------------------------------------------------------------


def test_concurrent_claim_same_task_single_winner(mj_runtime: Runtime) -> None:
    """Many workers claim concurrently against ONE queued task: exactly one wins.

    This is the core correctness guard for multi-job concurrency: the old
    select-then-update claim could let two workers claim the same row.
    """
    runtime = mj_runtime
    # Enqueue exactly one task directly into the queue.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        repos.task_queue().enqueue_task(
            Task(
                task_id="task_solo",
                task_type="analyze_video",
                payload={"analysis_job_id": "j", "video_id": "v"},
                status="queued",
                next_run_at=iso_in(-5),
            )
        )

    winners: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _claim() -> None:
        barrier.wait()
        with runtime.session() as session:
            repos = runtime.repositories(session)
            claimed = repos.task_queue().claim_task(
                f"w-{threading.get_ident()}", now=dt_in(0), lease_seconds=30
            )
        if claimed is not None:
            with lock:
                winners.append(claimed.task_id)

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert winners == ["task_solo"], f"expected exactly one winner, got {winners}"


def test_concurrent_claim_no_task_claimed_twice(mj_runtime: Runtime) -> None:
    """N tasks, 2N concurrent workers: no task is ever claimed by two workers.

    Stronger contention guard than the single-task case. The atomic conditional
    UPDATE must guarantee each task has at most one claimant even under heavy
    concurrent contention.
    """
    runtime = mj_runtime
    n = 12
    with runtime.session() as session:
        repos = runtime.repositories(session)
        for i in range(n):
            repos.task_queue().enqueue_task(
                Task(
                    task_id=f"task_{i}",
                    task_type="analyze_video",
                    payload={"analysis_job_id": "j", "video_id": "v"},
                    status="queued",
                    next_run_at=iso_in(-5),
                )
            )

    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2 * n)

    def _claim() -> None:
        barrier.wait()
        with runtime.session() as session:
            repos = runtime.repositories(session)
            task = repos.task_queue().claim_task(
                f"w-{threading.get_ident()}", now=dt_in(0), lease_seconds=60
            )
        if task is not None:
            with lock:
                claimed.append(task.task_id)

    threads = [threading.Thread(target=_claim) for _ in range(2 * n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == len(set(claimed)), (
        f"a task was claimed more than once: {sorted(claimed)}"
    )
    assert len(claimed) == n, f"expected all {n} tasks claimed once, got {len(claimed)}"


def test_claim_preserves_priority_and_expiry(mj_runtime: Runtime) -> None:
    """Atomic claim still honors priority ordering and lease-expiry reclaim."""
    runtime = mj_runtime
    with runtime.session() as session:
        repos = runtime.repositories(session)
        q = repos.task_queue()
        q.enqueue_task(
            Task(task_id="lo", task_type="analyze_video", payload={}, status="queued",
                 priority=1, next_run_at=iso_in(-5))
        )
        q.enqueue_task(
            Task(task_id="hi", task_type="analyze_video", payload={}, status="queued",
                 priority=9, next_run_at=iso_in(-5))
        )

    # Higher priority claimed first.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        first = repos.task_queue().claim_task("w1", now=dt_in(0), lease_seconds=30)
    assert first is not None and first.task_id == "hi"

    # Lease still valid: another worker cannot re-claim "hi"; it gets "lo".
    with runtime.session() as session:
        repos = runtime.repositories(session)
        second = repos.task_queue().claim_task("w2", now=dt_in(1), lease_seconds=30)
    assert second is not None and second.task_id == "lo"

    # After "hi" lease expiry, it can be reclaimed.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        third = repos.task_queue().claim_task("w3", now=dt_in(100), lease_seconds=30)
    assert third is not None and third.task_id == "hi"


# ---------------------------------------------------------------------------
# Stage 2 — config defaults + unit-pool ownership
# ---------------------------------------------------------------------------


def test_max_concurrent_jobs_default_is_one(mj_runtime: Runtime) -> None:
    """Default config keeps single-job serial behavior."""
    assert mj_runtime.config.worker.max_concurrent_jobs == 1
    assert mj_runtime.config.worker.max_unit_workers_per_job == 1


def test_per_job_unit_pool_uses_only_worker_unit_knob(mj_runtime: Runtime) -> None:
    """Unit-pool size is controlled by max_unit_workers_per_job in every mode.

    Regression: single-job mode used to size the unit pool from
    ``vlm.max_concurrent_requests`` to preserve legacy behavior. That made the VLM
    provider cap double as a fourth unit-concurrency knob. The agreed model has
    exactly three degree knobs with non-overlapping responsibilities, so the per-
    job unit pool must come from ``worker.max_unit_workers_per_job`` even when
    ``worker.max_concurrent_jobs == 1``.
    """
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 1
    runtime.config.worker.max_unit_workers_per_job = 3
    runtime.config.vlm.max_concurrent_requests = 7

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )

    assert worker._per_job_unit_workers() == 3


def test_high_concurrency_config_reaches_worker_scheduler(
    mj_runtime: Runtime,
) -> None:
    """Canonical 1000/1000/500 config must not collapse to a hidden cap of 2."""
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 1000
    runtime.config.worker.max_unit_workers_per_job = 1000
    runtime.config.vlm.max_concurrent_requests = 500

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )

    assert worker._per_job_unit_workers() == 1000
    assert worker._vlm_scheduler._semaphore._value == 500


def test_serial_drain_processes_all_jobs(mj_runtime: Runtime) -> None:
    """max_concurrent_jobs=1: drain still processes every queued job correctly."""
    runtime = mj_runtime
    job_ids = [_submit_job(runtime, key=f"serial-{i}", minute=i) for i in range(3)]

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    processed = worker.drain()
    assert processed == 3
    with runtime.session() as session:
        repos = runtime.repositories(session)
        for jid in job_ids:
            job = repos.analysis_job().get_job(jid)
            assert job is not None and job.job_status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Stage 3 — multi-job concurrency + global VLM cap
# ---------------------------------------------------------------------------


def test_multiple_one_unit_jobs_run_concurrently(mj_runtime: Runtime) -> None:
    """4 one-unit jobs with max_concurrent_jobs=4 overlap across DIFFERENT jobs.

    Proves multi-job concurrency: VLM calls from distinct analysis_job_ids are
    in-flight at the same time (the single-job unit pool cannot do this when each
    job has only one unit).
    """
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 4
    for i in range(4):
        _submit_job(runtime, key=f"conc-{i}", minute=i)

    active_jobs: set[str] = set()
    max_distinct_overlap = 0
    lock = threading.Lock()

    class _Probe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            nonlocal max_distinct_overlap
            with lock:
                active_jobs.add(request.analysis_job_id)
                max_distinct_overlap = max(max_distinct_overlap, len(active_jobs))
            time.sleep(0.05)
            with lock:
                active_jobs.discard(request.analysis_job_id)
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=_Probe(),
    )
    worker.drain()

    assert max_distinct_overlap >= 2, (
        f"expected concurrent VLM calls from >=2 distinct jobs, "
        f"got max overlap {max_distinct_overlap}"
    )


def test_global_vlm_cap_not_exceeded_across_jobs(mj_runtime: Runtime) -> None:
    """Global in-flight VLM calls never exceed vlm.max_concurrent_requests even
    with many concurrent jobs each running their own unit pool."""
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 6
    runtime.config.worker.max_unit_workers_per_job = 4
    runtime.config.vlm.max_concurrent_requests = 2  # global cap
    for i in range(6):
        _submit_job(runtime, key=f"cap-{i}", minute=i)

    peak = 0
    current = 0
    lock = threading.Lock()

    class _Probe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.02)
            with lock:
                current -= 1
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=_Probe(),
    )
    worker.drain()

    assert peak <= 2, (
        f"global VLM peak {peak} exceeded cap 2 "
        f"(max_concurrent_jobs=6 x max_unit_workers_per_job=4 must NOT multiply cap)"
    )


def test_one_job_failure_does_not_block_others(mj_runtime: Runtime) -> None:
    """One job whose VLM always fails must not prevent other jobs from succeeding."""
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 4
    fail_job = _submit_job(runtime, key="willfail", minute=0)
    ok_jobs = [_submit_job(runtime, key=f"willok-{i}", minute=i + 1) for i in range(3)]

    # Resolve the failing job's video_id so the probe can target only that job.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        fail_video_id = repos.analysis_job().get_job(fail_job).video_id  # type: ignore[union-attr]

    class _SelectiveVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            if request.video_id == fail_video_id:
                raise RuntimeError("provider down for this job")
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=_SelectiveVlm(),
    )
    worker.drain()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        failed = repos.analysis_job().get_job(fail_job)
        assert failed is not None and failed.job_status is JobStatus.FAILED
        for jid in ok_jobs:
            job = repos.analysis_job().get_job(jid)
            assert job is not None and job.job_status is JobStatus.SUCCEEDED


def test_no_duplicate_active_records_under_concurrency(mj_runtime: Runtime) -> None:
    """Concurrent multi-job processing keeps publication exactly-once: each job's
    single segment yields exactly one active ObservationRecord."""
    from cctv_memory.infrastructure.db.models import tables as orm
    from sqlalchemy import func, select

    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 4
    n = 5
    for i in range(n):
        _submit_job(runtime, key=f"dedup-{i}", minute=i)

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    worker.drain()

    with runtime.session() as session:
        rec_count = session.scalar(
            select(func.count()).select_from(orm.ObservationRecord)
        )
        unit_count = session.scalar(
            select(func.count()).select_from(orm.AnalysisUnit)
        )
    # One unit + one record per job, no duplicates.
    assert rec_count == n, f"expected {n} active records, got {rec_count}"
    assert unit_count == n, f"expected {n} units, got {unit_count}"


# ---------------------------------------------------------------------------
# Stage 4 — graceful shutdown / no stranded running
# ---------------------------------------------------------------------------


def test_should_stop_prevents_claiming_new_jobs(mj_runtime: Runtime) -> None:
    """When should_stop() is already True, drain claims NOTHING (graceful stop)."""
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 4
    for i in range(3):
        _submit_job(runtime, key=f"stop-{i}", minute=i)

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    processed = worker.drain(should_stop=lambda: True)
    assert processed == 0

    # Jobs remain queued (not claimed), so a normal drain still picks them up.
    assert worker.drain() == 3


def test_concurrent_drain_leaves_no_running_units(mj_runtime: Runtime) -> None:
    """After a concurrent drain completes, no unit is left in RUNNING (terminal
    state machine holds under multi-job concurrency; orphan recovery not needed)."""
    from cctv_memory.domain.enums import TaskStatus
    from cctv_memory.infrastructure.db.models import tables as orm
    from sqlalchemy import func, select

    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 2
    for i in range(6):
        _submit_job(runtime, key=f"norun-{i}", minute=i)

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    worker.drain()

    with runtime.session() as session:
        running = session.scalar(
            select(func.count())
            .select_from(orm.AnalysisUnit)
            .where(orm.AnalysisUnit.status == TaskStatus.RUNNING.value)
        )
    assert running == 0, f"{running} units left RUNNING after concurrent drain"


# ---------------------------------------------------------------------------
# §B4 — lease renewal / duplicate-claim protection for long jobs
# ---------------------------------------------------------------------------


def test_long_job_lease_is_renewed_so_no_duplicate_claim(mj_runtime: Runtime) -> None:
    """A job that runs longer than ``lease_seconds`` has its lease renewed by the
    heartbeat, so a competing worker cannot reclaim the in-flight task (no
    duplicate processing). Task §B4 / job-state-machine-contract §4.
    """
    runtime = mj_runtime
    # Short lease + fast renew so the renewal must fire during processing.
    runtime.config.worker.lease_seconds = 1
    runtime.config.worker.lease_renew_seconds = 1
    job_id = _submit_job(runtime, key="leasejob", minute=0)

    started = threading.Event()

    class _SlowVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            started.set()
            # Process well past the 1s lease so renewal is required.
            time.sleep(2.5)
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=_SlowVlm(),
    )

    result: dict[str, object] = {}

    def _run() -> None:
        result["task_id"] = worker.process_one()

    t = threading.Thread(target=_run)
    t.start()
    # Wait until processing is mid-flight (VLM in progress, past the 1s lease soon).
    assert started.wait(timeout=5.0)
    time.sleep(1.5)  # now beyond the original 1s lease window

    # A competing worker tries to claim: the lease must have been renewed, so the
    # running task is NOT reclaimable.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        stolen = repos.task_queue().claim_task(
            "intruder-worker", now=datetime.now(UTC), lease_seconds=30
        )
    assert stolen is None, "lease was not renewed; task got reclaimed (duplicate processing)"

    t.join(timeout=10.0)
    assert not t.is_alive()

    # Job finished cleanly under the single owner.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None and job.job_status is JobStatus.SUCCEEDED


def test_lease_renew_seconds_default_below_lease(mj_runtime: Runtime) -> None:
    """Config guard: renewal cadence is shorter than the lease window."""
    assert mj_runtime.config.worker.lease_renew_seconds < mj_runtime.config.worker.lease_seconds


# ---------------------------------------------------------------------------
# Regression — concurrency must be driven solely by max_concurrent_jobs,
# with no hardcoded batch cap silently throttling it
# ---------------------------------------------------------------------------


def test_drain_pool_is_not_capped_by_hardcoded_100(
    mj_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_concurrent_jobs above the old hardcoded 100 must size the pool fully.

    Regression: ``drain`` used ``max_tasks=100`` and ``_drain_concurrent`` sized
    the thread pool ``min(max_concurrent_jobs, max_tasks)``, so a config of e.g.
    150 was silently throttled to 100. The batch budget was removed entirely; the
    pool is now exactly ``max_concurrent_jobs``.
    """
    import cctv_memory.workers.analysis_worker as worker_mod

    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 150

    captured: dict[str, int] = {}
    real_pool = worker_mod.ThreadPoolExecutor

    def _spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["max_workers"] = kwargs.get("max_workers", args[0] if args else None)
        # Don't actually spawn 150 threads; run nothing.
        return real_pool(max_workers=1)

    monkeypatch.setattr(worker_mod, "ThreadPoolExecutor", _spy)

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    # No jobs queued: pool is still constructed, then workers find nothing.
    worker.drain()

    assert captured.get("max_workers") == 150, (
        f"pool sized {captured.get('max_workers')}, expected 150 "
        "(concurrency must follow max_concurrent_jobs, no hardcoded cap)"
    )


def test_drain_processes_whole_queue_in_one_pass(mj_runtime: Runtime) -> None:
    """drain() keeps claiming until the queue is empty (no batch-size cap)."""
    runtime = mj_runtime
    runtime.config.worker.max_concurrent_jobs = 1
    for i in range(5):
        _submit_job(runtime, key=f"drain-all-{i}", minute=i)

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    # All five jobs drain in a single pass; nothing is left for a second pass.
    assert worker.drain() == 5
    assert worker.drain() == 0
