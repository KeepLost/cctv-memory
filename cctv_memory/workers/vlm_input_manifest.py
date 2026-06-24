"""Canonical VLM request input manifest helpers.

The manifest is generated at the worker/VLM boundary after frame materialization
and before the VLM adapter reads media bytes. It stores hashes/metadata only:
never image bytes/base64, never source_uri, never absolute frame paths.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cctv_memory.contracts.vlm import VlmSegmentRequest
from cctv_memory.infrastructure.vlm.prompts import (
    STRICT_RETRY_INSTRUCTION,
    build_prompt,
)

_MANIFEST_VERSION = 1


def canonical_json(value: Any) -> str:
    """Return compact canonical JSON for hashing/diffing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_short_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_vlm_input_manifest(
    *,
    request: VlmSegmentRequest,
    media_refs: list[dict[str, Any]],
    provider_options: dict[str, Any] | None,
    pipeline_version: str | None,
) -> tuple[str, dict[str, Any]]:
    """Build ``(input_manifest_hash, compact_manifest)`` for one VLM attempt.

    ``request.frame_uris`` order is the actual order the adapter will read/send;
    the manifest hashes those files in that same order before cleanup can delete
    them. Paths are reduced to basenames only, so local source paths are not
    persisted.
    """
    options = dict(provider_options or {})
    prompt = build_prompt(scale=request.analysis_scale)
    media_entries = [
        _media_entry(index, uri, media_refs[index] if index < len(media_refs) else {})
        for index, uri in enumerate(request.frame_uris)
    ]
    media_order_hash = sha256_short_text(
        canonical_json(
            [
                {
                    "index": entry["index"],
                    "sha256": entry.get("sha256"),
                    "sha256_error": entry.get("sha256_error"),
                    "size_bytes": entry.get("size_bytes"),
                    "frame_index": entry.get("frame_index"),
                    "timestamp_ms": entry.get("timestamp_ms"),
                    "decode_backend": entry.get("decode_backend"),
                }
                for entry in media_entries
            ]
        )
    )
    provider_options_canonical = canonical_json(options)
    input_fingerprint: dict[str, Any] = {
        "analysis_scale": request.analysis_scale.value,
        "segment_start_ms": request.segment_start_ms,
        "segment_end_ms": request.segment_end_ms,
        "camera_id": request.camera_id,
        "model_version": request.model_version,
        "prompt_version": request.prompt_version,
        "pipeline_version": pipeline_version,
        "system_prompt_hash": sha256_short_text(prompt),
        "strict_retry_instruction_hash": sha256_short_text(
            STRICT_RETRY_INSTRUCTION
        ),
        "provider_options_hash": sha256_short_text(provider_options_canonical),
        "ordered_media_hash": media_order_hash,
    }
    input_manifest_hash = sha256_short_text(canonical_json(input_fingerprint))
    manifest: dict[str, Any] = {
        "schema": "vlm_input_manifest",
        "version": _MANIFEST_VERSION,
        "input_manifest_hash": input_manifest_hash,
        "input_fingerprint": input_fingerprint,
        "unit": {
            "analysis_job_id": request.analysis_job_id,
            "video_id": request.video_id,
            "analysis_scale": request.analysis_scale.value,
            "segment_start_ms": request.segment_start_ms,
            "segment_end_ms": request.segment_end_ms,
        },
        "config": {
            "model_version": request.model_version,
            "prompt_version": request.prompt_version,
            "pipeline_version": pipeline_version,
        },
        "prompt": {
            "system_prompt_hash": sha256_short_text(prompt),
            "strict_retry_instruction_hash": sha256_short_text(
                STRICT_RETRY_INSTRUCTION
            ),
            "strict_retry": False,
        },
        "provider_options": {
            "temperature": options.get("temperature"),
            "top_p": options.get("top_p"),
            "seed": options.get("seed"),
            "response_format": options.get("response_format"),
            "extra_body": options,
            "extra_body_hash": sha256_short_text(provider_options_canonical),
        },
        "media": {
            "count": len(media_entries),
            "ordered_entries": media_entries,
            "ordered_media_hash": media_order_hash,
        },
    }
    return input_manifest_hash, manifest


def attach_manifest_to_attempts(
    attempt_details: list[dict[str, Any]],
    *,
    input_manifest_hash: str,
    input_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Copy attempt details and attach manifest audit fields to each attempt."""
    return [
        {
            **detail,
            "input_manifest_hash": input_manifest_hash,
            "input_manifest": input_manifest,
        }
        for detail in attempt_details
    ]


def _media_entry(index: int, uri: str, ref: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "index": index,
        "uri_basename": os.path.basename(uri),
    }
    for key in (
        "frame_index",
        "timestamp_ms",
        "decode_backend",
        "selection_reason",
        "motion_score",
        "scene_score",
        "blur_score",
        "brightness",
    ):
        if key in ref:
            entry[key] = ref[key]
    path = Path(uri)
    try:
        data = path.read_bytes()
    except OSError as exc:
        entry["sha256_error"] = type(exc).__name__
        return entry
    entry["size_bytes"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    return entry
