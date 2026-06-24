"""Repository contract tests: CRUD roundtrip + idempotency (testing-contract §3)."""

from __future__ import annotations

import pytest
from cctv_memory.contracts.analysis import AnalysisJob, HighFreqTrigger
from cctv_memory.contracts.auth import AccessPolicy, Principal
from cctv_memory.contracts.video import (
    SubmitVideoSourceRequest,
    VideoSource,
)
from cctv_memory.domain.enums import (
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.repositories.types import IdempotencyConflictError

from tests.conftest import new_id, seed_camera

_TZ = "2026-06-06T21:00:00+08:00"


def test_camera_crud_roundtrip(factory: SqliteRepositoryFactory) -> None:
    location, camera = seed_camera(factory)
    got_loc = factory.camera().get_location(location.location_id)
    got_cam = factory.camera().get_camera(camera.camera_id)
    assert got_loc is not None and got_loc.area == "lobby"
    assert got_cam is not None and got_cam.camera_name == "Lobby Cam"


def test_principal_crud_roundtrip(factory: SqliteRepositoryFactory) -> None:
    principal = Principal(
        principal_id="user_001",
        principal_type=PrincipalType.USER,
        display_name="Security User",
        roles=["security_viewer"],
        groups=["security_team"],
    )
    factory.principal().create_principal(principal)
    got = factory.principal().get_principal("user_001")
    assert got is not None
    assert got.roles == ["security_viewer"]
    assert got.groups == ["security_team"]


def test_access_policy_roundtrip(factory: SqliteRepositoryFactory) -> None:
    policy = AccessPolicy(
        access_policy_id="policy_public_area",
        name="Public area",
        security_level=SecurityLevel.INTERNAL,
    )
    factory.access_policy().upsert_access_policy(policy)
    got = factory.access_policy().get_access_policy("policy_public_area")
    assert got is not None
    assert got.security_level is SecurityLevel.INTERNAL


def test_video_source_idempotent_get(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    request = SubmitVideoSourceRequest(
        source_type=SourceType.FILE,
        source_uri="/data/videos/lobby.mp4",
        camera_id="cam_lobby_01",
        video_start_time=_dt(_TZ),
        external_source_id="nvr-001",
    )
    v1 = factory.video_source().create_or_get_by_idempotency(request, video_id=new_id("video"))
    v2 = factory.video_source().create_or_get_by_idempotency(request, video_id=new_id("video"))
    assert v1.video_id == v2.video_id  # same (camera_id, video_start_time)


def test_video_source_idempotency_conflict(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    base = SubmitVideoSourceRequest(
        source_type=SourceType.FILE,
        source_uri="/data/videos/lobby.mp4",
        camera_id="cam_lobby_01",
        video_start_time=_dt(_TZ),
    )
    factory.video_source().create_or_get_by_idempotency(base, video_id=new_id("video"))
    conflicting = SubmitVideoSourceRequest(
        source_type=SourceType.FILE,
        source_uri="/data/videos/DIFFERENT.mp4",
        camera_id="cam_lobby_01",
        video_start_time=_dt(_TZ),
    )
    with pytest.raises(IdempotencyConflictError):
        factory.video_source().create_or_get_by_idempotency(conflicting, video_id=new_id("video"))


def test_analysis_job_idempotency(factory: SqliteRepositoryFactory) -> None:
    job = AnalysisJob(
        analysis_job_id="job_001",
        video_id="video_001",
        job_status=JobStatus.QUEUED,
        idempotency_key="idem-1",
    )
    created = factory.analysis_job().create_job(job)
    assert created.analysis_job_id == "job_001"
    # Same idempotency key + same video_id returns the existing job.
    again = factory.analysis_job().create_job(
        AnalysisJob(
            analysis_job_id="job_002",
            video_id="video_001",
            idempotency_key="idem-1",
        )
    )
    assert again.analysis_job_id == "job_001"
    # Same key + different video_id is a conflict.
    with pytest.raises(IdempotencyConflictError):
        factory.analysis_job().create_job(
            AnalysisJob(
                analysis_job_id="job_003",
                video_id="video_OTHER",
                idempotency_key="idem-1",
            )
        )


def test_high_freq_trigger_idempotent(factory: SqliteRepositoryFactory) -> None:
    key = HighFreqTrigger.build_idempotency_key("job_001", "video_001", 1000, 2000, "motion_spike")
    trigger = HighFreqTrigger(
        trigger_id="trigger_001",
        analysis_job_id="job_001",
        scale_task_id="scale_001",
        video_id="video_001",
        trigger_start_ms=1000,
        trigger_end_ms=2000,
        trigger_reason="motion_spike",
        idempotency_key=key,
    )
    t1 = factory.trigger().create_or_get_by_idempotency(trigger)
    t2 = factory.trigger().create_or_get_by_idempotency(
        trigger.model_copy(update={"trigger_id": "trigger_999"})
    )
    assert t1.trigger_id == t2.trigger_id == "trigger_001"


def _dt(value: str) -> object:
    from datetime import datetime

    return datetime.fromisoformat(value)


def test_video_source_dto_has_no_orm(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    request = SubmitVideoSourceRequest(
        source_type=SourceType.FILE,
        source_uri="/data/videos/lobby.mp4",
        camera_id="cam_lobby_01",
        video_start_time=_dt(_TZ),
    )
    result = factory.video_source().create_or_get_by_idempotency(request, video_id="video_x")
    assert isinstance(result, VideoSource)  # DTO, not ORM model
