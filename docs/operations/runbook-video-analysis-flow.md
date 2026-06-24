# Runbook: Video → Analysis → Storage → Retrieval (MVP Closed Loop)

- scope: how to run the full MVP closed loop locally and verify it.
- status: verified 2026-06-09 (commit c47ddc5).
- honesty: this is a **mock-VLM** loop. The VLM produces deterministic placeholder
  text tagged `[mock]`; there is NO real video understanding, no vector search,
  no real playback. `ffprobe` (real, bounded) only reads duration.

---

## 0. Safety first (read before running anything)

- ALWAYS wrap commands in an outer timeout, e.g. `timeout 60 uv run ...`.
- For local/dev/demo runs, prefer **static** video-metadata mode so nothing
  spawns `ffprobe`:

```bash
export CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static
export CCTV_MEMORY_PIPELINE__STATIC_DURATION_MS=30000   # 30s synthetic duration
```

- Only use the real ffprobe path (default mode) with an actual readable media
  file. ffprobe is hardened (stdin=DEVNULL + 10s timeout) so a bad/missing path
  fails fast as `video_decode_error` instead of hanging. See
  `status/archive/incidents/incident-blocking-subprocess.md`.
- FORBIDDEN as casual commands: `analyze --wait` on a real ffprobe path without a
  timeout, `serve`/uvicorn, unbounded ffprobe/ffmpeg.

---

## 1. The pipeline (what happens end to end)

```text
submit video source
  -> IngestionService: create/reuse VideoSource (idempotent on camera_id+video_start_time)
                       create AnalysisJob (idempotent on idempotency_key)
                       create default_segment AnalysisScaleTask
                       enqueue analyze_video task (TaskQueue)
  -> AnalysisWorker.claim: lease the queued task
  -> commit job RUNNING + scale task RUNNING (own transaction)
  -> DefaultSegmentProcessor:
       probe duration (ffprobe bounded, or static)
       plan default_segment windows (window/overlap from config)
       per window: MockVlmAnalyzer -> VlmObservationOutput (validated, no policy)
       attach SYSTEM-derived metadata: camera/location (from repos),
         access_policy_id + security_level (domain inheritance), observed times
       atomic publication of ObservationRecords (upsert active + archive + audit)
  -> mark scale task + job SUCCEEDED, video ready, task succeeded
  -> SearchService: authorized SQL-LIKE candidates (scope applied in SQL),
       deterministic keyword/tag score, persist SearchContext/Revision/Candidates,
       build facets
  -> LocatorService: authorized details + locator projection (2nd authz,
       NO source_uri, playback_url is a placeholder token) + overlap query
```

Security invariants enforced throughout: AuthorizedScope computed server-side
(empty allowed_* ⇒ deny, fail closed); identity never from request body;
`source_uri` never exposed; only the publication path writes active records; VLM
never sets policy/security.

### 1.1 Multi-scale (opt-in): motion_scan + high_freq_event

When the submit request sets `analysis_options.enable_motion_triggered_high_freq`
(CLI `analyze --enable-high-freq`), the job gets three scale tasks and the worker
processes them in order under the single `analyze_video` queue task:

```text
default_segment   -> baseline records (as above)
motion_scan       -> FrameDiffMotionDetector samples motion (one bounded ffmpeg
                     pass, downscaled grayscale frame diff) -> domain planner
                     -> HighFreqTrigger rows (NO active records; contract §2.2)
high_freq_event   -> plan short windows per trigger -> VLM with high_freq_event_v1
                     prompt -> publish ObservationRecords (analysis_scale=
                     high_freq_event). No triggers -> skipped(no_motion_trigger).
```

A required default_segment failure fails the whole job; an optional
motion_scan/high_freq_event failure downgrades the job to `partial_failed` while
keeping the published default_segment records (job-state-machine-contract §1.3).

---

## 2. CLI walkthrough (verified)

