"""PostgreSQL repository adapters behind existing ports."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cctv_memory.contracts.analysis import (
    AnalysisJob,
    AnalysisScaleTask,
    AnalysisUnit,
    DetectorGateLog,
    HighFreqTrigger,
    ModelCallLog,
)
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import AccessPolicy, AuthorizedScope, Principal
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublicationResult, PublishObservationRecordsCommand
from cctv_memory.contracts.pre_vlm_gate import PreVlmGateLog
from cctv_memory.contracts.search import SearchCandidate, SearchContext, SearchRevision
from cctv_memory.contracts.task import Task
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.contracts.video import (
    CameraDevice,
    CameraLocation,
    SubmitVideoSourceRequest,
    VideoSource,
)
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel, TaskStatus
from cctv_memory.infrastructure.db import mappers
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.infrastructure.db.postgres import text_index
from cctv_memory.infrastructure.db.postgres.vector import serialize_pgvector
from cctv_memory.infrastructure.db.repositories._helpers import map_integrity_error
from cctv_memory.infrastructure.db.repositories.admin import (
    SqliteAccessPolicyRepository,
    SqliteAnalysisJobRepository,
    SqliteAnalysisScaleTaskRepository,
    SqliteAnalysisUnitRepository,
    SqliteCameraRepository,
    SqliteDetectorGateLogRepository,
    SqliteHighFreqTriggerRepository,
    SqliteModelCallLogRepository,
    SqlitePrincipalRepository,
    SqlitePreVlmGateLogRepository,
    SqliteVideoSourceRepository,
)
from cctv_memory.infrastructure.db.repositories.audit import SqliteAuditRepository
from cctv_memory.infrastructure.db.repositories.observation_read import (
    SqliteObservationReadRepository,
)
from cctv_memory.infrastructure.db.repositories.search_context import (
    SqliteSearchContextRepository,
)
from cctv_memory.infrastructure.db.repositories.task_queue import SqliteTaskQueueRepository
from cctv_memory.infrastructure.db.repositories.timeline import SqliteTimelineRepository
from cctv_memory.repositories.index import StoredVector
from cctv_memory.repositories.types import ConflictError, IdempotencyConflictError


def _now() -> datetime:
    return datetime.now().astimezone()


def _as_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _as_dt_or_now(value: datetime | str | None) -> datetime:
    return _as_dt(value) or _now()


def _json(value: Any) -> str:
    return json.dumps(value)


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return cast(list[Any], json.loads(value))


class PostgresCameraRepository(SqliteCameraRepository):
    def upsert_location(self, location: CameraLocation) -> CameraLocation:
        self._session.execute(
            text(
                """
                INSERT INTO camera_locations(
                  location_id, tenant_id, building, floor, area, room_or_zone,
                  location_desc, access_policy_id, security_level, created_at, updated_at
                ) VALUES (
                  :location_id, :tenant_id, :building, :floor, :area, :room_or_zone,
                  :location_desc, :access_policy_id, :security_level, :created_at, :updated_at
                )
                ON CONFLICT (location_id) DO UPDATE SET
                  tenant_id = EXCLUDED.tenant_id,
                  building = EXCLUDED.building,
                  floor = EXCLUDED.floor,
                  area = EXCLUDED.area,
                  room_or_zone = EXCLUDED.room_or_zone,
                  location_desc = EXCLUDED.location_desc,
                  access_policy_id = EXCLUDED.access_policy_id,
                  security_level = EXCLUDED.security_level,
                  updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "location_id": location.location_id,
                "tenant_id": location.tenant_id,
                "building": location.building,
                "floor": location.floor,
                "area": location.area,
                "room_or_zone": location.room_or_zone,
                "location_desc": location.location_desc,
                "access_policy_id": location.access_policy_id,
                "security_level": location.security_level.value,
                "created_at": _as_dt_or_now(location.created_at),
                "updated_at": _as_dt_or_now(location.updated_at),
            },
        )
        self._session.flush()
        row = self._session.get(orm.CameraLocation, location.location_id)
        assert row is not None
        return mappers.location_to_dto(row)

    def upsert_camera(self, camera: CameraDevice) -> CameraDevice:
        self._session.execute(
            text(
                """
                INSERT INTO camera_devices(
                  camera_id, tenant_id, camera_name, location_id, manufacturer, model,
                  serial_number, install_position_desc, stream_uri, access_policy_id,
                  status, created_at, updated_at
                ) VALUES (
                  :camera_id, :tenant_id, :camera_name, :location_id, :manufacturer, :model,
                  :serial_number, :install_position_desc, :stream_uri, :access_policy_id,
                  :status, :created_at, :updated_at
                )
                ON CONFLICT (camera_id) DO UPDATE SET
                  tenant_id = EXCLUDED.tenant_id,
                  camera_name = EXCLUDED.camera_name,
                  location_id = EXCLUDED.location_id,
                  manufacturer = EXCLUDED.manufacturer,
                  model = EXCLUDED.model,
                  serial_number = EXCLUDED.serial_number,
                  install_position_desc = EXCLUDED.install_position_desc,
                  stream_uri = EXCLUDED.stream_uri,
                  access_policy_id = EXCLUDED.access_policy_id,
                  status = EXCLUDED.status,
                  updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "camera_id": camera.camera_id,
                "tenant_id": camera.tenant_id,
                "camera_name": camera.camera_name,
                "location_id": camera.location_id,
                "manufacturer": camera.manufacturer,
                "model": camera.model,
                "serial_number": camera.serial_number,
                "install_position_desc": camera.install_position_desc,
                "stream_uri": camera.stream_uri,
                "access_policy_id": camera.access_policy_id,
                "status": camera.status,
                "created_at": _as_dt_or_now(camera.created_at),
                "updated_at": _as_dt_or_now(camera.updated_at),
            },
        )
        self._session.flush()
        row = self._session.get(orm.CameraDevice, camera.camera_id)
        assert row is not None
        return mappers.camera_to_dto(row)


class PostgresVideoSourceRepository(SqliteVideoSourceRepository):
    def create_or_get_by_idempotency(
        self, request: SubmitVideoSourceRequest, *, video_id: str
    ) -> VideoSource:
        existing = self._session.execute(
            text(
                """
                SELECT video_id, source_uri
                FROM video_sources
                WHERE camera_id = :camera_id AND video_start_time = :video_start_time
                """
            ),
            {"camera_id": request.camera_id, "video_start_time": request.video_start_time},
        ).first()
        if existing is not None:
            if existing.source_uri != request.source_uri:
                raise IdempotencyConflictError(
                    "VideoSource exists for (camera_id, video_start_time) with different payload"
                )
            row = self._session.get(orm.VideoSource, existing.video_id)
            assert row is not None
            return mappers.video_to_dto(row)

        dto = VideoSource(
            video_id=video_id,
            source_type=request.source_type,
            source_uri=request.source_uri,
            camera_id=request.camera_id,
            video_start_time=request.video_start_time,
            external_source_id=request.external_source_id,
            source_status="pending",
        )
        self._session.execute(
            text(
                """
                INSERT INTO video_sources(
                  video_id, tenant_id, source_type, source_uri, original_source_uri,
                  camera_id, video_start_time, video_end_time, duration_ms, source_status,
                  external_source_id, access_policy_id, created_at, updated_at
                ) VALUES (
                  :video_id, :tenant_id, :source_type, :source_uri, :original_source_uri,
                  :camera_id, :video_start_time, :video_end_time, :duration_ms, :source_status,
                  :external_source_id, :access_policy_id, :created_at, :updated_at
                )
                """
            ),
            {
                "video_id": dto.video_id,
                "tenant_id": dto.tenant_id,
                "source_type": dto.source_type.value,
                "source_uri": dto.source_uri,
                "original_source_uri": dto.original_source_uri,
                "camera_id": dto.camera_id,
                "video_start_time": dto.video_start_time,
                "video_end_time": dto.video_end_time,
                "duration_ms": dto.duration_ms,
                "source_status": dto.source_status,
                "external_source_id": dto.external_source_id,
                "access_policy_id": dto.access_policy_id,
                "created_at": _as_dt_or_now(dto.created_at),
                "updated_at": _as_dt_or_now(dto.updated_at),
            },
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        row = self._session.get(orm.VideoSource, video_id)
        assert row is not None
        return mappers.video_to_dto(row)

    def update_probe_metadata(
        self, video_id: str, *, duration_ms: int, video_end_time: datetime
    ) -> None:
        self._session.execute(
            text(
                """
                UPDATE video_sources
                SET duration_ms = :duration_ms, video_end_time = :video_end_time
                WHERE video_id = :video_id
                """
            ),
            {
                "video_id": video_id,
                "duration_ms": duration_ms,
                "video_end_time": _as_dt(video_end_time),
            },
        )
        self._session.flush()


class PostgresAnalysisJobRepository(SqliteAnalysisJobRepository):
    def create_job(self, job: AnalysisJob) -> AnalysisJob:
        existing = self._session.execute(
            text(
                """
                SELECT analysis_job_id, video_id
                FROM analysis_jobs
                WHERE idempotency_key = :idempotency_key
                """
            ),
            {"idempotency_key": job.idempotency_key},
        ).first()
        if existing is not None:
            if existing.video_id != job.video_id:
                raise IdempotencyConflictError(
                    "AnalysisJob idempotency_key reused with different video_id"
                )
            row = self._session.get(orm.AnalysisJob, existing.analysis_job_id)
            assert row is not None
            return mappers.job_to_dto(row)
        self._session.execute(
            text(
                """
                INSERT INTO analysis_jobs(
                  analysis_job_id, video_id, job_status, idempotency_key,
                  analysis_options_json, model_version, prompt_version, pipeline_version,
                  created_record_ids_json, updated_record_ids_json, archived_record_ids_json,
                  failed_segment_ids_json, created_at, started_at, finished_at, error_code,
                  error_message
                ) VALUES (
                  :analysis_job_id, :video_id, :job_status, :idempotency_key,
                  CAST(:analysis_options_json AS jsonb), :model_version, :prompt_version,
                  :pipeline_version, CAST(:created_record_ids_json AS jsonb),
                  CAST(:updated_record_ids_json AS jsonb), CAST(:archived_record_ids_json AS jsonb),
                  CAST(:failed_segment_ids_json AS jsonb), :created_at, :started_at,
                  :finished_at, :error_code, :error_message
                )
                """
            ),
            {
                "analysis_job_id": job.analysis_job_id,
                "video_id": job.video_id,
                "job_status": job.job_status.value,
                "idempotency_key": job.idempotency_key,
                "analysis_options_json": _json(job.analysis_options),
                "model_version": job.model_version,
                "prompt_version": job.prompt_version,
                "pipeline_version": job.pipeline_version,
                "created_record_ids_json": _json(job.created_record_ids),
                "updated_record_ids_json": _json(job.updated_record_ids),
                "archived_record_ids_json": _json(job.archived_record_ids),
                "failed_segment_ids_json": _json(job.failed_segment_ids),
                "created_at": _as_dt_or_now(job.created_at),
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error_code": job.error_code,
                "error_message": job.error_message,
            },
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        row = self._session.get(orm.AnalysisJob, job.analysis_job_id)
        assert row is not None
        return mappers.job_to_dto(row)

    def update_status(
        self,
        analysis_job_id: str,
        status: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_jobs
                SET job_status = :status,
                    started_at = COALESCE(:started_at, started_at),
                    finished_at = COALESCE(:finished_at, finished_at),
                    error_code = :error_code,
                    error_message = :error_message
                WHERE analysis_job_id = :analysis_job_id
                """
            ),
            {
                "analysis_job_id": analysis_job_id,
                "status": status,
                "started_at": _as_dt(started_at),
                "finished_at": _as_dt(finished_at),
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        self._session.flush()

    def append_record_publish_summary(
        self,
        analysis_job_id: str,
        created_ids: list[str],
        updated_ids: list[str],
        archived_ids: list[str],
    ) -> None:
        row = self._session.get(orm.AnalysisJob, analysis_job_id)
        if row is None:
            return
        self._session.execute(
            text(
                """
                UPDATE analysis_jobs
                SET created_record_ids_json = CAST(:created AS jsonb),
                    updated_record_ids_json = CAST(:updated AS jsonb),
                    archived_record_ids_json = CAST(:archived AS jsonb)
                WHERE analysis_job_id = :analysis_job_id
                """
            ),
            {
                "analysis_job_id": analysis_job_id,
                "created": _json(_json_list(row.created_record_ids_json) + created_ids),
                "updated": _json(_json_list(row.updated_record_ids_json) + updated_ids),
                "archived": _json(_json_list(row.archived_record_ids_json) + archived_ids),
            },
        )
        self._session.flush()


class PostgresAnalysisScaleTaskRepository(SqliteAnalysisScaleTaskRepository):
    def create_scale_task(self, task: AnalysisScaleTask) -> AnalysisScaleTask:
        self._session.execute(
            text(
                """
                INSERT INTO analysis_scale_tasks(
                  scale_task_id, analysis_job_id, analysis_scale, status, total_units,
                  succeeded_units, failed_units, skipped_reason, created_at, started_at,
                  finished_at, error_code, error_message
                ) VALUES (
                  :scale_task_id, :analysis_job_id, :analysis_scale, :status, :total_units,
                  :succeeded_units, :failed_units, :skipped_reason, :created_at, :started_at,
                  :finished_at, :error_code, :error_message
                )
                """
            ),
            {
                "scale_task_id": task.scale_task_id,
                "analysis_job_id": task.analysis_job_id,
                "analysis_scale": task.analysis_scale.value,
                "status": task.status.value,
                "total_units": task.total_units,
                "succeeded_units": task.succeeded_units,
                "failed_units": task.failed_units,
                "skipped_reason": task.skipped_reason,
                "created_at": _as_dt_or_now(task.created_at),
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "error_code": task.error_code,
                "error_message": task.error_message,
            },
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc) from exc
        row = self._session.get(orm.AnalysisScaleTask, task.scale_task_id)
        assert row is not None
        return mappers.scale_task_to_dto(row)


class PostgresHighFreqTriggerRepository(SqliteHighFreqTriggerRepository):
    def create_or_get_by_idempotency(self, trigger: HighFreqTrigger) -> HighFreqTrigger:
        existing = self._session.scalar(
            select(orm.HighFreqTrigger).where(
                orm.HighFreqTrigger.idempotency_key == trigger.idempotency_key
            )
        )
        if existing is not None:
            return mappers.trigger_to_dto(existing)
        self._session.execute(
            text(
                """
                INSERT INTO high_freq_triggers(
                  trigger_id, analysis_job_id, scale_task_id, video_id, trigger_start_ms,
                  trigger_end_ms, motion_score, change_score, trigger_reason, status,
                  idempotency_key, created_at, updated_at, error_code, error_message
                ) VALUES (
                  :trigger_id, :analysis_job_id, :scale_task_id, :video_id, :trigger_start_ms,
                  :trigger_end_ms, :motion_score, :change_score, :trigger_reason, :status,
                  :idempotency_key, :created_at, :updated_at, :error_code, :error_message
                )
                """
            ),
            {
                "trigger_id": trigger.trigger_id,
                "analysis_job_id": trigger.analysis_job_id,
                "scale_task_id": trigger.scale_task_id,
                "video_id": trigger.video_id,
                "trigger_start_ms": trigger.trigger_start_ms,
                "trigger_end_ms": trigger.trigger_end_ms,
                "motion_score": trigger.motion_score,
                "change_score": trigger.change_score,
                "trigger_reason": trigger.trigger_reason,
                "status": trigger.status.value,
                "idempotency_key": trigger.idempotency_key,
                "created_at": _as_dt_or_now(trigger.created_at),
                "updated_at": _as_dt_or_now(trigger.updated_at),
                "error_code": trigger.error_code,
                "error_message": trigger.error_message,
            },
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        row = self._session.get(orm.HighFreqTrigger, trigger.trigger_id)
        assert row is not None
        return mappers.trigger_to_dto(row)


class PostgresAnalysisUnitRepository(SqliteAnalysisUnitRepository):
    def create_or_get_by_idempotency(self, unit: AnalysisUnit) -> AnalysisUnit:
        existing = self._session.scalar(
            select(orm.AnalysisUnit).where(orm.AnalysisUnit.idempotency_key == unit.idempotency_key)
        )
        if existing is not None:
            return mappers.analysis_unit_to_dto(existing)
        self._session.execute(
            text(
                """
                INSERT INTO analysis_units(
                  unit_id, analysis_job_id, scale_task_id, video_id, analysis_scale, unit_kind,
                  segment_start_ms, segment_end_ms, window_index, trigger_id, status,
                  attempt_count, max_attempts, last_error_code, last_error_message,
                  latest_model_call_id, successful_model_call_id, produced_record_ids_json,
                  idempotency_key, created_at, started_at, finished_at
                ) VALUES (
                  :unit_id, :analysis_job_id, :scale_task_id, :video_id,
                  :analysis_scale, :unit_kind,
                  :segment_start_ms, :segment_end_ms, :window_index, :trigger_id, :status,
                  :attempt_count, :max_attempts, :last_error_code, :last_error_message,
                  :latest_model_call_id, :successful_model_call_id,
                  CAST(:produced_record_ids_json AS jsonb), :idempotency_key, :created_at,
                  :started_at, :finished_at
                )
                """
            ),
            {
                "unit_id": unit.unit_id,
                "analysis_job_id": unit.analysis_job_id,
                "scale_task_id": unit.scale_task_id,
                "video_id": unit.video_id,
                "analysis_scale": unit.analysis_scale.value,
                "unit_kind": unit.unit_kind,
                "segment_start_ms": unit.segment_start_ms,
                "segment_end_ms": unit.segment_end_ms,
                "window_index": unit.window_index,
                "trigger_id": unit.trigger_id,
                "status": unit.status.value,
                "attempt_count": unit.attempt_count,
                "max_attempts": unit.max_attempts,
                "last_error_code": unit.last_error_code,
                "last_error_message": unit.last_error_message,
                "latest_model_call_id": unit.latest_model_call_id,
                "successful_model_call_id": unit.successful_model_call_id,
                "produced_record_ids_json": _json(unit.produced_record_ids),
                "idempotency_key": unit.idempotency_key,
                "created_at": _as_dt_or_now(unit.created_at),
                "started_at": unit.started_at,
                "finished_at": unit.finished_at,
            },
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        row = self._session.get(orm.AnalysisUnit, unit.unit_id)
        assert row is not None
        return mappers.analysis_unit_to_dto(row)

    def mark_running(self, unit_id: str, *, model_call_id: str | None = None) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_units
                SET status = :status,
                    attempt_count = attempt_count + 1,
                    latest_model_call_id = :model_call_id,
                    started_at = COALESCE(started_at, :started_at)
                WHERE unit_id = :unit_id
                """
            ),
            {
                "unit_id": unit_id,
                "status": TaskStatus.RUNNING.value,
                "model_call_id": model_call_id,
                "started_at": _now(),
            },
        )
        self._session.flush()

    def mark_succeeded(
        self, unit_id: str, *, model_call_id: str | None, record_ids: list[str],
        attempt_count: int | None = None,
    ) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_units
                SET status = :status,
                    latest_model_call_id = :model_call_id,
                    successful_model_call_id = :model_call_id,
                    produced_record_ids_json = CAST(:record_ids AS jsonb),
                    attempt_count = COALESCE(:attempt_count, attempt_count),
                    finished_at = :finished_at,
                    last_error_code = NULL,
                    last_error_message = NULL
                WHERE unit_id = :unit_id
                """
            ),
            {
                "unit_id": unit_id,
                "status": TaskStatus.SUCCEEDED.value,
                "model_call_id": model_call_id,
                "record_ids": _json(record_ids),
                "attempt_count": attempt_count,
                "finished_at": _now(),
            },
        )
        self._session.flush()

    def mark_failed(
        self,
        unit_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
        model_call_id: str | None = None,
        attempt_count: int | None = None,
    ) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_units
                SET status = :status,
                    latest_model_call_id = COALESCE(:model_call_id, latest_model_call_id),
                    attempt_count = COALESCE(:attempt_count, attempt_count),
                    last_error_code = :error_code,
                    last_error_message = :error_message,
                    finished_at = :finished_at
                WHERE unit_id = :unit_id
                """
            ),
            {
                "unit_id": unit_id,
                "status": TaskStatus.FAILED.value,
                "model_call_id": model_call_id,
                "attempt_count": attempt_count,
                "error_code": error_code,
                "error_message": error_message,
                "finished_at": _now(),
            },
        )
        self._session.flush()

    def mark_skipped(
        self,
        unit_id: str,
        *,
        skipped_reason: str,
        model_call_id: str | None = None,
    ) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_units
                SET status = :status,
                    latest_model_call_id = COALESCE(:model_call_id, latest_model_call_id),
                    last_error_code = :skipped_reason,
                    last_error_message = NULL,
                    finished_at = :finished_at
                WHERE unit_id = :unit_id
                """
            ),
            {
                "unit_id": unit_id,
                "status": TaskStatus.SKIPPED.value,
                "model_call_id": model_call_id,
                "skipped_reason": skipped_reason,
                "finished_at": _now(),
            },
        )
        self._session.flush()

    def list_stale_running(
        self, *, cutoff: datetime, limit: int
    ) -> list[AnalysisUnit]:
        if limit <= 0:
            return []
        rows = self._session.execute(
            text(
                """
                SELECT *
                FROM analysis_units
                WHERE status = :status
                  AND started_at IS NOT NULL
                  AND started_at < :cutoff
                ORDER BY started_at
                LIMIT :limit
                """
            ),
            {
                "status": TaskStatus.RUNNING.value,
                "cutoff": _as_dt(cutoff),
                "limit": limit,
            },
        )
        return [mappers.analysis_unit_to_dto(cast(Any, row)) for row in rows]


