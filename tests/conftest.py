"""Shared pytest fixtures for the data layer.

Two database setups:
- ``session`` / ``factory``: a fast temp-file SQLite DB built from ORM metadata
  (plus the FTS5/vector placeholders) for contract/security tests;
- the Alembic migration is exercised separately in tests/integration.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.video import CameraDevice, CameraLocation
from cctv_memory.domain.enums import Capability, SecurityLevel
from cctv_memory.infrastructure.db.engine import (
    create_session_factory,
    create_sqlite_engine,
)
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.infrastructure.db.models import Base
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

_FTS_DDL = (
    "CREATE VIRTUAL TABLE observation_static_fts USING fts5(record_id UNINDEXED, text)",
    "CREATE VIRTUAL TABLE observation_dynamic_fts USING fts5(record_id UNINDEXED, text)",
    "CREATE VIRTUAL TABLE observation_tags_fts USING fts5(record_id UNINDEXED, text)",
)


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    db_path = f"{tmp_path}/test_cctv.sqlite3"  # type: ignore[str-bytes-safe]
    eng = create_sqlite_engine(db_path)
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        for ddl in _FTS_DDL:
            conn.execute(text(ddl))
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    sess = factory()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()


@pytest.fixture
def factory(session: Session) -> SqliteRepositoryFactory:
    return SqliteRepositoryFactory(session)


@pytest.fixture
def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture
def runtime_factory(tmp_path: object):  # type: ignore[no-untyped-def]
    """Return a builder for a Runtime backed by a temp data dir.

    The returned Runtime creates the schema via metadata (mirrors the initial
    migration) so the closed-loop pipeline can run without invoking Alembic.
    """
    from cctv_memory.infrastructure.runtime import build_runtime

    created = []

    def _build():  # type: ignore[no-untyped-def]
        rt = build_runtime(data_dir=str(tmp_path))
        rt.init_storage()
        rt.create_schema()
        created.append(rt)
        return rt

    yield _build
    for rt in created:
        rt.dispose()


def iso_in(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def dt_in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def make_scope(
    *,
    camera_ids: list[str],
    location_ids: list[str],
    policy_ids: list[str],
    max_level: SecurityLevel = SecurityLevel.INTERNAL,
    tenant_id: str = "tenant_default",
) -> AuthorizedScope:
    return AuthorizedScope(
        tenant_id=tenant_id,
        principal_id="user_test",
        allowed_camera_ids=camera_ids,
        allowed_location_ids=location_ids,
        allowed_access_policy_ids=policy_ids,
        max_security_level=max_level,
        capabilities=[
            Capability.OBSERVATION_SEARCH,
            Capability.OBSERVATION_READ_DETAIL,
            Capability.OBSERVATION_READ_LOCATOR,
        ],
        scope_hash="scope_test",
    )


def seed_camera(factory: SqliteRepositoryFactory) -> tuple[CameraLocation, CameraDevice]:
    location = CameraLocation(
        location_id="loc_lobby_01",
        area="lobby",
        access_policy_id="policy_public_area",
        security_level=SecurityLevel.INTERNAL,
    )
    camera = CameraDevice(
        camera_id="cam_lobby_01",
        camera_name="Lobby Cam",
        location_id="loc_lobby_01",
        access_policy_id="policy_public_area",
        status="active",
    )
    factory.camera().upsert_location(location)
    factory.camera().upsert_camera(camera)
    return location, camera