```bash
cd /root/.openclaw/workspace/codes/cctv-memory
export CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static
export CCTV_MEMORY_PIPELINE__STATIC_DURATION_MS=30000

# 1) initialize data dir + schema + seed dev principal/policy/camera
timeout 60 uv run cctv-memory init --data-dir ./data
# -> {"status": "initialized", "data_dir": "./data"}

# 2) submit + run embedded worker to completion (--wait)
timeout 60 uv run cctv-memory analyze \
  --data-dir ./data \
  --source-uri /any/path/lobby.mp4 \
  --camera-id cam_lobby_01 \
  --video-start-time 2026-06-06T21:00:00+08:00 \
  --idempotency-key demo-1 \
  --wait
# -> {"video_id": "...", "analysis_job_id": "...", "accepted": true,
#     "worker_processed_tasks": 1, "job_status": "succeeded"}

# 3) authorized search (dev principal user_admin)
timeout 60 uv run cctv-memory search --data-dir ./data --query person --top-k 5
# -> {"context_id": "...", "candidate_count": 3, "facets": {...},
#     "results": [{"record_id": "...", "preview_text": "[mock] Camera cam_lobby_01 ...", ...}]}
```

Alternative: process the queue separately instead of `--wait`:

```bash
timeout 60 uv run cctv-memory analyze --data-dir ./data --source-uri ... \
  --camera-id cam_lobby_01 --video-start-time 2026-06-06T21:00:00+08:00 --idempotency-key demo-2
timeout 60 uv run cctv-memory worker --data-dir ./data --once   # processes one task
```

### Real ffprobe path (only with a real media file)

```bash
unset CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE   # back to default 'ffprobe'
# Bad/missing path fails fast (NOT a hang):
timeout 30 uv run cctv-memory analyze --data-dir ./data --source-uri /no/such.mp4 \
  --camera-id cam_lobby_01 --video-start-time 2026-06-06T21:00:00+08:00 --idempotency-key x --wait
# -> {"...","job_status": "failed"}   (error_code video_decode_error internally)
```

---

## 3. HTTP API walkthrough

The API is exercised via FastAPI `TestClient` in `tests/integration/test_api_smoke.py`
(no long-lived server). Endpoints + unified envelope:

```text
GET  /api/v1/health
POST /api/v1/video-sources/analyze            body: SubmitVideoSourceRequest
GET  /api/v1/analysis-jobs/{analysis_job_id}
POST /api/v1/observation-search/contexts      body: StartObservationSearchRequest
POST /api/v1/observation-search/details       body: ObservationDetailsRequest
POST /api/v1/observation-search/overlapping-records  body: OverlappingRecordsRequest
```

- Identity: header `X-Principal-Id` (default dev principal if absent). NEVER in body.
- Success: `{"ok": true, "request_id", "data", "meta": {schema_version, server_time}}`.
- Error: `{"ok": false, "request_id", "error": {code, message, details}, "meta"}`.
- Validation failures → HTTP 400 `validation_error`.
- Do NOT run `uvicorn`/`serve` for verification; use the TestClient-based tests.

---

## 4. How to verify (all bounded)

```bash
cd /root/.openclaw/workspace/codes/cctv-memory
timeout 60  uv run ruff check .
timeout 180 uv run pyright cctv_memory
timeout 150 uv run mypy cctv_memory/contracts cctv_memory/domain cctv_memory/application cctv_memory/repositories
timeout 240 uv run pytest tests/
```

Expected: ruff passed; pyright 0/0/0; mypy clean; pytest 79 passed (1 harmless
Starlette/httpx deprecation warning).

Closed-loop-specific tests:
- `tests/integration/test_closed_loop.py` — full loop in static mode.
- `tests/integration/test_pipeline.py` — worker success, worker-failure→failed,
  ffprobe-missing-fails-fast, static processor.
- `tests/integration/test_api_smoke.py` — health/analyze/job/search/details +
  source_uri-not-exposed.
- `tests/cli/test_cli_smoke.py` — init/analyze --wait/worker --once/search.
- `tests/search/test_search_service.py` — authorized search/details/locator/overlap.

---

## 5. What is mock vs real (do not overstate)

| Concern | State |
|---|---|
| VLM semantics | MOCK (deterministic `[mock]` text), no real vision |
| Video duration (ffprobe) | REAL but bounded; or `static` mode (no subprocess) |
| Frame extraction | placeholder URIs, no real decode |
| Search ranking | SQL LIKE keyword/tag score; NO vector/RRF/OpenSearch |
| playback_url | placeholder token; NO real playback endpoint |
| Analysis scales producing records | default_segment (always) + high_freq_event (opt-in via enable_motion_triggered_high_freq); motion_scan produces HighFreqTriggers only |
| Motion detection | REAL frame-difference (ffmpeg, bounded) driving high_freq_event triggers |
| Auth | dev principal + header/CLI; NO passwords/JWT/sessions |
| Write-path separation | interface-level; not OS/connection-enforced |
