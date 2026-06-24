# ARCHITECTURE_CONSTITUTION

## 0. Purpose

This document defines CCTV Memory's non-negotiable architecture rules. These rules exist to prevent security bugs, tangled dependencies, and unmaintainable code as requirements grow.

If implementation convenience conflicts with this document, this document wins.

---

## 1. Core Intent

CCTV Memory converts recorded surveillance video into searchable natural-language observation records with strict permission boundaries.

The system must remain:

```text
secure by default
modular by dependency direction
adapter-driven for infrastructure
contract-tested
migration-friendly from SQLite to PostgreSQL
safe for AI tool access
```

---

## 2. MVP Scope Lock

MVP MUST support:

```text
pre-recorded video files / externally prepared chunks
external camera_id + video_start_time
SQLite file database
FTS5 text search
optional sqlite vector extension or authorized-candidate rerank fallback
AI-facing search tools
SearchContext snapshot mode
server-side authorization
locator/playback secondary authorization
basic audit logging
```

MVP MUST NOT include:

```text
RTSP stream lifecycle management
real-time alerting
ReID
GraphRAG
frontend UI
complex policy management UI
object-level attribute database as primary search model
client/AI direct database access
```

MVP MAY include:

```text
embedded worker mode
admin seed config
limited CLI admin operations
optional thumbnails/clips
```

---

## 3. Layering Rule

Allowed dependency direction:

```text
api/client adapters
  -> application services / use cases
  -> domain models / policies
  -> repository/service ports
  -> infrastructure adapters
```

Forbidden reverse dependencies:

```text
domain imports FastAPI
domain imports SQLAlchemy
domain imports vendor VLM SDK
application imports raw database connection
API directly queries ORM models
infrastructure calls application business decisions upward
```

---

## 4. Contract Boundary Rule

All cross-module data must use explicit schema/DTO objects.

Forbidden:

```text
untyped dict passed across module boundaries
ORM model returned from repository port
vendor SDK response stored directly as active record
request body principal_id used as caller identity
```

---

## 5. Permission Red Lines

The following are hard failures:

```text
AI/client directly opens SQLite database file
AI-facing search repository can INSERT/UPDATE/DELETE business records
search/facet/count runs over full corpus then removes unauthorized rows
vector topK over full corpus then removes unauthorized rows
locator/playback URL returned without secondary authorization
source_uri exposed to external caller
VLM output decides access_policy_id or security_level
```

Every user-visible query must apply AuthorizedScope before result ranking, facet, count, details, locator, or playback.

---

## 6. Publication Red Lines

Only Publication flow may write active ObservationRecord.

Forbidden:

```text
VLM adapter writes ObservationRecord directly
video processing writes ObservationRecord directly
search/refine writes ObservationRecord
manual edit updates active record without history/audit
worker bypasses AnalysisJob/AnalysisScaleTask state rules
```

Publication must be atomic:

```text
upsert active records
archive replaced records
update job summary
mark index update state or emit index event
append audit event
commit or rollback as one unit
```

---

## 7. Database Boundary Rule

There are exactly two database contracts:

```text
docs/contracts/database-capability-contract.md
docs/contracts/database-adapter-contract.md
```

`docs/contracts/table-schema-spec.md` is a table structure spec, not a third database contract.

Upper layers depend on capability/repository contracts, not SQLite/PostgreSQL details.

---

## 8. Search Design Rule

Search is an AI-tool workflow, not a single backend natural-language query endpoint.

The backend accepts structured parameters:

```text
time_range
camera_ids/location_ids/video_ids
tag_filters
analysis_scale filters/preferences
search_mode
top_k
```

The external AI may decompose the user's natural language into multiple tool calls. The backend must remain deterministic, auditable, and permission-bounded.

---

## 9. Extensibility Rule

New requirements should extend through one of these mechanisms:

```text
new schema field with compatibility rule
new repository port method
new adapter implementation
new search op
new analysis_scale
new task type
new API endpoint behind capability check
```

New requirements must not:

```text
add ad-hoc SQL in application layer
bypass repository ports
write visual semantics into many strong structural columns prematurely
turn attributes JSON into uncontrolled business logic dependency
mix auth/search/VLM/video processing in one service class
```

---

## 9.1 Worker Concurrency Rule (task cctv-memory-20260615-1620)

In-process multi-job concurrency is allowed and bounded by the following non-negotiable rules:

```text
worker.max_concurrent_jobs bounds concurrent jobs (default 1 = serial, zero behavior change)
task claim MUST be atomic: no task processed by two workers (conditional UPDATE + rowcount)
ONE shared in-process provider limiter (VlmScheduler) is the single source of truth for the
  global provider-call cap; concurrent jobs MUST NOT multiply vlm.max_concurrent_requests
per-job unit pool size is decoupled (worker.max_unit_workers_per_job) and never raises the global cap
VLM calls stay OUTSIDE DB write locks; DB writes remain short critical sections
no new running-like state (no recoverable_running); crash/kill window covered by bounded orphan recovery
recovery stays bounded/index-backed/stale-cutoff; never a full-table scan
graceful shutdown stops claiming new jobs and lets in-flight jobs finish
```

SQLite stays the MVP backend under single-process thread-pool concurrency (WAL + busy_timeout +
short write critical sections). Migration to PostgreSQL is the documented next step when any of these
hold: multi-process / multi-host horizontal scaling is required; the in-process provider limiter can no
longer be the single source of truth; or SQLITE_BUSY / write-lock contention exceeds acceptable bounds.

---

## 10. Definition of Done

A non-trivial implementation is done only when:

```text
relevant contract/spec docs were followed
schema/DTO tests pass
repository contract tests pass where applicable
authorization tests pass for allowed and forbidden records
search/facet/count do not leak unauthorized records
publication atomicity tests pass if touched
error codes match docs/contracts/error-code-contract.md
migration or config impact is documented
```

If a task cannot meet these gates, it must stop and report the blocker instead of shipping a partial hidden assumption.
