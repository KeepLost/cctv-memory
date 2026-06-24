# Database Adapter Contract（面向底层实现的数据库适配器契约）

## 0. 文档目的

本文定义 SQLite、PostgreSQL、OpenSearch/Elasticsearch、Redis/RQ/Celery 等底层适配器如何实现 `database-capability-contract.md` 中规定的上层语义能力。

读者是：

- SQLite repository adapter 实现者；
- PostgreSQL repository adapter 实现者；
- Index adapter 实现者；
- Task queue adapter 实现者；
- Migration / backup adapter 实现者；
- Contract test 编写者。

核心目标：允许 MVP 使用 SQLite，同时让上层代码认为数据库系统具备稳定能力；未来迁移 PostgreSQL/OpenSearch 时尽量只替换 infrastructure adapter。

---

## 1. Adapter 分组

建议底层适配器分为：

```text
RelationalRepositoryAdapter
IndexAdapter
TaskQueueAdapter
AuthStoreAdapter
AuditAdapter
BackupAdapter
MigrationAdapter
```

SQLite MVP 可以由同一个 SQLite 文件实现多个 adapter；PostgreSQL 正式版可以拆成 PostgreSQL + Redis + OpenSearch 等多个后端。

---

## 2. Repository Port 列表

底层至少实现以下 repository ports：

```text
CameraRepository
VideoSourceRepository
AnalysisJobRepository
AnalysisScaleTaskRepository
HighFreqTriggerRepository
ObservationRecordRepository
ObservationRecordHistoryRepository
SearchContextRepository
PrincipalRepository
AccessPolicyRepository
AuditRepository
TaskQueueRepository
```

约束：

- port 输入输出使用 contracts/domain DTO；
- 不返回 ORM model；
- 不向上层暴露 connection/session/cursor；
- 不要求上层知道 SQLite/PostgreSQL 差异。

---

## 3. SQLite Adapter 实现要求

SQLite 是 MVP 文件型数据库，不提供原生用户/role/RLS。SQLite adapter 必须通过工程措施实现上层所需语义。

### 3.1 文件访问边界

要求：

```text
SQLite 文件只由服务端进程访问
客户端/AI 禁止直接访问 SQLite 文件
OS 文件权限限制数据库文件读写
备份/导出通过 BackupAdapter，不直接复制运行中 DB 文件
```

建议：

```text
PRAGMA journal_mode=WAL
PRAGMA foreign_keys=ON
PRAGMA busy_timeout=...
```

### 3.2 只读与写入分离

SQLite adapter 必须区分：

```text
SqliteSearchRepository       只读
SqlitePublicationRepository  可写 active/history/job/index state
SqliteAdminRepository        可写 principal/policy/camera 管理数据
```

Search repository 要求：

- 使用 `mode=ro` 连接，或只暴露只读方法；
- 可选使用 SQLite authorizer hook 拦截 INSERT/UPDATE/DELETE/DDL；
- 不暴露 raw execute；
- contract test 必须验证 search path 不能写业务记录。

### 3.3 权限过滤实现

所有用户可见查询必须接收：

```text
AuthorizedScope
```

SQLite adapter 必须将其编入 SQL / FTS / candidate query，并遵守 `authorization-policy-contract.md` §4.1 的组合语义：维度间 AND，空 allowed 列表表示无许可，歧义 fail closed。

```text
camera_id IN authorized.camera_ids
AND location_id IN authorized.location_ids
AND access_policy_id IN authorized.policy_ids
AND security_level <= authorized.max_security_level
AND observed time overlap requested range
```

无权资源不得进入：

```text
result
count
facet
top_tags
candidate set
locator
```

### 3.4 向量检索实现

若使用 sqlite-vec / sqlite-vss 且支持可靠 metadata pre-filter，则必须下推权限 metadata filter。

若不支持可靠 pre-filter，则必须采用：

```text
1. SQL/FTS 在授权范围内生成 bounded candidate ids
2. 只对这些 candidate ids 做应用层向量相似度或扩展内重排
3. 返回 topK
```

禁止：

```text
全库向量 topK -> 删除无权结果
```

### 3.5 Task queue 实现

SQLite task table 至少包含：

```text
task_id
schema_version
task_type
payload_json
status
priority
retry_count
max_retries
next_run_at
lease_owner
lease_expires_at
created_at
updated_at
error_code
error_message
```

claim 语义：

