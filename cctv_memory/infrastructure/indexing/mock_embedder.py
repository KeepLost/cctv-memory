"""Deterministic offline mock embedder (task §4).

The default ``EmbeddingPort`` implementation for CI/offline use: it makes no
network call and produces stable, deterministic vectors derived from the input
text. The same text always yields the same vector, and different texts almost
always differ, which is enough for index storage/retrieval tests and for keeping
the offline suite green without a real provider.

The vector space is purely synthetic; it is NOT semantically meaningful. Real
semantic search (C2) uses the real provider adapter. Dimension matches the
configured contract (default 1024) so storage/shape behavior mirrors the real
adapter.
"""

from __future__ import annotations

import hashlib
import math


class MockEmbedder:
    """Deterministic, network-free ``EmbeddingPort`` implementation."""

    def __init__(self, *, dimension: int = 1024, model_id: str = "mock-embedder-v1") -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = dimension
        self._model_id = model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float]:
        """Map text -> a deterministic unit vector of length ``dimension``.

        Uses successive SHA-256 hashes of ``<text>#<block>`` to fill the vector
        with reproducible pseudo-random floats in [-1, 1), then L2-normalizes so
        cosine similarity behaves sensibly in tests.
        """
        values: list[float] = []
        block = 0
        while len(values) < self._dimension:
            digest = hashlib.sha256(f"{text}#{block}".encode()).digest()
            for i in range(0, len(digest), 2):
                if len(values) >= self._dimension:
                    break
                pair = int.from_bytes(digest[i : i + 2], "big")
                values.append((pair / 32767.5) - 1.0)
            block += 1
        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:
            return values
        return [v / norm for v in values]
