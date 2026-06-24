"""Cold-start camera provisioning concurrency regression (task 20260617-1118, R1).

Covers the race the fix-options report identified: lazy camera/location
provisioning (`resolve_video_context` -> upsert_location/upsert_camera) was a
non-atomic SELECT-then-INSERT. Under multi-job cold-start concurrency two jobs
could both miss and both INSERT the shared `loc_auto_unregistered` (or the same
camera_id); the loser raised IntegrityError which bubbled up and failed the whole
job.

A1: upsert_location/upsert_camera now run the write in a SAVEPOINT and read the
winner back on conflict (idempotent, outer session not poisoned).
B1: the shared auto-location is pre-created in the seed path.

These tests deliberately do NOT pre-register the camera so the lazy-provisioning
path is exercised (the existing multi-job suite seeds the camera and never hits
this path).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.application.seed import seed_local_defaults
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.video import (
    CameraDevice,
    CameraLocation,
    SubmitVideoSourceRequest,
)
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import Capability, JobStatus, SourceType
from cctv_memory.domain.policies import AUTO_LOCATION_ID
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.workers.analysis_worker import AnalysisWorker

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def prov_runtime(tmp_path: object) -> Iterator[Runtime]:
    """Static-mode runtime seeded with principal+policy but NO camera.

    Leaving the camera unregistered forces the lazy-provisioning path during
    analysis, which is exactly where the cold-start race lived.
    """
    config = AppConfig().with_data_dir(str(tmp_path))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.static_duration_ms = 8_000
    config.pipeline.default_segment.window_seconds = 30
    config.pipeline.default_segment.overlap_seconds = 0
    runtime = Runtime(config)
    runtime.init_storage()
    runtime.create_schema()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        # Seed principal + policy ONLY (the default seed also pre-creates the
        # auto-location via B1; the race tests below assert idempotency holds).
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())
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


def _submit(runtime: Runtime, *, key: str, camera_id: str, minute: int = 0) -> str:
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        principal = repos.principal().get_principal("user_admin")
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=f"/data/videos/{key}.mp4",
                camera_id=camera_id,
                video_start_time=datetime(2026, 6, 17, 11, minute % 60, tzinfo=UTC),
                idempotency_key=key,
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


def _drain(runtime: Runtime) -> AnalysisWorker:
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=8_000),
        vlm=type("_V", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    worker.drain()
    return worker


# ---------------------------------------------------------------------------
# End-to-end: concurrent jobs with UNREGISTERED cameras must not fail
# ---------------------------------------------------------------------------


def test_concurrent_jobs_same_unregistered_camera_no_failure(prov_runtime: Runtime) -> None:
    """Many jobs sharing ONE unregistered camera_id, run concurrently: none FAIL.

    Both the shared auto-location AND the shared camera_id are first-provisioned
    concurrently; the savepoint+readback upsert must make the losers idempotent.
    """
    runtime = prov_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 4
    job_ids = [_submit(runtime, key=f"same-{i}", camera_id="cam_unreg", minute=i) for i in range(4)]

    _drain(runtime)

    with runtime.session() as session:
        repos = runtime.repositories(session)
        for jid in job_ids:
            job = repos.analysis_job().get_job(jid)
            assert job is not None, jid
            assert job.job_status is JobStatus.SUCCEEDED, (jid, job.job_status)


def test_concurrent_jobs_distinct_unregistered_cameras_single_auto_location(
    prov_runtime: Runtime,
) -> None:
    """Distinct unregistered cameras concurrently share ONE auto-location row.

    No job fails, exactly one `loc_auto_unregistered` exists, and each distinct
    camera was provisioned once under it.
    """
    runtime = prov_runtime
    runtime.config.worker.max_concurrent_jobs = 4
    runtime.config.vlm.max_concurrent_requests = 4
    job_ids = [
        _submit(runtime, key=f"dist-{i}", camera_id=f"cam_unreg_{i}", minute=i) for i in range(4)
    ]

    _drain(runtime)

    with runtime.session() as session:
        repos = runtime.repositories(session)
        for jid in job_ids:
            job = repos.analysis_job().get_job(jid)
            assert job is not None and job.job_status is JobStatus.SUCCEEDED, jid
        loc = repos.camera().get_location(AUTO_LOCATION_ID)
        assert loc is not None
        for i in range(4):
            cam = repos.camera().get_camera(f"cam_unreg_{i}")
            assert cam is not None and cam.location_id == AUTO_LOCATION_ID


# ---------------------------------------------------------------------------
# Repository-level: savepoint + conflict readback idempotency / session safety
# ---------------------------------------------------------------------------


def _loc(lid: str, area: str = "unregistered") -> CameraLocation:
    from cctv_memory.domain.enums import SecurityLevel

    return CameraLocation(location_id=lid, area=area, security_level=SecurityLevel.INTERNAL)


def _cam(cid: str, location_id: str = AUTO_LOCATION_ID) -> CameraDevice:
    return CameraDevice(camera_id=cid, camera_name=cid, location_id=location_id, status="active")


def test_upsert_location_idempotent_repeat(factory: SqliteRepositoryFactory) -> None:
    """Repeated upsert_location returns idempotently and never raises."""
    cameras = factory.camera()
    a = cameras.upsert_location(_loc("loc_x"))
    b = cameras.upsert_location(_loc("loc_x"))
    assert a.location_id == b.location_id == "loc_x"


def test_upsert_location_conflict_readback_keeps_session_usable(tmp_path: object) -> None:
    """The exact race ordering: loser SELECTs None, winner commits, loser conflicts.

    The loser's savepoint rolls back, it reads the winner row back as idempotent
    success, and its outer session remains usable for subsequent work + commit.
    """
    from cctv_memory.infrastructure.db.engine import (
        create_session_factory,
        create_sqlite_engine,
    )
    from cctv_memory.infrastructure.db.models import Base

    db_path = f"{tmp_path}/race.sqlite3"  # type: ignore[str-bytes-safe]
    engine = create_sqlite_engine(db_path)
    Base.metadata.create_all(engine)
    sf = create_session_factory(engine)
    win_sess, lose_sess = sf(), sf()
    try:
        win_repo = SqliteRepositoryFactory(win_sess).camera()
        lose_repo = SqliteRepositoryFactory(lose_sess).camera()

        # Loser establishes a read snapshot (row absent) FIRST.
        assert lose_repo.get_location(AUTO_LOCATION_ID) is None

        # Winner inserts + commits.
        win_repo.upsert_location(_loc(AUTO_LOCATION_ID, area="winner"))
        win_sess.commit()

        # Loser now upserts the SAME id -> PK conflict -> readback, no raise.
        result = lose_repo.upsert_location(_loc(AUTO_LOCATION_ID, area="loser"))
        assert result.location_id == AUTO_LOCATION_ID

        # Outer session not poisoned: more work + commit succeeds.
        lose_repo.upsert_camera(_cam("cam_after_conflict"))
        lose_sess.commit()
        assert lose_repo.get_camera("cam_after_conflict") is not None
    finally:
        win_sess.close()
        lose_sess.close()
        engine.dispose()


def test_upsert_camera_concurrent_first_provision_single_row(tmp_path: object) -> None:
    """Concurrent first-provision of one camera_id: no raw IntegrityError, one row."""
    from cctv_memory.infrastructure.db.engine import (
        create_session_factory,
        create_sqlite_engine,
    )
    from cctv_memory.infrastructure.db.models import Base
    from cctv_memory.infrastructure.db.models import tables as orm
    from sqlalchemy import func, select

    db_path = f"{tmp_path}/cam_race.sqlite3"  # type: ignore[str-bytes-safe]
    engine = create_sqlite_engine(db_path)
    Base.metadata.create_all(engine)
    sf = create_session_factory(engine)

    # Shared auto-location must exist for the FK-free placeholder camera rows.
    seed_sess = sf()
    SqliteRepositoryFactory(seed_sess).camera().upsert_location(_loc(AUTO_LOCATION_ID))
    seed_sess.commit()
    seed_sess.close()

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def _provision() -> None:
        session = sf()
        try:
            barrier.wait()
            repo = SqliteRepositoryFactory(session).camera()
            repo.upsert_camera(_cam("cam_shared_unreg"))
            session.commit()
        except Exception as exc:  # noqa: BLE001 - record any leaked error
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=_provision) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"upsert_camera leaked errors under concurrency: {errors!r}"
    check = sf()
    try:
        count = check.scalar(
            select(func.count()).select_from(orm.CameraDevice).where(
                orm.CameraDevice.camera_id == "cam_shared_unreg"
            )
        )
        assert count == 1, f"expected exactly one camera row, got {count}"
    finally:
        check.close()
        engine.dispose()


def test_seed_pre_creates_auto_location(prov_runtime: Runtime) -> None:
    """B1: the real seed path pre-creates the shared auto-location idempotently."""
    runtime = prov_runtime
    with runtime.session() as session:
        repos = runtime.repositories(session)
        loc = repos.camera().get_location(AUTO_LOCATION_ID)
        assert loc is not None
        # Re-seeding stays idempotent (no raise, still one row).
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())
        assert repos.camera().get_location(AUTO_LOCATION_ID) is not None
