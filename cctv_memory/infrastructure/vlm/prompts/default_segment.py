"""Prompt templates for VLM observation analysis (default_segment scale).

Versioned prompt templates (pipeline-experiment-contract §2.2). The prompt
instructs the model to emit the slim JSON matching ``VlmObservationOutput``
(static / dynamic / tags / quality / attr) and forbids policy/security/identity
fields (vlm-analysis-contract §3/§4).

Scale-aware selection lives in ``prompts/__init__.py:build_prompt`` /
``prompt_version_for_scale``; this module owns the default_segment template only.
The default_segment scale FOREGROUNDS the static scene description and keeps the
dynamic description brief (vlm-analysis-contract §2.1).
"""

from __future__ import annotations

PROMPT_VERSION = "default_segment_v3"

DEFAULT_SEGMENT_V1 = """分析这段监控摄像头视频。只输出符合以下结构的合法 JSON：

{
  "static": "静态画面描述：场景与环境、人物外观（衣着颜色/款式、携带物）、显著物体",
  "dynamic": "动态事件描述：明显的动作/移动/变化",
  "tags": ["tag1", "tag2"],
  "quality": { "reason": "简述看不清或不确定的地方", "score": 0.0 },
  "attr": { "alert": false }
}

规则：
- 本尺度（default_segment）以 static 为重点：详细描述场景、人物外观、物体；只写画面中可见的内容。
- dynamic 从简：只记录明显的动作/事件，不展开、不罗列未发生的事。
- tags：小写 snake_case 短标签（如 person, vehicle, door, walking, loitering）。
- quality.reason：简要说明看不清/不确定的内容；没有则留空字符串。
- quality.score：对上述描述的整体置信度，0.0 到 1.0 之间的数字。
- attr.alert：仅当画面存在“威胁人身或公共安全的异常”时为 true（威胁他人或自身安全、危害公共安全、
  有人陷入危险、有人自残）；正常通行、日常活动一律 false。
- 不要输出 access_policy_id 或 security_level 等任何权限/安全字段。
- 不要猜测身份、姓名或做人脸识别。
- 不要枚举大量“未发生”的事件（如“未见摔倒、未见打斗”）。
- 描述精简、克制，不说废话。证据不足时在 quality.reason 中说明。
- 只输出 JSON，不要用 markdown 代码块包裹。"""

STRICT_RETRY_SUFFIX = """

IMPORTANT: Your previous response could not be parsed as the required JSON. \
Respond with a SINGLE valid JSON object only — no prose, no markdown fences, \
no leading or trailing text. All keys shown above are required."""

# Strict retry guidance as a STANDALONE instruction (task cctv-memory-20260616-1339,
# P2): appended as a separate trailing user segment on retry so the STABLE system
# prefix is never mutated (prefix stays byte-identical for provider prompt cache).
STRICT_RETRY_INSTRUCTION = (
    "IMPORTANT: Your previous response could not be parsed as the required JSON. "
    "Respond with a SINGLE valid JSON object only — no prose, no markdown fences, "
    "no leading or trailing text. All keys shown above are required."
)
