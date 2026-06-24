"""M2 tests: ffprobe adapter, mock VLM determinism, default_segment worker."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.domain.policies import plan_default_segments
from cctv_memory.infrastructure.video.ffprobe_adapter import (
    FfprobeVideoProcessor,
    StaticVideoProcessor,
)
from cctv_memory.infrastructure.vlm.mock_adapter import MockVlmAnalyzer


def test_plan_default_segments_windows_and_overlap() -> None:
    windows = plan_default_segments(30_000, window_seconds=12, overlap_seconds=3)
    assert windows[0].start_ms == 0
    assert windows[0].end_ms == 12_000
    # step = 12 - 3 = 9s
    assert windows[1].start_ms == 9_000
    assert windows[-1].end_ms == 30_000


def test_plan_default_segments_zero_duration() -> None:
    assert plan_default_segments(0, window_seconds=12, overlap_seconds=3) == []


def test_mock_vlm_is_deterministic_and_has_no_policy_fields() -> None:
    req = VlmSegmentRequest(
        request_id="r1",
        analysis_job_id="job_1",
        video_id="video_1",
        camera_id="cam_lobby_01",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=12_000,
        frame_uris=["f0", "f1"],
    )
    vlm = MockVlmAnalyzer()
    out1 = vlm.analyze_segment(req)
    out2 = vlm.analyze_segment(req)
    assert isinstance(out1, VlmObservationOutput)
    assert out1.model_dump() == out2.model_dump()
    # No policy/security keys can exist (extra="forbid" guarantees this at the
    # type level); assert the output dump does not contain them either.
    dumped = out1.model_dump()
    assert "access_policy_id" not in dumped
    assert "security_level" not in dumped


def test_static_video_processor_probe_and_frames() -> None:
    proc = StaticVideoProcessor(duration_ms=24_000)
    assert proc.probe("/x.mp4").duration_ms == 24_000
    frames = proc.extract_frame_uris("/x.mp4", 0, 12_000, 6)
    assert len(frames) == 6


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available")
def test_ffprobe_missing_file_raises_quickly_without_hanging() -> None:
    # ffprobe on a non-existent path must fail deterministically (bounded), not
    # block. stdin=DEVNULL + timeout in the adapter guarantee termination.
    proc = FfprobeVideoProcessor(frame_root="/tmp", timeout_seconds=10)
    with pytest.raises(RuntimeError):
        proc.probe("/nonexistent/definitely-not-a-video.mp4")


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not available")
def test_ffprobe_probes_real_generated_clip(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available to synthesize a clip")
    clip = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=128x128:rate=10",
            str(clip),
        ],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    proc = FfprobeVideoProcessor(frame_root=str(tmp_path))
    meta = proc.probe(str(clip))
    assert 1500 <= meta.duration_ms <= 2500


def test_worker_processes_job_and_publishes_records(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    runtime = runtime_factory()
    # Seed camera/location/policy/principal.
    with runtime.session() as session:
        repos = runtime.repositories(session)
        from tests.conftest import seed_camera

        seed_camera(repos)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        principal = Principal(
            principal_id="svc_1",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            display_name="svc",
            roles=["security_viewer"],
        )
        repos.principal().create_principal(principal)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key="k1",
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        job_id = resp.analysis_job_id
        video_id = resp.video_id

    worker = AnalysisWorker(runtime, video_processor=StaticVideoProcessor(duration_ms=30_000))
    processed = worker.process_one()
    assert processed is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED
        source = repos.video_source().get_by_id(video_id)
        assert source is not None
        assert source.duration_ms == 30_000
        # Records were published (default_segment windows over 30s).
        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        count = session.scalar(select(func.count()).select_from(orm.ObservationRecord))
        assert count and count >= 3
    runtime.dispose()


def test_worker_marks_job_failed_when_probe_fails(runtime_factory) -> None:  # type: ignore[no-untyped-def]
    """A processing failure must yield job=failed (legal running->failed), not queued."""
    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.services.video_processor import VideoMetadata
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    class FailingVideoProcessor:
        def probe(self, source_uri: str) -> VideoMetadata:
            raise RuntimeError("ffprobe failed to read source")

        def extract_frame_uris(
            self, source_uri: str, segment_start_ms: int, segment_end_ms: int,
            frame_count: int, *, unit_key: str | None = None,
        ) -> list[str]:
            return []

    runtime = runtime_factory()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        from tests.conftest import seed_camera

        seed_camera(repos)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        principal = Principal(
            principal_id="svc_2",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            display_name="svc",
            roles=["security_viewer"],
        )
        repos.principal().create_principal(principal)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/data/videos/lobby.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 6, 21, 0, tzinfo=UTC),
                idempotency_key="kfail",
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        job_id = resp.analysis_job_id

    worker = AnalysisWorker(runtime, video_processor=FailingVideoProcessor())
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.FAILED
        assert job.error_code == "video_decode_error"
    runtime.dispose()
