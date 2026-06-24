"""pgvector serialization and candidate-bounded scoring SQL.

This module intentionally exposes only candidate-id-bounded vector query helpers.
There is no whole-corpus nearest-neighbor helper, preserving the authorization
before ranking invariant at the adapter boundary.
"""

from __future__ import annotations

import math

from sqlalchemy import bindparam, text
from sqlalchemy.sql.elements import TextClause


def serialize_pgvector(values: list[float], *, expected_dimension: int) -> str:
    """Serialize a vector literal and reject dimension/finite-value mismatches."""
    if len(values) != expected_dimension:
        raise ValueError(
            f"embedding dimension mismatch: got {len(values)}, expected {expected_dimension}"
        )
    out: list[str] = []
    for value in values:
        f = float(value)
        if not math.isfinite(f):
            raise ValueError("embedding contains non-finite value")
        out.append(format(f, ".12g"))
    return "[" + ",".join(out) + "]"


def build_candidate_vector_score_sql(
    *,
    candidate_ids: list[str],
    vector_type: str,
    model_id: str,
    query_embedding: list[float],
    limit: int,
) -> TextClause:
    """Build exact pgvector scoring SQL over an explicit authorized id set."""
    dimension = len(query_embedding)
    embedding = serialize_pgvector(query_embedding, expected_dimension=dimension)
    stmt = text(
        """
        WITH candidates(record_id) AS (
          SELECT unnest(CAST(:authorized_candidate_ids AS text[]))
        )
        SELECT v.record_id,
               1 - (v.embedding <=> CAST(:query_embedding AS vector)) AS similarity
        FROM observation_vectors v
        JOIN candidates c ON c.record_id = v.record_id
        WHERE v.vector_type = :vector_type
          AND v.model_id = :model_id
          AND v.dimension = :dimension
        ORDER BY v.embedding <=> CAST(:query_embedding AS vector), v.record_id
        LIMIT :limit
        """
    ).bindparams(
        bindparam("authorized_candidate_ids", value=candidate_ids),
        bindparam("query_embedding", value=embedding),
        bindparam("vector_type", value=vector_type),
        bindparam("model_id", value=model_id),
        bindparam("dimension", value=dimension),
        bindparam("limit", value=limit),
    )
    return stmt
