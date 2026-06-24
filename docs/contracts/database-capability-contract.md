# Database Capability Contract（面向上层的数据库能力契约）

## 0. 文档目的

本文定义 CCTV Memory 上层代码可以依赖的数据库语义能力。读者是：

- application service；
- domain service；
- search module；
- auth module；
- worker use case；
- API 层间接依赖者。

本文不描述 SQLite/PostgreSQL/OpenSearch 的具体实现方式，只描述上层可假定的能力与语义保证。底层实现要求见 `database-adapter-contract.md`。

核心目标：让上层代码只依赖 `DatabaseCapabilityContract`，不写 `if database == sqlite` 之类分支。

---

## 1. 总体能力列表

数据库系统（由 repository/index/task/audit adapters 组成）必须向上层提供以下能力：

```text
transaction
crud
upsert
pagination
idempotency
candidate_filter
authorized_read
authorized_count
authorized_facet
authorized_locator_lookup
write_path_separation
search_context_persistence
task_claim_with_lease
atomic_publication
audit_append
schema_version
backup_export
```

这些能力可以由单一 SQLite 文件实现，也可以由 PostgreSQL + pgvector + OpenSearch + Redis 等多个底层系统共同实现，但对上层表现必须一致。

---

## 2. 事务与一致性能力

### 2.1 transaction

必须支持在一个事务边界内完成多表一致性写入。

上层可假定：

```text
with transaction:
  write A
  write B
  write C
```

要么全部成功，要么全部回滚。

MVP 必须覆盖：

- 创建 VideoSource + AnalysisJob；
- 原子发布 ObservationRecord active + ObservationRecordHistory；
- 创建 SearchRevision + SearchCandidate；
- 写审计日志。

### 2.2 schema_version

数据库必须保存当前 schema version。启动时必须校验：

```text
app_supported_schema_range contains db_schema_version
```

不兼容时拒绝启动或要求 migration。

---

## 3. CRUD / Query 能力

### 3.1 crud

必须支持按主键和唯一键进行基础 CRUD：

```text
get_by_id
list_by_filter
insert
update
delete_or_archive
```

业务层不直接接触 ORM model；所有输入输出必须是 contract/domain DTO。

### 3.2 upsert

必须支持按业务唯一键 UPSERT。

MVP 必须支持：

```text
VideoSource: (camera_id, video_start_time)
ObservationRecord: (video_id, segment_start_ms, segment_end_ms, analysis_scale)
```

ObservationRecord UPSERT 只能由 publication path 调用。

### 3.3 pagination

必须支持稳定分页。列表 API 优先 cursor 分页，不依赖 offset 作为主契约。

---

## 4. 幂等能力

### 4.1 idempotency

mutation 类操作必须支持幂等键或等价唯一约束。

上层可假定：

```text
same idempotency_key + same payload -> same semantic result
same idempotency_key + different payload -> idempotency_conflict
```

适用：

- SubmitVideoSource；
- AnalysisJob 创建；
- HighFreqTrigger 创建；
- Publication command。

---

## 5. 权限读取能力

### 5.1 authorized_read

这是最重要能力。任何面向用户/AI 的读取必须支持：

```text
authorized_read(principal, authorized_scope, query) -> rows
```

语义保证：

- 无权记录不会出现在结果中；
- 无权记录不会进入排序候选；
- 无权记录不会影响 score、rank、cursor；
- 无权 record_id 查询详情时表现为不存在或空结果；
- 不返回“有结果但你无权访问”这类泄露存在性的信息；
- AuthorizedScope 各资源维度按 `authorization-policy-contract.md` §4.1 解释：维度间 AND，空 allowed 列表表示无许可，歧义 fail closed。

### 5.2 authorized_count

统计必须只统计授权范围内数据。

用于：

- candidate_count；
- SearchContext revision count；
- API list count（如需要）。

### 5.3 authorized_facet

facet / top tags / camera distribution / time distribution / analysis_scale distribution 必须只基于授权范围。

禁止：

```text
全库统计 -> 删除无权项
```

### 5.4 authorized_locator_lookup

locator / playback URL 查询必须二次鉴权。

语义：

```text
authorized_locator_lookup(principal, record_ids)
```

只返回 principal 可访问记录的 locator projection；无权 record_id 表现为不存在。

---

## 6. 搜索候选能力

### 6.1 candidate_filter

必须支持按以下 metadata 过滤候选：

```text
tenant_id
camera_id
location_id
video_id
analysis_scale
observed_start_time / observed_end_time
access_policy_id
security_level
tags
```

### 6.2 static_text_search

必须支持在授权候选范围内对静态描述搜索：

```text
static_description_text
static_description_vector（如可用）
```

### 6.3 dynamic_text_search

必须支持在授权候选范围内对动态描述搜索：

```text
dynamic_description_text
dynamic_description_vector（如可用）
```

