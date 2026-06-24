"""C2 unit tests: vector channel ranking math + cosine similarity (pure domain)."""

from __future__ import annotations

import math

from cctv_memory.domain import ranking


def test_cosine_similarity_basic() -> None:
    assert ranking.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert ranking.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    # Opposite vectors -> -1.
    assert math.isclose(ranking.cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)


def test_cosine_similarity_fail_soft() -> None:
    # Mismatched length / empty / zero-norm all return 0.0 (never crash ranking).
    assert ranking.cosine_similarity([1.0], [1.0, 2.0]) == 0.0
    assert ranking.cosine_similarity([], [1.0]) == 0.0
    assert ranking.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_rrf_fuse_without_vector_channel_is_unchanged() -> None:
    # When no vector signal is supplied, score_detail has no vector keys and the
    # fused score equals the static+dynamic-only RRF (backward compatible).
    inputs = ranking.ChannelInputs(
        static_ranks={"a": 1, "b": 2},
        dynamic_ranks={"a": 2, "b": 1},
    )
    weights = ranking.RrfWeights()
    fused = ranking.rrf_fuse(["a", "b"], inputs, weights)
    score_a, detail_a = fused["a"]
    assert "vector_rank" not in detail_a
    assert "static_score" not in detail_a
    expected = weights.static_weight * (1 / (1 + weights.k)) + (
        weights.dynamic_weight * (1 / (2 + weights.k))
    )
    assert math.isclose(score_a, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_rrf_fuse_vector_channel_adds_component_and_detail() -> None:
    inputs = ranking.ChannelInputs(
        static_ranks={"a": 1},
        vector_ranks={"a": 1},
        static_vector_scores={"a": 0.87},
    )
    weights = ranking.RrfWeights()
    fused = ranking.rrf_fuse(["a"], inputs, weights)
    score, detail = fused["a"]
    assert detail["vector_rank"] == 1
    assert detail["static_score"] == 0.87
    # final includes both the static FTS rank component and the vector component.
    expected = weights.static_weight * (1 / (1 + weights.k)) + (
        weights.vector_weight * (1 / (1 + weights.k))
    )
    assert math.isclose(score, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_rrf_fuse_is_deterministic() -> None:
    inputs = ranking.ChannelInputs(
        static_ranks={"a": 1, "b": 2, "c": 3},
        vector_ranks={"c": 1, "b": 2, "a": 3},
    )
    weights = ranking.RrfWeights()
    first = ranking.order_candidates(ranking.rrf_fuse(["a", "b", "c"], inputs, weights))
    second = ranking.order_candidates(ranking.rrf_fuse(["c", "b", "a"], inputs, weights))
    assert [r[0] for r in first] == [r[0] for r in second]
