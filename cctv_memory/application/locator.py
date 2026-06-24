"""Observation details + locator use case (application/locator.py).

- ``get_details``: authorized read of records; optional locator projection.
- Locator generation performs a SECOND authorization check (the read itself is
  scope-filtered) and NEVER exposes the internal ``source_uri``
  (ARCHITECTURE_CONSTITUTION §5, authorization-policy-contract §8). ``playback_url``
  is a short-TTL placeholder token in the MVP (no real playback endpoint).
- ``get_overlapping``: authorized overlapping records (search-contract §7).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.search import (
    LocatorProjection,
    ObservationDetailsItem,
    ObservationDetailsRequest,
    OverlappingRecordsRequest,
    SearchResultItem,
)
from cctv_memory.domain.enums import Capability
from cctv_memory.domain.exceptions import CapabilityDeniedError
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.observation import ObservationRecordReadRepository

_LOCATOR_TTL_SECONDS = 600


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LocatorService:
    """Authorized details + locator projection + overlap."""

    def __init__(
        self,
        observations: ObservationRecordReadRepository,
        audit: AuditRepository,
    ) -> None:
        self._observations = observations
        self._audit = audit

    def get_details(
        self,
        request: ObservationDetailsRequest,
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
    ) -> list[ObservationDetailsItem]:
        if Capability.OBSERVATION_READ_DETAIL not in scope.capabilities:
            raise CapabilityDeniedError("observation.read_detail required")

        # Locator is a privileged projection: requesting it requires a SECOND,
        # distinct capability (observation.read_locator) checked up front
        # (authorization-policy-contract §8). The per-record authorization is then
        # re-verified in _build_locator before any locator is emitted.
        if request.include_locator and (
            Capability.OBSERVATION_READ_LOCATOR not in scope.capabilities
        ):
            raise CapabilityDeniedError("observation.read_locator required for locator")

        records = self._observations.get_authorized_active_by_ids(
            request.record_ids, scope
        )
        items: list[ObservationDetailsItem] = []
        for rec in records:
            locator: LocatorProjection | None = None
            if request.include_locator:
                locator = self._build_locator(rec, scope)
            items.append(
                ObservationDetailsItem(
                    record_id=rec.record_id,
                    static_description_text=rec.static_description_text,
                    dynamic_description_text=rec.dynamic_description_text,
                    tags=rec.tags,
                    attributes=rec.attributes,
                    analysis_scale=rec.analysis_scale,
                    observed_start_time=rec.observed_start_time,
                    observed_end_time=rec.observed_end_time,
                    locator=locator,
                )
            )

        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type="details",
                request_id=request_id,
                principal_id=scope.principal_id,
                resource_scope_hash=scope.scope_hash,
                record_ids=[i.record_id for i in items],
                metadata={"include_locator": request.include_locator},
            )
        )
        return items

    def _build_locator(
        self, rec: ObservationRecord, scope: AuthorizedScope
    ) -> LocatorProjection | None:
        """Build a locator projection. No source_uri ever (constitution §5).

        Second authorization: re-verify the record is within the current
        AuthorizedScope before emitting any locator. Returns ``None`` if the
        record is no longer authorized (defense in depth — callers omit it).
        ``playback_url`` is a short-TTL placeholder token; there is no real
        playback endpoint in the MVP.
        """
        # Re-check the record against the live scope (second authorization).
        reauthorized = self._observations.get_authorized_active_by_id(
            rec.record_id, scope
        )
        if reauthorized is None:
            return None
        now = _now()
        token = _new_id("pbt")
        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type="locator",
                principal_id=scope.principal_id,
                resource_scope_hash=scope.scope_hash,
                record_ids=[rec.record_id],
                video_id=rec.video_id,
                camera_id=rec.camera_id,
                metadata={"playback_token": token, "placeholder": True},
            )
        )
        return LocatorProjection(
            video_id=rec.video_id,
            camera_id=rec.camera_id,
            segment_start_ms=rec.segment_start_ms,
            segment_end_ms=rec.segment_end_ms,
            absolute_start_time=rec.observed_start_time,
            absolute_end_time=rec.observed_end_time,
            playback_url=f"/api/v1/playback/{token}",
            thumbnail_url=None,
            expires_at=now + timedelta(seconds=_LOCATOR_TTL_SECONDS),
        )

    def get_overlapping(
        self,
        request: OverlappingRecordsRequest,
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
    ) -> list[SearchResultItem]:
        if Capability.OBSERVATION_READ_DETAIL not in scope.capabilities:
            raise CapabilityDeniedError("observation.read_detail required")
        records = self._observations.find_overlapping(
            request.record_id,
            scope,
            analysis_scale_filter=request.analysis_scale_filter or None,
            time_padding_ms=request.time_padding_ms,
            limit=request.top_k,
        )
        return [
            SearchResultItem(
                record_id=rec.record_id,
                rank=i + 1,
                score=0.0,
                score_detail={"overlap": True},
                preview_text=rec.static_description_text[:200],
                analysis_scale=rec.analysis_scale,
                observed_start_time=rec.observed_start_time,
                observed_end_time=rec.observed_end_time,
            )
            for i, rec in enumerate(records)
        ]
