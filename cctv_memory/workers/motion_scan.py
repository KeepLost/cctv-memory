"""motion_scan processing path (workers/motion_scan.py).

Cheap motion/change detection that drives high_freq_event trigger selection
(vlm-analysis-contract §2.2). This scale does NOT publish active
ObservationRecords — it only produces ``HighFreqTrigger`` rows (or a skipped/
no-trigger result). The detector samples motion, the domain planner turns samples
into trigger windows, and this processor persists them idempotently.

Receives ports/services by injection; never imports concrete infrastructure and
never writes ObservationRecords (ARCHITECTURE_CONSTITUTION §6).
"""

from __future__ import annotations

from cctv_memory.contracts.analysis import HighFreqTrigger
from cctv_memory.domain import policies
from cctv_memory.repositories.analysis import HighFreqTriggerRepository
from cctv_memory.repositories.video_source import VideoSourceRepository
from cctv_memory.services.motion_detector import MotionDetectorPort
from cctv_memory.workers.common import new_id


class MotionScanProcessor:
    """Detect motion and persist HighFreqTriggers for one analysis job."""

    def __init__(
        self,
        *,
        video_sources: VideoSourceRepository,
        triggers: HighFreqTriggerRepository,
        motion_detector: MotionDetectorPort,
        high_freq_scale_task_id: str,
        threshold: float = 0.15,
        min_duration_ms: int = 600,
        merge_gap_ms: int = 800,
    ) -> None:
        self._video_sources = video_sources
        self._triggers = triggers
        self._motion_detector = motion_detector
        self._high_freq_scale_task_id = high_freq_scale_task_id
        self._threshold = threshold
        self._min_duration_ms = min_duration_ms
        self._merge_gap_ms = merge_gap_ms

    def process(self, analysis_job_id: str, video_id: str) -> int:
        """Sample motion, plan triggers, persist them. Returns trigger count.

        Idempotent: triggers are stored via the repository's idempotency key
        (analysis_job_id:video_id:start:end:reason), so re-running a job does not
        duplicate triggers.
        """
        source = self._video_sources.get_by_id(video_id)
        if source is None:
            raise RuntimeError(f"video {video_id} not found")
        samples = self._motion_detector.sample_motion(source.source_uri)
        # Effective EOF bound for clamping near-end triggers: prefer the probed
        # duration when known, else the last decoded motion sample (the real
        # decode bound). motion_scan may run before the default_segment probe
        # (cross-scale Phase 1), so the sample-derived bound keeps trigger windows
        # within the decodable range either way (task cctv-memory-20260612-1854).
        duration_ms = source.duration_ms
        if duration_ms is None and samples:
            duration_ms = max(s.timestamp_ms for s in samples)
        windows = policies.plan_motion_triggers(
            samples,
            threshold=self._threshold,
            min_duration_ms=self._min_duration_ms,
            merge_gap_ms=self._merge_gap_ms,
            duration_ms=duration_ms,
        )
        count = 0
        for w in windows:
            idempotency_key = HighFreqTrigger.build_idempotency_key(
                analysis_job_id, video_id, w.start_ms, w.end_ms, w.reason
            )
            trigger = HighFreqTrigger(
                trigger_id=new_id("trigger"),
                analysis_job_id=analysis_job_id,
                scale_task_id=self._high_freq_scale_task_id,
                video_id=video_id,
                trigger_start_ms=w.start_ms,
                trigger_end_ms=w.end_ms,
                motion_score=round(w.peak_score, 4),
                change_score=round(w.peak_score, 4),
                trigger_reason=w.reason,
                idempotency_key=idempotency_key,
            )
            self._triggers.create_or_get_by_idempotency(trigger)
            count += 1
        return count
