"""SQLite backup/restore adapter (infrastructure/db/backup.py).

Uses the stdlib ``sqlite3`` online backup API (``Connection.backup``) to produce
a consistent copy of the database WITHOUT copying a live file mid-write
(backup-export-contract §3). No subprocess is spawned. Computes a sha256 checksum
of the backup file. Restore validates the checksum before replacing the target.

Infrastructure-only: application code depends on the BackupService, not this.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SqliteBackupAdapter:
    """Consistent SQLite backup + checksum + validated restore."""

    def __init__(self, sqlite_path: str) -> None:
        self._sqlite_path = Path(sqlite_path)

    def backup_to(self, out_path: str) -> str:
        """Create a consistent backup at ``out_path``; return its sha256 checksum.

        Uses the SQLite online backup API so concurrent readers/writers see a
        consistent snapshot (backup-export-contract §3). Never copies a live file.
        """
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(self._sqlite_path))
        try:
            dest = sqlite3.connect(str(out))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        return _sha256_file(out)

    def checksum(self, path: str) -> str:
        """Return the sha256 of an existing backup file."""
        return _sha256_file(Path(path))

    def table_counts(self) -> dict[str, int]:
        """Return row counts per user table for the manifest."""
        conn = sqlite3.connect(str(self._sqlite_path))
        try:
            cur = conn.cursor()
            tables = [
                row[0]
                for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for table in tables:
                try:
                    counts[table] = int(
                        cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    )
                except sqlite3.OperationalError:
                    # FTS5 shadow tables / virtual tables may not support COUNT.
                    continue
            return counts
        finally:
            conn.close()

    def restore_from(self, backup_path: str, expected_checksum: str) -> None:
        """Validate checksum then replace the target DB with the backup.

        Raises ``ValueError`` if the checksum does not match (caller maps to
        restore_failed). The replace is atomic-ish via a temp copy + os.replace.
        """
        backup = Path(backup_path)
        if not backup.exists():
            raise ValueError("backup file not found")
        actual = _sha256_file(backup)
        if actual != expected_checksum:
            raise ValueError("backup checksum mismatch")
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._sqlite_path.with_suffix(self._sqlite_path.suffix + ".restore-tmp")
        shutil.copyfile(backup, tmp)
        tmp.replace(self._sqlite_path)


class PostgresBackupAdapter:
    """PostgreSQL backup diagnostics adapter.

    Logical dump/restore is intentionally not faked in-process. Operators should
    use ``pg_dump``/``pg_restore`` or managed-database snapshots; this adapter
    supplies manifest table counts and explicit errors for unsupported file-copy
    backup calls.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def backup_to(self, out_path: str) -> str:
        raise NotImplementedError(
            "PostgreSQL backup_to is not implemented; use pg_dump or managed snapshots"
        )

    def checksum(self, path: str) -> str:
        return _sha256_file(Path(path))

    def table_counts(self) -> dict[str, int]:
        session = self._session_factory()
        try:
            table_rows = session.execute(
                text(
                    """
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public'
                    ORDER BY tablename
                    """
                )
            )
            counts: dict[str, int] = {}
            for row in table_rows:
                table = str(row.tablename)
                count = session.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
                counts[table] = int(count)
            return counts
        finally:
            session.close()

    def restore_from(self, backup_path: str, expected_checksum: str) -> None:
        raise NotImplementedError(
            "PostgreSQL restore_from is not implemented; use pg_restore after checksum validation"
        )
