#!/usr/bin/env python3
"""Generate a request-level CCTV Memory capacity planning report.

Backend selection:
- If ``CCTV_MEMORY_POSTGRES_DSN`` is set in the environment, read from that
  PostgreSQL instance.
- Otherwise read from SQLite.
- Passing ``--db`` explicitly forces SQLite (overrides the DSN env var) against
  that file.

Examples:
    # PostgreSQL (CCTV_MEMORY_POSTGRES_DSN exported):
    python scripts/capacity_report.py --camera-count 1000 --gpus-per-group 8

    # Force SQLite against a specific file:
    python scripts/capacity_report.py --db ./data/cctv_memory.sqlite3 \
        --camera-count 1000 --wall-time-seconds 420 --gpus-per-group 8
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cctv_memory.ops.capacity_estimation import (
    BenchmarkAssumptions,
    CapacityInputs,
    collect_capacity_metrics,
    estimate_capacity,
    render_capacity_report,
)

_DEFAULT_SQLITE_PATH = "./data/cctv_memory.sqlite3"
_POSTGRES_DSN_ENV = "CCTV_MEMORY_POSTGRES_DSN"


def _resolve_target(db_arg: Path | None) -> str:
    """Pick the metric-source target per the documented backend rules.

    Explicit ``--db`` forces SQLite; otherwise a PostgreSQL DSN env var wins;
    otherwise fall back to the default SQLite path.
    """
    if db_arg is not None:
        return str(db_arg)
    dsn = os.environ.get(_POSTGRES_DSN_ENV)
    if dsn:
        return dsn
    return _DEFAULT_SQLITE_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        help=(
            "Path to a cctv-memory SQLite DB. Passing this forces SQLite even if "
            f"{_POSTGRES_DSN_ENV} is set. If omitted, {_POSTGRES_DSN_ENV} (PostgreSQL) "
            f"is used when present, else {_DEFAULT_SQLITE_PATH}."
        ),
    )
    parser.add_argument("--camera-count", required=True, type=int, help="Production camera count")
    parser.add_argument("--video-hours", type=float, help="Override DB-derived test video hours")
    parser.add_argument(
        "--wall-time-seconds",
        type=float,
        help="Override DB-derived/benchmark end-to-end wall time seconds",
    )
    parser.add_argument(
        "--effective-req-s", type=float, help="Measured effective req/s per GPU group"
    )
    parser.add_argument(
        "--measured-req-s", type=float, help="Raw measured req/s per GPU group"
    )
    parser.add_argument(
        "--safety-factor", type=float, default=0.7, help="Safety factor for measured req/s"
    )
    parser.add_argument(
        "--headroom-factor",
        type=float,
        default=0.7,
        help="Headroom factor for required production req/s",
    )
    parser.add_argument(
        "--gpus-per-group", type=int, help="GPU count in the measured service group"
    )
    parser.add_argument("--gpu-type", help="GPU type, e.g. H100 80GB")
    parser.add_argument("--vram-gb-each", type=float, help="VRAM per GPU in GB")
    parser.add_argument(
        "--max-stable-concurrency", type=int, help="Measured max stable VLM concurrency"
    )
    parser.add_argument(
        "--p95-latency-s", type=float, help="Measured p95 latency for benchmark row"
    )
    parser.add_argument("--benchmark-notes", help="Free-form benchmark notes")
    parser.add_argument("--target-window-hours", type=float, default=1.0)
    parser.add_argument(
        "--output", type=Path, help="Write Markdown report to this path"
    )
    parser.add_argument(
        "--include-breakdowns",
        action="store_true",
        help="Include camera/video/job request breakdown tables",
    )
    args = parser.parse_args()

    metrics = collect_capacity_metrics(
        _resolve_target(args.db),
        video_hours_override=args.video_hours,
        wall_time_seconds_override=args.wall_time_seconds,
    )
    estimate = estimate_capacity(
        metrics,
        CapacityInputs(
            camera_count=args.camera_count,
            target_window_hours=args.target_window_hours,
            headroom_factor=args.headroom_factor,
        ),
        BenchmarkAssumptions(
            measured_req_s=args.measured_req_s,
            effective_req_s=args.effective_req_s,
            safety_factor=args.safety_factor,
            gpu_type=args.gpu_type,
            gpus_per_group=args.gpus_per_group,
            vram_gb_each=args.vram_gb_each,
            max_stable_concurrency=args.max_stable_concurrency,
            p95_latency_s=args.p95_latency_s,
            notes=args.benchmark_notes,
        ),
    )
    report = render_capacity_report(
        metrics, estimate, include_breakdowns=args.include_breakdowns
    )
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
