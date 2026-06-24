"""Add idx_units_status_started for bounded orphan-running recovery.

Revision ID: b7c4d9e2f810
Revises: a3f2e7c1d450
Create Date: 2026-06-12 19:10:00.000000

Adds a composite index on analysis_units(status, started_at) so the orphan
-running recovery sweep (task cctv-memory-20260612-1854 §E) can find stale
``running`` units with an index-backed, bounded query
(``status='running' AND started_at < cutoff ORDER BY started_at LIMIT batch``)
instead of a full-table scan. No table/column changes; index-only migration.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4d9e2f810"
down_revision: str | None = "a3f2e7c1d450"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_units", schema=None) as batch_op:
        batch_op.create_index(
            "idx_units_status_started", ["status", "started_at"], unique=False
        )

    # Update schema_metadata last_migration_at.
    now = sa.func.strftime("%Y-%m-%dT%H:%M:%fZ", "now")
    schema_metadata = sa.table(
        "schema_metadata",
        sa.column("key", sa.String),
        sa.column("value", sa.String),
    )
    op.execute(
        schema_metadata.update()
        .where(sa.literal_column("key") == "last_migration_at")
        .values(value=now)
    )


def downgrade() -> None:
    with op.batch_alter_table("analysis_units", schema=None) as batch_op:
        batch_op.drop_index("idx_units_status_started")
