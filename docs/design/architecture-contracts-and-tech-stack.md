# CCTV Memory 架构契约、模块边界与技术栈建议

## 0. 文档目的

本文是 `data-storage-and-retrieval-design.md` 的配套架构规格，用来单独定义：

1. 各阶段数据格式 / schema / 数据契约（详细字段见 `schema-contracts.md`）；
2. Python 项目的模块边界与依赖方向；
3. 第一版推荐技术栈；
4. 数据库上层能力契约摘要（完整契约见 `database-capability-contract.md`）；
5. 数据库底层适配器契约摘要（完整契约见 `database-adapter-contract.md`）；
6. 数据库表结构规格见 `table-schema-spec.md`；
7. Repository port 契约见 `repository-port-contract.md`；
8. 权限策略契约见 `authorization-policy-contract.md`；
9. 检索契约见 `search-contract.md`；
10. 状态机、错误码、测试、VLM、配置、备份契约分别见对应 `*-contract.md`；
11. 为后续 VLM 分析、LLM 检索、权限体系、索引方案单独演进预留边界。

核心目标：避免业务逻辑、数据库操作、模型调用、检索策略、权限鉴定和视频处理混在一起，降低未来维护和实验成本。

---

## 1. 总体架构风格

推荐采用 **Ports and Adapters / Hexagonal Architecture（端口-适配器架构）**，并结合 Python 的 typed schema 实践。

分层原则：

```text
API / Client Adapter
  ↓
Application Service / Use Case
  ↓
Domain Model / Domain Policy
  ↓
Repository / Gateway Port
  ↓
Infrastructure Adapter
```

含义：

- 业务用例不直接依赖 FastAPI、SQLAlchemy、pgvector、VLM SDK、对象存储 SDK。
- 业务用例依赖抽象 port，例如 `ObservationRepository`、`VectorIndexPort`、`VlmAnalyzerPort`、`AuthzPort`。
- 具体实现放在 infrastructure/adapters，例如 PostgreSQL、pgvector、OpenSearch、某个 VLM provider。
- 后续替换 VLM、换索引、换队列、优化检索策略时，只替换 adapter 或 application 层策略，不污染核心数据契约。

不建议第一版采用重 DDD 框架或过度抽象。只保留清晰依赖方向和 typed schema 即可。

---

## 2. 数据契约分层

所有跨模块流转的数据必须有显式 schema。建议分 5 类：

### 2.1 API Contract Schema

服务端 HTTP API 的请求/响应模型。

示例：

```text
SubmitVideoSourceRequest
SubmitVideoSourceResponse
StartObservationSearchRequest
StartObservationSearchResponse
RefineObservationSearchRequest
GetObservationDetailsRequest
ObservationDetailsResponse
```

要求：

- 只描述外部可见字段；
- 不直接暴露 ORM model；
- 不接受客户端在 body 中声明 principal/role/permission；身份来自 token/session；
- 错误响应统一使用 error code。

### 2.2 Domain Schema

核心业务对象。

```text
CameraLocation
CameraDevice
VideoSource
AnalysisJob
AnalysisScaleTask
HighFreqTrigger
ObservationRecord
ObservationRecordHistory
SearchContext
SearchRevision
SearchCandidate
Principal
AccessPolicy
```

要求：

- Domain schema 表达业务语义；
- 不包含数据库连接、HTTP request、VLM SDK response 等基础设施对象；
- 权限字段如 `access_policy_id`、`security_level` 是系统派生结果，不由 VLM 决定。

### 2.3 Pipeline Message Schema

异步队列和 worker 间传输的数据。

建议至少定义：

```text
AnalyzeVideoMessage
AnalyzeScaleTaskMessage
VlmSegmentRequest
VlmSegmentResult
PublishObservationRecordsCommand
AnalysisJobStatusEvent
```

要求：

- 每条消息必须包含 `schema_version`；
- 必须包含幂等键或可推导幂等键；
- 不传递大视频二进制，只传 `video_id`、`source_uri`、segment 时间范围、frame references；
- worker 输出先进入临时结果/事务，不能直接绕过发布逻辑写 active 表。

### 2.4 VLM I/O Schema

