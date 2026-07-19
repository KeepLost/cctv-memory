"""Deterministic mock VLM adapter (infrastructure/vlm).

Produces a valid ``VlmObservationOutput`` deterministically from the request
(camera_id, segment timing, scale). This is a MOCK: it performs NO real video
understanding. It exists so the closed-loop pipeline can be exercised and tested
without any external provider or network.

It never emits policy/security fields — those are system-derived during
publication (ARCHITECTURE_CONSTITUTION §5, vlm-analysis-contract §4).
"""

from __future__ import annotations

from cctv_memory.contracts.vlm import (
    VlmAttr,
    VlmObservationOutput,
    VlmQuality,
    VlmSegmentRequest,
)

# A small deterministic tag vocabulary keyed by a stable hash bucket.
_TAG_POOL: tuple[tuple[str, ...], ...] = (
    ("person", "dark_clothing", "backpack"),
    ("person", "loitering", "doorway"),
    ("vehicle", "parking"),
    ("person", "running"),
    ("person", "group", "queue"),
)


class MockVlmAnalyzer:
    """Deterministic, offline mock VLM analyzer."""

    def analyze_segment(
        self, request: VlmSegmentRequest, *, strict_schema: bool = False
    ) -> VlmObservationOutput:
        _ = strict_schema
        bucket = (request.segment_start_ms // 1000) % len(_TAG_POOL)
        tags = list(_TAG_POOL[bucket])
        seconds = request.segment_start_ms // 1000
        static_text = (
            f"[mock] Camera {request.camera_id} segment at {seconds}s shows "
            f"{', '.join(tags)} near the monitored area."
        )
        dynamic_text = (
            f"[mock] Activity observed between {request.segment_start_ms}ms and "
            f"{request.segment_end_ms}ms: subject(s) tagged {tags[0]} moving through frame."
        )
        return VlmObservationOutput(
            static=static_text,
            dynamic=dynamic_text,
            tags=tags,
            quality=VlmQuality(reason="mock_output_no_real_vision", score=0.5),
            attr=VlmAttr(alert=False),
        )
