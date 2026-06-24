"""Observation search use case (application/search.py).

Phase 5: full search with FTS5 + static/dynamic/hybrid (RRF) modes, SearchContext
/ Revision / Candidate lifecycle (TTL + idle expiry, limits), refine ops, facets,
and overlap. AuthorizedScope is applied during candidate retrieval (in SQL/FTS),
before any ranking/facet/count (search-contract §10, ARCHITECTURE_CONSTITUTION §5).
Unauthorized records never enter results, candidate_count, or facets.

Scoring uses RRF over static-FTS and dynamic-FTS rank lists plus tag /
analysis_scale boosts (domain.ranking). When FTS is unavailable, the service
falls back to deterministic keyword counting so behavior stays stable.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from cctv_memory.application.async_support import run_blocking
from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.search import (
    RefineObservationSearchRequest,
    SearchCandidate,
    SearchContext,
    SearchResultItem,
    SearchRevision,
    StartObservationSearchRequest,
    StartObservationSearchResponse,
)
from cctv_memory.domain import ranking
from cctv_memory.domain.enums import AnalysisScale, Capability, RefineOp, SearchMode
from cctv_memory.domain.exceptions import (
    CapabilityDeniedError,
    ContextExpiredError,
    LimitExceededError,
    NotFoundError,
)
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.index import IndexPort
from cctv_memory.repositories.observation import ObservationRecordReadRepository
from cctv_memory.repositories.search_context import SearchContextRepository
from cctv_memory.services.embedding import EmbeddingError, EmbeddingPort
from cctv_memory.services.reranker import (
    RerankDocument,
    RerankerError,
    RerankerPort,
)

DATASET_REVISION = "data_rev_mvp"


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _str_list(value: object) -> list[str]:
    """Coerce a refine-param value into a list of strings (defensive)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _keyword_relevance(
    records: list[ObservationRecord], query: str, field: str
) -> dict[str, float]:
    """Deterministic LIKE-style relevance fallback when FTS is unavailable."""
    terms = [t for t in query.lower().replace(",", " ").split() if t]
    if not terms:
        return {}
    out: dict[str, float] = {}
    for rec in records:
        if field == "static":
            text = rec.static_description_text.lower()
        elif field == "dynamic":
            text = rec.dynamic_description_text.lower()
        else:
            text = " ".join(rec.tags).lower()
        hits = sum(1 for t in terms if t in text)
        if hits:
            out[rec.record_id] = float(hits)
    return out


