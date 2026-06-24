"""Shared repository-port types and errors (repository-port-contract §1).

These are framework-agnostic value types used at the port boundary. They are
not ORM models and carry no infrastructure dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Page[T]:
    """A page of results with an optional next cursor (repository-port-contract §1)."""

    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class RepositoryError(Exception):
    """Base repository error."""


class ConflictError(RepositoryError):
    """Unique-constraint or state conflict (error-code-contract: conflict)."""


class IdempotencyConflictError(RepositoryError):
    """Same idempotency key with a different payload (error-code-contract)."""


class WriteNotPermittedError(RepositoryError):
    """Raised when a read-only path attempts a write (write_path_separation)."""
