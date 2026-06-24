"""Analysis job / scale task / unit / model-call contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import (
    AnalysisScale,
    JobStatus,
    ModelCallStatus,
    TaskStatus,
    TriggerStatus,
)


class AnalysisJob(ContractModel):
    """Analysis job (schema-contracts §3.4)."""

    analysis_job_id: str
    video_id: str
    job_status: JobStatus = JobStatus.QUEUED
    idempotency_key: str
    analysis_options: dict[str, bool] = Field(default_factory=dict)
    model_version: str | None = None
    prompt_version: str | None = None
    pipeline_version: str | None = None
    created_record_ids: list[str] = Field(default_factory=list)
    updated_record_ids: list[str] = Field(default_factory=list)
    archived_record_ids: list[str] = Field(default_factory=list)
    failed_segment_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class AnalysisScaleTask(ContractModel):
    """Analysis scale subtask (schema-contracts §3.5)."""

    scale_task_id: str
    analysis_job_id: str
    analysis_scale: AnalysisScale
    status: TaskStatus = TaskStatus.PENDING
    total_units: int = Field(default=0, ge=0)
    succeeded_units: int = Field(default=0, ge=0)
    failed_units: int = Field(default=0, ge=0)
    skipped_reason: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class AnalysisUnit(ContractModel):
    """Smallest schedulable/auditable VLM work unit inside a scale task."""

    unit_id: str
    analysis_job_id: str
    scale_task_id: str
    video_id: str
    analysis_scale: AnalysisScale
    unit_kind: str
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    window_index: int = Field(ge=0)
    trigger_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    last_error_code: str | None = None
    last_error_message: str | None = None
    latest_model_call_id: str | None = None
    successful_model_call_id: str | None = None
    produced_record_ids: list[str] = Field(default_factory=list)
    idempotency_key: str
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_unit(self) -> AnalysisUnit:
        if self.segment_start_ms >= self.segment_end_ms:
            raise ValueError("segment_start_ms must be strictly before segment_end_ms")
        return self


class ModelCallLog(ContractModel):
    """Auditable model-call record with textual IO and media refs/metadata only."""

    model_call_id: str
    analysis_job_id: str
    scale_task_id: str
    unit_id: str
    analysis_scale: AnalysisScale
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    provider: str
    model_id: str | None = None
    prompt_version: str | None = None
    pipeline_version: str | None = None
    status: ModelCallStatus | str = ModelCallStatus.RUNNING
    attempt_count: int = Field(default=0, ge=0)
    error_type: str | None = None
    error_message: str | None = None
    raw_text_input: str | None = None
    raw_text_output: str | None = None
    parsed_output: dict[str, Any] | None = None
    validation_status: str | None = None
    payload_hash: str | None = None
    response_hash: str | None = None
    media_refs: list[dict[str, Any]] = Field(default_factory=list)
    attempt_details: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None


class DetectorGateLog(ContractModel):
    """Auditable per-window detector gate log."""

    gate_log_id: str
    analysis_job_id: str
    scale_task_id: str
    unit_id: str
    video_id: str
    analysis_scale: AnalysisScale
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    provider: str
    model_id: str | None = None
    status: str = "succeeded"
    decision: dict[str, Any] = Field(default_factory=dict)
    frame_evidence: list[dict[str, Any]] = Field(default_factory=list)
    evidence_hash: str
    rule_config_hash: str | None = None
    media_refs: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_gate_log(self) -> DetectorGateLog:
        if self.segment_start_ms >= self.segment_end_ms:
            raise ValueError("segment_start_ms must be strictly before segment_end_ms")
        return self


class HighFreqTrigger(ContractModel):
    """High-frequency trigger (schema-contracts §3.6).

    The idempotency key format is fixed (questions.md Q2,
    job-state-machine-contract §3) as::

        analysis_job_id:video_id:trigger_start_ms:trigger_end_ms:trigger_reason
    """

    trigger_id: str
    analysis_job_id: str
    scale_task_id: str
    video_id: str
    trigger_start_ms: int = Field(ge=0)
    trigger_end_ms: int = Field(ge=0)
    motion_score: float | None = None
    change_score: float | None = None
    trigger_reason: str
    status: TriggerStatus = TriggerStatus.PENDING
    idempotency_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> HighFreqTrigger:
        if self.trigger_start_ms >= self.trigger_end_ms:
            raise ValueError("trigger_start_ms must be strictly before trigger_end_ms")
        expected = self.build_idempotency_key(
            self.analysis_job_id,
            self.video_id,
            self.trigger_start_ms,
            self.trigger_end_ms,
            self.trigger_reason,
        )
        if self.idempotency_key != expected:
            raise ValueError(
                "HighFreqTrigger.idempotency_key must equal "
                "'analysis_job_id:video_id:trigger_start_ms:trigger_end_ms:trigger_reason'"
            )
        return self

    @staticmethod
    def build_idempotency_key(
        analysis_job_id: str,
        video_id: str,
        trigger_start_ms: int,
        trigger_end_ms: int,
        trigger_reason: str,
    ) -> str:
        """Build the canonical idempotency key (includes video_id)."""
        return (
            f"{analysis_job_id}:{video_id}:{trigger_start_ms}:"
            f"{trigger_end_ms}:{trigger_reason}"
        )
