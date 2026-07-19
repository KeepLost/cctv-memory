# Configuration Contract（配置契约）

## 0. 文档目的

本文定义 CCTV Memory 的配置文件、环境变量、默认值、安全边界和 feature flags。配置不得成为绕过架构契约的后门。

---

## 1. Config Sources

Priority order:

```text
CLI args
ENV vars
config file
built-in defaults
```

MVP config file:

```text
config.yaml
```

Secrets should come from environment variables or secret manager, not committed config files.

---

## 2. Core Config Shape

```yaml
app:
  env: local
  timezone: Asia/Shanghai
  data_dir: ./data
  log_level: INFO

server:
  host: 127.0.0.1
  port: 8080
  public_base_url: null

database:
  backend: sqlite
  sqlite_path: ./data/cctv_memory.sqlite3
  postgres_dsn_env: CCTV_MEMORY_POSTGRES_DSN  # env var NAME only; value is never stored in YAML
  pool_size: 5
  max_overflow: 10
  echo_sql: false

storage:
  video_root: ./data/videos
  frame_root: ./data/frames
  artifact_root: ./data/artifacts

worker:
  enabled: true
  embedded: true
  worker_id: local-worker-1
  lease_seconds: 300
  max_retries: 3
  # --- 多 job 并发 (任务 cctv-memory-20260615-1620) ---
  max_concurrent_jobs: 1        # 本进程同时处理的 job 数(有界线程池)。1=旧的严格串行(零行为变化)
  max_unit_workers_per_job: 1   # 单 job 内 unit 线程池上限; 与全局 provider 上限解耦
  orphan_recovery_enabled: true
  orphan_stale_seconds: 900
  orphan_batch_limit: 100

search:
  context_ttl_seconds: 900
  context_idle_seconds: 300
  max_top_k: 100
  max_candidates_per_revision: 1000
  max_revisions_per_context: 8
  rrf_k: 60
  static_weight: 1.0
  dynamic_weight: 1.0
  fts_weight: 0.5
  max_tag_boost: 0.2
  max_analysis_scale_boost: 0.2

pipeline:
  default_segment:
    window_seconds: 12
    overlap_seconds: 3
    frame_strategy: uniform
    frames_per_segment: 6
  motion_scan:
    method: frame_diff        # detector 实现名，由 motion-detector 工厂按名选择
    threshold: 0.15           # 触发阈值 0..1（更敏感默认）
    min_duration_ms: 600      # 触发窗口最短时长
    merge_gap_ms: 800         # 相邻运动段合并间隔
    sample_fps: 4.0           # 运动采样帧率
    frame_width: 128          # 下采样宽
    frame_height: 72          # 下采样高
  high_freq_event:
    window_seconds: 3
    overlap_ratio: 0.5
    frame_strategy: uniform
    frames_per_segment: 8
  # --- 跨 scale unit 调度 (任务 cctv-memory-20260611-1905, Stage C2) ---
  cross_scale:
    enabled: true              # 关闭则回退旧的顺序 scale 循环
    high_freq_quota: 3         # 防饥饿配额: 每派发 ≤3 个 high_freq unit 强制 1 个 default unit
  # --- 帧解码后端 + 流式选帧 (任务 cctv-memory-20260611-1805) ---
  decode_backend: opencv            # opencv(默认) | ffmpeg
  decode_fallback_to_ffmpeg: true   # opencv 解码失败时回退到 ffmpeg 逐帧 seek
  frame_stream:
    sample_fps: 8.0                 # 流式解码采样 fps
    buffer_seconds: 4.0             # 环形缓冲保留秒数
    max_buffer_bytes: 268435456     # 256MiB 二次保险丝(并发时按 /并发数 分配)
    scoring_scale: "320x180"        # 评分用降采样灰度尺寸
    selection_strategy: bins_then_score   # uniform | score | bins_then_score
    selected_jpeg_quality: 80
    w_motion: 1.0
    w_scene: 0.5
    w_quality: 0.5
    min_blur: 50.0                  # 拉普拉斯方差质量门槛
    cleanup_selected_on_success: true     # 非 debug 时 unit 成功后清理选中帧
    decode_timeout_seconds: 120

features:
  sqlite_vec: false
  low_freq_summary: false
  user_registration: false
  admin_policy_api: false

vlm:
  provider: mock            # mock | real
  model: null
  model_id: gemini-3.1-pro-preview
  api_key_env: LLM_KEY      # 环境变量“名字”，真实 key 放该环境变量；实现默认 LLM_KEY
  base_url_env: CCTV_MEMORY_VLM_BASE_URL
  default_base_url: http://nginx:8081/api/ohmygpt/chat/completions
  media_input: frames       # frames（默认，抽帧多图） | video（整段视频，显式开启）
  include_audio: false      # 默认不带音频；仅 media_input=video 且显式 true 时携带
  timeout_seconds: 120
  max_retries: 2            # legacy adapter knob; schema 重生成不得在 adapter 内隐藏循环
  # --- 新增 (任务 cctv-memory-20260611-1410) ---
  extra_body: {}            # provider 自定义请求体参数（非敏感，JSON-serializable）
                            # 禁止覆盖 model/messages/Authorization/stream/tools 等核心字段
  max_concurrent_requests: 1  # 进程级全局 provider 调用上限(所有并发 job/unit/retry 共享, 单一真值源)
  min_request_interval_ms: 0  # 请求启动最小间隔（ms）；0=不限速
  # --- 新增 (任务 cctv-memory-20260615-1447: 单元级瞬时重试 + 终态写入加固) ---
  unit_max_attempts: 3          # per-unit 瞬时 provider 错误尝试次数；每次经 VlmScheduler
  retry_backoff_base_ms: 500    # 指数退避基数；base*2^(n-1)，0=不等待
  retry_backoff_cap_ms: 8000    # 退避上限
  retry_jitter: 0.2             # 退避抖动幅度 [0,1]，实际延迟 = backoff*(1±jitter)
  schema_repair_enabled: true   # VLM JSON/fence/禁止字段机械修复
  schema_regenerate_max_attempts: 1  # schema 失败后的严格重生成次数；每次经 VlmScheduler
  schema_regenerate_instruction: strict_json_retry_instruction
  schema_retry_backoff_ms: 0
  terminal_write_max_attempts: 3  # 终态 DB 写入(mark_failed/skipped/成功)瞬时锁重试次数；1=不重试
  terminal_write_backoff_ms: 100  # 终态写入重试线性退避基数
  media_log_mode: metadata_only   # metadata_only | debug_full_media
  debug_media_retention: false    # true 时才写 artifact_root 全量媒体 artifact
```

