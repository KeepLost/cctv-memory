# Table Schema Spec（数据库表结构规格）

## 0. 文档目的

本文定义 CCTV Memory 第一版数据库逻辑 schema：表、字段、约束、索引、FTS/向量索引映射、SQLite MVP 与 PostgreSQL 迁移差异。

本文不是第三份数据库契约。数据库契约只有两份：

- `database-capability-contract.md`：面向上层的数据库能力契约；
- `database-adapter-contract.md`：面向底层的数据库适配器契约。

本文只是表结构实现规格，服务于 `schema-contracts.md`、repository port 和 database adapter 实现。

本文面向：

- migration 编写者；
- SQLite/PostgreSQL repository adapter；
- contract test 编写者；
- 后续数据库迁移维护者。

相关文档：

- 上层能力语义：`database-capability-contract.md`
- 底层适配器实现：`database-adapter-contract.md`
- 跨模块数据格式：`schema-contracts.md`

---

## 1. 通用数据库约定

### 1.1 ID 与时间

- 所有主键第一版使用 `TEXT` 保存稳定 ID；
- 时间保存 ISO-8601 带 timezone 字符串，或在 PostgreSQL 中使用 `TIMESTAMPTZ`；
- duration/offset 使用毫秒整数；
- JSON 字段 SQLite 使用 `TEXT` 存 JSON，PostgreSQL 使用 `JSONB`。

> 注：上面列出的是各后端的**物理存储形态**。类型的**权威框架见 §1.1bis**——逻辑 schema
> 只有一套规范（领域）类型（时间戳=`datetime`、JSON=`dict`/`list`、嵌入=`list[float]`），
> 各后端物理类型是它的映射结果，由 adapter 双向转换。契约/DTO 一律使用规范类型，不使用
> 任何后端的物理形态。

### 1.1bis 单一规范类型 + 各后端物理映射（Canonical Type + Per-Backend Physical Mapping）

> 本节是 §1.1 的权威细化。逻辑 schema 只有**一套规范（领域）类型**；每个后端的物理类型是
> 该规范类型在该后端的**映射结果**，由对应 adapter 负责双向转换（见
> `database-adapter-contract.md §4.0`）。契约/DTO 永远使用规范类型，不使用任何后端的
> 物理形状。

| 规范（领域）类型 | 契约/DTO 表示 | SQLite 物理 | PostgreSQL 物理 |
|---|---|---|---|
| 时间戳 | `datetime`（带 tz） | `TEXT`（ISO-8601 字符串） | `TIMESTAMPTZ` |
| JSON 对象/数组 | `dict` / `list` | `TEXT`（JSON 文本） | `JSONB` |
| 向量嵌入 | `list[float]` | adapter 表示（序列化文本/占位） | `vector(N)`（pgvector，N 动态） |
| 稳定 ID / 短文本 | `str` | `TEXT` | `TEXT` |
| duration/offset | `int`（毫秒） | `INTEGER` | `INTEGER` |

规则：

- **嵌入向量的契约类型恒为 `list[float]`**；`vector(N)` 仅是 PostgreSQL 物理细节，维度 `N`
  来自 `config.indexing.embedding_dimensions`（动态），因此既不进契约、也不用静态 ORM 类型
  表达，统一由 index adapter 以显式 SQL（`CAST(... AS vector)`）处理。
- ORM 模型（`tables.py`）可用 SQLAlchemy `with_variant` 让同一列在 SQLite 保持
  `String`/`Text`、在 PostgreSQL 渲染 `TIMESTAMPTZ`/`JSONB`；这不改变 SQLite 物理 schema，
  也不改变 PostgreSQL 的权威 DDL（PG schema 由 `infrastructure/db/postgres/schema.py` 控制，
  不经 ORM `create_all` 生成）。
- 任何"假设时间戳是字符串、JSON 是字符串"的上层代码都属违规；转换只能发生在 adapter 边界。

### 1.2 schema_version

必须有表：

```text
schema_metadata
- key TEXT PRIMARY KEY
- value TEXT NOT NULL
```

至少保存：

```text
schema_version
created_at
last_migration_at
```

### 1.3 权限 metadata

所有会进入用户可见检索路径的事实表/索引必须可过滤：

