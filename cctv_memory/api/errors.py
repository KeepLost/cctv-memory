"""API error mapping (api/errors.py).

Maps domain exceptions to (HTTP status, error_code) per error-code-contract.
The API layer may import domain (allowed direction); it must not import
infrastructure concretes.
"""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

from cctv_memory.domain.exceptions import (
    AuthorizationError,
    CapabilityDeniedError,
    InvalidStateTransitionError,
    NotFoundError,
    PrincipalNotFoundError,
    ValidationError,
    VlmSchemaValidationError,
)

# Repository-layer errors are mapped by string name to avoid importing the
# repositories package types into the API layer beyond what is needed.
_EXC_MAP: dict[type[Exception], tuple[int, str]] = {
    PrincipalNotFoundError: (401, "unauthenticated"),
    AuthorizationError: (403, "account_disabled"),
    CapabilityDeniedError: (403, "capability_denied"),
    NotFoundError: (404, "not_found"),
    ValidationError: (400, "validation_error"),
    InvalidStateTransitionError: (409, "invalid_state_transition"),
    VlmSchemaValidationError: (422, "vlm_schema_validation_failed"),
}


def map_exception(exc: Exception) -> tuple[int, str, str]:
    """Return (http_status, error_code, message) for an exception.

    Unknown exceptions map to 500 internal_error with a non-sensitive message
    (error-code-contract §6: no stack trace / internal path leakage).
    """
    # Pydantic request-body validation failures -> 400 validation_error.
    if isinstance(exc, PydanticValidationError):
        return 400, "validation_error", "Request validation failed."
    for exc_type, (status, code) in _EXC_MAP.items():
        if isinstance(exc, exc_type):
            return status, code, str(exc)
    # Repository conflict errors by class name (avoid hard import coupling).
    name = type(exc).__name__
    if name == "IdempotencyConflictError":
        return 409, "idempotency_conflict", "Idempotency key reused with different payload."
    if name == "ConflictError":
        return 409, "conflict", "Resource conflict."
    return 500, "internal_error", "An internal error occurred."
