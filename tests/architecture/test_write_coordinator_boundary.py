"""WriteCoordinator boundary tests (task cctv-memory-20260616-1850, Phase 3 / A1).

Asserts the DB write-serialization policy now lives at the database adapter
boundary (constitution §7) and the worker/business layer no longer owns a
SQLite-specific global ``threading.Lock``.
"""

from __future__ import annotations

import threading
import time

from cctv_memory.infrastructure.db.write_coordinator import (
    NullWriteCoordinator,
    SqliteWriteCoordinator,
)
from cctv_memory.services.write_coordinator import WriteCoordinator


def test_sqlite_coordinator_satisfies_port() -> None:
    assert isinstance(SqliteWriteCoordinator(), WriteCoordinator)
    assert isinstance(NullWriteCoordinator(), WriteCoordinator)


def test_sqlite_coordinator_serializes_concurrent_writers() -> None:
    """Only one thread may be inside ``write()`` at a time (single-writer)."""
    coord = SqliteWriteCoordinator()
    inside = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _worker() -> None:
        nonlocal inside, peak
        barrier.wait()
        with coord.write():
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.01)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1, f"coordinator allowed {peak} concurrent writers (must serialize)"


def test_null_coordinator_does_not_serialize() -> None:
    """NullWriteCoordinator (future PG/MVCC) is a no-op: writers overlap freely."""
    coord = NullWriteCoordinator()
    inside = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def _worker() -> None:
        nonlocal inside, peak
        barrier.wait()
        with coord.write():
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak >= 2, "no-op coordinator should not serialize writers"


def test_worker_does_not_own_a_db_write_lock() -> None:
    """The worker/business layer must not own a SQLite-specific write lock.

    Phase 3 moved serialization to the DB boundary (WriteCoordinator). Guard
    against regressing to a worker-owned ``threading.Lock`` global policy.
    """
    import inspect

    from cctv_memory.workers import analysis_worker, default_segment, high_freq_event

    for module in (analysis_worker, default_segment, high_freq_event):
        src = inspect.getsource(module)
        assert "_db_write_lock" not in src, (
            f"{module.__name__} still references a worker-owned _db_write_lock"
        )


def test_sqlite_coordinator_sets_write_intent_only_inside_write() -> None:
    """SQLite coordinator marks write-intent for BEGIN IMMEDIATE at DB boundary."""
    from cctv_memory.infrastructure.db.write_intent import is_write_intent

    coord = SqliteWriteCoordinator()
    assert is_write_intent() is False
    with coord.write():
        assert is_write_intent() is True
    assert is_write_intent() is False


def test_worker_lifecycle_paths_use_coordinated_write_session() -> None:
    """Guard that lifecycle writes don't bypass the DB write boundary again."""
    import inspect

    from cctv_memory.workers.analysis_worker import AnalysisWorker

    coordinated_methods = (
        "_process_claimed_task",
        "_process_cross_scale",
        "_transition_running",
        "_process_scale_task",
        "_skip_scale_task",
        "_reconcile_running_units_for_job",
        "_fail_scale_task",
        "_handle_required_failure",
        "recover_orphans",
    )
    for method_name in coordinated_methods:
        src = inspect.getsource(getattr(AnalysisWorker, method_name))
        assert "_write_session" in src, f"{method_name} bypasses coordinated writes"


def test_worker_frame_extraction_forwards_unique_unit_key() -> None:
    """Production VLM unit paths must isolate frame files by model_call_id.

    Guards the historical R10 frame-path collision: worker calls to
    ``select_frames_for_unit`` must pass ``unit_key=mcall_id`` so concurrent units
    cannot share output dirs or cleanup each other's media.
    """
    import inspect

    from cctv_memory.workers.default_segment import DefaultSegmentProcessor
    from cctv_memory.workers.high_freq_event import HighFreqEventProcessor

    for cls in (DefaultSegmentProcessor, HighFreqEventProcessor):
        for method_name in ("_execute_running_unit", "_run_unit"):
            src = inspect.getsource(getattr(cls, method_name))
            assert "select_frames_for_unit" in src
            assert "unit_key=mcall_id" in src, (
                f"{cls.__name__}.{method_name} does not forward unit_key=mcall_id"
            )
