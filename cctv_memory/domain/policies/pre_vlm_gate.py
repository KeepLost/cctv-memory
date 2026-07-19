"""Pure pre-VLM gate decision policy."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cctv_memory.contracts.pre_vlm_gate import (
    GateDecisionBundle,
    GateProfile,
    GateRule,
    GateSignal,
    PreVlmGateDecision,
)
from cctv_memory.domain.enums import AnalysisScale


def decide_pre_vlm_gate(
    *,
    signals: list[GateSignal],
    rules: list[GateRule],
    analysis_scale: AnalysisScale,
    suppression_policy: str,
    trigger_context: dict[str, Any] | None = None,
    profile: GateProfile | None = None,
) -> GateDecisionBundle:
    """Return a deterministic gate decision from signals and rules.

    This generalizes the old detector-gate positive-frame-ratio logic. The first
    supported signal is object detection; future signals can add their own summary
    shape without changing workers.
    """

    context = dict(trigger_context or {})
    force_reasons = set(profile.force_vlm_on_trigger_reasons if profile else [])
    trigger_reason = str(context.get("trigger_reason", ""))
    evidence = _combined_frame_evidence(signals)
    evidence_hash = _hash_json(evidence)
    rule_hash = _hash_json([r.model_dump(mode="json") for r in rules])

    ratios: dict[str, float] = {}
    matched: list[str] = []
    for rule in rules:
        ratio = _positive_frame_ratio(signals=signals, rule=rule)
        ratios[rule.label] = ratio
        if rule.action == "call_vlm" and ratio >= rule.min_positive_frame_ratio:
            matched.append(_rule_match_name(rule))

    if trigger_reason and trigger_reason in force_reasons:
        decision = PreVlmGateDecision(
            triggered_vlm=True,
            action="force_vlm",
            matched_rules=matched,
            positive_frame_ratio_by_label=ratios,
            reason=f"force_vlm_on_trigger_reason:{trigger_reason}",
            evidence_hash=evidence_hash,
            rule_config_hash=rule_hash,
            suppression_policy=suppression_policy,
        )
    elif matched:
        decision = PreVlmGateDecision(
            triggered_vlm=True,
            action="call_vlm",
            matched_rules=matched,
            positive_frame_ratio_by_label=ratios,
            reason="matched gate rules",
            evidence_hash=evidence_hash,
            rule_config_hash=rule_hash,
            suppression_policy=suppression_policy,
        )
    else:
        decision = PreVlmGateDecision(
            triggered_vlm=False,
            action="suppress_vlm",
            matched_rules=[],
            positive_frame_ratio_by_label=ratios,
            reason="no gate rule matched",
            evidence_hash=evidence_hash,
            rule_config_hash=rule_hash,
            suppression_policy=suppression_policy,
        )

    summary = {
        "schema_version": "pre_vlm_gate_summary_v1",
        "analysis_scale": analysis_scale.value,
        "triggered_vlm": decision.triggered_vlm,
        "action": decision.action,
        "matched_rules": decision.matched_rules,
        "positive_frame_ratio_by_label": ratios,
        "evidence_hash": evidence_hash,
        "rule_config_hash": rule_hash,
        "suppression_policy": suppression_policy,
    }
    return GateDecisionBundle(
        decision=decision,
        signals=signals,
        frame_evidence=evidence,
        summary=summary,
    )


def _positive_frame_ratio(*, signals: list[GateSignal], rule: GateRule) -> float:
    frame_count = 0
    positive_frames = 0
    for signal in signals:
        if signal.signal_type != rule.signal_type:
            continue
        frame_count += signal.frame_count
        for frame in signal.frame_evidence:
            detections = frame.get("detections", [])
            if any(_detection_matches(d, rule) for d in detections if isinstance(d, dict)):
                positive_frames += 1
    if frame_count <= 0:
        return 0.0
    return positive_frames / frame_count


def _detection_matches(detection: dict[str, Any], rule: GateRule) -> bool:
    return (
        str(detection.get("label", "")) == rule.label
        and float(detection.get("confidence", 0.0)) >= rule.min_confidence
    )


def _combined_frame_evidence(signals: list[GateSignal]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for signal in signals:
        evidence.extend(signal.frame_evidence)
    return evidence


def _rule_match_name(rule: GateRule) -> str:
    rule_id = rule.rule_id or rule.label
    return f"{rule_id}.positive_frame_ratio>={rule.min_positive_frame_ratio}"


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"
