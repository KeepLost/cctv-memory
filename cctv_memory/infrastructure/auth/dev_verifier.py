"""Dev trusting auth verifier (infrastructure/auth/dev_verifier.py).

The MVP / dev implementation of ``AuthVerifierPort``. It TRUSTS the
``X-Principal-Id`` header (or falls back to a configured default principal) and
performs NO cryptographic verification. This preserves the historical dev
behavior of the API layer exactly.

This is intentionally the only identity seam for local/closed-loop use. A
production deployment replaces this with a token verifier (e.g. verifying a
signed ``Authorization: Bearer`` token) WITHOUT any change to the api,
application, or domain layers — both implement ``AuthVerifierPort.verify`` and
return a ``principal_id``.
"""

from __future__ import annotations

from cctv_memory.services.auth_verifier import RequestCredentials


class TrustingHeaderVerifier:
    """Trust the ``X-Principal-Id`` header; fall back to a default principal.

    No token, no signature, no expiry — dev only. Implements
    ``AuthVerifierPort`` structurally (Protocol).
    """

    def __init__(self, *, default_principal_id: str = "user_admin") -> None:
        self._default_principal_id = default_principal_id

    def verify(self, credentials: RequestCredentials) -> str:
        return credentials.principal_id_header or self._default_principal_id
