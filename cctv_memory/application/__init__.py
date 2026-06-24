"""Application use cases / orchestration.

Application depends on domain + repository/service ports only. It must not
import infrastructure concretes or raw DB drivers (ARCHITECTURE_CONSTITUTION §3,
testing-contract §10).
"""

from cctv_memory.application.doctor import build_doctor_report, render_doctor_text
from cctv_memory.application.health import HealthReport, get_health_report

__all__ = [
    "HealthReport",
    "build_doctor_report",
    "get_health_report",
    "render_doctor_text",
]
