"""Default golden-query fixtures for benchmark/experiment (application layer).

These build a small, deterministic golden query set. Because the MVP uses a mock
VLM, "relevance" is defined against the deterministic mock tags rather than human
judgement — this benchmarks the SEARCH layer, not real VLM quality (honest).

``golden_queries_from_records`` derives relevance from the records themselves:
for a tag query, the relevant set is every record carrying that tag. This keeps
the benchmark reproducible without external ground-truth files.
"""

from __future__ import annotations

from cctv_memory.contracts.experiment import GoldenQuery
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.domain.enums import SearchMode

# Static, dependency-free golden set keyed to the mock VLM tag vocabulary.
_DEFAULT_TERMS = ("person", "backpack", "loitering", "vehicle", "running")


def default_golden_queries() -> list[GoldenQuery]:
    """Return golden queries with relevance left empty (resolved by the caller).

    For CLI ``benchmark run`` without a fixture file, relevance is unknown ahead
    of time; callers may use ``golden_queries_from_records`` instead. This list
    is primarily useful for smoke-running the search path.
    """
    return [
        GoldenQuery(query_id=f"q_{term}", query_text=term, search_mode=SearchMode.HYBRID)
        for term in _DEFAULT_TERMS
    ]


def golden_queries_from_records(
    records: list[ObservationRecord], terms: tuple[str, ...] = _DEFAULT_TERMS
) -> list[GoldenQuery]:
    """Build golden queries whose relevant set is records containing each term.

    A record is relevant to ``term`` if the term appears in its tags or static
    text (case-insensitive). Deterministic and reproducible.
    """
    queries: list[GoldenQuery] = []
    for term in terms:
        relevant = [
            r.record_id
            for r in records
            if term in [t.lower() for t in r.tags]
            or term in r.static_description_text.lower()
        ]
        if relevant:
            queries.append(
                GoldenQuery(
                    query_id=f"q_{term}",
                    query_text=term,
                    search_mode=SearchMode.HYBRID,
                    relevant_record_ids=relevant,
                )
            )
    return queries
