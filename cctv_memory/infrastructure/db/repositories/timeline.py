"""Timeline repository adapters."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.infrastructure.db import mappers
from cctv_memory.infrastructure.db.models import tables as orm


class SqliteTimelineRepository:
    """SQLite adapter for append-only analysis timeline events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_event(self, event: AnalysisTimelineEvent) -> AnalysisTimelineEvent:
        row = mappers.timeline_event_to_orm(event)
        self._session.add(row)
        self._session.flush()
        return mappers.timeline_event_to_dto(row)

    def append_events(
        self, events: list[AnalysisTimelineEvent]
    ) -> list[AnalysisTimelineEvent]:
        return [self.append_event(event) for event in events]

    def list_by_job(
        self,
        analysis_job_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]:
        if limit <= 0:
            return []
        stmt = select(orm.AnalysisTimelineEvent).where(
            orm.AnalysisTimelineEvent.analysis_job_id == analysis_job_id
        )
        if since is not None:
            stmt = stmt.where(orm.AnalysisTimelineEvent.occurred_at >= since.isoformat())
        if until is not None:
            stmt = stmt.where(orm.AnalysisTimelineEvent.occurred_at <= until.isoformat())
        rows = self._session.scalars(
            stmt.order_by(
                orm.AnalysisTimelineEvent.occurred_at,
                orm.AnalysisTimelineEvent.created_at,
                orm.AnalysisTimelineEvent.timeline_event_id,
            ).limit(limit)
        )
        return [mappers.timeline_event_to_dto(row) for row in rows]

    def list_by_trace(
        self,
        trace_id: str,
        *,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]:
        if limit <= 0:
            return []
        rows = self._session.scalars(
            select(orm.AnalysisTimelineEvent)
            .where(orm.AnalysisTimelineEvent.trace_id == trace_id)
            .order_by(
                orm.AnalysisTimelineEvent.occurred_at,
                orm.AnalysisTimelineEvent.created_at,
                orm.AnalysisTimelineEvent.timeline_event_id,
            )
            .limit(limit)
        )
        return [mappers.timeline_event_to_dto(row) for row in rows]

    def list_all(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]:
        if limit <= 0:
            return []
        stmt = select(orm.AnalysisTimelineEvent)
        if since is not None:
            stmt = stmt.where(orm.AnalysisTimelineEvent.occurred_at >= since.isoformat())
        if until is not None:
            stmt = stmt.where(orm.AnalysisTimelineEvent.occurred_at <= until.isoformat())
        rows = self._session.scalars(
            stmt.order_by(
                orm.AnalysisTimelineEvent.occurred_at,
                orm.AnalysisTimelineEvent.created_at,
                orm.AnalysisTimelineEvent.timeline_event_id,
            ).limit(limit)
        )
        return [mappers.timeline_event_to_dto(row) for row in rows]
