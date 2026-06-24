"""Test-video generator helper (test support).

Generates tiny synthetic clips via ffmpeg with bounded timeout + stdin=DEVNULL
(testing-contract §12). Used by integration tests so they don't depend on
committed binary fixtures (``data/`` is gitignored).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_FFMPEG_TIMEOUT = 60


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def generate_testsrc(out_path: str | Path, *, duration: int = 5, rate: int = 10) -> Path:
    """Generate a color-bars testsrc clip; return the output path.

    Bounded + non-interactive. Raises RuntimeError if ffmpeg fails.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(  # noqa: S603 - fixed binary, no shell
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"testsrc=duration={duration}:size=320x240:rate={rate}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(out),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg test-clip generation failed: {type(exc).__name__}") from exc
    return out
