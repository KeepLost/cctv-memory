"""Live PostgreSQL parity for the capacity-estimation metric source.

Gated by ``CCTV_MEMORY_TEST_POSTGRES_DSN`` (skips locally when absent). Seeds the
same representative dataset as the SQLite unit test into a real PostgreSQL
instance (with native TIMESTAMPTZ/JSONB columns) and asserts
``collect_capacity_metrics`` returns the identical ``CapacityMetrics`` shape,
proving the offline capacity tool is backend-agnostic.
"""

from __future__ import annotations

import os

import pytest
from cctv_memory.ops.capacity_estimation import collect_capacity_metrics
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.postgres


def _dsn() -> str:
    dsn = os.environ.get("CCTV_MEMORY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("CCTV_MEMORY_TEST_POSTGRES_DSN is not set")
    return dsn


def _seed_capacity_schema(dsn: str) -> None:
    """Create a minimal capacity-relevant schema with PG-native types + data."""
    engine = create_engine(dsn, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(
                text(
                    """
                    CREATE TABLE video_sources (
                        video_id TEXT PRIMARY KEY,
                        camera_id TEXT NOT NULL,
                        duration_ms INTEGER NULL,
                        video_start_time TIMESTAMPTZ NOT NULL,
                        video_end_time TIMESTAMPTZ NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE analysis_jobs (
                        analysis_job_id TEXT PRIMARY KEY,
                        video_id TEXT NOT NULL,
                        job_status TEXT NOT NULL,
                        started_at TIMESTAMPTZ NULL,
                        finished_at TIMESTAMPTZ NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE model_call_logs (
                        model_call_id TEXT PRIMARY KEY,
                        analysis_job_id TEXT NOT NULL,
                        scale_task_id TEXT NOT NULL,
                        unit_id TEXT NOT NULL,
                        analysis_scale TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL,
                        duration_ms INTEGER NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE observation_records (
                        record_id TEXT PRIMARY KEY,
                        static_description_text TEXT NOT NULL,
                        dynamic_description_text TEXT NOT NULL,
                        tags_json JSONB NOT NULL,
                        attributes_json JSONB NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO video_sources VALUES
                      ('video_a','cam_a',3600000,'2026-06-22T00:00:00+00:00','2026-06-22T01:00:00+00:00'),
                      ('video_b','cam_b',1800000,'2026-06-22T00:00:00+00:00','2026-06-22T00:30:00+00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO analysis_jobs VALUES
                      ('job_a','video_a','succeeded','2026-06-22T00:00:00+00:00','2026-06-22T00:10:00+00:00','2026-06-22T00:00:00+00:00'),
                      ('job_b','video_b','partial_failed','2026-06-22T00:02:00+00:00','2026-06-22T00:12:00+00:00','2026-06-22T00:02:00+00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO model_call_logs VALUES
                      ('call_1','job_a','scale_a','unit_a1','default_segment','real','succeeded',1,4000,'2026-06-22T00:00:00+00:00'),
                      ('call_2','job_a','scale_a','unit_a2','high_freq_event','real','succeeded',1,6000,'2026-06-22T00:00:05+00:00'),
                      ('call_3','job_b','scale_b','unit_b1','default_segment','real','failed',2,8000,'2026-06-22T00:02:00+00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO observation_records VALUES
                      ('obs_detector_only','','',
                       CAST(:tags_empty AS jsonb), CAST(:attrs_off AS jsonb)),
                      ('obs_vlm_triggered','vlm static','vlm dynamic',
                       CAST(:tags_vlm AS jsonb), CAST(:attrs_on AS jsonb))
                    """
                ),
                {
                    "tags_empty": "[]",
                    "attrs_off": '{"detector_gate":{"triggered_vlm":false}}',
                    "tags_vlm": '["vlm_tag"]',
                    "attrs_on": '{"detector_gate":{"triggered_vlm":true}}',
                },
            )
    finally:
        engine.dispose()


def test_live_postgres_capacity_metrics_match_sqlite_shape() -> None:
    dsn = _dsn()
    _seed_capacity_schema(dsn)

    metrics = collect_capacity_metrics(dsn)

    # Identical to the SQLite unit-test expectations (same logical dataset).
    assert metrics.vlm_request_count == 3
    assert metrics.request_count_by_scale == {"default_segment": 2, "high_freq_event": 1}
    assert metrics.request_count_by_camera == {"cam_a": 2, "cam_b": 1}
    assert metrics.request_count_by_video == {"video_a": 2, "video_b": 1}
    assert metrics.request_count_by_job == {"job_a": 2, "job_b": 1}
    assert metrics.success_count == 2
    assert metrics.failed_count == 1
    assert metrics.retry_count_estimate == 1
    assert metrics.video_hours == pytest.approx(1.5)
    assert metrics.job_wall_time_seconds == pytest.approx(720.0)
    assert metrics.p50_latency_s == pytest.approx(6.0)
    assert metrics.p95_latency_s == pytest.approx(8.0)
    assert metrics.detector_only_record_count == 1
    assert metrics.vlm_triggered_detector_record_count == 1
    assert metrics.gate_positive_rate == pytest.approx(0.5)
    # The DSN (with credentials) must never appear in the reportable db label.
    assert metrics.db_path is not None
    assert "cctv:cctv" not in metrics.db_path
    assert "@" not in metrics.db_path
