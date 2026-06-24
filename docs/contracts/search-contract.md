# Search Contract（检索契约）

## 0. 文档目的

本文定义 CCTV Memory 的检索模式、输入输出、SearchContext、Revision、Candidate、score_detail、facet、RRF、权限过滤和 overlap 查询语义。

原则：

- 外部 AI 负责查询规划，后端不强制做自然语言 QueryDecomposer；
- 检索 API 接受结构化参数；
- 所有检索先做授权过滤；
- 无权记录不进入结果、facet、candidate_count；
- SearchContext 支持多轮 refine，MVP 为 snapshot mode。

---

## 1. Search Modes

```text
static_attribute  外观/场景/物体/静态属性
dynamic_event     动作/事件/异常变化
hybrid            外观 + 动作组合
auto_by_external_ai  外部 AI 已自行规划，服务端按参数执行
```

### 1.1 static_attribute

主要检索：

```text
static_description_text
static_description_vector
tags
```

适合：穿衣、背包、帽子、物体、区域外观。

### 1.2 dynamic_event

主要检索：

```text
dynamic_description_text
dynamic_description_vector
```

适合：徘徊、摔倒、奔跑、尾随、争执、异常变化。

### 1.3 hybrid

同时使用：

```text
static
dynamic
tags
analysis_scale boost
```

第一版推荐 RRF 合并。

---

## 2. Start Search

### 2.1 StartObservationSearchRequest

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

- time_range 可选但推荐；
- camera/location/video filters 进入结构化过滤；
- tag_filters 默认作为粗筛/boost，不默认多 tag 硬 AND，除非参数明确指定；
- preferred_analysis_scales 用于 boost，不等于 filter；
- analysis_scale_filter 是硬过滤。

---

## 3. Refine Search

### 3.1 Refine op 枚举

```text
narrow_by_tags
search_static_text
search_dynamic_text
hybrid_search_text
filter_by_analysis_scale
apply_rrf_fusion
rerank_current_candidates
```

### 3.2 RefineObservationSearchRequest

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

规则：

- refine 生成新 revision；
- base revision 不变；
- refine 不能扩大授权范围；
- refine 可以扩大语义召回范围，但仍必须在 context authorized_scope 内执行。

---

## 4. SearchContext / Revision / Candidate

### 4.1 SearchContext

```text
context_id
principal_id
tenant_id
session_id
authorized_scope_hash
dataset_revision
mode = snapshot / stream
```

MVP：`snapshot only`。

### 4.2 SearchRevision

Revision 不可变，记录一次检索操作的结果。

```text
revision_id
context_id
parent_revision_id
op
op_params
candidate_count
facets
created_at
```

### 4.3 SearchCandidate

```text
revision_id
record_id
rank
score
score_detail
```

Candidate 只保存 active ObservationRecord 的 record_id。

---

## 5. Ranking / Score Detail

### 5.1 ScoreDetail

```json
{
  "static_score": 0.7,
  "dynamic_score": 0.6,
  "static_rank": 2,
  "dynamic_rank": 4,
  "fts_score": 0.5,
  "tag_boost": 0.1,
  "analysis_scale_boost": 0.05,
  "rrf_score": 0.82,
  "final_score": 0.82,
  "explain": "optional debug string, admin/debug only"
}
```

普通 AI-facing response 可以不返回 `explain`。

### 5.2 RRF

MVP 推荐 Reciprocal Rank Fusion：

```text
rrf_score = Σ weight_i * (1 / (rank_i + k)) + boosts
```

默认：

```text
k = 60
static_weight = 1.0
dynamic_weight = 1.0
fts_weight = 0.5
tag_boost <= 0.2
analysis_scale_boost <= 0.2
```

具体权重可配置，但必须写入 `score_detail` 或 search config version，便于复现。

---

## 6. Facet Contract

Facet 只统计授权候选。

```json
{
  "candidate_count": 18,
  "top_tags": [{"tag": "backpack", "count": 9}],
  "camera_distribution": [{"camera_id": "cam_lobby_01", "count": 18}],
  "location_distribution": [{"location_id": "loc_lobby_01", "count": 18}],
  "time_distribution": [{"bucket_start": "...", "bucket_end": "...", "count": 5}],
  "analysis_scale_distribution": [{"analysis_scale": "default_segment", "count": 15}]
}
```

