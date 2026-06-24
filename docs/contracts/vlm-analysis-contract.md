# VLM Analysis Contract（视频理解分析契约）

## 0. 文档目的

本文定义视频切片、抽帧、VLM 输入、VLM 输出、prompt/version、质量校验和失败处理规则。

VLM adapter 不得直接写 active ObservationRecord。它只生成 validated VlmSegmentResult，交给 Publication flow。

## 0.1 Unit 生命周期（任务 cctv-memory-20260611-1410 新增）

每个 VLM 调用单元（default_segment 窗口 / high_freq_event 触发窗口）都有持久化的 AnalysisUnit 状态：

```text
pending -> running -> succeeded
pending -> running -> failed
```

- **每个成功单元立刻通过 PublicationService 发布**，不等待 scale 内其它单元。
- scale 内部分单元失败 -> AnalysisScaleTask.status = partial_failed；
  AnalysisJob 同步降级到 partial_failed（而非整体 failed）。
- 单元幂等键（idempotency_key）保证 crash 恢复不重复发布。
- ModelCallLog 记录每次调用的文本输入输出、媒体 refs/metadata、耗时、状态。
  不在 DB 中存 base64 媒体 blob；details 见 table-schema-spec §4.4–§4.5。

> 说明（跨 scale unit 调度, 任务 cctv-memory-20260611-1905, Stage C2）：motion_scan 产出
> trigger 后，default_segment 与 high_freq_event 的 unit 由 worker 内统一进程级优先级队列
> 调度（`workers/cross_scale_scheduler.py`）。high_freq unit 优先（确定性配额防饥饿，default
> 永不饿死），但这只改变 **VLM 请求派发顺序**，不改变：per-unit 幂等 + 立即发布语义（本节
> §0.1）、motion_scan→high_freq 硬依赖、多图必须按 `timestamp_ms` 升序（§4）、provider 全局
> 并发/限速（由 Stage C1 全局 `VlmScheduler` 施加）。unit 跨 scale 乱序完成是安全的；每个
> scale 在其全部 unit 终态后独立 finalize（job-state-machine-contract §3.5）。
>
> 多 job 并发（任务 cctv-memory-20260615-1620）：当 `worker.max_concurrent_jobs>1` 时多个 job 同时
> 处理，但 provider 调用上限仍由**唯一进程级共享 `VlmScheduler`** 跨所有并发 job/unit/retry 强制——
> 全进程在途 VLM 调用数恒 ≤ `vlm.max_concurrent_requests`，并发 job 数不会叠加放大该上限。
> 每个 unit 仍走 per-unit 幂等 + 立即发布（自然键 upsert 精确一次），故跨 job 并发完成是安全的。

---

## 1. Inputs

VLM 分析必须接收：

```text
analysis_job_id
video_id
camera_id
video_start_time
analysis_scale
segment_start_ms
segment_end_ms
frame_uris or clip_uri
model_version
prompt_version
pipeline_version
tag_vocabulary_hints
```

`camera_id/video_start_time` 必须来自外部可信输入或 VideoSource，不由 VLM 推断。

### 1.1 Media Input Shape（媒体输入形态）

VLM 段输入支持两种形态，由配置 `vlm.media_input` 选择（仅对 `provider=real` 有意义；
mock 不解码媒体）：

```text
frames（默认）= 抽帧后多图：每段抽 frames_per_segment 张 JPEG，逐帧作为多个 image part 发送
video（显式启用）= 整段视频：整段 clip 作为单个 video-MIME part 发送
```

规则：

- **默认即 `frames`（抽帧多图）**：本地部署更稳妥的默认；`video` 为非默认、需显式开启。
- **默认不带音频**：`frames` 形态天然无音频；`video` 形态在 `include_audio=false`（默认）时
  剥离音轨（ffmpeg `-an -c:v copy`）后再发送。仅 `include_audio=true` 且 `media_input=video`
  时才携带原始音轨。
- 二进制依赖：`frames`（含旧的 `pipeline.video_metadata_mode=ffmpeg_frames`）需要 `ffprobe`+`ffmpeg`；
  `video` 需要 `ffprobe`，并在默认剥音轨时需要 `ffmpeg`。
- 实现：`workers/analysis_worker.py:_default_video_processor` 选择 processor；
  `infrastructure/vlm/real_adapter.py:RealVlmAnalyzer._build_media_parts` 构造多图/单视频 part。

