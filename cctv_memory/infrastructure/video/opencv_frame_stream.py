"""OpenCV streaming-decode FrameStream adapter (infrastructure/video).

Implements both ``VideoProcessorPort`` (probe + ``extract_frame_uris``) and
``FrameStreamPort`` (``extract_selected_frames``). This is the DEFAULT decode
backend (``pipeline.decode_backend=opencv``).

How it works (frame-stream-selector-cache-design §2/§3/§4):
1. Decode the segment ``[start, end)`` ONCE with ``cv2.VideoCapture`` at
   ``sample_fps`` (no per-frame ffmpeg subprocess seeks).
2. Score every sampled frame on a downscaled grayscale copy, producing ONLY
   scalar metrics (motion / scene / blur / brightness). Raw frames are held in a
   BOUNDED ring buffer (``buffer_seconds`` x ``sample_fps`` frames, capped again
   by ``max_buffer_bytes``); evicted frames are explicitly dereferenced.
3. A pure domain selector (``domain.policies.select_frames``) picks ``frame_count``
   frames by strategy, preserving temporal coverage and chronological order.
4. Selected frames are JPEG-encoded and written atomically (temp + rename). A
   frame that has been evicted from the ring buffer is re-decoded via a single
   bounded re-seek pass.

Memory discipline (design §2.3 — the #1 risk): raw ``np.ndarray`` frames live
ONLY inside this adapter and the ring buffer; nothing outside ever receives a
pixel array. The ring buffer is cleared in a ``finally`` block and every
``VideoCapture`` is released on success AND failure.

Fallback: if OpenCV is unavailable or decode fails and
``decode_fallback_to_ffmpeg`` is set, delegate to the legacy
``SegmentFrameVideoProcessor`` so the pipeline degrades cleanly.
"""

from __future__ import annotations

import os
import tempfile
from collections import deque
from collections.abc import Iterator
from typing import Any

from cctv_memory.domain.exceptions import InsufficientFramesError
from cctv_memory.domain.policies import FrameScore, select_frames
from cctv_memory.infrastructure.video.ffprobe_adapter import (
    FfprobeVideoProcessor,
    SegmentFrameVideoProcessor,
)
from cctv_memory.infrastructure.video.opencv_import import (
    OpenCvImportError,
    cv2_available,
    import_cv2,
)
from cctv_memory.services.frame_stream import SelectedFrame
from cctv_memory.services.video_processor import VideoMetadata

_DECODE_BACKEND = "opencv"

# cv2 is an untyped vendor boundary (constitution §4): raw frames are numpy
# ndarrays and VideoCapture is a C-extension handle. We type them as ``Any``
# locally so they never leak as a structural type across the module boundary —
# only scalar ``FrameScore`` / ``SelectedFrame`` DTOs ever leave this adapter.
_Frame = Any
_Capture = Any


def _parse_scale(scale: str) -> tuple[int, int]:
    """Parse ``"WxH"`` into ``(width, height)``; raise on malformed values."""
    try:
        w_str, h_str = scale.lower().split("x", 1)
        width, height = int(w_str), int(h_str)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid scoring_scale {scale!r} (expected WxH)") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"scoring_scale dimensions must be positive: {scale!r}")
    return width, height


class _RingBuffer:
    """Bounded FIFO of recent raw frames keyed by frame_index (design §2.1/§10).

    Caps by BOTH frame count (``maxlen``) and total bytes (``max_bytes``). On
    eviction the stored array reference is dropped so Python can reclaim it
    immediately (closing reference-retention trap #1).
    """

    def __init__(self, maxlen: int, max_bytes: int) -> None:
        self._maxlen = max(1, maxlen)
        self._max_bytes = max(1, max_bytes)
        self._dq: deque[tuple[int, _Frame]] = deque()
        self._bytes = 0

    def append(self, frame_index: int, arr: _Frame) -> None:
        nbytes = int(getattr(arr, "nbytes", 0))
        while self._dq and (
            len(self._dq) >= self._maxlen or self._bytes + nbytes > self._max_bytes
        ):
            _old_idx, old_arr = self._dq.popleft()
            self._bytes -= int(getattr(old_arr, "nbytes", 0))
            del old_arr  # explicit dereference of the evicted frame
        self._dq.append((frame_index, arr))
        self._bytes += nbytes

    def get(self, frame_index: int) -> _Frame | None:
        for idx, arr in self._dq:
            if idx == frame_index:
                return arr
        return None

    @property
    def current_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._dq)

    def clear(self) -> None:
        self._dq.clear()
        self._bytes = 0


