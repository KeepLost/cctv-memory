# CCTV Memory 数据存储、视频分析与检索设计草案

## 0. 文档状态

- 项目：`cctv-memory`
- 当前阶段：需求与规格澄清
- 本文范围：记录目前已经讨论清楚的 **视频源接入、视频分析流水线、数据存储、索引、检索机制、AnalysisJob 与审计机制**。
- 暂不包含：具体 VLM 模型选型、最终 prompt 文案、运动检测算法阈值、前端 UI、完整权限管理 UI / 复杂组织架构同步 / 审批流、部署拓扑、ReID 实现、正式告警系统。
- 第一版必须包含：客户端-服务端身份边界、服务端鉴权/授权、AI-facing 检索只读边界、检索前资源权限过滤、locator 二次鉴权和基础审计预留。
- 配套工程契约、模块边界与技术栈建议见：`architecture-contracts-and-tech-stack.md`。
- 配套 API 格式、服务运行与客户端-服务端交互设计见：`api-and-service-runtime-design.md`。
- 本文保留总体设计背景和 rationale；具体字段、接口、权限、检索、状态机、错误码、测试、配置和备份以对应 contract/spec 文档为准。
- 若本文与 contract/spec 文档冲突，以 `docs/ARCHITECTURE_CONSTITUTION.md` 和 `docs/CONTEXT_MANIFEST.md` 定义的权威顺序为准。

本文当前结论以企业楼宇 / 公司 CCTV 监控视频内容理解与检索为目标，不是会议转写或语音转写系统。

---

## 1. 项目目标

本项目目标是构建一套面向企业楼宇监控视频的文字化记忆与检索系统。

系统接收外部提交的视频文件或已经落盘的 RTSP chunk 文件，将视频切分为时间片段后，由视觉语言模型生成该片段的文字记录，并建立可被 AI 调用的检索接口。

最终希望支持用户通过自然语言询问：

- 某个时间、某个摄像头、某个区域发生了什么；
- 有没有某种外观特征的人，例如穿深色衣服、背包、戴帽子；
- 有没有某类动态事件，例如徘徊、摔倒、尾随、争执、异常停留；
- 某个视频片段是否还有类似片段；
- 检索结果能否定位回原视频的具体时间段。

第一版核心是监控视频画面内容的文字化记录与检索。语音、ReID、实时告警、前端审核工作流都不是第一版主轴。

---

## 2. 总体设计原则

### 2.1 本程序只分析视频文件 / 已落盘 chunk

本程序不负责管理 RTSP 流生命周期。

RTSP 连接、断流重连、长期录制、chunk 切分、文件落盘等应由外部 recorder / NVR 管理程序负责。

本程序职责边界是：

```text
外部提交视频文件 / 已落盘 RTSP chunk
  ↓
本程序分析视频内容
  ↓
生成文字记录 ObservationRecord
  ↓
写入数据库和索引
  ↓
提供 AI 可调用检索接口
```

RTSP 来源进入本程序时，应表现为一个已经可读取的视频文件：

```text
source_type = rtsp_chunk
source_uri = /data/chunks/cam_lobby_01_20260606_210000.mp4
original_source_uri = rtsp://example/stream/1   # 可选，仅用于来源追溯
source_status = ready
```

不在本程序内设计长期 RTSP stream 父对象，也不通过 `parent_video_id` 管理 RTSP 流和 chunk 的父子关系。

### 2.2 视频真实时间必须由外部强制传入

系统不从画面内容或文件 metadata 中猜测视频真实开始时间。

外部通过 CLI 或 HTTP 提交分析任务时，必须传入：

```text
video_start_time
camera_id
source_uri
source_type
```

所有 ObservationRecord 的真实时间由以下方式推导：

```text
absolute_start_time = VideoSource.video_start_time + segment_start_ms
absolute_end_time   = VideoSource.video_start_time + segment_end_ms
```

如果外部无法提供可信 `video_start_time`，第一版应拒绝进入正式分析流程，避免污染时间检索。

### 2.3 固定事实可以结构化

关系数据库只结构化系统可确定、客观稳定的内容，例如：

- 摄像头设备 ID；
- 摄像头所属物理空间；
- 视频源；
- 视频起止时间；
- 片段相对视频的起止时间；
- 分析尺度；
- 模型版本、prompt 版本、pipeline 版本；
- 分析任务和审计信息。

### 2.4 视觉语义不进入强结构字段

不要把开放世界视觉语义设计成关系数据库字段，例如：

- 人的衣着、外观、年龄段、性别判断；
- 携带物，例如背包、手提袋、箱子；
- 动作，例如走路、奔跑、徘徊、摔倒；
- 事件类型，例如尾随、冲突、异常停留；
- 风险等级等模型主观判断。

这些内容来自视觉模型理解，不是固定事实。若把它们关系化，会带来：

- schema 不断膨胀；
- 类别维护灾难；
- 召回误伤；
- 伪确定性；
- 后续需求变化时难以维护。

因此视觉语义只进入自然语言描述和 tags，不进入强结构字段。

### 2.5 视频源与文字记录分开

视频源与观察记录必须分开存储。

一个视频源可以对应多个时间片段记录；一条观察记录只通过 `video_id` 和片段时间偏移定位到原视频。

这样第一版使用视频文件，未来使用外部 recorder 生成的 RTSP chunk 或对象存储文件时，不需要改变观察记录的核心结构。

### 2.6 检索是 AI 工具，不是单次向量搜索

检索层不应只是：

```text
用户问题 -> 生成 embedding -> 向量库 topK
```

而应提供一组 AI 可调用的工具，使 AI 可以像调用 memory-lancedb-pro 一样进行多轮检索：

1. 先用结构化字段缩小范围；
2. 用 tags 做粗召回；
3. 查看候选集统计；
4. 选择静态描述、动态描述或混合搜索；
5. 根据需要优先选择某种 `analysis_scale`；
6. 必要时重排序；
7. 返回证据化结果，或诚实说明没有找到。

---

## 3. 外部触发接口

外部程序不应直接调用内部分析函数，而应通过“提交视频源分析任务”的方式通知系统。

推荐提供两种入口：

1. CLI：用于本地测试、离线调试、批处理脚本。
2. HTTP API：用于实际部署，由外部系统、NVR 管理服务、调度器或业务系统调用。

两种入口进入同一套内部任务队列，创建同一种 VideoSource 与 AnalysisJob，后续处理流程一致。

### 3.1 CLI 入口

文件示例：

```bash
cctv-memory submit \
  --source-type file \
  --source-uri /data/company-surveillance-videos/lobby_20260606_210000.mp4 \
  --camera-id cam_lobby_01 \
  --video-start-time 2026-06-06T21:00:00+08:00
```

RTSP 来源应先由外部 recorder 落成 chunk 文件，再提交该 chunk：

```bash
cctv-memory submit \
  --source-type rtsp_chunk \
  --source-uri /data/company-surveillance-videos/chunks/cam_lobby_01_20260606_210000.mp4 \
  --camera-id cam_lobby_01 \
  --video-start-time 2026-06-06T21:00:00+08:00 \
  --original-source-uri rtsp://example/stream/1
```

