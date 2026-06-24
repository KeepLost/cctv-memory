"""AI embedding request-format adapters (task §2).

This module isolates the *wire format* of an embedding provider from everything
else. An ``EmbeddingRequestFormat`` knows how to:

- build the request path + JSON payload from a list of input texts, and
- parse the provider's JSON response into a list of float vectors.

It performs NO network I/O — the async HTTP adapter (``siliconflow_embedder``)
owns transport and injects the format. This keeps two boundaries clean:

1. Upper/business layers depend only on ``EmbeddingPort`` (services layer) and
   never see payloads (ARCHITECTURE_CONSTITUTION §3/§4).
2. New providers/formats are added by implementing this protocol — no app-layer
   branching (pipeline-experiment §2.5/§7).

The format objects intentionally do not hold the API key or base URL; secrets and
endpoints live with the transport adapter / config (configuration-contract §6).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from cctv_memory.services.embedding import EmbeddingError


@runtime_checkable
class EmbeddingRequestFormat(Protocol):
    """Provider-agnostic embedding request/response format (no I/O)."""

    @property
    def path(self) -> str:
        """Endpoint path appended to the provider base URL (e.g. ``/embeddings``)."""
        ...

    def build_payload(self, texts: list[str]) -> dict[str, Any]:
        """Build the JSON request body for embedding ``texts``."""
        ...

    def parse_response(
        self, body: dict[str, Any], *, expected_count: int
    ) -> list[list[float]]:
        """Parse the provider response into ``expected_count`` float vectors.

        Raises ``EmbeddingError`` if the shape is unexpected or the vector count
        does not match ``expected_count``.
        """
        ...


class OpenAICompatibleEmbeddingFormat:
    """OpenAI-compatible embeddings format (used by SiliconFlow bge-m3).

    Request (task §"Endpoint Facts"):
        POST <base>/embeddings
        {"model": <model_id>, "input": str | str[], "encoding_format": "float"}

    Response (OpenAI shape):
        {"data": [{"embedding": [float, ...], "index": 0}, ...], ...}

    Embeddings are returned in ``data[*].index`` order so callers always get
    vectors aligned to their input order.
    """

    def __init__(
        self,
        *,
        model_id: str,
        path: str = "/embeddings",
        encoding_format: str = "float",
    ) -> None:
        self._model_id = model_id
        self._path = path
        self._encoding_format = encoding_format

    @property
    def path(self) -> str:
        return self._path

    def build_payload(self, texts: list[str]) -> dict[str, Any]:
        # The OpenAI-compatible API accepts a string or array; we always send an
        # array for a deterministic response shape, even for a single query.
        return {
            "model": self._model_id,
            "input": texts,
            "encoding_format": self._encoding_format,
        }

    def parse_response(
        self, body: dict[str, Any], *, expected_count: int
    ) -> list[list[float]]:
        data = body.get("data")
        if not isinstance(data, list):
            raise EmbeddingError("unexpected embedding response: missing 'data' array")
        if len(data) != expected_count:
            raise EmbeddingError(
                f"embedding count mismatch: expected {expected_count}, got {len(data)}"
            )
        # Order by the provider-reported index when present (defensive); fall back
        # to response order otherwise.
        indexed: list[tuple[int, list[float]]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict) or "embedding" not in item:
                raise EmbeddingError("unexpected embedding response: missing 'embedding'")
            raw = item["embedding"]
            if not isinstance(raw, list):
                raise EmbeddingError("unexpected embedding response: embedding not a list")
            try:
                vector = [float(x) for x in raw]
            except (TypeError, ValueError) as exc:
                raise EmbeddingError("embedding contained non-numeric values") from exc
            index_value = item.get("index", position)
            index = index_value if isinstance(index_value, int) else position
            indexed.append((index, vector))
        indexed.sort(key=lambda pair: pair[0])
        return [vector for _, vector in indexed]
