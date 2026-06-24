# Non-Functional Requirements（非功能性需求）

## 0. 文档目的

定义 MVP 阶段的性能预期、容量目标和可接受延迟，为实现和优化提供明确基准。

---

## 1. MVP 数据规模目标

| 指标 | MVP 目标 | 说明 |
|------|---------|------|
| 摄像头数量 | 1-20 | 单楼宇 / 小型园区 |
| 视频源总量 | < 5000 条 VideoSource | 覆盖数周到数月录像 |
| ObservationRecord 总量 | < 500,000 条 active | 按 12s 窗口估算 |
| 单次检索候选上限 | 1000 条 | SearchContext revision 上限 |
| 并发用户 | 1-5 | MVP 阶段单用户或小团队 |
| 并发 AnalysisJob | 1-3 | SQLite 单写约束下 |

---

## 2. 延迟目标

| 操作 | P95 目标 | 说明 |
|------|---------|------|
| Health check | < 50ms | |
| Start search（FTS5） | < 500ms | 500K records, authorized scope |
| Start search（向量 rerank） | < 2s | 应用层向量重排 ≤1000 候选 |
| Refine search | < 500ms | 在已有候选集上操作 |
| Get details（含 locator） | < 200ms | |
| Submit video source | < 300ms | 入队，不含分析 |
| VLM 单片段分析 | 5-60s | 取决于 provider |
| 单视频完整分析（1小时） | 5-30min | 取决于 VLM 并发和窗口数 |

---

## 3. 存储预算

| 资源 | 单条估算 | 500K 条估算 |
|------|---------|-----------|
| ObservationRecord 行 | ~2KB | ~1GB |
| FTS5 索引 | ~1KB/record | ~500MB |
| 向量索引（1536d float32） | ~12KB/record × 2 | ~12GB |
| SQLite DB 总大小 | - | < 15GB |
| 视频文件 | 不计入 DB | 外部管理 |
| AnalysisTimelineEvent 行 | ~0.5-2KB | 诊断数据；按 retention 清理 |

如果 500K × 2 向量超出 SQLite 性能阈值，应考虑迁移 PostgreSQL + pgvector。

Timeline observability adds short append-only writes during analysis. MVP target:
timeline write overhead should stay below 5% wall-time for mock/static pipeline
tests, and timeline write failures must fail open. Operators should use
`observability.timeline_retention_days` to bound long-term storage growth.

> 说明（OpenCV FrameStream, 任务 cctv-memory-20260611-1805）：
> - **分析期峰值内存**：opencv 后端的裸帧只在有界环形缓冲中，峰值约
>   `max_buffer_bytes × max_concurrent_requests`（默认 256MiB × 1）；评分只保留每帧标量，
>   绝不全片驻留裸帧。
> - **选中帧磁盘占用**：仅选中帧落盘（JPEG）。`metadata_only`（默认）下 unit 成功即清理选中帧
>   工作文件（`cleanup_selected_on_success=true`），稳态占用极小；`debug_full_media` 才在
>   artifact_root 长期留存。设计实测量级：1h 视频选中帧 720p≈422MB / 1080p≈844MB（清理前）。

---

## 4. 可用性

| 指标 | MVP 目标 |
|------|---------|
| 服务启动时间 | < 10s |
| 优雅关闭 | < 30s |
| 单进程崩溃恢复 | 重启后自动 requeue expired tasks |
| 数据持久性 | SQLite WAL + backup |

---

## 5. 何时升级

满足以下任一条件时，应认真评估从 SQLite 迁移到 PostgreSQL + pgvector：

- active records > 500K 且检索延迟超标
- 并发 AnalysisJob > 3 导致 SQLite 写锁等待频繁
- 向量索引内存/性能不可接受
- 多用户并发 > 5 且存在明显排队

满足以下条件时考虑引入 OpenSearch：

- FTS5 中文分词无法满足检索质量
- 需要复杂聚合/多字段全文组合
- active records > 1M

---

## 6. Benchmark 验收

MVP 上线前应通过以下简单 benchmark：

```text
1. 导入 1000 条 ObservationRecord（含 FTS + 可选向量）
2. 执行 10 种标准 query（static / dynamic / hybrid / tag / overlap）
3. 验证 P95 延迟在上述目标内
4. 验证权限过滤正确（无权记录不出现）
5. 验证 rerun 后旧记录进入 history
```

不要求建立持续 benchmark CI，但首次交付和重大重构后应跑一次。
