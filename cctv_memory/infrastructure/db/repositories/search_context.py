"""SQLite SearchContext adapter (context/revision/candidate persistence).

Revisions are immutable: ``create_revision`` inserts a new revision and its
candidates; existing revisions are never mutated (search-contract §4.2).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cctv_memory.contracts.search import (
    SearchCandidate,
    SearchContext,
    SearchRevision,
)
from cctv_memory.infrastructure.db import mappers
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.repositories.types import ConflictError, Page


class SqliteSearchContextRepository:
    """SearchContextRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_context(self, context: SearchContext) -> SearchContext:
        self._session.add(mappers.context_to_orm(context))
        self._session.flush()
        return context

    def get_context(self, context_id: str) -> SearchContext | None:
        row = self._session.get(orm.SearchContext, context_id)
        return mappers.context_to_dto(row) if row else None

    def close_context(self, context_id: str) -> None:
        row = self._session.get(orm.SearchContext, context_id)
        if row is not None:
            row.status = "closed"
            self._session.flush()

    def expire_contexts(self, now: datetime) -> int:
        # SQLite stores expires_at as ISO text; convert at the adapter boundary.
        now_iso = now.isoformat()
        rows = list(
            self._session.scalars(
                select(orm.SearchContext).where(
                    orm.SearchContext.status == "active",
                    orm.SearchContext.expires_at < now_iso,
                )
            )
        )
        for row in rows:
            row.status = "expired"
        self._session.flush()
        return len(rows)

    def create_revision(
        self, revision: SearchRevision, candidates: list[SearchCandidate]
    ) -> SearchRevision:
        existing = self._session.get(orm.SearchRevision, revision.revision_id)
        if existing is not None:
            raise ConflictError(f"Revision {revision.revision_id} already exists (immutable)")
        self._session.add(mappers.revision_to_orm(revision))
        for candidate in candidates:
            self._session.add(mappers.candidate_to_orm(candidate))
        self._session.flush()
        return revision

    def get_revision(self, revision_id: str) -> SearchRevision | None:
        row = self._session.get(orm.SearchRevision, revision_id)
        return mappers.revision_to_dto(row) if row else None

    def count_revisions(self, context_id: str) -> int:
        result = self._session.scalar(
            select(func.count())
            .select_from(orm.SearchRevision)
            .where(orm.SearchRevision.context_id == context_id)
        )
        return int(result or 0)

    def list_candidates(
        self, revision_id: str, cursor: str | None = None, limit: int = 50
    ) -> Page[SearchCandidate]:
        rows = list(
            self._session.scalars(
                select(orm.SearchCandidate)
                .where(orm.SearchCandidate.revision_id == revision_id)
                .order_by(orm.SearchCandidate.rank)
                .limit(limit)
            )
        )
        return Page(items=[mappers.candidate_to_dto(r) for r in rows])

    def replace_default_revision(self, context_id: str, revision_id: str) -> None:
        row = self._session.get(orm.SearchContext, context_id)
        if row is not None:
            row.default_revision_id = revision_id
            row.last_accessed_at = datetime.now().astimezone().isoformat()
            self._session.flush()
