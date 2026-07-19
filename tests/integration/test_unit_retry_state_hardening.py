"""Tests for task cctv-memory-20260615-1447: unit-level VLM transient retry +
terminal-write state hardening + per-attempt auditability.

Covers:
- pure retry policy: backoff/jitter math, transient vs permanent classification,
  scheduler-routing, DB-write retry on transient lock;
- first transient provider failure then success => unit succeeded, ONE active record,
  ModelCallLog has a failed attempt + a success attempt with real attempt_count;
- all transient attempts fail => unit failed (no running residue), one ModelCallLog
  per attempt, error_code vlm_provider_error;
- permanent error (schema validation) => NO unit-level retry (single attempt);
- retry routes every attempt through the injected VlmScheduler;
- idempotency: retry success does not duplicate the active ObservationRecord;
- default_segment + high_freq_event, cross-scale + sequential paths.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    Capability,
    JobStatus,
    ModelCallStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
)
from cctv_memory.domain.exceptions import VlmSchemaValidationError
from cctv_memory.infrastructure.vlm.real_adapter import VlmProviderError
from cctv_memory.workers.analysis_worker import AnalysisWorker
from cctv_memory.workers.retry import (
    RetryPolicy,
    VlmAttempt,
    compute_backoff_ms,
    execute_vlm_with_retry,
    is_transient_db_error,
    is_transient_vlm_error,
    run_db_write_with_retry,
    vlm_failure_error_code,
)

# ---------------------------------------------------------------------------
# Pure retry-policy unit tests (no DB)
# ---------------------------------------------------------------------------


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s",
        dynamic="d",
        tags=["person"],
        quality={"reason": "", "score": 0.9},
        attr={"alert": False},
    )


def test_is_transient_vlm_error_classifies_provider_vs_permanent() -> None:
    assert is_transient_vlm_error(VlmProviderError("timeout")) is True
    assert is_transient_vlm_error(VlmSchemaValidationError("bad json")) is False
    assert is_transient_vlm_error(ValueError("nope")) is False


def test_vlm_failure_error_code_mapping() -> None:
    assert vlm_failure_error_code(VlmProviderError("x")) == "vlm_provider_error"
    assert (
        vlm_failure_error_code(VlmSchemaValidationError("x"))
        == "vlm_schema_validation_failed"
    )
    assert vlm_failure_error_code(ValueError("x")) == "analysis_unit_failed"


def test_compute_backoff_is_exponential_and_capped_without_jitter() -> None:
    assert compute_backoff_ms(1, base_ms=500, cap_ms=8000, jitter=0.0) == 500
    assert compute_backoff_ms(2, base_ms=500, cap_ms=8000, jitter=0.0) == 1000
    assert compute_backoff_ms(3, base_ms=500, cap_ms=8000, jitter=0.0) == 2000
    # capped
    assert compute_backoff_ms(10, base_ms=500, cap_ms=8000, jitter=0.0) == 8000
    # base 0 disables waiting
    assert compute_backoff_ms(3, base_ms=0, cap_ms=8000, jitter=0.5) == 0.0


def test_compute_backoff_jitter_within_bounds() -> None:
    rng = random.Random(42)
    for _ in range(100):
        # attempt=1 => raw = base = 1000; jitter 0.2 => [800, 1200]
        v = compute_backoff_ms(1, base_ms=1000, cap_ms=8000, jitter=0.2, rng=rng)
        assert 800.0 <= v <= 1200.0


class _RoutedScheduler:
    """Stand-in scheduler that records that every attempt was routed through it."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self, fn):  # type: ignore[no-untyped-def]
        self.runs += 1
        return fn()


def test_execute_vlm_retries_transient_then_succeeds() -> None:
    sched = _RoutedScheduler()
    attempts_seen: list[int] = []

    calls = {"n": 0}

    def analyze(_req, _strict_schema: bool = False):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise VlmProviderError("cold start")
        return _vlm_output()

    result = execute_vlm_with_retry(
        request=object(),  # type: ignore[arg-type]
        analyze=analyze,
        scheduler_run=sched.run,
        policy=RetryPolicy(max_attempts=3, backoff_base_ms=0, jitter=0.0),
        on_attempt_failed=lambda rec: attempts_seen.append(rec.attempt),
        sleep=lambda _s: None,
    )
    assert result.error is None
    assert result.output is not None
    assert result.attempts == 2
    assert attempts_seen == [1]  # one failed attempt recorded
    assert sched.runs == 2  # every attempt went through the scheduler
    # attempt_details has failed(1) + succeeded(2)
    assert [d["status"] for d in result.attempt_details] == ["failed", "succeeded"]