### 3.2 HTTP 入口

建议接口：

```http
POST /api/v1/video-sources/analyze
```

文件请求示例：

```json
{
  "source_type": "file",
  "source_uri": "/data/company-surveillance-videos/lobby_20260606_210000.mp4",
  "camera_id": "cam_lobby_01",
  "video_start_time": "2026-06-06T21:00:00+08:00",
  "external_source_id": "nvr-export-20260606-001",
  "idempotency_key": "nvr-export-20260606-001",
  "metadata": {
    "submitted_by": "external-system",
    "note": "NVR exported file"
  },
  "analysis_options": {
    "enable_default_segment": true,
    "enable_motion_triggered_high_freq": true
  }
}
```

RTSP chunk 请求示例：

```json
{
  "source_type": "rtsp_chunk",
  "source_uri": "/data/company-surveillance-videos/chunks/cam_lobby_01_20260606_210000.mp4",
  "original_source_uri": "rtsp://example/stream/1",
  "camera_id": "cam_lobby_01",
  "video_start_time": "2026-06-06T21:00:00+08:00",
  "external_source_id": "nvr-channel-1-chunk-20260606-210000",
  "idempotency_key": "nvr-channel-1-chunk-20260606-210000",
  "analysis_options": {
    "enable_default_segment": true,
    "enable_motion_triggered_high_freq": true
  }
}
```

返回示例：

```json
{
  "video_id": "video_001",
  "source_status": "ready",
  "analysis_job_id": "job_001",
  "accepted": true
}
```

### 3.3 文件路径安全

外部提交 `source_uri` 时，本程序不应读取任意系统路径。

应配置专门的视频根目录，例如：

```text
VIDEO_ROOT=/data/company-surveillance-videos
```

所有本地文件路径必须在 canonicalize / resolve 后仍位于 `VIDEO_ROOT` 内。任何试图通过 `../`、软链接、绝对路径绕出根目录的请求都应报错。

HTTP 部署中还应配合：

- 鉴权；
- 请求大小限制；
- source_uri 类型白名单；
- 审计日志；
- 幂等键检查。

---

## 4. 数据存储设计

核心对象：

```text
CameraLocation
CameraDevice
VideoSource
AnalysisJob
AnalysisScaleTask
HighFreqTrigger
ObservationRecord
ObservationRecordHistory
SearchContext / SearchRevision / SearchCandidate
```

### 4.1 CameraLocation：物理空间表

用于描述摄像头所在的建筑空间。

建议字段：

```text
location_id           主键
building              楼栋
floor                 楼层
area                  区域，例如大厅、走廊、机房区
room_or_zone          房间或更细区域，可选
location_desc         文字描述，例如“一楼大厅入口靠近闸机处”
access_policy_id      可选，访问策略；机密区域可绑定更严格策略
security_level        public / internal / confidential 等，可选但推荐
created_at
updated_at
```

说明：

- 物理空间是摄像头设备的属性，不应重复写入每条观察记录。
- 机密场所、研发区域、实验室等区域的访问限制优先绑定在 CameraLocation 上，再由 CameraDevice / VideoSource / ObservationRecord 继承或冗余。
- 后续可以根据企业实际空间模型扩展楼栋、园区、部门等字段。

### 4.2 CameraDevice：摄像头设备表

用于记录摄像头设备本身及其与物理空间的关系。

建议字段：

```text
camera_id             主键，摄像头设备 ID
camera_name           设备名称
location_id           外键，关联 CameraLocation
manufacturer          制造商，可选
model                 型号，可选
serial_number         序列号，可选
install_position_desc 安装位置描述，例如“大厅入口上方朝向电梯”
stream_uri            默认视频流地址，可选，仅作为设备信息
access_policy_id      可选，设备级访问策略；为空则继承 CameraLocation
status                active / inactive / maintenance
created_at
updated_at
```

说明：

- 摄像头高度附着于特定物理空间，因此设备和空间关系单独维护。
- `stream_uri` 表示设备当前默认流地址；实际被处理的视频来源仍以 `VideoSource.source_uri` 为准。
- 本程序不管理 RTSP 流生命周期。

### 4.3 VideoSource：视频源表

表示一段被系统处理的视频文件或已落盘 chunk。

建议字段：

```text
video_id              主键
source_type           file / rtsp_chunk / object_storage / external
source_uri            文件路径、对象存储 URI、RTSP chunk 文件 URI
original_source_uri   可选；RTSP chunk 可记录原始 RTSP 地址，仅作来源追溯
camera_id             外键，关联 CameraDevice
video_start_time      外部强制传入的视频现实开始时间
video_end_time        可由开始时间 + 时长推导或外部传入
duration_ms           视频时长
source_status         pending / ready / failed
external_source_id    可选；外部系统传入的文件、通道或任务 ID
access_policy_id      可选，视频源级访问策略；通常继承 CameraDevice / CameraLocation
created_at
updated_at
```

幂等性与去重约束：
- 在物理表上应对 `(camera_id, video_start_time)` 建立 UNIQUE 约束。
- 确保同一摄像头在同一绝对现实时间，绝不允许并发创建多条 VideoSource 从而触发重复分析。
- 即使外部系统使用不同的 `external_source_id` 重复推送相同时间的切片文件，底层数据库也能拒绝重复污染。

说明：

- 第一版主要是 `file`。
- RTSP 来源进入本程序时应是 `rtsp_chunk`，即外部 recorder 已落盘的视频文件。
- `VideoSource` 负责描述视频来源和摄像头关系。
- 一段视频可以被切成多个 ObservationRecord。
- `video_start_time + segment_start_ms` 可以推导观察记录的绝对开始时间。

### 4.4 AnalysisJob：分析任务表

外部提交视频源分析任务后，应创建 `AnalysisJob`。所谓重跑，也应表现为提交一个新的 AnalysisJob，而不是在原 job 内部直接改写结果。

建议字段：

```text
analysis_job_id       主键
video_id              外键，关联 VideoSource
job_status            queued / running / succeeded / partial_failed / failed
idempotency_key       幂等键
analysis_options      JSON，记录是否启用默认尺度、高频触发等
model_version
prompt_version
pipeline_version
created_record_ids    本次发布创建的新 record_id 列表
updated_record_ids    本次发布更新/替换的 record_id 列表
archived_record_ids   本次发布归档的旧 record_id 列表
failed_segment_ids    失败片段 ID 或时间范围列表，可选
created_at
started_at
finished_at
error_code
error_message
```

说明：

- CLI 和 HTTP 入口都进入同一套 AnalysisJob 流程。
- 中途取消不作为 MVP 能力。
- 未来如果确实需要干预，优先考虑人工改写分析记录或提交新的 AnalysisJob，而不是强行取消已发出的 VLM 请求。

### 4.5 AnalysisScaleTask：不同分析尺度子任务

