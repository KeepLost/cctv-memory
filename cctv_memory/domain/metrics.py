"""Retrieval metrics (pure domain).

precision@k, recall@k, and MRR over a golden query set. Pure functions —
deterministic, no infrastructure. Used by the benchmark and experiment runners.
"""

from __future__ import annotations

from dataclasses import dataclass


def precision_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-k results that are relevant."""
    if k <= 0:
        return 0.0
    top = ranked_ids[:k]
    if not top:
        return 0.0
    hits = sum(1 for rid in top if rid in relevant)
    return hits / len(top)


def recall_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    """Fraction of relevant items retrieved within the top-k results."""
    if not relevant:
        return 0.0
    top = set(ranked_ids[:k])
    hits = len(top & relevant)
    return hits / len(relevant)


def reciprocal_rank(ranked_ids: list[str], relevant: set[str]) -> float:
    """1/rank of the first relevant result, or 0 if none present."""
    for i, rid in enumerate(ranked_ids):
        if rid in relevant:
            return 1.0 / (i + 1)
    return 0.0


@dataclass(frozen=True)
class QueryMetrics:
    """Per-query metrics."""

    query_id: str
    precision_at_k: float
    recall_at_k: float
    reciprocal_rank: float


@dataclass(frozen=True)
class AggregateMetrics:
    """Mean metrics over a query set (MRR = mean reciprocal rank)."""

    k: int
    num_queries: int
    mean_precision_at_k: float
    mean_recall_at_k: float
    mrr: float


def evaluate_query(
    query_id: str, ranked_ids: list[str], relevant: set[str], k: int
) -> QueryMetrics:
    """Compute per-query metrics for one ranked result list."""
    return QueryMetrics(
        query_id=query_id,
        precision_at_k=precision_at_k(ranked_ids, relevant, k),
        recall_at_k=recall_at_k(ranked_ids, relevant, k),
        reciprocal_rank=reciprocal_rank(ranked_ids, relevant),
    )


def aggregate(per_query: list[QueryMetrics], k: int) -> AggregateMetrics:
    """Aggregate per-query metrics into means (MRR = mean reciprocal rank)."""
    n = len(per_query)
    if n == 0:
        return AggregateMetrics(k=k, num_queries=0, mean_precision_at_k=0.0,
                                mean_recall_at_k=0.0, mrr=0.0)
    return AggregateMetrics(
        k=k,
        num_queries=n,
        mean_precision_at_k=round(sum(q.precision_at_k for q in per_query) / n, 6),
        mean_recall_at_k=round(sum(q.recall_at_k for q in per_query) / n, 6),
        mrr=round(sum(q.reciprocal_rank for q in per_query) / n, 6),
    )
