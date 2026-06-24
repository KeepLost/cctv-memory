from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cctv_memory.ops.capacity_estimation import (
    BenchmarkAssumptions,
    CapacityInputs,
    collect_capacity_metrics,
    estimate_capacity,
    render_capacity_report,
)


def _create_representative_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE video_sources (
                video_id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL,
                duration_ms INTEGER NULL,
                video_start_time TEXT NOT NULL,
                video_end_time TEXT NULL
            );
            CREATE TABLE analysis_jobs (
                analysis_job_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                job_status TEXT NOT NULL,
                started_at TEXT NULL,
                finished_at TEXT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE analysis_units (
                unit_id TEXT PRIMARY KEY,
                analysis_job_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                analysis_scale TEXT NOT NULL
            );
            CREATE TABLE model_call_logs (
                model_call_id TEXT PRIMARY KEY,
                analysis_job_id TEXT NOT NULL,
                scale_task_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                analysis_scale TEXT NOT NULL,
                segment_start_ms INTEGER NOT NULL,
                segment_end_ms INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                started_at TEXT NULL,
                finished_at TEXT NULL,
                duration_ms INTEGER NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE observation_records (
                record_id TEXT PRIMARY KEY,
                static_description_text TEXT NOT NULL,
                dynamic_description_text TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                attributes_json TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO video_sources VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "video_a",
                    "cam_a",
                    3_600_000,
                    "2026-06-22T00:00:00+00:00",
                    "2026-06-22T01:00:00+00:00",
                ),
                (
                    "video_b",
                    "cam_b",
                    1_800_000,
                    "2026-06-22T00:00:00+00:00",
                    "2026-06-22T00:30:00+00:00",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO analysis_jobs VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "job_a",
                    "video_a",
                    "succeeded",
                    "2026-06-22T00:00:00+00:00",
                    "2026-06-22T00:10:00+00:00",
                    "2026-06-22T00:00:00+00:00",
                ),
                (
                    "job_b",
                    "video_b",
                    "partial_failed",
                    "2026-06-22T00:02:00+00:00",
                    "2026-06-22T00:12:00+00:00",
                    "2026-06-22T00:02:00+00:00",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO analysis_units VALUES (?, ?, ?, ?)",
            [
                ("unit_a1", "job_a", "video_a", "default_segment"),
                ("unit_a2", "job_a", "video_a", "high_freq_event"),
                ("unit_b1", "job_b", "video_b", "default_segment"),
            ],
        )
        conn.executemany(
            "INSERT INTO model_call_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "call_1",
                    "job_a",
                    "scale_a",
                    "unit_a1",
                    "default_segment",
                    0,
                    1000,
                    "real",
                    "qwen",
                    "succeeded",
                    1,
                    "2026-06-22T00:00:00+00:00",
                    "2026-06-22T00:00:04+00:00",
                    4000,
                    "2026-06-22T00:00:00+00:00",
                ),
                (
                    "call_2",
                    "job_a",
                    "scale_a",
                    "unit_a2",
                    "high_freq_event",
                    1000,
                    2000,
                    "real",
                    "qwen",
                    "succeeded",
                    1,
                    "2026-06-22T00:00:05+00:00",
                    "2026-06-22T00:00:11+00:00",
                    6000,
                    "2026-06-22T00:00:05+00:00",
                ),
                (
                    "call_3",
                    "job_b",
                    "scale_b",
                    "unit_b1",
                    "default_segment",
                    0,
                    1000,
                    "real",
                    "qwen",
                    "failed",
                    2,
                    "2026-06-22T00:02:00+00:00",
                    "2026-06-22T00:02:08+00:00",
                    8000,
                    "2026-06-22T00:02:00+00:00",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO observation_records VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "obs_detector_only",
                    "",
                    "",
                    "[]",
                    '{"detector_gate":{"triggered_vlm":false}}',
                ),
                (
                    "obs_vlm_triggered",
                    "vlm static",
                    "vlm dynamic",
                    '["vlm_tag"]',
                    '{"detector_gate":{"triggered_vlm":true}}',
                ),
            ],
        )


def test_collect_capacity_metrics_counts_requests_by_scale_camera_video_and_job(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "capacity.sqlite"
    _create_representative_db(db_path)

    metrics = collect_capacity_metrics(db_path)

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


def test_estimate_capacity_uses_request_level_formulas() -> None:
    metrics = collect_capacity_metrics.from_values(
        vlm_request_count=1260,
        video_hours=2.0,
        wall_time_seconds=420.0,
    )

    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=1000, target_window_hours=1.0),
        BenchmarkAssumptions(
            measured_req_s=None,
            effective_req_s=None,
            safety_factor=0.7,
            gpus_per_group=8,
            gpu_type="H100 80GB",
        ),
    )

    assert estimate.requests_per_camera_hour == pytest.approx(630.0)
    assert estimate.production_requests_per_hour == pytest.approx(630_000.0)
    assert estimate.required_realtime_req_s == pytest.approx(175.0)
    assert estimate.required_req_s_with_headroom == pytest.approx(250.0)
    assert estimate.measured_req_s == pytest.approx(3.0)
    assert estimate.effective_req_s == pytest.approx(2.1)
    assert estimate.processing_time_hours == pytest.approx(83.333333, rel=1e-6)
    assert estimate.needed_gpu_groups == pytest.approx(119.047619, rel=1e-6)
    assert estimate.needed_gpu_count == pytest.approx(952.380952, rel=1e-6)


