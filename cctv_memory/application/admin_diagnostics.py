"""Admin-only analysis failure diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.domain.enums import Capability
from cctv_memory.domain.exceptions import CapabilityDeniedError
from cctv_memory.repositories.analysis import (
    AnalysisJobRepository,
    AnalysisScaleTaskRepository,
    AnalysisUnitRepository,
    ModelCallLogRepository,
    PreVlmGateLogRepository,
)


@dataclass(frozen=True)
class AnalysisFailureDiagnostics:
    analysis_job_id: str
    job: dict[str, Any] | None
    units: list[dict[str, Any]]
    model_call_logs: list[dict[str, Any]]
    pre_vlm_gate_logs: list[dict[str, Any]]

    def model_dump(self) -> dict[str, Any]:
        return {
            "analysis_job_id": self.analysis_job_id,
            "job": self.job,
            "units": self.units,
            "model_call_logs": self.model_call_logs,
            "pre_vlm_gate_logs": self.pre_vlm_gate_logs,
        }


class AdminDiagnosticsService:
    """Read model-output failure details behind runtime.manage capability."""

    def __init__(
        self,
        jobs: AnalysisJobRepository,
        scale_tasks: AnalysisScaleTaskRepository,
        units: AnalysisUnitRepository,
        model_calls: ModelCallLogRepository,
        pre_vlm_gate_logs: PreVlmGateLogRepository,
    ) -> None:
        self._jobs = jobs
        self._scale_tasks = scale_tasks
        self._units = units
        self._model_calls = model_calls
        self._pre_vlm_gate_logs = pre_vlm_gate_logs

    def failure_details(
        self, analysis_job_id: str, scope: AuthorizedScope
    ) -> AnalysisFailureDiagnostics:
        if Capability.RUNTIME_MANAGE not in scope.capabilities:
            raise CapabilityDeniedError("runtime.manage required for failure diagnostics")
        job = self._jobs.get_job(analysis_job_id)
        scale_tasks = self._scale_tasks.list_by_job(analysis_job_id)
        units = [
            unit
            for task in scale_tasks
            for unit in self._units.list_by_scale_task(task.scale_task_id)
            if unit.status.value in {"failed", "skipped"} or unit.last_error_code
        ]
        model_calls = self._model_calls.list_by_job(analysis_job_id)
        pre_vlm_logs = self._pre_vlm_gate_logs.list_by_job(analysis_job_id)
        return AnalysisFailureDiagnostics(
            analysis_job_id=analysis_job_id,
            job=job.model_dump(mode="json") if job is not None else None,
            units=[unit.model_dump(mode="json") for unit in units],
            model_call_logs=[log.model_dump(mode="json") for log in model_calls],
            pre_vlm_gate_logs=[log.model_dump(mode="json") for log in pre_vlm_logs],
        )
