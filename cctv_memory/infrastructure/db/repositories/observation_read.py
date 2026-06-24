"""SQLite read-only ObservationRecord adapter (search/read path).

This adapter implements ObservationRecordReadRepository and intentionally
exposes NO method to write active records. It enforces AuthorizedScope
fail-closed filtering (auth §4.1).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.infrastructure.db import fts, mappers
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.infrastructure.db.repositories._helpers import (
    authorized_observation_filter,
)
from cctv_memory.repositories.types import Page


class SqliteObservationReadRepository:
    """Read-only adapter for active observation records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_authorized_active_by_id(
        self, record_id: str, authorized_scope: AuthorizedScope
    ) -> ObservationRecord | None:
        row = self._session.scalar(
            select(orm.ObservationRecord).where(
                orm.ObservationRecord.record_id == record_id,
                authorized_observation_filter(authorized_scope),
            )
        )
        return mappers.observation_to_dto(row) if row else None

    def get_authorized_active_by_ids(
        self, record_ids: list[str], authorized_scope: AuthorizedScope
    ) -> list[ObservationRecord]:
        if not record_ids:
            return []
        rows = self._session.scalars(
            select(orm.ObservationRecord).where(
                orm.ObservationRecord.record_id.in_(record_ids),
                authorized_observation_filter(authorized_scope),
            )
        )
        return [mappers.observation_to_dto(r) for r in rows]

    def list_active_by_video(
        self,
        video_id: str,
        authorized_scope: AuthorizedScope,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ObservationRecord]:
        rows = list(
            self._session.scalars(
                select(orm.ObservationRecord)
                .where(
                    orm.ObservationRecord.video_id == video_id,
                    authorized_observation_filter(authorized_scope),
                )
                .order_by(orm.ObservationRecord.segment_start_ms)
                .limit(limit)
            )
        )
        return Page(items=[mappers.observation_to_dto(r) for r in rows])

    def count_authorized(self, authorized_scope: AuthorizedScope) -> int:
        result = self._session.scalar(
            select(func.count())
            .select_from(orm.ObservationRecord)
            .where(authorized_observation_filter(authorized_scope))
        )
        return int(result or 0)

    def search_authorized_candidates(
        self,
        authorized_scope: AuthorizedScope,
        *,
        query_text: str | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        camera_ids: list[str] | None = None,
        location_ids: list[str] | None = None,
        video_ids: list[str] | None = None,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        tag_filters: list[str] | None = None,
        limit: int = 100,
    ) -> list[ObservationRecord]:
        stmt = select(orm.ObservationRecord).where(
            authorized_observation_filter(authorized_scope)
        )
        if camera_ids:
            stmt = stmt.where(orm.ObservationRecord.camera_id.in_(camera_ids))
        if location_ids:
            stmt = stmt.where(orm.ObservationRecord.location_id.in_(location_ids))
        if video_ids:
            stmt = stmt.where(orm.ObservationRecord.video_id.in_(video_ids))
        if analysis_scale_filter:
            stmt = stmt.where(
                orm.ObservationRecord.analysis_scale.in_(
                    [s.value for s in analysis_scale_filter]
                )
            )
        if time_start is not None:
            stmt = stmt.where(
                orm.ObservationRecord.observed_end_time >= time_start.isoformat()
            )
        if time_end is not None:
            stmt = stmt.where(
                orm.ObservationRecord.observed_start_time <= time_end.isoformat()
            )
        if query_text:
            like = f"%{query_text.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(orm.ObservationRecord.static_description_text).like(like),
                    func.lower(orm.ObservationRecord.dynamic_description_text).like(like),
                    func.lower(orm.ObservationRecord.tags_json).like(like),
                )
            )
        if tag_filters:
            for tag in tag_filters:
                stmt = stmt.where(
                    func.lower(orm.ObservationRecord.tags_json).like(f"%{tag.lower()}%")
                )
        stmt = stmt.order_by(
            orm.ObservationRecord.observed_start_time,
            orm.ObservationRecord.record_id,
        ).limit(limit)
        rows = self._session.scalars(stmt)
        return [mappers.observation_to_dto(r) for r in rows]

    def authorized_candidate_pool(
        self,
        authorized_scope: AuthorizedScope,
        *,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        camera_ids: list[str] | None = None,
        location_ids: list[str] | None = None,
        video_ids: list[str] | None = None,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        tag_filters: list[str] | None = None,
        limit: int = 1000,
    ) -> list[ObservationRecord]:
        """Return the structurally-authorized candidate pool (no text matching).

        Applies AuthorizedScope + structural filters in SQL (fail closed). This is
        the candidate set that FTS/keyword ranking then operates within, so the
        scope pre-filter is always honored before ranking (search-contract §10).
        """
        stmt = select(orm.ObservationRecord).where(
            authorized_observation_filter(authorized_scope)
        )
        if camera_ids:
            stmt = stmt.where(orm.ObservationRecord.camera_id.in_(camera_ids))
        if location_ids:
            stmt = stmt.where(orm.ObservationRecord.location_id.in_(location_ids))
        if video_ids:
            stmt = stmt.where(orm.ObservationRecord.video_id.in_(video_ids))
        if analysis_scale_filter:
            stmt = stmt.where(
                orm.ObservationRecord.analysis_scale.in_(
                    [s.value for s in analysis_scale_filter]
                )
            )
        if time_start is not None:
            stmt = stmt.where(
                orm.ObservationRecord.observed_end_time >= time_start.isoformat()
            )
        if time_end is not None:
            stmt = stmt.where(
                orm.ObservationRecord.observed_start_time <= time_end.isoformat()
            )
        if tag_filters:
            for tag in tag_filters:
                stmt = stmt.where(
                    func.lower(orm.ObservationRecord.tags_json).like(f"%{tag.lower()}%")
                )
        stmt = stmt.order_by(
            orm.ObservationRecord.observed_start_time,
            orm.ObservationRecord.record_id,
        ).limit(limit)
        rows = self._session.scalars(stmt)
        return [mappers.observation_to_dto(r) for r in rows]

    def fts_rank(
        self, query_text: str, candidate_ids: list[str], *, field: str
    ) -> dict[str, float]:
        """Return {record_id: relevance} from FTS within ``candidate_ids``.

        ``field`` is one of ``static`` / ``dynamic`` / ``tags``. The match is
        restricted to the authorized candidate ids, so scope is never bypassed.
        Returns {} when FTS is unavailable or the query has no usable terms
        (callers fall back to LIKE-based scoring deterministically).
        """
        if field == "static":
            return fts.search_static(self._session, query_text, candidate_ids)
        if field == "dynamic":
            return fts.search_dynamic(self._session, query_text, candidate_ids)
        if field == "tags":
            return fts.search_tags(self._session, query_text, candidate_ids)
        return {}

    def fts_available(self) -> bool:
        """Return True if FTS5 virtual tables are present."""
        return fts.fts_available(self._session)

    def find_overlapping(
        self,
        record_id: str,
        authorized_scope: AuthorizedScope,
        *,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        time_padding_ms: int = 0,
        limit: int = 20,
    ) -> list[ObservationRecord]:
        target = self.get_authorized_active_by_id(record_id, authorized_scope)
        if target is None:
            return []
        lo = target.segment_start_ms - time_padding_ms
        hi = target.segment_end_ms + time_padding_ms
        stmt = select(orm.ObservationRecord).where(
            authorized_observation_filter(authorized_scope),
            orm.ObservationRecord.video_id == target.video_id,
            orm.ObservationRecord.record_id != record_id,
            orm.ObservationRecord.segment_start_ms < hi,
            orm.ObservationRecord.segment_end_ms > lo,
        )
        if analysis_scale_filter:
            stmt = stmt.where(
                orm.ObservationRecord.analysis_scale.in_(
                    [s.value for s in analysis_scale_filter]
                )
            )
        stmt = stmt.order_by(orm.ObservationRecord.segment_start_ms).limit(limit)
        rows = self._session.scalars(stmt)
        return [mappers.observation_to_dto(r) for r in rows]
