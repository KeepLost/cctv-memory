"""VLM input manifest/fingerprint determinism tests.

No real VLM/provider calls. The fake video processor writes deterministic media
bytes and asserts the production worker supplies a unique unit_key for every unit.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cctv_memory.application.ingestion import IngestionService
from cctv_memory.config.settings import AppConfig
from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.contracts.vlm import VlmAttr, VlmObservationOutput, VlmQuality
from cctv_memory.domain.enums import (
    Capability,
    PrincipalType,
    SecurityLevel,
    SourceType,
)
from cctv_memory.infrastructure.db.models import tables as orm
from cctv_memory.infrastructure.runtime import Runtime
from cctv_memory.services.video_processor import VideoMetadata
from cctv_memory.workers.analysis_worker import AnalysisWorker
from sqlalchemy import select

from tests.conftest import seed_camera


class _DeterministicVideoProcessor:
    def __init__(self, frame_root: Path, *, duration_ms: int = 20_000) -> None:
        self._frame_root = frame_root
        self._duration_ms = duration_ms
        self.unit_keys: list[str] = []

    def probe(self, source_uri: str) -> VideoMetadata:
        return VideoMetadata(duration_ms=self._duration_ms)

    def extract_frame_uris(
        self,
        source_uri: str,
        segment_start_ms: int,
        segment_end_ms: int,
        frame_count: int,
        *,
        unit_key: str | None = None,
    ) -> list[str]:
        assert unit_key, "worker must pass a unique unit_key to frame extraction"
        self.unit_keys.append(unit_key)
        out_dir = self._frame_root / unit_key / f"{segment_start_ms}_{segment_end_ms}"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for index in range(frame_count):
            path = out_dir / f"frame_{index:04d}.jpg"
            path.write_bytes(
                f"frame:{segment_start_ms}:{segment_end_ms}:{index}".encode("ascii")
            )
            paths.append(str(path))
        return paths


class _DeterministicVlm:
    def analyze_segment(self, request):  # type: ignore[no-untyped-def]
        # Reverse completion order under concurrency to prove canonical comparison
        # ignores publication/log chronology but manifests stay input-equivalent.
        time.sleep(max(0.0, (20_000 - request.segment_start_ms) / 1_000_000.0))
        seconds = request.segment_start_ms // 1000
        return VlmObservationOutput(
            static=f"static segment {seconds}",
            dynamic=f"dynamic {request.segment_start_ms}-{request.segment_end_ms}",
            tags=["person", f"seg_{seconds}"],
            quality=VlmQuality(reason="deterministic_test", score=0.9),
            attr=VlmAttr(alert=False),
        )


def test_manifest_and_canonical_output_match_serial_vs_concurrent(tmp_path: Path) -> None:
    serial = _run_case(tmp_path / "serial", max_vlm=1, max_jobs=1)
    concurrent = _run_case(tmp_path / "concurrent", max_vlm=4, max_jobs=1)

    assert serial["manifest_hashes"] == concurrent["manifest_hashes"]
    assert serial["manifest_units"] == concurrent["manifest_units"]
    assert serial["canonical_records"] == concurrent["canonical_records"]
    assert serial["unit_key_count"] == concurrent["unit_key_count"] == 4


def test_manifest_and_canonical_output_repeatable_same_config(tmp_path: Path) -> None:
    first = _run_case(tmp_path / "first", max_vlm=3, max_jobs=1)
    second = _run_case(tmp_path / "second", max_vlm=3, max_jobs=1)

    assert first["manifest_hashes"] == second["manifest_hashes"]
    assert first["manifest_units"] == second["manifest_units"]
    assert first["canonical_records"] == second["canonical_records"]


def test_manifest_records_missing_media_hash_error(tmp_path: Path) -> None:
    from cctv_memory.contracts.vlm import VlmSegmentRequest
    from cctv_memory.domain.enums import AnalysisScale
    from cctv_memory.workers.vlm_input_manifest import build_vlm_input_manifest

    request = VlmSegmentRequest(
        request_id="req",
        analysis_job_id="job",
        video_id="video",
        camera_id="cam_lobby_01",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=1000,
        frame_uris=[str(tmp_path / "missing.jpg")],
        prompt_version="p",
        model_version="m",
    )
    manifest_hash, manifest = build_vlm_input_manifest(
        request=request,
        media_refs=[{"timestamp_ms": 0, "decode_backend": "test"}],
        provider_options={"temperature": 0, "response_format": {"type": "json_object"}},
        pipeline_version="pipe",
    )

    assert manifest_hash == manifest["input_manifest_hash"]
    entry = manifest["media"]["ordered_entries"][0]
    assert entry["sha256_error"] == "FileNotFoundError"
    assert "missing.jpg" == entry["uri_basename"]


def _run_case(base: Path, *, max_vlm: int, max_jobs: int) -> dict[str, Any]:
    runtime = _runtime(base, max_vlm=max_vlm, max_jobs=max_jobs)
    processor = _DeterministicVideoProcessor(base / "frames")
    try:
        _submit(runtime)
        worker = AnalysisWorker(runtime, video_processor=processor, vlm=_DeterministicVlm())
        assert worker.drain() == 1
        with runtime.session() as session:
            logs = list(
                session.scalars(
                    select(orm.ModelCallLog).order_by(
                        orm.ModelCallLog.segment_start_ms, orm.ModelCallLog.model_call_id
                    )
                )
            )
            records = list(
                session.scalars(
                    select(orm.ObservationRecord).order_by(
                        orm.ObservationRecord.segment_start_ms,
                        orm.ObservationRecord.analysis_scale,
                    )
                )
            )
        manifests = [_manifest(row) for row in logs if row.status == "succeeded"]
        return {
            "manifest_hashes": sorted(m["input_manifest_hash"] for m in manifests),
            "manifest_units": sorted(
                (
                    m["unit"]["analysis_scale"],
                    m["unit"]["segment_start_ms"],
                    m["unit"]["segment_end_ms"],
                    m["media"]["ordered_media_hash"],
                    m["provider_options"]["extra_body_hash"],
                )
                for m in manifests
            ),
            "canonical_records": _canonical_records(records),
            "unit_key_count": len(set(processor.unit_keys)),
        }
    finally:
        runtime.dispose()


def _runtime(base: Path, *, max_vlm: int, max_jobs: int) -> Runtime:
    config = AppConfig().with_data_dir(str(base))
    config.pipeline.video_metadata_mode = "static"
    config.pipeline.default_segment.window_seconds = 5
    config.pipeline.default_segment.overlap_seconds = 0
    config.pipeline.static_duration_ms = 20_000
    config.pipeline.cross_scale.enabled = True
    config.vlm.provider = "mock"
    config.vlm.max_concurrent_requests = max_vlm
    config.vlm.extra_body = {
        "temperature": 0,
        "top_p": 1,
        "seed": 7,
        "response_format": {"type": "json_object"},
    }
    config.worker.max_concurrent_jobs = max_jobs
    runtime = Runtime(config)
    runtime.init_storage()
    runtime.create_schema()
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
                principal_id="svc_manifest",
                principal_type=PrincipalType.SERVICE_ACCOUNT,
                display_name="svc",
                roles=["security_viewer"],
            )
        )
    return runtime


def _submit(runtime: Runtime) -> str:
    with runtime.session() as session:
        repos = runtime.repositories(session)
        ingestion = IngestionService(
            repos.video_source(),
            repos.analysis_job(),
            repos.scale_task(),
            repos.task_queue(),
            repos.audit(),
        )
        principal = repos.principal().get_principal("svc_manifest")
        assert principal is not None
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri="/videos/manifest.mp4",
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 17, 20, 8, tzinfo=UTC),
                idempotency_key="manifest-case",
                analysis_options={"enable_default_segment": True},
            ),
            principal,
            capabilities=[Capability.ANALYSIS_SUBMIT],
        )
        return resp.analysis_job_id


def _manifest(row: orm.ModelCallLog) -> dict[str, Any]:
    details = json.loads(row.attempt_details_json)
    assert details, "ModelCallLog must include manifest-bearing attempt_details"
    manifest = details[-1]["input_manifest"]
    assert row.payload_hash == details[-1]["input_manifest_hash"]
    assert row.payload_hash == manifest["input_manifest_hash"]
    for entry in manifest["media"]["ordered_entries"]:
        assert "sha256" in entry
        assert "uri_basename" in entry
        assert os.sep not in entry["uri_basename"]
    assert manifest["provider_options"]["temperature"] == 0
    assert manifest["provider_options"]["seed"] == 7
    return manifest


def _canonical_records(rows: list[orm.ObservationRecord]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            row.analysis_scale,
            row.segment_start_ms,
            row.segment_end_ms,
            row.static_description_text,
            row.dynamic_description_text,
            tuple(json.loads(row.tags_json)),
            json.loads(row.attributes_json).get("quality", {}).get("score"),
            row.model_version,
            row.prompt_version,
            row.pipeline_version,
        )
        for row in rows
    )
