"""Async SiliconFlow reranker adapter (task §C3).

Implements ``RerankerPort`` over a URL endpoint using ``httpx.AsyncClient`` with a
bounded timeout and a minimal transient retry (mirroring the embedder/VLM adapter
pattern). It owns transport only; the provider request/response *shape* is built
and parsed here, hidden from upper layers so the application never sees the
payload (task §2, ARCHITECTURE_CONSTITUTION §3/§4).

Provider facts (next-roadmap-review §5.1):
- base URL : ``http://nginx:8081/api/siliconflow`` (from config, env-overridable)
- path     : ``/rerank``
- model    : ``Qwen/Qwen3-Reranker-8B``
- payload  : ``{model, query, documents: [str, ...], top_n?}``
- response : ``{results: [{index, relevance_score}, ...]}`` (OpenAI-rerank shape)

The API key comes from an env var NAME held in config; the value is read by the
composition root and passed in. This adapter never prints/logs the key, payload,
or raw provider response, and raises ``RerankerError`` with a non-sensitive
summary on failure (configuration-contract §6, error-code-contract §2).
"""

from __future__ import annotations

from typing import Any

import httpx

from cctv_memory.services.reranker import RerankDocument, RerankerError, RerankScore

_QWEN_RERANKER_MODEL = "Qwen/Qwen3-Reranker-8B"


class SiliconFlowReranker:
    """Async reranker over a SiliconFlow-compatible ``/rerank`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str = _QWEN_RERANKER_MODEL,
        path: str = "/rerank",
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_id = model_id
        self._path = path
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    async def rerank(
        self, query: str, documents: list[RerankDocument]
    ) -> list[RerankScore]:
        if not documents:
            return []
        body = await self._request(query, [d.text for d in documents])
        return self._parse(body, documents)

    def _build_payload(self, query: str, texts: list[str]) -> dict[str, Any]:
        return {"model": self._model_id, "query": query, "documents": texts}

    def _parse(
        self, body: dict[str, Any], documents: list[RerankDocument]
    ) -> list[RerankScore]:
        results = body.get("results")
        if not isinstance(results, list):
            raise RerankerError("unexpected rerank response: missing 'results' array")
        # Map provider index -> score, then align back to our documents so the
        # caller always gets a score per input document in input order.
        by_index: dict[int, float] = {}
        for item in results:
            if not isinstance(item, dict):
                raise RerankerError("unexpected rerank response: result not an object")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if not isinstance(index, int) or not isinstance(score, (int, float)):
                raise RerankerError("unexpected rerank response: bad index/score")
            by_index[index] = float(score)
        scores: list[RerankScore] = []
        for position, doc in enumerate(documents):
            scores.append(
                RerankScore(record_id=doc.record_id, score=by_index.get(position, 0.0))
            )
        return scores

    async def _request(self, query: str, texts: list[str]) -> dict[str, Any]:
        url = f"{self._base_url}{self._path}"
        payload = self._build_payload(query, texts)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_error: RerankerError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._post(url, headers, payload)
            except RerankerError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    continue
                raise
        raise last_error or RerankerError("rerank request failed")

    async def _post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = await self._client.post(
                    url, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise RerankerError("rerank request timed out") from exc
        except httpx.HTTPError as exc:
            raise RerankerError(f"rerank request failed: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise RerankerError(f"rerank provider returned status {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RerankerError("rerank provider returned non-JSON body") from exc
        if not isinstance(data, dict):
            raise RerankerError("rerank provider returned unexpected JSON type")
        return data
