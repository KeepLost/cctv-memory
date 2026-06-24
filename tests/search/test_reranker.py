"""C3 tests: RerankerPort (mock + fake-transport adapter) + search rerank stage.

Mirrors the embedding test approach (network-free fake httpx transport). Verifies
the reranker only ever scores the candidate documents it is given, that the
SiliconFlow adapter hides the payload and never leaks the key, and that the
SearchService cross-encoder stage reorders ONLY the authorized candidate set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from cctv_memory.application.async_support import run_blocking
from cctv_memory.application.search import SearchService
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.search import (
    RefineObservationSearchRequest,
    StartObservationSearchRequest,
)
from cctv_memory.domain.enums import AnalysisScale, RefineOp, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.infrastructure.indexing.mock_reranker import MockReranker
from cctv_memory.infrastructure.indexing.siliconflow_reranker import SiliconFlowReranker
from cctv_memory.services.reranker import RerankDocument, RerankerError, RerankerPort

from tests.conftest import make_scope, seed_camera

# ---- mock reranker ---------------------------------------------------------


def test_mock_reranker_is_a_reranker_port() -> None:
    assert isinstance(MockReranker(), RerankerPort)


def test_mock_reranker_scores_by_overlap() -> None:
    reranker = MockReranker()
    docs = [
        RerankDocument(record_id="a", text="red car at the gate"),
        RerankDocument(record_id="b", text="blue bicycle by the wall"),
    ]
    scores = run_blocking(reranker.rerank("red car", docs))
    by_id = {s.record_id: s.score for s in scores}
    # "a" shares both query terms; "b" shares none.
    assert by_id["a"] > by_id["b"]
    assert len(scores) == 2


def test_mock_reranker_deterministic() -> None:
    reranker = MockReranker()
    docs = [RerankDocument(record_id="a", text="alpha beta")]
    first = run_blocking(reranker.rerank("alpha", docs))
    second = run_blocking(reranker.rerank("alpha", docs))
    assert first[0].score == second[0].score


# ---- siliconflow reranker adapter (fake transport) -------------------------


def _transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def test_siliconflow_reranker_builds_payload_and_parses() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.9},
            ]},
        )

    client = httpx.AsyncClient(transport=_transport(handler))
    reranker = SiliconFlowReranker(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="SECRET_KEY_VALUE",
        client=client,
    )
    docs = [
        RerankDocument(record_id="a", text="doc a"),
        RerankDocument(record_id="b", text="doc b"),
    ]
    scores = run_blocking(reranker.rerank("q", docs))
    run_blocking(client.aclose())
    # Scores aligned back to input order by provider index.
    assert scores[0].record_id == "a"
    assert scores[0].score == 0.2
    assert scores[1].record_id == "b"
    assert scores[1].score == 0.9
    # Payload shape: model + query + documents (texts only).
    body = captured["body"]
    assert body["model"] == "Qwen/Qwen3-Reranker-8B"
    assert body["query"] == "q"
    assert body["documents"] == ["doc a", "doc b"]
    assert str(captured["url"]).endswith("/rerank")


def test_siliconflow_reranker_http_error_maps_and_no_key_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=_transport(handler))
    reranker = SiliconFlowReranker(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="SUPER_SECRET",
        client=client,
        max_retries=0,
    )
    with pytest.raises(RerankerError) as exc:
        run_blocking(reranker.rerank("q", [RerankDocument(record_id="a", text="t")]))
    run_blocking(client.aclose())
    assert "SUPER_SECRET" not in str(exc.value)


def test_siliconflow_reranker_timeout_maps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    client = httpx.AsyncClient(transport=_transport(handler))
    reranker = SiliconFlowReranker(
        base_url="http://x/api", api_key="k", client=client, max_retries=0
    )
    with pytest.raises(RerankerError):
        run_blocking(reranker.rerank("q", [RerankDocument(record_id="a", text="t")]))
    run_blocking(client.aclose())


def test_siliconflow_reranker_empty_docs_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called for empty documents")

    client = httpx.AsyncClient(transport=_transport(handler))
    reranker = SiliconFlowReranker(base_url="http://x/api", api_key="k", client=client)
    assert run_blocking(reranker.rerank("q", [])) == []
    run_blocking(client.aclose())


# ---- search service rerank stage -------------------------------------------


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="cmd", analysis_job_id="job_test", records=list(records)
        )
    )


def _record(record_id: str, *, static_text: str, start_ms: int = 0) -> ObservationRecord:
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
        static_description_text=static_text,
        dynamic_description_text="moving",
        tags=["person"],
        access_policy_id="policy_public_area",
        security_level=SecurityLevel.INTERNAL,
    )


class _RecordingReranker:
    """Reranker spy that records which documents it was asked to score."""

    model_id = "spy-reranker"

    def __init__(self) -> None:
        self.seen_ids: list[str] = []

    async def rerank(self, query: str, documents: list[RerankDocument]):  # type: ignore[no-untyped-def]
        from cctv_memory.services.reranker import RerankScore

        self.seen_ids = [d.record_id for d in documents]
        # Score so that the LAST document wins, to prove reordering happened.
        return [
            RerankScore(record_id=d.record_id, score=float(i))
            for i, d in enumerate(documents)
        ]


def test_cross_encoder_rerank_reorders_authorized_candidates(
    factory: SqliteRepositoryFactory,
) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _record("obs_a", static_text="alpha keyword present", start_ms=0),
        _record("obs_b", static_text="alpha keyword present too", start_ms=12_000),
    )
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    spy = _RecordingReranker()
    svc = SearchService(
        factory.observation_read(), factory.search_context(), factory.audit(),
        reranker=spy, rerank_enabled=True, rerank_top_n=50,
    )
    start = svc.start_search(StartObservationSearchRequest(query_text="alpha", top_k=10), scope)
    refined = svc.refine_search(
        start.context_id,
        RefineObservationSearchRequest(
            base_revision_id=start.revision_id,
            op=RefineOp.RERANK_CURRENT_CANDIDATES,
            params={"query_text": "alpha", "top_k": 10},
        ),
        scope,
    )
    # The reranker saw exactly the authorized candidates (both public records).
    assert set(spy.seen_ids) == {"obs_a", "obs_b"}
    # Top result carries the cross-encoder score + model in score_detail.
    top = refined.results[0]
    assert "rerank_score" in top.score_detail
    assert top.score_detail["rerank_model"] == "spy-reranker"


def test_cross_encoder_disabled_by_default(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_a", static_text="alpha"))
    scope = make_scope(
        camera_ids=["cam_lobby_01"], location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    spy = _RecordingReranker()
    # rerank_enabled defaults False -> reranker never invoked.
    svc = SearchService(
        factory.observation_read(), factory.search_context(), factory.audit(),
        reranker=spy,
    )
    start = svc.start_search(StartObservationSearchRequest(query_text="alpha", top_k=10), scope)
    svc.refine_search(
        start.context_id,
        RefineObservationSearchRequest(
            base_revision_id=start.revision_id,
            op=RefineOp.RERANK_CURRENT_CANDIDATES,
            params={"query_text": "alpha", "top_k": 10},
        ),
        scope,
    )
    assert spy.seen_ids == []
