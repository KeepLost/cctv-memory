# Job State Machine Contract（任务状态机契约）

## 0. 文档目的

本文定义 AnalysisJob、AnalysisScaleTask、HighFreqTrigger、TaskQueue 与 Publication 的状态迁移规则。worker 实现不得自行发明状态语义。

AnalysisTimelineEvent（任务 cctv-memory-20260624-1228）只记录本地执行时间线，属于
observability evidence，不是状态机输入，也不新增任何 running-like / terminal-like 状态。
timeline 写入失败默认 fail-open，不能改变 job/unit/scale/task 状态迁移结果。

---

## 1. AnalysisJob 状态

```text
queued
running
succeeded
partial_failed
failed
cancelled
```

### 1.1 合法迁移

```text
queued -> running
queued -> cancelled
running -> succeeded
running -> partial_failed
running -> failed
running -> cancelled
partial_failed -> running        # rerun failed units
partial_failed -> succeeded      # rerun recovered
failed -> running                # explicit rerun only
```

MVP 不要求取消已经发出的 VLM provider 请求；`cancelled` 只表示系统不再调度后续本地 work units。

### 1.2 成功语义

`AnalysisJob.succeeded` 要求：

```text
all required AnalysisScaleTask succeeded or skipped by policy
publication succeeded
active ObservationRecord / history / job summary committed
index update event/state recorded
```

### 1.3 partial_failed 语义

`partial_failed` 用于：

```text
some optional analysis units failed
some segments/units failed but usable records were published via per-unit publication
index update failed but publication committed and reindex is scheduled
required default_segment scale has some unit failures but at least one unit published
```

`partial_failed` 不能掩盖安全错误、schema corruption 或 publication atomic failure。

> 实现说明（任务 cctv-memory-20260611-1410）：每个 analysis unit 独立发布（per-unit publication）；
> default_segment scale 内部分单元失败 -> scale.status = partial_failed，同时 job.status = partial_failed。
> 只有 default_segment 的所有单元都失败且没有任何记录发布，才进入 AnalysisJob.failed 路径。

> Detector-gated VLM（任务 cctv-memory-20260622-1800）：gate-negative default_segment unit
> 不是 skipped，也不是 failed。只要 detector-only ObservationRecord 已通过 Publication 发布，该 unit
> 视为 succeeded；其 `produced_record_ids` 指向 detector-only record，且不会有对应 VLM ModelCallLog。
> 因此 default_segment 成功语义变为“每个 required window 按策略发布 VLM-enriched 或 detector-only
> record”，而不是“每个 window 必须 VLM 成功”。

---

## 1.5 AnalysisUnit 状态（任务 cctv-memory-20260611-1410 新增）

```text
pending
running
succeeded
failed
skipped
```

合法迁移：

```text
pending -> running
pending -> skipped
running -> succeeded
running -> failed
running -> skipped        # 抽帧零帧 near-EOF: skipped(insufficient_frames)
```

幂等键格式：`analysis_job_id:scale_task_id:analysis_scale:segment_start_ms:segment_end_ms`

> 终态保证（任务 cctv-memory-20260612-1854）：unit 在 `mark_running` 之后，抽帧 / media-ref
> 构建与 VLM 调用都在 per-unit 终态处理内，任何异常都会把 unit 落到终态（不会卡 `running`）：
> - 抽帧返回**零可用帧**（near-EOF / 窗口越界）-> `skipped`，`last_error_code=insufficient_frames`；
> - 抽帧返回**少于请求数但 >=1 帧** -> 仍照常送 VLM 分析；
> - 抽帧 / media-ref 抛出其它异常 -> `failed`，`last_error_code=frame_extraction_failed`，并记一条
>   `status=failed` 的 ModelCallLog。
> `skipped` 是良性终态：既非成功也非失败，不会把 scale/job 拉成 partial_failed/failed。

