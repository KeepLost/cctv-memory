"""Domain-level exceptions (pure domain layer).

Each maps to an error-code-contract code at the API boundary. Keeping them in
the domain layer lets application services raise meaningful errors without
importing infrastructure or the API envelope.
"""

from __future__ import annotations

from typing import Any


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


class ModelOutputSchemaValidationError(DomainError):
    """Raised when model output fails parse/repair/schema validation."""

    def __init__(
        self,
        message: str,
        *,
        model_output_kind: str = "model_output",
        stage: str = "schema_validation_failed",
        raw_response: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
        repair_attempted: bool = False,
        repair_succeeded: bool = False,
        provider: str | None = None,
        model_id: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.model_output_kind = model_output_kind
        self.stage = stage
        self.raw_response = raw_response
        self.parsed_payload = parsed_payload
        self.validation_errors = list(validation_errors or [])
        self.repair_attempted = repair_attempted
        self.repair_succeeded = repair_succeeded
        self.provider = provider
        self.model_id = model_id
        self.attempts = list(attempts or [])

    def to_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "model_output_kind": self.model_output_kind,
            "stage": self.stage,
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
            "validation_errors": self.validation_errors,
        }
        if self.provider is not None:
            details["provider"] = self.provider
        if self.model_id is not None:
            details["model_id"] = self.model_id
        if self.parsed_payload is not None:
            details["parsed_payload"] = self.parsed_payload
        if self.attempts:
            details["attempts"] = self.attempts
        return details


class VlmSchemaValidationError(ModelOutputSchemaValidationError):
    """Raised when VLM output fails schema validation (vlm_schema_validation_failed)."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("model_output_kind", "vlm")
        super().__init__(message, **kwargs)


class ObjectDetectionSchemaValidationError(ModelOutputSchemaValidationError):
    """Raised when object detection provider output fails schema validation."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("model_output_kind", "object_detection")
        super().__init__(message, **kwargs)


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
