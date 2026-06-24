"""Local dev seeding (application/seed.py).

Seeds a minimal admin principal, a public-area access policy, and a sample
camera/location so the local CLI/API have a usable dataset. Local-only
convenience (auth §1 allows admin-preset principals for MVP); production would
provision principals/policies via proper admin flows.
"""

from __future__ import annotations

from cctv_memory.contracts.auth import AccessPolicy, AccessPolicyRules, Principal
from cctv_memory.contracts.video import CameraDevice, CameraLocation
from cctv_memory.domain.enums import PrincipalType, SecurityLevel
from cctv_memory.domain.policies import build_auto_location
from cctv_memory.repositories.camera import CameraRepository
from cctv_memory.repositories.principal import (
    AccessPolicyRepository,
    PrincipalRepository,
)

DEV_PRINCIPAL_ID = "user_admin"
DEV_POLICY_ID = "policy_public_area"
DEV_ROLE = "security_admin"


def seed_local_defaults(
    principals: PrincipalRepository,
    policies: AccessPolicyRepository,
    cameras: CameraRepository,
) -> None:
    """Idempotently seed a dev principal, policy, and sample camera/location."""
    # Pre-create the shared auto-location that hosts lazily-provisioned unregistered
    # cameras. Doing it once here (real `init` path) removes the highest-frequency
    # cold-start race: concurrent first-provision of `loc_auto_unregistered` by
    # multiple jobs (task cctv-memory-20260617-1118, B1). Idempotent: upsert keeps
    # an existing row untouched.
    cameras.upsert_location(build_auto_location())
    policies.upsert_access_policy(
        AccessPolicy(
            access_policy_id=DEV_POLICY_ID,
            name="Public Area",
            security_level=SecurityLevel.INTERNAL,
            rules=AccessPolicyRules(allowed_roles=[DEV_ROLE]),
        )
    )
    if principals.get_principal(DEV_PRINCIPAL_ID) is None:
        principals.create_principal(
            Principal(
                principal_id=DEV_PRINCIPAL_ID,
                principal_type=PrincipalType.ADMIN,
                display_name="Local Admin",
                roles=[DEV_ROLE],
            )
        )
    cameras.upsert_location(
        CameraLocation(
            location_id="loc_lobby_01",
            area="lobby",
            access_policy_id=DEV_POLICY_ID,
            security_level=SecurityLevel.INTERNAL,
        )
    )
    cameras.upsert_camera(
        CameraDevice(
            camera_id="cam_lobby_01",
            camera_name="Lobby Cam",
            location_id="loc_lobby_01",
            access_policy_id=DEV_POLICY_ID,
            status="active",
        )
    )
