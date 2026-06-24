"""Abstract service port: video processor (module-map §2.4).

The video processor extracts metadata (duration) and plans frame extraction.
It is an infrastructure-facing port; the ffprobe-backed adapter and a
deterministic test double both implement it. It never writes records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class VideoMetadata:
    """Probed video metadata."""

    duration_ms: int


@runtime_checkable
class VideoProcessorPort(Protocol):
    """Port for video metadata probing and frame extraction planning."""

    def probe(self, source_uri: str) -> VideoMetadata:
        """Return metadata for the video at ``source_uri``."""
        ...

    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        """Return frame URIs for a segment (placeholder paths for mock VLM).

        Structured for later real extraction; the MVP returns deterministic
        placeholder identifiers rather than decoding frames.

        ``unit_key`` (task cctv-memory-20260616-1339, R10/P0): OPTIONAL unique
        per-analysis-unit identifier (the worker passes ``model_call_id``) folded
        into the output directory so concurrent units do not collide on shared
        frame paths. ``None`` preserves legacy paths for standalone callers.
        """
        ...