```text
tenant_id
camera_id
location_id
access_policy_id
security_level
observed_start_time / observed_end_time
analysis_scale
```

---

## 2. Auth / Policy 表

### 2.1 principals

```text
principal_id TEXT PRIMARY KEY
principal_type TEXT NOT NULL        -- user / service_account / admin
tenant_id TEXT NOT NULL DEFAULT 'tenant_default'
external_subject_id TEXT NULL
display_name TEXT NOT NULL
status TEXT NOT NULL                -- active / disabled
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

索引：

```text
idx_principals_tenant_status(tenant_id, status)
idx_principals_external_subject(external_subject_id)
```

### 2.2 roles

```text
role_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL
role_name TEXT NOT NULL
capabilities_json TEXT NOT NULL     -- JSON array
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

唯一约束：

```text
UNIQUE(tenant_id, role_name)
```

### 2.3 groups

```text
group_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL
group_name TEXT NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

唯一约束：

```text
UNIQUE(tenant_id, group_name)
```

### 2.4 principal_roles / principal_groups

```text
principal_id TEXT NOT NULL
role_id TEXT NOT NULL
created_at TEXT NOT NULL
PRIMARY KEY(principal_id, role_id)
```

```text
principal_id TEXT NOT NULL
group_id TEXT NOT NULL
created_at TEXT NOT NULL
PRIMARY KEY(principal_id, group_id)
```

### 2.5 access_policies

```text
access_policy_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL
name TEXT NOT NULL
security_level TEXT NOT NULL        -- public / internal / confidential / restricted
rules_json TEXT NOT NULL            -- AccessPolicyRules
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

唯一约束：

```text
UNIQUE(tenant_id, name)
```

---

## 3. Camera / Video 表

### 3.1 camera_locations

```text
location_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL DEFAULT 'tenant_default'
building TEXT NULL
floor TEXT NULL
area TEXT NOT NULL
room_or_zone TEXT NULL
location_desc TEXT NULL
access_policy_id TEXT NULL
security_level TEXT NOT NULL DEFAULT 'internal'
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

索引：

```text
idx_locations_policy(access_policy_id, security_level)
idx_locations_area(area)
```

### 3.2 camera_devices

```text
camera_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL DEFAULT 'tenant_default'
camera_name TEXT NOT NULL
location_id TEXT NOT NULL
manufacturer TEXT NULL
model TEXT NULL
serial_number TEXT NULL
install_position_desc TEXT NULL
stream_uri TEXT NULL
access_policy_id TEXT NULL
status TEXT NOT NULL                -- active / inactive / maintenance
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

索引：

```text
idx_camera_location(location_id)
idx_camera_policy(access_policy_id)
idx_camera_status(status)
```

### 3.3 video_sources

