from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cctv_memory.application.ingestion import IngestionService
from cctv_memory.application.search import SearchService
from cctv_memory.config.settings import DetectorGateRuleSection, PreVlmGateRuleSection
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.search import StartObservationSearchRequest
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmAttr, VlmObservationOutput, VlmQuality
from cctv_memory.domain.enums import (
    Capability,
    PrincipalType,
    SearchMode,
    SecurityLevel,
    SourceType,
)
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.workers.analysis_worker import AnalysisWorker
from sqlalchemy import func, select

from tests.conftest import make_scope, seed_camera


class _CountingVlm:
    def __init__(self) -> None:
        self.calls = 0

    def analyze_segment(self, request: object) -> VlmObservationOutput:
        self.calls += 1
        return VlmObservationOutput(
            static="vlm static",
            dynamic="vlm dynamic",
            tags=["vlm_tag"],
            quality=VlmQuality(score=0.9, reason="ok"),
            attr=VlmAttr(alert=False),
        )


def _configure_gate(runtime: Any, *, positive_ratio: float) -> None:
    cfg = runtime.config
    cfg.pipeline.video_metadata_mode = "static"
    cfg.pipeline.static_duration_ms = 12_000
    cfg.pipeline.default_segment.window_seconds = 12
    cfg.pipeline.default_segment.overlap_seconds = 0
    cfg.pipeline.default_segment.frames_per_segment = 4
    cfg.pipeline.detector_gate.enabled = True
    cfg.pipeline.detector_gate.provider = "mock"
    cfg.pipeline.detector_gate.model_id = "mock-detector-v1"
    cfg.pipeline.detector_gate.mock_positive_labels = ["person"]
    cfg.pipeline.detector_gate.mock_positive_frame_ratio = positive_ratio
    cfg.pipeline.detector_gate.mock_confidence = 0.9
    cfg.pipeline.detector_gate.rules = [
        DetectorGateRuleSection(
            label="person",
            min_positive_frame_ratio=0.5,
            min_confidence=0.5,
        )
    ]


def _submit_one(runtime: Any, *, key: str) -> str:
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_camera(repos)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        principal = Principal(
            principal_id="svc_detector",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            display_name="svc",
            roles=["security_viewer"],
        )
        repos.principal().create_principal(principal)
        resp = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        ).submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
                idempotency_key=key,
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        return resp.analysis_job_id


def test_detector_gate_negative_publishes_detector_only_record(runtime_factory: Any) -> None:
    runtime = runtime_factory()
    _configure_gate(runtime, positive_ratio=0.0)
    _submit_one(runtime, key="detector-negative")
    vlm = _CountingVlm()

    processed = AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=12_000),
        vlm=vlm,
    ).process_one()

    assert processed is not None
    assert vlm.calls == 0
    with runtime.session() as session:
        records = list(session.scalars(select(orm.ObservationRecord)))
        assert len(records) == 1
        rec = records[0]
        assert rec.static_description_text == ""
        assert rec.dynamic_description_text == ""
        assert rec.tags_json == "[]"
        assert "detector_gate" in rec.attributes_json
        assert session.scalar(select(func.count()).select_from(orm.ModelCallLog)) == 0
        gates = list(session.scalars(select(orm.DetectorGateLog)))
        assert len(gates) == 1
        assert gates[0].decision_json
        assert gates[0].frame_evidence_json
        assert gates[0].evidence_hash.startswith("sha256:")
        assert "/data/videos" not in gates[0].frame_evidence_json
        assert "base64" not in gates[0].frame_evidence_json.lower()
    runtime.dispose()


def test_detector_gate_positive_calls_vlm_and_attaches_attr(runtime_factory: Any) -> None:
    runtime = runtime_factory()
    _configure_gate(runtime, positive_ratio=1.0)
    _submit_one(runtime, key="detector-positive")
    vlm = _CountingVlm()

    AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=12_000),
        vlm=vlm,
    ).process_one()

    assert vlm.calls == 1
    with runtime.session() as session:
        rec = session.scalar(select(orm.ObservationRecord))
        assert rec is not None
        assert rec.static_description_text == "vlm static"
        assert rec.dynamic_description_text == "vlm dynamic"
        assert rec.tags_json == '["vlm_tag"]'
        assert "detector_gate" in rec.attributes_json
        assert session.scalar(select(func.count()).select_from(orm.ModelCallLog)) == 1
        gate = session.scalar(select(orm.DetectorGateLog))
        assert gate is not None
        assert gate.segment_start_ms == rec.segment_start_ms
        assert gate.segment_end_ms == rec.segment_end_ms
    runtime.dispose()


def test_detector_only_record_retrievable_by_time_location_search(runtime_factory: Any) -> None:
    runtime = runtime_factory()
    _configure_gate(runtime, positive_ratio=0.0)
    _submit_one(runtime, key="detector-search")
    AnalysisWorker(
        runtime,
        video_processor=StaticVideoProcessor(duration_ms=12_000),
        vlm=_CountingVlm(),
    ).process_one()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        scope = make_scope(
            camera_ids=["cam_lobby_01"],
            location_ids=["loc_lobby_01"],
            policy_ids=["policy_public_area"],
        )
        service = SearchService(repos.observation_read(), repos.search_context(), repos.audit())
        resp = service.start_search(
            StartObservationSearchRequest(
                query_text="what happened",
                location_ids=["loc_lobby_01"],
                time_range={
                    "start": datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
                    "end": datetime(2026, 6, 22, 10, 1, tzinfo=UTC),
                },
                search_mode=SearchMode.HYBRID,
                top_k=5,
            ),
            scope,
        )
        assert resp.candidate_count == 1
    runtime.dispose()


def test_detector_gate_table_exists(engine: Any) -> None:
    assert orm.DetectorGateLog.__tablename__ == "detector_gate_logs"
    with engine.connect() as conn:
        names = {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master")}
    assert "detector_gate_logs" in names


def test_pre_vlm_gate_negative_publishes_gate_only_record(runtime_factory: Any) -> None:
    runtime = runtime_factory()
    cfg = runtime.config
    cfg.pipeline.video_metadata_mode = "static"
    cfg.pipeline.static_duration_ms = 12_000
    cfg.pipeline.default_segment.window_seconds = 12
    cfg.pipeline.default_segment.overlap_seconds = 0
    cfg.pipeline.default_segment.frames_per_segment = 4
    cfg.pipeline.pre_vlm_gate.enabled = True
    cfg.pipeline.pre_vlm_gate.provider = "mock"
    cfg.pipeline.pre_vlm_gate.mock.positive_labels = []
    cfg.pipeline.pre_vlm_gate.default_segment.enabled = True
    cfg.pipeline.pre_vlm_gate.default_segment.rules = [
        PreVlmGateRuleSection(label="person", min_positive_frame_ratio=0.5)
    ]
    _submit_one(runtime, key="pre-vlm-default-negative")
    vlm = _CountingVlm()

    AnalysisWorker(
        runtime, video_processor=StaticVideoProcessor(duration_ms=12_000), vlm=vlm
    ).process_one()

    assert vlm.calls == 0
    with runtime.session() as session:
        assert session.scalar(select(func.count()).select_from(orm.ObservationRecord)) == 1
        assert session.scalar(select(func.count()).select_from(orm.PreVlmGateLog)) == 1
    runtime.dispose()
