# CCTV Memory

CCTV Memory turns recorded surveillance video into searchable natural-language observation records. It provides a FastAPI server and operations CLI for ingestion, VLM analysis, authorized search, details, locators, playback tokens, backup, and export.

The project is server-side only. External clients should use the versioned HTTP API under `/api/v1`; they must not read database files directly.

## What It Does

- Ingests pre-recorded video files or externally prepared chunks.
- Runs configurable analysis pipelines that produce observation records.
- Stores active observations with server-side authorization metadata.
- Supports structured search, refine, facets, details, overlap, locator, and playback flows.
- Supports SQLite by default and PostgreSQL + pgvector for live PostgreSQL validation or production trials.
- Supports offline mock embedding/rerank providers by default and real providers through environment variables.

## Quick Start

Requirements:

- Python 3.12+
- `uv`
- Optional `ffmpeg` / `ffprobe` for real media processing
- Optional PostgreSQL with pgvector for the PostgreSQL backend

Install locally:

```bash
uv venv
uv pip install -e ".[dev]"
```

Inspect readiness:

```bash
uv run cctv-memory doctor
```

Initialize local SQLite storage:

```bash
uv run cctv-memory init --data-dir ./data
```

Run the API server:

```bash
uv run cctv-memory serve --data-dir ./data --host 127.0.0.1 --port 8080
```

Run tests:

```bash
uv run pytest
```

## Configuration

`config.yaml` at the repository root is a safe example configuration. The app loads `./config.yaml` by default when present, or the file named by `CCTV_MEMORY_CONFIG_FILE`.

Configuration precedence is:

```text
CLI/init args > environment variables > config.yaml > built-in defaults
```

Secrets must be provided through environment variables, not committed YAML. The config file stores only environment variable names such as `CCTV_MEMORY_POSTGRES_DSN`, `LLM_KEY`, `CCTV_MEMORY_EMBEDDING_API_KEY`, and `CCTV_MEMORY_RERANK_API_KEY`.

For PostgreSQL + pgvector, set:

```yaml
database:
  backend: postgres
  postgres_dsn_env: CCTV_MEMORY_POSTGRES_DSN
indexing:
  enabled: true
  provider: mock
  rerank_enabled: true
  rerank_provider: mock
```

Then export the DSN outside the repo:

```bash
export CCTV_MEMORY_POSTGRES_DSN='postgresql+psycopg://user:password@localhost:5432/cctv_memory'
```

Use real embedding or rerank providers only after setting the configured key and endpoint environment variables.

## Search

Search is exposed through the HTTP API and a local ops CLI. External clients should use HTTP `/api/v1`; the CLI is for server-side local operation and debugging.

Basic local search after records exist:

```bash
uv run cctv-memory search --data-dir ./data --query "red car near gate" --top-k 10
```

HTTP search flow:

```text
POST /api/v1/observation-search/contexts          start a SearchContext and first revision
POST /api/v1/observation-search/contexts/{ctx}/refine
GET  /api/v1/observation-search/contexts/{ctx}/facets
POST /api/v1/observation-search/details
POST /api/v1/observation-search/overlapping-records
DELETE /api/v1/observation-search/contexts/{ctx}
```

Start-search accepts business parameters such as `query_text`, `time_range`, `camera_ids`, `location_ids`, `video_ids`, `tag_filters`, `analysis_scale_filter`, `preferred_analysis_scales`, `search_mode`, and `top_k`. Clients should not send ranking weights; tune those in server config.

Search tuning lives in `config.yaml` under `search` and `indexing`:

```yaml
search:
  rrf_k: 60
  static_weight: 1.0
  dynamic_weight: 1.0
  fts_weight: 0.5
  vector_weight: 1.0
indexing:
  enabled: true
  provider: real
  embedding_model: BAAI/bge-m3
  embedding_dimensions: 1024
  rerank_enabled: true
  rerank_provider: real
  rerank_model: Qwen/Qwen3-Reranker-8B
```

For the SiliconFlow-compatible `BAAI/bge-m3` endpoint, `embedding_dimensions: 1024` is the storage/schema expectation. The upstream embedding request should not send an explicit `dimensions` field unless the live API is known to accept it.

After changing the embedding model or enabling vector search for existing records, rebuild stored vectors:

```bash
uv run cctv-memory reindex --data-dir ./data
```

Search uses a persisted context cache: `SearchContext` stores the active session, each operation creates an immutable `SearchRevision`, and cached `SearchCandidate` rows are reused by refine and facets. Close old contexts when a client is done with them.

Detailed search references:

- `docs/contracts/search-contract.md`: request fields, search modes, refine ops, scoring, facets, SearchContext cache lifecycle, and authorization rules.
- `docs/contracts/api-routes.md`: implemented search HTTP routes.
- `docs/contracts/configuration-contract.md`: search and indexing config fields.
- `docs/SERVER_CLIENT_BOUNDARY.md`: what clients may send and what stays server-side.
- `docs/operations/runbook-video-analysis-flow.md`: end-to-end local analyze/search runbook.

## Documentation

Detailed documentation lives under `docs/`:

- `docs/CONTEXT_MANIFEST.md`: documentation map and authority order.
- `docs/ARCHITECTURE_CONSTITUTION.md`: non-negotiable architecture and security rules.
- `docs/DEVELOPMENT.md`: local setup, testing, and workflow.
- `docs/USAGE.md`: verified usage guide.
- `docs/SERVER_CLIENT_BOUNDARY.md`: server/client responsibility boundary.
- `docs/contracts/`: API, search, database, configuration, schema, auth, and testing contracts.
- `docs/design/`: durable design rationale.
- `docs/operations/`: operational runbooks.

## Security Notes

- Do not commit API keys, passwords, PostgreSQL DSNs with credentials, tokens, or private media paths.
- User-visible search, facets, details, overlap, and locator flows must apply server-side authorization before ranking or projection.
- `source_uri` is internal and must not be exposed to clients.
- Production auth is designed as a verifier seam; local development uses a trusting header verifier for MVP workflows.
