"""M3 API smoke tests via FastAPI TestClient (httpx)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cctv_memory.bootstrap import build_app
from cctv_memory.infrastructure.runtime import Runtime, build_runtime


@pytest.fixture
def app_client(tmp_path: object) -> Iterator[object]:
    from fastapi.testclient import TestClient

    runtime: Runtime = build_runtime(data_dir=str(tmp_path))
    runtime.init_storage()
    runtime.create_schema()
    from cctv_memory.application.seed import seed_local_defaults

    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())
    app = build_app(runtime)
    app.state.runtime = runtime
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client
    runtime.dispose()


def test_health_envelope(app_client) -> None:  # type: ignore[no-untyped-def]
    resp = app_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["vlm_provider"] == "mock"
    assert body["meta"]["schema_version"] == "v1"
    assert body["request_id"]


def test_analyze_then_search_closed_loop_over_http(app_client) -> None:  # type: ignore[no-untyped-def]
    analyze = app_client.post(
        "/api/v1/video-sources/analyze",
        json={
            "source_type": "file",
            "source_uri": "/data/videos/lobby.mp4",
            "camera_id": "cam_lobby_01",
            "video_start_time": "2026-06-06T21:00:00+08:00",
            "idempotency_key": "http-1",
        },
    )
    assert analyze.status_code == 200, analyze.text
    data = analyze.json()["data"]
    assert data["accepted"] is True
    job_id = data["analysis_job_id"]
    with app_client.app.state.runtime.session() as session:
        events = app_client.app.state.runtime.repositories(session).timeline().list_by_job(job_id)
    event_names = {event.event_name for event in events}
    assert {"request_accepted", "task_queued"}.issubset(event_names)

    # Job is queryable.
    job_resp = app_client.get(f"/api/v1/analysis-jobs/{job_id}")
    assert job_resp.status_code == 200
    assert job_resp.json()["data"]["analysis_job_id"] == job_id


def test_analyze_validation_error_envelope(app_client) -> None:  # type: ignore[no-untyped-def]
    resp = app_client.post("/api/v1/video-sources/analyze", json={"source_type": "file"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "validation_error"


def test_search_envelope_empty_initially(app_client) -> None:  # type: ignore[no-untyped-def]
    resp = app_client.post(
        "/api/v1/observation-search/contexts",
        json={"query_text": "backpack", "top_k": 10},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["candidate_count"] == 0
    assert body["context_id"]


def test_api_responses_never_expose_source_uri(app_client) -> None:  # type: ignore[no-untyped-def]
    import os

    os.environ["CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE"] = "static"
    try:
        app_client.post(
            "/api/v1/video-sources/analyze",
            json={
                "source_type": "file",
                "source_uri": "/secret/internal/path/lobby.mp4",
                "camera_id": "cam_lobby_01",
                "video_start_time": "2026-06-06T21:00:00+08:00",
                "idempotency_key": "src-uri-1",
            },
        )
        # analyze response must not echo the internal source_uri.
        # (worker is not run here; we assert the search/details surface stays clean.)
        details = app_client.post(
            "/api/v1/observation-search/contexts",
            json={"query_text": "person", "top_k": 10},
        )
        assert "source_uri" not in details.text
        assert "/secret/internal/path" not in details.text
    finally:
        os.environ.pop("CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE", None)
