"""Small worker result DTOs for analysis unit processing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class UnitOutcome(StrEnum):
    """Terminal outcome of running ONE analysis unit.

    Used by the scale processors and the cross-scale scheduler to tally per-scale
    results. ``SKIPPED`` is a benign terminal state (e.g. a near-EOF window with
    zero usable frames => ``insufficient_frames``); it is neither a success that
    produced a record nor a failure (task cctv-memory-20260612-1854).
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ScaleProcessResult:
    """Summary of one scale task's processed units."""

    total: int
    succeeded: int
    failed: int = 0
    skipped: int = 0

    @property
    def produced(self) -> int:
        return self.succeeded


class UnitPhase:
    """Mutable phase tag for the unit lifecycle guard (task cctv-memory-20260616-1850 §B1).

    Tracks which stage a ``running`` unit is in (``pre_vlm`` / ``vlm`` /
    ``post_vlm``) so that if an unforeseen exception escapes the running-unit body,
    the lifecycle guard can record durable, diagnosable evidence of WHERE the unit
    died — replacing the old "running with no log" failure mode. This is
    diagnostics only; it is NOT a new durable unit state (the unit still lands in
    the existing FAILED terminal state).
    """

    __slots__ = ("name",)

    def __init__(self, name: str = "pre_vlm") -> None:
        self.name = name
