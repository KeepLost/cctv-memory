"""VLM output contract (schema-contracts §7, vlm-analysis-contract §4).

VLM output must NOT contain access_policy_id / security_level. Because
``ContractModel`` uses ``extra="forbid"``, any such forbidden field is
rejected at validation time.
"""

from __future__ import annotations

from pydantic import Field

from cctv_memory.contracts.common import SCHEMA_VERSION, ContractModel
from cctv_memory.domain.enums import AnalysisScale


class VlmSegmentRequest(ContractModel):
    """VLM segment analysis request (schema-contracts §6.3).

    Carries only non-sensitive context. Policy/security are NOT included and are
    system-derived after analysis (vlm-analysis-contract §1, §4).
    """

    schema_version: str = SCHEMA_VERSION
    request_id: str
    analysis_job_id: str
    video_id: str
    camera_id: str
    analysis_scale: AnalysisScale
    segment_start_ms: int = Field(ge=0)
    segment_end_ms: int = Field(ge=0)
    frame_uris: list[str] = Field(default_factory=list)
    prompt_version: str | None = None
    model_version: str | None = None
    tag_vocabulary_hints: list[str] = Field(default_factory=list)


class VlmQuality(ContractModel):
    """VLM self-assessed quality (vlm-analysis-contract §6).

    ``reason`` briefly states what was uncertain / hard to see (replaces the old
    free-form ``uncertainties`` list and ``quality.visibility``). ``score`` is the
    model's confidence in the description (0-1).
    """

    reason: str = ""
    score: float = Field(default=0.0, ge=0.0, le=1.0)


class VlmAttr(ContractModel):
    """Structured VLM attributes (slim).

    ``alert`` is the ONLY field: True iff the clip shows a threat to personal or
    public safety (someone endangering others/themselves, public-safety hazard,
    someone in danger). Everyday/normal activity is always False. The VLM never
    sets access_policy_id / security_level (those are system-derived).
    """

    alert: bool = False


class VlmObservationOutput(ContractModel):
    """Validated VLM output (schema-contracts §7.1).

    Slim format (task cctv-memory-20260611-2214): ``static`` / ``dynamic`` /
    ``tags`` / ``quality`` / ``attr``. The removed legacy fields (schema_version,
    uncertainties, attributes.objects/event_phase, quality.visibility) are no
    longer emitted. Forbidden fields (access_policy_id, security_level, camera_id,
    timing, etc.) are rejected via ``extra="forbid"``; those are system-derived.
    """

    static: str
    dynamic: str
    tags: list[str] = Field(default_factory=list)
    quality: VlmQuality = Field(default_factory=VlmQuality)
    attr: VlmAttr = Field(default_factory=VlmAttr)