```text
video_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL DEFAULT 'tenant_default'
source_type TEXT NOT NULL           -- file / rtsp_chunk / object_storage / external
source_uri TEXT NOT NULL
original_source_uri TEXT NULL
camera_id TEXT NOT NULL
video_start_time TEXT NOT NULL
video_end_time TEXT NULL
duration_ms INTEGER NULL
source_status TEXT NOT NULL         -- pending / ready / failed
external_source_id TEXT NULL
access_policy_id TEXT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

约束：

```text
UNIQUE(camera_id, video_start_time)
```

索引：

```text
idx_video_camera_time(camera_id, video_start_time, video_end_time)
idx_video_policy(access_policy_id)
idx_video_status(source_status)
```

---

## 4. Analysis 表

### 4.1 analysis_jobs

```text
analysis_job_id TEXT PRIMARY KEY
video_id TEXT NOT NULL
job_status TEXT NOT NULL            -- queued/running/succeeded/partial_failed/failed/cancelled
idempotency_key TEXT NOT NULL
analysis_options_json TEXT NOT NULL
model_version TEXT NULL
prompt_version TEXT NULL
pipeline_version TEXT NULL
created_record_ids_json TEXT NOT NULL DEFAULT '[]'
updated_record_ids_json TEXT NOT NULL DEFAULT '[]'
archived_record_ids_json TEXT NOT NULL DEFAULT '[]'
failed_segment_ids_json TEXT NOT NULL DEFAULT '[]'
created_at TEXT NOT NULL
started_at TEXT NULL
finished_at TEXT NULL
error_code TEXT NULL
error_message TEXT NULL
```

约束：

```text
UNIQUE(idempotency_key)
```

索引：

```text
idx_jobs_video(video_id)
idx_jobs_status(job_status, created_at)
```

### 4.2 analysis_scale_tasks

```text
scale_task_id TEXT PRIMARY KEY
analysis_job_id TEXT NOT NULL
analysis_scale TEXT NOT NULL
status TEXT NOT NULL
total_units INTEGER NOT NULL DEFAULT 0
succeeded_units INTEGER NOT NULL DEFAULT 0
failed_units INTEGER NOT NULL DEFAULT 0
skipped_reason TEXT NULL
created_at TEXT NOT NULL
started_at TEXT NULL
finished_at TEXT NULL
error_code TEXT NULL
error_message TEXT NULL
```

约束：

```text
UNIQUE(analysis_job_id, analysis_scale)
```

### 4.3 high_freq_triggers

```text
trigger_id TEXT PRIMARY KEY
analysis_job_id TEXT NOT NULL
scale_task_id TEXT NOT NULL
video_id TEXT NOT NULL
trigger_start_ms INTEGER NOT NULL
trigger_end_ms INTEGER NOT NULL
motion_score REAL NULL
change_score REAL NULL
trigger_reason TEXT NOT NULL
status TEXT NOT NULL
idempotency_key TEXT NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
error_code TEXT NULL
error_message TEXT NULL
```

约束：

```text
CHECK(trigger_start_ms < trigger_end_ms)
UNIQUE(idempotency_key)
```

---

### 4.4 analysis_units (新增: 任务 cctv-memory-20260611-1410)

最小可调度/审计的 VLM 工作单元（default_segment 窗口 / high_freq_event 触发窗口）。

```text
unit_id TEXT PRIMARY KEY
analysis_job_id TEXT NOT NULL
scale_task_id TEXT NOT NULL
video_id TEXT NOT NULL
analysis_scale TEXT NOT NULL
unit_kind TEXT NOT NULL           -- default_segment_window / high_freq_event_window
segment_start_ms INTEGER NOT NULL
segment_end_ms INTEGER NOT NULL
window_index INTEGER NOT NULL
trigger_id TEXT NULL
status TEXT NOT NULL              -- pending/running/succeeded/failed/skipped
attempt_count INTEGER NOT NULL DEFAULT 0
max_attempts INTEGER NOT NULL DEFAULT 1
last_error_code TEXT NULL
last_error_message TEXT NULL
latest_model_call_id TEXT NULL
successful_model_call_id TEXT NULL
produced_record_ids_json TEXT NOT NULL DEFAULT '[]'
idempotency_key TEXT NOT NULL     -- analysis_job_id:scale_task_id:scale:start:end
created_at TEXT NOT NULL
started_at TEXT NULL
finished_at TEXT NULL
```

约束：

```text
CHECK(segment_start_ms < segment_end_ms)
UNIQUE(idempotency_key)
```

索引：

```text
idx_units_scale_status(scale_task_id, status)
idx_units_job_scale(analysis_job_id, analysis_scale)
idx_units_status_started(status, started_at)   -- 任务 cctv-memory-20260612-1854: 有界孤儿回收
```

### 4.5 model_call_logs (新增: 任务 cctv-memory-20260611-1410)

VLM/model 调用的可观测性记录。仅存储文本型输入输出和媒体 refs/metadata，不存 base64 媒体 blob。

```text
model_call_id TEXT PRIMARY KEY
analysis_job_id TEXT NOT NULL
scale_task_id TEXT NOT NULL
unit_id TEXT NOT NULL
analysis_scale TEXT NOT NULL
segment_start_ms INTEGER NOT NULL
segment_end_ms INTEGER NOT NULL
provider TEXT NOT NULL
model_id TEXT NULL
prompt_version TEXT NULL
pipeline_version TEXT NULL
status TEXT NOT NULL              -- running/succeeded/failed
attempt_count INTEGER NOT NULL DEFAULT 0
error_type TEXT NULL
error_message TEXT NULL
raw_text_input TEXT NULL          -- 文本型 prompt（不含媒体 base64）
raw_text_output TEXT NULL         -- 原始文本响应（不含 key/内部路径）
parsed_output_json TEXT NULL
validation_status TEXT NULL
payload_hash TEXT NULL
response_hash TEXT NULL
media_refs_json TEXT NOT NULL DEFAULT '[]'    -- media refs/metadata，不含 base64
attempt_details_json TEXT NOT NULL DEFAULT '[]'
started_at TEXT NULL
finished_at TEXT NULL
duration_ms INTEGER NULL
created_at TEXT NOT NULL
```

安全约束：
- `raw_text_input/output` 不包含 API key、Authorization、source_uri、base64 媒体内容。
- `media_refs_json` 只存 uri/mime/size/hash/dimensions，不存 base64 blob。
- production 默认 `media_log_mode=metadata_only`（只记录 refs）；
  `debug_media_retention=true` 时才写 artifact_root 全量并记录 artifact refs。

> 说明（OpenCV FrameStream, 任务 cctv-memory-20260611-1805）：当 `decode_backend=opencv`，
> 每条 `media_refs` 在既有 uri/mime/size_bytes/sha256 之外，额外携带选帧来源标量
> `frame_index`、`timestamp_ms`、`decode_backend`、`selection_reason`、`motion_score`、
> `scene_score`、`blur_score`、`brightness`。这些是 JSON 内的非破坏新增键，**不改表 DDL**，
> 仍严禁 base64/source_uri。

索引：

```text
idx_model_calls_unit(unit_id, created_at)
idx_model_calls_job(analysis_job_id, analysis_scale)
```

### 4.6 detector_gate_logs（新增: 任务 cctv-memory-20260622-1800）

Detector-gated VLM 的轻量检测与 gate 决策审计记录。默认只保存 metadata/hash/检测框，不保存图片 bytes/base64、`source_uri` 或绝对帧路径。

```text
gate_log_id TEXT PRIMARY KEY
analysis_job_id TEXT NOT NULL
scale_task_id TEXT NOT NULL
unit_id TEXT NOT NULL
video_id TEXT NOT NULL
analysis_scale TEXT NOT NULL
segment_start_ms INTEGER NOT NULL
segment_end_ms INTEGER NOT NULL
provider TEXT NOT NULL
model_id TEXT NULL
status TEXT NOT NULL
error_type TEXT NULL
error_message TEXT NULL
raw_text_output TEXT NULL
parsed_output_json TEXT NULL
validation_status TEXT NULL
attempt_details_json TEXT NOT NULL DEFAULT '[]'
decision_json TEXT NOT NULL
frame_evidence_json TEXT NOT NULL
evidence_hash TEXT NOT NULL
rule_config_hash TEXT NULL
media_refs_json TEXT NOT NULL DEFAULT '[]'
artifact_refs_json TEXT NOT NULL DEFAULT '[]'
started_at TEXT NULL
finished_at TEXT NULL
duration_ms INTEGER NULL
created_at TEXT NOT NULL
```

索引：

```text
idx_detector_gate_unit(unit_id, created_at)
idx_detector_gate_job(analysis_job_id, analysis_scale)
```

`frame_evidence_json` 记录每帧 `frame_index/timestamp_ms/uri_basename/frame_hash/detections`。`ObservationRecord.attributes_json.detector_gate` 只保存 compact summary；完整逐帧证据在本表。

### 4.7 analysis_timeline_events（新增: 任务 cctv-memory-20260624-1228）

本地分析执行时间线事件表。该表是 append-only observability evidence，不是业务状态源；
`analysis_jobs` / `analysis_scale_tasks` / `analysis_units` / `model_call_logs` /
`detector_gate_logs` / publication 表仍是权威状态。

```text
timeline_event_id TEXT PRIMARY KEY
trace_id TEXT NOT NULL
span_id TEXT NULL
parent_span_id TEXT NULL
analysis_job_id TEXT NULL
task_id TEXT NULL
scale_task_id TEXT NULL
unit_id TEXT NULL
model_call_id TEXT NULL
video_id TEXT NULL
analysis_scale TEXT NULL
unit_kind TEXT NULL
segment_start_ms INTEGER NULL
segment_end_ms INTEGER NULL
event_name TEXT NOT NULL
event_phase TEXT NOT NULL          -- instant/start/finish/fail
status TEXT NULL
attempt_count INTEGER NULL
occurred_at TEXT NOT NULL
duration_ms INTEGER NULL
error_code TEXT NULL
error_message TEXT NULL            -- bounded/redacted
correlation_json TEXT NOT NULL DEFAULT '{}'
metadata_json TEXT NOT NULL DEFAULT '{}'
created_at TEXT NOT NULL
```

PostgreSQL physical mapping:

```text
occurred_at / created_at -> TIMESTAMPTZ
correlation_json / metadata_json -> JSONB
```

索引：

```text
idx_timeline_job_time(analysis_job_id, occurred_at)
idx_timeline_unit_time(unit_id, occurred_at)
idx_timeline_model_call(model_call_id, occurred_at)
idx_timeline_trace_time(trace_id, occurred_at)
idx_timeline_event_name_time(event_name, occurred_at)
```

安全约束：不得存 API key、Authorization、`source_uri`、raw media/base64、完整内部路径；
metadata/correlation 只允许安全 ID、窗口、耗时、计数、hash、basename/非敏感 artifact ref。

写入语义：timeline 写入失败默认 fail-open，不能回滚或失败分析主流程；实现应短事务写入，避免放大 SQLite 单写争用。

---

## 5. Observation 表

### 5.1 observation_records

当前 active 记录表。

```text
record_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL DEFAULT 'tenant_default'
video_id TEXT NOT NULL
analysis_job_id TEXT NOT NULL
analysis_scale TEXT NOT NULL
segment_start_ms INTEGER NOT NULL
segment_end_ms INTEGER NOT NULL
observed_start_time TEXT NOT NULL     -- video_start_time + segment_start_ms
observed_end_time TEXT NOT NULL       -- video_start_time + segment_end_ms
camera_id TEXT NOT NULL             -- 系统从 VideoSource/CameraDevice 派生，便于权限过滤
location_id TEXT NOT NULL           -- 系统从 CameraDevice/CameraLocation 派生，便于权限过滤
static_description_text TEXT NOT NULL
dynamic_description_text TEXT NOT NULL
tags_json TEXT NOT NULL             -- JSON array
clip_uri TEXT NULL
thumbnail_uri TEXT NULL
attributes_json TEXT NOT NULL       -- JSON object
access_policy_id TEXT NOT NULL
security_level TEXT NOT NULL
model_version TEXT NULL
prompt_version TEXT NULL
pipeline_version TEXT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

