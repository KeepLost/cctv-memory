"""C6 API tests: locator/playback (2nd authz), batch-refine, admin backup, exports.

Exercised via FastAPI TestClient (testing-contract §12: no live server). Verifies
locator/playback second authorization, that exports exclude forbidden records and
never include the SQLite file or source_uri, and the new envelope responses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from cctv_memory.bootstrap import build_app
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.contracts.video import (
    CameraDevice,
    CameraLocation,
    SubmitVideoSourceRequest,
)
from cctv_memory.domain.enums import (
    AnalysisScale,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.infrastructure.runtime import Runtime, build_runtime

_BASE = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)


def _rec(record_id: str, *, camera_id: str, location_id: str, policy_id: str,
         security_level: SecurityLevel, start_ms: int) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id, video_id="video_001", analysis_job_id="job_001",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=start_ms, segment_end_ms=start_ms + 12_000,
        observed_start_time=_BASE, observed_end_time=_BASE + timedelta(seconds=12),
        camera_id=camera_id, location_id=location_id,
        static_description_text="person with backpack",
        dynamic_description_text="loitering",
        tags=["person", "backpack"], access_policy_id=policy_id,
        security_level=security_level,
    )


@pytest.fixture
def client(tmp_path: object) -> Iterator[object]:
    from fastapi.testclient import TestClient

    runtime: Runtime = build_runtime(data_dir=str(tmp_path))
    runtime.init_storage()
    runtime.create_schema()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(access_policy_id="policy_public_area", name="Public",
                         security_level=SecurityLevel.INTERNAL,
                         rules=AccessPolicyRules(allowed_roles=["viewer", "admin"]))
        )
        repos.access_policy().upsert_access_policy(
            AccessPolicy(access_policy_id="policy_secret", name="Secret",
                         security_level=SecurityLevel.CONFIDENTIAL,
                         rules=AccessPolicyRules(allowed_roles=["admin"]))
        )
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
        repos.video_source().create_or_get_by_idempotency(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/secret/internal/lobby.mp4",
                camera_id="cam_pub", video_start_time=_BASE, idempotency_key="vk",
            ),
            video_id="video_001",
        )
        repos.principal().create_principal(
            Principal(principal_id="viewer_1", principal_type=PrincipalType.USER,
                      display_name="V", roles=["viewer"])
        )
        repos.principal().create_principal(
            Principal(principal_id="admin_1", principal_type=PrincipalType.ADMIN,
                      display_name="A", roles=["admin"])
        )
        repos.publication().publish_records_atomically(
            PublishObservationRecordsCommand(
                command_id="c", analysis_job_id="job_001",
                records=[
                    _rec("obs_pub", camera_id="cam_pub", location_id="loc_pub",
                         policy_id="policy_public_area",
                         security_level=SecurityLevel.INTERNAL, start_ms=0),
                    _rec("obs_sec", camera_id="cam_sec", location_id="loc_sec",
                         policy_id="policy_secret",
                         security_level=SecurityLevel.CONFIDENTIAL, start_ms=12_000),
                ],
            )
        )
    app = build_app(runtime)
    with TestClient(app) as c:  # type: ignore[arg-type]
        yield c
    runtime.dispose()


def _h(principal_id: str) -> dict[str, str]:
    return {"X-Principal-Id": principal_id}


def test_locators_route_issues_playback_token_no_source_uri(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post(
        "/api/v1/observation-search/locators",
        json={"record_ids": ["obs_pub"]}, headers=_h("viewer_1"),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["playback_url"].startswith("/api/v1/playback/")
    assert "source_uri" not in resp.text
    assert "/secret/internal" not in resp.text


def test_locators_excludes_forbidden_record(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post(
        "/api/v1/observation-search/locators",
        json={"record_ids": ["obs_pub", "obs_sec"]}, headers=_h("viewer_1"),
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    # viewer cannot see obs_sec -> only one locator returned.
    assert len(items) == 1


def test_playback_token_second_authz_and_roundtrip(client) -> None:  # type: ignore[no-untyped-def]
    issued = client.post(
        "/api/v1/observation-search/locators",
        json={"record_ids": ["obs_pub"]}, headers=_h("viewer_1"),
    )
    url = issued.json()["data"]["items"][0]["playback_url"]
    # Same principal can verify the token.
    ok = client.get(url, headers=_h("viewer_1"))
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["record_id"] == "obs_pub"
    assert "source_uri" not in ok.text


def test_playback_token_rejected_for_other_principal(client) -> None:  # type: ignore[no-untyped-def]
    issued = client.post(
        "/api/v1/observation-search/locators",
        json={"record_ids": ["obs_pub"]}, headers=_h("viewer_1"),
    )
    url = issued.json()["data"]["items"][0]["playback_url"]
    # A different principal presenting the token is treated as not_found.
    other = client.get(url, headers=_h("admin_1"))
    assert other.status_code == 404


def test_playback_token_tampered_rejected(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/v1/playback/not-a-valid-token", headers=_h("viewer_1"))
    assert resp.status_code == 404


def test_batch_refine_returns_one_response_per_op(client) -> None:  # type: ignore[no-untyped-def]
    start = client.post(
        "/api/v1/observation-search/contexts",
        json={"query_text": "backpack", "top_k": 10}, headers=_h("viewer_1"),
    )
    rev = start.json()["data"]["revision_id"]
    resp = client.post(
        f"/api/v1/observation-search/contexts/{start.json()['data']['context_id']}/batch-refine",
        json={"refinements": [
            {"base_revision_id": rev, "op": "search_static_text",
             "params": {"query_text": "backpack", "top_k": 10}},
            {"base_revision_id": rev, "op": "search_dynamic_text",
             "params": {"query_text": "loitering", "top_k": 10}},
        ]},
        headers=_h("viewer_1"),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert len(items) == 2
    assert all("revision_id" in it for it in items)


def test_admin_backup_route(client, tmp_path: object) -> None:  # type: ignore[no-untyped-def]
    out = f"{tmp_path}/api_backup.sqlite3"  # type: ignore[str-bytes-safe]
    resp = client.post(
        "/api/v1/admin/backups", json={"out_path": out}, headers=_h("admin_1")
    )
    assert resp.status_code == 200, resp.text
    manifest = resp.json()["data"]
    assert manifest["backup_type"] == "admin_full_backup"


def test_admin_backup_denied_for_viewer(client, tmp_path: object) -> None:  # type: ignore[no-untyped-def]
    out = f"{tmp_path}/api_backup2.sqlite3"  # type: ignore[str-bytes-safe]
    resp = client.post(
        "/api/v1/admin/backups", json={"out_path": out}, headers=_h("viewer_1")
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "capability_denied"


def test_user_export_route_excludes_forbidden_and_db_file(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/api/v1/exports/user", json={}, headers=_h("viewer_1"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    ids = {r["record_id"] for r in data["records"]}
    assert ids == {"obs_pub"}  # forbidden obs_sec excluded
    assert "source_uri" not in resp.text
    assert "/secret/internal" not in resp.text
    assert ".sqlite3" not in resp.text


def test_migration_export_requires_runtime_manage_over_http(client) -> None:  # type: ignore[no-untyped-def]
    denied = client.post("/api/v1/exports/migration", json={}, headers=_h("viewer_1"))
    assert denied.status_code == 403
    allowed = client.post("/api/v1/exports/migration", json={}, headers=_h("admin_1"))
    assert allowed.status_code == 200
    assert allowed.json()["data"]["manifest"]["backup_type"] == "migration_export"
