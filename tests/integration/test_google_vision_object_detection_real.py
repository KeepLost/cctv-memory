from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from cctv_memory.contracts.object_detection import (
    ObjectDetectionBatchRequest,
    ObjectDetectionImageInput,
    ObjectDetectionRequestOptions,
)
from cctv_memory.infrastructure.object_detection.google_vision_adapter import (
    DEFAULT_GOOGLE_VISION_PROXY_URL,
    GoogleVisionObjectDetectionAdapter,
)


@pytest.mark.integration
def test_google_vision_real_object_detection_proxy() -> None:
    if os.environ.get("CCTV_MEMORY_RUN_GOOGLE_VISION_TEST") != "1":
        pytest.skip("set CCTV_MEMORY_RUN_GOOGLE_VISION_TEST=1 to run real Google Vision test")
    image_path = Path("/tmp/test_objects.jpg")
    if not image_path.is_file():
        pytest.skip("/tmp/test_objects.jpg is not available")
    request = ObjectDetectionBatchRequest(
        request_id="google_real_test",
        provider="google_vision",
        images=[
            ObjectDetectionImageInput(
                image_id="frame_001",
                kind="bytes_base64",
                content_base64=base64.b64encode(image_path.read_bytes()).decode("ascii"),
                mime_type="image/jpeg",
            )
        ],
        options=ObjectDetectionRequestOptions(max_results=10),
    )
    adapter = GoogleVisionObjectDetectionAdapter(base_url=DEFAULT_GOOGLE_VISION_PROXY_URL)
    vendor_request = adapter.to_vendor_request(request)
    assert vendor_request["requests"][0]["features"][0]["type"] == "OBJECT_LOCALIZATION"
    result = adapter.detect_objects(request)
    assert result.provider == "google_vision"
    assert result.results[0].status == "succeeded"
    assert result.results[0].detections
    first = result.results[0].detections[0]
    assert first.label
    assert 0.0 <= first.confidence <= 1.0
    assert first.bbox.polygon
    assert first.bbox.xywh is not None
    assert first.category is not None
