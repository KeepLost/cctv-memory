"""Index document contracts (schema-contracts §8).

Index documents carry the metadata required for permission filtering
(architecture-contracts-and-tech-stack §2.5). Index documents are rebuildable;
the active ObservationRecord table is the fact source.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from cctv_memory.contracts.common import SCHEMA_VERSION, ContractModel
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel


class IndexDocumentMetadata(ContractModel):
    """Shared index metadata for permission-aware filtering (schema-contracts §8.1)."""

    video_id: str
    camera_id: str
    location_id: str
    analysis_scale: AnalysisScale
    observed_start_time: datetime
    observed_end_time: datetime
    access_policy_id: str
    security_level: SecurityLevel
    tags: list[str] = Field(default_factory=list)


class ObservationStaticIndexDocument(ContractModel):
    """Static-description index document (schema-contracts §8.1)."""

    schema_version: str = SCHEMA_VERSION
    record_id: str
    vector_type: str = "static"
    text: str
    embedding: list[float] | None = None
    metadata: IndexDocumentMetadata


class ObservationDynamicIndexDocument(ContractModel):
    """Dynamic-description index document (schema-contracts §8.2)."""

    schema_version: str = SCHEMA_VERSION
    record_id: str
    vector_type: str = "dynamic"
    text: str
    embedding: list[float] | None = None
    metadata: IndexDocumentMetadata
