"""Abstract service port: frame stream (frame-stream-selector-cache-design §1.3).

The frame stream decodes a video segment once, scores frames online, and
materializes ONLY the metric-selected frames to disk for the VLM. It is an
infrastructure-facing port; the OpenCV adapter implements it.

Boundary rule (ARCHITECTURE_CONSTITUTION §3/§4): this port NEVER exposes raw
pixels / ``np.ndarray``. It returns ``SelectedFrame`` DTOs carrying a file path
plus scalar metadata. Raw frames live and die inside the infrastructure adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SelectedFrame:
    """A frame chosen for the VLM, materialized to disk (no pixels in this DTO).

    ``uri`` is the on-disk JPEG path the VLM adapter reads. The remaining fields
    are scalar provenance/selection metadata recorded into ``ModelCallLog.media
    _refs`` (table-schema-spec §4.5: no base64, no source_uri). ``frame_index``
    and ``timestamp_ms`` are the frame's identity in the decoded stream.
    """

    uri: str
    frame_index: int
    timestamp_ms: int
    motion: float
    scene: float
    blur: float
    brightness: float
    decode_backend: str
    selection_reason: str


@runtime_checkable
class FrameStreamPort(Protocol):
    """Port for streaming decode + metric-driven frame selection + materialization."""

    def extract_selected_frames(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[SelectedFrame]:
        """Decode ``[start, end)`` once, score frames, select ``frame_count`` of
        them, write the selected frames to disk, and return their refs.

        The returned list is ordered by ``timestamp_ms`` ascending so the VLM
        receives chronological frames (vlm-analysis-contract §4.5). No raw pixel
        data crosses this boundary.

        ``unit_key`` (task cctv-memory-20260616-1339, R10/P0): an OPTIONAL unique
        per-analysis-unit identifier (the worker passes ``model_call_id``) folded
        into the on-disk output directory so concurrent units NEVER share frame
        files. Without it the output path is keyed only by source-stem + window,
        which collides across same-basename videos / repeated concurrent analysis
        and lets one unit overwrite/delete another's frames. Adapters MUST isolate
        output by ``unit_key`` when provided; ``None`` preserves legacy paths for
        non-concurrent/standalone callers.
        """
        ...