VLM 分析输入输出必须单独定义，不要让 prompt 临时返回自由 JSON。

输入示例：

```text
VlmSegmentInput
- video_id
- camera_id
- analysis_job_id
- analysis_scale
- segment_start_ms
- segment_end_ms
- frame_uris / frame_paths
- prompt_version
- model_version
- tag_vocabulary_hints
```

输出示例：

```text
VlmObservationOutput
- static_description_text
- dynamic_description_text
- tags
- attributes
- quality
- uncertainties
- schema_version
```

要求：

- VLM 输出必须经过 schema validation；
- 失败输出不能直接进入 active ObservationRecord；
- `attributes` 允许 schema-free 扩展，但外层仍必须是 JSON object；
- 任何 bbox/object/confidence 都先作为 attributes 透传，未来再提升为强结构索引。

### 2.5 Index Document Schema

写入全文/向量索引的 document 必须和数据库 schema 解耦。

建议至少两类：

```text
ObservationStaticIndexDocument
ObservationDynamicIndexDocument
```

共同 metadata：

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

要求：

- 权限过滤所需 metadata 必须进入索引；
- 向量检索必须支持 metadata filter，禁止全库向量 topK 后再删无权结果；
- 索引 document 可重建，数据库 active 表是事实源。

---

## 3. 核心模块划分

推荐 Python 包结构：

```text
cctv_memory/
  api/                  # FastAPI routers, request/response schema glue
  application/          # use cases / orchestration
  domain/               # domain models, policies, value objects
  contracts/            # Pydantic schemas shared by modules
  repositories/         # abstract repository interfaces
  services/             # domain/application services interfaces
  infrastructure/
    db/                 # SQLAlchemy models, migrations, repository impl
    indexing/           # pgvector/FTS/OpenSearch adapter
    queue/              # task queue adapter
    vlm/                # VLM provider adapters
    video/              # ffmpeg/frame extraction/file access adapter
    auth/               # token verification, authz policy adapter
    storage/            # local/object storage/playback URL adapter
  workers/              # worker entrypoints only
  cli/                  # CLI entrypoints
  config/               # typed settings
  tests/
```

依赖方向：

```text
api -> application -> domain
workers -> application -> domain
application -> repositories/services ports
infrastructure -> repositories/services ports
```

禁止：

- API router 直接操作 ORM；
- VLM adapter 直接写 ObservationRecord；
- 检索模块直接绕过 AuthZ；
- worker 绕过 AnalysisJob 发布逻辑；
- domain 层 import FastAPI / SQLAlchemy session / vendor SDK。

### 3.1 数据库适配器边界

数据库适配器是 infrastructure 层的低层实现，只负责按约定格式做数据读写，不包含上层业务逻辑。

适配器职责：

```text
CRUD
事务边界
按主键/索引查询
批量插入/更新
UPSERT 原语
分页
基础过滤
行锁/lease/claim task 等数据库相关机制
```

适配器不负责：

```text
决定是否允许某用户访问某资源
决定是否发布 ObservationRecord
决定 VLM 输出是否可信
决定 search_mode / RRF / rerank 策略
决定 AnalysisJob 状态语义
生成业务 error message
调用外部模型或视频处理工具
```

这些业务决策应在 application/domain service 中完成。适配器只执行已经由上层决定的读写命令。

建议定义 repository port，例如：

```text
CameraRepository
VideoSourceRepository
AnalysisJobRepository
ObservationRecordRepository
SearchContextRepository
PrincipalRepository
AuditRepository
TaskQueueRepository
```

每个 port 的输入输出都使用 contracts/domain schema 或明确的 DTO，不返回 ORM model，不向上层暴露 SQLite/PostgreSQL 特定对象。

SQLite 到 PostgreSQL 的迁移目标：

```text
SqliteObservationRecordRepository -> PostgresObservationRecordRepository
SqliteTaskQueueRepository         -> Postgres/RedisTaskQueueRepository
SqliteIndexAdapter                -> PgVector/OpenSearchIndexAdapter
```

application/domain 层不应因此改动。

### 3.2 数据库能力契约（Database Capability Contract）

