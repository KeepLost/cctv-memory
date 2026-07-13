"""Typed application settings (configuration-contract §2).

Application/domain code must not branch on the database backend
(configuration-contract §4); only infrastructure composition may read it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# Env var naming the config file path; falls back to ``./config.yaml`` if present.
CONFIG_FILE_ENV = "CCTV_MEMORY_CONFIG_FILE"
DEFAULT_CONFIG_FILENAME = "config.yaml"


def _reject_unknown_concurrency_env() -> None:
    """Fail fast on likely-misnamed worker/VLM nested env vars.

    Pydantic-settings only materializes known nested env names, so a typo such as
    ``CCTV_MEMORY_VLM__MAX_CONCURRENCY`` would otherwise be silently ignored and
    the runtime would keep the default ``vlm.max_concurrent_requests``. Rejecting
    unknown keys in these two sections preserves the single canonical config path
    instead of introducing aliases.
    """
    sections: dict[str, set[str]] = {
        "VLM": {name.upper() for name in VlmSection.model_fields},
        "WORKER": {name.upper() for name in WorkerSection.model_fields},
    }
    unknown: list[str] = []
    for key in os.environ:
        for section, allowed in sections.items():
            prefix = f"CCTV_MEMORY_{section}__"
            if not key.startswith(prefix):
                continue
            nested = key[len(prefix) :].split("__", 1)[0].upper()
            if nested not in allowed:
                unknown.append(key)
    if unknown:
        raise ValueError(
            "Unknown CCTV_MEMORY worker/VLM config env var(s): "
            + ", ".join(sorted(unknown))
        )


def _resolve_config_file() -> Path | None:
    """Locate the YAML config file (configuration-contract §1).

    Priority for the file path: the ``CCTV_MEMORY_CONFIG_FILE`` env var, else
    ``./config.yaml`` in the current working directory when it exists. Returns
    ``None`` when no file is configured/found so defaults+env still apply. The
    YAML file must NOT contain secrets (configuration-contract §6).
    """
    explicit = os.environ.get(CONFIG_FILE_ENV)
    if explicit:
        return Path(explicit)
    candidate = Path(DEFAULT_CONFIG_FILENAME)
    if candidate.is_file():
        return candidate
    return None


def resolved_config_file() -> Path | None:
    """Public accessor for the config file that ``AppConfig`` would load.

    Returns the same path ``settings_customise_sources`` resolves (or ``None``),
    so diagnostics (e.g. the ``doctor`` command) can report the effective config
    file truthfully without reaching into a private helper.
    """
    return _resolve_config_file()


class AppSection(BaseModel):
    env: str = "local"
    timezone: str = "Asia/Shanghai"
    data_dir: str = "./data"
    log_level: str = "INFO"


class ServerSection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str | None = None
    # Env var NAME holding the playback-token signing key. The value is read at
    # runtime start; if unset a per-process random key is generated (single-process
    # MVP). No key is ever committed (configuration-contract §6).
    playback_signing_key_env: str = "CCTV_MEMORY_PLAYBACK_SIGNING_KEY"


class DatabaseSection(BaseModel):
    backend: str = "sqlite"
    sqlite_path: str = "./data/cctv_memory.sqlite3"
    # Env var NAME holding the PostgreSQL SQLAlchemy DSN. The secret value is read
    # only by infrastructure/runtime.py and is never stored in config files or
    # diagnostics output (configuration-contract §6).
    postgres_dsn_env: str = "CCTV_MEMORY_POSTGRES_DSN"
    pool_size: int = Field(default=5, gt=0)
    max_overflow: int = Field(default=10, ge=0)
    echo_sql: bool = False


class StorageSection(BaseModel):
    video_root: str = "./data/videos"
    frame_root: str = "./data/frames"
    artifact_root: str = "./data/artifacts"


class ObservabilitySection(BaseModel):
    timeline_enabled: bool = True
    timeline_payload_mode: str = "minimal"  # "minimal" | "metadata"
    timeline_retention_days: int = Field(default=30, ge=0)
    timeline_export_max_events: int = Field(default=100_000, gt=0)
    timeline_fail_open: bool = True
    sql_trace_enabled: bool = False


class WorkerSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    embedded: bool = True
    worker_id: str = "local-worker-1"
    lease_seconds: int = Field(default=300, gt=0)
    # Lease renewal (task cctv-memory-20260616-1850 §B4). While a worker processes a
    # claimed job it renews the queue-task lease on this cadence so a long job under
    # raised ``max_concurrent_jobs`` is not re-claimed by another worker mid-flight
    # (duplicate processing). Must be < ``lease_seconds``; a heartbeat renews to
    # ``now + lease_seconds`` each tick. Default 60s vs 300s lease = ~5 renew windows.
    lease_renew_seconds: int = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0)
    # Multi-job concurrency (task cctv-memory-20260615-1620). ``max_concurrent_jobs``
    # is the number of analysis jobs this worker process handles AT ONCE (a bounded
    # in-process thread pool). Default 1 = the old strictly-serial drain (zero
    # behavior change). ``max_unit_workers_per_job`` bounds the per-job unit thread
    # pool and is DECOUPLED from the global provider cap: the GLOBAL in-flight VLM
    # call limit is always ``vlm.max_concurrent_requests`` enforced by the single
    # shared VlmScheduler, so concurrent jobs can never multiply the provider cap.
    max_concurrent_jobs: int = Field(default=1, gt=0)
    max_unit_workers_per_job: int = Field(default=1, gt=0)
    # Bounded retry for short worker lifecycle DB writes (job/scale transitions,
    # finalize, queue terminal writes) when SQLite reports transient BUSY/locked.
    # Publication/unit terminal writes have their own VLM-section knobs; these
    # cover the surrounding worker state-machine writes, including multi-process
    # SQLite contention where the process-local coordinator is insufficient.
    db_write_max_attempts: int = Field(default=3, ge=1)
    db_write_backoff_ms: int = Field(default=100, ge=0)
    # Bounded orphan-running recovery (task cctv-memory-20260612-1854 §E).
    # A unit stuck ``running`` longer than ``orphan_stale_seconds`` (must exceed
    # ``lease_seconds`` so only abandoned units are touched) is terminalized as
    # failed(orphan_timeout) and its parent scale/job reconciled. The sweep is
    # index-backed (idx_units_status_started) and capped at ``orphan_batch_limit``
    # rows per pass — never a full-table scan.
    orphan_recovery_enabled: bool = True
    orphan_stale_seconds: int = Field(default=900, gt=0)
    orphan_batch_limit: int = Field(default=100, gt=0)


class SearchSection(BaseModel):
    context_ttl_seconds: int = 900
    context_idle_seconds: int = 300
    max_top_k: int = 100
    max_candidates_per_revision: int = 1000
    max_revisions_per_context: int = 8
    rrf_k: int = 60
    static_weight: float = 1.0
    dynamic_weight: float = 1.0
    fts_weight: float = 0.5
    vector_weight: float = 1.0
    max_tag_boost: float = 0.2
    max_analysis_scale_boost: float = 0.2
    search_config_version: str = "search-v1"


class DefaultSegmentSection(BaseModel):
    window_seconds: int = Field(default=12, gt=0)
    overlap_seconds: int = Field(default=3, ge=0)
    frame_strategy: str = "uniform"
    frames_per_segment: int = Field(default=6, gt=0)


class MotionScanSection(BaseModel):
    """motion_scan parameters (pipeline-experiment-contract §2.3).

    Cheap frame-difference motion detection used to find high_freq_event triggers.
    ``method`` selects the detector implementation via the motion-detector factory
    (infrastructure/video/motion_detector_factory.py); all other values are
    per-method experiment knobs (config, not hardcoded branches).

    Defaults are tuned to be sensitive enough for CCTV event triggering while
    staying CPU-cheap (task cctv-memory-20260611-1049): a low threshold so small
    real motion triggers, a higher sampling fps and a larger downscaled frame so
    brief/small movements are not missed, and a shorter min_duration/merge_gap so
    short events still form a trigger window. 128x72 grayscale = 9216 px/frame at
    4 fps stays tiny. See status/execution-report.md for justification.
    """

    method: str = "frame_diff"
    threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    min_duration_ms: int = Field(default=600, gt=0)
    merge_gap_ms: int = Field(default=800, ge=0)
    sample_fps: float = Field(default=4.0, gt=0)
    frame_width: int = Field(default=128, gt=0)
    frame_height: int = Field(default=72, gt=0)


class HighFreqEventSection(BaseModel):
    """high_freq_event parameters (pipeline-experiment-contract §2.3)."""

    window_seconds: int = Field(default=3, gt=0)
    overlap_ratio: float = Field(default=0.5, ge=0.0, lt=1.0)
    frame_strategy: str = "uniform"
    frames_per_segment: int = Field(default=8, gt=0)


class DetectorGateRuleSection(BaseModel):
    """Configurable per-label detector gate rule."""

    label: str = "person"
    min_positive_frame_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    action: str = "call_vlm"


class DetectorGateSection(BaseModel):
    """Pre-VLM detector gate for default_segment windows."""

    enabled: bool = False
    provider: str = "mock"
    model_id: str = "mock-detector-v1"
    sample_fps: float = Field(default=1.0, gt=0)
    debug_media_retention: bool = False
    rules: list[DetectorGateRuleSection] = Field(default_factory=list)
    mock_positive_labels: list[str] = Field(default_factory=list)
    mock_positive_frame_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    mock_confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class PreVlmGateRuleSection(BaseModel):
    rule_id: str | None = None
    signal_type: str = "object_detection"
    label: str = "person"
    min_positive_frame_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    action: str = "call_vlm"


class PreVlmGateScaleProfileSection(BaseModel):
    enabled: bool = False
    profile_name: str = "default"
    suppression_policy: str = "skip_without_record"
    rules: list[PreVlmGateRuleSection] = Field(default_factory=list)
    force_vlm_on_trigger_reasons: list[str] = Field(default_factory=list)


class PreVlmGateMockSection(BaseModel):
    positive_labels: list[str] = Field(default_factory=list)
    positive_frame_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class PreVlmGateSection(BaseModel):
    enabled: bool = False
    provider: str = "mock"
    model_id: str = "mock-detector-v1"
    google_vision_url: str = "http://nginx:7070/api/google/v1/images:annotate"
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_results: int = Field(default=10, gt=0)
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    mock: PreVlmGateMockSection = Field(default_factory=PreVlmGateMockSection)
    default_segment: PreVlmGateScaleProfileSection = Field(
        default_factory=lambda: PreVlmGateScaleProfileSection(
            profile_name="default_segment",
            suppression_policy="publish_gate_only_record",
        )
    )
    high_freq_event: PreVlmGateScaleProfileSection = Field(
        default_factory=lambda: PreVlmGateScaleProfileSection(
            profile_name="high_freq_event",
            suppression_policy="skip_without_record",
        )
    )


class FrameStreamSection(BaseModel):
    """OpenCV streaming-decode + frame-selection knobs (frame-stream-selector
    -cache-design §2.4/§4/§8.1).

    The OpenCV backend decodes a segment once at ``sample_fps``, keeps only a
    bounded ring buffer of recent raw frames (``buffer_seconds`` x ``sample_fps``
    frames, capped again by ``max_buffer_bytes``), scores each decoded frame on
    downscaled grayscale (``scoring_scale``) producing only scalar metrics, then a
    pure domain selector picks frames by ``selection_strategy``. Selected frames
    are JPEG-encoded (``selected_jpeg_quality``) to disk for the VLM. Raw frames
    never leave the adapter; only scalars + selected file paths cross the boundary.

    ``cleanup_selected_on_success``: in metadata_only/normal mode, delete the
    unit's selected frame files after the unit succeeds (debug artifacts are
    never touched). All values are experiment knobs (pipeline-experiment §2.3).
    """

    sample_fps: float = Field(default=8.0, gt=0)
    buffer_seconds: float = Field(default=4.0, gt=0)
    max_buffer_bytes: int = Field(default=268_435_456, gt=0)  # 256 MiB safety fuse
    scoring_scale: str = "320x180"
    selection_strategy: str = "bins_then_score"  # uniform | score | bins_then_score
    selected_jpeg_quality: int = Field(default=80, ge=1, le=100)
    w_motion: float = Field(default=1.0, ge=0.0)
    w_scene: float = Field(default=0.5, ge=0.0)
    w_quality: float = Field(default=0.5, ge=0.0)
    min_blur: float = Field(default=50.0, ge=0.0)
    cleanup_selected_on_success: bool = True
    # ffmpeg used only for the bounded duration probe inside the opencv processor.
    decode_timeout_seconds: int = Field(default=120, gt=0)


class CrossScaleSection(BaseModel):
    """Cross-scale VLM unit scheduling knobs (Stage C2).

    After motion_scan triggers exist, default_segment + high_freq_event units are
    dispatched from one unified in-process queue. ``high_freq_quota`` is the
    deterministic anti-starvation knob: at most this many high_freq units are
    dispatched before one default_segment unit is forced, so high_freq is
    prioritized while default is never starved (job-state-machine-contract: scale
    completion = all its units terminal; finalization is per-scale on terminal
    counts). ``enabled`` falls back to the legacy sequential scale loop when off.
    """

    enabled: bool = True
    high_freq_quota: int = Field(default=3, gt=0)


class PipelineSection(BaseModel):
    default_segment: DefaultSegmentSection = Field(default_factory=DefaultSegmentSection)
    detector_gate: DetectorGateSection = Field(default_factory=DetectorGateSection)
    pre_vlm_gate: PreVlmGateSection = Field(default_factory=PreVlmGateSection)
    motion_scan: MotionScanSection = Field(default_factory=MotionScanSection)
    high_freq_event: HighFreqEventSection = Field(default_factory=HighFreqEventSection)
    cross_scale: CrossScaleSection = Field(default_factory=lambda: CrossScaleSection())
    # Video metadata source: "ffprobe" (real, bounded) or "static" (deterministic,
    # no subprocess — used for local/dev/test closed-loop runs without media files).
    video_metadata_mode: str = "ffprobe"
    static_duration_ms: int = Field(default=30_000, gt=0)
    # Frame decode backend (frame-stream-selector-cache-design §1.4/§5.4):
    #   "opencv"  -> OpenCvFrameStreamVideoProcessor: stream once, bounded ring
    #                buffer, online scalar scoring, metric-driven frame selection.
    #                THE DEFAULT (Eric decision 2026-06-11).
    #   "ffmpeg"  -> legacy per-frame ffmpeg seek (SegmentFrameVideoProcessor).
    # When opencv decode fails and the fallback flag is set, fall back to ffmpeg.
    decode_backend: str = "opencv"
    decode_fallback_to_ffmpeg: bool = True
    frame_stream: FrameStreamSection = Field(default_factory=FrameStreamSection)

    @model_validator(mode="after")
    def _map_legacy_detector_gate(self) -> PipelineSection:
        if self.pre_vlm_gate.enabled or not self.detector_gate.enabled:
            return self
        self.pre_vlm_gate.enabled = True
        self.pre_vlm_gate.provider = self.detector_gate.provider
        self.pre_vlm_gate.model_id = self.detector_gate.model_id
        self.pre_vlm_gate.mock.positive_labels = list(self.detector_gate.mock_positive_labels)
        self.pre_vlm_gate.mock.positive_frame_ratio = self.detector_gate.mock_positive_frame_ratio
        self.pre_vlm_gate.mock.confidence = self.detector_gate.mock_confidence
        self.pre_vlm_gate.default_segment.enabled = True
        self.pre_vlm_gate.default_segment.profile_name = "legacy_detector_gate_default_segment"
        self.pre_vlm_gate.default_segment.suppression_policy = "publish_gate_only_record"
        self.pre_vlm_gate.default_segment.rules = [
            PreVlmGateRuleSection(
                signal_type="object_detection",
                label=r.label,
                min_positive_frame_ratio=r.min_positive_frame_ratio,
                min_confidence=r.min_confidence,
                action=r.action,
            )
            for r in self.detector_gate.rules
        ]
        return self


class FeaturesSection(BaseModel):
    sqlite_vec: bool = False
    low_freq_summary: bool = False
    user_registration: bool = False
    admin_policy_api: bool = False


class VlmSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "mock"  # "mock" or "real"
    model: str | None = None
    model_id: str = "gemini-3.1-pro-preview"
    # Env var NAMES (not values): keeps secrets/urls out of committed config.
    base_url_env: str = "CCTV_MEMORY_VLM_BASE_URL"
    api_key_env: str = "LLM_KEY"
    default_base_url: str = "http://nginx:8081/api/ohmygpt/chat/completions"
    timeout_seconds: int = 120
    max_retries: int = 2
    # How the segment media reaches the VLM (vlm-analysis-contract §1 allows
    # "frame_uris OR clip_uri"). DEFAULT = "frames": extract sampled frames and
    # send them as MULTIPLE images — local-deployment friendly and the safer
    # default. "video": send the whole clip as a single video part (the legacy
    # path; opt-in only). Only meaningful for provider=real (mock decodes nothing).
    media_input: str = "frames"  # "frames" | "video"
    # Audio is DROPPED by default. Frames carry no audio inherently; for
    # media_input=video the audio track is stripped unless this is explicitly
    # enabled. Off by default so the default path never transmits audio.
    include_audio: bool = False
    # Provider-specific request-body options merged by the real adapter only.
    # Core/system fields (model/messages/media/auth) are never overridable.
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Bounded in-process scheduling for VLM analysis units.
    # PROCESS-WIDE GLOBAL provider-call cap (task cctv-memory-20260615-1620): this
    # is the maximum number of VLM provider calls in flight at once across ALL
    # concurrent jobs/units/retries in this worker process, enforced by ONE shared
    # VlmScheduler. worker.max_concurrent_jobs x worker.max_unit_workers_per_job
    # may exceed this, but actual in-flight VLM calls never do.
    max_concurrent_requests: int = Field(default=1, gt=0)
    min_request_interval_ms: int = Field(default=0, ge=0)
    # Unit-level transient retry (task cctv-memory-20260615-1447). Distinct from the
    # adapter's in-call ``max_retries`` (transport-level reprompt/transport retry inside a
    # single logical call): these knobs govern how many times the WORKER re-runs the whole
    # VLM call for ONE unit when it fails with a TRANSIENT provider error (cold start,
    # timeout, 5xx, 429). ``unit_max_attempts=1`` disables unit retry (prior behavior).
    # Default 3 absorbs first-call/cold-start blips. Every attempt still goes through the
    # global VlmScheduler. Permanent errors (schema/frame/insufficient_frames) are never
    # retried. Backoff is exponential (base*2^(n-1) capped) with +-jitter.
    unit_max_attempts: int = Field(default=3, ge=1)
    retry_backoff_base_ms: int = Field(default=500, ge=0)
    retry_backoff_cap_ms: int = Field(default=8_000, ge=0)
    retry_jitter: float = Field(default=0.2, ge=0.0, le=1.0)
    # Bounded retry for transient terminal DB writes (SQLite lock/busy) so a terminal
    # mark_failed/mark_skipped/success write cannot silently fail and strand a unit
    # ``running`` (no tally-vs-DB divergence). Exhaustion re-raises; the bounded orphan
    # sweep is the backstop. ``terminal_write_max_attempts=1`` disables this retry.
    terminal_write_max_attempts: int = Field(default=3, ge=1)
    terminal_write_backoff_ms: int = Field(default=100, ge=0)
    # Model-call observability and media artifact retention.
    media_log_mode: str = "metadata_only"  # "metadata_only" | "debug_full_media"
    debug_media_retention: bool = False


class IndexingSection(BaseModel):
    """Embedding / vector-index configuration (pipeline-experiment §2.5).

    ``provider`` selects the embedder adapter: ``mock`` (default, offline,
    deterministic — keeps CI network-free) or ``real`` (the SiliconFlow /
    OpenAI-compatible embedder). Only env var NAMES are stored here; the API key
    value lives in the environment and is read by the composition root, never
    printed or committed (configuration-contract §6).
    """

    provider: str = "mock"  # "mock" or "real"
    enabled: bool = False
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimensions: int = Field(default=1024, gt=0)
    # Env var NAMES (not values) + a documentation-safe default base URL.
    base_url_env: str = "CCTV_MEMORY_EMBEDDING_BASE_URL"
    default_base_url: str = "http://nginx:8081/api/siliconflow"
    embeddings_path: str = "/embeddings"
    api_key_env: str = "CCTV_MEMORY_EMBEDDING_API_KEY"
    encoding_format: str = "float"
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=1, ge=0)
    # External cross-encoder reranker (C3). Disabled by default so search behaves
    # exactly as before and CI stays offline; ``rerank_provider=mock`` is the
    # offline default when enabled. The key/base-url are env var NAMES only.
    rerank_enabled: bool = False
    rerank_provider: str = "mock"  # "mock" or "real"
    rerank_model: str = "Qwen/Qwen3-Reranker-8B"
    rerank_base_url_env: str = "CCTV_MEMORY_RERANK_BASE_URL"
    rerank_default_base_url: str = "http://nginx:8081/api/siliconflow"
    rerank_path: str = "/rerank"
    rerank_api_key_env: str = "CCTV_MEMORY_RERANK_API_KEY"
    rerank_top_n: int = Field(default=50, gt=0)
    rerank_timeout_seconds: float = Field(default=30.0, gt=0)
    rerank_max_retries: int = Field(default=1, ge=0)


class AppConfig(BaseSettings):
    """Top-level configuration (configuration-contract §2).

    Sources, highest precedence first (configuration-contract §1):
    ``init kwargs`` (CLI/programmatic) > environment variables > YAML config file
    > built-in defaults. The YAML file is loaded from ``CCTV_MEMORY_CONFIG_FILE``
    or ``./config.yaml`` when present (see ``_resolve_config_file``). Secrets must
    come from the environment, never the committed YAML (configuration-contract §6).
    """

    model_config = SettingsConfigDict(
        env_prefix="CCTV_MEMORY_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert a YAML source below env but above defaults (contract §1)."""
        yaml_file = _resolve_config_file()
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            env_settings,
            dotenv_settings,
        ]
        if yaml_file is not None:
            sources.append(
                YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file)
            )
        sources.append(file_secret_settings)
        return tuple(sources)

    app: AppSection = Field(default_factory=AppSection)
    server: ServerSection = Field(default_factory=ServerSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)
    worker: WorkerSection = Field(default_factory=WorkerSection)
    search: SearchSection = Field(default_factory=SearchSection)
    pipeline: PipelineSection = Field(default_factory=PipelineSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)
    vlm: VlmSection = Field(default_factory=VlmSection)
    indexing: IndexingSection = Field(default_factory=IndexingSection)

    def __init__(self, **data: Any) -> None:
        _reject_unknown_concurrency_env()
        super().__init__(**data)

    def with_data_dir(self, data_dir: str | Path) -> AppConfig:
        """Return a copy with all paths rooted under ``data_dir``.

        Used by CLI/composition root so a single ``--data-dir`` controls the
        SQLite file and storage roots (configuration-contract §3).
        """
        root = Path(data_dir)
        updated = self.model_copy(deep=True)
        updated.app.data_dir = str(root)
        updated.database.sqlite_path = str(root / "cctv_memory.sqlite3")
        updated.storage.video_root = str(root / "videos")
        updated.storage.frame_root = str(root / "frames")
        updated.storage.artifact_root = str(root / "artifacts")
        return updated
