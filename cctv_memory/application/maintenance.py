"""Maintenance use cases (application/maintenance.py).

Operational maintenance for the vector index and SearchContext lifecycle
(module-map §2.5, pipeline-experiment §4, search-contract §9.5.4):

- ``reindex``: (re)build observation text embeddings into the vector index. Reads
  active records WITHIN the caller's AuthorizedScope (authorized-scope-first even
  for maintenance — never a full-corpus unfiltered read), embeds static/dynamic
  text via the async ``EmbeddingPort``, and upserts vectors via ``IndexPort``.
  Idempotent + resumable: a record whose stored vector already matches the current
  ``model_id`` is skipped unless ``force`` is set, so a re-run is cheap and a model
  change (different ``model_id``) triggers a rebuild (pipeline-experiment §4).
- ``sweep_contexts``: expire stale SearchContexts (lazy + periodic sweep).

Embeddings are index artifacts only — this never writes an active ObservationRecord
(ARCHITECTURE_CONSTITUTION §6). Reindex requires the runtime/maintenance capability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from cctv_memory.application.async_support import run_blocking
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.domain.enums import Capability
from cctv_memory.domain.exceptions import CapabilityDeniedError
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.index import IndexPort, StoredVector
from cctv_memory.repositories.observation import ObservationRecordReadRepository
from cctv_memory.repositories.search_context import SearchContextRepository
from cctv_memory.services.embedding import EmbeddingError, EmbeddingPort

# Channels (text fields) the reindex embeds, mapped to their record attribute.
_VECTOR_CHANNELS: tuple[tuple[str, str], ...] = (
    ("static", "static_description_text"),
    ("dynamic", "dynamic_description_text"),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ReindexResult:
    """Outcome of a reindex run (counts only; no record content)."""

    scanned: int
    reindexed: int
    skipped: int
    vectors_written: int
    model_id: str
    dimension: int


@dataclass(frozen=True)
class SweepResult:
    """Outcome of a context sweep."""

    expired: int


class MaintenanceService:
    """Vector reindex/backfill + SearchContext sweep (admin maintenance)."""

    def __init__(
        self,
        observations: ObservationRecordReadRepository,
        index: IndexPort,
        contexts: SearchContextRepository,
        audit: AuditRepository,
        embedder: EmbeddingPort,
    ) -> None:
        self._observations = observations
        self._index = index
        self._contexts = contexts
        self._audit = audit
        self._embedder = embedder

    def reindex(
        self,
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
        batch_size: int = 64,
        force: bool = False,
        limit: int = 100_000,
    ) -> ReindexResult:
        """Rebuild embeddings for authorized active records. Idempotent/resumable.

        Requires ``runtime.manage``. Records are read within ``scope`` (fail
        closed). For each record, the static/dynamic channels are embedded and
        upserted; a channel already stored under the current ``model_id`` is
        skipped unless ``force``. Re-running is cheap (all skipped) and a model
        change re-embeds everything.
        """
        if Capability.RUNTIME_MANAGE not in scope.capabilities:
            raise CapabilityDeniedError("runtime.manage required for reindex")

        self._audit_event("reindex_started", scope, request_id, {"force": force})

        model_id = self._embedder.model_id
        dimension = self._embedder.dimension
        records = self._observations.authorized_candidate_pool(scope, limit=limit)

        scanned = 0
        reindexed_records: set[str] = set()
        skipped = 0
        vectors_written = 0
        try:
            for start in range(0, len(records), batch_size):
                batch = records[start : start + batch_size]
                written, reindexed_ids, skipped_count = self._reindex_batch(
                    batch, model_id, dimension, force
                )
                vectors_written += written
                reindexed_records |= reindexed_ids
                skipped += skipped_count
                scanned += len(batch)
        except EmbeddingError as exc:
            # Non-sensitive summary only; surfaces as a retryable index failure.
            self._audit_event(
                "reindex_failed", scope, request_id, {"reason": type(exc).__name__}
            )
            raise

        result = ReindexResult(
            scanned=scanned,
            reindexed=len(reindexed_records),
            skipped=skipped,
            vectors_written=vectors_written,
            model_id=model_id,
            dimension=dimension,
        )
        self._audit_event(
            "reindex_succeeded",
            scope,
            request_id,
            {
                "scanned": scanned,
                "reindexed": result.reindexed,
                "skipped": skipped,
                "vectors_written": vectors_written,
                "model_id": model_id,
                "dimension": dimension,
            },
        )
        return result

    def _reindex_batch(
        self,
        batch: list[ObservationRecord],
        model_id: str,
        dimension: int,
        force: bool,
    ) -> tuple[int, set[str], int]:
        """Embed + upsert one batch. Returns (vectors_written, reindexed_ids, skipped)."""
        record_ids = [r.record_id for r in batch]
        existing = self._index.get_vectors_for_records(record_ids)
        # Set of (record_id, vector_type) already stored under the current model.
        current: set[tuple[str, str]] = {
            (v.record_id, v.vector_type)
            for v in existing
            if v.model_id == model_id
        }

        pending_texts: list[str] = []
        pending_keys: list[tuple[str, str]] = []
        skipped = 0
        for rec in batch:
            for vector_type, attr in _VECTOR_CHANNELS:
                if not force and (rec.record_id, vector_type) in current:
                    skipped += 1
                    continue
                pending_texts.append(getattr(rec, attr))
                pending_keys.append((rec.record_id, vector_type))

        if not pending_texts:
            return 0, set(), skipped

        embeddings = run_blocking(self._embedder.embed_texts(pending_texts))
        vectors = [
            StoredVector(
                record_id=rid,
                vector_type=vector_type,
                embedding=embedding,
                model_id=model_id,
                dimension=dimension,
            )
            for (rid, vector_type), embedding in zip(
                pending_keys, embeddings, strict=True
            )
        ]
        written = self._index.upsert_vectors(vectors)
        reindexed_ids = {rid for rid, _ in pending_keys}
        return written, reindexed_ids, skipped

    def sweep_contexts(self, *, now: datetime | None = None) -> SweepResult:
        """Expire stale (past-TTL) active SearchContexts (search-contract §9.5.4).

        No capability gate: this is an internal maintenance sweep that only
        transitions expired contexts to ``expired`` (it reveals nothing and
        grants nothing). Returns the number expired.
        """
        expired = self._contexts.expire_contexts(now or _now())
        return SweepResult(expired=expired)

    def _audit_event(
        self,
        event_type: str,
        scope: AuthorizedScope,
        request_id: str | None,
        metadata: dict[str, object],
    ) -> None:
        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type=event_type,
                request_id=request_id,
                principal_id=scope.principal_id,
                resource_scope_hash=scope.scope_hash,
                metadata=metadata,
            )
        )