上层 application/search/auth 模块不应感知具体数据库是 SQLite、PostgreSQL 还是未来的 OpenSearch 组合。它们只依赖统一的数据库能力契约。

数据库适配器必须向上层提供以下语义能力：

```text
transaction
crud
upsert
pagination
candidate_filter
authorized_read
write_path_separation
audit_append
schema_migration
backup_export
```

其中最重要的是权限相关语义：

```text
authorized_read(principal, resource_scope, query) -> 只返回授权范围内数据
write_path_separation -> search path 无法写业务记录
audit_append -> 查询/详情/locator/发布操作可审计
```

对于 PostgreSQL，这些语义可以由数据库 role、GRANT/REVOKE、Row Level Security、只读账号/写账号共同实现。

对于 SQLite，这些语义不能依赖数据库原生用户权限实现；SQLite adapter 必须通过以下工程措施模拟同等语义保证：

```text
服务端独占数据库文件
客户端/AI 禁止直接访问 SQLite 文件
search 使用只读连接或只读 repository
所有查询由 AuthZ 先生成 authorized_scope
repository/index adapter 自动把 authorized_scope 编入 SQL/FTS/vector 候选查询
写入接口只存在于 worker/publication repository
可选 SQLite authorizer hook 禁止 search 连接执行 INSERT/UPDATE/DELETE/DDL
OS 文件权限限制数据库文件访问
```

因此，SQLite adapter 的目标不是让 SQLite 真正拥有 PostgreSQL 的权限系统，而是在服务端边界内向上层提供相同的安全语义。上层代码只相信 `DatabaseCapabilityContract`，不直接相信具体数据库。

适配器测试必须覆盖：

```text
无权记录不会出现在 search result
无权记录不会进入 facet/count/top tags
无权 record_id 调 details/locator 表现为不存在或空
search repository 不能写 ObservationRecord/VideoSource/Policy
Analysis worker 可以通过 publication path 写入
SQLite 与 PostgreSQL adapter 对同一 contract 测试行为一致
```

---

## 4. 功能模块职责

### 4.1 Ingestion Module

负责：

- 校验 `source_uri` 位于 VIDEO_ROOT；
- 校验必填 `camera_id`、`video_start_time`；
- 创建 VideoSource；
- 创建 AnalysisJob；
- 投递分析任务消息。

不负责：

- RTSP 长连接管理；
- VLM 调用；
- 直接生成 ObservationRecord。

### 4.2 Video Processing Module

负责：

- 读取视频元信息；
- 切片；
- 抽帧；
- 生成 VLM 输入引用。

不负责：

- 业务权限判断；
- 写 active ObservationRecord；
- 检索排序。

### 4.3 VLM Analysis Module

负责：

- 根据 analysis_scale 选择 prompt；
- 调用 VLM；
- 校验 VLM 输出 schema；
- 生成 `VlmSegmentResult`。

不负责：

- 直接写 active 表；
- 管理 SearchContext；
- 权限策略。

### 4.4 Publication Module

负责：

- 接收 validated VLM results；
- 在事务内 UPSERT active ObservationRecord；
- 归档旧记录到 ObservationRecordHistory；
- 写出 index update 事件；
- 更新 AnalysisJob / AnalysisScaleTask 状态。

这是唯一允许更新 active ObservationRecord 的业务路径。

### 4.5 Indexing Module

负责：

- 根据 active ObservationRecord 构造 index document；
- 写 static/dynamic vector 和 FTS；
- 删除/替换过期 active document；
- 支持按权限 metadata filter 查询。

数据库 active 表是事实源；索引可重建。

### 4.6 Search Module

负责：

- start/refine/batch_refine/facet；
- static_attribute / dynamic_event / hybrid；
- RRF 融合；
- SearchContext / Revision / Candidate 缓存；
- 调用 AuthZ 计算 authorized_scope；
- 只在授权范围内检索。

不负责：

- 写 ObservationRecord；
- 修改权限策略；
- 调用 VLM。

### 4.7 Auth Module

负责：

- 验证 token/session/service account；
- 解析 principal；
- 计算 capability；
- 计算 authorized_scope；
- 为检索模块提供权限 metadata filter。

第一版只做最小硬边界，不做复杂管理 UI。

