# Server / Client Boundary

> Authority: complements `docs/ARCHITECTURE_CONSTITUTION.md` §3 and
> `docs/design/api-and-service-runtime-design.md` §2/§6/§7. This doc states what THIS
> repository is responsible for, what it is not, and the seams a future client
> must use. It exists to prevent the "adapt forever on a mud-ball" failure mode of
> the previous system.

## 1. What this repository is

This repository is the **server** of CCTV Memory, plus an **in-process operations
CLI**. Concretely it provides:

- the FastAPI HTTP application (`cctv_memory/api/app.py`, served by
  `cctv-memory serve`);
- server-side authorization + authorized-scope computation
  (`application/auth.py` over `domain/policies.py`);
- ingestion / analysis / search / locator / playback / backup application
  services and their infrastructure adapters;
- an ops CLI (`cctv_memory/cli`) that drives those services **in-process** for
  admin, debugging, batch, and reproduction (constitution §2 "limited CLI admin
  operations").

The HTTP API is the only contract intended for external programs. The ops CLI is
a server-side tool, not the designed client.

## 2. What this repository is NOT (out of scope)

- **Not the client.** The designed client (SDK / tool proxy / client CLI) that
  authenticates, holds tokens, and calls the server over HTTP is a **separate
  deliverable**. No client code lives here (enforced by
  `tests/architecture/test_dependencies.py::test_no_client_component_lives_in_server_repo`).
- **Not a token-auth provider yet.** Production token issuance/verification and
  the `/api/v1/auth/*` routes are designed but not implemented (see §4).

## 3. How a future client must interact with the server

These are the seams the server now guarantees so a client can be built cleanly:

1. **HTTP `/api/v1` only.** The client talks to the server exclusively over the
   versioned HTTP API. It MUST NOT open the SQLite database directly
   (runtime-design §6.2.3). Breaking the API version requires `/api/v2`.
2. **Identity is header-borne, never in the body.** The client attaches identity
   as a header (today `X-Principal-Id` for the dev verifier; in production an
   `Authorization: Bearer <token>`). The request body never carries
   `principal_id` / `role` / `access_policy` / `security_level`
   (runtime-design §2.2). The server resolves identity at a single seam — the
   `AuthVerifierPort` (`cctv_memory/services/auth_verifier.py`).
3. **Business parameters only.** The client/AI passes business query parameters
   (query text, camera/location/time, analysis-scale filters, top_k). It MUST NOT
   pass ranking weights or other server-internal tuning knobs — those are tuned
   server-side and frozen as defaults (see §5).
4. **Generated from the OpenAPI contract.** Every route declares typed request
   bodies and the unified `ApiSuccessEnvelope` / `ApiErrorEnvelope` responses, so
   `/openapi.json` is complete and a client SDK can be generated from it. The
   route set and error-code vocabulary are snapshot-frozen
   (`tests/architecture/test_api_contract.py`); changing them is a deliberate,
   reviewed, client-affecting change.
5. **Stable error codes.** The client maps server error codes (see
   `docs/contracts/error-code-contract.md`) to tool errors. The server never leaks stack
   traces or internal paths.

## 4. The authentication seam (designed vs implemented)

| Concern | Status | Where |
|---|---|---|
| Identity resolution port | ✅ implemented | `services/auth_verifier.py` (`AuthVerifierPort`) |
| Dev trusting verifier (reads `X-Principal-Id` / default) | ✅ implemented | `infrastructure/auth/dev_verifier.py` |
| Verifier injection at composition root | ✅ implemented | `bootstrap.py::build_app` |
| Token issuance/verification, `/api/v1/auth/*` | 🟡 designed, not implemented | future; plug into `AuthVerifierPort` |

Production hardening swaps a token verifier in at the `AuthVerifierPort` seam.
The api/application/domain layers do not change: they only consume a resolved
`principal_id` (verifier) and `principal` (`AuthorizationService`). A guardrail
test forbids application/domain from importing the verifier implementation.

## 5. Tuning belongs to the server, not the client

All parameter tuning is a **server-side internal activity** that produces "default
best parameters"; the client/AI never sees or sends these:

- ingestion/analysis side: prompt version, slicing/frame-sampling, motion
  thresholds;
- retrieval side: ranking/RRF weights (`SearchWeightConfig`), which are injected
  into `SearchService` by the experiment runner and are **not** request-body
  fields.

Tuning is run via the server `experiment` / `benchmark` commands. The client-side
retrieval experiment (end-to-end latency, AI tool ergonomics, end-to-end
precision/recall over the real HTTP path) is a different activity — see
`docs/client-retrieval-experiment-guide.md`.
