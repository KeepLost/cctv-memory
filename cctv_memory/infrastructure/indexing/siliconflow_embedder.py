"""Async SiliconFlow / OpenAI-compatible embedding adapter (task §3).

Implements ``EmbeddingPort`` over a URL endpoint using ``httpx.AsyncClient`` with
a bounded timeout and a minimal transient retry (mirroring the real VLM adapter
pattern). It owns transport only; the request/response *format* is delegated to
an injected ``EmbeddingRequestFormat`` (default: OpenAI-compatible), so the wire
shape is never built by upper layers (task §2).

Provider facts (task §"Endpoint Facts"):
- base URL  : ``http://nginx:8081/api/siliconflow`` (from config, env-overridable)
- path      : ``/embeddings``
- model     : ``BAAI/bge-m3``
- dimensions: ``1024``
- payload   : ``{model, input, encoding_format: "float"}``

The API key comes from an env var NAME held in config; the value is read by the
composition root and passed in. This adapter never prints/logs the key, URL body,
or raw provider response, and raises ``EmbeddingError`` with a non-sensitive
summary on failure (configuration-contract §6, error-code-contract §2).
"""

from __future__ import annotations

import httpx

from cctv_memory.infrastructure.indexing.formats import (
    EmbeddingRequestFormat,
    OpenAICompatibleEmbeddingFormat,
)
from cctv_memory.services.embedding import EmbeddingError

_BGE_M3_MODEL = "BAAI/bge-m3"
_BGE_M3_DIMENSION = 1024


class SiliconFlowEmbedder:
    """Async embedder over an OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str = _BGE_M3_MODEL,
        dimension: int = _BGE_M3_DIMENSION,
        request_format: EmbeddingRequestFormat | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Normalize so base + path join cleanly regardless of trailing slash.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_id = model_id
        self._dimension = dimension
        self._format: EmbeddingRequestFormat = request_format or (
            OpenAICompatibleEmbeddingFormat(model_id=model_id)
        )
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        # An injected client (tests) avoids real network; otherwise one is made
        # per call so the adapter holds no long-lived connection.
        self._client = client

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = await self._request(texts)
        self._validate_dimensions(vectors)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_texts([text])
        return vectors[0]

    def _validate_dimensions(self, vectors: list[list[float]]) -> None:
        for vector in vectors:
            if len(vector) != self._dimension:
                raise EmbeddingError(
                    f"embedding dimension mismatch: expected {self._dimension}, "
                    f"got {len(vector)}"
                )

    async def _request(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._base_url}{self._format.path}"
        payload = self._format.build_payload(texts)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: EmbeddingError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                body = await self._post(url, headers, payload)
                return self._format.parse_response(body, expected_count=len(texts))
            except EmbeddingError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    continue
                raise
        # Unreachable, but keeps the type checker happy.
        raise last_error or EmbeddingError("embedding request failed")

    async def _post(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> dict[str, object]:
        try:
            if self._client is not None:
                response = await self._client.post(
                    url, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise EmbeddingError("embedding request timed out") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"embedding request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            raise EmbeddingError(f"embedding provider returned status {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise EmbeddingError("embedding provider returned non-JSON body") from exc
        if not isinstance(data, dict):
            raise EmbeddingError("embedding provider returned unexpected JSON type")
        return data