`AnalysisJob` 是父任务，不应直接等同于某一种频率尺度的分析。一个 AnalysisJob 下可以包含多个尺度子任务。

建议逻辑对象：`AnalysisScaleTask`。

常见尺度：

```text
default_segment   默认尺度持续分析，MVP 必选
motion_scan       低成本运动/突变扫描，用于决定是否触发高频尺度
high_freq_event   高频异常尺度分析，由 motion_scan / HighFreqTrigger 触发
low_freq_summary  低频场景摘要，未来可选，不进入 MVP 默认链路
```

建议字段：

```text
scale_task_id          主键
analysis_job_id        外键
analysis_scale         default_segment / motion_scan / high_freq_event / low_freq_summary
status                 pending / running / succeeded / partial_failed / failed / skipped
total_units            总片段/触发数量
succeeded_units        成功数量
failed_units           失败数量
skipped_reason         可选，例如 not_enabled / no_motion_trigger
created_at
started_at
finished_at
error_code
error_message
```

状态聚合规则：

- 默认尺度成功，未触发高频：job 可为 `succeeded`。
- 默认尺度成功，高频部分失败：job 可为 `partial_failed`。
- 默认尺度失败且没有有效记录：job 应为 `failed`。
- 低频摘要未启用不影响 MVP job 成功。
- `high_freq_event = skipped` 可以表示 motion scan 未发现足够运动突变，不是失败。

### 4.6 HighFreqTrigger：高频尺度触发记录

HighFreqTrigger 属于 AnalysisJob 内部、`high_freq_event` 尺度子任务下的触发记录/子任务，用于表示“某个时间范围因为运动/突变信号而需要高频尺度分析”。它不是业务异常结论。

建议字段：

```text
trigger_id
analysis_job_id
scale_task_id
video_id
trigger_start_ms
trigger_end_ms
motion_score       可选
change_score       可选
trigger_reason     motion_spike / local_motion_spike / scene_change / fast_movement 等
status             pending / running / succeeded / failed / skipped
idempotency_key
created_at
updated_at
error_code
error_message
```

幂等性建议：

```text
analysis_job_id + video_id + trigger_start_ms + trigger_end_ms + trigger_reason
```

失败的 trigger 可以由运维或调试工具手动重跑。手动重跑可以创建新的 AnalysisJob 或新的 trigger run，并保留原失败记录，重跑结果同样进入审计记录。

### 4.7 ObservationRecord：当前有效视频片段观察记录表

表示一个视频时间片段的当前 active 文字化观察记录。

建议字段：

```text
record_id                 主键
video_id                  外键，关联 VideoSource
analysis_job_id           生成该记录的 AnalysisJob
analysis_scale            default_segment / high_freq_event / low_freq_summary
segment_start_ms          片段相对视频起点的开始时间
segment_end_ms            片段相对视频起点的结束时间
observed_start_time       绝对观察开始时间，由 video_start_time + segment_start_ms 派生
observed_end_time         绝对观察结束时间，由 video_start_time + segment_end_ms 派生
camera_id                 必填冗余字段，由 VideoSource 派生，用于权限过滤/facet/index metadata
location_id               必填冗余字段，由 CameraDevice / CameraLocation 派生，用于权限过滤/facet/index metadata
static_description_text   静态描述文本
dynamic_description_text  动态描述文本
tags                      辅助关键词列表，可用 JSON/数组/文本形式保存
clip_uri                  可选，若保存了实际切片视频
thumbnail_uri             可选，若保存了缩略图
attributes                可选，JSON/JSONB 字段，存放模型透传的额外信息（例如空间坐标/Bounding Box/置信度/光照等），Schema-free，留作未来扩充空间过滤等
access_policy_id          访问策略快照/冗余字段，由 VideoSource / CameraDevice / CameraLocation 派生，不由 VLM 决定
security_level            安全级别快照/冗余字段，用于检索前权限过滤
model_version             生成描述的视觉模型版本
prompt_version            生成描述的 prompt 版本
pipeline_version          生成描述的 pipeline 版本
created_at
updated_at
```

物理去重约束：
数据库层应对 `(video_id, segment_start_ms, segment_end_ms, analysis_scale)` 设置唯一联合约束 (UNIQUE constraint)。写入使用 `UPSERT`（如 `INSERT ... ON CONFLICT DO UPDATE`），如果不同 AnalysisJob 甚至重复发送的任务命中了相同的片段尺度分析，直接覆盖，将去重逻辑下沉到数据库，避免冗余。

派生字段要求：

- 第一版必须保存 `segment_start_ms / segment_end_ms`，同时保存派生的 `observed_start_time / observed_end_time`，避免检索时重复计算绝对时间范围。
- 第一版必须在 active 表冗余 `camera_id / location_id`，避免权限过滤、facet 和索引 metadata 依赖运行时多表 join。
- `observed_start_time / observed_end_time / camera_id / location_id / access_policy_id / security_level` 都由系统派生，不由 VLM 输出决定。
- `analysis_scale` 是 ObservationRecord 中唯一和视频分析频率尺度直接相关的主字段；其他字段不因尺度不同而分叉。

### 4.8 ObservationRecordHistory：历史审计表

当前 active 记录被新的 AnalysisJob 覆盖时，旧记录不物理删除，而是进入历史审计表。

建议字段可复用 ObservationRecord 主字段，并额外记录：

```text
history_id
old_record_id
replaced_by_record_id
archived_by_analysis_job_id
archived_at
archive_reason          rerun / manual_edit / pipeline_update 等
```

说明：

- 默认检索只使用 ObservationRecord 当前 active 记录。
- 审计查询可以通过 `analysis_job_id`、`old_record_id` 或 `replaced_by_record_id` 查看历史。
- 人工改写未来可选，但也必须进入历史审计。

---

## 5. 视频切片、抽帧与分析尺度

### 5.1 核心观察

公司实验中观察到：

- 危险/异常事件更像高频信号：短时间内突变、快速动作、瞬时事件，需要短窗口 + 高 FPS 才容易捕获。
- 正常/安全事件更像低频信号：正常通行、排队、等待、日常办公，需要更长上下文才能判断。

但当前公司更看重实时性和事件及时记录，低频场景摘要不是 MVP 必要链路。

因此第一版收敛为：

```text
默认尺度持续分析
+
低成本运动/突变扫描
+
运动触发的高频尺度分析
```

### 5.2 默认尺度 default_segment

默认尺度是主干，负责持续产出文字记录，保证视频内容可检索。

建议初始参数：

```text
窗口长度：8-15 秒
overlap：2-5 秒
VLM 输入：1-2 FPS，或 4-8 张代表帧
analysis_scale = default_segment

关于重叠覆盖：
不同分析尺度之间存在物理时间上的重叠（如 high_freq_event 是对 default_segment 时间窗的特写）。本设计不为它们设置硬性的树状层级关系，而是保留平铺的信息冗余；在使用阶段，通过检索时间窗交叉接口 (`get_overlapping_records`) 查询片段间的时空联系。
```

