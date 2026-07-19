"""CLI entrypoints (MVP closed-loop).

Commands:
- ``version`` / ``health``    : smoke (health is DB-aware when --data-dir given).
- ``init --data-dir``         : create dirs + schema + seed local defaults.
- ``analyze --data-dir ...``  : submit a video source, optionally run worker.
- ``worker --data-dir --once``: process one queued task.
- ``search --data-dir --query``: authorized search (uses local dev principal).

The CLI uses a local dev principal (``--principal-id``, default ``user_admin``)
with clearly local-only semantics. Identity is never taken from arbitrary input
as a permission grant; the dev principal is resolved server-side and its
AuthorizedScope is computed from policy.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from cctv_memory import __version__
from cctv_memory.application import get_health_report
from cctv_memory.application.seed import DEV_PRINCIPAL_ID, seed_local_defaults
from cctv_memory.contracts.search import StartObservationSearchRequest
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.domain.enums import SourceType


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str))


def _parse_iso_datetime_arg(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(2) from exc


def _cmd_init(args: argparse.Namespace) -> int:
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    runtime.init_storage()
    runtime.create_schema()
    with runtime.request_services():
        pass
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())
    runtime.dispose()
    _print({"status": "initialized", "data_dir": args.data_dir})
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from cctv_memory.infrastructure.runtime import build_runtime
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    runtime = build_runtime(data_dir=args.data_dir)
    analysis_options: dict[str, bool] = {"enable_default_segment": True}
    if getattr(args, "enable_high_freq", False):
        analysis_options["enable_motion_triggered_high_freq"] = True
    request = SubmitVideoSourceRequest(
        source_type=SourceType(args.source_type),
        source_uri=args.source_uri,
        camera_id=args.camera_id,
        video_start_time=datetime.fromisoformat(args.video_start_time),
        external_source_id=args.external_source_id,
        idempotency_key=args.idempotency_key or args.external_source_id,
        analysis_options=analysis_options,
    )
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
        resp = svc.ingestion.submit(request, principal, capabilities=scope.capabilities)
    timeline = runtime.timeline_recorder()
    timeline.event(
        "request_accepted",
        analysis_job_id=resp.analysis_job_id,
        video_id=resp.video_id,
        status="accepted",
        metadata={
            "source_type": request.source_type.value,
            "principal_id": principal.principal_id,
        },
    )
    timeline.event(
        "task_queued",
        analysis_job_id=resp.analysis_job_id,
        video_id=resp.video_id,
        status="queued",
        metadata={"wait_requested": bool(args.wait)},
    )
    result = resp.model_dump(mode="json")

    if args.wait:
        worker = AnalysisWorker(runtime)
        processed = worker.drain()
        result["worker_processed_tasks"] = processed
        with runtime.request_services() as svc:
            job = svc.jobs.get_job(resp.analysis_job_id)
            result["job_status"] = job.job_status.value if job else None
    runtime.dispose()
    _print(result)
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from cctv_memory.infrastructure.runtime import build_runtime
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    runtime = build_runtime(data_dir=args.data_dir)
    worker = AnalysisWorker(runtime)
    # Bounded orphan-running recovery before claiming work (task §E): finalize any
    # units left stuck running past the stale cutoff (e.g. a previous crash).
    recovered = worker.recover_orphans()
    if args.once:
        task_id = worker.process_one()
        _print({"processed_task": task_id, "recovered_orphans": recovered})
    else:
        count = worker.drain()
        _print({"processed_tasks": count, "recovered_orphans": recovered})
    runtime.dispose()
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    request = StartObservationSearchRequest(query_text=args.query, top_k=args.top_k)
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
        resp = svc.search.start_search(request, scope)
    runtime.dispose()
    _print(resp.model_dump(mode="json"))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the effective configuration before running anything.

    Builds the SAME ``AppConfig`` (with ``--data-dir`` applied) that the runtime
    would use, then reports which path (mock vs real) is in effect and what is
    missing. Never prints secret values — only env-var NAMES and whether they are
    set. Does not open the DB or contact any external endpoint.
    """
    from cctv_memory.application import build_doctor_report, render_doctor_text
    from cctv_memory.config.settings import AppConfig

    config = AppConfig().with_data_dir(args.data_dir)
    report = build_doctor_report(config)
    if args.json:
        _print(report)
    else:
        print(render_doctor_text(report))
    return 0


