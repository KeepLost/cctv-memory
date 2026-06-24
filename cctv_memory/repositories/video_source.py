"""VideoSourceRepository port (repository-port-contract §3)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.video import SubmitVideoSourceRequest, VideoSource
from cctv_memory.repositories.types import Page


@runtime_checkable
class VideoSourceRepository(Protocol):
    """Video source persistence port.

    Constraint: ``(camera_id, video_start_time)`` is unique. source_uri
    canonicalization happens in the ingestion service, not here.
    """

    def create_or_get_by_idempotency(
        self, request: SubmitVideoSourceRequest, *, video_id: str
    ) -> VideoSource: ...

    def get_by_id(self, video_id: str) -> VideoSource | None: ...

    def get_authorized_by_id(
        self, video_id: str, authorized_scope: AuthorizedScope
    ) -> VideoSource | None: ...

    def list_authorized(
        self, authorized_scope: AuthorizedScope, cursor: str | None = None, limit: int = 50
    ) -> Page[VideoSource]: ...

    def mark_status(self, video_id: str, status: str, error: str | None = None) -> None: ...

    def update_probe_metadata(
        self, video_id: str, *, duration_ms: int, video_end_time: datetime
    ) -> None:
        """Persist probed duration and computed end time (worker fills these in)."""
        ...
