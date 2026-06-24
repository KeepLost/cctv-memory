"""Add analysis_units and model_call_logs tables.

Revision ID: a3f2e7c1d450
Revises: 05d5f971a583
Create Date: 2026-06-11 15:00:00.000000

Adds:
- analysis_units: persisted unit/work-unit layer for VLM windows/triggers
  (task-spec §3.A, table-schema-spec §4.4 new).
- model_call_logs: model-call observability record with textual IO and media refs
  (task-spec §3.E, table-schema-spec §4.5 new).

Both tables are append-only in normal operation; no changes to existing tables.
SQLite-compatible DDL; no ALTER TABLE on existing tables.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f2e7c1d450"
down_revision: str | None = "05d5f971a583"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_units",
        sa.Column("unit_id", sa.String(), nullable=False),
        sa.Column("analysis_job_id", sa.String(), nullable=False),
        sa.Column("scale_task_id", sa.String(), nullable=False),
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("analysis_scale", sa.String(), nullable=False),
        sa.Column("unit_kind", sa.String(), nullable=False),
        sa.Column("segment_start_ms", sa.Integer(), nullable=False),
        sa.Column("segment_end_ms", sa.Integer(), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("trigger_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("latest_model_call_id", sa.String(), nullable=True),
        sa.Column("successful_model_call_id", sa.String(), nullable=True),
        sa.Column("produced_record_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.CheckConstraint("segment_start_ms < segment_end_ms", name="ck_unit_time_order"),
        sa.PrimaryKeyConstraint("unit_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_analysis_unit_idempotency"),
    )
    with op.batch_alter_table("analysis_units", schema=None) as batch_op:
        batch_op.create_index("idx_units_scale_status", ["scale_task_id", "status"], unique=False)
        batch_op.create_index("idx_units_job_scale", ["analysis_job_id", "analysis_scale"], unique=False)

    op.create_table(
        "model_call_logs",
        sa.Column("model_call_id", sa.String(), nullable=False),
        sa.Column("analysis_job_id", sa.String(), nullable=False),
        sa.Column("scale_task_id", sa.String(), nullable=False),
        sa.Column("unit_id", sa.String(), nullable=False),
        sa.Column("analysis_scale", sa.String(), nullable=False),
        sa.Column("segment_start_ms", sa.Integer(), nullable=False),
        sa.Column("segment_end_ms", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("pipeline_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_text_input", sa.Text(), nullable=True),
        sa.Column("raw_text_output", sa.Text(), nullable=True),
        sa.Column("parsed_output_json", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(), nullable=True),
        sa.Column("payload_hash", sa.String(), nullable=True),
        sa.Column("response_hash", sa.String(), nullable=True),
        sa.Column("media_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("attempt_details_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("model_call_id"),
    )
    with op.batch_alter_table("model_call_logs", schema=None) as batch_op:
        batch_op.create_index("idx_model_calls_unit", ["unit_id", "created_at"], unique=False)
        batch_op.create_index("idx_model_calls_job", ["analysis_job_id", "analysis_scale"], unique=False)

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
    with op.batch_alter_table("model_call_logs", schema=None) as batch_op:
        batch_op.drop_index("idx_model_calls_job")
        batch_op.drop_index("idx_model_calls_unit")
    op.drop_table("model_call_logs")

    with op.batch_alter_table("analysis_units", schema=None) as batch_op:
        batch_op.drop_index("idx_units_job_scale")
        batch_op.drop_index("idx_units_scale_status")
    op.drop_table("analysis_units")