负责：

- 普通经过；
- 人的外观；
- 携带物；
- 基本动作；
- 场景中发生了什么；
- 大多数普通检索问题。

### 5.3 运动/突变扫描 motion_scan

运动/突变扫描不负责判断具体异常类型，也不产出 active ObservationRecord。

它的职责是判断：

```text
这一段画面变化是否足够快，默认尺度可能看不清，是否需要启用高频尺度 VLM 分析？
```

可用信号包括：

- 帧差 / motion score；
- 局部区域运动强度；
- 光照突变 / 画面变化；
- 快速移动；
- 画面突然遮挡、剧烈抖动；
- 如果已有轻量检测器，可加入人体框速度变化、目标数量变化等。

具体 motion score、阈值、窗口合并、触发策略应通过后续真实视频实验调参确定；当前设计只固定职责边界。

### 5.4 高频尺度 high_freq_event

如果 motion_scan 发现某个时间段出现明显运动突变、快速移动、局部剧烈变化、画面异常变化等信号，则自动触发高频异常分析。

建议初始参数：

```text
候选事件前后扩展：例如前 2 秒、后 3-5 秒
窗口长度：2-4 秒
overlap：50% 左右
抽帧：4-8 FPS，或尽量保留短视频动态
analysis_scale = high_freq_event
```

高频尺度主要用于捕获：

- 摔倒；
- 冲突；
- 奔跑；
- 突然闯入；
- 快速移动；
- 火光/烟雾突变；
- 门禁尾随瞬间等。

高频记录通常需要关联相邻 default_segment 记录解释上下文。

### 5.5 低频摘要 low_freq_summary

低频场景摘要不进入 MVP 默认链路。

若未来公司需要长时间场景概览、拥挤度趋势、排队趋势、区域长期状态等能力，可以增加：

```text
窗口长度：30-60 秒或更长
抽帧：0.2-1 FPS
analysis_scale = low_freq_summary
```

当前阶段如果 default_segment 与 high_freq_event 已能满足检索和事件记录需求，就不增加低频摘要，以免增加延迟、成本和复杂度。

---

## 6. VLM 输出结构与 prompt 原则

VLM 输出结构应保持一致，不因为 `analysis_scale` 不同而分叉。不同尺度可以使用不同 prompt，但最终都应产出同一类 ObservationRecord 字段。

统一输出 JSON 建议：

```json
{
  "static_description_text": "...",
  "dynamic_description_text": "...",
  "tags": ["..."],
  "quality_notes": "...",
  "uncertainties": ["..."]
}
```

### 6.1 static_description_text

静态描述关注画面中相对静态、可见的特征：

- 人穿了什么；
- 有什么显著打扮；
- 携带什么物品；
- 背景中有什么明显物体；
- 场景布局和环境特点。

示例：

```text
一楼大厅入口区域画面清晰，可见玻璃门、闸机和通往电梯方向的通道。画面中有几名人员，其中一名人员穿深色外套和深色裤子，背部似乎有包类物品。
```

### 6.2 dynamic_description_text

动态描述关注动作、事件和变化：

- 人做了什么；
- 物体如何移动；
- 是否发生明显事件；
- 场景中有什么不能忽略的变化。

示例：

```text
该时间段内，几名人员从大厅入口附近经过，其中穿深色外套的人员从入口方向向电梯区域移动。画面中主要是正常通行，没有明显剧烈冲突或突发变化。
```

### 6.3 tags

Tags 是从描述中整理出的关键检索要素，例如：

```text
一楼大厅
入口
人员经过
深色外套
疑似背包
走向电梯
正常通行
```

Tags 的定位：

- 辅助粗召回；
- 查询扩展；
- rerank 特征；
- UI 高亮；
- 候选集统计。

Tags 不应替代完整描述文本，也不应作为强结构字段。

### 6.4 prompt 原则

Prompt 可以根据分析尺度分别设置：

- `default_segment` prompt：重点描述场景、人员外观/携带物、基本动作、普通事件和可检索细节。
- `high_freq_event` prompt：重点描述短时间内的画面突变、快速动作、异常候选过程、关键时间顺序和不确定性。
- `low_freq_summary` prompt：未来可选，若启用则用于长时间场景概览。

但无论使用哪种 prompt，输出 JSON schema 应保持一致。

Prompt 应要求模型：

- 只描述画面中可见内容；
- 不要做身份识别；
- 不要过度推断年龄、性别、职业、关系；
- 对不确定内容使用“疑似 / 可能 / 看起来像”；
- 静态描述关注衣着、携带物、场景、物体、布局；
- 动态描述关注移动、交互、事件、变化；
- tags 从描述中抽取，不额外创造描述中没有的事实；
- 正常片段不要枚举大量具体异常否定词。

tag_vocabulary_hints：
可以在 AnalysisJob 提交阶段或系统全局提供一个“企业预定义核心 Tag 词表”。Prompt 会引导 VLM：“请优先使用以下标签归纳画面内容：[黑衣, 背包, 徘徊...]，如不适用可自行归纳。”，这样能在兼顾开放性的同时收敛通用词汇。

具体 prompt 措辞、示例、帧输入方式和 few-shot 样例应通过后续实验调参确定。

### 6.5 否定描述注意事项

动态描述里不要在每条正常记录中枚举大量未发生事件，例如：

```text
未见摔倒、未见打斗、未见尾随、未见烟火
```

Embedding 对否定不总是可靠。若正常片段都包含这些异常词，用户搜索“摔倒”或“尾随”时可能召回大量正常片段。

正常片段可以写：

```text
正常通行，没有明显异常变化。
```

只有确实出现某类事件时，再明确写入该事件。

---

## 7. AnalysisJob、消息队列与原子发布

### 7.1 消息队列执行模型

AnalysisJob 的实际执行应通过消息队列异步推进。VLM 调用高并发且耗时不稳定，不适合同步阻塞外部 API。

可以把队列理解为若干阶段：

```text
analysis_pending_queue
vlm_request_queue
analysis_success_queue
analysis_failed_queue
```

说明：

1. `analysis_pending_queue`：等待创建尺度子任务、切片、抽帧、运动扫描和任务拆分的 job。
2. `vlm_request_queue`：已经生成具体片段输入，等待或正在送往 VLM 分析的任务。消息中应带上 `analysis_job_id`、`analysis_scale` 和片段时间范围。
3. `analysis_success_queue`：VLM 分析成功，等待校验、覆盖 ObservationRecord、写索引和审计。
4. `analysis_failed_queue`：VLM 调用失败、输出解析失败、质量校验失败或写库/写索引失败，需要记录错误和可能重试。

这些可以是实际的多个消息队列，也可以是一个队列系统里的不同 topic/status。关键是外部 API 能查询到任务处于哪个阶段，以及属于哪个分析尺度。

### 7.2 状态映射

建议状态映射：

