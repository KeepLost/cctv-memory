"""Add analysis_timeline_events table.

Revision ID: d4e5f6a7b890
Revises: c1d2e3f4a590
Create Date: 2026-06-24 12:48:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b890"
down_revision: str | None = "c1d2e3f4a590"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_timeline_events",
        sa.Column("timeline_event_id", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("span_id", sa.String(), nullable=True),
        sa.Column("parent_span_id", sa.String(), nullable=True),
        sa.Column("analysis_job_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("scale_task_id", sa.String(), nullable=True),
        sa.Column("unit_id", sa.String(), nullable=True),
        sa.Column("model_call_id", sa.String(), nullable=True),
        sa.Column("video_id", sa.String(), nullable=True),
        sa.Column("analysis_scale", sa.String(), nullable=True),
        sa.Column("unit_kind", sa.String(), nullable=True),
        sa.Column("segment_start_ms", sa.Integer(), nullable=True),
        sa.Column("segment_end_ms", sa.Integer(), nullable=True),
        sa.Column("event_name", sa.String(), nullable=False),
        sa.Column("event_phase", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("correlation_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("timeline_event_id"),
    )
    with op.batch_alter_table("analysis_timeline_events", schema=None) as batch_op:
        batch_op.create_index(
            "idx_timeline_job_time", ["analysis_job_id", "occurred_at"], unique=False
        )
        batch_op.create_index(
            "idx_timeline_unit_time", ["unit_id", "occurred_at"], unique=False
        )
        batch_op.create_index(
            "idx_timeline_model_call", ["model_call_id", "occurred_at"], unique=False
        )
        batch_op.create_index(
            "idx_timeline_trace_time", ["trace_id", "occurred_at"], unique=False
        )
        batch_op.create_index(
            "idx_timeline_event_name_time", ["event_name", "occurred_at"], unique=False
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
    with op.batch_alter_table("analysis_timeline_events", schema=None) as batch_op:
        batch_op.drop_index("idx_timeline_event_name_time")
        batch_op.drop_index("idx_timeline_trace_time")
        batch_op.drop_index("idx_timeline_model_call")
        batch_op.drop_index("idx_timeline_unit_time")
        batch_op.drop_index("idx_timeline_job_time")
    op.drop_table("analysis_timeline_events")
