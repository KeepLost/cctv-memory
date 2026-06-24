"""M2 backup/restore + migration tests (backup-export-contract §8, testing §8)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from cctv_memory.application.backup import BackupService
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.backup import BackupChecksum
from cctv_memory.domain.enums import Capability, SecurityLevel
from cctv_memory.domain.exceptions import (
    CapabilityDeniedError,
    RestoreError,
)
from cctv_memory.infrastructure.runtime import Runtime, build_runtime


@pytest.fixture
def runtime(tmp_path: object) -> Iterator[Runtime]:
    rt = build_runtime(data_dir=str(tmp_path))
    rt.init_storage()
    rt.create_schema()
    yield rt
    rt.dispose()


def _admin_scope() -> AuthorizedScope:
    return AuthorizedScope(
        tenant_id="tenant_default",
        principal_id="admin_1",
        allowed_camera_ids=["cam_lobby_01"],
        allowed_location_ids=["loc_lobby_01"],
        allowed_access_policy_ids=["policy_public_area"],
        max_security_level=SecurityLevel.RESTRICTED,
        capabilities=[Capability.RUNTIME_MANAGE],
        scope_hash="h_admin",
    )


def _viewer_scope() -> AuthorizedScope:
    return AuthorizedScope(
        tenant_id="tenant_default",
        principal_id="viewer_1",
        allowed_camera_ids=[],
        allowed_location_ids=[],
        allowed_access_policy_ids=[],
        max_security_level=SecurityLevel.PUBLIC,
        capabilities=[Capability.OBSERVATION_SEARCH],
        scope_hash="h_viewer",
    )


def test_fresh_db_has_schema_version(runtime: Runtime) -> None:
    from sqlalchemy import text

    with runtime.session() as session:
        value = session.execute(
            text("SELECT value FROM schema_metadata WHERE key='schema_version'")
        ).scalar()
    assert value == "v1"


def test_admin_backup_manifest_valid(runtime: Runtime, tmp_path: object) -> None:
    out = f"{tmp_path}/backup.sqlite3"  # type: ignore[str-bytes-safe]
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        manifest = service.admin_backup(out, _admin_scope())
    assert manifest.schema_version == "v1"
    assert manifest.backup_type == "admin_full_backup"
    assert manifest.checksum.algorithm == "sha256"
    assert len(manifest.checksum.value) == 64
    assert manifest.table_counts  # non-empty
    assert Path(out).exists()
    assert Path(out + ".manifest.json").exists()


def test_backup_requires_runtime_capability(runtime: Runtime, tmp_path: object) -> None:
    out = f"{tmp_path}/b2.sqlite3"  # type: ignore[str-bytes-safe]
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        with pytest.raises(CapabilityDeniedError):
            service.admin_backup(out, _viewer_scope())


def test_sqlite_online_backup_consistent_and_restorable(
    runtime: Runtime, tmp_path: object
) -> None:
    from cctv_memory.application.seed import seed_local_defaults

    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())

    out = f"{tmp_path}/b3.sqlite3"  # type: ignore[str-bytes-safe]
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        manifest = service.admin_backup(out, _admin_scope())
        # Restore should validate checksum and succeed.
        service.restore(out, manifest, _admin_scope())

    # The restored DB still has the seeded principal.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        assert repos.principal().get_principal("user_admin") is not None


def test_restore_rejects_bad_checksum(runtime: Runtime, tmp_path: object) -> None:
    out = f"{tmp_path}/b4.sqlite3"  # type: ignore[str-bytes-safe]
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        manifest = service.admin_backup(out, _admin_scope())
        bad = manifest.model_copy(
            update={"checksum": BackupChecksum(algorithm="sha256", value="0" * 64)}
        )
        with pytest.raises(RestoreError):
            service.restore(out, bad, _admin_scope())


def test_restore_rejects_unsupported_schema(runtime: Runtime, tmp_path: object) -> None:
    out = f"{tmp_path}/b5.sqlite3"  # type: ignore[str-bytes-safe]
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        manifest = service.admin_backup(out, _admin_scope())
        bad = manifest.model_copy(update={"schema_version": "v999"})
        with pytest.raises(RestoreError):
            service.restore(out, bad, _admin_scope())


def test_user_export_excludes_forbidden_and_no_sqlite_file(runtime: Runtime) -> None:
    """user_export returns only authorized records, no DB file, no source_uri."""
    from datetime import UTC, datetime

    from cctv_memory.contracts.observation import ObservationRecord
    from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
    from cctv_memory.contracts.video import (
        CameraDevice,
        CameraLocation,
        SubmitVideoSourceRequest,
    )
    from cctv_memory.domain.enums import AnalysisScale, SourceType

    base = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)
    with runtime.session() as session:
        repos = runtime.repositories(session)
        repos.camera().upsert_location(
            CameraLocation(location_id="loc_lobby_01", area="lobby",
                           access_policy_id="policy_public_area",
                           security_level=SecurityLevel.INTERNAL)
        )
        repos.camera().upsert_camera(
            CameraDevice(camera_id="cam_lobby_01", camera_name="c",
                         location_id="loc_lobby_01",
                         access_policy_id="policy_public_area", status="active")
        )
        repos.video_source().create_or_get_by_idempotency(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/secret/internal/path/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=base,
                idempotency_key="vk",
            ),
            video_id="video_001",
        )
        repos.publication().publish_records_atomically(
            PublishObservationRecordsCommand(
                command_id="c", analysis_job_id="job",
                records=[
                    ObservationRecord(
                        record_id="obs_1", video_id="video_001", analysis_job_id="job",
                        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                        segment_start_ms=0, segment_end_ms=12_000,
                        observed_start_time=base, observed_end_time=base,
                        camera_id="cam_lobby_01", location_id="loc_lobby_01",
                        static_description_text="person", dynamic_description_text="walks",
                        tags=["person"], access_policy_id="policy_public_area",
                        security_level=SecurityLevel.INTERNAL,
                    )
                ],
            )
        )

    scope = AuthorizedScope(
        tenant_id="tenant_default", principal_id="viewer_1",
        allowed_camera_ids=["cam_lobby_01"], allowed_location_ids=["loc_lobby_01"],
        allowed_access_policy_ids=["policy_public_area"],
        max_security_level=SecurityLevel.INTERNAL,
        capabilities=[Capability.OBSERVATION_SEARCH], scope_hash="h",
    )
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = BackupService(
            runtime.backup_adapter(), repos.audit(),
            observations=repos.observation_read(),
            video_sources=repos.video_source(),
        )
        bundle = service.user_export(scope)
    assert bundle.manifest.backup_type == "user_authorized_export"
    assert len(bundle.records) == 1
    assert bundle.records[0].record_id == "obs_1"
    # No DB file path, no source_uri anywhere in the bundle.
    dumped = bundle.model_dump_json()
    assert "source_uri" not in dumped
    assert "/secret/internal/path" not in dumped
    assert ".sqlite3" not in dumped
    assert bundle.manifest.included_paths == []
    # video_sources are only included when individually authorized (fail-closed);
    # the key guarantee is no source_uri / DB file ever leaks (asserted above).
    assert all(v.video_id == "video_001" for v in bundle.video_sources)


def test_user_export_empty_scope_returns_nothing(runtime: Runtime) -> None:
    scope = AuthorizedScope(
        tenant_id="tenant_default", principal_id="viewer_1",
        allowed_camera_ids=[], allowed_location_ids=[], allowed_access_policy_ids=[],
        max_security_level=SecurityLevel.PUBLIC,
        capabilities=[Capability.OBSERVATION_SEARCH], scope_hash="h",
    )
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = BackupService(
            runtime.backup_adapter(), repos.audit(),
            observations=repos.observation_read(),
            video_sources=repos.video_source(),
        )
        bundle = service.user_export(scope)
    assert bundle.records == []


def test_user_export_requires_search_capability(runtime: Runtime) -> None:
    scope = AuthorizedScope(
        tenant_id="tenant_default", principal_id="x",
        allowed_camera_ids=[], allowed_location_ids=[], allowed_access_policy_ids=[],
        max_security_level=SecurityLevel.PUBLIC,
        capabilities=[], scope_hash="h",
    )
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = BackupService(
            runtime.backup_adapter(), repos.audit(),
            observations=repos.observation_read(),
            video_sources=repos.video_source(),
        )
        with pytest.raises(CapabilityDeniedError):
            service.user_export(scope)


def test_migration_export_requires_runtime_manage(runtime: Runtime) -> None:
    scope = AuthorizedScope(
        tenant_id="tenant_default", principal_id="viewer_1",
        allowed_camera_ids=["cam_lobby_01"], allowed_location_ids=["loc_lobby_01"],
        allowed_access_policy_ids=["policy_public_area"],
        max_security_level=SecurityLevel.INTERNAL,
        capabilities=[Capability.OBSERVATION_SEARCH], scope_hash="h",
    )
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = BackupService(
            runtime.backup_adapter(), repos.audit(),
            observations=repos.observation_read(),
            video_sources=repos.video_source(),
        )
        with pytest.raises(CapabilityDeniedError):
            service.migration_export(scope)
