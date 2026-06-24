"""§3.C VLM scheduler concurrency + rate-limit and §3.E debug media retention tests.

These tests are written against the *desired* behavior; they should fail until
the implementation adds real scheduler and debug media artifact logic.
"""
from __future__ import annotations

import time
from pathlib import Path

from cctv_memory.application.ingestion import IngestionService
from cctv_memory.application.publication import PublicationService
from cctv_memory.contracts.analysis import HighFreqTrigger
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TriggerStatus,
)
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.workers.analysis_worker import AnalysisWorker
from cctv_memory.workers.high_freq_event import HighFreqEventProcessor

from tests.conftest import seed_camera

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s",
        dynamic="d",
        tags=["person"],
        quality={"reason": "", "score": 0.9},
        attr={"alert": False},
    )


def _seed(runtime, pid: str = "svc_sched") -> None:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_camera(repos)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        repos.principal().create_principal(
            Principal(
                principal_id=pid,
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )


def _submit(runtime, *, pid: str = "svc_sched", key: str = "sched-1") -> str:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        principal = repos.principal().get_principal(pid)
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=__import__(
                    "datetime"
                ).datetime(2026, 6, 11, 15, 0, tzinfo=__import__("datetime").timezone.utc),
                idempotency_key=key,
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


# ---------------------------------------------------------------------------
# §3.C — scheduler tests
# ---------------------------------------------------------------------------


class _TimedVlm:
    """Records wall-clock start time for each call."""

    def __init__(self) -> None:
        self.start_times_ns: list[int] = []
        self.call_count = 0

    def analyze_segment(self, request):  # type: ignore[no-untyped-def]
        self.start_times_ns.append(time.monotonic_ns())
        self.call_count += 1
        return _vlm_output()


def test_min_request_interval_ms_is_respected(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """With 3 windows and min_request_interval_ms=50, successive request starts
    must be ≥ 50 ms apart."""
    runtime = runtime_factory()
    runtime.config.vlm.min_request_interval_ms = 50
    runtime.config.pipeline.default_segment.window_seconds = 10
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    _seed(runtime, "svc_interval")
    _submit(runtime, pid="svc_interval", key="interval-1")

    vlm = _TimedVlm()
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        vlm=vlm,
    )
    worker.process_one()

    assert vlm.call_count >= 2, "need at least 2 calls to measure interval"
    min_interval_ns = 50 * 1_000_000  # 50 ms in ns
    for i in range(1, len(vlm.start_times_ns)):
        gap_ns = vlm.start_times_ns[i] - vlm.start_times_ns[i - 1]
        assert gap_ns >= min_interval_ns, (
            f"gap between calls {i-1} and {i} was only {gap_ns/1e6:.1f}ms, "
            f"expected >= 50ms"
        )
    runtime.dispose()


def test_max_concurrent_requests_limits_in_flight(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Global VLM cap limits in-flight calls when the unit pool allows overlap.

    ``worker.max_unit_workers_per_job`` creates unit worker slots;
    ``vlm.max_concurrent_requests`` is only the provider-call cap.
    """
    import threading

    runtime = runtime_factory()
    runtime.config.worker.max_unit_workers_per_job = 2
    runtime.config.vlm.max_concurrent_requests = 2
    runtime.config.vlm.min_request_interval_ms = 0
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    _seed(runtime, "svc_concur")
    _submit(runtime, pid="svc_concur", key="concur-1")

    peak_concurrent = 0
    current_concurrent = 0
    lock = threading.Lock()

    class _ConcurrencyProbe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            nonlocal peak_concurrent, current_concurrent
            with lock:
                current_concurrent += 1
                if current_concurrent > peak_concurrent:
                    peak_concurrent = current_concurrent
            # Brief sleep to let other threads arrive
            time.sleep(0.02)
            with lock:
                current_concurrent -= 1
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_ConcurrencyProbe(),
    )
    worker.process_one()

    assert peak_concurrent <= 2, (
        f"peak concurrency was {peak_concurrent}, should be <= max_concurrent_requests=2"
    )
    assert peak_concurrent >= 2, "main path did not actually overlap VLM calls"
    runtime.dispose()


def test_high_freq_event_uses_bounded_parallel_vlm_calls(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    import threading

    runtime = runtime_factory()
    runtime.config.vlm.max_concurrent_requests = 2
    runtime.config.vlm.min_request_interval_ms = 0
    _seed(runtime, "svc_hf_concur")
    job_id = _submit(runtime, pid="svc_hf_concur", key="hf-concur-1")

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        video_id = job.video_id
        high_freq_task = repos.scale_task().get_by_job_and_scale(
            job_id, AnalysisScale.HIGH_FREQ_EVENT.value
        )
        if high_freq_task is None:
            from cctv_memory.contracts.analysis import AnalysisScaleTask
            from cctv_memory.domain.enums import TaskStatus
            from cctv_memory.workers.common import new_id

            high_freq_task = repos.scale_task().create_scale_task(
                AnalysisScaleTask(
                    scale_task_id=new_id("scale"),
                    analysis_job_id=job_id,
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    status=TaskStatus.PENDING,
                )
            )
        trigger = HighFreqTrigger(
            trigger_id="trig_hf_concur",
            analysis_job_id=job_id,
            scale_task_id=high_freq_task.scale_task_id,
            video_id=video_id,
            trigger_start_ms=0,
            trigger_end_ms=20_000,
            motion_score=0.9,
            trigger_reason="motion_spike",
            status=TriggerStatus.PENDING,
            idempotency_key=HighFreqTrigger.build_idempotency_key(
                job_id, video_id, 0, 20_000, "motion_spike"
            ),
        )
        repos.trigger().create_or_get_by_idempotency(trigger)

    peak_concurrent = 0
    current_concurrent = 0
    lock = threading.Lock()

    class _ConcurrencyProbe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            nonlocal peak_concurrent, current_concurrent
            with lock:
                current_concurrent += 1
                peak_concurrent = max(peak_concurrent, current_concurrent)
            time.sleep(0.02)
            with lock:
                current_concurrent -= 1
            return _vlm_output()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        processor = HighFreqEventProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            triggers=repos.trigger(),
            video_processor=StaticVideoProcessor(duration_ms=20_000),
            vlm=_ConcurrencyProbe(),
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scale_task_id=high_freq_task.scale_task_id,
            window_seconds=5,
            overlap_ratio=0,
            max_concurrent_requests=2,
            runtime=runtime,
            commit_before_concurrent=session.commit,
        )
        processor.process(job_id, video_id)

    assert peak_concurrent <= 2
    assert peak_concurrent >= 2, "high_freq_event did not overlap VLM calls"
    runtime.dispose()


# ---------------------------------------------------------------------------
# Stage C1 — GLOBAL VlmScheduler across scales
# ---------------------------------------------------------------------------


def _seed_motion_trigger(runtime, job_id: str) -> str:  # type: ignore[no-untyped-def]
    """Attach a high_freq_event scale task + one trigger so high_freq runs."""
    from cctv_memory.contracts.analysis import AnalysisScaleTask
    from cctv_memory.domain.enums import TaskStatus
    from cctv_memory.workers.common import new_id

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        video_id = job.video_id
        hf = repos.scale_task().get_by_job_and_scale(
            job_id, AnalysisScale.HIGH_FREQ_EVENT.value
        )
        if hf is None:
            hf = repos.scale_task().create_scale_task(
                AnalysisScaleTask(
                    scale_task_id=new_id("scale"),
                    analysis_job_id=job_id,
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    status=TaskStatus.PENDING,
                )
            )
        trigger = HighFreqTrigger(
            trigger_id="trig_global_sched",
            analysis_job_id=job_id,
            scale_task_id=hf.scale_task_id,
            video_id=video_id,
            trigger_start_ms=0,
            trigger_end_ms=20_000,
            motion_score=0.9,
            trigger_reason="motion_spike",
            status=TriggerStatus.PENDING,
            idempotency_key=HighFreqTrigger.build_idempotency_key(
                job_id, video_id, 0, 20_000, "motion_spike"
            ),
        )
        repos.trigger().create_or_get_by_idempotency(trigger)
    return video_id


def test_global_concurrency_cap_across_default_and_high_freq(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Stage C1: the GLOBAL VLM cap holds across both scale processors.

    ``worker.max_unit_workers_per_job`` creates enough worker slots for real
    overlap; ``vlm.max_concurrent_requests=2`` caps provider calls across BOTH
    default_segment AND high_freq_event units. Proven by a probe that records
    which scales overlap and the peak global concurrency.
    """
    import threading

    runtime = runtime_factory()
    runtime.config.worker.max_unit_workers_per_job = 4
    runtime.config.vlm.max_concurrent_requests = 2
    runtime.config.vlm.min_request_interval_ms = 0
    runtime.config.pipeline.default_segment.window_seconds = 5
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 5
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_global_cap")
    job_id = _submit(runtime, pid="svc_global_cap", key="global-cap-1")
    _seed_motion_trigger(runtime, job_id)

    peak_concurrent = 0
    current = 0
    scales_seen: set = set()
    lock = threading.Lock()

    class _GlobalProbe:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            nonlocal peak_concurrent, current
            with lock:
                current += 1
                peak_concurrent = max(peak_concurrent, current)
                scales_seen.add(request.analysis_scale)
            time.sleep(0.02)
            with lock:
                current -= 1
            return _vlm_output()

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_GlobalProbe(),
    )
    worker.process_one()

    assert peak_concurrent <= 2, (
        f"global peak concurrency {peak_concurrent} exceeded provider cap 2"
    )
    # Both scales must have produced VLM calls so the cap is genuinely global.
    assert AnalysisScale.DEFAULT_SEGMENT in scales_seen
    assert AnalysisScale.HIGH_FREQ_EVENT in scales_seen
    runtime.dispose()


def test_global_min_interval_across_scales(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Stage C1: min_request_interval_ms is enforced between VLM request STARTS
    globally — including across the default_segment -> high_freq_event boundary
    (the shared scheduler's last-start clock persists across scales)."""
    runtime = runtime_factory()
    runtime.config.vlm.max_concurrent_requests = 1
    runtime.config.vlm.min_request_interval_ms = 40
    runtime.config.pipeline.default_segment.window_seconds = 10
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.pipeline.high_freq_event.window_seconds = 10
    runtime.config.pipeline.high_freq_event.overlap_ratio = 0
    _seed(runtime, "svc_global_int")
    job_id = _submit(runtime, pid="svc_global_int", key="global-int-1")
    _seed_motion_trigger(runtime, job_id)

    vlm = _TimedVlm()
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=vlm,
    )
    worker.process_one()

    # At least one default + one high_freq call -> several starts overall.
    assert vlm.call_count >= 2
    min_interval_ns = 40 * 1_000_000
    starts = sorted(vlm.start_times_ns)
    for i in range(1, len(starts)):
        gap = starts[i] - starts[i - 1]
        assert gap >= min_interval_ns, (
            f"global interval violated across calls {i-1}->{i}: {gap/1e6:.1f}ms < 40ms"
        )
    runtime.dispose()


def test_worker_shares_one_scheduler_instance(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Stage C1 structural guarantee: the worker holds exactly one VlmScheduler and
    injects that same instance into both scale processors (not one per scale)."""
    runtime = runtime_factory()
    runtime.config.vlm.max_concurrent_requests = 3
    _seed(runtime, "svc_one_sched")
    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=20_000),
        vlm=_TimedVlm(),
    )
    # The shared scheduler exists and is a single object.
    assert worker._vlm_scheduler is not None
    sched = worker._vlm_scheduler
    # Processors built with that instance share it (not one-scheduler-per-scale).
    with runtime.session() as session:
        repos = runtime.repositories(session)
        from cctv_memory.workers.default_segment import DefaultSegmentProcessor
        from cctv_memory.workers.high_freq_event import HighFreqEventProcessor

        dproc = DefaultSegmentProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            video_processor=StaticVideoProcessor(duration_ms=20_000),
            vlm=_TimedVlm(),
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scale_task_id="st",
            scheduler=sched,
        )
        hproc = HighFreqEventProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            triggers=repos.trigger(),
            video_processor=StaticVideoProcessor(duration_ms=20_000),
            vlm=_TimedVlm(),
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scheduler=sched,
        )
        assert dproc._scheduler is sched
        assert hproc._scheduler is sched
    runtime.dispose()


# ---------------------------------------------------------------------------
# §3.E — debug media artifact retention tests
# ---------------------------------------------------------------------------


def test_default_metadata_only_mode_has_no_base64_in_model_call_log(
    runtime_factory,
) -> None:  # type: ignore[no-untyped-def]
    """In metadata_only mode (default), media_refs in ModelCallLog must not
    contain inline base64 data."""
    import json as _json

    runtime = runtime_factory()
    assert runtime.config.vlm.debug_media_retention is False
    assert runtime.config.vlm.media_log_mode == "metadata_only"
    runtime.config.pipeline.default_segment.window_seconds = 30
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    _seed(runtime, "svc_meta")
    _submit(runtime, pid="svc_meta", key="meta-1")

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=30_000),
        vlm=type("_FastVlm", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    worker.process_one()

    with runtime.session() as session:
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        logs = list(
            session.scalars(select(orm.ModelCallLog))
        )
        assert logs, "no model call logs written"
        for log in logs:
            raw = _json.loads(log.media_refs_json)
            for ref in raw:
                ref_str = _json.dumps(ref)
                assert "base64" not in ref_str.lower(), (
                    f"metadata_only mode must not store base64 in media_refs: {ref_str[:200]}"
                )
    runtime.dispose()


def test_debug_media_retention_writes_artifacts_and_records_refs(
    tmp_path: Path, runtime_factory
) -> None:  # type: ignore[no-untyped-def]
    """In debug_media_retention mode, frame files must be copied into artifact_root
    and ModelCallLog media_refs must contain artifact_uri pointing to those files."""
    import json as _json

    # The frame paths now include the per-unit isolation key (model_call_id, R10/
    # P0), so we cannot pre-seed a fixed directory. Use a processor that WRITES the
    # placeholder frames at whatever isolated path it returns, so debug_media has
    # real files to copy.
    frame_root = tmp_path / "frames"
    frame_root.mkdir(parents=True, exist_ok=True)

    from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor

    class _WritingStaticProcessor(StaticVideoProcessor):
        def extract_frame_uris(  # type: ignore[no-untyped-def]
            self, source_uri, start_ms, end_ms, frame_count, *, unit_key=None
        ):
            uris = super().extract_frame_uris(
                source_uri, start_ms, end_ms, frame_count, unit_key=unit_key
            )
            for i, uri in enumerate(uris):
                p = Path(uri)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"\xff\xd8\xff" + bytes([i % 256]))
            return uris

    runtime = runtime_factory()
    runtime.config.vlm.debug_media_retention = True
    runtime.config.vlm.media_log_mode = "debug_full_media"
    runtime.config.pipeline.default_segment.window_seconds = 30
    runtime.config.pipeline.default_segment.overlap_seconds = 0
    runtime.config.storage.frame_root = str(frame_root)

    _seed(runtime, "svc_debug")
    _submit(runtime, pid="svc_debug", key="debug-1")

    worker = AnalysisWorker(
        runtime,
        video_processor=_WritingStaticProcessor(
            duration_ms=30_000, frame_root=str(frame_root)
        ),
        vlm=type("_FastVlm", (), {"analyze_segment": lambda s, r: _vlm_output()})(),
    )
    worker.process_one()

    with runtime.session() as session:
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        logs = list(session.scalars(select(orm.ModelCallLog)))
        assert logs, "no model call logs written"
        for log in logs:
            refs = _json.loads(log.media_refs_json)
            # In debug mode at least one ref must have an artifact_uri
            assert any("artifact_uri" in ref for ref in refs), (
                f"debug_media_retention: expected artifact_uri in media_refs, got: {refs}"
            )
            # artifact_uri must point to an existing file
            for ref in refs:
                if "artifact_uri" in ref:
                    path = Path(ref["artifact_uri"])
                    assert path.exists(), (
                        f"artifact file does not exist: {path}"
                    )
    runtime.dispose()
