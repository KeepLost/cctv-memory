"""Deterministic mock object detection adapter."""

from __future__ import annotations

from cctv_memory.contracts.object_detection import (
    BoundingBox,
    BoundingBoxXywh,
    ObjectDetectionBatchRequest,
    ObjectDetectionBatchResult,
    ObjectDetectionImageMetadata,
    ObjectDetectionImageResult,
    ObjectDetectionItem,
    ObjectDetectionUsage,
)
from cctv_memory.services.object_detection import ObjectDetectionPort


class MockObjectDetectionAdapter(ObjectDetectionPort):
    """Configurable deterministic detector for local runs and tests."""

    def __init__(
        self,
        *,
        positive_labels: list[str] | None = None,
        positive_frame_ratio: float = 0.0,
        confidence: float = 0.9,
        model_id: str = "mock-detector-v1",
    ) -> None:
        self._labels = list(positive_labels or [])
        self._ratio = max(0.0, min(1.0, float(positive_frame_ratio)))
        self._confidence = max(0.0, min(1.0, float(confidence)))
        self._model_id = model_id

    def detect_objects(
        self, request: ObjectDetectionBatchRequest
    ) -> ObjectDetectionBatchResult:
        positive_count = int(round(len(request.images) * self._ratio))
        results: list[ObjectDetectionImageResult] = []
        for i, image in enumerate(request.images):
            detections: list[ObjectDetectionItem] = []
            if i < positive_count:
                detections = [
                    ObjectDetectionItem(
                        detection_id=f"{image.image_id}:mock:{idx}",
                        label=label,
                        confidence=self._confidence,
                        source_provider="mock",
                        bbox=BoundingBox(
                            coordinate_space="normalized",
                            format="xywh",
                            xywh=BoundingBoxXywh(x=0.1, y=0.1, width=0.8, height=0.8),
                        ),
                    )
                    for idx, label in enumerate(self._labels)
                ]
            results.append(
                ObjectDetectionImageResult(
                    image_id=image.image_id,
                    image=ObjectDetectionImageMetadata(
                        width_px=image.width_px,
                        height_px=image.height_px,
                        frame_index=image.frame_index,
                        timestamp_ms=image.timestamp_ms,
                        sha256=image.sha256,
                    ),
                    status="succeeded",
                    detections=detections,
                )
            )
        return ObjectDetectionBatchResult(
            request_id=request.request_id,
            provider="mock",
            model_id=self._model_id,
            results=results,
            usage=ObjectDetectionUsage(
                image_count=len(request.images), provider_request_count=1
            ),
        )