> 说明（OpenCV FrameStream, 任务 cctv-memory-20260611-1805）：`frames` 形态的默认实现是
> `infrastructure/video/opencv_frame_stream.py:OpenCvFrameStreamVideoProcessor`
> （`pipeline.decode_backend=opencv`，默认）：一次流式解码 + 有界环形缓冲 + 在线标量评分 +
> 度量驱动选帧（`domain/policies.py:select_frames`）+ 选中帧 JPEG 原子落盘。选帧可由度量驱动，
> 但 **materialize 与发送 VLM 的顺序必须按 `timestamp_ms` 升序**（多图时序正确性，见 §4 输出
> 语义）。worker 经 `workers/frame_selection.py:select_frames_for_unit` 取帧，
> `ModelCallLog.media_refs` 额外记录每帧 `frame_index/timestamp_ms/decode_backend/
> selection_reason` 及 motion/scene/blur/brightness 标量（仍不含 base64/source_uri，
> table-schema-spec §4.5）。裸 `np.ndarray` 永不跨出 infra adapter（宪法 §3/§4）。
> `ffmpeg` 后端（`SegmentFrameVideoProcessor`）保留为可配置后端与解码失败时的回退。

---

## 2. Analysis Scale Rules

### 2.1 default_segment

Purpose:

```text
stable baseline observation over regular windows
```

Initial window suggestion:

```text
10-30 seconds, configurable
```

Detector-gated VLM foundation（任务 cctv-memory-20260622-1800）：

- `default_segment` 是固定窗口 observation 生产路径，不再等价于“每个窗口一定调用 VLM”。
- 当 `pipeline.detector_gate.enabled=true` 时，worker 在每个 default window 抽帧后、VLM 前运行轻量 detector gate。
- gate-positive 窗口继续调用 VLM；VLM 文本/tags 保持原样，detector 信息只附加到 `ObservationRecord.attributes.detector_gate`。
- gate-negative 窗口不调用 VLM，但必须发布 detector-only ObservationRecord：`static_description_text=""`、`dynamic_description_text=""`、`tags=[]`，detector summary 只在 attributes/attr 中。
- 每个 gate 决策必须写入 `detector_gate_logs`，包含逐帧 metadata/hash evidence 和决策依据；默认不保存媒体 bytes/base64/source_uri/绝对路径。

### 2.2 motion_scan

Purpose:

```text
cheap motion/change detection to find high_freq_event triggers
```

May use CV/frame differencing, not necessarily VLM.

`motion_scan` 不产出 active ObservationRecord；它只产出 HighFreqTrigger 或 skipped/metrics 状态。任何可检索的语义记录必须来自 `default_segment`、`high_freq_event` 或未来明确设计的语义分析尺度。

实现：`infrastructure/video/motion_detector.py:FrameDiffMotionDetector`（单次有界 ffmpeg
下采样灰度帧差，stdlib 计算归一化平均绝对差）+ `domain/policies.py:plan_motion_triggers`
（样本→触发窗口）+ `workers/motion_scan.py:MotionScanProcessor`（幂等持久化
HighFreqTrigger，不写记录）。参数见 `pipeline.motion_scan`。

detector 是可插拔的：`MotionScanProcessor` 与 worker 只依赖 `services/motion_detector.py:
MotionDetectorPort` 抽象，具体实现由 `infrastructure/video/motion_detector_factory.py`
按 `pipeline.motion_scan.method` 选择（`frame_diff` 为首个注册实现）。新增 detector 通过注册表
接入，不改 application/domain/worker 业务逻辑（configuration-contract §2、
pipeline-experiment-contract §2.3）。

### 2.3 high_freq_event

Purpose:

```text
short windows around motion/anomaly triggers
```

Used for detailed event semantics.

实现：`workers/high_freq_event.py:HighFreqEventProcessor` 消费本 job 的
HighFreqTrigger，按 `pipeline.high_freq_event` 规划短窗口
（`domain/policies.py:plan_high_freq_windows`），用 high_freq_event 专用 prompt
（`infrastructure/vlm/prompts/high_freq_event.py`，`prompt_version=high_freq_event_v1`）
调用 VLM，并经 Publication 发布 `analysis_scale=high_freq_event` 的记录。无触发时该
scale task 记为 `skipped(no_motion_trigger)`。

> near-EOF 窗口 clamp（任务 cctv-memory-20260612-1854）：`plan_motion_triggers` 与
> `plan_high_freq_windows` 接受可选 `duration_ms`，把触发/高频窗口 clamp 到 `[0, duration_ms]`；
> 起点 >= 视频时长、或 clamp 后跨度过小（< `min_window_ms`）的窗口直接丢弃，避免在视频末尾产生
> 越界窗口导致解码零帧。motion_scan 在 default 探测前运行时用最后一个运动采样时间戳作为兜底 EOF
> 边界；high_freq 用已探测的 `VideoSource.duration_ms`。default_segment 窗口本就 clamp 到时长，
> 行为不变。

