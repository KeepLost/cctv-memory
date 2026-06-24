# Error Code Contract（统一错误码契约）

## 0. 文档目的

本文定义 API、CLI、tool proxy、worker 与 repository adapter 使用的统一错误码。实现不得随意发明同义错误码。

错误响应统一 envelope 见 `api-and-service-runtime-design.md`。

---

## 1. Error Shape

```json
{
  "code": "validation_error",
  "message": "Human readable but non-sensitive message.",
  "details": {},
  "retryable": false
}
```

规则：

- `message` 不暴露内部路径、SQL、token、source_uri、stack trace；
- `details` 面向客户端调试，AI-facing 工具默认只显示必要字段；
- 安全相关错误必须审计。

---

## 2. Auth / Permission

| HTTP | code | retryable | 暴露给 AI | 审计 | 语义 |
|---|---|---:|---:|---:|---|
| 401 | unauthenticated | false | yes | yes | token/session 缺失或无效 |
| 403 | capability_denied | false | yes | yes | 当前 principal 没有调用该接口的能力 |
| 404 | not_found | false | yes | optional | 资源不存在，或合法隐藏无权资源 |
| 403 | account_disabled | false | yes | yes | principal 非 active |

禁止返回：

```text
resource_exists_but_forbidden
```

因为这会泄露资源存在性。

---

## 3. Validation / Conflict

| HTTP | code | retryable | 语义 |
|---|---|---:|---|
| 400 | validation_error | false | request/schema/field validation failed |
| 409 | conflict | false | unique constraint 或状态冲突 |
| 409 | idempotency_conflict | false | 同一 idempotency_key 对应不同 payload |
| 409 | invalid_state_transition | false | 状态机非法迁移 |
| 413 | payload_too_large | false | 文件/请求过大 |
| 429 | rate_limited | true | 限流 |
| 400 | limit_exceeded | false | top_k/context/revision/page size 超限 |

---

## 4. Video / VLM / Pipeline

| HTTP/Worker | code | retryable | 语义 |
|---|---|---:|---|
| 422 | video_metadata_missing | false | 缺少 camera_id/video_start_time |
| 422 | video_decode_error | false/true | 视频无法解码；临时 IO 可重试 |
| worker | frame_extraction_failed | true | 抽帧失败 |
| worker | insufficient_frames | false | 抽帧零可用帧（near-EOF/越界窗口）；unit 记 skipped，非失败 |
| worker | orphan_timeout | false | unit 卡 running 超过 stale cutoff，被有界孤儿回收落 failed |
| worker | vlm_provider_error | true | provider 临时错误（超时/传输/5xx/429/冷启动）；unit 层做有界退避重试 |
| worker | vlm_rate_limited | true | provider 限流 |
| worker | vlm_schema_validation_failed | false | VLM 输出不符合 schema |
| worker | prompt_version_missing | false | prompt version 未注册 |
| worker | analysis_unit_failed | true/false | 单分析单元失败 |

---

## 5. Storage / Database / Index

| HTTP/Worker | code | retryable | 语义 |
|---|---|---:|---|
| 503 | storage_unavailable | true | DB/文件存储不可用 |
| 503 | retryable_storage_error | true | lock timeout / transient DB error |
| 500 | storage_corruption_detected | false | schema/data corruption |
| 500 | migration_required | false | DB schema 不兼容 |
| worker | index_update_failed | true | index update failed after fact publication |
| worker | index_rebuild_required | true | index stale or corrupted |
| 500 | backup_failed | true/false | 备份失败 |
| 500 | restore_failed | false | 恢复失败 |

---

## 6. Runtime / Internal

| HTTP | code | retryable | 语义 |
|---|---|---:|---|
| 503 | service_unavailable | true | service shutting down / not ready |
| 500 | internal_error | false | 未分类内部错误 |
| 501 | not_implemented | false | 明确未实现功能 |
| 503 | dependency_unavailable | true | 外部依赖不可用 |

`internal_error` 必须记录服务端日志，但响应不得暴露 stack trace。

---

## 7. Adapter Error Mapping

```text
unique_violation -> conflict or idempotency_conflict
foreign_key_violation -> validation_error or conflict
not_found -> not_found
permission/capability failure -> capability_denied
connection_error -> storage_unavailable
lock_timeout -> retryable_storage_error
```

---

## 8. Contract Tests

```text
forbidden_record_details_returns_not_found
capability_missing_returns_capability_denied
idempotency_payload_mismatch_returns_idempotency_conflict
invalid_job_transition_returns_invalid_state_transition
vlm_bad_json_returns_vlm_schema_validation_failed
sqlite_lock_timeout_maps_retryable_storage_error
internal_error_hides_stack_trace
```

> 单元级瞬时重试（任务 cctv-memory-20260615-1447）：`vlm_provider_error`（retryable=true）在
> per-unit runner 内做有界退避+抖动重试，每次尝试经全局 VlmScheduler；永久错误
> （`vlm_schema_validation_failed` / `frame_extraction_failed` / `insufficient_frames`）不重试。
> 重试预算耗尽后 unit 落 `failed(vlm_provider_error)`。终态 DB 写入遇到瞬时锁按
> `retryable_storage_error` 语义做有界重试，耗尽即抛错（由孤儿回收兜底，不引入 `recoverable_running`）。
> 相关测试：`tests/integration/test_unit_retry_state_hardening.py`。
