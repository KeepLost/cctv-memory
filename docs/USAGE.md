# USAGE — CCTV Memory 真实可用使用指南

本文档只描述**当前代码实测可用**的启动、配置、验证与用法。所有命令、环境变量、路由均与
`cctv_memory/` 源码一一对应，不含臆想路径。最后更新对应任务
`cctv-memory-20260610-usage-polish-and-doctor`。

> 术语：mock = 离线确定性占位实现（CI 默认，无网络）；real = 调用真实外部 VLM/embedding 服务。

---

## 0. 环境前提与安装（推荐 editable install）

- Python 3.12+，使用 `uv` 管理虚拟环境（每个项目自带 `.venv`，不污染全局）。
- 可选 `ffmpeg` / `ffprobe`（仅在使用真实视频时长探测或真实抽帧时需要）。

**安装方式**（“安装方式”决定包怎么进 `.venv`）：

```bash
cd codes/cctv-memory
uv venv                          # 创建项目本地 .venv

# 仅运行项目本体（持续开发首选 editable 安装）：
uv pip install -e .

# 需要跑测试 / lint / 类型检查时，装上 dev 附加依赖：
uv pip install -e ".[dev]"
```

**执行方式**（“执行方式”决定命令怎么跑，二者不要混淆）：

- 已 `uv pip install -e .` 后，`.venv` 里就有 `cctv-memory` 可执行入口
  （`pyproject.toml [project.scripts]`）。两种等价调用：

```bash
uv run cctv-memory <args>        # 让 uv 在项目环境里执行
# 或激活/直接用 .venv：
.venv/bin/cctv-memory <args>
```

> 说明：`uv run` 是“在项目环境里执行命令”，不等于“安装”。editable 安装（`-e .`）让你改完源码
> 立即生效、无需重装；这是日常开发推荐方式。下文示例统一用 `cctv-memory ...`，按上面任选一种执行。

### 0.1 第一步永远先跑 doctor

在 init/analyze/serve 之前，先用 `doctor` 确认“当前这套配置到底会走什么路径、缺什么”：

```bash
cctv-memory doctor                 # 人类可读
cctv-memory doctor --json          # 机器可读
```

详见 §9。

---

## 1. 配置：三层来源与优先级

配置由 `cctv_memory/config/settings.py:AppConfig` 加载，来源优先级（高到低）：

```text
1. 代码/CLI 传入的初始化参数（init kwargs）
2. 环境变量（前缀 CCTV_MEMORY_，嵌套用双下划线 __）
3. YAML 配置文件
4. 内置默认值
```

### 1.1 YAML 配置文件

- 文件位置：环境变量 `CCTV_MEMORY_CONFIG_FILE` 指定的路径；若未设置，则使用当前工作目录下的
  `./config.yaml`（存在时）。两者都没有时只用环境变量 + 默认值。
- **重要**：YAML 文件**不得**写入任何密钥/令牌（configuration-contract §6）。密钥只走环境变量。
- 字段结构与 `docs/contracts/configuration-contract.md §2` 一致。示例：

```yaml
# config.yaml （示例，可只写需要覆盖的字段）
app:
  log_level: INFO
server:
  host: 127.0.0.1
  port: 8080
vlm:
  provider: real          # mock | real
  model_id: gemini-3.1-pro-preview
  media_input: frames     # frames（默认，抽帧多图） | video（整段视频，需显式开启）
  include_audio: false    # 默认不带音频；仅 media_input=video 且显式 true 时才带
pipeline:
  video_metadata_mode: ffprobe   # ffprobe | static | ffmpeg_frames
indexing:
  enabled: false          # 开启向量检索时设 true（需要 reindex）
  provider: mock          # mock | real
```

环境变量等价覆盖（环境变量优先于 YAML）：

```bash
export CCTV_MEMORY_VLM__PROVIDER=real
export CCTV_MEMORY_SERVER__PORT=8090
export CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static
```

### 1.2 数据库后端：SQLite 默认，PostgreSQL + pgvector 可选

默认仍为本地 SQLite：

```yaml
database:
  backend: sqlite
  sqlite_path: ./data/cctv_memory.sqlite3
```

生产试运行可切换到 PostgreSQL + pgvector。DSN 是密钥/连接信息，只能放环境变量；YAML 只保存
环境变量“名字”：

