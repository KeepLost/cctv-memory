"""Pure ORM <-> contract-DTO mappers (infrastructure-internal).

Keeps SQLAlchemy ORM objects from ever leaking to application/domain. DTOs use
the canonical domain types (``datetime`` for timestamps, ``dict``/``list`` for
JSON); these mappers convert at the adapter boundary. The SQLite physical schema
stores timestamps as ISO-8601 text and JSON as serialized text, so the helpers
accept both shapes: ``_dt`` takes ``str | datetime`` and ``_loads_obj`` /
``_loads_list`` take ``str | dict/list`` (PostgreSQL TIMESTAMPTZ/JSONB return
``datetime``/``dict``). See table-schema-spec §1.1bis and
database-adapter-contract §4.0 for the canonical-type + per-backend mapping rule.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from cctv_memory.contracts.analysis import (
    AnalysisJob,
    AnalysisScaleTask,
    AnalysisUnit,
    DetectorGateLog,
    HighFreqTrigger,
    ModelCallLog,
)
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import (
    AccessPolicy,
    AccessPolicyRules,
    Principal,
)
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pre_vlm_gate import PreVlmGateLog
from cctv_memory.contracts.search import (
    SearchCandidate,
    SearchContext,
    SearchRevision,
)
from cctv_memory.contracts.task import Task
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.contracts.video import CameraDevice, CameraLocation, VideoSource
from cctv_memory.domain.enums import (
    AnalysisScale,
    ContextMode,
    JobStatus,
    ModelCallStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
    TaskStatus,
    TriggerStatus,
)
from cctv_memory.infrastructure.db.models import tables as orm


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value) if value else None


def _loads_list(value: str | list[Any]) -> list[Any]:
    if isinstance(value, list):
        return value
    result: list[Any] = json.loads(value)
    return result


def _loads_obj(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    result: dict[str, Any] = json.loads(value)
    return result


# --- CameraLocation -------------------------------------------------------


def location_to_orm(dto: CameraLocation) -> orm.CameraLocation:
    now = datetime.now().astimezone().isoformat()
    return orm.CameraLocation(
        location_id=dto.location_id,
        tenant_id=dto.tenant_id,
        building=dto.building,
        floor=dto.floor,
        area=dto.area,
        room_or_zone=dto.room_or_zone,
        location_desc=dto.location_desc,
        access_policy_id=dto.access_policy_id,
        security_level=dto.security_level.value,
        created_at=_iso(dto.created_at) or now,
        updated_at=_iso(dto.updated_at) or now,
    )


def location_to_dto(row: orm.CameraLocation) -> CameraLocation:
    return CameraLocation(
        location_id=row.location_id,
        tenant_id=row.tenant_id,
        building=row.building,
        floor=row.floor,
        area=row.area,
        room_or_zone=row.room_or_zone,
        location_desc=row.location_desc,
        access_policy_id=row.access_policy_id,
        security_level=SecurityLevel(row.security_level),
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


# --- CameraDevice ---------------------------------------------------------


def camera_to_orm(dto: CameraDevice) -> orm.CameraDevice:
    now = datetime.now().astimezone().isoformat()
    return orm.CameraDevice(
        camera_id=dto.camera_id,
        tenant_id=dto.tenant_id,
        camera_name=dto.camera_name,
        location_id=dto.location_id,
        manufacturer=dto.manufacturer,
        model=dto.model,
        serial_number=dto.serial_number,
        install_position_desc=dto.install_position_desc,
        stream_uri=dto.stream_uri,
        access_policy_id=dto.access_policy_id,
        status=dto.status,
        created_at=_iso(dto.created_at) or now,
        updated_at=_iso(dto.updated_at) or now,
    )


def camera_to_dto(row: orm.CameraDevice) -> CameraDevice:
    return CameraDevice(
        camera_id=row.camera_id,
        tenant_id=row.tenant_id,
        camera_name=row.camera_name,
        location_id=row.location_id,
        manufacturer=row.manufacturer,
        model=row.model,
        serial_number=row.serial_number,
        install_position_desc=row.install_position_desc,
        stream_uri=row.stream_uri,
        access_policy_id=row.access_policy_id,
        status=row.status,
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


# --- VideoSource ----------------------------------------------------------


def video_to_orm(dto: VideoSource) -> orm.VideoSource:
    now = datetime.now().astimezone().isoformat()
    return orm.VideoSource(
        video_id=dto.video_id,
        tenant_id=dto.tenant_id,
        source_type=dto.source_type.value,
        source_uri=dto.source_uri,
        original_source_uri=dto.original_source_uri,
        camera_id=dto.camera_id,
        video_start_time=dto.video_start_time.isoformat(),
        video_end_time=_iso(dto.video_end_time),
        duration_ms=dto.duration_ms,
        source_status=dto.source_status,
        external_source_id=dto.external_source_id,
        access_policy_id=dto.access_policy_id,
        created_at=_iso(dto.created_at) or now,
        updated_at=_iso(dto.updated_at) or now,
    )


def video_to_dto(row: orm.VideoSource) -> VideoSource:
    start = _dt(row.video_start_time)
    assert start is not None
    return VideoSource(
        video_id=row.video_id,
        tenant_id=row.tenant_id,
        source_type=SourceType(row.source_type),
        source_uri=row.source_uri,
        original_source_uri=row.original_source_uri,
        camera_id=row.camera_id,
        video_start_time=start,
        video_end_time=_dt(row.video_end_time),
        duration_ms=row.duration_ms,
        source_status=row.source_status,
        external_source_id=row.external_source_id,
        access_policy_id=row.access_policy_id,
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


# --- AnalysisJob ----------------------------------------------------------


def job_to_orm(dto: AnalysisJob) -> orm.AnalysisJob:
    now = datetime.now().astimezone().isoformat()
    return orm.AnalysisJob(
        analysis_job_id=dto.analysis_job_id,
        video_id=dto.video_id,
        job_status=dto.job_status.value,
        idempotency_key=dto.idempotency_key,
        analysis_options_json=json.dumps(dto.analysis_options),
        model_version=dto.model_version,
        prompt_version=dto.prompt_version,
        pipeline_version=dto.pipeline_version,
        created_record_ids_json=json.dumps(dto.created_record_ids),
        updated_record_ids_json=json.dumps(dto.updated_record_ids),
        archived_record_ids_json=json.dumps(dto.archived_record_ids),
        failed_segment_ids_json=json.dumps(dto.failed_segment_ids),
        created_at=_iso(dto.created_at) or now,
        started_at=_iso(dto.started_at),
        finished_at=_iso(dto.finished_at),
        error_code=dto.error_code,
        error_message=dto.error_message,
    )


def job_to_dto(row: orm.AnalysisJob) -> AnalysisJob:
    return AnalysisJob(
        analysis_job_id=row.analysis_job_id,
        video_id=row.video_id,
        job_status=JobStatus(row.job_status),
        idempotency_key=row.idempotency_key,
        analysis_options=_loads_obj(row.analysis_options_json),
        model_version=row.model_version,
        prompt_version=row.prompt_version,
        pipeline_version=row.pipeline_version,
        created_record_ids=_loads_list(row.created_record_ids_json),
        updated_record_ids=_loads_list(row.updated_record_ids_json),
        archived_record_ids=_loads_list(row.archived_record_ids_json),
        failed_segment_ids=_loads_list(row.failed_segment_ids_json),
        created_at=_dt(row.created_at),
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        error_code=row.error_code,
        error_message=row.error_message,
    )


# --- AnalysisScaleTask ----------------------------------------------------


def scale_task_to_orm(dto: AnalysisScaleTask) -> orm.AnalysisScaleTask:
    now = datetime.now().astimezone().isoformat()
    return orm.AnalysisScaleTask(
        scale_task_id=dto.scale_task_id,
        analysis_job_id=dto.analysis_job_id,
        analysis_scale=dto.analysis_scale.value,
        status=dto.status.value,
        total_units=dto.total_units,
        succeeded_units=dto.succeeded_units,
        failed_units=dto.failed_units,
        skipped_reason=dto.skipped_reason,
        created_at=_iso(dto.created_at) or now,
        started_at=_iso(dto.started_at),
        finished_at=_iso(dto.finished_at),
        error_code=dto.error_code,
        error_message=dto.error_message,
    )


def scale_task_to_dto(row: orm.AnalysisScaleTask) -> AnalysisScaleTask:
    return AnalysisScaleTask(
        scale_task_id=row.scale_task_id,
        analysis_job_id=row.analysis_job_id,
        analysis_scale=AnalysisScale(row.analysis_scale),
        status=TaskStatus(row.status),
        total_units=row.total_units,
        succeeded_units=row.succeeded_units,
        failed_units=row.failed_units,
        skipped_reason=row.skipped_reason,
        created_at=_dt(row.created_at),
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        error_code=row.error_code,
        error_message=row.error_message,
    )


# --- HighFreqTrigger ------------------------------------------------------


def trigger_to_orm(dto: HighFreqTrigger) -> orm.HighFreqTrigger:
    now = datetime.now().astimezone().isoformat()
    return orm.HighFreqTrigger(
        trigger_id=dto.trigger_id,
        analysis_job_id=dto.analysis_job_id,
        scale_task_id=dto.scale_task_id,
        video_id=dto.video_id,
        trigger_start_ms=dto.trigger_start_ms,
        trigger_end_ms=dto.trigger_end_ms,
        motion_score=dto.motion_score,
        change_score=dto.change_score,
        trigger_reason=dto.trigger_reason,
        status=dto.status.value,
        idempotency_key=dto.idempotency_key,
        created_at=_iso(dto.created_at) or now,
        updated_at=_iso(dto.updated_at) or now,
        error_code=dto.error_code,
        error_message=dto.error_message,
    )


def trigger_to_dto(row: orm.HighFreqTrigger) -> HighFreqTrigger:
    return HighFreqTrigger(
        trigger_id=row.trigger_id,
        analysis_job_id=row.analysis_job_id,
        scale_task_id=row.scale_task_id,
        video_id=row.video_id,
        trigger_start_ms=row.trigger_start_ms,
        trigger_end_ms=row.trigger_end_ms,
        motion_score=row.motion_score,
        change_score=row.change_score,
        trigger_reason=row.trigger_reason,
        status=TriggerStatus(row.status),
        idempotency_key=row.idempotency_key,
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
        error_code=row.error_code,
        error_message=row.error_message,
    )


# --- AnalysisUnit / ModelCallLog ------------------------------------------


def analysis_unit_to_orm(dto: AnalysisUnit) -> orm.AnalysisUnit:
    now = datetime.now().astimezone().isoformat()
    return orm.AnalysisUnit(
        unit_id=dto.unit_id,
        analysis_job_id=dto.analysis_job_id,
        scale_task_id=dto.scale_task_id,
        video_id=dto.video_id,
        analysis_scale=dto.analysis_scale.value,
        unit_kind=dto.unit_kind,
        segment_start_ms=dto.segment_start_ms,
        segment_end_ms=dto.segment_end_ms,
        window_index=dto.window_index,
        trigger_id=dto.trigger_id,
        status=dto.status.value,
        attempt_count=dto.attempt_count,
        max_attempts=dto.max_attempts,
        last_error_code=dto.last_error_code,
        last_error_message=dto.last_error_message,
        latest_model_call_id=dto.latest_model_call_id,
        successful_model_call_id=dto.successful_model_call_id,
        produced_record_ids_json=json.dumps(dto.produced_record_ids),
        idempotency_key=dto.idempotency_key,
        created_at=_iso(dto.created_at) or now,
        started_at=_iso(dto.started_at),
        finished_at=_iso(dto.finished_at),
    )


def analysis_unit_to_dto(row: orm.AnalysisUnit) -> AnalysisUnit:
    return AnalysisUnit(
        unit_id=row.unit_id,
        analysis_job_id=row.analysis_job_id,
        scale_task_id=row.scale_task_id,
        video_id=row.video_id,
        analysis_scale=AnalysisScale(row.analysis_scale),
        unit_kind=row.unit_kind,
        segment_start_ms=row.segment_start_ms,
        segment_end_ms=row.segment_end_ms,
        window_index=row.window_index,
        trigger_id=row.trigger_id,
        status=TaskStatus(row.status),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        latest_model_call_id=row.latest_model_call_id,
        successful_model_call_id=row.successful_model_call_id,
        produced_record_ids=_loads_list(row.produced_record_ids_json),
        idempotency_key=row.idempotency_key,
        created_at=_dt(row.created_at),
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
    )


def model_call_log_to_orm(dto: ModelCallLog) -> orm.ModelCallLog:
    now = datetime.now().astimezone().isoformat()
    status = dto.status.value if isinstance(dto.status, ModelCallStatus) else str(dto.status)
    return orm.ModelCallLog(
        model_call_id=dto.model_call_id,
        analysis_job_id=dto.analysis_job_id,
        scale_task_id=dto.scale_task_id,
        unit_id=dto.unit_id,
        analysis_scale=dto.analysis_scale.value,
        segment_start_ms=dto.segment_start_ms,
        segment_end_ms=dto.segment_end_ms,
        provider=dto.provider,
        model_id=dto.model_id,
        prompt_version=dto.prompt_version,
        pipeline_version=dto.pipeline_version,
        status=status,
        attempt_count=dto.attempt_count,
        error_type=dto.error_type,
        error_message=dto.error_message,
        raw_text_input=dto.raw_text_input,
        raw_text_output=dto.raw_text_output,
        parsed_output_json=json.dumps(dto.parsed_output) if dto.parsed_output is not None else None,
        validation_status=dto.validation_status,
        payload_hash=dto.payload_hash,
        response_hash=dto.response_hash,
        media_refs_json=json.dumps(dto.media_refs),
        attempt_details_json=json.dumps(dto.attempt_details),
        started_at=_iso(dto.started_at),
        finished_at=_iso(dto.finished_at),
        duration_ms=dto.duration_ms,
        created_at=_iso(dto.created_at) or now,
    )


def model_call_log_to_dto(row: orm.ModelCallLog) -> ModelCallLog:
    return ModelCallLog(
        model_call_id=row.model_call_id,
        analysis_job_id=row.analysis_job_id,
        scale_task_id=row.scale_task_id,
        unit_id=row.unit_id,
        analysis_scale=AnalysisScale(row.analysis_scale),
        segment_start_ms=row.segment_start_ms,
        segment_end_ms=row.segment_end_ms,
        provider=row.provider,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        pipeline_version=row.pipeline_version,
        status=ModelCallStatus(row.status),
        attempt_count=row.attempt_count,
        error_type=row.error_type,
        error_message=row.error_message,
        raw_text_input=row.raw_text_input,
        raw_text_output=row.raw_text_output,
        parsed_output=_loads_obj(row.parsed_output_json) if row.parsed_output_json else None,
        validation_status=row.validation_status,
        payload_hash=row.payload_hash,
        response_hash=row.response_hash,
        media_refs=_loads_list(row.media_refs_json),
        attempt_details=_loads_list(row.attempt_details_json),
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        duration_ms=row.duration_ms,
        created_at=_dt(row.created_at),
    )


def detector_gate_log_to_orm(dto: DetectorGateLog) -> orm.DetectorGateLog:
    now = datetime.now().astimezone().isoformat()
    return orm.DetectorGateLog(
        gate_log_id=dto.gate_log_id,
        analysis_job_id=dto.analysis_job_id,
        scale_task_id=dto.scale_task_id,
        unit_id=dto.unit_id,
        video_id=dto.video_id,
        analysis_scale=dto.analysis_scale.value,
        segment_start_ms=dto.segment_start_ms,
        segment_end_ms=dto.segment_end_ms,
        provider=dto.provider,
        model_id=dto.model_id,
        status=dto.status,
        decision_json=json.dumps(dto.decision),
        frame_evidence_json=json.dumps(dto.frame_evidence),
        evidence_hash=dto.evidence_hash,
        rule_config_hash=dto.rule_config_hash,
        media_refs_json=json.dumps(dto.media_refs),
        artifact_refs_json=json.dumps(dto.artifact_refs),
        started_at=_iso(dto.started_at),
        finished_at=_iso(dto.finished_at),
        duration_ms=dto.duration_ms,
        created_at=_iso(dto.created_at) or now,
    )


def detector_gate_log_to_dto(row: orm.DetectorGateLog) -> DetectorGateLog:
    return DetectorGateLog(
        gate_log_id=row.gate_log_id,
        analysis_job_id=row.analysis_job_id,
        scale_task_id=row.scale_task_id,
        unit_id=row.unit_id,
        video_id=row.video_id,
        analysis_scale=AnalysisScale(row.analysis_scale),
        segment_start_ms=row.segment_start_ms,
        segment_end_ms=row.segment_end_ms,
        provider=row.provider,
        model_id=row.model_id,
        status=row.status,
        decision=_loads_obj(row.decision_json),
        frame_evidence=_loads_list(row.frame_evidence_json),
        evidence_hash=row.evidence_hash,
        rule_config_hash=row.rule_config_hash,
        media_refs=_loads_list(row.media_refs_json),
        artifact_refs=_loads_list(row.artifact_refs_json),
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        duration_ms=row.duration_ms,
        created_at=_dt(row.created_at),
    )


def pre_vlm_gate_log_to_orm(dto: PreVlmGateLog) -> orm.PreVlmGateLog:
    now = datetime.now().astimezone().isoformat()
    return orm.PreVlmGateLog(
        gate_log_id=dto.gate_log_id,
        analysis_job_id=dto.analysis_job_id,
        scale_task_id=dto.scale_task_id,
        unit_id=dto.unit_id,
        video_id=dto.video_id,
        analysis_scale=dto.analysis_scale.value,
        unit_kind=dto.unit_kind,
        profile_name=dto.profile_name,
        segment_start_ms=dto.segment_start_ms,
        segment_end_ms=dto.segment_end_ms,
        provider=dto.provider,
        model_id=dto.model_id,
        status=dto.status,
        error_type=dto.error_type,
        error_message=dto.error_message,
        raw_text_output=dto.raw_text_output,
        parsed_output_json=json.dumps(dto.parsed_output) if dto.parsed_output is not None else None,
        validation_status=dto.validation_status,
        attempt_details_json=json.dumps(dto.attempt_details),
        decision_json=json.dumps(dto.decision),
        signals_json=json.dumps(dto.signals),
        frame_evidence_json=json.dumps(dto.frame_evidence),
        evidence_hash=dto.evidence_hash,
        rule_config_hash=dto.rule_config_hash,
        suppression_policy=dto.suppression_policy,
        media_refs_json=json.dumps(dto.media_refs),
        artifact_refs_json=json.dumps(dto.artifact_refs),
        started_at=_iso(dto.started_at),
        finished_at=_iso(dto.finished_at),
        duration_ms=dto.duration_ms,
        created_at=_iso(dto.created_at) or now,
    )


def pre_vlm_gate_log_to_dto(row: orm.PreVlmGateLog) -> PreVlmGateLog:
    return PreVlmGateLog(
        gate_log_id=row.gate_log_id,
        analysis_job_id=row.analysis_job_id,
        scale_task_id=row.scale_task_id,
        unit_id=row.unit_id,
        video_id=row.video_id,
        analysis_scale=AnalysisScale(row.analysis_scale),
        unit_kind=row.unit_kind,
        profile_name=row.profile_name,
        segment_start_ms=row.segment_start_ms,
        segment_end_ms=row.segment_end_ms,
        provider=row.provider,
        model_id=row.model_id,
        status=row.status,
        error_type=row.error_type,
        error_message=row.error_message,
        raw_text_output=row.raw_text_output,
        parsed_output=_loads_obj(row.parsed_output_json) if row.parsed_output_json else None,
        validation_status=row.validation_status,
        attempt_details=_loads_list(row.attempt_details_json),
        decision=_loads_obj(row.decision_json),
        signals=_loads_list(row.signals_json),
        frame_evidence=_loads_list(row.frame_evidence_json),
        evidence_hash=row.evidence_hash,
        rule_config_hash=row.rule_config_hash,
        suppression_policy=row.suppression_policy,
        media_refs=_loads_list(row.media_refs_json),
        artifact_refs=_loads_list(row.artifact_refs_json),
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        duration_ms=row.duration_ms,
        created_at=_dt(row.created_at),
    )


def timeline_event_to_orm(dto: AnalysisTimelineEvent) -> orm.AnalysisTimelineEvent:
    now = datetime.now().astimezone().isoformat()
    return orm.AnalysisTimelineEvent(
        timeline_event_id=dto.timeline_event_id,
        trace_id=dto.trace_id,
        span_id=dto.span_id,
        parent_span_id=dto.parent_span_id,
        analysis_job_id=dto.analysis_job_id,
        task_id=dto.task_id,
        scale_task_id=dto.scale_task_id,
        unit_id=dto.unit_id,
        model_call_id=dto.model_call_id,
        video_id=dto.video_id,
        analysis_scale=dto.analysis_scale.value if dto.analysis_scale is not None else None,
        unit_kind=dto.unit_kind,
        segment_start_ms=dto.segment_start_ms,
        segment_end_ms=dto.segment_end_ms,
        event_name=dto.event_name,
        event_phase=dto.event_phase,
        status=dto.status,
        attempt_count=dto.attempt_count,
        occurred_at=dto.occurred_at.isoformat(),
        duration_ms=dto.duration_ms,
        error_code=dto.error_code,
        error_message=dto.error_message,
        correlation_json=json.dumps(dto.correlation),
        metadata_json=json.dumps(dto.metadata),
        created_at=_iso(dto.created_at) or now,
    )


def timeline_event_to_dto(row: orm.AnalysisTimelineEvent) -> AnalysisTimelineEvent:
    occurred_at = _dt(row.occurred_at)
    assert occurred_at is not None
    return AnalysisTimelineEvent(
        timeline_event_id=row.timeline_event_id,
        trace_id=row.trace_id,
        span_id=row.span_id,
        parent_span_id=row.parent_span_id,
        analysis_job_id=row.analysis_job_id,
        task_id=row.task_id,
        scale_task_id=row.scale_task_id,
        unit_id=row.unit_id,
        model_call_id=row.model_call_id,
        video_id=row.video_id,
        analysis_scale=AnalysisScale(row.analysis_scale) if row.analysis_scale else None,
        unit_kind=row.unit_kind,
        segment_start_ms=row.segment_start_ms,
        segment_end_ms=row.segment_end_ms,
        event_name=row.event_name,
        event_phase=row.event_phase,
        status=row.status,
        attempt_count=row.attempt_count,
        occurred_at=occurred_at,
        duration_ms=row.duration_ms,
        error_code=row.error_code,
        error_message=row.error_message,
        correlation=_loads_obj(row.correlation_json),
        metadata=_loads_obj(row.metadata_json),
        created_at=_dt(row.created_at),
    )


# --- ObservationRecord ----------------------------------------------------


def observation_to_orm(dto: ObservationRecord) -> orm.ObservationRecord:
    now = datetime.now().astimezone().isoformat()
    return orm.ObservationRecord(
        record_id=dto.record_id,
        tenant_id=dto.tenant_id,
        video_id=dto.video_id,
        analysis_job_id=dto.analysis_job_id,
        analysis_scale=dto.analysis_scale.value,
        segment_start_ms=dto.segment_start_ms,
        segment_end_ms=dto.segment_end_ms,
        observed_start_time=dto.observed_start_time.isoformat(),
        observed_end_time=dto.observed_end_time.isoformat(),
        camera_id=dto.camera_id,
        location_id=dto.location_id,
        static_description_text=dto.static_description_text,
        dynamic_description_text=dto.dynamic_description_text,
        tags_json=json.dumps(dto.tags),
        clip_uri=dto.clip_uri,
        thumbnail_uri=dto.thumbnail_uri,
        attributes_json=json.dumps(dto.attributes),
        access_policy_id=dto.access_policy_id,
        security_level=dto.security_level.value,
        model_version=dto.model_version,
        prompt_version=dto.prompt_version,
        pipeline_version=dto.pipeline_version,
        created_at=_iso(dto.created_at) or now,
        updated_at=_iso(dto.updated_at) or now,
    )


def observation_to_dto(row: orm.ObservationRecord) -> ObservationRecord:
    start = _dt(row.observed_start_time)
    end = _dt(row.observed_end_time)
    assert start is not None
    assert end is not None
    return ObservationRecord(
        record_id=row.record_id,
        tenant_id=row.tenant_id,
        video_id=row.video_id,
        analysis_job_id=row.analysis_job_id,
        analysis_scale=AnalysisScale(row.analysis_scale),
        segment_start_ms=row.segment_start_ms,
        segment_end_ms=row.segment_end_ms,
        observed_start_time=start,
        observed_end_time=end,
        camera_id=row.camera_id,
        location_id=row.location_id,
        static_description_text=row.static_description_text,
        dynamic_description_text=row.dynamic_description_text,
        tags=_loads_list(row.tags_json),
        clip_uri=row.clip_uri,
        thumbnail_uri=row.thumbnail_uri,
        attributes=_loads_obj(row.attributes_json),
        access_policy_id=row.access_policy_id,
        security_level=SecurityLevel(row.security_level),
        model_version=row.model_version,
        prompt_version=row.prompt_version,
        pipeline_version=row.pipeline_version,
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


# --- Principal / AccessPolicy ---------------------------------------------


def principal_to_orm(dto: Principal) -> orm.Principal:
    now = datetime.now().astimezone().isoformat()
    return orm.Principal(
        principal_id=dto.principal_id,
        principal_type=dto.principal_type.value,
        tenant_id=dto.tenant_id,
        external_subject_id=dto.external_subject_id,
        display_name=dto.display_name,
        status=dto.status,
        roles_json=json.dumps(dto.roles),
        groups_json=json.dumps(dto.groups),
        created_at=now,
        updated_at=now,
    )


def principal_to_dto(row: orm.Principal) -> Principal:
    return Principal(
        principal_id=row.principal_id,
        principal_type=PrincipalType(row.principal_type),
        tenant_id=row.tenant_id,
        external_subject_id=row.external_subject_id,
        display_name=row.display_name,
        status=row.status,
        roles=_loads_list(row.roles_json),
        groups=_loads_list(row.groups_json),
    )


def policy_to_orm(dto: AccessPolicy) -> orm.AccessPolicy:
    now = datetime.now().astimezone().isoformat()
    return orm.AccessPolicy(
        access_policy_id=dto.access_policy_id,
        tenant_id=dto.tenant_id,
        name=dto.name,
        security_level=dto.security_level.value,
        rules_json=dto.rules.model_dump_json(),
        created_at=_iso(dto.created_at) or now,
        updated_at=_iso(dto.updated_at) or now,
    )


def policy_to_dto(row: orm.AccessPolicy) -> AccessPolicy:
    return AccessPolicy(
        access_policy_id=row.access_policy_id,
        tenant_id=row.tenant_id,
        name=row.name,
        security_level=SecurityLevel(row.security_level),
        # SQLite stores rules as a JSON *string*; the PostgreSQL JSONB column is
        # returned already-deserialized as a dict. ``_loads_obj`` accepts both,
        # so use ``model_validate`` instead of ``model_validate_json`` (which
        # only accepts str/bytes and would raise on the PostgreSQL dict).
        rules=AccessPolicyRules.model_validate(_loads_obj(row.rules_json)),
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


# --- SearchContext / Revision / Candidate ---------------------------------


def context_to_orm(dto: SearchContext) -> orm.SearchContext:
    now = datetime.now().astimezone().isoformat()
    return orm.SearchContext(
        context_id=dto.context_id,
        tenant_id=dto.tenant_id,
        principal_id=dto.principal_id,
        session_id=dto.session_id,
        authorized_scope_hash=dto.authorized_scope_hash,
        dataset_revision=dto.dataset_revision,
        mode=dto.mode.value,
        default_revision_id=dto.default_revision_id,
        created_at=_iso(dto.created_at) or now,
        last_accessed_at=_iso(dto.last_accessed_at) or now,
        expires_at=_iso(dto.expires_at) or now,
        status=dto.status,
    )


def context_to_dto(row: orm.SearchContext) -> SearchContext:
    return SearchContext(
        context_id=row.context_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        session_id=row.session_id,
        authorized_scope_hash=row.authorized_scope_hash,
        dataset_revision=row.dataset_revision,
        mode=ContextMode(row.mode),
        default_revision_id=row.default_revision_id,
        created_at=_dt(row.created_at),
        last_accessed_at=_dt(row.last_accessed_at),
        expires_at=_dt(row.expires_at),
        status=row.status,
    )


def revision_to_orm(dto: SearchRevision) -> orm.SearchRevision:
    now = datetime.now().astimezone().isoformat()
    return orm.SearchRevision(
        revision_id=dto.revision_id,
        context_id=dto.context_id,
        parent_revision_id=dto.parent_revision_id,
        op=dto.op,
        op_params_json=json.dumps(dto.op_params),
        candidate_count=dto.candidate_count,
        facets_json=json.dumps(dto.facets),
        created_at=_iso(dto.created_at) or now,
    )


def revision_to_dto(row: orm.SearchRevision) -> SearchRevision:
    return SearchRevision(
        revision_id=row.revision_id,
        context_id=row.context_id,
        parent_revision_id=row.parent_revision_id,
        op=row.op,
        op_params=_loads_obj(row.op_params_json),
        candidate_count=row.candidate_count,
        facets=_loads_obj(row.facets_json) if row.facets_json else {},
        created_at=_dt(row.created_at),
    )


def candidate_to_orm(dto: SearchCandidate) -> orm.SearchCandidate:
    return orm.SearchCandidate(
        revision_id=dto.revision_id,
        record_id=dto.record_id,
        rank=dto.rank,
        score=dto.score,
        score_detail_json=json.dumps(dto.score_detail),
    )


def candidate_to_dto(row: orm.SearchCandidate) -> SearchCandidate:
    return SearchCandidate(
        revision_id=row.revision_id,
        record_id=row.record_id,
        rank=row.rank,
        score=row.score,
        score_detail=_loads_obj(row.score_detail_json),
    )


# --- Task -----------------------------------------------------------------


def task_to_orm(dto: Task) -> orm.AnalysisTask:
    now = datetime.now().astimezone()
    # SQLite stores timestamps as ISO text; convert the canonical datetime DTO
    # fields at this adapter boundary. (The PostgreSQL adapter writes datetimes
    # natively via raw SQL and does not use this mapper.)
    return orm.AnalysisTask(
        task_id=dto.task_id,
        schema_version=dto.schema_version,
        task_type=dto.task_type,
        payload_json=json.dumps(dto.payload),
        status=dto.status,
        priority=dto.priority,
        retry_count=dto.retry_count,
        max_retries=dto.max_retries,
        next_run_at=_iso(dto.next_run_at),
        lease_owner=dto.lease_owner,
        lease_expires_at=_iso(dto.lease_expires_at),
        created_at=_iso(dto.created_at) or now.isoformat(),
        updated_at=_iso(dto.updated_at) or now.isoformat(),
        error_code=dto.error_code,
        error_message=dto.error_message,
    )


def task_to_dto(row: orm.AnalysisTask) -> Task:
    return Task(
        task_id=row.task_id,
        schema_version=row.schema_version,
        task_type=row.task_type,
        payload=_loads_obj(row.payload_json),
        status=row.status,
        priority=row.priority,
        retry_count=row.retry_count,
        max_retries=row.max_retries,
        next_run_at=_dt(row.next_run_at),
        lease_owner=row.lease_owner,
        lease_expires_at=_dt(row.lease_expires_at),
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
        error_code=row.error_code,
        error_message=row.error_message,
    )


# --- AuditEvent -----------------------------------------------------------


def audit_to_orm(dto: AuditEvent) -> orm.AuditEvent:
    now = datetime.now().astimezone().isoformat()
    return orm.AuditEvent(
        audit_event_id=dto.audit_event_id,
        event_type=dto.event_type,
        request_id=dto.request_id,
        principal_id=dto.principal_id,
        session_id=dto.session_id,
        context_id=dto.context_id,
        resource_scope_hash=dto.resource_scope_hash,
        record_ids_json=json.dumps(dto.record_ids),
        video_id=dto.video_id,
        camera_id=dto.camera_id,
        metadata_json=json.dumps(dto.metadata),
        created_at=_iso(dto.created_at) or now,
    )


def audit_to_dto(row: orm.AuditEvent) -> AuditEvent:
    return AuditEvent(
        audit_event_id=row.audit_event_id,
        event_type=row.event_type,
        request_id=row.request_id,
        principal_id=row.principal_id,
        session_id=row.session_id,
        context_id=row.context_id,
        resource_scope_hash=row.resource_scope_hash,
        record_ids=_loads_list(row.record_ids_json),
        video_id=row.video_id,
        camera_id=row.camera_id,
        metadata=_loads_obj(row.metadata_json),
        created_at=_dt(row.created_at),
    )
