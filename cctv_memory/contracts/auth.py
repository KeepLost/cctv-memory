"""Auth contracts: Principal, AccessPolicy, AuthorizedScope (schema-contracts §4).

AuthorizedScope combination semantics follow authorization-policy-contract §4.1:
- resource dimensions are AND-combined;
- empty ``allowed_*`` arrays mean NO permission, not unlimited access;
- ambiguous/missing fields fail closed.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from cctv_memory.contracts.common import ContractModel
from cctv_memory.domain.enums import Capability, PrincipalType, SecurityLevel


class Principal(ContractModel):
    """Authenticated principal (schema-contracts §4.1)."""

    principal_id: str
    principal_type: PrincipalType
    tenant_id: str = "tenant_default"
    external_subject_id: str | None = None
    display_name: str
    status: str = "active"
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class AccessPolicyRules(ContractModel):
    """Access policy rules (authorization-policy-contract §3)."""

    allowed_roles: list[str] = Field(default_factory=list)
    allowed_groups: list[str] = Field(default_factory=list)
    allowed_principals: list[str] = Field(default_factory=list)
    denied_principals: list[str] = Field(default_factory=list)


class AccessPolicy(ContractModel):
    """Access policy (schema-contracts §4.2)."""

    access_policy_id: str
    tenant_id: str = "tenant_default"
    name: str
    security_level: SecurityLevel
    rules: AccessPolicyRules = Field(default_factory=AccessPolicyRules)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AuthorizedScope(ContractModel):
    """Computed authorization scope (schema-contracts §4.3).

    Empty ``allowed_*`` lists mean the dimension grants NO access. To express
    "do not restrict by this dimension" an explicit admin/service bypass
    mechanism must be used instead (authorization-policy-contract §4.1).
    """

    tenant_id: str
    principal_id: str
    allowed_camera_ids: list[str] = Field(default_factory=list)
    allowed_location_ids: list[str] = Field(default_factory=list)
    allowed_access_policy_ids: list[str] = Field(default_factory=list)
    max_security_level: SecurityLevel
    capabilities: list[Capability] = Field(default_factory=list)
    scope_hash: str

    def has_capability(self, capability: Capability) -> bool:
        """Return True if this scope grants ``capability`` (interface gate only)."""
        return capability in self.capabilities

    def permits_resource(
        self,
        *,
        tenant_id: str,
        camera_id: str,
        location_id: str,
        access_policy_id: str,
        security_level: SecurityLevel,
    ) -> bool:
        """Return True only if every resource dimension is satisfied (AND), fail closed.

        Implements authorization-policy-contract §4.1. Empty allowed-list on any
        resource dimension denies access for that dimension.
        """
        if tenant_id != self.tenant_id:
            return False
        if camera_id not in self.allowed_camera_ids:
            return False
        if location_id not in self.allowed_location_ids:
            return False
        if access_policy_id not in self.allowed_access_policy_ids:
            return False
        return self.max_security_level.allows(security_level)
