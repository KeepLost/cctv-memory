"""Pre-run diagnostics (application/doctor.py).

``doctor`` answers a single honest question: *given the configuration that is
actually in effect right now, what path will this system take, and what (if
anything) is missing before it can run?*

Design rules (task cctv-memory-20260610-usage-polish-and-doctor):
- Pure + injectable: takes the resolved ``AppConfig`` plus an ``env`` mapping, a
  ``which`` callable (binary lookup), and ``cwd`` so it is deterministic and
  unit-testable without touching the real environment, filesystem subprocess, or
  network.
- Mirrors — never reinvents — the real selection logic in
  ``workers/analysis_worker._default_vlm`` / ``_default_video_processor`` so the
  report cannot drift from runtime behavior.
- NEVER prints a secret value. For every secret it reports the env-var NAME and a
  boolean "is it set", nothing else (configuration-contract §6).
- Does NOT open the database or probe external endpoints. Readiness is *local
  configuration* readiness; endpoint reachability is explicitly reported as
  "not probed" so the tool never falsely claims a remote service works.

Application layer: imports ``cctv_memory.config`` only (allowed by the
architecture dependency tests); no infrastructure/db/framework imports.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cctv_memory.config.settings import AppConfig, resolved_config_file

# Modes that can feed REAL media bytes to a real VLM. ``static`` produces only
# placeholder frame URIs the real adapter cannot read, so real analysis needs a
# real-media-capable mode.
_REAL_MEDIA_MODES = frozenset({"ffprobe", "ffmpeg_frames"})

EnvMap = Mapping[str, str]
WhichFn = Callable[[str], str | None]


def _selected_processor(config: AppConfig, provider: str) -> str:
    """Mirror ``workers/_default_video_processor`` selection (name only).

    ``provider`` is passed explicitly so the mock and real readiness lines can
    each report the processor THEY would select (the choice is provider-aware).
    """
    mode = config.pipeline.video_metadata_mode
    if mode == "static":
        return "StaticVideoProcessor"
    if mode == "ffmpeg_frames":
        return "SegmentFrameVideoProcessor"
    if provider == "real":
        if config.vlm.media_input == "video":
            return "WholeClipVideoProcessor"
        # frames path honors decode_backend: opencv (default) vs ffmpeg.
        if config.pipeline.decode_backend == "opencv":
            return "OpenCvFrameStreamVideoProcessor"
        return "SegmentFrameVideoProcessor"
    return "FfprobeVideoProcessor"


def _processor_binary_needs(config: AppConfig, processor: str) -> tuple[bool, bool]:
    """Return (needs_ffprobe, needs_ffmpeg) for the effective processor.

    Mirrors what each processor actually invokes so doctor never under- or
    over-reports binary requirements:
    - StaticVideoProcessor: nothing.
    - FfprobeVideoProcessor: ffprobe only (placeholder frames, no decode).
    - SegmentFrameVideoProcessor: ffprobe (duration) + ffmpeg (frame decode).
    - OpenCvFrameStreamVideoProcessor: ffprobe (duration); ffmpeg only when the
      ffmpeg fallback is enabled (decode itself uses OpenCV, no subprocess).
    - WholeClipVideoProcessor: ffprobe; + ffmpeg only when stripping audio
      (i.e. ``vlm.include_audio`` is False).
    """
    if processor == "StaticVideoProcessor":
        return (False, False)
    if processor == "FfprobeVideoProcessor":
        return (True, False)
    if processor == "SegmentFrameVideoProcessor":
        return (True, True)
    if processor == "OpenCvFrameStreamVideoProcessor":
        return (True, config.pipeline.decode_fallback_to_ffmpeg)
    if processor == "WholeClipVideoProcessor":
        return (True, not config.vlm.include_audio)
    return (False, False)


def _cv2_importable() -> bool:
    """True iff cv2 + numpy can be imported (no import side effects)."""
    from importlib.util import find_spec

    return find_spec("cv2") is not None and find_spec("numpy") is not None


def _env_set(env: EnvMap, name: str) -> bool:
    """True iff ``name`` is present AND non-empty in ``env`` (no value exposed)."""
    return bool(env.get(name))


def build_doctor_report(
    config: AppConfig,
    *,
    env: EnvMap | None = None,
    which: WhichFn | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Return a structured, secret-free diagnostic of the effective config.

    ``env``/``which``/``cwd`` are injectable for deterministic tests; by default
    they read the real process environment, ``shutil.which``, and the real cwd.
    The returned dict is JSON-serializable and contains only non-sensitive data
    (env-var NAMES + booleans, never secret values).
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    cwd = os.getcwd() if cwd is None else cwd

    cfg_file = resolved_config_file()

    # --- VLM selection (mirror workers/_default_vlm) ----------------------
    vlm = config.vlm
    vlm_effective = "real" if vlm.provider == "real" else "mock"
    vlm_api_key_set = _env_set(env, vlm.api_key_env)
    vlm_base_url_set = _env_set(env, vlm.base_url_env)

    # --- Pipeline / binary requirements -----------------------------------
    # Compute requirements from the EFFECTIVE processor each provider would
    # select (mode + provider + media_input), mirroring
    # workers/_default_video_processor, so doctor never mis-reports binaries.
    mode = config.pipeline.video_metadata_mode
    proc_for_mock = _selected_processor(config, "mock")
    proc_for_real = _selected_processor(config, "real")
    mock_needs_ffprobe, mock_needs_ffmpeg = _processor_binary_needs(config, proc_for_mock)
    real_needs_ffprobe, real_needs_ffmpeg = _processor_binary_needs(config, proc_for_real)
    ffprobe_found = which("ffprobe") is not None
    ffmpeg_found = which("ffmpeg") is not None
    # The processor that the ACTIVE provider will really use (for reporting).
    active_processor = proc_for_real if vlm.provider == "real" else proc_for_mock
    needs_ffprobe, needs_ffmpeg = _processor_binary_needs(config, active_processor)

    # --- Indexing / retrieval ---------------------------------------------
    idx = config.indexing
    idx_embed_real = idx.provider == "real"
    idx_rerank_real = idx.rerank_enabled and idx.rerank_provider == "real"
    idx_embed_key_set = _env_set(env, idx.api_key_env)
    idx_rerank_key_set = _env_set(env, idx.rerank_api_key_env)
    database_backend = config.database.backend
    postgres_dsn_env = config.database.postgres_dsn_env
    postgres_dsn_env_set = _env_set(env, postgres_dsn_env)

    max_concurrent_jobs = config.worker.max_concurrent_jobs
    max_unit_workers_per_job = config.worker.max_unit_workers_per_job
    max_concurrent_requests = config.vlm.max_concurrent_requests

    # --- Readiness computation (explicit reasons) -------------------------
    mock_reasons: list[str] = []
    if mock_needs_ffprobe and not ffprobe_found:
        mock_reasons.append(
            f"mock path ({proc_for_mock}) requires ffprobe but it was not found on PATH"
        )
    if mock_needs_ffmpeg and not ffmpeg_found:
        mock_reasons.append(
            f"mock path ({proc_for_mock}) requires ffmpeg but it was not found on PATH"
        )
    ready_for_mock = not mock_reasons

    real_reasons: list[str] = []
    if vlm.provider != "real":
        real_reasons.append(
            "vlm.provider is not 'real' (set vlm.provider=real or "
            "CCTV_MEMORY_VLM__PROVIDER=real to use the real VLM)"
        )
    if not vlm_api_key_set:
        real_reasons.append(
            f"vlm.api_key_env ({vlm.api_key_env}) is not set in the environment"
        )
    if mode not in _REAL_MEDIA_MODES:
        real_reasons.append(
            f"pipeline.video_metadata_mode={mode} cannot supply real media to the VLM "
            "(use ffprobe or ffmpeg_frames with a real readable video file)"
        )
    if real_needs_ffprobe and not ffprobe_found:
        real_reasons.append(
            f"real path ({proc_for_real}) requires ffprobe but it was not found on PATH"
        )
    if real_needs_ffmpeg and not ffmpeg_found:
        real_reasons.append(
            f"real path ({proc_for_real}) requires ffmpeg but it was not found on PATH "
            f"(media_input={vlm.media_input}"
            + (", include_audio=false strips audio" if vlm.media_input == "video" else "")
            + ")"
        )
    # OpenCV backend readiness: cv2/numpy must be importable unless fallback is on.
    opencv_selected = proc_for_real == "OpenCvFrameStreamVideoProcessor"
    cv2_ok = _cv2_importable()
    if opencv_selected and not cv2_ok and not config.pipeline.decode_fallback_to_ffmpeg:
        real_reasons.append(
            "pipeline.decode_backend=opencv but cv2/numpy is not importable and "
            "decode_fallback_to_ffmpeg is false (install the 'cv' extra: "
            "opencv-python-headless + numpy, or enable the ffmpeg fallback)"
        )
    ready_for_real = not real_reasons

    vector_reasons: list[str] = []
    if not idx.enabled:
        vector_reasons.append(
            "indexing.enabled is false (vector search falls back to FTS; set "
            "CCTV_MEMORY_INDEXING__ENABLED=true to enable it)"
        )
    if idx_embed_real and not idx_embed_key_set:
        vector_reasons.append(
            f"indexing.provider=real but indexing.api_key_env ({idx.api_key_env}) is not set"
        )
    if idx_rerank_real and not idx_rerank_key_set:
        vector_reasons.append(
            f"indexing.rerank_provider=real but rerank_api_key_env "
            f"({idx.rerank_api_key_env}) is not set"
        )
    ready_for_vector = not vector_reasons

    return {
        "base": {
            "cwd": cwd,
            "config_file": str(cfg_file) if cfg_file is not None else None,
            "config_file_exists": bool(cfg_file is not None and Path(cfg_file).is_file()),
            "env": config.app.env,
            "data_dir": config.app.data_dir,
            "database_backend": database_backend,
            "sqlite_path": config.database.sqlite_path,
            "postgres_dsn_env": postgres_dsn_env,
            "postgres_dsn_env_set": postgres_dsn_env_set,
        },
        "vlm": {
            "provider": vlm.provider,
            "effective": vlm_effective,
            "model_id": vlm.model_id,
            "media_input": vlm.media_input,
            "include_audio": vlm.include_audio,
            "base_url_env": vlm.base_url_env,
            "base_url_env_set": vlm_base_url_set,
            "default_base_url": vlm.default_base_url,
            "api_key_env": vlm.api_key_env,
            "api_key_env_set": vlm_api_key_set,
        },
        "pipeline": {
            "video_metadata_mode": mode,
            "active_video_processor": active_processor,
            "decode_backend": config.pipeline.decode_backend,
            "decode_fallback_to_ffmpeg": config.pipeline.decode_fallback_to_ffmpeg,
            "cv2_importable": cv2_ok,
            "needs_ffprobe": needs_ffprobe,
            "needs_ffmpeg": needs_ffmpeg,
            "ffprobe_found": ffprobe_found,
            "ffmpeg_found": ffmpeg_found,
        },
        "worker": {
            "enabled": config.worker.enabled,
            "embedded": config.worker.embedded,
        },
        "concurrency": {
            "worker_max_concurrent_jobs": max_concurrent_jobs,
            "worker_max_unit_workers_per_job": max_unit_workers_per_job,
            "vlm_max_concurrent_requests": max_concurrent_requests,
            "effective_vlm_call_upper_bound": min(
                max_concurrent_requests,
                max_concurrent_jobs * max_unit_workers_per_job,
            ),
            "one_unit_video_upper_bound": min(
                max_concurrent_jobs,
                max_concurrent_requests,
            ),
        },
        "server": {
            "host": config.server.host,
            "port": config.server.port,
        },
        "indexing": {
            "enabled": idx.enabled,
            "provider": idx.provider,
            "rerank_enabled": idx.rerank_enabled,
            "rerank_provider": idx.rerank_provider,
            "embedding_api_key_env": idx.api_key_env,
            "embedding_api_key_env_set": idx_embed_key_set,
            "rerank_api_key_env": idx.rerank_api_key_env,
            "rerank_api_key_env_set": idx_rerank_key_set,
        },
        "readiness": {
            "ready_for_mock_analysis": ready_for_mock,
            "ready_for_mock_analysis_reasons": mock_reasons,
            "ready_for_real_vlm_analysis": ready_for_real,
            "ready_for_real_vlm_analysis_reasons": real_reasons,
            "ready_for_vector_search": ready_for_vector,
            "ready_for_vector_search_reasons": vector_reasons,
            # We never call the remote VLM/embedding service from doctor.
            "external_endpoint_probed": False,
            "note": (
                "Readiness reflects LOCAL configuration only; the external "
                "VLM/embedding endpoint is not contacted by doctor."
            ),
        },
    }


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _ready(value: bool) -> str:
    return "READY" if value else "NOT READY"


def render_doctor_text(report: Mapping[str, Any]) -> str:
    """Render a human-readable report. Pure; contains no secret values."""
    base = report["base"]
    vlm = report["vlm"]
    pipe = report["pipeline"]
    worker = report["worker"]
    concurrency = report.get("concurrency", {})
    server = report["server"]
    idx = report["indexing"]
    rd = report["readiness"]

    lines: list[str] = []
    lines.append("cctv-memory doctor — effective configuration diagnosis")
    lines.append("=" * 60)

    lines.append("[base]")
    lines.append(f"  cwd                : {base['cwd']}")
    lines.append(
        f"  config file        : {base['config_file'] or '(none; env + defaults only)'}"
    )
    lines.append(f"  env                : {base['env']}")
    lines.append(f"  data dir           : {base['data_dir']}")
    lines.append(f"  database backend   : {base.get('database_backend', 'sqlite')}")
    if base.get("database_backend") == "postgres":
        lines.append(
            f"  postgres dsn env   : {base['postgres_dsn_env']} "
            f"(set: {_yn(base['postgres_dsn_env_set'])})"
        )
    else:
        lines.append(f"  sqlite path        : {base['sqlite_path']}")

    lines.append("[vlm]")
    lines.append(f"  provider           : {vlm['provider']}")
    lines.append(f"  effective path     : {vlm['effective'].upper()}")
    lines.append(f"  model_id           : {vlm['model_id']}")
    lines.append(
        f"  media_input        : {vlm['media_input']} "
        f"({'multi-image frames' if vlm['media_input'] == 'frames' else 'whole video clip'})"
    )
    lines.append(f"  include_audio      : {_yn(vlm['include_audio'])}")
    lines.append(
        f"  base_url_env       : {vlm['base_url_env']} (set: {_yn(vlm['base_url_env_set'])})"
    )
    lines.append(f"  default_base_url   : {vlm['default_base_url']}")
    lines.append(
        f"  api_key_env        : {vlm['api_key_env']} (set: {_yn(vlm['api_key_env_set'])})"
    )

    lines.append("[pipeline]")
    lines.append(f"  video_metadata_mode: {pipe['video_metadata_mode']}")
    lines.append(f"  video processor    : {pipe['active_video_processor']}")
    lines.append(
        f"  decode_backend     : {pipe.get('decode_backend', 'n/a')} "
        f"(cv2 importable: {_yn(pipe.get('cv2_importable', False))}, "
        f"ffmpeg fallback: {_yn(pipe.get('decode_fallback_to_ffmpeg', False))})"
    )
    lines.append(
        f"  needs ffprobe      : {_yn(pipe['needs_ffprobe'])} "
        f"(found: {_yn(pipe['ffprobe_found'])})"
    )
    lines.append(
        f"  needs ffmpeg       : {_yn(pipe['needs_ffmpeg'])} "
        f"(found: {_yn(pipe['ffmpeg_found'])})"
    )

    lines.append("[worker / http]")
    lines.append(f"  worker.enabled     : {_yn(worker['enabled'])}")
    lines.append(f"  worker.embedded    : {_yn(worker['embedded'])}")
    lines.append(f"  server.host        : {server['host']}")
    lines.append(f"  server.port        : {server['port']}")

    if concurrency:
        lines.append("[concurrency]")
        lines.append(
            "  worker.max_concurrent_jobs      : "
            f"{concurrency['worker_max_concurrent_jobs']}"
        )
        lines.append(
            "  worker.max_unit_workers_per_job : "
            f"{concurrency['worker_max_unit_workers_per_job']}"
        )
        lines.append(
            "  vlm.max_concurrent_requests     : "
            f"{concurrency['vlm_max_concurrent_requests']}"
        )
        lines.append(
            "  effective VLM cap               : "
            f"{concurrency['effective_vlm_call_upper_bound']} "
            "(min(vlm cap, jobs x units/job))"
        )
        lines.append(
            "  one-unit/video cap              : "
            f"{concurrency['one_unit_video_upper_bound']} "
            "(min(job pool, vlm cap))"
        )

    lines.append("[indexing / retrieval]")
    lines.append(f"  indexing.enabled   : {_yn(idx['enabled'])}")
    lines.append(f"  indexing.provider  : {idx['provider']}")
    lines.append(f"  rerank_enabled     : {_yn(idx['rerank_enabled'])}")
    lines.append(f"  rerank_provider    : {idx['rerank_provider']}")
    lines.append(
        f"  embedding key env  : {idx['embedding_api_key_env']} "
        f"(set: {_yn(idx['embedding_api_key_env_set'])})"
    )
    lines.append(
        f"  rerank key env     : {idx['rerank_api_key_env']} "
        f"(set: {_yn(idx['rerank_api_key_env_set'])})"
    )

    lines.append("[readiness]")
    lines.append(f"  mock analysis      : {_ready(rd['ready_for_mock_analysis'])}")
    for reason in rd["ready_for_mock_analysis_reasons"]:
        lines.append(f"      - {reason}")
    lines.append(f"  real VLM analysis  : {_ready(rd['ready_for_real_vlm_analysis'])}")
    for reason in rd["ready_for_real_vlm_analysis_reasons"]:
        lines.append(f"      - {reason}")
    lines.append(f"  vector search      : {_ready(rd['ready_for_vector_search'])}")
    for reason in rd["ready_for_vector_search_reasons"]:
        lines.append(f"      - {reason}")
    lines.append(f"  note               : {rd['note']}")

    return "\n".join(lines)
