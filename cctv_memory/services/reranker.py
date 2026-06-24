"""Abstract service port: cross-encoder reranking (services/reranker.py).

The ``RerankerPort`` is the boundary the search use case depends on to reorder an
ALREADY-AUTHORIZED candidate set by semantic relevance to a query, using an
external cross-encoder reranker (e.g. SiliconFlow ``Qwen/Qwen3-Reranker-8B``).
Upper layers depend ONLY on this port and the plain scores it returns — they
never know the provider request format, endpoint, or payload shape (task §C3,
ARCHITECTURE_CONSTITUTION §3/§4; pipeline-experiment §2.5/§7).

All reranker calls are asynchronous (task §"all AI calls async"): the real
provider call goes to a URL endpoint over ``httpx.AsyncClient``; the mock
implementation is async too so callers have a single async contract.

Reranking is a relevance reorder of candidate documents only; it NEVER writes an
ObservationRecord and NEVER widens the candidate set — the caller passes the
authorized candidate documents and gets back per-document scores (§5/§6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from cctv_memory.domain.exceptions import DomainError


class RerankerError(DomainError):
    """Reranker provider/transport failure.

    Carries only a non-sensitive summary (never the API key, URL, or raw provider
    body) so propagation cannot leak secrets (error-code-contract §2,
    configuration-contract §6).
    """


@dataclass(frozen=True)
class RerankDocument:
    """One candidate document to rerank (already authorized).

    ``record_id`` ties the score back to the authorized candidate; ``text`` is the
    document text the reranker scores against the query.
    """

    record_id: str
    text: str


@dataclass(frozen=True)
class RerankScore:
    """A reranker relevance score for one candidate document."""

    record_id: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class RerankerPort(Protocol):
    """Port for cross-encoder reranking of authorized candidate documents (async)."""

    @property
    def model_id(self) -> str:
        """Stable reranker model identifier (recorded in score_detail/audit)."""
        ...

    async def rerank(
        self, query: str, documents: list[RerankDocument]
    ) -> list[RerankScore]:
        """Return a relevance score per input document (input order preserved).

        Implementations score each document against ``query``. Raises
        ``RerankerError`` on provider/transport failure. The caller fuses/sorts;
        the reranker only scores the documents it is given (never the full corpus).
        """
        ...
