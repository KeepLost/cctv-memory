"""Experiment & benchmark contracts (pipeline-experiment-contract).

DTOs for the search experiment runner and benchmark. Experiment variables live
in config objects (not application if-branches), per the experiment contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import SearchMode


class GoldenQuery(ContractModel):
    """A benchmark query with its known-relevant record ids."""

    query_id: str
    query_text: str | None = None
    search_mode: SearchMode = SearchMode.HYBRID
    relevant_record_ids: list[str] = Field(default_factory=list)


class SearchWeightConfig(ContractModel):
    """A search-weight variant for an experiment arm (search-contract §5)."""

    name: str
    rrf_k: int = 60
    static_weight: float = 1.0
    dynamic_weight: float = 1.0
    fts_weight: float = 0.5
    max_tag_boost: float = 0.2
    max_analysis_scale_boost: float = 0.2


class ExperimentConfig(ContractModel):
    """An experiment: compare several search-weight arms on a golden query set."""

    name: str = "search-weight-experiment"
    k: int = 10
    arms: list[SearchWeightConfig] = Field(default_factory=list)
    queries: list[GoldenQuery] = Field(default_factory=list)


class ArmResult(ContractModel):
    """Aggregate metrics for one experiment arm."""

    arm_name: str
    k: int
    num_queries: int
    mean_precision_at_k: float
    mean_recall_at_k: float
    mrr: float
    per_query: dict[str, Any] = Field(default_factory=dict)


class ExperimentResult(ContractModel):
    """Structured, reproducible experiment comparison."""

    experiment_name: str
    k: int
    arms: list[ArmResult] = Field(default_factory=list)
    best_arm_by_mrr: str | None = None


class BenchmarkResult(ContractModel):
    """Benchmark metrics on the golden query set for the default config."""

    k: int
    num_queries: int
    mean_precision_at_k: float
    mean_recall_at_k: float
    mrr: float
    per_query: dict[str, Any] = Field(default_factory=dict)