def _cmd_serve(
    args: argparse.Namespace,
    *,
    runner: object | None = None,
) -> int:
    """Start the FastAPI HTTP server (real startup path).

    Builds the runtime + wired FastAPI app from ``--data-dir`` and serves it with
    uvicorn on ``--host``/``--port``. The host/port default to the configured
    ``server`` section but CLI flags win. ``runner`` is injectable so tests can
    assert the app/host/port without binding a real socket (testing-contract §12:
    no long-lived server in pytest).

    When ``config.worker.embedded`` is true, a background daemon worker thread
    drains queued analysis tasks in-process so a single ``serve`` is a usable
    closed loop; the standalone ``worker`` command remains the robust path for
    production. The embedded worker is hardened: each drain runs in its own
    sessions and exceptions are swallowed per-cycle so a bad task never kills the
    server thread.
    """
    import threading

    from cctv_memory.bootstrap import build_app
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    runtime.init_storage()
    cfg = runtime.config
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    app = build_app(runtime)

    stop_event = threading.Event()
    worker_thread: threading.Thread | None = None
    if cfg.worker.enabled and cfg.worker.embedded and not args.no_worker:
        from cctv_memory.workers.analysis_worker import AnalysisWorker

        def _drain_loop() -> None:
            worker = AnalysisWorker(runtime)
            # One bounded orphan-recovery pass at startup before the drain loop so a
            # prior crash's stuck-running units are finalized (task §E).
            try:
                worker.recover_orphans()
            except Exception:  # noqa: BLE001 - never kill the server thread
                logging.getLogger(__name__).exception(
                    "embedded worker orphan recovery failed; continuing"
                )
            while not stop_event.is_set():
                try:
                    worker.drain(should_stop=stop_event.is_set)
                except Exception:  # noqa: BLE001 - never kill the server thread
                    logging.getLogger(__name__).exception(
                        "embedded worker drain cycle failed; will retry next poll"
                    )
                stop_event.wait(args.worker_poll_seconds)

        worker_thread = threading.Thread(
            target=_drain_loop, name="embedded-worker", daemon=True
        )
        worker_thread.start()

    _print(
        {
            "serve": {
                "host": host,
                "port": port,
                "vlm_provider": cfg.vlm.provider,
                "indexing_provider": cfg.indexing.provider,
                "embedded_worker": worker_thread is not None,
            }
        }
    )

    try:
        if runner is not None:
            # Injected runner (tests): record and return without binding a socket.
            runner(app, host=host, port=port)  # type: ignore[operator]
        else:  # pragma: no cover - exercised via bounded manual smoke, not pytest
            import uvicorn

            uvicorn.run(app, host=host, port=port, log_level=cfg.app.log_level.lower())  # type: ignore[arg-type]
    finally:
        stop_event.set()
        if worker_thread is not None:
            worker_thread.join(timeout=max(1.0, float(args.worker_poll_seconds)))
        runtime.dispose()
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    from cctv_memory.application.backup import BackupService
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        manifest = service.admin_backup(args.out, scope)
    runtime.dispose()
    _print({"backup": args.out, "manifest": manifest.model_dump(mode="json")})
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from cctv_memory.application.backup import BackupService
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    manifest_path = args.manifest or (args.infile + ".manifest.json")
    manifest = BackupService.load_manifest(manifest_path)
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
    with runtime.session() as session:
        audit = runtime.repositories(session).audit()
        service = BackupService(runtime.backup_adapter(), audit)
        service.restore(args.infile, manifest, scope)
    runtime.dispose()
    _print({"restored_from": args.infile, "status": "ok"})
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    from cctv_memory.application.maintenance import MaintenanceService
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    embedder = runtime.build_embedder()
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = MaintenanceService(
            repos.observation_read(),
            repos.index(),
            repos.search_context(),
            repos.audit(),
            embedder,
        )
        result = service.reindex(scope, force=args.force)
    runtime.dispose()
    _print(
        {
            "reindex": {
                "scanned": result.scanned,
                "reindexed": result.reindexed,
                "skipped": result.skipped,
                "vectors_written": result.vectors_written,
                "model_id": result.model_id,
                "dimension": result.dimension,
            }
        }
    )
    return 0


