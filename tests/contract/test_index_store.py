"""IndexPort (SQLite) contract tests (task §8).

Verifies upsert/get/delete scoped to EXPLICIT record id sets over the
``observation_vectors`` table, and that there is no full-corpus retrieval path
(ARCHITECTURE_CONSTITUTION §5, database-capability-contract §6.4).
"""

from __future__ import annotations

from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory
from cctv_memory.repositories.index import IndexPort, StoredVector


def _vector(record_id: str, vtype: str, value: float, dim: int = 4) -> StoredVector:
    return StoredVector(
        record_id=record_id,
        vector_type=vtype,
        embedding=[value] * dim,
        model_id="BAAI/bge-m3",
        dimension=dim,
        metadata={"source": "test"},
    )


def test_index_repo_is_an_index_port(factory: SqliteRepositoryFactory) -> None:
    assert isinstance(factory.index(), IndexPort)


def test_upsert_and_get_by_explicit_ids(factory: SqliteRepositoryFactory) -> None:
    index = factory.index()
    written = index.upsert_vectors(
        [
            _vector("rec_a", "static", 0.1),
            _vector("rec_a", "dynamic", 0.2),
            _vector("rec_b", "static", 0.3),
        ]
    )
    assert written == 3

    got = index.get_vectors_for_records(["rec_a"])
    assert {v.vector_type for v in got} == {"static", "dynamic"}
    # rec_b is NOT returned because it was not in the explicit id set.
    assert all(v.record_id == "rec_a" for v in got)


def test_get_roundtrips_model_and_dimension_metadata(
    factory: SqliteRepositoryFactory,
) -> None:
    index = factory.index()
    index.upsert_vectors([_vector("rec_a", "static", 0.5, dim=4)])
    got = index.get_vectors_for_records(["rec_a"], vector_type="static")
    assert len(got) == 1
    stored = got[0]
    assert stored.model_id == "BAAI/bge-m3"
    assert stored.dimension == 4
    assert stored.embedding == [0.5, 0.5, 0.5, 0.5]
    assert stored.metadata == {"source": "test"}


def test_upsert_replaces_existing_vector(factory: SqliteRepositoryFactory) -> None:
    index = factory.index()
    index.upsert_vectors([_vector("rec_a", "static", 0.1)])
    index.upsert_vectors([_vector("rec_a", "static", 0.9)])
    got = index.get_vectors_for_records(["rec_a"], vector_type="static")
    assert len(got) == 1
    assert got[0].embedding == [0.9, 0.9, 0.9, 0.9]


def test_get_with_empty_id_set_returns_empty(factory: SqliteRepositoryFactory) -> None:
    index = factory.index()
    index.upsert_vectors([_vector("rec_a", "static", 0.1)])
    # Empty id set must NEVER fall through to "all vectors".
    assert index.get_vectors_for_records([]) == []


def test_vector_type_filter(factory: SqliteRepositoryFactory) -> None:
    index = factory.index()
    index.upsert_vectors(
        [_vector("rec_a", "static", 0.1), _vector("rec_a", "dynamic", 0.2)]
    )
    static_only = index.get_vectors_for_records(["rec_a"], vector_type="static")
    assert len(static_only) == 1
    assert static_only[0].vector_type == "static"


def test_delete_vectors_for_records(factory: SqliteRepositoryFactory) -> None:
    index = factory.index()
    index.upsert_vectors(
        [_vector("rec_a", "static", 0.1), _vector("rec_b", "static", 0.2)]
    )
    deleted = index.delete_vectors_for_records(["rec_a"])
    assert deleted == 1
    assert index.get_vectors_for_records(["rec_a"]) == []
    assert len(index.get_vectors_for_records(["rec_b"])) == 1


def test_no_full_corpus_topk_method() -> None:
    # Guard the permission red line: the port exposes no whole-corpus search.
    forbidden = {"search", "top_k", "topk", "nearest", "knn", "query_vectors", "all"}
    methods = {m for m in dir(IndexPort) if not m.startswith("_")}
    intersection = methods & forbidden
    assert not intersection, f"IndexPort exposes forbidden method(s): {intersection}"
