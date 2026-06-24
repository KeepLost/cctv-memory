"""Principal / AccessPolicy ports (repository-port-contract §9)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.contracts.auth import AccessPolicy, Principal


@runtime_checkable
class PrincipalRepository(Protocol):
    """Principal persistence port (repository-port-contract §9)."""

    def get_principal(self, principal_id: str) -> Principal | None: ...

    def get_principal_by_external_subject(
        self, external_subject_id: str
    ) -> Principal | None: ...

    def create_principal(self, principal: Principal) -> Principal: ...

    def set_principal_status(self, principal_id: str, status: str) -> None: ...


@runtime_checkable
class AccessPolicyRepository(Protocol):
    """AccessPolicy persistence port (repository-port-contract §9)."""

    def get_access_policy(self, access_policy_id: str) -> AccessPolicy | None: ...

    def list_access_policies(self) -> list[AccessPolicy]: ...

    def upsert_access_policy(self, policy: AccessPolicy) -> AccessPolicy: ...