def _cmd_maintenance(args: argparse.Namespace) -> int:
    from cctv_memory.application.maintenance import MaintenanceService
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    embedder = runtime.build_embedder()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = MaintenanceService(
            repos.observation_read(),
            repos.index(),
            repos.search_context(),
            repos.audit(),
            embedder,
        )
        result = service.sweep_contexts()
    runtime.dispose()
    _print({"maintenance_sweep": {"expired_contexts": result.expired}})
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    from cctv_memory.application.admin_diagnostics import AdminDiagnosticsService
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
    with runtime.session() as session:
        repos = runtime.repositories(session)
        service = AdminDiagnosticsService(
            repos.analysis_job(),
            repos.scale_task(),
            repos.analysis_unit(),
            repos.model_call_log(),
            repos.pre_vlm_gate_log(),
        )
        result = service.failure_details(args.job_id, scope)
    runtime.dispose()
    _print({"failure_diagnostics": result.model_dump()})
    return 0


def _cmd_timeline_export(args: argparse.Namespace) -> int:
    from cctv_memory.infrastructure.runtime import build_runtime
    from cctv_memory.ops.timeline_export import export_aggregate_timeline, export_timeline

    if args.out is None and args.out_dir is None:
        raise SystemExit(2)

    since = _parse_iso_datetime_arg(args.since)
    until = _parse_iso_datetime_arg(args.until)

    runtime = build_runtime(data_dir=args.data_dir)
    limit = min(args.limit, runtime.config.observability.timeline_export_max_events)
    timeline_config = {
        "worker.max_concurrent_jobs": runtime.config.worker.max_concurrent_jobs,
        "worker.max_unit_workers_per_job": runtime.config.worker.max_unit_workers_per_job,
        "vlm.max_concurrent_requests": runtime.config.vlm.max_concurrent_requests,
        "observability.timeline_export_max_events": (
            runtime.config.observability.timeline_export_max_events
        ),
    }
    with runtime.session() as session:
        timeline = runtime.repositories(session).timeline()
        if args.all:
            events = timeline.list_all(since=since, until=until, limit=limit)
        else:
            events = timeline.list_by_job(
                args.job_id, since=since, until=until, limit=limit
            )
    runtime.dispose()
    if args.out_dir is not None:
        out_dir = Path(args.out_dir)
        html_out: str | Path = out_dir / "index.html"
        json_out: str | Path | None = out_dir / "index.json"
    else:
        html_out = args.out
        json_out = args.json_out
    if args.all:
        result = export_aggregate_timeline(
            events=events,
            html_out=html_out,
            json_out=json_out,
            config=timeline_config,
        )
    else:
        result = export_timeline(
            analysis_job_id=args.job_id,
            events=events,
            html_out=html_out,
            json_out=json_out,
        )
    _print({"timeline": result.__dict__})
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    from cctv_memory.application.benchmark import BenchmarkRunner
    from cctv_memory.application.experiment_fixtures import golden_queries_from_records
    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=args.data_dir)
    with runtime.session() as session:
        repos = runtime.repositories(session)
        # Resolve scope, then derive golden queries from the authorized records so
        # the benchmark has a reproducible relevance set (mock-VLM tags).
        from cctv_memory.application.auth import AuthorizationService

        auth = AuthorizationService(
            repos.principal(), repos.access_policy(), repos.camera()
        )
        principal = auth.resolve_principal(args.principal_id)
        scope = auth.authorized_scope_for(principal)
        records = repos.observation_read().authorized_candidate_pool(scope, limit=1000)
        queries = golden_queries_from_records(records)
        runner = BenchmarkRunner(
            repos.observation_read(), repos.search_context(), repos.audit()
        )
        result = runner.run(queries, scope, k=args.k)
    runtime.dispose()
    _print(result.model_dump(mode="json"))
    return 0