- 只 claim `status=queued` 且 `next_run_at <= now` 的任务；
- claim 时写入 `lease_owner` 和 `lease_expires_at`；
- lease 过期后可被其它 worker claim；
- SQLite 单写限制下，claim 必须短事务完成。
- **原子性（任务 cctv-memory-20260615-1620 多 job 并发）**：claim 必须对并发 worker 安全——
  两个 worker 不得同时 claim 同一行。实现为**条件 UPDATE**：
  `UPDATE ... SET status='running', lease_owner=?, lease_expires_at=? WHERE task_id=? AND <仍可领条件>`，
  其中 `<仍可领条件>` 在写入时刻重新校验 `next_run_at<=now AND (status='queued' OR (status='running' AND
  lease 过期))`。至多一个 worker 的 UPDATE 命中（`rowcount==1`）即获得该任务；竞争失败者 `rowcount==0`，
  探测下一候选。候选选择顺序仍为 `priority desc, next_run_at`；探测次数有界（`_CLAIM_MAX_PROBES`，
  **非全表扫描**）。不新增任何任务状态枚举。优先级/过期重领/重试调度语义保持不变。

---

## 4. PostgreSQL Adapter 实现要求

PostgreSQL 是正式多用户/强权限/高并发目标后端。

### 4.0 原生类型边界规则（Native-Type Boundary Rule）

> 本节是强制规则。最近一连串 PostgreSQL 缺陷（提交 9c323a2 / 7f9cdd7 / 57aabd3 /
> 79aa7d9 / 5bf74ed）均来自同一裂缝：上层把"SQLite 一切皆字符串"的存储形状当成了
> 规范类型，导致 PostgreSQL 的原生 `datetime` / `dict` 泄漏到上层撞碎"它是字符串"的假设。
> 本规则把类型边界一次性钉死。

1. **契约/DTO 只使用最丰富的领域规范类型，既不用 SQLite 存储形状、也不耦合 PostgreSQL
   专有物理类型：**
   - 时间戳 → `datetime`（带 tz）；
   - JSON → `dict` / `list`；
   - 向量嵌入 → `list[float]`（**绝不**在契约里出现 `pgvector` / `vector(N)`，那是物理细节）。

2. **所有物理类型转换由各 DB adapter 在边界双向、对称地完成；domain/application/worker
   层保持后端无关，禁止出现 backend 条件分支。**
   - SQLite adapter：写 `datetime → ISO 文本`、`dict/list → JSON 文本`；读反向。
   - PostgreSQL adapter：`datetime ↔ TIMESTAMPTZ`、`dict/list ↔ JSONB`（原生）；
     `list[float] ↔ vector(N)` 由 index adapter 负责。

3. **PostgreSQL 仓库不得依赖 SQLite 形状的 ORM `String`/`Text` 标注来读写
   TIMESTAMPTZ/JSONB/vector 列。** ORM 模型的 `String`/`Text` 标注只对 SQLite 物理类型
   正确；在 PostgreSQL 上用 ORM `update().values()` 或 ORM 属性赋值写这些列，会渲染成
   `::VARCHAR` / `::TEXT` 而与原生列类型冲突（DatatypeMismatch），或在读取时把原生
   `datetime`/`dict` 直接抛给只接受字符串的上层。

4. **PostgreSQL 原生列必须经显式转换写入/读出**：使用 typed/text SQL、显式 `CAST(... AS
   jsonb)` / `CAST(... AS vector)`、或在 mapper 中经归一化 helper（`_dt` / `_loads_obj` /
   `_loads_list`）转换。即使 §6（ORM with_variant）让 ORM 标注变得"诚实"，PG 写路径仍以
   显式转换为准，不依赖 ORM 隐式渲染。

5. **防御纵深（见 §6）**：ORM 模型应通过 `with_variant` 在 PostgreSQL 方言下声明
   TIMESTAMPTZ/JSONB，使得即便有人误用自然 ORM 写法也不再渲染成 VARCHAR/TEXT。但这是
   兜底，不替代第 3、4 条的显式转换要求。架构测试必须守护本规则（见 `testing-contract.md`）。

### 4.1 账号与 role

建议至少三类 DB role：

```text
search_service_role  SELECT only
worker_role          INSERT/UPDATE on job/result/index state tables
admin_role           manage principal/policy/camera/location/migration
```

### 4.2 GRANT / REVOKE

PostgreSQL adapter 必须使用数据库权限加强 write_path_separation：

- search repository 使用只读 role；
- publication repository 使用 worker role；
- admin repository 使用 admin role。

### 4.3 Row Level Security

如果启用 RLS，必须保证 RLS policy 与服务端 AuthZ scope 语义一致。

即使启用 RLS，服务端仍应显式传入 AuthorizedScope，用于：

- query planning；
- facet 过滤；
- index metadata filter；
- audit resource_scope_hash。

### 4.4 pgvector / FTS

PostgreSQL adapter 使用 pgvector 时：