约束：

```text
CHECK(segment_start_ms < segment_end_ms)
UNIQUE(video_id, segment_start_ms, segment_end_ms, analysis_scale)
```

索引：

```text
idx_obs_video_time(video_id, segment_start_ms, segment_end_ms)
idx_obs_observed_time(observed_start_time, observed_end_time)
idx_obs_camera_time(camera_id, observed_start_time, observed_end_time)
idx_obs_location_time(location_id, observed_start_time, observed_end_time)
idx_obs_policy(access_policy_id, security_level)
idx_obs_scale(analysis_scale)
```

`camera_id` / `location_id` 是 MVP 必填冗余字段，由 VideoSource/CameraDevice/CameraLocation 派生，不由 VLM 决定。这样 search/facet/index 权限过滤不需要依赖运行时多表 join，降低泄露风险和实现复杂度。

### 5.2 observation_record_history

历史审计表。字段可复用 observation_records 快照，并增加：

```text
history_id TEXT PRIMARY KEY
old_record_id TEXT NOT NULL
replaced_by_record_id TEXT NULL
archived_by_analysis_job_id TEXT NOT NULL
archived_at TEXT NOT NULL
archive_reason TEXT NOT NULL
record_snapshot_json TEXT NOT NULL
```

### 5.3 FTS 表