def test_execute_vlm_exhausts_transient_budget() -> None:
    sched = _RoutedScheduler()
    failed: list[int] = []

    def analyze(_req, _strict_schema: bool = False):  # type: ignore[no-untyped-def]
        raise VlmProviderError("still down")

    result = execute_vlm_with_retry(
        request=object(),  # type: ignore[arg-type]
        analyze=analyze,
        scheduler_run=sched.run,
        policy=RetryPolicy(max_attempts=3, backoff_base_ms=0, jitter=0.0),
        on_attempt_failed=lambda rec: failed.append(rec.attempt),
        sleep=lambda _s: None,
    )
    assert result.output is None
    assert isinstance(result.error, VlmProviderError)
    assert result.attempts == 3
    assert failed == [1, 2, 3]
    assert sched.runs == 3


def test_execute_vlm_permanent_error_not_retried() -> None:
    sched = _RoutedScheduler()
    failed: list[int] = []

    def analyze(_req, _strict_schema: bool = False):  # type: ignore[no-untyped-def]
        raise VlmSchemaValidationError("bad schema")

    result = execute_vlm_with_retry(
        request=object(),  # type: ignore[arg-type]
        analyze=analyze,
        scheduler_run=sched.run,
        policy=RetryPolicy(max_attempts=5, backoff_base_ms=0, jitter=0.0),
        on_attempt_failed=lambda rec: failed.append(rec.attempt),
        sleep=lambda _s: None,
    )
    assert isinstance(result.error, VlmSchemaValidationError)
    assert result.attempts == 1  # NO retry for permanent errors
    assert sched.runs == 1
    assert failed == [1]


def test_execute_vlm_schema_regeneration_uses_scheduler_and_succeeds() -> None:
    sched = _RoutedScheduler()
    strict_flags: list[bool] = []

    def analyze(_req, strict_schema: bool = False):  # type: ignore[no-untyped-def]
        strict_flags.append(strict_schema)
        if not strict_schema:
            raise VlmSchemaValidationError(
                "bad schema",
                stage="schema_validation_failed",
                raw_response='{"static":"only"}',
                parsed_payload={"static": "only"},
            )
        return _vlm_output()

    result = execute_vlm_with_retry(
        request=object(),  # type: ignore[arg-type]
        analyze=analyze,
        scheduler_run=sched.run,
        policy=RetryPolicy(
            max_attempts=1,
            schema_regenerate_max_attempts=1,
            schema_retry_backoff_ms=0,
        ),
        sleep=lambda _s: None,
    )
    assert result.error is None
    assert result.attempts == 2
    assert strict_flags == [False, True]
    assert sched.runs == 2
    assert result.attempt_details[0]["validation_status"] == "schema_validation_failed"


def test_execute_vlm_schema_regeneration_budget_exhausts() -> None:
    sched = _RoutedScheduler()

    def analyze(_req, strict_schema: bool = False):  # type: ignore[no-untyped-def]
        _ = strict_schema
        raise VlmSchemaValidationError("bad schema", raw_response="still bad")

    result = execute_vlm_with_retry(
        request=object(),  # type: ignore[arg-type]
        analyze=analyze,
        scheduler_run=sched.run,
        policy=RetryPolicy(
            max_attempts=1,
            schema_regenerate_max_attempts=2,
            schema_retry_backoff_ms=0,
        ),
        sleep=lambda _s: None,
    )
    assert isinstance(result.error, VlmSchemaValidationError)
    assert result.attempts == 3
    assert sched.runs == 3


def test_vlm_attempt_to_dict_is_compact() -> None:
    d = VlmAttempt(attempt=2, status="failed", error_type="VlmProviderError").to_dict()
    assert d["attempt"] == 2
    assert d["status"] == "failed"
    assert d["error_type"] == "VlmProviderError"


# ---------------------------------------------------------------------------
# DB-write retry hardening (pure)
# ---------------------------------------------------------------------------


def test_is_transient_db_error_detects_lock_and_excludes_integrity() -> None:
    class OperationalError(Exception):
        pass

    class IntegrityError(Exception):
        pass

    assert is_transient_db_error(OperationalError("database is locked")) is True
    assert is_transient_db_error(IntegrityError("unique constraint")) is False
    assert is_transient_db_error(ValueError("totally different")) is False


def test_run_db_write_retries_transient_then_succeeds() -> None:
    class OperationalError(Exception):
        pass

    calls = {"n": 0}

    def write() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("database is locked")
        return "ok"

    out = run_db_write_with_retry(
        write, max_attempts=5, backoff_ms=0, sleep=lambda _s: None
    )
    assert out == "ok"
    assert calls["n"] == 3


