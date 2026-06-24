"""Benchmark & experiment runners (application layer).

Run a golden query set through the SearchService and compute retrieval metrics
(precision@k / recall@k / MRR). The experiment runner compares several
search-weight arms. Per pipeline-experiment-contract, experiment variables are
config objects passed to the service — NOT if-branches in application logic, and
they never bypass repository ports.

These services benchmark the SEARCH layer over deterministic (mock-VLM) records;
they do not measure real VLM quality (honest scope).
"""

from __future__ import annotations

from cctv_memory.application.search import SearchService
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.experiment import (
    ArmResult,
    BenchmarkResult,
    ExperimentConfig,
    ExperimentResult,
    GoldenQuery,
)
from cctv_memory.contracts.search import StartObservationSearchRequest
from cctv_memory.domain import metrics, ranking
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.observation import ObservationRecordReadRepository
from cctv_memory.repositories.search_context import SearchContextRepository


def _ranked_ids(
    service: SearchService, query: GoldenQuery, scope: AuthorizedScope, k: int
) -> list[str]:
    resp = service.start_search(
        StartObservationSearchRequest(
            query_text=query.query_text,
            search_mode=query.search_mode,
            top_k=k,
        ),
        scope,
    )
    return [item.record_id for item in resp.results]


class BenchmarkRunner:
    """Compute precision@k / recall@k / MRR for the default search config."""

    def __init__(
        self,
        observations: ObservationRecordReadRepository,
        contexts: SearchContextRepository,
        audit: AuditRepository,
        *,
        weights: ranking.RrfWeights | None = None,
    ) -> None:
        self._observations = observations
        self._contexts = contexts
        self._audit = audit
        self._weights = weights or ranking.RrfWeights()

    def run(
        self, queries: list[GoldenQuery], scope: AuthorizedScope, *, k: int = 10
    ) -> BenchmarkResult:
        service = SearchService(
            self._observations, self._contexts, self._audit, weights=self._weights
        )
        per_query = []
        for q in queries:
            ranked = _ranked_ids(service, q, scope, k)
            per_query.append(
                metrics.evaluate_query(q.query_id, ranked, set(q.relevant_record_ids), k)
            )
        agg = metrics.aggregate(per_query, k)
        return BenchmarkResult(
            k=agg.k,
            num_queries=agg.num_queries,
            mean_precision_at_k=agg.mean_precision_at_k,
            mean_recall_at_k=agg.mean_recall_at_k,
            mrr=agg.mrr,
            per_query={
                qm.query_id: {
                    "precision_at_k": qm.precision_at_k,
                    "recall_at_k": qm.recall_at_k,
                    "reciprocal_rank": qm.reciprocal_rank,
                }
                for qm in per_query
            },
        )


class ExperimentRunner:
    """Compare several search-weight arms over a golden query set."""

    def __init__(
        self,
        observations: ObservationRecordReadRepository,
        contexts: SearchContextRepository,
        audit: AuditRepository,
    ) -> None:
        self._observations = observations
        self._contexts = contexts
        self._audit = audit

    def run(self, config: ExperimentConfig, scope: AuthorizedScope) -> ExperimentResult:
        arms: list[ArmResult] = []
        for arm in config.arms:
            weights = ranking.RrfWeights(
                k=arm.rrf_k,
                static_weight=arm.static_weight,
                dynamic_weight=arm.dynamic_weight,
                fts_weight=arm.fts_weight,
                max_tag_boost=arm.max_tag_boost,
                max_analysis_scale_boost=arm.max_analysis_scale_boost,
                config_version=arm.name,
            )
            runner = BenchmarkRunner(
                self._observations, self._contexts, self._audit, weights=weights
            )
            result = runner.run(config.queries, scope, k=config.k)
            arms.append(
                ArmResult(
                    arm_name=arm.name,
                    k=result.k,
                    num_queries=result.num_queries,
                    mean_precision_at_k=result.mean_precision_at_k,
                    mean_recall_at_k=result.mean_recall_at_k,
                    mrr=result.mrr,
                    per_query=result.per_query,
                )
            )
        best = max(arms, key=lambda a: a.mrr).arm_name if arms else None
        return ExperimentResult(
            experiment_name=config.name, k=config.k, arms=arms, best_arm_by_mrr=best
        )