class PostgresModelCallLogRepository(SqliteModelCallLogRepository):
    def create_log(self, log: ModelCallLog) -> ModelCallLog:
        status = log.status.value if hasattr(log.status, "value") else str(log.status)
        self._session.execute(
            text(
                """
                INSERT INTO model_call_logs(
                  model_call_id, analysis_job_id, scale_task_id, unit_id, analysis_scale,
                  segment_start_ms, segment_end_ms, provider, model_id, prompt_version,
                  pipeline_version, status, attempt_count, error_type, error_message,
                  raw_text_input, raw_text_output, parsed_output_json, validation_status,
                  payload_hash, response_hash, media_refs_json, attempt_details_json,
                  started_at, finished_at, duration_ms, created_at
                ) VALUES (
                  :model_call_id, :analysis_job_id, :scale_task_id, :unit_id, :analysis_scale,
                  :segment_start_ms, :segment_end_ms, :provider, :model_id, :prompt_version,
                  :pipeline_version, :status, :attempt_count, :error_type, :error_message,
                  :raw_text_input, :raw_text_output, CAST(:parsed_output_json AS jsonb),
                  :validation_status, :payload_hash, :response_hash,
                  CAST(:media_refs_json AS jsonb), CAST(:attempt_details_json AS jsonb),
                  :started_at, :finished_at, :duration_ms, :created_at
                )
                """
            ),
            {
                "model_call_id": log.model_call_id,
                "analysis_job_id": log.analysis_job_id,
                "scale_task_id": log.scale_task_id,
                "unit_id": log.unit_id,
                "analysis_scale": log.analysis_scale.value,
                "segment_start_ms": log.segment_start_ms,
                "segment_end_ms": log.segment_end_ms,
                "provider": log.provider,
                "model_id": log.model_id,
                "prompt_version": log.prompt_version,
                "pipeline_version": log.pipeline_version,
                "status": status,
                "attempt_count": log.attempt_count,
                "error_type": log.error_type,
                "error_message": log.error_message,
                "raw_text_input": log.raw_text_input,
                "raw_text_output": log.raw_text_output,
                "parsed_output_json": (
                    _json(log.parsed_output) if log.parsed_output is not None else None
                ),
                "validation_status": log.validation_status,
                "payload_hash": log.payload_hash,
                "response_hash": log.response_hash,
                "media_refs_json": _json(log.media_refs),
                "attempt_details_json": _json(log.attempt_details),
                "started_at": log.started_at,
                "finished_at": log.finished_at,
                "duration_ms": log.duration_ms,
                "created_at": _as_dt_or_now(log.created_at),
            },
        )
        self._session.flush()
        row = self._session.get(orm.ModelCallLog, log.model_call_id)
        assert row is not None
        return mappers.model_call_log_to_dto(row)


