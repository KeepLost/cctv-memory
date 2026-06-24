"""M4 tests: search, details, locator (no source_uri), overlap, empty scope."""

from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.application.locator import LocatorService
from cctv_memory.application.search import SearchService
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.search import (
    ObservationDetailsRequest,
    OverlappingRecordsRequest,
    StartObservationSearchRequest,
)
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory

from tests.conftest import make_scope, seed_camera


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="cmd_test",
            analysis_job_id="job_test",
            records=list(records),
        )
    )


def _record(
    record_id: str,
    *,
    static_text: str = "person dark_clothing backpack near entrance",
    dynamic_text: str = "subject loitering then moving",
    tags: list[str] | None = None,
    camera_id: str = "cam_lobby_01",
    location_id: str = "loc_lobby_01",
    policy_id: str = "policy_public_area",
    security_level: SecurityLevel = SecurityLevel.INTERNAL,
    segment_start_ms: int = 0,
    segment_end_ms: int = 12_000,
) -> ObservationRecord:
    base = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_test",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=segment_start_ms,
        segment_end_ms=segment_end_ms,
        observed_start_time=base,
        observed_end_time=base,
        camera_id=camera_id,
        location_id=location_id,
        static_description_text=static_text,
        dynamic_description_text=dynamic_text,
        tags=tags if tags is not None else ["person", "dark_clothing", "backpack"],
        access_policy_id=policy_id,
        security_level=security_level,
    )


def _search_service(factory: SqliteRepositoryFactory) -> SearchService:
    return SearchService(
        factory.observation_read(), factory.search_context(), factory.audit()
    )


def test_search_returns_authorized_match(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = _search_service(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(query_text="backpack", top_k=10), scope
    )
    assert resp.candidate_count == 1
    assert resp.results[0].record_id == "obs_1"
    # Context + revision + candidate persisted.
    ctx = factory.search_context().get_context(resp.context_id)
    assert ctx is not None
    assert ctx.default_revision_id == resp.revision_id
    cands = factory.search_context().list_candidates(resp.revision_id).items
    assert len(cands) == 1


def test_search_empty_scope_returns_nothing(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    # Empty allowed lists => deny everything (fail closed).
    scope = make_scope(camera_ids=[], location_ids=[], policy_ids=[])
    svc = _search_service(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(query_text="backpack", top_k=10), scope
    )
    assert resp.candidate_count == 0
    assert resp.results == []


def test_search_excludes_forbidden_security_level(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _record("obs_pub", security_level=SecurityLevel.INTERNAL),
        _record(
            "obs_conf",
            security_level=SecurityLevel.CONFIDENTIAL,
            segment_start_ms=12_000,
            segment_end_ms=24_000,
        ),
    )
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
        max_level=SecurityLevel.INTERNAL,
    )
    svc = _search_service(factory)
    resp = svc.start_search(StartObservationSearchRequest(top_k=10), scope)
    ids = {r.record_id for r in resp.results}
    assert ids == {"obs_pub"}


def test_details_and_locator_hide_source_uri(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = LocatorService(factory.observation_read(), factory.audit())
    items = svc.get_details(
        ObservationDetailsRequest(record_ids=["obs_1"], include_locator=True), scope
    )
    assert len(items) == 1
    item = items[0]
    assert item.locator is not None
    dumped = item.model_dump()
    # source_uri must never appear anywhere in the projection.
    assert "source_uri" not in str(dumped)
    assert item.locator.playback_url is not None
    assert item.locator.playback_url.startswith("/api/v1/playback/")


def test_details_forbidden_record_returns_empty(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_1"))
    scope = make_scope(camera_ids=[], location_ids=[], policy_ids=[])
    svc = LocatorService(factory.observation_read(), factory.audit())
    items = svc.get_details(
        ObservationDetailsRequest(record_ids=["obs_1"], include_locator=True), scope
    )
    assert items == []


def test_overlapping_records_respects_scope(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _record("obs_a", segment_start_ms=0, segment_end_ms=12_000),
        _record("obs_b", segment_start_ms=6_000, segment_end_ms=18_000),
        _record(
            "obs_far", segment_start_ms=60_000, segment_end_ms=72_000
        ),
    )
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = LocatorService(factory.observation_read(), factory.audit())
    overlapping = svc.get_overlapping(
        OverlappingRecordsRequest(record_id="obs_a", top_k=10), scope
    )
    ids = {r.record_id for r in overlapping}
    assert "obs_b" in ids
    assert "obs_a" not in ids  # target excluded
    assert "obs_far" not in ids


def test_overlapping_forbidden_target_returns_empty(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_a"))
    scope = make_scope(camera_ids=[], location_ids=[], policy_ids=[])
    svc = LocatorService(factory.observation_read(), factory.audit())
    assert svc.get_overlapping(
        OverlappingRecordsRequest(record_id="obs_a", top_k=10), scope
    ) == []
