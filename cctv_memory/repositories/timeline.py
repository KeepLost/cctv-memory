"""TimelineRepository port for local analysis observability."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cctv_memory.contracts.timeline import AnalysisTimelineEvent


@runtime_checkable
class TimelineRepository(Protocol):
    """Append-only local analysis timeline events."""

    def append_event(self, event: AnalysisTimelineEvent) -> AnalysisTimelineEvent: ...

    def append_events(
        self, events: list[AnalysisTimelineEvent]
    ) -> list[AnalysisTimelineEvent]: ...

    def list_by_job(
        self,
        analysis_job_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]: ...

    def list_by_trace(
        self,
        trace_id: str,
        *,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]: ...

    def list_all(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]: ...
