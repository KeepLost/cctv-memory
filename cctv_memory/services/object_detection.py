"""Object detection service port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.contracts.object_detection import (
    ObjectDetectionBatchRequest,
    ObjectDetectionBatchResult,
)


@runtime_checkable
class ObjectDetectionPort(Protocol):
    """Provider-neutral object detection boundary used by gate runners."""

    def detect_objects(
        self,
        request: ObjectDetectionBatchRequest,
    ) -> ObjectDetectionBatchResult:
        """Detect objects for an ordered image batch."""
        ...
