"""Abstract detector gate service port."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: list[float] = field(default_factory=list)
    bbox_format: str = "xyxy_normalized"


@dataclass(frozen=True)
class DetectorFrameInput:
    uri: str
    frame_index: int | None
    timestamp_ms: int
    frame_hash: str | None = None


@dataclass(frozen=True)
class DetectorFrameResult:
    frame: DetectorFrameInput
    detections: list[Detection] = field(default_factory=list)


@runtime_checkable
class DetectorGatePort(Protocol):
    """Per-frame lightweight detector used before optional VLM enrichment."""

    def detect_frames(self, frames: list[DetectorFrameInput]) -> list[DetectorFrameResult]:
        """Return detections for ordered frame inputs without persisting media bytes."""
        ...
