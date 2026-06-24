# API Routes（接口路由表）

## 0. 文档目的

快速查阅所有 HTTP 端点的 method、path、所需 capability 和对应 schema。详细 request/response 格式见 `schema-contracts.md` 和 `api-and-service-runtime-design.md`。

> 实现状态图例：✅=已实现并经测试（OpenAPI 契约完整，见 tests/architecture/test_api_contract.py
> 路由快照）；🟡=已设计未实现（仅文档/设计，主路径尚无代码）。本仓库=服务端；下列 ✅ 路由由
> `cctv_memory/api/app.py` 提供，身份经 `AuthVerifierPort`（dev 信任实现读 `X-Principal-Id`）解析，
> 请求体/响应 envelope 均已契约化进 `/openapi.json`。🟡 路由（auth/runtime/admin-principals 等）
> 是未来客户端 + 生产 token 鉴权落地时的配套，当前未实现。

---

## 1. Health / Runtime

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| GET | `/api/v1/health` | 无需认证 | ✅ | 存活检查 |
| GET | `/api/v1/runtime/status` | `runtime.manage` | 🟡 | 服务状态摘要（未实现） |
| POST | `/api/v1/runtime/shutdown` | `runtime.manage` + 本机 | 🟡 | 优雅关闭（未实现） |

---

## 2. Auth（🟡 全部未实现 — 配套未来客户端 + 生产 token 鉴权）

当前服务端身份解析经 `AuthVerifierPort`，MVP 用 dev 信任实现（读 `X-Principal-Id` 头或默认 principal，
无 token 校验）。下表为设计中的认证 API，待生产 token verifier 落地时实现；届时在
`AuthVerifierPort` 处插入校验逻辑，api/application/domain 其余层无需改动。

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| POST | `/api/v1/auth/register` | 需注册码或 admin 授权 | 🟡 | 创建 principal |
| POST | `/api/v1/auth/login` | 无需认证 | 🟡 | 获取 token |
| POST | `/api/v1/auth/refresh` | valid refresh token | 🟡 | 刷新 access token |
| POST | `/api/v1/auth/logout` | authenticated | 🟡 | 注销 session |
| GET | `/api/v1/auth/me` | authenticated | 🟡 | 当前 principal + capabilities |

---

## 3. Video Source / Analysis Job

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| POST | `/api/v1/video-sources/analyze` | `analysis.submit` | ✅ | 提交视频源并创建分析任务 |
| GET | `/api/v1/video-sources/{video_id}` | authorized read | 🟡 | 查询视频源详情（未实现） |
| GET | `/api/v1/video-sources/{video_id}/records` | `observation.search` | 🟡 | 该视频的 active 记录（未实现） |
| GET | `/api/v1/analysis-jobs` | `analysis.submit` or admin | 🟡 | 列表查询分析任务（未实现） |
| GET | `/api/v1/analysis-jobs/{job_id}` | `analysis.submit` or admin | ✅ | 查询单个任务含子任务状态 |
| GET | `/api/v1/analysis-jobs/{job_id}/errors` | `analysis.submit` or admin | 🟡 | 失败详情（未实现） |
| POST | `/api/v1/analysis-jobs/{job_id}/rerun` | `analysis.rerun` | 🟡 | 重跑失败片段（未实现） |

---

## 4. Observation Search

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| POST | `/api/v1/observation-search/contexts` | `observation.search` | ✅ | 创建 SearchContext + 初始检索 |
| POST | `/api/v1/observation-search/contexts/{ctx}/refine` | `observation.search` | ✅ | 单轮 refine |
| POST | `/api/v1/observation-search/contexts/{ctx}/batch-refine` | `observation.search` | ✅ | 多策略并行 refine |
| GET | `/api/v1/observation-search/contexts/{ctx}/facets` | `observation.search` | ✅ | 候选集统计 |
| POST | `/api/v1/observation-search/details` | `observation.read_detail` | ✅ | 获取记录详情 |
| POST | `/api/v1/observation-search/overlapping-records` | `observation.read_detail` | ✅ | 时间交叉记录 |
| DELETE | `/api/v1/observation-search/contexts/{ctx}` | `observation.search` | ✅ | 关闭上下文 |

---

## 5. Locator / Playback

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| POST | `/api/v1/observation-search/locators` | `observation.read_locator` | ✅ | 批量获取 locator projection |
| GET | `/api/v1/playback/{token}` | token 验证 | ✅ | 视频片段播放/下载 |

Locator 也可通过 details 接口的 `include_locator=true` 获取。

---

## 6. Admin / Policy（🟡 MVP 可选，全部未实现）

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| GET | `/api/v1/admin/principals` | `user.manage` | 🟡 | 列表 principal |
| POST | `/api/v1/admin/principals` | `user.manage` | 🟡 | 创建 principal |
| GET | `/api/v1/admin/access-policies` | `policy.manage` | 🟡 | 列表策略 |
| POST | `/api/v1/admin/access-policies` | `policy.manage` | 🟡 | 创建/更新策略 |
| PATCH | `/api/v1/admin/camera-locations/{id}/policy` | `policy.manage` | 🟡 | 修改位置策略 |
| PATCH | `/api/v1/admin/camera-devices/{id}/policy` | `policy.manage` | 🟡 | 修改设备策略 |

---

## 6b. Backup / Export（C6 新增 — 实现于 cctv-memory-20260609-2327-c2-c6-overnight）

| Method | Path | Capability | 状态 | 说明 |
|--------|------|-----------|------|------|
| POST | `/api/v1/admin/backups` | `runtime.manage` | ✅ | 管理员完整备份(body: out_path) |
| POST | `/api/v1/exports/user` | `observation.search` | ✅ | 用户授权范围导出(仅授权记录, 不含 DB 文件/source_uri) |
| POST | `/api/v1/exports/migration` | `runtime.manage` | ✅ | 迁移导出(contract DTO 行, 不含 DB 文件/source_uri) |

新增原因(CONTEXT_MANIFEST §5 流程): `backup-export-contract.md` §1.2/§1.3 已定义
`user_authorized_export` / `migration_export` 语义, 但 `api-routes.md` 此前只列出 CLI 等价能力,
未列 HTTP 路由。本次为达成 HTTP parity 显式补充, 不削弱任何权限边界: user 导出按 AuthorizedScope
过滤(无权记录不出现), 绝不含完整 SQLite 文件或内部 `source_uri`; migration 导出需 `runtime.manage`。
响应沿用统一 envelope。

---

## 7. 通用规则

- 所有响应使用统一 envelope（见 `schema-contracts.md` §1.4）
- 所有错误使用统一 error 格式（见 `error-code-contract.md`）
- `X-Request-Id` 贯穿请求链路
- 列表接口默认 cursor 分页
- mutation 接口支持 `idempotency_key`
