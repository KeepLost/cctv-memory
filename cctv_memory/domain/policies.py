"""Domain policies: authorization scope computation, segment planning.

Pure domain logic only — no FastAPI, SQLAlchemy, or vendor SDK imports
(ARCHITECTURE_CONSTITUTION §3, module-map §2.2). These functions take and
return contract DTOs / value types and contain the security and planning
rules that the application layer orchestrates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from cctv_memory.contracts.auth import AccessPolicy, AuthorizedScope, Principal
from cctv_memory.contracts.video import CameraDevice, CameraLocation
from cctv_memory.domain.enums import Capability, PrincipalType, SecurityLevel

# Default capabilities granted to an AI-facing user principal
# (authorization-policy-contract §2).
_DEFAULT_USER_CAPABILITIES: tuple[Capability, ...] = (
    Capability.OBSERVATION_SEARCH,
    Capability.OBSERVATION_READ_DETAIL,
    Capability.OBSERVATION_READ_LOCATOR,
    Capability.VIDEO_PLAYBACK,
)

# Additional capabilities for service accounts / submitters.
_SERVICE_CAPABILITIES: tuple[Capability, ...] = (
    Capability.ANALYSIS_SUBMIT,
    Capability.ANALYSIS_RERUN,
)

# Admin capabilities (management). Admins do NOT bypass audit (auth §1).
_ADMIN_CAPABILITIES: tuple[Capability, ...] = (
    Capability.OBSERVATION_SEARCH,
    Capability.OBSERVATION_READ_DETAIL,
    Capability.OBSERVATION_READ_LOCATOR,
    Capability.VIDEO_PLAYBACK,
    Capability.ANALYSIS_SUBMIT,
    Capability.ANALYSIS_RERUN,
    Capability.ANALYSIS_PUBLISH,
    Capability.CAMERA_MANAGE,
    Capability.POLICY_MANAGE,
    Capability.USER_MANAGE,
    Capability.AUDIT_READ,
    Capability.RUNTIME_MANAGE,
)


# Shared placeholder location id that hosts cameras provisioned lazily from
# analysis requests without prior registration (lenient camera_id provisioning).
# Lives in the domain layer so BOTH the worker (lazy provisioning) and the
# application seed (eager pre-create) can reference the same constant without a
# workers->application reverse dependency (ARCHITECTURE_CONSTITUTION §3).
AUTO_LOCATION_ID = "loc_auto_unregistered"


def build_auto_location() -> CameraLocation:
    """Build the canonical minimal placeholder location for unregistered cameras.

    Identity/policy/security are system-derived (default INTERNAL), never from VLM
    output (ARCHITECTURE_CONSTITUTION §5). Idempotent callers (seed / lazy
    provisioning) upsert this so location_id is always resolvable.
    """
    return CameraLocation(
        location_id=AUTO_LOCATION_ID,
        area="unregistered",
        location_desc="Auto-created for an unregistered camera",
        security_level=SecurityLevel.INTERNAL,
    )


def capabilities_for(principal: Principal) -> list[Capability]:
    """Return the capability set for a principal by its type (MVP role mapping)."""
    if principal.principal_type is PrincipalType.ADMIN:
        return list(_ADMIN_CAPABILITIES)
    if principal.principal_type is PrincipalType.SERVICE_ACCOUNT:
        return list(_DEFAULT_USER_CAPABILITIES) + list(_SERVICE_CAPABILITIES)
    return list(_DEFAULT_USER_CAPABILITIES)


def policy_permits_principal(policy: AccessPolicy, principal: Principal) -> bool:
    """Return True if ``principal`` is permitted by ``policy`` (auth §3).

    ``denied_principals`` takes precedence; then role/group/principal allow-lists.
    """
    rules = policy.rules
    if principal.principal_id in rules.denied_principals:
        return False
    if principal.principal_id in rules.allowed_principals:
        return True
    if any(role in rules.allowed_roles for role in principal.roles):
        return True
    if any(group in rules.allowed_groups for group in principal.groups):
        return True
    # No allow-list match. An empty rule set means no grant (fail closed).
    return False


def effective_security_level(
    location: CameraLocation,
    camera: CameraDevice | None,
    policy: AccessPolicy | None,
) -> SecurityLevel:
    """Return the stricter (higher) security level along the inheritance chain (§5.1)."""
    level = location.security_level
    if policy is not None:
        level = SecurityLevel.stricter(level, policy.security_level)
    return level


def resolve_access_policy_id(
    location: CameraLocation,
    camera: CameraDevice | None,
    video_access_policy_id: str | None,
    default_policy_id: str,
) -> str:
    """Resolve the effective access_policy_id via the inheritance chain (auth §5).

    VideoSource -> CameraDevice -> CameraLocation -> system default.
    """
    if video_access_policy_id:
        return video_access_policy_id
    if camera is not None and camera.access_policy_id:
        return camera.access_policy_id
    if location.access_policy_id:
        return location.access_policy_id
    return default_policy_id


def compute_scope_hash(
    *,
    tenant_id: str,
    principal_id: str,
    allowed_camera_ids: list[str],
    allowed_location_ids: list[str],
    allowed_access_policy_ids: list[str],
    max_security_level: SecurityLevel,
    capabilities: list[Capability],
) -> str:
    """Compute a stable scope hash binding all resource dimensions + capabilities.

    Must be deterministic so it can bind a SearchContext (auth §4 / §4.1).
    """
    payload = {
        "tenant_id": tenant_id,
        "principal_id": principal_id,
        "allowed_camera_ids": sorted(allowed_camera_ids),
        "allowed_location_ids": sorted(allowed_location_ids),
        "allowed_access_policy_ids": sorted(allowed_access_policy_ids),
        "max_security_level": max_security_level.value,
        "capabilities": sorted(c.value for c in capabilities),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "scope_" + hashlib.sha256(encoded).hexdigest()[:32]


def compute_authorized_scope(
    *,
    principal: Principal,
    policies: list[AccessPolicy],
    locations: list[CameraLocation],
    cameras: list[CameraDevice],
) -> AuthorizedScope:
    """Compute the AuthorizedScope for a principal (auth §4, §4.1).

    Resource dimensions are derived from the policies the principal is permitted
    to use, then the locations/cameras bound to those policies. Empty allowed_*
    lists mean NO access for that dimension (fail closed); we never substitute an
    "unlimited" interpretation.
    """
    permitted_policy_ids = {
        policy.access_policy_id
        for policy in policies
        if policy_permits_principal(policy, principal)
    }
    policy_by_id = {p.access_policy_id: p for p in policies}

    allowed_location_ids: set[str] = set()
    for loc in locations:
        if loc.tenant_id != principal.tenant_id:
            continue
        if loc.access_policy_id and loc.access_policy_id in permitted_policy_ids:
            allowed_location_ids.add(loc.location_id)

    allowed_camera_ids: set[str] = set()
    for cam in cameras:
        if cam.tenant_id != principal.tenant_id:
            continue
        # A camera is allowed if its own policy is permitted, or it inherits a
        # permitted location's policy.
        cam_policy_ok = bool(cam.access_policy_id) and cam.access_policy_id in permitted_policy_ids
        loc_inherited_ok = cam.location_id in allowed_location_ids
        if cam_policy_ok or loc_inherited_ok:
            allowed_camera_ids.add(cam.camera_id)

    # max_security_level: the highest level among permitted policies the principal
    # may view. If no policy is permitted, default to PUBLIC (least access).
    if permitted_policy_ids:
        max_level = SecurityLevel.PUBLIC
        for pid in permitted_policy_ids:
            policy = policy_by_id.get(pid)
            if policy is not None:
                max_level = SecurityLevel.stricter(max_level, policy.security_level)
    else:
        max_level = SecurityLevel.PUBLIC

    capabilities = capabilities_for(principal)
    scope_hash = compute_scope_hash(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        allowed_camera_ids=list(allowed_camera_ids),
        allowed_location_ids=list(allowed_location_ids),
        allowed_access_policy_ids=list(permitted_policy_ids),
        max_security_level=max_level,
        capabilities=capabilities,
    )
    return AuthorizedScope(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        allowed_camera_ids=sorted(allowed_camera_ids),
        allowed_location_ids=sorted(allowed_location_ids),
        allowed_access_policy_ids=sorted(permitted_policy_ids),
        max_security_level=max_level,
        capabilities=capabilities,
        scope_hash=scope_hash,
    )


@dataclass(frozen=True)
class SegmentWindow:
    """A planned analysis window in milliseconds relative to video start."""

    start_ms: int
    end_ms: int


def plan_default_segments(
    duration_ms: int, *, window_seconds: int, overlap_seconds: int
) -> list[SegmentWindow]:
    """Plan fixed default_segment windows over a video (vlm-analysis-contract §2.1).

    Windows of ``window_seconds`` with ``overlap_seconds`` overlap. The final
    window is clamped to ``duration_ms``. Pure function — deterministic.
    """
    if duration_ms <= 0:
        return []
    window_ms = window_seconds * 1000
    overlap_ms = overlap_seconds * 1000
    if window_ms <= 0:
        raise ValueError("window_seconds must be positive")
    step = window_ms - overlap_ms
    if step <= 0:
        raise ValueError("overlap_seconds must be smaller than window_seconds")

    windows: list[SegmentWindow] = []
    start = 0
    while start < duration_ms:
        end = min(start + window_ms, duration_ms)
        windows.append(SegmentWindow(start_ms=start, end_ms=end))
        if end >= duration_ms:
            break
        start += step
    return windows


@dataclass(frozen=True)
class MotionSample:
    """A motion score at a sampled timestamp (relative to video start, ms).

    ``score`` is a normalized [0,1] inter-frame change magnitude produced by the
    motion detector. Higher means more change since the previous sample.
    """

    timestamp_ms: int
    score: float


@dataclass(frozen=True)
class TriggerWindow:
    """A planned high-frequency trigger window in ms (relative to video start)."""

    start_ms: int
    end_ms: int
    peak_score: float
    reason: str = "motion_spike"


def plan_motion_sample_timestamps(
    duration_ms: int, *, sample_interval_ms: int
) -> list[int]:
    """Plan evenly spaced motion-sampling timestamps over a video.

    Pure/deterministic (vlm-analysis-contract §2.2: cheap motion scan). Returns
    timestamps at ``0, interval, 2*interval, ...`` strictly within the duration.
    """
    if duration_ms <= 0:
        return []
    if sample_interval_ms <= 0:
        raise ValueError("sample_interval_ms must be positive")
    return list(range(0, duration_ms, sample_interval_ms))


def plan_motion_triggers(
    samples: list[MotionSample],
    *,
    threshold: float,
    min_duration_ms: int,
    merge_gap_ms: int = 0,
    reason: str = "motion_spike",
    duration_ms: int | None = None,
) -> list[TriggerWindow]:
    """Derive trigger windows from motion samples (vlm-analysis-contract §2.2).

    A trigger spans contiguous samples whose ``score >= threshold``. Adjacent runs
    separated by a gap of at most ``merge_gap_ms`` are merged. Each resulting
    window must be at least ``min_duration_ms`` long (short blips are dropped);
    a window's ``end_ms`` extends to the next sample after the last in-run sample
    (or, for the final sample, by the median sampling step) so it has real span.
    When ``duration_ms`` is provided, trigger windows are clamped to
    ``[0, duration_ms]`` and any window that would start at/after EOF (or whose
    clamped span collapses to <= 0) is dropped, so a motion spike near the very end
    of the video never yields a window beyond the decodable range (task
    cctv-memory-20260612-1854). Pure/deterministic — the application persists the
    windows as HighFreqTriggers.
    """
    if not samples:
        return []
    ordered = sorted(samples, key=lambda s: s.timestamp_ms)
    # Estimate the sampling step to give the last in-run sample a real end.
    step = _median_step([s.timestamp_ms for s in ordered])

    def sample_end(index: int) -> int:
        if index + 1 < len(ordered):
            return ordered[index + 1].timestamp_ms
        return ordered[index].timestamp_ms + step

    # Build raw runs of consecutive above-threshold samples.
    raw: list[tuple[int, int, float]] = []  # (start_ms, end_ms, peak)
    run_start: int | None = None
    run_peak = 0.0
    for i, s in enumerate(ordered):
        if s.score >= threshold:
            if run_start is None:
                run_start = s.timestamp_ms
                run_peak = s.score
            else:
                run_peak = max(run_peak, s.score)
        else:
            if run_start is not None:
                raw.append((run_start, sample_end(i - 1), run_peak))
                run_start = None
                run_peak = 0.0
    if run_start is not None:
        raw.append((run_start, sample_end(len(ordered) - 1), run_peak))

    # Merge runs separated by <= merge_gap_ms.
    merged: list[list[float]] = []
    for start_ms, end_ms, peak in raw:
        if merged and start_ms - int(merged[-1][1]) <= merge_gap_ms:
            merged[-1][1] = float(max(int(merged[-1][1]), end_ms))
            merged[-1][2] = max(merged[-1][2], peak)
        else:
            merged.append([float(start_ms), float(end_ms), peak])

    windows: list[TriggerWindow] = []
    for start_ms_f, end_ms_f, peak in merged:
        start_ms = int(start_ms_f)
        end_ms = int(end_ms_f)
        if end_ms - start_ms < min_duration_ms:
            # Extend a too-short window to the minimum duration so it is usable.
            end_ms = start_ms + min_duration_ms
        if duration_ms is not None:
            # Clamp to the real video duration; drop windows starting at/after EOF
            # or whose clamped span collapses (near-EOF correctness).
            if start_ms >= duration_ms:
                continue
            end_ms = min(end_ms, duration_ms)
            if end_ms <= start_ms:
                continue
        windows.append(
            TriggerWindow(
                start_ms=start_ms, end_ms=end_ms, peak_score=peak, reason=reason
            )
        )
    return windows


def _median_step(timestamps: list[int]) -> int:
    """Median spacing between sorted timestamps (>=1ms). Used to size end bounds."""
    if len(timestamps) < 2:
        return 1
    diffs = sorted(
        timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)
    )
    mid = diffs[len(diffs) // 2]
    return max(1, mid)


def plan_high_freq_windows(
    trigger_start_ms: int,
    trigger_end_ms: int,
    *,
    window_seconds: int,
    overlap_ratio: float,
    duration_ms: int | None = None,
    min_window_ms: int = 1,
) -> list[SegmentWindow]:
    """Plan short high_freq_event windows over a trigger span (contract §2.3).

    Windows of ``window_seconds`` step by ``window*(1-overlap_ratio)``; the final
    window is clamped to ``trigger_end_ms``. If the trigger is shorter than one
    window, a single clamped window is returned. When ``duration_ms`` is provided
    the trigger span is first clamped to ``[0, duration_ms]`` and any window whose
    span is below ``min_window_ms`` after clamping is dropped, so a trigger that
    extends past video EOF never produces an out-of-range window (task
    cctv-memory-20260612-1854). Pure/deterministic.
    """
    start_bound = trigger_start_ms
    end_bound = trigger_end_ms
    if duration_ms is not None:
        end_bound = min(end_bound, duration_ms)
        if start_bound >= duration_ms:
            return []
    if end_bound <= start_bound:
        return []
    window_ms = window_seconds * 1000
    if window_ms <= 0:
        raise ValueError("window_seconds must be positive")
    if not 0.0 <= overlap_ratio < 1.0:
        raise ValueError("overlap_ratio must be in [0, 1)")
    span = end_bound - start_bound
    if window_ms >= span:
        if span < min_window_ms:
            return []
        return [SegmentWindow(start_ms=start_bound, end_ms=end_bound)]
    step = max(1, int(window_ms * (1.0 - overlap_ratio)))
    windows: list[SegmentWindow] = []
    start = start_bound
    while start < end_bound:
        end = min(start + window_ms, end_bound)
        if end - start >= min_window_ms:
            windows.append(SegmentWindow(start_ms=start, end_ms=end))
        if end >= end_bound:
            break
        start += step
    return windows


@dataclass(frozen=True)
class FrameScore:
    """Per-frame scalar metrics for VLM frame selection (frame-stream design §4.1).

    Produced by the OpenCV FrameStream adapter from downscaled grayscale frames;
    carries NO pixels (constitution §3/§4 — no ndarray across the boundary). All
    fields are scalars so selection is a pure, deterministic domain function.

    - ``frame_index``/``timestamp_ms``: the frame's identity in the decoded stream.
    - ``motion``: normalized mean-abs-diff vs the previous scored frame, [0,1].
    - ``scene``: normalized histogram change vs the previous frame, [0,1].
    - ``blur``: variance of Laplacian (higher = sharper); not normalized.
    - ``brightness``: mean grayscale value, [0,255].
    """

    frame_index: int
    timestamp_ms: int
    motion: float
    scene: float
    blur: float
    brightness: float


def select_frames(
    scores: list[FrameScore],
    k: int,
    *,
    strategy: str = "bins_then_score",
    w_motion: float = 1.0,
    w_scene: float = 0.5,
    w_quality: float = 0.5,
    min_blur: float = 50.0,
    bins: int | None = None,
) -> list[FrameScore]:
    """Pick ``k`` frames from per-frame scores (frame-stream design §4.2/§10).

    Pure/deterministic — no cv2/numpy, no I/O. Strategies:
    - ``uniform``: evenly spaced over the (timestamp-ordered) eligible frames.
    - ``score``: top-``k`` by composite score (favors motion/scene peaks).
    - ``bins_then_score``: partition the time span into ``bins`` buckets, take the
      best-scoring frame per bucket (guarantees temporal coverage), then fill any
      remaining budget with the next highest-scoring frames.

    Quality gate: frames with ``blur < min_blur`` or extreme brightness are
    dropped UNLESS that would leave nothing (then the gate is ignored so we never
    return empty for a non-empty input). Ties break deterministically by
    ``frame_index``. The result is ALWAYS returned sorted by ``timestamp_ms``
    ascending (vlm-analysis-contract §4.5: VLM input must be chronological).

    Returns the selected ``FrameScore`` objects (callers map them to frames).
    """
    if k <= 0 or not scores:
        return []
    ordered = sorted(scores, key=lambda s: (s.timestamp_ms, s.frame_index))

    def quality_ok(s: FrameScore) -> bool:
        return s.blur >= min_blur and 16.0 <= s.brightness <= 240.0

    eligible = [s for s in ordered if quality_ok(s)] or ordered

    def composite(s: FrameScore) -> tuple[float, int]:
        # Normalize blur into [0,1] with a soft cap so it cannot dominate.
        q = min(s.blur, 500.0) / 500.0
        score = w_motion * s.motion + w_scene * s.scene + w_quality * q
        # -frame_index makes ties deterministic (earlier frame wins on equal score).
        return (score, -s.frame_index)

    if strategy == "uniform":
        if len(eligible) <= k:
            picked = list(eligible)
        else:
            step = len(eligible) / float(k)
            picked = [eligible[min(len(eligible) - 1, int(i * step))] for i in range(k)]
            # De-dup while preserving order (int() collisions on dense clusters).
            seen_idx: set[int] = set()
            deduped: list[FrameScore] = []
            for s in picked:
                if s.frame_index not in seen_idx:
                    seen_idx.add(s.frame_index)
                    deduped.append(s)
            picked = deduped
    elif strategy == "score":
        picked = sorted(eligible, key=composite, reverse=True)[:k]
    else:  # bins_then_score (default)
        nb = bins if bins is not None else k
        nb = max(1, min(nb, k))
        picked = []
        chosen_indices: set[int] = set()
        if nb > 0 and len(ordered) > 0:
            t0 = ordered[0].timestamp_ms
            span = max(1, ordered[-1].timestamp_ms - t0 + 1)
            buckets: list[list[FrameScore]] = [[] for _ in range(nb)]
            for s in eligible:
                idx = min(nb - 1, (s.timestamp_ms - t0) * nb // span)
                buckets[idx].append(s)
            for bucket in buckets:
                if bucket:
                    best = max(bucket, key=composite)
                    picked.append(best)
                    chosen_indices.add(best.frame_index)
        rest = sorted(
            (s for s in eligible if s.frame_index not in chosen_indices),
            key=composite,
            reverse=True,
        )
        picked += rest[: max(0, k - len(picked))]

    picked = picked[:k]
    return sorted(picked, key=lambda s: (s.timestamp_ms, s.frame_index))
