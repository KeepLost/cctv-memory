"""Real VLM integration tests (task §6).

Two groups:
1. Network-free unit tests for RealVlmAnalyzer using a fake httpx client
   (deterministic, bounded, always run): parse, fence-stripping, schema-fail
   retry, timeout/HTTP error mapping.
2. Live end-to-end test gated by LLM_KEY + ffmpeg: generate a short clip, run the
   real pipeline, assert a real ObservationRecord with non-empty text/tags, search
   it, and PRINT the VLM output for manual inspection.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest
from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.domain.exceptions import VlmSchemaValidationError
from cctv_memory.infrastructure.vlm.real_adapter import RealVlmAnalyzer, VlmProviderError

from tests.support.video_gen import ffmpeg_available, generate_testsrc

LLM_KEY_SET = bool(os.environ.get("LLM_KEY"))


def _request(tmp_path: object, video_path: str) -> VlmSegmentRequest:
    return VlmSegmentRequest(
        request_id="r1",
        analysis_job_id="job_1",
        video_id="video_1",
        camera_id="cam_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=5000,
        frame_uris=[video_path],
    )


def _fake_client(content: str, *, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"choices": [{"message": {"content": content}}]},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _adapter(client: httpx.Client, *, max_retries: int = 1) -> RealVlmAnalyzer:
    return RealVlmAnalyzer(
        base_url="http://fake/api",
        api_key="test",
        model_id="test-model",
        timeout_seconds=5,
        max_retries=max_retries,
        client=client,
    )


# ---- network-free unit tests ----------------------------------------------


def test_parses_clean_json(tmp_path: object) -> None:
    video = generate_dummy_file(tmp_path)
    content = (
        '{"static":"a lobby",'
        '"dynamic":"a person walks","tags":["person","lobby"],'
        '"quality":{"reason":"","score":0.8},"attr":{"alert":false}}'
    )
    out = _adapter(_fake_client(content)).analyze_segment(_request(tmp_path, str(video)))
    assert isinstance(out, VlmObservationOutput)
    assert out.static == "a lobby"
    assert out.tags == ["person", "lobby"]


def test_strips_markdown_fences(tmp_path: object) -> None:
    video = generate_dummy_file(tmp_path)
    content = (
        "```json\n"
        '{"static":"x",'
        '"dynamic":"y","tags":["t"],'
        '"quality":{"reason":"","score":0.5},"attr":{"alert":false}}\n'
        "```"
    )
    out = _adapter(_fake_client(content)).analyze_segment(_request(tmp_path, str(video)))
    assert out.dynamic == "y"


def test_strips_forbidden_policy_fields(tmp_path: object) -> None:
    video = generate_dummy_file(tmp_path)
    content = (
        '{"static":"x",'
        '"dynamic":"y","tags":["t"],'
        '"quality":{"reason":"","score":0.5},"attr":{"alert":false},'
        '"access_policy_id":"HACK","security_level":"restricted"}'
    )
    # extra forbidden fields are sanitized out, not allowed through.
    out = _adapter(_fake_client(content)).analyze_segment(_request(tmp_path, str(video)))
    assert isinstance(out, VlmObservationOutput)
    assert not hasattr(out, "access_policy_id")


def test_schema_failure_after_retries_raises(tmp_path: object) -> None:
    video = generate_dummy_file(tmp_path)
    # Always returns junk -> retry once -> still junk -> VlmSchemaValidationError.
    adapter = _adapter(_fake_client("not json at all"), max_retries=1)
    with pytest.raises(VlmSchemaValidationError):
        adapter.analyze_segment(_request(tmp_path, str(video)))


def test_timeout_maps_to_provider_error(tmp_path: object) -> None:
    video = generate_dummy_file(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(VlmProviderError):
        _adapter(client).analyze_segment(_request(tmp_path, str(video)))


def test_http_error_status_maps_to_provider_error(tmp_path: object) -> None:
    video = generate_dummy_file(tmp_path)
    with pytest.raises(VlmProviderError):
        _adapter(_fake_client("{}", status=500)).analyze_segment(
            _request(tmp_path, str(video))
        )


def generate_dummy_file(tmp_path: object) -> object:
    from pathlib import Path

    p = Path(str(tmp_path)) / "dummy.mp4"
    p.write_bytes(b"\x00\x01\x02fake-video-bytes")
    return p


# ---- P2: cache-friendly message structure (task cctv-memory-20260616-1339) -


def _capturing_client(captured: list[dict], content: str) -> httpx.Client:
    """Fake client that records each outgoing JSON body for assertion."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


_VALID_JSON = (
    '{"static":"a","dynamic":"b","tags":["t"],'
    '"quality":{"reason":"","score":0.5},"attr":{"alert":false}}'
)


