"""Infrastructure adapter package: indexing (embedding adapters + vector index).

Provides the C1 embedding infrastructure: a deterministic offline mock embedder
(CI default), an async OpenAI-compatible / SiliconFlow embedder, and the request
format adapters that hide provider payload shapes from upper layers.
"""

from cctv_memory.infrastructure.indexing.formats import (
    EmbeddingRequestFormat,
    OpenAICompatibleEmbeddingFormat,
)
from cctv_memory.infrastructure.indexing.mock_embedder import MockEmbedder
from cctv_memory.infrastructure.indexing.mock_reranker import MockReranker
from cctv_memory.infrastructure.indexing.siliconflow_embedder import SiliconFlowEmbedder
from cctv_memory.infrastructure.indexing.siliconflow_reranker import SiliconFlowReranker

__all__ = [
    "EmbeddingRequestFormat",
    "OpenAICompatibleEmbeddingFormat",
    "MockEmbedder",
    "SiliconFlowEmbedder",
    "MockReranker",
    "SiliconFlowReranker",
]