禁止包含无权记录统计。

---

## 7. Overlap 查询

### 7.1 get_overlapping_records

输入：

```json
{
  "record_id": "obs_high_001",
  "analysis_scale_filter": ["default_segment"],
  "time_padding_ms": 0,
  "top_k": 20
}
```

语义：

```text
candidate.segment_start_ms < target.segment_end_ms + padding
AND candidate.segment_end_ms > target.segment_start_ms - padding
```

也可用 observed_start/end_time 实现。

规则：

- 不建立层级关系；
- 只查授权范围；
- target record 本身无权时返回 not_found/empty；
- 结果仍是 ObservationRecord 列表或 SearchResultItem 列表。

---

## 8. Locator in Search

Search result 默认只返回轻量 preview。详情接口可通过：

```text
include_locator=true
```

返回 locator projection。

locator 不参与 ranking；locator 生成必须二次鉴权。

---

## 9. Limits

MVP 默认限制：

```text
max_top_k = 100
max_candidates_per_revision = 1000
max_revisions_per_context = 8
context_ttl = 15 minutes
context_idle_timeout = 5 minutes
max_active_contexts_per_principal = 3
```

超限返回 `limit_exceeded`。

---

## 9.5. SearchContext Lifecycle（上下文生命周期）

### 9.5.1 创建

SearchContext 在 `start_observation_search` 时创建，绑定：

```text
principal_id
tenant_id
session_id
authorized_scope_hash
dataset_revision
mode = snapshot
expires_at = now + context_ttl
last_accessed_at = now
status = active
```

如果 principal 已有 `max_active_contexts_per_principal` 个 active context，返回 `limit_exceeded`，提示关闭旧 context。

### 9.5.2 访问刷新

每次 refine / facet / details / overlap 操作时：

```text
last_accessed_at = now
expires_at = max(expires_at, now + context_idle_timeout)
```

但 `expires_at` 不得超过 `created_at + context_ttl`（硬 TTL 上限）。

### 9.5.3 过期

Context 过期条件（满足任一）：

```text
now > expires_at
now > last_accessed_at + context_idle_timeout
```

过期 context 状态变为 `expired`。

### 9.5.4 驱逐策略

MVP 推荐 **lazy + periodic sweep**：

- **Lazy：** 每次访问 context 时检查是否过期；过期则返回 `context_expired` 错误，客户端需重新 start search。
- **Periodic sweep：** maintenance worker 定期（建议每 60s）扫描并删除 expired/closed context 及其 revisions + candidates。

不需要复杂 LRU 或 eviction queue；数据规模小，定期扫描足够。

### 9.5.5 关闭

`close_search_context` 将 status 设为 `closed`，下次 sweep 清理。

### 9.5.6 权限变更影响

如果 access policy 变更后导致 `authorized_scope_hash` 失效：

MVP 推荐策略：策略变更时使所有相关 active context 立即 expire。

这避免了权限漂移（旧 context 返回已不应访问的记录）。客户端收到 `context_expired` 后重新 start search 会使用新 scope。

---

## 10. 权限硬规则

所有 search/facet/details/overlap/locator：

1. 验证 principal；
2. 验证 capability；
3. 计算 AuthorizedScope；
4. 在 SQL/FTS/vector/candidate 阶段应用 scope；
5. 无权内容不进入结果或统计。

禁止：

```text
全库向量 topK 后裁剪无权结果
全库 facet 后删除无权 bucket
返回“有结果但你无权查看”
```

---

## 11. Response Items

### 11.1 SearchResultItem

```json
{
  "record_id": "obs_001",
  "rank": 1,
  "score": 0.82,
  "score_detail": {},
  "preview_text": "...",
  "analysis_scale": "default_segment",
  "observed_start_time": "...",
  "observed_end_time": "..."
}
```

### 11.2 Empty result wording

AI-facing client 推荐话术：

```text
在你可访问范围内没有找到匹配记录。
```

---

## 12. Contract Tests

必须覆盖：

```text
static_search_returns_authorized_matches_only
dynamic_search_returns_authorized_matches_only
hybrid_rrf_is_deterministic
facets_exclude_forbidden_records
overlap_respects_authorization
details_hidden_for_forbidden_record
locator_requires_second_authz
revision_is_immutable
refine_does_not_expand_authorized_scope
```
