"""OpenCV cold-start import/warmup regression tests.

Task cctv-memory-20260617-1441: ``opencv-python`` bootstrap is not safe under
concurrent FIRST import. The adapter must centralize cv2 import and the worker
must warm it once, single-threaded, before unit/job thread pools fan out.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest
from cctv_memory.config.settings import AppConfig
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.infrastructure.video.ffprobe_adapter import StaticVideoProcessor
from cctv_memory.infrastructure.video.opencv_import import OpenCvImportError

_CV2 = find_spec("cv2") is not None and find_spec("numpy") is not None


@pytest.mark.skipif(not _CV2, reason="OpenCV/numpy not installed")
def test_fresh_subprocess_concurrent_opencv_warmup_is_thread_safe() -> None:
    """Fresh interpreter: many threads hit the central import/warmup simultaneously."""
    code = r'''
import threading
from cctv_memory.infrastructure.video.opencv_import import import_cv2, warmup_opencv

errors = []
barrier = threading.Barrier(16)

def worker():
    try:
        barrier.wait()
        assert warmup_opencv(required=True) is True
        cv2, _np = import_cv2()
        assert cv2.__spec__ is not None
        assert hasattr(cv2, "VideoCapture")
    except BaseException as exc:  # noqa: BLE001 - report to parent process
        errors.append(f"{type(exc).__name__}: {exc}")

threads = [threading.Thread(target=worker) for _ in range(16)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

if errors:
    raise SystemExit("\n".join(errors))
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_analysis_worker_warms_opencv_before_thread_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cctv_memory.workers import analysis_worker
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    calls: list[bool] = []

    def fake_warmup(*, required: bool = False) -> bool:
        calls.append(required)
        return True

    monkeypatch.setattr(analysis_worker, "warmup_opencv", fake_warmup)
    config = AppConfig().with_data_dir(str(tmp_path))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.decode_backend = "opencv"
    config.pipeline.decode_fallback_to_ffmpeg = True
    runtime = Runtime(config)
    try:
        AnalysisWorker(runtime, video_processor=StaticVideoProcessor(duration_ms=1_000))
    finally:
        runtime.dispose()
    assert calls == [False]


def test_analysis_worker_fails_fast_when_opencv_required_and_warmup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cctv_memory.workers import analysis_worker
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    def fake_warmup(*, required: bool = False) -> bool:
        raise OpenCvImportError("broken cv2")

    monkeypatch.setattr(analysis_worker, "warmup_opencv", fake_warmup)
    config = AppConfig().with_data_dir(str(tmp_path))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.decode_backend = "opencv"
    config.pipeline.decode_fallback_to_ffmpeg = False
    runtime = Runtime(config)
    try:
        with pytest.raises(OpenCvImportError, match="broken cv2"):
            AnalysisWorker(runtime, video_processor=StaticVideoProcessor(duration_ms=1_000))
    finally:
        runtime.dispose()


def test_analysis_worker_allows_ffmpeg_fallback_when_optional_opencv_warmup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cctv_memory.workers import analysis_worker
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    def fake_warmup(*, required: bool = False) -> bool:
        assert required is False
        return False

    monkeypatch.setattr(analysis_worker, "warmup_opencv", fake_warmup)
    config = AppConfig().with_data_dir(str(tmp_path))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.decode_backend = "opencv"
    config.pipeline.decode_fallback_to_ffmpeg = True
    runtime = Runtime(config)
    try:
        AnalysisWorker(runtime, video_processor=StaticVideoProcessor(duration_ms=1_000))
    finally:
        runtime.dispose()
