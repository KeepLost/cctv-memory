"""Local static health report (Phase 0 smoke).

No database, worker, or external dependency is queried in Phase 0. This only
reports the static local status used by the CLI ``health`` command.
"""

from __future__ import annotations

from dataclasses import dataclass

from cctv_memory import __version__
from cctv_memory.contracts.common import SCHEMA_VERSION


@dataclass(frozen=True)
class HealthReport:
    """Static local health report."""

    status: str
    version: str
    schema_version: str
    phase: str


def get_health_report() -> HealthReport:
    """Return a static local health report (no I/O)."""
    return HealthReport(
        status="ok",
        version=__version__,
        schema_version=SCHEMA_VERSION,
        phase="mvp-closed-loop",
    )
