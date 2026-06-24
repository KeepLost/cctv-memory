"""VLM unit scheduler: bounded concurrency + minimum request-start interval.

Provides ``VlmScheduler.run(unit_fn)``: acquires a semaphore (capping concurrent
calls to ``max_concurrent``) and enforces a monotonic gap of at least
``min_interval_ms`` between successive call *starts*.

Design constraints:
- No external dependencies: pure stdlib (threading, time, contextlib).
- Not a distributed lock; this governs one in-process worker only.
- Works for both serial (max_concurrent=1) and parallel (>1) modes.
- ``max_concurrent=1`` + ``min_interval_ms=0`` is identical to the previous
  serial behavior; existing tests remain unaffected.
- Thread-safety: ``_last_start_ns`` is protected by ``_interval_lock`` to
  prevent race conditions even when multiple threads share one scheduler.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")


class VlmScheduler:
    """Rate-limit and concurrency-cap wrapper for VLM unit calls.

    Args:
        max_concurrent: Maximum number of VLM calls in flight simultaneously.
        min_interval_ms: Minimum wall-clock gap (ms) between successive call starts.
    """

    def __init__(self, max_concurrent: int = 1, min_interval_ms: int = 0) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if min_interval_ms < 0:
            raise ValueError("min_interval_ms must be >= 0")
        self._max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self._min_interval_ns = min_interval_ms * 1_000_000
        self._last_start_ns: int = 0
        self._interval_lock = threading.Lock()

    def run(
        self,
        unit_fn: Callable[[], _T],
        *,
        on_event: Callable[[str, dict[str, int]], None] | None = None,
    ) -> _T:
        """Run ``unit_fn`` under concurrency cap and rate limit.

        Blocks until:
        1. A semaphore slot is available (concurrency cap), AND
        2. Enough wall time has elapsed since the last call start (rate limit).

        Returns the result of ``unit_fn``.
        """
        wait_started_ns = time.monotonic_ns()
        if on_event is not None:
            on_event("wait_start", {"max_concurrent": self._max_concurrent})
        with self._semaphore:
            acquired_ns = time.monotonic_ns()
            if on_event is not None:
                on_event(
                    "wait_finish",
                    {
                        "duration_ms": int((acquired_ns - wait_started_ns) / 1_000_000),
                        "max_concurrent": self._max_concurrent,
                    },
                )
            # Enforce minimum interval between *starts*.
            if self._min_interval_ns > 0:
                with self._interval_lock:
                    now_ns = time.monotonic_ns()
                    elapsed = now_ns - self._last_start_ns
                    if elapsed < self._min_interval_ns:
                        interval_started_ns = time.monotonic_ns()
                        if on_event is not None:
                            on_event("interval_start", {})
                        time.sleep((self._min_interval_ns - elapsed) / 1e9)
                        if on_event is not None:
                            on_event(
                                "interval_finish",
                                {
                                    "duration_ms": int(
                                        (time.monotonic_ns() - interval_started_ns)
                                        / 1_000_000
                                    )
                                },
                            )
                    self._last_start_ns = time.monotonic_ns()
            call_started_ns = time.monotonic_ns()
            if on_event is not None:
                on_event("call_start", {})
            try:
                result = unit_fn()
            except Exception:
                if on_event is not None:
                    on_event(
                        "call_fail",
                        {
                            "duration_ms": int(
                                (time.monotonic_ns() - call_started_ns) / 1_000_000
                            )
                        },
                    )
                raise
            if on_event is not None:
                on_event(
                    "call_finish",
                    {
                        "duration_ms": int(
                            (time.monotonic_ns() - call_started_ns) / 1_000_000
                        )
                    },
                )
            return result
