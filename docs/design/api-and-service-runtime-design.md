# CCTV Memory API 格式、服务运行与客户端-服务端交互设计

> 实现状态（2026-06-10）：本文是**设计权威**，描述目标态客户端-服务端架构。当前代码只实现
> **服务端**侧：FastAPI `/api/v1`（请求体+响应 envelope 已契约化进 `/openapi.json`）、服务端
> 授权与范围裁定、以及 `AuthVerifierPort` 身份接缝（dev 信任实现读 `X-Principal-Id`）。本文
> §2.1/§2.3 的**客户端 SDK / Tool Proxy** 与 §2.3 的 `/api/v1/auth/*`、token 签发/校验**尚未
> 实现**，是独立交付物 + 生产鉴权的未来工作；落地时在 `AuthVerifierPort` 处插入，api/
> application/domain 其余层不变。已实现 vs 未实现的逐路由标注见 `docs/contracts/api-routes.md`，边界
> 定义见 `docs/SERVER_CLIENT_BOUNDARY.md`。

## 0. 文档目的

本文是 `data-storage-and-retrieval-design.md` 与 `architecture-contracts-and-tech-stack.md` 的配套规格，单独定义：

1. HTTP API 的统一格式；
2. 客户端与服务端的交互方式；
3. 认证、授权、错误处理和涉密结果隐藏规则；
4. 服务端启动、关闭、备份和运行模式；
5. 文件型 SQLite MVP 下的运行边界；
6. 未来迁移数据库或拆分服务时的兼容原则。

目标：让 API、客户端 SDK / Tool Proxy、服务端、worker、数据库 adapter 和后续 UI 都围绕同一套契约开发，避免接口格式漂移和后期维护困难。

---

## 1. API 总体风格

### 1.1 基础风格

推荐使用 HTTP + JSON，第一版 API 前缀：

```text
/api/v1
```

原则：

- 所有请求和响应使用 `application/json`，视频文件上传接口除外；
- 所有外部可见 schema 使用 Pydantic v2 定义；
- 不直接暴露 ORM model；
- 所有 API response 都使用统一 envelope；
- 所有错误都使用统一 error envelope；
- 所有接口都接受 `request_id`，若客户端未提供则服务端生成；
- 所有 mutation API 支持 `idempotency_key` 或等价机制。

### 1.2 统一成功响应格式

```json
{
  "ok": true,
  "request_id": "req_01HX...",
  "data": {},
  "meta": {
    "server_time": "2026-06-08T16:00:00+08:00",
    "schema_version": "v1"
  }
}
```

说明：

- `data` 放业务响应；
- `meta` 放分页、schema、服务时间等非业务核心信息；
- `ok=true` 时不返回 `error`。

### 1.3 统一错误响应格式

```json
{
  "ok": false,
  "request_id": "req_01HX...",
  "error": {
    "code": "capability_denied",
    "message": "This operation is not permitted for the current principal.",
    "details": {}
  },
  "meta": {
    "server_time": "2026-06-08T16:00:00+08:00",
    "schema_version": "v1"
  }
}
```

错误规则：

- 接口能力非法：返回明确错误，如 `403 capability_denied`；
- 请求格式错误：返回 `400 validation_error`；
- 身份无效：返回 `401 unauthenticated`；
- 资源不存在或无权访问且属于合法查询范围：尽量表现为不存在，避免泄露；
- 服务内部错误：返回 `500 internal_error`，不暴露堆栈和内部路径。

### 1.4 分页格式

列表接口使用 cursor 分页：

```json
{
  "items": [],
  "page": {
    "limit": 50,
    "next_cursor": "cursor_xxx",
    "has_more": true
  }
}
```

不建议第一版使用 offset 分页作为主接口，避免数据变动时分页不稳定。

---

## 2. 认证与客户端身份携带

### 2.1 客户端职责

客户端 SDK / Tool Proxy 负责：

- 用户注册 / 登录 / token 刷新；
- 在 AI 调用工具时自动附带身份凭证；
- 不把 principal、role、group、policy 放进 query body；
- 将服务端返回的错误转换成 AI 工具可理解的错误；
- 对 locator / playback URL 不做越权缓存。

AI 不需要知道用户身份、角色或权限策略。

### 2.2 请求身份层

推荐 header：

```http
Authorization: Bearer <access_token>
X-Request-Id: req_...
X-Client-Version: cctv-memory-client/0.1.0
```

可选：

```http
X-Session-Id: sess_...
```

请求 body 中不得接受如下字段作为权限依据：

```text
principal_id
user_id
role
group
access_policy
security_level
```

如果业务请求确实需要 user_id 作为查询对象，也必须和当前 principal 权限分开处理，不能作为调用者身份。

### 2.3 注册 / 登录 API

第一版最小 API：

```http
POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
GET  /api/v1/auth/me
```