```yaml
database:
  backend: postgres
  postgres_dsn_env: CCTV_MEMORY_POSTGRES_DSN
indexing:
  embedding_dimensions: 1024
```

```bash
export CCTV_MEMORY_DATABASE__BACKEND=postgres
export CCTV_MEMORY_POSTGRES_DSN='postgresql+psycopg://user:password@localhost:5432/cctv_memory'
uv run cctv-memory init --data-dir ./data
```

要求：目标数据库已安装 `vector` 扩展（pgvector）。`init` 会创建 PostgreSQL 表、JSONB 字段、
TIMESTAMPTZ 时间字段、`observation_text_index` 以及 `observation_vectors.embedding vector(N)`。
PostgreSQL 路径不会模拟 SQLite 的 `BEGIN IMMEDIATE`；运行时注入 no-op write coordinator。

本地/CI 默认不需要 PostgreSQL。可选 live 测试：

```bash
export CCTV_MEMORY_TEST_POSTGRES_DSN='postgresql+psycopg://user:password@localhost:5432/cctv_memory_test'
uv run pytest -m postgres
```

未设置 `CCTV_MEMORY_TEST_POSTGRES_DSN` 时 PostgreSQL live 测试会显式跳过。

> 历史问题修复说明：此前 `config.yaml` 从未被代码加载，因此“在 config.yaml 里把 vlm.provider
> 改成 real”会被静默忽略、系统仍走 mock。现已修复，YAML 会被真正读取并遵守上述优先级。

---

## 2. 真实 VLM：所需环境变量与启用方式

真实 VLM 适配器为 `cctv_memory/infrastructure/vlm/real_adapter.py:RealVlmAnalyzer`，
通过 OpenAI 兼容的多模态 `chat/completions` 端点调用。选择逻辑在
`cctv_memory/workers/analysis_worker.py:_default_vlm`：仅当 `vlm.provider == "real"` 才使用真实适配器。

### 2.1 启用真实 VLM（任选其一）

```bash
# 方式 A：环境变量
export CCTV_MEMORY_VLM__PROVIDER=real

# 方式 B：config.yaml 中 vlm.provider: real
```

> ⚠️ **关键：只配 endpoint/key 不会自动切到 real。** 系统是否走真实 VLM **只取决于
> `vlm.provider` 是否等于 `real`**（见 `workers/analysis_worker.py:_default_vlm`）。
> 你即使设置了 `LLM_KEY` 和 `CCTV_MEMORY_VLM_BASE_URL`，只要 `vlm.provider` 仍是默认的
> `mock`，系统就**继续走 mock**。必须显式设 `vlm.provider=real`（环境变量
> `CCTV_MEMORY_VLM__PROVIDER=real` 或 config.yaml）。用 `cctv-memory doctor` 可直接看到
> `effective path` 与 `real VLM analysis: READY/NOT READY`。

### 2.2 真实 VLM 相关变量（变量“名字”在配置里，值在环境里）

| 配置字段 | 默认值 | 含义 |
|---|---|---|
| `vlm.provider` | `mock` | 设为 `real` 启用真实 VLM |
| `vlm.model_id` | `gemini-3.1-pro-preview` | 模型 ID（请求体 `model`） |
| `vlm.media_input` | `frames` | VLM 输入形态：`frames`=抽帧后多图发送（默认，本地友好）；`video`=整段视频单段发送（需显式开启） |
| `vlm.include_audio` | `false` | 是否带音频。默认 `false`：`frames` 天然无音频；`video` 模式会剥离音轨，除非显式设 `true` |
| `vlm.api_key_env` | `LLM_KEY` | **持有 API key 的环境变量名**；真实 key 放在该环境变量里 |
| `vlm.base_url_env` | `CCTV_MEMORY_VLM_BASE_URL` | 持有端点 URL 的环境变量名（可选） |
| `vlm.default_base_url` | `http://nginx:8081/api/ohmygpt/chat/completions` | 未设 base_url_env 时使用 |
| `vlm.timeout_seconds` | `120` | 单次请求超时 |
| `vlm.max_retries` | `2` | schema/瞬时错误重试次数 |

因此最小启用方式（默认网关）：

