"""Cross-scale VLM unit scheduling (Stage C2).

After ``motion_scan`` produces triggers, the ``default_segment`` and
``high_freq_event`` scales have NO data dependency on each other (research report
§5.2/§5.3). This module lets their per-unit VLM work be planned up front and
dispatched from ONE unified in-process queue, so high-frequency units can be
prioritized while default-segment units are never starved.

Design constraints (task-spec + ARCHITECTURE_CONSTITUTION):
- In-process worker-local only. No distributed/external queue.
- Each unit still runs in its own DB session and publishes per-unit (the existing
  processor ``_run_unit_in_fresh_session`` discipline). Out-of-order completion is
  therefore safe: idempotency keys + immediate per-unit publication are unchanged.
- The real provider concurrency/interval cap is enforced by the shared
  ``VlmScheduler`` (Stage C1) inside each unit's VLM call; this layer governs
  *dispatch order/priority*, not the provider limit.
- Deterministic, starvation-free priority: emit at most ``high_freq_quota``
  high_freq units, then force one default unit, repeat; drain the remainder when
  one side empties. The unit set is finite and planned up front, so strict
  starvation cannot occur; the quota makes priority + fairness explicit and the
  order unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.workers.unit_result import ScaleProcessResult, UnitOutcome


@dataclass(frozen=True)
class PlannedUnit:
    """A planned-but-not-yet-executed VLM unit.

    ``run`` executes the unit end to end in its own fresh DB session (frame
    selection -> VLM via the shared scheduler -> per-unit publication -> mark
    terminal) and returns a ``UnitOutcome`` (succeeded/failed/skipped). It MUST
    NOT raise: the processor terminalizes the unit's DB state for every outcome,
    so the scheduler is never stranded. No pixels/ndarray cross this boundary —
    only a closure and the unit's scale.
    """

    scale: AnalysisScale
    run: Callable[[], UnitOutcome]


def dispatch_order(
    high_freq: list[PlannedUnit],
    default: list[PlannedUnit],
    *,
    high_freq_quota: int,
) -> list[PlannedUnit]:
    """Deterministic, starvation-free dispatch order: high_freq prioritized.

    Emit up to ``high_freq_quota`` high_freq units, then one default unit, and
    repeat; when one side empties, drain the other. Input order within each scale
    is preserved (planners already produce chronological/idempotent order).
    """
    quota = max(1, int(high_freq_quota))
    hf = list(high_freq)
    df = list(default)
    ordered: list[PlannedUnit] = []
    hi = di = 0
    while hi < len(hf) or di < len(df):
        # Prefer up to `quota` high_freq units first.
        emitted_hf = 0
        while hi < len(hf) and emitted_hf < quota:
            ordered.append(hf[hi])
            hi += 1
            emitted_hf += 1
        # Then force one default unit so it is never starved.
        if di < len(df):
            ordered.append(df[di])
            di += 1
        elif hi >= len(hf):
            break
    return ordered


class CrossScaleUnitScheduler:
    """Run default_segment + high_freq_event units from one unified queue.

    Dispatches in deterministic priority order (high_freq first, default never
    starved) through a single bounded thread pool. Per-scale terminal counts are
    aggregated so each scale can be finalized on "all its units terminal" rather
    than a sequential block ending.
    """

    def __init__(self, *, max_workers: int = 1, high_freq_quota: int = 3) -> None:
        self._max_workers = max(1, int(max_workers))
        self._high_freq_quota = max(1, int(high_freq_quota))

    def run(
        self,
        *,
        high_freq_units: list[PlannedUnit],
        default_units: list[PlannedUnit],
    ) -> dict[AnalysisScale, ScaleProcessResult]:
        """Execute all units; return per-scale results once every unit is terminal."""
        ordered = dispatch_order(
            high_freq_units, default_units, high_freq_quota=self._high_freq_quota
        )
        # Per scale: [total, succeeded, failed, skipped].
        totals: dict[AnalysisScale, list[int]] = {
            AnalysisScale.DEFAULT_SEGMENT: [len(default_units), 0, 0, 0],
            AnalysisScale.HIGH_FREQ_EVENT: [len(high_freq_units), 0, 0, 0],
        }

        if not ordered:
            return {
                scale: ScaleProcessResult(
                    total=t[0], succeeded=t[1], failed=t[2], skipped=t[3]
                )
                for scale, t in totals.items()
            }

        if self._max_workers == 1:
            # Strict priority order (deterministic): run one at a time.
            for unit in ordered:
                self._tally(totals[unit.scale], self._safe_run(unit))
        else:
            # Submit in priority order; the shared VlmScheduler caps real provider
            # concurrency. as_completed handles out-of-order completion safely.
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                future_to_scale = {
                    executor.submit(self._safe_run, unit): unit.scale
                    for unit in ordered
                }
                for future in as_completed(future_to_scale):
                    scale = future_to_scale[future]
                    # _safe_run never raises, so future.result() is safe.
                    self._tally(totals[scale], future.result())

        return {
            scale: ScaleProcessResult(
                total=t[0], succeeded=t[1], failed=t[2], skipped=t[3]
            )
            for scale, t in totals.items()
        }

    @staticmethod
    def _safe_run(unit: PlannedUnit) -> UnitOutcome:
        """Run a unit, converting any UNEXPECTED escape into a FAILED outcome.

        Defense in depth (task-spec §B): the processor already terminalizes the
        unit's DB state for every handled path and returns a ``UnitOutcome``. If an
        unforeseen exception still escapes ``run``, count it as a failed unit so a
        single rogue unit can never strand Phase 4 / the job — the exception is NOT
        swallowed silently into a ``running`` DB state, it becomes an explicit
        failure tally that drives finalization.
        """
        try:
            return unit.run()
        except Exception:  # noqa: BLE001 - last-resort guard; see docstring
            return UnitOutcome.FAILED

    @staticmethod
    def _tally(counter: list[int], outcome: UnitOutcome) -> None:
        if outcome is UnitOutcome.SUCCEEDED:
            counter[1] += 1
        elif outcome is UnitOutcome.SKIPPED:
            counter[3] += 1
        else:
            counter[2] += 1