def test_run_db_write_reraises_permanent_immediately() -> None:
    class IntegrityError(Exception):
        pass

    calls = {"n": 0}

    def write() -> None:
        calls["n"] += 1
        raise IntegrityError("constraint")

    with pytest.raises(IntegrityError):
        run_db_write_with_retry(write, max_attempts=5, backoff_ms=0, sleep=lambda _s: None)
    assert calls["n"] == 1  # not retried


def test_run_db_write_exhaustion_reraises() -> None:
    class OperationalError(Exception):
        pass

    def write() -> None:
        raise OperationalError("database is locked")

    with pytest.raises(OperationalError):
        run_db_write_with_retry(write, max_attempts=3, backoff_ms=0, sleep=lambda _s: None)


# ---------------------------------------------------------------------------
# Integration harness (mirrors test_frame_extraction_near_eof_fix.py)
# ---------------------------------------------------------------------------


class _StaticProc:
    """Probes a fixed duration and yields N frame URIs per window (>=1)."""

    def __init__(self, duration_ms: int = 30_000, frames: int = 3) -> None:
        self._duration_ms = duration_ms
        self._frames = frames

    def probe(self, source_uri: str):  # type: ignore[no-untyped-def]
        from cctv_memory.services.video_processor import VideoMetadata

        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(self, source_uri, start_ms, end_ms, frame_count, *, unit_key=None):  # type: ignore[no-untyped-def]
        return [f"/tmp/f/{start_ms}_{end_ms}/f{i}.jpg" for i in range(self._frames)]


class _FlakyVlm:
    """Fails the FIRST call with a transient provider error, then always succeeds."""

    def __init__(self) -> None:
        self.calls = 0

    def analyze_segment(self, request, strict_schema: bool = False):  # type: ignore[no-untyped-def]
        _ = strict_schema
        self.calls += 1
        if self.calls == 1:
            raise VlmProviderError("cold start / first call failed")
        return _vlm_output()


class _AlwaysProviderErrorVlm:
    def __init__(self) -> None:
        self.calls = 0

    def analyze_segment(self, request, strict_schema: bool = False):  # type: ignore[no-untyped-def]
        _ = strict_schema
        self.calls += 1
        raise VlmProviderError("provider permanently down")


class _PermanentSchemaVlm:
    def __init__(self) -> None:
        self.calls = 0

    def analyze_segment(self, request, strict_schema: bool = False):  # type: ignore[no-untyped-def]
        _ = strict_schema
        self.calls += 1
        raise VlmSchemaValidationError(
            "schema invalid after adapter budget",
            stage="schema_validation_failed",
            raw_response='{"static":"s"}',
            parsed_payload={"static": "s"},
            validation_errors=[{"loc": ("dynamic",), "msg": "Field required"}],
        )


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


def _submit(runtime, pid: str, key: str) -> str:  # type: ignore[no-untyped-def]
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
                video_start_time=datetime(2026, 6, 15, 15, 0, tzinfo=UTC),
                idempotency_key=key,
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


def _collect(runtime, job_id: str):  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        scale_tasks = repos.scale_task().list_by_job(job_id)
        units = []
        logs = []
        for st in scale_tasks:
            us = repos.analysis_unit().list_by_scale_task(st.scale_task_id)
            units.extend(us)
            for u in us:
                logs.extend(repos.model_call_log().list_by_unit(u.unit_id))
        job = repos.analysis_job().get_job(job_id)
        return job, scale_tasks, units, logs


def _configure_retry(runtime, *, attempts: int) -> None:  # type: ignore[no-untyped-def]
    runtime.config.vlm.unit_max_attempts = attempts
    runtime.config.vlm.retry_backoff_base_ms = 0  # no real sleeping in tests
    runtime.config.vlm.retry_jitter = 0.0


# ---------------------------------------------------------------------------
# default_segment (cross-scale path)
# ---------------------------------------------------------------------------


