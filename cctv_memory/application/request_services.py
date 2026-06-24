"""Per-request application services bundle + provider protocol.

The API layer depends on this (application layer) rather than on infrastructure
concretes, preserving the dependency direction (ARCHITECTURE_CONSTITUTION §3;
architecture tests forbid api -> infrastructure). The composition root builds a
concrete provider that yields a ``RequestServices`` bound to a DB session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from cctv_memory.application.analysis_orchestrator import AnalysisOrchestrator
from cctv_memory.application.auth import AuthorizationService
from cctv_memory.application.backup import BackupService
from cctv_memory.application.ingestion import IngestionService
from cctv_memory.application.locator import LocatorService
from cctv_memory.application.playback import PlaybackTokenService
from cctv_memory.application.search import SearchService
from cctv_memory.repositories.analysis import AnalysisJobRepository


@dataclass
class RequestServices:
    """Application services wired to a single request/session."""

    auth: AuthorizationService
    ingestion: IngestionService
    search: SearchService
    locator: LocatorService
    jobs: AnalysisJobRepository
    orchestrator: AnalysisOrchestrator
    playback: PlaybackTokenService
    backup: BackupService


class ServicesProvider(Protocol):
    """Provides a transactional ``RequestServices`` scope.

    Calling the provider yields a context manager that commits on success and
    rolls back on error (the composition root implements this over a DB session).
    """

    def __call__(self) -> AbstractContextManager[RequestServices]: ...


# Re-exported for typing convenience in the API layer.
RequestServicesScope = Iterator[RequestServices]
