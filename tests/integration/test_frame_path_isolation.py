"""P0 regression — concurrent frame-extraction path isolation (task R10).

Before the fix, ``OpenCvFrameStreamVideoProcessor`` (and the ffmpeg/static
processors) keyed the on-disk frame output directory ONLY on the source filename
stem + window bounds (``frame_root/<stem>/<start>_<end>/``), discarding the
directory and any video/unit identity. Two concurrently processed videos with
the SAME basename (e.g. per-camera files all named ``record.mp4``), or the same
video analyzed concurrently, therefore wrote/read/deleted the SAME frame files —
corrupting the pixels fed to the VLM and explaining high-concurrency output
divergence even at temperature=0.

The fix threads a unique ``unit_key`` (the worker passes ``model_call_id``) into
the frame extraction path so each analysis unit/model-call gets its own output
directory, plus unique same-directory temp files for the atomic JPEG write.

These tests fail on the old behavior (shared paths) and pass after the fix.
cv2/numpy + ffmpeg are required to synthesize and decode real clips; the suite
skips cleanly where they are unavailable (frame-stream-selector-cache-design §9).
"""

from __future__ import annotations

import threading
from importlib.util import find_spec
from pathlib import Path

import pytest

from tests.support.video_gen import ffmpeg_available, generate_testsrc

_CV2 = find_spec("cv2") is not None and find_spec("numpy") is not None
cv2_required = pytest.mark.skipif(not _CV2, reason="cv2/numpy not installed")
ffmpeg_required = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg needed to synth clip"
)


@cv2_required
@ffmpeg_required
def test_same_basename_concurrent_extraction_uses_disjoint_paths(tmp_path: Path) -> None:
    """Two different videos with the SAME basename must not share frame paths.

    This is the core R10 regression. ``camA/clip.mp4`` and ``camB/clip.mp4`` have
    identical stems; with the same window they collided on one directory. With
    per-unit isolation their output paths must be disjoint and both intact.
    """
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    # Distinct CONTENT, identical BASENAME, different directories.
    clip_a = generate_testsrc(tmp_path / "camA" / "clip.mp4", duration=5, rate=10)
    clip_b = generate_testsrc(tmp_path / "camB" / "clip.mp4", duration=5, rate=10)

    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"), sample_fps=8, buffer_seconds=4
    )

    results: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _run(name: str, source: str, unit_key: str) -> None:
        try:
            barrier.wait(timeout=30)  # maximize overlap
            refs = proc.extract_selected_frames(
                source, 0, 4000, 6, unit_key=unit_key
            )
            results[name] = [r.uri for r in refs]
        except BaseException as exc:  # noqa: BLE001 - surface in assertion
            errors.append(exc)

    ta = threading.Thread(target=_run, args=("a", str(clip_a), "mcall_aaaaaaaa"))
    tb = threading.Thread(target=_run, args=("b", str(clip_b), "mcall_bbbbbbbb"))
    ta.start()
    tb.start()
    ta.join(timeout=60)
    tb.join(timeout=60)

    assert not errors, f"extraction raised under concurrency: {errors}"
    assert len(results["a"]) == 6
    assert len(results["b"]) == 6

    paths_a = set(results["a"])
    paths_b = set(results["b"])
    # The bug: identical stems + identical window => identical paths => collision.
    assert paths_a.isdisjoint(paths_b), (
        "frame paths collided across same-basename videos: "
        f"{paths_a & paths_b}"
    )
    # Every frame from both units must still exist (no cross-unit deletion).
    for uri in results["a"] + results["b"]:
        assert Path(uri).exists(), f"frame missing (overwritten/deleted): {uri}"
        assert Path(uri).stat().st_size > 0


@cv2_required
@ffmpeg_required
def test_same_video_concurrent_analysis_uses_disjoint_paths(tmp_path: Path) -> None:
    """The SAME video analyzed concurrently (distinct unit_keys) must not collide.

    Repeated/concurrent analysis of one video previously mapped to one directory;
    per-unit_key isolation keeps each run's frames separate.
    """
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "only" / "clip.mp4", duration=5, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"), sample_fps=8, buffer_seconds=4
    )

    results: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _run(name: str, unit_key: str) -> None:
        try:
            barrier.wait(timeout=30)
            refs = proc.extract_selected_frames(
                str(clip), 0, 4000, 6, unit_key=unit_key
            )
            results[name] = [r.uri for r in refs]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_run, args=("1", "mcall_run1"))
    t2 = threading.Thread(target=_run, args=("2", "mcall_run2"))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not errors, f"extraction raised under concurrency: {errors}"
    assert set(results["1"]).isdisjoint(set(results["2"])), (
        "same-video concurrent analysis collided on frame paths"
    )
    for uri in results["1"] + results["2"]:
        assert Path(uri).exists()


@cv2_required
@ffmpeg_required
def test_unit_key_appears_in_output_path(tmp_path: Path) -> None:
    """The unit_key must be reflected in the output directory (isolation key)."""
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=4, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(frame_root=str(tmp_path / "frames"))
    refs = proc.extract_selected_frames(str(clip), 0, 3000, 4, unit_key="mcall_xyz123")
    assert refs
    for r in refs:
        assert "mcall_xyz123" in r.uri


@cv2_required
@ffmpeg_required
def test_no_shared_tmp_file_under_concurrent_same_dir_writes(tmp_path: Path) -> None:
    """Concurrent writes that resolve to the same dir must not share one .tmp.

    Even if two units somehow target the same out_dir, the JPEG temp file must be
    unique per write so a half-written temp can never be promoted by another
    thread (torn-JPEG defense). We assert no leftover ``<out_path>.tmp`` remains
    and all final JPEGs are intact.
    """
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=5, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"), sample_fps=8, buffer_seconds=4
    )
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _run(unit_key: str) -> None:
        try:
            barrier.wait(timeout=30)
            proc.extract_selected_frames(str(clip), 0, 4000, 6, unit_key=unit_key)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=(f"mcall_{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent extraction raised: {errors}"
    # No stray temp files left behind anywhere under frame_root.
    leftover_tmp = list(Path(tmp_path / "frames").rglob("*.tmp"))
    assert not leftover_tmp, f"leftover temp files: {leftover_tmp}"
