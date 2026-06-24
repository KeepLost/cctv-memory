"""Cross-module Pydantic v2 contract schemas.

Contracts only define data shapes (module-map §2.1). They must not contain
business logic and must not import infrastructure, FastAPI, or vendor SDKs.
"""

from cctv_memory.contracts.analysis import (
    AnalysisJob,
    AnalysisScaleTask,
    AnalysisUnit,
    HighFreqTrigger,
    ModelCallLog,
)
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import (
    AccessPolicy,
    AccessPolicyRules,
    AuthorizedScope,
    Principal,
)
from cctv_memory.contracts.backup import (
    AdminBackupRequest,
    BackupChecksum,
    BackupManifest,
)
from cctv_memory.contracts.common import (
    SCHEMA_VERSION,
    ApiErrorEnvelope,
    ApiSuccessEnvelope,
    ContractModel,
    ErrorDetail,
    PageRequest,
    PageResponse,
    ResponseMeta,
    TimeRange,
)
from cctv_memory.contracts.index import (
    IndexDocumentMetadata,
    ObservationDynamicIndexDocument,
    ObservationStaticIndexDocument,
)
from cctv_memory.contracts.observation import (
    ObservationRecord,
    ObservationRecordHistory,
)
from cctv_memory.contracts.pipeline import (
    PublicationResult,
    PublishObservationRecordsCommand,
)
from cctv_memory.contracts.search import (
    RefineObservationSearchRequest,
    SearchCandidate,
    SearchContext,
    SearchResultItem,
    SearchRevision,
    StartObservationSearchRequest,
    StartObservationSearchResponse,
)
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.contracts.video import (
    CameraDevice,
    CameraLocation,
    SubmitVideoSourceRequest,
    SubmitVideoSourceResponse,
    VideoSource,
)
from cctv_memory.contracts.vlm import VlmObservationOutput

__all__ = [
    "SCHEMA_VERSION",
    # common
    "ContractModel",
    "TimeRange",
    "PageRequest",
    "PageResponse",
    "ResponseMeta",
    "ErrorDetail",
    "ApiSuccessEnvelope",
    "ApiErrorEnvelope",
    # video
    "CameraLocation",
    "CameraDevice",
    "VideoSource",
    "SubmitVideoSourceRequest",
    "SubmitVideoSourceResponse",
    # analysis
    "AnalysisJob",
    "AnalysisScaleTask",
    "HighFreqTrigger",
    "AnalysisUnit",
    "ModelCallLog",
    "AnalysisTimelineEvent",
    # observation
    "ObservationRecord",
    "ObservationRecordHistory",
    # auth
    "Principal",
    "AccessPolicy",
    "AccessPolicyRules",
    "AuthorizedScope",
    # search
    "StartObservationSearchRequest",
    "StartObservationSearchResponse",
    "RefineObservationSearchRequest",
    "SearchResultItem",
    "SearchContext",
    "SearchRevision",
    "SearchCandidate",
    # pipeline / publication
    "PublishObservationRecordsCommand",
    "PublicationResult",
    # vlm / index / audit / backup
    "VlmObservationOutput",
    "IndexDocumentMetadata",
    "ObservationStaticIndexDocument",
    "ObservationDynamicIndexDocument",
    "AuditEvent",
    "AdminBackupRequest",
    "BackupChecksum",
    "BackupManifest",
]
