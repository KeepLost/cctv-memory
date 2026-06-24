"""Domain-level exceptions (pure domain layer).

Each maps to an error-code-contract code at the API boundary. Keeping them in
the domain layer lets application services raise meaningful errors without
importing infrastructure or the API envelope.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for domain-level errors."""


class AuthorizationError(DomainError):
    """Raised when an authorization invariant is violated (account_disabled)."""


class PrincipalNotFoundError(DomainError):
    """Raised when a principal cannot be resolved (unauthenticated)."""


class CapabilityDeniedError(DomainError):
    """Raised when the principal lacks a required capability (capability_denied)."""


class NotFoundError(DomainError):
    """Raised when a resource does not exist or is hidden by scope (not_found)."""


class ValidationError(DomainError):
    """Raised when input fails domain validation (validation_error)."""


class InvalidStateTransitionError(DomainError):
    """Raised on an illegal job/task state transition (invalid_state_transition)."""


class VlmSchemaValidationError(DomainError):
    """Raised when VLM output fails schema validation (vlm_schema_validation_failed)."""


class InsufficientFramesError(DomainError):
    """Raised when frame extraction yields ZERO usable frames for a window.

    Distinct from a hard decode error: a near-EOF / out-of-range window that
    decodes no frames is an expected, non-failure condition. Per owner decision
    (task cctv-memory-20260612-1854) the unit is marked ``skipped`` with reason
    ``insufficient_frames`` rather than ``failed``. Genuine decode/open failures
    raise ``RuntimeError`` and map to ``frame_extraction_failed`` instead.
    """


class LimitExceededError(DomainError):
    """Raised when top_k/context/revision/page limits are exceeded (limit_exceeded)."""


class ContextExpiredError(DomainError):
    """Raised when a SearchContext is expired/closed (context_expired)."""


class RestoreError(DomainError):
    """Raised when a backup restore fails validation (restore_failed)."""


class NotImplementedFeatureError(DomainError):
    """Raised when a disabled/unbuilt feature is invoked (not_implemented)."""
