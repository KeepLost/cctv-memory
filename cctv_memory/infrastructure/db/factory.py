"""Repository factory: build SQLite adapters from a session.

This is the infrastructure composition point. Application code should receive
repository ports (not this factory) via dependency injection in later phases.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

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
from cctv_memory.infrastructure.db.repositories.index_store import (
    SqliteIndexRepository,
)
from cctv_memory.infrastructure.db.repositories.observation_read import (
    SqliteObservationReadRepository,
)
from cctv_memory.infrastructure.db.repositories.publication import (
    SqlitePublicationRepository,
)
from cctv_memory.infrastructure.db.repositories.search_context import (
    SqliteSearchContextRepository,
)
from cctv_memory.infrastructure.db.repositories.task_queue import (
    SqliteTaskQueueRepository,
)
from cctv_memory.infrastructure.db.repositories.timeline import SqliteTimelineRepository
from cctv_memory.repositories.index import IndexPort


class RepositoryFactoryProtocol(Protocol):
    """Structural repository factory protocol used by the composition root."""

    def camera(self) -> Any: ...
    def video_source(self) -> Any: ...
    def analysis_job(self) -> Any: ...
    def scale_task(self) -> Any: ...
    def trigger(self) -> Any: ...
    def analysis_unit(self) -> Any: ...
    def model_call_log(self) -> Any: ...
    def detector_gate_log(self) -> Any: ...
    def pre_vlm_gate_log(self) -> Any: ...
    def principal(self) -> Any: ...
    def access_policy(self) -> Any: ...
    def observation_read(self) -> Any: ...
    def publication(self) -> Any: ...
    def search_context(self) -> Any: ...
    def task_queue(self) -> Any: ...
    def audit(self) -> Any: ...
    def timeline(self) -> Any: ...
    def index(self) -> IndexPort: ...


class SqliteRepositoryFactory:
    """Construct SQLite repository adapters bound to a single session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def camera(self) -> SqliteCameraRepository:
        return SqliteCameraRepository(self._session)

    def video_source(self) -> SqliteVideoSourceRepository:
        return SqliteVideoSourceRepository(self._session)

    def analysis_job(self) -> SqliteAnalysisJobRepository:
        return SqliteAnalysisJobRepository(self._session)

    def scale_task(self) -> SqliteAnalysisScaleTaskRepository:
        return SqliteAnalysisScaleTaskRepository(self._session)

    def trigger(self) -> SqliteHighFreqTriggerRepository:
        return SqliteHighFreqTriggerRepository(self._session)

    def analysis_unit(self) -> SqliteAnalysisUnitRepository:
        return SqliteAnalysisUnitRepository(self._session)

    def model_call_log(self) -> SqliteModelCallLogRepository:
        return SqliteModelCallLogRepository(self._session)

    def detector_gate_log(self) -> SqliteDetectorGateLogRepository:
        return SqliteDetectorGateLogRepository(self._session)

    def pre_vlm_gate_log(self) -> SqlitePreVlmGateLogRepository:
        return SqlitePreVlmGateLogRepository(self._session)

    def principal(self) -> SqlitePrincipalRepository:
        return SqlitePrincipalRepository(self._session)

    def access_policy(self) -> SqliteAccessPolicyRepository:
        return SqliteAccessPolicyRepository(self._session)

    def observation_read(self) -> SqliteObservationReadRepository:
        return SqliteObservationReadRepository(self._session)

    def publication(self) -> SqlitePublicationRepository:
        return SqlitePublicationRepository(self._session)

    def search_context(self) -> SqliteSearchContextRepository:
        return SqliteSearchContextRepository(self._session)

    def task_queue(self) -> SqliteTaskQueueRepository:
        return SqliteTaskQueueRepository(self._session)

    def audit(self) -> SqliteAuditRepository:
        return SqliteAuditRepository(self._session)

    def timeline(self) -> SqliteTimelineRepository:
        return SqliteTimelineRepository(self._session)

    def index(self) -> SqliteIndexRepository:
        return SqliteIndexRepository(self._session)