> 说明（任务 cctv-memory-20260615-1447 单元级重试）：`vlm.unit_max_attempts` 等控制 **worker 层
> per-unit 重试**——当 VLM 调用因**瞬时 provider 错误**（`VlmProviderError`：超时/传输/5xx/429/冷启动）
> 失败时，per-unit runner 在同一 `running` unit 内重试整个 VLM 调用（指数退避+抖动）；当 schema
> 校验失败时，先机械修复，再按 `schema_regenerate_max_attempts` 进行严格重生成。**每次尝试仍经
> 全局 VlmScheduler**。默认 `unit_max_attempts=3`
> 用于吸收冷启动首调失败；设为 1 即恢复旧的不重试行为。`terminal_write_max_attempts` 让终态 DB 写入
> 在遇到瞬时 SQLite 锁/busy 时短暂重试，避免终态写入静默失败导致 tally 与 DB 状态分歧；耗尽即抛错
> （不假装成功），由有界孤儿回收兜底。不引入 `recoverable_running`。重试期间发布保持精确一次。

> 说明（authority reconcile, 任务 cctv-memory-20260610-frame-default-vlm-path）：新增
> `vlm.media_input`（默认 `frames`）与 `vlm.include_audio`（默认 `false`）。默认 VLM 输入
> 形态为“抽帧后多图发送、且不带音频”（vlm-analysis-contract §1 允许 frame_uris 或 clip_uri）。
> `media_input=video` 为非默认、需显式启用的整段视频路径；`include_audio` 仅在 `video` 模式下
> 生效（`frames` 天然无音频）。worker 选择见 `workers/analysis_worker.py:_default_video_processor`：
> real + frames -> `SegmentFrameVideoProcessor`（多图）；real + video -> `WholeClipVideoProcessor`
> （默认 ffmpeg 剥音轨）。

