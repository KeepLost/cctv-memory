"""Lifecycle guard + job-finalize DB-truth reconciliation tests.

Task cctv-memory-20260616-1850:
- §B1: once a unit is ``mark_running``, ANY unforeseen exception in the
  running-unit body force-terminalizes it to FAILED with phase-tagged durable
  evidence (a ModelCallLog whose error_type is ``lifecycle_guard:<phase>``). The
  unit can never remain ``running`` with no diagnosis. No new durable state.
- §B2: before a job is finalized, its own leftover ``running`` units are
  reconciled from DB truth; other jobs' running units are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.application.ingestion import IngestionService
from cctv_memory.application.publication import PublicationService
from cctv_memory.contracts.analysis import AnalysisUnit
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
)
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.workers.analysis_worker import AnalysisWorker
from cctv_memory.workers.common import (
    DEFAULT_POLICY_ID,
    new_id,
    resolve_video_context,
)
from cctv_memory.workers.default_segment import DefaultSegmentProcessor
from cctv_memory.workers.unit_result import UnitOutcome


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s", dynamic="d", tags=["person"],
        quality={"reason": "", "score": 0.9}, attr={"alert": False},
    )


class _Vlm:
    def analyze_segment(self, request):  # type: ignore[no-untyped-def]
        return _vlm_output()


def _seed(runtime, pid: str) -> None:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
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


def _submit(runtime, pid: str, key: str, minute: int = 0) -> str:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
        )
        principal = repos.principal().get_principal(pid)
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=f"/data/videos/{key}.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 16, 18, minute % 60, tzinfo=UTC),
                idempotency_key=key,
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


def _units_for_job(runtime, job_id: str):  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        scale_tasks = repos.scale_task().list_by_job(job_id)
        units = []
        for st in scale_tasks:
            units.extend(repos.analysis_unit().list_by_scale_task(st.scale_task_id))
        job = repos.analysis_job().get_job(job_id)
        return job, scale_tasks, units


# ---------------------------------------------------------------------------
# §B1 — lifecycle guard
# ---------------------------------------------------------------------------


def _build_default_processor(runtime, scale_task_id: str):  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        return DefaultSegmentProcessor(
            video_sources=repos.video_source(),
            jobs=repos.analysis_job(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            video_processor=StaticVideoProcessor(duration_ms=30_000),
            vlm=_Vlm(),
            publication=PublicationService(repos.publication()),
            units=repos.analysis_unit(),
            model_calls=repos.model_call_log(),
            scale_task_id=scale_task_id,
            runtime=runtime,
            scheduler=None,
            write_coordinator=runtime.write_coordinator,
        )


def test_exception_after_mark_running_force_terminalizes_unit(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """An unforeseen escape in the running-unit body must NOT leave the unit
    running: the guard marks it FAILED with phase-tagged evidence."""
    runtime = runtime_factory()
    _seed(runtime, "svc_guard")
    job_id = _submit(runtime, "svc_guard", "k_guard")
    _job, scale_tasks, _units = _units_for_job(runtime, job_id)
    default_st = next(
        st for st in scale_tasks if st.analysis_scale.value == "default_segment"
    )

    proc = _build_default_processor(runtime, default_st.scale_task_id)

    # Force an UNFORESEEN escape from the running-unit body (simulates e.g. a
    # terminal-write that exhausted bounded retry and re-raised).
    def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("unexpected escape after mark_running")

    proc._execute_running_unit = _boom  # type: ignore[method-assign]

    unit_template = AnalysisUnit(
        unit_id=new_id("unit"),
        analysis_job_id=job_id,
        scale_task_id=default_st.scale_task_id,
        video_id=_job.video_id,  # type: ignore[union-attr]
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        segment_start_ms=0,
        segment_end_ms=12_000,
        window_index=0,
        idempotency_key=f"{job_id}:{default_st.scale_task_id}:default_segment:0:12000",
    )

    with runtime.session() as session:
        repos = runtime.repositories(session)
        ctx = resolve_video_context(
            _job.video_id,  # type: ignore[union-attr]
            video_sources=repos.video_source(),
            cameras=repos.camera(),
            policies_repo=repos.access_policy(),
            default_policy_id=DEFAULT_POLICY_ID,
        )

    outcome = proc._run_unit_in_fresh_session(
        unit_template, 0, 12_000, job_id, _job.video_id, ctx  # type: ignore[union-attr]
    )
    assert outcome is UnitOutcome.FAILED

    # The unit is terminal FAILED (never left running) with diagnosable evidence.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        units = repos.analysis_unit().list_by_scale_task(default_st.scale_task_id)
        target = next(u for u in units if u.idempotency_key == unit_template.idempotency_key)
        assert target.status is TaskStatus.FAILED
        assert target.status is not TaskStatus.RUNNING
        logs = repos.model_call_log().list_by_unit(target.unit_id)
        assert any(
            (log.error_type or "").startswith("lifecycle_guard:") for log in logs
        ), "expected a phase-tagged lifecycle_guard ModelCallLog as durable evidence"


# ---------------------------------------------------------------------------
# §B2 — job finalize reconciles its OWN running units (DB truth)
# ---------------------------------------------------------------------------


def test_job_finalize_has_no_residual_running_units(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """A normal job run leaves zero RUNNING units after finalize (baseline)."""
    runtime = runtime_factory()
    _seed(runtime, "svc_fin")
    job_id = _submit(runtime, "svc_fin", "k_fin")
    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    worker.process_one()
    _job, _st, units = _units_for_job(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)


def test_reconcile_only_touches_target_jobs_running_units(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """§B2: per-job reconciliation marks the target job's residual running units
    FAILED while leaving another job's running unit untouched."""
    from cctv_memory.infrastructure.db.models import tables as orm

    runtime = runtime_factory()
    _seed(runtime, "svc_recon")
    job_a = _submit(runtime, "svc_recon", "k_recon_a", minute=1)
    job_b = _submit(runtime, "svc_recon", "k_recon_b", minute=2)

    worker = AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=30_000), vlm=_Vlm()
    )
    # Process both jobs normally first so units exist.
    worker.process_one()
    worker.process_one()

    _ja, _sta, units_a = _units_for_job(runtime, job_a)
    _jb, _stb, units_b = _units_for_job(runtime, job_b)
    # Inject a residual RUNNING unit into BOTH jobs (simulating a terminal-write
    # that didn't persist).
    with runtime.session() as session:
        for uid in (units_a[0].unit_id, units_b[0].unit_id):
            row = session.get(orm.AnalysisUnit, uid)
            row.status = TaskStatus.RUNNING.value
            row.finished_at = None

    # Reconcile ONLY job_a.
    worker._reconcile_running_units_for_job(job_a)

    with runtime.session() as session:
        repos = runtime.repositories(session)
        a_unit = repos.analysis_unit().get_unit(units_a[0].unit_id)
        b_unit = repos.analysis_unit().get_unit(units_b[0].unit_id)
    # job_a's residual running unit is now FAILED; job_b's is untouched (still running).
    assert a_unit is not None and a_unit.status is TaskStatus.FAILED
    assert b_unit is not None and b_unit.status is TaskStatus.RUNNING