SQLite MVP：

```text
observation_static_fts(record_id UNINDEXED, text)
observation_dynamic_fts(record_id UNINDEXED, text)
observation_tags_fts(record_id UNINDEXED, text)
```

FTS 表可重建，不是事实源。

### 5.4 Vector 表

MVP 可选：

```text
observation_vectors
- record_id TEXT NOT NULL
- vector_type TEXT NOT NULL      -- static / dynamic / tags
- embedding BLOB/TEXT NOT NULL   -- adapter-defined
- metadata_json TEXT NOT NULL
- PRIMARY KEY(record_id, vector_type)
```

如果使用 sqlite-vec/sqlite-vss，可由 adapter 定义具体虚拟表，但必须满足 `database-capability-contract.md` 的权限语义。

---

## 6. SearchContext 表

### 6.1 search_contexts

```text
context_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL
principal_id TEXT NOT NULL
session_id TEXT NULL
authorized_scope_hash TEXT NOT NULL
dataset_revision TEXT NOT NULL
mode TEXT NOT NULL                 -- snapshot / stream
default_revision_id TEXT NULL
created_at TEXT NOT NULL
last_accessed_at TEXT NOT NULL
expires_at TEXT NOT NULL
status TEXT NOT NULL               -- active / expired / closed / failed
```

