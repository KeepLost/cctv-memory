"""C4 tests: vector reindex/backfill + SearchContext sweep (MaintenanceService)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cctv_memory.application.maintenance import MaintenanceService
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.search import SearchContext
from cctv_memory.domain.enums import AnalysisScale, Capability, SecurityLevel
from cctv_memory.domain.exceptions import CapabilityDeniedError
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.infrastructure.indexing.mock_embedder import MockEmbedder

from tests.conftest import make_scope, seed_camera


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="cmd", analysis_job_id="job_test", records=list(records)
        )
    )


def _record(record_id: str, *, start_ms: int = 0) -> ObservationRecord:
    base = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_test",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=start_ms,
        segment_end_ms=start_ms + 12_000,
        observed_start_time=base,
        observed_end_time=base,
        camera_id="cam_lobby_01",
        location_id="loc_lobby_01",
        static_description_text=f"static text {record_id}",
        dynamic_description_text=f"dynamic text {record_id}",
        tags=["person"],
        access_policy_id="policy_public_area",
        security_level=SecurityLevel.INTERNAL,
    )


def _admin_scope() -> AuthorizedScope:
    base = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    return base.model_copy(
        update={"capabilities": [*base.capabilities, Capability.RUNTIME_MANAGE]}
    )


def _service(factory: SqliteRepositoryFactory, embedder: MockEmbedder) -> MaintenanceService:
    return MaintenanceService(
        factory.observation_read(),
        factory.index(),
        factory.search_context(),
        factory.audit(),
        embedder,
    )


def test_reindex_backfills_vectors(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1", start_ms=0), _record("obs_2", start_ms=12_000))
    embedder = MockEmbedder(dimension=32)
    svc = _service(factory, embedder)
    result = svc.reindex(_admin_scope())
    assert result.scanned == 2
    assert result.reindexed == 2
    # 2 records x 2 channels (static + dynamic) = 4 vectors.
    assert result.vectors_written == 4
    assert result.model_id == embedder.model_id
    stored = factory.index().get_vectors_for_records(["obs_1", "obs_2"])
    assert len(stored) == 4
    assert {v.vector_type for v in stored} == {"static", "dynamic"}


def test_reindex_is_idempotent(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    embedder = MockEmbedder(dimension=32)
    svc = _service(factory, embedder)
    first = svc.reindex(_admin_scope())
    assert first.vectors_written == 2
    second = svc.reindex(_admin_scope())
    # Re-run: nothing re-embedded (same model_id), all channels skipped.
    assert second.vectors_written == 0
    assert second.skipped == 2
    assert second.reindexed == 0


def test_reindex_force_rebuilds(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    embedder = MockEmbedder(dimension=32)
    svc = _service(factory, embedder)
    svc.reindex(_admin_scope())
    forced = svc.reindex(_admin_scope(), force=True)
    assert forced.vectors_written == 2
    assert forced.skipped == 0


def test_reindex_model_change_triggers_rebuild(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    svc_old = _service(factory, MockEmbedder(dimension=32, model_id="model-A"))
    svc_old.reindex(_admin_scope())
    # New model id -> existing vectors are considered stale -> re-embedded.
    svc_new = _service(factory, MockEmbedder(dimension=32, model_id="model-B"))
    result = svc_new.reindex(_admin_scope())
    assert result.vectors_written == 2
    assert result.skipped == 0
    stored = factory.index().get_vectors_for_records(["obs_1"], vector_type="static")
    assert stored[0].model_id == "model-B"


def test_reindex_requires_runtime_manage(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    svc = _service(factory, MockEmbedder(dimension=32))
    # Scope without runtime.manage.
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    with pytest.raises(CapabilityDeniedError):
        svc.reindex(scope)


def test_reindex_only_authorized_records(factory: SqliteRepositoryFactory) -> None:
    """Reindex reads within the maintenance scope; forbidden records are not embedded."""
    from cctv_memory.contracts.video import CameraDevice, CameraLocation

    for loc_id, pol in (("loc_lobby_01", "policy_public_area"), ("loc_sec", "policy_secret")):
        factory.camera().upsert_location(
            CameraLocation(location_id=loc_id, area="a", access_policy_id=pol,
                           security_level=SecurityLevel.INTERNAL)
        )
    for cam_id, loc_id, pol in (
        ("cam_lobby_01", "loc_lobby_01", "policy_public_area"),
        ("cam_sec", "loc_sec", "policy_secret"),
    ):
        factory.camera().upsert_camera(
            CameraDevice(camera_id=cam_id, camera_name=cam_id, location_id=loc_id,
                         access_policy_id=pol, status="active")
        )
    pub = _record("obs_pub")
    sec = _record("obs_sec", start_ms=12_000).model_copy(
        update={"camera_id": "cam_sec", "location_id": "loc_sec",
                "access_policy_id": "policy_secret",
                "security_level": SecurityLevel.CONFIDENTIAL}
    )
    _publish(factory, pub, sec)
    svc = _service(factory, MockEmbedder(dimension=32))
    # Admin scope limited to the public location only.
    result = svc.reindex(_admin_scope())
    assert result.scanned == 1  # only obs_pub is in the authorized pool
    assert factory.index().get_vectors_for_records(["obs_sec"]) == []
    assert len(factory.index().get_vectors_for_records(["obs_pub"])) == 2


def test_sweep_expires_stale_contexts(factory: SqliteRepositoryFactory) -> None:
    now = datetime.now(UTC)
    # One expired, one active.
    factory.search_context().create_context(
        SearchContext(
            context_id="ctx_old", principal_id="user_admin",
            authorized_scope_hash="h", dataset_revision="d",
            created_at=now - timedelta(hours=1),
            last_accessed_at=now - timedelta(hours=1),
            expires_at=now - timedelta(minutes=30), status="active",
        )
    )
    factory.search_context().create_context(
        SearchContext(
            context_id="ctx_new", principal_id="user_admin",
            authorized_scope_hash="h", dataset_revision="d",
            created_at=now, last_accessed_at=now,
            expires_at=now + timedelta(minutes=30), status="active",
        )
    )
    svc = _service(factory, MockEmbedder(dimension=32))
    result = svc.sweep_contexts()
    assert result.expired == 1
    assert factory.search_context().get_context("ctx_old").status == "expired"
    assert factory.search_context().get_context("ctx_new").status == "active"