def _two_frames(tmp_path: object) -> list[str]:
    from pathlib import Path

    paths = []
    for i in range(2):
        p = Path(str(tmp_path)) / f"frame_{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff" + bytes([i]))
        paths.append(str(p))
    return paths


def _request_with_frames(frames: list[str]) -> VlmSegmentRequest:
    return VlmSegmentRequest(
        request_id="r1",
        analysis_job_id="job_1",
        video_id="video_1",
        camera_id="cam_1",
        analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=0,
        segment_end_ms=5000,
        frame_uris=frames,
    )


def test_stable_prompt_is_system_message_and_images_follow(tmp_path: object) -> None:
    """P2: stable prompt is the system message; images are the user content."""
    from cctv_memory.infrastructure.vlm.prompts import build_prompt

    captured: list[dict] = []
    frames = _two_frames(tmp_path)
    _adapter(_capturing_client(captured, _VALID_JSON)).analyze_segment(
        _request_with_frames(frames)
    )
    assert len(captured) == 1
    messages = captured[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == build_prompt(scale=AnalysisScale.DEFAULT_SEGMENT)
    # User content is the images only (no stable text smuggled before them).
    user = messages[1]
    assert user["role"] == "user"
    assert all(part["type"] == "image_url" for part in user["content"])
    assert len(user["content"]) == 2


def test_two_requests_share_byte_identical_stable_prefix(tmp_path: object) -> None:
    """P2: the system prefix is byte-identical across different segment requests."""
    captured: list[dict] = []
    client = _capturing_client(captured, _VALID_JSON)
    adapter = _adapter(client)

    frames_a = _two_frames(tmp_path)
    adapter.analyze_segment(_request_with_frames(frames_a))

    # A different segment (different images / timing) — prefix must not change.
    from pathlib import Path

    other = Path(str(tmp_path)) / "other.jpg"
    other.write_bytes(b"\xff\xd8\xffZZ")
    req_b = VlmSegmentRequest(
        request_id="r2", analysis_job_id="job_1", video_id="video_1",
        camera_id="cam_2", analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
        segment_start_ms=9000, segment_end_ms=14000, frame_uris=[str(other)],
    )
    adapter.analyze_segment(req_b)

    assert len(captured) == 2
    sys_a = captured[0]["messages"][0]["content"]
    sys_b = captured[1]["messages"][0]["content"]
    assert sys_a == sys_b  # byte-identical reusable prefix


def test_image_order_is_preserved_in_user_content(tmp_path: object) -> None:
    """P2: media part order is deterministic / preserved (chronological)."""
    captured: list[dict] = []
    frames = _two_frames(tmp_path)
    _adapter(_capturing_client(captured, _VALID_JSON)).analyze_segment(
        _request_with_frames(frames)
    )
    urls = [p["image_url"]["url"] for p in captured[0]["messages"][1]["content"]]
    # Two distinct frames -> two distinct data URLs, in input order.
    assert len(urls) == 2
    assert urls[0] != urls[1]


def test_strict_retry_does_not_mutate_system_prefix(tmp_path: object) -> None:
    """P2: a strict retry keeps the system prefix stable; strictness goes to user."""
    # First response is junk -> triggers one strict retry; second is valid.
    import json as _json

    from cctv_memory.infrastructure.vlm.prompts import (
        STRICT_RETRY_INSTRUCTION,
        build_prompt,
    )

    responses = ["not json", _VALID_JSON]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(_json.loads(request.content))
        body = responses[min(len(captured) - 1, len(responses) - 1)]
        return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    frames = _two_frames(tmp_path)
    _adapter(client, max_retries=1).analyze_segment(_request_with_frames(frames))

    assert len(captured) == 2
    stable = build_prompt(scale=AnalysisScale.DEFAULT_SEGMENT)
    # System prefix identical on the normal attempt AND the strict retry.
    assert captured[0]["messages"][0]["content"] == stable
    assert captured[1]["messages"][0]["content"] == stable
    # Normal attempt: user content is images only.
    assert all(p["type"] == "image_url" for p in captured[0]["messages"][1]["content"])
    # Strict retry: a trailing user TEXT segment carries the strict instruction.
    retry_user = captured[1]["messages"][1]["content"]
    assert retry_user[-1]["type"] == "text"
    assert retry_user[-1]["text"] == STRICT_RETRY_INSTRUCTION
    # The images still precede the strict text segment.
    assert retry_user[0]["type"] == "image_url"


def test_response_format_is_opt_in_via_extra_body(tmp_path: object) -> None:
    """P2: response_format only appears when configured via extra_body (safe gate)."""
    frames = _two_frames(tmp_path)

    # Default: no response_format in the body.
    captured_default: list[dict] = []
    _adapter(_capturing_client(captured_default, _VALID_JSON)).analyze_segment(
        _request_with_frames(frames)
    )
    assert "response_format" not in captured_default[0]

    # Opt-in via extra_body: response_format is forwarded.
    captured_optin: list[dict] = []
    adapter = RealVlmAnalyzer(
        base_url="http://fake/api", api_key="test", model_id="test-model",
        timeout_seconds=5, max_retries=1,
        extra_body={"response_format": {"type": "json_object"}},
        client=_capturing_client(captured_optin, _VALID_JSON),
    )
    adapter.analyze_segment(_request_with_frames(frames))
    assert captured_optin[0]["response_format"] == {"type": "json_object"}


# ---- live end-to-end test (gated) -----------------------------------------


@pytest.mark.skipif(not LLM_KEY_SET, reason="LLM_KEY not set")
@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not available")
def test_real_vlm_end_to_end(tmp_path: object, capsys: pytest.CaptureFixture[str]) -> None:
    from cctv_memory.application.ingestion import IngestionService
    from cctv_memory.application.seed import DEV_PRINCIPAL_ID, seed_local_defaults
    from cctv_memory.config.settings import AppConfig
    from cctv_memory.contracts.search import StartObservationSearchRequest
    from cctv_memory.contracts.video import SubmitVideoSourceRequest
    from cctv_memory.domain.enums import JobStatus, SourceType
    from cctv_memory.infrastructure.runtime import Runtime
    from cctv_memory.workers.analysis_worker import AnalysisWorker

    # Generate a short clip.
    clip = generate_testsrc(f"{tmp_path}/clip.mp4", duration=5)  # type: ignore[str-bytes-safe]

    # Runtime in real VLM mode + ffprobe metadata.
    config = AppConfig().with_data_dir(str(tmp_path))
    config.vlm.provider = "real"
    config.pipeline.video_metadata_mode = "ffprobe"
    runtime = Runtime(config)
    runtime.init_storage()
    runtime.create_schema()
    with runtime.session() as session:
        repos = runtime.repositories(session)
        seed_local_defaults(repos.principal(), repos.access_policy(), repos.camera())
        ingestion = IngestionService(
            repos.video_source(), repos.analysis_job(), repos.scale_task(),
            repos.task_queue(), repos.audit(),
        )
        from cctv_memory.application.auth import AuthorizationService

        auth = AuthorizationService(repos.principal(), repos.access_policy(), repos.camera())
        principal = auth.resolve_principal(DEV_PRINCIPAL_ID)
        scope = auth.authorized_scope_for(principal)
        resp = ingestion.submit(
            SubmitVideoSourceRequest(
                source_type=SourceType.FILE,
                source_uri=str(clip),
                camera_id="cam_lobby_01",
                video_start_time=datetime(2026, 6, 9, 9, 0, tzinfo=UTC),
                idempotency_key="real-e2e-1",
            ),
            principal,
            capabilities=scope.capabilities,
        )
        job_id = resp.analysis_job_id

    # Process with the REAL VLM (one bounded HTTP call per segment).
    worker = AnalysisWorker(runtime)
    worker.drain()

    with runtime.session() as session:
        repos = runtime.repositories(session)
        job = repos.analysis_job().get_job(job_id)
        assert job is not None
        assert job.job_status is JobStatus.SUCCEEDED, f"job failed: {job.error_code}"

        # Inspect the produced record(s).
        from cctv_memory.application.auth import AuthorizationService

        auth = AuthorizationService(repos.principal(), repos.access_policy(), repos.camera())
        scope = auth.authorized_scope_for(auth.resolve_principal(DEV_PRINCIPAL_ID))
        records = repos.observation_read().authorized_candidate_pool(scope, limit=10)
        assert records, "no ObservationRecord produced by real VLM"
        rec = records[0]
        assert rec.static_description_text.strip(), "empty static text"
        assert rec.dynamic_description_text.strip(), "empty dynamic text"
        assert isinstance(rec.tags, list) and rec.tags, "empty tags"

        # Print for manual inspection (visible with -s / on failure).
        print("\n=== REAL VLM OUTPUT ===")
        print("static:", rec.static_description_text)
        print("dynamic:", rec.dynamic_description_text)
        print("tags:", rec.tags)
        print("=======================")

        # Searchable.
        from cctv_memory.application.search import SearchService

        search = SearchService(
            repos.observation_read(), repos.search_context(), repos.audit()
        )
        result = search.start_search(
            StartObservationSearchRequest(query_text="video", top_k=5), scope
        )
        assert result.candidate_count >= 1

    runtime.dispose()
    captured = capsys.readouterr()
    assert "REAL VLM OUTPUT" in captured.out
