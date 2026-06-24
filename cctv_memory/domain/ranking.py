"""Search ranking math (pure domain).

Reciprocal Rank Fusion (RRF) and boost combination per search-contract §5.
Pure functions — no infrastructure, deterministic, unit-testable.

RRF: score = Σ weight_i * (1 / (rank_i + k)) + boosts
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (pure domain math).

    Returns 0.0 for mismatched lengths or a zero-norm vector (fail-soft, so a
    degenerate vector never crashes ranking). Deterministic.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def to_rank_map(scores: dict[str, float]) -> dict[str, int]:
    """Convert a {id: relevance} map to {id: rank} (1-based, higher relevance first).

    Deterministic tie-break by record_id so equal scores rank stably.
    """
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return {rid: i + 1 for i, (rid, _) in enumerate(ordered)}


@dataclass
class RrfWeights:
    """Configurable RRF weights and boosts (search-contract §5.2).

    ``vector_weight`` weights the optional semantic vector channel (C2). It is a
    third RRF rank list fused alongside the static/dynamic FTS channels; when no
    vector scores are supplied (indexing disabled / vectors absent) the channel
    contributes nothing and fusion is byte-identical to the FTS-only behavior.
    """

    k: int = 60
    static_weight: float = 1.0
    dynamic_weight: float = 1.0
    fts_weight: float = 0.5
    vector_weight: float = 1.0
    max_tag_boost: float = 0.2
    max_analysis_scale_boost: float = 0.2
    config_version: str = "search-v1"


@dataclass
class ChannelInputs:
    """Per-record channel signals fed into fusion.

    ``vector_ranks`` / ``vector_scores`` carry the optional semantic channel (C2):
    ``vector_ranks`` feeds RRF, ``vector_scores`` (raw cosine) is surfaced in
    ``score_detail`` as ``static_score`` / ``dynamic_score`` per search-contract
    §5.1 without altering the deterministic RRF math.
    """

    static_ranks: dict[str, int] = field(default_factory=dict)
    dynamic_ranks: dict[str, int] = field(default_factory=dict)
    tag_boosts: dict[str, float] = field(default_factory=dict)
    scale_boosts: dict[str, float] = field(default_factory=dict)
    vector_ranks: dict[str, int] = field(default_factory=dict)
    static_vector_scores: dict[str, float] = field(default_factory=dict)
    dynamic_vector_scores: dict[str, float] = field(default_factory=dict)


def rrf_fuse(
    candidate_ids: list[str], inputs: ChannelInputs, weights: RrfWeights
) -> dict[str, tuple[float, dict[str, object]]]:
    """Fuse channels into {record_id: (final_score, score_detail)} deterministically."""
    result: dict[str, tuple[float, dict[str, object]]] = {}
    for rid in candidate_ids:
        s_rank = inputs.static_ranks.get(rid)
        d_rank = inputs.dynamic_ranks.get(rid)
        v_rank = inputs.vector_ranks.get(rid)
        static_component = (
            weights.static_weight * (1.0 / (s_rank + weights.k)) if s_rank else 0.0
        )
        dynamic_component = (
            weights.dynamic_weight * (1.0 / (d_rank + weights.k)) if d_rank else 0.0
        )
        vector_component = (
            weights.vector_weight * (1.0 / (v_rank + weights.k)) if v_rank else 0.0
        )
        tag_boost = min(weights.max_tag_boost, inputs.tag_boosts.get(rid, 0.0))
        scale_boost = min(weights.max_analysis_scale_boost, inputs.scale_boosts.get(rid, 0.0))
        rrf_score = static_component + dynamic_component + vector_component
        final = rrf_score + tag_boost + scale_boost
        detail: dict[str, object] = {
            "static_rank": s_rank,
            "dynamic_rank": d_rank,
            "static_component": round(static_component, 6),
            "dynamic_component": round(dynamic_component, 6),
            "tag_boost": round(tag_boost, 6),
            "analysis_scale_boost": round(scale_boost, 6),
            "rrf_score": round(rrf_score, 6),
            "final_score": round(final, 6),
            "config_version": weights.config_version,
        }
        # Optional semantic vector channel (search-contract §5.1). Only emitted
        # when a vector signal exists for this record so FTS-only score_detail is
        # unchanged when indexing is disabled.
        if v_rank is not None:
            detail["vector_rank"] = v_rank
            detail["vector_component"] = round(vector_component, 6)
        if rid in inputs.static_vector_scores:
            detail["static_score"] = round(inputs.static_vector_scores[rid], 6)
        if rid in inputs.dynamic_vector_scores:
            detail["dynamic_score"] = round(inputs.dynamic_vector_scores[rid], 6)
        result[rid] = (final, detail)
    return result


def order_candidates(
    fused: dict[str, tuple[float, dict[str, object]]],
) -> list[tuple[str, float, dict[str, object]]]:
    """Order fused candidates by final score desc, tie-break by record_id asc."""
    items = [(rid, score, detail) for rid, (score, detail) in fused.items()]
    items.sort(key=lambda x: (-x[1], x[0]))
    return items