```bash
export CCTV_MEMORY_VLM__PROVIDER=real
export LLM_KEY=<你的真实key>          # 注意：默认读取的环境变量名是 LLM_KEY
# 如需自定义端点：
# export CCTV_MEMORY_VLM_BASE_URL=https://your-endpoint/v1/chat/completions
```

如果 `provider=real` 但 `LLM_KEY`（或你配置的 `api_key_env`）未设置，worker 会
**快速失败**并报错 `VLM provider=real but env var LLM_KEY is not set`，不会静默回退到 mock。

### 2.3 VLM 媒体输入：默认抽帧多图，整段视频为可选项

真实 VLM 的输入形态由 `vlm.media_input` 决定（仅对 `provider=real` 有意义，mock 不解码任何媒体）：

| `vlm.media_input` | 行为 | 选择的 video processor | 所需二进制 | 音频 |
|---|---|---|---|---|
| `frames`（**默认**） | 每个分段用 ffmpeg 抽 `frames_per_segment` 张 JPEG，**逐帧作为多个 `image_url` 发送**（多图） | `SegmentFrameVideoProcessor` | `ffprobe`+`ffmpeg` | 无（图像天然不含音频） |
| `video`（**需显式开启**） | 整段视频文件 base64 作为**单个** `image_url`（视频 MIME）发送 | `WholeClipVideoProcessor` | `ffprobe`（+ 默认 `ffmpeg` 剥音轨） | 默认剥离 |

要点：

- **默认就是抽帧多图、且不带音频**。这是本地部署更稳妥的默认行为，无需任何额外配置。
- 想退回“整段视频直发”，必须显式设 `vlm.media_input=video`（环境变量 `CCTV_MEMORY_VLM__MEDIA_INPUT=video` 或 config.yaml）。
- 音频默认 `false`：`frames` 路径天然无音频；`video` 路径默认用 `ffmpeg -an -c:v copy` 把音轨剥掉再发。
  只有显式设 `vlm.include_audio=true`（且 `media_input=video`）才会把原始带音轨文件直接发送。
- 真实抽帧（`frames` 或旧的 `pipeline.video_metadata_mode=ffmpeg_frames`）需要 `ffmpeg`；用 `cctv-memory doctor`
  可直接看到 `media_input`、`video processor`、`needs ffmpeg` 与 `real VLM analysis: READY/NOT READY`。

```bash
# 默认（推荐）：抽帧多图、无音频，无需额外设置
export CCTV_MEMORY_VLM__PROVIDER=real

# 可选：退回整段视频直发（非默认）
export CCTV_MEMORY_VLM__MEDIA_INPUT=video
# 可选：整段视频且保留音频（仅在 media_input=video 下生效）
export CCTV_MEMORY_VLM__INCLUDE_AUDIO=true
```

### 2.4 如何确认“真的在用 real 而不是 mock”

0. **最快：doctor**（运行前即可确认，无需起服务/无需网络）：

```bash
cctv-memory doctor
# [vlm] effective path : REAL    -> 已会走真实路径
# [readiness] real VLM analysis : READY/NOT READY（NOT READY 会列出缺什么）
```

1. **健康端点**（运行中确认）。`GET /api/v1/health` 现在如实返回当前激活的 provider：

```bash
curl -s http://127.0.0.1:8080/api/v1/health
# data.vlm_provider == "real" 表示真实路径已激活
# （此前该字段被硬编码为 "mock"，无法区分，现已修复）
```

2. **记录内容**。mock 适配器产出的文本以 `[mock]` 开头（见 `mock_adapter.py`）。真实 VLM 的
   `static_description_text` / `dynamic_description_text` 是真实自然语言、不含 `[mock]` 前缀。

3. **端到端测试**（需要 `LLM_KEY` + `ffmpeg`）：

```bash
uv run pytest tests/integration/test_real_vlm.py::test_real_vlm_end_to_end -s
# 该用例生成 5 秒测试视频 -> 真实 VLM 调用 -> 断言产出真实记录 -> 打印 VLM 输出
```

---

## 3. HTTP 服务启动（真实路径）

### 3.1 `serve` 命令

`cctv-memory serve` 启动 FastAPI + uvicorn（`cctv_memory/cli/__init__.py:_cmd_serve` →
`bootstrap.build_app` → `api/app.py:create_app`）。

