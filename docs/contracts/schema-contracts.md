# Schema Contracts（CCTV Memory 数据格式契约）

## 0. 文档目的

本文定义 CCTV Memory 第一版主要跨模块数据格式。所有 API、pipeline、VLM、index、auth、search、audit、backup 相关数据都应使用显式 schema，不允许在模块间传递无约束裸 dict。

约定：

- schema 使用 Pydantic v2 实现；
- 时间字段使用 ISO-8601 字符串或 Python aware datetime；
- ID 字段使用字符串，具体生成策略由实现决定；
- 跨进程/队列/持久化消息必须带 `schema_version`；
- API request 不携带 principal/role/policy 作为调用者身份；身份来自 token/session；
- 下面是逻辑契约，不是最终 Python 代码。

---

## 1. 通用类型

### 1.1 ID 类型

```text
camera_id: string
location_id: string
video_id: string
analysis_job_id: string
scale_task_id: string
trigger_id: string
record_id: string
history_id: string
context_id: string
revision_id: string
principal_id: string
access_policy_id: string
request_id: string
```

### 1.2 时间范围

```json
{
  "start": "2026-06-06T21:00:00+08:00",
  "end": "2026-06-06T22:00:00+08:00"
}
```

规则：

- `start < end`；
- 必须带 timezone；
- 如果用户输入无 timezone，客户端或服务端必须按配置 timezone 规范化。

### 1.3 分页

```json
{
  "limit": 50,
  "cursor": "cursor_xxx"
}
```

响应：

```json
{
  "limit": 50,
  "next_cursor": "cursor_xxx",
  "has_more": true
}
```

### 1.4 API Envelope

成功：

```json
{
  "ok": true,
  "request_id": "req_xxx",
  "data": {},
  "meta": {"schema_version": "v1", "server_time": "..."}
}
```

失败：

```json
{
  "ok": false,
  "request_id": "req_xxx",
  "error": {"code": "validation_error", "message": "...", "details": {}},
  "meta": {"schema_version": "v1", "server_time": "..."}
}
```

---

## 2. 枚举

### 2.1 SourceType

```text
file
rtsp_chunk
object_storage
external
```

### 2.2 AnalysisScale

```text
default_segment
motion_scan
high_freq_event
low_freq_summary
```

### 2.3 JobStatus

```text
queued
running
succeeded
partial_failed
failed
cancelled
```

MVP 不主动取消已发出的 VLM 请求；`cancelled` 可预留。

### 2.4 TaskStatus

```text
pending
running
succeeded
partial_failed
failed
skipped
```

### 2.5 SearchMode

```text
static_attribute
dynamic_event
hybrid
auto_by_external_ai
```

### 2.6 ContextMode

```text
snapshot
stream
```

MVP 默认只实现 `snapshot`。

### 2.7 PrincipalType

```text
user
service_account
admin
```

### 2.8 SecurityLevel

```text
public
internal
confidential
restricted
```

实现可配置排序，但必须全局一致。

---

## 3. Domain Schemas

### 3.1 CameraLocation

```json
{
  "location_id": "loc_lobby_01",
  "building": "T1",
  "floor": "1F",
  "area": "lobby",
  "room_or_zone": "entrance",
  "location_desc": "一楼大厅入口靠近闸机处",
  "access_policy_id": "policy_public_area",
  "security_level": "internal",
  "created_at": "...",
  "updated_at": "..."
}
```

### 3.2 CameraDevice

```json
{
  "camera_id": "cam_lobby_01",
  "camera_name": "大厅入口摄像头",
  "location_id": "loc_lobby_01",
  "manufacturer": null,
  "model": null,
  "serial_number": null,
  "install_position_desc": "大厅入口上方朝向电梯",
  "stream_uri": "rtsp://...",
  "access_policy_id": null,
  "status": "active",
  "created_at": "...",
  "updated_at": "..."
}
```

### 3.3 VideoSource

```json
{
  "video_id": "video_001",
  "source_type": "file",
  "source_uri": "/data/videos/lobby.mp4",
  "original_source_uri": null,
  "camera_id": "cam_lobby_01",
  "video_start_time": "2026-06-06T21:00:00+08:00",
  "video_end_time": "2026-06-06T22:00:00+08:00",
  "duration_ms": 3600000,
  "source_status": "ready",
  "external_source_id": "nvr-export-001",
  "access_policy_id": "policy_public_area",
  "created_at": "...",
  "updated_at": "..."
}
```

### 3.4 AnalysisJob

