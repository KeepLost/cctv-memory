# Testing Contract（测试契约）

## 0. 文档目的

本文定义 CCTV Memory 的测试分层、必须覆盖的架构/权限/数据库/搜索/状态机测试。没有这些测试，后续需求扩展很容易破坏边界。

---

## 1. Test Layers

```text
unit tests
schema validation tests
repository contract tests
adapter integration tests
authorization/security tests
search golden tests
job state machine tests
API envelope/error tests
migration tests
backup/restore tests
architecture dependency tests
```

---

## 2. Schema Tests

必须覆盖：

```text
API request/response schema
Domain schema
Pipeline message schema
VLM output schema
Index document schema
Audit event schema
Backup manifest schema
```

Rules:

- missing required fields fail；
- unknown fields policy must be explicit；
- schema_version compatibility tests required for persisted/queued messages。

---

## 3. Repository Contract Tests

SQLite adapter and future PostgreSQL adapter must pass the same repository contract tests.

Required:

```text
crud_roundtrip
upsert_idempotency
authorized_read_hides_forbidden_records
authorized_facet_excludes_forbidden_records
search_repository_is_read_only
publication_upsert_archives_old_record
publication_rollback_on_failure
task_claim_lease_expiry_reclaim
audit_append_roundtrip
timeline_append_roundtrip
```

### 3.1 PostgreSQL parity / native-type tests（强制）

PostgreSQL 是带原生 `TIMESTAMPTZ` / `JSONB` / `vector(N)` 的后端，与 SQLite 的
"一切皆字符串" 物理形状不同。因此：

- 接受任何 PostgreSQL 相关改动时，原生类型 / parity 测试**必须**针对真实
  PostgreSQL + pgvector 实例运行，而非仅静态分析或 SQLite。
- 测试通过 `CCTV_MEMORY_TEST_POSTGRES_DSN` 环境变量定位实例；**本地缺失时可 skip**，
  但 **CI / release 验证必须提供该 DSN**，否则不得判定 PG 改动通过。
- 至少覆盖：schema 原生类型断言、代表性写入回读（TIMESTAMPTZ/JSONB/vector 往返）、
  task 队列 claim→DTO 回读、job 终态写（mark_succeeded/mark_failed 不得报 DatatypeMismatch）、
  端到端 worker 把 job 推进到终态。
- 方法论：对每个 PG 专有修复，应能用"回退即复现"反证（移除修复后真实 PG 上复现缺陷）。

---

## 4. Authorization / Security Tests

Required:

```text
request_body_principal_ignored
capability_required_for_each_endpoint
forbidden_record_not_in_search_results
forbidden_record_not_in_count
forbidden_record_not_in_facets
forbidden_record_details_returns_not_found_or_empty
locator_requires_second_authz
playback_token_expires
source_uri_never_exposed
search_repository_cannot_write_active_records
```

Any failure in these tests is release-blocking.

---

## 5. Search Golden Tests

Use small deterministic fixture datasets.

Required fixtures:

```text
same camera, multiple time ranges
same visual appearance, different dynamic events
same event, different security levels
same tag across authorized and forbidden records
high_freq_event overlapping default_segment
```

Required tests:

```text
static_attribute_search_matches_static_text
dynamic_event_search_matches_dynamic_text
hybrid_rrf_deterministic
analysis_scale_preference_boosts_but_does_not_filter
analysis_scale_filter_filters
facet_counts_only_authorized_candidates
overlap_returns_authorized_overlapping_records
refine_revision_is_immutable
```

---

## 6. Job / Worker Tests

Required:

```text
valid_state_transitions_allowed
invalid_state_transitions_rejected
worker_claim_sets_lease
expired_lease_can_be_reclaimed
retry_count_and_next_run_at_updated
partial_failed_for_optional_unit_failure
publication_idempotent_under_duplicate_message
index_update_failure_schedules_rebuild
```

### 6.1 多 job 并发（任务 cctv-memory-20260615-1620）

Required（见 `tests/integration/test_multi_job_concurrency.py`）：

```text
concurrent_claim_same_task_single_winner        # 原子 claim: 8 线程争 1 task, 恰 1 胜
concurrent_claim_no_task_claimed_twice          # N task / 2N worker, 无 task 被领两次
claim_preserves_priority_and_expiry             # 原子化后仍保优先级 + 过期重领
max_concurrent_jobs_default_is_one              # 默认配置 = 串行旧行为
serial_drain_processes_all_jobs                 # max_concurrent_jobs=1 全部 job 正确完成
multiple_one_unit_jobs_run_concurrently         # >1 时不同 job 的 VLM 调用真重叠
global_vlm_cap_not_exceeded_across_jobs         # 全局在途 VLM ≤ vlm.max_concurrent_requests
per_job_unit_limit_does_not_multiply_provider_cap  # jobs×unit池 不放大全局上限
one_job_failure_does_not_block_others           # 单 job 失败隔离, 其它成功
no_duplicate_active_records_under_concurrency   # 并发下发布精确一次
should_stop_prevents_claiming_new_jobs          # 优雅关停不再 claim 新 job
concurrent_drain_leaves_no_running_units        # 并发 drain 后无残留 running
```

