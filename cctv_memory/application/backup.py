"""Backup / export / restore use case (application/backup.py).

- ``admin_backup``: consistent SQLite backup (via BackupPort) + manifest with
  schema_version / app_version / checksum / table_counts (backup-export-contract).
- ``restore``: validates the manifest schema_version + checksum before replacing.
- ``user_export``: scoped export is NOT built in this MVP — returns not_implemented
  (NotImplementedFeatureError) rather than pretending. Honest scope.

Admin backup requires the runtime/admin capability; restore likewise. Audit
events are appended for backup/restore.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.backup import (
    BackupChecksum,
    BackupManifest,
    ExportedObservationRecord,
    ExportedVideoSource,
    MigrationExportBundle,
    UserExportBundle,
)
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.domain.enums import Capability
from cctv_memory.domain.exceptions import (
    CapabilityDeniedError,
    RestoreError,
)
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.backup import BackupPort
from cctv_memory.repositories.observation import ObservationRecordReadRepository
from cctv_memory.repositories.video_source import VideoSourceRepository

APP_VERSION = "0.1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"v1"})


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class BackupService:
    """Admin full backup + validated restore + scope-bounded user/migration export."""

    def __init__(
        self,
        backup: BackupPort,
        audit: AuditRepository,
        *,
        observations: ObservationRecordReadRepository | None = None,
        video_sources: VideoSourceRepository | None = None,
    ) -> None:
        self._backup = backup
        self._audit = audit
        self._observations = observations
        self._video_sources = video_sources

    def admin_backup(
        self, out_path: str, scope: AuthorizedScope, *, request_id: str | None = None
    ) -> BackupManifest:
        if Capability.RUNTIME_MANAGE not in scope.capabilities:
            raise CapabilityDeniedError("runtime.manage required for admin backup")

        self._audit_event("backup_started", scope, request_id, {"out_path": out_path})
        checksum = self._backup.backup_to(out_path)
        manifest = BackupManifest(
            app_version=APP_VERSION,
            backup_type="admin_full_backup",
            created_at=_now(),
            created_by_principal_id=scope.principal_id,
            database_engine="sqlite",
            data_scope="admin_full",
            included_paths=[Path(out_path).name],
            table_counts=self._backup.table_counts(),
            checksum=BackupChecksum(algorithm="sha256", value=checksum),
            export_scope="admin_full_backup",
        )
        # Write the manifest next to the backup for restore validation.
        manifest_path = Path(out_path).with_suffix(Path(out_path).suffix + ".manifest.json")
        manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        self._audit_event(
            "backup_succeeded", scope, request_id, {"checksum": checksum}
        )
        return manifest

    def restore(
        self,
        backup_path: str,
        manifest: BackupManifest,
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
    ) -> None:
        if Capability.RUNTIME_MANAGE not in scope.capabilities:
            raise CapabilityDeniedError("runtime.manage required for restore")
        if manifest.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise RestoreError(f"unsupported schema_version {manifest.schema_version}")

        self._audit_event("restore_started", scope, request_id, {"backup_path": backup_path})
        try:
            self._backup.restore_from(backup_path, manifest.checksum.value)
        except ValueError as exc:
            self._audit_event("restore_failed", scope, request_id, {"error": str(exc)})
            raise RestoreError(str(exc)) from exc
        self._audit_event("restore_succeeded", scope, request_id, {})

    @staticmethod
    def load_manifest(path: str) -> BackupManifest:
        """Load a manifest JSON written by ``admin_backup``."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return BackupManifest.model_validate(data)

    def user_export(
        self, scope: AuthorizedScope, *, request_id: str | None = None, limit: int = 100_000
    ) -> UserExportBundle:
        """Export ONLY the caller's authorized records + video metadata (§1.2, §4).

        Requires ``observation.search``. Reads are AuthorizedScope-filtered (fail
        closed) so forbidden records never appear in the bundle, counts, or
        metadata. Never includes the full SQLite DB file or internal ``source_uri``
        (backup-export-contract §4 forbidden list). Audited (export_started/
        succeeded).
        """
        if Capability.OBSERVATION_SEARCH not in scope.capabilities:
            raise CapabilityDeniedError("observation.search required for user export")
        if self._observations is None:
            raise CapabilityDeniedError("user export not available in this context")

        self._audit_event("export_started", scope, request_id, {"export_type": "user"})
        records = self._observations.authorized_candidate_pool(scope, limit=limit)
        exported_records = [self._to_exported_record(r) for r in records]
        exported_videos = self._exported_video_sources(records, scope)
        manifest = BackupManifest(
            app_version=APP_VERSION,
            backup_type="user_authorized_export",
            created_at=_now(),
            created_by_principal_id=scope.principal_id,
            database_engine="sqlite",
            data_scope="user_authorized",
            included_paths=[],  # never a DB file / filesystem path
            table_counts={
                "observation_records": len(exported_records),
                "video_sources": len(exported_videos),
            },
            checksum=BackupChecksum(algorithm="sha256", value=""),
            export_scope="user_authorized_export",
            metadata={"scope_hash": scope.scope_hash},
        )
        self._audit_event(
            "export_succeeded",
            scope,
            request_id,
            {"export_type": "user", "record_count": len(exported_records)},
        )
        return UserExportBundle(
            manifest=manifest,
            records=exported_records,
            video_sources=exported_videos,
        )

    def migration_export(
        self, scope: AuthorizedScope, *, request_id: str | None = None, limit: int = 1_000_000
    ) -> MigrationExportBundle:
        """Export authorized contract rows for migration (§1.3).

        Emits contract DTO rows (not ORM private objects) so a SQLite -> PostgreSQL
        or version migration can re-import them. Requires ``runtime.manage`` (a
        migration is an admin operation). Still scope-bounded and source_uri-free.
        """
        if Capability.RUNTIME_MANAGE not in scope.capabilities:
            raise CapabilityDeniedError("runtime.manage required for migration export")
        if self._observations is None:
            raise CapabilityDeniedError("migration export not available in this context")

        self._audit_event(
            "export_started", scope, request_id, {"export_type": "migration"}
        )
        records = self._observations.authorized_candidate_pool(scope, limit=limit)
        exported_records = [self._to_exported_record(r) for r in records]
        exported_videos = self._exported_video_sources(records, scope)
        manifest = BackupManifest(
            app_version=APP_VERSION,
            backup_type="migration_export",
            created_at=_now(),
            created_by_principal_id=scope.principal_id,
            database_engine="sqlite",
            data_scope="migration",
            included_paths=[],
            table_counts={
                "observation_records": len(exported_records),
                "video_sources": len(exported_videos),
            },
            checksum=BackupChecksum(algorithm="sha256", value=""),
            export_scope="migration_export",
            metadata={"scope_hash": scope.scope_hash},
        )
        self._audit_event(
            "export_succeeded",
            scope,
            request_id,
            {"export_type": "migration", "record_count": len(exported_records)},
        )
        return MigrationExportBundle(
            manifest=manifest,
            records=exported_records,
            video_sources=exported_videos,
        )

    @staticmethod
    def _to_exported_record(rec: ObservationRecord) -> ExportedObservationRecord:
        """Project an ObservationRecord to its sanitized export shape (no source_uri)."""
        return ExportedObservationRecord(
            record_id=rec.record_id,
            video_id=rec.video_id,
            camera_id=rec.camera_id,
            location_id=rec.location_id,
            analysis_scale=rec.analysis_scale.value,
            segment_start_ms=rec.segment_start_ms,
            segment_end_ms=rec.segment_end_ms,
            observed_start_time=rec.observed_start_time,
            observed_end_time=rec.observed_end_time,
            static_description_text=rec.static_description_text,
            dynamic_description_text=rec.dynamic_description_text,
            tags=rec.tags,
            attributes=rec.attributes,
            access_policy_id=rec.access_policy_id,
            security_level=rec.security_level.value,
            model_version=rec.model_version,
            prompt_version=rec.prompt_version,
            pipeline_version=rec.pipeline_version,
        )

    def _exported_video_sources(
        self, records: list[ObservationRecord], scope: AuthorizedScope
    ) -> list[ExportedVideoSource]:
        """Sanitized VideoSource metadata for the authorized records (no source_uri)."""
        if self._video_sources is None:
            return []
        video_ids = sorted({r.video_id for r in records})
        out: list[ExportedVideoSource] = []
        for vid in video_ids:
            # Re-check authorization for each video source (defense in depth).
            source = self._video_sources.get_authorized_by_id(vid, scope)
            if source is None:
                continue
            out.append(
                ExportedVideoSource(
                    video_id=source.video_id,
                    camera_id=source.camera_id,
                    source_type=source.source_type.value,
                    video_start_time=source.video_start_time,
                    video_end_time=source.video_end_time,
                    duration_ms=source.duration_ms,
                    external_source_id=source.external_source_id,
                    access_policy_id=source.access_policy_id,
                )
            )
        return out

    def _audit_event(
        self,
        event_type: str,
        scope: AuthorizedScope,
        request_id: str | None,
        metadata: dict[str, object],
    ) -> None:
        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type=event_type,
                request_id=request_id,
                principal_id=scope.principal_id,
                resource_scope_hash=scope.scope_hash,
                metadata=metadata,
            )
        )
