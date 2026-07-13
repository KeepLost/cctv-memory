"""Shared worker helper for pre-VLM gate execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from cctv_memory.contracts.pre_vlm_gate import (
    GateDecisionBundle,
    GateFrameInput,
    GateProfile,
    PreVlmGateRequest,
)
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.services.frame_stream import SelectedFrame
from cctv_memory.services.pre_vlm_gate import PreVlmGatePort
from cctv_memory.workers.common import new_id


def run_pre_vlm_gate(
    *,
    gate: PreVlmGatePort | None,
    profile: GateProfile | None,
    media_refs_input: list[str] | list[SelectedFrame],
    analysis_job_id: str,
    scale_task_id: str,
    unit_id: str,
    video_id: str,
    analysis_scale: AnalysisScale,
    unit_kind: str,
    segment_start_ms: int,
    segment_end_ms: int,
    provider: str,
    model_id: str | None,
    trigger_context: dict[str, Any] | None = None,
) -> GateDecisionBundle | None:
    if gate is None or profile is None or not profile.enabled:
        return None
    request = PreVlmGateRequest(
        request_id=new_id("pgate_req"),
        gate_log_id=new_id("pgate"),
        analysis_job_id=analysis_job_id,
        scale_task_id=scale_task_id,
        unit_id=unit_id,
        video_id=video_id,
        analysis_scale=analysis_scale,
        unit_kind=unit_kind,
        segment_start_ms=segment_start_ms,
        segment_end_ms=segment_end_ms,
        provider=provider,
        model_id=model_id,
        profile=profile,
        frames=_gate_frame_inputs(media_refs_input),
        trigger_context=dict(trigger_context or {}),
    )
    return gate.evaluate(request)


def _gate_frame_inputs(media_refs_input: list[str] | list[SelectedFrame]) -> list[GateFrameInput]:
    frames: list[GateFrameInput] = []
    for index, item in enumerate(media_refs_input):
        if isinstance(item, SelectedFrame):
            uri = item.uri
            frame_index = item.frame_index
            timestamp_ms = item.timestamp_ms
        else:
            uri = str(item)
            frame_index = index
            timestamp_ms = index
        frames.append(
            GateFrameInput(
                uri=uri,
                frame_index=frame_index,
                timestamp_ms=timestamp_ms,
                frame_hash=_file_sha256(uri),
            )
        )
    return frames


def _file_sha256(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"
