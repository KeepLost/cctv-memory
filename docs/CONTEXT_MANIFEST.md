# CONTEXT_MANIFEST

## 0. Purpose

This file is the entry point for anyone developing CCTV Memory. It defines which documents to read, their authority order, and how to resolve conflicts.

If a task touches architecture, data model, permissions, search, storage, API behavior, analysis pipeline behavior, configuration, or operations, start here.

## 1. Documentation Layout

- `docs/`: durable, future-facing project knowledge: architecture constitution, contracts, specs, design references, runbooks, prompt references, usage/development guides, ADR index, and test-data provenance.
- `status/`: active OpenCode/OpenClaw communication for the current task only: task spec, confirmations, progress, kickoff, current questions, and current final reports.
- `status/archive/`: historical investigations, completed implementation reports, superseded plans, old task specs, incidents, and prior execution/architecture reports.

## 2. Authority Order

When documents conflict, use this order:

```text
1. docs/ARCHITECTURE_CONSTITUTION.md
2. docs/contracts/*-contract.md and docs/contracts/*-spec.md
3. docs/contracts/api-routes.md
4. docs/design/api-and-service-runtime-design.md
5. docs/design/architecture-contracts-and-tech-stack.md
6. docs/design/data-storage-and-retrieval-design.md
7. current status/task-spec.md for the active task
8. status/archive/ historical notes, investigations, and old reports
```

Rules:

- Contract/spec docs are implementation authority.
- Durable design docs preserve architecture intent, rationale, and compatibility expectations.
- `status/archive/` is evidence and context, not exact current authority.
- Chat history is never a stable source of truth once a contract/spec document exists.
- If two contract/spec docs conflict, stop and ask for clarification or update the docs before implementing.

## 3. Must-Read for Any Development

```text
docs/ARCHITECTURE_CONSTITUTION.md
docs/DEVELOPMENT.md
docs/INDEX.md
docs/contracts/schema-contracts.md
docs/contracts/module-map.md
docs/contracts/repository-port-contract.md
docs/contracts/authorization-policy-contract.md
docs/contracts/search-contract.md
docs/contracts/database-capability-contract.md
docs/contracts/database-adapter-contract.md
docs/contracts/table-schema-spec.md
docs/contracts/error-code-contract.md
docs/contracts/testing-contract.md
docs/contracts/api-routes.md
docs/contracts/pipeline-experiment-contract.md
docs/contracts/nonfunctional-requirements.md
```

## 4. Must-Read by Area

### API / Client / Tool Proxy

```text
docs/SERVER_CLIENT_BOUNDARY.md
docs/client-retrieval-experiment-guide.md
docs/design/api-and-service-runtime-design.md
docs/contracts/api-routes.md
docs/contracts/schema-contracts.md
docs/contracts/error-code-contract.md
docs/contracts/authorization-policy-contract.md
```

Notes:

- `docs/SERVER_CLIENT_BOUNDARY.md` defines this repo as server plus ops CLI. The client is a separate deliverable using HTTP `/api/v1` only.
- `docs/contracts/api-routes.md` marks each route implemented or designed-not-implemented.
- The route set and error-code vocabulary are snapshot-frozen by `tests/architecture/test_api_contract.py`.

### Database / Repository / Migration

```text
docs/contracts/database-capability-contract.md
docs/contracts/database-adapter-contract.md
docs/contracts/table-schema-spec.md
docs/contracts/repository-port-contract.md
docs/contracts/backup-export-contract.md
```

### Search / Retrieval / Locator

```text
docs/contracts/search-contract.md
docs/contracts/authorization-policy-contract.md
docs/contracts/schema-contracts.md
docs/contracts/database-capability-contract.md
```

### VLM / Video Processing / Pipeline

```text
docs/contracts/vlm-analysis-contract.md
docs/contracts/job-state-machine-contract.md
docs/contracts/schema-contracts.md
docs/design/data-storage-and-retrieval-design.md
docs/prompts/default_segment_v1.md
docs/prompts/high_freq_event_v1.md
```

### Worker / Task Queue / Publication

```text
docs/contracts/job-state-machine-contract.md
docs/contracts/repository-port-contract.md
docs/contracts/database-capability-contract.md
docs/contracts/table-schema-spec.md
docs/contracts/error-code-contract.md
```

### Config / Deployment / Backup

```text
docs/contracts/configuration-contract.md
docs/contracts/backup-export-contract.md
docs/design/api-and-service-runtime-design.md
docs/DEVELOPMENT.md
docs/USAGE.md
```

### Pipeline Experiment / Performance

```text
docs/contracts/pipeline-experiment-contract.md
docs/contracts/nonfunctional-requirements.md
docs/baselines/performance-baseline.md
docs/prompts/default_segment_v1.md
```

## 5. Background References

These are useful for rationale, but contract/spec docs win when details conflict:

```text
docs/design/data-storage-and-retrieval-design.md
docs/design/architecture-contracts-and-tech-stack.md
docs/design/api-and-service-runtime-design.md
docs/prompts/
status/archive/
```

## 6. Conflict Handling

If implementation finds a conflict:

1. Stop the coding task.
2. Identify the conflicting documents and sections.
3. Propose the smallest contract/spec update.
4. Do not resolve by hidden implementation assumptions.
5. Resume only after the contract/spec is updated or explicitly waived.

## 7. Definition of Ready for Coding Tasks

A coding task is ready only if it states:

```text
scope
files/modules allowed to change
must-read docs
acceptance tests
security/permission constraints
migration impact
out-of-scope items
```

If any of these are missing for non-trivial work, ask for clarification or create a task spec before coding.
