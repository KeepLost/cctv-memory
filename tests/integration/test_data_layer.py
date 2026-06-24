"""Integration tests: publication atomicity, task lease, audit, migration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cctv_memory.contracts.analysis import AnalysisJob
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.task import Task
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.domain.enums import AnalysisScale, JobStatus, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import dt_in, iso_in, new_id, seed_camera

_T0 = datetime.fromisoformat("2026-06-06T21:00:00+08:00")
_T1 = datetime.fromisoformat("2026-06-06T21:00:15+08:00")


def _record(record_id: str, static_text: str = "v1") -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_001",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=15000,
        observed_start_time=_T0,
        observed_end_time=_T1,
        camera_id="cam_lobby_01",
        location_id="loc_lobby_01",
        static_description_text=static_text,
        dynamic_description_text="d",
        tags=["person"],
        access_policy_id="policy_public_area",
        security_level=SecurityLevel.INTERNAL,
    )


def _seed_job(factory: SqliteRepositoryFactory) -> None:
    factory.analysis_job().create_job(
        AnalysisJob(
            analysis_job_id="job_001",
            video_id="video_001",
            job_status=JobStatus.RUNNING,
            idempotency_key="idem-job-001",
        )
    )


def test_publication_upsert_archives_old_record(
    factory: SqliteRepositoryFactory, session: Session
) -> None:
    seed_camera(factory)
    _seed_job(factory)

    # First publication: creates the active record.
    r1 = factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="pub_1", analysis_job_id="job_001", records=[_record("obs_v1", "v1")]
        )
    )
    assert r1.created_record_ids == ["obs_v1"]

    # Second publication on same (video, segment, scale): replaces + archives.
    r2 = factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="pub_2", analysis_job_id="job_001", records=[_record("obs_v2", "v2")]
        )
    )
    assert r2.updated_record_ids == ["obs_v2"]
    assert r2.archived_record_ids == ["obs_v1"]

    # Active table has exactly one record (the new one).
    active_count = session.scalar(text("SELECT COUNT(*) FROM observation_records"))
    assert active_count == 1
    history_count = session.scalar(text("SELECT COUNT(*) FROM observation_record_history"))
    assert history_count == 1

    # Job publish summary updated.
    job = factory.analysis_job().get_job("job_001")
    assert job is not None
    assert "obs_v1" in job.created_record_ids
    assert "obs_v2" in job.updated_record_ids
    assert "obs_v1" in job.archived_record_ids


def test_publication_writes_audit_event(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _seed_job(factory)
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="pub_1", analysis_job_id="job_001", records=[_record("obs_a")]
        )
    )
    events = factory.audit().list_events(event_type="publication_succeeded")
    assert len(events.items) == 1
    assert "obs_a" in events.items[0].record_ids


def test_publication_rollback_leaves_no_partial_records(
    factory: SqliteRepositoryFactory, session: Session
) -> None:
    seed_camera(factory)
    _seed_job(factory)
    # Two records with the SAME primary key (record_id) force an IntegrityError
    # on the second insert, which must roll back the whole publication.
    dup_a = _record("obs_dup")
    dup_b = _record("obs_dup").model_copy(
        update={"segment_start_ms": 20000, "segment_end_ms": 35000}
    )
    with pytest.raises(IntegrityError):
        factory.publication().publish_records_atomically(
            PublishObservationRecordsCommand(
                command_id="pub_bad",
                analysis_job_id="job_001",
                records=[dup_a, dup_b],
            )
        )
    session.rollback()
    active_count = session.scalar(text("SELECT COUNT(*) FROM observation_records"))
    assert active_count == 0  # no partial active records remain


def test_task_claim_sets_lease_and_expiry_reclaim(factory: SqliteRepositoryFactory) -> None:
    queue = factory.task_queue()
    past = iso_in(-100)
    queue.enqueue_task(
        Task(task_id="task_1", task_type="analyze_video", next_run_at=past)
    )

    # Claim with a lease that is already expired (lease_seconds negative => past).
    claimed = queue.claim_task("worker-A", now=dt_in(-50), lease_seconds=10)
    assert claimed is not None
    assert claimed.task_id == "task_1"
    assert claimed.lease_owner == "worker-A"
    assert claimed.lease_expires_at is not None

    # A fresh worker cannot claim while the lease is valid.
    assert queue.claim_task("worker-B", now=dt_in(-45), lease_seconds=10) is None

    # After lease expiry, another worker reclaims it.
    reclaimed = queue.claim_task("worker-B", now=dt_in(100), lease_seconds=10)
    assert reclaimed is not None
    assert reclaimed.task_id == "task_1"
    assert reclaimed.lease_owner == "worker-B"


def test_audit_append_roundtrip(factory: SqliteRepositoryFactory) -> None:
    audit = factory.audit()
    audit.append_event(
        AuditEvent(
            audit_event_id=new_id("audit"),
            event_type="query",
            principal_id="user_001",
            record_ids=["obs_1", "obs_2"],
        )
    )
    listed = audit.list_events(principal_id="user_001")
    assert len(listed.items) == 1
    assert listed.items[0].event_type == "query"
    assert listed.items[0].record_ids == ["obs_1", "obs_2"]


def test_timeline_append_roundtrip(factory: SqliteRepositoryFactory) -> None:
    now = datetime.now(UTC)
    created = factory.timeline().append_event(
        AnalysisTimelineEvent(
            timeline_event_id="tl_001",
            trace_id="job_001",
            span_id="span_frame_select",
            analysis_job_id="job_001",
            scale_task_id="scale_001",
            unit_id="unit_001",
            video_id="video_001",
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            unit_kind="default_segment_window",
            segment_start_ms=0,
            segment_end_ms=12000,
            event_name="frame_select",
            event_phase="finish",
            status="succeeded",
            occurred_at=now,
            duration_ms=25,
            correlation={"vlm_request_id": "req_001"},
            metadata={"frames_selected": 6},
        )
    )
    assert created.created_at is not None

    listed = factory.timeline().list_by_job("job_001")
    assert [event.timeline_event_id for event in listed] == ["tl_001"]
    assert listed[0].correlation == {"vlm_request_id": "req_001"}
    assert listed[0].metadata == {"frames_selected": 6}
    assert listed[0].analysis_scale is AnalysisScale.DEFAULT_SEGMENT


def test_timeline_list_all_time_window_sqlite(factory: SqliteRepositoryFactory) -> None:
    repo = factory.timeline()
    t0 = datetime.fromisoformat("2026-06-24T10:00:00+00:00")
    t1 = datetime.fromisoformat("2026-06-24T10:01:00+00:00")
    t2 = datetime.fromisoformat("2026-06-24T10:02:00+00:00")
    for idx, (job_id, occurred_at) in enumerate(
        [("job_a", t0), ("job_b", t1), ("job_c", t2)], start=1
    ):
        repo.append_event(
            AnalysisTimelineEvent(
                timeline_event_id=f"tl_all_{idx}",
                trace_id=job_id,
                analysis_job_id=job_id,
                event_name="unit_running",
                event_phase="instant",
                occurred_at=occurred_at,
            )
        )

    listed = repo.list_all(since=t1, until=t2, limit=10)

    assert [event.timeline_event_id for event in listed] == ["tl_all_2", "tl_all_3"]


def test_search_context_revision_immutable(factory: SqliteRepositoryFactory) -> None:
    from cctv_memory.contracts.search import (
        SearchCandidate,
        SearchContext,
        SearchRevision,
    )
    from cctv_memory.domain.enums import ContextMode
    from cctv_memory.repositories.types import ConflictError

    repo = factory.search_context()
    repo.create_context(
        SearchContext(
            context_id="ctx_1",
            principal_id="user_001",
            authorized_scope_hash="h",
            dataset_revision="d",
            mode=ContextMode.SNAPSHOT,
            expires_at=datetime.fromisoformat(iso_in(900)),
        )
    )
    rev = SearchRevision(revision_id="rev_1", context_id="ctx_1", op="start", candidate_count=1)
    cand = SearchCandidate(revision_id="rev_1", record_id="obs_1", rank=1, score=0.5)
    repo.create_revision(rev, [cand])

    # Re-creating the same revision id must fail (immutable).
    with pytest.raises(ConflictError):
        repo.create_revision(rev, [cand])

    candidates = repo.list_candidates("rev_1")
    assert len(candidates.items) == 1
    assert candidates.items[0].record_id == "obs_1"
