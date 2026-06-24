"""Deterministic offline mock reranker (task §C3).

The default ``RerankerPort`` implementation for CI/offline use: it makes no
network call and produces stable, deterministic relevance scores from simple
lexical token overlap between the query and each document. The same (query,
document) pair always yields the same score, which is enough to test the rerank
wiring and authorized-scope confinement without a real provider.

The score is NOT a real cross-encoder relevance; the real provider adapter
(``siliconflow_reranker``) provides that. The mock is only for deterministic,
offline tests and as the safe default.
"""

from __future__ import annotations

from cctv_memory.services.reranker import RerankDocument, RerankScore


class MockReranker:
    """Deterministic, network-free ``RerankerPort`` implementation."""

    def __init__(self, *, model_id: str = "mock-reranker-v1") -> None:
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    async def rerank(
        self, query: str, documents: list[RerankDocument]
    ) -> list[RerankScore]:
        query_terms = {t for t in query.lower().split() if t}
        scores: list[RerankScore] = []
        for doc in documents:
            doc_terms = {t for t in doc.text.lower().split() if t}
            if not query_terms or not doc_terms:
                overlap = 0.0
            else:
                overlap = len(query_terms & doc_terms) / len(query_terms)
            scores.append(RerankScore(record_id=doc.record_id, score=round(overlap, 6)))
        return scores
