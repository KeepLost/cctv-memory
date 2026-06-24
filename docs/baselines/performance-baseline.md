# Performance Baseline

- date: 2026-06-09
- task: cctv-memory-20260609-0818-phase5-phase6
- mode: mock VLM + static video metadata (no ffprobe, no network); SQLite WAL.
- machine: development container (single process, embedded worker).
- NOTE: This is a documented STARTING POINT, not a hard gate
  (nonfunctional-requirements §6). Numbers reflect the mock pipeline + FTS search,
  not real VLM latency (real VLM would dominate; see vlm-analysis-contract).

## Method

Single in-process run via `cctv_memory.infrastructure.runtime`:
- 120s synthetic video, default_segment window=12s / overlap=3s → 13 segments.
- mock VLM (deterministic), atomic publication with FTS5 indexing.
- one hybrid search (`query_text="person backpack"`, top_k=10).

All measurements were taken with bounded execution (no blocking subprocess).

## Results (single sample)

| Operation | Latency | Notes |
|---|---|---|
| Submit video source (ingest) | ~15 ms | create VideoSource+Job+scale task+queue task |
| Worker drain (13 segments) | ~43 ms | probe(static)+segment+mockVLM+atomic publish+FTS index |
| Hybrid search (start, top_k=10) | ~15 ms | authorized pool + FTS rank + RRF + persist context/revision/candidates |
| Records produced | 13 | one default_segment record per window |

Derived: ~3.3 ms/segment end-to-end (mock VLM + publish + FTS index).

## Interpretation vs targets (nonfunctional-requirements §2)

- Start search target P95 < 500ms (FTS): observed ~15ms at this tiny scale — far
  under target, but dataset is small (13 records). Re-measure at ~500K records
  before claiming the target is met at scale.
- Submit video source target < 300ms: observed ~15ms — under target.
- VLM single-segment 5–60s: N/A here (mock VLM is sub-ms). Real VLM will dominate
  the worker time once a real provider is wired; the mock number is not predictive
  of production analysis latency.

## Caveats (honest)

- Mock VLM does no real inference; worker timing is not representative of
  production analysis (which is VLM-bound).
- Single sample, tiny dataset, warm process. Not a statistical P95.
- FTS search latency at MVP target scale (≤500K records) is not yet measured;
  flagged for a future scaled benchmark (nonfunctional-requirements §5/§6).

## How to reproduce

```bash
export CCTV_MEMORY_PIPELINE__VIDEO_METADATA_MODE=static
export CCTV_MEMORY_PIPELINE__STATIC_DURATION_MS=120000
# init a temp data dir, analyze --wait, then `cctv-memory benchmark run`
# (see docs/operations/runbook-video-analysis-flow.md for the safe, bounded command forms)
```
