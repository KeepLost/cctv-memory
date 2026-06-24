"""Abstract service port: VLM analyzer (module-map §2.4, services section).

The VLM analyzer produces validated output only; it never writes active
ObservationRecord (ARCHITECTURE_CONSTITUTION §6, vlm-analysis-contract §0).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest


@runtime_checkable
class VlmAnalyzerPort(Protocol):
    """Port for VLM segment analysis.

    Adapters (mock or real provider) return validated output only and never set
    policy/security fields (enforced by the contract's ``extra="forbid"``).
    """

    def analyze_segment(self, request: VlmSegmentRequest) -> VlmObservationOutput:
        """Analyze a video segment and return validated VLM output."""
        ...
