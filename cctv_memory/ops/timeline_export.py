"""Export analysis timeline events to JSON and offline Plotly HTML."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import plotly.graph_objects as go  # type: ignore[import-untyped]
import plotly.io as pio  # type: ignore[import-untyped]

from cctv_memory.contracts.timeline import AnalysisTimelineEvent

_CATEGORY_BY_EVENT_PREFIX = {
    "request": "request/queue",
    "task": "request/queue",
    "job": "job/scale",
    "scale": "job/scale",
    "unit": "job/scale",
    "frame": "pre-vlm local",
    "media": "pre-vlm local",
    "detector": "pre-vlm local",
    "vlm_scheduler": "scheduler wait",
    "vlm": "provider wall time",
    "model_call": "provider wall time",
    "publication": "post-vlm publication",
    "observation": "post-vlm publication",
    "audit": "post-vlm publication",
}
_PLOTLY_CLOUD_URL_RE = re.compile(r"https?://cdn[.]plot[.]ly/[^\"']*")
_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|token|secret|password|credential|source[_-]?uri|"
    r"source[_-]?path|raw[_-]?media|base64|image[_-]?bytes|frame[_-]?bytes|uri|url|path)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(r"^(/|[A-Za-z]:[\\/])")
_PRE_VLM_EVENTS = {"frame_select", "media_refs_built", "detector_gate"}
_STAGE_EVENT_NAMES = {
    "frame_select": "frame_select_ms",
    "media_refs_built": "media_refs_build_ms",
    "detector_gate": "detector_gate_ms",
    "vlm_scheduler_wait": "scheduler_wait_ms",
    "vlm_provider_call": "provider_wall_ms",
    "publication_finished": "publication_ms",
}


@dataclass(frozen=True)
class TimelineExportResult:
    analysis_job_id: str
    event_count: int
    html_path: str
    json_path: str | None


def _event_category(event: AnalysisTimelineEvent) -> str:
    for prefix, category in _CATEGORY_BY_EVENT_PREFIX.items():
        if event.event_name.startswith(prefix):
            return category
    return "other"


def _event_label(event: AnalysisTimelineEvent) -> str:
    parts = [event.event_name, event.event_phase]
    if event.unit_id:
        parts.append(event.unit_id)
    elif event.scale_task_id:
        parts.append(event.scale_task_id)
    elif event.task_id:
        parts.append(event.task_id)
    return " | ".join(parts)


def _event_scale(event: AnalysisTimelineEvent) -> str:
    return event.analysis_scale.value if event.analysis_scale is not None else "unknown"


def _event_unit_kind(event: AnalysisTimelineEvent) -> str:
    return event.unit_kind or "unknown"


def _sanitize_value(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, key=key) for item in value]
    if isinstance(value, str):
        if value.startswith("data:") or _ABSOLUTE_PATH_RE.match(value):
            return "[redacted]"
        if len(value) > 2048:
            return "[redacted-long-string]"
    return value


def _event_to_safe_dict(event: AnalysisTimelineEvent) -> dict[str, Any]:
    raw = event.model_dump(mode="json")
    return {key: _sanitize_value(value, key=key) for key, value in raw.items()}


def _safe_html(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _duration_between_ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return int(round(ordered[lower] * (1 - weight) + ordered[upper] * weight))


def _latency_stats(values: list[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def timeline_events_to_payload(
    *, analysis_job_id: str, events: list[AnalysisTimelineEvent]
) -> dict[str, Any]:
    return {
        "analysis_job_id": analysis_job_id,
        "event_count": len(events),
        "categories": sorted({_event_category(event) for event in events}),
        "events": [_event_to_safe_dict(event) for event in events],
    }


def _series_for_active_keys(
    events: list[AnalysisTimelineEvent],
    *,
    start_names: set[str],
    finish_names: set[str],
    key_attr: str,
) -> tuple[list[Any], list[int], int]:
    active: set[str] = set()
    times: list[Any] = []
    counts: list[int] = []
    peak = 0
    for event in events:
        key = getattr(event, key_attr) or event.span_id or event.timeline_event_id
        if event.event_name in start_names and event.event_phase in {"instant", "start"}:
            active.add(str(key))
        elif event.event_name in finish_names and event.event_phase in {
            "instant",
            "finish",
            "fail",
        }:
            active.discard(str(key))
        if event.event_name in start_names or event.event_name in finish_names:
            peak = max(peak, len(active))
            times.append(event.occurred_at)
            counts.append(len(active))
    return times, counts, peak


def _stage_series(
    events: list[AnalysisTimelineEvent], event_names: set[str]
) -> tuple[list[Any], list[int], int]:
    active: set[str] = set()
    times: list[Any] = []
    counts: list[int] = []
    peak = 0
    for event in events:
        if event.event_name not in event_names:
            continue
        key = event.span_id or event.unit_id or event.model_call_id or event.timeline_event_id
        if event.event_phase == "start":
            active.add(str(key))
        elif event.event_phase in {"finish", "fail"}:
            active.discard(str(key))
        else:
            continue
        peak = max(peak, len(active))
        times.append(event.occurred_at)
        counts.append(len(active))
    return times, counts, peak


def _event_rate_series(
    events: list[AnalysisTimelineEvent], *, event_name: str, phase: str
) -> tuple[list[Any], list[int], int]:
    buckets: dict[datetime, int] = {}
    for event in events:
        if event.event_name != event_name or event.event_phase != phase:
            continue
        bucket = event.occurred_at.replace(microsecond=0)
        buckets[bucket] = buckets.get(bucket, 0) + 1
    times = sorted(buckets)
    counts = [buckets[time] for time in times]
    return times, counts, max(counts) if counts else 0


def _active_series(
    events: list[AnalysisTimelineEvent],
) -> dict[str, tuple[list[Any], list[int], int]]:
    return {
        "Active units": _series_for_active_keys(
            events,
            start_names={"unit_running"},
            finish_names={"unit_finished"},
            key_attr="unit_id",
        ),
        "Pre-VLM active": _stage_series(events, _PRE_VLM_EVENTS),
        "Active frame_select": _stage_series(events, {"frame_select"}),
        "Active media_refs_built": _stage_series(events, {"media_refs_built"}),
        "Active detector_gate": _stage_series(events, {"detector_gate"}),
        "Scheduler waiters": _series_for_active_keys(
            events,
            start_names={"vlm_scheduler_wait"},
            finish_names={"vlm_scheduler_wait"},
            key_attr="model_call_id",
        ),
        "Outbound VLM calls": _series_for_active_keys(
            events,
            start_names={"vlm_provider_call"},
            finish_names={"vlm_provider_call"},
            key_attr="model_call_id",
        ),
        "VLM-ready rate per sec": _event_rate_series(
            events, event_name="vlm_scheduler_wait", phase="start"
        ),
        "Provider start rate per sec": _event_rate_series(
            events, event_name="vlm_provider_call", phase="start"
        ),
    }


def _stage_duration_ms(
    event: AnalysisTimelineEvent,
    starts: dict[tuple[str, str], AnalysisTimelineEvent],
) -> int | None:
    if event.event_phase not in {"finish", "fail", "instant"}:
        return None
    if event.duration_ms is not None:
        return event.duration_ms
    key = (event.event_name, event.span_id or event.unit_id or event.timeline_event_id)
    start = starts.get(key)
    if start is None:
        return None
    return _duration_between_ms(start.occurred_at, event.occurred_at)


def _unit_stage_breakdown(events: list[AnalysisTimelineEvent]) -> dict[str, dict[str, Any]]:
    starts: dict[tuple[str, str], AnalysisTimelineEvent] = {}
    unit_start: dict[str, AnalysisTimelineEvent] = {}
    units: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.unit_id:
            units.setdefault(
                event.unit_id,
                {
                    "analysis_job_id": event.analysis_job_id,
                    "analysis_scale": _event_scale(event),
                    "unit_kind": _event_unit_kind(event),
                    "model_call_id": event.model_call_id,
                    "segment_start_ms": event.segment_start_ms,
                    "segment_end_ms": event.segment_end_ms,
                },
            )
        if event.event_name == "unit_running" and event.unit_id:
            unit_start[event.unit_id] = event
        if event.event_phase == "start":
            start_key = event.span_id or event.unit_id or event.timeline_event_id
            starts[(event.event_name, start_key)] = event
            continue
        if event.event_name in _STAGE_EVENT_NAMES and event.unit_id:
            duration = _stage_duration_ms(event, starts)
            if duration is not None:
                units.setdefault(event.unit_id, {})[_STAGE_EVENT_NAMES[event.event_name]] = duration
        if event.event_name == "unit_finished" and event.unit_id:
            started = unit_start.get(event.unit_id)
            if started is not None:
                units.setdefault(event.unit_id, {})["total_unit_ms"] = _duration_between_ms(
                    started.occurred_at, event.occurred_at
                )
    return units


def _stage_latency_summary(events: list[AnalysisTimelineEvent]) -> dict[str, dict[str, Any]]:
    starts: dict[tuple[str, str], AnalysisTimelineEvent] = {}
    durations: dict[str, list[int]] = {name: [] for name in _STAGE_EVENT_NAMES}
    for event in events:
        if event.event_phase == "start":
            start_key = event.span_id or event.unit_id or event.timeline_event_id
            starts[(event.event_name, start_key)] = event
        if event.event_name not in _STAGE_EVENT_NAMES:
            continue
        duration = _stage_duration_ms(event, starts)
        if duration is not None:
            durations[event.event_name].append(duration)
    return {name: _latency_stats(values) for name, values in durations.items()}


def _group_summary(events: list[AnalysisTimelineEvent]) -> dict[str, dict[str, dict[str, Any]]]:
    summary: dict[str, dict[str, dict[str, Any]]] = {"analysis_scale": {}, "unit_kind": {}}
    for group_name, key_fn in (
        ("analysis_scale", _event_scale),
        ("unit_kind", _event_unit_kind),
    ):
        keys = sorted({key_fn(event) for event in events})
        for key in keys:
            group_events = [event for event in events if key_fn(event) == key]
            series = _active_series(group_events)
            summary[group_name][key] = {
                "event_count": len(group_events),
                "unit_count": len({event.unit_id for event in group_events if event.unit_id}),
                "model_call_count": len(
                    {event.model_call_id for event in group_events if event.model_call_id}
                ),
                "peak_active_units": series["Active units"][2],
                "peak_pre_vlm_active": series["Pre-VLM active"][2],
                "peak_outbound_vlm_calls": series["Outbound VLM calls"][2],
                "counts_by_event": dict(Counter(event.event_name for event in group_events)),
            }
    return summary


def _top_slow_units(
    unit_breakdown: dict[str, dict[str, Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for unit_id, data in unit_breakdown.items():
        durations = {
            key: value
            for key, value in data.items()
            if key.endswith("_ms") and isinstance(value, int)
        }
        if not durations:
            continue
        slow_stage, slow_ms = max(durations.items(), key=lambda item: item[1])
        rows.append(
            {
                "unit_id": unit_id,
                "analysis_job_id": data.get("analysis_job_id"),
                "analysis_scale": data.get("analysis_scale"),
                "unit_kind": data.get("unit_kind"),
                "model_call_id": data.get("model_call_id"),
                "slowest_stage": slow_stage,
                "slowest_stage_ms": slow_ms,
                "total_unit_ms": data.get("total_unit_ms"),
            }
        )
    return sorted(rows, key=lambda row: row["slowest_stage_ms"], reverse=True)[:limit]


def _bottleneck_hints(summary: dict[str, Any], config: dict[str, Any] | None) -> list[str]:
    hints: list[str] = []
    vlm_cap = int(config.get("vlm.max_concurrent_requests", 0)) if config else 0
    peak_pre = int(summary.get("peak_pre_vlm_active") or 0)
    peak_wait = int(summary.get("peak_scheduler_waiters") or 0)
    peak_out = int(summary.get("peak_outbound_vlm_calls") or 0)
    peak_units = int(summary.get("peak_active_units") or 0)
    if peak_pre > 0 and peak_out <= max(1, peak_pre // 4) and peak_wait == 0:
        hints.append(
            "High pre-VLM activity with low scheduler/provider activity: "
            "likely frame/media/decode supply bottleneck."
        )
    if peak_wait > 0 and peak_out < peak_wait:
        hints.append(
            "Scheduler waiters exceed outbound calls: local semaphore or min interval "
            "may be limiting provider starts."
        )
    if vlm_cap and peak_out < max(1, vlm_cap // 4) and peak_units > peak_out:
        hints.append(
            "Outbound VLM calls are far below configured VLM cap while units are active: "
            "inspect upstream supply and scheduler traces."
        )
    if peak_out and vlm_cap and peak_out >= vlm_cap:
        hints.append("Outbound VLM calls reached configured cap: local VLM cap is saturated.")
    if not hints:
        hints.append(
            "No single bottleneck rule fired; compare stage latency tables with "
            "concurrency curves."
        )
    return hints


def _aggregate_summary(
    events: list[AnalysisTimelineEvent], *, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    series = _active_series(events)
    job_ids = {event.analysis_job_id for event in events if event.analysis_job_id}
    unit_ids = {event.unit_id for event in events if event.unit_id}
    model_call_ids = {event.model_call_id for event in events if event.model_call_id}
    time_range = {
        "start": _iso(events[0].occurred_at) if events else None,
        "end": _iso(events[-1].occurred_at) if events else None,
    }
    summary = {
        "time_range": time_range,
        "event_count": len(events),
        "job_count": len(job_ids),
        "unit_count": len(unit_ids),
        "model_call_count": len(model_call_ids),
        "counts_by_event": dict(Counter(event.event_name for event in events)),
        "counts_by_category": dict(Counter(_event_category(event) for event in events)),
        "counts_by_status": dict(
            Counter(event.status or "unknown" for event in events)
        ),
        "peak_active_units": series["Active units"][2],
        "peak_pre_vlm_active": series["Pre-VLM active"][2],
        "peak_frame_select_active": series["Active frame_select"][2],
        "peak_media_refs_build_active": series["Active media_refs_built"][2],
        "peak_detector_gate_active": series["Active detector_gate"][2],
        "peak_scheduler_waiters": series["Scheduler waiters"][2],
        "peak_vlm_calls": series["Outbound VLM calls"][2],
        "peak_outbound_vlm_calls": series["Outbound VLM calls"][2],
        "peak_vlm_ready_rate_per_sec": series["VLM-ready rate per sec"][2],
        "peak_provider_start_rate_per_sec": series["Provider start rate per sec"][2],
        "publication_write_count": sum(
            1 for event in events if event.event_name.startswith("publication")
        ),
    }
    summary["bottleneck_hints"] = _bottleneck_hints(summary, config)
    return summary


def timeline_events_to_aggregate_payload(
    *, events: list[AnalysisTimelineEvent], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    unit_breakdown = _unit_stage_breakdown(events)
    return {
        "scope": "all",
        "summary": _aggregate_summary(events, config=config),
        "config": config or {},
        "job_ids": sorted({event.analysis_job_id for event in events if event.analysis_job_id}),
        "categories": sorted({_event_category(event) for event in events}),
        "stage_latency_ms": _stage_latency_summary(events),
        "unit_stage_breakdown": unit_breakdown,
        "top_slow_units": _top_slow_units(unit_breakdown),
        "group_summary": _group_summary(events),
        "events": [_event_to_safe_dict(event) for event in events],
    }


def build_timeline_figure(events: list[AnalysisTimelineEvent]) -> go.Figure:
    categories = [_event_category(event) for event in events]
    labels = [_event_label(event) for event in events]
    times = [event.occurred_at for event in events]
    durations = [event.duration_ms or 0 for event in events]
    hover = [
        "<br>".join(
            [
                f"event={event.event_name}",
                f"phase={event.event_phase}",
                f"status={event.status or ''}",
                f"duration_ms={event.duration_ms or 0}",
                f"analysis_job_id={event.analysis_job_id or ''}",
                f"trace_id={event.trace_id}",
                f"analysis_scale={_event_scale(event)}",
                f"unit_kind={_event_unit_kind(event)}",
                f"unit_id={event.unit_id or ''}",
                f"model_call_id={event.model_call_id or ''}",
            ]
        )
        for event in events
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=times,
            y=categories,
            mode="markers+text",
            text=labels,
            textposition="top center",
            marker={
                "size": [max(8, min(32, 8 + duration / 100)) for duration in durations],
                "color": durations,
                "colorscale": "Viridis",
                "showscale": True,
                "colorbar": {"title": "duration_ms"},
            },
            hovertext=hover,
            hoverinfo="text+x+y",
        )
    )
    fig.update_layout(
        title="CCTV Memory Analysis Timeline",
        xaxis_title="time",
        yaxis_title="pipeline category",
        template="plotly_white",
        height=max(500, 220 + 35 * len(set(categories))),
        margin={"l": 140, "r": 40, "t": 70, "b": 60},
    )
    return fig


def build_aggregate_timeline_figure(
    events: list[AnalysisTimelineEvent], *, config: dict[str, Any] | None = None
) -> go.Figure:
    fig = build_timeline_figure(events)
    fig.update_layout(title="CCTV Memory Bottleneck Dashboard Timeline")
    for name, (times, counts, _) in _active_series(events).items():
        fig.add_trace(
            go.Scatter(
                x=times,
                y=counts,
                mode="lines+markers",
                name=name,
                yaxis="y2",
            )
        )
    if config and events:
        cap = config.get("vlm.max_concurrent_requests")
        if isinstance(cap, int) and cap > 0:
            fig.add_trace(
                go.Scatter(
                    x=[events[0].occurred_at, events[-1].occurred_at],
                    y=[cap, cap],
                    mode="lines",
                    name="Configured VLM cap",
                    yaxis="y2",
                    line={"dash": "dash"},
                )
            )
    fig.update_layout(
        yaxis2={
            "title": "active count",
            "overlaying": "y",
            "side": "right",
            "rangemode": "tozero",
        },
        legend={"orientation": "h", "y": -0.2},
    )
    return fig


def _render_key_value_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(
        f"<tr><th>{_safe_html(key)}</th><td>{_safe_html(value)}</td></tr>"
        for key, value in rows
    )
    return f"<table>{body}</table>"


def _render_stage_latency_table(stage_latency: dict[str, dict[str, Any]]) -> str:
    rows = ["<tr><th>stage</th><th>count</th><th>p50</th><th>p90</th><th>p99</th><th>max</th></tr>"]
    for stage, stats in stage_latency.items():
        rows.append(
            "<tr>"
            f"<td>{_safe_html(stage)}</td>"
            f"<td>{_safe_html(stats.get('count'))}</td>"
            f"<td>{_safe_html(stats.get('p50'))}</td>"
            f"<td>{_safe_html(stats.get('p90'))}</td>"
            f"<td>{_safe_html(stats.get('p99'))}</td>"
            f"<td>{_safe_html(stats.get('max'))}</td>"
            "</tr>"
        )
    return "<table>" + "".join(rows) + "</table>"


def _render_group_table(group_summary: dict[str, dict[str, dict[str, Any]]]) -> str:
    rows = [
        "<tr><th>group</th><th>key</th><th>events</th><th>units</th>"
        "<th>model calls</th><th>peak units</th><th>peak pre-VLM</th><th>peak outbound</th></tr>"
    ]
    for group, values in group_summary.items():
        for key, data in values.items():
            rows.append(
                "<tr>"
                f"<td>{_safe_html(group)}</td>"
                f"<td>{_safe_html(key)}</td>"
                f"<td>{_safe_html(data.get('event_count'))}</td>"
                f"<td>{_safe_html(data.get('unit_count'))}</td>"
                f"<td>{_safe_html(data.get('model_call_count'))}</td>"
                f"<td>{_safe_html(data.get('peak_active_units'))}</td>"
                f"<td>{_safe_html(data.get('peak_pre_vlm_active'))}</td>"
                f"<td>{_safe_html(data.get('peak_outbound_vlm_calls'))}</td>"
                "</tr>"
            )
    return "<table>" + "".join(rows) + "</table>"


def _render_top_slow_units_table(rows_data: list[dict[str, Any]]) -> str:
    rows = [
        "<tr><th>unit_id</th><th>job_id</th><th>scale</th><th>unit_kind</th>"
        "<th>model_call_id</th><th>slowest_stage</th><th>slowest_ms</th><th>total_unit_ms</th></tr>"
    ]
    for row in rows_data:
        rows.append(
            "<tr>"
            f"<td>{_safe_html(row.get('unit_id'))}</td>"
            f"<td>{_safe_html(row.get('analysis_job_id'))}</td>"
            f"<td>{_safe_html(row.get('analysis_scale'))}</td>"
            f"<td>{_safe_html(row.get('unit_kind'))}</td>"
            f"<td>{_safe_html(row.get('model_call_id'))}</td>"
            f"<td>{_safe_html(row.get('slowest_stage'))}</td>"
            f"<td>{_safe_html(row.get('slowest_stage_ms'))}</td>"
            f"<td>{_safe_html(row.get('total_unit_ms'))}</td>"
            "</tr>"
        )
    return "<table>" + "".join(rows) + "</table>"


def _render_dashboard_html(fig: go.Figure, payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    config = payload.get("config", {})
    figure_html = pio.to_html(fig, include_plotlyjs=True, full_html=False)
    summary_table = _render_key_value_table(
        [
            (
                "selected_time_range",
                f"{summary['time_range']['start']} -> {summary['time_range']['end']}",
            ),
            ("event_count", summary.get("event_count")),
            ("job_count", summary.get("job_count")),
            ("unit_count", summary.get("unit_count")),
            ("model_call_count", summary.get("model_call_count")),
            ("worker.max_concurrent_jobs", config.get("worker.max_concurrent_jobs")),
            ("worker.max_unit_workers_per_job", config.get("worker.max_unit_workers_per_job")),
            ("vlm.max_concurrent_requests", config.get("vlm.max_concurrent_requests")),
            (
                "observability.timeline_export_max_events",
                config.get("observability.timeline_export_max_events"),
            ),
            ("peak_active_units", summary.get("peak_active_units")),
            ("peak_pre_vlm_active", summary.get("peak_pre_vlm_active")),
            ("peak_frame_select_active", summary.get("peak_frame_select_active")),
            ("peak_media_refs_build_active", summary.get("peak_media_refs_build_active")),
            ("peak_detector_gate_active", summary.get("peak_detector_gate_active")),
            ("peak_scheduler_waiters", summary.get("peak_scheduler_waiters")),
            ("peak_outbound_vlm_calls", summary.get("peak_outbound_vlm_calls")),
            ("peak_vlm_ready_rate_per_sec", summary.get("peak_vlm_ready_rate_per_sec")),
            (
                "peak_provider_start_rate_per_sec",
                summary.get("peak_provider_start_rate_per_sec"),
            ),
            ("publication_write_count", summary.get("publication_write_count")),
        ]
    )
    hints = "".join(
        f"<li>{_safe_html(hint)}</li>" for hint in summary.get("bottleneck_hints", [])
    )
    css = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  margin: 24px; color: #1f2937; }
h1, h2 { color: #111827; }
.panel { border: 1px solid #d1d5db; border-radius: 12px; padding: 16px;
  margin: 16px 0; background: #f9fafb; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 13px; }
th, td { border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #e5e7eb; }
li { margin: 6px 0; }
"""
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>CCTV Memory Bottleneck Dashboard</title>
<style>{css}</style>
</head>
<body>
<h1>CCTV Memory Bottleneck Dashboard</h1>
<section class="panel">
<h2>Bottleneck Summary</h2>
{summary_table}
<h2>Likely Bottleneck Hints</h2>
<ul>{hints}</ul>
</section>
<section class="panel">
<h2>Stage Latency Percentiles</h2>
{_render_stage_latency_table(payload['stage_latency_ms'])}
</section>
<section class="panel">
<h2>Group Breakdown</h2>
{_render_group_table(payload['group_summary'])}
</section>
<section class="panel">
<h2>Top Slow Units</h2>
{_render_top_slow_units_table(payload['top_slow_units'])}
</section>
<section class="panel">
<h2>Timeline and Concurrency Curves</h2>
{figure_html}
</section>
</body>
</html>"""
    return _PLOTLY_CLOUD_URL_RE.sub("", html)


def export_timeline(
    *,
    analysis_job_id: str,
    events: list[AnalysisTimelineEvent],
    html_out: str | Path,
    json_out: str | Path | None = None,
) -> TimelineExportResult:
    html_path = Path(html_out)
    html_path.parent.mkdir(parents=True, exist_ok=True)

    payload = timeline_events_to_payload(analysis_job_id=analysis_job_id, events=events)
    fig = build_timeline_figure(events)
    html = pio.to_html(fig, include_plotlyjs=True, full_html=True)
    html = _PLOTLY_CLOUD_URL_RE.sub("", html)
    html_path.write_text(html, encoding="utf-8")

    json_path: str | None = None
    if json_out is not None:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        json_path = str(path)

    return TimelineExportResult(
        analysis_job_id=analysis_job_id,
        event_count=len(events),
        html_path=str(html_path),
        json_path=json_path,
    )


def export_aggregate_timeline(
    *,
    events: list[AnalysisTimelineEvent],
    html_out: str | Path,
    json_out: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> TimelineExportResult:
    html_path = Path(html_out)
    html_path.parent.mkdir(parents=True, exist_ok=True)

    payload = timeline_events_to_aggregate_payload(events=events, config=config)
    fig = build_aggregate_timeline_figure(events, config=config)
    html = _render_dashboard_html(fig, payload)
    html_path.write_text(html, encoding="utf-8")

    json_path: str | None = None
    if json_out is not None:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        json_path = str(path)

    return TimelineExportResult(
        analysis_job_id="all",
        event_count=len(events),
        html_path=str(html_path),
        json_path=json_path,
    )
