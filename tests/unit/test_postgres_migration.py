from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cctv_memory.ops.postgres_migration import validate_sqlite_vectors_for_pgvector


def test_validate_sqlite_vectors_quarantines_mismatches(tmp_path: Path) -> None:
    db = tmp_path / "vectors.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE observation_vectors(
              record_id TEXT,
              vector_type TEXT,
              embedding TEXT,
              metadata_json TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO observation_vectors VALUES (?, ?, ?, ?)",
            (
                "obs_ok",
                "static",
                json.dumps([0.1, 0.2]),
                json.dumps({"model_id": "m", "dimension": 2}),
            ),
        )
        conn.execute(
            "INSERT INTO observation_vectors VALUES (?, ?, ?, ?)",
            (
                "obs_bad",
                "static",
                json.dumps([0.1]),
                json.dumps({"model_id": "m", "dimension": 1}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = validate_sqlite_vectors_for_pgvector(
        db, expected_model_id="m", expected_dimension=2
    )

    assert report.total_rows == 2
    assert report.convertible_rows == 1
    assert [(i.record_id, i.vector_type) for i in report.issues] == [("obs_bad", "static")]
    assert "dimension mismatch" in report.issues[0].reason
