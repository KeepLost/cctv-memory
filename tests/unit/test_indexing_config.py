"""IndexingSection config + embedder selection tests (task §8).

Checks default values (mock/offline by default, bge-m3/1024), env override of the
nested ``indexing`` section, and that the runtime builds a mock embedder by
default and refuses ``real`` without the API key (without ever printing it).
"""

from __future__ import annotations

import pytest
from cctv_memory.config.settings import AppConfig, IndexingSection
from cctv_memory.infrastructure.indexing.mock_embedder import MockEmbedder
from cctv_memory.infrastructure.indexing.siliconflow_embedder import SiliconFlowEmbedder
from cctv_memory.infrastructure.runtime import build_runtime


def test_indexing_section_defaults() -> None:
    cfg = IndexingSection()
    assert cfg.provider == "mock"
    assert cfg.enabled is False
    assert cfg.embedding_model == "BAAI/bge-m3"
    assert cfg.embedding_dimensions == 1024
    assert cfg.default_base_url == "http://nginx:8081/api/siliconflow"
    assert cfg.embeddings_path == "/embeddings"
    assert cfg.encoding_format == "float"
    # Only env var NAMES are stored, never secret values.
    assert cfg.api_key_env == "CCTV_MEMORY_EMBEDDING_API_KEY"
    assert cfg.base_url_env == "CCTV_MEMORY_EMBEDDING_BASE_URL"


def test_appconfig_includes_indexing_section() -> None:
    cfg = AppConfig()
    assert cfg.indexing.provider == "mock"
    assert cfg.indexing.embedding_dimensions == 1024


def test_indexing_section_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCTV_MEMORY_INDEXING__PROVIDER", "real")
    monkeypatch.setenv("CCTV_MEMORY_INDEXING__EMBEDDING_DIMENSIONS", "768")
    cfg = AppConfig()
    assert cfg.indexing.provider == "real"
    assert cfg.indexing.embedding_dimensions == 768


def test_runtime_builds_mock_embedder_by_default(tmp_path: object) -> None:
    runtime = build_runtime(data_dir=str(tmp_path))
    try:
        embedder = runtime.build_embedder()
        assert isinstance(embedder, MockEmbedder)
        assert embedder.dimension == 1024
    finally:
        runtime.dispose()


def test_runtime_real_embedder_requires_api_key(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig().with_data_dir(str(tmp_path))
    config.indexing.provider = "real"
    monkeypatch.delenv(config.indexing.api_key_env, raising=False)
    from cctv_memory.infrastructure.runtime import Runtime

    runtime = Runtime(config)
    try:
        with pytest.raises(RuntimeError) as exc:
            runtime.build_embedder()
        # The error names the env var, never a secret value.
        assert config.indexing.api_key_env in str(exc.value)
    finally:
        runtime.dispose()


def test_runtime_real_embedder_when_key_present(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig().with_data_dir(str(tmp_path))
    config.indexing.provider = "real"
    monkeypatch.setenv(config.indexing.api_key_env, "test-key")
    from cctv_memory.infrastructure.runtime import Runtime

    runtime = Runtime(config)
    try:
        embedder = runtime.build_embedder()
        assert isinstance(embedder, SiliconFlowEmbedder)
        assert embedder.dimension == 1024
    finally:
        runtime.dispose()
