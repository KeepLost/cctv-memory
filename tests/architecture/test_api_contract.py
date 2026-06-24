"""API contract snapshot + OpenAPI completeness + auth-seam tests (M3).

These tests freeze the SERVER's outward contract so a future client SDK / tool
proxy can rely on it, and breaking changes must be made explicitly (update the
snapshot in code review) rather than drifting silently:

- ``test_openapi_route_set_snapshot``: the exact set of (METHOD, path) routes.
- ``test_error_code_set_snapshot``: the exact set of error codes the API maps.
- ``test_openapi_has_complete_request_and_response_schemas``: every POST has a
  typed requestBody and every route advertises a response schema (client codegen
  depends on this).
- ``test_auth_verifier_dev_behavior``: the dev verifier preserves the historical
  header/default behavior, and identity comes from headers only.

Server-only scope: identity is header-borne; the body never carries identity or
ranking-weight fields (api-and-service-runtime-design §2.2).
"""

from __future__ import annotations

import tempfile
from typing import Any

from cctv_memory.api.errors import _EXC_MAP, map_exception
from cctv_memory.bootstrap import build_app_from_data_dir
from cctv_memory.services.auth_verifier import RequestCredentials


def _openapi() -> dict[str, Any]:
    app = build_app_from_data_dir(tempfile.mkdtemp())
    return app.openapi()  # type: ignore[attr-defined,no-any-return]


# The frozen outward contract. Changing this set is a client-affecting change and
# must be done deliberately (update this snapshot + bump /api version if breaking).
_EXPECTED_ROUTES: set[tuple[str, str]] = {
    ("DELETE", "/api/v1/observation-search/contexts/{context_id}"),
    ("GET", "/api/v1/analysis-jobs/{analysis_job_id}"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/observation-search/contexts/{context_id}/facets"),
    ("GET", "/api/v1/playback/{token}"),
    ("POST", "/api/v1/admin/backups"),
    ("POST", "/api/v1/exports/migration"),
    ("POST", "/api/v1/exports/user"),
    ("POST", "/api/v1/observation-search/contexts"),
    ("POST", "/api/v1/observation-search/contexts/{context_id}/batch-refine"),
    ("POST", "/api/v1/observation-search/contexts/{context_id}/refine"),
    ("POST", "/api/v1/observation-search/details"),
    ("POST", "/api/v1/observation-search/locators"),
    ("POST", "/api/v1/observation-search/overlapping-records"),
    ("POST", "/api/v1/video-sources/analyze"),
}

# Stable error-code vocabulary the client maps to tool errors (error-code-contract).
_EXPECTED_ERROR_CODES: set[str] = {
    "unauthenticated",
    "account_disabled",
    "capability_denied",
    "not_found",
    "validation_error",
    "invalid_state_transition",
    "vlm_schema_validation_failed",
    "idempotency_conflict",
    "conflict",
    "internal_error",
}

# POST routes that legitimately take no request body (scope-only operations).
_POST_WITHOUT_BODY: set[str] = {
    "/api/v1/exports/user",
    "/api/v1/exports/migration",
}


def test_openapi_route_set_snapshot() -> None:
    spec = _openapi()
    actual = {
        (method.upper(), path)
        for path, methods in spec["paths"].items()
        for method in methods
    }
    assert actual == _EXPECTED_ROUTES, (
        "API route set changed — this is a client-affecting contract change. "
        f"Added={actual - _EXPECTED_ROUTES} Removed={_EXPECTED_ROUTES - actual}. "
        "Update the snapshot deliberately (and bump /api version if breaking)."
    )


def test_error_code_set_snapshot() -> None:
    mapped = {code for (_status, code) in _EXC_MAP.values()}
    # Repository conflict codes + the internal fallback are produced by map_exception
    # branches, not the _EXC_MAP table; include them explicitly.
    mapped |= {"idempotency_conflict", "conflict", "internal_error"}
    assert mapped == _EXPECTED_ERROR_CODES, (
        "Error-code vocabulary changed — clients map these to tool errors. "
        f"Diff added={mapped - _EXPECTED_ERROR_CODES} "
        f"removed={_EXPECTED_ERROR_CODES - mapped}."
    )


def test_openapi_has_complete_request_and_response_schemas() -> None:
    spec = _openapi()
    missing_request: list[str] = []
    missing_response: list[str] = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            resp200 = op.get("responses", {}).get("200", {})
            if not resp200.get("content"):
                missing_response.append(f"{method.upper()} {path}")
            if method == "post" and path not in _POST_WITHOUT_BODY:
                if "requestBody" not in op:
                    missing_request.append(f"{method.upper()} {path}")
    assert not missing_request, f"POST routes missing requestBody schema: {missing_request}"
    assert not missing_response, f"routes missing 200 response schema: {missing_response}"


def test_openapi_success_envelope_is_referenced() -> None:
    spec = _openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    assert "ApiSuccessEnvelope" in schemas
    assert "ApiErrorEnvelope" in schemas
    # A representative route references the success envelope for its 200 response.
    health = spec["paths"]["/api/v1/health"]["get"]["responses"]["200"]
    ref = health["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("ApiSuccessEnvelope")


def test_map_exception_unknown_is_internal_error() -> None:
    status, code, _msg = map_exception(RuntimeError("boom"))
    assert (status, code) == (500, "internal_error")


def test_auth_verifier_dev_behavior() -> None:
    from cctv_memory.infrastructure.auth.dev_verifier import TrustingHeaderVerifier

    verifier = TrustingHeaderVerifier(default_principal_id="user_admin")
    # Header present -> use it.
    assert verifier.verify(RequestCredentials(principal_id_header="viewer_1")) == "viewer_1"
    # Header absent -> default principal (historical dev behavior).
    assert verifier.verify(RequestCredentials()) == "user_admin"