> 说明（authority reconcile, 任务 cctv-memory-20260610-real-vlm-http-fix）：实现中
> `VlmSection.api_key_env` 默认值为 `LLM_KEY`（共享网关 key 的环境变量名），并新增
> `model_id` / `base_url_env` / `default_base_url` / `max_retries`。本契约示例此前写
> `CCTV_MEMORY_VLM_API_KEY`，与实现不一致，会误导使用者。已对齐到实现真实默认值。
> 真实端点 e2e（`tests/integration/test_real_vlm.py::test_real_vlm_end_to_end`）以 `LLM_KEY`
> + 默认 base_url 实测通过。`config.yaml` 仅存放变量“名字”/非敏感项，密钥只走环境变量。

> 说明（pluggable motion detection, 任务 cctv-memory-20260611-1049）：`pipeline.motion_scan`
> 现在是“可插拔运动检测”的配置面。`method` 选择 detector 实现，由
> `infrastructure/video/motion_detector_factory.py:build_motion_detector` 按名从注册表解析；
> `frame_diff` 是首个注册实现，其余字段（threshold/min_duration_ms/merge_gap_ms/sample_fps/
> frame_width/frame_height）是该实现的可调参数。切换 detector 或调参只改本配置，不改
> worker/application/domain（宪法 §3/§9）。未知 `method` 在组合点抛 `ValueError`（列出受支持
> 方法），不静默回退（§8：无效配置应清晰失败）。未来新 detector（如 `opencv_mog2`/`ssim`/ML）
> 通过 `register_motion_detector` 注册自己的 builder + 各自的 per-method 参数即可接入，
> `MotionScanProcessor` 与 worker 仍只见 `MotionDetectorPort` 抽象。默认值（threshold=0.15、
> sample_fps=4.0、128x72、min_duration_ms=600、merge_gap_ms=800）比旧默认（0.4/2.0/64x36/1500/
> 1000）更敏感，便于 CCTV 事件触发，且 128x72 灰度帧在 4fps 下仍极低开销。理由见
> `status/execution-report.md` for the current active task.

> 说明（OpenCV FrameStream 默认解码后端, 任务 cctv-memory-20260611-1805）：新增
> `pipeline.decode_backend`（默认 `opencv`）、`pipeline.decode_fallback_to_ffmpeg`（默认 true）
> 与 `pipeline.frame_stream`。`opencv` 后端用 `infrastructure/video/opencv_frame_stream.py:
> OpenCvFrameStreamVideoProcessor` 顺序流式解码一段、只保留有界环形缓冲的近期裸帧
> （`buffer_seconds × sample_fps` 帧，再受 `max_buffer_bytes` 二次封顶），在线计算每帧标量
> 度量（motion/scene/blur/brightness），由纯 domain 函数 `domain/policies.py:select_frames`
> 按 `selection_strategy` 选帧（保证时间覆盖 + 时序升序），仅把选中帧 JPEG（`selected_jpeg_
> quality`）原子落盘供 VLM 读取。裸帧 `np.ndarray` 只活在该 adapter 内，永不跨模块（宪法 §3/§4）。
> `ffmpeg` 后端保留旧逐帧 seek 行为（`SegmentFrameVideoProcessor`）。`opencv` 解码失败且
> `decode_fallback_to_ffmpeg=true` 时回退到 ffmpeg，行为确定且有测试覆盖。
> `cleanup_selected_on_success=true`（默认且仅在 `media_log_mode=metadata_only` 下生效）使
> unit 成功后删除该 unit 的选中帧工作文件；`debug_full_media` 模式保留 artifact 不清理。
> worker 选择见 `workers/analysis_worker.py:_default_video_processor`/`_frames_processor`
> （real+frames 默认 -> opencv）。切换后端/选帧策略改变发给 VLM 的实际帧，故对应不同
> `pipeline_version`（见 pipeline-experiment-contract §3.2 与下方组合根说明）。`cv` 依赖
> （`opencv-python-headless`+`numpy`）为 `pyproject` 可选 extra；当 `decode_backend=opencv`、
> cv2 不可导入且 fallback 关闭时，doctor 报 NOT READY（§8）。

