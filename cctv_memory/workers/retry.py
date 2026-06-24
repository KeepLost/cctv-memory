"""Unit-level VLM retry policy + bounded terminal-write hardening.

This module centralizes the worker-layer retry concerns for ONE analysis unit
(task cctv-memory-20260615-1447, design in status/vlm-first-call-retry-review-20260615.md):

- Transient VLM provider/API failures (cold start, timeout, transport, 5xx, 429 — all
  surfaced today as ``VlmProviderError``) are retried with bounded backoff + jitter.
- Permanent failures (schema/contract validation, frame extraction, insufficient frames,
  publication, storage corruption) are NOT retried — fail fast to a terminal state.
- Every attempt still runs through the injected ``VlmScheduler`` so global concurrency +
  min-request-interval semantics are preserved (the runner passes a ``scheduler_run``
  callable that wraps the call in ``VlmScheduler.run``).
- Terminal DB writes (``mark_failed`` / ``mark_skipped`` / success commit) can hit a
  transient SQLite lock/busy error; ``run_db_write_with_retry`` retries those briefly so a
  terminal write does not silently fail and leave the unit ``running`` (tally-vs-DB
  divergence). If it still cannot persist, the exception propagates and the bounded orphan
  sweep remains the backstop — we never pretend success.

Layer note: this is worker-layer code. It does not import SQLAlchemy or any provider SDK;
transient-DB detection is by exception class name / message so the worker stays decoupled
from infrastructure (ARCHITECTURE_CONSTITUTION §3).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cctv_memory.contracts.vlm import VlmObservationOutput

if TYPE_CHECKING:
    from cctv_memory.contracts.vlm import VlmSegmentRequest


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def is_transient_vlm_error(exc: BaseException) -> bool:
    """Return True if a VLM-call exception is a transient provider/API error.

    Transient == ``VlmProviderError`` (the real adapter wraps timeout / transport /
    non-200 / 429 / unexpected-response into it). Everything else — schema validation,
    contract/value errors, anything raised by frame extraction or publication — is
    permanent and must NOT be retried. We match by class NAME (not import) so the worker
    layer needs no dependency on the infrastructure adapter module.
    """
    for klass in type(exc).__mro__:
        if klass.__name__ == "VlmProviderError":
            return True
    return False


def vlm_failure_error_code(exc: BaseException) -> str:
    """Map a VLM-call exception to the unit ``last_error_code`` (error-code-contract §4)."""
    name = type(exc).__name__
    if name == "VlmProviderError" or "provider" in name.lower():
        return "vlm_provider_error"
    if name == "VlmSchemaValidationError":
        return "vlm_schema_validation_failed"
    return "analysis_unit_failed"


def is_transient_db_error(exc: BaseException) -> bool:
    """Return True for a transient SQLite write error (lock / busy / timeout).

    Detected by class name / message so the worker layer does not import SQLAlchemy.
    Maps conceptually to ``retryable_storage_error`` (error-code-contract §5/§7).
    Corruption / integrity / programming errors are NOT transient.
    """
    permanent_markers = ("IntegrityError", "DataError", "ProgrammingError")
    for klass in type(exc).__mro__:
        if klass.__name__ in permanent_markers:
            return False
    text = f"{type(exc).__name__}: {exc}".lower()
    transient_markers = (
        "operationalerror",
        "database is locked",
        "database table is locked",
        "busy",
        "timeout",
    )
    return any(m in text for m in transient_markers)


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def compute_backoff_ms(
    attempt: int,
    *,
    base_ms: int,
    cap_ms: int,
    jitter: float,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with full-ish jitter for retry ``attempt`` (1-based).

    Delay before the attempt-th *retry* = clamp(base * 2**(attempt-1), cap) scaled by a
    uniform factor in ``[1 - jitter, 1 + jitter]`` (clamped to >= 0). ``jitter=0`` gives a
    deterministic backoff (used by tests). ``base_ms<=0`` disables waiting entirely.
    """
    if base_ms <= 0:
        return 0.0
    raw: float = min(float(base_ms) * float(2 ** max(0, attempt - 1)), float(cap_ms))
    if jitter <= 0:
        return max(0.0, raw)
    r = rng.uniform(-jitter, jitter) if rng is not None else random.uniform(-jitter, jitter)
    return max(0.0, raw * (1.0 + r))


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded unit-level VLM retry policy.

    ``max_attempts=1`` reproduces the previous no-retry behavior exactly (one attempt,
    terminal on failure). ``max_attempts>1`` retries only transient provider errors.
    """

    max_attempts: int = 1
    backoff_base_ms: int = 500
    backoff_cap_ms: int = 8_000
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_base_ms < 0 or self.backoff_cap_ms < 0:
            raise ValueError("backoff must be >= 0")
        if not (0.0 <= self.jitter <= 1.0):
            raise ValueError("jitter must be in [0, 1]")


@dataclass(frozen=True)
class VlmAttempt:
    """Audit metadata for one VLM attempt (kept small; stored in attempt_details)."""

    attempt: int
    status: str  # "succeeded" | "failed"
    error_type: str | None = None
    error_message: str | None = None
    transient: bool | None = None
    backoff_ms: float | None = None

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"attempt": self.attempt, "status": self.status}
        if self.error_type is not None:
            d["error_type"] = self.error_type
        if self.error_message is not None:
            d["error_message"] = self.error_message
        if self.transient is not None:
            d["transient"] = self.transient
        if self.backoff_ms is not None:
            d["backoff_ms"] = round(self.backoff_ms, 1)
        return d


@dataclass
class VlmRetryResult:
    """Outcome of ``execute_vlm_with_retry``.

    Exactly one of ``output`` / ``error`` is set. ``attempts`` is the number of attempts
    actually made (>=1). ``attempt_details`` is the per-attempt audit trail.
    """

    output: VlmObservationOutput | None
    error: BaseException | None
    attempts: int
    attempt_details: list[dict[str, object]]


def execute_vlm_with_retry(
    *,
    request: VlmSegmentRequest,
    analyze: Callable[[VlmSegmentRequest], VlmObservationOutput],
    scheduler_run: Callable[[Callable[[], VlmObservationOutput]], VlmObservationOutput],
    policy: RetryPolicy,
    on_attempt_started: Callable[[int], None] | None = None,
    on_attempt_succeeded: Callable[[VlmAttempt], None] | None = None,
    on_attempt_failed: Callable[[VlmAttempt], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> VlmRetryResult:
    """Run the VLM call with bounded transient retry; never raises for VLM errors.

    - Each attempt is executed via ``scheduler_run`` (so VlmScheduler concurrency +
      min-interval still applies to EVERY attempt).
    - On a transient error with budget remaining: record the failed attempt (via
      ``on_attempt_failed``), back off, and retry.
    - On a permanent error: stop immediately (no retry) and return it.
    - On success: return the output.

    The caller is responsible for writing ModelCallLog rows; this function only invokes
    ``on_attempt_failed`` for each FAILED attempt so the caller can persist a per-attempt
    FAILED log with the right ``attempt_count``.
    """
    details: list[dict[str, object]] = []
    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        if on_attempt_started is not None:
            on_attempt_started(attempt)
        try:
            output = scheduler_run(lambda: analyze(request))
        except Exception as exc:  # noqa: BLE001 - classified below; never propagated here
            last_error = exc
            transient = is_transient_vlm_error(exc)
            has_budget = attempt < policy.max_attempts
            will_retry = transient and has_budget
            backoff_ms = (
                compute_backoff_ms(
                    attempt,
                    base_ms=policy.backoff_base_ms,
                    cap_ms=policy.backoff_cap_ms,
                    jitter=policy.jitter,
                    rng=rng,
                )
                if will_retry
                else None
            )
            record = VlmAttempt(
                attempt=attempt,
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
                transient=transient,
                backoff_ms=backoff_ms,
            )
            details.append(record.to_dict())
            if on_attempt_failed is not None:
                on_attempt_failed(record)
            if not will_retry:
                return VlmRetryResult(
                    output=None, error=exc, attempts=attempt, attempt_details=details
                )
            if backoff_ms and backoff_ms > 0:
                sleep(backoff_ms / 1000.0)
            continue
        else:
            success = VlmAttempt(attempt=attempt, status="succeeded")
            details.append(success.to_dict())
            if on_attempt_succeeded is not None:
                on_attempt_succeeded(success)
            return VlmRetryResult(
                output=output, error=None, attempts=attempt, attempt_details=details
            )
    # Unreachable in practice (loop returns), but keep the type-checker happy.
    return VlmRetryResult(
        output=None,
        error=last_error or RuntimeError("vlm retry exhausted"),
        attempts=policy.max_attempts,
        attempt_details=details,
    )


def run_db_write_with_retry[T](
    write: Callable[[], T],
    *,
    max_attempts: int = 3,
    backoff_ms: int = 100,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run a terminal DB write, retrying briefly on transient lock/busy errors.

    State-hardening (design §4.5): a terminal ``mark_failed`` / ``mark_skipped`` / success
    commit that hits a transient SQLite lock should not silently fail and strand the unit
    ``running``. We retry a bounded number of times with a short linear backoff. A
    permanent error (integrity/corruption) or final exhaustion re-raises — the caller must
    surface it and rely on the bounded orphan sweep as the backstop (never pretend success).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return write()
        except Exception as exc:  # noqa: BLE001 - reclassified; re-raised if not transient
            last_error = exc
            if attempt >= max_attempts or not is_transient_db_error(exc):
                raise
            if backoff_ms > 0:
                sleep((backoff_ms * attempt) / 1000.0)
    raise last_error if last_error else RuntimeError("db write retry exhausted")
