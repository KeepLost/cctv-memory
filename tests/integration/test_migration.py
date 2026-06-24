"""Migration test: Alembic initial migration runs on a fresh temp SQLite DB.

testing-contract §8: fresh_db_has_schema_version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(sqlite_path: Path) -> Config:
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", "cctv_memory/infrastructure/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{sqlite_path}")
    return cfg


def test_alembic_upgrade_head_creates_schema(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "migrated.sqlite3"
    # Ensure env.py resolves to this temp DB.
    monkeypatch.setenv("CCTV_MEMORY_SQLITE_PATH", str(db_path))

    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # Core fact tables present.
        for expected in (
            "observation_records",
            "observation_record_history",
            "analysis_jobs",
            "video_sources",
            "principals",
            "access_policies",
            "search_contexts",
            "analysis_tasks",
            "audit_events",
            "analysis_timeline_events",
            "observation_vectors",
            "schema_metadata",
        ):
            assert expected in tables, f"missing table {expected}"

        # FTS5 virtual tables present.
        for fts in (
            "observation_static_fts",
            "observation_dynamic_fts",
            "observation_tags_fts",
        ):
            assert fts in tables, f"missing fts table {fts}"

        # schema_version seeded.
        version = conn.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        assert version is not None and version[0] == "v1"
    finally:
        conn.close()


def test_alembic_downgrade_base(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path = tmp_path / "migrated2.sqlite3"
    monkeypatch.setenv("CCTV_MEMORY_SQLITE_PATH", str(db_path))
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "observation_records" not in tables
        assert "observation_static_fts" not in tables
    finally:
        conn.close()