```bash
# 初始化数据目录 + schema + 本地种子（principal/policy/camera）
uv run cctv-memory init --data-dir ./data

# 启动 HTTP 服务（默认 host/port 来自 server 配置：127.0.0.1:8080）
uv run cctv-memory serve --data-dir ./data --host 127.0.0.1 --port 8080
```

`serve` 选项：

| 选项 | 默认 | 说明 |
|---|---|---|
| `--data-dir` | `./data` | 数据目录（SQLite + storage 根） |
| `--host` | 配置 `server.host`（默认 127.0.0.1） | 监听地址 |
| `--port` | 配置 `server.port`（默认 8080） | 监听端口 |
| `--no-worker` | 关 | 即使配置启用内嵌 worker 也不启动它 |
| `--worker-poll-seconds` | `2.0` | 内嵌 worker 空闲轮询间隔 |

启动时若 `worker.enabled && worker.embedded`（默认都为 true），会在后台守护线程内嵌运行
worker 持续 drain 任务，使单进程 `serve` 即可跑通闭环。生产环境推荐 `serve --no-worker`
搭配独立 `worker` 进程（见 §4）。

> 运维安全提醒（`status/archive/incidents/incident-blocking-subprocess.md`）：不要把 `serve` 当作临时调试命令在
> 无超时保护下手动长跑；真实 ffprobe/ffmpeg 路径必须有真实可读媒体文件。本地演示可用
> `CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static` 避免任何子进程。

### 3.2 健康检查

```bash
curl -s http://127.0.0.1:8080/api/v1/health
# {"ok":true,...,"data":{"status":"ok","vlm_provider":"mock|real",
#  "indexing_provider":"mock|real","vector_search_enabled":false,...}}
```

### 3.3 可用 HTTP 路由（与 `api/app.py` 一致）

```text
GET    /api/v1/health
POST   /api/v1/video-sources/analyze
GET    /api/v1/analysis-jobs/{analysis_job_id}
POST   /api/v1/observation-search/contexts
POST   /api/v1/observation-search/contexts/{context_id}/refine
POST   /api/v1/observation-search/contexts/{context_id}/batch-refine
GET    /api/v1/observation-search/contexts/{context_id}/facets
DELETE /api/v1/observation-search/contexts/{context_id}
POST   /api/v1/observation-search/details
POST   /api/v1/observation-search/overlapping-records
POST   /api/v1/observation-search/locators
GET    /api/v1/playback/{token}
POST   /api/v1/admin/backups
POST   /api/v1/exports/user
POST   /api/v1/exports/migration
```

身份：通过请求头 `X-Principal-Id` 传入（缺省用 dev principal `user_admin`）；**绝不**从请求体取身份。
所有响应使用统一 envelope（`{ok, request_id, data|error, meta}`）。

---

## 4. CLI 启动方式（不依赖 HTTP）

CLI 子命令（`cctv_memory/cli/__init__.py`，实测可用）：

```bash
uv run cctv-memory version
uv run cctv-memory health
uv run cctv-memory doctor    [--data-dir ./data] [--json]
uv run cctv-memory init      --data-dir ./data
uv run cctv-memory analyze   --data-dir ./data --source-uri <path> --camera-id cam_lobby_01 \
                              --video-start-time 2026-06-06T21:00:00+08:00 --idempotency-key k1 [--wait] [--enable-high-freq]
uv run cctv-memory worker    --data-dir ./data [--once]
uv run cctv-memory search    --data-dir ./data --query person --top-k 10
uv run cctv-memory reindex   --data-dir ./data [--force]
uv run cctv-memory maintenance sweep --data-dir ./data
uv run cctv-memory backup    --data-dir ./data --out ./backup.sqlite3
uv run cctv-memory restore   --data-dir ./data --in ./backup.sqlite3
uv run cctv-memory benchmark run --data-dir ./data
uv run cctv-memory experiment run --config <file.yaml> --data-dir ./data
```

> `init` 不存在 stop/status 子命令（旧设计草案里的 `stop`/`status` 未实现）。

### 4.1 容量估算报告（离线只读 SQLite）

`scripts/capacity_report.py` 可从现有 SQLite 的 `model_call_logs` / `analysis_jobs` /
`video_sources` 统计 VLM 请求量，并结合实测 GPU/vLLM 吞吐假设生成 Markdown 报告。
它不会调用 VLM，不读取密钥，也不会在报告里输出 `source_uri`。