### 6.4 vector_rerank_within_authorized_scope

如果底层向量库不能权限 prefilter，则必须先生成授权候选集，再只在授权候选集内做向量相似度或重排。

硬规则：

```text
禁止全库向量 topK 后再删除无权结果。
```

### 6.5 hybrid_search_support

必须支持保存多通道 score detail：

```text
static_score / static_rank
dynamic_score / dynamic_rank
tag_boost
analysis_scale_boost
rrf_score
final_score
```

---

## 7. SearchContext 能力

数据库系统必须支持：

```text
create_context
get_context
close_context
expire_context
create_revision
save_candidates
load_candidates
facet_candidates
```

语义：

- context 绑定 principal/session/authorized_scope_hash；
- revision 不可变；
- candidate 属于某个 revision；
- refine 不能扩大授权范围；
- context_id 不是权限凭证。

---

## 8. 任务队列能力

MVP 可以使用 SQLite task table，后续可换 Redis/RQ/Celery，但上层只依赖以下能力：

```text
enqueue_task
claim_task_with_lease
refresh_lease
mark_succeeded
mark_failed
schedule_retry
list_pending
```

语义：

- 同一任务不会被多个 worker 同时长期持有；
- worker 崩溃后 lease 过期可重试；
- retry_count / next_run_at / error_code 可记录；
- task payload 遵守 pipeline message schema。

---

## 9. 原子发布能力

### 9.1 atomic_publication

Publication path 必须支持：

```text
publish_observation_records_atomically(command)
```

语义：

1. 校验 AnalysisJob / AnalysisScaleTask 状态；
2. 对同一 `(video_id, segment_start_ms, segment_end_ms, analysis_scale)` UPSERT active ObservationRecord；
3. 被替换旧记录进入 ObservationRecordHistory；
4. 更新 AnalysisJob created/updated/archived record ids；
5. 生成 index update 事件或标记待重建索引；
6. 追加 audit event；
7. 整体事务一致。

Publication 是唯一可写 active ObservationRecord 的业务能力。

---

## 10. 写入路径隔离能力

### 10.1 write_path_separation

数据库系统必须保证：

```text
AI-facing search path 无法写业务记录
worker/publication path 才能写 active/history/job/index state
admin path 才能写 policy/camera/user 管理数据
```

在 PostgreSQL 中可通过 DB roles 实现；在 SQLite 中由 repository capability、只读连接、authorizer hook 和服务端边界模拟。

---

## 11. 审计能力

### 11.1 audit_append

必须支持追加审计事件：

```text
append_audit_event(event)
```

事件类型至少包括：

```text
query
facet
details
locator
playback_url_issued
analysis_job_created
publication_succeeded
publication_failed
policy_changed
```

审计事件必须可关联：

```text
request_id
principal_id
session_id
context_id
record_ids
video_id
camera_id
resource_scope_hash
timestamp
```

---

## 12. Timeline observability 能力

### 12.1 timeline_append

必须支持追加本地分析时间线事件：

```text
append_timeline_event(event)
list_timeline_events_by_job(analysis_job_id, time_range)
```

语义：

- append-only；
- observability-only，不作为 AnalysisJob / AnalysisUnit / Publication 的权威状态；
- 支持按 job/trace/unit/model_call 时间顺序查询；
- 时间和 JSON 字段在 repository DTO 中使用规范类型；
- timeline write failure 默认 fail-open，不改变分析结果；
- 不存 secrets、Authorization、source_uri、raw media/base64、完整内部路径。

---

## 13. Backup / Export 能力

### 13.1 backup_export

必须区分：

- 管理员完整备份；
- 普通用户授权范围导出。

语义：

```text
admin_backup -> 可包含完整数据库与视频文件
user_export(principal) -> 只能包含授权范围内数据
```

完整 SQLite DB 文件只允许管理员级备份/迁移，不可作为普通用户导出格式。

---

## 14. 禁止事项

上层代码禁止：

- 直接判断数据库类型；
- 直接拼接 SQLite/PostgreSQL 特定 SQL；
- 直接访问 ORM model；
- 绕过 authorized_read 做用户可见查询；
- 全库向量 topK 后裁剪无权结果；
- 从 search path 调写入方法；
- 直接向客户端暴露数据库文件路径或内部 source_uri。

---

## 15. Contract Test 要求

任何数据库能力实现都必须通过同一套 contract tests：

```text
authorized_read_hides_forbidden_records
authorized_facet_excludes_forbidden_records
locator_requires_second_authz
search_repository_is_read_only
publication_can_upsert_and_archive_atomically
idempotency_conflict_detected
task_claim_lease_expires_and_retries
audit_event_appended
timeline_event_appended
backup_export_respects_scope
```

SQLite adapter 和未来 PostgreSQL adapter 必须对这些测试表现一致。
