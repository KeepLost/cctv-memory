"""Analysis timeline observability contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import AnalysisScale

TimelineEventPhase = Literal["instant", "start", "finish", "fail"]


class AnalysisTimelineEvent(ContractModel):
    """Append-only local analysis timeline event.

    Observability-only evidence: not authoritative business state.
    """

    timeline_event_id: str
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    analysis_job_id: str | None = None
    task_id: str | None = None
    scale_task_id: str | None = None
    unit_id: str | None = None
    model_call_id: str | None = None
    video_id: str | None = None
    analysis_scale: AnalysisScale | None = None
    unit_kind: str | None = None
    segment_start_ms: int | None = Field(default=None, ge=0)
    segment_end_ms: int | None = Field(default=None, ge=0)
    event_name: str
    event_phase: TimelineEventPhase
    status: str | None = None
    attempt_count: int | None = Field(default=None, ge=0)
    occurred_at: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    correlation: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_timeline_event(self) -> AnalysisTimelineEvent:
        if self.segment_start_ms is not None and self.segment_end_ms is not None:
            if self.segment_start_ms >= self.segment_end_ms:
                raise ValueError("segment_start_ms must be before segment_end_ms")
        for label, value in (
            ("occurred_at", self.occurred_at),
            ("created_at", self.created_at),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"AnalysisTimelineEvent.{label} must be timezone-aware")
        return self