```bash
uv run python scripts/capacity_report.py \
  --db ./data/cctv_memory.sqlite \
  --camera-count 1000 \
  --wall-time-seconds 420 \
  --gpu-type "H100 80GB" \
  --gpus-per-group 8 \
  --vram-gb-each 80 \
  --max-stable-concurrency 16 \
  --target-window-hours 1 \
  --output capacity-report.md
```

`gpus_per_group` 表示本次 benchmark 的一个 vLLM 服务组里实际用了几张 GPU。
例如你用一套 8×H100 vLLM 服务测出吞吐，那么 `--gpus-per-group 8`；报告里的
`needed_gpu_groups` 是需要复制多少组同样的服务，`needed_gpu_count` 是组数乘以 8。
如果传 `--vram-gb-each`，报告会额外给出等价 `needed_vram_gb`，但 VRAM 只是按已测
benchmark row 做比例换算，不比实测 `req/s` 更精确。

默认报告只展示 analysis scale 汇总，camera/video/job 细分表会隐藏。需要排查单路或
单视频差异时再加：

```bash
uv run python scripts/capacity_report.py ... --include-breakdowns
```

如果数据库缺少可靠视频时长或端到端测试耗时，可显式传：

```bash
uv run python scripts/capacity_report.py \
  --db ./data/cctv_memory.sqlite \
  --camera-count 1000 \
  --video-hours 2.0 \
  --measured-req-s 3.0 \
  --safety-factor 0.7
```

### 4.2 Detector-gated VLM（mock foundation）

Detector gate 默认关闭。开启后，`default_segment` 每个固定窗口会先跑 mock detector；
命中配置规则才调用 VLM，否则发布 detector-only ObservationRecord。Detector-only 记录的
`static_description_text`、`dynamic_description_text`、`tags` 均为空，检测摘要只写入
`attributes.detector_gate`（产品语义 `attr.detector_gate`）。完整逐帧审计写入
`detector_gate_logs`。

示例（仅 mock detector，不接真实 API）：

```yaml
pipeline:
  detector_gate:
    enabled: true
    provider: mock
    model_id: mock-detector-v1
    sample_fps: 1.0
    mock_positive_labels: [person]
    mock_positive_frame_ratio: 0.0
    mock_confidence: 0.9
    rules:
      - label: person
        min_positive_frame_ratio: 0.5
        min_confidence: 0.5
        action: call_vlm
```

生产默认不保存图片 bytes/base64、`source_uri` 或绝对帧路径；只记录每帧 basename/hash/时间戳和 detection metadata。

---

## 5. 完整闭环示例

### 5.1 本地安全闭环（无外部依赖，mock VLM + static 元数据）

```bash
export CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static
uv run cctv-memory init --data-dir ./data
uv run cctv-memory analyze --data-dir ./data \
  --source-uri /any/lobby.mp4 --camera-id cam_lobby_01 \
  --video-start-time 2026-06-06T21:00:00+08:00 --idempotency-key demo-1 --wait
# -> {"accepted": true, "worker_processed_tasks": 1, "job_status": "succeeded"}
uv run cctv-memory search --data-dir ./data --query person --top-k 5
# -> candidate_count >= 1，preview_text 以 [mock] 开头
```

### 5.2 真实 VLM 闭环（需要 LLM_KEY + ffmpeg + 真实视频）

```bash
export CCTV_MEMORY_VLM__PROVIDER=real
export LLM_KEY=<你的key>
# 默认 media_input=frames：抽帧多图发送、无音频（需要 ffmpeg/ffprobe）。
unset CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE
uv run cctv-memory init --data-dir ./data
uv run cctv-memory analyze --data-dir ./data \
  --source-uri /real/readable/clip.mp4 --camera-id cam_lobby_01 \
  --video-start-time 2026-06-06T21:00:00+08:00 --idempotency-key real-1 --wait
# 记录里 static/dynamic 文本为真实描述，不含 [mock]
uv run cctv-memory search --data-dir ./data --query "person" --top-k 5

# 可选：退回整段视频直发（非默认），需显式开启：
# export CCTV_MEMORY_VLM__MEDIA_INPUT=video
```

### 5.4 分析尺度：default_segment / motion_scan / high_freq_event

系统支持多个分析尺度（`AnalysisScale`）。默认只跑 `default_segment`；运动触发的
高频路径为**可选开启**：