```text
job 已提交但未开始 -> queued
任一必要尺度或片段正在执行 -> running
全部必要尺度成功，非必要尺度 skipped 或成功 -> succeeded
必要尺度部分成功、部分失败，仍有有效记录产出 -> partial_failed
没有任何有效记录产出或关键流程失败 -> failed
```

中途取消不作为 MVP 能力。

### 7.3 AnalysisJob 成功后的原子发布

AnalysisJob 一旦成功产出可用结果，应以一次原子发布操作更新当前 active 的 ObservationRecord，并记录本次发布到底写入/替换了哪些记录。

推荐规则：

1. AnalysisJob 先在临时结果区或事务上下文中完成片段分析、JSON 校验、质量检查和待写入记录准备。
2. 发布时，对同一 `video_id + segment_start_ms + segment_end_ms + analysis_scale` 的新结果，原子替换当前 active ObservationRecord。
3. 被替换的旧 active 记录进入 `ObservationRecordHistory` 或审计历史表，不物理删除。
4. 新 active 记录生成新的 `record_id` 或新的记录版本标识。
5. AnalysisJob 记录本次发布涉及的 ID 信息，例如：
   - `created_record_ids`
   - `updated_record_ids`
   - `archived_record_ids`
   - `failed_segment_ids`，如有
6. 默认检索、向量索引、FTS/BM25 只使用当前 active 记录。
7. 审计查询可以通过 `analysis_job_id` 追踪本次 job 写了哪些记录、替换了哪些旧记录。

重跑本质上是新的 AnalysisJob。新的 AnalysisJob 成功发布后，旧 job 对应的 active 记录会被新记录替换；旧记录和旧 job 的 ID 信息仍保留在审计历史中。

这些写操作应依赖数据库事务或等价的原子提交机制保证一致性。如果关系库更新成功但向量库/FTS 更新失败，应把 job 标记为 `partial_failed` 或进入补偿流程，不能静默认为完全成功。

### 7.4 人工改写分析记录

中途取消分析任务不作为 MVP 能力。未来可以提供人工改写 ObservationRecord 的能力，用于修正明显错误的 VLM 描述。

人工改写不应直接覆盖且丢弃原记录，而应：

- 生成审计记录；
- 标明 `edited_by` / `edited_at` / `edit_reason`；
- 更新当前 active ObservationRecord；
- 同步更新向量索引与 FTS/BM25。

---

## 8. 质量校验与规范化

写库前应做一层后处理：

- JSON schema 校验；
- 必填字段检查；
- tags 数量限制，例如 5-20 个；
- tag 去重、去空、长度限制；
- 禁止明显越界内容，例如身份识别、人脸识别结果、无依据的身份判断；
- 检查描述是否为空或过短；
- 检查时间范围、record_id、video_id、camera_id 是否完整。

低质量结果不要直接污染索引。可以标记为：

```text
analysis_status = success / failed / partial / low_confidence
error_code
error_message
retry_count
```

---

## 9. 索引设计

第一版建议至少建立三类可检索文本：

```text
static_description_text
dynamic_description_text
tag_text，即 tags 拼接后的文本
```

建议索引：

```text
static_description_vector
dynamic_description_vector
tag_text_vector 或 tag_text FTS
```

说明：

- 静态描述用于回答外观、携带物、场景特征相关问题。
- 动态描述用于回答动作、事件、变化相关问题。
- tags 用于粗召回、统计和 rerank 辅助。
- 可同时保留 FTS/BM25 与向量索引，便于 hybrid search。
- 默认检索和索引只使用 active ObservationRecord。

每条 ObservationRecord 至少写两条向量：

```text
record_id = obs_001, vector_type = static, text = static_description_text
record_id = obs_001, vector_type = dynamic, text = dynamic_description_text
```

可选第三条：

```text
record_id = obs_001, vector_type = tags, text = tags joined as text
```

但 tags 向量只作为辅助召回，不应替代静态 / 动态描述向量。

---

## 10. 检索总体流程

### 10.0 外部 AI 查询规划边界

本系统的核心检索接口面向外部 AI / Agent 使用。外部 AI 已知道本系统有哪些工具、每个工具的参数与含义，也可以结合自身对用户问题的上下文理解进行多轮调用。

因此，后端第一版不需要强制提供类似 VSS 的“自然语言 QueryDecomposer”作为必经步骤。外部 AI 的多轮工具调用本身就是查询分解：它可以先按时间、摄像头、地点创建候选集，再根据 tags、静态描述、动态描述、分析尺度和重叠时间窗逐步 refine。

后端可以在未来提供可选的 `suggest_observation_search_plan(user_query)` 辅助简单客户端，但该 helper 不是核心链路；核心 API 应直接接受结构化检索参数。

默认检索流程：

```text
用户问题
  ↓
外部 AI 根据工具 schema 和上下文规划查询
  ↓
结构化过滤，创建初始候选集
  ↓
tag 粗召回 / facet 统计 / 静态或动态文本检索
  ↓
根据问题类型选择 static_attribute / dynamic_event / hybrid
  ↓
根据问题类型选择或偏好 analysis_scale
  ↓
必要时使用 RRF / 加权融合 / rerank 合并结果
  ↓
获取候选详情；按需附带 locator projection
  ↓
回答用户或说明未找到
```

### 10.1 结构化过滤

处理确定条件：

- 时间范围；
- 摄像头 ID；
- 摄像头位置；
- 视频源；
- record_id / video_id；
- analysis_scale，可选。

这些过滤依赖关系数据库，不依赖模型语义。结构化过滤应先于向量检索和全文检索，以减少候选规模并避免跨摄像头、跨时间段误召回。

### 10.2 tags 的定位：外观属性粗召回与 facet

Tags 主要用于外观属性、常见事件和场景元素的粗召回与候选集统计，例如：

```text
black_clothing / backpack / hat / red_jacket / uniform / helmet / loitering / doorway
```

可选方式：

- tag 文本 grep；
- FTS/BM25；
- tag_text 向量搜索；
- tag 与用户查询关键词混合匹配；
- 当前候选集的 tag 分布统计。

注意：tag 是模型生成的辅助线索，不是事实字段。除非用户明确指定强条件，否则不要一开始就用多个 tag 做硬 AND 过滤。

外观属性第一版不应只靠 tags。推荐策略是：

```text
tags 粗筛
  +
static_description_text / static_description_vector 语义搜索
  +
attributes JSON 中可选字段作为二次校验材料
```

这样可以兼容“黑衣 / 深色外套 / 黑色夹克”等同义表达，并避免 tag 词汇不统一导致漏召回。

### 10.3 静态描述搜索（static_attribute）

适合问题：

- 穿什么衣服；
- 带什么东西；
- 有什么明显物体；
- 背景环境如何；
- 某人或某物的外观属性。

主要检索：

```text
static_description_text
static_description_vector
```

### 10.4 动态描述搜索（dynamic_event）

适合问题：

- 人做了什么；
- 有没有摔倒；
- 有没有尾随；
- 有没有徘徊；
- 是否发生冲突；
- 是否有明显异常变化。

主要检索：

```text
dynamic_description_text
dynamic_description_vector
```