def test_first_call_fails_then_succeeds_default(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _configure_retry(runtime, attempts=3)
    _seed(runtime, "svc_flaky")
    job_id = _submit(runtime, "svc_flaky", "k_flaky")
    vlm = _FlakyVlm()
    worker = AnalysisWorker(runtime, video_processor=_StaticProc(), vlm=vlm)
    worker.process_one()

    job, _st, units, logs = _collect(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    assert all(u.status is TaskStatus.SUCCEEDED for u in units)
    assert job.job_status is JobStatus.SUCCEEDED
    # The flaky failure happened on the very first unit's first attempt.
    failed_logs = [m for m in logs if m.status == ModelCallStatus.FAILED]
    success_logs = [m for m in logs if m.status == ModelCallStatus.SUCCEEDED]
    assert len(failed_logs) >= 1
    assert any(m.attempt_count == 1 for m in failed_logs)
    # The unit that retried recorded a success with attempt_count == 2.
    assert any(m.attempt_count == 2 for m in success_logs)
    # Exactly-once: each successful unit produced unique record ids.
    produced = [rid for u in units for rid in u.produced_record_ids]
    assert len(produced) == len(set(produced))


def test_all_attempts_fail_default_unit_failed_no_running(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _configure_retry(runtime, attempts=3)
    _seed(runtime, "svc_down")
    job_id = _submit(runtime, "svc_down", "k_down")
    vlm = _AlwaysProviderErrorVlm()
    worker = AnalysisWorker(runtime, video_processor=_StaticProc(), vlm=vlm)
    worker.process_one()

    job, scale_tasks, units, logs = _collect(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    assert units and all(u.status is TaskStatus.FAILED for u in units)
    assert all(u.last_error_code == "vlm_provider_error" for u in units)
    # Each unit attempted exactly 3 times => 3 failed ModelCallLogs per unit.
    per_unit_failed = {}
    for m in logs:
        if m.status == ModelCallStatus.FAILED:
            per_unit_failed.setdefault(m.unit_id, []).append(m.attempt_count)
    for unit in units:
        assert sorted(per_unit_failed[unit.unit_id]) == [1, 2, 3]
        assert unit.attempt_count == 3
    # required default produced nothing => job failed.
    assert job.job_status is JobStatus.FAILED
    assert all(st.status is not TaskStatus.RUNNING for st in scale_tasks)


def test_schema_error_regenerates_then_fails_default(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    _configure_retry(runtime, attempts=4)
    _seed(runtime, "svc_perm")
    job_id = _submit(runtime, "svc_perm", "k_perm")
    vlm = _PermanentSchemaVlm()
    worker = AnalysisWorker(runtime, video_processor=_StaticProc(), vlm=vlm)
    worker.process_one()

    job, _st, units, logs = _collect(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    assert units and all(u.status is TaskStatus.FAILED for u in units)
    assert all(u.last_error_code == "vlm_schema_validation_failed" for u in units)
    # Schema regeneration is a scheduler-routed model attempt, separate from
    # transient provider retry. Default budget is one regeneration.
    for unit in units:
        unit_logs = [m for m in logs if m.unit_id == unit.unit_id]
        assert len(unit_logs) == 2
        assert [log.attempt_count for log in unit_logs] == [1, 2]
        for log in unit_logs:
            assert log.raw_text_output == '{"static":"s"}'
            assert log.parsed_output == {"static": "s"}
            assert log.validation_status == "schema_validation_failed"
            assert log.attempt_details[0]["schema_details"]["validation_errors"]
        assert unit.attempt_count == 2
    assert vlm.calls == len(units) * 2


def test_idempotent_active_records_after_retry_success(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Retry-driven success yields exactly one active ObservationRecord per segment."""
    runtime = runtime_factory()
    _configure_retry(runtime, attempts=3)
    _seed(runtime, "svc_idem")
    job_id = _submit(runtime, "svc_idem", "k_idem")
    worker = AnalysisWorker(runtime, video_processor=_StaticProc(), vlm=_FlakyVlm())
    worker.process_one()
    _job1, _st1, units1, _logs1 = _collect(runtime, job_id)

    from cctv_memory.infrastructure.db.models import tables as orm

    with runtime.session() as session:
        rows = session.query(orm.ObservationRecord).all()
        # Exactly-once at the active-record natural key (video, start, end, scale):
        keys = [
            (r.video_id, r.segment_start_ms, r.segment_end_ms, r.analysis_scale)
            for r in rows
        ]
    assert len(keys) == len(set(keys)), "duplicate active records for a segment"
    # one active record per successful unit.
    succeeded = [u for u in units1 if u.status is TaskStatus.SUCCEEDED]
    assert len(keys) == len(succeeded)


# ---------------------------------------------------------------------------
# high_freq_event + sequential path
# ---------------------------------------------------------------------------


def test_first_call_fails_then_succeeds_sequential(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    runtime.config.pipeline.cross_scale.enabled = False
    _configure_retry(runtime, attempts=3)
    _seed(runtime, "svc_seq")
    job_id = _submit(runtime, "svc_seq", "k_seq_retry")
    vlm = _FlakyVlm()
    worker = AnalysisWorker(runtime, video_processor=_StaticProc(), vlm=vlm)
    worker.process_one()

    job, _st, units, logs = _collect(runtime, job_id)
    assert not any(u.status is TaskStatus.RUNNING for u in units)
    assert all(u.status is TaskStatus.SUCCEEDED for u in units)
    assert job.job_status is JobStatus.SUCCEEDED
    assert any(
        m.status == ModelCallStatus.FAILED and m.attempt_count == 1 for m in logs
    )
