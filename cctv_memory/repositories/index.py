"""IndexPort: vector storage/retrieval port (module-map §2.4, repository-port-contract).

The ``IndexPort`` is the boundary for persisting and retrieving observation text
embeddings in the ``observation_vectors`` table (table-schema-spec §5.4). It is a
storage port, not an AI call, so it is synchronous like every other repository
adapter; the asynchronous work (generating embeddings) lives behind
``EmbeddingPort``.

Permission red line (ARCHITECTURE_CONSTITUTION §5, database-capability-contract
§6.4): this port deliberately exposes NO "search the whole corpus for the top-K
nearest vectors" method. Vectors are only ever retrieved for an EXPLICIT set of
record ids — the authorized candidate id set that the caller has already computed
via ``authorized_candidate_pool``. Vector similarity is therefore always confined
to an already-authorized id set; full-corpus vector top-K then trim is impossible
by construction.

Embeddings are index artifacts, never active-record writes (§6): writing vectors
here does not create/modify an ObservationRecord.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StoredVector:
    """A single stored embedding for one record + text channel.

    ``vector_type`` is one of ``static`` / ``dynamic`` / ``tags`` (matches the
    index document contract). ``model_id`` and ``dimension`` are stored so a model
    change can be detected and trigger a reindex (pipeline-experiment §4); they
    travel in the row's ``metadata_json`` along with anything in ``metadata``.
    """

    record_id: str
    vector_type: str
    embedding: list[float]
    model_id: str
    dimension: int
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class IndexPort(Protocol):
    """Port for storing/retrieving observation embeddings within an id set."""

    def upsert_vectors(self, vectors: list[StoredVector]) -> int:
        """Insert or replace stored vectors by ``(record_id, vector_type)``.

        Returns the number of rows written. Idempotent: re-upserting the same key
        replaces the prior vector (e.g. after a reindex with a new model).
        """
        ...

    def get_vectors_for_records(
        self, record_ids: list[str], *, vector_type: str | None = None
    ) -> list[StoredVector]:
        """Return stored vectors for an EXPLICIT set of record ids only.

        ``record_ids`` MUST be the already-authorized candidate id set computed by
        the caller. Optionally filter by ``vector_type``. An empty id list returns
        an empty list (never "all vectors"). There is no full-corpus query method.
        """
        ...

    def delete_vectors_for_records(self, record_ids: list[str]) -> int:
        """Delete stored vectors for the given record ids. Returns rows deleted."""
        ...
