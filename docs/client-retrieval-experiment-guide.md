# Client Retrieval Experiment Guide (skill seed)

> Status: forward-looking guide. The client does not exist yet; this is the
> blueprint for the retrieval experiment that MUST run through a client once the
> client is built. It also clarifies the split from server-side tuning. Pair with
> `docs/SERVER_CLIENT_BOUNDARY.md` and `docs/contracts/pipeline-experiment-contract.md`.

## 1. Why two different experiments

Retrieval quality work splits into two activities that run in two places. Mixing
them produces the leaky, hard-to-maintain interfaces we are trying to avoid.

| | Server-side tuning experiment | Client retrieval experiment |
|---|---|---|
| Question | "How good can the ranking get?" | "How good/fast is it for the AI through the client?" |
| What varies | internal ranking weights (RRF k, static/dynamic/fts weights, tag/scale boosts), prompt/slicing/motion params | business query params only (query text, camera/location/time, scale filters, top_k) |
| Path | in-process `experiment` / `benchmark` (reads candidate pool directly) | client → HTTP `/api/v1` → server |
| Output | the default best parameters (frozen server-side) | end-to-end latency, AI tool ergonomics, end-to-end precision/recall; guidance docs |
| Who sees the knobs | server only | nobody — AI/client never sends weights |

Rule: **ranking-weight tuning stays server-side** (`SearchWeightConfig` is
injected into `SearchService`, never a request-body field). The client experiment
must not try to pass weights.

## 2. What the client retrieval experiment measures

1. **End-to-end latency** — including HTTP round-trip, (de)serialization, and the
   auth verifier overhead. This is invisible to the in-process `benchmark`; it can
   only be measured through the client path.
2. **AI tool ergonomics** — how the AI-facing tool semantics behave in practice:
   how to phrase `query_text`, whether `camera_ids` / `time_range` are needed,
   how empty results read, whether error codes are understandable. These findings
   feed the guidance/skill docs.
3. **End-to-end precision/recall** — run a fixed golden query set over the full
   client→server path and confirm the retrieval quality matches what server-side
   tuning achieved (i.e. the frozen default weights hold up in production shape).

## 3. How it should work (once a client exists)

1. Seed a deterministic corpus (mock-VLM records) on the server, exactly as the
   server-side benchmark does, so relevance is known.
2. Use the **same golden query set** as the server benchmark
   (`GoldenQuery` / `ExperimentConfig.queries`), but drive it through the client:
   client attaches identity header → calls
   `POST /api/v1/observation-search/contexts` (and `/refine`, `/facets`,
   `/details`) → collects ranked `record_id`s.
3. Compute precision@k / recall@k / MRR with the same `domain.metrics` definitions
   the server benchmark uses, so numbers are comparable.
4. Record wall-clock latency per call (p50/p95) separately from quality.
5. Emit a structured report (mirror `ExperimentResult`) plus a short narrative of
   tool-ergonomics findings.

## 4. Boundaries the client experiment must respect

- HTTP `/api/v1` only; never read the SQLite DB directly.
- Identity via header only (dev: `X-Principal-Id`; prod: `Authorization`); never
  in the body.
- Business query params only; no ranking weights, no `principal_id`/role/policy.
- Empty result handling: report "no matching records in your authorized scope",
  not "filtered/hidden" hints (runtime-design §6.4).

## 5. Server readiness for this experiment (already in place)

- Complete OpenAPI contract (typed request bodies + envelope responses) for client
  codegen — `/openapi.json`.
- `AuthVerifierPort` identity seam (dev trusting verifier today).
- Snapshot-frozen route set + error-code vocabulary
  (`tests/architecture/test_api_contract.py`).
- Server-side `experiment` / `benchmark` already produce the frozen default
  weights this experiment validates end-to-end.

## 6. Promotion to a real skill

When the client is built, promote this guide into an executable skill: a thin
client-side experiment harness that consumes `/openapi.json`, runs the golden set
over HTTP, and emits the comparable metrics + latency report described above.
