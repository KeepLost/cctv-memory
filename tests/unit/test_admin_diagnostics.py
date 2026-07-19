from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cctv_memory.application.admin_diagnostics import AdminDiagnosticsService
from cctv_memory.contracts.analysis import (
    AnalysisJob,
    AnalysisScaleTask,
    AnalysisUnit,
    ModelCallLog,
)
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.pre_vlm_gate import PreVlmGateLog
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    JobStatus,
    ModelCallStatus,
    SecurityLevel,
    TaskStatus,
)
from cctv_memory.domain.exceptions import CapabilityDeniedError


def _scope(*capabilities: Capability) -> AuthorizedScope:
    return AuthorizedScope(
        tenant_id="tenant_default",
        principal_id="admin_1",
        allowed_camera_ids=["cam_1"],
        allowed_location_ids=["loc_1"],
        allowed_access_policy_ids=["policy_1"],
        max_security_level=SecurityLevel.RESTRICTED,
        capabilities=list(capabilities),
        scope_hash="scope",
    )


def test_admin_diagnostics_requires_runtime_manage() -> None:
    service = AdminDiagnosticsService(None, None, None, None, None)  # type: ignore[arg-type]
    with pytest.raises(CapabilityDeniedError):
        service.failure_details("job_1", _scope(Capability.OBSERVATION_SEARCH))


def test_admin_diagnostics_returns_model_and_gate_failures() -> None:
    now = datetime.now(UTC)
    job = AnalysisJob(
        analysis_job_id="job_1",
        video_id="video_1",
        job_status=JobStatus.FAILED,
        idempotency_key="job-key",
        created_at=now,
    )
    scale = AnalysisScaleTask(
        scale_task_id="scale_1",
        analysis_job_id="job_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        status=TaskStatus.FAILED,
        created_at=now,
    )
    unit = AnalysisUnit(
        unit_id="unit_1",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        video_id="video_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        segment_start_ms=0,
        segment_end_ms=12000,
        window_index=0,
        status=TaskStatus.FAILED,
        last_error_code="vlm_schema_validation_failed",
        idempotency_key="unit-key",
        created_at=now,
    )
    model_log = ModelCallLog(
        model_call_id="mcall_1",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        unit_id="unit_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=12000,
        provider="real",
        status=ModelCallStatus.FAILED,
        raw_text_output='{"bad": true}',
    )
    gate_log = PreVlmGateLog(
        gate_log_id="pgate_1",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        unit_id="unit_1",
        video_id="video_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        profile_name="default_segment",
        segment_start_ms=0,
        segment_end_ms=12000,
        provider="google_vision",
        status="failed",
        raw_text_output='{"not_responses": []}',
        evidence_hash="sha256:schema_failure",
    )

    class Jobs:
        def get_job(self, _job_id: str) -> AnalysisJob:
            return job

    class Scales:
        def list_by_job(self, _job_id: str) -> list[AnalysisScaleTask]:
            return [scale]

    class Units:
        def list_by_scale_task(self, _scale_id: str) -> list[AnalysisUnit]:
            return [unit]

    class ModelLogs:
        def __init__(self, rows: list[ModelCallLog]) -> None:
            self._rows = rows

        def list_by_job(self, _job_id: str) -> list[ModelCallLog]:
            return self._rows

    class GateLogs:
        def __init__(self, rows: list[PreVlmGateLog]) -> None:
            self._rows = rows

        def list_by_job(self, _job_id: str) -> list[PreVlmGateLog]:
            return self._rows

    service = AdminDiagnosticsService(
        Jobs(), Scales(), Units(), ModelLogs([model_log]), GateLogs([gate_log])  # type: ignore[arg-type]
    )
    result = service.failure_details("job_1", _scope(Capability.RUNTIME_MANAGE))
    dumped = result.model_dump()
    assert dumped["model_call_logs"][0]["raw_text_output"] == '{"bad": true}'
    assert dumped["pre_vlm_gate_logs"][0]["raw_text_output"] == '{"not_responses": []}'