| 尺度 | 作用 | 是否产出可检索记录 | 用到的 prompt |
|------|------|------------------|--------------|
| `default_segment` | 全程定长窗口的基线观察 | 是（`default_segment_v1`） | `default_segment_v1` |
| `motion_scan` | 帧差运动检测，找出高频事件触发点 | 否（只产出 `HighFreqTrigger`） | 不调用 VLM |
| `high_freq_event` | 围绕运动触发的短窗口事件细节分析 | 是（`high_freq_event_v1`） | `high_freq_event_v1` |
| `low_freq_summary` | 预留（未启用） | 否 | — |

启用方式（CLI 加 `--enable-high-freq`，或提交时 `analysis_options.enable_motion_triggered_high_freq=true`）：

```bash
# 真实视频 + 运动检测 + 高频事件（需要 ffmpeg/ffprobe）
export CCTV_MEMORY_VLM__PROVIDER=real
export LLM_KEY=<你的key>
unset CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE      # 用真实 ffprobe/ffmpeg
uv run cctv-memory analyze --data-dir ./data \
  --source-uri /real/readable/clip.mp4 --camera-id cam_lobby_01 \
  --video-start-time 2026-06-06T21:00:00+08:00 --idempotency-key hf-1 \
  --wait --enable-high-freq
```

运行后一个 AnalysisJob 会有 3 个 scale task：`default_segment`、`motion_scan`、
`high_freq_event`。若没有检测到运动，`high_freq_event` 会被
`skipped(no_motion_trigger)`，`default_segment` 记录不受影响（job 仍 succeeded）。
若可选尺度处理失败，job 记为 `partial_failed`，但 `default_segment` 记录已发布并保留。

运动检测参数（实验旋钮，`pipeline.motion_scan` / `pipeline.high_freq_event`）：

```yaml
pipeline:
  motion_scan:
    method: frame_diff        # 帧差法（下采样灰度帧的归一化平均绝对差）
    threshold: 0.4            # 触发阈值（0..1）
    min_duration_ms: 1500     # 触发窗口最短时长
    merge_gap_ms: 1000        # 相邻运动段合并间隔
    sample_fps: 2.0           # 运动采样帧率
    frame_width: 64
    frame_height: 36
  high_freq_event:
    window_seconds: 3         # 高频窗口长度
    overlap_ratio: 0.5
    frames_per_segment: 8
```

#### 按尺度检索 / 过滤

记录带有 `analysis_scale` 字段，检索可硬过滤或软偏好：

```bash
# 仅返回高频事件记录（硬过滤）
curl -s -X POST http://127.0.0.1:8080/api/v1/observation-search/contexts \
  -H 'Content-Type: application/json' \
  -d '{"query_text":"running","analysis_scale_filter":["high_freq_event"],"top_k":10}'

# 偏好高频事件（软 boost，不排除其他尺度）
#   -d '{"query_text":"running","preferred_analysis_scales":["high_freq_event"],"top_k":10}'
```

facet 中包含 `analysis_scale_distribution`，可看到各尺度命中数量。

### 5.3 HTTP 闭环（完整：诊断 → 启动 → 健康 → 提交 → 查任务 → 检索 → 关停）

```bash
# 0) 运行前诊断当前配置（确认 mock/real 与缺失项）
cctv-memory doctor

# 1) 安全本地模式（mock VLM + static 元数据，无外部依赖）
export CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static

# 2) 初始化并后台启动（内嵌 worker 会自动 drain 队列）
cctv-memory init  --data-dir ./data
cctv-memory serve --data-dir ./data --host 127.0.0.1 --port 8080 &
SERVER_PID=$!
sleep 2

# 3) 健康检查：确认 vlm_provider 实际值
curl -s http://127.0.0.1:8080/api/v1/health
# -> data.vlm_provider == "mock"（若设 provider=real 则为 "real"）

# 4) 提交视频源做分析（身份走 X-Principal-Id，缺省 user_admin；绝不放进 body）
curl -s -X POST http://127.0.0.1:8080/api/v1/video-sources/analyze \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"file","source_uri":"/any/lobby.mp4","camera_id":"cam_lobby_01",
       "video_start_time":"2026-06-06T21:00:00+08:00","idempotency_key":"http-1"}'
# -> data.analysis_job_id = "job_..."（记下它）

# 5) 轮询任务状态（内嵌 worker 通常很快置为 succeeded）
curl -s http://127.0.0.1:8080/api/v1/analysis-jobs/<job_id>
# -> data.job_status 最终为 "succeeded"

# 6) 检索（创建 SearchContext + 初始检索）
curl -s -X POST http://127.0.0.1:8080/api/v1/observation-search/contexts \
  -H 'Content-Type: application/json' -d '{"query_text":"person","top_k":10}'
# -> data.candidate_count >= 1, data.results[].preview_text 以 [mock] 开头

# 7) 关停
kill "$SERVER_PID"
```

