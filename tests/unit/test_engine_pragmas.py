"""Engine pragma tests (task cctv-memory-20260616-1850, Phase 1a / A0).

The SQLite adapter must apply WAL + synchronous=NORMAL per connection. NORMAL is
the write-throughput tuning (no per-commit fsync) and is crash-safe under WAL
(no corruption); the durability trade-off is owner-approved. These pragmas are a
backend-specific decision living in the adapter layer only.
"""

from __future__ import annotations

from pathlib import Path

from cctv_memory.infrastructure.db.engine import create_sqlite_engine


def _pragma(engine, name: str):  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        from sqlalchemy import text

        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_sqlite_engine_applies_wal_and_synchronous_normal(tmp_path: Path) -> None:
    engine = create_sqlite_engine(str(tmp_path / "t.sqlite3"))
    try:
        # journal_mode=WAL
        assert str(_pragma(engine, "journal_mode")).lower() == "wal"
        # synchronous=NORMAL == 1 (FULL would be 2). This is the key tuning.
        assert int(_pragma(engine, "synchronous")) == 1
        # foreign_keys ON and busy_timeout still applied (regression guard).
        assert int(_pragma(engine, "foreign_keys")) == 1
        assert int(_pragma(engine, "busy_timeout")) == 5000
    finally:
        engine.dispose()
