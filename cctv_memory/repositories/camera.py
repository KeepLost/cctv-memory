"""CameraRepository port (repository-port-contract §2)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cctv_memory.contracts.video import CameraDevice, CameraLocation
from cctv_memory.repositories.types import Page


@runtime_checkable
class CameraRepository(Protocol):
    """Camera/location management port.

    User-visible list/get must apply AuthZ at the service layer; admin paths may
    manage directly (repository-port-contract §2).
    """

    def get_location(self, location_id: str) -> CameraLocation | None: ...

    def list_locations(
        self, cursor: str | None = None, limit: int = 50
    ) -> Page[CameraLocation]: ...

    def upsert_location(self, location: CameraLocation) -> CameraLocation: ...

    def get_camera(self, camera_id: str) -> CameraDevice | None: ...

    def list_cameras(
        self, cursor: str | None = None, limit: int = 50
    ) -> Page[CameraDevice]: ...

    def upsert_camera(self, camera: CameraDevice) -> CameraDevice: ...
