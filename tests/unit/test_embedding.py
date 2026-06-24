"""Unit tests for the C1 embedding infrastructure (task §8).

All tests are network-free and offline:
- the mock embedder (CI default) determinism + dimension;
- the OpenAI-compatible request format payload/parse;
- the async SiliconFlow embedder via a fake httpx transport (request shape,
  1024-dim parse, dimension-mismatch + HTTP/timeout error mapping).

Async adapters are driven with ``asyncio.run`` so no pytest-asyncio plugin /
extra dependency is required.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from cctv_memory.infrastructure.indexing.formats import OpenAICompatibleEmbeddingFormat
from cctv_memory.infrastructure.indexing.mock_embedder import MockEmbedder
from cctv_memory.infrastructure.indexing.siliconflow_embedder import SiliconFlowEmbedder
from cctv_memory.services.embedding import EmbeddingError, EmbeddingPort

# ---- mock embedder ---------------------------------------------------------


def test_mock_embedder_is_an_embedding_port() -> None:
    embedder = MockEmbedder()
    assert isinstance(embedder, EmbeddingPort)
    assert embedder.dimension == 1024
    assert embedder.model_id


def test_mock_embedder_dimension_matches_config() -> None:
    embedder = MockEmbedder(dimension=8, model_id="m")
    vectors = asyncio.run(embedder.embed_texts(["a", "b"]))
    assert len(vectors) == 2
    assert all(len(v) == 8 for v in vectors)


def test_mock_embedder_is_deterministic() -> None:
    embedder = MockEmbedder(dimension=16)
    v1 = asyncio.run(embedder.embed_query("a lobby with a person"))
    v2 = asyncio.run(embedder.embed_query("a lobby with a person"))
    assert v1 == v2


def test_mock_embedder_differs_for_different_text() -> None:
    embedder = MockEmbedder(dimension=16)
    v1 = asyncio.run(embedder.embed_query("alpha"))
    v2 = asyncio.run(embedder.embed_query("beta"))
    assert v1 != v2


def test_mock_embedder_vectors_are_unit_norm() -> None:
    embedder = MockEmbedder(dimension=32)
    v = asyncio.run(embedder.embed_query("normalize me"))
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_mock_embedder_rejects_nonpositive_dimension() -> None:
    with pytest.raises(ValueError):
        MockEmbedder(dimension=0)


# ---- OpenAI-compatible request format --------------------------------------


def test_openai_format_builds_expected_payload() -> None:
    fmt = OpenAICompatibleEmbeddingFormat(model_id="BAAI/bge-m3")
    payload = fmt.build_payload(["hello", "world"])
    assert payload == {
        "model": "BAAI/bge-m3",
        "input": ["hello", "world"],
        "encoding_format": "float",
    }
    assert fmt.path == "/embeddings"


def test_openai_format_parses_and_orders_by_index() -> None:
    fmt = OpenAICompatibleEmbeddingFormat(model_id="m")
    body = {
        "data": [
            {"embedding": [0.2, 0.2], "index": 1},
            {"embedding": [0.1, 0.1], "index": 0},
        ]
    }
    vectors = fmt.parse_response(body, expected_count=2)
    assert vectors == [[0.1, 0.1], [0.2, 0.2]]


def test_openai_format_count_mismatch_raises() -> None:
    fmt = OpenAICompatibleEmbeddingFormat(model_id="m")
    body = {"data": [{"embedding": [0.1], "index": 0}]}
    with pytest.raises(EmbeddingError):
        fmt.parse_response(body, expected_count=2)


def test_openai_format_missing_data_raises() -> None:
    fmt = OpenAICompatibleEmbeddingFormat(model_id="m")
    with pytest.raises(EmbeddingError):
        fmt.parse_response({}, expected_count=1)


# ---- async SiliconFlow embedder via fake transport -------------------------


def _fake_async_client(
    handler: httpx.MockTransport | object,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)  # type: ignore[arg-type]


def _embedding_response(vectors: list[list[float]], *, status: int = 200) -> httpx.MockTransport:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        data = [{"embedding": v, "index": i} for i, v in enumerate(vectors)]
        return httpx.Response(status, json={"data": data})

    transport = httpx.MockTransport(handler)
    transport.captured = captured  # type: ignore[attr-defined]
    return transport


def test_siliconflow_embedder_sends_openai_payload_to_path() -> None:
    transport = _embedding_response([[0.0] * 4, [1.0] * 4])
    client = _fake_async_client(transport)
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="secret-key",
        model_id="BAAI/bge-m3",
        dimension=4,
        client=client,
    )
    vectors = asyncio.run(embedder.embed_texts(["a", "b"]))
    assert len(vectors) == 2
    captured = transport.captured  # type: ignore[attr-defined]
    assert captured["url"] == "http://nginx:8081/api/siliconflow/embeddings"
    assert captured["body"] == {
        "model": "BAAI/bge-m3",
        "input": ["a", "b"],
        "encoding_format": "float",
    }
    # The key is sent only in the Authorization header (never echoed elsewhere).
    assert captured["auth"] == "Bearer secret-key"
    asyncio.run(client.aclose())


def test_siliconflow_embedder_parses_1024_dim() -> None:
    transport = _embedding_response([[0.5] * 1024])
    client = _fake_async_client(transport)
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="k",
        client=client,
    )
    vector = asyncio.run(embedder.embed_query("a lobby"))
    assert len(vector) == 1024
    assert embedder.dimension == 1024
    assert embedder.model_id == "BAAI/bge-m3"
    asyncio.run(client.aclose())


def test_siliconflow_embedder_dimension_mismatch_raises() -> None:
    # Provider returns 3-dim vectors but adapter expects 1024 -> EmbeddingError.
    transport = _embedding_response([[0.1, 0.2, 0.3]])
    client = _fake_async_client(transport)
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="k",
        dimension=1024,
        client=client,
    )
    with pytest.raises(EmbeddingError):
        asyncio.run(embedder.embed_query("x"))
    asyncio.run(client.aclose())


def test_siliconflow_embedder_http_error_maps_to_embedding_error() -> None:
    transport = _embedding_response([[0.0] * 4], status=500)
    client = _fake_async_client(transport)
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="k",
        dimension=4,
        client=client,
    )
    with pytest.raises(EmbeddingError):
        asyncio.run(embedder.embed_texts(["x"]))
    asyncio.run(client.aclose())


def test_siliconflow_embedder_timeout_maps_to_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="k",
        dimension=4,
        client=client,
        max_retries=0,
    )
    with pytest.raises(EmbeddingError):
        asyncio.run(embedder.embed_query("x"))
    asyncio.run(client.aclose())


def test_siliconflow_embedder_does_not_leak_key_in_errors() -> None:
    transport = _embedding_response([[0.0] * 4], status=503)
    client = _fake_async_client(transport)
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow",
        api_key="super-secret",
        dimension=4,
        client=client,
        max_retries=0,
    )
    with pytest.raises(EmbeddingError) as exc:
        asyncio.run(embedder.embed_query("x"))
    assert "super-secret" not in str(exc.value)
    asyncio.run(client.aclose())


def test_siliconflow_embedder_empty_input_returns_empty() -> None:
    transport = _embedding_response([])
    client = _fake_async_client(transport)
    embedder = SiliconFlowEmbedder(
        base_url="http://nginx:8081/api/siliconflow", api_key="k", client=client
    )
    assert asyncio.run(embedder.embed_texts([])) == []
    asyncio.run(client.aclose())
