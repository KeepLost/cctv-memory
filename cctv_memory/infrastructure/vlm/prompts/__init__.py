"""VLM prompt templates package.

Scale-aware prompt selection (vlm-analysis-contract §2/§3/§9): each analysis
scale maps to its own versioned template. ``build_prompt`` selects the template
by scale; ``prompt_version_for_scale`` returns the matching prompt_version so
records are traceable to the exact prompt that produced them.
"""

from __future__ import annotations

from cctv_memory.domain.enums import AnalysisScale
from cctv_memory.infrastructure.vlm.prompts.default_segment import (
    DEFAULT_SEGMENT_V1,
    PROMPT_VERSION,
    STRICT_RETRY_INSTRUCTION,
    STRICT_RETRY_SUFFIX,
)
from cctv_memory.infrastructure.vlm.prompts.high_freq_event import (
    HIGH_FREQ_EVENT_V1,
)
from cctv_memory.infrastructure.vlm.prompts.high_freq_event import (
    PROMPT_VERSION as HIGH_FREQ_EVENT_PROMPT_VERSION,
)

# Templates and versions keyed by the scale that uses them. Scales without a
# dedicated template (e.g. motion_scan, which does not call the VLM) fall back to
# the default_segment template.
_TEMPLATES: dict[AnalysisScale, str] = {
    AnalysisScale.DEFAULT_SEGMENT: DEFAULT_SEGMENT_V1,
    AnalysisScale.HIGH_FREQ_EVENT: HIGH_FREQ_EVENT_V1,
}
_VERSIONS: dict[AnalysisScale, str] = {
    AnalysisScale.DEFAULT_SEGMENT: PROMPT_VERSION,
    AnalysisScale.HIGH_FREQ_EVENT: HIGH_FREQ_EVENT_PROMPT_VERSION,
}


def prompt_version_for_scale(scale: AnalysisScale) -> str:
    """Return the prompt_version used for ``scale`` (default_segment fallback)."""
    return _VERSIONS.get(scale, PROMPT_VERSION)


def build_prompt(
    *, scale: AnalysisScale = AnalysisScale.DEFAULT_SEGMENT, strict: bool = False
) -> str:
    """Return the scale-appropriate STABLE prompt template.

    Scale selection makes the prompt differ by analysis_scale (event-focused for
    high_freq_event vs. balanced baseline for default_segment). Unknown scales
    fall back to the default_segment template.

    The returned template is the STABLE prefix used as the system message
    (task cctv-memory-20260616-1339, P2): it is byte-identical across all requests
    of a scale so the provider can reuse it as a cached prefix. ``strict`` is kept
    for backward compatibility (it appends the legacy retry suffix), but the real
    adapter NO LONGER mutates the prefix on retry — it appends
    ``STRICT_RETRY_INSTRUCTION`` as a separate trailing user segment instead, so
    the system prefix stays stable. Prefer the stable template (strict=False).
    """
    template = _TEMPLATES.get(scale, DEFAULT_SEGMENT_V1)
    if strict:
        return template + STRICT_RETRY_SUFFIX
    return template


__all__ = [
    "DEFAULT_SEGMENT_V1",
    "HIGH_FREQ_EVENT_V1",
    "PROMPT_VERSION",
    "HIGH_FREQ_EVENT_PROMPT_VERSION",
    "STRICT_RETRY_INSTRUCTION",
    "STRICT_RETRY_SUFFIX",
    "build_prompt",
    "prompt_version_for_scale",
]