class PostgresRepositoryFactory:
    """Construct PostgreSQL repository adapters bound to a single session."""

    def __init__(self, session: Session, *, vector_dimension: int) -> None:
        from cctv_memory.infrastructure.db.repositories.postgres import (
            PostgresAccessPolicyRepository,
            PostgresAnalysisJobRepository,
            PostgresAnalysisScaleTaskRepository,
            PostgresAnalysisUnitRepository,
            PostgresAuditRepository,
            PostgresCameraRepository,
            PostgresDetectorGateLogRepository,
            PostgresHighFreqTriggerRepository,
            PostgresIndexRepository,
            PostgresModelCallLogRepository,
            PostgresObservationReadRepository,
            PostgresPrincipalRepository,
            PostgresPreVlmGateLogRepository,
            PostgresPublicationRepository,
            PostgresSearchContextRepository,
            PostgresTaskQueueRepository,
            PostgresTimelineRepository,
            PostgresVideoSourceRepository,
        )

        self._session = session
        self._vector_dimension = vector_dimension
        self._camera_cls = PostgresCameraRepository
        self._video_source_cls = PostgresVideoSourceRepository
        self._analysis_job_cls = PostgresAnalysisJobRepository
        self._scale_task_cls = PostgresAnalysisScaleTaskRepository
        self._trigger_cls = PostgresHighFreqTriggerRepository
        self._analysis_unit_cls = PostgresAnalysisUnitRepository
        self._model_call_log_cls = PostgresModelCallLogRepository
        self._detector_gate_log_cls = PostgresDetectorGateLogRepository
        self._pre_vlm_gate_log_cls = PostgresPreVlmGateLogRepository
        self._principal_cls = PostgresPrincipalRepository
        self._access_policy_cls = PostgresAccessPolicyRepository
        self._observation_read_cls = PostgresObservationReadRepository
        self._publication_cls = PostgresPublicationRepository
        self._search_context_cls = PostgresSearchContextRepository
        self._task_queue_cls = PostgresTaskQueueRepository
        self._audit_cls = PostgresAuditRepository
        self._timeline_cls = PostgresTimelineRepository
        self._index_cls = PostgresIndexRepository

    def camera(self):  # type: ignore[no-untyped-def]
        return self._camera_cls(self._session)

    def video_source(self):  # type: ignore[no-untyped-def]
        return self._video_source_cls(self._session)

    def analysis_job(self):  # type: ignore[no-untyped-def]
        return self._analysis_job_cls(self._session)

    def scale_task(self):  # type: ignore[no-untyped-def]
        return self._scale_task_cls(self._session)

    def trigger(self):  # type: ignore[no-untyped-def]
        return self._trigger_cls(self._session)

    def analysis_unit(self):  # type: ignore[no-untyped-def]
        return self._analysis_unit_cls(self._session)

    def model_call_log(self):  # type: ignore[no-untyped-def]
        return self._model_call_log_cls(self._session)

    def detector_gate_log(self):  # type: ignore[no-untyped-def]
        return self._detector_gate_log_cls(self._session)

    def pre_vlm_gate_log(self):  # type: ignore[no-untyped-def]
        return self._pre_vlm_gate_log_cls(self._session)

    def principal(self):  # type: ignore[no-untyped-def]
        return self._principal_cls(self._session)

    def access_policy(self):  # type: ignore[no-untyped-def]
        return self._access_policy_cls(self._session)

    def observation_read(self):  # type: ignore[no-untyped-def]
        return self._observation_read_cls(self._session)

    def publication(self):  # type: ignore[no-untyped-def]
        return self._publication_cls(self._session)

    def search_context(self):  # type: ignore[no-untyped-def]
        return self._search_context_cls(self._session)

    def task_queue(self):  # type: ignore[no-untyped-def]
        return self._task_queue_cls(self._session)

    def audit(self):  # type: ignore[no-untyped-def]
        return self._audit_cls(self._session)

    def timeline(self):  # type: ignore[no-untyped-def]
        return self._timeline_cls(self._session)

    def index(self) -> IndexPort:
        return self._index_cls(self._session, dimension=self._vector_dimension)