- vector query 必须带权限 metadata filter；
- 不允许全表 vector topK 后裁剪；
- FTS query 同样必须带权限 filter；
- 复杂 hybrid/RRF 逻辑可在 application/search service 层完成。

### 4.5 扩展安装与权限（生产）

`CREATE EXTENSION vector` / `pg_trgm` 需要较高权限。生产环境必须：

- 由 DBA/管理员或迁移流程在部署前**预置扩展**（`CREATE EXTENSION IF NOT EXISTS vector`），
  使用专门的迁移/管理 role；
- **禁止**给长期运行的应用 role（search/worker/admin app user）授予 `SUPERUSER`；
- 应用运行 role 只需 §4.2 规定的最小读写权限，不应承担扩展安装职责。

> 注：本地一次性测试库为方便起见可临时授予测试 role superuser 以建扩展，这仅限本地测试，
> 不得带入生产配置或文档示例。

### 4.6 TimelineRepository 原生类型

PostgreSQL adapter 必须用显式 SQL/CAST 处理 timeline 写入：

- `occurred_at` / `created_at` 绑定为 TIMESTAMPTZ；
- `correlation_json` / `metadata_json` 写为 JSONB；
- list 查询返回 canonical DTO 类型；
- 不依赖 SQLite 物理字符串形状。

---

## 5. OpenSearch / Elasticsearch Index Adapter 要求

若未来引入 OpenSearch/Elasticsearch：

### 5.1 Index document 必备 metadata

```text
record_id
video_id
camera_id
location_id
analysis_scale
observed_start_time
observed_end_time
access_policy_id
security_level
tags
model_version
prompt_version
pipeline_version
```

### 5.2 权限过滤

所有 search / aggregation / facet query 必须包含授权 metadata filter。

禁止：

```text
全索引 topK / aggregation -> 应用层删除无权结果
```

### 5.3 索引可重建

OpenSearch/Elasticsearch 不是事实源。事实源是关系数据库 active ObservationRecord。索引必须可从关系数据库重建。

---

## 6. Backup Adapter 要求

### 6.1 SQLite backup

运行中备份必须使用：

- SQLite backup API；或
- 停写 + WAL checkpoint + 打包；或
- 专门 export 流程。

不可简单复制正在写入的 `.sqlite3` 文件并声称备份一致。

### 6.2 Admin backup vs User export

```text
admin_backup -> 可包含完整数据库/视频/frames/artifacts
user_export  -> 只包含授权范围内数据
```

BackupAdapter 必须区分两者。

---

## 7. Migration Adapter 要求

Migration 必须满足：

- schema version 可检查；
- migration 可重复运行或有明确状态；
- SQLite -> PostgreSQL 导出导入必须通过 contract schema，不依赖 ORM 私有结构；
- migration 后 contract tests 必须通过。

SQLite MVP 阶段使用 Alembic 时，应避免大量 PostgreSQL-only DDL 泄漏进 domain/application。

---

## 8. 错误语义映射

底层数据库错误必须映射为统一错误语义：

```text
unique_violation -> conflict / idempotency_conflict
foreign_key_violation -> validation_error or conflict
not_found -> not_found
permission_denied -> capability_denied
connection_error -> storage_unavailable
lock_timeout -> retryable_storage_error
```

适配器不生成面向用户的自然语言业务解释，只返回标准错误 code 与必要 details。

---

## 9. Contract Tests

所有 adapter 必须跑同一套 contract tests。

### 9.1 权限测试

```text
forbidden_records_hidden_from_search
forbidden_records_hidden_from_facet
forbidden_record_details_returns_empty_or_not_found
locator_requires_authorization
```

### 9.2 写入隔离测试

```text
search_repository_cannot_insert
search_repository_cannot_update
search_repository_cannot_delete
publication_repository_can_upsert_active
admin_repository_can_update_policy
```

### 9.3 原子发布测试

```text
publication_upsert_archives_old_record
publication_rollback_on_history_failure
publication_updates_job_record_ids
```

### 9.4 任务队列测试

```text
task_claim_sets_lease
task_lease_expiry_allows_reclaim
task_retry_updates_next_run_at
```

### 9.5 迁移一致性测试

```text
sqlite_and_postgres_return_same_authorized_results
sqlite_and_postgres_apply_same_idempotency_semantics
sqlite_and_postgres_search_context_behaviour_same
```

---

## 10. 禁止事项

Adapter 实现禁止：

- 把业务策略写进 SQL adapter；
- 返回 ORM model 给 application；
- 暴露 raw SQL 给上层；
- 在无 AuthorizedScope 的情况下执行用户可见查询；
- 在 index adapter 中先全局召回再裁剪无权结果；
- 让客户端/AI 直接打开 SQLite 文件；
- 让 search path 拥有写 active ObservationRecord 的能力。
