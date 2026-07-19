"""Add pre-VLM gate detector schema diagnostics.

Revision ID: f8a9b0c1d234
Revises: e6f7a8b9c012
Create Date: 2026-07-18 17:42:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a9b0c1d234"
down_revision: str | None = "e6f7a8b9c012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pre_vlm_gate_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_type", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("raw_text_output", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("parsed_output_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("validation_status", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("attempt_details_json", sa.Text(), nullable=False, server_default="[]")
        )

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
    with op.batch_alter_table("pre_vlm_gate_logs", schema=None) as batch_op:
        batch_op.drop_column("attempt_details_json")
        batch_op.drop_column("validation_status")
        batch_op.drop_column("parsed_output_json")
        batch_op.drop_column("raw_text_output")
        batch_op.drop_column("error_message")
        batch_op.drop_column("error_type")
