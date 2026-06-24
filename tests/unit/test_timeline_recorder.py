from __future__ import annotations

from datetime import UTC, datetime

from cctv_memory.contracts.timeline import AnalysisTimelineEvent
from cctv_memory.services.timeline_recorder import TimelineRecorder


def test_timeline_recorder_redacts_sensitive_metadata() -> None:
    events: list[AnalysisTimelineEvent] = []
    recorder = TimelineRecorder(events.append)

    recorder.event(
        "request_accepted",
        analysis_job_id="job_001",
        occurred_at=datetime.now(UTC),
        metadata={
            "source_uri": "/secret/video.mp4",
            "sourceUri": "/secret/other.mp4",
            "Authorization": "Bearer secret",
            "nested": {"api_key": "secret-key", "x-api-key": "secret-key-2"},
            "message": "failed reading /secret/internal/frames/frame_001.jpg",
            "image": "data:image/jpeg;base64," + "a" * 140,
            "safe_count": 3,
        },
    )

    assert events[0].metadata["source_uri"] == "<redacted>"
    assert events[0].metadata["sourceUri"] == "<redacted>"
    assert events[0].metadata["Authorization"] == "<redacted>"
    assert events[0].metadata["nested"] == {
        "api_key": "<redacted>",
        "x-api-key": "<redacted>",
    }
    assert events[0].metadata["message"] == "failed reading <path:frame_001.jpg>"
    assert events[0].metadata["image"] == "<redacted-data-url>"
    assert events[0].metadata["safe_count"] == 3


def test_timeline_recorder_redacts_error_message_paths() -> None:
    events: list[AnalysisTimelineEvent] = []
    recorder = TimelineRecorder(events.append)

    recorder.event(
        "vlm_attempt",
        event_phase="fail",
        analysis_job_id="job_001",
        error_message="cannot read media: /secret/internal/frames/frame_002.jpg",
    )

    assert events[0].error_message == "cannot read media: <path:frame_002.jpg>"


def test_timeline_recorder_fail_open() -> None:
    def _raise(_event: AnalysisTimelineEvent) -> None:
        raise RuntimeError("database is locked")

    recorder = TimelineRecorder(_raise, fail_open=True)
    recorder.event("unit_running", analysis_job_id="job_001")
