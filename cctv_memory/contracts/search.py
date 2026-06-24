"""Search contracts (schema-contracts §5.3-§5.5, §9; search-contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from cctv_memory.contracts.common import ContractModel, TimeRange
from cctv_memory.domain.enums import (
    AnalysisScale,
    ContextMode,
    RefineOp,
    ScaleStrategy,
    SearchMode,
)


class StartObservationSearchRequest(ContractModel):
    """Start-search request (schema-contracts §5.3).

    Includes ``video_ids`` (structured filter), ``analysis_scale_filter``
    (hard filter), and ``scale_strategy`` (preference) per the resolved
    doc-drift items (questions.md).
    """

    query_text: str | None = None
    time_range: TimeRange | None = None
    camera_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    tag_filters: list[str] = Field(default_factory=list)
    preferred_text_fields: list[str] = Field(default_factory=list)
    analysis_scale_filter: list[AnalysisScale] = Field(default_factory=list)
    preferred_analysis_scales: list[AnalysisScale] = Field(default_factory=list)
    scale_strategy: ScaleStrategy | None = None
    search_mode: SearchMode = SearchMode.HYBRID
    top_k: int = Field(default=50, ge=1, le=100)
    score_threshold: float | None = None


class SearchResultItem(ContractModel):
    """Search result item (schema-contracts §5.4)."""

    record_id: str
    rank: int = Field(ge=1)
    score: float
    score_detail: dict[str, Any] = Field(default_factory=dict)
    preview_text: str | None = None
    analysis_scale: AnalysisScale
    observed_start_time: datetime
    observed_end_time: datetime


class StartObservationSearchResponse(ContractModel):
    """Start-search response (schema-contracts §5.5)."""

    context_id: str
    revision_id: str
    candidate_count: int = Field(ge=0)
    facets: dict[str, Any] = Field(default_factory=dict)
    results: list[SearchResultItem] = Field(default_factory=list)


class RefineObservationSearchRequest(ContractModel):
    """Refine-search request (schema-contracts §5.6, search-contract §3.2)."""

    base_revision_id: str
    op: RefineOp
    params: dict[str, Any] = Field(default_factory=dict)


class BatchRefineObservationSearchRequest(ContractModel):
    """Multi-strategy parallel refine request (api-routes §4 batch-refine).

    Each op is applied independently against the SAME base revision; the response
    is the list of resulting revisions. refine never widens authorized scope.
    """

    refinements: list[RefineObservationSearchRequest] = Field(default_factory=list)


class LocatorRequest(ContractModel):
    """Batch locator request (api-routes §5 POST /observation-search/locators)."""

    record_ids: list[str] = Field(default_factory=list)


class SearchContext(ContractModel):
    """Search context (schema-contracts §9.1). MVP: snapshot mode only."""

    context_id: str
    tenant_id: str = "tenant_default"
    principal_id: str
    session_id: str | None = None
    authorized_scope_hash: str
    dataset_revision: str
    mode: ContextMode = ContextMode.SNAPSHOT
    default_revision_id: str | None = None
    created_at: datetime | None = None
    last_accessed_at: datetime | None = None
    expires_at: datetime | None = None
    status: str = "active"


class SearchRevision(ContractModel):
    """Immutable search revision (schema-contracts §9.2)."""

    revision_id: str
    context_id: str
    parent_revision_id: str | None = None
    op: str
    op_params: dict[str, Any] = Field(default_factory=dict)
    candidate_count: int = Field(ge=0)
    facets: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class SearchCandidate(ContractModel):
    """Search candidate within a revision (schema-contracts §9.3)."""

    revision_id: str
    record_id: str
    rank: int = Field(ge=1)
    score: float
    score_detail: dict[str, Any] = Field(default_factory=dict)


class LocatorProjection(ContractModel):
    """Locator projection (schema-contracts §5.9).

    Derived from ObservationRecord + VideoSource. Never includes the internal
    ``source_uri`` (ARCHITECTURE_CONSTITUTION §5). ``playback_url`` /
    ``thumbnail_url`` are short-TTL placeholders in the MVP.
    """

    video_id: str
    camera_id: str
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    absolute_start_time: datetime
    absolute_end_time: datetime
    playback_url: str | None = None
    thumbnail_url: str | None = None
    expires_at: datetime | None = None


class ObservationDetailsRequest(ContractModel):
    """Observation details request (schema-contracts §5.7)."""

    record_ids: list[str] = Field(default_factory=list)
    include_locator: bool = False


class ObservationDetailsItem(ContractModel):
    """Observation details item (schema-contracts §5.8)."""

    record_id: str
    static_description_text: str
    dynamic_description_text: str
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    analysis_scale: AnalysisScale
    observed_start_time: datetime
    observed_end_time: datetime
    locator: LocatorProjection | None = None


class OverlappingRecordsRequest(ContractModel):
    """Overlapping-records request (schema-contracts §4.5, search-contract §7.1)."""

    record_id: str
    analysis_scale_filter: list[AnalysisScale] = Field(default_factory=list)
    time_padding_ms: int = Field(default=0, ge=0)
    top_k: int = Field(default=20, ge=1, le=100)
