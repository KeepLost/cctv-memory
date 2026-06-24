"""Common contract base and shared schemas.

Contracts only define data shapes (module-map §2.1) — no business logic.
All cross-module data uses explicit schema/DTO objects
(ARCHITECTURE_CONSTITUTION §4).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "v1"


class ContractModel(BaseModel):
    """Base for all contract DTOs.

    ``extra="forbid"`` enforces explicit fields and rejects unknown ones,
    which also implements the VLM "forbidden policy/security fields" rule
    at the schema boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)


class TimeRange(ContractModel):
    """Time range with timezone-aware bounds and start < end (schema-contracts §1.2)."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _validate_range(self) -> TimeRange:
        for label, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"TimeRange.{label} must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("TimeRange.start must be strictly before TimeRange.end")
        return self


class PageRequest(ContractModel):
    """Cursor-based pagination request (schema-contracts §1.3)."""

    limit: int = Field(default=50, ge=1, le=1000)
    cursor: str | None = None


class PageResponse(ContractModel):
    """Cursor-based pagination response (schema-contracts §1.3)."""

    limit: int = Field(ge=1, le=1000)
    next_cursor: str | None = None
    has_more: bool = False


class ResponseMeta(ContractModel):
    """Envelope metadata (schema-contracts §1.4)."""

    schema_version: str = SCHEMA_VERSION
    server_time: datetime | None = None


class ErrorDetail(ContractModel):
    """Error shape (error-code-contract §1)."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ApiSuccessEnvelope(ContractModel):
    """Unified success envelope (schema-contracts §1.4)."""

    ok: bool = True
    request_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    meta: ResponseMeta = Field(default_factory=ResponseMeta)


class ApiErrorEnvelope(ContractModel):
    """Unified error envelope (schema-contracts §1.4)."""

    ok: bool = False
    request_id: str
    error: ErrorDetail
    meta: ResponseMeta = Field(default_factory=ResponseMeta)
