"""CLI smoke tests (testing-contract; task-spec Phase 0)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cctv_memory import __version__
from cctv_memory.cli import main
from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.domain.enums import AnalysisScale


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["version"])
    captured = capsys.readouterr()
    assert code == 0
    assert __version__ in captured.out


def test_health_command(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["health"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert payload["phase"] == "mvp-closed-loop"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_init_analyze_worker_search_closed_loop(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Force deterministic, no-ffprobe metadata so the closed loop never depends
    # on a real media file or a subprocess (resume-task constraint #3).
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE", "static")
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__STATIC_DURATION_MS", "30000")
    data_dir = f"{tmp_path}/data"  # type: ignore[str-bytes-safe]

    assert main(["init", "--data-dir", data_dir]) == 0
    init_out = json.loads(capsys.readouterr().out)
    assert init_out["status"] == "initialized"

    # analyze with --wait runs the embedded worker to completion.
    code = main(
        [
            "analyze",
            "--data-dir",
            data_dir,
            "--source-uri",
            "/data/videos/lobby.mp4",
            "--camera-id",
            "cam_lobby_01",
            "--video-start-time",
            "2026-06-06T21:00:00+08:00",
            "--idempotency-key",
            "cli-1",
            "--wait",
        ]
    )
    assert code == 0
    analyze_out = json.loads(capsys.readouterr().out)
    assert analyze_out["accepted"] is True
    assert analyze_out["job_status"] == "succeeded"
    assert analyze_out["worker_processed_tasks"] >= 1

    from cctv_memory.infrastructure.runtime import build_runtime

    runtime = build_runtime(data_dir=data_dir)
    with runtime.session() as session:
        events = runtime.repositories(session).timeline().list_by_job(
            analyze_out["analysis_job_id"]
        )
    runtime.dispose()
    event_names = {event.event_name for event in events}
    assert {
        "request_accepted",
        "task_queued",
        "task_claimed",
        "job_running",
        "unit_running",
        "frame_select",
        "media_refs_built",
        "vlm_scheduler_wait",
        "vlm_provider_call",
        "vlm_attempt",
        "publication_finished",
        "unit_finished",
        "job_finished",
    }.issubset(event_names)

    # search should now find the mock-generated records.
    assert main(["search", "--data-dir", data_dir, "--query", "person", "--top-k", "10"]) == 0
    search_out = json.loads(capsys.readouterr().out)
    assert search_out["candidate_count"] >= 1
    assert search_out["results"]


def test_worker_once_with_no_tasks(tmp_path: object, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = f"{tmp_path}/data2"  # type: ignore[str-bytes-safe]
    assert main(["init", "--data-dir", data_dir]) == 0
    capsys.readouterr()
    assert main(["worker", "--data-dir", data_dir, "--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["processed_task"] is None


def test_timeline_export_json_and_offline_html(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from cctv_memory.infrastructure.runtime import build_runtime

    data_dir = f"{tmp_path}/timeline_data"  # type: ignore[str-bytes-safe]
    assert main(["init", "--data-dir", data_dir]) == 0
    capsys.readouterr()

    runtime = build_runtime(data_dir=data_dir)
    with runtime.session() as session:
        runtime.repositories(session).timeline().append_event(
            AnalysisTimelineEvent(
                timeline_event_id="tl_export_001",
                trace_id="job_export",
                analysis_job_id="job_export",
                event_name="request_accepted",
                event_phase="instant",
                occurred_at=datetime.now(UTC),
                metadata={"queued": True},
            )
        )
    runtime.dispose()

    html_out = f"{tmp_path}/timeline.html"  # type: ignore[str-bytes-safe]
    json_out = f"{tmp_path}/timeline.json"  # type: ignore[str-bytes-safe]
    assert (
        main(
            [
                "timeline",
                "export",
                "--data-dir",
                data_dir,
                "--job-id",
                "job_export",
                "--out",
                html_out,
                "--json-out",
                json_out,
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["timeline"]["event_count"] == 1

    payload = json.loads(open(json_out, encoding="utf-8").read())
    assert payload["events"][0]["event_name"] == "request_accepted"
    html = open(html_out, encoding="utf-8").read()
    assert "Plotly.newPlot" in html
    assert "cdn.plot.ly" not in html
    assert "https://cdn" not in html
    assert "http://cdn" not in html
    assert "<script src=" not in html.lower()


def test_timeline_export_requires_exactly_one_selector(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = f"{tmp_path}/timeline_selector"  # type: ignore[str-bytes-safe]
    assert main(["init", "--data-dir", data_dir]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as missing:
        main(["timeline", "export", "--data-dir", data_dir, "--out", f"{tmp_path}/x.html"])
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as both:
        main(
            [
                "timeline",
                "export",
                "--data-dir",
                data_dir,
                "--job-id",
                "job_1",
                "--all",
                "--out",
                f"{tmp_path}/x.html",
            ]
        )
    assert both.value.code == 2


def test_timeline_export_all_json_html_and_out_dir(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from cctv_memory.infrastructure.runtime import build_runtime

    data_dir = f"{tmp_path}/timeline_all_data"  # type: ignore[str-bytes-safe]
    assert main(["init", "--data-dir", data_dir]) == 0
    capsys.readouterr()

    t0 = datetime.fromisoformat("2026-06-24T10:00:00+00:00")
    t1 = datetime.fromisoformat("2026-06-24T10:00:01+00:00")
    t2 = datetime.fromisoformat("2026-06-24T10:00:02+00:00")
    t3 = datetime.fromisoformat("2026-06-24T10:00:03+00:00")
    t4 = datetime.fromisoformat("2026-06-24T10:00:04+00:00")
    t5 = datetime.fromisoformat("2026-06-24T10:00:05+00:00")
    t6 = datetime.fromisoformat("2026-06-24T10:00:06+00:00")
    t7 = datetime.fromisoformat("2026-06-24T10:00:07+00:00")
    t8 = datetime.fromisoformat("2026-06-24T10:00:08+00:00")
    t9 = datetime.fromisoformat("2026-06-24T10:00:09+00:00")
    t10 = datetime.fromisoformat("2026-06-24T10:00:10+00:00")
    runtime = build_runtime(data_dir=data_dir)
    with runtime.session() as session:
        timeline = runtime.repositories(session).timeline()
        timeline.append_events(
            [
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_a",
                    trace_id="job_a",
                    analysis_job_id="job_a",
                    unit_id="unit_a",
                    model_call_id="mcall_a",
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    unit_kind="default_segment_window",
                    event_name="unit_running",
                    event_phase="instant",
                    status="running",
                    occurred_at=t0,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_frame_a_start",
                    trace_id="job_a",
                    span_id="span_frame_a",
                    analysis_job_id="job_a",
                    unit_id="unit_a",
                    model_call_id="mcall_a",
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    unit_kind="default_segment_window",
                    event_name="frame_select",
                    event_phase="start",
                    status="running",
                    occurred_at=t1,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_frame_a_finish",
                    trace_id="job_a",
                    span_id="span_frame_a",
                    analysis_job_id="job_a",
                    unit_id="unit_a",
                    model_call_id="mcall_a",
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    unit_kind="default_segment_window",
                    event_name="frame_select",
                    event_phase="finish",
                    status="succeeded",
                    duration_ms=1200,
                    occurred_at=t2,
                    metadata={"source_uri": "/private/videos/secret.mp4"},
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_media_a_start",
                    trace_id="job_a",
                    span_id="span_media_a",
                    analysis_job_id="job_a",
                    unit_id="unit_a",
                    model_call_id="mcall_a",
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    unit_kind="default_segment_window",
                    event_name="media_refs_built",
                    event_phase="start",
                    occurred_at=t3,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_media_a_finish",
                    trace_id="job_a",
                    span_id="span_media_a",
                    analysis_job_id="job_a",
                    unit_id="unit_a",
                    model_call_id="mcall_a",
                    analysis_scale=AnalysisScale.DEFAULT_SEGMENT,
                    unit_kind="default_segment_window",
                    event_name="media_refs_built",
                    event_phase="finish",
                    status="succeeded",
                    duration_ms=300,
                    occurred_at=t4,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_b",
                    trace_id="job_b",
                    analysis_job_id="job_b",
                    unit_id="unit_b",
                    model_call_id="mcall_b",
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    unit_kind="high_freq_event_window",
                    event_name="vlm_scheduler_wait",
                    event_phase="start",
                    status="running",
                    occurred_at=t5,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_b_wait_finish",
                    trace_id="job_b",
                    analysis_job_id="job_b",
                    unit_id="unit_b",
                    model_call_id="mcall_b",
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    unit_kind="high_freq_event_window",
                    event_name="vlm_scheduler_wait",
                    event_phase="finish",
                    status="succeeded",
                    duration_ms=200,
                    occurred_at=t6,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_c",
                    trace_id="job_b",
                    analysis_job_id="job_b",
                    unit_id="unit_b",
                    model_call_id="mcall_b",
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    unit_kind="high_freq_event_window",
                    event_name="vlm_provider_call",
                    event_phase="start",
                    status="running",
                    occurred_at=t7,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_c_finish",
                    trace_id="job_b",
                    analysis_job_id="job_b",
                    unit_id="unit_b",
                    model_call_id="mcall_b",
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    unit_kind="high_freq_event_window",
                    event_name="vlm_provider_call",
                    event_phase="finish",
                    status="succeeded",
                    duration_ms=900,
                    occurred_at=t8,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_d",
                    trace_id="job_b",
                    analysis_job_id="job_b",
                    unit_id="unit_b",
                    model_call_id="mcall_b",
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    unit_kind="high_freq_event_window",
                    event_name="publication_finished",
                    event_phase="instant",
                    status="succeeded",
                    duration_ms=80,
                    occurred_at=t9,
                ),
                AnalysisTimelineEvent(
                    timeline_event_id="tl_all_unit_b_finish",
                    trace_id="job_b",
                    analysis_job_id="job_b",
                    unit_id="unit_b",
                    model_call_id="mcall_b",
                    analysis_scale=AnalysisScale.HIGH_FREQ_EVENT,
                    unit_kind="high_freq_event_window",
                    event_name="unit_finished",
                    event_phase="instant",
                    status="succeeded",
                    occurred_at=t10,
                ),
            ]
        )
    runtime.dispose()

    out_dir = f"{tmp_path}/timelines"  # type: ignore[str-bytes-safe]
    assert (
        main(
            [
                "timeline",
                "export",
                "--data-dir",
                data_dir,
                "--all",
                "--since",
                t0.isoformat(),
                "--until",
                t10.isoformat(),
                "--out-dir",
                out_dir,
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["timeline"]["event_count"] == 11
    assert out["timeline"]["html_path"].endswith("index.html")
    assert out["timeline"]["json_path"].endswith("index.json")

    payload = json.loads(open(f"{out_dir}/index.json", encoding="utf-8").read())
    assert payload["scope"] == "all"
    assert payload["summary"]["job_count"] == 2
    assert payload["summary"]["event_count"] == 11
    assert payload["summary"]["peak_active_units"] >= 1
    assert payload["summary"]["peak_pre_vlm_active"] >= 1
    assert payload["summary"]["peak_frame_select_active"] >= 1
    assert payload["summary"]["peak_media_refs_build_active"] >= 1
    assert payload["summary"]["peak_scheduler_waiters"] >= 1
    assert payload["summary"]["peak_outbound_vlm_calls"] >= 1
    assert payload["summary"]["peak_provider_start_rate_per_sec"] >= 1
    assert payload["config"]["worker.max_concurrent_jobs"] >= 1
    assert payload["stage_latency_ms"]["frame_select"]["p50"] == 1200
    assert payload["stage_latency_ms"]["media_refs_built"]["p50"] == 300
    assert payload["stage_latency_ms"]["vlm_scheduler_wait"]["p50"] == 200
    assert payload["stage_latency_ms"]["vlm_provider_call"]["p50"] == 900
    assert payload["unit_stage_breakdown"]["unit_a"]["frame_select_ms"] == 1200
    assert payload["group_summary"]["analysis_scale"]["default_segment"]["peak_pre_vlm_active"] >= 1
    assert (
        payload["group_summary"]["unit_kind"]["high_freq_event_window"][
            "peak_outbound_vlm_calls"
        ]
        >= 1
    )
    assert payload["events"][2]["metadata"]["source_uri"] == "[redacted]"
    assert set(payload["job_ids"]) == {"job_a", "job_b"}

    html = open(f"{out_dir}/index.html", encoding="utf-8").read()
    assert "Plotly.newPlot" in html
    assert "Bottleneck Summary" in html
    assert "Likely Bottleneck Hints" in html
    assert "Stage Latency Percentiles" in html
    assert "Group Breakdown" in html
    assert "Top Slow Units" in html
    assert "Active units" in html
    assert "Pre-VLM active" in html
    assert "Active frame_select" in html
    assert "Active media_refs_built" in html
    assert "Provider start rate per sec" in html
    assert "analysis_job_id=job_a" in html
    assert "analysis_scale=default_segment" in html
    assert "unit_kind=default_segment_window" in html
    assert "/private/videos/secret.mp4" not in html
    assert "cdn.plot.ly" not in html
    assert "https://cdn" not in html
    assert "<script src=" not in html.lower()


def test_reindex_and_maintenance_sweep(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE", "static")
    monkeypatch.setenv("CCTV_MEMORY_PIPELINE__STATIC_DURATION_MS", "30000")
    data_dir = f"{tmp_path}/data3"  # type: ignore[str-bytes-safe]
    assert main(["init", "--data-dir", data_dir]) == 0
    capsys.readouterr()
    # Produce some records to index.
    main(
        [
            "analyze", "--data-dir", data_dir,
            "--source-uri", "/data/videos/lobby.mp4",
            "--camera-id", "cam_lobby_01",
            "--video-start-time", "2026-06-06T21:00:00+08:00",
            "--idempotency-key", "cli-reindex", "--wait",
        ]
    )
    capsys.readouterr()

    # Reindex builds vectors via the (mock by default) embedder.
    assert main(["reindex", "--data-dir", data_dir]) == 0
    reindex_out = json.loads(capsys.readouterr().out)["reindex"]
    assert reindex_out["scanned"] >= 1
    assert reindex_out["vectors_written"] >= 1

    # A second reindex is idempotent (everything skipped).
    assert main(["reindex", "--data-dir", data_dir]) == 0
    second = json.loads(capsys.readouterr().out)["reindex"]
    assert second["vectors_written"] == 0

    # Maintenance sweep runs and reports an expired count (>= 0).
    assert main(["maintenance", "sweep", "--data-dir", data_dir]) == 0
    sweep_out = json.loads(capsys.readouterr().out)["maintenance_sweep"]
    assert "expired_contexts" in sweep_out