class PostgresDetectorGateLogRepository(SqliteDetectorGateLogRepository):
    def create_log(self, log: DetectorGateLog) -> DetectorGateLog:
        self._session.execute(
            text(
                """
                INSERT INTO detector_gate_logs(
                  gate_log_id, analysis_job_id, scale_task_id, unit_id, video_id,
                  analysis_scale, segment_start_ms, segment_end_ms, provider, model_id,
                  status, decision_json, frame_evidence_json, evidence_hash,
                  rule_config_hash, media_refs_json, artifact_refs_json, started_at,
                  finished_at, duration_ms, created_at
                ) VALUES (
                  :gate_log_id, :analysis_job_id, :scale_task_id, :unit_id, :video_id,
                  :analysis_scale, :segment_start_ms, :segment_end_ms, :provider, :model_id,
                  :status, CAST(:decision_json AS jsonb), CAST(:frame_evidence_json AS jsonb),
                  :evidence_hash, :rule_config_hash, CAST(:media_refs_json AS jsonb),
                  CAST(:artifact_refs_json AS jsonb), :started_at, :finished_at,
                  :duration_ms, :created_at
                )
                """
            ),
            {
                "gate_log_id": log.gate_log_id,
                "analysis_job_id": log.analysis_job_id,
                "scale_task_id": log.scale_task_id,
                "unit_id": log.unit_id,
                "video_id": log.video_id,
                "analysis_scale": log.analysis_scale.value,
                "segment_start_ms": log.segment_start_ms,
                "segment_end_ms": log.segment_end_ms,
                "provider": log.provider,
                "model_id": log.model_id,
                "status": log.status,
                "decision_json": _json(log.decision),
                "frame_evidence_json": _json(log.frame_evidence),
                "evidence_hash": log.evidence_hash,
                "rule_config_hash": log.rule_config_hash,
                "media_refs_json": _json(log.media_refs),
                "artifact_refs_json": _json(log.artifact_refs),
                "started_at": log.started_at,
                "finished_at": log.finished_at,
                "duration_ms": log.duration_ms,
                "created_at": _as_dt_or_now(log.created_at),
            },
        )
        self._session.flush()
        row = self._session.get(orm.DetectorGateLog, log.gate_log_id)
        assert row is not None
        return mappers.detector_gate_log_to_dto(row)


