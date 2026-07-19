"""SQLite admin/general repository adapters.

Camera, VideoSource, AnalysisJob, AnalysisScaleTask, HighFreqTrigger,
Principal, AccessPolicy. These adapters own writes for management/analysis
metadata. They never write active ObservationRecord (only the publication
adapter does).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
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
from cctv_memory.contracts.auth import AccessPolicy, Principal
from cctv_memory.contracts.pre_vlm_gate import PreVlmGateLog
from cctv_memory.contracts.video import (
    CameraDevice,
    CameraLocation,
    SubmitVideoSourceRequest,
    VideoSource,
)
from cctv_memory.domain.enums import TaskStatus
from cctv_memory.infrastructure.db import mappers
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.infrastructure.db.repositories._helpers import map_integrity_error, upsert_by_pk
from cctv_memory.repositories.types import IdempotencyConflictError, Page


class SqliteCameraRepository:
    """CameraRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_location(self, location_id: str) -> CameraLocation | None:
        row = self._session.get(orm.CameraLocation, location_id)
        return mappers.location_to_dto(row) if row else None

    def list_locations(
        self, cursor: str | None = None, limit: int = 50
    ) -> Page[CameraLocation]:
        rows = list(
            self._session.scalars(
                select(orm.CameraLocation).order_by(orm.CameraLocation.location_id).limit(limit)
            )
        )
        return Page(items=[mappers.location_to_dto(r) for r in rows])

    def upsert_location(self, location: CameraLocation) -> CameraLocation:
        """Idempotent, concurrency-safe upsert (write-first + SAVEPOINT).

        See ``upsert_by_pk``: the write runs first inside a savepoint (no preceding
        read, so SQLite ``busy_timeout`` serializes concurrent writers and the
        read->write upgrade BUSY is avoided); a racing first-INSERT loser rolls the
        savepoint back, reads the winner row, and applies the incoming fields as an
        idempotent update. The outer session is never poisoned and no raw
        IntegrityError escapes (task cctv-memory-20260617-1118; capability §3.2,
        adapter §8). ``created_at`` is preserved on the update path.
        """
        incoming = mappers.location_to_orm(location)

        def _apply(existing: orm.CameraLocation) -> None:
            existing.tenant_id = incoming.tenant_id
            existing.building = incoming.building
            existing.floor = incoming.floor
            existing.area = incoming.area
            existing.room_or_zone = incoming.room_or_zone
            existing.location_desc = incoming.location_desc
            existing.access_policy_id = incoming.access_policy_id
            existing.security_level = incoming.security_level
            existing.updated_at = incoming.updated_at

        row = upsert_by_pk(
            self._session,
            orm.CameraLocation,
            orm.CameraLocation.location_id == location.location_id,
            build_new=lambda: incoming,
            apply_update=_apply,
        )
        return mappers.location_to_dto(row)

    def get_camera(self, camera_id: str) -> CameraDevice | None:
        row = self._session.get(orm.CameraDevice, camera_id)
        return mappers.camera_to_dto(row) if row else None

    def list_cameras(self, cursor: str | None = None, limit: int = 50) -> Page[CameraDevice]:
        rows = list(
            self._session.scalars(
                select(orm.CameraDevice).order_by(orm.CameraDevice.camera_id).limit(limit)
            )
        )
        return Page(items=[mappers.camera_to_dto(r) for r in rows])

    def upsert_camera(self, camera: CameraDevice) -> CameraDevice:
        """Idempotent, concurrency-safe upsert (write-first + SAVEPOINT).

        Same discipline as ``upsert_location``: a concurrent first-provision of the
        same ``camera_id`` cannot leak a raw IntegrityError, cannot poison the
        outer session, and cannot hit a read->write-upgrade BUSY
        (task cctv-memory-20260617-1118). ``created_at`` is preserved on update.
        """
        incoming = mappers.camera_to_orm(camera)

        def _apply(existing: orm.CameraDevice) -> None:
            existing.tenant_id = incoming.tenant_id
            existing.camera_name = incoming.camera_name
            existing.location_id = incoming.location_id
            existing.manufacturer = incoming.manufacturer
            existing.model = incoming.model
            existing.serial_number = incoming.serial_number
            existing.install_position_desc = incoming.install_position_desc
            existing.stream_uri = incoming.stream_uri
            existing.access_policy_id = incoming.access_policy_id
            existing.status = incoming.status
            existing.updated_at = incoming.updated_at

        row = upsert_by_pk(
            self._session,
            orm.CameraDevice,
            orm.CameraDevice.camera_id == camera.camera_id,
            build_new=lambda: incoming,
            apply_update=_apply,
        )
        return mappers.camera_to_dto(row)


