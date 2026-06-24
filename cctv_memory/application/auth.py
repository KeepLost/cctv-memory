"""Authorization application service (application/auth.py).

Resolves a verified Principal and computes its AuthorizedScope by reading
principal/policy/camera repositories and applying domain policy. Identity is
NEVER taken from a request body (authorization-policy-contract §0,
api-and-service-runtime-design §2.2): the caller supplies only a principal id
that was already verified by the runtime (dev resolver / header / CLI default).

This service contains orchestration only; the scope math lives in
``domain.policies`` and stays infrastructure-free.
"""

from __future__ import annotations

from cctv_memory.contracts.auth import AuthorizedScope, Principal
from cctv_memory.domain import policies
from cctv_memory.domain.exceptions import AuthorizationError, PrincipalNotFoundError
from cctv_memory.repositories.camera import CameraRepository
from cctv_memory.repositories.principal import (
    AccessPolicyRepository,
    PrincipalRepository,
)


class AuthorizationService:
    """Resolve principals and compute AuthorizedScope (fail-closed)."""

    def __init__(
        self,
        principals: PrincipalRepository,
        policies_repo: AccessPolicyRepository,
        cameras: CameraRepository,
    ) -> None:
        self._principals = principals
        self._policies = policies_repo
        self._cameras = cameras

    def resolve_principal(self, principal_id: str) -> Principal:
        """Return an active principal or raise.

        A disabled / missing principal must not access business APIs (auth §1).
        """
        principal = self._principals.get_principal(principal_id)
        if principal is None:
            raise PrincipalNotFoundError(f"principal {principal_id} not found")
        if principal.status != "active":
            raise AuthorizationError(f"principal {principal_id} is not active")
        return principal

    def authorized_scope_for(self, principal: Principal) -> AuthorizedScope:
        """Compute the AuthorizedScope for ``principal`` (auth §4, fail closed)."""
        all_policies = self._policies.list_access_policies()
        # MVP scale: read all locations/cameras (small dataset) then filter in policy.
        locations = self._cameras.list_locations(limit=10_000).items
        cameras = self._cameras.list_cameras(limit=10_000).items
        return policies.compute_authorized_scope(
            principal=principal,
            policies=all_policies,
            locations=locations,
            cameras=cameras,
        )
