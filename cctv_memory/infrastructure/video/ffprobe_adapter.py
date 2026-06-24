"""ffprobe-backed VideoProcessor adapter (infrastructure/video).

Implements VideoProcessorPort. Uses ``ffprobe`` to read duration when available.
Frame extraction returns deterministic placeholder URIs (structured for later
real extraction) — the MVP mock VLM does not decode frames.

For tests that should not depend on a real media file, use
``StaticVideoProcessor`` with an injected duration.
"""

from __future__ import annotations

import json
import os
import subprocess

from cctv_memory.services.video_processor import VideoMetadata

# Bounded so a stuck ffprobe can never hang the worker. ffprobe on a local file
# returns in well under a second; 10s is a generous MVP ceiling.
_FFPROBE_TIMEOUT_SECONDS = 10
# ffmpeg frame extraction is bounded too (testing-contract §12). A handful of
# frames from one local segment is fast; 30s is a generous ceiling.
_FFMPEG_TIMEOUT_SECONDS = 30


def _segment_subdir(
    unit_key: str | None, segment_start_ms: int, segment_end_ms: int
) -> str:
    """Per-segment subdir, isolated per analysis unit when ``unit_key`` is set.

    R10/P0 (task cctv-memory-20260616-1339): the legacy layout keyed the output
    dir only by ``<start>_<end>`` window, which collides across ANY two videos
    sharing that window (the window grid is identical for all videos) — concurrent
    units would overwrite/delete each other's frames. The ``unit_key`` (the worker
    passes ``model_call_id``) makes the path unique per unit; ``None`` preserves
    the legacy layout for non-concurrent/standalone callers.
    """
    window = f"{segment_start_ms}_{segment_end_ms}"
    if unit_key:
        safe_key = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in unit_key
        )[:80]
        return os.path.join(safe_key, window)
    return window


