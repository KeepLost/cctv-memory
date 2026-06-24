"""SQLite Audit adapter (append/list)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.infrastructure.db import mappers
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.repositories.types import Page


class SqliteAuditRepository:
    """AuditRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_event(self, event: AuditEvent) -> AuditEvent:
        self._session.add(mappers.audit_to_orm(event))
        self._session.flush()
        return event

    def list_events(
        self,
        principal_id: str | None = None,
        event_type: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[AuditEvent]:
        stmt = select(orm.AuditEvent)
        if principal_id is not None:
            stmt = stmt.where(orm.AuditEvent.principal_id == principal_id)
        if event_type is not None:
            stmt = stmt.where(orm.AuditEvent.event_type == event_type)
        stmt = stmt.order_by(orm.AuditEvent.created_at).limit(limit)
        rows = list(self._session.scalars(stmt))
        return Page(items=[mappers.audit_to_dto(r) for r in rows])
