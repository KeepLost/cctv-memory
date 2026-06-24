"""Abstract repository port interfaces.

Ports are the boundary between application/domain and infrastructure adapters.
They depend only on contracts/domain DTOs and never expose ORM models,
sessions, or connections (repository-port-contract §0).
"""

from cctv_memory.repositories.analysis import (
    AnalysisJobRepository,
    AnalysisScaleTaskRepository,
    HighFreqTriggerRepository,
)
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.camera import CameraRepository
from cctv_memory.repositories.index import IndexPort, StoredVector
from cctv_memory.repositories.observation import (
    ObservationRecordPublicationRepository,
    ObservationRecordReadRepository,
)
from cctv_memory.repositories.principal import (
    AccessPolicyRepository,
    PrincipalRepository,
)
from cctv_memory.repositories.search_context import SearchContextRepository
from cctv_memory.repositories.task_queue import TaskQueueRepository
from cctv_memory.repositories.timeline import TimelineRepository
from cctv_memory.repositories.types import (
    ConflictError,
    IdempotencyConflictError,
    Page,
    RepositoryError,
    WriteNotPermittedError,
)
from cctv_memory.repositories.video_source import VideoSourceRepository

__all__ = [
    # shared types
    "Page",
    "RepositoryError",
    "ConflictError",
    "IdempotencyConflictError",
    "WriteNotPermittedError",
    # ports
    "CameraRepository",
    "VideoSourceRepository",
    "AnalysisJobRepository",
    "AnalysisScaleTaskRepository",
    "HighFreqTriggerRepository",
    "ObservationRecordReadRepository",
    "ObservationRecordPublicationRepository",
    "SearchContextRepository",
    "PrincipalRepository",
    "AccessPolicyRepository",
    "TaskQueueRepository",
    "TimelineRepository",
    "AuditRepository",
    "IndexPort",
    "StoredVector",
]
