"""Unit tests for multi-scale domain planners + scale-aware prompts.

Pure/deterministic — no subprocess, no DB. Covers:
- plan_motion_triggers distinguishes motion vs no-motion sample series;
- plan_motion_sample_timestamps spacing;
- plan_high_freq_windows short-window planning;
- build_prompt / prompt_version_for_scale differ by analysis_scale.
"""

from __future__ import annotations

import pytest
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.domain.policies import (
    MotionSample,
    plan_high_freq_windows,
    plan_motion_sample_timestamps,
    plan_motion_triggers,
)
from cctv_memory.infrastructure.vlm.prompts import (
    build_prompt,
    prompt_version_for_scale,
)


def test_plan_motion_sample_timestamps_spacing() -> None:
    ts = plan_motion_sample_timestamps(5_000, sample_interval_ms=1000)
    assert ts == [0, 1000, 2000, 3000, 4000]
    assert plan_motion_sample_timestamps(0, sample_interval_ms=1000) == []


def test_plan_motion_sample_timestamps_rejects_bad_interval() -> None:
    with pytest.raises(ValueError):
        plan_motion_sample_timestamps(5_000, sample_interval_ms=0)


def test_plan_motion_triggers_detects_motion_window() -> None:
    # A burst of high motion in the middle of an otherwise quiet series.
    samples = [
        MotionSample(0, 0.05),
        MotionSample(500, 0.05),
        MotionSample(1000, 0.8),
        MotionSample(1500, 0.85),
        MotionSample(2000, 0.9),
        MotionSample(2500, 0.05),
        MotionSample(3000, 0.04),
    ]
    triggers = plan_motion_triggers(
        samples, threshold=0.4, min_duration_ms=500, merge_gap_ms=600
    )
    assert len(triggers) == 1
    t = triggers[0]
    assert t.start_ms == 1000
    assert t.end_ms >= 2000  # spans the high-motion run
    assert t.peak_score == pytest.approx(0.9)
    assert t.reason == "motion_spike"


def test_plan_motion_triggers_no_motion_yields_nothing() -> None:
    # A quiet series (static scene) produces NO triggers.
    samples = [MotionSample(i * 500, 0.02) for i in range(10)]
    triggers = plan_motion_triggers(
        samples, threshold=0.4, min_duration_ms=500, merge_gap_ms=600
    )
    assert triggers == []


def test_plan_motion_triggers_merges_close_runs() -> None:
    samples = [
        MotionSample(0, 0.9),
        MotionSample(500, 0.1),  # brief dip
        MotionSample(1000, 0.9),
        MotionSample(1500, 0.1),
    ]
    # merge_gap large enough to join the two spikes into one window.
    triggers = plan_motion_triggers(
        samples, threshold=0.4, min_duration_ms=200, merge_gap_ms=1000
    )
    assert len(triggers) == 1


def test_plan_motion_triggers_short_blip_extended_to_min_duration() -> None:
    samples = [MotionSample(0, 0.1), MotionSample(1000, 0.9), MotionSample(1100, 0.1)]
    triggers = plan_motion_triggers(
        samples, threshold=0.4, min_duration_ms=2000, merge_gap_ms=0
    )
    assert len(triggers) == 1
    assert triggers[0].end_ms - triggers[0].start_ms >= 2000


def test_plan_high_freq_windows_splits_trigger() -> None:
    windows = plan_high_freq_windows(0, 9_000, window_seconds=3, overlap_ratio=0.5)
    assert windows[0].start_ms == 0
    assert windows[0].end_ms == 3_000
    assert windows[-1].end_ms == 9_000
    assert len(windows) >= 3


def test_plan_high_freq_windows_short_trigger_single_window() -> None:
    windows = plan_high_freq_windows(1000, 2500, window_seconds=3, overlap_ratio=0.5)
    assert windows == [type(windows[0])(start_ms=1000, end_ms=2500)]


def test_plan_high_freq_windows_rejects_bad_overlap() -> None:
    with pytest.raises(ValueError):
        plan_high_freq_windows(0, 9000, window_seconds=3, overlap_ratio=1.0)


def test_prompt_is_scale_aware() -> None:
    default_prompt = build_prompt(scale=AnalysisScale.DEFAULT_SEGMENT)
    high_freq_prompt = build_prompt(scale=AnalysisScale.HIGH_FREQ_EVENT)
    assert default_prompt != high_freq_prompt
    # high_freq prompt foregrounds the dynamic event; default foregrounds static.
    assert "dynamic" in high_freq_prompt and "事件" in high_freq_prompt
    assert "static" in default_prompt
    # Both emit the slim schema and forbid policy/security fields.
    for prompt in (default_prompt, high_freq_prompt):
        assert "attr" in prompt and "alert" in prompt
        assert "access_policy_id" in prompt  # negative instruction
        assert "schema_version" not in prompt
    assert prompt_version_for_scale(AnalysisScale.DEFAULT_SEGMENT) == "default_segment_v3"
    assert prompt_version_for_scale(AnalysisScale.HIGH_FREQ_EVENT) == "high_freq_event_v3"
    # Unknown/non-VLM scale falls back to default_segment version.
    assert prompt_version_for_scale(AnalysisScale.MOTION_SCAN) == "default_segment_v3"


def test_prompt_strict_suffix_applies_per_scale() -> None:
    base = build_prompt(scale=AnalysisScale.HIGH_FREQ_EVENT)
    strict = build_prompt(scale=AnalysisScale.HIGH_FREQ_EVENT, strict=True)
    assert strict.startswith(base)
    assert len(strict) > len(base)
