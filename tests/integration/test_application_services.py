"""Application-service tests for ingestion, auth-scope, orchestration, publication."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cctv_memory.application.analysis_orchestrator import AnalysisOrchestrator
from cctv_memory.application.auth import AuthorizationService
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.domain.enums import (
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
)
from cctv_memory.domain.exceptions import (
    CapabilityDeniedError,
    InvalidStateTransitionError,
)
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory

from tests.conftest import seed_camera


def _seed_principal_and_policy(factory: SqliteRepositoryFactory) -> Principal:
    factory.access_policy().upsert_access_policy(
        AccessPolicy(
            access_policy_id="policy_public_area",
            name="Public Area",
            security_level=SecurityLevel.INTERNAL,
            rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
        )
    )
    principal = Principal(
        principal_id="user_001",
        principal_type=PrincipalType.SERVICE_ACCOUNT,
        display_name="Service",
        roles=["security_viewer"],
    )
    factory.principal().create_principal(principal)
    return principal


def _ingestion(factory: SqliteRepositoryFactory) -> IngestionService:
    return IngestionService(
        factory.video_source(),
        factory.analysis_job(),
        factory.scale_task(),
        factory.task_queue(),
        factory.audit(),
    )


def _submit_request() -> SubmitVideoSourceRequest:
    return SubmitVideoSourceRequest(
        source_type=SourceType.FILE,
        source_uri="/data/videos/lobby.mp4",
        camera_id="cam_lobby_01",
        video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
        external_source_id="nvr-export-001",
        idempotency_key="nvr-export-001",
        analysis_options={"enable_default_segment": True},
    )


def test_ingestion_creates_source_job_scaletask_and_task(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    principal = _seed_principal_and_policy(factory)
    svc = _ingestion(factory)

    resp = svc.submit(_submit_request(), principal, capabilities=[Capability.ANALYSIS_SUBMIT])

    assert resp.accepted
    assert resp.analysis_job_id
    job = factory.analysis_job().get_job(resp.analysis_job_id)
    assert job is not None
    assert job.job_status is JobStatus.QUEUED
    scale = factory.scale_task().get_by_job_and_scale(
        resp.analysis_job_id, "default_segment"
    )
    assert scale is not None
    pending = factory.task_queue().list_pending().items
    assert len(pending) == 1
    assert pending[0].payload["analysis_job_id"] == resp.analysis_job_id


def test_ingestion_is_idempotent_on_resubmit(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    principal = _seed_principal_and_policy(factory)
    svc = _ingestion(factory)
    first = svc.submit(_submit_request(), principal, capabilities=[Capability.ANALYSIS_SUBMIT])
    second = svc.submit(_submit_request(), principal, capabilities=[Capability.ANALYSIS_SUBMIT])
    assert first.analysis_job_id == second.analysis_job_id
    assert first.video_id == second.video_id


def test_ingestion_requires_capability(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    principal = _seed_principal_and_policy(factory)
    svc = _ingestion(factory)
    with pytest.raises(CapabilityDeniedError):
        svc.submit(_submit_request(), principal, capabilities=[Capability.OBSERVATION_SEARCH])


def test_authorization_scope_is_fail_closed_for_unpermitted_principal(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    # Policy that this principal does NOT match (different role).
    factory.access_policy().upsert_access_policy(
        AccessPolicy(
            access_policy_id="policy_public_area",
            name="Public Area",
            security_level=SecurityLevel.INTERNAL,
            rules=AccessPolicyRules(allowed_roles=["lab_manager"]),
        )
    )
    principal = Principal(
        principal_id="user_x",
        principal_type=PrincipalType.USER,
        display_name="Outsider",
        roles=["random_role"],
    )
    factory.principal().create_principal(principal)
    auth = AuthorizationService(
        factory.principal(), factory.access_policy(), factory.camera()
    )
    scope = auth.authorized_scope_for(principal)
    assert scope.allowed_camera_ids == []
    assert scope.allowed_location_ids == []
    assert scope.allowed_access_policy_ids == []


def test_authorization_scope_grants_matching_principal(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    principal = _seed_principal_and_policy(factory)
    auth = AuthorizationService(
        factory.principal(), factory.access_policy(), factory.camera()
    )
    scope = auth.authorized_scope_for(principal)
    assert "cam_lobby_01" in scope.allowed_camera_ids
    assert "loc_lobby_01" in scope.allowed_location_ids
    assert "policy_public_area" in scope.allowed_access_policy_ids


def test_orchestrator_rejects_invalid_transition(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    principal = _seed_principal_and_policy(factory)
    svc = _ingestion(factory)
    resp = svc.submit(_submit_request(), principal, capabilities=[Capability.ANALYSIS_SUBMIT])
    orch = AnalysisOrchestrator(factory.analysis_job(), factory.scale_task())
    # queued -> succeeded is illegal (must go through running).
    with pytest.raises(InvalidStateTransitionError):
        orch.transition_job(resp.analysis_job_id, JobStatus.SUCCEEDED)


def test_orchestrator_applies_legal_transition(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    principal = _seed_principal_and_policy(factory)
    svc = _ingestion(factory)
    resp = svc.submit(_submit_request(), principal, capabilities=[Capability.ANALYSIS_SUBMIT])
    orch = AnalysisOrchestrator(factory.analysis_job(), factory.scale_task())
    orch.transition_job(resp.analysis_job_id, JobStatus.RUNNING)
    job = factory.analysis_job().get_job(resp.analysis_job_id)
    assert job is not None
    assert job.job_status is JobStatus.RUNNING
    assert job.started_at is not None
    scale = factory.scale_task().get_by_job_and_scale(
        resp.analysis_job_id, "default_segment"
    )
    assert scale is not None
    orch.transition_scale_task(scale.scale_task_id, TaskStatus.RUNNING)
    refreshed = factory.scale_task().get_scale_task(scale.scale_task_id)
    assert refreshed is not None
    assert refreshed.status is TaskStatus.RUNNING