### 10.5 analysis_scale 检索偏好

检索接口应允许外部 AI 指定或偏好某种分析尺度，例如：

```json
{
  "preferred_analysis_scales": ["high_freq_event", "default_segment"],
  "scale_strategy": "prefer_high_freq"
}
```

或强制过滤：

```json
{
  "analysis_scale_filter": ["default_segment"]
}
```

推荐策略：

- 用户问“有没有摔倒、打架、奔跑、闯入、烟火”等瞬时异常：优先查 `high_freq_event`，并展开相邻 `default_segment` 解释上下文。
- 用户问普通经过、外观、携带物、基本动作、某个时间点发生了什么：优先查 `default_segment`。
- 用户问复杂事件：混合召回 `default_segment` 与 `high_freq_event`，再由 reranker 或 AI 汇总。
- 用户问长期趋势、拥挤度变化、排队趋势等当前 MVP 不重点覆盖的问题：未来可引入 `low_freq_summary`。

### 10.6 混合搜索（hybrid）

适合问题：

```text
穿黑衣服背包的人有没有靠近机房？
戴帽子的人是否在门口徘徊？
```

可以同时检索：

```text
static_description_text / static_description_vector
dynamic_description_text / dynamic_description_vector
tag_text / tag filters
```

第一版推荐使用 RRF（Reciprocal Rank Fusion）合并不同通道结果，因为 static、dynamic、tag/FTS 的分数分布不一定可直接比较。可选融合项：

```text
RRF(static_rank)
RRF(dynamic_rank)
tag_boost
analysis_scale_boost
```

加权线性融合和 rerank 可以作为后续增强，但第一版不应依赖复杂 reranker 才能工作。

---

## 11. AI 可调用的检索工具

第一版建议提供以下核心工具。

### 11.1 start_observation_search

创建 SearchContext 和初始 revision。

能力：

- 接收外部 AI 已规划好的结构化过滤条件，而不是强制接收一整句自然语言后由后端自行分解；
- 支持 `query_text`、`time_range`、`camera_ids`、`location_ids`、`video_ids`、`tag_filters`、`preferred_text_fields`、`analysis_scale_filter` / `preferred_analysis_scales` / `scale_strategy`、`top_k`、`score_threshold`；
- 可选 tag/text 初筛；
- 支持 `search_mode`：`static_attribute` / `dynamic_event` / `hybrid` / `auto_by_external_ai`；
- 返回初始候选数量、top tags、时间分布、摄像头分布。

示例参数：

```json
{
  "query_text": "穿深色衣服并背包的人是否在门口徘徊",
  "time_range": {"start": "2026-06-06T21:00:00+08:00", "end": "2026-06-06T22:00:00+08:00"},
  "camera_ids": ["cam_lobby_01"],
  "tag_filters": ["dark_clothing", "backpack"],
  "preferred_text_fields": ["static", "dynamic"],
  "preferred_analysis_scales": ["high_freq_event", "default_segment"],
  "search_mode": "hybrid",
  "top_k": 50
}
```

会生成新缓存：是。

### 11.2 refine_observation_search

基于某个 `base_revision_id` 执行增量检索。

可支持 op：

```text
narrow_by_tags
search_static_text
search_dynamic_text
hybrid_search_text
filter_by_analysis_scale
rerank_current_candidates
apply_rrf_fusion
```

说明：

- `narrow_by_tags` 主要用于粗筛或候选集缩小；除非外部 AI 明确要求强条件，否则不应默认多个 tag 硬 AND。
- `search_static_text` 主要服务外观属性 / 场景 / 物体查询。
- `search_dynamic_text` 主要服务动作 / 事件 / 异常变化查询。
- `hybrid_search_text` 主要服务“外观 + 动作”组合查询，第一版推荐用 RRF 合并 static、dynamic、tag/FTS 和 analysis_scale boost。

会生成新缓存：是。

### 11.3 batch_refine_observation_search

基于同一个 `base_revision_id` 并行执行多组策略。

示例：

```text
rev2 = narrow_by_tags(黑衣, 背包)
rev3 = search_static_text(穿深色外套并背包的人)
rev4 = search_dynamic_text(走向电梯)
rev5 = filter_by_analysis_scale(high_freq_event)
```

会生成新缓存：是，生成多个 revision。

### 11.4 facet_observation_search

查看当前候选集统计。

返回：

- 候选数量；
- 高频 tags；
- 摄像头分布；
- 时间分布；
- 地点分布；
- analysis_scale 分布。

会生成新缓存：否。

### 11.5 get_observation_details

根据 record_id 获取完整记录。

返回：

- static_description_text；
- dynamic_description_text；
- tags；
- attributes；
- analysis_scale；
- video_id；
- camera_id；
- 摄像头位置；
- 片段时间。

可选参数：

```text
include_locator = true / false
```

当 `include_locator=true` 时，同时返回由 ObservationRecord + VideoSource 派生出的 locator projection，包括：

- playback_url（短 TTL，或经服务端代理生成，不暴露内部 source_uri）；
- segment_start_ms；
- segment_end_ms；
- absolute_start_time；
- absolute_end_time；
- thumbnail_uri；
- clip_uri（如部署侧支持，且必须经二次鉴权）。

会生成新缓存：否。

### 11.6 get_video_locator（可选批量工具）

`VideoLocator` 不应作为独立业务实体或独立存储表。它只是 ObservationRecord 与 VideoSource join 后派生出的播放定位视图，用于把文字证据映射回原视频时间段。

第一版可优先通过 `get_observation_details(record_ids, include_locator=true)` 返回 locator。若后期需要为大量候选批量生成播放链接、缩略图或签名 URL，可保留 `get_video_locator(record_ids)` 作为可选批量工具。

返回：

- playback_url（短 TTL，或经服务端代理生成，不暴露内部 source_uri）；
- segment_start_ms；
- segment_end_ms；
- absolute_start_time；
- absolute_end_time；
- thumbnail_uri；
- clip_uri（如部署侧支持，且必须经二次鉴权）。

会生成新缓存：否。

### 11.6_bis get_overlapping_records

获取和指定记录存在时间交叉的其他分析记录。例如传入一个 `high_freq_event` 的 `record_id`，用于查找包围该时段的 `default_segment` 记录。

返回：
- 符合 `start < target_end AND end > target_start` 的 active 记录列表。

会生成新缓存：否。

### 11.7 close_search_context

显式关闭检索上下文，释放缓存资源。

会生成新缓存：否。

---

## 12. SearchContext 缓存设计

为了支持 AI 多轮检索，系统需要短生命周期、有权限隔离的检索上下文缓存。

推荐设计：

```text
SearchContext = 一次多轮检索会话
Revision = 某一轮不可变候选集快照
Candidate = 某个 revision 下的候选 record_id、rank、score
```

### 12.1 search_contexts 表