class PostgresPreVlmGateLogRepository(SqlitePreVlmGateLogRepository):
    def create_log(self, log: PreVlmGateLog) -> PreVlmGateLog:
        self._session.execute(
            text(
                """
                INSERT INTO pre_vlm_gate_logs(
                  gate_log_id, analysis_job_id, scale_task_id, unit_id, video_id,
                  analysis_scale, unit_kind, profile_name, segment_start_ms,
                  segment_end_ms, provider, model_id, status, decision_json,
                  signals_json, frame_evidence_json, evidence_hash, rule_config_hash,
                  suppression_policy, media_refs_json, artifact_refs_json, started_at,
                  finished_at, duration_ms, created_at
                ) VALUES (
                  :gate_log_id, :analysis_job_id, :scale_task_id, :unit_id, :video_id,
                  :analysis_scale, :unit_kind, :profile_name, :segment_start_ms,
                  :segment_end_ms, :provider, :model_id, :status,
                  CAST(:decision_json AS jsonb), CAST(:signals_json AS jsonb),
                  CAST(:frame_evidence_json AS jsonb), :evidence_hash,
                  :rule_config_hash, :suppression_policy, CAST(:media_refs_json AS jsonb),
                  CAST(:artifact_refs_json AS jsonb), :started_at, :finished_at,
                  :duration_ms, :created_at
                )
                """
            ),
            {
                "gate_log_id": log.gate_log_id,
                "analysis_job_id": log.analysis_job_id,
                "scale_task_id": log.scale_task_id,
                "unit_id": log.unit_id,
                "video_id": log.video_id,
                "analysis_scale": log.analysis_scale.value,
                "unit_kind": log.unit_kind,
                "profile_name": log.profile_name,
                "segment_start_ms": log.segment_start_ms,
                "segment_end_ms": log.segment_end_ms,
                "provider": log.provider,
                "model_id": log.model_id,
                "status": log.status,
                "decision_json": _json(log.decision),
                "signals_json": _json(log.signals),
                "frame_evidence_json": _json(log.frame_evidence),
                "evidence_hash": log.evidence_hash,
                "rule_config_hash": log.rule_config_hash,
                "suppression_policy": log.suppression_policy,
                "media_refs_json": _json(log.media_refs),
                "artifact_refs_json": _json(log.artifact_refs),
                "started_at": log.started_at,
                "finished_at": log.finished_at,
                "duration_ms": log.duration_ms,
                "created_at": _as_dt_or_now(log.created_at),
            },
        )
        self._session.flush()
        row = self._session.get(orm.PreVlmGateLog, log.gate_log_id)
        assert row is not None
        return mappers.pre_vlm_gate_log_to_dto(row)


