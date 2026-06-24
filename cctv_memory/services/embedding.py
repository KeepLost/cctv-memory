"""Abstract service port: text embedding (module-map §2.4 services section).

The ``EmbeddingPort`` is the boundary the application/orchestration layer depends
on for generating text embeddings. Upper layers depend ONLY on this port and the
plain ``list[float]`` vectors it returns — they never know the provider request
format, the endpoint, or the HTTP payload shape (task §1/§3). Provider/model
selection is a composition-root + adapter concern (pipeline-experiment §2.5/§7).

All embedding calls are asynchronous (task §5): the real provider call goes to a
URL endpoint over ``httpx.AsyncClient``; the mock implementation is async too so
callers have a single async contract regardless of provider.

Embeddings are index artifacts only; an ``EmbeddingPort`` never writes an active
ObservationRecord (ARCHITECTURE_CONSTITUTION §6).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.domain.exceptions import DomainError


class EmbeddingError(DomainError):
    """Embedding provider/transport failure.

    Carries only a non-sensitive summary (never the API key, URL, or raw
    provider body) so error propagation cannot leak secrets
    (error-code-contract §2, configuration-contract §6).
    """


@runtime_checkable
class EmbeddingPort(Protocol):
    """Port for generating text embeddings (async).

    Implementations (mock or a real provider adapter) return unit-length-agnostic
    float vectors of a fixed ``dimension``. Document and query embedding share the
    same vector space so cosine similarity is meaningful in later semantic search
    (C2); this C1 task only provides the capability, it does not wire search.
    """

    @property
    def dimension(self) -> int:
        """The fixed embedding dimension (e.g. 1024 for bge-m3)."""
        ...

    @property
    def model_id(self) -> str:
        """Stable model identifier stored alongside vectors for reindex/versioning."""
        ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Returns one vector per input text, in input order, each of length
        ``dimension``. Raises ``EmbeddingError`` on provider/transport failure or
        if the returned vector count/dimension does not match expectations.
        """
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into one vector of length ``dimension``."""
        ...
