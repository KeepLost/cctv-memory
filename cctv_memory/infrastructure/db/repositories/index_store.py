"""SQLite IndexPort adapter over the ``observation_vectors`` table.

Stores observation text embeddings as JSON-encoded float arrays (the table's
``embedding`` column is TEXT, table-schema-spec §5.4) with model/dimension/
channel metadata in ``metadata_json``. Retrieval is ALWAYS scoped to an explicit
set of record ids — there is no full-corpus nearest-neighbour query
(ARCHITECTURE_CONSTITUTION §5, database-capability-contract §6.4).

This adapter does not write active ObservationRecord rows; embeddings are index
artifacts only (§6).
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.repositories.index import StoredVector


class SqliteIndexRepository:
    """SQLite adapter implementing ``IndexPort`` over ``observation_vectors``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_vectors(self, vectors: list[StoredVector]) -> int:
        if not vectors:
            return 0
        for vector in vectors:
            metadata = dict(vector.metadata)
            metadata["model_id"] = vector.model_id
            metadata["dimension"] = vector.dimension
            metadata["vector_type"] = vector.vector_type
            stmt = sqlite_insert(orm.ObservationVector).values(
                record_id=vector.record_id,
                vector_type=vector.vector_type,
                embedding=json.dumps(vector.embedding),
                metadata_json=json.dumps(metadata),
            )
            # UPSERT on the (record_id, vector_type) primary key so reindexing
            # replaces a prior vector deterministically.
            stmt = stmt.on_conflict_do_update(
                index_elements=["record_id", "vector_type"],
                set_={
                    "embedding": stmt.excluded.embedding,
                    "metadata_json": stmt.excluded.metadata_json,
                },
            )
            self._session.execute(stmt)
        self._session.flush()
        return len(vectors)

    def get_vectors_for_records(
        self, record_ids: list[str], *, vector_type: str | None = None
    ) -> list[StoredVector]:
        if not record_ids:
            return []
        stmt = select(orm.ObservationVector).where(
            orm.ObservationVector.record_id.in_(record_ids)
        )
        if vector_type is not None:
            stmt = stmt.where(orm.ObservationVector.vector_type == vector_type)
        rows = self._session.scalars(stmt)
        return [self._to_stored_vector(row) for row in rows]

    def delete_vectors_for_records(self, record_ids: list[str]) -> int:
        if not record_ids:
            return 0
        result = self._session.execute(
            delete(orm.ObservationVector).where(
                orm.ObservationVector.record_id.in_(record_ids)
            )
        )
        self._session.flush()
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0)

    @staticmethod
    def _to_stored_vector(row: orm.ObservationVector) -> StoredVector:
        metadata: dict[str, object] = json.loads(row.metadata_json) if row.metadata_json else {}
        embedding = [float(x) for x in json.loads(row.embedding)]
        model_id = str(metadata.get("model_id", ""))
        dimension_raw = metadata.get("dimension", len(embedding))
        dimension = dimension_raw if isinstance(dimension_raw, int) else len(embedding)
        # Keep only caller-supplied metadata keys; system keys are surfaced on the
        # dedicated fields instead of duplicated in ``metadata``.
        extra = {
            k: v
            for k, v in metadata.items()
            if k not in {"model_id", "dimension", "vector_type"}
        }
        return StoredVector(
            record_id=row.record_id,
            vector_type=row.vector_type,
            embedding=embedding,
            model_id=model_id,
            dimension=dimension,
            metadata=extra,
        )
