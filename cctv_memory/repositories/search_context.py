"""SearchContextRepository port (repository-port-contract §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from cctv_memory.contracts.search import (
    SearchCandidate,
    SearchContext,
    SearchRevision,
)
from cctv_memory.repositories.types import Page


@runtime_checkable
class SearchContextRepository(Protocol):
    """SearchContext / Revision / Candidate persistence port.

    Revisions are immutable. The context is bound to
    principal/session/authorized_scope_hash; the repository stores and verifies
    these binding fields but does not itself decide permissions.
    """

    def create_context(self, context: SearchContext) -> SearchContext: ...

    def get_context(self, context_id: str) -> SearchContext | None: ...

    def close_context(self, context_id: str) -> None: ...

    def expire_contexts(self, now: datetime) -> int: ...

    def create_revision(
        self, revision: SearchRevision, candidates: list[SearchCandidate]
    ) -> SearchRevision: ...

    def get_revision(self, revision_id: str) -> SearchRevision | None: ...

    def count_revisions(self, context_id: str) -> int: ...

    def list_candidates(
        self, revision_id: str, cursor: str | None = None, limit: int = 50
    ) -> Page[SearchCandidate]: ...

    def replace_default_revision(self, context_id: str, revision_id: str) -> None: ...
