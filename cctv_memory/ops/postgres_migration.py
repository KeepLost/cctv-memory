"""SQLite -> PostgreSQL migration validation helpers.

These helpers are offline and do not call embedding providers. They validate
existing SQLite vector artifacts before an operator imports them into pgvector.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VectorMigrationIssue:
    record_id: str
    vector_type: str
    reason: str


@dataclass(frozen=True)
class VectorMigrationReport:
    total_rows: int
    convertible_rows: int
    issues: list[VectorMigrationIssue] = field(default_factory=list)


def validate_sqlite_vectors_for_pgvector(
    sqlite_path: str | Path,
    *,
    expected_model_id: str,
    expected_dimension: int,
) -> VectorMigrationReport:
    """Validate SQLite JSON embeddings before pgvector import.

    Rows with missing/mismatched model/dimension or non-finite values are reported
    as issues. The function never mutates either database and never pads/truncates.
    """
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute(
            "SELECT record_id, vector_type, embedding, metadata_json FROM observation_vectors"
        ).fetchall()
    finally:
        conn.close()

    issues: list[VectorMigrationIssue] = []
    convertible = 0
    for record_id, vector_type, embedding_json, metadata_json in rows:
        reason = _vector_issue(
            embedding_json,
            metadata_json,
            expected_model_id=expected_model_id,
            expected_dimension=expected_dimension,
        )
        if reason is None:
            convertible += 1
        else:
            issues.append(
                VectorMigrationIssue(
                    record_id=str(record_id),
                    vector_type=str(vector_type),
                    reason=reason,
                )
            )
    return VectorMigrationReport(
        total_rows=len(rows), convertible_rows=convertible, issues=issues
    )


def _vector_issue(
    embedding_json: str,
    metadata_json: str,
    *,
    expected_model_id: str,
    expected_dimension: int,
) -> str | None:
    try:
        embedding_raw: Any = json.loads(embedding_json)
        metadata_raw: Any = json.loads(metadata_json or "{}")
    except json.JSONDecodeError as exc:
        return f"invalid json: {exc.msg}"
    if not isinstance(embedding_raw, list):
        return "embedding is not a list"
    if not isinstance(metadata_raw, dict):
        return "metadata is not an object"
    model_id = metadata_raw.get("model_id")
    if model_id != expected_model_id:
        return f"model_id mismatch: {model_id!r} != {expected_model_id!r}"
    dimension = metadata_raw.get("dimension", len(embedding_raw))
    if dimension != expected_dimension or len(embedding_raw) != expected_dimension:
        return (
            "dimension mismatch: "
            f"metadata={dimension!r}, len={len(embedding_raw)}, expected={expected_dimension}"
        )
    for value in embedding_raw:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "embedding contains non-numeric value"
        if not math.isfinite(number):
            return "embedding contains non-finite value"
    return None
