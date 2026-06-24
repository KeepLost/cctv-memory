"""Publication use case (application/publication.py).

Thin orchestration over the publication repository, which performs the atomic
upsert/archive/summary/audit transaction (ARCHITECTURE_CONSTITUTION §6,
database-capability-contract §9). Only this path writes active ObservationRecord.
"""

from __future__ import annotations

from cctv_memory.contracts.pipeline import (
    PublicationResult,
    PublishObservationRecordsCommand,
)
from cctv_memory.repositories.observation import ObservationRecordPublicationRepository


class PublicationService:
    """Publish validated observation records atomically."""

    def __init__(self, publication: ObservationRecordPublicationRepository) -> None:
        self._publication = publication

    def publish(self, command: PublishObservationRecordsCommand) -> PublicationResult:
        """Publish records atomically; the adapter guarantees all-or-nothing."""
        return self._publication.publish_records_atomically(command)
