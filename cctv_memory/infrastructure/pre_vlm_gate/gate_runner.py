"""Concrete PreVlmGatePort implementation."""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable

from cctv_memory.contracts.object_detection import (
    ObjectDetectionBatchRequest,
    ObjectDetectionImageInput,
    ObjectDetectionRequestOptions,
)
from cctv_memory.contracts.pre_vlm_gate import (
    GateDecisionBundle,
    GateSignal,
    PreVlmGateLog,
    PreVlmGateRequest,
)
from cctv_memory.domain.policies.pre_vlm_gate import decide_pre_vlm_gate
from cctv_memory.repositories.analysis import PreVlmGateLogRepository
from cctv_memory.services.object_detection import ObjectDetectionPort
from cctv_memory.services.pre_vlm_gate import PreVlmGatePort


class PreVlmGateRunner(PreVlmGatePort):
    """Evaluate pre-VLM gate rules using object detection as the first signal."""

    def __init__(
        self,
        *,
        object_detection: ObjectDetectionPort,
        logs: PreVlmGateLogRepository | None = None,
        log_writer: Callable[[PreVlmGateLog], PreVlmGateLog] | None = None,
        max_results: int = 10,
        min_confidence: float | None = None,
    ) -> None:
        self._object_detection = object_detection
        self._logs = logs
        self._log_writer = log_writer
        self._max_results = max_results
        self._min_confidence = min_confidence

    def evaluate(self, request: PreVlmGateRequest) -> GateDecisionBundle:
        started = datetime.now(UTC)
        if not request.profile.enabled:
            bundle = self._disabled_bundle(request)
        else:
            signal = self._object_detection_signal(request)
            bundle = decide_pre_vlm_gate(
                signals=[signal],
                rules=request.profile.rules,
                analysis_scale=request.analysis_scale,
                suppression_policy=request.profile.suppression_policy,
                trigger_context=request.trigger_context,
                profile=request.profile,
            )
        finished = datetime.now(UTC)
        log = PreVlmGateLog(
                gate_log_id=request.gate_log_id,
                analysis_job_id=request.analysis_job_id,
                scale_task_id=request.scale_task_id,
                unit_id=request.unit_id,
                video_id=request.video_id,
                analysis_scale=request.analysis_scale,
                unit_kind=request.unit_kind,
                profile_name=request.profile.profile_name,
                segment_start_ms=request.segment_start_ms,
                segment_end_ms=request.segment_end_ms,
                provider=request.provider,
                model_id=request.model_id,
                status="succeeded",
                decision=bundle.decision.model_dump(mode="json"),
                signals=[s.model_dump(mode="json") for s in bundle.signals],
                frame_evidence=bundle.frame_evidence,
                evidence_hash=bundle.decision.evidence_hash,
                rule_config_hash=bundle.decision.rule_config_hash,
                suppression_policy=bundle.decision.suppression_policy,
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
                created_at=finished,
        )
        if self._log_writer is not None:
            self._log_writer(log)
        elif self._logs is not None:
            self._logs.create_log(log)
        else:
            raise RuntimeError("PreVlmGateRunner requires logs or log_writer")
        return bundle

    def _object_detection_signal(self, request: PreVlmGateRequest) -> GateSignal:
        od_request = ObjectDetectionBatchRequest(
            request_id=f"{request.request_id}:object_detection",
            provider=request.provider,
            images=[self._image_input(frame, idx) for idx, frame in enumerate(request.frames)],
            options=ObjectDetectionRequestOptions(
                max_results=self._max_results,
                min_confidence=self._min_confidence,
            ),
        )
        result = self._object_detection.detect_objects(od_request)
        frame_evidence = []
        failed = False
        for image_result in result.results:
            failed = failed or image_result.status == "failed"
            frame_evidence.append(
                {
                    "frame_index": image_result.image.frame_index,
                    "timestamp_ms": image_result.image.timestamp_ms or 0,
                    "uri_basename": self._basename_for(request, image_result.image_id),
                    "frame_hash": image_result.image.sha256,
                    "detections": [
                        {
                            "label": d.label,
                            "confidence": d.confidence,
                            "bbox": d.bbox.model_dump(mode="json"),
                            "category": d.category.model_dump(mode="json") if d.category else None,
                            "source_provider": d.source_provider,
                        }
                        for d in image_result.detections
                    ],
                    "error": image_result.error.model_dump(mode="json")
                    if image_result.error
                    else None,
                }
            )
        return GateSignal(
            signal_type="object_detection",
            provider=result.provider,
            model_id=result.model_id,
            status="failed" if failed else "succeeded",
            frame_count=len(result.results),
            summary={"usage": result.usage.model_dump(mode="json") if result.usage else None},
            frame_evidence=frame_evidence,
        )

    def _image_input(self, frame: object, index: int) -> ObjectDetectionImageInput:
        from cctv_memory.contracts.pre_vlm_gate import GateFrameInput

        assert isinstance(frame, GateFrameInput)
        path = Path(frame.uri)
        if path.is_file():
            content = base64.b64encode(path.read_bytes()).decode("ascii")
            kind = "bytes_base64"
            artifact_ref = None
        else:
            content = None
            kind = "artifact_ref"
            artifact_ref = os.path.basename(frame.uri)
        return ObjectDetectionImageInput(
            image_id=f"frame_{index}",
            kind=kind,  # type: ignore[arg-type]
            content_base64=content,
            artifact_ref=artifact_ref,
            mime_type=frame.mime_type or "image/jpeg",
            width_px=frame.width_px,
            height_px=frame.height_px,
            frame_index=frame.frame_index,
            timestamp_ms=frame.timestamp_ms,
            sha256=frame.frame_hash,
        )

    def _basename_for(self, request: PreVlmGateRequest, image_id: str) -> str | None:
        try:
            index = int(image_id.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            return None
        if index < 0 or index >= len(request.frames):
            return None
        return os.path.basename(request.frames[index].uri)

    def _disabled_bundle(self, request: PreVlmGateRequest) -> GateDecisionBundle:
        from cctv_memory.contracts.pre_vlm_gate import PreVlmGateDecision

        decision = PreVlmGateDecision(
            triggered_vlm=True,
            action="disabled",
            matched_rules=[],
            positive_frame_ratio_by_label={},
            reason="pre_vlm_gate disabled",
            evidence_hash="sha256:disabled",
            rule_config_hash=None,
            suppression_policy=request.profile.suppression_policy,
        )
        return GateDecisionBundle(
            decision=decision,
            signals=[],
            frame_evidence=[],
            summary={"schema_version": "pre_vlm_gate_summary_v1", "triggered_vlm": True},
        )