```json
{
  "analysis_job_id": "job_001",
  "video_id": "video_001",
  "job_status": "queued",
  "idempotency_key": "nvr-export-001",
  "analysis_options": {
    "enable_default_segment": true,
    "enable_motion_triggered_high_freq": true
  },
  "model_version": "vlm-x",
  "prompt_version": "prompt-v1",
  "pipeline_version": "pipeline-v1",
  "created_record_ids": [],
  "updated_record_ids": [],
  "archived_record_ids": [],
  "failed_segment_ids": [],
  "created_at": "...",
  "started_at": null,
  "finished_at": null,
  "error_code": null,
  "error_message": null
}
```

### 3.5 AnalysisScaleTask

```json
{
  "scale_task_id": "scale_001",
  "analysis_job_id": "job_001",
  "analysis_scale": "default_segment",
  "status": "pending",
  "total_units": 0,
  "succeeded_units": 0,
  "failed_units": 0,
  "skipped_reason": null,
  "created_at": "...",
  "started_at": null,
  "finished_at": null,
  "error_code": null,
  "error_message": null
}
```

### 3.6 HighFreqTrigger

```json
{
  "trigger_id": "trigger_001",
  "analysis_job_id": "job_001",
  "scale_task_id": "scale_high_001",
  "video_id": "video_001",
  "trigger_start_ms": 120000,
  "trigger_end_ms": 130000,
  "motion_score": 0.8,
  "change_score": 0.7,
  "trigger_reason": "motion_spike",
  "status": "pending",
  "idempotency_key": "job_001:video_001:120000:130000:motion_spike",
  "created_at": "...",
  "updated_at": "...",
  "error_code": null,
  "error_message": null
}
```

### 3.7 ObservationRecord

```json
{
  "record_id": "obs_001",
  "video_id": "video_001",
  "analysis_job_id": "job_001",
  "analysis_scale": "default_segment",
  "segment_start_ms": 120000,
  "segment_end_ms": 135000,
  "observed_start_time": "2026-06-06T21:02:00+08:00",
  "observed_end_time": "2026-06-06T21:02:15+08:00",
  "camera_id": "cam_lobby_01",
  "location_id": "loc_lobby_01",
  "static_description_text": "大厅入口附近有一名穿深色衣服、背双肩包的人。",
  "dynamic_description_text": "该人员在门口附近短暂停留后向走廊方向移动。",
  "tags": ["person", "dark_clothing", "backpack", "loitering"],
  "clip_uri": null,
  "thumbnail_uri": null,
  "attributes": {},
  "access_policy_id": "policy_public_area",
  "security_level": "internal",
  "model_version": "vlm-x",
  "prompt_version": "prompt-v1",
  "pipeline_version": "pipeline-v1",
  "created_at": "...",
  "updated_at": "..."
}
```

规则：

- `segment_start_ms < segment_end_ms`；
- `observed_start_time/observed_end_time/camera_id/location_id/access_policy_id/security_level` 由系统派生，不由 VLM 决定；
- `observed_start_time/observed_end_time/camera_id/location_id` 是 MVP 必填冗余字段，用于权限过滤、facet 和索引 metadata；
- active record 唯一键为 `(video_id, segment_start_ms, segment_end_ms, analysis_scale)`。
- `attributes` 是产品语义里的 `attr`。Detector-gated VLM 中，detector-only 记录必须写入
  `attributes.detector_gate`，同时 `static_description_text=""`、`dynamic_description_text=""`、`tags=[]`；
  detector label 不得写入自然语言字段或 tags。

### 3.7.1 DetectorGateLog

```json
{
  "gate_log_id": "gate_001",
  "analysis_job_id": "job_001",
  "scale_task_id": "scale_default_001",
  "unit_id": "unit_001",
  "video_id": "video_001",
  "analysis_scale": "default_segment",
  "segment_start_ms": 120000,
  "segment_end_ms": 132000,
  "provider": "mock",
  "model_id": "mock-detector-v1",
  "status": "succeeded",
  "decision": {
    "triggered_vlm": false,
    "matched_rules": [],
    "positive_frame_ratio_by_label": {"person": 0.0},
    "evidence_hash": "sha256:...",
    "rule_config_hash": "sha256:..."
  },
  "frame_evidence": [
    {
      "frame_index": 1,
      "timestamp_ms": 120500,
      "uri_basename": "frame_001.jpg",
      "frame_hash": "sha256:...",
      "detections": []
    }
  ],
  "created_at": "..."
}
```

规则：生产默认不得保存 image bytes/base64、`source_uri` 或绝对帧路径；只保存 basename、hash、时间戳、bbox/label/confidence 等检测 metadata。

### 3.8 ObservationRecordHistory