class FfprobeVideoProcessor:
    """Probe duration via ffprobe; plan deterministic placeholder frame URIs."""

    def __init__(
        self,
        frame_root: str = "./data/frames",
        *,
        ffprobe_bin: str = "ffprobe",
        timeout_seconds: int = _FFPROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._frame_root = frame_root
        self._ffprobe_bin = ffprobe_bin
        self._timeout_seconds = timeout_seconds

    def probe(self, source_uri: str) -> VideoMetadata:
        """Return video metadata using ffprobe.

        Bounded and non-interactive: ``stdin`` is closed (DEVNULL) so ffprobe can
        never block waiting for input, and a hard ``timeout`` guarantees the call
        returns. Any failure/timeout raises ``RuntimeError`` (the worker maps it
        to ``video_decode_error`` without leaking internal paths/stderr).
        """
        try:
            result = subprocess.run(  # noqa: S603 - fixed-binary, no shell
                [
                    self._ffprobe_bin,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    source_uri,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=True,
            )
            data = json.loads(result.stdout)
            duration_s = float(data["format"]["duration"])
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffprobe timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError("ffprobe failed to read source") from exc
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("ffprobe returned no usable duration") from exc
        return VideoMetadata(duration_ms=int(duration_s * 1000))

    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        # Deterministic placeholder identifiers; no real decoding in the MVP.
        seg = _segment_subdir(unit_key, segment_start_ms, segment_end_ms)
        return [
            f"{self._frame_root}/{seg}/frame_{i:04d}.jpg"
            for i in range(frame_count)
        ]


class StaticVideoProcessor:
    """Deterministic VideoProcessor for tests (no ffprobe dependency)."""

    def __init__(self, duration_ms: int, frame_root: str = "./data/frames") -> None:
        self._duration_ms = duration_ms
        self._frame_root = frame_root

    def probe(self, source_uri: str) -> VideoMetadata:
        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        seg = _segment_subdir(unit_key, segment_start_ms, segment_end_ms)
        return [
            f"{self._frame_root}/{seg}/frame_{i:04d}.jpg"
            for i in range(frame_count)
        ]


class WholeClipVideoProcessor:
    """Real-mode opt-in processor: ffprobe duration + pass the WHOLE clip to VLM.

    Used only when ``vlm.media_input=video`` (non-default). For short videos
    (≤ one window) the entire clip is the analysis unit, so ``extract_frame_uris``
    returns a single path to the video file (the real VLM adapter base64-encodes
    that file as one video part).

    Audio is DROPPED by default (``include_audio=False``): the clip is rewritten
    once with a bounded, non-interactive ``ffmpeg -an -c:v copy`` stream copy
    (no re-encode, fast) into the frame_root and that audio-less path is returned.
    With ``include_audio=True`` the original ``source_uri`` is passed through
    unchanged. Subprocess safety (testing-contract §12): stdin=DEVNULL + bounded
    timeout + capture_output; any failure maps to ``RuntimeError``.
    """

    def __init__(
        self,
        frame_root: str = "./data/frames",
        *,
        include_audio: bool = False,
        ffmpeg_bin: str = "ffmpeg",
        timeout_seconds: int = _FFPROBE_TIMEOUT_SECONDS,
        ffmpeg_timeout_seconds: int = _FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        self._frame_root = frame_root
        self._probe = FfprobeVideoProcessor(frame_root, timeout_seconds=timeout_seconds)
        self._include_audio = include_audio
        self._ffmpeg_bin = ffmpeg_bin
        self._ffmpeg_timeout = ffmpeg_timeout_seconds

    def probe(self, source_uri: str) -> VideoMetadata:
        return self._probe.probe(source_uri)

    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        # Whole-clip mode: the VLM receives the full video file, not frames.
        if self._include_audio:
            return [source_uri]
        # Default: strip the audio track before sending the clip to the VLM.
        return [
            self._strip_audio(
                source_uri, segment_start_ms, segment_end_ms, unit_key=unit_key
            )
        ]

    def _strip_audio(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        *,
        unit_key: str | None = None,
    ) -> str:
        seg = _segment_subdir(unit_key, segment_start_ms, segment_end_ms)
        out_dir = os.path.join(self._frame_root, seg)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "clip_noaudio.mp4")
        try:
            subprocess.run(  # noqa: S603 - fixed binary, no shell
                [
                    self._ffmpeg_bin,
                    "-nostdin",
                    "-y",
                    "-i",
                    source_uri,
                    "-an",  # drop audio
                    "-c:v",
                    "copy",  # stream copy: no re-encode, fast
                    out_path,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._ffmpeg_timeout,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg audio strip timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError("ffmpeg failed to strip audio") from exc
        if not os.path.exists(out_path):
            raise RuntimeError("ffmpeg produced no audio-stripped output")
        return out_path


class SegmentFrameVideoProcessor:
    """Real per-segment frame extraction via bounded ffmpeg (C5 long-video).

    For videos longer than a single window, the default_segment planner produces
    multiple segments; this processor decodes ``frames_per_segment`` real JPEG
    frames PER segment so each segment drives its own VLM call with real frame
    inputs (instead of the placeholder paths or whole-clip behavior).

    Subprocess safety (testing-contract §12, BINDING): every ffprobe/ffmpeg call
    passes ``stdin=DEVNULL`` + an explicit bounded ``timeout`` + ``capture_output``
    and maps any failure/timeout to ``RuntimeError`` (the worker maps that to
    ``video_decode_error`` / ``frame_extraction_failed`` without leaking stderr or
    internal paths). ``frame_strategy`` selects the in-segment sampling:
    ``uniform`` (evenly spaced timestamps) is the MVP default.
    """

    def __init__(
        self,
        frame_root: str = "./data/frames",
        *,
        ffprobe_bin: str = "ffprobe",
        ffmpeg_bin: str = "ffmpeg",
        frame_strategy: str = "uniform",
        timeout_seconds: int = _FFPROBE_TIMEOUT_SECONDS,
        ffmpeg_timeout_seconds: int = _FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        self._frame_root = frame_root
        self._probe_adapter = FfprobeVideoProcessor(
            frame_root, ffprobe_bin=ffprobe_bin, timeout_seconds=timeout_seconds
        )
        self._ffmpeg_bin = ffmpeg_bin
        self._frame_strategy = frame_strategy
        self._ffmpeg_timeout = ffmpeg_timeout_seconds

    def probe(self, source_uri: str) -> VideoMetadata:
        return self._probe_adapter.probe(source_uri)

    def _frame_timestamps_ms(
        self, segment_start_ms: int, segment_end_ms: int, frame_count: int
    ) -> list[int]:
        """Plan in-segment frame timestamps (ms). Deterministic.

        ``uniform`` spaces ``frame_count`` samples evenly across the segment,
        biased to the segment interior so the first/last frames are not exactly on
        the boundaries. Any unknown strategy falls back to uniform.
        """
        if frame_count <= 0 or segment_end_ms <= segment_start_ms:
            return []
        span = segment_end_ms - segment_start_ms
        if frame_count == 1:
            return [segment_start_ms + span // 2]
        step = span / (frame_count + 1)
        return [int(segment_start_ms + step * (i + 1)) for i in range(frame_count)]

    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        """Decode real frames for one segment; return the written JPEG paths.

        Each frame is extracted with a bounded, non-interactive ffmpeg seek. If a
        single frame fails to decode the whole call raises ``RuntimeError`` (no
        partial/ambiguous result) so the worker can fail the unit cleanly.
        """
        timestamps = self._frame_timestamps_ms(
            segment_start_ms, segment_end_ms, frame_count
        )
        if not timestamps:
            return []
        seg = _segment_subdir(unit_key, segment_start_ms, segment_end_ms)
        out_dir = os.path.join(self._frame_root, seg)
        os.makedirs(out_dir, exist_ok=True)
        uris: list[str] = []
        for i, ts_ms in enumerate(timestamps):
            out_path = os.path.join(out_dir, f"frame_{i:04d}.jpg")
            self._extract_one(source_uri, ts_ms, out_path)
            uris.append(out_path)
        return uris

    def _extract_one(self, source_uri: str, timestamp_ms: int, out_path: str) -> None:
        seek = f"{timestamp_ms / 1000.0:.3f}"
        try:
            subprocess.run(  # noqa: S603 - fixed binary, no shell
                [
                    self._ffmpeg_bin,
                    "-nostdin",
                    "-y",
                    "-ss",
                    seek,
                    "-i",
                    source_uri,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    out_path,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._ffmpeg_timeout,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg frame extraction timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError("ffmpeg failed to extract frame") from exc
        if not os.path.exists(out_path):
            raise RuntimeError("ffmpeg produced no frame output")