MVP 可以采用管理员预置用户或 service account；`register` 可先只支持带注册码 / 管理员授权的注册，避免开放注册导致权限失控。

`/auth/me` 返回当前 principal 的非敏感信息和 capability 摘要，但不返回完整策略细节：

```json
{
  "principal_id": "user_001",
  "principal_type": "user",
  "display_name": "Security User",
  "capabilities": [
    "observation.search",
    "observation.read_detail",
    "observation.read_locator"
  ]
}
```

---

## 3. API 分组

### 3.1 Health / Runtime

```http
GET  /api/v1/health
GET  /api/v1/runtime/status
POST /api/v1/runtime/shutdown
```

说明：

- `/health` 用于进程存活检查，不暴露敏感配置；
- `/runtime/status` 返回数据库连接、worker 状态、队列积压等摘要，需要 admin 或 service capability；
- `/runtime/shutdown` 只允许本机 CLI 或 admin token 调用，默认不暴露给 AI-facing client。

### 3.2 Auth

```http
POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
GET  /api/v1/auth/me
```

### 3.3 Video Source / AnalysisJob

```http
POST /api/v1/video-sources/analyze
GET  /api/v1/video-sources/{video_id}
GET  /api/v1/video-sources/{video_id}/records
GET  /api/v1/analysis-jobs/{analysis_job_id}
GET  /api/v1/analysis-jobs
GET  /api/v1/analysis-jobs/{analysis_job_id}/errors
POST /api/v1/analysis-jobs/{analysis_job_id}/rerun
```

能力要求：

- `video-sources/analyze`：`analysis.submit`；
- job 查询：调用者必须有对应 video/camera/location 的读权限，或 admin；
- rerun：`analysis.submit` 或更高 capability；
- active 发布不通过公开 API 暴露，只由 worker 内部调用 application service。

### 3.4 Observation Search

```http
POST /api/v1/observation-search/contexts
POST /api/v1/observation-search/contexts/{context_id}/refine
POST /api/v1/observation-search/contexts/{context_id}/batch-refine
GET  /api/v1/observation-search/contexts/{context_id}/facets
POST /api/v1/observation-search/details
POST /api/v1/observation-search/overlapping-records
DELETE /api/v1/observation-search/contexts/{context_id}
```

对应工具：

```text
start_observation_search
refine_observation_search
batch_refine_observation_search
facet_observation_search
get_observation_details
get_overlapping_records
close_search_context
```

所有 search API 都必须：

1. 验证当前 principal；
2. 验证 capability；
3. 计算 authorized_scope；
4. 在 SQL / FTS / vector 检索前应用权限过滤；
5. 不把无权内容计入结果、facet、candidate_count。

### 3.5 Locator / Playback

第一版优先通过 details 返回 locator：

```http
POST /api/v1/observation-search/details
```

请求参数：

```json
{
  "record_ids": ["obs_001"],
  "include_locator": true
}
```

可选批量接口：

```http
POST /api/v1/observation-search/locators
```

播放接口：

```http
GET /api/v1/playback/{playback_token}
```

规则：

- locator 不是独立实体，只是 ObservationRecord + VideoSource 派生视图；
- 每次生成 locator 都要二次鉴权；
- playback token 必须短 TTL；
- 不直接暴露内部文件路径；
- 播放/下载行为进入审计。

### 3.6 Admin / Policy（非 MVP UI，但服务端可预留内部 API）

```http
GET  /api/v1/admin/principals
POST /api/v1/admin/principals
GET  /api/v1/admin/access-policies
POST /api/v1/admin/access-policies
PATCH /api/v1/admin/camera-locations/{location_id}/policy
PATCH /api/v1/admin/camera-devices/{camera_id}/policy
```

第一版可以不做完整 UI；可通过 CLI 或 seed config 初始化 principal / policy。

---

## 4. 核心请求/响应草案

### 4.1 SubmitVideoSourceRequest

```json
{
  "source_type": "file",
  "source_uri": "/data/videos/lobby_20260606_210000.mp4",
  "camera_id": "cam_lobby_01",
  "video_start_time": "2026-06-06T21:00:00+08:00",
  "external_source_id": "nvr-export-001",
  "idempotency_key": "nvr-export-001",
  "analysis_options": {
    "enable_default_segment": true,
    "enable_motion_triggered_high_freq": true
  }
}
```

注意：身份凭证在 header，不在 body。

### 4.2 StartObservationSearchRequest

```json
{
  "query_text": "穿深色衣服并背包的人是否在门口徘徊",
  "time_range": {
    "start": "2026-06-06T21:00:00+08:00",
    "end": "2026-06-06T22:00:00+08:00"
  },
  "camera_ids": ["cam_lobby_01"],
  "location_ids": [],
  "tag_filters": ["dark_clothing", "backpack"],
  "preferred_text_fields": ["static", "dynamic"],
  "preferred_analysis_scales": ["high_freq_event", "default_segment"],
  "search_mode": "hybrid",
  "top_k": 50,
  "score_threshold": null
}
```

