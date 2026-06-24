from __future__ import annotations

import pytest
from cctv_memory.config.settings import AppConfig
from cctv_memory.infrastructure.runtime import Runtime


def test_database_config_defaults_to_sqlite() -> None:
    config = AppConfig()

    assert config.database.backend == "sqlite"
    assert config.database.sqlite_path.endswith("cctv_memory.sqlite3")


def test_postgres_config_uses_env_name_not_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCTV_MEMORY_POSTGRES_DSN", "postgresql+psycopg://user:secret@db/app")

    config = AppConfig(database={"backend": "postgres"})

    assert config.database.backend == "postgres"
    assert config.database.postgres_dsn_env == "CCTV_MEMORY_POSTGRES_DSN"
    assert "secret" not in config.database.model_dump_json()


def test_postgres_runtime_requires_dsn_env_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CCTV_MEMORY_POSTGRES_DSN", raising=False)
    config = AppConfig(database={"backend": "postgres"})

    with pytest.raises(RuntimeError, match="CCTV_MEMORY_POSTGRES_DSN") as exc_info:
        Runtime(config)

    assert "postgresql" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
