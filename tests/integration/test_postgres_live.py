from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.analysis import AnalysisJob
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import (
    AccessPolicy,
    AccessPolicyRules,
    AuthorizedScope,
)
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.task import Task
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.contracts.video import CameraDevice, CameraLocation, SubmitVideoSourceRequest
from cctv_memory.domain.enums import AnalysisScale, JobStatus, SecurityLevel, SourceType
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.repositories.index import StoredVector
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _postgres_dsn() -> str:
    dsn = os.environ.get("CCTV_MEMORY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CCTV_MEMORY_TEST_POSTGRES_DSN is not set")
    return dsn


@pytest.fixture(autouse=True)
def _clean_public_schema() -> None:
    """Reset the database to a clean state before each live test.

    These tests share one database and create tables with
    ``CREATE TABLE IF NOT EXISTS``; some use different
    ``embedding_dimensions`` (so ``observation_vectors`` would otherwise be
    reused with the wrong ``vector(N)`` width). Dropping the public schema up
    front keeps the tests order-independent.
    """
    dsn = os.environ.get("CCTV_MEMORY_TEST_POSTGRES_DSN")
    if not dsn:
        return
    from sqlalchemy import create_engine

    engine = create_engine(dsn, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()


def test_live_postgres_schema_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = _postgres_dsn()
    monkeypatch.setenv("CCTV_MEMORY_POSTGRES_DSN", dsn)
    config = AppConfig(
        database={"backend": "postgres"},
        indexing={"embedding_dimensions": 1024},
    )

    runtime = Runtime(config)
    try:
        runtime.create_schema()
        with runtime.engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('public.observation_vectors')")).scalar()
            assert conn.execute(text("SELECT to_regclass('public.detector_gate_logs')")).scalar()
            vector_col = conn.execute(
                text(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod)
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = 'observation_vectors'
                      AND a.attname = 'embedding'
                    """
                )
            ).scalar_one()
            assert vector_col == "vector(1024)"
    finally:
        runtime.dispose()


def test_live_postgres_representative_writes_keep_native_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _postgres_dsn()
    monkeypatch.setenv("CCTV_MEMORY_POSTGRES_DSN", dsn)
    config = AppConfig(
        database={"backend": "postgres"},
        indexing={"embedding_dimensions": 4},
    )
    runtime = Runtime(config)
    suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    location_id = f"pg_loc_{suffix}"
    camera_id = f"pg_cam_{suffix}"
    video_id = f"pg_vid_{suffix}"
    job_id = f"pg_job_{suffix}"
    task_id = f"pg_task_{suffix}"
    audit_id = f"pg_audit_{suffix}"
    timeline_id = f"pg_timeline_{suffix}"
    record_id = f"pg_record_{suffix}"
    now = datetime.now(UTC)

    try:
        runtime.create_schema()
        with runtime.session() as session:
            repos = runtime.repositories(session)
            repos.camera().upsert_location(
                CameraLocation(
                    location_id=location_id,
                    area="PG Lab",
                    created_at=now,
                    updated_at=now,
                )
            )
            repos.camera().upsert_camera(
                CameraDevice(
                    camera_id=camera_id,
                    camera_name="PG Camera",
                    location_id=location_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            repos.video_source().create_or_get_by_idempotency(
                SubmitVideoSourceRequest(
                    source_type=SourceType.FILE,
                    source_uri="file:///tmp/pg-test.mp4",
                    camera_id=camera_id,
                    video_start_time=now,
                    analysis_options={"daily": True},
                ),
                video_id=video_id,
            )
            repos.analysis_job().create_job(
                AnalysisJob(
                    analysis_job_id=job_id,
                    video_id=video_id,
                    idempotency_key=f"idem_{job_id}",
                    analysis_options={"daily": True},
                    created_at=now,
                )
            )
            repos.task_queue().enqueue_task(
                Task(
                    task_id=task_id,
                    task_type="analysis",
                    payload={"analysis_job_id": job_id, "nested": {"ok": True}},
                    next_run_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            claimed = repos.task_queue().claim_task(
                worker_id=f"worker_{suffix}",
                now=now,
                lease_seconds=30,
            )
            # claim_task reads the row back through mappers.task_to_dto. On
            # PostgreSQL the TIMESTAMPTZ columns come back as datetime objects;
            # the Task DTO uses the canonical datetime type, so task_to_dto
            # normalizes both SQLite ISO text and PostgreSQL datetime through
            # _dt. (Before type unification the DTO typed these as str, which
            # raised a Pydantic string_type error here, rolled back the claim,
            # and left the queue undrained forever.)
            assert claimed is not None
            assert claimed.task_id == task_id
            assert claimed.payload["analysis_job_id"] == job_id
            assert isinstance(claimed.next_run_at, datetime)
            assert claimed.lease_owner == f"worker_{suffix}"
            assert isinstance(claimed.lease_expires_at, datetime)
            # mark_succeeded must clear the TIMESTAMPTZ lease_expires_at column.
            # The ORM-based update rendered ``lease_expires_at=$::VARCHAR`` which
            # PostgreSQL rejects (DatatypeMismatch), so the task could never reach
            # a terminal state and the analysis job stayed stuck in RUNNING even
            # though the VLM ran and results were written. Raw SQL avoids the
            # VARCHAR coercion.
            repos.task_queue().mark_succeeded(task_id)
            session.flush()
            task_status = session.execute(
                text("SELECT status, lease_expires_at FROM analysis_tasks WHERE task_id = :t"),
                {"t": task_id},
            ).one()
            assert task_status[0] == "succeeded"
            assert task_status[1] is None
            repos.audit().append_event(
                AuditEvent(
                    audit_event_id=audit_id,
                    event_type="pg_native_type_test",
                    record_ids=[record_id],
                    metadata={"analysis_job_id": job_id, "ok": True},
                    created_at=now,
                )
            )
            repos.timeline().append_event(
                AnalysisTimelineEvent(
                    timeline_event_id=timeline_id,
                    trace_id=job_id,
                    analysis_job_id=job_id,
                    event_name="pg_native_type_test",
                    event_phase="instant",
                    occurred_at=now,
                    correlation={"vlm_request_id": "pg_vlm_req"},
                    metadata={"ok": True},
                )
            )
            timeline_events = repos.timeline().list_by_job(job_id)
            assert [event.timeline_event_id for event in timeline_events] == [timeline_id]
            assert isinstance(timeline_events[0].occurred_at, datetime)
            assert timeline_events[0].correlation == {"vlm_request_id": "pg_vlm_req"}
            assert timeline_events[0].metadata == {"ok": True}
            repos.publication().publish_records_atomically(
                PublishObservationRecordsCommand(
                    command_id=f"cmd_{suffix}",
                    analysis_job_id=job_id,
                    records=[
                        ObservationRecord(
                            record_id=record_id,
                            video_id=video_id,
                            analysis_job_id=job_id,
                            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                            segment_start_ms=0,
                            segment_end_ms=1000,
                            observed_start_time=now,
                            observed_end_time=now,
                            camera_id=camera_id,
                            location_id=location_id,
                            static_description_text="person near the door",
                            dynamic_description_text="person walks through the door",
                            tags=["person", "door"],
                            attributes={"confidence": 0.9},
                            access_policy_id="policy_default",
                            security_level=SecurityLevel.INTERNAL,
                            created_at=now,
                            updated_at=now,
                        )
                    ],
                )
            )
            repos.index().upsert_vectors(
                [
                    StoredVector(
                        record_id=record_id,
                        vector_type="semantic",
                        embedding=[0.1, 0.2, 0.3, 0.4],
                        model_id="test-embedding",
                        dimension=4,
                        metadata={"source": "live-test"},
                    )
                ]
            )
            stale_units = repos.analysis_unit().list_stale_running(
                cutoff=now,
                limit=10,
            )
            assert stale_units == []
            # Reading an access policy back exercises the JSONB rules column ->
            # dict round-trip that the submit path depends on (regression for the
            # spurious 400 on /video-sources/analyze). list_access_policies is the
            # exact call authorized_scope_for makes before ranking.
            policy_id = f"pg_policy_{suffix}"
            repos.access_policy().upsert_access_policy(
                AccessPolicy(
                    access_policy_id=policy_id,
                    name=f"pg-policy-{suffix}",
                    security_level=SecurityLevel.INTERNAL,
                    rules=AccessPolicyRules(allowed_roles=["analyst"]),
                    created_at=now,
                    updated_at=now,
                )
            )
            fetched = repos.access_policy().get_access_policy(policy_id)
            assert fetched is not None
            assert fetched.rules.allowed_roles == ["analyst"]
            assert any(
                p.access_policy_id == policy_id
                for p in repos.access_policy().list_access_policies()
            )
            pool = repos.observation_read().authorized_candidate_pool(
                AuthorizedScope(
                    principal_id="pg_test_principal",
                    tenant_id="tenant_default",
                    allowed_camera_ids=[camera_id],
                    allowed_location_ids=[location_id],
                    allowed_access_policy_ids=["policy_default"],
                    max_security_level=SecurityLevel.INTERNAL,
                    scope_hash="pg_test_scope_hash",
                ),
                time_start=now,
                time_end=now,
                limit=10,
            )
            assert [record.record_id for record in pool] == [record_id]

        with runtime.engine.connect() as conn:
            type_rows = conn.execute(
                text(
                    """
                    SELECT table_name, column_name, data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND (table_name, column_name) IN (
                        ('camera_locations', 'created_at'),
                        ('camera_devices', 'updated_at'),
                        ('video_sources', 'video_start_time'),
                        ('analysis_jobs', 'analysis_options_json'),
                        ('analysis_tasks', 'payload_json'),
                        ('analysis_timeline_events', 'occurred_at'),
                        ('analysis_timeline_events', 'metadata_json'),
                        ('audit_events', 'metadata_json'),
                        ('observation_records', 'attributes_json')
                      )
                    """
                )
            ).mappings()
            types = {(row["table_name"], row["column_name"]): row for row in type_rows}
            assert (
                types[("camera_locations", "created_at")]["data_type"]
                == "timestamp with time zone"
            )
            assert (
                types[("camera_devices", "updated_at")]["data_type"]
                == "timestamp with time zone"
            )
            assert (
                types[("video_sources", "video_start_time")]["data_type"]
                == "timestamp with time zone"
            )
            assert types[("analysis_jobs", "analysis_options_json")]["udt_name"] == "jsonb"
            assert types[("analysis_tasks", "payload_json")]["udt_name"] == "jsonb"
            assert (
                types[("analysis_timeline_events", "occurred_at")]["data_type"]
                == "timestamp with time zone"
            )
            assert (
                types[("analysis_timeline_events", "metadata_json")]["udt_name"]
                == "jsonb"
            )
            assert types[("audit_events", "metadata_json")]["udt_name"] == "jsonb"
            assert types[("observation_records", "attributes_json")]["udt_name"] == "jsonb"
            vector_type = conn.execute(
                text(
                    """
                    SELECT pg_typeof(embedding)::text
                    FROM observation_vectors
                    WHERE record_id = :record_id
                    """
                ),
                {"record_id": record_id},
            ).scalar_one()
            assert vector_type == "vector"
    finally:
        runtime.dispose()


def test_live_postgres_worker_drains_job_to_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a submitted job must reach a terminal state on PostgreSQL.

    Regression for "job stuck in RUNNING": the worker ran the VLM and wrote
    observation records, but the finalize step called task_queue.mark_succeeded,
    whose ORM-based UPDATE rendered the TIMESTAMPTZ lease_expires_at column as
    VARCHAR. PostgreSQL rejected it (DatatypeMismatch), the finalize transaction
    was swallowed by the worker loop, and the job never left RUNNING despite the
    results being committed in their own per-unit sessions.
    """
    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.contracts.auth import Principal
    from cctv_memory.domain.enums import Capability, PrincipalType
    from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    dsn = _postgres_dsn()
    monkeypatch.setenv("CCTV_MEMORY_POSTGRES_DSN", dsn)
    config = AppConfig(
        database={"backend": "postgres"},
        indexing={"embedding_dimensions": 1024, "provider": "mock"},
        vlm={"provider": "mock"},
    )
    runtime = Runtime(config)
    suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    principal_id = f"pg_svc_{suffix}"
    try:
        runtime.create_schema()
        with runtime.session() as session:
            repos = runtime.repositories(session)
            repos.camera().upsert_location(
                CameraLocation(
                    location_id="loc_lobby_01",
                    area="lobby",
                    access_policy_id="policy_public_area",
                    security_level=SecurityLevel.INTERNAL,
                )
            )
            repos.camera().upsert_camera(
                CameraDevice(
                    camera_id="cam_lobby_01",
                    camera_name="Lobby Cam",
                    location_id="loc_lobby_01",
                    access_policy_id="policy_public_area",
                    status="active",
                )
            )
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

        with runtime.session() as session:
            repos = runtime.repositories(session)
            ingestion = IngestionService(
                repos.video_source(),
                repos.analysis_job(),
                repos.scale_task(),
                repos.task_queue(),
                repos.audit(),
            )
            principal = repos.principal().get_principal(principal_id)
            assert principal is not None
            resp = ingestion.submit(
                SubmitVideoSourceRequest(
                    source_type=SourceType.FILE,
                    source_uri="/data/videos/lobby.mp4",
                    camera_id="cam_lobby_01",
                    video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                    idempotency_key=f"pg-terminal-{suffix}",
                    analysis_options={"enable_default_segment": True},
                ),
                principal,
                capabilities=[Capability.ANALYSIS_SUBMIT],
            )
        job_id = resp.analysis_job_id

        worker = AnalysisWorker(
            runtime,
            video_processor=StaticVideoProcessor(duration_ms=30_000),
        )
        # process_one must NOT raise; the finalize write (mark_succeeded) used to
        # throw a DatatypeMismatch here.
        assert worker.process_one() is not None

        with runtime.session() as session:
            repos = runtime.repositories(session)
            job = repos.analysis_job().get_job(job_id)
            assert job is not None
            assert job.job_status in (JobStatus.SUCCEEDED, JobStatus.PARTIAL_FAILED)
            record_count = session.execute(
                text(
                    "SELECT count(*) FROM observation_records WHERE analysis_job_id = :j"
                ),
                {"j": job_id},
            ).scalar()
            assert record_count and record_count > 0
            task_rows = session.execute(
                text(
                    "SELECT status FROM analysis_tasks WHERE payload_json->>'analysis_job_id' = :j"
                ),
                {"j": job_id},
            ).scalars().all()
            assert task_rows and all(s == "succeeded" for s in task_rows)
    finally:
        runtime.dispose()
