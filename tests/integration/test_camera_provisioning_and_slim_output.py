"""Tests for task cctv-memory-20260611-2214: lenient camera_id provisioning +
slim VLM output format (static/dynamic/tags/quality/attr).

Fix 1: an externally supplied camera_id that was never pre-registered must NOT be
rejected — analysis lazily provisions a minimal CameraDevice/CameraLocation and
proceeds, with policy/security still system-derived.

Fix 2: the slim VLM output maps onto the (unchanged) ObservationRecord columns;
quality + attr.alert ride in the attributes JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmAttr, VlmObservationOutput, VlmQuality
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.infrastructure.vlm.mock_adapter import MockVlmAnalyzer
from cctv_memory.infrastructure.vlm.prompts import prompt_version_for_scale


def _seed_policy_and_principal(repos, principal_id: str) -> None:  # type: ignore[no-untyped-def]
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


def test_unregistered_camera_id_is_not_rejected(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """A brand-new camera_id (no CameraDevice row) must analyze successfully."""
    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    runtime = runtime_factory()
    new_camera_id = "cam_brand_new_42"
    with runtime.session() as session:
        repos = runtime.repositories(session)
        # NOTE: deliberately do NOT seed any camera/location for new_camera_id.
        _seed_policy_and_principal(repos, "svc_newcam")
        principal = repos.principal().get_principal("svc_newcam")
        assert principal is not None
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/new.mp4",
                camera_id=new_camera_id,
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key="k_newcam",
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        job_id = resp.analysis_job_id

    worker = AnalysisWorker(runtime, video_processor=StaticVideoProcessor(duration_ms=30_000))
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        # The job must NOT fail just because the camera was unregistered.
        assert job.job_status is JobStatus.SUCCEEDED, f"job failed: {job.error_code}"
        # The camera was lazily provisioned under the auto-location.
        camera = repos.camera().get_camera(new_camera_id)
        assert camera is not None
        assert camera.camera_id == new_camera_id
        location = repos.camera().get_location(camera.location_id)
        assert location is not None
        # Records carry the externally supplied camera_id.
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        rows = list(session.scalars(select(orm.ObservationRecord)))
        assert rows
        assert all(r.camera_id == new_camera_id for r in rows)
    runtime.dispose()


def test_provisioning_is_idempotent_across_two_videos(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """Two videos from the same unregistered camera reuse one provisioned device."""
    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    runtime = runtime_factory()
    cam = "cam_shared_unreg"
    with runtime.session() as session:
        repos = runtime.repositories(session)
        _seed_policy_and_principal(repos, "svc_shared")
        principal = repos.principal().get_principal("svc_shared")
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
        )
        for i in range(2):
            ingestion.submit(
                SubmitVideoSourceRequest(
                    source_type=SourceType.FILE,
                    source_uri=f"/data/videos/v{i}.mp4",
                    camera_id=cam,
                    video_start_time=datetime(2026, 6, 6, 21, i, tzinfo=UTC),
                    idempotency_key=f"k_shared_{i}",
                ),
                principal,
                capabilities=[Capability.ANALYSIS_SUBMIT],
            )

    worker = AnalysisWorker(runtime, video_processor=StaticVideoProcessor(duration_ms=20_000))
    while worker.process_one() is not None:
        pass

    with runtime.session() as session:
        repos = runtime.repositories(session)
        cameras = repos.camera().list_cameras().items
        assert sum(1 for c in cameras if c.camera_id == cam) == 1
    runtime.dispose()


def test_build_observation_record_maps_slim_output() -> None:
    """static/dynamic map to *_description_text; quality + alert ride in attributes."""
    from cctv_memory.contracts.video import (
        CameraDevice,
        CameraLocation,
        VideoSource,
    )
    from cctv_memory.workers.common import VideoContext, build_observation_record

    ctx = VideoContext(
        source=VideoSource(
            video_id="video_1",
            source_type=SourceType.FILE,
            source_uri="/x.mp4",
            camera_id="cam_x",
            video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
        ),
        camera=CameraDevice(camera_id="cam_x", camera_name="x", location_id="loc_x"),
        location=CameraLocation(location_id="loc_x", area="a"),
        access_policy_id="policy_public_area",
        security_level=SecurityLevel.INTERNAL,
    )
    output = VlmObservationOutput(
        static="a person near the door",
        dynamic="the person walks away",
        tags=["person", "walking"],
        quality=VlmQuality(reason="backpack color unclear", score=0.7),
        attr=VlmAttr(alert=True),
    )
    record = build_observation_record(
        ctx=ctx,
        analysis_job_id="job_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=12_000,
        output=output,
        model_version="m",
        prompt_version="default_segment_v3",
        pipeline_version="pipeline-v1",
    )
    assert record.static_description_text == "a person near the door"
    assert record.dynamic_description_text == "the person walks away"
    assert record.tags == ["person", "walking"]
    # quality + alert are carried in attributes (DB schema unchanged).
    assert record.attributes["alert"] is True
    assert record.attributes["quality"]["reason"] == "backpack color unclear"
    assert record.attributes["quality"]["score"] == 0.7
    # System-derived fields are NOT taken from VLM output.
    assert record.access_policy_id == "policy_public_area"
    assert record.security_level is SecurityLevel.INTERNAL


def test_mock_adapter_emits_slim_format() -> None:
    from cctv_memory.contracts.vlm import VlmSegmentRequest

    out = MockVlmAnalyzer().analyze_segment(
        VlmSegmentRequest(
            request_id="r",
            analysis_job_id="j",
            video_id="v",
            camera_id="cam_x",
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            segment_start_ms=0,
            segment_end_ms=1000,
            frame_uris=["f0"],
        )
    )
    dumped = out.model_dump()
    assert set(dumped.keys()) == {"static", "dynamic", "tags", "quality", "attr"}
    assert dumped["attr"] == {"alert": False}
    assert set(dumped["quality"].keys()) == {"reason", "score"}
    # Removed legacy fields are gone.
    for legacy in ("schema_version", "uncertainties", "attributes"):
        assert legacy not in dumped


def test_prompt_versions_bumped_to_v2() -> None:
    assert prompt_version_for_scale(AnalysisScale.DEFAULT_SEGMENT) == "default_segment_v3"
    assert prompt_version_for_scale(AnalysisScale.HIGH_FREQ_EVENT) == "high_freq_event_v3"
