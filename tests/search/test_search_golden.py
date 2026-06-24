"""Search golden tests (testing-contract §5) for the Phase 5 search system.

Deterministic fixtures: same camera, multiple time ranges, same appearance with
different dynamic events, same event at different security levels, same tag
across authorized + forbidden records, and overlapping segments. Mock-VLM text is
replaced here by explicit controlled text so relevance is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cctv_memory.application.search import SearchService
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.search import (
    RefineObservationSearchRequest,
    StartObservationSearchRequest,
)
from cctv_memory.domain.enums import AnalysisScale, RefineOp, SearchMode, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory

from tests.conftest import make_scope, seed_camera

_BASE = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)


def _rec(
    record_id: str,
    *,
    static_text: str,
    dynamic_text: str,
    tags: list[str],
    scale: AnalysisScale = AnalysisScale.DEFAULT_SEGMENT,
    security_level: SecurityLevel = SecurityLevel.INTERNAL,
    policy_id: str = "policy_public_area",
    camera_id: str = "cam_lobby_01",
    location_id: str = "loc_lobby_01",
    start_ms: int = 0,
    end_ms: int = 12_000,
    offset_min: int = 0,
) -> ObservationRecord:
    obs = _BASE + timedelta(minutes=offset_min)
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_001",
        analysis_scale=scale,
        segment_start_ms=start_ms,
        segment_end_ms=end_ms,
        observed_start_time=obs,
        observed_end_time=obs + timedelta(seconds=12),
        camera_id=camera_id,
        location_id=location_id,
        static_description_text=static_text,
        dynamic_description_text=dynamic_text,
        tags=tags,
        access_policy_id=policy_id,
        security_level=security_level,
    )


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="cmd", analysis_job_id="job_001", records=list(records)
        )
    )


def _svc(factory: SqliteRepositoryFactory) -> SearchService:
    return SearchService(
        factory.observation_read(), factory.search_context(), factory.audit()
    )


def _full_scope(max_level: SecurityLevel = SecurityLevel.INTERNAL):  # type: ignore[no-untyped-def]
    return make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
        max_level=max_level,
    )


def test_static_attribute_search_matches_static_text(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_backpack", static_text="person with backpack near door",
             dynamic_text="standing still", tags=["person", "backpack"], start_ms=0, end_ms=12000),
        _rec("obs_umbrella", static_text="person with umbrella in lobby",
             dynamic_text="walking fast", tags=["person", "umbrella"],
             start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(
            query_text="backpack", search_mode=SearchMode.STATIC_ATTRIBUTE, top_k=10
        ),
        _full_scope(),
    )
    assert resp.results[0].record_id == "obs_backpack"
    assert all(r.record_id != "obs_umbrella" or r.rank > 1 for r in resp.results)


def test_dynamic_event_search_matches_dynamic_text(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_loiter", static_text="person in lobby",
             dynamic_text="loitering and pacing near entrance", tags=["person"],
             start_ms=0, end_ms=12000),
        _rec("obs_run", static_text="person in lobby",
             dynamic_text="running quickly through hall", tags=["person"],
             start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(
            query_text="loitering", search_mode=SearchMode.DYNAMIC_EVENT, top_k=10
        ),
        _full_scope(),
    )
    assert resp.results[0].record_id == "obs_loiter"


def test_hybrid_rrf_deterministic(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_a", static_text="person with backpack",
             dynamic_text="loitering near door", tags=["person", "backpack"],
             start_ms=0, end_ms=12000),
        _rec("obs_b", static_text="person with bag",
             dynamic_text="walking", tags=["person"], start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    req = StartObservationSearchRequest(
        query_text="backpack loitering", search_mode=SearchMode.HYBRID, top_k=10
    )
    r1 = svc.start_search(req, _full_scope())
    r2 = svc.start_search(req, _full_scope())
    ids1 = [(x.record_id, x.rank) for x in r1.results]
    ids2 = [(x.record_id, x.rank) for x in r2.results]
    assert ids1 == ids2  # deterministic
    assert r1.results[0].record_id == "obs_a"
    # score_detail carries RRF channels
    assert "rrf_score" in r1.results[0].score_detail


def test_analysis_scale_preference_boosts_but_does_not_filter(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_default", static_text="person backpack",
             dynamic_text="loitering", tags=["person"], scale=AnalysisScale.DEFAULT_SEGMENT,
             start_ms=0, end_ms=12000),
        _rec("obs_highfreq", static_text="person backpack",
             dynamic_text="loitering", tags=["person"], scale=AnalysisScale.HIGH_FREQ_EVENT,
             start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(
            query_text="backpack loitering",
            search_mode=SearchMode.HYBRID,
            preferred_analysis_scales=[AnalysisScale.HIGH_FREQ_EVENT],
            top_k=10,
        ),
        _full_scope(),
    )
    ids = {r.record_id for r in resp.results}
    # preference boosts high_freq but does NOT filter out default_segment
    assert ids == {"obs_default", "obs_highfreq"}
    assert resp.results[0].record_id == "obs_highfreq"


def test_analysis_scale_filter_filters(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_default", static_text="person", dynamic_text="x", tags=["person"],
             scale=AnalysisScale.DEFAULT_SEGMENT, start_ms=0, end_ms=12000),
        _rec("obs_highfreq", static_text="person", dynamic_text="x", tags=["person"],
             scale=AnalysisScale.HIGH_FREQ_EVENT, start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(
            analysis_scale_filter=[AnalysisScale.HIGH_FREQ_EVENT], top_k=10
        ),
        _full_scope(),
    )
    ids = {r.record_id for r in resp.results}
    assert ids == {"obs_highfreq"}


def test_facet_counts_only_authorized_candidates(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_ok", static_text="person backpack", dynamic_text="x",
             tags=["person", "backpack"], start_ms=0, end_ms=12000),
        _rec("obs_secret", static_text="person backpack", dynamic_text="x",
             tags=["person", "backpack"], security_level=SecurityLevel.CONFIDENTIAL,
             start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    resp = svc.start_search(
        StartObservationSearchRequest(top_k=10),
        _full_scope(max_level=SecurityLevel.INTERNAL),
    )
    assert resp.facets["candidate_count"] == 1
    cam = resp.facets["camera_distribution"]
    assert sum(entry["count"] for entry in cam) == 1


def test_overlap_returns_authorized_overlapping_records(
    factory: SqliteRepositoryFactory,
) -> None:
    from cctv_memory.application.locator import LocatorService
    from cctv_memory.contracts.search import OverlappingRecordsRequest

    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_a", static_text="x", dynamic_text="y", tags=["person"],
             start_ms=0, end_ms=12000),
        _rec("obs_b", static_text="x", dynamic_text="y", tags=["person"],
             start_ms=6000, end_ms=18000),
        _rec("obs_far", static_text="x", dynamic_text="y", tags=["person"],
             start_ms=60000, end_ms=72000),
    )
    loc = LocatorService(factory.observation_read(), factory.audit())
    overlapping = loc.get_overlapping(
        OverlappingRecordsRequest(record_id="obs_a", top_k=10), _full_scope()
    )
    ids = {r.record_id for r in overlapping}
    assert "obs_b" in ids
    assert "obs_a" not in ids
    assert "obs_far" not in ids


def test_refine_revision_is_immutable(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_a", static_text="person backpack", dynamic_text="loitering",
             tags=["person", "backpack"], start_ms=0, end_ms=12000),
        _rec("obs_b", static_text="person bag", dynamic_text="walking",
             tags=["person", "bag"], start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    scope = _full_scope()
    start = svc.start_search(
        StartObservationSearchRequest(query_text="person", search_mode=SearchMode.HYBRID, top_k=10),
        scope,
    )
    base_rev_id = start.revision_id
    base_candidates_before = [
        c.record_id for c in factory.search_context().list_candidates(base_rev_id).items
    ]

    refined = svc.refine_search(
        start.context_id,
        RefineObservationSearchRequest(
            base_revision_id=base_rev_id,
            op=RefineOp.NARROW_BY_TAGS,
            params={"tags": ["backpack"]},
        ),
        scope,
    )
    # New revision created; base revision candidates unchanged (immutable).
    assert refined.revision_id != base_rev_id
    base_candidates_after = [
        c.record_id for c in factory.search_context().list_candidates(base_rev_id).items
    ]
    assert base_candidates_before == base_candidates_after
    # Refined result narrowed to the backpack record.
    assert {r.record_id for r in refined.results} == {"obs_a"}


def test_refine_does_not_expand_authorized_scope(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_ok", static_text="person backpack", dynamic_text="x",
             tags=["person", "backpack"], start_ms=0, end_ms=12000),
        _rec("obs_secret", static_text="person backpack", dynamic_text="x",
             tags=["person", "backpack"], security_level=SecurityLevel.CONFIDENTIAL,
             start_ms=12000, end_ms=24000),
    )
    svc = _svc(factory)
    scope = _full_scope(max_level=SecurityLevel.INTERNAL)
    start = svc.start_search(StartObservationSearchRequest(top_k=10), scope)
    refined = svc.refine_search(
        start.context_id,
        RefineObservationSearchRequest(
            base_revision_id=start.revision_id,
            op=RefineOp.HYBRID_SEARCH_TEXT,
            params={"query_text": "backpack"},
        ),
        scope,
    )
    assert all(r.record_id != "obs_secret" for r in refined.results)
