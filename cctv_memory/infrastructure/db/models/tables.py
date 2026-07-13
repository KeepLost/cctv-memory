"""SQLAlchemy ORM models matching status/table-schema-spec.md.

Timestamp and JSON columns use the canonical-type + per-backend physical mapping
(table-schema-spec §1.1bis, database-adapter-contract §4.0):

- ``_TS()``    -> SQLite ``String`` (ISO-8601 TEXT), PostgreSQL ``TIMESTAMPTZ``
- ``_JSONB()`` -> SQLite ``Text`` (JSON text),       PostgreSQL ``JSONB``

The base (SQLite) type is preserved, so ``Base.metadata.create_all`` still emits
TEXT columns and the SQLite physical schema is unchanged. The PostgreSQL variant
makes the ORM honest: if an inherited write path ever flushes one of these
columns through the ORM on PostgreSQL, SQLAlchemy renders the native
TIMESTAMPTZ/JSONB type instead of ``::VARCHAR``/``::TEXT`` (the bug class behind
the task-queue terminal-write failure). The authoritative PostgreSQL DDL is
still ``infrastructure/db/postgres/schema.py`` (never ORM ``create_all``).

``observation_vectors.embedding`` is intentionally NOT varianted: the contract
type is ``list[float]`` and the PostgreSQL physical type is ``vector(N)`` with a
dynamic dimension, handled only by the index adapter's explicit SQL.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Float as SAFloat,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from cctv_memory.infrastructure.db.models.base import Base

_TENANT_DEFAULT = "tenant_default"


def _TS() -> TypeEngine[str]:
    """Canonical timestamp column type: SQLite TEXT, PostgreSQL TIMESTAMPTZ."""
    return String().with_variant(TIMESTAMP(timezone=True), "postgresql")


def _JSONB() -> TypeEngine[str]:
    """Canonical JSON column type: SQLite TEXT, PostgreSQL JSONB."""
    return Text().with_variant(JSONB(), "postgresql")


class SchemaMetadata(Base):
    __tablename__ = "schema_metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class Principal(Base):
    __tablename__ = "principals"

    principal_id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_type: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default=_TENANT_DEFAULT)
    external_subject_id: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    roles_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    groups_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        Index("idx_principals_tenant_status", "tenant_id", "status"),
        Index("idx_principals_external_subject", "external_subject_id"),
    )


class AccessPolicy(Base):
    __tablename__ = "access_policies"

    access_policy_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default=_TENANT_DEFAULT)
    name: Mapped[str] = mapped_column(String, nullable=False)
    security_level: Mapped[str] = mapped_column(String, nullable=False)
    rules_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_policy_tenant_name"),)


class CameraLocation(Base):
    __tablename__ = "camera_locations"

    location_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default=_TENANT_DEFAULT)
    building: Mapped[str | None] = mapped_column(String, nullable=True)
    floor: Mapped[str | None] = mapped_column(String, nullable=True)
    area: Mapped[str] = mapped_column(String, nullable=False)
    room_or_zone: Mapped[str | None] = mapped_column(String, nullable=True)
    location_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_policy_id: Mapped[str | None] = mapped_column(String, nullable=True)
    security_level: Mapped[str] = mapped_column(String, nullable=False, default="internal")
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        Index("idx_locations_policy", "access_policy_id", "security_level"),
        Index("idx_locations_area", "area"),
    )


class CameraDevice(Base):
    __tablename__ = "camera_devices"

    camera_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default=_TENANT_DEFAULT)
    camera_name: Mapped[str] = mapped_column(String, nullable=False)
    location_id: Mapped[str] = mapped_column(String, nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String, nullable=True)
    install_position_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    access_policy_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        Index("idx_camera_location", "location_id"),
        Index("idx_camera_policy", "access_policy_id"),
        Index("idx_camera_status", "status"),
    )


class VideoSource(Base):
    __tablename__ = "video_sources"

    video_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default=_TENANT_DEFAULT)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_uri: Mapped[str] = mapped_column(String, nullable=False)
    original_source_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    camera_id: Mapped[str] = mapped_column(String, nullable=False)
    video_start_time: Mapped[str] = mapped_column(_TS(), nullable=False)
    video_end_time: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_status: Mapped[str] = mapped_column(String, nullable=False)
    external_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    access_policy_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        UniqueConstraint("camera_id", "video_start_time", name="uq_video_camera_starttime"),
        Index("idx_video_camera_time", "camera_id", "video_start_time", "video_end_time"),
        Index("idx_video_policy", "access_policy_id"),
        Index("idx_video_status", "source_status"),
    )


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    analysis_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    video_id: Mapped[str] = mapped_column(String, nullable=False)
    job_status: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    analysis_options_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_record_ids_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    updated_record_ids_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    archived_record_ids_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    failed_segment_ids_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    started_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency"),
        Index("idx_jobs_video", "video_id"),
        Index("idx_jobs_status", "job_status", "created_at"),
    )


class AnalysisScaleTask(Base):
    __tablename__ = "analysis_scale_tasks"

    scale_task_id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_scale: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    started_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("analysis_job_id", "analysis_scale", name="uq_scaletask_job_scale"),
    )


class HighFreqTrigger(Base):
    __tablename__ = "high_freq_triggers"

    trigger_id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    scale_task_id: Mapped[str] = mapped_column(String, nullable=False)
    video_id: Mapped[str] = mapped_column(String, nullable=False)
    trigger_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    motion_score: Mapped[float | None] = mapped_column(SAFloat, nullable=True)
    change_score: Mapped[float | None] = mapped_column(SAFloat, nullable=True)
    trigger_reason: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("trigger_start_ms < trigger_end_ms", name="ck_trigger_time_order"),
        UniqueConstraint("idempotency_key", name="uq_trigger_idempotency"),
    )


class AnalysisUnit(Base):
    __tablename__ = "analysis_units"

    unit_id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    scale_task_id: Mapped[str] = mapped_column(String, nullable=False)
    video_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_scale: Mapped[str] = mapped_column(String, nullable=False)
    unit_kind: Mapped[str] = mapped_column(String, nullable=False)
    segment_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_model_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    successful_model_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    produced_record_ids_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    started_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)

    __table_args__ = (
        CheckConstraint("segment_start_ms < segment_end_ms", name="ck_unit_time_order"),
        UniqueConstraint("idempotency_key", name="uq_analysis_unit_idempotency"),
        Index("idx_units_scale_status", "scale_task_id", "status"),
        Index("idx_units_job_scale", "analysis_job_id", "analysis_scale"),
        # Backs the bounded orphan-running sweep (task cctv-memory-20260612-1854):
        # status='running' AND started_at < cutoff ORDER BY started_at LIMIT batch.
        Index("idx_units_status_started", "status", "started_at"),
    )


class ModelCallLog(Base):
    __tablename__ = "model_call_logs"

    model_call_id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    scale_task_id: Mapped[str] = mapped_column(String, nullable=False)
    unit_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_scale: Mapped[str] = mapped_column(String, nullable=False)
    segment_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_output_json: Mapped[str | None] = mapped_column(_JSONB(), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    media_refs_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    attempt_details_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    started_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        Index("idx_model_calls_unit", "unit_id", "created_at"),
        Index("idx_model_calls_job", "analysis_job_id", "analysis_scale"),
    )


class DetectorGateLog(Base):
    __tablename__ = "detector_gate_logs"

    gate_log_id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    scale_task_id: Mapped[str] = mapped_column(String, nullable=False)
    unit_id: Mapped[str] = mapped_column(String, nullable=False)
    video_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_scale: Mapped[str] = mapped_column(String, nullable=False)
    segment_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    decision_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    frame_evidence_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String, nullable=False)
    rule_config_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    media_refs_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    artifact_refs_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    started_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        CheckConstraint("segment_start_ms < segment_end_ms", name="ck_gate_time_order"),
        Index("idx_detector_gate_unit", "unit_id", "created_at"),
        Index("idx_detector_gate_job", "analysis_job_id", "analysis_scale"),
    )


class PreVlmGateLog(Base):
    __tablename__ = "pre_vlm_gate_logs"

    gate_log_id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    scale_task_id: Mapped[str] = mapped_column(String, nullable=False)
    unit_id: Mapped[str] = mapped_column(String, nullable=False)
    video_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_scale: Mapped[str] = mapped_column(String, nullable=False)
    unit_kind: Mapped[str] = mapped_column(String, nullable=False)
    profile_name: Mapped[str] = mapped_column(String, nullable=False)
    segment_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    decision_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    signals_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    frame_evidence_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String, nullable=False)
    rule_config_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    suppression_policy: Mapped[str | None] = mapped_column(String, nullable=True)
    media_refs_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    artifact_refs_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    started_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        CheckConstraint("segment_start_ms < segment_end_ms", name="ck_pre_vlm_gate_time_order"),
        Index("idx_pre_vlm_gate_unit", "unit_id", "created_at"),
        Index("idx_pre_vlm_gate_job", "analysis_job_id", "analysis_scale"),
    )


class AnalysisTimelineEvent(Base):
    __tablename__ = "analysis_timeline_events"

    timeline_event_id: Mapped[str] = mapped_column(String, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String, nullable=False)
    span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scale_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    video_id: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_scale: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    segment_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segment_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_name: Mapped[str] = mapped_column(String, nullable=False)
    event_phase: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="{}")
    metadata_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        Index("idx_timeline_job_time", "analysis_job_id", "occurred_at"),
        Index("idx_timeline_unit_time", "unit_id", "occurred_at"),
        Index("idx_timeline_model_call", "model_call_id", "occurred_at"),
        Index("idx_timeline_trace_time", "trace_id", "occurred_at"),
        Index("idx_timeline_event_name_time", "event_name", "occurred_at"),
    )


class ObservationRecord(Base):
    __tablename__ = "observation_records"

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default=_TENANT_DEFAULT)
    video_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    analysis_scale: Mapped[str] = mapped_column(String, nullable=False)
    segment_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_start_time: Mapped[str] = mapped_column(_TS(), nullable=False)
    observed_end_time: Mapped[str] = mapped_column(_TS(), nullable=False)
    camera_id: Mapped[str] = mapped_column(String, nullable=False)
    location_id: Mapped[str] = mapped_column(String, nullable=False)
    static_description_text: Mapped[str] = mapped_column(Text, nullable=False)
    dynamic_description_text: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    clip_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    thumbnail_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    attributes_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="{}")
    access_policy_id: Mapped[str] = mapped_column(String, nullable=False)
    security_level: Mapped[str] = mapped_column(String, nullable=False)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        CheckConstraint("segment_start_ms < segment_end_ms", name="ck_obs_time_order"),
        UniqueConstraint(
            "video_id",
            "segment_start_ms",
            "segment_end_ms",
            "analysis_scale",
            name="uq_obs_segment_scale",
        ),
        Index("idx_obs_video_time", "video_id", "segment_start_ms", "segment_end_ms"),
        Index("idx_obs_observed_time", "observed_start_time", "observed_end_time"),
        Index("idx_obs_camera_time", "camera_id", "observed_start_time", "observed_end_time"),
        Index("idx_obs_location_time", "location_id", "observed_start_time", "observed_end_time"),
        Index("idx_obs_policy", "access_policy_id", "security_level"),
        Index("idx_obs_scale", "analysis_scale"),
    )


class ObservationRecordHistory(Base):
    __tablename__ = "observation_record_history"

    history_id: Mapped[str] = mapped_column(String, primary_key=True)
    old_record_id: Mapped[str] = mapped_column(String, nullable=False)
    replaced_by_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    archived_by_analysis_job_id: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    archive_reason: Mapped[str] = mapped_column(String, nullable=False)
    record_snapshot_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)


class ObservationVector(Base):
    __tablename__ = "observation_vectors"

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    vector_type: Mapped[str] = mapped_column(String, primary_key=True)
    embedding: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="{}")


class SearchContext(Base):
    __tablename__ = "search_contexts"

    context_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    authorized_scope_hash: Mapped[str] = mapped_column(String, nullable=False)
    dataset_revision: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    default_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    last_accessed_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    expires_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)


class SearchRevision(Base):
    __tablename__ = "search_revisions"

    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    context_id: Mapped[str] = mapped_column(String, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    op: Mapped[str] = mapped_column(String, nullable=False)
    op_params_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    facets_json: Mapped[str | None] = mapped_column(_JSONB(), nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)


class SearchCandidate(Base):
    __tablename__ = "search_candidates"

    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(SAFloat, nullable=False)
    score_detail_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)

    __table_args__ = (Index("idx_candidates_revision_rank", "revision_id", "rank"),)


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(_JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_run_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    updated_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_tasks_claim", "status", "next_run_at", "priority"),
        Index("idx_tasks_lease", "lease_expires_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    audit_event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    principal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    context_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resource_scope_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    record_ids_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="[]")
    video_id: Mapped[str | None] = mapped_column(String, nullable=True)
    camera_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str] = mapped_column(_JSONB(), nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)

    __table_args__ = (
        Index("idx_audit_principal_time", "principal_id", "created_at"),
        Index("idx_audit_event_type_time", "event_type", "created_at"),
        Index("idx_audit_request", "request_id"),
    )


class BackupJob(Base):
    __tablename__ = "backup_jobs"

    backup_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    backup_type: Mapped[str] = mapped_column(String, nullable=False)
    principal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    manifest_json: Mapped[str | None] = mapped_column(_JSONB(), nullable=True)
    created_at: Mapped[str] = mapped_column(_TS(), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(_TS(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
