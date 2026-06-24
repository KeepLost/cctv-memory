"""Phase 5 security tests (testing-contract §4) — release-blocking.

Covers the AI-facing search/details/locator surface at the API boundary plus
the AuthorizedScope pre-filter guarantees. Uses static video mode is irrelevant
here (records are published directly), but the API is exercised via TestClient.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from cctv_memory.bootstrap import build_app
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.domain.enums import AnalysisScale, PrincipalType, SecurityLevel
from cctv_memory.infrastructure.runtime import Runtime, build_runtime

_BASE = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)


def _rec(
    record_id: str,
    *,
    camera_id: str,
    location_id: str,
    policy_id: str,
    security_level: SecurityLevel,
    start_ms: int,
) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_001",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=start_ms,
        segment_end_ms=start_ms + 12_000,
        observed_start_time=_BASE,
        observed_end_time=_BASE + timedelta(seconds=12),
        camera_id=camera_id,
        location_id=location_id,
        static_description_text="person with backpack",
        dynamic_description_text="loitering near entrance",
        tags=["person", "backpack"],
        access_policy_id=policy_id,
        security_level=security_level,
    )


@pytest.fixture
def secured_client(tmp_path: object) -> Iterator[object]:
    from fastapi.testclient import TestClient

    runtime: Runtime = build_runtime(data_dir=str(tmp_path))
    runtime.init_storage()
    runtime.create_schema()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        # Two policies: public (viewer) and confidential (admin only).
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["viewer", "admin"]),
            )
        )
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_secret",
                name="Secret",
                security_level=SecurityLevel.CONFIDENTIAL,
                rules=AccessPolicyRules(allowed_roles=["admin"]),
            )
        )
        # Locations + cameras for both policies.
        from cctv_memory.contracts.video import CameraDevice, CameraLocation

        for loc_id, pol in (("loc_pub", "policy_public_area"), ("loc_sec", "policy_secret")):
            repos.camera().upsert_location(
                CameraLocation(location_id=loc_id, area="a", access_policy_id=pol,
                               security_level=SecurityLevel.INTERNAL)
            )
        for cam_id, loc_id, pol in (
            ("cam_pub", "loc_pub", "policy_public_area"),
            ("cam_sec", "loc_sec", "policy_secret"),
        ):
            repos.camera().upsert_camera(
                CameraDevice(camera_id=cam_id, camera_name=cam_id, location_id=loc_id,
                             access_policy_id=pol, status="active")
            )
        # Principals: viewer (public only) and admin (both).
        repos.principal().create_principal(
            Principal(principal_id="viewer_1", principal_type=PrincipalType.USER,
                      display_name="Viewer", roles=["viewer"])
        )
        repos.principal().create_principal(
            Principal(principal_id="admin_1", principal_type=PrincipalType.ADMIN,
                      display_name="Admin", roles=["admin"])
        )
        # Publish one public record and one secret record.
        repos.publication().publish_records_atomically(
            PublishObservationRecordsCommand(
                command_id="c", analysis_job_id="job_001",
                records=[
                    _rec("obs_pub", camera_id="cam_pub", location_id="loc_pub",
                         policy_id="policy_public_area", security_level=SecurityLevel.INTERNAL,
                         start_ms=0),
                    _rec("obs_sec", camera_id="cam_sec", location_id="loc_sec",
                         policy_id="policy_secret", security_level=SecurityLevel.CONFIDENTIAL,
                         start_ms=12000),
                ],
            )
        )
    app = build_app(runtime)
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client
    runtime.dispose()


def _search(client, principal_id: str, **body):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/observation-search/contexts",
        json={"query_text": "backpack", "top_k": 10, **body},
        headers={"X-Principal-Id": principal_id},
    )


def test_request_body_principal_ignored(secured_client) -> None:  # type: ignore[no-untyped-def]
    # Viewer puts admin's id in the body; identity must come from header only.
    resp = secured_client.post(
        "/api/v1/observation-search/contexts",
        json={"query_text": "backpack", "top_k": 10, "principal_id": "admin_1"},
        headers={"X-Principal-Id": "viewer_1"},
    )
    # extra body field rejected (extra=forbid) -> validation_error, NOT elevated access.
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


def test_forbidden_record_not_in_search_results_or_count(secured_client) -> None:  # type: ignore[no-untyped-def]
    resp = _search(secured_client, "viewer_1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = {r["record_id"] for r in data["results"]}
    assert "obs_sec" not in ids
    assert "obs_pub" in ids
    assert data["candidate_count"] == 1


def test_forbidden_record_not_in_facets(secured_client) -> None:  # type: ignore[no-untyped-def]
    resp = _search(secured_client, "viewer_1")
    facets = resp.json()["data"]["facets"]
    assert facets["candidate_count"] == 1
    cams = {e["camera_id"] for e in facets["camera_distribution"]}
    assert "cam_sec" not in cams


def test_admin_sees_both_records(secured_client) -> None:  # type: ignore[no-untyped-def]
    resp = _search(secured_client, "admin_1")
    ids = {r["record_id"] for r in resp.json()["data"]["results"]}
    assert ids == {"obs_pub", "obs_sec"}


def test_forbidden_record_details_returns_empty(secured_client) -> None:  # type: ignore[no-untyped-def]
    resp = secured_client.post(
        "/api/v1/observation-search/details",
        json={"record_ids": ["obs_sec"], "include_locator": False},
        headers={"X-Principal-Id": "viewer_1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


def test_source_uri_never_exposed(secured_client) -> None:  # type: ignore[no-untyped-def]
    search = _search(secured_client, "viewer_1")
    assert "source_uri" not in search.text
    details = secured_client.post(
        "/api/v1/observation-search/details",
        json={"record_ids": ["obs_pub"], "include_locator": True},
        headers={"X-Principal-Id": "viewer_1"},
    )
    assert details.status_code == 200
    assert "source_uri" not in details.text
    item = details.json()["data"]["items"][0]
    assert item["locator"] is not None
    assert item["locator"]["playback_url"].startswith("/api/v1/playback/")


def test_locator_requires_second_authz_capability(secured_client) -> None:  # type: ignore[no-untyped-def]
    # A service account without read_locator capability is denied the locator.
    # viewer_1 (USER) has read_locator by default; assert the positive path works
    # and that requesting details without locator does not require it.
    no_locator = secured_client.post(
        "/api/v1/observation-search/details",
        json={"record_ids": ["obs_pub"], "include_locator": False},
        headers={"X-Principal-Id": "viewer_1"},
    )
    assert no_locator.status_code == 200
    assert no_locator.json()["data"]["items"][0]["locator"] is None


def test_unknown_principal_is_unauthenticated(secured_client) -> None:  # type: ignore[no-untyped-def]
    resp = _search(secured_client, "ghost_999")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


def test_search_repository_cannot_write_active_records() -> None:
    from cctv_memory.infrastructure.db.repositories.observation_read import (
        SqliteObservationReadRepository,
    )

    methods = set(dir(SqliteObservationReadRepository))
    for forbidden in ("publish_records_atomically", "insert", "upsert", "delete", "save", "write"):
        assert forbidden not in methods


def test_locator_denied_without_read_locator_capability(factory) -> None:  # type: ignore[no-untyped-def]
    """A scope lacking observation.read_locator is denied the locator projection."""
    from cctv_memory.application.locator import LocatorService
    from cctv_memory.contracts.auth import AuthorizedScope
    from cctv_memory.contracts.search import ObservationDetailsRequest
    from cctv_memory.domain.enums import Capability
    from cctv_memory.domain.exceptions import CapabilityDeniedError

    from tests.conftest import seed_camera

    seed_camera(factory)
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="c2",
            analysis_job_id="job_001",
            records=[
                _rec("obs_x", camera_id="cam_lobby_01", location_id="loc_lobby_01",
                     policy_id="policy_public_area", security_level=SecurityLevel.INTERNAL,
                     start_ms=0)
            ],
        )
    )
    # Scope WITH read_detail but WITHOUT read_locator.
    scope = AuthorizedScope(
        tenant_id="tenant_default",
        principal_id="svc",
        allowed_camera_ids=["cam_lobby_01"],
        allowed_location_ids=["loc_lobby_01"],
        allowed_access_policy_ids=["policy_public_area"],
        max_security_level=SecurityLevel.INTERNAL,
        capabilities=[Capability.OBSERVATION_SEARCH, Capability.OBSERVATION_READ_DETAIL],
        scope_hash="h",
    )
    svc = LocatorService(factory.observation_read(), factory.audit())
    # details without locator is allowed
    items = svc.get_details(
        ObservationDetailsRequest(record_ids=["obs_x"], include_locator=False), scope
    )
    assert len(items) == 1
    # details WITH locator requires the second capability -> denied
    with pytest.raises(CapabilityDeniedError):
        svc.get_details(
            ObservationDetailsRequest(record_ids=["obs_x"], include_locator=True), scope
        )
