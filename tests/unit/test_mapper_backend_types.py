"""Mappers must accept both SQLite (TEXT/JSON-string) and PostgreSQL
(JSONB->dict/list, TIMESTAMPTZ->datetime) native row shapes.

SQLite stores JSON columns as serialized text, while the PostgreSQL backend
uses JSONB, so the psycopg driver returns already-deserialized ``dict``/``list``
objects. A mapper that assumes a JSON *string* (e.g. ``model_validate_json``)
breaks on the PostgreSQL backend and surfaces as a spurious HTTP 400.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from cctv_memory.infrastructure.db import mappers


def _policy_row(rules_value: object, *, ts: object) -> SimpleNamespace:
    return SimpleNamespace(
        access_policy_id="pol_1",
        tenant_id="tenant_default",
        name="default",
        security_level="internal",
        rules_json=rules_value,
        created_at=ts,
        updated_at=ts,
    )


def test_policy_to_dto_accepts_sqlite_json_string() -> None:
    row = _policy_row(
        '{"allowed_roles": ["analyst"], "allowed_groups": [],'
        ' "allowed_principals": [], "denied_principals": []}',
        ts="2026-06-23T00:00:00+00:00",
    )

    dto = mappers.policy_to_dto(row)  # type: ignore[arg-type]

    assert dto.rules.allowed_roles == ["analyst"]


def test_policy_to_dto_accepts_postgres_jsonb_dict() -> None:
    # PostgreSQL JSONB -> psycopg returns a dict, and TIMESTAMPTZ -> datetime.
    row = _policy_row(
        {
            "allowed_roles": ["analyst"],
            "allowed_groups": [],
            "allowed_principals": [],
            "denied_principals": [],
        },
        ts=datetime.fromisoformat("2026-06-23T00:00:00+00:00"),
    )

    dto = mappers.policy_to_dto(row)  # type: ignore[arg-type]

    assert dto.rules.allowed_roles == ["analyst"]
    assert dto.created_at is not None


def _task_row(*, payload: object, next_run_at: object, ts: object) -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task_1",
        schema_version="1.0.0",
        task_type="analyze_video",
        payload_json=payload,
        status="queued",
        priority=0,
        retry_count=0,
        max_retries=3,
        next_run_at=next_run_at,
        lease_owner=None,
        lease_expires_at=ts,
        created_at=ts,
        updated_at=ts,
        error_code=None,
        error_message=None,
    )


def test_task_to_dto_accepts_sqlite_string_row() -> None:
    row = _task_row(
        payload='{"analysis_job_id": "job_1"}',
        next_run_at="2026-06-23T00:00:00+00:00",
        ts="2026-06-23T00:00:00+00:00",
    )

    dto = mappers.task_to_dto(row)  # type: ignore[arg-type]

    # SQLite stores timestamps as ISO text; the mapper normalizes to the
    # canonical datetime DTO type.
    expected = datetime.fromisoformat("2026-06-23T00:00:00+00:00")
    assert dto.payload == {"analysis_job_id": "job_1"}
    assert dto.next_run_at == expected
    assert dto.created_at == expected


def test_task_to_dto_accepts_postgres_native_row() -> None:
    # PostgreSQL: JSONB payload -> dict, TIMESTAMPTZ columns -> datetime.
    # The Task DTO now uses the canonical datetime type, so the native datetime
    # flows straight through _dt. (Before the type unification the DTO typed
    # these as str, which raised a Pydantic string_type error and crashed the
    # worker claim path so the VLM never ran and nothing was written.)
    ts = datetime.fromisoformat("2026-06-23T00:00:00+00:00")
    row = _task_row(
        payload={"analysis_job_id": "job_1"},
        next_run_at=ts,
        ts=ts,
    )

    dto = mappers.task_to_dto(row)  # type: ignore[arg-type]

    assert dto.payload == {"analysis_job_id": "job_1"}
    assert dto.next_run_at == ts
    assert dto.lease_expires_at == ts
    assert dto.created_at == ts
    assert dto.updated_at == ts


def test_task_to_dto_sqlite_and_postgres_rows_agree() -> None:
    # Parity: the same logical timestamp produces the same canonical datetime
    # whether stored as SQLite ISO text or returned as a PostgreSQL datetime.
    ts = datetime.fromisoformat("2026-06-23T00:00:00+00:00")
    sqlite_dto = mappers.task_to_dto(
        _task_row(payload="{}", next_run_at=ts.isoformat(), ts=ts.isoformat())  # type: ignore[arg-type]
    )
    postgres_dto = mappers.task_to_dto(
        _task_row(payload={}, next_run_at=ts, ts=ts)  # type: ignore[arg-type]
    )
    assert sqlite_dto.next_run_at == postgres_dto.next_run_at == ts
    assert sqlite_dto.created_at == postgres_dto.created_at == ts
