"""Task queue contract (table-schema-spec §7, database-capability-contract §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from cctv_memory.contracts.common import SCHEMA_VERSION, ContractModel


class Task(ContractModel):
    """A queued analysis task (table-schema-spec §7).

    Timestamp fields use the canonical domain type ``datetime`` (not the SQLite
    storage shape). Adapters convert at the boundary: SQLite <-> ISO text,
    PostgreSQL <-> TIMESTAMPTZ (database-adapter-contract §4.0).
    """

    task_id: str
    schema_version: str = SCHEMA_VERSION
    task_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "queued"
    priority: int = 0
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    next_run_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