class SqliteVideoSourceRepository:
    """VideoSourceRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_or_get_by_idempotency(
        self, request: SubmitVideoSourceRequest, *, video_id: str
    ) -> VideoSource:
        # Idempotency anchor: (camera_id, video_start_time) unique constraint.
        existing = self._session.scalar(
            select(orm.VideoSource).where(
                orm.VideoSource.camera_id == request.camera_id,
                orm.VideoSource.video_start_time == request.video_start_time.isoformat(),
            )
        )
        if existing is not None:
            if existing.source_uri != request.source_uri:
                raise IdempotencyConflictError(
                    "VideoSource exists for (camera_id, video_start_time) with different payload"
                )
            return mappers.video_to_dto(existing)

        dto = VideoSource(
            video_id=video_id,
            source_type=request.source_type,
            source_uri=request.source_uri,
            camera_id=request.camera_id,
            video_start_time=request.video_start_time,
            external_source_id=request.external_source_id,
            source_status="pending",
        )
        row = mappers.video_to_orm(dto)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        return mappers.video_to_dto(row)

    def get_by_id(self, video_id: str) -> VideoSource | None:
        row = self._session.get(orm.VideoSource, video_id)
        return mappers.video_to_dto(row) if row else None

    def get_authorized_by_id(self, video_id: str, authorized_scope: object) -> VideoSource | None:
        from cctv_memory.contracts.auth import AuthorizedScope
        from cctv_memory.infrastructure.db.repositories._helpers import authorized_video_filter

        assert isinstance(authorized_scope, AuthorizedScope)
        row = self._session.scalar(
            select(orm.VideoSource).where(
                orm.VideoSource.video_id == video_id,
                authorized_video_filter(authorized_scope),
            )
        )
        return mappers.video_to_dto(row) if row else None

    def list_authorized(
        self, authorized_scope: object, cursor: str | None = None, limit: int = 50
    ) -> Page[VideoSource]:
        from cctv_memory.contracts.auth import AuthorizedScope
        from cctv_memory.infrastructure.db.repositories._helpers import authorized_video_filter

        assert isinstance(authorized_scope, AuthorizedScope)
        rows = list(
            self._session.scalars(
                select(orm.VideoSource)
                .where(authorized_video_filter(authorized_scope))
                .order_by(orm.VideoSource.video_id)
                .limit(limit)
            )
        )
        return Page(items=[mappers.video_to_dto(r) for r in rows])

    def mark_status(self, video_id: str, status: str, error: str | None = None) -> None:
        row = self._session.get(orm.VideoSource, video_id)
        if row is not None:
            row.source_status = status
            self._session.flush()

    def update_probe_metadata(
        self, video_id: str, *, duration_ms: int, video_end_time: datetime
    ) -> None:
        row = self._session.get(orm.VideoSource, video_id)
        if row is not None:
            row.duration_ms = duration_ms
            # SQLite stores video_end_time as ISO text; convert at the boundary.
            row.video_end_time = video_end_time.isoformat()
            self._session.flush()


class SqliteAnalysisJobRepository:
    """AnalysisJobRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_job(self, job: AnalysisJob) -> AnalysisJob:
        existing = self._session.scalar(
            select(orm.AnalysisJob).where(
                orm.AnalysisJob.idempotency_key == job.idempotency_key
            )
        )
        if existing is not None:
            if existing.video_id != job.video_id:
                raise IdempotencyConflictError(
                    "AnalysisJob idempotency_key reused with different video_id"
                )
            return mappers.job_to_dto(existing)
        row = mappers.job_to_orm(job)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        return mappers.job_to_dto(row)

    def get_job(self, analysis_job_id: str) -> AnalysisJob | None:
        row = self._session.get(orm.AnalysisJob, analysis_job_id)
        return mappers.job_to_dto(row) if row else None

    def get_by_idempotency_key(self, idempotency_key: str) -> AnalysisJob | None:
        row = self._session.scalar(
            select(orm.AnalysisJob).where(orm.AnalysisJob.idempotency_key == idempotency_key)
        )
        return mappers.job_to_dto(row) if row else None

    def get_jobs_for_video(self, video_id: str) -> list[AnalysisJob]:
        rows = self._session.scalars(
            select(orm.AnalysisJob).where(orm.AnalysisJob.video_id == video_id)
        )
        return [mappers.job_to_dto(r) for r in rows]

    def list_jobs(self, cursor: str | None = None, limit: int = 50) -> Page[AnalysisJob]:
        rows = list(
            self._session.scalars(
                select(orm.AnalysisJob).order_by(orm.AnalysisJob.created_at).limit(limit)
            )
        )
        return Page(items=[mappers.job_to_dto(r) for r in rows])

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
        row = self._session.get(orm.AnalysisJob, analysis_job_id)
        if row is None:
            return
        row.job_status = status
        if started_at is not None:
            row.started_at = started_at
        if finished_at is not None:
            row.finished_at = finished_at
        row.error_code = error_code
        row.error_message = error_message
        self._session.flush()

    def append_record_publish_summary(
        self,
        analysis_job_id: str,
        created_ids: list[str],
        updated_ids: list[str],
        archived_ids: list[str],
    ) -> None:
        import json

        row = self._session.get(orm.AnalysisJob, analysis_job_id)
        if row is None:
            return
        row.created_record_ids_json = json.dumps(
            json.loads(row.created_record_ids_json) + created_ids
        )
        row.updated_record_ids_json = json.dumps(
            json.loads(row.updated_record_ids_json) + updated_ids
        )
        row.archived_record_ids_json = json.dumps(
            json.loads(row.archived_record_ids_json) + archived_ids
        )
        self._session.flush()


