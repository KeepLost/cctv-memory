from __future__ import annotations

import pytest

from cctv_memory.contracts.object_detection import (
    BoundingBox,
    ObjectDetectionBatchRequest,
    ObjectDetectionImageInput,
    Point2D,
    polygon_to_xywh,
)
from cctv_memory.contracts.pre_vlm_gate import GateRule, GateSignal
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.domain.policies.pre_vlm_gate import decide_pre_vlm_gate


def test_object_detection_request_requires_matching_payload() -> None:
    with pytest.raises(ValueError):
        ObjectDetectionImageInput(image_id="f1", kind="bytes_base64")
    req = ObjectDetectionBatchRequest(
        request_id="req1",
        provider="mock",
        images=[ObjectDetectionImageInput(image_id="f1", kind="artifact_ref", artifact_ref="f1.jpg")],
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