> 说明（pipeline_version 来源, 任务 cctv-memory-20260611-1805）：`pipeline_version` 由组合根
> `infrastructure/runtime.py:_pipeline_version_for(config)` 按 `decode_backend` 派生
> （opencv -> `pipeline-v2-opencv-selector`，ffmpeg -> `pipeline-v1`），写入 AnalysisJob。
> worker 现统一从 job 行读取该值用于 ModelCallLog（修复了此前 worker 硬编码 `"pipeline-v1"`
> 与 job/ObservationRecord 可能漂移的缺陷）。

> 说明（全局 VLM 调度 + 跨 scale unit 调度, 任务 cctv-memory-20260611-1905, Stage C1+C2）：
> `vlm.max_concurrent_requests` 与 `vlm.min_request_interval_ms` 现在是 **provider 级全局
> 限制**：`AnalysisWorker` 构建单一 `VlmScheduler` 并注入 default_segment 与 high_freq_event
> 两个 processor，使并发上限/最小请求间隔跨所有 scale/unit 全局生效（此前是 per-scale，可能
> 超 provider 总额）。新增 `pipeline.cross_scale`：`enabled`（默认 true，关闭回退旧顺序 scale
> 循环）、`high_freq_quota`（默认 3，确定性防饥饿配额——每派发 ≤quota 个 high_freq unit 强制
> 1 个 default unit，high_freq 优先且 default 不饿死）。motion_scan→high_freq 硬依赖与 per-unit
> 幂等/发布不变；scale 完成判定 = 该 scale 全部 unit 终态（job-state-machine-contract §3.5）。
>
> 多 job 并发下（任务 cctv-memory-20260615-1620）该 `VlmScheduler` 仍是**进程内唯一**实例，
> 由所有并发 job worker 共享，因此即便多个 job 同时处理、每 job 又有自己的 unit 线程池，
> 全进程在途 VLM 调用数仍恒 ≤ `vlm.max_concurrent_requests`，绝不会因并发 job 数叠加而突破。

---

## 3. Path Safety

Path config must obey:

```text
source video paths must be under allowed roots or explicitly imported
client never receives internal source_uri
backup/export must not include arbitrary filesystem paths
```

`video_root`, `frame_root`, and `artifact_root` must be canonicalized at startup.

---

## 4. Database Config

MVP:

```yaml
database.backend: sqlite
```

Allowed future values:

```text
sqlite
postgres
```

Application/domain code must not branch on `database.backend`; only infrastructure composition may.

---

## 5. Feature Flags

Feature flags must only enable code paths already protected by tests and contracts.

MVP defaults:

```yaml
features.sqlite_vec: false
features.low_freq_summary: false
features.user_registration: false
features.admin_policy_api: false
```

Rules:

- disabled feature returns `not_implemented` or remains absent from API；
- enabling feature must not weaken permission boundaries；
- feature flag state should appear in admin runtime status, not AI-facing responses。

---

## 6. VLM Provider Config

VLM provider config must not leak API keys.

Required fields:

```text
provider
model
api_key_env
timeout_seconds
max_retries
```

Provider-specific options live under namespaced keys:

```yaml
vlm:
  provider: openai
  openai:
    model: example
```

---

## 7. Observability Config

Timeline observability records local analysis execution events and powers the CLI
HTML/JSON exporter. Timeline events are append-only diagnostics, not business state.

Required defaults:

```text
observability.timeline_enabled = true
observability.timeline_payload_mode = minimal
observability.timeline_retention_days = 30
observability.timeline_export_max_events = 100000
observability.timeline_fail_open = true
observability.sql_trace_enabled = false
```

