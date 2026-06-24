"""Config-file loading, VLM selection, and serve startup tests.

Covers the real-usability fixes (task cctv-memory-20260610-real-vlm-http-fix):

1. ``config.yaml`` is actually loaded (configuration-contract §1) and respects
   the documented precedence init > env > yaml > defaults. Before the fix a
   user-edited ``config.yaml`` was silently ignored, so a configured real VLM
   still ran as mock.
2. ``vlm.provider=real`` (via YAML or env) makes the worker select the real VLM
   adapter; ``mock`` selects the mock adapter — without any network call.
3. The ``serve`` command builds a wired FastAPI app and would run it on the
   resolved host/port (verified via an injected runner; no socket is bound,
   per testing-contract §12).
4. ``/health`` reports the ACTIVE vlm/indexing provider rather than a hardcoded
   ``mock`` string.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from cctv_memory.config.settings import CONFIG_FILE_ENV, AppConfig
from cctv_memory.infrastructure.runtime import build_runtime
from cctv_memory.infrastructure.vlm.mock_adapter import MockVlmAnalyzer
from cctv_memory.infrastructure.vlm.real_adapter import RealVlmAnalyzer
from cctv_memory.workers.analysis_worker import _default_vlm
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate each test from ambient CCTV_MEMORY_* env + config files."""
    for key in list(os.environ):
        if key.startswith("CCTV_MEMORY_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---- config.yaml loading + precedence -------------------------------------


def test_yaml_config_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = _write_yaml(
        tmp_path,
        "vlm:\n  provider: real\n  model_id: yaml-model\n"
        "pipeline:\n  video_metadata_mode: static\n",
    )
    monkeypatch.setenv(CONFIG_FILE_ENV, str(cfg_path))
    cfg = AppConfig()
    assert cfg.vlm.provider == "real"
    assert cfg.vlm.model_id == "yaml-model"
    assert cfg.pipeline.video_metadata_mode == "static"


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = _write_yaml(tmp_path, "vlm:\n  provider: real\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(cfg_path))
    monkeypatch.setenv("CCTV_MEMORY_VLM__PROVIDER", "mock")
    cfg = AppConfig()
    # env beats yaml (configuration-contract §1)
    assert cfg.vlm.provider == "mock"


def test_unknown_vlm_worker_yaml_keys_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Misnamed concurrency YAML keys must not be silently ignored.

    Regression for VLM cap diagnosis: an operator can otherwise set plausible
    names like ``vlm.max_concurrency`` and believe a high cap is active while the
    runtime keeps the canonical ``vlm.max_concurrent_requests`` default.
    """
    cfg_path = _write_yaml(
        tmp_path,
        "vlm:\n  max_concurrency: 500\n"
        "worker:\n  max_concurrency: 1000\n  max_unit_concurrency: 1000\n",
    )
    monkeypatch.setenv(CONFIG_FILE_ENV, str(cfg_path))

    with pytest.raises(ValidationError) as exc:
        AppConfig()

    message = str(exc.value)
    assert "max_concurrency" in message
    assert "max_unit_concurrency" in message


def test_unknown_vlm_worker_env_keys_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Misnamed concurrency env vars must fail loudly, not keep defaults."""
    monkeypatch.setenv("CCTV_MEMORY_VLM__MAX_CONCURRENCY", "500")
    monkeypatch.setenv("CCTV_MEMORY_WORKER__MAX_CONCURRENCY", "1000")
    monkeypatch.setenv("CCTV_MEMORY_WORKER__MAX_UNIT_CONCURRENCY", "1000")

    with pytest.raises(ValueError) as exc:
        AppConfig()

    message = str(exc.value)
    assert "CCTV_MEMORY_VLM__MAX_CONCURRENCY" in message
    assert "CCTV_MEMORY_WORKER__MAX_CONCURRENCY" in message
    assert "CCTV_MEMORY_WORKER__MAX_UNIT_CONCURRENCY" in message


def test_canonical_high_concurrency_env_values_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supported env names activate the three intended concurrency knobs."""
    monkeypatch.setenv("CCTV_MEMORY_VLM__MAX_CONCURRENT_REQUESTS", "500")
    monkeypatch.setenv("CCTV_MEMORY_WORKER__MAX_CONCURRENT_JOBS", "1000")
    monkeypatch.setenv("CCTV_MEMORY_WORKER__MAX_UNIT_WORKERS_PER_JOB", "1000")

    cfg = AppConfig()

    assert cfg.vlm.max_concurrent_requests == 500
    assert cfg.worker.max_concurrent_jobs == 1000
    assert cfg.worker.max_unit_workers_per_job == 1000


def test_init_kwarg_overrides_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = _write_yaml(tmp_path, "app:\n  log_level: ERROR\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(cfg_path))
    monkeypatch.setenv("CCTV_MEMORY_APP__LOG_LEVEL", "WARNING")
    from cctv_memory.config.settings import AppSection

    cfg = AppConfig(app=AppSection(log_level="DEBUG"))
    assert cfg.app.log_level == "DEBUG"


def test_no_config_file_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # No CONFIG_FILE env and cwd has no config.yaml in tmp -> defaults.
    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
    cfg = AppConfig()
    assert cfg.vlm.provider == "mock"


def test_missing_explicit_config_file_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "does_not_exist.yaml"))
    # A nonexistent YAML path falls back to env+defaults rather than raising.
    cfg = AppConfig()
    assert cfg.vlm.provider == "mock"


# ---- VLM adapter selection (no network) -----------------------------------


def test_worker_selects_mock_vlm_by_default(tmp_path: Path) -> None:
    runtime = build_runtime(data_dir=str(tmp_path))
    try:
        assert isinstance(_default_vlm(runtime), MockVlmAnalyzer)
    finally:
        runtime.dispose()


def test_worker_selects_real_vlm_when_provider_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = _write_yaml(tmp_path, "vlm:\n  provider: real\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(cfg_path))
    monkeypatch.setenv("LLM_KEY", "test-key-not-printed")
    runtime = build_runtime(data_dir=str(tmp_path))
    try:
        adapter = _default_vlm(runtime)
        assert isinstance(adapter, RealVlmAnalyzer)
    finally:
        runtime.dispose()


def test_real_vlm_without_api_key_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = _write_yaml(tmp_path, "vlm:\n  provider: real\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(cfg_path))
    monkeypatch.delenv("LLM_KEY", raising=False)
    runtime = build_runtime(data_dir=str(tmp_path))
    try:
        with pytest.raises(RuntimeError) as exc:
            _default_vlm(runtime)
        # The error names the env var but never prints a key value.
        assert "LLM_KEY" in str(exc.value)
    finally:
        runtime.dispose()


# ---- serve command (injected runner; no socket) ---------------------------


def test_serve_builds_app_and_resolves_host_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cctv_memory.cli import _build_parser, _cmd_serve

    data_dir = str(tmp_path / "data")
    args = _build_parser().parse_args(
        ["serve", "--data-dir", data_dir, "--host", "0.0.0.0", "--port", "9123", "--no-worker"]
    )
    captured: dict[str, object] = {}

    def runner(app: object, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["routes"] = {r.path for r in app.routes}  # type: ignore[attr-defined]

    rc = _cmd_serve(args, runner=runner)
    assert rc == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123
    assert "/api/v1/health" in captured["routes"]  # type: ignore[operator]


def test_serve_defaults_host_port_from_config(tmp_path: Path) -> None:
    from cctv_memory.cli import _build_parser, _cmd_serve

    data_dir = str(tmp_path / "data2")
    args = _build_parser().parse_args(["serve", "--data-dir", data_dir, "--no-worker"])
    captured: dict[str, object] = {}

    def runner(app: object, *, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    assert _cmd_serve(args, runner=runner) == 0
    # Defaults from ServerSection.
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8080


# ---- /health reports the active provider ----------------------------------


def test_health_reports_real_provider_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cctv_memory.bootstrap import build_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CCTV_MEMORY_VLM__PROVIDER", "real")
    monkeypatch.setenv("LLM_KEY", "test-key")
    runtime = build_runtime(data_dir=str(tmp_path))
    runtime.init_storage()
    runtime.create_schema()
    app = build_app(runtime)
    try:
        with TestClient(app) as client:  # type: ignore[arg-type]
            body = client.get("/api/v1/health").json()
        assert body["data"]["vlm_provider"] == "real"
        assert "indexing_provider" in body["data"]
    finally:
        runtime.dispose()