响应：

```json
{
  "context_id": "ctx_001",
  "revision_id": "rev_001",
  "candidate_count": 18,
  "facets": {
    "top_tags": [{"tag": "backpack", "count": 9}],
    "camera_distribution": [{"camera_id": "cam_lobby_01", "count": 18}],
    "analysis_scale_distribution": [{"analysis_scale": "default_segment", "count": 15}]
  },
  "results": [
    {
      "record_id": "obs_001",
      "rank": 1,
      "score": 0.82,
      "score_detail": {
        "static_rank": 2,
        "dynamic_rank": 4,
        "tag_boost": 0.1,
        "scale_boost": 0.05
      },
      "preview_text": "大厅入口附近有一名穿深色衣服并背包的人短暂停留。"
    }
  ]
}
```

### 4.3 RefineObservationSearchRequest

```json
{
  "base_revision_id": "rev_001",
  "op": "search_dynamic_text",
  "params": {
    "query_text": "在门口徘徊或反复停留",
    "top_k": 30
  }
}
```

### 4.4 ObservationDetailsRequest

```json
{
  "record_ids": ["obs_001", "obs_002"],
  "include_locator": true
}
```

响应中的 locator projection：

```json
{
  "record_id": "obs_001",
  "static_description_text": "大厅入口附近有一名穿深色衣服、背双肩包的人。",
  "dynamic_description_text": "该人员在门口附近短暂停留后向走廊方向移动。",
  "tags": ["person", "dark_clothing", "backpack", "loitering"],
  "analysis_scale": "default_segment",
  "locator": {
    "video_id": "video_001",
    "camera_id": "cam_lobby_01",
    "segment_start_ms": 120000,
    "segment_end_ms": 135000,
    "absolute_start_time": "2026-06-06T21:02:00+08:00",
    "absolute_end_time": "2026-06-06T21:02:15+08:00",
    "playback_url": "/api/v1/playback/pbt_abc",
    "thumbnail_url": "/api/v1/playback/thumb_tn_abc"
  }
}
```

### 4.5 OverlappingRecordsRequest

```json
{
  "record_id": "obs_high_001",
  "analysis_scale_filter": ["default_segment"],
  "time_padding_ms": 0,
  "top_k": 20
}
```

返回：与 search result item 类似，但只包含授权范围内记录。

---

## 5. 服务运行模式

### 5.1 进程角色

第一版推荐 3 类进程角色，但可以由同一个 CLI 管理：

```text
server      FastAPI HTTP 服务，处理客户端/API/search/auth/locator
worker      AnalysisJob / VLM / indexing worker
maintenance 备份、清理过期 SearchContext、重建索引等维护任务
```

本地 MVP 可以单进程启动 server，并在同进程内启动轻量 worker；正式一点的部署应拆成 server + worker 两个进程。

### 5.2 CLI 设计草案

建议提供统一 CLI：

```bash
cctv-memory init --data-dir ./data
cctv-memory serve --data-dir ./data --host 127.0.0.1 --port 8080
cctv-memory worker --data-dir ./data --concurrency 1
cctv-memory stop --data-dir ./data
cctv-memory status --data-dir ./data
cctv-memory backup --data-dir ./data --out ./backup.tar.zst
cctv-memory restore --in ./backup.tar.zst --data-dir ./data
cctv-memory reindex --data-dir ./data
```

这些是产品层面的接口草案，最终命令名称可在实现阶段确认。

### 5.3 启动流程

`serve` 启动时：

1. 读取配置；
2. 打开 SQLite；
3. 启用 WAL；
4. 检查 schema migration；
5. 检查 VIDEO_ROOT；
6. 初始化 Auth / Repository / Index adapters；
7. 启动 FastAPI；
8. 如配置允许，启动内嵌 worker。

`worker` 启动时：

1. 读取同一 data-dir；
2. 连接 SQLite；
3. claim queued task；
4. 执行视频处理、VLM、发布、索引；
5. 定期刷新 worker lease；
6. 失败时写 error_code / retry_count / next_run_at。

### 5.4 关闭流程

`stop` 或收到 SIGTERM 时：

1. server 停止接受新请求；
2. 等待正在处理的非长任务完成；
3. worker 不再 claim 新任务；
4. 正在执行的 VLM 请求不强制中断，完成后写入状态；
5. checkpoint SQLite WAL；
6. 退出。

MVP 不要求中途取消已发出的 VLM 请求；取消能力未来单独设计。

### 5.5 备份与上传下载

SQLite 文件运行中不能简单粗暴复制。推荐：