class SearchService:
    """Full search: start / refine / facets / overlap with RRF and lifecycle."""

    def __init__(
        self,
        observations: ObservationRecordReadRepository,
        contexts: SearchContextRepository,
        audit: AuditRepository,
        *,
        context_ttl_seconds: int = 900,
        context_idle_seconds: int = 300,
        max_top_k: int = 100,
        max_candidates_per_revision: int = 1000,
        max_revisions_per_context: int = 8,
        weights: ranking.RrfWeights | None = None,
        embedder: EmbeddingPort | None = None,
        index: IndexPort | None = None,
        vector_search_enabled: bool = False,
        reranker: RerankerPort | None = None,
        rerank_enabled: bool = False,
        rerank_top_n: int = 50,
    ) -> None:
        self._observations = observations
        self._contexts = contexts
        self._audit = audit
        self._context_ttl_seconds = context_ttl_seconds
        self._context_idle_seconds = context_idle_seconds
        self._max_top_k = max_top_k
        self._max_candidates_per_revision = max_candidates_per_revision
        self._max_revisions_per_context = max_revisions_per_context
        self._weights = weights or ranking.RrfWeights()
        # C2 semantic rerank dependencies. Vector reranking only runs when it is
        # explicitly enabled AND both an embedder and an index are wired; otherwise
        # the service behaves exactly like the FTS-only path (deterministic
        # fallback), so existing behavior/tests are unchanged when indexing is off.
        self._embedder = embedder
        self._index = index
        self._vector_search_enabled = vector_search_enabled
        # C3 external cross-encoder reranker. Opt-in/config-gated; only ever sees
        # the already-authorized candidate documents (never the full corpus).
        self._reranker = reranker
        self._rerank_enabled = rerank_enabled
        self._rerank_top_n = rerank_top_n

    @property
    def _vector_enabled(self) -> bool:
        return (
            self._vector_search_enabled
            and self._embedder is not None
            and self._index is not None
        )

    @property
    def _reranker_enabled(self) -> bool:
        return self._rerank_enabled and self._reranker is not None

    # ---- start -----------------------------------------------------------

    def start_search(
        self,
        request: StartObservationSearchRequest,
        scope: AuthorizedScope,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> StartObservationSearchResponse:
        if Capability.OBSERVATION_SEARCH not in scope.capabilities:
            raise CapabilityDeniedError("observation.search required")
        if request.top_k > self._max_top_k:
            raise LimitExceededError(f"top_k exceeds max {self._max_top_k}")
        top_k = min(request.top_k, self._max_top_k)

        pool = self._authorized_pool(request, scope)
        ordered = self._rank(
            pool,
            query_text=request.query_text,
            search_mode=request.search_mode,
            tag_filters=request.tag_filters,
            preferred_scales=request.preferred_analysis_scales,
        )
        ordered = ordered[:top_k]

        now = _now()
        context = SearchContext(
            context_id=_new_id("ctx"),
            tenant_id=scope.tenant_id,
            principal_id=scope.principal_id,
            session_id=session_id,
            authorized_scope_hash=scope.scope_hash,
            dataset_revision=DATASET_REVISION,
            default_revision_id=None,
            created_at=now,
            last_accessed_at=now,
            expires_at=now + timedelta(seconds=self._context_ttl_seconds),
            status="active",
        )
        self._contexts.create_context(context)

        pool_by_id = {r.record_id: r for r in pool}
        revision = self._persist_revision(
            context_id=context.context_id,
            parent_revision_id=None,
            op="start",
            op_params={"query_text": request.query_text, "search_mode": request.search_mode.value,
                       "top_k": top_k},
            ordered=ordered,
            pool_by_id=pool_by_id,
        )
        self._contexts.replace_default_revision(context.context_id, revision.revision_id)

        results = self._to_results(ordered, pool_by_id)
        self._audit_query(
            request_id, scope, context.context_id, session_id, [r.record_id for r in results]
        )
        return StartObservationSearchResponse(
            context_id=context.context_id,
            revision_id=revision.revision_id,
            candidate_count=len(results),
            facets=revision.facets,
            results=results,
        )

    # ---- refine ----------------------------------------------------------

    def refine_search(
        self,
        context_id: str,
        request: RefineObservationSearchRequest,
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
    ) -> StartObservationSearchResponse:
        if Capability.OBSERVATION_SEARCH not in scope.capabilities:
            raise CapabilityDeniedError("observation.search required")
        context = self._active_context(context_id, scope)

        base = self._contexts.get_revision(request.base_revision_id)
        if base is None or base.context_id != context_id:
            raise NotFoundError("base revision not found in context")

        # Reload the base revision's candidates -> the working record set. This is
        # already within the authorized scope (candidates only ever held authorized
        # records). Re-fetch under scope to be safe (refine never widens scope).
        base_candidates = self._contexts.list_candidates(
            base.revision_id, limit=self._max_candidates_per_revision
        ).items
        base_ids = [c.record_id for c in base_candidates]
        records = self._observations.get_authorized_active_by_ids(base_ids, scope)

        params = request.params
        raw_query = params.get("query_text")
        query_text = str(raw_query) if isinstance(raw_query, str) else None
        raw_top_k = params.get("top_k", self._max_top_k)
        top_k = int(raw_top_k) if isinstance(raw_top_k, int) else self._max_top_k
        top_k = min(top_k, self._max_top_k)

        ordered = self._apply_refine_op(request.op, records, params, query_text)
        ordered = ordered[:top_k]

        self._enforce_revision_limit(context_id)
        pool_by_id = {r.record_id: r for r in records}
        revision = self._persist_revision(
            context_id=context_id,
            parent_revision_id=base.revision_id,
            op=request.op.value,
            op_params=params,
            ordered=ordered,
            pool_by_id=pool_by_id,
        )
        self._contexts.replace_default_revision(context_id, revision.revision_id)

        results = self._to_results(ordered, pool_by_id)
        self._audit_query(
            request_id, scope, context_id, context.session_id, [r.record_id for r in results]
        )
        return StartObservationSearchResponse(
            context_id=context_id,
            revision_id=revision.revision_id,
            candidate_count=len(results),
            facets=revision.facets,
            results=results,
        )

    def batch_refine_search(
        self,
        context_id: str,
        requests: list[RefineObservationSearchRequest],
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
    ) -> list[StartObservationSearchResponse]:
        """Apply several refine ops against the same context (api-routes §4).

        Each refinement is executed independently via ``refine_search`` (so each
        produces its own immutable revision) and never widens the authorized
        scope. Returns one response per refinement, in input order.
        """
        return [
            self.refine_search(context_id, req, scope, request_id=request_id)
            for req in requests
        ]

    def _apply_refine_op(
        self,
        op: RefineOp,
        records: list[ObservationRecord],
        params: dict[str, object],
        query_text: str | None,
    ) -> list[tuple[str, float, dict[str, object]]]:
        tag_filters = _str_list(params.get("tags"))
        if op is RefineOp.NARROW_BY_TAGS:
            wanted = set(tag_filters)
            filtered = [r for r in records if wanted.issubset(set(r.tags))] if wanted else records
            return self._rank(filtered, query_text=None, search_mode=SearchMode.HYBRID,
                              tag_filters=tag_filters, preferred_scales=[])
        if op is RefineOp.SEARCH_STATIC_TEXT:
            return self._rank(records, query_text=query_text,
                              search_mode=SearchMode.STATIC_ATTRIBUTE, tag_filters=[],
                              preferred_scales=[])
        if op is RefineOp.SEARCH_DYNAMIC_TEXT:
            return self._rank(records, query_text=query_text,
                              search_mode=SearchMode.DYNAMIC_EVENT, tag_filters=[],
                              preferred_scales=[])
        if op is RefineOp.HYBRID_SEARCH_TEXT:
            return self._rank(records, query_text=query_text, search_mode=SearchMode.HYBRID,
                              tag_filters=tag_filters, preferred_scales=[])
        if op is RefineOp.FILTER_BY_ANALYSIS_SCALE:
            scales = set(_str_list(params.get("analysis_scales")))
            filtered = (
                [r for r in records if r.analysis_scale.value in scales]
                if scales
                else records
            )
            return self._rank(filtered, query_text=query_text, search_mode=SearchMode.HYBRID,
                              tag_filters=[], preferred_scales=[])
        if op is RefineOp.RERANK_CURRENT_CANDIDATES:
            # Semantic rerank: force the vector channel on (within the authorized
            # candidate set already held by this revision). Falls back to FTS-only
            # fusion when indexing is disabled or vectors are absent.
            ranked = self._rank(records, query_text=query_text, search_mode=SearchMode.HYBRID,
                                tag_filters=tag_filters, preferred_scales=[], use_vector=True)
            # Optional external cross-encoder rerank stage (C3), applied ONLY to the
            # already-authorized, already-ranked candidate set. Config-gated/opt-in.
            return self._apply_cross_encoder(ranked, records, query_text)
        # APPLY_RRF_FUSION or unknown: hybrid re-rank.
        return self._rank(records, query_text=query_text, search_mode=SearchMode.HYBRID,
                          tag_filters=tag_filters, preferred_scales=[])

    # ---- facets ----------------------------------------------------------

    def facets(self, context_id: str, scope: AuthorizedScope) -> dict[str, object]:
        if Capability.OBSERVATION_SEARCH not in scope.capabilities:
            raise CapabilityDeniedError("observation.search required")
        context = self._active_context(context_id, scope)
        if context.default_revision_id is None:
            return self._build_facets([])
        candidates = self._contexts.list_candidates(
            context.default_revision_id, limit=self._max_candidates_per_revision
        ).items
        records = self._observations.get_authorized_active_by_ids(
            [c.record_id for c in candidates], scope
        )
        return self._build_facets(records)

    # ---- close -----------------------------------------------------------

    def close_context(self, context_id: str, scope: AuthorizedScope) -> None:
        context = self._contexts.get_context(context_id)
        if context is None or context.principal_id != scope.principal_id:
            raise NotFoundError("context not found")
        self._contexts.close_context(context_id)

    # ---- helpers ---------------------------------------------------------

    def _authorized_pool(
        self, request: StartObservationSearchRequest, scope: AuthorizedScope
    ) -> list[ObservationRecord]:
        time_start = request.time_range.start if request.time_range else None
        time_end = request.time_range.end if request.time_range else None
        return self._observations.authorized_candidate_pool(
            scope,
            time_start=time_start,
            time_end=time_end,
            camera_ids=request.camera_ids or None,
            location_ids=request.location_ids or None,
            video_ids=request.video_ids or None,
            analysis_scale_filter=request.analysis_scale_filter or None,
            tag_filters=request.tag_filters or None,
            limit=self._max_candidates_per_revision,
        )

    def _rank(
        self,
        records: list[ObservationRecord],
        *,
        query_text: str | None,
        search_mode: SearchMode,
        tag_filters: list[str],
        preferred_scales: list[AnalysisScale],
        use_vector: bool | None = None,
    ) -> list[tuple[str, float, dict[str, object]]]:
        """Rank records via RRF over the requested channels. Deterministic.

        When vector rerank is enabled and a query is present, a semantic cosine
        channel is computed STRICTLY within the candidate id set (which is already
        authorized) and fused alongside the FTS channels. ``use_vector`` defaults
        to the service-level vector enablement; callers (e.g. an explicit
        ``rerank_current_candidates`` op) may force it on. The fusion gracefully
        degrades to FTS-only when no vectors are stored.
        """
        candidate_ids = [r.record_id for r in records]
        if not candidate_ids:
            return []

        use_static = search_mode in (SearchMode.STATIC_ATTRIBUTE, SearchMode.HYBRID,
                                      SearchMode.AUTO_BY_EXTERNAL_AI)
        use_dynamic = search_mode in (SearchMode.DYNAMIC_EVENT, SearchMode.HYBRID,
                                      SearchMode.AUTO_BY_EXTERNAL_AI)

        static_scores: dict[str, float] = {}
        dynamic_scores: dict[str, float] = {}
        if query_text:
            if use_static:
                static_scores = self._channel_scores(records, query_text, "static")
            if use_dynamic:
                dynamic_scores = self._channel_scores(records, query_text, "dynamic")

        # If no text query, rank by recency proxy so order is deterministic and
        # every candidate is retained (filters/facets still meaningful).
        if not query_text:
            ordered_ids = sorted(
                candidate_ids,
                key=lambda rid: (records_index(records)[rid].observed_start_time, rid),
            )
            inputs = ranking.ChannelInputs(
                static_ranks={rid: i + 1 for i, rid in enumerate(ordered_ids)},
            )
            fused = ranking.rrf_fuse(ordered_ids, inputs, self._weights)
            return ranking.order_candidates(fused)

        # C2 semantic vector channel (within the authorized candidate id set only).
        want_vector = self._vector_enabled if use_vector is None else (
            use_vector and self._vector_enabled
        )
        static_vec: dict[str, float] = {}
        dynamic_vec: dict[str, float] = {}
        if want_vector:
            if use_static:
                static_vec = self._vector_channel(candidate_ids, query_text, "static")
            if use_dynamic:
                dynamic_vec = self._vector_channel(candidate_ids, query_text, "dynamic")
        # Combine per-channel cosine into a single vector relevance (max across
        # the active channels) used for the fused vector rank list.
        combined_vec: dict[str, float] = {}
        for rid in candidate_ids:
            scores = [v for v in (static_vec.get(rid), dynamic_vec.get(rid)) if v is not None]
            if scores:
                combined_vec[rid] = max(scores)

        inputs = ranking.ChannelInputs(
            static_ranks=ranking.to_rank_map(static_scores) if static_scores else {},
            dynamic_ranks=ranking.to_rank_map(dynamic_scores) if dynamic_scores else {},
            tag_boosts=self._tag_boosts(records, tag_filters),
            scale_boosts=self._scale_boosts(records, preferred_scales),
            vector_ranks=ranking.to_rank_map(combined_vec) if combined_vec else {},
            static_vector_scores=static_vec,
            dynamic_vector_scores=dynamic_vec,
        )
        # Keep candidates that matched at least one channel (text OR vector) when a
        # query was given; otherwise keep all (avoid empty results). The vector
        # channel can recall records FTS missed, but always within the authorized
        # candidate id set (never the full corpus).
        matched_ids = (
            set(inputs.static_ranks)
            | set(inputs.dynamic_ranks)
            | set(inputs.vector_ranks)
        )
        ranked_ids = list(matched_ids) if matched_ids else candidate_ids
        fused = ranking.rrf_fuse(ranked_ids, inputs, self._weights)
        return ranking.order_candidates(fused)

    def _vector_channel(
        self, candidate_ids: list[str], query_text: str, vector_type: str
    ) -> dict[str, float]:
        """Cosine similarity of the query vs stored candidate vectors.

        Vectors are fetched ONLY for the explicit ``candidate_ids`` set (already
        authorized) via the IndexPort — there is no full-corpus vector search
        (ARCHITECTURE_CONSTITUTION §5, database-capability-contract §6.4). Returns
        {} on any embedding/transport failure so search degrades to FTS instead of
        erroring (deterministic fallback).
        """
        if self._embedder is None or self._index is None:
            return {}
        stored = self._index.get_vectors_for_records(
            candidate_ids, vector_type=vector_type
        )
        if not stored:
            return {}
        try:
            query_vec = run_blocking(self._embedder.embed_query(query_text))
        except EmbeddingError:
            return {}
        out: dict[str, float] = {}
        for sv in stored:
            similarity = ranking.cosine_similarity(query_vec, sv.embedding)
            if similarity > 0.0:
                out[sv.record_id] = similarity
        return out

    def _apply_cross_encoder(
        self,
        ranked: list[tuple[str, float, dict[str, object]]],
        records: list[ObservationRecord],
        query_text: str | None,
    ) -> list[tuple[str, float, dict[str, object]]]:
        """Reorder the top-N already-authorized candidates with the reranker (C3).

        Only the candidates already produced by the authorized-scope ranking are
        sent to the reranker (never the full corpus). The reranker score becomes
        the new ordering key for the reranked head; the tail (beyond ``top_n``) is
        appended unchanged. The score_detail records the cross-encoder score and
        model so results stay explainable. Falls back to the input order on any
        reranker failure (deterministic, no hard dependency).
        """
        if not self._reranker_enabled or self._reranker is None or not query_text or not ranked:
            return ranked
        text_by_id = {r.record_id: r.static_description_text for r in records}
        head = ranked[: self._rerank_top_n]
        tail = ranked[self._rerank_top_n :]
        documents = [
            RerankDocument(record_id=rid, text=text_by_id.get(rid, ""))
            for rid, _, _ in head
        ]
        try:
            scores = run_blocking(self._reranker.rerank(query_text, documents))
        except RerankerError:
            return ranked
        score_by_id = {s.record_id: s.score for s in scores}
        model_id = self._reranker.model_id
        rescored: list[tuple[str, float, dict[str, object]]] = []
        for rid, prior_score, detail in head:
            rerank_score = score_by_id.get(rid, 0.0)
            new_detail = dict(detail)
            new_detail["rerank_score"] = round(rerank_score, 6)
            new_detail["rerank_model"] = model_id
            new_detail["prefusion_score"] = round(prior_score, 6)
            rescored.append((rid, rerank_score, new_detail))
        # Sort the reranked head by the cross-encoder score (deterministic
        # tie-break by record_id), keep the untouched tail after it.
        rescored.sort(key=lambda x: (-x[1], x[0]))
        return rescored + tail

    def _channel_scores(
        self, records: list[ObservationRecord], query_text: str, field: str
    ) -> dict[str, float]:
        ids = [r.record_id for r in records]
        if self._observations.fts_available():
            scores = self._observations.fts_rank(query_text, ids, field=field)
            if scores:
                return scores
        # Fallback: deterministic keyword counting.
        return _keyword_relevance(records, query_text, field)

    def _tag_boosts(
        self, records: list[ObservationRecord], tag_filters: list[str]
    ) -> dict[str, float]:
        if not tag_filters:
            return {}
        wanted = [t.lower() for t in tag_filters]
        out: dict[str, float] = {}
        for r in records:
            matched = sum(1 for t in wanted if t in [x.lower() for x in r.tags])
            if matched:
                out[r.record_id] = 0.05 * matched
        return out

    def _scale_boosts(
        self, records: list[ObservationRecord], preferred_scales: list[AnalysisScale]
    ) -> dict[str, float]:
        if not preferred_scales:
            return {}
        preferred = {s.value for s in preferred_scales}
        return {
            r.record_id: 0.1 for r in records if r.analysis_scale.value in preferred
        }

    def _persist_revision(
        self,
        *,
        context_id: str,
        parent_revision_id: str | None,
        op: str,
        op_params: dict[str, object],
        ordered: list[tuple[str, float, dict[str, object]]],
        pool_by_id: dict[str, ObservationRecord],
    ) -> SearchRevision:
        revision_id = _new_id("rev")
        facet_records = [pool_by_id[rid] for rid, _, _ in ordered if rid in pool_by_id]
        revision = SearchRevision(
            revision_id=revision_id,
            context_id=context_id,
            parent_revision_id=parent_revision_id,
            op=op,
            op_params=op_params,
            candidate_count=len(ordered),
            facets=self._build_facets(facet_records),
            created_at=_now(),
        )
        candidate_rows = [
            SearchCandidate(
                revision_id=revision_id,
                record_id=rid,
                rank=i + 1,
                score=score,
                score_detail=detail,
            )
            for i, (rid, score, detail) in enumerate(ordered)
        ]
        self._contexts.create_revision(revision, candidate_rows)
        return revision

    def _to_results(
        self,
        ordered: list[tuple[str, float, dict[str, object]]],
        pool_by_id: dict[str, ObservationRecord],
    ) -> list[SearchResultItem]:
        results: list[SearchResultItem] = []
        for i, (rid, score, detail) in enumerate(ordered):
            rec = pool_by_id.get(rid)
            if rec is None:
                continue
            results.append(
                SearchResultItem(
                    record_id=rid,
                    rank=i + 1,
                    score=score,
                    score_detail=detail,
                    preview_text=rec.static_description_text[:200],
                    analysis_scale=rec.analysis_scale,
                    observed_start_time=rec.observed_start_time,
                    observed_end_time=rec.observed_end_time,
                )
            )
        return results

    def _active_context(self, context_id: str, scope: AuthorizedScope) -> SearchContext:
        context = self._contexts.get_context(context_id)
        if context is None or context.principal_id != scope.principal_id:
            raise NotFoundError("context not found")
        if context.authorized_scope_hash != scope.scope_hash:
            # Scope changed since context creation -> expire to avoid drift.
            self._contexts.close_context(context_id)
            raise ContextExpiredError("authorized scope changed; restart search")
        if context.status != "active":
            raise ContextExpiredError("context is not active")
        now = _now()
        if context.expires_at is not None and now > context.expires_at:
            self._contexts.expire_contexts(now)
            raise ContextExpiredError("context expired")
        if (
            context.last_accessed_at is not None
            and now > context.last_accessed_at + timedelta(seconds=self._context_idle_seconds)
        ):
            self._contexts.expire_contexts(now)
            raise ContextExpiredError("context idle-expired")
        return context

    def _enforce_revision_limit(self, context_id: str) -> None:
        count = self._contexts.count_revisions(context_id)
        if count >= self._max_revisions_per_context:
            raise LimitExceededError(
                f"context revisions exceed max {self._max_revisions_per_context}"
            )

    def _audit_query(
        self,
        request_id: str | None,
        scope: AuthorizedScope,
        context_id: str,
        session_id: str | None,
        record_ids: list[str],
    ) -> None:
        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type="query",
                request_id=request_id,
                principal_id=scope.principal_id,
                session_id=session_id,
                context_id=context_id,
                resource_scope_hash=scope.scope_hash,
                record_ids=record_ids,
                metadata={"candidate_count": len(record_ids)},
            )
        )

    @staticmethod
    def _build_facets(records: list[ObservationRecord]) -> dict[str, object]:
        """Build facets over authorized candidates only (search-contract §6)."""
        tag_counter: Counter[str] = Counter()
        camera_counter: Counter[str] = Counter()
        location_counter: Counter[str] = Counter()
        scale_counter: Counter[str] = Counter()
        time_buckets: Counter[str] = Counter()
        for rec in records:
            tag_counter.update(rec.tags)
            camera_counter[rec.camera_id] += 1
            location_counter[rec.location_id] += 1
            scale_counter[rec.analysis_scale.value] += 1
            # Hour bucket for time distribution.
            bucket = rec.observed_start_time.replace(minute=0, second=0, microsecond=0)
            time_buckets[bucket.isoformat()] += 1
        return {
            "candidate_count": len(records),
            "top_tags": [
                {"tag": tag, "count": count} for tag, count in tag_counter.most_common(10)
            ],
            "camera_distribution": [
                {"camera_id": cam, "count": count}
                for cam, count in camera_counter.most_common()
            ],
            "location_distribution": [
                {"location_id": loc, "count": count}
                for loc, count in location_counter.most_common()
            ],
            "time_distribution": [
                {"bucket_start": bucket, "count": count}
                for bucket, count in sorted(time_buckets.items())
            ],
            "analysis_scale_distribution": [
                {"analysis_scale": scale, "count": count}
                for scale, count in scale_counter.most_common()
            ],
        }


def records_index(records: list[ObservationRecord]) -> dict[str, ObservationRecord]:
    """Return a {record_id: record} index."""
    return {r.record_id: r for r in records}
