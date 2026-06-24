"""Frame selection + cleanup helpers shared by the analysis scale processors.

Bridges the two video-processor shapes used on the main path:
- ``FrameStreamPort`` (OpenCV default): returns ``SelectedFrame`` DTOs with frame
  provenance + selection scalars, materialized as JPEGs on disk.
- ``VideoProcessorPort`` (ffmpeg / whole-clip / static / mock): returns plain
  ``list[str]`` frame paths.

Both default_segment and high_freq_event call ``select_frames_for_unit`` so the
OpenCV streaming-selection path is the real main path (not a side module), and
``cleanup_selected_frames`` so successful units in metadata_only mode do not leave
working frame files behind (frame-stream-selector-cache-design §3.2/§7.3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cctv_memory.services.frame_stream import FrameStreamPort, SelectedFrame
from cctv_memory.services.video_processor import VideoProcessorPort


@dataclass(frozen=True)
class FrameSelection:
    """Result of selecting frames for one analysis unit.

    ``frame_uris`` (timestamp-ascending) feed the VLM request; ``media_refs_input``
    feeds ``build_media_refs`` (carries SelectedFrame scalars when available).
    """

    frame_uris: list[str]
    media_refs_input: list[str] | list[SelectedFrame]


def select_frames_for_unit(
    video_processor: VideoProcessorPort,
    source_uri: str,
    segment_start_ms: int,
    segment_end_ms: int,
    frame_count: int,
    *,
    unit_key: str | None = None,
) -> FrameSelection:
    """Extract the frames for one unit, preserving chronological order.

    ``unit_key`` (task cctv-memory-20260616-1339, R10/P0): a unique per-unit
    identifier (the worker passes ``model_call_id``) forwarded to the processor so
    each unit's frames land in their OWN directory. This prevents concurrent units
    (same-basename videos / repeated same-video analysis / overlapping windows)
    from overwriting or deleting each other's frame files.
    """
    if isinstance(video_processor, FrameStreamPort):
        selected: list[SelectedFrame] = video_processor.extract_selected_frames(
            source_uri, segment_start_ms, segment_end_ms, frame_count,
            unit_key=unit_key,
        )
        return FrameSelection(
            frame_uris=[sf.uri for sf in selected],
            media_refs_input=selected,
        )
    uris = video_processor.extract_frame_uris(
        source_uri, segment_start_ms, segment_end_ms, frame_count,
        unit_key=unit_key,
    )
    return FrameSelection(frame_uris=uris, media_refs_input=uris)


def cleanup_selected_frames(frame_uris: list[str]) -> None:
    """Delete the unit's selected frame working files (best-effort).

    Called only after a unit SUCCEEDS in non-debug/metadata_only mode. Debug
    artifacts live under ``artifact_root`` and are never touched here. Missing
    files are ignored (idempotent / crash-safe).
    """
    for uri in frame_uris:
        try:
            os.remove(uri)
        except OSError:
            pass  # already gone / not a real file (mock/static placeholder)
