"""Runtime composition root (infrastructure/runtime.py).

The single place allowed to construct concrete infrastructure (engine, session,
adapters) and wire it into application services. API/CLI/worker entrypoints
depend on this, keeping application/domain free of infrastructure imports
(ARCHITECTURE_CONSTITUTION §3). Only this composition layer may read
``config.database.backend`` (configuration-contract §4).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from cctv_memory.application.analysis_orchestrator import AnalysisOrchestrator
from cctv_memory.application.auth import AuthorizationService
from cctv_memory.application.backup import BackupService
from cctv_memory.application.ingestion import (
    DEFAULT_PIPELINE_VERSION,
    OPENCV_PIPELINE_VERSION,
    IngestionService,
)
from cctv_memory.application.locator import LocatorService
from cctv_memory.application.playback import PlaybackTokenService
from cctv_memory.application.request_services import RequestServices
from cctv_memory.application.search import SearchService
from cctv_memory.config.settings import AppConfig
from cctv_memory.domain.ranking import RrfWeights
from cctv_memory.infrastructure.db.backup import PostgresBackupAdapter, SqliteBackupAdapter
from cctv_memory.infrastructure.db.engine import (
    create_postgres_engine,
    create_session_factory,
    create_sqlite_engine,
)
from cctv_memory.infrastructure.db.factory import (
    PostgresRepositoryFactory,
    SqliteRepositoryFactory,
)
from cctv_memory.infrastructure.db.models import Base
from cctv_memory.infrastructure.db.postgres.schema import postgres_schema_ddl
from cctv_memory.infrastructure.db.write_coordinator import (
    NullWriteCoordinator,
    SqliteWriteCoordinator,
)
from cctv_memory.infrastructure.indexing.mock_embedder import MockEmbedder
from cctv_memory.infrastructure.indexing.mock_reranker import MockReranker
from cctv_memory.infrastructure.indexing.siliconflow_embedder import SiliconFlowEmbedder
from cctv_memory.infrastructure.indexing.siliconflow_reranker import SiliconFlowReranker
from cctv_memory.services.embedding import EmbeddingPort
from cctv_memory.services.reranker import RerankerPort
from cctv_memory.services.timeline_recorder import TimelineRecorder

_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS observation_static_fts "
    "USING fts5(record_id UNINDEXED, text)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS observation_dynamic_fts "
    "USING fts5(record_id UNINDEXED, text)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS observation_tags_fts "
    "USING fts5(record_id UNINDEXED, text)",
)


def _pipeline_version_for(config: AppConfig) -> str:
    """Effective pipeline_version from the decode backend (honest versioning).

    The OpenCV streaming-selection path and the legacy ffmpeg path send different
    frames to the VLM, so they must be distinguishable for experiment/repro
    (pipeline-experiment-contract §3.2). Backend is an infra/composition concern,
    so this lives in the composition root (not domain/application logic).
    """
    if config.pipeline.decode_backend == "opencv":
        return OPENCV_PIPELINE_VERSION
    return DEFAULT_PIPELINE_VERSION


class Runtime:
    """Holds the engine + session factory for a configured data dir."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._backend = config.database.backend
        self._write_coordinator: SqliteWriteCoordinator | NullWriteCoordinator
        if self._backend == "sqlite":
            self._engine = create_sqlite_engine(
                config.database.sqlite_path, echo=config.database.echo_sql
            )
            self._write_coordinator = SqliteWriteCoordinator()
        elif self._backend == "postgres":
            dsn = os.environ.get(config.database.postgres_dsn_env)
            if not dsn:
                raise RuntimeError(
                    "database.backend=postgres but env var "
                    f"{config.database.postgres_dsn_env} is not set"
                )
            self._engine = create_postgres_engine(
                dsn,
                echo=config.database.echo_sql,
                pool_size=config.database.pool_size,
                max_overflow=config.database.max_overflow,
            )
            self._write_coordinator = NullWriteCoordinator()
        else:
            raise ValueError(
                f"unsupported database backend: {config.database.backend} "
                "(expected sqlite or postgres)"
            )
        self._session_factory: sessionmaker[Session] = create_session_factory(self._engine)
        # Backend-specific write-serialization policy lives at the database
        # boundary (ARCHITECTURE_CONSTITUTION §7), not in worker/business code. For
        # SQLite (single-writer) this is a process-global lock shared across all
        # units/scales/jobs; a future PostgreSQL runtime would inject a
        # NullWriteCoordinator (MVCC) with no worker change. The worker wraps each
        # short DB write critical section in ``write_coordinator.write()``; VLM
        # calls stay outside it (§9.1).
        # Playback-token signing key: from the configured env-var NAME, else a
        # per-process random key (single-process MVP). Never committed/printed.
        self._playback_signing_key: str = os.environ.get(
            config.server.playback_signing_key_env
        ) or secrets.token_hex(32)

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def write_coordinator(self) -> SqliteWriteCoordinator | NullWriteCoordinator:
        """Backend-specific DB write-serialization coordinator (composition root).

        Shared process-wide so concurrent worker units/scales/jobs serialize on one
        SQLite writer. Upper layers depend only on the ``WriteCoordinator`` port.
        """
        return self._write_coordinator

    def init_storage(self) -> None:
        """Create data/storage directories (idempotent)."""
        for path in (
            self.config.app.data_dir,
            self.config.storage.video_root,
            self.config.storage.frame_root,
            self.config.storage.artifact_root,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)
        if self._backend == "sqlite":
            Path(self.config.database.sqlite_path).parent.mkdir(parents=True, exist_ok=True)

    def create_schema(self) -> None:
        """Create all tables + FTS placeholders (idempotent dev init).

        Production migration uses Alembic; this convenience path mirrors the
        initial migration for local ``init`` so a single command yields a usable
        database.
        """
        if self._backend == "sqlite":
            Base.metadata.create_all(self._engine)
            with self._engine.begin() as conn:
                for ddl in _FTS_DDL:
                    conn.execute(text(ddl))
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO schema_metadata(key, value) "
                        "VALUES ('schema_version', 'v1')"
                    )
                )
            return
        with self._engine.begin() as conn:
            for ddl in postgres_schema_ddl(
                vector_dimension=self.config.indexing.embedding_dimensions
            ):
                conn.execute(text(ddl))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session scope: commit on success, rollback on error."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def repositories(self, session: Session) -> SqliteRepositoryFactory | PostgresRepositoryFactory:
        if self._backend == "postgres":
            return PostgresRepositoryFactory(
                session, vector_dimension=self.config.indexing.embedding_dimensions
            )
        return SqliteRepositoryFactory(session)

    def backup_adapter(self) -> SqliteBackupAdapter | PostgresBackupAdapter:
        """Return a backend-specific backup adapter."""
        if self._backend == "postgres":
            return PostgresBackupAdapter(self._session_factory)
        return SqliteBackupAdapter(self.config.database.sqlite_path)

    def timeline_recorder(self) -> TimelineRecorder:
        """Return a fail-open timeline recorder using short independent writes."""
        cfg = self.config.observability

        def _append(event):  # type: ignore[no-untyped-def]
            with self._write_coordinator.write(), self.session() as session:
                self.repositories(session).timeline().append_event(event)

        return TimelineRecorder(
            _append,
            enabled=cfg.timeline_enabled,
            fail_open=cfg.timeline_fail_open,
        )

    def build_embedder(self) -> EmbeddingPort:
        """Construct the configured ``EmbeddingPort`` (mock default, offline).

        ``indexing.provider=mock`` (the default) returns a deterministic, network-
        free embedder so CI stays offline. ``real`` returns the SiliconFlow /
        OpenAI-compatible adapter and is only used when explicitly configured AND
        the API-key env var (named in config) is present; otherwise this raises so
        a misconfiguration fails fast instead of silently calling the network.
        The API key is read here (composition root) from its env var NAME and is
        never printed or committed (configuration-contract §6).
        """
        cfg = self.config.indexing
        if cfg.provider != "real":
            return MockEmbedder(
                dimension=cfg.embedding_dimensions,
                model_id=cfg.embedding_model,
            )
        api_key = os.environ.get(cfg.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"indexing.provider=real but env var {cfg.api_key_env} is not set"
            )
        base_url = os.environ.get(cfg.base_url_env, cfg.default_base_url)
        from cctv_memory.infrastructure.indexing.formats import (
            OpenAICompatibleEmbeddingFormat,
        )

        request_format = OpenAICompatibleEmbeddingFormat(
            model_id=cfg.embedding_model,
            path=cfg.embeddings_path,
            encoding_format=cfg.encoding_format,
        )
        return SiliconFlowEmbedder(
            base_url=base_url,
            api_key=api_key,
            model_id=cfg.embedding_model,
            dimension=cfg.embedding_dimensions,
            request_format=request_format,
            timeout_seconds=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
        )

    def build_reranker(self) -> RerankerPort:
        """Construct the configured ``RerankerPort`` (mock default, offline).

        ``indexing.rerank_provider=mock`` (the default) returns a deterministic,
        network-free reranker so CI stays offline. ``real`` returns the
        SiliconFlow cross-encoder adapter and is only used when explicitly
        configured AND the API-key env var (named in config) is present; otherwise
        this raises so a misconfiguration fails fast instead of silently calling
        the network. The key is read here (composition root) from its env var NAME
        and is never printed or committed (configuration-contract §6).
        """
        cfg = self.config.indexing
        if cfg.rerank_provider != "real":
            return MockReranker(model_id="mock-reranker-v1")
        api_key = os.environ.get(cfg.rerank_api_key_env)
        if not api_key:
            raise RuntimeError(
                f"indexing.rerank_provider=real but env var {cfg.rerank_api_key_env} is not set"
            )
        base_url = os.environ.get(cfg.rerank_base_url_env, cfg.rerank_default_base_url)
        return SiliconFlowReranker(
            base_url=base_url,
            api_key=api_key,
            model_id=cfg.rerank_model,
            path=cfg.rerank_path,
            timeout_seconds=cfg.rerank_timeout_seconds,
            max_retries=cfg.rerank_max_retries,
        )

    @contextmanager
    def request_services(self) -> Iterator[RequestServices]:
        """Yield application services bound to a transactional session.

        This is the composition root: it is the only place that constructs
        concrete repository adapters and injects them into application services.
        """
        with self.session() as session:
            repos = self.repositories(session)
            cfg = self.config
            embedder = self.build_embedder()
            yield RequestServices(
                auth=AuthorizationService(
                    repos.principal(), repos.access_policy(), repos.camera()
                ),
                ingestion=IngestionService(
                    repos.video_source(),
                    repos.analysis_job(),
                    repos.scale_task(),
                    repos.task_queue(),
                    repos.audit(),
                    model_version=(
                        cfg.vlm.model_id if cfg.vlm.provider == "real" else "mock-vlm-v1"
                    ),
                    pipeline_version=_pipeline_version_for(cfg),
                ),
                search=SearchService(
                    repos.observation_read(),
                    repos.search_context(),
                    repos.audit(),
                    context_ttl_seconds=cfg.search.context_ttl_seconds,
                    context_idle_seconds=cfg.search.context_idle_seconds,
                    max_top_k=cfg.search.max_top_k,
                    max_candidates_per_revision=cfg.search.max_candidates_per_revision,
                    max_revisions_per_context=cfg.search.max_revisions_per_context,
                    weights=RrfWeights(
                        k=cfg.search.rrf_k,
                        static_weight=cfg.search.static_weight,
                        dynamic_weight=cfg.search.dynamic_weight,
                        fts_weight=cfg.search.fts_weight,
                        vector_weight=cfg.search.vector_weight,
                        max_tag_boost=cfg.search.max_tag_boost,
                        max_analysis_scale_boost=cfg.search.max_analysis_scale_boost,
                        config_version=cfg.search.search_config_version,
                    ),
                    embedder=embedder,
                    index=repos.index(),
                    # Vector rerank only runs when indexing is enabled; default
                    # off keeps existing FTS behavior + offline CI deterministic.
                    vector_search_enabled=cfg.indexing.enabled,
                    reranker=self.build_reranker(),
                    rerank_enabled=cfg.indexing.rerank_enabled,
                    rerank_top_n=cfg.indexing.rerank_top_n,
                ),
                locator=LocatorService(repos.observation_read(), repos.audit()),
                jobs=repos.analysis_job(),
                orchestrator=AnalysisOrchestrator(
                    repos.analysis_job(), repos.scale_task()
                ),
                playback=PlaybackTokenService(
                    repos.observation_read(),
                    repos.audit(),
                    signing_key=self._playback_signing_key,
                ),
                backup=BackupService(
                    self.backup_adapter(),
                    repos.audit(),
                    observations=repos.observation_read(),
                    video_sources=repos.video_source(),
                ),
            )

    def dispose(self) -> None:
        self._engine.dispose()


def build_runtime(config: AppConfig | None = None, *, data_dir: str | None = None) -> Runtime:
    """Build a Runtime, optionally rooting all paths under ``data_dir``."""
    cfg = config or AppConfig()
    if data_dir is not None:
        cfg = cfg.with_data_dir(data_dir)
    return Runtime(cfg)
