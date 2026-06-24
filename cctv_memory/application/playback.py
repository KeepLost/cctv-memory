"""Playback token service (application/playback.py).

Issues and verifies short-TTL, signed playback tokens for locator/playback
(authorization-policy-contract §8, api-routes §5). A token binds
principal/record/video/segment/expiry and is HMAC-signed so it cannot be forged
or replayed by a different principal. Verifying a token performs a SECOND
authorization check: the bound record is re-fetched under the caller's live
AuthorizedScope before any playback descriptor is returned. The internal
``source_uri`` is NEVER included (ARCHITECTURE_CONSTITUTION §5).

The signing key is provided by the composition root (an env-var value or a
per-process random key); it is never committed. There is no real media streaming
in the MVP — verification returns an authorized playback descriptor (segment
bounds + short-TTL), an honest placeholder, not a file path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cctv_memory.contracts.audit import AuditEvent
from cctv_memory.contracts.auth import AuthorizedScope
from cctv_memory.contracts.search import LocatorProjection
from cctv_memory.domain.enums import Capability
from cctv_memory.domain.exceptions import CapabilityDeniedError, NotFoundError
from cctv_memory.repositories.audit import AuditRepository
from cctv_memory.repositories.observation import ObservationRecordReadRepository

_PLAYBACK_TTL_SECONDS = 600


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True)
class PlaybackDescriptor:
    """An authorized playback descriptor (no source_uri; MVP placeholder)."""

    record_id: str
    video_id: str
    camera_id: str
    segment_start_ms: int
    segment_end_ms: int
    expires_at: datetime


class PlaybackTokenService:
    """Issue + verify signed, short-TTL playback tokens (2nd authz on verify)."""

    def __init__(
        self,
        observations: ObservationRecordReadRepository,
        audit: AuditRepository,
        *,
        signing_key: str,
        ttl_seconds: int = _PLAYBACK_TTL_SECONDS,
    ) -> None:
        self._observations = observations
        self._audit = audit
        self._key = signing_key.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def issue_locators(
        self,
        record_ids: list[str],
        scope: AuthorizedScope,
        *,
        request_id: str | None = None,
    ) -> list[LocatorProjection]:
        """Issue locator projections (with signed playback tokens) for records.

        Requires ``observation.read_locator`` (the second, distinct capability).
        Each record is authorized under ``scope`` (unauthorized records are simply
        omitted — never surfaced). No ``source_uri`` ever appears.
        """
        if Capability.OBSERVATION_READ_LOCATOR not in scope.capabilities:
            raise CapabilityDeniedError("observation.read_locator required")
        records = self._observations.get_authorized_active_by_ids(record_ids, scope)
        now = _now()
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        projections: list[LocatorProjection] = []
        for rec in records:
            token = self._sign(
                principal_id=scope.principal_id,
                record_id=rec.record_id,
                video_id=rec.video_id,
                segment_start_ms=rec.segment_start_ms,
                segment_end_ms=rec.segment_end_ms,
                expires_at=expires_at,
            )
            self._audit.append_event(
                AuditEvent(
                    audit_event_id=_new_id("audit"),
                    event_type="locator",
                    request_id=request_id,
                    principal_id=scope.principal_id,
                    resource_scope_hash=scope.scope_hash,
                    record_ids=[rec.record_id],
                    video_id=rec.video_id,
                    camera_id=rec.camera_id,
                    metadata={"issued": True},
                )
            )
            projections.append(
                LocatorProjection(
                    video_id=rec.video_id,
                    camera_id=rec.camera_id,
                    segment_start_ms=rec.segment_start_ms,
                    segment_end_ms=rec.segment_end_ms,
                    absolute_start_time=rec.observed_start_time,
                    absolute_end_time=rec.observed_end_time,
                    playback_url=f"/api/v1/playback/{token}",
                    thumbnail_url=None,
                    expires_at=expires_at,
                )
            )
        return projections

    def verify_playback(
        self, token: str, scope: AuthorizedScope, *, request_id: str | None = None
    ) -> PlaybackDescriptor:
        """Verify a playback token + RE-AUTHORIZE the bound record (2nd authz).

        Raises ``NotFoundError`` if the signature is invalid, the token is
        expired, the token's principal differs from the caller, or the bound
        record is no longer authorized under the live scope (all表现为 not_found so
        existence is never leaked). Requires ``video.playback``.
        """
        if Capability.VIDEO_PLAYBACK not in scope.capabilities:
            raise CapabilityDeniedError("video.playback required")
        payload = self._verify(token)
        if payload is None:
            raise NotFoundError("playback token invalid or expired")
        if payload.get("principal_id") != scope.principal_id:
            # Token issued for a different principal — treat as not found.
            raise NotFoundError("playback token invalid or expired")
        record_id = str(payload.get("record_id"))
        # Second authorization: the record must still be authorized for the caller.
        record = self._observations.get_authorized_active_by_id(record_id, scope)
        if record is None:
            raise NotFoundError("playback target not found")
        exp_value = payload["exp"]
        exp_ts = float(exp_value) if isinstance(exp_value, (int, float)) else 0.0
        self._audit.append_event(
            AuditEvent(
                audit_event_id=_new_id("audit"),
                event_type="playback_url_issued",
                request_id=request_id,
                principal_id=scope.principal_id,
                resource_scope_hash=scope.scope_hash,
                record_ids=[record.record_id],
                video_id=record.video_id,
                camera_id=record.camera_id,
                metadata={"verified": True},
            )
        )
        return PlaybackDescriptor(
            record_id=record.record_id,
            video_id=record.video_id,
            camera_id=record.camera_id,
            segment_start_ms=record.segment_start_ms,
            segment_end_ms=record.segment_end_ms,
            expires_at=datetime.fromtimestamp(exp_ts, tz=UTC),
        )

    def _sign(
        self,
        *,
        principal_id: str,
        record_id: str,
        video_id: str,
        segment_start_ms: int,
        segment_end_ms: int,
        expires_at: datetime,
    ) -> str:
        payload = {
            "principal_id": principal_id,
            "record_id": record_id,
            "video_id": video_id,
            "segment_start_ms": segment_start_ms,
            "segment_end_ms": segment_end_ms,
            "exp": expires_at.timestamp(),
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return f"{_b64url(body)}.{_b64url(signature)}"

    def _verify(self, token: str) -> dict[str, object] | None:
        try:
            body_b64, sig_b64 = token.split(".", 1)
            body = _b64url_decode(body_b64)
            signature = _b64url_decode(sig_b64)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self._key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or _now().timestamp() > exp:
            return None
        return payload
