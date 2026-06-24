"""Domain enums.

Pure domain layer: no FastAPI, SQLAlchemy, or vendor SDK imports.
Mirrors `status/schema-contracts.md` §2 and the security-level order in
`status/authorization-policy-contract.md` §5.1.
"""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    """Video source origin type (schema-contracts §2.1)."""

    FILE = "file"
    RTSP_CHUNK = "rtsp_chunk"
    OBJECT_STORAGE = "object_storage"
    EXTERNAL = "external"


class AnalysisScale(StrEnum):
    """Analysis frequency scale (schema-contracts §2.2)."""

    DEFAULT_SEGMENT = "default_segment"
    MOTION_SCAN = "motion_scan"
    HIGH_FREQ_EVENT = "high_freq_event"
    LOW_FREQ_SUMMARY = "low_freq_summary"


class JobStatus(StrEnum):
    """AnalysisJob status (schema-contracts §2.3)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """AnalysisScaleTask status (schema-contracts §2.4)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TriggerStatus(StrEnum):
    """HighFreqTrigger status (job-state-machine-contract §3)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ModelCallStatus(StrEnum):
    """Model-call log status for provider attempts."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SearchMode(StrEnum):
    """Search mode (schema-contracts §2.5)."""

    STATIC_ATTRIBUTE = "static_attribute"
    DYNAMIC_EVENT = "dynamic_event"
    HYBRID = "hybrid"
    AUTO_BY_EXTERNAL_AI = "auto_by_external_ai"


class ContextMode(StrEnum):
    """SearchContext mode (schema-contracts §2.6). MVP implements snapshot only."""

    SNAPSHOT = "snapshot"
    STREAM = "stream"


class PrincipalType(StrEnum):
    """Principal type (schema-contracts §2.7)."""

    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    ADMIN = "admin"


class SecurityLevel(StrEnum):
    """Security level with a globally fixed order.

    Order (authorization-policy-contract §5.1):
        public < internal < confidential < restricted
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        """Numeric rank for ordering; higher is stricter."""
        return _SECURITY_LEVEL_ORDER[self]

    def allows(self, resource_level: SecurityLevel) -> bool:
        """Return True if a principal capped at ``self`` may view ``resource_level``.

        Implements ``resource_level <= max_security_level`` from
        authorization-policy-contract §4.1 / §5.1.
        """
        return resource_level.rank <= self.rank

    @staticmethod
    def stricter(left: SecurityLevel, right: SecurityLevel) -> SecurityLevel:
        """Return the stricter (higher) of two levels (inheritance conflict rule §5.1)."""
        return left if left.rank >= right.rank else right


_SECURITY_LEVEL_ORDER: dict[SecurityLevel, int] = {
    SecurityLevel.PUBLIC: 0,
    SecurityLevel.INTERNAL: 1,
    SecurityLevel.CONFIDENTIAL: 2,
    SecurityLevel.RESTRICTED: 3,
}


class Capability(StrEnum):
    """First-version capabilities (authorization-policy-contract §2)."""

    OBSERVATION_SEARCH = "observation.search"
    OBSERVATION_READ_DETAIL = "observation.read_detail"
    OBSERVATION_READ_LOCATOR = "observation.read_locator"
    VIDEO_PLAYBACK = "video.playback"
    ANALYSIS_SUBMIT = "analysis.submit"
    ANALYSIS_RERUN = "analysis.rerun"
    ANALYSIS_PUBLISH = "analysis.publish"
    CAMERA_MANAGE = "camera.manage"
    POLICY_MANAGE = "policy.manage"
    USER_MANAGE = "user.manage"
    AUDIT_READ = "audit.read"
    RUNTIME_MANAGE = "runtime.manage"


class RefineOp(StrEnum):
    """Refine operations (search-contract §3.1)."""

    NARROW_BY_TAGS = "narrow_by_tags"
    SEARCH_STATIC_TEXT = "search_static_text"
    SEARCH_DYNAMIC_TEXT = "search_dynamic_text"
    HYBRID_SEARCH_TEXT = "hybrid_search_text"
    FILTER_BY_ANALYSIS_SCALE = "filter_by_analysis_scale"
    APPLY_RRF_FUSION = "apply_rrf_fusion"
    RERANK_CURRENT_CANDIDATES = "rerank_current_candidates"


class ScaleStrategy(StrEnum):
    """Analysis-scale preference strategy (schema-contracts §5.3)."""

    PREFER_DEFAULT_SEGMENT = "prefer_default_segment"
    PREFER_HIGH_FREQ = "prefer_high_freq"
    BALANCED = "balanced"
