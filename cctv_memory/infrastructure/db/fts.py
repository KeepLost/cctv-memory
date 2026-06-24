"""FTS5 index helpers (infrastructure-internal).

Maintains the FTS5 virtual tables (``observation_static_fts``,
``observation_dynamic_fts``, ``observation_tags_fts``) that index observation
text for full-text search. These are an INDEX over the active
``observation_records`` table, which remains the fact source
(database-capability-contract §6.4). All writes happen inside the publication
transaction so the index stays consistent with active records.

Rows are keyed by ``record_id`` (UNINDEXED) + a searchable ``text`` column.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

_STATIC_FTS = "observation_static_fts"
_DYNAMIC_FTS = "observation_dynamic_fts"
_TAGS_FTS = "observation_tags_fts"


def fts_available(session: Session) -> bool:
    """Return True if the FTS5 virtual tables exist (graceful LIKE fallback otherwise)."""
    row = session.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=:n"
        ),
        {"n": _STATIC_FTS},
    ).first()
    return row is not None


def index_record(
    session: Session,
    *,
    record_id: str,
    static_text: str,
    dynamic_text: str,
    tags: list[str],
) -> None:
    """Insert FTS rows for a newly published record (call inside publication tx)."""
    if not fts_available(session):
        return
    session.execute(
        text(f"INSERT INTO {_STATIC_FTS}(record_id, text) VALUES (:r, :t)"),
        {"r": record_id, "t": static_text},
    )
    session.execute(
        text(f"INSERT INTO {_DYNAMIC_FTS}(record_id, text) VALUES (:r, :t)"),
        {"r": record_id, "t": dynamic_text},
    )
    session.execute(
        text(f"INSERT INTO {_TAGS_FTS}(record_id, text) VALUES (:r, :t)"),
        {"r": record_id, "t": " ".join(tags)},
    )


def deindex_record(session: Session, record_id: str) -> None:
    """Remove FTS rows for an archived/replaced record (call inside publication tx)."""
    if not fts_available(session):
        return
    for table in (_STATIC_FTS, _DYNAMIC_FTS, _TAGS_FTS):
        session.execute(
            text(f"DELETE FROM {table} WHERE record_id = :r"),
            {"r": record_id},
        )


def search_static(session: Session, query: str, record_ids: list[str]) -> dict[str, float]:
    """Return {record_id: bm25_rank_score} for static FTS matches within record_ids.

    Restricted to the provided ``record_ids`` (the authorized candidate set), so
    the scope pre-filter is always honored. Lower bm25 is better; we return a
    positive relevance score (negated bm25) for convenience. Empty list -> {}.
    """
    return _search(session, _STATIC_FTS, query, record_ids)


def search_dynamic(session: Session, query: str, record_ids: list[str]) -> dict[str, float]:
    """Return {record_id: relevance} for dynamic FTS matches within record_ids."""
    return _search(session, _DYNAMIC_FTS, query, record_ids)


def search_tags(session: Session, query: str, record_ids: list[str]) -> dict[str, float]:
    """Return {record_id: relevance} for tag FTS matches within record_ids."""
    return _search(session, _TAGS_FTS, query, record_ids)


def _search(
    session: Session, table: str, query: str, record_ids: list[str]
) -> dict[str, float]:
    if not record_ids or not query.strip() or not fts_available(session):
        return {}
    match_query = _to_match_query(query)
    if not match_query:
        return {}
    # Parameterize the record_id allow-list to keep the scope filter in SQL.
    placeholders = ",".join(f":id{i}" for i in range(len(record_ids)))
    params: dict[str, object] = {f"id{i}": rid for i, rid in enumerate(record_ids)}
    params["q"] = match_query
    sql = text(
        f"SELECT record_id, bm25({table}) AS score FROM {table} "
        f"WHERE {table} MATCH :q AND record_id IN ({placeholders})"
    )
    result: dict[str, float] = {}
    for row in session.execute(sql, params):
        # bm25 is lower-is-better; convert to higher-is-better relevance.
        result[row.record_id] = -float(row.score)
    return result


def _to_match_query(query: str) -> str:
    """Build a safe FTS5 MATCH query: OR of quoted bare terms.

    Quoting each term avoids FTS5 syntax errors from punctuation/operators in
    free text and keeps the query deterministic.
    """
    terms = [t for t in query.lower().replace(",", " ").split() if t.isalnum()]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)
