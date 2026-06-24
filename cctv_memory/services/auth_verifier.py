"""Abstract service port: auth verifier (module-map §2.4, runtime-design §2.2).

The auth verifier turns request-borne *credentials* (extracted from transport
headers, NEVER from the request body) into a ``principal_id``. This is the single
seam where "who is calling" enters the server.

Design intent (api-and-service-runtime-design §2.2):
- Identity MUST come from headers (e.g. ``Authorization: Bearer <token>`` in
  production, ``X-Principal-Id`` in the dev trusting verifier), never from the
  query/body.
- The verifier returns only a ``principal_id``; the application layer
  (``AuthorizationService``) is solely responsible for resolving the principal
  and computing the authorized scope. The verifier makes NO authorization
  decisions and grants NO capabilities.

MVP wiring uses a dev trusting verifier (``infrastructure/auth/dev_verifier.py``).
Production swaps in a token verifier here WITHOUT touching application/domain
code — both yield a ``principal_id`` and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RequestCredentials:
    """Transport-extracted credentials for identity verification.

    Carries only header-borne material. ``principal_id_header`` holds the dev
    ``X-Principal-Id`` value; ``authorization`` holds a future
    ``Authorization`` header (e.g. ``Bearer <token>``). Both are optional so the
    same shape serves the dev verifier and a future token verifier.
    """

    principal_id_header: str | None = None
    authorization: str | None = None


class AuthVerificationError(Exception):
    """Raised when credentials cannot be verified into a principal.

    Maps to an unauthenticated (401) response at the API boundary. The dev
    trusting verifier never raises this (it always yields the header value or a
    configured default); a production token verifier raises it on invalid /
    expired / missing tokens.
    """


@runtime_checkable
class AuthVerifierPort(Protocol):
    """Port: verify request credentials into a ``principal_id``."""

    def verify(self, credentials: RequestCredentials) -> str:
        """Return the caller's ``principal_id`` for the given credentials.

        Implementations MUST NOT make authorization decisions or read the request
        body. They either return a ``principal_id`` or raise
        ``AuthVerificationError``.
        """
        ...
