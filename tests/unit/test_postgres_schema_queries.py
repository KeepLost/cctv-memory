from __future__ import annotations

from cctv_memory.infrastructure.db.postgres.schema import postgres_schema_ddl
from cctv_memory.infrastructure.db.postgres.vector import (
    build_candidate_vector_score_sql,
    serialize_pgvector,
)
from sqlalchemy.dialects import postgresql


def test_postgres_schema_ddl_uses_jsonb_timestamptz_and_pgvector() -> None:
    ddl = "\n".join(postgres_schema_ddl(vector_dimension=1024))

    assert "CREATE EXTENSION IF NOT EXISTS vector" in ddl
    assert "JSONB" in ddl
    assert "TIMESTAMPTZ" in ddl
    assert "embedding vector(1024)" in ddl
    assert "detector_gate_logs" in ddl
    assert "decision_json JSONB" in ddl
    assert "analysis_timeline_events" in ddl
    assert "correlation_json JSONB" in ddl
    assert "occurred_at TIMESTAMPTZ" in ddl
    assert "observation_text_index" in ddl


def test_pgvector_candidate_query_is_candidate_bounded() -> None:
    stmt = build_candidate_vector_score_sql(
        candidate_ids=["obs_1", "obs_2"],
        vector_type="static",
        model_id="mock-embedder",
        query_embedding=[0.1, 0.2],
        limit=5,
    )

    sql = str(stmt.compile(dialect=postgresql.dialect()))

    assert "unnest" in sql
    assert "authorized_candidate_ids" in sql
    assert "observation_vectors" in sql
    assert "<=>" in sql
    assert "JOIN candidates" in sql


def test_pgvector_serialization_rejects_dimension_mismatch() -> None:
    assert serialize_pgvector([0.1, 0.2], expected_dimension=2) == "[0.1,0.2]"

    try:
        serialize_pgvector([0.1], expected_dimension=2)
    except ValueError as exc:
        assert "dimension" in str(exc)
    else:  # pragma: no cover - defensive failure message
        raise AssertionError("dimension mismatch should fail explicitly")