> 单元级瞬时重试（任务 cctv-memory-20260615-1447）：VLM 调用失败时，per-unit runner 会在
> **同一个 `running` unit 内**对 VLM 调用做有界重试，**不引入任何新状态**（无 `recoverable_running`）：
> - 仅重试**瞬时 provider 错误**（`VlmProviderError`：超时 / 传输 / 5xx / 429 / 冷启动），重试次数
>   由 `vlm.unit_max_attempts` 决定（默认 3；=1 即旧行为不重试），退避为指数 + 抖动，**每次尝试仍经过
>   全局 `VlmScheduler`**（并发上限 + 最小请求间隔不被绕过）；
> - **永久错误不重试**：schema/contract 校验、抽帧失败、`insufficient_frames`、发布错误、存储损坏 ——
>   立即落终态；
> - 重试预算耗尽 -> unit `failed`，`last_error_code=vlm_provider_error`（无 unit 残留 `running`）；
> - **审计**：每次失败的尝试各记一条 `status=failed` 的 ModelCallLog（`attempt_count` 为真实尝试序号，
>   `attempt_details` 记录错误类型/是否瞬时/退避毫秒）；最终成功记一条 `status=succeeded` 的 ModelCallLog
>   且 `attempt_count` 为成功时的尝试序号。`AnalysisUnit.attempt_count` 记录实际模型尝试次数，
>   `max_attempts` 为配置预算。
> 终态写入加固：`mark_failed` / `mark_skipped` / 成功提交都经有界 DB-写入重试（`vlm.terminal_write_max_attempts`），
> 遇到瞬时 SQLite 锁/busy 会短暂重试，避免终态写入静默失败造成 tally 与 DB 状态分歧；若仍无法持久化则
> 抛出错误（绝不假装成功），由有界孤儿回收（§7）兜底。重试期间发布保持精确一次（自然键 upsert，§6 publication）。

---

### 1.35 retryable 与永久错误（unit 层）

```text
transient (retry): vlm_provider_error  (timeout/transport/5xx/429/cold-start)
permanent (no retry): vlm_schema_validation_failed, frame_extraction_failed,
                      insufficient_frames(=skipped), publication/storage errors
```

---

### 1.4 failed 语义

`failed` 用于：

```text
video unreadable
required VLM step failed after retries
schema validation failure rate exceeds threshold
publication transaction failed
unrecoverable storage error
```

---

## 2. AnalysisScaleTask 状态

```text
pending
running
succeeded
partial_failed
failed
skipped
```

合法迁移：

```text
pending -> running
pending -> skipped
running -> succeeded
running -> partial_failed
running -> failed
partial_failed -> running        # rerun failed units
failed -> running                # explicit rerun only
```

`skipped` 必须记录 `skipped_reason`：

```text
not_enabled
no_motion_trigger
not_supported_by_provider
input_too_short
```

---

## 3. HighFreqTrigger 状态

```text
pending
running
succeeded
failed
skipped
```

合法迁移：

```text
pending -> running
pending -> skipped
running -> succeeded
running -> failed
failed -> running                # explicit rerun only
```

Trigger idempotency key：

```text
analysis_job_id:video_id:trigger_start_ms:trigger_end_ms:trigger_reason
```

---

## 3.5 Cross-Scale Unit Scheduling（任务 cctv-memory-20260611-1905, Stage C2）

依赖与调度规则（不发明新状态，复用 §1.5 AnalysisUnit / §2 ScaleTask 状态）：

```text
motion_scan -> high_freq_event 是硬依赖（high_freq unit 仅在 motion_scan 产出 trigger 后才被规划/调度）
default_segment 与 high_freq_event 之间无数据依赖（trigger 存在后可统一入队、交错调度）
motion_scan 仍不写 ObservationRecord；VLM adapter 仍不写 ObservationRecord
publication 仍走 PublicationService/repository ports；per-unit 幂等键不变（§1.5）
```

调度语义（worker 内进程级，非分布式）：

- worker 先串行跑 `motion_scan`（满足硬依赖），再把 `default_segment` 与
  `high_freq_event` 的 unit 统一入一个进程内优先级队列调度
  （`workers/cross_scale_scheduler.py:CrossScaleUnitScheduler`）。
- 优先级是确定性且防饥饿的：每派发至多 `pipeline.cross_scale.high_freq_quota` 个
  high_freq unit 就强制派发 1 个 default unit；一侧排空后排干另一侧。high_freq 被优先，
  default 永不饿死。unit 集合在调度前已全部规划完毕，故严格饥饿不可能发生。
- 真正的 provider 并发/限速由全局共享 `VlmScheduler`（Stage C1）在每个 unit 的 VLM 调用内
  施加；本调度层只决定派发顺序/优先级。
- `max_concurrent_requests>1` 时用单个有界线程池并发执行（仍受全局 VlmScheduler 上限约束），
  unit 完成顺序可乱序——`create_or_get_by_idempotency` + 立即 per-unit 发布保证乱序安全。

