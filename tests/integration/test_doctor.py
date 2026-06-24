"""Doctor diagnostic tests (task cctv-memory-20260610-usage-polish-and-doctor).

The diagnostic core ``build_doctor_report`` is pure and injectable (env mapping +
``which`` callable + cwd), so every path is deterministic without touching the
real environment, a subprocess, or the network. We assert:

- default/mock config is ready for mock analysis, not for real;
- provider=real + missing key -> real NOT ready with an explicit reason;
- provider=real + key set + ffprobe-capable mode + ffprobe present -> real READY;
- static mode cannot feed real media -> real NOT ready even with a key;
- missing ffprobe blocks both mock and real for an ffprobe mode;
- vector-search readiness flips with indexing.enabled + real-embedder key;
- NEITHER text nor JSON output ever contains a secret value;
- the CLI ``doctor`` / ``doctor --json`` commands run and are truthful.
"""

from __future__ import annotations

import json

import pytest
from cctv_memory.application.doctor import build_doctor_report, render_doctor_text
from cctv_memory.cli import main
from cctv_memory.config.settings import AppConfig


def _which_all_found(_: str) -> str | None:
    return "/usr/bin/x"


def _which_none(_: str) -> str | None:
    return None


def _cfg(**overrides: object) -> AppConfig:
    """Build a config from explicit init kwargs (no ambient env/yaml needed)."""
    return AppConfig(**overrides)  # type: ignore[arg-type]


# ---- mock / default path --------------------------------------------------


def test_default_config_ready_for_mock_not_real() -> None:
    report = build_doctor_report(
        _cfg(), env={}, which=_which_all_found, cwd="/work"
    )
    assert report["vlm"]["effective"] == "mock"
    assert report["readiness"]["ready_for_mock_analysis"] is True
    assert report["readiness"]["ready_for_real_vlm_analysis"] is False
    # real reasons must mention provider not real.
    reasons = report["readiness"]["ready_for_real_vlm_analysis_reasons"]
    assert any("provider" in r for r in reasons)
    assert report["readiness"]["external_endpoint_probed"] is False


def test_base_paths_reported_from_config() -> None:
    cfg = AppConfig().with_data_dir("/tmp/somewhere")
    report = build_doctor_report(cfg, env={}, which=_which_all_found, cwd="/work")
    assert report["base"]["data_dir"] == "/tmp/somewhere"
    assert report["base"]["sqlite_path"].endswith("/tmp/somewhere/cctv_memory.sqlite3")
    assert report["base"]["cwd"] == "/work"


def test_doctor_reports_effective_concurrency_knobs() -> None:
    cfg = _cfg(
        worker={"max_concurrent_jobs": 1000, "max_unit_workers_per_job": 1000},
        vlm={"max_concurrent_requests": 500},
    )

    report = build_doctor_report(cfg, env={}, which=_which_all_found, cwd="/work")

    assert report["concurrency"] == {
        "worker_max_concurrent_jobs": 1000,
        "worker_max_unit_workers_per_job": 1000,
        "vlm_max_concurrent_requests": 500,
        "effective_vlm_call_upper_bound": 500,
        "one_unit_video_upper_bound": 500,
    }
    text = render_doctor_text(report)
    assert "[concurrency]" in text
    assert "worker.max_concurrent_jobs" in text
    assert "effective VLM cap" in text


# ---- real VLM readiness ---------------------------------------------------


def test_real_provider_missing_key_not_ready() -> None:
    cfg = _cfg(vlm={"provider": "real"})
    report = build_doctor_report(cfg, env={}, which=_which_all_found, cwd="/w")
    assert report["vlm"]["effective"] == "real"
    rd = report["readiness"]
    assert rd["ready_for_real_vlm_analysis"] is False
    assert any("api_key_env" in r for r in rd["ready_for_real_vlm_analysis_reasons"])


def test_real_provider_with_key_and_ffprobe_is_ready() -> None:
    cfg = _cfg(vlm={"provider": "real"})  # default mode ffprobe, api_key_env LLM_KEY
    report = build_doctor_report(
        cfg, env={"LLM_KEY": "x"}, which=_which_all_found, cwd="/w"
    )
    rd = report["readiness"]
    assert rd["ready_for_real_vlm_analysis"] is True
    assert rd["ready_for_real_vlm_analysis_reasons"] == []
    # api key reported as set, but value never present in the report.
    assert report["vlm"]["api_key_env_set"] is True