def test_missing_duration_and_timing_require_overrides_without_crashing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "minimal.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE model_call_logs (
                model_call_id TEXT PRIMARY KEY,
                analysis_job_id TEXT NOT NULL,
                analysis_scale TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO model_call_logs VALUES
                ('call_1', 'job_a', 'default_segment', 'succeeded', 1, '2026-06-22T00:00:00+00:00'),
                ('call_2', 'job_a', 'default_segment', 'succeeded', 1, '2026-06-22T00:00:01+00:00');
            """
        )

    metrics = collect_capacity_metrics(
        db_path, video_hours_override=0.5, wall_time_seconds_override=10
    )
    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=10, target_window_hours=1.0),
        BenchmarkAssumptions(measured_req_s=None, effective_req_s=None, safety_factor=0.5),
    )
    report = render_capacity_report(metrics, estimate)

    assert metrics.video_hours == pytest.approx(0.5)
    assert metrics.wall_time_seconds == pytest.approx(10.0)
    assert estimate.measured_req_s == pytest.approx(0.2)
    assert "video_hours supplied by --video-hours override" in report
    assert "wall_time_seconds supplied by --wall-time-seconds override" in report
    assert "requests_per_camera_hour" in report


def test_report_contains_key_numbers_and_formula_names(tmp_path: Path) -> None:
    db_path = tmp_path / "capacity.sqlite"
    _create_representative_db(db_path)
    metrics = collect_capacity_metrics(db_path, wall_time_seconds_override=30)
    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=1000, target_window_hours=1.0),
        BenchmarkAssumptions(
            measured_req_s=None,
            effective_req_s=None,
            safety_factor=0.7,
            gpu_type="H100 80GB",
            gpus_per_group=4,
            max_stable_concurrency=16,
            p95_latency_s=2.5,
            notes="local vLLM benchmark row",
        ),
    )

    report = render_capacity_report(metrics, estimate)

    assert "# CCTV Memory Capacity Estimate" in report
    assert "vlm_request_count" in report
    assert "3" in report
    assert "requests_per_camera_hour" in report
    assert "production_requests_per_hour" in report
    assert "needed_gpu_groups" in report
    assert "H100 80GB" in report
    assert "max_stable_concurrency" in report
    assert "Measured benchmark assumptions" in report


def test_report_hides_camera_video_job_breakdowns_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "capacity.sqlite"
    _create_representative_db(db_path)
    metrics = collect_capacity_metrics(db_path, wall_time_seconds_override=30)
    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=1000, target_window_hours=1.0),
        BenchmarkAssumptions(measured_req_s=2.0, safety_factor=0.7),
    )

    report = render_capacity_report(metrics, estimate)

    assert "### Requests By Analysis Scale" in report
    assert "### Requests By Camera" not in report
    assert "### Requests By Video" not in report
    assert "### Requests By Job" not in report
    assert "Run with --include-breakdowns" in report


def test_report_can_include_camera_video_job_breakdowns(tmp_path: Path) -> None:
    db_path = tmp_path / "capacity.sqlite"
    _create_representative_db(db_path)
    metrics = collect_capacity_metrics(db_path, wall_time_seconds_override=30)
    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=1000, target_window_hours=1.0),
        BenchmarkAssumptions(measured_req_s=2.0, safety_factor=0.7),
    )

    report = render_capacity_report(metrics, estimate, include_breakdowns=True)

    assert "### Requests By Camera" in report
    assert "| cam_a | 2 |" in report
    assert "### Requests By Video" in report
    assert "| video_a | 2 |" in report
    assert "### Requests By Job" in report
    assert "| job_a | 2 |" in report


def test_estimate_reports_vram_equivalent_when_gpu_shape_is_supplied() -> None:
    metrics = collect_capacity_metrics.from_values(
        vlm_request_count=1260,
        video_hours=2.0,
        wall_time_seconds=420.0,
    )

    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=1000, target_window_hours=1.0),
        BenchmarkAssumptions(
            safety_factor=0.7,
            gpus_per_group=8,
            vram_gb_each=80,
        ),
    )
    report = render_capacity_report(metrics, estimate)

    assert estimate.benchmark_group_vram_gb == pytest.approx(640)
    assert estimate.needed_vram_gb == pytest.approx(76_190.47619, rel=1e-6)
    assert "benchmark_group_vram_gb" in report
    assert "needed_vram_gb" in report
    assert "VRAM is reported as an equivalent benchmark-row scale" in report


def test_report_contains_detector_gate_capacity_metrics(tmp_path: Path) -> None:
    db_path = tmp_path / "capacity.sqlite"
    _create_representative_db(db_path)

    metrics = collect_capacity_metrics(db_path, wall_time_seconds_override=30)
    estimate = estimate_capacity(
        metrics,
        CapacityInputs(camera_count=1000, target_window_hours=1.0),
        BenchmarkAssumptions(measured_req_s=2.0, safety_factor=0.7),
    )
    report = render_capacity_report(metrics, estimate)

    assert "detector_only_record_count: 1" in report
    assert "vlm_triggered_detector_record_count: 1" in report
    assert "gate_positive_rate: 0.5" in report