复用 ObservationRecord 主字段，额外字段：

```json
{
  "history_id": "hist_001",
  "old_record_id": "obs_001",
  "replaced_by_record_id": "obs_002",
  "archived_by_analysis_job_id": "job_002",
  "archived_at": "...",
  "archive_reason": "rerun"
}
```

---

## 4. Auth Schemas

### 4.1 Principal

```json
{
  "principal_id": "user_001",
  "principal_type": "user",
  "tenant_id": "tenant_default",
  "external_subject_id": null,
  "display_name": "Security User",
  "status": "active",
  "roles": ["security_viewer"],
  "groups": ["security_team"]
}
```

### 4.2 AccessPolicy

```json
{
  "access_policy_id": "policy_lab_confidential",
  "tenant_id": "tenant_default",
  "name": "研发实验室机密策略",
  "security_level": "confidential",
  "rules": {
    "allowed_roles": ["security_admin", "lab_manager"],
    "allowed_groups": ["lab_team"]
  },
  "created_at": "...",
  "updated_at": "..."
}
```

### 4.3 AuthorizedScope

```json
{
  "tenant_id": "tenant_default",
  "principal_id": "user_001",
  "allowed_camera_ids": ["cam_lobby_01"],
  "allowed_location_ids": ["loc_lobby_01"],
  "allowed_access_policy_ids": ["policy_public_area"],
  "max_security_level": "internal",
  "capabilities": ["observation.search", "observation.read_detail"],
  "scope_hash": "scope_hash_abc"
}
```

---

## 5. API Schemas

### 5.1 SubmitVideoSourceRequest