class PostgresTimelineRepository(SqliteTimelineRepository):
    def append_event(self, event: AnalysisTimelineEvent) -> AnalysisTimelineEvent:
        self._session.execute(
            text(
                """
                INSERT INTO analysis_timeline_events(
                  timeline_event_id, trace_id, span_id, parent_span_id, analysis_job_id,
                  task_id, scale_task_id, unit_id, model_call_id, video_id,
                  analysis_scale, unit_kind, segment_start_ms, segment_end_ms,
                  event_name, event_phase, status, attempt_count, occurred_at,
                  duration_ms, error_code, error_message, correlation_json,
                  metadata_json, created_at
                ) VALUES (
                  :timeline_event_id, :trace_id, :span_id, :parent_span_id,
                  :analysis_job_id, :task_id, :scale_task_id, :unit_id,
                  :model_call_id, :video_id, :analysis_scale, :unit_kind,
                  :segment_start_ms, :segment_end_ms, :event_name, :event_phase,
                  :status, :attempt_count, :occurred_at, :duration_ms, :error_code,
                  :error_message, CAST(:correlation_json AS jsonb),
                  CAST(:metadata_json AS jsonb), :created_at
                )
                """
            ),
            {
                "timeline_event_id": event.timeline_event_id,
                "trace_id": event.trace_id,
                "span_id": event.span_id,
                "parent_span_id": event.parent_span_id,
                "analysis_job_id": event.analysis_job_id,
                "task_id": event.task_id,
                "scale_task_id": event.scale_task_id,
                "unit_id": event.unit_id,
                "model_call_id": event.model_call_id,
                "video_id": event.video_id,
                "analysis_scale": event.analysis_scale.value if event.analysis_scale else None,
                "unit_kind": event.unit_kind,
                "segment_start_ms": event.segment_start_ms,
                "segment_end_ms": event.segment_end_ms,
                "event_name": event.event_name,
                "event_phase": event.event_phase,
                "status": event.status,
                "attempt_count": event.attempt_count,
                "occurred_at": event.occurred_at,
                "duration_ms": event.duration_ms,
                "error_code": event.error_code,
                "error_message": event.error_message,
                "correlation_json": _json(event.correlation),
                "metadata_json": _json(event.metadata),
                "created_at": _as_dt_or_now(event.created_at),
            },
        )
        self._session.flush()
        row = self._session.get(orm.AnalysisTimelineEvent, event.timeline_event_id)
        assert row is not None
        return mappers.timeline_event_to_dto(row)

    def append_events(
        self, events: list[AnalysisTimelineEvent]
    ) -> list[AnalysisTimelineEvent]:
        return [self.append_event(event) for event in events]

    def list_by_job(
        self,
        analysis_job_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]:
        if limit <= 0:
            return []
        params: dict[str, Any] = {"analysis_job_id": analysis_job_id, "limit": limit}
        filters = ["analysis_job_id = :analysis_job_id"]
        if since is not None:
            filters.append("occurred_at >= :since")
            params["since"] = since
        if until is not None:
            filters.append("occurred_at <= :until")
            params["until"] = until
        rows = self._session.execute(
            text(
                """
                SELECT *
                FROM analysis_timeline_events
                WHERE """
                + " AND ".join(filters)
                + """
                ORDER BY occurred_at, created_at, timeline_event_id
                LIMIT :limit
                """
            ),
            params,
        )
        return [mappers.timeline_event_to_dto(cast(Any, row)) for row in rows]

    def list_by_trace(
        self,
        trace_id: str,
        *,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]:
        if limit <= 0:
            return []
        rows = self._session.execute(
            text(
                """
                SELECT *
                FROM analysis_timeline_events
                WHERE trace_id = :trace_id
                ORDER BY occurred_at, created_at, timeline_event_id
                LIMIT :limit
                """
            ),
            {"trace_id": trace_id, "limit": limit},
        )
        return [mappers.timeline_event_to_dto(cast(Any, row)) for row in rows]

    def list_all(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100_000,
    ) -> list[AnalysisTimelineEvent]:
        if limit <= 0:
            return []
        params: dict[str, Any] = {"limit": limit}
        filters: list[str] = []
        if since is not None:
            filters.append("occurred_at >= :since")
            params["since"] = since
        if until is not None:
            filters.append("occurred_at <= :until")
            params["until"] = until
        where_sql = "WHERE " + " AND ".join(filters) if filters else ""
        rows = self._session.execute(
            text(
                f"""
                SELECT *
                FROM analysis_timeline_events
                {where_sql}
                ORDER BY occurred_at, created_at, timeline_event_id
                LIMIT :limit
                """
            ),
            params,
        )
        return [mappers.timeline_event_to_dto(cast(Any, row)) for row in rows]


class PostgresPrincipalRepository(SqlitePrincipalRepository):
    def create_principal(self, principal: Principal) -> Principal:
        now = _now()
        self._session.execute(
            text(
                """
                INSERT INTO principals(
                  principal_id, principal_type, tenant_id, external_subject_id, display_name,
                  status, roles_json, groups_json, created_at, updated_at
                ) VALUES (
                  :principal_id, :principal_type, :tenant_id, :external_subject_id, :display_name,
                  :status, CAST(:roles_json AS jsonb), CAST(:groups_json AS jsonb),
                  :created_at, :updated_at
                )
                """
            ),
            {
                "principal_id": principal.principal_id,
                "principal_type": principal.principal_type.value,
                "tenant_id": principal.tenant_id,
                "external_subject_id": principal.external_subject_id,
                "display_name": principal.display_name,
                "status": principal.status,
                "roles_json": _json(principal.roles),
                "groups_json": _json(principal.groups),
                "created_at": now,
                "updated_at": now,
            },
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc) from exc
        row = self._session.get(orm.Principal, principal.principal_id)
        assert row is not None
        return mappers.principal_to_dto(row)


