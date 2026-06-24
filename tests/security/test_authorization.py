"""Security tests: AuthorizedScope fail-closed + write-path separation.

testing-contract §4, authorization-policy-contract §4.1,
ARCHITECTURE_CONSTITUTION §5/§6.
"""

from __future__ import annotations

from datetime import datetime

from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.domain.enums import AnalysisScale, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.infrastructure.db.repositories.observation_read import (
    SqliteObservationReadRepository,
)
from cctv_memory.repositories.observation import (
    ObservationRecordPublicationRepository,
    ObservationRecordReadRepository,
)

from tests.conftest import make_scope, seed_camera

_T0 = datetime.fromisoformat("2026-06-06T21:00:00+08:00")
_T1 = datetime.fromisoformat("2026-06-06T21:00:15+08:00")


def _record(
    record_id: str,
    *,
    camera_id: str = "cam_lobby_01",
    location_id: str = "loc_lobby_01",
    policy_id: str = "policy_public_area",
    security_level: SecurityLevel = SecurityLevel.INTERNAL,
    segment_start_ms: int = 0,
    segment_end_ms: int = 15000,
) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_001",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=segment_start_ms,
        segment_end_ms=segment_end_ms,
        observed_start_time=_T0,
        observed_end_time=_T1,
        camera_id=camera_id,
        location_id=location_id,
        static_description_text="static",
        dynamic_description_text="dynamic",
        tags=["person"],
        access_policy_id=policy_id,
        security_level=security_level,
    )


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="pub_1",
            analysis_job_id="job_001",
            records=list(records),
        )
    )


def test_authorized_read_returns_authorized_record(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_ok"))
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    got = factory.observation_read().get_authorized_active_by_id("obs_ok", scope)
    assert got is not None and got.record_id == "obs_ok"


def test_authorized_read_hides_forbidden_record(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    # Record belongs to a restricted policy/camera the principal cannot see.
    _publish(
        factory,
        _record(
            "obs_secret",
            camera_id="cam_secret",
            location_id="loc_secret",
            policy_id="policy_confidential",
            security_level=SecurityLevel.CONFIDENTIAL,
        ),
    )
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )
    assert factory.observation_read().get_authorized_active_by_id("obs_secret", scope) is None
    assert factory.observation_read().count_authorized(scope) == 0


def test_empty_allowed_lists_return_no_records(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(factory, _record("obs_ok"))
    # Empty allowed_* arrays must mean NO permission (auth §4.1), not unlimited.
    empty_scope = make_scope(camera_ids=[], location_ids=[], policy_ids=[])
    assert factory.observation_read().count_authorized(empty_scope) == 0
    assert factory.observation_read().get_authorized_active_by_id("obs_ok", empty_scope) is None


def test_security_level_cap_excludes_higher_levels(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _record(
            "obs_conf",
            policy_id="policy_public_area",
            security_level=SecurityLevel.CONFIDENTIAL,
        ),
    )
    scope = make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
        max_level=SecurityLevel.INTERNAL,
    )
    assert factory.observation_read().get_authorized_active_by_id("obs_conf", scope) is None


def test_search_read_repository_has_no_write_method() -> None:
    # write_path_separation: the read repository exposes no active-write method.
    read_port_methods = set(dir(SqliteObservationReadRepository))
    for forbidden in (
        "publish_records_atomically",
        "insert",
        "upsert",
        "delete",
        "save",
        "write",
    ):
        assert forbidden not in read_port_methods


def test_read_and_publication_ports_are_distinct() -> None:
    # The read port type and publication port type are separate protocols.
    assert ObservationRecordReadRepository is not ObservationRecordPublicationRepository
    assert hasattr(ObservationRecordPublicationRepository, "publish_records_atomically")
    assert not hasattr(ObservationRecordReadRepository, "publish_records_atomically")
