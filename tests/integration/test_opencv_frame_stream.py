"""Stage A & B — OpenCV FrameStream + selector + bounded cache + cleanup tests.

Split into:
- PURE unit tests (no cv2/numpy): the domain selector and the ring buffer logic.
- cv2-gated tests: the OpenCV adapter (decode/score/select/materialize), ffmpeg
  fallback, both default_segment and high_freq_event end-to-end on the OpenCV
  default backend, metadata_only cleanup, debug retention, and media-ref safety.

cv2-gated tests use ``importlib.util.find_spec`` so CI without OpenCV skips them
rather than failing (frame-stream-selector-cache-design §9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

import pytest
from cctv_memory.application.ingestion import (
    OPENCV_PIPELINE_VERSION,
    IngestionService,
)
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.domain.enums import (
    AnalysisScale,
    Capability,
    JobStatus,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.domain.policies import FrameScore, select_frames
from cctv_memory.workers.analysis_worker import AnalysisWorker

from tests.conftest import seed_camera
from tests.support.video_gen import ffmpeg_available, generate_testsrc

_CV2 = find_spec("cv2") is not None and find_spec("numpy") is not None
cv2_required = pytest.mark.skipif(not _CV2, reason="cv2/numpy not installed")


# ===========================================================================
# PURE: domain selector (no cv2)
# ===========================================================================


def _scores(n: int, *, motion=None, blur=100.0, bright=128.0) -> list[FrameScore]:
    return [
        FrameScore(
            frame_index=i,
            timestamp_ms=i * 100,
            motion=(motion[i] if motion else 0.1),
            scene=0.1,
            blur=blur,
            brightness=bright,
        )
        for i in range(n)
    ]


def test_select_frames_empty_and_zero_budget() -> None:
    assert select_frames([], 5) == []
    assert select_frames(_scores(5), 0) == []


def test_select_frames_uniform_count_and_order() -> None:
    out = select_frames(_scores(10), 4, strategy="uniform")
    assert len(out) == 4
    ts = [s.timestamp_ms for s in out]
    assert ts == sorted(ts)  # chronological


def test_select_frames_score_picks_motion_peaks() -> None:
    motion = [0.0] * 10
    motion[3] = 0.9
    motion[7] = 0.8
    out = select_frames(_scores(10, motion=motion), 2, strategy="score", w_motion=1.0)
    assert {s.frame_index for s in out} == {3, 7}
    # still returned chronologically
    assert [s.timestamp_ms for s in out] == sorted(s.timestamp_ms for s in out)


def test_select_frames_bins_then_score_keeps_temporal_coverage() -> None:
    # All motion concentrated in the first 2 frames; bins must still spread picks.
    motion = [0.9, 0.9] + [0.0] * 8
    out = select_frames(_scores(10, motion=motion), 4, strategy="bins_then_score")
    assert len(out) == 4
    idx = [s.frame_index for s in out]
    # coverage: not all four crammed into the first cluster
    assert max(idx) - min(idx) >= 5
    assert idx == sorted(idx)


def test_select_frames_quality_gate_drops_blurry_but_never_empty() -> None:
    # All blurry -> gate would drop everything -> gate ignored (non-empty result).
    out = select_frames(_scores(5, blur=1.0), 2, min_blur=50.0)
    assert len(out) == 2


def test_select_frames_deterministic_tiebreak() -> None:
    # Identical scores: tie-break by frame_index is deterministic + repeatable.
    a = select_frames(_scores(10), 3, strategy="score")
    b = select_frames(_scores(10), 3, strategy="score")
    assert [s.frame_index for s in a] == [s.frame_index for s in b]


# ===========================================================================
# PURE: ring buffer (no cv2 — uses objects with a .nbytes attribute)
# ===========================================================================


class _FakeArr:
    def __init__(self, nbytes: int) -> None:
        self.nbytes = nbytes


def test_ring_buffer_evicts_by_maxlen_and_derefs() -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import _RingBuffer

    rb = _RingBuffer(maxlen=3, max_bytes=10**9)
    for i in range(6):
        rb.append(i, _FakeArr(100))
    assert len(rb) == 3
    assert rb.get(0) is None  # oldest evicted (dereferenced)
    assert rb.get(5) is not None  # newest retained


def test_ring_buffer_evicts_by_byte_cap() -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import _RingBuffer

    rb = _RingBuffer(maxlen=1000, max_bytes=250)  # ~2 x 100-byte frames
    for i in range(5):
        rb.append(i, _FakeArr(100))
    assert rb.current_bytes <= 250
    assert len(rb) <= 2


def test_ring_buffer_clear_drops_everything() -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import _RingBuffer

    rb = _RingBuffer(maxlen=10, max_bytes=10**9)
    for i in range(5):
        rb.append(i, _FakeArr(100))
    rb.clear()
    assert len(rb) == 0
    assert rb.current_bytes == 0


# ===========================================================================
# cv2-gated: OpenCV adapter
# ===========================================================================


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_opencv_adapter_materializes_selected_jpegs_with_metadata(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=5, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"), sample_fps=8, buffer_seconds=4
    )
    refs = proc.extract_selected_frames(str(clip), 0, 4000, 6)
    assert len(refs) == 6
    # all written, decodable JPEGs, with stream identity + scalar metadata
    for r in refs:
        assert Path(r.uri).exists()
        assert Path(r.uri).stat().st_size > 0
        assert r.decode_backend == "opencv"
        assert r.timestamp_ms >= 0
    ts = [r.timestamp_ms for r in refs]
    assert ts == sorted(ts)  # chronological for the VLM


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_opencv_extract_frame_uris_returns_list_str(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=4, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(frame_root=str(tmp_path / "frames"))
    uris = proc.extract_frame_uris(str(clip), 0, 3000, 4)
    assert len(uris) == 4
    assert all(isinstance(u, str) and Path(u).exists() for u in uris)


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_opencv_tiny_buffer_reseeks_evicted_frames(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=5, rate=10)
    # buffer_seconds tiny so selected frames are evicted -> re-seek path exercised
    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"),
        sample_fps=8,
        buffer_seconds=0.2,
        max_buffer_bytes=10**9,
    )
    refs = proc.extract_selected_frames(str(clip), 0, 4000, 6)
    assert len(refs) == 6
    assert all(Path(r.uri).exists() for r in refs)


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed for fallback")
def test_opencv_fallback_to_ffmpeg_on_decode_failure(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=4, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"), decode_fallback_to_ffmpeg=True
    )
    # _fallback_or_raise mirrors the path taken when opencv decode raises.
    refs = proc._fallback_or_raise(str(clip), 0, 3000, 4, reason="forced")
    assert len(refs) == 4
    assert all(r.decode_backend == "ffmpeg" for r in refs)
    assert all(Path(r.uri).exists() for r in refs)


def test_opencv_fallback_disabled_raises(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"), decode_fallback_to_ffmpeg=False
    )
    with pytest.raises(RuntimeError, match="fallback disabled"):
        proc._fallback_or_raise("/nope.mp4", 0, 3000, 4, reason="x")


@cv2_required
def test_opencv_zero_frames_near_eof_raises_insufficient_frames(tmp_path: Path) -> None:
    """A window entirely past the clip's real end decodes zero frames and signals
    InsufficientFramesError (skip, not a hard failure) — task §A near-EOF case."""
    from cctv_memory.domain.exceptions import InsufficientFramesError
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )

    clip = generate_testsrc(tmp_path / "clip.mp4", duration=2, rate=10)
    proc = OpenCvFrameStreamVideoProcessor(
        frame_root=str(tmp_path / "frames"),
        # fallback off so we observe the zero-frame signal directly (not ffmpeg).
        decode_fallback_to_ffmpeg=False,
    )
    # Window [10s, 12s) is well past the 2s clip end -> zero decodable frames.
    with pytest.raises(InsufficientFramesError):
        proc.extract_selected_frames(str(clip), 10_000, 12_000, 6)



# ===========================================================================
# cv2-gated: end-to-end on the OpenCV default backend (both scales) + cleanup
# ===========================================================================


def _vlm_output() -> VlmObservationOutput:
    return VlmObservationOutput(
        static="s",
        dynamic="d",
        tags=["person"],
        quality={"reason": "", "score": 0.9},
        attr={"alert": False},
    )


class _CapturingVlm:
    """Records the frame_uris the VLM received (verifies it reads real files)."""

    def __init__(self) -> None:
        self.seen_uris: list[list[str]] = []

    def analyze_segment(self, request):  # type: ignore[no-untyped-def]
        # Prove the selected frames exist and are readable at call time.
        for uri in request.frame_uris:
            assert Path(uri).exists(), f"VLM frame missing: {uri}"
        self.seen_uris.append(list(request.frame_uris))
        return _vlm_output()


def _seed(runtime, pid: str) -> None:  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_camera(repos)
        repos.access_policy().upsert_access_policy(
            AccessPolicy(
                access_policy_id="policy_public_area",
                name="Public Area",
                security_level=SecurityLevel.INTERNAL,
                rules=AccessPolicyRules(allowed_roles=["security_viewer"]),
            )
        )
        repos.principal().create_principal(
            Principal(
                principal_id=pid,
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )


def _submit(runtime, clip: Path, *, pid: str, key: str, hf: bool):  # type: ignore[no-untyped-def]
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
            pipeline_version=OPENCV_PIPELINE_VERSION,
        )
        principal = repos.principal().get_principal(pid)
        assert principal is not None
        opts = {"enable_default_segment": True}
        if hf:
            opts["enable_motion_triggered_high_freq"] = True
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=str(clip),
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 11, 15, 0, tzinfo=UTC),
                idempotency_key=key,
                analysis_options=opts,
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
    return resp.analysis_job_id


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_default_segment_uses_opencv_backend_and_vlm_reads_frames(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.runtime import build_runtime

    clip = generate_testsrc(tmp_path / "media" / "clip.mp4", duration=8, rate=10)
    runtime = build_runtime(data_dir=str(tmp_path / "data"))
    runtime.init_storage()
    runtime.create_schema()
    # real provider so frames path is taken; decode_backend defaults to opencv.
    runtime.config.vlm.provider = "real"
    assert runtime.config.pipeline.decode_backend == "opencv"
    _seed(runtime, "svc_ds")
    job_id = _submit(runtime, clip, pid="svc_ds", key="ds-1", hf=False)

    vlm = _CapturingVlm()
    worker = AnalysisWorker(runtime, vlm=vlm)  # default video processor = opencv
    assert worker.process_one() is not None

    assert vlm.seen_uris, "VLM was never called"
    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED
        # honest pipeline_version for the opencv selector path
        assert job.pipeline_version == OPENCV_PIPELINE_VERSION

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        logs = list(session.scalars(select(orm.ModelCallLog)))
        assert logs
        import json as _json

        found_frame_meta = False
        for log in logs:
            refs = _json.loads(log.media_refs_json)
            for ref in refs:
                blob = _json.dumps(ref).lower()
                assert "base64" not in blob
                assert "source_uri" not in ref
                if "frame_index" in ref and "timestamp_ms" in ref:
                    found_frame_meta = True
                    assert ref["decode_backend"] == "opencv"
                    assert "sha256" in ref and "size_bytes" in ref
        assert found_frame_meta, "opencv refs must carry frame_index/timestamp_ms"
    runtime.dispose()


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_metadata_only_cleanup_removes_selected_frames(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.runtime import build_runtime

    clip = generate_testsrc(tmp_path / "media" / "clip.mp4", duration=8, rate=10)
    runtime = build_runtime(data_dir=str(tmp_path / "data"))
    runtime.init_storage()
    runtime.create_schema()
    runtime.config.vlm.provider = "real"
    assert runtime.config.vlm.media_log_mode == "metadata_only"
    assert runtime.config.pipeline.frame_stream.cleanup_selected_on_success is True
    _seed(runtime, "svc_clean")
    _submit(runtime, clip, pid="svc_clean", key="clean-1", hf=False)

    captured: list[str] = []

    class _RecordingVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            captured.extend(request.frame_uris)
            return _vlm_output()

    worker = AnalysisWorker(runtime, vlm=_RecordingVlm())
    assert worker.process_one() is not None
    assert captured, "no frames captured"
    # cleanup_selected_on_success=true + metadata_only -> working frames removed
    for uri in captured:
        assert not Path(uri).exists(), f"selected frame not cleaned up: {uri}"
    runtime.dispose()


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_debug_full_media_preserves_artifacts_and_keeps_frames(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.runtime import build_runtime

    clip = generate_testsrc(tmp_path / "media" / "clip.mp4", duration=8, rate=10)
    runtime = build_runtime(data_dir=str(tmp_path / "data"))
    runtime.init_storage()
    runtime.create_schema()
    runtime.config.vlm.provider = "real"
    runtime.config.vlm.debug_media_retention = True
    runtime.config.vlm.media_log_mode = "debug_full_media"
    _seed(runtime, "svc_dbg")
    _submit(runtime, clip, pid="svc_dbg", key="dbg-1", hf=False)

    captured: list[str] = []

    class _RecordingVlm:
        def analyze_segment(self, request):  # type: ignore[no-untyped-def]
            captured.extend(request.frame_uris)
            return _vlm_output()

    worker = AnalysisWorker(runtime, vlm=_RecordingVlm())
    assert worker.process_one() is not None

    # debug retention: working frames NOT cleaned up, artifact copies recorded.
    assert captured
    assert any(Path(u).exists() for u in captured), "debug mode must keep frames"
    with runtime.session() as session:
        import json as _json

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import select

        logs = list(session.scalars(select(orm.ModelCallLog)))
        assert logs
        saw_artifact = False
        for log in logs:
            for ref in _json.loads(log.media_refs_json):
                if "artifact_uri" in ref:
                    saw_artifact = True
                    assert Path(ref["artifact_uri"]).exists()
        assert saw_artifact, "debug mode must record artifact_uri refs"
    runtime.dispose()


@cv2_required
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg needed to synth clip")
def test_high_freq_event_uses_opencv_backend_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cctv_memory.infrastructure.runtime import build_runtime

    clip = generate_testsrc(tmp_path / "media" / "moving.mp4", duration=10, rate=10)
    # Calibrate motion threshold for the gradual synthetic testsrc (see existing
    # multi-scale e2e test rationale).
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__MOTION_SCAN__THRESHOLD", "0.005")
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__MOTION_SCAN__MIN_DURATION_MS", "1000")

    runtime = build_runtime(data_dir=str(tmp_path / "data"))
    runtime.init_storage()
    runtime.create_schema()
    runtime.config.vlm.provider = "real"
    assert runtime.config.pipeline.decode_backend == "opencv"
    _seed(runtime, "svc_hf")
    job_id = _submit(runtime, clip, pid="svc_hf", key="hf-1", hf=True)

    vlm = _CapturingVlm()
    worker = AnalysisWorker(runtime, vlm=vlm)  # opencv backend, real detector
    assert worker.process_one() is not None

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert len(repos.trigger().list_by_job(job_id)) >= 1

        from cctv_memory.infrastructure.db.models import tables as orm
        from sqlalchemy import func, select

        hf_count = session.scalar(
            select(func.count())
            .select_from(orm.ObservationRecord)
            .where(
                orm.ObservationRecord.analysis_scale
                == AnalysisScale.HIGH_FREQ_EVENT.value
            )
        )
        assert hf_count and hf_count >= 1

        # high_freq ModelCallLogs carry opencv frame metadata, no base64.
        import json as _json

        hf_logs = [
            log
            for log in session.scalars(select(orm.ModelCallLog))
            if log.analysis_scale == AnalysisScale.HIGH_FREQ_EVENT.value
        ]
        assert hf_logs
        for log in hf_logs:
            for ref in _json.loads(log.media_refs_json):
                assert "base64" not in _json.dumps(ref).lower()
    runtime.dispose()
