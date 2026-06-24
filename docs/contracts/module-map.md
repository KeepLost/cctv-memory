# Module Map（包结构与职责映射）

## 0. 文档目的

定义 Python 包结构中每个模块的职责边界和文件组织，开发者拿到后知道代码写在哪里。

---

## 1. 顶层包结构

```text
cctv_memory/
├── __init__.py
├── cli/                     # CLI 入口
├── api/                     # FastAPI routers + request/response glue
├── application/             # use cases / orchestration services
├── domain/                  # domain models, value objects, policies
├── contracts/               # Pydantic schemas（跨模块共享）
├── repositories/            # abstract repository port interfaces
├── services/                # abstract service port interfaces
├── infrastructure/          # 具体实现 adapters
│   ├── db/                  # SQLAlchemy models, migrations, repository impl
│   ├── indexing/            # FTS5 / sqlite-vec / pgvector adapter
│   ├── queue/               # task queue adapter
│   ├── vlm/                 # VLM provider adapters
│   ├── video/               # ffmpeg/frame extraction adapter
│   ├── auth/                # token/session verification adapter
│   └── storage/             # file/object storage, playback URL adapter
├── workers/                 # worker entrypoints
└── config/                  # typed settings (pydantic-settings)
```

---

## 2. 各模块职责

### 2.1 `contracts/`

跨模块共享的 Pydantic v2 schema 定义。

```text
contracts/
├── __init__.py
├── common.py               # TimeRange, Pagination, ApiEnvelope, ErrorResponse
├── video.py                # VideoSource, SubmitVideoSourceRequest/Response
├── observation.py          # ObservationRecord, ObservationRecordHistory
├── analysis.py             # AnalysisJob, AnalysisScaleTask, HighFreqTrigger
├── search.py               # SearchContext, SearchRevision, SearchCandidate, SearchRequest/Response
├── auth.py                 # Principal, AccessPolicy, AuthorizedScope
├── vlm.py                  # VlmSegmentRequest, VlmSegmentResult, VlmObservationOutput
├── pipeline.py             # AnalyzeVideoMessage, PublishCommand 等 pipeline messages
├── index.py                # ObservationStaticIndexDocument, DynamicIndexDocument
├── audit.py                # AuditEvent
└── backup.py               # BackupManifest
```

规则：这里只定义数据形状，不含业务逻辑。

### 2.2 `domain/`

领域模型和领域策略。

```text
domain/
├── __init__.py
├── models.py               # 核心 domain entities（如果需要行为方法）
├── policies.py             # AuthZ scope 计算、publication 规则、security_level 比较
├── enums.py                # SourceType, AnalysisScale, JobStatus, SearchMode 等枚举
└── exceptions.py           # domain-level 异常
```

规则：不 import FastAPI、SQLAlchemy、任何 vendor SDK。

### 2.3 `repositories/`

抽象 port 接口（Protocol 或 ABC）。

```text
repositories/
├── __init__.py
├── camera.py               # CameraRepository
├── video_source.py         # VideoSourceRepository
├── analysis_job.py         # AnalysisJobRepository
├── scale_task.py           # AnalysisScaleTaskRepository
├── trigger.py              # HighFreqTriggerRepository
├── observation.py          # ObservationRecordRepository
├── search_context.py       # SearchContextRepository
├── principal.py            # PrincipalRepository
├── access_policy.py        # AccessPolicyRepository
├── index.py                # IndexPort
├── task_queue.py           # TaskQueueRepository
├── audit.py                # AuditRepository
└── backup.py               # BackupPort
```

### 2.4 `services/`

抽象 service port 接口。

```text
services/
├── __init__.py
├── vlm_analyzer.py         # VlmAnalyzerPort
├── video_processor.py      # VideoProcessorPort
├── motion_detector.py      # MotionDetectorPort
└── auth_verifier.py        # AuthVerifierPort (token -> Principal)
```

### 2.5 `application/`

业务 use case 编排。每个文件对应一个业务流程。

```text
application/
├── __init__.py
├── ingestion.py            # SubmitVideoSource use case
├── analysis_orchestrator.py # AnalysisJob 调度与状态推进
├── publication.py          # 原子发布 ObservationRecord
├── search.py               # Start/Refine/Facet/Details/Overlap
├── locator.py              # Locator projection + 二次鉴权
├── auth.py                 # Login/Register/AuthorizedScope 计算
├── backup.py               # Backup/Export/Restore
└── maintenance.py          # Reindex, expire contexts, cleanup
```

规则：
- 决定事务边界
- 调用 repository/service ports
- 不 import infrastructure 具体实现

### 2.6 `api/`

FastAPI routers。

```text
api/
├── __init__.py
├── app.py                  # FastAPI app factory
├── deps.py                 # Depends：get_current_principal, get_db_session, etc.
├── middleware.py           # request_id, error envelope, CORS
├── routers/
│   ├── health.py
│   ├── auth.py
│   ├── video_sources.py
│   ├── analysis_jobs.py
│   ├── observation_search.py
│   ├── playback.py
│   └── admin.py
└── schemas.py              # 仅 API 层的 response wrapper（如有特殊需要）
```

规则：
- router 只做参数解析、调用 application service、返回 envelope
- 不直接操作 ORM
- 不包含业务决策

### 2.7 `infrastructure/db/`

```text
infrastructure/db/
├── __init__.py
├── engine.py               # create_engine, session factory
├── models/                 # SQLAlchemy ORM models（内部用）
├── repositories/           # port 的 SQLite/PostgreSQL 实现
│   ├── sqlite_observation.py
│   ├── sqlite_video_source.py
│   ├── sqlite_search_context.py
│   └── ...
└── migrations/             # Alembic versions
```

### 2.8 `infrastructure/vlm/`

```text
infrastructure/vlm/
├── __init__.py
├── mock_adapter.py         # 测试/开发用 mock
├── openai_adapter.py       # OpenAI compatible adapter
├── gemini_adapter.py       # Google Gemini adapter
└── base.py                 # 共享工具函数
```

### 2.9 `workers/`

```text
workers/
├── __init__.py
├── analysis_worker.py      # claim task, dispatch to scale handlers
├── default_segment.py      # default_segment 切片 + VLM + 校验
├── motion_scan.py          # motion detection logic
├── high_freq_event.py      # high_freq triggered analysis
└── publisher.py            # publication atomic commit
```

### 2.10 `config/`

```text
config/
├── __init__.py
└── settings.py             # pydantic-settings AppConfig
```

---

## 3. 依赖方向总结

```text
api → application → domain
                  → repositories (ports)
                  → services (ports)

workers → application → (same as above)

infrastructure/db → repositories (implements ports)
infrastructure/vlm → services (implements VlmAnalyzerPort)
infrastructure/video → services (implements VideoProcessorPort)

contracts ← 所有模块共享读取
```

禁止反向依赖（见 ARCHITECTURE_CONSTITUTION §3）。

---

## 4. 新增功能应放在哪里

| 需求 | 放置位置 |
|------|---------|
| 新 API endpoint | `api/routers/` + `application/` use case |
| 新 domain 规则 | `domain/policies.py` |
| 新 schema/DTO | `contracts/` 对应文件 |
| 新 repository 方法 | `repositories/` port + `infrastructure/db/repositories/` impl |
| 新 VLM provider | `infrastructure/vlm/` 新 adapter |
| 新 search op | `application/search.py` + `search-contract.md` 更新 |
| 新 analysis_scale | `domain/enums.py` + `workers/` handler + contracts |
| 新配置项 | `config/settings.py` + `configuration-contract.md` |
