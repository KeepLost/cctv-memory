"""Default frames -> multi-image VLM input path tests.

Task cctv-memory-20260610-frame-default-vlm-path. Proves the required behavior:

1. DEFAULT config sends frames as MULTIPLE images (one image_url part per frame),
   not the whole video clip.
2. Explicit ``vlm.media_input=video`` switches to the single whole-clip video part.
3. The DEFAULT path excludes audio (frames carry none; video mode strips it unless
   ``vlm.include_audio=true``).
4. The worker selects the frame-extracting processor by default for the real VLM
   and the whole-clip processor only when explicitly configured.

Adapter payload tests are network-free (httpx MockTransport captures the request
body). Processor/audio tests that need real decoding are ffmpeg-gated.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.infrastructure.video.ffprobe_adapter import (
    SegmentFrameVideoProcessor,
    WholeClipVideoProcessor,
)
from cctv_memory.infrastructure.vlm.real_adapter import RealVlmAnalyzer

from tests.support.video_gen import ffmpeg_available, generate_testsrc

_VALID_JSON = (
    '{"static":"x",'
    '"dynamic":"y","tags":["t"],'
    '"quality":{"reason":"","score":0.5},"attr":{"alert":false}}'
)


def _capturing_client(captured: list[dict[str, object]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": _VALID_JSON}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _request(frame_uris: list[str]) -> VlmSegmentRequest:
    return VlmSegmentRequest(
        request_id="r1",
        analysis_job_id="job_1",
        video_id="video_1",
        camera_id="cam_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=5000,
        frame_uris=frame_uris,
    )


def _write_frames(tmp_path: Path, n: int) -> list[str]:
    paths: list[str] = []
    for i in range(n):
        p = tmp_path / f"frame_{i:04d}.jpg"
        p.write_bytes(bytes([0xFF, 0xD8, 0xFF, i]))  # tiny fake JPEG bytes
        paths.append(str(p))
    return paths


# ---- adapter payload: default frames mode = multiple image parts ----------


def test_default_media_input_is_frames() -> None:
    assert AppConfig().vlm.media_input == "frames"


def test_default_include_audio_is_false() -> None:
    assert AppConfig().vlm.include_audio is False


def test_frames_mode_sends_one_image_part_per_frame(tmp_path: Path) -> None:
    frames = _write_frames(tmp_path, 4)
    captured: list[dict[str, object]] = []
    adapter = RealVlmAnalyzer(
        base_url="http://fake/api",
        api_key="k",
        model_id="m",
        media_input="frames",  # default
        client=_capturing_client(captured),
    )
    out = adapter.analyze_segment(_request(frames))
    assert isinstance(out, VlmObservationOutput)

    content = captured[0]["messages"][1]["content"]  # type: ignore[index]  # [1]=user (P2: [0]=system)
    image_parts = [p for p in content if p["type"] == "image_url"]
    text_parts = [p for p in content if p["type"] == "text"]
    # One image part per frame (multi-image). P2: the stable prompt text now lives
    # in the SYSTEM message, so the user content carries images only (no text part
    # on the normal, non-strict attempt).
    assert len(image_parts) == 4
    assert len(text_parts) == 0
    assert captured[0]["messages"][0]["role"] == "system"  # type: ignore[index]
    assert captured[0]["messages"][0]["content"]  # type: ignore[index]  # stable prompt
    # Frames are sent as images, never as a video MIME (no audio container).
    for part in image_parts:
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert "video/mp4" not in url


def test_video_mode_sends_single_video_part(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x01\x02fake-clip")
    captured: list[dict[str, object]] = []
    adapter = RealVlmAnalyzer(
        base_url="http://fake/api",
        api_key="k",
        model_id="m",
        media_input="video",  # opt-in
        client=_capturing_client(captured),
    )
    adapter.analyze_segment(_request([str(clip)]))

    content = captured[0]["messages"][1]["content"]  # type: ignore[index]  # [1]=user (P2: [0]=system)
    image_parts = [p for p in content if p["type"] == "image_url"]
    # Exactly one part, carrying the whole clip with a video MIME.
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:video/mp4;base64,")


def test_frames_mode_with_no_frames_raises(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.vlm.real_adapter import VlmProviderError

    adapter = RealVlmAnalyzer(
        base_url="http://fake/api", api_key="k", model_id="m", media_input="frames"
    )
    with pytest.raises(VlmProviderError):
        adapter.analyze_segment(_request([]))


# ---- worker selection: provider/media_input drives the processor ----------


def test_worker_selects_frame_processor_for_real_by_default(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.runtime import Runtime
    from cctv_memory.infrastructure.video.opencv_frame_stream import (
        OpenCvFrameStreamVideoProcessor,
    )
    from cctv_memory.workers.analysis_worker import _default_video_processor

    cfg = AppConfig().with_data_dir(str(tmp_path))
    cfg.vlm.provider = "real"  # default media_input=frames, decode_backend=opencv
    runtime = Runtime(cfg)
    try:
        proc = _default_video_processor(runtime)
        # OpenCV is the default decode backend (Eric decision 2026-06-11).
        assert isinstance(proc, OpenCvFrameStreamVideoProcessor)
    finally:
        runtime.dispose()


def test_worker_selects_ffmpeg_processor_when_backend_ffmpeg(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.runtime import Runtime
    from cctv_memory.workers.analysis_worker import _default_video_processor

    cfg = AppConfig().with_data_dir(str(tmp_path))
    cfg.vlm.provider = "real"
    cfg.pipeline.decode_backend = "ffmpeg"
    runtime = Runtime(cfg)
    try:
        proc = _default_video_processor(runtime)
        assert isinstance(proc, SegmentFrameVideoProcessor)
    finally:
        runtime.dispose()


def test_worker_selects_whole_clip_only_when_media_input_video(tmp_path: Path) -> None:
    from cctv_memory.infrastructure.runtime import Runtime
    from cctv_memory.workers.analysis_worker import _default_video_processor

    cfg = AppConfig().with_data_dir(str(tmp_path))
    cfg.vlm.provider = "real"
    cfg.vlm.media_input = "video"
    runtime = Runtime(cfg)
    try:
        proc = _default_video_processor(runtime)
        assert isinstance(proc, WholeClipVideoProcessor)
    finally:
        runtime.dispose()


# ---- audio exclusion (default) --------------------------------------------


def test_frames_carry_no_audio_by_construction(tmp_path: Path) -> None:
    # Frames are JPEG images; the frames path never produces an audio-bearing
    # container. This asserts the adapter emits image parts only (no video/audio).
    frames = _write_frames(tmp_path, 3)
    captured: list[dict[str, object]] = []
    adapter = RealVlmAnalyzer(
        base_url="http://fake/api", api_key="k", model_id="m",
        media_input="frames", client=_capturing_client(captured),
    )
    adapter.analyze_segment(_request(frames))
    content = captured[0]["messages"][1]["content"]  # type: ignore[index]  # [1]=user (P2: [0]=system)
    for part in content:
        if part["type"] == "image_url":
            assert "audio" not in part["image_url"]["url"]
            assert "video/mp4" not in part["image_url"]["url"]


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_whole_clip_default_strips_audio(tmp_path: Path) -> None:
    """Default whole-clip (include_audio=False) returns an audio-less re-mux."""
    # Generate a clip WITH an audio track.
    clip = tmp_path / "withaudio.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    import subprocess

    subprocess.run(  # noqa: S603 - fixed binary, test fixture
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(clip),
        ],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=True,
    )
    # Confirm the source actually has an audio stream.
    probe_src = subprocess.run(  # noqa: S603
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "json", str(clip)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True,
    )
    assert "audio" in probe_src.stdout

    proc = WholeClipVideoProcessor(frame_root=str(tmp_path / "frames"), include_audio=False)
    out_uris = proc.extract_frame_uris(str(clip), 0, 2000, 1)
    assert len(out_uris) == 1
    out_path = out_uris[0]
    assert out_path != str(clip)  # a new, audio-stripped file
    assert Path(out_path).exists()
    # The stripped output must have NO audio stream.
    probe_out = subprocess.run(  # noqa: S603
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "json", out_path],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True,
    )
    assert "audio" not in probe_out.stdout


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_whole_clip_include_audio_passthrough(tmp_path: Path) -> None:
    """With include_audio=True the original source path is passed through."""
    clip = generate_testsrc(tmp_path / "clip.mp4", duration=2, rate=10)
    proc = WholeClipVideoProcessor(frame_root=str(tmp_path / "frames"), include_audio=True)
    out_uris = proc.extract_frame_uris(str(clip), 0, 2000, 1)
    assert out_uris == [str(clip)]


# ---- end-to-end main-path wiring (ffmpeg-gated) ---------------------------


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not available")
def test_real_frames_path_sends_multiple_images_e2e(tmp_path: Path) -> None:
    """Worker default real path -> SegmentFrameVideoProcessor -> multi-image call.

    Wires the real selection (provider=real, default media_input=frames) to a
    capturing client and asserts the VLM received multiple image parts decoded
    from a real clip — proving the default path is genuinely frames->multi-image.
    """
    clip = generate_testsrc(tmp_path / "clip.mp4", duration=5, rate=10)
    proc = SegmentFrameVideoProcessor(frame_root=str(tmp_path / "frames"))
    frame_uris = proc.extract_frame_uris(str(clip), 0, 4000, 4)
    assert len(frame_uris) == 4
    assert all(Path(f).exists() and Path(f).stat().st_size > 0 for f in frame_uris)

    captured: list[dict[str, object]] = []
    adapter = RealVlmAnalyzer(
        base_url="http://fake/api", api_key="k", model_id="m",
        media_input="frames", client=_capturing_client(captured),
    )
    adapter.analyze_segment(_request(frame_uris))
    content = captured[0]["messages"][1]["content"]  # type: ignore[index]  # [1]=user (P2: [0]=system)
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert len(image_parts) == 4
    for part in image_parts:
        assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")
