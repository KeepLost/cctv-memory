"""Top-level composition root / bootstrap.

This is the single seam that wires the infrastructure Runtime to the FastAPI
application. It deliberately lives ABOVE both layers (not inside infrastructure)
so the architecture rule "infrastructure must not import api" holds: the
dependency arrow points api <- bootstrap -> infrastructure, never
infrastructure -> api.
"""

from __future__ import annotations

from cctv_memory.api.app import create_app
from cctv_memory.infrastructure.auth.dev_verifier import TrustingHeaderVerifier
from cctv_memory.infrastructure.runtime import Runtime, build_runtime


def build_app(runtime: Runtime, *, default_principal_id: str = "user_admin") -> object:
    """Build the FastAPI app bound to ``runtime``.

    Returns ``object`` to avoid leaking the FastAPI type into callers that do not
    need it; the concrete return is a ``fastapi.FastAPI`` instance. The active
    (non-sensitive) provider labels are read from the runtime config so ``/health``
    reports the real VLM/indexing selection instead of a hardcoded value.

    This composition root injects the auth verifier (the single identity seam):
    the dev ``TrustingHeaderVerifier`` for MVP/local use. Production swaps in a
    token verifier here without touching the api/application/domain layers.
    """
    cfg = runtime.config
    return create_app(
        runtime.request_services,
        default_principal_id=default_principal_id,
        auth_verifier=TrustingHeaderVerifier(default_principal_id=default_principal_id),
        vlm_provider=cfg.vlm.provider,
        indexing_provider=cfg.indexing.provider,
        indexing_enabled=cfg.indexing.enabled,
        timeline_recorder=runtime.timeline_recorder(),
    )


def build_app_from_data_dir(
    data_dir: str | None = None, *, default_principal_id: str = "user_admin"
) -> object:
    """Build a runtime for ``data_dir`` and return a wired FastAPI app."""
    runtime = build_runtime(data_dir=data_dir)
    return build_app(runtime, default_principal_id=default_principal_id)
