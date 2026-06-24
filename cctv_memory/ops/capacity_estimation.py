"""Request-level CCTV Memory capacity estimation from observability logs.

Backend-agnostic: reads metrics from either a SQLite DB file or a live
PostgreSQL instance through a small ``_MetricSource`` abstraction. The compute
and report-rendering layers are pure and never touch a database; only the
metric-source layer knows backend specifics (connection, table/column probing,
JSON column dialect). Both backends produce the identical ``CapacityMetrics``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Self

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class CapacityMetrics:
    db_path: str | None
    vlm_request_count: int
    request_count_by_scale: dict[str, int] = field(default_factory=dict)
    request_count_by_camera: dict[str, int] = field(default_factory=dict)
    request_count_by_video: dict[str, int] = field(default_factory=dict)
    request_count_by_job: dict[str, int] = field(default_factory=dict)
    success_count: int = 0
    failed_count: int = 0
    retry_count_estimate: int = 0
    video_hours: float | None = None
    wall_time_seconds: float | None = None
    job_wall_time_seconds: float | None = None
    p50_latency_s: float | None = None
    p95_latency_s: float | None = None
    detector_only_record_count: int = 0
    vlm_triggered_detector_record_count: int = 0
    gate_positive_rate: float | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_values(
        cls,
        *,
        vlm_request_count: int,
        video_hours: float | None = None,
        wall_time_seconds: float | None = None,
    ) -> Self:
        return cls(
            db_path=None,
            vlm_request_count=vlm_request_count,
            video_hours=video_hours,
            wall_time_seconds=wall_time_seconds,
            notes=["metrics supplied directly for formula evaluation"],
        )


@dataclass(frozen=True)
class CapacityInputs:
    camera_count: int
    target_window_hours: float = 1.0
    headroom_factor: float = 0.7


@dataclass(frozen=True)
class BenchmarkAssumptions:
    measured_req_s: float | None = None
    effective_req_s: float | None = None
    safety_factor: float = 0.7
    gpu_type: str | None = None
    gpus_per_group: int | None = None
    vram_gb_each: float | None = None
    max_stable_concurrency: int | None = None
    p95_latency_s: float | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CapacityEstimate:
    inputs: CapacityInputs
    benchmark: BenchmarkAssumptions
    requests_per_camera_hour: float | None
    production_requests_per_hour: float | None
    production_requests: float | None
    required_realtime_req_s: float | None
    required_req_s_with_headroom: float | None
    measured_req_s: float | None
    effective_req_s: float | None
    processing_time_hours: float | None
    needed_gpu_groups: float | None
    needed_gpu_count: float | None
    benchmark_group_vram_gb: float | None
    needed_vram_gb: float | None
    notes: list[str] = field(default_factory=list)


class _MetricSource(Protocol):
    """Minimal read-only data-access surface the metric collector needs.

    Backend specifics (connection, table/column probing, JSON column dialect)
    live behind this protocol; the collection logic above is written once
    against it so SQLite and PostgreSQL share the same standard SQL.
    """

    label: str

    def has_table(self, table: str) -> bool: ...

    def columns(self, table: str) -> set[str]: ...

    def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]: ...

    def json_text(self, column: str) -> str:
        """Return a SQL expression yielding ``column`` as JSON text.

        SQLite already stores JSON as TEXT; PostgreSQL JSONB needs an explicit
        cast so the Python side always receives a ``str`` to ``json.loads``.
        """
        ...


class _SqliteMetricSource:
    """SQLite metric source (offline, read-only file access)."""

    def __init__(self, db_path: Path) -> None:
        self.label = str(db_path)
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def has_table(self, table: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def columns(self, table: str) -> set[str]:
        if not self.has_table(table):
            return set()
        return {str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
        cursor = self._conn.execute(sql, dict(params) if params else {})
        return [tuple(row) for row in cursor.fetchall()]

    def json_text(self, column: str) -> str:
        return column


class _PostgresMetricSource:
    """PostgreSQL metric source (offline, read-only via SQLAlchemy)."""

    def __init__(self, dsn: str) -> None:
        # Never expose credentials: the reportable label is host/db only.
        url = make_url(dsn)
        self.label = f"postgres://{url.host or 'localhost'}/{url.database or ''}"
        self._engine = create_engine(dsn, future=True)
        self._conn = self._engine.connect()

    def close(self) -> None:
        self._conn.close()
        self._engine.dispose()

    def has_table(self, table: str) -> bool:
        return bool(
            self._conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            ).scalar()
        )

    def columns(self, table: str) -> set[str]:
        rows = self._conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return {str(r[0]) for r in rows}

    def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
        result = self._conn.execute(text(sql), dict(params) if params else {})
        return [tuple(row) for row in result.fetchall()]

    def json_text(self, column: str) -> str:
        # JSONB -> text so the Python side always gets a str for json.loads.
        return f"({column})::text"


def _looks_like_dsn(value: str) -> bool:
    """True if ``value`` is a SQLAlchemy URL for a non-SQLite backend."""
    try:
        url = make_url(value)
    except Exception:  # noqa: BLE001 - not a URL -> treat as a filesystem path
        return False
    backend = url.get_backend_name()
    return bool(url.drivername) and backend not in ("", "sqlite")


@contextmanager
def _open_source(target: str | Path):  # type: ignore[no-untyped-def]
    """Open the right metric source for ``target`` (DSN -> PG, else SQLite)."""
    if isinstance(target, str) and _looks_like_dsn(target):
        source: _SqliteMetricSource | _PostgresMetricSource = _PostgresMetricSource(target)
    else:
        source = _SqliteMetricSource(Path(target))
    try:
        yield source
    finally:
        source.close()


class _MetricCollector:
    def __call__(
        self,
        target: str | Path,
        *,
        video_hours_override: float | None = None,
        wall_time_seconds_override: float | None = None,
    ) -> CapacityMetrics:
        return _collect_capacity_metrics(
            target,
            video_hours_override=video_hours_override,
            wall_time_seconds_override=wall_time_seconds_override,
        )

    def from_values(
        self,
        *,
        vlm_request_count: int,
        video_hours: float | None = None,
        wall_time_seconds: float | None = None,
    ) -> CapacityMetrics:
        return CapacityMetrics.from_values(
            vlm_request_count=vlm_request_count,
            video_hours=video_hours,
            wall_time_seconds=wall_time_seconds,
        )


collect_capacity_metrics = _MetricCollector()


def estimate_capacity(
    metrics: CapacityMetrics,
    inputs: CapacityInputs,
    benchmark: BenchmarkAssumptions,
) -> CapacityEstimate:
    notes: list[str] = []
    requests_per_camera_hour = None
    if metrics.video_hours and metrics.video_hours > 0:
        requests_per_camera_hour = metrics.vlm_request_count / metrics.video_hours
    else:
        notes.append("video_hours unavailable; cannot compute requests_per_camera_hour")

    production_requests_per_hour = None
    production_requests = None
    required_realtime_req_s = None
    required_req_s_with_headroom = None
    if requests_per_camera_hour is not None:
        production_requests_per_hour = requests_per_camera_hour * inputs.camera_count
        production_requests = production_requests_per_hour * inputs.target_window_hours
        required_realtime_req_s = production_requests_per_hour / 3600
        if inputs.headroom_factor > 0:
            required_req_s_with_headroom = required_realtime_req_s / inputs.headroom_factor

    measured_req_s = benchmark.measured_req_s
    if measured_req_s is None and metrics.wall_time_seconds and metrics.wall_time_seconds > 0:
        measured_req_s = metrics.vlm_request_count / metrics.wall_time_seconds
    elif measured_req_s is None:
        notes.append("measured_req_s unavailable; provide --effective-req-s or wall-time input")

    effective_req_s = benchmark.effective_req_s
    if effective_req_s is None and measured_req_s is not None:
        effective_req_s = measured_req_s * benchmark.safety_factor

    processing_time_hours = None
    if production_requests is not None and effective_req_s and effective_req_s > 0:
        processing_time_hours = production_requests / effective_req_s / 3600
    elif production_requests is not None:
        notes.append("effective_req_s unavailable; cannot compute processing_time_hours")

    needed_gpu_groups = None
    needed_gpu_count = None
    benchmark_group_vram_gb = None
    needed_vram_gb = None
    if benchmark.gpus_per_group is not None and benchmark.vram_gb_each is not None:
        benchmark_group_vram_gb = benchmark.gpus_per_group * benchmark.vram_gb_each
    if required_req_s_with_headroom is not None and effective_req_s and effective_req_s > 0:
        needed_gpu_groups = required_req_s_with_headroom / effective_req_s
        if benchmark.gpus_per_group is not None:
            needed_gpu_count = needed_gpu_groups * benchmark.gpus_per_group
        if benchmark_group_vram_gb is not None:
            needed_vram_gb = needed_gpu_groups * benchmark_group_vram_gb

    return CapacityEstimate(
        inputs=inputs,
        benchmark=benchmark,
        requests_per_camera_hour=requests_per_camera_hour,
        production_requests_per_hour=production_requests_per_hour,
        production_requests=production_requests,
        required_realtime_req_s=required_realtime_req_s,
        required_req_s_with_headroom=required_req_s_with_headroom,
        measured_req_s=measured_req_s,
        effective_req_s=effective_req_s,
        processing_time_hours=processing_time_hours,
        needed_gpu_groups=needed_gpu_groups,
        needed_gpu_count=needed_gpu_count,
        benchmark_group_vram_gb=benchmark_group_vram_gb,
        needed_vram_gb=needed_vram_gb,
        notes=notes,
    )


def render_capacity_report(
    metrics: CapacityMetrics,
    estimate: CapacityEstimate,
    *,
    include_breakdowns: bool = False,
) -> str:
    lines = [
        "# CCTV Memory Capacity Estimate",
        "",
        "## Input Assumptions",
        f"- camera_count: {estimate.inputs.camera_count}",
        f"- target_window_hours: {_fmt(estimate.inputs.target_window_hours)}",
        f"- headroom_factor: {_fmt(estimate.inputs.headroom_factor)}",
        f"- safety_factor: {_fmt(estimate.benchmark.safety_factor)}",
    ]
    if metrics.db_path:
        lines.append(f"- db: `{metrics.db_path}`")
    lines.extend(_benchmark_lines(estimate.benchmark))

    lines.extend(
        [
            "",
            "## DB-Derived Counts",
            f"- vlm_request_count: {metrics.vlm_request_count}",
            f"- success_count: {metrics.success_count}",
            f"- failed_count: {metrics.failed_count}",
            f"- retry_count_estimate: {metrics.retry_count_estimate}",
            f"- video_hours: {_fmt_optional(metrics.video_hours)}",
            f"- wall_time_seconds: {_fmt_optional(metrics.wall_time_seconds)}",
            f"- job_wall_time_seconds: {_fmt_optional(metrics.job_wall_time_seconds)}",
            f"- p50_latency_s: {_fmt_optional(metrics.p50_latency_s)}",
            f"- p95_latency_s: {_fmt_optional(metrics.p95_latency_s)}",
            f"- detector_only_record_count: {metrics.detector_only_record_count}",
            "- vlm_triggered_detector_record_count: "
            f"{metrics.vlm_triggered_detector_record_count}",
            f"- gate_positive_rate: {_fmt_optional(metrics.gate_positive_rate)}",
            "",
            "### Requests By Analysis Scale",
            _dict_table(metrics.request_count_by_scale, "analysis_scale"),
            "",
            "## Formulas Used",
            "- requests_per_camera_hour = vlm_request_count / video_hours",
            "- production_requests_per_hour = requests_per_camera_hour * camera_count",
            "- required_realtime_req_s = production_requests_per_hour / 3600",
            "- effective_req_s = measured_req_s * safety_factor, "
            "unless --effective-req-s is supplied",
            "- processing_time_hours = production_requests / effective_req_s / 3600",
            "- needed_gpu_groups = required_req_s_with_headroom / effective_req_s_per_group",
            "- needed_gpu_count = needed_gpu_groups * gpus_per_group",
            "- benchmark_group_vram_gb = gpus_per_group * vram_gb_each",
            "- needed_vram_gb = needed_gpu_groups * benchmark_group_vram_gb",
            "",
            "## GPU Group Interpretation",
            "- One GPU group means the exact GPU/vLLM service shape that produced "
            "the measured throughput row.",
            "- If you tested one 8-GPU vLLM deployment, then effective_req_s is per "
            "8-GPU group and gpus_per_group should be 8.",
            "- needed_gpu_groups is how many copies of that measured group are needed "
            "to reach required_req_s_with_headroom.",
            "- VRAM is reported as an equivalent benchmark-row scale, not as a "
            "more precise substitute for throughput benchmarking.",
            "",
            "## Production Estimates",
            f"- requests_per_camera_hour: {_fmt_optional(estimate.requests_per_camera_hour)}",
            "- production_requests_per_hour: "
            f"{_fmt_optional(estimate.production_requests_per_hour)}",
            f"- production_requests: {_fmt_optional(estimate.production_requests)}",
            f"- required_realtime_req_s: {_fmt_optional(estimate.required_realtime_req_s)}",
            "- required_req_s_with_headroom: "
            f"{_fmt_optional(estimate.required_req_s_with_headroom)}",
            f"- measured_req_s: {_fmt_optional(estimate.measured_req_s)}",
            f"- effective_req_s: {_fmt_optional(estimate.effective_req_s)}",
            f"- processing_time_hours: {_fmt_optional(estimate.processing_time_hours)}",
            f"- needed_gpu_groups: {_fmt_optional(estimate.needed_gpu_groups)}",
            f"- needed_gpu_count: {_fmt_optional(estimate.needed_gpu_count)}",
            f"- benchmark_group_vram_gb: {_fmt_optional(estimate.benchmark_group_vram_gb)}",
            f"- needed_vram_gb: {_fmt_optional(estimate.needed_vram_gb)}",
            "",
            "## Limitations",
            "- GPU VRAM and max concurrency are treated as measured benchmark row fields, "
            "not derived from parameter count.",
            "- Request shape, prompt length, image count, output tokens, vLLM batching, "
            "and retry behavior can change throughput.",
            "- DB-derived request counts are only representative for the exact "
            "cctv-memory segmentation/high-frequency configuration used in the run.",
            "- For production planning, remeasure when model, quantization, GPU type/count, "
            "max model length, vLLM concurrency, or analysis config changes.",
            "",
            "## Notes",
        ]
    )
    if include_breakdowns:
        lines[lines.index("## Formulas Used"):lines.index("## Formulas Used")] = [
            "### Requests By Camera",
            _dict_table(metrics.request_count_by_camera, "camera_id"),
            "",
            "### Requests By Video",
            _dict_table(metrics.request_count_by_video, "video_id"),
            "",
            "### Requests By Job",
            _dict_table(metrics.request_count_by_job, "analysis_job_id"),
            "",
        ]
    else:
        lines[lines.index("## Formulas Used"):lines.index("## Formulas Used")] = [
            "_Camera/video/job breakdowns hidden by default. Run with --include-breakdowns "
            "to include detailed tables._",
            "",
        ]
    notes = [*metrics.notes, *estimate.notes]
    if estimate.benchmark.notes:
        notes.append(f"benchmark notes: {estimate.benchmark.notes}")
    lines.extend([f"- {note}" for note in notes] or ["- none"])
    lines.extend(
        [
            "",
            "## Recommended Next Measurements",
            "- Run a representative video set for each camera class and record video_hours, "
            "VLM request count, wall_time_seconds, retry rate, p50/p95 latency.",
            "- Benchmark each GPU/vLLM group as a row: gpu_type, gpu_count, "
            "vram_gb_each, max_stable_concurrency, measured_req_s, p95_latency_s, notes.",
            "- Re-run this report for each segmentation/high-frequency configuration "
            "because request count is configuration-dependent.",
        ]
    )
    return "\n".join(lines) + "\n"


def _collect_capacity_metrics(
    target: str | Path,
    *,
    video_hours_override: float | None,
    wall_time_seconds_override: float | None,
) -> CapacityMetrics:
    notes: list[str] = []
    with _open_source(target) as src:
        if not src.has_table("model_call_logs"):
            raise ValueError("model_call_logs table is required for request-level capacity reports")

        mcols = src.columns("model_call_logs")
        request_count = _scalar_int(src, "SELECT COUNT(*) FROM model_call_logs")
        by_scale = _group_counts(src, "model_call_logs", "analysis_scale", mcols)
        by_job = _group_counts(src, "model_call_logs", "analysis_job_id", mcols)
        by_video = _request_counts_by_video(src, mcols, notes)
        by_camera = _request_counts_by_camera(src, mcols, notes)
        detector_counts = _detector_gate_record_counts(src, notes)

        status_counts = _status_counts(src, mcols)
        retry_count = _retry_count(src, mcols)
        p50, p95 = _latency_percentiles(src, mcols, notes)
        video_hours = _video_hours(src, video_hours_override, notes)
        job_wall = _job_wall_time(src, notes)
        wall_time = wall_time_seconds_override
        if wall_time is not None:
            notes.append("wall_time_seconds supplied by --wall-time-seconds override")
        elif job_wall is not None:
            wall_time = job_wall
            notes.append(
                "wall_time_seconds inferred from min job.started_at to max job.finished_at"
            )
        else:
            notes.append(
                "wall_time_seconds unavailable; provide --wall-time-seconds or --measured-req-s"
            )
        db_label = src.label

    return CapacityMetrics(
        db_path=db_label,
        vlm_request_count=request_count,
        request_count_by_scale=by_scale,
        request_count_by_camera=by_camera,
        request_count_by_video=by_video,
        request_count_by_job=by_job,
        success_count=status_counts.get("succeeded", 0),
        failed_count=status_counts.get("failed", 0),
        retry_count_estimate=retry_count,
        video_hours=video_hours,
        wall_time_seconds=wall_time,
        job_wall_time_seconds=job_wall,
        p50_latency_s=p50,
        p95_latency_s=p95,
        detector_only_record_count=detector_counts[0],
        vlm_triggered_detector_record_count=detector_counts[1],
        gate_positive_rate=detector_counts[2],
        notes=notes,
    )


def _detector_gate_record_counts(
    src: _MetricSource, notes: list[str]
) -> tuple[int, int, float | None]:
    if not src.has_table("observation_records"):
        return 0, 0, None
    cols = src.columns("observation_records")
    if "attributes_json" not in cols:
        return 0, 0, None
    detector_only = 0
    triggered = 0
    total = 0
    attrs_text = src.json_text("attributes_json")
    tags_text = src.json_text("tags_json")
    rows = src.rows(
        "SELECT static_description_text, dynamic_description_text, "
        f"{tags_text} AS tags_text, {attrs_text} AS attrs_text "
        "FROM observation_records WHERE "
        f"{attrs_text} LIKE '%detector_gate%'"
    )
    for static_text, dynamic_text, tags_value, attrs_value in rows:
        try:
            attrs = json.loads(attrs_value or "{}")
        except json.JSONDecodeError:
            continue
        gate = attrs.get("detector_gate")
        if not isinstance(gate, dict):
            continue
        total += 1
        if bool(gate.get("triggered_vlm")):
            triggered += 1
        tags = (tags_value or "[]").replace(" ", "")
        if not static_text and not dynamic_text and tags == "[]":
            detector_only += 1
    if total == 0:
        return 0, 0, None
    notes.append("detector gate metrics derived from ObservationRecord attr.detector_gate")
    return detector_only, triggered, triggered / total


def _scalar_int(src: _MetricSource, sql: str) -> int:
    rows = src.rows(sql)
    value = rows[0][0] if rows else 0
    return int(value or 0)


def _group_counts(
    src: _MetricSource, table: str, column: str, columns: set[str]
) -> dict[str, int]:
    if column not in columns:
        return {}
    rows = src.rows(
        f"SELECT {column} AS key, COUNT(*) AS count "
        f"FROM {table} GROUP BY {column} ORDER BY {column}"
    )
    return {str(key): int(count) for key, count in rows if key is not None}


def _request_counts_by_video(
    src: _MetricSource, mcols: set[str], notes: list[str]
) -> dict[str, int]:
    if "video_id" in mcols:
        return _group_counts(src, "model_call_logs", "video_id", mcols)
    if not src.has_table("analysis_jobs") or "analysis_job_id" not in mcols:
        notes.append(
            "request_count_by_video unavailable; missing video_id or analysis_jobs join path"
        )
        return {}
    rows = src.rows(
        """
        SELECT j.video_id AS key, COUNT(*) AS count
        FROM model_call_logs m
        JOIN analysis_jobs j ON j.analysis_job_id = m.analysis_job_id
        GROUP BY j.video_id
        ORDER BY j.video_id
        """
    )
    return {str(key): int(count) for key, count in rows if key is not None}


def _request_counts_by_camera(
    src: _MetricSource, mcols: set[str], notes: list[str]
) -> dict[str, int]:
    if "camera_id" in mcols:
        return _group_counts(src, "model_call_logs", "camera_id", mcols)
    if not (src.has_table("analysis_jobs") and src.has_table("video_sources")):
        notes.append(
            "request_count_by_camera unavailable; missing analysis_jobs/video_sources join path"
        )
        return {}
    rows = src.rows(
        """
        SELECT v.camera_id AS key, COUNT(*) AS count
        FROM model_call_logs m
        JOIN analysis_jobs j ON j.analysis_job_id = m.analysis_job_id
        JOIN video_sources v ON v.video_id = j.video_id
        GROUP BY v.camera_id
        ORDER BY v.camera_id
        """
    )
    return {str(key): int(count) for key, count in rows if key is not None}


def _status_counts(src: _MetricSource, mcols: set[str]) -> dict[str, int]:
    return _group_counts(src, "model_call_logs", "status", mcols)


def _retry_count(src: _MetricSource, mcols: set[str]) -> int:
    if "attempt_count" not in mcols:
        return 0
    if "unit_id" in mcols:
        rows = src.rows(
            "SELECT SUM(CASE WHEN max_attempt > 1 THEN max_attempt - 1 ELSE 0 END) "
            "FROM (SELECT unit_id, MAX(attempt_count) AS max_attempt "
            "FROM model_call_logs GROUP BY unit_id) AS per_unit"
        )
        return int((rows[0][0] if rows else 0) or 0)
    rows = src.rows(
        "SELECT SUM(CASE WHEN attempt_count > 1 THEN attempt_count - 1 ELSE 0 END) "
        "FROM model_call_logs"
    )
    return int((rows[0][0] if rows else 0) or 0)


def _latency_percentiles(
    src: _MetricSource, mcols: set[str], notes: list[str]
) -> tuple[float | None, float | None]:
    if "duration_ms" not in mcols:
        notes.append("per-request latency unavailable; model_call_logs.duration_ms missing")
        return None, None
    durations = [
        float(row[0]) / 1000
        for row in src.rows(
            "SELECT duration_ms FROM model_call_logs "
            "WHERE duration_ms IS NOT NULL ORDER BY duration_ms"
        )
    ]
    if not durations:
        notes.append("per-request latency unavailable; no non-null duration_ms values")
        return None, None
    return _percentile(durations, 0.50), _percentile(durations, 0.95)


def _percentile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = math.ceil(q * len(sorted_values)) - 1
    idx = min(max(idx, 0), len(sorted_values) - 1)
    return sorted_values[idx]


def _video_hours(
    src: _MetricSource, override: float | None, notes: list[str]
) -> float | None:
    if override is not None:
        notes.append("video_hours supplied by --video-hours override")
        return override
    if not src.has_table("video_sources"):
        notes.append("video_hours unavailable; video_sources table missing")
        return None
    cols = src.columns("video_sources")
    if "duration_ms" in cols:
        rows = src.rows(
            "SELECT SUM(duration_ms) FROM video_sources WHERE duration_ms IS NOT NULL"
        )
        total_ms = rows[0][0] if rows else None
        if total_ms:
            return float(total_ms) / 3_600_000
        notes.append("video_hours unavailable; video_sources.duration_ms values are null")
    else:
        notes.append("video_hours unavailable; video_sources.duration_ms column missing")
    return None


def _job_wall_time(src: _MetricSource, notes: list[str]) -> float | None:
    if not src.has_table("analysis_jobs"):
        notes.append("job wall time unavailable; analysis_jobs table missing")
        return None
    cols = src.columns("analysis_jobs")
    if not {"started_at", "finished_at"}.issubset(cols):
        notes.append("job wall time unavailable; analysis_jobs started_at/finished_at missing")
        return None
    rows = src.rows(
        "SELECT MIN(started_at), MAX(finished_at) FROM analysis_jobs "
        "WHERE started_at IS NOT NULL AND finished_at IS NOT NULL"
    )
    row = rows[0] if rows else None
    if not row or row[0] is None or row[1] is None:
        notes.append("job wall time unavailable; job timestamps are incomplete")
        return None
    # SQLite returns ISO text; PostgreSQL TIMESTAMPTZ returns datetime. Accept both.
    start = _as_dt(row[0])
    finish = _as_dt(row[1])
    return max((finish - start).total_seconds(), 0.0)


def _as_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _benchmark_lines(benchmark: BenchmarkAssumptions) -> list[str]:
    rows = ["", "### Measured benchmark assumptions"]
    values = {
        "gpu_type": benchmark.gpu_type,
        "gpus_per_group": benchmark.gpus_per_group,
        "vram_gb_each": benchmark.vram_gb_each,
        "max_stable_concurrency": benchmark.max_stable_concurrency,
        "measured_req_s": benchmark.measured_req_s,
        "effective_req_s": benchmark.effective_req_s,
        "p95_latency_s": benchmark.p95_latency_s,
        "notes": benchmark.notes,
    }
    for key, value in values.items():
        if value is not None:
            rows.append(f"- {key}: {value}")
    if len(rows) == 2:
        rows.append("- none supplied")
    return rows


def _dict_table(values: dict[str, int], key_name: str) -> str:
    if not values:
        return "_Not available._"
    lines = [f"| {key_name} | requests |", "|---|---:|"]
    lines.extend(f"| {key} | {count} |" for key, count in values.items())
    return "\n".join(lines)


def _fmt_optional(value: float | None) -> str:
    return "n/a" if value is None else _fmt(value)


def _fmt(value: float) -> str:
    if math.isfinite(value) and abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.6g}"
