# Pipeline Experiment Contract（分析管线实验契约）

## 0. 文档目的

本文定义如何在不破坏架构、不制造屎山的前提下，对视频分析与检索的多个 pipeline 变量进行实验，发现最优组合，并将结果塞进正式代码。

核心目标：实验可并行跑，结果可对比复现，最优选可无缝进入主链路。

---

## 1. 可实验的 Pipeline 变量

以下维度是实验空间：

| 变量维度 | 示例值 | 影响范围 |
|---------|--------|---------|
| VLM provider/model | Gemini, GPT-4o, Qwen-VL, 本地模型 | `infrastructure/vlm/` adapter |
| Prompt 版本 | prompt-v1, prompt-v2-concise, prompt-v3-event-focus | prompt template 文件 |
| 切片窗口长度 | 8s, 12s, 15s, 20s | worker config |
| 切片 overlap | 0s, 2s, 5s | worker config |
| 抽帧策略 | 1fps, 2fps, keyframe-only, uniform-8 | `infrastructure/video/` |
| 解码后端/选帧 | decode_backend=opencv/ffmpeg；selection_strategy=uniform/score/bins_then_score | `infrastructure/video/`, config |
| motion_scan 阈值 | threshold=0.3, 0.5, 0.7 | worker config |
| high_freq 窗口 | 2s, 4s, 6s | worker config |
| 检索 RRF 权重 | static=1.0/dynamic=1.0, static=1.5/dynamic=0.8 | search config |
| Embedding model | text-embedding-3-small, bge-m3, local-e5 | `infrastructure/indexing/` |
| Tag 词表 | open, restricted-50, domain-specific | prompt + post-processing |

---

## 2. 实验如何不破坏架构

### 2.1 VLM Provider 实验

**规则：** 新 provider = 新 adapter 文件，实现同一个 `VlmAnalyzerPort`。

```text
infrastructure/vlm/
├── openai_adapter.py       # provider A
├── gemini_adapter.py       # provider B
├── qwen_adapter.py         # provider C
└── mock_adapter.py         # dev/test
```

选择哪个 provider 由 `config.yaml` 的 `vlm.provider` 决定。切换 provider 不改 application/domain 代码。

### 2.2 Prompt 版本实验

**规则：** Prompt 以版本化模板文件管理，不硬编码在代码中。

```text
docs/prompts/
├── default_segment_v1.md
├── high_freq_event_v1.md
└── (future variants: default_segment_v2_concise.md, high_freq_event_v2_event_focus.md)
```

- `vlm.prompt_version` 控制 default_segment 的当前版本；high_freq_event 使用其
  scale 专用版本（`high_freq_event_v1`），由 `prompts/__init__.py` 按 scale 选择。
- 每条 ObservationRecord 记录 `prompt_version`
- 对比实验时可以对同一视频用不同 prompt_version 跑两个 AnalysisJob

### 2.3 切片/抽帧参数实验

**规则：** 切片和抽帧参数由 pipeline config 控制，不硬编码在 worker 逻辑中。

```yaml
pipeline:
  default_segment:
    window_seconds: 12
    overlap_seconds: 3
    frame_strategy: uniform
    frames_per_segment: 6
  high_freq_event:
    window_seconds: 3
    overlap_ratio: 0.5
    frame_strategy: uniform
    frames_per_segment: 8
  motion_scan:
    method: frame_diff
    threshold: 0.15
    min_duration_ms: 600
```

改参数 → 改 config → 重跑 → 对比结果。不改 application/domain。

motion_scan 的 `method` 还可切换 detector 实现本身（不只调参）：实现选择走
`infrastructure/video/motion_detector_factory.py` 的注册表/工厂，按 `method` 名解析出一个
`MotionDetectorPort`。`frame_diff` 是首个注册实现；未来 `opencv_mog2`/`ssim`/ML detector 各自
注册 builder + per-method 参数即可，作为本契约 §2.1「新实现=新 adapter，实现同一 Port」规则
在运动检测维度的实例。worker 只依赖 `MotionDetectorPort`，不分支 detector 内部算法。

### 2.4 检索权重实验

**规则：** RRF 和 boost 参数走 search config，不硬编码在 search service 中。

```yaml
search:
  rrf_k: 60
  static_weight: 1.0
  dynamic_weight: 1.0
  fts_weight: 0.5
  max_tag_boost: 0.2
  max_analysis_scale_boost: 0.2
```

