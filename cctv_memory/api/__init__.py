"""API layer (FastAPI routers + glue).

Phase 0 provides only a placeholder app factory. No real routes, no
infrastructure imports. Routers must not import infrastructure concrete
repositories (testing-contract §10).
"""

from cctv_memory.api.app import create_app

__all__ = ["create_app"]