> 真实 VLM 的 HTTP 闭环：把第 1 步换成 `export CCTV_MEMORY_VLM__PROVIDER=real`、
> `export LLM_KEY=<你的key>`，并使用真实可读视频 + `ffprobe` 模式（不要用 static），
> 其余步骤一致；`/health` 的 `vlm_provider` 应为 `real`。

---

## 6. 检索 / details / locator / playback 与 reindex

- **检索**：默认是 FTS5 + 确定性关键词/标签打分（`SearchService`），无需网络、无需 reindex。
- **details**：`POST /observation-search/details`，返回授权范围内记录详情（`include_locator` 可选）。
- **locator / playback**：`POST /observation-search/locators` 签发短期 playback token；
  `GET /api/v1/playback/{token}` 做**二次鉴权**后返回片段描述。**绝不**暴露内部 `source_uri`。
  MVP 不做真实媒体流，playback 返回授权描述符（非真实视频字节）。
- **overlapping-records**：时间交叉记录查询，同样在授权范围内。

### 何时需要 reindex

仅当启用**向量检索**时才需要：

```bash
# 开启向量检索（默认关闭）
export CCTV_MEMORY_INDEXING__ENABLED=true
# 用真实 embedding 时还需： CCTV_MEMORY_INDEXING__PROVIDER=real + 对应 key 环境变量
uv run cctv-memory reindex --data-dir ./data    # 为已授权记录构建向量
```

`indexing.enabled=false`（默认）时不需要 reindex，检索走 FTS。reindex 幂等：第二次运行对
未变化记录会 skip。

---

## 7. doctor —— 运行前配置/就绪诊断

`cctv-memory doctor`（`cctv_memory/application/doctor.py` + CLI）回答一个问题：
**“我现在这套配置到底会走什么路径、缺什么。”** 它：

- 只读取**当前生效配置**（与 runtime 同一套 `AppConfig`，含 `--data-dir`）；
- **绝不打印任何 secret 值**：对每个密钥只显示“环境变量名 + 是否已设置(yes/no)”；
- **不连数据库、不访问外部端点**：readiness 是“本地配置就绪”，并显式标注外部端点未探测；
- readiness 的判定**直接镜像** worker 的真实选择逻辑（`_default_vlm` / `_default_video_processor`），不会与运行时漂移。

用法：

```bash
cctv-memory doctor                 # 人类可读
cctv-memory doctor --json          # 机器可读（同样不含 secret 值）
cctv-memory doctor --data-dir ./data
```

默认配置（mock）下的人类可读输出示例：

```text
cctv-memory doctor — effective configuration diagnosis
============================================================
[base]
  cwd                : /path/to/cctv-memory
  config file        : (none; env + defaults only)
  env                : local
  data dir           : data
  sqlite path        : data/cctv_memory.sqlite3
[vlm]
  provider           : mock
  effective path     : MOCK
  model_id           : gemini-3.1-pro-preview
  media_input        : frames (multi-image frames)
  include_audio      : no
  base_url_env       : CCTV_MEMORY_VLM_BASE_URL (set: no)
  default_base_url   : http://nginx:8081/api/ohmygpt/chat/completions
  api_key_env        : LLM_KEY (set: no)
[pipeline]
  video_metadata_mode: ffprobe
  video processor    : FfprobeVideoProcessor
  needs ffprobe      : yes (found: yes)
  needs ffmpeg       : no (found: yes)
[worker / http]
  worker.enabled     : yes
  worker.embedded    : yes
  server.host        : 127.0.0.1
  server.port        : 8080
[indexing / retrieval]
  indexing.enabled   : no
  indexing.provider  : mock
  rerank_enabled     : no
  rerank_provider    : mock
  embedding key env  : CCTV_MEMORY_EMBEDDING_API_KEY (set: no)
  rerank key env     : CCTV_MEMORY_RERANK_API_KEY (set: no)
[readiness]
  mock analysis      : READY
  real VLM analysis  : NOT READY
      - vlm.provider is not 'real' (set vlm.provider=real or CCTV_MEMORY_VLM__PROVIDER=real to use the real VLM)
      - vlm.api_key_env (LLM_KEY) is not set in the environment
  vector search      : NOT READY
      - indexing.enabled is false (vector search falls back to FTS; set CCTV_MEMORY_INDEXING__ENABLED=true to enable it)
  note               : Readiness reflects LOCAL configuration only; the external VLM/embedding endpoint is not contacted by doctor.
```