### 2.4 low_freq_summary

Post-MVP optional. Do not build core logic that depends on it.

---

## 3. Prompt Contract

Prompt 按 `analysis_scale` 选择版本化模板（scale-aware）：`default_segment` →
`default_segment_v3`，`high_freq_event` → `high_freq_event_v3`（事件聚焦）。实现见
`infrastructure/vlm/prompts/__init__.py:build_prompt(scale=...)` /
`prompt_version_for_scale`；`real_adapter` 按 `request.analysis_scale` 取 prompt。
每条记录写入对应的 `prompt_version`，使尺度行为可被实验追溯（§9）。

> 说明（cache 友好消息布局, 任务 cctv-memory-20260616-1339, P2）：prompt 文本作为**稳定前缀**
> 经 `system` 角色发送，逐请求字节相同以命中供应商隐式 prefix cache；每段视频的图像 part 放在其后的
> `user` 消息内，媒体顺序仍按 `timestamp_ms` 升序（§4）。strict 重试**不修改 system 稳定前缀**——
> 严格 JSON 提醒作为独立的尾部 user 文本段追加（`STRICT_RETRY_INSTRUCTION`），保证前缀稳定。
> `response_format`（如 `{"type":"json_object"}`）仅经 `vlm.extra_body` opt-in（默认不开），
> 不破坏不支持该字段的供应商。此布局变更属模型可见结构变更，故 prompt_version 从 `*_v2` bump 到
> `*_v3`（见 §9）。

> 说明（输出精简 + 尺度侧重, 任务 cctv-memory-20260611-2214）：prompt 语义实质变更，版本
> 从 `*_v1` bump 到 `*_v2`。两个模板都要求输出 §4 的精简 JSON（static/dynamic/tags/quality/
> attr），并区分侧重点：**default_segment 以 static 为重点、dynamic 从简；high_freq_event 以
> dynamic 为重点、static 从简**。两者都要求精简克制、禁止枚举大量“未发生”的事件。

Prompt template variables:

```text
analysis_scale
camera_context
location_context
video_time_context
segment_relative_time
frame_count
model_output_schema
tag_vocabulary_hints
negative_instruction
```

Prompt must instruct model:

```text
separate static (静态画面) from dynamic (动态事件)
use coarse tags only
state uncertainty briefly in quality.reason when visual evidence is weak
set attr.alert=true ONLY for personal/public-safety threats
never output access_policy_id/security_level
never invent exact identity/ReID
do not enumerate large lists of non-events
```

---

## 4. VLM Output Schema

VLM output must validate against `VlmObservationOutput` in `schema-contracts.md`
（精简格式, 任务 cctv-memory-20260611-2214）：

```text
static          # 静态画面描述（原 static_description_text）
dynamic         # 动态事件描述（原 dynamic_description_text）
tags            # string array（不变）
quality.reason  # 简述不确定/看不清的内容（新增，替代 uncertainties + quality.visibility）
quality.score   # 置信度 0-1（原 quality.confidence）
attr.alert      # boolean，仅表示人身/公共安全威胁异常（新增）
```

被移除字段（不再出现在 VLM 输出中）：`schema_version`、`uncertainties`、
`attributes.objects`、`attributes.event_phase`、`quality.visibility`。

Rules:

- static/dynamic 必须是字符串，可为空但不能缺失；
- tags must be string array；
- quality.score 必须是 0-1 之间的数字；quality.reason 是字符串（可为空）；
- attr.alert 是 boolean；仅当存在威胁人身/公共安全的异常时为 true，正常活动一律 false；
- policy/security fields are forbidden（ContractModel `extra="forbid"` 强制）；
- record timing, camera, location, policy are system-derived outside VLM output；
- 内部映射：`static/dynamic` → ObservationRecord 的 `*_description_text`；`quality` 与
  `attr.alert` 写入 ObservationRecord.attributes JSON（`{"quality": {...}, "alert": bool}`），
  DB schema 不变（workers/common.py:build_observation_record）。

---

## 5. Tag Contract

Tags are coarse filters/facets/boost hints, not complete object attribute database.

Tag rules:

```text
lowercase snake_case preferred
short semantic labels
no sensitive identity guess
no raw natural-language sentences
```

Examples:

```text
person
backpack
dark_clothing
loitering
vehicle
doorway
falling
running
```

---

## 6. Quality / Uncertainty / Alert

