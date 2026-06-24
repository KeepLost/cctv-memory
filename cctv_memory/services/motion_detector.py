"""Abstract service port: motion detector (module-map §2.4, vlm-analysis-contract §2.2).

The motion detector performs cheap motion/change sampling over a video to drive
high_freq_event trigger selection. It returns motion samples (timestamp + score)
and never writes records or makes business decisions — the domain planner turns
samples into trigger windows and the application persists them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.domain.policies import MotionSample


@runtime_checkable
class MotionDetectorPort(Protocol):
    """Port for cheap motion sampling over a video source."""

    def sample_motion(self, source_uri: str) -> list[MotionSample]:
        """Return motion samples (timestamp_ms, normalized [0,1] score).

        Implementations must be bounded and non-interactive (no unbounded loops,
        no blocking stdin). A failure to read the source should raise
        ``RuntimeError`` so the worker can fail the unit cleanly.
        """
        ...
