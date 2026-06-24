"""C2 integration tests: authorized-scope semantic vector rerank in SearchService.

Verifies that, when vector search is enabled with a (mock) embedder + IndexPort:
- the vector channel is fused into ranking and surfaced in score_detail;
- vector rerank operates ONLY within the authorized candidate id set (forbidden
  records never get embedded/ranked — the candidate set is already scope-filtered);
- behavior gracefully falls back to FTS when vectors are absent;
- the deterministic FTS-only path is unchanged when vector search is disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.application.search import SearchService
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.search import (
    RefineObservationSearchRequest,
    StartObservationSearchRequest,
)
from cctv_memory.domain.enums import AnalysisScale, RefineOp, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.infrastructure.indexing.mock_embedder import MockEmbedder
from cctv_memory.repositories.index import StoredVector

from tests.conftest import make_scope, seed_camera


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="cmd_test", analysis_job_id="job_test", records=list(records)
        )
    )


def _record(
    record_id: str,
    *,
    static_text: str,
    dynamic_text: str = "subject moving",
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
        tags=["person"],
        access_policy_id=policy_id,
        security_level=security_level,
    )


def _index_vectors(
    factory: SqliteRepositoryFactory, embedder: MockEmbedder, records: list[tuple[str, str]]
) -> None:
    """Embed (record_id, text) pairs with the mock embedder and upsert as static vectors."""
    from cctv_memory.application.async_support import run_blocking

    vecs = []
    for rid, text in records:
        emb = run_blocking(embedder.embed_query(text))
        vecs.append(
            StoredVector(
                record_id=rid,
                vector_type="static",
                embedding=emb,
                model_id=embedder.model_id,
                dimension=embedder.dimension,
            )
        )
    factory.index().upsert_vectors(vecs)


def _service(
    factory: SqliteRepositoryFactory, *, embedder: MockEmbedder | None, enabled: bool
) -> SearchService:
    return SearchService(
        factory.observation_read(),
        factory.search_context(),
        factory.audit(),
        embedder=embedder,
        index=factory.index() if embedder else None,
        vector_search_enabled=enabled,
    )


def test_vector_rerank_surfaces_vector_score_detail(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _record("obs_match", static_text="a red car parked by the gate",
                segment_start_ms=0, segment_end_ms=12_000),
        _record("obs_other", static_text="a blue bicycle near the wall",
                segment_start_ms=12_000, segment_end_ms=24_000),
    )
    embedder = MockEmbedder(dimension=64)
    # Store the query text's own vector against obs_match so cosine == 1.0 there.
    query = "a red car parked by the gate"
    _index_vectors(factory, embedder, [("obs_match", query),
                                       ("obs_other", "a blue bicycle near the wall")])
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = _service(factory, embedder=embedder, enabled=True)
    resp = svc.start_search(
        StartObservationSearchRequest(query_text=query, top_k=10), scope
    )
    by_id = {r.record_id: r for r in resp.results}
    assert "obs_match" in by_id
    # Vector channel populated static_score and a vector_rank for the matched doc.
    detail = by_id["obs_match"].score_detail
    assert "static_score" in detail
    assert detail.get("vector_rank") == 1
    # Perfect cosine match ranks first.
    assert resp.results[0].record_id == "obs_match"


def test_vector_rerank_only_within_authorized_scope(factory: SqliteRepositoryFactory) -> None:
    """Forbidden records are never embedded/ranked: the candidate pool is scope-filtered."""
    # Two policies: viewer sees public, not secret.
    from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules
    from cctv_memory.contracts.video import CameraDevice, CameraLocation

    factory.access_policy().upsert_access_policy(
        AccessPolicy(access_policy_id="policy_public_area", name="pub",
                     security_level=SecurityLevel.INTERNAL,
                     rules=AccessPolicyRules(allowed_roles=["viewer"]))
    )
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
    _publish(
        factory,
        _record("obs_pub", static_text="confidential secret document on the desk"),
        _record("obs_sec", static_text="confidential secret document on the desk",
                camera_id="cam_sec", location_id="loc_sec", policy_id="policy_secret",
                security_level=SecurityLevel.CONFIDENTIAL,
                segment_start_ms=12_000, segment_end_ms=24_000),
    )
    embedder = MockEmbedder(dimension=64)
    query = "confidential secret document on the desk"
    # Index BOTH records' vectors (including the forbidden one) to prove the
    # forbidden record is excluded by the authorized candidate pool, not by luck.
    _index_vectors(factory, embedder, [("obs_pub", query), ("obs_sec", query)])
    # Viewer scope: only the public camera/location/policy.
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = _service(factory, embedder=embedder, enabled=True)
    resp = svc.start_search(
        StartObservationSearchRequest(query_text=query, top_k=10), scope
    )
    ids = {r.record_id for r in resp.results}
    assert "obs_sec" not in ids
    assert ids == {"obs_pub"}


def test_rerank_current_candidates_op_uses_vectors(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _record("obs_a", static_text="alpha gate camera", segment_start_ms=0,
                segment_end_ms=12_000),
        _record("obs_b", static_text="beta wall sensor", segment_start_ms=12_000,
                segment_end_ms=24_000),
    )
    embedder = MockEmbedder(dimension=64)
    _index_vectors(factory, embedder, [("obs_a", "alpha gate camera"),
                                       ("obs_b", "beta wall sensor")])
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = _service(factory, embedder=embedder, enabled=True)
    start = svc.start_search(StartObservationSearchRequest(query_text="alpha", top_k=10), scope)
    refined = svc.refine_search(
        start.context_id,
        RefineObservationSearchRequest(
            base_revision_id=start.revision_id,
            op=RefineOp.RERANK_CURRENT_CANDIDATES,
            params={"query_text": "alpha gate camera", "top_k": 10},
        ),
        scope,
    )
    # Vector rerank produced a vector channel in the detail for the matched doc.
    top = refined.results[0]
    assert top.record_id == "obs_a"
    assert "static_score" in top.score_detail


def test_disabled_vector_search_falls_back_to_fts(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_a", static_text="backpack near entrance"))
    embedder = MockEmbedder(dimension=64)
    _index_vectors(factory, embedder, [("obs_a", "backpack near entrance")])
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    # enabled=False -> no vector channel, deterministic FTS behavior.
    svc = _service(factory, embedder=embedder, enabled=False)
    resp = svc.start_search(
        StartObservationSearchRequest(query_text="backpack", top_k=10), scope
    )
    assert resp.results[0].record_id == "obs_a"
    assert "vector_rank" not in resp.results[0].score_detail
    assert "static_score" not in resp.results[0].score_detail


def test_vector_enabled_but_no_vectors_stored_still_returns_fts(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_a", static_text="backpack near entrance"))
    embedder = MockEmbedder(dimension=64)
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    svc = _service(factory, embedder=embedder, enabled=True)
    resp = svc.start_search(
        StartObservationSearchRequest(query_text="backpack", top_k=10), scope
    )
    # No vectors stored -> FTS still finds it; no vector detail keys.
    assert resp.results[0].record_id == "obs_a"
    assert "vector_rank" not in resp.results[0].score_detail
