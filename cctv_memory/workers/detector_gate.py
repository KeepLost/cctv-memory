"""Detector evidence manifest and gate decision helpers."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cctv_memory.config.settings import DetectorGateRuleSection
from cctv_memory.contracts.pre_vlm_gate import GateRule, GateSignal
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.domain.policies.pre_vlm_gate import decide_pre_vlm_gate
from cctv_memory.services.detector_gate import DetectorFrameInput, DetectorFrameResult
from cctv_memory.services.frame_stream import SelectedFrame


@dataclass(frozen=True)
class DetectorGateDecisionBundle:
    triggered_vlm: bool
    decision: dict[str, Any]
    frame_evidence: list[dict[str, Any]]
    evidence_hash: str
    rule_config_hash: str
    summary: dict[str, Any]


def build_detector_frame_inputs(
    media_refs_input: list[str] | list[SelectedFrame],
) -> list[DetectorFrameInput]:
    frames: list[DetectorFrameInput] = []
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
            DetectorFrameInput(
                uri=uri,
                frame_index=frame_index,
                timestamp_ms=timestamp_ms,
                frame_hash=_file_sha256(uri),
            )
        )
    return frames


def decide_detector_gate(
    *,
    results: list[DetectorFrameResult],
    rules: list[DetectorGateRuleSection],
    provider: str,
    model_id: str,
    gate_log_id: str,
) -> DetectorGateDecisionBundle:
    evidence = [_frame_result_to_evidence(r) for r in results]
    gate_rules = [
        GateRule(
            signal_type="object_detection",
            label=r.label,
            min_positive_frame_ratio=r.min_positive_frame_ratio,
            min_confidence=r.min_confidence,
            action=r.action,  # type: ignore[arg-type]
        )
        for r in rules
    ]
    gate_bundle = decide_pre_vlm_gate(
        signals=[
            GateSignal(
                signal_type="object_detection",
                provider=provider,
                model_id=model_id,
                frame_count=len(results),
                frame_evidence=evidence,
            )
        ],
        rules=gate_rules,
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        suppression_policy="publish_gate_only_record",
    )
    decision_data = gate_bundle.decision.model_dump(mode="json")
    triggered = gate_bundle.decision.triggered_vlm
    matched = list(gate_bundle.decision.matched_rules)
    ratios = dict(gate_bundle.decision.positive_frame_ratio_by_label)
    evidence_hash = gate_bundle.decision.evidence_hash
    rule_hash = gate_bundle.decision.rule_config_hash or _hash_json([])
    decision = {
        "schema_version": "detector_gate_decision_v1",
        "triggered_vlm": triggered,
        "matched_rules": matched,
        "positive_frame_ratio_by_label": ratios,
        "reason": decision_data["reason"],
        "rule_config_hash": rule_hash,
        "evidence_hash": evidence_hash,
    }
    summary = {
        "schema_version": "detector_gate_summary_v1",
        "provider": provider,
        "model_id": model_id,
        "gate_log_id": gate_log_id,
        "triggered_vlm": triggered,
        "matched_rules": matched,
        "positive_frame_ratio_by_label": ratios,
        "evidence_hash": evidence_hash,
        "rule_config_hash": rule_hash,
    }
    return DetectorGateDecisionBundle(
        triggered_vlm=triggered,
        decision=decision,
        frame_evidence=evidence,
        evidence_hash=evidence_hash,
        rule_config_hash=rule_hash,
        summary=summary,
    )


def _frame_result_to_evidence(result: DetectorFrameResult) -> dict[str, Any]:
    uri = result.frame.uri
    return {
        "frame_index": result.frame.frame_index,
        "timestamp_ms": result.frame.timestamp_ms,
        "uri_basename": os.path.basename(uri),
        "frame_hash": result.frame.frame_hash,
        "detections": [
            {
                "label": d.label,
                "confidence": d.confidence,
                "bbox": d.bbox,
                "bbox_format": d.bbox_format,
            }
            for d in result.detections
        ],
    }


def _file_sha256(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"
