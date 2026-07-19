from __future__ import annotations

import httpx
import pytest
from cctv_memory.contracts.object_detection import (
    BoundingBox,
    ObjectDetectionBatchRequest,
    ObjectDetectionImageInput,
    Point2D,
    polygon_to_xywh,
)
from cctv_memory.contracts.pre_vlm_gate import (
    GateFrameInput,
    GateProfile,
    GateRule,
    GateSignal,
    PreVlmGateRequest,
)
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.domain.exceptions import ObjectDetectionSchemaValidationError
from cctv_memory.domain.policies.pre_vlm_gate import decide_pre_vlm_gate
from cctv_memory.infrastructure.object_detection.google_vision_adapter import (
    GoogleVisionObjectDetectionAdapter,
)
from cctv_memory.infrastructure.pre_vlm_gate.gate_runner import PreVlmGateRunner


def test_object_detection_request_requires_matching_payload() -> None:
    with pytest.raises(ValueError):
        ObjectDetectionImageInput(image_id="f1", kind="bytes_base64")
    req = ObjectDetectionBatchRequest(
        request_id="req1",
        provider="mock",
        images=[
            ObjectDetectionImageInput(
                image_id="f1", kind="artifact_ref", artifact_ref="f1.jpg"
            )
        ],
    )
    assert req.schema_version == "object_detection_v1"


def test_polygon_to_xywh_and_bbox_validation() -> None:
    points = [
        Point2D(x=0.1, y=0.2),
        Point2D(x=0.4, y=0.2),
        Point2D(x=0.4, y=0.7),
        Point2D(x=0.1, y=0.7),
    ]
    xywh = polygon_to_xywh(points)
    bbox = BoundingBox(coordinate_space="normalized", format="polygon", polygon=points, xywh=xywh)
    assert bbox.xywh is not None
    assert bbox.xywh.width == pytest.approx(0.3)
    assert bbox.xywh.height == pytest.approx(0.5)


def test_decide_pre_vlm_gate_positive_negative_and_force() -> None:
    signal = GateSignal(
        signal_type="object_detection",
        provider="mock",
        frame_count=2,
        frame_evidence=[
            {"detections": [{"label": "person", "confidence": 0.9}]},
            {"detections": []},
        ],
    )
    rule = GateRule(label="person", min_positive_frame_ratio=0.5, min_confidence=0.5)
    decision = decide_pre_vlm_gate(
        signals=[signal],
        rules=[rule],
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        suppression_policy="publish_gate_only_record",
    ).decision
    assert decision.triggered_vlm is True

    decision = decide_pre_vlm_gate(
        signals=[signal],
        rules=[GateRule(label="person", min_positive_frame_ratio=1.0, min_confidence=0.5)],
        analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
        suppression_policy="skip_without_record",
    ).decision
    assert decision.triggered_vlm is False


def test_google_vision_missing_responses_raises_schema_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_responses": []})

    adapter = GoogleVisionObjectDetectionAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    request = ObjectDetectionBatchRequest(
        request_id="od_req",
        provider="google_vision",
        images=[
            ObjectDetectionImageInput(
                image_id="frame_0", kind="bytes_base64", content_base64="AA=="
            )
        ],
    )
    with pytest.raises(ObjectDetectionSchemaValidationError) as excinfo:
        adapter.detect_objects(request)
    assert excinfo.value.raw_response == '{"not_responses": []}'
    assert excinfo.value.stage == "schema_validation_failed"


def test_pre_vlm_gate_writes_failed_log_on_detector_schema_error() -> None:
    class BadDetector:
        calls = 0

        def detect_objects(self, _request):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise ObjectDetectionSchemaValidationError(
                "bad detector schema",
                stage="schema_validation_failed",
                raw_response='{"bad": true}',
                parsed_payload={"bad": True},
            )

    logs = []
    detector = BadDetector()
    runner = PreVlmGateRunner(
        object_detection=detector,
        log_writer=lambda log: logs.append(log) or log,
        schema_regenerate_max_attempts=1,
    )
    request = PreVlmGateRequest(
        request_id="pgate_req",
        gate_log_id="pgate_log",
        analysis_job_id="job_1",
        scale_task_id="scale_1",
        unit_id="unit_1",
        video_id="video_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        unit_kind="default_segment_window",
        segment_start_ms=0,
        segment_end_ms=12000,
        provider="bad_detector",
        model_id="bad-v1",
        profile=GateProfile(
            profile_name="default_segment",
            enabled=True,
            analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
            suppression_policy="publish_gate_only_record",
            rules=[GateRule(label="person")],
        ),
        frames=[GateFrameInput(uri="f.jpg", frame_index=0, timestamp_ms=0)],
    )
    with pytest.raises(ObjectDetectionSchemaValidationError):
        runner.evaluate(request)
    assert detector.calls == 2
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].raw_text_output == '{"bad": true}'
    assert logs[0].validation_status == "schema_validation_failed"
