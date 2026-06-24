"""M2 tests: retrieval metrics, benchmark runner, experiment runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cctv_memory.application.benchmark import BenchmarkRunner, ExperimentRunner
from cctv_memory.application.experiment_fixtures import golden_queries_from_records
from cctv_memory.contracts.experiment import (
    ExperimentConfig,
    GoldenQuery,
    SearchWeightConfig,
)
from cctv_memory.contracts.observation import ObservationRecord
from cctv_memory.contracts.pipeline import PublishObservationRecordsCommand
from cctv_memory.domain import metrics
from cctv_memory.domain.enums import AnalysisScale, SearchMode, SecurityLevel
from cctv_memory.infrastructure.db.factory import SqliteRepositoryFactory

from tests.conftest import make_scope, seed_camera

_BASE = datetime(2026, 6, 6, 21, 0, tzinfo=UTC)


def test_precision_recall_mrr_basic() -> None:
    ranked = ["a", "b", "c", "d"]
    relevant = {"b", "d"}
    assert metrics.precision_at_k(ranked, relevant, 2) == 0.5
    assert metrics.recall_at_k(ranked, relevant, 2) == 0.5
    assert metrics.recall_at_k(ranked, relevant, 4) == 1.0
    assert metrics.reciprocal_rank(ranked, relevant) == 0.5  # first relevant at rank 2


def test_mrr_no_relevant_is_zero() -> None:
    assert metrics.reciprocal_rank(["a", "b"], set()) == 0.0
    agg = metrics.aggregate([], k=10)
    assert agg.mrr == 0.0


def _rec(record_id: str, *, static: str, tags: list[str], start_ms: int) -> ObservationRecord:
    return ObservationRecord(
        record_id=record_id,
        video_id="video_001",
        analysis_job_id="job_001",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=start_ms,
        segment_end_ms=start_ms + 12_000,
        observed_start_time=_BASE,
        observed_end_time=_BASE + timedelta(seconds=12),
        camera_id="cam_lobby_01",
        location_id="loc_lobby_01",
        static_description_text=static,
        dynamic_description_text="moving",
        tags=tags,
        access_policy_id="policy_public_area",
        security_level=SecurityLevel.INTERNAL,
    )


def _publish(factory: SqliteRepositoryFactory, *records: ObservationRecord) -> None:
    factory.publication().publish_records_atomically(
        PublishObservationRecordsCommand(
            command_id="c", analysis_job_id="job_001", records=list(records)
        )
    )


def _scope():  # type: ignore[no-untyped-def]
    return make_scope(
        camera_ids=["cam_lobby_01"],
        location_ids=["loc_lobby_01"],
        policy_ids=["policy_public_area"],
    )


def test_benchmark_runner_scores_relevant_records(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_bp", static="person with backpack", tags=["person", "backpack"], start_ms=0),
        _rec("obs_car", static="a vehicle parked", tags=["vehicle"], start_ms=12000),
    )
    queries = [
        GoldenQuery(query_id="q1", query_text="backpack", search_mode=SearchMode.HYBRID,
                    relevant_record_ids=["obs_bp"]),
        GoldenQuery(query_id="q2", query_text="vehicle", search_mode=SearchMode.HYBRID,
                    relevant_record_ids=["obs_car"]),
    ]
    runner = BenchmarkRunner(
        factory.observation_read(), factory.search_context(), factory.audit()
    )
    result = runner.run(queries, _scope(), k=5)
    assert result.num_queries == 2
    assert result.mrr == 1.0  # each relevant record ranks first
    assert result.mean_precision_at_k > 0


def test_golden_queries_from_records_derives_relevance(
    factory: SqliteRepositoryFactory,
) -> None:
    records = [
        _rec("obs_bp", static="person with backpack", tags=["person", "backpack"], start_ms=0),
        _rec("obs_car", static="a vehicle", tags=["vehicle"], start_ms=12000),
    ]
    queries = golden_queries_from_records(records)
    by_id = {q.query_id: q for q in queries}
    assert "obs_bp" in by_id["q_backpack"].relevant_record_ids
    assert "obs_car" in by_id["q_vehicle"].relevant_record_ids


def test_experiment_runner_compares_arms(factory: SqliteRepositoryFactory) -> None:
    seed_camera(factory)
    _publish(
        factory,
        _rec("obs_bp", static="person with backpack", tags=["person", "backpack"], start_ms=0),
        _rec("obs_run", static="person running", tags=["person", "running"], start_ms=12000),
    )
    config = ExperimentConfig(
        name="weights-ab",
        k=5,
        arms=[
            SearchWeightConfig(name="balanced", static_weight=1.0, dynamic_weight=1.0),
            SearchWeightConfig(name="static-heavy", static_weight=2.0, dynamic_weight=0.5),
        ],
        queries=[
            GoldenQuery(query_id="q1", query_text="backpack",
                        relevant_record_ids=["obs_bp"]),
        ],
    )
    runner = ExperimentRunner(
        factory.observation_read(), factory.search_context(), factory.audit()
    )
    result = runner.run(config, _scope())
    assert len(result.arms) == 2
    assert {a.arm_name for a in result.arms} == {"balanced", "static-heavy"}
    assert result.best_arm_by_mrr is not None
