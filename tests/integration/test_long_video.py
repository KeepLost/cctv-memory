"""C5 tests: long-video bounded per-segment real frame extraction.

Unit tests for the deterministic in-segment timestamp planner (no subprocess),
plus ffmpeg-gated integration tests that synthesize a multi-window clip and assert
real per-segment frame extraction and multi-segment -> multiple VLM calls. All
ffmpeg/ffprobe usage is bounded (stdin=DEVNULL + timeout), testing-contract §12.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain.policies import plan_default_segments
from cctv_memory.infrastructure.video.ffprobe_adapter import SegmentFrameVideoProcessor

from tests.support.video_gen import ffmpeg_available, generate_testsrc


def test_frame_timestamps_uniform_spacing() -> None:
    proc = SegmentFrameVideoProcessor(frame_strategy="uniform")
    ts = proc._frame_timestamps_ms(0, 12_000, 3)
    assert len(ts) == 3
    # Evenly spaced in the interior, strictly inside the segment, increasing.
    assert all(0 < t < 12_000 for t in ts)
    assert ts == sorted(ts)
    assert len(set(ts)) == 3


def test_frame_timestamps_single_frame_is_midpoint() -> None:
    proc = SegmentFrameVideoProcessor()
    assert proc._frame_timestamps_ms(0, 10_000, 1) == [5_000]


def test_frame_timestamps_empty_for_bad_input() -> None:
    proc = SegmentFrameVideoProcessor()
    assert proc._frame_timestamps_ms(0, 0, 4) == []
    assert proc._frame_timestamps_ms(0, 12_000, 0) == []


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_extracts_real_frames_per_segment(tmp_path: Path) -> None:
    clip = generate_testsrc(tmp_path / "clip.mp4", duration=6, rate=10)
    proc = SegmentFrameVideoProcessor(frame_root=str(tmp_path / "frames"))
    frames = proc.extract_frame_uris(str(clip), 0, 4_000, 3)
    assert len(frames) == 3
    for f in frames:
        p = Path(f)
        assert p.exists()
        assert p.stat().st_size > 0  # real decoded JPEG, not a placeholder path


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_long_video_multiple_segments_drive_multiple_vlm_calls(tmp_path: Path) -> None:
    # 30s clip with 12s window / 3s overlap -> multiple segments.
    clip = generate_testsrc(tmp_path / "long.mp4", duration=30, rate=5)
    proc = SegmentFrameVideoProcessor(frame_root=str(tmp_path / "frames"))
    duration_ms = proc.probe(str(clip)).duration_ms
    windows = plan_default_segments(duration_ms, window_seconds=12, overlap_seconds=3)
    assert len(windows) >= 2  # genuinely multi-segment

    # Simulate the per-segment loop the worker runs: each segment extracts real
    # frames and produces one VLM request.
    vlm_calls: list[VlmSegmentRequest] = []

    class _SpyVlm:
        def analyze_segment(self, request: VlmSegmentRequest) -> VlmObservationOutput:
            vlm_calls.append(request)
            return VlmObservationOutput(
                static="s", dynamic="d", tags=[]
            )

    vlm = _SpyVlm()
    for w in windows:
        frame_uris = proc.extract_frame_uris(str(clip), w.start_ms, w.end_ms, 4)
        assert len(frame_uris) == 4
        assert all(Path(f).exists() for f in frame_uris)
        vlm.analyze_segment(
            VlmSegmentRequest(
                request_id=f"r_{w.start_ms}",
                analysis_job_id="job",
                video_id="vid",
                camera_id="cam_lobby_01",
                analysis_scale="default_segment",  # type: ignore[arg-type]
                segment_start_ms=w.start_ms,
                segment_end_ms=w.end_ms,
                frame_uris=frame_uris,
            )
        )
    # One VLM call per segment, and the segments are distinct.
    assert len(vlm_calls) == len(windows)
    assert len({(c.segment_start_ms, c.segment_end_ms) for c in vlm_calls}) == len(windows)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_extract_frame_missing_source_raises_bounded(tmp_path: Path) -> None:
    proc = SegmentFrameVideoProcessor(
        frame_root=str(tmp_path / "frames"), ffmpeg_timeout_seconds=10
    )
    with pytest.raises(RuntimeError):
        proc.extract_frame_uris("/nonexistent/not-a-video.mp4", 0, 4_000, 2)


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_worker_ffmpeg_frames_mode_publishes_multi_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: worker in ffmpeg_frames mode extracts real frames per segment.

    Proves the C5 processor is wired into the real main path (analysis_worker
    selection by pipeline.video_metadata_mode=ffmpeg_frames) and that a long clip
    publishes multiple records (one per default_segment window).
    """
    from datetime import UTC, datetime

    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
    from cctv_memory.contracts.video import SubmitVideoSourceRequest
    from cctv_memory.domain.enums import (
        Capability,
        JobStatus,
        PrincipalType,
        SecurityLevel,
        SourceType,
    )
    from cctv_memory.infrastructure.runtime import build_runtime
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    clip = generate_testsrc(tmp_path / "media" / "long.mp4", duration=30, rate=5)
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE", "ffmpeg_frames")

    runtime = build_runtime(data_dir=str(tmp_path / "data"))
    runtime.init_storage()
    runtime.create_schema()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        from tests.conftest import seed_camera

        seed_camera(repos)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area", name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        principal = Principal(
            principal_id="svc_c5", principal_type=PrincipalType.SERVICE_ACCOUNT,
            display_name="svc", roles=["security_viewer"],
        )
        repos.principal().create_principal(principal)
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
        )
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=str(clip),
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key="c5-e2e",
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        job_id = resp.analysis_job_id

    worker = AnalysisWorker(runtime)  # selects SegmentFrameVideoProcessor from config
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        count = session.scalar(select(func.count()).select_from(orm.ObservationRecord))
        # 30s @ 12s window / 3s overlap -> multiple segments -> multiple records.
        assert count and count >= 2
    runtime.dispose()

