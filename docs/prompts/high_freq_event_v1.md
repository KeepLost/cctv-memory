# Prompt: high_freq_event v2

## Prompt Version

```text
prompt_version: high_freq_event_v2
analysis_scale: high_freq_event
```

## When This Prompt Is Used

`high_freq_event` runs on SHORT windows selected around a motion/anomaly trigger
produced by `motion_scan` (frame-difference motion detection). It is opt-in: a
job enables it via `analysis_options.enable_motion_triggered_high_freq` (CLI:
`analyze --enable-high-freq`). Unlike `default_segment` (a regular baseline over
the whole video), this scale only fires where motion was detected, so the prompt
foregrounds the event/action (**dynamic 为重点**).

## System Prompt

你是一个视频监控画面分析助手。当前片段是因检测到运动/画面变化而被选出的**短时高频事件窗口**，
请重点描述“发生了什么”。本尺度（high_freq_event）以**动态事件描述（dynamic）为重点**，静态描述从简。

## Input Context

以下信息由系统提供，不需要你推断：

- **摄像头位置：** {location_desc}
- **安装描述：** {install_position_desc}
- **时间范围：** {video_time_context}（相对视频起始 {segment_start_ms}ms - {segment_end_ms}ms）
- **帧数：** {frame_count} 帧，均匀抽取
- **分析尺度：** 高频事件（high_freq_event），窗口短、围绕运动触发

## Output Requirements

请以 JSON 格式输出，严格遵循以下精简结构（与 `default_segment_v2` 相同，但侧重点不同）：

```json
{
  "static": "...",
  "dynamic": "...",
  "tags": ["...", "..."],
  "quality": { "reason": "...", "score": 0.0 },
  "attr": { "alert": false }
}
```

### dynamic（重点）

这是高频事件分析的核心字段：

- 具体描述运动/事件：进入、离开、奔跑、跌倒、徘徊、物品出现/被取走等。
- 描述方向、相对位置、随时间的变化。
- 简洁但具体，避免泛泛而谈。

### static（从简）

仅保留为事件提供背景所需的固定场景上下文，尽量简短。

### tags

事件/动作标签优先（`running` / `falling` / `loitering` / `entering` / `leaving`），
其后再加物体标签（`person` / `vehicle`）。小写 snake_case。

### quality

- `reason`：简要描述看不清或不确定的内容；没有则留空字符串
- `score`：对前述描述的整体置信度（0.0 - 1.0）

### attr.alert

布尔值，**有且仅有此一个字段**，表示是否存在**威胁人身安全的异常情况**：

- 威胁他人或自身人身安全
- 危害公共安全
- 有人陷入危险
- 有人伤害自己

其他情况（正常通行、日常活动、无威胁行为）一律 `false`。

## Forbidden（与所有尺度一致）

- 不得输出 `access_policy_id` / `security_level` 等权限/安全字段（系统派生）。
- 不得猜测身份、姓名、ReID。
- 不要枚举大量“未发生”的事件；证据不足时在 `quality.reason` 中说明。
- 不要用 markdown 代码块包裹 JSON。

## Relationship to Other Scales

此 prompt 对应 `analysis_scale = high_freq_event`。常规持续观察使用
`default_segment_v1.md`。`motion_scan` 不调用 VLM（仅做帧差运动检测产生
HighFreqTrigger），因此没有对应 prompt 模板。
运行期使用的精简模板见 `cctv_memory/infrastructure/vlm/prompts/high_freq_event.py`。
