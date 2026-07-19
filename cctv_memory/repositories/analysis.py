"""AnalysisJob / AnalysisScaleTask / HighFreqTrigger ports.

repository-port-contract §4-§6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cctv_memory.contracts.analysis import (
    AnalysisJob,
    AnalysisScaleTask,
    AnalysisUnit,
    DetectorGateLog,
    HighFreqTrigger,
    ModelCallLog,
)
from cctv_memory.contracts.pre_vlm_gate import PreVlmGateLog
from cctv_memory.repositories.types import Page


@runtime_checkable
class AnalysisJobRepository(Protocol):
    """AnalysisJob persistence port (repository-port-contract §4)."""

    def create_job(self, job: AnalysisJob) -> AnalysisJob: ...

    def get_job(self, analysis_job_id: str) -> AnalysisJob | None: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> AnalysisJob | None: ...

    def get_jobs_for_video(self, video_id: str) -> list[AnalysisJob]: ...

    def list_jobs(self, cursor: str | None = None, limit: int = 50) -> Page[AnalysisJob]: ...

    def update_status(
        self,
        analysis_job_id: str,
        status: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None: ...

    def append_record_publish_summary(
        self,
        analysis_job_id: str,
        created_ids: list[str],
        updated_ids: list[str],
        archived_ids: list[str],
    ) -> None: ...


@runtime_checkable
class AnalysisScaleTaskRepository(Protocol):
    """AnalysisScaleTask persistence port (repository-port-contract §5)."""

    def create_scale_task(self, task: AnalysisScaleTask) -> AnalysisScaleTask: ...

    def get_scale_task(self, scale_task_id: str) -> AnalysisScaleTask | None: ...

    def get_by_job_and_scale(
        self, analysis_job_id: str, analysis_scale: str
    ) -> AnalysisScaleTask | None: ...

    def list_by_job(self, analysis_job_id: str) -> list[AnalysisScaleTask]: ...

    def update_counters(
        self, scale_task_id: str, total: int, succeeded: int, failed: int
    ) -> None: ...

    def update_status(
        self,
        scale_task_id: str,
        status: str,
        *,
        error_code: str | None = None,
        skipped_reason: str | None = None,
    ) -> None: ...


@runtime_checkable
class HighFreqTriggerRepository(Protocol):
    """HighFreqTrigger persistence port (repository-port-contract §6)."""

    def create_or_get_by_idempotency(self, trigger: HighFreqTrigger) -> HighFreqTrigger: ...

    def get_trigger(self, trigger_id: str) -> HighFreqTrigger | None: ...

    def list_by_job(self, analysis_job_id: str) -> list[HighFreqTrigger]: ...

    def update_status(
        self, trigger_id: str, status: str, error_code: str | None = None
    ) -> None: ...


@runtime_checkable
class AnalysisUnitRepository(Protocol):
    """Analysis unit persistence port."""

    def create_or_get_by_idempotency(self, unit: AnalysisUnit) -> AnalysisUnit: ...

    def get_unit(self, unit_id: str) -> AnalysisUnit | None: ...

    def list_by_scale_task(self, scale_task_id: str) -> list[AnalysisUnit]: ...

    def mark_running(self, unit_id: str, *, model_call_id: str | None = None) -> None: ...

    def mark_succeeded(
        self,
        unit_id: str,
        *,
        model_call_id: str | None,
        record_ids: list[str],
        attempt_count: int | None = None,
    ) -> None: ...

    def mark_failed(
        self,
        unit_id: str,
        *,
        error_code: str,
        error_message: str | None = None,
        model_call_id: str | None = None,
        attempt_count: int | None = None,
    ) -> None: ...

    def mark_skipped(
        self,
        unit_id: str,
        *,
        skipped_reason: str,
        model_call_id: str | None = None,
    ) -> None: ...

    def list_stale_running(
        self, *, cutoff: datetime, limit: int
    ) -> list[AnalysisUnit]: ...


@runtime_checkable
class ModelCallLogRepository(Protocol):
    """Model-call log persistence port."""

    def create_log(self, log: ModelCallLog) -> ModelCallLog: ...

    def get_log(self, model_call_id: str) -> ModelCallLog | None: ...

    def list_by_unit(self, unit_id: str) -> list[ModelCallLog]: ...

    def list_by_job(self, analysis_job_id: str) -> list[ModelCallLog]: ...


@runtime_checkable
class DetectorGateLogRepository(Protocol):
    """Detector gate log persistence port."""

    def create_log(self, log: DetectorGateLog) -> DetectorGateLog: ...

    def get_log(self, gate_log_id: str) -> DetectorGateLog | None: ...

    def list_by_unit(self, unit_id: str) -> list[DetectorGateLog]: ...


@runtime_checkable
class PreVlmGateLogRepository(Protocol):
    """Generic pre-VLM gate log persistence port."""

    def create_log(self, log: PreVlmGateLog) -> PreVlmGateLog: ...

    def get_log(self, gate_log_id: str) -> PreVlmGateLog | None: ...

    def list_by_unit(self, unit_id: str) -> list[PreVlmGateLog]: ...

    def list_by_job(self, analysis_job_id: str) -> list[PreVlmGateLog]: ...
