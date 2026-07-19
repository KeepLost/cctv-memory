from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.contracts.pre_vlm_gate import PreVlmGateLog
from cctv_memory.domain.enums import AnalysisScale


def test_pre_vlm_gate_log_roundtrip_sqlite(factory) -> None:  # type: ignore[no-untyped-def]
    log = PreVlmGateLog(
        gate_log_id="pgate_1",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        unit_id="unit_1",
        video_id="video_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        profile_name="default_segment",
        segment_start_ms=0,
        segment_end_ms=12000,
        provider="mock",
        model_id="mock-detector-v1",
        decision={"triggered_vlm": False},
        signals=[{"signal_type": "object_detection"}],
        frame_evidence=[{"uri_basename": "frame_001.jpg", "detections": []}],
        evidence_hash="sha256:test",
        rule_config_hash="sha256:rules",
        suppression_policy="publish_gate_only_record",
        created_at=datetime.now(UTC),
    )
    created = factory.pre_vlm_gate_log().create_log(log)
    assert created.gate_log_id == "pgate_1"
    loaded = factory.pre_vlm_gate_log().get_log("pgate_1")
    assert loaded is not None
    assert loaded.frame_evidence[0]["uri_basename"] == "frame_001.jpg"
    assert factory.pre_vlm_gate_log().list_by_unit("unit_1")[0].evidence_hash == "sha256:test"


def test_pre_vlm_gate_log_error_details_roundtrip_sqlite(factory) -> None:  # type: ignore[no-untyped-def]
    log = PreVlmGateLog(
        gate_log_id="pgate_failed",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        unit_id="unit_1",
        video_id="video_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        profile_name="default_segment",
        segment_start_ms=0,
        segment_end_ms=12000,
        provider="google_vision",
        model_id="google-vision-object-localization",
        status="failed",
        error_type="ObjectDetectionSchemaValidationError",
        error_message="missing responses",
        raw_text_output='{"not_responses": []}',
        parsed_output={"not_responses": []},
        validation_status="schema_validation_failed",
        attempt_details=[{"attempt": 1, "status": "failed"}],
        decision={},
        signals=[],
        frame_evidence=[],
        evidence_hash="sha256:schema_failure",
        suppression_policy="publish_gate_only_record",
        created_at=datetime.now(UTC),
    )
    created = factory.pre_vlm_gate_log().create_log(log)
    assert created.raw_text_output == '{"not_responses": []}'
    loaded = factory.pre_vlm_gate_log().get_log("pgate_failed")
    assert loaded is not None
    assert loaded.parsed_output == {"not_responses": []}
    assert loaded.validation_status == "schema_validation_failed"
    assert loaded.attempt_details == [{"attempt": 1, "status": "failed"}]
