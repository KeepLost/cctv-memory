"""Async-to-sync bridge for AI service ports (application/async_support.py).

The AI service ports (``EmbeddingPort``, ``RerankerPort``) are asynchronous by
contract: every model call goes to a URL endpoint over ``httpx.AsyncClient`` and
must be awaited (task §"all AI calls async"). The search use case, the CLI, and
the FastAPI route handlers that orchestrate them are synchronous, however, and
rewriting the entire sync stack to async is out of scope and risk for this task.

``run_blocking`` runs a coroutine to completion and returns its result, working
both when there is no running event loop (the common sync case: CLI, tests,
sync route bodies) and when one is already running on the current thread (it
offloads to a private event loop on a worker thread so it never deadlocks). This
keeps the async port contract intact while letting synchronous orchestration
await a single embedding/rerank call.

This module is pure stdlib (asyncio/concurrent.futures) — no infrastructure or
framework imports — so it respects the layering rule (ARCHITECTURE_CONSTITUTION
§3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor


def run_blocking[T](coro: Coroutine[object, object, T]) -> T:
    """Run ``coro`` to completion from synchronous code and return its result.

    - No running loop on this thread (CLI / tests / sync handler): use
      ``asyncio.run`` semantics via a fresh loop.
    - A loop IS already running on this thread: run the coroutine on a private
      loop in a separate thread and block for the result, avoiding
      "asyncio.run() cannot be called from a running event loop".
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is None:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    # A loop is already running on this thread; execute on a worker thread with
    # its own loop so we can block synchronously without deadlocking the caller.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run_on_new_loop, coro).result()


def _run_on_new_loop[T](coro: Coroutine[object, object, T]) -> T:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)
