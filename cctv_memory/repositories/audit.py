"""AuditRepository port (repository-port-contract §12)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.repositories.types import Page


@runtime_checkable
class AuditRepository(Protocol):
    """Audit event append/list port.

    Audit append must not record plaintext tokens or secrets
    (authorization-policy-contract §10).
    """

    def append_event(self, event: AuditEvent) -> AuditEvent: ...

    def list_events(
        self,
        principal_id: str | None = None,
        event_type: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[AuditEvent]: ...