class SqliteAnalysisScaleTaskRepository:
    """AnalysisScaleTaskRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_scale_task(self, task: AnalysisScaleTask) -> AnalysisScaleTask:
        row = mappers.scale_task_to_orm(task)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc) from exc
        return mappers.scale_task_to_dto(row)

    def get_scale_task(self, scale_task_id: str) -> AnalysisScaleTask | None:
        row = self._session.get(orm.AnalysisScaleTask, scale_task_id)
        return mappers.scale_task_to_dto(row) if row else None

    def get_by_job_and_scale(
        self, analysis_job_id: str, analysis_scale: str
    ) -> AnalysisScaleTask | None:
        row = self._session.scalar(
            select(orm.AnalysisScaleTask).where(
                orm.AnalysisScaleTask.analysis_job_id == analysis_job_id,
                orm.AnalysisScaleTask.analysis_scale == analysis_scale,
            )
        )
        return mappers.scale_task_to_dto(row) if row else None

    def list_by_job(self, analysis_job_id: str) -> list[AnalysisScaleTask]:
        rows = self._session.scalars(
            select(orm.AnalysisScaleTask)
            .where(orm.AnalysisScaleTask.analysis_job_id == analysis_job_id)
            .order_by(orm.AnalysisScaleTask.created_at)
        )
        return [mappers.scale_task_to_dto(r) for r in rows]

    def update_counters(
        self, scale_task_id: str, total: int, succeeded: int, failed: int
    ) -> None:
        row = self._session.get(orm.AnalysisScaleTask, scale_task_id)
        if row is None:
            return
        row.total_units = total
        row.succeeded_units = succeeded
        row.failed_units = failed
        self._session.flush()

    def update_status(
        self,
        scale_task_id: str,
        status: str,
        *,
        error_code: str | None = None,
        skipped_reason: str | None = None,
    ) -> None:
        row = self._session.get(orm.AnalysisScaleTask, scale_task_id)
        if row is None:
            return
        row.status = status
        row.error_code = error_code
        row.skipped_reason = skipped_reason
        self._session.flush()


class SqliteHighFreqTriggerRepository:
    """HighFreqTriggerRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_or_get_by_idempotency(self, trigger: HighFreqTrigger) -> HighFreqTrigger:
        existing = self._session.scalar(
            select(orm.HighFreqTrigger).where(
                orm.HighFreqTrigger.idempotency_key == trigger.idempotency_key
            )
        )
        if existing is not None:
            return mappers.trigger_to_dto(existing)
        row = mappers.trigger_to_orm(trigger)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        return mappers.trigger_to_dto(row)

    def get_trigger(self, trigger_id: str) -> HighFreqTrigger | None:
        row = self._session.get(orm.HighFreqTrigger, trigger_id)
        return mappers.trigger_to_dto(row) if row else None

    def list_by_job(self, analysis_job_id: str) -> list[HighFreqTrigger]:
        rows = self._session.scalars(
            select(orm.HighFreqTrigger).where(
                orm.HighFreqTrigger.analysis_job_id == analysis_job_id
            )
        )
        return [mappers.trigger_to_dto(r) for r in rows]

    def update_status(
        self, trigger_id: str, status: str, error_code: str | None = None
    ) -> None:
        row = self._session.get(orm.HighFreqTrigger, trigger_id)
        if row is None:
            return
        row.status = status
        row.error_code = error_code
        self._session.flush()


