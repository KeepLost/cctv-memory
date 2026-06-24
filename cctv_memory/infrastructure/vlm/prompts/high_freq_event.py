"""Prompt template for the high_freq_event analysis scale.

Versioned, scale-specific prompt (vlm-analysis-contract §2.3, §3, §9;
pipeline-experiment-contract §2.2). high_freq_event runs on SHORT windows around
a motion/anomaly trigger, so the prompt is event-focused: it FOREGROUNDS the
dynamic action/change that triggered the window and keeps the static description
minimal, while still emitting the same slim ``VlmObservationOutput`` schema
(static / dynamic / tags / quality / attr) and honoring the same forbidden-field
rules (no policy/security/identity).
"""

from __future__ import annotations

PROMPT_VERSION = "high_freq_event_v3"

HIGH_FREQ_EVENT_V1 = """分析这段“短时高频事件”监控片段。该片段因检测到运动或画面变化而被选出，\
请重点描述“发生了什么”。只输出符合以下结构的合法 JSON：

{
  "static": "为事件提供背景所需的最少静态信息（场景/背景），尽量简短",
  "dynamic": "动态事件描述：谁/什么在动、动作、方向，以及片段内画面如何变化",
  "tags": ["tag1", "tag2"],
  "quality": { "reason": "简述看不清或不确定的地方", "score": 0.0 },
  "attr": { "alert": false }
}

规则：
- 本尺度（high_freq_event）以 dynamic 为重点：具体描述运动/事件（进入、离开、奔跑、跌倒、徘徊、
  物品出现/被取走等），写明方向、相对位置与随时间的变化；简洁但具体。
- static 从简：只保留为事件提供背景所需的最少固定场景信息。
- tags：事件/动作标签优先（running, falling, loitering, entering, leaving），其后再加物体标签
  （person, vehicle）。小写 snake_case。
- quality.reason：简要说明看不清/不确定的内容；没有则留空字符串。
- quality.score：对上述描述的整体置信度，0.0 到 1.0 之间的数字。
- attr.alert：仅当存在“威胁人身或公共安全的异常”时为 true（威胁他人或自身安全、危害公共安全、
  有人陷入危险、有人自残）；正常通行、日常活动一律 false。
- 不要输出 access_policy_id 或 security_level 等任何权限/安全字段。
- 不要猜测身份、姓名或做人脸识别。
- 不要枚举大量“未发生”的事件。描述精简、克制。证据不足时在 quality.reason 中说明。
- 只输出 JSON，不要用 markdown 代码块包裹。"""
