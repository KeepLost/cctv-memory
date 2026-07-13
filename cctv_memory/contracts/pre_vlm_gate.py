"""Contracts for the generic pre-VLM gate."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import AnalysisScale


PreVlmGateSchemaVersion = Literal["pre_vlm_gate_v1"]
GateSignalType = Literal["object_detection", "motion", "quality", "custom"]
GateRuleAction = Literal["call_vlm", "suppress_vlm"]
GateSuppressionPolicy = Literal["publish_gate_only_record", "skip_without_record"]
GateDecisionAction = Literal["call_vlm", "suppress_vlm", "disabled", "force_vlm"]


class GateFrameInput(ContractModel):
    uri: str
    frame_index: int | None = Field(default=None, ge=0)
    timestamp_ms: int = Field(ge=0)
    frame_hash: str | None = None
    width_px: int | None = Field(default=None, gt=0)
    height_px: int | None = Field(default=None, gt=0)
    mime_type: str | None = None


class GateRule(ContractModel):
    rule_id: str | None = None
    signal_type: GateSignalType = "object_detection"
    label: str
    min_positive_frame_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    action: GateRuleAction = "call_vlm"


class GateProfile(ContractModel):
    profile_name: str
    enabled: bool = False
    analysis_scale: AnalysisScale
    suppression_policy: GateSuppressionPolicy
    provider: str = "mock"
    model_id: str | None = None
    rules: list[GateRule] = Field(default_factory=list)
    force_vlm_on_trigger_reasons: list[str] = Field(default_factory=list)


class GateSignal(ContractModel):
    signal_type: GateSignalType
    provider: str
    model_id: str | None = None
    status: str = "succeeded"
    frame_count: int = Field(default=0, ge=0)
    summary: dict[str, Any] = Field(default_factory=dict)
    frame_evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class PreVlmGateRequest(ContractModel):
    schema_version: PreVlmGateSchemaVersion = "pre_vlm_gate_v1"
    request_id: str
    gate_log_id: str
    analysis_job_id: str
    scale_task_id: str
    unit_id: str
    video_id: str
    analysis_scale: AnalysisScale
    unit_kind: str
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    provider: str
    model_id: str | None = None
    profile: GateProfile
    frames: list[GateFrameInput]
    trigger_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_request(self) -> PreVlmGateRequest:
        if self.segment_start_ms >= self.segment_end_ms:
            raise ValueError("segment_start_ms must be strictly before segment_end_ms")
        if self.profile.analysis_scale != self.analysis_scale:
            raise ValueError("profile.analysis_scale must match request.analysis_scale")
        return self


class PreVlmGateDecision(ContractModel):
    schema_version: PreVlmGateSchemaVersion = "pre_vlm_gate_v1"
    triggered_vlm: bool
    action: GateDecisionAction
    matched_rules: list[str] = Field(default_factory=list)
    positive_frame_ratio_by_label: dict[str, float] = Field(default_factory=dict)
    reason: str
    evidence_hash: str
    rule_config_hash: str | None = None
    suppression_policy: GateSuppressionPolicy | None = None


class GateDecisionBundle(ContractModel):
    decision: PreVlmGateDecision
    signals: list[GateSignal] = Field(default_factory=list)
    frame_evidence: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def triggered_vlm(self) -> bool:
        return self.decision.triggered_vlm


class PreVlmGateLog(ContractModel):
    gate_log_id: str
    analysis_job_id: str
    scale_task_id: str
    unit_id: str
    video_id: str
    analysis_scale: AnalysisScale
    unit_kind: str
    profile_name: str
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    provider: str
    model_id: str | None = None
    status: str = "succeeded"
    decision: dict[str, Any] = Field(default_factory=dict)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    frame_evidence: list[dict[str, Any]] = Field(default_factory=list)
    evidence_hash: str
    rule_config_hash: str | None = None
    suppression_policy: GateSuppressionPolicy | None = None
    media_refs: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_log(self) -> PreVlmGateLog:
        if self.segment_start_ms >= self.segment_end_ms:
            raise ValueError("segment_start_ms must be strictly before segment_end_ms")
        return self
