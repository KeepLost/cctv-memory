"""Tests for the frame-difference motion detector.

Unit test ``score_frames`` with synthetic raw gray frames (no subprocess), then
ffmpeg-gated integration tests proving the detector distinguishes a MOVING clip
(ffmpeg ``testsrc``, animated) from a STATIC clip (single solid color), per
task-spec requirement "distinguish motion vs no-motion using generated media".
All ffmpeg usage is bounded (stdin=DEVNULL + timeout), testing-contract §12.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest
from cctv_memory.domain.policies import MotionSample
from cctv_memory.infrastructure.video.motion_detector import FrameDiffMotionDetector


def _reference_score_frames(
    frames: list[bytes], width: int, height: int, sample_fps: float
) -> list[MotionSample]:
    """Frozen pure-Python reference (the pre-NumPy semantics).

    The optimized ``score_frames`` MUST stay byte-exactly equal to this on every
    input: per-frame ``sum(|a-b|)`` over ``n=min(len(prev),len(cur),frame_size)``
    bytes, divided by ``n*255.0``, clamped with ``min(1.0, .)``; timestamp is
    ``int(round(i / sample_fps * 1000.0))`` (banker's rounding preserved).
    """
    frame_size = width * height
    if frame_size == 0 or len(frames) < 2:
        return []
    samples: list[MotionSample] = []
    prev = frames[0]
    for i in range(1, len(frames)):
        cur = frames[i]
        n = min(len(prev), len(cur), frame_size)
        if n == 0:
            score = 0.0
        else:
            total = 0
            for a, b in zip(prev[:n], cur[:n], strict=False):
                total += a - b if a >= b else b - a
            score = total / (n * 255.0)
        timestamp_ms = int(round(i / sample_fps * 1000.0))
        samples.append(MotionSample(timestamp_ms=timestamp_ms, score=min(1.0, score)))
        prev = cur
    return samples


def _assert_exact(got: list[MotionSample], expected: list[MotionSample]) -> None:
    assert len(got) == len(expected)
    for g, e in zip(got, expected, strict=True):
        assert g.timestamp_ms == e.timestamp_ms
        # Byte-exact: integer abs-diff sum / identical float division -> identical float.
        assert g.score == e.score


def test_score_frames_zero_for_identical_frames() -> None:
    # Two identical gray frames -> no change -> score 0.
    frame = bytes([128] * (4 * 4))
    samples = FrameDiffMotionDetector.score_frames([frame, frame], 4, 4, sample_fps=2.0)
    assert len(samples) == 1
    assert samples[0].score == 0.0
    assert samples[0].timestamp_ms == 500  # frame 1 at 2fps


def test_score_frames_high_for_opposite_frames() -> None:
    black = bytes([0] * (4 * 4))
    white = bytes([255] * (4 * 4))
    samples = FrameDiffMotionDetector.score_frames([black, white], 4, 4, sample_fps=2.0)
    assert len(samples) == 1
    assert samples[0].score == pytest.approx(1.0)


def test_score_frames_needs_two_frames() -> None:
    frame = bytes([10] * 16)
    assert FrameDiffMotionDetector.score_frames([frame], 4, 4, sample_fps=2.0) == []
    assert FrameDiffMotionDetector.score_frames([], 4, 4, sample_fps=2.0) == []


def test_score_frames_zero_frame_size_returns_empty() -> None:
    frame = bytes([10] * 16)
    assert FrameDiffMotionDetector.score_frames([frame, frame], 0, 4, sample_fps=2.0) == []
    assert FrameDiffMotionDetector.score_frames([frame, frame], 4, 0, sample_fps=2.0) == []


def test_score_frames_equiv_known_partial_diff() -> None:
    # 2x2 frame, only some pixels differ, asserts exact normalized value.
    a = bytes([10, 20, 30, 40])
    b = bytes([10, 25, 20, 40])  # diffs: 0,5,10,0 -> total 15
    got = FrameDiffMotionDetector.score_frames([a, b], 2, 2, sample_fps=4.0)
    assert got == _reference_score_frames([a, b], 2, 2, sample_fps=4.0)
    assert got[0].score == 15 / (4 * 255.0)


def test_score_frames_equiv_ragged_lengths() -> None:
    # len(prev) != len(cur) and both differ from frame_size -> n = min(...).
    a = bytes([5, 5, 5, 5, 5, 5])  # len 6
    b = bytes([9, 9, 9])  # len 3
    # frame_size = 4 -> n = min(6, 3, 4) = 3 ; only first 3 bytes compared.
    got = FrameDiffMotionDetector.score_frames([a, b], 2, 2, sample_fps=5.0)
    assert got == _reference_score_frames([a, b], 2, 2, sample_fps=5.0)
    assert got[0].score == (4 + 4 + 4) / (3 * 255.0)


def test_score_frames_equiv_n_zero_when_a_frame_empty() -> None:
    # An empty frame in the middle -> n == 0 -> score 0.0 for affected pairs.
    a = bytes([100] * 4)
    empty = b""
    c = bytes([200] * 4)
    frames = [a, empty, c]
    got = FrameDiffMotionDetector.score_frames(frames, 2, 2, sample_fps=2.0)
    assert got == _reference_score_frames(frames, 2, 2, sample_fps=2.0)
    assert got[0].score == 0.0  # a vs empty -> n=0
    assert got[1].score == 0.0  # empty vs c -> n=0


def test_score_frames_equiv_clamps_above_one() -> None:
    # If a frame is longer than frame_size but n is capped at frame_size, score
    # stays normalized by n*255; ensure the min(1.0, .) clamp path is exercised.
    black = bytes([0] * 4)
    white = bytes([255] * 4)
    got = FrameDiffMotionDetector.score_frames([black, white], 2, 2, sample_fps=1.0)
    assert got == _reference_score_frames([black, white], 2, 2, sample_fps=1.0)
    assert got[0].score == 1.0


def test_score_frames_equiv_random_fuzz() -> None:
    # Byte-exact equivalence vs the frozen reference across many random cases,
    # including non-square dims and odd fps (timestamp rounding).
    rng = random.Random(20260616)
    for _ in range(200):
        w = rng.randint(1, 7)
        h = rng.randint(1, 7)
        frame_size = w * h
        n_frames = rng.randint(2, 6)
        fps = rng.choice([1.0, 2.0, 3.0, 4.0, 5.0, 7.5, 30.0])
        frames = [
            bytes(rng.randrange(256) for _ in range(frame_size)) for _ in range(n_frames)
        ]
        got = FrameDiffMotionDetector.score_frames(frames, w, h, fps)
        exp = _reference_score_frames(frames, w, h, fps)
        _assert_exact(got, exp)


def test_score_frames_equiv_full_size_max_diff() -> None:
    # Default detector dims (128x72) at the extreme (all 0 vs all 255) — exercises
    # the wide-sum path and confirms exact 1.0 with no integer overflow.
    w, h = 128, 72
    black = bytes([0] * (w * h))
    white = bytes([255] * (w * h))
    got = FrameDiffMotionDetector.score_frames([black, white], w, h, sample_fps=4.0)
    assert got == _reference_score_frames([black, white], w, h, sample_fps=4.0)
    assert got[0].score == 1.0



def test_detector_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        FrameDiffMotionDetector(sample_fps=0)
    with pytest.raises(ValueError):
        FrameDiffMotionDetector(frame_width=0)


def _ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_moving_clip(out: Path, *, duration: int = 4) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc2=duration={duration}:size=160x120:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=True,
    )
    return out


def _make_static_clip(out: Path, *, duration: int = 4) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=gray:size=160x120:duration={duration}:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=True,
    )
    return out


@pytest.mark.skipif(not _ffmpeg(), reason="ffmpeg not available")
def test_detector_distinguishes_motion_from_static(tmp_path: Path) -> None:
    moving = _make_moving_clip(tmp_path / "moving.mp4")
    static = _make_static_clip(tmp_path / "static.mp4")
    det = FrameDiffMotionDetector(sample_fps=5.0)

    moving_samples = det.sample_motion(str(moving))
    static_samples = det.sample_motion(str(static))

    assert moving_samples, "expected motion samples for the animated clip"
    assert static_samples, "expected samples for the static clip too"

    peak_moving = max(s.score for s in moving_samples)
    peak_static = max(s.score for s in static_samples)
    # The animated testsrc2 has substantial inter-frame change; the solid color
    # clip is (near) zero. The moving clip's peak must clearly exceed the static.
    assert peak_moving > 0.03
    assert peak_static < 0.01
    assert peak_moving > peak_static * 5


@pytest.mark.skipif(not _ffmpeg(), reason="ffmpeg not available")
def test_detector_missing_source_raises_bounded(tmp_path: Path) -> None:
    det = FrameDiffMotionDetector()
    with pytest.raises(RuntimeError):
        det.sample_motion("/nonexistent/not-a-video.mp4")