```text
context_id            主键，不可猜测随机 ID
tenant_id             租户 ID
principal_id          当前 principal ID
session_id            AI 会话 ID，可选但推荐
dataset_revision      数据快照版本
mode                  snapshot / stream
default_revision_id   当前默认 revision，可选
created_at
last_accessed_at
expires_at
status                active / expired / closed / failed
```

说明：

- `context_id` 不是权限凭证，每次使用必须校验 tenant/principal/session。
- `dataset_revision` 保证多轮检索基于同一数据快照。
- `snapshot` 模式下，新入库视频记录不会出现在已有 context 中，保证结果集数量和分页不变。第一版 MVP 默认只实现该模式。
- `stream` 模式是为未来常驻 Agent 盯防分析场景预留的扩展模式。它允许通过游标（如 `cursor_ms` 或 `latest_scanned_record_id`）拉取会话开启之后不断流入系统的新 ObservationRecord，但不进入第一版默认链路。

### 12.2 search_revisions 表

```text
revision_id           主键
context_id            外键，关联 SearchContext
parent_revision_id    父 revision，可为空
op                    产生该 revision 的操作
op_params_json        操作参数摘要
candidate_count       候选数量
facets_json           候选统计，可选
created_at
```

说明：

- revision 不可变。
- 多个 revision 可以有同一个 parent，用于并行搜索。
- 第一版使用单父 revision；复杂多父 DAG 可暂不实现。

### 12.3 search_candidates 表

```text
revision_id           外键，关联 SearchRevision
record_id             外键，关联 active ObservationRecord
rank                  当前 revision 内排序
score                 综合分数
score_detail_json     各通道得分，例如 tag/vector/FTS/rerank
created_at
```

说明：

- 每个 revision 最多保存有限数量候选，例如 1000 条。
- 若候选过大，应返回 `too_broad` 和 facets，让 AI 增加过滤条件，而不是缓存几十万条候选。

### 12.4 缓存边界

第一版建议：

```text
TTL: 15 分钟
idle timeout: 5 分钟
单用户最多 3 个 active context
单 context 最多 8 个 revision
单 revision 最多保存 1000 条 candidate
mode: snapshot only
```

过期清理：

- 定时任务清理 expired/closed context；
- 删除对应 revisions 与 candidates；
- 也可按 tenant/principal 做配额清理。

---

## 13. 权限与客户端-服务端安全边界

权限不是第一版的完整后台功能，但硬安全边界必须从第一版开始存在。目标是：即使外部 AI 用错接口，也不能篡改记录；即使用户查询涉密区域，未授权内容也不会进入候选集、统计或播放定位结果。

### 13.1 客户端-服务端架构

推荐架构：

```text
外部 AI
  ↓
客户端 SDK / Tool Proxy
  ↓ 自动携带已验证身份凭证，不把权限信息塞进 query body
CCTV-Memory Server
  ↓ AuthN / AuthZ / 资源范围过滤
数据库 + 向量/全文索引
```

客户端可以负责登录、注册、刷新 token，并在 AI 调用工具时自动附加身份凭证。AI 不需要意识到用户身份或权限策略的存在，只需要正常调用检索工具。

服务端不能信任客户端在请求正文里声明的用户身份、角色或可访问区域。服务端只信任经过验证的 token / session / mTLS / 签名凭证，并根据服务端保存的 principal、role、group、access_policy 决定权限。

### 13.2 用户注册与身份核验

客户端可以提供注册/登录接口，但注册必须和服务端交互完成核验。

最小流程：

```text
客户端发起注册 / 登录
  ↓
服务端核验身份或由管理员预置用户
  ↓
服务端创建 principal，并绑定 role / group / access_policy
  ↓
客户端保存 token / session
  ↓
后续 AI 调用由客户端自动携带凭证
```

第一版可以先支持管理员预置用户或 service account；完整 SSO、LDAP、组织架构同步和审批流不进入第一版。

### 13.3 Principal 与 capability

服务端认证后得到 principal：

```text
principal_id
principal_type = user / service_account / admin
tenant_id       可选；单租户 MVP 可固定
roles
groups
status
```

接口按 capability 授权。建议最小能力集合：

```text
observation.search
observation.read_detail
observation.read_locator
video.playback
analysis.submit
analysis.rerun
analysis.publish
camera.manage
policy.manage
user.manage
audit.read
runtime.manage
```

AI-facing 客户端默认只应获得只读检索能力：

```text
observation.search
observation.read_detail
observation.read_locator
video.playback
```

分析写入、策略管理、摄像头管理、用户管理等能力不暴露给 AI-facing 检索上下文。

### 13.4 非法接口与涉密结果的不同处理

权限错误分两类处理：

1. **接口能力非法**：例如当前 principal 调用了 `analysis.publish`、`policy.manage` 或任何写入/删除接口。服务端应返回明确错误，例如 `403 capability_denied`。
2. **接口合法但资源无权访问**：例如搜索请求命中了机密区域记录。无权限资源必须表现为不存在，不进入结果、不进入 facet、不进入 candidate_count，也不返回“有结果但你无权查看”之类信息。

推荐对合法查询的空结果表述：

```text
没有找到你可访问范围内的匹配记录。
```

这样不承认也不否认无权区域是否存在相关内容。

### 13.5 检索前权限过滤

权限过滤必须发生在 SQL / FTS / vector / facet 之前，而不是全库检索后删除无权结果。

正确流程：

```text
验证 principal
  ↓
根据 role / group / access_policy 计算 authorized_scope
  ↓
把 authorized_scope 编入关系查询、全文检索和向量检索 metadata filter
  ↓
只在授权范围内召回、排序、统计、分页
```

错误流程：

```text
全库向量 topK
  ↓
再删除无权记录
```

后者可能泄露候选数量、tag 分布、相似度分布或机密区域存在性。

### 13.6 资源权限继承与冗余

最小权限模型可以复用已有资源层级：

```text
CameraLocation.access_policy_id / security_level
  ↓
CameraDevice.access_policy_id（可覆盖或继承）
  ↓
VideoSource.access_policy_id（可快照/继承）
  ↓
ObservationRecord.access_policy_id / security_level（冗余快照，用于快速过滤）
```

ObservationRecord 的权限字段由系统在写入时根据 VideoSource / CameraDevice / CameraLocation 派生，不由 VLM 输出决定。这样向量索引和 FTS 索引也可以携带 `access_policy_id` / `security_level` / `camera_id` / `location_id` 等 metadata，保证检索前过滤可落地。

### 13.7 SearchContext 权限绑定

SearchContext 创建时必须绑定当前 principal 和授权范围摘要：

```text
context_id
principal_id
tenant_id
session_id
authorized_scope_hash
dataset_revision
mode
```

`context_id` 不是权限凭证。每次调用 `refine_observation_search`、`get_observation_details`、`get_video_locator` 都必须重新校验当前请求凭证，并确认不能扩大到当前 principal 的授权范围之外。

### 13.8 locator 二次鉴权

`include_locator=true` 和 `get_video_locator(record_ids)` 必须二次鉴权。

流程：