class SqliteAnalysisUnitRepository:
    """AnalysisUnit repository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_or_get_by_idempotency(self, unit: AnalysisUnit) -> AnalysisUnit:
        existing = self._session.scalar(
            select(orm.AnalysisUnit).where(
                orm.AnalysisUnit.idempotency_key == unit.idempotency_key
            )
        )
        if existing is not None:
            return mappers.analysis_unit_to_dto(existing)
        row = mappers.analysis_unit_to_orm(unit)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc, idempotency=True) from exc
        return mappers.analysis_unit_to_dto(row)

    def get_unit(self, unit_id: str) -> AnalysisUnit | None:
        row = self._session.get(orm.AnalysisUnit, unit_id)
        return mappers.analysis_unit_to_dto(row) if row else None

    def list_by_scale_task(self, scale_task_id: str) -> list[AnalysisUnit]:
        rows = self._session.scalars(
            select(orm.AnalysisUnit)
            .where(orm.AnalysisUnit.scale_task_id == scale_task_id)
            .order_by(orm.AnalysisUnit.window_index)
        )
        return [mappers.analysis_unit_to_dto(r) for r in rows]

    def mark_running(self, unit_id: str, *, model_call_id: str | None = None) -> None:

        row = self._session.get(orm.AnalysisUnit, unit_id)
        if row is None:
            return
        row.status = TaskStatus.RUNNING.value
        row.attempt_count = row.attempt_count + 1
        row.latest_model_call_id = model_call_id
        row.started_at = row.started_at or datetime.now().astimezone().isoformat()
        self._session.flush()

    def mark_succeeded(
        self, unit_id: str, *, model_call_id: str | None, record_ids: list[str],
        attempt_count: int | None = None,
    ) -> None:
        import json

        row = self._session.get(orm.AnalysisUnit, unit_id)
        if row is None:
            return
        row.status = TaskStatus.SUCCEEDED.value
        row.latest_model_call_id = model_call_id
        row.successful_model_call_id = model_call_id
        row.produced_record_ids_json = json.dumps(record_ids)
        if attempt_count is not None:
            row.attempt_count = attempt_count
        row.finished_at = datetime.now().astimezone().isoformat()
        row.last_error_code = None
        row.last_error_message = None
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

        row = self._session.get(orm.AnalysisUnit, unit_id)
        if row is None:
            return
        row.status = TaskStatus.FAILED.value
        row.latest_model_call_id = model_call_id or row.latest_model_call_id
        if attempt_count is not None:
            row.attempt_count = attempt_count
        row.last_error_code = error_code
        row.last_error_message = error_message
        row.finished_at = datetime.now().astimezone().isoformat()
        self._session.flush()

    def mark_skipped(
        self,
        unit_id: str,
        *,
        skipped_reason: str,
        model_call_id: str | None = None,
    ) -> None:

        row = self._session.get(orm.AnalysisUnit, unit_id)
        if row is None:
            return
        row.status = TaskStatus.SKIPPED.value
        row.latest_model_call_id = model_call_id or row.latest_model_call_id
        # skipped is a benign terminal state (e.g. insufficient_frames): record the
        # reason in last_error_code for auditability without implying a hard failure.
        row.last_error_code = skipped_reason
        row.last_error_message = None
        row.finished_at = datetime.now().astimezone().isoformat()
        self._session.flush()

    def list_stale_running(
        self, *, cutoff: datetime, limit: int
    ) -> list[AnalysisUnit]:
        """Return units stuck ``running`` with ``started_at`` older than the cutoff.

        Bounded + index-backed orphan-recovery query (task cctv-memory-20260612-1854):
        filters ``status='running' AND started_at < cutoff`` ordered by
        ``started_at`` and capped at ``limit`` rows. Backed by index
        ``idx_units_status_started(status, started_at)`` so it is O(log N + K),
        K = batch size — never a full-table scan. Rows with NULL started_at are
        excluded (they have not begun and are not orphan-stale).
        """
        if limit <= 0:
            return []
        # SQLite stores started_at as ISO text; convert at the adapter boundary.
        cutoff_iso = cutoff.isoformat()
        rows = self._session.scalars(
            select(orm.AnalysisUnit)
            .where(
                orm.AnalysisUnit.status == TaskStatus.RUNNING.value,
                orm.AnalysisUnit.started_at.is_not(None),
                orm.AnalysisUnit.started_at < cutoff_iso,
            )
            .order_by(orm.AnalysisUnit.started_at)
            .limit(limit)
        )
        return [mappers.analysis_unit_to_dto(r) for r in rows]


class SqliteModelCallLogRepository:
    """ModelCallLog repository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_log(self, log: ModelCallLog) -> ModelCallLog:
        row = mappers.model_call_log_to_orm(log)
        self._session.add(row)
        self._session.flush()
        return mappers.model_call_log_to_dto(row)

    def get_log(self, model_call_id: str) -> ModelCallLog | None:
        row = self._session.get(orm.ModelCallLog, model_call_id)
        return mappers.model_call_log_to_dto(row) if row else None

    def list_by_unit(self, unit_id: str) -> list[ModelCallLog]:
        rows = self._session.scalars(
            select(orm.ModelCallLog)
            .where(orm.ModelCallLog.unit_id == unit_id)
            .order_by(orm.ModelCallLog.created_at)
        )
        return [mappers.model_call_log_to_dto(r) for r in rows]

    def list_by_job(self, analysis_job_id: str) -> list[ModelCallLog]:
        rows = self._session.scalars(
            select(orm.ModelCallLog)
            .where(orm.ModelCallLog.analysis_job_id == analysis_job_id)
            .order_by(orm.ModelCallLog.created_at)
        )
        return [mappers.model_call_log_to_dto(r) for r in rows]