Quality object（精简, 任务 cctv-memory-20260611-2214）：

```text
reason   # 简述看不清/不确定的内容（自由文本，可为空），替代旧的 uncertainties 列表与 visibility
score    # 置信度 0-1，替代旧的 quality.confidence
```

`attr.alert`（boolean）：仅表示视频中是否存在**威胁人身/公共安全的异常**（威胁他人或自身安全、
危害公共安全、有人陷入危险、有人自残）；其他情况一律 false。alert 不是权限/安全分级，不参与
access_policy_id/security_level 派生。

Low score（低置信度）does not automatically discard a result; Publication policy
decides acceptance thresholds.

---

## 7. Validation and Normalization

Before Publication:

```text
parse JSON
normalize tags
strip forbidden fields (access_policy_id/security_level/camera_id/... )
ensure static/dynamic are strings
ensure quality.score in [0,1], quality.reason is string
ensure attr.alert is boolean
attach system-derived timing/camera/location/policy metadata
```

If validation fails:

```text
mark analysis unit failed with vlm_schema_validation_failed
store sanitized raw/error snippet for debugging if allowed
never publish invalid output to active ObservationRecord
```

### 7.1 抽帧结果语义（任务 cctv-memory-20260612-1854）

抽帧在 `mark_running` 之后、VLM 调用之前，处于 per-unit 终态处理内：

```text
零可用帧（near-EOF / 越界窗口）-> unit skipped, last_error_code=insufficient_frames（非失败）
少于请求数但 >=1 帧        -> 仍照常送 VLM（不因没凑满帧而失败）
抽帧/media-ref 其它异常     -> unit failed, last_error_code=frame_extraction_failed + 记 failed ModelCallLog
```

OpenCV 适配器对「解码零帧」抛 `InsufficientFramesError`（域异常，非 RuntimeError），不触发
ffmpeg 回退；真正的打开/解码错误仍抛 `RuntimeError` 走回退或映射 `frame_extraction_failed`。

---

## 8. Provider Failure Handling

Retryable:

```text
provider timeout
rate limit
transient network error
5xx provider error
```

Non-retryable:

```text
prompt template missing
unsupported media format
schema repeatedly invalid after configured retries
input segment missing
```

> 单元级瞬时重试 + 逐次审计（任务 cctv-memory-20260615-1447）：worker 的 per-unit runner 在同一
> `running` unit 内对上面 Retryable 一类（统一表现为 `VlmProviderError`）做有界重试——次数
> `vlm.unit_max_attempts`（默认 3），指数退避 + 抖动，**每次尝试仍经全局 VlmScheduler**（并发/限速
> 不被绕过）；Non-retryable 一类不重试，立即落终态。这是在**适配器自身 `max_retries`（单次逻辑调用内
> 的传输级/重提示重试）之上的第二层**重试。
>
> ModelCallLog 逐次记录：每次失败的尝试各写一条 `status=failed` 的 ModelCallLog，`attempt_count`
> 为真实尝试序号，`attempt_details` 记录 `error_type` / `transient` / `backoff_ms`；最终成功写一条
> `status=succeeded` 的 ModelCallLog，`attempt_count` 为成功时序号、`attempt_details` 为完整尝试轨迹。
> `AnalysisUnit.attempt_count` = 实际模型尝试次数，`max_attempts` = 配置预算。重试达预算上限 -> unit
> `failed(vlm_provider_error)`，绝不残留 `running`，发布保持精确一次（自然键 upsert）。

---

## 9. Versioning

Every result must carry:

```text
model_version
prompt_version
pipeline_version
```

Changing prompt semantics requires new `prompt_version`（任务 cctv-memory-20260611-2214
将 `default_segment_v1/high_freq_event_v1` bump 到 `*_v2`；任务 cctv-memory-20260616-1339
因 cache 友好的 system 前缀 + 图像后置布局变更，将 `*_v2` bump 到 `*_v3`，见 §3）。
Changing VLM output fields or interpretation requires schema/version update。
`VlmObservationOutput` 不再带 `schema_version` 字段（精简移除）；DTO 形状的演进由代码契约
（`contracts/vlm.py`）+ 本文档版本化记录，跨进程消息仍由各自 envelope 的 schema_version 承载。

---

## 10. Contract Tests

```text
valid_vlm_output_passes
missing_text_field_fails
policy_field_in_vlm_output_rejected
tags_normalized
low_confidence_result_marked_but_not_crashes
invalid_json_sets_vlm_schema_validation_failed
publication_receives_system_derived_metadata_not_vlm_policy
```
