# Repository Port Contract（仓储接口契约）

## 0. 文档目的

本文定义 application/domain 层可调用的 repository port。Repository port 是上层业务与底层数据库适配器之间的边界。

原则：

- port 方法只接收/返回 contracts/domain DTO；
- 不暴露 ORM model、SQLAlchemy session、SQLite connection、PostgreSQL cursor；
- 不包含业务决策；
- 用户可见读取必须接收 AuthorizedScope 或由上层保证已经在安全上下文中；
- SQLite/PostgreSQL adapter 必须实现同一 port contract；
- **时间参数一律使用规范类型 `datetime`，不使用 ISO 字符串**（database-adapter-contract §4.0、
  table-schema-spec §1.1bis）。各 adapter 在自身边界做物理转换：SQLite 转 ISO 文本比较/写入，
  PostgreSQL 经 `_as_dt(...)` 绑定原生 TIMESTAMPTZ。上层无论传参还是读 DTO 字段都只见 `datetime`，
  全项目已无 `*_iso: str` 时间参数残留（含 TaskQueue、SearchContext、AnalysisUnit、VideoSource）。

---

## 1. 通用约定

### 1.1 TransactionManager

```text
begin() -> Transaction
commit(transaction)
rollback(transaction)
```

Application service 负责决定事务边界。Adapter 负责实现事务。

### 1.2 Result 语义

- 找不到：返回 `None` 或空列表；
- 无权资源：对用户可见读取表现为 `None` 或空列表；
- 唯一冲突：抛出/返回 `conflict`；
- 幂等冲突：抛出/返回 `idempotency_conflict`。

---

## 2. CameraRepository

```text
get_location(location_id) -> CameraLocation | None
list_locations(filter, page) -> Page[CameraLocation]
upsert_location(location) -> CameraLocation

get_camera(camera_id) -> CameraDevice | None
list_cameras(filter, page) -> Page[CameraDevice]
upsert_camera(camera) -> CameraDevice
```

用户可见 list/get 必须通过 service 层做 AuthZ；admin path 可直接管理。

---

## 3. VideoSourceRepository

```text
create_or_get_by_idempotency(request) -> VideoSource
get_by_id(video_id) -> VideoSource | None
get_authorized_by_id(video_id, authorized_scope) -> VideoSource | None
list_authorized(filter, authorized_scope, page) -> Page[VideoSource]
mark_status(video_id, status, error=None) -> None
```

约束：

- `(camera_id, video_start_time)` 唯一；
- source_uri canonicalization 在 ingestion service 完成，repository 只保存结果。

---

## 4. AnalysisJobRepository

```text
create_job(job) -> AnalysisJob
get_job(analysis_job_id) -> AnalysisJob | None
get_job_for_video(video_id) -> list[AnalysisJob]
list_jobs(filter, page) -> Page[AnalysisJob]
update_status(analysis_job_id, status, timestamps, error=None) -> None
append_record_publish_summary(analysis_job_id, created_ids, updated_ids, archived_ids) -> None
```

幂等：

```text
get_by_idempotency_key(idempotency_key) -> AnalysisJob | None
```

---

## 5. AnalysisScaleTaskRepository

```text
create_scale_task(task) -> AnalysisScaleTask
get_scale_task(scale_task_id) -> AnalysisScaleTask | None
get_by_job_and_scale(analysis_job_id, analysis_scale) -> AnalysisScaleTask | None
update_counters(scale_task_id, total, succeeded, failed) -> None
update_status(scale_task_id, status, error=None, skipped_reason=None) -> None
```

---

## 6. HighFreqTriggerRepository

```text
create_or_get_by_idempotency(trigger) -> HighFreqTrigger
get_trigger(trigger_id) -> HighFreqTrigger | None
list_by_job(analysis_job_id) -> list[HighFreqTrigger]
update_status(trigger_id, status, error=None) -> None
```

---

## 7. ObservationRecordRepository

### 7.1 只读方法

```text
get_active_by_id(record_id) -> ObservationRecord | None
get_authorized_active_by_id(record_id, authorized_scope) -> ObservationRecord | None
get_authorized_active_by_ids(record_ids, authorized_scope) -> list[ObservationRecord]
list_active_by_video(video_id, authorized_scope, page) -> Page[ObservationRecord]
find_overlapping(record_id, filters, authorized_scope) -> list[ObservationRecord]
```

### 7.2 Publication 写入方法

只允许 publication path 使用：

```text
publish_records_atomically(command) -> PublicationResult
```

内部语义：

- UPSERT active records；
- archive replaced records；
- update AnalysisJob publish summary；
- append audit event；
- transaction 一致。

Search path 不得获得该 repository 的写接口。

---

## 8. SearchContextRepository

```text
create_context(context) -> SearchContext
get_context(context_id) -> SearchContext | None
close_context(context_id) -> None
expire_contexts(now) -> int
create_revision(revision, candidates) -> SearchRevision
get_revision(revision_id) -> SearchRevision | None
list_candidates(revision_id, page) -> Page[SearchCandidate]
replace_default_revision(context_id, revision_id) -> None
facet_revision(revision_id, facet_spec) -> FacetResult
```

