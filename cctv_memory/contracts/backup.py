"""Backup manifest contract (schema-contracts §11, backup-export-contract §2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from cctv_memory.contracts.common import SCHEMA_VERSION, ContractModel


class AdminBackupRequest(ContractModel):
    """Admin full-backup request body (api/v1/admin/backups).

    Declared so the HTTP route can type its request body and expose a real
    ``requestBody`` schema in OpenAPI (replacing an untyped ``dict`` read).
    """

    out_path: str = Field(min_length=1)


class BackupChecksum(ContractModel):
    """Backup checksum (schema-contracts §11)."""

    algorithm: str = "sha256"
    value: str


class BackupManifest(ContractModel):
    """Backup manifest (schema-contracts §11, backup-export-contract §2)."""

    schema_version: str = SCHEMA_VERSION
    app_version: str
    backup_type: str
    created_at: datetime | None = None
    created_by_principal_id: str | None = None
    database_engine: str = "sqlite"
    data_scope: str | None = None
    included_paths: list[str] = Field(default_factory=list)
    table_counts: dict[str, int] = Field(default_factory=dict)
    checksum: BackupChecksum
    export_scope: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExportedObservationRecord(ContractModel):
    """A sanitized ObservationRecord for export (backup-export-contract §1.2/§1.3).

    Deliberately omits the internal ``source_uri`` and any clip/thumbnail/internal
    storage paths (ARCHITECTURE_CONSTITUTION §5). Carries only the
    externally-safe, authorized observation fields.
    """

    record_id: str
    video_id: str
    camera_id: str
    location_id: str
    analysis_scale: str
    segment_start_ms: int
    segment_end_ms: int
    observed_start_time: datetime
    observed_end_time: datetime
    static_description_text: str
    dynamic_description_text: str
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    access_policy_id: str
    security_level: str
    model_version: str | None = None
    prompt_version: str | None = None
    pipeline_version: str | None = None


class ExportedVideoSource(ContractModel):
    """Sanitized VideoSource metadata for export (NO source_uri)."""

    video_id: str
    camera_id: str
    source_type: str
    video_start_time: datetime
    video_end_time: datetime | None = None
    duration_ms: int | None = None
    external_source_id: str | None = None
    access_policy_id: str | None = None


class UserExportBundle(ContractModel):
    """User authorized export bundle (backup-export-contract §1.2).

    Contains ONLY resources within the caller's AuthorizedScope. Never includes a
    full SQLite DB file, forbidden records, or internal ``source_uri``.
    """

    manifest: BackupManifest
    records: list[ExportedObservationRecord] = Field(default_factory=list)
    video_sources: list[ExportedVideoSource] = Field(default_factory=list)


class MigrationExportBundle(ContractModel):
    """Migration export (backup-export-contract §1.3): contract DTO rows only.

    Uses contract schema rows (not ORM private objects) so a SQLite -> PostgreSQL
    or version migration can re-import them. Still scope-bounded and source_uri-free.
    """

    manifest: BackupManifest
    records: list[ExportedObservationRecord] = Field(default_factory=list)
    video_sources: list[ExportedVideoSource] = Field(default_factory=list)
