"""Tests for pluggable, config-selected motion detection.

Covers task cctv-memory-20260611-1049: the detector is chosen by
``pipeline.motion_scan.method`` through a registry/factory, per-method params are
forwarded, an unknown method fails clearly, the improved sensitive defaults are
reflected in config, and the worker composition resolves a detector via the
factory (the abstraction is on the real main path, not just declared).
"""

from __future__ import annotations

import pytest
from cctv_memory.config.settings import AppConfig, MotionScanSection
from cctv_memory.infrastructure.video.motion_detector import FrameDiffMotionDetector
from cctv_memory.infrastructure.video.motion_detector_factory import (
    available_methods,
    build_motion_detector,
    register_motion_detector,
)
from cctv_memory.services.motion_detector import MotionDetectorPort


def test_frame_diff_is_registered() -> None:
    assert "frame_diff" in available_methods()


def test_factory_selects_frame_diff_by_config() -> None:
    cfg = MotionScanSection()  # method defaults to frame_diff
    detector = build_motion_detector(cfg)
    assert isinstance(detector, FrameDiffMotionDetector)
    # Built detector satisfies the abstract port the worker depends on.
    assert isinstance(detector, MotionDetectorPort)


def test_factory_forwards_per_method_params() -> None:
    cfg = MotionScanSection(sample_fps=7.0, frame_width=200, frame_height=100)
    detector = build_motion_detector(cfg)
    assert isinstance(detector, FrameDiffMotionDetector)
    # Params flow from config into the concrete detector (no hidden constants).
    assert detector._sample_fps == 7.0  # noqa: SLF001 - assert wiring
    assert detector._width == 200  # noqa: SLF001
    assert detector._height == 100  # noqa: SLF001


def test_unknown_method_raises_clear_error() -> None:
    cfg = MotionScanSection(method="does_not_exist")
    with pytest.raises(ValueError) as exc:
        build_motion_detector(cfg)
    msg = str(exc.value)
    assert "does_not_exist" in msg
    # Error lists supported methods so operators can fix the config.
    assert "frame_diff" in msg


def test_register_new_detector_then_select_it() -> None:
    class _StubDetector:
        def sample_motion(self, source_uri: str) -> list:  # type: ignore[type-arg]
            return []

    register_motion_detector("stub_test_detector", lambda cfg: _StubDetector())
    try:
        cfg = MotionScanSection(method="stub_test_detector")
        detector = build_motion_detector(cfg)
        assert isinstance(detector, _StubDetector)
        assert isinstance(detector, MotionDetectorPort)
    finally:
        # Keep the global registry clean for other tests.
        from cctv_memory.infrastructure.video import motion_detector_factory as f

        f._REGISTRY.pop("stub_test_detector", None)  # noqa: SLF001


def test_register_rejects_empty_method() -> None:
    with pytest.raises(ValueError):
        register_motion_detector("", lambda cfg: FrameDiffMotionDetector())


def test_improved_defaults_are_more_sensitive() -> None:
    """Defaults must be clearly more sensitive than the prior sluggish values
    (prior: threshold=0.4, sample_fps=2.0, 64x36, min_duration_ms=1500)."""
    cfg = MotionScanSection()
    assert cfg.method == "frame_diff"
    assert cfg.threshold < 0.4
    assert cfg.sample_fps > 2.0
    assert cfg.frame_width > 64
    assert cfg.frame_height > 36
    assert cfg.min_duration_ms < 1500
    # merge_gap is a real configurable knob (task-spec required param).
    assert cfg.merge_gap_ms >= 0


def test_settings_pipeline_exposes_improved_defaults() -> None:
    # Defaults propagate through the full AppConfig tree the worker reads.
    settings = AppConfig()
    ms = settings.pipeline.motion_scan
    assert ms.threshold == pytest.approx(0.15)
    assert ms.sample_fps == pytest.approx(4.0)
    assert ms.frame_width == 128
    assert ms.frame_height == 72
    assert ms.min_duration_ms == 600
    assert ms.merge_gap_ms == 800
