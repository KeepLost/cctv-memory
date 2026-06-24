"""SQLite write-intent / BEGIN IMMEDIATE regression tests.

Task cctv-memory-20260617-1441: company deployment is multi-process, so the
process-local WriteCoordinator lock is not sufficient. Coordinated write sections
must also begin SQLite transactions as ``BEGIN IMMEDIATE`` so SQLite's own file
lock serializes writers across processes and avoids deferred read->write upgrade
BUSY/lost-update behavior.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from cctv_memory.infrastructure.db.engine import (
    create_session_factory,
    create_sqlite_engine,
)
from cctv_memory.infrastructure.db.write_coordinator import SqliteWriteCoordinator
from sqlalchemy import text


def _increment_counter(db_path: str, iterations: int, out: mp.Queue[str | None]) -> None:
    engine = create_sqlite_engine(db_path)
    session_factory = create_session_factory(engine)
    coord = SqliteWriteCoordinator()
    try:
        for _ in range(iterations):
            with coord.write():
                session = session_factory()
                try:
                    value = session.execute(
                        text("SELECT value FROM counters WHERE id = 1")
                    ).scalar_one()
                    session.execute(
                        text("UPDATE counters SET value = :value WHERE id = 1"),
                        {"value": int(value) + 1},
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
    except Exception as exc:  # noqa: BLE001 - report child failure to parent
        out.put(f"{type(exc).__name__}: {exc}")
    else:
        out.put(None)
    finally:
        engine.dispose()


def test_begin_immediate_serializes_read_then_write_across_processes(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "multi_process.sqlite3")
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE counters (id INTEGER PRIMARY KEY, value INTEGER)"))
        conn.execute(text("INSERT INTO counters(id, value) VALUES (1, 0)"))
    engine.dispose()

    processes = 4
    iterations = 25
    ctx = mp.get_context("spawn")
    out: mp.Queue[str | None] = ctx.Queue()
    children = [
        ctx.Process(target=_increment_counter, args=(db_path, iterations, out))
        for _ in range(processes)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=30)

    errors = [out.get(timeout=5) for _ in children]
    for child in children:
        assert child.exitcode == 0
    assert errors == [None] * processes

    engine = create_sqlite_engine(db_path)
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        final = session.execute(text("SELECT value FROM counters WHERE id = 1")).scalar_one()
    finally:
        session.close()
        engine.dispose()
    assert final == processes * iterations


def test_read_only_sessions_do_not_require_write_intent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "read_only.sqlite3")
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT)"))
        conn.execute(text("INSERT INTO rows(id, value) VALUES (1, 'ok')"))
    session_factory = create_session_factory(engine)
    session = session_factory()
    try:
        assert session.execute(text("SELECT value FROM rows WHERE id = 1")).scalar_one() == "ok"
        session.commit()
    finally:
        session.close()
        engine.dispose()