### Scale 完成判定（关键澄清）

```text
AnalysisScaleTask 完成 = 该 scale 的全部 unit 进入终态（succeeded/failed），
而不是“顺序处理块结束”。
```

每个 scale 在其自身 unit 的终态计数上独立 finalize（`_finalize_scale_task`，按
succeeded/failed 计数得到 succeeded / partial_failed / failed），与另一 scale 的完成顺序无关。

> 非中止保证 + skipped 计数（任务 cctv-memory-20260612-1854 §B）：`PlannedUnit.run` 返回
> `UnitOutcome`（succeeded/failed/skipped）且**约定不抛异常**（unit 终态由 processor 落库）。
> `CrossScaleUnitScheduler` 仍加一层 `_safe_run` 防御：若 unit 仍意外抛异常，则计为 **failed**
> 单元（绝不静默吞掉而留 `running`），所以单个 unit 永远不会卡死 Phase 4 / 整个 job。`skipped`
> 单元（near-EOF insufficient_frames）单独计数，是良性终态——既不算成功也不算失败，不会把 scale
> 拉成 partial_failed。`_finalize_scale_task` 的 skipped 不影响 succeeded/failed 判定。

### Job finalize（跨 scale）

```text
required default_segment 全部 unit 失败且无任何记录发布 -> AnalysisJob.failed
default_segment 部分 unit 失败（已有记录发布）或可选 scale(motion/high_freq) 失败 -> partial_failed
否则全部成功 -> succeeded
```

> near-EOF 精化（任务 cctv-memory-20260612-1854）：required default_segment 若有规划 unit 但
> **零成功**（全部 failed 和/或 skipped，没有产出任何记录）-> job `failed`（baseline 没产出）。
> 但「部分成功 + 末尾 near-EOF skip」不算失败：至少一个 default unit 成功即可 succeeded /
> partial_failed。顺序路径（cross_scale 关闭）采用相同判定，且因 per-unit 隔离会话，末尾失败/skip
> **不会回滚**前面已提交成功的 unit（§D）。

job finalize 在 default + high_freq 的全部 unit 终态、且各 scale 已 finalize 之后进行。
`partial_failed` 语义保持有意义：部分 unit 在已有记录发布后失败仍是 partial_failed（§1.3）。

### Crash recovery / 幂等

跨 scale 调度不改变崩溃恢复语义（§7）：unit 幂等键不变，rerun 经
`create_or_get_by_idempotency` 命中已成功 unit 即跳过，不重复发布。调度顺序/并发不参与
幂等判定。帧的内存环形缓冲是易失派生数据，不参与幂等（frame-stream-selector-cache-design §7）。

### 配置

```text
pipeline.cross_scale.enabled       默认 true（关闭则回退到旧的顺序 scale 循环）
pipeline.cross_scale.high_freq_quota 默认 3（防饥饿配额）
```

---

## 4. TaskQueue 状态

```text
queued
running
succeeded
failed
retry_scheduled
cancelled
```

合法迁移：

```text
queued -> running
running -> succeeded
running -> failed
running -> retry_scheduled
retry_scheduled -> queued
queued -> cancelled
running -> cancelled
```

Claim rules:

```text
claim only status=queued and next_run_at<=now
claim writes lease_owner and lease_expires_at
lease expiry allows reclaim
worker must refresh lease for long tasks
```

> 原子 claim 与多 job 并发（任务 cctv-memory-20260615-1620）：claim 对并发 worker 安全——
> 实现为条件 UPDATE（`WHERE task_id=? AND <仍可领条件>` + `rowcount==1` 判定），两个 worker 不可能
> 同时领到同一行（详见 database-adapter-contract §3.5）。`worker.max_concurrent_jobs>1` 时，
> `AnalysisWorker.drain` 用有界线程池让多个 job 并发 claim+process；每个 job 仍各自走完整、不变的
> 单 job 状态机（本文 §1/§2/§3.5）。并发**不**改变任何 job/scale/unit 状态语义，只是多个 job 同时推进。
> 单 job 失败被隔离（其自身终态由 process_one 落定，意外逃逸由池守护包裹并回退孤儿回收），不阻塞其它 job。
> 全局 provider 上限由唯一共享 `VlmScheduler` 跨所有并发 job 强制。优雅关停：drain 的 `should_stop`
> 置位后停止 claim 新 job，在途 job 跑完；崩溃/kill 窗口仍由 §7.1 有界孤儿回收兜底。不引入任何新状态。