### 6.2 search_revisions

```text
revision_id TEXT PRIMARY KEY
context_id TEXT NOT NULL
parent_revision_id TEXT NULL
op TEXT NOT NULL
op_params_json TEXT NOT NULL
candidate_count INTEGER NOT NULL
facets_json TEXT NULL
created_at TEXT NOT NULL
```

### 6.3 search_candidates

```text
revision_id TEXT NOT NULL
record_id TEXT NOT NULL
rank INTEGER NOT NULL
score REAL NOT NULL
score_detail_json TEXT NOT NULL
PRIMARY KEY(revision_id, record_id)
```

索引：

```text
idx_candidates_revision_rank(revision_id, rank)
```

---

## 7. Task Queue 表

```text
analysis_tasks
task_id TEXT PRIMARY KEY
schema_version TEXT NOT NULL
task_type TEXT NOT NULL
payload_json TEXT NOT NULL
status TEXT NOT NULL              -- queued / running / succeeded / failed / retry_scheduled
priority INTEGER NOT NULL DEFAULT 0
retry_count INTEGER NOT NULL DEFAULT 0
max_retries INTEGER NOT NULL DEFAULT 3
next_run_at TEXT NOT NULL
lease_owner TEXT NULL
lease_expires_at TEXT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
error_code TEXT NULL
error_message TEXT NULL
```

索引：

```text
idx_tasks_claim(status, next_run_at, priority)
idx_tasks_lease(lease_expires_at)
```

---

## 8. Audit 表

```text
audit_events
audit_event_id TEXT PRIMARY KEY
event_type TEXT NOT NULL
request_id TEXT NULL
principal_id TEXT NULL
session_id TEXT NULL
context_id TEXT NULL
resource_scope_hash TEXT NULL
record_ids_json TEXT NOT NULL DEFAULT '[]'
video_id TEXT NULL
camera_id TEXT NULL
metadata_json TEXT NOT NULL DEFAULT '{}'
created_at TEXT NOT NULL
```

索引：

```text
idx_audit_principal_time(principal_id, created_at)
idx_audit_event_type_time(event_type, created_at)
idx_audit_request(request_id)
```

---

## 9. Backup / Export 表

可选记录备份任务：

```text
backup_jobs
backup_job_id TEXT PRIMARY KEY
backup_type TEXT NOT NULL          -- admin_full_backup / user_export
principal_id TEXT NULL
status TEXT NOT NULL
manifest_json TEXT NULL
created_at TEXT NOT NULL
finished_at TEXT NULL
error_code TEXT NULL
error_message TEXT NULL
```

---

## 10. SQLite 与 PostgreSQL 差异约束

SQLite MVP：

- JSON 用 TEXT；
- FTS5 虚拟表；
- 向量表由 adapter 决定；
- 无 DB user/RLS；
- 用只读连接/repository/authorizer/OS 文件权限模拟权限语义。

PostgreSQL：

- JSON 用 JSONB；
- 时间用 TIMESTAMPTZ；
- 向量用 pgvector；
- 可启用 DB role/RLS；
- FTS 可用 tsvector/GIN。

上层不得依赖这些差异。

---

## 11. Migration 要求

- 所有 schema 变更必须有 migration；
- SQLite migration 与未来 PostgreSQL migration 应尽量共享逻辑字段名；
- 破坏性 migration 必须提供导出/导入或数据修复步骤；
- migration 后必须通过 contract tests。
