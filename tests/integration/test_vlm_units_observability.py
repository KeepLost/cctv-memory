"""VLM unit scheduling / observability regression tests.

These tests are intentionally written against the desired main-path behavior from
status/task-spec.md (2026-06-11 VLM unit scheduling task). They should fail until
the implementation adds real unit state, per-unit publication, safe provider
options, and model-call logs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
)
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.infrastructure.vlm.real_adapter import RealVlmAnalyzer
from cctv_memory.workers.analysis_worker import AnalysisWorker

from tests.conftest import seed_camera


class _FailSecondVlm:
    """VLM fake that succeeds first unit and fails every later unit."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def analyze_segment(self, request):  # type: ignore[no-untyped-def]
        self.calls.append((request.segment_start_ms, request.segment_end_ms))
        if len(self.calls) >= 2:
            raise RuntimeError("boom after first unit")
        return VlmObservationOutput(
            static="unit one static",
            dynamic="unit one dynamic",
            tags=["person"],
            quality={"reason": "", "score": 0.9},
            attr={"alert": False},
        )


def _seed(runtime, principal_id: str = "svc_units") -> None:  # type: ignore[no-untyped-def]
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
                principal_id=principal_id,
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )


def _submit(runtime, *, principal_id: str = "svc_units") -> str:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        principal = repos.principal().get_principal(principal_id)
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 11, 14, 0, tzinfo=UTC),
                idempotency_key="units-1",
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


def test_config_exposes_vlm_provider_options_and_scheduler_defaults() -> None:
    cfg = AppConfig()

    assert cfg.vlm.extra_body == {}
    assert cfg.vlm.max_concurrent_requests == 1
    assert cfg.vlm.min_request_interval_ms == 0
    assert cfg.vlm.media_log_mode == "metadata_only"
    assert cfg.vlm.debug_media_retention is False


def test_real_adapter_merges_allowed_extra_body_and_rejects_core_overrides(
    tmp_path: Path,
) -> None:
    media = tmp_path / "frame.jpg"
    media.write_bytes(b"fake-frame")
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "static": "s",
                                    "dynamic": "d",
                                    "tags": [],
                                    "quality": {"reason": "", "score": 0.0},
                                    "attr": {"alert": False},
                                }
                            )
                        }
                    }
                ]
            },
        )

    adapter = RealVlmAnalyzer(
        base_url="http://fake/api",
        api_key="k",
        model_id="authoritative-model",
        extra_body={
            "temperature": 0,
            "top_p": 0.2,
            "messages": "must-not-override",
            "model": "must-not-override",
        },
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    from cctv_memory.contracts.vlm import VlmSegmentRequest

    adapter.analyze_segment(
        VlmSegmentRequest(
            request_id="vlm_req_test",
            analysis_job_id="job_units",
            video_id="video_units",
            camera_id="cam_lobby_01",
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            segment_start_ms=0,
            segment_end_ms=1000,
            frame_uris=[str(media)],
        )
    )

    payload = captured[0]
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0.2
    assert payload["model"] == "authoritative-model"
    assert isinstance(payload["messages"], list)


def test_unit_repository_is_idempotent_and_model_call_log_excludes_inline_media(
    factory,
) -> None:  # type: ignore[no-untyped-def]
    unit_repo = factory.analysis_unit()
    log_repo = factory.model_call_log()

    from cctv_memory.contracts.analysis import AnalysisUnit, ModelCallLog

    unit = AnalysisUnit(
        unit_id="unit_1",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        video_id="video_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        segment_start_ms=0,
        segment_end_ms=12000,
        window_index=0,
        idempotency_key="job_1:scale_1:default_segment:0:12000",
    )

    created = unit_repo.create_or_get_by_idempotency(unit)
    duplicate = unit_repo.create_or_get_by_idempotency(
        unit.model_copy(update={"unit_id": "unit_2"})
    )
    assert duplicate.unit_id == created.unit_id

    log = ModelCallLog(
        model_call_id="mcall_1",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        unit_id=created.unit_id,
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=12000,
        provider="real",
        model_id="m",
        prompt_version="p",
        pipeline_version="v1",
        status="succeeded",
        attempt_count=1,
        raw_text_input="prompt text",
        raw_text_output="response text",
        media_refs=[{"uri": "artifact://frame1", "mime": "image/jpeg", "sha256": "abc"}],
    )
    log_repo.create_log(log)
    loaded = log_repo.get_log("mcall_1")
    assert loaded is not None
    assert loaded.raw_text_input == "prompt text"
    assert "base64" not in json.dumps(loaded.media_refs).lower()


def test_default_segment_publishes_first_unit_and_marks_later_failure_partial(
    runtime_factory,
) -> None:  # type: ignore[no-untyped-def]
    runtime = runtime_factory()
    cfg = runtime.config
    cfg.pipeline.default_segment.window_seconds = 10
    cfg.pipeline.default_segment.overlap_seconds = 0
    _seed(runtime)
    job_id = _submit(runtime)

    worker = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=25_000),
        vlm=_FailSecondVlm(),
    )
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.PARTIAL_FAILED
        scale = repos.scale_task().get_by_job_and_scale(
            job_id, AnalysisScale.DEFAULT_SEGMENT.value
        )
        assert scale is not None
        assert scale.status is TaskStatus.PARTIAL_FAILED
        assert scale.total_units == 3
        assert scale.succeeded_units == 1
        assert scale.failed_units == 2
        units = repos.analysis_unit().list_by_scale_task(scale.scale_task_id)
        assert [u.status for u in units].count(TaskStatus.SUCCEEDED) == 1
        assert [u.status for u in units].count(TaskStatus.FAILED) == 2
        logs = repos.model_call_log().list_by_unit(units[0].unit_id)
        assert logs

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        active_count = session.scalar(select(func.count()).select_from(orm.ObservationRecord))
        assert active_count == 1

    runtime.dispose()
