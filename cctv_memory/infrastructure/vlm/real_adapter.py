"""Real VLM adapter (infrastructure/vlm/real_adapter.py).

Implements ``VlmAnalyzerPort`` by calling an OpenAI-compatible multimodal chat
completions endpoint. Two input shapes (vlm-analysis-contract §1; selected by
``media_input``):

- ``frames`` (DEFAULT): the worker extracts sampled JPEG frames for the segment
  and the adapter sends ONE ``image_url`` part per frame
  (``data:image/jpeg;base64,...``). This is the local-deployment-friendly default
  and carries no audio.
- ``video`` (opt-in): the worker passes the whole clip path and the adapter sends
  a single ``image_url`` part with a video MIME (``data:video/mp4;base64,...``) —
  a known provider quirk where ``video_url`` is silently ignored but ``image_url``
  with a video MIME works. Audio is stripped upstream unless explicitly enabled.

Pure HTTP (httpx, sync, bounded timeout). No subprocess. Output is parsed and
validated into ``VlmObservationOutput``. Schema failure raises a structured
``VlmSchemaValidationError`` carrying full raw response text; schema regeneration
is worker/scheduler-owned so no hidden adapter model loop bypasses VlmScheduler.
HTTP/timeout failures raise ``VlmProviderError`` (mapped to vlm_provider_error /
retryable).

Cache-friendly message layout (task cctv-memory-20260616-1339, P2): the STABLE
prompt (schema + rules, selected by analysis_scale) is sent as the ``system``
message — byte-identical across requests so providers can reuse it as a cached
prefix — and the per-segment images follow in the ``user`` message. The strict
retry reminder is appended as a separate trailing user segment, so the system
prefix is NEVER mutated. ``response_format`` (e.g. ``{"type":"json_object"}``) is
opt-in only via ``extra_body`` so providers that reject it are unaffected.

The adapter NEVER sets policy/security fields — those are system-derived during
publication (ARCHITECTURE_CONSTITUTION §5, vlm-analysis-contract §4); the
``VlmObservationOutput`` contract (extra="forbid") rejects them anyway.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from cctv_memory.contracts.vlm import VlmObservationOutput, VlmSegmentRequest
from cctv_memory.domain.exceptions import DomainError, VlmSchemaValidationError
from cctv_memory.infrastructure.vlm.prompts import STRICT_RETRY_INSTRUCTION, build_prompt
from cctv_memory.services.model_output_validation import (
    ModelOutputValidationFailure,
    validate_json_model_output,
)


class VlmProviderError(DomainError):
    """Transient provider/transport failure (maps to vlm_provider_error, retryable)."""


class RealVlmAnalyzer:
    """Real VLM analyzer over an OpenAI-compatible multimodal endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        timeout_seconds: int = 120,
        max_retries: int = 1,
        media_input: str = "frames",
        extra_body: dict[str, Any] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model_id = model_id
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._media_input = media_input
        self._extra_body = dict(extra_body or {})
        # An injected client (tests) avoids real network; otherwise create one.
        self._client = client

    def analyze_segment(
        self, request: VlmSegmentRequest, *, strict_schema: bool = False
    ) -> VlmObservationOutput:
        content_parts = self._build_media_parts(request)

        # Cache-friendly layout (task cctv-memory-20260616-1339, P2): the STABLE
        # prompt (schema + rules) is the system message, byte-identical across all
        # requests of a scale, so it forms a reusable prefix for implicit provider
        # prompt caching. The per-segment images go in the user message AFTER the
        # stable prefix. The system prompt is selected by analysis_scale and NEVER
        # mutated by the strict retry (strict guidance is appended as a separate
        # user text segment instead, preserving prefix stability).
        system_prompt = build_prompt(scale=request.analysis_scale)

        content = self._call_api(content_parts, system_prompt, strict=strict_schema)
        result = validate_json_model_output(
            raw_response=content,
            schema_type=VlmObservationOutput,
            forbidden_fields=self._forbidden_fields(),
        )
        if isinstance(result, ModelOutputValidationFailure):
            raise VlmSchemaValidationError(
                result.message,
                stage=result.stage,
                raw_response=result.raw_response,
                parsed_payload=result.repaired_payload or result.parsed_payload,
                validation_errors=result.validation_errors,
                repair_attempted=result.repair_attempted,
                repair_succeeded=result.repair_succeeded,
                provider="real",
                model_id=self._model_id,
                attempts=[result.to_attempt_detail()],
            )
        return result.value

    @staticmethod
    def _forbidden_fields() -> set[str]:
        """System-derived/forbidden keys the model must not control."""
        return {
            "access_policy_id",
            "security_level",
            "camera_id",
            "location_id",
            "observed_start_time",
            "observed_end_time",
            "record_id",
        }

    def _build_media_parts(self, request: VlmSegmentRequest) -> list[dict[str, object]]:
        """Build the multimodal content parts for this segment.

        ``frames`` mode: one ``image_url`` part per extracted JPEG frame
        (``data:image/jpeg;base64,...``) — multiple images, no audio. ``video``
        mode: a single ``image_url`` part with a video MIME for the whole clip.
        Each referenced path is read and base64-encoded; an unreadable path raises
        ``VlmProviderError`` (mapped to a retryable provider error upstream).
        """
        if not request.frame_uris:
            raise VlmProviderError("no media (frame_uris) provided for VLM")
        if self._media_input == "video":
            # Whole-clip path: frame_uris[0] is the (audio-stripped) video file.
            video_b64 = self._read_base64(request.frame_uris[0])
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:video/mp4;base64,{video_b64}"},
                }
            ]
        # Default frames path: one image part per frame (multi-image, no audio).
        parts: list[dict[str, object]] = []
        for uri in request.frame_uris:
            frame_b64 = self._read_base64(uri)
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"},
                }
            )
        return parts

    @staticmethod
    def _read_base64(uri: str) -> str:
        path = Path(uri)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise VlmProviderError(f"cannot read media for VLM: {exc}") from exc
        return base64.b64encode(data).decode("ascii")

    def _call_api(
        self,
        media_parts: list[dict[str, object]],
        system_prompt: str,
        *,
        strict: bool = False,
    ) -> str:
        # Cache-friendly message layout (P2): a STABLE system prefix first, then a
        # user message whose content is the per-segment media. On a strict retry a
        # trailing user text segment is appended AFTER the media, so the system
        # prefix (the large reusable part) stays byte-identical across requests and
        # across the retry of the same request.
        user_content: list[dict[str, object]] = [*media_parts]
        if strict:
            user_content.append({"type": "text", "text": STRICT_RETRY_INSTRUCTION})
        payload = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        payload.update(self._safe_extra_body())
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    self._base_url, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                response = httpx.post(
                    self._base_url, headers=headers, json=payload, timeout=self._timeout
                )
        except httpx.TimeoutException as exc:
            raise VlmProviderError("VLM request timed out") from exc
        except httpx.HTTPError as exc:
            raise VlmProviderError(f"VLM request failed: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise VlmProviderError(f"VLM returned status {response.status_code}")
        try:
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise VlmProviderError(f"unexpected VLM response shape: {type(exc).__name__}") from exc

    def _safe_extra_body(self) -> dict[str, Any]:
        """Return provider options allowed to merge into the JSON body.

        The config is intentionally additive only. Core/system-controlled fields
        remain authoritative so custom options cannot replace media/messages/model
        or smuggle auth/header material into the provider body.
        """
        denied = {
            "model",
            "messages",
            "stream",
            "tools",
            "tool_choice",
            "authorization",
            "headers",
            "api_key",
            "key",
        }
        return {k: v for k, v in self._extra_body.items() if k.lower() not in denied}
