"""Pre-VLM gate service port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.contracts.pre_vlm_gate import (
    GateDecisionBundle,
    PreVlmGateRequest,
)


@runtime_checkable
class PreVlmGatePort(Protocol):
    """Business-facing gate boundary used by workers before VLM calls."""

    def evaluate(self, request: PreVlmGateRequest) -> GateDecisionBundle:
        """Evaluate a pre-VLM gate request and persist any required gate log."""
        ...
