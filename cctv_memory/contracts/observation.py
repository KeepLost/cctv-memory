"""Observation record contracts (schema-contracts §3.7-§3.8).

System-derived fields (timing, camera, location, policy, security_level) are
never set by VLM output (ARCHITECTURE_CONSTITUTION §5, schema-contracts §3.7).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel


class ObservationRecord(ContractModel):
    """Active observation record (schema-contracts §3.7)."""

    record_id: str
    tenant_id: str = "tenant_default"
    video_id: str
    analysis_job_id: str
    analysis_scale: AnalysisScale
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    observed_start_time: datetime
    observed_end_time: datetime
    camera_id: str
    location_id: str
    static_description_text: str
    dynamic_description_text: str
    tags: list[str] = Field(default_factory=list)
    clip_uri: str | None = None
    thumbnail_uri: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    access_policy_id: str
    security_level: SecurityLevel
    model_version: str | None = None
    prompt_version: str | None = None
    pipeline_version: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> ObservationRecord:
        if self.segment_start_ms >= self.segment_end_ms:
            raise ValueError("segment_start_ms must be strictly before segment_end_ms")
        return self


class ObservationRecordHistory(ContractModel):
    """Archived observation record (schema-contracts §3.8)."""

    history_id: str
    old_record_id: str
    replaced_by_record_id: str | None = None
    archived_by_analysis_job_id: str
    archived_at: datetime
    archive_reason: str
    record_snapshot: dict[str, Any] = Field(default_factory=dict)