class PostgresAccessPolicyRepository(SqliteAccessPolicyRepository):
    def upsert_access_policy(self, policy: AccessPolicy) -> AccessPolicy:
        self._session.execute(
            text(
                """
                INSERT INTO access_policies(
                  access_policy_id, tenant_id, name, security_level, rules_json,
                  created_at, updated_at
                ) VALUES (
                  :access_policy_id, :tenant_id, :name, :security_level,
                  CAST(:rules_json AS jsonb), :created_at, :updated_at
                )
                ON CONFLICT (access_policy_id) DO UPDATE SET
                  tenant_id = EXCLUDED.tenant_id,
                  name = EXCLUDED.name,
                  security_level = EXCLUDED.security_level,
                  rules_json = EXCLUDED.rules_json,
                  updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "access_policy_id": policy.access_policy_id,
                "tenant_id": policy.tenant_id,
                "name": policy.name,
                "security_level": policy.security_level.value,
                "rules_json": policy.rules.model_dump_json(),
                "created_at": _as_dt_or_now(policy.created_at),
                "updated_at": _as_dt_or_now(policy.updated_at),
            },
        )
        self._session.flush()
        return policy


class PostgresAuditRepository(SqliteAuditRepository):
    def append_event(self, event: AuditEvent) -> AuditEvent:
        self._session.execute(
            text(
                """
                INSERT INTO audit_events(
                  audit_event_id, event_type, request_id, principal_id, session_id,
                  context_id, resource_scope_hash, record_ids_json, video_id, camera_id,
                  metadata_json, created_at
                ) VALUES (
                  :audit_event_id, :event_type, :request_id, :principal_id, :session_id,
                  :context_id, :resource_scope_hash, CAST(:record_ids_json AS jsonb),
                  :video_id, :camera_id, CAST(:metadata_json AS jsonb), :created_at
                )
                """
            ),
            {
                "audit_event_id": event.audit_event_id,
                "event_type": event.event_type,
                "request_id": event.request_id,
                "principal_id": event.principal_id,
                "session_id": event.session_id,
                "context_id": event.context_id,
                "resource_scope_hash": event.resource_scope_hash,
                "record_ids_json": _json(event.record_ids),
                "video_id": event.video_id,
                "camera_id": event.camera_id,
                "metadata_json": _json(event.metadata),
                "created_at": _as_dt_or_now(event.created_at),
            },
        )
        self._session.flush()
        return event


class PostgresSearchContextRepository(SqliteSearchContextRepository):
    def create_context(self, context: SearchContext) -> SearchContext:
        self._session.execute(
            text(
                """
                INSERT INTO search_contexts(
                  context_id, tenant_id, principal_id, session_id, authorized_scope_hash,
                  dataset_revision, mode, default_revision_id, created_at, last_accessed_at,
                  expires_at, status
                ) VALUES (
                  :context_id, :tenant_id, :principal_id, :session_id, :authorized_scope_hash,
                  :dataset_revision, :mode, :default_revision_id, :created_at, :last_accessed_at,
                  :expires_at, :status
                )
                """
            ),
            {
                "context_id": context.context_id,
                "tenant_id": context.tenant_id,
                "principal_id": context.principal_id,
                "session_id": context.session_id,
                "authorized_scope_hash": context.authorized_scope_hash,
                "dataset_revision": context.dataset_revision,
                "mode": context.mode.value,
                "default_revision_id": context.default_revision_id,
                "created_at": _as_dt_or_now(context.created_at),
                "last_accessed_at": _as_dt_or_now(context.last_accessed_at),
                "expires_at": _as_dt_or_now(context.expires_at),
                "status": context.status,
            },
        )
        self._session.flush()
        return context

    def expire_contexts(self, now: datetime) -> int:
        result = self._session.execute(
            text(
                """
                UPDATE search_contexts
                SET status = 'expired'
                WHERE status = 'active' AND expires_at < :now
                """
            ),
            {"now": _as_dt(now)},
        )
        self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    def create_revision(
        self, revision: SearchRevision, candidates: list[SearchCandidate]
    ) -> SearchRevision:
        if self._session.get(orm.SearchRevision, revision.revision_id) is not None:
            raise ConflictError(f"Revision {revision.revision_id} already exists (immutable)")
        self._session.execute(
            text(
                """
                INSERT INTO search_revisions(
                  revision_id, context_id, parent_revision_id, op, op_params_json,
                  candidate_count, facets_json, created_at
                ) VALUES (
                  :revision_id, :context_id, :parent_revision_id, :op,
                  CAST(:op_params_json AS jsonb), :candidate_count,
                  CAST(:facets_json AS jsonb), :created_at
                )
                """
            ),
            {
                "revision_id": revision.revision_id,
                "context_id": revision.context_id,
                "parent_revision_id": revision.parent_revision_id,
                "op": revision.op,
                "op_params_json": _json(revision.op_params),
                "candidate_count": revision.candidate_count,
                "facets_json": _json(revision.facets),
                "created_at": _as_dt_or_now(revision.created_at),
            },
        )
        for candidate in candidates:
            self._session.execute(
                text(
                    """
                    INSERT INTO search_candidates(
                      revision_id, record_id, rank, score, score_detail_json
                    ) VALUES (
                      :revision_id, :record_id, :rank, :score,
                      CAST(:score_detail_json AS jsonb)
                    )
                    """
                ),
                {
                    "revision_id": candidate.revision_id,
                    "record_id": candidate.record_id,
                    "rank": candidate.rank,
                    "score": candidate.score,
                    "score_detail_json": _json(candidate.score_detail),
                },
            )
        self._session.flush()
        return revision

    def replace_default_revision(self, context_id: str, revision_id: str) -> None:
        self._session.execute(
            text(
                """
                UPDATE search_contexts
                SET default_revision_id = :revision_id, last_accessed_at = :last_accessed_at
                WHERE context_id = :context_id
                """
            ),
            {"context_id": context_id, "revision_id": revision_id, "last_accessed_at": _now()},
        )
        self._session.flush()


class PostgresTaskQueueRepository(SqliteTaskQueueRepository):
    def enqueue_task(self, task: Task) -> Task:
        self._session.execute(
            text(
                """
                INSERT INTO analysis_tasks(
                  task_id, schema_version, task_type, payload_json, status, priority,
                  retry_count, max_retries, next_run_at, lease_owner, lease_expires_at,
                  created_at, updated_at, error_code, error_message
                ) VALUES (
                  :task_id, :schema_version, :task_type, CAST(:payload_json AS jsonb),
                  :status, :priority, :retry_count, :max_retries, :next_run_at,
                  :lease_owner, :lease_expires_at, :created_at, :updated_at,
                  :error_code, :error_message
                )
                """
            ),
            {
                "task_id": task.task_id,
                "schema_version": task.schema_version,
                "task_type": task.task_type,
                "payload_json": _json(task.payload),
                "status": task.status,
                "priority": task.priority,
                "retry_count": task.retry_count,
                "max_retries": task.max_retries,
                "next_run_at": _as_dt_or_now(task.next_run_at),
                "lease_owner": task.lease_owner,
                "lease_expires_at": _as_dt(task.lease_expires_at),
                "created_at": _as_dt_or_now(task.created_at),
                "updated_at": _as_dt_or_now(task.updated_at),
                "error_code": task.error_code,
                "error_message": task.error_message,
            },
        )
        self._session.flush()
        return task

    def claim_task(self, worker_id: str, now: datetime, lease_seconds: int) -> Task | None:
        from datetime import timedelta

        now_dt = _as_dt_or_now(now)
        lease_until = now_dt + timedelta(seconds=lease_seconds)
        claimed_id = self._session.execute(
            text(
                """
                UPDATE analysis_tasks
                SET status = 'running', lease_owner = :worker_id,
                    lease_expires_at = :lease_until, updated_at = :now
                WHERE task_id = (
                  SELECT task_id
                  FROM analysis_tasks
                  WHERE next_run_at <= :now
                    AND (
                      status = 'queued'
                      OR (
                        status = 'running'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at < :now
                      )
                    )
                  ORDER BY priority DESC, next_run_at
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
                )
                RETURNING task_id
                """
            ),
            {"worker_id": worker_id, "lease_until": lease_until, "now": now_dt},
        ).scalar_one_or_none()
        self._session.flush()
        if claimed_id is None:
            return None
        row = self._session.get(orm.AnalysisTask, claimed_id)
        return mappers.task_to_dto(row) if row else None

    def refresh_lease(self, task_id: str, worker_id: str, lease_until: datetime) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_tasks
                SET lease_expires_at = :lease_until
                WHERE task_id = :task_id AND lease_owner = :worker_id
                """
            ),
            {"task_id": task_id, "worker_id": worker_id, "lease_until": _as_dt(lease_until)},
        )
        self._session.flush()

    def mark_succeeded(self, task_id: str) -> None:
        # Use raw SQL (not the ORM update construct) so the TIMESTAMPTZ
        # lease_expires_at column is not coerced to VARCHAR: the ORM model
        # declares it String (correct only for SQLite), and on PostgreSQL the
        # ORM renders ``lease_expires_at=$::VARCHAR`` which the TIMESTAMPTZ
        # column rejects (DatatypeMismatch), leaving the job stuck RUNNING.
        self._session.execute(
            text(
                """
                UPDATE analysis_tasks
                SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id},
        )
        self._session.flush()

    def mark_failed(
        self, task_id: str, error_code: str, error_message: str | None = None
    ) -> None:
        # Raw SQL for the same TIMESTAMPTZ-vs-VARCHAR reason as mark_succeeded;
        # the retry/terminal decision mirrors the SQLite adapter.
        self._session.execute(
            text(
                """
                UPDATE analysis_tasks
                SET status = CASE
                        WHEN retry_count < max_retries THEN 'retry_scheduled'
                        ELSE 'failed'
                    END,
                    error_code = :error_code,
                    error_message = :error_message,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE task_id = :task_id
                """
            ),
            {
                "task_id": task_id,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        self._session.flush()

    def schedule_retry(self, task_id: str, next_run_at: datetime) -> None:
        self._session.execute(
            text(
                """
                UPDATE analysis_tasks
                SET status = 'queued', retry_count = retry_count + 1,
                    next_run_at = :next_run_at, lease_owner = NULL, lease_expires_at = NULL
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id, "next_run_at": _as_dt(next_run_at)},
        )
        self._session.flush()