Rules:

- Timeline metadata must not include API keys, Authorization headers, `source_uri`, raw media/base64, or full internal paths.
- Timeline write failures must fail open by default and log diagnostic errors without failing analysis.
- SQL statement tracing is not part of the default product path.
- Timeline HTML export must be fully offline at runtime. It may embed local Plotly.js from the Python `plotly` package, but must not reference CDN/cloud URLs such as `cdn.plot.ly`.

---

## 8. Search Config

Search config must include deterministic defaults:

```text
context_ttl_seconds
context_idle_seconds
max_top_k
max_candidates_per_revision
max_revisions_per_context
rrf_k
static_weight
dynamic_weight
fts_weight
max_tag_boost
max_analysis_scale_boost
```

Changing search weights should be reflected in search config version or score_detail for reproducibility.

---

## 9. Config Validation

Startup must validate:

```text
timezone valid
data_dir exists or creatable
sqlite_path parent exists or creatable
video_root/frame_root/artifact_root canonicalized
max_top_k within safe bound
worker lease_seconds positive
worker orphan_stale_seconds positive (应 > lease_seconds，只回收真正废弃的 unit)
worker orphan_batch_limit positive (每次 sweep 上限，避免一次处理过多)
worker max_concurrent_jobs positive (1=串行；>1 并发处理多个 job)
worker max_unit_workers_per_job positive (单 job 内 unit 并发；不放大全局 provider 上限)
required secrets present for selected provider
observability timeline_export_max_events positive
observability timeline_retention_days non-negative
```

> 多 job 并发（任务 cctv-memory-20260615-1620）：`worker.max_concurrent_jobs` 控制本 worker 进程
> 同时处理的 job 数（有界 in-process 线程池）。默认 1 = 旧的严格串行 drain（claim→process 逐个），
> 零行为变化。>1 时每个槽位独立原子 claim 并处理一个 job；**原子 claim**（database-adapter-contract §3.5）
> 保证同一 task 不被处理两次，单 job 失败被隔离不阻塞其它槽位。`worker.max_unit_workers_per_job`
> 是单 job 内 unit 线程池规模的唯一配置项，单 job 与多 job 模式都一样生效；它与全局 provider
> 上限**解耦**。`vlm.max_concurrent_requests` 只控制唯一共享 VlmScheduler 的全进程 provider
> 在途调用上限，不再兼任 unit 池大小。因此无论 `max_concurrent_jobs × max_unit_workers_per_job`
> 多大，**全进程在途 VLM 调用数恒 ≤ `vlm.max_concurrent_requests`**。优雅关停：drain 接受
> `should_stop`，置位后不再 claim 新 job，在途 job 跑完；崩溃/kill 窗口仍由有界孤儿回收兜底。

> 孤儿回收（任务 cctv-memory-20260612-1854 §E）：`worker.orphan_recovery_enabled` 开启时，worker
> 启动 / 每次 drain 前做一次有界、索引支撑的孤儿单元回收（详见 job-state-machine-contract §7.1）。
> `orphan_stale_seconds` 须大于 `lease_seconds` 以免回收仍在租约内的活跃 unit；`orphan_batch_limit`
> 限制单次处理行数，保证 sweep 成本有界（O(log N + K)）。

`decode_backend=opencv` 需要 cv2/numpy 可导入；当其不可导入且 `decode_fallback_to_ffmpeg=false`
时，doctor/启动诊断必须清晰报告 NOT READY（不静默回退、不在运行期才崩）。`max_buffer_bytes`
为单个并发 unit 的环形缓冲上限，峰值内存约为 `max_buffer_bytes × max_concurrent_requests`。

Invalid config fails startup with `validation_error` or `service_unavailable`, not partial runtime failure.

---

## 10. Contract Tests

```text
default_config_loads
invalid_timezone_rejected
paths_canonicalized
secret_not_printed_in_runtime_status
feature_disabled_returns_not_implemented
application_layer_does_not_branch_on_database_backend
observability_defaults_load
timeline_export_uses_offline_plotly
```