def test_real_provider_static_mode_cannot_feed_real_media() -> None:
    cfg = _cfg(
        vlm={"provider": "real"},
        pipeline={"video_metadata_mode": "static"},
    )
    report = build_doctor_report(
        cfg, env={"LLM_KEY": "x"}, which=_which_all_found, cwd="/w"
    )
    rd = report["readiness"]
    assert rd["ready_for_real_vlm_analysis"] is False
    assert any("static" in r for r in rd["ready_for_real_vlm_analysis_reasons"])


def test_missing_ffprobe_blocks_mock_and_real() -> None:
    cfg = _cfg(vlm={"provider": "real"})  # ffprobe mode by default
    report = build_doctor_report(
        cfg, env={"LLM_KEY": "x"}, which=_which_none, cwd="/w"
    )
    rd = report["readiness"]
    assert rd["ready_for_mock_analysis"] is False
    assert any("ffprobe" in r for r in rd["ready_for_mock_analysis_reasons"])
    assert rd["ready_for_real_vlm_analysis"] is False
    assert any("ffprobe" in r for r in rd["ready_for_real_vlm_analysis_reasons"])


def test_static_mode_needs_no_binaries_for_mock() -> None:
    cfg = _cfg(pipeline={"video_metadata_mode": "static"})
    report = build_doctor_report(cfg, env={}, which=_which_none, cwd="/w")
    assert report["pipeline"]["needs_ffprobe"] is False
    assert report["readiness"]["ready_for_mock_analysis"] is True


def test_ffmpeg_frames_mode_needs_ffmpeg() -> None:
    cfg = _cfg(pipeline={"video_metadata_mode": "ffmpeg_frames"})
    report = build_doctor_report(cfg, env={}, which=_which_none, cwd="/w")
    assert report["pipeline"]["needs_ffprobe"] is True
    assert report["pipeline"]["needs_ffmpeg"] is True
    reasons = report["readiness"]["ready_for_mock_analysis_reasons"]
    assert any("ffmpeg" in r for r in reasons)


# ---- vector search readiness ----------------------------------------------


def test_vector_search_not_ready_when_disabled() -> None:
    report = build_doctor_report(_cfg(), env={}, which=_which_all_found, cwd="/w")
    rd = report["readiness"]
    assert rd["ready_for_vector_search"] is False
    assert any("indexing.enabled" in r for r in rd["ready_for_vector_search_reasons"])


def test_vector_search_ready_with_mock_embedder_enabled() -> None:
    cfg = _cfg(indexing={"enabled": True})  # provider mock by default
    report = build_doctor_report(cfg, env={}, which=_which_all_found, cwd="/w")
    assert report["readiness"]["ready_for_vector_search"] is True


def test_vector_search_real_embedder_requires_key() -> None:
    cfg = _cfg(indexing={"enabled": True, "provider": "real"})
    report_missing = build_doctor_report(
        cfg, env={}, which=_which_all_found, cwd="/w"
    )
    assert report_missing["readiness"]["ready_for_vector_search"] is False
    report_ok = build_doctor_report(
        cfg,
        env={"CCTV_MEMORY_EMBEDDING_API_KEY": "x"},
        which=_which_all_found,
        cwd="/w",
    )
    assert report_ok["readiness"]["ready_for_vector_search"] is True


def test_vector_search_real_rerank_requires_key() -> None:
    cfg = _cfg(
        indexing={"enabled": True, "rerank_enabled": True, "rerank_provider": "real"}
    )
    report = build_doctor_report(cfg, env={}, which=_which_all_found, cwd="/w")
    rd = report["readiness"]
    assert rd["ready_for_vector_search"] is False
    assert any("rerank" in r for r in rd["ready_for_vector_search_reasons"])


# ---- media input (frames default) ----------------------------------------


def test_doctor_reports_default_media_input_frames() -> None:
    report = build_doctor_report(_cfg(), env={}, which=_which_all_found, cwd="/w")
    assert report["vlm"]["media_input"] == "frames"
    assert report["vlm"]["include_audio"] is False