```json
{
  "source_type": "file",
  "source_uri": "/data/videos/lobby.mp4",
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

### 5.2 SubmitVideoSourceResponse

```json
{
  "video_id": "video_001",
  "source_status": "ready",
  "analysis_job_id": "job_001",
  "accepted": true
}
```

### 5.3 StartObservationSearchRequest

```json
{
  "query_text": "穿深色衣服并背包的人是否在门口徘徊",
  "time_range": {"start": "2026-06-06T21:00:00+08:00", "end": "2026-06-06T22:00:00+08:00"},
  "camera_ids": ["cam_lobby_01"],
  "location_ids": [],
  "video_ids": [],
  "tag_filters": ["dark_clothing", "backpack"],
  "preferred_text_fields": ["static", "dynamic"],
  "analysis_scale_filter": [],
  "preferred_analysis_scales": ["high_freq_event", "default_segment"],
  "scale_strategy": "prefer_high_freq",
  "search_mode": "hybrid",
  "top_k": 50,
  "score_threshold": null
}
```

规则：

- `video_ids`、`camera_ids`、`location_ids` 是结构化过滤字段；
- `analysis_scale_filter` 是硬过滤；
- `preferred_analysis_scales` 是排序/boost 偏好，不等于硬过滤；
- `scale_strategy` 可选，MVP 推荐值：`prefer_default_segment` / `prefer_high_freq` / `balanced`；
- 空过滤数组表示该请求不按该字段缩小搜索范围；权限范围仍由 AuthorizedScope 强制决定。

### 5.4 SearchResultItem

```json
{
  "record_id": "obs_001",
  "rank": 1,
  "score": 0.82,
  "score_detail": {
    "static_score": 0.7,
    "dynamic_score": 0.6,
    "static_rank": 2,
    "dynamic_rank": 4,
    "tag_boost": 0.1,
    "analysis_scale_boost": 0.05,
    "rrf_score": 0.82
  },
  "preview_text": "大厅入口附近有一名穿深色衣服并背包的人短暂停留。",
  "analysis_scale": "default_segment",
  "observed_start_time": "2026-06-06T21:02:00+08:00",
  "observed_end_time": "2026-06-06T21:02:15+08:00"
}
```

### 5.5 StartObservationSearchResponse

```json
{
  "context_id": "ctx_001",
  "revision_id": "rev_001",
  "candidate_count": 18,
  "facets": {},
  "results": []
}
```

### 5.6 RefineObservationSearchRequest

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

### 5.7 ObservationDetailsRequest

```json
{
  "record_ids": ["obs_001"],
  "include_locator": true
}
```

### 5.8 ObservationDetailsItem

```json
{
  "record_id": "obs_001",
  "static_description_text": "...",
  "dynamic_description_text": "...",
  "tags": [],
  "attributes": {},
  "analysis_scale": "default_segment",
  "locator": null
}
```

### 5.9 LocatorProjection

```json
{
  "video_id": "video_001",
  "camera_id": "cam_lobby_01",
  "segment_start_ms": 120000,
  "segment_end_ms": 135000,
  "absolute_start_time": "2026-06-06T21:02:00+08:00",
  "absolute_end_time": "2026-06-06T21:02:15+08:00",
  "playback_url": "/api/v1/playback/pbt_abc",
  "thumbnail_url": "/api/v1/playback/thumb_tn_abc",
  "expires_at": "2026-06-06T21:12:00+08:00"
}
```

---

## 6. Pipeline Message Schemas

### 6.1 AnalyzeVideoMessage

```json
{
  "schema_version": "v1",
  "message_id": "msg_001",
  "analysis_job_id": "job_001",
  "video_id": "video_001",
  "source_uri": "/data/videos/lobby.mp4",
  "camera_id": "cam_lobby_01",
  "video_start_time": "2026-06-06T21:00:00+08:00",
  "analysis_options": {}
}
```

### 6.2 AnalyzeScaleTaskMessage

```json
{
  "schema_version": "v1",
  "message_id": "msg_002",
  "analysis_job_id": "job_001",
  "scale_task_id": "scale_001",
  "video_id": "video_001",
  "analysis_scale": "default_segment"
}
```

### 6.3 VlmSegmentRequest

```json
{
  "schema_version": "v1",
  "request_id": "vlm_req_001",
  "analysis_job_id": "job_001",
  "video_id": "video_001",
  "camera_id": "cam_lobby_01",
  "analysis_scale": "default_segment",
  "segment_start_ms": 120000,
  "segment_end_ms": 135000,
  "frame_uris": ["frames/video_001/0001.jpg"],
  "prompt_version": "prompt-v1",
  "model_version": "vlm-x",
  "tag_vocabulary_hints": ["person", "backpack", "dark_clothing"]
}
```

### 6.4 VlmSegmentResult

```json
{
  "schema_version": "v1",
  "request_id": "vlm_req_001",
  "analysis_job_id": "job_001",
  "video_id": "video_001",
  "analysis_scale": "default_segment",
  "segment_start_ms": 120000,
  "segment_end_ms": 135000,
  "vlm_output": {},
  "status": "succeeded",
  "error_code": null,
  "error_message": null
}
```

### 6.5 PublishObservationRecordsCommand

```json
{
  "schema_version": "v1",
  "command_id": "pub_001",
  "analysis_job_id": "job_001",
  "records": []
}
```

---

## 7. VLM Output Schema

### 7.1 VlmObservationOutput

精简格式（任务 cctv-memory-20260611-2214）：

```json
{
  "static": "大厅入口附近有一名穿深色衣服、背双肩包的人。",
  "dynamic": "该人员在门口附近短暂停留后向走廊方向移动。",
  "tags": ["person", "dark_clothing", "backpack", "loitering"],
  "quality": {
    "reason": "背包颜色不确定",
    "score": 0.72
  },
  "attr": {
    "alert": false
  }
}
```

字段映射（旧 → 新）：

| 新字段 | 旧字段 | 说明 |
|--------|--------|------|
| `static` | `static_description_text` | 静态画面描述 |
| `dynamic` | `dynamic_description_text` | 动态事件描述 |
| `tags` | `tags` | 不变 |
| `quality.reason` | 新增 | 简述不确定/看不清的内容 |
| `quality.score` | `quality.confidence` | 置信度 0-1 |
| `attr.alert` | 新增 | boolean，仅表示人身/公共安全威胁异常 |

被移除：`schema_version`、`uncertainties`、`attributes.objects`、`attributes.event_phase`、
`quality.visibility`。

规则：

- `static` 和 `dynamic` 必须是字符串，可为空但不能缺失；
- `tags` 必须是字符串数组；
- `quality.score` 必须是 0-1 之间的数字；`quality.reason` 是字符串（可为空）；
- `attr.alert` 是 boolean，仅威胁人身/公共安全的异常才为 true；
- VLM 不得输出 `access_policy_id` / `security_level`（`extra="forbid"` 拒绝）；
- schema validation 失败不得进入 active ObservationRecord；
- 内部映射：`static/dynamic` → ObservationRecord 的 `*_description_text`；`quality`+`attr.alert`
  写入 ObservationRecord.attributes JSON（`{"quality": {...}, "alert": bool}`），DB schema 不变。

---

## 8. Index Document Schemas

### 8.1 ObservationStaticIndexDocument

```json
{
  "schema_version": "v1",
  "record_id": "obs_001",
  "vector_type": "static",
  "text": "大厅入口附近有一名穿深色衣服、背双肩包的人。",
  "embedding": [0.1, 0.2],
  "metadata": {
    "video_id": "video_001",
    "camera_id": "cam_lobby_01",
    "location_id": "loc_lobby_01",
    "analysis_scale": "default_segment",
    "observed_start_time": "2026-06-06T21:02:00+08:00",
    "observed_end_time": "2026-06-06T21:02:15+08:00",
    "access_policy_id": "policy_public_area",
    "security_level": "internal",
    "tags": ["person", "backpack"]
  }
}
```

### 8.2 ObservationDynamicIndexDocument

Same as static, but:

```json
{
  "vector_type": "dynamic",
  "text": "该人员在门口附近短暂停留后向走廊方向移动。"
}
```

---

## 9. SearchContext Schemas

### 9.1 SearchContext

```json
{
  "context_id": "ctx_001",
  "tenant_id": "tenant_default",
  "principal_id": "user_001",
  "session_id": "sess_001",
  "authorized_scope_hash": "scope_hash_abc",
  "dataset_revision": "data_rev_001",
  "mode": "snapshot",
  "default_revision_id": "rev_001",
  "created_at": "...",
  "last_accessed_at": "...",
  "expires_at": "...",
  "status": "active"
}
```

### 9.2 SearchRevision

```json
{
  "revision_id": "rev_001",
  "context_id": "ctx_001",
  "parent_revision_id": null,
  "op": "start",
  "op_params": {},
  "candidate_count": 18,
  "facets": {},
  "created_at": "..."
}
```

### 9.3 SearchCandidate

```json
{
  "revision_id": "rev_001",
  "record_id": "obs_001",
  "rank": 1,
  "score": 0.82,
  "score_detail": {}
}
```

---

## 10. Analysis Timeline Schemas

### 10.1 AnalysisTimelineEvent

Append-only observability event for local CCTV Memory analysis execution. This is
not business state; `AnalysisJob`, `AnalysisScaleTask`, `AnalysisUnit`,
`ModelCallLog`, `DetectorGateLog`, and publication tables remain authoritative.

```json
{
  "timeline_event_id": "tl_001",
  "trace_id": "job_001",
  "span_id": "span_001",
  "parent_span_id": null,
  "analysis_job_id": "job_001",
  "task_id": "task_001",
  "scale_task_id": "scale_001",
  "unit_id": "unit_001",
  "model_call_id": "mcall_001",
  "video_id": "video_001",
  "analysis_scale": "default_segment",
  "unit_kind": "default_segment_window",
  "segment_start_ms": 0,
  "segment_end_ms": 12000,
  "event_name": "frame_select",
  "event_phase": "start",
  "status": "running",
  "attempt_count": 1,
  "occurred_at": "2026-06-24T12:00:00Z",
  "duration_ms": null,
  "error_code": null,
  "error_message": null,
  "correlation": {"vlm_request_id": "vlm_req_001"},
  "metadata": {"frames_requested": 6},
  "created_at": "2026-06-24T12:00:00Z"
}
```

Rules:

- `event_phase` is one of `instant`, `start`, `finish`, `fail`.
- `occurred_at` and `created_at` are canonical timezone-aware datetimes in DTOs.
- `correlation` and `metadata` are JSON objects; adapters map to SQLite JSON text or PostgreSQL JSONB.
- Timeline events must be safe to export: no API keys, Authorization headers, `source_uri`, raw media/base64, raw media paths, or full internal filesystem paths.
- Error messages must be bounded and redacted.
- Timeline write failures are fail-open by default and must not fail video analysis.

---

## 11. Audit Schemas

### 11.1 AuditEvent

```json
{
  "audit_event_id": "audit_001",
  "event_type": "query",
  "request_id": "req_001",
  "principal_id": "user_001",
  "session_id": "sess_001",
  "context_id": "ctx_001",
  "resource_scope_hash": "scope_hash_abc",
  "record_ids": [],
  "video_id": null,
  "camera_id": null,
  "metadata": {},
  "created_at": "..."
}
```

---

## 12. Backup Manifest Schema

```json
{
  "schema_version": "v1",
  "app_version": "0.1.0",
  "database_engine": "sqlite",
  "created_at": "...",
  "included_paths": ["cctv_memory.sqlite3", "videos/"],
  "checksum": {
    "algorithm": "sha256",
    "value": "..."
  },
  "export_scope": "admin_full_backup"
}
```

---

## 13. 兼容与版本规则

- `schema_version` 变更必须记录 migration；
- 增加可选字段属于兼容变更；
- 删除字段、改变字段语义、改变枚举含义属于破坏性变更；
- API 破坏性变更进入 `/api/v2`；
- pipeline message 破坏性变更必须同时支持旧版本消费或完成迁移。