class OpenCvFrameStreamVideoProcessor:
    """Streaming OpenCV decode + bounded ring buffer + metric frame selection."""

    def __init__(
        self,
        frame_root: str = "./data/frames",
        *,
        sample_fps: float = 8.0,
        buffer_seconds: float = 4.0,
        max_buffer_bytes: int = 268_435_456,
        scoring_scale: str = "320x180",
        selection_strategy: str = "bins_then_score",
        selected_jpeg_quality: int = 80,
        w_motion: float = 1.0,
        w_scene: float = 0.5,
        w_quality: float = 0.5,
        min_blur: float = 50.0,
        decode_fallback_to_ffmpeg: bool = True,
        ffprobe_bin: str = "ffprobe",
        ffmpeg_bin: str = "ffmpeg",
        frame_strategy: str = "uniform",
    ) -> None:
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        self._frame_root = frame_root
        self._sample_fps = sample_fps
        self._buffer_seconds = buffer_seconds
        self._max_buffer_bytes = max_buffer_bytes
        self._scoring_w, self._scoring_h = _parse_scale(scoring_scale)
        self._strategy = selection_strategy
        self._jpeg_quality = selected_jpeg_quality
        self._w_motion = w_motion
        self._w_scene = w_scene
        self._w_quality = w_quality
        self._min_blur = min_blur
        self._fallback_enabled = decode_fallback_to_ffmpeg
        self._ffmpeg_bin = ffmpeg_bin
        self._probe_adapter = FfprobeVideoProcessor(frame_root, ffprobe_bin=ffprobe_bin)
        self._frame_strategy = frame_strategy
        # Built lazily only if fallback is actually needed.
        self._ffmpeg_processor: SegmentFrameVideoProcessor | None = None

    # ------------------------------------------------------------------ probe
    def probe(self, source_uri: str) -> VideoMetadata:
        return self._probe_adapter.probe(source_uri)

    # ----------------------------------------------- VideoProcessorPort shape
    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        """Return selected-frame paths (timestamp-ascending) — list[str] shape.

        Keeps the ``VideoProcessorPort`` contract so callers that only need paths
        (and the VLM adapter) are unchanged.
        """
        return [
            sf.uri
            for sf in self.extract_selected_frames(
                source_uri, segment_start_ms, segment_end_ms, frame_count,
                unit_key=unit_key,
            )
        ]

    # ----------------------------------------------------- FrameStreamPort API
    def extract_selected_frames(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[SelectedFrame]:
        if frame_count <= 0 or segment_end_ms <= segment_start_ms:
            return []
        if not cv2_available():
            return self._fallback_or_raise(
                source_uri, segment_start_ms, segment_end_ms, frame_count,
                reason="opencv/numpy not importable", unit_key=unit_key,
            )
        try:
            return self._extract_with_opencv(
                source_uri, segment_start_ms, segment_end_ms, frame_count,
                unit_key=unit_key,
            )
        except OpenCvImportError as exc:
            # A cv2 import/bootstrap failure (e.g. the cold-start ``__spec__ is
            # None`` race, should it ever slip past the central serialized import)
            # is treated like any other decode failure: fall back to ffmpeg (with
            # an honest backend tag) rather than failing the unit. Distinct from
            # RuntimeError only for clarity; both route here.
            return self._fallback_or_raise(
                source_uri, segment_start_ms, segment_end_ms, frame_count,
                reason=f"opencv import/bootstrap failed: {exc}", unit_key=unit_key,
            )
        except RuntimeError as exc:
            return self._fallback_or_raise(
                source_uri, segment_start_ms, segment_end_ms, frame_count,
                reason=str(exc), unit_key=unit_key,
            )

    # ----------------------------------------------------------- opencv core
    def _extract_with_opencv(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[SelectedFrame]:
        cv2, _np = import_cv2()  # process-wide thread-safe one-time import

        out_dir = self._segment_dir(
            source_uri, segment_start_ms, segment_end_ms, unit_key=unit_key
        )
        os.makedirs(out_dir, exist_ok=True)

        maxlen = max(1, int(round(self._buffer_seconds * self._sample_fps)))
        ring = _RingBuffer(maxlen=maxlen, max_bytes=self._max_buffer_bytes)
        scores: list[FrameScore] = []

        cap = cv2.VideoCapture(source_uri)
        prev_small: _Frame | None = None  # call-local: safe under concurrency
        try:
            if not cap.isOpened():
                raise RuntimeError("opencv could not open source")
            for frame_index, ts_ms, frame in self._iter_sampled_frames(
                cap, segment_start_ms, segment_end_ms
            ):
                score, prev_small = self._score_frame(
                    frame_index, ts_ms, frame, prev_small
                )
                scores.append(score)
                ring.append(frame_index, frame)
            if not scores:
                # Zero decodable frames in this window (e.g. a near-EOF / out-of-range
                # segment). This is an EXPECTED non-failure condition: signal it as
                # InsufficientFramesError so the worker marks the unit
                # skipped(insufficient_frames) rather than failing (task
                # cctv-memory-20260612-1854). It is NOT caught by the RuntimeError
                # fallback path — a genuine decode/open error still raises RuntimeError.
                raise InsufficientFramesError("opencv decoded no frames in segment")

            selected = select_frames(
                scores,
                frame_count,
                strategy=self._strategy,
                w_motion=self._w_motion,
                w_scene=self._w_scene,
                w_quality=self._w_quality,
                min_blur=self._min_blur,
            )
            return self._materialize(source_uri, out_dir, selected, ring, cap)
        finally:
            cap.release()
            ring.clear()  # drop every retained ndarray reference (design §2.3)

    def _iter_sampled_frames(
        self, cap: _Capture, start_ms: int, end_ms: int
    ) -> Iterator[tuple[int, int, _Frame]]:
        """Yield ``(frame_index, timestamp_ms, frame)`` sampled at sample_fps.

        Streams sequentially from ``start_ms``; keeps a frame only once the real
        stream timestamp has advanced by at least one sampling interval. Stops at
        ``end_ms`` or end-of-stream. ``frame`` is a raw BGR ndarray (infra-only).
        """
        cv2, _np = import_cv2()

        interval_ms = 1000.0 / self._sample_fps
        cap.set(cv2.CAP_PROP_POS_MSEC, float(start_ms))
        frame_index = 0
        next_sample_ms = float(start_ms)
        # Guard against malformed streams: bound the read loop generously.
        max_reads = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            if pos_ms <= 0:
                # Some containers report 0 before advancing; synthesize a clock.
                pos_ms = next_sample_ms
            if pos_ms >= end_ms:
                break
            if pos_ms + 1e-6 >= next_sample_ms:
                yield frame_index, int(round(pos_ms)), frame
                frame_index += 1
                next_sample_ms = pos_ms + interval_ms
            max_reads += 1
            if max_reads > 10_000_000:  # absolute safety ceiling
                break

    def _score_frame(
        self,
        frame_index: int,
        ts_ms: int,
        frame: _Frame,
        prev_small: _Frame | None,
    ) -> tuple[FrameScore, _Frame]:
        """Compute scalar metrics on a downscaled grayscale copy (design §4.1).

        Returns ``(score, small)`` so the caller threads ``small`` as the next
        frame's ``prev_small`` — scoring state is call-local (concurrency-safe).
        """
        cv2, np = import_cv2()

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(
            gray, (self._scoring_w, self._scoring_h), interpolation=cv2.INTER_AREA
        )
        brightness = float(np.mean(small))
        blur = float(cv2.Laplacian(small, cv2.CV_64F).var())
        motion = 0.0
        scene = 0.0
        if prev_small is not None:
            diff = cv2.absdiff(small, prev_small)
            motion = float(np.mean(diff)) / 255.0
            # Histogram-based scene change (normalized correlation distance).
            h_cur = cv2.calcHist([small], [0], None, [32], [0, 256])
            h_prev = cv2.calcHist([prev_small], [0], None, [32], [0, 256])
            cv2.normalize(h_cur, h_cur)
            cv2.normalize(h_prev, h_prev)
            corr = float(cv2.compareHist(h_cur, h_prev, cv2.HISTCMP_CORREL))
            scene = max(0.0, min(1.0, 1.0 - corr))
        score = FrameScore(
            frame_index=frame_index,
            timestamp_ms=ts_ms,
            motion=min(1.0, motion),
            scene=scene,
            blur=blur,
            brightness=brightness,
        )
        return score, small

    def _materialize(
        self,
        source_uri: str,
        out_dir: str,
        selected: list[FrameScore],
        ring: _RingBuffer,
        cap: _Capture,
    ) -> list[SelectedFrame]:
        """Encode selected frames to JPEG; re-seek any evicted from the buffer."""
        cv2, _np = import_cv2()

        refs: list[SelectedFrame] = []
        reseek_cap: _Capture | None = None
        try:
            for s in selected:
                frame = ring.get(s.frame_index)
                if frame is None:
                    if reseek_cap is None:
                        reseek_cap = cv2.VideoCapture(source_uri)
                        if not reseek_cap.isOpened():
                            raise RuntimeError("opencv re-seek could not open source")
                    frame = self._reseek_one(reseek_cap, s.timestamp_ms)
                out_path = os.path.join(
                    out_dir, f"f{s.frame_index:08d}_t{s.timestamp_ms}.jpg"
                )
                self._write_jpeg(out_path, frame)
                refs.append(
                    SelectedFrame(
                        uri=out_path,
                        frame_index=s.frame_index,
                        timestamp_ms=s.timestamp_ms,
                        motion=round(s.motion, 6),
                        scene=round(s.scene, 6),
                        blur=round(s.blur, 3),
                        brightness=round(s.brightness, 3),
                        decode_backend=_DECODE_BACKEND,
                        selection_reason=self._strategy,
                    )
                )
            return refs
        finally:
            if reseek_cap is not None:
                reseek_cap.release()

    def _reseek_one(self, cap: _Capture, timestamp_ms: int) -> _Frame:
        cv2, _np = import_cv2()

        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp_ms))
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("opencv re-seek failed to read selected frame")
        return frame

    def _write_jpeg(self, out_path: str, frame: _Frame) -> None:
        """Atomically write a JPEG (unique temp + rename) to avoid partial files.

        The temp file name is UNIQUE per write (``tempfile.mkstemp`` in the SAME
        directory) so even if two writers ever target the same ``out_path`` they
        never share one ``<out_path>.tmp`` and cannot promote each other's
        half-written bytes (R10/P0 torn-JPEG defense). ``os.replace`` is atomic
        within a directory.
        """
        cv2, _np = import_cv2()

        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
        )
        if not ok:
            raise RuntimeError("opencv failed to JPEG-encode selected frame")
        out_dir = os.path.dirname(out_path) or "."
        fd, tmp_path = tempfile.mkstemp(
            dir=out_dir, prefix=os.path.basename(out_path) + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(buf.tobytes())
            os.replace(tmp_path, out_path)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------- fallback
    def _fallback_or_raise(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        reason: str,
        unit_key: str | None = None,
    ) -> list[SelectedFrame]:
        if not self._fallback_enabled:
            raise RuntimeError(f"opencv decode failed and fallback disabled: {reason}")
        if self._ffmpeg_processor is None:
            self._ffmpeg_processor = SegmentFrameVideoProcessor(
                frame_root=self._frame_root,
                ffmpeg_bin=self._ffmpeg_bin,
                frame_strategy=self._frame_strategy,
            )
        uris = self._ffmpeg_processor.extract_frame_uris(
            source_uri, segment_start_ms, segment_end_ms, frame_count,
            unit_key=unit_key,
        )
        # ffmpeg uniform timestamps mirror the legacy planner so refs stay honest.
        timestamps = self._ffmpeg_processor._frame_timestamps_ms(  # noqa: SLF001
            segment_start_ms, segment_end_ms, frame_count
        )
        refs: list[SelectedFrame] = []
        for i, uri in enumerate(uris):
            ts = timestamps[i] if i < len(timestamps) else segment_start_ms
            refs.append(
                SelectedFrame(
                    uri=uri,
                    frame_index=i,
                    timestamp_ms=ts,
                    motion=0.0,
                    scene=0.0,
                    blur=0.0,
                    brightness=0.0,
                    decode_backend="ffmpeg",
                    selection_reason="ffmpeg_fallback",
                )
            )
        return refs

    # --------------------------------------------------------------- helpers
    def _segment_dir(
        self, source_uri: str, start_ms: int, end_ms: int, *, unit_key: str | None = None
    ) -> str:
        """Per-segment output dir, isolated per analysis unit when ``unit_key`` set.

        Keyed by source stem + span and, when provided, a ``unit_key`` segment
        (the worker passes ``model_call_id``). The stem alone is NOT collision-free
        across same-basename videos in different directories or repeated concurrent
        analysis of one video (R10): two such units would otherwise share this
        directory and overwrite/delete each other's frames. The ``unit_key`` makes
        the path globally unique per unit; ``None`` preserves the legacy layout for
        non-concurrent/standalone callers.
        """
        stem = os.path.splitext(os.path.basename(source_uri))[0] or "video"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:64]
        parts = [self._frame_root, safe]
        if unit_key:
            safe_key = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in unit_key
            )[:80]
            parts.append(safe_key)
        parts.append(f"{start_ms}_{end_ms}")
        return os.path.join(*parts)