def test_doctor_real_frames_uses_opencv_processor_by_default() -> None:
    cfg = _cfg(vlm={"provider": "real"})  # default media_input=frames, backend=opencv
    report = build_doctor_report(cfg, env={"LLM_KEY": "x"}, which=_which_none, cwd="/w")
    assert report["pipeline"]["active_video_processor"] == "OpenCvFrameStreamVideoProcessor"
    assert report["pipeline"]["decode_backend"] == "opencv"
    # OpenCV decode needs ffmpeg only for the fallback (enabled by default).
    assert report["pipeline"]["needs_ffmpeg"] is True
    rd = report["readiness"]
    assert rd["ready_for_real_vlm_analysis"] is False
    assert any("ffmpeg" in r for r in rd["ready_for_real_vlm_analysis_reasons"])


def test_doctor_real_frames_ffmpeg_backend_uses_segment_processor() -> None:
    cfg = _cfg(vlm={"provider": "real"}, pipeline={"decode_backend": "ffmpeg"})
    report = build_doctor_report(cfg, env={"LLM_KEY": "x"}, which=_which_none, cwd="/w")
    assert report["pipeline"]["active_video_processor"] == "SegmentFrameVideoProcessor"
    assert report["pipeline"]["needs_ffmpeg"] is True
    rd = report["readiness"]
    assert rd["ready_for_real_vlm_analysis"] is False
    assert any("ffmpeg" in r for r in rd["ready_for_real_vlm_analysis_reasons"])


def test_doctor_real_video_mode_uses_whole_clip_processor() -> None:
    cfg = _cfg(vlm={"provider": "real", "media_input": "video"})
    report = build_doctor_report(cfg, env={"LLM_KEY": "x"}, which=_which_all_found, cwd="/w")
    assert report["pipeline"]["active_video_processor"] == "WholeClipVideoProcessor"
    # video mode + default include_audio=false strips audio -> needs ffmpeg.
    assert report["pipeline"]["needs_ffmpeg"] is True
    assert report["readiness"]["ready_for_real_vlm_analysis"] is True


def test_doctor_real_video_include_audio_skips_ffmpeg_strip() -> None:
    cfg = _cfg(vlm={"provider": "real", "media_input": "video", "include_audio": True})
    report = build_doctor_report(cfg, env={"LLM_KEY": "x"}, which=_which_all_found, cwd="/w")
    # include_audio=true passes the source through, no ffmpeg strip needed.
    assert report["pipeline"]["needs_ffmpeg"] is False
    assert report["vlm"]["include_audio"] is True


# ---- secret safety --------------------------------------------------------


def test_report_and_text_never_leak_secret_values() -> None:
    secret = "SUPER-SECRET-abc123"
    cfg = _cfg(
        vlm={"provider": "real"},
        indexing={"enabled": True, "provider": "real"},
    )
    env = {
        "LLM_KEY": secret,
        "CCTV_MEMORY_EMBEDDING_API_KEY": secret,
        "CCTV_MEMORY_VLM_BASE_URL": "http://secret-host/" + secret,
    }
    report = build_doctor_report(cfg, env=env, which=_which_all_found, cwd="/w")
    blob = json.dumps(report)
    assert secret not in blob
    assert secret not in render_doctor_text(report)
    # but presence flags are truthful
    assert report["vlm"]["api_key_env_set"] is True
    assert report["vlm"]["base_url_env_set"] is True


def test_text_render_contains_key_sections() -> None:
    text = render_doctor_text(
        build_doctor_report(_cfg(), env={}, which=_which_all_found, cwd="/w")
    )
    for needle in ("[base]", "[vlm]", "[pipeline]", "[readiness]", "effective path"):
        assert needle in text


# ---- CLI smoke ------------------------------------------------------------


def test_cli_doctor_runs(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doctor", "--data-dir", "/tmp/doc_cli"])
    out = capsys.readouterr().out
    assert code == 0
    assert "cctv-memory doctor" in out
    assert "readiness" in out


def test_cli_doctor_json_runs(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doctor", "--json", "--data-dir", "/tmp/doc_cli"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert "readiness" in payload
    assert payload["base"]["data_dir"] == "/tmp/doc_cli"
