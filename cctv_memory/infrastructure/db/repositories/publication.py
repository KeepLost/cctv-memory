"""SQLite publication adapter (the only writer of active ObservationRecord).

Implements atomic publication (ARCHITECTURE_CONSTITUTION §6,
database-capability-contract §9): upsert active records, archive replaced
records into history, update the AnalysisJob publish summary, and append an
audit event — all within a single transaction. On any failure the whole
transaction is rolled back, leaving no partial active records.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.pipeline import (
    PublicationResult,
    PublishObservationRecordsCommand,
)
from cctv_memory.infrastructure.db import fts, mappers
from cctv_memory.infrastructure.db.models import tables as orm


class SqlitePublicationRepository:
    """Publication-only adapter for active ObservationRecord writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def publish_records_atomically(
        self, command: PublishObservationRecordsCommand
    ) -> PublicationResult:
        created: list[str] = []
        updated: list[str] = []
        archived: list[str] = []
        now = datetime.now().astimezone().isoformat()

        # All writes happen inside one transaction managed by the surrounding
        # session_scope; a raised exception rolls everything back.
        with self._session.begin_nested():
            for record in command.records:
                existing = self._session.scalar(
                    select(orm.ObservationRecord).where(
                        orm.ObservationRecord.video_id == record.video_id,
                        orm.ObservationRecord.segment_start_ms == record.segment_start_ms,
                        orm.ObservationRecord.segment_end_ms == record.segment_end_ms,
                        orm.ObservationRecord.analysis_scale == record.analysis_scale.value,
                    )
                )
                if existing is not None:
                    # Archive the replaced active record into history.
                    snapshot = mappers.observation_to_dto(existing)
                    self._session.add(
                        orm.ObservationRecordHistory(
                            history_id=f"hist_{uuid.uuid4().hex}",
                            old_record_id=existing.record_id,
                            replaced_by_record_id=record.record_id,
                            archived_by_analysis_job_id=command.analysis_job_id,
                            archived_at=now,
                            archive_reason=command.archive_reason,
                            record_snapshot_json=snapshot.model_dump_json(),
                        )
                    )
                    archived.append(existing.record_id)
                    self._session.delete(existing)
                    self._session.flush()
                    # Remove the replaced record from the FTS index.
                    fts.deindex_record(self._session, existing.record_id)
                    updated.append(record.record_id)
                else:
                    created.append(record.record_id)

                self._session.add(mappers.observation_to_orm(record))
                self._session.flush()
                # Index the newly published record for full-text search.
                fts.index_record(
                    self._session,
                    record_id=record.record_id,
                    static_text=record.static_description_text,
                    dynamic_text=record.dynamic_description_text,
                    tags=record.tags,
                )

            # Update AnalysisJob publish summary.
            job = self._session.get(orm.AnalysisJob, command.analysis_job_id)
            if job is not None:
                import json

                job.created_record_ids_json = json.dumps(
                    json.loads(job.created_record_ids_json) + created
                )
                job.updated_record_ids_json = json.dumps(
                    json.loads(job.updated_record_ids_json) + updated
                )
                job.archived_record_ids_json = json.dumps(
                    json.loads(job.archived_record_ids_json) + archived
                )

            # Append an audit event for the publication.
            audit = AuditEvent(
                audit_event_id=f"audit_{uuid.uuid4().hex}",
                event_type="publication_succeeded",
                record_ids=created + updated,
                metadata={"analysis_job_id": command.analysis_job_id},
            )
            self._session.add(mappers.audit_to_orm(audit))
            self._session.flush()

        return PublicationResult(
            analysis_job_id=command.analysis_job_id,
            created_record_ids=created,
            updated_record_ids=updated,
            archived_record_ids=archived,
        )