- 使用 SQLite backup API；或
- 停止写入后执行 checkpoint，再打包；或
- 通过 `cctv-memory backup` 统一导出。

备份包建议包含：

```text
cctv_memory.sqlite3
videos/              可选，可能很大
frames/              可选，可重建
artifacts/           可选
manifest.json
```

`manifest.json` 包含：

```text
schema_version
created_at
app_version
database_engine
included_paths
checksum
```

---

## 6. 客户端-服务端交互方式

### 6.1 Client / Tool Proxy 职责

客户端可以是 Python SDK、CLI、Web UI 后端，或 AI tool proxy。

职责：

- 保存服务端地址；
- 管理登录 token / session；
- 自动为 AI 工具请求加 Authorization header；
- 暴露稳定工具函数给 AI；
- 将服务端 error code 映射为工具错误；
- 不自行做权限判断；
- 不缓存无 TTL 的 locator/playback URL。

### 6.2 AI 与客户端的交互方式

AI 与客户端之间建议同时保留两种方式：CLI 和本地 HTTP tool proxy。

#### 6.2.1 CLI 方式

CLI 适合：

- 本地调试；
- 脚本批处理；
- 没有常驻客户端进程的简单部署；
- 人工运维和问题复现。

示例：

```bash
cctv-memory-client search start \
  --server http://127.0.0.1:8080 \
  --query "穿深色衣服并背包的人是否在门口徘徊" \
  --camera cam_lobby_01 \
  --start 2026-06-06T21:00:00+08:00 \
  --end 2026-06-06T22:00:00+08:00
```

CLI 从本地 client config / keychain / token file 读取凭证，然后向服务端 HTTP/HTTPS API 发请求。AI 如果通过 shell/tool 调 CLI，不需要也不应该手工拼 Authorization header。

#### 6.2.2 本地 HTTP Tool Proxy 方式

本地 HTTP tool proxy 适合：

- OpenClaw / 其它 Agent runtime 以 HTTP 工具方式调用；
- 需要长会话、自动 token refresh、统一错误映射；
- 未来 Web UI 或桌面客户端复用同一套本地代理。

推荐本地监听：

```text
http://127.0.0.1:<local_port>/tools/...
```

AI 调用本地 tool proxy：

```text
AI -> local client/tool proxy -> CCTV-Memory Server
```

本地 proxy 自动：

- 附加 Authorization；
- 注入 X-Request-Id；
- 处理 token refresh；
- 将服务端 envelope 转成 AI 工具返回；
- 隐藏 principal/role/policy 细节。

#### 6.2.3 两种方式的共同规则

- CLI 和 HTTP tool proxy 使用同一套 client library；
- 二者对外暴露相同的工具语义；
- 二者都不让 AI 直接管理身份字段；
- 二者都只通过服务端 HTTP/HTTPS API 访问数据；
- 不允许客户端绕过服务端直接读写 SQLite 数据库文件。

### 6.3 AI 工具视角

AI 看到的工具应尽量简单：

```text
submit_video_source
start_observation_search
refine_observation_search
facet_observation_search
get_observation_details
get_overlapping_records
close_search_context
```

AI 不需要传 `principal_id`，也不需要知道用户能看哪些区域。

如果服务端返回空结果，AI 应回答：

```text
在你可访问范围内没有找到匹配记录。
```

而不是推测是否存在无权内容。

### 6.4 非法接口与合法空结果

- AI 调用了当前 principal 没有 capability 的接口：客户端收到服务端 403，工具返回权限错误。
- AI 调用了合法搜索接口，但涉密结果被过滤：服务端返回空结果或仅返回授权结果，客户端不附加“有结果但被过滤”的提示。

---

## 7. 未来兼容原则

1. API version 使用 `/api/v1`，破坏性变更进入 `/api/v2`。
2. 所有 request/response schema 带文档化字段，不依赖 ORM。
3. 客户端只依赖 API contract，不依赖 SQLite/PostgreSQL 实现。
4. 数据库迁移只影响 server infrastructure，不影响客户端。
5. 搜索策略新增字段应向后兼容：未知字段服务端应拒绝或忽略，策略需明确。
6. 文件型 MVP 到服务化部署的迁移路径：SQLite -> PostgreSQL/pgvector -> OpenSearch 可选。

---

## 8. 当前建议结论

第一版应提供：

```text
FastAPI server
SQLite-backed repository / index / task table
client SDK / tool proxy 自动带 token
统一 API envelope
只读 AI-facing search API
可选内嵌 worker + 独立 worker 模式
backup / restore / reindex CLI
```

这能同时满足：

- 本地文件型数据库，方便上传下载；
- 客户端-服务端清晰隔离；
- AI 不感知身份权限；
- 服务端硬性鉴权与权限过滤；
- 后续迁移 PostgreSQL/OpenSearch 时客户端和 application/domain/contracts 尽量不改。