要求：并发测试用注入式探针 VLM（记录在途峰值 / 不同 job 重叠）+ static 视频模式（无子进程）；
不依赖真实 provider；断言确定性（峰值上限、唯一胜者、无重复记录）。

### 6.2 Analysis timeline observability（任务 cctv-memory-20260624-1228）

Required:

```text
timeline_dto_rejects_bad_phase
timeline_repository_roundtrip_sqlite
timeline_repository_roundtrip_postgres_if_dsn
timeline_redacts_source_uri_secrets_and_base64
worker_emits_request_queue_unit_frame_scheduler_vlm_publication_events
vlm_scheduler_emits_wait_and_acquire_spans
timeline_export_writes_json_and_offline_plotly_html
```

Exporter tests must assert the generated HTML contains embedded Plotly runtime and
does not reference external CDN/cloud URLs such as `cdn.plot.ly`, `https://`, or
`http://` for Plotly assets.

---

## 7. API Tests

Required:

```text
all_success_responses_use_envelope
all_error_responses_use_envelope
request_id_propagated_or_generated
pagination_cursor_shape_stable
error_codes_match_error_code_contract
```

---

## 8. Migration Tests

Required:

```text
fresh_db_has_schema_version
migration_from_previous_version_preserves_records
migration_rejects_unsupported_version
sqlite_export_can_feed_postgres_import_contract_future
```

---

## 9. Backup / Restore Tests

Required:

```text
admin_backup_manifest_valid
user_export_filters_authorized_scope
restore_rejects_bad_checksum
sqlite_online_backup_is_consistent
```

---

## 10. Architecture Dependency Tests

At minimum enforce by import tests or static checks:

```text
domain_does_not_import_fastapi_sqlalchemy_vlm_sdk
application_does_not_import_raw_db_driver
api_does_not_import_infrastructure_concrete_adapter_directly
infrastructure_does_not_import_api_router
```

---

## 11. Release Gate

Before merging non-trivial changes:

```text
pytest unit + contract tests pass
security tests pass
search golden tests pass if search changed
migration tests pass if schema changed
backup tests pass if backup/export changed
architecture dependency tests pass
```

Skipping a gate requires explicit written waiver in the task/PR notes.

---

## 12. Subprocess & Blocking-Command Safety (BINDING)

Background: a manual `cctv-memory analyze --wait` against the real ffprobe path
blocked an agent session for 6+ hours. See `status/archive/incidents/incident-blocking-subprocess.md`
for the full root-cause analysis. The following rules are binding to prevent
recurrence.

### 12.1 Production code (subprocess invocation)

Any `subprocess.run` / `Popen` that calls an external binary (ffprobe, ffmpeg, …)
MUST:

```text
- pass stdin=subprocess.DEVNULL          # never wait for interactive input
- pass an explicit, bounded timeout       # never wait forever
- capture_output=True                      # never inherit/share a blocking pipe
- map TimeoutExpired / CalledProcessError / OSError to a domain error
  (e.g. video_decode_error) WITHOUT leaking stderr / internal paths
```

A subprocess call without all four is a release-blocking defect.
Reference implementation: `cctv_memory/infrastructure/video/ffprobe_adapter.py`.

### 12.2 Tests

```text
- Tests MUST NOT depend on real ffprobe/ffmpeg or real media for the closed loop.
  Use StaticVideoProcessor (or pipeline.video_metadata_mode=static).
- A test that DOES spawn ffmpeg/ffprobe (e.g. to synthesize a fixture) MUST pass
  stdin=subprocess.DEVNULL + a bounded timeout, and be guarded by a
  shutil.which(...) skipif so it is skipped when the binary is absent.
- No test may start a long-lived server (uvicorn). Use FastAPI TestClient instead.
```

### 12.3 Agent / operator verification commands

```text
- NEVER run a command that can block without an explicit outer timeout
  (e.g. `timeout 60 uv run ...`).
- FORBIDDEN as ad-hoc debug commands: `cctv-memory analyze --wait` against a real
  ffprobe path, `cctv-memory serve` / uvicorn, any unbounded ffprobe/ffmpeg.
- To exercise the closed loop safely, set
  CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static (no subprocess) — see
  `docs/operations/runbook-video-analysis-flow.md`.
- If a command appears to exceed its expected time, abort and report; do not wait.
```

### 12.4 Required regression tests

```text
ffprobe_missing_file_raises_quickly_without_hanging   # bounded failure, no hang
worker_marks_job_failed_when_probe_fails              # running -> failed, not stuck queued
cli/api closed loop runs in static mode without ffprobe
```
