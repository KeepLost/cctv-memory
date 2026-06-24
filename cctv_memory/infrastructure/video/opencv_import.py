"""Central, thread-safe OpenCV (cv2) import + warmup (infrastructure/video).

Why this module exists (task cctv-memory-20260617-1441): ``opencv-python``'s
``cv2/__init__.py`` runs a ``bootstrap()`` at import time that temporarily does
``sys.modules.pop("cv2")`` and re-inserts the module after loading the native
extension. That sequence is NOT thread-safe: if several worker threads execute
their FIRST ``import cv2`` simultaneously (exactly what a cold multi-job /
multi-unit start does), a thread can observe a half-initialized module whose
``__spec__`` is ``None`` (or which is missing native symbols), raising
``ImportError`` / ``AttributeError`` and failing frame extraction.

The fix is to funnel EVERY cv2 import through one process-wide, lock-guarded,
idempotent entry point, and to perform one eager single-threaded import +
sanity ``warmup`` at worker startup BEFORE any thread pool fans out. After the
first successful import the module object is cached and reused by all threads;
no thread ever races the bootstrap again. Multi-process deployments cannot share
Python module state, so each process performs its own warmup once.

Layer note: cv2 stays an infrastructure-only, optional vendor dependency. Raw
frames never cross this boundary (constitution §3/§4); only this module and the
decode adapter touch cv2. ``import numpy`` is funnelled the same way for symmetry
(numpy is import-safe but we keep a single choke point).
"""

from __future__ import annotations

import threading
from importlib.util import find_spec
from typing import Any

# cv2 / numpy are untyped C-extension vendor boundaries (constitution §4): type
# them as ``Any`` locally so they never leak as a structural type upward.
_Cv2Module = Any
_NumpyModule = Any

_import_lock = threading.Lock()
_cv2: _Cv2Module | None = None
_numpy: _NumpyModule | None = None
_warmed_up = False


class OpenCvImportError(RuntimeError):
    """OpenCV could not be imported / initialized.

    A ``RuntimeError`` subtype so existing decode error handling (which already
    treats ``RuntimeError`` as a fallback trigger) routes a cv2 import/bootstrap
    failure to the ffmpeg fallback instead of failing the unit outright. The
    decode adapter also catches the underlying ImportError/AttributeError that the
    cv2 bootstrap race raises and re-wraps it here so the classification is honest.
    """


def cv2_available() -> bool:
    """True iff both cv2 and numpy are importable (no import side effects).

    Uses ``find_spec`` only — does not import — so callers can cheaply gate the
    OpenCV path without triggering the (now-serialized) bootstrap. Once the module
    has been imported and cached, returns True directly.
    """
    if _cv2 is not None and _numpy is not None:
        return True
    return find_spec("cv2") is not None and find_spec("numpy") is not None


def import_cv2() -> tuple[_Cv2Module, _NumpyModule]:
    """Return ``(cv2, numpy)``, importing them once under a process-wide lock.

    The FIRST caller completes cv2's non-thread-safe ``bootstrap()`` entirely
    while holding the lock; subsequent callers (including concurrent worker
    threads) get the cached modules without ever re-entering the bootstrap. Any
    import/bootstrap failure is re-raised as ``OpenCvImportError`` so the decode
    adapter can fall back honestly.
    """
    global _cv2, _numpy
    if _cv2 is not None and _numpy is not None:
        return _cv2, _numpy
    with _import_lock:
        # Re-check under the lock: another thread may have completed the import
        # while we waited.
        if _cv2 is not None and _numpy is not None:
            return _cv2, _numpy
        try:
            import cv2  # noqa: PLC0415 - intentional one-time guarded import
            import numpy as np  # noqa: PLC0415 - intentional one-time guarded import
        except (ImportError, AttributeError) as exc:
            # AttributeError covers the half-initialized-bootstrap symptom
            # (cv2.__spec__ is None / missing native attribute) just in case it
            # surfaces despite the lock (e.g. a partial import from elsewhere).
            raise OpenCvImportError(f"opencv import failed: {exc!r}") from exc
        _numpy = np
        _cv2 = cv2
        return _cv2, _numpy


def warmup_opencv(*, required: bool = False) -> bool:
    """Eagerly import cv2 + run a tiny sanity op, ONCE, single-threaded.

    Call this at worker/process startup BEFORE any thread pool fans out so the
    cv2 bootstrap completes exactly once with no concurrency. Performs a minimal,
    cheap sanity check (encode a 1x1 image) to surface a broken OpenCV install
    immediately and clearly, instead of as N random ``frame_extraction_failed``
    units later.

    Returns True if OpenCV is ready, False if it is unavailable. When ``required``
    is True a failure raises ``OpenCvImportError`` (caller wants a hard stop, e.g.
    fallback disabled); when False an unavailable/broken OpenCV is reported via the
    return value so the caller can degrade to the ffmpeg fallback. Idempotent: the
    sanity op runs only on the first successful warmup.
    """
    global _warmed_up
    if _warmed_up:
        return True
    if not cv2_available():
        if required:
            raise OpenCvImportError("opencv/numpy not importable")
        return False
    try:
        cv2, np = import_cv2()
        # Minimal sanity op: encode a 1x1 black image. Exercises the native
        # extension end to end without touching disk or a real video.
        ok, _buf = cv2.imencode(".jpg", np.zeros((1, 1, 3), dtype=np.uint8))
        if not ok:
            raise OpenCvImportError("opencv sanity imencode returned not-ok")
    except OpenCvImportError:
        if required:
            raise
        return False
    except Exception as exc:  # noqa: BLE001 - any native error => not usable
        if required:
            raise OpenCvImportError(f"opencv warmup failed: {exc!r}") from exc
        return False
    _warmed_up = True
    return True


def _reset_for_tests() -> None:
    """Reset cached module state (TEST ONLY).

    Lets a test re-exercise the import path. Production never calls this.
    """
    global _cv2, _numpy, _warmed_up
    with _import_lock:
        _cv2 = None
        _numpy = None
        _warmed_up = False
