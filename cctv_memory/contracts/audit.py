"""Audit event contract (schema-contracts §10).

Audit events must not record plaintext tokens or secrets
(authorization-policy-contract §10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from cctv_memory.contracts.common import ContractModel


class AuditEvent(ContractModel):
    """Audit event (schema-contracts §10.1)."""

    audit_event_id: str
    event_type: str
    request_id: str | None = None
    principal_id: str | None = None
    session_id: str | None = None
    context_id: str | None = None
    resource_scope_hash: str | None = None
    record_ids: list[str] = Field(default_factory=list)
    video_id: str | None = None
    camera_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