class SqliteDetectorGateLogRepository:
    """DetectorGateLog repository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_log(self, log: DetectorGateLog) -> DetectorGateLog:
        row = mappers.detector_gate_log_to_orm(log)
        self._session.add(row)
        self._session.flush()
        return mappers.detector_gate_log_to_dto(row)

    def get_log(self, gate_log_id: str) -> DetectorGateLog | None:
        row = self._session.get(orm.DetectorGateLog, gate_log_id)
        return mappers.detector_gate_log_to_dto(row) if row else None

    def list_by_unit(self, unit_id: str) -> list[DetectorGateLog]:
        rows = self._session.scalars(
            select(orm.DetectorGateLog)
            .where(orm.DetectorGateLog.unit_id == unit_id)
            .order_by(orm.DetectorGateLog.created_at)
        )
        return [mappers.detector_gate_log_to_dto(r) for r in rows]


class SqlitePreVlmGateLogRepository:
    """PreVlmGateLog repository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_log(self, log: PreVlmGateLog) -> PreVlmGateLog:
        row = mappers.pre_vlm_gate_log_to_orm(log)
        self._session.add(row)
        self._session.flush()
        return mappers.pre_vlm_gate_log_to_dto(row)

    def get_log(self, gate_log_id: str) -> PreVlmGateLog | None:
        row = self._session.get(orm.PreVlmGateLog, gate_log_id)
        return mappers.pre_vlm_gate_log_to_dto(row) if row else None

    def list_by_unit(self, unit_id: str) -> list[PreVlmGateLog]:
        rows = self._session.scalars(
            select(orm.PreVlmGateLog)
            .where(orm.PreVlmGateLog.unit_id == unit_id)
            .order_by(orm.PreVlmGateLog.created_at)
        )
        return [mappers.pre_vlm_gate_log_to_dto(r) for r in rows]

    def list_by_job(self, analysis_job_id: str) -> list[PreVlmGateLog]:
        rows = self._session.scalars(
            select(orm.PreVlmGateLog)
            .where(orm.PreVlmGateLog.analysis_job_id == analysis_job_id)
            .order_by(orm.PreVlmGateLog.created_at)
        )
        return [mappers.pre_vlm_gate_log_to_dto(r) for r in rows]


