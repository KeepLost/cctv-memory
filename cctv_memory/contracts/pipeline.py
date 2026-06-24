"""Pipeline / publication command contracts (schema-contracts §6.5).

These DTOs cross the worker→publication boundary and the repository
publication port. Publication is the only path that writes active
ObservationRecord (ARCHITECTURE_CONSTITUTION §6).
"""

from __future__ import annotations

from pydantic import Field

from cctv_memory.contracts.common import SCHEMA_VERSION, ContractModel
from cctv_memory.contracts.observation import ObservationRecord


class PublishObservationRecordsCommand(ContractModel):
    """Atomic publication command (schema-contracts §6.5)."""

    schema_version: str = SCHEMA_VERSION
    command_id: str
    analysis_job_id: str
    records: list[ObservationRecord] = Field(default_factory=list)
    archive_reason: str = "rerun"


class PublicationResult(ContractModel):
    """Result of an atomic publication."""

    analysis_job_id: str
    created_record_ids: list[str] = Field(default_factory=list)
    updated_record_ids: list[str] = Field(default_factory=list)
    archived_record_ids: list[str] = Field(default_factory=list)