改权重后 score_detail 中应体现配置版本，便于复现。

### 2.5 Embedding Model 实验

**规则：** Embedding 作为 IndexAdapter 的配置参数。

```yaml
indexing:
  embedding_provider: openai
  embedding_model: text-embedding-3-small
  embedding_dimensions: 1536
```

更换 embedding model 后需要 reindex（`cctv-memory reindex`）。

---

## 3. 实验对比流程

### 3.1 同一视频多配置对比

```text
1. 选择一组 benchmark 视频（已知 ground truth 或人工标注）
2. 对同一视频用不同 config 各跑一个 AnalysisJob
3. 每个 job 的 ObservationRecord 记录了 model_version + prompt_version + pipeline_version
4. 对比输出质量：描述准确度、tag 覆盖率、事件检出率
5. 对比检索质量：标准 query set → recall@K、precision@K、MRR
```

### 3.2 版本三元组

每条 ObservationRecord 必带：

```text
model_version       VLM 模型标识
prompt_version      prompt 模板版本
pipeline_version    切片/抽帧/后处理配置版本
```

这三个字段保证任何结果都能追溯到产出它的完整 pipeline 配置。

> 说明（任务 cctv-memory-20260611-1805）：`decode_backend` 与选帧策略会改变实际发给 VLM 的
> 像素帧，因此必须由不同 `pipeline_version` 区分。实现把 `pipeline_version` 在组合根按
> `decode_backend` 派生（opencv -> `pipeline-v2-opencv-selector`，ffmpeg -> `pipeline-v1`），
> 写入 AnalysisJob，worker 统一读取（不再硬编码）。**可复现性限制**：OpenCV/FFmpeg 跨版本/
> 跨平台对同一文件的 timestamp 与 seek 可能有细微差异，可复现性保证是“同环境同版本”级别，
> 不是逐字节跨机一致。

### 3.3 实验隔离

实验不应污染生产 active 数据。两种安全方式：

1. **独立 data_dir：** 复制 benchmark 视频到独立目录，用独立 SQLite DB 跑实验。
2. **同 DB 但不同 video_id：** 同一视频以不同 `external_source_id` / `video_start_time` 提交多次，产出独立 ObservationRecord 集。对比后保留最优，archive 次优。

推荐方式 1 用于大规模对比；方式 2 用于快速 A/B。

---

## 4. 最优选如何进入正式代码

一旦确认最优配置：

1. 更新 `config.yaml` 默认值（prompt_version、pipeline 参数、VLM provider）
2. 如果最优选需要新 adapter 或新 prompt 模板，确认它已存在于代码中
3. 对生产数据执行 `cctv-memory rerun --video-ids ... --config new_config.yaml` 或全量重跑
4. 旧 active records 自动进入 ObservationRecordHistory
5. 如有 embedding model 变更，执行 `cctv-memory reindex`

不需要改 domain/application/contracts 代码——因为变量都在 config + adapter + prompt 层。

---

## 5. 禁止事项

- 不为实验创建 if/else 分支写死在 application 层
- 不绕过 repository port 直接操作数据库做"快速实验"
- 不把实验参数硬编码在 worker 逻辑中
- 不在 VLM adapter 内部混合多种 prompt 逻辑（一个 adapter 一个 provider，prompt 模板外置）
- 不跳过 schema validation 为了"实验更快"
- 不在 search service 中 hardcode 特定 model 的 score 修正

---

## 6. Benchmark 工具（建议）

未来可补充：

```bash
cctv-memory benchmark run \
  --videos ./benchmark/videos/ \
  --ground-truth ./benchmark/labels.json \
  --configs config_a.yaml config_b.yaml config_c.yaml \
  --output ./benchmark/results/

cctv-memory benchmark compare \
  --results ./benchmark/results/ \
  --metrics recall@10 precision@10 mrr description_accuracy
```

第一版不强制实现 benchmark CLI，但应保证数据模型支持这种对比（靠 version 三元组 + 可重跑 + 结果可隔离）。

---

## 7. 与架构的关系

本契约是 `ARCHITECTURE_CONSTITUTION.md` §9 Extensibility Rule 的具体实例：

> 新 VLM / LLM 实验只能替换 adapter 或 strategy，不改 domain schema。

实验变量全部落在：
- `infrastructure/` adapters
- `config/` settings
- `docs/prompts/` 模板文件
- `workers/` 的 pipeline 参数

不触碰 domain/application/contracts/repositories 的核心逻辑。