Retry rules:

```text
retry_count < max_retries -> retry_scheduled
retry_count >= max_retries -> failed
retry delay should be exponential or configured
```

---

## 5. Publication State Rules

Publication command may run only when:

```text
AnalysisJob exists
AnalysisScaleTask has validated VLM results
records pass schema validation
records have system-derived camera/location/policy/security metadata
```

Publication transaction must:

```text
UPSERT active ObservationRecord
archive replaced records
update AnalysisJob created/updated/archived ids
mark scale task/job status
record index update state/event
append audit event
```

If transaction fails, no partial active records may remain.

---

## 6. Index Update Failure

Index update is not the fact source. If active publication succeeds but index update fails:

```text
AnalysisJob -> partial_failed
index_rebuild_needed = true or index update task queued
records remain queryable by DB/FTS fallback if supported
```

Never roll back successful fact publication solely because optional external index update failed, unless the configured deployment declares index mandatory.

---

## 7. Worker Crash Recovery

On startup:

```text
requeue tasks with expired lease
mark orphan running scale tasks as partial_failed or recoverable_running according to task evidence
never duplicate publication without idempotency key / unique constraint
```

Publication command must be idempotent against duplicate task delivery.

### 7.1 Orphan-running 单元恢复（任务 cctv-memory-20260612-1854 §E，已实现）

`AnalysisWorker.recover_orphans()` 在 worker 启动 / 每次 `drain` 前做一次**有界、索引支撑、
stale-cutoff、批量受限**的孤儿单元回收，避免崩溃后 unit 永久卡 `running`：

```text
查询条件: status='running' AND started_at < (now - worker.orphan_stale_seconds)
排序/限制: ORDER BY started_at LIMIT worker.orphan_batch_limit
索引: idx_units_status_started(status, started_at)  -> O(log N + K)，K=批量大小
绝不做全表扫描；started_at 为 NULL 的单元（未开始）不参与
```

处理：

```text
命中的每个 stale unit -> mark_failed(error_code=orphan_timeout)
仅 reconcile 这些单元的父 scale_task（按其 unit 终态计数重新 finalize；仍有非终态 unit 则跳过）
仅 reconcile 受影响的父 job（所有 scale 终态后按 §3.5 规则 finalize：required default 零成功->failed；
  任一 scale 失败->partial_failed；否则 succeeded）
```

约束：本任务**不引入 `recoverable_running`**（Eric 决策）；孤儿单元一律落 `failed(orphan_timeout)`。
配置见 configuration：`worker.orphan_recovery_enabled` / `orphan_stale_seconds`（须 > `lease_seconds`）
/ `orphan_batch_limit`。该回收是幂等的：干净库扫描 0 行。

---

## 8. Contract Tests

Required tests:

```text
job_success_requires_publication
partial_failed_when_optional_segment_fails
publication_rollback_leaves_no_active_partial_records
expired_task_lease_can_be_reclaimed
duplicate_publication_command_is_idempotent
index_failure_sets_rebuild_needed_not_data_loss
invalid_state_transition_rejected
cross_scale_high_freq_prioritized_after_triggers
cross_scale_default_not_starved
cross_scale_out_of_order_completion_finalizes_scale_and_job
cross_scale_partial_failed_with_mixed_scale_failures
cross_scale_rerun_does_not_duplicate_units_or_publications
zero_frames_near_eof_unit_skipped_insufficient_frames
partial_frames_still_sent_to_vlm
frame_extraction_exception_unit_failed_no_stuck_running
cross_scale_near_eof_failure_does_not_strand_job
sequential_path_preserves_earlier_successes
near_eof_window_clamped_to_duration
orphan_running_recovery_bounded_index_backed
no_recoverable_running_state
concurrent_claim_same_task_single_winner
concurrent_claim_no_task_claimed_twice
multi_job_default_serial_preserves_behavior
multiple_one_unit_jobs_run_concurrently
global_vlm_cap_not_exceeded_across_jobs
per_job_unit_limit_does_not_multiply_provider_cap
one_job_failure_does_not_block_others
multi_job_publication_exactly_once
graceful_should_stop_prevents_new_claims
concurrent_drain_leaves_no_running_units
```
