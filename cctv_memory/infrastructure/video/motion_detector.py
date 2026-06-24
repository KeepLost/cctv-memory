"""Frame-difference motion detector (infrastructure/video/motion_detector.py).

Implements ``MotionDetectorPort`` using a SINGLE bounded, non-interactive ffmpeg
call that decodes the whole clip into downscaled grayscale raw frames at a low
sampling fps. Consecutive frames are compared with a normalized mean-absolute
pixel difference (NumPy-vectorized — the per-pixel math runs in C and releases
the GIL, so concurrent motion scans no longer serialize on one CPU-bound Python
loop), yielding a [0,1] motion score per sampled instant. This is real motion
detection (``method=frame_diff``, pipeline-experiment-contract §2.3), not a
placeholder constant.

Subprocess safety (testing-contract §12, BINDING): ``stdin=DEVNULL`` + explicit
bounded ``timeout`` + ``capture_output``; any failure/timeout maps to
``RuntimeError`` without leaking stderr or internal paths. The downscale
(default 128x72) and low fps keep memory/CPU tiny and the output bounded.
"""

from __future__ import annotations

import subprocess

import numpy as np

from cctv_memory.domain.policies import MotionSample

# Bounded so a stuck ffmpeg can never hang the worker. A short clip decoded at a
# low fps to a tiny size returns quickly; 60s is a generous ceiling for the
# whole-clip single pass.
_FFMPEG_TIMEOUT_SECONDS = 60


class FrameDiffMotionDetector:
    """Motion detector via downscaled grayscale frame differencing (bounded)."""

    def __init__(
        self,
        *,
        sample_fps: float = 4.0,
        frame_width: int = 128,
        frame_height: int = 72,
        ffmpeg_bin: str = "ffmpeg",
        timeout_seconds: int = _FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("frame dimensions must be positive")
        self._sample_fps = sample_fps
        self._width = frame_width
        self._height = frame_height
        self._ffmpeg_bin = ffmpeg_bin
        self._timeout = timeout_seconds

    def sample_motion(self, source_uri: str) -> list[MotionSample]:
        """Decode the clip to raw gray frames and score inter-frame change."""
        raw = self._decode_gray_frames(source_uri)
        frame_size = self._width * self._height
        if frame_size == 0:
            return []
        num_frames = len(raw) // frame_size
        if num_frames < 2:
            # Not enough frames to measure change (e.g. extremely short clip).
            return []
        frames = [
            raw[i * frame_size : (i + 1) * frame_size] for i in range(num_frames)
        ]
        return self.score_frames(frames, self._width, self._height, self._sample_fps)

    @staticmethod
    def score_frames(
        frames: list[bytes], width: int, height: int, sample_fps: float
    ) -> list[MotionSample]:
        """Compute normalized mean-abs-diff samples from raw gray frames.

        Separated from decoding so it can be unit-tested with synthetic frames and
        no subprocess. The score for frame ``i`` (i>=1) is the mean absolute pixel
        difference vs. frame ``i-1`` divided by 255, in [0,1]; its timestamp is the
        frame's position at ``sample_fps``.

        Vectorized with NumPy: for each adjacent pair only the first
        ``n = min(len(prev), len(cur), frame_size)`` bytes are compared (preserving
        the original ragged-length behavior), and the absolute difference is summed
        on a signed integer view so the result is BYTE-EXACTLY identical to the
        prior pure-Python ``sum(|a-b|)`` — but the heavy per-pixel work happens in
        C with the GIL released, so concurrent motion scans run in parallel.
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
                # int16 view avoids uint8 wraparound; abs-diff sum is the SAME
                # integer the pure-Python loop produced, so score is bit-identical.
                a = np.frombuffer(prev, dtype=np.uint8, count=n).astype(np.int16)
                b = np.frombuffer(cur, dtype=np.uint8, count=n).astype(np.int16)
                total = int(np.abs(a - b).sum())
                score = total / (n * 255.0)
            timestamp_ms = int(round(i / sample_fps * 1000.0))
            samples.append(
                MotionSample(timestamp_ms=timestamp_ms, score=min(1.0, score))
            )
            prev = cur
        return samples

    def _decode_gray_frames(self, source_uri: str) -> bytes:
        vf = f"fps={self._sample_fps},scale={self._width}:{self._height},format=gray"
        try:
            result = subprocess.run(  # noqa: S603 - fixed binary, no shell
                [
                    self._ffmpeg_bin,
                    "-nostdin",
                    "-i",
                    source_uri,
                    "-vf",
                    vf,
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "gray",
                    "-",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg motion sampling timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError("ffmpeg failed to sample motion") from exc
        return result.stdout