class PostgresObservationReadRepository(SqliteObservationReadRepository):
    """PostgreSQL read adapter with native text ranking replacement for FTS5."""

    def _authorized_observation_where(
        self,
        authorized_scope: AuthorizedScope,
        params: dict[str, object],
    ) -> list[str]:
        allowed_levels = [
            level.value
            for level in SecurityLevel
            if authorized_scope.max_security_level.allows(level)
        ]
        if (
            not authorized_scope.allowed_camera_ids
            or not authorized_scope.allowed_location_ids
            or not authorized_scope.allowed_access_policy_ids
            or not allowed_levels
        ):
            return ["FALSE"]
        params.update(
            {
                "tenant_id": authorized_scope.tenant_id,
                "camera_ids": authorized_scope.allowed_camera_ids,
                "location_ids": authorized_scope.allowed_location_ids,
                "access_policy_ids": authorized_scope.allowed_access_policy_ids,
                "security_levels": allowed_levels,
            }
        )
        return [
            "tenant_id = :tenant_id",
            "camera_id = ANY(CAST(:camera_ids AS text[]))",
            "location_id = ANY(CAST(:location_ids AS text[]))",
            "access_policy_id = ANY(CAST(:access_policy_ids AS text[]))",
            "security_level = ANY(CAST(:security_levels AS text[]))",
        ]

    def _search_observations(
        self,
        authorized_scope: AuthorizedScope,
        *,
        query_text: str | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        camera_ids: list[str] | None = None,
        location_ids: list[str] | None = None,
        video_ids: list[str] | None = None,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        tag_filters: list[str] | None = None,
        limit: int = 100,
    ) -> list[Any]:
        params: dict[str, object] = {"limit": limit}
        where = self._authorized_observation_where(authorized_scope, params)
        if camera_ids:
            where.append("camera_id = ANY(CAST(:filter_camera_ids AS text[]))")
            params["filter_camera_ids"] = camera_ids
        if location_ids:
            where.append("location_id = ANY(CAST(:filter_location_ids AS text[]))")
            params["filter_location_ids"] = location_ids
        if video_ids:
            where.append("video_id = ANY(CAST(:filter_video_ids AS text[]))")
            params["filter_video_ids"] = video_ids
        if analysis_scale_filter:
            where.append("analysis_scale = ANY(CAST(:analysis_scales AS text[]))")
            params["analysis_scales"] = [scale.value for scale in analysis_scale_filter]
        if time_start is not None:
            where.append("observed_end_time >= :time_start")
            params["time_start"] = time_start
        if time_end is not None:
            where.append("observed_start_time <= :time_end")
            params["time_end"] = time_end
        if query_text:
            where.append(
                """(
                static_description_text ILIKE :query_like
                OR dynamic_description_text ILIKE :query_like
                OR tags_json::text ILIKE :query_like
                )"""
            )
            params["query_like"] = f"%{query_text}%"
        if tag_filters:
            for index, tag in enumerate(tag_filters):
                key = f"tag_like_{index}"
                where.append(f"tags_json::text ILIKE :{key}")
                params[key] = f"%{tag}%"
        rows = self._session.execute(
            text(
                f"""
                SELECT *
                FROM observation_records
                WHERE {' AND '.join(where)}
                ORDER BY observed_start_time, record_id
                LIMIT :limit
                """
            ),
            params,
        )
        return list(rows)

    def search_authorized_candidates(
        self,
        authorized_scope: AuthorizedScope,
        *,
        query_text: str | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        camera_ids: list[str] | None = None,
        location_ids: list[str] | None = None,
        video_ids: list[str] | None = None,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        tag_filters: list[str] | None = None,
        limit: int = 100,
    ) -> list[ObservationRecord]:
        rows = self._search_observations(
            authorized_scope,
            query_text=query_text,
            time_start=time_start,
            time_end=time_end,
            camera_ids=camera_ids,
            location_ids=location_ids,
            video_ids=video_ids,
            analysis_scale_filter=analysis_scale_filter,
            tag_filters=tag_filters,
            limit=limit,
        )
        return [mappers.observation_to_dto(cast(Any, row)) for row in rows]

    def authorized_candidate_pool(
        self,
        authorized_scope: AuthorizedScope,
        *,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        camera_ids: list[str] | None = None,
        location_ids: list[str] | None = None,
        video_ids: list[str] | None = None,
        analysis_scale_filter: list[AnalysisScale] | None = None,
        tag_filters: list[str] | None = None,
        limit: int = 1000,
    ) -> list[ObservationRecord]:
        rows = self._search_observations(
            authorized_scope,
            time_start=time_start,
            time_end=time_end,
            camera_ids=camera_ids,
            location_ids=location_ids,
            video_ids=video_ids,
            analysis_scale_filter=analysis_scale_filter,
            tag_filters=tag_filters,
            limit=limit,
        )
        return [mappers.observation_to_dto(cast(Any, row)) for row in rows]

    def fts_rank(
        self, query_text: str, candidate_ids: list[str], *, field: str
    ) -> dict[str, float]:
        if field not in {"static", "dynamic", "tags"}:
            return {}
        return text_index.search(self._session, query_text, candidate_ids, field=field)

    def fts_available(self) -> bool:
        return text_index.text_index_available(self._session)