class SqlitePrincipalRepository:
    """PrincipalRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_principal(self, principal_id: str) -> Principal | None:
        row = self._session.get(orm.Principal, principal_id)
        return mappers.principal_to_dto(row) if row else None

    def get_principal_by_external_subject(self, external_subject_id: str) -> Principal | None:
        row = self._session.scalar(
            select(orm.Principal).where(
                orm.Principal.external_subject_id == external_subject_id
            )
        )
        return mappers.principal_to_dto(row) if row else None

    def create_principal(self, principal: Principal) -> Principal:
        row = mappers.principal_to_orm(principal)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise map_integrity_error(exc) from exc
        return mappers.principal_to_dto(row)

    def set_principal_status(self, principal_id: str, status: str) -> None:
        row = self._session.get(orm.Principal, principal_id)
        if row is None:
            return
        row.status = status
        self._session.flush()


class SqliteAccessPolicyRepository:
    """AccessPolicyRepository SQLite adapter."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_access_policy(self, access_policy_id: str) -> AccessPolicy | None:
        row = self._session.get(orm.AccessPolicy, access_policy_id)
        return mappers.policy_to_dto(row) if row else None

    def list_access_policies(self) -> list[AccessPolicy]:
        rows = self._session.scalars(select(orm.AccessPolicy))
        return [mappers.policy_to_dto(r) for r in rows]

    def upsert_access_policy(self, policy: AccessPolicy) -> AccessPolicy:
        existing = self._session.get(orm.AccessPolicy, policy.access_policy_id)
        merged = mappers.policy_to_orm(policy)
        if existing is not None:
            merged.created_at = existing.created_at
        self._session.merge(merged)
        self._session.flush()
        return policy
