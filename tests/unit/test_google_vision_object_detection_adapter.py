from __future__ import annotations

from cctv_memory.contracts.object_detection import (
    ObjectDetectionBatchRequest,
    ObjectDetectionImageInput,
    ObjectDetectionRequestOptions,
)
from cctv_memory.infrastructure.object_detection.google_vision_adapter import (
    GoogleVisionObjectDetectionAdapter,
)


def _request() -> ObjectDetectionBatchRequest:
    return ObjectDetectionBatchRequest(
        request_id="od_req_001",
        provider="google_vision",
        images=[
            ObjectDetectionImageInput(
                image_id="frame_001",
                kind="bytes_base64",
                content_base64="abc",
                mime_type="image/jpeg",
                width_px=640,
                frame_index=1,
                timestamp_ms=120500,
                sha256="sha256:test",
            )
        ],
        options=ObjectDetectionRequestOptions(max_results=10),
    )


def test_google_vision_to_vendor_request() -> None:
    adapter = GoogleVisionObjectDetectionAdapter()
    payload = adapter.to_vendor_request(_request())
    assert payload == {
        "requests": [
            {
                "image": {"content": "abc"},
                "features": [{"type": "OBJECT_LOCALIZATION", "maxResults": 10}],
            }
        ]
    }


def test_google_vision_from_vendor_response_maps_bbox_and_category() -> None:
    adapter = GoogleVisionObjectDetectionAdapter()
    result = adapter.from_vendor_response(
        _request(),
        {
            "responses": [
                {
                    "localizedObjectAnnotations": [
                        {
                            "mid": "/m/0199g",
                            "name": "Bicycle",
                            "score": 0.664858,
                            "boundingPoly": {
                                "normalizedVertices": [
                                    {"x": 0.12695313, "y": 0.26757813},
                                    {"x": 0.8359375, "y": 0.26757813},
                                    {"x": 0.8359375, "y": 0.9375},
                                    {"x": 0.12695313, "y": 0.9375},
                                ]
                            },
                        }
                    ]
                }
            ]
        },
    )
    item = result.results[0].detections[0]
    assert item.label == "Bicycle"
    assert item.confidence == 0.664858
    assert item.bbox.polygon is not None
    assert item.bbox.xywh is not None
    assert item.bbox.xywh.width > 0
    assert item.category is not None
    assert item.category.provider_category_id == "/m/0199g"