### 4.8 Locator / Playback Module

负责：

- 从 ObservationRecord + VideoSource 派生 locator projection；
- 二次鉴权；
- 生成短 TTL playback_url / thumbnail_url / clip_uri。

不负责存储独立 VideoLocator 实体。

### 4.9 Audit Module

负责记录：

- query；
- details；
- locator；
- playback URL issuance；
- AnalysisJob publish。

第一版可先落表或结构化日志，后续再做审计后台。

---

## 5. 技术栈建议

### 5.1 第一版推荐技术栈：文件型 SQLite 优先

为了降低 MVP 部署、备份、上传下载和本地试验成本，第一版推荐使用**文件型数据库优先**的技术栈。核心原则是：业务代码依赖 repository/index/auth/storage port，不依赖具体数据库实现；未来迁移 PostgreSQL / OpenSearch 时主要替换 infrastructure adapter 和 migration，不改 application/domain/contracts。

推荐第一版：

```text
语言：Python 3.12+
Web API：FastAPI
Schema / validation：Pydantic v2
配置：pydantic-settings
主数据库：SQLite 3（单文件，WAL mode）
ORM / SQL：SQLAlchemy 2.x
Migration：Alembic（同时约束 SQLite 与未来 PostgreSQL 的可迁移 DDL）
全文：SQLite FTS5
向量：MVP 可选 sqlite-vec / sqlite-vss；若扩展不可用，则先用授权候选集内的应用层向量重排
队列：SQLite-backed task table + worker loop（MVP）；后续可替换 Redis/RQ 或 Celery
视频处理：ffmpeg / ffprobe + Python wrapper
存储：本地 VIDEO_ROOT 起步；数据库文件、索引文件和视频目录可打包/上传下载
认证：opaque session token / signed token；服务端 SQLite 保存 principal/role/policy
测试：pytest + pytest-asyncio；SQLite 临时文件测试；未来迁移时增加 testcontainers(PostgreSQL/Redis)
代码质量：ruff + mypy（至少对 contracts/application/domain 开启）
```

推荐文件布局：

```text
data/
  cctv_memory.sqlite3        # 主业务库、FTS、SearchContext、审计、权限
  cctv_memory.sqlite3-wal    # WAL，运行时存在
  cctv_memory.sqlite3-shm    # WAL，运行时存在
  videos/                    # VIDEO_ROOT，可选单独管理
  frames/                    # 抽帧缓存，可重建
  artifacts/                 # 缩略图/clip/临时分析产物，可重建或按策略保留
```

上传/下载或备份时应使用 SQLite backup API 或停写后的快照，避免直接复制正在写入的 WAL 数据库导致不一致。

### 5.2 为什么第一版推荐 SQLite + FTS5 + 可选向量扩展

优点：

- 单文件，便于本地运行、上传下载、备份、迁移和问题复现；
- 部署复杂度最低，不需要先引入 PostgreSQL、Redis、OpenSearch；
- SQLAlchemy 和 Alembic 可为未来 PostgreSQL 迁移保留路径；
- SQLite FTS5 足以支撑 MVP 的 tags/static/dynamic 文本检索；
- 权限过滤容易保持在同一个 SQL 查询上下文内；
- SearchContext、审计、权限、任务队列都可以先落在同一文件库内。

限制：

- SQLite 是单写多读，适合 MVP、个人/小团队试验、中小规模批处理，不适合高并发多 worker 大规模写入；
- 向量检索能力不如 pgvector / OpenSearch 成熟；
- 中文分词和复杂全文检索能力有限；
- 数据量大后，FTS 和向量重排需要压测。

### 5.3 文件型 MVP 下的权限过滤与向量检索

硬规则不变：**不能全库向量 topK 后再删除无权结果**。

如果 SQLite 向量扩展支持 metadata pre-filter，应把 `camera_id`、`location_id`、`access_policy_id`、`security_level`、时间范围等过滤条件下推到索引查询。

如果扩展不支持可靠 pre-filter，MVP 应采用安全优先的两阶段策略：

```text
1. 先用 SQLite SQL / FTS5 在授权范围内得到 candidate record_id 集合
2. 只对该授权 candidate 集合做向量相似度计算或重排
3. 返回 topK
```

