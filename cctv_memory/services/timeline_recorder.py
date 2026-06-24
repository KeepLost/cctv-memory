"""Fail-open recorder for append-only analysis timeline events."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from time import perf_counter_ns
from typing import Any

from cctv_memory.contracts.timeline import AnalysisTimelineEvent, TimelineEventPhase
from cctv_memory.domain.enums import AnalysisScale

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "xapikey",
    "token",
    "secret",
    "password",
    "source_uri",
    "sourceuri",
    "uri",
    "url",
    "path",
    "base64",
    "raw_media",
    "rawmedia",
    "raw_image",
    "rawimage",
    "raw_video",
    "rawvideo",
)
_MAX_STRING_LENGTH = 500
_DATA_URL_RE = re.compile(r"data:[^\s'\"]+;base64,[A-Za-z0-9+/=]+")
_LONG_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{120,}={0,2}")
_UNIX_PATH_RE = re.compile(r"/(?:[^\s'\"<>:]+/)+[^\s'\"<>:]+")

logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _safe_error_message(value: BaseException | str | None) -> str | None:
    if value is None:
        return None
    text = _sanitize_string(str(value))
    return text[:_MAX_STRING_LENGTH]


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_string(value: str) -> str:
    text = _DATA_URL_RE.sub("<redacted-data-url>", value)
    text = _LONG_BASE64_RE.sub("<redacted-base64>", text)

    def _path_repl(match: re.Match[str]) -> str:
        path = match.group(0).rstrip(").,;]")
        name = path.rsplit("/", 1)[-1]
        return f"<path:{name or 'redacted'}>"

    text = _UNIX_PATH_RE.sub(_path_repl, text)
    if len(text) > _MAX_STRING_LENGTH:
        return text[:_MAX_STRING_LENGTH] + "...<truncated>"
    return text


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _sensitive_key(key_text):
                clean[key_text] = "<redacted>"
                continue
            clean[key_text] = _sanitize_value(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value[:100]]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value[:100]]
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING_LENGTH]


class TimelineRecorder:
    """Small fail-open helper for timeline events.

    The append callable owns the physical transaction boundary. This class never
    imports infrastructure and never decides job/unit state.
    """

    def __init__(
        self,
        append: Callable[[AnalysisTimelineEvent], None],
        *,
        enabled: bool = True,
        fail_open: bool = True,
    ) -> None:
        self._append = append
        self._enabled = enabled
        self._fail_open = fail_open

    @classmethod
    def disabled(cls) -> TimelineRecorder:
        return cls(lambda _event: None, enabled=False)

    def event(
        self,
        event_name: str,
        *,
        event_phase: TimelineEventPhase = "instant",
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        analysis_job_id: str | None = None,
        task_id: str | None = None,
        scale_task_id: str | None = None,
        unit_id: str | None = None,
        model_call_id: str | None = None,
        video_id: str | None = None,
        analysis_scale: AnalysisScale | None = None,
        unit_kind: str | None = None,
        segment_start_ms: int | None = None,
        segment_end_ms: int | None = None,
        status: str | None = None,
        attempt_count: int | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_message: BaseException | str | None = None,
        correlation: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        if not self._enabled:
            return
        try:
            now = datetime.now(UTC)
            event = AnalysisTimelineEvent(
                timeline_event_id=_new_id("tl"),
                trace_id=trace_id or analysis_job_id or task_id or unit_id or "local",
                span_id=span_id,
                parent_span_id=parent_span_id,
                analysis_job_id=analysis_job_id,
                task_id=task_id,
                scale_task_id=scale_task_id,
                unit_id=unit_id,
                model_call_id=model_call_id,
                video_id=video_id,
                analysis_scale=analysis_scale,
                unit_kind=unit_kind,
                segment_start_ms=segment_start_ms,
                segment_end_ms=segment_end_ms,
                event_name=event_name,
                event_phase=event_phase,
                status=status,
                attempt_count=attempt_count,
                occurred_at=occurred_at or now,
                duration_ms=duration_ms,
                error_code=error_code,
                error_message=_safe_error_message(error_message),
                correlation=_sanitize_value(correlation or {}),
                metadata=_sanitize_value(metadata or {}),
                created_at=now,
            )
            self._append(event)
        except Exception:
            if not self._fail_open:
                raise
            logger.exception("timeline event append failed; continuing")

    @contextmanager
    def span(
        self,
        event_name: str,
        **kwargs: Any,
    ) -> Iterator[str]:
        span_id = str(kwargs.pop("span_id", None) or _new_id("span"))
        start_ns = perf_counter_ns()
        self.event(event_name, event_phase="start", span_id=span_id, **kwargs)
        try:
            yield span_id
        except Exception as exc:
            duration_ms = int((perf_counter_ns() - start_ns) / 1_000_000)
            self.event(
                event_name,
                event_phase="fail",
                span_id=span_id,
                duration_ms=duration_ms,
                error_code=type(exc).__name__,
                error_message=exc,
                **kwargs,
            )
            raise
        else:
            duration_ms = int((perf_counter_ns() - start_ns) / 1_000_000)
            self.event(
                event_name,
                event_phase="finish",
                span_id=span_id,
                duration_ms=duration_ms,
                **kwargs,
            )
