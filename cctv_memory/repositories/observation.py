"""ObservationRecord ports (repository-port-contract §7).

Read methods require an AuthorizedScope and hide unauthorized rows. The write
method (atomic publication) is exposed on a SEPARATE port so the search/read
path has no way to write active records (write_path_separation).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import (
    PublicationResult,
    PublishObservationRecordsCommand,
)
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.repositories.types import Page


@runtime_checkable
class ObservationRecordReadRepository(Protocol):
    """Read-only port for active observation records (repository-port-contract §7.1).

    User-visible reads apply AuthorizedScope; unauthorized records are hidden
    (return ``None`` / empty), never leaked. This port intentionally exposes NO
    write method.
    """

    def get_authorized_active_by_id(
        self, record_id: str, authorized_scope: AuthorizedScope
    ) -> ObservationRecord | None: ...

    def get_authorized_active_by_ids(
        self, record_ids: list[str], authorized_scope: AuthorizedScope
    ) -> list[ObservationRecord]: ...

    def list_active_by_video(
        self,
        video_id: str,
        authorized_scope: AuthorizedScope,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[ObservationRecord]: ...

    def count_authorized(self, authorized_scope: AuthorizedScope) -> int: ...

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
        """Return authorized candidate records matching structured filters.

        AuthorizedScope is applied in SQL before any ranking (search-contract §10,
        database-capability-contract §5/§6). ``query_text`` does a case-insensitive
        LIKE over static/dynamic text and tags (MVP; no vector/FTS ranking here).
        The empty-allowed-list fail-closed behavior is inherited from the scope
        filter. Ranking is the caller's (application) responsibility.
        """
        ...

    def find_overlapping(
        self,
        record_id: str,
        authorized_scope: AuthorizedScope,
        *,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        time_padding_ms: int = 0,
        limit: int = 20,
    ) -> list[ObservationRecord]:
        """Return authorized records whose segment overlaps the target record.

        If the target record is itself unauthorized, returns an empty list
        (search-contract §7). The target is excluded from its own results.
        """
        ...

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

        AuthorizedScope + structural filters applied in SQL (fail closed). This is
        the pool FTS/keyword ranking operates within (search-contract §10).
        """
        ...

    def fts_rank(
        self, query_text: str, candidate_ids: list[str], *, field: str
    ) -> dict[str, float]:
        """Return {record_id: relevance} from FTS within the candidate id set.

        ``field`` is ``static`` / ``dynamic`` / ``tags``. Restricted to the
        authorized candidate ids so scope is never bypassed. {} when FTS is
        unavailable or no usable terms (callers fall back to LIKE scoring).
        """
        ...

    def fts_available(self) -> bool:
        """Return True if the FTS5 virtual tables are present."""
        ...


@runtime_checkable
class ObservationRecordPublicationRepository(Protocol):
    """Publication-only port (repository-port-contract §7.2).

    The single business path allowed to write active ObservationRecord. The
    implementation must perform the publication atomically (upsert active +
    archive replaced + update job summary + audit append) within one
    transaction (ARCHITECTURE_CONSTITUTION §6).
    """

    def publish_records_atomically(
        self, command: PublishObservationRecordsCommand
    ) -> PublicationResult: ...
