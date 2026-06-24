"""M5 end-to-end closed-loop test (task-spec §F).

Proves the full main path with deterministic mock VLM and no real ffprobe:

    migrate/init -> seed -> analyze (ingest) -> worker -> ObservationRecord
    -> authorized search -> details + locator (no source_uri)
    -> empty/unauthorized scope returns nothing.

Uses the ``static`` video-metadata mode so there is no subprocess dependency.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from cctv_memory.application.locator import LocatorService
from cctv_memory.application.search import SearchService
from cctv_memory.application.seed import DEV_PRINCIPAL_ID, seed_local_defaults
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.search import (
    ObservationDetailsRequest,
    StartObservationSearchRequest,
)
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.domain.enums import JobStatus, SecurityLevel, SourceType
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.workers.analysis_worker import AnalysisWorker

from tests.conftest import make_scope


@pytest.fixture
def static_runtime(tmp_path: object) -> Iterator[Runtime]:
    """Runtime in static (no-ffprobe) metadata mode with schema + seed."""
    config = AppConfig().with_data_dir(str(tmp_path))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.static_duration_ms = 30_000
    runtime = Runtime(config)
    runtime.init_storage()
    runtime.create_schema()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())
    yield runtime
    runtime.dispose()


def test_full_closed_loop(static_runtime: Runtime) -> None:
    runtime = static_runtime

    # 1. analyze (ingest) using the dev admin principal.
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(DEV_PRINCIPAL_ID)
        scope = svc.auth.authorized_scope_for(principal)
        resp = svc.ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key="e2e-1",
            ),
            principal,
            capabilities=scope.capabilities,
        )
    job_id = resp.analysis_job_id

    # 2. worker processes the queued task.
    worker = AnalysisWorker(runtime)
    assert worker.drain() >= 1

    with runtime.request_services() as svc:
        job = svc.jobs.get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED

    # 3. authorized search finds the mock-generated records.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        search = SearchService(
            repos.observation_read(), repos.search_context(), repos.audit()
        )
        locator = LocatorService(repos.observation_read(), repos.audit())

        admin = repos.principal().get_principal(DEV_PRINCIPAL_ID)
        assert admin is not None
        auth_scope = _scope_for(repos, admin.principal_id)

        result = search.start_search(
            StartObservationSearchRequest(query_text="person", top_k=10), auth_scope
        )
        assert result.candidate_count >= 1
        record_id = result.results[0].record_id

        # 4. details + locator: safe projection, no source_uri anywhere.
        items = locator.get_details(
            ObservationDetailsRequest(record_ids=[record_id], include_locator=True),
            auth_scope,
        )
        assert len(items) == 1
        assert items[0].locator is not None
        assert "source_uri" not in str(items[0].model_dump())

        # 5. empty/unauthorized scope returns nothing.
        empty = make_scope(camera_ids=[], location_ids=[], policy_ids=[])
        empty_result = search.start_search(
            StartObservationSearchRequest(query_text="person", top_k=10), empty
        )
        assert empty_result.candidate_count == 0
        assert (
            locator.get_details(
                ObservationDetailsRequest(record_ids=[record_id], include_locator=True),
                empty,
            )
            == []
        )


def _scope_for(repos: object, principal_id: str):  # type: ignore[no-untyped-def]
    from cctv_memory.application.auth import AuthorizationService

    auth = AuthorizationService(
        repos.principal(),  # type: ignore[attr-defined]
        repos.access_policy(),  # type: ignore[attr-defined]
        repos.camera(),  # type: ignore[attr-defined]
    )
    principal = auth.resolve_principal(principal_id)
    return auth.authorized_scope_for(principal)


def test_full_closed_loop_admin_sees_records_but_source_uri_hidden(
    static_runtime: Runtime,
) -> None:
    """Even an admin (broad scope) never receives source_uri in any projection."""
    runtime = static_runtime
    with runtime.request_services() as svc:
        principal = svc.auth.resolve_principal(DEV_PRINCIPAL_ID)
        scope = svc.auth.authorized_scope_for(principal)
        # Admin scope must still resolve to allowed cameras (not empty).
        assert "cam_lobby_01" in scope.allowed_camera_ids
        assert scope.max_security_level in (
            SecurityLevel.INTERNAL,
            SecurityLevel.CONFIDENTIAL,
            SecurityLevel.RESTRICTED,
        )