def _cmd_experiment(args: argparse.Namespace) -> int:
    import yaml

    from cctv_memory.application.benchmark import ExperimentRunner
    from cctv_memory.contracts.experiment import ExperimentConfig
    from cctv_memory.infrastructure.runtime import build_runtime

    with open(args.config, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    config = ExperimentConfig.model_validate(raw)

    runtime = build_runtime(data_dir=args.data_dir)
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(args.principal_id)
        scope = svc.auth.authorized_scope_for(principal)
    with runtime.session() as session:
        repos = runtime.repositories(session)
        runner = ExperimentRunner(
            repos.observation_read(), repos.search_context(), repos.audit()
        )
        result = runner.run(config, scope)
    runtime.dispose()
    _print(result.model_dump(mode="json"))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cctv-memory", description="CCTV Memory CLI (MVP closed-loop)."
    )
    parser.add_argument("--version", action="version", version=f"cctv-memory {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="Print the version.")
    sub.add_parser("health", help="Print a local health report.")

    p_doctor = sub.add_parser(
        "doctor", help="Diagnose the effective config + readiness (no secrets)."
    )
    p_doctor.add_argument("--data-dir", default="./data")
    p_doctor.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of text."
    )

    p_init = sub.add_parser("init", help="Create data dir + schema + seed defaults.")
    p_init.add_argument("--data-dir", default="./data")

    p_analyze = sub.add_parser("analyze", help="Submit a video source for analysis.")
    p_analyze.add_argument("--data-dir", default="./data")
    p_analyze.add_argument("--source-uri", required=True)
    p_analyze.add_argument("--camera-id", required=True)
    p_analyze.add_argument("--video-start-time", required=True, help="ISO-8601 timestamp")
    p_analyze.add_argument("--source-type", default="file")
    p_analyze.add_argument("--external-source-id", default=None)
    p_analyze.add_argument("--idempotency-key", default=None)
    p_analyze.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)
    p_analyze.add_argument("--wait", action="store_true", help="Run worker until drained.")
    p_analyze.add_argument(
        "--enable-high-freq",
        action="store_true",
        help="Also run motion_scan + high_freq_event scales (motion-triggered).",
    )

    p_worker = sub.add_parser("worker", help="Process queued analysis tasks.")
    p_worker.add_argument("--data-dir", default="./data")
    p_worker.add_argument("--once", action="store_true")

    p_serve = sub.add_parser("serve", help="Start the FastAPI HTTP server.")
    p_serve.add_argument("--data-dir", default="./data")
    p_serve.add_argument("--host", default=None, help="Override server.host.")
    p_serve.add_argument("--port", type=int, default=None, help="Override server.port.")
    p_serve.add_argument(
        "--no-worker",
        action="store_true",
        help="Do not start the embedded worker even if config enables it.",
    )
    p_serve.add_argument(
        "--worker-poll-seconds",
        type=float,
        default=2.0,
        help="Embedded worker idle poll interval in seconds.",
    )

    p_search = sub.add_parser("search", help="Run an authorized observation search.")
    p_search.add_argument("--data-dir", default="./data")
    p_search.add_argument("--query", default=None)
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)

    p_backup = sub.add_parser("backup", help="Create an admin full backup.")
    p_backup.add_argument("--data-dir", default="./data")
    p_backup.add_argument("--out", required=True)
    p_backup.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)

    p_restore = sub.add_parser("restore", help="Restore from an admin backup.")
    p_restore.add_argument("--data-dir", default="./data")
    p_restore.add_argument("--in", dest="infile", required=True)
    p_restore.add_argument("--manifest", default=None, help="Defaults to <in>.manifest.json")
    p_restore.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)

    p_bench = sub.add_parser("benchmark", help="Run search benchmark metrics.")
    p_bench_sub = p_bench.add_subparsers(dest="benchmark_command")
    p_bench_run = p_bench_sub.add_parser("run", help="Run benchmark on golden queries.")
    p_bench_run.add_argument("--data-dir", default="./data")
    p_bench_run.add_argument("--k", type=int, default=10)
    p_bench_run.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)

    p_exp = sub.add_parser("experiment", help="Run a search-weight experiment.")
    p_exp_sub = p_exp.add_subparsers(dest="experiment_command")
    p_exp_run = p_exp_sub.add_parser("run", help="Run experiment from a config YAML.")
    p_exp_run.add_argument("--config", required=True)
    p_exp_run.add_argument("--data-dir", default="./data")
    p_exp_run.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)

    p_reindex = sub.add_parser(
        "reindex", help="Rebuild vector index for authorized records (admin)."
    )
    p_reindex.add_argument("--data-dir", default="./data")
    p_reindex.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)
    p_reindex.add_argument(
        "--force", action="store_true", help="Re-embed even if vectors are current."
    )

    p_maint = sub.add_parser("maintenance", help="Maintenance operations.")
    p_maint_sub = p_maint.add_subparsers(dest="maintenance_command")
    p_maint_sweep = p_maint_sub.add_parser(
        "sweep", help="Expire stale SearchContexts."
    )
    p_maint_sweep.add_argument("--data-dir", default="./data")
    p_maint_sweep.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)

    p_diag = sub.add_parser("diagnostics", help="Admin diagnostics operations.")
    p_diag_sub = p_diag.add_subparsers(dest="diagnostics_command")
    p_diag_failures = p_diag_sub.add_parser(
        "failures", help="Show model-output failure diagnostics for one job."
    )
    p_diag_failures.add_argument("--data-dir", default="./data")
    p_diag_failures.add_argument("--principal-id", default=DEV_PRINCIPAL_ID)
    p_diag_failures.add_argument("--job-id", required=True)

    p_timeline = sub.add_parser("timeline", help="Timeline observability operations.")
    p_timeline_sub = p_timeline.add_subparsers(dest="timeline_command")
    p_timeline_export = p_timeline_sub.add_parser(
        "export", help="Export an analysis job timeline to offline Plotly HTML."
    )
    p_timeline_export.add_argument("--data-dir", default="./data")
    timeline_selector = p_timeline_export.add_mutually_exclusive_group(required=True)
    timeline_selector.add_argument("--job-id", default=None)
    timeline_selector.add_argument("--all", action="store_true")
    p_timeline_export.add_argument("--out", default=None)
    p_timeline_export.add_argument("--json-out", default=None)
    p_timeline_export.add_argument("--out-dir", default=None)
    p_timeline_export.add_argument("--since", default=None, help="ISO-8601 start time")
    p_timeline_export.add_argument("--until", default=None, help="ISO-8601 end time")
    p_timeline_export.add_argument("--limit", type=int, default=100_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"cctv-memory {__version__}")
        return 0
    if args.command == "health":
        report = get_health_report()
        _print(
            {
                "status": report.status,
                "version": report.version,
                "schema_version": report.schema_version,
                "phase": report.phase,
            }
        )
        return 0
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "worker":
        return _cmd_worker(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "search":
        return _cmd_search(args)
    if args.command == "backup":
        return _cmd_backup(args)
    if args.command == "restore":
        return _cmd_restore(args)
    if args.command == "benchmark":
        if getattr(args, "benchmark_command", None) == "run":
            return _cmd_benchmark(args)
        parser.print_help()
        return 0
    if args.command == "experiment":
        if getattr(args, "experiment_command", None) == "run":
            return _cmd_experiment(args)
        parser.print_help()
        return 0
    if args.command == "reindex":
        return _cmd_reindex(args)
    if args.command == "maintenance":
        if getattr(args, "maintenance_command", None) == "sweep":
            return _cmd_maintenance(args)
        parser.print_help()
        return 0
    if args.command == "diagnostics":
        if getattr(args, "diagnostics_command", None) == "failures":
            return _cmd_diagnostics(args)
        parser.print_help()
        return 0
    if args.command == "timeline":
        if getattr(args, "timeline_command", None) == "export":
            return _cmd_timeline_export(args)
        parser.print_help()
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