readiness 判定规则（truthful）：

| 项 | READY 的条件 |
|---|---|
| `ready_for_mock_analysis` | mock 路径选中的 video processor 所需的 `ffprobe`/`ffmpeg` 在 PATH 上存在（static 模式无需任何二进制） |
| `ready_for_real_vlm_analysis` | `vlm.provider=real` **且** `api_key_env` 已设 **且** 模式能喂真实媒体（`ffprobe`/`ffmpeg_frames`，非 `static`）**且** real 路径选中的 processor 所需二进制存在（默认 `media_input=frames` 需要 `ffmpeg`；`media_input=video` 默认剥音轨也需要 `ffmpeg`，`include_audio=true` 则不需要） |
| `ready_for_vector_search` | `indexing.enabled=true` **且**（real embedder ⇒ 其 key 已设）**且**（real rerank ⇒ 其 key 已设）。注意仍需 `reindex` 才有向量数据 |

任何 `NOT READY` 都会列出**明确缺失原因**。`real VLM analysis: READY` 只代表本地配置就绪，
**不代表**外部端点已被验证可达（doctor 不做网络探测）。

---

## 8. 当前有意不支持 / 仍为占位的部分（不要高估）

| 能力 | 状态 |
|---|---|
| `doctor` 配置/就绪诊断 | ✅ 真实可用（本次新增） |
| `serve` HTTP 启动 | ✅ 真实可用 |
| config.yaml 加载 | ✅ 真实可用 |
| 真实 VLM 调用 | ✅ 可用（需 provider=real + key + 端点可达） |
| 默认抽帧多图发送（media_input=frames，无音频） | ✅ 真实可用（需 ffmpeg；本次新增为默认） |
| 整段视频直发（media_input=video，可选音频） | ✅ 可用（非默认，需显式开启） |
| 真实视频时长（ffprobe）/真实抽帧（ffmpeg_frames） | ✅ 可用（需 ffmpeg/真实文件） |
| FTS 检索 / details / overlap | ✅ 可用 |
| 向量检索 / 外部 reranker | ⚠️ 默认关闭，需开 flag + reindex |
| playback 真实媒体流 | ❌ 占位 token + 授权描述符，无真实视频字节 |
| 产生记录的分析尺度 | `default_segment`（默认）+ `high_freq_event`（运动触发，需 `--enable-high-freq`）；`motion_scan` 只产 HighFreqTrigger；`low_freq_summary` 未启用 |
| 运动检测 | ✅ 真实可用（帧差法，需 ffmpeg；驱动 high_freq_event 触发） |
| auth | dev principal + header；无密码/JWT/会话 |
| `auth/*`、`admin/principals`、`runtime/shutdown` 等 HTTP 路由 | ❌ 未实现（仅契约草案） |
| `init stop` / `init status` 子命令 | ❌ 未实现 |
| Docker | ❌ 当前不打包 |

---

## 9. 验证命令（全部有界）

```bash
cd codes/cctv-memory
uv pip install -e ".[dev]"         # 跑测试/工具需要 dev 依赖
uv run ruff check .
uv run mypy cctv_memory/contracts cctv_memory/domain cctv_memory/application
uv run pytest                      # 全量
uv run pytest tests/integration/test_doctor.py            # doctor 诊断/就绪/无 secret 泄露
uv run pytest tests/integration/test_serve_and_config.py  # serve + config + provider 选择
uv run pytest tests/integration/test_real_vlm.py          # 真实 VLM（含离线单测 + 受控 e2e）
```
