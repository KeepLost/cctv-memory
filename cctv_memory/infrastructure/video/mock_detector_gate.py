"""Deterministic mock detector gate adapter."""

from __future__ import annotations

from cctv_memory.services.detector_gate import (
    Detection,
    DetectorFrameInput,
    DetectorFrameResult,
    DetectorGatePort,
)


class MockDetectorGate(DetectorGatePort):
    """Configurable deterministic detector for tests and local dry runs."""

    def __init__(
        self,
        *,
        positive_labels: list[str] | None = None,
        positive_frame_ratio: float = 0.0,
        confidence: float = 0.9,
    ) -> None:
        self._labels = list(positive_labels or [])
        self._ratio = max(0.0, min(1.0, float(positive_frame_ratio)))
        self._confidence = max(0.0, min(1.0, float(confidence)))

    def detect_frames(self, frames: list[DetectorFrameInput]) -> list[DetectorFrameResult]:
        if not frames:
            return []
        positive_count = int(round(len(frames) * self._ratio))
        results: list[DetectorFrameResult] = []
        for i, frame in enumerate(frames):
            detections: list[Detection] = []
            if i < positive_count:
                detections = [
                    Detection(
                        label=label,
                        confidence=self._confidence,
                        bbox=[0.1, 0.1, 0.9, 0.9],
                    )
                    for label in self._labels
                ]
            results.append(DetectorFrameResult(frame=frame, detections=detections))
        return results
