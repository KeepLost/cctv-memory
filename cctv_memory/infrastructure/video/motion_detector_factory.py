"""Motion detector selection (infrastructure/video/motion_detector_factory.py).

Config-driven composition point for choosing a ``MotionDetectorPort``
implementation by name (``pipeline.motion_scan.method``). This keeps detector
selection in infrastructure/composition, NOT in domain/application/worker logic
(ARCHITECTURE_CONSTITUTION §3 layering, §9 extensibility; pipeline-experiment
-contract §5: no ad-hoc branches in business code).

Adding a future detector (e.g. ``opencv_mog2``, ``ssim``, an ML detector) means:
register a builder here and add its per-method config — no change to
``MotionScanProcessor`` or the worker's business path, which only ever see the
``MotionDetectorPort`` abstraction.
"""

from __future__ import annotations

from collections.abc import Callable

from cctv_memory.config.settings import MotionScanSection
from cctv_memory.infrastructure.video.motion_detector import FrameDiffMotionDetector
from cctv_memory.services.motion_detector import MotionDetectorPort

# Registry: method name -> builder(MotionScanSection) -> MotionDetectorPort.
# A module-level dict so new implementations register without touching callers.
MotionDetectorBuilder = Callable[[MotionScanSection], MotionDetectorPort]


def _build_frame_diff(cfg: MotionScanSection) -> MotionDetectorPort:
    """Build the frame-difference detector from its per-method config knobs."""
    return FrameDiffMotionDetector(
        sample_fps=cfg.sample_fps,
        frame_width=cfg.frame_width,
        frame_height=cfg.frame_height,
    )


_REGISTRY: dict[str, MotionDetectorBuilder] = {
    "frame_diff": _build_frame_diff,
}


def available_methods() -> list[str]:
    """Return the registered motion detector method names (sorted)."""
    return sorted(_REGISTRY)


def register_motion_detector(method: str, builder: MotionDetectorBuilder) -> None:
    """Register a detector builder under ``method`` (future detectors / tests)."""
    if not method:
        raise ValueError("motion detector method name must be non-empty")
    _REGISTRY[method] = builder


def build_motion_detector(cfg: MotionScanSection) -> MotionDetectorPort:
    """Select and build a ``MotionDetectorPort`` from ``pipeline.motion_scan``.

    Selection is by ``cfg.method``. An unknown method is a configuration error and
    raises ``ValueError`` listing the supported methods — surfaced at composition
    BEFORE any processing, never silently falling back to a default detector
    (configuration-contract §8: invalid config fails clearly, not partially).
    """
    builder = _REGISTRY.get(cfg.method)
    if builder is None:
        supported = ", ".join(available_methods())
        raise ValueError(
            f"unknown motion detection method {cfg.method!r}; "
            f"supported methods: {supported}"
        )
    return builder(cfg)
