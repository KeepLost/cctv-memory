"""Shared worker helpers (workers/common.py).

Pure orchestration glue reused by the scale processors (default_segment,
high_freq_event): resolve the SYSTEM-derived permission/timing metadata for a
video and build validated ``ObservationRecord``s. System-derived metadata
(camera/location/access_policy/security_level/observed times) comes from
repositories + domain policy, NEVER from VLM output (ARCHITECTURE_CONSTITUTION
§5, vlm-analysis-contract §4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.video import CameraDevice, CameraLocation, VideoSource
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain import policies
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel
from cctv_memory.domain.exceptions import NotFoundError
from cctv_memory.repositories.camera import CameraRepository
from cctv_memory.repositories.principal import AccessPolicyRepository
from cctv_memory.repositories.video_source import VideoSourceRepository

DEFAULT_POLICY_ID = "policy_public_area"
# Location auto-created to host cameras that arrive via analysis requests without
# prior registration (lenient camera_id provisioning, see resolve_video_context).
# The constant + placeholder factory live in domain/policies so the application
# seed path can pre-create the same row without a workers->application dependency.
AUTO_LOCATION_ID = policies.AUTO_LOCATION_ID


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class VideoContext:
    """Resolved video + SYSTEM-derived permission/identity metadata for a job."""

    source: VideoSource
    camera: CameraDevice
    location: CameraLocation
    access_policy_id: str
    security_level: SecurityLevel


def resolve_video_context(
    video_id: str,
    *,
    video_sources: VideoSourceRepository,
    cameras: CameraRepository,
    policies_repo: AccessPolicyRepository,
    default_policy_id: str = DEFAULT_POLICY_ID,
) -> VideoContext:
    """Resolve source/camera/location and the system-derived policy/security level.

    ``camera_id`` is an external fact carried on the VideoSource; it must NOT be
    rejected just because the device was never pre-registered. When the camera (or
    its location) is missing we lazily provision a minimal CameraDevice/
    CameraLocation under the default policy so analysis proceeds. This is lenient
    provisioning, NOT camera management: identity/policy/security stay
    system-derived (ARCHITECTURE_CONSTITUTION §5) and VLM never influences them.
    """
    source = video_sources.get_by_id(video_id)
    if source is None:
        raise NotFoundError(f"video {video_id} not found")
    camera = cameras.get_camera(source.camera_id)
    if camera is None:
        camera = _provision_camera(source.camera_id, cameras=cameras)
    location = cameras.get_location(camera.location_id)
    if location is None:
        location = _provision_location(camera.location_id, cameras=cameras)
    policy_id = policies.resolve_access_policy_id(
        location, camera, source.access_policy_id, default_policy_id
    )
    policy = policies_repo.get_access_policy(policy_id)
    security_level = policies.effective_security_level(location, camera, policy)
    return VideoContext(
        source=source,
        camera=camera,
        location=location,
        access_policy_id=policy_id,
        security_level=security_level,
    )


def _provision_location(
    location_id: str, *, cameras: CameraRepository
) -> CameraLocation:
    """Idempotently create a minimal placeholder location for unregistered cameras."""
    location = CameraLocation(
        location_id=location_id,
        area="unregistered",
        location_desc="Auto-created for an unregistered camera",
        security_level=SecurityLevel.INTERNAL,
    )
    return cameras.upsert_location(location)


def _provision_camera(camera_id: str, *, cameras: CameraRepository) -> CameraDevice:
    """Idempotently create a minimal CameraDevice for an externally supplied camera_id.

    The placeholder hangs off a shared auto-location so location_id (a required
    ObservationRecord field) is always resolvable. Access policy/security level are
    still derived via the normal inheritance chain (default policy here).
    """
    # Ensure the placeholder location exists before referencing it.
    if cameras.get_location(AUTO_LOCATION_ID) is None:
        _provision_location(AUTO_LOCATION_ID, cameras=cameras)
    camera = CameraDevice(
        camera_id=camera_id,
        camera_name=camera_id,
        location_id=AUTO_LOCATION_ID,
        status="active",
    )
    return cameras.upsert_camera(camera)


def build_observation_record(
    *,
    ctx: VideoContext,
    analysis_job_id: str,
    analysis_scale: AnalysisScale,
    segment_start_ms: int,
    segment_end_ms: int,
    output: VlmObservationOutput,
    model_version: str | None,
    prompt_version: str | None,
    pipeline_version: str | None,
    extra_attributes: dict[str, Any] | None = None,
) -> ObservationRecord:
    """Build a validated ObservationRecord with system-derived metadata attached."""
    observed_start = ctx.source.video_start_time + timedelta(milliseconds=segment_start_ms)
    observed_end = ctx.source.video_start_time + timedelta(milliseconds=segment_end_ms)
    # Map the slim VLM output onto the (unchanged) ObservationRecord columns:
    # static/dynamic -> *_description_text; quality + attr.alert are carried in the
    # attributes JSON so the DB schema needs no migration (task 2214 scope).
    attributes: dict[str, Any] = {
        "quality": output.quality.model_dump(),
        "alert": output.attr.alert,
    }
    if extra_attributes:
        attributes.update(extra_attributes)
    return ObservationRecord(
        record_id=new_id("obs"),
        tenant_id=ctx.source.tenant_id,
        video_id=ctx.source.video_id,
        analysis_job_id=analysis_job_id,
        analysis_scale=analysis_scale,
        segment_start_ms=segment_start_ms,
        segment_end_ms=segment_end_ms,
        observed_start_time=observed_start,
        observed_end_time=observed_end,
        camera_id=ctx.source.camera_id,
        location_id=ctx.camera.location_id,
        static_description_text=output.static,
        dynamic_description_text=output.dynamic,
        tags=output.tags,
        attributes=attributes,
        access_policy_id=ctx.access_policy_id,
        security_level=ctx.security_level,
        model_version=model_version,
        prompt_version=prompt_version,
        pipeline_version=pipeline_version,
    )


def build_detector_only_observation_record(
    *,
    ctx: VideoContext,
    analysis_job_id: str,
    analysis_scale: AnalysisScale,
    segment_start_ms: int,
    segment_end_ms: int,
    model_version: str | None,
    prompt_version: str | None,
    pipeline_version: str | None,
    detector_gate_summary: dict[str, Any],
) -> ObservationRecord:
    """Build a detector-only record with empty text/tags and attr evidence summary."""
    observed_start = ctx.source.video_start_time + timedelta(milliseconds=segment_start_ms)
    observed_end = ctx.source.video_start_time + timedelta(milliseconds=segment_end_ms)
    return ObservationRecord(
        record_id=new_id("obs"),
        tenant_id=ctx.source.tenant_id,
        video_id=ctx.source.video_id,
        analysis_job_id=analysis_job_id,
        analysis_scale=analysis_scale,
        segment_start_ms=segment_start_ms,
        segment_end_ms=segment_end_ms,
        observed_start_time=observed_start,
        observed_end_time=observed_end,
        camera_id=ctx.source.camera_id,
        location_id=ctx.camera.location_id,
        static_description_text="",
        dynamic_description_text="",
        tags=[],
        attributes={"detector_gate": detector_gate_summary},
        access_policy_id=ctx.access_policy_id,
        security_level=ctx.security_level,
        model_version=model_version,
        prompt_version=prompt_version,
        pipeline_version=pipeline_version,
    )


def video_end_iso(source: VideoSource, duration_ms: int) -> tuple[datetime, str]:
    """Return the video end datetime + ISO string for a probed duration."""
    end = source.video_start_time + timedelta(milliseconds=duration_ms)
    return end, end.isoformat()
