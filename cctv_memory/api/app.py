"""FastAPI application factory (api/app.py).

The app receives a ``ServicesProvider`` (built by the composition root) so this
module never imports infrastructure concretes — preserving the dependency
direction (ARCHITECTURE_CONSTITUTION §3; architecture tests forbid
api -> infrastructure / sqlalchemy).

Identity is resolved through an ``AuthVerifierPort`` (auth §0, runtime-design
§2.2): credentials are extracted from request headers (NEVER the request body)
and verified into a ``principal_id``. The default wiring uses a dev trusting
verifier (reads ``X-Principal-Id`` or a configured default); production can swap
in a token verifier without touching application/domain code.

Request/response shapes are declared with Pydantic models so ``/openapi.json``
carries a complete contract (request bodies + the unified success/error
envelope), which a future client SDK / tool proxy can generate from. The handlers
still return the hand-built envelope ``JSONResponse`` (so the runtime envelope is
byte-for-byte unchanged); ``response_model`` is documentation-only.
All responses use the unified envelope (schema-contracts §1.4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cctv_memory.api.errors import map_exception
from cctv_memory.application.request_services import ServicesProvider
from cctv_memory.contracts.backup import AdminBackupRequest
from cctv_memory.contracts.common import ApiErrorEnvelope, ApiSuccessEnvelope
from cctv_memory.contracts.search import (
    BatchRefineObservationSearchRequest,
    LocatorRequest,
    ObservationDetailsRequest,
    OverlappingRecordsRequest,
    RefineObservationSearchRequest,
    StartObservationSearchRequest,
)
from cctv_memory.contracts.video import SubmitVideoSourceRequest
from cctv_memory.services.auth_verifier import AuthVerifierPort, RequestCredentials
from cctv_memory.services.timeline_recorder import TimelineRecorder

SCHEMA_VERSION = "v1"

# Documentation-only response map: every route advertises the unified envelope so
# the OpenAPI contract is complete for client codegen. Handlers return the
# hand-built JSONResponse, so these models never alter the runtime response.
_ENVELOPE_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorEnvelope, "description": "Validation or request error."},
    403: {"model": ApiErrorEnvelope, "description": "Authorization / capability error."},
    404: {"model": ApiErrorEnvelope, "description": "Resource not found."},
    409: {"model": ApiErrorEnvelope, "description": "State / idempotency conflict."},
    422: {"model": ApiErrorEnvelope, "description": "Schema validation failed."},
    500: {"model": ApiErrorEnvelope, "description": "Internal error."},
}


def _meta() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "server_time": datetime.now(UTC).isoformat()}


def _success(request_id: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "request_id": request_id, "data": data, "meta": _meta()}


def _error(
    request_id: str, code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": request_id,
        "error": {"code": code, "message": message, "details": details or {}},
        "meta": _meta(),
    }


def _items(models: Any) -> dict[str, Any]:
    return {"items": [m.model_dump(mode="json") for m in models]}


class _DefaultHeaderVerifier:
    """Inline dev fallback verifier (api-layer, no infrastructure dependency).

    Used only when ``create_app`` is called without an injected verifier. Mirrors
    ``infrastructure.auth.dev_verifier.TrustingHeaderVerifier``: trust the
    ``X-Principal-Id`` header or fall back to the default principal. Production /
    standard wiring injects a verifier via the composition root instead.
    """

    def __init__(self, default_principal_id: str) -> None:
        self._default_principal_id = default_principal_id

    def verify(self, credentials: RequestCredentials) -> str:
        return credentials.principal_id_header or self._default_principal_id



def _rid(x_request_id: str | None) -> str:
    return x_request_id or f"req_{uuid.uuid4().hex[:16]}"


def create_app(
    provider: ServicesProvider,
    *,
    default_principal_id: str = "user_admin",
    auth_verifier: AuthVerifierPort | None = None,
    vlm_provider: str = "mock",
    indexing_provider: str = "mock",
    indexing_enabled: bool = False,
    timeline_recorder: TimelineRecorder | None = None,
) -> FastAPI:
    """Build the FastAPI app wired to a services provider.

    ``auth_verifier`` resolves request credentials -> principal_id. When omitted,
    a dev trusting verifier is used (reads ``X-Principal-Id`` or
    ``default_principal_id``) so behavior is identical to the previous header
    resolver; production wiring injects a real token verifier here.

    ``vlm_provider`` / ``indexing_provider`` / ``indexing_enabled`` reflect the
    ACTIVE runtime configuration so ``/health`` reports the real selection
    (``mock`` vs ``real``) instead of a hardcoded value. They are non-sensitive
    selection labels only (no keys/urls), safe to expose.
    """
    # The api layer must not import infrastructure (architecture rule §3). The
    # composition root (bootstrap) injects the real dev/prod verifier. This inline
    # fallback only covers a direct ``create_app`` call without a verifier and
    # mirrors the dev trusting behavior (header or default principal).
    verifier: AuthVerifierPort = auth_verifier or _DefaultHeaderVerifier(
        default_principal_id
    )
    timeline = timeline_recorder or TimelineRecorder.disabled()

    app = FastAPI(title="CCTV Memory", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def _on_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Preserve the historical contract: malformed/typed-body failures map to a
        # 400 ``validation_error`` envelope (NOT FastAPI's default 422 plain body).
        rid = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:16]}"
        return JSONResponse(
            _error(rid, "validation_error", "Request validation failed."),
            status_code=400,
        )

    def resolve_principal_id(x_principal_id: str | None) -> str:
        return verifier.verify(RequestCredentials(principal_id_header=x_principal_id))

    def _model_data(model: Any) -> Any:
        return model.model_dump(mode="json")

    @app.get(
        "/api/v1/health",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def health(x_request_id: str | None = Header(default=None)) -> JSONResponse:
        rid = _rid(x_request_id)
        data = {
            "status": "ok",
            "version": "0.1.0",
            "schema_version": SCHEMA_VERSION,
            "vlm_provider": vlm_provider,
            "indexing_provider": indexing_provider,
            "vector_search_enabled": indexing_enabled,
            "mode": "mvp-closed-loop",
        }
        return JSONResponse(_success(rid, data))

    @app.post(
        "/api/v1/video-sources/analyze",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def analyze(
        body: SubmitVideoSourceRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                resp = svc.ingestion.submit(body, principal, capabilities=scope.capabilities)
            timeline.event(
                "request_accepted",
                analysis_job_id=resp.analysis_job_id,
                video_id=resp.video_id,
                status="accepted",
                metadata={
                    "request_id": rid,
                    "source_type": body.source_type.value,
                    "principal_id": principal.principal_id,
                },
            )
            timeline.event(
                "task_queued",
                analysis_job_id=resp.analysis_job_id,
                video_id=resp.video_id,
                status="queued",
                metadata={"request_id": rid},
            )
            return JSONResponse(_success(rid, _model_data(resp)))
        except Exception as exc:  # noqa: BLE001 - mapped to envelope
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.get(
        "/api/v1/analysis-jobs/{analysis_job_id}",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def get_job(
        analysis_job_id: str,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                job = svc.jobs.get_job(analysis_job_id)
                if job is None:
                    return JSONResponse(
                        _error(rid, "not_found", "Analysis job not found."),
                        status_code=404,
                    )
                data = _model_data(job)
            return JSONResponse(_success(rid, data))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/observation-search/contexts",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def start_search(
        body: StartObservationSearchRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                resp = svc.search.start_search(body, scope, request_id=rid)
            return JSONResponse(_success(rid, _model_data(resp)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/observation-search/contexts/{context_id}/refine",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def refine_search(
        context_id: str,
        body: RefineObservationSearchRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                resp = svc.search.refine_search(context_id, body, scope, request_id=rid)
            return JSONResponse(_success(rid, _model_data(resp)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.get(
        "/api/v1/observation-search/contexts/{context_id}/facets",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def facets(
        context_id: str,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                data = svc.search.facets(context_id, scope)
            return JSONResponse(_success(rid, data))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.delete(
        "/api/v1/observation-search/contexts/{context_id}",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def close_context(
        context_id: str,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                svc.search.close_context(context_id, scope)
            return JSONResponse(_success(rid, {"context_id": context_id, "closed": True}))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/observation-search/details",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def details(
        body: ObservationDetailsRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                items = svc.locator.get_details(body, scope, request_id=rid)
            return JSONResponse(_success(rid, _items(items)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/observation-search/overlapping-records",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def overlapping(
        body: OverlappingRecordsRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                items = svc.locator.get_overlapping(body, scope, request_id=rid)
            return JSONResponse(_success(rid, _items(items)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/observation-search/contexts/{context_id}/batch-refine",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def batch_refine(
        context_id: str,
        body: BatchRefineObservationSearchRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                responses = svc.search.batch_refine_search(
                    context_id, body.refinements, scope, request_id=rid
                )
            return JSONResponse(
                _success(rid, {"items": [r.model_dump(mode="json") for r in responses]})
            )
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/observation-search/locators",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def locators(
        body: LocatorRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                projections = svc.playback.issue_locators(
                    body.record_ids, scope, request_id=rid
                )
            return JSONResponse(_success(rid, _items(projections)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.get(
        "/api/v1/playback/{token}",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def playback(
        token: str,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                descriptor = svc.playback.verify_playback(token, scope, request_id=rid)
            data = {
                "record_id": descriptor.record_id,
                "video_id": descriptor.video_id,
                "camera_id": descriptor.camera_id,
                "segment_start_ms": descriptor.segment_start_ms,
                "segment_end_ms": descriptor.segment_end_ms,
                "expires_at": descriptor.expires_at.isoformat(),
            }
            return JSONResponse(_success(rid, data))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/admin/backups",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def admin_backup(
        body: AdminBackupRequest,
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                manifest = svc.backup.admin_backup(body.out_path, scope, request_id=rid)
            return JSONResponse(_success(rid, _model_data(manifest)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/exports/user",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def user_export(
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                bundle = svc.backup.user_export(scope, request_id=rid)
            return JSONResponse(_success(rid, _model_data(bundle)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    @app.post(
        "/api/v1/exports/migration",
        response_model=ApiSuccessEnvelope,
        responses=_ENVELOPE_RESPONSES,
    )
    def migration_export(
        x_principal_id: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        rid = _rid(x_request_id)
        try:
            with provider() as svc:
                principal = svc.auth.resolve_principal(resolve_principal_id(x_principal_id))
                scope = svc.auth.authorized_scope_for(principal)
                bundle = svc.backup.migration_export(scope, request_id=rid)
            return JSONResponse(_success(rid, _model_data(bundle)))
        except Exception as exc:  # noqa: BLE001
            status, code, message = map_exception(exc)
            return JSONResponse(_error(rid, code, message), status_code=status)

    return app