```text
record_id
  ↓
查 ObservationRecord
  ↓
查/校验 VideoSource + CameraDevice + CameraLocation 权限
  ↓
确认当前 principal 可访问
  ↓
生成短 TTL playback_url / clip_uri / thumbnail_uri
```

不要直接向外暴露内部 `source_uri` 物理路径。若部署侧支持播放，应返回短期签名 URL 或经服务端代理的播放 URL，并记录审计日志。

### 13.9 只读检索与写入隔离

AI-facing search service 必须只能读取 active ObservationRecord、VideoSource、CameraDevice、CameraLocation、SearchContext 相关缓存和必要索引，不能写入或删除业务记录。

不同数据库的实现方式不同：

- SQLite MVP：使用服务端内部只读连接 / 只读 repository / 可选 SQLite authorizer hook / OS 文件权限来模拟只读边界。SQLite 本身不提供用户级 GRANT/RLS，因此不能让客户端或 AI 直接访问数据库文件。
- PostgreSQL 正式版：使用 search_service 只读数据库账号、worker 写入账号、admin 管理账号，并可进一步启用 GRANT/REVOKE 与 Row Level Security。

写入只允许走 AnalysisJob / worker 路径：

```text
AnalysisJob / VLM worker
  ↓
临时结果区或事务上下文
  ↓
原子发布 ObservationRecord active + ObservationRecordHistory
```

分析 worker、管理后台和 search service 应使用不同 repository / adapter capability，或在支持数据库权限的后端上使用不同数据库账号。即使 AI 用错接口，也不应拥有数据库写权限。

### 13.10 审计日志预留

第一版至少预留审计日志能力，记录：

```text
principal_id
session_id
tool_name
request_id
query_params_hash
resource_scope_hash
candidate_count
record_ids（详情/locator 时）
include_locator
issued_playback_url_id（如有）
timestamp
```

审计日志用于追踪查询、详情查看、播放链接签发、AnalysisJob 发布等敏感操作。完整审计后台可以后做，但日志写入点应预留。

---

## 14. 外部 API 建议

外部 API 至少需要覆盖任务提交、任务查询、错误查询、记录查询和审计查询。

建议接口：

```http
POST /api/v1/video-sources/analyze
GET  /api/v1/analysis-jobs/{analysis_job_id}
GET  /api/v1/analysis-jobs?status=running
GET  /api/v1/analysis-jobs/{analysis_job_id}/errors
GET  /api/v1/video-sources/{video_id}/records
GET  /api/v1/observation-records/{record_id}
GET  /api/v1/observation-records/{record_id}/history
POST /api/v1/analysis-jobs/{analysis_job_id}/rerun
POST /api/v1/high-freq-triggers/{trigger_id}/rerun
```

未来人工改写可选：

```http
PATCH /api/v1/observation-records/{record_id}
```

API 查询 AnalysisJob 时，建议返回尺度子任务状态：

```json
{
  "analysis_job_id": "job_001",
  "video_id": "video_001",
  "job_status": "partial_failed",
  "scale_tasks": [
    {
      "analysis_scale": "default_segment",
      "status": "succeeded",
      "total_segments": 120,
      "succeeded_segments": 120,
      "failed_segments": 0
    },
    {
      "analysis_scale": "motion_scan",
      "status": "succeeded",
      "trigger_count": 8
    },
    {
      "analysis_scale": "high_freq_event",
      "status": "partial_failed",
      "trigger_count": 8,
      "succeeded_triggers": 7,
      "failed_triggers": 1
    },
    {
      "analysis_scale": "low_freq_summary",
      "status": "skipped",
      "reason": "not_enabled"
    }
  ]
}
```

---

## 15. ReID 预留

第一版不实现 ReID，但需要为未来留余地。

目前只需要保证每条 ObservationRecord 有：

```text
record_id
video_id
camera_id 可通过 video_id 间接获取
segment_start_ms
segment_end_ms
analysis_scale
```

未来可以新增：

```text
reid_identity
reid_observation_link
```

用于记录：

- 某个 ReID 人员在哪些 ObservationRecord 中出现；
- 在不同摄像头、不同时间片段中的轨迹；
- 展示同一 ReID 的跨场景移动路径。

ReID 属于未来独立能力，不应污染第一版基础数据模型。

---

## 16. 第一版 MVP 范围

MVP 建议范围：

1. 支持视频文件 / 已落盘 RTSP chunk 接入。
2. 外部通过 CLI / HTTP 提交分析任务。
3. 外部强制传入 `camera_id` 和 `video_start_time`。
4. 本程序只读取 `VIDEO_ROOT` 内的视频文件。
5. 创建 VideoSource 和 AnalysisJob。
6. 默认尺度 `default_segment` 持续分析。
7. 运动/突变扫描 `motion_scan` 决定是否触发高频尺度。
8. 高频尺度 `high_freq_event` 只在运动/突变触发时运行。
9. VLM 输出统一 JSON：static/dynamic/tags/quality/uncertainties。
10. 写入当前 active ObservationRecord。
11. 重跑通过新的 AnalysisJob 覆盖 active 记录，旧记录进入 ObservationRecordHistory。
12. 写入静态 / 动态两个向量索引，保留 FTS/BM25。
13. 支持 AI 工具化多轮检索和 snapshot SearchContext 缓存。
14. 支持客户端-服务端身份凭证、服务端鉴权/授权、检索只读边界、检索前权限过滤和 locator 二次鉴权。
15. 支持根据 record_id 回放定位原视频时间段。

---

## 17. 不进入第一版的内容

为了避免不必要负担，第一版暂不实现：

- 本程序直接管理 RTSP 流；
- RTSP 断流重连、长期 recorder 状态机；
- stream_context / live mode 检索上下文默认链路；
- 复杂多父 DAG；
- ReID；
- 正式实时告警系统；
- 人工审核工作流；
- 强结构化视觉语义字段；
- 完整前端 UI；
- 完整权限管理 UI、复杂组织架构同步、审批流；
- 低频场景摘要默认链路；
- 中途取消已经发出的 VLM 请求。

---

## 18. 当前结论

第一版最小闭环：

```text
外部视频文件 / 已落盘 RTSP chunk
  ↓
VideoSource + AnalysisJob
  ↓
default_segment + motion_scan + optional high_freq_event
  ↓
VLM 输出 static_description_text / dynamic_description_text / tags
  ↓
ObservationRecord active + ObservationRecordHistory 审计
  ↓
static/dynamic/tag 索引
  ↓
AI 工具化检索（客户端自动携带身份凭证，服务端先做权限过滤）
  ↓
SearchContext + Revision + Candidate 缓存
  ↓
返回授权范围内的证据化结果和 locator projection
```

该设计功能上足以支撑企业监控视频的文字记忆和自然语言检索。复杂度主要来自异步 AnalysisJob、不同分析尺度子任务、HighFreqTrigger 和多轮检索缓存；这些复杂度分别服务于高并发 VLM 分析、异常高频信号捕获、审计可追溯和 AI 策略式检索，是当前需求下合理的最小复杂度。
