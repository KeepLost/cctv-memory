"""Add pre_vlm_gate_logs table.

Revision ID: e6f7a8b9c012
Revises: d4e5f6a7b890
Create Date: 2026-07-10 00:04:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c012"
down_revision: str | None = "d4e5f6a7b890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pre_vlm_gate_logs",
        sa.Column("gate_log_id", sa.String(), nullable=False),
        sa.Column("analysis_job_id", sa.String(), nullable=False),
        sa.Column("scale_task_id", sa.String(), nullable=False),
        sa.Column("unit_id", sa.String(), nullable=False),
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("analysis_scale", sa.String(), nullable=False),
        sa.Column("unit_kind", sa.String(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("segment_start_ms", sa.Integer(), nullable=False),
        sa.Column("segment_end_ms", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        sa.Column("signals_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("frame_evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(), nullable=False),
        sa.Column("rule_config_hash", sa.String(), nullable=True),
        sa.Column("suppression_policy", sa.String(), nullable=True),
        sa.Column("media_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("artifact_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "segment_start_ms < segment_end_ms", name="ck_pre_vlm_gate_time_order"
        ),
        sa.PrimaryKeyConstraint("gate_log_id"),
    )
    with op.batch_alter_table("pre_vlm_gate_logs", schema=None) as batch_op:
        batch_op.create_index("idx_pre_vlm_gate_unit", ["unit_id", "created_at"], unique=False)
        batch_op.create_index(
            "idx_pre_vlm_gate_job", ["analysis_job_id", "analysis_scale"], unique=False
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
        batch_op.drop_index("idx_pre_vlm_gate_job")
        batch_op.drop_index("idx_pre_vlm_gate_unit")
    op.drop_table("pre_vlm_gate_logs")
