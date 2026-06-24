"""BackupPort (repository-port-contract §13, backup-export-contract).

Abstraction over consistent DB backup + validated restore. The application
BackupService depends on this port; the SQLite implementation lives in
``infrastructure/db/backup.py``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BackupPort(Protocol):
    """Consistent backup + checksum + validated restore."""

    def backup_to(self, out_path: str) -> str:
        """Create a consistent backup at ``out_path``; return its sha256 checksum."""
        ...

    def checksum(self, path: str) -> str:
        """Return the sha256 of an existing backup file."""
        ...

    def table_counts(self) -> dict[str, int]:
        """Return row counts per user table (for the manifest)."""
        ...

    def restore_from(self, backup_path: str, expected_checksum: str) -> None:
        """Validate checksum then replace the target DB. Raises on bad checksum."""
        ...