约束：

- revision 不可变；
- context 绑定 principal/session/authorized_scope_hash；
- repository 不决定权限，只保存与校验绑定字段。

---

## 9. PrincipalRepository / AccessPolicyRepository

```text
get_principal(principal_id) -> Principal | None
get_principal_by_external_subject(provider, subject) -> Principal | None
create_principal(principal) -> Principal
set_principal_status(principal_id, status) -> None
list_roles(principal_id) -> list[Role]
list_groups(principal_id) -> list[Group]

get_access_policy(access_policy_id) -> AccessPolicy | None
list_access_policies(filter) -> list[AccessPolicy]
upsert_access_policy(policy) -> AccessPolicy
```

---

## 10. IndexRepository / IndexPort

```text
upsert_static_document(doc) -> None
upsert_dynamic_document(doc) -> None
delete_record_documents(record_id) -> None
search_static(query, authorized_scope, filters, limit) -> list[IndexHit]
search_dynamic(query, authorized_scope, filters, limit) -> list[IndexHit]
fts_search(query, authorized_scope, filters, limit) -> list[IndexHit]
vector_rerank(candidate_ids, query_embedding, vector_type, limit) -> list[IndexHit]
rebuild_from_active_records(batch_size) -> ReindexResult
```

硬规则：

- search_* 必须应用 authorized_scope；
- vector_rerank 只能在授权 candidate_ids 内执行；
- Index document 可重建，数据库 active 表是事实源。

---

## 11. TaskQueueRepository

```text
enqueue_task(task) -> Task
claim_task(worker_id, now) -> Task | None
refresh_lease(task_id, worker_id, lease_until) -> None
mark_succeeded(task_id) -> None
mark_failed(task_id, error, retry_policy) -> None
schedule_retry(task_id, next_run_at) -> None
list_pending(filter, page) -> Page[Task]
```

> 类型约定（type-unification 后）：`Task` DTO 的时间字段是规范类型 `datetime`
> （database-adapter-contract §4.0、table-schema-spec §1.1bis）。`claim_task` /
> `refresh_lease` / `schedule_retry` 的**标量时间参数**（`now` / `lease_until` /
> `next_run_at`）也已统一为 `datetime`：调用方传 `datetime`，adapter 在各自边界转换
> （SQLite 转 ISO 文本比较/写入，PostgreSQL 经 `_as_dt(...)` 用原生 TIMESTAMPTZ 绑定）。
> 上层无论拿数据还是调方法，都只见规范类型 `datetime`，无 ISO 字符串残留。

---

## 12. AuditRepository

```text
append_event(event) -> AuditEvent
list_events(filter, page) -> Page[AuditEvent]
```

Audit append 不应阻塞核心查询太久；实现可先同步落表，未来可异步化。

---

## 12.5 TimelineRepository

TimelineRepository stores append-only local analysis observability events. It is
not a business-state repository and must not be used to decide job/unit success.
Existing lifecycle repositories remain authoritative.

```text
append_event(event: AnalysisTimelineEvent) -> AnalysisTimelineEvent
append_events(events: list[AnalysisTimelineEvent]) -> list[AnalysisTimelineEvent]
list_by_job(analysis_job_id, since: datetime | None = None, until: datetime | None = None, limit: int = 100000) -> list[AnalysisTimelineEvent]
list_by_trace(trace_id, limit: int = 100000) -> list[AnalysisTimelineEvent]
```

Rules:

- Port DTO fields use canonical `datetime` and `dict` types.
- Adapter boundary owns SQLite TEXT/JSON and PostgreSQL TIMESTAMPTZ/JSONB conversion.
- Append/list methods must not expose ORM/session/cursor.
- Timeline writer failures are fail-open in the recorder/helper, not in adapter code.
- Timeline metadata must already be redacted before append; adapter should not add sensitive data.

---

## 12.6 Admin Model Failure Diagnostics

Failure diagnostics are internal/admin reads only. They must not be exposed through
AI-facing search/detail repositories.

```text
ModelCallLogRepository.list_by_job(analysis_job_id) -> list[ModelCallLog]
PreVlmGateLogRepository.list_by_job(analysis_job_id) -> list[PreVlmGateLog]
```

Rules:

- Caller must enforce `runtime.manage` or an equivalent admin/operator capability.
- Normal observation search/detail/locator paths must not depend on these methods.

---

## 13. BackupRepository / BackupPort

```text
create_admin_backup(request) -> BackupManifest
create_user_export(request, authorized_scope) -> BackupManifest
restore_admin_backup(manifest, source) -> RestoreResult
```

普通用户 export 必须带 authorized_scope。

---

## 14. Contract Test 要求

每个 repository adapter 必须跑：

```text
sqlite_repository_contract_tests
postgres_repository_contract_tests（未来）
index_adapter_contract_tests
queue_adapter_contract_tests
```

测试重点：

- SQLite/PostgreSQL 对同一 port 行为一致；
- authorized_scope 强制生效；
- search path 无写能力；
- publication path 原子性；
- backup/export scope 正确。
