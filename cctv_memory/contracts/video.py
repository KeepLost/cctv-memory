"""Camera and video source contracts (schema-contracts §3.1-§3.3, §5.1-§5.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import SecurityLevel, SourceType


class CameraLocation(ContractModel):
    """Physical space hosting cameras (schema-contracts §3.1)."""

    location_id: str
    tenant_id: str = "tenant_default"
    building: str | None = None
    floor: str | None = None
    area: str
    room_or_zone: str | None = None
    location_desc: str | None = None
    access_policy_id: str | None = None
    security_level: SecurityLevel = SecurityLevel.INTERNAL
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CameraDevice(ContractModel):
    """Camera device (schema-contracts §3.2)."""

    camera_id: str
    tenant_id: str = "tenant_default"
    camera_name: str
    location_id: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    install_position_desc: str | None = None
    stream_uri: str | None = None
    access_policy_id: str | None = None
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class VideoSource(ContractModel):
    """Video source / chunk (schema-contracts §3.3).

    ``source_uri`` is internal and must never be exposed to external callers
    (ARCHITECTURE_CONSTITUTION §5).
    """

    video_id: str
    tenant_id: str = "tenant_default"
    source_type: SourceType
    source_uri: str
    original_source_uri: str | None = None
    camera_id: str
    video_start_time: datetime
    video_end_time: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    source_status: str = "pending"
    external_source_id: str | None = None
    access_policy_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SubmitVideoSourceRequest(ContractModel):
    """Submit-video-source request (schema-contracts §5.1).

    Identity is carried in headers, never in the body
    (api-and-service-runtime-design §2.2).
    """

    source_type: SourceType
    source_uri: str
    camera_id: str
    video_start_time: datetime
    external_source_id: str | None = None
    idempotency_key: str | None = None
    analysis_options: dict[str, bool] = Field(default_factory=dict)


class SubmitVideoSourceResponse(ContractModel):
    """Submit-video-source response (schema-contracts §5.2)."""

    video_id: str
    source_status: str
    analysis_job_id: str
    accepted: bool
