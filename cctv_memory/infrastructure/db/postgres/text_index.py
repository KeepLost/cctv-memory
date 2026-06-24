"""PostgreSQL text-index helpers.

The table is an index artifact over ``observation_records``. All ranking helpers
require an explicit candidate-id set so authorized/structured filtering happens
before text ranking.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def text_index_available(session: Session) -> bool:
    row = session.execute(
        text("SELECT to_regclass('public.observation_text_index') AS name")
    ).first()
    return bool(row and row.name)


def index_record(
    session: Session,
    *,
    record_id: str,
    static_text: str,
    dynamic_text: str,
    tags: list[str],
) -> None:
    if not text_index_available(session):
        return
    for field, content in (
        ("static", static_text),
        ("dynamic", dynamic_text),
        ("tags", " ".join(tags)),
    ):
        session.execute(
            text(
                """
                INSERT INTO observation_text_index(record_id, text_field, content, tsv)
                VALUES (:record_id, :text_field, :content,
                        to_tsvector('simple', coalesce(:content, '')))
                ON CONFLICT (record_id, text_field) DO UPDATE SET
                  content = EXCLUDED.content,
                  tsv = EXCLUDED.tsv
                """
            ),
            {"record_id": record_id, "text_field": field, "content": content},
        )


def deindex_record(session: Session, record_id: str) -> None:
    if not text_index_available(session):
        return
    session.execute(
        text("DELETE FROM observation_text_index WHERE record_id = :record_id"),
        {"record_id": record_id},
    )


def search(session: Session, query: str, record_ids: list[str], *, field: str) -> dict[str, float]:
    if not record_ids or not query.strip() or not text_index_available(session):
        return {}
    rows = session.execute(
        text(
            """
            WITH candidates(record_id) AS (
              SELECT unnest(CAST(:candidate_ids AS text[]))
            ), q AS (
              SELECT websearch_to_tsquery('simple', :query) AS tsq
            )
            SELECT i.record_id,
                   ts_rank_cd(i.tsv, q.tsq) + similarity(i.content, :query) * 0.05 AS score
            FROM observation_text_index i
            JOIN candidates c ON c.record_id = i.record_id
            CROSS JOIN q
            WHERE i.text_field = :field
              AND (i.tsv @@ q.tsq OR i.content % :query)
            ORDER BY score DESC, i.record_id
            """
        ),
        {"candidate_ids": record_ids, "query": query, "field": field},
    )
    return {str(row.record_id): float(row.score) for row in rows}