这可能比专用向量库慢，但不会泄露无权记录。MVP 可以通过候选上限、时间范围、摄像头过滤和 SearchContext 缓存控制性能。

### 5.4 何时迁移 PostgreSQL + pgvector

满足以下条件之一时，迁移到 PostgreSQL：

- 多用户并发写入明显增多；
- AnalysisJob / worker 并发提高，SQLite 单写瓶颈明显；
- 数据量增长到 SQLite FTS/向量重排响应过慢；
- 需要更强事务、连接池、备份、权限隔离或运维能力；
- 需要服务端长期运行而不是便携式项目文件。

迁移目标技术栈：

```text
数据库：PostgreSQL 16+
向量：pgvector
全文：PostgreSQL FTS（或与 OpenSearch 组合）
队列：Redis + RQ / Celery
测试：testcontainers(PostgreSQL/Redis)
```

迁移要求：

- application/domain/contracts 不改；
- 替换 `SqliteObservationRepository` 为 `PostgresObservationRepository`；
- 替换 `SqliteIndexAdapter` 为 `PgVectorIndexAdapter`；
- 数据迁移脚本从 SQLite 导出并导入 PostgreSQL；
- API contract 不变。

### 5.5 何时引入 OpenSearch / Elasticsearch

暂不作为第一版默认依赖。满足以下条件之一时再引入：

- PostgreSQL/pgvector/FTS 性能无法满足；
- 需要更强中文分词、高级全文检索、复杂聚合；
- 需要多索引、多租户、大规模日志/审计/检索分析；
- 需要类似 VSS 的 object-level embedding / frame-level lookup / 多模态检索平台能力。

即使未来引入，也应通过 `IndexPort` adapter 替换，不影响 domain/application 层。

### 5.6 队列选择

MVP 推荐 SQLite-backed task table：

```text
analysis_tasks / worker_leases / retry_count / next_run_at / status
```

优点是少一个外部依赖，数据库文件可整体备份。缺点是高并发和复杂调度能力有限。

如果后续需要复杂 routing、优先级、多 worker 拓扑、分布式重试或高吞吐，再升级 Redis + RQ 或 Celery。队列消息 schema 必须稳定，避免迁移时影响业务层。

### 5.7 VLM Provider

第一版不要绑定具体模型。定义 `VlmAnalyzerPort`：

```text
analyze_segment(input: VlmSegmentInput) -> VlmObservationOutput
```

具体 provider adapter 可以是：

- 本地模型；
- 云 API；
- OpenAI/Anthropic/Gemini/Qwen 等多模态模型；
- 后续专门视频模型。

Prompt 和模型版本必须进入 ObservationRecord，支持重跑和审计。


## 6. 后期可维护性规则

1. 所有跨模块数据必须使用 Pydantic schema 或明确 dataclass，不传裸 dict。
2. 所有 schema 带 `schema_version`，至少 pipeline message / VLM output / index document 必须带。
3. 数据库 ORM model 不直接作为 API response。
4. Index document 可从数据库 active 表重建。
5. Search service 永远只读 ObservationRecord，不提供写接口。
6. 权限过滤在检索前执行，不能检索后裁剪。
7. 新增 VLM / LLM 实验只能替换 adapter 或 strategy，不改 domain schema。
8. 新增强结构视觉能力时，优先进入 `attributes`，确认稳定后再升级为表和索引。
9. MVP 保持 snapshot SearchContext；stream_context 后续单独设计。
10. 每个模块必须有单元测试；跨模块流程使用 integration test 覆盖。

---

## 7. 当前建议结论

现在方案需要从“需求设计”继续推进到“工程契约设计”。各契约已经分别落地，具体见 `docs/CONTEXT_MANIFEST.md` 的权威文档列表。

开发环境、启动流程与 uv 工作流见 `docs/DEVELOPMENT.md`。
包结构与模块职责见 `docs/contracts/module-map.md`。
API 路由表见 `docs/contracts/api-routes.md`。
Pipeline 实验契约见 `docs/contracts/pipeline-experiment-contract.md`。
非功能性需求见 `docs/contracts/nonfunctional-requirements.md`。