class PostgresPublicationRepository:
    """Publication adapter using PostgreSQL text index artifacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def publish_records_atomically(
        self, command: PublishObservationRecordsCommand
    ) -> PublicationResult:
        created: list[str] = []
        updated: list[str] = []
        archived: list[str] = []
        now = _now()

        with self._session.begin_nested():
            for record in command.records:
                existing = self._session.scalar(
                    select(orm.ObservationRecord).where(
                        orm.ObservationRecord.video_id == record.video_id,
                        orm.ObservationRecord.segment_start_ms == record.segment_start_ms,
                        orm.ObservationRecord.segment_end_ms == record.segment_end_ms,
                        orm.ObservationRecord.analysis_scale == record.analysis_scale.value,
                    )
                )
                if existing is not None:
                    snapshot = mappers.observation_to_dto(existing)
                    self._session.execute(
                        text(
                            """
                            INSERT INTO observation_record_history(
                              history_id, old_record_id, replaced_by_record_id,
                              archived_by_analysis_job_id, archived_at, archive_reason,
                              record_snapshot_json
                            ) VALUES (
                              :history_id, :old_record_id, :replaced_by_record_id,
                              :archived_by_analysis_job_id, :archived_at, :archive_reason,
                              CAST(:record_snapshot_json AS jsonb)
                            )
                            """
                        ),
                        {
                            "history_id": f"hist_{uuid.uuid4().hex}",
                            "old_record_id": existing.record_id,
                            "replaced_by_record_id": record.record_id,
                            "archived_by_analysis_job_id": command.analysis_job_id,
                            "archived_at": now,
                            "archive_reason": command.archive_reason,
                            "record_snapshot_json": snapshot.model_dump_json(),
                        },
                    )
                    archived.append(existing.record_id)
                    text_index.deindex_record(self._session, existing.record_id)
                    self._session.execute(
                        text("DELETE FROM observation_records WHERE record_id = :record_id"),
                        {"record_id": existing.record_id},
                    )
                    self._session.flush()
                    updated.append(record.record_id)
                else:
                    created.append(record.record_id)

                self._session.execute(
                    text(
                        """
                        INSERT INTO observation_records(
                          record_id, tenant_id, video_id, analysis_job_id, analysis_scale,
                          segment_start_ms, segment_end_ms, observed_start_time,
                          observed_end_time, camera_id, location_id, static_description_text,
                          dynamic_description_text, tags_json, clip_uri, thumbnail_uri,
                          attributes_json, access_policy_id, security_level, model_version,
                          prompt_version, pipeline_version, created_at, updated_at
                        ) VALUES (
                          :record_id, :tenant_id, :video_id, :analysis_job_id, :analysis_scale,
                          :segment_start_ms, :segment_end_ms, :observed_start_time,
                          :observed_end_time, :camera_id, :location_id, :static_description_text,
                          :dynamic_description_text, CAST(:tags_json AS jsonb), :clip_uri,
                          :thumbnail_uri, CAST(:attributes_json AS jsonb), :access_policy_id,
                          :security_level, :model_version, :prompt_version, :pipeline_version,
                          :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "record_id": record.record_id,
                        "tenant_id": record.tenant_id,
                        "video_id": record.video_id,
                        "analysis_job_id": record.analysis_job_id,
                        "analysis_scale": record.analysis_scale.value,
                        "segment_start_ms": record.segment_start_ms,
                        "segment_end_ms": record.segment_end_ms,
                        "observed_start_time": record.observed_start_time,
                        "observed_end_time": record.observed_end_time,
                        "camera_id": record.camera_id,
                        "location_id": record.location_id,
                        "static_description_text": record.static_description_text,
                        "dynamic_description_text": record.dynamic_description_text,
                        "tags_json": _json(record.tags),
                        "clip_uri": record.clip_uri,
                        "thumbnail_uri": record.thumbnail_uri,
                        "attributes_json": _json(record.attributes),
                        "access_policy_id": record.access_policy_id,
                        "security_level": record.security_level.value,
                        "model_version": record.model_version,
                        "prompt_version": record.prompt_version,
                        "pipeline_version": record.pipeline_version,
                        "created_at": _as_dt_or_now(record.created_at),
                        "updated_at": _as_dt_or_now(record.updated_at),
                    },
                )
                self._session.flush()
                text_index.index_record(
                    self._session,
                    record_id=record.record_id,
                    static_text=record.static_description_text,
                    dynamic_text=record.dynamic_description_text,
                    tags=record.tags,
                )

            job = self._session.get(orm.AnalysisJob, command.analysis_job_id)
            if job is not None:
                self._session.execute(
                    text(
                        """
                        UPDATE analysis_jobs
                        SET created_record_ids_json = CAST(:created AS jsonb),
                            updated_record_ids_json = CAST(:updated AS jsonb),
                            archived_record_ids_json = CAST(:archived AS jsonb)
                        WHERE analysis_job_id = :analysis_job_id
                        """
                    ),
                    {
                        "analysis_job_id": command.analysis_job_id,
                        "created": _json(_json_list(job.created_record_ids_json) + created),
                        "updated": _json(_json_list(job.updated_record_ids_json) + updated),
                        "archived": _json(_json_list(job.archived_record_ids_json) + archived),
                    },
                )

            audit = AuditEvent(
                audit_event_id=f"audit_{uuid.uuid4().hex}",
                event_type="publication_succeeded",
                record_ids=created + updated,
                metadata={"analysis_job_id": command.analysis_job_id},
            )
            self._session.execute(
                text(
                    """
                    INSERT INTO audit_events(
                      audit_event_id, event_type, request_id, principal_id, session_id,
                      context_id, resource_scope_hash, record_ids_json, video_id, camera_id,
                      metadata_json, created_at
                    ) VALUES (
                      :audit_event_id, :event_type, :request_id, :principal_id, :session_id,
                      :context_id, :resource_scope_hash, CAST(:record_ids_json AS jsonb),
                      :video_id, :camera_id, CAST(:metadata_json AS jsonb), :created_at
                    )
                    """
                ),
                {
                    "audit_event_id": audit.audit_event_id,
                    "event_type": audit.event_type,
                    "request_id": audit.request_id,
                    "principal_id": audit.principal_id,
                    "session_id": audit.session_id,
                    "context_id": audit.context_id,
                    "resource_scope_hash": audit.resource_scope_hash,
                    "record_ids_json": _json(audit.record_ids),
                    "video_id": audit.video_id,
                    "camera_id": audit.camera_id,
                    "metadata_json": _json(audit.metadata),
                    "created_at": _as_dt_or_now(audit.created_at),
                },
            )
            self._session.flush()

        return PublicationResult(
            analysis_job_id=command.analysis_job_id,
            created_record_ids=created,
            updated_record_ids=updated,
            archived_record_ids=archived,
        )


class PostgresIndexRepository:
    """pgvector IndexPort adapter over ``observation_vectors``."""

    def __init__(self, session: Session, *, dimension: int) -> None:
        self._session = session
        self._dimension = dimension

    def upsert_vectors(self, vectors: list[StoredVector]) -> int:
        if not vectors:
            return 0
        for vector in vectors:
            if vector.dimension != self._dimension:
                raise ValueError(
                    "embedding dimension mismatch: "
                    f"got {vector.dimension}, expected {self._dimension}"
                )
            metadata = dict(vector.metadata)
            self._session.execute(
                text(
                    """
                    INSERT INTO observation_vectors(
                      record_id, vector_type, model_id, dimension, embedding, metadata_json
                    ) VALUES (
                      :record_id, :vector_type, :model_id, :dimension,
                      CAST(:embedding AS vector), CAST(:metadata_json AS jsonb)
                    )
                    ON CONFLICT (record_id, vector_type, model_id) DO UPDATE SET
                      dimension = EXCLUDED.dimension,
                      embedding = EXCLUDED.embedding,
                      metadata_json = EXCLUDED.metadata_json,
                      updated_at = now()
                    """
                ),
                {
                    "record_id": vector.record_id,
                    "vector_type": vector.vector_type,
                    "model_id": vector.model_id,
                    "dimension": vector.dimension,
                    "embedding": serialize_pgvector(
                        vector.embedding, expected_dimension=self._dimension
                    ),
                    "metadata_json": json.dumps(metadata),
                },
            )
        self._session.flush()
        return len(vectors)

    def get_vectors_for_records(
        self, record_ids: list[str], *, vector_type: str | None = None
    ) -> list[StoredVector]:
        if not record_ids:
            return []
        params: dict[str, object] = {"record_ids": record_ids}
        where = "record_id = ANY(CAST(:record_ids AS text[]))"
        if vector_type is not None:
            where += " AND vector_type = :vector_type"
            params["vector_type"] = vector_type
        rows = self._session.execute(
            text(
                f"""
                SELECT record_id, vector_type, model_id, dimension,
                       embedding::text AS embedding_text, metadata_json::text AS metadata_text
                FROM observation_vectors
                WHERE {where}
                ORDER BY record_id, vector_type, model_id
                """
            ),
            params,
        )
        out: list[StoredVector] = []
        for row in rows:
            embedding_text = str(row.embedding_text).strip("[]")
            embedding = [float(x) for x in embedding_text.split(",") if x]
            metadata = json.loads(row.metadata_text or "{}")
            out.append(
                StoredVector(
                    record_id=str(row.record_id),
                    vector_type=str(row.vector_type),
                    embedding=embedding,
                    model_id=str(row.model_id),
                    dimension=int(row.dimension),
                    metadata=metadata,
                )
            )
        return out

    def delete_vectors_for_records(self, record_ids: list[str]) -> int:
        if not record_ids:
            return 0
        result = self._session.execute(
            delete(orm.ObservationVector).where(orm.ObservationVector.record_id.in_(record_ids))
        )
        self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)
